"""Executable safety proofs for the PROPOSED Stage D operator toolkit.

Stage D is a PROPOSAL: nothing in `scripts/release/stage-d/` has been run
against production. These tests are what make the proposal reviewable —
they prove offline, with no network, no gcloud and no provider call, that
every gate REFUSES the situations it claims to refuse:

  - baseline drift (database rows and Worker executions, in BOTH
    directions — a vanished row fails exactly like an extra one);
  - MILO_ENABLE_CATALOG_EXECUTION enabled on either surface;
  - an unexpected active/unverifiable Worker execution;
  - a wrong (unpinned) release image on either surface;
  - a reused idempotency key — including the consumed Stage C keys and the
    prepared Government capture's key;
  - a provider key present on the API in any form;
  - missing cleanup: a disposable probe job surviving the lockdown.

Plus the two Stage-D-specific invariants:
  - the prepared Government capture run is never claimed or executed; and
  - its resolution is a guarded compare-and-set, EXECUTED HERE against a
    real ephemeral PostgreSQL to prove it applies exactly once, refuses
    every drifted pre-state, and is never an unconditional UPDATE.
"""

from __future__ import annotations

import base64
import gzip
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
STAGE_D = REPO / "scripts" / "release" / "stage-d"
STAGE_C = REPO / "scripts" / "release" / "stage-c"

RELEASE_SHA = "84cd8696119c24662a954d0f0e23195268dab23f"
REGISTRY = "us-central1-docker.pkg.dev/big-cabinet-457321-t7/milo-agent"
STAGE_D_KEY = "stage-d-expansion-1-20260918-01"
EXPECTED_PRIOR_RUNS = "7"
EXPECTED_PRIOR_EXECUTIONS = "7"

GOV_RUN_ID = "555101dc-46f6-4048-bd67-efccbc98f528"
GOV_KEY = "catalog-government-capture-20260919-01"
GOV_OPERATION = "catalog.government.capture"
GOV_CONVERSATION_ID = "79ee2539-511c-4485-b470-c5539a22eba8"
GOV_REQUESTED_BY = "35e3c271-e2f0-44f1-b69a-066f13121e56"

# The ACCEPTED release digests. The release identity is the digest, not
# the tag: this repository cannot reproduce a build byte-for-byte
# (mutable python:3.12-slim base, unpinned openai>=1.30.0, no lockfile),
# so a rebuild pushed under the same tag would replace the accepted
# image rather than re-prove it.
API_DIGEST = "sha256:04275e81995d7bbaf23d0e71e71c2ac83adf37f45eca8686ddb812050a18caa6"
WORKER_DIGEST = "sha256:d3743e5a8dabc3f663970abe83886ea91b030ad7b339e1178d0ab5efad8f64b5"
# A digest the Worker tag really resolved to earlier in this project
# (execution milo-agent-worker-bw8kj) — proof the hazard is not theoretical.
STALE_WORKER_DIGEST = "sha256:2314852868a8abca211731960a178e7372997cc64674407d1c5119c86de9b265"

# Every idempotency key production has already seen. None may be reused.
CONSUMED_KEYS = (
    "stage-c-smoke-0001",
    "stage-c-smoke-attempt-7-20260819",
    "swarm-v2-smoke-20260824-04c1094",
    "swarm-v2-smoke-attempt-2-20260824-04c1094",
    "swarm-v2-smoke-20260824-4fecdfe-01",
    "swarm-v2-smoke-20260825-4dbdcd6-01",
    GOV_KEY,
)

CAPS = (
    "MILO_MAX_MODEL_CALLS_PER_RUN=150,MILO_MAX_INPUT_TOKENS_PER_RUN=500000,"
    "MILO_MAX_OUTPUT_TOKENS_PER_RUN=120000,MILO_MAX_TOTAL_TOKENS_PER_RUN=600000,"
    "MILO_MAX_ESTIMATED_COST_PER_RUN=3.00,MILO_MAX_COST_PER_RUN=1.00,"
    "MILO_MAX_RUN_DURATION_SECONDS=1800,MILO_MAX_RETRIES=15,MILO_MAX_AGENT_STEPS=56,"
    "MILO_MAX_CONCURRENT_RUNS_PER_USER=1,MILO_MAX_CONCURRENT_RUNS_PER_PROJECT=1,"
    "MILO_DAILY_USER_BUDGET=4.00,MILO_DAILY_PROJECT_BUDGET=4.00,MILO_ESTIMATED_COST_PER_CALL=0.02"
)

# Byte-for-byte the Stage C Attempt 7 envelope. Production currently
# carries MILO_PROVIDER_MAX_CONCURRENCY=8; restoring 2 is a tightening.
PROVIDER_LIMITS = (
    "MILO_PROVIDER_MAX_CONCURRENCY=2,MILO_PROVIDER_RPM_LIMIT=350,"
    "MILO_PROVIDER_TPM_LIMIT=2400000,MILO_PROVIDER_MAX_RATE_LIMIT_RETRIES=5,"
    "MILO_PROVIDER_MAX_BACKPRESSURE_WAIT_SECONDS=240,"
    "MILO_PROVIDER_BACKOFF_BASE_SECONDS=2,MILO_PROVIDER_BACKOFF_MAX_SECONDS=30"
)

# The Stage C caps, as the consumed Stage C authorization pinned them. No
# Stage D cap may ever exceed its Stage C counterpart.
STAGE_C_CAPS = {
    "MILO_MAX_MODEL_CALLS_PER_RUN": 200,
    "MILO_MAX_INPUT_TOKENS_PER_RUN": 700000,
    "MILO_MAX_OUTPUT_TOKENS_PER_RUN": 250000,
    "MILO_MAX_TOTAL_TOKENS_PER_RUN": 900000,
    "MILO_MAX_ESTIMATED_COST_PER_RUN": 4.00,
    "MILO_MAX_COST_PER_RUN": 3.00,
    "MILO_MAX_RUN_DURATION_SECONDS": 3300,
    "MILO_MAX_RETRIES": 15,
    "MILO_MAX_AGENT_STEPS": 60,
    "MILO_MAX_CONCURRENT_RUNS_PER_USER": 1,
    "MILO_MAX_CONCURRENT_RUNS_PER_PROJECT": 1,
    "MILO_DAILY_USER_BUDGET": 5.00,
    "MILO_DAILY_PROJECT_BUDGET": 5.00,
    "MILO_ESTIMATED_COST_PER_CALL": 0.02,
}

# Stage C Attempt 7's verified production evidence — the derivation base.
ATTEMPT7 = {
    "model_calls": 84,
    "input_tokens": 277882,
    "output_tokens": 34136,
    "total_tokens": 312018,
    "actual_cost": 0.252069,
    "agent_steps": 32,
    "elapsed_seconds": 934.235,
}


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def parse_pairs(raw: str) -> dict[str, str]:
    return dict(pair.split("=", 1) for pair in raw.split(","))


@pytest.fixture
def gw(monkeypatch):
    monkeypatch.setenv("STAGE_D_API_URL", "https://api.invalid")
    monkeypatch.setenv("STAGE_D_USER_ID", "user-1")
    monkeypatch.setenv("STAGE_D_CONVERSATION_ID", "conv-1")
    monkeypatch.setenv("STAGE_D_RUN_ID", "run-1")
    monkeypatch.setenv("STAGE_D_IDEMPOTENCY_KEY", STAGE_D_KEY)
    monkeypatch.setenv("STAGE_D_GOV_CAPTURE_RUN_ID", GOV_RUN_ID)
    monkeypatch.setenv("STAGE_D_GOV_CAPTURE_KEY", GOV_KEY)
    monkeypatch.setenv("STAGE_D_POLL_SECONDS", "5")
    monkeypatch.setenv("STAGE_D_POLL_INTERVAL_SECONDS", "0")
    return load_module("stage_d_probe_gateway", STAGE_D / "probe_gateway.py")


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://db.invalid")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "offline-placeholder")
    monkeypatch.setenv("STAGE_D_GOV_CAPTURE_RUN_ID", GOV_RUN_ID)
    monkeypatch.setenv("STAGE_D_GOV_CAPTURE_KEY", GOV_KEY)
    monkeypatch.setenv("STAGE_D_IDEMPOTENCY_KEY", STAGE_D_KEY)
    return load_module("stage_d_probe_db", STAGE_D / "probe_db.py")


# ---------------------------------------------------------------------------
# A. Pinned constants — the authorization cannot be widened or redirected
# ---------------------------------------------------------------------------

AUTHORIZED = {
    "STAGE_D_PROJECT": "big-cabinet-457321-t7",
    "STAGE_D_REGION": "us-central1",
    "STAGE_D_RELEASE_SHA": RELEASE_SHA,
    "STAGE_D_IDEMPOTENCY_KEY": STAGE_D_KEY,
    "STAGE_D_EXPECTED_PRIOR_RUNS": EXPECTED_PRIOR_RUNS,
    "STAGE_D_EXPECTED_PRIOR_EXECUTIONS": EXPECTED_PRIOR_EXECUTIONS,
    "STAGE_D_ACCEPTABLE_TERMINAL_STATES": "completed",
    "STAGE_D_WORKER_PROVIDER_LIMITS": PROVIDER_LIMITS,
    "STAGE_D_GOV_CAPTURE_RUN_ID": GOV_RUN_ID,
    "STAGE_D_GOV_CAPTURE_KEY": GOV_KEY,
    "STAGE_D_GOV_CAPTURE_OPERATION": GOV_OPERATION,
    "STAGE_D_GOV_CAPTURE_CONVERSATION_ID": GOV_CONVERSATION_ID,
    "STAGE_D_GOV_CAPTURE_REQUESTED_BY": GOV_REQUESTED_BY,
    "STAGE_D_API_IMAGE_DIGEST": API_DIGEST,
    "STAGE_D_WORKER_IMAGE_DIGEST": WORKER_DIGEST,
    "STAGE_D_WORKFLOW_KEY": "vehicle_catalog_v1",
}

EXTRA_DUMPED = ["STAGE_D_CAPS", "STAGE_D_REGISTRY", "STAGE_D_API_URL",
                "STAGE_D_PROJECT_SLUG", "STAGE_D_FORBIDDEN_PROJECT_IDS"]


def source_stage_d_env(overrides=None):
    """Source stage-d-env.sh the way every step script does and dump the
    resulting authorized values (empty env apart from the overrides)."""
    dump = "; ".join(f'echo "{k}=${{{k}}}"' for k in list(AUTHORIZED) + EXTRA_DUMPED)
    return subprocess.run(
        ["bash", "-c", f"set -euo pipefail; source '{STAGE_D / 'stage-d-env.sh'}'; {dump}"],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", **(overrides or {})},
        timeout=60,
    )


def test_env_exports_the_exact_authorized_constants():
    result = source_stage_d_env()
    assert result.returncode == 0, result.stderr
    for key, value in AUTHORIZED.items():
        assert f"{key}={value}" in result.stdout
    assert f"STAGE_D_CAPS={CAPS}" in result.stdout


def test_env_pins_all_seven_provider_limits_exactly():
    result = source_stage_d_env()
    assert result.returncode == 0, result.stderr
    line = next(r for r in result.stdout.splitlines() if r.startswith("STAGE_D_WORKER_PROVIDER_LIMITS="))
    pinned = parse_pairs(line.split("=", 1)[1])
    assert pinned == {
        # Restores the Attempt 7 value; production currently carries 8.
        "MILO_PROVIDER_MAX_CONCURRENCY": "2",
        "MILO_PROVIDER_RPM_LIMIT": "350",
        "MILO_PROVIDER_TPM_LIMIT": "2400000",
        "MILO_PROVIDER_MAX_RATE_LIMIT_RETRIES": "5",
        "MILO_PROVIDER_MAX_BACKPRESSURE_WAIT_SECONDS": "240",
        "MILO_PROVIDER_BACKOFF_BASE_SECONDS": "2",
        "MILO_PROVIDER_BACKOFF_MAX_SECONDS": "30",
    }
    # Provider scheduling must NOT ride along in STAGE_D_CAPS (caps are
    # applied and verified on BOTH surfaces; the envelope is worker-only).
    caps_line = next(r for r in result.stdout.splitlines() if r.startswith("STAGE_D_CAPS="))
    assert "MILO_PROVIDER_" not in caps_line


@pytest.mark.parametrize("var,hostile", [
    ("STAGE_D_PROJECT", "attacker-project-123"),
    ("STAGE_D_REGION", "europe-west1"),
    ("STAGE_D_RELEASE_SHA", "791f7af9" + "0" * 32),
    ("STAGE_D_RELEASE_SHA", "88224bccc836f80f3dc1d173306a1aa63cddcc7a"),  # the Stage C release
    ("STAGE_D_EXPECTED_PRIOR_RUNS", "0"),   # pretending the history is empty
    ("STAGE_D_EXPECTED_PRIOR_RUNS", "6"),   # pretending the capture row is gone
    ("STAGE_D_EXPECTED_PRIOR_RUNS", "8"),
    ("STAGE_D_EXPECTED_PRIOR_EXECUTIONS", "0"),
    ("STAGE_D_EXPECTED_PRIOR_EXECUTIONS", "1"),  # the stale Stage C baseline
    ("STAGE_D_EXPECTED_PRIOR_EXECUTIONS", "8"),
    ("STAGE_D_ACCEPTABLE_TERMINAL_STATES", "completed,failed,budget_exhausted"),
    ("STAGE_D_GOV_CAPTURE_RUN_ID", "8b4a4277-fdf0-41b2-8515-d7e1d50e441b"),
    ("STAGE_D_WORKFLOW_KEY", "swarm_v2"),
])
def test_env_refuses_inherited_override_of_authorized_constants(var, hostile):
    result = source_stage_d_env({var: hostile})
    assert result.returncode != 0, f"{var} override was silently accepted"
    assert "STAGE D REFUSED" in result.stderr
    assert var in result.stderr
    assert f"{var}={hostile}" not in result.stdout


@pytest.mark.parametrize("var,hostile", [
    ("STAGE_D_CAPS", "MILO_MAX_COST_PER_RUN=300.00"),
    ("STAGE_D_WORKER_PROVIDER_LIMITS", "MILO_PROVIDER_MAX_CONCURRENCY=100,MILO_PROVIDER_RPM_LIMIT=500"),
    # The live drift must not be smuggled in as the pinned envelope.
    ("STAGE_D_WORKER_PROVIDER_LIMITS", PROVIDER_LIMITS.replace("CONCURRENCY=2", "CONCURRENCY=8")),
    ("STAGE_D_REGISTRY", "us-central1-docker.pkg.dev/attacker-project/evil"),
    ("STAGE_D_API_IMAGE_DIGEST", "sha256:" + "0" * 64),
    ("STAGE_D_WORKER_IMAGE_DIGEST", STALE_WORKER_DIGEST),
    ("STAGE_D_GOV_CAPTURE_OPERATION", "something.else"),
    ("STAGE_D_GOV_CAPTURE_REQUESTED_BY", "00000000-0000-0000-0000-000000000000"),
    ("STAGE_D_API_URL", "https://attacker.example.run.app"),
    ("STAGE_D_IDEMPOTENCY_KEY", "second-run-key"),
    ("STAGE_D_FORBIDDEN_PROJECT_IDS", "00000000-0000-0000-0000-000000000000"),
])
def test_env_refuses_redirection_of_derived_authorization_surface(var, hostile):
    result = source_stage_d_env({var: hostile})
    assert result.returncode != 0, f"{var} override was silently accepted"
    assert "STAGE D REFUSED" in result.stderr


@pytest.mark.parametrize("consumed", CONSUMED_KEYS)
def test_env_refuses_every_already_consumed_idempotency_key(consumed):
    """REUSED IDEMPOTENCY KEY — refusal at the authorization surface."""
    result = source_stage_d_env({"STAGE_D_IDEMPOTENCY_KEY": consumed})
    assert result.returncode != 0, f"consumed key {consumed} was silently accepted"
    assert "STAGE D REFUSED" in result.stderr


def test_stage_d_key_is_fresh_and_appears_in_no_consumed_form():
    result = source_stage_d_env()
    assert result.returncode == 0, result.stderr
    assert f"STAGE_D_IDEMPOTENCY_KEY={STAGE_D_KEY}" in result.stdout
    key_line = next(r for r in result.stdout.splitlines() if r.startswith("STAGE_D_IDEMPOTENCY_KEY="))
    for consumed in CONSUMED_KEYS:
        assert consumed not in key_line


def test_env_accepts_values_equal_to_the_authorized_constants():
    """Re-sourcing (every step script sources the file) must stay idempotent."""
    result = source_stage_d_env(dict(AUTHORIZED))
    assert result.returncode == 0, result.stderr


def test_step_scripts_abort_when_env_refuses():
    for script in ("01-verify-release-images.sh", "05-execute-run.sh", "06-collect-evidence.sh",
                   "07-post-run-lockdown.sh", "kill-switch.sh", "03b-verify-stage-d-posture.sh"):
        text = (STAGE_D / script).read_text()
        assert "set -euo pipefail" in text
        assert "source ./stage-d-env.sh" in text
    result = subprocess.run(
        ["bash", str(STAGE_D / "03b-verify-stage-d-posture.sh")],
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "STAGE_D_PROJECT": "attacker-project-123"},
        timeout=60,
    )
    assert result.returncode != 0
    assert "STAGE D REFUSED" in result.stderr
    assert "gcloud" not in result.stdout


def test_stage_d_never_sources_or_edits_the_consumed_stage_c_toolkit():
    """The Stage C authorization is spent; Stage D is self-contained."""
    for path in STAGE_D.iterdir():
        if not path.is_file() or path.suffix not in (".sh", ".py"):
            continue
        # Prose may DISCUSS Stage C; executable lines may not touch it.
        code = [line for line in path.read_text().splitlines()
                if line.strip() and not line.lstrip().startswith("#")]
        for marker in ("stage-c-env.sh", "STAGE_C_CAPS", "STAGE_C_IDEMPOTENCY_KEY",
                       "release/stage-c", "STAGE_C_WORKER_PROVIDER_LIMITS"):
            offenders = [line for line in code if marker in line]
            assert not offenders, f"{path.name} executable line reuses Stage C: {offenders}"
    # And the Stage C directory itself is untouched by this change. The
    # base is the pinned release SHA (this branch's base), not a possibly
    # stale origin/main.
    diff = subprocess.run(
        ["git", "diff", "--name-only", RELEASE_SHA, "--", "scripts/release/stage-c"],
        cwd=REPO, capture_output=True, text=True, timeout=60,
    )
    if diff.returncode == 0:
        assert diff.stdout.strip() == "", f"Stage C toolkit modified: {diff.stdout}"
    else:
        pytest.skip("the pinned release SHA is not available in this checkout")


def test_no_committed_line_enables_an_execution_flag():
    """Policy: enabling stays a deliberate manual operator action."""
    enable_re = re.compile(
        r"MILO_ENABLE_(RUN_CREATION|PAID_EXECUTION|CATALOG_EXECUTION|PROPOSAL_MUTATIONS"
        r"|PROPOSAL_READS|RUN_CANCELLATION|EXECUTION_CONTROL)\s*[:=]\s*['\"]?(1|true|yes|on)['\"]?",
        re.I,
    )
    for path in STAGE_D.iterdir():
        if path.is_file() and path.suffix in (".sh", ".py"):
            assert not enable_re.search(path.read_text()), f"{path.name} enables an execution flag"


# ---------------------------------------------------------------------------
# B. Cap derivation — from Stage C evidence, and nothing is raised
# ---------------------------------------------------------------------------

def stage_d_caps() -> dict[str, float]:
    return {k: float(v) for k, v in parse_pairs(CAPS).items()}


def test_env_caps_match_the_documented_stage_d_values():
    result = source_stage_d_env()
    line = next(r for r in result.stdout.splitlines() if r.startswith("STAGE_D_CAPS="))
    assert parse_pairs(line.split("=", 1)[1]) == parse_pairs(CAPS)


@pytest.mark.parametrize("name,stage_c_value", sorted(STAGE_C_CAPS.items()))
def test_no_stage_d_cap_exceeds_its_stage_c_counterpart(name, stage_c_value):
    """The core promise: Stage D raises no Stage C limit."""
    assert stage_d_caps()[name] <= stage_c_value, f"{name} was RAISED above the Stage C value"


def test_every_cap_stage_c_pinned_is_still_pinned_by_stage_d():
    """No cap may be silently dropped — an absent cap is an unbounded one."""
    assert set(stage_d_caps()) == set(STAGE_C_CAPS)


@pytest.mark.parametrize("name,observed", [
    ("MILO_MAX_MODEL_CALLS_PER_RUN", ATTEMPT7["model_calls"]),
    ("MILO_MAX_INPUT_TOKENS_PER_RUN", ATTEMPT7["input_tokens"]),
    ("MILO_MAX_OUTPUT_TOKENS_PER_RUN", ATTEMPT7["output_tokens"]),
    ("MILO_MAX_TOTAL_TOKENS_PER_RUN", ATTEMPT7["total_tokens"]),
    ("MILO_MAX_COST_PER_RUN", ATTEMPT7["actual_cost"]),
    ("MILO_MAX_AGENT_STEPS", ATTEMPT7["agent_steps"]),
    ("MILO_MAX_RUN_DURATION_SECONDS", ATTEMPT7["elapsed_seconds"]),
])
def test_each_cap_keeps_at_least_the_documented_headroom_over_attempt_7(name, observed):
    """Derived from evidence, but never so tight that the run false-fails.

    The documented rule is >= 1.75x the Attempt 7 observation.
    """
    assert stage_d_caps()[name] >= 1.75 * observed, f"{name} leaves less than 1.75x headroom"


def test_estimated_cost_ceiling_admits_exactly_the_call_cap_and_no_more():
    """backend/budget.py rejects when estimated + per_call > the ceiling."""
    caps = stage_d_caps()
    per_call = caps["MILO_ESTIMATED_COST_PER_CALL"]
    ceiling = caps["MILO_MAX_ESTIMATED_COST_PER_RUN"]
    max_calls = int(caps["MILO_MAX_MODEL_CALLS_PER_RUN"])
    assert round((max_calls - 1) * per_call + per_call, 6) <= ceiling, "the call cap cannot be reached"
    assert round(max_calls * per_call + per_call, 6) > ceiling, "more calls than the cap could reserve"


def test_joint_token_ceiling_binds_before_the_separate_token_caps():
    caps = stage_d_caps()
    assert caps["MILO_MAX_TOTAL_TOKENS_PER_RUN"] < (
        caps["MILO_MAX_INPUT_TOKENS_PER_RUN"] + caps["MILO_MAX_OUTPUT_TOKENS_PER_RUN"]
    )


def test_daily_budgets_cannot_bind_before_the_per_run_estimated_ceiling():
    caps = stage_d_caps()
    for daily in ("MILO_DAILY_USER_BUDGET", "MILO_DAILY_PROJECT_BUDGET"):
        assert caps[daily] > caps["MILO_MAX_ESTIMATED_COST_PER_RUN"]


def test_run_duration_cap_stays_below_the_cloud_run_job_timeout():
    """The worker job's timeoutSeconds is 3600 (verified read-only)."""
    assert stage_d_caps()["MILO_MAX_RUN_DURATION_SECONDS"] < 3600


def test_retry_allowance_is_held_not_tightened():
    """Stage C Attempt 6 FAILED at RETRY_LIMIT_REACHED after provider 429s.

    15 retries together with the pinned provider envelope is the pair that
    produced the successful Attempt 7; tightening it would reintroduce that
    failure mode for no exposure benefit.
    """
    assert stage_d_caps()["MILO_MAX_RETRIES"] == STAGE_C_CAPS["MILO_MAX_RETRIES"]


def test_env_documents_the_reason_for_every_non_tightened_cap():
    text = (STAGE_D / "stage-d-env.sh").read_text()
    assert "RETRY_LIMIT_REACHED" in text
    assert "MILO_MAX_OUTPUT_TOKENS_PER_RUN keeps a 3.5x margin" in text
    for observed in ("84", "277,882", "34,136", "312,018", "0.252069", "934.235"):
        assert observed in text, f"the Attempt 7 evidence value {observed} is not cited"


# ---------------------------------------------------------------------------
# C. verify_caps.py — wrong image, catalog flag, provider key on API, drift
# ---------------------------------------------------------------------------

def env_entries(pairs: str) -> list[dict]:
    return [{"name": k, "value": v} for k, v in parse_pairs(pairs).items()]


def worker_spec(*, caps=CAPS, provider=PROVIDER_LIMITS, image=None, bind_key=True, extra=None):
    env = env_entries(caps) + env_entries(provider) + [
        {"name": "MILO_ENABLE_PAID_EXECUTION", "value": "true"},
        {"name": "MILO_ENABLE_CATALOG_EXECUTION", "value": "false"},
    ]
    if bind_key:
        env.append({"name": "KIMI_API_KEY", "valueFrom": {"secretKeyRef": {"key": "latest", "name": "KIMI_API_KEY"}}})
    env.extend(extra or [])
    return {"spec": {"template": {"spec": {"template": {"spec": {"containers": [
        {"image": image or f"{REGISTRY}/worker:{RELEASE_SHA}", "env": env}
    ]}}}}}}


def api_spec(*, caps=CAPS, image=None, extra=None):
    env = env_entries(caps) + [
        {"name": "MILO_ENABLE_PAID_EXECUTION", "value": "false"},
        {"name": "MILO_ENABLE_RUN_CREATION", "value": "true"},
        {"name": "JOB_LAUNCHER", "value": "cloud_run"},
        {"name": "MILO_ENABLE_PROPOSAL_MUTATIONS", "value": "false"},
        {"name": "MILO_ENABLE_PROPOSAL_READS", "value": "false"},
        {"name": "MILO_ENABLE_RUN_CANCELLATION", "value": "false"},
        {"name": "MILO_ENABLE_EXECUTION_CONTROL", "value": "false"},
        {"name": "MILO_ENABLE_CATALOG_EXECUTION", "value": "false"},
    ]
    env.extend(extra or [])
    return {"spec": {"template": {"spec": {"containers": [
        {"image": image or f"{REGISTRY}/api:{RELEASE_SHA}", "env": env}
    ]}}}}


def run_verify_caps(tmp_path, worker, api, caps=CAPS, provider_limits=PROVIDER_LIMITS):
    worker_path = tmp_path / "worker.json"
    api_path = tmp_path / "api.json"
    worker_path.write_text(json.dumps(worker))
    api_path.write_text(json.dumps(api))
    return subprocess.run(
        [sys.executable, str(STAGE_D / "verify_caps.py"),
         "--worker-json", str(worker_path), "--api-json", str(api_path)],
        capture_output=True, text=True,
        env={**os.environ, "STAGE_D_CAPS": caps, "STAGE_D_WORKER_PROVIDER_LIMITS": provider_limits,
             "STAGE_D_REGISTRY": REGISTRY, "STAGE_D_RELEASE_SHA": RELEASE_SHA,
             "STAGE_D_API_IMAGE_DIGEST": API_DIGEST, "STAGE_D_WORKER_IMAGE_DIGEST": WORKER_DIGEST},
        timeout=60,
    )


def test_verify_caps_passes_on_the_exact_authorized_posture(tmp_path):
    result = run_verify_caps(tmp_path, worker_spec(), api_spec())
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"release {RELEASE_SHA[:12]}" in result.stdout


@pytest.mark.parametrize("surface,bad", [
    ("worker", {"image": f"{REGISTRY}/worker:88224bccc836f80f3dc1d173306a1aa63cddcc7a"}),
    ("worker", {"image": f"{REGISTRY}/worker:latest"}),
    ("worker", {"image": f"{REGISTRY}/worker@{STALE_WORKER_DIGEST}"}),
    ("api", {"image": f"{REGISTRY}/api:88224bccc836f80f3dc1d173306a1aa63cddcc7a"}),
    ("api", {"image": "us-central1-docker.pkg.dev/attacker/evil/api:" + RELEASE_SHA}),
    ("api", {"image": f"{REGISTRY}/api@sha256:" + "0" * 64}),
])
def test_verify_caps_refuses_a_wrong_release_image(tmp_path, surface, bad):
    """WRONG RELEASE IMAGE — wrong tag, wrong registry, or wrong digest."""
    worker = worker_spec(**bad) if surface == "worker" else worker_spec()
    api = api_spec(**bad) if surface == "api" else api_spec()
    result = run_verify_caps(tmp_path, worker, api)
    assert result.returncode != 0
    assert "is neither the accepted release digest" in result.stdout
    assert "do NOT create the run" in result.stdout


@pytest.mark.parametrize("surface", ["worker", "api"])
def test_verify_caps_accepts_the_accepted_digest_reference(tmp_path, surface):
    """Pinning the digest directly is the strongest form and must pass."""
    worker = worker_spec(image=f"{REGISTRY}/worker@{WORKER_DIGEST}") if surface == "worker" else worker_spec()
    api = api_spec(image=f"{REGISTRY}/api@{API_DIGEST}") if surface == "api" else api_spec()
    assert run_verify_caps(tmp_path, worker, api).returncode == 0


def test_verify_caps_fails_closed_without_the_accepted_digests(tmp_path):
    """The release identity is the digest; without it there is nothing to verify."""
    result = subprocess.run(
        [sys.executable, str(STAGE_D / "verify_caps.py"),
         "--worker-json", str(tmp_path / "w.json"), "--api-json", str(tmp_path / "a.json")],
        capture_output=True, text=True,
        env={**os.environ, "STAGE_D_CAPS": CAPS, "STAGE_D_WORKER_PROVIDER_LIMITS": PROVIDER_LIMITS,
             "STAGE_D_REGISTRY": REGISTRY, "STAGE_D_RELEASE_SHA": RELEASE_SHA,
             "STAGE_D_API_IMAGE_DIGEST": "", "STAGE_D_WORKER_IMAGE_DIGEST": ""},
        timeout=60,
    )
    assert result.returncode != 0


@pytest.mark.parametrize("surface", ["worker", "api"])
@pytest.mark.parametrize("enabled", ["true", "TRUE", "1"])
def test_verify_caps_refuses_catalog_execution_enabled(tmp_path, surface, enabled):
    """CATALOG FLAG ENABLED — on either surface, in any spelling."""
    entry = [{"name": "MILO_ENABLE_CATALOG_EXECUTION", "value": enabled}]
    if surface == "worker":
        worker = worker_spec()
        worker["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["env"] = [
            e for e in worker["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["env"]
            if e["name"] != "MILO_ENABLE_CATALOG_EXECUTION"
        ] + entry
        api = api_spec()
    else:
        worker = worker_spec()
        api = api_spec()
        api["spec"]["template"]["spec"]["containers"][0]["env"] = [
            e for e in api["spec"]["template"]["spec"]["containers"][0]["env"]
            if e["name"] != "MILO_ENABLE_CATALOG_EXECUTION"
        ] + entry
    result = run_verify_caps(tmp_path, worker, api)
    assert result.returncode != 0
    assert "MILO_ENABLE_CATALOG_EXECUTION" in result.stdout
    assert "requires its own explicit authorization" in result.stdout


@pytest.mark.parametrize("entry", [
    {"name": "KIMI_API_KEY", "valueFrom": {"secretKeyRef": {"key": "latest", "name": "KIMI_API_KEY"}}},
    {"name": "MOONSHOT_API_KEY", "valueFrom": {"secretKeyRef": {"key": "latest", "name": "KIMI_API_KEY"}}},
    {"name": "KIMI_API_KEY", "value": "sk-not-a-real-key"},
    {"name": "MOONSHOT_API_KEY", "value": "sk-not-a-real-key"},
])
def test_verify_caps_refuses_a_provider_key_on_the_api(tmp_path, entry):
    """PROVIDER KEY ON API — binding or literal, either alias."""
    result = run_verify_caps(tmp_path, worker_spec(), api_spec(extra=[entry]))
    assert result.returncode != 0
    assert "must NEVER" in result.stdout
    # The secret VALUE is never echoed back.
    assert "sk-not-a-real-key" not in result.stdout


def test_verify_caps_refuses_the_live_provider_concurrency_drift(tmp_path):
    """Production currently carries MILO_PROVIDER_MAX_CONCURRENCY=8."""
    drifted = PROVIDER_LIMITS.replace("MILO_PROVIDER_MAX_CONCURRENCY=2", "MILO_PROVIDER_MAX_CONCURRENCY=8")
    result = run_verify_caps(tmp_path, worker_spec(provider=drifted), api_spec())
    assert result.returncode != 0
    assert "MILO_PROVIDER_MAX_CONCURRENCY" in result.stdout
    assert "differs from pinned" in result.stdout


def test_verify_caps_refuses_any_provider_variable_on_the_api(tmp_path):
    api = api_spec(extra=[{"name": "MILO_PROVIDER_MAX_CONCURRENCY", "value": "2"}])
    result = run_verify_caps(tmp_path, worker_spec(), api)
    assert result.returncode != 0
    assert "must NEVER be set on the API service" in result.stdout


@pytest.mark.parametrize("cap,loosened", [
    ("MILO_MAX_COST_PER_RUN=1.00", "MILO_MAX_COST_PER_RUN=3.00"),
    ("MILO_MAX_MODEL_CALLS_PER_RUN=150", "MILO_MAX_MODEL_CALLS_PER_RUN=200"),
])
def test_verify_caps_refuses_a_loosened_cap_on_either_surface(tmp_path, cap, loosened):
    live = CAPS.replace(cap, loosened)
    assert run_verify_caps(tmp_path, worker_spec(caps=live), api_spec()).returncode != 0
    assert run_verify_caps(tmp_path, worker_spec(), api_spec(caps=live)).returncode != 0


def test_verify_caps_refuses_an_unexpected_extra_budget_variable(tmp_path):
    extra = [{"name": "MILO_MAX_SOMETHING_ELSE", "value": "999999"}]
    result = run_verify_caps(tmp_path, worker_spec(extra=extra), api_spec())
    assert result.returncode != 0
    assert "unexpected budget/cap variable" in result.stdout


def test_verify_caps_fails_closed_without_expected_values(tmp_path):
    assert run_verify_caps(tmp_path, worker_spec(), api_spec(), caps="").returncode != 0
    assert run_verify_caps(tmp_path, worker_spec(), api_spec(), provider_limits="").returncode != 0


def test_verify_caps_requires_the_worker_secret_binding(tmp_path):
    result = run_verify_caps(tmp_path, worker_spec(bind_key=False), api_spec())
    assert result.returncode != 0
    assert "provider key is not bound" in result.stdout


def test_verify_caps_tolerates_unrelated_live_worker_variables(tmp_path):
    """The live worker carries swarm/model variables that are not caps."""
    extra = [
        {"name": "MILO_SWARM_MAX_ACTIVE_WORKERS", "value": "8"},
        {"name": "MILO_COMMANDER_MODEL", "value": "kimi-k2.6"},
        {"name": "MILO_MODEL_BASE_URL", "value": "https://api.moonshot.ai/v1"},
    ]
    assert run_verify_caps(tmp_path, worker_spec(extra=extra), api_spec()).returncode == 0


# ---------------------------------------------------------------------------
# D. verify_executions.py — baseline drift and unexpected active executions
# ---------------------------------------------------------------------------

LIVE_EXECUTION_NAMES = [
    "milo-agent-worker-mcfrx", "milo-agent-worker-gggdc", "milo-agent-worker-dk4xv",
    "milo-agent-worker-gnj5d", "milo-agent-worker-fvfcb", "milo-agent-worker-2tckh",
    "milo-agent-worker-bw8kj",
]


def terminal(name, ok=True):
    return {"metadata": {"name": name},
            "status": {"completionTime": "2026-08-24T21:47:38Z",
                       "conditions": [{"type": "Completed", "status": "True" if ok else "False"}]}}


def run_verify_executions(listing, expected_total):
    payload = listing if isinstance(listing, str) else json.dumps(listing)
    return subprocess.run(
        [sys.executable, str(STAGE_D / "verify_executions.py"), "--expected-total", str(expected_total)],
        input=payload, capture_output=True, text=True, timeout=60,
    )


def test_exactly_seven_visible_terminal_executions_pass_the_pre_run_gate():
    result = run_verify_executions([terminal(n) for n in LIVE_EXECUTION_NAMES], 7)
    assert result.returncode == 0, result.stdout
    verdict = json.loads(result.stdout)
    assert verdict["ok"] is True and verdict["total"] == 7 and verdict["nonterminal"] == 0


@pytest.mark.parametrize("count", [0, 1, 6, 8, 9])
def test_pre_run_gate_refuses_any_count_other_than_the_pinned_baseline(count):
    """BASELINE DRIFT — in both directions."""
    listing = [terminal(f"milo-agent-worker-x{i}") for i in range(count)]
    result = run_verify_executions(listing, 7)
    assert result.returncode != 0
    assert "expected exactly 7" in result.stdout


@pytest.mark.parametrize("status", [
    {"completionTime": None},
    {},
    {"conditions": [{"type": "Completed", "status": "Unknown"}]},
    {"conditions": []},
    None,
])
def test_an_unexpected_active_or_unverifiable_execution_refuses(status):
    """UNEXPECTED ACTIVE EXECUTION — and fail-safe on unverifiable status."""
    listing = [terminal(n) for n in LIVE_EXECUTION_NAMES]
    listing.append({"metadata": {"name": "milo-agent-worker-live1"}, "status": status})
    result = run_verify_executions(listing, 8)  # count is right; state is not
    assert result.returncode != 0
    verdict = json.loads(result.stdout)
    assert verdict["ok"] is False and verdict["nonterminal"] == 1
    assert "milo-agent-worker-live1" in json.dumps(verdict)


def test_exactly_eight_terminal_executions_pass_the_post_run_gate():
    listing = [terminal(n) for n in LIVE_EXECUTION_NAMES] + [terminal("milo-agent-worker-staged1")]
    assert run_verify_executions(listing, 8).returncode == 0


@pytest.mark.parametrize("extra", [0, 2, 3])
def test_post_run_gate_refuses_unless_exactly_one_execution_was_added(extra):
    listing = [terminal(n) for n in LIVE_EXECUTION_NAMES]
    listing += [terminal(f"milo-agent-worker-new{i}") for i in range(extra)]
    assert run_verify_executions(listing, 8).returncode != 0


@pytest.mark.parametrize("listing", ["not json", '{"not": "a list"}', ""])
def test_unparseable_listings_fail_closed(listing):
    result = run_verify_executions(listing, 7)
    assert result.returncode != 0
    assert "failing closed" in result.stdout


def test_execution_gate_never_claims_success_with_problems():
    verdict = json.loads(run_verify_executions([], 7).stdout)
    assert verdict["ok"] is False and verdict["problems"]


@pytest.mark.parametrize("payload,expected", [
    ({"status": {"conditions": [{"type": "Completed", "status": "True"}]}}, "succeeded"),
    ({"status": {"conditions": [{"type": "Completed", "status": "False"}]}}, "failed"),
    ({"status": {"completionTime": "2026-09-18T00:00:00Z"}}, "failed"),
    ({"status": {}}, "running"),
    ({}, "running"),
    ("garbage", "running"),
])
def test_execution_state_verdicts_are_fail_safe(payload, expected):
    body = payload if isinstance(payload, str) else json.dumps(payload)
    result = subprocess.run([sys.executable, str(STAGE_D / "execution_state.py")],
                            input=body, capture_output=True, text=True, timeout=60)
    assert result.stdout.strip() == expected


# ---------------------------------------------------------------------------
# E. probe_db.py — baseline drift, reused key, Government-capture invariant
# ---------------------------------------------------------------------------

PREPARED_CAPTURE_ROW = {
    "id": GOV_RUN_ID, "status": "queued", "launch_state": "none", "worker_id": None,
    "attempt": 1, "started_at": None, "finished_at": None, "last_heartbeat_at": None,
    "lease_expires_at": None, "idempotency_key": GOV_KEY, "cancellation_reason": None,
}
RETIRED_CAPTURE_ROW = {**PREPARED_CAPTURE_ROW, "status": "cancelled",
                       "finished_at": "2026-09-18T23:00:00Z"}


def wire_db(db, monkeypatch, *, capture_row=PREPARED_CAPTURE_ROW, counts=None, openapi=True):
    """Route probe_db's HTTP layer at a deterministic fake production."""
    counts = counts or {}

    def fake_call(method, path, body=None, headers=None):
        if path == "/rest/v1/":
            if not openapi:
                return 503, None
            paths = {
                f"/rpc/{rpc}": {"post": {"parameters": [
                    {"in": "body", "schema": {"properties": {a: {} for a in args}}}
                ]}}
                for rpc, args in db.REQUIRED_RPC_ARGS.items()
            }
            return 200, {"paths": paths}
        if path.startswith(f"/rest/v1/runs?id=eq.{GOV_RUN_ID}"):
            return 200, ([capture_row] if capture_row is not None else [])
        return 200, []

    def fake_count(path):
        for prefix, value in counts.items():
            if prefix in path:
                return value
        if "/rest/v1/runs?select=id&idempotency_key=eq." in path:
            return 0
        if path == "/rest/v1/runs?select=id":
            return 7
        return 0  # every Government-capture trace table

    monkeypatch.setattr(db, "call", fake_call)
    monkeypatch.setattr(db, "count_exact", fake_count)


def preflight_output(db, capsys):
    out = capsys.readouterr().out.strip().splitlines()[-1]
    return json.loads(out)


def test_preflight_passes_on_the_exact_live_baseline(db, monkeypatch, capsys):
    monkeypatch.setenv("STAGE_D_EXPECTED_PRIOR_RUNS", EXPECTED_PRIOR_RUNS)
    wire_db(db, monkeypatch)
    db.preflight()
    verdict = preflight_output(db, capsys)
    assert verdict["ok"] is True, verdict["problems"]
    assert verdict["government_capture"]["posture"] == db.GOV_PREPARED


@pytest.mark.parametrize("total", [0, 6, 8, 12])
def test_preflight_refuses_on_database_baseline_drift(db, monkeypatch, capsys, total):
    """BASELINE DRIFT — a vanished row fails exactly like an extra one."""
    monkeypatch.setenv("STAGE_D_EXPECTED_PRIOR_RUNS", EXPECTED_PRIOR_RUNS)
    wire_db(db, monkeypatch, counts={"/rest/v1/runs?select=id": total})
    with pytest.raises(SystemExit):
        db.preflight()
    verdict = preflight_output(db, capsys)
    assert verdict["ok"] is False
    assert any("expected exactly the pinned prior baseline" in p for p in verdict["problems"])


@pytest.mark.parametrize("prior", [None, "", "seven", "-1", "7.0"])
def test_preflight_fails_closed_on_missing_or_invalid_baseline_config(db, monkeypatch, capsys, prior):
    monkeypatch.delenv("STAGE_D_EXPECTED_PRIOR_RUNS", raising=False)
    if prior is not None:
        monkeypatch.setenv("STAGE_D_EXPECTED_PRIOR_RUNS", prior)
    wire_db(db, monkeypatch)
    with pytest.raises(SystemExit):
        db.preflight()
    verdict = preflight_output(db, capsys)
    assert any("STAGE_D_EXPECTED_PRIOR_RUNS" in p for p in verdict["problems"])


def test_preflight_fails_closed_when_the_exact_count_is_unavailable(db, monkeypatch, capsys):
    monkeypatch.setenv("STAGE_D_EXPECTED_PRIOR_RUNS", EXPECTED_PRIOR_RUNS)
    wire_db(db, monkeypatch, counts={"/rest/v1/runs?select=id": None})
    with pytest.raises(SystemExit):
        db.preflight()
    assert any("unavailable" in p for p in preflight_output(db, capsys)["problems"])


@pytest.mark.parametrize("existing", [1, 2])
def test_preflight_refuses_a_reused_idempotency_key(db, monkeypatch, capsys, existing):
    """REUSED IDEMPOTENCY KEY — any pre-existing row under it blocks."""
    monkeypatch.setenv("STAGE_D_EXPECTED_PRIOR_RUNS", EXPECTED_PRIOR_RUNS)
    wire_db(db, monkeypatch, counts={"idempotency_key=eq.": existing})
    with pytest.raises(SystemExit):
        db.preflight()
    verdict = preflight_output(db, capsys)
    assert any("pre-existing run" in p for p in verdict["problems"])


def test_preflight_refuses_when_the_stage_d_key_is_the_capture_key(db, monkeypatch, capsys):
    monkeypatch.setenv("STAGE_D_EXPECTED_PRIOR_RUNS", EXPECTED_PRIOR_RUNS)
    monkeypatch.setenv("STAGE_D_IDEMPOTENCY_KEY", GOV_KEY)
    wire_db(db, monkeypatch)
    with pytest.raises(SystemExit):
        db.preflight()
    verdict = preflight_output(db, capsys)
    assert any("Government capture key" in p for p in verdict["problems"])


@pytest.mark.parametrize("mutation,marker", [
    ({"worker_id": "worker-abc"}, "CLAIMED"),
    ({"started_at": "2026-09-19T00:00:00Z"}, "CLAIMED"),
    ({"last_heartbeat_at": "2026-09-19T00:00:00Z"}, "CLAIMED"),
    ({"lease_expires_at": "2026-09-19T00:05:00Z"}, "CLAIMED"),
    ({"status": "running"}, "allowed postures"),
    ({"status": "completed"}, "allowed postures"),
    ({"launch_state": "launched"}, "allowed postures"),
    ({"launch_state": "pending"}, "allowed postures"),
    ({"attempt": 2}, "a claim incremented it"),
    ({"idempotency_key": "something-else"}, "not the prepared capture row"),
])
def test_government_capture_invariant_refuses_a_disturbed_row(db, monkeypatch, capsys, mutation, marker):
    monkeypatch.setenv("STAGE_D_EXPECTED_PRIOR_RUNS", EXPECTED_PRIOR_RUNS)
    wire_db(db, monkeypatch, capture_row={**PREPARED_CAPTURE_ROW, **mutation})
    with pytest.raises(SystemExit):
        db.preflight()
    verdict = preflight_output(db, capsys)
    assert verdict["ok"] is False
    assert any(marker in p for p in verdict["problems"]), verdict["problems"]


def test_government_capture_invariant_refuses_a_vanished_row(db, monkeypatch, capsys):
    """Stage D deletes no run row, so an absent capture row is drift."""
    monkeypatch.setenv("STAGE_D_EXPECTED_PRIOR_RUNS", EXPECTED_PRIOR_RUNS)
    wire_db(db, monkeypatch, capture_row=None)
    with pytest.raises(SystemExit):
        db.preflight()
    assert any("ABSENT" in p for p in preflight_output(db, capsys)["problems"])


@pytest.mark.parametrize("table", list(("run_events", "run_usage_ledger",
                                        "model_call_budget_reservations", "worker_heartbeats",
                                        "run_invocations", "run_checkpoints", "run_blackboards")))
def test_government_capture_invariant_refuses_any_execution_trace(db, monkeypatch, capsys, table):
    monkeypatch.setenv("STAGE_D_EXPECTED_PRIOR_RUNS", EXPECTED_PRIOR_RUNS)
    wire_db(db, monkeypatch, counts={f"/rest/v1/{table}?select=run_id": 1})
    with pytest.raises(SystemExit):
        db.preflight()
    verdict = preflight_output(db, capsys)
    assert any("was EXECUTED" in p for p in verdict["problems"]), verdict["problems"]


def test_government_capture_invariant_accepts_the_retired_posture(db, monkeypatch, capsys):
    """After resolve-government-capture.sh retires it, gates still pass."""
    monkeypatch.setenv("STAGE_D_EXPECTED_PRIOR_RUNS", EXPECTED_PRIOR_RUNS)
    wire_db(db, monkeypatch, capture_row=RETIRED_CAPTURE_ROW)
    db.preflight()
    verdict = preflight_output(db, capsys)
    assert verdict["ok"] is True, verdict["problems"]
    assert verdict["government_capture"]["posture"] == db.GOV_RETIRED


def test_govcheck_mode_is_a_standalone_read_only_gate(db, monkeypatch, capsys):
    wire_db(db, monkeypatch)
    db.govcheck()
    verdict = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert verdict["stage_d_probe"] == "govcheck" and verdict["ok"] is True


def test_preflight_fails_closed_when_the_rpc_surface_cannot_be_established(db, monkeypatch, capsys):
    monkeypatch.setenv("STAGE_D_EXPECTED_PRIOR_RUNS", EXPECTED_PRIOR_RUNS)
    wire_db(db, monkeypatch, openapi=False)
    with pytest.raises(SystemExit):
        db.preflight()
    assert any("OpenAPI introspection unavailable" in p for p in preflight_output(db, capsys)["problems"])


def test_preflight_rpc_checks_perform_no_mutating_posts(db, monkeypatch):
    """Probing by INVOKING a mutating RPC is unsafe by construction."""
    methods: list[str] = []
    monkeypatch.setenv("STAGE_D_EXPECTED_PRIOR_RUNS", EXPECTED_PRIOR_RUNS)
    wire_db(db, monkeypatch)
    wired = db.call  # the deterministic fake installed above — never the network

    def spy(method, path, body=None, headers=None):
        methods.append(method)
        return wired(method, path, body, headers)

    monkeypatch.setattr(db, "call", spy)
    db.preflight()
    assert methods, "preflight issued no HTTP call at all"
    assert set(methods) == {"GET"}


# ---------------------------------------------------------------------------
# F. probe_gateway.py — acceptance policy and capture-identity refusal
# ---------------------------------------------------------------------------

def run_status(state):
    return 200, {"status": state, "attempt": 1, "usage": {}}


def test_poll_completed_is_pass(gw, monkeypatch):
    monkeypatch.setattr(gw, "call", lambda *a, **k: run_status("completed"))
    gw.poll()


@pytest.mark.parametrize("state", ["failed", "cancelled", "timed_out", "budget_exhausted", "partial_success"])
def test_poll_unacceptable_terminal_states_exit_nonzero(gw, monkeypatch, capsys, state):
    monkeypatch.setattr(gw, "call", lambda *a, **k: run_status(state))
    with pytest.raises(SystemExit) as excinfo:
        gw.poll()
    assert excinfo.value.code == 2
    out = capsys.readouterr().out
    assert '"acceptable": false' in out and "kill-switch.sh" in out


def test_poll_timeout_exits_nonzero_with_kill_switch_instruction(gw, monkeypatch, capsys):
    monkeypatch.setattr(gw, "call", lambda *a, **k: run_status("running"))
    with pytest.raises(SystemExit) as excinfo:
        gw.poll()
    assert excinfo.value.code == 1
    assert "kill-switch.sh" in capsys.readouterr().out


def test_gateway_probe_refuses_the_capture_key_before_any_api_call(monkeypatch, capsys):
    """The probe must not even reach the network with a borrowed identity."""
    monkeypatch.setenv("STAGE_D_API_URL", "https://api.invalid")
    monkeypatch.setenv("STAGE_D_USER_ID", "user-1")
    monkeypatch.setenv("STAGE_D_CONVERSATION_ID", "conv-1")
    monkeypatch.setenv("STAGE_D_IDEMPOTENCY_KEY", GOV_KEY)
    monkeypatch.setenv("STAGE_D_GOV_CAPTURE_KEY", GOV_KEY)
    module = load_module("stage_d_probe_gateway_capture", STAGE_D / "probe_gateway.py")
    called = []
    monkeypatch.setattr(module, "call", lambda *a, **k: called.append(a) or (200, {}))
    with pytest.raises(SystemExit):
        module.create()
    assert not called, "an API call was made despite the identity collision"
    assert "replay the prepared capture" in capsys.readouterr().out


def test_gateway_probe_refuses_if_creation_returns_the_capture_run(gw, monkeypatch, capsys):
    def fake(method, path, body=None):
        if path == "/health":
            return 200, {}
        return 202, {"run_id": GOV_RUN_ID, "status": "queued"}
    monkeypatch.setattr(gw, "call", fake)
    with pytest.raises(SystemExit):
        gw.create()
    assert "prepared Government capture run id" in capsys.readouterr().out


def test_gateway_probe_run_request_carries_no_capture_marker(gw):
    assert gw.RUN_REQUEST["metadata"] == {"stage": "stage-d-smoke"}
    assert "milo_operation" not in gw.RUN_REQUEST["metadata"]


def test_gateway_probe_requires_an_explicit_idempotency_key(monkeypatch):
    """A probe that invented a key could create an unauthorized run."""
    monkeypatch.setenv("STAGE_D_API_URL", "https://api.invalid")
    monkeypatch.setenv("STAGE_D_USER_ID", "u")
    monkeypatch.setenv("STAGE_D_CONVERSATION_ID", "c")
    monkeypatch.delenv("STAGE_D_IDEMPOTENCY_KEY", raising=False)
    with pytest.raises(KeyError):
        load_module("stage_d_probe_gateway_nokey", STAGE_D / "probe_gateway.py")


# ---------------------------------------------------------------------------
# G. Cleanup and lockdown — asserted on the real END STATE
#
# These tests run the REAL kill-switch.sh and 07-post-run-lockdown.sh
# against a stateful gcloud mock, so the assertions are about the posture
# those scripts actually produced, never about whether they were called.
# ---------------------------------------------------------------------------

MOCK_GCLOUD = REPO / "tests" / "fixtures" / "stage_d" / "mock_gcloud.py"

ENABLED_WORKER_ENV = {
    "MILO_ENABLE_PAID_EXECUTION": "true",
    "MILO_ENABLE_CATALOG_EXECUTION": "false",
}
ENABLED_API_ENV = {
    "MILO_ENABLE_RUN_CREATION": "true",
    "JOB_LAUNCHER": "cloud_run",
    "MILO_ENABLE_PAID_EXECUTION": "false",
    "MILO_ENABLE_CATALOG_EXECUTION": "false",
}


class StageDWorld:
    """A temporary copy of the toolkit wired to the stateful gcloud mock."""

    def __init__(self, tmp_path, state=None):
        self.root = tmp_path / "repo"
        (self.root / "scripts" / "release").mkdir(parents=True)
        shutil.copytree(STAGE_D, self.root / "scripts" / "release" / "stage-d")
        shutil.copytree(REPO / "scripts" / "release" / "lib",
                        self.root / "scripts" / "release" / "lib")
        self.dir = self.root / "scripts" / "release" / "stage-d"
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.state_path = tmp_path / "mock-state.json"
        self.log = tmp_path / "gcloud.log"
        self.workdir = tmp_path / "workdir"
        self.workdir.mkdir()

        base = json.loads(subprocess.run(
            [sys.executable, "-c",
             f"import sys, json; sys.path.insert(0, {str(MOCK_GCLOUD.parent)!r});"
             " import mock_gcloud; print(json.dumps(mock_gcloud.default_state()))"],
            capture_output=True, text=True, check=True).stdout)
        base.update(state or {})
        self.state_path.write_text(json.dumps(base))

        shim = self.bin / "gcloud"
        shim.write_text(
            "#!/usr/bin/env bash\n"
            f'exec {sys.executable} "{MOCK_GCLOUD}" "$@"\n'
        )
        shim.chmod(0o755)
        # `git rev-parse --show-toplevel` must resolve to this copy so the
        # pasted operator block runs against it.
        git = self.bin / "git"
        git.write_text(
            "#!/usr/bin/env bash\n"
            f'if [ "$1" = "rev-parse" ]; then echo "{self.root}"; exit 0; fi\n'
            "exit 0\n"
        )
        git.chmod(0o755)

    def env(self, **extra):
        return {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.root.parent),
            "MOCK_STATE": str(self.state_path),
            "MOCK_LOG": str(self.log),
            "STAGE_D_WORKDIR": str(self.workdir),
            **extra,
        }

    def read_state(self):
        return json.loads(self.state_path.read_text())

    def set_state(self, **kwargs):
        state = self.read_state()
        state.update(kwargs)
        self.state_path.write_text(json.dumps(state))

    def enable_execution_surface(self):
        """Put production in the mid-run, fully enabled, dirty posture."""
        state = self.read_state()
        state["worker_env"].update(ENABLED_WORKER_ENV)
        state["worker_secrets"] = ["KIMI_API_KEY"]
        state["api_env"].update(ENABLED_API_ENV)
        state["jobs"] = ["milo-agent-worker", "stage-d-db-probe", "stage-d-gw-probe"]
        self.state_path.write_text(json.dumps(state))

    def run(self, script, *args, **env):
        return subprocess.run(
            ["bash", str(self.dir / script), *args],
            capture_output=True, text=True, cwd=str(self.dir),
            env=self.env(**env), timeout=300,
        )

    # -- assertions on the END STATE -----------------------------------
    def assert_fail_closed(self):
        state = self.read_state()
        for surface in ("worker_env", "api_env"):
            for flag in ("MILO_ENABLE_PAID_EXECUTION", "MILO_ENABLE_CATALOG_EXECUTION"):
                assert state[surface].get(flag) == "false", f"{surface}.{flag} is not false"
        assert state["api_env"].get("MILO_ENABLE_RUN_CREATION") == "false"
        assert state["api_env"].get("JOB_LAUNCHER") == "disabled"
        for surface in ("worker", "api"):
            for alias in ("KIMI_API_KEY", "MOONSHOT_API_KEY"):
                assert alias not in state[f"{surface}_secrets"], f"{alias} still bound to the {surface}"
                assert alias not in state[f"{surface}_env"], f"{alias} still set on the {surface}"
        assert all(e["terminal"] for e in state["executions"]), "an execution is still active"

    def assert_probes_absent(self):
        jobs = self.read_state()["jobs"]
        for probe in ("stage-d-db-probe", "stage-d-gw-probe"):
            assert probe not in jobs, f"{probe} survived cleanup"


def test_lockdown_reaches_the_full_fail_closed_end_state(tmp_path):
    world = StageDWorld(tmp_path)
    world.enable_execution_surface()
    result = world.run("07-post-run-lockdown.sh")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "STAGE D LOCKDOWN COMPLETE" in result.stdout
    world.assert_fail_closed()
    world.assert_probes_absent()


def test_lockdown_proves_the_capture_before_deleting_the_probe(tmp_path):
    """Checking after deleting the only credentialed reader checks nothing."""
    world = StageDWorld(tmp_path)
    world.enable_execution_surface()
    result = world.run("07-post-run-lockdown.sh")
    assert result.returncode == 0, result.stdout + result.stderr
    calls = world.log.read_text().splitlines()
    govcheck_at = next(i for i, c in enumerate(calls) if "STAGE_D_MODE=govcheck" in c)
    delete_at = next(i for i, c in enumerate(calls) if "jobs delete stage-d-db-probe" in c)
    assert govcheck_at < delete_at, "the capture was checked after its reader was deleted"


def test_lockdown_refuses_to_claim_success_when_the_capture_check_fails(tmp_path):
    world = StageDWorld(tmp_path, state={"govcheck_ok": False})
    world.enable_execution_surface()
    result = world.run("07-post-run-lockdown.sh")
    assert result.returncode != 0
    assert "STAGE D LOCKDOWN COMPLETE" not in result.stdout
    assert "Government capture posture check FAILED" in result.stderr
    # The probes are STILL removed: a failed check is never a reason to
    # leave a credentialed job standing.
    world.assert_probes_absent()
    world.assert_fail_closed()


def test_lockdown_refuses_to_claim_success_when_the_capture_check_cannot_run(tmp_path):
    world = StageDWorld(tmp_path, state={"govcheck_available": False})
    world.enable_execution_surface()
    result = world.run("07-post-run-lockdown.sh")
    assert result.returncode != 0
    assert "STAGE D LOCKDOWN COMPLETE" not in result.stdout
    assert "UNVERIFIED" in result.stderr
    world.assert_probes_absent()


def test_lockdown_is_partial_not_complete_when_a_run_existed_but_the_probe_is_gone(tmp_path):
    """A run happened, the reader is gone: that is UNVERIFIED, not fine."""
    world = StageDWorld(tmp_path)
    (world.workdir / "state.json").write_text(json.dumps({"run_id": "11111111-1111-1111-1111-111111111111"}))
    result = world.run("07-post-run-lockdown.sh")
    assert result.returncode != 0
    assert "STAGE D LOCKDOWN COMPLETE" not in result.stdout
    assert "cannot be checked" in result.stderr


def test_lockdown_states_not_applicable_when_no_run_was_ever_created(tmp_path):
    """The one documented exception — stated explicitly, never silent."""
    world = StageDWorld(tmp_path)
    result = world.run("07-post-run-lockdown.sh")
    # Flags off and probes absent, but the capture was not PROVEN, so the
    # verdict is PARTIAL rather than COMPLETE.
    assert result.returncode == 2
    assert "NOT APPLICABLE" in result.stdout
    assert "STAGE D LOCKDOWN PARTIAL" in result.stdout
    assert "STAGE D LOCKDOWN COMPLETE" not in result.stdout
    world.assert_fail_closed()
    world.assert_probes_absent()


@pytest.mark.parametrize("survivor", ["stage-d-db-probe", "stage-d-gw-probe"])
def test_lockdown_refuses_when_a_probe_job_survives(tmp_path, survivor):
    """MISSING CLEANUP — a surviving probe is a standing credentialed path."""
    world = StageDWorld(tmp_path)
    world.enable_execution_surface()
    world.set_state(fail_commands=[f"jobs delete {survivor}"])
    result = world.run("07-post-run-lockdown.sh")
    assert result.returncode != 0
    assert f"{survivor} still EXISTS after cleanup" in result.stderr
    assert "STAGE D LOCKDOWN COMPLETE" not in result.stdout


def test_lockdown_fails_closed_when_the_job_listing_is_unobtainable(tmp_path):
    world = StageDWorld(tmp_path)
    world.enable_execution_surface()
    world.set_state(fail_commands=["run jobs list"])
    result = world.run("07-post-run-lockdown.sh")
    assert result.returncode != 0
    assert "UNVERIFIED" in result.stderr


def test_lockdown_cancels_an_active_execution_and_proves_zero_active(tmp_path):
    world = StageDWorld(tmp_path)
    world.enable_execution_surface()
    state = world.read_state()
    state["executions"].append({"name": "milo-agent-worker-live1", "terminal": False})
    world.state_path.write_text(json.dumps(state))
    result = world.run("07-post-run-lockdown.sh")
    assert result.returncode == 0, result.stdout + result.stderr
    world.assert_fail_closed()  # includes: no execution left non-terminal


def test_kill_switch_alone_reaches_the_fail_closed_posture(tmp_path):
    world = StageDWorld(tmp_path)
    world.enable_execution_surface()
    result = world.run("kill-switch.sh")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "KILL SWITCH APPLIED" in result.stdout
    world.assert_fail_closed()


def test_kill_switch_does_not_delete_probe_jobs():
    """Deleting evidence-collection capability mid-incident is wrong."""
    assert "jobs delete" not in (STAGE_D / "kill-switch.sh").read_text()


# ---------------------------------------------------------------------------
# G2. The guarded operator block — failure injected at EVERY mutation boundary
# ---------------------------------------------------------------------------


def guarded_block() -> str:
    """The ONE fenced bash block of 02-guarded-run.md."""
    blocks = re.findall(r"```bash\n(.*?)```", (STAGE_D / "02-guarded-run.md").read_text(), re.S)
    assert len(blocks) == 1, "the guarded run must be exactly ONE operator block"
    return blocks[0]


def test_guarded_block_is_one_trap_guarded_subshell(tmp_path):
    block = guarded_block()
    script = tmp_path / "block.sh"
    script.write_text("#!/usr/bin/env bash\n" + block)
    assert subprocess.run(["bash", "-n", str(script)], capture_output=True,
                          text=True, timeout=60).returncode == 0
    assert block.lstrip().startswith("(") and block.rstrip().endswith(")")
    assert "set -Eeuo pipefail" in block
    # The cleanup must be DEFINED before the trap references it, and the
    # trap must be armed before every mutation.
    trap_at = block.index("trap on_exit EXIT ERR INT TERM")
    assert block.index("cleanup_body()") < trap_at
    assert block.index("on_exit()") < trap_at
    for mutation in ("gcloud run jobs update", "gcloud run services update",
                     "./04-create-probes.sh", "./05-execute-run.sh", "./06-collect-evidence.sh",
                     "MILO_ENABLE_PAID_EXECUTION=${STAGE_D_ON}",
                     "MILO_ENABLE_RUN_CREATION=${STAGE_D_ON}", "--update-secrets"):
        assert trap_at < block.index(mutation), f"the trap is armed after {mutation}"
    # Read-only image verification happens BEFORE anything is armed.
    assert block.index("01-verify-release-images.sh") < trap_at
    # No soft stops anywhere.
    assert "|| echo" not in block and "|| true" not in block
    # No committed line pairs a flag name with an enabled literal.
    assert "MILO_ENABLE_PAID_EXECUTION=true" not in block
    assert "MILO_ENABLE_RUN_CREATION=true" not in block
    # Completion is only claimed after the lockdown succeeded.
    assert block.rindex("./07-post-run-lockdown.sh") < block.index("stage_d_completed=1")


#: Every mutation boundary in the guarded block, and the gcloud/script
#: invocation that is made to fail there.
MUTATION_BOUNDARIES = {
    "worker-enable": "run jobs update",
    "api-enable": "run services update",
    "posture-verification": "__SCRIPT__03b-verify-stage-d-posture.sh",
    "probe-creation": "__SCRIPT__04-create-probes.sh",
    "probes-partial": "__PARTIAL_PROBES__",
    "run-execution": "__SCRIPT__05-execute-run.sh",
    "evidence-gate": "__SCRIPT__06-collect-evidence.sh",
}

STUB_OK = "#!/usr/bin/env bash\necho \"stub $0 ok\"\nexit 0\n"
STUB_FAIL = "#!/usr/bin/env bash\necho \"stub $0 FAILED\" >&2\nexit 1\n"
# Creates the db probe, then dies: the "only one probe exists" case.
STUB_PARTIAL_PROBES = (
    "#!/usr/bin/env bash\n"
    "gcloud run jobs create stage-d-db-probe --project=p --region=r\n"
    "echo 'stub 04 died after creating one probe' >&2\n"
    "exit 1\n"
)
STUB_CREATE_BOTH = (
    "#!/usr/bin/env bash\n"
    "gcloud run jobs create stage-d-db-probe --project=p --region=r\n"
    "gcloud run jobs create stage-d-gw-probe --project=p --region=r\n"
    "exit 0\n"
)


def run_guarded_block(tmp_path, fail_at=None):
    """Run the pasted operator block with a failure injected at one boundary.

    01/03b/04/05/06 are stubbed — they need probes and a database that do
    not exist here — but the ENABLE commands, kill-switch.sh and
    07-post-run-lockdown.sh are the REAL ones running against the stateful
    mock, so the asserted end state is the one the real cleanup produced.
    """
    world = StageDWorld(tmp_path)
    stubs = {
        "01-verify-release-images.sh": STUB_OK,
        "03b-verify-stage-d-posture.sh": STUB_OK,
        "04-create-probes.sh": STUB_CREATE_BOTH,
        "05-execute-run.sh": STUB_OK,
        "06-collect-evidence.sh": STUB_OK,
    }
    injected = MUTATION_BOUNDARIES.get(fail_at) if fail_at else None
    gcloud_failures = []
    if injected == "__PARTIAL_PROBES__":
        stubs["04-create-probes.sh"] = STUB_PARTIAL_PROBES
    elif injected and injected.startswith("__SCRIPT__"):
        stubs[injected.removeprefix("__SCRIPT__")] = STUB_FAIL
    elif injected:
        gcloud_failures.append(injected)

    for name, body in stubs.items():
        path = world.dir / name
        path.write_text(body)
        path.chmod(0o755)
    if gcloud_failures:
        world.set_state(fail_commands=gcloud_failures)

    script = tmp_path / "guarded-block.sh"
    script.write_text("#!/usr/bin/env bash\n" + guarded_block())
    result = subprocess.run(
        ["bash", str(script)], capture_output=True, text=True,
        cwd=str(world.root), env=world.env(), timeout=300,
    )
    return world, result


def test_guarded_block_succeeds_and_locks_down(tmp_path):
    world, result = run_guarded_block(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "STAGE D EXPANSION STEP 1 COMPLETE" in result.stdout
    world.assert_fail_closed()
    world.assert_probes_absent()


@pytest.mark.parametrize("boundary", sorted(MUTATION_BOUNDARIES))
def test_guarded_block_cleans_up_from_every_mutation_boundary(tmp_path, boundary):
    """The whole point: whatever fails, production ends fail-closed.

    Asserted on the real posture the real kill switch and lockdown left
    behind — final flags off, both provider aliases absent on both
    surfaces, zero active executions, both probe jobs gone.
    """
    world, result = run_guarded_block(tmp_path, fail_at=boundary)
    assert result.returncode != 0, f"failure at {boundary} was not propagated"
    assert "did not complete cleanly" in result.stderr
    world.assert_fail_closed()
    world.assert_probes_absent()


@pytest.mark.parametrize("signal_name,signal_number", [("INT", 2), ("TERM", 15)])
def test_guarded_block_cleans_up_on_interrupt_and_terminate(tmp_path, signal_name, signal_number):
    """Ctrl-C during the run must not leave the paid flag on."""
    world = StageDWorld(tmp_path)
    for name, body in {
        "01-verify-release-images.sh": STUB_OK,
        "03b-verify-stage-d-posture.sh": STUB_OK,
        "04-create-probes.sh": STUB_CREATE_BOTH,
        "06-collect-evidence.sh": STUB_OK,
    }.items():
        path = world.dir / name
        path.write_text(body)
        path.chmod(0o755)
    # 05 kills its own process group's leader the way an operator's Ctrl-C
    # or a scheduler's SIGTERM would.
    killer = world.dir / "05-execute-run.sh"
    killer.write_text(f'#!/usr/bin/env bash\nkill -{signal_number} $PPID\nsleep 5\n')
    killer.chmod(0o755)

    script = tmp_path / "guarded-block.sh"
    script.write_text("#!/usr/bin/env bash\n" + guarded_block())
    result = subprocess.run(["bash", str(script)], capture_output=True, text=True,
                            cwd=str(world.root), env=world.env(), timeout=300)
    assert result.returncode != 0
    world.assert_fail_closed()
    world.assert_probes_absent()


def test_guarded_block_persists_the_workdir_machine_readably(tmp_path):
    world, result = run_guarded_block(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    state = json.loads((world.workdir / "state.json").read_text())
    assert state["stage_d_workdir"] == str(world.workdir)
    assert state["idempotency_key"] == STAGE_D_KEY


def test_execute_step_persists_the_run_id_before_polling():
    """A cleanup fired mid-poll must still be able to name the run."""
    text = (STAGE_D / "05-execute-run.sh").read_text()
    persist_at = text.index('write_state run_id "${RUN_ID}"')
    poll_at = text.index("STAGE_D_MODE=poll")
    assert persist_at < poll_at
    for key in ("stage_d_workdir", "idempotency_key", "user_id", "conversation_id", "run_id"):
        assert f"write_state {key}" in text


def test_evidence_gate_reads_the_run_id_from_state_not_from_a_human():
    text = (STAGE_D / "06-collect-evidence.sh").read_text()
    assert "state_file.py" in text
    assert "RECORDED_RUN_ID" in text
    # An explicitly passed id that disagrees with the record is refused.
    assert "disagrees with the recorded authorized run" in text


def test_state_file_reads_never_raise_and_writes_are_atomic(tmp_path):
    """A reader that raised would take down a cleanup trap."""
    helper = str(STAGE_D / "state_file.py")
    missing = subprocess.run([sys.executable, helper, str(tmp_path / "nope.json"), "read", "run_id"],
                             capture_output=True, text=True, timeout=60)
    assert missing.returncode == 0 and missing.stdout.strip() == ""
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json")
    bad = subprocess.run([sys.executable, helper, str(corrupt), "read", "run_id"],
                         capture_output=True, text=True, timeout=60)
    assert bad.returncode == 0 and bad.stdout.strip() == ""
    target = tmp_path / "state.json"
    subprocess.run([sys.executable, helper, str(target), "write", "run_id", "r-1"], check=True, timeout=60)
    subprocess.run([sys.executable, helper, str(target), "write", "user_id", "u-1"], check=True, timeout=60)
    assert json.loads(target.read_text()) == {"run_id": "r-1", "user_id": "u-1"}
    assert not list(tmp_path.glob("*.tmp")), "an atomic write left its temp file behind"


# ---------------------------------------------------------------------------
# H. The Government-capture resolution, EXECUTED against real PostgreSQL
# ---------------------------------------------------------------------------

PG_BIN_CANDIDATES = ["/usr/lib/postgresql/16/bin", "/usr/lib/postgresql/15/bin", ""]
PG_PORT = "54994"

# Only the columns the guarded CAS reads or writes, with production's real
# constraints (verified read-only 2026-09-18) so an invalid status or
# launch_state would be rejected here exactly as it is in production.
# Only the columns and tables the guarded CAS reads or writes, with
# production's real constraints (verified read-only 2026-09-18) so an
# invalid status or launch_state is rejected here exactly as in production.
# All seven execution-trace tables exist, because the retirement guard now
# refuses to retire a capture that shows ANY sign of having executed.
GOV_TRACE_TABLES = (
    "run_events",
    "run_usage_ledger",
    "model_call_budget_reservations",
    "worker_heartbeats",
    "run_invocations",
    "run_checkpoints",
    "run_blackboards",
)

RUNS_DDL = """
create table public.runs (
  id uuid primary key,
  conversation_id uuid,
  requested_by uuid,
  input jsonb not null default '{}'::jsonb,
  status text not null default 'queued' check (status in (
    'queued','launching','starting','running','waiting','completed','partial_success',
    'failed','cancellation_requested','cancelled','timed_out','budget_exhausted')),
  launch_state text not null default 'none' check (launch_state in (
    'none','pending','launching','launched','launch_failed','launch_unknown')),
  idempotency_key text,
  worker_id text,
  lease_token text,
  lease_expires_at timestamptz,
  started_at timestamptz,
  finished_at timestamptz,
  attempt integer not null default 1,
  cancellation_requested_at timestamptz,
  cancellation_reason text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create or replace function public.set_updated_at() returns trigger language plpgsql as $fn$
begin new.updated_at = now(); return new; end $fn$;
create trigger runs_set_updated_at before update on public.runs
  for each row execute function public.set_updated_at();
""" + "".join(
    f"\ncreate table public.{table} (id bigserial primary key, run_id uuid not null);"
    for table in GOV_TRACE_TABLES
)


def _find_pg_bin():
    for candidate in PG_BIN_CANDIDATES:
        initdb = os.path.join(candidate, "initdb") if candidate else "initdb"
        if shutil.which(initdb):
            return candidate
    return None


class EphemeralPostgres:
    """A throwaway PostgreSQL cluster on a unix socket."""

    def __init__(self, pg_bin, port=PG_PORT):
        self.pg_bin = pg_bin
        self.port = port
        self.as_postgres_user = os.geteuid() == 0
        self.dir = tempfile.mkdtemp(prefix="milo-staged-", dir="/tmp")
        os.chmod(self.dir, 0o755)
        if self.as_postgres_user:
            shutil.chown(self.dir, "postgres", "postgres")

    def _server_cmd(self, command):
        if self.as_postgres_user:
            return ["su", "postgres", "-s", "/bin/bash", "-c", command]
        return ["/bin/bash", "-c", command]

    def start(self):
        initdb = os.path.join(self.pg_bin, "initdb")
        pg_ctl = os.path.join(self.pg_bin, "pg_ctl")
        subprocess.run(self._server_cmd(f"{initdb} -D {self.dir}/data -U postgres --auth=trust"),
                       check=True, capture_output=True)
        subprocess.run(self._server_cmd(
            f"{pg_ctl} -D {self.dir}/data -l {self.dir}/log -w "
            f"-o '-k {self.dir} -p {self.port} -c listen_addresses=' start"),
            check=True, capture_output=True)

    def stop(self):
        pg_ctl = os.path.join(self.pg_bin, "pg_ctl")
        subprocess.run(self._server_cmd(f"{pg_ctl} -D {self.dir}/data -m immediate stop"), capture_output=True)
        shutil.rmtree(self.dir, ignore_errors=True)

    def create_database(self, name="milo"):
        subprocess.run(["psql", "-h", self.dir, "-p", self.port, "-U", "postgres", "-d", "postgres",
                        "-X", "-q", "-c", f"create database {name}"], check=True, capture_output=True)

    def run_sql(self, sql):
        return subprocess.run(
            ["psql", "-h", self.dir, "-p", self.port, "-U", "postgres", "-d", "milo",
             "-v", "ON_ERROR_STOP=1", "-X", "-q", "-t", "-A"],
            input=sql, capture_output=True, text=True, timeout=120,
        )

    def query(self, sql):
        result = self.run_sql(sql)
        if result.returncode != 0:
            raise AssertionError(f"psql failed:\n{result.stderr}\n(sql: {sql})")
        return result.stdout.strip()


@pytest.fixture(scope="module")
def pg():
    pg_bin = _find_pg_bin()
    if pg_bin is None:
        if os.environ.get("MILO_REQUIRE_PG_TESTS"):
            pytest.fail("PostgreSQL server binaries are required but unavailable")
        pytest.skip("PostgreSQL server binaries unavailable")
    server = EphemeralPostgres(pg_bin)
    server.start()
    try:
        server.create_database()
        server.query(RUNS_DDL)
        yield server
    finally:
        server.stop()


def emitted_retire_sql() -> str:
    """The EXACT SQL the script prints — the same text an operator runs."""
    result = subprocess.run(
        ["bash", str(STAGE_D / "resolve-government-capture.sh")],
        capture_output=True, text=True, cwd=REPO, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    start = result.stdout.index("begin;")
    end = result.stdout.index("commit;", start) + len("commit;")
    return result.stdout[start:end]


def seed_capture_row(pg, *, trace_table=None, **overrides):
    """Seed the prepared capture row, optionally with an execution trace.

    Defaults reproduce the exact live row: the operator-capture marker, the
    owning conversation, the requesting user and the untouched posture.
    """
    pg.query("delete from public.runs;")
    for table in GOV_TRACE_TABLES:
        pg.query(f"delete from public.{table};")
    row = {
        "status": "queued",
        "launch_state": "none",
        "idempotency_key": GOV_KEY,
        "conversation_id": GOV_CONVERSATION_ID,
        "requested_by": GOV_REQUESTED_BY,
        "operation": GOV_OPERATION,
        "worker_id": None,
        "lease_token": None,
        "lease_expires_at": None,
        "started_at": None,
        "finished_at": None,
        "attempt": 1,
    }
    row.update(overrides)

    def lit(value):
        return "null" if value is None else "'" + str(value).replace("'", "''") + "'"

    def uuid_lit(value):
        return "null" if value is None else lit(value) + "::uuid"

    if row["operation"] is None:
        input_json = '{"content": "operator catalog capture run"}'
    else:
        input_json = json.dumps({"content": "operator catalog capture run",
                                 "metadata": {"milo_operation": row["operation"]}})

    pg.query(
        "insert into public.runs (id, conversation_id, requested_by, input, status, launch_state, "
        "idempotency_key, worker_id, lease_token, lease_expires_at, started_at, finished_at, attempt) values "
        f"('{GOV_RUN_ID}'::uuid, {uuid_lit(row['conversation_id'])}, {uuid_lit(row['requested_by'])}, "
        f"{lit(input_json)}::jsonb, {lit(row['status'])}, {lit(row['launch_state'])}, "
        f"{lit(row['idempotency_key'])}, {lit(row['worker_id'])}, {lit(row['lease_token'])}, "
        f"{lit(row['lease_expires_at'])}, {lit(row['started_at'])}, {lit(row['finished_at'])}, "
        f"{row['attempt']});"
    )
    if trace_table:
        pg.query(f"insert into public.{trace_table} (run_id) values ('{GOV_RUN_ID}'::uuid);")


def capture_state(pg):
    return pg.query(
        "select status || '|' || launch_state || '|' || coalesce(worker_id, 'none') || '|' || "
        f"coalesce(started_at::text, 'none') from public.runs where id = '{GOV_RUN_ID}'::uuid;"
    )


def test_guarded_cas_retires_the_prepared_capture_exactly_once(pg):
    sql = emitted_retire_sql()
    seed_capture_row(pg)
    assert capture_state(pg) == "queued|none|none|none"

    first = pg.run_sql(sql)
    assert first.returncode == 0, first.stderr
    assert capture_state(pg) == "cancelled|none|none|none"
    # The run is now terminal, so claim_run_lease's acquirable set excludes
    # it: no worker can ever claim it again.
    assert pg.query(f"select status from public.runs where id = '{GOV_RUN_ID}'::uuid;") == "cancelled"

    # Re-running is NOT silently idempotent: the guard refuses, because the
    # pre-state no longer matches. Nothing changes.
    second = pg.run_sql(sql)
    assert second.returncode != 0
    assert "STAGE_D_GOV_GUARD_IDENTITY" in second.stderr
    assert capture_state(pg) == "cancelled|none|none|none"


@pytest.mark.parametrize("drift", [
    # posture drift
    {"status": "running"},
    {"status": "completed"},
    {"status": "cancellation_requested"},
    {"launch_state": "launched"},
    {"launch_state": "pending"},
    {"worker_id": "worker-abc"},
    {"lease_token": "deadbeef"},
    {"lease_expires_at": "2026-09-19T00:05:00Z"},
    {"started_at": "2026-09-19T00:00:00Z"},
    {"finished_at": "2026-09-19T00:00:00Z"},
    {"attempt": 2},
    # identity drift — a row sharing the primary key is not the capture
    {"idempotency_key": "some-other-key"},
    {"idempotency_key": None},
    {"operation": "something.else"},
    {"operation": None},
    {"conversation_id": "11111111-1111-1111-1111-111111111111"},
    {"conversation_id": None},
    {"requested_by": "22222222-2222-2222-2222-222222222222"},
    {"requested_by": None},
])
def test_guarded_cas_refuses_every_drifted_pre_state_and_rolls_back(pg, drift):
    sql = emitted_retire_sql()
    seed_capture_row(pg, **drift)
    before = capture_state(pg)

    result = pg.run_sql(sql)
    assert result.returncode != 0, f"drift {drift} was silently accepted"
    assert "STAGE_D_GOV_GUARD_IDENTITY" in result.stderr
    # Fully rolled back: the row is byte-for-byte what it was.
    assert capture_state(pg) == before


@pytest.mark.parametrize("trace_table", GOV_TRACE_TABLES)
def test_guarded_cas_refuses_a_capture_that_shows_any_execution_trace(pg, trace_table):
    """A single trace row means a worker touched it: retiring would be
    destroying evidence, not closing an unused preparation."""
    sql = emitted_retire_sql()
    seed_capture_row(pg, trace_table=trace_table)
    before = capture_state(pg)
    assert before == "queued|none|none|none"  # posture alone still looks pristine

    result = pg.run_sql(sql)
    assert result.returncode != 0, f"a trace row in {trace_table} was silently accepted"
    assert "STAGE_D_GOV_GUARD_TRACE" in result.stderr
    assert trace_table in result.stderr
    assert capture_state(pg) == before


def test_guarded_cas_touches_no_other_run(pg):
    sql = emitted_retire_sql()
    seed_capture_row(pg)
    pg.query(
        "insert into public.runs (id, status, launch_state, idempotency_key, input) values "
        "('11111111-1111-1111-1111-111111111111'::uuid, 'queued', 'none', 'another-queued-run', "
        "'{\"metadata\": {\"milo_operation\": \"catalog.government.capture\"}}'::jsonb);"
    )
    assert pg.run_sql(sql).returncode == 0
    assert pg.query(
        "select status || '|' || launch_state from public.runs "
        "where id = '11111111-1111-1111-1111-111111111111'::uuid;"
    ) == "queued|none"
    # And no row was deleted.
    assert pg.query("select count(*) from public.runs;") == "2"


def test_guarded_cas_refuses_when_the_run_is_absent(pg):
    sql = emitted_retire_sql()
    pg.query("delete from public.runs;")
    result = pg.run_sql(sql)
    assert result.returncode != 0
    assert "STAGE_D_GOV_GUARD_IDENTITY" in result.stderr
    assert pg.query("select count(*) from public.runs;") == "0"


def test_guarded_cas_follows_the_repository_state_machine(pg):
    """queued -> cancellation_requested -> cancelled, both steps guarded.

    A direct queued -> cancelled is NOT in backend/runtime.py's
    VALID_TRANSITIONS, so the script must not forge one.
    """
    sql = emitted_retire_sql()
    assert "'cancellation_requested'" in sql
    assert "'cancelled'" in sql
    assert sql.index("'cancellation_requested'") < sql.index("status      = 'cancelled'")
    # Both steps inside ONE transaction: claim_run_lease CAN acquire from
    # 'cancellation_requested', so it must never be externally visible.
    assert sql.startswith("begin;") and sql.rstrip().endswith("commit;")
    assert sql.count("begin;") == 1 and sql.count("commit;") == 1


# ---------------------------------------------------------------------------
# I. Static safety properties of the resolution script and the runbook
# ---------------------------------------------------------------------------

def squash(text: str) -> str:
    """Collapse the emitted SQL's alignment padding for comparison."""
    return re.sub(r"\s+", " ", text)


def test_resolution_never_emits_an_unconditional_update():
    """Every UPDATE carries the full expected pre-state, and the complete
    invariant is asserted inside the transaction before either of them."""
    sql = emitted_retire_sql()

    # The identity + trace preconditions run BEFORE the first UPDATE.
    identity_at = sql.index("STAGE_D_GOV_GUARD_IDENTITY")
    trace_at = sql.index("STAGE_D_GOV_GUARD_TRACE")
    first_update_at = sql.index("update public.runs set")
    assert identity_at < first_update_at
    assert trace_at < first_update_at
    for table in GOV_TRACE_TABLES:
        assert f"from public.{table}" in sql, f"{table} is not counted by the guard"

    updates = sql.split("update public.runs set")[1:]
    assert len(updates) == 2
    for body in updates:
        where = squash(body.split("where", 1)[1].split(";", 1)[0])
        assert f"'{GOV_RUN_ID}'::uuid" in where
        assert "launch_state = 'none'" in where
        assert "worker_id is null" in where
        assert "lease_token is null" in where
        assert "started_at is null" in where
        # No UPDATE may rely on the primary key alone.
        assert where.count("and ") >= 4, f"under-guarded WHERE clause: {where}"

    # The first transition additionally re-asserts the full identity.
    first_where = squash(updates[0].split("where", 1)[1].split(";", 1)[0])
    for fragment in (f"idempotency_key = '{GOV_KEY}'",
                     f"milo_operation' = '{GOV_OPERATION}'",
                     f"conversation_id = '{GOV_CONVERSATION_ID}'::uuid",
                     f"requested_by = '{GOV_REQUESTED_BY}'::uuid",
                     "attempt = 1"):
        assert fragment in first_where, f"missing from the guarded WHERE: {fragment}"

    # Each step asserts an affected-row count of exactly one.
    assert sql.count("get diagnostics v_rows = row_count;") == 2
    # Three v_rows assertions: the identity precondition plus both transitions.
    assert sql.count("if v_rows <> 1 then") == 3
    assert sql.count("if v_traces <> 0 then") == 1
    assert sql.count("raise exception") == 4  # identity, trace, and both transitions


def test_resolution_never_executes_or_enables_the_capture():
    text = (STAGE_D / "resolve-government-capture.sh").read_text()
    for forbidden in ("--capture", "operator_capture", "MILO_ENABLE_CATALOG_EXECUTION=true",
                      "claim_run_lease(", "delete from public.runs"):
        assert forbidden not in text, f"the resolution script references {forbidden!r}"
    assert "NEVER executes, launches, claims or resumes the capture" in text


def test_resolution_requires_the_full_operator_guard_for_any_mutation():
    text = (STAGE_D / "resolve-government-capture.sh").read_text()
    assert "apply_guard" in text
    assert "--confirm-production-change" in text
    assert "MILO_OPERATOR_ACK" in text
    # The run id is a pinned constant, never a command-line argument.
    assert "--run-id" not in text
    assert 'RUN_ID="${STAGE_D_GOV_CAPTURE_RUN_ID}"' in text


def test_resolution_default_mode_mutates_nothing():
    result = subprocess.run(
        ["bash", str(STAGE_D / "resolve-government-capture.sh")],
        capture_output=True, text=True, cwd=REPO, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "THE CAPTURE IS NEVER EXECUTED BY THIS SCRIPT." in result.stdout
    assert "RESULT: OK" in result.stdout


def test_resolution_offers_both_reviewed_outcomes():
    text = (STAGE_D / "resolve-government-capture.sh").read_text()
    assert "retire|leave-prepared" in text
    assert "Preparing a run is not" in text


# ---------------------------------------------------------------------------
# J. Step-script structure: gates run before mutations; probes are disposable
# ---------------------------------------------------------------------------

def test_there_is_no_build_or_deploy_step_at_all():
    """Stage D must not be able to rebuild or redeploy the accepted release.

    A rebuild is not byte-reproducible here (mutable python:3.12-slim base,
    unpinned openai>=1.30.0, no lockfile), so pushing the same tag would
    REPLACE the accepted image rather than re-prove it.
    """
    assert not (STAGE_D / "01-build-images.sh").exists()
    assert not (STAGE_D / "02-deploy-images.sh").exists()
    # No build, push or tag-moving command anywhere.
    forbidden = ("gcloud builds submit", "docker build", "docker push",
                 "gcloud artifacts docker tags", "cloudbuild")
    for path in STAGE_D.iterdir():
        if not path.is_file() or path.suffix not in (".sh", ".py"):
            continue
        code = [line for line in path.read_text().splitlines()
                if line.strip() and not line.lstrip().startswith("#")]
        for marker in forbidden:
            offenders = [line for line in code if marker in line]
            assert not offenders, f"{path.name} can build/push/re-tag: {offenders}"

    # The RELEASE images are never set on any surface. (The disposable
    # probe jobs legitimately run the stock python:3.12-slim image — that
    # is not the release and never reaches the API service or Worker job.)
    for path in STAGE_D.glob("*.sh"):
        code = [line for line in path.read_text().splitlines()
                if line.strip() and not line.lstrip().startswith("#")]
        release_image_writes = [
            line for line in code
            if "--image=" in line and "STAGE_D_REGISTRY" in line
        ]
        assert not release_image_writes, f"{path.name} sets a release image: {release_image_writes}"
        for verb in ("jobs update", "services update"):
            assert not [line for line in code if verb in line and "--image" in line], \
                f"{path.name} updates a Cloud Run image via {verb}"

    # The probe image must stay the stock one, never anything from the
    # release registry.
    probes = (STAGE_D / "04-create-probes.sh").read_text()
    assert "--image=python:3.12-slim" in probes
    assert f"--image={REGISTRY}" not in probes


def test_the_toolkit_documents_why_a_rebuild_is_not_a_proof():
    """The reasoning must be written down where an operator will read it."""
    for path, needle in (
        (STAGE_D / "stage-d-env.sh", "openai>=1.30.0"),
        (STAGE_D / "01-verify-release-images.sh", "python:3.12-slim"),
        (STAGE_D / "verify_images.py", "not reproducible"),
        (STAGE_D / "README.md", "not byte-reproducible"),
        (REPO / "docs/production-readiness/STAGE_D_AUTHORIZATION.md", "openai>=1.30.0"),
    ):
        assert needle in path.read_text(), f"{path.name} does not explain {needle!r}"


def test_execute_step_gates_on_acceptable_terminal_and_exact_caps():
    text = (STAGE_D / "05-execute-run.sh").read_text()
    assert "verify_caps.py" in text
    assert "verify_executions.py" in text
    assert "STAGE_D_MODE=preflight" in text
    assert 'test "${RUN_ID}" != "${STAGE_D_GOV_CAPTURE_RUN_ID}"' in text
    assert "acceptable" in text


def test_collect_evidence_is_a_gate_not_a_checklist():
    text = (STAGE_D / "06-collect-evidence.sh").read_text()
    assert "--async --format='value(metadata.name)'" in text
    code_lines = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    assert not any("--wait" in line for line in code_lines)
    assert "probe_ok" in text and "execution_state.py" in text
    assert "expected_total_executions=$((STAGE_D_EXPECTED_PRIOR_EXECUTIONS + 1))" in text
    assert "is the prepared Government capture run" in text


def test_probe_transport_is_size_and_delimiter_checked_before_any_mutation():
    text = (STAGE_D / "04-create-probes.sh").read_text()
    assert text.index("check_transport_value") < text.index("gcloud run jobs create")
    assert "CLOUD_RUN_ENV_VALUE_MAX=32768" in text
    assert "round-trip mismatch" in text


def test_probe_sources_fit_the_cloud_run_env_value_limit():
    """The compressed transport must actually fit — Stage C learned this."""
    for name in ("probe_db.py", "probe_gateway.py"):
        raw = (STAGE_D / name).read_bytes()
        encoded = base64.b64encode(gzip.compress(raw, compresslevel=9, mtime=0)).decode("ascii")
        assert gzip.decompress(base64.b64decode(encoded)) == raw
        assert len(encoded) <= 32768, f"{name} compresses to {len(encoded)} chars, over the Cloud Run limit"
        assert ":::" not in encoded


def test_probes_are_created_with_the_operator_controlled_identities():
    text = (STAGE_D / "04-create-probes.sh").read_text()
    assert "${STAGE_D_API_SA}" in text and "${STAGE_D_GATEWAY_SA}" in text
    # The gateway probe holds NO secrets.
    gw_block = text.split("Creating ${STAGE_D_GW_PROBE_JOB}")[1]
    assert "--set-secrets" not in gw_block


def test_no_stage_d_script_prints_a_secret_value():
    for path in STAGE_D.iterdir():
        if path.is_file() and path.suffix in (".sh", ".py"):
            text = path.read_text()
            assert "gcloud secrets versions access" not in text
            assert "SUPABASE_SERVICE_ROLE_KEY}" not in text


# ---------------------------------------------------------------------------
# K. Documentation states PROPOSED, never completed acceptance
# ---------------------------------------------------------------------------

AUTHORIZATION_DOC = REPO / "docs" / "production-readiness" / "STAGE_D_AUTHORIZATION.md"


def test_authorization_doc_is_marked_proposed_not_accepted():
    text = AUTHORIZATION_DOC.read_text()
    assert "PROPOSED" in text
    assert "Nothing in this proposal has been executed" in text
    assert "Merging this PR authorizes nothing" in text
    for false_claim in ("Stage D PASSED", "Stage D is PASSED", "Stage D acceptance record",
                        "STAGE D PASSED"):
        assert false_claim not in text, f"the doc claims completed acceptance: {false_claim!r}"


def test_authorization_doc_records_the_discovered_baselines():
    text = AUTHORIZATION_DOC.read_text()
    for fact in (GOV_RUN_ID, GOV_KEY, STAGE_D_KEY, RELEASE_SHA,
                 "big-cabinet-457321-t7", "us-central1"):
        assert fact in text
    assert "7" in text and "8" in text


def test_authorization_doc_states_the_cap_derivation_and_the_held_values():
    text = AUTHORIZATION_DOC.read_text()
    assert "RETRY_LIMIT_REACHED" in text
    assert "0.252069" in text and "312,018" in text and "84" in text
    for name in STAGE_C_CAPS:
        assert name in text, f"{name} is not in the cap table"


def test_readme_is_marked_proposed_and_not_executed():
    text = (STAGE_D / "README.md").read_text()
    assert "PROPOSED — NOT AUTHORIZED, NOT EXECUTED" in text
    assert "Merging this PR authorizes nothing" in text


def test_stage_c_acceptance_record_still_says_stage_c_is_consumed():
    """Stage D must not quietly rewrite the Stage C record."""
    text = (REPO / "docs" / "production-readiness" / "STAGE_C_ACCEPTANCE.md").read_text()
    assert "Stage C: **PASSED (2026-08-22).**" in text
    assert "consumed" in text


def test_staged_activation_points_at_the_stage_d_proposal():
    text = (REPO / "docs" / "production-readiness" / "STAGED_ACTIVATION.md").read_text()
    assert "STAGE_D_AUTHORIZATION.md" in text
    assert "scripts/release/stage-d" in text


# ---------------------------------------------------------------------------
# L. verify_images.py — the accepted release is a DIGEST, not a tag
# ---------------------------------------------------------------------------

API_REPO = f"{REGISTRY}/api"
WORKER_REPO = f"{REGISTRY}/worker"
READY_REVISION = "milo-agent-api-00080-nm8"


def registry_listing(api_digest=API_DIGEST, worker_digest=WORKER_DIGEST, tag=RELEASE_SHA, extra=None):
    listing = [
        {"package": API_REPO, "version": api_digest, "tags": tag},
        {"package": WORKER_REPO, "version": worker_digest, "tags": tag},
    ]
    listing.extend(extra or [])
    return listing


def api_service_doc(ready=READY_REVISION, percent=100):
    return {"status": {"latestReadyRevisionName": ready,
                       "traffic": [{"revisionName": ready, "percent": percent}]}}


def api_revision_doc(image=None, name=READY_REVISION):
    return {"metadata": {"name": name},
            "spec": {"containers": [{"image": image or f"{API_REPO}@{API_DIGEST}"}]}}


def worker_job_doc(image=None):
    return {"spec": {"template": {"spec": {"template": {"spec": {"containers": [
        {"image": image or f"{WORKER_REPO}:{RELEASE_SHA}"}]}}}}}}


def execution_doc(image=None, name="milo-agent-worker-staged1"):
    return {"metadata": {"name": name},
            "spec": {"template": {"spec": {"containers": [
                {"image": image or f"{WORKER_REPO}@{WORKER_DIGEST}"}]}}}}


def run_verify_images(tmp_path, *, registry=None, service=None, revision=None, job=None,
                      execution=None, api_digest=API_DIGEST, worker_digest=WORKER_DIGEST):
    paths = {}
    for label, doc in (("registry", registry if registry is not None else registry_listing()),
                       ("service", service if service is not None else api_service_doc()),
                       ("revision", revision if revision is not None else api_revision_doc()),
                       ("job", job if job is not None else worker_job_doc())):
        path = tmp_path / f"{label}.json"
        path.write_text(doc if isinstance(doc, str) else json.dumps(doc))
        paths[label] = str(path)
    argv = [sys.executable, str(STAGE_D / "verify_images.py"),
            "--registry-json", paths["registry"], "--api-service-json", paths["service"],
            "--api-revision-json", paths["revision"], "--worker-job-json", paths["job"]]
    if execution is not None:
        exec_path = tmp_path / "execution.json"
        exec_path.write_text(json.dumps(execution))
        argv += ["--execution-json", str(exec_path)]
    return subprocess.run(
        argv, capture_output=True, text=True,
        env={**os.environ, "STAGE_D_REGISTRY": REGISTRY, "STAGE_D_RELEASE_SHA": RELEASE_SHA,
             "STAGE_D_API_IMAGE_DIGEST": api_digest, "STAGE_D_WORKER_IMAGE_DIGEST": worker_digest},
        timeout=60)


def test_verify_images_passes_on_the_accepted_digests(tmp_path):
    result = run_verify_images(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    verdict = json.loads(result.stdout)
    assert verdict["ok"] is True
    assert verdict["registry_resolution"] == {"api": API_DIGEST, "worker": WORKER_DIGEST}
    assert verdict["api_serving_revision"]["digest"] == API_DIGEST
    assert verdict["worker_job"]["digest"] == WORKER_DIGEST


def test_verify_images_refuses_a_moved_registry_tag(tmp_path):
    """The exact hazard: same tag, different bytes."""
    result = run_verify_images(
        tmp_path, registry=registry_listing(worker_digest=STALE_WORKER_DIGEST))
    assert result.returncode != 0
    out = result.stdout
    assert "the worker tag" in out and "was re-pushed" in out
    assert "separate reviewed release" in out


def test_verify_images_refuses_a_moved_api_tag(tmp_path):
    result = run_verify_images(tmp_path, registry=registry_listing(api_digest="sha256:" + "a" * 64))
    assert result.returncode != 0
    assert "the api tag" in result.stdout


def test_a_tag_match_alone_is_never_acceptance(tmp_path):
    """Everything still carries the right TAG; only the digest moved."""
    moved = registry_listing(worker_digest=STALE_WORKER_DIGEST)
    result = run_verify_images(tmp_path, registry=moved, job=worker_job_doc(f"{WORKER_REPO}:{RELEASE_SHA}"))
    assert result.returncode != 0
    verdict = json.loads(result.stdout)
    assert verdict["ok"] is False
    assert any("re-pushed" in problem or "was moved" in problem for problem in verdict["problems"])


def test_verify_images_refuses_a_missing_release_tag(tmp_path):
    result = run_verify_images(tmp_path, registry=[])
    assert result.returncode != 0
    assert "no image in" in result.stdout


def test_verify_images_refuses_an_ambiguous_registry_listing(tmp_path):
    duplicate = registry_listing() + [{"package": WORKER_REPO, "version": STALE_WORKER_DIGEST, "tags": RELEASE_SHA}]
    result = run_verify_images(tmp_path, registry=duplicate)
    assert result.returncode != 0
    assert "the tag is ambiguous" in result.stdout


def test_verify_images_refuses_a_serving_revision_on_the_wrong_digest(tmp_path):
    result = run_verify_images(tmp_path, revision=api_revision_doc(f"{API_REPO}@sha256:" + "b" * 64))
    assert result.returncode != 0
    assert "production is NOT serving the accepted image" in result.stdout


def test_verify_images_refuses_when_the_serving_revision_is_not_taking_all_traffic(tmp_path):
    result = run_verify_images(tmp_path, service=api_service_doc(percent=50))
    assert result.returncode != 0
    assert "not serving 100% of traffic" in result.stdout


def test_verify_images_refuses_when_the_wrong_revision_was_inspected(tmp_path):
    result = run_verify_images(tmp_path, revision=api_revision_doc(name="milo-agent-api-00001-aaa"))
    assert result.returncode != 0
    assert "is not the serving revision" in result.stdout


def test_verify_images_refuses_a_worker_job_on_a_foreign_digest(tmp_path):
    result = run_verify_images(tmp_path, job=worker_job_doc(f"{WORKER_REPO}@{STALE_WORKER_DIGEST}"))
    assert result.returncode != 0
    assert "production is NOT serving the accepted image" in result.stdout


def test_verify_images_refuses_a_worker_job_on_an_unpinned_tag(tmp_path):
    result = run_verify_images(tmp_path, job=worker_job_doc(f"{WORKER_REPO}:latest"))
    assert result.returncode != 0
    assert "is not the pinned release" in result.stdout


def test_verify_images_refuses_an_image_from_another_repository(tmp_path):
    result = run_verify_images(
        tmp_path, job=worker_job_doc("us-central1-docker.pkg.dev/attacker/evil/worker:" + RELEASE_SHA))
    assert result.returncode != 0
    assert "not from the pinned repository" in result.stdout


def test_verify_images_says_plainly_when_the_worker_reference_is_a_mutable_tag(tmp_path):
    """A Cloud Run job is not a revision: it resolves the tag every run."""
    result = run_verify_images(tmp_path, job=worker_job_doc(f"{WORKER_REPO}:{RELEASE_SHA}"))
    assert result.returncode == 0
    verdict = json.loads(result.stdout)
    assert verdict["worker_job"]["reference_kind"] == "tag"
    assert any("point-in-time" in note for note in verdict["notes"])


def test_verify_images_reports_a_digest_pinned_worker_as_the_stronger_form(tmp_path):
    result = run_verify_images(tmp_path, job=worker_job_doc(f"{WORKER_REPO}@{WORKER_DIGEST}"))
    assert result.returncode == 0
    assert json.loads(result.stdout)["worker_job"]["reference_kind"] == "digest"


def test_verify_images_fails_closed_without_the_pinned_digests(tmp_path):
    assert run_verify_images(tmp_path, api_digest="").returncode != 0
    assert run_verify_images(tmp_path, worker_digest="").returncode != 0
    assert run_verify_images(tmp_path, worker_digest="not-a-digest").returncode != 0


@pytest.mark.parametrize("field", ["registry", "service", "revision", "job"])
def test_verify_images_fails_closed_on_unreadable_input(tmp_path, field):
    result = run_verify_images(tmp_path, **{field: "{not json"})
    assert result.returncode != 0
    assert "failing closed" in result.stdout


def test_verify_images_proves_what_the_authorized_execution_actually_ran(tmp_path):
    result = run_verify_images(tmp_path, execution=execution_doc())
    assert result.returncode == 0
    assert json.loads(result.stdout)["worker_execution"]["digest"] == WORKER_DIGEST


def test_verify_images_refuses_an_execution_that_ran_a_different_digest(tmp_path):
    """The check a moved tag cannot defeat, because the execution records it."""
    result = run_verify_images(tmp_path, execution=execution_doc(f"{WORKER_REPO}@{STALE_WORKER_DIGEST}"))
    assert result.returncode != 0
    assert "worker execution" in result.stdout
    assert "production is NOT serving the accepted image" in result.stdout


def test_verify_images_refuses_an_execution_recorded_only_as_a_tag(tmp_path):
    """Without a recorded digest, what it ran cannot be established."""
    result = run_verify_images(tmp_path, execution=execution_doc(f"{WORKER_REPO}:{RELEASE_SHA}"))
    assert result.returncode != 0
    assert "cannot be established" in result.stdout


def test_the_image_gate_runs_before_run_creation_and_in_the_evidence_gate():
    execute = (STAGE_D / "05-execute-run.sh").read_text()
    assert "verify_images.py" in execute
    assert execute.index("verify_images.py") < execute.index("STAGE_D_MODE=create")
    evidence = (STAGE_D / "06-collect-evidence.sh").read_text()
    assert "--execution-json" in evidence
    assert "latestCreatedExecution" in evidence


# ---------------------------------------------------------------------------
# M. Stage D test-project isolation is PROVED, not asserted
# ---------------------------------------------------------------------------

STAGE_D_PROJECT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
STAGE_D_USER_ID = "ffffffff-1111-2222-3333-444444444444"
STAGE_D_CONVERSATION = "99999999-8888-7777-6666-555555555555"


#: Distinguishes "use the default" from an explicit null, so a test can
#: express a project whose configuration really is null.
_DEFAULT = object()


def wire_setup(db, monkeypatch, *, workflow_key="vehicle_catalog_v1",
               configuration=_DEFAULT, members=_DEFAULT, user_active=0, project_active=0,
               project_id=STAGE_D_PROJECT_ID, conversations=_DEFAULT):
    """Route probe_db.setup()'s HTTP layer at a deterministic fake."""
    if configuration is _DEFAULT:
        configuration = {"stage": "stage-d"}
    if members is _DEFAULT:
        members = [{"user_id": STAGE_D_USER_ID, "role": "owner"}]
    if conversations is _DEFAULT:
        conversations = [{"id": STAGE_D_CONVERSATION}]

    def fake_call(method, path, body=None, headers=None):
        if path == "/auth/v1/admin/users":
            return 201, {"id": STAGE_D_USER_ID}
        if path.startswith("/rest/v1/projects") and method == "POST":
            # Simulate the "already exists" path so the REUSE branch, which
            # is the one that must prove everything, is exercised.
            return 409, {"code": "23505"}
        if path.startswith("/rest/v1/projects") and method == "GET":
            return 200, [{"id": project_id, "workflow_key": workflow_key,
                          "configuration": configuration}]
        if path.startswith("/rest/v1/project_members") and method == "POST":
            return 201, None
        if path.startswith("/rest/v1/project_members") and method == "GET":
            return 200, members
        if path.startswith("/rest/v1/conversations") and method == "GET":
            return 200, conversations
        return 200, []

    def fake_count(path):
        if "requested_by=eq." in path:
            return user_active
        if "conversation_id=in." in path:
            return project_active
        return 0

    monkeypatch.setattr(db, "call", fake_call)
    monkeypatch.setattr(db, "count_exact", fake_count)


def setup_verdict(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_setup_passes_and_reports_the_proved_isolation(db, monkeypatch, capsys):
    wire_setup(db, monkeypatch)
    db.setup()
    verdict = setup_verdict(capsys)
    assert verdict["ok"] is True, verdict.get("problems")
    assert verdict["members"] == [STAGE_D_USER_ID]
    assert verdict["active_runs_for_user"] == 0
    assert verdict["active_runs_for_project"] == 0
    assert verdict["configuration"] == {"stage": "stage-d"}


def test_setup_refuses_a_reused_project_with_the_wrong_engine(db, monkeypatch, capsys):
    wire_setup(db, monkeypatch, workflow_key="swarm_v2")
    with pytest.raises(SystemExit):
        db.setup()
    verdict = setup_verdict(capsys)
    assert any("workflow_key" in p for p in verdict["problems"])


@pytest.mark.parametrize("configuration", [{}, {"stage": "stage-c"}, {"stage": "stage-d", "extra": 1}, None])
def test_setup_refuses_a_reused_project_with_unexpected_configuration(db, monkeypatch, capsys, configuration):
    """A project wearing the slug but carrying something else is not it."""
    wire_setup(db, monkeypatch, configuration=configuration)
    with pytest.raises(SystemExit):
        db.setup()
    verdict = setup_verdict(capsys)
    assert any("configuration" in p for p in verdict["problems"])


@pytest.mark.parametrize("members", [
    [],
    [{"user_id": "someone-else", "role": "owner"}],
    [{"user_id": STAGE_D_USER_ID, "role": "owner"}, {"user_id": "someone-else", "role": "member"}],
])
def test_setup_refuses_any_membership_other_than_the_dedicated_user(db, monkeypatch, capsys, members):
    """Another member could create a run and break the one-run guarantee."""
    wire_setup(db, monkeypatch, members=members)
    with pytest.raises(SystemExit):
        db.setup()
    verdict = setup_verdict(capsys)
    assert any("membership" in p for p in verdict["problems"])


def test_setup_refuses_an_active_run_for_the_test_user(db, monkeypatch, capsys):
    wire_setup(db, monkeypatch, user_active=1)
    with pytest.raises(SystemExit):
        db.setup()
    assert any("PER_USER" in p for p in setup_verdict(capsys)["problems"])


def test_setup_refuses_an_active_run_anywhere_in_the_test_project(db, monkeypatch, capsys):
    """The cap is per PROJECT too — another user's run in it also blocks."""
    wire_setup(db, monkeypatch, project_active=1)
    with pytest.raises(SystemExit):
        db.setup()
    assert any("PER_PROJECT" in p for p in setup_verdict(capsys)["problems"])


def test_setup_refuses_a_forbidden_project_id(db, monkeypatch, capsys):
    monkeypatch.setenv("STAGE_D_FORBIDDEN_PROJECT_IDS", STAGE_D_PROJECT_ID)
    wire_setup(db, monkeypatch)
    with pytest.raises(SystemExit):
        db.setup()
    assert "forbidden list" in capsys.readouterr().out


def test_setup_never_merely_claims_test_user_only(db, monkeypatch, capsys):
    """The old output printed an unverified claim; it must query instead."""
    wire_setup(db, monkeypatch)
    db.setup()
    out = capsys.readouterr().out
    assert '"members": "test user only"' not in out
    assert "/rest/v1/project_members?project_id=eq." in (STAGE_D / "probe_db.py").read_text()


# ---------------------------------------------------------------------------
# N. The web-search cost statement is honest about what is NOT bounded
# ---------------------------------------------------------------------------


def test_the_withdrawn_hard_total_bound_is_not_claimed_anywhere():
    """The "<= $5.50 total" claim rested on an unenforced assumption."""
    doc = AUTHORIZATION_DOC.read_text()
    readme = (STAGE_D / "README.md").read_text()
    for text, label in ((doc, "authorization doc"), (readme, "README")):
        assert "Conservative maximum total" not in text, f"{label} still claims a total bound"
    # The doc may only mention $5.50 to withdraw it.
    for line in doc.splitlines():
        if "5.50" in line:
            context = doc[max(0, doc.index(line) - 400):doc.index(line) + 400]
            assert "withdrawn" in context or "wrong" in context, f"unretracted claim: {line}"
    assert "5.50" not in readme


def test_the_cost_statement_separates_tracked_from_untracked():
    doc = AUTHORIZATION_DOC.read_text()
    assert "Tracked, token-derived cost — HARD-CAPPED at $1.00" in doc
    assert "NOT CAPPED BY MILO AT ALL" in doc
    assert "not bounded by this repository" in doc


def test_the_cost_statement_cites_the_current_official_fee_and_deprecation():
    doc = AUTHORIZATION_DOC.read_text()
    assert "$0.005" in doc
    assert "2026-10-20" in doc
    for text in (doc, (STAGE_D / "README.md").read_text()):
        assert "0.005" in text and "2026-10-20" in text


def test_the_cost_statement_names_the_real_runtime_behaviour():
    """MAX_TOOL_ROUNDS bounds rounds; tool_calls per round is unbounded."""
    doc = AUTHORIZATION_DOC.read_text()
    assert "MAX_TOOL_ROUNDS = 15" in doc
    assert "message.tool_calls" in doc
    assert "not bounded by the runtime" in doc


def test_the_runtime_claim_in_the_doc_matches_the_actual_runtime():
    """If the runtime ever gains a cap, this doc must stop saying it has none."""
    core = (REPO / "backend/engines/vehicle_catalog_v1/core.py").read_text()
    assert "MAX_TOOL_ROUNDS = 15" in core
    # The loop still iterates every tool call with no per-response ceiling.
    assert core.count("for tool_call in message.tool_calls or []:") == 2
    assert "MAX_TOOL_CALLS_PER_RESPONSE" not in core


def test_a_provider_wallet_ceiling_is_a_prerequisite_not_a_suggestion():
    doc = AUTHORIZATION_DOC.read_text()
    assert "PREREQUISITE" in doc
    assert "spending/wallet ceiling" in doc
    # It must appear in the operator steps AND in the results table.
    steps = doc[doc.index("## 8. Remaining manual operator steps"):doc.index("## 9. Results")]
    assert "wallet ceiling" in steps
    results = doc[doc.index("## 9. Results"):]
    assert "wallet ceiling" in results


def test_this_pr_proposes_no_runtime_change():
    """Adding an enforceable web-search cap is a separate reviewed release."""
    doc = AUTHORIZATION_DOC.read_text()
    assert "No runtime change is proposed here" in doc
    changed = subprocess.run(
        ["git", "diff", "--name-only", RELEASE_SHA],
        cwd=REPO, capture_output=True, text=True, timeout=60)
    if changed.returncode != 0:
        pytest.skip("the pinned release SHA is not available in this checkout")
    touched = [f for f in changed.stdout.split() if f.startswith("backend/")]
    assert not touched, f"this PR changes runtime code: {touched}"

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
    "STAGE_D_WORKFLOW_KEY": "vehicle_catalog_v1",
}

EXTRA_DUMPED = ["STAGE_D_CAPS", "STAGE_D_REGISTRY", "STAGE_D_REPO_URL", "STAGE_D_API_URL",
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
    ("STAGE_D_REPO_URL", "https://github.com/attacker/fork.git"),
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
    for script in ("02-deploy-images.sh", "05-execute-run.sh", "06-collect-evidence.sh",
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
             "STAGE_D_REGISTRY": REGISTRY, "STAGE_D_RELEASE_SHA": RELEASE_SHA},
        timeout=60,
    )


def test_verify_caps_passes_on_the_exact_authorized_posture(tmp_path):
    result = run_verify_caps(tmp_path, worker_spec(), api_spec())
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"release images {RELEASE_SHA[:12]}" in result.stdout


@pytest.mark.parametrize("surface,bad", [
    ("worker", {"image": f"{REGISTRY}/worker:88224bccc836f80f3dc1d173306a1aa63cddcc7a"}),
    ("worker", {"image": f"{REGISTRY}/worker:latest"}),
    ("api", {"image": f"{REGISTRY}/api:88224bccc836f80f3dc1d173306a1aa63cddcc7a"}),
    ("api", {"image": "us-central1-docker.pkg.dev/attacker/evil/api:" + RELEASE_SHA}),
])
def test_verify_caps_refuses_a_wrong_release_image(tmp_path, surface, bad):
    """WRONG RELEASE IMAGE."""
    worker = worker_spec(**bad) if surface == "worker" else worker_spec()
    api = api_spec(**bad) if surface == "api" else api_spec()
    result = run_verify_caps(tmp_path, worker, api)
    assert result.returncode != 0
    assert "is not the signed-off release" in result.stdout
    assert "do NOT create the run" in result.stdout


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
# G. 07-post-run-lockdown.sh — MISSING CLEANUP is a failure, not a warning
# ---------------------------------------------------------------------------

LOCKDOWN_MOCK_GCLOUD = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "${MOCK_LOG}"
case "$*" in
  *"run jobs list"*)
    cat "${MOCK_DIR}/jobs.txt" ;;
  *"run jobs delete"*)
    exit "${DELETE_EXIT:-0}" ;;
esac
exit 0
"""

# The kill switch is mocked wholesale: this test is about cleanup proof.
FAKE_KILL_SWITCH = "#!/usr/bin/env bash\necho 'KILL SWITCH APPLIED (mock)'\nexit ${KILL_EXIT:-0}\n"


def run_lockdown(tmp_path, jobs_listing, *, delete_exit=0, kill_exit=0):
    work = tmp_path / "stage-d"
    shutil.copytree(STAGE_D, work)
    (work / "kill-switch.sh").write_text(FAKE_KILL_SWITCH)
    (work / "kill-switch.sh").chmod(0o755)
    mock_dir = tmp_path / "mock"
    mock_dir.mkdir()
    (mock_dir / "jobs.txt").write_text("\n".join(jobs_listing) + ("\n" if jobs_listing else ""))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gcloud = bin_dir / "gcloud"
    gcloud.write_text(LOCKDOWN_MOCK_GCLOUD)
    gcloud.chmod(0o755)
    log = mock_dir / "gcloud.log"
    log.write_text("")
    return subprocess.run(
        ["bash", str(work / "07-post-run-lockdown.sh")],
        capture_output=True, text=True,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "MOCK_DIR": str(mock_dir),
             "MOCK_LOG": str(log), "DELETE_EXIT": str(delete_exit), "KILL_EXIT": str(kill_exit)},
        timeout=120,
    )


def test_lockdown_passes_when_both_probes_are_proven_absent(tmp_path):
    result = run_lockdown(tmp_path, ["milo-agent-worker"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "STAGE D LOCKDOWN COMPLETE" in result.stdout
    assert "OK: stage-d-db-probe is absent" in result.stdout
    assert "OK: stage-d-gw-probe is absent" in result.stdout


@pytest.mark.parametrize("survivor", ["stage-d-db-probe", "stage-d-gw-probe"])
def test_lockdown_refuses_when_a_probe_job_survives(tmp_path, survivor):
    """MISSING CLEANUP — a surviving probe is a standing credentialed path."""
    result = run_lockdown(tmp_path, ["milo-agent-worker", survivor])
    assert result.returncode != 0
    assert f"{survivor} still EXISTS after cleanup" in result.stderr
    assert "STAGE D LOCKDOWN INCOMPLETE" in result.stderr
    assert "STAGE D LOCKDOWN COMPLETE" not in result.stdout


def test_lockdown_refuses_when_a_probe_survives_a_failed_delete(tmp_path):
    """A delete that errors must never be read as successful cleanup."""
    result = run_lockdown(tmp_path, ["milo-agent-worker", "stage-d-db-probe"], delete_exit=1)
    assert result.returncode != 0
    assert "still EXISTS after cleanup" in result.stderr


def test_lockdown_never_infers_absence_from_the_delete_exit_status(tmp_path):
    """A successful delete with the job still listed must still fail."""
    result = run_lockdown(tmp_path, ["stage-d-gw-probe"], delete_exit=0)
    assert result.returncode != 0
    assert "stage-d-gw-probe still EXISTS" in result.stderr


def test_lockdown_fails_closed_when_the_job_listing_is_unobtainable(tmp_path):
    work = tmp_path / "stage-d"
    shutil.copytree(STAGE_D, work)
    (work / "kill-switch.sh").write_text(FAKE_KILL_SWITCH)
    (work / "kill-switch.sh").chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gcloud = bin_dir / "gcloud"
    gcloud.write_text("#!/usr/bin/env bash\ncase \"$*\" in *\"run jobs list\"*) exit 7 ;; esac\nexit 0\n")
    gcloud.chmod(0o755)
    result = subprocess.run(
        ["bash", str(work / "07-post-run-lockdown.sh")],
        capture_output=True, text=True,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"}, timeout=120,
    )
    assert result.returncode != 0
    assert "probe-job absence is UNVERIFIED" in result.stderr


def test_lockdown_refuses_when_the_kill_switch_did_not_complete(tmp_path):
    result = run_lockdown(tmp_path, ["milo-agent-worker"], kill_exit=1)
    assert result.returncode != 0
    assert "kill-switch.sh did not complete" in result.stderr


def test_kill_switch_does_not_delete_probe_jobs():
    """Deleting evidence-collection capability mid-incident is wrong."""
    assert "jobs delete" not in (STAGE_D / "kill-switch.sh").read_text()


# ---------------------------------------------------------------------------
# H. The Government-capture resolution, EXECUTED against real PostgreSQL
# ---------------------------------------------------------------------------

PG_BIN_CANDIDATES = ["/usr/lib/postgresql/16/bin", "/usr/lib/postgresql/15/bin", ""]
PG_PORT = "54994"

# Only the columns the guarded CAS reads or writes, with production's real
# constraints (verified read-only 2026-09-18) so an invalid status or
# launch_state would be rejected here exactly as it is in production.
RUNS_DDL = """
create table public.runs (
  id uuid primary key,
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
"""


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


def seed_capture_row(pg, **overrides):
    pg.query("delete from public.runs;")
    row = {"status": "queued", "launch_state": "none", "idempotency_key": GOV_KEY,
           "worker_id": None, "lease_token": None, "lease_expires_at": None,
           "started_at": None, "finished_at": None, "attempt": 1}
    row.update(overrides)

    def lit(value):
        return "null" if value is None else "'" + str(value).replace("'", "''") + "'"

    pg.query(
        f"insert into public.runs (id, status, launch_state, idempotency_key, worker_id, "
        f"lease_token, lease_expires_at, started_at, finished_at, attempt) values "
        f"('{GOV_RUN_ID}'::uuid, {lit(row['status'])}, {lit(row['launch_state'])}, "
        f"{lit(row['idempotency_key'])}, {lit(row['worker_id'])}, {lit(row['lease_token'])}, "
        f"{lit(row['lease_expires_at'])}, {lit(row['started_at'])}, {lit(row['finished_at'])}, "
        f"{row['attempt']});"
    )


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
    assert "STAGE_D_GOV_GUARD_1" in second.stderr
    assert capture_state(pg) == "cancelled|none|none|none"


@pytest.mark.parametrize("drift", [
    {"status": "running"},
    {"status": "completed"},
    {"status": "cancellation_requested"},
    {"launch_state": "launched"},
    {"launch_state": "pending"},
    {"worker_id": "worker-abc"},
    {"lease_token": "deadbeef"},
    {"started_at": "2026-09-19T00:00:00Z"},
    {"finished_at": "2026-09-19T00:00:00Z"},
    {"attempt": 2},
    {"idempotency_key": "some-other-key"},
    {"idempotency_key": None},
])
def test_guarded_cas_refuses_every_drifted_pre_state_and_rolls_back(pg, drift):
    sql = emitted_retire_sql()
    seed_capture_row(pg, **drift)
    before = capture_state(pg)

    result = pg.run_sql(sql)
    assert result.returncode != 0, f"drift {drift} was silently accepted"
    assert "STAGE_D_GOV_GUARD_1" in result.stderr
    # Fully rolled back: the row is byte-for-byte what it was.
    assert capture_state(pg) == before


def test_guarded_cas_touches_no_other_run(pg):
    sql = emitted_retire_sql()
    seed_capture_row(pg)
    pg.query(
        "insert into public.runs (id, status, launch_state, idempotency_key) values "
        "('11111111-1111-1111-1111-111111111111'::uuid, 'queued', 'none', 'another-queued-run');"
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
    assert "STAGE_D_GOV_GUARD_1" in result.stderr
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
    """Every UPDATE must carry the full expected pre-state."""
    sql = emitted_retire_sql()
    updates = sql.split("update public.runs set")[1:]
    assert len(updates) == 2
    for body in updates:
        # The clause ends at the statement's terminating semicolon, so
        # trailing prose from the next block can never satisfy an assertion.
        where = squash(body.split("where", 1)[1].split(";", 1)[0])
        assert f"'{GOV_RUN_ID}'::uuid" in where
        assert "launch_state = 'none'" in where
        assert "worker_id is null" in where
        assert "lease_token is null" in where
        assert "started_at is null" in where
        # No UPDATE may rely on the primary key alone.
        assert where.count("and ") >= 4, f"under-guarded WHERE clause: {where}"
    # Each step asserts an affected-row count of exactly one.
    assert sql.count("get diagnostics v_rows = row_count;") == 2
    assert sql.count("if v_rows <> 1 then") == 2
    assert sql.count("raise exception") == 2


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

def test_deploy_gates_before_any_mutation_and_again_after():
    text = (STAGE_D / "02-deploy-images.sh").read_text()
    pre = text.index('verify_execution_baseline "pre-deploy"')
    first_mutation = text.index("gcloud run jobs update")
    post = text.index('verify_execution_baseline "post-deploy"')
    assert pre < first_mutation < post
    assert text.index('verify_fail_closed_posture "pre-deploy"') < first_mutation


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

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
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from backend.provider_scheduler import ProviderLimitsConfig
from backend.runtime_policy import (CAP_ENV_PREFIXES, DIMENSIONS, ENGINE_ENV_PREFIXES,
                                    PROVIDER, PROVIDER_ENV_PREFIXES,
                                    reviewed_first_run_policy)

REPO = Path(__file__).resolve().parents[1]
STAGE_D = REPO / "scripts" / "release" / "stage-d"
STAGE_C = REPO / "scripts" / "release" / "stage-c"

RELEASE_SHA = "84cd8696119c24662a954d0f0e23195268dab23f"
# The commit this checkout is actually at. verify_caps.py REFUSES unless
# backend/runtime_policy.py here is byte-for-byte the file at
# STAGE_D_RELEASE_SHA; HEAD is simply the most convenient commit that
# satisfies that, since the working tree is committed in CI. It is NOT a
# requirement that the checkout BE the release --
# `test_a_later_authorization_commit_may_reference_an_earlier_release` proves
# the opposite, which is what makes re-authorization possible at all.
CHECKOUT_SHA = subprocess.run(
    ["git", "-C", str(Path(__file__).resolve().parents[1]), "rev-parse", "HEAD"],
    capture_output=True, text=True, timeout=60).stdout.strip()
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

# The operating envelope, READ from the ONE canonical runtime policy exactly
# as `stage-d-env.sh` now generates it. This file used to carry its own
# transcription of all three groups, which made the whole suite a proof that
# one transcription matched another: it passed while the toolkit pinned
# MILO_PROVIDER_RPM_LIMIT=350 against MILO's organization ceiling of 80 --
# a posture `ProviderLimitsConfig.from_env` refuses, so no Worker could have
# started under it.
POLICY = reviewed_first_run_policy()


def _rendered(prefixes) -> str:
    return ",".join(f"{k}={v}" for k, v in POLICY.env_expectations(prefixes=prefixes).items())


CAPS = _rendered(CAP_ENV_PREFIXES)
PROVIDER_LIMITS = _rendered(PROVIDER_ENV_PREFIXES)
ENGINE_LIMITS = _rendered(ENGINE_ENV_PREFIXES)
POLICY_FINGERPRINT = POLICY.fingerprint()

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


def git_sha(rev: str) -> str:
    """Resolve a revision in THIS repository to a full 40-character SHA."""
    return subprocess.run(["git", "-C", str(REPO), "rev-parse", rev],
                          capture_output=True, text=True, timeout=60).stdout.strip()


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

EXTRA_DUMPED = ["STAGE_D_CAPS", "STAGE_D_WORKER_ENGINE_LIMITS",
                "STAGE_D_POLICY_FINGERPRINT", "STAGE_D_AUTHORIZED_EXECUTION_INCREMENT",
                "STAGE_D_REGISTRY", "STAGE_D_API_URL",
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


def test_env_pins_every_provider_limit_the_runtime_actually_reads():
    """Pinned by DERIVATION: every provider dimension the policy declares.

    The old version of this test listed the seven names and their values by
    hand, which is how it certified an RPM of 350 that the runtime refuses.
    """
    result = source_stage_d_env()
    assert result.returncode == 0, result.stderr
    line = next(r for r in result.stdout.splitlines() if r.startswith("STAGE_D_WORKER_PROVIDER_LIMITS="))
    pinned = parse_pairs(line.split("=", 1)[1])
    assert pinned == POLICY.env_expectations(prefixes=PROVIDER_ENV_PREFIXES)
    assert set(pinned) == {DIMENSIONS[d].env_key for d in DIMENSIONS
                           if DIMENSIONS[d].enforced_by == PROVIDER}
    # Restores the Attempt 7 concurrency; production currently carries 8.
    assert pinned["MILO_PROVIDER_MAX_CONCURRENCY"] == "2"
    # And the pinned envelope is one the runtime will actually accept.
    ProviderLimitsConfig.from_env(dict(pinned))
    # Provider scheduling must NOT ride along in STAGE_D_CAPS (caps are
    # applied and verified on BOTH surfaces; the envelope is worker-only).
    caps_line = next(r for r in result.stdout.splitlines() if r.startswith("STAGE_D_CAPS="))
    assert "MILO_PROVIDER_" not in caps_line


def test_env_pins_the_engine_parallelism_and_the_policy_fingerprint():
    """Newly pinned, because both were reachable ways to widen a paid run.

    MILO_SWARM_MAX_ACTIVE_WORKERS was never pinned by this toolkit and its
    code default of 4 is WIDER than the reviewed width of 2.
    """
    result = source_stage_d_env()
    assert result.returncode == 0, result.stderr
    line = next(r for r in result.stdout.splitlines()
                if r.startswith("STAGE_D_WORKER_ENGINE_LIMITS="))
    assert parse_pairs(line.split("=", 1)[1]) == POLICY.env_expectations(
        prefixes=ENGINE_ENV_PREFIXES)
    assert f"STAGE_D_POLICY_FINGERPRINT={POLICY_FINGERPRINT}" in result.stdout


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


def test_env_caps_are_the_canonical_runtime_policy_value_for_value():
    result = source_stage_d_env()
    line = next(r for r in result.stdout.splitlines() if r.startswith("STAGE_D_CAPS="))
    assert parse_pairs(line.split("=", 1)[1]) == POLICY.env_expectations(
        prefixes=CAP_ENV_PREFIXES)


@pytest.mark.parametrize("name,stage_c_value", sorted(STAGE_C_CAPS.items()))
def test_no_stage_d_cap_exceeds_its_stage_c_counterpart(name, stage_c_value):
    """The core promise: Stage D raises no Stage C limit."""
    assert stage_d_caps()[name] <= stage_c_value, f"{name} was RAISED above the Stage C value"


def test_every_cap_stage_c_pinned_is_still_pinned_by_stage_d():
    """No cap may be silently dropped — an absent cap is an unbounded one."""
    assert set(STAGE_C_CAPS) <= set(stage_d_caps())


def test_stage_d_additionally_pins_the_plan_shape_stage_c_never_did():
    """The three dimensions the reviewed profile advertised and nothing pinned.

    Stage C bounded model calls, tokens, cost and duration. It bounded
    nothing about plan SHAPE, so the Swarm V2 firewall ran on its own
    defaults -- 64 tasks, 3 replans, 100 tool calls -- inside a profile that
    advertised 23, 1 and 24.
    """
    added = set(stage_d_caps()) - set(STAGE_C_CAPS)
    assert added == {"MILO_MAX_TASKS_PER_RUN", "MILO_MAX_TOOL_CALLS_PER_RUN",
                     "MILO_MAX_REPLANS_PER_RUN"}


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


def test_the_policy_documents_the_reason_for_every_non_tightened_cap():
    """The reasons live with the numbers, which now live in ONE place.

    They used to be a comment block in stage-d-env.sh beside a second copy of
    the envelope. Moving the numbers into the canonical policy without moving
    their justification would have left the reviewed values unexplained.
    """
    text = (REPO / "backend" / "runtime_policy.py").read_text()
    assert "RETRY_LIMIT_REACHED" in text
    assert "3.5x rather than 1.75x" in text
    for observed in ("84", "277,882", "34,136", "312,018", "0.252069", "934.235"):
        assert observed in text, f"the Attempt 7 evidence value {observed} is not cited"
    env_text = (STAGE_D / "stage-d-env.sh").read_text()
    assert "backend/runtime_policy.py" in env_text, (
        "stage-d-env.sh no longer points at the authority its envelope comes from")


# ---------------------------------------------------------------------------
# C. verify_caps.py — wrong image, catalog flag, provider key on API, drift
# ---------------------------------------------------------------------------

def env_entries(pairs: str) -> list[dict]:
    return [{"name": k, "value": v} for k, v in parse_pairs(pairs).items()]


def worker_spec(*, caps=CAPS, provider=PROVIDER_LIMITS, engine=ENGINE_LIMITS,
                image=None, bind_key=True, extra=None, release_sha=None):
    env = env_entries(caps) + env_entries(provider) + env_entries(engine) + [
        {"name": "MILO_ENABLE_PAID_EXECUTION", "value": "true"},
        {"name": "MILO_ENABLE_CATALOG_EXECUTION", "value": "false"},
        # The release this deployment STATES it is serving. It is what
        # `backend/run_identity.py` binds onto every run the deployment
        # creates, so an absent value would make every run record no release
        # and the evidence gate would refuse the run as unbindable.
        {"name": "MILO_RELEASE_SHA", "value": release_sha or CHECKOUT_SHA},
    ]
    if bind_key:
        env.append({"name": "KIMI_API_KEY", "valueFrom": {"secretKeyRef": {"key": "latest", "name": "KIMI_API_KEY"}}})
    env.extend(extra or [])
    return {"spec": {"template": {"spec": {"template": {"spec": {"containers": [
        {"image": image or f"{REGISTRY}/worker:{release_sha or CHECKOUT_SHA}", "env": env}
    ]}}}}}}


def api_spec(*, caps=CAPS, image=None, extra=None, release_sha=None):
    env = env_entries(caps) + [
        {"name": "MILO_ENABLE_PAID_EXECUTION", "value": "false"},
        {"name": "MILO_ENABLE_RUN_CREATION", "value": "true"},
        {"name": "JOB_LAUNCHER", "value": "cloud_run"},
        {"name": "MILO_ENABLE_PROPOSAL_MUTATIONS", "value": "false"},
        {"name": "MILO_ENABLE_PROPOSAL_READS", "value": "false"},
        {"name": "MILO_ENABLE_RUN_CANCELLATION", "value": "false"},
        {"name": "MILO_ENABLE_EXECUTION_CONTROL", "value": "false"},
        {"name": "MILO_ENABLE_CATALOG_EXECUTION", "value": "false"},
        {"name": "MILO_RELEASE_SHA", "value": release_sha or CHECKOUT_SHA},
    ]
    env.extend(extra or [])
    return {"spec": {"template": {"spec": {"containers": [
        {"image": image or f"{REGISTRY}/api:{release_sha or CHECKOUT_SHA}", "env": env}
    ]}}}}


_UNSET = object()


def run_verify_caps(tmp_path, worker, api, caps=CAPS, provider_limits=PROVIDER_LIMITS,
                    engine_limits=ENGINE_LIMITS, fingerprint=None, release_sha=_UNSET):
    worker_path = tmp_path / "worker.json"
    api_path = tmp_path / "api.json"
    worker_path.write_text(json.dumps(worker))
    api_path.write_text(json.dumps(api))
    return subprocess.run(
        [sys.executable, str(STAGE_D / "verify_caps.py"),
         "--worker-json", str(worker_path), "--api-json", str(api_path)],
        capture_output=True, text=True,
        env={**os.environ, "STAGE_D_CAPS": caps, "STAGE_D_WORKER_PROVIDER_LIMITS": provider_limits,
             "STAGE_D_WORKER_ENGINE_LIMITS": engine_limits,
             "STAGE_D_POLICY_FINGERPRINT": fingerprint or POLICY_FINGERPRINT,
             "STAGE_D_REGISTRY": REGISTRY,
             "STAGE_D_RELEASE_SHA": (CHECKOUT_SHA if release_sha is _UNSET
                                     else release_sha),
             "STAGE_D_API_IMAGE_DIGEST": API_DIGEST, "STAGE_D_WORKER_IMAGE_DIGEST": WORKER_DIGEST},
        timeout=60,
    )


def test_verify_caps_refuses_a_deployment_that_states_no_release(tmp_path):
    """The run identity is the last link of the release chain, and it is bound
    from `MILO_RELEASE_SHA`. A deployment that states none would create runs
    recording no release, and an unpinned run cannot be bound to the accepted
    release -- so it is refused BEFORE the run is created rather than after it
    has been paid for."""
    for surface, worker, api in (
        ("worker", worker_spec(extra=[{"name": "MILO_RELEASE_SHA", "value": ""}]), api_spec()),
        ("api", worker_spec(), api_spec(extra=[{"name": "MILO_RELEASE_SHA", "value": ""}])),
    ):
        result = run_verify_caps(tmp_path, worker, api)
        assert result.returncode == 1, surface
        assert f"{surface}: MILO_RELEASE_SHA is MISSING" in result.stdout, surface


def test_verify_caps_refuses_a_deployment_pinned_to_another_release(tmp_path):
    other = "b" * 40
    result = run_verify_caps(
        tmp_path, worker_spec(extra=[{"name": "MILO_RELEASE_SHA", "value": other}]), api_spec())
    assert result.returncode == 1
    assert "is not the accepted release" in result.stdout


def test_verify_caps_passes_on_the_exact_authorized_posture(tmp_path):
    result = run_verify_caps(tmp_path, worker_spec(), api_spec())
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"release {CHECKOUT_SHA[:12]}" in result.stdout
    assert "bound to the accepted release" in result.stdout


@pytest.mark.parametrize("surface,bad", [
    ("worker", {"image": f"{REGISTRY}/worker:88224bccc836f80f3dc1d173306a1aa63cddcc7a"}),
    ("worker", {"image": f"{REGISTRY}/worker:latest"}),
    ("worker", {"image": f"{REGISTRY}/worker@{STALE_WORKER_DIGEST}"}),
    ("api", {"image": f"{REGISTRY}/api:88224bccc836f80f3dc1d173306a1aa63cddcc7a"}),
    ("api", {"image": "us-central1-docker.pkg.dev/attacker/evil/api:" + CHECKOUT_SHA}),
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
             "STAGE_D_WORKER_ENGINE_LIMITS": ENGINE_LIMITS,
             "STAGE_D_POLICY_FINGERPRINT": POLICY_FINGERPRINT,
             "STAGE_D_REGISTRY": REGISTRY, "STAGE_D_RELEASE_SHA": CHECKOUT_SHA,
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


# --- the release binding: the policy Stage D verifies must BE the release ---

def test_verify_caps_refuses_a_release_whose_policy_is_not_this_one(tmp_path):
    """CHECKOUT-POLICY DRIFT — the whole reason the binding exists.

    Generating the envelope from the local checkout and verifying against the
    same local checkout only ever proves the checkout agrees with itself. The
    run executes separately pinned release IMAGES, which may carry a different
    policy entirely, so Stage D refuses unless the policy here is byte-for-byte
    the policy at the accepted release.

    Run end to end against a purpose-built repository rather than a commit of
    this one: CI checks out at depth 1, so a test that names a real historical
    SHA asserts a message the environment cannot produce and proves nothing
    about the case it claims to cover.
    """
    root, release_sha = build_release_toolkit_repo(tmp_path, change_policy_after=True)
    result = run_verify_caps_in(root, tmp_path, release_sha)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "is not byte-for-byte the policy at the accepted release" in result.stdout


def test_verify_caps_refuses_a_release_that_predates_the_policy(tmp_path):
    """The currently pinned Stage D release is exactly this case."""
    root, release_sha = build_release_toolkit_repo(tmp_path, policy_at_release=False)
    result = run_verify_caps_in(root, tmp_path, release_sha)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "does not contain backend/runtime_policy.py" in result.stdout


def test_verify_caps_accepts_a_later_authorization_commit_end_to_end(tmp_path):
    """The re-authorization property, proven through verify_caps itself."""
    root, release_sha = build_release_toolkit_repo(tmp_path)
    head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                          capture_output=True, text=True, timeout=60).stdout.strip()
    assert head != release_sha
    result = run_verify_caps_in(root, tmp_path, release_sha)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "bound to the accepted release" in result.stdout


@pytest.mark.parametrize("bad_sha", ["", "not-a-sha", "84cd8696", "z" * 40])
def test_verify_caps_refuses_an_unprovable_release_sha(tmp_path, bad_sha):
    """Cannot PROVE the binding is a refusal, never a pass."""
    result = run_verify_caps(tmp_path, worker_spec(), api_spec(), release_sha=bad_sha)
    assert result.returncode != 0
    assert "not a full 40-character commit SHA" in result.stdout


def test_verify_caps_refuses_a_release_commit_this_checkout_cannot_read(tmp_path):
    result = run_verify_caps(tmp_path, worker_spec(), api_spec(), release_sha="f" * 40)
    assert result.returncode != 0
    assert "is not a commit this checkout can read" in result.stdout


def test_the_pinned_policy_fingerprint_is_the_checkouts_policy():
    """The literal reviewed pin, kept honest by CI rather than by a run.

    Editing a reviewed value without re-pinning would otherwise be discovered
    by a production gate. It is discovered here instead.
    """
    sys.path.insert(0, str(STAGE_D))
    import policy_envelope

    assert policy_envelope.PINNED_POLICY_FINGERPRINT == POLICY.fingerprint()
    assert policy_envelope.fingerprint_problems() == []


def test_a_drifted_checkout_policy_cannot_even_print_an_envelope(monkeypatch):
    """Every selector refuses, so a drifted checkout produces no pins at all."""
    sys.path.insert(0, str(STAGE_D))
    import policy_envelope

    monkeypatch.setattr(policy_envelope, "PINNED_POLICY_FINGERPRINT", "0" * 64)
    assert policy_envelope.fingerprint_problems()
    for selector in ("caps", "provider-limits", "engine-limits", "fingerprint",
                     "execution-increment", "document", "binding"):
        assert policy_envelope.main(["policy_envelope.py", selector]) != 0, selector


def test_the_binding_refuses_when_it_cannot_read_the_checkout(monkeypatch):
    """Cannot prove is a refusal, never a pass: no git, no Stage D."""
    sys.path.insert(0, str(STAGE_D))
    import policy_envelope

    monkeypatch.setattr(policy_envelope, "_git", lambda *a, **k: None)
    problems = policy_envelope.release_binding_problems(CHECKOUT_SHA)
    assert any("is not a commit this checkout can read" in problem
               for problem in problems)


def test_the_binding_refuses_a_policy_imported_from_outside_the_checkout(monkeypatch):
    """The bytes compared must be the bytes in use."""
    sys.path.insert(0, str(STAGE_D))
    import policy_envelope

    monkeypatch.setattr(policy_envelope, "_imported_policy_source",
                        lambda: Path("/somewhere/else/runtime_policy.py"))
    problems = policy_envelope.release_binding_problems(CHECKOUT_SHA)
    assert any("imported from outside this checkout" in problem for problem in problems)


def test_the_binding_accepts_this_checkout_against_its_own_head():
    """A committed checkout verifying against its own HEAD is one accept path.

    This fails on a working tree with uncommitted changes to
    `backend/runtime_policy.py`, and that is the point: the envelope may only
    be generated from a policy that is identical to a released one. CI always
    runs against a clean checkout.
    """
    sys.path.insert(0, str(STAGE_D))
    import policy_envelope

    assert policy_envelope.release_binding_problems(CHECKOUT_SHA) == []


# --- re-authorization: a later commit may reference an earlier release ------

def build_release_repo(tmp_path, *, change_policy_after=False):
    """A miniature repo with release R, then a LATER authorization commit.

    Deterministic and independent of this repository's own history: R carries
    the real policy source, and the commit after it edits a runbook — exactly
    the shape of a reviewed authorization commit that pins R.
    """
    root = tmp_path / "release-repo"
    (root / "backend").mkdir(parents=True)
    (root / "scripts" / "release" / "stage-d").mkdir(parents=True)

    def run(*args):
        subprocess.run(("git", "-C", str(root), *args), check=True,
                       capture_output=True, timeout=60)

    subprocess.run(["git", "init", "-q", str(root)], check=True, timeout=60)
    run("config", "user.email", "release@invalid")
    run("config", "user.name", "Release")
    policy = root / "backend" / "runtime_policy.py"
    policy.write_bytes((REPO / "backend" / "runtime_policy.py").read_bytes())
    (root / "scripts" / "release" / "stage-d" / "03-enable-stage-d.md").write_text("# v1\n")
    run("add", "-A")
    run("commit", "-qm", "release R")
    release_sha = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                                 capture_output=True, text=True, timeout=60).stdout.strip()

    # The later, reviewed authorization commit. It references R; it is not R.
    (root / "scripts" / "release" / "stage-d" / "03-enable-stage-d.md").write_text(
        "# v2 — pins release R\n")
    if change_policy_after:
        policy.write_bytes(policy.read_bytes() + b"\n# an authorization commit changed the policy\n")
    run("add", "-A")
    run("commit", "-qm", "authorize release R")
    head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                          capture_output=True, text=True, timeout=60).stdout.strip()
    assert head != release_sha
    return root, release_sha


def build_release_toolkit_repo(tmp_path, *, policy_at_release=True,
                               change_policy_after=False):
    """A miniature repo carrying BOTH the policy and the Stage D verifiers.

    `verify_caps.py` resolves its repository root from its own location, so a
    copy of the toolkit inside this repo imports THIS repo's policy and binds
    against THIS repo's history. That makes the end-to-end refusal paths
    testable without depending on how deeply the CI runner cloned us.
    """
    root = tmp_path / "release-toolkit"
    toolkit = root / "scripts" / "release" / "stage-d"
    (root / "backend").mkdir(parents=True)
    toolkit.mkdir(parents=True)

    def run(*args):
        subprocess.run(("git", "-C", str(root), *args), check=True,
                       capture_output=True, timeout=60)

    subprocess.run(["git", "init", "-q", str(root)], check=True, timeout=60)
    run("config", "user.email", "release@invalid")
    run("config", "user.name", "Release")
    for name in ("policy_envelope.py", "verify_caps.py"):
        (toolkit / name).write_bytes((STAGE_D / name).read_bytes())
    (root / "backend" / "__init__.py").write_bytes(
        (REPO / "backend" / "__init__.py").read_bytes())
    policy = root / "backend" / "runtime_policy.py"
    released_policy = (REPO / "backend" / "runtime_policy.py").read_bytes()
    if policy_at_release:
        policy.write_bytes(released_policy)
    (toolkit / "03-enable-stage-d.md").write_text("# v1\n")
    run("add", "-A")
    run("commit", "-qm", "release R")
    release_sha = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                                 capture_output=True, text=True, timeout=60).stdout.strip()

    # The later, reviewed authorization commit. It references R; it is not R.
    (toolkit / "03-enable-stage-d.md").write_text("# v2 — pins release R\n")
    if not policy_at_release:
        # The policy only appears AFTER the release, so R cannot carry it.
        policy.write_bytes(released_policy)
    if change_policy_after:
        # A comment-only change: the DOCUMENT is identical, so the fingerprint
        # pin still matches and only the byte comparison can refuse. That
        # isolates the message this test is about.
        policy.write_bytes(released_policy + b"\n# an authorization commit touched the policy\n")
    run("add", "-A")
    run("commit", "-qm", "authorize release R")
    return root, release_sha


def run_verify_caps_in(root, tmp_path, release_sha, *, caps=CAPS,
                       provider_limits=PROVIDER_LIMITS, engine_limits=ENGINE_LIMITS):
    """Run the COPY of verify_caps.py that lives inside `root`."""
    worker_path = tmp_path / "sandbox-worker.json"
    api_path = tmp_path / "sandbox-api.json"
    worker_path.write_text(json.dumps(worker_spec(release_sha=release_sha)))
    api_path.write_text(json.dumps(api_spec(release_sha=release_sha)))
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.update({
        "STAGE_D_CAPS": caps, "STAGE_D_WORKER_PROVIDER_LIMITS": provider_limits,
        "STAGE_D_WORKER_ENGINE_LIMITS": engine_limits,
        "STAGE_D_POLICY_FINGERPRINT": POLICY_FINGERPRINT,
        "STAGE_D_REGISTRY": REGISTRY, "STAGE_D_RELEASE_SHA": release_sha,
        "STAGE_D_API_IMAGE_DIGEST": API_DIGEST,
        "STAGE_D_WORKER_IMAGE_DIGEST": WORKER_DIGEST,
    })
    return subprocess.run(
        [sys.executable, str(root / "scripts" / "release" / "stage-d" / "verify_caps.py"),
         "--worker-json", str(worker_path), "--api-json", str(api_path)],
        capture_output=True, text=True, env=env, cwd=str(root), timeout=120)


def test_a_later_authorization_commit_may_reference_an_earlier_release(tmp_path):
    """THE re-authorization property.

    Requiring HEAD == STAGE_D_RELEASE_SHA made the binding self-referential:
    the reviewed commit that updates the pin to release R cannot itself be R,
    so no authorization commit could ever satisfy its own pin. What has to
    hold is that the POLICY is the released one, and it does here.
    """
    sys.path.insert(0, str(STAGE_D))
    import policy_envelope

    root, release_sha = build_release_repo(tmp_path)
    assert policy_envelope.release_binding_problems(release_sha, repo_root=root) == []


def test_an_authorization_commit_that_changes_the_policy_is_refused(tmp_path):
    """Changing the policy needs a new release, not a new authorization."""
    sys.path.insert(0, str(STAGE_D))
    import policy_envelope

    root, release_sha = build_release_repo(tmp_path, change_policy_after=True)
    problems = policy_envelope.release_binding_problems(release_sha, repo_root=root)
    assert any("is not byte-for-byte the policy at the accepted release" in problem
               for problem in problems)
    assert any("requires a new reviewed release" in problem for problem in problems)


def test_an_earlier_real_commit_with_the_same_policy_is_accepted():
    """The same property over this repository's OWN history.

    The sandbox test proves the rule; this proves it holds for a real
    ancestor, which is what an authorization commit pinning the previous
    release actually looks like. Skipped only when the policy was changed in
    HEAD itself, in which case no ancestor can carry identical bytes.
    """
    sys.path.insert(0, str(STAGE_D))
    import policy_envelope

    current = (REPO / "backend" / "runtime_policy.py").read_bytes()
    ancestors = subprocess.run(
        ["git", "-C", str(REPO), "rev-list", "--max-count=25", "HEAD~1"],
        capture_output=True, text=True, timeout=60).stdout.split()
    match = next(
        (sha for sha in ancestors
         if subprocess.run(["git", "-C", str(REPO), "show", f"{sha}:backend/runtime_policy.py"],
                           capture_output=True, timeout=60).stdout == current),
        None)
    if match is None:
        pytest.skip(
            "no readable ancestor carries this policy — either HEAD changed it, "
            "or this is a shallow clone (CI checks out at depth 1). The same "
            "property is proven without history by "
            "test_a_later_authorization_commit_may_reference_an_earlier_release")
    assert match != CHECKOUT_SHA
    assert policy_envelope.release_binding_problems(match) == []


def test_the_binding_never_asks_which_commit_is_checked_out():
    """Stated over the parsed source, so prose cannot pass or fail it."""
    import inspect

    sys.path.insert(0, str(STAGE_D))
    import policy_envelope

    source = inspect.getsource(policy_envelope.release_binding_problems)
    assert "rev-parse" not in source or "HEAD" not in source
    assert "HEAD" not in source, "the binding is back to comparing checkout HEAD"


def test_the_step_scripts_gate_on_the_binding_before_creating_a_run():
    for name in ("03b-verify-stage-d-posture.sh", "05-execute-run.sh"):
        text = (STAGE_D / name).read_text()
        assert "python3 ./policy_envelope.py binding" in text, name
        assert (text.index("python3 ./policy_envelope.py binding")
                < text.index("python3 ./verify_caps.py")), name


def test_the_enable_runbooks_gate_on_the_binding_before_applying_an_envelope():
    """Applying the caps MUTATES the deployment, so it is bound too.

    03b and 05 would refuse the run afterwards, but by then a drifted envelope
    would already be on the job.
    """
    for name in ("03-enable-stage-d.md", "02-guarded-run.md"):
        text = (STAGE_D / name).read_text()
        assert "policy_envelope.py binding" in text, name
        assert text.index("policy_envelope.py binding") < text.index(
            "--update-env-vars"), name


# --- the execution increment has ONE authority -----------------------------

def test_the_authorized_execution_increment_comes_from_the_runtime_policy():
    result = source_stage_d_env()
    assert result.returncode == 0, result.stderr
    line = next(r for r in result.stdout.splitlines()
                if r.startswith("STAGE_D_AUTHORIZED_EXECUTION_INCREMENT="))
    assert line.split("=", 1)[1] == str(int(POLICY["first_paid_run_execution_cap"]))


def test_the_execution_gate_refuses_an_increment_the_policy_did_not_authorize():
    """A widened shell expression is caught by the verifier, not accepted."""
    listing = [terminal(n) for n in LIVE_EXECUTION_NAMES]
    listing += [terminal("milo-agent-worker-a"), terminal("milo-agent-worker-b")]
    result = run_verify_executions(listing, 9, baseline=7)
    assert result.returncode != 0
    verdict = json.loads(result.stdout)
    assert verdict["implied_increment"] == 2 and verdict["authorized_increment"] == 1


def test_the_execution_gate_accepts_exactly_the_authorized_increment():
    listing = [terminal(n) for n in LIVE_EXECUTION_NAMES] + [terminal("milo-agent-worker-staged1")]
    result = run_verify_executions(listing, 8, baseline=7)
    assert result.returncode == 0, result.stdout
    assert json.loads(result.stdout)["authorized_increment"] == 1


def test_verify_caps_tolerates_unrelated_live_worker_variables(tmp_path):
    """Model selection is not part of the envelope, so it is not verified."""
    extra = [
        {"name": "MILO_COMMANDER_MODEL", "value": "kimi-k2.6"},
        {"name": "MILO_MODEL_BASE_URL", "value": "https://api.moonshot.ai/v1"},
    ]
    assert run_verify_caps(tmp_path, worker_spec(extra=extra), api_spec()).returncode == 0


def test_verify_caps_refuses_the_swarm_width_that_used_to_be_unverified(tmp_path):
    """MILO_SWARM_MAX_ACTIVE_WORKERS=8 was tolerated as "unrelated".

    It is not unrelated: it is the Swarm V2 queueing width, the canonical
    policy reviews it at 2, and a paid Worker carrying 8 would have run at
    four times the reviewed width with every Stage D check passing.
    """
    extra = [{"name": "MILO_SWARM_MAX_ACTIVE_WORKERS", "value": "8"}]
    result = run_verify_caps(tmp_path, worker_spec(extra=extra), api_spec())
    assert result.returncode != 0
    assert "MILO_SWARM_MAX_ACTIVE_WORKERS" in result.stdout


def test_verify_caps_refuses_an_engine_variable_on_the_api(tmp_path):
    """Engine parallelism belongs to the Worker alone, like provider limits."""
    api = api_spec()
    api["spec"]["template"]["spec"]["containers"][0]["env"].append(
        {"name": "MILO_SWARM_MAX_ACTIVE_WORKERS", "value": "2"})
    result = run_verify_caps(tmp_path, worker_spec(), api)
    assert result.returncode != 0
    assert "must NEVER be set on the API service" in result.stdout


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


def run_verify_executions(listing, expected_total, baseline=None):
    payload = listing if isinstance(listing, str) else json.dumps(listing)
    command = [sys.executable, str(STAGE_D / "verify_executions.py"),
               "--expected-total", str(expected_total)]
    if baseline is not None:
        command += ["--baseline", str(baseline)]
    return subprocess.run(command, input=payload, capture_output=True, text=True, timeout=60)


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
            # A DEPLOYED signature advertises its defaulted parameters too,
            # so the exact-checked RPC advertises its exact pin.
            paths = {
                f"/rpc/{rpc}": {"post": {"parameters": [
                    {"in": "body", "schema": {"properties": {
                        a: {} for a in (db.EXACT_RPC_SIGNATURES.get(rpc) or args)}}}
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
            "MOCK_PROBE_DIR": str(self.dir),
            "STAGE_D_WORKDIR": str(self.workdir),
            **extra,
        }

    def read_state(self):
        return json.loads(self.state_path.read_text())

    def set_state(self, **kwargs):
        state = self.read_state()
        state.update(kwargs)
        self.state_path.write_text(json.dumps(state))

    # -- a REAL database behind the db probe -----------------------------
    def seed_db(self, *, runs, reservations=(), conversations=None):
        """Put real rows behind the terminalize probe. The mock then runs the
        REAL probe_db.py against the fake PostgREST, with exactly the env the
        shell passed, instead of reporting a canned verdict."""
        self.set_state(db={
            "runs": list(runs), "reservations": list(reservations),
            "conversations": (conversations if conversations is not None
                              else [{"id": RUN_CONVERSATION, "project_id": RUN_PROJECT}]),
        })

    def db(self):
        return self.read_state()["db"]

    def db_probe_runs(self):
        """Every real terminalize execution: exit, verdict, env, mutations."""
        return self.read_state().get("db_probe_runs", [])

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


def recorded_state(**overrides):
    """What state.json holds once 05-execute-run.sh has created the run: the
    identity is written BEFORE the run, the run id after. A None value
    removes the key."""
    state = {"stage_d_workdir": "<workdir>", "idempotency_key": STAGE_D_KEY,
             "user_id": RUN_USER, "conversation_id": RUN_CONVERSATION, "run_id": STAGE_D_RUN_ID}
    state.update(overrides)
    return {k: v for k, v in state.items() if v is not None}


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
    world = StageDWorld(tmp_path, state={"probe_verdicts": {"govcheck": False, "terminalize": True}})
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
    """An execution that cannot even run proves nothing."""
    world = StageDWorld(tmp_path, state={"govcheck_available": False})
    world.enable_execution_surface()
    result = world.run("07-post-run-lockdown.sh")
    assert result.returncode != 0
    assert "STAGE D LOCKDOWN COMPLETE" not in result.stdout
    assert "LOCKDOWN CRITICAL" in result.stderr
    assert "Government capture posture check" in result.stderr
    # The probes are still removed even though the checks could not run.
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

    `policy_envelope.py binding` is stubbed for the same reason as 01: it
    proves the policy here is the released one, and this sandbox is a partial
    copy of the toolkit with no `backend/` tree and no git history, so it can
    prove nothing. That the block CONTAINS that gate before it mutates anything is
    asserted directly by
    `test_the_enable_runbooks_gate_on_the_binding_before_applying_an_envelope`,
    and the gate's own accept/refuse behaviour by the binding tests above.
    """
    world = StageDWorld(tmp_path)
    stubs = {
        "01-verify-release-images.sh": STUB_OK,
        "03b-verify-stage-d-posture.sh": STUB_OK,
        "04-create-probes.sh": STUB_CREATE_BOTH,
        "05-execute-run.sh": STUB_OK,
        "06-collect-evidence.sh": STUB_OK,
    }
    (world.dir / "policy_envelope.py").write_text(
        "#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n")
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

    # The probe image is the pinned DIGEST of the reviewed probe runtime,
    # never the release registry and never a tag.
    probes = (STAGE_D / "04-create-probes.sh").read_text()
    assert '--image="${PROBE_IMAGE}"' in probes
    # The mutable tag may be DISCUSSED in comments but never used in code.
    probe_code = [line for line in probes.splitlines()
                  if line.strip() and not line.lstrip().startswith("#")]
    assert not [line for line in probe_code if "python:3.12-slim" in line]
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
    # The increment is the canonical policy's, not a literal in the shell.
    assert ("expected_total_executions=$((STAGE_D_EXPECTED_PRIOR_EXECUTIONS "
            "+ STAGE_D_AUTHORIZED_EXECUTION_INCREMENT))") in text
    assert "STAGE_D_EXPECTED_PRIOR_EXECUTIONS + 1" not in text, (
        "a literal execution increment is back in the shell")
    assert '--baseline "${STAGE_D_EXPECTED_PRIOR_EXECUTIONS}"' in text
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
               project_id=STAGE_D_PROJECT_ID, conversations=_DEFAULT,
               user_exists=True, project_exists=True, record=None):
    """Route probe_db.setup()'s HTTP layer at a deterministic fake."""
    if configuration is _DEFAULT:
        configuration = {"stage": "stage-d"}
    if members is _DEFAULT:
        members = [{"user_id": STAGE_D_USER_ID, "role": "owner"}]
    if conversations is _DEFAULT:
        conversations = [{"id": STAGE_D_CONVERSATION}]

    def fake_call(method, path, body=None, headers=None):
        if record is not None:
            record.append((method, path))
        if path.startswith("/auth/v1/admin/users") and method == "GET":
            return 200, {"users": [{"id": STAGE_D_USER_ID, "email": db.TEST_EMAIL}] if user_exists else []}
        if path.startswith("/auth/v1/admin/users") and method == "POST":
            return 201, {"id": STAGE_D_USER_ID}
        if path.startswith("/rest/v1/projects") and method == "GET":
            if not project_exists:
                return 200, []
            return 200, [{"id": project_id, "workflow_key": workflow_key,
                          "configuration": configuration}]
        if path.startswith("/rest/v1/projects") and method == "POST":
            return 201, [{"id": project_id, "workflow_key": workflow_key,
                          "configuration": configuration}]
        if path.startswith("/rest/v1/project_members") and method == "POST":
            return 201, None
        if path.startswith("/rest/v1/project_members") and method == "GET":
            return 200, members
        if path.startswith("/rest/v1/conversations") and method == "GET":
            return 200, conversations
        if path.startswith("/rest/v1/conversations") and method == "POST":
            return 201, [{"id": STAGE_D_CONVERSATION}]
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
    assert verdict["members"] == [f"{STAGE_D_USER_ID}:owner"]
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
# N. The web-search cost statement matches the runtime, in BOTH directions
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


def test_the_cost_statement_matches_the_verified_standalone_price():
    """Search price is now a reviewed runtime input, not an unknown interface.

    MILO books the higher of the two current international standalone-search
    prices before execution, so both Basic and Pro are inside the recorded
    $1.00 run ceiling even when a failed/empty search is conservatively
    over-counted.
    """
    doc = AUTHORIZATION_DOC.read_text()
    assert "Recorded total cost" in doc
    assert "≤ $1.00, hard" in doc
    assert "Search invocations" in doc
    assert "≤ 60 per run, hard" in doc
    assert "max_search_invocations_per_run" in doc
    assert "$0.002" in doc
    assert "$0.003" in doc
    assert "≤ $0.18 recorded" in doc


def test_the_superseded_unbounded_claim_is_restated_not_deleted():
    """A warning that stops being true is restated, never silently dropped.

    An operator who read the old exposure statement must be able to see what
    replaced it and why, rather than finding the paragraph simply gone.
    """
    doc = AUTHORIZATION_DOC.read_text()
    lowered = doc.lower()
    assert "not capped by milo at all" in lowered
    index = lowered.index("not capped by milo at all")
    context = lowered[max(0, index - 800):index + 800]
    assert "superseded" in context
    assert "provider decided how many" in context


def test_the_cost_statement_cites_current_standalone_and_legacy_fees():
    doc = AUTHORIZATION_DOC.read_text()
    readme = (STAGE_D / "README.md").read_text()
    for text in (doc, readme):
        assert "0.002" in text and "0.003" in text
        assert "0.005" in text and "2026-10-20" in text


def test_the_cost_statement_names_the_real_runtime_behaviour():
    """It must name the mechanism, not just assert a number.

    "60" on its own is a figure an operator has to take on trust; naming the
    dimension and the admission path is what makes it checkable.
    """
    doc = AUTHORIZATION_DOC.read_text()
    assert "ProviderAdapter.run_search" in doc
    assert "before it runs" in doc
    assert "backend/standalone_search.py" in doc


def test_the_runtime_claim_in_the_doc_matches_the_actual_runtime():
    """The doc and the runtime must agree about whether a cap exists.

    This is the inverse of what this guard used to assert, for exactly the
    same reason: a document describing an exposure the runtime no longer has
    misleads an operator just as badly as one hiding an exposure it does.
    """
    core = (REPO / "backend/engines/vehicle_catalog_v1/core.py").read_text()
    policy = (REPO / "backend/runtime_policy.py").read_text()
    authority = (REPO / "backend/provider_authority.py").read_text()
    doc = AUTHORIZATION_DOC.read_text()

    # The preserved per-call round bound is still there...
    assert "MAX_TOOL_ROUNDS = 15" in core
    # ...and the second factor, which used to be the provider's alone, is now
    # bounded too: V1 hands the provider no search capability at all.
    assert '"builtin_function"' not in core
    # Every invocation goes through the one admission path...
    assert "def run_search(" in authority
    assert "max_search_invocations_per_run" in policy
    # ...and the doc says so, with the number the policy really publishes.
    from backend.runtime_policy import reviewed_first_run_policy

    policy_values = reviewed_first_run_policy().values
    ceiling = policy_values["max_search_invocations_per_run"]
    search_price = policy_values["search_cost_per_invocation"]
    assert f"≤ {ceiling} per run, hard" in doc, (
        "the authorization doc states a search ceiling the policy does not")
    assert search_price == 0.003
    assert "$0.003 per admitted search" in doc


def test_a_provider_wallet_ceiling_is_a_prerequisite_not_a_suggestion():
    doc = AUTHORIZATION_DOC.read_text()
    assert "PREREQUISITE" in doc
    assert "spending/wallet ceiling" in doc
    # It must appear in the operator steps AND in the results table.
    steps = doc[doc.index("## 8. Remaining manual operator steps"):doc.index("## 9. Results")]
    assert "wallet ceiling" in steps
    results = doc[doc.index("## 9. Results"):]
    assert "wallet ceiling" in results


def test_runtime_changes_since_the_pinned_release_are_declared_superseding():
    """Stage D pinned image digests on a "no runtime change" premise.

    That premise is checkable: if `backend/` has moved since the pinned
    release SHA, the digests this document pins no longer describe what would
    run, and the document must say so rather than reading as still-valid.
    A release that changes runtime code is exactly the "separate reviewed
    release" §7 anticipated, and it owes Stage D a re-authorization.
    """
    doc = AUTHORIZATION_DOC.read_text()
    assert "No runtime change is proposed here" in doc
    changed = subprocess.run(
        ["git", "diff", "--name-only", RELEASE_SHA],
        cwd=REPO, capture_output=True, text=True, timeout=60)
    if changed.returncode != 0:
        pytest.skip("the pinned release SHA is not available in this checkout")
    touched = [f for f in changed.stdout.split() if f.startswith("backend/")]
    if not touched:
        return
    assert "SUPERSEDED BY A RUNTIME RELEASE" in doc, (
        f"backend runtime moved since the pinned release ({touched}) but the "
        "Stage D authorization still presents its pinned digests as valid")
    assert "RE-AUTHORIZATION REQUIRED" in doc
    assert "invalidated" in doc


# ---------------------------------------------------------------------------
# O. The PRIVILEGED probe runtime is pinned by digest (round-3 finding 1)
# ---------------------------------------------------------------------------

PROBE_REPO = "docker.io/library/python"
PROBE_DIGEST = "sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea"
PROBE_IMAGE = f"{PROBE_REPO}@{PROBE_DIGEST}"
API_SA = "milo-api-runtime@big-cabinet-457321-t7.iam.gserviceaccount.com"
GATEWAY_SA = "milo-vercel-gateway@big-cabinet-457321-t7.iam.gserviceaccount.com"
#: env name -> (Secret Manager secret, version). The env NAME is only a
#: label; the reference is what is actually read.
DB_PROBE_SECRET_REFS = {
    "SUPABASE_URL": ("SUPABASE_URL", "latest"),
    "SUPABASE_SERVICE_ROLE_KEY": ("SUPABASE_SECRET_KEY", "latest"),
}


def probe_job_doc(image=PROBE_IMAGE, sa=API_SA, secrets=None):
    secrets = DB_PROBE_SECRET_REFS if secrets is None else secrets
    env = [{"name": "PROBE_SOURCE_GZIP_B64", "value": "<elided>"}]
    for env_name, ref in dict(secrets).items():
        secret, version = ref if isinstance(ref, (list, tuple)) else (ref, "latest")
        env.append({"name": env_name,
                    "valueFrom": {"secretKeyRef": {"name": secret, "key": version}}})
    return {"spec": {"template": {"spec": {"template": {"spec": {
        "serviceAccountName": sa,
        "containers": [{"image": image, "env": env}],
    }}}}}}


def run_verify_probe_jobs(tmp_path, *, db=None, gw=None, repo=PROBE_REPO, digest=PROBE_DIGEST):
    argv = [sys.executable, str(STAGE_D / "verify_probe_jobs.py")]
    for flag, doc in (("--db-json", db), ("--gw-json", gw)):
        if doc is None:
            continue
        path = tmp_path / f"{flag.strip('-')}.json"
        path.write_text(doc if isinstance(doc, str) else json.dumps(doc))
        argv += [flag, str(path)]
    return subprocess.run(
        argv, capture_output=True, text=True,
        env={**os.environ, "STAGE_D_PROBE_IMAGE_REPO": repo, "STAGE_D_PROBE_IMAGE_DIGEST": digest,
             "STAGE_D_API_SA": API_SA, "STAGE_D_GATEWAY_SA": GATEWAY_SA},
        timeout=60)


def test_probe_job_gate_passes_on_the_reviewed_jobs(tmp_path):
    result = run_verify_probe_jobs(
        tmp_path, db=probe_job_doc(), gw=probe_job_doc(sa=GATEWAY_SA, secrets={}))
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["ok"] is True


def test_probe_job_gate_refuses_a_tag_only_image(tmp_path):
    """The exact defect: a credentialed job created from a mutable tag."""
    result = run_verify_probe_jobs(tmp_path, db=probe_job_doc(image="python:3.12-slim"))
    assert result.returncode != 0
    assert "is a TAG reference" in result.stdout
    assert "production credentials" in result.stdout
    assert "service-role access" in result.stderr


def test_probe_job_gate_refuses_a_moved_digest(tmp_path):
    result = run_verify_probe_jobs(tmp_path, db=probe_job_doc(image=f"{PROBE_REPO}@sha256:" + "a" * 64))
    assert result.returncode != 0
    assert "not the reviewed probe runtime digest" in result.stdout


def test_probe_job_gate_refuses_a_missing_image(tmp_path):
    doc = probe_job_doc()
    doc["spec"]["template"]["spec"]["template"]["spec"]["containers"][0].pop("image")
    result = run_verify_probe_jobs(tmp_path, db=doc)
    assert result.returncode != 0
    assert "image reference is missing" in result.stdout


@pytest.mark.parametrize("foreign", [
    f"ghcr.io/attacker/python@{PROBE_DIGEST}",
    f"us-central1-docker.pkg.dev/attacker/evil/python@{PROBE_DIGEST}",
    f"python-evil@{PROBE_DIGEST}",
])
def test_probe_job_gate_refuses_a_foreign_image(tmp_path, foreign):
    result = run_verify_probe_jobs(tmp_path, db=probe_job_doc(image=foreign))
    assert result.returncode != 0
    assert "not the reviewed probe repository" in result.stdout


def test_probe_job_gate_refuses_an_unexpected_identity(tmp_path):
    result = run_verify_probe_jobs(tmp_path, db=probe_job_doc(sa="attacker@evil.iam.gserviceaccount.com"))
    assert result.returncode != 0
    assert "operator-controlled" in result.stdout


@pytest.mark.parametrize("secrets,marker", [
    ({}, "is missing"),
    ({"SUPABASE_URL": ("SUPABASE_URL", "latest")}, "is missing"),
    ({**DB_PROBE_SECRET_REFS, "EXTRA": ("SOMETHING", "latest")}, "unexpected secret reference"),
])
def test_probe_job_gate_refuses_unexpected_secret_bindings(tmp_path, secrets, marker):
    result = run_verify_probe_jobs(tmp_path, db=probe_job_doc(secrets=secrets))
    assert result.returncode != 0
    assert marker in result.stdout


@pytest.mark.parametrize("wrong_ref,marker", [
    # The interesting attack: right env NAME, wrong backing secret.
    ({"SUPABASE_SERVICE_ROLE_KEY": ("SOME_OTHER_SECRET", "latest")}, "SOME_OTHER_SECRET:latest"),
    ({"SUPABASE_SERVICE_ROLE_KEY": ("KIMI_API_KEY", "latest")}, "KIMI_API_KEY:latest"),
    ({"SUPABASE_URL": ("SUPABASE_SECRET_KEY", "latest")}, "SUPABASE_SECRET_KEY:latest"),
    # Wrong version pins the probe to a stale or attacker-chosen value.
    ({"SUPABASE_SERVICE_ROLE_KEY": ("SUPABASE_SECRET_KEY", "3")}, "SUPABASE_SECRET_KEY:3"),
    ({"SUPABASE_URL": ("SUPABASE_URL", "1")}, "SUPABASE_URL:1"),
])
def test_probe_job_gate_refuses_a_wrong_secret_reference_or_version(tmp_path, wrong_ref, marker):
    """Checking env NAMES alone would accept a variable wired elsewhere."""
    result = run_verify_probe_jobs(
        tmp_path, db=probe_job_doc(secrets={**DB_PROBE_SECRET_REFS, **wrong_ref}))
    assert result.returncode != 0
    assert marker in result.stdout
    assert "the reference is what is actually read" in result.stdout


def test_probe_job_gate_refuses_an_unreadable_secret_reference(tmp_path):
    doc = probe_job_doc()
    env = doc["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["env"]
    for entry in env:
        if entry.get("name") == "SUPABASE_SERVICE_ROLE_KEY":
            entry["valueFrom"] = {"somethingElse": {}}
    result = run_verify_probe_jobs(tmp_path, db=doc)
    assert result.returncode != 0
    assert "<unreadable>" in result.stdout


@pytest.mark.parametrize("spelling", ["python", "docker.io/library/python", "index.docker.io/library/python"])
def test_probe_job_gate_accepts_every_canonical_spelling_of_the_same_image(tmp_path, spelling):
    """Cloud Run may rewrite the repository; the DIGEST is what is enforced."""
    result = run_verify_probe_jobs(tmp_path, db=probe_job_doc(image=f"{spelling}@{PROBE_DIGEST}"))
    assert result.returncode == 0, result.stdout + result.stderr


def test_probe_job_gate_refuses_a_provider_alias_on_a_probe(tmp_path):
    result = run_verify_probe_jobs(
        tmp_path, db=probe_job_doc(
            secrets={**DB_PROBE_SECRET_REFS, "KIMI_API_KEY": ("KIMI_API_KEY", "latest")}))
    assert result.returncode != 0
    assert "provider alias KIMI_API_KEY must NEVER be present" in result.stdout


def test_probe_job_gate_requires_the_gateway_probe_to_hold_no_secrets(tmp_path):
    result = run_verify_probe_jobs(
        tmp_path, gw=probe_job_doc(sa=GATEWAY_SA,
                                   secrets={"SUPABASE_SERVICE_ROLE_KEY": ("SUPABASE_SECRET_KEY", "latest")}))
    assert result.returncode != 0
    assert "unexpected secret reference" in result.stdout


def test_probe_job_gate_fails_closed_without_a_pinned_digest(tmp_path):
    assert run_verify_probe_jobs(tmp_path, db=probe_job_doc(), digest="").returncode != 0
    assert run_verify_probe_jobs(tmp_path, db=probe_job_doc(), repo="").returncode != 0
    assert run_verify_probe_jobs(tmp_path, db=probe_job_doc(), digest="3.12-slim").returncode != 0


def test_probe_creation_uses_the_pinned_digest_and_never_a_tag():
    text = (STAGE_D / "04-create-probes.sh").read_text()
    assert 'PROBE_IMAGE="${STAGE_D_PROBE_IMAGE_REPO}@${STAGE_D_PROBE_IMAGE_DIGEST}"' in text
    assert text.count('--image="${PROBE_IMAGE}"') == 2
    code = [line for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")]
    assert not [line for line in code if "python:3.12-slim" in line]


def test_probe_jobs_are_verified_after_creation_and_before_every_execution():
    creation = (STAGE_D / "04-create-probes.sh").read_text()
    assert "verify_probe_jobs.py --db-json" in creation
    assert creation.index("gcloud run jobs create") < creation.index("Post-creation verification")
    for name in ("05-execute-run.sh", "06-collect-evidence.sh"):
        text = (STAGE_D / name).read_text()
        assert "source ./probe_exec.sh" in text
        assert 'verify_probe_job "${job}"' in text
        # The verification precedes the execution within run_probe.
        assert text.index('verify_probe_job "${job}"') < text.index("gcloud run jobs execute")
    lockdown = (STAGE_D / "07-post-run-lockdown.sh").read_text()
    assert "execute_probe_attributed" in lockdown
    assert 'verify_probe_job "${job}"' in (STAGE_D / "probe_exec.sh").read_text()


def test_the_env_documents_the_mirror_posture_honestly():
    text = (STAGE_D / "stage-d-env.sh").read_text()
    assert "STANDARD repository, not a REMOTE one" in text
    assert "mirroring preserves the manifest digest" in text
    assert PROBE_DIGEST in text


# ---------------------------------------------------------------------------
# P. The reviewed probe SOURCE is pinned by hash (round-3 finding 5)
# ---------------------------------------------------------------------------


def pinned_source_hashes() -> dict[str, str]:
    text = (STAGE_D / "stage-d-env.sh").read_text()
    out = {}
    for var, name in (("STAGE_D_PROBE_DB_SHA256", "probe_db.py"),
                      ("STAGE_D_PROBE_GW_SHA256", "probe_gateway.py")):
        match = re.search(rf'stage_d_pin {var} "([0-9a-f]{{64}})"', text)
        assert match, f"{var} is not pinned to a sha256"
        out[name] = match.group(1)
    return out


def test_the_pinned_probe_source_hashes_match_the_committed_files():
    """A stale pin would block the operator; a missing one would ship
    unreviewed privileged code. CI keeps them honest."""
    import hashlib
    for name, expected in pinned_source_hashes().items():
        actual = hashlib.sha256((STAGE_D / name).read_bytes()).hexdigest()
        assert actual == expected, (
            f"{name} changed but STAGE_D_PROBE_*_SHA256 was not regenerated: {actual}")


def run_probe_creation(tmp_path, *, tamper=None, env_overrides=None):
    """Run 04-create-probes.sh far enough to reach (or refuse before) gcloud."""
    world = StageDWorld(tmp_path)
    if tamper:
        target = world.dir / tamper
        target.write_text(target.read_text() + "\n# tampered\n")
    return world, subprocess.run(
        ["bash", str(world.dir / "04-create-probes.sh")],
        capture_output=True, text=True, cwd=str(world.dir),
        env=world.env(**(env_overrides or {})), timeout=300)


@pytest.mark.parametrize("tampered", ["probe_db.py", "probe_gateway.py"])
def test_probe_creation_refuses_tampered_source_before_any_mutation(tmp_path, tampered):
    """TAMPER TEST — an edited privileged probe must never be shipped."""
    world, result = run_probe_creation(tmp_path, tamper=tampered)
    assert result.returncode != 0
    assert "is NOT the reviewed source" in result.stderr
    assert "refusing before any gcloud mutation" in result.stderr
    # Nothing was created: the refusal precedes every gcloud call.
    assert world.read_state()["jobs"] == ["milo-agent-worker"]
    assert not world.log.exists() or "jobs create" not in world.log.read_text()


def test_the_source_pin_cannot_be_cleared_from_the_environment(tmp_path):
    """Clearing the pin from the shell must not disable the check."""
    world, result = run_probe_creation(
        tmp_path, env_overrides={"STAGE_D_PROBE_DB_SHA256": ""})
    # An empty inherited value is treated as unset, so the committed pin is
    # used and the reviewed source still verifies.
    assert result.returncode == 0, result.stdout + result.stderr
    assert "matches its reviewed SHA-256" in result.stdout


def test_a_forged_source_hash_cannot_be_supplied_from_the_environment(tmp_path):
    """The real attack: tamper with the file AND supply its new hash."""
    import hashlib
    world = StageDWorld(tmp_path)
    target = world.dir / "probe_db.py"
    target.write_text(target.read_text() + "\n# tampered\n")
    forged = hashlib.sha256(target.read_bytes()).hexdigest()
    result = subprocess.run(
        ["bash", str(world.dir / "04-create-probes.sh")],
        capture_output=True, text=True, cwd=str(world.dir),
        env=world.env(STAGE_D_PROBE_DB_SHA256=forged), timeout=300)
    assert result.returncode != 0
    # stage-d-env.sh refuses the conflicting override before anything runs.
    assert "STAGE D REFUSED" in result.stderr
    assert world.read_state()["jobs"] == ["milo-agent-worker"]


def test_probe_creation_succeeds_on_the_reviewed_source(tmp_path):
    world, result = run_probe_creation(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "matches its reviewed SHA-256" in result.stdout
    assert sorted(world.read_state()["jobs"]) == ["milo-agent-worker", "stage-d-db-probe", "stage-d-gw-probe"]
    # Created from the pinned digest, never a tag.
    created = [line for line in world.log.read_text().splitlines() if "jobs create" in line]
    assert len(created) == 2
    for line in created:
        assert PROBE_IMAGE in line
        assert "python:3.12-slim" not in line


def test_probe_creation_verifies_the_templates_it_created(tmp_path):
    """A job that was created wrong must be caught, not assumed correct."""
    world = StageDWorld(tmp_path)
    world.set_state(probe_jobs={"stage-d-db-probe": {
        "image": "python:3.12-slim", "sa": API_SA,
        "secrets": {k: list(v) for k, v in DB_PROBE_SECRET_REFS.items()}}})
    result = subprocess.run(
        ["bash", str(world.dir / "04-create-probes.sh")],
        capture_output=True, text=True, cwd=str(world.dir), env=world.env(), timeout=300)
    assert result.returncode != 0
    assert "is a TAG reference" in result.stdout
    assert "not what was reviewed" in result.stderr


# ---------------------------------------------------------------------------
# Q. The DATABASE run is terminalized and proved (round-3 finding 2)
# ---------------------------------------------------------------------------

sys.path.insert(0, str(REPO / "tests" / "fixtures" / "stage_d"))
from fake_postgrest import FakePostgrest, wire as wire_postgrest  # noqa: E402

STAGE_D_RUN_ID = "aaaa1111-2222-3333-4444-555566667777"
RUN_USER = "ffffffff-1111-2222-3333-444444444444"
RUN_CONVERSATION = "99999999-8888-7777-6666-555555555555"
RUN_PROJECT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def stage_d_run(status="running", metadata_stage="stage-d-smoke", **overrides):
    row = {
        "id": STAGE_D_RUN_ID,
        "input": {"content": "Stage D expansion step 1",
                  "metadata": ({"stage": metadata_stage} if metadata_stage is not None else {})},
        "status": status,
        "launch_state": "launched",
        "worker_id": "worker-1",
        "attempt": 1,
        "started_at": "2026-09-19T00:00:00Z",
        "finished_at": None,
        "idempotency_key": STAGE_D_KEY,
        "requested_by": RUN_USER,
        "conversation_id": RUN_CONVERSATION,
    }
    row.update(overrides)
    return row


def reservation(seq, status="reserved"):
    return {"id": f"res-{seq}", "run_id": STAGE_D_RUN_ID, "call_seq": seq, "status": status}


def set_recorded_identity(monkeypatch, expected_user, expected_conversation):
    """None models a lockdown that could not read the field from state.json."""
    for name, value in (("STAGE_D_EXPECTED_USER_ID", expected_user),
                        ("STAGE_D_EXPECTED_CONVERSATION_ID", expected_conversation)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def wire_terminalize(db, monkeypatch, *, runs=None, reservations=None, run_id=STAGE_D_RUN_ID,
                     expected_user=RUN_USER, expected_conversation=RUN_CONVERSATION):
    monkeypatch.setenv("STAGE_D_RUN_ID", run_id)
    monkeypatch.setenv("STAGE_D_IDEMPOTENCY_KEY", STAGE_D_KEY)
    monkeypatch.setenv("STAGE_D_GOV_CAPTURE_RUN_ID", GOV_RUN_ID)
    set_recorded_identity(monkeypatch, expected_user, expected_conversation)
    fake = FakePostgrest(
        runs=runs if runs is not None else [stage_d_run()],
        reservations=reservations or [],
        conversations=[{"id": RUN_CONVERSATION, "project_id": RUN_PROJECT}],
    )
    return wire_postgrest(db, monkeypatch, fake)


def run_terminalize(db) -> int:
    """Run terminalize and return its exit code.

    The probe always exits — 0 when every proof held, 1 otherwise — the way
    a CLI gate should, so the shell can read the status.
    """
    try:
        db.terminalize()
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


def terminalize_verdict(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


@pytest.mark.parametrize("status", ["queued", "launching", "starting", "running", "waiting"])
def test_terminalize_drives_an_interrupted_run_to_cancelled(db, monkeypatch, capsys, status):
    """Cancelling the Cloud Run execution does not close the DB run.

    The Worker has no SIGTERM handler, so an interrupted run stays in an
    active state holding its lease and its concurrency slot until
    something terminalizes it.
    """
    fake = wire_terminalize(db, monkeypatch, runs=[stage_d_run(status=status)])
    assert run_terminalize(db) == 0
    verdict = terminalize_verdict(capsys)
    assert verdict["ok"] is True, verdict["problems"]
    assert verdict["terminal"] is True
    # The ROW really changed — this is state, not a canned response.
    assert fake.run(STAGE_D_RUN_ID)["status"] == "cancelled"
    assert fake.run(STAGE_D_RUN_ID)["finished_at"]
    assert verdict["active_runs_for_user"] == 0
    assert verdict["active_runs_for_project"] == 0


def test_terminalize_follows_the_supported_two_step_lifecycle(db, monkeypatch, capsys):
    """running -> cancellation_requested -> cancelled, not a forged jump."""
    fake = wire_terminalize(db, monkeypatch)
    assert run_terminalize(db) == 0
    verdict = terminalize_verdict(capsys)
    assert verdict["actions"][:2] == ["running->cancellation_requested", "cancellation_requested->cancelled"]
    patches = [p for m, p in fake.calls if m == "PATCH"]
    assert len(patches) == 2
    # Every PATCH is guarded on the observed status AND the authorized key.
    for path in patches:
        assert f"id=eq.{STAGE_D_RUN_ID}" in path
        assert "status=eq." in path
        assert f"idempotency_key=eq.{STAGE_D_KEY}" in path


def test_terminalize_releases_dangling_reservations_and_proves_zero(db, monkeypatch, capsys):
    """A dangling reservation keeps the daily budget consumed forever."""
    fake = wire_terminalize(
        db, monkeypatch,
        reservations=[reservation(1), reservation(2), reservation(3, status="settled")])
    assert fake.reserved_count(STAGE_D_RUN_ID) == 2
    assert run_terminalize(db) == 0
    verdict = terminalize_verdict(capsys)
    assert verdict["ok"] is True, verdict["problems"]
    assert verdict["reservations_released"] == 2
    assert verdict["reservations_still_reserved"] == 0
    assert fake.reserved_count(STAGE_D_RUN_ID) == 0
    # Released through the SUPPORTED RPC, not a raw table write.
    assert len(fake.rpc_calls) == 2
    for call in fake.rpc_calls:
        assert call["p_status"] == "released"
        assert call["p_actual_cost"] == 0
        assert "stage-d cleanup" in call["p_rejection_reason"]
    assert not [p for m, p in fake.calls
                if m == "PATCH" and "model_call_budget_reservations" in p]


def test_terminalize_interruption_with_running_row_and_dangling_reservation(db, monkeypatch, capsys):
    """The full interruption shape: mid-run row plus held budget."""
    fake = wire_terminalize(
        db, monkeypatch,
        runs=[stage_d_run(status="running")],
        reservations=[reservation(i) for i in range(1, 6)])
    assert run_terminalize(db) == 0
    verdict = terminalize_verdict(capsys)
    assert verdict["ok"] is True, verdict["problems"]
    assert fake.run(STAGE_D_RUN_ID)["status"] == "cancelled"
    assert fake.reserved_count(STAGE_D_RUN_ID) == 0
    assert verdict["active_runs_for_user"] == 0
    assert verdict["active_runs_for_project"] == 0


def test_terminalize_leaves_an_already_terminal_run_alone(db, monkeypatch, capsys):
    fake = wire_terminalize(db, monkeypatch, runs=[stage_d_run(status="completed",
                                                              finished_at="2026-09-19T01:00:00Z")])
    assert run_terminalize(db) == 0
    verdict = terminalize_verdict(capsys)
    assert verdict["ok"] is True, verdict["problems"]
    assert verdict["actions"] == []
    assert fake.run(STAGE_D_RUN_ID)["status"] == "completed"
    assert not [c for c in fake.mutating_calls()]


def test_terminalize_refuses_a_run_this_authorization_does_not_own(db, monkeypatch, capsys):
    """IDENTITY CHECK — never terminalize somebody else's run."""
    fake = wire_terminalize(db, monkeypatch,
                            runs=[stage_d_run(idempotency_key="swarm-v2-smoke-20260825-4dbdcd6-01")])
    assert run_terminalize(db) != 0
    verdict = terminalize_verdict(capsys)
    assert "not the authorized Stage D key" in " ".join(verdict["problems"])
    assert fake.mutating_calls() == []
    assert fake.run(STAGE_D_RUN_ID)["status"] == "running"


def test_terminalize_refuses_the_government_capture_run(db, monkeypatch, capsys):
    monkeypatch.setenv("STAGE_D_RUN_ID", GOV_RUN_ID)
    monkeypatch.setenv("STAGE_D_IDEMPOTENCY_KEY", STAGE_D_KEY)
    monkeypatch.setenv("STAGE_D_GOV_CAPTURE_RUN_ID", GOV_RUN_ID)
    set_recorded_identity(monkeypatch, RUN_USER, RUN_CONVERSATION)
    fake = wire_postgrest(db, monkeypatch, FakePostgrest(runs=[stage_d_run(id=GOV_RUN_ID)]))
    assert run_terminalize(db) != 0
    assert "refusing to touch it" in " ".join(terminalize_verdict(capsys)["problems"])
    assert fake.mutating_calls() == []


def test_terminalize_never_overwrites_a_concurrent_terminal_result(db, monkeypatch, capsys):
    """A worker writing its own terminal result must win the race.

    Losing the CAS is not a failure — the desired end state is exactly
    what the other writer produced. The probe re-reads, sees a terminal
    run, and the POSTCONDITION PROOF is what decides.
    """
    fake = wire_terminalize(db, monkeypatch)
    original_call = fake.call
    raced = {"done": False}

    def racing_call(method, path, body=None, headers=None):
        if method == "PATCH" and not raced["done"]:
            # Someone else finishes the run first: the guarded CAS matches 0.
            fake.run(STAGE_D_RUN_ID)["status"] = "failed"
            fake.run(STAGE_D_RUN_ID)["finished_at"] = "2026-09-19T02:00:00Z"
            raced["done"] = True
        return original_call(method, path, body, headers)

    monkeypatch.setattr(db, "call", racing_call)
    assert run_terminalize(db) == 0
    verdict = terminalize_verdict(capsys)
    # The other writer's result stands, untouched.
    assert fake.run(STAGE_D_RUN_ID)["status"] == "failed"
    # And the cleanup still succeeds, because the postcondition holds.
    assert verdict["ok"] is True, verdict["problems"]
    assert verdict["terminal"] is True
    assert any(a.startswith("cas_lost_at_") for a in verdict["actions"])


def test_terminalize_fails_closed_when_the_cas_keeps_matching_nothing(db, monkeypatch, capsys):
    """A CAS that matches nothing while the row is unchanged is a real
    problem: it means the row is not the authorized run."""
    fake = wire_terminalize(db, monkeypatch)
    original_call = fake.call

    def refusing_call(method, path, body=None, headers=None):
        if method == "PATCH":
            return 200, []  # matched no row, and nothing changed
        return original_call(method, path, body, headers)

    monkeypatch.setattr(db, "call", refusing_call)
    assert run_terminalize(db) != 0
    verdict = terminalize_verdict(capsys)
    assert "matched no row although the run is still" in " ".join(verdict["problems"])
    assert fake.run(STAGE_D_RUN_ID)["status"] == "running"


def test_terminalize_reports_remaining_active_runs_as_a_failure(db, monkeypatch, capsys):
    other = stage_d_run(id="bbbb1111-2222-3333-4444-555566667777", status="running",
                        idempotency_key="other-key")
    fake = wire_terminalize(db, monkeypatch, runs=[stage_d_run(), other])
    with pytest.raises(SystemExit):
        db.terminalize()
    problems = " ".join(terminalize_verdict(capsys)["problems"])
    assert "active run(s) remain" in problems
    # The authorized run was still closed; only the proof failed.
    assert fake.run(STAGE_D_RUN_ID)["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Q2. The LOST-RUN window (round-4 finding 1)
#
# 05-execute-run.sh creates the run inside the gateway probe and writes
# run_id to state.json only after parsing the probe's structured output. If
# anything dies in between, the run EXISTS but the cleanup is handed an
# empty id. Treating that as "no run" would delete the credentialed probe
# and walk away from an active run holding a lease, a concurrency slot and
# budget reservations.
# ---------------------------------------------------------------------------


def wire_lost_run(db, monkeypatch, *, runs, reservations=None,
                  expected_user=RUN_USER, expected_conversation=RUN_CONVERSATION):
    """The crash window: state.json has the identity but NOT the run id."""
    monkeypatch.setenv("STAGE_D_RUN_ID", "")
    monkeypatch.setenv("STAGE_D_IDEMPOTENCY_KEY", STAGE_D_KEY)
    monkeypatch.setenv("STAGE_D_GOV_CAPTURE_RUN_ID", GOV_RUN_ID)
    monkeypatch.setenv("STAGE_D_GOV_CAPTURE_OPERATION", GOV_OPERATION)
    set_recorded_identity(monkeypatch, expected_user, expected_conversation)
    return wire_postgrest(db, monkeypatch, FakePostgrest(
        runs=runs, reservations=reservations or [],
        conversations=[{"id": RUN_CONVERSATION, "project_id": RUN_PROJECT}]))


def test_a_lost_run_id_is_recovered_from_the_idempotency_key(db, monkeypatch, capsys):
    """THE crash window: an active run exists, its id never reached state."""
    fake = wire_lost_run(db, monkeypatch,
                         runs=[stage_d_run(status="running")],
                         reservations=[reservation(1)])
    assert run_terminalize(db) == 0
    verdict = terminalize_verdict(capsys)
    assert verdict["ok"] is True, verdict["problems"]
    assert verdict["recovered"] is True
    assert verdict["recovered_run_id"] == STAGE_D_RUN_ID
    assert "recovered_run_id_from_idempotency_key" in verdict["actions"]
    # The recovered run really was closed and its budget really released.
    assert fake.run(STAGE_D_RUN_ID)["status"] == "cancelled"
    assert fake.reserved_count(STAGE_D_RUN_ID) == 0
    assert verdict["active_runs_for_user"] == 0
    assert verdict["active_runs_for_project"] == 0


def test_a_recovered_terminal_run_still_has_its_reservations_released(db, monkeypatch, capsys):
    """A finished run can still hold reserved budget forever."""
    fake = wire_lost_run(db, monkeypatch,
                         runs=[stage_d_run(status="failed", finished_at="2026-09-19T01:00:00Z")],
                         reservations=[reservation(1), reservation(2)])
    assert run_terminalize(db) == 0
    verdict = terminalize_verdict(capsys)
    assert verdict["ok"] is True, verdict["problems"]
    assert verdict["recovered"] is True
    assert verdict["reservations_released"] == 2
    assert fake.reserved_count(STAGE_D_RUN_ID) == 0
    # The terminal status was not rewritten.
    assert fake.run(STAGE_D_RUN_ID)["status"] == "failed"


def test_no_rows_under_the_key_is_a_proved_no_run_verdict(db, monkeypatch, capsys):
    wire_lost_run(db, monkeypatch, runs=[])
    assert run_terminalize(db) == 0
    verdict = terminalize_verdict(capsys)
    assert verdict["ok"] is True
    assert verdict["no_run_recorded"] is True
    assert verdict["rows_under_key"] == 0
    assert verdict["recovered"] is False


def test_two_rows_under_the_key_fails_closed_without_mutating(db, monkeypatch, capsys):
    second = stage_d_run(id="bbbb1111-2222-3333-4444-555566667777", status="running")
    fake = wire_lost_run(db, monkeypatch, runs=[stage_d_run(status="running"), second])
    assert run_terminalize(db) != 0
    verdict = terminalize_verdict(capsys)
    assert verdict["rows_under_key"] == 2
    assert "ambiguous" in " ".join(verdict["problems"])
    assert fake.mutating_calls() == []
    assert fake.run(STAGE_D_RUN_ID)["status"] == "running"


@pytest.mark.parametrize("mismatch,marker", [
    ({"requested_by": "11111111-1111-1111-1111-111111111111"}, "not the recorded Stage D test user"),
    ({"conversation_id": "22222222-2222-2222-2222-222222222222"}, "not the recorded Stage D conversation"),
    ({"metadata_stage": "something-else"}, "not the Stage D smoke run"),
    ({"metadata_stage": None}, "not the Stage D smoke run"),
])
def test_a_recovered_row_must_match_the_recorded_identity(db, monkeypatch, capsys, mismatch, marker):
    """A row sharing the key is not automatically the authorized run."""
    fake = wire_lost_run(db, monkeypatch, runs=[stage_d_run(status="running", **mismatch)])
    assert run_terminalize(db) != 0
    verdict = terminalize_verdict(capsys)
    assert marker in " ".join(verdict["problems"])
    assert fake.mutating_calls() == []
    assert fake.run(STAGE_D_RUN_ID)["status"] == "running"


def test_a_recovered_row_colliding_with_the_government_capture_is_refused(db, monkeypatch, capsys):
    """Belt and braces: even under the Stage D key, never touch the capture —
    and the capture refusal is named explicitly even when the recorded
    identity is ALSO missing (which is a refusal in its own right)."""
    capture = stage_d_run(id=GOV_RUN_ID, status="queued")
    fake = wire_lost_run(db, monkeypatch, runs=[capture],
                         expected_user=None, expected_conversation=None)
    assert run_terminalize(db) != 0
    verdict = terminalize_verdict(capsys)
    assert "prepared Government capture" in " ".join(verdict["problems"])
    assert fake.mutating_calls() == []


def test_a_recovered_row_carrying_the_capture_marker_is_refused(db, monkeypatch, capsys):
    marked = stage_d_run(status="running")
    marked["input"] = {"metadata": {"stage": "stage-d-smoke",
                                    "milo_operation": GOV_OPERATION}}
    fake = wire_lost_run(db, monkeypatch, runs=[marked])
    assert run_terminalize(db) != 0
    assert "operator-capture marker" in " ".join(terminalize_verdict(capsys)["problems"])
    assert fake.mutating_calls() == []


def test_an_absent_run_id_is_never_read_as_no_run(db, monkeypatch, capsys):
    """The regression itself: the old code only COUNTED and never recovered."""
    fake = wire_lost_run(db, monkeypatch, runs=[stage_d_run(status="running")])
    assert run_terminalize(db) == 0
    verdict = terminalize_verdict(capsys)
    assert verdict.get("no_run_recorded") is not True
    assert fake.run(STAGE_D_RUN_ID)["status"] == "cancelled"


def test_lockdown_recovers_a_lost_run_end_to_end(tmp_path):
    """state.json has user_id and conversation_id but NO run_id."""
    world = StageDWorld(tmp_path)
    world.enable_execution_surface()
    (world.workdir / "state.json").write_text(json.dumps({
        "stage_d_workdir": str(world.workdir),
        "idempotency_key": STAGE_D_KEY,
        "user_id": RUN_USER,
        "conversation_id": RUN_CONVERSATION,
    }))
    result = world.run("07-post-run-lockdown.sh")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "STAGE D LOCKDOWN COMPLETE" in result.stdout
    # The recorded identity was handed to the probe so a recovered row can
    # be checked against it.
    calls = world.log.read_text()
    assert f"STAGE_D_EXPECTED_USER_ID={RUN_USER}" in calls
    assert f"STAGE_D_EXPECTED_CONVERSATION_ID={RUN_CONVERSATION}" in calls


def test_lockdown_still_requires_the_terminalize_proof_with_no_recorded_run(tmp_path):
    """An empty run id must not become a free pass."""
    world = StageDWorld(tmp_path, state={
        "probe_verdicts": {"govcheck": True, "terminalize": False}})
    world.enable_execution_surface()
    result = world.run("07-post-run-lockdown.sh")
    assert result.returncode != 0
    assert "STAGE D LOCKDOWN COMPLETE" not in result.stdout
    assert "could not be proven terminal and clean" in result.stderr


def test_lockdown_runs_terminalize_before_deleting_the_probe(tmp_path):
    world = StageDWorld(tmp_path)
    world.enable_execution_surface()
    (world.workdir / "state.json").write_text(json.dumps(recorded_state()))
    result = world.run("07-post-run-lockdown.sh")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "STAGE D LOCKDOWN COMPLETE" in result.stdout
    calls = world.log.read_text().splitlines()
    terminalize_at = next(i for i, c in enumerate(calls) if "STAGE_D_MODE=terminalize" in c)
    delete_at = next(i for i, c in enumerate(calls) if "jobs delete stage-d-db-probe" in c)
    assert terminalize_at < delete_at


def test_lockdown_is_not_complete_when_the_database_run_cannot_be_closed(tmp_path):
    world = StageDWorld(tmp_path, state={
        "probe_verdicts": {"govcheck": True, "terminalize": False}})
    world.enable_execution_surface()
    (world.workdir / "state.json").write_text(json.dumps(recorded_state()))
    result = world.run("07-post-run-lockdown.sh")
    assert result.returncode != 0
    assert "STAGE D LOCKDOWN COMPLETE" not in result.stdout
    assert "could not be proven terminal and clean" in result.stderr
    # Probes are still removed.
    world.assert_probes_absent()
    world.assert_fail_closed()


def test_lockdown_is_partial_when_a_run_existed_but_no_probe_remains(tmp_path):
    world = StageDWorld(tmp_path)
    (world.workdir / "state.json").write_text(json.dumps(recorded_state()))
    result = world.run("07-post-run-lockdown.sh")
    assert result.returncode != 0
    assert "STAGE D LOCKDOWN COMPLETE" not in result.stdout
    assert "cannot be terminalized" in result.stderr


# ---------------------------------------------------------------------------
# R. Setup mutates NOTHING when it rejects a project (round-3 finding 3)
# ---------------------------------------------------------------------------


def rejected_setup_calls(db, monkeypatch, **kwargs):
    record: list[tuple[str, str]] = []
    wire_setup(db, monkeypatch, record=record, **kwargs)
    with pytest.raises(SystemExit):
        db.setup()
    return record


def mutating(record):
    return [(m, p) for m, p in record if m in ("POST", "PATCH", "PUT", "DELETE")]


@pytest.mark.parametrize("bad", [
    {"workflow_key": "swarm_v2"},
    {"configuration": {"stage": "stage-c"}},
    {"configuration": None},
    {"members": [{"user_id": "someone-else", "role": "owner"}]},
    {"user_active": 1},
    {"project_active": 1},
])
def test_rejected_setup_writes_nothing_at_all(db, monkeypatch, capsys, bad):
    """The defect: problems were recorded, then membership was upserted
    and a conversation created in the project that had just been rejected."""
    record = rejected_setup_calls(db, monkeypatch, **bad)
    verdict = setup_verdict(capsys)
    assert verdict["ok"] is False and verdict["mutations"] == []
    writes = mutating(record)
    assert writes == [], f"a rejected setup still wrote: {writes}"
    assert not [p for _m, p in record if "project_members" in p and _m == "POST"]
    assert not [p for _m, p in record if "conversations" in p and _m == "POST"]
    # It must not even create the test user before validating.
    assert not [p for _m, p in record if "admin/users" in p and _m == "POST"]


def test_rejected_setup_reports_an_empty_mutation_list(db, monkeypatch, capsys):
    rejected_setup_calls(db, monkeypatch, workflow_key="swarm_v2")
    assert setup_verdict(capsys)["mutations"] == []


def test_setup_validates_before_it_mutates(db, monkeypatch, capsys):
    """Ordering proof on the happy path.

    Every validation read must happen BEFORE the first write. (The
    membership set is read again afterwards, as the post-mutation proof —
    that later read is not part of validation.)
    """
    record: list[tuple[str, str]] = []
    wire_setup(db, monkeypatch, record=record)
    db.setup()
    assert setup_verdict(capsys)["ok"] is True
    writes = [i for i, (m, _p) in enumerate(record) if m == "POST"]
    assert writes, "the happy path performed no mutation at all"
    first_write = min(writes)
    before = [p for m, p in record[:first_write] if m == "GET"]
    # The three validation reads all precede the first write.
    assert any("admin/users" in p for p in before), "the user was not looked up before writing"
    assert any("projects?slug" in p for p in before), "the project was not validated before writing"
    assert any("project_members" in p for p in before), "membership was not validated before writing"


def test_setup_reproves_membership_after_the_authorized_mutation(db, monkeypatch, capsys):
    record: list[tuple[str, str]] = []
    wire_setup(db, monkeypatch, record=record)
    db.setup()
    verdict = setup_verdict(capsys)
    assert verdict["members"] == [f"{STAGE_D_USER_ID}:owner"]
    member_reads = [i for i, (m, p) in enumerate(record) if m == "GET" and "project_members" in p]
    member_write = next(i for i, (m, p) in enumerate(record) if m == "POST" and "project_members" in p)
    # Read before (validation) AND after (proof) the upsert.
    assert any(i < member_write for i in member_reads)
    assert any(i > member_write for i in member_reads)


def test_setup_creates_everything_for_a_brand_new_project(db, monkeypatch, capsys):
    record: list[tuple[str, str]] = []
    wire_setup(db, monkeypatch, project_exists=False, user_exists=False,
               members=[{"user_id": STAGE_D_USER_ID, "role": "owner"}], record=record)
    db.setup()
    verdict = setup_verdict(capsys)
    assert verdict["ok"] is True, verdict.get("problems")
    assert "created_project" in verdict["mutations"]
    assert "created_test_user" in verdict["mutations"]
    assert "upserted_membership" in verdict["mutations"]


# ---------------------------------------------------------------------------
# S. govcheck logs are attributed to the exact execution (round-3 finding 4)
# ---------------------------------------------------------------------------

STALE_PASS = {"stage_d_probe": "govcheck", "ok": True}


def test_a_stale_pass_from_an_older_job_cannot_satisfy_govcheck(tmp_path):
    """The defect: filtering by job NAME alone.

    A deleted-and-recreated job of the same name leaves retained logs. An
    old PASS plus a current execution that emits nothing must FAIL.
    """
    world = StageDWorld(tmp_path, state={
        "stale_logs": [STALE_PASS],
        "probe_silent_modes": ["govcheck"],
    })
    world.enable_execution_surface()
    result = world.run("07-post-run-lockdown.sh")
    assert result.returncode != 0
    assert "STAGE D LOCKDOWN COMPLETE" not in result.stdout
    assert "Government capture posture check" in result.stderr


def test_a_stale_pass_cannot_mask_a_current_failure(tmp_path):
    world = StageDWorld(tmp_path, state={
        "stale_logs": [STALE_PASS],
        "probe_verdicts": {"govcheck": False, "terminalize": True},
    })
    world.enable_execution_surface()
    result = world.run("07-post-run-lockdown.sh")
    assert result.returncode != 0
    assert "STAGE D LOCKDOWN COMPLETE" not in result.stdout


def test_a_stale_pass_cannot_mask_a_missing_terminalize_record(tmp_path):
    world = StageDWorld(tmp_path, state={
        "stale_logs": [{"stage_d_probe": "terminalize", "ok": True}],
        "probe_silent_modes": ["terminalize"],
    })
    world.enable_execution_surface()
    (world.workdir / "state.json").write_text(json.dumps(recorded_state()))
    result = world.run("07-post-run-lockdown.sh")
    assert result.returncode != 0
    assert "could not be proven terminal and clean" in result.stderr


def test_lockdown_filters_probe_logs_by_the_exact_execution_name(tmp_path):
    world = StageDWorld(tmp_path)
    world.enable_execution_surface()
    world.run("07-post-run-lockdown.sh")
    reads = [c for c in world.log.read_text().splitlines() if "logging read" in c]
    assert reads, "the lockdown read no probe logs at all"
    for call in reads:
        assert 'execution_name"=' in call, f"unattributed log query: {call}"
        assert 'execution_name"= ' not in call


def test_the_attributed_helper_launches_async_and_waits_on_that_execution():
    text = (STAGE_D / "probe_exec.sh").read_text()
    assert "--async --format='value(metadata.name)'" in text
    assert 'execution_name\\"=${exec_name}' in text
    assert "execution_state.py" in text
    assert "could not establish the execution name" in text
    # probe_verdict must fail closed when no record exists.
    assert "no structured" in text and "failing closed" in text


def test_probe_verdict_fails_closed_on_a_missing_record(tmp_path):
    log = tmp_path / "probe.log"
    log.write_text('{"stage_d_probe": "something-else", "ok": true}\n')
    script = (
        f'set -euo pipefail\n'
        f'cd "{STAGE_D}"\n'
        f'STAGE_D_PROJECT=p STAGE_D_REGION=r source ./probe_exec.sh\n'
        f'probe_verdict "{log}" govcheck\n'
    )
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    assert result.returncode != 0
    assert "failing closed" in result.stderr


# ---------------------------------------------------------------------------
# T. Preflight verifies the cleanup RPC it depends on (round-4 finding 2)
# ---------------------------------------------------------------------------

#: The live signature, verified read-only against production 2026-09-19:
#: SECURITY DEFINER, service_role may execute, anon/authenticated may not.
SETTLE_RPC = "settle_model_call_budget"
#: EVERY parameter of the deployed function. This is what the EXACT check
#: compares against, because an extra deployed parameter is a different
#: function and the cleanup calls this one with exactly these four keys.
SETTLE_RPC_ARGS = {"p_reservation_id", "p_actual_cost", "p_status", "p_rejection_reason"}
#: The subset a caller MUST supply: `p_status` and `p_rejection_reason` carry
#: defaults. The two sets answer different questions and the inventory now
#: derives both from the migration rather than conflating them.
SETTLE_RPC_REQUIRED_ARGS = {"p_reservation_id", "p_actual_cost"}


def test_preflight_requires_the_cleanup_rpc_terminalize_calls(db):
    """terminalize() releases reservations through it, so a missing or
    mismatched signature must block BEFORE any production enable."""
    assert SETTLE_RPC in db.REQUIRED_RPC_ARGS
    assert set(db.REQUIRED_RPC_ARGS[SETTLE_RPC]) == SETTLE_RPC_REQUIRED_ARGS
    assert set(db.EXACT_RPC_SIGNATURES[SETTLE_RPC]) == SETTLE_RPC_ARGS
    # And the probe really does call it.
    assert f"/rest/v1/rpc/{SETTLE_RPC}" in (STAGE_D / "probe_db.py").read_text()


def test_preflight_refuses_when_the_cleanup_rpc_is_absent(db, monkeypatch, capsys):
    monkeypatch.setenv("STAGE_D_EXPECTED_PRIOR_RUNS", EXPECTED_PRIOR_RUNS)
    original = db.REQUIRED_RPC_ARGS

    def fake_call(method, path, body=None, headers=None):
        if path == "/rest/v1/":
            paths = {f"/rpc/{rpc}": {"post": {"parameters": [
                {"in": "body", "schema": {"properties": {
                    a: {} for a in (db.EXACT_RPC_SIGNATURES.get(rpc) or args)}}}]}}
                for rpc, args in original.items() if rpc != SETTLE_RPC}
            return 200, {"paths": paths}
        if path.startswith(f"/rest/v1/runs?id=eq.{GOV_RUN_ID}"):
            return 200, [PREPARED_CAPTURE_ROW]
        return 200, []

    monkeypatch.setattr(db, "call", fake_call)
    monkeypatch.setattr(db, "count_exact", lambda path: 7 if path == "/rest/v1/runs?select=id" else 0)
    with pytest.raises(SystemExit):
        db.preflight()
    verdict = preflight_output(db, capsys)
    assert f"rpc_{SETTLE_RPC} is MISSING" in " ".join(verdict["problems"])


@pytest.mark.parametrize("dropped", sorted(SETTLE_RPC_ARGS))
def test_preflight_refuses_a_mismatched_cleanup_rpc_signature(db, monkeypatch, capsys, dropped):
    monkeypatch.setenv("STAGE_D_EXPECTED_PRIOR_RUNS", EXPECTED_PRIOR_RUNS)
    original = db.REQUIRED_RPC_ARGS

    def fake_call(method, path, body=None, headers=None):
        if path == "/rest/v1/":
            paths = {}
            for rpc, args in original.items():
                deployed = set(db.EXACT_RPC_SIGNATURES.get(rpc) or args)
                advertised = deployed - ({dropped} if rpc == SETTLE_RPC else set())
                paths[f"/rpc/{rpc}"] = {"post": {"parameters": [
                    {"in": "body", "schema": {"properties": {a: {} for a in advertised}}}]}}
            return 200, {"paths": paths}
        if path.startswith(f"/rest/v1/runs?id=eq.{GOV_RUN_ID}"):
            return 200, [PREPARED_CAPTURE_ROW]
        return 200, []

    monkeypatch.setattr(db, "call", fake_call)
    monkeypatch.setattr(db, "count_exact", lambda path: 7 if path == "/rest/v1/runs?select=id" else 0)
    with pytest.raises(SystemExit):
        db.preflight()
    problems = " ".join(preflight_output(db, capsys)["problems"])
    assert f"rpc_{SETTLE_RPC}" in problems and dropped in problems


def test_the_guarded_and_unguarded_settle_rpcs_are_both_required(db):
    """They are different functions with different signatures and callers."""
    assert "settle_model_call_budget_guarded" in db.REQUIRED_RPC_ARGS
    assert db.REQUIRED_RPC_ARGS["settle_model_call_budget_guarded"] != SETTLE_RPC_ARGS


# ---------------------------------------------------------------------------
# U. Exact membership includes the ROLE (round-4 finding 4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["viewer", "member", "admin", "", None])
def test_setup_refuses_the_right_user_with_the_wrong_role(db, monkeypatch, capsys, role):
    """Comparing user ids alone would accept the right person with a role
    that authorization reads differently."""
    record = rejected_setup_calls(
        db, monkeypatch, members=[{"user_id": STAGE_D_USER_ID, "role": role}])
    verdict = setup_verdict(capsys)
    assert verdict["ok"] is False
    assert "membership" in " ".join(verdict["problems"])
    # Refused during the READ-ONLY phase: nothing was written.
    assert verdict["mutations"] == []
    assert mutating(record) == []


def test_setup_reports_membership_with_roles_not_bare_ids(db, monkeypatch, capsys):
    wire_setup(db, monkeypatch)
    db.setup()
    verdict = setup_verdict(capsys)
    assert verdict["members"] == [f"{STAGE_D_USER_ID}:owner"]
    assert verdict["members_before"] == [f"{STAGE_D_USER_ID}:owner"]


def test_setup_proves_the_owner_role_after_the_authorized_mutation(db, monkeypatch, capsys):
    """The post-mutation proof must also require owner, not just presence."""
    wire_setup(db, monkeypatch, members=[{"user_id": STAGE_D_USER_ID, "role": "viewer"}],
               project_exists=False)
    with pytest.raises(SystemExit):
        db.setup()
    verdict = setup_verdict(capsys)
    assert "expected exactly" in " ".join(verdict["problems"])
    assert f"{STAGE_D_USER_ID}:owner" in " ".join(verdict["problems"])
    # A brand-new project is created, so the write DID happen — what failed
    # is the proof, which is the point.
    assert "created_project" in verdict["mutations"]


def test_membership_comparison_is_a_tuple_not_a_set_of_ids(db):
    assert db.EXPECTED_MEMBER_ROLE == "owner"
    assert db.membership_tuples([{"user_id": "u", "role": "owner"}]) == [("u", "owner")]
    assert db.membership_tuples([{"user_id": "u", "role": None}]) == [("u", "<null>")]
    assert db.membership_tuples([{"user_id": "u"}]) == [("u", "<null>")]


# ---------------------------------------------------------------------------
# V. The cleanup RPC signature must match EXACTLY (round-5 finding 1)
# ---------------------------------------------------------------------------
#
# PostgREST resolves a function by name AND argument keys. The cleanup calls
# settle_model_call_budget with exactly four keys, so a deployed signature
# with an ADDITIONAL argument is as disqualifying as one with a missing
# argument: a required extra makes the cleanup's call fail (PGRST202) and
# leaves the reservation held; a defaulted extra routes the call into a
# function body this authorization never reviewed. Subset semantics remain
# for the other RPCs, which the toolkit never invokes itself.


def rpc_surface(db, overrides=None):
    """An OpenAPI document advertising every required RPC, with per-RPC
    argument-set overrides."""
    paths = {}
    for rpc, args in db.REQUIRED_RPC_ARGS.items():
        # A DEPLOYED signature advertises its defaulted parameters too, so the
        # default advertisement for an exact-checked RPC is its exact pin, not
        # the subset a caller is required to supply.
        deployed = set(db.EXACT_RPC_SIGNATURES.get(rpc) or args)
        advertised = (overrides or {}).get(rpc, deployed)
        paths[f"/rpc/{rpc}"] = {"post": {"parameters": [
            {"in": "body", "schema": {"properties": {a: {} for a in advertised}}}]}}
    return {"paths": paths}


def rpc_surface_checks(db, monkeypatch, overrides=None):
    invocations = []

    def fake_call(method, path, body=None, headers=None):
        if method != "GET":
            invocations.append((method, path))
        if path == "/rest/v1/":
            return 200, rpc_surface(db, overrides)
        return 200, []

    monkeypatch.setattr(db, "call", fake_call)
    checks, problems = {}, []
    db.check_rpc_surface(checks, problems)
    assert invocations == [], "the surface check must never invoke an RPC"
    return checks, problems


def test_the_cleanup_rpc_is_held_to_an_exact_signature(db):
    assert SETTLE_RPC in db.EXACT_RPC_SIGNATURES
    assert set(db.EXACT_RPC_SIGNATURES[SETTLE_RPC]) == SETTLE_RPC_ARGS


def test_the_exact_four_argument_cleanup_signature_passes(db, monkeypatch):
    checks, problems = rpc_surface_checks(db, monkeypatch)
    assert checks[f"rpc_{SETTLE_RPC}"] == "present"
    assert problems == []


@pytest.mark.parametrize("dropped", sorted(SETTLE_RPC_ARGS))
def test_every_missing_cleanup_argument_fails_the_surface_check(db, monkeypatch, dropped):
    checks, problems = rpc_surface_checks(db, monkeypatch, {SETTLE_RPC: SETTLE_RPC_ARGS - {dropped}})
    assert checks[f"rpc_{SETTLE_RPC}"] == "SIGNATURE_MISMATCH"
    text = " ".join(problems)
    assert f"rpc_{SETTLE_RPC}" in text and dropped in text and "exactly" in text


@pytest.mark.parametrize("extra", ["p_run_id", "p_lease_token", "p_note"])
def test_one_additional_cleanup_argument_fails_the_surface_check(db, monkeypatch, extra):
    """A superset used to pass the subset check — that is the finding."""
    checks, problems = rpc_surface_checks(db, monkeypatch, {SETTLE_RPC: SETTLE_RPC_ARGS | {extra}})
    assert checks[f"rpc_{SETTLE_RPC}"] == "SIGNATURE_MISMATCH"
    text = " ".join(problems)
    assert f"rpc_{SETTLE_RPC}" in text and extra in text and "unexpected" in text


@pytest.mark.parametrize("extras", [
    {"p_run_id", "p_worker_id"},
    # The guarded overload's extra arguments grafted onto the plain one.
    {"p_run_id", "p_worker_id", "p_attempt", "p_lease_token"},
    {"p_a", "p_b", "p_c", "p_d", "p_e"},
])
def test_multiple_additional_cleanup_arguments_fail_the_surface_check(db, monkeypatch, extras):
    checks, problems = rpc_surface_checks(db, monkeypatch, {SETTLE_RPC: SETTLE_RPC_ARGS | extras})
    assert checks[f"rpc_{SETTLE_RPC}"] == "SIGNATURE_MISMATCH"
    text = " ".join(problems)
    for extra in extras:
        assert extra in text


def test_a_renamed_cleanup_argument_is_reported_as_missing_and_unexpected(db, monkeypatch):
    swapped = (SETTLE_RPC_ARGS - {"p_rejection_reason"}) | {"p_reason"}
    checks, problems = rpc_surface_checks(db, monkeypatch, {SETTLE_RPC: swapped})
    assert checks[f"rpc_{SETTLE_RPC}"] == "SIGNATURE_MISMATCH"
    text = " ".join(problems)
    assert "p_rejection_reason" in text and "p_reason" in text


def test_the_other_rpcs_keep_tolerant_subset_semantics(db, monkeypatch):
    """A migration adding an OPTIONAL parameter to an RPC the toolkit never
    calls itself must not fail the preflight — but a missing one still must."""
    others = [rpc for rpc in db.REQUIRED_RPC_ARGS if rpc not in db.EXACT_RPC_SIGNATURES]
    assert others, "the exact-signature rule is scoped to the cleanup RPC, not universal"
    assert len(others) > 40, "the required RPC surface is far smaller than current main's"
    widened = {rpc: set(db.REQUIRED_RPC_ARGS[rpc]) | {"p_new_optional"} for rpc in others}
    checks, problems = rpc_surface_checks(db, monkeypatch, widened)
    assert problems == []
    assert all(checks[f"rpc_{rpc}"] == "present" for rpc in others)
    narrowed = {rpc: set(sorted(db.REQUIRED_RPC_ARGS[rpc])[1:]) for rpc in others}
    checks, problems = rpc_surface_checks(db, monkeypatch, narrowed)
    assert all(checks[f"rpc_{rpc}"] == "SIGNATURE_MISMATCH" for rpc in others)


@pytest.mark.parametrize("advertised", [
    pytest.param(SETTLE_RPC_ARGS | {"p_run_id"}, id="one-extra"),
    pytest.param(SETTLE_RPC_ARGS | {"p_run_id", "p_worker_id", "p_attempt", "p_lease_token"},
                 id="many-extra"),
])
def test_preflight_refuses_an_additional_cleanup_argument_end_to_end(db, monkeypatch, capsys, advertised):
    """Through preflight() itself, the way the shell runs it."""
    monkeypatch.setenv("STAGE_D_EXPECTED_PRIOR_RUNS", EXPECTED_PRIOR_RUNS)

    def fake_call(method, path, body=None, headers=None):
        if path == "/rest/v1/":
            return 200, rpc_surface(db, {SETTLE_RPC: advertised})
        if path.startswith(f"/rest/v1/runs?id=eq.{GOV_RUN_ID}"):
            return 200, [PREPARED_CAPTURE_ROW]
        return 200, []

    monkeypatch.setattr(db, "call", fake_call)
    monkeypatch.setattr(db, "count_exact", lambda path: 7 if path == "/rest/v1/runs?select=id" else 0)
    with pytest.raises(SystemExit):
        db.preflight()
    verdict = preflight_output(db, capsys)
    assert verdict["checks"][f"rpc_{SETTLE_RPC}"] == "SIGNATURE_MISMATCH"
    assert "unexpected" in " ".join(verdict["problems"])


# ---------------------------------------------------------------------------
# W. The recorded identity is MANDATORY before terminalize touches a run
#    (round-5 finding 2)
# ---------------------------------------------------------------------------
#
# 05-execute-run.sh records user_id and conversation_id in state.json BEFORE
# the run is created, so the run this authorization created always has both
# on record. A cleanup that cannot produce them has lost the only evidence
# tying a row to that run. Sharing the idempotency key and the metadata
# marker is not enough: the probe refuses — recorded or recovered alike —
# before any PATCH or settlement RPC. Zero rows under the key stays a
# proved no-run verdict, because there is nothing to mutate.

MISSING_IDENTITY_CASES = [
    pytest.param({"expected_user": None}, ["STAGE_D_EXPECTED_USER_ID"], id="user-absent"),
    pytest.param({"expected_conversation": None}, ["STAGE_D_EXPECTED_CONVERSATION_ID"],
                 id="conversation-absent"),
    pytest.param({"expected_user": None, "expected_conversation": None},
                 ["STAGE_D_EXPECTED_USER_ID", "STAGE_D_EXPECTED_CONVERSATION_ID"], id="both-absent"),
    pytest.param({"expected_user": ""}, ["STAGE_D_EXPECTED_USER_ID"], id="user-empty"),
    pytest.param({"expected_conversation": "   "}, ["STAGE_D_EXPECTED_CONVERSATION_ID"],
                 id="conversation-blank"),
]


def assert_refused_without_a_write(fake, verdict, named):
    text = " ".join(verdict["problems"])
    for name in named:
        assert name in text, f"{name} not named in {text!r}"
    assert "refusing to terminalize" in text
    assert verdict["ok"] is False
    assert verdict.get("terminal") is not True
    assert verdict["recorded_identity_present"] == {
        "STAGE_D_EXPECTED_USER_ID": "STAGE_D_EXPECTED_USER_ID" not in named,
        "STAGE_D_EXPECTED_CONVERSATION_ID": "STAGE_D_EXPECTED_CONVERSATION_ID" not in named,
    }
    # Nothing was written: no PATCH, no settlement RPC.
    assert fake.mutating_calls() == []
    assert fake.rpc_calls == []
    assert fake.run(STAGE_D_RUN_ID)["status"] == "running"
    assert fake.reserved_count(STAGE_D_RUN_ID) == 1


@pytest.mark.parametrize("missing,named", MISSING_IDENTITY_CASES)
def test_a_recovered_row_is_refused_when_the_recorded_identity_is_missing(
        db, monkeypatch, capsys, missing, named):
    """The row matches the key and the marker — and is still refused."""
    fake = wire_lost_run(db, monkeypatch, runs=[stage_d_run(status="running")],
                         reservations=[reservation(1)], **missing)
    assert run_terminalize(db) != 0
    verdict = terminalize_verdict(capsys)
    assert verdict["recovered"] is True
    assert verdict["recovered_run_id"] == STAGE_D_RUN_ID
    assert_refused_without_a_write(fake, verdict, named)


@pytest.mark.parametrize("missing,named", MISSING_IDENTITY_CASES)
def test_a_recorded_run_id_is_refused_when_the_recorded_identity_is_missing(
        db, monkeypatch, capsys, missing, named):
    """The same rule for a run id that state.json DID preserve."""
    fake = wire_terminalize(db, monkeypatch, runs=[stage_d_run(status="running")],
                            reservations=[reservation(1)], **missing)
    assert run_terminalize(db) != 0
    verdict = terminalize_verdict(capsys)
    assert verdict["recovered"] is False
    assert_refused_without_a_write(fake, verdict, named)


def test_a_recovered_row_with_the_recorded_identity_is_terminalized(db, monkeypatch, capsys):
    """The positive control for the refusals above."""
    fake = wire_lost_run(db, monkeypatch, runs=[stage_d_run(status="running")],
                         reservations=[reservation(1)])
    assert run_terminalize(db) == 0
    verdict = terminalize_verdict(capsys)
    assert verdict["recorded_identity_present"] == {
        "STAGE_D_EXPECTED_USER_ID": True, "STAGE_D_EXPECTED_CONVERSATION_ID": True}
    assert fake.run(STAGE_D_RUN_ID)["status"] == "cancelled"
    assert fake.reserved_count(STAGE_D_RUN_ID) == 0


def test_zero_rows_with_no_recorded_identity_is_still_a_proved_no_run_verdict(db, monkeypatch, capsys):
    fake = wire_lost_run(db, monkeypatch, runs=[], expected_user=None, expected_conversation=None)
    assert run_terminalize(db) == 0
    verdict = terminalize_verdict(capsys)
    assert verdict["ok"] is True
    assert verdict["no_run_recorded"] is True
    assert verdict["rows_under_key"] == 0
    assert fake.mutating_calls() == []


def test_two_rows_with_no_recorded_identity_still_fail_closed_without_mutating(db, monkeypatch, capsys):
    second = stage_d_run(id="bbbb1111-2222-3333-4444-555566667777", status="running")
    fake = wire_lost_run(db, monkeypatch, runs=[stage_d_run(status="running"), second],
                         expected_user=None, expected_conversation=None)
    assert run_terminalize(db) != 0
    verdict = terminalize_verdict(capsys)
    assert "ambiguous" in " ".join(verdict["problems"])
    assert fake.mutating_calls() == []
    assert fake.run(STAGE_D_RUN_ID)["status"] == "running"


def test_the_identity_gate_names_a_missing_field_as_a_refusal(db, monkeypatch):
    """Unit: the gate itself, with nothing else wrong with the row."""
    monkeypatch.setenv("STAGE_D_GOV_CAPTURE_RUN_ID", GOV_RUN_ID)
    set_recorded_identity(monkeypatch, None, RUN_CONVERSATION)
    problems = []
    db.check_stage_d_run_identity(stage_d_run(), STAGE_D_KEY, problems)
    assert len(problems) == 1 and "STAGE_D_EXPECTED_USER_ID" in problems[0]
    set_recorded_identity(monkeypatch, RUN_USER, None)
    problems = []
    db.check_stage_d_run_identity(stage_d_run(), STAGE_D_KEY, problems)
    assert len(problems) == 1 and "STAGE_D_EXPECTED_CONVERSATION_ID" in problems[0]
    set_recorded_identity(monkeypatch, RUN_USER, RUN_CONVERSATION)
    problems = []
    db.check_stage_d_run_identity(stage_d_run(), STAGE_D_KEY, problems)
    assert problems == []


def test_the_identity_gate_precedes_every_write_in_terminalize(db):
    """Structural: the gate, and its exit, come before the first PATCH and
    before the settlement RPC."""
    source = inspect.getsource(db.terminalize)
    gate = source.index("check_stage_d_run_identity(")
    assert gate < source.index("guarded_transition(")
    assert gate < source.index('"/rest/v1/rpc/settle_model_call_budget"')
    after_gate = source[gate:gate + 160]
    assert "if problems:" in after_gate and "emit_and_exit()" in after_gate


def test_the_lockdown_hands_the_probe_only_what_state_json_recorded():
    """No fallback, no default, no operator input for the identity."""
    script = (STAGE_D / "07-post-run-lockdown.sh").read_text()
    assert 'STAGE_D_EXPECTED_USER_ID=${recorded_user_id}' in script
    assert 'STAGE_D_EXPECTED_CONVERSATION_ID=${recorded_conversation_id}' in script
    assert 'read user_id)' in script and 'read conversation_id)' in script
    assert "read -r" not in script and "read -p" not in script


# -- end to end: the lockdown, the shell's REAL argument passing, the REAL
#    probe against a real (fake-PostgREST) database -----------------------


def live_run_world(tmp_path, *, runs=None, reservations=None, state=None):
    world = StageDWorld(tmp_path, state=state)
    world.enable_execution_surface()
    world.seed_db(runs=[stage_d_run(status="running")] if runs is None else runs,
                  reservations=[reservation(1)] if reservations is None else reservations)
    return world


def real_probe_outcome(world):
    runs = world.db_probe_runs()
    assert runs, "the lockdown never executed the terminalize probe"
    return runs[-1]


LOST_IDENTITY_STATE_FILES = [
    pytest.param(None, id="state-json-missing"),
    pytest.param("{not json", id="state-json-malformed"),
    pytest.param("[1, 2, 3]", id="state-json-not-an-object"),
    pytest.param(json.dumps({"run_id": STAGE_D_RUN_ID}), id="both-identity-fields-missing"),
    pytest.param(json.dumps({"run_id": STAGE_D_RUN_ID, "user_id": RUN_USER}), id="conversation-missing"),
    pytest.param(json.dumps({"run_id": STAGE_D_RUN_ID, "conversation_id": RUN_CONVERSATION}),
                 id="user-missing"),
    pytest.param(json.dumps({"user_id": RUN_USER}), id="run-id-and-conversation-missing"),
    pytest.param(json.dumps({"run_id": STAGE_D_RUN_ID, "user_id": "", "conversation_id": ""}),
                 id="identity-fields-empty"),
]


@pytest.mark.parametrize("state_json", LOST_IDENTITY_STATE_FILES)
def test_lockdown_cannot_complete_when_the_recorded_identity_is_lost(tmp_path, state_json):
    """A live run exists; state.json cannot vouch for it. The REAL probe,
    under the shell's real argument passing, must refuse without a write,
    and the lockdown must not print COMPLETE."""
    world = live_run_world(tmp_path)
    if state_json is not None:
        (world.workdir / "state.json").write_text(state_json)
    result = world.run("07-post-run-lockdown.sh")
    assert result.returncode != 0
    assert "STAGE D LOCKDOWN COMPLETE" not in result.stdout
    assert "could not be proven terminal and clean" in result.stderr
    assert "did not yield both user_id and conversation_id" in result.stdout
    probe = real_probe_outcome(world)
    assert probe["exit"] != 0
    assert probe["mutating_calls"] == []
    assert probe["rpc_calls"] == []
    assert probe["verdict"]["ok"] is False
    assert "STAGE_D_EXPECTED_" in " ".join(probe["verdict"]["problems"])
    db = world.db()
    assert db["runs"][0]["status"] == "running"
    assert db["reservations"][0]["status"] == "reserved"
    # The rest of the lockdown still happened: posture fail-closed, probes gone.
    world.assert_fail_closed()
    world.assert_probes_absent()


def test_lockdown_terminalizes_a_recorded_run_through_the_real_probe(tmp_path):
    """Positive control: with the identity on record the same path closes the
    run, releases the reservation and prints COMPLETE."""
    world = live_run_world(tmp_path)
    (world.workdir / "state.json").write_text(json.dumps(recorded_state()))
    result = world.run("07-post-run-lockdown.sh")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "STAGE D LOCKDOWN COMPLETE" in result.stdout
    assert "did not yield both user_id and conversation_id" not in result.stdout
    probe = real_probe_outcome(world)
    assert probe["exit"] == 0 and probe["verdict"]["ok"] is True
    assert probe["verdict"]["recovered"] is False
    assert len(probe["rpc_calls"]) == 1
    db = world.db()
    assert db["runs"][0]["status"] == "cancelled"
    assert db["reservations"][0]["status"] == "released"


def test_lockdown_recovers_a_lost_run_through_the_real_probe(tmp_path):
    world = live_run_world(tmp_path)
    (world.workdir / "state.json").write_text(json.dumps(recorded_state(run_id=None)))
    result = world.run("07-post-run-lockdown.sh")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "STAGE D LOCKDOWN COMPLETE" in result.stdout
    probe = real_probe_outcome(world)
    assert probe["verdict"]["recovered"] is True
    assert probe["verdict"]["recovered_run_id"] == STAGE_D_RUN_ID
    assert world.db()["runs"][0]["status"] == "cancelled"
    assert world.db()["reservations"][0]["status"] == "released"


def test_lockdown_with_no_state_and_no_run_is_a_proved_no_run_verdict(tmp_path):
    """Zero rows under the key needs no identity: there is nothing to mutate."""
    world = live_run_world(tmp_path, runs=[], reservations=[])
    result = world.run("07-post-run-lockdown.sh")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "STAGE D LOCKDOWN COMPLETE" in result.stdout
    probe = real_probe_outcome(world)
    assert probe["verdict"]["no_run_recorded"] is True
    assert probe["verdict"]["rows_under_key"] == 0
    assert probe["mutating_calls"] == []


def test_the_real_probe_saw_exactly_the_environment_the_shell_passed(tmp_path):
    """The refusal is caused by the shell's argument passing, not by the
    mock: the env the probe received is recorded."""
    world = live_run_world(tmp_path)
    (world.workdir / "state.json").write_text("{not json")
    world.run("07-post-run-lockdown.sh")
    env = real_probe_outcome(world)["env"]
    assert env["STAGE_D_MODE"] == "terminalize"
    assert env["STAGE_D_RUN_ID"] == ""
    assert env["STAGE_D_EXPECTED_USER_ID"] == ""
    assert env["STAGE_D_EXPECTED_CONVERSATION_ID"] == ""
    assert env["STAGE_D_IDEMPOTENCY_KEY"] == STAGE_D_KEY
    assert env["STAGE_D_GOV_CAPTURE_RUN_ID"] == GOV_RUN_ID


def test_the_docs_state_the_mandatory_identity_and_the_exact_rpc_signature():
    readme = " ".join((STAGE_D / "README.md").read_text().split())
    doc = " ".join(AUTHORIZATION_DOC.read_text().split())
    for text in (readme, doc):
        assert "identity fields are mandatory" in text
        assert "before any PATCH or settlement RPC" in text
        assert "additional" in text and "exactly" in text
        assert "zero-row" in text

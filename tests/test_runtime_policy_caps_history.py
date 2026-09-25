"""The reviewed caps, checked as relationships and against their evidence.

These tests moved unchanged from tests/test_stage_d_toolkit.py section B when
the Stage D toolkit was deleted (cleanup D8). They test values in
backend/runtime_policy.py, not the toolkit: the reviewed caps as rendered
from the one canonical policy (CAPS), the evidence they were derived from
(Stage C Attempt 7), the Stage C values none may exceed without a cited
reviewed change, and the relationships between caps that budget.py relies on.
The fingerprint pin freezes today's values; these checks keep the
relationships honest the next time a value is deliberately re-pinned.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.runtime_policy import (
    CAP_ENV_PREFIXES,
    DIMENSIONS,
    reviewed_first_run_policy,
)

REPO = Path(__file__).resolve().parents[1]


# The operating envelope, READ from the ONE canonical runtime policy exactly
# as the Stage D env generated it. The Stage D tests used to carry their own
# transcription of all three groups, which made the whole suite a proof that
# one transcription matched another: it passed while the toolkit pinned
# MILO_PROVIDER_RPM_LIMIT=350 against MILO's organization ceiling of 80 --
# a posture `ProviderLimitsConfig.from_env` refuses, so no Worker could have
# started under it.
POLICY = reviewed_first_run_policy()


def _rendered(prefixes) -> str:
    return ",".join(f"{k}={v}" for k, v in POLICY.env_expectations(prefixes=prefixes).items())


CAPS = _rendered(CAP_ENV_PREFIXES)


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


def parse_pairs(raw: str) -> dict[str, str]:
    return dict(pair.split("=", 1) for pair in raw.split(","))


def stage_d_caps() -> dict[str, float]:
    return {k: float(v) for k, v in parse_pairs(CAPS).items()}


#: The ONLY caps a reviewed change has raised above Stage C, and the change
#: that did it: PR-R (MILO_V2_REASONING_BUDGET_PR_SPEC.md 4.8). Reasoning
#: tokens are output tokens, and the daily reservation now holds each call's
#: worst-case cost, so these two dimensions were re-sized deliberately. The
#: dollar ceiling that bounds a RUN (MILO_MAX_COST_PER_RUN) stays at the
#: Stage C value. Every entry must be justified on its own dimension.
PR_R_RAISED_ABOVE_STAGE_C = {
    "MILO_MAX_OUTPUT_TOKENS_PER_RUN": 400_000,
    "MILO_DAILY_USER_BUDGET": 10.00,
    "MILO_DAILY_PROJECT_BUDGET": 10.00,
}


@pytest.mark.parametrize("name,stage_c_value", sorted(STAGE_C_CAPS.items()))
def test_no_stage_d_cap_exceeds_its_stage_c_counterpart(name, stage_c_value):
    """The core promise: Stage D raises no Stage C limit -- except the ones a
    reviewed change raised by name, to exactly its value, with its reason."""
    if name in PR_R_RAISED_ABOVE_STAGE_C:
        assert stage_d_caps()[name] == PR_R_RAISED_ABOVE_STAGE_C[name]
        dimension = next(d for d in DIMENSIONS.values() if d.env_key == name)
        assert "PR-R" in dimension.why and "4.8" in dimension.why, (
            f"{name} was raised without citing the reviewed change that raised it")
        return
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
    # PR-R (spec 4.8) sets 900,000 = 500,000 + 400,000: the joint ceiling may
    # bind together with the components, never after them -- the same rule
    # `reviewed_policy_violations` enforces.
    caps = stage_d_caps()
    assert caps["MILO_MAX_TOTAL_TOKENS_PER_RUN"] <= (
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

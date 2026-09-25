"""ONE authoritative runtime policy: the proofs that it is actually the one.

Every test here answers the same question from a different direction: can two
surfaces of this repository describe different effective safety envelopes for
the same run? Before `backend/runtime_policy.py` the answer was yes, in at
least five places at once -- the first-run profile said 1 replan and 24 tool
calls while the plan firewall admitted 3 and 100; the mandatory-for-paid set
named neither the agent-step ceiling nor the recorded-cost cap the profile
advertised; and the Stage D release toolkit pinned a provider RPM the runtime
refuses to start under.

No provider call, no network, no database, no run.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from backend.budget import BudgetConfig, BudgetExceeded, BudgetTracker
from backend.engines.swarm_v2.correction import correction_allowance
from backend.engines.swarm_v2.contracts import RemainingBudget
from backend.engines.swarm_v2.feasibility import plan_worst_case
from backend.engines.swarm_v2.model_gateway import ModelGateway
from backend.engines.swarm_v2.validation import (PlanLimitError, PlanLimits, PlanValidator,
                                                 provider_plan_policy)
from backend.production_config import validate
from backend.provider_scheduler import ProviderLimitsConfig
from backend.runtime_policy import (BUDGET, CAP_ENV_PREFIXES, DIMENSIONS,
                                    ENGINE_ENV_PREFIXES, MANDATORY_FOR_PAID_EXECUTION,
                                    POLICY_DIMENSIONS, POLICY_ENV_KEYS, POLICY_SCOPE,
                                    PROVIDER_ENV_PREFIXES, RuntimePolicyError,
                                    dimensions_for, paid_posture, policy_violations,
                                    resolve_runtime_policy, resolved_dimension,
                                    reviewed_first_run_policy, reviewed_policy_violations)
from backend.tools import ToolContext, ToolMode, ToolOperation, ToolRegistry

REPO = Path(__file__).resolve().parents[1]
STAGE_D = REPO / "scripts" / "release" / "stage-d"
#: The permanent copies of policy_envelope.py and verify_caps.py (cleanup D8).
PINS = REPO / "scripts" / "release" / "pins"

POLICY = reviewed_first_run_policy()
REVIEWED_ENV = POLICY.env_expectations()

PROD_REF = "abcdefghijklmnopqrst"
BASE_PROD = {
    "ENVIRONMENT": "production",
    "SUPABASE_URL": f"https://{PROD_REF}.supabase.co",
    "MILO_EXPECTED_SUPABASE_PROJECT_REF": PROD_REF,
    "SUPABASE_SERVICE_ROLE_KEY": "placeholder-not-a-real-key",
    "ALLOWED_CORS_ORIGINS": "https://app.example.com",
    "UPSTASH_REDIS_REST_URL": "https://redis.example",
    "UPSTASH_REDIS_REST_TOKEN": "token",
    "MILO_GATEWAY_AUDIENCE": "https://milo-api.example.internal",
    "MILO_APPROVED_GATEWAY_IDENTITIES": "gateway@example.iam.gserviceaccount.com",
}
PAID_PROD = {**BASE_PROD, "MILO_ENABLE_PAID_EXECUTION": "true",
             "KIMI_API_KEY": "placeholder", **REVIEWED_ENV}


def codes(report) -> set[str]:
    return {issue.code for issue in report.errors}


# =============================================================================
# A plan fixture, built from a REAL registry so descriptors are never invented
# =============================================================================

QUERY_SCHEMA = {"type": "object", "properties": {"query": {"type": "string"}},
                "required": ["query"], "additionalProperties": False}
ROWS_SCHEMA = {"type": "object",
               "properties": {"rows": {"type": "array", "items": {"type": "string"}}},
               "required": ["rows"], "additionalProperties": False}


class FixtureTool:
    name = "search"
    description = "offline fixture tool"
    required_scope = "fixture:read"
    mode = ToolMode.READ
    operations = {"search": ToolOperation("search", "Offline fixture lookup.",
                                          QUERY_SCHEMA, ROWS_SCHEMA)}

    def execute(self, context: ToolContext, operation, payload):
        return {"rows": []}


def descriptors():
    return ToolRegistry([FixtureTool()]).descriptors()


def task(task_id: str, *, calls: int) -> dict:
    return {
        "task_id": task_id,
        "goal": f"establish a public fact for {task_id}",
        "scope": f"bounded scope for {task_id}",
        "dependencies": [],
        "tools": [{"call_id": f"c{index}", "name": "search", "operation": "search",
                   "arguments": {"query": "public facts"}, "dependency_bindings": []}
                  for index in range(calls)],
        "output_schema": {"type": "object", "properties": {"answer": {"type": "string"}},
                          "required": ["answer"], "additionalProperties": False},
        "evidence": {"minimum_sources": 1, "required_fields": ["answer"], "min_confidence": 0.5},
        "priority": 50, "recursion_depth": 0, "estimated_cost_units": 10,
        "completion": {"required_outputs": ["answer"], "evidence_satisfied": True,
                       "allow_partial": False},
    }


def plan_with(total_tool_calls: int, *, max_replans: int = 0) -> dict:
    """A well-formed plan carrying exactly ``total_tool_calls`` planned calls."""
    per_task = DIMENSIONS["max_tool_calls_per_task"].reviewed
    tasks, remaining, index = [], total_tool_calls, 0
    while remaining > 0:
        take = min(per_task, remaining)
        tasks.append(task(f"t{index}", calls=take))
        remaining -= take
        index += 1
    return {
        "version": "1", "objective": "Answer a taxonomy-neutral question",
        "graph": {"tasks": tasks},
        "assignments": [{"task_id": item["task_id"], "worker_role": "generic researcher",
                         "context_task_ids": []} for item in tasks],
        "max_replans": max_replans,
        "estimated_cost_units": sum(item["estimated_cost_units"] for item in tasks),
    }


# =============================================================================
# 1. A 24-tool-call policy accepts 24 and rejects 25
# =============================================================================

def test_the_tool_call_ceiling_accepts_exactly_the_reviewed_number():
    limits = POLICY.plan_limits()
    assert limits.max_tool_calls == 24
    validator = PlanValidator(allowed_tools=descriptors(), limits=limits)
    accepted = validator.validate(plan_with(24))
    assert sum(len(item.tools) for item in accepted.graph.tasks) == 24


def test_the_tool_call_ceiling_rejects_one_call_more():
    validator = PlanValidator(allowed_tools=descriptors(), limits=POLICY.plan_limits())
    with pytest.raises(PlanLimitError) as excinfo:
        validator.validate(plan_with(25))
    assert excinfo.value.reason == "AGGREGATE_TOOL_CALL_LIMIT"


# =============================================================================
# 2. A 1-replan policy makes two replans impossible
# =============================================================================

def test_a_plan_that_reserves_more_than_one_replan_is_rejected():
    validator = PlanValidator(allowed_tools=descriptors(), limits=POLICY.plan_limits())
    assert validator.validate(plan_with(4, max_replans=1)).max_replans == 1
    with pytest.raises(PlanLimitError) as excinfo:
        validator.validate(plan_with(4, max_replans=2))
    assert excinfo.value.reason == "REPLAN_LIMIT"


def test_a_second_replan_is_refused_at_execution_time_too():
    """The firewall bounds what a plan may RESERVE; this bounds what it SPENDS.

    A correction round is charged as a replan against the same allowance, so
    with one replan already spent there is nothing left to start one with.
    """
    remaining = RemainingBudget(cost_units=1_000, tool_calls=24, tasks=23,
                               model_calls=150, retries=15, agent_steps=56)
    first = correction_allowance(rounds_used=0, remaining=remaining,
                                 replans_used=0, max_replans=1)
    assert first.allowed
    second = correction_allowance(rounds_used=0, remaining=remaining,
                                  replans_used=1, max_replans=1)
    assert not second.allowed


def test_the_engine_charges_a_replan_against_the_plans_own_allowance():
    """Checked over the parsed code, so prose cannot pass or fail it."""
    import inspect

    from backend.engines.swarm_v2 import engine as engine_module

    source = inspect.getsource(engine_module.SwarmV2Engine)
    assert "if len(state.replans) >= plan.max_replans:" in source
    assert "replans_used=len(state.replans), max_replans=plan.max_replans" in source


# =============================================================================
# 3. 56 agent steps is enforced in the controlled paid posture
# =============================================================================

def test_the_agent_step_ceiling_is_enforced_by_the_tracker_it_configures():
    tracker = BudgetTracker(POLICY.budget_config(), kill_switch=lambda: True)
    assert tracker.config.max_agent_steps == 56
    for _ in range(56):
        tracker.record_agent_step()
    with pytest.raises(BudgetExceeded) as excinfo:
        tracker.record_agent_step()
    assert excinfo.value.code == "AGENT_STEP_LIMIT_REACHED"


def test_the_plan_ceiling_cannot_outrun_the_agent_step_ceiling():
    limits = POLICY.plan_limits()
    worst = plan_worst_case(limits.max_tasks, max_replans=limits.max_replans)
    assert worst.agent_steps <= POLICY["max_agent_steps"]
    # And one task more would not fit, so the ceiling is the real boundary.
    assert plan_worst_case(limits.max_tasks + 2,
                           max_replans=limits.max_replans).agent_steps > POLICY["max_agent_steps"]


# =============================================================================
# 4. A missing mandatory dimension rejects paid production execution
# =============================================================================

@pytest.mark.parametrize("dimension", MANDATORY_FOR_PAID_EXECUTION)
def test_every_mandatory_dimension_is_required_for_paid_production(dimension):
    env = {key: value for key, value in PAID_PROD.items()
           if key != POLICY_ENV_KEYS[dimension]}
    report = validate(env)
    assert not report.ok()
    assert "POLICY_DIMENSION_ABSENT" in codes(report) or "PAID_WITHOUT_BUDGET" in codes(report)


def test_the_whole_reviewed_envelope_is_accepted():
    report = validate(PAID_PROD)
    assert report.ok(), [issue.message for issue in report.errors]


def test_the_mandatory_set_is_derived_and_covers_what_the_profile_advertises():
    """It used to be five hand-written names, and the profile had moved on."""
    required = {POLICY_ENV_KEYS[name] for name in MANDATORY_FOR_PAID_EXECUTION}
    for advertised in ("MILO_MAX_AGENT_STEPS", "MILO_MAX_COST_PER_RUN",
                       "MILO_MAX_TASKS_PER_RUN", "MILO_MAX_TOOL_CALLS_PER_RUN",
                       "MILO_MAX_REPLANS_PER_RUN", "MILO_SWARM_MAX_ACTIVE_WORKERS"):
        assert advertised in required, f"{advertised} is advertised but not required"
    # Derived, not declared: membership is exactly "leaving it out would let
    # the runtime operate wider than the reviewed value".
    for dimension in POLICY_DIMENSIONS:
        if dimension.env_key is None:
            continue
        expected = (dimension.runtime_default is None or
                    dimension.is_wider(dimension.runtime_default))
        assert dimension.mandatory_for_paid is expected, dimension.name


def test_budget_config_takes_its_mandatory_set_from_the_policy():
    assert BudgetConfig.MANDATORY_FOR_PAID_EXECUTION == tuple(
        d.name for d in dimensions_for(BUDGET) if d.mandatory_for_paid)
    # Only the dimensions a DEPLOYMENT may set. A budget dimension with no
    # env key (the search volume and price bounds) is a reviewed value the
    # runtime enforces and a deployment does not move, so it belongs in the
    # policy and NOT in a map of environment variable names.
    assert BudgetConfig.ENV_KEYS == {d.name: d.env_key for d in dimensions_for(BUDGET)
                                     if d.env_key is not None}
    assert {d.name for d in dimensions_for(BUDGET) if d.env_key is None} == {
        "max_search_invocations_per_run", "max_builtin_searches_per_request",
        "search_cost_per_invocation"}


# =============================================================================
# 5. Provider-visible limits and deterministic limits cannot drift
# =============================================================================

def test_the_model_sees_exactly_the_limits_the_firewall_enforces():
    limits = POLICY.plan_limits()
    advertised = provider_plan_policy(limits, ["search"])["limits"]
    enforced = PlanValidator(allowed_tools=descriptors(), limits=limits).limits
    assert advertised == {
        "max_tasks": enforced.max_tasks,
        "max_graph_depth": enforced.max_graph_depth,
        "max_recursion_depth": enforced.max_recursion_depth,
        "max_replans": enforced.max_replans,
        "max_cost_units": enforced.max_cost_units,
        "max_tool_calls_per_task": enforced.max_tool_calls_per_task,
        "max_tool_calls": enforced.max_tool_calls,
    }
    # Every field of PlanLimits is advertised: a limit the firewall enforces
    # but never states is one the Commander cannot plan inside.
    assert set(advertised) == set(PlanLimits().__dataclass_fields__)


def test_the_gateway_and_the_validator_are_wired_from_the_same_object():
    """Checked over the parsed worker source, not over prose."""
    import ast

    tree = ast.parse((REPO / "backend" / "worker" / "main.py").read_text())
    names = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        for keyword in node.keywords:
            if keyword.arg in ("plan_limits", "limits") and isinstance(keyword.value, ast.Name):
                names.setdefault(node.func.id, set()).add(keyword.value.id)
    assert names.get("ModelGateway") == names.get("PlanValidator") == {"limits"}, names
    assert "limits = policy.plan_limits()" in (REPO / "backend" / "worker" / "main.py").read_text()


def test_the_gateway_derives_its_visible_policy_from_the_limits_it_is_given():
    """The model-visible contract has ONE producer, and it is the limits.

    Checked over the parsed gateway source: a second place that renders a
    limit into a prompt is a second place it can be rendered differently.
    """
    import ast
    import inspect

    source = inspect.getsource(ModelGateway.__init__)
    assert "provider_plan_policy(self._plan_limits, self._allowed_tool_names)" in source
    tree = ast.parse(inspect.getsource(ModelGateway))
    producers = [node for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                 and node.func.id == "provider_plan_policy"]
    assert len(producers) == 1, "the provider-visible policy has more than one producer"
    # And the rendered policy really carries the reviewed numbers.
    rendered = provider_plan_policy(POLICY.plan_limits(), ["search"])["limits"]
    assert (rendered["max_replans"], rendered["max_tool_calls"], rendered["max_tasks"]) == (1, 24, 23)


# =============================================================================
# 6. Stage D cannot disagree silently with the runtime
# =============================================================================

def policy_envelope(selector: str) -> str:
    result = subprocess.run([sys.executable, str(PINS / "policy_envelope.py"), selector],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.mark.parametrize("selector,prefixes", [
    ("caps", CAP_ENV_PREFIXES),
    ("provider-limits", PROVIDER_ENV_PREFIXES),
    ("engine-limits", ENGINE_ENV_PREFIXES),
])
def test_stage_d_generates_its_envelope_from_the_runtime_policy(selector, prefixes):
    rendered = dict(pair.split("=", 1) for pair in policy_envelope(selector).split(","))
    assert rendered == POLICY.env_expectations(prefixes=prefixes)


def test_stage_d_keeps_no_hand_written_copy_of_a_policy_value():
    """A literal cap assignment in the shell file is how the copies started."""
    import re

    text = (STAGE_D / "stage-d-env.sh").read_text()
    executable = [line for line in text.splitlines()
                  if line.strip() and not line.lstrip().startswith("#")]
    for line in executable:
        assert not re.search(r"MILO_(MAX|PROVIDER|DAILY|SWARM|V1|ESTIMATED)_[A-Z_]*=", line), (
            f"stage-d-env.sh assigns a policy value by hand: {line.strip()}")


def test_stage_d_refuses_a_pinned_value_that_disagrees_with_the_runtime(tmp_path):
    sys.path.insert(0, str(PINS))
    import verify_caps  # noqa: E402  (path bootstrap must run first)

    pinned = dict(POLICY.env_expectations(prefixes=PROVIDER_ENV_PREFIXES))
    problems: list[str] = []
    verify_caps.check_against_canonical_policy(
        "provider-limits", "STAGE_D_WORKER_PROVIDER_LIMITS", pinned, problems)
    assert problems == []
    # The exact historical drift: an RPM the runtime refuses to start under.
    pinned["MILO_PROVIDER_RPM_LIMIT"] = "350"
    verify_caps.check_against_canonical_policy(
        "provider-limits", "STAGE_D_WORKER_PROVIDER_LIMITS", pinned, problems)
    assert any("MILO_PROVIDER_RPM_LIMIT" in problem for problem in problems)


def test_the_reviewed_provider_envelope_is_one_the_runtime_accepts():
    """The drift that made the pinned Stage D posture unstartable."""
    ProviderLimitsConfig.from_env(POLICY.env_expectations(prefixes=PROVIDER_ENV_PREFIXES))
    assert reviewed_policy_violations() == []
    with pytest.raises(ValueError):
        ProviderLimitsConfig.from_env({"MILO_PROVIDER_RPM_LIMIT": "350"})


def test_stage_d_binds_the_policy_by_content_not_by_checkout():
    """Re-authorization must be possible: a reviewed commit that pins release
    R cannot itself be R, so the binding is a statement about the policy
    CONTENT at R, never about which commit is checked out."""
    import inspect
    import sys as _sys

    _sys.path.insert(0, str(PINS))
    import policy_envelope

    source = inspect.getsource(policy_envelope.release_binding_problems)
    assert "HEAD" not in source
    assert policy_envelope.POLICY_SOURCE_PATH == "backend/runtime_policy.py"
    # And it is the file the policy in use was really imported from.
    assert policy_envelope._imported_policy_source() == (
        REPO / "backend" / "runtime_policy.py").resolve()


def test_the_reviewed_first_run_plan_shape_is_pinned():
    # Formerly asserted through backend/tier2_profile.py, which only re-read
    # these values; asserted on the policy itself now.
    assert POLICY["max_tasks"] == 23
    assert POLICY["max_agent_steps"] == 56
    assert POLICY["max_tool_calls"] == 24
    assert POLICY["max_replans"] == 1


def test_the_policy_fingerprint_is_deterministic():
    assert reviewed_first_run_policy().fingerprint() == POLICY.fingerprint()
    assert policy_envelope("fingerprint") == POLICY.fingerprint()


# =============================================================================
# 7. A contradictory catalog posture fails configuration validation
# =============================================================================

def test_promotion_without_government_read_fails_config_validation():
    report = validate({**BASE_PROD,
                       "MILO_ENABLE_CATALOG_EXECUTION": "true",
                       "MILO_ENABLE_GOVERNMENT_CATALOG_READ": "false",
                       "MILO_ENABLE_CATALOG_PROMOTION": "true"})
    assert "POLICY_CATALOG_POSTURE_CONTRADICTORY" in codes(report)
    assert not report.ok()


def test_promotion_without_read_is_refused_even_behind_the_kill_switch():
    """The master switch masking a contradiction does not make it coherent."""
    report = validate({**BASE_PROD,
                       "MILO_ENABLE_CATALOG_EXECUTION": "false",
                       "MILO_ENABLE_GOVERNMENT_CATALOG_READ": "false",
                       "MILO_ENABLE_CATALOG_PROMOTION": "true"})
    assert "POLICY_CATALOG_POSTURE_CONTRADICTORY" in codes(report)


def test_resolving_a_policy_never_asks_whether_the_catalog_is_on():
    """The V1 engine path must not consult the catalog switch, and the policy
    is resolved on EVERY run -- so the policy must not consult it either.

    It owns which catalog combinations are LEGAL. What the posture IS stays
    with `backend.catalog.execution`, which only the Swarm V2 factory asks.
    """
    import backend.catalog.execution as catalog_execution

    asked: list[str] = []
    original = catalog_execution.catalog_execution_enabled
    catalog_execution.catalog_execution_enabled = (
        lambda *a, **k: asked.append("read") or False)
    try:
        policy = resolve_runtime_policy({**REVIEWED_ENV,
                                         "MILO_ENABLE_PAID_EXECUTION": "true",
                                         "MILO_ENABLE_CATALOG_EXECUTION": "true"})
    finally:
        catalog_execution.catalog_execution_enabled = original
    assert asked == []
    assert policy.reviewed_catalog_posture == {"master": False, "government_read": False,
                                               "promotion": False}


def test_read_without_promotion_is_a_legitimate_posture():
    report = validate({**BASE_PROD,
                       "MILO_ENABLE_CATALOG_EXECUTION": "true",
                       "MILO_ENABLE_GOVERNMENT_CATALOG_READ": "true",
                       "MILO_ENABLE_CATALOG_PROMOTION": "false"})
    assert "POLICY_CATALOG_POSTURE_CONTRADICTORY" not in codes(report)
    assert report.ok(), [issue.message for issue in report.errors]


# =============================================================================
# 8. Generic defaults cannot widen the first-run policy
# =============================================================================

def test_the_generic_plan_defaults_are_wider_than_the_reviewed_policy():
    """Stated explicitly, because this is the gap that shipped."""
    defaults = PlanLimits()
    assert defaults.max_tasks == 64 > POLICY["max_tasks"]
    assert defaults.max_replans == 3 > POLICY["max_replans"]
    assert defaults.max_tool_calls == 100 > POLICY["max_tool_calls"]


def test_a_paid_deployment_cannot_inherit_a_generic_default():
    for dimension in POLICY_DIMENSIONS:
        if dimension.env_key is None or not dimension.mandatory_for_paid:
            continue
        env = {key: value for key, value in REVIEWED_ENV.items()
               if key != dimension.env_key}
        env["MILO_ENABLE_PAID_EXECUTION"] = "true"
        with pytest.raises(RuntimePolicyError) as excinfo:
            resolve_runtime_policy(env)
        assert "POLICY_DIMENSION_ABSENT" in excinfo.value.codes, dimension.name


def test_the_resolved_paid_plan_ceiling_is_the_reviewed_shape_not_the_default():
    policy = resolve_runtime_policy({**REVIEWED_ENV, "MILO_ENABLE_PAID_EXECUTION": "true"})
    assert policy.plan_limits() == PlanLimits(
        max_tasks=23, max_graph_depth=12, max_recursion_depth=4, max_replans=1,
        max_cost_units=100_000, max_tool_calls_per_task=4, max_tool_calls=24)


def test_an_unpaid_deployment_is_left_exactly_as_it_was():
    """The reviewed envelope binds where money can be spent, and only there.

    An unpaid run cannot make a provider call at all -- `BudgetTracker`'s kill
    switch refuses every one -- so retroactively imposing a first-run policy
    on a development stack would narrow something without protecting anything.
    """
    policy = resolve_runtime_policy({})
    assert policy.paid is False
    assert policy.budget_config() == BudgetConfig.from_env({})
    assert policy.plan_limits() == PlanLimits()


STAGING_ENV = {
    "MILO_MAX_MODEL_CALLS_PER_RUN": "25", "MILO_MAX_INPUT_TOKENS_PER_RUN": "100000",
    "MILO_MAX_OUTPUT_TOKENS_PER_RUN": "50000", "MILO_MAX_TOTAL_TOKENS_PER_RUN": "120000",
    "MILO_MAX_ESTIMATED_COST_PER_RUN": "1.0", "MILO_MAX_COST_PER_RUN": "1.0",
    "MILO_MAX_RUN_DURATION_SECONDS": "600", "MILO_MAX_RETRIES": "3",
    "MILO_MAX_AGENT_STEPS": "50", "MILO_DAILY_USER_BUDGET": "5.0",
    "MILO_DAILY_PROJECT_BUDGET": "5.0", "MILO_ESTIMATED_COST_PER_CALL": "0.001",
}


def test_an_unpaid_stack_may_exceed_a_reviewed_value_but_never_silently():
    """The real `scripts/deploy/staging-cloud-run.sh` defaults, exactly.

    Staging runs the zero-cost mock engine with paid execution pinned off, so
    its $5.00 daily budget and $0.001 per-call reservation rate cost nothing.
    Refusing them would narrow a stack that cannot spend and break a working
    deployment; they are RECORDED instead, and the same configuration is
    refused the moment paid execution is armed.
    """
    policy = resolve_runtime_policy(STAGING_ENV)
    assert policy.paid is False
    # PR-R raised the reviewed daily budgets to 10.00, so staging's $5.00 is
    # now a TIGHTENING; only the per-call reservation rate remains relaxed.
    assert set(policy.relaxed) == {"estimated_cost_per_call"}
    assert policy.document()["relaxed_by_unpaid_deployment"] == sorted(policy.relaxed)


def test_arming_paid_execution_on_that_same_stack_is_a_refusal():
    with pytest.raises(RuntimePolicyError) as excinfo:
        resolve_runtime_policy({**STAGING_ENV, "MILO_ENABLE_PAID_EXECUTION": "true"})
    assert "POLICY_WIDER_THAN_REVIEWED" in excinfo.value.codes


@pytest.mark.parametrize("value,expected", [
    ("true", True), ("1", True), ("on", True), ("yes", True), ("TRUE", True),
    ("false", False), ("", False), ("maybe", False), ("0", False),
])
def test_the_paid_reading_matches_the_budget_modules_own_kill_switch(
        monkeypatch, value, expected):
    """One spelling of "paid is armed", shared with the tracker's kill switch.

    A policy that thought a deployment was unpaid while `BudgetTracker` let
    calls through would be a policy that binds nothing.
    """
    from backend.budget import paid_execution_enabled

    assert paid_posture({"MILO_ENABLE_PAID_EXECUTION": value}) is expected
    monkeypatch.setenv("MILO_ENABLE_PAID_EXECUTION", value)
    assert paid_execution_enabled() is expected


# =============================================================================
# 9. Tighter deployment values remain legal
# =============================================================================

@pytest.mark.parametrize("dimension", [d for d in POLICY_DIMENSIONS if d.env_key])
def test_a_tighter_deployment_value_is_accepted(dimension):
    tighter = _tighter_than(dimension)
    if tighter is None:
        pytest.skip(f"{dimension.name} has no representable tighter value")
    env = {**REVIEWED_ENV, "MILO_ENABLE_PAID_EXECUTION": "true",
           dimension.env_key: tighter}
    violations = [v for v in policy_violations(env, paid=True)
                  if v.code == "POLICY_WIDER_THAN_REVIEWED"]
    assert violations == [], f"tightening {dimension.env_key} was refused"


def _tighter_than(dimension) -> str | None:
    from backend.runtime_policy import DECLARED_NOT_BOUNDED, LOWER_IS_TIGHTER

    if dimension.direction == DECLARED_NOT_BOUNDED:
        return None
    if dimension.direction == LOWER_IS_TIGHTER:
        if dimension.kind is int:
            return None if dimension.reviewed <= 1 else str(int(dimension.reviewed) - 1)
        return dimension.format(float(dimension.reviewed) / 2)
    return dimension.format(float(dimension.reviewed) * 2)


def test_a_tighter_run_budget_still_produces_a_smaller_plan_ceiling():
    """Tightening is honoured, not merely tolerated."""
    policy = resolve_runtime_policy({**REVIEWED_ENV, "MILO_ENABLE_PAID_EXECUTION": "true",
                                     "MILO_MAX_AGENT_STEPS": "20"})
    assert policy.plan_limits().max_tasks < POLICY["max_tasks"]
    assert plan_worst_case(policy.plan_limits().max_tasks,
                           max_replans=policy.plan_limits().max_replans).agent_steps <= 20


# =============================================================================
# 10. Wider deployment values fail closed
# =============================================================================

@pytest.mark.parametrize("dimension", [d for d in POLICY_DIMENSIONS if d.env_key])
def test_a_wider_deployment_value_fails_closed(dimension):
    wider = _wider_than(dimension)
    if wider is None:
        pytest.skip(f"{dimension.name} is declared without a widening direction")
    env = {**REVIEWED_ENV, "MILO_ENABLE_PAID_EXECUTION": "true", dimension.env_key: wider}
    with pytest.raises(RuntimePolicyError) as excinfo:
        resolve_runtime_policy(env)
    assert "POLICY_WIDER_THAN_REVIEWED" in excinfo.value.codes


def _wider_than(dimension) -> str | None:
    from backend.runtime_policy import DECLARED_NOT_BOUNDED, LOWER_IS_TIGHTER

    if dimension.direction == DECLARED_NOT_BOUNDED:
        return None
    if dimension.direction == LOWER_IS_TIGHTER:
        return dimension.format(float(dimension.reviewed) * 2)
    return dimension.format(float(dimension.reviewed) / 2)


@pytest.mark.parametrize("bad", ["0", "-1", "not-a-number", "unlimited", "1e400"])
def test_an_unparseable_or_non_positive_value_fails_closed(bad):
    env = {**REVIEWED_ENV, "MILO_ENABLE_PAID_EXECUTION": "true",
           "MILO_MAX_MODEL_CALLS_PER_RUN": bad}
    with pytest.raises(RuntimePolicyError) as excinfo:
        resolve_runtime_policy(env)
    assert "POLICY_VALUE_INVALID" in excinfo.value.codes
    assert "not shown" in " ".join(v.message for v in excinfo.value.violations)


@pytest.mark.parametrize("blank", ["", " ", "\t"])
def test_a_blank_value_is_absent_rather_than_permissive(blank):
    """Empty-after-strip is how a cap silently becomes unlimited."""
    env = {**REVIEWED_ENV, "MILO_ENABLE_PAID_EXECUTION": "true",
           "MILO_MAX_MODEL_CALLS_PER_RUN": blank}
    with pytest.raises(RuntimePolicyError) as excinfo:
        resolve_runtime_policy(env)
    assert "POLICY_DIMENSION_ABSENT" in excinfo.value.codes


def test_a_refusal_never_echoes_the_configured_value():
    env = {**REVIEWED_ENV, "MILO_ENABLE_PAID_EXECUTION": "true",
           "MILO_MAX_COST_PER_RUN": "987654.32"}
    with pytest.raises(RuntimePolicyError) as excinfo:
        resolve_runtime_policy(env)
    rendered = str(excinfo.value) + " ".join(v.message for v in excinfo.value.violations)
    assert "987654.32" not in rendered


def test_more_money_than_the_hard_cap_authorizes_fails_closed():
    env = {**REVIEWED_ENV, "MILO_ENABLE_PAID_EXECUTION": "true",
           "MILO_MAX_COST_PER_RUN": "5.00"}
    with pytest.raises(RuntimePolicyError) as excinfo:
        resolve_runtime_policy(env)
    assert "POLICY_WIDER_THAN_REVIEWED" in excinfo.value.codes


def test_a_run_duration_the_worker_process_cannot_survive_fails_closed():
    from backend.runtime_policy import WORKER_JOB_TIMEOUT_SECONDS

    violations = [v for v in policy_violations(
        {**REVIEWED_ENV, "MILO_ENABLE_PAID_EXECUTION": "true",
         "MILO_MAX_RUN_DURATION_SECONDS": str(WORKER_JOB_TIMEOUT_SECONDS)}, paid=True)]
    assert any(v.code in ("POLICY_INVARIANT_VIOLATED", "POLICY_WIDER_THAN_REVIEWED")
               for v in violations)


# =============================================================================
# The policy is DEPLOYMENT-scoped, and says so rather than implying otherwise
# =============================================================================

def test_the_policy_carries_no_engine_scope_metadata():
    """`applies_to` was serialized and never consulted.

    One worker image serves both engines and the environment is per-deployment,
    so a job carrying a wider Swarm V2 width is misconfigured even while it
    happens to be executing a V1 run: the next run on the same job may be V2.
    Metadata that looked like it scoped validation, while validation ignored
    it, was worse than no metadata at all.
    """
    assert POLICY_SCOPE == "deployment"
    for dimension in POLICY_DIMENSIONS:
        assert not hasattr(dimension, "applies_to"), dimension.name
    document = POLICY.document()
    assert document["scope"] == "deployment"
    assert "engine" not in document
    for rendered in document["dimensions"].values():
        assert "applies_to" not in rendered


def test_resolution_takes_no_engine_and_validates_every_dimension():
    """The mandatory set is engine-independent, in the signature and in fact."""
    import inspect

    for function in (resolve_runtime_policy, policy_violations,
                     reviewed_first_run_policy):
        assert "engine" not in inspect.signature(function).parameters, function.__name__
    assert not hasattr(POLICY, "engine")
    # A V1-only deployment is still refused for a wider V2 width, because the
    # same job runs both engines.
    env = {**REVIEWED_ENV, "MILO_ENABLE_PAID_EXECUTION": "true",
           "MILO_SWARM_MAX_ACTIVE_WORKERS": "8"}
    with pytest.raises(RuntimePolicyError) as excinfo:
        resolve_runtime_policy(env)
    assert "POLICY_WIDER_THAN_REVIEWED" in excinfo.value.codes
    # ...and symmetrically for a V1 dimension.
    env = {**REVIEWED_ENV, "MILO_ENABLE_PAID_EXECUTION": "true",
           "MILO_V1_TECHNICAL_PARALLELISM": "8"}
    with pytest.raises(RuntimePolicyError) as excinfo:
        resolve_runtime_policy(env)
    assert "POLICY_WIDER_THAN_REVIEWED" in excinfo.value.codes


# =============================================================================
# V1's ACTUAL width is the resolved policy's, in both postures
# =============================================================================

@pytest.mark.parametrize("configured,paid,expected", [
    (None, False, 1),      # nothing set: the engine's own default, unchanged
    (None, True, 1),
    ("2", False, 2),       # tighter than reviewed, honoured
    ("2", True, 2),
    ("4", True, 4),        # exactly the reviewed value
    ("8", False, 8),       # unpaid: the policy RECORDS the relaxation, so the
                           # engine must really run at that width
    ("8", True, 4),        # paid: the policy refuses it; the run never starts,
                           # and a direct call never exceeds the reviewed value
])
def test_v1_parallelism_is_exactly_what_the_policy_resolved(configured, paid, expected):
    """It used to clamp unconditionally to the reviewed 4.

    That made the engine disagree with the policy in the unpaid posture: the
    policy recorded a wider unpaid value (an unpaid run cannot spend, so
    nothing was protected by narrowing it) while this function silently ran
    the phase at a different width.
    """
    from backend.engines.vehicle_catalog_v1 import core

    env = {}
    if configured is not None:
        env["MILO_V1_TECHNICAL_PARALLELISM"] = configured
    if paid:
        env["MILO_ENABLE_PAID_EXECUTION"] = "true"
    assert core.technical_parallelism(env) == expected
    assert core.technical_parallelism(env) == resolved_dimension(
        "v1_technical_parallelism", env)


def test_v1_keeps_its_own_structural_bound_above_the_policy():
    """A local refusal stricter than the policy is always allowed."""
    from backend.engines.vehicle_catalog_v1 import core

    with pytest.raises(ValueError, match="between 1 and 32"):
        core.technical_parallelism({"MILO_V1_TECHNICAL_PARALLELISM": "64"})
    with pytest.raises(ValueError, match="must be an integer"):
        core.technical_parallelism({"MILO_V1_TECHNICAL_PARALLELISM": "wide"})


def test_reading_one_dimension_uses_the_same_rules_as_the_whole_policy():
    """`resolved_dimension` must not be a second, softer resolver."""
    env = {**REVIEWED_ENV, "MILO_MAX_AGENT_STEPS": "20"}
    policy = resolve_runtime_policy(env)
    for name in ("max_agent_steps", "v1_technical_parallelism", "v2_max_active_workers"):
        assert resolved_dimension(name, env) == policy.values[name], name
    # Non-raising even when the whole policy would be refused: refusing the
    # run is the worker's job, and it has already done it by this point.
    broken = {"MILO_ENABLE_PAID_EXECUTION": "true"}
    with pytest.raises(RuntimePolicyError):
        resolve_runtime_policy(broken)
    assert resolved_dimension("v1_technical_parallelism", broken) == 1


# =============================================================================
# The execution increment has ONE authority
# =============================================================================

def test_the_execution_increment_is_the_policys_and_nothing_elses():
    """Stage D used to compute `baseline + 1` in shell arithmetic."""
    import sys as _sys

    _sys.path.insert(0, str(PINS))
    import policy_envelope

    assert policy_envelope.authorized_execution_increment() == int(
        POLICY["first_paid_run_execution_cap"]) == 1
    collect = (STAGE_D / "06-collect-evidence.sh").read_text()
    assert "STAGE_D_AUTHORIZED_EXECUTION_INCREMENT" in collect
    assert "STAGE_D_EXPECTED_PRIOR_EXECUTIONS + 1" not in collect


# =============================================================================
# The reviewed policy is pinned as a literal, so a checkout cannot drift
# =============================================================================

def test_the_reviewed_policy_fingerprint_is_pinned_for_the_release():
    import sys as _sys

    _sys.path.insert(0, str(PINS))
    import policy_envelope

    assert policy_envelope.PINNED_POLICY_FINGERPRINT == POLICY.fingerprint(), (
        "backend/runtime_policy.py changed without re-pinning "
        "PINNED_POLICY_FINGERPRINT in scripts/release/pins/policy_envelope.py")


def test_a_sub_cent_price_survives_the_canonical_document():
    """A formatter must not round a real price away to nothing.

    `search_cost_per_invocation` is $0.003. Under the two-decimal money
    format it printed as "0.00": the canonical document published a price of
    ZERO while the runtime charged three tenths of a cent and the prose beside
    it said $0.003. The document is generated precisely so the two cannot
    disagree, and a formatter that loses the value puts them back in
    disagreement silently.
    """
    dimension = DIMENSIONS["search_cost_per_invocation"]
    published = POLICY.document()["dimensions"]["search_cost_per_invocation"]
    value = POLICY.values["search_cost_per_invocation"]

    assert value > 0, "the reviewed search price is no longer positive"
    assert float(published["value"]) == pytest.approx(value), (
        f"the document publishes {published['value']!r} for a price of {value!r}")
    assert float(published["reviewed"]) == pytest.approx(dimension.reviewed)
    assert published["value"] != "0.00", "a real price was published as zero"


def test_raising_a_sub_cent_price_moves_the_fingerprint():
    """THE property the fingerprint exists for, at sub-cent resolution.

    Two surfaces printing the same fingerprint are supposed to be provably
    talking about the same policy. While the price was rendered to two
    decimals, raising it from 0.000 to 0.003 left the digest untouched -- so
    Stage D's pin still passed, and the release binding could not tell that
    the money a run may spend had changed.
    """
    dimension = DIMENSIONS["search_cost_per_invocation"]
    cheaper = replace(dimension, reviewed=0.0005)
    dearer = replace(dimension, reviewed=0.0030)
    assert cheaper.reviewed_text != dearer.reviewed_text, (
        "two different sub-cent prices share one canonical spelling, so a "
        "price change cannot move the policy fingerprint")

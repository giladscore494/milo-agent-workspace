"""Feasibility, retry semantics, truthful outcomes, export and the Tier 2 profile.

All deterministic and local: no provider call, no network, no database.
"""

from __future__ import annotations

import pytest

from backend.engines.swarm_v2.contracts import RemainingBudget
from backend.engines.swarm_v2.feasibility import (MINIMUM_VIABLE_MODEL_CALLS,
                                                  envelope_supports_a_run,
                                                  plan_worst_case)
from backend.engines.swarm_v2.outcome import (USEFUL_TERMINAL_OUTCOMES, is_useful_outcome)
from backend.engines.swarm_v2.validation import PlanLimits
from backend.export_envelope import (SCHEMA_VERSION, ExportRefused, build_export_envelope,
                                     validate_export_envelope)
from backend.provider_scheduler import (ENGINE_OVERLOADED, EXCEEDED_CURRENT_QUOTA,
                                        RATE_LIMIT_REACHED, SEARCH_RATE_LIMITED,
                                        classify_provider_error, rate_limit_headers)


# =============================================================================
# C. feasibility counts what a run can really cost
# =============================================================================

def test_the_worst_case_counts_repairs_replans_verification_and_correction():
    """The old gate required `len(pending) + 2`, which counted none of these."""
    worst = plan_worst_case(8, max_replans=3)
    assert worst.model_calls > 8 + 2
    # one call + one repair per task, 3+1 commander decisions, >=1 verifier
    # batch, the correction round, and the commander plan repair.
    assert worst.model_calls == 8 + 8 + 4 + 1 + 3 + 1
    assert worst.agent_steps == worst.model_calls
    assert worst.retries == 8 + 1


def test_a_54_task_plan_cannot_fit_a_56_agent_step_envelope():
    """The exact production shape: PlanLimits() admitted 64 tasks into this."""
    worst = plan_worst_case(54, max_replans=3)
    assert worst.agent_steps > 56


def test_provider_attempts_are_counted_separately_from_semantic_retries():
    """A 429 retry costs organization RPM even though it is not a retry."""
    worst = plan_worst_case(4, max_replans=1, max_rate_limit_retries=5)
    assert worst.provider_attempts == worst.model_calls * 6
    assert worst.retries < worst.provider_attempts


def test_retries_are_reported_but_are_not_an_admission_precondition():
    """Calls and steps are spent unconditionally; retries only on failure.

    Demanding the retry worst case up front would refuse every plan on a
    healthy deployment -- 23 tasks can need 24 repairs against a configured
    allowance of 15 -- so the retry limiter stays its own fail-closed gate and
    this number is for capacity planning.
    """
    import inspect

    from backend.engines.swarm_v2 import engine as engine_module

    check = inspect.getsource(engine_module.SwarmV2Engine._check_feasible)
    assert "worst.model_calls > remaining.model_calls" in check
    assert "worst.agent_steps > remaining.agent_steps" in check
    assert "worst.retries > remaining.retries" not in check
    assert plan_worst_case(23, max_replans=3).retries > 15


def test_plan_limits_shrink_to_what_the_envelope_can_pay_for():
    derived = PlanLimits.from_envelope(max_agent_steps=56, max_model_calls=150)
    assert derived.max_tasks < PlanLimits().max_tasks
    assert plan_worst_case(derived.max_tasks,
                           max_replans=derived.max_replans).agent_steps <= 56


def test_deriving_plan_limits_never_widens_anything():
    base = PlanLimits()
    derived = PlanLimits.from_envelope(max_agent_steps=10_000, max_model_calls=10_000)
    assert derived.max_tasks <= base.max_tasks
    assert derived.max_tool_calls <= base.max_tool_calls
    assert PlanLimits.from_envelope(max_tool_calls=5).max_tool_calls == 5


def test_an_envelope_that_cannot_pay_for_any_plan_is_refused_before_planning():
    for calls in range(MINIMUM_VIABLE_MODEL_CALLS):
        assert not envelope_supports_a_run(
            RemainingBudget(cost_units=1_000, tool_calls=10, tasks=10,
                            model_calls=calls, agent_steps=1_000))
    assert envelope_supports_a_run(
        RemainingBudget(cost_units=1_000, tool_calls=10, tasks=10,
                        model_calls=MINIMUM_VIABLE_MODEL_CALLS, agent_steps=1_000))


def test_agent_steps_are_a_first_class_remaining_budget_dimension():
    assert "agent_steps" in RemainingBudget.model_fields
    assert not envelope_supports_a_run(
        RemainingBudget(cost_units=1_000, tool_calls=10, tasks=10,
                        model_calls=1_000, agent_steps=1))


# =============================================================================
# retry semantics and the distinct Kimi 429 classes
# =============================================================================

@pytest.mark.parametrize("text,expected", [
    ("rate_limit_reached_error: slow down", RATE_LIMIT_REACHED),
    ("engine_overloaded_error", ENGINE_OVERLOADED),
    ("exceeded_current_quota_error: no balance", EXCEEDED_CURRENT_QUOTA),
    ("project qps limit exceeded", SEARCH_RATE_LIMITED),
    ("Error code: 429", RATE_LIMIT_REACHED),
    ("max organization concurrency reached", RATE_LIMIT_REACHED),
    ("a perfectly ordinary bug", None),
])
def test_each_kimi_failure_class_is_named_rather_than_collapsed(text, expected):
    """They demand different responses, so one boolean is not enough.

    Collapsing `exceeded_current_quota_error` into "rate limited" is how a hard
    quota exhaustion becomes a retry storm that cannot possibly succeed.
    """
    assert classify_provider_error(RuntimeError(text)) == expected


def test_rate_limit_headers_are_read_but_can_only_slow_milo_down():
    class Response:
        headers = {"X-RateLimit-Limit": "100", "X-RateLimit-Remaining": "0",
                   "X-RateLimit-Reset": "3"}

    class Error(Exception):
        response = Response()

    assert rate_limit_headers(Error()) == {"limit": 100, "remaining": 0, "reset": 3}
    assert rate_limit_headers(RuntimeError("no headers")) == {}


def test_hidden_sdk_retries_are_disabled_in_every_client_construction():
    """The SDK default of two retries turns one request into three attempts.

    Those attempts spend organization RPM and concurrency while MILO's
    scheduler, budget and distributed limiter see nothing, so the account can
    be over its ceiling with every MILO counter reading clean.
    """
    import ast
    import inspect

    from backend import budget as budget_module
    from backend.engines.vehicle_catalog_v1 import core as v1_core

    for module in (budget_module, v1_core):
        tree = ast.parse(inspect.getsource(module))
        constructions = [node for node in ast.walk(tree)
                         if isinstance(node, ast.Call)
                         and isinstance(node.func, ast.Name) and node.func.id == "OpenAI"]
        assert constructions, f"no OpenAI client construction found in {module.__name__}"
        for call in constructions:
            retries = {kw.arg: kw.value for kw in call.keywords}.get("max_retries")
            assert isinstance(retries, ast.Constant) and retries.value == 0, (
                f"{module.__name__} builds an OpenAI client without max_retries=0")


# =============================================================================
# E. truthful terminal outcomes
# =============================================================================

def test_partial_success_with_a_real_result_counts_as_useful():
    """Acceptance written as `status == "completed"` rejects the normal case."""
    assert is_useful_outcome("completed", "usable_result")
    assert is_useful_outcome("partial_success", "usable_result")
    assert is_useful_outcome("partial_success", "partial_result")


@pytest.mark.parametrize("status,kind", [
    ("partial_success", "no_usable_result"),
    ("timed_out", "partial_result"),
    ("cancelled", "usable_result"),
    ("failed", "usable_result"),
    ("budget_exhausted", "partial_result"),
])
def test_an_empty_or_non_product_outcome_is_never_useful(status, kind):
    assert not is_useful_outcome(status, kind)


def test_the_useful_set_excludes_every_non_product_terminal_state():
    statuses = {status for status, _ in USEFUL_TERMINAL_OUTCOMES}
    assert statuses == {"completed", "partial_success"}


# =============================================================================
# F. the export envelope
# =============================================================================

def v2_run(status="partial_success", **over):
    run = {"id": "11111111-1111-1111-1111-111111111111",
           "status": status,
           "input": {"workflow_key": "swarm_v2"},
           "output": {"status": "partial_success", "result_kind": "partial_result",
                      "fields": {"engine": [{"value": "1.6T",
                                             "provenance": {"claim_id": "c1",
                                                            "source_id": "gov:1",
                                                            "run_id": "r", "task_id": "t",
                                                            "scope": {}}}]},
                      "needs_review": [{"field": "power", "value": None,
                                        "reason": "UNVERIFIED", "provenance": {}}]},
           "usage": {"model_calls": 3}}
    run.update(over)
    return run


def test_a_v2_envelope_carries_the_validated_output_unchanged():
    envelope = build_export_envelope(v2_run())
    validate_export_envelope(envelope)
    assert envelope["schema_version"] == SCHEMA_VERSION
    assert envelope["engine"] == "swarm_v2"
    assert envelope["terminal_status"] == "partial_success"
    assert envelope["result_kind"] == "partial_result"
    assert envelope["result"] == v2_run()["output"]
    assert envelope["government_provenance"]["source_ids"] == ["gov:1"]
    assert envelope["government_provenance"]["present"] is True


def test_a_v2_payload_the_contract_could_not_have_produced_is_refused():
    """Exporting it would launder an invalid result into an official document."""
    broken = v2_run()
    broken["output"] = {"status": "complete", "result_kind": "usable_result",
                        "fields": {}, "needs_review": []}
    with pytest.raises(ExportRefused):
        build_export_envelope(broken)


def test_a_v1_result_is_wrapped_without_model_post_processing():
    run = {"id": "22222222-2222-2222-2222-222222222222", "status": "completed",
           "input": {"workflow_key": "vehicle_catalog_v1"},
           "output": {"status": "complete", "models": [{"canonical_model_name": "X"}]},
           "usage": {}}
    envelope = build_export_envelope(run)
    validate_export_envelope(envelope)
    assert envelope["engine"] == "vehicle_catalog_v1"
    assert envelope["result_kind"] == "usable_result"
    assert envelope["result"] is run["output"], "the V1 result was rewritten"


def test_a_v1_partial_result_is_classified_truthfully():
    run = {"id": "3", "status": "partial_success",
           "input": {"workflow_key": "vehicle_catalog_v1"},
           "output": {"status": "partial_success", "models": [{"a": 1}]}}
    assert build_export_envelope(run)["result_kind"] == "partial_result"


@pytest.mark.parametrize("status", ["timed_out", "cancelled", "failed", "budget_exhausted"])
def test_a_non_product_terminal_state_exports_without_claiming_a_result(status):
    """A Cloud Run process exiting is not a product result, and neither is a
    timeout: the envelope must not let one be read as the other."""
    run = {"id": "4", "status": status, "input": {"workflow_key": "swarm_v2"},
           "output": None, "error": {"code": "RUN_DURATION_EXCEEDED"}}
    envelope = build_export_envelope(run)
    validate_export_envelope(envelope)
    assert envelope["result_kind"] is None
    assert envelope["error"]["code"] == "RUN_DURATION_EXCEEDED"
    assert envelope["government_provenance"]["present"] is False


def test_a_non_terminal_run_cannot_be_exported():
    with pytest.raises(ExportRefused):
        build_export_envelope({"id": "5", "status": "running"})


def test_validation_rejects_a_result_kind_on_a_non_product_status():
    envelope = build_export_envelope({"id": "6", "status": "timed_out",
                                      "input": {"workflow_key": "swarm_v2"}})
    envelope["result_kind"] = "usable_result"
    with pytest.raises(ExportRefused, match="non-product terminal status"):
        validate_export_envelope(envelope)


def test_the_envelope_adds_no_route_and_imports_nothing():
    """Checked over the parsed code, so prose cannot pass or fail it."""
    import ast
    import inspect

    from backend import export_envelope

    tree = ast.parse(inspect.getsource(export_envelope))
    imported = {node.module for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module}
    imported |= {alias.name for node in ast.walk(tree)
                 if isinstance(node, ast.Import) for alias in node.names}
    forbidden = {"fastapi", "httpx", "requests", "supabase", "openai"}
    assert not (imported & forbidden), f"the export module imports {imported & forbidden}"
    assert not any(name.startswith("backend.catalog.")
                   for name in imported), "the export module reaches into the catalog"
    # No decorator anywhere -- a route would need one.
    assert not [node for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.decorator_list]


# =============================================================================
# G. the Tier 2 first-run profile
# =============================================================================

def test_the_profile_states_both_the_ceiling_and_the_lower_active_profile():
    from backend.tier2_profile import tier2_first_run_profile

    profile = tier2_first_run_profile()
    ceiling = profile["milo_organization_ceiling"]
    active = profile["active_profile"]
    assert (ceiling["inference_concurrency"], ceiling["rpm"], ceiling["tpm"]) == \
        (32, 80, 2_400_000)
    assert profile["provider_limits"] == {
        "inference_concurrency": 40, "rpm": 100, "tpm": 3_000_000, "tpd": "Unlimited"}
    # The active profile is deliberately well below the ceiling.
    assert active["rpm"] < ceiling["rpm"]
    assert active["tpm"] < ceiling["tpm"]
    assert active["engine_active_concurrency"]["swarm_v2_max_active_workers"] < \
        ceiling["inference_concurrency"]


def test_the_profile_caps_the_first_paid_run_at_one_worker_execution():
    from backend.tier2_profile import tier2_first_run_profile

    active = tier2_first_run_profile()["active_profile"]
    assert active["first_paid_run_execution_cap"] == 1
    assert active["max_simultaneous_provider_using_worker_executions"] == 1
    assert active["no_automatic_relaunch_after_terminal_failure"] is True


def test_the_profile_keeps_the_hard_money_cap_under_ten_dollars():
    from backend.tier2_profile import tier2_first_run_profile

    active = tier2_first_run_profile()["active_profile"]
    assert active["hard_monetary_cap_usd"] < 10
    assert active["hard_monetary_cap_usd"] == 3.00
    assert active["recorded_cost_cap_usd"] <= active["hard_monetary_cap_usd"]


def test_the_profile_does_not_raise_the_duration_limit_after_the_timeout():
    from backend.tier2_profile import tier2_first_run_profile

    profile = tier2_first_run_profile()
    assert profile["evidence"]["duration_limit_raised"] is False
    assert profile["active_profile"]["max_run_duration_seconds"] == 1800


def test_the_profile_records_the_web_search_qps_status_honestly():
    from backend.tier2_profile import tier2_first_run_profile

    search = tier2_first_run_profile()["web_search_qps"]
    for endpoint in ("search", "search_pro"):
        assert search[endpoint]["verified"] is False
        assert search[endpoint]["qps"] == 1
        assert "FALLBACK" in search[endpoint]["basis"]
        assert search[endpoint]["consumes_chat_quota"] is False


def test_the_profile_states_that_a_dedicated_key_does_not_prove_isolation():
    from backend.tier2_profile import tier2_first_run_profile

    external = tier2_first_run_profile()["external_usage"]
    assert external["isolated_per_api_key"] is False
    assert "does NOT" in external["note"] or "does not" in external["note"]


def test_the_profile_declares_sdk_automatic_retries_off():
    from backend.tier2_profile import tier2_first_run_profile

    assert tier2_first_run_profile()["active_profile"]["sdk_automatic_retries"] == 0


def test_the_profile_is_json_serializable():
    import json

    from backend.tier2_profile import tier2_first_run_profile

    assert json.loads(json.dumps(tier2_first_run_profile()))

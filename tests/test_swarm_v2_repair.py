"""Provider-policy parity, safe diagnostic classification and the single
bounded Commander plan repair.

Everything here is offline: fake provider clients behind the REAL
ModelGateway, BudgetTracker, ProviderScheduler and worker entrypoint.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

import backend.engines.swarm_v2 as swarm_pkg
import backend.worker.main as worker_main
from backend.budget import BudgetExceeded
from backend.engines.swarm_v2 import (
    VALIDATION_REASONS,
    Commander,
    CommanderModelResolver,
    CommanderPlanFailure,
    ModelGateway,
    PlanLimits,
    PlanValidationError,
    PlanValidator,
    provider_plan_policy,
)
from backend.errors import AppError
from backend.provider_scheduler import ProviderLimitsConfig, ProviderScheduler
from backend.runtime import CancellationRequested
from test_swarm_v2 import plan, task
from test_swarm_v2_smoke_offline import (
    FakeKimiCompletions,
    build_repo,
    kimi_response,
    minimal_plan,
    run_worker_directly,
    swarm_env,
)

MODEL_TEXT = "MODEL_CONTROLLED_SENTINEL"
RAW_SENTINEL = "RAW_INVALID_PLAN_SENTINEL"


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _validator(**limits):
    return PlanValidator(allowed_tools={"search", "calculator"}, limits=PlanLimits(**limits))


def _system_of(call):
    return " ".join(m.get("content", "") for m in call.get("messages", [])
                    if m.get("role") == "system")


class _Completions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = self.responses.pop(0)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=content))])


def gateway_with_limits(limits, responses, allowed=()):
    completions = _Completions(responses)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    scheduler = ProviderScheduler(ProviderLimitsConfig(
        max_concurrency=1, rpm_limit=None, max_rate_limit_retries=0,
        max_backpressure_wait_seconds=1, backoff_base_seconds=.001,
        backoff_max_seconds=.001))
    gateway = ModelGateway(
        guarded_client_factory=lambda *_: client, scheduler=scheduler,
        api_key="offline", base_url="offline", allowed_tool_names=allowed,
        plan_limits=limits)
    return gateway, completions


# --- A. provider policy parity ----------------------------------------------

def test_plan_prompt_carries_policy_derived_from_plan_limits():
    limits = PlanLimits(max_tasks=2)
    gateway, completions = gateway_with_limits(limits, [json.dumps(minimal_plan(1))])
    gateway.create_plan(model="fake", objective="offline", context={})
    system = _system_of(completions.calls[0])
    policy = _canonical(provider_plan_policy(limits, ()))
    assert policy in system
    assert '"max_tasks":2' in policy
    for key in ("max_tasks", "max_graph_depth", "max_recursion_depth",
                "max_replans", "max_cost_units", "max_tool_calls"):
        assert f'"{key}":' in policy
    # The semantic rules JSON Schema alone cannot encode.
    for fragment in (
        "COMPLETE direct and transitive dependency closure",
        "exactly one entry in assignments",
        "duplicate task signatures are rejected",
        "no cycles",
        "existing task_id",
        "completion criteria can never disable evidence requirements",
        "additionalProperties=false",
        "must not exceed plan.estimated_cost_units",
        "limits.max_tool_calls",
    ):
        assert fragment in system, fragment


def test_replan_prompt_carries_the_same_policy():
    limits = PlanLimits(max_tasks=5)
    gateway, completions = gateway_with_limits(
        limits, [json.dumps({"decision": "FINISH", "plan": None, "reason": "done"})])
    gateway.create_replan(model="fake", objective="offline", summary={})
    assert _canonical(provider_plan_policy(limits, ())) in _system_of(completions.calls[0])


def test_changing_plan_limits_changes_policy_and_firewall_consistently():
    narrow = PlanLimits(max_tasks=2)
    wide = PlanLimits(max_tasks=40)
    three_tasks = minimal_plan(num_tasks=3)

    gateway, completions = gateway_with_limits(narrow, [json.dumps(minimal_plan(1))])
    gateway.create_plan(model="fake", objective="offline", context={})
    assert '"max_tasks":2' in _system_of(completions.calls[0])
    with pytest.raises(PlanValidationError) as rejected:
        PlanValidator(allowed_tools=set(), limits=narrow).validate(three_tasks)
    assert rejected.value.reason == "TASK_COUNT_LIMIT"

    gateway_wide, completions_wide = gateway_with_limits(wide, [json.dumps(minimal_plan(1))])
    gateway_wide.create_plan(model="fake", objective="offline", context={})
    assert '"max_tasks":40' in _system_of(completions_wide.calls[0])
    assert PlanValidator(allowed_tools=set(), limits=wide).validate(three_tasks)


def test_empty_tool_registry_forces_empty_tools_and_empty_policy_allowlist():
    gateway, completions = gateway_with_limits(PlanLimits(), [json.dumps(minimal_plan(1))])
    gateway.create_plan(model="fake", objective="offline", context={})
    system = _system_of(completions.calls[0])
    assert "every task must use tools: []" in system
    assert '"allowed_tools":[]' in system

    listed, listed_completions = gateway_with_limits(
        PlanLimits(), [json.dumps(minimal_plan(1))], allowed=("mock.search",))
    listed.create_plan(model="fake", objective="offline", context={})
    assert '"allowed_tools":["mock.search"]' in _system_of(listed_completions.calls[0])


def test_worker_wires_one_plan_limits_instance_into_gateway_and_validator(monkeypatch):
    captured = {}
    real_gateway, real_validator = swarm_pkg.ModelGateway, swarm_pkg.PlanValidator

    class SpyGateway(real_gateway):
        def __init__(self, **kwargs):
            captured["gateway_limits"] = kwargs.get("plan_limits")
            super().__init__(**kwargs)

    class SpyValidator(real_validator):
        def __init__(self, **kwargs):
            captured["validator_limits"] = kwargs.get("limits")
            super().__init__(**kwargs)

    monkeypatch.setattr(swarm_pkg, "ModelGateway", SpyGateway)
    monkeypatch.setattr(swarm_pkg, "PlanValidator", SpyValidator)
    swarm_env(monkeypatch)
    repo, conversation_id = build_repo()
    run_id = run_worker_directly(repo, conversation_id, monkeypatch,
                                 FakeKimiCompletions(), idempotency_key="parity-000001")
    assert worker_main.execute_run(run_id, repo) == 0
    assert repo.get_run(run_id)["status"] == "completed"
    assert isinstance(captured.get("gateway_limits"), PlanLimits)
    assert captured["gateway_limits"] is captured["validator_limits"]


# --- B. safe diagnostic classification --------------------------------------

def _two_task_candidate():
    return plan([task("a", f"one {MODEL_TEXT}"), task("b", "two", dependencies=["a"])],
                contexts={"b": ["a"]})


@pytest.mark.parametrize("mutate,limits,reason", [
    (lambda p: p.pop("objective"), {}, "SCHEMA_MISSING_FIELD"),
    (lambda p: p.update({"surprise": MODEL_TEXT}), {}, "SCHEMA_EXTRA_FIELD"),
    (lambda p: p.update({"max_replans": "2"}), {}, "SCHEMA_TYPE_MISMATCH"),
    (lambda p: p["assignments"][1].update({"context_task_ids": []}), {},
     "ASSIGNMENT_CONTEXT_INCOMPLETE"),
    (lambda p: p["graph"]["tasks"][0]["completion"].update({"evidence_satisfied": False}),
     {}, "EVIDENCE_COMPLETION_MISMATCH"),
    (lambda p: p["graph"]["tasks"][0]["dependencies"].append("missing"), {},
     "MISSING_DEPENDENCY"),
    (lambda p: p["graph"]["tasks"][0]["dependencies"].append("b"), {},
     "DEPENDENCY_CYCLE"),
    (lambda p: p["graph"]["tasks"][0]["tools"][0].update({"name": "shell.exec"}), {},
     "TOOL_NOT_ALLOWLISTED"),
    (lambda p: p["graph"]["tasks"][1].update(
        {"goal": p["graph"]["tasks"][0]["goal"],
         "scope": p["graph"]["tasks"][0]["scope"],
         "dependencies": []}), {}, "DUPLICATE_TASK_SIGNATURE"),
    (lambda p: None, {"max_tasks": 1}, "TASK_COUNT_LIMIT"),
    (lambda p: None, {"max_graph_depth": 1}, "GRAPH_DEPTH_LIMIT"),
    (lambda p: p["graph"]["tasks"][0].update({"recursion_depth": 9}),
     {"max_recursion_depth": 2}, "RECURSION_LIMIT"),
    (lambda p: None, {"max_replans": 1}, "REPLAN_LIMIT"),
    (lambda p: None, {"max_cost_units": 5}, "COST_LIMIT"),
    (lambda p: None, {"max_tool_calls": 3}, "AGGREGATE_TOOL_CALL_LIMIT"),
])
def test_every_rejection_maps_to_one_static_safe_reason(mutate, limits, reason):
    candidate = _two_task_candidate()
    mutate(candidate)
    with pytest.raises(PlanValidationError) as failure:
        _validator(**limits).validate(candidate)
    assert failure.value.reason == reason
    assert failure.value.reason in VALIDATION_REASONS


def test_json_and_non_object_payloads_have_static_reasons():
    with pytest.raises(PlanValidationError) as broken:
        _validator().validate("{broken " + MODEL_TEXT)
    assert broken.value.reason == "JSON_DECODE_FAILED"
    with pytest.raises(PlanValidationError) as array:
        _validator().validate(json.dumps([MODEL_TEXT]))
    assert array.value.reason == "SCHEMA_TYPE_MISMATCH"


def test_reason_codes_are_a_fixed_code_owned_alphabet():
    # Static uppercase identifiers only: no model values, user text, field
    # values or exception strings can ever be a reason code.
    assert all(re.fullmatch(r"[A-Z][A-Z_]+", reason) for reason in VALIDATION_REASONS)
    assert MODEL_TEXT not in VALIDATION_REASONS
    with pytest.raises(ValueError):
        PlanValidationError("x", reason=MODEL_TEXT)
    with pytest.raises(ValueError):
        CommanderPlanFailure("COMMANDER_PLAN_SCHEMA_INVALID", validation_reason=MODEL_TEXT)


# --- C/D. the single bounded semantic repair through the real stack ---------

class SequencedPlanCompletions(FakeKimiCompletions):
    """Kimi fake serving a different Commander plan body per plan call."""

    def __init__(self, plan_bodies, **kwargs):
        super().__init__(**kwargs)
        self._plan_bodies = list(plan_bodies)

    def create(self, **kwargs):
        system = " ".join(m.get("content", "") for m in kwargs.get("messages", [])
                          if m.get("role") == "system")
        if "CommanderPlan JSON Schema" in system:
            self.calls.append(kwargs)
            return kimi_response(self._plan_bodies.pop(0))
        return super().create(**kwargs)


def _invalid_plan_body():
    bad = minimal_plan()
    bad["unexpected_field"] = RAW_SENTINEL
    return json.dumps(bad)


def _plan_calls(completions):
    return [call for call in completions.calls
            if "CommanderPlan JSON Schema" in _system_of(call)]


def _durable_dump(repo):
    return json.dumps({
        "runs": {k: {kk: str(vv) for kk, vv in v.items()} for k, v in repo.runs.items()},
        "events": [str(e) for e in repo.run_events],
        "checkpoints": [str(c) for c in repo.checkpoints],
        "ledger": [str(entry) for entry in getattr(repo, "usage_ledger", [])],
    })


def _settled_reservations(repo):
    return [r for r in getattr(repo, "usage_ledger", []) if r.get("status") == "reserved"]


def test_one_repair_success_completes_within_the_same_attempt(monkeypatch):
    swarm_env(monkeypatch)
    repo, conversation_id = build_repo()
    completions = SequencedPlanCompletions([_invalid_plan_body(), json.dumps(minimal_plan())])
    run_id = run_worker_directly(repo, conversation_id, monkeypatch, completions,
                                 idempotency_key="repair-ok-00001")
    assert worker_main.execute_run(run_id, repo) == 0
    run = repo.get_run(run_id)
    assert run["status"] == "completed"
    assert run["attempt"] == 1

    plan_calls = _plan_calls(completions)
    assert len(plan_calls) == 2  # exactly two Commander provider calls
    # Both calls traversed the guarded BudgetTracker: 2 plans + 2 tasks + 1
    # FINISH decision, each with a settled daily reservation.
    assert run["usage"]["model_calls"] == 5
    assert run["usage"]["retries"] == 1
    assert run["usage"]["provider_backpressure_events"] == 0
    reservations = _settled_reservations(repo)
    assert len(reservations) == 5 and all(r.get("settled") for r in reservations)

    # The repair prompt carries only the static reason, never the raw plan.
    repair_system = _system_of(plan_calls[1])
    assert "SCHEMA_EXTRA_FIELD" in repair_system
    assert RAW_SENTINEL not in repair_system
    assert RAW_SENTINEL not in _durable_dump(repo)

    checkpoints = [c for c in repo.checkpoints if str(c.get("run_id")) == str(run_id)]
    assert checkpoints
    assert all(c.get("engine_version") == "swarm_v2.1" for c in checkpoints)

    # A simulated Cloud Run retry of the finalized run performs no work.
    calls_before = len(completions.calls)
    assert worker_main.execute_run(run_id, repo) == 0
    assert len(completions.calls) == calls_before
    assert repo.get_run(run_id)["attempt"] == 1


def test_second_invalid_response_terminates_safely_with_safe_reason(monkeypatch):
    swarm_env(monkeypatch)
    repo, conversation_id = build_repo()
    completions = SequencedPlanCompletions([_invalid_plan_body(), _invalid_plan_body(),
                                            json.dumps(minimal_plan())])
    run_id = run_worker_directly(repo, conversation_id, monkeypatch, completions,
                                 idempotency_key="repair-bad-0001")
    assert worker_main.execute_run(run_id, repo) == 0  # handled: exit 0
    run = repo.get_run(run_id)
    assert run["status"] == "failed"
    assert run["attempt"] == 1
    assert run["error"] == {"code": "COMMANDER_PLAN_SCHEMA_INVALID",
                            "message": "Commander plan schema is invalid"}
    assert len(_plan_calls(completions)) == 2  # never a third attempt
    assert len(completions.calls) == 2
    assert run["usage"]["retries"] == 1

    failed_events = [e for e in repo.run_events if str(e["run_id"]) == str(run_id)
                     and e["event_type"] == "run_failed"]
    assert failed_events
    assert failed_events[-1]["payload"] == {"code": "COMMANDER_PLAN_SCHEMA_INVALID",
                                            "validation_reason": "SCHEMA_EXTRA_FIELD"}
    assert RAW_SENTINEL not in _durable_dump(repo)
    reservations = _settled_reservations(repo)
    assert len(reservations) == 2 and all(r.get("settled") for r in reservations)

    # A simulated Cloud Run retry against the terminal run is a no-op.
    events_before = len(repo.run_events)
    assert worker_main.execute_run(run_id, repo) == 0
    assert len(completions.calls) == 2
    assert len(repo.run_events) == events_before
    assert repo.get_run(run_id)["status"] == "failed"


# --- E. infrastructure failures are never repaired ---------------------------

class _RaisingClient:
    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    def create_plan(self, *, model, objective, context, repair_reason=None):
        self.calls += 1
        raise self.exc


def _commander(client, retries):
    return Commander(
        client=client,
        resolver=CommanderModelResolver(("fake",), {"fake"}),
        validator=PlanValidator(allowed_tools=set()),
        retry_callback=lambda agent, phase, reason: retries.append((agent, phase, reason)),
    )


@pytest.mark.parametrize("exc", [
    AppError("REPOSITORY_ERROR", "guarded persistence operation failed", 502),
    BudgetExceeded("MODEL_CALL_LIMIT_REACHED", "limit", "budget_exhausted", "budget_exhausted"),
    CancellationRequested("RUN_CANCELLED"),
])
def test_infrastructure_failures_escape_without_repair(exc):
    client = _RaisingClient(exc)
    retries = []
    with pytest.raises(type(exc)):
        _commander(client, retries).plan(requested_model="fake", objective="o", context={})
    assert client.calls == 1
    assert retries == []


@pytest.mark.parametrize("exc,code", [
    (RuntimeError("provider transport blew up"), "COMMANDER_COMPLETION_FAILED"),
    (CommanderPlanFailure("COMMANDER_COMPLETION_SHAPE_INVALID"),
     "COMMANDER_COMPLETION_SHAPE_INVALID"),
])
def test_provider_completion_failures_are_never_repaired(exc, code):
    client = _RaisingClient(exc)
    retries = []
    with pytest.raises(CommanderPlanFailure) as failure:
        _commander(client, retries).plan(requested_model="fake", objective="o", context={})
    assert failure.value.code == code
    assert client.calls == 1
    assert retries == []


def test_repair_counts_exactly_one_semantic_retry_and_only_on_repair():
    class InvalidThenValid:
        def __init__(self):
            self.calls = []

        def create_plan(self, *, model, objective, context, repair_reason=None):
            self.calls.append(repair_reason)
            return minimal_plan() if repair_reason else {"version": "1"}

    client = InvalidThenValid()
    retries = []
    approved = _commander(client, retries).plan(
        requested_model="fake", objective="o", context={})
    assert approved.graph.tasks
    assert client.calls == [None, "SCHEMA_MISSING_FIELD"]
    assert retries == [("commander", "planning", "SCHEMA_MISSING_FIELD")]

    valid_first = InvalidThenValid()
    valid_first.create_plan = lambda **kwargs: minimal_plan()
    no_retries = []
    _commander(valid_first, no_retries).plan(requested_model="fake", objective="o", context={})
    assert no_retries == []

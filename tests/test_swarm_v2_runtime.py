from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

from backend.budget import BudgetConfig, BudgetTracker, build_guarded_client_factory
from backend.engines.swarm_v2 import (
    BoundedTaskExecutor,
    Commander,
    CommanderDecision,
    CommanderModelResolver,
    CommanderPlanFailure,
    CommanderPlan,
    GenericWorker,
    ModelGateway,
    PlanValidator,
    TaskGraph,
    TaskResult,
)
from backend.engines.swarm_v2.contracts import (
    commander_decision_json_schema,
    commander_plan_json_schema,
)
from backend.engines.swarm_v2.validation import PlanLimits
from backend.provider_scheduler import ProviderLimitsConfig, ProviderScheduler
from backend.runtime import CancellationRequested
from backend.tools import MockSearchTool, ToolContext, ToolError, ToolMode, ToolRegistry

from test_swarm_v2 import task


def graph(*items):
    return TaskGraph.model_validate({"tasks": list(items)})


class RecordingWorker:
    def __init__(self, log, lock, *, fail=None, delay=.02):
        self.log, self.lock, self.fail, self.delay = log, lock, fail, delay

    def execute(self, spec, dependencies):
        with self.lock:
            self.log.append(("start", spec.task_id, tuple(dependencies)))
        time.sleep(self.delay)
        if spec.task_id == self.fail:
            return TaskResult(spec.task_id, "failed", error={"code": "EXPECTED"})
        with self.lock:
            self.log.append(("end", spec.task_id, ()))
        return TaskResult(spec.task_id, "completed", output={"answer": spec.task_id})


def test_dependency_priority_bounded_parallelism_and_failure_isolation():
    log, lock = [], threading.Lock()
    executor = BoundedTaskExecutor(worker_factory=lambda: RecordingWorker(log, lock, fail="bad"), max_active_workers=2)
    result = executor.execute(graph(
        task("low", "low", tool="search", dependencies=[]),
        {**task("high", "high"), "priority": 100},
        task("bad", "bad"),
        task("dependent", "dependent", dependencies=["high"]),
        task("blocked", "blocked", dependencies=["bad"]),
    ))
    starts = [entry[1] for entry in log if entry[0] == "start"]
    assert starts[0] == "high"
    assert starts.index("dependent") > starts.index("high")
    assert result.max_active_workers == 2
    assert result.tasks["bad"].status == "failed"
    assert result.tasks["blocked"].status == "blocked"
    assert result.tasks["low"].status == "completed"


def test_external_cancellation_stops_active_tool_and_queued_work_cooperatively():
    cancelled = threading.Event()
    tool_started = threading.Event()
    starts, model_calls = [], []
    class CooperativeTool(MockSearchTool):
        def execute(self, context, payload):
            starts.append(payload["query"])
            tool_started.set()
            while True:
                context.check_cancelled()
                time.sleep(.001)
    class Gateway:
        def call(self, **kwargs):
            model_calls.append(kwargs)
            return {"answer": "unexpected"}
    registry = ToolRegistry([CooperativeTool()])
    context = ToolContext(scopes=frozenset({"mock:search"}))
    worker_factory = lambda: GenericWorker(gateway=Gateway(), tools=registry, model="fake",
        tool_context=context, cancellation_checker=cancelled.is_set)
    executor = BoundedTaskExecutor(worker_factory=worker_factory, max_active_workers=1,
                                   cancellation_checker=cancelled.is_set)
    canceller = threading.Thread(target=lambda: (tool_started.wait(1), cancelled.set()))
    canceller.start()
    with pytest.raises(CancellationRequested):
        executor.execute(graph(task("a", "a", tool="mock.search"), task("b", "b", tool="mock.search")))
    canceller.join()
    assert starts == ["a"]
    assert model_calls == []


def test_registry_allowlist_schemas_scope_and_write_capability():
    registry = ToolRegistry([MockSearchTool({"q": ("answer",)})])
    context = ToolContext(scopes=frozenset({"mock:search"}))
    assert registry.execute("mock.search", context, {"query": "q"}) == {"rows": ["answer"]}
    with pytest.raises(ToolError, match="not registered") as unknown:
        registry.execute("missing", context, {})
    assert unknown.value.code == "TOOL_NOT_ALLOWED"
    with pytest.raises(ToolError) as invalid:
        registry.execute("mock.search", context, {"query": 4})
    assert invalid.value.code == "TOOL_INPUT_INVALID"

    class BadOutput(MockSearchTool):
        def execute(self, context, payload): return {"rows": [7]}
    with pytest.raises(ToolError) as output:
        ToolRegistry([BadOutput()]).execute("mock.search", context, {"query": "q"})
    assert output.value.code == "TOOL_OUTPUT_INVALID"

    class Writer(MockSearchTool):
        mode = ToolMode.WRITE
        def __init__(self): super().__init__()
    writer = Writer()
    object.__setattr__(writer, "mode", ToolMode.WRITE)
    writes = ToolRegistry([writer])
    with pytest.raises(ToolError) as denied:
        writes.execute("mock.search", context, {"query": "q"})
    assert denied.value.code == "TOOL_WRITE_NOT_APPROVED"
    approved = ToolContext(scopes=frozenset({"mock:search"}), write_approved=True,
                           capabilities=frozenset({"tool:write:mock.search"}))
    assert writes.execute("mock.search", approved, {"query": "q"}) == {"rows": []}


@pytest.mark.parametrize("target,schema", [
    ("input_schema", {"type": "object", "properties": {"nested": {"type": "mystery"}},
                      "required": [], "additionalProperties": False}),
    ("output_schema", {"type": "object", "properties": {"nested": {"type": "object",
                       "properties": {}, "required": []}}, "required": [], "additionalProperties": False}),
    ("input_schema", {"type": "array"}),
    ("output_schema", {"type": "string", "pattern": ".*"}),
])
def test_registry_rejects_invalid_nested_schemas_before_execution(target, schema):
    executed = []
    class InvalidTool(MockSearchTool):
        def execute(self, context, payload):
            executed.append(True)
            return {"rows": []}
    tool = InvalidTool()
    object.__setattr__(tool, target, schema)
    with pytest.raises(ValueError):
        ToolRegistry([tool])
    assert executed == []


def test_generic_worker_sanitizes_internal_exception_text():
    secret = "SECRET_SENTINEL_DO_NOT_EXPOSE"
    class FailingGateway:
        def call(self, **kwargs): raise RuntimeError(secret)
    worker = GenericWorker(gateway=FailingGateway(), tools=ToolRegistry(), model="fake", tool_context=ToolContext())
    task_data = task("safe", "safe", tool="search")
    task_data["tools"] = []
    spec = TaskGraph.model_validate({"tasks": [task_data]}).tasks[0]
    result = worker.execute(spec, {})
    assert result.error == {"code": "TASK_FAILED", "message": "task execution failed"}
    assert secret not in str(result)


def test_every_call_uses_one_gateway_guard_accounting_and_429_is_not_semantic_retry():
    calls = 0
    class RateLimited(Exception): status_code = 429
    class Completions:
        def create(self, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1: raise RateLimited("HTTP 429")
            usage = SimpleNamespace(prompt_tokens=3, completion_tokens=2, cost=.001)
            message = SimpleNamespace(content='{"answer":"ok"}')
            return SimpleNamespace(usage=usage, choices=[SimpleNamespace(message=message)])
    inner = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    ledger = []
    tracker = BudgetTracker(BudgetConfig(max_model_calls_per_run=10, max_retries=2), kill_switch=lambda: True,
                            ledger_recorder=ledger.append)
    scheduler = ProviderScheduler(ProviderLimitsConfig(max_concurrency=1, rpm_limit=None, max_rate_limit_retries=2,
        max_backpressure_wait_seconds=1, backoff_base_seconds=.001, backoff_max_seconds=.001), sleep_fn=lambda _: None, rng=lambda: 0)
    gateway = ModelGateway(guarded_client_factory=build_guarded_client_factory(tracker, lambda *_: inner),
                           scheduler=scheduler, api_key="offline", base_url="offline")
    gateway.call(model="fake", messages=[{"role": "user", "content": "x"}], agent="worker:a", phase="execute")
    assert calls == 2
    assert tracker.model_calls == 2
    assert tracker.provider_backpressure_events == 1
    assert tracker.retries == 0
    assert [item["decision"] for item in ledger] == ["reserved", "settled", "reserved", "settled"]

class RecordingCompletions:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        message = SimpleNamespace(content=json.dumps(next(self.responses)))
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _recording_gateway(responses, *, allowed_tools=()):
    completions = RecordingCompletions(responses)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    scheduler = ProviderScheduler(ProviderLimitsConfig(
        max_concurrency=1,
        rpm_limit=None,
        max_rate_limit_retries=0,
        max_backpressure_wait_seconds=1,
        backoff_base_seconds=.001,
        backoff_max_seconds=.001,
    ))
    gateway = ModelGateway(
        guarded_client_factory=lambda *_: client,
        scheduler=scheduler,
        api_key="offline",
        base_url="offline",
        allowed_tool_names=allowed_tools,
    )
    return gateway, completions


def _canonical_schema(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _valid_provider_plan(*, tool_name=None):
    tools = [] if tool_name is None else [{
        "name": tool_name, "scope": "mock:search", "max_calls": 1,
    }]
    return {
        "version": "1",
        "objective": "offline",
        "graph": {"tasks": [{
            "task_id": "only",
            "goal": "produce an offline answer",
            "scope": "provided objective only",
            "dependencies": [],
            "tools": tools,
            "output_schema": {
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
                "additionalProperties": False,
            },
            "evidence": {
                "minimum_sources": 0,
                "required_fields": [],
                "min_confidence": 0.0,
            },
            "priority": 1,
            "recursion_depth": 0,
            "estimated_cost_units": 1,
            "completion": {
                "required_outputs": ["answer"],
                "evidence_satisfied": False,
                "allow_partial": False,
            },
        }]},
        "assignments": [{
            "task_id": "only",
            "worker_role": "generic",
            "context_task_ids": [],
        }],
        "max_replans": 0,
        "estimated_cost_units": 1,
    }


def _commander_for_gateway(gateway, *, max_tasks=64):
    return Commander(
        client=gateway,
        resolver=CommanderModelResolver(("fake",), {"fake"}),
        validator=PlanValidator(
            allowed_tools=set(), limits=PlanLimits(max_tasks=max_tasks)
        ),
    )


def test_provider_shaped_json_string_traverses_completion_decode_schema_and_limits():
    gateway, _ = _recording_gateway([_valid_provider_plan()])
    approved = _commander_for_gateway(gateway).plan(
        requested_model="fake", objective="offline", context={}
    )
    assert approved.graph.tasks[0].task_id == "only"


@pytest.mark.parametrize("content", [None, ""])
def test_empty_provider_content_has_stable_completion_shape_code(content):
    class Completions:
        def create(self, **kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=content)
            )])
    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    gateway, _ = _recording_gateway([_valid_provider_plan()])
    gateway._client = client
    with pytest.raises(CommanderPlanFailure) as failure:
        _commander_for_gateway(gateway).plan(
            requested_model="fake", objective="offline", context={}
        )
    assert failure.value.code == "COMMANDER_COMPLETION_SHAPE_INVALID"


@pytest.mark.parametrize("content,code", [
    ("{broken", "COMMANDER_PLAN_JSON_INVALID"),
    (json.dumps({"version": "1"}), "COMMANDER_PLAN_SCHEMA_INVALID"),
])
def test_bad_provider_plan_has_only_stable_safe_code(content, code):
    sentinel = "SECRET_PROVIDER_SENTINEL"
    class Completions:
        def create(self, **kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=content + sentinel)
            )])
    # Schema-invalid JSON must remain valid JSON, so use the ordinary shaped
    # completion for that case.
    if code == "COMMANDER_PLAN_SCHEMA_INVALID":
        response = json.loads(content)
        gateway, _ = _recording_gateway([response])
    else:
        client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
        gateway, _ = _recording_gateway([_valid_provider_plan()])
        gateway._client = client
    with pytest.raises(CommanderPlanFailure) as failure:
        _commander_for_gateway(gateway).plan(
            requested_model="fake", objective="offline", context={}
        )
    assert failure.value.code == code
    assert sentinel not in str(failure.value)


def test_plan_limit_and_provider_exception_codes_are_sanitized():
    gateway, _ = _recording_gateway([_valid_provider_plan()])
    with pytest.raises(CommanderPlanFailure) as limited:
        _commander_for_gateway(gateway, max_tasks=0).plan(
            requested_model="fake", objective="offline", context={}
        )
    assert limited.value.code == "COMMANDER_PLAN_LIMIT_EXCEEDED"

    sentinel = "SECRET_PROVIDER_SENTINEL"
    class ProviderFailure:
        def create_plan(self, **kwargs):
            raise RuntimeError(sentinel)
    with pytest.raises(CommanderPlanFailure) as provider:
        _commander_for_gateway(ProviderFailure()).plan(
            requested_model="fake", objective="offline", context={}
        )
    assert provider.value.code == "COMMANDER_COMPLETION_FAILED"
    assert sentinel not in str(provider.value)


@pytest.mark.parametrize("response", [object(), SimpleNamespace(choices=[]),
                                        SimpleNamespace(choices=[object()])])
def test_unexpected_completion_shapes_have_stable_safe_code(response):
    class Completions:
        def create(self, **kwargs): return response
    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    gateway, _ = _recording_gateway([_valid_provider_plan()])
    gateway._client = client
    with pytest.raises(CommanderPlanFailure) as failure:
        _commander_for_gateway(gateway).plan(
            requested_model="fake", objective="offline", context={}
        )
    assert failure.value.code == "COMMANDER_COMPLETION_SHAPE_INVALID"


def test_real_model_commander_requests_authoritative_contracts_and_tools():
    valid_plan = _valid_provider_plan()
    finish = {"decision": "FINISH", "plan": None, "reason": "all tasks completed"}
    gateway, completions = _recording_gateway([valid_plan, finish])

    plan_payload = gateway.create_plan(
        model="fake",
        objective="offline",
        context={"allowed_tools": ["user.injected"]},
    )
    decision_payload = gateway.create_replan(
        model="fake",
        objective="offline",
        summary={
            "decision_context": {
                "all_tasks_completed": True,
                "has_unresolved_issues": False,
                "valid_terminal_decision": "FINISH",
            }
        },
    )

    assert CommanderPlan.model_validate_json(plan_payload)
    assert CommanderDecision.model_validate_json(decision_payload)
    assert all(call["response_format"] == {"type": "json_object"}
               for call in completions.calls)

    plan_system = completions.calls[0]["messages"][0]["content"]
    decision_system = completions.calls[1]["messages"][0]["content"]
    assert _canonical_schema(commander_plan_json_schema()) in plan_system
    assert _canonical_schema(commander_decision_json_schema()) in decision_system
    assert "Server-authorized tool names: []" in plan_system
    assert "every task must use tools: []" in plan_system
    assert "user.injected" not in plan_system
    assert "ADD_TASKS and REVISE_TASK require a replacement plan" in decision_system
    assert "REQUEST_VERIFICATION and FINISH forbid one" in decision_system


def test_model_visible_tool_allowlist_is_sorted_and_validation_stays_fail_closed():
    gateway, completions = _recording_gateway(
        [_valid_provider_plan(tool_name="mock.search")],
        allowed_tools=("mock.zeta", "mock.search"),
    )
    payload = gateway.create_plan(
        model="fake", objective="offline", context={"allowed_tools": ["evil.write"]}
    )
    system = completions.calls[0]["messages"][0]["content"]
    assert 'Server-authorized tool names: ["mock.search","mock.zeta"]' in system
    assert "evil.write" not in system

    validator = PlanValidator(
        allowed_tools={"mock.search"},
        limits=PlanLimits(max_tasks=1, max_tool_calls=1),
    )
    assert validator.validate(payload).graph.tasks[0].tools[0].name == "mock.search"

    unknown = json.loads(payload)
    unknown["graph"]["tasks"][0]["tools"][0]["name"] = "evil.write"
    with pytest.raises(ValueError, match="allowlisted"):
        validator.validate(unknown)

    excessive = json.loads(payload)
    excessive["graph"]["tasks"].append({
        **excessive["graph"]["tasks"][0],
        "task_id": "second",
    })
    excessive["assignments"].append({
        "task_id": "second",
        "worker_role": "generic",
        "context_task_ids": [],
    })
    with pytest.raises(ValueError, match="task count limit"):
        validator.validate(excessive)

    with pytest.raises(ValueError):
        validator.validate({"version": "1"})

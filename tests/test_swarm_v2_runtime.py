from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from backend.budget import BudgetConfig, BudgetTracker, build_guarded_client_factory
from backend.engines.swarm_v2 import BoundedTaskExecutor, GenericWorker, ModelGateway, TaskGraph, TaskResult
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

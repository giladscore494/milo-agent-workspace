from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from backend.budget import BudgetConfig, BudgetTracker, build_guarded_client_factory
from backend.engines.swarm_v2 import BoundedTaskExecutor, ModelGateway, TaskGraph, TaskResult
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


def test_cancellation_stops_queued_and_active_work():
    cancelled = threading.Event()
    class CancellingWorker(RecordingWorker):
        def execute(self, spec, dependencies):
            cancelled.set()
            time.sleep(.02)
            raise CancellationRequested("RUN_CANCELLED")
    executor = BoundedTaskExecutor(worker_factory=lambda: CancellingWorker([], threading.Lock()),
                                   max_active_workers=1, cancellation_checker=cancelled.is_set)
    with pytest.raises(CancellationRequested):
        executor.execute(graph(task("a", "a"), task("b", "b")))


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

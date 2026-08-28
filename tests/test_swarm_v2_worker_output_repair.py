"""The single bounded GenericWorker output repair (Swarm V2 hardening B3).

Everything here is offline: provider-shaped Kimi fakes behind the REAL
ModelGateway, BudgetTracker, ProviderScheduler, ToolRegistry and worker
entrypoint. No network and no paid call.

The invariants under test:
  * a valid first completion still costs exactly ONE semantic model call;
  * only an invalid-JSON or schema-invalid completion earns exactly ONE
    repair call, and never a second;
  * tools execute exactly once per declared tool, repair or not;
  * the malformed completion is never re-sent and never durable;
  * tool, provider, budget, cancellation and infrastructure failures keep
    their existing semantics and are never repaired.
"""

from __future__ import annotations

import json
from uuid import UUID

import pytest

import backend.worker.main as worker_main
from backend.budget import BudgetConfig, BudgetExceeded, BudgetTracker, build_guarded_client_factory
from backend.engines.swarm_v2 import (
    MAX_WORKER_OUTPUT_MODEL_ATTEMPTS,
    WORKER_OUTPUT_REASONS,
    GenericWorker,
    ModelGateway,
    TaskGraph,
    WorkerOutputValidationError,
    build_worker_request,
    validate_worker_output,
)
from backend.provider_scheduler import ProviderLimitsConfig, ProviderScheduler
from backend.runtime import CancellationRequested
from backend.tools import MockCatalogTool, MockSearchTool, ToolContext, ToolError, ToolRegistry

from test_swarm_v2_smoke_offline import (
    ScriptedCompletions,
    build_repo,
    fake_kimi_client,
    kimi_response,
    minimal_plan,
    run_worker_directly,
    swarm_env,
)

# A unique marker planted inside malformed model output. It must never reach
# a durable event, a TaskResult, a checkpoint, or the repair request.
SENTINEL = "RAW_WORKER_COMPLETION_SENTINEL_B3"

MALFORMED_JSON = f'not json at all {{{{ {SENTINEL}'
SCHEMA_VIOLATION = json.dumps({"answer": 7, "unexpected": SENTINEL})
VALID_OUTPUT = json.dumps({"answer": "bounded structured answer"})

TOOL_CONTEXT = ToolContext(scopes=frozenset({"mock:search", "mock:catalog"}))


# --- offline fixtures -------------------------------------------------------

def worker_task(*, tools=("mock.search",)):
    return TaskGraph.model_validate({"tasks": [{
        "task_id": "only",
        "goal": "produce one bounded structured answer",
        "scope": "offline fixture material only",
        "dependencies": [],
        "tools": [{"name": name, "scope": "read offline fixtures", "max_calls": 1}
                  for name in tools],
        "output_schema": {"type": "object", "properties": {"answer": {"type": "string"}},
                          "required": ["answer"], "additionalProperties": False},
        "evidence": {"minimum_sources": 0, "required_fields": [], "min_confidence": 0.0},
        "priority": 1, "recursion_depth": 0, "estimated_cost_units": 1,
        "completion": {"required_outputs": ["answer"], "evidence_satisfied": False,
                       "allow_partial": False},
    }]}).tasks[0]


def counting_registry(calls):
    """A registry whose tools record every invocation."""

    class CountingSearch(MockSearchTool):
        def execute(self, context, payload):
            calls.append(("mock.search", payload["query"]))
            return {"rows": ["validated search material"]}

    class CountingCatalog(MockCatalogTool):
        def execute(self, context, payload):
            calls.append(("mock.catalog", payload["query"]))
            return {"rows": ["validated catalog material"]}

    return ToolRegistry([CountingSearch(), CountingCatalog()])


def build_worker(*bodies, tracker=None, cancellation_checker=None, tool_calls=None,
                 events=None, agent_steps=None, retries=None, retry_callback=None,
                 provider_limits=None, registry=None):
    """The real gateway/budget/scheduler stack over a scripted fake provider."""
    completions = ScriptedCompletions(*bodies)
    tracker = tracker or BudgetTracker(
        BudgetConfig(max_model_calls_per_run=10, max_retries=10), kill_switch=lambda: True)
    scheduler = ProviderScheduler(provider_limits or ProviderLimitsConfig(
        max_concurrency=2, rpm_limit=None, tpm_limit=None, max_rate_limit_retries=0,
        max_backpressure_wait_seconds=1.0, backoff_base_seconds=.001,
        backoff_max_seconds=.001), sleep_fn=lambda _: None, rng=lambda: 0)
    gateway = ModelGateway(
        guarded_client_factory=build_guarded_client_factory(
            tracker, inner_factory=lambda key, url: fake_kimi_client(completions)),
        scheduler=scheduler, api_key="offline", base_url="offline",
        cancellation_checker=cancellation_checker,
        agent_step_callback=(None if agent_steps is None
                             else lambda agent, phase: agent_steps.append((agent, phase))))
    worker = GenericWorker(
        gateway=gateway,
        tools=registry if registry is not None else counting_registry(
            tool_calls if tool_calls is not None else []),
        model="kimi-k2.6", tool_context=TOOL_CONTEXT,
        cancellation_checker=cancellation_checker,
        event_sink=(None if events is None else lambda kind, payload: events.append((kind, payload))),
        retry_callback=retry_callback or (
            None if retries is None
            else lambda agent, phase, reason: retries.append((agent, phase, reason))))
    return worker, completions, tracker


def system_of(call):
    return " ".join(message.get("content", "") for message in call["messages"]
                    if message.get("role") == "system")


def user_payload(call):
    return json.loads(next(message["content"] for message in call["messages"]
                           if message.get("role") == "user"))


def repair_events(events):
    return [payload for kind, payload in events if kind == "worker_output_repair_started"]


# --- the bound itself -------------------------------------------------------

def test_maximum_semantic_worker_output_attempts_is_an_explicit_constant():
    assert MAX_WORKER_OUTPUT_MODEL_ATTEMPTS == 2
    assert WORKER_OUTPUT_REASONS == {"WORKER_OUTPUT_JSON_INVALID", "WORKER_OUTPUT_SCHEMA_INVALID"}


# --- A: a valid first completion is unchanged -------------------------------

def test_valid_initial_output_costs_one_call_one_tool_and_no_repair():
    tool_calls, events = [], []
    worker, completions, tracker = build_worker(
        kimi_response(VALID_OUTPUT), tool_calls=tool_calls, events=events)

    result = worker.execute(worker_task(), {})

    assert result.status == "completed"
    assert result.output == {"answer": "bounded structured answer"}
    assert len(completions.calls) == 1
    assert tracker.model_calls == 1
    assert tool_calls == [("mock.search", "produce one bounded structured answer")]
    assert repair_events(events) == []
    assert "static reason code" not in system_of(completions.calls[0])


# --- B/C: exactly one repair recovers each repairable class -----------------

@pytest.mark.parametrize("malformed,reason", [
    (MALFORMED_JSON, "WORKER_OUTPUT_JSON_INVALID"),
    (SCHEMA_VIOLATION, "WORKER_OUTPUT_SCHEMA_INVALID"),
])
def test_one_repair_recovers_invalid_json_and_schema_invalid_output(malformed, reason):
    tool_calls, events, retries = [], [], []
    worker, completions, tracker = build_worker(
        kimi_response(malformed), kimi_response(VALID_OUTPUT),
        tool_calls=tool_calls, events=events, retries=retries)

    result = worker.execute(worker_task(), {})

    assert result.status == "completed"
    assert result.output == {"answer": "bounded structured answer"}
    # Exactly two semantic model calls, both accounted for as real calls.
    assert len(completions.calls) == 2
    assert tracker.model_calls == 2
    # ...and exactly one tool execution: the repair reuses captured material.
    assert tool_calls == [("mock.search", "produce one bounded structured answer")]
    assert repair_events(events) == [
        {"task_id": "only", "reason_code": reason, "attempt_number": 2}]
    assert retries == [("worker:only", "execute", reason)]
    assert reason in system_of(completions.calls[1])


# --- D/E/F: a second structural failure stops, it never escalates -----------

@pytest.mark.parametrize("first,second,final_code", [
    # D: invalid JSON twice.
    (MALFORMED_JSON, MALFORMED_JSON, "WORKER_OUTPUT_JSON_INVALID"),
    # E: schema-invalid twice.
    (SCHEMA_VIOLATION, SCHEMA_VIOLATION, "WORKER_OUTPUT_SCHEMA_INVALID"),
    # F: the repair changes the failure class; the FINAL class classifies it.
    (MALFORMED_JSON, SCHEMA_VIOLATION, "WORKER_OUTPUT_SCHEMA_INVALID"),
    (SCHEMA_VIOLATION, MALFORMED_JSON, "WORKER_OUTPUT_JSON_INVALID"),
])
def test_second_structural_failure_is_terminal_with_no_third_attempt(first, second, final_code):
    tool_calls, events = [], []
    # A third scripted response is available and deliberately VALID: if the
    # worker ever attempted it the task would succeed, so a failing result
    # proves the attempt bound rather than an exhausted script.
    worker, completions, tracker = build_worker(
        kimi_response(first), kimi_response(second), kimi_response(VALID_OUTPUT),
        tool_calls=tool_calls, events=events)

    result = worker.execute(worker_task(), {})

    assert result.status == "failed"
    assert result.error["code"] == final_code
    assert len(completions.calls) == MAX_WORKER_OUTPUT_MODEL_ATTEMPTS == 2
    assert tracker.model_calls == 2
    assert tool_calls == [("mock.search", "produce one bounded structured answer")]
    assert len(repair_events(events)) == 1


def test_final_failure_distinguishes_structural_output_failure_from_tool_failure():
    """Debugging must tell 'the tool failed' from 'the worker could not
    produce valid output', without any raw exception body."""
    structural, _, _ = build_worker(kimi_response(MALFORMED_JSON), kimi_response(MALFORMED_JSON))
    structural_result = structural.execute(worker_task(), {})

    class FailingSearch(MockSearchTool):
        def execute(self, context, payload):
            raise ToolError("TOOL_EXECUTION_FAILED", "tool execution failed", tool="mock.search")

    tool_worker, tool_completions, _ = build_worker(
        kimi_response(VALID_OUTPUT), registry=ToolRegistry([FailingSearch()]))
    tool_result = tool_worker.execute(worker_task(), {})

    assert structural_result.error["code"] == "WORKER_OUTPUT_JSON_INVALID"
    assert tool_result.error["code"] == "TOOL_EXECUTION_FAILED"
    assert structural_result.error["code"] != tool_result.error["code"]
    assert tool_completions.calls == []  # G: a tool failure never reaches the model


# --- G: ToolError is never repaired -----------------------------------------

def test_tool_error_never_triggers_a_repair_and_keeps_existing_semantics():
    events = []

    class FailingSearch(MockSearchTool):
        def execute(self, context, payload):
            raise ToolError("TOOL_EXECUTION_FAILED", "tool execution failed", tool="mock.search")

    worker, completions, tracker = build_worker(
        kimi_response(VALID_OUTPUT), events=events, registry=ToolRegistry([FailingSearch()]))

    result = worker.execute(worker_task(), {})

    assert result.status == "failed"
    assert result.error == {"code": "TOOL_EXECUTION_FAILED",
                            "message": "tool execution failed", "tool": "mock.search"}
    assert completions.calls == []
    assert tracker.model_calls == 0
    assert repair_events(events) == []


# --- H: budget authority outranks repair ------------------------------------

def test_budget_refusal_before_repair_propagates_and_is_never_a_worker_failure():
    events = []
    # One model-call slot: the first completion consumes it, so the repair's
    # reservation is refused by the tracker BEFORE any provider call.
    tracker = BudgetTracker(BudgetConfig(max_model_calls_per_run=1), kill_switch=lambda: True)
    worker, completions, tracker = build_worker(
        kimi_response(MALFORMED_JSON), kimi_response(VALID_OUTPUT),
        tracker=tracker, events=events)

    with pytest.raises(BudgetExceeded) as refused:
        worker.execute(worker_task(), {})

    assert refused.value.code == "MODEL_CALL_LIMIT_REACHED"
    # The repair provider call did not happen; budget terminal semantics
    # propagate instead of being laundered into TASK_FAILED or a fake
    # worker-output reason.
    assert len(completions.calls) == 1
    assert tracker.model_calls == 1
    # The repair was started (the event and its bounded payload are emitted
    # before the guarded call) but the guarded call itself was refused.
    assert len(repair_events(events)) == 1


def test_exhausted_semantic_retry_allowance_refuses_the_repair_call():
    """A worker repair consumes the SAME run-level semantic retry allowance
    the Commander repair does, so an exhausted allowance stops it as a
    budget outcome -- never as a worker-output failure."""
    events = []
    tracker = BudgetTracker(BudgetConfig(max_model_calls_per_run=10, max_retries=0),
                            kill_switch=lambda: True)
    # The semantic-retry callback is routed at the real tracker, exactly as
    # backend.worker.main wires record_retry.
    worker, completions, tracker = build_worker(
        kimi_response(MALFORMED_JSON), kimi_response(VALID_OUTPUT),
        tracker=tracker, events=events,
        retry_callback=lambda agent, phase, reason: tracker.record_retry())

    with pytest.raises(BudgetExceeded) as refused:
        worker.execute(worker_task(), {})

    assert refused.value.code == "RETRY_LIMIT_REACHED"
    assert len(completions.calls) == 1        # the repair never reached the provider
    assert len(repair_events(events)) == 1    # but the decision to repair is visible


# --- I: cancellation outranks repair ----------------------------------------

def test_cancellation_between_invalid_output_and_repair_stops_before_the_call():
    events = []
    delivered = []

    class CancellingCompletions(ScriptedCompletions):
        def create(self, **kwargs):
            response = super().create(**kwargs)
            delivered.append(True)
            return response

    completions = CancellingCompletions(kimi_response(MALFORMED_JSON), kimi_response(VALID_OUTPUT))
    tracker = BudgetTracker(BudgetConfig(max_model_calls_per_run=10), kill_switch=lambda: True)
    scheduler = ProviderScheduler(ProviderLimitsConfig(
        max_concurrency=2, rpm_limit=None, tpm_limit=None, max_rate_limit_retries=0,
        max_backpressure_wait_seconds=1.0, backoff_base_seconds=.001, backoff_max_seconds=.001),
        sleep_fn=lambda _: None, rng=lambda: 0)
    # Cancellation becomes true only AFTER the first completion is delivered.
    cancelled = lambda: bool(delivered)
    gateway = ModelGateway(
        guarded_client_factory=build_guarded_client_factory(
            tracker, inner_factory=lambda key, url: fake_kimi_client(completions)),
        scheduler=scheduler, api_key="offline", base_url="offline",
        cancellation_checker=cancelled)
    worker = GenericWorker(gateway=gateway, tools=counting_registry([]), model="kimi-k2.6",
                           tool_context=TOOL_CONTEXT, cancellation_checker=cancelled,
                           event_sink=lambda kind, payload: events.append((kind, payload)))

    with pytest.raises(CancellationRequested):
        worker.execute(worker_task(), {})

    assert len(completions.calls) == 1
    assert tracker.model_calls == 1
    # Cancellation is checked before the repair is announced or paid for.
    assert repair_events(events) == []


# --- J/K: provider failures are not structural output failures --------------

class RateLimited(Exception):
    status_code = 429


def test_exhausted_provider_backpressure_never_starts_a_semantic_repair():
    events = []
    worker, completions, tracker = build_worker(
        RateLimited("Error code: 429"), kimi_response(VALID_OUTPUT), events=events)

    result = worker.execute(worker_task(), {})

    assert result.status == "failed"
    assert result.error["code"] == "PROVIDER_BACKPRESSURE_EXCEEDED"
    # No completion was delivered, so there is nothing structurally invalid
    # to repair.
    assert repair_events(events) == []
    assert tracker.retries == 0
    assert tracker.provider_backpressure_events >= 1


def test_scheduler_429_retry_is_transport_not_a_second_semantic_attempt():
    """A 429 the scheduler absorbs stays ONE semantic worker-output attempt."""
    events, tool_calls = [], []
    worker, completions, tracker = build_worker(
        RateLimited("Error code: 429"), kimi_response(VALID_OUTPUT),
        events=events, tool_calls=tool_calls,
        provider_limits=ProviderLimitsConfig(
            max_concurrency=2, rpm_limit=None, tpm_limit=None, max_rate_limit_retries=1,
            max_backpressure_wait_seconds=5.0, backoff_base_seconds=.001,
            backoff_max_seconds=.001))

    result = worker.execute(worker_task(), {})

    assert result.status == "completed"
    assert len(completions.calls) == 2          # two transport attempts...
    assert repair_events(events) == []          # ...but ZERO semantic repairs
    assert tracker.retries == 0                 # a 429 never consumes the
    assert tracker.provider_backpressure_events >= 1  # semantic allowance
    assert tool_calls == [("mock.search", "produce one bounded structured answer")]


def test_provider_authentication_or_transport_failure_never_repairs():
    events = []
    worker, completions, tracker = build_worker(
        RuntimeError(f"401 unauthorized {SENTINEL}"), kimi_response(VALID_OUTPUT), events=events)

    result = worker.execute(worker_task(), {})

    assert result.status == "failed"
    assert result.error == {"code": "TASK_FAILED", "message": "task execution failed"}
    assert len(completions.calls) == 1
    assert repair_events(events) == []
    assert SENTINEL not in json.dumps([result.error, events], default=str)


# --- L/O: tool invocation count and reused material -------------------------

def test_every_declared_tool_runs_exactly_once_even_when_the_output_is_repaired():
    tool_calls = []
    worker, completions, _ = build_worker(
        kimi_response(SCHEMA_VIOLATION), kimi_response(VALID_OUTPUT), tool_calls=tool_calls)

    result = worker.execute(worker_task(tools=("mock.search", "mock.catalog")), {})

    assert result.status == "completed"
    assert len(completions.calls) == 2
    assert [name for name, _ in tool_calls] == ["mock.search", "mock.catalog"]
    assert len(tool_calls) == 2  # one invocation per DECLARED tool, not per attempt


def test_repair_reuses_the_already_validated_tool_and_dependency_material():
    tool_calls = []
    dependencies = {"upstream": {"answer": "dependency result"}}
    worker, completions, _ = build_worker(
        kimi_response(MALFORMED_JSON), kimi_response(VALID_OUTPUT), tool_calls=tool_calls)

    worker.execute(worker_task(tools=("mock.search", "mock.catalog")), dependencies)

    initial, repair = (user_payload(call) for call in completions.calls)
    assert initial["tools"] == {"mock.search": {"rows": ["validated search material"]},
                                "mock.catalog": {"rows": ["validated catalog material"]}}
    # Byte-identical material: no tool re-ran and no dependency was reloaded.
    assert repair["tools"] == initial["tools"]
    assert repair["dependencies"] == initial["dependencies"] == dependencies
    assert repair["goal"] == initial["goal"]
    assert repair["scope"] == initial["scope"]
    assert repair["output_schema"] == initial["output_schema"]
    assert len(tool_calls) == 2


# --- M/N: the malformed completion is never reused and never durable --------

def test_repair_request_carries_the_static_reason_code_and_not_the_raw_output():
    worker, completions, _ = build_worker(
        kimi_response(MALFORMED_JSON), kimi_response(VALID_OUTPUT))

    worker.execute(worker_task(), {})

    repair_call = completions.calls[1]
    system = system_of(repair_call)
    assert "WORKER_OUTPUT_JSON_INVALID" in system
    assert "final attempt" in system
    # The raw malformed completion is deliberately NOT sent back.
    assert SENTINEL not in json.dumps(repair_call, default=str)
    assert MALFORMED_JSON not in json.dumps(repair_call, default=str)
    # Only the request shape the initial call already used.
    assert set(repair_call) == set(completions.calls[0])


def test_malformed_completion_never_reaches_a_durable_surface(monkeypatch):
    """End to end through the real worker entrypoint: the sentinel must not
    appear in run events, the durable run row, or any checkpoint."""
    swarm_env(monkeypatch)
    repo, conversation_id = build_repo()

    class MalformedWorkerCompletions:
        def __init__(self):
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            system = " ".join(m.get("content", "") for m in kwargs.get("messages", [])
                              if m.get("role") == "system")
            if "CommanderPlan JSON Schema" in system:
                return kimi_response(json.dumps(minimal_plan(1)))
            if "CommanderDecision JSON Schema" in system:
                return kimi_response(json.dumps(
                    {"decision": "FINISH", "plan": None, "reason": "complete"}))
            return kimi_response(MALFORMED_JSON)

    completions = MalformedWorkerCompletions()
    run_id = run_worker_directly(repo, conversation_id, monkeypatch, completions,
                                 idempotency_key="b3-hygiene-001")
    assert worker_main.execute_run(run_id, repo) == 0

    run = repo.get_run(run_id)
    assert run["status"] == "failed"
    # plan + one worker attempt + its single repair + the replan decision;
    # the worker never made a third semantic attempt.
    assert len(completions.calls) == 4
    assert run["usage"]["model_calls"] == 4

    durable = json.dumps({
        "run": {key: str(value) for key, value in run.items()},
        "events": [str(event) for event in repo.run_events],
        "checkpoints": [str(checkpoint) for checkpoint in repo.checkpoints],
    })
    assert SENTINEL not in durable
    assert MALFORMED_JSON not in durable
    # The bounded static classification IS durable and reaches the task event.
    assert "WORKER_OUTPUT_JSON_INVALID" in durable


# --- P: the repair event payload is bounded ---------------------------------

def test_repair_event_payload_contains_only_the_approved_bounded_fields():
    events = []
    worker, _, _ = build_worker(kimi_response(SCHEMA_VIOLATION), kimi_response(VALID_OUTPUT),
                                events=events)

    worker.execute(worker_task(), {})

    payloads = repair_events(events)
    assert len(payloads) == 1
    payload = payloads[0]
    assert set(payload) == {"task_id", "reason_code", "attempt_number"}
    assert payload["task_id"] == "only"
    assert payload["reason_code"] in WORKER_OUTPUT_REASONS
    assert payload["attempt_number"] == MAX_WORKER_OUTPUT_MODEL_ATTEMPTS == 2
    assert all(isinstance(value, (str, int)) for value in payload.values())


# --- Q: both semantic calls use the same guarded gateway path ---------------

def test_both_semantic_calls_travel_the_same_model_gateway_and_budget_path():
    agent_steps = []
    worker, completions, tracker = build_worker(
        kimi_response(MALFORMED_JSON), kimi_response(VALID_OUTPUT), agent_steps=agent_steps)

    result = worker.execute(worker_task(), {})

    assert result.status == "completed"
    # Same agent/phase telemetry, same guarded reservation, twice.
    assert agent_steps == [("worker:only", "execute"), ("worker:only", "execute")]
    assert tracker.model_calls == 2
    assert tracker.input_tokens > 0 and tracker.output_tokens > 0
    assert tracker.actual_cost > 0
    for call in completions.calls:
        assert call["model"] == "kimi-k2.6"
        assert call["response_format"] == {"type": "json_object"}
        assert [message["role"] for message in call["messages"]] == ["system", "user"]


# --- section 20: call/usage accounting regression ---------------------------

class RepairingKimiCompletions:
    """Valid plan/decision bodies; the FIRST worker completion is malformed."""

    def __init__(self, *, repair_worker_output):
        self.repair_worker_output = repair_worker_output
        self.worker_calls = 0
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        system = " ".join(m.get("content", "") for m in kwargs.get("messages", [])
                          if m.get("role") == "system")
        if "CommanderPlan JSON Schema" in system:
            return kimi_response(json.dumps(minimal_plan(1)))
        if "CommanderDecision JSON Schema" in system:
            return kimi_response(json.dumps(
                {"decision": "FINISH", "plan": None, "reason": "complete"}))
        self.worker_calls += 1
        if self.repair_worker_output and self.worker_calls == 1:
            return kimi_response(MALFORMED_JSON)
        return kimi_response(VALID_OUTPUT)


@pytest.mark.parametrize("repaired,expected_model_calls,expected_retries", [
    (False, 3, 0),   # plan + worker + replan
    (True, 4, 1),    # plan + worker + ONE repair + replan
])
def test_a_repaired_worker_costs_exactly_one_extra_model_call(
        monkeypatch, repaired, expected_model_calls, expected_retries):
    """One logical worker task with two semantic model calls -- not two
    agents, and not a free retry."""
    swarm_env(monkeypatch)
    repo, conversation_id = build_repo()
    completions = RepairingKimiCompletions(repair_worker_output=repaired)
    run_id = run_worker_directly(repo, conversation_id, monkeypatch, completions,
                                 idempotency_key=f"b3-usage-{int(repaired)}0001")

    assert worker_main.execute_run(run_id, repo) == 0

    run = repo.get_run(run_id)
    assert run["status"] == "completed"
    assert len(completions.calls) == expected_model_calls
    assert run["usage"]["model_calls"] == expected_model_calls
    # A worker repair IS a semantic retry; provider backpressure is not.
    assert run["usage"]["retries"] == expected_retries
    assert run["usage"]["provider_backpressure_events"] == 0


# --- the shared validation helper -------------------------------------------

SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}},
          "required": ["answer"], "additionalProperties": False}


@pytest.mark.parametrize("completion,reason", [
    (f"not json {SENTINEL}", "WORKER_OUTPUT_JSON_INVALID"),
    ("", "WORKER_OUTPUT_JSON_INVALID"),
    (None, "WORKER_OUTPUT_JSON_INVALID"),
    (["chunked", "content"], "WORKER_OUTPUT_JSON_INVALID"),
    ('{"answer": 7}', "WORKER_OUTPUT_SCHEMA_INVALID"),
    ('{"answer": "ok", "extra": "' + SENTINEL + '"}', "WORKER_OUTPUT_SCHEMA_INVALID"),
    ('{"wrong": "ok"}', "WORKER_OUTPUT_SCHEMA_INVALID"),
    ('["not", "an", "object"]', "WORKER_OUTPUT_SCHEMA_INVALID"),
])
def test_one_validator_classifies_json_and_schema_failures_without_raw_material(
        completion, reason):
    with pytest.raises(WorkerOutputValidationError) as failure:
        validate_worker_output(completion, SCHEMA)
    assert failure.value.reason_code == reason
    assert SENTINEL not in str(failure.value)
    assert SENTINEL not in repr(failure.value.args)


@pytest.mark.parametrize("completion", ['{"answer": "ok"}', b'{"answer": "ok"}',
                                        {"answer": "ok"}])
def test_validator_accepts_every_shape_the_gateway_can_deliver(completion):
    assert validate_worker_output(completion, SCHEMA) == {"answer": "ok"}


def test_reason_codes_are_a_closed_static_allowlist():
    with pytest.raises(ValueError, match="static allowlist"):
        WorkerOutputValidationError("SOMETHING_ELSE")
    with pytest.raises(ValueError, match="static allowlist"):
        build_worker_request(worker_task(), {}, {}, repair_reason="SOMETHING_ELSE")


def test_the_initial_request_carries_no_repair_instruction():
    initial = build_worker_request(worker_task(), {"mock.search": {"rows": []}}, {})
    repair = build_worker_request(worker_task(), {"mock.search": {"rows": []}}, {},
                                  repair_reason="WORKER_OUTPUT_SCHEMA_INVALID")
    assert "static reason code" not in system_of({"messages": initial})
    assert "WORKER_OUTPUT_SCHEMA_INVALID" in system_of({"messages": repair})
    # Same trusted material either way; only the instruction differs.
    assert user_payload({"messages": initial}) == user_payload({"messages": repair})

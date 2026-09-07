"""The structured Swarm V2 tool-invocation contract (R2).

The blocker this replaces: `ToolRequirement` named a tool, a free-text scope
and a `max_calls` promise, and `GenericWorker` invoked every declared tool
once with `{"query": task.goal}`. Any operation needing real arguments --
`make` and `model`, say -- therefore failed with `TOOL_INPUT_INVALID` no
matter what the plan intended, dependency outputs could not reach a payload
at all, and results keyed by tool name made a second call to the same tool
unrepresentable.

Everything here is offline: fixture tools, fake gateways, no network and no
paid call. The production ToolRegistry stays empty; these tools are fixtures.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from backend.engines.swarm_v2 import (
    MAX_TASK_OUTPUT_JSON_BYTES,
    MAX_TOOL_CALLS_PER_TASK,
    MAX_TOOL_INPUT_JSON_BYTES,
    MAX_TOOL_MATERIAL_JSON_BYTES,
    MAX_TOOL_OUTPUT_JSON_BYTES,
    TOOL_CALL_REASONS,
    BoundedTaskExecutor,
    GenericWorker,
    PlanLimits,
    PlanValidationError,
    PlanValidator,
    RemainingBudget,
    SwarmState,
    SwarmV2Engine,
    TaskGraph,
    ToolCallError,
    ToolCallRecord,
    Verifier,
)
from backend.tools import (MockSearchTool, MockVehicleCatalogTool, ToolContext, ToolError,
                           ToolMode, ToolOperation, ToolRegistry)

from test_swarm_v2 import call, plan, task
from test_swarm_v2_stage1_e2e import (Plans, StubResolver, VerifyGateway, Worker,
                                      commander, evidence)

CATALOG = {"Toyota": ("Corolla", "Yaris"), "Mazda": ("3",)}
SCOPES = frozenset({"mock:vehicle_catalog", "mock:search", "fixture:bulk"})
CONTEXT = ToolContext(scopes=SCOPES)

OUTPUT_SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}},
                 "required": ["answer"], "additionalProperties": False}
ROWS_SCHEMA = {"type": "object",
               "properties": {"rows": {"type": "array", "items": {"type": "string"}}},
               "required": ["rows"], "additionalProperties": False}
SIZE_SCHEMA = {"type": "object", "properties": {"size": {"type": "integer"}},
               "required": ["size"], "additionalProperties": False}


# --- offline fixtures -------------------------------------------------------

@dataclass(frozen=True)
class BulkTool:
    """Returns a result whose size the caller chooses, to probe the bounds."""

    name: str = "fixture.bulk"
    description: str = "offline fixture returning sized payloads"
    required_scope: str = "fixture:bulk"
    mode: ToolMode = ToolMode.READ
    operations = {"emit": ToolOperation("emit", "Emit one row of the requested size.",
                                        SIZE_SCHEMA, ROWS_SCHEMA)}

    def execute(self, context: ToolContext, operation: str,
                payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return {"rows": ["x" * int(payload["size"])]}


@dataclass(frozen=True)
class WriteTool:
    name: str = "fixture.writer"
    description: str = "offline fixture write tool"
    required_scope: str = "fixture:bulk"
    mode: ToolMode = ToolMode.WRITE
    operations = {"emit": ToolOperation("emit", "Write one row.", SIZE_SCHEMA, ROWS_SCHEMA)}

    def execute(self, context: ToolContext, operation: str,
                payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return {"rows": ["written"]}


def registry(*extra):
    return ToolRegistry([MockVehicleCatalogTool(CATALOG),
                         MockSearchTool({"q": ("row",)}), *extra])


class StubGateway:
    """A worker model that returns scripted completions and records calls.

    A dict body is returned directly (the shape the worker accepts inline);
    a string body is wrapped in the provider completion shape, so malformed
    and oversized completions travel the real decode path.
    """

    def __init__(self, *bodies, timeline=None):
        self.bodies = list(bodies) or [{"answer": "ok"}]
        self.calls: list[dict] = []
        self.timeline = timeline

    def call(self, **kwargs):
        self.calls.append(kwargs)
        if self.timeline is not None:
            self.timeline.append("model")
        body = self.bodies[min(len(self.calls) - 1, len(self.bodies) - 1)]
        if isinstance(body, dict):
            return body
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=body))])


def build_worker(*bodies, tools=None, events=None, records=None, context=CONTEXT):
    gateway = StubGateway(*bodies)
    worker = GenericWorker(
        gateway=gateway, tools=tools if tools is not None else registry(),
        model="fake", tool_context=context,
        event_sink=(None if events is None else lambda kind, payload: events.append((kind, payload))),
        tool_result_sink=(None if records is None else records.append))
    return worker, gateway


def spec(*calls, task_id="lookup", dependencies=()):
    """One task whose tools list is exactly the supplied planned calls."""
    return TaskGraph.model_validate({"tasks": [{
        "task_id": task_id, "goal": "identify the vehicle",
        "scope": "offline fixture material only", "dependencies": list(dependencies),
        "tools": list(calls), "output_schema": OUTPUT_SCHEMA,
        "evidence": {"minimum_sources": 0, "required_fields": [], "min_confidence": 0.0},
        "priority": 1, "recursion_depth": 0, "estimated_cost_units": 1,
        "completion": {"required_outputs": ["answer"], "evidence_satisfied": False,
                       "allow_partial": False},
    }]}).tasks[0]


def get_model(call_id="vehicle_lookup", *, arguments=None, bindings=None):
    return call(call_id, "mock.vehicle_catalog", operation="get_model",
                arguments={"make": "Toyota", "model": "Corolla"} if arguments is None
                else arguments,
                bindings=bindings)


def catalog_validator(**limits):
    return PlanValidator(allowed_tools=registry(BulkTool(), WriteTool()).descriptors(),
                         limits=PlanLimits(**limits))


def tool_task(*calls, task_id="lookup", dependencies=(), **overrides):
    """A plan-shaped task carrying exactly the supplied planned calls."""
    planned = task(task_id, f"goal {task_id}", dependencies=list(dependencies))
    planned["tools"] = list(calls)
    planned.update(overrides)
    return planned


# --- A. the regression this PR removes --------------------------------------

def test_a_structured_operation_cannot_be_satisfied_by_a_goal_shaped_query():
    """The exact pre-R2 failure, pinned so it cannot come back.

    `{"query": task.goal}` was the only payload the worker could build. A
    `get_model(make, model)` operation rejects it at the Registry boundary,
    which is why a structured tool was unreachable before this contract.
    """
    with pytest.raises(ToolError) as failure:
        registry().execute("mock.vehicle_catalog", "get_model", CONTEXT,
                           {"query": "identify the vehicle"})
    assert failure.value.code == "TOOL_INPUT_INVALID"


def test_the_structured_make_model_call_now_runs_with_the_exact_arguments():
    records = []
    worker, gateway = build_worker({"answer": "Toyota Corolla"}, records=records)

    result = worker.execute(spec(get_model()), {})

    assert result.status == "completed"
    assert result.output == {"answer": "Toyota Corolla"}
    # The fixture echoes its resolved arguments, so this is proof the tool ran
    # with make="Toyota" and model="Corolla" -- not with a guessed payload.
    material = json.loads(gateway.calls[0]["messages"][1]["content"])["tools"]
    assert material == {"vehicle_lookup": {"rows": ["Toyota Corolla"]}}
    assert [record.result for record in records] == [{"rows": ["Toyota Corolla"]}]


def test_a_literal_only_call_succeeds():
    worker, gateway = build_worker({"answer": "ok"})
    result = worker.execute(spec(call("rows", "mock.search", arguments={"query": "q"})), {})
    assert result.status == "completed"
    assert json.loads(gateway.calls[0]["messages"][1]["content"])["tools"] == {
        "rows": {"rows": ["row"]}}


# --- B. dependency-bound arguments ------------------------------------------

def test_a_dependency_bound_argument_is_resolved_in_trusted_code():
    worker, gateway = build_worker({"answer": "ok"})
    planned = get_model(bindings=[{"argument": "model", "task_id": "normalize_identity",
                                   "path": ["model"]}],
                        arguments={"make": "Toyota"})

    result = worker.execute(spec(planned, dependencies=["normalize_identity"]),
                            {"normalize_identity": {"model": "Corolla"}})

    assert result.status == "completed"
    assert json.loads(gateway.calls[0]["messages"][1]["content"])["tools"] == {
        "vehicle_lookup": {"rows": ["Toyota Corolla"]}}


def test_a_bounded_array_index_path_resolves():
    worker, gateway = build_worker({"answer": "ok"})
    planned = get_model(arguments={"make": "Toyota"},
                        bindings=[{"argument": "model", "task_id": "upstream",
                                   "path": ["models", 1]}])

    result = worker.execute(spec(planned, dependencies=["upstream"]),
                            {"upstream": {"models": ["Yaris", "Corolla"]}})

    assert result.status == "completed"
    assert json.loads(gateway.calls[0]["messages"][1]["content"])["tools"] == {
        "vehicle_lookup": {"rows": ["Toyota Corolla"]}}


@pytest.mark.parametrize("path,dependency,code", [
    (["missing"], {"model": "Corolla"}, "TOOL_BINDING_UNRESOLVED"),
    (["model", "deeper"], {"model": "Corolla"}, "TOOL_BINDING_UNRESOLVED"),
    (["models", 5], {"models": ["Corolla"]}, "TOOL_BINDING_UNRESOLVED"),
    (["model", 0], {"model": "Corolla"}, "TOOL_BINDING_UNRESOLVED"),
])
def test_an_unresolvable_path_fails_the_task_safely(path, dependency, code):
    worker, gateway = build_worker({"answer": "ok"})
    planned = get_model(arguments={"make": "Toyota"},
                        bindings=[{"argument": "model", "task_id": "upstream", "path": path}])

    result = worker.execute(spec(planned, dependencies=["upstream"]), {"upstream": dependency})

    assert result.status == "failed"
    assert result.error["code"] == code
    assert gateway.calls == []  # no model call is paid for after a bad binding


def test_only_direct_dependency_outputs_are_readable_at_runtime():
    """Defence in depth: the executor supplies only direct dependencies, and
    the resolver refuses anything else even if a plan slipped through."""
    worker, _ = build_worker({"answer": "ok"})
    planned = get_model(arguments={"make": "Toyota"},
                        bindings=[{"argument": "model", "task_id": "unrelated",
                                   "path": ["model"]}])

    result = worker.execute(spec(planned, dependencies=["upstream"]),
                            {"upstream": {"model": "Corolla"}})

    assert result.status == "failed"
    assert result.error["code"] == "TOOL_BINDING_UNKNOWN_DEPENDENCY"


def test_the_executor_hands_a_worker_only_its_direct_dependency_outputs():
    seen = {}

    class Capturing(Worker):
        def execute(self, task_spec, dependencies):
            seen[task_spec.task_id] = dict(dependencies)
            return super().execute(task_spec, dependencies)

    graph = TaskGraph.model_validate({"tasks": [
        tool_task(task_id="root"), tool_task(task_id="mid", dependencies=["root"]),
        tool_task(task_id="leaf", dependencies=["mid"])]})
    BoundedTaskExecutor(worker_factory=lambda: Capturing([]), max_active_workers=1).execute(graph)

    assert set(seen["leaf"]) == {"mid"}  # never the transitive "root"


# --- C. repeat calls, identity and ordering ---------------------------------

def test_the_same_tool_can_be_called_twice_under_distinct_call_ids():
    worker, gateway = build_worker({"answer": "ok"})

    result = worker.execute(spec(
        get_model("first", arguments={"make": "Toyota", "model": "Corolla"}),
        get_model("second", arguments={"make": "Mazda", "model": "3"})), {})

    assert result.status == "completed"
    # Keyed by call_id, so neither result overwrites the other.
    assert json.loads(gateway.calls[0]["messages"][1]["content"])["tools"] == {
        "first": {"rows": ["Toyota Corolla"]}, "second": {"rows": ["Mazda 3"]}}


def test_calls_run_once_each_in_declaration_order():
    seen = []

    class Ordered(MockSearchTool):
        def execute(self, context, operation, payload):
            seen.append(payload["query"])
            return {"rows": []}

    worker, _ = build_worker({"answer": "ok"}, tools=ToolRegistry([Ordered()]))
    worker.execute(spec(call("c1", "mock.search", arguments={"query": "one"}),
                        call("c2", "mock.search", arguments={"query": "two"}),
                        call("c3", "mock.search", arguments={"query": "three"})), {})
    assert seen == ["one", "two", "three"]


def test_a_failing_call_stops_the_task_before_any_later_call():
    seen = []

    class FailsSecond(MockSearchTool):
        def execute(self, context, operation, payload):
            seen.append(payload["query"])
            if payload["query"] == "two":
                raise RuntimeError("fixture failure")
            return {"rows": []}

    worker, gateway = build_worker({"answer": "ok"}, tools=ToolRegistry([FailsSecond()]))
    result = worker.execute(spec(call("c1", "mock.search", arguments={"query": "one"}),
                                 call("c2", "mock.search", arguments={"query": "two"}),
                                 call("c3", "mock.search", arguments={"query": "three"})), {})

    assert result.status == "failed"
    assert result.error["code"] == "TOOL_EXECUTION_FAILED"
    assert seen == ["one", "two"]  # "three" is never attempted
    assert gateway.calls == []


def test_a_duplicate_call_id_is_refused_by_the_worker_as_well():
    """The firewall rejects duplicates; the worker refuses them anyway."""
    worker, _ = build_worker({"answer": "ok"})
    graph = TaskGraph.model_validate({"tasks": [{
        "task_id": "lookup", "goal": "identify", "scope": "offline", "dependencies": [],
        "tools": [get_model("same"), get_model("same", arguments={"make": "Mazda",
                                                                  "model": "3"})],
        "output_schema": OUTPUT_SCHEMA,
        "evidence": {"minimum_sources": 0, "required_fields": [], "min_confidence": 0.0},
        "priority": 1, "recursion_depth": 0, "estimated_cost_units": 1,
        "completion": {"required_outputs": ["answer"], "evidence_satisfied": False,
                       "allow_partial": False}}]}).tasks[0]

    result = worker.execute(graph, {})
    assert result.status == "failed"
    assert result.error["code"] == "DUPLICATE_TOOL_CALL_ID"


# --- D. the deterministic plan firewall -------------------------------------

@pytest.mark.parametrize("planned,reason", [
    (call("c", "shell.exec", operation="run", arguments={}), "TOOL_NOT_ALLOWLISTED"),
    ({**get_model(), "operation": "drop_table"}, "TOOL_OPERATION_UNKNOWN"),
    ({**get_model(), "arguments": {"make": "Toyota"}}, "TOOL_ARGUMENTS_INVALID"),
    ({**get_model(), "arguments": {"make": "Toyota", "model": "C", "extra": "x"}},
     "TOOL_ARGUMENTS_INVALID"),
    ({**get_model(), "arguments": {"make": "Toyota", "model": 7}}, "TOOL_ARGUMENTS_INVALID"),
    ({**get_model(), "arguments": {"make": "T" * 5000, "model": "C"}},
     "TOOL_ARGUMENTS_TOO_LARGE"),
])
def test_the_firewall_rejects_a_bad_call_with_one_static_reason(planned, reason):
    with pytest.raises(PlanValidationError) as failure:
        catalog_validator().validate(plan([tool_task(planned)]))
    assert failure.value.reason == reason


def test_deeply_nested_arguments_are_rejected_before_execution():
    nested: Any = "leaf"
    for _ in range(12):
        nested = {"deeper": nested}
    with pytest.raises(PlanValidationError) as failure:
        catalog_validator().validate(plan([tool_task(
            {**get_model(), "arguments": {"make": "Toyota", "model": nested}})]))
    assert failure.value.reason == "TOOL_ARGUMENTS_TOO_LARGE"


def test_a_duplicate_call_id_within_a_task_is_rejected():
    with pytest.raises(PlanValidationError) as failure:
        catalog_validator().validate(plan([tool_task(
            get_model("same"),
            get_model("same", arguments={"make": "Mazda", "model": "3"}))]))
    assert failure.value.reason == "DUPLICATE_TOOL_CALL_ID"


def test_the_same_call_id_may_be_reused_in_a_different_task():
    """`call_id` is unique within a task, which is the executable scope."""
    candidate = plan([tool_task(get_model("lookup"), task_id="a"),
                      tool_task(get_model("lookup"), task_id="b", goal="different goal")])
    assert catalog_validator().validate(candidate)


@pytest.mark.parametrize("binding,reason", [
    ({"argument": "model", "task_id": "unrelated", "path": ["model"]},
     "TOOL_BINDING_UNKNOWN_DEPENDENCY"),
    ({"argument": "model", "task_id": "root", "path": ["*"]}, "TOOL_BINDING_PATH_INVALID"),
    ({"argument": "model", "task_id": "root", "path": ["$..model"]},
     "TOOL_BINDING_PATH_INVALID"),
    ({"argument": "model", "task_id": "root", "path": ["items[0]"]},
     "TOOL_BINDING_PATH_INVALID"),
    ({"argument": "model", "task_id": "root", "path": ["a", "b", "c", "d", "e", "f", "g"]},
     "SCHEMA_CONSTRAINT_FAILED"),
    ({"argument": "model", "task_id": "root", "path": ["k" * 80]},
     "TOOL_BINDING_PATH_INVALID"),
    ({"argument": "model", "task_id": "root", "path": [-1]}, "TOOL_BINDING_PATH_INVALID"),
    ({"argument": "make", "task_id": "root", "path": ["make"]}, "TOOL_BINDING_CONFLICT"),
    ({"argument": "brand", "task_id": "root", "path": ["brand"]}, "TOOL_ARGUMENTS_INVALID"),
])
def test_the_firewall_rejects_a_bad_dependency_binding(binding, reason):
    planned = {**get_model(), "arguments": {"make": "Toyota"},
               "dependency_bindings": [binding]}
    candidate = plan([tool_task(task_id="root"),
                      tool_task(planned, task_id="child", dependencies=["root"],
                                goal="child goal")],
                     contexts={"child": ["root"]})
    with pytest.raises(PlanValidationError) as failure:
        catalog_validator().validate(candidate)
    assert failure.value.reason == reason


def test_two_bindings_may_not_target_the_same_argument():
    planned = {**get_model(), "arguments": {},
               "dependency_bindings": [
                   {"argument": "make", "task_id": "root", "path": ["make"]},
                   {"argument": "make", "task_id": "root", "path": ["other"]}]}
    candidate = plan([tool_task(task_id="root"),
                      tool_task(planned, task_id="child", dependencies=["root"],
                                goal="child goal")],
                     contexts={"child": ["root"]})
    with pytest.raises(PlanValidationError) as failure:
        catalog_validator().validate(candidate)
    assert failure.value.reason == "TOOL_BINDING_DUPLICATE"


def test_required_arguments_may_be_supplied_by_literals_bindings_or_both():
    planned = {**get_model(), "arguments": {"make": "Toyota"},
               "dependency_bindings": [
                   {"argument": "model", "task_id": "root", "path": ["model"]}]}
    candidate = plan([tool_task(task_id="root"),
                      tool_task(planned, task_id="child", dependencies=["root"],
                                goal="child goal")],
                     contexts={"child": ["root"]})
    assert catalog_validator().validate(candidate)


def test_a_transitive_dependency_is_not_a_direct_one():
    planned = {**get_model(), "arguments": {"make": "Toyota"},
               "dependency_bindings": [
                   {"argument": "model", "task_id": "root", "path": ["model"]}]}
    candidate = plan([tool_task(task_id="root"),
                      tool_task(task_id="mid", dependencies=["root"], goal="mid goal"),
                      tool_task(planned, task_id="leaf", dependencies=["mid"],
                                goal="leaf goal")],
                     contexts={"mid": ["root"], "leaf": ["root", "mid"]})
    with pytest.raises(PlanValidationError) as failure:
        catalog_validator().validate(candidate)
    assert failure.value.reason == "TOOL_BINDING_UNKNOWN_DEPENDENCY"


def test_an_unregistered_tool_or_operation_never_reaches_execution():
    executed = []

    class Recording(MockSearchTool):
        def execute(self, context, operation, payload):
            executed.append(operation)
            return {"rows": []}

    validator = PlanValidator(allowed_tools=ToolRegistry([Recording()]).descriptors())
    for planned in ({**get_model(), "name": "mock.search"},
                    call("c", "mock.search", operation="exfiltrate", arguments={"query": "q"})):
        with pytest.raises(PlanValidationError):
            validator.validate(plan([tool_task(planned)]))
    assert executed == []


def test_every_tool_call_reason_is_a_static_upper_case_code():
    assert all(reason.isupper() and reason.replace("_", "").isalpha()
               for reason in TOOL_CALL_REASONS)


# --- E. Registry authority at execution time --------------------------------

def test_the_registry_revalidates_the_resolved_payload_and_the_result():
    class BadOutput(MockVehicleCatalogTool):
        def execute(self, context, operation, payload):
            return {"rows": [7]}

    bad = ToolRegistry([BadOutput(CATALOG)])
    with pytest.raises(ToolError) as output:
        bad.execute("mock.vehicle_catalog", "get_model", CONTEXT,
                    {"make": "Toyota", "model": "Corolla"})
    assert output.value.code == "TOOL_OUTPUT_INVALID"

    # Each operation is validated against its OWN schema, not a shared one.
    with pytest.raises(ToolError) as wrong:
        registry().execute("mock.vehicle_catalog", "list_models", CONTEXT,
                           {"make": "Toyota", "model": "Corolla"})
    assert wrong.value.code == "TOOL_INPUT_INVALID"
    assert registry().execute("mock.vehicle_catalog", "list_models", CONTEXT,
                              {"make": "Toyota"}) == {"rows": ["Corolla", "Yaris"]}


def test_read_scope_is_enforced_by_the_server_owned_tool_context():
    worker, gateway = build_worker({"answer": "ok"}, context=ToolContext())
    result = worker.execute(spec(get_model()), {})
    assert result.status == "failed"
    assert result.error["code"] == "TOOL_SCOPE_REQUIRED"
    assert gateway.calls == []


def test_a_write_operation_needs_both_approval_and_capability():
    writes = ToolRegistry([WriteTool()])
    payload = {"size": 1}
    for context in (ToolContext(scopes=frozenset({"fixture:bulk"})),
                    ToolContext(scopes=frozenset({"fixture:bulk"}), write_approved=True),
                    ToolContext(scopes=frozenset({"fixture:bulk"}),
                                capabilities=frozenset({"tool:write:fixture.writer"}))):
        with pytest.raises(ToolError) as denied:
            writes.execute("fixture.writer", "emit", context, payload)
        assert denied.value.code == "TOOL_WRITE_NOT_APPROVED"
    approved = ToolContext(scopes=frozenset({"fixture:bulk"}), write_approved=True,
                           capabilities=frozenset({"tool:write:fixture.writer"}))
    assert writes.execute("fixture.writer", "emit", approved, payload) == {"rows": ["written"]}


def test_an_approved_plan_cannot_grant_itself_a_write_capability():
    """A plan may REQUEST a registered capability; it can never grant one."""
    validated = catalog_validator().validate(plan([tool_task(
        call("write", "fixture.writer", operation="emit", arguments={"size": 1}))]))
    assert validated.graph.tasks[0].tools[0].name == "fixture.writer"

    worker, _ = build_worker({"answer": "ok"}, tools=registry(WriteTool()),
                             context=ToolContext(scopes=SCOPES))
    result = worker.execute(spec(call("write", "fixture.writer", operation="emit",
                                      arguments={"size": 1})), {})
    assert result.status == "failed"
    assert result.error["code"] == "TOOL_WRITE_NOT_APPROVED"


# --- F. descriptors ---------------------------------------------------------

def test_descriptors_are_deterministic_sanitized_and_registry_derived():
    first, second = registry(BulkTool()).descriptors(), registry(BulkTool()).descriptors()
    assert [item.name for item in first] == ["fixture.bulk", "mock.search",
                                             "mock.vehicle_catalog"]
    assert json.dumps([item.as_payload() for item in first], sort_keys=True) == \
        json.dumps([item.as_payload() for item in second], sort_keys=True)

    catalog = registry(BulkTool()).descriptor_payload()
    vehicle = next(item for item in catalog if item["name"] == "mock.vehicle_catalog")
    assert [op["name"] for op in vehicle["operations"]] == ["get_model", "list_models"]
    assert vehicle["mode"] == "read"
    assert vehicle["required_scope"] == "mock:vehicle_catalog"
    assert vehicle["operations"][0]["input_schema"]["required"] == ["make", "model"]
    # Only plain JSON: no runtime object, callback, credential or grant.
    assert json.loads(json.dumps(catalog)) == catalog
    for forbidden in ("execute", "records", "api_key", "token", "capabilit",
                      "write_approved", "scopes"):
        assert forbidden not in json.dumps(catalog)


def test_a_descriptor_schema_is_detached_from_the_live_tool():
    tool = MockVehicleCatalogTool(CATALOG)
    descriptor = ToolRegistry([tool]).descriptors()[0]
    descriptor.operation("get_model").input_schema["properties"]["injected"] = {"type": "string"}
    assert "injected" not in tool.operations["get_model"].input_schema["properties"]


def test_an_oversized_descriptor_catalog_is_refused_at_registration():
    huge = {"type": "object", "additionalProperties": False, "required": [],
            "properties": {f"field_{index}": {"type": "string"} for index in range(4000)}}

    @dataclass(frozen=True)
    class Huge:
        name: str = "fixture.huge"
        description: str = "huge fixture"
        required_scope: str = "fixture:bulk"
        mode: ToolMode = ToolMode.READ
        operations = {"emit": ToolOperation("emit", "huge", huge, ROWS_SCHEMA)}

        def execute(self, context, operation, payload):
            return {"rows": []}

    with pytest.raises(ValueError, match="prompt size bound"):
        ToolRegistry([Huge()])


def test_the_commander_prompt_carries_operations_and_schemas():
    from backend.engines.swarm_v2 import ModelGateway
    from backend.provider_scheduler import ProviderLimitsConfig, ProviderScheduler
    from types import SimpleNamespace

    calls = []

    class Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps({"decision": "FINISH",
                                                            "plan": None, "reason": "done"})))])

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    gateway = ModelGateway(
        guarded_client_factory=lambda *_: client,
        scheduler=ProviderScheduler(ProviderLimitsConfig(
            max_concurrency=1, rpm_limit=None, max_rate_limit_retries=0,
            max_backpressure_wait_seconds=1, backoff_base_seconds=.001,
            backoff_max_seconds=.001)),
        api_key="offline", base_url="offline",
        tool_descriptors=registry().descriptors())
    gateway.create_replan(model="fake", objective="offline", summary={})
    gateway.create_plan(model="fake", objective="offline",
                        context={"allowed_tools": ["evil.write"], "grant": "write"})

    # BOTH the planning and the replanning prompt carry the same catalog.
    for kwargs in calls:
        system = kwargs["messages"][0]["content"]
        assert "Server-authorized tool catalog:" in system
        assert '"get_model"' in system and '"list_models"' in system
        assert '"required":["make","model"]' in system
        assert "evil.write" not in system


# --- G. budgets and size bounds ---------------------------------------------

def test_the_exact_call_count_drives_plan_and_remaining_budget_checks():
    calls = []
    planned = tool_task(get_model("one"), get_model("two", arguments={"make": "Mazda",
                                                                     "model": "3"}))
    client = Plans(plan([planned]), [{"decision": "FINISH", "plan": None, "reason": "done"}])
    budgets = []

    def remaining():
        budgets.append(True)
        return RemainingBudget(cost_units=100, tool_calls=len(budgets) and 2,
                               tasks=10, model_calls=10)

    engine = SwarmV2Engine(
        commander=commander_over(registry().descriptors(), client),
        executor=BoundedTaskExecutor(worker_factory=lambda: Worker(calls),
                                     max_active_workers=1),
        verifier=Verifier(gateway=VerifyGateway(), model="fake", resolver=StubResolver()),
        evidence_loader=evidence, remaining_budget=remaining)
    engine.run({"id": "run-1", "input": {"objective": "budget", "commander_model": "fake"}})
    assert calls == ["lookup"]  # exactly 2 planned calls fit a 2-call budget

    engine = SwarmV2Engine(
        commander=commander_over(registry().descriptors(), Plans(plan([planned]), [])),
        executor=BoundedTaskExecutor(worker_factory=lambda: Worker([]), max_active_workers=1),
        verifier=Verifier(gateway=VerifyGateway(), model="fake", resolver=StubResolver()),
        remaining_budget=lambda: RemainingBudget(cost_units=100, tool_calls=1, tasks=10,
                                                 model_calls=10))
    with pytest.raises(ValueError, match="remaining budget"):
        engine.run({"id": "run-2", "input": {"objective": "budget", "commander_model": "fake"}})


def commander_over(descriptors, client):
    from backend.engines.swarm_v2 import Commander, CommanderModelResolver
    return Commander(client=client, resolver=CommanderModelResolver(("fake",), {"fake"}),
                     validator=PlanValidator(allowed_tools=descriptors,
                                             limits=PlanLimits(max_tasks=10)))


def test_the_per_task_and_aggregate_call_limits_are_both_enforced():
    assert MAX_TOOL_CALLS_PER_TASK == 4
    over_task = tool_task(*(get_model(f"c{index}",
                                      arguments={"make": "Toyota", "model": f"m{index}"})
                            for index in range(MAX_TOOL_CALLS_PER_TASK + 1)))
    with pytest.raises(PlanValidationError) as per_task:
        catalog_validator().validate(plan([over_task]))
    assert per_task.value.reason == "TASK_TOOL_CALL_LIMIT"

    spread = [tool_task(get_model("a"), get_model("b", arguments={"make": "Mazda",
                                                                 "model": "3"}),
                        task_id=f"t{index}", goal=f"goal {index}")
              for index in range(3)]
    with pytest.raises(PlanValidationError) as aggregate:
        catalog_validator(max_tool_calls=5).validate(plan(spread))
    assert aggregate.value.reason == "AGGREGATE_TOOL_CALL_LIMIT"
    assert catalog_validator(max_tool_calls=6).validate(plan(spread))


@pytest.mark.parametrize("size,code", [
    (MAX_TOOL_OUTPUT_JSON_BYTES + 100, "TOOL_OUTPUT_TOO_LARGE"),
])
def test_an_oversized_tool_output_is_rejected_safely(size, code):
    worker, gateway = build_worker({"answer": "ok"}, tools=registry(BulkTool()))
    result = worker.execute(spec(call("bulk", "fixture.bulk", operation="emit",
                                      arguments={"size": size})), {})
    assert result.status == "failed"
    assert result.error == {"code": code,
                            "message": ToolCallError(code).safe_message}
    assert gateway.calls == []  # never reaches a model prompt


def test_oversized_combined_tool_material_is_rejected_before_the_prompt():
    each = MAX_TOOL_OUTPUT_JSON_BYTES - 200
    assert each * 3 > MAX_TOOL_MATERIAL_JSON_BYTES
    worker, gateway = build_worker({"answer": "ok"}, tools=registry(BulkTool()))
    result = worker.execute(spec(*(call(f"bulk{index}", "fixture.bulk", operation="emit",
                                        arguments={"size": each}) for index in range(3))), {})
    assert result.status == "failed"
    assert result.error["code"] == "TOOL_MATERIAL_TOO_LARGE"
    assert gateway.calls == []


def test_an_oversized_bound_argument_is_rejected_before_the_tool_runs():
    executed = []

    class Recording(MockVehicleCatalogTool):
        def execute(self, context, operation, payload):
            executed.append(payload)
            return {"rows": []}

    worker, _ = build_worker({"answer": "ok"}, tools=ToolRegistry([Recording(CATALOG)]))
    planned = get_model(arguments={"make": "Toyota"},
                        bindings=[{"argument": "model", "task_id": "upstream",
                                   "path": ["model"]}])
    result = worker.execute(spec(planned, dependencies=["upstream"]),
                            {"upstream": {"model": "m" * (MAX_TOOL_INPUT_JSON_BYTES + 10)}})

    assert result.status == "failed"
    assert result.error["code"] == "TOOL_ARGUMENTS_TOO_LARGE"
    assert executed == []


def test_an_oversized_worker_output_is_rejected_without_a_repair():
    huge = json.dumps({"answer": "a" * (MAX_TASK_OUTPUT_JSON_BYTES + 10)})
    worker, gateway = build_worker(huge, huge)
    result = worker.execute(spec(get_model()), {})
    assert result.status == "failed"
    assert result.error["code"] == "TASK_OUTPUT_TOO_LARGE"
    assert len(gateway.calls) == 1  # an oversized completion earns no repair


def test_the_size_bounds_never_persist_the_rejected_payload():
    sentinel = "OVERSIZED_PAYLOAD_SENTINEL"
    worker, _ = build_worker({"answer": "ok"}, tools=registry(BulkTool()))
    result = worker.execute(spec(call("bulk", "fixture.bulk", operation="emit",
                                      arguments={"size": MAX_TOOL_OUTPUT_JSON_BYTES + 1})), {})
    assert sentinel not in json.dumps(result.error)
    assert "x" * 100 not in json.dumps(result.error)


# --- H. events and the bounded repair ---------------------------------------

def test_tool_events_expose_identifiers_but_never_arguments_or_results():
    events = []
    worker, _ = build_worker({"answer": "ok"}, events=events)
    worker.execute(spec(get_model("first"),
                        get_model("second", arguments={"make": "Mazda", "model": "3"})), {})

    called = [payload for kind, payload in events if kind == "tool_called"]
    assert called == [
        {"task_id": "lookup", "tool": "mock.vehicle_catalog", "call_id": "first",
         "operation": "get_model"},
        {"task_id": "lookup", "tool": "mock.vehicle_catalog", "call_id": "second",
         "operation": "get_model"},
    ]
    encoded = json.dumps(events)
    for forbidden in ("Toyota", "Corolla", "Mazda", "rows", "arguments"):
        assert forbidden not in encoded


def test_the_engine_event_sink_passes_the_new_identifiers_through():
    forwarded = []
    engine = SwarmV2Engine(commander=None, event_sink=lambda kind, payload:
                           forwarded.append((kind, payload)))
    engine._emit("tool_called", {"task_id": "t", "tool": "mock.vehicle_catalog",
                                 "call_id": "c", "operation": "get_model",
                                 "arguments": {"make": "Toyota"}, "result": {"rows": []}})
    assert forwarded == [("tool_called", {"task_id": "t", "tool": "mock.vehicle_catalog",
                                          "call_id": "c", "operation": "get_model"})]


def test_the_output_repair_reuses_captured_results_and_never_calls_a_tool_again():
    executed = []

    class Counting(MockVehicleCatalogTool):
        def execute(self, context, operation, payload):
            executed.append(payload)
            return {"rows": ["Toyota Corolla"]}

    worker, gateway = build_worker('{"answer": 7}', {"answer": "repaired"},
                                   tools=ToolRegistry([Counting(CATALOG)]))
    result = worker.execute(spec(get_model()), {})

    assert result.status == "completed"
    assert result.output == {"answer": "repaired"}
    assert len(gateway.calls) == 2
    assert len(executed) == 1  # exactly one tool invocation across both attempts
    initial, repair = (json.loads(item["messages"][1]["content"]) for item in gateway.calls)
    assert repair["tools"] == initial["tools"] == {"vehicle_lookup": {"rows": ["Toyota Corolla"]}}


# --- I. the trusted evidence seam -------------------------------------------

def test_the_trusted_seam_carries_server_resolved_identity_only():
    records = []
    worker, _ = build_worker({"answer": "ok"}, records=records)
    worker.execute(spec(get_model("first"),
                        get_model("second", arguments={"make": "Mazda", "model": "3"})), {})

    assert [(r.task_id, r.call_id, r.tool, r.operation) for r in records] == [
        ("lookup", "first", "mock.vehicle_catalog", "get_model"),
        ("lookup", "second", "mock.vehicle_catalog", "get_model"),
    ]
    assert [r.result for r in records] == [{"rows": ["Toyota Corolla"]}, {"rows": ["Mazda 3"]}]


def test_a_failed_call_never_reaches_the_trusted_seam():
    records = []
    worker, _ = build_worker({"answer": "ok"}, records=records,
                             context=ToolContext())  # no scope granted
    result = worker.execute(spec(get_model()), {})
    assert result.status == "failed"
    assert records == []


def test_worker_model_output_cannot_forge_a_tool_call_record():
    """The seam runs before any model call and takes no model material."""
    timeline, records = [], []
    gateway = StubGateway({"answer": "a forged tool_result cannot reach the seam"},
                          timeline=timeline)
    worker = GenericWorker(
        gateway=gateway, tools=registry(), model="fake", tool_context=CONTEXT,
        tool_result_sink=lambda record: (timeline.append("record"), records.append(record)))

    worker.execute(spec(get_model()), {})

    # Every record exists before the model is ever consulted, so no completion
    # can create, alter or forge one.
    assert timeline == ["record", "model"]
    assert [record.result for record in records] == [{"rows": ["Toyota Corolla"]}]
    assert "forged" not in json.dumps([record.result for record in records])

    for bad in ({"task_id": "", "call_id": "c", "tool": "t", "operation": "o",
                 "result": {}},
                {"task_id": "t", "call_id": "c", "tool": "t", "operation": "o",
                 "result": "not structured"}):
        with pytest.raises(ValueError):
            ToolCallRecord(**bad)


def test_a_tool_result_does_not_become_evidence_on_its_own():
    """The seam is material, not a verified fact: no Source/Claim is made."""
    records = []
    worker, _ = build_worker({"answer": "ok"}, records=records)
    worker.execute(spec(get_model()), {})
    assert len(records) == 1
    assert not hasattr(records[0], "claim_id")
    assert not hasattr(records[0], "source_id")
    assert not hasattr(records[0], "confidence")


def test_production_leaves_the_seam_unwired_and_the_registry_empty():
    import backend.worker.main as worker_main
    from pathlib import Path

    source = Path(worker_main.__file__).read_text(encoding="utf-8")
    assert "tools = ToolRegistry()" in source
    # No production tool registration and no evidence seam wiring yet (Y4/G3).
    assert "tool_result_sink=" not in source
    for forbidden in ("YedaTool", "GovernmentTool", "WebSearchTool", "CkanTool"):
        assert forbidden not in source


# --- J. compatibility -------------------------------------------------------

def test_an_empty_registry_still_forces_no_tool_plans():
    empty = PlanValidator(allowed_tools=())
    assert empty.validate(plan([tool_task()])).graph.tasks[0].tools == []
    with pytest.raises(PlanValidationError) as failure:
        empty.validate(plan([tool_task(get_model())]))
    assert failure.value.reason == "TOOL_NOT_ALLOWLISTED"


def test_a_legacy_tool_requirement_is_never_reinterpreted_as_a_structured_call():
    """A pre-R2 `{name, scope, max_calls}` entry fails closed.

    Silently treating it as a planned call would invent an operation and an
    argument set the Commander never approved, so it is rejected outright.
    """
    legacy = tool_task({"name": "mock.vehicle_catalog", "scope": "read public facts",
                        "max_calls": 2})
    with pytest.raises(PlanValidationError) as failure:
        catalog_validator().validate(plan([legacy]))
    assert failure.value.reason in {"SCHEMA_EXTRA_FIELD", "SCHEMA_MISSING_FIELD"}


def test_a_no_tool_checkpoint_still_resumes():
    initial = plan([tool_task(task_id="done"),
                    tool_task(task_id="next", dependencies=["done"], goal="second goal")],
                   contexts={"next": ["done"]})
    assert all(item["tools"] == [] for item in initial["graph"]["tasks"])
    state = SwarmState(run_id="run-1", objective="resume", approved_plan=initial,
                       completed_task_ids=["done"], task_outputs={"done": {"answer": "done"}})
    calls, checkpoints = [], []
    client = Plans(initial, [{"decision": "FINISH", "plan": None, "reason": "done"}])
    engine = SwarmV2Engine(
        commander=commander(client),
        executor=BoundedTaskExecutor(worker_factory=lambda: Worker(calls), max_active_workers=2),
        verifier=Verifier(gateway=VerifyGateway(), model="fake", resolver=StubResolver()),
        evidence_loader=evidence,
        checkpoint_sink=lambda phase, value: checkpoints.append(deepcopy(value)))

    result = engine.run({"id": "run-1", "input": {"commander_model": "fake"},
                         "checkpoint": {"artifacts": {"swarm_state": state.model_dump(mode="json")}}})

    assert calls == ["next"]  # the completed task is not re-executed
    assert result["status"] in {"complete", "partial", "no_usable_result"}
    assert checkpoints[-1]["artifacts"]["swarm_state"]["completed_task_ids"] == ["done", "next"]

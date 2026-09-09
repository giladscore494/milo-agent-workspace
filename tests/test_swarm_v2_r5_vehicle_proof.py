"""R5: one real, read-only vehicle evidence proof.

Everything here is offline. No network, no provider, no paid call, no
browser capture, no production Supabase and no committed secret: the proof
reads pinned fixtures captured once, by hand, as an explicit development
action, and every assertion below is a deterministic function of those bytes.

This module currently covers the R5 execution seam. The three-source vehicle
proof itself lands with the pinned Yeda/Government/Web fixtures.

--- the deterministic zero-model execution seam ------------------------------

R5 must prove a real evidence path with ZERO model and provider calls, which
needs one thing the engine did not have: a way for a TOOL-COMPLETE structured
task to finish without a worker model call. `GenericWorker` gained exactly
one optional, constructor-injected `TaskOutputStrategy` for that, and these
tests pin its boundaries:

*   absent -- the production default -- the model-backed path is unchanged;
*   present, it is reached only after the tool loop has already executed and
    validated every planned call, so it can never influence tool material,
    evidence acquisition or verification;
*   its output is re-validated against the task's own closed `output_schema`
    through the same path a model completion travels;
*   a strategy that fails NEVER falls back to a model call, because a silent
    fallback would make "zero model calls" unprovable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.engines.swarm_v2 import (
    DETERMINISTIC_OUTPUT_REASONS,
    MAX_TASK_OUTPUT_JSON_BYTES,
    WORKER_OUTPUT_REASONS,
    GenericWorker,
)
from backend.engines.swarm_v2.contracts import CommanderPlan, DynamicTask, PlannedToolCall
from backend.tools import ToolContext, ToolRegistry

from test_swarm_v2_tool_contract import CONTEXT, StubGateway, get_model, registry, spec


# --- helpers ----------------------------------------------------------------

def worker_with(strategy, *bodies, tools=None, context=CONTEXT):
    """One worker plus the gateway that records every model call it makes."""
    gateway = StubGateway(*bodies)
    return GenericWorker(gateway=gateway, tools=tools if tools is not None else registry(),
                         model="fake", tool_context=context,
                         task_output_strategy=strategy), gateway


def constant(value):
    """A trusted strategy that states one fixed output, ignoring its inputs."""
    def strategy(*, task, tool_outputs, dependency_outputs):
        return value
    return strategy


# =============================================================================
# 1. the production default is untouched
# =============================================================================

def test_without_a_strategy_the_worker_still_calls_the_model():
    """The seam is opt-in. Injecting nothing keeps the exact previous path."""
    worker, gateway = worker_with(None, {"answer": "from the model"})
    result = worker.execute(spec(get_model()), {})
    assert result.status == "completed"
    assert result.output == {"answer": "from the model"}
    assert len(gateway.calls) == 1


def test_the_production_worker_wiring_injects_no_strategy():
    """R5 adds no production capability.

    The deterministic strategy is selected by trusted wiring only, and the
    production wiring in backend/worker/main.py deliberately selects none --
    so every production Swarm V2 task keeps its model-backed worker output.
    """
    worker_main = Path("backend/worker/main.py").read_text()
    assert "task_output_strategy" not in worker_main


# =============================================================================
# 2. a tool-complete task finishes with no model call at all
# =============================================================================

def test_a_deterministic_strategy_completes_a_task_with_zero_model_calls():
    worker, gateway = worker_with(constant({"answer": "stated by trusted code"}))
    result = worker.execute(spec(get_model()), {})
    assert result.status == "completed"
    assert result.output == {"answer": "stated by trusted code"}
    # The whole point: a completed structured task that cost no completion.
    assert gateway.calls == []


def test_the_strategy_receives_only_validated_tool_material_and_dependencies():
    """A strategy reads what the server already validated, and nothing else.

    The tool outputs it sees are keyed by `call_id` and are exactly the
    Registry-validated results of the approved plan's own calls -- the same
    trusted material `build_worker_request` would otherwise place in a prompt.
    """
    seen = {}

    def strategy(*, task, tool_outputs, dependency_outputs):
        seen.update(task_id=task.task_id, tools=dict(tool_outputs),
                    dependencies=dict(dependency_outputs))
        return {"answer": tool_outputs["vehicle_lookup"]["rows"][0]}

    worker, gateway = worker_with(strategy)
    result = worker.execute(spec(get_model(), task_id="lookup"), {})
    assert result.status == "completed"
    # The value came out of the real tool result, not out of a model.
    assert result.output == {"answer": "Toyota Corolla"}
    assert seen["task_id"] == "lookup"
    assert seen["tools"] == {"vehicle_lookup": {"rows": ["Toyota Corolla"]}}
    assert seen["dependencies"] == {}
    assert gateway.calls == []


def test_the_strategy_runs_only_after_every_planned_call_has_succeeded():
    """The strategy is downstream of the tool loop, never a way around it.

    A task whose planned call is refused never reaches the strategy, so a
    deterministic answer can never be produced from absent tool material --
    the seam finishes a tool-COMPLETE task and nothing else.
    """
    reached = []

    def strategy(*, task, tool_outputs, dependency_outputs):
        reached.append(dict(tool_outputs))
        return {"answer": "reached"}

    # The registry refuses the call outright: no scope was granted.
    denied, denied_gateway = worker_with(strategy,
                                         context=ToolContext(scopes=frozenset()))
    denied_result = denied.execute(spec(get_model()), {})
    assert denied_result.status == "failed"
    assert denied_result.error["code"] == "TOOL_SCOPE_REQUIRED"
    assert reached == []
    assert denied_gateway.calls == []

    # A call that succeeds with an EMPTY answer is still a completed call, and
    # the strategy sees that real emptiness rather than a substitute for it.
    empty, empty_gateway = worker_with(strategy)
    empty_result = empty.execute(
        spec(get_model(arguments={"make": "Ford", "model": "Focus"})), {})
    assert empty_result.status == "completed"
    assert reached == [{"vehicle_lookup": {"rows": []}}]
    assert empty_gateway.calls == []


# =============================================================================
# 3. the strategy is held to the task's own closed contract
# =============================================================================

@pytest.mark.parametrize("produced", [
    {"answer": 7},                              # wrong property type
    {"answer": "ok", "extra": "smuggled"},       # a field the schema forbids
    {},                                          # a required property missing
    "not a json object",                        # not the declared object at all
    None,
])
def test_deterministic_output_that_fails_the_task_schema_fails_closed(produced):
    worker, gateway = worker_with(constant(produced))
    result = worker.execute(spec(get_model()), {})
    assert result.status == "failed"
    assert result.error["code"] == "WORKER_OUTPUT_STRATEGY_INVALID"
    # No silent fallback: a server defect never becomes a paid completion.
    assert gateway.calls == []


def test_oversized_deterministic_output_is_refused_by_the_durable_bound():
    oversized = {"answer": "x" * (MAX_TASK_OUTPUT_JSON_BYTES + 1)}
    worker, gateway = worker_with(constant(oversized))
    result = worker.execute(spec(get_model()), {})
    assert result.status == "failed"
    assert result.error["code"] == "WORKER_OUTPUT_STRATEGY_INVALID"
    assert gateway.calls == []


def test_a_strategy_that_raises_fails_closed_and_leaks_nothing():
    def strategy(*, task, tool_outputs, dependency_outputs):
        raise RuntimeError("secret-bearing internal detail")

    worker, gateway = worker_with(strategy)
    result = worker.execute(spec(get_model()), {})
    assert result.status == "failed"
    assert result.error["code"] == "WORKER_OUTPUT_STRATEGY_FAILED"
    # Only the static code and its static message travel.
    assert "secret-bearing" not in str(result.error)
    assert gateway.calls == []


def test_a_deterministic_failure_is_never_a_repairable_model_failure():
    """The two vocabularies are disjoint, by construction.

    WORKER_OUTPUT_REASONS is the closed family a bounded model repair may
    answer. A deterministic strategy has no model to repair with, so its
    reasons must never appear there -- otherwise a strategy defect could earn
    a paid repair call and quietly break the zero-model guarantee.
    """
    assert DETERMINISTIC_OUTPUT_REASONS.isdisjoint(WORKER_OUTPUT_REASONS)
    assert DETERMINISTIC_OUTPUT_REASONS == {"WORKER_OUTPUT_STRATEGY_FAILED",
                                            "WORKER_OUTPUT_STRATEGY_INVALID"}


# =============================================================================
# 4. nothing a client, a plan or a model controls can select the strategy
# =============================================================================

def test_no_plan_or_run_input_field_can_name_a_deterministic_strategy():
    """The closed plan contracts have no seam for it.

    A client-controlled `skip_model`, an `approved_plan` override or a raw
    strategy field would make the zero-model path reachable from untrusted
    input. The contracts are `extra="forbid"`, so the absence is enforced
    rather than merely observed.
    """
    for contract in (PlannedToolCall, DynamicTask, CommanderPlan):
        fields = set(contract.model_fields)
        assert not fields & {"task_output_strategy", "skip_model", "deterministic",
                             "strategy", "approved_plan"}
    with pytest.raises(Exception):
        PlannedToolCall.model_validate({"call_id": "c1", "name": "mock.search",
                                        "operation": "search", "arguments": {},
                                        "task_output_strategy": "deterministic"})


def test_the_strategy_is_reachable_only_through_the_worker_constructor():
    """Trusted wiring is the ONE selection path.

    The engine, the executor, the commander and the plan validator never
    mention the seam, so no orchestration layer can turn it on from data.
    """
    for module in ("engine.py", "executor.py", "commander.py", "validation.py",
                   "model_gateway.py"):
        source = Path("backend/engines/swarm_v2", module).read_text()
        assert "task_output_strategy" not in source


# =============================================================================
# 5. R5 introduces no production tool registration and no write capability
# =============================================================================

def test_the_production_tool_registry_is_still_empty():
    assert ToolRegistry().allowed_names == frozenset()
    worker_main = Path("backend/worker/main.py").read_text()
    assert "tools = ToolRegistry()" in worker_main

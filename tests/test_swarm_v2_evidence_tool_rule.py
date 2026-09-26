"""PR-V V4: evidence requirements need an evidence-producing tool operation.

Run aa63369b planned `register_meta` -- ONE `get_variants` call -- with
evidence.minimum_sources 1. Only `resolve_variant` becomes evidence (the
production evidence-mapper allowlist), so the task was paid for and then
reported EVIDENCE_REQUIREMENTS_UNMET. The tool descriptor now states, from that
authoritative allowlist, which operations produce evidence, and the plan
firewall refuses such a task up front with EVIDENCE_REQUIRES_EVIDENCE_TOOL --
one repairable plan failure instead of a paid, unmet task.
"""

from __future__ import annotations

import copy

import pytest

from backend.engines.swarm_v2 import (VALIDATION_REASONS, Commander, CommanderModelResolver,
                                      CommanderPlanFailure, PlanLimits, PlanValidationError,
                                      PlanValidator, provider_plan_policy)
from backend.engines.swarm_v2.commander import REPAIRABLE_PLAN_FAILURES
from backend.engines.swarm_v2.evidence_mapping import (PRODUCTION_EVIDENCE_MAPPER_OPERATIONS,
                                                       production_evidence_mappers)
from backend.tools import ToolRegistry
from backend.tools.government_vehicle import (GOVERNMENT_TOOL_NAME, OPERATIONS,
                                              GovernmentVehicleTool)
from test_swarm_v2 import FixtureTool, plan, task, tool_descriptors

REASON = "EVIDENCE_REQUIRES_EVIDENCE_TOOL"
TOYOTA = "טויוטה"


def _government_descriptors():
    return ToolRegistry([GovernmentVehicleTool(None)]).descriptors()


def _call(operation: str, call_id: str = "c1") -> dict:
    full = {"manufacturer": TOYOTA, "commercial_model": "4RUNNER", "model_year": 2026,
            "trim": "LIMITED"}
    accepted = OPERATIONS[operation].input_schema["properties"]
    arguments = {key: value for key, value in full.items() if key in accepted}
    return {"call_id": call_id, "name": GOVERNMENT_TOOL_NAME, "operation": operation,
            "arguments": arguments, "dependency_bindings": []}


def _task(task_id: str, operations: list[str], *, minimum_sources: int = 1,
          required_fields: list[str] | None = None) -> dict:
    item = task(task_id, f"register task {task_id}")
    item["tools"] = [_call(operation, f"c{index}") for index, operation
                     in enumerate(operations, start=1)]
    item["evidence"] = {"minimum_sources": minimum_sources,
                        "required_fields": required_fields or [], "min_confidence": 0.5}
    return item


def _validator() -> PlanValidator:
    return PlanValidator(allowed_tools=_government_descriptors(), limits=PlanLimits())


# --- the descriptor -------------------------------------------------------------

def test_the_descriptor_states_which_operations_produce_evidence():
    (descriptor,) = _government_descriptors()
    flags = {item.name: item.produces_evidence for item in descriptor.operations}
    assert flags == {name: name == "resolve_variant" for name in OPERATIONS}
    payload = {item["name"]: item["produces_evidence"]
               for item in descriptor.as_payload()["operations"]}
    assert payload == flags


def test_the_flags_come_from_the_authoritative_mapper_allowlist():
    (descriptor,) = _government_descriptors()
    stated = {(descriptor.name, item.name) for item in descriptor.operations
              if item.produces_evidence}
    assert stated == set(PRODUCTION_EVIDENCE_MAPPER_OPERATIONS)
    assert stated == set(production_evidence_mappers().registered)


def test_a_tool_the_allowlist_does_not_govern_states_nothing():
    (descriptor,) = tool_descriptors("search")
    assert all(item.produces_evidence is None for item in descriptor.operations)
    assert all("produces_evidence" not in item
               for item in descriptor.as_payload()["operations"])


def test_trusted_wiring_may_state_its_own_allowlist():
    registry = ToolRegistry([FixtureTool("search")], evidence_operations={("search", "search")})
    (descriptor,) = registry.descriptors()
    assert [item.produces_evidence for item in descriptor.operations] == [True]


# --- the firewall ------------------------------------------------------------------

def test_the_aa63369b_register_meta_task_is_rejected_at_plan_time():
    """STOP CHECK V4."""
    candidate = plan([_task("t01", ["resolve_variant"]),
                      _task("register_meta", ["get_variants"])])
    with pytest.raises(PlanValidationError) as refused:
        _validator().validate(candidate)
    assert refused.value.reason == REASON
    assert REASON in VALIDATION_REASONS


def test_required_fields_alone_are_refused_too():
    with pytest.raises(PlanValidationError) as refused:
        _validator().validate(plan([_task("t01", ["get_variants"], minimum_sources=0,
                                          required_fields=["trim"])]))
    assert refused.value.reason == REASON


@pytest.mark.parametrize("operations,minimum", [
    (["resolve_variant"], 1),                     # the evidence-producing task still passes
    (["get_variants", "resolve_variant"], 1),     # one evidence-producing call suffices
    (["get_variants"], 0),                        # a listing that asks for no evidence
    (["dataset_meta", "list_models"], 0),
])
def test_what_stays_valid(operations, minimum):
    _validator().validate(plan([_task("t01", operations, minimum_sources=minimum)]))


def test_tools_without_a_stated_flag_are_left_exactly_as_before():
    validator = PlanValidator(allowed_tools=tool_descriptors("search"), limits=PlanLimits())
    validator.validate(plan([task("t01", "research t01")]))


def test_a_task_with_no_planned_call_is_left_as_before():
    item = _task("t01", [])
    PlanValidator(allowed_tools=_government_descriptors(), limits=PlanLimits()).validate(
        plan([item]))


def test_the_rule_reaches_the_provider_visible_policy():
    rules = provider_plan_policy(PlanLimits(), [GOVERNMENT_TOOL_NAME])["rules"]
    (rule,) = [rule for rule in rules if "produces_evidence" in rule]
    assert "get_variants" in rule and "resolve_variant" in rule


class _Scripted:
    def __init__(self, *bodies):
        self.bodies = list(bodies)
        self.repair_reasons = []

    def create_plan(self, *, model, objective, context, repair_reason=None):
        self.repair_reasons.append(repair_reason)
        return self.bodies.pop(0)


def _commander(client, retries):
    return Commander(client=client, resolver=CommanderModelResolver(("kimi-k3",), {"kimi-k3"}),
                     validator=_validator(),
                     retry_callback=lambda agent, phase, reason: retries.append(
                         (agent, phase, reason)))


def test_the_rejection_is_repaired_once_with_exactly_the_static_reason():
    bad = plan([_task("t01", ["resolve_variant"]), _task("register_meta", ["get_variants"])])
    good = copy.deepcopy(bad)
    good["graph"]["tasks"][1]["evidence"]["minimum_sources"] = 0
    client, retries = _Scripted(bad, good), []
    approved = _commander(client, retries).plan(requested_model="kimi-k3", objective="o",
                                                context={})
    assert len(approved.graph.tasks) == 2
    assert client.repair_reasons == [None, REASON]
    assert retries == [("commander", "planning", REASON)]


def test_a_second_refusal_is_a_schema_failure_with_no_third_call():
    bad = plan([_task("register_meta", ["get_variants"])])
    client = _Scripted(bad, copy.deepcopy(bad), copy.deepcopy(bad))
    with pytest.raises(CommanderPlanFailure) as failure:
        _commander(client, []).plan(requested_model="kimi-k3", objective="o", context={})
    assert failure.value.code == "COMMANDER_PLAN_SCHEMA_INVALID"
    assert failure.value.code in REPAIRABLE_PLAN_FAILURES
    assert failure.value.validation_reason == REASON
    assert len(client.bodies) == 1

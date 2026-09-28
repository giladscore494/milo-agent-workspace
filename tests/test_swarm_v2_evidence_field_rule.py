"""PR-EV: evidence.required_fields must name fields the task's tools can evidence.

Run 29eb076c (the T batch) resolved 11/11 candidates and still ended
partial_success. Every task declared

    "evidence": {"required_fields": ["resolved", "ambiguous", "match_count"],
                 "minimum_sources": 1, "min_confidence": 0.9}

-- the resolve_variant OUTPUT keys. Government evidence is recorded under the
catalog field names the evidence mapper emits (trim, model_year_start,
model_year_end, official_model_code, identity_dimensions.fuel_type), and the
engine requires required_fields <= evidenced fields, so every task reported
EVIDENCE_REQUIREMENTS_UNMET after it had been paid for.

The descriptor now states each evidence operation's `evidence_fields`, read
from the mapper itself; the firewall refuses a required field no planned call
can evidence (EVIDENCE_FIELD_NOT_PRODUCIBLE, repairable once, the repair
naming the allowed fields); and the Commander's policy says so up front.
"""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from backend.catalog.government.evidence import (GOVERNMENT_EVIDENCE_FIELDS,
                                                 GOVERNMENT_FIELD_SOURCES,
                                                 GovernmentVariantEvidenceMapper)
from backend.engines.swarm_v2 import (VALIDATION_REASONS, Commander, CommanderModelResolver,
                                      CommanderPlanFailure, PlanLimits, PlanValidationError,
                                      PlanValidator, provider_plan_policy)
from backend.engines.swarm_v2.commander import REPAIRABLE_PLAN_FAILURES
from backend.engines.swarm_v2.evidence_mapping import (PRODUCTION_EVIDENCE_MAPPER_OPERATIONS,
                                                       production_evidence_fields,
                                                       production_evidence_mappers)
from backend.engines.swarm_v2.model_gateway import ModelGateway
from backend.tools import ToolRegistry
from backend.tools.government_vehicle import (GOVERNMENT_TOOL_NAME, OPERATIONS,
                                              GovernmentVehicleTool)
from replay_harness import ReplayProviderAdapter, fixture_dirs, load_manifest
from test_swarm_v2 import FixtureTool, plan, task, tool_descriptors

REASON = "EVIDENCE_FIELD_NOT_PRODUCIBLE"
TOYOTA = "טויוטה"
#: The evidence block every task of run 29eb076c declared.
T_EVIDENCE = {"required_fields": ["resolved", "ambiguous", "match_count"],
              "minimum_sources": 1, "min_confidence": 0.9}
REPAIRED_FIELDS = ["trim", "official_model_code"]
FIXTURES = {path.name: path for path in fixture_dirs()}


def _government_descriptors():
    return ToolRegistry([GovernmentVehicleTool(None)]).descriptors()


def _validator() -> PlanValidator:
    return PlanValidator(allowed_tools=_government_descriptors(), limits=PlanLimits())


def _call(operation: str, call_id: str = "c1") -> dict:
    full = {"manufacturer": TOYOTA, "commercial_model": "RAV4", "model_year": 2024,
            "trim": "LIMITED"}
    accepted = OPERATIONS[operation].input_schema["properties"]
    arguments = {key: value for key, value in full.items() if key in accepted}
    return {"call_id": call_id, "name": GOVERNMENT_TOOL_NAME, "operation": operation,
            "arguments": arguments, "dependency_bindings": []}


def _task(task_id: str, operations: list[str], evidence: dict | None = None) -> dict:
    item = task(task_id, f"register task {task_id}")
    item["tools"] = [_call(operation, f"c{index}") for index, operation
                     in enumerate(operations, start=1)]
    item["evidence"] = copy.deepcopy(evidence or T_EVIDENCE)
    return item


def _evidence(*fields: str) -> dict:
    return {**T_EVIDENCE, "required_fields": list(fields)}


def _t_plan(evidence: dict | None = None) -> dict:
    """The exact T task shape: ONE resolve_variant call, the recorded evidence."""
    return plan([_task(f"t{index:02d}", ["resolve_variant"], evidence)
                 for index in range(1, 4)])


# --- EV-1: the descriptor states the fields, from the mapper -----------------------

def test_resolve_variant_states_exactly_the_mapper_fields():
    (descriptor,) = _government_descriptors()
    fields = {item.name: item.evidence_fields for item in descriptor.operations}
    assert fields == {name: (tuple(sorted(GOVERNMENT_EVIDENCE_FIELDS))
                             if name == "resolve_variant" else None) for name in OPERATIONS}
    assert set(GOVERNMENT_EVIDENCE_FIELDS) == {"trim", "model_year_start", "model_year_end",
                                               "official_model_code",
                                               "identity_dimensions.fuel_type"}


def test_the_fields_have_one_source_the_government_mapper():
    # Read from the mapper's own field table, never restated.
    assert GOVERNMENT_EVIDENCE_FIELDS == tuple(row[0] for row in GOVERNMENT_FIELD_SOURCES)
    assert GovernmentVariantEvidenceMapper.evidence_fields is GOVERNMENT_EVIDENCE_FIELDS
    assert production_evidence_fields() == {
        (GOVERNMENT_TOOL_NAME, "resolve_variant"): frozenset(GOVERNMENT_EVIDENCE_FIELDS)}
    assert set(production_evidence_fields()) == set(PRODUCTION_EVIDENCE_MAPPER_OPERATIONS)
    assert production_evidence_mappers().evidence_fields() == production_evidence_fields()


def test_the_commander_sees_the_fields_in_the_tool_catalog():
    (descriptor,) = _government_descriptors()
    operations = {item["name"]: item for item in descriptor.as_payload()["operations"]}
    assert operations["resolve_variant"]["evidence_fields"] == sorted(GOVERNMENT_EVIDENCE_FIELDS)
    assert all("evidence_fields" not in item for name, item in operations.items()
               if name != "resolve_variant")
    gateway = _gateway([])
    catalog = json.loads(gateway._tool_catalog)
    (resolve,) = [item for item in catalog[0]["operations"] if item["name"] == "resolve_variant"]
    assert resolve["evidence_fields"] == sorted(GOVERNMENT_EVIDENCE_FIELDS)


def test_what_a_resolved_answer_evidences_is_inside_the_stated_fields():
    """The descriptor never under-states the mapper: every fact of a real
    resolved answer (the T batch's recorded tool results) is a stated field."""
    manifest = load_manifest(FIXTURES["29eb076c"])
    mapper = GovernmentVariantEvidenceMapper()
    for entry in manifest["tool_results"]:
        bundle = mapper.map(SimpleNamespace(result=entry["result"]))
        emitted = {fact.field_key for fact in bundle.facts}
        assert set(REPAIRED_FIELDS) <= emitted <= set(GOVERNMENT_EVIDENCE_FIELDS)


def test_a_tool_the_allowlist_does_not_govern_states_no_fields():
    (descriptor,) = tool_descriptors("search")
    assert all(item.evidence_fields is None for item in descriptor.operations)
    assert all("evidence_fields" not in item for item in descriptor.as_payload()["operations"])


def test_trusted_wiring_that_states_operations_but_no_fields_states_no_fields():
    registry = ToolRegistry([FixtureTool("search")], evidence_operations={("search", "search")})
    (descriptor,) = registry.descriptors()
    assert [(item.produces_evidence, item.evidence_fields)
            for item in descriptor.operations] == [(True, None)]


def test_trusted_wiring_may_state_fields_only_for_an_evidence_operation():
    registry = ToolRegistry([FixtureTool("search")], evidence_operations={("search", "search")},
                            evidence_fields={("search", "search"): ["title", "answer"]})
    (descriptor,) = registry.descriptors()
    assert descriptor.operations[0].evidence_fields == ("answer", "title")
    with pytest.raises(ValueError):
        ToolRegistry([FixtureTool("search")], evidence_operations=set(),
                     evidence_fields={("search", "search"): ["title"]})
    with pytest.raises(ValueError):
        ToolRegistry([FixtureTool("search")], evidence_operations={("search", "search")},
                     evidence_fields={("search", "search"): "title"})


# --- EV-2: the firewall ------------------------------------------------------------

def test_the_exact_t_plan_shape_is_refused():
    """STOP CHECK: run 29eb076c's evidence block on a resolve_variant task."""
    with pytest.raises(PlanValidationError) as refused:
        _validator().validate(_t_plan())
    assert refused.value.reason == REASON
    assert REASON in VALIDATION_REASONS


def test_the_repaired_t_plan_is_accepted():
    approved = _validator().validate(_t_plan(_evidence(*REPAIRED_FIELDS)))
    assert [list(item.evidence.required_fields) for item in approved.graph.tasks] == \
        [REPAIRED_FIELDS] * 3
    # Never stripped or rewritten: what was approved is what was planned.
    assert approved.graph.tasks[0].evidence.min_confidence == 0.9


@pytest.mark.parametrize("field", GOVERNMENT_EVIDENCE_FIELDS)
def test_every_stated_field_is_producible(field):
    _validator().validate(_t_plan(_evidence(field)))


@pytest.mark.parametrize("fields", [["resolved"], ["trim", "match_count"],
                                    ["fuel_type"], ["Trim"]])
def test_any_field_outside_the_stated_set_is_refused(fields):
    with pytest.raises(PlanValidationError) as refused:
        _validator().validate(_t_plan(_evidence(*fields)))
    assert refused.value.reason == REASON


def test_a_listing_call_beside_resolve_variant_adds_no_fields():
    ok = plan([_task("t01", ["get_variants", "resolve_variant"], _evidence("trim"))])
    _validator().validate(ok)
    bad = plan([_task("t01", ["get_variants", "resolve_variant"], _evidence("total"))])
    with pytest.raises(PlanValidationError) as refused:
        _validator().validate(bad)
    assert refused.value.reason == REASON


def test_a_task_whose_calls_produce_no_evidence_keeps_the_pr_v_reason():
    """EV-5: EVIDENCE_REQUIRES_EVIDENCE_TOOL is decided first and unchanged."""
    for evidence in (T_EVIDENCE, _evidence("trim")):
        with pytest.raises(PlanValidationError) as refused:
            _validator().validate(plan([_task("t01", ["get_variants"], evidence)]))
        assert refused.value.reason == "EVIDENCE_REQUIRES_EVIDENCE_TOOL"


def test_minimum_sources_alone_is_untouched():
    _validator().validate(_t_plan({"required_fields": [], "minimum_sources": 1,
                                   "min_confidence": 0.9}))


def test_tools_that_state_nothing_are_left_exactly_as_before():
    validator = PlanValidator(allowed_tools=tool_descriptors("search"), limits=PlanLimits())
    item = task("t01", "research t01")
    item["evidence"]["required_fields"] = ["resolved"]
    validator.validate(plan([item]))


def test_an_evidence_operation_without_stated_fields_is_left_as_before():
    registry = ToolRegistry([FixtureTool("search")], evidence_operations={("search", "search")})
    item = task("t01", "research t01")
    item["evidence"]["required_fields"] = ["anything"]
    PlanValidator(allowed_tools=registry.descriptors(), limits=PlanLimits()).validate(
        plan([item]))


# --- EV-3: the Commander's rules ------------------------------------------------------

def test_the_general_rule_reaches_the_provider_visible_policy():
    rules = provider_plan_policy(PlanLimits(), [GOVERNMENT_TOOL_NAME])["rules"]
    (rule,) = [rule for rule in rules if "evidence_fields" in rule]
    assert "never tool output keys" in rule
    assert all(key in rule for key in ("resolved", "ambiguous", "match_count"))
    assert "explains its own evidence gap" in rule


def test_the_resolve_variant_rule_names_the_allowed_fields():
    policy = provider_plan_policy(PlanLimits(), [GOVERNMENT_TOOL_NAME])["source_policy"]
    (rule,) = [rule for rule in policy if "evidence.required_fields may name only" in rule]
    assert rule.startswith(f"{GOVERNMENT_TOOL_NAME}.resolve_variant:")
    assert ", ".join(sorted(GOVERNMENT_EVIDENCE_FIELDS)) in rule
    assert "never one of resolve_variant's output keys" in rule
    assert "ambiguous or not-found register answer) explains its own gap" in rule
    # Only when the tool is registered.
    assert not any("may name only" in line
                   for line in provider_plan_policy(PlanLimits(), ["search"])["source_policy"])


# --- the one semantic repair -----------------------------------------------------------

class _Recording(ReplayProviderAdapter):
    def __init__(self, *plans: dict) -> None:
        super().__init__({"commander": [{"phase": "planning", "content": json.dumps(item),
                                         "finish_reason": "stop"} for item in plans]})
        self.systems: list[str] = []

    def chat(self, request, **kwargs):
        self.systems.append(request["messages"][0]["content"])
        return super().chat(request, **kwargs)


def _gateway(plans, provider=None):
    return ModelGateway(guarded_client_factory=lambda _key, _url: None,
                        adapter=provider or _Recording(*plans), api_key="", base_url="",
                        tool_descriptors=_government_descriptors(), plan_limits=PlanLimits())


def _commander(provider, retries):
    return Commander(client=_gateway([], provider),
                     resolver=CommanderModelResolver(("kimi-k3",), {"kimi-k3"}),
                     validator=_validator(),
                     retry_callback=lambda agent, phase, reason: retries.append(
                         (agent, phase, reason)))


def test_the_t_plan_is_refused_then_accepted_after_the_one_repair():
    provider, retries = _Recording(_t_plan(), _t_plan(_evidence(*REPAIRED_FIELDS))), []
    approved = _commander(provider, retries).plan(requested_model="kimi-k3", objective="o",
                                                  context={})
    assert [list(item.evidence.required_fields) for item in approved.graph.tasks] == \
        [REPAIRED_FIELDS] * 3
    assert retries == [("commander", "planning", REASON)]
    first, repair = provider.systems
    # The repair tells the Commander the allowed names -- server data only,
    # never the rejected plan.
    names = ", ".join(sorted(GOVERNMENT_EVIDENCE_FIELDS))
    assert f"static reason code {REASON}" in repair
    assert (f"{GOVERNMENT_TOOL_NAME}.resolve_variant: evidence.required_fields may name only "
            f"these catalog fields: {names};") in repair[len(first):]
    assert repair.startswith(first)
    assert '"match_count"]' not in repair


def test_another_repair_reason_does_not_carry_the_field_guidance():
    bad = plan([_task("t01", ["get_variants"])])
    provider = _Recording(bad, _t_plan(_evidence(*REPAIRED_FIELDS)))
    _commander(provider, []).plan(requested_model="kimi-k3", objective="o", context={})
    first, repair = provider.systems
    assert "static reason code EVIDENCE_REQUIRES_EVIDENCE_TOOL" in repair
    assert "may name only these catalog fields" not in repair[len(first):]


def test_a_second_refusal_fails_with_no_third_call():
    provider = _Recording(_t_plan(), _t_plan(), _t_plan())
    with pytest.raises(CommanderPlanFailure) as failure:
        _commander(provider, []).plan(requested_model="kimi-k3", objective="o", context={})
    assert failure.value.code == "COMMANDER_PLAN_SCHEMA_INVALID"
    assert failure.value.code in REPAIRABLE_PLAN_FAILURES
    assert failure.value.validation_reason == REASON
    assert len(provider.systems) == 2

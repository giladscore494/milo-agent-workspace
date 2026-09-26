"""PR-W S5: harmless output_schema keywords never cost a repair or a run.

S1/S4 made plan-time validation equal runtime validation, which left a new
failure mode: a Commander plan whose task output_schema carried a harmless
JSON-Schema annotation (``title``, ``format``, ``pattern``, ...) was rejected,
paid for a repair, and could fail the run over a keyword nothing enforces.

``normalize_output_schema`` now removes the never-enforced annotation keywords
in ONE place -- the DynamicTask ``output_schema`` validator, mode="before" --
so the plan, a checkpoint resume, the provider's strict json_schema and the
runtime validator all see the same normalized schema. Simple constraints are
kept and ENFORCED; structural defects and every other keyword are still
rejected exactly as before.
"""

from __future__ import annotations

import copy
import json

import pytest

from backend.engines.swarm_v2 import (Commander, CommanderModelResolver, PlanLimits,
                                      PlanValidator)
from backend.engines.swarm_v2.contracts import (DynamicTask, commander_decision_json_schema,
                                                commander_plan_json_schema,
                                                output_schema_is_runtime_valid)
from backend.engines.swarm_v2.state import SwarmState
from backend.engines.swarm_v2.tool_calls import MAX_TOOL_COLLECTION_ITEMS, MAX_TOOL_VALUE_DEPTH
from backend.engines.swarm_v2.validation import PlanSchemaError
from backend.engines.swarm_v2.worker import (GenericWorker, WorkerOutputValidationError,
                                             validate_worker_output)
from backend.tools import ToolContext, ToolRegistry
from backend.tools.registry import (MAX_OUTPUT_SCHEMA_DEPTH, MAX_OUTPUT_SCHEMA_ITEMS,
                                    OUTPUT_SCHEMA_CONSTRAINT_KEYWORDS,
                                    OUTPUT_SCHEMA_STRIPPED_KEYWORDS, normalize_output_schema,
                                    validate_json_schema, validate_output_schema,
                                    validate_schema)
from test_swarm_v2 import plan, task, tool_descriptors
from test_swarm_v2_output_schema_nested import (RUN_280FC9E5_T02_SCHEMA,
                                                RUN_6825EB96_ENUM_SCHEMA)

REASON = "OUTPUT_SCHEMA_NESTED_INVALID"

ANNOTATED_STRING = {"type": "string", "pattern": "^T", "title": "x", "format": "date"}


def _object(**properties: dict) -> dict:
    return {"type": "object", "properties": properties,
            "required": sorted(properties), "additionalProperties": False}


def _task(task_id: str, schema: dict) -> dict:
    item = task(task_id, f"bounded question {task_id}", cost=5)
    item["output_schema"] = copy.deepcopy(schema)
    item["evidence"] = {"minimum_sources": 0, "required_fields": [], "min_confidence": 0.5}
    item["completion"] = {"required_outputs": [sorted(schema["properties"])[0]],
                          "evidence_satisfied": True, "allow_partial": True}
    return item


def _validator() -> PlanValidator:
    return PlanValidator(allowed_tools=tool_descriptors("search"), limits=PlanLimits())


def _approve(schema: dict):
    return _validator().validate(plan([_task("t01", schema)])).graph.tasks[0]


def _rejected(schema: dict) -> None:
    with pytest.raises(PlanSchemaError) as failure:
        _validator().validate(plan([_task("t01", schema)]))
    assert failure.value.reason == REASON


# --- the keyword lists, as implemented ----------------------------------------

def test_the_keyword_lists_are_exactly_the_specified_ones():
    assert OUTPUT_SCHEMA_STRIPPED_KEYWORDS == {
        "title", "default", "examples", "format", "pattern", "$schema", "$id",
        "$comment", "readOnly", "writeOnly", "deprecated"}
    assert dict(OUTPUT_SCHEMA_CONSTRAINT_KEYWORDS) == {
        "integer": {"minimum", "maximum"}, "number": {"minimum", "maximum"},
        "string": {"minLength", "maxLength"}, "array": {"minItems"}}


def test_the_schema_bounds_are_the_existing_output_value_bounds():
    assert MAX_OUTPUT_SCHEMA_DEPTH == MAX_TOOL_VALUE_DEPTH
    assert MAX_OUTPUT_SCHEMA_ITEMS == MAX_TOOL_COLLECTION_ITEMS


# --- the two recorded runs ------------------------------------------------------

def test_the_6825eb96_enum_schema_is_unchanged_and_accepted():
    normalized, stripped = normalize_output_schema(RUN_6825EB96_ENUM_SCHEMA)
    assert normalized == RUN_6825EB96_ENUM_SCHEMA and stripped == []
    approved = _approve(RUN_6825EB96_ENUM_SCHEMA)
    assert approved.output_schema == RUN_6825EB96_ENUM_SCHEMA
    assert approved.stripped_output_keywords == []


def test_the_280fc9e5_schema_is_still_rejected():
    _rejected(RUN_280FC9E5_T02_SCHEMA)
    # Annotations do not rescue a structural defect.
    _rejected(_object(variants={"type": "array", "title": "variants"}))
    _rejected(_object(provenance={"type": "object", "format": "x"}))


# --- stripping -------------------------------------------------------------------

def test_annotation_keywords_are_stripped_and_named():
    approved = _approve(_object(answer=ANNOTATED_STRING))
    assert approved.output_schema == _object(answer={"type": "string"})
    assert approved.stripped_output_keywords == ["format", "pattern", "title"]


def test_every_stripped_keyword_is_stripped_at_every_structural_position():
    every = {key: "annotation" for key in OUTPUT_SCHEMA_STRIPPED_KEYWORDS}
    schema = {**_object(tags={"type": "array", "items": {"type": "string", **every}, **every},
                        nested={**_object(leaf={"type": "integer", **every}), **every}),
              **every}
    normalized, stripped = normalize_output_schema(schema)
    assert stripped == sorted(OUTPUT_SCHEMA_STRIPPED_KEYWORDS)
    assert normalized == _object(tags={"type": "array", "items": {"type": "string"}},
                                 nested=_object(leaf={"type": "integer"}))
    assert output_schema_is_runtime_valid(normalized)


def test_names_only_never_values():
    _, stripped = normalize_output_schema(
        {"type": "string", "title": "SECRET_TITLE", "examples": ["SECRET_EXAMPLE"]})
    assert stripped == ["examples", "title"]
    assert "SECRET" not in json.dumps(stripped)


def test_a_property_named_like_a_keyword_is_never_stripped():
    schema = _object(title={"type": "string"}, format={"type": "string"},
                     pattern={"type": "string"})
    normalized, stripped = normalize_output_schema(schema)
    assert normalized == schema and stripped == []
    assert set(_approve(schema).output_schema["properties"]) == {"format", "pattern", "title"}


def test_normalization_is_idempotent_and_never_mutates_its_input():
    schema = {**_object(answer=ANNOTATED_STRING, n={"type": "integer", "minimum": 0,
                                                    "default": 3}), "$schema": "x"}
    original = copy.deepcopy(schema)
    once, stripped = normalize_output_schema(schema)
    twice, stripped_again = normalize_output_schema(once)
    assert schema == original
    assert once == twice and stripped_again == []
    assert stripped == ["$schema", "default", "format", "pattern", "title"]
    # A fresh structure: nothing in the result aliases the input.
    once["properties"]["n"]["minimum"] = 99
    once["required"].append("x")
    assert schema == original


def test_the_dynamic_task_does_not_mutate_the_raw_plan():
    raw = plan([_task("t01", _object(answer=ANNOTATED_STRING))])
    original = copy.deepcopy(raw)
    _validator().validate(raw)
    assert raw == original


# --- simple constraints: kept, validated, enforced --------------------------------

def test_integer_bounds_are_enforced():
    schema = _object(n={"type": "integer", "minimum": 0, "maximum": 10})
    approved = _approve(schema)
    assert approved.output_schema == schema
    assert validate_worker_output('{"n": 5}', approved.output_schema) == {"n": 5}
    for value in (11, -1):
        with pytest.raises(WorkerOutputValidationError) as failure:
            validate_worker_output(json.dumps({"n": value}), approved.output_schema)
        assert failure.value.reason_code == "WORKER_OUTPUT_SCHEMA_INVALID"


@pytest.mark.parametrize("schema,good,bad", [
    ({"type": "number", "minimum": 0.5, "maximum": 1.5}, [0.5, 1, 1.5], [0.4, 2]),
    ({"type": "string", "minLength": 2, "maxLength": 3}, ["ab", "abc"], ["a", "abcd"]),
    ({"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 2},
     [["a"], ["a", "b"]], [[], ["a", "b", "c"]]),
    ({"type": "array", "items": {"type": "integer", "minimum": 1}}, [[1, 2]], [[0]]),
])
def test_length_and_item_bounds_are_enforced(schema, good, bad):
    approved = _approve(_object(value=schema))
    for value in good:
        validate_worker_output(json.dumps({"value": value}), approved.output_schema)
    for value in bad:
        with pytest.raises(WorkerOutputValidationError) as failure:
            validate_worker_output(json.dumps({"value": value}), approved.output_schema)
        assert failure.value.reason_code == "WORKER_OUTPUT_SCHEMA_INVALID"


@pytest.mark.parametrize("nested", [
    {"type": "integer", "minimum": 10, "maximum": 1},
    {"type": "number", "minimum": 1.5, "maximum": 1.0},
    {"type": "string", "minLength": 5, "maxLength": 2},
    {"type": "array", "items": {"type": "string"}, "minItems": 3, "maxItems": 1},
    {"type": "integer", "minimum": "0"},
    {"type": "integer", "maximum": True},
    {"type": "string", "minLength": -1},
    {"type": "string", "maxLength": 1.5},
    {"type": "array", "items": {"type": "string"}, "minItems": -1},
    # A constraint on a type it does not apply to is a structural defect too.
    {"type": "string", "minimum": 0},
    {"type": "integer", "minLength": 1},
    {"type": "boolean", "maxItems": 1},
])
def test_a_malformed_constraint_is_rejected_at_plan_time_not_stripped(nested):
    _rejected(_object(value=nested))


# --- structural keywords stay rejected ---------------------------------------------

@pytest.mark.parametrize("nested", [
    {"type": "string", "oneOf": [{"type": "string"}]},
    {"type": "string", "anyOf": [{"type": "string"}]},
    {"type": "string", "allOf": [{"type": "string"}]},
    {"type": "string", "not": {"type": "integer"}},
    {"$ref": "#/definitions/x"},
    {"type": "string", "$ref": "#/definitions/x"},
    {"type": "string", "const": "x"},
    {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
    {"type": "object", "properties": {}, "required": [], "additionalProperties": False,
     "patternProperties": {}},
])
def test_structural_keywords_are_still_rejected(nested):
    assert normalize_output_schema(nested)[1] == []
    _rejected(_object(value=nested))


def test_nesting_beyond_the_output_depth_bound_is_rejected():
    leaf: dict = {"type": "string"}
    for _ in range(MAX_OUTPUT_SCHEMA_DEPTH):
        leaf = _object(inner=leaf)
    normalize_output_schema(leaf)
    with pytest.raises(ValueError):
        normalize_output_schema(_object(inner=leaf))
    _rejected(_object(inner=leaf))


def test_width_beyond_the_output_size_bound_is_rejected():
    wide = _object(**{f"p{index}": {"type": "string"}
                      for index in range(MAX_OUTPUT_SCHEMA_ITEMS + 1)})
    with pytest.raises(ValueError):
        normalize_output_schema(wide)
    _rejected(wide)


# --- tool schemas are untouched -------------------------------------------------------

@pytest.mark.parametrize("schema", [
    ANNOTATED_STRING,
    {"type": "integer", "minimum": 0, "maximum": 10},
    {"type": "string", "minLength": 1},
    {"type": "array", "items": {"type": "string"}, "minItems": 1},
    {"type": "string", "enum": ["a"]},
    {"type": "string", "description": "d"},
    {"type": "string", "oneOf": [{"type": "string"}]},
    {"type": "string", "const": "x"},
])
def test_tool_schemas_still_refuse_every_one_of_these(schema):
    with pytest.raises(ValueError):
        validate_schema(schema)
    with pytest.raises(ValueError):
        validate_schema(_object(value=schema))


# --- one normalized schema everywhere ---------------------------------------------------

def _commander_for_resume() -> Commander:
    return Commander(client=object(),
                     resolver=CommanderModelResolver(("kimi-k2.6",), {"kimi-k2.6"}),
                     validator=_validator())


def test_the_stripped_names_survive_a_checkpoint_round_trip_and_a_resume():
    """Persisted with the approved plan: the engine stores
    `plan.model_dump(mode="json")` as SwarmState.approved_plan and resumes via
    `SwarmState.resume` + `Commander.validate_saved_plan`."""
    approved = _validator().validate(plan([_task("t01", _object(answer=ANNOTATED_STRING)),
                                           _task("t02", RUN_6825EB96_ENUM_SCHEMA)]))
    state = SwarmState(run_id="run-1", objective="o",
                       approved_plan=approved.model_dump(mode="json"))
    # What the durable checkpoint row holds, and what a resume reads back.
    durable = json.loads(json.dumps(state.model_dump(mode="json")))
    stored_tasks = durable["approved_plan"]["graph"]["tasks"]
    assert [item["stripped_output_keywords"] for item in stored_tasks] == \
        [["format", "pattern", "title"], []]
    resumed_state = SwarmState.resume(durable, run_id="run-1")
    resumed = _commander_for_resume().validate_saved_plan(resumed_state.approved_plan)
    assert [item.stripped_output_keywords for item in resumed.graph.tasks] == \
        [["format", "pattern", "title"], []]
    # Idempotent: the resumed plan is byte-identical to the one checkpointed.
    assert resumed.model_dump(mode="json") == approved.model_dump(mode="json")


def test_the_recorded_names_are_static_keyword_names_only():
    raw = _task("t01", _object(answer={"type": "string"}))
    for bad in (["SECRET"], ["title", "title"], ["title", "format"], "title", [1]):
        with pytest.raises(PlanSchemaError):
            _validator().validate(plan([{**raw, "stripped_output_keywords": bad}]))
    # A recorded (checkpointed) name is kept and merged with new ones.
    raw_annotated = _task("t01", _object(answer={"type": "string", "title": "t"}))
    approved = _validator().validate(
        plan([{**raw_annotated, "stripped_output_keywords": ["format"]}]))
    assert approved.graph.tasks[0].stripped_output_keywords == ["format", "title"]


def test_the_field_is_not_in_the_provider_visible_plan_schema():
    assert "stripped_output_keywords" not in json.dumps(commander_plan_json_schema())
    assert "stripped_output_keywords" not in json.dumps(commander_decision_json_schema())


def test_a_pre_s5_checkpoint_still_loads():
    """A checkpoint written before this change holds S4-valid schemas, which
    normalization leaves byte-identical."""
    stored = plan([_task("t01", RUN_6825EB96_ENUM_SCHEMA),
                   _task("t02", _object(answer={"type": "string", "description": "d"}))])
    resumed = _validator().validate(copy.deepcopy(stored))
    assert [item.output_schema for item in resumed.graph.tasks] == \
        [item["output_schema"] for item in stored["graph"]["tasks"]]
    assert [item.stripped_output_keywords for item in resumed.graph.tasks] == [[], []]


class _RecordingGateway:
    def __init__(self, answer: dict) -> None:
        self.answer = answer
        self.calls: list[dict] = []

    def call(self, **kwargs):
        self.calls.append(kwargs)
        return json.dumps(self.answer)


def test_the_provider_request_carries_the_normalized_schema():
    raw = _task("t01", _object(day={**ANNOTATED_STRING, "minLength": 1}))
    raw["tools"] = []
    approved = DynamicTask.model_validate(raw)
    gateway = _RecordingGateway({"day": "Tuesday"})
    worker = GenericWorker(gateway=gateway, tools=ToolRegistry([]), model="kimi-k2.6",
                           tool_context=ToolContext())
    result = worker.execute(approved, {})
    assert result.status == "completed"
    (call,) = gateway.calls
    expected = _object(day={"type": "string", "minLength": 1})
    assert call["schema"] == expected
    # The schema embedded in the prompt is the same normalized one.
    prompt = json.loads(call["messages"][-1]["content"])
    assert prompt["output_schema"] == expected


def test_an_annotated_plan_needs_no_repair():
    class _Client:
        def __init__(self) -> None:
            self.repair_reasons: list = []

        def create_plan(self, *, model, objective, context, repair_reason=None):
            self.repair_reasons.append(repair_reason)
            return plan([_task("t01", _object(answer=ANNOTATED_STRING))])

    client = _Client()
    approved = Commander(client=client,
                         resolver=CommanderModelResolver(("kimi-k2.6",), {"kimi-k2.6"}),
                         validator=_validator()).plan(requested_model="kimi-k2.6",
                                                      objective="o", context={})
    assert client.repair_reasons == [None]
    assert approved.graph.tasks[0].stripped_output_keywords == ["format", "pattern", "title"]


def test_the_runtime_still_ignores_description_and_enforces_enum_after_normalization():
    schema = _approve(RUN_6825EB96_ENUM_SCHEMA).output_schema
    validate_json_schema(schema, {"outcome": "resolved", "resolved": True, "match_count": 0,
                                  "candidate_id": "c"})
    with pytest.raises(ValueError):
        validate_json_schema(schema, {"outcome": "other", "resolved": True, "match_count": 0,
                                      "candidate_id": "c"})
    validate_output_schema(schema)

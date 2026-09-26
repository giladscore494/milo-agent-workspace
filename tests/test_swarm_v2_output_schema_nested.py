"""PR-W S1: plan-time output_schema validation equals runtime validation.

Run 280fc9e5 (revision 1): the Commander declared task output schemas with
nested properties the runtime validator cannot enforce --

    "variants":   {"type": "array"}     # no "items"
    "provenance": {"type": "object"}    # not a closed object

The firewall only checked the TOP level, so the plan was approved and paid
for. At run time ``validate_json_schema`` then read ``schema["items"]`` and
raised a bare KeyError, which the worker folded into TASK_FAILED: 11/11 tasks
failed after successful model calls.

Every output_schema is now checked RECURSIVELY with the registry's own
``validate_schema`` at plan time, rejected with the static reason
OUTPUT_SCHEMA_NESTED_INVALID (under COMMANDER_PLAN_SCHEMA_INVALID, so the one
existing Commander repair applies), and the runtime validator can no longer
raise anything but its documented failures.
"""

from __future__ import annotations

import copy
import json
import random

import pytest
from pydantic import ValidationError

from backend.engines.swarm_v2 import (VALIDATION_REASONS, Commander, CommanderModelResolver,
                                      CommanderPlanFailure, PlanLimits, PlanValidator,
                                      provider_plan_policy)
from backend.engines.swarm_v2.commander import REPAIRABLE_PLAN_FAILURES
from backend.engines.swarm_v2.contracts import (OUTPUT_SCHEMA_NESTED_ERROR_TYPE, CommanderPlan,
                                                DynamicTask, output_schema_is_runtime_valid)
from backend.engines.swarm_v2.tool_calls import ToolCallError
from backend.engines.swarm_v2.validation import PlanSchemaError
from backend.engines.swarm_v2.worker import WorkerOutputValidationError, validate_worker_output
from backend.tools.registry import validate_json_schema, validate_schema
from test_swarm_v2 import plan, task, tool_descriptors

REASON = "OUTPUT_SCHEMA_NESTED_INVALID"

#: The t02 output_schema of run 280fc9e5, revision 1.
RUN_280FC9E5_T02_SCHEMA = {
    "type": "object",
    "properties": {
        "variants": {"type": "array"},
        "provenance": {"type": "object"},
        "candidate_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["variants", "provenance", "candidate_ids"],
    "additionalProperties": False,
}

#: The same task with the shape the new Commander rule asks for: scalars and
#: arrays of scalars only; the raw tool material stays with the tool.
CORRECTED_T02_SCHEMA = {
    "type": "object",
    "properties": {
        "variant_count": {"type": "integer"},
        "resolved": {"type": "boolean"},
        "candidate_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["variant_count", "resolved", "candidate_ids"],
    "additionalProperties": False,
}


def _t02_task(task_id: str, schema: dict) -> dict:
    item = task(task_id, f"resolve register variants {task_id}", cost=5)
    item["output_schema"] = copy.deepcopy(schema)
    item["evidence"] = {"minimum_sources": 0, "required_fields": [], "min_confidence": 0.5}
    item["completion"] = {"required_outputs": ["candidate_ids"],
                          "evidence_satisfied": True, "allow_partial": True}
    return item


def run_280fc9e5_plan() -> dict:
    return plan([_t02_task("t01", CORRECTED_T02_SCHEMA),
                 _t02_task("t02", RUN_280FC9E5_T02_SCHEMA)])


def corrected_plan() -> dict:
    return plan([_t02_task("t01", CORRECTED_T02_SCHEMA),
                 _t02_task("t02", CORRECTED_T02_SCHEMA | {"required": ["candidate_ids"]})])


def _validator() -> PlanValidator:
    return PlanValidator(allowed_tools=tool_descriptors("search"), limits=PlanLimits())


# --- the reproduction --------------------------------------------------------

def test_the_280fc9e5_output_used_to_escape_the_runtime_validator():
    """The exact local reproduction now fails with the documented static code."""
    with pytest.raises(WorkerOutputValidationError) as failure:
        validate_worker_output('{"variants":[{"a":1}],"provenance":{},"candidate_ids":[]}',
                               RUN_280FC9E5_T02_SCHEMA)
    assert failure.value.reason_code == "WORKER_OUTPUT_SCHEMA_INVALID"


@pytest.mark.parametrize("value", [[], [{"a": 1}], ["x", 1]])
def test_an_array_schema_without_items_is_a_value_error_never_a_key_error(value):
    with pytest.raises(ValueError):
        validate_json_schema({"type": "array"}, value)


@pytest.mark.parametrize("schema", [
    {"type": "array", "items": {"type": "string"}, "maxItems": None},
    {"type": "array", "items": {"type": "string"}, "maxItems": "2"},
    {"type": ["string"]},
    {"type": "object", "properties": [], "required": [], "additionalProperties": False},
    {"type": "object", "properties": {}, "required": [["a"]], "additionalProperties": False},
    "not a schema",
    None,
])
def test_malformed_schemas_are_value_errors(schema):
    with pytest.raises(ValueError):
        validate_json_schema(schema, ["x"] if isinstance(schema, dict) and schema.get("type") == "array" else {})


# --- plan time ---------------------------------------------------------------

def test_the_t02_schema_is_rejected_by_the_dynamic_task_contract():
    with pytest.raises(ValidationError) as failure:
        DynamicTask.model_validate(_t02_task("t02", RUN_280FC9E5_T02_SCHEMA))
    assert {error["type"] for error in failure.value.errors()} == {OUTPUT_SCHEMA_NESTED_ERROR_TYPE}


def test_the_t02_plan_is_rejected_at_plan_time_with_the_static_reason():
    with pytest.raises(PlanSchemaError) as failure:
        _validator().validate(run_280fc9e5_plan())
    assert failure.value.reason == REASON
    assert REASON in VALIDATION_REASONS
    # Static text only: model-chosen property names never reach the reason.
    assert "variants" not in str(failure.value.reason)


def test_the_plan_validator_enforces_it_independently_of_the_contract():
    """Defence in depth: a plan object built WITHOUT contract validation (for
    example a model_copy) is still refused by the firewall itself."""
    approved = CommanderPlan.model_validate(corrected_plan())
    bad_task = approved.graph.tasks[1].model_copy(
        update={"output_schema": copy.deepcopy(RUN_280FC9E5_T02_SCHEMA)})
    graph = approved.graph.model_copy(update={"tasks": [approved.graph.tasks[0], bad_task]})
    with pytest.raises(PlanSchemaError) as failure:
        _validator()._validate_plan(approved.model_copy(update={"graph": graph}))
    assert failure.value.reason == REASON


@pytest.mark.parametrize("nested", [
    {"type": "array"},
    {"type": "object"},
    {"type": "object", "properties": {"a": {"type": "string"}}, "required": []},
    {"type": "array", "items": {"type": "object"}},
    {"type": "array", "items": {"type": "array"}},
    {"type": "string", "description": "free text"},
    {"type": "tuple"},
])
def test_every_nested_shape_the_runtime_cannot_enforce_is_rejected(nested):
    candidate = corrected_plan()
    schema = candidate["graph"]["tasks"][1]["output_schema"]
    schema["properties"]["extra"] = nested
    with pytest.raises(PlanSchemaError) as failure:
        _validator().validate(candidate)
    assert failure.value.reason == REASON


def test_the_corrected_schema_passes():
    approved = _validator().validate(corrected_plan())
    assert approved.graph.tasks[1].output_schema["properties"]["candidate_ids"] == \
        {"type": "array", "items": {"type": "string"}}


def test_a_closed_nested_object_the_runtime_supports_still_passes():
    candidate = corrected_plan()
    schema = candidate["graph"]["tasks"][1]["output_schema"]
    schema["properties"]["summary"] = {
        "type": "object", "properties": {"n": {"type": "integer"}},
        "required": ["n"], "additionalProperties": False}
    _validator().validate(candidate)


def test_the_rule_reaches_the_provider_visible_policy():
    rules = provider_plan_policy(PlanLimits(), ["search"])["rules"]
    matching = [rule for rule in rules if "scalar" in rule and '"items"' in rule]
    assert len(matching) == 1
    assert "provenance" in matching[0] and "variants" in matching[0]


# --- the one existing repair path -------------------------------------------

class _ScriptedCommanderClient:
    def __init__(self, *bodies: dict) -> None:
        self.bodies = list(bodies)
        self.repair_reasons: list[str | None] = []

    def create_plan(self, *, model, objective, context, repair_reason=None):
        self.repair_reasons.append(repair_reason)
        return self.bodies.pop(0)


def _commander(client, retries: list) -> Commander:
    return Commander(
        client=client,
        resolver=CommanderModelResolver(("kimi-k2.6",), {"kimi-k2.6"}),
        validator=_validator(),
        retry_callback=lambda agent, phase, reason: retries.append((agent, phase, reason)))


def test_the_rejection_is_repaired_once_with_exactly_the_static_reason():
    client = _ScriptedCommanderClient(run_280fc9e5_plan(), corrected_plan())
    retries: list = []
    approved = _commander(client, retries).plan(requested_model="kimi-k2.6",
                                                objective="o", context={})
    assert len(approved.graph.tasks) == 2
    assert client.repair_reasons == [None, REASON]
    assert retries == [("commander", "planning", REASON)]


def test_a_second_bad_plan_is_a_schema_failure_with_no_third_call():
    client = _ScriptedCommanderClient(run_280fc9e5_plan(), run_280fc9e5_plan(),
                                      corrected_plan())
    with pytest.raises(CommanderPlanFailure) as failure:
        _commander(client, []).plan(requested_model="kimi-k2.6", objective="o", context={})
    assert failure.value.code == "COMMANDER_PLAN_SCHEMA_INVALID"
    assert failure.value.code in REPAIRABLE_PLAN_FAILURES
    assert failure.value.validation_reason == REASON
    assert len(client.bodies) == 1


# --- property: the runtime validator has exactly two failure families -------

_SCALARS = ("string", "integer", "number", "boolean", "null")


def _random_schema(rng: random.Random, depth: int = 0):
    """Valid AND malformed schemas: missing items, open objects, bad keys."""
    roll = rng.random()
    if depth >= 3 or roll < 0.35:
        return rng.choice([{"type": rng.choice(_SCALARS)}, {"type": "tuple"},
                           {"type": ["string"]}, {}, None, "string", 7])
    if roll < 0.65:
        schema: dict = {"type": "array"}
        if rng.random() < 0.7:
            schema["items"] = _random_schema(rng, depth + 1)
        if rng.random() < 0.3:
            schema["maxItems"] = rng.choice([0, 1, 3, -1, None, "2", True])
        return schema
    schema = {"type": "object"}
    if rng.random() < 0.85:
        schema["properties"] = {f"p{index}": _random_schema(rng, depth + 1)
                                for index in range(rng.randint(0, 3))}
    if rng.random() < 0.8:
        schema["additionalProperties"] = rng.choice([False, False, True, None])
    if rng.random() < 0.8:
        names = list(schema.get("properties") or {})
        schema["required"] = rng.choice([names, names[:1], ["missing"], "p0", [["p0"]], [1]])
    return schema


def _random_value(rng: random.Random, depth: int = 0):
    roll = rng.random()
    if depth >= 4 or roll < 0.4:
        return rng.choice(["x", 0, 1.5, True, None, -3])
    if roll < 0.7:
        return [_random_value(rng, depth + 1) for _ in range(rng.randint(0, 3))]
    return {rng.choice(["p0", "p1", "p2", "other"]): _random_value(rng, depth + 1)
            for _ in range(rng.randint(0, 3))}


def _random_completion(rng: random.Random, value):
    return rng.choice([
        json.dumps(value), json.dumps(value).encode(), value if isinstance(value, dict) else value,
        "{not json", "", "[" * 5000, "[" * 5000 + "]" * 5000, None, 42,
        "x" * 40_000, json.dumps({"p0": [[[[[[[[[["deep"]]]]]]]]]]}),
    ])


@pytest.mark.parametrize("seed", range(20))
def test_validate_worker_output_raises_only_its_documented_failures(seed):
    rng = random.Random(seed)
    for _ in range(250):
        top = _random_schema(rng) if rng.random() < 0.3 else {
            "type": "object", "additionalProperties": False,
            "properties": {name: _random_schema(rng, 1) for name in ("p0", "p1", "p2")},
            "required": rng.choice([[], ["p0"], ["p0", "p1"]])}
        completion = _random_completion(rng, _random_value(rng))
        try:
            validate_worker_output(completion, top)
        except (WorkerOutputValidationError, ToolCallError):
            pass


@pytest.mark.parametrize("seed", range(10))
def test_plan_time_acceptance_implies_runtime_enforceability(seed):
    """Whatever plan time accepts, the runtime validator can judge: for every
    value it answers with a verdict (return or ValueError), and a schema
    plan time accepts never fails with a SCHEMA defect."""
    rng = random.Random(1000 + seed)
    for _ in range(250):
        schema = _random_schema(rng)
        accepted = output_schema_is_runtime_valid(schema)
        try:
            validate_schema(schema)
        except ValueError:
            assert not accepted
        else:
            assert accepted
        for _ in range(5):
            try:
                validate_json_schema(schema, _random_value(rng))
            except ValueError:
                pass

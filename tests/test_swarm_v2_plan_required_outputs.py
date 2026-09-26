"""PR-U S2: a completion criterion may only require what the task's schema requires.

Run 6825eb96 (Gate 0, partial_success): the Commander set
``completion.required_outputs = ["evidence_record"]`` on all ten tasks, while
every task's ``output_schema`` declared only outcome / resolved / ambiguous /
match_count / candidate_id. The firewall approved the plan, every task was paid
for, and every completed task then failed ``REQUIRED_OUTPUT_MISSING``.

The firewall now rejects that shape before anything is paid for, with the
static reason ``REQUIRED_OUTPUT_NOT_IN_SCHEMA``, and the Commander gets its one
existing repair attempt with exactly that code.
"""

from __future__ import annotations

import copy

import pytest

from backend.engines.swarm_v2 import (VALIDATION_REASONS, Commander, CommanderModelResolver,
                                      CommanderPlanFailure, PlanLimits, PlanValidationError,
                                      PlanValidator, provider_plan_policy)
from backend.engines.swarm_v2.commander import REPAIRABLE_PLAN_FAILURES
from test_swarm_v2 import plan, task, tool_descriptors

REASON = "REQUIRED_OUTPUT_NOT_IN_SCHEMA"

#: The output_schema every 6825eb96 task declared.
RUN_6825EB96_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "outcome": {"type": "string"},
        "resolved": {"type": "boolean"},
        "ambiguous": {"type": "boolean"},
        "match_count": {"type": "integer"},
        "candidate_id": {"type": "string"},
    },
    "required": ["outcome", "resolved", "ambiguous", "match_count", "candidate_id"],
    "additionalProperties": False,
}


def _6825eb96_task(task_id: str, required_outputs: list[str]) -> dict:
    item = task(task_id, f"verify register candidate {task_id}", cost=5)
    item["output_schema"] = copy.deepcopy(RUN_6825EB96_OUTPUT_SCHEMA)
    item["evidence"] = {"minimum_sources": 1, "required_fields": ["outcome"],
                        "min_confidence": 0.5}
    item["completion"] = {"required_outputs": list(required_outputs),
                          "evidence_satisfied": True, "allow_partial": True}
    return item


def run_6825eb96_plan() -> dict:
    """The ten-task shape of run 6825eb96: every task requires evidence_record."""
    return plan([_6825eb96_task(f"t{n:02d}", ["evidence_record"]) for n in range(1, 11)])


def corrected_plan() -> dict:
    return plan([_6825eb96_task(f"t{n:02d}", ["outcome", "candidate_id"])
                 for n in range(1, 11)])


def _validator() -> PlanValidator:
    return PlanValidator(allowed_tools=tool_descriptors("search"), limits=PlanLimits())


# --- the firewall ------------------------------------------------------------

def test_the_6825eb96_task_shape_is_rejected_with_the_new_static_reason():
    with pytest.raises(PlanValidationError) as failure:
        _validator().validate(run_6825eb96_plan())
    assert failure.value.reason == REASON
    assert REASON in VALIDATION_REASONS
    # Static text only: the offending output name never reaches the message.
    assert "evidence_record" not in str(failure.value)


def test_the_corrected_shape_passes():
    approved = _validator().validate(corrected_plan())
    assert [item.completion.required_outputs for item in approved.graph.tasks] == \
        [["outcome", "candidate_id"]] * 10


def test_one_bad_task_among_good_ones_is_enough_to_reject():
    candidate = corrected_plan()
    candidate["graph"]["tasks"][6]["completion"]["required_outputs"] = ["outcome",
                                                                        "evidence_record"]
    with pytest.raises(PlanValidationError) as failure:
        _validator().validate(candidate)
    assert failure.value.reason == REASON


def test_a_declared_but_optional_property_is_not_enough():
    """Both conditions: in output_schema.required AND in output_schema.properties."""
    candidate = corrected_plan()
    for item in candidate["graph"]["tasks"]:
        item["output_schema"]["required"] = ["outcome"]
        item["completion"]["required_outputs"] = ["outcome", "candidate_id"]
    with pytest.raises(PlanValidationError) as failure:
        _validator().validate(candidate)
    assert failure.value.reason == REASON


def test_the_rule_reaches_the_provider_visible_policy_in_plain_words():
    rules = provider_plan_policy(PlanLimits(), ["search"])["rules"]
    matching = [rule for rule in rules if "completion.required_outputs" in rule]
    assert len(matching) == 1
    assert "output_schema.required" in matching[0]
    assert "output_schema.properties" in matching[0]


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
    client = _ScriptedCommanderClient(run_6825eb96_plan(), corrected_plan())
    retries: list = []
    approved = _commander(client, retries).plan(requested_model="kimi-k2.6",
                                                objective="o", context={})
    assert len(approved.graph.tasks) == 10
    assert client.repair_reasons == [None, REASON]
    assert retries == [("commander", "planning", REASON)]


def test_a_second_bad_plan_ends_planning_with_no_third_call():
    client = _ScriptedCommanderClient(run_6825eb96_plan(), run_6825eb96_plan(),
                                      corrected_plan())
    retries: list = []
    with pytest.raises(CommanderPlanFailure) as failure:
        _commander(client, retries).plan(requested_model="kimi-k2.6", objective="o",
                                         context={})
    assert failure.value.code == "COMMANDER_PLAN_SCHEMA_INVALID"
    assert failure.value.code in REPAIRABLE_PLAN_FAILURES
    assert failure.value.validation_reason == REASON
    assert client.repair_reasons == [None, REASON]
    assert len(client.bodies) == 1

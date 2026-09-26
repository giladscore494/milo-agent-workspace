"""PR-X S2: a refused replan decision leaves ONE diagnosable, material-free line.

Run c4b8bb54 failed on COMMANDER_DECISION_INVALID and nothing in the logs said
which rule the answer broke. `Commander.replan` now writes one structured line
before it raises: pydantic error TYPES only, the top-level keys only where they
are decision field names, every other key only as a count, and the length of
`reason` -- never a value, never an unknown key's name.
"""

from __future__ import annotations

import json
import logging

import pytest

from backend.engines.swarm_v2 import (Commander, CommanderModelResolver, PlanLimits,
                                      PlanValidator)
from backend.engines.swarm_v2.commander import CommanderPlanFailure
from backend.engines.swarm_v2.contracts import commander_decision_json_schema
from backend.engines.swarm_v2.request_builder import is_strict_compatible
from test_swarm_v2 import plan, task, tool_descriptors

LOGGER = "milo.swarm_v2.commander"
SENTINEL = "PROVIDER_SECRET_SENTINEL sk-live-000"


class Client:
    def __init__(self, decision):
        self.decision = decision

    def create_plan(self, **_kwargs):
        raise AssertionError("planning is not under test")

    def create_replan(self, **_kwargs):
        return self.decision


def replan(decision):
    commander = Commander(client=Client(decision),
                          resolver=CommanderModelResolver(("fake",), {"fake"}),
                          validator=PlanValidator(allowed_tools=tool_descriptors("search"),
                                                  limits=PlanLimits(max_tasks=3,
                                                                    max_tool_calls=10)))
    return commander.replan(requested_model="fake", objective="o", summary={})


def lines(caplog) -> list[dict]:
    return [json.loads(record.getMessage()) for record in caplog.records
            if record.name == LOGGER]


def refused(caplog, decision) -> dict:
    caplog.set_level(logging.INFO, logger=LOGGER)
    with pytest.raises(CommanderPlanFailure) as failure:
        replan(decision)
    assert failure.value.code == "COMMANDER_DECISION_INVALID"
    (line,) = lines(caplog)
    assert set(line) == {"event", "error_types", "top_level_fields",
                         "unknown_field_count", "reason_length"}
    assert line["event"] == "commander_decision_invalid"
    return line


def test_an_extra_top_level_key_is_counted_never_named(caplog):
    line = refused(caplog, {"decision": "FINISH", "plan": None, "reason": "done",
                            SENTINEL: SENTINEL, "another_secret_key": [SENTINEL]})
    assert line == {"event": "commander_decision_invalid", "error_types": ["extra_forbidden"],
                    "top_level_fields": ["decision", "plan", "reason"],
                    "unknown_field_count": 2, "reason_length": 4}
    assert SENTINEL not in json.dumps(line) and "another_secret_key" not in json.dumps(line)


def test_finish_with_a_plan_reports_the_type_not_the_plan(caplog):
    line = refused(caplog, json.dumps({"decision": "FINISH", "reason": SENTINEL,
                                       "plan": plan([task("a", SENTINEL)])}))
    assert line["top_level_fields"] == ["decision", "plan", "reason"]
    assert line["unknown_field_count"] == 0
    assert line["reason_length"] == len(SENTINEL)
    assert "value_error" in line["error_types"]
    assert SENTINEL not in json.dumps(line)


def test_a_600_char_reason_reports_its_length_only(caplog):
    line = refused(caplog, {"decision": "FINISH", "plan": None, "reason": "Z" * 600})
    assert line["error_types"] == ["string_too_long"]
    assert line["reason_length"] == 600
    assert "ZZZ" not in json.dumps(line)


def test_non_json_text_reports_json_invalid_and_no_fields(caplog):
    line = refused(caplog, f"I think we are done. {SENTINEL}")
    assert line == {"event": "commander_decision_invalid", "error_types": ["json_invalid"],
                    "top_level_fields": [], "unknown_field_count": 0, "reason_length": None}


def test_a_nested_invalid_plan_lists_only_static_types(caplog):
    broken = task("a", "a")
    broken["output_schema"] = {"type": "object",
                               "properties": {"answer": {"type": SENTINEL}},
                               "required": ["answer"], "additionalProperties": False}
    line = refused(caplog, {"decision": "ADD_TASKS", "plan": plan([broken]), "reason": "r"})
    assert line["error_types"] and all(isinstance(item, str) for item in line["error_types"])
    assert SENTINEL not in json.dumps(line)


def test_an_unsafe_reason_is_named_by_a_static_type(caplog):
    line = refused(caplog, {"decision": "FINISH", "plan": None,
                            "reason": "leaky hidden reasoning follows"})
    assert line["error_types"] == ["durable_value_rejected"]
    assert "leaky" not in json.dumps(line)


def test_a_plan_refused_by_the_firewall_logs_its_static_codes_only(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    too_many = plan([task(f"t{index}", SENTINEL) for index in range(4)])
    with pytest.raises(CommanderPlanFailure) as failure:
        replan({"decision": "ADD_TASKS", "plan": too_many, "reason": "more"})
    assert failure.value.code == "COMMANDER_PLAN_LIMIT_EXCEEDED"
    (line,) = lines(caplog)
    assert line == {"event": "commander_replan_plan_invalid",
                    "code": "COMMANDER_PLAN_LIMIT_EXCEEDED",
                    "validation_reason": failure.value.validation_reason}
    assert SENTINEL not in json.dumps(line)


def test_a_valid_decision_logs_nothing(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    assert replan({"decision": "FINISH", "plan": None, "reason": "done"}).decision == "FINISH"
    assert lines(caplog) == []


def test_s2_3_the_decision_schema_is_not_sent_as_strict_json_schema():
    """Documents the S2.3 finding (not changed in this PR): create_replan
    passes no `schema=` to the gateway, so the request carries
    `response_format: {"type": "json_object"}` and the decision schema only
    as system-prompt text. It would not qualify for strict mode either."""
    assert not is_strict_compatible(commander_decision_json_schema())

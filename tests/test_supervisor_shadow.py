from uuid import uuid4

import pytest
from pydantic import ValidationError

from backend.supervisor import (
    AgentMessage,
    MessageType,
    ShadowEvaluationReport,
    SupervisorDecision,
    SupervisorInput,
    WakeCondition,
    apply_event_to_blackboard,
    build_evaluation_report,
    initial_blackboard,
    make_shadow_decision,
    route_event_message,
)


def test_blackboard_updates_artifacts_missing_fields_and_completion():
    board = initial_blackboard("catalog Hyundai models")
    updated = apply_event_to_blackboard(board, "chunk_completed", {"phase": "technical_enrichment", "agent": "pricing", "result": {"missing_data": [{"field": "price"}], "items": []}})
    assert "technical_enrichment" in updated.completed_tasks
    assert updated.artifacts["technical_enrichment"]["items"] == []
    assert updated.missing_fields[0]["item"]["field"] == "price"
    assert updated.completion_score > 0


def test_message_routing_persists_supported_event_types():
    run_id = uuid4()
    message = route_event_message(run_id, "chunk_failed", {"phase": "verification", "agent": "source_verifier", "error": "bad"})
    assert isinstance(message, AgentMessage)
    assert message.type == MessageType.TASK_FAILED
    assert message.recipient == "supervisor"


def test_wake_conditions_and_gap_recommendation_are_structured():
    board = initial_blackboard("catalog")
    board = apply_event_to_blackboard(board, "chunk_completed", {"phase": "technical", "result": {"missing_data": [{"field": "range"}]}})
    decision = make_shadow_decision(SupervisorInput(goal="catalog", compiled_workflow=board.approved_plan, blackboard=board))
    assert isinstance(decision.next_wake_condition, WakeCondition)
    assert decision.proposed_commands[0].command == "fill_gap"


def test_malformed_and_hidden_reasoning_decisions_are_rejected():
    with pytest.raises(ValidationError):
        SupervisorDecision.model_validate({"assessment": "hidden chain-of-thought", "proposed_commands": [], "next_wake_condition": {"kind": "event"}, "rationale_summary": "ok"})
    with pytest.raises(ValidationError):
        SupervisorDecision.model_validate({"assessment": "ok", "proposed_commands": [{"command": "execute_tool", "rationale": "bad"}], "next_wake_condition": {"kind": "event"}, "rationale_summary": "ok"})


def test_repeated_decisions_are_suppressed_by_loop_detection():
    board = initial_blackboard("catalog")
    board = apply_event_to_blackboard(board, "chunk_completed", {"phase": "technical", "result": {"missing_data": [{"field": "range"}]}})
    prior = [{"proposed_commands": [{"command": "fill_gap", "task_key": "targeted_gap_filler"}]}, {"proposed_commands": [{"command": "fill_gap", "task_key": "targeted_gap_filler"}]}]
    decision = make_shadow_decision(SupervisorInput(goal="catalog", compiled_workflow=board.approved_plan, blackboard=board), previous_decisions=prior)
    assert decision.proposed_commands == []


def test_shadow_mode_cannot_mutate_execution_or_report_executed_commands():
    board = initial_blackboard("catalog")
    decision = make_shadow_decision(SupervisorInput(goal="catalog", compiled_workflow=board.approved_plan, blackboard=board))
    report = build_evaluation_report(decision, ["phase_started", "phase_completed"])
    assert report.executed_commands == []
    assert report.workflow_mutated is False
    with pytest.raises(ValidationError):
        ShadowEvaluationReport(summary="bad", executed_commands=[{"command": "fill_gap"}])

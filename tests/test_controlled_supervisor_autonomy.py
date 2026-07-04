import pytest
from pydantic import ValidationError

from backend.supervisor import (
    CollaborationRecord,
    CollaborationType,
    CommandValidationError,
    ControlledCommand,
    ControlledCommandType,
    ControlledRunState,
    SupervisorLimits,
    SupervisorMode,
    execute_controlled_command,
    record_collaboration,
    spawn_dynamic_agent,
    validate_controlled_command,
)


def state(**kwargs):
    defaults = {"goal": "catalog Hyundai models"}
    defaults.update(kwargs)
    return ControlledRunState(**defaults)


def command(command_type, **kwargs):
    defaults = {"command": command_type, "goal_reference": "catalog Hyundai models"}
    defaults.update(kwargs)
    return ControlledCommand(**defaults)


def test_missing_data_spawns_approved_gap_filler_with_typed_contract():
    run = state(mode=SupervisorMode.ACTIVE)
    cmd = command(ControlledCommandType.SPAWN_AGENT, template="targeted_gap_filler", task_key="fill_range", payload={"missing_fields": [{"field": "range"}]})

    updated = execute_controlled_command(cmd, run)

    agent = updated.dynamic_agents["fill_range"]
    assert agent.template == "targeted_gap_filler"
    assert agent.input_schema == {"missing_fields": "list"}
    assert agent.internet_allowed is False
    assert agent.completion_criteria


def test_conflict_can_spawn_resolver_and_collaboration_is_visible_to_supervisor():
    run = state(mode=SupervisorMode.ACTIVE, open_conflicts=[{"claim": "price mismatch"}])
    updated = execute_controlled_command(command(ControlledCommandType.SPAWN_AGENT, template="conflict_resolver", task_key="resolve_price"), run)
    updated = record_collaboration(updated, CollaborationRecord(type=CollaborationType.CONFLICT, sender="resolve_price", recipient="supervisor", task_key="resolve_price", payload={"conflict": "price mismatch"}))

    assert "resolve_price" in updated.dynamic_agents
    assert updated.collaborations[0].type == CollaborationType.CONFLICT
    assert updated.audit_log[-1]["visible_to"] == "supervisor"


def test_failure_retry_is_limited_and_persisted():
    run = state(mode=SupervisorMode.ACTIVE, failed_tasks={"technical": 0})
    updated = execute_controlled_command(command(ControlledCommandType.RETRY_TASK, task_key="technical"), run)
    assert updated.failed_tasks["technical"] == 1

    blocked = state(mode=SupervisorMode.ACTIVE, failed_tasks={"technical": 2}, limits=SupervisorLimits(max_retries=2))
    with pytest.raises(CommandValidationError, match="retry limit"):
        validate_controlled_command(command(ControlledCommandType.RETRY_TASK, task_key="technical"), blocked)


def test_duplicate_task_rejected():
    cmd = command(ControlledCommandType.SPAWN_AGENT, template="targeted_gap_filler", task_key="fill_range")
    run = execute_controlled_command(cmd, state(mode=SupervisorMode.ACTIVE))
    with pytest.raises(CommandValidationError, match="duplicate task"):
        validate_controlled_command(cmd, run)


def test_exhausted_budget_blocks_spawn():
    run = state(limits=SupervisorLimits(token_budget=100, search_budget=0))
    cmd = command(ControlledCommandType.SPAWN_AGENT, template="targeted_gap_filler", task_key="fill_range")
    with pytest.raises(CommandValidationError, match="budget exhausted"):
        validate_controlled_command(cmd, run)


def test_user_constraint_update_is_stored_wakes_supervisor_and_audited():
    run = state(mode=SupervisorMode.ACTIVE)
    cmd = command(ControlledCommandType.UPDATE_RUN_CONSTRAINT, payload={"forbid_internet": True, "task_keys": ["fill_range"]})
    updated = execute_controlled_command(cmd, run)
    assert updated.user_instructions[0]["type"] == "user_instruction"
    assert updated.audit_log[-1] == {"event": "wake_supervisor", "reason": "user_instruction"}


def test_invalid_command_and_unapproved_template_are_rejected():
    with pytest.raises(ValidationError):
        ControlledCommand.model_validate({"command": "delete_everything", "goal_reference": "catalog Hyundai models"})
    with pytest.raises(CommandValidationError, match="invalid target/template|unapproved"):
        validate_controlled_command(command(ControlledCommandType.SPAWN_AGENT, template="freeform_agent", task_key="bad"), state())
    with pytest.raises(CommandValidationError, match="separate authorization"):
        validate_controlled_command(command(ControlledCommandType.SEND_MESSAGE, target="user", requires_internet=True), state())


def test_loop_detection_recursion_and_decision_caps_block_commands():
    with pytest.raises(CommandValidationError, match="recursion"):
        validate_controlled_command(command(ControlledCommandType.SPLIT_TASK, recursion_depth=3), state(limits=SupervisorLimits(max_recursion=2)))
    with pytest.raises(CommandValidationError, match="decision limit"):
        validate_controlled_command(command(ControlledCommandType.ASK_USER, target="user"), state(decisions_made=20, limits=SupervisorLimits(max_decisions=20)))


def test_shadow_mode_validates_but_does_not_spawn_while_active_mode_spawns():
    cmd = command(ControlledCommandType.SPAWN_AGENT, template="targeted_gap_filler", task_key="fill_range")
    shadow = execute_controlled_command(cmd, state(mode=SupervisorMode.SHADOW))
    active = execute_controlled_command(cmd, state(mode=SupervisorMode.ACTIVE))

    assert shadow.dynamic_agents == {}
    assert shadow.audit_log[0]["mode"] == "shadow"
    assert "fill_range" in active.dynamic_agents


def test_final_build_gate_requires_tasks_conflicts_evidence_and_verification():
    cmd = command(ControlledCommandType.REQUEST_FINAL_BUILD, target="final_builder")
    blocked = state(required_tasks=["discovery"], completed_tasks=[])
    with pytest.raises(CommandValidationError, match="final-build prerequisites"):
        validate_controlled_command(cmd, blocked)

    ready = state(required_tasks=["discovery"], completed_tasks=["discovery"], evidence_contract_met=True, verification_complete=True)
    validate_controlled_command(cmd, ready)


def test_agent_factory_rejects_unapproved_templates():
    with pytest.raises(CommandValidationError):
        spawn_dynamic_agent("not_approved", "x")

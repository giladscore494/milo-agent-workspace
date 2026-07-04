from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.engines.vehicle_catalog_v1.workflow import build_milo_blueprint, compile_workflow


class MessageType(StrEnum):
    TASK_ASSIGNED = "task_assigned"
    TASK_STARTED = "task_started"
    PROGRESS = "progress"
    FINDING = "finding"
    ARTIFACT_CREATED = "artifact_created"
    MISSING_INFORMATION = "missing_information"
    CONFLICT_FOUND = "conflict_found"
    REQUEST_HELP = "request_help"
    REQUEST_CONTEXT = "request_context"
    PROPOSE_SUBTASK = "propose_subtask"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    REVIEW_RESULT = "review_result"
    USER_INSTRUCTION = "user_instruction"
    DECISION = "decision"


class ProposedCommandType(StrEnum):
    ASSIGN_TASK = "assign_task"
    REQUEST_CONTEXT = "request_context"
    REVIEW_ARTIFACT = "review_artifact"
    FILL_GAP = "fill_gap"
    RESOLVE_CONFLICT = "resolve_conflict"
    WAIT = "wait"


class WakeCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["event", "message_count", "phase_change", "budget_threshold", "run_finished"]
    value: str | int | float | None = None


class ProposedCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: ProposedCommandType
    target_agent: str | None = None
    task_key: str | None = None
    rationale: str = Field(min_length=1, max_length=500)
    payload: dict[str, Any] = Field(default_factory=dict)


class SupervisorDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assessment: str = Field(min_length=1, max_length=1000)
    proposed_commands: list[ProposedCommand] = Field(default_factory=list, max_length=5)
    next_wake_condition: WakeCondition
    rationale_summary: str = Field(min_length=1, max_length=500)

    @field_validator("assessment", "rationale_summary")
    @classmethod
    def no_hidden_reasoning(cls, value: str) -> str:
        banned = ("chain-of-thought", "hidden reasoning", "private reasoning", "step-by-step internal")
        if any(term in value.lower() for term in banned):
            raise ValueError("hidden reasoning must not be stored")
        return value


class Blackboard(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str
    approved_plan: dict[str, Any]
    known_entities: list[dict[str, Any]] = Field(default_factory=list)
    completed_tasks: list[str] = Field(default_factory=list)
    active_agents: list[str] = Field(default_factory=list)
    open_questions: list[dict[str, Any]] = Field(default_factory=list)
    missing_fields: list[dict[str, Any]] = Field(default_factory=list)
    claims_conflict_summaries: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: dict[str, Any] = Field(default_factory=dict)
    remaining_budget: dict[str, int] = Field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0})
    completion_score: float = Field(default=0.0, ge=0.0, le=1.0)


class AgentMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    type: MessageType
    sender: str
    recipient: str
    task_key: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    read_at: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class SupervisorInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str
    compiled_workflow: dict[str, Any]
    blackboard: Blackboard
    agent_statuses: list[dict[str, Any]] = Field(default_factory=list)
    unread_messages: list[AgentMessage] = Field(default_factory=list)
    open_conflicts: list[dict[str, Any]] = Field(default_factory=list)
    budget: dict[str, int] = Field(default_factory=dict)
    recent_user_instructions: list[str] = Field(default_factory=list)


class ShadowEvaluationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["shadow"] = "shadow"
    executed_commands: list[dict[str, Any]] = Field(default_factory=list)
    proposed_commands: list[dict[str, Any]] = Field(default_factory=list)
    workflow_mutated: bool = False
    summary: str

    @model_validator(mode="after")
    def shadow_never_executes(self) -> "ShadowEvaluationReport":
        if self.executed_commands or self.workflow_mutated:
            raise ValueError("shadow mode cannot execute commands or mutate workflow")
        return self


def initial_blackboard(goal: str) -> Blackboard:
    workflow = build_milo_blueprint().workflows[0]
    compiled = compile_workflow(workflow)
    return Blackboard(goal=goal, approved_plan=compiled.model_dump(), active_agents=list(compiled.agents))


def apply_event_to_blackboard(blackboard: Blackboard, event_type: str, payload: dict[str, Any]) -> Blackboard:
    data = blackboard.model_dump()
    phase = payload.get("phase") or event_type
    if event_type in {"phase_completed", "chunk_completed", "checkpoint_saved"} and phase not in data["completed_tasks"]:
        data["completed_tasks"].append(phase)
    if payload.get("agent") and payload["agent"] not in data["active_agents"]:
        data["active_agents"].append(payload["agent"])
    result = payload.get("result") or payload.get("results")
    if result is not None:
        data["artifacts"][phase] = result
    for item in _collect_key(result, "missing_data"):
        data["missing_fields"].append({"phase": phase, "item": item})
    for item in _collect_key(result, "needs_review"):
        data["open_questions"].append({"phase": phase, "item": item})
    for item in _collect_key(result, "rejected_data_points"):
        data["claims_conflict_summaries"].append({"phase": phase, "item": item})
    total = len(data["approved_plan"].get("ordered_task_keys", ())) or 1
    data["completion_score"] = min(1.0, len(set(data["completed_tasks"])) / total)
    return Blackboard.model_validate(data)


def route_event_message(run_id: UUID, event_type: str, payload: dict[str, Any]) -> AgentMessage | None:
    mapping = {
        "phase_started": MessageType.TASK_STARTED,
        "phase_completed": MessageType.TASK_COMPLETED,
        "chunk_completed": MessageType.ARTIFACT_CREATED,
        "chunk_failed": MessageType.TASK_FAILED,
        "run_failed": MessageType.TASK_FAILED,
        "checkpoint_saved": MessageType.PROGRESS,
    }
    msg_type = mapping.get(event_type)
    if not msg_type:
        return None
    agent = payload.get("agent") or payload.get("phase") or "engine"
    return AgentMessage(run_id=run_id, type=msg_type, sender=agent, recipient="supervisor", task_key=payload.get("phase"), payload=payload)


def make_shadow_decision(supervisor_input: SupervisorInput, *, max_decisions_per_run: int = 20, proposed_agent_cap: int = 3, previous_decisions: list[dict[str, Any]] | None = None) -> SupervisorDecision:
    previous_decisions = previous_decisions or []
    if len(previous_decisions) >= max_decisions_per_run:
        return SupervisorDecision(assessment="Decision frequency cap reached; supervisor should wait.", proposed_commands=[], next_wake_condition=WakeCondition(kind="run_finished"), rationale_summary="Frequency cap prevents additional shadow recommendations.")
    open_conflicts = supervisor_input.open_conflicts or supervisor_input.blackboard.claims_conflict_summaries
    commands: list[ProposedCommand] = []
    if open_conflicts:
        commands.append(ProposedCommand(command=ProposedCommandType.RESOLVE_CONFLICT, task_key="conflict_resolver", rationale="Open conflicts should be reviewed in shadow mode only.", payload={"conflicts": open_conflicts[:3]}))
    if supervisor_input.blackboard.missing_fields:
        commands.append(ProposedCommand(command=ProposedCommandType.FILL_GAP, task_key="targeted_gap_filler", rationale="Missing fields are candidates for targeted follow-up in shadow mode only.", payload={"missing_fields": supervisor_input.blackboard.missing_fields[:5]}))
    if not commands:
        commands.append(ProposedCommand(command=ProposedCommandType.WAIT, rationale="Fixed MILO workflow is progressing without supervisor intervention."))
    commands = _remove_repeated_commands(commands, previous_decisions)[:proposed_agent_cap]
    return SupervisorDecision(assessment="Shadow supervisor assessed the current run state without changing execution.", proposed_commands=commands, next_wake_condition=WakeCondition(kind="event", value="next_run_event"), rationale_summary="Recommendations are persisted for review and are never executed in shadow mode.")


def build_evaluation_report(decision: SupervisorDecision, fixed_workflow_events: list[str]) -> ShadowEvaluationReport:
    return ShadowEvaluationReport(proposed_commands=[c.model_dump(mode="json") for c in decision.proposed_commands], summary=f"Compared {len(decision.proposed_commands)} shadow recommendations against {len(fixed_workflow_events)} fixed workflow events; no commands executed.")


def _collect_key(value: Any, key: str) -> list[Any]:
    found: list[Any] = []
    if isinstance(value, dict):
        if isinstance(value.get(key), list):
            found.extend(value[key])
        for child in value.values():
            found.extend(_collect_key(child, key))
    elif isinstance(value, list):
        for child in value:
            found.extend(_collect_key(child, key))
    return found


def _remove_repeated_commands(commands: list[ProposedCommand], previous_decisions: list[dict[str, Any]]) -> list[ProposedCommand]:
    seen = Counter()
    for decision in previous_decisions:
        for command in decision.get("proposed_commands", []):
            seen[(command.get("command"), command.get("task_key"))] += 1
    return [command for command in commands if seen[(command.command.value, command.task_key)] < 2]

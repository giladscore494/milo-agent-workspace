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


class SupervisorMode(StrEnum):
    SHADOW = "shadow"
    ACTIVE = "active"


class ControlledCommandType(StrEnum):
    SPAWN_AGENT = "spawn_agent"
    ASSIGN_TASK = "assign_task"
    SEND_MESSAGE = "send_message"
    REQUEST_REVISION = "request_revision"
    SPLIT_TASK = "split_task"
    RETRY_TASK = "retry_task"
    CANCEL_TASK = "cancel_task"
    ASK_USER = "ask_user"
    UPDATE_RUN_CONSTRAINT = "update_run_constraint"
    MARK_CLAIM_REVIEWED = "mark_claim_reviewed"
    REQUEST_FINAL_BUILD = "request_final_build"
    FINISH_RUN = "finish_run"


class CollaborationType(StrEnum):
    CLARIFICATION = "clarification"
    CONFLICT = "conflict"
    CONTEXT_REQUEST = "context_request"
    PROPOSED_SUBTASK = "proposed_subtask"
    ARTIFACT = "artifact"


class CommandValidationError(ValueError):
    pass


class AgentTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    responsibility: str = Field(min_length=1)
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    tools: list[str] = Field(default_factory=list)
    internet_allowed: bool = False
    token_budget: int = Field(gt=0)
    search_budget: int = Field(default=0, ge=0)
    timeout_seconds: int = Field(gt=0)
    max_retries: int = Field(default=1, ge=0)
    fallback: str | None = None
    completion_criteria: list[str] = Field(default_factory=list, min_length=1)


APPROVED_AGENT_TEMPLATES: dict[str, AgentTemplate] = {
    "targeted_gap_filler": AgentTemplate(key="targeted_gap_filler", responsibility="Fill explicitly identified missing catalog fields only.", input_schema={"missing_fields": "list"}, output_schema={"filled_fields": "list", "evidence": "list"}, tools=["catalog_read"], internet_allowed=False, token_budget=4000, timeout_seconds=300, completion_criteria=["Every requested field is filled or marked unavailable with evidence."]),
    "conflict_resolver": AgentTemplate(key="conflict_resolver", responsibility="Review conflicting claims and recommend a deterministic resolution or needs_review.", input_schema={"conflicts": "list"}, output_schema={"resolutions": "list", "needs_review": "list"}, tools=["catalog_read"], internet_allowed=False, token_budget=3000, timeout_seconds=240, completion_criteria=["Every conflict has a resolution or needs_review reason."]),
    "failure_retry_worker": AgentTemplate(key="failure_retry_worker", responsibility="Retry a failed narrow task using the original task contract.", input_schema={"failed_task": "object"}, output_schema={"status": "string", "artifact": "object"}, tools=["catalog_read"], internet_allowed=False, token_budget=3000, timeout_seconds=240, completion_criteria=["Retry result preserves original task contract."]),
    "final_build_verifier": AgentTemplate(key="final_build_verifier", responsibility="Verify final-build prerequisites and evidence contract.", input_schema={"artifacts": "object"}, output_schema={"verification_complete": "boolean", "gaps": "list"}, tools=["catalog_read"], internet_allowed=False, token_budget=2000, timeout_seconds=180, completion_criteria=["Verification is complete and all gaps are listed."]),
}


class SupervisorLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_dynamic_agents: int = 3
    max_active_agents: int = 2
    max_messages: int = 100
    max_retries: int = 2
    max_recursion: int = 2
    max_decisions: int = 20
    token_budget: int = 12000
    search_budget: int = 0
    run_timeout_seconds: int = 1800


class DynamicAgentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    template: str
    responsibility: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    tool_policy: list[str]
    internet_allowed: bool
    token_budget: int
    search_budget: int
    timeout_seconds: int
    max_retries: int
    fallback: str | None
    completion_criteria: list[str]


class CollaborationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: CollaborationType
    sender: str
    recipient: str
    task_key: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class ControlledCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: ControlledCommandType
    target: str | None = None
    template: str | None = None
    task_key: str | None = None
    goal_reference: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    requires_internet: bool = False
    recursion_depth: int = 0


class ControlledRunState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: UUID = Field(default_factory=uuid4)
    mode: SupervisorMode = SupervisorMode.SHADOW
    goal: str
    limits: SupervisorLimits = Field(default_factory=SupervisorLimits)
    dynamic_agents: dict[str, DynamicAgentSpec] = Field(default_factory=dict)
    active_agents: list[str] = Field(default_factory=list)
    completed_tasks: list[str] = Field(default_factory=list)
    required_tasks: list[str] = Field(default_factory=list)
    failed_tasks: dict[str, int] = Field(default_factory=dict)
    task_signatures: list[str] = Field(default_factory=list)
    messages: list[AgentMessage] = Field(default_factory=list)
    collaborations: list[CollaborationRecord] = Field(default_factory=list)
    user_instructions: list[dict[str, Any]] = Field(default_factory=list)
    audit_log: list[dict[str, Any]] = Field(default_factory=list)
    open_conflicts: list[dict[str, Any]] = Field(default_factory=list)
    needs_review: list[dict[str, Any]] = Field(default_factory=list)
    evidence_contract_met: bool = False
    verification_complete: bool = False
    decisions_made: int = 0
    tokens_used: int = 0
    searches_used: int = 0
    internet_authorized: bool = False


def spawn_dynamic_agent(template_key: str, task_key: str, responsibility: str | None = None) -> DynamicAgentSpec:
    template = APPROVED_AGENT_TEMPLATES.get(template_key)
    if template is None:
        raise CommandValidationError(f"unapproved agent template: {template_key}")
    return DynamicAgentSpec(id=task_key, template=template.key, responsibility=responsibility or template.responsibility, input_schema=template.input_schema, output_schema=template.output_schema, tool_policy=template.tools, internet_allowed=template.internet_allowed, token_budget=template.token_budget, search_budget=template.search_budget, timeout_seconds=template.timeout_seconds, max_retries=template.max_retries, fallback=template.fallback, completion_criteria=template.completion_criteria)


def validate_controlled_command(command: ControlledCommand, state: ControlledRunState) -> None:
    if command.command not in ControlledCommandType:
        raise CommandValidationError("command is not allowed")
    if state.decisions_made >= state.limits.max_decisions:
        raise CommandValidationError("decision limit exhausted")
    if len(state.messages) >= state.limits.max_messages:
        raise CommandValidationError("message limit exhausted")
    if command.recursion_depth > state.limits.max_recursion:
        raise CommandValidationError("recursion depth exceeded")
    if command.requires_internet and not state.internet_authorized:
        raise CommandValidationError("internet use requires separate authorization")
    if command.template and command.template not in APPROVED_AGENT_TEMPLATES:
        raise CommandValidationError("invalid target/template")
    if command.target and command.target not in state.dynamic_agents and command.target not in {"supervisor", "user", "final_builder"}:
        raise CommandValidationError("invalid target/template")
    if command.goal_reference.lower() not in state.goal.lower() and state.goal.lower() not in command.goal_reference.lower():
        raise CommandValidationError("task is not related to goal")
    missing_deps = [dep for dep in command.depends_on if dep not in state.completed_tasks]
    if missing_deps:
        raise CommandValidationError(f"dependencies are incomplete: {missing_deps}")
    signature = f"{command.command.value}:{command.template}:{command.task_key}:{command.payload}"
    if command.command in {ControlledCommandType.SPAWN_AGENT, ControlledCommandType.ASSIGN_TASK, ControlledCommandType.SPLIT_TASK} and signature in state.task_signatures:
        raise CommandValidationError("duplicate task rejected")
    if command.command == ControlledCommandType.SPAWN_AGENT:
        if not command.template or not command.task_key:
            raise CommandValidationError("spawn_agent requires template and task_key")
        template = APPROVED_AGENT_TEMPLATES[command.template]
        if len(state.dynamic_agents) >= state.limits.max_dynamic_agents or len(state.active_agents) >= state.limits.max_active_agents:
            raise CommandValidationError("concurrency limit blocks spawn")
        if state.tokens_used + template.token_budget > state.limits.token_budget or state.searches_used + template.search_budget > state.limits.search_budget:
            raise CommandValidationError("budget exhausted")
    if command.command == ControlledCommandType.RETRY_TASK and command.task_key:
        if state.failed_tasks.get(command.task_key, 0) >= state.limits.max_retries:
            raise CommandValidationError("retry limit exhausted")
    if command.command == ControlledCommandType.REQUEST_FINAL_BUILD:
        incomplete = [task for task in state.required_tasks if task not in state.completed_tasks]
        unresolved = [c for c in state.open_conflicts if c not in state.needs_review]
        if incomplete or unresolved or not state.evidence_contract_met or not state.verification_complete:
            raise CommandValidationError("final-build prerequisites are not met")


def execute_controlled_command(command: ControlledCommand, state: ControlledRunState) -> ControlledRunState:
    validate_controlled_command(command, state)
    data = state.model_dump()
    data["decisions_made"] += 1
    data["task_signatures"].append(f"{command.command.value}:{command.template}:{command.task_key}:{command.payload}")
    data["audit_log"].append({"command": command.model_dump(mode="json"), "validated_at": datetime.now(UTC).isoformat(), "mode": state.mode.value})
    if state.mode == SupervisorMode.SHADOW:
        return ControlledRunState.model_validate(data)
    if command.command == ControlledCommandType.SPAWN_AGENT:
        spec = spawn_dynamic_agent(command.template or "", command.task_key or "", command.payload.get("responsibility"))
        data["dynamic_agents"][spec.id] = spec.model_dump()
        data["active_agents"].append(spec.id)
        data["tokens_used"] += spec.token_budget
        data["searches_used"] += spec.search_budget
    elif command.command == ControlledCommandType.UPDATE_RUN_CONSTRAINT:
        data["user_instructions"].append({"type": "user_instruction", "payload": command.payload, "created_at": datetime.now(UTC).isoformat()})
        data["audit_log"].append({"event": "wake_supervisor", "reason": "user_instruction"})
    elif command.command == ControlledCommandType.SEND_MESSAGE:
        data["messages"].append(AgentMessage(run_id=state.run_id, type=MessageType.DECISION, sender="supervisor", recipient=command.target or "supervisor", task_key=command.task_key, payload=command.payload).model_dump())
    elif command.command == ControlledCommandType.RETRY_TASK and command.task_key:
        data["failed_tasks"][command.task_key] = data["failed_tasks"].get(command.task_key, 0) + 1
    return ControlledRunState.model_validate(data)


def record_collaboration(state: ControlledRunState, record: CollaborationRecord) -> ControlledRunState:
    data = state.model_dump()
    data["collaborations"].append(record.model_dump())
    data["audit_log"].append({"collaboration": record.model_dump(), "visible_to": "supervisor"})
    return ControlledRunState.model_validate(data)

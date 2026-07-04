from datetime import datetime
from typing import Any
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field


class HealthResponse(BaseModel):
    status: str = "ok"


class Project(BaseModel):
    id: UUID
    slug: str
    name: str
    description: str | None = None
    workflow_key: str
    configuration: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    model_config = ConfigDict(from_attributes=True)


class ConversationCreate(BaseModel):
    title: str | None = None


class Conversation(BaseModel):
    id: UUID
    project_id: UUID
    title: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class RunCreate(BaseModel):
    content: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Run(BaseModel):
    id: UUID
    conversation_id: UUID
    status: str
    attempt: int = 1
    started_at: datetime | None = None
    finished_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    cancellation_requested_at: datetime | None = None
    cancellation_reason: str | None = None
    input: dict[str, Any] | None = None
    output: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class RunEvent(BaseModel):
    id: UUID
    run_id: UUID
    event_type: str
    message: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    agent: str | None = None
    phase: str | None = None
    progress: dict[str, Any] | None = None
    created_at: datetime | None = None


class RunCreated(BaseModel):
    run_id: UUID
    status: str


class RunCancelRequest(BaseModel):
    reason: str | None = None


class RunCancelResponse(BaseModel):
    run_id: UUID
    status: str


class RunCheckpoint(BaseModel):
    id: UUID
    run_id: UUID
    engine_version: str
    workflow_key: str
    phase: str
    completed_tasks: list[str] = Field(default_factory=list)
    artifacts: dict[str, Any] = Field(default_factory=dict)
    failures: list[dict[str, Any]] = Field(default_factory=list)
    token_usage: dict[str, int] = Field(default_factory=dict)
    last_event: dict[str, Any] | None = None
    attempt: int = 1
    created_at: datetime | None = None


class ProposalCreate(BaseModel):
    user_request: str = Field(min_length=1)
    budget_preference: str | None = None
    force_missing_verifier: bool = False
    force_bad_internet: bool = False


class ProposalRevise(BaseModel):
    user_request: str = Field(min_length=1)
    budget_preference: str | None = None


class ProposalDecision(BaseModel):
    reason: str | None = None


class WorkflowProposal(BaseModel):
    id: UUID
    status: str
    user_request: str
    task_spec: dict[str, Any]
    draft: dict[str, Any]
    critiques: list[dict[str, Any]] = Field(default_factory=list)
    estimates: dict[str, Any]
    repair_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None
    approved_at: datetime | None = None
    rejected_at: datetime | None = None


class ProposalProjectCreate(BaseModel):
    slug: str
    name: str
    description: str | None = None


class ProposalRunCreate(BaseModel):
    conversation_id: UUID
    content: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolAccessRequestCreate(BaseModel):
    agent: str
    tool: str
    reason: str
    scope: dict[str, Any] = Field(default_factory=dict)
    requested_limits: dict[str, Any] = Field(default_factory=dict)
    trigger: dict[str, Any] | None = None

class ToolGrantCreate(BaseModel):
    request_id: UUID | None = None
    agent: str
    tool: str
    max_searches: int
    max_rounds: int
    domains: list[str] | None = None
    expires_at: datetime
    approver_policy: str

class ToolUsageCreate(BaseModel):
    grant_id: UUID
    agent: str
    tool: str
    operation: str
    query: str | None = None
    url: str | None = None
    status: str = "succeeded"
    error: dict[str, Any] | None = None

class SourceCreate(BaseModel):
    agent: str
    url: str
    title: str
    domain: str
    source_type: str
    source_strength: str
    source_date: str | None = None
    query: str
    tool_operation: str

class ClaimCreate(BaseModel):
    entity_key: str
    field_key: str
    value: Any
    unit: str | None = None
    time_scope: dict[str, Any] = Field(default_factory=dict)
    geography: str | None = None
    market: str | None = None
    source_id: UUID
    source_strength: str
    confidence: float
    agent: str
    status: str = "active"

class ConflictCreate(BaseModel):
    entity_key: str
    field_key: str
    claim_ids: list[UUID]
    outcome: str = "unresolved_needs_review"
    rationale: str | None = None

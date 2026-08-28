from datetime import datetime
from typing import Any
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, field_validator

DEFAULT_CONVERSATION_TITLE = "New conversation"


def normalize_conversation_title(title: str | None) -> str:
    """Non-null conversation title for the NOT NULL conversations.title column.

    Missing/None/blank/whitespace-only titles become the safe default;
    explicit titles are preserved (surrounding whitespace stripped).
    """
    normalized = (title or "").strip()
    return normalized or DEFAULT_CONVERSATION_TITLE


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
    # The request may omit the title entirely; the contract guarantees a
    # non-null value so inserts can never violate conversations.title NOT NULL.
    title: str = DEFAULT_CONVERSATION_TITLE

    @field_validator("title", mode="before")
    @classmethod
    def _non_null_title(cls, value: Any) -> str:
        if value is not None and not isinstance(value, str):
            raise ValueError("title must be a string")
        return normalize_conversation_title(value)


class Conversation(BaseModel):
    id: UUID
    project_id: UUID
    title: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class RunCreate(BaseModel):
    content: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=8, max_length=128)


class RunUsage(BaseModel):
    """Browser-safe aggregate usage for one run.

    This mirrors ``BudgetTracker.snapshot()`` (backend/budget.py) field for
    field, which is the ONLY shape ever written to ``runs.usage``: the worker
    persists it through ``update_run_usage`` after every settled provider call
    and through ``transition_run`` on a budget terminal.

    The contract is deliberately a closed set of non-negative numbers. Extra
    keys are ignored rather than forwarded, so nothing outside this allowlist
    can reach the browser through it -- no provider or model identity, prompt,
    model response, evidence text, reservation, lease, ledger row or error
    detail, none of which the snapshot carries in the first place.

    Every field is optional. A run that has not settled a call stores ``{}``
    (migration 010's ``NOT NULL DEFAULT``), and absent is never rewritten as
    zero: "nothing recorded yet" and "zero model calls" are different facts.
    """

    # `model_` is a pydantic protected namespace; `model_calls` is the durable
    # column name and is not renamed for the wire.
    model_config = ConfigDict(extra="ignore", protected_namespaces=())

    model_calls: int | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    estimated_cost: float | None = Field(default=None, ge=0)
    actual_cost: float | None = Field(default=None, ge=0)
    retries: int | None = Field(default=None, ge=0)
    provider_backpressure_events: int | None = Field(default=None, ge=0)
    # Backend-counted model-backed steps; NOT a count of UI agents.
    agent_steps: int | None = Field(default=None, ge=0)
    elapsed_seconds: float | None = Field(default=None, ge=0)


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
    launch_state: str | None = None
    launch_error_class: str | None = None
    launch_reconciliation_required: bool = False
    # Authoritative aggregate usage for this run. `null` means nothing has been
    # recorded yet; it is never a synonym for zero spend.
    usage: RunUsage | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class RunEvent(BaseModel):
    id: int  # production run_events.id is bigint; run_id (below) remains UUID
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
    project_id: UUID
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
    created_by: UUID | None = None
    project_id: UUID | None = None
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
    idempotency_key: str = Field(min_length=8, max_length=128)


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


class WorkerRunEventCreate(BaseModel):
    event_type: str = Field(min_length=1)
    message: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    agent: str | None = None
    phase: str | None = None
    progress: dict[str, Any] | None = None


class WorkerRunCompleteRequest(BaseModel):
    output: dict[str, Any] = Field(default_factory=dict)


class WorkerRunFailRequest(BaseModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)

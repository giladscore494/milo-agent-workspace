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

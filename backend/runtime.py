from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID, uuid4

RUN_STATES = {
    "queued", "starting", "running", "waiting", "completed", "partial_success", "failed",
    "cancellation_requested", "cancelled",
}
TERMINAL_STATES = {"completed", "partial_success", "failed", "cancelled"}
VALID_TRANSITIONS: dict[str, set[str]] = {
    "queued": {"starting", "cancellation_requested", "failed"},
    "starting": {"running", "waiting", "cancellation_requested", "failed"},
    "running": {"waiting", "completed", "partial_success", "failed", "cancellation_requested"},
    "waiting": {"running", "failed", "cancellation_requested"},
    "cancellation_requested": {"cancelled", "failed"},
    "completed": set(),
    "partial_success": set(),
    "failed": set(),
    "cancelled": set(),
}
EVENT_TYPES = {
    "run_created", "run_started", "run_resumed", "phase_started", "phase_completed",
    "agent_created", "agent_started", "agent_progress", "agent_completed", "agent_failed",
    "chunk_started", "chunk_completed", "chunk_failed", "fallback_started", "fallback_completed",
    "checkpoint_saved", "cancellation_requested", "run_completed", "run_partial_success",
    "run_failed", "run_cancelled",
    "tool_access_requested", "tool_access_granted", "tool_access_denied", "tool_used",
    "source_recorded", "claim_recorded", "conflict_detected",
}

class InvalidTransition(ValueError):
    pass

class CancellationRequested(Exception):
    pass


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def validate_transition(current: str, new: str) -> None:
    if current not in RUN_STATES or new not in RUN_STATES:
        raise InvalidTransition(f"unknown run state transition {current!r} -> {new!r}")
    if new not in VALID_TRANSITIONS[current]:
        raise InvalidTransition(f"invalid run state transition {current!r} -> {new!r}")

@dataclass(frozen=True)
class RunEventRecord:
    run_id: UUID
    type: str
    message: str
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=now_iso)
    id: UUID = field(default_factory=uuid4)
    agent: str | None = None
    phase: str | None = None
    progress: dict[str, Any] | None = None

    def as_payload(self) -> dict[str, Any]:
        data = {
            "run_id": str(self.run_id),
            "timestamp": self.timestamp,
            "type": self.type,
            "message": self.message,
            "payload": self.payload,
        }
        if self.agent is not None:
            data["agent"] = self.agent
        if self.phase is not None:
            data["phase"] = self.phase
        if self.progress is not None:
            data["progress"] = self.progress
        return data

class EventSink(Protocol):
    def emit(self, event: RunEventRecord) -> RunEventRecord: ...

class InMemoryEventSink:
    def __init__(self) -> None:
        self.events: list[RunEventRecord] = []
    def emit(self, event: RunEventRecord) -> RunEventRecord:
        if event.type not in EVENT_TYPES:
            raise ValueError(f"unknown event type {event.type}")
        self.events.append(event)
        return event

class SupabaseEventSink:
    def __init__(self, repo: Any) -> None:
        self.repo = repo
    def emit(self, event: RunEventRecord) -> RunEventRecord:
        self.repo.append_run_event(event.run_id, event.type, event.as_payload())
        return event

@dataclass
class Checkpoint:
    run_id: UUID
    engine_version: str
    workflow_key: str
    phase: str
    completed_tasks: list[str]
    artifacts: dict[str, Any]
    failures: list[dict[str, Any]]
    token_usage: dict[str, int]
    last_event: dict[str, Any] | None = None
    attempt: int = 1

    def to_record(self) -> dict[str, Any]:
        return {
            "run_id": str(self.run_id),
            "engine_version": self.engine_version,
            "workflow_key": self.workflow_key,
            "phase": self.phase,
            "completed_tasks": self.completed_tasks,
            "artifacts": self.artifacts,
            "failures": self.failures,
            "token_usage": self.token_usage,
            "last_event": self.last_event,
            "attempt": self.attempt,
        }

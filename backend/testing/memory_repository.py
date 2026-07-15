"""In-memory Repository implementation for isolated E2E stacks.

Test-only: mirrors the SupabaseRepository authorization semantics
(membership scoping, 404 without existence disclosure, idempotency,
launch state) against process-local dictionaries. Never used by
production entrypoints.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from backend.errors import AppError, NotFoundError
from backend.runtime import RUN_STATES, InvalidTransition, validate_transition


def _now() -> str:
    return datetime.now(UTC).isoformat()


class MemoryRepository:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.users: set[str] = set()
        self.projects: dict[str, dict[str, Any]] = {}
        self.members: set[tuple[str, str]] = set()  # (project_id, user_id)
        self.conversations: dict[str, dict[str, Any]] = {}
        self.messages: list[dict[str, Any]] = []
        self.runs: dict[str, dict[str, Any]] = {}
        self.run_events: list[dict[str, Any]] = []
        self.proposals: dict[str, dict[str, Any]] = {}
        self.invocations: list[dict[str, Any]] = []
        self.tool_rows: list[dict[str, Any]] = []
        self.checkpoints: list[dict[str, Any]] = []

    # -- seeding -------------------------------------------------------------
    def seed_user(self, user_id: str) -> None:
        self.users.add(user_id)

    def seed_project(self, project_id: str, slug: str, name: str, members: list[str]) -> None:
        self.projects[project_id] = {
            "id": project_id, "slug": slug, "name": name, "description": None,
            "workflow_key": "vehicle_catalog_v1", "configuration": {},
            "created_at": _now(), "updated_at": _now(),
        }
        for user_id in members:
            self.members.add((project_id, user_id))

    # -- helpers ---------------------------------------------------------------
    def _is_member(self, project_id: str, user_id: UUID | str | None) -> bool:
        if user_id is None:
            return True  # trusted service path
        return (str(project_id), str(user_id)) in self.members

    def _conversation_project(self, conversation_id: UUID | str) -> str:
        conversation = self.conversations.get(str(conversation_id))
        if conversation is None:
            raise NotFoundError("conversation", str(conversation_id))
        return conversation["project_id"]

    # -- projects / conversations ----------------------------------------------
    def list_projects(self, user_id: UUID | None = None) -> list[dict[str, Any]]:
        return [dict(p) for p in self.projects.values() if self._is_member(p["id"], user_id)]

    def get_project(self, project_id: UUID, user_id: UUID | None = None) -> dict[str, Any]:
        project = self.projects.get(str(project_id))
        if project is None or not self._is_member(str(project_id), user_id):
            raise NotFoundError("project", str(project_id))
        return dict(project)

    def create_conversation(self, project_id: UUID, title: str | None, user_id: UUID | None = None) -> dict[str, Any]:
        self.get_project(project_id, user_id)
        conversation = {"id": str(uuid4()), "project_id": str(project_id), "title": title, "created_at": _now(), "updated_at": _now()}
        self.conversations[conversation["id"]] = conversation
        return dict(conversation)

    def list_conversations(self, project_id: UUID) -> list[dict[str, Any]]:
        return [dict(c) for c in self.conversations.values() if c["project_id"] == str(project_id)]

    def get_conversation(self, conversation_id: UUID, user_id: UUID | None = None) -> dict[str, Any]:
        conversation = self.conversations.get(str(conversation_id))
        if conversation is None or not self._is_member(conversation["project_id"], user_id):
            raise NotFoundError("conversation", str(conversation_id))
        return dict(conversation)

    # -- messages / runs ---------------------------------------------------------
    def create_user_message(self, conversation_id: UUID, content: str, metadata: dict[str, Any]) -> dict[str, Any]:
        message = {"id": len(self.messages) + 1, "conversation_id": str(conversation_id), "role": "user", "content": content, "metadata": metadata}
        self.messages.append(message)
        return dict(message)

    def create_queued_run(self, conversation_id: UUID, user_message_id: Any, content: str, metadata: dict[str, Any], requested_by: UUID | None = None, idempotency_key: str | None = None, request_fingerprint: str | None = None) -> dict[str, Any]:
        with self.lock:
            if requested_by is not None and idempotency_key:
                existing = self.find_run_by_idempotency(conversation_id, requested_by, idempotency_key)
                if existing is not None:
                    return existing
            run = {
                "id": str(uuid4()), "conversation_id": str(conversation_id), "status": "queued",
                "attempt": 1, "launch_state": "pending",
                "requested_by": str(requested_by) if requested_by else None,
                "idempotency_key": idempotency_key, "request_fingerprint": request_fingerprint,
                "input": {"message_id": str(user_message_id), "content": content, "metadata": metadata},
                "output": None, "error": None, "usage": {},
                "created_at": _now(), "updated_at": _now(),
            }
            self.runs[run["id"]] = run
            return dict(run)

    def find_run_by_idempotency(self, conversation_id: UUID, user_id: UUID, idempotency_key: str) -> dict[str, Any] | None:
        for run in self.runs.values():
            if run["conversation_id"] == str(conversation_id) and run.get("requested_by") == str(user_id) and run.get("idempotency_key") == idempotency_key:
                return dict(run)
        return None

    def create_message_and_run(self, conversation_id: UUID, content: str, metadata: dict[str, Any], requested_by: UUID, idempotency_key: str | None, request_fingerprint: str, max_user_active: int | None = None, max_project_active: int | None = None) -> dict[str, Any]:
        # Mirrors migration 012: replay lookup, admission and both writes
        # under one lock, so the E2E stack exercises the atomic contract.
        with self.lock:
            if idempotency_key:
                existing = self.find_run_by_idempotency(conversation_id, requested_by, idempotency_key)
                if existing is not None:
                    return {"run": existing, "created": False}
            if max_user_active is not None and self.count_active_runs_for_user(requested_by) >= max_user_active:
                raise AppError("USER_CONCURRENCY_LIMIT", "too many active runs for this user", 429)
            project_id = self._conversation_project(conversation_id)
            if max_project_active is not None and self.count_active_runs_for_project(project_id) >= max_project_active:
                raise AppError("PROJECT_CONCURRENCY_LIMIT", "too many active runs for this project", 429)
            message = self.create_user_message(conversation_id, content, metadata)
            run = self.create_queued_run(conversation_id, message["id"], content, metadata, requested_by=requested_by, idempotency_key=idempotency_key, request_fingerprint=request_fingerprint)
            return {"run": run, "created": True}

    def try_acquire_launch(self, run_id: UUID) -> dict[str, Any] | None:
        with self.lock:
            run = self.runs.get(str(run_id))
            if run is None or run["status"] != "queued" or run.get("launch_state") not in {"pending", "launch_failed"}:
                return None
            run["launch_state"] = "launching"
            return dict(run)

    def set_launch_state(self, run_id: UUID, state: str, error: dict[str, Any] | None = None) -> dict[str, Any]:
        run = self.runs[str(run_id)]
        run["launch_state"] = state
        if state == "launched":
            run["launched_at"] = _now()
        if error is not None:
            run["launch_error"] = error
        return dict(run)

    def get_run(self, run_id: UUID, user_id: UUID | None = None) -> dict[str, Any]:
        run = self.runs.get(str(run_id))
        if run is None:
            raise NotFoundError("run", str(run_id))
        project_id = self._conversation_project(run["conversation_id"])
        if not self._is_member(project_id, user_id):
            raise NotFoundError("run", str(run_id))
        return dict(run)

    def list_run_events(self, run_id: UUID, user_id: UUID | None = None, after_event_id: int | None = None) -> list[dict[str, Any]]:
        self.get_run(run_id, user_id)
        events = [e for e in self.run_events if e["run_id"] == str(run_id)]
        if after_event_id is not None:
            events = [e for e in events if e["id"] > after_event_id]
        return [dict(e) for e in events]

    def append_run_event(self, run_id: UUID, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            event = {
                "id": len(self.run_events) + 1,
                "run_id": str(run_id),
                "event_type": event_type,
                "payload": payload.get("payload", payload) or {},
                "message": payload.get("message"),
                "agent": payload.get("agent"),
                "phase": payload.get("phase"),
                "progress": payload.get("progress"),
                "created_at": _now(),
            }
            self.run_events.append(event)
            return dict(event)

    def transition_run(self, run_id: UUID, status: str, expected_worker_id: str | None = None, **fields: Any) -> dict[str, Any]:
        with self.lock:
            run = self.runs.get(str(run_id))
            if run is None:
                raise NotFoundError("run", str(run_id))
            if expected_worker_id is not None and run.get("worker_id") != expected_worker_id:
                raise AppError("RUN_TRANSITION_CONFLICT", "run lease is no longer held", 409)
            current = run["status"]
            if status != current and current in RUN_STATES:
                try:
                    validate_transition(current, status)
                except InvalidTransition as exc:
                    raise AppError("INVALID_RUN_TRANSITION", str(exc), 409) from exc
            run.update({"status": status, "updated_at": _now(), **fields})
            return dict(run)

    def request_cancellation(self, run_id: UUID, reason: str | None = None) -> dict[str, Any]:
        return self.transition_run(run_id, "cancellation_requested", cancellation_requested_at=_now(), cancellation_reason=reason)

    def mark_run_failed(self, run_id: UUID, code: str, message: str, worker_id: str | None = None) -> dict[str, Any]:
        return self.transition_run(run_id, "failed", expected_worker_id=worker_id, error={"code": code, "message": message}, finished_at=_now())

    def mark_run_complete(self, run_id: UUID, output: dict[str, Any], worker_id: str | None = None) -> dict[str, Any]:
        return self.transition_run(run_id, "completed", expected_worker_id=worker_id, output=output, error=None, finished_at=_now())

    def record_run_invocation(self, run_id: UUID, invocation: dict[str, Any]) -> dict[str, Any]:
        row = {"id": str(uuid4()), "run_id": str(run_id), **invocation}
        self.invocations.append(row)
        return dict(row)

    def update_run_usage(self, run_id: UUID, usage: dict[str, Any]) -> dict[str, Any]:
        run = self.runs[str(run_id)]
        run["usage"] = usage
        return dict(run)

    def append_usage_ledger(self, entry: dict[str, Any]) -> dict[str, Any]:
        row = {"id": len(getattr(self, "usage_ledger", [])) + 1, "created_at": _now(), **entry}
        if not hasattr(self, "usage_ledger"):
            self.usage_ledger = []
        self.usage_ledger.append(row)
        return dict(row)

    def sum_daily_ledger_cost(self, user_id: str | None = None, project_id: str | None = None, run_id: str | None = None, hours: int = 24) -> float:
        rows = getattr(self, "usage_ledger", [])
        per_call: dict[tuple, float] = {}
        for row in rows:
            if user_id and str(row.get("user_id")) != str(user_id):
                continue
            if project_id and str(row.get("project_id")) != str(project_id):
                continue
            if run_id and str(row.get("run_id")) != str(run_id):
                continue
            key = (str(row.get("run_id")), row.get("call_seq"))
            if row.get("decision") == "settled" and row.get("actual_cost") is not None:
                per_call[key] = float(row["actual_cost"])
            elif row.get("decision") == "reserved":
                per_call.setdefault(key, float(row.get("estimated_cost") or 0.0))
        return round(sum(per_call.values()), 6)

    def count_active_runs_for_user(self, user_id: UUID) -> int:
        active = {"queued", "launching", "starting", "running", "waiting", "cancellation_requested"}
        return sum(1 for run in self.runs.values() if run.get("requested_by") == str(user_id) and run["status"] in active)

    def count_active_runs_for_project(self, project_id: UUID) -> int:
        active = {"queued", "launching", "starting", "running", "waiting", "cancellation_requested"}
        return sum(1 for run in self.runs.values() if self._conversation_project(run["conversation_id"]) == str(project_id) and run["status"] in active)

    # -- proposals -----------------------------------------------------------------
    def create_workflow_proposal(self, user_request: str, proposal: dict[str, Any], project_id: UUID | None = None, created_by: UUID | None = None) -> dict[str, Any]:
        row = {
            "id": str(uuid4()), "user_request": user_request,
            "project_id": str(project_id) if project_id else None,
            "created_by": str(created_by) if created_by else None,
            "created_at": _now(), "updated_at": _now(),
            **proposal,
        }
        self.proposals[row["id"]] = row
        return dict(row)

    def get_workflow_proposal(self, proposal_id: UUID, user_id: UUID | None = None) -> dict[str, Any]:
        proposal = self.proposals.get(str(proposal_id))
        if proposal is None:
            raise NotFoundError("workflow_proposal", str(proposal_id))
        if user_id is not None:
            if not proposal.get("project_id") or not self._is_member(proposal["project_id"], user_id):
                raise NotFoundError("workflow_proposal", str(proposal_id))
        return dict(proposal)

    def update_workflow_proposal(self, proposal_id: UUID, fields: dict[str, Any]) -> dict[str, Any]:
        proposal = self.proposals.get(str(proposal_id))
        if proposal is None:
            raise NotFoundError("workflow_proposal", str(proposal_id))
        proposal.update(fields)
        proposal["updated_at"] = _now()
        return dict(proposal)

    def create_project_from_proposal(self, proposal_id: UUID, slug: str, name: str, description: str | None, configuration: dict[str, Any], created_by: UUID | None = None) -> dict[str, Any]:
        project_id = str(uuid4())
        self.projects[project_id] = {"id": project_id, "slug": slug, "name": name, "description": description, "workflow_key": "chat_architect_v1", "configuration": configuration, "created_at": _now(), "updated_at": _now()}
        if created_by is not None:
            self.members.add((project_id, str(created_by)))
        return dict(self.projects[project_id])

    # -- worker-side extras ----------------------------------------------------------
    def claim_run(self, run_id: UUID, worker_id: str, lease_seconds: int = 300) -> dict[str, Any]:
        run = self.get_run(run_id)
        target = run["status"] if run["status"] == "cancellation_requested" else "starting"
        return self.transition_run(run_id, target, worker_id=worker_id, started_at=run.get("started_at") or _now())

    def heartbeat(self, run_id: UUID, worker_id: str, lease_seconds: int = 300) -> dict[str, Any]:
        return self.get_run(run_id)

    def latest_checkpoint(self, run_id: UUID, workflow_key: str | None = None) -> dict[str, Any] | None:
        return None

    def save_checkpoint(self, checkpoint: dict[str, Any]) -> dict[str, Any]:
        self.checkpoints.append(checkpoint)
        return dict(checkpoint)

    def _tool_row(self, run_id: UUID, payload: dict[str, Any]) -> dict[str, Any]:
        row = {"id": str(uuid4()), "run_id": str(run_id), **payload}
        self.tool_rows.append(row)
        return dict(row)

    def create_tool_access_request(self, run_id: UUID, request: dict[str, Any]) -> dict[str, Any]:
        return self._tool_row(run_id, request)

    def create_tool_grant(self, run_id: UUID, grant: dict[str, Any]) -> dict[str, Any]:
        return self._tool_row(run_id, grant)

    def create_tool_usage(self, run_id: UUID, usage: dict[str, Any]) -> dict[str, Any]:
        return self._tool_row(run_id, usage)

    def create_source(self, run_id: UUID, source: dict[str, Any]) -> dict[str, Any]:
        return self._tool_row(run_id, source)

    def create_claim(self, run_id: UUID, claim: dict[str, Any]) -> dict[str, Any]:
        return self._tool_row(run_id, claim)

    def create_conflict(self, run_id: UUID, conflict: dict[str, Any]) -> dict[str, Any]:
        return self._tool_row(run_id, conflict)

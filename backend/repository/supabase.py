from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID
from supabase import create_client
from backend.config import Settings
from backend.errors import AppError, NotFoundError


class Repository(Protocol):
    def list_projects(self) -> list[dict[str, Any]]: ...
    def get_project(self, project_id: UUID) -> dict[str, Any]: ...
    def create_conversation(self, project_id: UUID, title: str | None) -> dict[str, Any]: ...
    def get_conversation(self, conversation_id: UUID) -> dict[str, Any]: ...
    def create_user_message(self, conversation_id: UUID, content: str, metadata: dict[str, Any]) -> dict[str, Any]: ...
    def create_queued_run(self, conversation_id: UUID, user_message_id: UUID, content: str, metadata: dict[str, Any]) -> dict[str, Any]: ...
    def get_run(self, run_id: UUID) -> dict[str, Any]: ...
    def list_run_events(self, run_id: UUID) -> list[dict[str, Any]]: ...
    def append_run_event(self, run_id: UUID, event_type: str, payload: dict[str, Any]) -> dict[str, Any]: ...
    def save_checkpoint(self, checkpoint: dict[str, Any]) -> dict[str, Any]: ...
    def latest_checkpoint(self, run_id: UUID, workflow_key: str | None = None) -> dict[str, Any] | None: ...
    def transition_run(self, run_id: UUID, status: str, **fields: Any) -> dict[str, Any]: ...
    def claim_run(self, run_id: UUID, worker_id: str, lease_seconds: int = 300) -> dict[str, Any]: ...
    def heartbeat(self, run_id: UUID, worker_id: str, lease_seconds: int = 300) -> dict[str, Any]: ...
    def request_cancellation(self, run_id: UUID, reason: str | None = None) -> dict[str, Any]: ...
    def mark_run_failed(self, run_id: UUID, code: str, message: str) -> dict[str, Any]: ...
    def mark_run_complete(self, run_id: UUID, output: dict[str, Any]) -> dict[str, Any]: ...


class SupabaseRepository:
    def __init__(self, settings: Settings):
        self.client = create_client(str(settings.supabase_url), settings.supabase_service_role_key)

    def _single(self, query: Any, resource: str, identifier: str) -> dict[str, Any]:
        try:
            data = query.execute().data
        except Exception as exc:  # Supabase boundary only
            raise AppError("REPOSITORY_ERROR", str(exc), 502) from exc
        if not data:
            raise NotFoundError(resource, identifier)
        return data[0] if isinstance(data, list) else data

    def _many(self, query: Any) -> list[dict[str, Any]]:
        try:
            return query.execute().data or []
        except Exception as exc:
            raise AppError("REPOSITORY_ERROR", str(exc), 502) from exc

    def list_projects(self) -> list[dict[str, Any]]:
        return self._many(self.client.table("projects").select("*").order("created_at"))

    def get_project(self, project_id: UUID) -> dict[str, Any]:
        return self._single(self.client.table("projects").select("*").eq("id", str(project_id)).limit(1), "project", str(project_id))

    def create_conversation(self, project_id: UUID, title: str | None) -> dict[str, Any]:
        self.get_project(project_id)
        return self._single(self.client.table("conversations").insert({"project_id": str(project_id), "title": title}).select("*"), "conversation", "new")

    def get_conversation(self, conversation_id: UUID) -> dict[str, Any]:
        return self._single(self.client.table("conversations").select("*").eq("id", str(conversation_id)).limit(1), "conversation", str(conversation_id))

    def create_user_message(self, conversation_id: UUID, content: str, metadata: dict[str, Any]) -> dict[str, Any]:
        self.get_conversation(conversation_id)
        payload = {"conversation_id": str(conversation_id), "role": "user", "content": content, "metadata": metadata}
        return self._single(self.client.table("messages").insert(payload).select("*"), "message", "new")

    def create_queued_run(self, conversation_id: UUID, user_message_id: UUID, content: str, metadata: dict[str, Any]) -> dict[str, Any]:
        payload = {"conversation_id": str(conversation_id), "status": "queued", "input": {"message_id": str(user_message_id), "content": content, "metadata": metadata}}
        return self._single(self.client.table("runs").insert(payload).select("*"), "run", "new")

    def get_run(self, run_id: UUID) -> dict[str, Any]:
        return self._single(self.client.table("runs").select("*").eq("id", str(run_id)).limit(1), "run", str(run_id))

    def list_run_events(self, run_id: UUID) -> list[dict[str, Any]]:
        self.get_run(run_id)
        return self._many(self.client.table("run_events").select("*").eq("run_id", str(run_id)).order("created_at"))

    def append_run_event(self, run_id: UUID, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        row = {
            "run_id": str(run_id),
            "event_type": event_type,
            "payload": payload.get("payload", payload),
            "message": payload.get("message"),
            "agent": payload.get("agent"),
            "phase": payload.get("phase"),
            "progress": payload.get("progress"),
        }
        return self._single(self.client.table("run_events").insert(row).select("*"), "run_event", "new")

    def save_checkpoint(self, checkpoint: dict[str, Any]) -> dict[str, Any]:
        return self._single(self.client.table("run_checkpoints").insert(checkpoint).select("*"), "run_checkpoint", "new")

    def latest_checkpoint(self, run_id: UUID, workflow_key: str | None = None) -> dict[str, Any] | None:
        query = self.client.table("run_checkpoints").select("*").eq("run_id", str(run_id)).order("created_at", desc=True).limit(1)
        if workflow_key:
            query = query.eq("workflow_key", workflow_key)
        rows = self._many(query)
        return rows[0] if rows else None

    def transition_run(self, run_id: UUID, status: str, **fields: Any) -> dict[str, Any]:
        payload = {"status": status, **fields}
        return self._single(self.client.table("runs").update(payload).eq("id", str(run_id)).select("*"), "run", str(run_id))

    def claim_run(self, run_id: UUID, worker_id: str, lease_seconds: int = 300) -> dict[str, Any]:
        run = self.get_run(run_id)
        now = datetime.now(UTC)
        lease_expired = not run.get("lease_expires_at") or datetime.fromisoformat(str(run["lease_expires_at"]).replace("Z", "+00:00")) < now
        if run["status"] not in {"queued", "waiting", "cancellation_requested"} and not lease_expired:
            raise AppError("RUN_ALREADY_CLAIMED", "run is already claimed by another worker", 409)
        attempt = int(run.get("attempt") or 1) + (1 if lease_expired and run.get("worker_id") else 0)
        expires = (now + timedelta(seconds=lease_seconds)).isoformat()
        return self.transition_run(run_id, "starting", worker_id=worker_id, lease_expires_at=expires, attempt=attempt, started_at=run.get("started_at") or now.isoformat())

    def heartbeat(self, run_id: UUID, worker_id: str, lease_seconds: int = 300) -> dict[str, Any]:
        run = self.get_run(run_id)
        if run.get("worker_id") != worker_id:
            raise AppError("RUN_LEASE_LOST", "run lease is held by another worker", 409)
        now = datetime.now(UTC)
        expires = (now + timedelta(seconds=lease_seconds)).isoformat()
        self.client.table("worker_heartbeats").insert({"run_id": str(run_id), "worker_id": worker_id, "attempt": run.get("attempt", 1), "heartbeat_at": now.isoformat(), "lease_expires_at": expires}).execute()
        return self.transition_run(run_id, run["status"], last_heartbeat_at=now.isoformat(), lease_expires_at=expires)

    def request_cancellation(self, run_id: UUID, reason: str | None = None) -> dict[str, Any]:
        return self.transition_run(run_id, "cancellation_requested", cancellation_requested_at=datetime.now(UTC).isoformat(), cancellation_reason=reason)

    def mark_run_failed(self, run_id: UUID, code: str, message: str) -> dict[str, Any]:
        payload = {"status": "failed", "error": {"code": code, "message": message}, "finished_at": datetime.now(UTC).isoformat()}
        return self._single(self.client.table("runs").update(payload).eq("id", str(run_id)).select("*"), "run", str(run_id))

    def mark_run_complete(self, run_id: UUID, output: dict[str, Any]) -> dict[str, Any]:
        payload = {"status": "completed", "output": output, "error": None, "finished_at": datetime.now(UTC).isoformat()}
        return self._single(self.client.table("runs").update(payload).eq("id", str(run_id)).select("*"), "run", str(run_id))

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
    def create_queued_run(self, conversation_id: UUID, user_message_id: int | str | UUID, content: str, metadata: dict[str, Any]) -> dict[str, Any]: ...
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
    def create_workflow_proposal(self, user_request: str, proposal: dict[str, Any]) -> dict[str, Any]: ...
    def get_workflow_proposal(self, proposal_id: UUID) -> dict[str, Any]: ...
    def update_workflow_proposal(self, proposal_id: UUID, fields: dict[str, Any]) -> dict[str, Any]: ...
    def create_project_from_proposal(self, proposal_id: UUID, slug: str, name: str, description: str | None, configuration: dict[str, Any]) -> dict[str, Any]: ...
    def create_tool_access_request(self, run_id: UUID, request: dict[str, Any]) -> dict[str, Any]: ...
    def create_tool_grant(self, run_id: UUID, grant: dict[str, Any]) -> dict[str, Any]: ...
    def create_tool_usage(self, run_id: UUID, usage: dict[str, Any]) -> dict[str, Any]: ...
    def create_source(self, run_id: UUID, source: dict[str, Any]) -> dict[str, Any]: ...
    def create_claim(self, run_id: UUID, claim: dict[str, Any]) -> dict[str, Any]: ...
    def create_conflict(self, run_id: UUID, conflict: dict[str, Any]) -> dict[str, Any]: ...
    def record_run_invocation(self, run_id: UUID, invocation: dict[str, Any]) -> dict[str, Any]: ...

    def upsert_run_blackboard(self, run_id: UUID, blackboard: dict[str, Any]) -> dict[str, Any]: ...
    def create_agent_message(self, message: dict[str, Any]) -> dict[str, Any]: ...
    def list_unread_agent_messages(self, run_id: UUID, recipient: str = "supervisor") -> list[dict[str, Any]]: ...
    def create_supervisor_decision(self, run_id: UUID, decision: dict[str, Any]) -> dict[str, Any]: ...
    def list_supervisor_decisions(self, run_id: UUID) -> list[dict[str, Any]]: ...


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

    def create_queued_run(self, conversation_id: UUID, user_message_id: int | str | UUID, content: str, metadata: dict[str, Any]) -> dict[str, Any]:
        # Production messages.id is bigint; run input stores its string form.
        idempotency_key = metadata.get("idempotency_key") or metadata.get("proposal_id") or str(user_message_id)
        payload = {"conversation_id": str(conversation_id), "status": "queued", "input": {"message_id": str(user_message_id), "content": content, "metadata": metadata}, "idempotency_key": idempotency_key}
        existing = self._many(self.client.table("runs").select("*").eq("conversation_id", str(conversation_id)).eq("idempotency_key", idempotency_key).in_("status", ["queued", "starting", "running", "waiting", "cancellation_requested"]).limit(1))
        if existing:
            return existing[0]
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

    def create_workflow_proposal(self, user_request: str, proposal: dict[str, Any]) -> dict[str, Any]:
        payload = {"user_request": user_request, **proposal}
        return self._single(self.client.table("workflow_proposals").insert(payload).select("*"), "workflow_proposal", "new")

    def get_workflow_proposal(self, proposal_id: UUID) -> dict[str, Any]:
        return self._single(self.client.table("workflow_proposals").select("*").eq("id", str(proposal_id)).limit(1), "workflow_proposal", str(proposal_id))

    def update_workflow_proposal(self, proposal_id: UUID, fields: dict[str, Any]) -> dict[str, Any]:
        fields = {**fields, "updated_at": datetime.now(UTC).isoformat()}
        return self._single(self.client.table("workflow_proposals").update(fields).eq("id", str(proposal_id)).select("*"), "workflow_proposal", str(proposal_id))

    def create_project_from_proposal(self, proposal_id: UUID, slug: str, name: str, description: str | None, configuration: dict[str, Any]) -> dict[str, Any]:
        payload = {"slug": slug, "name": name, "description": description, "workflow_key": "chat_architect_v1", "configuration": {**configuration, "proposal_id": str(proposal_id)}}
        return self._single(self.client.table("projects").insert(payload).select("*"), "project", "new")

    def upsert_run_blackboard(self, run_id: UUID, blackboard: dict[str, Any]) -> dict[str, Any]:
        payload = {"run_id": str(run_id), **blackboard, "updated_at": datetime.now(UTC).isoformat()}
        return self._single(self.client.table("run_blackboards").upsert(payload, on_conflict="run_id").select("*"), "run_blackboard", str(run_id))

    def create_agent_message(self, message: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "id": str(message.get("id")) if message.get("id") else None,
            "run_id": str(message["run_id"]),
            "message_type": message["type"],
            "sender": message["sender"],
            "recipient": message["recipient"],
            "task_key": message.get("task_key"),
            "payload": message.get("payload", {}),
            "read_at": message.get("read_at"),
        }
        payload = {k: v for k, v in payload.items() if v is not None}
        return self._single(self.client.table("agent_messages").insert(payload).select("*"), "agent_message", "new")

    def list_unread_agent_messages(self, run_id: UUID, recipient: str = "supervisor") -> list[dict[str, Any]]:
        return self._many(self.client.table("agent_messages").select("*").eq("run_id", str(run_id)).eq("recipient", recipient).is_("read_at", "null").order("created_at"))

    def create_supervisor_decision(self, run_id: UUID, decision: dict[str, Any]) -> dict[str, Any]:
        payload = {"run_id": str(run_id), "mode": "shadow", **decision}
        return self._single(self.client.table("supervisor_decisions").insert(payload).select("*"), "supervisor_decision", "new")

    def list_supervisor_decisions(self, run_id: UUID) -> list[dict[str, Any]]:
        return self._many(self.client.table("supervisor_decisions").select("*").eq("run_id", str(run_id)).order("created_at"))

    def create_tool_access_request(self, run_id: UUID, request: dict[str, Any]) -> dict[str, Any]:
        payload = {"run_id": str(run_id), **request}
        return self._single(self.client.table("tool_access_requests").insert(payload).select("*"), "tool_access_request", "new")

    def create_tool_grant(self, run_id: UUID, grant: dict[str, Any]) -> dict[str, Any]:
        payload = {"run_id": str(run_id), **grant}
        if payload.get("request_id"):
            self.client.table("tool_access_requests").update({"status": "granted"}).eq("id", str(payload["request_id"])).execute()
        return self._single(self.client.table("tool_grants").insert(payload).select("*"), "tool_grant", "new")

    def create_tool_usage(self, run_id: UUID, usage: dict[str, Any]) -> dict[str, Any]:
        payload = {"run_id": str(run_id), **usage}
        return self._single(self.client.table("tool_usage").insert(payload).select("*"), "tool_usage", "new")

    def create_source(self, run_id: UUID, source: dict[str, Any]) -> dict[str, Any]:
        payload = {"run_id": str(run_id), **source}
        return self._single(self.client.table("sources").insert(payload).select("*"), "source", "new")

    def create_claim(self, run_id: UUID, claim: dict[str, Any]) -> dict[str, Any]:
        payload = {"run_id": str(run_id), **claim}
        row = self._single(self.client.table("claims").insert(payload).select("*"), "claim", "new")
        self.client.table("source_claim_links").insert({"source_id": str(row["source_id"]), "claim_id": str(row["id"])}).execute()
        return row

    def create_conflict(self, run_id: UUID, conflict: dict[str, Any]) -> dict[str, Any]:
        payload = {"run_id": str(run_id), **conflict}
        return self._single(self.client.table("conflicts").insert(payload).select("*"), "conflict", "new")

    def record_run_invocation(self, run_id: UUID, invocation: dict[str, Any]) -> dict[str, Any]:
        payload = {"run_id": str(run_id), "launcher": invocation.get("mode"), "execution_name": invocation.get("execution"), "payload": invocation}
        return self._single(self.client.table("run_invocations").insert(payload).select("*"), "run_invocation", "new")

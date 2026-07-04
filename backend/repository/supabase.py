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
    def mark_run_failed(self, run_id: UUID, code: str, message: str) -> dict[str, Any]: ...


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
        return self._single(self.client.table("run_events").insert({"run_id": str(run_id), "event_type": event_type, "payload": payload}).select("*"), "run_event", "new")

    def mark_run_failed(self, run_id: UUID, code: str, message: str) -> dict[str, Any]:
        payload = {"status": "failed", "error": {"code": code, "message": message}}
        return self._single(self.client.table("runs").update(payload).eq("id", str(run_id)).select("*"), "run", str(run_id))

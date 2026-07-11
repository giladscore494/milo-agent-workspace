from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID
from supabase import create_client
from backend.config import Settings
from backend.errors import AppError, NotFoundError
from backend.runtime import RUN_STATES, InvalidTransition, validate_transition


class Repository(Protocol):
    def list_projects(self, user_id: UUID | None = None) -> list[dict[str, Any]]: ...
    def get_project(self, project_id: UUID, user_id: UUID | None = None) -> dict[str, Any]: ...
    def create_conversation(self, project_id: UUID, title: str | None, user_id: UUID | None = None) -> dict[str, Any]: ...
    def list_conversations(self, project_id: UUID) -> list[dict[str, Any]]: ...
    def get_conversation(self, conversation_id: UUID, user_id: UUID | None = None) -> dict[str, Any]: ...
    def create_user_message(self, conversation_id: UUID, content: str, metadata: dict[str, Any]) -> dict[str, Any]: ...
    def create_queued_run(self, conversation_id: UUID, user_message_id: int | str | UUID, content: str, metadata: dict[str, Any], requested_by: UUID | None = None, idempotency_key: str | None = None, request_fingerprint: str | None = None) -> dict[str, Any]: ...
    def create_message_and_run(self, conversation_id: UUID, content: str, metadata: dict[str, Any], requested_by: UUID, idempotency_key: str | None, request_fingerprint: str, max_user_active: int | None = None, max_project_active: int | None = None) -> dict[str, Any]: ...
    def find_run_by_idempotency(self, conversation_id: UUID, user_id: UUID, idempotency_key: str) -> dict[str, Any] | None: ...
    def set_launch_state(self, run_id: UUID, state: str, error: dict[str, Any] | None = None) -> dict[str, Any]: ...
    def try_acquire_launch(self, run_id: UUID) -> dict[str, Any] | None: ...
    def count_active_runs_for_user(self, user_id: UUID) -> int: ...
    def count_active_runs_for_project(self, project_id: UUID) -> int: ...
    def update_run_usage(self, run_id: UUID, usage: dict[str, Any]) -> dict[str, Any]: ...
    def get_run(self, run_id: UUID, user_id: UUID | None = None) -> dict[str, Any]: ...
    def list_run_events(self, run_id: UUID, user_id: UUID | None = None) -> list[dict[str, Any]]: ...
    def append_run_event(self, run_id: UUID, event_type: str, payload: dict[str, Any]) -> dict[str, Any]: ...
    def save_checkpoint(self, checkpoint: dict[str, Any]) -> dict[str, Any]: ...
    def latest_checkpoint(self, run_id: UUID, workflow_key: str | None = None) -> dict[str, Any] | None: ...
    def transition_run(self, run_id: UUID, status: str, **fields: Any) -> dict[str, Any]: ...
    def claim_run(self, run_id: UUID, worker_id: str, lease_seconds: int = 300) -> dict[str, Any]: ...
    def heartbeat(self, run_id: UUID, worker_id: str, lease_seconds: int = 300) -> dict[str, Any]: ...
    def request_cancellation(self, run_id: UUID, reason: str | None = None) -> dict[str, Any]: ...
    def mark_run_failed(self, run_id: UUID, code: str, message: str) -> dict[str, Any]: ...
    def mark_run_complete(self, run_id: UUID, output: dict[str, Any]) -> dict[str, Any]: ...
    def create_workflow_proposal(self, user_request: str, proposal: dict[str, Any], project_id: UUID | None = None, created_by: UUID | None = None) -> dict[str, Any]: ...
    def get_workflow_proposal(self, proposal_id: UUID, user_id: UUID | None = None) -> dict[str, Any]: ...
    def update_workflow_proposal(self, proposal_id: UUID, fields: dict[str, Any]) -> dict[str, Any]: ...
    def create_project_from_proposal(self, proposal_id: UUID, slug: str, name: str, description: str | None, configuration: dict[str, Any], created_by: UUID | None = None) -> dict[str, Any]: ...
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

    def list_projects(self, user_id: UUID | None = None) -> list[dict[str, Any]]:
        if user_id is None:
            return self._many(self.client.table("projects").select("*").order("created_at"))
        return self._many(self.client.table("projects").select("*, project_members!inner(user_id)").eq("project_members.user_id", str(user_id)).order("created_at"))

    def get_project(self, project_id: UUID, user_id: UUID | None = None) -> dict[str, Any]:
        query = self.client.table("projects").select("*").eq("id", str(project_id)).limit(1)
        if user_id is not None:
            query = self.client.table("projects").select("*, project_members!inner(user_id)").eq("id", str(project_id)).eq("project_members.user_id", str(user_id)).limit(1)
        return self._single(query, "project", str(project_id))

    def create_conversation(self, project_id: UUID, title: str | None, user_id: UUID | None = None) -> dict[str, Any]:
        self.get_project(project_id, user_id)
        return self._single(self.client.table("conversations").insert({"project_id": str(project_id), "title": title}).select("*"), "conversation", "new")

    def list_conversations(self, project_id: UUID) -> list[dict[str, Any]]:
        # Callers must have already verified project membership.
        return self._many(self.client.table("conversations").select("*").eq("project_id", str(project_id)).order("created_at", desc=True).limit(200))

    def get_conversation(self, conversation_id: UUID, user_id: UUID | None = None) -> dict[str, Any]:
        query = self.client.table("conversations").select("*").eq("id", str(conversation_id)).limit(1)
        if user_id is not None:
            query = self.client.table("conversations").select("*, projects!inner(project_members!inner(user_id))").eq("id", str(conversation_id)).eq("projects.project_members.user_id", str(user_id)).limit(1)
        return self._single(query, "conversation", str(conversation_id))

    def create_user_message(self, conversation_id: UUID, content: str, metadata: dict[str, Any]) -> dict[str, Any]:
        self.get_conversation(conversation_id)
        payload = {"conversation_id": str(conversation_id), "role": "user", "content": content, "metadata": metadata}
        return self._single(self.client.table("messages").insert(payload).select("*"), "message", "new")

    def create_queued_run(self, conversation_id: UUID, user_message_id: int | str | UUID, content: str, metadata: dict[str, Any], requested_by: UUID | None = None, idempotency_key: str | None = None, request_fingerprint: str | None = None) -> dict[str, Any]:
        # Production messages.id is bigint; run input stores its string form.
        key = idempotency_key or metadata.get("idempotency_key") or metadata.get("proposal_id") or str(user_message_id)
        payload = {
            "conversation_id": str(conversation_id),
            "status": "queued",
            "input": {"message_id": str(user_message_id), "content": content, "metadata": metadata},
            "idempotency_key": key,
            "launch_state": "pending",
        }
        if requested_by is not None:
            payload["requested_by"] = str(requested_by)
        if request_fingerprint is not None:
            payload["request_fingerprint"] = request_fingerprint
        if requested_by is not None and idempotency_key:
            existing = self.find_run_by_idempotency(conversation_id, requested_by, idempotency_key)
            if existing is not None:
                return existing
        try:
            return self._single(self.client.table("runs").insert(payload).select("*"), "run", "new")
        except AppError as exc:
            # Unique (conversation, requested_by, idempotency_key) index may
            # reject a concurrent duplicate; return the winner instead.
            if requested_by is not None and idempotency_key and ("23505" in exc.message or "duplicate" in exc.message.lower()):
                existing = self.find_run_by_idempotency(conversation_id, requested_by, idempotency_key)
                if existing is not None:
                    return existing
            raise

    def find_run_by_idempotency(self, conversation_id: UUID, user_id: UUID, idempotency_key: str) -> dict[str, Any] | None:
        rows = self._many(
            self.client.table("runs").select("*")
            .eq("conversation_id", str(conversation_id))
            .eq("requested_by", str(user_id))
            .eq("idempotency_key", idempotency_key)
            .limit(1)
        )
        return rows[0] if rows else None

    def create_message_and_run(self, conversation_id: UUID, content: str, metadata: dict[str, Any], requested_by: UUID, idempotency_key: str | None, request_fingerprint: str, max_user_active: int | None = None, max_project_active: int | None = None) -> dict[str, Any]:
        """Transactionally create the user message + queued run (migration
        012): idempotent replay, concurrency admission and both inserts
        happen in ONE database transaction under advisory locks."""
        try:
            response = self.client.rpc("create_message_and_run", {
                "p_conversation_id": str(conversation_id),
                "p_content": content,
                "p_metadata": metadata,
                "p_requested_by": str(requested_by),
                "p_idempotency_key": idempotency_key,
                "p_request_fingerprint": request_fingerprint,
                "p_max_user_active": max_user_active,
                "p_max_project_active": max_project_active,
            }).execute()
        except Exception as exc:
            message = str(exc)
            if "USER_CONCURRENCY_LIMIT" in message:
                raise AppError("USER_CONCURRENCY_LIMIT", "too many active runs for this user", 429) from exc
            if "PROJECT_CONCURRENCY_LIMIT" in message:
                raise AppError("PROJECT_CONCURRENCY_LIMIT", "too many active runs for this project", 429) from exc
            if "CONVERSATION_NOT_FOUND" in message:
                raise NotFoundError("conversation", str(conversation_id)) from exc
            raise AppError("REPOSITORY_ERROR", message, 502) from exc
        data = response.data
        if isinstance(data, list):
            data = data[0] if data else None
        if not data or "run" not in data:
            raise AppError("REPOSITORY_ERROR", "run creation returned no row", 502)
        return data

    def try_acquire_launch(self, run_id: UUID) -> dict[str, Any] | None:
        """Atomic compare-and-set on launch ownership: only one caller can
        move pending/launch_failed -> launching for a queued run."""
        try:
            rows = self.client.table("runs").update({"launch_state": "launching"}).eq("id", str(run_id)).eq("status", "queued").in_("launch_state", ["pending", "launch_failed"]).select("*").execute().data or []
        except Exception as exc:
            raise AppError("REPOSITORY_ERROR", str(exc), 502) from exc
        return rows[0] if rows else None

    def set_launch_state(self, run_id: UUID, state: str, error: dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"launch_state": state}
        if state == "launched":
            payload["launched_at"] = datetime.now(UTC).isoformat()
        if error is not None:
            payload["launch_error"] = error
        return self._single(self.client.table("runs").update(payload).eq("id", str(run_id)).select("*"), "run", str(run_id))

    ACTIVE_RUN_STATES = ("queued", "launching", "starting", "running", "waiting", "cancellation_requested")

    def count_active_runs_for_user(self, user_id: UUID) -> int:
        rows = self._many(self.client.table("runs").select("id").eq("requested_by", str(user_id)).in_("status", list(self.ACTIVE_RUN_STATES)).limit(1000))
        return len(rows)

    def count_active_runs_for_project(self, project_id: UUID) -> int:
        rows = self._many(self.client.table("runs").select("id, conversations!inner(project_id)").eq("conversations.project_id", str(project_id)).in_("status", list(self.ACTIVE_RUN_STATES)).limit(1000))
        return len(rows)

    def update_run_usage(self, run_id: UUID, usage: dict[str, Any]) -> dict[str, Any]:
        return self._single(self.client.table("runs").update({"usage": usage}).eq("id", str(run_id)).select("*"), "run", str(run_id))

    LEDGER_FIELDS = (
        "run_id", "project_id", "user_id", "provider", "model", "call_seq", "decision",
        "rejection_reason", "reserved_input_tokens", "reserved_output_tokens",
        "actual_input_tokens", "actual_output_tokens", "estimated_cost", "actual_cost",
    )

    def append_usage_ledger(self, entry: dict[str, Any]) -> dict[str, Any]:
        payload = {key: entry[key] for key in self.LEDGER_FIELDS if entry.get(key) is not None}
        return self._single(self.client.table("run_usage_ledger").insert(payload).select("*"), "run_usage_ledger", "new")

    def sum_daily_ledger_cost(self, user_id: str | None = None, project_id: str | None = None, run_id: str | None = None, hours: int = 24) -> float:
        """Conservative daily spend: per call, the settled actual cost when
        recorded, otherwise the reserved estimate."""
        since = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
        query = self.client.table("run_usage_ledger").select("run_id, call_seq, decision, estimated_cost, actual_cost").gte("created_at", since).limit(2000)
        if user_id:
            query = query.eq("user_id", str(user_id))
        if project_id:
            query = query.eq("project_id", str(project_id))
        if run_id:
            query = query.eq("run_id", str(run_id))
        rows = self._many(query)
        per_call: dict[tuple, float] = {}
        for row in rows:
            key = (str(row.get("run_id")), row.get("call_seq"))
            if row.get("decision") == "settled" and row.get("actual_cost") is not None:
                per_call[key] = float(row["actual_cost"])
            elif row.get("decision") == "reserved":
                per_call.setdefault(key, float(row.get("estimated_cost") or 0.0))
        return round(sum(per_call.values()), 6)

    def reserve_daily_user_budget(self, run_id: UUID, user_id: str, amount: float, daily_limit: float) -> dict[str, Any]:
        row = self.client.rpc("reserve_daily_user_budget", {
            "p_run_id": str(run_id), "p_user_id": str(user_id),
            "p_amount": amount, "p_daily_limit": daily_limit,
        }).execute().data
        row = row[0] if isinstance(row, list) else row
        if row and row.get("decision") == "rejected":
            raise AppError("DAILY_USER_BUDGET_REACHED", "daily user budget exhausted", 429)
        return row

    def reserve_daily_project_budget(self, run_id: UUID, project_id: str, amount: float, daily_limit: float) -> dict[str, Any]:
        row = self.client.rpc("reserve_daily_project_budget", {
            "p_run_id": str(run_id), "p_project_id": str(project_id),
            "p_amount": amount, "p_daily_limit": daily_limit,
        }).execute().data
        row = row[0] if isinstance(row, list) else row
        if row and row.get("decision") == "rejected":
            raise AppError("DAILY_PROJECT_BUDGET_REACHED", "daily project budget exhausted", 429)
        return row

    def reserve_model_call_budget(self, run_id: UUID, call_seq: int, user_id: str | None, project_id: str | None, amount: float, daily_user_limit: float | None, daily_project_limit: float | None) -> dict[str, Any]:
        row = self.client.rpc("reserve_model_call_budget", {
            "p_run_id": str(run_id),
            "p_call_seq": int(call_seq),
            "p_user_id": str(user_id) if user_id else None,
            "p_project_id": str(project_id) if project_id else None,
            "p_estimated_cost": amount,
            "p_daily_user_limit": daily_user_limit,
            "p_daily_project_limit": daily_project_limit,
        }).execute().data
        row = row[0] if isinstance(row, list) else row
        if row and row.get("status") == "rejected":
            reason = row.get("rejection_reason") or "DAILY_BUDGET_REACHED"
            raise AppError(reason, "daily budget exhausted", 429)
        return row

    def settle_model_call_budget(self, reservation_id: str, actual_cost: float, status: str = "settled", rejection_reason: str | None = None) -> dict[str, Any]:
        row = self.client.rpc("settle_model_call_budget", {
            "p_reservation_id": str(reservation_id),
            "p_actual_cost": actual_cost,
            "p_status": status,
            "p_rejection_reason": rejection_reason,
        }).execute().data
        return row[0] if isinstance(row, list) else row

    def get_run(self, run_id: UUID, user_id: UUID | None = None) -> dict[str, Any]:
        query = self.client.table("runs").select("*").eq("id", str(run_id)).limit(1)
        if user_id is not None:
            # Browser-facing reads must prove project membership through
            # runs -> conversations -> projects -> project_members.
            query = self.client.table("runs").select("*, conversations!inner(projects!inner(project_members!inner(user_id)))").eq("id", str(run_id)).eq("conversations.projects.project_members.user_id", str(user_id)).limit(1)
        return self._single(query, "run", str(run_id))

    def list_run_events(self, run_id: UUID, user_id: UUID | None = None, after_event_id: int | None = None) -> list[dict[str, Any]]:
        self.get_run(run_id, user_id)
        query = self.client.table("run_events").select("*").eq("run_id", str(run_id))
        if after_event_id is not None:
            query = query.gt("id", after_event_id)
        return self._many(query.order("id").limit(500))

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

    def transition_run(self, run_id: UUID, status: str, expected_worker_id: str | None = None, expected_attempt: int | None = None, lease_token: str | None = None, **fields: Any) -> dict[str, Any]:
        current = str(self.get_run(run_id).get("status", ""))
        # Same-status updates (heartbeats, metadata refresh) are no-op
        # transitions; unknown legacy statuses bypass validation so legacy
        # production rows can still be repaired through the service path.
        transitioning = status != current and current in RUN_STATES
        if transitioning:
            try:
                validate_transition(current, status)
            except InvalidTransition as exc:
                raise AppError("INVALID_RUN_TRANSITION", str(exc), 409) from exc
        payload = {"status": status, **fields}
        query = self.client.table("runs").update(payload).eq("id", str(run_id))
        if transitioning:
            # Compare-and-set on the observed status: a concurrent transition
            # (e.g. a newer worker writing a terminal result) makes this
            # update match zero rows instead of silently overwriting it.
            query = query.eq("status", current)
        if expected_worker_id is not None:
            # Stale workers whose lease was reclaimed cannot overwrite the
            # newer holder's state. Lease expiry is part of ownership.
            query = query.eq("worker_id", expected_worker_id)
            query = query.gt("lease_expires_at", datetime.now(UTC).isoformat())
        if expected_attempt is not None:
            query = query.eq("attempt", expected_attempt)
        if lease_token is not None:
            query = query.eq("lease_token", lease_token)
        try:
            rows = query.select("*").execute().data or []
        except Exception as exc:
            raise AppError("REPOSITORY_ERROR", str(exc), 502) from exc
        if not rows:
            raise AppError("RUN_TRANSITION_CONFLICT", "run was modified concurrently or the lease is no longer held", 409)
        return rows[0] if isinstance(rows, list) else rows

    def claim_run(self, run_id: UUID, worker_id: str, lease_seconds: int = 300) -> dict[str, Any]:
        """Atomic lease claim via migration 012's single-statement CAS."""
        try:
            response = self.client.rpc("claim_run_lease", {
                "p_run_id": str(run_id),
                "p_worker_id": worker_id,
                "p_lease_seconds": lease_seconds,
            }).execute()
        except Exception as exc:
            raise AppError("REPOSITORY_ERROR", str(exc), 502) from exc
        rows = response.data or []
        if not rows:
            # Either the run does not exist or the lease is held/finished.
            self.get_run(run_id)  # raises 404 when missing
            raise AppError("RUN_ALREADY_CLAIMED", "run is already claimed by another worker", 409)
        return rows[0] if isinstance(rows, list) else rows

    def heartbeat(self, run_id: UUID, worker_id: str, lease_seconds: int = 300, attempt: int | None = None, lease_token: str | None = None) -> dict[str, Any]:
        now = datetime.now(UTC)
        expires = (now + timedelta(seconds=lease_seconds)).isoformat()
        # Atomic ownership check: the lease only extends when this worker
        # still holds it.
        try:
            query = self.client.table("runs").update({"last_heartbeat_at": now.isoformat(), "lease_expires_at": expires}).eq("id", str(run_id)).eq("worker_id", worker_id).gt("lease_expires_at", now.isoformat()).in_("status", ["starting", "running", "cancellation_requested"])
            if attempt is not None:
                query = query.eq("attempt", attempt)
            if lease_token is not None:
                query = query.eq("lease_token", lease_token)
            rows = query.select("*").execute().data or []
        except Exception as exc:
            raise AppError("REPOSITORY_ERROR", str(exc), 502) from exc
        if not rows:
            raise AppError("RUN_LEASE_LOST", "run lease is held by another worker", 409)
        run = rows[0]
        self.client.table("worker_heartbeats").insert({"run_id": str(run_id), "worker_id": worker_id, "attempt": run.get("attempt", 1), "heartbeat_at": now.isoformat(), "lease_expires_at": expires}).execute()
        return run

    def request_cancellation(self, run_id: UUID, reason: str | None = None) -> dict[str, Any]:
        return self.transition_run(run_id, "cancellation_requested", cancellation_requested_at=datetime.now(UTC).isoformat(), cancellation_reason=reason)

    def mark_run_failed(self, run_id: UUID, code: str, message: str, worker_id: str | None = None) -> dict[str, Any]:
        return self.transition_run(run_id, "failed", expected_worker_id=worker_id, error={"code": code, "message": message}, finished_at=datetime.now(UTC).isoformat())

    def mark_run_complete(self, run_id: UUID, output: dict[str, Any], worker_id: str | None = None) -> dict[str, Any]:
        return self.transition_run(run_id, "completed", expected_worker_id=worker_id, output=output, error=None, finished_at=datetime.now(UTC).isoformat())

    def create_workflow_proposal(self, user_request: str, proposal: dict[str, Any], project_id: UUID | None = None, created_by: UUID | None = None) -> dict[str, Any]:
        payload = {"user_request": user_request, **proposal}
        if project_id is not None:
            payload["project_id"] = str(project_id)
        if created_by is not None:
            payload["created_by"] = str(created_by)
        return self._single(self.client.table("workflow_proposals").insert(payload).select("*"), "workflow_proposal", "new")

    def get_workflow_proposal(self, proposal_id: UUID, user_id: UUID | None = None) -> dict[str, Any]:
        query = self.client.table("workflow_proposals").select("*").eq("id", str(proposal_id)).limit(1)
        if user_id is not None:
            # Browser-facing reads require ownership: a project relationship
            # plus membership. Legacy proposals with NULL project_id never
            # match the inner join, so they 404 for every browser user.
            query = self.client.table("workflow_proposals").select("*, projects!inner(project_members!inner(user_id))").eq("id", str(proposal_id)).eq("projects.project_members.user_id", str(user_id)).limit(1)
        return self._single(query, "workflow_proposal", str(proposal_id))

    def update_workflow_proposal(self, proposal_id: UUID, fields: dict[str, Any]) -> dict[str, Any]:
        fields = {**fields, "updated_at": datetime.now(UTC).isoformat()}
        return self._single(self.client.table("workflow_proposals").update(fields).eq("id", str(proposal_id)).select("*"), "workflow_proposal", str(proposal_id))

    def create_project_from_proposal(self, proposal_id: UUID, slug: str, name: str, description: str | None, configuration: dict[str, Any], created_by: UUID | None = None) -> dict[str, Any]:
        # Atomic: the project row and the initial owner membership commit in
        # one transaction (migration 011); no orphan project can remain if
        # the membership insert fails.
        try:
            response = self.client.rpc("create_project_from_proposal_with_owner", {
                "p_proposal_id": str(proposal_id),
                "p_slug": slug,
                "p_name": name,
                "p_description": description,
                "p_configuration": configuration,
                "p_owner": str(created_by) if created_by is not None else None,
            }).execute()
        except Exception as exc:  # Supabase boundary only
            raise AppError("REPOSITORY_ERROR", str(exc), 502) from exc
        data = response.data
        if not data:
            raise AppError("REPOSITORY_ERROR", "project creation returned no row", 502)
        return data[0] if isinstance(data, list) else data

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

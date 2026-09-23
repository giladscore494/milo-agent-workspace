from datetime import UTC, datetime, timedelta
from typing import Any, Iterable, Protocol
from uuid import UUID
from postgrest.types import CountMethod
from supabase import create_client
from backend.catalog.diff import MAX_DIFF_ITEMS
from backend.catalog.payloads import (prepare_candidate, prepare_evidence_link,
                                      prepare_promotion, prepare_raw_record,
                                      prepare_snapshot)
from backend.config import Settings
from backend.errors import AppError, NotFoundError
from backend.runtime import (CLAIMED_RUN_STATES, RUN_STATES, TERMINAL_STATES, InvalidTransition,
                             cancellation_refusal, validate_transition)
from backend.schemas import normalize_conversation_title


class Repository(Protocol):
    def list_projects(self, user_id: UUID | None = None) -> list[dict[str, Any]]: ...
    def get_project(self, project_id: UUID, user_id: UUID | None = None) -> dict[str, Any]: ...
    def create_conversation(self, project_id: UUID, title: str | None, user_id: UUID | None = None) -> dict[str, Any]: ...
    def list_conversations(self, project_id: UUID) -> list[dict[str, Any]]: ...
    def get_conversation(self, conversation_id: UUID, user_id: UUID | None = None) -> dict[str, Any]: ...
    def create_user_message(self, conversation_id: UUID, content: str, metadata: dict[str, Any]) -> dict[str, Any]: ...
    def create_queued_run(self, conversation_id: UUID, user_message_id: int | str | UUID, content: str, metadata: dict[str, Any], requested_by: UUID | None = None, idempotency_key: str | None = None, request_fingerprint: str | None = None) -> dict[str, Any]:
        """Refuse the superseded split run-creation primitive.

        Console 6 makes immutable identity an INSERT-time property. Production
        run creation must therefore go through create_message_and_run_v3, which
        inserts message + run + identity in one transaction. Keeping a direct
        runs-table INSERT here would be a second writer whose only possible
        outcome under the database trigger is failure, and a future caller
        could mistake it for a supported creation authority.
        """
        raise AppError(
            "RUN_IDENTITY_ATOMIC_CREATION_REQUIRED",
            "queued runs must be created atomically with immutable identity",
            503,
        )

    def find_run_by_idempotency(self, conversation_id: UUID, user_id: UUID, idempotency_key: str) -> dict[str, Any] | None: ...
    def set_launch_state(self, run_id: UUID, state: str, error: dict[str, Any] | None = None) -> dict[str, Any]: ...
    def try_acquire_launch(self, run_id: UUID) -> dict[str, Any] | None: ...
    def count_active_runs_for_user(self, user_id: UUID) -> int: ...
    def count_active_runs_for_project(self, project_id: UUID) -> int: ...
    def update_run_usage(self, run_id: UUID, usage: dict[str, Any], worker_id: str | None = None, attempt: int | None = None, lease_token: str | None = None) -> dict[str, Any]: ...
    def record_run_usage(self, run_id: UUID, ledger: dict[str, Any], *, worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]: ...
    def get_run_usage_ledger(self, run_id: UUID) -> dict[str, Any] | None: ...
    def append_usage_ledger(self, entry: dict[str, Any], *, worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]: ...
    def get_run(self, run_id: UUID, user_id: UUID | None = None) -> dict[str, Any]: ...
    def list_conversation_runs(self, conversation_id: UUID, user_id: UUID | None = None, limit: int = 20) -> list[dict[str, Any]]: ...
    def list_run_events(self, run_id: UUID, user_id: UUID | None = None) -> list[dict[str, Any]]: ...
    def terminal_run_event(self, run_id: UUID) -> dict[str, Any] | None: ...
    def terminal_run_events(self, run_ids: list[UUID]) -> dict[str, dict[str, Any]]: ...
    def append_run_event(self, run_id: UUID, event_type: str, payload: dict[str, Any], worker_id: str | None = None, attempt: int | None = None, lease_token: str | None = None) -> dict[str, Any]: ...
    def save_checkpoint(self, checkpoint: dict[str, Any], worker_id: str | None = None, attempt: int | None = None, lease_token: str | None = None) -> dict[str, Any]: ...
    def latest_checkpoint(self, run_id: UUID, workflow_key: str | None = None) -> dict[str, Any] | None: ...
    def transition_run(self, run_id: UUID, status: str, expected_worker_id: str | None = None, expected_attempt: int | None = None, expected_lease_token: str | None = None, **fields: Any) -> dict[str, Any]: ...
    def finalize_run(self, run_id: UUID, status: str, expected_status: str, event: dict[str, Any] | None = None, *, worker_id: str, attempt: int | None, lease_token: str | None, **fields: Any) -> dict[str, Any]: ...
    def claim_run(self, run_id: UUID, worker_id: str, lease_seconds: int = 300) -> dict[str, Any]: ...
    def heartbeat(self, run_id: UUID, worker_id: str, lease_seconds: int = 300) -> dict[str, Any]: ...
    def request_cancellation(self, run_id: UUID, reason: str | None = None) -> dict[str, Any]: ...
    def mark_run_failed(self, run_id: UUID, code: str, message: str, worker_id: str | None = None, attempt: int | None = None, lease_token: str | None = None) -> dict[str, Any]: ...
    def mark_run_complete(self, run_id: UUID, output: dict[str, Any], worker_id: str | None = None, attempt: int | None = None, lease_token: str | None = None) -> dict[str, Any]: ...
    def create_workflow_proposal(self, user_request: str, proposal: dict[str, Any], project_id: UUID | None = None, created_by: UUID | None = None) -> dict[str, Any]: ...
    def get_workflow_proposal(self, proposal_id: UUID, user_id: UUID | None = None) -> dict[str, Any]: ...
    def update_workflow_proposal(self, proposal_id: UUID, fields: dict[str, Any]) -> dict[str, Any]: ...
    def create_project_from_proposal(self, proposal_id: UUID, slug: str, name: str, description: str | None, configuration: dict[str, Any], created_by: UUID | None = None) -> dict[str, Any]: ...
    def create_tool_access_request(self, run_id: UUID, request: dict[str, Any], *, worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]: ...
    def create_tool_grant(self, run_id: UUID, grant: dict[str, Any], *, worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]: ...
    def create_tool_usage(self, run_id: UUID, usage: dict[str, Any], *, worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]: ...
    def create_source(self, run_id: UUID, source: dict[str, Any], *, worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]: ...
    def create_claim(self, run_id: UUID, claim: dict[str, Any], *, worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]: ...
    def create_conflict(self, run_id: UUID, conflict: dict[str, Any], *, worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]: ...
    def record_evidence_fragment(self, run_id: UUID, fragment: dict[str, Any], *, worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]: ...
    def list_sources_for_ids(self, run_id: UUID, source_ids: Iterable[Any], *, limit: int = 50) -> list[dict[str, Any]]: ...
    def list_evidence_fragments_for_sources(self, run_id: UUID, source_ids: Iterable[Any], *, limit: int = 200) -> list[dict[str, Any]]: ...
    def list_structured_facts_for_sources(self, run_id: UUID, source_ids: Iterable[Any], *, limit: int = 200) -> list[dict[str, Any]]: ...
    def record_claim_verdict(self, run_id: UUID, verdict: dict[str, Any], *, worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]: ...
    def claim_current_verdict_states(self, run_id: UUID, claim_ids: Iterable[Any] | None = None, *, limit: int = 200) -> list[dict[str, Any]]: ...
    def record_conflict_resolution(self, run_id: UUID, resolution: dict[str, Any], *, worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]: ...
    def patch_run_blackboard_evidence(self, run_id: UUID, summary: dict[str, Any], *, worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]: ...
    def record_run_invocation(self, run_id: UUID, invocation: dict[str, Any]) -> dict[str, Any]: ...

    # --- durable catalog staging (PR1: persistence only) ---------------------
    #
    # The evidence relations above are RUN-SCOPED and cascade away with their
    # run.  These five write into the long-lived catalog namespace instead.
    # Every one is lease-guarded exactly like the evidence writes, and every
    # one is idempotent on a backend-derived key.  There is deliberately no
    # canonical-promotion method here: the canonical tables are read-only in
    # PR1 at the database level, so no repository method could write one.
    def record_catalog_snapshot(self, run_id: UUID, snapshot: dict[str, Any], *, worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]: ...
    def record_catalog_raw_record(self, run_id: UUID, record: dict[str, Any], *, worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]: ...
    def activate_catalog_snapshot(self, run_id: UUID, activation: dict[str, Any], *, worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]: ...
    def record_catalog_candidate(self, run_id: UUID, candidate: dict[str, Any], *, worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]: ...
    def link_catalog_candidate_evidence(self, run_id: UUID, link: dict[str, Any], *, worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]: ...

    # --- durable catalog reads (PR2: internal, bounded, no lease) ------------
    #
    # READS, so they take no lease: a lease authorizes a durable WRITE, and
    # requiring one to look at already-durable catalog state would make a
    # query layer impossible to build without holding a run open.  Each is
    # bounded, each orders deterministically, and none of them accepts SQL, a
    # table name, a column name or an ordering from its caller.
    def list_active_catalog_snapshots(self, source_family: str, *, resource_id: str | None = None, limit: int = 50, capture_scope_key: str | None = None) -> list[dict[str, Any]]: ...
    def find_active_catalog_snapshot(self, source_family: str, resource_id: str, snapshot_key: str) -> dict[str, Any] | None: ...
    def list_catalog_raw_records(self, snapshot_id: Any, *, limit: int = 500, offset: int = 0) -> list[dict[str, Any]]: ...
    def list_catalog_candidates(self, snapshot_id: Any, *, limit: int = 500, offset: int = 0) -> list[dict[str, Any]]: ...

    # --- bounded database-side catalog aggregation (PR3) ---------------------
    #
    # The Python projection reads a whole snapshot and refuses beyond
    # MAX_PROJECTION_CANDIDATES.  These answer over a snapshot of ANY size by
    # aggregating in the database: fixed filters, fixed ordering, an explicit
    # page and the EXACT total, so `has_more` is a fact rather than a guess.
    # Still reads, so still no lease, and still no SQL, table name, column name
    # or ordering from a caller.
    def catalog_candidate_manufacturers(self, snapshot_id: Any, *, limit: int = 50, offset: int = 0, allow_incomplete: bool = False) -> list[dict[str, Any]]: ...
    def catalog_candidate_models(self, snapshot_id: Any, *, manufacturer: str, limit: int = 50, offset: int = 0, allow_incomplete: bool = False) -> list[dict[str, Any]]: ...
    def catalog_candidate_model_years(self, snapshot_id: Any, *, manufacturer: str, commercial_model: str, limit: int = 50, offset: int = 0, allow_incomplete: bool = False) -> list[dict[str, Any]]: ...
    def catalog_candidate_variant_page(self, snapshot_id: Any, *, manufacturer: str | None = None, commercial_model: str | None = None, model_year: int | None = None, official_model_code: str | None = None, trim: str | None = None, identity_dimensions: dict[str, Any] | None = None, status: str | None = None, limit: int = 50, offset: int = 0, allow_incomplete: bool = False) -> list[dict[str, Any]]: ...
    def catalog_raw_record_by_upstream_id(self, snapshot_id: Any, upstream_record_id: str, *, allow_incomplete: bool = False) -> dict[str, Any] | None: ...
    def catalog_snapshot_candidate_diff(self, previous_snapshot_id: Any, snapshot_id: Any, *, limit: int = MAX_DIFF_ITEMS, allow_incomplete: bool = False) -> list[dict[str, Any]]: ...
    def catalog_run_pending_promotions(self, run_id: UUID, tool_operation: str, *, limit: int = 25) -> list[dict[str, Any]]: ...

    # --- field-level canonical promotion (PR3) -------------------------------
    #
    # The ONE write path into the canonical catalog.  Lease-guarded like every
    # other durable worker write, idempotent on a derived promotion key, and
    # atomic: the canonical identity and EVERY field's provenance are created
    # in one transaction or not at all.
    def promote_catalog_variant(self, run_id: UUID, promotion: dict[str, Any], *, worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]: ...
    def get_canonical_catalog_variant(self, canonical_key: str) -> dict[str, Any] | None: ...
    def list_canonical_field_provenance(self, variant_id: Any, *, limit: int = 200) -> list[dict[str, Any]]: ...
    def list_canonical_catalog_variants(self, *, manufacturer: str | None = None, commercial_model: str | None = None, model_year: int | None = None, canonical_key: str | None = None, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]: ...
    # Mapping plans (20260922000100_catalog_work_scopes.sql).
    def create_work_scope(self, conversation_id: UUID, created_by: UUID, revision: dict[str, Any]) -> dict[str, Any]: ...
    def revise_work_scope(self, work_scope_id: UUID, expected_revision: int, expected_digest: str, created_by: UUID, revision: dict[str, Any]) -> dict[str, Any]: ...
    def get_work_scope(self, work_scope_id: UUID) -> dict[str, Any] | None: ...
    def open_work_scope(self, conversation_id: UUID) -> dict[str, Any] | None: ...
    def list_work_scope_revisions(self, work_scope_id: UUID, *, limit: int = 11) -> list[dict[str, Any]]: ...
    def catalog_canonical_manufacturer_coverage(self, manufacturers: list[str]) -> list[dict[str, Any]]: ...
    # Plan preparation (20260923000100_catalog_work_scope_preparation.sql).
    def get_work_scope_revision(self, work_scope_id: UUID, revision: int) -> dict[str, Any] | None: ...
    def prepare_work_scope_queue(self, run_id: UUID, preparation: dict[str, Any], *, worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]: ...
    def bind_work_scope_batch_run(self, batch_id: UUID, run_id: UUID, expected_revision: int, expected_digest: str, bound_by: UUID) -> dict[str, Any]: ...
    def work_scope_batch_for_run(self, run_id: UUID) -> dict[str, Any] | None: ...
    # Batch runs, pause / resume and progress (20260924000100_catalog_work_scope_batch_runs.sql).
    def create_work_scope_batch_run(self, work_scope_id: UUID, batch_id: UUID, expected_revision: int, expected_digest: str, *, run_id: UUID, run_identity: dict[str, Any], content: str, metadata: dict[str, Any], requested_by: UUID, idempotency_key: str, request_fingerprint: str, max_user_active: int | None = None, max_project_active: int | None = None) -> dict[str, Any]: ...
    def set_work_scope_paused(self, work_scope_id: UUID, paused: bool, requested_by: UUID) -> dict[str, Any]: ...
    def work_scope_progress(self, work_scope_id: UUID) -> dict[str, Any] | None: ...

    def upsert_run_blackboard(self, run_id: UUID, blackboard: dict[str, Any], worker_id: str | None = None, attempt: int | None = None, lease_token: str | None = None) -> dict[str, Any]: ...
    def create_agent_message(self, message: dict[str, Any], worker_id: str | None = None, attempt: int | None = None, lease_token: str | None = None) -> dict[str, Any]: ...
    def list_unread_agent_messages(self, run_id: UUID, recipient: str = "supervisor") -> list[dict[str, Any]]: ...
    def create_supervisor_decision(self, run_id: UUID, decision: dict[str, Any], worker_id: str | None = None, attempt: int | None = None, lease_token: str | None = None) -> dict[str, Any]: ...
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

    def _many_with_count(self, query: Any) -> tuple[list[dict[str, Any]], int | None]:
        """Rows PLUS the exact count PostgREST reported, or None for "not said".

        Deliberately not folded into `_many`: a count is only present when the
        query asked for one, and a caller that did not ask must not receive a
        number it cannot account for.  The sanitized message is the same one
        `_many` raises -- a PostgREST detail can quote SQL values, so it never
        becomes the message.
        """
        try:
            response = query.execute()
        except Exception as exc:
            raise AppError("REPOSITORY_ERROR", str(exc), 502) from exc
        count = getattr(response, "count", None)
        return (response.data or []), (None if count is None else int(count))

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
        # conversations.title is NOT NULL in production; never insert None.
        return self._single(self.client.table("conversations").insert({"project_id": str(project_id), "title": normalize_conversation_title(title)}).select("*"), "conversation", "new")

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
        """Refuse the superseded split run-creation primitive.

        Console 6 makes immutable identity an INSERT-time property. Production
        run creation must therefore go through create_message_and_run_v3, which
        inserts message + run + identity in one transaction. Keeping a direct
        runs-table INSERT here would be a second writer whose only possible
        outcome under the database trigger is failure, and a future caller
        could mistake it for a supported creation authority.
        """
        raise AppError(
            "RUN_IDENTITY_ATOMIC_CREATION_REQUIRED",
            "queued runs must be created atomically with immutable identity",
            503,
        )

    def find_run_by_idempotency(self, conversation_id: UUID, user_id: UUID, idempotency_key: str) -> dict[str, Any] | None:
        rows = self._many(
            self.client.table("runs").select("*")
            .eq("conversation_id", str(conversation_id))
            .eq("requested_by", str(user_id))
            .eq("idempotency_key", idempotency_key)
            .limit(1)
        )
        return rows[0] if rows else None

    def create_message_and_run(self, conversation_id: UUID, content: str, metadata: dict[str, Any], requested_by: UUID, idempotency_key: str | None, request_fingerprint: str, max_user_active: int | None = None, max_project_active: int | None = None, *, run_id: UUID, run_identity: dict[str, Any]) -> dict[str, Any]:
        """Atomically create the message, queued run and immutable identity."""
        try:
            response = self.client.rpc("create_message_and_run_v3", {
                "p_run_id": str(run_id),
                "p_run_identity": run_identity,
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
            if "IDEMPOTENCY_CONFLICT" in message:
                raise AppError(
                    "IDEMPOTENCY_CONFLICT",
                    "idempotency key was already used with a different payload",
                    409,
                ) from exc
            if "IDEMPOTENCY_FINGERPRINT_REQUIRED" in message:
                raise AppError(
                    "IDEMPOTENCY_FINGERPRINT_REQUIRED",
                    "run creation requires a request fingerprint",
                    409,
                ) from exc
            if "RUN_IDENTITY_WORKFLOW_DRIFT" in message:
                raise AppError("RUN_IDENTITY_WORKFLOW_DRIFT",
                               "project workflow changed before the run could be created", 409) from exc
            if "RUN_IDENTITY_INVALID" in message or "RUN_IDENTITY_REQUIRED" in message:
                raise AppError("RUN_IDENTITY_INVALID", "run identity was rejected", 409) from exc
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

    def update_run_usage(self, run_id: UUID, usage: dict[str, Any], worker_id: str | None = None, attempt: int | None = None, lease_token: str | None = None) -> dict[str, Any]:
        if worker_id is not None:
            # Worker-originated usage snapshot: lease validity (including
            # expiry) is decided by the DATABASE clock inside the guarded
            # RPC; a stale worker cannot clobber the live worker's accounting.
            return self._guarded_rpc("update_run_usage_guarded", {
                "p_run_id": str(run_id),
                "p_worker_id": worker_id,
                "p_attempt": attempt,
                "p_lease_token": lease_token,
                "p_usage": usage,
            }, "run")
        try:
            rows = self.client.table("runs").update({"usage": usage}).eq("id", str(run_id)).select("*").execute().data or []
        except Exception as exc:
            raise AppError("REPOSITORY_ERROR", str(exc), 502) from exc
        if not rows:
            raise NotFoundError("run", str(run_id))
        return rows[0]

    def record_run_usage(self, run_id: UUID, ledger: dict[str, Any], *, worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]:
        """Record the run's cumulative ExecutionUsageLedger (migration
        20260920000100). The database MERGES component-wise (no accepted
        write can lower a counter), advances `version` only on a real change,
        verifies the lease under the database clock and projects the public
        aggregate into `runs.usage` in the same transaction. Returns the
        durable row: `ledger` (with its `ledger_version`) and `version`."""
        row = self._guarded_rpc("record_run_usage_guarded", {
            "p_run_id": str(run_id),
            "p_worker_id": worker_id,
            "p_attempt": attempt,
            "p_lease_token": lease_token,
            "p_ledger": ledger,
        }, "run_execution_usage")
        return row

    def get_run_usage_ledger(self, run_id: UUID) -> dict[str, Any] | None:
        """The durable ledger row of a run, or None before its first write."""
        rows = self._many(
            self.client.table("run_execution_usage").select("*").eq("run_id", str(run_id)).limit(1)
        )
        return rows[0] if rows else None

    LEDGER_FIELDS = (
        "run_id", "project_id", "user_id", "provider", "model", "call_seq", "decision",
        "rejection_reason", "reserved_input_tokens", "reserved_output_tokens",
        "actual_input_tokens", "actual_output_tokens", "estimated_cost", "actual_cost",
    )

    def append_usage_ledger(self, entry: dict[str, Any], *, worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]:
        """Append ONE per-call ledger row, under the worker's lease.

        This was the last unfenced durable write a running worker performed:
        every other usage surface (runs.usage, run_execution_usage) has been
        lease-guarded since migration 20260920000100, but the per-call rows the
        DAILY budget is summed from went straight into the table. A replaced
        worker could therefore keep charging a run it no longer owned, against
        the live worker's daily allowance.

        The run id travels as the FENCED argument, not as a payload field, so
        an entry naming another run cannot charge one.
        """
        run_id = entry.get("run_id")
        if not run_id:
            raise AppError("REPOSITORY_ERROR", "a usage ledger entry requires its run id", 502)
        payload = {key: entry[key] for key in self.LEDGER_FIELDS
                   if key != "run_id" and entry.get(key) is not None}
        params = {**self._lease_params(UUID(str(run_id)), worker_id, attempt, lease_token),
                  "p_entry": payload}
        return self._guarded_rpc("append_usage_ledger_guarded", params, "run_usage_ledger")

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

    def reserve_model_call_budget(self, run_id: UUID, call_seq: int, user_id: str | None, project_id: str | None, amount: float, daily_user_limit: float | None, daily_project_limit: float | None, worker_id: str | None = None, attempt: int | None = None, lease_token: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {
            "p_run_id": str(run_id),
            "p_call_seq": int(call_seq),
            "p_user_id": str(user_id) if user_id else None,
            "p_project_id": str(project_id) if project_id else None,
            "p_estimated_cost": amount,
            "p_daily_user_limit": daily_user_limit,
            "p_daily_project_limit": daily_project_limit,
        }
        if worker_id is not None:
            # A stale worker cannot reserve budget for a run it no longer owns.
            row = self._guarded_rpc("reserve_model_call_budget_guarded", {
                **params, "p_worker_id": worker_id, "p_attempt": attempt, "p_lease_token": lease_token,
            }, "model_call_budget_reservation")
        else:
            row = self.client.rpc("reserve_model_call_budget_v2", params).execute().data
            row = row[0] if isinstance(row, list) else row
        if row and row.get("status") == "rejected":
            reason = row.get("rejection_reason") or "DAILY_BUDGET_REACHED"
            raise AppError(reason, "daily budget exhausted", 429)
        return row

    def settle_model_call_budget(self, reservation_id: str, actual_cost: float, status: str = "settled", rejection_reason: str | None = None, run_id: UUID | None = None, worker_id: str | None = None, attempt: int | None = None, lease_token: str | None = None) -> dict[str, Any]:
        if worker_id is not None and run_id is not None:
            return self._guarded_rpc("settle_model_call_budget_guarded", {
                "p_reservation_id": str(reservation_id),
                "p_actual_cost": actual_cost,
                "p_run_id": str(run_id),
                "p_worker_id": worker_id,
                "p_attempt": attempt,
                "p_lease_token": lease_token,
                "p_status": status,
                "p_rejection_reason": rejection_reason,
            }, "model_call_budget_reservation")
        row = self.client.rpc("settle_model_call_budget_v2", {
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

    #: The events the canonical finalizer writes in the same transaction as
    #: the terminal status (backend/finalization.py `_TERMINAL_EVENT`).
    TERMINAL_EVENT_TYPES = ("run_completed", "run_partial_success", "run_failed", "run_cancelled")

    def terminal_run_event(self, run_id: UUID) -> dict[str, Any] | None:
        """The LATEST terminal event of a run, or None.

        Read separately from the bounded event page because a long run can
        hold more events than one page returns, and the ProductOutcome the
        finalizer recorded rides on exactly this event. Callers must already
        have authorized the run read.
        """
        rows = self._many(
            self.client.table("run_events").select("*")
            .eq("run_id", str(run_id))
            .in_("event_type", list(self.TERMINAL_EVENT_TYPES))
            .order("id", desc=True).limit(1)
        )
        return rows[0] if rows else None

    def terminal_run_events(self, run_ids: list[UUID]) -> dict[str, dict[str, Any]]:
        """The latest terminal event of EACH run in one bounded read.

        One query for a whole history page instead of one per row; the newest
        event per run wins, exactly as `terminal_run_event` decides for one.
        """
        ids = [str(run_id) for run_id in run_ids][:50]
        if not ids:
            return {}
        rows = self._many(
            self.client.table("run_events").select("*")
            .in_("run_id", ids)
            .in_("event_type", list(self.TERMINAL_EVENT_TYPES))
            .order("id", desc=True).limit(4 * len(ids))
        )
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            latest.setdefault(str(row.get("run_id")), row)
        return latest

    def list_conversation_runs(self, conversation_id: UUID, user_id: UUID | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """A conversation's runs, newest first, bounded.

        Membership is proven through the conversation (conversations ->
        projects -> project_members) before any run row is read, exactly as
        the single-run read proves it through the run. The payload columns are
        not selected: the history is for choosing a run, and the product is
        read through `get_run` once one is chosen.
        """
        self.get_conversation(conversation_id, user_id)
        bounded = max(1, min(int(limit), 50))
        columns = ("id, conversation_id, status, attempt, started_at, finished_at, created_at, updated_at, "
                   "launch_state, usage, run_identity")
        return self._many(
            self.client.table("runs").select(columns)
            .eq("conversation_id", str(conversation_id))
            .order("created_at", desc=True).limit(bounded)
        )

    @staticmethod
    def _is_stale_lease_error(exc: Exception) -> bool:
        return "STALE_WORKER_WRITE" in str(exc)

    def _guarded_rpc(self, function: str, params: dict[str, Any], resource: str) -> dict[str, Any]:
        """Call a lease-guarded RPC (migration 20260810000300); a stale lease
        surfaces as RUN_LEASE_LOST so worker code paths treat it exactly like
        a failed heartbeat."""
        try:
            data = self.client.rpc(function, params).execute().data
        except Exception as exc:
            if self._is_stale_lease_error(exc):
                raise AppError("RUN_LEASE_LOST", "run lease is held by another worker", 409) from exc
            # Provider/PostgREST details can contain SQL values, URLs, or
            # credentials.  Keep the original exception only as an internal
            # cause; durable/API-visible errors are deliberately generic.
            raise AppError("REPOSITORY_ERROR", "guarded persistence operation failed", 502) from exc
        if isinstance(data, list):
            data = data[0] if data else None
        if data is None:
            raise AppError("REPOSITORY_ERROR", f"{resource} guarded write returned no row", 502)
        return data

    def append_run_event(self, run_id: UUID, event_type: str, payload: dict[str, Any], worker_id: str | None = None, attempt: int | None = None, lease_token: str | None = None) -> dict[str, Any]:
        if worker_id is not None:
            # Worker-originated events are lease-guarded atomically in the
            # database: a stale worker cannot append to the event stream.
            return self._guarded_rpc("append_run_event_guarded", {
                "p_run_id": str(run_id),
                "p_worker_id": worker_id,
                "p_attempt": attempt,
                "p_lease_token": lease_token,
                "p_event_type": event_type,
                "p_message": payload.get("message"),
                "p_agent": payload.get("agent"),
                "p_phase": payload.get("phase"),
                "p_progress": payload.get("progress"),
                "p_payload": payload.get("payload", payload),
            }, "run_event")
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

    CHECKPOINT_COLUMNS = ("run_id", "engine_version", "workflow_key", "phase", "completed_tasks", "artifacts", "failures", "token_usage", "last_event", "attempt")

    def save_checkpoint(self, checkpoint: dict[str, Any], worker_id: str | None = None, attempt: int | None = None, lease_token: str | None = None) -> dict[str, Any]:
        # run_checkpoints requires these columns NOT NULL; refuse locally with
        # a clear error instead of surfacing a database constraint violation
        # after a paid model call already happened.
        for column in ("engine_version", "workflow_key", "phase"):
            if not checkpoint.get(column):
                raise AppError("CHECKPOINT_INVALID", f"checkpoint is missing mandatory field: {column}", 422)
        if worker_id is not None:
            return self._guarded_rpc("save_checkpoint_guarded", {
                "p_run_id": str(checkpoint["run_id"]),
                "p_worker_id": worker_id,
                "p_attempt": attempt,
                "p_lease_token": lease_token,
                "p_engine_version": checkpoint.get("engine_version"),
                "p_workflow_key": checkpoint.get("workflow_key"),
                "p_phase": checkpoint.get("phase"),
                "p_completed_tasks": checkpoint.get("completed_tasks", []),
                "p_artifacts": checkpoint.get("artifacts", {}),
                "p_failures": checkpoint.get("failures", []),
                "p_token_usage": checkpoint.get("token_usage", {}),
                "p_last_event": checkpoint.get("last_event"),
            }, "run_checkpoint")
        # run_checkpoints has no worker_id/lease_token columns; keep the row
        # to the real schema.
        row = {key: checkpoint[key] for key in self.CHECKPOINT_COLUMNS if key in checkpoint}
        return self._single(self.client.table("run_checkpoints").insert(row).select("*"), "run_checkpoint", "new")

    def latest_checkpoint(self, run_id: UUID, workflow_key: str | None = None) -> dict[str, Any] | None:
        query = self.client.table("run_checkpoints").select("*").eq("run_id", str(run_id)).order("created_at", desc=True).limit(1)
        if workflow_key:
            query = query.eq("workflow_key", workflow_key)
        rows = self._many(query)
        return rows[0] if rows else None

    WORKER_TRANSITION_FIELDS = {"output", "error", "usage", "started_at", "finished_at"}

    def transition_run(self, run_id: UUID, status: str, expected_worker_id: str | None = None, expected_attempt: int | None = None, expected_lease_token: str | None = None, **fields: Any) -> dict[str, Any]:
        if status in TERMINAL_STATES:
            raise AppError(
                "CANONICAL_FINALIZER_REQUIRED",
                "terminal run states must be written through the canonical finalizer",
                409,
            )
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
        if expected_worker_id is not None:
            # Worker-originated transition: lease validity (including expiry)
            # is decided by the DATABASE clock inside the guarded RPC, never
            # by this container's clock.
            unsupported = set(fields) - self.WORKER_TRANSITION_FIELDS
            if unsupported:
                raise AppError("REPOSITORY_ERROR", f"unsupported worker transition fields: {sorted(unsupported)}", 502)
            return self._guarded_rpc("transition_run_worker_guarded", {
                "p_run_id": str(run_id),
                "p_status": status,
                "p_expected_status": current if transitioning else None,
                "p_worker_id": expected_worker_id,
                "p_attempt": expected_attempt,
                "p_lease_token": expected_lease_token,
                "p_output": fields.get("output"),
                "p_error": fields.get("error"),
                "p_clear_error": "error" in fields and fields["error"] is None,
                "p_usage": fields.get("usage"),
                "p_started_at": fields.get("started_at"),
                "p_finished_at": fields.get("finished_at"),
            }, "run")
        payload = {"status": status, **fields}
        query = self.client.table("runs").update(payload).eq("id", str(run_id))
        if transitioning:
            # Compare-and-set on the observed status: a concurrent transition
            # (e.g. a newer worker writing a terminal result) makes this
            # update match zero rows instead of silently overwriting it.
            query = query.eq("status", current)
        try:
            rows = query.select("*").execute().data or []
        except Exception as exc:
            raise AppError("REPOSITORY_ERROR", str(exc), 502) from exc
        if not rows:
            raise AppError("RUN_TRANSITION_CONFLICT", "run was modified concurrently or the lease is no longer held", 409)
        return rows[0] if isinstance(rows, list) else rows

    #: The fields a terminal finalization may carry. `started_at` is absent on
    #: purpose: a run being finalized has started, and a terminal write that
    #: could also rewrite when it started would be two decisions in one call.
    FINALIZATION_FIELDS = {"output", "error", "usage", "finished_at"}

    def finalize_run(self, run_id: UUID, status: str, expected_status: str, event: dict[str, Any] | None = None, *, worker_id: str, attempt: int | None, lease_token: str | None, **fields: Any) -> dict[str, Any]:
        """Make a run terminal AND record its terminal event, atomically.

        Migration 20260920000200's `finalize_run_guarded` performs the
        lease-guarded, compare-and-set transition and the terminal event
        insert in ONE transaction, so a terminal event can never exist for a
        decision that did not become the run's durable state, and a terminal
        run can never be left without the evidence its event carries. The CAS
        is against `expected_status`, the state the decision was taken under:
        a run that moved since (a cancellation request, a reclaim) rejects the
        finalization with RUN_LEASE_LOST, exactly like a stale lease, so the
        caller re-reads and decides again rather than overwriting.

        `event` is `{"type", "message", "payload"}` or None (a budget stop's
        cause event was already emitted by the tracker).
        """
        if status not in TERMINAL_STATES:
            raise AppError("INVALID_RUN_TRANSITION", f"{status!r} is not a terminal run status", 409)
        if expected_status in RUN_STATES:
            try:
                validate_transition(expected_status, status)
            except InvalidTransition as exc:
                raise AppError("INVALID_RUN_TRANSITION", str(exc), 409) from exc
        unsupported = set(fields) - self.FINALIZATION_FIELDS
        if unsupported:
            raise AppError("REPOSITORY_ERROR", f"unsupported finalization fields: {sorted(unsupported)}", 502)
        event = event or {}
        return self._guarded_rpc("finalize_run_guarded", {
            "p_run_id": str(run_id),
            "p_status": status,
            "p_expected_status": expected_status,
            "p_worker_id": worker_id,
            "p_attempt": attempt,
            "p_lease_token": lease_token,
            "p_output": fields.get("output"),
            "p_error": fields.get("error"),
            "p_clear_error": "error" in fields and fields["error"] is None,
            "p_usage": fields.get("usage"),
            "p_finished_at": fields.get("finished_at"),
            "p_event_type": event.get("type"),
            "p_event_message": event.get("message"),
            "p_event_payload": event.get("payload") or {},
        }, "run")

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
        # Atomic ownership check with DATABASE-clock expiry: the lease only
        # extends when this worker still holds it according to PostgreSQL
        # now(); a skewed container clock cannot revive an expired lease.
        return self._guarded_rpc("heartbeat_run_guarded", {
            "p_run_id": str(run_id),
            "p_worker_id": worker_id,
            "p_attempt": attempt,
            "p_lease_token": lease_token,
            "p_lease_seconds": int(lease_seconds),
        }, "run")

    def request_cancellation(self, run_id: UUID, reason: str | None = None) -> dict[str, Any]:
        """Request cancellation of a run a worker will finalize -- and only of one.

        The request is a compare-and-set the DATABASE evaluates in the same
        statement as the write: on the status that was read and, for a run no
        worker has claimed yet, on its launch being recorded `launched`. A run
        whose launch never happened or is unresolved is refused
        (`cancellation_refusal`), and a run that moved between the read and the
        write matches nothing and is a conflict. So no request can rest at
        `cancellation_requested` with nobody to finalize it.
        """
        run = self.get_run(run_id)
        status = str(run.get("status") or "")
        refusal = cancellation_refusal(status, run.get("launch_state"))
        if refusal is not None:
            raise AppError(refusal, "a cancellation of this run could never be finalized", 409)
        try:
            validate_transition(status, "cancellation_requested")
        except InvalidTransition as exc:
            raise AppError("INVALID_RUN_TRANSITION", str(exc), 409) from exc
        query = (self.client.table("runs")
                 .update({"status": "cancellation_requested",
                          "cancellation_requested_at": datetime.now(UTC).isoformat(),
                          "cancellation_reason": reason})
                 .eq("id", str(run_id)).eq("status", status))
        if status not in CLAIMED_RUN_STATES:
            # No worker holds it yet: only a launch that started one may be
            # cancelled, and the database checks that at the moment of writing.
            query = query.eq("launch_state", "launched")
        try:
            rows = query.select("*").execute().data or []
        except Exception as exc:
            raise AppError("REPOSITORY_ERROR", str(exc), 502) from exc
        if not rows:
            raise AppError("RUN_TRANSITION_CONFLICT", "run was modified concurrently", 409)
        return rows[0] if isinstance(rows, list) else rows

    def mark_run_failed(self, run_id: UUID, code: str, message: str, worker_id: str | None = None, attempt: int | None = None, lease_token: str | None = None) -> dict[str, Any]:
        raise AppError(
            "CANONICAL_FINALIZER_REQUIRED",
            "run failure must be written through the canonical finalizer",
            409,
        )

    def mark_run_complete(self, run_id: UUID, output: dict[str, Any], worker_id: str | None = None, attempt: int | None = None, lease_token: str | None = None) -> dict[str, Any]:
        raise AppError(
            "CANONICAL_FINALIZER_REQUIRED",
            "run completion must be written through the canonical finalizer",
            409,
        )

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
            response = self.client.rpc("create_project_from_proposal_with_owner_v2", {
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

    def upsert_run_blackboard(self, run_id: UUID, blackboard: dict[str, Any], worker_id: str | None = None, attempt: int | None = None, lease_token: str | None = None) -> dict[str, Any]:
        if worker_id is not None:
            return self._guarded_rpc("upsert_run_blackboard_guarded", {
                "p_run_id": str(run_id),
                "p_worker_id": worker_id,
                "p_attempt": attempt,
                "p_lease_token": lease_token,
                "p_blackboard": blackboard,
            }, "run_blackboard")
        payload = {"run_id": str(run_id), **blackboard, "updated_at": datetime.now(UTC).isoformat()}
        return self._single(self.client.table("run_blackboards").upsert(payload, on_conflict="run_id").select("*"), "run_blackboard", str(run_id))

    def create_agent_message(self, message: dict[str, Any], worker_id: str | None = None, attempt: int | None = None, lease_token: str | None = None) -> dict[str, Any]:
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
        if worker_id is not None:
            return self._guarded_rpc("create_agent_message_guarded", {
                "p_run_id": payload["run_id"],
                "p_worker_id": worker_id,
                "p_attempt": attempt,
                "p_lease_token": lease_token,
                "p_message": payload,
            }, "agent_message")
        return self._single(self.client.table("agent_messages").insert(payload).select("*"), "agent_message", "new")

    def list_unread_agent_messages(self, run_id: UUID, recipient: str = "supervisor") -> list[dict[str, Any]]:
        return self._many(self.client.table("agent_messages").select("*").eq("run_id", str(run_id)).eq("recipient", recipient).is_("read_at", "null").order("created_at"))

    def create_supervisor_decision(self, run_id: UUID, decision: dict[str, Any], worker_id: str | None = None, attempt: int | None = None, lease_token: str | None = None) -> dict[str, Any]:
        if worker_id is not None:
            return self._guarded_rpc("create_supervisor_decision_guarded", {
                "p_run_id": str(run_id),
                "p_worker_id": worker_id,
                "p_attempt": attempt,
                "p_lease_token": lease_token,
                "p_decision": decision,
            }, "supervisor_decision")
        payload = {"run_id": str(run_id), "mode": "shadow", **decision}
        return self._single(self.client.table("supervisor_decisions").insert(payload).select("*"), "supervisor_decision", "new")

    def list_supervisor_decisions(self, run_id: UUID) -> list[dict[str, Any]]:
        return self._many(self.client.table("supervisor_decisions").select("*").eq("run_id", str(run_id)).order("created_at"))

    def create_tool_access_request(self, run_id: UUID, request: dict[str, Any], *, worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]:
        """Lease-guarded (migration 20260921000200).

        This was a direct table insert on behalf of a worker, so a replaced
        worker could still open tool access on a run it no longer owned.
        """
        params = {**self._lease_params(run_id, worker_id, attempt, lease_token), "p_request": request}
        return self._guarded_rpc("create_tool_access_request_guarded", params, "tool_access_request")

    def create_tool_grant(self, run_id: UUID, grant: dict[str, Any], *, worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]:
        """Lease-guarded (migration 20260921000200).

        The unfenced version mutated TWO tables -- the grant insert and the
        referenced request's status -- in two separate unfenced statements.
        The guarded RPC does both inside one function body, under the lease.
        """
        params = {**self._lease_params(run_id, worker_id, attempt, lease_token), "p_grant": grant}
        return self._guarded_rpc("create_tool_grant_guarded", params, "tool_grant")

    @staticmethod
    def _lease_params(run_id: UUID, worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]:
        if not worker_id or not lease_token or attempt < 1:
            raise AppError("INVALID_WORKER_LEASE", "complete worker lease is required", 422)
        return {"p_run_id": str(run_id), "p_worker_id": worker_id,
                "p_attempt": attempt, "p_lease_token": lease_token}

    def create_tool_usage(self, run_id: UUID, usage: dict[str, Any], *, worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]:
        params = {**self._lease_params(run_id, worker_id, attempt, lease_token), "p_usage": usage}
        return self._guarded_rpc("create_tool_usage_guarded", params, "tool_usage")

    def create_source(self, run_id: UUID, source: dict[str, Any], *, worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]:
        params = {**self._lease_params(run_id, worker_id, attempt, lease_token), "p_source": source}
        return self._guarded_rpc("upsert_source_guarded", params, "source")

    def create_claim(self, run_id: UUID, claim: dict[str, Any], *, worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]:
        params = {**self._lease_params(run_id, worker_id, attempt, lease_token), "p_claim": claim}
        return self._guarded_rpc("create_claim_with_source_guarded", params, "claim")

    def create_conflict(self, run_id: UUID, conflict: dict[str, Any], *, worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]:
        params = {**self._lease_params(run_id, worker_id, attempt, lease_token), "p_conflict": conflict}
        return self._guarded_rpc("create_conflict_guarded", params, "conflict")

    def record_evidence_fragment(self, run_id: UUID, fragment: dict[str, Any], *, worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]:
        params = {**self._lease_params(run_id, worker_id, attempt, lease_token), "p_fragment": fragment}
        return self._guarded_rpc("record_evidence_fragment_guarded", params, "evidence_fragment")

    # The explicit safe column allowlist a grounded verifier may see: durable
    # provenance plus the browser-visible descriptive metadata that already
    # travels with a source.  Never `*`: agent, query, tool_operation,
    # retrieved_at and evidence_key are operational bookkeeping, not evidence.
    # R3 adds the source VERSION, which is evidence provenance: it says which
    # version of the source the fragments below were read at.  retrieved_at
    # stays excluded -- when we looked is not what we looked at.
    SOURCE_CONTEXT_COLUMNS = ("id, run_id, task_key, url, title, domain, "
                              "source_type, source_strength, source_date, "
                              "source_version_kind, source_version_id")
    # 50 sources per read keeps the paired fragment read (<= 4 rows per source)
    # inside MAX_EVIDENCE_FRAGMENT_ROWS, so neither cap can silently truncate.
    MAX_SOURCE_CONTEXT_ROWS = 50

    def list_sources_for_ids(self, run_id: UUID, source_ids: Iterable[Any], *, limit: int = MAX_SOURCE_CONTEXT_ROWS) -> list[dict[str, Any]]:
        """Internal bounded read of durable source metadata for the grounded
        verifier.

        Server/service path only: no browser endpoint exposes it, it performs
        no provider call, and the caller supplies a run plus source ids -- never
        SQL.  Rows are restricted to the requested run AND the requested
        sources, selected through an explicit column allowlist, capped at
        MAX_SOURCE_CONTEXT_ROWS and ordered deterministically by id.  The
        public SourceRecord/browser contract is untouched: this adds a read,
        not a column, a table or an endpoint."""
        identifiers = sorted({str(source_id) for source_id in source_ids})
        if not identifiers:
            return []
        bounded = max(1, min(int(limit), self.MAX_SOURCE_CONTEXT_ROWS))
        return self._many(
            self.client.table("sources").select(self.SOURCE_CONTEXT_COLUMNS)
            .eq("run_id", str(run_id)).in_("id", identifiers).order("id").limit(bounded))

    EVIDENCE_FRAGMENT_COLUMNS = ("id, run_id, source_id, task_key, evidence_key, "
                                 "fragment_text, content_hash, fragment_index, "
                                 "fragment_type, locator_key, created_at")
    MAX_EVIDENCE_FRAGMENT_ROWS = 200

    def list_evidence_fragments_for_sources(self, run_id: UUID, source_ids: Iterable[Any], *, limit: int = MAX_EVIDENCE_FRAGMENT_ROWS) -> list[dict[str, Any]]:
        """Internal bounded read of durable evidence fragments for a future
        grounded verifier.

        Server/service path only: no browser endpoint exposes it, it performs
        no provider call, and the caller supplies a run plus source ids -- never
        SQL.  Rows are restricted to the requested run AND the requested
        sources, capped at MAX_EVIDENCE_FRAGMENT_ROWS, and ordered
        deterministically by (source_id, fragment_index, content_hash).  A
        source with no fragment simply yields no row: a legacy source without
        grounding context is never fabricated."""
        identifiers = sorted({str(source_id) for source_id in source_ids})
        if not identifiers:
            return []
        bounded = max(1, min(int(limit), self.MAX_EVIDENCE_FRAGMENT_ROWS))
        return self._many(
            self.client.table("source_evidence_fragments").select(self.EVIDENCE_FRAGMENT_COLUMNS)
            .eq("run_id", str(run_id)).in_("source_id", identifiers)
            .order("source_id").order("fragment_index").order("content_hash").limit(bounded))

    # R4: the explicit safe column allowlist of a durable STRUCTURED SOURCE
    # FACT -- a claim that was read from an exact location in a versioned
    # source.  It is the comparison authority of the deterministic verifier, so
    # it carries exactly what a comparison needs: identity, scope, value, unit
    # and the locator it was read at.  Never `*`: agent, confidence,
    # source_strength, status, evidence_key and the canonical scope columns are
    # bookkeeping, not the fact.
    STRUCTURED_FACT_COLUMNS = ("id, run_id, source_id, task_key, entity_key, field_key, "
                               "value, unit, time_scope, geography, market, "
                               "evidence_locator, identity_scope")
    MAX_STRUCTURED_FACT_ROWS = 200

    def list_structured_facts_for_sources(self, run_id: UUID, source_ids: Iterable[Any], *, limit: int = MAX_STRUCTURED_FACT_ROWS) -> list[dict[str, Any]]:
        """Internal bounded read of the structured facts recorded FROM sources.

        The third and last internal read the grounded/deterministic verifier
        may perform.  Server/service path only: no browser endpoint exposes it,
        it performs no provider call, and the caller supplies a run plus source
        ids -- never SQL.  Rows are restricted to the requested run AND the
        requested sources, filtered to claims that carry an R3 evidence locator
        (a claim with no locator is a statement ABOUT a source, not a fact read
        FROM it), selected through an explicit column allowlist, capped at
        MAX_STRUCTURED_FACT_ROWS and ordered deterministically by id.  The
        public claim/browser contract is untouched: this adds a read, not a
        column, a table or an endpoint."""
        identifiers = sorted({str(source_id) for source_id in source_ids})
        if not identifiers:
            return []
        bounded = max(1, min(int(limit), self.MAX_STRUCTURED_FACT_ROWS))
        return self._many(
            self.client.table("claims").select(self.STRUCTURED_FACT_COLUMNS)
            .eq("run_id", str(run_id)).in_("source_id", identifiers)
            .not_.is_("evidence_locator", "null")
            .order("source_id").order("id").limit(bounded))

    def record_claim_verdict(self, run_id: UUID, verdict: dict[str, Any], *, worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]:
        params = {**self._lease_params(run_id, worker_id, attempt, lease_token), "p_verdict": verdict}
        return self._guarded_rpc("record_claim_verdict_guarded", params, "claim_verdict")

    #: The durable bound of ONE current-verdict read, mirrored from
    #: `public.claim_current_verdict_states`, which clamps to the same number.
    MAX_CURRENT_VERDICT_ROWS = 500

    def claim_current_verdict_states(self, run_id: UUID, claim_ids: Iterable[Any] | None = None,
                                     *, limit: int = 200) -> list[dict[str, Any]]:
        """R5: which of a run's claims are verified RIGHT NOW, and on what.

        One bounded READ. It takes no lease because it writes nothing, and it
        is the ONLY question a consumer of verified evidence should be asking:
        `public.claim_verdicts` is append-only history, so "a verified verdict
        exists" stays true forever after a re-verification has rejected the
        claim. The resolution itself is
        `backend/engines/swarm_v2/current_verdict.py`, implemented in SQL by
        `public.claim_current_verdict_state` so the same rule holds for a
        writer that never came through this process.
        """
        identifiers = None if claim_ids is None else [str(item) for item in claim_ids]
        if identifiers is not None and not identifiers:
            return []
        return self._read_rpc("claim_current_verdict_states",
                              {"p_run_id": str(run_id), "p_claim_ids": identifiers,
                               "p_limit": max(0, min(int(limit),
                                                     self.MAX_CURRENT_VERDICT_ROWS))})

    # --- durable catalog staging --------------------------------------------
    #
    # Guarded RPCs, never direct inserts: the lease check, the referential
    # provenance checks, the cross-run and cross-snapshot guards and the
    # fail-closed replay conflict all live in the database, so they hold for
    # any caller rather than for whichever backend release happens to be
    # deployed.  The function name is a literal in every case -- no table or
    # function is ever selected from a payload.
    #
    # Note the exact boundary: these RPCs are SECURITY INVOKER and `service_role`
    # retains direct DML on the staging tables, so the accurate statement is
    # that THE REPOSITORY'S CATALOG WRITE PATH goes through them -- not that
    # they are the only way to write those tables from the database.  Anything
    # that must hold for every writer is a constraint or a trigger.
    #
    # Every identity key is DERIVED from the object's own structural fields
    # (`backend/catalog/payloads.py`), and a caller-supplied key that disagrees
    # is refused rather than trusted.  The memory repository shares these
    # preparers, so the two implementations cannot drift.
    def record_catalog_snapshot(self, run_id: UUID, snapshot: dict[str, Any], *, worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]:
        params = {**self._lease_params(run_id, worker_id, attempt, lease_token),
                  "p_snapshot": prepare_snapshot(snapshot)}
        return self._guarded_rpc("record_catalog_snapshot_guarded", params, "catalog_snapshot")

    def record_catalog_raw_record(self, run_id: UUID, record: dict[str, Any], *, worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]:
        params = {**self._lease_params(run_id, worker_id, attempt, lease_token),
                  "p_record": prepare_raw_record(record)}
        return self._guarded_rpc("record_catalog_raw_record_guarded", params, "catalog_raw_record")

    def activate_catalog_snapshot(self, run_id: UUID, activation: dict[str, Any], *, worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]:
        params = {**self._lease_params(run_id, worker_id, attempt, lease_token), "p_activation": activation}
        return self._guarded_rpc("activate_catalog_snapshot_guarded", params, "catalog_snapshot")

    def record_catalog_candidate(self, run_id: UUID, candidate: dict[str, Any], *, worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]:
        params = {**self._lease_params(run_id, worker_id, attempt, lease_token),
                  "p_candidate": prepare_candidate(candidate)}
        return self._guarded_rpc("record_catalog_candidate_guarded", params, "catalog_candidate")

    def link_catalog_candidate_evidence(self, run_id: UUID, link: dict[str, Any], *, worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]:
        params = {**self._lease_params(run_id, worker_id, attempt, lease_token),
                  "p_link": prepare_evidence_link(link)}
        return self._guarded_rpc("link_catalog_candidate_evidence_guarded", params, "catalog_evidence_link")

    # --- durable catalog reads ------------------------------------------------
    #
    # Explicit column allowlists, never `*`: a column added later joins a read
    # only when a reviewer adds it here.  Every ordering is by columns that are
    # ASCII and unique within their scope (`snapshot_key`, `record_key`,
    # `candidate_key`), so the order a caller receives does not depend on the
    # database's text collation -- the Government identity text is Hebrew, and
    # collation-dependent ordering would make "the same snapshot produces the
    # same ordered tree" false on a differently configured cluster.  Semantic
    # ordering is applied above this layer, over a materialized bounded set.
    CATALOG_SNAPSHOT_COLUMNS = ("id, created_by_run_id, source_family, trust_state, "
                                "resource_id, upstream_version, upstream_version_kind, "
                                "content_sha256, retrieved_at, retrieval_metadata, "
                                "declared_record_count, stored_record_count, "
                                "validation_state, activated_at, snapshot_key, created_at")
    CATALOG_RAW_RECORD_COLUMNS = ("id, snapshot_id, resource_id, upstream_record_id, "
                                  "payload, payload_sha256, record_key, source_locator, "
                                  "created_at")
    CATALOG_CANDIDATE_COLUMNS = ("id, snapshot_id, raw_record_id, manufacturer, "
                                 "commercial_model, model_year_start, model_year_end, "
                                 "official_model_code, trim, identity_dimensions, status, "
                                 "candidate_key, created_at")
    MAX_CATALOG_SNAPSHOT_ROWS = 50
    MAX_CATALOG_RECORD_ROWS = 500
    MAX_CATALOG_CANDIDATE_ROWS = 500

    def list_active_catalog_snapshots(self, source_family: str, *, resource_id: str | None = None, limit: int = MAX_CATALOG_SNAPSHOT_ROWS, capture_scope_key: str | None = None) -> list[dict[str, Any]]:
        """ACTIVE snapshots of one family, newest activation first.

        `activated_at is not null` is the ONLY thing that makes a snapshot
        readable: a pending capture is still being appended to and a failed one
        is the record that a capture was unusable, so neither may answer a
        query.  The filter is a column predicate, not a convention above it.

        SCOPE (scoped catalog PR2). Without `capture_scope_key` the listing is
        of snapshots that declare NO capture scope -- the register -- so a run
        of per-manufacturer snapshots can never answer an unscoped read nor
        push the register out of this bounded window.  With one, it lists only
        snapshots declaring exactly that scope.  Both are database predicates
        on the declaration's JSON path, and `capture_scope_key` is a value, so
        a caller still names no column, table or ordering."""
        bounded = max(1, min(int(limit), self.MAX_CATALOG_SNAPSHOT_ROWS))
        query = (self.client.table("catalog_source_snapshots").select(self.CATALOG_SNAPSHOT_COLUMNS)
                 .eq("source_family", str(source_family)).not_.is_("activated_at", "null"))
        if resource_id is not None:
            query = query.eq("resource_id", str(resource_id))
        if capture_scope_key is None:
            query = query.is_("retrieval_metadata->capture_scope", "null")
        else:
            query = query.eq("retrieval_metadata->capture_scope->>scope_key", str(capture_scope_key))
        return self._many(query.order("activated_at", desc=True).order("snapshot_key").limit(bounded))

    def find_active_catalog_snapshot(self, source_family: str, resource_id: str, snapshot_key: str) -> dict[str, Any] | None:
        """ONE active snapshot named exactly, or None.

        Deliberately not a search of `list_active_catalog_snapshots`: that
        listing is bounded to the NEWEST rows, so resolving an explicit
        `snapshot_key` through it made every active snapshot older than the
        bound unreachable -- a "no such snapshot" for a row sitting active in
        the table.  This is an equality lookup on all four properties, capped
        at one row, so its cost does not grow with the catalog.

        `source_family` and `resource_id` are part of the lookup rather than
        checked afterwards: a key is unique, but a caller asking for a
        Government WLTP snapshot must not be handed a row of another family or
        another resource that happens to carry it."""
        rows = self._many(
            self.client.table("catalog_source_snapshots").select(self.CATALOG_SNAPSHOT_COLUMNS)
            .eq("source_family", str(source_family)).eq("resource_id", str(resource_id))
            .eq("snapshot_key", str(snapshot_key)).not_.is_("activated_at", "null").limit(1))
        return rows[0] if rows else None

    def list_catalog_raw_records(self, snapshot_id: Any, *, limit: int = MAX_CATALOG_RECORD_ROWS, offset: int = 0) -> list[dict[str, Any]]:
        """One snapshot's captured rows, in a stable, collation-free order."""
        bounded = max(1, min(int(limit), self.MAX_CATALOG_RECORD_ROWS))
        start = max(0, int(offset))
        return self._many(
            self.client.table("catalog_raw_records").select(self.CATALOG_RAW_RECORD_COLUMNS)
            .eq("snapshot_id", str(snapshot_id)).order("record_key")
            .range(start, start + bounded - 1))

    def list_catalog_candidates(self, snapshot_id: Any, *, limit: int = MAX_CATALOG_CANDIDATE_ROWS, offset: int = 0) -> list[dict[str, Any]]:
        """One snapshot's candidate readings, in a stable, collation-free order."""
        bounded = max(1, min(int(limit), self.MAX_CATALOG_CANDIDATE_ROWS))
        start = max(0, int(offset))
        return self._many(
            self.client.table("catalog_candidate_variants").select(self.CATALOG_CANDIDATE_COLUMNS)
            .eq("snapshot_id", str(snapshot_id)).order("candidate_key")
            .range(start, start + bounded - 1))

    # --- bounded database-side catalog aggregation (PR3) ---------------------
    #
    # Reviewed RPCs, never a client-built query: the filters, the ordering, the
    # page bound and the exact total all live in
    # `20260916090000_catalog_bounded_candidate_queries.sql`, so a backend
    # release cannot widen them and a caller cannot name a column, a table or
    # an ordering.  The function name is a literal in every case.
    def _read_rpc(self, function: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Call a bounded READ rpc and return its rows, or fail sanitized.

        Deliberately not `_guarded_rpc`: these take no lease, so a stale-lease
        classification would be a lie.  PostgREST details can quote SQL values,
        so the original exception stays an internal cause.
        """
        try:
            data = self.client.rpc(function, params).execute().data
        except Exception as exc:
            raise AppError("REPOSITORY_ERROR", "bounded catalog read failed", 502) from exc
        if data is None:
            return []
        if isinstance(data, dict):
            return [data]
        return [row for row in data if isinstance(row, dict)]

    MAX_CATALOG_AGGREGATE_ROWS = 200

    def _page_params(self, snapshot_id: Any, limit: int, offset: int,
                     allow_incomplete: bool) -> dict[str, Any]:
        return {"p_snapshot_id": str(snapshot_id),
                "p_limit": max(1, min(int(limit), self.MAX_CATALOG_AGGREGATE_ROWS)),
                "p_offset": max(0, int(offset)),
                "p_allow_incomplete": bool(allow_incomplete)}

    def catalog_candidate_manufacturers(self, snapshot_id: Any, *, limit: int = 50, offset: int = 0, allow_incomplete: bool = False) -> list[dict[str, Any]]:
        return self._read_rpc("catalog_candidate_manufacturers",
                              self._page_params(snapshot_id, limit, offset, allow_incomplete))

    def catalog_candidate_models(self, snapshot_id: Any, *, manufacturer: str, limit: int = 50, offset: int = 0, allow_incomplete: bool = False) -> list[dict[str, Any]]:
        return self._read_rpc("catalog_candidate_models",
                              {**self._page_params(snapshot_id, limit, offset, allow_incomplete),
                               "p_manufacturer": str(manufacturer)})

    def catalog_candidate_model_years(self, snapshot_id: Any, *, manufacturer: str, commercial_model: str, limit: int = 50, offset: int = 0, allow_incomplete: bool = False) -> list[dict[str, Any]]:
        return self._read_rpc("catalog_candidate_model_years",
                              {**self._page_params(snapshot_id, limit, offset, allow_incomplete),
                               "p_manufacturer": str(manufacturer),
                               "p_commercial_model": str(commercial_model)})

    def catalog_candidate_variant_page(self, snapshot_id: Any, *, manufacturer: str | None = None, commercial_model: str | None = None, model_year: int | None = None, official_model_code: str | None = None, trim: str | None = None, identity_dimensions: dict[str, Any] | None = None, status: str | None = None, limit: int = 50, offset: int = 0, allow_incomplete: bool = False) -> list[dict[str, Any]]:
        return self._read_rpc("catalog_candidate_variant_page", {
            **self._page_params(snapshot_id, limit, offset, allow_incomplete),
            "p_manufacturer": None if manufacturer is None else str(manufacturer),
            "p_commercial_model": None if commercial_model is None else str(commercial_model),
            "p_model_year": None if model_year is None else int(model_year),
            "p_official_model_code": None if official_model_code is None else str(official_model_code),
            "p_trim": None if trim is None else str(trim),
            "p_identity_dimensions": dict(identity_dimensions) if identity_dimensions else None,
            "p_status": None if status is None else str(status)})

    def catalog_run_pending_promotions(self, run_id: UUID, tool_operation: str, *, limit: int = 25) -> list[dict[str, Any]]:
        """What one RUN still has to promote, reconstructed from durable state.

        One bounded READ. It takes no lease because it writes nothing, and it
        is what makes the promotion path survive a worker restart: the
        candidate a claim is evidence for is derived from rows the server
        itself wrote, never from a ledger in a process that may be gone.
        """
        return self._read_rpc("catalog_run_pending_promotions",
                              {"p_run_id": str(run_id),
                               "p_tool_operation": str(tool_operation),
                               "p_limit": max(0, int(limit))})

    def catalog_snapshot_candidate_diff(self, previous_snapshot_id: Any, snapshot_id: Any, *, limit: int = MAX_DIFF_ITEMS, allow_incomplete: bool = False) -> list[dict[str, Any]]:
        """What changed between two snapshots, compared INSIDE the database.

        The counts come back exact for the whole resource; only the delta list
        is bounded. Nothing about either snapshot is read into this process to
        produce them, which is what keeps a ~101 000-row comparison from being
        an unbounded read.
        """
        return self._read_rpc("catalog_snapshot_candidate_diff", {
            "p_previous_snapshot_id": None if previous_snapshot_id is None
                                      else str(previous_snapshot_id),
            "p_snapshot_id": str(snapshot_id),
            "p_limit": max(0, min(int(limit), MAX_DIFF_ITEMS)),
            "p_allow_incomplete": bool(allow_incomplete)})

    def catalog_raw_record_by_upstream_id(self, snapshot_id: Any, upstream_record_id: str, *, allow_incomplete: bool = False) -> dict[str, Any] | None:
        rows = self._read_rpc("catalog_raw_record_by_upstream_id",
                              {"p_snapshot_id": str(snapshot_id),
                               "p_upstream_record_id": str(upstream_record_id),
                               "p_allow_incomplete": bool(allow_incomplete)})
        return rows[0] if rows else None

    # --- field-level canonical promotion (PR3) -------------------------------
    #
    # ONE lease-guarded RPC, exactly like every other durable catalog write.
    # The canonical identity and every field's provenance are created in ONE
    # transaction; the database refuses a canonical row whose stated fields are
    # not all covered by verified field provenance, whichever path wrote it.
    def promote_catalog_variant(self, run_id: UUID, promotion: dict[str, Any], *, worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]:
        params = {**self._lease_params(run_id, worker_id, attempt, lease_token),
                  "p_promotion": prepare_promotion(promotion)}
        return self._guarded_rpc("promote_catalog_variant_guarded", params, "catalog_promotion")

    CANONICAL_VARIANT_COLUMNS = ("variant_id, model_id, canonical_key, manufacturer, "
                                 "commercial_model, model_canonical_key, "
                                 "promoted_from_candidate_id, promoted_from_verdict_id, "
                                 "model_year_start, model_year_end, official_model_code, "
                                 "trim, identity_dimensions, field_revisions, "
                                 "promoted_at, revised_at")
    CANONICAL_PROVENANCE_COLUMNS = ("id, model_id, variant_id, field_key, field_value, "
                                    "revision, candidate_id, evidence_link_id, snapshot_id, "
                                    "source_id, claim_id, verdict_id, run_id, worker_id, "
                                    "attempt, source_version, source_version_kind, "
                                    "record_locator, promotion_key, created_at")
    MAX_CANONICAL_PROVENANCE_ROWS = 200

    def get_canonical_catalog_variant(self, canonical_key: str) -> dict[str, Any] | None:
        """ONE canonical variant's CURRENT state, from the authoritative view.

        `catalog_canonical_variant_current` is what "the current canonical
        value" means: it is assembled from the append-only field provenance, so
        a later revision of one field is visible here without any canonical row
        ever having been rewritten."""
        rows = self._many(
            self.client.table("catalog_canonical_variant_current")
            .select(self.CANONICAL_VARIANT_COLUMNS)
            .eq("canonical_key", str(canonical_key)).limit(1))
        return rows[0] if rows else None

    def list_canonical_field_provenance(self, variant_id: Any, *, limit: int = MAX_CANONICAL_PROVENANCE_ROWS) -> list[dict[str, Any]]:
        """Every promoted fact of one canonical variant, oldest revision first."""
        bounded = max(1, min(int(limit), self.MAX_CANONICAL_PROVENANCE_ROWS))
        return self._many(
            self.client.table("catalog_canonical_field_provenance")
            .select(self.CANONICAL_PROVENANCE_COLUMNS)
            .eq("variant_id", str(variant_id))
            .order("field_key").order("revision").limit(bounded))

    # --- the bounded canonical LISTING (CODE-3) ------------------------------
    #
    # The only canonical read that was missing.  `get_canonical_catalog_variant`
    # answers for one named key, which cannot answer "what is in the catalog";
    # this does, one explicitly bounded page at a time.
    #
    # No migration: the authoritative view already assembles exactly the shape a
    # reviewer needs, and every degree of freedom a caller could otherwise have
    # is spelled out HERE as server data -- the column list, the filter columns,
    # the ordering and the page bound are all literals in this method.  A caller
    # passes values, never columns, never an ordering and never a table.
    #
    # Ordering is `canonical_key`: `cv1.` plus 32 hex characters
    # (`backend/catalog/keys.py`), so it is unique, ASCII and therefore
    # collation-free -- the same reason every other catalog listing orders by a
    # derived key rather than by the Hebrew identity text.
    MAX_CANONICAL_LIST_ROWS = 100

    def list_canonical_catalog_variants(self, *, manufacturer: str | None = None,
                                        commercial_model: str | None = None,
                                        model_year: int | None = None,
                                        canonical_key: str | None = None,
                                        limit: int = 50,
                                        offset: int = 0) -> list[dict[str, Any]]:
        """One bounded canonical page, each row carrying the EXACT total.

        Mirrors the row contract of the PR3 aggregations rather than inventing a
        second one: `total_count` travels on every row, and a page past the last
        matching row returns ONE count row whose item columns are all null --
        the shape `government.query.is_count_row` already recognises.  That is
        what lets an out-of-range page report the real total instead of zero.

        The total is PostgREST's `count=exact`, so it is the database's own
        count over every matching row, never a guess from the page length.  When
        the server declines to report one, `total_count` is None -- "not
        reported", which the layer above carries as unknown rather than as zero.
        """
        bounded = max(1, min(int(limit), self.MAX_CANONICAL_LIST_ROWS))
        start = max(0, int(offset))
        query = (self.client.table("catalog_canonical_variant_current")
                 .select(self.CANONICAL_VARIANT_COLUMNS, count=CountMethod.exact))
        if manufacturer is not None:
            query = query.eq("manufacturer", str(manufacturer))
        if commercial_model is not None:
            query = query.eq("commercial_model", str(commercial_model))
        if canonical_key is not None:
            query = query.eq("canonical_key", str(canonical_key))
        if model_year is not None:
            # A variant states a RANGE; the filter selects the variants whose
            # range contains the year, exactly as `p_model_year` does for
            # candidates in `catalog_candidate_variant_page`.
            year = int(model_year)
            query = query.lte("model_year_start", year).gte("model_year_end", year)
        rows, total = self._many_with_count(
            query.order("canonical_key").range(start, start + bounded - 1))
        if not rows:
            empty = {name.strip(): None for name in self.CANONICAL_VARIANT_COLUMNS.split(",")}
            return [{**empty, "total_count": total}]
        return [{**row, "total_count": total} for row in rows]

    # --- mapping plans (20260922000100_catalog_work_scopes.sql) ---------------
    #
    # Two reviewed writers and three bounded reads. The writers derive the
    # digest, the project and the revision number in the database and re-check
    # membership there; this layer passes values, never a column, a table or an
    # ordering. Every refusal the SQL raises by name is mapped to the same code
    # the in-memory mirror raises, and anything else becomes one sanitized
    # classification -- a PostgREST message can quote SQL values.
    WORK_SCOPE_COLUMNS = ("id, project_id, conversation_id, created_by, status, "
                          "head_revision, head_digest, created_at, updated_at, closed_at")
    WORK_SCOPE_REVISION_COLUMNS = ("id, work_scope_id, revision, scope_text, digest, "
                                   "input_kind, instruction, notes, created_by, created_at")
    MAX_WORK_SCOPE_REVISION_ROWS = 50
    _WORK_SCOPE_REFUSALS = (
        ("WORK_SCOPE_STALE", "the plan changed since it was read; reload it and try again", 409),
        ("WORK_SCOPE_OPEN_EXISTS", "this conversation already has an open mapping plan", 409),
        ("WORK_SCOPE_NOT_EDITABLE", "the mapping plan is not editable", 409),
        ("WORK_SCOPE_WORKFLOW_UNSUPPORTED", "this project's engine does not read a mapping plan", 409),
        ("WORK_SCOPE_REVISION_INVALID", "invalid work scope revision", 422),
    )

    def _work_scope_write(self, call: Any, identifier: str) -> dict[str, Any]:
        """Run one writer RPC and map what it refuses.

        `call` is the RPC itself, spelled out at the method below with its
        literal name, so the release inventory (`scripts/release/
        release_inventory.py`) sees every writer as the runtime dependency it
        is.
        """
        try:
            data = call().execute().data
        except Exception as exc:
            message = str(exc)
            if "WORK_SCOPE_CONVERSATION_NOT_FOUND" in message:
                raise NotFoundError("conversation", identifier) from None
            if "WORK_SCOPE_NOT_FOUND" in message:
                raise NotFoundError("work_scope", identifier) from None
            for code, safe, status in self._WORK_SCOPE_REFUSALS:
                if code in message:
                    raise AppError(code, safe, status) from None
            raise AppError("REPOSITORY_ERROR", "mapping plan write failed", 502) from None
        if isinstance(data, list):
            data = data[0] if data else None
        if not isinstance(data, dict) or not isinstance(data.get("work_scope"), dict):
            raise AppError("REPOSITORY_ERROR", "mapping plan write returned no row", 502)
        return data

    def create_work_scope(self, conversation_id: UUID, created_by: UUID, revision: dict[str, Any]) -> dict[str, Any]:
        return self._work_scope_write(lambda: self.client.rpc("create_work_scope", {
            "p_conversation_id": str(conversation_id), "p_created_by": str(created_by),
            "p_revision": revision}), str(conversation_id))

    def revise_work_scope(self, work_scope_id: UUID, expected_revision: int, expected_digest: str, created_by: UUID, revision: dict[str, Any]) -> dict[str, Any]:
        return self._work_scope_write(lambda: self.client.rpc("revise_work_scope", {
            "p_work_scope_id": str(work_scope_id), "p_expected_revision": int(expected_revision),
            "p_expected_digest": str(expected_digest), "p_created_by": str(created_by),
            "p_revision": revision}), str(work_scope_id))

    def get_work_scope(self, work_scope_id: UUID) -> dict[str, Any] | None:
        """ONE plan by id. Membership is the caller's check, made before this."""
        rows = self._many(self.client.table("catalog_work_scopes").select(self.WORK_SCOPE_COLUMNS)
                          .eq("id", str(work_scope_id)).limit(1))
        return rows[0] if rows else None

    def open_work_scope(self, conversation_id: UUID) -> dict[str, Any] | None:
        """The conversation's open plan -- at most one, by a partial unique index."""
        rows = self._many(self.client.table("catalog_work_scopes").select(self.WORK_SCOPE_COLUMNS)
                          .eq("conversation_id", str(conversation_id))
                          .is_("closed_at", "null").limit(1))
        return rows[0] if rows else None

    def list_work_scope_revisions(self, work_scope_id: UUID, *, limit: int = 11) -> list[dict[str, Any]]:
        """One plan's revisions, newest first, bounded."""
        bounded = max(1, min(int(limit), self.MAX_WORK_SCOPE_REVISION_ROWS))
        return self._many(self.client.table("catalog_work_scope_revisions")
                          .select(self.WORK_SCOPE_REVISION_COLUMNS)
                          .eq("work_scope_id", str(work_scope_id))
                          .order("revision", desc=True).limit(bounded))

    def catalog_canonical_manufacturer_coverage(self, manufacturers: list[str]) -> list[dict[str, Any]]:
        """Exact canonical variant counts per register marque, plus the total."""
        return self._read_rpc("catalog_canonical_manufacturer_coverage",
                              {"p_manufacturers": [str(name) for name in manufacturers]})

    # --- plan preparation (20260923000100_catalog_work_scope_preparation.sql) --
    #
    # One lease-guarded writer (an OPERATOR CAPTURE run only), one binding
    # compare-and-set and two reads. Every refusal the SQL raises by name maps
    # to the code the in-memory mirror raises; anything else is one sanitized
    # classification, because a PostgREST message can quote SQL values.
    _WORK_SCOPE_PREPARATION_REFUSALS = (
        ("WORK_SCOPE_PREPARATION_RUN_INVALID",
         "only an operator capture run prepares a mapping plan", 409),
        ("WORK_SCOPE_PREPARATION_INVALID", "invalid mapping plan preparation", 422),
        ("WORK_SCOPE_UNIT_SNAPSHOT_INVALID",
         "a unit's snapshot is not that marque's usable scoped capture", 422),
        ("WORK_SCOPE_ALREADY_PREPARED", "this plan revision was already prepared differently", 409),
        ("WORK_SCOPE_BATCH_IN_PROGRESS", "another batch of this plan is still running", 409),
        ("WORK_SCOPE_BATCH_ALREADY_COMPLETED", "this batch already completed", 409),
        ("WORK_SCOPE_BATCH_RUN_TAKEN", "this run is already bound to another batch", 409),
        ("WORK_SCOPE_BATCH_RUN_INVALID", "this run cannot execute this batch", 422),
        # The binding's continuation rules (20260924000100).
        ("WORK_SCOPE_PAUSED", "the mapping plan is paused; resume it before starting a batch", 409),
        ("WORK_SCOPE_BATCH_NOT_NEXT", "only the next batch of the plan can start", 409),
    ) + _WORK_SCOPE_REFUSALS

    def _work_scope_preparation_call(self, call: Any, identifier: str, *, guarded: bool) -> Any:
        """Run one preparation-family RPC and map what it refuses.

        `call` is the RPC itself, spelled out at the method below with its
        literal name, so the release inventory sees each one."""
        try:
            data = call().execute().data
        except Exception as exc:
            if guarded and self._is_stale_lease_error(exc):
                raise AppError("RUN_LEASE_LOST", "run lease is held by another worker", 409) from None
            message = str(exc)
            if "WORK_SCOPE_BATCH_NOT_FOUND" in message:
                raise NotFoundError("work_scope_batch", identifier) from None
            if "WORK_SCOPE_NOT_FOUND" in message:
                raise NotFoundError("work_scope", identifier) from None
            for code, safe, status in self._WORK_SCOPE_PREPARATION_REFUSALS:
                if code in message:
                    raise AppError(code, safe, status) from None
            raise AppError("REPOSITORY_ERROR", "mapping plan preparation failed", 502) from None
        if isinstance(data, list):
            data = data[0] if data else None
        return data

    def get_work_scope_revision(self, work_scope_id: UUID, revision: int) -> dict[str, Any] | None:
        """ONE exact revision of one plan, or None. An equality read, not a search."""
        rows = self._many(self.client.table("catalog_work_scope_revisions")
                          .select(self.WORK_SCOPE_REVISION_COLUMNS + ", scope")
                          .eq("work_scope_id", str(work_scope_id))
                          .eq("revision", int(revision)).limit(1))
        return rows[0] if rows else None

    def prepare_work_scope_queue(self, run_id: UUID, preparation: dict[str, Any], *, worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]:
        params = {**self._lease_params(run_id, worker_id, attempt, lease_token),
                  "p_preparation": preparation}
        data = self._work_scope_preparation_call(
            lambda: self.client.rpc("prepare_work_scope_queue", params),
            str(preparation.get("work_scope_id") if isinstance(preparation, dict) else ""),
            guarded=True)
        if not isinstance(data, dict) or not isinstance(data.get("preparation"), dict):
            raise AppError("REPOSITORY_ERROR", "mapping plan preparation returned no row", 502)
        return data

    def bind_work_scope_batch_run(self, batch_id: UUID, run_id: UUID, expected_revision: int, expected_digest: str, bound_by: UUID) -> dict[str, Any]:
        data = self._work_scope_preparation_call(
            lambda: self.client.rpc("bind_work_scope_batch_run", {
                "p_batch_id": str(batch_id), "p_run_id": str(run_id),
                "p_expected_revision": int(expected_revision),
                "p_expected_digest": str(expected_digest), "p_bound_by": str(bound_by)}),
            str(batch_id), guarded=False)
        if not isinstance(data, dict) or not data.get("id"):
            raise AppError("REPOSITORY_ERROR", "batch binding returned no row", 502)
        return data

    def work_scope_batch_for_run(self, run_id: UUID) -> dict[str, Any] | None:
        """The run's exact batch and its items, or None when it is unbound."""
        data = self._work_scope_preparation_call(
            lambda: self.client.rpc("work_scope_batch_for_run", {"p_run_id": str(run_id)}),
            str(run_id), guarded=False)
        if data is None:
            return None
        if not isinstance(data, dict) or not isinstance(data.get("batch"), dict) \
                or not isinstance(data.get("items"), list):
            raise AppError("REPOSITORY_ERROR", "batch read returned an unreadable row", 502)
        return data

    # -- batch runs, pause / resume and progress (scoped catalog PR3) ----------
    #
    # `20260924000100_catalog_work_scope_batch_runs.sql`. Starting a batch is ONE
    # RPC that wraps the run creator and the binding in one transaction; the
    # refusals it can answer with are the plan's, the binding's and the run
    # creator's own, each mapped to the same code the API already speaks.
    _WORK_SCOPE_BATCH_REFUSALS = (
        ("WORK_SCOPE_BATCH_IDEMPOTENCY_REQUIRED", "starting a batch requires an idempotency key", 422),
        ("WORK_SCOPE_CONTROL_OUT_OF_SEQUENCE", "the plan's pause state changed; reload it and try again", 409),
        ("WORK_SCOPE_CONTROL_INVALID", "invalid mapping plan control", 422),
    ) + _WORK_SCOPE_PREPARATION_REFUSALS
    _RUN_CREATION_REFUSALS = (
        ("USER_CONCURRENCY_LIMIT", "too many active runs for this user", 429),
        ("PROJECT_CONCURRENCY_LIMIT", "too many active runs for this project", 429),
        ("IDEMPOTENCY_CONFLICT", "idempotency key was already used with a different payload", 409),
        ("RUN_IDENTITY_WORKFLOW_DRIFT", "project workflow changed before the run could be created", 409),
        ("RUN_IDENTITY_INVALID", "run identity was rejected", 409),
        ("RUN_IDENTITY_REQUIRED", "run identity was rejected", 409),
    )

    def _work_scope_batch_call(self, call: Any, identifier: str) -> Any:
        """Run one batch-family RPC and map what it refuses to a static code.

        `call` is the RPC itself, spelled out at the method below with its
        literal name, so the release inventory sees each one."""
        try:
            data = call().execute().data
        except Exception as exc:
            message = str(exc)
            if "WORK_SCOPE_BATCH_NOT_FOUND" in message:
                raise NotFoundError("work_scope_batch", identifier) from None
            if "WORK_SCOPE_NOT_FOUND" in message:
                raise NotFoundError("work_scope", identifier) from None
            for code, safe, status in self._WORK_SCOPE_BATCH_REFUSALS + self._RUN_CREATION_REFUSALS:
                if code in message:
                    raise AppError("RUN_IDENTITY_INVALID" if code == "RUN_IDENTITY_REQUIRED" else code,
                                   safe, status) from None
            raise AppError("REPOSITORY_ERROR", "mapping plan batch request failed", 502) from None
        if isinstance(data, list):
            data = data[0] if data else None
        return data

    def create_work_scope_batch_run(self, work_scope_id: UUID, batch_id: UUID, expected_revision: int, expected_digest: str, *, run_id: UUID, run_identity: dict[str, Any], content: str, metadata: dict[str, Any], requested_by: UUID, idempotency_key: str, request_fingerprint: str, max_user_active: int | None = None, max_project_active: int | None = None) -> dict[str, Any]:
        """ONE batch run: message, queued run, immutable identity and binding,
        committed together or not at all. A replay answers `created: false`."""
        data = self._work_scope_batch_call(
            lambda: self.client.rpc("create_work_scope_batch_run", {
                "p_work_scope_id": str(work_scope_id), "p_batch_id": str(batch_id),
                "p_expected_revision": int(expected_revision),
                "p_expected_digest": str(expected_digest),
                "p_run_id": str(run_id), "p_run_identity": run_identity,
                "p_content": content, "p_metadata": metadata,
                "p_requested_by": str(requested_by), "p_idempotency_key": idempotency_key,
                "p_request_fingerprint": request_fingerprint,
                "p_max_user_active": max_user_active,
                "p_max_project_active": max_project_active}),
            str(work_scope_id))
        if not isinstance(data, dict) or not isinstance(data.get("run"), dict) \
                or not isinstance(data.get("binding"), dict):
            raise AppError("REPOSITORY_ERROR", "batch run creation returned no row", 502)
        return data

    def set_work_scope_paused(self, work_scope_id: UUID, paused: bool, requested_by: UUID) -> dict[str, Any]:
        """Pause or resume one plan: an append-only control, written once."""
        data = self._work_scope_batch_call(
            lambda: self.client.rpc("set_work_scope_paused", {
                "p_work_scope_id": str(work_scope_id), "p_paused": bool(paused),
                "p_requested_by": str(requested_by)}),
            str(work_scope_id))
        if not isinstance(data, dict) or not isinstance(data.get("paused"), bool):
            raise AppError("REPOSITORY_ERROR", "mapping plan control returned no row", 502)
        return data

    def work_scope_progress(self, work_scope_id: UUID) -> dict[str, Any] | None:
        """The plan's progress, derived by the database; None when it does not exist."""
        data = self._work_scope_batch_call(
            lambda: self.client.rpc("work_scope_progress", {"p_work_scope_id": str(work_scope_id)}),
            str(work_scope_id))
        if data is None:
            return None
        if not isinstance(data, dict) or not data.get("work_scope_id"):
            raise AppError("REPOSITORY_ERROR", "mapping plan progress returned an unreadable row", 502)
        return data

    def record_conflict_resolution(self, run_id: UUID, resolution: dict[str, Any], *, worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]:
        params = {**self._lease_params(run_id, worker_id, attempt, lease_token), "p_resolution": resolution}
        return self._guarded_rpc("record_conflict_resolution_guarded", params, "conflict_resolution")

    def patch_run_blackboard_evidence(self, run_id: UUID, summary: dict[str, Any], *, worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]:
        params = {**self._lease_params(run_id, worker_id, attempt, lease_token), "p_summary": summary}
        return self._guarded_rpc("patch_run_blackboard_evidence_guarded", params, "run_blackboard")

    def record_run_invocation(self, run_id: UUID, invocation: dict[str, Any]) -> dict[str, Any]:
        payload = {"run_id": str(run_id), "launcher": invocation.get("mode"), "execution_name": invocation.get("execution"), "payload": invocation}
        return self._single(self.client.table("run_invocations").insert(payload).select("*"), "run_invocation", "new")

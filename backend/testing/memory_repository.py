"""In-memory Repository implementation for isolated E2E stacks.

Test-only: mirrors the SupabaseRepository authorization semantics
(membership scoping, 404 without existence disclosure, idempotency,
launch state) against process-local dictionaries. Never used by
production entrypoints.
"""

from __future__ import annotations

import threading
import secrets
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from backend.catalog.contracts import (CANDIDATE_STATUSES, CATALOG_SOURCE_FAMILIES,
                                       stated_identity_dimensions, trust_state_for)
from backend.errors import AppError, NotFoundError
from backend.runtime import RUN_STATES, InvalidTransition, validate_transition
from backend.schemas import normalize_conversation_title


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
        # The durable catalog namespace (PR1). Keyed by the same idempotency
        # identities the guarded RPCs use, so a replay collapses here exactly
        # as it does in PostgreSQL.
        self.catalog_snapshots: dict[str, dict[str, Any]] = {}       # snapshot_key
        self.catalog_raw_records: dict[tuple[str, str], dict[str, Any]] = {}
        self.catalog_candidates: dict[tuple[str, str], dict[str, Any]] = {}
        self.catalog_evidence_links: dict[tuple[str, str], dict[str, Any]] = {}
        # Canonical state. Present so a test can PROVE it stays empty; there
        # is deliberately no method anywhere that appends to either list,
        # mirroring the database, where service_role holds SELECT only.
        self.catalog_models: list[dict[str, Any]] = []
        self.catalog_model_variants: list[dict[str, Any]] = []
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
        # Mirrors production: conversations.title is NOT NULL, never store None.
        conversation = {"id": str(uuid4()), "project_id": str(project_id), "title": normalize_conversation_title(title), "created_at": _now(), "updated_at": _now()}
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

    def append_run_event(self, run_id: UUID, event_type: str, payload: dict[str, Any], worker_id: str | None = None, attempt: int | None = None, lease_token: str | None = None) -> dict[str, Any]:
        with self.lock:
            if worker_id is not None:
                run = self.runs.get(str(run_id))
                if run is None:
                    raise NotFoundError("run", str(run_id))
                self._assert_active_lease(run, worker_id, attempt, lease_token)
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

    def _assert_active_lease(self, run: dict[str, Any], expected_worker_id: str | None = None, expected_attempt: int | None = None, lease_token: str | None = None) -> None:
        if expected_worker_id is not None and run.get("worker_id") != expected_worker_id:
            raise AppError("RUN_TRANSITION_CONFLICT", "run lease is no longer held", 409)
        if expected_attempt is not None and int(run.get("attempt") or 0) != int(expected_attempt):
            raise AppError("RUN_TRANSITION_CONFLICT", "run attempt is no longer active", 409)
        if lease_token is not None and run.get("lease_token") != lease_token:
            raise AppError("RUN_TRANSITION_CONFLICT", "run lease token is no longer active", 409)
        if (expected_worker_id is not None or expected_attempt is not None or lease_token is not None) and run.get("lease_expires_at") and run["lease_expires_at"] <= _now():
            raise AppError("RUN_TRANSITION_CONFLICT", "run lease has expired", 409)

    def transition_run(self, run_id: UUID, status: str, expected_worker_id: str | None = None, expected_attempt: int | None = None, expected_lease_token: str | None = None, **fields: Any) -> dict[str, Any]:
        with self.lock:
            run = self.runs.get(str(run_id))
            if run is None:
                raise NotFoundError("run", str(run_id))
            self._assert_active_lease(run, expected_worker_id, expected_attempt, expected_lease_token)
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

    def mark_run_failed(self, run_id: UUID, code: str, message: str, worker_id: str | None = None, attempt: int | None = None, lease_token: str | None = None) -> dict[str, Any]:
        return self.transition_run(run_id, "failed", expected_worker_id=worker_id, expected_attempt=attempt, expected_lease_token=lease_token, error={"code": code, "message": message}, finished_at=_now())

    def mark_run_complete(self, run_id: UUID, output: dict[str, Any], worker_id: str | None = None, attempt: int | None = None, lease_token: str | None = None) -> dict[str, Any]:
        return self.transition_run(run_id, "completed", expected_worker_id=worker_id, expected_attempt=attempt, expected_lease_token=lease_token, output=output, error=None, finished_at=_now())

    def record_run_invocation(self, run_id: UUID, invocation: dict[str, Any]) -> dict[str, Any]:
        row = {"id": str(uuid4()), "run_id": str(run_id), **invocation}
        self.invocations.append(row)
        return dict(row)

    def update_run_usage(self, run_id: UUID, usage: dict[str, Any], worker_id: str | None = None, attempt: int | None = None, lease_token: str | None = None) -> dict[str, Any]:
        run = self.runs[str(run_id)]
        if worker_id is not None:
            self._assert_active_lease(run, worker_id, attempt, lease_token)
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
    CLAIMABLE_STATES = {"queued", "launching", "waiting", "cancellation_requested", "starting", "running"}

    def claim_run(self, run_id: UUID, worker_id: str, lease_seconds: int = 300) -> dict[str, Any]:
        # Parity with public.claim_run_lease: a terminal run (or one whose
        # lease another worker still holds unexpired) matches no rows and
        # surfaces as RUN_ALREADY_CLAIMED, exactly like production.
        from datetime import datetime, UTC, timedelta
        with self.lock:
            run = self.get_run(run_id)
            if run["status"] not in self.CLAIMABLE_STATES:
                raise AppError("RUN_ALREADY_CLAIMED", "run is already claimed by another worker", 409)
            holder = run.get("worker_id")
            expires = run.get("lease_expires_at")
            expired = not expires or expires < _now()
            if holder and holder != worker_id and not expired:
                raise AppError("RUN_ALREADY_CLAIMED", "run is already claimed by another worker", 409)
            target = run["status"] if run["status"] == "cancellation_requested" else "starting"
            attempt = int(run.get("attempt") or 1)
            if holder and holder != worker_id and expired:
                attempt += 1
            # The production claim is a single-statement CAS that does not
            # consult the transition table (e.g. an expired 'running' run is
            # reclaimed back to 'starting'); mirror that exactly.
            stored = self.runs[str(run_id)]
            stored.update({
                "status": target, "worker_id": worker_id, "attempt": attempt,
                "lease_token": secrets.token_urlsafe(32),
                "started_at": run.get("started_at") or _now(),
                "lease_expires_at": (datetime.now(UTC) + timedelta(seconds=lease_seconds)).isoformat(),
                "updated_at": _now(),
            })
            return dict(stored)

    def heartbeat(self, run_id: UUID, worker_id: str, lease_seconds: int = 300, attempt: int | None = None, lease_token: str | None = None) -> dict[str, Any]:
        from datetime import datetime, UTC, timedelta
        run = self.get_run(run_id)
        self._assert_active_lease(run, worker_id, attempt, lease_token)
        run["last_heartbeat_at"] = _now()
        run["lease_expires_at"] = (datetime.now(UTC) + timedelta(seconds=lease_seconds)).isoformat()
        return dict(run)

    def latest_checkpoint(self, run_id: UUID, workflow_key: str | None = None) -> dict[str, Any] | None:
        matches = [
            checkpoint for checkpoint in self.checkpoints
            if str(checkpoint.get("run_id")) == str(run_id)
            and (workflow_key is None or checkpoint.get("workflow_key") == workflow_key)
        ]
        return dict(matches[-1]) if matches else None

    def save_checkpoint(self, checkpoint: dict[str, Any], worker_id: str | None = None, attempt: int | None = None, lease_token: str | None = None) -> dict[str, Any]:
        run_id = checkpoint.get("run_id")
        if run_id:
            run = self.runs[str(run_id)]
            self._assert_active_lease(
                run,
                worker_id if worker_id is not None else checkpoint.get("worker_id"),
                attempt if attempt is not None else checkpoint.get("attempt"),
                lease_token if lease_token is not None else checkpoint.get("lease_token"),
            )
        self.checkpoints.append(checkpoint)
        return dict(checkpoint)

    def reserve_model_call_budget(self, run_id: UUID, call_seq: int, user_id: str | None, project_id: str | None, amount: float, daily_user_limit: float | None, daily_project_limit: float | None, worker_id: str | None = None, attempt: int | None = None, lease_token: str | None = None) -> dict[str, Any]:
        with self.lock:
            if worker_id is not None:
                run = self.runs.get(str(run_id))
                if run is None:
                    raise NotFoundError("run", str(run_id))
                self._assert_active_lease(run, worker_id, attempt, lease_token)
            user_spend = self.sum_daily_ledger_cost(user_id=user_id) if user_id else 0.0
            project_spend = self.sum_daily_ledger_cost(project_id=project_id) if project_id else 0.0
            status, reason = "reserved", None
            if user_id and daily_user_limit is not None and user_spend + amount > daily_user_limit:
                status, reason = "rejected", "DAILY_USER_BUDGET_REACHED"
            elif project_id and daily_project_limit is not None and project_spend + amount > daily_project_limit:
                status, reason = "rejected", "DAILY_PROJECT_BUDGET_REACHED"
            return self.append_usage_ledger({"run_id": str(run_id), "call_seq": call_seq, "user_id": user_id, "project_id": project_id, "decision": status, "status": status, "estimated_cost": amount, "rejection_reason": reason})

    def settle_model_call_budget(self, reservation_id: str, actual_cost: float, status: str = "settled", rejection_reason: str | None = None, run_id: UUID | None = None, worker_id: str | None = None, attempt: int | None = None, lease_token: str | None = None) -> dict[str, Any]:
        with self.lock:
            if worker_id is not None and run_id is not None:
                run = self.runs.get(str(run_id))
                if run is None:
                    raise NotFoundError("run", str(run_id))
                self._assert_active_lease(run, worker_id, attempt, lease_token)
            for row in getattr(self, "usage_ledger", []):
                if str(row.get("id")) == str(reservation_id):
                    # A lease for one run must never settle another run's
                    # reservation (parity with settle_model_call_budget_guarded).
                    if run_id is not None and str(row.get("run_id")) != str(run_id):
                        raise AppError("RESERVATION_RUN_MISMATCH", "reservation does not belong to this run", 409)
                    if row.get("settled"):
                        raise AppError("BUDGET_RESERVATION_SETTLED", "reservation already settled", 409)
                    row["settled"] = True
                    settled = {**row, "id": len(self.usage_ledger) + 1, "decision": status, "status": status, "actual_cost": actual_cost, "rejection_reason": rejection_reason}
                    self.usage_ledger.append(settled)
                    return dict(settled)
            raise NotFoundError("model_call_budget_reservation", reservation_id)

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

    # --- durable catalog staging (PR1) ---------------------------------------
    #
    # These mirror the guarded RPCs of
    # `20260914200000_catalog_evidence_foundation.sql`, and mirror the parts
    # that MATTER for a test to be meaningful: the lease guard, the
    # idempotency identity, the fail-closed replay conflict, the activation
    # gate, and the cross-run / cross-snapshot / unverified-source refusals. A
    # mirror that accepted everything would let a unit test pass against
    # behavior PostgreSQL rejects.

    _CATALOG_SNAPSHOT_IDENTITY = ("source_family", "resource_id", "upstream_version",
                                  "upstream_version_kind", "content_sha256",
                                  "declared_record_count")
    _CATALOG_RECORD_IDENTITY = ("upstream_record_id", "payload_sha256", "payload")
    _CATALOG_CANDIDATE_IDENTITY = ("raw_record_id", "manufacturer", "commercial_model",
                                   "model_year_start", "model_year_end",
                                   "official_model_code", "trim", "identity_dimensions")
    _CATALOG_LINK_IDENTITY = ("source_id", "claim_id", "verdict_id", "record_locator",
                              "source_version", "source_version_kind")

    def _catalog_lease(self, run_id: UUID, worker_id: str, attempt: int, lease_token: str) -> None:
        run = self.runs.get(str(run_id))
        if run is None:
            raise NotFoundError("run", str(run_id))
        self._assert_active_lease(run, worker_id, attempt, lease_token)

    @staticmethod
    def _catalog_replay(existing: dict[str, Any], incoming: dict[str, Any],
                        identity: tuple[str, ...], kind: str) -> dict[str, Any]:
        """Return the existing row for an exact replay, or fail closed.

        Same rule as the RPCs: one idempotency identity may only ever mean one
        thing. A replay carrying different content is a caller bug, never a
        silent overwrite and never a silent no-op.
        """
        if any(existing.get(field) != incoming.get(field) for field in identity):
            raise AppError(f"CATALOG_{kind}_IDEMPOTENCY_CONFLICT",
                           f"catalog {kind.lower()} idempotency conflict", 409)
        return dict(existing)

    def record_catalog_snapshot(self, run_id: UUID, snapshot: dict[str, Any], *,
                                worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]:
        with self.lock:
            self._catalog_lease(run_id, worker_id, attempt, lease_token)
            family = snapshot.get("source_family")
            if family not in CATALOG_SOURCE_FAMILIES:
                raise AppError("CATALOG_SNAPSHOT_INVALID", "unknown catalog source family", 400)
            # Derived, never accepted: the legacy catalog cannot be submitted
            # as evidence even by a trusted caller.
            trust = trust_state_for(family)
            if snapshot.get("trust_state") not in (None, trust):
                raise AppError("CATALOG_SNAPSHOT_INVALID",
                               "catalog trust state is pinned to the source family", 400)
            if "activated_at" in snapshot or "stored_record_count" in snapshot:
                raise AppError("CATALOG_SNAPSHOT_INVALID",
                               "catalog snapshot activation is not a caller-supplied field", 400)
            key = snapshot.get("snapshot_key")
            if not key:
                raise AppError("CATALOG_SNAPSHOT_INVALID", "catalog snapshot key is required", 400)
            existing = self.catalog_snapshots.get(key)
            if existing is not None:
                return self._catalog_replay(existing, snapshot,
                                            self._CATALOG_SNAPSHOT_IDENTITY, "SNAPSHOT")
            row = {"id": str(uuid4()), "created_by_run_id": str(run_id), "trust_state": trust,
                   "validation_state": snapshot.get("validation_state", "pending"),
                   "stored_record_count": 0, "activated_at": None,
                   **{field: snapshot.get(field) for field in self._CATALOG_SNAPSHOT_IDENTITY},
                   "retrieved_at": snapshot.get("retrieved_at"),
                   "retrieval_metadata": snapshot.get("retrieval_metadata", {}),
                   "snapshot_key": key, "created_at": _now()}
            self.catalog_snapshots[key] = row
            return dict(row)

    def record_catalog_raw_record(self, run_id: UUID, record: dict[str, Any], *,
                                  worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]:
        with self.lock:
            self._catalog_lease(run_id, worker_id, attempt, lease_token)
            snapshot = self._catalog_snapshot_by_id(record.get("snapshot_id"))
            if snapshot["activated_at"] is not None:
                raise AppError("CATALOG_SNAPSHOT_ACTIVE",
                               "an active catalog snapshot is immutable", 409)
            if record.get("resource_id") != snapshot["resource_id"]:
                raise AppError("CATALOG_RECORD_INVALID",
                               "catalog raw record resource mismatch", 400)
            key = (snapshot["id"], record.get("record_key") or "")
            if not key[1]:
                raise AppError("CATALOG_RECORD_INVALID", "catalog record key is required", 400)
            existing = self.catalog_raw_records.get(key)
            if existing is not None:
                return self._catalog_replay(existing, record,
                                            self._CATALOG_RECORD_IDENTITY, "RECORD")
            row = {"id": str(uuid4()), "snapshot_id": snapshot["id"],
                   "resource_id": snapshot["resource_id"], "record_key": key[1],
                   **{field: record.get(field) for field in self._CATALOG_RECORD_IDENTITY},
                   "created_at": _now()}
            self.catalog_raw_records[key] = row
            snapshot["stored_record_count"] += 1
            return dict(row)

    def activate_catalog_snapshot(self, run_id: UUID, activation: dict[str, Any], *,
                                  worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]:
        with self.lock:
            self._catalog_lease(run_id, worker_id, attempt, lease_token)
            snapshot = self._catalog_snapshot_by_id(activation.get("snapshot_id"))
            state = activation.get("validation_state") or "complete"
            if state not in ("complete", "failed"):
                raise AppError("CATALOG_SNAPSHOT_INVALID",
                               "invalid catalog snapshot validation state", 400)
            if snapshot["activated_at"] is not None:
                if state != "complete":
                    raise AppError("CATALOG_SNAPSHOT_CONFLICT",
                                   "catalog snapshot activation conflict", 409)
                return dict(snapshot)
            if state == "failed":
                snapshot["validation_state"] = "failed"
                return dict(snapshot)
            # The completeness gate: a capture holding fewer records than the
            # upstream declared is not a complete snapshot.
            if snapshot["stored_record_count"] != snapshot["declared_record_count"]:
                raise AppError("CATALOG_SNAPSHOT_INCOMPLETE",
                               "catalog snapshot is incomplete", 409)
            snapshot["validation_state"] = "complete"
            snapshot["activated_at"] = _now()
            return dict(snapshot)

    def record_catalog_candidate(self, run_id: UUID, candidate: dict[str, Any], *,
                                 worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]:
        with self.lock:
            self._catalog_lease(run_id, worker_id, attempt, lease_token)
            record = next((row for row in self.catalog_raw_records.values()
                           if row["id"] == str(candidate.get("raw_record_id"))), None)
            if record is None:
                raise AppError("CATALOG_CANDIDATE_INVALID",
                               "invalid catalog candidate raw record", 400)
            # Cross-snapshot linkage: a candidate belongs to the snapshot its
            # record belongs to, and to no other.
            if str(candidate.get("snapshot_id")) != record["snapshot_id"]:
                raise AppError("CATALOG_CANDIDATE_INVALID",
                               "catalog candidate snapshot mismatch", 400)
            status = candidate.get("status") or "candidate"
            if status not in CANDIDATE_STATUSES:
                raise AppError("CATALOG_CANDIDATE_INVALID",
                               "invalid catalog candidate status", 400)
            if (candidate.get("model_year_start") is None) != (candidate.get("model_year_end") is None):
                raise AppError("CATALOG_CANDIDATE_INVALID",
                               "a model year range must be whole", 400)
            # Raises on an unknown dimension, an empty one, or a padded one.
            stated_identity_dimensions(candidate.get("identity_dimensions"))
            key = (record["snapshot_id"], candidate.get("candidate_key") or "")
            if not key[1]:
                raise AppError("CATALOG_CANDIDATE_INVALID", "catalog candidate key is required", 400)
            existing = self.catalog_candidates.get(key)
            if existing is not None:
                replayed = self._catalog_replay(existing, {**candidate,
                                                           "raw_record_id": record["id"]},
                                                self._CATALOG_CANDIDATE_IDENTITY, "CANDIDATE")
                # The READING may be revised; the identity may not.
                existing["status"] = status
                return {**replayed, "status": status}
            row = {"id": str(uuid4()), "snapshot_id": record["snapshot_id"],
                   "candidate_key": key[1], "status": status,
                   **{field: candidate.get(field) for field in self._CATALOG_CANDIDATE_IDENTITY},
                   "raw_record_id": record["id"],
                   "identity_dimensions": candidate.get("identity_dimensions") or {},
                   "created_at": _now()}
            self.catalog_candidates[key] = row
            return dict(row)

    def link_catalog_candidate_evidence(self, run_id: UUID, link: dict[str, Any], *,
                                        worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]:
        with self.lock:
            self._catalog_lease(run_id, worker_id, attempt, lease_token)
            candidate = next((row for row in self.catalog_candidates.values()
                              if row["id"] == str(link.get("candidate_id"))), None)
            if candidate is None:
                raise AppError("CATALOG_LINK_INVALID", "invalid catalog evidence link candidate", 400)
            snapshot = self._catalog_snapshot_by_id(candidate["snapshot_id"])
            # Cross-RUN linkage: the cited source must belong to the run
            # holding the lease.
            source = next((row for row in self.tool_rows
                           if row["id"] == str(link.get("source_id"))
                           and row["run_id"] == str(run_id)), None)
            if source is None:
                raise AppError("CATALOG_LINK_INVALID", "invalid catalog evidence link source", 400)
            if link.get("verdict_id") is not None:
                if link.get("claim_id") is None:
                    raise AppError("CATALOG_LINK_INVALID",
                                   "catalog evidence link verdict requires its claim", 400)
                # The legacy catalog never verifies a fact.
                if snapshot["trust_state"] != "evidence":
                    raise AppError("CATALOG_LINK_UNVERIFIED_SOURCE",
                                   "an unverified catalog source cannot carry a verdict", 409)
            key = (candidate["id"], link.get("link_key") or "")
            if not key[1]:
                raise AppError("CATALOG_LINK_INVALID", "catalog link key is required", 400)
            existing = self.catalog_evidence_links.get(key)
            if existing is not None:
                return self._catalog_replay(existing, link, self._CATALOG_LINK_IDENTITY, "LINK")
            row = {"id": str(uuid4()), "candidate_id": candidate["id"],
                   "snapshot_id": snapshot["id"], "run_id": str(run_id), "link_key": key[1],
                   **{field: link.get(field) for field in self._CATALOG_LINK_IDENTITY},
                   "created_at": _now()}
            self.catalog_evidence_links[key] = row
            return dict(row)

    def _catalog_snapshot_by_id(self, snapshot_id: Any) -> dict[str, Any]:
        snapshot = next((row for row in self.catalog_snapshots.values()
                         if row["id"] == str(snapshot_id)), None)
        if snapshot is None:
            raise AppError("CATALOG_SNAPSHOT_INVALID", "invalid catalog snapshot", 400)
        return snapshot

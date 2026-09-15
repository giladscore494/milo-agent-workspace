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
from typing import Any, Mapping
from uuid import UUID, uuid4

from backend.catalog.contracts import (CANDIDATE_STATUSES, CATALOG_SOURCE_FAMILIES,
                                       stated_identity_dimensions, stated_source_locator,
                                       trust_state_for)
from backend.catalog.digest import catalog_payload_digest
from backend.engines.swarm_v2.evidence_bounds import FRAGMENT_TYPES
from backend.engines.swarm_v2.evidence_contracts import (fragment_type_for,
                                                         parse_locator_key)
from backend.engines.swarm_v2.fragments import fragment_content_hash
from backend.engines.swarm_v2.support import VERIFICATION_MODES, VERIFIER_CONTRACT_VERSION
from backend.catalog.payloads import (prepare_candidate, prepare_evidence_link,
                                      prepare_raw_record, prepare_snapshot)
from backend.errors import AppError, NotFoundError
from backend.runtime import RUN_STATES, InvalidTransition, validate_transition
from backend.schemas import normalize_conversation_title


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _support_set(support: Any) -> frozenset[tuple[str, Any, Any]]:
    """A verdict's durable support as the SET PostgreSQL actually stores.

    `claim_verdict_supports` holds one relational row per cited fragment,
    unique on `(verdict_id, fragment_id)` and carrying the fragment's own
    `content_hash` and `locator_key`. So the identity of a verdict's support
    is the SET of those triples: input order is not part of it, and one
    fragment cited twice is one row, never two.

    Collapsing to a set is therefore the comparison, and the CARDINALITY of
    the incoming list is checked separately by the caller -- a list that
    shrinks when it becomes a set was never a valid citation.
    """
    return frozenset((str(link.get("fragment_id")), link.get("content_hash"),
                      link.get("locator_key"))
                     for link in (support or []) if isinstance(link, Mapping))


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
        # Evidence row id -> what KIND of evidence row it is. Kept beside the
        # rows rather than inside them so a row's shape stays exactly what the
        # caller wrote, while a typed lookup can still refuse a source that is
        # being passed off as a claim.
        self.evidence_kinds: dict[str, str] = {}
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

    def _tool_row(self, run_id: UUID, payload: dict[str, Any],
                  kind: str | None = None) -> dict[str, Any]:
        """Append one evidence row, recording WHAT KIND of row it is.

        Every evidence row shares `tool_rows`, so without a recorded type a
        lookup could only check an id and a run -- and a source id would then
        pass as a claim id, and a claim id as a verdict id. The type is kept in
        a side table rather than inside the row so the row's shape stays
        exactly what a caller wrote, and a row written with no kind is
        untyped: it can never satisfy a typed lookup.
        """
        row = {"id": str(uuid4()), "run_id": str(run_id), **payload}
        self.tool_rows.append(row)
        if kind is not None:
            self.evidence_kinds[row["id"]] = kind
        return dict(row)

    def _evidence_lease(self, run_id: UUID, worker_id: str | None, attempt: int | None,
                        lease_token: str | None) -> None:
        """Hold a durable evidence write to the same lease its RPC requires.

        The PostgreSQL counterparts all call `assert_worker_lease` first, and
        its four arguments are NOT optional there, so a wrong worker, a
        superseded attempt, a wrong token or an expired lease writes nothing.

        FAIL CLOSED ON AN ABSENT VALUE. `_assert_active_lease` skips whichever
        component is `None` -- that is deliberate for the older call paths that
        legitimately pass none -- so handing it a missing value here would turn
        "no lease" into "no check", which is how an earlier round of this branch
        let a leaseless write succeed. A durable evidence write states all three
        or writes nothing.
        """
        missing = [name for name, value in (("worker_id", worker_id), ("attempt", attempt),
                                            ("lease_token", lease_token))
                   if value is None or (isinstance(value, str) and not value.strip())]
        if missing:
            raise AppError("RUN_TRANSITION_CONFLICT",
                           "a durable evidence write requires a complete worker lease", 409)
        run = self.runs.get(str(run_id))
        if run is None:
            raise NotFoundError("run", str(run_id))
        self._assert_active_lease(run, worker_id, attempt, lease_token)

    def create_tool_access_request(self, run_id: UUID, request: dict[str, Any]) -> dict[str, Any]:
        return self._tool_row(run_id, request)

    def create_tool_grant(self, run_id: UUID, grant: dict[str, Any]) -> dict[str, Any]:
        return self._tool_row(run_id, grant)

    def create_tool_usage(self, run_id: UUID, usage: dict[str, Any], **lease: Any) -> dict[str, Any]:
        # COMPATIBILITY BOUNDARY, unchanged and documented: these predate the
        # lease contract and many callers pass none. A lease that IS supplied
        # is enforced completely -- partial is refused like any other -- but an
        # omitted one is still permitted here, unlike the three durable
        # evidence writers above.
        if lease:
            self._evidence_lease(run_id, lease.get("worker_id"), lease.get("attempt"),
                                 lease.get("lease_token"))
        return self._tool_row(run_id, usage, kind="tool_usage")

    def create_source(self, run_id: UUID, source: dict[str, Any], **lease: Any) -> dict[str, Any]:
        # COMPATIBILITY BOUNDARY, unchanged and documented: these predate the
        # lease contract and many callers pass none. A lease that IS supplied
        # is enforced completely -- partial is refused like any other -- but an
        # omitted one is still permitted here, unlike the three durable
        # evidence writers above.
        if lease:
            self._evidence_lease(run_id, lease.get("worker_id"), lease.get("attempt"),
                                 lease.get("lease_token"))
        return self._tool_row(run_id, source, kind="source")

    def create_claim(self, run_id: UUID, claim: dict[str, Any], **lease: Any) -> dict[str, Any]:
        # COMPATIBILITY BOUNDARY, unchanged and documented: these predate the
        # lease contract and many callers pass none. A lease that IS supplied
        # is enforced completely -- partial is refused like any other -- but an
        # omitted one is still permitted here, unlike the three durable
        # evidence writers above.
        if lease:
            self._evidence_lease(run_id, lease.get("worker_id"), lease.get("attempt"),
                                 lease.get("lease_token"))
        return self._tool_row(run_id, claim, kind="claim")

    def create_conflict(self, run_id: UUID, conflict: dict[str, Any], **lease: Any) -> dict[str, Any]:
        # COMPATIBILITY BOUNDARY, unchanged and documented: these predate the
        # lease contract and many callers pass none. A lease that IS supplied
        # is enforced completely -- partial is refused like any other -- but an
        # omitted one is still permitted here, unlike the three durable
        # evidence writers above.
        if lease:
            self._evidence_lease(run_id, lease.get("worker_id"), lease.get("attempt"),
                                 lease.get("lease_token"))
        return self._tool_row(run_id, conflict, kind="conflict")

    # The Repository protocol declares these three, and their absence was
    # itself a parity gap: a test could not build a real verdict to cite, so a
    # catalog test had no choice but to invent a uuid for one. They are
    # lease-guarded here exactly as their RPCs are.
    def record_evidence_fragment(self, run_id: UUID, fragment: dict[str, Any], *,
                                 worker_id: str, attempt: int,
                                 lease_token: str) -> dict[str, Any]:
        """One focused fragment, held to `record_evidence_fragment_guarded`'s rules.

        Mirrored here because the catalog link path depends on them: a fragment
        is what a verdict's support link names, so a fragment that could not
        exist in PostgreSQL would make every verdict citing it meaningless.
        """
        self._evidence_lease(run_id, worker_id, attempt, lease_token)
        key = self._require_evidence_key(fragment, "EVIDENCE_FRAGMENT")
        task = fragment.get("task_key")
        if not task:
            raise AppError("EVIDENCE_FRAGMENT_INVALID",
                           "invalid evidence fragment: evidence_key and task_key are required",
                           400)
        source = self._typed_evidence_row(fragment.get("source_id"), run_id, "source",
                                          "EVIDENCE_FRAGMENT", "evidence fragment source")
        # Task provenance: a fragment may only be attributed to the task that
        # captured its source. Belonging to the same run is not enough.
        if source.get("task_key") != task:
            raise AppError("EVIDENCE_FRAGMENT_TASK",
                           "evidence fragment task provenance mismatch", 400)
        # A FOCUSED fragment states a type and a locator that agree, or states
        # neither. `r3_focus_valid` pairs a `record_field` locator with a
        # `structured_projection` and a `document_span` with a
        # `verbatim_excerpt`, and nothing else.
        fragment_type = fragment.get("fragment_type")
        locator = fragment.get("locator_key")
        if (fragment_type is None) != (locator is None):
            raise AppError("EVIDENCE_FRAGMENT_FOCUS",
                           "invalid evidence fragment: a focused fragment requires both a "
                           "type and a locator", 400)
        if fragment_type is not None:
            if fragment_type not in FRAGMENT_TYPES:
                raise AppError("EVIDENCE_FRAGMENT_FOCUS",
                               "invalid evidence fragment: unknown fragment type", 400)
            # The pairing rule is the contract module's own, not a copy: a
            # non-canonical locator raises here rather than resolving to a kind.
            try:
                expected = fragment_type_for(parse_locator_key(locator))
            except Exception:
                raise AppError("EVIDENCE_FRAGMENT_FOCUS",
                               "invalid evidence fragment: locator is not a canonical "
                               "bounded location", 400) from None
            if expected != fragment_type:
                raise AppError("EVIDENCE_FRAGMENT_FOCUS",
                               "invalid evidence fragment: fragment type does not match the "
                               "locator kind", 400)
        text = fragment.get("fragment_text")
        if not text or not str(text).strip():
            raise AppError("EVIDENCE_FRAGMENT_INVALID",
                           "invalid evidence fragment: fragment_text must not be empty", 400)
        if fragment.get("content_hash") != fragment_content_hash(text):
            raise AppError("EVIDENCE_FRAGMENT_HASH",
                           "invalid evidence fragment: content hash does not match the "
                           "bounded text", 400)
        return self._replayable_evidence_row(run_id, key, fragment, "evidence_fragment",
                                             ("source_id", "task_key", "fragment_text",
                                              "content_hash", "fragment_type", "locator_key"),
                                             "EVIDENCE_FRAGMENT")

    def record_claim_verdict(self, run_id: UUID, verdict: dict[str, Any], *,
                             worker_id: str, attempt: int,
                             lease_token: str) -> dict[str, Any]:
        """One verdict, held to the support contract `record_claim_verdict_guarded` applies.

        The rule that matters most here: **an accepted verdict must cite
        durable evidence**. PR #86's memory implementation accepted a
        `verified` verdict with no support at all, and its own catalog fixture
        built exactly such a verdict -- so the catalog tests were citing
        something PostgreSQL would have refused to create.

        THE REPLAY IDENTITY IS THE WHOLE VERDICT, SUPPORT INCLUDED. An earlier
        round compared only `(claim_id, verdict, verification_mode,
        verifier_contract_version)`, so the same evidence key could be replayed
        with a different stated `reason`, or with evidence added, removed or
        swapped, and quietly return the stored row. PostgreSQL compares the
        reason too, and holds the stored support set to the cited one.

        SUPPORT IS A SET, NOT A SEQUENCE. PostgreSQL stores support as rows
        unique on `(verdict_id, fragment_id)`, so input ORDER is not identity
        -- membership and cardinality are. The same links in a different order
        replay onto the same row; a repeated link cites one fragment and can
        never produce two stored rows, so it is refused.

        NOTHING IS WRITTEN UNTIL EVERY CHECK HAS PASSED. The lease, the
        vocabulary, the lineage of each cited link, the set cardinality and the
        replay comparison all run before the row is appended, so a refused call
        -- first write or replay -- leaves the stored verdict and its support
        exactly as they were.
        """
        self._evidence_lease(run_id, worker_id, attempt, lease_token)
        key = self._require_evidence_key(verdict, "CLAIM_VERDICT")
        # HOW a verdict was reached and under WHICH contract are part of it.
        if verdict.get("verification_mode") not in VERIFICATION_MODES:
            raise AppError("CLAIM_VERDICT_INVALID",
                           "invalid claim verdict: unknown verification mode", 400)
        if verdict.get("verifier_contract_version") != VERIFIER_CONTRACT_VERSION:
            raise AppError("CLAIM_VERDICT_CONTRACT",
                           "invalid claim verdict: unknown verifier contract version", 400)
        if verdict.get("verdict") not in ("verified", "needs_review", "rejected"):
            raise AppError("CLAIM_VERDICT_INVALID",
                           "invalid claim verdict: unknown verdict", 400)
        # A MISSING `support` key means "cites nothing"; a SUPPLIED one must be
        # a JSON array. `verdict.get("support") or []` conflated the two: every
        # falsy value -- `null`, `{}`, `""`, `0`, `false` -- became an empty
        # list, so the type check below could never fire and the junk value was
        # stored verbatim in the row.
        #
        # PostgreSQL keeps the two apart, and not by convention:
        # `p_verdict->'support'` is SQL NULL only when the KEY IS ABSENT, so
        # `coalesce(..., '[]'::jsonb)` substitutes an empty array there alone,
        # while a supplied JSON `null` is `'null'::jsonb` -- not SQL NULL --
        # and reaches `jsonb_typeof(v_support) <> 'array'`.
        support = verdict["support"] if "support" in verdict else []
        if not isinstance(support, list):
            raise AppError("CLAIM_VERDICT_INVALID",
                           "invalid claim verdict: support must be an array", 400)
        if len(support) > 4:
            raise AppError("CLAIM_VERDICT_INVALID",
                           "claim verdict cites more evidence than a source can hold", 400)
        claim = self._typed_evidence_row(verdict.get("claim_id"), run_id, "claim",
                                         "CLAIM_VERDICT", "claim")
        # A locally settled verdict compares no evidence, so it may cite none.
        # With the rule below, this also makes `verified` + `deterministic_local`
        # unreachable from either side: local permits no support, and an
        # accepted verdict requires it.
        if verdict.get("verification_mode") == "deterministic_local" and support:
            raise AppError("CLAIM_VERDICT_LOCAL_SUPPORT",
                           "a locally settled verdict cites no evidence", 400)
        if verdict.get("verdict") == "verified" and not support:
            raise AppError("CLAIM_VERDICT_UNSUPPORTED",
                           "an accepted verdict must cite durable evidence", 400)
        for link in support:
            if not isinstance(link, dict):
                raise AppError("CLAIM_VERDICT_INVALID",
                               "invalid claim verdict: support must be an array", 400)
            # A support link naming no durable fragment of this run is forged.
            fragment = self._typed_evidence_row(link.get("fragment_id"), run_id,
                                                "evidence_fragment", "CLAIM_VERDICT",
                                                "support link")
            # The lineage: the evidence must belong to the claim's own source.
            if str(fragment.get("source_id")) != str(claim.get("source_id")):
                raise AppError("CLAIM_VERDICT_SUPPORT_SOURCE",
                               "verdict support link belongs to another source", 400)
            if fragment.get("content_hash") != link.get("content_hash"):
                raise AppError("CLAIM_VERDICT_SUPPORT_HASH",
                               "verdict support link content hash mismatch", 400)
            if fragment.get("locator_key") != link.get("locator_key"):
                raise AppError("CLAIM_VERDICT_SUPPORT_LOCATOR",
                               "verdict support link locator mismatch", 400)
        # Support is a SET: a list naming one fragment twice cites one fragment,
        # so it could never become two rows unique on (verdict_id, fragment_id).
        if len(_support_set(support)) != len(support):
            raise AppError("CLAIM_VERDICT_SUPPORT_SET",
                           "verdict support links do not match the cited evidence", 400)
        return self._replayable_evidence_row(run_id, key, verdict, "claim_verdict",
                                             ("claim_id", "verdict", "reason",
                                              "verification_mode",
                                              "verifier_contract_version", "support"),
                                             "CLAIM_VERDICT",
                                             normalizers={"support": _support_set},
                                             messages={"support": "verdict support links "
                                                       "do not match the cited evidence"})

    def record_conflict_resolution(self, run_id: UUID, resolution: dict[str, Any], *,
                                   worker_id: str, attempt: int,
                                   lease_token: str) -> dict[str, Any]:
        self._evidence_lease(run_id, worker_id, attempt, lease_token)
        return self._tool_row(run_id, resolution, kind="conflict_resolution")

    @staticmethod
    def _require_evidence_key(payload: Mapping[str, Any], code: str) -> str:
        """The stable replay identity every durable evidence row must carry."""
        key = payload.get("evidence_key")
        if not key or not str(key).strip():
            raise AppError(f"{code}_INVALID",
                           "invalid durable evidence: evidence_key is required", 400)
        return str(key)

    def _replayable_evidence_row(self, run_id: UUID, key: str, payload: dict[str, Any],
                                 kind: str, identity: tuple[str, ...],
                                 code: str, *,
                                 normalizers: Mapping[str, Any] | None = None,
                                 messages: Mapping[str, str] | None = None,
                                 ) -> dict[str, Any]:
        """Return the existing row for an exact replay, or fail closed.

        `(run_id, evidence_key)` is the replay identity in PostgreSQL, so a
        retry of the same logical row collapses onto it and a reuse of the key
        for different content is a conflict -- never a second row and never a
        silent overwrite.
        """
        normalizers = normalizers or {}
        messages = messages or {}
        existing = next((row for row in self.tool_rows
                         if str(row.get("run_id")) == str(run_id)
                         and row.get("evidence_key") == key
                         and self.evidence_kinds.get(str(row.get("id"))) == kind), None)
        if existing is None:
            return self._tool_row(run_id, payload, kind=kind)
        for field in identity:
            # A normalized field is compared by VALUE, not by representation:
            # a verdict's support is a set of relational rows in PostgreSQL, so
            # the same links in a different order are the same support.
            normalize = normalizers.get(field)
            stored, cited = existing.get(field), payload.get(field)
            if normalize is not None:
                stored, cited = normalize(stored), normalize(cited)
            if stored != cited:
                raise AppError(f"{code}_IDEMPOTENCY_CONFLICT",
                               messages.get(field,
                                            f"{kind.replace('_', ' ')} idempotency conflict"),
                               409)
        return dict(existing)

    def _typed_evidence_row(self, row_id: Any, run_id: UUID, kind: str,
                            code: str, label: str) -> dict[str, Any]:
        """One evidence row of THIS run AND of this exact kind, or fail closed.

        The type check is the point: `tool_rows` holds sources, claims,
        fragments, verdicts and resolutions together, so an id-and-run lookup
        alone would let a source stand in for a claim.
        """
        row = next((item for item in self.tool_rows
                    if str(item.get("id")) == str(row_id)
                    and str(item.get("run_id")) == str(run_id)
                    and self.evidence_kinds.get(str(item.get("id"))) == kind), None)
        if row is None:
            raise AppError(f"{code}_INVALID", f"invalid {label}", 400)
        return row

    # --- durable catalog staging --------------------------------------------
    #
    # These mirror the guarded RPCs of the catalog migrations, and they mirror
    # the parts that MATTER for a test to be meaningful: the lease guard, the
    # derived identity keys and payload digest, the referential provenance
    # checks, the fail-closed replay conflict, the snapshot lifecycle and the
    # cross-run / cross-snapshot / unverified-source refusals.
    #
    # The identity keys and the payload rules come from the SAME
    # `backend.catalog.payloads` preparers the Supabase repository uses, and
    # every rejection below has a counterpart proven against real PostgreSQL in
    # `tests/test_migrations_postgres.py`. A mirror that accepted what
    # PostgreSQL rejects would let a unit test pass against behaviour the
    # database refuses -- which is what the PR1 round did.
    #
    # WHAT THIS IS NOT. This is a mirror of the rules these catalog and
    # evidence paths depend on, enumerated and tested one by one -- not a
    # reimplementation of PostgreSQL. It does not reproduce every R3/R4
    # validation (fragment size and count bounds, the credential/reasoning
    # marker screens, canonical scope hashing, conflict grouping), and the
    # older `create_*` evidence writers enforce a lease only when one is passed,
    # because their signatures predate the lease contract. Read the tests for
    # what is actually established; do not read this class as a claim that
    # anything PostgreSQL refuses is refused here.
    #
    # HOW FAR REPLAY PARITY GOES. It is established, case by case against the
    # real RPCs, for the DURABLE EVIDENCE WRITERS ONLY:
    #
    #   * `record_claim_verdict` -- the full replay identity (claim, verdict,
    #     reason, mode, contract version) and the durable support set, as a
    #     set: see `tests/test_catalog_persistence.py` section 4, paired with
    #     `test_the_verdict_replay_contract_is_the_databases_own`;
    #   * `record_evidence_fragment` and the catalog writers -- `(run_id,
    #     evidence_key)` / the catalog identity keys, each with a counterpart
    #     PostgreSQL test.
    #
    # It is NOT claimed for `create_source`, `create_claim`, `create_conflict`
    # or `create_tool_usage`, whose replay behaviour is untested here and
    # unchanged.
    #
    # One rule is deliberately absent from `record_claim_verdict` because it is
    # unreachable, not because it is unenforced: PostgreSQL re-checks each
    # support link's `task_key` against the cited source's. Here
    # `record_evidence_fragment` already refuses a fragment whose task differs
    # from its own source's, so a fragment of the claim's source always carries
    # that source's task.

    _CATALOG_SNAPSHOT_IDENTITY = ("source_family", "resource_id", "upstream_version",
                                  "upstream_version_kind", "content_sha256",
                                  "declared_record_count")
    _CATALOG_RECORD_IDENTITY = ("upstream_record_id", "payload_sha256", "payload",
                                "source_locator")
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

    #: What kind of evidence row each catalog link field must name.
    _CATALOG_LINK_ROW_KINDS = {"SOURCE": "source", "CLAIM": "claim",
                               "VERDICT": "claim_verdict"}

    def _catalog_evidence_row(self, row_id: Any, run_id: UUID, kind: str) -> dict[str, Any]:
        """One evidence row of THIS run AND of the right KIND, or fail closed.

        No arbitrary uuid ever stands in for evidence, and no row of the wrong
        type does either: PostgreSQL reads `public.sources`, `public.claims`
        and `public.claim_verdicts` as separate relations, so a source id
        simply cannot resolve as a claim there. Checking only id and run here
        -- which is what this did before -- let exactly that through.
        """
        row = next((item for item in self.tool_rows
                    if str(item.get("id")) == str(row_id)
                    and str(item.get("run_id")) == str(run_id)
                    and self.evidence_kinds.get(str(item.get("id")))
                    == self._CATALOG_LINK_ROW_KINDS[kind]), None)
        if row is None:
            raise AppError(f"CATALOG_LINK_{kind}_INVALID",
                           f"invalid catalog evidence link {kind.lower()}", 400)
        return row

    @staticmethod
    def _catalog_replay(existing: dict[str, Any], incoming: Mapping[str, Any],
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
            snapshot = prepare_snapshot(snapshot)
            family = snapshot.get("source_family")
            if family not in CATALOG_SOURCE_FAMILIES:
                raise AppError("CATALOG_SNAPSHOT_INVALID", "unknown catalog source family", 400)
            # Derived, never accepted: the legacy catalog cannot be submitted
            # as evidence even by a trusted caller.
            trust = trust_state_for(family)
            if snapshot.get("trust_state") not in (None, trust):
                raise AppError("CATALOG_SNAPSHOT_INVALID",
                               "catalog trust state is pinned to the source family", 400)
            key = snapshot["snapshot_key"]
            existing = self.catalog_snapshots.get(key)
            if existing is not None:
                return self._catalog_replay(existing, snapshot,
                                            self._CATALOG_SNAPSHOT_IDENTITY, "SNAPSHOT")
            # Natural uniqueness: a rename cannot duplicate a retrieval.
            natural = tuple(snapshot.get(field) for field in
                            ("source_family", "resource_id", "upstream_version_kind",
                             "upstream_version", "content_sha256"))
            if any(tuple(row.get(field) for field in
                         ("source_family", "resource_id", "upstream_version_kind",
                          "upstream_version", "content_sha256")) == natural
                   for row in self.catalog_snapshots.values()):
                raise AppError("CATALOG_SNAPSHOT_DUPLICATE",
                               "catalog_source_snapshots_natural_uidx", 409)
            row = {"id": str(uuid4()), "created_by_run_id": str(run_id), "trust_state": trust,
                   "validation_state": snapshot.get("validation_state", "pending"),
                   "stored_record_count": 0, "activated_at": None,
                   **{field: snapshot.get(field) for field in self._CATALOG_SNAPSHOT_IDENTITY},
                   "retrieved_at": snapshot.get("retrieved_at"),
                   "retrieval_metadata": snapshot.get("retrieval_metadata", {}),
                   "snapshot_key": key, "created_at": _now()}
            self.catalog_snapshots[key] = row
            return dict(row)

    def _catalog_owned_snapshot(self, snapshot_id: Any, run_id: UUID) -> dict[str, Any]:
        """The snapshot this run opened, or fail closed.

        A snapshot belongs to the run that opened it: holding SOME valid lease
        is not enough to fill or decide another run's capture.
        """
        snapshot = self._catalog_snapshot_by_id(snapshot_id)
        if snapshot["created_by_run_id"] != str(run_id):
            raise AppError("CATALOG_SNAPSHOT_NOT_OWNED",
                           "this catalog snapshot does not belong to this run", 409)
        return snapshot

    def record_catalog_raw_record(self, run_id: UUID, record: dict[str, Any], *,
                                  worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]:
        with self.lock:
            self._catalog_lease(run_id, worker_id, attempt, lease_token)
            record = prepare_raw_record(record)
            snapshot = self._catalog_owned_snapshot(record.get("snapshot_id"), run_id)
            # Both decided states are terminal.
            if snapshot["validation_state"] == "failed":
                raise AppError("CATALOG_SNAPSHOT_FAILED",
                               "a failed catalog snapshot is terminal", 409)
            if snapshot["activated_at"] is not None:
                raise AppError("CATALOG_SNAPSHOT_ACTIVE",
                               "an active catalog snapshot is immutable", 409)
            if record.get("resource_id") != snapshot["resource_id"]:
                raise AppError("CATALOG_RECORD_INVALID",
                               "catalog raw record resource mismatch", 400)
            # The digest is derived from the stored payload, exactly as the
            # database derives it -- never predicted by the caller.
            # The locator is the record's position in the capture: validated
            # against the closed vocabulary, and normalized to `{}` when the
            # retrieval had no pagination, exactly as the column's default is.
            record = {**record, "payload_sha256": catalog_payload_digest(record["payload"]),
                      "source_locator": stated_source_locator(record.get("source_locator"))}
            key = (snapshot["id"], record["record_key"])
            existing = self.catalog_raw_records.get(key)
            if existing is not None:
                return self._catalog_replay(existing, record,
                                            self._CATALOG_RECORD_IDENTITY, "RECORD")
            # One POSITION belongs to one row. Mirrors the partial unique index
            # `catalog_raw_records_snapshot_position_uidx`: a record that states
            # no position collides with nothing.
            position = record["source_locator"].get("capture_index")
            if position is not None and any(
                    row["snapshot_id"] == snapshot["id"]
                    and (row.get("source_locator") or {}).get("capture_index") == position
                    for row in self.catalog_raw_records.values()):
                raise AppError("CATALOG_RECORD_DUPLICATE",
                               "catalog_raw_records_snapshot_position_uidx", 409)
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
            snapshot = self._catalog_owned_snapshot(activation.get("snapshot_id"), run_id)
            state = activation.get("validation_state") or "complete"
            if state not in ("complete", "failed"):
                raise AppError("CATALOG_SNAPSHOT_INVALID",
                               "invalid catalog snapshot validation state", 400)
            # `failed` is terminal: re-declaring it is a no-op, anything else
            # is refused, so an unusable capture never becomes complete.
            if snapshot["validation_state"] == "failed":
                if state != "failed":
                    raise AppError("CATALOG_SNAPSHOT_FAILED",
                                   "a failed catalog snapshot is terminal", 409)
                return dict(snapshot)
            if snapshot["activated_at"] is not None:
                if state != "complete":
                    raise AppError("CATALOG_SNAPSHOT_ACTIVE",
                                   "an active catalog snapshot is immutable", 409)
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
            candidate = prepare_candidate(candidate)
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
            # Raises on an unknown dimension, an empty one, or a padded one.
            stated_identity_dimensions(candidate.get("identity_dimensions"))
            key = (record["snapshot_id"], candidate["candidate_key"])
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
            link = prepare_evidence_link(link)
            candidate = next((row for row in self.catalog_candidates.values()
                              if row["id"] == str(link.get("candidate_id"))), None)
            if candidate is None:
                raise AppError("CATALOG_LINK_INVALID", "invalid catalog evidence link candidate", 400)
            snapshot = self._catalog_snapshot_by_id(candidate["snapshot_id"])

            # Cross-RUN linkage: every cited row must belong to the run holding
            # the lease. A later run may add evidence to an existing candidate
            # -- deliberately -- but only ever evidence of its own.
            source = self._catalog_evidence_row(link.get("source_id"), run_id, "SOURCE")
            claim = self._catalog_evidence_row(link.get("claim_id"), run_id, "CLAIM")
            # The claim must rest on the source being cited, not merely share a run.
            if str(claim.get("source_id")) != str(source["id"]):
                raise AppError("CATALOG_LINK_CLAIM_SOURCE_MISMATCH",
                               "catalog evidence link claim source mismatch", 400)

            # Provenance is DERIVED from the evidence, never taken from the
            # caller; a caller that states it is held to it.
            locator = claim.get("evidence_locator")
            kind = source.get("source_version_kind")
            version = source.get("source_version_id")
            if not locator:
                raise AppError("CATALOG_LINK_NO_LOCATOR",
                               "the cited claim states no evidence locator", 400)
            if not kind or not version:
                raise AppError("CATALOG_LINK_NO_VERSION",
                               "the cited source states no version to pin this link to", 400)
            if link.get("record_locator") not in (None, locator):
                raise AppError("CATALOG_LINK_LOCATOR_MISMATCH",
                               "catalog evidence link record locator does not match the cited claim",
                               400)
            if link.get("source_version") not in (None, version) or \
                    link.get("source_version_kind") not in (None, kind):
                raise AppError("CATALOG_LINK_VERSION_MISMATCH",
                               "catalog evidence link source version does not match the cited source",
                               400)

            verdict_id = link.get("verdict_id")
            if verdict_id is not None:
                # The legacy catalog never verifies a fact.
                if snapshot["trust_state"] != "evidence":
                    raise AppError("CATALOG_LINK_UNVERIFIED_SOURCE",
                                   "an unverified catalog source cannot carry a verdict", 409)
                verdict = self._catalog_evidence_row(verdict_id, run_id, "VERDICT")
                if str(verdict.get("claim_id")) != str(claim["id"]):
                    raise AppError("CATALOG_LINK_VERDICT_CLAIM_MISMATCH",
                                   "catalog evidence link verdict claim mismatch", 400)
                # What the verdict SAYS, not merely that it exists.
                if verdict.get("verdict") != "verified":
                    raise AppError("CATALOG_LINK_VERDICT_NOT_VERIFIED",
                                   "catalog evidence link verdict is not verified", 409)

            resolved = {**link, "record_locator": locator, "source_version": version,
                        "source_version_kind": kind}
            key = (candidate["id"], link["link_key"])
            existing = self.catalog_evidence_links.get(key)
            if existing is not None:
                return self._catalog_replay(existing, resolved,
                                            self._CATALOG_LINK_IDENTITY, "LINK")
            # Natural uniqueness: one citation of one claim per candidate, per
            # verdict state.
            natural = (candidate["id"], str(claim["id"]),
                       None if verdict_id is None else str(verdict_id))
            if any((row["candidate_id"], str(row["claim_id"]),
                    None if row["verdict_id"] is None else str(row["verdict_id"])) == natural
                   for row in self.catalog_evidence_links.values()):
                raise AppError("CATALOG_LINK_DUPLICATE",
                               "catalog_candidate_evidence_links_natural_uidx", 409)
            row = {"id": str(uuid4()), "candidate_id": candidate["id"],
                   "snapshot_id": snapshot["id"], "run_id": str(run_id), "link_key": key[1],
                   **{field: resolved.get(field) for field in self._CATALOG_LINK_IDENTITY},
                   "created_at": _now()}
            self.catalog_evidence_links[key] = row
            return dict(row)

    # --- durable catalog reads ------------------------------------------------
    #
    # Mirrors of the Supabase reads, including the parts that MATTER for a
    # query layer to be meaningful: only ACTIVE snapshots are visible, every
    # ordering is by an ASCII key that is unique within its scope, and every
    # read is bounded and offset-paged.

    MAX_CATALOG_SNAPSHOT_ROWS = 50
    MAX_CATALOG_RECORD_ROWS = 500
    MAX_CATALOG_CANDIDATE_ROWS = 500

    def list_active_catalog_snapshots(self, source_family: str, *, resource_id: Any = None,
                                      limit: int = MAX_CATALOG_SNAPSHOT_ROWS) -> list[dict[str, Any]]:
        with self.lock:
            rows = [dict(row) for row in self.catalog_snapshots.values()
                    if row.get("source_family") == str(source_family)
                    and row.get("activated_at") is not None
                    and (resource_id is None or row.get("resource_id") == str(resource_id))]
        # `activated_at` DESC, then `snapshot_key` ASC -- exactly the Supabase
        # ordering.  Sorting the whole tuple in reverse would reverse the
        # TIEBREAK too, so snapshots activated in the same instant came back in
        # the opposite order from the database's.  Two stable passes, ascending
        # tiebreak first, is the one spelling that cannot drift.
        rows.sort(key=lambda row: row["snapshot_key"])
        rows.sort(key=lambda row: row["activated_at"], reverse=True)
        return rows[:max(1, min(int(limit), self.MAX_CATALOG_SNAPSHOT_ROWS))]

    def find_active_catalog_snapshot(self, source_family: str, resource_id: Any,
                                     snapshot_key: str) -> dict[str, Any] | None:
        """ONE active snapshot named exactly, or None. Not a bounded search."""
        with self.lock:
            return next((dict(row) for row in self.catalog_snapshots.values()
                         if row.get("snapshot_key") == str(snapshot_key)
                         and row.get("source_family") == str(source_family)
                         and row.get("resource_id") == str(resource_id)
                         and row.get("activated_at") is not None), None)

    def list_catalog_raw_records(self, snapshot_id: Any, *, limit: int = MAX_CATALOG_RECORD_ROWS,
                                 offset: int = 0) -> list[dict[str, Any]]:
        with self.lock:
            rows = [dict(row) for row in self.catalog_raw_records.values()
                    if row["snapshot_id"] == str(snapshot_id)]
        rows.sort(key=lambda row: row["record_key"])
        start = max(0, int(offset))
        return rows[start:start + max(1, min(int(limit), self.MAX_CATALOG_RECORD_ROWS))]

    def list_catalog_candidates(self, snapshot_id: Any, *, limit: int = MAX_CATALOG_CANDIDATE_ROWS,
                                offset: int = 0) -> list[dict[str, Any]]:
        with self.lock:
            rows = [dict(row) for row in self.catalog_candidates.values()
                    if row["snapshot_id"] == str(snapshot_id)]
        rows.sort(key=lambda row: row["candidate_key"])
        start = max(0, int(offset))
        return rows[start:start + max(1, min(int(limit), self.MAX_CATALOG_CANDIDATE_ROWS))]

    def _catalog_snapshot_by_id(self, snapshot_id: Any) -> dict[str, Any]:
        snapshot = next((row for row in self.catalog_snapshots.values()
                         if row["id"] == str(snapshot_id)), None)
        if snapshot is None:
            raise AppError("CATALOG_SNAPSHOT_INVALID", "invalid catalog snapshot", 400)
        return snapshot

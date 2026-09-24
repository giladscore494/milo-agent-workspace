"""In-memory Repository implementation for isolated E2E stacks.

Test-only: mirrors the SupabaseRepository authorization semantics
(membership scoping, 404 without existence disclosure, idempotency,
launch state) against process-local dictionaries. Never used by
production entrypoints.
"""

from __future__ import annotations

import copy
import json
import re
import threading
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping
from uuid import UUID, uuid4

from backend.catalog.contracts import (CANDIDATE_STATUSES, CATALOG_SOURCE_FAMILIES,
                                       CANONICAL_DIMENSION_PREFIX,
                                       MAX_CATALOG_WRITE_BATCH,
                                       MAX_PROMOTIONS_PER_RUN, candidate_identity_scope,
                                       claim_entity_key, record_locator_id,
                                       stated_canonical_fields,
                                       stated_identity_dimensions, stated_source_locator,
                                       trust_state_for)
from backend.catalog.diff import MAX_DIFF_ITEMS, diff_rows
from backend.catalog.digest import catalog_payload_digest
from backend.engines.swarm_v2.current_verdict import (CurrentVerdict,
                                                      contested_claim_ids,
                                                      resolve_current_verdict,
                                                      superseded_claim_ids)
from backend.engines.swarm_v2.evidence_bounds import FRAGMENT_TYPES
from backend.engines.swarm_v2.evidence_contracts import (fragment_type_for,
                                                         parse_locator_key)
from backend.engines.swarm_v2.fragments import fragment_content_hash
from backend.engines.swarm_v2.support import VERIFICATION_MODES, VERIFIER_CONTRACT_VERSION
from backend.catalog.scope import contract as work_scope_contract
from backend.catalog.payloads import (CANONICAL_VARIANT_KEY_FIELDS, prepare_candidate,
                                      prepare_evidence_link, prepare_promotion,
                                      prepare_raw_record, prepare_snapshot)
from backend.errors import AppError, NotFoundError
from backend.runtime import (RUN_STATES, InvalidTransition, cancellation_refusal,
                             validate_transition)
from backend.schemas import normalize_conversation_title


def _now() -> str:
    return datetime.now(UTC).isoformat()


#: The pinned WLTP resource, as the preparation SQL pins it.
_WLTP_RESOURCE_ID = "142afde2-6228-49f9-8a29-9b6c3a0cbe40"
_HEX64 = re.compile(r"[0-9a-f]{64}")


def _declared_scope_key(row: Mapping[str, Any]) -> tuple[str, str] | None:
    """What the scope predicate of the snapshot listing sees in one row.

    None when `retrieval_metadata` carries no `capture_scope` key at all;
    ("key", scope_key) for a declaration stating a string `scope_key`; and
    ("declared", "") for any other declaration -- present, so excluded from an
    unscoped listing, but matching no requested key."""
    metadata = row.get("retrieval_metadata")
    if not isinstance(metadata, Mapping) or "capture_scope" not in metadata:
        return None
    declaration = metadata.get("capture_scope")
    if isinstance(declaration, Mapping) and isinstance(declaration.get("scope_key"), str):
        return ("key", declaration["scope_key"])
    return ("declared", "")


def _variant_page_key(row: Mapping[str, Any]) -> tuple:
    """The order `catalog_candidate_variant_page` returns candidates in.

    Collation-free by construction: PostgreSQL orders those text columns with
    `collate "C"`, which is codepoint order -- exactly what Python compares
    strings with -- so the two implementations agree on Hebrew marque names
    rather than on whatever locale the server happens to run under.
    """
    return (row["manufacturer"], row["commercial_model"],
            row.get("model_year_start") or 0, row.get("model_year_end") or 0,
            row.get("official_model_code") or "", row.get("trim") or "",
            row["candidate_key"])


def _scope_identity(scope: Mapping[str, Any]) -> str:
    """One comparable rendering of a claim's scope, order-independent.

    PostgreSQL compares `jsonb` by value, so `{"a":1,"b":2}` and `{"b":2,"a":1}`
    are one scope there. Canonical JSON is how a dictionary says the same
    thing, and it keeps this mirror from refusing a promotion PostgreSQL would
    accept.
    """
    return json.dumps(scope, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


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
        # A strictly increasing write sequence for durable VERDICT rows.
        # PostgreSQL stamps `claim_verdicts.created_at` with the transaction
        # clock, and the CURRENT verdict of a claim is the latest one by that
        # column (`backend/engines/swarm_v2/current_verdict.py`). Separate
        # calls here are separate transactions there, so a monotonic counter is
        # the faithful mirror -- and a faithful one matters: without it every
        # verdict of a claim would tie, and "the newer verdict wins" could not
        # be tested against this backend at all.
        self._verdict_sequence: int = 0
        # The durable catalog namespace (PR1). Keyed by the same idempotency
        # identities the guarded RPCs use, so a replay collapses here exactly
        # as it does in PostgreSQL.
        self.catalog_snapshots: dict[str, dict[str, Any]] = {}       # snapshot_key
        self.catalog_raw_records: dict[tuple[str, str], dict[str, Any]] = {}
        self.catalog_candidates: dict[tuple[str, str], dict[str, Any]] = {}
        self.catalog_evidence_links: dict[tuple[str, str], dict[str, Any]] = {}
        # Snapshot adoptions (`20260924000200_catalog_ingestion_recovery.sql`):
        # append-only, written only by `adopt_catalog_snapshot`.
        self.catalog_snapshot_adoptions: list[dict[str, Any]] = []
        # Canonical state (PR3). Written ONLY by `promote_catalog_variant`,
        # which mirrors the promotion transaction: the canonical identity and
        # every field's provenance are created together or not at all, and a
        # canonical row that states a field with no verified provenance is
        # refused here exactly as the deferred constraint trigger refuses it in
        # PostgreSQL.
        self.catalog_models: list[dict[str, Any]] = []
        self.catalog_model_variants: list[dict[str, Any]] = []
        self.catalog_canonical_field_provenance: list[dict[str, Any]] = []
        self.checkpoints: list[dict[str, Any]] = []
        # Mapping plans (`20260922000100_catalog_work_scopes.sql`): one row per
        # plan and an append-only revision list, written only by the two
        # methods that mirror `create_work_scope` / `revise_work_scope`.
        self.work_scopes: dict[str, dict[str, Any]] = {}
        self.work_scope_revisions: list[dict[str, Any]] = []
        # Plan preparation (`20260923000100_catalog_work_scope_preparation.sql`):
        # append-only, written only by the mirror of `prepare_work_scope_queue`
        # and, for bindings, of `bind_work_scope_batch_run`.
        self.work_scope_preparations: dict[str, dict[str, Any]] = {}
        self.work_scope_units: list[dict[str, Any]] = []
        self.work_scope_batches: list[dict[str, Any]] = []
        self.work_scope_queue_items: list[dict[str, Any]] = []
        self.work_scope_batch_runs: list[dict[str, Any]] = []
        # Pause / resume (`20260924000100_catalog_work_scope_batch_runs.sql`):
        # append-only, alternating, written only by `set_work_scope_paused`.
        self.work_scope_controls: list[dict[str, Any]] = []

    # -- seeding -------------------------------------------------------------
    def seed_user(self, user_id: str) -> None:
        self.users.add(user_id)

    #: The configuration the canonical V1 project is seeded with in
    #: `001_project_workspace.sql` -- and carries in Production. A V1 project's
    #: configuration is the trusted source of what its runs map
    #: (`backend/vehicle_catalog_scope.py`), so a seeded V1 project states one
    #: exactly as the real one does.
    CANONICAL_V1_CONFIGURATION: dict[str, Any] = {
        "manufacturer": "Hyundai", "market": "Israel",
        "period": {"from": "2010", "to": "June 2026"},
    }

    def seed_project(self, project_id: str, slug: str, name: str, members: list[str],
                     workflow_key: str = "vehicle_catalog_v1",
                     configuration: dict[str, Any] | None = None) -> None:
        # `workflow_key` is TRUSTED project state: the frontend selects its run
        # and result surfaces from it and never from a payload. It defaults to
        # the V1 engine, so every existing caller is unchanged. So is
        # `configuration`: a V1 project defaults to the canonical scope, and a
        # test that needs an unconfigured one says so explicitly.
        if configuration is None:
            configuration = (copy.deepcopy(self.CANONICAL_V1_CONFIGURATION)
                             if workflow_key == "vehicle_catalog_v1" else {})
        self.projects[project_id] = {
            "id": project_id, "slug": slug, "name": name, "description": None,
            "workflow_key": workflow_key, "configuration": configuration,
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
        # Production parity with SupabaseRepository.create_queued_run: Console 6
        # makes immutable identity an INSERT-time property, so the split
        # message + run writer is a refusal-only compatibility method here too.
        # A test that wants a run goes through `create_message_and_run`, the
        # single atomic creator, exactly as the product does.
        raise AppError(
            "RUN_IDENTITY_ATOMIC_CREATION_REQUIRED",
            "queued runs must be created atomically with immutable identity",
            503,
        )

    def find_run_by_idempotency(self, conversation_id: UUID, user_id: UUID, idempotency_key: str) -> dict[str, Any] | None:
        for run in self.runs.values():
            if run["conversation_id"] == str(conversation_id) and run.get("requested_by") == str(user_id) and run.get("idempotency_key") == idempotency_key:
                return dict(run)
        return None

    def create_message_and_run(self, conversation_id: UUID, content: str, metadata: dict[str, Any], requested_by: UUID, idempotency_key: str | None, request_fingerprint: str, max_user_active: int | None = None, max_project_active: int | None = None, *, run_id: UUID, run_identity: dict[str, Any]) -> dict[str, Any]:
        # Mirrors Console 6's atomic creator: replay lookup, admission, message,
        # run and immutable identity all settle under one lock.
        with self.lock:
            if not request_fingerprint:
                raise AppError(
                    "IDEMPOTENCY_FINGERPRINT_REQUIRED",
                    "run creation requires a request fingerprint",
                    409,
                )
            if idempotency_key:
                existing = self.find_run_by_idempotency(conversation_id, requested_by, idempotency_key)
                if existing is not None:
                    if existing.get("request_fingerprint") != request_fingerprint:
                        raise AppError(
                            "IDEMPOTENCY_CONFLICT",
                            "idempotency key was already used with a different payload",
                            409,
                        )
                    return {"run": existing, "created": False}
            if max_user_active is not None and self.count_active_runs_for_user(requested_by) >= max_user_active:
                raise AppError("USER_CONCURRENCY_LIMIT", "too many active runs for this user", 429)
            project_id = self._conversation_project(conversation_id)
            project = self.projects.get(str(project_id)) or {}
            if run_identity.get("run_id") != str(run_id):
                raise AppError("RUN_IDENTITY_INVALID", "identity names a different run", 409)
            if run_identity.get("workflow_key") == "operator_capture":
                if metadata.get("milo_operation") != "catalog.government.capture":
                    raise AppError("RUN_IDENTITY_INVALID", "operator capture identity requires capture marker", 409)
            elif run_identity.get("workflow_key") != project.get("workflow_key"):
                raise AppError("RUN_IDENTITY_WORKFLOW_DRIFT", "project workflow changed before run creation", 409)
            if max_project_active is not None and self.count_active_runs_for_project(project_id) >= max_project_active:
                raise AppError("PROJECT_CONCURRENCY_LIMIT", "too many active runs for this project", 429)
            message = self.create_user_message(conversation_id, content, metadata)
            run = {
                "id": str(run_id),
                "conversation_id": str(conversation_id),
                "status": "queued",
                "attempt": 1,
                "launch_state": "pending",
                "requested_by": str(requested_by),
                "idempotency_key": idempotency_key,
                "request_fingerprint": request_fingerprint,
                "input": {"message_id": str(message["id"]), "content": content, "metadata": metadata},
                "output": None,
                "error": None,
                "usage": {},
                "run_identity": dict(run_identity),
                "created_at": _now(),
                "updated_at": _now(),
            }
            self.runs[run["id"]] = run
            return {"run": dict(run), "created": True}

    def try_acquire_launch(self, run_id: UUID) -> dict[str, Any] | None:
        with self.lock:
            run = self.runs.get(str(run_id))
            if run is None or run["status"] != "queued" or run.get("launch_state") not in {"pending", "launch_failed"}:
                return None
            # `runs_set_updated_at` stamps every UPDATE in the database.
            run.update(launch_state="launching", updated_at=_now())
            return dict(run)

    def set_launch_state(self, run_id: UUID, state: str, error: dict[str, Any] | None = None) -> dict[str, Any]:
        run = self.runs[str(run_id)]
        run.update(launch_state=state, updated_at=_now())
        if state == "launched":
            run["launched_at"] = _now()
        if error is not None:
            run["launch_error"] = error
        return dict(run)

    #: `reconcile_lost_launch`'s floor: no lost launch is reconciled sooner.
    LOST_LAUNCH_MIN_QUIET_SECONDS = 900
    #: The only event the API writes about a queued run before a worker claims it.
    LOST_LAUNCH_API_EVENTS = frozenset({"launch_failed"})

    def reconcile_lost_launch(self, run_id: UUID, *, outcome: str, min_quiet_seconds: int,
                              operator: str) -> dict[str, Any]:
        """Mirrors `reconcile_lost_launch`: an operator's guarded decision on a
        launch that was lost before any worker claimed the run -- `launched`
        (an execution exists) or `not_launched` (none does, and nothing but the
        API ever wrote about the run). It never launches anything:
        `not_launched` returns the run to `launch_failed`, the state the launch
        compare-and-set can take again."""
        if outcome not in ("launched", "not_launched") or not isinstance(operator, str) \
                or not operator.strip() or len(operator) > 200:
            raise AppError("LOST_LAUNCH_INVALID", "invalid lost-launch reconciliation", 422)
        if not isinstance(min_quiet_seconds, int) or isinstance(min_quiet_seconds, bool) \
                or min_quiet_seconds < self.LOST_LAUNCH_MIN_QUIET_SECONDS:
            raise AppError("LOST_LAUNCH_THRESHOLD_TOO_SHORT",
                           "a lost launch is never reconciled sooner than the floor", 422)
        target = "launched" if outcome == "launched" else "launch_failed"
        with self.lock:
            run = self.runs.get(str(run_id))
            if run is None:
                raise NotFoundError("run", str(run_id))
            if run.get("launch_state") == target:
                return {"reconciled": False, "run_id": run["id"], "status": run["status"],
                        "launch_state": target}
            if run["status"] != "queued" or run.get("launch_state") != "launching":
                raise AppError("LOST_LAUNCH_WRONG_STATE", "the run is not a lost launch", 409)
            if any(run.get(field) for field in ("worker_id", "lease_token", "lease_expires_at",
                                                "started_at", "finished_at")):
                raise AppError("LOST_LAUNCH_CLAIMED", "a worker claimed the run", 409)
            quiet_since = datetime.fromisoformat(str(run.get("updated_at") or run["created_at"]))
            if quiet_since > datetime.now(UTC) - timedelta(seconds=min_quiet_seconds):
                raise AppError("LOST_LAUNCH_NOT_QUIET", "the run changed too recently", 409)
            rid = run["id"]
            traced = (any(row.get("run_id") == rid for row in self.invocations)
                      or any(row.get("run_id") == rid for row in self.checkpoints)
                      or bool(run.get("last_heartbeat_at"))
                      or any(row.get("run_id") == rid for row in getattr(self, "usage_ledger", []))
                      or rid in self.__dict__.get("run_usage_ledgers", {})
                      or any(event["run_id"] == rid
                             and event["event_type"] not in self.LOST_LAUNCH_API_EVENTS
                             for event in self.run_events))
            if outcome == "not_launched" and traced:
                raise AppError("LOST_LAUNCH_TRACED", "more than the API wrote about the run", 409)
            run.update(launch_state=target, updated_at=_now())
            if outcome == "not_launched":
                run["launch_error"] = {
                    "code": "RUN_LAUNCH_LOST",
                    "message": "launch ownership was taken but its outcome was never recorded; "
                               "an operator verified that no worker was started",
                    "reconciled_by": operator}
                self.append_run_event(UUID(rid), "launch_failed", {
                    "message": "The worker launch was never confirmed and an operator verified "
                               "that no worker was started; the run remains queued and can be "
                               "launched again",
                    "payload": {"recoverable": True, "reconciled": True,
                                "previous_launch_state": "launching"}})
            return {"reconciled": True, "run_id": rid, "status": run["status"],
                    "launch_state": target, "previous_launch_state": "launching"}

    #: The only events the API writes about a run no worker was started for.
    UNLAUNCHED_RUN_API_EVENTS = frozenset({"launch_failed", "cancellation_requested"})
    UNLAUNCHED_RUN_MESSAGE = "No worker was ever started for this run; an operator retired it"

    def retire_unlaunched_run(self, run_id: UUID, *, min_quiet_seconds: int,
                              operator: str) -> dict[str, Any]:
        """Mirrors `retire_unlaunched_run`: an operator's guarded retirement of a
        run whose launch is KNOWN not to have started a worker (`pending`,
        `launch_failed`), proven unclaimed, untraced and quiet. It ends the run
        as the canonical finalizer ends a cancellation -- the terminal state and
        its `run_cancelled` event together -- and never launches anything."""
        if not isinstance(operator, str) or not operator.strip() or len(operator) > 200:
            raise AppError("UNLAUNCHED_RUN_INVALID", "invalid retirement", 422)
        if not isinstance(min_quiet_seconds, int) or isinstance(min_quiet_seconds, bool) \
                or min_quiet_seconds < self.LOST_LAUNCH_MIN_QUIET_SECONDS:
            raise AppError("UNLAUNCHED_RUN_THRESHOLD_TOO_SHORT",
                           "a run is never retired sooner than the floor", 422)
        with self.lock:
            run = self.runs.get(str(run_id))
            if run is None:
                raise NotFoundError("run", str(run_id))
            if run["status"] == "cancelled" and (run.get("error") or {}).get("code") == "RUN_NOT_LAUNCHED":
                return {"retired": False, "run_id": run["id"], "status": run["status"]}
            if run["status"] not in {"queued", "cancellation_requested"} \
                    or run.get("launch_state") not in {"pending", "launch_failed"}:
                raise AppError("UNLAUNCHED_RUN_WRONG_STATE", "the run's launch is not known to have failed", 409)
            if any(run.get(field) for field in ("worker_id", "lease_token", "lease_expires_at",
                                                "started_at", "finished_at")):
                raise AppError("UNLAUNCHED_RUN_CLAIMED", "a worker claimed the run", 409)
            rid = run["id"]
            traced = (any(row.get("run_id") == rid for row in self.invocations)
                      or any(row.get("run_id") == rid for row in self.checkpoints)
                      or bool(run.get("last_heartbeat_at"))
                      or any(row.get("run_id") == rid for row in getattr(self, "usage_ledger", []))
                      or rid in self.__dict__.get("run_usage_ledgers", {})
                      or any(event["run_id"] == rid
                             and event["event_type"] not in self.UNLAUNCHED_RUN_API_EVENTS
                             for event in self.run_events))
            if traced:
                raise AppError("UNLAUNCHED_RUN_TRACED", "more than the API wrote about the run", 409)
            quiet_since = datetime.fromisoformat(str(run.get("updated_at") or run["created_at"]))
            if quiet_since > datetime.now(UTC) - timedelta(seconds=min_quiet_seconds):
                raise AppError("UNLAUNCHED_RUN_NOT_QUIET", "the run changed too recently", 409)
            previous = run["status"]
            now = _now()
            if previous == "queued":
                run.update(status="cancellation_requested", cancellation_requested_at=now,
                           cancellation_reason="retired by an operator: no worker was ever started",
                           updated_at=now)
            run.update(status="cancelled", finished_at=now, updated_at=now,
                       error={"code": "RUN_NOT_LAUNCHED", "message": self.UNLAUNCHED_RUN_MESSAGE})
            self.append_run_event(UUID(rid), "run_cancelled", {
                "message": self.UNLAUNCHED_RUN_MESSAGE, "payload": {"code": "RUN_NOT_LAUNCHED"}})
            return {"retired": True, "run_id": rid, "status": "cancelled",
                    "previous_status": previous}

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

    TERMINAL_EVENT_TYPES = ("run_completed", "run_partial_success", "run_failed", "run_cancelled")

    def terminal_run_event(self, run_id: UUID) -> dict[str, Any] | None:
        events = [e for e in self.run_events
                  if e["run_id"] == str(run_id) and e["event_type"] in self.TERMINAL_EVENT_TYPES]
        return dict(events[-1]) if events else None

    def terminal_run_events(self, run_ids: list[UUID]) -> dict[str, dict[str, Any]]:
        wanted = {str(run_id) for run_id in run_ids}
        latest: dict[str, dict[str, Any]] = {}
        for event in self.run_events:
            if event["run_id"] in wanted and event["event_type"] in self.TERMINAL_EVENT_TYPES:
                latest[event["run_id"]] = dict(event)  # later rows overwrite: newest wins
        return latest

    def list_conversation_runs(self, conversation_id: UUID, user_id: UUID | None = None, limit: int = 20) -> list[dict[str, Any]]:
        self.get_conversation(conversation_id, user_id)
        bounded = max(1, min(int(limit), 50))
        rows = [run for run in self.runs.values() if run["conversation_id"] == str(conversation_id)]
        rows.sort(key=lambda run: str(run.get("created_at") or ""), reverse=True)
        projected = []
        for run in rows[:bounded]:
            row = {key: value for key, value in run.items() if key not in ("output", "input", "error")}
            projected.append(row)
        return projected

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
            if fields.get("usage") is not None:
                # Parity with transition_run_worker_guarded: a terminal usage
                # snapshot merges into the aggregate, it never lowers it.
                from backend.execution_usage import merge_usage_snapshots

                fields = {**fields, "usage": merge_usage_snapshots(run.get("usage"), fields["usage"])}
            run.update({"status": status, "updated_at": _now(), **fields})
            return dict(run)

    def finalize_run(self, run_id: UUID, status: str, expected_status: str, event: dict[str, Any] | None = None, *, worker_id: str, attempt: int | None, lease_token: str | None, **fields: Any) -> dict[str, Any]:
        """Parity with `finalize_run_guarded` (migration 20260920000200).

        The transition and the terminal event are ONE step under the lock:
        every check runs before anything is mutated, so a rejected
        finalization leaves neither a status change nor an event behind, and
        an accepted one leaves both. The compare-and-set on `expected_status`
        is mandatory, exactly as in the database.
        """
        from backend.execution_usage import merge_usage_snapshots
        from backend.runtime import TERMINAL_STATES

        with self.lock:
            run = self.runs.get(str(run_id))
            if run is None:
                raise NotFoundError("run", str(run_id))
            if status not in TERMINAL_STATES:
                raise AppError("INVALID_RUN_TRANSITION", f"{status!r} is not a terminal run status", 409)
            self._assert_active_lease(run, worker_id, attempt, lease_token)
            if run["status"] != expected_status:
                # The state the decision was taken under has moved; the
                # database rejects this with STALE_WORKER_WRITE, which the
                # repository surfaces as a lease-class conflict.
                raise AppError("RUN_TRANSITION_CONFLICT", "run status moved before finalization", 409)
            try:
                validate_transition(run["status"], status)
            except InvalidTransition as exc:
                raise AppError("INVALID_RUN_TRANSITION", str(exc), 409) from exc
            if event is not None and not isinstance(event.get("payload", {}), dict):
                raise AppError("REPOSITORY_ERROR", "a terminal event payload must be an object", 502)
            if fields.get("usage") is not None:
                fields = {**fields, "usage": merge_usage_snapshots(run.get("usage"), fields["usage"])}
            run.update({"status": status, "updated_at": _now(),
                        "finished_at": fields.get("finished_at") or run.get("finished_at") or _now(),
                        **{k: v for k, v in fields.items() if k != "finished_at"}})
            if event is not None:
                self.run_events.append({
                    "id": len(self.run_events) + 1,
                    "run_id": str(run_id),
                    "event_type": event.get("type"),
                    "payload": event.get("payload") or {},
                    "message": event.get("message"),
                    "agent": None,
                    "phase": None,
                    "progress": None,
                    "created_at": _now(),
                })
            return dict(run)

    def request_cancellation(self, run_id: UUID, reason: str | None = None) -> dict[str, Any]:
        """Parity with the Supabase compare-and-set: a cancellation is accepted
        only for a run a worker will finalize, decided under the same lock as
        the write."""
        with self.lock:
            run = self.runs.get(str(run_id))
            if run is None:
                raise NotFoundError("run", str(run_id))
            refusal = cancellation_refusal(run["status"], run.get("launch_state"))
            if refusal is not None:
                raise AppError(refusal, "a cancellation of this run could never be finalized", 409)
            return self.transition_run(run_id, "cancellation_requested",
                                       cancellation_requested_at=_now(),
                                       cancellation_reason=reason)

    def mark_run_failed(self, run_id: UUID, code: str, message: str, worker_id: str | None = None, attempt: int | None = None, lease_token: str | None = None) -> dict[str, Any]:
        return self.transition_run(run_id, "failed", expected_worker_id=worker_id, expected_attempt=attempt, expected_lease_token=lease_token, error={"code": code, "message": message}, finished_at=_now())

    def mark_run_complete(self, run_id: UUID, output: dict[str, Any], worker_id: str | None = None, attempt: int | None = None, lease_token: str | None = None) -> dict[str, Any]:
        return self.transition_run(run_id, "completed", expected_worker_id=worker_id, expected_attempt=attempt, expected_lease_token=lease_token, output=output, error=None, finished_at=_now())

    def record_run_invocation(self, run_id: UUID, invocation: dict[str, Any]) -> dict[str, Any]:
        row = {"id": str(uuid4()), "run_id": str(run_id), **invocation}
        self.invocations.append(row)
        return dict(row)

    def update_run_usage(self, run_id: UUID, usage: dict[str, Any], worker_id: str | None = None, attempt: int | None = None, lease_token: str | None = None) -> dict[str, Any]:
        # Parity with update_run_usage_guarded (migration 20260920000100):
        # the aggregate is MERGED component-wise, never overwritten, so a
        # snapshot that is merely behind cannot lower a counter.
        from backend.execution_usage import merge_usage_snapshots

        with self.lock:
            run = self.runs[str(run_id)]
            if worker_id is not None:
                self._assert_active_lease(run, worker_id, attempt, lease_token)
            run["usage"] = merge_usage_snapshots(run.get("usage"), usage)
            return dict(run)

    def record_run_usage(self, run_id: UUID, ledger: dict[str, Any], *, worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]:
        """Parity with record_run_usage_guarded: lease-guarded, merging,
        versioned (a duplicate never advances the version), and projecting
        the public aggregate into runs.usage in the same step."""
        from backend.execution_usage import merge_usage_snapshots, public_usage_projection

        with self.lock:
            run = self.runs.get(str(run_id))
            if run is None:
                raise NotFoundError("run", str(run_id))
            self._assert_active_lease(run, worker_id, attempt, lease_token)
            if not isinstance(ledger, dict):
                raise AppError("REPOSITORY_ERROR", "guarded persistence operation failed", 502)
            ledgers = self.__dict__.setdefault("run_usage_ledgers", {})
            existing = ledgers.get(str(run_id)) or {
                "run_id": str(run_id), "schema_version": 1, "version": 0,
                "attempt": int(attempt or 1), "worker_id": worker_id, "ledger": {},
                "created_at": _now(), "updated_at": _now(),
            }
            incoming = {k: v for k, v in ledger.items() if k != "ledger_version"}
            merged = merge_usage_snapshots(existing["ledger"], incoming)
            if merged != existing["ledger"] or existing["version"] == 0:
                version = existing["version"] + 1
                merged["ledger_version"] = version
                merged["schema_version"] = max(int(merged.get("schema_version", 1)), int(existing["schema_version"]))
                existing = {**existing, "ledger": merged, "version": version,
                            "schema_version": merged["schema_version"],
                            "attempt": max(int(existing["attempt"]), int(attempt or 1)),
                            "worker_id": worker_id, "updated_at": _now()}
            ledgers[str(run_id)] = existing
            run["usage"] = merge_usage_snapshots(run.get("usage"), public_usage_projection(existing["ledger"]))
            return {**existing, "ledger": dict(existing["ledger"])}

    def get_run_usage_ledger(self, run_id: UUID) -> dict[str, Any] | None:
        row = self.__dict__.get("run_usage_ledgers", {}).get(str(run_id))
        return {**row, "ledger": dict(row["ledger"])} if row else None

    def append_usage_ledger(self, entry: dict[str, Any], *,
                            worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]:
        """Parity with `append_usage_ledger_guarded` (migration 20260921000200).

        The per-call rows `sum_daily_ledger_cost` reads were the last durable
        write a running worker made unfenced, so a replaced worker could keep
        charging a run it no longer owned against the live worker's daily
        allowance. The run id comes from the FENCED argument, so an entry
        naming another run cannot charge one.
        """
        run_id = entry.get("run_id")
        if not run_id:
            raise AppError("REPOSITORY_ERROR", "a usage ledger entry requires its run id", 502)
        self._evidence_lease(UUID(str(run_id)), worker_id, attempt, lease_token)
        return self._append_usage_ledger_row(entry)

    def _append_usage_ledger_row(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Write the row itself, once the caller's authority is settled."""
        run_id = entry.get("run_id")
        if not run_id:
            raise AppError("REPOSITORY_ERROR", "a usage ledger entry requires its run id", 502)
        if not hasattr(self, "usage_ledger"):
            self.usage_ledger = []
        row = {"id": len(self.usage_ledger) + 1, "created_at": _now(),
               **entry, "run_id": str(run_id)}
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
            # `claim_run_lease` (migration 20260921000200) predicates on
            # `run_identity is not null`: a legacy identity-less run matches no
            # rows, stays readable history, and is never executed. Same
            # outcome here, same code, same status.
            if run.get("run_identity") is None:
                raise AppError("RUN_ALREADY_CLAIMED", "run is already claimed by another worker", 409)
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
            entry = {"run_id": str(run_id), "call_seq": call_seq, "user_id": user_id,
                     "project_id": project_id, "decision": status, "status": status,
                     "estimated_cost": amount, "rejection_reason": reason}
            if worker_id is None:
                # Parity with production, which takes the UNGUARDED
                # reserve_model_call_budget_v2 path when the caller states no
                # lease. The lease was already asserted above when one was
                # given, so the row is written directly either way rather than
                # re-entering the fenced writer with a lease it may not have.
                return self._append_usage_ledger_row(entry)
            return self.append_usage_ledger(entry, worker_id=worker_id, attempt=attempt,
                                            lease_token=lease_token)

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

    def _replay_or_append(self, run_id: UUID, payload: dict[str, Any], kind: str, *,
                          conflicts: tuple[tuple[str, str], ...]) -> dict[str, Any]:
        """Return the stored row for an exact replay, or append a new one.

        Mirrors the `on conflict (run_id, evidence_key) where evidence_key is
        not null do nothing` shape both R3 upserts use: the same key returns
        the same row, and a replay that disagrees on a field naming WHERE the
        evidence came from fails closed rather than being merged.

        A payload with NO `evidence_key` has no replay identity and is appended
        -- which is the partial index's own behaviour, and keeps every
        pre-R3 caller that passes none working exactly as it did.
        """
        key = payload.get("evidence_key")
        if not key:
            return self._tool_row(run_id, payload, kind=kind)
        existing = next((row for row in self.tool_rows
                         if str(row.get("run_id")) == str(run_id)
                         and row.get("evidence_key") == key
                         and self.evidence_kinds.get(str(row.get("id"))) == kind), None)
        if existing is None:
            return self._tool_row(run_id, payload, kind=kind)
        for field, message in conflicts:
            if str(existing.get(field) or "") != str(payload.get(field) or ""):
                raise AppError(f"{kind.upper()}_IDEMPOTENCY_CONFLICT", message, 409)
        return dict(existing)

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

    def create_tool_access_request(self, run_id: UUID, request: dict[str, Any], *,
                                   worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]:
        """Parity with `create_tool_access_request_guarded` (20260921000200).

        Both of these were direct inserts on either side, so a worker whose
        lease had been reclaimed could still open tool access on a run it no
        longer owned. The complete lease is now required here exactly as the
        RPC requires it: a missing component is a refusal, never a skipped
        check.
        """
        self._evidence_lease(run_id, worker_id, attempt, lease_token)
        return self._tool_row(run_id, request)

    def create_tool_grant(self, run_id: UUID, grant: dict[str, Any], *,
                          worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]:
        self._evidence_lease(run_id, worker_id, attempt, lease_token)
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
        # `upsert_source_guarded` is idempotent on `(run_id, evidence_key)`, so
        # a RESUMED run replays onto the stored row instead of appending a
        # second source for the same acquisition. Mirrored here because an
        # offline resume proof that duplicated sources would be proving the
        # opposite of what PostgreSQL does.
        #
        # The partial index is `where evidence_key is not null`: a row with no
        # key has no replay identity and is appended, exactly as before.
        return self._replay_or_append(
            run_id, source, "source",
            conflicts=(("source_version_kind", "source version identity conflict"),
                       ("source_version_id", "source version identity conflict")))

    def create_claim(self, run_id: UUID, claim: dict[str, Any], **lease: Any) -> dict[str, Any]:
        # COMPATIBILITY BOUNDARY, unchanged and documented: these predate the
        # lease contract and many callers pass none. A lease that IS supplied
        # is enforced completely -- partial is refused like any other -- but an
        # omitted one is still permitted here, unlike the three durable
        # evidence writers above.
        if lease:
            self._evidence_lease(run_id, lease.get("worker_id"), lease.get("attempt"),
                                 lease.get("lease_token"))
        # `create_claim_with_source_guarded` is idempotent on the same identity,
        # and refuses a replay that would move a stored fact to a different
        # source, a different location or a different scope.
        return self._replay_or_append(
            run_id, claim, "claim",
            conflicts=(("source_id", "idempotency key belongs to a different source"),
                       ("evidence_locator", "claim evidence locator mismatch"),
                       ("canonical_scope_hash", "claim canonical scope identity mismatch")))

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
        # The write ORDER, made durable. `created_at` is not part of the replay
        # identity (below), so a replayed verdict keeps the timestamp of the
        # row it collapses onto -- exactly as `on conflict do nothing` does.
        stamped = {**verdict, "created_at": self._next_verdict_timestamp()}
        return self._replayable_evidence_row(run_id, key, stamped, "claim_verdict",
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

    # --- CURRENT verdict authority (R5) -------------------------------------
    #
    # Mirrors `public.claim_current_verdict_state` /
    # `public.claim_current_verdict_states`, and mirrors them by CALLING the
    # same rule: `backend/engines/swarm_v2/current_verdict.py` is the one
    # definition, and the SQL function implements exactly it. There is no
    # second copy of the resolution here to drift.

    def _next_verdict_timestamp(self) -> str:
        """A strictly increasing durable write time for one verdict row.

        A real ISO timestamp, so the resolution reads it exactly as it reads
        `claim_verdicts.created_at`. The sequence offset is what makes it
        STRICTLY increasing: the wall clock can report the same microsecond
        twice, and two verdicts that tie here would be resolved by the
        fail-closed tiebreak rather than by the order they were written.
        """
        self._verdict_sequence += 1
        return (datetime.now(UTC) + timedelta(microseconds=self._verdict_sequence)).isoformat()

    def _verdict_rows(self, claim_id: Any) -> list[dict[str, Any]]:
        return [dict(row) for row in self.tool_rows
                if self.evidence_kinds.get(str(row.get("id"))) == "claim_verdict"
                and str(row.get("claim_id")) == str(claim_id)]

    def _claim_row(self, claim_id: Any) -> dict[str, Any] | None:
        return next((dict(row) for row in self.tool_rows
                     if self.evidence_kinds.get(str(row.get("id"))) == "claim"
                     and str(row.get("id")) == str(claim_id)), None)

    def _current_verdict(self, claim_id: Any) -> CurrentVerdict | None:
        """The resolved CURRENT state of one claim, or None when unknown.

        An unknown claim resolves to NOTHING rather than to "not verified":
        the SQL function returns no row for one, and a caller must never read
        the absence of a claim as a statement about a claim.
        """
        claim = self._claim_row(claim_id)
        if claim is None:
            return None
        return resolve_current_verdict(
            claim=claim, verdicts=self._verdict_rows(claim_id),
            superseded_claim_ids=superseded_claim_ids(
                row for row in self.tool_rows
                if self.evidence_kinds.get(str(row.get("id"))) == "conflict_resolution"),
            contested_claim_ids=contested_claim_ids(
                row for row in self.tool_rows
                if self.evidence_kinds.get(str(row.get("id"))) == "conflict"))

    def claim_current_verdict_states(self, run_id: UUID,
                                     claim_ids: Any = None, *,
                                     limit: int = 200) -> list[dict[str, Any]]:
        """Mirrors `public.claim_current_verdict_states`: one bounded READ."""
        with self.lock:
            wanted = None if claim_ids is None else {str(item) for item in claim_ids}
            rows = [row for row in self.tool_rows
                    if self.evidence_kinds.get(str(row.get("id"))) == "claim"
                    and str(row.get("run_id")) == str(run_id)
                    and (wanted is None or str(row.get("id")) in wanted)]
            resolved = []
            for claim in sorted(rows, key=lambda row: str(row["id"])):
                current = self._current_verdict(claim["id"])
                if current is not None:
                    resolved.append(current.as_row())
            return resolved[:max(0, min(int(limit), 500))]

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

    # -- ingestion recovery (20260924000200) ------------------------------------
    #
    # The batches apply the unchanged single-row writes to each row, in order,
    # ALL OR NOTHING -- the database runs them in one transaction, so a refusal
    # of any row leaves none of the batch behind here either.

    def _catalog_all_or_nothing(self, write):
        with self.lock:
            # Rows are only ever added, except a snapshot's counters and a
            # candidate's status, so row-level copies are a complete undo.
            saved = ({key: dict(row) for key, row in self.catalog_snapshots.items()},
                     dict(self.catalog_raw_records),
                     {key: dict(row) for key, row in self.catalog_candidates.items()})
            try:
                return write()
            except BaseException:
                (self.catalog_snapshots, self.catalog_raw_records,
                 self.catalog_candidates) = saved
                raise

    @staticmethod
    def _catalog_batch_snapshot(rows: Any) -> str:
        rows = list(rows or [])
        if not 1 <= len(rows) <= MAX_CATALOG_WRITE_BATCH or any(
                not isinstance(row, Mapping) for row in rows):
            raise AppError("CATALOG_BATCH_INVALID",
                           f"a catalog write batch holds 1 to {MAX_CATALOG_WRITE_BATCH} rows", 400)
        snapshots = {str(row.get("snapshot_id") or "") for row in rows}
        if len(snapshots) != 1 or "" in snapshots:
            raise AppError("CATALOG_BATCH_INVALID", "one snapshot per catalog write batch", 400)
        return snapshots.pop()

    def record_catalog_raw_records(self, run_id: UUID, records: Any, *, worker_id: str,
                                   attempt: int, lease_token: str) -> list[dict[str, Any]]:
        records = list(records or [])
        self._catalog_batch_snapshot(records)
        lease = {"worker_id": worker_id, "attempt": attempt, "lease_token": lease_token}
        return self._catalog_all_or_nothing(lambda: [
            self.record_catalog_raw_record(run_id, dict(record), **lease) for record in records])

    def record_catalog_candidates(self, run_id: UUID, candidates: Any, *, worker_id: str,
                                  attempt: int, lease_token: str) -> list[dict[str, Any]]:
        candidates = list(candidates or [])
        snapshot_id = self._catalog_batch_snapshot(candidates)
        lease = {"worker_id": worker_id, "attempt": attempt, "lease_token": lease_token}

        def write() -> list[dict[str, Any]]:
            self._catalog_lease(run_id, worker_id, attempt, lease_token)
            # Stricter than the single-row write, exactly as the batch RPC is:
            # readings land only on this run's own, still-pending capture.
            snapshot = self._catalog_owned_snapshot(snapshot_id, run_id)
            if snapshot["validation_state"] == "failed":
                raise AppError("CATALOG_SNAPSHOT_FAILED", "a failed catalog snapshot is terminal", 409)
            if snapshot["activated_at"] is not None:
                raise AppError("CATALOG_SNAPSHOT_ACTIVE", "an active catalog snapshot is immutable", 409)
            return [self.record_catalog_candidate(run_id, dict(candidate), **lease)
                    for candidate in candidates]
        return self._catalog_all_or_nothing(write)

    #: The metadata an adopter's capture must reproduce exactly.
    _CATALOG_ADOPTION_METADATA = ("capture_scope", "capture_contract", "page_chain_sha256",
                                  "normalization_contract", "normalized_record_count",
                                  "normalization_issue_count", "normalization_issues",
                                  "normalization_issue_records")

    def adopt_catalog_snapshot(self, run_id: UUID, snapshot: dict[str, Any], *,
                               worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]:
        """Mirror of `adopt_catalog_snapshot_guarded` and its adoption trigger."""
        refused = AppError("CATALOG_SNAPSHOT_ADOPTION_REFUSED",
                           "the database refused this operation", 409)
        with self.lock:
            self._catalog_lease(run_id, worker_id, attempt, lease_token)
            adopter = self.runs[str(run_id)]
            if (adopter.get("run_identity") or {}).get("workflow_key") != "operator_capture":
                raise refused
            snapshot = prepare_snapshot(snapshot)
            if "activated_at" in snapshot or "stored_record_count" in snapshot:
                raise AppError("CATALOG_SNAPSHOT_INVALID",
                               "catalog snapshot activation is not a caller-supplied field", 400)
            existing = self.catalog_snapshots.get(snapshot["snapshot_key"])
            if existing is None:
                raise refused
            self._catalog_replay(existing, snapshot, self._CATALOG_SNAPSHOT_IDENTITY, "SNAPSHOT")
            stored = existing.get("retrieval_metadata") or {}
            offered = snapshot.get("retrieval_metadata") or {}
            if any(stored.get(field) != offered.get(field)
                   for field in self._CATALOG_ADOPTION_METADATA):
                raise refused
            if existing["created_by_run_id"] == str(run_id):
                return dict(existing)
            previous = self.runs.get(existing["created_by_run_id"])
            now = _now()
            if (adopter.get("status") not in ("starting", "running")
                    or not adopter.get("lease_expires_at") or adopter["lease_expires_at"] <= now):
                raise refused
            if (previous is None or previous.get("status") not in ("failed", "cancelled", "timed_out")
                    or (previous.get("lease_expires_at") and previous["lease_expires_at"] > now)):
                raise refused
            if existing["activated_at"] is not None or existing["validation_state"] != "pending":
                raise refused
            if any(row["snapshot_id"] == existing["id"]
                   and row["previous_run_id"] == existing["created_by_run_id"]
                   for row in self.catalog_snapshot_adoptions):
                raise refused
            self.catalog_snapshot_adoptions.append({
                "id": str(uuid4()), "snapshot_id": existing["id"],
                "previous_run_id": existing["created_by_run_id"],
                "adopted_by_run_id": str(run_id), "previous_run_status": previous["status"],
                "stored_record_count_at_adoption": existing["stored_record_count"],
                "adopted_at": now})
            existing["created_by_run_id"] = str(run_id)
            return dict(existing)

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
                # R5: and what the claim says NOW. A verdict that some newer
                # verdict, contradiction or supersession has replaced is
                # history; a link may not be created citing it. Mirrors the
                # `catalog_candidate_evidence_links_current_verdict` trigger.
                current = self._current_verdict(claim["id"])
                if current is None or not current.authorizes(verdict_id):
                    raise AppError("CATALOG_LINK_VERDICT_NOT_CURRENT",
                                   "catalog evidence link verdict is not the claim's "
                                   "current supported verdict", 409)

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
                                      limit: int = MAX_CATALOG_SNAPSHOT_ROWS,
                                      capture_scope_key: str | None = None) -> list[dict[str, Any]]:
        """Mirror of the Supabase listing, INCLUDING its scope predicate.

        Without `capture_scope_key` only snapshots declaring NO capture scope
        are listed -- the database's `retrieval_metadata->capture_scope is
        null`, under which a present-but-null declaration is still a
        declaration. With one, only snapshots whose declaration states that
        exact `scope_key`."""
        with self.lock:
            rows = [dict(row) for row in self.catalog_snapshots.values()
                    if row.get("source_family") == str(source_family)
                    and row.get("activated_at") is not None
                    and (resource_id is None or row.get("resource_id") == str(resource_id))
                    and _declared_scope_key(row) == (
                        None if capture_scope_key is None else ("key", str(capture_scope_key)))]
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

    # --- bounded database-side catalog aggregation (PR3) ---------------------
    #
    # Mirrors of the reviewed RPCs in
    # `20260916090000_catalog_bounded_candidate_queries.sql`, including the
    # parts that MATTER for the query layer to be meaningful: the active +
    # complete + USABLE snapshot gate, the fixed codepoint ordering, the
    # explicit page bound and the EXACT total on every row.
    #
    # The ordering is Python's own string comparison, which is codepoint order,
    # and the SQL orders `collate "C"`, which for UTF-8 is byte order and
    # therefore the same order. The identity text here is Hebrew, so a
    # collation-dependent ordering would make the two disagree.
    MAX_CATALOG_AGGREGATE_ROWS = 200

    def _readable_snapshot(self, snapshot_id: Any, allow_incomplete: bool) -> dict[str, Any]:
        """The snapshot gate, applied here exactly as `catalog_readable_snapshot`
        applies it: active, complete, states a reading, and free of unresolved
        issues unless the caller has acknowledged them."""
        snapshot = self._catalog_snapshot_by_id(snapshot_id)
        if snapshot.get("activated_at") is None or snapshot.get("validation_state") != "complete":
            raise AppError("CATALOG_SNAPSHOT_NOT_ACTIVE", "catalog snapshot is not active", 409)
        metadata = snapshot.get("retrieval_metadata") or {}
        contract = metadata.get("normalization_contract")
        if not contract or contract == "raw_only":
            raise AppError("CATALOG_SNAPSHOT_NOT_READ",
                           "catalog snapshot states no readable identities", 409)
        issues = metadata.get("normalization_issue_count")
        if isinstance(issues, bool) or not isinstance(issues, int):
            raise AppError("CATALOG_SNAPSHOT_STATE_INVALID",
                           "catalog snapshot reading state is malformed", 409)
        if issues > 0 and not allow_incomplete:
            raise AppError("CATALOG_SNAPSHOT_INCOMPLETE",
                           "catalog snapshot holds rows its vocabulary could not read", 409)
        return snapshot

    def _snapshot_candidates(self, snapshot_id: Any) -> list[dict[str, Any]]:
        return [row for row in self.catalog_candidates.values()
                if row["snapshot_id"] == str(snapshot_id)]

    @staticmethod
    def _aggregate_page(items: list[dict[str, Any]], limit: int, offset: int) -> list[dict[str, Any]]:
        """Attach the EXACT total to every returned row, then cut the page.

        An EMPTY page still returns one row: the COUNT ROW the SQL aggregations
        emit, for the same reason they emit it -- an offset past the last
        matching row must report the total the filter matched, not the zero its
        emptiness would suggest.

        Its item columns are null where they are known. When the filter matched
        NOTHING AT ALL there is no row to take the column names from, so the
        count row carries only `total_count`; `is_count_row` in
        `backend/catalog/government/query.py` recognises both shapes, and no
        caller ever sees one -- `_read` drops it and keeps the total.
        """
        total = len(items)
        bounded = max(1, min(int(limit), MemoryRepository.MAX_CATALOG_AGGREGATE_ROWS))
        start = max(0, int(offset))
        page = items[start:start + bounded]
        if not page:
            columns = items[0] if items else {}
            return [{name: None for name in columns} | {"total_count": total}]
        return [{**row, "total_count": total} for row in page]

    def catalog_candidate_manufacturers(self, snapshot_id: Any, *, limit: int = 50,
                                        offset: int = 0,
                                        allow_incomplete: bool = False) -> list[dict[str, Any]]:
        with self.lock:
            self._readable_snapshot(snapshot_id, allow_incomplete)
            grouped: dict[str, dict[str, Any]] = {}
            for row in self._snapshot_candidates(snapshot_id):
                entry = grouped.setdefault(row["manufacturer"],
                                           {"manufacturer": row["manufacturer"],
                                            "_models": set(), "variant_count": 0,
                                            "ambiguous_variant_count": 0})
                entry["_models"].add(row["commercial_model"])
                entry["variant_count"] += 1
                entry["ambiguous_variant_count"] += 1 if row["status"] == "ambiguous" else 0
            items = [{"manufacturer": name, "model_count": len(entry.pop("_models")),
                      "variant_count": entry["variant_count"],
                      "ambiguous_variant_count": entry["ambiguous_variant_count"]}
                     for name, entry in sorted(grouped.items())]
            return self._aggregate_page(items, limit, offset)

    def catalog_candidate_models(self, snapshot_id: Any, *, manufacturer: str, limit: int = 50,
                                 offset: int = 0,
                                 allow_incomplete: bool = False) -> list[dict[str, Any]]:
        with self.lock:
            self._readable_snapshot(snapshot_id, allow_incomplete)
            if not str(manufacturer).strip():
                raise AppError("CATALOG_QUERY_INVALID", "a manufacturer is required", 400)
            grouped: dict[str, dict[str, Any]] = {}
            for row in self._snapshot_candidates(snapshot_id):
                if row["manufacturer"] != manufacturer:
                    continue
                entry = grouped.setdefault(row["commercial_model"], {
                    "manufacturer": manufacturer, "commercial_model": row["commercial_model"],
                    "variant_count": 0, "ambiguous_variant_count": 0,
                    "model_year_start": None, "model_year_end": None})
                entry["variant_count"] += 1
                entry["ambiguous_variant_count"] += 1 if row["status"] == "ambiguous" else 0
                start, end = row.get("model_year_start"), row.get("model_year_end")
                if start is not None:
                    entry["model_year_start"] = start if entry["model_year_start"] is None \
                        else min(entry["model_year_start"], start)
                    entry["model_year_end"] = end if entry["model_year_end"] is None \
                        else max(entry["model_year_end"], end)
            items = [entry for _, entry in sorted(grouped.items())]
            return self._aggregate_page(items, limit, offset)

    def catalog_candidate_model_years(self, snapshot_id: Any, *, manufacturer: str,
                                      commercial_model: str, limit: int = 50, offset: int = 0,
                                      allow_incomplete: bool = False) -> list[dict[str, Any]]:
        with self.lock:
            self._readable_snapshot(snapshot_id, allow_incomplete)
            if not str(manufacturer).strip() or not str(commercial_model).strip():
                raise AppError("CATALOG_QUERY_INVALID",
                               "a manufacturer and a commercial model are required", 400)
            grouped: dict[int, dict[str, Any]] = {}
            for row in self._snapshot_candidates(snapshot_id):
                if row["manufacturer"] != manufacturer \
                        or row["commercial_model"] != commercial_model \
                        or row.get("model_year_start") is None:
                    continue
                for year in range(row["model_year_start"], row["model_year_end"] + 1):
                    entry = grouped.setdefault(year, {
                        "manufacturer": manufacturer, "commercial_model": commercial_model,
                        "model_year": year, "variant_count": 0, "ambiguous_variant_count": 0})
                    entry["variant_count"] += 1
                    entry["ambiguous_variant_count"] += 1 if row["status"] == "ambiguous" else 0
            items = [entry for _, entry in sorted(grouped.items())]
            return self._aggregate_page(items, limit, offset)

    def catalog_candidate_variant_page(self, snapshot_id: Any, *, manufacturer: str | None = None,
                                       commercial_model: str | None = None,
                                       model_year: int | None = None,
                                       official_model_code: str | None = None,
                                       trim: str | None = None,
                                       identity_dimensions: dict[str, Any] | None = None,
                                       status: str | None = None, limit: int = 50,
                                       offset: int = 0,
                                       allow_incomplete: bool = False) -> list[dict[str, Any]]:
        with self.lock:
            self._readable_snapshot(snapshot_id, allow_incomplete)
            if status is not None and status not in CANDIDATE_STATUSES:
                raise AppError("CATALOG_QUERY_INVALID",
                               "unknown catalog candidate status", 400)
            # Raises on a dimension outside the closed vocabulary, exactly as
            # `catalog_identity_dimensions_valid` refuses one in SQL.
            wanted = stated_identity_dimensions(identity_dimensions or {})
            records = {row["id"]: row for row in self.catalog_raw_records.values()
                       if row["snapshot_id"] == str(snapshot_id)}
            matched: list[dict[str, Any]] = []
            for row in self._snapshot_candidates(snapshot_id):
                if manufacturer is not None and row["manufacturer"] != manufacturer:
                    continue
                if commercial_model is not None and row["commercial_model"] != commercial_model:
                    continue
                if official_model_code is not None \
                        and row.get("official_model_code") != official_model_code:
                    continue
                if trim is not None and row.get("trim") != trim:
                    continue
                if status is not None and row["status"] != status:
                    continue
                if model_year is not None:
                    start = row.get("model_year_start")
                    if start is None or not start <= int(model_year) <= row["model_year_end"]:
                        continue
                stored = row.get("identity_dimensions") or {}
                if any(stored.get(name) != value for name, value in wanted.items()):
                    continue
                record = records.get(row["raw_record_id"])
                if record is None:
                    continue
                matched.append({
                    "id": row["id"], "snapshot_id": row["snapshot_id"],
                    "raw_record_id": row["raw_record_id"],
                    "manufacturer": row["manufacturer"],
                    "commercial_model": row["commercial_model"],
                    "model_year_start": row.get("model_year_start"),
                    "model_year_end": row.get("model_year_end"),
                    "official_model_code": row.get("official_model_code"),
                    "trim": row.get("trim"),
                    "identity_dimensions": dict(stored), "status": row["status"],
                    "candidate_key": row["candidate_key"],
                    "upstream_record_id": record["upstream_record_id"],
                    "resource_id": record["resource_id"],
                    "source_locator": dict(record.get("source_locator") or {}),
                    "payload_sha256": record["payload_sha256"]})
            matched.sort(key=_variant_page_key)
            return self._aggregate_page(matched, limit, offset)

    def catalog_raw_record_by_upstream_id(self, snapshot_id: Any, upstream_record_id: str, *,
                                          allow_incomplete: bool = False) -> dict[str, Any] | None:
        with self.lock:
            self._readable_snapshot(snapshot_id, allow_incomplete)
            if not str(upstream_record_id).strip():
                raise AppError("CATALOG_QUERY_INVALID", "an upstream record id is required", 400)
            return next((dict(row) for row in self.catalog_raw_records.values()
                         if row["snapshot_id"] == str(snapshot_id)
                         and row["upstream_record_id"] == str(upstream_record_id)), None)

    def catalog_run_pending_promotions(self, run_id: UUID, tool_operation: str, *,
                                       limit: int = MAX_PROMOTIONS_PER_RUN
                                       ) -> list[dict[str, Any]]:
        """Mirrors `public.catalog_run_pending_promotions`, rule for rule.

        The association between a claim and the CANDIDATE it is evidence for is
        derived here exactly as it is derived in SQL: from the claim's own
        evidence locator, through the captured upstream row it names, to the
        candidate that is a reading of that row with exactly this identity
        scope. Nothing is remembered in this process, so a promotion path
        driven from this survives losing the process that gathered the
        evidence.
        """
        with self.lock:
            if not str(tool_operation or "").strip():
                raise AppError("CATALOG_QUERY_INVALID",
                               "a run and a tool operation are required", 400)
            bound = max(0, min(int(limit), MAX_PROMOTIONS_PER_RUN))
            snapshots = {row["id"]: row for row in self.catalog_snapshots.values()
                         if row.get("trust_state") == "evidence"
                         and row.get("activated_at") is not None
                         and row.get("validation_state") == "complete"}
            records = {row["id"]: row for row in self.catalog_raw_records.values()
                       if row["snapshot_id"] in snapshots}
            by_locator: dict[str, dict[str, Any]] = {}
            for row in records.values():
                snapshot = snapshots[row["snapshot_id"]]
                by_locator[record_locator_id(str(snapshot["snapshot_key"]),
                                             str(row["upstream_record_id"]))] = row
            sources = {str(row["id"]): row for row in self.tool_rows
                       if self.evidence_kinds.get(str(row.get("id"))) == "source"
                       and str(row.get("run_id")) == str(run_id)
                       and row.get("tool_operation") == str(tool_operation)}
            matched: list[dict[str, Any]] = []
            for claim in self.tool_rows:
                if self.evidence_kinds.get(str(claim.get("id"))) != "claim":
                    continue
                if str(claim.get("run_id")) != str(run_id) \
                        or claim.get("status", "active") != "active" \
                        or str(claim.get("source_id")) not in sources \
                        or not claim.get("evidence_locator"):
                    continue
                # R5: the CURRENT verdict, never "a verified verdict exists".
                # An older `verified` row that a newer verdict, contradiction
                # or supersession has replaced authorizes nothing.
                current = self._current_verdict(claim["id"])
                if current is None or not current.supported:
                    continue
                try:
                    locator = parse_locator_key(claim["evidence_locator"]).record_id
                except Exception:
                    continue
                record = by_locator.get(locator)
                if record is None:
                    continue
                scope = dict(claim.get("identity_scope") or {})
                readings = [row for row in self.catalog_candidates.values()
                            if row["raw_record_id"] == record["id"]
                            and row["snapshot_id"] == record["snapshot_id"]
                            and row["status"] in ("candidate", "ready_for_review")
                            and candidate_identity_scope(row.get("identity_dimensions"),
                                                         row.get("official_model_code"),
                                                         row.get("trim")) == scope]
                # A locator and identity that resolve to more than one reading
                # is an ambiguity this read refuses to settle.
                if len(readings) != 1:
                    continue
                candidate = readings[0]
                snapshot = snapshots[record["snapshot_id"]]
                matched.append({
                    "candidate_id": candidate["id"], "candidate_key": candidate["candidate_key"],
                    "status": candidate["status"], "snapshot_id": snapshot["id"],
                    "snapshot_key": snapshot["snapshot_key"],
                    "source_family": snapshot["source_family"],
                    "resource_id": snapshot["resource_id"],
                    "raw_record_id": record["id"],
                    "upstream_record_id": record["upstream_record_id"],
                    "record_key": record["record_key"],
                    "manufacturer": candidate["manufacturer"],
                    "commercial_model": candidate["commercial_model"],
                    "model_year_start": candidate.get("model_year_start"),
                    "model_year_end": candidate.get("model_year_end"),
                    "official_model_code": candidate.get("official_model_code"),
                    "trim": candidate.get("trim"),
                    "identity_dimensions": dict(candidate.get("identity_dimensions") or {}),
                    "claim_id": claim["id"], "source_id": claim["source_id"],
                    "verdict_id": current.verdict_id, "field_key": claim["field_key"],
                    "field_value": claim["value"]})
            # The SAME bound and the SAME order the SQL applies, so a resumed
            # worker sees exactly the set the crashed one would have.
            keys = sorted({row["candidate_key"] for row in matched})[:bound]
            return sorted((row for row in matched if row["candidate_key"] in keys),
                          key=lambda row: (row["candidate_key"], row["field_key"],
                                           str(row["claim_id"])))

    def catalog_snapshot_candidate_diff(self, previous_snapshot_id: Any, snapshot_id: Any, *,
                                        limit: int = MAX_DIFF_ITEMS,
                                        allow_incomplete: bool = False) -> list[dict[str, Any]]:
        """Mirrors `public.catalog_snapshot_candidate_diff`, rule for rule.

        Both sides pass the same readability gate, the comparison itself is the
        ONE definition in `backend/catalog/diff.py` that the SQL function
        mirrors, and the rows come back in the same shape -- including the
        COUNT ROW when there are no items to list.

        What is deliberately NOT claimed here is the property the SQL function
        exists FOR: doing the comparison without reading either snapshot into
        this process. A dictionary already holds every row, so there is nothing
        to avoid reading.
        """
        with self.lock:
            if previous_snapshot_id is not None:
                self._readable_snapshot(previous_snapshot_id, allow_incomplete)
            self._readable_snapshot(snapshot_id, allow_incomplete)
            return diff_rows(self._diff_side(previous_snapshot_id),
                             self._diff_side(snapshot_id),
                             limit=max(0, min(int(limit), MAX_DIFF_ITEMS)))

    def _diff_side(self, snapshot_id: Any) -> list[dict[str, Any]]:
        """One snapshot's candidates, joined to their captured rows, in page order."""
        if snapshot_id is None:
            return []
        records = {row["id"]: row for row in self.catalog_raw_records.values()}
        rows = [{**row, "upstream_record_id":
                 records.get(str(row["raw_record_id"]), {}).get("upstream_record_id", "")}
                for row in self.catalog_candidates.values()
                if str(row["snapshot_id"]) == str(snapshot_id)]
        return sorted(rows, key=_variant_page_key)

    # --- field-level canonical promotion (PR3) -------------------------------
    #
    # Mirrors `promote_catalog_variant_guarded` and, more importantly, the two
    # TRIGGERS that hold for every writer: the per-fact support chain
    # (`catalog_check_field_provenance`) and the deferred coverage check
    # (`catalog_require_field_provenance`).
    #
    # WHAT THIS IS NOT. It is a mirror of the rules, not a second
    # implementation of PostgreSQL. Two protections are deliberately
    # DATABASE-ONLY and are documented as such rather than claimed here: the
    # DEFERRED timing (this implementation checks coverage before it appends
    # anything, so there is no window at all rather than a window that closes
    # at COMMIT), and the concurrency semantics of the unique indexes under
    # simultaneous writers, which a single-process dictionary cannot exhibit.
    _CANONICAL_PROVENANCE_DERIVED = ("snapshot_id", "source_id", "claim_id", "verdict_id",
                                     "record_locator", "source_version",
                                     "source_version_kind")

    def _check_field_provenance(self, run_id: UUID, candidate: dict[str, Any],
                                field_key: str, value: Any, link_id: Any, *,
                                model_key: str, variant: dict[str, Any] | None,
                                promotion_key: str, worker_id: str,
                                attempt: int) -> tuple[dict[str, Any], dict[str, Any]]:
        """One promoted fact, held to its WHOLE support chain, or refused.

        Returns the cited link and the SCOPE the cited claim stated, which the
        caller stores on the provenance row exactly as the BEFORE INSERT
        trigger derives it in PostgreSQL.
        """
        link = next((row for row in self.catalog_evidence_links.values()
                     if row["id"] == str(link_id)), None)
        if link is None:
            raise AppError("CATALOG_PROMOTION_LINK_INVALID",
                           "canonical field provenance cites no catalog evidence link", 400)
        if str(link["candidate_id"]) != str(candidate["id"]):
            raise AppError("CATALOG_PROMOTION_LINK_CANDIDATE",
                           "canonical field provenance cites evidence of another candidate", 400)
        # ONE RUN, checked before anything that looks a row up BY run: a
        # cross-run promotion must be refused for the reason it actually
        # failed, not for a lookup that happens to filter on the same column.
        # `promote_catalog_variant` proved the run holds a valid worker lease,
        # so binding the link to that run carries the lease's authority down to
        # the stored fact.
        if str(link.get("run_id")) != str(run_id):
            raise AppError("CATALOG_PROMOTION_RUN_MISMATCH",
                           "canonical field provenance was not promoted by its linking run", 409)
        snapshot = self._catalog_snapshot_by_id(link["snapshot_id"])
        if str(candidate["snapshot_id"]) != str(link["snapshot_id"]):
            raise AppError("CATALOG_PROMOTION_SNAPSHOT_MISMATCH",
                           "canonical field provenance snapshot mismatch", 400)
        if snapshot.get("activated_at") is None or snapshot.get("validation_state") != "complete":
            raise AppError("CATALOG_PROMOTION_SNAPSHOT_UNUSABLE",
                           "catalog promotion requires an active complete snapshot", 409)
        if snapshot.get("trust_state") != "evidence":
            raise AppError("CATALOG_PROMOTION_UNVERIFIED_SOURCE",
                           "an unverified catalog source cannot support a canonical fact", 409)
        if link.get("verdict_id") is None:
            raise AppError("CATALOG_PROMOTION_UNVERIFIED",
                           "canonical field provenance cites unverified evidence", 409)
        verdict = self._catalog_evidence_row(link["verdict_id"], run_id, "VERDICT")
        if verdict.get("verdict") != "verified":
            raise AppError("CATALOG_PROMOTION_VERDICT_NOT_VERIFIED",
                           "canonical field provenance verdict is not verified", 409)
        # R5: THE ONE THAT MATTERS. A link created while its verdict was
        # current, followed by a newer invalidation, is exactly the stale
        # authorization the link-time gate cannot see -- the link was
        # legitimate when it was written. Mirrors the
        # `catalog_canonical_field_provenance_current_verdict` trigger.
        current = self._current_verdict(link["claim_id"])
        if current is None or not current.authorizes(link["verdict_id"]):
            raise AppError("CATALOG_PROMOTION_VERDICT_NOT_CURRENT",
                           "canonical field provenance cites a verdict that is no "
                           "longer current", 409)
        claim = self._catalog_evidence_row(link["claim_id"], run_id, "CLAIM")
        if str(verdict.get("claim_id")) != str(claim["id"]):
            raise AppError("CATALOG_PROMOTION_VERDICT_CLAIM_MISMATCH",
                           "canonical field provenance verdict claim mismatch", 400)
        if str(claim.get("source_id")) != str(link["source_id"]):
            raise AppError("CATALOG_PROMOTION_CLAIM_SOURCE_MISMATCH",
                           "canonical field provenance claim source mismatch", 400)
        if claim.get("status", "active") != "active":
            raise AppError("CATALOG_PROMOTION_CLAIM_INACTIVE",
                           "canonical field provenance cites a claim that is not active", 400)
        # THE FIELD GATE: the verified claim must state this exact field at
        # this exact value. A verdict that confirmed one field says nothing
        # about the field beside it.
        if claim.get("field_key") != field_key:
            raise AppError("CATALOG_PROMOTION_FIELD_MISMATCH",
                           "canonical field provenance claim states a different field", 400)
        if claim.get("value") != value:
            raise AppError("CATALOG_PROMOTION_VALUE_MISMATCH",
                           "canonical field provenance claim states a different value", 400)
        if any(row.get("outcome") == "unresolved_needs_review"
               and str(claim["id"]) in [str(item) for item in (row.get("claim_ids") or [])]
               for row in self.tool_rows
               if self.evidence_kinds.get(str(row.get("id"))) == "conflict"):
            raise AppError("CATALOG_PROMOTION_UNRESOLVED_CONFLICT",
                           "canonical field provenance claim is in an unresolved conflict", 409)

        # THE TIME SCOPE, and THE ENTITY. Everything above proves the evidence
        # is sound; none of it proves the evidence is about the vehicle being
        # written. The candidate's own identity was already held to the
        # promotion's, so binding the claim to the candidate here binds it to
        # the canonical row.
        time_scope = claim.get("time_scope") or {}
        model_year = time_scope.get("model_year")
        if not isinstance(model_year, int) or isinstance(model_year, bool):
            raise AppError("CATALOG_PROMOTION_TIME_SCOPE",
                           "canonical field provenance claim states no model year scope", 400)
        if not (int(candidate["model_year_start"]) <= model_year
                <= int(candidate["model_year_end"])):
            raise AppError("CATALOG_PROMOTION_TIME_SCOPE",
                           "canonical field provenance claim is scoped to another model year", 400)
        if claim.get("entity_key") != claim_entity_key(model_key, model_year):
            raise AppError("CATALOG_PROMOTION_ENTITY_MISMATCH",
                           "canonical field provenance claim is about another vehicle", 400)

        # THE MARKET. A vehicle fact is a fact somewhere; an unscoped value
        # cannot be compared to any other, and a canonical catalog built out of
        # unscoped values silently mixes markets.
        if not str(claim.get("market") or "").strip() \
                or not str(claim.get("geography") or "").strip():
            raise AppError("CATALOG_PROMOTION_MARKET_SCOPE",
                           "canonical field provenance claim states no market scope", 400)

        # THE IDENTITY SCOPE. Exactly the identity the candidate states: an
        # extra dimension means the evidence is about a narrower vehicle than
        # this row, a missing one means a wider one, and neither is evidence
        # for THIS variant. Keys exactly; values under the same normalization
        # R4 stored them with.
        identity = dict(claim.get("identity_scope") or {})
        if identity != candidate_identity_scope(candidate.get("identity_dimensions"),
                                                candidate.get("official_model_code"),
                                                candidate.get("trim")):
            raise AppError("CATALOG_PROMOTION_IDENTITY_SCOPE",
                           "canonical field provenance claim is scoped to another vehicle identity",
                           400)

        # THE SOURCE RECORD. A candidate is a READING of one captured upstream
        # row, and the evidence that promotes it must have been read from THAT
        # row -- not from a different record of the same snapshot.
        record = next((row for row in self.catalog_raw_records.values()
                       if row["id"] == str(candidate["raw_record_id"])), None)
        if record is None:
            raise AppError("CATALOG_PROMOTION_RECORD_INVALID",
                           "canonical field provenance cites no source record", 400)
        if claim.get("evidence_locator") != link["record_locator"]:
            raise AppError("CATALOG_PROMOTION_LOCATOR_MISMATCH",
                           "canonical field provenance locator does not match its cited claim", 400)
        expected_record = record_locator_id(str(snapshot["snapshot_key"]),
                                            str(record["upstream_record_id"]))
        if parse_locator_key(link["record_locator"]).record_id != expected_record:
            raise AppError("CATALOG_PROMOTION_RECORD_MISMATCH",
                           "canonical field provenance cites evidence read from another source record",
                           400)

        # ONE RUN. `promote_catalog_variant` proves the run holds a valid
        # worker lease before it writes; binding the link, the source, the
        # claim and the verdict to that same run is what carries the lease's
        # authority down to the stored fact. (`_catalog_evidence_row` already
        # required the claim and the verdict to be this run's.)
        source = self._catalog_evidence_row(link["source_id"], run_id, "SOURCE")
        if str(source.get("run_id")) != str(run_id) or str(claim.get("run_id")) != str(run_id) \
                or str(verdict.get("run_id")) != str(run_id):
            raise AppError("CATALOG_PROMOTION_RUN_MISMATCH",
                           "canonical field provenance support chain spans more than one run", 409)

        scope = {"entity_key": claim["entity_key"], "market": claim["market"],
                 "geography": claim["geography"], "time_scope": dict(time_scope),
                 "identity_scope": identity}
        # One promotion is ONE act: every field it writes shares its run, its
        # worker, its attempt, its candidate and its variant.
        if any(row["run_id"] != str(run_id) or row["worker_id"] != worker_id
               or row["attempt"] != int(attempt)
               or row["candidate_id"] != candidate["id"]
               or (variant is not None and row["variant_id"] != variant["id"])
               for row in self.catalog_canonical_field_provenance
               if row["promotion_key"] == promotion_key):
            raise AppError("CATALOG_PROMOTION_RUN_MISMATCH",
                           "catalog promotion is not one act of one run", 409)
        # Every fact about one canonical variant is read under ONE scope, so a
        # variant can never accumulate facts about two markets, two model years
        # or two vehicle identities.
        if variant is not None and any(
                {name: row[name] for name in scope} != scope
                for row in self.catalog_canonical_field_provenance
                if row["variant_id"] == variant["id"]):
            raise AppError("CATALOG_PROMOTION_SCOPE_CONFLICT",
                           "canonical field provenance scope disagrees with this variant", 409)
        return link, scope

    def promote_catalog_variant(self, run_id: UUID, promotion: dict[str, Any], *,
                                worker_id: str, attempt: int,
                                lease_token: str) -> dict[str, Any]:
        with self.lock:
            self._catalog_lease(run_id, worker_id, attempt, lease_token)
            promotion = prepare_promotion(promotion)
            candidate = next((row for row in self.catalog_candidates.values()
                              if row["id"] == str(promotion.get("candidate_id"))), None)
            if candidate is None:
                raise AppError("CATALOG_PROMOTION_INVALID",
                               "invalid catalog promotion candidate", 400)
            # An AMBIGUOUS, rejected or still-unreviewed candidate is not
            # promotable: ambiguity is a first-class answer in this schema.
            if candidate.get("status") != "ready_for_review":
                raise AppError("CATALOG_PROMOTION_CANDIDATE_NOT_READY",
                               "catalog candidate is not ready for promotion", 409)
            # The promotion may not state an identity its own candidate does
            # not. The caller derives the canonical key from these six fields,
            # so without this the caller -- not the reviewed candidate -- would
            # decide which vehicle a verified fact lands on.
            if any(candidate.get(name) != promotion.get(name)
                   for name in ("manufacturer", "commercial_model", "model_year_start",
                                "model_year_end", "official_model_code", "trim")):
                raise AppError("CATALOG_PROMOTION_CANDIDATE_IDENTITY",
                               "catalog promotion states an identity its candidate does not", 400)
            stated = stated_canonical_fields(promotion)
            requested = {entry["field_key"]: entry["value"] for entry in promotion["fields"]}
            if requested != stated:
                raise AppError("CATALOG_PROMOTION_FIELDS_MISMATCH",
                               "promoted catalog fields do not match the canonical row", 400)

            key = str(promotion["promotion_key"])
            stored = {row["field_key"]: row["field_value"]
                      for row in self.catalog_canonical_field_provenance
                      if row["promotion_key"] == key}
            if stored:
                variant = next(row for row in self.catalog_model_variants
                               if row["id"] == next(item["variant_id"]
                                                    for item in self.catalog_canonical_field_provenance
                                                    if item["promotion_key"] == key))
                if stored != requested or variant["canonical_key"] != promotion["canonical_key"] \
                        or any(row["candidate_id"] != candidate["id"]
                               for row in self.catalog_canonical_field_provenance
                               if row["promotion_key"] == key):
                    raise AppError("CATALOG_PROMOTION_IDEMPOTENCY_CONFLICT",
                                   "catalog promotion idempotency conflict", 409)
                return dict(variant)

            # The existing canonical rows this promotion would land on, looked
            # up BEFORE anything is validated, because a promoted fact is
            # checked against the variant it is about.
            model = next((row for row in self.catalog_models
                          if row["canonical_key"] == promotion["model_canonical_key"]), None)
            if model is not None and (model["manufacturer"] != promotion["manufacturer"]
                                      or model["commercial_model"] != promotion["commercial_model"]):
                raise AppError("CATALOG_PROMOTION_MODEL_CONFLICT",
                               "catalog canonical model identity conflict", 409)
            variant = next((row for row in self.catalog_model_variants
                            if row["canonical_key"] == promotion["canonical_key"]), None)
            if variant is not None:
                # A key is a caller-derived string. The stored row is the
                # authority on who it is, so a promotion presenting this key
                # for a different vehicle is a conflict, not a revision.
                #
                # `identity_dimensions` is deliberately NOT compared: it is a
                # revisable fact about the variant rather than part of its
                # identity, so a better source revising one appends a revision
                # to THIS variant (`canonical_variant_key` in
                # `backend/catalog/keys.py` states the same decision).
                if (model is None or variant["model_id"] != model["id"]
                        or any(variant.get(name) != promotion.get(name)
                               for name in CANONICAL_VARIANT_KEY_FIELDS)):
                    raise AppError("CATALOG_PROMOTION_VARIANT_CONFLICT",
                                   "catalog canonical variant identity conflict", 409)

            # Validate EVERY field before appending ANY row: a promotion is
            # atomic, so a refusal must leave the canonical catalog exactly as
            # it was -- including leaving no half-created model.
            checked = {entry["field_key"]: self._check_field_provenance(
                        run_id, candidate, entry["field_key"], entry["value"],
                        entry["evidence_link_id"],
                        model_key=str(promotion["model_canonical_key"]), variant=variant,
                        promotion_key=key, worker_id=worker_id, attempt=int(attempt))
                       for entry in promotion["fields"]}
            links = {name: value[0] for name, value in checked.items()}
            scopes = {name: value[1] for name, value in checked.items()}
            # Every field of ONE promotion is read under ONE scope, whether or
            # not the variant already exists to compare against.
            if len({_scope_identity(scope) for scope in scopes.values()}) > 1:
                raise AppError("CATALOG_PROMOTION_SCOPE_CONFLICT",
                               "canonical field provenance scope disagrees with this variant", 409)

            if model is None:
                model = {"id": str(uuid4()), "manufacturer": promotion["manufacturer"],
                         "commercial_model": promotion["commercial_model"],
                         "canonical_key": promotion["model_canonical_key"], "revision": 1,
                         "created_at": _now(), "updated_at": _now()}
                self.catalog_models.append(model)

            anchor = links[sorted(links)[0]]
            if variant is None:
                variant = {"id": str(uuid4()), "model_id": model["id"],
                           "promoted_from_candidate_id": candidate["id"],
                           "promoted_from_verdict_id": anchor["verdict_id"],
                           "canonical_key": promotion["canonical_key"],
                           "model_year_start": promotion["model_year_start"],
                           "model_year_end": promotion["model_year_end"],
                           "official_model_code": promotion.get("official_model_code"),
                           "trim": promotion.get("trim"),
                           "identity_dimensions": dict(promotion.get("identity_dimensions") or {}),
                           "revision": 1, "created_at": _now()}
                self.catalog_model_variants.append(variant)
            for entry in promotion["fields"]:
                link = links[entry["field_key"]]
                revision = 1 + max((row["revision"] for row in self.catalog_canonical_field_provenance
                                    if row["variant_id"] == variant["id"]
                                    and row["field_key"] == entry["field_key"]), default=0)
                self.catalog_canonical_field_provenance.append({
                    "id": str(uuid4()), "model_id": model["id"], "variant_id": variant["id"],
                    "field_key": entry["field_key"], "field_value": entry["value"],
                    "revision": revision, "candidate_id": candidate["id"],
                    "evidence_link_id": link["id"], "snapshot_id": link["snapshot_id"],
                    "source_id": link["source_id"], "claim_id": link["claim_id"],
                    "verdict_id": link["verdict_id"], "run_id": str(run_id),
                    "worker_id": worker_id, "attempt": int(attempt),
                    "source_version": link["source_version"],
                    "source_version_kind": link["source_version_kind"],
                    "record_locator": link["record_locator"], "promotion_key": key,
                    **scopes[entry["field_key"]], "created_at": _now()})
            return dict(variant)

    def _canonical_variant_current(self, variant: dict[str, Any]) -> dict[str, Any]:
        """One variant as `catalog_canonical_variant_current` would state it.

        Shared by the single lookup and the bounded listing, so the two cannot
        drift: the view is ONE definition of "the current canonical value" and a
        second assembly of it here would be a second definition. Caller holds
        the lock.
        """
        model = next(row for row in self.catalog_models if row["id"] == variant["model_id"])
        current: dict[str, dict[str, Any]] = {}
        for row in self.catalog_canonical_field_provenance:
            if row["variant_id"] != variant["id"]:
                continue
            held = current.get(row["field_key"])
            if held is None or row["revision"] > held["revision"]:
                current[row["field_key"]] = row
        dimensions = {name[len(CANONICAL_DIMENSION_PREFIX):]: row["field_value"]
                      for name, row in current.items()
                      if name.startswith(CANONICAL_DIMENSION_PREFIX)}
        return {"variant_id": variant["id"], "model_id": model["id"],
                "canonical_key": variant["canonical_key"],
                "model_canonical_key": model["canonical_key"],
                "manufacturer": model["manufacturer"],
                "commercial_model": model["commercial_model"],
                "promoted_from_candidate_id": variant["promoted_from_candidate_id"],
                "promoted_from_verdict_id": variant["promoted_from_verdict_id"],
                "model_year_start": current["model_year_start"]["field_value"],
                "model_year_end": current["model_year_end"]["field_value"],
                "official_model_code": (current["official_model_code"]["field_value"]
                                        if "official_model_code" in current else None),
                "trim": current["trim"]["field_value"] if "trim" in current else None,
                "identity_dimensions": dimensions,
                "field_revisions": {name: row["revision"] for name, row in current.items()},
                "promoted_at": variant["created_at"],
                "revised_at": max(row["created_at"] for row in current.values())}

    def get_canonical_catalog_variant(self, canonical_key: str) -> dict[str, Any] | None:
        """ONE canonical variant's CURRENT state, assembled exactly as the
        authoritative view assembles it: the HIGHEST revision of every promoted
        field, never the columns frozen at revision 1."""
        with self.lock:
            variant = next((row for row in self.catalog_model_variants
                            if row["canonical_key"] == str(canonical_key)), None)
            if variant is None:
                return None
            return self._canonical_variant_current(variant)

    #: The canonical listing's own page bound, mirroring
    #: `SupabaseRepository.MAX_CANONICAL_LIST_ROWS`.
    MAX_CANONICAL_LIST_ROWS = 100

    def list_canonical_catalog_variants(self, *, manufacturer: str | None = None,
                                        commercial_model: str | None = None,
                                        model_year: int | None = None,
                                        canonical_key: str | None = None,
                                        limit: int = 50,
                                        offset: int = 0) -> list[dict[str, Any]]:
        """CODE-3's bounded canonical page, with the same row contract as SQL.

        `total_count` on every row and one COUNT ROW for an empty page, exactly
        as `_aggregate_page` does for the candidate aggregations and as
        `SupabaseRepository.list_canonical_catalog_variants` does over
        PostgREST's `count=exact`. Ordering is `canonical_key`, which is the
        ordering the view is read under.
        """
        with self.lock:
            rows = [self._canonical_variant_current(variant)
                    for variant in self.catalog_model_variants]
        matched = [row for row in rows
                   if (manufacturer is None or row["manufacturer"] == str(manufacturer))
                   and (commercial_model is None
                        or row["commercial_model"] == str(commercial_model))
                   and (canonical_key is None or row["canonical_key"] == str(canonical_key))
                   and (model_year is None
                        or (row["model_year_start"] is not None
                            and row["model_year_start"] <= int(model_year)
                            <= row["model_year_end"]))]
        matched.sort(key=lambda row: str(row["canonical_key"]))
        total = len(matched)
        bounded = max(1, min(int(limit), self.MAX_CANONICAL_LIST_ROWS))
        start = max(0, int(offset))
        page = matched[start:start + bounded]
        if not page:
            columns = matched[0] if matched else {}
            return [{name: None for name in columns} | {"total_count": total}]
        return [{**row, "total_count": total} for row in page]

    def list_canonical_field_provenance(self, variant_id: Any, *,
                                        limit: int = 200) -> list[dict[str, Any]]:
        with self.lock:
            rows = [dict(row) for row in self.catalog_canonical_field_provenance
                    if row["variant_id"] == str(variant_id)]
        rows.sort(key=lambda row: (row["field_key"], row["revision"]))
        return rows[:max(1, min(int(limit), 200))]

    def _catalog_snapshot_by_id(self, snapshot_id: Any) -> dict[str, Any]:
        snapshot = next((row for row in self.catalog_snapshots.values()
                         if row["id"] == str(snapshot_id)), None)
        if snapshot is None:
            raise AppError("CATALOG_SNAPSHOT_INVALID", "invalid catalog snapshot", 400)
        return snapshot

    # -- mapping plans ---------------------------------------------------------------
    #
    # Mirrors `20260922000100_catalog_work_scopes.sql`: the digest is derived
    # here from the canonical TEXT exactly as the CHECK constraint derives it,
    # the record is held to the same shape and bounds
    # (`work_scope_contract.stored_record_valid` is the Python twin of
    # `catalog_work_scope_record_valid`), membership and the trusted project
    # workflow are re-checked, and a revision lands only against the head the
    # caller names. `tests/test_work_scope.py` and the PostgreSQL suite hold the
    # two to the same expectations.

    def _work_scope_revision(self, work_scope_id: str, number: int, created_by: UUID,
                             revision: Any) -> dict[str, Any]:
        if not isinstance(revision, Mapping) or not isinstance(revision.get("scope_text"), str) \
                or not isinstance(revision.get("input_kind"), str) \
                or not isinstance(revision.get("instruction"), (str, type(None))) \
                or not isinstance(revision.get("notes", []), list):
            raise AppError("WORK_SCOPE_REVISION_INVALID", "invalid work scope revision", 422)
        text = revision["scope_text"]
        instruction = revision.get("instruction")
        notes = revision.get("notes", [])
        try:
            record = json.loads(text)
        except ValueError:
            raise AppError("WORK_SCOPE_REVISION_INVALID", "invalid work scope revision", 422) from None
        if not 2 <= len(text) <= work_scope_contract.MAX_SCOPE_TEXT_CHARS \
                or not work_scope_contract.stored_record_valid(record) \
                or revision["input_kind"] not in ("instruction", "edit") \
                or (revision["input_kind"] == "instruction") != (instruction is not None) \
                or (instruction is not None and not 1 <= len(instruction) <= 500) \
                or len(json.dumps(notes, ensure_ascii=False)) > 4000:
            raise AppError("WORK_SCOPE_REVISION_INVALID", "invalid work scope revision", 422)
        row = {"id": str(uuid4()), "work_scope_id": work_scope_id, "revision": number,
               "scope_text": text, "scope": record,
               "digest": work_scope_contract.scope_digest(text),
               "input_kind": revision["input_kind"], "instruction": instruction,
               "notes": copy.deepcopy(notes), "created_by": str(created_by),
               "created_at": _now()}
        self.work_scope_revisions.append(row)
        return row

    def create_work_scope(self, conversation_id: UUID, created_by: UUID,
                          revision: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            conversation = self.conversations.get(str(conversation_id))
            if conversation is None or not self._is_member(conversation["project_id"], created_by):
                raise NotFoundError("conversation", str(conversation_id))
            project = self.projects[conversation["project_id"]]
            if project.get("workflow_key") != "swarm_v2":
                raise AppError("WORK_SCOPE_WORKFLOW_UNSUPPORTED",
                               "this project's engine does not read a mapping plan", 409)
            if any(row["conversation_id"] == str(conversation_id) and row["closed_at"] is None
                   for row in self.work_scopes.values()):
                raise AppError("WORK_SCOPE_OPEN_EXISTS",
                               "this conversation already has an open mapping plan", 409)
            scope_id = str(uuid4())
            row = self._work_scope_revision(scope_id, 1, created_by, revision)
            scope = {"id": scope_id, "project_id": conversation["project_id"],
                     "conversation_id": str(conversation_id), "created_by": str(created_by),
                     "status": "draft", "head_revision": 1, "head_digest": row["digest"],
                     "created_at": row["created_at"], "updated_at": row["created_at"],
                     "closed_at": None}
            self.work_scopes[scope_id] = scope
            return {"work_scope": dict(scope), "revision": copy.deepcopy(row)}

    def revise_work_scope(self, work_scope_id: UUID, expected_revision: int,
                          expected_digest: str, created_by: UUID,
                          revision: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            scope = self.work_scopes.get(str(work_scope_id))
            if scope is None or not self._is_member(scope["project_id"], created_by):
                raise NotFoundError("work_scope", str(work_scope_id))
            if self.projects[scope["project_id"]].get("workflow_key") != "swarm_v2":
                raise AppError("WORK_SCOPE_WORKFLOW_UNSUPPORTED",
                               "this project's engine does not read a mapping plan", 409)
            if scope["closed_at"] is not None or scope["status"] != "draft":
                raise AppError("WORK_SCOPE_NOT_EDITABLE", "the mapping plan is not editable", 409)
            if scope["head_revision"] != expected_revision or scope["head_digest"] != expected_digest:
                raise AppError("WORK_SCOPE_STALE",
                               "the plan changed since it was read; reload it and try again", 409)
            row = self._work_scope_revision(scope["id"], scope["head_revision"] + 1,
                                            created_by, revision)
            scope.update(head_revision=row["revision"], head_digest=row["digest"],
                         updated_at=row["created_at"])
            return {"work_scope": dict(scope), "revision": copy.deepcopy(row)}

    def get_work_scope(self, work_scope_id: UUID) -> dict[str, Any] | None:
        with self.lock:
            scope = self.work_scopes.get(str(work_scope_id))
            return dict(scope) if scope is not None else None

    def open_work_scope(self, conversation_id: UUID) -> dict[str, Any] | None:
        with self.lock:
            return next((dict(row) for row in self.work_scopes.values()
                         if row["conversation_id"] == str(conversation_id)
                         and row["closed_at"] is None), None)

    def list_work_scope_revisions(self, work_scope_id: UUID, *,
                                  limit: int = 11) -> list[dict[str, Any]]:
        """Newest first, bounded -- the order and bound of the PostgREST read."""
        with self.lock:
            rows = [copy.deepcopy(row) for row in self.work_scope_revisions
                    if row["work_scope_id"] == str(work_scope_id)]
        rows.sort(key=lambda row: row["revision"], reverse=True)
        return rows[:max(1, min(int(limit), self.MAX_WORK_SCOPE_REVISION_ROWS))]

    #: Mirrors `SupabaseRepository.MAX_WORK_SCOPE_REVISION_ROWS`.
    MAX_WORK_SCOPE_REVISION_ROWS = 50

    def get_work_scope_revision(self, work_scope_id: UUID, revision: int) -> dict[str, Any] | None:
        """ONE exact revision of one plan, or None. Not a search."""
        with self.lock:
            row = next((row for row in self.work_scope_revisions
                        if row["work_scope_id"] == str(work_scope_id)
                        and row["revision"] == int(revision)), None)
            return copy.deepcopy(row) if row is not None else None

    # -- mapping plan preparation ------------------------------------------------------
    #
    # Mirrors `20260923000100_catalog_work_scope_preparation.sql` decision for
    # decision: the same refusals under the same codes, the same two passes, the
    # same in-range counting, the same vocabulary gate and the same canonical
    # candidate order (`_variant_page_key`, the order the SQL `collate "C"`
    # ordering produces). The PostgreSQL suite and `tests/test_work_scope_
    # preparation.py` hold the two to the same answers.

    _WORK_SCOPE_TERMINAL_RUN_STATES = frozenset({
        "completed", "partial_success", "failed", "cancelled", "timed_out", "budget_exhausted"})
    _WORK_SCOPE_UNUSABLE_REASONS = frozenset({
        "GOV_PROJECTION_SNAPSHOT_INCOMPLETE", "GOV_PROJECTION_SNAPSHOT_NOT_READ",
        "GOV_PROJECTION_RESOURCE_NOT_NORMALIZED", "GOV_PROJECTION_SNAPSHOT_STATE_INVALID"})
    _WORK_SCOPE_UNIT_FIELDS = frozenset({"priority", "reason_code", "register_marque",
                                         "snapshot_id", "state", "unit_key"})

    @staticmethod
    def _in_plan_years(row: Mapping[str, Any], year_from: Any, year_to: Any) -> bool:
        """`(from is null or start >= from) and (to is null or end <= to)`."""
        start, end = row.get("model_year_start"), row.get("model_year_end")
        if year_from is not None and (start is None or start < year_from):
            return False
        if year_to is not None and (end is None or end > year_to):
            return False
        return True

    def _work_scope_preparation_summary(self, preparation: Mapping[str, Any],
                                        replayed: bool) -> dict[str, Any]:
        units = sorted((dict(row) for row in self.work_scope_units
                        if row["preparation_id"] == preparation["id"]),
                       key=lambda row: row["priority"])
        batches = sorted((row for row in self.work_scope_batches
                          if row["preparation_id"] == preparation["id"]),
                         key=lambda row: row["batch_number"])
        return {"replayed": replayed, "preparation": dict(preparation), "units": units,
                "batches": [{"id": row["id"], "batch_number": row["batch_number"],
                             "unit_key": row["unit_key"], "snapshot_key": row["snapshot_key"],
                             "item_count": row["item_count"],
                             "first_position": row["first_position"]} for row in batches]}

    def prepare_work_scope_queue(self, run_id: UUID, preparation: dict[str, Any], *,
                                 worker_id: str, attempt: int, lease_token: str) -> dict[str, Any]:
        invalid = AppError("WORK_SCOPE_PREPARATION_INVALID",
                           "invalid mapping plan preparation", 422)
        snapshot_invalid = AppError("WORK_SCOPE_UNIT_SNAPSHOT_INVALID",
                                    "a unit's snapshot is not that marque's usable scoped capture",
                                    422)
        stale = AppError("WORK_SCOPE_STALE",
                         "the plan changed since it was read; reload it and try again", 409)
        with self.lock:
            self._catalog_lease(run_id, worker_id, attempt, lease_token)
            identity = self.runs[str(run_id)].get("run_identity") or {}
            if identity.get("workflow_key") != "operator_capture":
                raise AppError("WORK_SCOPE_PREPARATION_RUN_INVALID",
                               "only an operator capture run prepares a mapping plan", 409)
            if not isinstance(preparation, Mapping) \
                    or set(preparation) != {"revision", "scope_digest", "units", "work_scope_id"}:
                raise invalid
            revision, digest = preparation["revision"], preparation["scope_digest"]
            units, scope_id = preparation["units"], preparation["work_scope_id"]
            if isinstance(revision, bool) or not isinstance(revision, int) \
                    or not 0 <= revision <= 999_999_999 \
                    or not isinstance(digest, str) or not _HEX64.fullmatch(digest) \
                    or not isinstance(units, list) or not isinstance(scope_id, str):
                raise invalid
            try:
                UUID(scope_id)
            except ValueError:
                raise invalid from None
            plan = self.work_scopes.get(scope_id)
            if plan is None:
                raise NotFoundError("work_scope", scope_id)
            if self.projects[plan["project_id"]].get("workflow_key") != "swarm_v2":
                raise AppError("WORK_SCOPE_WORKFLOW_UNSUPPORTED",
                               "this project's engine does not read a mapping plan", 409)
            if plan["closed_at"] is not None:
                raise AppError("WORK_SCOPE_NOT_EDITABLE", "the mapping plan is not editable", 409)
            if plan["head_revision"] != revision or plan["head_digest"] != digest:
                raise stale
            stored_revision = next((row for row in self.work_scope_revisions
                                    if row["work_scope_id"] == scope_id
                                    and row["revision"] == revision), None)
            if stored_revision is None or stored_revision["digest"] != digest:
                raise stale
            scope = stored_revision["scope"]
            plan_units = list(scope["units"])
            year_from = scope["model_years"]["from"]
            year_to = scope["model_years"]["to"]
            size = int(scope["batch_size"])
            if len(units) != len(plan_units):
                raise invalid

            existing = next((row for row in self.work_scope_preparations.values()
                             if row["work_scope_id"] == scope_id
                             and row["revision"] == revision), None)
            if existing is not None:
                stored = [{"unit_key": row["unit_key"], "snapshot_id": row["snapshot_id"],
                           "register_marque": row["register_marque"],
                           "captured": row["state"] in ("prepared", "vocabulary_insufficient")}
                          for row in sorted((row for row in self.work_scope_units
                                             if row["preparation_id"] == existing["id"]),
                                            key=lambda row: row["priority"])]
                if not all(isinstance(entry, Mapping) for entry in units):
                    raise invalid
                submitted = [{"unit_key": entry.get("unit_key"),
                              "snapshot_id": entry.get("snapshot_id"),
                              "register_marque": entry.get("register_marque"),
                              "captured": entry.get("state") == "captured"}
                             for entry in sorted(units, key=lambda entry: entry.get("priority"))]
                if stored != submitted:
                    raise AppError("WORK_SCOPE_ALREADY_PREPARED",
                                   "this plan revision was already prepared differently", 409)
                return self._work_scope_preparation_summary(existing, True)

            # Pass 1: decide every unit before writing anything.
            budget = int(scope["max_items"])
            decided: list[dict[str, Any]] = []
            prepared = queued = batch_total = 0
            seen: set[str] = set()
            for index, entry in enumerate(units):
                if not isinstance(entry, Mapping) or set(entry) != self._WORK_SCOPE_UNIT_FIELDS \
                        or entry["unit_key"] != plan_units[index] \
                        or isinstance(entry["priority"], bool) \
                        or entry["priority"] != index + 1:
                    raise invalid
                state = entry["state"]
                marque = entry["register_marque"] if isinstance(entry["register_marque"], str) \
                    else None
                readable = ambiguous = eligible = take = 0
                reason: str | None = None
                snapshot: dict[str, Any] | None = None
                if state == "register_unverified":
                    if entry["register_marque"] is not None or entry["snapshot_id"] is not None \
                            or entry["reason_code"] is not None:
                        raise invalid
                    reason = "WORK_SCOPE_REGISTER_UNVERIFIED"
                elif state in ("captured", "snapshot_unusable"):
                    if marque is None or not 1 <= len(marque) <= 120 \
                            or not isinstance(entry["snapshot_id"], str):
                        raise invalid
                    try:
                        UUID(entry["snapshot_id"])
                    except ValueError:
                        raise invalid from None
                    snapshot = next((row for row in self.catalog_snapshots.values()
                                     if row["id"] == entry["snapshot_id"]), None)
                    declaration = ((snapshot or {}).get("retrieval_metadata") or {}) \
                        .get("capture_scope")
                    filters = declaration.get("filters") if isinstance(declaration, Mapping) else None
                    if snapshot is None or snapshot.get("source_family") != "government" \
                            or snapshot.get("resource_id") != _WLTP_RESOURCE_ID \
                            or snapshot.get("activated_at") is None \
                            or not isinstance(filters, Mapping) or filters.get("tozar") != marque:
                        raise snapshot_invalid
                    if snapshot["id"] in seen:
                        raise invalid
                    seen.add(snapshot["id"])
                    if state == "snapshot_unusable":
                        if entry["reason_code"] not in self._WORK_SCOPE_UNUSABLE_REASONS:
                            raise invalid
                        reason = entry["reason_code"]
                    else:
                        if entry["reason_code"] is not None:
                            raise invalid
                        try:
                            self._readable_snapshot(snapshot["id"], False)
                        except AppError:
                            raise snapshot_invalid from None
                        in_range = [row for row in self._snapshot_candidates(snapshot["id"])
                                    if self._in_plan_years(row, year_from, year_to)]
                        readable = sum(1 for row in in_range if row["status"] != "ambiguous")
                        ambiguous = sum(1 for row in in_range if row["status"] == "ambiguous")
                        eligible = sum(1 for row in in_range if row["status"] == "candidate")
                        if ambiguous > readable:
                            state, reason = "vocabulary_insufficient", \
                                "WORK_SCOPE_VOCABULARY_INSUFFICIENT"
                        else:
                            state = "prepared"
                            take = min(eligible, budget)
                            budget -= take
                            prepared += 1
                            queued += take
                            batch_total += -(-take // size)
                else:
                    raise invalid
                decided.append({
                    "priority": index + 1, "unit_key": entry["unit_key"],
                    "register_marque": marque, "state": state, "reason_code": reason,
                    "snapshot_id": snapshot["id"] if snapshot else None,
                    "snapshot_key": snapshot["snapshot_key"] if snapshot else None,
                    "capture_scope_key": (snapshot["retrieval_metadata"]["capture_scope"]
                                          .get("scope_key") if snapshot else None),
                    "readable": readable, "ambiguous": ambiguous, "eligible": eligible,
                    "take": take})

            # Pass 2: write the whole decision.
            now = _now()
            record = {"id": str(uuid4()), "work_scope_id": scope_id, "revision": revision,
                      "scope_digest": digest, "prepared_by_run_id": str(run_id),
                      "unit_count": len(units), "prepared_unit_count": prepared,
                      "queued_item_count": queued, "batch_count": batch_total, "created_at": now}
            self.work_scope_preparations[record["id"]] = record
            position = batch_number = 0
            for unit in decided:
                row = {"id": str(uuid4()), "preparation_id": record["id"],
                       "work_scope_id": scope_id, "revision": revision,
                       "priority": unit["priority"], "unit_key": unit["unit_key"],
                       "register_marque": unit["register_marque"], "state": unit["state"],
                       "reason_code": unit["reason_code"], "snapshot_id": unit["snapshot_id"],
                       "snapshot_key": unit["snapshot_key"],
                       "capture_scope_key": unit["capture_scope_key"],
                       "readable_count": unit["readable"], "ambiguous_count": unit["ambiguous"],
                       "eligible_count": unit["eligible"], "queued_count": unit["take"],
                       "created_at": now}
                self.work_scope_units.append(row)
                if not unit["take"]:
                    continue
                chosen = sorted((candidate for candidate
                                 in self._snapshot_candidates(unit["snapshot_id"])
                                 if candidate["status"] == "candidate"
                                 and self._in_plan_years(candidate, year_from, year_to)),
                                key=_variant_page_key)[:unit["take"]]
                for start in range(0, len(chosen), size):
                    batch_number += 1
                    members = chosen[start:start + size]
                    batch = {"id": str(uuid4()), "preparation_id": record["id"],
                             "work_scope_id": scope_id, "revision": revision,
                             "scope_digest": digest, "batch_number": batch_number,
                             "unit_id": row["id"], "unit_key": row["unit_key"],
                             "snapshot_id": row["snapshot_id"],
                             "snapshot_key": row["snapshot_key"],
                             "item_count": len(members), "first_position": position + 1,
                             "created_at": now}
                    self.work_scope_batches.append(batch)
                    for offset, candidate in enumerate(members, start=1):
                        position += 1
                        self.work_scope_queue_items.append({
                            "id": str(uuid4()), "preparation_id": record["id"],
                            "batch_id": batch["id"], "position": position,
                            "batch_position": offset, "unit_key": row["unit_key"],
                            "snapshot_id": row["snapshot_id"], "candidate_id": candidate["id"],
                            "candidate_key": candidate["candidate_key"], "created_at": now})
            return self._work_scope_preparation_summary(record, False)

    def bind_work_scope_batch_run(self, batch_id: UUID, run_id: UUID, expected_revision: int,
                                  expected_digest: str, bound_by: UUID) -> dict[str, Any]:
        """Mirrors `bind_work_scope_batch_run`, refusal for refusal."""
        run_invalid = AppError("WORK_SCOPE_BATCH_RUN_INVALID",
                               "this run cannot execute this batch", 422)
        if batch_id is None or run_id is None or bound_by is None:
            raise run_invalid
        with self.lock:
            batch = next((row for row in self.work_scope_batches
                          if row["id"] == str(batch_id)), None)
            if batch is None:
                raise NotFoundError("work_scope_batch", str(batch_id))
            plan = self.work_scopes[batch["work_scope_id"]]
            if (str(plan["project_id"]), str(bound_by)) not in self.members:
                raise NotFoundError("work_scope_batch", str(batch_id))
            if self.projects[plan["project_id"]].get("workflow_key") != "swarm_v2":
                raise AppError("WORK_SCOPE_WORKFLOW_UNSUPPORTED",
                               "this project's engine does not read a mapping plan", 409)
            if plan["closed_at"] is not None:
                raise AppError("WORK_SCOPE_NOT_EDITABLE", "the mapping plan is not editable", 409)
            if plan["head_revision"] != batch["revision"] \
                    or plan["head_digest"] != batch["scope_digest"] \
                    or expected_revision != batch["revision"] \
                    or expected_digest != batch["scope_digest"]:
                raise AppError("WORK_SCOPE_STALE",
                               "the plan changed since it was read; reload it and try again", 409)
            run = self.runs.get(str(run_id))
            if run is None or str(run.get("conversation_id")) != plan["conversation_id"] \
                    or (run.get("run_identity") or {}).get("workflow_key") != "swarm_v2":
                raise run_invalid
            existing = next((row for row in self.work_scope_batch_runs
                             if row["run_id"] == str(run_id)), None)
            if existing is not None:
                if existing["batch_id"] == str(batch_id):
                    return {**existing, "replayed": True}
                raise AppError("WORK_SCOPE_BATCH_RUN_TAKEN",
                               "this run is already bound to another batch", 409)
            if run.get("status") in self._WORK_SCOPE_TERMINAL_RUN_STATES:
                raise run_invalid
            # 20260924000100: paused, one live batch, settled, and in order.
            self._check_batch_continuation(plan, batch)
            return self._bind_batch(plan, batch, run_id, bound_by)

    #: A batch whose run finished its work (20260924000100). Never bound again.
    _WORK_SCOPE_SETTLED_RUN_STATES = frozenset({"completed", "partial_success"})

    def _work_scope_paused(self, work_scope_id: str) -> bool:
        latest = max((row for row in self.work_scope_controls
                      if row["work_scope_id"] == str(work_scope_id)),
                     key=lambda row: row["sequence"], default=None)
        return latest is not None and latest["action"] == "pause"

    def _batch_run_status(self, binding: Mapping[str, Any]) -> Any:
        return (self.runs.get(binding["run_id"]) or {}).get("status")

    def _live_batch_binding(self, work_scope_id: str) -> dict[str, Any] | None:
        live = [row for row in self.work_scope_batch_runs
                if row["work_scope_id"] == str(work_scope_id)
                and self._batch_run_status(row) not in self._WORK_SCOPE_TERMINAL_RUN_STATES]
        return max(live, key=lambda row: row["bound_at"], default=None)

    def _batch_settled(self, batch_id: str) -> bool:
        return any(row["batch_id"] == str(batch_id)
                   and self._batch_run_status(row) in self._WORK_SCOPE_SETTLED_RUN_STATES
                   for row in self.work_scope_batch_runs)

    def _next_batch(self, preparation_id: str) -> dict[str, Any] | None:
        return min((row for row in self.work_scope_batches
                    if row["preparation_id"] == preparation_id
                    and not self._batch_settled(row["id"])),
                   key=lambda row: row["batch_number"], default=None)

    def _check_batch_continuation(self, plan: Mapping[str, Any], batch: Mapping[str, Any]) -> None:
        """The continuation rules, in the SQL's order (caller holds the lock)."""
        if self._work_scope_paused(plan["id"]):
            raise AppError("WORK_SCOPE_PAUSED",
                           "the mapping plan is paused; resume it before starting a batch", 409)
        if self._live_batch_binding(plan["id"]) is not None:
            raise AppError("WORK_SCOPE_BATCH_IN_PROGRESS",
                           "another batch of this plan is still running", 409)
        if self._batch_settled(batch["id"]):
            raise AppError("WORK_SCOPE_BATCH_ALREADY_COMPLETED", "this batch already completed", 409)
        following = self._next_batch(batch["preparation_id"])
        if following is None or following["id"] != batch["id"]:
            raise AppError("WORK_SCOPE_BATCH_NOT_NEXT",
                           "only the next batch of the plan can start", 409)

    def _bind_batch(self, plan: Mapping[str, Any], batch: Mapping[str, Any], run_id: Any,
                    bound_by: Any) -> dict[str, Any]:
        attempt = 1 + max((binding["attempt"] for binding in self.work_scope_batch_runs
                           if binding["batch_id"] == batch["id"]), default=0)
        binding = {"id": str(uuid4()), "batch_id": batch["id"],
                   "work_scope_id": plan["id"], "run_id": str(run_id), "attempt": attempt,
                   "bound_by": str(bound_by), "bound_at": _now()}
        self.work_scope_batch_runs.append(binding)
        return {**binding, "replayed": False}

    # -- batch runs, pause / resume and progress (scoped catalog PR3) ---------
    #
    # Mirrors `20260924000100_catalog_work_scope_batch_runs.sql` refusal for
    # refusal and in the same order. `tests/test_work_scope_batches.py` and the
    # PostgreSQL suite hold the two to the same answers.

    def set_work_scope_paused(self, work_scope_id: UUID, paused: bool,
                              requested_by: UUID) -> dict[str, Any]:
        if work_scope_id is None or not isinstance(paused, bool) or requested_by is None:
            raise AppError("WORK_SCOPE_CONTROL_INVALID", "invalid mapping plan control", 422)
        with self.lock:
            plan = self.work_scopes.get(str(work_scope_id))
            if plan is None or not self._is_member(plan["project_id"], requested_by):
                raise NotFoundError("work_scope", str(work_scope_id))
            if self.projects[plan["project_id"]].get("workflow_key") != "swarm_v2":
                raise AppError("WORK_SCOPE_WORKFLOW_UNSUPPORTED",
                               "this project's engine does not read a mapping plan", 409)
            if plan["closed_at"] is not None:
                raise AppError("WORK_SCOPE_NOT_EDITABLE", "the mapping plan is not editable", 409)
            history = [row for row in self.work_scope_controls if row["work_scope_id"] == plan["id"]]
            last = max(history, key=lambda row: row["sequence"], default=None)
            if (last is not None and last["action"] == "pause") == paused:
                return {"changed": False, "paused": paused,
                        "control": dict(last) if last is not None else None}
            row = {"id": str(uuid4()), "work_scope_id": plan["id"],
                   "sequence": (last["sequence"] if last is not None else 0) + 1,
                   "action": "pause" if paused else "resume",
                   "requested_by": str(requested_by), "created_at": _now()}
            self.work_scope_controls.append(row)
            return {"changed": True, "paused": paused, "control": dict(row)}

    def create_work_scope_batch_run(self, work_scope_id: UUID, batch_id: UUID,
                                    expected_revision: int, expected_digest: str, *,
                                    run_id: UUID, run_identity: dict[str, Any], content: str,
                                    metadata: dict[str, Any], requested_by: UUID,
                                    idempotency_key: str, request_fingerprint: str,
                                    max_user_active: int | None = None,
                                    max_project_active: int | None = None) -> dict[str, Any]:
        """Mirrors `create_work_scope_batch_run`: the run creator and the binding,
        together or not at all, under one lock."""
        run_invalid = AppError("WORK_SCOPE_BATCH_RUN_INVALID",
                               "this run cannot execute this batch", 422)
        stale = AppError("WORK_SCOPE_STALE",
                         "the plan changed since it was read; reload it and try again", 409)
        if work_scope_id is None or batch_id is None or run_id is None or requested_by is None:
            raise run_invalid
        if not idempotency_key or not str(idempotency_key).strip() \
                or not request_fingerprint or not str(request_fingerprint).strip():
            raise AppError("WORK_SCOPE_BATCH_IDEMPOTENCY_REQUIRED",
                           "starting a batch requires an idempotency key", 422)
        with self.lock:
            plan = self.work_scopes.get(str(work_scope_id))
            if plan is None or not self._is_member(plan["project_id"], requested_by):
                raise NotFoundError("work_scope", str(work_scope_id))
            existing = self.find_run_by_idempotency(UUID(plan["conversation_id"]), requested_by,
                                                    idempotency_key)
            if existing is not None:
                binding = next((row for row in self.work_scope_batch_runs
                                if row["run_id"] == existing["id"]), None)
                if existing.get("request_fingerprint") != request_fingerprint \
                        or binding is None or binding["work_scope_id"] != plan["id"]:
                    raise AppError("IDEMPOTENCY_CONFLICT",
                                   "idempotency key was already used with a different payload", 409)
                # A replay the caller would LAUNCH is a start: it obeys the
                # start rules as they stand now.
                if existing["status"] == "queued" \
                        and existing.get("launch_state") in {"pending", "launch_failed"}:
                    if plan["closed_at"] is not None:
                        raise AppError("WORK_SCOPE_NOT_EDITABLE", "the mapping plan is not editable",
                                       409)
                    bound = next(row for row in self.work_scope_batches
                                 if row["id"] == binding["batch_id"])
                    if bound["revision"] != plan["head_revision"] \
                            or bound["scope_digest"] != plan["head_digest"]:
                        raise stale
                    if self._work_scope_paused(plan["id"]):
                        raise AppError("WORK_SCOPE_PAUSED",
                                       "the mapping plan is paused; resume it before starting a batch",
                                       409)
                return {"run": existing, "binding": dict(binding), "created": False}
            if self.projects[plan["project_id"]].get("workflow_key") != "swarm_v2":
                raise AppError("WORK_SCOPE_WORKFLOW_UNSUPPORTED",
                               "this project's engine does not read a mapping plan", 409)
            if not isinstance(run_identity, Mapping) \
                    or run_identity.get("workflow_key") != "swarm_v2":
                raise run_invalid
            if plan["closed_at"] is not None:
                raise AppError("WORK_SCOPE_NOT_EDITABLE", "the mapping plan is not editable", 409)
            if expected_revision != plan["head_revision"] or expected_digest != plan["head_digest"]:
                raise stale
            batch = next((row for row in self.work_scope_batches
                          if row["id"] == str(batch_id) and row["work_scope_id"] == plan["id"]),
                         None)
            if batch is None:
                raise NotFoundError("work_scope_batch", str(batch_id))
            if batch["revision"] != plan["head_revision"] \
                    or batch["scope_digest"] != plan["head_digest"]:
                raise stale
            if self._work_scope_paused(plan["id"]):
                raise AppError("WORK_SCOPE_PAUSED",
                               "the mapping plan is paused; resume it before starting a batch", 409)
            live = self._live_batch_binding(plan["id"])
            if live is not None:
                if live["batch_id"] != batch["id"]:
                    raise AppError("WORK_SCOPE_BATCH_IN_PROGRESS",
                                   "another batch of this plan is still running", 409)
                return {"run": dict(self.runs[live["run_id"]]), "binding": dict(live),
                        "created": False}
            self._check_batch_continuation(plan, batch)
            created = self.create_message_and_run(
                UUID(plan["conversation_id"]), content, metadata, requested_by, idempotency_key,
                request_fingerprint, max_user_active, max_project_active, run_id=run_id,
                run_identity=run_identity)
            if not created.get("created") or created["run"]["id"] != str(run_id):
                raise run_invalid
            binding = self._bind_batch(plan, batch, run_id, requested_by)
            binding.pop("replayed", None)
            return {"run": created["run"], "binding": binding, "created": True}

    def work_scope_progress(self, work_scope_id: UUID) -> dict[str, Any] | None:
        """Mirrors `work_scope_progress`: derived from the bound runs' own durable
        status and promotion events, counts and codes only."""
        with self.lock:
            plan = self.work_scopes.get(str(work_scope_id))
            if plan is None:
                return None
            history = [row for row in self.work_scope_controls if row["work_scope_id"] == plan["id"]]
            control = max(history, key=lambda row: row["sequence"], default=None)
            live_binding = self._live_batch_binding(plan["id"])
            live = None
            if live_binding is not None:
                batch = next(row for row in self.work_scope_batches
                             if row["id"] == live_binding["batch_id"])
                run = self.runs[live_binding["run_id"]]
                live = {"batch_id": batch["id"], "batch_number": batch["batch_number"],
                        "revision": batch["revision"], "unit_key": batch["unit_key"],
                        "item_count": batch["item_count"], "attempt": live_binding["attempt"],
                        "run_id": run["id"], "run_status": run["status"],
                        "launch_state": run.get("launch_state"),
                        "bound_at": live_binding["bound_at"]}
            preparation = next((row for row in self.work_scope_preparations.values()
                                if row["work_scope_id"] == plan["id"]
                                and row["revision"] == plan["head_revision"]), None)
            return {"work_scope_id": plan["id"], "revision": plan["head_revision"],
                    "digest": plan["head_digest"], "closed": plan["closed_at"] is not None,
                    "paused": control is not None and control["action"] == "pause",
                    "control": ({"sequence": control["sequence"], "action": control["action"],
                                 "created_at": control["created_at"]}
                                if control is not None else None),
                    "live": live,
                    "preparation": (self._work_scope_preparation_progress(preparation)
                                    if preparation is not None else None)}

    def _work_scope_preparation_progress(self, preparation: Mapping[str, Any]) -> dict[str, Any]:
        batches = sorted((row for row in self.work_scope_batches
                          if row["preparation_id"] == preparation["id"]),
                         key=lambda row: row["batch_number"])
        batch_ids = {row["id"] for row in batches}
        bound = [row for row in self.work_scope_batch_runs if row["batch_id"] in batch_ids]
        bound_runs = {row["run_id"] for row in bound}
        items = [row for row in self.work_scope_queue_items
                 if row["preparation_id"] == preparation["id"]]
        batch_of = {row["candidate_key"]: row["batch_id"] for row in items}
        promoted: set[str] = set()
        refused: set[str] = set()
        for event in self.run_events:
            if event["run_id"] not in bound_runs or event["event_type"] not in (
                    "catalog_variant_promoted", "catalog_promotion_refused"):
                continue
            payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
            key = payload.get("candidate_key")
            if key not in batch_of:
                continue
            if event["event_type"] == "catalog_promotion_refused":
                refused.add(key)
            elif payload.get("promoted") is True:
                promoted.add(key)
        rows = []
        for batch in batches:
            runs = sorted((row for row in bound if row["batch_id"] == batch["id"]),
                          key=lambda row: row["attempt"])
            statuses = [self._batch_run_status(row) for row in runs]
            if "completed" in statuses:
                state = "completed"
            elif "partial_success" in statuses:
                state = "partial"
            elif any(status not in self._WORK_SCOPE_TERMINAL_RUN_STATES for status in statuses):
                state = "active"
            elif runs:
                state = "interrupted"
            else:
                state = "pending"
            keys = [key for key, owner in batch_of.items() if owner == batch["id"]]
            batch_promoted = sum(1 for key in keys if key in promoted)
            batch_refused = sum(1 for key in keys if key in refused and key not in promoted)
            settled = state in ("completed", "partial")
            rows.append({"batch": batch, "state": state, "attempts": len(runs),
                         "settled": settled,
                         "last_bound_at": runs[-1]["bound_at"] if runs else None,
                         "last_run": runs[-1] if runs else None,
                         "promoted": batch_promoted, "refused": batch_refused,
                         "unresolved": (batch["item_count"] - batch_promoted - batch_refused
                                        if settled else 0)})
        units = sorted((row for row in self.work_scope_units
                        if row["preparation_id"] == preparation["id"]),
                       key=lambda row: row["priority"])
        unit_rows = []
        for unit in units:
            mine = [row for row in rows if row["batch"]["unit_key"] == unit["unit_key"]]
            unit_rows.append({
                "priority": unit["priority"], "unit_key": unit["unit_key"],
                "state": unit["state"], "reason_code": unit["reason_code"],
                "readable_count": unit["readable_count"],
                "ambiguous_count": unit["ambiguous_count"],
                "eligible_count": unit["eligible_count"], "queued_count": unit["queued_count"],
                "batch_count": len(mine),
                "settled_batches": sum(1 for row in mine if row["settled"]),
                "active": any(row["state"] == "active" for row in mine),
                "promoted": sum(row["promoted"] for row in mine),
                "refused": sum(row["refused"] for row in mine),
                "unresolved": sum(row["unresolved"] for row in mine)})
        following = next((row for row in rows if not row["settled"]), None)
        recent = sorted((row for row in rows if row["attempts"] > 0),
                        key=lambda row: row["last_bound_at"], reverse=True)[:5]
        return {
            "id": preparation["id"], "revision": preparation["revision"],
            "created_at": preparation["created_at"], "unit_count": preparation["unit_count"],
            "prepared_unit_count": preparation["prepared_unit_count"],
            "queued_item_count": preparation["queued_item_count"],
            "batch_count": preparation["batch_count"],
            "units": unit_rows,
            "next": ({"batch_id": following["batch"]["id"],
                      "batch_number": following["batch"]["batch_number"],
                      "unit_key": following["batch"]["unit_key"],
                      "item_count": following["batch"]["item_count"],
                      "first_position": following["batch"]["first_position"],
                      "state": following["state"], "attempts": following["attempts"]}
                     if following is not None else None),
            "recent": [{"batch_id": row["batch"]["id"],
                        "batch_number": row["batch"]["batch_number"],
                        "unit_key": row["batch"]["unit_key"],
                        "item_count": row["batch"]["item_count"], "state": row["state"],
                        "attempts": row["attempts"], "run_id": row["last_run"]["run_id"],
                        "run_status": self._batch_run_status(row["last_run"]),
                        "promoted": row["promoted"], "refused": row["refused"],
                        "unresolved": row["unresolved"]} for row in recent],
            "batches": {"total": len(rows),
                        "settled": sum(1 for row in rows if row["settled"]),
                        "active": sum(1 for row in rows if row["state"] == "active"),
                        "interrupted": sum(1 for row in rows if row["state"] == "interrupted")},
            "items": {"total": preparation["queued_item_count"],
                      "promoted": sum(row["promoted"] for row in rows),
                      "refused": sum(row["refused"] for row in rows),
                      "unresolved": sum(row["unresolved"] for row in rows)},
        }

    def work_scope_batch_for_run(self, run_id: UUID) -> dict[str, Any] | None:
        """Mirrors `work_scope_batch_for_run`: None when the run is unbound."""
        with self.lock:
            binding = next((row for row in self.work_scope_batch_runs
                            if row["run_id"] == str(run_id)), None)
            if binding is None:
                return None
            batch = next(row for row in self.work_scope_batches
                         if row["id"] == binding["batch_id"])
            candidates = {row["id"]: row for row in self.catalog_candidates.values()}
            items = []
            for item in sorted((row for row in self.work_scope_queue_items
                                if row["batch_id"] == batch["id"]),
                               key=lambda row: row["batch_position"]):
                candidate = candidates[item["candidate_id"]]
                items.append({
                    "position": item["position"], "batch_position": item["batch_position"],
                    "candidate_id": item["candidate_id"], "candidate_key": item["candidate_key"],
                    "manufacturer": candidate["manufacturer"],
                    "commercial_model": candidate["commercial_model"],
                    "model_year_start": candidate.get("model_year_start"),
                    "model_year_end": candidate.get("model_year_end"),
                    "official_model_code": candidate.get("official_model_code"),
                    "trim": candidate.get("trim"), "status": candidate["status"],
                    "snapshot_id": candidate["snapshot_id"]})
            return {"binding": dict(binding), "batch": dict(batch), "items": items}

    def catalog_canonical_manufacturer_coverage(self, manufacturers: list[str]) -> list[dict[str, Any]]:
        """Mirrors `catalog_canonical_manufacturer_coverage`: an exact count per
        requested marque, zero included, plus one NULL-marque total row."""
        if not isinstance(manufacturers, list) or len(manufacturers) > 64 \
                or any(not isinstance(name, str) or not 1 <= len(name) <= 120
                       for name in manufacturers):
            raise AppError("REPOSITORY_ERROR", "bounded catalog read failed", 502)
        with self.lock:
            models = {row["id"]: row["manufacturer"] for row in self.catalog_models}
            variants = [models.get(row["model_id"]) for row in self.catalog_model_variants]
        rows: list[dict[str, Any]] = [
            {"manufacturer": name, "canonical_variants": variants.count(name)}
            for name in sorted(set(manufacturers))]
        rows.append({"manufacturer": None, "canonical_variants": len(variants)})
        return rows

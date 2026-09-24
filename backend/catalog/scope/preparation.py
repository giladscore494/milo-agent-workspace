"""Scoped catalog PR2: prepare ONE exact plan revision into a durable queue.

Where this runs, and where it cannot
------------------------------------

Only inside the operator capture entrypoint (`backend/catalog/operator_capture
.py`), under the lease of a prepared OPERATOR CAPTURE run, in the capture Cloud
Run job -- the one place this repository constructs a Government transport.
The product worker never imports this module, and the database refuses the
final write (`prepare_work_scope_queue`) from any run that is not an operator
capture run, so Government preparation stays outside the paid batch-run clock
by construction.

What it does, in order
----------------------

1.  Reads the plan and the EXACT revision it was asked to prepare, and refuses
    unless that revision is still the plan's head with exactly that digest. A
    stale plan is never prepared.
2.  Parses the revision's stored canonical text through the same strict
    `scope_from_text` the plan was validated with, and refuses a plan made
    under another manufacturer directory.
3.  For each unit in priority order:
    *   no verified register spelling (`directory.py`) -> `register_unverified`;
        nothing is captured, because filtering the register by a guessed
        spelling would be inventing a query the register never answers;
    *   otherwise a SCOPED refresh (`capture_scope.py`) of the pinned WLTP
        resource filtered to that one marque. The refresh is query-aware, so
        an unchanged register reuses the unit's last scoped snapshot and a
        changed one captures a new immutable snapshot through the existing
        ingestion path -- the same bounds, completeness gate, normalization and
        activation as every other capture;
    *   the landed snapshot is read back by its exact key and must declare this
        unit's scope. Usable -> `captured`; otherwise `snapshot_unusable` with
        the projection's own reason.
4.  Hands the decisions to `prepare_work_scope_queue`, which re-derives
    everything it can from durable state -- the plan's units, years, limit and
    batch size, every count, and the vocabulary gate -- and materializes the
    deterministic queue and its batches in one transaction.

A capture that FAILS (transport, completeness, schema, lease) fails the whole
preparation: a revision is prepared once, so it must never be prepared from a
partial read. Nothing about normalization changes here; rows the reviewed
vocabulary cannot read stay `ambiguous` and are never queued.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping
from uuid import UUID

from backend.catalog.government import source as src
from backend.catalog.government.capture_scope import (CaptureScope, CaptureScopeError,
                                                      declared_scope)
from backend.catalog.government.projection import snapshot_usability
from backend.catalog.government.refresh import GovernmentCatalogRefresh, RefreshOutcome

from . import contract as wsc
from . import directory as mdir

#: Static refusals this stage can produce. Each names a condition, never a
#: plan, a register value or a SQL message.
PREPARATION_REASONS: Mapping[str, str] = {
    "WORK_SCOPE_PREPARATION_PLAN_UNAVAILABLE":
        "the mapping plan named for preparation does not exist",
    "WORK_SCOPE_PREPARATION_STALE":
        "the mapping plan revision named for preparation is not the plan's current head",
    "WORK_SCOPE_PREPARATION_DIRECTORY_MISMATCH":
        "the mapping plan was made under a different manufacturer directory",
    "WORK_SCOPE_PREPARATION_SNAPSHOT_MISSING":
        "a scoped capture landed but its snapshot could not be read back as that scope",
}


class WorkScopePreparationError(Exception):
    """Preparation refused before anything was written. Static reasons only."""

    def __init__(self, reason_code: str) -> None:
        if reason_code not in PREPARATION_REASONS:
            raise ValueError("work scope preparation reason must come from the static allowlist")
        self.reason_code = reason_code
        self.safe_message = PREPARATION_REASONS[reason_code]
        super().__init__(self.safe_message)


@dataclass(frozen=True)
class UnitCapture:
    """What the Government side of preparing one unit did."""

    unit_key: str
    priority: int
    #: `captured`, `snapshot_unusable` or `register_unverified` -- the decision
    #: the database is handed; it turns `captured` into `prepared` or
    #: `vocabulary_insufficient` itself.
    state: str
    register_marque: str | None = None
    snapshot_id: str | None = None
    snapshot_key: str = ""
    reason_code: str | None = None
    #: What the scoped refresh established: `unchanged` (the register still
    #: publishes the version of the unit's last usable scoped snapshot, so
    #: nothing was read), `reused` (another run's identical completed
    #: snapshot), `landed` (this run captured the scope and landed or replayed
    #: its snapshot), `adopted` (this run took over the pending snapshot a
    #: failed run left for exactly this content and finished it), or `none`
    #: when nothing was captured for the unit.
    capture: str = "none"
    #: The run the unit's snapshot was adopted from (its previous writer),
    #: when `capture` is `adopted`; durable in `catalog_snapshot_adoptions`
    #: and as a `catalog_snapshot_adopted` run event as well.
    adopted_from_run_id: str = ""
    adoption_seq: int = 0
    #: What landing the unit's capture cost, per phase (counts and seconds
    #: only; `backend.catalog.government.ingest.new_metrics`). Empty when
    #: nothing was ingested (unchanged register, unverified marque).
    ingestion: Mapping[str, Any] = field(default_factory=dict)

    def submission(self) -> dict[str, Any]:
        """The unit exactly as `prepare_work_scope_queue` accepts it."""
        return {"unit_key": self.unit_key, "priority": self.priority, "state": self.state,
                "register_marque": self.register_marque, "snapshot_id": self.snapshot_id,
                "reason_code": self.reason_code}


@dataclass(frozen=True)
class WorkScopePreparation:
    """One preparation: the Government side, and what the database decided."""

    work_scope_id: str
    revision: int
    scope_digest: str
    captures: tuple[UnitCapture, ...]
    summary: Mapping[str, Any]

    @property
    def replayed(self) -> bool:
        return bool(self.summary.get("replayed"))


def _refresh_state(outcome: RefreshOutcome) -> str:
    """Only what the refresh states for certain -- never inferred from counts."""
    if not outcome.changed:
        return "unchanged"
    if outcome.report is not None and outcome.report.reused_existing:
        return "reused"
    if outcome.report is not None and outcome.report.adopted_from_run_id:
        return "adopted"
    return "landed"


def read_prepared_revision(repository: Any, work_scope_id: str, revision: int,
                           digest: str) -> wsc.WorkScope:
    """The EXACT head revision, parsed strictly, or a refusal."""
    plan = repository.get_work_scope(UUID(str(work_scope_id)))
    if plan is None:
        raise WorkScopePreparationError("WORK_SCOPE_PREPARATION_PLAN_UNAVAILABLE")
    if plan.get("closed_at") is not None or int(plan.get("head_revision") or 0) != revision \
            or str(plan.get("head_digest") or "") != digest:
        raise WorkScopePreparationError("WORK_SCOPE_PREPARATION_STALE")
    stored = repository.get_work_scope_revision(UUID(str(work_scope_id)), revision)
    if stored is None or str(stored.get("digest") or "") != digest:
        raise WorkScopePreparationError("WORK_SCOPE_PREPARATION_STALE")
    try:
        scope = wsc.scope_from_text(str(stored.get("scope_text") or ""))
    except wsc.WorkScopeError:
        raise WorkScopePreparationError("WORK_SCOPE_PREPARATION_STALE") from None
    if scope.digest() != digest:
        raise WorkScopePreparationError("WORK_SCOPE_PREPARATION_STALE")
    if scope.directory_version != mdir.DIRECTORY_VERSION:
        raise WorkScopePreparationError("WORK_SCOPE_PREPARATION_DIRECTORY_MISMATCH")
    return scope


def capture_unit(repository: Any, lease: Any, *, client: Any, unit_key: str, priority: int,
                 cancellation_checker: Callable[[], bool] | None = None,
                 event_sink: Callable[[str, Mapping[str, Any]], None] | None = None,
                 ) -> UnitCapture:
    """The Government side of ONE unit: a scoped refresh, read back and judged."""
    entry = mdir.DIRECTORY_BY_KEY[unit_key]
    if not entry.register_marque_verified or entry.register_marque is None:
        return UnitCapture(unit_key=unit_key, priority=priority, state="register_unverified")
    scope = CaptureScope.for_register_marque(entry.register_marque)
    outcome = GovernmentCatalogRefresh(
        repository, lease, client=client, resource_id=src.WLTP_RESOURCE_ID,
        cancellation_checker=cancellation_checker, event_sink=event_sink,
        capture_scope=scope).sync_if_changed(package_id=src.CKAN_PACKAGE_ID)
    row = repository.find_active_catalog_snapshot(
        src.GOVERNMENT_SOURCE_FAMILY, src.WLTP_RESOURCE_ID, outcome.active_snapshot_key)
    try:
        declared = declared_scope(row) if row is not None else None
    except CaptureScopeError:
        declared = None
    if row is None or declared is None or declared.key() != scope.key():
        raise WorkScopePreparationError("WORK_SCOPE_PREPARATION_SNAPSHOT_MISSING")
    reason = snapshot_usability(row)
    return UnitCapture(
        unit_key=unit_key, priority=priority,
        state="captured" if reason is None else "snapshot_unusable",
        register_marque=entry.register_marque, snapshot_id=str(row["id"]),
        snapshot_key=str(row["snapshot_key"]), reason_code=reason,
        capture=_refresh_state(outcome),
        adopted_from_run_id=(outcome.report.adopted_from_run_id
                             if outcome.report is not None else ""),
        adoption_seq=outcome.report.adoption_seq if outcome.report is not None else 0,
        ingestion=dict(outcome.report.ingestion) if outcome.report is not None else {})


def prepare_work_scope(repository: Any, lease: Any, *, client: Any, work_scope_id: str,
                       revision: int, digest: str,
                       cancellation_checker: Callable[[], bool] | None = None,
                       event_sink: Callable[[str, Mapping[str, Any]], None] | None = None,
                       ) -> WorkScopePreparation:
    """Prepare one exact head revision. See the module docstring for the order."""
    scope = read_prepared_revision(repository, work_scope_id, revision, digest)
    captures = tuple(
        capture_unit(repository, lease, client=client, unit_key=key, priority=priority,
                     cancellation_checker=cancellation_checker, event_sink=event_sink)
        for priority, key in enumerate(scope.units, start=1))
    summary = repository.prepare_work_scope_queue(
        lease.run_id,
        {"work_scope_id": str(work_scope_id), "revision": int(revision),
         "scope_digest": str(digest), "units": [unit.submission() for unit in captures]},
        worker_id=lease.worker_id, attempt=lease.attempt, lease_token=lease.lease_token)
    return WorkScopePreparation(work_scope_id=str(work_scope_id), revision=int(revision),
                                scope_digest=str(digest), captures=captures, summary=summary)


__all__ = ["PREPARATION_REASONS", "UnitCapture", "WorkScopePreparation",
           "WorkScopePreparationError", "capture_unit", "prepare_work_scope",
           "read_prepared_revision"]

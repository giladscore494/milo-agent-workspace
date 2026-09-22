"""Catalog PR3: a deterministic `sync_if_changed`, and a bounded diff.

What this is FOR
----------------

A connection that is read once is a snapshot of a moment. This module is the
operation that keeps it current without rebuilding the world: it asks the
register what version it is publishing, compares that against the newest
snapshot MILO already holds, and ingests only when the answer differs.

What this is NOT
----------------

**Not a schedule.** Nothing here registers a cron entry, a timer, a Cloud
Scheduler job or a background thread, and no production entrypoint calls it.
It is a service operation a reviewed caller invokes; activating it is a
separate, deliberate act with its own approval, exactly as activating a live
capture is.

The shape of the answer
-----------------------

*   **No change** -- the resource's published version equals the active
    snapshot's. Nothing is captured, nothing is written, no snapshot is
    created and no research is requested. The outcome says so, with the version
    both sides agree on.
*   **Changed** -- a full bounded capture runs and lands as a NEW immutable
    snapshot. The previous one is untouched: PR1 makes an active snapshot
    immutable and PR2 makes activation the last step, so a partial or failed
    refresh can never replace the last usable snapshot -- it simply never
    activates, and readers keep answering from the one before it.
*   **Diff** -- the new snapshot is compared against the previous one and the
    difference is reported as ADDED / CHANGED / REMOVED candidates. That is the
    focused work item: the Commander can research a handful of new model years
    instead of re-reading the whole register.

Where the comparison runs, and what is bounded
-----------------------------------------------

Inside PostgreSQL, in `public.catalog_snapshot_candidate_diff`. The real
resource is ~101 000 candidate rows per side, so comparing them by reading both
into this process would materialize 200 000 rows to answer one question -- the
unbounded read this package exists to avoid.

The three COUNTS that comes back are EXACT for the whole resource, always: they
are computed over every matching row of both snapshots, never over a page, and
they are never refused for size. What is bounded is the ITEM LIST -- at most
`MAX_DIFF_ITEMS` deltas, dropped whole rather than truncated when more than
that changed, because a truncated list is a diff claiming a completeness it
does not have. `SnapshotDiff.bounded` says which happened, and the counts are
the answer either way.

Rollback, stated exactly
------------------------

There is no "active pointer" to move, and this module does not invent one. A
snapshot is active or it is not, an active one is immutable, and raw history is
never deleted -- so rolling back means READING an older snapshot again, by
pinning its `snapshot_key`, which every reader here already accepts. Anything
stronger (deactivating a snapshot, deleting a capture) is not something the
existing immutable contracts permit, and this module does not add it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from . import source as src
from .capture_scope import CaptureScope
from .client import DataGovClient, ResourceMetadata
from .ingest import GovernmentCatalogIngestor, IngestionReport
from .source import GovernmentSourceError
from backend.runtime import CancellationRequested

from backend.catalog.diff import (DIFF_IDENTITY, MAX_DIFF_ITEMS, candidate_identity,
                                  diff_rows, is_count_row)
from backend.errors import AppError

from .projection import (GovernmentProjectionError, resolve_active_snapshot,
                         snapshot_usability)

#: The comparison itself is ONE rule, in `backend/catalog/diff.py`, mirrored by
#: `public.catalog_snapshot_candidate_diff`. Re-exported here because this is
#: where a reader of the refresh looks for it.


@dataclass(frozen=True)
class CandidateDelta:
    """One vehicle identity that appeared, changed or disappeared."""

    state: str
    manufacturer: str
    commercial_model: str
    model_year_start: int
    model_year_end: int
    official_model_code: str | None = None
    trim: str | None = None
    changed_fields: tuple[str, ...] = ()
    upstream_record_id: str = ""


@dataclass(frozen=True)
class SnapshotDiff:
    """What changed between two snapshots of one resource.

    The three counts are exact. The three lists are bounded by
    `MAX_DIFF_ITEMS` and are dropped WHOLE rather than truncated when they do
    not fit, for the same reason PR2 drops an over-long page-checksum list
    whole: a truncated list is a diff claiming completeness it does not have.
    """

    previous_snapshot_key: str
    snapshot_key: str
    added_count: int
    changed_count: int
    removed_count: int
    added: tuple[CandidateDelta, ...] = ()
    changed: tuple[CandidateDelta, ...] = ()
    removed: tuple[CandidateDelta, ...] = ()
    bounded: bool = False

    @property
    def is_empty(self) -> bool:
        return not (self.added_count or self.changed_count or self.removed_count)


@dataclass(frozen=True)
class RefreshOutcome:
    """What one `sync_if_changed` did, in terms a reviewer can check."""

    changed: bool
    resource_id: str
    upstream_version: str
    upstream_version_kind: str
    active_snapshot_key: str = ""
    report: IngestionReport | None = None
    diff: SnapshotDiff | None = None
    #: True when this refresh created no snapshot and requested no work. The
    #: property a scheduled refresh has to have: a quiet source is quiet.
    no_op: bool = True
    detail: tuple[str, ...] = ()

    @property
    def diff_unavailable(self) -> bool:
        """A CHANGED source whose difference could not be computed.

        Distinct from an empty diff, and the distinction matters: an empty diff
        means nothing changed between two snapshots, while this means the
        comparison was not performed. The new snapshot landed and is readable
        either way; what is absent is the focused work item.
        """
        return self.changed and self.diff is None

    @property
    def research_required(self) -> bool:
        """Whether this refresh produced focused work for the Commander.

        False for an unchanged source, and false for a changed source whose
        diff turned out to be empty. A refresh that found nothing new never
        asks anyone to look at anything.
        """
        return bool(self.diff is not None and not self.diff.is_empty)


def _delta(row: Mapping[str, Any]) -> CandidateDelta:
    """One returned delta row, as the dataclass a refresh reports."""
    return CandidateDelta(
        state=str(row["state"]), manufacturer=str(row["manufacturer"]),
        commercial_model=str(row["commercial_model"]),
        model_year_start=int(row["model_year_start"]),
        model_year_end=int(row["model_year_end"]),
        official_model_code=row.get("official_model_code"), trim=row.get("trim"),
        changed_fields=tuple(row.get("changed_fields") or ()),
        upstream_record_id=str(row.get("upstream_record_id") or ""))


def snapshot_diff_from_rows(rows: Sequence[Mapping[str, Any]], *,
                            previous_snapshot_key: str = "",
                            snapshot_key: str = "") -> SnapshotDiff:
    """Assemble a `SnapshotDiff` from what the diff RPC returned.

    The three counts are read from the rows rather than counted here: they are
    exact for the WHOLE resource, and the returned items are at most
    `MAX_DIFF_ITEMS` of them. `bounded` is therefore "more changed than was
    listed", derived by comparing the two -- never by re-counting a page.
    """
    if not rows:
        # Only reachable if a repository returned nothing at all; both
        # implementations return a COUNT ROW for an empty diff.
        return SnapshotDiff(previous_snapshot_key=previous_snapshot_key,
                            snapshot_key=snapshot_key, added_count=0, changed_count=0,
                            removed_count=0)
    counts = rows[0]
    items = [_delta(row) for row in rows if not is_count_row(row)]
    added = tuple(item for item in items if item.state == "added")
    changed = tuple(item for item in items if item.state == "changed")
    removed = tuple(item for item in items if item.state == "removed")
    added_count = int(counts["added_count"])
    changed_count = int(counts["changed_count"])
    removed_count = int(counts["removed_count"])
    return SnapshotDiff(
        previous_snapshot_key=previous_snapshot_key, snapshot_key=snapshot_key,
        added_count=added_count, changed_count=changed_count, removed_count=removed_count,
        added=added, changed=changed, removed=removed,
        # Dropped WHOLE rather than truncated, so "listed fewer than counted"
        # can only mean the whole list was dropped.
        bounded=len(items) < added_count + changed_count + removed_count)


def diff_candidate_sets(previous: Sequence[Mapping[str, Any]],
                        current: Sequence[Mapping[str, Any]], *,
                        previous_snapshot_key: str = "", snapshot_key: str = "",
                        max_items: int = MAX_DIFF_ITEMS) -> SnapshotDiff:
    """Compare two snapshots' candidate readings, deterministically.

    The pure form of the comparison, over rows already in hand. Production does
    NOT go through here -- `GovernmentCatalogRefresh` asks the repository,
    which asks PostgreSQL -- but it is the same rule, because both call
    `backend/catalog/diff.py`.
    """
    return snapshot_diff_from_rows(
        diff_rows(previous, current, limit=max_items),
        previous_snapshot_key=previous_snapshot_key, snapshot_key=snapshot_key)


class GovernmentCatalogRefresh:
    """The deterministic, schedulable refresh operation. NOT scheduled here.

    QUERY-AWARE (scoped catalog PR2). A refresh is a refresh OF ONE SCOPE.
    Without a `capture_scope` it is the register's, and it compares only with
    snapshots that declare no scope. With one it is that scope's: its version
    check and its diff are against the newest usable snapshot declaring exactly
    that scope, never against the register. Otherwise a scoped capture would
    be skipped as "unchanged" merely because the register's version had not
    moved, and its diff would report every other marque as removed.
    """

    def __init__(self, repository: Any, lease: Any, *, client: DataGovClient,
                 resource_id: str = src.WLTP_RESOURCE_ID,
                 cancellation_checker: Callable[[], bool] | None = None,
                 event_sink: Callable[[str, Mapping[str, Any]], None] | None = None,
                 capture_scope: CaptureScope | None = None) -> None:
        self._repository = repository
        self._lease = lease
        self._client = client
        self._resource_id = src.require_allowed_resource(resource_id)
        self._cancellation_checker = cancellation_checker
        self._event_sink = event_sink
        self._capture_scope = capture_scope

    def sync_if_changed(self, *, package_id: str = src.CKAN_PACKAGE_ID,
                        query: Mapping[str, str] | None = None) -> RefreshOutcome:
        """Read the resource's published version, and ingest only if it moved.

        The metadata read is ONE bounded `package_show` request. An unchanged
        source therefore costs one request and zero durable writes -- which is
        what makes this safe to run often, and what makes "no change, no
        research" a property rather than an intention.
        """
        if self._capture_scope is not None:
            # A scoped refresh captures exactly its scope's query. Restating it
            # is allowed; stating anything else is a contradiction, refused
            # before a request is sent.
            if query is not None and dict(query) != self._capture_scope.query():
                raise GovernmentSourceError("GOV_CAPTURE_SCOPE_MISMATCH")
            query = None
        metadata = self._client.package_show(self._resource_id, package_id=package_id)
        active = self._active_snapshot()
        if active is not None and self._matches(active, metadata):
            self._emit("catalog_refresh_unchanged",
                       {"resource_id": self._resource_id,
                        "upstream_version": metadata.upstream_version})
            return RefreshOutcome(
                changed=False, resource_id=self._resource_id,
                upstream_version=metadata.upstream_version,
                upstream_version_kind=metadata.upstream_version_kind,
                active_snapshot_key=str(active["snapshot_key"]), no_op=True,
                detail=("the register publishes the version this catalog already holds",))

        report = GovernmentCatalogIngestor(
            self._repository, self._lease, client=self._client,
            cancellation_checker=self._cancellation_checker,
            event_sink=self._event_sink).ingest_resource(
                self._resource_id, package_id=package_id, query=query,
                capture_scope=self._capture_scope)
        landed = self._repository.find_active_catalog_snapshot(
            src.GOVERNMENT_SOURCE_FAMILY, self._resource_id, report.snapshot_key)
        diff, refusal = self._diff(active, landed, report.snapshot_key)
        self._emit("catalog_refresh_completed",
                   {"resource_id": self._resource_id, "snapshot_key": report.snapshot_key,
                    "added": diff.added_count if diff else 0,
                    "changed": diff.changed_count if diff else 0,
                    "removed": diff.removed_count if diff else 0,
                    "diff_refused": refusal or ""})
        return RefreshOutcome(
            changed=True, resource_id=self._resource_id,
            upstream_version=metadata.upstream_version,
            upstream_version_kind=metadata.upstream_version_kind,
            active_snapshot_key=report.snapshot_key, report=report, diff=diff,
            # A replay of an identical capture creates no snapshot and writes
            # nothing, so it is still a no-op even though the version check
            # sent it down this path. A refused comparison is NOT a no-op: the
            # snapshot landed and nobody knows what moved.
            no_op=bool(diff) and report.candidate_count == 0 and diff.is_empty,
            detail=() if not refusal else
                   (f"the snapshot landed; its difference was not computed ({refusal})",))

    # --- helpers -------------------------------------------------------------

    def _active_snapshot(self) -> Mapping[str, Any] | None:
        """The newest USABLE snapshot OF THIS SCOPE, or None when there is none."""
        try:
            return resolve_active_snapshot(self._repository, resource_id=self._resource_id,
                                           snapshot_key=None, allow_incomplete=False,
                                           capture_scope=self._capture_scope)
        except GovernmentProjectionError:
            # No active snapshot, or none this catalog may read from. Either
            # way there is nothing to compare against, so the refresh behaves
            # exactly as a first ingestion does.
            return None

    @staticmethod
    def _matches(snapshot: Mapping[str, Any], metadata: ResourceMetadata) -> bool:
        """Whether the register is publishing the version this snapshot holds.

        Compared on the version the RESOURCE states -- its revision or its
        modification time -- and never on the retrieval time or on a digest of
        whatever came back, either of which would make every refresh look like
        a change.
        """
        return (str(snapshot.get("upstream_version")) == metadata.upstream_version
                and str(snapshot.get("upstream_version_kind")) == metadata.upstream_version_kind
                and snapshot_usability(snapshot) is None)

    def _diff(self, previous: Mapping[str, Any] | None, landed: Mapping[str, Any] | None,
              snapshot_key: str) -> tuple[SnapshotDiff | None, str | None]:
        """Ask the DATABASE what changed, and never fabricate an answer.

        Returns `(diff, refusal)` rather than raising, because the CAPTURE must
        not be undone by a failure of the COMPARISON: by the time this is
        called an immutable snapshot has already landed and is already
        readable. What is absent on a refusal is the focused work item, not the
        data.

        The comparison itself does not read a candidate row into this process
        at all -- `catalog_snapshot_candidate_diff` computes it over both whole
        snapshots and returns exact counts with at most `MAX_DIFF_ITEMS`
        deltas. A first ingestion has no previous side, which is not a refusal:
        everything is added, and the database is told so with a null.
        """
        self._check_cancelled()
        if landed is None:
            # The snapshot this ingestion just activated is not readable back.
            # Comparing against an EMPTY set here would report every row of the
            # other side as added or removed -- a fabricated diff, which is
            # worse than no diff at all.
            return None, "GOV_PROJECTION_NO_ACTIVE_SNAPSHOT"
        try:
            rows = list(self._repository.catalog_snapshot_candidate_diff(
                None if previous is None else previous["id"], landed["id"],
                limit=MAX_DIFF_ITEMS, allow_incomplete=False))
        except AppError:
            return None, "GOV_QUERY_UNAVAILABLE"
        self._check_cancelled()
        return snapshot_diff_from_rows(
            rows, snapshot_key=snapshot_key,
            previous_snapshot_key="" if previous is None else str(previous["snapshot_key"])), None

    def _check_cancelled(self) -> None:
        if self._cancellation_checker is not None and self._cancellation_checker():
            raise CancellationRequested("RUN_CANCELLED")

    def _emit(self, event_type: str, payload: Mapping[str, Any]) -> None:
        if self._event_sink is not None:
            self._event_sink(event_type, dict(payload))


__all__ = ["DIFF_IDENTITY", "MAX_DIFF_ITEMS", "CandidateDelta",
           "GovernmentCatalogRefresh", "RefreshOutcome", "SnapshotDiff",
           "candidate_identity", "diff_candidate_sets", "snapshot_diff_from_rows"]

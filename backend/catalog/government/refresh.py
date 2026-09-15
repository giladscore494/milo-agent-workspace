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
    difference is reported as bounded ADDED / CHANGED / REMOVED candidates.
    That is the focused work item: the Commander can research a handful of new
    model years instead of re-reading the whole register.

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

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from . import source as src
from .client import DataGovClient, ResourceMetadata
from .ingest import GovernmentCatalogIngestor, IngestionReport
from .projection import GovernmentProjectionError, resolve_active_snapshot, snapshot_usability

#: How many changed candidates one diff reports individually. The COUNTS are
#: always exact; the lists are bounded, because a refresh that changed
#: everything must not be able to produce an unbounded work item.
MAX_DIFF_ITEMS = 100

#: What a diff compares: the candidate's COMPLETE stated identity, never its
#: surrogate id and never the register's own `_id`.
#:
#: Not the id, because a re-capture produces new rows with new uuids for the
#: same vehicles, so comparing ids would report every row as added and every
#: row as removed. Not the register's `_id` either -- PR2 recorded that the
#: datastore reuses that number space across captures, so it identifies a row
#: within ONE retrieval and nothing beyond it.
#:
#: The COMPLETE identity, dimensions included, because the register publishes
#: several rows that share a marque, model, year, code and trim and differ only
#: in their coded dimensions. Leaving the dimensions out would make those rows
#: one identity, and which of them "won" would then depend on the order they
#: were read in -- a diff that changes with the read order is not a diff.
DIFF_IDENTITY = ("manufacturer", "commercial_model", "model_year_start",
                 "model_year_end", "official_model_code", "trim", "identity_dimensions")


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
    def research_required(self) -> bool:
        """Whether this refresh produced focused work for the Commander.

        False for an unchanged source, and false for a changed source whose
        diff turned out to be empty. A refresh that found nothing new never
        asks anyone to look at anything.
        """
        return bool(self.diff is not None and not self.diff.is_empty)


def _identity(row: Mapping[str, Any]) -> tuple:
    """One candidate's complete stated identity, as a hashable, ordered tuple.

    The dimensions are sorted, so two readings that stated the same dimensions
    in a different key order are the same identity -- which they are.
    """
    identity: list[Any] = []
    for name in DIFF_IDENTITY:
        value = row.get(name)
        if name == "identity_dimensions":
            identity.append(tuple(sorted((str(key), str(item))
                                         for key, item in (value or {}).items())))
        else:
            identity.append(value)
    return tuple(identity)


def _delta(row: Mapping[str, Any], state: str,
           changed_fields: Sequence[str] = ()) -> CandidateDelta:
    return CandidateDelta(
        state=state, manufacturer=str(row["manufacturer"]),
        commercial_model=str(row["commercial_model"]),
        model_year_start=int(row["model_year_start"]),
        model_year_end=int(row["model_year_end"]),
        official_model_code=row.get("official_model_code"), trim=row.get("trim"),
        changed_fields=tuple(changed_fields),
        upstream_record_id=str(row.get("upstream_record_id") or ""))


def diff_candidate_sets(previous: Sequence[Mapping[str, Any]],
                        current: Sequence[Mapping[str, Any]], *,
                        previous_snapshot_key: str = "", snapshot_key: str = "",
                        max_items: int = MAX_DIFF_ITEMS) -> SnapshotDiff:
    """Compare two snapshots' candidate readings, deterministically.

    `added` and `removed` are identities one side has and the other does not.
    `changed` is an identity BOTH state whose READING differs -- which, since
    every stated identity field is part of the identity itself, means its
    STATUS: a candidate the newer capture could read where the older one left
    it `ambiguous`, or the reverse.

    A snapshot may state one identity more than once; the register does. The
    FIRST row wins, deterministically, because the rows arrive in a stable
    order -- and the alternative, last-wins, would make the diff depend on
    which duplicate happened to be read last.

    Ordered by the identity's own values, so two runs over the same pair of
    snapshots produce the same diff.
    """
    before: dict[tuple, Mapping[str, Any]] = {}
    after: dict[tuple, Mapping[str, Any]] = {}
    for row in previous:
        before.setdefault(_identity(row), row)
    for row in current:
        after.setdefault(_identity(row), row)
    added = [_delta(after[key], "added") for key in sorted(set(after) - set(before),
                                                           key=lambda item: tuple(
                                                               "" if part is None else str(part)
                                                               for part in item))]
    removed = [_delta(before[key], "removed") for key in sorted(set(before) - set(after),
                                                                key=lambda item: tuple(
                                                                    "" if part is None else str(part)
                                                                    for part in item))]
    changed: list[CandidateDelta] = []
    for key in sorted(set(before) & set(after),
                      key=lambda item: tuple("" if part is None else str(part) for part in item)):
        if before[key].get("status") != after[key].get("status"):
            changed.append(_delta(after[key], "changed", ("status",)))
    total = len(added) + len(changed) + len(removed)
    bounded = total > max_items
    return SnapshotDiff(
        previous_snapshot_key=previous_snapshot_key, snapshot_key=snapshot_key,
        added_count=len(added), changed_count=len(changed), removed_count=len(removed),
        # Dropped WHOLE when they do not fit, never truncated: the counts stay
        # exact either way, and `bounded` says which happened.
        added=() if bounded else tuple(added), changed=() if bounded else tuple(changed),
        removed=() if bounded else tuple(removed), bounded=bounded)


class GovernmentCatalogRefresh:
    """The deterministic, schedulable refresh operation. NOT scheduled here."""

    def __init__(self, repository: Any, lease: Any, *, client: DataGovClient,
                 resource_id: str = src.WLTP_RESOURCE_ID,
                 cancellation_checker: Callable[[], bool] | None = None,
                 event_sink: Callable[[str, Mapping[str, Any]], None] | None = None) -> None:
        self._repository = repository
        self._lease = lease
        self._client = client
        self._resource_id = src.require_allowed_resource(resource_id)
        self._cancellation_checker = cancellation_checker
        self._event_sink = event_sink

    def sync_if_changed(self, *, package_id: str = src.CKAN_PACKAGE_ID,
                        query: Mapping[str, str] | None = None) -> RefreshOutcome:
        """Read the resource's published version, and ingest only if it moved.

        The metadata read is ONE bounded `package_show` request. An unchanged
        source therefore costs one request and zero durable writes -- which is
        what makes this safe to run often, and what makes "no change, no
        research" a property rather than an intention.
        """
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

        previous = self._candidate_rows(active) if active is not None else ()
        report = GovernmentCatalogIngestor(
            self._repository, self._lease, client=self._client,
            cancellation_checker=self._cancellation_checker,
            event_sink=self._event_sink).ingest_resource(
                self._resource_id, package_id=package_id, query=query)
        current = self._candidate_rows(
            self._repository.find_active_catalog_snapshot(
                src.GOVERNMENT_SOURCE_FAMILY, self._resource_id, report.snapshot_key))
        diff = diff_candidate_sets(
            previous, current,
            previous_snapshot_key=str(active["snapshot_key"]) if active is not None else "",
            snapshot_key=report.snapshot_key)
        self._emit("catalog_refresh_completed",
                   {"resource_id": self._resource_id, "snapshot_key": report.snapshot_key,
                    "added": diff.added_count, "changed": diff.changed_count,
                    "removed": diff.removed_count})
        return RefreshOutcome(
            changed=True, resource_id=self._resource_id,
            upstream_version=metadata.upstream_version,
            upstream_version_kind=metadata.upstream_version_kind,
            active_snapshot_key=report.snapshot_key, report=report, diff=diff,
            # A replay of an identical capture creates no snapshot and writes
            # nothing, so it is still a no-op even though the version check
            # sent it down this path.
            no_op=report.candidate_count == 0 and diff.is_empty)

    # --- helpers -------------------------------------------------------------

    def _active_snapshot(self) -> Mapping[str, Any] | None:
        """The newest USABLE snapshot, or None when there is none to compare to."""
        try:
            return resolve_active_snapshot(self._repository, resource_id=self._resource_id,
                                           snapshot_key=None, allow_incomplete=False)
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

    def _candidate_rows(self, snapshot: Mapping[str, Any] | None) -> tuple[Mapping[str, Any], ...]:
        """One snapshot's candidates joined to the register id each was read from.

        Bounded by the same page bound every other read in this namespace uses;
        a snapshot larger than the diff can hold reports exact COUNTS with the
        item lists dropped whole.
        """
        if snapshot is None:
            return ()
        rows: list[Mapping[str, Any]] = []
        offset = 0
        while True:
            page = list(self._repository.catalog_candidate_variant_page(
                snapshot["id"], limit=200, offset=offset, allow_incomplete=False))
            rows.extend(page)
            if len(page) < 200:
                return tuple(rows)
            offset += 200

    def _emit(self, event_type: str, payload: Mapping[str, Any]) -> None:
        if self._event_sink is not None:
            self._event_sink(event_type, dict(payload))


__all__ = ["DIFF_IDENTITY", "MAX_DIFF_ITEMS", "CandidateDelta", "GovernmentCatalogRefresh",
           "RefreshOutcome", "SnapshotDiff", "diff_candidate_sets"]

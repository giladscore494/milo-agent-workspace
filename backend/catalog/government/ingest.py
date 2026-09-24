"""Capture -> snapshot -> raw records -> candidates -> activation. In that order.

The order is the safety property
--------------------------------

1.  **Capture first, and completely.** The whole query is fetched and validated
    as one result set BEFORE a single durable write. A partial, inconsistent,
    over-limit or malformed capture therefore leaves no trace at all -- not a
    pending snapshot, not a row.
2.  **Read the whole capture.** Normalization is a pure function of the capture
    and runs before anything durable exists, so the summary the snapshot
    carries and the candidates the database receives come from ONE computation.
    A row that cannot be read is counted here, once, and that count becomes
    part of the snapshot's own durable state rather than of this report.
3.  **Open the snapshot**, carrying that summary. It is born `pending` and can
    never be born active: the payload preparer refuses `activated_at` and
    `stored_record_count` as inputs and the guarded RPC refuses them again.
4.  **Append every raw record**, each with the exact page and index it occupied.
5.  **Record the readings** that step 2 produced.
6.  **Activate last.** The database's own gate refuses activation unless the
    snapshot holds exactly as many records as the upstream declared, so a
    prefix cannot be activated even by a caller that wanted to.

Because activation is last and gated, a crash, a cancellation or a lost lease
at ANY point leaves a non-active snapshot. Nothing downstream reads a non-active
snapshot, so an interrupted ingestion is invisible to a reader rather than being
a smaller truth.

Every durable write carries the lease
-------------------------------------

`run_id`, `worker_id`, `attempt` and `lease_token` travel with every write, and
`assert_worker_lease` validates all four atomically before a byte is written. A
stale, superseded or expired worker writes nothing at all. This module holds no
other way to reach the database: there is no direct insert here, and no function
or table name is ever assembled from data.

Replay, refresh and ownership
-----------------------------

*   **Exact replay is a deterministic no-op.** The snapshot identity is a
    function of what was captured, so re-reading unchanged content derives the
    same `content_sha256`, the same `snapshot_key`, the same record keys and the
    same candidate keys. Every write collapses onto the existing row.
*   **A later run may REUSE an already-active identical snapshot.** It is
    returned by the idempotent snapshot write, recognised as another run's
    completed work, and left completely alone -- this module never attempts to
    fill or decide a capture it did not open, which the database would refuse
    anyway.
*   **A later run may not adopt another run's LIVE, unfinished capture.** That
    is a refusal here, with its own reason, rather than an attempt that fails
    halfway.
*   **An ORPHANED capture is adopted, never duplicated.** The key is derived
    from content, so a re-capture of unchanged content lands on the pending
    snapshot a failed run left behind. The database lets an operator capture
    run become its writer only while it is pending and its current writer
    ended `failed`, `cancelled` or `timed_out` with no live lease
    (`20260924000200_catalog_ingestion_recovery.sql`). `created_by_run_id`
    never moves; the adoption row (and a `catalog_snapshot_adopted` event)
    records the previous writer durably. The adopter then re-submits EVERY row through the
    same idempotent writes -- a row already stored collapses onto its key and
    must match it exactly, a missing one is written -- and activation still
    passes the completeness gate.
*   **Changed content is a new snapshot.** A different capture derives a
    different `content_sha256`, so it is a different `snapshot_key` and a
    different row; the previous snapshot and all its raw records are untouched.
*   **A failed refresh never replaces the last valid active snapshot**, because
    it never reaches activation and the previous snapshot is never modified.

This is AT-LEAST-ONCE delivery onto idempotent writes, not exactly-once. There
is no exactly-once guarantee across the window between a durable write and the
checkpoint that records it: a crash inside that window re-executes the step on
resume. That is safe here for two specific reasons, and only those -- every
upstream call is READ-ONLY, and every durable write is idempotent on a key
derived from content -- so a replayed step lands on the same rows.

A snapshot states its own reading gap
-------------------------------------

An active snapshot may legitimately hold rows this catalog could not read: a
code/label contradiction is the register disagreeing with itself, and inventing
an identity for such a row would be worse than not reading it. What must never
happen is that the gap disappears -- which is what happened while the only
record of it was this report, produced once by whichever run wrote the
snapshot and gone on every replay.

So the gap is DURABLE. `retrieval_metadata` carries the contract the rows were
read under, how many were read, how many were refused, the count per reason and
a bounded list of the refused ids. A replay recomputes it and is held to it
(`GOV_SNAPSHOT_NORMALIZATION_DRIFT`), and every report -- first write, replay,
cross-run reuse -- states what the SNAPSHOT says rather than what the caller
just computed.

Reading is then a separate decision, made in `projection.py`: a snapshot with
an unresolved gap is not usable, so the last usable snapshot keeps answering
and a reader who wants the incomplete one has to say so.

No model, no provider and no canonical write
--------------------------------------------

Nothing on this path calls a provider or a model. Nothing here writes
`catalog_models` or `catalog_model_variants` -- there is no repository method
that could, and `service_role` holds SELECT only on both. No claim, verdict or
evidence link is created either: PR2 provenance is snapshot -> raw record ->
candidate, and evidence mapping is PR3's work.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from backend.catalog.contracts import CATALOG_WRITE_BATCH_SIZE
from backend.engines.swarm_v2.evidence import WorkerLease
from backend.errors import LEASE_FAILURE_CODES, AppError, RepositoryFailure
from backend.catalog.write_diagnostics import CatalogWriteDiagnostics
from backend.runtime import CancellationRequested

from . import snapshot as snapshot_module
from . import source as src
from .capture_scope import CaptureScope, CaptureScopeError, declared_scope
from .client import DataGovClient, ResourceCapture
from .normalize import (CaptureNormalization, RAW_ONLY_CONTRACT, UNMAPPED_FIELDS,
                        read_capture)
from .source import GovernmentSourceError

#: The bound on how many per-row refusals one report carries verbatim. The
#: COUNT is always exact; the list is bounded so a systematically unreadable
#: capture cannot produce an unbounded report.
MAX_REPORTED_REJECTIONS = 100

#: Ingestion-level refusals, beyond the capture and normalization vocabularies.
GOVERNMENT_INGESTION_REASONS: Mapping[str, str] = {
    "GOV_SNAPSHOT_OWNED_BY_ANOTHER_RUN":
        "this capture was opened by another run that is live or did not fail; it cannot be adopted",
    "GOV_SNAPSHOT_NOT_ACTIVATED":
        "the capture was written in full but the snapshot did not activate",
    "GOV_SNAPSHOT_NORMALIZATION_DRIFT":
        "this snapshot was read under a different normalization contract than this code applies",
    # Scoped catalog PR2. The same content under a different declared scope is
    # a different claim about what the snapshot is, so it is never adopted.
    "GOV_SNAPSHOT_SCOPE_MISMATCH":
        "this snapshot declares a different capture scope than this ingestion requested",
}


class GovernmentIngestionError(ValueError):
    """An ingestion refusal carrying ONLY a static, code-owned reason."""

    def __init__(self, reason_code: str):
        if reason_code not in GOVERNMENT_INGESTION_REASONS:
            raise ValueError("government ingestion reason must come from the static allowlist")
        self.reason_code = reason_code
        self.safe_message = GOVERNMENT_INGESTION_REASONS[reason_code]
        super().__init__(self.safe_message)


@dataclass(frozen=True)
class IngestionReport:
    """What one ingestion did, in terms a reviewer can check against the rows."""

    snapshot_id: str
    snapshot_key: str
    content_sha256: str
    resource_id: str
    upstream_version: str
    upstream_version_kind: str
    schema_fingerprint: str
    declared_record_count: int
    stored_record_count: int
    page_count: int
    #: Candidates THIS ingestion recorded. Zero on a replay or a reuse, where
    #: the candidates were already durable and nothing was written.
    candidate_count: int
    candidate_status_counts: Mapping[str, int]
    #: The reading gap, read back from the SNAPSHOT'S OWN durable metadata --
    #: never from whatever this ingestion happened to compute. That is what
    #: makes a replay and a cross-run reuse report the same truth as the
    #: ingestion that wrote the snapshot.
    normalization_contract: str = ""
    normalized_record_count: int = 0
    normalization_issue_count: int = 0
    normalization_issues: Mapping[str, int] = field(default_factory=dict)
    normalization_issue_records: tuple[str, ...] = ()
    #: `(upstream_record_id, reason_code)` for rows THIS ingestion captured,
    #: stored and could not read into an identity -- with the reason for each,
    #: bounded by `MAX_REPORTED_REJECTIONS`. Empty on a replay or a reuse,
    #: where this ingestion read nothing: the `normalization_*` fields below
    #: are the ones that always speak, because they are read back off the
    #: snapshot itself. No raw row is ever lost because of a refusal here -- it
    #: is durable either way.
    rejected_records: tuple[tuple[str, str], ...] = ()
    rejected_record_count: int = 0
    #: The captured fields this ingestion deliberately did not read, each with
    #: a reviewer's reason.
    unmapped_fields: tuple[tuple[str, str], ...] = UNMAPPED_FIELDS
    activated: bool = False
    #: True when the snapshot already existed, ACTIVE, and held exactly this
    #: content -- so this ingestion wrote nothing at all.
    reused_existing: bool = False
    #: The run that opened the snapshot, which is this run unless it was reused.
    created_by_run_id: str = ""
    #: The scope the snapshot DECLARES (`capture_scope.py`), read back off the
    #: snapshot; empty for an unscoped capture.
    capture_scope_key: str = ""
    #: The run this ingestion ADOPTED the pending snapshot from (its previous
    #: writer), or empty. The database keeps the same fact durably in
    #: `catalog_snapshot_adoptions` and as a `catalog_snapshot_adopted` event.
    adopted_from_run_id: str = ""
    #: That adoption's number in the snapshot's adoption sequence, or 0.
    adoption_seq: int = 0
    #: What this ingestion cost, per phase: database calls and wall time, and
    #: for the row writes how many rows were inserted and how many were
    #: already present (`new_metrics`). Counts and seconds only.
    ingestion: Mapping[str, Any] = field(default_factory=dict)
    candidates: tuple[Mapping[str, Any], ...] = field(default=(), repr=False)


class GovernmentCatalogIngestor:
    """One deterministic, bounded, read-only Government ingestion."""

    def __init__(self, repository: Any, lease: WorkerLease, *, client: DataGovClient,
                 cancellation_checker: Callable[[], bool] | None = None,
                 event_sink: Callable[[str, Mapping[str, Any]], None] | None = None) -> None:
        self._repository = repository
        self._lease = lease
        self._client = client
        self._cancellation_checker = cancellation_checker
        self._event_sink = event_sink
        #: This ingestion's per-phase measurements (`new_metrics`), reset by
        #: every `ingest_capture`.
        self._metrics: dict[str, Any] = new_metrics()

    @property
    def _lease_kwargs(self) -> dict[str, Any]:
        return {"worker_id": self._lease.worker_id, "attempt": self._lease.attempt,
                "lease_token": self._lease.lease_token}

    # --- the whole path ------------------------------------------------------

    def ingest_resource(self, resource_id: str, *, package_id: str = src.CKAN_PACKAGE_ID,
                        query: Mapping[str, str] | None = None,
                        capture_scope: CaptureScope | None = None) -> IngestionReport:
        """Capture one complete bounded query and land it in the catalog.

        With a `capture_scope` the query IS the scope's: a caller may restate
        it, never contradict it, and the snapshot declares the scope durably so
        no unpinned reader can take it for the register.
        """
        if capture_scope is not None:
            if query is not None and dict(query) != capture_scope.query():
                raise GovernmentSourceError("GOV_CAPTURE_SCOPE_MISMATCH")
            query = capture_scope.query()
        # Both source identities are allowlisted at THIS boundary too, so an
        # entry point that is read on its own states the rule it applies rather
        # than relying on a callee to apply it.
        capture = self._client.capture_resource(
            src.require_allowed_resource(resource_id),
            package_id=src.require_allowed_package(package_id), query=query)
        return self.ingest_capture(capture, capture_scope=capture_scope)

    def ingest_capture(self, capture: ResourceCapture, *,
                       capture_scope: CaptureScope | None = None) -> IngestionReport:
        """Land a capture this process already took. AN INTERNAL SEAM.

        Stated accurately, because the distinction matters: this method TRUSTS
        its argument. The validation that makes a `ResourceCapture` meaningful
        -- the allowlists, the publisher and version checks, the per-page echo
        checks, the completeness gate -- all live in
        `DataGovClient.capture_resource`, which is the only thing in this
        package that produces one from a response. A `ResourceCapture` built by
        hand is a Python object with the right fields and nothing more: it is
        not remotely verified, not cryptographically attested, and carries no
        evidence that any of its digests were ever computed over bytes a server
        sent.

        It is separated from `ingest_resource` so a capture can be taken once
        and landed without a second transport -- which is a test convenience,
        and is why this method is not a public entry point. Callers that need
        the guarantees call `ingest_resource`.
        """
        self._check_cancelled()
        self._metrics = new_metrics()
        # Read the WHOLE capture before opening the snapshot. The summary the
        # snapshot carries and the candidates the database receives then come
        # from one computation of one pure function, so they cannot disagree --
        # and the gap is durable from the moment the snapshot exists rather
        # than being discovered halfway through writing it.
        normalization = read_capture([record for _, record in capture.located_records()],
                                     resource_id=capture.resource_id)
        payload = snapshot_module.snapshot_payload(capture, normalization, capture_scope)
        with self._phase("snapshot", request=payload):
            snapshot = self._repository.record_catalog_snapshot(
                self._lease.run_id, payload, **self._lease_kwargs,
                diagnostics=self._diagnostics("snapshot"))
        # A replay or a reuse lands on an EXISTING row. Whatever it declares
        # must be exactly what this ingestion asked for -- a scoped request is
        # never satisfied by an unscoped snapshot, nor the reverse.
        self._check_scope(snapshot, capture_scope)
        # The CREATOR, which never changes. Anything but this run -- including
        # a snapshot this run already adopted -- goes through `_adopt`, which
        # asks the database for the current writer and is idempotent for it.
        owner = str(snapshot.get("created_by_run_id"))
        adopted_from = ""
        if owner != str(self._lease.run_id):
            # Another run opened this capture. If it FINISHED it, this run has
            # nothing to do and must not touch it. If it did not, this run may
            # continue it only by ADOPTING it, which the database allows only
            # when that run is over and the capture is still pending.
            if snapshot.get("activated_at") is not None:
                self._check_normalization(snapshot, normalization)
                return self._report(capture, snapshot, candidates=(), reused=True)
            snapshot, adoption = self._adopt(snapshot, payload, normalization)
            if adoption:
                adopted_from = str(adoption.get("previous_writer_run_id") or "")
                self._metrics["adoption_seq"] = int(adoption.get("adoption_seq") or 0)
                self._metrics["previous_writer_run_id"] = adopted_from

        if snapshot.get("activated_at") is not None:
            # Exact replay of our own completed capture: the records and the
            # candidates are already durable and the snapshot is frozen, so
            # this is a deterministic no-op rather than a second ingestion.
            self._check_normalization(snapshot, normalization)
            self._emit("catalog_snapshot_replayed", {"snapshot_key": snapshot["snapshot_key"]})
            return self._report(capture, snapshot, candidates=(), reused=False)

        records = self._write_records(capture, snapshot)
        candidates, rejected = self._write_candidates(normalization, records, snapshot)
        snapshot = self._activate(snapshot)
        return self._report(capture, snapshot, candidates=candidates, reused=False,
                            rejected=rejected, adopted_from=adopted_from)

    # --- steps ---------------------------------------------------------------

    def _adopt(self, snapshot: Mapping[str, Any], payload: Mapping[str, Any],
               normalization: CaptureNormalization
               ) -> tuple[Mapping[str, Any], Mapping[str, Any] | None]:
        """Become the writer of an ORPHANED pending capture of exactly this content.

        The database decides whether the previous writer is over and the
        snapshot still pending; this side only refuses early what can never be
        adopted, and holds the stored reading gap to the one this capture
        reconstructs before asking. A refusal is the same static reason as
        before. Answers the snapshot and the adoption (None when this run
        already was the writer, which is an idempotent replay).
        """
        adopt = getattr(self._repository, "adopt_catalog_snapshot", None)
        if not callable(adopt) or snapshot.get("validation_state") == "failed":
            raise GovernmentIngestionError("GOV_SNAPSHOT_OWNED_BY_ANOTHER_RUN")
        self._check_normalization(snapshot, normalization)
        try:
            with self._phase("snapshot", request=payload):
                answer = adopt(self._lease.run_id, dict(payload), **self._lease_kwargs,
                               diagnostics=self._diagnostics("adopt", snapshot))
        except AppError as refusal:
            if refusal.code == "CATALOG_SNAPSHOT_ADOPTION_REFUSED":
                raise GovernmentIngestionError("GOV_SNAPSHOT_OWNED_BY_ANOTHER_RUN") from None
            raise
        adopted = answer.get("snapshot") if isinstance(answer, Mapping) else None
        adoption = answer.get("adoption") if isinstance(answer, Mapping) else None
        if not isinstance(adopted, Mapping) or adopted.get("id") != snapshot.get("id"):
            raise GovernmentIngestionError("GOV_SNAPSHOT_OWNED_BY_ANOTHER_RUN")
        if adoption is not None and (not isinstance(adoption, Mapping) or str(
                adoption.get("adopted_by_run_id")) != str(self._lease.run_id)):
            raise GovernmentIngestionError("GOV_SNAPSHOT_OWNED_BY_ANOTHER_RUN")
        return adopted, adoption

    def _write_records(self, capture: ResourceCapture, snapshot: Mapping[str, Any]
                       ) -> list[tuple[dict[str, Any], Mapping[str, Any]]]:
        """Append every captured row, in capture order, with its position.

        Returns each stored row PAIRED with the register row it came from, so
        the reading step never has to re-establish that pairing by index.

        Rows travel ONLY in bounded batches (`CATALOG_WRITE_BATCH_SIZE`): one
        lease-guarded, set-based, all-or-nothing call per batch that applies
        the single-row rules -- never the single-row write. Each stored row comes back
        LEAN (id, snapshot, record key, upstream id, payload digest), which is
        all the reading step needs to bind a candidate to it.
        """
        stored: list[tuple[dict[str, Any], Mapping[str, Any]]] = []
        pairs = list(snapshot_module.raw_record_payloads(capture, snapshot))
        for ordinal, chunk in enumerate(_chunks(pairs), start=1):
            self._check_cancelled()
            with self._phase("raw", batch=True):
                answer = self._repository.record_catalog_raw_records(
                    self._lease.run_id, [payload for payload, _ in chunk], **self._lease_kwargs,
                    diagnostics=self._diagnostics("raw", snapshot, batch=ordinal))
            self._count_batch("raw", answer)
            stored.extend(zip(answer["rows"], (record for _, record in chunk)))
        self._emit("catalog_records_written", {"snapshot_key": snapshot["snapshot_key"],
                                               "count": len(stored)})
        return stored

    def _write_candidates(self, normalization: CaptureNormalization,
                          records: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]],
                          snapshot: Mapping[str, Any] | None = None
                          ) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
        """Write the readings this capture already produced, in capture order.

        The readings were computed before the snapshot was opened, so nothing
        is decided here -- this walks the positional entries and writes the ones
        that exist. A row that could not be read is NOT lost: it is durable as a
        raw record, and its reason is already in the snapshot's own metadata, so
        it survives this ingestion rather than living only in this report.

        A RAW-ONLY resource produces no entries at all and therefore no
        candidates, which is its stated contract rather than 233 failures.
        """
        if normalization.contract == RAW_ONLY_CONTRACT:
            self._emit("catalog_candidates_skipped", {"contract": RAW_ONLY_CONTRACT,
                                                      "records": len(records)})
            return [], []
        candidates: list[dict[str, Any]] = []
        payloads = [reading.candidate_payload(record_row)
                    for (record_row, _raw), reading in zip(records, normalization.entries)
                    if reading is not None]
        # ONLY the batch write, which asks the snapshot write authority. The
        # single-row candidate write does not, and ingestion never calls it
        # (tests/test_catalog_ingestion_recovery.py holds that statically).
        for ordinal, chunk in enumerate(_chunks(payloads), start=1):
            self._check_cancelled()
            with self._phase("candidates", batch=True):
                answer = self._repository.record_catalog_candidates(
                    self._lease.run_id, chunk, **self._lease_kwargs,
                    diagnostics=self._diagnostics("candidates", snapshot, batch=ordinal))
            self._count_batch("candidates", answer)
            candidates.extend(answer["rows"])
        self._emit("catalog_candidates_written", {"count": len(candidates),
                                                  "rejected": normalization.issue_count})
        return candidates, list(normalization.issues)

    def _activate(self, snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
        """Decide the snapshot, and record a failure AS a failure.

        The completeness gate lives in the database. If it REFUSES, this
        capture did not establish what it claimed, so the snapshot is marked
        `failed` -- terminal, and never activatable afterwards -- rather than
        left pending for a later attempt to finish with different material.

        Only a refusal does that. A lost lease, a transient failure (after the
        repository's bounded retry) or an unclassified one says nothing about
        the capture, and `failed` cannot be undone: that content could then
        never be activated again under its content-derived key. So those
        propagate and leave the snapshot PENDING, which no reader reads and
        which a later operator capture run can adopt and finish.
        """
        self._check_cancelled()
        try:
            with self._phase("activate", request={"snapshot_id": snapshot["id"]}):
                decided = self._repository.activate_catalog_snapshot(
                    self._lease.run_id, {"snapshot_id": snapshot["id"]}, **self._lease_kwargs,
                    diagnostics=self._diagnostics("activate", snapshot))
        except AppError as failure:
            if not _is_refusal(failure):
                raise
            with self._phase("activate", request={"snapshot_id": snapshot["id"]}):
                self._repository.activate_catalog_snapshot(
                    self._lease.run_id, {"snapshot_id": snapshot["id"],
                                         "validation_state": "failed"},
                    **self._lease_kwargs, diagnostics=self._diagnostics("activate", snapshot))
            raise GovernmentIngestionError("GOV_SNAPSHOT_NOT_ACTIVATED") from None
        self._emit("catalog_snapshot_activated", {"snapshot_key": decided["snapshot_key"],
                                                  "records": decided["stored_record_count"]})
        return decided

    @staticmethod
    def _check_scope(snapshot: Mapping[str, Any], requested: CaptureScope | None) -> None:
        """The stored declaration must be the requested one, or nothing lands."""
        try:
            stored = declared_scope(snapshot)
        except CaptureScopeError:
            raise GovernmentIngestionError("GOV_SNAPSHOT_SCOPE_MISMATCH") from None
        if (stored.key() if stored else None) != (requested.key() if requested else None):
            raise GovernmentIngestionError("GOV_SNAPSHOT_SCOPE_MISMATCH")

    @staticmethod
    def _check_normalization(snapshot: Mapping[str, Any],
                             normalization: CaptureNormalization) -> None:
        """A replay or a reuse must RECONSTRUCT the stored gap, not assume it.

        The summary is a function of the captured content, so the same content
        read under the same contract reproduces it exactly. If it does not, the
        stored snapshot was read under different rules -- a vocabulary entry
        changed, or another release wrote it -- and reusing it as though this
        code had read it would be the drift the summary exists to prevent. So
        it fails closed instead, and the stored snapshot is left untouched.
        """
        stored = snapshot.get("retrieval_metadata") or {}
        fresh = normalization.durable_summary()
        if any(stored.get(field) != value for field, value in fresh.items()):
            raise GovernmentIngestionError("GOV_SNAPSHOT_NORMALIZATION_DRIFT")

    # --- reporting -----------------------------------------------------------

    def _report(self, capture: ResourceCapture, snapshot: Mapping[str, Any], *,
                candidates: Sequence[Mapping[str, Any]], reused: bool,
                rejected: Sequence[tuple[str, str]] = (),
                adopted_from: str = "") -> IngestionReport:
        statuses: dict[str, int] = {}
        for candidate in candidates:
            status = str(candidate.get("status"))
            statuses[status] = statuses.get(status, 0) + 1
        # Read from the SNAPSHOT, so a replay and a reuse report exactly what
        # the ingestion that wrote it recorded.
        metadata = snapshot.get("retrieval_metadata") or {}
        return IngestionReport(
            snapshot_id=str(snapshot["id"]), snapshot_key=str(snapshot["snapshot_key"]),
            content_sha256=str(snapshot["content_sha256"]),
            resource_id=str(snapshot["resource_id"]),
            upstream_version=str(snapshot["upstream_version"]),
            upstream_version_kind=str(snapshot["upstream_version_kind"]),
            schema_fingerprint=capture.schema_fingerprint,
            declared_record_count=int(snapshot["declared_record_count"]),
            stored_record_count=int(snapshot["stored_record_count"]),
            page_count=len(capture.pages), candidate_count=len(candidates),
            candidate_status_counts=statuses,
            rejected_records=tuple(rejected[:MAX_REPORTED_REJECTIONS]),
            rejected_record_count=len(rejected),
            normalization_contract=str(metadata.get("normalization_contract") or ""),
            normalized_record_count=int(metadata.get("normalized_record_count") or 0),
            normalization_issue_count=int(metadata.get("normalization_issue_count") or 0),
            normalization_issues={str(entry["reason"]): int(entry["count"])
                                  for entry in metadata.get("normalization_issues") or []},
            normalization_issue_records=tuple(
                str(record) for record in metadata.get("normalization_issue_records") or ()),
            activated=snapshot.get("activated_at") is not None,
            reused_existing=reused, created_by_run_id=str(snapshot.get("created_by_run_id")),
            capture_scope_key=_declared_key(snapshot), adopted_from_run_id=adopted_from,
            adoption_seq=int(self._metrics.get("adoption_seq") or 0),
            ingestion=_frozen_metrics(self._metrics),
            candidates=tuple(dict(candidate) for candidate in candidates))

    # --- measurement ---------------------------------------------------------

    def _diagnostics(self, phase: str, snapshot: Mapping[str, Any] | None = None, *,
                     batch: int | None = None) -> CatalogWriteDiagnostics:
        """WHERE a write happens, for the repository's server-log line if it
        fails: this run, the snapshot, the phase and the batch ordinal.
        Identifiers and whole numbers only (`CatalogWriteDiagnostics`); never
        a payload, and never part of a report."""
        return CatalogWriteDiagnostics(run_id=self._lease.run_id, phase=phase,
                                       snapshot_id=(snapshot or {}).get("id"), batch=batch)

    def _phase(self, name: str, *, request: Any = None, batch: bool = False) -> "_Phase":
        return _Phase(self._metrics[name], request=request, batch=batch)

    def _count_batch(self, name: str, answer: Mapping[str, Any]) -> None:
        """Fold what ONE batch write cost -- its actual RPC attempts (a split or
        a retry is more than one), their request bytes and the slowest of them
        -- and what it wrote, into the phase."""
        phase = self._metrics[name]
        phase["calls"] += _whole(answer.get("rpc_calls"))
        phase["request_bytes"] += _whole(answer.get("request_bytes"))
        slowest = answer.get("max_call_seconds")
        if isinstance(slowest, (int, float)) and not isinstance(slowest, bool):
            phase["max_call_seconds"] = max(phase["max_call_seconds"], float(slowest))
        phase["inserted"] += _whole(answer.get("inserted"))
        phase["already_present"] += _whole(answer.get("already_present"))

    def _emit(self, event_type: str, payload: Mapping[str, Any]) -> None:
        if self._event_sink is not None:
            self._event_sink(event_type, dict(payload))

    def _check_cancelled(self) -> None:
        if self._cancellation_checker is not None and self._cancellation_checker():
            raise CancellationRequested("RUN_CANCELLED")


#: The phases an ingestion is measured in, in order.
INGESTION_PHASES = ("snapshot", "raw", "candidates", "activate")


def new_metrics() -> dict[str, Any]:
    """Zeroed per-phase measurements: database `calls` actually sent,
    `seconds` of wall time, `request_bytes` sent (the JSON-encoded request
    bodies) and `max_call_seconds`, the slowest single call -- the number the
    8 s statement timeout is measured against. The row phases also count rows
    `inserted` and `already_present`."""
    metrics: dict[str, Any] = {name: {"calls": 0, "seconds": 0.0, "request_bytes": 0,
                                      "max_call_seconds": 0.0} for name in INGESTION_PHASES}
    for name in ("raw", "candidates"):
        metrics[name].update({"inserted": 0, "already_present": 0})
    metrics.update({"adoption_seq": 0, "previous_writer_run_id": ""})
    return metrics


def _whole(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _frozen_metrics(metrics: Mapping[str, Any]) -> dict[str, Any]:
    frozen = {name: {**metrics[name], "seconds": round(float(metrics[name]["seconds"]), 3),
                     "max_call_seconds": round(float(metrics[name]["max_call_seconds"]), 3)}
              for name in INGESTION_PHASES}
    frozen["adoption_seq"] = int(metrics.get("adoption_seq") or 0)
    frozen["previous_writer_run_id"] = str(metrics.get("previous_writer_run_id") or "")
    frozen["total_calls"] = sum(int(frozen[name]["calls"]) for name in INGESTION_PHASES)
    frozen["total_seconds"] = round(sum(float(frozen[name]["seconds"])
                                        for name in INGESTION_PHASES), 3)
    frozen["total_request_bytes"] = sum(int(frozen[name]["request_bytes"])
                                        for name in INGESTION_PHASES)
    frozen["max_call_seconds"] = max(float(frozen[name]["max_call_seconds"])
                                     for name in INGESTION_PHASES)
    return frozen


class _Phase:
    """Measures one step of a phase, even on failure.

    A single-row step (`request` given) is ONE call: it counts it, its wall
    time, its JSON request size and whether it was the slowest. A batch step
    only adds wall time here; the repository reports its actual calls, bytes
    and slowest call, folded in by `_count_batch`.
    """

    def __init__(self, phase: dict[str, Any], *, request: Any = None, batch: bool = False) -> None:
        self._phase = phase
        self._batch = batch
        self._bytes = 0 if batch else len(json.dumps(request, default=str).encode("utf-8"))

    def __enter__(self) -> "_Phase":
        self._started = time.monotonic()
        return self

    def __exit__(self, *_exc: Any) -> None:
        elapsed = time.monotonic() - self._started
        self._phase["seconds"] += elapsed
        if not self._batch:
            self._phase["calls"] += 1
            self._phase["request_bytes"] += self._bytes
            self._phase["max_call_seconds"] = max(self._phase["max_call_seconds"], elapsed)


def _chunks(items: Sequence[Any]):
    """`items` in consecutive, order-preserving slices of at most
    `CATALOG_WRITE_BATCH_SIZE` (read at call time)."""
    size = CATALOG_WRITE_BATCH_SIZE
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _is_refusal(failure: AppError) -> bool:
    """Whether the database REFUSED an activation, as opposed to failing it.

    A classified repository failure is a refusal only when it is `rejected`
    (SQLSTATE 22/23, which is how the completeness gate refuses). A lost lease
    is never one. Any other `AppError` is a repository's own static refusal
    (the in-memory repository's `CATALOG_SNAPSHOT_INCOMPLETE`, for instance).
    """
    if isinstance(failure, RepositoryFailure):
        return failure.failure_class == "rejected"
    return failure.code not in LEASE_FAILURE_CODES and failure.code != "REPOSITORY_ERROR"


def _declared_key(snapshot: Mapping[str, Any]) -> str:
    """The declared scope key of a snapshot `_check_scope` already accepted."""
    scope = declared_scope(snapshot)
    return scope.key() if scope is not None else ""


__all__ = ["GOVERNMENT_INGESTION_REASONS", "INGESTION_PHASES", "MAX_REPORTED_REJECTIONS",
           "GovernmentCatalogIngestor", "GovernmentIngestionError", "IngestionReport",
           "new_metrics",
           "GovernmentSourceError"]

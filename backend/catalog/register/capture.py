"""The capture job's side of register capture (PR-D1, D1-3/D1-4/D1-8).

`capture_group` captures every unit of ONE request group, under the operator
capture run's lease, each unit into its OWN snapshot (a tozar is never split
across snapshots):

1. the scoped capture of the exact tozar (`CaptureScope.for_register_marque`),
   through the same client, bounds and completeness gates as Prepare -- so a
   tozar Prepare already captured with the same content IS the same snapshot;
2. every row written in bounded batches (the ingestor's own batch writes);
3. BEFORE ACTIVATION (`GovernmentCatalogIngestor(before_activation=...)`):
   * count verification: the rows stored for the snapshot must equal a
     FRESH, independent count of that exact tozar taken at the end of the
     capture (the directory's bounded ``limit=0`` request, not the capture's
     own reported total), else the snapshot stays inactive with
     ``CATALOG_CAPTURE_COUNT_MISMATCH`` (never used by Prepare);
   * the archive: the object is written create-only and recorded in the
     database, else the snapshot stays inactive with
     ``CATALOG_ARCHIVE_WRITE_FAILED``. No archive, no activation.
4. activation (the database's own completeness gate), then the unit's
   outcome with the snapshot's MEASURED bytes (computed in the database);
5. PR-L1: the captured snapshot's catalog variants, built in bounded batches
   (`variants.build_after_capture`; reported, never failing the unit);
6. PR-L2: once built (and archived, step 3), its raw payloads and its
   candidates' identity are removed from the database, then the tozar's
   superseded snapshots keep only their referenced rows
   (`compaction.compact_after_build`; reported, never failing the unit).

A snapshot that was already active (captured earlier, e.g. by Prepare) is
reused as is: its archive is written if missing and its count verified. A
count mismatch there fails the unit (never `captured`) but cannot deactivate
a snapshot another run activated; `unverified_snapshots` in REGISTER_COVERAGE
counts it.
A lost lease or a cancellation ends the whole group; any other failure ends
only its unit, recorded with a static code.

`refresh_directory` records a new directory version (only on change).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from backend.catalog.government import source as src
from backend.catalog.government.capture_scope import (CAPTURE_SCOPE_REASONS, CaptureScope,
                                                      CaptureScopeError)
from backend.catalog.government.client import DataGovClient, ResourceCapture
from backend.catalog.government.directory import RegisterDirectory, count_tozar, discover_directory
from backend.catalog.government.ingest import (GOVERNMENT_INGESTION_REASONS,
                                               GovernmentCatalogIngestor, GovernmentIngestionError)
from backend.catalog.government.source import GOVERNMENT_SOURCE_REASONS, GovernmentSourceError
from backend.catalog.register import archive as archive_module
from backend.catalog.register.compaction import compact_after_build
from backend.catalog.register.variants import build_after_capture
from backend.errors import AppError, LEASE_FAILURE_CODES
from backend.runtime import CancellationRequested

#: This module's own unit failure codes (static, code-owned).
REGISTER_CAPTURE_REASONS: Mapping[str, str] = {
    "CATALOG_CAPTURE_COUNT_MISMATCH":
        "the rows stored for the snapshot are not the source's total for that tozar",
    "CATALOG_ARCHIVE_WRITE_FAILED":
        "the snapshot's archive could not be written or verified",
    "CATALOG_ARCHIVE_NOT_CONFIGURED":
        "no archive bucket is configured for register capture",
    "CATALOG_REGISTER_UNIT_NOT_THIS_RUN":
        "that register capture group is not captured by this run",
    "CATALOG_REGISTER_CAPTURE_FAILED":
        "the unit could not be captured",
    "CATALOG_REGISTER_NO_DIRECTORY":
        "the register directory has not been read yet",
}


class RegisterCaptureError(Exception):
    def __init__(self, reason_code: str) -> None:
        if reason_code not in REGISTER_CAPTURE_REASONS:
            raise ValueError("register capture reason must come from the static allowlist")
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass
class UnitOutcome:
    tozar: str
    status: str
    snapshot_key: str = ""
    api_total: int | None = None
    captured_rows: int | None = None
    failure_code: str = ""
    #: The numeric HTTP status of a ``GOV_HTTP_STATUS_UNEXPECTED`` failure.
    http_status: int | None = None
    #: PR-L1: the snapshot's variant build after capture (never fails the unit).
    variants: dict[str, Any] | None = None
    #: PR-L2: its payload compaction after a complete build (never fails the unit).
    compaction: dict[str, Any] | None = None

    def as_document(self) -> dict[str, Any]:
        return {"tozar": self.tozar, "status": self.status, "snapshot_key": self.snapshot_key,
                "api_total": self.api_total, "captured_rows": self.captured_rows,
                "failure_code": self.failure_code, "variants": self.variants,
                "compaction": self.compaction,
                **({"http_status": self.http_status} if self.http_status is not None else {})}


@dataclass
class GroupReport:
    group_id: str
    units: list[UnitOutcome] = field(default_factory=list)

    def as_document(self) -> dict[str, Any]:
        return {"group_id": self.group_id, "units": [unit.as_document() for unit in self.units],
                "captured": sum(unit.status == "captured" for unit in self.units),
                "failed": sum(unit.status == "failed" for unit in self.units)}


def _lease_kwargs(lease: Any) -> dict[str, Any]:
    return {"worker_id": lease.worker_id, "attempt": lease.attempt, "lease_token": lease.lease_token}


def _unit_code(failure: BaseException) -> str:
    """One static code for a unit's failure; never exception text."""
    if isinstance(failure, RegisterCaptureError):
        return failure.reason_code
    if isinstance(failure, GovernmentSourceError) and failure.reason_code in GOVERNMENT_SOURCE_REASONS:
        return failure.reason_code
    if isinstance(failure, GovernmentIngestionError) and failure.reason_code in GOVERNMENT_INGESTION_REASONS:
        return failure.reason_code
    if isinstance(failure, CaptureScopeError) and failure.reason_code in CAPTURE_SCOPE_REASONS:
        return failure.reason_code
    if isinstance(failure, AppError) and failure.code.startswith("CATALOG_") \
            and failure.code.replace("_", "").isalnum() and failure.code.isupper():
        return failure.code
    return "CATALOG_REGISTER_CAPTURE_FAILED"


def _is_fatal(failure: BaseException) -> bool:
    """A failure that ends the whole group (the run no longer owns the work)."""
    return isinstance(failure, AppError) and failure.code in LEASE_FAILURE_CODES


class _Archiver:
    """Write (create-only) and record one snapshot's archive; idempotent."""

    def __init__(self, repository: Any, lease: Any, writer: Any, resource_id: str) -> None:
        self._repository = repository
        self._lease = lease
        self._writer = writer
        self._resource_id = resource_id
        self._built: dict[int, archive_module.ArchiveObject] = {}

    def ensure(self, tozar: str, capture: ResourceCapture, snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
        snapshot_id = str(snapshot["id"])
        recorded = self._repository.register_snapshot_archive(snapshot_id)
        if recorded is not None:
            # The snapshot is content-addressed and its archive was verified
            # when it was recorded: the record stands. (Rebuilding and
            # comparing the gzip bytes would tie it to one zlib build.)
            return recorded
        obj = self._built.get(id(capture))
        if obj is None:
            obj = archive_module.build(record for _locator, record in capture.located_records())
            self._built[id(capture)] = obj
        if self._writer is None:
            raise RegisterCaptureError("CATALOG_ARCHIVE_NOT_CONFIGURED")
        name = archive_module.object_name(self._resource_id, tozar, str(snapshot["snapshot_key"]))
        try:
            outcome = self._writer.put(name, obj)
        except archive_module.ArchiveWriteError:
            raise RegisterCaptureError("CATALOG_ARCHIVE_WRITE_FAILED") from None
        if outcome not in (archive_module.CREATED, archive_module.EXISTS_VERIFIED):
            # The object exists and this identity cannot read it, and the
            # database holds no record of it: it cannot be shown to be ours.
            raise RegisterCaptureError("CATALOG_ARCHIVE_WRITE_FAILED")
        try:
            return self._repository.record_register_snapshot_archive(
                self._lease.run_id, snapshot_id, archive_module.gcs_uri(self._writer.bucket, name),
                obj.byte_size, obj.sha256, obj.line_count, **_lease_kwargs(self._lease))
        except AppError as refused:
            if refused.code in LEASE_FAILURE_CODES:
                raise
            if refused.code == "CATALOG_CAPTURE_COUNT_MISMATCH":
                raise RegisterCaptureError("CATALOG_CAPTURE_COUNT_MISMATCH") from None
            raise RegisterCaptureError("CATALOG_ARCHIVE_WRITE_FAILED") from None


def _verify_count(repository: Any, snapshot_id: str, api_total: int) -> int:
    """The stored rows of the snapshot against the fresh source count."""
    stored = int(repository.count_catalog_raw_records(snapshot_id))
    if stored != int(api_total):
        raise RegisterCaptureError("CATALOG_CAPTURE_COUNT_MISMATCH")
    return stored


def capture_unit(repository: Any, lease: Any, *, client: DataGovClient, unit: Mapping[str, Any],
                 archive_writer: Any, resource_id: str = src.WLTP_RESOURCE_ID,
                 package_id: str = src.CKAN_PACKAGE_ID,
                 cancellation_checker: Callable[[], bool] | None = None,
                 event_sink: Callable[[str, Mapping[str, Any]], None] | None = None) -> UnitOutcome:
    tozar = str(unit["tozar"])
    unit_id = str(unit["id"])
    lease_kwargs = _lease_kwargs(lease)

    def record(status: str, *, code: str | None = None, snapshot_id: str | None = None,
               api_total: int | None = None, captured: int | None = None,
               verified: bool | None = None) -> None:
        repository.record_register_unit_status(
            lease.run_id, unit_id, status, code, snapshot_id, api_total, captured, verified,
            **lease_kwargs)

    record("capturing")
    outcome = UnitOutcome(tozar=tozar, status="failed")
    seen: dict[str, Any] = {}
    archiver = _Archiver(repository, lease, archive_writer, resource_id)
    try:
        scope = CaptureScope.for_register_marque(tozar)
        capture = client.capture_resource(src.require_allowed_resource(resource_id),
                                          package_id=src.require_allowed_package(package_id),
                                          query=scope.query())

        def fresh_total() -> int:
            # Taken once, after every row is written: an independent count
            # of this exact tozar, never the capture's own reported total.
            if outcome.api_total is None:
                outcome.api_total = count_tozar(client, tozar, resource_id=resource_id)
            return outcome.api_total

        def before_activation(taken: ResourceCapture, snapshot: Mapping[str, Any]) -> None:
            seen["snapshot"] = dict(snapshot)
            outcome.captured_rows = _verify_count(repository, str(snapshot["id"]), fresh_total())
            archiver.ensure(tozar, taken, snapshot)

        ingestor = GovernmentCatalogIngestor(repository, lease, client=client,
                                             cancellation_checker=cancellation_checker,
                                             event_sink=event_sink, before_activation=before_activation)
        report = ingestor.ingest_capture(capture, capture_scope=scope)
        snapshot = {"id": report.snapshot_id, "snapshot_key": report.snapshot_key}
        seen["snapshot"] = snapshot
        # A reused or replayed ACTIVE snapshot skipped the hook: the same two
        # checks, now (its archive is written if it is missing).
        outcome.captured_rows = _verify_count(repository, report.snapshot_id, fresh_total())
        archiver.ensure(tozar, capture, snapshot)
        record("captured", snapshot_id=report.snapshot_id, api_total=outcome.api_total,
               captured=outcome.captured_rows, verified=True)
        outcome.status, outcome.snapshot_key = "captured", report.snapshot_key
        # PR-L1: the captured snapshot's variants, in bounded batches. A
        # failed build is reported and leaves the unit captured (the operator
        # backfill builds it again); an already built snapshot is a no-op.
        outcome.variants = build_after_capture(repository, report.snapshot_id)
        # PR-L2: built and archived, the payloads leave the database (a
        # refusal or a failure is reported and changes nothing).
        outcome.compaction = compact_after_build(repository, report.snapshot_key, outcome.variants,
                                                 writer=archive_writer, snapshot_id=str(report.snapshot_id),
                                                 run_id=lease.run_id)
        return outcome
    except Exception as failure:  # noqa: BLE001 - reduced to a static code
        if _is_fatal(failure) or isinstance(failure, CancellationRequested):
            raise
        outcome.failure_code = _unit_code(failure)
        if isinstance(failure, GovernmentSourceError):
            outcome.http_status = failure.http_status
        snapshot = seen.get("snapshot") or {}
        outcome.snapshot_key = str(snapshot.get("snapshot_key") or "")
        if outcome.failure_code == "CATALOG_CAPTURE_COUNT_MISMATCH":
            verified: bool | None = False
        else:
            # The count passed (captured_rows is set only then), or was never checked.
            verified = True if outcome.captured_rows is not None else None
        record("failed", code=outcome.failure_code,
               snapshot_id=str(snapshot["id"]) if snapshot.get("id") else None,
               api_total=outcome.api_total, captured=outcome.captured_rows, verified=verified)
        return outcome


def capture_group(repository: Any, lease: Any, *, client: DataGovClient, group_id: str,
                  archive_writer: Any, resource_id: str = src.WLTP_RESOURCE_ID,
                  cancellation_checker: Callable[[], bool] | None = None,
                  event_sink: Callable[[str, Mapping[str, Any]], None] | None = None,
                  stop_before_unit: Callable[[Mapping[str, Any]], bool] | None = None) -> GroupReport:
    """`stop_before_unit` (PR-SYNC-1, a sync's budget and throttle stop): when
    it answers True for the next unit, that unit and the rest are left as they
    are -- `requested` under this run, retryable once the run has ended."""
    answer = repository.register_capture_group(group_id)
    group = answer.get("group") or {}
    if str(group.get("run_id")) != str(lease.run_id) or group.get("kind", "capture") != "capture":
        raise RegisterCaptureError("CATALOG_REGISTER_UNIT_NOT_THIS_RUN")
    report = GroupReport(group_id=str(group_id))
    for unit in answer.get("units") or []:
        if unit.get("status") not in ("requested", "capturing"):
            continue
        if stop_before_unit is not None and stop_before_unit(unit):
            break
        report.units.append(capture_unit(
            repository, lease, client=client, unit=unit, archive_writer=archive_writer,
            resource_id=resource_id, cancellation_checker=cancellation_checker,
            event_sink=event_sink))
    return report


def refresh_directory(repository: Any, *, client: DataGovClient,
                      resource_id: str = src.WLTP_RESOURCE_ID,
                      env: Mapping[str, str] | None = None) -> dict[str, Any]:
    directory: RegisterDirectory = discover_directory(client, resource_id=resource_id, env=env)
    answer = repository.record_register_directory(
        directory.resource_id, directory.fetched_at.isoformat(), directory.rpc_units())
    version = answer.get("version") or {}
    return {"decision": answer.get("decision"), "register_version": version.get("register_version"),
            "unit_count": len(directory.units), "total_rows": directory.total_rows,
            "requests": directory.requests, "unfilterable_values": directory.unfilterable_values,
            "distinct_total": directory.distinct_total}


__all__ = ["GroupReport", "REGISTER_CAPTURE_REASONS", "RegisterCaptureError", "UnitOutcome",
           "capture_group", "capture_unit", "refresh_directory"]

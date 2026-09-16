"""CODE-1: the one guarded, operator-invoked Government capture entrypoint.

Why this exists
---------------

Every component of a live Government capture already exists and is tested --
`HttpsDataGovTransport`, `DataGovClient`, `GovernmentCatalogIngestor`,
`GovernmentCatalogRefresh`, the lease-guarded catalog RPCs -- and **nothing
constructs them outside tests**. An operator holding full credentials and an
explicit authorization had no supported way to produce a snapshot from this
repository (Gap Audit S3-02a, `MISSING`).

This module is that way, and nothing more. It is ORCHESTRATION: it connects
the reviewed components in the reviewed order. It does not re-implement, wrap
or relax the capture, the pagination arithmetic, normalization, the snapshot
identity, activation, the diff, provenance or the lease contract -- every one
of those stays exactly where it already lives and is exercised unmodified.

Refusal is the default, and it is structural
--------------------------------------------

Importing this module, asking it for `--help`, running it with no arguments or
asking it to plan opens no socket, constructs no transport, connects to no
database, claims no run, acquires no lease, writes no file and mutates nothing.
That is not a policy the code checks late: the repository and the transport are
CONSTRUCTED by `_open_repository()` and `_open_transport()`, and both are
called only after every prerequisite in `_refusal()` has passed.

The nine prerequisites, all required together, in this order:

1.  ``--execute``. Absent -- including under ``--plan`` -- is a refusal.
2.  ``--acknowledge-live-government-egress`` matching `EGRESS_ACKNOWLEDGEMENT`
    exactly. Not a boolean flag: a boolean is something a shell history, an
    alias or a copied command supplies by accident.
3.  ``--acknowledge-schema-report-reviewed`` matching
    `SCHEMA_REPORT_ACKNOWLEDGEMENT` exactly. Separate from (2) because they
    are separate facts: one is "this will reach `data.gov.il`", the other is
    "OPERATOR-0's read-only schema report was completed and read". An enabled
    catalog over an unverified schema is the posture the flag exists to
    prevent, so it is asserted here too.
4.  ``--project-ref`` equal to the project reference this process is actually
    configured for, derived from `SUPABASE_URL`. A capture aimed at a project
    the operator did not name is refused before anything is opened, and
    neither value is ever printed.
5.  ``--run-id``, because every durable catalog write in this repository is
    lease-guarded and a lease belongs to a run. See "the lease boundary".
6.  `MILO_ENABLE_CATALOG_EXECUTION` explicitly enabled, through CODE-2's
    `catalog_execution_enabled()` -- the same flag, the same true-value
    parser, no second overlapping switch.
7.  `MILO_ENABLE_PAID_EXECUTION` disabled. A capture needs no model spend and
    must not be bundled with one.
8.  ``--package-id`` and ``--resource-id`` equal to the pinned WLTP package and
    resource. There is no URL, hostname, action, query, filter or page-key
    argument anywhere in this entrypoint.
9.  ``--page-limit`` equal to `CAPTURE_PAGE_LIMIT`, stated explicitly by the
    operator rather than defaulted, plus `--max-pages` / `--max-records` which
    may only restate the reviewed bounds.

A missing, malformed, contradictory or unrecognised value is a refusal with a
static reason code and a non-zero exit, before any database or network access.

The page size, and why it is 1 000
-----------------------------------

The WLTP resource is expected to hold roughly 101 000 rows.
`MAX_PAGES_PER_CAPTURE` is 200 and `DEFAULT_PAGE_LIMIT` is 100, so a default
capture computes `ceil(101000/100) = 1010` pages and fails closed with
`GOV_PAGE_BUDGET_EXCEEDED` before the first page is even read. At
`CAPTURE_PAGE_LIMIT = 1000` -- which is `MAX_PAGE_LIMIT`, not an increase of it
-- the same arithmetic gives `ceil(101000/1000) = 101` pages, inside the
existing ceiling. Nothing else moves: the page bound, the record bound, the
response-byte bound, the per-record bound, the exact-total and pagination
checks, the schema fingerprint check, the duplicate `_id` check and
activation-after-complete-persistence are all untouched. A live page that
exceeds `MAX_RESPONSE_BYTES` fails closed, and this module does not raise that
limit to make a future capture succeed.

The lease boundary, and the call-graph blocker
-----------------------------------------------

Catalog writes are lease-guarded, so this controller needs an AUTHENTIC lease:
it calls the existing `claim_run` (the single-statement CAS in migration 012),
builds `WorkerLease` from what the database returned, and heartbeats through
the existing guarded RPC. It invents no lease, holds no direct insert, and
never passes or prints lease material.

That leaves one real problem, and it is worth stating exactly rather than
working around. **There is no way in this repository to create a run that is
not an ordinary model run.** Every creation route reaches
`backend.main._create_and_launch_run`, which creates the run and immediately
hands it to `JobLauncher.launch()`; the launched worker resolves an engine from
`project.workflow_key` and runs a model. A controller that claimed any run the
operator named would therefore race that worker for the same lease and finalize
somebody's chat run with a catalog output.

The seam that closes it needs no migration and no new repository method,
because the run row already carries a server-owned discriminator. Both creation
paths insert `launch_state = 'pending'`, and `try_acquire_launch` moves it to
`'launching'` before the HTTP response returns. So a run that is simultaneously
`status = 'queued'`, `launch_state = 'pending'` and carries
`input.metadata.milo_operation = OPERATOR_CAPTURE_OPERATION` is one an operator
prepared deliberately and no launcher has ever touched. All three are checked
in `_run_is_eligible`, server-side, BEFORE the claim -- so a mistyped run id
refuses rather than hijacking a run. The marker alone would not be enough (a
browser request's metadata reaches `input.metadata`); the launch state alone
would not be enough (there is a sub-request window where it is still pending);
together they are.

Stopping safely
---------------

The heartbeat is also the watch. `heartbeat_run_guarded` returns the run row,
so one call both extends the lease and observes cancellation, with no
per-record database read. `_CaptureSupervisor.should_stop` is the cancellation
checker the ingestor already accepts, so a cancelled or lost-lease capture
raises `CancellationRequested` out of the existing path rather than needing new
control flow. A stale process cannot continue: every durable write and the
activation itself re-check the lease atomically in the database, so the writes
simply stop being accepted. Activation is the last step and is gated on
complete persistence, so an interruption anywhere leaves a non-active snapshot,
which no reader reads, and the previous usable snapshot keeps answering.

What this is NOT
----------------

No schedule, cron, timer, background loop or automatic refresh. No HTTP route,
no browser surface, no model-callable tool, no Commander, no Swarm worker, no
provider credential, no paid execution and no canonical promotion. Capturing
and activating a snapshot is not authorization to run MILO against it or to
promote canonical facts. Disabling `MILO_ENABLE_CATALOG_EXECUTION` prevents
another capture from starting and deletes or deactivates nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from backend.catalog.execution import CATALOG_EXECUTION_FLAG, catalog_execution_enabled
from backend.catalog.government import source as src
from backend.catalog.government.client import DataGovClient
from backend.catalog.government.ingest import (GOVERNMENT_INGESTION_REASONS,
                                               GovernmentIngestionError)
from backend.catalog.government.refresh import GovernmentCatalogRefresh, RefreshOutcome
from backend.catalog.government.source import (GOVERNMENT_SOURCE_REASONS,
                                               GovernmentSourceError)
from backend.engines.swarm_v2.evidence import WorkerLease
from backend.errors import AppError
from backend.production_config import TRUE_VALUES
from backend.runtime import TERMINAL_STATES, CancellationRequested

#: How this entrypoint names itself in its own report and in documentation.
CAPTURE_ENTRYPOINT = "catalog.government.capture"

#: The paid-execution switch, which must be OFF. Spelled here and parsed with
#: the same `TRUE_VALUES` set `backend.budget.paid_execution_enabled` uses, so
#: "an operator turned this on" has one spelling in the repository; a test
#: holds the two readings to the same answer for every value.
PAID_EXECUTION_FLAG = "MILO_ENABLE_PAID_EXECUTION"

#: The two acknowledgements, matched EXACTLY. Separate facts, separate values,
#: and neither is a boolean: a bare flag is what a shell alias or a copied
#: command supplies without anybody deciding anything.
EGRESS_ACKNOWLEDGEMENT = "I ACKNOWLEDGE LIVE GOVERNMENT EGRESS"
SCHEMA_REPORT_ACKNOWLEDGEMENT = "I ACKNOWLEDGE OPERATOR-0 SCHEMA REPORT REVIEWED"

#: What the operator must have put in the prepared run's input metadata, and
#: the two server-owned run fields that prove no launcher ever touched it.
OPERATOR_CAPTURE_OPERATION = "catalog.government.capture"
ELIGIBLE_RUN_STATUS = "queued"
ELIGIBLE_LAUNCH_STATE = "pending"

#: The reviewed capture bounds. `CAPTURE_PAGE_LIMIT` is `MAX_PAGE_LIMIT`, not
#: an increase of it; the other two RESTATE the existing ceilings so the
#: command line can only ever confirm them, never widen them.
CAPTURE_PAGE_LIMIT = src.MAX_PAGE_LIMIT
CAPTURE_MAX_PAGES = src.MAX_PAGES_PER_CAPTURE
CAPTURE_MAX_RECORDS = src.MAX_RECORDS_PER_CAPTURE

#: The report is bounded field by field: every string is truncated, every list
#: is clipped. The schema below is closed, so this is a second bound rather
#: than the only one.
MAX_REPORT_TEXT_CHARS = 128
MAX_REPORT_ISSUE_RECORDS = 100
MAX_REPORT_ISSUE_REASONS = 50
MAX_REPORT_STATUS_COUNTS = 20

#: Lease sizing. Deliberately the WORKER's variables rather than new ones: the
#: lease model, the guarded RPC and the expiry rules are the worker's, so a
#: second spelling of "how long is a lease" would be a second answer.
LEASE_SECONDS_VAR = "MILO_WORKER_LEASE_SECONDS"
HEARTBEAT_INTERVAL_VAR = "MILO_WORKER_HEARTBEAT_INTERVAL_SECONDS"
DEFAULT_LEASE_SECONDS = 300
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 30.0

#: Exit statuses. Both failure statuses are non-zero and they are distinct, so
#: a wrapper can tell "nothing happened" from "something started and stopped".
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REFUSED = 2

#: This entrypoint's own refusals. Each names the PROPERTY that failed and
#: carries no value, no path, no URL, no identifier and no exception text. The
#: capture and ingestion vocabularies (`GOVERNMENT_SOURCE_REASONS`,
#: `GOVERNMENT_INGESTION_REASONS`) are reported as they already stand.
CAPTURE_REASONS: Mapping[str, str] = {
    "CAPTURE_NOT_AUTHORIZED":
        "this entrypoint refuses unless execution is requested explicitly",
    "CAPTURE_MODE_CONTRADICTORY":
        "a plan and an execution were requested at the same time",
    "CAPTURE_ARGUMENT_NOT_SUPPORTED":
        "an argument this entrypoint does not support was supplied",
    "CAPTURE_EGRESS_NOT_ACKNOWLEDGED":
        "live government egress was not acknowledged exactly",
    "CAPTURE_SCHEMA_REPORT_NOT_ACKNOWLEDGED":
        "the read-only schema report was not acknowledged exactly",
    "CAPTURE_PROJECT_NOT_CONFIGURED":
        "this process is not configured with a target project to verify against",
    "CAPTURE_PROJECT_MISMATCH":
        "the supplied project reference is not the configured target project",
    "CAPTURE_RUN_IDENTITY_INVALID":
        "an explicit run identity is required and must be a UUID",
    "CAPTURE_CATALOG_EXECUTION_DISABLED":
        "catalog execution is not enabled for this process",
    "CAPTURE_PAID_EXECUTION_ENABLED":
        "paid execution must be disabled for a capture",
    "CAPTURE_PACKAGE_NOT_SUPPORTED":
        "that package is not the package this entrypoint captures",
    "CAPTURE_RESOURCE_NOT_SUPPORTED":
        "that resource is not the resource this entrypoint captures",
    "CAPTURE_BOUNDS_NOT_SUPPORTED":
        "the capture bounds are not the reviewed ones",
    "CAPTURE_LEASE_CONFIG_INVALID":
        "the lease configuration of this process is not readable",
    "CAPTURE_RUN_NOT_ELIGIBLE":
        "that run is not a prepared operator capture run",
    "CAPTURE_RUN_UNAVAILABLE":
        "that run could not be read or claimed",
    "CAPTURE_CANCELLED":
        "the run was cancelled before the capture finished",
    "CAPTURE_LEASE_LOST":
        "the worker lease was lost before the capture finished",
    "CAPTURE_REPOSITORY_UNAVAILABLE":
        "a durable catalog operation could not be completed",
    "CAPTURE_REPORT_NOT_WRITTEN":
        "the execution report could not be written to the requested path",
    "CAPTURE_UNEXPECTED_FAILURE":
        "the capture stopped on an unexpected condition",
}


def safe_message(reason_code: str) -> str:
    """The static message for any reason code this entrypoint can report.

    Three closed vocabularies, looked up in order and never fallen through to
    a formatted value: an unknown code is reported as unknown rather than
    echoed back with whatever produced it.
    """
    for vocabulary in (CAPTURE_REASONS, GOVERNMENT_SOURCE_REASONS,
                       GOVERNMENT_INGESTION_REASONS):
        if reason_code in vocabulary:
            return vocabulary[reason_code]
    return CAPTURE_REASONS["CAPTURE_UNEXPECTED_FAILURE"]


# =============================================================================
# bounded, sanitized values
# =============================================================================

def _text(value: Any, limit: int = MAX_REPORT_TEXT_CHARS) -> str:
    """One reported string, coerced and truncated. Never formatted from an
    exception, a URL, a row or a credential -- the schema below decides what
    reaches this function, and this function decides how much of it."""
    return str(value)[:limit]


def _counts(value: Any, *, limit: int) -> dict[str, int]:
    """A bounded `{code: count}` map, ordered and fail-closed per entry.

    A value that is not an integer is DROPPED rather than coerced: a count
    that is not a count is not evidence of zero, and `0 rejected` would be
    exactly the kind of false operational statement this report exists to
    avoid. Keys are sorted so the same capture renders the same document.
    """
    if not isinstance(value, Mapping):
        return {}
    bounded: dict[str, int] = {}
    for key in sorted(value, key=str):
        if len(bounded) >= limit:
            break
        count = value[key]
        if isinstance(count, bool) or not isinstance(count, int):
            continue
        bounded[_text(key)] = count
    return bounded


def _identifiers(values: Any, *, limit: int) -> list[str]:
    """A bounded list of short identifiers. Order is preserved because the
    upstream list is already the bounded one the snapshot recorded."""
    if not isinstance(values, (list, tuple)):
        return []
    return [_text(value, 64) for value in values[:limit]]


# =============================================================================
# the construction seams -- the ONLY two places a socket or a database appears
# =============================================================================

def _open_transport() -> Any:
    """The real, approved transport. Called only after every prerequisite."""
    from backend.catalog.government.transport import HttpsDataGovTransport

    return HttpsDataGovTransport()


def _open_repository() -> Any:
    """The real repository. Called only after every prerequisite.

    Imported here rather than at module scope so that importing this module
    neither builds a Supabase client nor pulls one into the process.
    """
    from backend.config import get_settings
    from backend.repository import SupabaseRepository

    return SupabaseRepository(get_settings())


# =============================================================================
# the prerequisites
# =============================================================================

def build_parser() -> argparse.ArgumentParser:
    """Every supported argument, and nothing that could name a URL.

    There is no `--url`, `--host`, `--action`, `--query`, `--filters`,
    `--offset` or `--limit` here, and unrecognised arguments are a refusal
    rather than something to ignore. Every value is parsed as TEXT and
    validated by this module, so argparse never echoes an operator-supplied
    value into an error message.
    """
    parser = argparse.ArgumentParser(
        prog="python -m backend.catalog.operator_capture",
        description=("Operator-invoked, explicitly authorized, bounded capture of the "
                     "pinned WLTP Government resource. Refuses by default."))
    parser.add_argument("--execute", action="store_true",
                        help="actually perform the capture (required; refuses without it)")
    parser.add_argument("--plan", action="store_true",
                        help="describe what an execution would do and exit; no side effects")
    parser.add_argument("--acknowledge-live-government-egress", default=None,
                        help="must be exactly the live-egress acknowledgement")
    parser.add_argument("--acknowledge-schema-report-reviewed", default=None,
                        help="must be exactly the schema-report acknowledgement")
    parser.add_argument("--project-ref", default=None,
                        help="the target project reference, verified against this process")
    parser.add_argument("--run-id", default=None,
                        help="the prepared operator capture run that owns the lease")
    parser.add_argument("--package-id", default=None, help="must be the pinned package")
    parser.add_argument("--resource-id", default=None, help="must be the pinned WLTP resource")
    parser.add_argument("--page-limit", default=None,
                        help=f"must be exactly {CAPTURE_PAGE_LIMIT}")
    parser.add_argument("--max-pages", default=None,
                        help=f"optional; may only restate {CAPTURE_MAX_PAGES}")
    parser.add_argument("--max-records", default=None,
                        help=f"optional; may only restate {CAPTURE_MAX_RECORDS}")
    parser.add_argument("--report-path", default=None,
                        help="optional path for the sanitized report; the only file written")
    return parser


def configured_project_ref(env: Mapping[str, str]) -> str:
    """The project reference this process is configured to write to.

    Derived from `SUPABASE_URL`'s first host label, which is the project this
    repository's every durable write actually lands in. Returned for an exact
    comparison and never printed, logged or reported.
    """
    host = urlsplit((env.get("SUPABASE_URL") or "").strip()).hostname or ""
    return host.split(".")[0] if host else ""


def _exact_int(value: Any, expected: int) -> bool:
    """Whether a supplied text value is exactly one integer. Malformed text,
    a float, a sign, padding or a different number are all False."""
    text = str(value).strip()
    return text.isdigit() and int(text) == expected


def _refusal(args: argparse.Namespace, extra: Sequence[str],
             env: Mapping[str, str]) -> str:
    """The reason this capture must not proceed, or an empty string.

    PURE: it reads the parsed arguments and the process environment and
    nothing else. No socket, no file, no database, no clock. Every caller
    evaluates it to completion before anything is constructed, which is what
    makes "refuses before side effects" a property of the call graph.
    """
    if extra:
        return "CAPTURE_ARGUMENT_NOT_SUPPORTED"
    if args.plan and args.execute:
        return "CAPTURE_MODE_CONTRADICTORY"
    if not args.execute:
        return "CAPTURE_NOT_AUTHORIZED"
    if args.acknowledge_live_government_egress != EGRESS_ACKNOWLEDGEMENT:
        return "CAPTURE_EGRESS_NOT_ACKNOWLEDGED"
    if args.acknowledge_schema_report_reviewed != SCHEMA_REPORT_ACKNOWLEDGEMENT:
        return "CAPTURE_SCHEMA_REPORT_NOT_ACKNOWLEDGED"
    configured = configured_project_ref(env)
    if not configured:
        return "CAPTURE_PROJECT_NOT_CONFIGURED"
    if not args.project_ref or str(args.project_ref) != configured:
        return "CAPTURE_PROJECT_MISMATCH"
    try:
        UUID(str(args.run_id))
    except (AttributeError, TypeError, ValueError):
        return "CAPTURE_RUN_IDENTITY_INVALID"
    if not catalog_execution_enabled(env):
        return "CAPTURE_CATALOG_EXECUTION_DISABLED"
    if (env.get(PAID_EXECUTION_FLAG) or "").strip().lower() in TRUE_VALUES:
        return "CAPTURE_PAID_EXECUTION_ENABLED"
    if args.package_id != src.CKAN_PACKAGE_ID:
        return "CAPTURE_PACKAGE_NOT_SUPPORTED"
    if args.resource_id != src.WLTP_RESOURCE_ID:
        return "CAPTURE_RESOURCE_NOT_SUPPORTED"
    if not _exact_int(args.page_limit, CAPTURE_PAGE_LIMIT):
        return "CAPTURE_BOUNDS_NOT_SUPPORTED"
    if args.max_pages is not None and not _exact_int(args.max_pages, CAPTURE_MAX_PAGES):
        return "CAPTURE_BOUNDS_NOT_SUPPORTED"
    if args.max_records is not None and not _exact_int(args.max_records, CAPTURE_MAX_RECORDS):
        return "CAPTURE_BOUNDS_NOT_SUPPORTED"
    try:
        _lease_settings(env)
    except (TypeError, ValueError):
        # A lease this process cannot size is a lease it must not take. Read
        # here, with the rest of the prerequisites, so it refuses before the
        # repository exists rather than after the run has been claimed.
        return "CAPTURE_LEASE_CONFIG_INVALID"
    return ""


def _lease_settings(env: Mapping[str, str]) -> tuple[int, float]:
    """The worker's lease duration and heartbeat interval, or fail closed.

    A malformed value is a refusal rather than a default: silently falling
    back would mean a capture ran under a lease nobody chose.
    """
    lease_seconds = int(str(env.get(LEASE_SECONDS_VAR) or DEFAULT_LEASE_SECONDS).strip())
    interval = float(str(env.get(HEARTBEAT_INTERVAL_VAR)
                         or DEFAULT_HEARTBEAT_INTERVAL_SECONDS).strip())
    if lease_seconds < 3 or interval <= 0:
        raise ValueError("lease configuration out of range")
    return lease_seconds, max(1.0, min(interval, lease_seconds / 3))


def _run_is_eligible(run: Mapping[str, Any]) -> bool:
    """Whether this run is one an operator prepared for a capture.

    All three conditions, together. See the module docstring: the marker alone
    can come from a browser request's metadata, and the launch state alone has
    a sub-request window where it is still `pending`.
    """
    if str(run.get("status")) != ELIGIBLE_RUN_STATUS:
        return False
    if str(run.get("launch_state")) != ELIGIBLE_LAUNCH_STATE:
        return False
    run_input = run.get("input")
    metadata = run_input.get("metadata") if isinstance(run_input, Mapping) else None
    if not isinstance(metadata, Mapping):
        return False
    return str(metadata.get("milo_operation")) == OPERATOR_CAPTURE_OPERATION


# =============================================================================
# the lease: kept alive, and watched
# =============================================================================

class _CaptureSupervisor:
    """One heartbeat that both extends the lease and observes cancellation.

    `heartbeat_run_guarded` returns the run row, so a single call answers both
    questions. That is what makes the cancellation checker an in-memory flag
    read: the ingestor consults it once per record, and a per-record database
    read over ~101 000 rows would be its own outage.

    Losing the lease is NOT the thing that protects the data -- the database
    re-checks the lease atomically on every guarded write and on activation, so
    a stale process simply stops being accepted. This stops it sooner, and
    tells the operator which of the two happened.
    """

    def __init__(self, repository: Any, lease: WorkerLease, *, lease_seconds: int,
                 interval: float) -> None:
        self._repository = repository
        self._lease = lease
        self._lease_seconds = lease_seconds
        self._interval = interval
        self._lease_lost = threading.Event()
        self._cancelled = threading.Event()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    def beat(self) -> bool:
        """One heartbeat. False once the lease is gone."""
        try:
            run = self._repository.heartbeat(
                self._lease.run_id, self._lease.worker_id,
                lease_seconds=self._lease_seconds, attempt=self._lease.attempt,
                lease_token=self._lease.lease_token)
        except Exception:
            # Deliberately broad and deliberately silent: whatever the cause,
            # this process can no longer prove it holds the lease, and the
            # exception may quote a URL, a row or a database message.
            self._lease_lost.set()
            return False
        status = str((run or {}).get("status") or "")
        if status == "cancellation_requested" or status in TERMINAL_STATES:
            self._cancelled.set()
        return True

    def should_stop(self) -> bool:
        return self._lease_lost.is_set() or self._cancelled.is_set()

    @property
    def stop_reason(self) -> str:
        if self._lease_lost.is_set():
            return "CAPTURE_LEASE_LOST"
        if self._cancelled.is_set():
            return "CAPTURE_CANCELLED"
        return ""

    @property
    def lease_lost(self) -> bool:
        return self._lease_lost.is_set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def start(self) -> None:
        """Beat ONCE, then keep beating.

        The immediate beat is not decoration. It proves the lease this process
        just claimed is the one the database still recognises, and it observes
        a cancellation that was requested before the capture began -- both
        before a single page is read, rather than one heartbeat interval into
        the work.
        """
        self.beat()
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="milo-capture-heartbeat",
                                            daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout=max(2.0, self._interval + 1.0))
            self._thread = None

    def _loop(self) -> None:
        while not self._stopping.wait(self._interval):
            if not self.beat():
                return


# =============================================================================
# the report
# =============================================================================

def _snapshot_document(report: Any) -> dict[str, Any]:
    """The reviewed operational facts of one ingestion, and only those.

    Field by field from a closed schema. `IngestionReport.candidates` holds
    whole candidate payloads and is never read here; neither is any response
    body, page body, raw row or exception.
    """
    return {
        "snapshot_id": _text(report.snapshot_id),
        "snapshot_key": _text(report.snapshot_key),
        "content_sha256": _text(report.content_sha256),
        "schema_fingerprint": _text(report.schema_fingerprint),
        "resource_id": _text(report.resource_id),
        "upstream_version": _text(report.upstream_version),
        "upstream_version_kind": _text(report.upstream_version_kind),
        "declared_record_count": int(report.declared_record_count),
        "stored_record_count": int(report.stored_record_count),
        "page_count": int(report.page_count),
        "candidate_count": int(report.candidate_count),
        "candidate_status_counts": _counts(report.candidate_status_counts,
                                           limit=MAX_REPORT_STATUS_COUNTS),
        "normalization_contract": _text(report.normalization_contract),
        "normalized_record_count": int(report.normalized_record_count),
        "normalization_issue_count": int(report.normalization_issue_count),
        "normalization_issues": _counts(report.normalization_issues,
                                        limit=MAX_REPORT_ISSUE_REASONS),
        "normalization_issue_records": _identifiers(report.normalization_issue_records,
                                                    limit=MAX_REPORT_ISSUE_RECORDS),
        "rejected_record_count": int(report.rejected_record_count),
        "activated": bool(report.activated),
        "reused_existing": bool(report.reused_existing),
    }


def capture_document(outcome: RefreshOutcome, *, replayed: bool) -> dict[str, Any]:
    """What this capture did, as the bounded record an operator keeps.

    The four outcomes are distinguished from what the refresh and the snapshot
    STATE rather than from what this process happened to observe: `unchanged`
    is the register publishing the version already held, `reused` is another
    run's completed identical snapshot, `replayed` is this run's own completed
    capture re-derived, and `changed` is a new snapshot.
    """
    report = outcome.report
    if not outcome.changed:
        state = "unchanged"
    elif report is not None and report.reused_existing:
        state = "reused"
    elif replayed:
        state = "replayed"
    else:
        state = "changed"
    document: dict[str, Any] = {
        "outcome": state,
        "resource_id": _text(outcome.resource_id),
        "upstream_version": _text(outcome.upstream_version),
        "upstream_version_kind": _text(outcome.upstream_version_kind),
        "active_snapshot_key": _text(outcome.active_snapshot_key),
        "no_op": bool(outcome.no_op),
        "diff_unavailable": bool(outcome.diff_unavailable),
        "research_required": bool(outcome.research_required),
    }
    if report is not None:
        document["snapshot"] = _snapshot_document(report)
    if outcome.diff is not None:
        # COUNTS only. The delta items carry manufacturer and model text read
        # out of the register, which is exactly what a sanitized report does
        # not carry; the counts are exact for the whole resource either way.
        document["diff"] = {
            "previous_snapshot_key": _text(outcome.diff.previous_snapshot_key),
            "added_count": int(outcome.diff.added_count),
            "changed_count": int(outcome.diff.changed_count),
            "removed_count": int(outcome.diff.removed_count),
            "bounded": bool(outcome.diff.bounded),
        }
    return document


def plan_document() -> dict[str, Any]:
    """Exactly what an execution WOULD construct, computed from constants."""
    return {
        "package_id": src.CKAN_PACKAGE_ID,
        "resource_id": src.WLTP_RESOURCE_ID,
        "query": "whole_resource",
        "page_limit": CAPTURE_PAGE_LIMIT,
        "max_pages": CAPTURE_MAX_PAGES,
        "max_records": CAPTURE_MAX_RECORDS,
        "max_response_bytes": src.MAX_RESPONSE_BYTES,
        "transport": "HttpsDataGovTransport",
        "client": "DataGovClient",
        "operation": "GovernmentCatalogRefresh.sync_if_changed",
        "requires_prepared_run": True,
        "would_open_network_egress": True,
        "would_connect_to_database": True,
        "side_effects_performed": [],
    }


def _envelope(status: str, reason_code: str = "", **sections: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "entrypoint": CAPTURE_ENTRYPOINT,
        "status": status,
        "reason_code": reason_code,
        "reason": safe_message(reason_code) if reason_code else "",
    }
    document.update(sections)
    return document


def _emit(document: Mapping[str, Any], report_path: str | None) -> bool:
    """One JSON document to stdout, and optionally that same document to the
    one file an operator explicitly asked for. Returns False if the file could
    not be written, which is itself a non-zero outcome rather than a silence.
    """
    rendered = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if report_path is None:
        return True
    try:
        with open(report_path, "w", encoding="utf-8") as handle:
            handle.write(rendered + "\n")
    except OSError:
        return False
    return True


# =============================================================================
# the execution
# =============================================================================

def _classify(failure: BaseException) -> str:
    """One static reason code for anything the capture can raise.

    A library or database exception never reaches output: `AppError.message`
    can carry a database message and a transport exception can carry a URL, so
    both are reduced to the property that failed. The capture and ingestion
    vocabularies are re-checked against their own allowlists rather than
    trusted, so only a code this repository owns can ever be printed.
    """
    if isinstance(failure, GovernmentSourceError):
        if failure.reason_code in GOVERNMENT_SOURCE_REASONS:
            return failure.reason_code
    if isinstance(failure, GovernmentIngestionError):
        if failure.reason_code in GOVERNMENT_INGESTION_REASONS:
            return failure.reason_code
    if isinstance(failure, AppError):
        return "CAPTURE_REPOSITORY_UNAVAILABLE"
    return "CAPTURE_UNEXPECTED_FAILURE"


def _finalize(repository: Any, lease: WorkerLease, *, document: Mapping[str, Any],
              reason_code: str, cancelled: bool) -> None:
    """Record the run's terminal state under the lease, or leave it alone.

    A lost lease means this process may no longer decide anything about the
    run, so the conflict is swallowed here: the database has already refused,
    and re-raising would replace a precise capture reason with a transition
    error. Nothing durable depends on this succeeding -- the snapshot is
    already decided by the time it runs.
    """
    lease_kwargs = {"worker_id": lease.worker_id, "attempt": lease.attempt,
                    "lease_token": lease.lease_token}
    try:
        if cancelled:
            repository.transition_run(lease.run_id, "cancelled",
                                      expected_worker_id=lease.worker_id,
                                      expected_attempt=lease.attempt,
                                      expected_lease_token=lease.lease_token)
        elif reason_code:
            repository.mark_run_failed(lease.run_id, reason_code, safe_message(reason_code),
                                       **lease_kwargs)
        else:
            repository.mark_run_complete(lease.run_id, dict(document), **lease_kwargs)
    except Exception:
        # Broad and silent for the same reason the heartbeat is: the message
        # may quote the database, and the capture's own outcome is already
        # established and already reported.
        return


def _execute(args: argparse.Namespace, env: Mapping[str, str]) -> tuple[int, dict[str, Any]]:
    """The authorized path. Every prerequisite has already passed."""
    # Already validated by `_refusal`, which every caller evaluates first.
    lease_seconds, interval = _lease_settings(env)
    run_id = UUID(str(args.run_id))

    # The project identity is verified a SECOND time, against `os.environ` --
    # the mapping the repository itself will read. `env` is a seam for tests
    # and validators, so without this a caller could satisfy the gate with one
    # mapping while the connection went to the project named by another. In
    # production the two are the same object and this is a no-op.
    if configured_project_ref(os.environ) != str(args.project_ref):
        return EXIT_REFUSED, _envelope("refused", "CAPTURE_PROJECT_MISMATCH")

    repository = _open_repository()

    # The run gate, server-side and BEFORE the claim: a mistyped identity
    # refuses instead of taking somebody else's lease.
    try:
        run = repository.get_run(run_id)
    except Exception:
        return EXIT_REFUSED, _envelope("refused", "CAPTURE_RUN_UNAVAILABLE")
    if not _run_is_eligible(run):
        return EXIT_REFUSED, _envelope("refused", "CAPTURE_RUN_NOT_ELIGIBLE")

    worker_id = f"operator-capture-{uuid4()}"
    try:
        claimed = repository.claim_run(run_id, worker_id, lease_seconds=lease_seconds)
        lease = WorkerLease(run_id=run_id, worker_id=worker_id,
                            attempt=int(claimed.get("attempt") or 0),
                            lease_token=str(claimed.get("lease_token") or ""))
    except Exception:
        return EXIT_REFUSED, _envelope("refused", "CAPTURE_RUN_UNAVAILABLE")

    supervisor = _CaptureSupervisor(repository, lease, lease_seconds=lease_seconds,
                                    interval=interval)
    observed: list[str] = []

    def record_event(event_type: str, _payload: Mapping[str, Any]) -> None:
        """The ingestor's own progress signals, kept in memory and bounded.

        Nothing is written to `run_events` here: these names are not in
        `backend.runtime.EVENT_TYPES`, and inventing durable events for an
        operator capture is not this stage's work. Only the type is kept, and
        only to tell a replay from a first capture in the report.
        """
        if len(observed) < 64:
            observed.append(str(event_type))

    supervisor.start()
    try:
        client = DataGovClient(_open_transport(), page_limit=CAPTURE_PAGE_LIMIT,
                               max_pages=CAPTURE_MAX_PAGES, max_records=CAPTURE_MAX_RECORDS,
                               cancellation_checker=supervisor.should_stop)
        operation = GovernmentCatalogRefresh(
            repository, lease, client=client, resource_id=src.WLTP_RESOURCE_ID,
            cancellation_checker=supervisor.should_stop, event_sink=record_event)
        # Whole-resource capture: no `q`, no `filters`, no paging argument.
        outcome = operation.sync_if_changed(package_id=src.CKAN_PACKAGE_ID, query=None)
    except CancellationRequested:
        reason = supervisor.stop_reason or "CAPTURE_CANCELLED"
        supervisor.stop()
        _finalize(repository, lease, document={}, reason_code=reason,
                  cancelled=supervisor.cancelled and not supervisor.lease_lost)
        return EXIT_FAILED, _envelope("failed", reason)
    except Exception as failure:  # noqa: BLE001 - reduced to a static code below
        reason = _classify(failure)
        supervisor.stop()
        _finalize(repository, lease, document={}, reason_code=reason, cancelled=False)
        return EXIT_FAILED, _envelope("failed", reason)
    supervisor.stop()

    document = capture_document(outcome, replayed="catalog_snapshot_replayed" in observed)
    _finalize(repository, lease, document=document, reason_code="", cancelled=False)
    return EXIT_OK, _envelope("succeeded", "", capture=document)


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None) -> int:
    """Refuse, plan, or capture. Returns the process exit status."""
    environment = os.environ if env is None else env
    args, extra = build_parser().parse_known_args(list(argv or []))

    reason = _refusal(args, extra, environment)
    if args.plan and not args.execute and not extra:
        # A plan states what an execution would construct and performs none of
        # it. It is reported as a plan whether or not the other prerequisites
        # are satisfied, and it never reads the project, the run or the flags
        # into an outcome.
        status, document = EXIT_OK, _envelope("planned", "", plan=plan_document())
    elif reason:
        status, document = EXIT_REFUSED, _envelope("refused", reason)
    else:
        status, document = _execute(args, environment)

    if not _emit(document, args.report_path):
        print(safe_message("CAPTURE_REPORT_NOT_WRITTEN"), file=sys.stderr)
        return EXIT_FAILED if status == EXIT_OK else status
    if document["reason_code"]:
        print(f'{document["reason_code"]}: {document["reason"]}', file=sys.stderr)
    return status


__all__ = ["CAPTURE_ENTRYPOINT", "CAPTURE_MAX_PAGES", "CAPTURE_MAX_RECORDS",
           "CAPTURE_PAGE_LIMIT", "CAPTURE_REASONS", "EGRESS_ACKNOWLEDGEMENT",
           "ELIGIBLE_LAUNCH_STATE", "ELIGIBLE_RUN_STATUS", "EXIT_FAILED", "EXIT_OK",
           "EXIT_REFUSED", "OPERATOR_CAPTURE_OPERATION", "PAID_EXECUTION_FLAG",
           "SCHEMA_REPORT_ACKNOWLEDGEMENT", "build_parser", "capture_document",
           "configured_project_ref", "main", "plan_document", "safe_message"]


if __name__ == "__main__":  # pragma: no cover - the process entry point
    raise SystemExit(main(sys.argv[1:]))

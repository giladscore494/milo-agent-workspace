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

Two modes, and neither is the default
-------------------------------------

``--prepare`` makes an operator capture run and takes ownership of its launch.
It opens no socket, sends no request and captures nothing.

``--execute`` performs the capture, and must be given a prepared run's identity
explicitly. Preparing a run never starts a capture, and capturing never
prepares one: they are separate invocations, separately gated, so neither can
be a side effect of the other.

Refusal is the default, and it is structural
--------------------------------------------

Importing this module, asking it for `--help`, running it with no arguments or
asking it to plan opens no socket, constructs no transport, connects to no
database, claims no run, acquires no lease, writes no file and mutates nothing.
That is not a policy the code checks late: the repository and the transport are
CONSTRUCTED by `_open_repository()` and `_open_transport()`, and both are
called only after every prerequisite in `_refusal()` has passed.

``--prepare`` is gated too: it requires the OPERATOR-0 acknowledgement, the
project identity, the catalog flag and paid execution off -- everything below
except the live-egress acknowledgement, which it does not ask for because it
performs no egress, and the resource/bounds arguments, which it has no use for.

The nine prerequisites of a CAPTURE, all required together, in this order:

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
    lease-guarded and a lease belongs to a run. The run is one ``--prepare``
    made and owns; see "the lease boundary, the launch boundary".
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

The lease boundary, the launch boundary, and why they are different
-------------------------------------------------------------------

Catalog writes are lease-guarded, so this controller needs an AUTHENTIC lease:
it calls the existing `claim_run` (the single-statement CAS in migration 012),
builds `WorkerLease` from what the database returned, and heartbeats through
the existing guarded RPC. It invents no lease, holds no direct insert, and
never passes or prints lease material.

But the lease is the WRONG boundary for the question "may an ordinary model
worker execute this run". `claim_run_lease` predicates its CAS on status,
worker and lease expiry only -- not on `launch_state`, not on run metadata --
so it hands a lease to whoever asks first, launcher or operator alike. The
launch boundary is a DIFFERENT CAS, `try_acquire_launch`, and that is the one
that decides who owns a run.

**There is no way in this repository to create a run that is not an ordinary
model run.** Every creation route reaches
`backend.main._create_and_launch_run`, which creates the run and immediately
hands it to `JobLauncher.launch()`; the launched worker resolves an engine from
`project.workflow_key` and runs a model. So an operator needs a supported way
to make a run no launcher will ever take -- and `--prepare` is it (see
`_prepare`), through existing repository methods, with no migration, no new
repository method and no direct table write.

`--prepare` creates the run the ordinary way and then wins
`try_acquire_launch` -- the SAME single-statement CAS the ordinary path uses.
That is the one authoritative transition, and exactly one side can win it:

*   **The operator wins.** The run is rested in `OPERATOR_OWNED_LAUNCH_STATE`,
    which `try_acquire_launch` cannot acquire from, so
    `_create_and_launch_run` can never call `JobLauncher.launch()` for it.
*   **The launcher wins.** `try_acquire_launch` returns `None` to the
    operator, preparation refuses with `CAPTURE_LAUNCH_OWNERSHIP_LOST`, and
    the run is left entirely alone -- no claim, no transport, no request, no
    write.

An earlier round of this module checked `status`, `launch_state` and the
marker with a read and then called `claim_run`. That is a read-then-act
window: the launcher could take the run between the two, and `claim_run` would
not notice. The window is closed twice over. Eligibility now requires a launch
state the ordinary path can neither produce nor acquire, and it is re-verified
on the row `claim_run` ITSELF returned (`returning *`), so the check and the
claim are one step. The pre-claim read survives only as a cheap early refusal
for a wrong run id, and nothing depends on it still being true afterwards.

The marker alone is still not enough, and was never meant to be: a browser
request's `metadata` reaches `input.metadata`, so a user can put the string on
an ordinary run. What a user cannot do is give that run
`OPERATOR_OWNED_LAUNCH_STATE`.


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
import hashlib
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
from backend.event_registry import CAPTURE_SNAPSHOT_REPLAYED
from backend.run_identity import RunIdentity
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

#: What `--prepare` writes into the run's input metadata, and the two
#: server-owned run fields that prove the operator -- not a launcher -- owns
#: this run's launch.
OPERATOR_CAPTURE_OPERATION = "catalog.government.capture"
ELIGIBLE_RUN_STATUS = "queued"

#: The launch state an operator-prepared run comes to rest in, and the set
#: `try_acquire_launch` is able to acquire from.
#:
#: `'none'` is migration 009's own default and is inside the
#: `runs_launch_state_check` constraint, so nothing here invents a state or
#: needs a migration. Two properties make it the right one, and both are
#: repository facts rather than conventions:
#:
#: *   **Unacquirable.** `try_acquire_launch` moves a run only from
#:     `pending` or `launch_failed`, so a run resting in `'none'` can never be
#:     acquired by the ordinary launch path again. Its single caller is
#:     `backend/main.py`, and `set_launch_state`'s single caller only ever
#:     writes `launching`/`launched`/`launch_failed`/`launch_unknown` -- so
#:     nothing in the product can move a run back out of `'none'` either.
#: *   **Truthful.** It asserts the ABSENCE of a launch, which is exactly what
#:     an operator capture run is. `launching`, `launched` and
#:     `launch_unknown` would each claim a launch that never happened, and
#:     `launch_unknown` would additionally park the run for an operator
#:     reconciliation that is not owed.
OPERATOR_OWNED_LAUNCH_STATE = "none"
LAUNCH_ACQUIRABLE_STATES: frozenset[str] = frozenset({"pending", "launch_failed"})

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
    "CAPTURE_LAUNCH_OWNERSHIP_LOST":
        "the ordinary launch path owns this run, so the operator may not take it",
    "CAPTURE_CONVERSATION_IDENTITY_INVALID":
        "preparation requires an existing conversation and the operator's own user identity",
    "CAPTURE_CONVERSATION_UNAVAILABLE":
        "that conversation does not exist or that user is not a member of its project",
    "CAPTURE_PREPARATION_FAILED":
        "the operator capture run could not be prepared",
    "CAPTURE_IDEMPOTENCY_KEY_IN_USE":
        "a run this entrypoint did not prepare already holds that idempotency identity",
    "CAPTURE_ARGUMENT_NOT_VALID_IN_MODE":
        "that argument belongs to a different mode of this entrypoint",
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
    parser.add_argument("--prepare", action="store_true",
                        help="create and take operator ownership of a capture run; captures nothing")
    parser.add_argument("--conversation-id", default=None,
                        help="--prepare only: the existing conversation the run belongs to")
    parser.add_argument("--requested-by", default=None,
                        help="--prepare only: the operator's own user id, checked for membership")
    parser.add_argument("--idempotency-key", default=None,
                        help="--prepare only: replaying it returns the same prepared run")
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


def _shared_refusal(args: argparse.Namespace, env: Mapping[str, str]) -> str:
    """What `--prepare` and `--execute` BOTH require.

    Preparation is a catalog operation that writes a durable run, so it is
    gated on the operator's acknowledgement of OPERATOR-0, on the configured
    project identity and on CODE-2's flag exactly as the capture is. It is NOT
    gated on the live-egress acknowledgement, because it performs no egress --
    demanding an egress acknowledgement for an operation that sends nothing
    would train an operator to supply it without meaning it.
    """
    if args.acknowledge_schema_report_reviewed != SCHEMA_REPORT_ACKNOWLEDGEMENT:
        return "CAPTURE_SCHEMA_REPORT_NOT_ACKNOWLEDGED"
    configured = configured_project_ref(env)
    if not configured:
        return "CAPTURE_PROJECT_NOT_CONFIGURED"
    if not args.project_ref or str(args.project_ref) != configured:
        return "CAPTURE_PROJECT_MISMATCH"
    if not catalog_execution_enabled(env):
        return "CAPTURE_CATALOG_EXECUTION_DISABLED"
    if (env.get(PAID_EXECUTION_FLAG) or "").strip().lower() in TRUE_VALUES:
        return "CAPTURE_PAID_EXECUTION_ENABLED"
    try:
        _lease_settings(env)
    except (TypeError, ValueError):
        return "CAPTURE_LEASE_CONFIG_INVALID"
    return ""


def _prepare_refusal(args: argparse.Namespace, env: Mapping[str, str]) -> str:
    """The reason a PREPARATION must not proceed, or an empty string.

    Pure, like `_refusal`, and evaluated to completion before the repository
    exists. Preparation takes no resource, bound, page size or run identity:
    it MAKES the run identity, and the capture still has to be given one
    explicitly afterwards.
    """
    shared = _shared_refusal(args, env)
    if shared:
        return shared
    for identity in (args.conversation_id, args.requested_by):
        try:
            UUID(str(identity))
        except (AttributeError, TypeError, ValueError):
            return "CAPTURE_CONVERSATION_IDENTITY_INVALID"
    return ""


#: Which arguments belong to which mode. One parser serves both modes, so an
#: argument that means nothing in the mode it was given used to be accepted
#: and silently ignored -- `--prepare --page-limit 1000` looked honoured and
#: was not, and `--execute --conversation-id ...` looked like it scoped the
#: capture and did not. Both now fail closed.
#:
#: `--project-ref`, the schema-report acknowledgement and `--report-path` are
#: deliberately universal: identity and output are not mode-specific.
CAPTURE_ONLY_ARGUMENTS: tuple[tuple[str, str], ...] = (
    ("acknowledge_live_government_egress", "--acknowledge-live-government-egress"),
    ("run_id", "--run-id"),
    ("package_id", "--package-id"),
    ("resource_id", "--resource-id"),
    ("page_limit", "--page-limit"),
    ("max_pages", "--max-pages"),
    ("max_records", "--max-records"),
)
PREPARE_ONLY_ARGUMENTS: tuple[tuple[str, str], ...] = (
    ("conversation_id", "--conversation-id"),
    ("requested_by", "--requested-by"),
    ("idempotency_key", "--idempotency-key"),
)


def _supplied(args: argparse.Namespace, arguments: Sequence[tuple[str, str]]) -> bool:
    """Whether any of those arguments was given at all.

    `None` is "absent"; an EMPTY string is supplied, because an operator who
    wrote `--run-id ""` did type the argument and deserves to be told it does
    not belong in this mode rather than have it ignored.
    """
    return any(getattr(args, name, None) is not None for name, _flag in arguments)


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
    # The three modes are mutually exclusive. Two at once is a contradiction
    # rather than a precedence question: an operator who asked for both did
    # not decide which one they wanted.
    if sum(bool(mode) for mode in (args.plan, args.execute, args.prepare)) > 1:
        return "CAPTURE_MODE_CONTRADICTORY"
    # Mode-incompatible arguments fail closed rather than being ignored. A
    # plan describes the CAPTURE and is a constant document, so it accepts
    # neither set: `--plan --conversation-id ...` would otherwise print the
    # capture plan to somebody who asked about preparation.
    if args.prepare and _supplied(args, CAPTURE_ONLY_ARGUMENTS):
        return "CAPTURE_ARGUMENT_NOT_VALID_IN_MODE"
    if not args.prepare and _supplied(args, PREPARE_ONLY_ARGUMENTS):
        return "CAPTURE_ARGUMENT_NOT_VALID_IN_MODE"
    if args.plan and _supplied(args, CAPTURE_ONLY_ARGUMENTS):
        return "CAPTURE_ARGUMENT_NOT_VALID_IN_MODE"
    if args.plan:
        # A plan has no further prerequisites: it constructs nothing, reads no
        # project, no run and no flag, and prints a constant document. The
        # checks below are about doing something, and a plan does nothing.
        return ""
    if args.prepare:
        return _prepare_refusal(args, env)
    if not args.execute:
        return "CAPTURE_NOT_AUTHORIZED"
    if args.acknowledge_live_government_egress != EGRESS_ACKNOWLEDGEMENT:
        return "CAPTURE_EGRESS_NOT_ACKNOWLEDGED"
    shared = _shared_refusal(args, env)
    if shared:
        return shared
    try:
        UUID(str(args.run_id))
    except (AttributeError, TypeError, ValueError):
        return "CAPTURE_RUN_IDENTITY_INVALID"
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


def _carries_operator_marker(run: Mapping[str, Any]) -> bool:
    """Whether this run's input metadata states the operator capture intent.

    NEVER sufficient on its own. A browser request's `metadata` reaches
    `input.metadata`, so a user can put this string on an ordinary run. What
    it cannot do is give that run `OPERATOR_OWNED_LAUNCH_STATE`, because the
    only way to reach that state is `--prepare` winning the launch CAS.
    """
    run_input = run.get("input")
    metadata = run_input.get("metadata") if isinstance(run_input, Mapping) else None
    if not isinstance(metadata, Mapping):
        return False
    return str(metadata.get("milo_operation")) == OPERATOR_CAPTURE_OPERATION


def _operator_owns_launch(run: Mapping[str, Any]) -> bool:
    """Whether the OPERATOR, not a launcher, owns this run's launch.

    The launch state is the load-bearing half and the marker is the intent
    half, and they are checked together. The state cannot be reached from the
    ordinary path at all: `try_acquire_launch` acquires only from
    `LAUNCH_ACQUIRABLE_STATES` and `set_launch_state` never writes this value,
    so `OPERATOR_OWNED_LAUNCH_STATE` is reachable only through `--prepare`
    winning that CAS, and is terminal with respect to the launcher once
    reached.
    """
    return (str(run.get("launch_state")) == OPERATOR_OWNED_LAUNCH_STATE
            and _carries_operator_marker(run))


def _run_is_eligible(run: Mapping[str, Any]) -> bool:
    """Whether this run is one `--prepare` produced and still owns.

    `status` is checked too, so a prepared run that has since been started,
    cancelled or finished is not captured a second time. The CLAIM re-checks
    the ownership half on its own returned row (`_execute`), which is what
    removes the read-then-claim window rather than narrowing it.
    """
    return str(run.get("status")) == ELIGIBLE_RUN_STATUS and _operator_owns_launch(run)


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


#: What the prepared run's message and input carry. Fixed text: nothing an
#: operator types reaches the run body, so a run cannot carry a prompt.
PREPARED_RUN_CONTENT = "operator catalog capture run; not executed by a model worker"


def _preparation_fingerprint() -> str:
    """The request fingerprint a prepared run is stored with.

    A deliberate MIRROR of `backend.main._request_fingerprint`, not an import:
    importing `backend.main` would pull FastAPI, the execution guard and the
    whole API surface into an entrypoint whose import has to stay inert.
    `tests/test_catalog_operator_capture.py` holds the two to the same output
    for the same input, so the mirror cannot drift silently.

    It is computed over FIXED content and metadata, so every preparation
    produces the same value and a replay stores nothing new. It is recorded,
    never used to decide ownership -- that decision rests on server-owned
    launch state alone.
    """
    canonical = json.dumps(
        {"content": PREPARED_RUN_CONTENT,
         "metadata": {"milo_operation": OPERATOR_CAPTURE_OPERATION}},
        sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _prepare(args: argparse.Namespace, env: Mapping[str, str]) -> tuple[int, dict[str, Any]]:
    """Create a capture run and take operator ownership of its launch.

    This is the supported answer to "where does the run come from". Before it
    existed, the only way to reach the required state was a manual row edit --
    an unsupported, unreviewable workaround for a capability whose whole point
    is that it is reviewable.

    Everything here goes through existing repository methods. There is no SQL,
    no table name, no direct insert, no migration, and `JobLauncher` is never
    imported, constructed or called.

    Creation is ONE transactional call, and it says whether it created
    --------------------------------------------------------------------

    An earlier round of this function used `create_user_message` followed by
    `create_queued_run(idempotency_key=K)`. That had two defects, and the
    first was serious.

    `create_queued_run` RETURNS AN EXISTING RUN when `(conversation,
    requested_by, idempotency_key)` already identifies one, and says nothing
    about which happened. So a key collision with an ORDINARY product run
    handed this function a `queued`/`pending` run it had not created -- and it
    went on to win `try_acquire_launch` for that run and rest it in the
    operator-owned state. An ordinary run would have been silently converted
    into an operator capture run and made permanently unlaunchable, purely
    because a key collided. If that run also carried a browser-supplied
    `milo_operation` marker, the result satisfied the capture predicate too.

    `create_message_and_run` is the contract that fixes it, and repository
    inspection confirms it rather than assuming it. Console 6's atomic creator
    takes the per-user and per-project advisory locks, performs the idempotency
    lookup FIRST, and on a hit returns `{'run': ..., 'created': false}` before
    inserting anything. Otherwise it inserts the message, the run and this
    control-plane run's immutable identity in ONE transaction.
    `MemoryRepository.create_message_and_run` mirrors all of that exactly.

    The ownership rule follows directly from that flag:

    *   **`created` is true** -- this run is this call's own work. It is born
        `queued`/`pending`, which is LAUNCHABLE, and it stays that way for
        exactly as long as the CAS below takes.
    *   **`created` is false** -- something else already holds this identity.
        Launch ownership is NEVER acquired in this branch. The run is reported
        as a replay only when it is one this entrypoint already prepared and
        has not yet consumed (`_run_is_eligible`), and that predicate is
        anchored on `OPERATOR_OWNED_LAUNCH_STATE` -- server-owned, unreachable
        from any browser path -- so the marker is never load-bearing. Anything
        else is refused and left exactly as it was found.

    The ORDER of the rest is the safety property:

    1.  `get_conversation(id, requested_by)` -- membership, enforced by the
        repository exactly as it is for a browser request. An operator who is
        not a member of the conversation's project gets the same not-found the
        browser would, and nothing is created.
    2.  `create_message_and_run` -- as above.
    3.  `try_acquire_launch` -- THE atomic boundary, and only ever on a run
        this call created. This is the same single-statement CAS
        `backend/main.py` uses, so the operator and the ordinary launch path
        compete at one authoritative transition and exactly one can win.
        Losing it is a refusal, not a retry.
    4.  `set_launch_state(OPERATOR_OWNED_LAUNCH_STATE)` -- rest the run in a
        state the launcher can never acquire again. Safe as a plain UPDATE
        precisely because step 3 already established exclusivity; doing it
        WITHOUT step 3 would be the defect, since `set_launch_state` is
        unconditional and would happily overwrite a launcher's `launching`.

    A failure or a crash between 3 and 4 leaves the run at `launching`, which
    is fail-closed in both directions: `try_acquire_launch` cannot acquire it,
    so no worker is ever launched for it, and `_run_is_eligible` refuses it,
    so it is not capturable either. It is inert, and it is not a model run.
    """
    from backend.budget import BudgetConfig

    repository = _open_repository()
    conversation_id = UUID(str(args.conversation_id))
    requested_by = UUID(str(args.requested_by))

    try:
        repository.get_conversation(conversation_id, requested_by)
    except Exception:
        # Membership and existence collapse to one answer here for the same
        # reason they do for the browser: distinguishing them would say
        # whether a conversation this operator cannot see exists.
        return EXIT_REFUSED, _envelope("refused", "CAPTURE_CONVERSATION_UNAVAILABLE")

    idempotency_key = str(args.idempotency_key) if args.idempotency_key else None
    # The product's own admission limits, read the same way the API reads
    # them. An operator preparation is a real run and does not get to skip the
    # concurrency ceiling the product enforces.
    limits = BudgetConfig.from_env()
    new_run_id = uuid4()
    run_identity = RunIdentity.bind(new_run_id, "operator_capture", env=env).as_record()
    try:
        result = repository.create_message_and_run(
            conversation_id, PREPARED_RUN_CONTENT,
            {"milo_operation": OPERATOR_CAPTURE_OPERATION}, requested_by,
            idempotency_key, _preparation_fingerprint(),
            limits.max_concurrent_runs_per_user, limits.max_concurrent_runs_per_project,
            run_id=new_run_id, run_identity=run_identity)
        run = result["run"]
        created = bool(result["created"])
    except Exception:
        return EXIT_FAILED, _envelope("failed", "CAPTURE_PREPARATION_FAILED")

    run_id = UUID(str(run["id"]))
    if not created:
        # Nothing was written by the call above -- the lookup happens before
        # every insert -- and nothing is written here either, in either branch.
        if _run_is_eligible(run):
            # A genuine replay: the same run, already prepared, already owned
            # and already at rest. Idempotent, and it re-acquires nothing.
            return EXIT_OK, _envelope("prepared", "", preparation=_preparation_document(
                run_id, already_prepared=True))
        # An ordinary run -- or one already consumed, or one left mid-transition
        # -- holds this identity. Adopting it would convert somebody else's run
        # into an operator capture run and make it permanently unlaunchable.
        return EXIT_REFUSED, _envelope("refused", "CAPTURE_IDEMPOTENCY_KEY_IN_USE")

    try:
        acquired = repository.try_acquire_launch(run_id)
    except Exception:
        return EXIT_FAILED, _envelope("failed", "CAPTURE_PREPARATION_FAILED")
    if acquired is None:
        # The ordinary launch path won. Do not touch its launch state: it
        # belongs to whoever holds it.
        return EXIT_REFUSED, _envelope("refused", "CAPTURE_LAUNCH_OWNERSHIP_LOST")

    try:
        repository.set_launch_state(run_id, OPERATOR_OWNED_LAUNCH_STATE)
    except Exception:
        # The run stays at `launching`: inert, unlaunchable and uncapturable.
        return EXIT_FAILED, _envelope("failed", "CAPTURE_PREPARATION_FAILED")

    return EXIT_OK, _envelope("prepared", "", preparation=_preparation_document(
        run_id, already_prepared=False))



def _preparation_document(run_id: UUID, *, already_prepared: bool) -> dict[str, Any]:
    """The minimum an operator needs to invoke the capture, and nothing else.

    The run id only. No conversation, no project, no user, no message, no
    idempotency key, no lease material -- the capture invocation needs none of
    them, and a preparation report is not a place to widen what is printed.
    """
    return {
        "run_id": _text(run_id),
        "already_prepared": bool(already_prepared),
        "launch_owner": "operator",
        "captured": False,
    }


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

    # Ownership is re-checked on the row THE CLAIM ITSELF RETURNED, not on the
    # row read before it. `claim_run_lease` is `returning *`, so that row is
    # the run as it exists at the instant this process took the lease -- which
    # closes the read-then-claim window rather than narrowing it. The earlier
    # read is only a cheap early refusal for a wrong run id; it is not the
    # exclusivity mechanism, and nothing here depends on it still being true.
    #
    # `status` is deliberately not re-checked: the claim moved it to
    # `starting`, which is the claim working. What must still hold is that the
    # OPERATOR owns the launch, and that cannot have changed under us --
    # reaching `OPERATOR_OWNED_LAUNCH_STATE` requires winning the launch CAS,
    # and no product path can move a run back out of it.
    if not _operator_owns_launch(claimed):
        _finalize(repository, lease, document={},
                  reason_code="CAPTURE_LAUNCH_OWNERSHIP_LOST", cancelled=False)
        return EXIT_FAILED, _envelope("failed", "CAPTURE_LAUNCH_OWNERSHIP_LOST")

    supervisor = _CaptureSupervisor(repository, lease, lease_seconds=lease_seconds,
                                    interval=interval)
    observed: list[str] = []

    def record_event(event_type: str, _payload: Mapping[str, Any]) -> None:
        """The ingestor's own progress signals, kept in memory and bounded.

        Nothing is written to `run_events` here: these names are the capture
        vocabulary (`event_registry.CAPTURE_PROGRESS_EVENT_TYPES`), which is
        declared OUTSIDE the durable acceptance set on purpose, and inventing
        durable events for an operator capture is not this stage's work. Only
        the type is kept, and only to tell a replay from a first capture in
        the report.
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

    document = capture_document(outcome, replayed=CAPTURE_SNAPSHOT_REPLAYED in observed)
    _finalize(repository, lease, document=document, reason_code="", cancelled=False)
    return EXIT_OK, _envelope("succeeded", "", capture=document)


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None) -> int:
    """Refuse, plan, or capture. Returns the process exit status."""
    environment = os.environ if env is None else env
    args, extra = build_parser().parse_known_args(list(argv or []))

    reason = _refusal(args, extra, environment)
    if args.plan and not reason:
        # A plan states what an execution would construct and performs none of
        # it. It is reported as a plan whether or not the other prerequisites
        # are satisfied, and it never reads the project, the run or the flags
        # into an outcome.
        status, document = EXIT_OK, _envelope("planned", "", plan=plan_document())
    elif reason:
        status, document = EXIT_REFUSED, _envelope("refused", reason)
    elif args.prepare:
        # Preparation, and ONLY preparation. It opens no transport, sends no
        # request and captures nothing; the capture is a separate invocation
        # that must be given the run identity explicitly.
        status, document = _prepare(args, environment)
    else:
        status, document = _execute(args, environment)

    if not _emit(document, args.report_path):
        print(safe_message("CAPTURE_REPORT_NOT_WRITTEN"), file=sys.stderr)
        return EXIT_FAILED if status == EXIT_OK else status
    if document["reason_code"]:
        print(f'{document["reason_code"]}: {document["reason"]}', file=sys.stderr)
    return status


__all__ = ["CAPTURE_ENTRYPOINT", "CAPTURE_MAX_PAGES", "CAPTURE_MAX_RECORDS",
           "CAPTURE_ONLY_ARGUMENTS", "PREPARE_ONLY_ARGUMENTS",
           "CAPTURE_PAGE_LIMIT", "CAPTURE_REASONS", "EGRESS_ACKNOWLEDGEMENT",
           "ELIGIBLE_RUN_STATUS", "EXIT_FAILED", "EXIT_OK", "EXIT_REFUSED",
           "LAUNCH_ACQUIRABLE_STATES", "OPERATOR_CAPTURE_OPERATION",
           "OPERATOR_OWNED_LAUNCH_STATE", "PAID_EXECUTION_FLAG",
           "PREPARED_RUN_CONTENT", "SCHEMA_REPORT_ACKNOWLEDGEMENT", "build_parser",
           "capture_document", "configured_project_ref", "main", "plan_document",
           "safe_message"]


if __name__ == "__main__":  # pragma: no cover - the process entry point
    raise SystemExit(main(sys.argv[1:]))

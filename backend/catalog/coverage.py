"""PR-Z: the variant coverage ledger -- never pay twice for the same variant.

What it records
---------------

ONE row per (variant, level) in `catalog_variant_coverage`
(`supabase/migrations/20260927000100_catalog_variant_coverage.sql`): what the
latest run that finished `completed` or `partial_success` established about
that register variant at that level of work, the snapshot it read, and the
content hash of the register row it read.

======================  ====================================================
``enriched``            at least one VERIFIED field for the variant
``unresolved_ambiguous``  the register matched the variant's identity on
                        more than one row
``unresolved_not_found``  the register matched no row
``pending``             resolved, but no field is verified yet (awaiting
                        review)
``failed``              the run was handed the variant and settled nothing
======================  ====================================================

Where a status comes from, and where it never comes from
--------------------------------------------------------

ONLY from durable run data: the run's final payload (`runs.output`, whose
``vehicles`` and ``unresolved_groups`` the vehicle-result assembler built from
the typed ``candidate_outcomes``, the claims and their current verdicts) and
the run's own preparation record (the queue it was handed). Never from model
text, never from a browser. The database derives the variant's identity key,
its content hash and the vocabulary version from the stored candidate row
itself; this module only names WHICH candidate of the run's pinned snapshot
earned WHICH status.

The ledger is an INDEX, not evidence: every status in it can be rebuilt from
run history (`backfill`), so a ledger write that fails never changes a run's
outcome, and source values are never touched.

The variant identity key
------------------------

`variant_identity_key` is the SHA-256 of `variant_identity_text`: the marque
(`tozar`), commercial model (`kinuy_mishari`), model years (`shnat_yitzur`),
official model code (`degem_nm`), trim (`ramat_gimur`) and every identity
dimension, exactly as the reviewed normalization stored them on the candidate
row -- never a model's value. NOT the register's `_id`: the register reuses
ids across captures, so an id names a row of ONE snapshot and nothing more.
Two rows that state the same identity (a duplicate-identity group) share one
key, which is exactly right: they are one variant the register cannot tell
apart. `public.catalog_variant_identity_key` is the database's copy of the
same rule, and tests/test_migrations_postgres.py proves the two agree.

Pure except `record_run_coverage` and `backfill`, which take a repository.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from backend.catalog.digest import catalog_payload_digest
from backend.catalog.government.vocabulary import (GOVERNMENT_RECORD_ID_FIELD,
                                                   VOCABULARY_VERSION)

#: The contract the identity text is written under. Changing the text changes
#: every key, so it is versioned like every other durable rendering here.
VARIANT_IDENTITY_CONTRACT = "milo-variant-identity/1"

#: The levels of work a variant can be covered at. ONE today: the Government
#: register resolution a Mapping Plan batch run performs. A later level (a web
#: enrichment pass) is a new value here and in the database's CHECK.
LEVEL_REGISTER = "register"
COVERAGE_LEVELS = (LEVEL_REGISTER,)
#: The level a Mapping Plan batch works at.
BATCH_COVERAGE_LEVEL = LEVEL_REGISTER

ENRICHED = "enriched"
UNRESOLVED_AMBIGUOUS = "unresolved_ambiguous"
UNRESOLVED_NOT_FOUND = "unresolved_not_found"
FAILED = "failed"
PENDING = "pending"
COVERAGE_STATUSES = (ENRICHED, UNRESOLVED_AMBIGUOUS, UNRESOLVED_NOT_FOUND, FAILED, PENDING)
UNRESOLVED_STATUSES = frozenset({UNRESOLVED_AMBIGUOUS, UNRESOLVED_NOT_FOUND})

#: How strongly a status settles a variant. For the SAME content and
#: vocabulary a weaker status never replaces a stronger one (a later failed
#: attempt does not erase an enrichment); a changed row replaces whatever was
#: recorded. Mirrored by `public.catalog_variant_coverage_rank`.
STATUS_RANK: Mapping[str, int] = {ENRICHED: 4, UNRESOLVED_AMBIGUOUS: 3, UNRESOLVED_NOT_FOUND: 3,
                                  PENDING: 2, FAILED: 1}

#: What filtering decides for one candidate. `queue` keeps it.
DECISION_QUEUE = "queue"
EXCLUDED_ALREADY_ENRICHED = "excluded_already_enriched"
EXCLUDED_KNOWN_UNRESOLVED = "excluded_known_unresolved"
COVERAGE_DECISIONS = (DECISION_QUEUE, EXCLUDED_ALREADY_ENRICHED, EXCLUDED_KNOWN_UNRESOLVED)

#: The most entries one ledger write carries. A batch run is handed at most
#: `MAX_PROMOTIONS_PER_RUN` candidates; a duplicate group can name a few rows
#: more. The database holds the same bound.
MAX_COVERAGE_ENTRIES = 200

#: The most candidate rows ONE identity read looks at while mapping a result's
#: register rows back to candidates -- the same bounded page the Government
#: Tool's reads use. An identity stating more rows than this is left unmapped.
MAX_IDENTITY_SCAN_ROWS = 200

#: The register fields whose value never describes the variant: the
#: datastore's own row id, which the register reuses across captures.
VOLATILE_PAYLOAD_FIELDS = frozenset({GOVERNMENT_RECORD_ID_FIELD})


# ---------------------------------------------------------------------------
# Z1: the variant identity key, and the row's content hash.
# ---------------------------------------------------------------------------

def _token(value: str | None) -> str:
    """Length-prefixed, so no value can imitate a separator."""
    return "-" if value is None else f"{len(value)}:{value}"


def _year(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("a model year is a whole number")
    return str(value)


def variant_identity_text(manufacturer: str, commercial_model: str,
                          model_year_start: int | None, model_year_end: int | None,
                          official_model_code: str | None, trim: str | None,
                          identity_dimensions: Mapping[str, str] | None) -> str:
    """The ONE canonical rendering the key is taken over.

    ``milo-variant-identity/1`` then, separated by ``|``, each identity column
    as ``<length>:<text>`` (``-`` when absent), then each identity dimension in
    code-point key order as ``<length>:<name>=<length>:<value>``.
    """
    parts = [VARIANT_IDENTITY_CONTRACT]
    for value in (manufacturer, commercial_model, _year(model_year_start),
                  _year(model_year_end), official_model_code, trim):
        if value is not None and not isinstance(value, str):
            raise ValueError("an identity column is text")
        parts.append(_token(value))
    dimensions = dict(identity_dimensions or {})
    for name in sorted(dimensions):
        value = dimensions[name]
        if not isinstance(name, str) or not isinstance(value, str):
            raise ValueError("an identity dimension is text")
        parts.append(f"{_token(name)}={_token(value)}")
    return "|".join(parts)


def variant_identity_key(manufacturer: str, commercial_model: str,
                         model_year_start: int | None, model_year_end: int | None,
                         official_model_code: str | None, trim: str | None,
                         identity_dimensions: Mapping[str, str] | None) -> str:
    """SHA-256 (lowercase hex) of `variant_identity_text`."""
    text = variant_identity_text(manufacturer, commercial_model, model_year_start,
                                 model_year_end, official_model_code, trim,
                                 identity_dimensions)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def candidate_identity_key(candidate: Mapping[str, Any]) -> str:
    """The key of one stored candidate row (or anything shaped like one)."""
    return variant_identity_key(
        candidate["manufacturer"], candidate["commercial_model"],
        candidate.get("model_year_start"), candidate.get("model_year_end"),
        candidate.get("official_model_code"), candidate.get("trim"),
        candidate.get("identity_dimensions") or {})


def variant_content_sha256(payload: Mapping[str, Any]) -> str:
    """The content hash of one stored register row, minus its volatile `_id`.

    Storage-local, like `catalog_payload_digest`: the in-memory repository
    hashes its canonical text, PostgreSQL (`catalog_variant_content_sha256`)
    hashes `(payload - '_id')::text`. The ledger only ever compares a hash with
    one the SAME storage computed, so the two never need to agree.
    """
    return catalog_payload_digest({key: value for key, value in payload.items()
                                   if key not in VOLATILE_PAYLOAD_FIELDS})


# ---------------------------------------------------------------------------
# Z3: the ONE filtering rule. `public.catalog_variant_coverage_decision` is
# the database's copy; the postgres suite proves they agree case by case.
# ---------------------------------------------------------------------------

def coverage_decision(status: str | None, recorded_content: str | None,
                      recorded_vocabulary: str | None, content: str, *,
                      include_unresolved: bool = False,
                      vocabulary: str | None = None) -> str:
    """Queue a candidate, or say why it is left out.

    *   ``enriched`` with the SAME content hash: left out (already enriched);
    *   ``unresolved_*``: left out unless the row's content hash or the
        vocabulary version changed since, or the plan revision sets
        ``include_unresolved``;
    *   anything else -- ``failed``, ``pending``, a changed row, or no
        ledger row at all: queued.
    """
    vocabulary = VOCABULARY_VERSION if vocabulary is None else vocabulary
    if status == ENRICHED and recorded_content == content:
        return EXCLUDED_ALREADY_ENRICHED
    if status in UNRESOLVED_STATUSES and not include_unresolved \
            and recorded_content == content and recorded_vocabulary == vocabulary:
        return EXCLUDED_KNOWN_UNRESOLVED
    return DECISION_QUEUE


# ---------------------------------------------------------------------------
# Z2: which candidate of the run's snapshot earned which status.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CoverageDerivation:
    """The entries one run's durable data supports, and what could not be mapped."""

    entries: tuple[Mapping[str, str], ...]
    #: Register rows the result names that no bounded read of the pinned
    #: snapshot could map back to a candidate (counted, never guessed at).
    unmapped_records: int = 0
    counts: Mapping[str, int] = field(default_factory=dict)


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _identity_filters(identity: Mapping[str, Any]) -> tuple[Any, ...] | None:
    manufacturer = _text(identity.get("manufacturer"))
    model = _text(identity.get("commercial_model"))
    year = identity.get("model_year")
    if manufacturer is None or model is None or isinstance(year, bool) \
            or not isinstance(year, int):
        return None
    return (manufacturer, model, year, _text(identity.get("official_model_code")),
            _text(identity.get("trim")))


def _stronger(current: str | None, status: str) -> str:
    return status if current is None or STATUS_RANK[status] > STATUS_RANK[current] else current


def derive_coverage(result: Mapping[str, Any], queue: Sequence[Any], query: Any
                    ) -> CoverageDerivation:
    """The ledger entries one finished run's durable data supports.

    `result` is the run's final payload, `queue` the work items the run was
    handed (its preparation record's queue), `query` a bounded reader pinned
    to that preparation's snapshot (`GovernmentCatalogQuery`).

    *   a ``vehicles`` entry -- a row a task RESOLVED -- is ``enriched`` when
        at least one of its fields carries the ``verified`` verdict, else
        ``pending``;
    *   an ``unresolved_groups`` entry names its rows (ambiguous) or, for a
        not-found answer, only the identity asked: the rows go by id, the
        identity goes to the handed queue items that state exactly it;
    *   every handed queue item nothing above settled is ``failed``.

    A row is mapped back to its candidate by ONE bounded page per distinct
    identity (the read `resolve_variant` answers from), never a scan of the
    snapshot. The strongest status wins when a candidate earns two.
    """
    by_record: dict[str, str] = {}
    identity_of: dict[str, Mapping[str, Any]] = {}
    not_found: list[Mapping[str, Any]] = []
    for vehicle in result.get("vehicles") or []:
        if not isinstance(vehicle, Mapping) or _text(vehicle.get("vehicle_key")) is None:
            continue
        fields = vehicle.get("fields") if isinstance(vehicle.get("fields"), Mapping) else {}
        verified = any(isinstance(item, Mapping) and item.get("verdict") == "verified"
                       for item in fields.values())
        record = str(vehicle["vehicle_key"])
        by_record[record] = _stronger(by_record.get(record), ENRICHED if verified else PENDING)
        identity = vehicle.get("identity")
        identity_of.setdefault(record, identity if isinstance(identity, Mapping) else {})
    for group in result.get("unresolved_groups") or []:
        if not isinstance(group, Mapping) or group.get("outcome") not in UNRESOLVED_STATUSES:
            continue
        identity = group.get("candidate") if isinstance(group.get("candidate"), Mapping) else {}
        records = [str(value) for value in group.get("record_ids") or [] if _text(value)]
        if not records:
            not_found.append(identity)
            continue
        for record in records:
            by_record[record] = _stronger(by_record.get(record), str(group["outcome"]))
            identity_of.setdefault(record, identity)

    by_candidate: dict[str, str] = {}
    unmapped = 0
    wanted: dict[tuple[Any, ...], set[str]] = {}
    for record, identity in identity_of.items():
        filters = _identity_filters(identity)
        if filters is None:
            unmapped += 1
            continue
        wanted.setdefault(filters, set()).add(record)
    for filters, records in sorted(wanted.items(), key=lambda item: repr(item[0])):
        found = _rows_by_record(query, filters, records)
        for record in sorted(records):
            row = found.get(record)
            if row is None:
                unmapped += 1
                continue
            candidate = str(row.candidate_id)
            by_candidate[candidate] = _stronger(by_candidate.get(candidate), by_record[record])

    for identity in not_found:
        filters = _identity_filters(identity)
        if filters is None:
            continue
        for item in queue:
            if (item.manufacturer, item.commercial_model, item.model_year_start,
                    item.official_model_code, item.trim) == filters:
                by_candidate[item.candidate_id] = _stronger(by_candidate.get(item.candidate_id),
                                                            UNRESOLVED_NOT_FOUND)
    for item in queue:
        by_candidate.setdefault(str(item.candidate_id), FAILED)

    entries = tuple({"candidate_id": candidate, "status": status}
                    for candidate, status in sorted(by_candidate.items()))
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    return CoverageDerivation(entries=entries, unmapped_records=unmapped,
                              counts=dict(sorted(counts.items())))


def _rows_by_record(query: Any, filters: tuple[Any, ...], records: set[str]) -> dict[str, Any]:
    """The candidate rows of `records`, read by the identity they were asked as.

    One bounded page stating every filter; when a row is not on it (the
    PR-V separator-insensitive retry resolved a code spelled differently from
    the one asked), one more bounded page without the code filter.
    """
    manufacturer, model, year, code, trim = filters
    found: dict[str, Any] = {}
    page = query.list_variants(manufacturer=manufacturer, commercial_model=model,
                               model_year=year, official_model_code=code, trim=trim,
                               limit=MAX_IDENTITY_SCAN_ROWS, offset=0)
    found.update({str(row.upstream_record_id): row for row in page.items
                  if str(row.upstream_record_id) in records})
    if set(found) != records and code is not None:
        wider = query.list_variants(manufacturer=manufacturer, commercial_model=model,
                                    model_year=year, trim=trim,
                                    limit=MAX_IDENTITY_SCAN_ROWS, offset=0)
        found.update({str(row.upstream_record_id): row for row in wider.items
                      if str(row.upstream_record_id) in records
                      and str(row.upstream_record_id) not in found})
    return found


def _pinned_query(repository: Any, snapshot_key: str) -> Any:
    from backend.catalog.government import source as src
    from backend.catalog.government.query import GovernmentCatalogQuery
    return GovernmentCatalogQuery(repository, resource_id=src.WLTP_RESOURCE_ID,
                                  snapshot_key=snapshot_key, allow_incomplete=False)


def _log(message: str) -> None:
    print(message, flush=True)


def record_run_coverage(repository: Any, run_id: Any, preparation: Any,
                        result: Mapping[str, Any], lease: Mapping[str, Any], *,
                        level: str = BATCH_COVERAGE_LEVEL,
                        log: Callable[[str], None] = _log) -> dict[str, Any] | None:
    """Write the ledger rows ONE finished run earned, from the finalize path.

    Called by the worker right after the run's terminal state is durable as
    ``completed`` or ``partial_success``, under the same lease identity that
    finalized it (`record_catalog_variant_coverage_guarded` accepts nothing
    else). NEVER raises: the ledger is rebuildable from run history, so a
    failure here is logged -- a static line, the exception CLASS only -- and
    the run's outcome is exactly what it already is.
    """
    write = getattr(repository, "record_catalog_variant_coverage", None)
    if not callable(write) or preparation is None or not isinstance(result, Mapping):
        return None
    try:
        derived = derive_coverage(result, tuple(preparation.queue),
                                  _pinned_query(repository, preparation.snapshot_key))
        if not derived.entries:
            return None
        answer = write(run_id, level, [dict(entry) for entry in derived.entries],
                       worker_id=lease.get("worker_id"), attempt=lease.get("attempt"),
                       lease_token=lease.get("lease_token"))
    except Exception as exc:
        log(f"catalog coverage ledger write failed: run_id={run_id} "
            f"exception_class={type(exc).__name__}")
        return None
    if derived.unmapped_records:
        log(f"catalog coverage ledger: run_id={run_id} "
            f"unmapped_records={derived.unmapped_records}")
    return dict(answer) if isinstance(answer, Mapping) else None


# ---------------------------------------------------------------------------
# The operator backfill: rebuild the ledger from run history.
# ---------------------------------------------------------------------------

#: The most runs one listing page returns.
MAX_BACKFILL_PAGE = 50

#: The static reasons a run is skipped by the backfill, each counted.
BACKFILL_SKIP_REASONS = ("NO_OUTPUT", "NO_PREPARATION", "PREPARATION_UNREADABLE",
                         "SNAPSHOT_UNAVAILABLE", "NOTHING_TO_RECORD", "WRITE_REFUSED")


@dataclass
class BackfillReport:
    runs_seen: int = 0
    runs_recorded: int = 0
    entries: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    by_run: dict[str, dict[str, int]] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def as_record(self) -> dict[str, Any]:
        return {"runs_seen": self.runs_seen, "runs_recorded": self.runs_recorded,
                "entries": self.entries, "skipped": dict(sorted(self.skipped.items())),
                "by_run": {run: dict(counts) for run, counts in sorted(self.by_run.items())}}


def _preparation_queue(checkpoint: Any) -> tuple[str, tuple[Any, ...]] | None:
    """The snapshot key and handed queue a run's latest checkpoint records."""
    from backend.catalog.government.preparation import (ARTIFACT_SCHEMA, GovernmentWorkItem,
                                                        prepared_artifact)
    record = prepared_artifact(checkpoint)
    if record is None:
        return None
    if record.get("schema") != ARTIFACT_SCHEMA or not _text(record.get("snapshot_key")) \
            or not isinstance(record.get("queue"), list):
        raise ValueError("unreadable preparation record")
    return str(record["snapshot_key"]), tuple(GovernmentWorkItem.from_record(item)
                                              for item in record["queue"])


def backfill_run(repository: Any, run: Mapping[str, Any], report: BackfillReport, *,
                 level: str = BATCH_COVERAGE_LEVEL, dry_run: bool = False) -> None:
    """Rebuild the ledger rows of ONE finished batch run. Idempotent."""
    run_id = str(run["run_id"])
    report.runs_seen += 1
    output = repository.get_run(run_id).get("output")
    if not isinstance(output, Mapping):
        report.skip("NO_OUTPUT")
        return
    try:
        prepared = _preparation_queue(repository.latest_checkpoint(run_id))
    except Exception:
        report.skip("PREPARATION_UNREADABLE")
        return
    if prepared is None:
        report.skip("NO_PREPARATION")
        return
    snapshot_key, queue = prepared
    try:
        derived = derive_coverage(output, queue, _pinned_query(repository, snapshot_key))
    except Exception:
        report.skip("SNAPSHOT_UNAVAILABLE")
        return
    if not derived.entries:
        report.skip("NOTHING_TO_RECORD")
        return
    report.by_run[run_id] = dict(derived.counts)
    if not dry_run:
        try:
            repository.rebuild_catalog_variant_coverage(
                run_id, level, [dict(entry) for entry in derived.entries])
        except Exception:
            report.skip("WRITE_REFUSED")
            return
    report.runs_recorded += 1
    report.entries += len(derived.entries)


def backfill(repository: Any, *, level: str = BATCH_COVERAGE_LEVEL,
             page_size: int = MAX_BACKFILL_PAGE, dry_run: bool = False,
             run_ids: Iterable[str] | None = None) -> BackfillReport:
    """Rebuild the ledger from every finished batch run, oldest first.

    Keyset pages of at most `MAX_BACKFILL_PAGE` runs
    (`catalog_variant_coverage_runs`), one run's output and latest
    checkpoint at a time, and bounded snapshot reads per run: never the whole
    ledger, never a whole snapshot. Replaying the runs in the order they
    finished leaves the ledger exactly as the finalize path would have, so a
    second backfill changes nothing.
    """
    report = BackfillReport()
    if run_ids is not None:
        for run_id in run_ids:
            backfill_run(repository, {"run_id": run_id}, report, level=level, dry_run=dry_run)
        return report
    limit = max(1, min(int(page_size), MAX_BACKFILL_PAGE))
    after: tuple[Any, Any] = (None, None)
    while True:
        page = repository.catalog_variant_coverage_runs(after_finished_at=after[0],
                                                        after_run_id=after[1], limit=limit)
        for run in page:
            backfill_run(repository, run, report, level=level, dry_run=dry_run)
        if len(page) < limit:
            return report
        after = (page[-1]["finished_at"], page[-1]["run_id"])


__all__ = ["BACKFILL_SKIP_REASONS", "BATCH_COVERAGE_LEVEL", "BackfillReport",
           "COVERAGE_DECISIONS", "COVERAGE_LEVELS", "COVERAGE_STATUSES", "CoverageDerivation",
           "DECISION_QUEUE", "ENRICHED", "EXCLUDED_ALREADY_ENRICHED",
           "EXCLUDED_KNOWN_UNRESOLVED", "FAILED", "LEVEL_REGISTER", "MAX_BACKFILL_PAGE",
           "MAX_COVERAGE_ENTRIES", "MAX_IDENTITY_SCAN_ROWS", "PENDING", "STATUS_RANK",
           "UNRESOLVED_AMBIGUOUS", "UNRESOLVED_NOT_FOUND", "UNRESOLVED_STATUSES",
           "VARIANT_IDENTITY_CONTRACT", "VOLATILE_PAYLOAD_FIELDS", "backfill", "backfill_run",
           "candidate_identity_key", "coverage_decision", "derive_coverage",
           "record_run_coverage", "variant_content_sha256", "variant_identity_key",
           "variant_identity_text"]

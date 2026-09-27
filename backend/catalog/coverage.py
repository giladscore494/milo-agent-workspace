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

The paid-work claim (`catalog_variant_reservations`)
----------------------------------------------------

The ledger alone cannot stop two plans paying for one variant at once, nor
stop a second plan paying while a FINISHED run's settlement is still missing.
So before its first paid call a run CLAIMS every variant it is handed
(`acquire_catalog_variant_reservations_guarded`, lease-guarded and atomic on
the (identity key, level) row). A claim blocks every other run while its owner
is running, and -- the crash window -- while its owner has FINISHED
`completed` / `partial_success` and is not yet settled. Settlement (the ledger
upsert of `derive_coverage`'s entries) releases the claims in the same
transaction. It happens on the finalize path; when that write fails or the
worker dies, it happens AUTOMATICALLY the next time anything needs the
variant: the run preparation that meets a `settlement_pending` claim, and the
queue build's bounded sweep (`reconcile_pending_settlements`), both settle the
finished run from its own durable output with `settle_run`. The operator
backfill stays a repair tool only. A claim whose owner ended without a result,
or whose lease has been expired longer than the takeover grace, is dead and is
taken over atomically by the next claimant.

The variant identity key
------------------------

`variant_identity_key` is the SHA-256 of `variant_identity_text`: the marque
(`tozar`), commercial model (`kinuy_mishari`), model years (`shnat_yitzur`),
official model code (`degem_nm`), trim (`ramat_gimur`), exactly as the
reviewed normalization stored them on the candidate row; then (contract
``milo-variant-identity/2``) the Government's own registration identifiers
-- manufacturer code (`tozeret_cd`), model code (`degem_cd`) and vehicle
type code (`sug_degem`) -- VERBATIM from the stored raw payload, exactly as
PostgreSQL's ``payload->>'field'`` renders them, no normalization; then every
identity dimension. Never a model's value. NOT the register's `_id`: the
register reuses ids across captures, so an id names a row of ONE snapshot and
nothing more.

Version 1 left the registration identifiers out, and production showed what
that costs: Toyota 2018+ has 835 groups of rows that shared a v1 key while
stating DIFFERENT vehicles (different `degem_cd`, some a different
`sug_degem`), so one enrichment could exclude another vehicle and two runs
could overwrite each other's row forever. Under v2 every one of those rows has
its own key.

A duplicate is ONLY rows whose content minus `_id` is identical: they share a
key and are one variant the register cannot tell apart. Rows that share a key
while their content differs are a KEY COLLISION (`KEY_COLLISION`): the
ledger never picks one of them -- the key is recorded ``failed`` with that
reason (`settle_keys`), and the run is untouched.
`public.catalog_variant_identity_key` is the database's copy of the same
rule, and tests/test_migrations_postgres.py proves the two agree.

Pure except `record_run_coverage` and `backfill`, which take a repository.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from backend.catalog.digest import catalog_payload_digest
# The register-code rendering lives in a leaf module so the Government query
# layer can share it without importing this one (which imports the package).
from backend.catalog.register_codes import (REGISTER_IDENTITY_FIELDS, register_code,
                                            register_codes)
from backend.catalog.government.vocabulary import (GOVERNMENT_RECORD_ID_FIELD,
                                                   VOCABULARY_VERSION)

#: The contract the identity text is written under. Changing the text changes
#: every key, so it is versioned like every other durable rendering here.
VARIANT_IDENTITY_CONTRACT = "milo-variant-identity/2"

#: The ledger reason of a key whose rows state DIFFERENT content: recorded as
#: ``failed``, never settled by picking one of them.
KEY_COLLISION = "CATALOG_COVERAGE_KEY_COLLISION"

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

#: What claiming one candidate answers (`acquire_catalog_variant_reservations_
#: guarded`): the run owns the paid work on it, the ledger already settles it,
#: another live run owns it, or another run FINISHED it and awaits settlement.
RESERVED = "reserved"
RESERVED_BY_OTHER = "reserved_by_other"
SETTLEMENT_PENDING = "settlement_pending"
CLAIM_DECISIONS = (RESERVED, EXCLUDED_ALREADY_ENRICHED, EXCLUDED_KNOWN_UNRESOLVED,
                   RESERVED_BY_OTHER, SETTLEMENT_PENDING)
#: How long an owner's lease must have been expired before its claim is dead
#: and may be taken over. Mirrors `catalog_variant_reservation_grace()`.
RESERVATION_TAKEOVER_GRACE_SECONDS = 15 * 60
#: The most runs one reconciliation pass settles.
MAX_RECONCILE_RUNS = 50

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
                          register_manufacturer_code: str | None,
                          register_model_code: str | None,
                          vehicle_type_code: str | None,
                          identity_dimensions: Mapping[str, str] | None) -> str:
    """The ONE canonical rendering the key is taken over.

    ``milo-variant-identity/2`` then, separated by ``|``, each identity column
    -- manufacturer, commercial model, first and last model year, official
    model code, trim, register manufacturer code, register model code, vehicle
    type code -- as ``<length>:<text>`` (``-`` when absent), then each identity
    dimension in code-point key order as ``<length>:<name>=<length>:<value>``.
    """
    parts = [VARIANT_IDENTITY_CONTRACT]
    for value in (manufacturer, commercial_model, _year(model_year_start),
                  _year(model_year_end), official_model_code, trim,
                  register_manufacturer_code, register_model_code, vehicle_type_code):
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
                         register_manufacturer_code: str | None,
                         register_model_code: str | None,
                         vehicle_type_code: str | None,
                         identity_dimensions: Mapping[str, str] | None) -> str:
    """SHA-256 (lowercase hex) of `variant_identity_text`."""
    text = variant_identity_text(manufacturer, commercial_model, model_year_start,
                                 model_year_end, official_model_code, trim,
                                 register_manufacturer_code, register_model_code,
                                 vehicle_type_code, identity_dimensions)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def candidate_identity_key(candidate: Any, payload: Mapping[str, Any]) -> str:
    """The key of one stored candidate row and the raw payload it was normalized from.

    `candidate` is a stored candidate row (a mapping) or anything with the
    same attributes (`CandidateVariantRow`).
    """
    def read(name: str, default: Any = None) -> Any:
        if isinstance(candidate, Mapping):
            return candidate.get(name, default)
        return getattr(candidate, name, default)

    return variant_identity_key(
        read("manufacturer"), read("commercial_model"),
        read("model_year_start"), read("model_year_end"),
        read("official_model_code"), read("trim"), *register_codes(payload),
        dict(read("identity_dimensions") or {}))


def variant_content_sha256(payload: Mapping[str, Any]) -> str:
    """The content hash of one stored register row, minus its volatile `_id`.

    Storage-local, like `catalog_payload_digest`: the in-memory repository
    hashes its canonical text, PostgreSQL (`catalog_variant_content_sha256`)
    hashes `(payload - '_id')::text`. The ledger only ever compares a hash with
    one the SAME storage computed, so the two never need to agree.
    """
    return catalog_payload_digest({key: value for key, value in payload.items()
                                   if key not in VOLATILE_PAYLOAD_FIELDS})


def collision_content_sha256(contents: Iterable[str]) -> str:
    """The content a KEY COLLISION is recorded with: every distinct content hash
    its rows state, sorted, joined by ``,``, hashed. Mirrored by
    `catalog_variant_coverage_apply`; like every content hash, storage-local."""
    joined = ",".join(sorted(set(contents)))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class KeySettlement:
    """What ONE run's entries settle for one identity key."""

    status: str
    content_sha256: str
    #: None, or `KEY_COLLISION`.
    reason_code: str | None = None


def settle_keys(entries: Iterable[tuple[str, str, str]],
                snapshot_contents: Mapping[str, Iterable[str]]
                ) -> dict[str, KeySettlement]:
    """Per identity key, what one run's entries settle.

    `entries` holds (identity key, content hash, status) per entry;
    `snapshot_contents` the content hash of EVERY row of the run's snapshot
    that states each key (the entries' own rows included).

    *   every row of the key states the SAME content (one row, or a true
        duplicate group): the strongest status any entry earned, with that
        content;
    *   the key's rows state DIFFERENT content: a KEY COLLISION -- never one
        of them picked -- ``failed`` with `KEY_COLLISION` and
        `collision_content_sha256` over every content involved. Nothing is
        refused: the ledger write itself succeeds and the run is untouched.
    """
    statuses: dict[str, str] = {}
    contents: dict[str, set[str]] = {}
    for key, content, status in entries:
        contents.setdefault(key, set()).add(content)
        current = statuses.get(key)
        if current is None or (-STATUS_RANK[status], status.encode()) \
                < (-STATUS_RANK[current], current.encode()):
            statuses[key] = status
    settled: dict[str, KeySettlement] = {}
    for key, status in statuses.items():
        seen = contents[key] | set(snapshot_contents.get(key, ()))
        if len(seen) > 1:
            settled[key] = KeySettlement(FAILED, collision_content_sha256(seen), KEY_COLLISION)
        else:
            (content,) = seen
            settled[key] = KeySettlement(status, content)
    return settled


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
    else). The same write releases the run's paid-work claims -- with no
    entries at all, too, so a finished run never keeps a claim it earned
    nothing on. NEVER raises: a failure here is logged -- a static line, the
    exception CLASS only -- and the run's outcome is exactly what it already
    is. The claims then keep blocking as `settlement_pending` until
    `settle_run` settles the run from its durable output.
    """
    write = getattr(repository, "record_catalog_variant_coverage", None)
    if not callable(write) or preparation is None or not isinstance(result, Mapping):
        return None
    try:
        derived = derive_coverage(result, tuple(preparation.queue),
                                  _pinned_query(repository, preparation.snapshot_key))
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
    collisions = answer.get("collisions") if isinstance(answer, Mapping) else None
    if collisions:
        log(f"catalog coverage ledger: run_id={run_id} reason_code={KEY_COLLISION} "
            f"keys={collisions}")
    return dict(answer) if isinstance(answer, Mapping) else None


# ---------------------------------------------------------------------------
# The operator backfill: rebuild the ledger from run history.
# ---------------------------------------------------------------------------

#: The most runs one listing page returns.
MAX_BACKFILL_PAGE = 50

#: The static reasons a run is skipped by the backfill, each counted.
BACKFILL_SKIP_REASONS = ("NO_OUTPUT", "NO_PREPARATION", "PREPARATION_UNREADABLE",
                         "SNAPSHOT_UNAVAILABLE", "WRITE_REFUSED")


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


def _checkpoint_evidence(checkpoint: Any) -> tuple[list[Any], list[Any]]:
    """The evidence and verdicts a run's LAST engine checkpoint carries.

    The same `swarm_state` the engine finalized from: its evidence references,
    and its verifier state under the same grounding rule the engine applies
    (`SwarmV2Engine._verdicts_from_state`: a state written under another
    grounding version contributes no verdict, so its claims stay unverified).
    Anything unreadable is left out, never repaired -- a claim with no
    readable verdict is simply not verified.
    """
    from backend.engines.swarm_v2.contracts import EvidenceReference, VerificationVerdict
    from backend.engines.swarm_v2.grounding import VERIFIER_GROUNDING_VERSION

    artifacts = checkpoint.get("artifacts") if isinstance(checkpoint, Mapping) else None
    state = artifacts.get("swarm_state") if isinstance(artifacts, Mapping) else None
    if not isinstance(state, Mapping):
        return [], []
    evidence = []
    for raw in state.get("evidence_references") or []:
        try:
            evidence.append(EvidenceReference.model_validate(raw))
        except Exception:
            continue
    verdicts = []
    stored = state.get("verifier_state")
    if state.get("verifier_grounding_version") == VERIFIER_GROUNDING_VERSION \
            and isinstance(stored, Mapping):
        claims = {item.claim_id for item in evidence}
        for claim_id, raw in sorted(stored.items()):
            if claim_id not in claims:
                continue
            try:
                verdicts.append(VerificationVerdict.model_validate(raw))
            except Exception:
                continue
    return evidence, verdicts


def assembled_output(output: Mapping[str, Any], checkpoint: Any) -> Mapping[str, Any]:
    """E'-6: a pre-PR-2 output, given the vehicle view the finalize path adds today.

    Runs that finished before PR-2 (6825eb96 among them) stored
    ``candidate_outcomes`` but no ``vehicles`` / ``unresolved_groups`` -- the
    only keys `derive_coverage` reads -- so every item they were handed read as
    ``failed``. This builds those two keys with the SAME
    `VehicleCatalogResultAssembler` the finalize path (`FinalBuilder`) runs,
    from the SAME inputs it had: the typed outcomes stored in the output, the
    evidence and verdicts of the run's last engine checkpoint, and the output's
    own task codes. No new rule: pure assembly. An output that already carries
    either key, or has no outcomes, is returned unchanged.
    """
    if "vehicles" in output or "unresolved_groups" in output:
        return output
    outcomes = output.get("candidate_outcomes")
    if not isinstance(outcomes, list) or not outcomes:
        return output
    from backend.catalog.result.assembler import VehicleCatalogResultAssembler

    evidence, verdicts = _checkpoint_evidence(checkpoint)
    codes = [item for item in output.get("needs_review") or []
             if isinstance(item, Mapping) and _text(item.get("task_id")) and _text(item.get("code"))]
    view = VehicleCatalogResultAssembler().assemble(
        evidence=evidence, verdicts=verdicts,
        candidate_outcomes=[item for item in outcomes if isinstance(item, Mapping)],
        coverage_gaps=codes)
    return {**output, "vehicles": view["vehicles"], "unresolved_groups": view["unresolved_groups"]}


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
        checkpoint = repository.latest_checkpoint(run_id)
        prepared = _preparation_queue(checkpoint)
    except Exception:
        report.skip("PREPARATION_UNREADABLE")
        return
    if prepared is None:
        report.skip("NO_PREPARATION")
        return
    snapshot_key, queue = prepared
    try:
        # E'-6: an output written before PR-2 gets its vehicle view from the
        # finalize path's own assembler first (unchanged when it has one).
        output = assembled_output(output, checkpoint)
        derived = derive_coverage(output, queue, _pinned_query(repository, snapshot_key))
    except Exception:
        report.skip("SNAPSHOT_UNAVAILABLE")
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


def settle_run(repository: Any, run_id: Any, *, level: str = BATCH_COVERAGE_LEVEL,
               log: Callable[[str], None] = _log) -> bool:
    """Settle ONE finished run from its own durable data. Never raises.

    The same derivation and the same write as the operator backfill
    (`rebuild_catalog_variant_coverage`), which upserts the ledger and releases
    the run's claims in one transaction; idempotent, so a crash before or after
    it, or two callers at once, converge on the same state. The database
    refuses a run that did not finish `completed` / `partial_success`. False
    (logged) when the run could not be settled now -- its claims then keep
    blocking, which is the safe side.
    """
    report = BackfillReport()
    try:
        backfill_run(repository, {"run_id": str(run_id)}, report, level=level)
    except Exception as exc:
        log(f"catalog coverage settlement failed: run_id={run_id} "
            f"exception_class={type(exc).__name__}")
        return False
    if report.runs_recorded != 1:
        log(f"catalog coverage settlement deferred: run_id={run_id} "
            f"reason={','.join(sorted(report.skipped)) or 'UNKNOWN'}")
        return False
    return True


def reconcile_pending_settlements(repository: Any, *, run_ids: Iterable[Any] | None = None,
                                  level: str = BATCH_COVERAGE_LEVEL,
                                  limit: int = MAX_RECONCILE_RUNS,
                                  log: Callable[[str], None] = _log) -> dict[str, int]:
    """Settle finished runs whose claims still await settlement. Never raises.

    With `run_ids`: exactly those runs (the owners a claim answered
    `settlement_pending` for). Without: ONE bounded page of them
    (`catalog_variant_reservations_settling`, oldest first). This is the
    AUTOMATIC recovery of the finalize path's ledger write: no operator runs
    it.
    """
    bound = max(1, min(int(limit), MAX_RECONCILE_RUNS))
    if run_ids is None:
        listing = getattr(repository, "catalog_variant_reservations_settling", None)
        if not callable(listing):
            return {"settled": 0, "deferred": 0}
        try:
            run_ids = [row["run_id"] for row in listing(limit=bound)]
        except Exception as exc:
            log(f"catalog coverage reconciliation listing failed: "
                f"exception_class={type(exc).__name__}")
            return {"settled": 0, "deferred": 0}
    wanted = list(dict.fromkeys(str(run) for run in run_ids))[:bound]
    settled = sum(1 for run in wanted if settle_run(repository, run, level=level, log=log))
    return {"settled": settled, "deferred": len(wanted) - settled}


__all__ = ["BACKFILL_SKIP_REASONS", "CLAIM_DECISIONS", "MAX_RECONCILE_RUNS",
           "RESERVATION_TAKEOVER_GRACE_SECONDS", "RESERVED", "RESERVED_BY_OTHER",
           "SETTLEMENT_PENDING", "reconcile_pending_settlements", "settle_run",
           "BATCH_COVERAGE_LEVEL", "BackfillReport",
           "COVERAGE_DECISIONS", "COVERAGE_LEVELS", "COVERAGE_STATUSES", "CoverageDerivation",
           "DECISION_QUEUE", "ENRICHED", "EXCLUDED_ALREADY_ENRICHED",
           "EXCLUDED_KNOWN_UNRESOLVED", "FAILED", "LEVEL_REGISTER", "MAX_BACKFILL_PAGE",
           "MAX_COVERAGE_ENTRIES", "MAX_IDENTITY_SCAN_ROWS", "PENDING", "STATUS_RANK",
           "UNRESOLVED_AMBIGUOUS", "UNRESOLVED_NOT_FOUND", "UNRESOLVED_STATUSES",
           "VARIANT_IDENTITY_CONTRACT", "VOLATILE_PAYLOAD_FIELDS", "assembled_output",
           "backfill", "backfill_run",
           "candidate_identity_key", "coverage_decision", "derive_coverage",
           "record_run_coverage", "variant_content_sha256", "variant_identity_key",
           "variant_identity_text", "KEY_COLLISION", "KeySettlement",
           "REGISTER_IDENTITY_FIELDS", "collision_content_sha256", "register_code",
           "register_codes", "settle_keys"]

"""Catalog PR3: the bounded DATABASE-SIDE read the Government Tool answers from.

Why this exists beside `projection.py`
--------------------------------------

`GovernmentCatalogProjection` (PR2) materializes a whole snapshot in Python and
groups it there. That is correct and cheap for the pinned `q=RAV4` capture (233
rows) and it deliberately REFUSES anything past
`MAX_PROJECTION_CANDIDATES` (5 000) rather than truncating. A complete WLTP
resource is around 101 000 rows, so a Tool that must answer over one could not
use that path at all.

This module is that missing reader, and it is a SECOND reader with a different
cost model -- not a wider version of the first. `MAX_PROJECTION_CANDIDATES` is
untouched, the Python projection still refuses beyond it, and nothing here
truncates: every answer is one explicitly bounded page carrying the EXACT total
the filter matched.

What the two share, verbatim
----------------------------

Which snapshot may answer is ONE rule, `resolve_active_snapshot` in
`projection.py`, imported here rather than restated: the same exact pinned
lookup, the same newest-usable search, the same `parse_normalization_state`
gate and the same single acknowledgeable refusal. Two copies of that rule is
precisely the drift that would let one reader answer from a snapshot the other
refuses. The dataclasses a result is made of are the projection's too.

What is different, and stated plainly
-------------------------------------

A page from this layer carries the candidate's stored identity and its exact
provenance. It does NOT carry the preserved raw payload, so it does not carry
`engine_displacement_cc` or `unresolved_dimensions`, both of which the
projection re-derives by reading that payload again. Those travel with ONE
record, through `resolve_variant`'s identity projection, which is a bounded,
server-selected set of register fields rather than a row dump.

Nothing here opens a socket, calls a provider, or constructs a `DataGovClient`.
It reads durable rows a previous ingestion already landed, through injected
repository methods, and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from backend.catalog.register_codes import REGISTER_IDENTITY_FIELDS, register_code
from backend.errors import AppError
from backend.runtime import CancellationRequested

from . import source as src
from .projection import (DEFAULT_RESULT_ITEMS, MAX_RESULT_ITEMS, DatasetProvenance,
                         GovernmentProjectionError, ModelYearSummary, provenance_of,
                         resolve_active_snapshot)

#: The register fields a resolved variant states its reviewed identity in, and
#: nothing else. STATIC server data: the identity projection is this tuple, so
#: no register row can widen what a tool result carries by containing more
#: columns, and no caller can name a field to have projected.
#:
#: Exactly the fields `read_wltp_record` reads for identity -- the marque, the
#: commercial model, the model year, the official model code, the trim and the
#: fuel code with its label. `koah_sus` and every other field PR2 deliberately
#: left unread is absent here for the same reviewed reason.
#: Each field travels with the JSON TYPE the reviewed reading expects, and a
#: value of any other type is DROPPED rather than carried: a register that one
#: day publishes a year as text has changed what it states, and quoting it into
#: a closed output schema as though nothing happened is how a type confusion
#: becomes a fact.
IDENTITY_RECORD_FIELD_TYPES: Mapping[str, type] = {
    "tozar": str, "kinuy_mishari": str, "shnat_yitzur": int, "degem_nm": str,
    "ramat_gimur": str, "delek_cd": int, "delek_nm": str,
}
IDENTITY_RECORD_FIELDS: tuple[str, ...] = tuple(IDENTITY_RECORD_FIELD_TYPES)

#: PR-Z3: the Government's own registration identifiers, as (filter / row
#: attribute name, register field). A resolved row's identity projection
#: carries them too, so evidence and the vehicle output name the exact
#: registration. Unlike the reviewed identity fields above they travel as TEXT
#: -- the `payload->>'field'` rendering (`register_codes.register_code`) that the
#: database filter compares -- so the projection states exactly what a caller
#: must state to narrow by them. A register value that is no JSON scalar is
#: dropped, like every unstated field.
REGISTER_CODE_FIELDS: tuple[tuple[str, str], ...] = REGISTER_IDENTITY_FIELDS
REGISTER_CODE_FILTERS: tuple[str, ...] = tuple(name for name, _field in REGISTER_CODE_FIELDS)

#: The largest number of matches `resolve_variant` will carry back. An
#: ambiguity wider than this is still an ambiguity -- the count is exact and
#: the caller is told there are more -- but nothing here turns a tool result
#: into a bulk listing.
MAX_RESOLUTION_MATCHES = 20

#: PR-V: how `resolve_variant` matched the official model code. `exact` is the
#: register's own spelling; `separator_insensitive` is the ONE retry taken
#: only when the exact spelling matched nothing (see `separator_key`).
MATCH_EXACT = "exact"
MATCH_SEPARATOR_INSENSITIVE = "separator_insensitive"
MATCH_MODES = (MATCH_EXACT, MATCH_SEPARATOR_INSENSITIVE)

#: The most candidate rows the separator-insensitive retry reads to find its
#: matches: ONE bounded page of the model-year's rows with the code filter
#: dropped. A model year stating more rows than this is not scanned at all --
#: the answer stays the exact one (no match), never a partial guess.
MAX_SEPARATOR_SCAN_ROWS = 200


def separator_key(code: str | None) -> str | None:
    """An official model code with "-" vs " " and repeated spaces ignored.

    A COMPARISON key only, never stored and never shown: the register writes
    the same code both ways across rows (`TZNA55L-GKZSZA` and `TZNA55L GKZSZA`
    are two different register rows), and the key is what lets a request in
    one spelling find a row stored in the other. Case and every other
    character are kept exactly.
    """
    if not isinstance(code, str):
        return None
    return " ".join(code.replace("-", " ").split()) or None


@dataclass(frozen=True)
class ManufacturerCoverage:
    """One manufacturer's coverage in one snapshot, aggregated in the database."""

    manufacturer: str
    model_count: int
    variant_count: int
    ambiguous_variant_count: int


@dataclass(frozen=True)
class ModelCoverage:
    """One commercial model's coverage.

    `model_year_start`/`model_year_end` are the FIRST and LAST model year the
    snapshot states for this model -- a span, never a claim that every year
    between them exists. The exact set of years is its own bounded operation
    (`list_model_years`), because deriving it here would mean expanding every
    candidate's range into a set whose size nothing bounds.
    """

    manufacturer: str
    commercial_model: str
    variant_count: int
    ambiguous_variant_count: int
    model_year_start: int | None
    model_year_end: int | None


@dataclass(frozen=True)
class CandidateVariantRow:
    """One candidate variant as the database returned it, with its provenance.

    Deliberately NOT `projection.VariantView`: that type carries
    `engine_displacement_cc` and `unresolved_dimensions`, which are re-derived
    by reading the preserved raw payload, and this layer never reads a payload
    for a whole page. A type that shared the name and silently carried `None`
    for both would be a harder bug than two types.
    """

    candidate_id: str
    candidate_key: str
    status: str
    manufacturer: str
    commercial_model: str
    model_year_start: int
    model_year_end: int
    official_model_code: str | None
    trim: str | None
    identity_dimensions: Mapping[str, str]
    upstream_record_id: str
    raw_record_id: str
    resource_id: str
    source_locator: Mapping[str, int]
    payload_sha256: str
    snapshot_key: str
    upstream_version: str
    upstream_version_kind: str
    #: PR-Z3: the row's `payload->>'tozeret_cd'`, `->>'degem_cd'` and
    #: `->>'sug_degem'`, verbatim; None when the register states none.
    register_manufacturer_code: str | None = None
    register_model_code: str | None = None
    vehicle_type_code: str | None = None

    @property
    def register_codes(self) -> dict[str, str]:
        """The register codes this row states, by filter name (absent when unstated)."""
        return {name: value for name, value in (
            ("register_manufacturer_code", self.register_manufacturer_code),
            ("register_model_code", self.register_model_code),
            ("vehicle_type_code", self.vehicle_type_code)) if value is not None}


@dataclass(frozen=True)
class QueryPage:
    """One bounded, explicitly paginated answer with its EXACT total."""

    items: tuple[Any, ...]
    total: int
    offset: int
    limit: int
    provenance: DatasetProvenance

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total


@dataclass(frozen=True)
class VariantResolutionResult:
    """One variant, or every match -- never a choice between them.

    `identity_projection` is populated ONLY when exactly one variant matched:
    it is the bounded, server-selected set of register fields that variant's
    own row states, and it is the material the trusted evidence mapper reads.
    An ambiguous resolution carries none, because there is no single row whose
    fields could be quoted.
    """

    matches: tuple[CandidateVariantRow, ...]
    match_count: int
    provenance: DatasetProvenance
    identity_projection: Mapping[str, Any] = field(default_factory=dict)
    #: PR-V: `exact`, or `separator_insensitive` when the exact code matched
    #: nothing and the one separator-insensitive retry answered instead.
    match_mode: str = MATCH_EXACT

    @property
    def variant(self) -> CandidateVariantRow | None:
        return self.matches[0] if self.match_count == 1 and self.matches else None

    @property
    def ambiguous(self) -> bool:
        return self.match_count > 1


#: The column every bounded aggregation carries the EXACT match count in.
TOTAL_COUNT_FIELD = "total_count"


def is_count_row(row: Mapping[str, Any]) -> bool:
    """Whether one returned row is the COUNT ROW rather than a result.

    A page whose offset is past the last matching row has no rows to hang the
    count on, so the aggregation returns exactly one row with every item column
    null and the real total. Recognising it by SHAPE keeps this generic: no
    caller needs to know which column a particular aggregation would have
    filled in.
    """
    return all(value is None for name, value in row.items() if name != TOTAL_COUNT_FIELD)


def bounded_limit(limit: Any) -> int:
    """The server-owned page bound. A caller asking for more gets this many."""
    try:
        requested = int(limit)
    except (TypeError, ValueError):
        requested = DEFAULT_RESULT_ITEMS
    return max(1, min(requested, MAX_RESULT_ITEMS))


def bounded_offset(offset: Any) -> int:
    try:
        return max(0, int(offset))
    except (TypeError, ValueError):
        return 0


def identity_projection(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The reviewed identity fields one register row STATES, and nothing else.

    A field the row does not carry is an ABSENT KEY, never a null and never an
    empty string: an absent key is what keeps "the register said nothing here"
    distinguishable from "the register said nothing". A field whose value is an
    object or a list is dropped for the same reason a projection refuses one --
    it is not a stated scalar.
    """
    if not isinstance(payload, Mapping):
        return {}
    projected: dict[str, Any] = {}
    for name, expected in IDENTITY_RECORD_FIELD_TYPES.items():
        if name not in payload:
            continue
        value = payload[name]
        # `True` is an `int` in Python and is not a model year. Booleans are
        # excluded explicitly rather than by hoping no register publishes one.
        if isinstance(value, bool) or not isinstance(value, expected):
            continue
        if isinstance(value, str) and not value.strip():
            continue
        projected[name] = value
    for _name, field_name in REGISTER_CODE_FIELDS:
        try:
            code = register_code(payload, field_name)
        except ValueError:
            continue
        if code is not None:
            projected[field_name] = code
    return projected


class GovernmentCatalogQuery:
    """The bounded database-side read over one active Government snapshot."""

    def __init__(self, repository: Any, *, resource_id: str = src.WLTP_RESOURCE_ID,
                 snapshot_key: str | None = None, allow_incomplete: bool = False,
                 cancellation_checker: Callable[[], bool] | None = None) -> None:
        self._repository = repository
        self._resource_id = src.require_allowed_resource(resource_id)
        self._snapshot_key = snapshot_key
        self._allow_incomplete = bool(allow_incomplete)
        self._cancellation_checker = cancellation_checker
        self._snapshot: Mapping[str, Any] | None = None

    # --- the active snapshot -------------------------------------------------

    def _active(self) -> Mapping[str, Any]:
        """Resolve the answering snapshot ONCE, through the shared rule."""
        if self._snapshot is None:
            self._check_cancelled()
            self._snapshot = resolve_active_snapshot(
                self._repository, resource_id=self._resource_id,
                snapshot_key=self._snapshot_key, allow_incomplete=self._allow_incomplete)
        return self._snapshot

    def dataset_metadata(self) -> DatasetProvenance:
        return provenance_of(self._active())

    # --- the bounded read surface -------------------------------------------

    def list_manufacturers(self, *, limit: int = DEFAULT_RESULT_ITEMS,
                           offset: int = 0) -> QueryPage:
        snapshot, provenance = self._active(), self.dataset_metadata()
        rows, total = self._read(self._repository.catalog_candidate_manufacturers,
                                 snapshot["id"], limit=limit, offset=offset)
        return self._page([ManufacturerCoverage(
            manufacturer=str(row["manufacturer"]),
            model_count=int(row["model_count"]), variant_count=int(row["variant_count"]),
            ambiguous_variant_count=int(row["ambiguous_variant_count"]))
            for row in rows], total, limit, offset, provenance)

    def get_manufacturer_summary(self, manufacturer: str) -> ManufacturerCoverage | None:
        """ONE manufacturer's coverage, or None when the snapshot states none.

        An exact equality lookup: the register's marque text is its own, so a
        prefix or a case-folded match here would answer for a manufacturer the
        caller did not ask about.
        """
        page = self.list_variants(manufacturer=str(manufacturer), limit=1, offset=0)
        if page.total == 0:
            return None
        _models, model_count = self._read(
            self._repository.catalog_candidate_models, self._active()["id"],
            manufacturer=str(manufacturer), limit=1, offset=0)
        ambiguous = self.list_variants(manufacturer=str(manufacturer), status="ambiguous",
                                       limit=1, offset=0)
        return ManufacturerCoverage(
            manufacturer=str(manufacturer), model_count=model_count,
            variant_count=page.total, ambiguous_variant_count=ambiguous.total)

    def list_models(self, manufacturer: str, *, limit: int = DEFAULT_RESULT_ITEMS,
                    offset: int = 0) -> QueryPage:
        provenance = self.dataset_metadata()
        rows, total = self._read(self._repository.catalog_candidate_models,
                                 self._active()["id"], manufacturer=str(manufacturer),
                                 limit=limit, offset=offset)
        return self._page([ModelCoverage(
            manufacturer=str(row["manufacturer"]),
            commercial_model=str(row["commercial_model"]),
            variant_count=int(row["variant_count"]),
            ambiguous_variant_count=int(row["ambiguous_variant_count"]),
            model_year_start=_whole(row.get("model_year_start")),
            model_year_end=_whole(row.get("model_year_end")))
            for row in rows], total, limit, offset, provenance)

    def list_model_years(self, manufacturer: str, commercial_model: str, *,
                         limit: int = DEFAULT_RESULT_ITEMS, offset: int = 0) -> QueryPage:
        provenance = self.dataset_metadata()
        rows, total = self._read(self._repository.catalog_candidate_model_years,
                                 self._active()["id"], manufacturer=str(manufacturer),
                                 commercial_model=str(commercial_model),
                                 limit=limit, offset=offset)
        return self._page([ModelYearSummary(
            manufacturer=str(row["manufacturer"]),
            commercial_model=str(row["commercial_model"]),
            model_year=int(row["model_year"]), variant_count=int(row["variant_count"]),
            ambiguous_variant_count=int(row["ambiguous_variant_count"]))
            for row in rows], total, limit, offset, provenance)

    def list_variants(self, *, manufacturer: str | None = None,
                      commercial_model: str | None = None, model_year: int | None = None,
                      official_model_code: str | None = None, trim: str | None = None,
                      identity_dimensions: Mapping[str, str] | None = None,
                      status: str | None = None,
                      limit: int = DEFAULT_RESULT_ITEMS, offset: int = 0,
                      register_manufacturer_code: str | None = None,
                      register_model_code: str | None = None,
                      vehicle_type_code: str | None = None) -> QueryPage:
        """Every candidate matching exactly the STATED filters, one page at a time.

        Nothing collapses two rows and nothing picks a first: a model year with
        several trims is several rows here, because that is what the register
        states.

        PR-Z3: the register's own identifiers are three more EXACT filters,
        applied in the database in the same one bounded read (never a Python
        filter over a page): `register_manufacturer_code` (`tozeret_cd`),
        `register_model_code` (`degem_cd`), `vehicle_type_code` (`sug_degem`),
        each compared verbatim with the row's `payload->>'field'`. An unstated
        filter states nothing, like every other one.
        """
        provenance = self.dataset_metadata()
        codes = {name: str(value) for name, value in (
            ("register_manufacturer_code", register_manufacturer_code),
            ("register_model_code", register_model_code),
            ("vehicle_type_code", vehicle_type_code)) if value is not None}
        rows, total = self._read(
            self._repository.catalog_candidate_variant_page, self._active()["id"],
            manufacturer=manufacturer, commercial_model=commercial_model,
            model_year=model_year, official_model_code=official_model_code, trim=trim,
            identity_dimensions=dict(identity_dimensions or {}) or None, status=status,
            limit=limit, offset=offset, **codes)
        return self._page([self._variant_row(row, provenance) for row in rows],
                          total, limit, offset, provenance)

    def find_by_model_code(self, official_model_code: str, *,
                           limit: int = DEFAULT_RESULT_ITEMS, offset: int = 0) -> QueryPage:
        """Exact official-model-code lookup. Exact: never a prefix, never a substring."""
        return self.list_variants(official_model_code=str(official_model_code),
                                  limit=limit, offset=offset)

    def resolve_variant(self, manufacturer: str, commercial_model: str, model_year: int, *,
                        trim: str | None = None, official_model_code: str | None = None,
                        identity_dimensions: Mapping[str, str] | None = None,
                        register_manufacturer_code: str | None = None,
                        register_model_code: str | None = None,
                        vehicle_type_code: str | None = None
                        ) -> VariantResolutionResult:
        """Narrow by what the caller STATES, and refuse to choose beyond it.

        A request that still matches two rows returns both and says so. It does
        not return the first, and no further argument could break the tie --
        allowing that would let a caller resolve a real ambiguity by fiat.

        PR-Z3: the register's own identifiers narrow too, as exact database
        filters. They are not a caller's choice between rows: they are what
        ONE register row states, and the trusted tool accepts them only when
        they name a row the server handed the run (`GovernmentVehicleTool`,
        GOVERNMENT_REGISTER_CODE_NOT_IN_BATCH). Rows that state the same
        identity AND the same codes stay ambiguous here, exactly as before.

        The identity projection is read ONLY for a unique resolution, and only
        for that one row: an ambiguous answer quotes nothing, because there is
        no single row whose fields it could quote.
        """
        codes = {"register_manufacturer_code": register_manufacturer_code,
                 "register_model_code": register_model_code,
                 "vehicle_type_code": vehicle_type_code}
        page = self.list_variants(manufacturer=str(manufacturer),
                                  commercial_model=str(commercial_model),
                                  model_year=int(model_year), trim=trim,
                                  official_model_code=official_model_code,
                                  identity_dimensions=identity_dimensions,
                                  limit=MAX_RESOLUTION_MATCHES, offset=0, **codes)
        matches, total, mode = tuple(page.items), page.total, MATCH_EXACT
        if total == 0 and official_model_code is not None:
            # PR-V: ONLY when the exact spelling matched nothing. An exact
            # answer -- unique or ambiguous -- is never widened by this.
            retried = self._separator_insensitive(
                str(manufacturer), str(commercial_model), int(model_year), trim=trim,
                official_model_code=str(official_model_code),
                identity_dimensions=identity_dimensions, codes=codes)
            if retried is not None:
                matches, total, mode = retried[0], retried[1], MATCH_SEPARATOR_INSENSITIVE
        if total != 1 or not matches:
            return VariantResolutionResult(matches=matches, match_count=total,
                                           provenance=page.provenance, match_mode=mode)
        record = self._raw_record(matches[0].upstream_record_id)
        payload = record.get("payload") if isinstance(record, Mapping) else None
        return VariantResolutionResult(
            matches=matches, match_count=1, provenance=page.provenance,
            identity_projection=identity_projection(payload if isinstance(payload, Mapping) else {}),
            match_mode=mode)

    def _separator_insensitive(self, manufacturer: str, commercial_model: str,
                               model_year: int, *, trim: str | None,
                               official_model_code: str,
                               identity_dimensions: Mapping[str, str] | None,
                               codes: Mapping[str, str | None] | None = None
                               ) -> tuple[tuple[CandidateVariantRow, ...], int] | None:
        """The ONE retry: the same stated filters, the code compared by `separator_key`.

        One bounded page of the model year's rows with the code filter dropped
        (everything else still filtered in the database), compared here by the
        separator-insensitive key. None -- "no retry answer" -- when the code
        has no key, when no row's code shares it, or when the model year states
        more rows than one bounded page holds: a partial scan could miss a
        match and must not be read as "the only match".
        """
        wanted = separator_key(official_model_code)
        if wanted is None:
            return None
        page = self.list_variants(manufacturer=manufacturer, commercial_model=commercial_model,
                                  model_year=model_year, trim=trim,
                                  identity_dimensions=identity_dimensions,
                                  limit=MAX_SEPARATOR_SCAN_ROWS, offset=0, **dict(codes or {}))
        if page.has_more:
            return None
        found = tuple(item for item in page.items
                      if item.official_model_code is not None
                      and separator_key(item.official_model_code) == wanted)
        if not found:
            return None
        return found[:MAX_RESOLUTION_MATCHES], len(found)

    # --- helpers -------------------------------------------------------------

    def raw_record(self, upstream_record_id: str) -> Mapping[str, Any]:
        """ONE stored register row of the pinned snapshot, by its register id.

        A single-row read (never a page of payloads), refused like every other
        read of this layer when the row is missing or unreadable.
        """
        return self._raw_record(upstream_record_id)

    def _raw_record(self, upstream_record_id: str) -> Mapping[str, Any]:
        self._check_cancelled()
        try:
            row = self._repository.catalog_raw_record_by_upstream_id(
                self._active()["id"], str(upstream_record_id),
                allow_incomplete=self._allow_incomplete)
        except AppError:
            raise GovernmentProjectionError("GOV_QUERY_UNAVAILABLE") from None
        if not row:
            # A candidate must name a row of its own snapshot; the schema
            # enforces it with a composite foreign key. Reaching this means the
            # read set is inconsistent, so the query refuses rather than
            # answering without provenance.
            raise GovernmentProjectionError("GOV_PROJECTION_RECORD_MISSING")
        return row

    def _read(self, reader: Callable[..., Sequence[Mapping[str, Any]]], snapshot_id: Any,
              **kwargs: Any) -> tuple[list[Mapping[str, Any]], int]:
        """One bounded database read, with the shared cancellation gate.

        Returns the page's rows AND the exact total the filter matched, which
        are two different things: a page whose offset is past the last matching
        row has no rows and a total of thousands.

        Every repository refusal collapses to ONE static reason: the underlying
        message can quote SQL values, and a classification is what a caller of
        this layer is meant to receive.
        """
        self._check_cancelled()
        kwargs["limit"] = bounded_limit(kwargs.get("limit", DEFAULT_RESULT_ITEMS))
        kwargs["offset"] = bounded_offset(kwargs.get("offset", 0))
        kwargs["allow_incomplete"] = self._allow_incomplete
        try:
            rows = list(reader(snapshot_id, **kwargs))
        except AppError:
            raise GovernmentProjectionError("GOV_QUERY_UNAVAILABLE") from None
        self._check_cancelled()
        # The aggregation carries `total_count` on every row, and on a COUNT
        # ROW when the page is empty -- one row whose every other column is
        # null. Reading the total before dropping it is what makes an
        # out-of-range page report the real total instead of zero.
        total = int(rows[0][TOTAL_COUNT_FIELD]) if rows else 0
        return [row for row in rows if not is_count_row(row)], total

    def _variant_row(self, row: Mapping[str, Any],
                     provenance: DatasetProvenance) -> CandidateVariantRow:
        return CandidateVariantRow(
            candidate_id=str(row["id"]), candidate_key=str(row["candidate_key"]),
            status=str(row["status"]), manufacturer=str(row["manufacturer"]),
            commercial_model=str(row["commercial_model"]),
            model_year_start=int(row["model_year_start"]),
            model_year_end=int(row["model_year_end"]),
            official_model_code=row.get("official_model_code"), trim=row.get("trim"),
            identity_dimensions=dict(row.get("identity_dimensions") or {}),
            upstream_record_id=str(row["upstream_record_id"]),
            raw_record_id=str(row["raw_record_id"]), resource_id=str(row["resource_id"]),
            source_locator=dict(row.get("source_locator") or {}),
            payload_sha256=str(row.get("payload_sha256") or ""),
            snapshot_key=provenance.snapshot_key,
            upstream_version=provenance.upstream_version,
            upstream_version_kind=provenance.upstream_version_kind,
            register_manufacturer_code=_code(row.get("register_manufacturer_code")),
            register_model_code=_code(row.get("register_model_code")),
            vehicle_type_code=_code(row.get("vehicle_type_code")))

    @staticmethod
    def _page(items: Sequence[Any], total: int, limit: Any, offset: Any,
              provenance: DatasetProvenance) -> QueryPage:
        """Assemble one page around the database's own exact count.

        The total is NEVER inferred from the page: `_read` took it from the
        aggregation, which states it whether or not the page has rows.
        """
        return QueryPage(items=tuple(items), total=int(total),
                         offset=bounded_offset(offset), limit=bounded_limit(limit),
                         provenance=provenance)

    def _check_cancelled(self) -> None:
        if self._cancellation_checker is not None and self._cancellation_checker():
            raise CancellationRequested("RUN_CANCELLED")


def _code(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _whole(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


__all__ = ["IDENTITY_RECORD_FIELDS", "IDENTITY_RECORD_FIELD_TYPES",
           "MATCH_EXACT", "MATCH_MODES", "MATCH_SEPARATOR_INSENSITIVE",
           "MAX_RESOLUTION_MATCHES", "MAX_SEPARATOR_SCAN_ROWS", "CandidateVariantRow",
           "REGISTER_CODE_FIELDS", "REGISTER_CODE_FILTERS",
           "GovernmentCatalogQuery", "ManufacturerCoverage", "ModelCoverage", "QueryPage",
           "TOTAL_COUNT_FIELD", "VariantResolutionResult", "bounded_limit",
           "bounded_offset", "is_count_row",
           "identity_projection", "separator_key"]

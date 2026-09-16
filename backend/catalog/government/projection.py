"""The internal, bounded read that reconstructs the Government tree.

THIS IS NOT A TOOL. It is a service/query component: a plain class with typed
methods, no `Tool` protocol, no operations mapping, no input or output schema
and no registration. `backend/tools/registry.py` is untouched and the
production `ToolRegistry` is still empty. Wrapping this in a
`GovernmentVehicleTool`, granting it a scope and registering it is PR3's work,
and doing it here would connect a capability nobody has reviewed end to end.

What it guarantees
------------------

*   **Only ACTIVE snapshots are readable.** `activated_at is not null` is a
    column predicate in the repository read, and a snapshot is only ever
    activated once its own record count equals the total the upstream declared.
    A pending capture and a failed one are invisible here.
*   **Deterministic ordering.** Every ordering is applied in Python, over a
    materialized bounded set, using Python's own codepoint comparison -- never
    the database's text collation. The Government identity text is Hebrew, so a
    collation-ordered read would make "the same snapshot produces the same
    ordered tree" false on a differently configured cluster.
*   **Explicit pagination and bounds.** Every listing takes `limit` and
    `offset`, reports the true total and says whether more remains. The whole
    per-snapshot read is bounded too, and a snapshot larger than that bound is
    a REFUSAL rather than a silent truncation -- a query layer that quietly
    answers from a prefix is the failure the R5 pagination round was about.
*   **Provenance travels with every result.** Every variant states the
    snapshot, the resource, the upstream version and kind, the register's own
    `_id`, the durable raw-record id and the exact page and index it was
    captured at.
*   **Ambiguity is returned, never resolved.** `resolve_variant` answers with
    one variant or with every match it found; it never picks a first row. A
    candidate the normalizer could not fully settle keeps status `ambiguous`
    and says which dimensions were not settled.

What it deliberately does not do
--------------------------------

It manufactures no claim, no verdict and no evidence: a projected variant is
backed by `snapshot -> raw record -> candidate`, which is what PR2 established,
and nothing here promotes anything to the canonical catalog.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from backend.runtime import CancellationRequested

from . import source as src
from .normalize import (GOVERNMENT_NORMALIZATION_REASONS, GovernmentNormalizationError,
                        MAX_DURABLE_ISSUE_RECORDS, NORMALIZATION_CONTRACT,
                        RAW_ONLY_CONTRACT, UNMAPPED_FIELDS, read_wltp_record)

#: The largest number of candidates one snapshot may be projected from in a
#: single read. A snapshot beyond it is refused rather than truncated.
#:
#: STATED PLAINLY: the pinned `q=RAV4` capture holds 233 rows and fits easily.
#: A capture of the WHOLE WLTP resource -- about 101,000 rows -- does NOT fit,
#: and is deliberately out of scope for PR2: answering it needs database-side
#: aggregation and ordering, which belongs with the Tool that will consume it.
MAX_PROJECTION_CANDIDATES = 5_000

#: The largest page one listing may return.
MAX_RESULT_ITEMS = 200
DEFAULT_RESULT_ITEMS = 50

#: How many rows each repository read asks for at a time while accumulating.
READ_CHUNK = 500

GOVERNMENT_PROJECTION_REASONS: Mapping[str, str] = {
    "GOV_PROJECTION_NO_ACTIVE_SNAPSHOT":
        "no active government snapshot answers for that resource",
    "GOV_PROJECTION_BOUND_EXCEEDED":
        "this snapshot holds more candidates than one projection may read",
    "GOV_PROJECTION_RECORD_MISSING":
        "a candidate names a raw record this snapshot does not hold",
    "GOV_PROJECTION_SNAPSHOT_UNKNOWN":
        "no active government snapshot carries that snapshot key",
    "GOV_PROJECTION_SNAPSHOT_INCOMPLETE":
        "that government snapshot holds rows its reviewed vocabulary could not read",
    "GOV_PROJECTION_RESOURCE_NOT_NORMALIZED":
        "that government resource is captured raw-only and states no vehicle identities",
    "GOV_PROJECTION_SNAPSHOT_NOT_READ":
        "that government snapshot records no reading of its rows at all",
    "GOV_PROJECTION_SNAPSHOT_STATE_INVALID":
        "that government snapshot's recorded reading is malformed or disagrees with its rows",
    # Catalog PR3: the database-side reader (`query.py`) collapses every
    # repository refusal onto ONE static reason. The underlying message can
    # quote SQL values, and a classification is what a caller of that layer is
    # meant to receive.
    "GOV_QUERY_UNAVAILABLE":
        "the bounded government catalog query could not be answered",
}

#: The ONE refusal an explicit acknowledgement may bypass.
#:
#: An incomplete snapshot is a real capture with a stated, counted gap, so a
#: caller that has seen the gap may still read it. The other two are not gaps
#: in an answer -- a raw-only resource states no identities at all, and a
#: snapshot with no recorded reading cannot say what it is missing -- so no
#: acknowledgement makes either answerable.
ACKNOWLEDGEABLE_REFUSALS = frozenset({"GOV_PROJECTION_SNAPSHOT_INCOMPLETE"})


class GovernmentProjectionError(ValueError):
    """A projection refusal carrying ONLY a static, code-owned reason."""

    def __init__(self, reason_code: str):
        if reason_code not in GOVERNMENT_PROJECTION_REASONS:
            raise ValueError("government projection reason must come from the static allowlist")
        self.reason_code = reason_code
        self.safe_message = GOVERNMENT_PROJECTION_REASONS[reason_code]
        super().__init__(self.safe_message)


@dataclass(frozen=True)
class NormalizationState:
    """A snapshot's recorded reading, PARSED rather than taken on trust.

    `_usability` used to ask two questions of the stored metadata -- does the
    contract string match, and is one integer zero -- and treat the answer as
    the truth. Everything else about the summary was accepted as written, so a
    recorded reading that was missing, mistyped, self-contradicting or simply
    invented produced a snapshot that answered queries as though it were whole.

    This type exists so that the only way to reach a projection is through a
    parse that either yields a consistent state or refuses. Constructing one
    means every field was present, of the right type, in range, and in
    agreement with the snapshot's own row counts.
    """

    contract: str
    normalized_record_count: int
    issue_count: int
    issues: Mapping[str, int]
    issue_records: tuple[str, ...]


@dataclass(frozen=True)
class DatasetProvenance:
    """Everything a result needs to say about where it came from."""

    snapshot_id: str
    snapshot_key: str
    source_family: str
    trust_state: str
    resource_id: str
    package_id: str
    publisher: str
    dataset_title: str
    dataset_market_scope: str
    upstream_version: str
    upstream_version_kind: str
    content_sha256: str
    schema_fingerprint: str
    page_chain_sha256: str
    page_count: int
    declared_record_count: int
    stored_record_count: int
    retrieved_at: str
    activated_at: str
    query: Mapping[str, str]
    #: The snapshot's own durable reading gap. Carried on EVERY answer, so a
    #: result read under an acknowledgement is never mistaken for a complete
    #: one further down the line.
    normalization_contract: str = ""
    normalized_record_count: int = 0
    normalization_issue_count: int = 0
    normalization_issues: Mapping[str, int] = field(default_factory=dict)
    normalization_issue_records: tuple[str, ...] = ()


@dataclass(frozen=True)
class VariantView:
    """One candidate variant, with the exact row it was read from."""

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
    #: The register's OWN row identity, and the durable row that preserves it.
    upstream_record_id: str
    raw_record_id: str
    resource_id: str
    #: The exact page and index the row was captured at.
    source_locator: Mapping[str, int]
    upstream_version: str
    upstream_version_kind: str
    snapshot_key: str
    #: Read again from the preserved raw payload rather than stored on the
    #: candidate: the closed identity vocabulary has no displacement dimension,
    #: and inventing one in a normalizer would be a schema change in disguise.
    engine_displacement_cc: int | None = None
    #: Dimensions the register stated that the reviewed vocabulary could not
    #: settle. Non-empty is exactly why `status` is `ambiguous`.
    unresolved_dimensions: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModelYearSummary:
    manufacturer: str
    commercial_model: str
    model_year: int
    variant_count: int
    ambiguous_variant_count: int

    @property
    def resolves_to_one_variant(self) -> bool:
        return self.variant_count == 1 and self.ambiguous_variant_count == 0


@dataclass(frozen=True)
class ModelSummary:
    manufacturer: str
    commercial_model: str
    variant_count: int
    model_years: tuple[int, ...]


@dataclass(frozen=True)
class ManufacturerSummary:
    manufacturer: str
    model_count: int
    variant_count: int


@dataclass(frozen=True)
class ResultPage:
    """One bounded, explicitly paginated answer, with its provenance."""

    items: tuple[Any, ...]
    total: int
    offset: int
    limit: int
    provenance: DatasetProvenance

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total


@dataclass(frozen=True)
class VariantResolution:
    """One variant, or every match -- never a choice between them."""

    matches: tuple[VariantView, ...]
    provenance: DatasetProvenance

    @property
    def variant(self) -> VariantView | None:
        """The single match, or None. `None` with several matches is AMBIGUOUS,
        and the matches are returned so a caller can see what it is between."""
        return self.matches[0] if len(self.matches) == 1 else None

    @property
    def ambiguous(self) -> bool:
        return len(self.matches) > 1


@dataclass(frozen=True)
class RecordView:
    """One register row, exactly as captured, with its provenance."""

    upstream_record_id: str
    raw_record_id: str
    resource_id: str
    payload: Mapping[str, Any]
    payload_sha256: str
    source_locator: Mapping[str, int]
    provenance: DatasetProvenance
    variants: tuple[VariantView, ...] = ()


#: What a durable issue-record id may look like. The register's own `_id` is an
#: integer, so a stored id is its decimal text: bounded, and never a path, a
#: locator or free text that a reader might be tempted to resolve.
_ISSUE_RECORD_PATTERN = re.compile(r"^[0-9]{1,20}$")

#: Exactly the keys one durable issue entry carries. Closed in BOTH directions:
#: a missing key and an extra one are equally a refusal, because an entry this
#: code does not fully understand is not an entry it may count.
_ISSUE_ENTRY_KEYS = frozenset({"reason", "count"})


def _whole(value: Any) -> int | None:
    """A non-negative JSON integer, or None. `True` is not 1 here."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def parse_normalization_state(snapshot: Mapping[str, Any]) -> NormalizationState:
    """Read a snapshot's recorded reading STRICTLY, or refuse it.

    Every rule below is a way the stored summary could be wrong while still
    satisfying an equality check on the contract string, and each one is a
    refusal rather than a coercion:

    *   both counts present, JSON integers, non-negative -- and `True`/`False`
        are not integers here, because a boolean that reads as 1 would let a
        malformed summary pass as a count;
    *   the two counts SUM to the snapshot's own `stored_record_count`, so a
        summary cannot describe a different number of rows than the snapshot
        holds;
    *   `normalization_issues` is a list of `{reason, count}` objects and
        nothing else: every reason is in the normalization refusal vocabulary,
        no reason repeats, every count is a POSITIVE integer, and the counts sum
        to exactly `normalization_issue_count`;
    *   `normalization_issue_records` is a bounded list of distinct id strings,
        and its length is exactly what the issue count implies given the durable
        bound -- so neither an invented id nor a quietly dropped one survives;
    *   zero issues means an empty reason list AND an empty record list.

    A raw-only snapshot states no reading at all, so it is held to zeroes and
    empty lists rather than to the rules above.

    Never raises anything but `GovernmentProjectionError`: a `KeyError` or a
    `ValueError` escaping here would carry a field name or a row into a caller
    that is meant to receive a classification.
    """
    metadata = snapshot.get("retrieval_metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    contract = metadata.get("normalization_contract")
    if contract not in (RAW_ONLY_CONTRACT, NORMALIZATION_CONTRACT):
        raise GovernmentProjectionError("GOV_PROJECTION_SNAPSHOT_NOT_READ")

    normalized = _whole(metadata.get("normalized_record_count"))
    issue_count = _whole(metadata.get("normalization_issue_count"))
    entries = metadata.get("normalization_issues")
    records = metadata.get("normalization_issue_records")
    if normalized is None or issue_count is None or not isinstance(entries, list) \
            or not isinstance(records, list):
        raise GovernmentProjectionError("GOV_PROJECTION_SNAPSHOT_STATE_INVALID")

    stored = _whole(snapshot.get("stored_record_count"))
    if stored is None:
        raise GovernmentProjectionError("GOV_PROJECTION_SNAPSHOT_STATE_INVALID")

    if contract == RAW_ONLY_CONTRACT:
        # Nothing was read, so nothing may be claimed.
        if normalized or issue_count or entries or records:
            raise GovernmentProjectionError("GOV_PROJECTION_SNAPSHOT_STATE_INVALID")
        return NormalizationState(contract=contract, normalized_record_count=0,
                                  issue_count=0, issues={}, issue_records=())

    if normalized + issue_count != stored:
        raise GovernmentProjectionError("GOV_PROJECTION_SNAPSHOT_STATE_INVALID")

    issues: dict[str, int] = {}
    for entry in entries:
        if not isinstance(entry, Mapping) or set(entry) != _ISSUE_ENTRY_KEYS:
            raise GovernmentProjectionError("GOV_PROJECTION_SNAPSHOT_STATE_INVALID")
        reason, count = entry.get("reason"), _whole(entry.get("count"))
        if reason not in GOVERNMENT_NORMALIZATION_REASONS or reason in issues \
                or count is None or count < 1:
            raise GovernmentProjectionError("GOV_PROJECTION_SNAPSHOT_STATE_INVALID")
        issues[str(reason)] = count
    if sum(issues.values()) != issue_count:
        raise GovernmentProjectionError("GOV_PROJECTION_SNAPSHOT_STATE_INVALID")

    if any(not isinstance(record, str) or not _ISSUE_RECORD_PATTERN.fullmatch(record)
           for record in records) or len(set(records)) != len(records):
        raise GovernmentProjectionError("GOV_PROJECTION_SNAPSHOT_STATE_INVALID")
    # The list is bounded, so its length is decided: every refused row while
    # they fit, and exactly the bound once they do not.
    if len(records) != min(issue_count, MAX_DURABLE_ISSUE_RECORDS):
        raise GovernmentProjectionError("GOV_PROJECTION_SNAPSHOT_STATE_INVALID")
    return NormalizationState(contract=str(contract), normalized_record_count=normalized,
                              issue_count=issue_count, issues=dict(sorted(issues.items())),
                              issue_records=tuple(records))


def _bounded(limit: int) -> int:
    return max(1, min(int(limit), MAX_RESULT_ITEMS))


def _variant_sort_key(view: VariantView) -> tuple:
    """The ONE deterministic order of this projection.

    Python string comparison is codepoint order, which is the same on every
    platform and in every locale. The candidate key is the final tiebreak and
    is unique within a snapshot, so the order is total -- two runs over one
    snapshot cannot produce two orders.
    """
    return (view.manufacturer, view.commercial_model, view.model_year_start,
            view.model_year_end, view.official_model_code or "", view.trim or "",
            view.candidate_key)


def snapshot_usability(snapshot: Mapping[str, Any]) -> str | None:
    """Why this snapshot may not answer a query, or None if it may.

    Read from the snapshot's OWN durable metadata, and PARSED before it is
    read -- so usability is a property of a state that was checked, not of two
    fields that happened to look right.

    Module level because Catalog PR3's database-side reader
    (`backend/catalog/government/query.py`) applies the SAME gate. Two copies
    of "which snapshot may answer" is exactly the drift that would let one
    reader answer from a snapshot the other refuses.
    """
    metadata = snapshot.get("retrieval_metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    contract = metadata.get("normalization_contract")
    if contract not in (RAW_ONLY_CONTRACT, NORMALIZATION_CONTRACT):
        # No stated reading at all: a snapshot that cannot say what it is
        # missing is not a snapshot this layer will answer from.
        return "GOV_PROJECTION_SNAPSHOT_NOT_READ"
    try:
        state = parse_normalization_state(snapshot)
    except GovernmentProjectionError as refusal:
        return refusal.reason_code
    if state.contract == RAW_ONLY_CONTRACT:
        return "GOV_PROJECTION_RESOURCE_NOT_NORMALIZED"
    if state.issue_count > 0:
        return "GOV_PROJECTION_SNAPSHOT_INCOMPLETE"
    return None


def require_usable_snapshot(snapshot: Mapping[str, Any], *, allow_incomplete: bool) -> None:
    """Refuse a snapshot that may not answer, honouring the ONE acknowledgement."""
    reason = snapshot_usability(snapshot)
    if reason is None or (allow_incomplete and reason in ACKNOWLEDGEABLE_REFUSALS):
        return
    raise GovernmentProjectionError(reason)


def resolve_active_snapshot(repository: Any, *, resource_id: str, snapshot_key: str | None,
                            allow_incomplete: bool) -> Mapping[str, Any]:
    """The snapshot a Government read answers from, or a refusal.

    A PINNED key is resolved by an exact repository lookup, never by searching
    the bounded newest-first listing: that listing exists to choose the newest
    snapshot, and using it to resolve an explicit key made every active
    snapshot older than the bound unreachable.

    Without a pin, the newest USABLE snapshot answers. Usability is a property
    of the snapshot's own durable state, so a newer capture that is
    raw-complete but semantically incomplete does not displace the last usable
    one -- it is skipped, and the refusal that would otherwise be returned
    names the newest one's gap.

    STATED LIMITATION: the unpinned search covers the BOUNDED listing, so a
    usable snapshot sitting behind more than `MAX_CATALOG_SNAPSHOT_ROWS`
    unusable ones is not found by it. That is deliberate -- an unbounded scan
    is not a read this layer will perform -- and it is reachable by name
    through the exact lookup above, which has no such bound.
    """
    if snapshot_key is not None:
        pinned = repository.find_active_catalog_snapshot(
            src.GOVERNMENT_SOURCE_FAMILY, resource_id, snapshot_key)
        if pinned is None:
            raise GovernmentProjectionError("GOV_PROJECTION_SNAPSHOT_UNKNOWN")
        require_usable_snapshot(pinned, allow_incomplete=allow_incomplete)
        return pinned
    rows = repository.list_active_catalog_snapshots(
        src.GOVERNMENT_SOURCE_FAMILY, resource_id=resource_id)
    if not rows:
        raise GovernmentProjectionError("GOV_PROJECTION_NO_ACTIVE_SNAPSHOT")
    for row in rows:
        if snapshot_usability(row) is None:
            return row
    # Every active snapshot in the listing is unusable. The NEWEST one is held
    # to the rule, so the refusal names the state a reader would otherwise have
    # been answered from -- and an acknowledgement reaches exactly that
    # snapshot rather than an arbitrary older one.
    require_usable_snapshot(rows[0], allow_incomplete=allow_incomplete)
    return rows[0]


class GovernmentCatalogProjection:
    """The bounded internal read over one active Government snapshot."""

    def __init__(self, repository: Any, *, resource_id: str = src.WLTP_RESOURCE_ID,
                 snapshot_key: str | None = None,
                 max_candidates: int = MAX_PROJECTION_CANDIDATES,
                 allow_incomplete: bool = False,
                 cancellation_checker: Callable[[], bool] | None = None) -> None:
        self._repository = repository
        self._resource_id = src.require_allowed_resource(resource_id)
        self._snapshot_key = snapshot_key
        #: Read a snapshot whose reading gap is stated and counted anyway. The
        #: gap then travels on every answer; it is never a way to be handed an
        #: incomplete tree without knowing it.
        self._allow_incomplete = bool(allow_incomplete)
        self._max_candidates = int(max_candidates)
        self._cancellation_checker = cancellation_checker
        self._cache: tuple[DatasetProvenance, tuple[VariantView, ...],
                           Mapping[str, Mapping[str, Any]]] | None = None

    # --- the active snapshot -------------------------------------------------

    def _active_snapshot(self) -> Mapping[str, Any]:
        """The snapshot this projection answers from, or a refusal.

        One line, because the rule is `resolve_active_snapshot` above and is
        shared verbatim with the database-side reader.
        """
        return resolve_active_snapshot(self._repository, resource_id=self._resource_id,
                                       snapshot_key=self._snapshot_key,
                                       allow_incomplete=self._allow_incomplete)

    _usability = staticmethod(snapshot_usability)

    def _require_usable(self, snapshot: Mapping[str, Any]) -> None:
        require_usable_snapshot(snapshot, allow_incomplete=self._allow_incomplete)

    def _load(self) -> tuple[DatasetProvenance, tuple[VariantView, ...],
                             Mapping[str, Mapping[str, Any]]]:
        """Materialize one active snapshot, once, bounded and ordered."""
        if self._cache is not None:
            return self._cache
        snapshot = self._active_snapshot()
        state = parse_normalization_state(snapshot)
        records = {str(row["id"]): row
                   for row in self._read_all(self._repository.list_catalog_raw_records,
                                             snapshot["id"])}
        candidates = self._read_all(self._repository.list_catalog_candidates, snapshot["id"])
        self._require_candidates_match(state, candidates, records)
        provenance = _provenance_of(snapshot)
        views: list[VariantView] = []
        for candidate in candidates:
            self._check_cancelled()
            record = records.get(str(candidate.get("raw_record_id")))
            if record is None:
                # A candidate must name a row of its own snapshot; the schema
                # enforces it with a composite foreign key. Reaching this means
                # the read set is inconsistent, so the projection refuses
                # rather than answering without provenance.
                raise GovernmentProjectionError("GOV_PROJECTION_RECORD_MISSING")
            views.append(_variant_view(candidate, record, provenance))
        views.sort(key=_variant_sort_key)
        self._cache = (provenance, tuple(views),
                       {str(row["upstream_record_id"]): row for row in records.values()})
        return self._cache

    @staticmethod
    def _require_candidates_match(state: NormalizationState,
                                  candidates: Sequence[Mapping[str, Any]],
                                  records: Mapping[str, Mapping[str, Any]]) -> None:
        """The rows must BE what the recorded reading says they are.

        The summary is durable and the candidates are durable, and nothing in
        the schema ties the two together -- so a summary claiming 233 readings
        can sit above a snapshot holding none, and a projection that trusted it
        would answer an empty tree while reporting a complete capture.

        Three things are checked, and each catches something the others do not:

        *   the CARDINALITY -- as many candidates as the summary says were read;
        *   the DISTINCTNESS -- one raw record read at most once, so a dropped
            candidate cannot be hidden by a duplicated one while the count
            still balances;
        *   the OWNERSHIP -- every candidate names a raw record of this
            snapshot, so a count cannot be made up out of another capture's
            rows.

        The raw-only branch is defence in depth: `_active_snapshot` refuses such
        a snapshot outright, so this is unreachable through the ordinary path
        and exists so the invariant survives if that gate ever moves.
        """
        if state.contract == RAW_ONLY_CONTRACT:
            if candidates:
                raise GovernmentProjectionError("GOV_PROJECTION_SNAPSHOT_STATE_INVALID")
            return
        owners = [str(candidate.get("raw_record_id")) for candidate in candidates]
        if len(candidates) != state.normalized_record_count \
                or len(set(owners)) != len(owners) \
                or any(owner not in records for owner in owners):
            raise GovernmentProjectionError("GOV_PROJECTION_SNAPSHOT_STATE_INVALID")

    def _read_all(self, reader: Callable[..., Sequence[Mapping[str, Any]]],
                  snapshot_id: Any) -> list[Mapping[str, Any]]:
        """Page through ONE relation of one snapshot, or refuse.

        `MAX_PROJECTION_CANDIDATES` bounds each relation independently -- raw
        records and candidates are one-to-one here, so one number describes
        both. The bound is checked against what was READ, so a snapshot larger
        than the projection can hold is a refusal with its own reason and never
        a silently smaller answer.
        """
        rows: list[Mapping[str, Any]] = []
        offset = 0
        while True:
            self._check_cancelled()
            chunk = list(reader(snapshot_id, limit=READ_CHUNK, offset=offset))
            rows.extend(chunk)
            if len(rows) > self._max_candidates:
                raise GovernmentProjectionError("GOV_PROJECTION_BOUND_EXCEEDED")
            if len(chunk) < READ_CHUNK:
                return rows
            offset += READ_CHUNK

    # --- the read surface ----------------------------------------------------

    def dataset_metadata(self) -> DatasetProvenance:
        """What the active snapshot says about the dataset and about itself."""
        return self._load()[0]

    def list_manufacturers(self, *, limit: int = DEFAULT_RESULT_ITEMS,
                           offset: int = 0) -> ResultPage:
        provenance, views, _ = self._load()
        grouped: dict[str, tuple[set[str], int]] = {}
        for view in views:
            models, count = grouped.setdefault(view.manufacturer, (set(), 0))
            models.add(view.commercial_model)
            grouped[view.manufacturer] = (models, count + 1)
        items = [ManufacturerSummary(manufacturer=name, model_count=len(models),
                                     variant_count=count)
                 for name, (models, count) in sorted(grouped.items())]
        return self._page(items, limit, offset, provenance)

    def list_models(self, manufacturer: str, *, limit: int = DEFAULT_RESULT_ITEMS,
                    offset: int = 0) -> ResultPage:
        provenance, views, _ = self._load()
        grouped: dict[str, tuple[set[int], int]] = {}
        for view in views:
            if view.manufacturer != manufacturer:
                continue
            years, count = grouped.setdefault(view.commercial_model, (set(), 0))
            years.update(range(view.model_year_start, view.model_year_end + 1))
            grouped[view.commercial_model] = (years, count + 1)
        items = [ModelSummary(manufacturer=manufacturer, commercial_model=model,
                              variant_count=count, model_years=tuple(sorted(years)))
                 for model, (years, count) in sorted(grouped.items())]
        return self._page(items, limit, offset, provenance)

    def list_model_years(self, manufacturer: str, commercial_model: str, *,
                         limit: int = DEFAULT_RESULT_ITEMS, offset: int = 0) -> ResultPage:
        provenance, views, _ = self._load()
        grouped: dict[int, list[VariantView]] = {}
        for view in views:
            if view.manufacturer != manufacturer or view.commercial_model != commercial_model:
                continue
            for year in range(view.model_year_start, view.model_year_end + 1):
                grouped.setdefault(year, []).append(view)
        items = [ModelYearSummary(
                     manufacturer=manufacturer, commercial_model=commercial_model,
                     model_year=year, variant_count=len(group),
                     ambiguous_variant_count=sum(1 for item in group if item.status == "ambiguous"))
                 for year, group in sorted(grouped.items())]
        return self._page(items, limit, offset, provenance)

    def list_variants(self, manufacturer: str, commercial_model: str, *,
                      model_year: int | None = None,
                      limit: int = DEFAULT_RESULT_ITEMS, offset: int = 0) -> ResultPage:
        """Every variant of one model, and one year of it when asked.

        EVERY match is returned. A model year with several trims is several
        variants here, because that is what the register states; nothing
        collapses them and nothing picks one.
        """
        provenance, views, _ = self._load()
        items = [view for view in views
                 if view.manufacturer == manufacturer
                 and view.commercial_model == commercial_model
                 and (model_year is None
                      or view.model_year_start <= int(model_year) <= view.model_year_end)]
        return self._page(items, limit, offset, provenance)

    def find_by_model_code(self, official_model_code: str, *,
                           limit: int = DEFAULT_RESULT_ITEMS, offset: int = 0) -> ResultPage:
        """Exact model-code lookup. Exact: never a prefix and never a substring."""
        provenance, views, _ = self._load()
        items = [view for view in views if view.official_model_code == official_model_code]
        return self._page(items, limit, offset, provenance)

    def get_record(self, upstream_record_id: Any) -> RecordView:
        """One register row by its OWN `_id`, with every reading of it."""
        provenance, views, records = self._load()
        record = records.get(str(upstream_record_id))
        if record is None:
            raise GovernmentProjectionError("GOV_PROJECTION_RECORD_MISSING")
        return RecordView(
            upstream_record_id=str(record["upstream_record_id"]),
            raw_record_id=str(record["id"]), resource_id=str(record["resource_id"]),
            payload=dict(record["payload"]), payload_sha256=str(record["payload_sha256"]),
            source_locator=dict(record.get("source_locator") or {}), provenance=provenance,
            variants=tuple(view for view in views
                           if view.raw_record_id == str(record["id"])))

    def resolve_variant(self, manufacturer: str, commercial_model: str, model_year: int, *,
                        trim: str | None = None, official_model_code: str | None = None,
                        **dimensions: str) -> VariantResolution:
        """Narrow by what the caller STATES, and refuse to choose beyond it.

        A request that still matches two rows returns both. It does not return
        the first, and naming a record id could not break the tie either --
        allowing that would let a caller resolve a real ambiguity by fiat.
        """
        provenance, views, _ = self._load()
        matches = [view for view in views
                   if view.manufacturer == manufacturer
                   and view.commercial_model == commercial_model
                   and view.model_year_start <= int(model_year) <= view.model_year_end
                   and (trim is None or view.trim == trim)
                   and (official_model_code is None
                        or view.official_model_code == official_model_code)
                   and all(view.identity_dimensions.get(name) == value
                           for name, value in dimensions.items())]
        return VariantResolution(matches=tuple(matches), provenance=provenance)

    def government_tree(self, *, limit: int = DEFAULT_RESULT_ITEMS,
                        offset: int = 0) -> ResultPage:
        """`manufacturer -> model -> years -> variants`, in one ordered answer.

        Bounded and paginated over MANUFACTURERS, with each manufacturer's
        models, years and variants carried whole underneath it, so a page break
        never splits a model across two answers. Every value is derived from the
        same ordered variant list, so the same snapshot yields the same tree,
        byte for byte, every time.
        """
        provenance, views, _ = self._load()
        tree: dict[str, dict[str, dict[int, list[VariantView]]]] = {}
        for view in views:
            years = tree.setdefault(view.manufacturer, {}).setdefault(view.commercial_model, {})
            for year in range(view.model_year_start, view.model_year_end + 1):
                years.setdefault(year, []).append(view)
        items = [{"manufacturer": manufacturer,
                  "models": [{"commercial_model": model,
                              "model_years": [{"model_year": year,
                                               "variants": tuple(group)}
                                              for year, group in sorted(years.items())]}
                             for model, years in sorted(models.items())]}
                 for manufacturer, models in sorted(tree.items())]
        return self._page(items, limit, offset, provenance)

    # --- helpers -------------------------------------------------------------

    @staticmethod
    def _page(items: Sequence[Any], limit: int, offset: int,
              provenance: DatasetProvenance) -> ResultPage:
        bounded, start = _bounded(limit), max(0, int(offset))
        return ResultPage(items=tuple(items[start:start + bounded]), total=len(items),
                          offset=start, limit=bounded, provenance=provenance)

    def _check_cancelled(self) -> None:
        if self._cancellation_checker is not None and self._cancellation_checker():
            raise CancellationRequested("RUN_CANCELLED")


def _provenance_of(snapshot: Mapping[str, Any]) -> DatasetProvenance:
    """Read a snapshot row into provenance, using only what it stored.

    The reading gap comes from the PARSED state rather than from the raw
    metadata, so a provenance object can never carry a count this layer refused
    to believe.
    """
    metadata = snapshot.get("retrieval_metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    state = parse_normalization_state(snapshot)
    return DatasetProvenance(
        snapshot_id=str(snapshot["id"]), snapshot_key=str(snapshot["snapshot_key"]),
        source_family=str(snapshot["source_family"]), trust_state=str(snapshot["trust_state"]),
        resource_id=str(snapshot["resource_id"]),
        package_id=str(metadata.get("package_id") or ""),
        publisher=str(metadata.get("publisher") or ""),
        dataset_title=str(metadata.get("dataset_title") or ""),
        dataset_market_scope=str(metadata.get("dataset_market_scope") or ""),
        upstream_version=str(snapshot["upstream_version"]),
        upstream_version_kind=str(snapshot["upstream_version_kind"]),
        content_sha256=str(snapshot["content_sha256"]),
        schema_fingerprint=str(metadata.get("schema_fingerprint") or ""),
        page_chain_sha256=str(metadata.get("page_chain_sha256") or ""),
        # From the jsonb blob rather than a typed column, so it is read through
        # the same total helper the state parser uses: a value that is not a
        # whole number reports as 0 rather than raising a TypeError out of a
        # layer whose refusals are meant to be classifications.
        page_count=_whole(metadata.get("page_count")) or 0,
        declared_record_count=int(snapshot["declared_record_count"]),
        stored_record_count=int(snapshot["stored_record_count"]),
        retrieved_at=str(snapshot["retrieved_at"]), activated_at=str(snapshot["activated_at"]),
        query={str(key): str(value) for key, value in (metadata.get("query") or {}).items()},
        normalization_contract=state.contract,
        normalized_record_count=state.normalized_record_count,
        normalization_issue_count=state.issue_count,
        normalization_issues=dict(state.issues),
        normalization_issue_records=state.issue_records)


def _variant_view(candidate: Mapping[str, Any], record: Mapping[str, Any],
                  provenance: DatasetProvenance) -> VariantView:
    """One candidate joined to its raw record, with the reading re-derived.

    The displacement and the unresolved dimensions come from reading the
    PRESERVED payload again through the same normalizer that produced the
    candidate. Deterministic, so a projection cannot disagree with the
    ingestion that wrote the row; and if the payload is one the normalizer can
    no longer read, the candidate is still returned with its stored identity --
    the durable reading is the record of what was established.
    """
    try:
        reading = read_wltp_record(record["payload"], resource_id=str(record["resource_id"]))
    except (GovernmentNormalizationError, KeyError, TypeError):
        displacement, unresolved = None, ()
    else:
        displacement, unresolved = reading.engine_displacement_cc, reading.unresolved_dimensions
    return VariantView(
        candidate_id=str(candidate["id"]), candidate_key=str(candidate["candidate_key"]),
        status=str(candidate["status"]), manufacturer=str(candidate["manufacturer"]),
        commercial_model=str(candidate["commercial_model"]),
        model_year_start=int(candidate["model_year_start"]),
        model_year_end=int(candidate["model_year_end"]),
        official_model_code=candidate.get("official_model_code"),
        trim=candidate.get("trim"),
        identity_dimensions=dict(candidate.get("identity_dimensions") or {}),
        upstream_record_id=str(record["upstream_record_id"]),
        raw_record_id=str(record["id"]), resource_id=str(record["resource_id"]),
        source_locator=dict(record.get("source_locator") or {}),
        upstream_version=provenance.upstream_version,
        upstream_version_kind=provenance.upstream_version_kind,
        snapshot_key=provenance.snapshot_key,
        engine_displacement_cc=displacement, unresolved_dimensions=unresolved)


__all__ = ["ACKNOWLEDGEABLE_REFUSALS", "DEFAULT_RESULT_ITEMS",
           "GOVERNMENT_PROJECTION_REASONS", "NormalizationState",
           "parse_normalization_state", "provenance_of", "require_usable_snapshot",
           "resolve_active_snapshot", "snapshot_usability", "variant_sort_key",
           "MAX_PROJECTION_CANDIDATES", "MAX_RESULT_ITEMS", "UNMAPPED_FIELDS",
           "DatasetProvenance", "GovernmentCatalogProjection", "GovernmentProjectionError",
           "ManufacturerSummary", "ModelSummary", "ModelYearSummary", "RecordView",
           "ResultPage", "VariantResolution", "VariantView"]

#: Public aliases for the two pure readers Catalog PR3's database-side query
#: layer shares with this projection. Aliases rather than renames, so every
#: existing call site and every PR2 test keeps reading exactly as it did.
provenance_of = _provenance_of
variant_sort_key = _variant_sort_key

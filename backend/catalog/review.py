"""CODE-3: the bounded, read-only catalog REVIEW layer.

What this answers, and what it deliberately does not
----------------------------------------------------

Two questions, and only two:

1.  **"What is in the canonical catalog right now?"** -- a bounded page of
    `catalog_canonical_variant_current`.
2.  **"What is waiting for a human to look at it?"** -- a bounded page of the
    active Government WLTP snapshot's candidates whose durable status is
    exactly `ready_for_review`.

It is an INSPECTION layer. There is no approve, no reject, no promote, no edit,
no activation, no capture and no retry here, and there is no code path from any
function in this module to a repository method that writes. Every repository
method it may call is named in `READ_ONLY_REPOSITORY_METHODS` below, and
`tests/test_catalog_review_surface.py` holds the module to that list by
recording every call a request makes.

Why it is not `catalogStatus`'s question
----------------------------------------

CODE-2's run projection answers *"what did THIS RUN do to the catalog?"* from
the run's own event stream. This answers *"what durable state EXISTS?"* from
durable rows. They are different questions with different sources and different
lifetimes -- a run that promoted nothing and a catalog that holds nothing are
not the same fact -- so they are two contracts, never one reducer.

Why reading is not execution
----------------------------

Nothing here is gated on `MILO_ENABLE_CATALOG_EXECUTION`, and that is the
point. The catalog flag exists so an operator can stop the catalog WRITE path
mid-incident without stopping the product (`backend/catalog/execution.py`), and
it is explicitly non-destructive: the rows stay. An operator who has just
pulled that switch is exactly the person who most needs to see what is already
there. Gating this read behind it would make the rollback blind.

The same holds for `MILO_ENABLE_PAID_EXECUTION`, run creation, promotion,
Government egress and a `WorkerLease`: a durable read needs none of them, and
asking for one would be a false prerequisite. This module constructs no
transport, no client, no model gateway and no lease.

What may reach a browser
------------------------

Every value in a response passes through a CLOSED projection in this module.
The underlying rows are not returned, not merged into, and not iterated: each
projection names its fields, reads them by name, checks each one's type, and
drops anything that does not fit rather than coercing it. An unexpected stored
value therefore becomes an ABSENT field, never a fabricated one and never a raw
blob.

Never projected, from any row: raw payloads, response bodies, evidence
fragments, model text, SQL or database messages, lease tokens, worker ids,
credentials, internal row ids, or any jsonb this module has not itself walked.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from backend.catalog.contracts import CANDIDATE_IDENTITY_DIMENSIONS
from backend.errors import AppError
from backend.redaction import redact_secret_text

from .government import source as src
from .government.projection import DatasetProvenance, GovernmentProjectionError
from .government.query import (GovernmentCatalogQuery, TOTAL_COUNT_FIELD, is_count_row)

# ---------------------------------------------------------------------------
# Server-owned bounds. Every one of these is a literal here; none is reachable
# from a request, a payload, project metadata or an environment variable.
# ---------------------------------------------------------------------------

#: The hard maximum page size for BOTH review surfaces. A caller asking for
#: more receives this many -- there is no argument that raises it.
#:
#: Tighter than the database's own `catalog_page_limit()` (200) on purpose:
#: both bounds apply and the smaller one wins, so the durable bound stays the
#: backstop rather than the only bound.
#: `test_the_review_page_bound_is_within_the_database_bound` pins the relation.
MAX_REVIEW_PAGE_ITEMS = 100

#: The page size used when a request states none.
DEFAULT_REVIEW_PAGE_ITEMS = 25

#: The largest offset a request may state. A bound rather than a clamp: an
#: offset past this is a request nobody meant to make, and answering it with a
#: silently different one would be answering a different question.
MAX_REVIEW_OFFSET = 1_000_000

#: The longest a filter VALUE may be. The identity text this filters on is a
#: marque or a commercial model; a canonical key is 36 characters. Anything
#: past this is not one of those.
MAX_REVIEW_FILTER_CHARS = 120

#: The model-year range a filter may state. Outside it the value is not a model
#: year, and passing it to the database would be asking a question about a year
#: no register states.
MIN_REVIEW_MODEL_YEAR = 1900
MAX_REVIEW_MODEL_YEAR = 2200

#: The ONE candidate status this surface reads. Not a default and not a
#: caller-supplied filter: CODE-3 reviews what is waiting for review, and a
#: request cannot name another status.
REVIEW_CANDIDATE_STATUS = "ready_for_review"

#: Every repository method a CODE-3 request is permitted to call. All four are
#: reads. The list is here so a test can assert it, and so a reviewer can see
#: the whole surface in one place.
READ_ONLY_REPOSITORY_METHODS = (
    "get_project",
    "list_canonical_catalog_variants",
    "list_active_catalog_snapshots",
    "catalog_candidate_variant_page",
)

# ---------------------------------------------------------------------------
# Closed field allowlists.
# ---------------------------------------------------------------------------

#: What a canonical row may say to a browser. A strict subset of the view's
#: columns, chosen by what a REVIEWER needs to recognise the vehicle:
#:
#: `variant_id` and `model_id` are internal database ids and are absent --
#: `canonical_key` is the safe operational identifier, derived and ASCII
#: (`backend/catalog/keys.py`). `promoted_from_candidate_id` and
#: `promoted_from_verdict_id` are internal execution linkage. `field_revisions`
#: is internal revision bookkeeping and an unprojected jsonb map, so it is not
#: carried; per-field provenance is deliberately out of CODE-3's scope and is
#: recorded as future operator work.
CANONICAL_ITEM_FIELDS = ("canonical_key", "model_canonical_key", "manufacturer",
                         "commercial_model", "model_year_start", "model_year_end",
                         "official_model_code", "trim", "identity_dimensions",
                         "promoted_at", "revised_at")

#: What a candidate waiting for review may say. The candidate's stated identity
#: and its status, and nothing from the raw record it was read from: the page
#: query joins `upstream_record_id`, `resource_id`, `source_locator` and
#: `payload_sha256`, and none of them is projected here -- they describe where
#: a register row sat during a capture, which is capture-internal provenance
#: rather than something a reviewer reads. The snapshot context below states
#: what is being reviewed, once, for the whole page.
REVIEW_CANDIDATE_ITEM_FIELDS = ("candidate_key", "status", "manufacturer",
                                "commercial_model", "model_year_start",
                                "model_year_end", "official_model_code", "trim",
                                "identity_dimensions")

#: What the page may say about the snapshot it read. Reviewed, already
#: browser-safe metadata: the snapshot's own derived key, the public dataset
#: identifiers, the upstream version, the activation time, the record counts
#: and the snapshot's own stated reading gap. No content checksum, no page
#: chain, no retrieval query, no internal row id.
REVIEW_SNAPSHOT_FIELDS = ("snapshot_key", "resource_id", "package_id", "publisher",
                          "dataset_title", "dataset_market_scope", "upstream_version",
                          "upstream_version_kind", "activated_at",
                          "declared_record_count", "stored_record_count",
                          "normalization_contract", "normalization_issue_count")

# ---------------------------------------------------------------------------
# The typed unavailable state.
# ---------------------------------------------------------------------------

#: Why the review surface has no snapshot to read, as CODE-3's OWN closed
#: vocabulary. Deliberately not the `GOV_PROJECTION_*` codes themselves: those
#: are the Government reader's internal classifications, and a browser contract
#: that echoed them would inherit every future code that layer adds.
#:
#: An empty catalog and an unavailable one are different answers, and the
#: difference is this field. A page that is `available: false` states NO items
#: and a NULL total -- never `0`, which would be the claim that a snapshot was
#: read and found empty.
REVIEW_UNAVAILABLE_REASONS = ("no_active_snapshot", "snapshot_not_read",
                              "snapshot_not_normalized", "snapshot_incomplete",
                              "snapshot_state_invalid", "snapshot_unknown",
                              "snapshot_unavailable")

#: Government refusal code -> CODE-3 reason. Anything this map does not name
#: becomes `snapshot_unavailable`, so a code added upstream degrades to the
#: honest generic rather than escaping into the response.
#:
#: `GOV_QUERY_UNAVAILABLE` is deliberately ABSENT. It is the one code that is
#: not a statement about the snapshot -- it is the bounded reader collapsing a
#: repository failure -- and answering it with a snapshot reason would tell an
#: operator the catalog is empty when the database is simply unreachable.
#: `_REPOSITORY_REFUSAL` below handles it, and the two stay distinguishable.
_UNAVAILABLE_BY_PROJECTION_REASON: Mapping[str, str] = {
    "GOV_PROJECTION_NO_ACTIVE_SNAPSHOT": "no_active_snapshot",
    "GOV_PROJECTION_SNAPSHOT_NOT_READ": "snapshot_not_read",
    "GOV_PROJECTION_RESOURCE_NOT_NORMALIZED": "snapshot_not_normalized",
    "GOV_PROJECTION_SNAPSHOT_INCOMPLETE": "snapshot_incomplete",
    "GOV_PROJECTION_SNAPSHOT_STATE_INVALID": "snapshot_state_invalid",
    "GOV_PROJECTION_SNAPSHOT_UNKNOWN": "snapshot_unknown",
}

#: The bounded reader's code for "the repository refused", which is a different
#: condition from every snapshot state above and is reported as one.
_REPOSITORY_REFUSAL = "GOV_QUERY_UNAVAILABLE"


def _unavailable_repository() -> AppError:
    """The ONE sanitized refusal a repository failure becomes.

    `SupabaseRepository` raises `AppError("REPOSITORY_ERROR", str(exc), 502)`,
    and `str(exc)` is a PostgREST message that can quote SQL text and row
    values. That message is fine inside the server and must never be the body
    of a browser response, so every repository failure reaching this layer
    collapses onto one static, code-owned classification -- the same posture
    `GovernmentCatalogQuery._read` already takes for the candidate path.
    """
    return AppError("CATALOG_REVIEW_UNAVAILABLE",
                    "the catalog review read could not be answered", 502)


class CatalogReviewError(AppError):
    """A refusal carrying ONLY a static, code-owned message.

    A malformed request is told what was wrong with it in words authored here.
    Nothing a caller sent is echoed back, so a filter value cannot travel into
    an error string and out to a browser.
    """

    def __init__(self, code: str, message: str):
        super().__init__(code, message, 400)


# ---------------------------------------------------------------------------
# Request validation. Malformed is REFUSED, never silently ignored.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# The query contract: which parameter NAMES each route accepts.
# ---------------------------------------------------------------------------
#
# Independent review found the original surface treated an undeclared parameter
# as INERT -- the handlers declared their parameters, so anything else was
# discarded and the request answered as though it had not been sent. "Never
# read" was mistaken for "refused", and the two are not the same thing to the
# person reading the answer:
#
#     /catalog/review-candidates?snapshot_key=X
#
# returned the ACTIVE snapshot while the operator believed X had been
# inspected, and `?status=promoted` returned `ready_for_review` rows under a
# heading nobody asked for. Several of these names are real query controls one
# layer down -- `p_limit` and `p_status` are arguments of
# `catalog_candidate_variant_page`, `allow_incomplete` is the acknowledgement
# that lets an incomplete snapshot answer at all -- which is exactly why
# silence was the wrong answer.
#
# So the SET of names is now part of the contract, and anything outside it
# fails closed.

#: Everything the canonical route accepts. Nothing else, in any casing.
CANONICAL_QUERY_PARAMETERS = ("limit", "offset", "manufacturer",
                              "commercial_model", "model_year", "canonical_key")

#: Everything the review route accepts. A strict subset: a candidate has no
#: canonical key, so naming one here would be accepting a parameter that could
#: never mean anything.
REVIEW_QUERY_PARAMETERS = ("limit", "offset", "manufacturer",
                           "commercial_model", "model_year")

#: The ONE refusal an unsupported or ambiguous parameter produces. Static and
#: code-owned: the message below never quotes the name or the value, so the
#: refusal cannot be used to enumerate what the server recognises and cannot
#: reflect a caller's input back at it.
UNSUPPORTED_PARAMETER_CODE = "CATALOG_REVIEW_QUERY_PARAMETER_UNSUPPORTED"
UNSUPPORTED_PARAMETER_MESSAGE = (
    "this catalog review route accepts only its documented query parameters, "
    "each stated at most once"
)


def supported_query(params: Any, allowed: tuple[str, ...]) -> dict[str, str]:
    """The raw values of a request's query parameters, or a refusal.

    Two things are checked, and both fail onto the SAME static refusal so the
    answer carries no signal about which rule was broken:

    *   every name is in `allowed`. An unknown name is refused rather than
        dropped, because a dropped name is answered as though it had been
        honoured;
    *   no name is stated twice. `?limit=10&limit=20` is ambiguous, and no
        repository-wide policy defines a reviewed behaviour for a repeated
        query parameter -- `after_event_id` on the run-events route is the only
        other query parameter in the API and states none -- so there is nothing
        to follow, and selecting the first or the last would answer a question
        the caller did not unambiguously ask.

    Takes the multidict Starlette hands the handler, so it sees EVERY pair the
    client sent rather than the one value a declared parameter would have bound.
    """
    seen: dict[str, str] = {}
    for name, value in params.multi_items():
        if name not in allowed or name in seen:
            raise CatalogReviewError(UNSUPPORTED_PARAMETER_CODE,
                                     UNSUPPORTED_PARAMETER_MESSAGE)
        seen[name] = value
    return seen


def _whole_parameter(raw: str | None, label: str, code: str) -> int | None:
    """One query parameter as a whole number, or a refusal.

    Strict decimal text only. `int()` would accept `' 10 '`, `'+10'`, `'10_0'`
    and a Unicode digit, and a page bound that depends on Python's parsing
    quirks is not a bound anybody reviewed. Absent stays absent so the caller
    below can apply its own default.
    """
    if raw is None:
        return None
    if not raw.isascii() or not raw.lstrip("-").isdigit() or raw in ("-", ""):
        raise CatalogReviewError(code, f"{label} must be a whole number")
    return int(raw)


def canonical_query(params: Any) -> dict[str, Any]:
    """Validate and parse the canonical route's query string.

    Names first, then values. The ranges are applied further down by
    `canonical_catalog`, which is also reachable directly, so this layer adds
    parsing rather than replacing the bounds.
    """
    raw = supported_query(params, CANONICAL_QUERY_PARAMETERS)
    return {
        "limit": _whole_parameter(raw.get("limit"), "page size",
                                  "CATALOG_REVIEW_PAGE_INVALID"),
        "offset": _whole_parameter(raw.get("offset"), "offset",
                                   "CATALOG_REVIEW_PAGE_INVALID"),
        "manufacturer": raw.get("manufacturer"),
        "commercial_model": raw.get("commercial_model"),
        "model_year": _whole_parameter(raw.get("model_year"), "model year filter",
                                       "CATALOG_REVIEW_FILTER_INVALID"),
        "canonical_key": raw.get("canonical_key"),
    }


def review_query(params: Any) -> dict[str, Any]:
    """Validate and parse the review route's query string."""
    raw = supported_query(params, REVIEW_QUERY_PARAMETERS)
    return {
        "limit": _whole_parameter(raw.get("limit"), "page size",
                                  "CATALOG_REVIEW_PAGE_INVALID"),
        "offset": _whole_parameter(raw.get("offset"), "offset",
                                   "CATALOG_REVIEW_PAGE_INVALID"),
        "manufacturer": raw.get("manufacturer"),
        "commercial_model": raw.get("commercial_model"),
        "model_year": _whole_parameter(raw.get("model_year"), "model year filter",
                                       "CATALOG_REVIEW_FILTER_INVALID"),
    }


def bounded_page_size(limit: Any) -> int:
    """The page size for a request, or a refusal.

    A limit that is absent takes the default. A limit that is present must be a
    whole number in `[1, MAX_REVIEW_PAGE_ITEMS]`: zero, a negative, a fraction
    and a non-number are all refusals rather than clamps, because a caller who
    asked for -1 items did not ask for 25 and should be told so. A limit ABOVE
    the maximum is also a refusal, so the bound is visible to the caller rather
    than silently applied.
    """
    if limit is None:
        return DEFAULT_REVIEW_PAGE_ITEMS
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise CatalogReviewError("CATALOG_REVIEW_PAGE_INVALID",
                                 "page size must be a whole number")
    if limit < 1 or limit > MAX_REVIEW_PAGE_ITEMS:
        raise CatalogReviewError(
            "CATALOG_REVIEW_PAGE_INVALID",
            f"page size must be between 1 and {MAX_REVIEW_PAGE_ITEMS}")
    return limit


def bounded_offset(offset: Any) -> int:
    """The offset for a request, or a refusal. Same posture as the page size."""
    if offset is None:
        return 0
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise CatalogReviewError("CATALOG_REVIEW_PAGE_INVALID",
                                 "offset must be a whole number")
    if offset < 0 or offset > MAX_REVIEW_OFFSET:
        raise CatalogReviewError(
            "CATALOG_REVIEW_PAGE_INVALID",
            f"offset must be between 0 and {MAX_REVIEW_OFFSET}")
    return offset


def bounded_filter(value: Any, name: str) -> str | None:
    """One exact-match text filter, or a refusal.

    Exact, never a pattern: the value is compared with `=` by the database and
    is never interpolated, so there is no syntax for a caller to reach for. An
    empty or whitespace-only value is a refusal rather than "no filter" --
    `?manufacturer=` is a request that matched nothing, and answering it with
    the unfiltered catalog would answer a different question.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise CatalogReviewError("CATALOG_REVIEW_FILTER_INVALID",
                                 f"{name} filter must be text")
    if not value.strip():
        raise CatalogReviewError("CATALOG_REVIEW_FILTER_INVALID",
                                 f"{name} filter must not be empty")
    if len(value) > MAX_REVIEW_FILTER_CHARS:
        raise CatalogReviewError(
            "CATALOG_REVIEW_FILTER_INVALID",
            f"{name} filter must be at most {MAX_REVIEW_FILTER_CHARS} characters")
    return value


def bounded_model_year(value: Any) -> int | None:
    """One model-year filter, or a refusal."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise CatalogReviewError("CATALOG_REVIEW_FILTER_INVALID",
                                 "model year filter must be a whole number")
    if value < MIN_REVIEW_MODEL_YEAR or value > MAX_REVIEW_MODEL_YEAR:
        raise CatalogReviewError(
            "CATALOG_REVIEW_FILTER_INVALID",
            f"model year filter must be between {MIN_REVIEW_MODEL_YEAR} "
            f"and {MAX_REVIEW_MODEL_YEAR}")
    return value


# ---------------------------------------------------------------------------
# Closed value projection.
# ---------------------------------------------------------------------------

def _text(value: Any) -> str | None:
    """A stored value as browser text, redacted and bounded, or absent.

    Absent rather than `''` for anything that is not a non-empty string: an
    empty string renders as a field the row states and does not, which is the
    false-claim shape §9 of the CODE-3 contract forbids.

    `redact_secret_text` runs on the way OUT of the API, not only in the
    browser. Independent review made the reason explicit: the frontend parser
    redacts before it renders, but by then the response has already been
    delivered -- it has sat in the network panel and in anything else that
    observes traffic. A credential must not be SENT and then hidden.

    Redaction runs BEFORE truncation on purpose: truncating first could split a
    credential across the 120-character bound and leave a fragment the patterns
    no longer match. The same ordering, for the same reason, as the browser
    counterpart in `frontend/lib/sanitize.ts`.

    This is defense in depth over the closed field allowlists above, not a
    substitute for them. A manufacturer, a trim, a model code and a dataset
    title are all ordinary product data as far as the contract is concerned,
    and none of them is proof that a credential cannot be inside one.
    """
    if not isinstance(value, str):
        return None
    stripped = redact_secret_text(value).strip()
    if not stripped:
        return None
    return stripped[:MAX_REVIEW_FILTER_CHARS]


def _whole(value: Any) -> int | None:
    """A stored value as a whole number, or absent. `True` is not a year."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _dimensions(value: Any) -> dict[str, str]:
    """The identity dimensions a row states, through the CLOSED vocabulary.

    Never the stored object: a name outside `CANDIDATE_IDENTITY_DIMENSIONS` and
    a value that is not usable text are both DROPPED, so a jsonb column that
    somehow held something else cannot put it on a screen. Dropping rather than
    refusing is deliberate here -- the rest of the row is a real, reviewable
    identity, and losing it over one unreadable dimension would hide more than
    it protects.
    """
    if not isinstance(value, Mapping):
        return {}
    stated: dict[str, str] = {}
    for name in CANDIDATE_IDENTITY_DIMENSIONS:
        text = _text(value.get(name))
        if text is not None:
            stated[name] = text
    return stated


def canonical_item(row: Mapping[str, Any]) -> dict[str, Any]:
    """One canonical variant, projected onto `CANONICAL_ITEM_FIELDS`.

    Built key by key from a literal set. Nothing iterates the row, so a column
    added to the view later cannot appear here without an edit to this
    function.
    """
    return {
        "canonical_key": _text(row.get("canonical_key")),
        "model_canonical_key": _text(row.get("model_canonical_key")),
        "manufacturer": _text(row.get("manufacturer")),
        "commercial_model": _text(row.get("commercial_model")),
        "model_year_start": _whole(row.get("model_year_start")),
        "model_year_end": _whole(row.get("model_year_end")),
        "official_model_code": _text(row.get("official_model_code")),
        "trim": _text(row.get("trim")),
        "identity_dimensions": _dimensions(row.get("identity_dimensions")),
        "promoted_at": _text(row.get("promoted_at")),
        "revised_at": _text(row.get("revised_at")),
    }


def review_candidate_item(row: Any) -> dict[str, Any]:
    """One `ready_for_review` candidate, projected onto its closed allowlist.

    Takes the `CandidateVariantRow` the bounded query produced rather than a
    database row, so the register's raw record has already been left behind two
    layers up. The status is re-read from the row rather than assumed: this
    surface states what the durable row says, and a page that somehow held
    another status must show it rather than relabel it. The page-level guard in
    `review_candidates` refuses such a page outright.
    """
    return {
        "candidate_key": _text(getattr(row, "candidate_key", None)),
        "status": _text(getattr(row, "status", None)),
        "manufacturer": _text(getattr(row, "manufacturer", None)),
        "commercial_model": _text(getattr(row, "commercial_model", None)),
        "model_year_start": _whole(getattr(row, "model_year_start", None)),
        "model_year_end": _whole(getattr(row, "model_year_end", None)),
        "official_model_code": _text(getattr(row, "official_model_code", None)),
        "trim": _text(getattr(row, "trim", None)),
        "identity_dimensions": _dimensions(getattr(row, "identity_dimensions", None)),
    }


def review_snapshot(provenance: DatasetProvenance) -> dict[str, Any]:
    """The snapshot context a review page states, projected and bounded."""
    return {
        "snapshot_key": _text(provenance.snapshot_key),
        "resource_id": _text(provenance.resource_id),
        "package_id": _text(provenance.package_id),
        "publisher": _text(provenance.publisher),
        "dataset_title": _text(provenance.dataset_title),
        "dataset_market_scope": _text(provenance.dataset_market_scope),
        "upstream_version": _text(provenance.upstream_version),
        "upstream_version_kind": _text(provenance.upstream_version_kind),
        "activated_at": _text(provenance.activated_at),
        "declared_record_count": _whole(provenance.declared_record_count),
        "stored_record_count": _whole(provenance.stored_record_count),
        "normalization_contract": _text(provenance.normalization_contract),
        "normalization_issue_count": _whole(provenance.normalization_issue_count),
    }


# ---------------------------------------------------------------------------
# The two bounded pages.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CatalogPage:
    """One bounded answer, with everything the caller needs to page it.

    `total` is `None` when the database did not state one. That is "not
    reported", and it is carried as such rather than as `0`: a zero total beside
    a non-empty page would be a contradiction, and a zero total beside an empty
    page would be a claim the catalog is empty.

    `has_more` is NEVER `len(items) == limit`. It comes from the exact total
    when there is one, and when there is not, the page simply does not claim to
    know -- `None`, not `False`.
    """

    items: tuple[dict[str, Any], ...]
    limit: int
    offset: int
    total: int | None
    available: bool = True
    unavailable_reason: str | None = None
    snapshot: dict[str, Any] | None = None

    @property
    def has_more(self) -> bool | None:
        if self.total is None:
            return None
        return self.offset + len(self.items) < self.total


def _unavailable(reason: str, limit: int, offset: int) -> CatalogPage:
    """The honest empty answer: no items, NO total, and why."""
    if reason not in REVIEW_UNAVAILABLE_REASONS:  # pragma: no cover - defensive
        reason = "snapshot_unavailable"
    return CatalogPage(items=(), limit=limit, offset=offset, total=None,
                       available=False, unavailable_reason=reason, snapshot=None)


def canonical_catalog(repository: Any, *, manufacturer: Any = None,
                      commercial_model: Any = None, model_year: Any = None,
                      canonical_key: Any = None, limit: Any = None,
                      offset: Any = None) -> CatalogPage:
    """One bounded page of the CURRENT canonical catalog.

    Every filter is validated before the repository is touched, so a malformed
    request never reaches the database at all. The repository method decides the
    columns, the ordering and its own bound; this decides the values and the
    page, and neither can be named by a caller.

    The canonical catalog is GLOBAL rather than project-owned. The caller has
    already been authorized against a project it is a member of -- that is the
    authorization anchor, and it happens in `backend/main.py` before this is
    called. Nothing here pretends these rows belong to that project.
    """
    page_size = bounded_page_size(limit)
    start = bounded_offset(offset)
    filters = {"manufacturer": bounded_filter(manufacturer, "manufacturer"),
               "commercial_model": bounded_filter(commercial_model, "commercial model"),
               "model_year": bounded_model_year(model_year),
               "canonical_key": bounded_filter(canonical_key, "canonical key")}
    try:
        rows = repository.list_canonical_catalog_variants(limit=page_size, offset=start,
                                                          **filters)
    except AppError:
        # The repository's own message can quote SQL; only the classification
        # travels. `from None` so no cause is chained into a traceback that a
        # logger might one day serialize.
        raise _unavailable_repository() from None
    total = _stated_total(rows)
    items = tuple(canonical_item(row) for row in rows if not is_count_row(row))
    return CatalogPage(items=items, limit=page_size, offset=start, total=total)


def review_candidates(repository: Any, *, manufacturer: Any = None,
                      commercial_model: Any = None, model_year: Any = None,
                      limit: Any = None, offset: Any = None) -> CatalogPage:
    """One bounded page of candidates waiting for review.

    The snapshot is resolved by the repository's OWN trusted rule
    (`resolve_active_snapshot`), pinned to the reviewed WLTP resource constant.
    A caller states no resource, no host, no table and no snapshot: there is no
    argument here that could name one, so a browser cannot point this at
    anything else.

    `allow_incomplete` is deliberately NOT exposed. A snapshot whose own
    metadata records rows its vocabulary could not read answers nothing here
    and reports `snapshot_incomplete`; acknowledging that gap is an operator
    decision made by a capture, not a query string.
    """
    page_size = bounded_page_size(limit)
    start = bounded_offset(offset)
    filters = {"manufacturer": bounded_filter(manufacturer, "manufacturer"),
               "commercial_model": bounded_filter(commercial_model, "commercial model"),
               "model_year": bounded_model_year(model_year)}
    query = GovernmentCatalogQuery(repository, resource_id=src.WLTP_RESOURCE_ID,
                                   snapshot_key=None, allow_incomplete=False)
    try:
        page = query.list_variants(status=REVIEW_CANDIDATE_STATUS, limit=page_size,
                                   offset=start, **filters)
        snapshot = review_snapshot(query.dataset_metadata())
    except GovernmentProjectionError as refusal:
        if refusal.reason_code == _REPOSITORY_REFUSAL:
            raise _unavailable_repository() from None
        return _unavailable(
            _UNAVAILABLE_BY_PROJECTION_REASON.get(refusal.reason_code,
                                                  "snapshot_unavailable"),
            page_size, start)
    except AppError:
        # `resolve_active_snapshot` reads the snapshot listing directly rather
        # than through the bounded reader, so a repository failure can arrive
        # here as an `AppError` too.
        raise _unavailable_repository() from None
    # The status filter is applied by the database, and this asserts the answer
    # it gave. A page carrying any other status is a read set that disagrees
    # with its own filter, so it is refused WHOLE rather than filtered here --
    # quietly dropping the odd rows would publish a page that looks complete
    # and is not.
    if any(getattr(row, "status", None) != REVIEW_CANDIDATE_STATUS for row in page.items):
        raise AppError("CATALOG_REVIEW_UNAVAILABLE",
                       "the catalog review read is inconsistent", 502)
    return CatalogPage(items=tuple(review_candidate_item(row) for row in page.items),
                       limit=page_size, offset=start, total=page.total,
                       snapshot=snapshot)


def _stated_total(rows: Any) -> int | None:
    """The exact total a bounded listing stated, or None for "not reported"."""
    if not rows:
        return None
    return _whole(rows[0].get(TOTAL_COUNT_FIELD))


__all__ = ["CANONICAL_ITEM_FIELDS", "CANONICAL_QUERY_PARAMETERS", "CatalogPage",
           "CatalogReviewError",
           "DEFAULT_REVIEW_PAGE_ITEMS", "MAX_REVIEW_FILTER_CHARS",
           "MAX_REVIEW_MODEL_YEAR", "MAX_REVIEW_OFFSET", "MAX_REVIEW_PAGE_ITEMS",
           "MIN_REVIEW_MODEL_YEAR", "READ_ONLY_REPOSITORY_METHODS",
           "REVIEW_CANDIDATE_ITEM_FIELDS", "REVIEW_CANDIDATE_STATUS",
           "REVIEW_QUERY_PARAMETERS",
           "REVIEW_SNAPSHOT_FIELDS", "REVIEW_UNAVAILABLE_REASONS",
           "UNSUPPORTED_PARAMETER_CODE", "UNSUPPORTED_PARAMETER_MESSAGE",
           "bounded_filter", "bounded_model_year", "bounded_offset",
           "bounded_page_size", "canonical_catalog", "canonical_item",
           "canonical_query", "review_candidate_item", "review_candidates",
           "review_query", "review_snapshot", "supported_query"]

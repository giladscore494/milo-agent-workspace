"""R5: the read-only, fixture-backed proof tool for the Israeli registry.

The Government source family of the R5 proof is the Israeli Ministry of
Transport's official vehicle-model dataset `degem-rechev-wltp`, captured as
public unauthenticated CKAN `datastore_search` responses and committed
byte-for-byte. The tool below reads ONLY those committed response pages: no
network access, no credential, no CKAN query language, no SQL, no write
operation, and no registration in the production ToolRegistry.

The committed pages are one COMPLETE query
------------------------------------------

`q=RAV4&limit=100` over the pinned resource reports 233 matching rows, and the
datastore served them as three pages of 100, 100 and 33. All three are
committed, and `_pages` validates them as one result set before a single row
reaches the scan: the offsets must be exactly 0, 100 and 200, every page must
have been requested at the pinned page size and must echo the same query token
and resource id, every page must report the same total of 233, the pages must
hold exactly 100 + 100 + 33 rows, those rows must sum to the reported total,
and no `_id` may appear on two pages.

That gate exists because an incomplete capture is invisible from inside any one
page: each page honestly reports the full total, so two committed pages of a
three-page query look exactly like a complete query whose rows happen to number
200. A proof that scanned the first 200 of 233 rows would still answer -- and
would be answering from a prefix while its provenance named the whole query.
Here that is a refusal instead.

Selection is conservative, and deliberately so
----------------------------------------------

`get_model_record` narrows by the identity dimensions a caller states and then
requires the result to be UNIQUE. If two rows still match, it fails closed with
`R5_GOV_RECORD_AMBIGUOUS` instead of returning the first one -- and that is not
a defensive hypothetical here. In the committed pages, "Toyota RAV4, Israeli
market, plug-in hybrid, four-wheel drive" resolves to exactly one row for model
year 2021 and to exactly TWO rows for model year 2026, which differ only by
`ramat_gimur` (trim) -- a dimension the aggregated catalog this proof compares
against does not state at all. The 2026 question therefore has no conservative
answer, and the proof records that rather than inventing one.

`record_id` is an ASSERTION, never a selector. A caller may state the `_id` it
expects and have it checked against the row identity actually selected, but
naming an `_id` can never resolve an ambiguity: a request that matches two rows
fails whether or not it names one of them. Allowing otherwise would let the
caller pick a winner, which is exactly what the ambiguity refusal exists to
prevent.

Every vocabulary is closed and read from the record's OWN code field
----------------------------------------------------------------------

The dataset's meaning-bearing fields are Hebrew and code-backed: `delek_cd` 7
travels with `delek_nm` "חשמל/בנזין", `technologiat_hanaa_cd` 2 with "PLUG IN",
`hanaa_cd` 3 with "4X4". This module maps the CODE through a closed table and
then requires the record's own name field to be the name that code is paired
with everywhere in the captured data. A code this table does not name, or a
code whose name has drifted, fails closed. Nothing is guessed from a Hebrew
string, and nothing is inferred from a substring of a model name.

What is deliberately NOT read
-----------------------------

`koah_sus` (power) is captured and is NOT mapped. Across the committed plug-in
rows it takes 177, 185, 186, 302 and 324 for vehicles of the same commercial
model and drivetrain -- values that cannot all be the same quantity, and the
dataset publishes no definition saying which rows state engine output and which
state combined system output. A field whose semantics are unresolved in the
source is not evidence, so it is left out and reported as a real coverage gap
rather than mapped to `horsepower_hp` on the strength of its name.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from backend.catalog.government import vocabulary as _vocabulary
from backend.tools.contracts import ToolContext, ToolError, ToolMode, ToolOperation

from .manifest import ProofManifestError, load_fixture

#: The exact upstream identity this tool is pinned to.
CKAN_PACKAGE_ID = "degem-rechev-wltp"
WLTP_RESOURCE_ID = "142afde2-6228-49f9-8a29-9b6c3a0cbe40"

#: The committed pages of the pinned query, in the server's own pagination
#: order, each with the offset it was requested at and the number of rows it
#: returned. There is no operation that reads "the dataset": a call reads these
#: pages and nothing else, so the bound on a call is a constant of this module
#: rather than a property of whatever the caller asked for.
#:
#: These three pages are ONE query. `q=RAV4&limit=100` over the pinned resource
#: reports 233 matching rows and the datastore served them as 100 + 100 + 33.
#: Committing only the first two would leave the proof scanning a PREFIX of a
#: query whose provenance names the whole of it -- a gap that is invisible in
#: any single page, because every page reports the same honest total. So the
#: plan is checked as a whole on every call: a missing page, a gap or overlap
#: in the offsets, a page that answered a different query or resource, a total
#: that disagrees between pages, a short or long page, a sum that is not the
#: reported total, and a row that appears on two pages are each a refusal
#: rather than a quietly smaller scan.
WLTP_PAGE_PLAN: tuple[tuple[str, int, int], ...] = (
    ("government_wltp_page_1", 0, 100),
    ("government_wltp_page_2", 100, 100),
    ("government_wltp_page_3", 200, 33),
)

#: The page source keys alone, in the same pagination order.
WLTP_PAGE_SOURCE_KEYS: tuple[str, ...] = tuple(key for key, _, _ in WLTP_PAGE_PLAN)

#: The page size every page of the pinned query was requested at.
WLTP_PAGE_LIMIT = 100

#: The query token the pinned pages answer. A page captured for a different
#: query is not part of THIS query's result set, whatever else it holds.
WLTP_QUERY_TOKEN = "RAV4"

#: The row count the datastore itself reports for the pinned query, on every
#: page. The committed pages must sum to exactly this.
WLTP_REPORTED_TOTAL = 233

#: The closed vocabulary of pagination refusals. Static and code-owned: a
#: refusal names which property of the committed result set failed, and never
#: carries an offset, a row or a page a caller could read back out of it.
R5_GOV_PAGINATION_REASONS = frozenset({
    "R5_GOV_PAGE_MISSING",
    "R5_GOV_PAGE_OFFSET_UNEXPECTED",
    "R5_GOV_PAGE_QUERY_MISMATCH",
    "R5_GOV_PAGE_COUNT_UNEXPECTED",
    "R5_GOV_PAGE_TOTAL_INCONSISTENT",
    "R5_GOV_PAGINATION_INCOMPLETE",
    "R5_GOV_RECORD_ID_DUPLICATED",
})

#: The dataset metadata page, read only for the publisher and the scope the
#: dataset states about ITSELF.
PACKAGE_SOURCE_KEY = "government_package"

#: The market this dataset is authoritative for. It is a property of the
#: SOURCE, not of a row: the publisher is the Israeli Ministry of Transport and
#: the dataset is the register of vehicle models approved for the Israeli
#: market, so no row carries a market field and none is invented. A request for
#: any other market is refused rather than answered from this source.
GOVERNMENT_DATASET_MARKET = "IL"

#: The publisher the package metadata must still name. A dataset that changed
#: hands is not the source this proof reviewed.
GOVERNMENT_PUBLISHER = "ministry_of_transport"

#: The bound on how many committed rows one call may consider.
MAX_SCANNED_RECORDS = 400

#: `tozar` (make) -> the canonical make name. Closed: the captured pages hold
#: exactly one make, and a make this table does not name fails closed rather
#: than being transliterated at run time.
MAKE_BY_TOZAR: Mapping[str, str] = {"טויוטה": "Toyota"}

# The register's own code/label semantics live in ONE place --
# `backend/catalog/government/vocabulary.py`, in the production catalog
# namespace -- because Catalog PR2 reads the same fields of the same resource
# and two copies of a semantic table are two things that can drift.
#
# What this proof keeps is its own KEY SET. Every table below SELECTS exactly
# the codes R5 reviewed against the committed capture, so the shared module may
# grow for a wider capture without changing which rows this tool decodes: this
# selection is conservative, and a table that silently widened could turn a
# settled unique match into an ambiguity. A change to the MEANING of a code
# this proof does read breaks here immediately, which is the point.

#: `delek_cd` -> (fuel type, the `delek_nm` that code is paired with). The code
#: decides; the name is a cross-check that the dataset's own pairing still
#: holds. `7` is electricity/petrol -- a plug-in hybrid, not a "hybrid".
FUEL_BY_CODE: Mapping[int, tuple[str, str]] = {
    code: _vocabulary.FUEL_BY_CODE[code] for code in (1, 7)
}

#: `technologiat_hanaa_cd` -> (propulsion technology, its paired name). The
#: conventional-drive rows carry NO code at all, so they are absent here and a
#: request can never select one by propulsion.
PROPULSION_BY_CODE: Mapping[int, tuple[str, str]] = {
    code: _vocabulary.PROPULSION_BY_CODE[code] for code in (1, 2)
}

#: `hanaa_cd` -> (drivetrain, its paired name). `לא ידוע קוד` ("unknown code")
#: is deliberately absent: a row that does not state its drivetrain must not
#: match a request that does.
DRIVETRAIN_BY_CODE: Mapping[int, tuple[str, str]] = {
    code: _vocabulary.DRIVETRAIN_BY_CODE[code] for code in (1, 3)
}

#: `merkav` (body) -> body style. Closed, and read only as an R4 identity
#: dimension.
BODY_STYLE_BY_MERKAV: Mapping[str, str] = {
    body: _vocabulary.BODY_STYLE_BY_MERKAV[body] for body in ("פנאי-שטח",)
}

#: Fuel and propulsion are two independent statements about one row, and a row
#: whose two statements disagree is not material this proof will read. Only
#: these pairings occur in the captured data; anything else fails closed.
CONSISTENT_FUEL_PROPULSION: frozenset[tuple[str, str]] = \
    _vocabulary.CONSISTENT_FUEL_PROPULSION

_RECORD_REQUEST = {
    "type": "object",
    "properties": {
        "resource_id": {"type": "string"},
        # The identity a caller must state.
        "make": {"type": "string"},
        "commercial_model": {"type": "string"},
        "market": {"type": "string"},
        "model_year": {"type": "integer"},
        # The narrowing dimensions. Optional so an under-specified request
        # stays ambiguous instead of resolving to whichever row comes first.
        "fuel_type": {"type": "string"},
        "propulsion_technology": {"type": "string"},
        "drivetrain": {"type": "string"},
        "trim": {"type": "string"},
        # An ASSERTION about the row the identity above selects. Checked after
        # selection; it never selects and never resolves an ambiguity.
        "expected_record_id": {"type": "integer"},
    },
    "required": ["commercial_model", "make", "market", "model_year", "resource_id"],
    "additionalProperties": False,
}

_RECORD_RESULT = {
    "type": "object",
    "properties": {
        "source": {
            "type": "object",
            "properties": {
                "resource_id": {"type": "string"},
                "ckan_package_id": {"type": "string"},
                "publisher": {"type": "string"},
                "dataset_title": {"type": "string"},
                "dataset_version": {"type": "string"},
                "dataset_version_kind": {"type": "string"},
                "canonical_url": {"type": "string"},
                "retrieved_at_utc": {"type": "string"},
                "response_sha256": {"type": "string"},
                "query": {"type": "string"},
            },
            "required": ["canonical_url", "ckan_package_id", "dataset_title",
                         "dataset_version", "dataset_version_kind", "publisher", "query",
                         "resource_id", "response_sha256", "retrieved_at_utc"],
            "additionalProperties": False,
        },
        "record_id": {"type": "integer"},
        "durable_record_id": {"type": "string"},
        "record_locator": {
            "type": "object",
            "properties": {"fixture_path": {"type": "string"},
                           "record_index": {"type": "integer"},
                           "government_id": {"type": "integer"}},
            "required": ["fixture_path", "government_id", "record_index"],
            "additionalProperties": False,
        },
        "model": {
            "type": "object",
            "properties": {"make": {"type": "string"},
                           "commercial_model": {"type": "string"},
                           "market": {"type": "string"},
                           "model_year": {"type": "integer"}},
            "required": ["commercial_model", "make", "market", "model_year"],
            "additionalProperties": False,
        },
        "variant": {
            "type": "object",
            "properties": {
                # An exact homologated displacement in cubic centimetres --
                # never the nominal engine-class label an aggregated catalog
                # states, and deliberately not named as though it were one.
                "engine_displacement_cc": {"type": "integer"},
                "fuel_type": {"type": "string"},
                "propulsion_technology": {"type": "string"},
                "official_model_code": {"type": "string"},
                "drivetrain": {"type": "string"},
                "body_style": {"type": "string"},
                "trim": {"type": "string"},
            },
            "required": ["body_style", "drivetrain", "engine_displacement_cc", "fuel_type",
                         "official_model_code", "propulsion_technology", "trim"],
            "additionalProperties": False,
        },
        # The record's OWN field names and OWN values, for exactly the fields
        # this proof reads. A durable locator must name the field the SOURCE
        # has, and a durable fragment must quote what the source actually
        # wrote -- "delek_nm=חשמל/בנזין", not this module's reading of it --
        # so the raw material travels beside the decoded one.
        "upstream_fields": {
            "type": "object",
            "properties": {
                "tozar": {"type": "string"},
                "kinuy_mishari": {"type": "string"},
                "shnat_yitzur": {"type": "integer"},
                "nefah_manoa": {"type": "integer"},
                "delek_cd": {"type": "integer"},
                "delek_nm": {"type": "string"},
                "technologiat_hanaa_cd": {"type": "integer"},
                "technologiat_hanaa_nm": {"type": "string"},
                "degem_nm": {"type": "string"},
                "hanaa_cd": {"type": "integer"},
                "hanaa_nm": {"type": "string"},
                "merkav": {"type": "string"},
                "ramat_gimur": {"type": "string"},
            },
            "required": ["degem_nm", "delek_cd", "delek_nm", "hanaa_cd", "hanaa_nm",
                         "kinuy_mishari", "merkav", "nefah_manoa", "ramat_gimur",
                         "shnat_yitzur", "technologiat_hanaa_cd", "technologiat_hanaa_nm",
                         "tozar"],
            "additionalProperties": False,
        },
        # The fields this proof does NOT map out of the record it just read,
        # each with the reason. Reported so a real gap travels with the
        # evidence instead of being invisible.
        "unmapped_fields": {
            "type": "array", "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {"field": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["field", "reason"], "additionalProperties": False,
            },
        },
    },
    "required": ["durable_record_id", "model", "record_id", "record_locator", "source",
                 "unmapped_fields", "upstream_fields", "variant"],
    "additionalProperties": False,
}

#: Why a captured field of the selected row is not evidence. Static: the reason
#: is written by a reviewer, never derived from the row in front of the tool.
#: The three this proof reports are named explicitly and in this order -- the
#: reasons are the shared module's, the SELECTION is this proof's, so a field
#: added there for a wider capture does not silently join this tool's result.
UNMAPPED_FIELDS: tuple[tuple[str, str], ...] = _vocabulary.unmapped_fields(
    "koah_sus", "dg_metach_solela", "mishkal_kolel")


#: The record's own field names this proof copies verbatim. Closed and static:
#: a row that grows a new field contributes nothing new until a reviewer adds
#: it to the shared vocabulary, and the durable locators can only ever name one
#: of these. Deliberately the WHOLE shared list rather than a selection: this
#: proof and the catalog normalizer read exactly the same fields of the same
#: resource, and `_RECORD_RESULT` below names all thirteen in a closed schema,
#: so a widening there fails this suite loudly rather than passing silently.
UPSTREAM_FIELDS: tuple[str, ...] = _vocabulary.GOVERNMENT_IDENTITY_FIELDS


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _as_int(value: Any) -> int | None:
    """Read an integer the source states as a number or as a digit string.

    The manifest records a captured query exactly as it was sent -- as URL
    parameter strings -- while the response echoes the same values as JSON
    numbers. Both are read here and nothing else is, so a float, a boolean or
    any other text can never compare equal to a page's position in the plan.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


@dataclass(frozen=True)
class GovernmentVehicleRegistryTool:
    """The pinned Israeli vehicle-model registry, as ONE bounded lookup.

    `get_model_record` answers with exactly one row or refuses. There is no
    list operation, no free-text search and no way to ask for the dataset, so
    the 101,476-row upstream resource can never cross this boundary even in
    principle -- only the 233 rows of the one pinned query exist here at all,
    committed as the three response pages the datastore served them in.
    """

    name: str = "gov_il.vehicle_registry"
    description: str = ("Pinned Israeli Ministry of Transport vehicle-model registry; "
                        "one exact homologated model record per call")
    required_scope: str = "gov_il:registry_read"
    mode: ToolMode = ToolMode.READ
    operations = {
        "get_model_record": ToolOperation(
            "get_model_record",
            "Return one exact registry record for a stated vehicle identity, or fail closed.",
            _RECORD_REQUEST, _RECORD_RESULT),
    }

    def execute(self, context: ToolContext, operation: str,
                payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if str(payload["resource_id"]) != WLTP_RESOURCE_ID:
            raise ToolError("R5_GOV_RESOURCE_UNKNOWN",
                            "no committed response page belongs to this resource",
                            tool=self.name)
        if str(payload["market"]).strip().upper() != GOVERNMENT_DATASET_MARKET:
            # The dataset's scope is the Israeli market. It states nothing
            # about any other, so it must answer for no other.
            raise ToolError("R5_GOV_MARKET_OUT_OF_SCOPE",
                            "this dataset is scoped to the Israeli market only",
                            tool=self.name)
        package = self._package()
        candidates = self._candidates(payload)
        if not candidates:
            raise ToolError("R5_GOV_RECORD_NOT_FOUND",
                            "no committed registry record matches this identity",
                            tool=self.name)
        if len(candidates) > 1:
            # The whole point of the narrowing dimensions. Two rows of one
            # commercial model, year, fuel and drivetrain that differ only by a
            # dimension the request did not state are two different variants,
            # and this operation will not choose between them.
            raise ToolError("R5_GOV_RECORD_AMBIGUOUS",
                            "this identity matches more than one registry record",
                            tool=self.name)
        entry, index, record, projected = candidates[0]
        expected = payload.get("expected_record_id")
        if expected is not None and int(expected) != int(record["_id"]):
            raise ToolError("R5_GOV_RECORD_ID_MISMATCH",
                            "the selected registry record is not the asserted record",
                            tool=self.name)
        return self._project(entry, index, record, projected, package, payload)

    # --- reading the committed pages -----------------------------------------

    def _package(self) -> Mapping[str, Any]:
        """The dataset's own metadata, checksum-gated and publisher-checked."""
        try:
            _, document = load_fixture(PACKAGE_SOURCE_KEY)
        except ProofManifestError as failure:
            raise ToolError(failure.reason_code, failure.safe_message, tool=self.name) from None
        result = document.get("result")
        if not isinstance(result, Mapping) or result.get("name") != CKAN_PACKAGE_ID:
            raise ToolError("R5_GOV_FIXTURE_INVALID",
                            "the committed dataset metadata is not the pinned package",
                            tool=self.name)
        organization = result.get("organization")
        if not isinstance(organization, Mapping) or \
                organization.get("name") != GOVERNMENT_PUBLISHER:
            raise ToolError("R5_GOV_FIXTURE_INVALID",
                            "the committed dataset is not published by the expected ministry",
                            tool=self.name)
        resource = next((item for item in result.get("resources") or []
                         if isinstance(item, Mapping) and item.get("id") == WLTP_RESOURCE_ID),
                        None)
        if resource is None or not _text(resource.get("last_modified")):
            raise ToolError("R5_GOV_FIXTURE_INVALID",
                            "the pinned resource states no version to read evidence at",
                            tool=self.name)
        return {"title": str(result.get("title") or CKAN_PACKAGE_ID),
                "publisher": GOVERNMENT_PUBLISHER,
                "dataset_version": str(resource["last_modified"])}

    def _pages(self):
        """The complete pinned query, checksum-gated and validated as ONE set.

        Each page is re-hashed against the manifest, then held to the position
        it occupies in `WLTP_PAGE_PLAN`: the offset it was requested at, the
        page size, the query token and resource it answered, the total the
        datastore reported, and the number of rows it actually returned. The
        pages are then checked TOGETHER -- they must sum to that reported total
        and must share no `_id`.

        Eager on purpose. A generator would let the caller select a record from
        page one while a later page was still unvalidated, so the whole result
        set is proven complete BEFORE the first row is offered to the scan.
        """
        pages: list[tuple[Mapping[str, Any], list[Any]]] = []
        seen: set[int] = set()
        for source_key, offset, expected_count in WLTP_PAGE_PLAN:
            try:
                entry, document = load_fixture(source_key)
            except ProofManifestError as failure:
                # A page the manifest does not describe at all is the one
                # failure that means the committed query is INCOMPLETE rather
                # than corrupt, and it is reported as its own refusal.
                if failure.reason_code == "R5_MANIFEST_SOURCE_UNKNOWN":
                    raise ToolError("R5_GOV_PAGE_MISSING",
                                    "a page of the pinned query is not committed",
                                    tool=self.name) from None
                raise ToolError(failure.reason_code, failure.safe_message,
                                tool=self.name) from None
            result = document.get("result")
            records = result.get("records") if isinstance(result, Mapping) else None
            if not isinstance(records, list) or not records:
                raise ToolError("R5_GOV_FIXTURE_INVALID",
                                "a committed response page states no records", tool=self.name)
            if str(entry.get("resource_id")) != WLTP_RESOURCE_ID:
                raise ToolError("R5_GOV_FIXTURE_INVALID",
                                "a committed response page belongs to another resource",
                                tool=self.name)
            self._check_page(entry, result, records, offset, expected_count, seen)
            pages.append((entry, records))

        scanned = sum(len(records) for _, records in pages)
        if scanned != WLTP_REPORTED_TOTAL or len(seen) != WLTP_REPORTED_TOTAL:
            # Every page agreed on the total and every page was the size the
            # plan expects, yet the committed pages do not add up to it: the
            # result set is not the query it claims to be.
            raise ToolError("R5_GOV_PAGINATION_INCOMPLETE",
                            "the committed pages are not the whole pinned query",
                            tool=self.name)
        return tuple(pages)

    def _check_page(self, entry: Mapping[str, Any], result: Mapping[str, Any],
                    records: list[Any], offset: int, expected_count: int,
                    seen: set[int]) -> None:
        """Hold ONE page to its place in the query, or fail closed.

        The manifest entry and the response body are checked against the plan
        AND against each other, so neither an edited manifest nor an edited
        body can move a page, resize it, or re-point it at another query on its
        own. The row identities are accumulated across pages as they are read,
        which is what makes a row that appears twice a refusal rather than two
        candidates that look like an ambiguity.
        """
        declared = entry.get("query")
        declared = declared if isinstance(declared, Mapping) else {}
        locator = entry.get("record_locator")
        locator = locator if isinstance(locator, Mapping) else {}
        page = locator.get("page")
        page = page if isinstance(page, Mapping) else {}
        # Three independent statements about where this page sits: the query
        # the capture RECORDED sending, the position the manifest's locator
        # records, and what the response itself SAYS it answered. All three
        # must be this page's place in the plan, so moving a page takes an
        # edit to the body AND to two places in its provenance.
        if _as_int(page.get("requested_offset")) != offset or \
                _as_int(page.get("requested_limit")) != WLTP_PAGE_LIMIT or \
                _as_int(page.get("returned_record_count")) != expected_count or \
                _as_int(page.get("reported_total")) != WLTP_REPORTED_TOTAL:
            raise ToolError("R5_GOV_PAGE_OFFSET_UNEXPECTED",
                            "a committed page does not record the position the query needs",
                            tool=self.name)
        if _text(page.get("query_token")) != WLTP_QUERY_TOKEN or \
                str(page.get("resource_id")) != WLTP_RESOURCE_ID:
            raise ToolError("R5_GOV_PAGE_QUERY_MISMATCH",
                            "a committed page records a different query or resource",
                            tool=self.name)
        if _as_int(declared.get("offset")) != offset or _as_int(result.get("offset")) != offset:
            raise ToolError("R5_GOV_PAGE_OFFSET_UNEXPECTED",
                            "a committed page is not at the offset the pinned query needs",
                            tool=self.name)
        if _as_int(declared.get("limit")) != WLTP_PAGE_LIMIT or \
                _as_int(result.get("limit")) != WLTP_PAGE_LIMIT:
            raise ToolError("R5_GOV_PAGE_OFFSET_UNEXPECTED",
                            "a committed page was not requested at the pinned page size",
                            tool=self.name)
        if _text(declared.get("q")) != WLTP_QUERY_TOKEN or \
                _text(result.get("q")) != WLTP_QUERY_TOKEN or \
                str(declared.get("resource_id")) != WLTP_RESOURCE_ID or \
                str(result.get("resource_id")) != WLTP_RESOURCE_ID:
            raise ToolError("R5_GOV_PAGE_QUERY_MISMATCH",
                            "a committed page answers a different query or resource",
                            tool=self.name)
        if _as_int(result.get("total")) != WLTP_REPORTED_TOTAL:
            raise ToolError("R5_GOV_PAGE_TOTAL_INCONSISTENT",
                            "a committed page reports a different total for the pinned query",
                            tool=self.name)
        if len(records) != expected_count:
            raise ToolError("R5_GOV_PAGE_COUNT_UNEXPECTED",
                            "a committed page does not hold the rows the pinned query needs",
                            tool=self.name)
        for record in records:
            identity = record.get("_id") if isinstance(record, Mapping) else None
            if not isinstance(identity, int) or isinstance(identity, bool):
                raise ToolError("R5_GOV_FIXTURE_INVALID",
                                "a committed row states no registry identity", tool=self.name)
            if identity in seen:
                # One row reachable twice would be two candidates for one
                # vehicle -- an ambiguity manufactured by the pagination, not
                # stated by the register.
                raise ToolError("R5_GOV_RECORD_ID_DUPLICATED",
                                "one registry row appears on more than one committed page",
                                tool=self.name)
            seen.add(identity)

    def _candidates(self, payload: Mapping[str, Any]):
        """Every committed row matching the STATED identity, and only those."""
        wanted_make = _text(payload["make"])
        wanted_model = _text(payload["commercial_model"])
        model_year = payload["model_year"]
        found, scanned = [], 0
        for entry, records in self._pages():
            for index, record in enumerate(records):
                scanned += 1
                if scanned > MAX_SCANNED_RECORDS:
                    raise ToolError("R5_GOV_SCAN_BOUND_EXCEEDED",
                                    "the committed pages exceed the scan bound",
                                    tool=self.name)
                if not isinstance(record, Mapping) or \
                        not isinstance(record.get("_id"), int) or \
                        record.get("shnat_yitzur") != model_year or \
                        _text(record.get("kinuy_mishari")) != wanted_model or \
                        MAKE_BY_TOZAR.get(str(record.get("tozar"))) != wanted_make:
                    continue
                projected = self._decode(record)
                if projected is None or not self._matches(projected, record, payload):
                    continue
                found.append((entry, index, record, projected))
        return found

    def _decode(self, record: Mapping[str, Any]) -> dict[str, Any] | None:
        """Read one row through the closed vocabularies, or skip it.

        A row whose code is unknown, whose paired name has drifted, or whose
        fuel and propulsion statements disagree is not a candidate. It is
        skipped rather than raised on: another row may still answer the
        request, and a dataset-wide quality problem must not be reported as if
        the CALLER had asked for something invalid.
        """
        def coded(code_field: str, name_field: str, table: Mapping[int, tuple[str, str]]):
            code = record.get(code_field)
            if isinstance(code, bool) or not isinstance(code, int):
                return None
            paired = table.get(code)
            if paired is None or _text(record.get(name_field)) != paired[1]:
                return None
            return paired[0]

        fuel = coded("delek_cd", "delek_nm", FUEL_BY_CODE)
        propulsion = coded("technologiat_hanaa_cd", "technologiat_hanaa_nm", PROPULSION_BY_CODE)
        drivetrain = coded("hanaa_cd", "hanaa_nm", DRIVETRAIN_BY_CODE)
        body_style = BODY_STYLE_BY_MERKAV.get(str(record.get("merkav")))
        displacement = record.get("nefah_manoa")
        model_code, trim = _text(record.get("degem_nm")), _text(record.get("ramat_gimur"))
        if None in (fuel, propulsion, drivetrain, body_style, model_code, trim) or \
                isinstance(displacement, bool) or not isinstance(displacement, int) or \
                displacement <= 0:
            return None
        if (fuel, propulsion) not in CONSISTENT_FUEL_PROPULSION:
            return None
        return {"engine_displacement_cc": displacement, "fuel_type": fuel,
                "propulsion_technology": propulsion, "official_model_code": model_code,
                "drivetrain": drivetrain, "body_style": body_style, "trim": trim}

    @staticmethod
    def _matches(projected: Mapping[str, Any], record: Mapping[str, Any],
                 payload: Mapping[str, Any]) -> bool:
        """Narrow by the dimensions the CALLER stated, and only those."""
        for selector in ("fuel_type", "propulsion_technology", "drivetrain", "trim"):
            wanted = payload.get(selector)
            if wanted is not None and projected[selector] != _text(wanted):
                return False
        return True

    # --- building the bounded result -----------------------------------------

    def _project(self, entry: Mapping[str, Any], index: int, record: Mapping[str, Any],
                 projected: Mapping[str, Any], package: Mapping[str, Any],
                 payload: Mapping[str, Any]) -> dict[str, Any]:
        government_id = int(record["_id"])
        return {
            "source": {
                "resource_id": WLTP_RESOURCE_ID, "ckan_package_id": CKAN_PACKAGE_ID,
                "publisher": str(package["publisher"]),
                "dataset_title": str(package["title"]),
                "dataset_version": str(package["dataset_version"]),
                "dataset_version_kind": "dataset_version",
                "canonical_url": str(entry["canonical_url"]),
                "retrieved_at_utc": str(entry["retrieved_at_utc"]),
                "response_sha256": str(entry["upstream_sha256"]),
                "query": json.dumps(dict(entry["query"]), sort_keys=True,
                                    separators=(",", ":"), ensure_ascii=True),
            },
            "record_id": government_id,
            "durable_record_id": self.record_id(government_id),
            "record_locator": {"fixture_path": str(entry["fixture_path"]),
                               "record_index": index, "government_id": government_id},
            "model": {"make": _text(payload["make"]),
                      "commercial_model": _text(payload["commercial_model"]),
                      "market": GOVERNMENT_DATASET_MARKET,
                      "model_year": payload["model_year"]},
            "variant": dict(projected),
            "upstream_fields": {field: record[field] for field in UPSTREAM_FIELDS},
            "unmapped_fields": [{"field": field, "reason": reason}
                                for field, reason in UNMAPPED_FIELDS],
        }

    @staticmethod
    def record_id(government_id: int) -> str:
        """The stable durable identifier of ONE registry row.

        Built around the dataset's OWN `_id`, unchanged, so a durable locator
        names the row upstream names and a reviewer can fetch exactly it.
        """
        return f"gov_il.wltp.{government_id}"


__all__ = ["BODY_STYLE_BY_MERKAV", "CKAN_PACKAGE_ID", "CONSISTENT_FUEL_PROPULSION",
           "UPSTREAM_FIELDS",
           "DRIVETRAIN_BY_CODE", "FUEL_BY_CODE", "GOVERNMENT_DATASET_MARKET",
           "GOVERNMENT_PUBLISHER", "MAKE_BY_TOZAR", "MAX_SCANNED_RECORDS",
           "PACKAGE_SOURCE_KEY", "PROPULSION_BY_CODE", "R5_GOV_PAGINATION_REASONS",
           "UNMAPPED_FIELDS", "WLTP_PAGE_LIMIT", "WLTP_PAGE_PLAN",
           "WLTP_PAGE_SOURCE_KEYS", "WLTP_QUERY_TOKEN", "WLTP_REPORTED_TOTAL",
           "WLTP_RESOURCE_ID", "GovernmentVehicleRegistryTool"]

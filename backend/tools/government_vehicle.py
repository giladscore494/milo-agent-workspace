"""Catalog PR3: the Israeli vehicle register as ONE bounded, read-only Tool.

What this is
------------

A thin wrapper around the reviewed catalog query layer
(`backend/catalog/government/query.py`), registered through the EXISTING
`Tool` protocol and `ToolRegistry`. There is no second tool framework, no
second evidence board, no second registry and no second model gateway.

Every guarantee the Registry already provides applies unchanged: the operation
and the resolved payload are validated against the authoritative registered
schemas immediately before execution, the required scope is checked against the
server-owned `ToolContext`, and the result is validated against the declared
output schema before a caller sees it.

What this tool can and cannot do
--------------------------------

*   READ mode, one static server-owned scope (`catalog:government:read`), and
    no write operation of any kind. A plan may REQUEST this capability; only
    trusted worker wiring can grant it.
*   It reads ALREADY DURABLE rows: active, complete, usable Government
    snapshots a previous ingestion landed. It holds no `DataGovClient`, no
    transport and no credential, so a chat run cannot reach `data.gov.il`
    through it -- not because nothing calls the client, but because this object
    has no way to construct one.
*   Every operation is explicitly paginated with a server-owned bound, orders
    deterministically, and returns the EXACT total, so `has_more` is a fact.
*   Every factual result carries the provenance of the snapshot it came from.
*   NO OPERATION CAN RETURN A COMPLETE RAW RESOURCE. There is no "dump", no
    "list every record", no free-text query and no caller-chosen ordering; the
    one operation that returns a preserved register row returns the bounded,
    server-selected IDENTITY PROJECTION of exactly one row, and only when the
    caller's filters resolved to exactly one variant.
*   Every failure is a static, code-owned `ToolError`. The refusal reason from
    the query layer travels as a code; the underlying message, which can quote
    a snapshot, a row or a SQL value, does not.

Cancellation is checked through the `ToolContext` the Registry already passes,
and again inside the query layer between database reads, so a cancelled run
stops between pages rather than after the last one.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from backend.catalog.government import source as src
from backend.catalog.government.projection import (DEFAULT_RESULT_ITEMS, MAX_RESULT_ITEMS,
                                                   DatasetProvenance,
                                                   GovernmentProjectionError)
from backend.catalog.contracts import CANDIDATE_IDENTITY_DIMENSIONS
from backend.catalog.government.query import (IDENTITY_RECORD_FIELD_TYPES,
                                              MAX_RESOLUTION_MATCHES, CandidateVariantRow,
                                              GovernmentCatalogQuery)

from .contracts import ToolContext, ToolError, ToolMode, ToolOperation

#: The tool's registered name and the ONE scope that makes it callable. Both
#: are static server data: a model request can neither create nor enlarge a
#: scope, and this string appears in exactly one place in trusted wiring.
GOVERNMENT_TOOL_NAME = "catalog.government_vehicle"
GOVERNMENT_TOOL_SCOPE = "catalog:government:read"

#: The bound a caller may ask for, and the one it gets when it asks for more.
#: The same `MAX_RESULT_ITEMS` the query layer and `catalog_page_limit()` apply,
#: named here so the advertised schema and the enforced bound are one number.
MAX_TOOL_PAGE_ITEMS = MAX_RESULT_ITEMS
DEFAULT_TOOL_PAGE_ITEMS = DEFAULT_RESULT_ITEMS


def _object(properties: Mapping[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"type": "object", "properties": dict(properties),
            "required": list(required), "additionalProperties": False}


def _array(items: Mapping[str, Any], max_items: int) -> dict[str, Any]:
    return {"type": "array", "items": dict(items), "maxItems": max_items}


_STR = {"type": "string"}
_INT = {"type": "integer"}
_BOOL = {"type": "boolean"}

#: The closed identity-dimension vocabulary, as a closed object schema. Every
#: entry is OPTIONAL -- an unstated dimension is an absent key -- and a
#: dimension outside `CANDIDATE_IDENTITY_DIMENSIONS` cannot be expressed at
#: all, so a tool result can never advertise one the durable schema refuses.
_DIMENSIONS = _object({name: _STR for name in CANDIDATE_IDENTITY_DIMENSIONS})

#: Provenance, compactly. Every factual result carries it, so it is kept to the
#: fields that let a reviewer find the exact rows again: which snapshot, of
#: which resource, at which upstream version, with which content identity, read
#: under which normalization contract and with how many rows unread.
_PROVENANCE = _object({
    "snapshot_key": _STR, "resource_id": _STR, "package_id": _STR, "publisher": _STR,
    "dataset_market_scope": _STR, "upstream_version": _STR, "upstream_version_kind": _STR,
    "content_sha256": _STR, "schema_fingerprint": _STR, "activated_at": _STR,
    "stored_record_count": _INT, "normalization_contract": _STR,
    "normalization_issue_count": _INT,
}, ("snapshot_key", "resource_id", "upstream_version", "upstream_version_kind",
    "content_sha256", "normalization_contract", "normalization_issue_count"))

#: One candidate variant, exactly as the register states it, with the exact row
#: it was read from. No preserved payload: a page never carries register rows.
_VARIANT = _object({
    "candidate_id": _STR, "status": _STR, "manufacturer": _STR, "commercial_model": _STR,
    "model_year_start": _INT, "model_year_end": _INT, "official_model_code": _STR,
    "trim": _STR, "identity_dimensions": _DIMENSIONS, "upstream_record_id": _STR,
    "resource_id": _STR, "payload_sha256": _STR,
}, ("candidate_id", "status", "manufacturer", "commercial_model", "model_year_start",
    "model_year_end", "upstream_record_id", "resource_id"))

#: The bounded, server-selected identity projection of ONE register row.
#: Exactly `IDENTITY_RECORD_FIELDS`; a field the row does not state is an
#: ABSENT key, never a null.
_IDENTITY_RECORD = _object({
    "upstream_record_id": _STR,
    **{name: (_INT if expected is int else _STR)
       for name, expected in IDENTITY_RECORD_FIELD_TYPES.items()},
}, ("upstream_record_id",))

_PAGE_INPUT = {"limit": _INT, "offset": _INT}
_PAGE_OUTPUT = {"total": _INT, "offset": _INT, "limit": _INT, "has_more": _BOOL,
                "provenance": _PROVENANCE}
_PAGE_REQUIRED = ("total", "offset", "limit", "has_more", "provenance")

OPERATIONS: dict[str, ToolOperation] = {
    "dataset_meta": ToolOperation(
        "dataset_meta",
        "Describe the active Israeli vehicle register snapshot this tool answers from.",
        _object({}),
        _object({"provenance": _PROVENANCE, "declared_record_count": _INT,
                 "stored_record_count": _INT, "normalized_record_count": _INT},
                ("provenance", "declared_record_count", "stored_record_count",
                 "normalized_record_count"))),
    "list_manufacturers": ToolOperation(
        "list_manufacturers",
        "List manufacturers the register states, one bounded page at a time.",
        _object(_PAGE_INPUT),
        _object({**_PAGE_OUTPUT, "manufacturers": _array(_object(
            {"manufacturer": _STR, "model_count": _INT, "variant_count": _INT,
             "ambiguous_variant_count": _INT},
            ("manufacturer", "model_count", "variant_count", "ambiguous_variant_count")),
            MAX_TOOL_PAGE_ITEMS)}, (*_PAGE_REQUIRED, "manufacturers"))),
    "get_manufacturer_summary": ToolOperation(
        "get_manufacturer_summary",
        "Coverage of ONE exact manufacturer: how many models and variants the register states.",
        _object({"manufacturer": _STR}, ("manufacturer",)),
        _object({"found": _BOOL, "manufacturer": _STR, "model_count": _INT,
                 "variant_count": _INT, "ambiguous_variant_count": _INT,
                 "provenance": _PROVENANCE},
                ("found", "manufacturer", "model_count", "variant_count",
                 "ambiguous_variant_count", "provenance"))),
    "list_models": ToolOperation(
        "list_models",
        "List the commercial models one exact manufacturer has in the register.",
        _object({"manufacturer": _STR, **_PAGE_INPUT}, ("manufacturer",)),
        _object({**_PAGE_OUTPUT, "models": _array(_object(
            {"manufacturer": _STR, "commercial_model": _STR, "variant_count": _INT,
             "ambiguous_variant_count": _INT, "model_year_start": _INT,
             "model_year_end": _INT},
            ("manufacturer", "commercial_model", "variant_count",
             "ambiguous_variant_count")), MAX_TOOL_PAGE_ITEMS)},
            (*_PAGE_REQUIRED, "models"))),
    "get_model_years": ToolOperation(
        "get_model_years",
        "List the Israeli model years the register states for one exact manufacturer and model.",
        _object({"manufacturer": _STR, "commercial_model": _STR, **_PAGE_INPUT},
                ("manufacturer", "commercial_model")),
        _object({**_PAGE_OUTPUT, "model_years": _array(_object(
            {"manufacturer": _STR, "commercial_model": _STR, "model_year": _INT,
             "variant_count": _INT, "ambiguous_variant_count": _INT},
            ("manufacturer", "commercial_model", "model_year", "variant_count",
             "ambiguous_variant_count")), MAX_TOOL_PAGE_ITEMS)},
            (*_PAGE_REQUIRED, "model_years"))),
    "get_variants": ToolOperation(
        "get_variants",
        "List every register variant of one exact model, optionally for one model year.",
        _object({"manufacturer": _STR, "commercial_model": _STR, "model_year": _INT,
                 **_PAGE_INPUT}, ("manufacturer", "commercial_model")),
        _object({**_PAGE_OUTPUT, "variants": _array(_VARIANT, MAX_TOOL_PAGE_ITEMS)},
                (*_PAGE_REQUIRED, "variants"))),
    "resolve_variant": ToolOperation(
        "resolve_variant",
        "Resolve ONE register variant, or report every match. An ambiguity is never resolved here.",
        _object({"manufacturer": _STR, "commercial_model": _STR, "model_year": _INT,
                 "trim": _STR, "official_model_code": _STR},
                ("manufacturer", "commercial_model", "model_year")),
        _object({"resolved": _BOOL, "ambiguous": _BOOL, "match_count": _INT,
                 "variants": _array(_VARIANT, MAX_RESOLUTION_MATCHES),
                 "source_record": _IDENTITY_RECORD, "provenance": _PROVENANCE},
                ("resolved", "ambiguous", "match_count", "variants", "provenance"))),
    "search_codes": ToolOperation(
        "search_codes",
        "Look up register variants by ONE exact official model code. Exact: never a prefix.",
        _object({"official_model_code": _STR, **_PAGE_INPUT}, ("official_model_code",)),
        _object({**_PAGE_OUTPUT, "variants": _array(_VARIANT, MAX_TOOL_PAGE_ITEMS)},
                (*_PAGE_REQUIRED, "variants"))),
}


def _provenance_payload(provenance: DatasetProvenance) -> dict[str, Any]:
    return {"snapshot_key": provenance.snapshot_key, "resource_id": provenance.resource_id,
            "package_id": provenance.package_id, "publisher": provenance.publisher,
            "dataset_market_scope": provenance.dataset_market_scope,
            "upstream_version": provenance.upstream_version,
            "upstream_version_kind": provenance.upstream_version_kind,
            "content_sha256": provenance.content_sha256,
            "schema_fingerprint": provenance.schema_fingerprint,
            "activated_at": provenance.activated_at,
            "stored_record_count": provenance.stored_record_count,
            "normalization_contract": provenance.normalization_contract,
            "normalization_issue_count": provenance.normalization_issue_count}


def _variant_payload(row: CandidateVariantRow) -> dict[str, Any]:
    """One variant as the register states it.

    An unstated code or trim is an ABSENT KEY, never a null and never an empty
    string: an absent key is what keeps "the register said nothing here"
    distinguishable from "the register said nothing".
    """
    payload: dict[str, Any] = {
        "candidate_id": row.candidate_id, "status": row.status,
        "manufacturer": row.manufacturer, "commercial_model": row.commercial_model,
        "model_year_start": row.model_year_start, "model_year_end": row.model_year_end,
        "identity_dimensions": dict(row.identity_dimensions),
        "upstream_record_id": row.upstream_record_id, "resource_id": row.resource_id,
        "payload_sha256": row.payload_sha256}
    if row.official_model_code is not None:
        payload["official_model_code"] = row.official_model_code
    if row.trim is not None:
        payload["trim"] = row.trim
    return payload


class GovernmentVehicleTool:
    """The registered read capability over the durable Government catalog."""

    name = GOVERNMENT_TOOL_NAME
    description = ("Read the Israeli Ministry of Transport vehicle register as MILO captured "
                   "it: manufacturers, models, Israeli model years, official model codes and "
                   "variants, bounded and with full provenance. Read-only.")
    mode = ToolMode.READ
    required_scope = GOVERNMENT_TOOL_SCOPE
    operations = OPERATIONS

    def __init__(self, repository: Any, *, resource_id: str = src.WLTP_RESOURCE_ID,
                 snapshot_key: str | None = None, allow_incomplete: bool = False,
                 query_factory: Callable[..., GovernmentCatalogQuery] | None = None) -> None:
        """Bind the tool to a repository and ONE reviewed resource.

        The resource, the pinned snapshot and the incompleteness
        acknowledgement are constructor arguments -- trusted server wiring --
        and are deliberately NOT operation inputs: a model that could name its
        own resource or acknowledge its own gap would be granting itself
        something the server decides.
        """
        self._repository = repository
        self._resource_id = src.require_allowed_resource(resource_id)
        self._snapshot_key = snapshot_key
        self._allow_incomplete = bool(allow_incomplete)
        self._query_factory = query_factory or GovernmentCatalogQuery

    def execute(self, context: ToolContext, operation: str,
                payload: Mapping[str, Any]) -> Mapping[str, Any]:
        context.check_cancelled()
        query = self._query_factory(
            self._repository, resource_id=self._resource_id, snapshot_key=self._snapshot_key,
            allow_incomplete=self._allow_incomplete,
            cancellation_checker=context.cancellation_checker)
        handler = getattr(self, f"_op_{operation}", None)
        if handler is None:
            # Unreachable through the Registry, which rejects an unregistered
            # operation before execution. Defence in depth for a direct caller.
            raise ToolError("TOOL_OPERATION_NOT_ALLOWED", "tool operation is not registered",
                            tool=self.name)
        try:
            result = handler(query, payload)
        except GovernmentProjectionError as refusal:
            # Only the static, code-owned reason travels. The safe message
            # names a condition, never a snapshot, a row or a SQL value.
            raise ToolError(refusal.reason_code, refusal.safe_message, tool=self.name) from None
        context.check_cancelled()
        return result

    # --- operations ----------------------------------------------------------

    def _op_dataset_meta(self, query: GovernmentCatalogQuery,
                         payload: Mapping[str, Any]) -> dict[str, Any]:
        provenance = query.dataset_metadata()
        return {"provenance": _provenance_payload(provenance),
                "declared_record_count": provenance.declared_record_count,
                "stored_record_count": provenance.stored_record_count,
                "normalized_record_count": provenance.normalized_record_count}

    def _op_list_manufacturers(self, query: GovernmentCatalogQuery,
                               payload: Mapping[str, Any]) -> dict[str, Any]:
        page = query.list_manufacturers(limit=payload.get("limit", DEFAULT_TOOL_PAGE_ITEMS),
                                        offset=payload.get("offset", 0))
        return {**_page_payload(page), "manufacturers": [
            {"manufacturer": item.manufacturer, "model_count": item.model_count,
             "variant_count": item.variant_count,
             "ambiguous_variant_count": item.ambiguous_variant_count}
            for item in page.items]}

    def _op_get_manufacturer_summary(self, query: GovernmentCatalogQuery,
                                     payload: Mapping[str, Any]) -> dict[str, Any]:
        manufacturer = str(payload["manufacturer"])
        summary = query.get_manufacturer_summary(manufacturer)
        provenance = _provenance_payload(query.dataset_metadata())
        if summary is None:
            # A manufacturer the register does not state is an ANSWER, not an
            # error: `found: false` with real provenance says the snapshot was
            # read and states nothing here.
            return {"found": False, "manufacturer": manufacturer, "model_count": 0,
                    "variant_count": 0, "ambiguous_variant_count": 0, "provenance": provenance}
        return {"found": True, "manufacturer": summary.manufacturer,
                "model_count": summary.model_count, "variant_count": summary.variant_count,
                "ambiguous_variant_count": summary.ambiguous_variant_count,
                "provenance": provenance}

    def _op_list_models(self, query: GovernmentCatalogQuery,
                        payload: Mapping[str, Any]) -> dict[str, Any]:
        page = query.list_models(str(payload["manufacturer"]),
                                 limit=payload.get("limit", DEFAULT_TOOL_PAGE_ITEMS),
                                 offset=payload.get("offset", 0))
        models = []
        for item in page.items:
            entry = {"manufacturer": item.manufacturer,
                     "commercial_model": item.commercial_model,
                     "variant_count": item.variant_count,
                     "ambiguous_variant_count": item.ambiguous_variant_count}
            if item.model_year_start is not None:
                entry["model_year_start"] = item.model_year_start
                entry["model_year_end"] = item.model_year_end
            models.append(entry)
        return {**_page_payload(page), "models": models}

    def _op_get_model_years(self, query: GovernmentCatalogQuery,
                            payload: Mapping[str, Any]) -> dict[str, Any]:
        page = query.list_model_years(str(payload["manufacturer"]),
                                      str(payload["commercial_model"]),
                                      limit=payload.get("limit", DEFAULT_TOOL_PAGE_ITEMS),
                                      offset=payload.get("offset", 0))
        return {**_page_payload(page), "model_years": [
            {"manufacturer": item.manufacturer, "commercial_model": item.commercial_model,
             "model_year": item.model_year, "variant_count": item.variant_count,
             "ambiguous_variant_count": item.ambiguous_variant_count}
            for item in page.items]}

    def _op_get_variants(self, query: GovernmentCatalogQuery,
                         payload: Mapping[str, Any]) -> dict[str, Any]:
        page = query.list_variants(manufacturer=str(payload["manufacturer"]),
                                   commercial_model=str(payload["commercial_model"]),
                                   model_year=payload.get("model_year"),
                                   limit=payload.get("limit", DEFAULT_TOOL_PAGE_ITEMS),
                                   offset=payload.get("offset", 0))
        return {**_page_payload(page),
                "variants": [_variant_payload(item) for item in page.items]}

    def _op_search_codes(self, query: GovernmentCatalogQuery,
                         payload: Mapping[str, Any]) -> dict[str, Any]:
        page = query.find_by_model_code(str(payload["official_model_code"]),
                                        limit=payload.get("limit", DEFAULT_TOOL_PAGE_ITEMS),
                                        offset=payload.get("offset", 0))
        return {**_page_payload(page),
                "variants": [_variant_payload(item) for item in page.items]}

    def _op_resolve_variant(self, query: GovernmentCatalogQuery,
                            payload: Mapping[str, Any]) -> dict[str, Any]:
        resolution = query.resolve_variant(
            str(payload["manufacturer"]), str(payload["commercial_model"]),
            int(payload["model_year"]), trim=payload.get("trim"),
            official_model_code=payload.get("official_model_code"))
        result = {"resolved": resolution.variant is not None,
                  "ambiguous": resolution.ambiguous,
                  "match_count": resolution.match_count,
                  "variants": [_variant_payload(item) for item in resolution.matches],
                  "provenance": _provenance_payload(resolution.provenance)}
        if resolution.variant is not None and resolution.identity_projection:
            # The identity projection of exactly ONE row, and only for a unique
            # resolution. This is the material the trusted evidence mapper
            # reads; an ambiguous answer quotes nothing, because there is no
            # single row whose fields it could quote.
            result["source_record"] = {
                "upstream_record_id": resolution.variant.upstream_record_id,
                **dict(resolution.identity_projection)}
        return result


def _page_payload(page: Any) -> dict[str, Any]:
    return {"total": page.total, "offset": page.offset, "limit": page.limit,
            "has_more": page.has_more,
            "provenance": _provenance_payload(page.provenance)}


__all__ = ["DEFAULT_TOOL_PAGE_ITEMS", "GOVERNMENT_TOOL_NAME", "GOVERNMENT_TOOL_SCOPE",
           "MAX_TOOL_PAGE_ITEMS", "OPERATIONS", "GovernmentVehicleTool"]

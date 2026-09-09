"""R5: the read-only, fixture-backed proof tools for one real vehicle.

These are REAL source adapters in shape and REAL data in content -- the Yeda
tool answers out of the pinned Yeda catalog record, at the exact commit the
manifest names -- but they read only committed fixtures. There is no network
access, no credential, no query language and no write operation anywhere in
this module, and none of these tools is registered in the production
ToolRegistry (which stays empty).

What every operation here guarantees, and what a production adapter must also
guarantee:

*   A closed input schema and a closed output schema, both validated by the
    Registry before and after execution.
*   Deterministic size and count bounds. There is deliberately NO
    whole-catalog and no whole-dataset operation: an operation returns one
    identified record, or it fails.
*   No arbitrary path, URL, JSONPath, filter or query is ever accepted or
    evaluated. Selection is by named, typed identity dimensions only.
*   Fail closed on a missing fixture, a manifest mismatch, a checksum
    mismatch, an unexpected fixture shape, or an identity that matches more
    than one variant.
*   The result carries SOURCE MATERIAL and provenance for a trusted mapper to
    read. It never carries a pre-authored claim supplied by the caller, and
    the caller cannot influence what provenance is reported.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from backend.engines.swarm_v2.evidence_bounds import IDENTITY_DIMENSIONS
from backend.tools.contracts import ToolContext, ToolError, ToolMode, ToolOperation

from .manifest import ProofManifestError, load_fixture

# The bound on how many attributed source URLs one variant may report. The
# catalog record is small and bounded already; this exists so the operation's
# output size is a server-owned constant rather than a property of the data.
MAX_ATTRIBUTED_SOURCES = 8

#: The closed identity dimensions the Yeda catalog is capable of stating for a
#: variant, mapped from ITS field names onto the shared R4 vocabulary. Static
#: server data: the tool never derives a dimension from a field name it happens
#: to find, so a catalog that grows a new field states nothing new here until a
#: reviewer adds it. `generation` and `model_code` are absent because this
#: catalog states neither -- which is a real coverage gap, reported as one.
YEDA_IDENTITY_FIELDS: Mapping[str, str] = {
    "body_type": "body_style",
    "drivetrain": "drivetrain",
    "engine": "engine",
    "transmission": "transmission",
    "version_or_trim": "trim",
}

#: The dimensions a caller may narrow a lookup by, mapped onto the record field
#: each one reads. This is deliberately a SUPERSET of the identity dimensions
#: above: `fuel_type` is a technical specification rather than an R4 identity
#: dimension, but it is exactly what separates the five RAV4 variants that
#: overlap model year 2021, so it must be selectable. Being selectable is not
#: being an identity dimension -- the mapper still records `fuel_type` as a
#: FACT and never as part of the variant's identity.
YEDA_SELECTOR_FIELDS: Mapping[str, str] = {
    "body_style": "body_type",
    "drivetrain": "drivetrain",
    "engine": "engine",
    "fuel_type": "fuel_type",
    "transmission": "transmission",
    "trim": "version_or_trim",
}

#: The market-presence vocabulary this catalog's `market` field actually uses.
#: It is NOT a market identifier -- across the pinned catalog it takes the
#: values below -- so it is read through this closed map and never parsed for a
#: market name. `global-reference-only` is deliberately absent: such a record
#: is not an Israeli-market record at all and must not answer an IL request.
YEDA_MARKET_PRESENCE: Mapping[str, tuple[str, str]] = {
    # record `market` value -> (market identifier, presence confidence)
    "IL": ("IL", "unqualified"),
    "IL-confirmed": ("IL", "confirmed"),
    "IL-likely": ("IL", "likely"),
}

_VARIANT_REQUEST = {
    "type": "object",
    "properties": {
        # The identity a caller must state.
        "make": {"type": "string"},
        "commercial_model": {"type": "string"},
        "market": {"type": "string"},
        "model_year": {"type": "integer"},
        # The OPTIONAL narrowing dimensions. They are optional precisely so
        # the ambiguous case is reachable: make + commercial model + an
        # overlapping year matches five RAV4 variants in the pinned catalog,
        # and this operation refuses that rather than picking one.
        "fuel_type": {"type": "string"},
        "drivetrain": {"type": "string"},
        "transmission": {"type": "string"},
        "body_style": {"type": "string"},
        "engine": {"type": "string"},
        "trim": {"type": "string"},
    },
    "required": ["commercial_model", "make", "market", "model_year"],
    "additionalProperties": False,
}

_VARIANT_RESULT = {
    "type": "object",
    "properties": {
        "source": {
            "type": "object",
            "properties": {
                "repository": {"type": "string"},
                "repository_path": {"type": "string"},
                "commit_sha": {"type": "string"},
                "blob_sha": {"type": "string"},
                "catalog_hash": {"type": "string"},
                "catalog_generated_at": {"type": "string"},
                "catalog_market": {"type": "string"},
                "canonical_url": {"type": "string"},
            },
            "required": ["blob_sha", "canonical_url", "catalog_generated_at", "catalog_hash",
                         "catalog_market", "commit_sha", "repository", "repository_path"],
            "additionalProperties": False,
        },
        "record_id": {"type": "string"},
        "record_locator": {
            "type": "object",
            "properties": {"model_index": {"type": "integer"},
                           "variant_index": {"type": "integer"}},
            "required": ["model_index", "variant_index"],
            "additionalProperties": False,
        },
        "model": {
            "type": "object",
            "properties": {
                "make": {"type": "string"},
                "commercial_model": {"type": "string"},
                "canonical_model": {"type": "string"},
                "market": {"type": "string"},
                "market_presence": {"type": "string"},
                "profile_confidence": {"type": "string"},
                "model_year": {"type": "integer"},
            },
            "required": ["canonical_model", "commercial_model", "make", "market",
                         "market_presence", "model_year"],
            "additionalProperties": False,
        },
        "variant": {
            "type": "object",
            "properties": {
                "body_style": {"type": "string"},
                "fuel_type": {"type": "string"},
                "engine": {"type": "string"},
                # Deliberately named `nominal_...`: the catalog states an
                # engine-CLASS label (2.5), not a homologated displacement.
                "nominal_engine_displacement_l": {"type": "number"},
                "horsepower_hp": {"type": "integer"},
                "transmission": {"type": "string"},
                "drivetrain": {"type": "string"},
                "year_start": {"type": "integer"},
                "year_end": {"type": "integer"},
                "support_level": {"type": "string"},
                # Present ONLY when the catalog states one. Its absence is the
                # honest representation of a gap, and it is also reported in
                # `declared_missing_identity`.
                "trim": {"type": "string"},
            },
            "required": ["body_style", "drivetrain", "engine", "fuel_type", "horsepower_hp",
                         "nominal_engine_displacement_l", "support_level", "transmission",
                         "year_start"],
            "additionalProperties": False,
        },
        "declared_missing_identity": {"type": "array", "items": {"type": "string"},
                                      "maxItems": len(IDENTITY_DIMENSIONS)},
        "attributed_source_urls": {"type": "array", "items": {"type": "string"},
                                   "maxItems": MAX_ATTRIBUTED_SOURCES},
    },
    "required": ["attributed_source_urls", "declared_missing_identity", "model",
                 "record_id", "record_locator", "source", "variant"],
    "additionalProperties": False,
}


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


@dataclass(frozen=True)
class YedaVehicleCatalogTool:
    """The pinned Yeda catalog, exposed as ONE bounded read-only lookup.

    `get_model_variant` answers with exactly one variant or refuses. It is the
    whole capability: there is no list-all operation, no free-text search and
    no way to ask for the catalog itself, so the 7.3 MB upstream document can
    never cross this boundary even if it were committed (it is not -- only the
    single bounded record subset the manifest pins is).
    """

    name: str = "yeda.vehicle_catalog"
    description: str = "Pinned Israeli vehicle knowledge catalog; one exact model variant per call"
    required_scope: str = "yeda:catalog_read"
    mode: ToolMode = ToolMode.READ
    source_key: str = "yeda"
    operations = {
        "get_model_variant": ToolOperation(
            "get_model_variant",
            "Return one exact catalog variant for a stated vehicle identity, or fail closed.",
            _VARIANT_REQUEST, _VARIANT_RESULT),
    }

    def execute(self, context: ToolContext, operation: str,
                payload: Mapping[str, Any]) -> Mapping[str, Any]:
        try:
            entry, document = load_fixture(self.source_key)
        except ProofManifestError as failure:
            # A missing, tampered or unmanifested fixture is a hard refusal,
            # never a degraded answer.
            raise ToolError(failure.reason_code, failure.safe_message, tool=self.name) from None
        return self._get_model_variant(entry, document, payload)

    def _get_model_variant(self, entry: Mapping[str, Any], document: Mapping[str, Any],
                           payload: Mapping[str, Any]) -> Mapping[str, Any]:
        catalog, record = self._read_fixture(document)
        market, presence = self._market_of(record)
        if _text(payload["make"]) != record["make"] or \
                _text(payload["commercial_model"]) != record["model"] or \
                _text(payload["market"]) != market:
            raise ToolError("R5_YEDA_RECORD_NOT_FOUND",
                            "the pinned catalog record does not describe this vehicle",
                            tool=self.name)
        model_year = payload["model_year"]
        matches = [(index, variant) for index, variant
                   in enumerate(record["technical_variants_il"])
                   if self._covers_year(variant, model_year)
                   and self._matches_requested_identity(variant, payload)]
        if not matches:
            raise ToolError("R5_YEDA_VARIANT_NOT_FOUND",
                            "no catalog variant matches this identity", tool=self.name)
        if len(matches) > 1:
            # The whole reason the narrowing dimensions exist. Make, commercial
            # model and an overlapping year do NOT identify a variant, and this
            # operation refuses to choose one on the caller's behalf.
            raise ToolError("R5_YEDA_VARIANT_AMBIGUOUS",
                            "this identity matches more than one catalog variant",
                            tool=self.name)
        index, variant = matches[0]
        return self._project(entry, catalog, record, variant, index=index,
                             market=market, presence=presence, model_year=model_year)

    def _read_fixture(self, document: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        """Read the two objects the fixture must contain, or fail closed."""
        catalog, record = document.get("catalog"), document.get("model")
        if not isinstance(catalog, Mapping) or not isinstance(record, Mapping):
            raise ToolError("R5_YEDA_FIXTURE_INVALID",
                            "the pinned catalog fixture is not the document the manifest describes",
                            tool=self.name)
        variants = record.get("technical_variants_il")
        if not isinstance(variants, list) or not variants:
            raise ToolError("R5_YEDA_FIXTURE_INVALID",
                            "the pinned catalog record states no technical variants",
                            tool=self.name)
        return catalog, record

    def _market_of(self, record: Mapping[str, Any]) -> tuple[str, str]:
        """Split the record's `market` field into market and presence confidence.

        Read through the closed vocabulary above and nothing else. A value the
        vocabulary does not name -- `global-reference-only`, or anything a
        future catalog invents -- fails closed rather than being parsed for a
        market prefix it happens to start with.
        """
        presence = YEDA_MARKET_PRESENCE.get(str(record.get("market")))
        if presence is None:
            raise ToolError("R5_YEDA_MARKET_UNKNOWN",
                            "the catalog record states no recognised market presence",
                            tool=self.name)
        return presence

    @staticmethod
    def _covers_year(variant: Mapping[str, Any], model_year: int) -> bool:
        start, end = variant.get("year_start"), variant.get("year_end")
        if not isinstance(start, int) or start > model_year:
            return False
        # An open-ended variant (`year_end: null`) is still current.
        return end is None or (isinstance(end, int) and model_year <= end)

    @staticmethod
    def _matches_requested_identity(variant: Mapping[str, Any],
                                    payload: Mapping[str, Any]) -> bool:
        """Narrow by the dimensions the CALLER stated, and only those.

        A dimension the caller did not state never narrows anything, which is
        exactly why an under-specified request stays ambiguous instead of
        quietly resolving to whichever variant happens to come first.
        """
        for selector, field in YEDA_SELECTOR_FIELDS.items():
            wanted = payload.get(selector)
            if wanted is None:
                continue
            if _text(variant.get(field)) != _text(wanted):
                return False
        return True

    def _project(self, entry: Mapping[str, Any], catalog: Mapping[str, Any],
                 record: Mapping[str, Any], variant: Mapping[str, Any], *, index: int,
                 market: str, presence: str, model_year: int) -> dict[str, Any]:
        """Build the bounded result from EXPLICITLY named fields only."""
        stated = {dimension: _text(variant.get(field))
                  for field, dimension in YEDA_IDENTITY_FIELDS.items()}
        missing = sorted(dimension for dimension in IDENTITY_DIMENSIONS
                         if not stated.get(dimension))
        projected: dict[str, Any] = {
            "body_style": stated["body_style"], "fuel_type": _text(variant.get("fuel_type")),
            "engine": stated["engine"],
            "nominal_engine_displacement_l": variant.get("engine_displacement_l"),
            "horsepower_hp": variant.get("horsepower_hp"),
            "transmission": stated["transmission"], "drivetrain": stated["drivetrain"],
            "year_start": variant.get("year_start"),
            "support_level": _text(variant.get("support_level")),
        }
        if isinstance(variant.get("year_end"), int):
            projected["year_end"] = variant["year_end"]
        if stated["trim"] is not None:
            projected["trim"] = stated["trim"]
        model: dict[str, Any] = {
            "make": record["make"], "commercial_model": record["model"],
            "canonical_model": record.get("canonical_model") or record["model"],
            "market": market, "market_presence": presence, "model_year": model_year,
        }
        if _text(record.get("profile_confidence")):
            model["profile_confidence"] = _text(record["profile_confidence"])
        return {
            "source": {
                "repository": entry["repository"], "repository_path": entry["repository_path"],
                "commit_sha": entry["commit_sha"], "blob_sha": entry["blob_sha"],
                "catalog_hash": str(catalog["catalog_hash"]),
                "catalog_generated_at": str(catalog["generated_at"]),
                "catalog_market": str(catalog["market"]),
                "canonical_url": entry["canonical_url"],
            },
            "record_id": self.record_id(entry["record_locator"]["model_index"], index),
            "record_locator": {"model_index": entry["record_locator"]["model_index"],
                               "variant_index": index},
            "model": model,
            "variant": projected,
            "declared_missing_identity": missing,
            "attributed_source_urls": self._attributed(record, variant),
        }

    @staticmethod
    def record_id(model_index: int, variant_index: int) -> str:
        """The stable durable record identifier of ONE catalog variant."""
        return f"yeda.models.{model_index}.variants.{variant_index}"

    @staticmethod
    def _attributed(record: Mapping[str, Any], variant: Mapping[str, Any]) -> list[str]:
        """The catalog's own source attribution for THIS variant, bounded.

        Read from the variant's declared `source_indexes` against the record's
        own `sources` list -- never from a guess about which source looks
        official.
        """
        by_index = {item.get("source_index"): item for item in record.get("sources") or []
                    if isinstance(item, Mapping)}
        urls: list[str] = []
        for source_index in (variant.get("source_indexes") or [])[:MAX_ATTRIBUTED_SOURCES]:
            url = _text((by_index.get(source_index) or {}).get("url"))
            if url is not None:
                urls.append(url)
        return urls


__all__ = ["MAX_ATTRIBUTED_SOURCES", "YEDA_IDENTITY_FIELDS", "YEDA_MARKET_PRESENCE",
           "YEDA_SELECTOR_FIELDS", "YedaVehicleCatalogTool"]

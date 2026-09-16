"""Catalog PR3: the trusted mapping from a Government tool result into evidence.

What this module is
-------------------

ONE evidence mapper, registered for ONE exact tool operation:
`catalog.government_vehicle.resolve_variant`. It reads only the fields that
operation's declared output schema guarantees, and it derives every piece of
provenance rather than accepting any of it.

Why only that operation
-----------------------

The register's other reads are legitimate answers that state nothing about one
vehicle. `dataset_meta` describes a retrieval; `list_manufacturers`,
`list_models`, `get_model_years` and `get_manufacturer_summary` are coverage
counts; `get_variants` and `search_codes` return SEVERAL rows, and turning a
listing into evidence would mean attributing a fact to whichever row happened
to come first.

`resolve_variant` is the operation that ends with exactly one register row or
with a stated ambiguity, so it is the only one where "this exact row states
this exact field" is true. An ambiguous or empty resolution states no such
fact, and the mapper DECLINES it (`NO_EVIDENCE`) rather than inventing one or
failing a task that asked a reasonable question.

What the model can and cannot influence
---------------------------------------

The model chooses the ARGUMENTS of the call -- which manufacturer, which model,
which year. It cannot choose the source, the version, the locator, the fragment
text, the field key, the value or the confidence: every one of those is derived
here from the server-produced tool result and from server-owned constants in
`backend/catalog/government/source.py`. The result itself is not model output
either: the Registry validated it against the registered output schema, and the
tool built it from durable rows.

Government authority is FIELD-SPECIFIC
--------------------------------------

The register is the anchor for what it actually defines, and this mapper emits
a fact for exactly those fields:

    model_year_start / model_year_end   <- shnat_yitzur
    official_model_code                 <- degem_nm
    trim                                <- ramat_gimur
    identity_dimensions.fuel_type       <- delek_cd (cross-checked by delek_nm)

and for NOTHING else. Reliability, price, market value and every unsupported
semantic conversion are absent, deliberately and by construction rather than by
omission -- there is no branch here that could emit one. `koah_sus` in
particular is never mapped to horsepower: PR2 recorded the reviewer's reason
(the dataset publishes no definition and the captured values are inconsistent
in scale), and this mapper does not read the field at all.

The FRAGMENT BOUND, stated exactly. One durable source may carry at most four
focused fragments (`MAX_FRAGMENTS_PER_SOURCE`), and every promoted field needs
its own EXACT locator, therefore its own fragment. Four locators is what a
single source can support, which is why the four groups above are what this PR
promotes. The remaining reviewed dimensions -- body style, drivetrain,
propulsion technology -- are stated by the register, are carried on the
candidate, and travel here as the fact IDENTITY that scopes every claim; they
are not promotable canonical fields in this PR, and the promotion transaction
REFUSES a canonical row that states one without its own verified provenance
rather than dropping it silently.

Nothing here opens a socket, reads a database, calls a provider or consults a
model.
"""

from __future__ import annotations

from typing import Any, Mapping

from backend.catalog import keys as catalog_keys
from backend.catalog.contracts import (CANONICAL_DIMENSION_PREFIX,
                                       SHARED_IDENTITY_DIMENSIONS,
                                       claim_entity_key, record_locator_id)
from backend.engines.swarm_v2.evidence_bounds import IDENTITY_DIMENSIONS
from backend.engines.swarm_v2.evidence_contracts import (EvidenceBundle, EvidenceContractError,
                                                         SourceVersion, StructuredEvidenceFact,
                                                         VersionedEvidenceSource,
                                                         build_evidence_bundle,
                                                         record_field_locator,
                                                         structured_projection)
from backend.engines.swarm_v2.evidence_mapping import NO_EVIDENCE

from . import source as src

#: The `Tool.name` and operation this mapper is registered for. Written here as
#: literals rather than imported from `backend.tools`, so the catalog package
#: keeps its one-way dependency: the Tool imports the query layer, and nothing
#: in this package imports the Tool. `tests/test_catalog_pr3_swarm_promotion.py`
#: pins the two spellings together.
GOVERNMENT_TOOL_NAME = "catalog.government_vehicle"
RESOLVE_VARIANT_OPERATION = "resolve_variant"

#: How strongly the register speaks about the fields it defines. Static server
#: data: never derived from a row, never adjusted per answer, and never
#: something the model can influence.
GOVERNMENT_SOURCE_TYPE = "government_register"
GOVERNMENT_SOURCE_STRENGTH = "strong"
GOVERNMENT_CONFIDENCE = 0.95
GOVERNMENT_AGENT = "catalog.government"

#: canonical field key -> (register field it is read from, the other register
#: fields that qualify the projection, the unit the value is stated in).
#:
#: STATIC and CLOSED. The mapper copies what the record states and never
#: infers, renames or invents one: a row that states no trim produces no trim
#: fact, which can then only ever be compared to another statement about trim.
#:
#: A model year carries the unit `year` because R3 refuses a numeric fact with
#: no unit -- "2022" is not a fact until it says what 2022 counts.
GOVERNMENT_FIELD_SOURCES: tuple[tuple[str, str, tuple[str, ...], str | None], ...] = (
    ("model_year_start", "shnat_yitzur", ("tozar", "kinuy_mishari"), "year"),
    ("model_year_end", "shnat_yitzur", ("tozar", "kinuy_mishari"), "year"),
    ("official_model_code", "degem_nm", ("tozar", "kinuy_mishari", "shnat_yitzur"), None),
    ("trim", "ramat_gimur", ("tozar", "kinuy_mishari", "shnat_yitzur"), None),
    (f"{CANONICAL_DIMENSION_PREFIX}fuel_type", "delek_cd",
     ("tozar", "kinuy_mishari", "shnat_yitzur", "delek_nm"), None),
)

#: candidate identity dimension -> the evidence identity dimension it states.
#: Only the dimensions BOTH closed vocabularies name: a dimension one side does
#: not have is left unstated rather than translated into the nearest word.
IDENTITY_DIMENSION_MAP: Mapping[str, str] = {
    name: name for name in SHARED_IDENTITY_DIMENSIONS if name in IDENTITY_DIMENSIONS
}


def government_record_id(snapshot_key: str, upstream_record_id: str) -> str:
    """The locator's record identity: one register row, inside one snapshot.

    The catalog-wide rule, not a Government one: `record_locator_id` in
    `backend/catalog/contracts.py` is the single definition, and
    `public.catalog_record_locator_id` is its SQL mirror. The promotion trigger
    uses that mirror to refuse a promoted fact whose evidence was read from a
    record other than the candidate's own, so this spelling and that refusal
    can never drift apart.
    """
    return record_locator_id(snapshot_key, upstream_record_id)


def government_entity_key(manufacturer: str, commercial_model: str, model_year: Any) -> str:
    """The VEHICLE a fact is about: the canonical model, at one model year.

    Derived from the same builder the canonical catalog keys its models with,
    so a Government claim and a Web claim about one vehicle share a scope and
    can therefore CONFLICT -- which is the point. Using the register's own row
    id here would make every source's statement about the same car a different
    entity, and two contradictory values would never meet.

    It is also what makes "this evidence is about this canonical row" checkable
    inside PostgreSQL without re-deriving a digest there: the model key is a
    column the canonical catalog already stores, and
    `public.catalog_claim_entity_key` assembles the same string from it.
    """
    return claim_entity_key(
        catalog_keys.canonical_model_key(manufacturer=manufacturer,
                                         commercial_model=commercial_model),
        model_year)


class GovernmentVariantEvidenceMapper:
    """`catalog.government_vehicle.resolve_variant` -> versioned, located evidence."""

    tool = GOVERNMENT_TOOL_NAME
    operation = RESOLVE_VARIANT_OPERATION

    def map(self, call: Any) -> EvidenceBundle | Any:
        result = call.result
        # A resolution that did not settle on ONE row states no fact about any
        # row. Declining is not a failure: the Commander asked a reasonable
        # question and got a true answer -- "the register states more than one
        # of these" -- and there is nothing here to quote.
        if not result.get("resolved") or result.get("match_count") != 1:
            return NO_EVIDENCE
        record = result.get("source_record")
        variants = result.get("variants") or []
        provenance = result.get("provenance")
        if not isinstance(record, Mapping) or not isinstance(provenance, Mapping) \
                or len(variants) != 1 or not isinstance(variants[0], Mapping):
            raise EvidenceContractError("EVIDENCE_VALUE_INVALID")
        variant = variants[0]

        record_id = government_record_id(str(provenance["snapshot_key"]),
                                         str(record["upstream_record_id"]))
        # The projection is built over the record EXTENDED with its own
        # identity, so a locator always resolves inside the object the fragment
        # was cut from -- `read_locator_path` checks exactly that.
        projected = {name: value for name, value in record.items()
                     if name != "upstream_record_id"}
        model_year = variant["model_year_start"]
        entity_key = government_entity_key(str(variant["manufacturer"]),
                                           str(variant["commercial_model"]), model_year)
        identity = self._identity(variant)

        facts: list[StructuredEvidenceFact] = []
        fragments = []
        located: dict[str, Any] = {}
        for field_key, register_field, context, unit in GOVERNMENT_FIELD_SOURCES:
            value = self._value(variant, field_key)
            if value is None or register_field not in projected:
                # The register states nothing here, or the reviewed identity
                # projection does not carry the field the value was read from.
                # Either way there is no exact location to attribute a fact to.
                continue
            locator = record_field_locator(record_id, (register_field,))
            key = locator.locator_key
            if key not in located:
                located[key] = len(fragments)
                fragments.append(structured_projection(
                    record=projected, fields=(*context, register_field), locator=locator,
                    fragment_index=len(fragments)))
            facts.append(StructuredEvidenceFact(
                entity_key=entity_key, field_key=field_key, value=value, unit=unit,
                time_scope={"model_year": model_year},
                geography=src.GOVERNMENT_DATASET_MARKET, market=src.GOVERNMENT_DATASET_MARKET,
                identity=identity, locator=locator))
        if not facts:
            # A resolved row whose reviewed identity fields are all absent
            # states nothing this mapper can locate. It declines rather than
            # producing a bundle with no fact in it.
            return NO_EVIDENCE

        return build_evidence_bundle(
            source=self._source(provenance, record_id, variant), locator_scope=(record_id,),
            facts=facts, fragments=fragments)

    # --- derivation ----------------------------------------------------------

    @staticmethod
    def _value(variant: Mapping[str, Any], field_key: str) -> Any:
        """What the resolved variant STATES for one canonical field, or None."""
        if field_key.startswith(CANONICAL_DIMENSION_PREFIX):
            dimensions = variant.get("identity_dimensions") or {}
            return dimensions.get(field_key[len(CANONICAL_DIMENSION_PREFIX):])
        return variant.get(field_key)

    @staticmethod
    def _identity(variant: Mapping[str, Any]) -> dict[str, str]:
        """The closed identity dimensions the RECORD stated, and nothing else.

        This is what keeps two facts about two trims of one model year from
        being compared as though they were about one vehicle. Every entry is
        copied from a stated field; nothing is inferred and nothing invented.
        """
        dimensions = variant.get("identity_dimensions") or {}
        identity = {target: str(dimensions[name])
                    for name, target in IDENTITY_DIMENSION_MAP.items() if name in dimensions}
        if variant.get("official_model_code") is not None:
            identity["model_code"] = str(variant["official_model_code"])
        if variant.get("trim") is not None:
            identity["trim"] = str(variant["trim"])
        return identity

    @staticmethod
    def _source(provenance: Mapping[str, Any], record_id: str,
                variant: Mapping[str, Any]) -> VersionedEvidenceSource:
        """The versioned source, derived entirely from server-owned material.

        The host and the path come from `source.py` constants, never from the
        result: a source URL assembled from a value in a payload is a value in a
        payload pretending to be provenance. The VERSION is the snapshot's own
        upstream version -- what the register published -- never the retrieval
        time and never a hash of the fragment that was selected.
        """
        resource_id = str(provenance["resource_id"])
        package_id = str(provenance.get("package_id") or src.CKAN_PACKAGE_ID)
        return VersionedEvidenceSource(
            agent=GOVERNMENT_AGENT,
            url=(f"{src.DATA_GOV_SCHEME}://{src.DATA_GOV_HOST}/dataset/{package_id}"
                 f"/resource/{resource_id}"),
            title=f"{variant['manufacturer']} {variant['commercial_model']} "
                  f"{variant['model_year_start']}",
            domain=src.DATA_GOV_HOST, source_type=GOVERNMENT_SOURCE_TYPE,
            source_strength=GOVERNMENT_SOURCE_STRENGTH, source_date=None,
            query=record_id, tool_operation=f"{GOVERNMENT_TOOL_NAME}.{RESOLVE_VARIANT_OPERATION}",
            version=SourceVersion(kind=str(provenance["upstream_version_kind"]),  # type: ignore[arg-type]
                                  identifier=str(provenance["upstream_version"])),
            confidence=GOVERNMENT_CONFIDENCE)


__all__ = ["GOVERNMENT_AGENT", "GOVERNMENT_CONFIDENCE", "GOVERNMENT_FIELD_SOURCES",
           "GOVERNMENT_SOURCE_STRENGTH", "GOVERNMENT_SOURCE_TYPE", "GOVERNMENT_TOOL_NAME",
           "IDENTITY_DIMENSION_MAP", "RESOLVE_VARIANT_OPERATION",
           "GovernmentVariantEvidenceMapper", "government_entity_key",
           "government_record_id"]

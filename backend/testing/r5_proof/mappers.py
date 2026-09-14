"""R5: trusted, operation-specific evidence mappers for the real proof tools.

One mapper per exact `(tool, operation)` pair. Each is shipped, reviewable
server code held to the same rules a production adapter's mapper must meet,
and NONE of them is added to `PRODUCTION_EVIDENCE_MAPPERS`, which stays empty.

What every mapper here does, and what a real one must also do:

*   Accept ONLY a validated `ToolCallRecord`. The registry has already checked
    the operation, the resolved payload and the result against the registered
    schemas, so a mapper reads named, guaranteed fields -- never "whatever key
    looks like text", and never a model completion.
*   Take the source VERSION from the source's own immutable identifier, never
    from a timestamp and never from a hash of the fragment that happened to be
    selected. Each family has a different one, and each is the strongest
    identifier that source actually publishes: the pinned git commit for the
    catalog, the dataset's own `last_modified` for the registry, and -- for a
    page that publishes no ETag, no Last-Modified and no revision -- the
    SHA-256 of the exact FULL captured response body.
*   Give every fact and every fragment the exact locator it was read from, and
    validate that locator against the record the tool actually returned.
*   Carry the unit WITH the value. R3 preserves units; it never converts one.
*   State only the identity dimensions the SOURCE states. A dimension the
    record does not carry stays absent, which under R4 means "unknown" and can
    therefore only ever match another record that is equally silent.
*   Perform no network call, no database read, no provider call and no second
    tool call, and consult no model.

Field naming is part of the contract. The Yeda catalog states an engine-CLASS
label ("2.5L" / 2.5), which is NOT a homologated displacement measurement, so
it is recorded under `nominal_engine_displacement_l` and can never land in the
same comparison scope as an exact `engine_displacement_cc` from a homologation
record. Conflating the two would manufacture either a contradiction or an
agreement that the sources do not actually state.

Source authority is likewise decided by what a source IS, not by how official
it sounds. The three types below land in three different places in
`conflict_policy.SOURCE_TYPE_AUTHORITY`: the government register is
authoritative for regulatory identity and technical specification; the
aggregated catalog is authoritative for nothing; and the official
archived-model page is authoritative for nothing either, because an
authentic manufacturer page that establishes who a model IS does not thereby
establish anything it MEASURES.
"""

from __future__ import annotations

from typing import Any, Mapping

from backend.engines.swarm_v2.evidence_contracts import (EvidenceBundle, EvidenceContractError,
                                                         SourceVersion, StructuredEvidenceFact,
                                                         VersionedEvidenceSource,
                                                         build_evidence_bundle,
                                                         document_span_locator,
                                                         record_field_locator,
                                                         structured_projection)
from backend.engines.swarm_v2.evidence_contracts import FocusedEvidenceFragment
from backend.engines.swarm_v2.fragments import fragment_content_hash, normalize_fragment_text
from backend.engines.swarm_v2.evidence_mapping import EvidenceMapperRegistry
from backend.engines.swarm_v2.tool_calls import ToolCallRecord

from .government import GovernmentVehicleRegistryTool
from .identity import vehicle_entity_key
from .tools import YEDA_IDENTITY_FIELDS
from .web import ToyotaArchivedModelDocumentTool

#: The facts this operation's output schema guarantees, as
#: `(semantic field key, upstream catalog field, unit)`. STATIC server data: a
#: mapper never derives this list from the record in front of it, so a fixture
#: cannot widen what becomes evidence.
#:
#: `nominal_engine_displacement_l` is named for what the catalog states -- an
#: engine-class label -- rather than for what a reader might wish it were.
YEDA_FACT_FIELDS: tuple[tuple[str, str, str | None], ...] = (
    ("fuel_type", "fuel_type", None),
    ("horsepower_hp", "horsepower_hp", "hp"),
    ("nominal_engine_displacement_l", "engine_displacement_l", "l"),
)

#: The qualifying context every projection carries, in this order. Context, not
#: evidence: the fact's own value is the last field of its own projection.
YEDA_CONTEXT_FIELDS: tuple[str, ...] = ("make", "commercial_model", "market", "model_year")

#: The catalog's OWN stated confidence label, rendered onto the numeric scale
#: the evidence contract uses. A closed map, not a computation: the proof never
#: invents a confidence the source did not express, and an unrecognised label
#: fails closed rather than defaulting to something flattering.
YEDA_PROFILE_CONFIDENCE: Mapping[str, float] = {"high": 0.8, "medium": 0.6, "low": 0.4}

#: The conservative floor used when the record states no confidence at all.
YEDA_UNSTATED_CONFIDENCE = 0.5

#: Yeda is a DERIVED, aggregated catalog, not a primary registry and not a
#: manufacturer publication. `conflict_policy.SOURCE_TYPE_AUTHORITY` does not
#: name this type, which is exactly right: under the R4 policy an unknown
#: source type is authoritative for nothing, so a Yeda claim can never close a
#: conflict on its own however confident the catalog sounds.
YEDA_SOURCE_TYPE = "aggregated_vehicle_catalog"


class YedaCatalogEvidenceMapper:
    """`yeda.vehicle_catalog.get_model_variant` -> versioned, located evidence.

    One catalog variant in; one versioned source out, carrying one structured
    fact and one bounded projection per known field. The record is never
    flattened into a sentence: each fact keeps its own value, its own unit and
    its own field locator, and every fragment is typed `structured_projection`
    so a deterministic rendering of a record can never be presented as a quote.

    The source VERSION is the pinned git commit of the catalog -- an immutable
    upstream identifier. The blob sha and the catalog's own short hash travel
    in the manifest beside it; neither is used as the version, because the
    version must be the thing the whole source is identified by.
    """

    tool = "yeda.vehicle_catalog"
    operation = "get_model_variant"

    def __init__(self, *, agent: str = "r5_proof", domain: str = "github.com",
                 source_strength: str = "secondary"):
        self._agent, self._domain = agent, domain
        self._source_strength = source_strength

    def map(self, call: ToolCallRecord) -> EvidenceBundle:
        result: Mapping[str, Any] = call.result
        source_material, model, variant = result["source"], result["model"], result["variant"]
        record_id = str(result["record_id"])
        entity = vehicle_entity_key(make=model["make"],
                                    commercial_model=model["commercial_model"],
                                    market=model["market"])
        # The identity dimensions the RECORD states, mapped onto the closed R4
        # vocabulary. `generation` and `model_code` are absent because this
        # catalog states neither; absence means unknown, never "equal".
        identity = {dimension: str(variant[dimension])
                    for dimension in YEDA_IDENTITY_FIELDS.values() if dimension in variant}
        # The variant's own year range, as the catalog states it.
        time_scope: dict[str, Any] = {"year_start": variant["year_start"]}
        if "year_end" in variant:
            time_scope["year_end"] = variant["year_end"]
        projection = self._projection_record(model, variant)

        facts, fragments = [], []
        for index, (field_key, catalog_field, unit) in enumerate(YEDA_FACT_FIELDS):
            # The locator names the UPSTREAM field the value was read from; the
            # fact's field_key names what the value MEANS. Keeping them apart is
            # what lets a nominal engine-class label be recorded honestly
            # without pretending it is a homologated measurement.
            locator = record_field_locator(record_id, (catalog_field,))
            facts.append(StructuredEvidenceFact(
                entity_key=entity, field_key=field_key, value=projection[catalog_field],
                unit=unit, time_scope=time_scope, geography=model["market"],
                market=model["market"], identity=identity, locator=locator))
            fragments.append(structured_projection(
                record=projection, fields=(*YEDA_CONTEXT_FIELDS, catalog_field),
                locator=locator, fragment_index=index))

        source = VersionedEvidenceSource(
            agent=self._agent, url=str(source_material["canonical_url"]),
            title=self._title(model, variant), domain=self._domain,
            source_type=YEDA_SOURCE_TYPE, source_strength=self._source_strength,
            source_date=str(source_material["catalog_generated_at"]),
            query=record_id, tool_operation=f"{self.tool}.{self.operation}",
            version=SourceVersion(kind="git_commit",
                                  identifier=str(source_material["commit_sha"])),
            confidence=self._confidence(model))
        return build_evidence_bundle(source=source, locator_scope=(record_id,),
                                     facts=facts, fragments=fragments)

    @staticmethod
    def _projection_record(model: Mapping[str, Any],
                           variant: Mapping[str, Any]) -> dict[str, Any]:
        """The flat, scalar record every locator and projection reads from.

        Built from EXPLICITLY named fields of the operation's declared output,
        under the upstream catalog's own field names, so a locator points at
        the field the source actually has.
        """
        return {
            "make": model["make"], "commercial_model": model["commercial_model"],
            "market": model["market"], "model_year": model["model_year"],
            "fuel_type": variant["fuel_type"],
            "horsepower_hp": variant["horsepower_hp"],
            "engine_displacement_l": variant["nominal_engine_displacement_l"],
        }

    @staticmethod
    def _title(model: Mapping[str, Any], variant: Mapping[str, Any]) -> str:
        return (f"{model['make']} {model['commercial_model']} "
                f"{variant['fuel_type']} {variant['drivetrain']} "
                f"({model['market']} catalog variant)")

    @staticmethod
    def _confidence(model: Mapping[str, Any]) -> float:
        stated = model.get("profile_confidence")
        if stated is None:
            return YEDA_UNSTATED_CONFIDENCE
        confidence = YEDA_PROFILE_CONFIDENCE.get(str(stated))
        if confidence is None:
            # An unrecognised confidence label is inconsistent manifest/record
            # material, not something to interpret charitably.
            raise EvidenceContractError("EVIDENCE_VALUE_INVALID")
        return confidence




# --- Government: the Israeli Ministry of Transport model registry ------------

#: The facts this operation's output guarantees, as `(semantic field key,
#: upstream CKAN field, unit)`. STATIC server data, exactly as for Yeda.
#:
#: `engine_displacement_cc` is an EXACT homologated displacement in cubic
#: centimetres and is named as one. It deliberately shares no field key with
#: the catalog's nominal `2.5` engine-class label, so the two can never land in
#: one comparison scope and neither a contradiction nor an agreement is
#: manufactured between two statements about different things.
#:
#: `koah_sus` is absent on purpose. See `government.UNMAPPED_FIELDS`: the
#: dataset publishes no definition of it and its values are mutually
#: inconsistent across rows of one model, so it is a real coverage gap rather
#: than a `horsepower_hp` waiting to be claimed.
GOVERNMENT_FACT_FIELDS: tuple[tuple[str, str, str | None], ...] = (
    ("engine_displacement_cc", "nefah_manoa", "cc"),
    ("fuel_type", "delek_nm", None),
    ("official_model_code", "degem_nm", None),
)

#: The qualifying context every Government projection carries, under the
#: registry's OWN field names, so a locator points at a field the source has.
GOVERNMENT_CONTEXT_FIELDS: tuple[str, ...] = ("tozar", "kinuy_mishari", "shnat_yitzur")

#: The closed identity dimensions the registry states for a row, mapped from
#: the operation's declared output onto the shared R4 vocabulary. `generation`
#: is absent because the dataset states none; `engine` and `transmission` are
#: absent because it publishes no comparable designation for either.
GOVERNMENT_IDENTITY_FIELDS: Mapping[str, str] = {
    "body_style": "body_style",
    "drivetrain": "drivetrain",
    "official_model_code": "model_code",
    "trim": "trim",
}

#: The Israeli Ministry of Transport register is a primary regulatory source,
#: and `conflict_policy.SOURCE_TYPE_AUTHORITY` names this type: it is
#: authoritative for `regulatory_identity` and `technical_specification`, and
#: for nothing else. It therefore cannot settle a price, a reliability figure
#: or anything outside the dataset's documented scope, however official it is.
GOVERNMENT_SOURCE_TYPE = "government_registry"

#: The confidence a homologated registry row is recorded at. A constant, not a
#: computation: the dataset expresses no per-row confidence, so nothing here
#: may vary one from the data in front of it.
GOVERNMENT_RECORD_CONFIDENCE = 0.9


class GovernmentRegistryEvidenceMapper:
    """`gov_il.vehicle_registry.get_model_record` -> versioned, located evidence.

    One homologated registry row in; one versioned source out, carrying one
    structured fact and one bounded projection per known field.

    Two choices are load-bearing. The source VERSION is the DATASET's own
    `last_modified`, taken from the CKAN package metadata -- not the retrieval
    time and not a hash of the page that happened to be read, because the
    version must identify the thing the whole source is. And every fragment
    projects the registry's OWN field names and OWN values, Hebrew included,
    so a reviewer reading a durable fragment sees `delek_nm=חשמל/בנזין` -- what
    the ministry published -- rather than this module's reading of it.
    """

    tool = GovernmentVehicleRegistryTool.name
    operation = "get_model_record"

    def __init__(self, *, agent: str = "r5_proof", domain: str = "data.gov.il",
                 source_strength: str = "primary"):
        self._agent, self._domain = agent, domain
        self._source_strength = source_strength

    def map(self, call: ToolCallRecord) -> EvidenceBundle:
        result: Mapping[str, Any] = call.result
        source_material, model, variant = result["source"], result["model"], result["variant"]
        upstream: Mapping[str, Any] = result["upstream_fields"]
        record_id = str(result["durable_record_id"])
        entity = vehicle_entity_key(make=model["make"],
                                    commercial_model=model["commercial_model"],
                                    market=model["market"])
        # A registry row is homologated for ONE model year, and says nothing
        # about any other. The scope states exactly that year at both ends.
        time_scope = {"year_start": model["model_year"], "year_end": model["model_year"]}
        identity = {dimension: str(variant[field])
                    for field, dimension in GOVERNMENT_IDENTITY_FIELDS.items()
                    if field in variant}

        facts, fragments = [], []
        for index, (field_key, upstream_field, unit) in enumerate(GOVERNMENT_FACT_FIELDS):
            locator = record_field_locator(record_id, (upstream_field,))
            facts.append(StructuredEvidenceFact(
                entity_key=entity, field_key=field_key, value=variant[field_key],
                unit=unit, time_scope=time_scope, geography=model["market"],
                market=model["market"], identity=identity, locator=locator))
            fragments.append(structured_projection(
                record=upstream, fields=(*GOVERNMENT_CONTEXT_FIELDS, upstream_field),
                locator=locator, fragment_index=index))

        source = VersionedEvidenceSource(
            agent=self._agent, url=str(source_material["canonical_url"]),
            title=(f"{model['make']} {model['commercial_model']} "
                   f"{variant['trim']} ({model['model_year']} {source_material['publisher']} "
                   "registry record)"),
            domain=self._domain, source_type=GOVERNMENT_SOURCE_TYPE,
            source_strength=self._source_strength,
            source_date=str(source_material["dataset_version"]),
            query=str(source_material["query"]),
            tool_operation=f"{self.tool}.{self.operation}",
            version=SourceVersion(kind="dataset_version",
                                  identifier=str(source_material["dataset_version"])),
            # The register states what it homologated. There is no confidence
            # label to render and none is invented, so a primary regulatory
            # record is recorded at the contract's stated-fact confidence.
            confidence=GOVERNMENT_RECORD_CONFIDENCE)
        return build_evidence_bundle(source=source, locator_scope=(record_id,),
                                     facts=facts, fragments=fragments)


# --- Web: the official Toyota Israel archived-model page ---------------------

#: Toyota Israel's own archived-model page is a manufacturer publication, but
#: it is NOT a specification sheet, and `conflict_policy.SOURCE_TYPE_AUTHORITY`
#: deliberately does not name this type. Under the R4 policy an unknown source
#: type is authoritative for nothing, which is exactly the intent: an official
#: page may establish who a model IS and that its marketing ENDED without
#: thereby acquiring authority over a displacement, a power figure or a price.
#: Typing it `manufacturer_specification` would hand it precisely that
#: authority on the strength of the brand, for statements it never makes.
WEB_SOURCE_TYPE = "manufacturer_archived_model_page"

#: The confidence an official first-party page is recorded at. As above: a
#: constant, because the page expresses no confidence about itself.
WEB_DOCUMENT_CONFIDENCE = 0.8


class ToyotaArchivedDocumentEvidenceMapper:
    """`toyota.archived_model_document.read_archived_model_document` -> evidence.

    One saved official page in; one versioned source out, carrying one fact and
    one VERBATIM EXCERPT per statement the closed table located. The excerpt is
    the page's own visible text at the exact span the tool returned, so every
    fact points at text a reviewer can find in the committed bytes rather than
    at a summary of them.

    The span was proven inside the tool, against the whole projection, because
    a ten-thousand-character Hebrew projection cannot cross the tool-result
    size bound -- see `web.ToyotaArchivedModelDocumentTool`. This mapper
    therefore re-checks what it CAN check without the document: that the
    excerpt is non-empty and exactly as long as the span it claims. A statement
    whose text and offsets disagree is refused rather than recorded.

    Two absences are deliberate and are the honest reading of this source:

    *   `identity` is EMPTY. The page states no generation, engine,
        transmission, drivetrain, body style, model code or trim, and under R4
        an empty identity only ever matches another empty identity -- it never
        widens a comparison.
    *   `time_scope` is EMPTY. The page states no model year, so the evidence
        claims none. "Marketing has ended" is a statement about the model, not
        about a year the page never names.

    The ENTITY uses Toyota's own commercial name, `RAV4 Plug-in`, which is not
    the bare `RAV4` the catalog and the registry use. Normalizing it to match
    them would silently merge this page's statement into their entity on the
    strength of a guess; leaving it as the source wrote it keeps the
    unresolved naming difference visible, which is what it is.
    """

    tool = ToyotaArchivedModelDocumentTool.name
    operation = "read_archived_model_document"

    def __init__(self, *, agent: str = "r5_proof", domain: str = "www.toyota.co.il",
                 source_strength: str = "primary"):
        self._agent, self._domain = agent, domain
        self._source_strength = source_strength

    def map(self, call: ToolCallRecord) -> EvidenceBundle:
        result: Mapping[str, Any] = call.result
        source_material, model = result["source"], result["model"]
        document_id = str(source_material["document_id"])
        entity = vehicle_entity_key(make=model["make"],
                                    commercial_model=model["commercial_model"],
                                    market=model["market"])

        facts, fragments = [], []
        for index, statement in enumerate(result["statements"]):
            start, end = int(statement["char_start"]), int(statement["char_end"])
            excerpt = str(statement["text"])
            if not excerpt or end - start != len(excerpt):
                raise EvidenceContractError("EVIDENCE_LOCATOR_OUT_OF_SCOPE")
            locator = document_span_locator(document_id, start, end)
            facts.append(StructuredEvidenceFact(
                entity_key=entity, field_key=str(statement["field_key"]),
                value=str(statement["value"]), unit=None, time_scope={},
                geography=model["market"], market=model["market"],
                identity={}, locator=locator))
            # Same normalization and same derived hash every durable fragment
            # gets; the excerpt is the page's own characters, never a summary.
            text = normalize_fragment_text(excerpt)
            fragments.append(FocusedEvidenceFragment(
                fragment_type="verbatim_excerpt", text=text, locator=locator,
                fragment_index=index, content_hash=fragment_content_hash(text)))

        source = VersionedEvidenceSource(
            agent=self._agent, url=str(source_material["canonical_url"]),
            title=f"{model['make']} {model['commercial_model']} ({model['market']} archived model page)",
            domain=self._domain, source_type=WEB_SOURCE_TYPE,
            source_strength=self._source_strength,
            source_date=str(source_material["retrieved_at_utc"]),
            query=document_id, tool_operation=f"{self.tool}.{self.operation}",
            # The page publishes no ETag, no Last-Modified and no revision, so
            # the SHA-256 of the EXACT FULL captured body is the only immutable
            # identifier it has -- which is the case `content_sha256` exists
            # for. It is the whole response, never a hash of a chosen span.
            version=SourceVersion(kind="content_sha256",
                                  identifier=str(source_material["content_sha256"])),
            confidence=WEB_DOCUMENT_CONFIDENCE)
        return build_evidence_bundle(source=source, locator_scope=(document_id,),
                                     facts=facts, fragments=fragments)


def proof_evidence_mappers() -> EvidenceMapperRegistry:
    """The R5 proof mapper allowlist.

    Deliberately a FUNCTION, not a module-level singleton, for the same reason
    `backend/testing/evidence_mappers.offline_evidence_mappers` is one: nothing
    importable from here can be mistaken for -- or accidentally merged into --
    the empty production registry.
    """
    return EvidenceMapperRegistry((YedaCatalogEvidenceMapper(),
                                  GovernmentRegistryEvidenceMapper(),
                                  ToyotaArchivedDocumentEvidenceMapper()))


__all__ = ["GOVERNMENT_CONTEXT_FIELDS", "GOVERNMENT_FACT_FIELDS",
           "GOVERNMENT_IDENTITY_FIELDS", "GOVERNMENT_RECORD_CONFIDENCE",
           "GOVERNMENT_SOURCE_TYPE", "WEB_DOCUMENT_CONFIDENCE", "WEB_SOURCE_TYPE",
           "YEDA_CONTEXT_FIELDS", "YEDA_FACT_FIELDS", "YEDA_PROFILE_CONFIDENCE",
           "YEDA_SOURCE_TYPE", "YEDA_UNSTATED_CONFIDENCE",
           "GovernmentRegistryEvidenceMapper", "ToyotaArchivedDocumentEvidenceMapper",
           "YedaCatalogEvidenceMapper", "proof_evidence_mappers"]

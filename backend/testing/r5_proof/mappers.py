"""R5: trusted, operation-specific evidence mappers for the real proof tools.

One mapper per exact `(tool, operation)` pair. Each is shipped, reviewable
server code held to the same rules a production adapter's mapper must meet,
and NONE of them is added to `PRODUCTION_EVIDENCE_MAPPERS`, which stays empty.

What every mapper here does, and what a real one must also do:

*   Accept ONLY a validated `ToolCallRecord`. The registry has already checked
    the operation, the resolved payload and the result against the registered
    schemas, so a mapper reads named, guaranteed fields -- never "whatever key
    looks like text", and never a model completion.
*   Take the source VERSION from the source's own immutable identifier (here,
    the pinned git commit), never from a timestamp and never from a hash of
    the fragment that happened to be selected.
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
"""

from __future__ import annotations

from typing import Any, Mapping

from backend.engines.swarm_v2.evidence_contracts import (EvidenceBundle, EvidenceContractError,
                                                         SourceVersion, StructuredEvidenceFact,
                                                         VersionedEvidenceSource,
                                                         build_evidence_bundle,
                                                         record_field_locator,
                                                         structured_projection)
from backend.engines.swarm_v2.evidence_mapping import EvidenceMapperRegistry
from backend.engines.swarm_v2.tool_calls import ToolCallRecord

from .identity import vehicle_entity_key
from .tools import YEDA_IDENTITY_FIELDS

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


def proof_evidence_mappers() -> EvidenceMapperRegistry:
    """The R5 proof mapper allowlist.

    Deliberately a FUNCTION, not a module-level singleton, for the same reason
    `backend/testing/evidence_mappers.offline_evidence_mappers` is one: nothing
    importable from here can be mistaken for -- or accidentally merged into --
    the empty production registry.
    """
    return EvidenceMapperRegistry((YedaCatalogEvidenceMapper(),))


__all__ = ["YEDA_CONTEXT_FIELDS", "YEDA_FACT_FIELDS", "YEDA_PROFILE_CONFIDENCE",
           "YEDA_SOURCE_TYPE", "YEDA_UNSTATED_CONFIDENCE", "YedaCatalogEvidenceMapper",
           "proof_evidence_mappers"]

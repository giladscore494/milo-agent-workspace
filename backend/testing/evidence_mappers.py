"""Trusted OFFLINE evidence mappers for the R3 fixture tools.

These are server code, not test scaffolding that reaches into internals: a
mapper is exactly what a real adapter's mapper will be, and it is held to the
same rules.  They live under backend/testing/ for the same reason
memory_repository.py and e2e_app.py do -- they are shipped, reviewable server
code that is never wired into a production path.

What every mapper here demonstrates, and what a real one must also do:

*   It is registered for ONE exact `tool.operation` pair and reads only the
    fields that operation's declared output schema guarantees.  There is no
    scan over "whatever key looks like text", and no key name is interpreted
    for its meaning at runtime.
*   The source VERSION comes from the tool result's own version field (the
    dataset version, the document revision), never from a timestamp and never
    from a hash of the fragment that was selected.
*   Every fact and every fragment carries the exact locator it was read from,
    and the locator is validated against the record/document the tool actually
    returned.
*   A numeric fact carries its unit.  R3 preserves the unit; it never
    converts one.
*   Nothing consults a model, a network, a database or another tool.

The production registry (`PRODUCTION_EVIDENCE_MAPPERS`) does not contain any
of these, and the production ToolRegistry does not register their tools.
"""

from __future__ import annotations

from typing import Any, Mapping

from backend.engines.swarm_v2.evidence_contracts import (EvidenceBundle, EvidenceContractError,
                                                         SourceVersion, StructuredEvidenceFact,
                                                         VersionedEvidenceSource,
                                                         build_evidence_bundle,
                                                         document_span_locator,
                                                         record_field_locator,
                                                         structured_projection, verbatim_excerpt)
from backend.engines.swarm_v2.evidence_mapping import EvidenceMapperRegistry
from backend.engines.swarm_v2.tool_calls import ToolCallRecord

# The fields this registry operation is known to expose, and the ones that may
# appear in a projection as qualifying context.  Both lists are STATIC server
# data: a mapper never derives them from the record it is looking at, so a
# hostile fixture cannot widen what gets projected.
REGISTRY_FACT_FIELDS: tuple[tuple[str, str | None], ...] = (
    # (field name in the record, the unit its value is stated in)
    ("engine_displacement_cc", "cc"),
    ("fuel_type", None),
)
REGISTRY_PRICE_FIELD = "list_price"
REGISTRY_PRICE_UNIT_FIELD = "price_currency"
REGISTRY_CONTEXT_FIELDS: tuple[str, ...] = ("model_name", "model_year", "market")


class StructuredRegistryEvidenceMapper:
    """`mock.structured_registry.get_record` -> versioned, located evidence.

    One record in, one versioned source out, with one structured fact and one
    bounded projection per known field.  The record is never flattened into a
    string: each fact keeps its own value, its own unit and its own field
    locator, and the projection exists only so a grounded verifier still has
    textual material -- which is why every fragment it produces is typed
    `structured_projection` and can never be presented as a quote.
    """

    tool = "mock.structured_registry"
    operation = "get_record"

    def __init__(self, *, agent: str = "evidence", domain: str = "registry.example.test",
                 source_strength: str = "strong", confidence: float = 0.9):
        self._agent, self._domain = agent, domain
        self._source_strength, self._confidence = source_strength, confidence

    def map(self, call: ToolCallRecord) -> EvidenceBundle:
        result = call.result
        record = result["record"]
        if not isinstance(record, Mapping):
            raise EvidenceContractError("EVIDENCE_VALUE_INVALID")
        record_id = str(result["record_id"])
        version = SourceVersion(kind="dataset_version",
                                identifier=str(result["dataset_version"]))
        # The unit of a price is the record's own currency field, read as data
        # from a declared field -- never inferred from the number itself.
        priced = (REGISTRY_PRICE_FIELD, str(record[REGISTRY_PRICE_UNIT_FIELD]))
        facts, fragments = [], []
        for index, (name, unit) in enumerate((*REGISTRY_FACT_FIELDS, priced)):
            locator = record_field_locator(record_id, (name,))
            facts.append(StructuredEvidenceFact(
                entity_key=record_id, field_key=name, value=record[name], unit=unit,
                time_scope={"model_year": record["model_year"]},
                geography=str(record["market"]), market=str(record["market"]),
                locator=locator))
            fragments.append(structured_projection(
                record=record, fields=(*REGISTRY_CONTEXT_FIELDS, name), locator=locator,
                fragment_index=index))
        source = VersionedEvidenceSource(
            agent=self._agent, url=f"https://{self._domain}/records/{record_id}",
            title=str(record["model_name"]), domain=self._domain, source_type="structured",
            source_strength=self._source_strength, source_date=None, query=record_id,
            tool_operation=f"{self.tool}.{self.operation}", version=version,
            confidence=self._confidence)
        return build_evidence_bundle(source=source, locator_scope=(record_id,),
                                     facts=facts, fragments=fragments)


class DocumentArchiveEvidenceMapper:
    """`mock.document_archive.locate_passage` -> a focused, located excerpt.

    The adapter reports WHERE the answering passage is; this mapper cuts the
    excerpt at exactly that span out of the document the adapter returned, so
    the stored evidence is the passage itself no matter how much irrelevant
    introduction precedes it.  There is no prefix, no truncation and no whole
    page: the span is validated against the real document text, and a span
    that does not belong to it fails closed.
    """

    tool = "mock.document_archive"
    operation = "locate_passage"

    def __init__(self, *, agent: str = "evidence", domain: str = "docs.example.test",
                 source_strength: str = "strong", confidence: float = 0.9):
        self._agent, self._domain = agent, domain
        self._source_strength, self._confidence = source_strength, confidence

    def map(self, call: ToolCallRecord) -> EvidenceBundle:
        result: Mapping[str, Any] = call.result
        document_id, field = str(result["document_id"]), str(result["field"])
        locator = document_span_locator(document_id, int(result["match_start"]),
                                        int(result["match_end"]),
                                        section=str(result["section"]))
        fragment = verbatim_excerpt(document_text=str(result["text"]), locator=locator,
                                    fragment_index=0)
        fact = StructuredEvidenceFact(entity_key=document_id, field_key=field,
                                      value=result["value"], unit=str(result["unit"]),
                                      locator=locator)
        source = VersionedEvidenceSource(
            agent=self._agent, url=f"https://{self._domain}/{document_id}",
            title=str(result["section"]), domain=self._domain, source_type="document",
            source_strength=self._source_strength, source_date=None, query=field,
            tool_operation=f"{self.tool}.{self.operation}",
            # The document's OWN revision.  `retrieved_at` is not a version and
            # neither is the excerpt's content hash.
            version=SourceVersion(kind="document_revision", identifier=str(result["revision"])),
            confidence=self._confidence)
        return build_evidence_bundle(source=source, locator_scope=(document_id,),
                                     facts=(fact,), fragments=(fragment,))


def offline_evidence_mappers(**overrides: Any) -> EvidenceMapperRegistry:
    """The offline mapper allowlist used by the R3 tests.

    Deliberately a FUNCTION, not a module-level singleton: nothing importable
    from here can be mistaken for -- or accidentally merged into -- the empty
    production registry.  `mock.structured_registry.list_records` is
    conspicuously absent, so an operation with no mapper stays unmapped.
    """
    return EvidenceMapperRegistry((StructuredRegistryEvidenceMapper(**overrides),
                                   DocumentArchiveEvidenceMapper(**overrides)))


__all__ = ["REGISTRY_CONTEXT_FIELDS", "REGISTRY_FACT_FIELDS", "REGISTRY_PRICE_FIELD",
           "REGISTRY_PRICE_UNIT_FIELD", "DocumentArchiveEvidenceMapper",
           "StructuredRegistryEvidenceMapper", "offline_evidence_mappers"]

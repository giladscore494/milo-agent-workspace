"""R3: the trusted path from a validated tool result to real evidence.

R2 introduced `ToolCallRecord`: the post-execution seam that carries a
Registry-validated tool result together with server-resolved provenance, and
that a worker model can neither reach nor forge.  R2 deliberately left it
unwired, because a validated result is evidence MATERIAL, not a fact.

This module is the missing conversion, and it is the ONLY one:

    validated ToolCallRecord
      -> the mapper registered for THAT tool operation (trusted server code)
      -> EvidenceBundle (versioned source + structured facts + focused fragments)
      -> EvidenceBoard (lease-guarded, idempotent, append-only persistence)

Boundaries this module exists to keep:

*   `ToolCallRecord` is the only accepted input.  A worker completion, a task
    result, a plain mapping and a model-authored payload are all rejected by
    type before anything else happens, so a model can never turn its own
    answer into evidence.
*   Mapping is OPERATION-SPECIFIC.  A mapper is registered for one exact
    `tool.operation` pair and knows that operation's declared output schema.
    There is no generic parser that guesses meaning from arbitrary field
    names, and no fallback that scans a result for text-shaped keys.
*   An operation with no registered mapper FAILS CLOSED.  It never falls back
    to the pre-R3 prefix extractor: no source, no fragment and no claim is
    written for a result nobody knows how to read.
*   The model chooses nothing here.  It cannot invent source metadata, select
    a source version, select a locator, write a fragment, or promote its
    completion into evidence.

Catalog PR3 made the production registry NON-EMPTY for the first time.  Exactly
one operation is mapped -- `catalog.government_vehicle.resolve_variant`, the
only Government read that ends with one exact register row -- and the trusted
sink is routed so that the tool's seven other read operations record nothing at
all rather than failing the task that called them
(`RegisteredOperationEvidenceSink`).  No Yeda, CKAN or web tool is registered,
as a tool or as a mapper.  The contract is additionally proven end to end
against deterministic offline test tools and trusted offline test mappers
(backend/testing/evidence_mappers.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Protocol

from .evidence_contracts import (EvidenceBundle, EvidenceContractError,
                                 revalidate_evidence_bundle)
from .tool_calls import ToolCallRecord

EVIDENCE_MAPPING_REASONS = frozenset({
    "EVIDENCE_BUNDLE_INVALID",
    "EVIDENCE_BUNDLE_PROVENANCE_MISMATCH",
    "EVIDENCE_MAPPER_NOT_REGISTERED",
    "EVIDENCE_RESULT_STATES_NO_FACT",
    "EVIDENCE_SOURCE_NOT_TRUSTED",
})


class _NoEvidence:
    """What a REGISTERED mapper returns for a result that states no fact.

    Distinct from "no mapper is registered", and the distinction matters. An
    unregistered operation is one nobody has reviewed as evidence-bearing, and
    it fails closed. A registered operation can still legitimately answer with
    a result that states nothing about any one record -- `resolve_variant`
    reporting an AMBIGUITY is the exact case: it is a true, useful answer, and
    there is no single register row whose fields could be quoted for it.

    Without this marker a mapper would have to choose between inventing a fact
    for an ambiguous answer and failing the task that asked an entirely
    reasonable question. It returns neither.

    A singleton rather than `None`, so a mapper that forgets to return anything
    at all is still an invalid bundle rather than a silent decline.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return "NO_EVIDENCE"

    def __bool__(self) -> bool:
        return False


NO_EVIDENCE = _NoEvidence()


class EvidenceMappingError(ValueError):
    """An acquisition failure carrying ONLY a static, code-owned reason.

    The tool result, the mapper traceback and the rejected bundle never
    travel with the classification, so the safe representation is fit for a
    durable task result, a run event and telemetry alike.
    """

    MESSAGES = {
        "EVIDENCE_BUNDLE_INVALID": "the trusted mapper did not produce a valid evidence bundle",
        "EVIDENCE_BUNDLE_PROVENANCE_MISMATCH": "the evidence bundle does not describe the executed tool operation",
        "EVIDENCE_MAPPER_NOT_REGISTERED": "no evidence mapper is registered for this tool operation",
        "EVIDENCE_RESULT_STATES_NO_FACT": "this validated tool result states no evidence-bearing fact",
        "EVIDENCE_SOURCE_NOT_TRUSTED": "evidence may only be acquired from a validated tool call record",
    }

    def __init__(self, reason_code: str):
        if reason_code not in EVIDENCE_MAPPING_REASONS:
            raise ValueError("evidence mapping reason must come from the static allowlist")
        self.reason_code = reason_code
        self.safe_message = self.MESSAGES[reason_code]
        super().__init__(self.safe_message)


class EvidenceMapper(Protocol):
    """Trusted server code that knows ONE registered operation's shape.

    A mapper reads only the validated result of the operation it is
    registered for, and returns a complete `EvidenceBundle`.  It never
    performs a network call, a database read, a provider call or a tool call,
    and it never consults a model.
    """

    tool: str
    operation: str

    def map(self, record: ToolCallRecord) -> EvidenceBundle | _NoEvidence: ...


class EvidenceMapperRegistry:
    """The explicit allowlist of operations whose results may become evidence.

    Registration is by exact `(tool, operation)` pair, mirroring the
    ToolRegistry's own posture: a tool is a namespace and an OPERATION is the
    unit that carries a contract.  `mock.catalog.search` gaining an evidence
    mapper says nothing about `mock.catalog.list_models`.
    """

    def __init__(self, mappers: Iterable[EvidenceMapper] = ()):
        self._mappers: dict[tuple[str, str], EvidenceMapper] = {}
        for mapper in mappers:
            tool, operation = str(getattr(mapper, "tool", "")), str(getattr(mapper, "operation", ""))
            if not tool or not operation or not callable(getattr(mapper, "map", None)):
                raise ValueError("an evidence mapper must name one tool operation and be callable")
            if (tool, operation) in self._mappers:
                raise ValueError(f"duplicate evidence mapper: {tool}.{operation}")
            self._mappers[(tool, operation)] = mapper

    @property
    def registered(self) -> frozenset[tuple[str, str]]:
        return frozenset(self._mappers)

    def mapper_for(self, tool: str, operation: str) -> EvidenceMapper | None:
        return self._mappers.get((str(tool), str(operation)))

    def map(self, record: Any) -> EvidenceBundle:
        """Convert ONE validated tool result into a validated evidence bundle.

        Strict: a result the registered mapper DECLINES is a refusal here, so
        a caller that requires evidence gets one static reason instead of a
        `None` it might forget to check. `map_optional` is the same path with
        the decline expressed as `None`.
        """
        bundle = self.map_optional(record)
        if bundle is None:
            raise EvidenceMappingError("EVIDENCE_RESULT_STATES_NO_FACT")
        return bundle

    def map_optional(self, record: Any) -> EvidenceBundle | None:
        """The same conversion, with an explicit DECLINE expressed as `None`.

        Fails closed at every step: an untrusted input, an unmapped
        operation, a mapper that raises, a mapper that returns something else
        and a bundle describing a different operation all raise instead of
        producing partial evidence. The ONE thing that is not a failure is a
        registered mapper returning `NO_EVIDENCE` for a result that states no
        fact -- see `_NoEvidence`.
        """
        if not isinstance(record, ToolCallRecord):
            # A worker completion, a task result or a hand-built mapping can
            # never enter the evidence path: only the R2 seam constructs this
            # type, and only after the Registry validated the call.
            raise EvidenceMappingError("EVIDENCE_SOURCE_NOT_TRUSTED")
        mapper = self.mapper_for(record.tool, record.operation)
        if mapper is None:
            # No silent fallback to the pre-R3 generic text extractor.
            raise EvidenceMappingError("EVIDENCE_MAPPER_NOT_REGISTERED")
        try:
            bundle = mapper.map(record)
        except EvidenceContractError:
            # Already a static, safe R3 reason: keep the precise cause.
            raise
        except Exception:
            # `from None`: a mapper traceback can quote the tool result.
            raise EvidenceMappingError("EVIDENCE_BUNDLE_INVALID") from None
        if bundle is NO_EVIDENCE:
            # The mapper READ the result and found nothing to record. Not a
            # failure, and not a fallback: no source, no fragment and no claim
            # is written, exactly as for an unmapped operation.
            return None
        if not isinstance(bundle, EvidenceBundle):
            raise EvidenceMappingError("EVIDENCE_BUNDLE_INVALID")
        # The mapper's object is not trusted as validated just because it has
        # the right type: it is rebuilt from a copy of its own data so every
        # contract validator runs again, here, before anything downstream can
        # see it.  A bundle assembled around validation, or mutated after it,
        # fails at this line with a static reason.
        bundle = revalidate_evidence_bundle(bundle)
        if bundle.source.tool_operation != f"{record.tool}.{record.operation}":
            # The bundle must describe the operation that actually ran, so a
            # mapper cannot attribute evidence to a different capability.
            raise EvidenceMappingError("EVIDENCE_BUNDLE_PROVENANCE_MISMATCH")
        return bundle


#: The production allowlist, as static data: the exact `(tool, operation)`
#: pairs whose results may become evidence in a release.
#:
#: Catalog PR3 made this non-empty for the first time. One entry, and it is the
#: only Government operation that ends with exactly one register row; the other
#: seven read operations of the same tool are legitimate answers that state no
#: fact about one vehicle, and they are absent here rather than mapped loosely.
#:
#: Written as literals so a reader -- and a test -- can see what production maps
#: without importing the catalog package or constructing anything.
PRODUCTION_EVIDENCE_MAPPER_OPERATIONS = frozenset({
    ("catalog.government_vehicle", "resolve_variant"),
})


def production_evidence_mappers() -> EvidenceMapperRegistry:
    """Build the production evidence-mapper allowlist.

    A FUNCTION rather than the module constant it replaced, for one structural
    reason: the production mapper lives in `backend/catalog/government/`, which
    imports this package's evidence CONTRACTS. Importing it back at module
    level here would make the two packages initialize each other -- and because
    importing any `backend.engines.swarm_v2` submodule runs this package's
    `__init__`, whichever side happened to be imported first would fail. The
    deferred import keeps the dependency one-way at load time and explicit at
    call time.

    Trusted wiring calls this once, in the worker construction path, alongside
    registering the Tool whose operation it maps. Registering a mapper without
    registering its tool would be inert; registering the tool without the
    mapper would make its results material that nothing can turn into evidence.
    """
    from backend.catalog.government.evidence import GovernmentVariantEvidenceMapper

    registry = EvidenceMapperRegistry((GovernmentVariantEvidenceMapper(),))
    if registry.registered != PRODUCTION_EVIDENCE_MAPPER_OPERATIONS:
        # The literal list above is what a reviewer and a test read. If the
        # built registry ever disagrees with it, the documented allowlist is
        # wrong -- which is exactly the drift this check exists to refuse.
        raise ValueError("the production evidence mapper allowlist does not match its declaration")
    return registry


@dataclass(frozen=True)
class AcquiredEvidence:
    """The durable rows one validated tool call produced, in write order."""

    source: Mapping[str, Any]
    fragments: tuple[Mapping[str, Any], ...]
    claims: tuple[Mapping[str, Any], ...]


class TrustedEvidenceAcquisition:
    """The R3 `ToolResultSink`: validated tool result in, durable evidence out.

    Constructed by trusted wiring that already holds the run's Evidence Board
    (and therefore its worker lease), so every write this performs goes
    through the same lease-guarded, idempotent RPCs as every other evidence
    write.  It holds no repository handle, no credentials and no model
    client of its own.

    `task_id` comes from the ToolCallRecord -- the server-resolved plan task
    that made the call -- so evidence provenance is the executed plan, never
    anything a model wrote.
    """

    def __init__(self, *, board: Any, mappers: EvidenceMapperRegistry):
        if not isinstance(mappers, EvidenceMapperRegistry):
            raise ValueError("a trusted evidence mapper registry is required")
        self._board = board
        self._mappers = mappers

    @property
    def mappers(self) -> EvidenceMapperRegistry:
        return self._mappers

    def acquire(self, record: Any) -> AcquiredEvidence | None:
        """Persist one validated tool result's evidence, or `None` for a decline.

        `None` means the registered mapper read the result and found no fact in
        it. Nothing is written in that case -- not a source, not a fragment and
        not a claim -- so a declined result is indistinguishable from a result
        that never reached the board.
        """
        bundle = self._mappers.map_optional(record)
        if bundle is None:
            return None
        return self._board.record_evidence_bundle(bundle, task_key=record.task_id)

    def __call__(self, record: Any) -> None:
        """The ToolResultSink signature the worker's tool loop calls."""
        self.acquire(record)


class RegisteredOperationEvidenceSink:
    """The PRODUCTION `ToolResultSink`: acquire where a mapper exists, else nothing.

    Catalog PR3 registers a real Tool with eight read operations, and only
    SOME of them state evidence-bearing facts about a specific vehicle. The
    rest -- a dataset description, a manufacturer listing, a coverage count --
    are legitimate answers that are not evidence about any one record.

    `TrustedEvidenceAcquisition` alone would fail the task for every one of
    them, because an unmapped operation raises. This sink is the routing that
    makes "wire the sink only for the registered operations" real:

    *   a validated result of a REGISTERED operation goes to the trusted
        acquisition, with every R3/R4 rule applied;
    *   a validated result of any other operation records NOTHING. Not generic
        text evidence, not a bare source, not a fragment: the pre-R3 prefix
        extractor is still unreachable, and an operation nobody reviewed as
        evidence-bearing still produces no evidence;
    *   anything that is not a `ToolCallRecord` is still refused outright, so
        a worker completion or a hand-built mapping can never enter this path.
    """

    def __init__(self, acquisition: TrustedEvidenceAcquisition):
        if not isinstance(acquisition, TrustedEvidenceAcquisition):
            raise ValueError("a trusted evidence acquisition is required")
        self._acquisition = acquisition

    @property
    def acquisition(self) -> TrustedEvidenceAcquisition:
        return self._acquisition

    def acquire(self, record: Any) -> AcquiredEvidence | None:
        """Persist this result's evidence, or `None` when there is none.

        `None` covers both of the routing's legitimate cases: an operation with
        no registered mapper (nothing is written at all) and a registered
        mapper that read the result and found no fact in it. Trusted wiring
        that needs to know WHAT was written -- Catalog PR3's promotion ledger
        is the first -- calls this; `__call__` is the sink signature and
        discards it.
        """
        if not isinstance(record, ToolCallRecord):
            raise EvidenceMappingError("EVIDENCE_SOURCE_NOT_TRUSTED")
        if self._acquisition.mappers.mapper_for(record.tool, record.operation) is None:
            return None
        return self._acquisition.acquire(record)

    def __call__(self, record: Any) -> None:
        self.acquire(record)


__all__ = ["EVIDENCE_MAPPING_REASONS", "NO_EVIDENCE",
           "PRODUCTION_EVIDENCE_MAPPER_OPERATIONS", "AcquiredEvidence", "EvidenceMapper",
           "EvidenceMapperRegistry", "EvidenceMappingError",
           "RegisteredOperationEvidenceSink", "TrustedEvidenceAcquisition",
           "production_evidence_mappers"]

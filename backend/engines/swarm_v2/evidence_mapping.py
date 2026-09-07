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

The production registry is EMPTY.  No Yeda, government, CKAN or web tool is
registered as a tool, and none is registered as an evidence mapper either;
wiring this sink into production additionally requires a real evidence grant,
which is deliberately not created here.  The contract is proven end to end
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
    "EVIDENCE_SOURCE_NOT_TRUSTED",
})


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

    def map(self, record: ToolCallRecord) -> EvidenceBundle: ...


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

        Fails closed at every step: an untrusted input, an unmapped
        operation, a mapper that raises, a mapper that returns something else
        and a bundle describing a different operation all raise instead of
        producing partial evidence.
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


# The production allowlist. Deliberately EMPTY, exactly like the production
# ToolRegistry: no real Yeda, government, CKAN or web source is mapped into
# evidence in this release, and connecting this sink to a production run
# additionally requires a real evidence grant that R3 does not create.
PRODUCTION_EVIDENCE_MAPPERS = EvidenceMapperRegistry()


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

    def acquire(self, record: Any) -> AcquiredEvidence:
        bundle = self._mappers.map(record)
        return self._board.record_evidence_bundle(bundle, task_key=record.task_id)

    def __call__(self, record: Any) -> None:
        """The ToolResultSink signature the worker's tool loop calls."""
        self.acquire(record)


__all__ = ["EVIDENCE_MAPPING_REASONS", "PRODUCTION_EVIDENCE_MAPPERS", "AcquiredEvidence",
           "EvidenceMapper", "EvidenceMapperRegistry", "EvidenceMappingError",
           "TrustedEvidenceAcquisition"]

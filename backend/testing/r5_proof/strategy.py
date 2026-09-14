"""R5: the deterministic task-output strategy for the compiled proof plan.

`GenericWorker`'s `TaskOutputStrategy` seam lets a TOOL-COMPLETE task finish
without a worker model call. This is the strategy the R5 proof injects, and it
is deliberately the least capable thing that can do the job.

It reads three identifiers out of the validated tool results the worker
already captured -- the record the source was read from, the version it was
read at, and the canonical entity of the vehicle -- and returns them as the
task's declared output. It has no access to a model, a network, a database, a
tool or the evidence board, and the object it returns is re-validated against
the task's own closed `output_schema` by the worker before anything sees it.

The three source families answer in three different shapes, so there is one
reader per exact `(tool, operation)` pair, in a closed server-owned table, and
each reads only fields that operation's registered output schema guarantees.
The task's OWN approved calls select the reader -- not a name in the result, a
key that happens to be present, or anything a caller supplied -- so a result
can never route itself to a reader that would read it more generously.

What it explicitly cannot do, and what makes the zero-model path safe:

*   It cannot create evidence. Evidence was already acquired inside the tool
    loop by the trusted mapper and the lease-guarded Evidence Board, before
    this strategy ran; the task output is a summary for the plan, never a fact.
*   It cannot alter source metadata. It copies identifiers out of a result the
    Registry validated; it never authors a version, a locator or a URL.
*   It cannot override verification. Verdicts are decided later, from durable
    evidence, by the deterministic comparison contract.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from .government import GovernmentVehicleRegistryTool
from .identity import vehicle_entity_key
from .tools import YedaVehicleCatalogTool
from .web import ToyotaArchivedModelDocumentTool


class ProofOutputError(ValueError):
    """The strategy could not state the output from validated tool material."""


def _entity(model: Mapping[str, Any]) -> str:
    return vehicle_entity_key(make=model["make"],
                              commercial_model=model["commercial_model"],
                              market=model["market"])


def _from_yeda(result: Mapping[str, Any]) -> dict[str, str]:
    """The pinned catalog variant, identified by its immutable commit."""
    return {"record_id": str(result["record_id"]),
            "source_version": f"git_commit:{result['source']['commit_sha']}",
            "vehicle_entity": _entity(result["model"])}


def _from_government(result: Mapping[str, Any]) -> dict[str, str]:
    """The registry row, identified by the DATASET's own published version."""
    return {"record_id": str(result["durable_record_id"]),
            "source_version": f"dataset_version:{result['source']['dataset_version']}",
            "vehicle_entity": _entity(result["model"])}


def _from_web(result: Mapping[str, Any]) -> dict[str, str]:
    """The saved page, identified by the SHA-256 of its full captured body.

    The page publishes no ETag, no Last-Modified and no revision, so the
    content digest is the only immutable identifier it has -- and it is the
    digest of the whole response, never of the spans that were quoted.
    """
    source = result["source"]
    return {"record_id": str(source["document_id"]),
            "source_version": f"content_sha256:{source['content_sha256']}",
            "vehicle_entity": _entity(result["model"])}


#: The closed reader table: exact `(tool, operation)` -> how that operation's
#: guaranteed output states the three identifiers. An operation absent here has
#: no deterministic output at all, which is why adding a tool to the proof
#: cannot silently acquire one.
PROOF_OUTPUT_READERS: Mapping[tuple[str, str],
                              Callable[[Mapping[str, Any]], dict[str, str]]] = {
    (YedaVehicleCatalogTool.name, "get_model_variant"): _from_yeda,
    (GovernmentVehicleRegistryTool.name, "get_model_record"): _from_government,
    (ToyotaArchivedModelDocumentTool.name, "read_archived_model_document"): _from_web,
}


class VehicleProofOutputStrategy:
    """State one proof task's declared output from its own tool results.

    Bounded by construction: it looks at the task's own approved calls, takes
    the first whose operation has a registered reader, copies three scalar
    identifiers from that call's validated result, and returns them. There is
    no aggregation, no free text and no branch that could grow with the data.
    """

    def __call__(self, *, task: Any, tool_outputs: Mapping[str, Any],
                 dependency_outputs: Mapping[str, Any]) -> dict[str, Any]:
        for call in getattr(task, "tools", ()) or ():
            reader = PROOF_OUTPUT_READERS.get((str(call.name), str(call.operation)))
            if reader is None:
                continue
            result = tool_outputs.get(str(call.call_id))
            if isinstance(result, Mapping):
                return reader(result)
        # The task is not tool-complete, or names no operation this strategy
        # can state an output for. Failing here is correct: the worker turns it
        # into a static reason and never falls back to a model call, so an
        # incomplete task can never be quietly answered.
        raise ProofOutputError("the proof task produced no result it can state an output from")


__all__ = ["PROOF_OUTPUT_READERS", "ProofOutputError", "VehicleProofOutputStrategy"]

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

from typing import Any, Mapping

from .identity import vehicle_entity_key


class ProofOutputError(ValueError):
    """The strategy could not state the output from validated tool material."""


class VehicleProofOutputStrategy:
    """State one proof task's declared output from its own tool results.

    Bounded by construction: it looks at exactly one named call result, copies
    three scalar identifiers, and returns them. There is no aggregation, no
    free text and no branch that could grow with the data.
    """

    def __init__(self, *, call_id: str = "yeda-1"):
        self._call_id = call_id

    def __call__(self, *, task: Any, tool_outputs: Mapping[str, Any],
                 dependency_outputs: Mapping[str, Any]) -> dict[str, Any]:
        result = tool_outputs.get(self._call_id)
        if not isinstance(result, Mapping):
            # The task is not tool-complete. Failing here is correct: the
            # worker turns it into a static reason and never falls back to a
            # model call, so an incomplete task can never be quietly answered.
            raise ProofOutputError("the proof task produced no result for its own call")
        model, source = result["model"], result["source"]
        return {
            "record_id": str(result["record_id"]),
            "source_version": f"git_commit:{source['commit_sha']}",
            "vehicle_entity": vehicle_entity_key(
                make=model["make"], commercial_model=model["commercial_model"],
                market=model["market"]),
        }


__all__ = ["ProofOutputError", "VehicleProofOutputStrategy"]

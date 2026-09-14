"""R5: the server-owned deterministic plan compiler for the proof request.

The R5 proof must reach a real result with zero model calls, which includes
the Commander. This module is how: a `CommanderClient` that compiles ONE
recognised proof request into ONE exact plan, from a static server-owned
table, and hands it to the ordinary `Commander` -> `PlanValidator` firewall
like any other candidate plan.

What makes it safe, and what a production compiler would also have to do:

*   The request is selected by CONSTRUCTOR ARGUMENT from trusted wiring, and
    is looked up in a closed table. The run's `objective` and `context` --
    the only fields a client can influence -- are never parsed for a request
    key, never used to choose a tool, and never used to build an argument.
*   It grants nothing. A plan may REQUEST a registered capability; scopes,
    capabilities and write approval live on the server-owned `ToolContext`
    and are unreachable from here, exactly as they are for a model-authored
    plan.
*   Its output is inert JSON until `PlanValidator` approves it. The compiler
    gets no shortcut past the firewall: same contract, same limits, same
    semantic rules.
*   It emits only allowlisted calls: every planned call names a tool
    operation this proof registers, with arguments taken verbatim from the
    static table.

Pure module: no I/O, no provider, no model, no global mutable state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .government import GovernmentVehicleRegistryTool, WLTP_RESOURCE_ID
from .tools import YedaVehicleCatalogTool
from .web import ToyotaArchivedModelDocumentTool

#: The task output every proof task declares. Small, closed and structural:
#: the deterministic strategy states exactly these fields and the worker
#: re-validates them against this same schema.
PROOF_OUTPUT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "record_id": {"type": "string"},
        "vehicle_entity": {"type": "string"},
        "source_version": {"type": "string"},
    },
    "required": ["record_id", "source_version", "vehicle_entity"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class ProofCall:
    """One exact allowlisted call, with its arguments fixed in advance."""

    call_id: str
    tool: str
    operation: str
    arguments: Mapping[str, Any]

    def as_plan_call(self) -> dict[str, Any]:
        return {"call_id": self.call_id, "name": self.tool, "operation": self.operation,
                "arguments": dict(self.arguments), "dependency_bindings": []}


@dataclass(frozen=True)
class ProofTask:
    """One planned task of the proof, with its exact call list."""

    task_id: str
    goal: str
    scope: str
    calls: tuple[ProofCall, ...]
    required_fields: tuple[str, ...]
    minimum_sources: int = 1
    min_confidence: float = 0.5
    #: Whether this task is ALLOWED not to resolve. False for every task that
    #: must produce evidence -- a failure there is a defect and must stop the
    #: run. True only for a task whose honest answer may be "this cannot be
    #: determined conservatively": its failure is then a recorded unresolved
    #: item in `needs_review`, which is what such a question deserves.
    allow_partial: bool = False

    def as_plan_task(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id, "goal": self.goal, "scope": self.scope,
            "dependencies": [], "tools": [call.as_plan_call() for call in self.calls],
            "output_schema": dict(PROOF_OUTPUT_SCHEMA),
            "evidence": {"minimum_sources": self.minimum_sources,
                         "required_fields": list(self.required_fields),
                         "min_confidence": self.min_confidence},
            "priority": 50, "recursion_depth": 0, "estimated_cost_units": 0,
            "completion": {"required_outputs": sorted(PROOF_OUTPUT_SCHEMA["required"]),
                           "evidence_satisfied": True, "allow_partial": self.allow_partial},
        }


@dataclass(frozen=True)
class ProofRequest:
    """ONE recognised proof request and the exact plan it compiles to."""

    key: str
    objective: str
    tasks: tuple[ProofTask, ...] = field(default_factory=tuple)

    def as_plan(self) -> dict[str, Any]:
        return {
            "version": "1", "objective": self.objective,
            "graph": {"tasks": [task.as_plan_task() for task in self.tasks]},
            "assignments": [{"task_id": task.task_id, "worker_role": "vehicle_evidence",
                             "context_task_ids": []} for task in self.tasks],
            # No replan allowance: this compiler has no research capability to
            # spend one on, and saying so is more honest than reserving one.
            "max_replans": 0, "estimated_cost_units": 0,
        }


#: The one vehicle this proof is about, as the catalog and the register both
#: name it. `fuel_type` is present because make + commercial model + model year
#: matches FIVE RAV4 variants in the catalog and three registry rows: without
#: it the registered operations fail closed as ambiguous rather than choosing.
_VEHICLE = {"make": "Toyota", "commercial_model": "RAV4", "market": "IL"}

#: The registry row the 2021 question resolves to. It is NOT a chosen row: for
#: model year 2021 exactly one committed record carries this commercial model,
#: this market, plug-in propulsion and four-wheel drive, and the `_id` below is
#: stated so the tool can CHECK the row it selected is the row this plan was
#: reviewed against. Naming it can never resolve an ambiguity -- a request that
#: matches two rows fails whether or not it names one of them.
GOVERNMENT_RECORD_ID_2021 = 36327

TOYOTA_RAV4_PHEV_IL_2021 = ProofRequest(
    key="toyota-rav4-phev-il-2021",
    objective=("Establish what the pinned Israeli vehicle knowledge catalog, the Israeli "
               "Ministry of Transport vehicle-model register and the official Toyota "
               "Israel archived-model page each state about the Toyota RAV4 plug-in "
               "hybrid offered in Israel."),
    tasks=(
        ProofTask(
            task_id="yeda_variant",
            goal=("Read the Toyota RAV4 plug-in hybrid variant from the pinned Israeli "
                  "vehicle knowledge catalog at its exact commit."),
            scope="One catalog variant, identified conservatively. Read-only.",
            calls=(ProofCall(
                call_id="yeda-1", tool=YedaVehicleCatalogTool.name,
                operation="get_model_variant",
                arguments={**_VEHICLE, "model_year": 2021, "fuel_type": "plug_in_hybrid"}),),
            required_fields=("fuel_type",),
        ),
        ProofTask(
            task_id="government_record",
            goal=("Read the homologated model-year 2021 Toyota RAV4 plug-in hybrid record "
                  "from the pinned Israeli Ministry of Transport vehicle-model register."),
            scope="One registry record, identified conservatively. Read-only.",
            calls=(ProofCall(
                call_id="gov-1", tool=GovernmentVehicleRegistryTool.name,
                operation="get_model_record",
                arguments={**_VEHICLE, "resource_id": WLTP_RESOURCE_ID, "model_year": 2021,
                           "fuel_type": "plug_in_hybrid", "propulsion_technology": "plug_in",
                           "drivetrain": "awd",
                           "expected_record_id": GOVERNMENT_RECORD_ID_2021}),),
            required_fields=("engine_displacement_cc",),
        ),
        ProofTask(
            task_id="web_archived_status",
            goal=("Read the official Toyota Israel archived-model page for the RAV4 "
                  "Plug-in: model identity and marketing status only."),
            scope=("One saved official page. Identity and archived status only -- never a "
                   "technical specification. Read-only."),
            calls=(ProofCall(
                call_id="web-1", tool=ToyotaArchivedModelDocumentTool.name,
                operation="read_archived_model_document",
                arguments={"document_id": "toyota_il_rav4_phev", "make": "Toyota",
                           # Toyota Israel's OWN commercial name for the model.
                           "commercial_model": "RAV4 Plug-in", "market": "IL"}),),
            required_fields=("marketing_status",),
        ),
        ProofTask(
            task_id="government_record_2026",
            goal=("Identify the model-year 2026 Toyota RAV4 plug-in hybrid registry record "
                  "using only the dimensions the catalog itself states."),
            scope=("The same conservative identity as the catalog variant -- make, "
                   "commercial model, market, model year, fuel, propulsion, drivetrain -- "
                   "and deliberately NO trim and NO model code, because the catalog states "
                   "neither. Read-only."),
            calls=(ProofCall(
                call_id="gov-2", tool=GovernmentVehicleRegistryTool.name,
                operation="get_model_record",
                arguments={**_VEHICLE, "resource_id": WLTP_RESOURCE_ID, "model_year": 2026,
                           "fuel_type": "plug_in_hybrid", "propulsion_technology": "plug_in",
                           "drivetrain": "awd"}),),
            required_fields=("engine_displacement_cc",),
            # The one task allowed not to resolve. Two committed registry rows
            # answer this identity for 2026 and differ only by trim, which the
            # catalog does not state, so there is no conservative answer and
            # the register is asked to refuse rather than choose. Its refusal
            # is the run's real unresolved item, and it reaches `needs_review`
            # through the ordinary task-failure path.
            allow_partial=True,
        ),
    ),
)

#: The closed table. A request key not named here has no plan at all.
PROOF_REQUESTS: Mapping[str, ProofRequest] = {
    TOYOTA_RAV4_PHEV_IL_2021.key: TOYOTA_RAV4_PHEV_IL_2021,
}


class UnknownProofRequest(ValueError):
    """A request key the closed proof table does not name."""


class DeterministicProofCommanderClient:
    """A `CommanderClient` that compiles a plan instead of generating one.

    It satisfies the same protocol `ModelGateway` does, so `Commander`,
    `PlanValidator` and the engine are used exactly as they are in production
    -- the only difference is that the candidate plan came from a static table
    rather than from a completion. Every call is counted, so a test can assert
    the whole run asked this compiler for exactly the decisions it expected.
    """

    def __init__(self, request_key: str):
        request = PROOF_REQUESTS.get(str(request_key))
        if request is None:
            raise UnknownProofRequest("no such proof request")
        self._request = request
        self.plan_calls: list[str] = []
        self.replan_calls: list[Mapping[str, Any]] = []

    @property
    def request(self) -> ProofRequest:
        return self._request

    def create_plan(self, *, model: str, objective: str, context: Mapping[str, Any],
                    repair_reason: str | None = None) -> dict[str, Any]:
        """Compile the ONE plan this client was constructed for.

        `objective` and `context` are recorded for assertions and deliberately
        never read: they are the client-influenced half of a run, and letting
        either steer tool selection or arguments would hand plan authority to
        untrusted input. There is likewise no repair path -- a compiled plan
        that failed the firewall is a server defect, not something to retry.
        """
        self.plan_calls.append(str(objective))
        if repair_reason is not None:
            raise UnknownProofRequest("a compiled plan is never repaired")
        return self._request.as_plan()

    def create_replan(self, *, model: str, objective: str,
                      summary: Mapping[str, Any]) -> dict[str, Any]:
        """Always the same terminal decision: verify what was gathered.

        The compiler adds no tasks and revises none, so it can only ever
        return a terminal decision. It never asks for more research, which is
        why the proof's result is exactly what the pinned sources support.
        """
        self.replan_calls.append(dict(summary))
        return {"decision": "REQUEST_VERIFICATION", "plan": None,
                "reason": "the compiled proof plan is complete; verify the acquired evidence"}


__all__ = ["GOVERNMENT_RECORD_ID_2021", "PROOF_OUTPUT_SCHEMA", "PROOF_REQUESTS",
           "TOYOTA_RAV4_PHEV_IL_2021",
           "DeterministicProofCommanderClient", "ProofCall", "ProofRequest", "ProofTask",
           "UnknownProofRequest"]

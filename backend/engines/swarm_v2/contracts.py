"""Strict, provider-neutral contracts for Commander plans."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .evidence_bounds import (IDENTITY_DIMENSIONS, MAX_IDENTITY_DIMENSION_CHARS,
                              MAX_LOCATOR_KEY_CHARS, MAX_SOURCE_VERSION_KEY_CHARS,
                              MAX_UNIT_CHARS, MAX_VERIFIER_CONTRACT_VERSION_CHARS)
from .fragments import MAX_FRAGMENTS_PER_SOURCE
from .tool_calls import MAX_BINDING_PATH_SEGMENTS, MAX_DEPENDENCY_BINDINGS_PER_CALL


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class DependencyBinding(StrictContract):
    """One argument taken from one declared dependency's output.

    `path` is a bounded literal key/index path, never a query expression:
    see .tool_calls.validate_binding_path for the deterministic bound that
    both the plan firewall and the trusted resolver apply to it.
    """

    argument: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    task_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    path: list[str | int] = Field(min_length=1, max_length=MAX_BINDING_PATH_SEGMENTS)


class PlannedToolCall(StrictContract):
    """ONE exact tool call: the single executable tool representation.

    This replaced the ambiguous `ToolRequirement`, which named a tool and a
    free-text scope, promised `max_calls` invocations the worker never made,
    and left the worker to invent a `{"query": task.goal}` payload that no
    structured operation could accept. Here the plan states the operation and
    the arguments outright, the firewall checks them against the registered
    operation schema, and the worker executes exactly what was approved.

    The number of entries in a task's list is therefore both the promise and
    the charge: `len(task.tools)` is what tool-call budgeting counts.
    """

    call_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_.-]*$")
    operation: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    arguments: dict[str, Any] = Field(default_factory=dict)
    dependency_bindings: list[DependencyBinding] = Field(
        default_factory=list, max_length=MAX_DEPENDENCY_BINDINGS_PER_CALL)


class EvidenceRequirement(StrictContract):
    minimum_sources: int = Field(ge=0, le=100)
    required_fields: list[str] = Field(default_factory=list, max_length=100)
    min_confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("required_fields")
    @classmethod
    def unique_required_fields(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value) or len(set(value)) != len(value):
            raise ValueError("required_fields must be non-empty and unique")
        return value


class CompletionCriteria(StrictContract):
    required_outputs: list[str] = Field(min_length=1, max_length=100)
    evidence_satisfied: bool
    allow_partial: bool = False

    @field_validator("required_outputs")
    @classmethod
    def unique_outputs(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value) or len(set(value)) != len(value):
            raise ValueError("required_outputs must be non-empty and unique")
        return value


class DynamicTask(StrictContract):
    task_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    goal: str = Field(min_length=1, max_length=2000)
    scope: str = Field(min_length=1, max_length=1000)
    dependencies: list[str] = Field(default_factory=list, max_length=100)
    tools: list[PlannedToolCall] = Field(default_factory=list, max_length=50)
    output_schema: dict[str, Any]
    evidence: EvidenceRequirement
    priority: int = Field(ge=0, le=100)
    recursion_depth: int = Field(ge=0)
    estimated_cost_units: int = Field(ge=0)
    completion: CompletionCriteria

    @field_validator("dependencies")
    @classmethod
    def unique_dependencies(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("dependencies must be unique")
        return value

    @field_validator("output_schema")
    @classmethod
    def structured_object_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        if value.get("type") != "object" or not isinstance(value.get("properties"), dict):
            raise ValueError("output_schema must define a JSON object with properties")
        if value.get("additionalProperties") is not False:
            raise ValueError("output_schema must set additionalProperties=false")
        required = value.get("required")
        if not isinstance(required, list) or not required:
            raise ValueError("output_schema must define non-empty required fields")
        if any(not isinstance(item, str) for item in required) or not set(required) <= set(value["properties"]):
            raise ValueError("output_schema required fields must exist in properties")
        return value


class TaskGraph(StrictContract):
    tasks: list[DynamicTask] = Field(min_length=1)


class WorkerAssignment(StrictContract):
    task_id: str = Field(min_length=1, max_length=80)
    worker_role: str = Field(min_length=1, max_length=200)
    context_task_ids: list[str] = Field(default_factory=list, max_length=100)


class CommanderPlan(StrictContract):
    version: Literal["1"]
    objective: str = Field(min_length=1, max_length=4000)
    graph: TaskGraph
    assignments: list[WorkerAssignment] = Field(min_length=1)
    max_replans: int = Field(ge=0)
    estimated_cost_units: int = Field(ge=0)

    @model_validator(mode="after")
    def assignment_identity(self) -> "CommanderPlan":
        task_id_list = [task.task_id for task in self.graph.tasks]
        if len(set(task_id_list)) != len(task_id_list):
            raise ValueError("duplicate task id")
        task_ids = set(task_id_list)
        assigned = [assignment.task_id for assignment in self.assignments]
        if len(set(assigned)) != len(assigned):
            raise ValueError("worker assignments must be unique")
        if set(assigned) != task_ids:
            raise ValueError("every task must have exactly one worker assignment")
        for assignment in self.assignments:
            if not set(assignment.context_task_ids) <= task_ids:
                raise ValueError("assignment context references an unknown task")
        return self


class CommanderDecision(StrictContract):
    """A replan is inert data until its replacement plan is validated."""

    decision: Literal["ADD_TASKS", "REVISE_TASK", "REQUEST_VERIFICATION", "FINISH"]
    plan: CommanderPlan | None = None
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def plan_matches_decision(self) -> "CommanderDecision":
        needs_plan = self.decision in {"ADD_TASKS", "REVISE_TASK"}
        if needs_plan != (self.plan is not None):
            raise ValueError("ADD_TASKS/REVISE_TASK require a plan and terminal decisions forbid one")
        return self


def commander_plan_json_schema() -> dict[str, Any]:
    """Return the authoritative provider-visible CommanderPlan contract."""
    return CommanderPlan.model_json_schema()


def commander_decision_json_schema() -> dict[str, Any]:
    """Return the decision contract with model-validator rules made explicit."""
    schema = CommanderDecision.model_json_schema()
    schema["allOf"] = [
        *schema.get("allOf", []),
        {
            "if": {
                "properties": {
                    "decision": {"enum": ["ADD_TASKS", "REVISE_TASK"]}
                },
                "required": ["decision"],
            },
            "then": {
                "properties": {"plan": {"not": {"type": "null"}}},
                "required": ["plan"],
            },
            "else": {
                "properties": {"plan": {"type": "null"}},
                "required": ["plan"],
            },
        },
    ]
    return schema


class EvidenceReference(StrictContract):
    """One durable claim, as the internal grounding/verification layer sees it.

    `unit`, `locator` and `source_version` are the R3 provenance additions.
    They are optional on purpose: a reference rebuilt from a pre-R3 claim, or
    restored from a checkpoint written before R3, simply carries None and
    behaves exactly as it did before.  R3 carries a unit; it never converts or
    compares one (that is R4).
    """

    claim_id: str = Field(min_length=1, max_length=200)
    source_id: str = Field(min_length=1, max_length=200)
    run_id: str = Field(min_length=1, max_length=200)
    task_id: str = Field(min_length=1, max_length=80)
    entity: str = Field(default="general", min_length=1, max_length=200)
    field: str = Field(min_length=1, max_length=200)
    geography: str | None = Field(default=None, max_length=200)
    market: str | None = Field(default=None, max_length=200)
    time_scope: dict[str, Any] = Field(default_factory=dict)
    value: Any
    unit: str | None = Field(default=None, max_length=MAX_UNIT_CHARS)
    locator: str | None = Field(default=None, max_length=MAX_LOCATOR_KEY_CHARS)
    source_version: str | None = Field(default=None, max_length=MAX_SOURCE_VERSION_KEY_CHARS)
    # R4: the closed identity dimensions the record stated (generation,
    # engine, transmission, official/model code, ...).  Empty means the
    # evidence simply did not qualify itself -- never that the qualification
    # does not matter -- so a claim with an empty identity can only ever be
    # compared to a fact with an empty identity.
    identity: dict[str, str] = Field(default_factory=dict)
    confidence: float = Field(ge=0, le=1)
    supported: bool = True

    @field_validator("identity")
    @classmethod
    def closed_identity(cls, value: dict[str, str]) -> dict[str, str]:
        if not set(value) <= set(IDENTITY_DIMENSIONS):
            raise ValueError("identity dimensions must come from the closed vocabulary")
        if any(not item.strip() or len(item) > MAX_IDENTITY_DIMENSION_CHARS
               for item in value.values()):
            raise ValueError("identity dimension values must be bounded and non-empty")
        return value


class SupportLink(StrictContract):
    """R4: ONE durable evidence row a stored verdict rests on.

    A POINTER to durable evidence, never a copy of it: there is no text field
    here, so no quoted source material, prompt, provider payload or
    explanation can ride out of the verifier on a stored verdict.
    `fragment_id` is the durable row id when the resolver could supply one --
    an offline resolver that never read a database legitimately cannot -- and
    `content_hash` is always present, so a verdict stays replayable either
    way.  It lives in this module, next to the verdict that carries it,
    so .support can hold the validation rules without an import cycle.
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    source_id: str = Field(min_length=1, max_length=200)
    content_hash: str = Field(min_length=64, max_length=64,
                              pattern=r"^[0-9a-f]{64}$")
    fragment_id: str | None = Field(default=None, min_length=1, max_length=200,
                                    pattern=r"^[A-Za-z0-9_:.@-]+$")
    locator: str | None = Field(default=None, min_length=1,
                                max_length=MAX_LOCATOR_KEY_CHARS)

    @property
    def identity(self) -> tuple[str, str, str | None]:
        """What makes this link THIS link: source, evidence and its place."""
        return (self.source_id, self.content_hash, self.locator)


class VerificationVerdict(StrictContract):
    """One durable verdict, and (R4) the record of how it was reached.

    `mode`, `contract_version` and `support` are optional on purpose: a
    verdict restored from a checkpoint written before R4 simply carries None
    and an empty support list, which is exactly what a reader needs to know --
    it is readable, and it is NOT a fully R4-grounded verdict.  Nothing here
    is model-authored: the verdict comes from a closed vocabulary, the reason
    from a backend-owned allowlist, and the support links from durable
    evidence the backend itself resolved.
    """

    claim_id: str = Field(min_length=1, max_length=200)
    verdict: Literal["verified", "needs_review", "rejected"]
    reason: str = Field(min_length=1, max_length=500)
    mode: Literal["deterministic_local", "deterministic_structured",
                  "grounded_model"] | None = None
    contract_version: str | None = Field(default=None, min_length=1,
                                         max_length=MAX_VERIFIER_CONTRACT_VERSION_CHARS)
    support: list[SupportLink] = Field(default_factory=list,
                                       max_length=MAX_FRAGMENTS_PER_SOURCE)

    @model_validator(mode="after")
    def support_requires_mode(self) -> "VerificationVerdict":
        # Support links are evidence provenance, so they may only exist on a
        # verdict that says which contract and which mechanism produced them.
        if self.support and (self.mode is None or self.contract_version is None):
            raise ValueError("support links require a verification mode and contract version")
        if self.mode == "deterministic_local" and self.support:
            raise ValueError("a locally settled verdict cites no evidence")
        identities = [link.identity for link in self.support]
        if len(set(identities)) != len(identities):
            raise ValueError("support links must be unique")
        if len({link.source_id for link in self.support}) > 1:
            raise ValueError("support links must all belong to one source")
        return self

    @property
    def is_r4_grounded(self) -> bool:
        """Whether this verdict carries complete R4 verification provenance.

        A legacy verdict stays perfectly readable and keeps its meaning; it
        simply answers False here, so nothing can present it as a decision of
        the current contract.
        """
        if self.mode is None or self.contract_version is None:
            return False
        return bool(self.support) or self.verdict != "verified"


class RemainingBudget(StrictContract):
    """Independent remaining capacities; cost units are not model-call slots."""

    cost_units: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    tasks: int = Field(ge=0)
    model_calls: int = Field(default=1_000, ge=0)
    # R4: the remaining SEMANTIC retry allowance (BudgetTracker.max_retries
    # minus the retries already recorded).  A bounded correction round is
    # refused when it is exhausted.  The permissive default keeps every
    # existing caller -- and every checkpoint written before R4 -- behaving
    # exactly as it did: a deployment that sets no retry limit is not
    # retroactively given one.
    retries: int = Field(default=1_000, ge=0)

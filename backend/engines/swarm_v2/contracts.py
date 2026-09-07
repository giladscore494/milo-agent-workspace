"""Strict, provider-neutral contracts for Commander plans."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .evidence_bounds import (MAX_LOCATOR_KEY_CHARS, MAX_SOURCE_VERSION_KEY_CHARS,
                              MAX_UNIT_CHARS)
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
    confidence: float = Field(ge=0, le=1)
    supported: bool = True


class VerificationVerdict(StrictContract):
    claim_id: str = Field(min_length=1, max_length=200)
    verdict: Literal["verified", "needs_review", "rejected"]
    reason: str = Field(min_length=1, max_length=500)


class RemainingBudget(StrictContract):
    """Independent remaining capacities; cost units are not model-call slots."""

    cost_units: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    tasks: int = Field(ge=0)
    model_calls: int = Field(default=1_000, ge=0)

"""Strict, provider-neutral contracts for Commander plans."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ToolRequirement(StrictContract):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_.-]*$")
    scope: str = Field(min_length=1, max_length=200)
    max_calls: int = Field(ge=1, le=100)


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
    tools: list[ToolRequirement] = Field(default_factory=list, max_length=50)
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

    @field_validator("tools")
    @classmethod
    def unique_tools(cls, value: list[ToolRequirement]) -> list[ToolRequirement]:
        if len({tool.name for tool in value}) != len(value):
            raise ValueError("tools must be unique per task")
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

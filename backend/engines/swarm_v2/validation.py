"""Deterministic firewall between Commander JSON and later execution stages."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Collection

from pydantic import ValidationError

from .contracts import CommanderPlan, DynamicTask


class PlanValidationError(ValueError):
    """A plan is malformed or exceeds a configured safety boundary."""


class PlanJsonError(PlanValidationError):
    """The Commander response is not a JSON object."""


class PlanSchemaError(PlanValidationError):
    """The decoded object does not satisfy the Commander contract."""


class PlanLimitError(PlanValidationError):
    """A valid Commander contract exceeds a deterministic safety limit."""


@dataclass(frozen=True)
class PlanLimits:
    max_tasks: int = 64
    max_graph_depth: int = 12
    max_recursion_depth: int = 4
    max_replans: int = 3
    max_cost_units: int = 100_000
    max_tool_calls: int = 100

    def __post_init__(self) -> None:
        if any(value < 0 for value in self.__dict__.values()):
            raise ValueError("plan limits cannot be negative")


class PlanValidator:
    def __init__(self, *, allowed_tools: Collection[str], limits: PlanLimits | None = None):
        self._allowed_tools = frozenset(allowed_tools)
        self._limits = limits or PlanLimits()

    def validate(self, candidate: str | bytes | dict[str, Any]) -> CommanderPlan:
        """Return an executable contract only after all checks succeed."""
        decoded: Any = candidate
        if isinstance(candidate, (str, bytes)):
            try:
                decoded = json.loads(candidate)
            except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
                raise PlanJsonError("Commander plan is not valid JSON") from None
        if not isinstance(decoded, dict):
            raise PlanSchemaError("Commander plan must be a JSON object")
        try:
            plan = CommanderPlan.model_validate(decoded)
        except (ValidationError, ValueError, TypeError) as exc:
            # Detailed validation diagnostics remain inside this deterministic
            # boundary for callers/tests. Commander replaces them with a safe
            # stable code before the worker can persist or emit the failure.
            raise PlanSchemaError(f"invalid Commander plan: {exc}") from exc
        self._validate_plan(plan)
        return plan

    def _validate_plan(self, plan: CommanderPlan) -> None:
        limits = self._limits
        tasks = plan.graph.tasks
        if len(tasks) > limits.max_tasks:
            raise PlanLimitError("task count limit exceeded")
        if plan.max_replans > limits.max_replans:
            raise PlanLimitError("replan/recursion limit exceeded")
        if plan.estimated_cost_units > limits.max_cost_units:
            raise PlanLimitError("plan cost limit exceeded")
        calculated_cost = sum(task.estimated_cost_units for task in tasks)
        if calculated_cost > limits.max_cost_units or calculated_cost > plan.estimated_cost_units:
            raise PlanLimitError("task cost exceeds the declared or configured budget")

        by_id: dict[str, DynamicTask] = {}
        signatures: set[str] = set()
        total_tool_calls = 0
        for task in tasks:
            if task.task_id in by_id:
                raise PlanValidationError(f"duplicate task id: {task.task_id}")
            by_id[task.task_id] = task
            signature = json.dumps({
                "goal": " ".join(task.goal.casefold().split()),
                "scope": " ".join(task.scope.casefold().split()),
                "tools": sorted(tool.name for tool in task.tools),
                "output": task.output_schema,
            }, sort_keys=True, separators=(",", ":"))
            if signature in signatures:
                raise PlanValidationError("duplicate task signature")
            signatures.add(signature)
            if task.recursion_depth > limits.max_recursion_depth:
                raise PlanLimitError("task recursion limit exceeded")
            if (not task.completion.evidence_satisfied and
                    (task.evidence.minimum_sources or task.evidence.required_fields)):
                raise PlanValidationError(
                    "evidence requirements cannot be disabled by completion criteria")
            for tool in task.tools:
                if tool.name not in self._allowed_tools:
                    raise PlanValidationError(f"tool is not allowlisted: {tool.name}")
                total_tool_calls += tool.max_calls
        if total_tool_calls > limits.max_tool_calls:
            raise PlanLimitError("aggregate tool call limit exceeded")

        ids = set(by_id)
        for task in tasks:
            missing = set(task.dependencies) - ids
            if missing:
                raise PlanValidationError(f"missing dependencies for {task.task_id}: {sorted(missing)}")
            if task.task_id in task.dependencies:
                raise PlanValidationError(f"dependency cycle at {task.task_id}")

        visiting: set[str] = set()
        depths: dict[str, int] = {}
        def depth(task_id: str) -> int:
            if task_id in visiting:
                raise PlanValidationError("dependency cycle detected")
            if task_id in depths:
                return depths[task_id]
            visiting.add(task_id)
            value = 1 + max((depth(dep) for dep in by_id[task_id].dependencies), default=0)
            visiting.remove(task_id)
            depths[task_id] = value
            return value
        if max(depth(task_id) for task_id in by_id) > limits.max_graph_depth:
            raise PlanLimitError("graph depth limit exceeded")

        for assignment in plan.assignments:
            dependencies = self._dependency_closure(assignment.task_id, by_id)
            if not dependencies <= set(assignment.context_task_ids):
                raise PlanValidationError(f"assignment for {assignment.task_id} lacks dependency closure")

    @staticmethod
    def _dependency_closure(task_id: str, tasks: dict[str, DynamicTask]) -> set[str]:
        result: set[str] = set()
        pending = list(tasks[task_id].dependencies)
        while pending:
            dependency = pending.pop()
            if dependency not in result:
                result.add(dependency)
                pending.extend(tasks[dependency].dependencies)
        return result

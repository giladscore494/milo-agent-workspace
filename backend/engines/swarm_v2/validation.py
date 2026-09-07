"""Deterministic firewall between Commander JSON and later execution stages."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Collection

from pydantic import ValidationError

from backend.tools import ToolDescriptor
from backend.tools.registry import validate_json_schema

from .contracts import CommanderPlan, DynamicTask, PlannedToolCall
from .tool_calls import (MAX_TOOL_ARGUMENT_KEYS, MAX_TOOL_CALLS_PER_TASK,
                         MAX_TOOL_INPUT_JSON_BYTES, PLAN_TOOL_CALL_REASONS,
                         ToolCallError, check_material, validate_binding_path)

# The ONLY strings allowed to describe WHY a plan was rejected on any
# durable or provider-visible surface. Static and code-owned: never model
# output, user text, field values or raw validation diagnostics.
VALIDATION_REASONS = frozenset({
    "JSON_DECODE_FAILED",
    "SCHEMA_MISSING_FIELD",
    "SCHEMA_EXTRA_FIELD",
    "SCHEMA_TYPE_MISMATCH",
    "SCHEMA_CONSTRAINT_FAILED",
    "DUPLICATE_TASK_ID",
    "DUPLICATE_TASK_SIGNATURE",
    "MISSING_DEPENDENCY",
    "DEPENDENCY_CYCLE",
    "ASSIGNMENT_CONTEXT_INCOMPLETE",
    "EVIDENCE_COMPLETION_MISMATCH",
    "TOOL_NOT_ALLOWLISTED",
    "TOOL_OPERATION_UNKNOWN",
    "TASK_COUNT_LIMIT",
    "GRAPH_DEPTH_LIMIT",
    "RECURSION_LIMIT",
    "REPLAN_LIMIT",
    "COST_LIMIT",
    "TASK_TOOL_CALL_LIMIT",
    "AGGREGATE_TOOL_CALL_LIMIT",
    # Planned-call rejections share ONE allowlist with the trusted resolver
    # (see .tool_calls), so the firewall and the worker can never disagree
    # about what a rejection is called.
    *PLAN_TOOL_CALL_REASONS,
})


class PlanValidationError(ValueError):
    """A plan is malformed or exceeds a configured safety boundary."""

    def __init__(self, message: str, *, reason: str = "SCHEMA_CONSTRAINT_FAILED"):
        if reason not in VALIDATION_REASONS:
            raise ValueError("validation reason must come from the static allowlist")
        super().__init__(message)
        self.reason = reason


class PlanJsonError(PlanValidationError):
    """The Commander response is not a JSON object."""

    def __init__(self, message: str, *, reason: str = "JSON_DECODE_FAILED"):
        super().__init__(message, reason=reason)


class PlanSchemaError(PlanValidationError):
    """The decoded object does not satisfy the Commander contract."""


class PlanLimitError(PlanValidationError):
    """A valid Commander contract exceeds a deterministic safety limit."""


def classify_schema_failure(exc: Exception) -> str:
    """Map a contract failure to a static safe reason using error TYPE only.

    Pydantic messages, locations and input values can embed model output;
    only the bounded error-type slug participates, and the returned code is
    always one of the fixed VALIDATION_REASONS."""
    if not isinstance(exc, ValidationError):
        return "SCHEMA_CONSTRAINT_FAILED"
    try:
        kinds = {str(error.get("type", "")) for error in exc.errors()}
    except Exception:
        return "SCHEMA_CONSTRAINT_FAILED"
    if "missing" in kinds:
        return "SCHEMA_MISSING_FIELD"
    if "extra_forbidden" in kinds:
        return "SCHEMA_EXTRA_FIELD"
    if any(kind.endswith("_type") for kind in kinds):
        return "SCHEMA_TYPE_MISMATCH"
    return "SCHEMA_CONSTRAINT_FAILED"


@dataclass(frozen=True)
class PlanLimits:
    max_tasks: int = 64
    max_graph_depth: int = 12
    max_recursion_depth: int = 4
    max_replans: int = 3
    max_cost_units: int = 100_000
    # Two independent bounds, both charged against the EXACT planned call
    # list: a low fixed ceiling per task, and the existing aggregate run
    # ceiling. Neither is a dynamic allowance a plan can grow into.
    max_tool_calls_per_task: int = MAX_TOOL_CALLS_PER_TASK
    max_tool_calls: int = 100

    def __post_init__(self) -> None:
        if any(value < 0 for value in self.__dict__.values()):
            raise ValueError("plan limits cannot be negative")


# Semantic firewall rules that CommanderPlan.model_json_schema() cannot
# express. Static, code-owned text: no model output, no taxonomies.
PROVIDER_PLAN_RULES = (
    "Every task_id must be unique.",
    "No two tasks may share the same normalized goal, scope, planned tool calls and output_schema (duplicate task signatures are rejected).",
    "Every dependency must reference an existing task_id and the dependency graph must contain no cycles.",
    "Every task must have exactly one entry in assignments.",
    "Each assignment's context_task_ids must contain the COMPLETE direct and transitive dependency closure of its task.",
    "If a task declares evidence.minimum_sources > 0 or any evidence.required_fields, its completion.evidence_satisfied must be true; completion criteria can never disable evidence requirements.",
    "Every output_schema must be a JSON object schema with properties, a non-empty required list of existing properties, and additionalProperties=false.",
    "Every entry in a task's tools list is ONE exact tool call: it must name a tool from allowed_tools and an operation listed for that tool in the tool catalog; when allowed_tools is empty every task must use tools: [].",
    "Each call_id must be unique within its task, and the call's literal arguments must satisfy the selected operation's input schema.",
    "A dependency_bindings entry may read only a task listed in the SAME task's dependencies, using a literal key/index path; wildcards, filters and expressions are rejected.",
    "A dependency binding must not target an argument already given as a literal, and no two bindings may target the same argument.",
    "Every required property of the selected operation's input schema must be supplied by a literal argument or by exactly one dependency binding.",
    "The number of tasks must not exceed limits.max_tasks.",
    "The dependency graph depth must not exceed limits.max_graph_depth.",
    "No task recursion_depth may exceed limits.max_recursion_depth.",
    "The plan max_replans must not exceed limits.max_replans.",
    "plan.estimated_cost_units must not exceed limits.max_cost_units.",
    "The sum of all task estimated_cost_units must not exceed plan.estimated_cost_units or limits.max_cost_units.",
    "No task may declare more than limits.max_tool_calls_per_task tool calls.",
    "The total number of planned tool calls across every task must not exceed limits.max_tool_calls.",
)


def provider_plan_policy(limits: PlanLimits,
                         allowed_tools: Collection[str]) -> dict[str, Any]:
    """Deterministic provider-visible policy derived from the SAME PlanLimits
    the deterministic firewall enforces, so contract and firewall cannot
    drift independently. Contains only server-owned limits, the server tool
    allowlist and static rule text."""
    return {
        "limits": {
            "max_tasks": limits.max_tasks,
            "max_graph_depth": limits.max_graph_depth,
            "max_recursion_depth": limits.max_recursion_depth,
            "max_replans": limits.max_replans,
            "max_cost_units": limits.max_cost_units,
            "max_tool_calls_per_task": limits.max_tool_calls_per_task,
            "max_tool_calls": limits.max_tool_calls,
        },
        "allowed_tools": sorted(set(allowed_tools)),
        "rules": list(PROVIDER_PLAN_RULES),
    }


class PlanValidator:
    """The deterministic firewall. Commander output is DATA until it passes.

    A plan may REQUEST a registered capability; it can never grant one. No
    scope, write approval, credential or capability is read from a plan: the
    only tool authority is the server-owned descriptor set injected here and
    the server-owned ToolContext used at execution time.
    """

    def __init__(self, *, allowed_tools: Collection[ToolDescriptor],
                 limits: PlanLimits | None = None):
        # Descriptors, not bare names: an operation and its input schema are
        # part of what makes a planned call valid, so a name-only allowlist
        # could never fail closed on an unknown operation or a bad argument
        # and is refused outright.
        if any(not isinstance(item, ToolDescriptor) for item in allowed_tools):
            raise TypeError("allowed_tools must be registered ToolDescriptor values")
        self._tools = {descriptor.name: descriptor for descriptor in allowed_tools}
        self._limits = limits or PlanLimits()

    @property
    def limits(self) -> PlanLimits:
        return self._limits

    def validate(self, candidate: str | bytes | dict[str, Any]) -> CommanderPlan:
        """Return an executable contract only after all checks succeed."""
        decoded: Any = candidate
        if isinstance(candidate, (str, bytes)):
            try:
                decoded = json.loads(candidate)
            except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
                raise PlanJsonError("Commander plan is not valid JSON") from None
        if not isinstance(decoded, dict):
            raise PlanSchemaError("Commander plan must be a JSON object",
                                  reason="SCHEMA_TYPE_MISMATCH")
        try:
            plan = CommanderPlan.model_validate(decoded)
        except (ValidationError, ValueError, TypeError) as exc:
            # Detailed validation diagnostics remain inside this deterministic
            # boundary for callers/tests. Commander replaces them with a safe
            # stable code before the worker can persist or emit the failure.
            raise PlanSchemaError(f"invalid Commander plan: {exc}",
                                  reason=classify_schema_failure(exc)) from exc
        self._validate_plan(plan)
        return plan

    def _validate_plan(self, plan: CommanderPlan) -> None:
        limits = self._limits
        tasks = plan.graph.tasks
        if len(tasks) > limits.max_tasks:
            raise PlanLimitError("task count limit exceeded", reason="TASK_COUNT_LIMIT")
        if plan.max_replans > limits.max_replans:
            raise PlanLimitError("replan/recursion limit exceeded", reason="REPLAN_LIMIT")
        if plan.estimated_cost_units > limits.max_cost_units:
            raise PlanLimitError("plan cost limit exceeded", reason="COST_LIMIT")
        calculated_cost = sum(task.estimated_cost_units for task in tasks)
        if calculated_cost > limits.max_cost_units or calculated_cost > plan.estimated_cost_units:
            raise PlanLimitError("task cost exceeds the declared or configured budget",
                                 reason="COST_LIMIT")

        by_id: dict[str, DynamicTask] = {}
        signatures: set[str] = set()
        total_tool_calls = 0
        for task in tasks:
            if task.task_id in by_id:
                raise PlanValidationError(f"duplicate task id: {task.task_id}",
                                          reason="DUPLICATE_TASK_ID")
            by_id[task.task_id] = task
            signature = json.dumps({
                "goal": " ".join(task.goal.casefold().split()),
                "scope": " ".join(task.scope.casefold().split()),
                "tools": [call.model_dump(mode="json") for call in task.tools],
                "output": task.output_schema,
            }, sort_keys=True, separators=(",", ":"))
            if signature in signatures:
                raise PlanValidationError("duplicate task signature",
                                          reason="DUPLICATE_TASK_SIGNATURE")
            signatures.add(signature)
            if task.recursion_depth > limits.max_recursion_depth:
                raise PlanLimitError("task recursion limit exceeded", reason="RECURSION_LIMIT")
            if (not task.completion.evidence_satisfied and
                    (task.evidence.minimum_sources or task.evidence.required_fields)):
                raise PlanValidationError(
                    "evidence requirements cannot be disabled by completion criteria",
                    reason="EVIDENCE_COMPLETION_MISMATCH")
            # The EXACT call list is what is charged: the number of planned
            # calls is the number the worker executes, so a promise that
            # disagrees with execution is no longer representable.
            if len(task.tools) > limits.max_tool_calls_per_task:
                raise PlanLimitError("task tool call limit exceeded",
                                     reason="TASK_TOOL_CALL_LIMIT")
            total_tool_calls += len(task.tools)
            self._validate_tool_calls(task)
        if total_tool_calls > limits.max_tool_calls:
            raise PlanLimitError("aggregate tool call limit exceeded",
                                 reason="AGGREGATE_TOOL_CALL_LIMIT")

        ids = set(by_id)
        for task in tasks:
            missing = set(task.dependencies) - ids
            if missing:
                raise PlanValidationError(f"missing dependencies for {task.task_id}: {sorted(missing)}",
                                          reason="MISSING_DEPENDENCY")
            if task.task_id in task.dependencies:
                raise PlanValidationError(f"dependency cycle at {task.task_id}",
                                          reason="DEPENDENCY_CYCLE")

        visiting: set[str] = set()
        depths: dict[str, int] = {}
        def depth(task_id: str) -> int:
            if task_id in visiting:
                raise PlanValidationError("dependency cycle detected",
                                          reason="DEPENDENCY_CYCLE")
            if task_id in depths:
                return depths[task_id]
            visiting.add(task_id)
            value = 1 + max((depth(dep) for dep in by_id[task_id].dependencies), default=0)
            visiting.remove(task_id)
            depths[task_id] = value
            return value
        if max(depth(task_id) for task_id in by_id) > limits.max_graph_depth:
            raise PlanLimitError("graph depth limit exceeded", reason="GRAPH_DEPTH_LIMIT")

        for assignment in plan.assignments:
            dependencies = self._dependency_closure(assignment.task_id, by_id)
            if not dependencies <= set(assignment.context_task_ids):
                raise PlanValidationError(f"assignment for {assignment.task_id} lacks dependency closure",
                                          reason="ASSIGNMENT_CONTEXT_INCOMPLETE")

    def _validate_tool_calls(self, task: DynamicTask) -> None:
        """Reject every planned call a later stage could not execute safely.

        Everything decidable without the real dependency outputs is decided
        HERE, so a run never pays for a model call, a tool call or a durable
        write on a plan that was already unexecutable.
        """
        seen_call_ids: set[str] = set()
        for call in task.tools:
            if call.call_id in seen_call_ids:
                raise PlanValidationError(f"duplicate tool call id: {call.call_id}",
                                          reason="DUPLICATE_TOOL_CALL_ID")
            seen_call_ids.add(call.call_id)
            descriptor = self._tools.get(call.name)
            if descriptor is None:
                raise PlanValidationError(f"tool is not allowlisted: {call.name}",
                                          reason="TOOL_NOT_ALLOWLISTED")
            operation = descriptor.operation(call.operation)
            if operation is None:
                raise PlanValidationError(
                    f"tool operation is not registered: {call.name}.{call.operation}",
                    reason="TOOL_OPERATION_UNKNOWN")
            self._validate_call_arguments(task, call, operation.input_schema)

    def _validate_call_arguments(self, task: DynamicTask, call: PlannedToolCall,
                                 input_schema: Any) -> None:
        properties = input_schema.get("properties") or {}
        if len(call.arguments) > MAX_TOOL_ARGUMENT_KEYS:
            raise PlanLimitError("planned tool arguments exceed the argument bound",
                                 reason="TOOL_ARGUMENTS_TOO_LARGE")
        try:
            check_material(call.arguments, MAX_TOOL_INPUT_JSON_BYTES,
                           "TOOL_ARGUMENTS_TOO_LARGE")
        except ToolCallError as exc:
            raise PlanLimitError("planned tool arguments exceed a deterministic bound",
                                 reason=exc.code) from None

        bound: set[str] = set()
        for binding in call.dependency_bindings:
            if binding.argument in bound:
                raise PlanValidationError("duplicate dependency binding target",
                                          reason="TOOL_BINDING_DUPLICATE")
            bound.add(binding.argument)
            if binding.argument in call.arguments:
                # Merge precedence is explicit: a binding ADDS an argument and
                # may never silently replace a literal the plan already shows.
                raise PlanValidationError("dependency binding overwrites a literal argument",
                                          reason="TOOL_BINDING_CONFLICT")
            # Only DIRECT declared dependencies: transitive context a worker
            # can read is deliberately not readable as a tool argument.
            if binding.task_id not in task.dependencies:
                raise PlanValidationError("dependency binding target is not a direct dependency",
                                          reason="TOOL_BINDING_UNKNOWN_DEPENDENCY")
            try:
                validate_binding_path(binding.path)
            except ToolCallError as exc:
                raise PlanValidationError("dependency binding path is not bounded and literal",
                                          reason=exc.code) from None

        supplied = set(call.arguments) | bound
        unknown = supplied - set(properties)
        missing = set(input_schema.get("required") or []) - supplied
        if unknown or missing:
            raise PlanValidationError(
                "planned arguments do not match the operation input schema",
                reason="TOOL_ARGUMENTS_INVALID")
        try:
            # Literals are type-checked now against their own sub-schema; the
            # FULL resolved payload is validated again by the Registry against
            # the authoritative operation schema immediately before execution.
            validate_json_schema({"type": "object", "additionalProperties": False,
                                  "required": [],
                                  "properties": {key: properties[key] for key in call.arguments}},
                                 dict(call.arguments))
        except (TypeError, ValueError):
            raise PlanValidationError("planned literal arguments fail the operation schema",
                                      reason="TOOL_ARGUMENTS_INVALID") from None

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

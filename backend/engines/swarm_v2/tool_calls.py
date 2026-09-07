"""Bounded, deterministic material for exactly-planned tool calls.

Everything here is trusted APPLICATION code. Commander picks which registered
capability to call; this module decides what an argument may look like, how a
dependency value is read, and how much material may exist at all. No path is
evaluated, no expression is interpreted and no value is trusted for its size.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, Sequence

# --- deterministic global bounds --------------------------------------------
# Server-owned constants, not plan data: a plan can never raise them, and every
# one of them is checked BEFORE the value reaches a model prompt or durable
# state. They are separate from PlanLimits, which bounds plan SHAPE; these
# bound plan MATERIAL.
MAX_TOOL_CALLS_PER_TASK = 4
MAX_DEPENDENCY_BINDINGS_PER_CALL = 8
MAX_BINDING_PATH_SEGMENTS = 6
MAX_BINDING_PATH_SEGMENT_CHARS = 64
MAX_BINDING_ARRAY_INDEX = 999
MAX_TOOL_ARGUMENT_KEYS = 32
MAX_TOOL_INPUT_JSON_BYTES = 4_096
MAX_TOOL_OUTPUT_JSON_BYTES = 32_768
MAX_TOOL_MATERIAL_JSON_BYTES = 65_536
MAX_TASK_OUTPUT_JSON_BYTES = 32_768
MAX_TOOL_VALUE_DEPTH = 8
MAX_TOOL_COLLECTION_ITEMS = 200

# A path segment is a literal object key. The pattern admits no wildcard, no
# `$`, no bracket, no quote and no dot-dot, so JSONPath/expression syntax is
# rejected as a MALFORMED KEY rather than being parsed and then refused.
PATH_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")

# Rejections that can be decided while validating a plan.
PLAN_TOOL_CALL_REASONS = frozenset({
    "DUPLICATE_TOOL_CALL_ID",
    "TOOL_ARGUMENTS_INVALID",
    "TOOL_ARGUMENTS_TOO_LARGE",
    "TOOL_BINDING_CONFLICT",
    "TOOL_BINDING_DUPLICATE",
    "TOOL_BINDING_PATH_INVALID",
    "TOOL_BINDING_UNKNOWN_DEPENDENCY",
})
# Rejections only the real dependency outputs can decide.
RUNTIME_TOOL_CALL_REASONS = frozenset({
    "TASK_OUTPUT_TOO_LARGE",
    "TOOL_BINDING_UNRESOLVED",
    "TOOL_MATERIAL_TOO_LARGE",
    "TOOL_OUTPUT_TOO_LARGE",
})
TOOL_CALL_REASONS = PLAN_TOOL_CALL_REASONS | RUNTIME_TOOL_CALL_REASONS


class ToolCallError(ValueError):
    """A tool-call failure carrying ONLY a static, code-owned reason.

    The rejected argument, the dependency value and the oversized payload
    never travel with the classification, so the safe representation is fit
    for a durable task result, a run event and telemetry alike.
    """

    MESSAGES = {
        "DUPLICATE_TOOL_CALL_ID": "planned tool calls must have unique call ids",
        "TASK_OUTPUT_TOO_LARGE": "task output exceeds the deterministic size bound",
        "TOOL_ARGUMENTS_INVALID": "planned tool arguments do not satisfy the operation schema",
        "TOOL_ARGUMENTS_TOO_LARGE": "planned tool arguments exceed the deterministic size bound",
        "TOOL_BINDING_CONFLICT": "a dependency binding may not overwrite a literal argument",
        "TOOL_BINDING_DUPLICATE": "two dependency bindings target the same argument",
        "TOOL_BINDING_PATH_INVALID": "dependency binding path is not a bounded literal key path",
        "TOOL_BINDING_UNKNOWN_DEPENDENCY": "dependency binding references a task that is not a declared dependency",
        "TOOL_BINDING_UNRESOLVED": "dependency binding could not be resolved to a bounded value",
        "TOOL_MATERIAL_TOO_LARGE": "combined tool material exceeds the deterministic size bound",
        "TOOL_OUTPUT_TOO_LARGE": "tool output exceeds the deterministic size bound",
    }

    def __init__(self, code: str):
        if code not in TOOL_CALL_REASONS:
            raise ValueError("tool call reason must come from the static allowlist")
        self.code = code
        self.safe_message = self.MESSAGES[code]
        super().__init__(self.safe_message)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def check_size(value: Any, limit: int, code: str) -> str:
    """Bound a value's serialized size, failing with a static reason.

    The rejected payload is deliberately not returned or logged: only the
    fact that it was too large crosses this boundary.
    """
    try:
        encoded = canonical_json(value)
    except (TypeError, ValueError):
        raise ToolCallError(code) from None
    if len(encoded.encode()) > limit:
        raise ToolCallError(code)
    return encoded


def check_shape(value: Any, code: str, *, depth: int = 0) -> None:
    """Reject excessive nesting or collection size before serializing.

    Serialized size alone is not enough: a deeply nested or wide structure is
    cheap to encode and expensive for everything downstream.
    """
    if depth > MAX_TOOL_VALUE_DEPTH:
        raise ToolCallError(code)
    if isinstance(value, Mapping):
        if len(value) > MAX_TOOL_COLLECTION_ITEMS:
            raise ToolCallError(code)
        for key, item in value.items():
            if not isinstance(key, str):
                raise ToolCallError(code)
            check_shape(item, code, depth=depth + 1)
    elif isinstance(value, (list, tuple)):
        if len(value) > MAX_TOOL_COLLECTION_ITEMS:
            raise ToolCallError(code)
        for item in value:
            check_shape(item, code, depth=depth + 1)
    elif not (value is None or isinstance(value, (bool, int, float, str))):
        raise ToolCallError(code)


def check_material(value: Any, limit: int, code: str) -> None:
    """The single gate every tool-derived value passes: shape, then size."""
    check_shape(value, code)
    check_size(value, limit, code)


def validate_binding_path(path: Sequence[Any]) -> None:
    """Accept only a bounded literal key/index path.

    This is NOT a query language. There is no wildcard, no filter, no slice,
    no recursive descent and no expression: a segment is either a literal
    object key matching PATH_SEGMENT_PATTERN or a small non-negative array
    index. Anything else is rejected without being interpreted.
    """
    if not path or len(path) > MAX_BINDING_PATH_SEGMENTS:
        raise ToolCallError("TOOL_BINDING_PATH_INVALID")
    for segment in path:
        if isinstance(segment, bool):
            raise ToolCallError("TOOL_BINDING_PATH_INVALID")
        if isinstance(segment, int):
            if not 0 <= segment <= MAX_BINDING_ARRAY_INDEX:
                raise ToolCallError("TOOL_BINDING_PATH_INVALID")
            continue
        if not isinstance(segment, str) or len(segment) > MAX_BINDING_PATH_SEGMENT_CHARS:
            raise ToolCallError("TOOL_BINDING_PATH_INVALID")
        if not PATH_SEGMENT_PATTERN.fullmatch(segment):
            raise ToolCallError("TOOL_BINDING_PATH_INVALID")


def read_path(root: Any, path: Sequence[Any]) -> Any:
    """Walk one validated path with plain container access only."""
    current = root
    for segment in path:
        if isinstance(segment, int) and not isinstance(segment, bool):
            if not isinstance(current, list) or not 0 <= segment < len(current):
                raise ToolCallError("TOOL_BINDING_UNRESOLVED")
            current = current[segment]
            continue
        if not isinstance(current, Mapping) or segment not in current:
            raise ToolCallError("TOOL_BINDING_UNRESOLVED")
        current = current[segment]
    return current


def resolve_tool_arguments(call: Any, dependency_outputs: Mapping[str, Any]) -> dict[str, Any]:
    """Build the FINAL payload for one planned call, in trusted code.

    Merge precedence is explicit and one-directional: literal arguments are
    authoritative and a dependency binding may only ADD an argument. A binding
    that would replace a literal is rejected at plan time and refused again
    here, so the plan's visible arguments always equal what the tool receives.

    Only the outputs of tasks the call's own task declared as direct
    dependencies are ever passed in by the executor, and only the exact path a
    binding names is read, so no unrelated task output can leak into a payload.
    """
    arguments = dict(call.arguments)
    for binding in call.dependency_bindings:
        if binding.argument in arguments:
            raise ToolCallError("TOOL_BINDING_CONFLICT")
        if binding.task_id not in dependency_outputs:
            raise ToolCallError("TOOL_BINDING_UNKNOWN_DEPENDENCY")
        validate_binding_path(binding.path)
        value = read_path(dependency_outputs[binding.task_id], binding.path)
        check_material(value, MAX_TOOL_INPUT_JSON_BYTES, "TOOL_ARGUMENTS_TOO_LARGE")
        arguments[binding.argument] = value
    check_material(arguments, MAX_TOOL_INPUT_JSON_BYTES, "TOOL_ARGUMENTS_TOO_LARGE")
    return arguments


@dataclass(frozen=True)
class ToolCallRecord:
    """The trusted post-execution boundary for a validated tool result.

    Construction is reachable only from the worker's tool loop, AFTER the
    Registry has validated the operation, the resolved payload and the result
    against the authoritative registered schemas. Every identifier on it is
    server-resolved from the already-approved plan; none of it is worker-model
    output, and the worker model has no way to reach or forge this type -- it
    only ever sees bounded tool material as prompt text.

    A record is evidence MATERIAL, not a verified fact: it deliberately
    creates no Source, Claim or EvidenceReference. Turning a domain result
    into evidence needs domain-specific mapping and a real evidence grant,
    which is Y4/G3 work; until then this seam stays unwired in production.
    """

    task_id: str
    call_id: str
    tool: str
    operation: str
    result: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not (self.task_id and self.call_id and self.tool and self.operation):
            raise ValueError("a tool call record requires complete server-resolved provenance")
        if not isinstance(self.result, Mapping):
            raise ValueError("a tool call record requires a structured tool result")


class ToolResultSink(Protocol):
    """Receives every validated tool result with its server-owned identity."""

    def __call__(self, record: ToolCallRecord) -> None: ...


ToolResultCallback = Callable[[ToolCallRecord], None]

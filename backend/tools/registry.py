"""Explicit tool allowlist with schema, operation and capability enforcement."""

from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from backend.runtime import CancellationRequested

from .contracts import Tool, ToolContext, ToolError, ToolMode, ToolOperation

# Deterministic registration bounds. A descriptor catalog is copied verbatim
# into every Commander prompt, so its serialized size is a server-owned
# constant rather than something a tool author can grow without noticing.
MAX_OPERATIONS_PER_TOOL = 16
MAX_TOOL_DESCRIPTION_CHARS = 300
MAX_TOOL_CATALOG_JSON_BYTES = 24_576

OPERATION_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
TOOL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,79}$")


#: The scalar types an output-schema "enum" may annotate, with the Python types
#: its values must have (a bool is never an integer or a number here).
_ENUM_VALUE_TYPES: Mapping[str, tuple[type, ...]] = {
    "string": (str,), "integer": (int,), "number": (int, float), "boolean": (bool,)}


#: PR-W S5: annotation keywords a task OUTPUT schema may carry that are never
#: enforced. `normalize_output_schema` removes them before validation, so a
#: plan is never rejected (and never pays a repair) for one of them.
OUTPUT_SCHEMA_STRIPPED_KEYWORDS = frozenset({
    "title", "default", "examples", "format", "pattern", "$schema", "$id",
    "$comment", "readOnly", "writeOnly", "deprecated"})
#: PR-W S5: simple constraints a task output schema may carry, per type. They
#: are KEPT, validated by `validate_output_schema` and ENFORCED by
#: `validate_json_schema`.
OUTPUT_SCHEMA_CONSTRAINT_KEYWORDS: Mapping[str, frozenset[str]] = {
    "integer": frozenset({"minimum", "maximum"}),
    "number": frozenset({"minimum", "maximum"}),
    "string": frozenset({"minLength", "maxLength"}),
    "array": frozenset({"minItems"}),
}
#: Structural bounds of a task output schema. They mirror the bounds every
#: task OUTPUT value already passes (`swarm_v2.tool_calls.MAX_TOOL_VALUE_DEPTH`
#: and `MAX_TOOL_COLLECTION_ITEMS`, pinned equal by a test): a schema nested
#: deeper than any admissible output, or wider than one, can describe nothing
#: the runtime would accept. Beyond them the schema is rejected.
MAX_OUTPUT_SCHEMA_DEPTH = 8
MAX_OUTPUT_SCHEMA_ITEMS = 200


def normalize_output_schema(schema: Any) -> tuple[Any, list[str]]:
    """Remove never-enforced annotation keywords from a TASK output schema.

    Pure and deterministic: no I/O, the input is never mutated (every mapping
    and value in the result is a fresh copy), and the walk follows only the
    structural positions -- `properties` values and `items` -- so a property
    NAMED like a keyword is never touched. Returns the normalized schema and
    the sorted, de-duplicated NAMES of the keywords removed (never values).

    Only `OUTPUT_SCHEMA_STRIPPED_KEYWORDS` are removed. Every other keyword is
    left in place for `validate_output_schema` to judge, so a structural or
    unsupported keyword (oneOf, $ref, const, ...) is still rejected. A
    non-mapping node is returned as is, for validation to reject. Idempotent:
    normalizing a normalized schema changes nothing and strips nothing.

    Raises ValueError beyond MAX_OUTPUT_SCHEMA_DEPTH / MAX_OUTPUT_SCHEMA_ITEMS.
    Tool schemas never pass through here.
    """
    stripped: set[str] = set()

    def walk(node: Any, depth: int) -> Any:
        if not isinstance(node, Mapping):
            return copy.deepcopy(node)
        if depth > MAX_OUTPUT_SCHEMA_DEPTH:
            raise ValueError("output schema nesting exceeds the output depth bound")
        if len(node) > MAX_OUTPUT_SCHEMA_ITEMS:
            raise ValueError("output schema exceeds the output size bound")
        result: dict[Any, Any] = {}
        for key, value in node.items():
            if key in OUTPUT_SCHEMA_STRIPPED_KEYWORDS:
                stripped.add(key)
            elif key == "properties" and isinstance(value, Mapping):
                if len(value) > MAX_OUTPUT_SCHEMA_ITEMS:
                    raise ValueError("output schema exceeds the output size bound")
                result[key] = {name: walk(nested, depth + 1) for name, nested in value.items()}
            elif key == "items":
                result[key] = walk(value, depth + 1)
            else:
                result[key] = copy.deepcopy(value)
        return result

    try:
        normalized = walk(schema, 0)
    except RecursionError:
        raise ValueError("output schema exceeds the output depth bound") from None
    return normalized, sorted(stripped)


def _bound(value: Any, *, integer: bool) -> bool:
    """A usable constraint value: an int (or, for a numeric bound, a finite
    float), never a bool."""
    if isinstance(value, bool):
        return False
    if integer:
        return isinstance(value, int) and value >= 0
    return isinstance(value, int) or (isinstance(value, float) and math.isfinite(value))


def _check_constraints(schema: Mapping[str, Any], expected: str, path: str) -> None:
    """Type rules of the S5 constraint keywords; a malformed one is a
    structural defect, never silently dropped."""
    if expected in {"integer", "number"}:
        low, high, integer = "minimum", "maximum", False
    elif expected == "string":
        low, high, integer = "minLength", "maxLength", True
    elif expected == "array":
        low, high, integer = "minItems", "maxItems", True
    else:
        return
    for key in (low, high):
        if key in schema and not _bound(schema[key], integer=integer):
            raise ValueError(f"{path}.{key} is not a valid bound")
    if low in schema and high in schema and schema[low] > schema[high]:
        raise ValueError(f"{path}.{low} exceeds {high}")


def _enum_values_valid(expected: str, values: Any) -> bool:
    if expected not in _ENUM_VALUE_TYPES or not isinstance(values, list) or not values:
        return False
    kinds = _ENUM_VALUE_TYPES[expected]
    if any(not isinstance(item, kinds) or (expected != "boolean" and isinstance(item, bool))
           for item in values):
        return False
    return len(set(values)) == len(values)


def validate_schema(schema: Mapping[str, Any], path: str = "$schema") -> None:
    """Validate the complete supported schema subset before registration."""
    _check_schema(schema, path, annotations=False)


def validate_output_schema(schema: Mapping[str, Any], path: str = "$output_schema") -> None:
    """Validate a TASK output schema: the tool subset plus two annotations.

    The structure is exactly what `validate_schema` accepts for tools -- arrays
    need `items`, objects are closed with `properties`/`required` -- and on top
    of it an output schema may carry `description` (a string, ignored at run
    time) and `enum` on a string/integer/number/boolean schema (a non-empty
    list of unique values of that type, ENFORCED by `validate_json_schema`).
    Run 6825eb96's successful Gate-0 plan used exactly that enum. Tool schemas
    are unaffected: registration still uses `validate_schema`.

    PR-W S5: it also accepts the simple constraints of
    OUTPUT_SCHEMA_CONSTRAINT_KEYWORDS -- minimum/maximum on integer/number,
    minLength/maxLength on string, minItems on array -- with their type rules
    (see `_check_constraints`); `validate_json_schema` enforces them. The
    annotation keywords of OUTPUT_SCHEMA_STRIPPED_KEYWORDS are not accepted
    here: `normalize_output_schema` removes them first.
    """
    _check_schema(schema, path, annotations=True)


def _check_schema(schema: Any, path: str, *, annotations: bool) -> None:
    if not isinstance(schema, Mapping):
        raise ValueError(f"{path} must be an object")
    expected = schema.get("type")
    allowed_types = {"object", "array", "string", "integer", "number", "boolean", "null"}
    if not isinstance(expected, str) or expected not in allowed_types:
        raise ValueError(f"unsupported schema type at {path}")
    allowed_keys = {"type"}
    if expected == "object":
        allowed_keys |= {"properties", "required", "additionalProperties"}
        properties = schema.get("properties")
        required = schema.get("required")
        if not isinstance(properties, Mapping):
            raise ValueError(f"{path}.properties must be an object")
        if schema.get("additionalProperties") is not False:
            raise ValueError(f"{path} must be a closed object schema")
        if not isinstance(required, list) or any(not isinstance(key, str) for key in required):
            raise ValueError(f"{path}.required must be a string array")
        if len(required) != len(set(required)) or not set(required) <= set(properties):
            raise ValueError(f"{path}.required must be unique properties")
        for name, nested in properties.items():
            if not isinstance(name, str) or not name:
                raise ValueError(f"{path}.properties names must be non-empty strings")
            _check_schema(nested, f"{path}.properties.{name}", annotations=annotations)
    elif expected == "array":
        allowed_keys |= {"items", "maxItems"}
        if "items" not in schema:
            raise ValueError(f"{path}.items is required")
        _check_schema(schema["items"], f"{path}.items", annotations=annotations)
        if "maxItems" in schema and (not isinstance(schema["maxItems"], int) or isinstance(schema["maxItems"], bool) or schema["maxItems"] < 0):
            raise ValueError(f"{path}.maxItems must be a non-negative integer")
    if annotations:
        allowed_keys |= OUTPUT_SCHEMA_CONSTRAINT_KEYWORDS.get(expected, frozenset())
        _check_constraints(schema, expected, path)
        allowed_keys.add("description")
        if "description" in schema and not isinstance(schema["description"], str):
            raise ValueError(f"{path}.description must be a string")
        if "enum" in schema:
            allowed_keys.add("enum")
            if not _enum_values_valid(expected, schema["enum"]):
                raise ValueError(f"{path}.enum must be a non-empty list of unique {expected} values")
    unsupported = set(schema) - allowed_keys
    if unsupported:
        raise ValueError(f"unsupported schema keywords at {path}: {sorted(unsupported)}")


def validate_json_schema(schema: Mapping[str, Any], value: Any, path: str = "$input") -> None:
    """Validate the deliberately small JSON-schema subset tools may expose.

    A malformed SCHEMA is reported exactly like a non-conforming value: as a
    ValueError, never a KeyError/TypeError/AttributeError. Run 280fc9e5 lost
    every task to an array schema without "items", which used to escape here
    as a bare KeyError.
    """
    if not isinstance(schema, Mapping):
        raise ValueError(f"schema at {path} must be an object")
    expected = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "integer": int,
             "number": (int, float), "boolean": bool, "null": type(None)}
    if not isinstance(expected, str) or expected not in types:
        raise ValueError(f"unsupported schema type at {path}")
    if not isinstance(value, types[expected]) or (expected in {"integer", "number"} and isinstance(value, bool)):
        raise ValueError(f"{path} must be {expected}")
    if "enum" in schema:
        # Enforced, never merely advertised. A malformed enum is a schema
        # defect and reported the same way; "description" is ignored here.
        allowed = schema["enum"]
        if not _enum_values_valid(expected, allowed):
            raise ValueError(f"schema at {path} has an invalid enum")
        if not any(value == item and isinstance(value, bool) == isinstance(item, bool)
                   for item in allowed):
            raise ValueError(f"{path} is not one of the allowed values")
    _enforce_constraints(schema, expected, value, path)
    if expected == "object":
        properties = schema.get("properties")
        if not isinstance(properties, Mapping) or schema.get("additionalProperties") is not False:
            raise ValueError(f"{path} must use a closed object schema")
        required = schema.get("required", [])
        if not isinstance(required, list) or any(not isinstance(key, str) for key in required):
            raise ValueError(f"{path}.required must be a string array")
        if any(key not in value for key in required):
            raise ValueError(f"{path} is missing required properties")
        unknown = set(value) - set(properties)
        if unknown:
            raise ValueError(f"{path} has unknown properties: {sorted(unknown)}")
        for key, item in value.items():
            validate_json_schema(properties[key], item, f"{path}.{key}")
    elif expected == "array":
        if "items" not in schema:
            raise ValueError(f"schema at {path} must declare items")
        max_items = schema.get("maxItems")
        if "maxItems" in schema and (not isinstance(max_items, int) or isinstance(max_items, bool)):
            raise ValueError(f"schema at {path} has an invalid maxItems")
        if max_items is not None and len(value) > max_items:
            raise ValueError(f"{path} exceeds maxItems")
        for index, item in enumerate(value):
            validate_json_schema(schema["items"], item, f"{path}[{index}]")


def _enforce_constraints(schema: Mapping[str, Any], expected: str, value: Any, path: str) -> None:
    """PR-W S5: enforce minimum/maximum, minLength/maxLength and minItems.

    Tool schemas can never carry these (`validate_schema` refuses them at
    registration), so only task output schemas reach a check here. A
    malformed bound is a schema defect, reported as a ValueError like every
    other one; maxItems keeps its existing check in `validate_json_schema`.
    """
    if expected in {"integer", "number"}:
        measure, low, high, integer = value, "minimum", "maximum", False
    elif expected == "string":
        measure, low, high, integer = len(value), "minLength", "maxLength", True
    elif expected == "array":
        measure, low, high, integer = len(value), "minItems", None, True
    else:
        return
    for key in (low, high):
        if key is None or key not in schema:
            continue
        bound = schema[key]
        if not _bound(bound, integer=integer):
            raise ValueError(f"schema at {path} has an invalid {key}")
        if (key == low and measure < bound) or (key == high and measure > bound):
            raise ValueError(f"{path} violates {key}")


def _json_copy(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Detach a schema from the live tool object.

    A descriptor is server-owned data handed to an untrusted model, so it must
    never alias a runtime object, expose a callable, or let a later mutation of
    the tool change what was already advertised. Round-tripping through JSON
    both copies and PROVES the schema is plain JSON.
    """
    copied = json.loads(json.dumps(schema, sort_keys=True))
    if not isinstance(copied, dict):
        raise ValueError("schema must serialize to a JSON object")
    return copied


@dataclass(frozen=True)
class ToolOperationDescriptor:
    """Sanitized, server-owned description of one selectable operation."""

    name: str
    description: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]

    def as_payload(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "input_schema": dict(self.input_schema),
                "output_schema": dict(self.output_schema)}


@dataclass(frozen=True)
class ToolDescriptor:
    """Everything Commander may know about a tool, and nothing else.

    Deliberately derived from registered tools only: no credentials, no
    environment configuration, no runtime object, no callback and no
    user-supplied authorization can reach a prompt through this type. A plan
    may REQUEST any capability listed here; granting it stays with the
    server-owned ToolContext.
    """

    name: str
    description: str
    mode: str
    required_scope: str
    operations: tuple[ToolOperationDescriptor, ...]

    def operation(self, name: str) -> ToolOperationDescriptor | None:
        return next((item for item in self.operations if item.name == name), None)

    def as_payload(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "mode": self.mode,
                "required_scope": self.required_scope,
                "operations": [item.as_payload() for item in self.operations]}


def _describe(tool: Tool) -> ToolDescriptor:
    if not TOOL_NAME_PATTERN.fullmatch(str(tool.name or "")):
        raise ValueError(f"invalid tool name: {tool.name!r}")
    if not tool.description or len(tool.description) > MAX_TOOL_DESCRIPTION_CHARS:
        raise ValueError(f"invalid tool description: {tool.name}")
    operations = getattr(tool, "operations", None)
    if not isinstance(operations, Mapping) or not operations:
        raise ValueError(f"tool must declare at least one operation: {tool.name}")
    if len(operations) > MAX_OPERATIONS_PER_TOOL:
        raise ValueError(f"too many operations: {tool.name}")
    described: list[ToolOperationDescriptor] = []
    for key, operation in operations.items():
        if not isinstance(operation, ToolOperation) or key != operation.name:
            raise ValueError(f"invalid operation contract: {tool.name}")
        if not OPERATION_NAME_PATTERN.fullmatch(operation.name):
            raise ValueError(f"invalid operation name: {tool.name}.{operation.name}")
        if not operation.description or len(operation.description) > MAX_TOOL_DESCRIPTION_CHARS:
            raise ValueError(f"invalid operation description: {tool.name}.{operation.name}")
        # Reject unsafe/ambiguous contracts at registration, not invocation.
        validate_schema(operation.input_schema, f"{tool.name}.{operation.name}.input_schema")
        validate_schema(operation.output_schema, f"{tool.name}.{operation.name}.output_schema")
        described.append(ToolOperationDescriptor(
            operation.name, operation.description,
            _json_copy(operation.input_schema), _json_copy(operation.output_schema)))
    # Deterministic ordering: identical registrations always produce an
    # identical catalog, so a Commander prompt is byte-stable across processes.
    described.sort(key=lambda item: item.name)
    return ToolDescriptor(tool.name, tool.description, ToolMode(tool.mode).value,
                          tool.required_scope, tuple(described))


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()):
        self._tools: dict[str, Tool] = {}
        self._descriptors: dict[str, ToolDescriptor] = {}
        for tool in tools:
            if tool.name in self._tools:
                raise ValueError(f"duplicate tool: {tool.name}")
            if tool.mode not in (ToolMode.READ, ToolMode.WRITE) or not tool.required_scope:
                raise ValueError(f"invalid tool contract: {tool.name}")
            self._descriptors[tool.name] = _describe(tool)
            self._tools[tool.name] = tool
        catalog = json.dumps(self.descriptor_payload(), sort_keys=True,
                             separators=(",", ":"), ensure_ascii=True)
        if len(catalog.encode()) > MAX_TOOL_CATALOG_JSON_BYTES:
            raise ValueError("tool descriptor catalog exceeds the prompt size bound")

    @property
    def allowed_names(self) -> frozenset[str]:
        return frozenset(self._tools)

    def descriptors(self) -> tuple[ToolDescriptor, ...]:
        """Sanitized descriptors in deterministic order, for Commander."""
        return tuple(self._descriptors[name] for name in sorted(self._descriptors))

    def descriptor_payload(self) -> list[dict[str, Any]]:
        return [descriptor.as_payload() for descriptor in self.descriptors()]

    def execute(self, name: str, operation: str, context: ToolContext,
                payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """Run one exact call after re-validating it against live authority.

        Plan-time validation is a firewall, not a permit: the selected
        operation and the FINAL resolved payload are checked again here,
        immediately before execution, against the authoritative registered
        schemas. Scope and write approval come from the server-owned
        ToolContext and are never inferred from the plan.
        """
        tool = self._tools.get(name)
        descriptor = self._descriptors.get(name)
        if tool is None or descriptor is None:
            raise ToolError("TOOL_NOT_ALLOWED", "tool is not registered", tool=name)
        selected = descriptor.operation(str(operation))
        if selected is None:
            raise ToolError("TOOL_OPERATION_NOT_ALLOWED", "tool operation is not registered", tool=name)
        if tool.required_scope not in context.scopes:
            raise ToolError("TOOL_SCOPE_REQUIRED", "required tool scope was not granted", tool=name)
        if ToolMode(tool.mode) is ToolMode.WRITE and (not context.write_approved or f"tool:write:{name}" not in context.capabilities):
            raise ToolError("TOOL_WRITE_NOT_APPROVED", "write approval and capability are required", tool=name)
        try:
            validate_json_schema(selected.input_schema, dict(payload))
        except (TypeError, ValueError) as exc:
            raise ToolError("TOOL_INPUT_INVALID", str(exc), tool=name) from None
        try:
            context.check_cancelled()
            result = tool.execute(context, selected.name, payload)
            context.check_cancelled()
        except CancellationRequested:
            raise
        except ToolError:
            raise
        except Exception:
            raise ToolError("TOOL_EXECUTION_FAILED", "tool execution failed", tool=name) from None
        try:
            validate_json_schema(selected.output_schema, result, "$output")
        except (TypeError, ValueError) as exc:
            raise ToolError("TOOL_OUTPUT_INVALID", str(exc), tool=name) from None
        return result

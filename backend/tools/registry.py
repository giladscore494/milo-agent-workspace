"""Explicit tool allowlist with schema, operation and capability enforcement."""

from __future__ import annotations

import json
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


def validate_schema(schema: Mapping[str, Any], path: str = "$schema") -> None:
    """Validate the complete supported schema subset before registration."""
    if not isinstance(schema, Mapping):
        raise ValueError(f"{path} must be an object")
    expected = schema.get("type")
    allowed_types = {"object", "array", "string", "integer", "number", "boolean", "null"}
    if expected not in allowed_types:
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
            validate_schema(nested, f"{path}.properties.{name}")
    elif expected == "array":
        allowed_keys |= {"items", "maxItems"}
        if "items" not in schema:
            raise ValueError(f"{path}.items is required")
        validate_schema(schema["items"], f"{path}.items")
        if "maxItems" in schema and (not isinstance(schema["maxItems"], int) or isinstance(schema["maxItems"], bool) or schema["maxItems"] < 0):
            raise ValueError(f"{path}.maxItems must be a non-negative integer")
    unsupported = set(schema) - allowed_keys
    if unsupported:
        raise ValueError(f"unsupported schema keywords at {path}: {sorted(unsupported)}")


def validate_json_schema(schema: Mapping[str, Any], value: Any, path: str = "$input") -> None:
    """Validate the deliberately small JSON-schema subset tools may expose."""
    expected = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "integer": int,
             "number": (int, float), "boolean": bool, "null": type(None)}
    if expected not in types:
        raise ValueError(f"unsupported schema type at {path}")
    if not isinstance(value, types[expected]) or (expected in {"integer", "number"} and isinstance(value, bool)):
        raise ValueError(f"{path} must be {expected}")
    if expected == "object":
        properties = schema.get("properties")
        if not isinstance(properties, dict) or schema.get("additionalProperties") is not False:
            raise ValueError(f"{path} must use a closed object schema")
        required = schema.get("required", [])
        if not isinstance(required, list) or any(key not in value for key in required):
            raise ValueError(f"{path} is missing required properties")
        unknown = set(value) - set(properties)
        if unknown:
            raise ValueError(f"{path} has unknown properties: {sorted(unknown)}")
        for key, item in value.items():
            validate_json_schema(properties[key], item, f"{path}.{key}")
    elif expected == "array":
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            raise ValueError(f"{path} exceeds maxItems")
        for index, item in enumerate(value):
            validate_json_schema(schema["items"], item, f"{path}[{index}]")


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

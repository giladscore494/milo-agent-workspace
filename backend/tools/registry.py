"""Explicit tool allowlist with schema and capability enforcement."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from backend.runtime import CancellationRequested

from .contracts import Tool, ToolContext, ToolError, ToolMode


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


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()):
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            if tool.name in self._tools:
                raise ValueError(f"duplicate tool: {tool.name}")
            # Reject unsafe/ambiguous contracts at registration, not invocation.
            validate_schema(tool.input_schema, f"{tool.name}.input_schema")
            validate_schema(tool.output_schema, f"{tool.name}.output_schema")
            if tool.mode not in (ToolMode.READ, ToolMode.WRITE) or not tool.required_scope:
                raise ValueError(f"invalid tool contract: {tool.name}")
            self._tools[tool.name] = tool

    @property
    def allowed_names(self) -> frozenset[str]:
        return frozenset(self._tools)

    def execute(self, name: str, context: ToolContext, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolError("TOOL_NOT_ALLOWED", "tool is not registered", tool=name)
        if tool.required_scope not in context.scopes:
            raise ToolError("TOOL_SCOPE_REQUIRED", "required tool scope was not granted", tool=name)
        if ToolMode(tool.mode) is ToolMode.WRITE and (not context.write_approved or f"tool:write:{name}" not in context.capabilities):
            raise ToolError("TOOL_WRITE_NOT_APPROVED", "write approval and capability are required", tool=name)
        try:
            validate_json_schema(tool.input_schema, dict(payload))
        except (TypeError, ValueError) as exc:
            raise ToolError("TOOL_INPUT_INVALID", str(exc), tool=name) from None
        try:
            context.check_cancelled()
            result = tool.execute(context, payload)
            context.check_cancelled()
        except CancellationRequested:
            raise
        except ToolError:
            raise
        except Exception:
            raise ToolError("TOOL_EXECUTION_FAILED", "tool execution failed", tool=name) from None
        try:
            validate_json_schema(tool.output_schema, result, "$output")
        except (TypeError, ValueError) as exc:
            raise ToolError("TOOL_OUTPUT_INVALID", str(exc), tool=name) from None
        return result

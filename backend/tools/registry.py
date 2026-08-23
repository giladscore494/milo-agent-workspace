"""Explicit tool allowlist with schema and capability enforcement."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from .contracts import Tool, ToolContext, ToolError, ToolMode


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
            validate_json_schema(tool.input_schema, {}) if not tool.input_schema.get("required") else None
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
            result = tool.execute(context, payload)
        except ToolError:
            raise
        except Exception:
            raise ToolError("TOOL_EXECUTION_FAILED", "tool execution failed", tool=name) from None
        try:
            validate_json_schema(tool.output_schema, result, "$output")
        except (TypeError, ValueError) as exc:
            raise ToolError("TOOL_OUTPUT_INVALID", str(exc), tool=name) from None
        return result

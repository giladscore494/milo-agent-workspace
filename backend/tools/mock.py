"""Deterministic offline-only tools used by Swarm tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Any

from .contracts import ToolContext, ToolMode


_QUERY = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"], "additionalProperties": False}
_ROWS = {"type": "object", "properties": {"rows": {"type": "array", "items": {"type": "string"}, "maxItems": 20}}, "required": ["rows"], "additionalProperties": False}


@dataclass(frozen=True)
class _OfflineLookup:
    name: str
    description: str
    required_scope: str
    records: Mapping[str, tuple[str, ...]]
    mode: ToolMode = ToolMode.READ
    input_schema = _QUERY
    output_schema = _ROWS

    def execute(self, context: ToolContext, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return {"rows": list(self.records.get(str(payload["query"]), ()))}


class MockSearchTool(_OfflineLookup):
    def __init__(self, records: Mapping[str, tuple[str, ...]] | None = None):
        super().__init__("mock.search", "Offline fixture search", "mock:search", records or {})


class MockCatalogTool(_OfflineLookup):
    def __init__(self, records: Mapping[str, tuple[str, ...]] | None = None):
        super().__init__("mock.catalog", "Offline fixture catalog lookup", "mock:catalog", records or {})


class MockStructuredDataTool(_OfflineLookup):
    def __init__(self, records: Mapping[str, tuple[str, ...]] | None = None):
        super().__init__("mock.structured_data", "Offline fixture structured data", "mock:data", records or {})

"""Deterministic offline-only tools used by Swarm tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Any

from .contracts import ToolContext, ToolMode, ToolOperation


_QUERY = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"], "additionalProperties": False}
_ROWS = {"type": "object", "properties": {"rows": {"type": "array", "items": {"type": "string"}, "maxItems": 20}}, "required": ["rows"], "additionalProperties": False}
_MAKE = {"type": "object", "properties": {"make": {"type": "string"}}, "required": ["make"], "additionalProperties": False}
_MAKE_MODEL = {"type": "object", "properties": {"make": {"type": "string"}, "model": {"type": "string"}},
               "required": ["make", "model"], "additionalProperties": False}

_SEARCH = ToolOperation("search", "Look up offline fixture rows by exact query key.", _QUERY, _ROWS)


@dataclass(frozen=True)
class _OfflineLookup:
    name: str
    description: str
    required_scope: str
    records: Mapping[str, tuple[str, ...]]
    mode: ToolMode = ToolMode.READ
    operations = {"search": _SEARCH}

    def execute(self, context: ToolContext, operation: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
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


@dataclass(frozen=True)
class MockVehicleCatalogTool:
    """An offline tool whose operations need STRUCTURED arguments.

    Deliberately impossible to satisfy with a single free-text `query`: it is
    the fixture that proves a planned call carries the exact arguments an
    operation declares, rather than one guessed payload shape reused for every
    tool. It is a test fixture only and is never registered in production.
    """

    records: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    name: str = "mock.vehicle_catalog"
    description: str = "Offline fixture vehicle catalog keyed by make and model"
    required_scope: str = "mock:vehicle_catalog"
    mode: ToolMode = ToolMode.READ
    operations = {
        "get_model": ToolOperation(
            "get_model", "Return catalog rows for one exact make and model.",
            _MAKE_MODEL, _ROWS),
        "list_models": ToolOperation(
            "list_models", "Return every catalog model known for one make.",
            _MAKE, _ROWS),
    }

    def execute(self, context: ToolContext, operation: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        make = str(payload["make"])
        known = self.records.get(make, ())
        if operation == "list_models":
            return {"rows": list(known)}
        model = str(payload["model"])
        # The resolved arguments are echoed back, so a test can assert the
        # tool ran with the intended values rather than a fallback payload.
        return {"rows": [f"{make} {model}"] if model in known else []}

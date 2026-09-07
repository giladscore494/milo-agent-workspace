"""Deterministic offline-only tools used by Swarm tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Any

from .contracts import ToolContext, ToolError, ToolMode, ToolOperation


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


# --- R3 evidence-shaped offline fixtures -------------------------------------
#
# The two tools below exist to prove the R3 evidence contract end to end
# without a network, a provider or a paid service.  They are the SHAPE a real
# adapter must have, not a stand-in for one: each operation declares exactly
# what it returns, so the trusted mapper for that operation reads named,
# schema'd fields instead of guessing meaning from arbitrary key names.
#
# Both are test fixtures.  Neither is registered in the production
# ToolRegistry, and registering one would additionally require granting its
# scope on the server-owned ToolContext.

_STRUCTURED_RECORD_FIELDS = {
    "model_name": {"type": "string"},
    "model_year": {"type": "integer"},
    "market": {"type": "string"},
    "engine_displacement_cc": {"type": "integer"},
    "list_price": {"type": "number"},
    "price_currency": {"type": "string"},
    "fuel_type": {"type": "string"},
}
# R4: the identity dimensions a real registry record states about the VARIANT,
# as opposed to the commercial model.  They are OPTIONAL in the schema (a
# fixture written before R4 stays valid and simply qualifies itself no
# further) and the mapper copies only the ones the record actually carries: a
# generation, an engine, a transmission or an official model code is never
# inferred and never invented.
_STRUCTURED_IDENTITY_FIELDS = {
    "generation": {"type": "string"},
    "engine": {"type": "string"},
    "transmission": {"type": "string"},
    "model_code": {"type": "string"},
}
_RECORD_REQUEST = {"type": "object", "properties": {"record_id": {"type": "string"}},
                   "required": ["record_id"], "additionalProperties": False}
_RECORD_RESULT = {
    "type": "object",
    "properties": {
        # The dataset's own version identifier: this operation's answer is a
        # snapshot OF A VERSION, and says so rather than leaving the caller to
        # infer one from a timestamp.
        "dataset_version": {"type": "string"},
        "record_id": {"type": "string"},
        "record": {"type": "object",
                   "properties": {**_STRUCTURED_RECORD_FIELDS,
                                  **_STRUCTURED_IDENTITY_FIELDS},
                   "required": sorted(_STRUCTURED_RECORD_FIELDS),
                   "additionalProperties": False},
    },
    "required": ["dataset_version", "record", "record_id"], "additionalProperties": False,
}
_RECORD_LIST_RESULT = {"type": "object",
                       "properties": {"record_ids": {"type": "array", "items": {"type": "string"},
                                                     "maxItems": 20}},
                       "required": ["record_ids"], "additionalProperties": False}
_DATASET_REQUEST = {"type": "object", "properties": {"dataset": {"type": "string"}},
                    "required": ["dataset"], "additionalProperties": False}


@dataclass(frozen=True)
class MockStructuredRegistryTool:
    """An offline tool whose results are RECORDS, not text.

    `get_record` returns one exact record together with the dataset version it
    was read from, so a trusted mapper can build a versioned source, a
    located structured fact and a bounded projection without ever flattening
    the record into an arbitrary string.

    `list_records` deliberately has NO evidence mapper: it is the fixture that
    proves an unmapped operation fails closed instead of falling back to
    generic prefix extraction.
    """

    records: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    dataset_version: str = "2026.08.1"
    name: str = "mock.structured_registry"
    description: str = "Offline fixture registry of structured vehicle records"
    required_scope: str = "mock:structured_registry"
    mode: ToolMode = ToolMode.READ
    operations = {
        "get_record": ToolOperation(
            "get_record", "Return one exact registry record with its dataset version.",
            _RECORD_REQUEST, _RECORD_RESULT),
        "list_records": ToolOperation(
            "list_records", "Return the record ids this fixture dataset holds.",
            _DATASET_REQUEST, _RECORD_LIST_RESULT),
    }

    def execute(self, context: ToolContext, operation: str,
                payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if operation == "list_records":
            return {"record_ids": sorted(self.records)}
        record_id = str(payload["record_id"])
        record = self.records.get(record_id)
        if record is None:
            raise ToolError("RECORD_NOT_FOUND", "no such registry record", tool=self.name)
        return {"dataset_version": self.dataset_version, "record_id": record_id,
                "record": dict(record)}


_PASSAGE_REQUEST = {"type": "object",
                    "properties": {"document_id": {"type": "string"}, "field": {"type": "string"}},
                    "required": ["document_id", "field"], "additionalProperties": False}
_PASSAGE_RESULT = {
    "type": "object",
    "properties": {
        "document_id": {"type": "string"},
        # The document's own revision: a real version identifier, never a
        # retrieval timestamp and never a hash of the excerpt.
        "revision": {"type": "string"},
        # The FULL captured document plus the exact span of the passage that
        # answers the request.  The adapter locates; the trusted mapper cuts
        # the excerpt at that span and validates it against this text, so a
        # span that does not belong to the document fails closed.
        "text": {"type": "string"},
        "match_start": {"type": "integer"},
        "match_end": {"type": "integer"},
        "section": {"type": "string"},
        "field": {"type": "string"},
        "value": {"type": "number"},
        "unit": {"type": "string"},
    },
    "required": ["document_id", "field", "match_end", "match_start", "revision", "section",
                 "text", "unit", "value"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class MockDocumentArchiveTool:
    """An offline document tool that LOCATES rather than dumps.

    Each fixture entry is `(revision, section, intro, sentence, field, value,
    unit)`.  The document the tool returns is `intro + sentence`, and the
    reported span points at the sentence -- which, in the fixtures that matter,
    starts well past the first 400 characters.  A generic prefix extractor
    would therefore store the introduction; the R3 path stores the sentence.
    """

    documents: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    name: str = "mock.document_archive"
    description: str = "Offline fixture document archive with located passages"
    required_scope: str = "mock:document_archive"
    mode: ToolMode = ToolMode.READ
    operations = {
        "locate_passage": ToolOperation(
            "locate_passage", "Return one document and the exact span of the passage asked for.",
            _PASSAGE_REQUEST, _PASSAGE_RESULT),
    }

    def execute(self, context: ToolContext, operation: str,
                payload: Mapping[str, Any]) -> Mapping[str, Any]:
        document_id, wanted = str(payload["document_id"]), str(payload["field"])
        entry = self.documents.get((document_id, wanted)) or self.documents.get(document_id)
        if entry is None or entry.get("field") != wanted:
            raise ToolError("PASSAGE_NOT_FOUND", "no such document passage", tool=self.name)
        text = f"{entry['intro']}{entry['sentence']}"
        start = len(entry["intro"])
        return {"document_id": document_id, "revision": entry["revision"],
                "text": text, "match_start": start, "match_end": start + len(entry["sentence"]),
                "section": entry["section"], "field": wanted,
                "value": entry["value"], "unit": entry["unit"]}

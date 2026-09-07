"""R3: strict, bounded contracts for versioned, located, focused evidence.

Before R3 the evidence path was text-shaped: `extract_source_fragments`
scanned a tool result for generic keys ("snippet", "text", "content",
"rows") and stored a 400-character PREFIX of whatever it found.  That loses
a fact stated after a long introduction, flattens a structured record into
an arbitrary string, keeps no record of WHERE inside the source the fact
was found, and keeps no version of the source at all -- `retrieved_at` and
a fragment hash describe when we looked and what we quoted, never which
version of the source we were looking at.

This module is the replacement contract.  It defines, and only defines,
what a piece of real evidence IS:

*   `SourceVersion` -- a closed version type plus an immutable, bounded
    version identifier.
*   `EvidenceLocator` -- an exact record + field path, or an exact bounded
    document span.  It is NOT a query language: no wildcard, no JSONPath,
    no filter, no expression, no executable path.
*   `FocusedEvidenceFragment` -- focused text with its locator, its
    position and a content hash derived from the final bounded text, and an
    explicit, structural distinction between a verbatim document excerpt
    and a deterministic projection of a structured record.
*   `StructuredEvidenceFact` -- entity/field/value/unit/scope plus the
    locator the value was read from.  A numeric value MUST carry a unit.
*   `VersionedEvidenceSource` / `EvidenceBundle` -- the whole acquisition
    result of one validated tool call.

Pure module: no database access, no provider calls, no tool execution, no
network access, no global mutable state.  Everything is a deterministic
function of its input, so replaying the same tool result yields byte-identical
contracts.

Two rules define this boundary and must never be relaxed:

1.  Nothing here accepts, requests or reconstructs a model completion.  The
    inputs are the already validated result of a registered tool operation
    and the trusted mapping code that knows that operation's shape.
2.  Everything is bounded BEFORE it can reach durable storage, and a
    violation FAILS CLOSED.  There is no truncation, no prefix fallback and
    no "best effort" repair anywhere in this module.

Deliberately NOT here (R4): unit conversion, semantic value equivalence and
any comparison between a claim and a structured fact.  A unit is carried and
preserved verbatim; it is never interpreted.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable, Literal, Mapping, Sequence

from pydantic import ConfigDict, Field, ValidationError, model_validator

from .contracts import StrictContract
from .evidence_bounds import (FRAGMENT_TYPES, LOCATOR_KINDS, MAX_DOCUMENT_OFFSET,
                              MAX_FACTS_PER_BUNDLE, MAX_FACT_COLLECTION_ITEMS,
                              MAX_FACT_VALUE_DEPTH, MAX_FACT_VALUE_JSON_BYTES,
                              MAX_LOCATOR_KEY_CHARS, MAX_LOCATOR_PATH_SEGMENTS,
                              MAX_LOCATOR_RECORD_ID_CHARS, MAX_LOCATOR_SCOPE_IDS,
                              MAX_LOCATOR_SECTION_CHARS, MAX_LOCATOR_SEGMENT_CHARS,
                              MAX_PROJECTION_FIELDS, MAX_SOURCE_VERSION_CHARS,
                              MAX_SOURCE_VERSION_KEY_CHARS, MAX_TIME_SCOPE_KEYS,
                              MAX_TOOL_SNAPSHOT_JSON_BYTES, MAX_UNIT_CHARS,
                              SOURCE_VERSION_KINDS)
from .fragments import (MAX_FRAGMENT_CHARS, MAX_FRAGMENTS_PER_SOURCE,
                        MAX_FRAGMENT_TOTAL_CHARS_PER_SOURCE, fragment_content_hash,
                        normalize_fragment_text)

# The deterministic bounds live in .evidence_bounds (a dependency-free leaf,
# so .contracts can bound the same provenance without an import cycle) and are
# re-exported here; the three FRAGMENT bounds come from .fragments unchanged.
#
# A locator segment is a LITERAL object key and a record id is a LITERAL
# identifier.  Neither pattern admits `$`, `*`, `[`, `]`, `?`, a quote, a
# comma or `..`, so JSONPath/expression/filter syntax is rejected as a
# malformed key rather than being parsed and then refused.
_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")
_RECORD_ID_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:@-]{0,127}$")
_UNIT_PATTERN = re.compile(r"^[A-Za-z%][A-Za-z0-9%^/._-]{0,31}$")
_VERSION_PATTERNS = {
    "content_sha256": re.compile(r"^[0-9a-f]{64}$"),
    "dataset_version": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$"),
    "document_revision": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$"),
    "git_commit": re.compile(r"^[0-9a-f]{7,64}$"),
}

EVIDENCE_CONTRACT_REASONS = frozenset({
    "EVIDENCE_CONTRACT_INVALID",
    "EVIDENCE_FACT_UNIT_REQUIRED",
    "EVIDENCE_FRAGMENT_HASH_MISMATCH",
    "EVIDENCE_FRAGMENT_TEXT_INVALID",
    "EVIDENCE_LIMIT_EXCEEDED",
    "EVIDENCE_LOCATOR_INVALID",
    "EVIDENCE_LOCATOR_OUT_OF_SCOPE",
    "EVIDENCE_SOURCE_VERSION_INVALID",
    "EVIDENCE_VALUE_INVALID",
})


class EvidenceContractError(ValueError):
    """An R3 contract failure carrying ONLY a static, code-owned reason.

    The rejected value, the source text and the pydantic diagnostic (which
    embeds the offending input verbatim) never travel with the
    classification, so the safe representation is fit for a durable task
    result, a run event and telemetry alike.
    """

    MESSAGES = {
        "EVIDENCE_CONTRACT_INVALID": "evidence does not satisfy the R3 contract",
        "EVIDENCE_FACT_UNIT_REQUIRED": "a numeric structured fact requires an explicit unit",
        "EVIDENCE_FRAGMENT_HASH_MISMATCH": "fragment content hash does not match the bounded text",
        "EVIDENCE_FRAGMENT_TEXT_INVALID": "fragment text is empty or not normalized",
        "EVIDENCE_LIMIT_EXCEEDED": "evidence exceeds a deterministic size bound",
        "EVIDENCE_LOCATOR_INVALID": "evidence locator is not a bounded literal location",
        "EVIDENCE_LOCATOR_OUT_OF_SCOPE": "evidence locator does not belong to this source",
        "EVIDENCE_SOURCE_VERSION_INVALID": "source version is missing or malformed",
        "EVIDENCE_VALUE_INVALID": "structured value is not bounded JSON evidence",
    }

    def __init__(self, reason_code: str):
        if reason_code not in EVIDENCE_CONTRACT_REASONS:
            raise ValueError("evidence contract reason must come from the static allowlist")
        self.reason_code = reason_code
        self.safe_message = self.MESSAGES[reason_code]
        super().__init__(self.safe_message)


def _reason_of(exc: ValidationError) -> str:
    """Map a pydantic failure back onto ONE static reason code.

    Only the raised `EvidenceContractError` is read.  Pydantic's own message
    and its `input` echo are deliberately dropped: they quote the rejected
    evidence, which must never reach a durable result or a log line.
    """
    for item in exc.errors():
        cause = item.get("ctx", {}).get("error") if isinstance(item.get("ctx"), Mapping) else None
        if isinstance(cause, EvidenceContractError):
            return cause.reason_code
    return "EVIDENCE_CONTRACT_INVALID"


class BoundedEvidenceContract(StrictContract):
    """Strict, frozen, closed contract with ONE safe failure representation.

    `extra="forbid"` and `strict=True` come from StrictContract; `frozen=True`
    is added here because an acquired piece of evidence is an audit record and
    must not be mutated after validation.  Both construction paths funnel
    through `__init__`, so a caller can only ever see EvidenceContractError.
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    def __init__(self, **data: Any) -> None:
        try:
            super().__init__(**data)
        except ValidationError as exc:
            # `from None`: the pydantic exception carries the rejected input.
            raise EvidenceContractError(_reason_of(exc)) from None

    @classmethod
    def model_validate(cls, obj: Any, *args: Any, **kwargs: Any):  # type: ignore[override]
        if not isinstance(obj, Mapping):
            raise EvidenceContractError("EVIDENCE_CONTRACT_INVALID")
        return cls(**dict(obj))


def canonical_json(value: Any) -> str:
    """Deterministic JSON identity; the ONE serialization this module uses."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _bounded_value(value: Any, *, depth: int = 0) -> Any:
    """Reject anything that is not small, shallow, JSON-shaped evidence."""
    if depth > MAX_FACT_VALUE_DEPTH:
        raise EvidenceContractError("EVIDENCE_VALUE_INVALID")
    if isinstance(value, Mapping):
        if len(value) > MAX_FACT_COLLECTION_ITEMS:
            raise EvidenceContractError("EVIDENCE_VALUE_INVALID")
        for key, item in value.items():
            if not isinstance(key, str) or not _SEGMENT_PATTERN.fullmatch(key):
                raise EvidenceContractError("EVIDENCE_VALUE_INVALID")
            _bounded_value(item, depth=depth + 1)
        return value
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_FACT_COLLECTION_ITEMS:
            raise EvidenceContractError("EVIDENCE_VALUE_INVALID")
        for item in value:
            _bounded_value(item, depth=depth + 1)
        return value
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise EvidenceContractError("EVIDENCE_VALUE_INVALID")


def _bounded_json(value: Any, limit: int) -> str:
    _bounded_value(value)
    try:
        encoded = canonical_json(value)
    except (TypeError, ValueError):
        raise EvidenceContractError("EVIDENCE_VALUE_INVALID") from None
    if len(encoded.encode()) > limit:
        raise EvidenceContractError("EVIDENCE_LIMIT_EXCEEDED")
    return encoded


class SourceVersion(BoundedEvidenceContract):
    """The exact version of the source the evidence was read from.

    A closed set of version TYPES, each with its own shape, plus an immutable
    bounded identifier.  `retrieved_at` is not a version (it says when we
    looked, not what we looked at) and a fragment hash is not a version (it
    identifies the excerpt, not the source).  When a snapshot has no external
    version identifier the trusted adapter may use the SHA-256 of the EXACT
    FULL snapshot it acquired -- see `snapshot_version` -- never a hash of the
    selected partial fragment.
    """

    kind: Literal["content_sha256", "dataset_version", "document_revision", "git_commit"]
    identifier: str = Field(min_length=1, max_length=MAX_SOURCE_VERSION_CHARS)

    @model_validator(mode="after")
    def _shape(self) -> "SourceVersion":
        if not _VERSION_PATTERNS[self.kind].fullmatch(self.identifier):
            raise EvidenceContractError("EVIDENCE_SOURCE_VERSION_INVALID")
        return self

    @property
    def version_key(self) -> str:
        """The single durable/canonical representation: `kind:identifier`."""
        return f"{self.kind}:{self.identifier}"


def snapshot_version(snapshot: Any) -> SourceVersion:
    """A content version over the EXACT FULL snapshot the adapter acquired.

    Deliberately takes the whole snapshot, never a selected fragment: a
    version computed from the part we quoted would change whenever the
    selection changed and would silently equate two different sources that
    happened to share one sentence.
    """
    encoded = _bounded_json(snapshot, MAX_TOOL_SNAPSHOT_JSON_BYTES)
    return SourceVersion(kind="content_sha256",
                         identifier=hashlib.sha256(encoded.encode()).hexdigest())


class EvidenceLocator(BoundedEvidenceContract):
    """WHERE inside the source this evidence came from.

    Two closed shapes and nothing else:

    *   `record_field` -- one record identifier plus a bounded literal field
        path (at most 6 segments, each a literal object key).
    *   `document_span` -- one document identifier plus a bounded half-open
        character span, optionally qualified by a section/table heading so a
        model year or a qualifying footnote can travel with the excerpt.

    There is no expression, no wildcard, no filter, no slice, no recursive
    descent and no query syntax of any kind, and nothing here is ever
    evaluated against anything: a locator is compared and stored, never run.
    """

    kind: Literal["document_span", "record_field"]
    record_id: str = Field(min_length=1, max_length=MAX_LOCATOR_RECORD_ID_CHARS)
    field_path: tuple[str, ...] = ()
    section: str | None = Field(default=None, max_length=MAX_LOCATOR_SECTION_CHARS)
    char_start: int | None = None
    char_end: int | None = None

    @model_validator(mode="after")
    def _shape(self) -> "EvidenceLocator":
        if not _RECORD_ID_PATTERN.fullmatch(self.record_id):
            raise EvidenceContractError("EVIDENCE_LOCATOR_INVALID")
        if self.section is not None and (
                not self.section or normalize_fragment_text(self.section) != self.section):
            raise EvidenceContractError("EVIDENCE_LOCATOR_INVALID")
        if self.kind == "record_field":
            if not 1 <= len(self.field_path) <= MAX_LOCATOR_PATH_SEGMENTS:
                raise EvidenceContractError("EVIDENCE_LOCATOR_INVALID")
            if any(not _SEGMENT_PATTERN.fullmatch(segment) for segment in self.field_path):
                raise EvidenceContractError("EVIDENCE_LOCATOR_INVALID")
            if self.char_start is not None or self.char_end is not None or self.section is not None:
                raise EvidenceContractError("EVIDENCE_LOCATOR_INVALID")
        else:
            if self.field_path:
                raise EvidenceContractError("EVIDENCE_LOCATOR_INVALID")
            start, end = self.char_start, self.char_end
            if not isinstance(start, int) or not isinstance(end, int):
                raise EvidenceContractError("EVIDENCE_LOCATOR_INVALID")
            if not 0 <= start < end <= MAX_DOCUMENT_OFFSET:
                raise EvidenceContractError("EVIDENCE_LOCATOR_INVALID")
            # A span can never be wider than one durable fragment, so an
            # "excerpt" covering a whole page is impossible by construction.
            if end - start > MAX_FRAGMENT_CHARS:
                raise EvidenceContractError("EVIDENCE_LIMIT_EXCEEDED")
        if len(self.locator_key) > MAX_LOCATOR_KEY_CHARS:
            raise EvidenceContractError("EVIDENCE_LIMIT_EXCEEDED")
        return self

    @property
    def locator_key(self) -> str:
        """The single durable/canonical representation of this location.

        A canonical JSON array rather than a formatted string: it is provably
        injective, so two different locations can never collapse onto one
        durable identity and dedupe evidence that is not the same evidence.
        """
        return canonical_json([self.kind, self.record_id, list(self.field_path),
                               self.section, self.char_start, self.char_end])


def record_field_locator(record_id: str, field_path: Sequence[str]) -> EvidenceLocator:
    """A record + literal field-path locator built by trusted mapping code."""
    if not isinstance(field_path, Sequence) or isinstance(field_path, (str, bytes)):
        raise EvidenceContractError("EVIDENCE_LOCATOR_INVALID")
    return EvidenceLocator(kind="record_field", record_id=record_id,
                           field_path=tuple(field_path))


def document_span_locator(document_id: str, char_start: int, char_end: int, *,
                          section: str | None = None) -> EvidenceLocator:
    """A bounded document-span locator built by trusted mapping code."""
    return EvidenceLocator(kind="document_span", record_id=document_id,
                           char_start=char_start, char_end=char_end, section=section)


class FocusedEvidenceFragment(BoundedEvidenceContract):
    """One focused piece of evidence with its exact location.

    The fragment TYPE is structural, not advisory: a `verbatim_excerpt` can
    only ever carry a document span, and a `structured_projection` can only
    ever carry a record field path.  A deterministic projection of a
    structured record is therefore impossible to present as a verbatim quote.

    `content_hash` is derived from the FINAL bounded text and re-derived here,
    so a hash from another fragment, a rewritten text and an invented hash all
    fail closed -- exactly as the durable RPC re-derives it in PostgreSQL.
    """

    fragment_type: Literal["structured_projection", "verbatim_excerpt"]
    text: str = Field(min_length=1, max_length=MAX_FRAGMENT_CHARS)
    locator: EvidenceLocator
    fragment_index: int = Field(ge=0, lt=MAX_FRAGMENTS_PER_SOURCE)
    content_hash: str

    @model_validator(mode="after")
    def _shape(self) -> "FocusedEvidenceFragment":
        if normalize_fragment_text(self.text) != self.text:
            raise EvidenceContractError("EVIDENCE_FRAGMENT_TEXT_INVALID")
        expected = "document_span" if self.fragment_type == "verbatim_excerpt" else "record_field"
        if self.locator.kind != expected:
            raise EvidenceContractError("EVIDENCE_LOCATOR_INVALID")
        if fragment_content_hash(self.text) != self.content_hash:
            raise EvidenceContractError("EVIDENCE_FRAGMENT_HASH_MISMATCH")
        return self


def verbatim_excerpt(*, document_text: str, locator: EvidenceLocator,
                     fragment_index: int) -> FocusedEvidenceFragment:
    """Cut the excerpt the locator names out of the source's OWN text.

    The span is validated against the real document the tool returned, so a
    locator that does not belong to this source fails closed instead of
    silently producing a shorter quote.  There is no prefix fallback here:
    the excerpt is whatever the locator points at, wherever that is in the
    document, so a fact stated after a long introduction is captured exactly
    and the introduction is not stored in its place.

    The cut text passes through the SAME whitespace/control-character
    normalization every durable fragment gets, so the stored quote can be
    marginally shorter than the span it names.  The locator keeps naming the
    source's own raw offsets, which is what makes it verifiable against the
    source rather than against our copy of it.
    """
    if locator.kind != "document_span":
        raise EvidenceContractError("EVIDENCE_LOCATOR_INVALID")
    if not isinstance(document_text, str):
        raise EvidenceContractError("EVIDENCE_FRAGMENT_TEXT_INVALID")
    if locator.char_end > len(document_text):
        raise EvidenceContractError("EVIDENCE_LOCATOR_OUT_OF_SCOPE")
    text = normalize_fragment_text(document_text[locator.char_start:locator.char_end])
    if not text:
        raise EvidenceContractError("EVIDENCE_FRAGMENT_TEXT_INVALID")
    if len(text) > MAX_FRAGMENT_CHARS:
        raise EvidenceContractError("EVIDENCE_LIMIT_EXCEEDED")
    return FocusedEvidenceFragment(fragment_type="verbatim_excerpt", text=text, locator=locator,
                                   fragment_index=fragment_index,
                                   content_hash=fragment_content_hash(text))


def read_locator_path(record: Mapping[str, Any], locator: EvidenceLocator) -> Any:
    """Walk a `record_field` locator with plain literal container access.

    No expression is interpreted and no key is guessed: a path that does not
    resolve inside the record the tool actually returned is a locator that
    does not belong to this source.
    """
    if locator.kind != "record_field":
        raise EvidenceContractError("EVIDENCE_LOCATOR_INVALID")
    current: Any = record
    for segment in locator.field_path:
        if not isinstance(current, Mapping) or segment not in current:
            raise EvidenceContractError("EVIDENCE_LOCATOR_OUT_OF_SCOPE")
        current = current[segment]
    return current


def canonical_projection(record: Mapping[str, Any], fields: Sequence[str]) -> str:
    """A deterministic `key=value` projection of EXPLICITLY allowed fields.

    This is the textual material a grounded verifier can still read for a
    structured source.  It is never presented as a quote: every fragment
    built from it is typed `structured_projection`.  Only scalar fields the
    caller named are projected, in the order it named them, and a projection
    that would exceed the durable fragment bound is REJECTED rather than
    trimmed -- silently shortening it would drop the very field the fact
    rests on.
    """
    if not isinstance(record, Mapping):
        raise EvidenceContractError("EVIDENCE_VALUE_INVALID")
    if not 1 <= len(fields) <= MAX_PROJECTION_FIELDS or len(set(fields)) != len(fields):
        raise EvidenceContractError("EVIDENCE_LIMIT_EXCEEDED")
    parts: list[str] = []
    for name in fields:
        if not isinstance(name, str) or not _SEGMENT_PATTERN.fullmatch(name) or name not in record:
            raise EvidenceContractError("EVIDENCE_LOCATOR_OUT_OF_SCOPE")
        value = record[name]
        if isinstance(value, (Mapping, list, tuple)):
            raise EvidenceContractError("EVIDENCE_VALUE_INVALID")
        _bounded_value(value)
        parts.append(f"{name}={value if isinstance(value, str) else canonical_json(value)}")
    text = normalize_fragment_text("; ".join(parts))
    if not text:
        raise EvidenceContractError("EVIDENCE_FRAGMENT_TEXT_INVALID")
    if len(text) > MAX_FRAGMENT_CHARS:
        raise EvidenceContractError("EVIDENCE_LIMIT_EXCEEDED")
    return text


def structured_projection(*, record: Mapping[str, Any], fields: Sequence[str],
                          locator: EvidenceLocator,
                          fragment_index: int) -> FocusedEvidenceFragment:
    """Project a structured record into ONE bounded, explicitly-typed fragment.

    The locator must resolve inside the record the tool actually returned, so
    a projection can never be attributed to a field the source does not have.
    """
    read_locator_path(record, locator)
    text = canonical_projection(record, fields)
    return FocusedEvidenceFragment(fragment_type="structured_projection", text=text,
                                   locator=locator, fragment_index=fragment_index,
                                   content_hash=fragment_content_hash(text))


class StructuredEvidenceFact(BoundedEvidenceContract):
    """One structured fact read from an exact location in a versioned source.

    The unit travels WITH the value from here to the durable claim and on
    into the grounding contract, so no layer has to re-derive it from prose.
    A numeric value without a unit is a contract violation, not a default:
    "1798" is not a fact until it says cc.  Non-quantitative facts use
    `unit=None` explicitly.

    R3 carries the unit; it never interprets it.  Unit conversion and
    semantic value equivalence belong to R4 and are deliberately absent.
    """

    entity_key: str = Field(min_length=1, max_length=200)
    field_key: str = Field(min_length=1, max_length=200)
    value: Any
    unit: str | None = Field(default=None, max_length=MAX_UNIT_CHARS)
    time_scope: dict[str, Any] = Field(default_factory=dict)
    geography: str | None = Field(default=None, max_length=200)
    market: str | None = Field(default=None, max_length=200)
    locator: EvidenceLocator

    @model_validator(mode="after")
    def _shape(self) -> "StructuredEvidenceFact":
        _bounded_json(self.value, MAX_FACT_VALUE_JSON_BYTES)
        if isinstance(self.value, bool) or not isinstance(self.value, (int, float)):
            pass
        elif self.unit is None:
            raise EvidenceContractError("EVIDENCE_FACT_UNIT_REQUIRED")
        if self.unit is not None and not _UNIT_PATTERN.fullmatch(self.unit):
            raise EvidenceContractError("EVIDENCE_VALUE_INVALID")
        if len(self.time_scope) > MAX_TIME_SCOPE_KEYS:
            raise EvidenceContractError("EVIDENCE_LIMIT_EXCEEDED")
        _bounded_json(self.time_scope, MAX_FACT_VALUE_JSON_BYTES)
        return self


class VersionedEvidenceSource(BoundedEvidenceContract):
    """Source metadata that is INSEPARABLE from the version it was read at.

    The same descriptive metadata the existing `SourceCreate` carries, plus
    the one thing it never had: which version of the source this is.  The
    version comes from the trusted adapter/mapper and is persisted with the
    source, so evidence acquired from two versions can never merge.
    """

    agent: str = Field(min_length=1, max_length=120)
    url: str = Field(min_length=1, max_length=1000)
    title: str = Field(min_length=1, max_length=400)
    domain: str = Field(min_length=1, max_length=200)
    source_type: str = Field(min_length=1, max_length=64)
    source_strength: str = Field(min_length=1, max_length=64)
    source_date: str | None = Field(default=None, max_length=64)
    query: str = Field(min_length=1, max_length=500)
    tool_operation: str = Field(min_length=1, max_length=160)
    version: SourceVersion
    confidence: float = Field(ge=0.0, le=1.0)


class EvidenceBundle(BoundedEvidenceContract):
    """Everything one validated tool call contributed, as ONE unit.

    `locator_scope` is the closed set of record/document identifiers the
    validated tool result actually contained.  Every fact locator and every
    fragment locator must name one of them, so a mapper cannot attribute
    evidence to a record the source never returned.
    """

    source: VersionedEvidenceSource
    locator_scope: tuple[str, ...]
    facts: tuple[StructuredEvidenceFact, ...]
    fragments: tuple[FocusedEvidenceFragment, ...]

    @model_validator(mode="after")
    def _shape(self) -> "EvidenceBundle":
        scope = self.locator_scope
        if not 1 <= len(scope) <= MAX_LOCATOR_SCOPE_IDS or len(set(scope)) != len(scope):
            raise EvidenceContractError("EVIDENCE_LIMIT_EXCEEDED")
        if any(not _RECORD_ID_PATTERN.fullmatch(item) for item in scope):
            raise EvidenceContractError("EVIDENCE_LOCATOR_INVALID")
        if not 1 <= len(self.facts) <= MAX_FACTS_PER_BUNDLE:
            raise EvidenceContractError("EVIDENCE_LIMIT_EXCEEDED")
        if not 1 <= len(self.fragments) <= MAX_FRAGMENTS_PER_SOURCE:
            raise EvidenceContractError("EVIDENCE_LIMIT_EXCEEDED")
        allowed = frozenset(scope)
        for item in (*self.facts, *self.fragments):
            if item.locator.record_id not in allowed:
                raise EvidenceContractError("EVIDENCE_LOCATOR_OUT_OF_SCOPE")
        # Positions are assigned by trusted code, so they are exactly the
        # bundle's own order -- never a mapper-chosen number.
        if [item.fragment_index for item in self.fragments] != list(range(len(self.fragments))):
            raise EvidenceContractError("EVIDENCE_CONTRACT_INVALID")
        identities = [(item.locator.locator_key, item.content_hash) for item in self.fragments]
        if len(set(identities)) != len(identities):
            raise EvidenceContractError("EVIDENCE_CONTRACT_INVALID")
        if sum(len(item.text) for item in self.fragments) > MAX_FRAGMENT_TOTAL_CHARS_PER_SOURCE:
            raise EvidenceContractError("EVIDENCE_LIMIT_EXCEEDED")
        return self


def build_evidence_bundle(*, source: VersionedEvidenceSource, locator_scope: Iterable[str],
                          facts: Iterable[StructuredEvidenceFact],
                          fragments: Iterable[FocusedEvidenceFragment]) -> EvidenceBundle:
    """Assemble a bundle from trusted mapping output."""
    return EvidenceBundle(source=source, locator_scope=tuple(locator_scope),
                          facts=tuple(facts), fragments=tuple(fragments))


__all__ = [
    "EVIDENCE_CONTRACT_REASONS", "FRAGMENT_TYPES", "LOCATOR_KINDS",
    "MAX_DOCUMENT_OFFSET", "MAX_FACTS_PER_BUNDLE", "MAX_FACT_COLLECTION_ITEMS",
    "MAX_FACT_VALUE_DEPTH", "MAX_FACT_VALUE_JSON_BYTES", "MAX_LOCATOR_KEY_CHARS",
    "MAX_LOCATOR_PATH_SEGMENTS", "MAX_LOCATOR_RECORD_ID_CHARS",
    "MAX_LOCATOR_SCOPE_IDS", "MAX_LOCATOR_SECTION_CHARS", "MAX_LOCATOR_SEGMENT_CHARS",
    "MAX_PROJECTION_FIELDS", "MAX_SOURCE_VERSION_CHARS", "MAX_TIME_SCOPE_KEYS",
    "MAX_SOURCE_VERSION_KEY_CHARS", "MAX_TOOL_SNAPSHOT_JSON_BYTES",
    "MAX_UNIT_CHARS", "SOURCE_VERSION_KINDS",
    "BoundedEvidenceContract", "EvidenceBundle", "EvidenceContractError",
    "EvidenceLocator", "FocusedEvidenceFragment", "SourceVersion",
    "StructuredEvidenceFact", "VersionedEvidenceSource",
    "build_evidence_bundle", "canonical_json", "canonical_projection",
    "document_span_locator", "read_locator_path", "record_field_locator",
    "snapshot_version", "structured_projection", "verbatim_excerpt",
]

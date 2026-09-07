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

import copy
import hashlib
import json
import math
import re
from typing import Any, Iterable, Literal, Mapping, Sequence

from pydantic import ConfigDict, Field, ValidationError, model_validator

from .contracts import StrictContract
from .evidence_bounds import (FRAGMENT_TYPE_BY_LOCATOR_KIND, FRAGMENT_TYPES,
                              IDENTITY_DIMENSIONS, LOCATOR_KINDS,
                              LOCATOR_RECORD_ID_PATTERN, LOCATOR_SEGMENT_PATTERN,
                              MAX_DOCUMENT_OFFSET, MAX_FACTS_PER_BUNDLE,
                              MAX_IDENTITY_DIMENSION_CHARS,
                              MAX_FACT_COLLECTION_ITEMS, MAX_FACT_VALUE_DEPTH,
                              MAX_FACT_VALUE_JSON_BYTES, MAX_LOCATOR_KEY_CHARS,
                              MAX_LOCATOR_PATH_SEGMENTS, MAX_LOCATOR_RECORD_ID_CHARS,
                              MAX_LOCATOR_SCOPE_IDS, MAX_LOCATOR_SECTION_CHARS,
                              MAX_LOCATOR_SEGMENT_CHARS, MAX_PROJECTION_FIELDS,
                              MAX_SOURCE_VERSION_CHARS, MAX_SOURCE_VERSION_KEY_CHARS,
                              MAX_TIME_SCOPE_KEYS, MAX_TOOL_SNAPSHOT_JSON_BYTES,
                              MAX_UNIT_CHARS, SOURCE_VERSION_KINDS, SOURCE_VERSION_PATTERNS)
from .fragments import (MAX_FRAGMENT_CHARS, MAX_FRAGMENTS_PER_SOURCE,
                        MAX_FRAGMENT_TOTAL_CHARS_PER_SOURCE, fragment_content_hash,
                        normalize_fragment_text)

# The deterministic bounds, the closed vocabularies and the shared identifier
# patterns live in .evidence_bounds (a dependency-free leaf, so .contracts can
# bound the same provenance without an import cycle, and so the SQL copies can
# be pinned against ONE definition) and are re-exported here; the three
# FRAGMENT bounds come from .fragments unchanged.
_SEGMENT_PATTERN = re.compile(LOCATOR_SEGMENT_PATTERN)
_RECORD_ID_PATTERN = re.compile(LOCATOR_RECORD_ID_PATTERN)
_UNIT_PATTERN = re.compile(r"^[A-Za-z%][A-Za-z0-9%^/._-]{0,31}$")
_VERSION_PATTERNS = {kind: re.compile(pattern) for kind, pattern in SOURCE_VERSION_PATTERNS.items()}

EVIDENCE_CONTRACT_REASONS = frozenset({
    "EVIDENCE_CONTRACT_INVALID",
    "EVIDENCE_FACT_IDENTITY_INVALID",
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
        "EVIDENCE_FACT_IDENTITY_INVALID": "structured fact identity is not a bounded closed dimension set",
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
    """Deterministic JSON identity; the ONE serialization this module uses.

    `allow_nan=False` is deliberate: NaN and the infinities are not JSON, and a
    token like `Infinity` would otherwise be emitted here and only fail later,
    at a serialization or PostgreSQL boundary, instead of at acquisition.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False)


def _locator_json(value: Any) -> str:
    """The canonical rendering of a locator: compact, unicode kept verbatim.

    `ensure_ascii=False` (unlike canonical_json) so the rendering is exactly
    what PostgreSQL's own `to_jsonb(text)::text` produces for the section
    heading: the guarded RPCs rebuild this string from the parsed locator and
    require the stored text to match it byte for byte.
    """
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


class FrozenDict(dict):
    """A dict that cannot be changed after the contract validated it.

    A frozen pydantic model only protects its own attributes; a nested dict
    inside a `value: Any` field stayed mutable, so a caller holding the
    already-validated fact could deepen or widen it past the bounds after the
    check had run.  Freezing the nested structure at validation time closes
    that gap at its source.  It stays a real `dict` so json, pydantic
    serialization and every existing consumer read it unchanged.
    """

    __slots__ = ()

    def _immutable(self, *_args: Any, **_kwargs: Any) -> None:
        raise TypeError("validated evidence values are immutable")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = _immutable  # type: ignore[assignment]
    __ior__ = _immutable  # type: ignore[assignment]

    def __reduce__(self) -> tuple[Any, ...]:
        return (FrozenDict, (dict(self),))

    def __copy__(self) -> "FrozenDict":
        return FrozenDict(self)

    def __deepcopy__(self, memo: dict[int, Any]) -> "FrozenDict":
        return FrozenDict({key: copy.deepcopy(item, memo) for key, item in self.items()})


def _freeze(value: Any) -> Any:
    """Deep-freeze a validated JSON value: dicts become FrozenDict, lists tuples."""
    if isinstance(value, Mapping):
        return FrozenDict({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    """A fresh, mutable deep copy of a JSON value, for revalidation."""
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


def _bounded_value(value: Any, *, depth: int = 0) -> Any:
    """Reject anything that is not small, shallow, finite, JSON-shaped evidence."""
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
    if isinstance(value, float) and not math.isfinite(value):
        # NaN, +inf and -inf are not JSON and can never be evidence.
        raise EvidenceContractError("EVIDENCE_VALUE_INVALID")
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


def _reject_constant(_token: str) -> Any:
    # json.loads would otherwise accept the non-JSON tokens NaN/Infinity.
    raise ValueError("non-standard JSON numeric token")


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


def parse_version_key(value: Any) -> SourceVersion:
    """Rebuild a SourceVersion from its canonical durable `kind:identifier`.

    This is how the grounding reader validates a version that came back from
    the database: the kind must be one of the closed set, the identifier must
    satisfy that kind's own rule, and the rebuilt key must equal the stored
    text exactly.  `content_sha256:abc` therefore fails here just as it fails
    in the Python contract and in the guarded RPC.
    """
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_SOURCE_VERSION_KEY_CHARS:
        raise EvidenceContractError("EVIDENCE_SOURCE_VERSION_INVALID")
    kind, separator, identifier = value.partition(":")
    if not separator or not identifier or kind not in SOURCE_VERSION_KINDS:
        raise EvidenceContractError("EVIDENCE_SOURCE_VERSION_INVALID")
    version = SourceVersion(kind=kind, identifier=identifier)  # type: ignore[arg-type]
    if version.version_key != value:
        raise EvidenceContractError("EVIDENCE_SOURCE_VERSION_INVALID")
    return version


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
        return _locator_json([self.kind, self.record_id, list(self.field_path),
                              self.section, self.char_start, self.char_end])


def parse_locator_key(value: Any) -> EvidenceLocator:
    """Rebuild an EvidenceLocator from its canonical durable text, or fail.

    The ONLY way a locator string re-enters the contract: it must be a JSON
    array of exactly six elements `[kind, record_id, field_path, section,
    char_start, char_end]`, every element must satisfy the closed shape for
    its kind, and the rebuilt locator's own canonical rendering must equal the
    input byte for byte.  A non-JSON string, a differently spaced or ordered
    rendering, a wildcard segment, an extra element, a float offset and a
    locator of the wrong kind for its fragment type all fail here.  Nothing is
    evaluated: the string is parsed, compared and rebuilt, never run.
    """
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_LOCATOR_KEY_CHARS:
        raise EvidenceContractError("EVIDENCE_LOCATOR_INVALID")
    try:
        parsed = json.loads(value, parse_constant=_reject_constant)
    except (TypeError, ValueError, RecursionError):
        raise EvidenceContractError("EVIDENCE_LOCATOR_INVALID") from None
    if not isinstance(parsed, list) or len(parsed) != 6:
        raise EvidenceContractError("EVIDENCE_LOCATOR_INVALID")
    kind, record_id, field_path, section, char_start, char_end = parsed
    if not isinstance(kind, str) or kind not in LOCATOR_KINDS:
        raise EvidenceContractError("EVIDENCE_LOCATOR_INVALID")
    if not isinstance(record_id, str) or not isinstance(field_path, list) \
            or any(not isinstance(segment, str) for segment in field_path):
        raise EvidenceContractError("EVIDENCE_LOCATOR_INVALID")
    if section is not None and not isinstance(section, str):
        raise EvidenceContractError("EVIDENCE_LOCATOR_INVALID")
    for offset in (char_start, char_end):
        if offset is not None and (isinstance(offset, bool) or not isinstance(offset, int)):
            raise EvidenceContractError("EVIDENCE_LOCATOR_INVALID")
    locator = EvidenceLocator(kind=kind, record_id=record_id,  # type: ignore[arg-type]
                              field_path=tuple(field_path), section=section,
                              char_start=char_start, char_end=char_end)
    if locator.locator_key != value:
        raise EvidenceContractError("EVIDENCE_LOCATOR_INVALID")
    return locator


def fragment_type_for(locator: EvidenceLocator) -> str:
    """The one fragment type a locator of this kind may carry."""
    return FRAGMENT_TYPE_BY_LOCATOR_KIND[locator.kind]


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
        if FRAGMENT_TYPE_BY_LOCATOR_KIND[self.locator.kind] != self.fragment_type:
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
    # R4: the closed identity dimensions the record itself stated.  The
    # trusted mapper copies them from declared fields of the operation it is
    # registered for; it never invents one, and a dimension outside the closed
    # vocabulary fails closed rather than being carried as free-form metadata.
    # Empty is the honest default for a record that qualifies itself no
    # further, and it never widens a later comparison: an empty identity only
    # ever matches another empty identity.
    identity: dict[str, str] = Field(default_factory=dict)
    locator: EvidenceLocator

    @model_validator(mode="after")
    def _shape(self) -> "StructuredEvidenceFact":
        _bounded_json(self.value, MAX_FACT_VALUE_JSON_BYTES)
        if not set(self.identity) <= set(IDENTITY_DIMENSIONS):
            raise EvidenceContractError("EVIDENCE_FACT_IDENTITY_INVALID")
        if any(not isinstance(item, str) or not item.strip()
               or len(item) > MAX_IDENTITY_DIMENSION_CHARS
               for item in self.identity.values()):
            raise EvidenceContractError("EVIDENCE_FACT_IDENTITY_INVALID")
        if isinstance(self.value, bool) or not isinstance(self.value, (int, float)):
            pass
        elif self.unit is None:
            raise EvidenceContractError("EVIDENCE_FACT_UNIT_REQUIRED")
        if self.unit is not None and not _UNIT_PATTERN.fullmatch(self.unit):
            raise EvidenceContractError("EVIDENCE_VALUE_INVALID")
        if len(self.time_scope) > MAX_TIME_SCOPE_KEYS:
            raise EvidenceContractError("EVIDENCE_LIMIT_EXCEEDED")
        _bounded_json(self.time_scope, MAX_FACT_VALUE_JSON_BYTES)
        # What was validated is what stays: the nested value and scope are
        # deep-frozen so no caller can deepen, widen or replace them after
        # the bounds above were checked.  `frozen=True` alone only guards the
        # attributes of this model, not the containers inside them.
        object.__setattr__(self, "value", _freeze(self.value))
        object.__setattr__(self, "time_scope", _freeze(self.time_scope))
        object.__setattr__(self, "identity", _freeze(self.identity))
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
        # A located fact is only a fact because focused evidence at that exact
        # location supports it: every fact locator must be one of this
        # bundle's own fragment locators.  The guarded claim RPC enforces the
        # same rule against the durable fragments of the same source.
        located = {item.locator.locator_key for item in self.fragments}
        if any(item.locator.locator_key not in located for item in self.facts):
            raise EvidenceContractError("EVIDENCE_LOCATOR_OUT_OF_SCOPE")
        return self


def build_evidence_bundle(*, source: VersionedEvidenceSource, locator_scope: Iterable[str],
                          facts: Iterable[StructuredEvidenceFact],
                          fragments: Iterable[FocusedEvidenceFragment]) -> EvidenceBundle:
    """Assemble a bundle from trusted mapping output."""
    return EvidenceBundle(source=source, locator_scope=tuple(locator_scope),
                          facts=tuple(facts), fragments=tuple(fragments))


def _copy_locator(locator: EvidenceLocator) -> EvidenceLocator:
    return EvidenceLocator(kind=locator.kind, record_id=locator.record_id,
                           field_path=tuple(locator.field_path), section=locator.section,
                           char_start=locator.char_start, char_end=locator.char_end)


def revalidate_evidence_bundle(bundle: Any) -> EvidenceBundle:
    """Rebuild a bundle from a copy of its own data so EVERY validator runs again.

    Called at the two trust boundaries a bundle crosses -- immediately after
    the mapper returns it and immediately before the first durable write --
    so a bundle assembled around validation (`model_construct`, a subclass, a
    shared reference mutated after construction) is caught before it can
    cause any write, including a partially persisted source.  Every nested
    value is thawed into a fresh copy first, so the rebuilt contract shares no
    container with the input and cannot be changed underneath it afterwards.
    """
    if not isinstance(bundle, EvidenceBundle):
        raise EvidenceContractError("EVIDENCE_CONTRACT_INVALID")
    try:
        descriptor, version = bundle.source, bundle.source.version
        source = VersionedEvidenceSource(
            agent=descriptor.agent, url=descriptor.url, title=descriptor.title,
            domain=descriptor.domain, source_type=descriptor.source_type,
            source_strength=descriptor.source_strength, source_date=descriptor.source_date,
            query=descriptor.query, tool_operation=descriptor.tool_operation,
            version=SourceVersion(kind=version.kind, identifier=version.identifier),
            confidence=descriptor.confidence)
        facts = tuple(StructuredEvidenceFact(
            entity_key=item.entity_key, field_key=item.field_key, value=_thaw(item.value),
            unit=item.unit, time_scope=_thaw(item.time_scope), geography=item.geography,
            market=item.market, identity=_thaw(item.identity),
            locator=_copy_locator(item.locator)) for item in bundle.facts)
        fragments = tuple(FocusedEvidenceFragment(
            fragment_type=item.fragment_type, text=item.text,
            locator=_copy_locator(item.locator), fragment_index=item.fragment_index,
            content_hash=item.content_hash) for item in bundle.fragments)
        return EvidenceBundle(source=source, locator_scope=tuple(bundle.locator_scope),
                              facts=facts, fragments=fragments)
    except EvidenceContractError:
        raise
    except Exception:
        # `from None`: an attribute/type error from a forged bundle can quote
        # its contents.
        raise EvidenceContractError("EVIDENCE_CONTRACT_INVALID") from None


__all__ = [
    "EVIDENCE_CONTRACT_REASONS", "FRAGMENT_TYPES", "FRAGMENT_TYPE_BY_LOCATOR_KIND",
    "IDENTITY_DIMENSIONS", "LOCATOR_KINDS", "MAX_IDENTITY_DIMENSION_CHARS",
    "MAX_DOCUMENT_OFFSET", "MAX_FACTS_PER_BUNDLE", "MAX_FACT_COLLECTION_ITEMS",
    "MAX_FACT_VALUE_DEPTH", "MAX_FACT_VALUE_JSON_BYTES", "MAX_LOCATOR_KEY_CHARS",
    "MAX_LOCATOR_PATH_SEGMENTS", "MAX_LOCATOR_RECORD_ID_CHARS",
    "MAX_LOCATOR_SCOPE_IDS", "MAX_LOCATOR_SECTION_CHARS", "MAX_LOCATOR_SEGMENT_CHARS",
    "MAX_PROJECTION_FIELDS", "MAX_SOURCE_VERSION_CHARS", "MAX_TIME_SCOPE_KEYS",
    "MAX_SOURCE_VERSION_KEY_CHARS", "MAX_TOOL_SNAPSHOT_JSON_BYTES",
    "MAX_UNIT_CHARS", "SOURCE_VERSION_KINDS",
    "BoundedEvidenceContract", "EvidenceBundle", "EvidenceContractError",
    "EvidenceLocator", "FocusedEvidenceFragment", "FrozenDict", "SourceVersion",
    "StructuredEvidenceFact", "VersionedEvidenceSource",
    "build_evidence_bundle", "canonical_json", "canonical_projection",
    "document_span_locator", "fragment_type_for", "parse_locator_key",
    "parse_version_key", "read_locator_path", "record_field_locator",
    "revalidate_evidence_bundle", "snapshot_version", "structured_projection",
    "verbatim_excerpt",
]

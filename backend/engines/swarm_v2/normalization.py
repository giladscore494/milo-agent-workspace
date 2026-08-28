"""One deterministic canonical scope identity for Swarm V2 conflict grouping.

Normalization here is formatting, not identity resolution: unicode NFKC,
casefold, whitespace trim/collapse, and unambiguous separator unification
(whitespace, underscore, hyphen).  Semantic aliases (Accent vs i25, Tucson vs
Tucson New, Israel vs Global) are never merged; that belongs to a later
explicit reconciliation layer.  Original evidence values are never rewritten —
canonical values exist only for scope equality, conflict grouping and
deterministic dedup.

Pure module: no database access, provider calls, tool calls, global mutable
state, or vehicle catalog data.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Any, Mapping, NamedTuple

SCOPE_NORMALIZATION_VERSION = 1

_SEPARATORS = str.maketrans({"_": " ", "-": " "})


def _normalize_text(value: str) -> str:
    folded = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(folded.translate(_SEPARATORS).split())


def normalize_entity_key(value: str) -> str:
    return _normalize_text(value)


def normalize_field_key(value: str) -> str:
    return _normalize_text(value)


def normalize_geography_key(value: str | None) -> str | None:
    return None if value is None else _normalize_text(value)


def normalize_market_key(value: str | None) -> str | None:
    return None if value is None else _normalize_text(value)


def normalize_time_scope(value: Mapping[str, Any] | None) -> str:
    """Canonical JSON: key insertion order never matters; 2020 != 2021."""
    return canonical_value_key(dict(value or {}))


def canonical_value_key(value: Any) -> str:
    """Deterministic JSON identity for claim values; no fuzzy equality."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


class CanonicalScope(NamedTuple):
    """The single evidence-scope contract; never re-create this shape elsewhere."""

    entity: str
    field: str
    geography: str | None
    market: str | None
    time_scope: str


def canonical_scope_key(*, entity: str, field: str, geography: str | None = None,
                        market: str | None = None,
                        time_scope: Mapping[str, Any] | None = None) -> CanonicalScope:
    return CanonicalScope(entity=normalize_entity_key(entity),
                          field=normalize_field_key(field),
                          geography=normalize_geography_key(geography),
                          market=normalize_market_key(market),
                          time_scope=normalize_time_scope(time_scope))


def canonical_scope_hash(scope: CanonicalScope) -> str:
    """Bounded durable identity of a canonical scope: SHA-256 over its
    deterministic serialization.  PostgreSQL validates equality of this
    backend-computed value instead of re-implementing normalization."""
    encoded = json.dumps(list(scope), separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


__all__ = ["SCOPE_NORMALIZATION_VERSION", "CanonicalScope", "canonical_scope_hash",
           "canonical_scope_key", "canonical_value_key", "normalize_entity_key",
           "normalize_field_key", "normalize_geography_key", "normalize_market_key",
           "normalize_time_scope"]

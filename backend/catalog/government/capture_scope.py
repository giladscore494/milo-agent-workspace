"""A SCOPED Government capture: one register marque, declared durably.

What this is for
----------------

Scoped catalog PR2 prepares a Mapping Plan one manufacturer at a time. Each
unit is a bounded capture of the WLTP resource filtered to ONE register marque
(`filters={"tozar": "<the register's own spelling>"}`) and lands as an
ordinary immutable snapshot through the existing ingestion path. What it must
never become is "the catalog": a snapshot holding one marque's rows is not the
register, and a reader that took it for the register would conclude that every
other marque had vanished.

So a scoped capture DECLARES itself. Its snapshot's `retrieval_metadata`
carries a `capture_scope` object, written by the capture that took it and held
consistent with the snapshot's own recorded query by a database CHECK
(`catalog_capture_scope_consistent`, migration 20260923000100):

    {"contract": "gov.capture_scope.1",
     "filters": {"tozar": "טויוטה"},
     "scope_key": sha256(<the exact filters text the capture sent>)}

and every reader treats a declaration as a hard boundary:

*   an UNPINNED read -- "the newest usable snapshot" -- never answers from a
    declared scope. The repository excludes them in the database, so a run of
    per-manufacturer snapshots can never push the register out of the bounded
    newest-first listing either;
*   a SCOPED read answers only from a snapshot declaring exactly that scope;
*   a refresh compares a scope only with the same scope, so a scoped capture is
    never "unchanged" because the register is, and never diffs against it.

A snapshot that declares nothing reads exactly as it always has. That is
deliberate: the only production capture before this was the whole resource,
and every existing reader keeps its behaviour.

Why the key is a digest of the FILTERS TEXT
-------------------------------------------

The capture sends `filters` as one canonical JSON text (`client.
_canonical_filters`), every page must echo it, and the snapshot records it as
`query.filters`. Keying the scope on the SHA-256 of that exact text lets the
database re-derive the key from the query it already stores, so a declaration
cannot name one scope while the capture read another.

Pure module: constants, one dataclass and strict parsers. No I/O.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping

#: The declaration's own contract version, pinned in SQL as well.
CAPTURE_SCOPE_CONTRACT = "gov.capture_scope.1"
#: Where the declaration lives inside a snapshot's `retrieval_metadata`.
METADATA_KEY = "capture_scope"
#: The register fields a scope may filter on. Closed: the marque, and nothing
#: else. A plan's model years are applied when its queue is materialized, over
#: the scoped snapshot's candidates; they are never a capture filter, because
#: the register's CKAN datastore filters by equality only.
SCOPE_FILTER_FIELDS: frozenset[str] = frozenset({"tozar"})
#: The durable bound on one filter value -- the candidate manufacturer column's.
MAX_SCOPE_VALUE_CHARS = 120

_SCOPE_KEY = re.compile(r"^[0-9a-f]{64}$")
_DECLARATION_KEYS = frozenset({"contract", "filters", "scope_key"})

CAPTURE_SCOPE_REASONS: Mapping[str, str] = {
    "GOV_CAPTURE_SCOPE_INVALID":
        "a government capture scope is not a well-formed register-marque scope",
}


class CaptureScopeError(ValueError):
    """A refused scope. Carries ONLY a static, code-owned reason."""

    def __init__(self, reason_code: str = "GOV_CAPTURE_SCOPE_INVALID") -> None:
        if reason_code not in CAPTURE_SCOPE_REASONS:
            raise ValueError("capture scope reason must come from the static allowlist")
        self.reason_code = reason_code
        self.safe_message = CAPTURE_SCOPE_REASONS[reason_code]
        super().__init__(self.safe_message)


def _filter_value(value: Any) -> str:
    """One register value, exactly as the register writes it, or a refusal.

    Never normalized, trimmed or case-folded: the register filters by exact
    text, and a "tidied" spelling is a different filter. Refused instead when
    it could not be that text -- empty, padded, over-long, or carrying a
    control or format character that no register spelling contains.
    """
    if not isinstance(value, str) or not value or value != value.strip():
        raise CaptureScopeError()
    if len(value) > MAX_SCOPE_VALUE_CHARS:
        raise CaptureScopeError()
    if any(unicodedata.category(char) in ("Cc", "Cf") for char in value):
        raise CaptureScopeError()
    return value


@dataclass(frozen=True)
class CaptureScope:
    """The register rows ONE scoped capture reads. Immutable and hashable."""

    filters: tuple[tuple[str, str], ...]

    @classmethod
    def from_filters(cls, filters: Any) -> "CaptureScope":
        """A scope from a filters object, validated field by field."""
        if not isinstance(filters, Mapping) or not filters:
            raise CaptureScopeError()
        pairs: list[tuple[str, str]] = []
        for name, value in filters.items():
            if not isinstance(name, str) or name not in SCOPE_FILTER_FIELDS:
                raise CaptureScopeError()
            pairs.append((name, _filter_value(value)))
        return cls(filters=tuple(sorted(pairs)))

    @classmethod
    def for_register_marque(cls, marque: str) -> "CaptureScope":
        """The scope of one marque, by the register's own `tozar` spelling."""
        return cls.from_filters({"tozar": marque})

    def filters_mapping(self) -> dict[str, str]:
        return dict(self.filters)

    def filters_text(self) -> str:
        """The exact `filters` text the capture sends and every page echoes.

        Identical by construction to `client._canonical_filters` over the same
        object (a test holds the two together): sorted keys, compact
        separators, and the register's own characters rather than escapes.
        """
        return json.dumps(self.filters_mapping(), sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False)

    def query(self) -> dict[str, str]:
        """The non-paging query this scope captures with. `filters` only."""
        return {"filters": self.filters_text()}

    def key(self) -> str:
        """The scope's identity: SHA-256 of `filters_text()`, lowercase hex."""
        return hashlib.sha256(self.filters_text().encode("utf-8")).hexdigest()

    def as_metadata(self) -> dict[str, Any]:
        """The declaration a scoped snapshot's `retrieval_metadata` carries."""
        return {"contract": CAPTURE_SCOPE_CONTRACT, "filters": self.filters_mapping(),
                "scope_key": self.key()}


def declared_scope(snapshot: Mapping[str, Any]) -> CaptureScope | None:
    """The scope a snapshot DECLARES, parsed strictly, or None if it declares none.

    A declaration that is present but malformed is a refusal, never "no
    scope": treating an unreadable declaration as the register would be the
    exact confusion the declaration exists to prevent.
    """
    metadata = snapshot.get("retrieval_metadata") if isinstance(snapshot, Mapping) else None
    if not isinstance(metadata, Mapping) or METADATA_KEY not in metadata:
        return None
    declaration = metadata[METADATA_KEY]
    if declaration is None:
        return None
    if not isinstance(declaration, Mapping) or set(declaration) != _DECLARATION_KEYS:
        raise CaptureScopeError()
    if declaration.get("contract") != CAPTURE_SCOPE_CONTRACT:
        raise CaptureScopeError()
    scope = CaptureScope.from_filters(declaration.get("filters"))
    stated_key = declaration.get("scope_key")
    if not isinstance(stated_key, str) or not _SCOPE_KEY.fullmatch(stated_key) \
            or stated_key != scope.key():
        raise CaptureScopeError()
    # The declaration must describe the query the snapshot actually recorded.
    query = metadata.get("query")
    if not isinstance(query, Mapping) or dict(query) != scope.query():
        raise CaptureScopeError()
    return scope


def is_scoped(snapshot: Mapping[str, Any]) -> bool:
    """Whether a snapshot may NOT answer an unpinned read.

    True for a declared scope and -- failing closed -- for a declaration that
    cannot be parsed.
    """
    try:
        return declared_scope(snapshot) is not None
    except CaptureScopeError:
        return True


__all__ = ["CAPTURE_SCOPE_CONTRACT", "CAPTURE_SCOPE_REASONS", "METADATA_KEY",
           "MAX_SCOPE_VALUE_CHARS", "SCOPE_FILTER_FIELDS", "CaptureScope",
           "CaptureScopeError", "declared_scope", "is_scoped"]

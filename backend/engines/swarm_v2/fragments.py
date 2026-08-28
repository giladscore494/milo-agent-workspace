"""Bounded evidence-fragment acquisition from real tool material.

A fragment is a small verbatim slice of text that a TOOL actually returned in
association with a source.  It exists so a later grounded verifier can read
the evidence that existed during research without re-fetching a page and
without trusting model memory.

Pure module: no database access, no provider calls, no network access, no
tool execution, no global mutable state.  Everything here is a deterministic
function of its input, so an exact replay yields an identical fragment list.

Two rules define this boundary and must never be relaxed:

1.  Fragment text originates from tool/source material only.  Nothing here
    accepts, requests, or reconstructs a model completion; B2 deliberately
    has no path that asks a worker to "write the excerpt that supports your
    answer" (that would make an LLM the evidence for its own claim).
2.  Text is bounded BEFORE it can reach durable storage.  Complete pages,
    search-result dumps, provider response bodies and raw HTML are never
    persistable through this contract.
"""

from __future__ import annotations

import hashlib
from typing import Any, Mapping

# Hard durable limits.  Small enough that a full page can never be stored,
# large enough to keep one useful factual sentence/paragraph per fragment.
# supabase/migrations/20260828000200_source_evidence_fragments.sql repeats
# these three numbers as SQL literals; tests/test_evidence_migration_static.py
# proves the two definitions can never drift apart.
MAX_FRAGMENT_CHARS = 400
MAX_FRAGMENTS_PER_SOURCE = 4
MAX_FRAGMENT_TOTAL_CHARS_PER_SOURCE = 1200

# Tool-result keys that may carry source-associated text, in the fixed order
# they are read.  `rows` is the ONLY key the current registry can produce
# (backend/tools/mock.py `_ROWS`); the rest are reserved names for tools a
# later PR registers.  A key that is absent contributes nothing: no metadata
# is ever inferred, defaulted, or invented when a tool does not supply it.
TOOL_TEXT_FIELDS: tuple[str, ...] = ("snippet", "excerpt", "text", "content", "rows")

# Truncation never cuts a fragment below this fraction of the bound, so a
# passage with no early whitespace still keeps a usable slice.
_MIN_BOUNDARY = MAX_FRAGMENT_CHARS // 2


class FragmentExtractionError(ValueError):
    """A safe, provider-neutral fragment acquisition failure."""


def normalize_fragment_text(value: str) -> str:
    """Collapse whitespace and drop non-printable control characters.

    Deliberately NOT unicode-folded or case-folded: evidence must stay as
    close to the source's own characters as possible.  This only removes what
    could never carry meaning in a durable quote.
    """
    kept = "".join(character if character.isprintable()
                   else (" " if character.isspace() else "")
                   for character in value)
    return " ".join(kept.split())


def bound_fragment_text(value: str) -> str:
    """Deterministically bound one candidate to MAX_FRAGMENT_CHARS.

    Truncation prefers the last whitespace boundary inside the window so a
    stored quote ends on a whole word.  No marker is appended: durable text
    stays a verbatim prefix of the tool's own text.
    """
    text = normalize_fragment_text(value)
    if len(text) <= MAX_FRAGMENT_CHARS:
        return text
    window = text[:MAX_FRAGMENT_CHARS]
    boundary = window.rfind(" ")
    return (window[:boundary] if boundary >= _MIN_BOUNDARY else window).rstrip()


def fragment_content_hash(text: str) -> str:
    """Stable SHA-256 identity of the final bounded text.

    Used for idempotency, traceability and exact deduplication only.  It is
    not a similarity measure: two fragments whose text differs at all stay
    separate rows, and no embedding is ever computed.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _candidate_texts(value: Any) -> list[str]:
    """Only genuine strings the tool returned; nothing is coerced to text."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [item for item in value if isinstance(item, str)]
    return []


def extract_source_fragments(tool_result: Mapping[str, Any]) -> list[str]:
    """Return the bounded fragments a tool result supports, in tool order.

    `tool_result` MUST be the structured result a registered tool returned
    for the source being persisted -- never a worker/model completion.  The
    result is capped by count and by total characters, and exact duplicates
    are collapsed by content hash so a repeated snippet is stored once.
    """
    if not isinstance(tool_result, Mapping):
        raise FragmentExtractionError("evidence fragments require a structured tool result")
    fragments: list[str] = []
    seen: set[str] = set()
    total = 0
    for field in TOOL_TEXT_FIELDS:
        if field not in tool_result:
            continue
        for candidate in _candidate_texts(tool_result[field]):
            if len(fragments) >= MAX_FRAGMENTS_PER_SOURCE:
                return fragments
            text = bound_fragment_text(candidate)
            # A candidate that does not fit the remaining character budget is
            # skipped, never trimmed further: a later shorter fragment may
            # still fit, and the outcome stays a pure function of the input.
            if not text or fragment_content_hash(text) in seen:
                continue
            if total + len(text) > MAX_FRAGMENT_TOTAL_CHARS_PER_SOURCE:
                continue
            seen.add(fragment_content_hash(text))
            fragments.append(text)
            total += len(text)
    return fragments


__all__ = ["MAX_FRAGMENTS_PER_SOURCE", "MAX_FRAGMENT_CHARS",
           "MAX_FRAGMENT_TOTAL_CHARS_PER_SOURCE", "TOOL_TEXT_FIELDS",
           "FragmentExtractionError", "bound_fragment_text", "extract_source_fragments",
           "fragment_content_hash", "normalize_fragment_text"]

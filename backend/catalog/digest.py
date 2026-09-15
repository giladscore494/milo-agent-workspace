"""The raw-record payload digest — deliberately STORAGE-LOCAL, not portable.

What this is, and what it is not
--------------------------------

Each backend derives this digest over ITS OWN stored representation of a
payload:

*   PostgreSQL stores `jsonb` and derives
    `encode(sha256(convert_to(payload::text, 'UTF8')), 'hex')`. `jsonb`
    normalizes on the way in -- object keys are reordered by (key length, then
    bytes) and rendered with `", "` and `": "` separators -- so the digest is a
    function of the value PostgreSQL stores, not of the text anyone sent.
*   The in-memory repository stores a Python object and derives the digest over
    the CANONICAL form below: keys sorted bytewise, compact separators.

**These two renderings are different on purpose, and the digests therefore
differ for any object with more than one key.** Nothing in this repository
claims they are equal, and `tests/test_migrations_postgres.py` asserts the
inequality so the claim cannot quietly reappear.

Reproducing PostgreSQL's rendering in Python would mean reproducing its key
ordering, its numeric normalization and its escaping rules -- a coupling to one
database's internals of exactly the kind this corrective round removed when it
stopped requiring callers to predict `jsonb::text`. A digest that is honestly
local to its storage is safer than a digest that is portable until the day it
silently is not.

What IS guaranteed, in both backends
------------------------------------

The digest is a function of the payload's VALUE, never of the order its keys
happened to arrive in. `{"a": 1, "b": 2}` and `{"b": 2, "a": 1}` are one value,
at any nesting depth, so within either backend they produce one digest, collapse
onto one row on replay, and raise the same idempotency conflict against a
genuinely different payload. That behavioural parity is what replay depends on,
and it is proven executably against both backends rather than asserted here.

The digest is an identity and deduplication device only; no similarity or
embedding is involved anywhere in this path.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

#: How the in-memory backend renders a payload before hashing it. Sorted keys
#: make the result independent of insertion order at every depth; the compact
#: separators make it VISIBLY not PostgreSQL's rendering, so the two are never
#: mistaken for one canonical form.
_CANONICAL_SEPARATORS = (",", ":")


def canonical_payload_text(payload: Mapping[str, Any]) -> str:
    """The memory backend's canonical rendering of one payload.

    `sort_keys` applies recursively, so nested objects are canonical too.
    `ensure_ascii=False` keeps source text as the source wrote it rather than
    escaping it into a different string.
    """
    return json.dumps(payload, sort_keys=True, separators=_CANONICAL_SEPARATORS,
                      ensure_ascii=False)


def catalog_payload_digest(payload: Mapping[str, Any]) -> str:
    """The SHA-256 of one stored raw-record payload, lowercase hex.

    Storage-local to the in-memory backend. Equal payloads -- in any key order,
    at any depth -- give equal digests; different payloads give different ones.
    It is NOT equal to the digest PostgreSQL stores for the same payload.
    """
    return hashlib.sha256(canonical_payload_text(payload).encode("utf-8")).hexdigest()


__all__ = ["canonical_payload_text", "catalog_payload_digest"]

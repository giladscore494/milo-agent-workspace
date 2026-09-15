"""The raw-record payload digest, derived the way PostgreSQL derives it.

The database computes `encode(sha256(convert_to(payload::text, 'UTF8')), 'hex')`
over the jsonb it actually stores. `jsonb` normalizes as it stores -- object
keys are reordered and whitespace is dropped -- so the digest is a function of
the STORED value, not of whatever text a caller happened to send.

This helper exists so the in-memory repository derives the same value for the
same payload, and so nothing in the backend has to predict that rendering in
order to WRITE a record: the field is refused on input everywhere, and the
digest is produced on the storage side. It is an identity and deduplication
device only; no similarity or embedding is involved.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


def catalog_payload_digest(payload: Mapping[str, Any]) -> str:
    """The SHA-256 of one stored raw-record payload, lowercase hex.

    PostgreSQL renders jsonb with `", "` between pairs and `": "` after a key,
    and preserves the insertion order it normalized to. Reproducing that here
    keeps the memory repository's stored digest equal to the database's for the
    payloads the catalog accepts -- flat objects of scalars, which is what a
    bounded upstream record is.
    """
    return hashlib.sha256(
        json.dumps(payload, separators=(", ", ": "), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


__all__ = ["catalog_payload_digest"]

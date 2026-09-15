"""One rule for what a catalog write payload must be, shared by both repositories.

Catalog PR1 let the caller name every durable object and hand in the payload
digest it wanted stored. Both are now DERIVED here, in one place, so the
Supabase repository and the in-memory repository cannot disagree about what a
valid payload is -- a unit test that passes against the memory implementation
is then a test of the same rule PostgreSQL applies, not of a looser one.

What each preparer does, and why
--------------------------------

*   It DERIVES the object's identity key from the object's own structural
    fields (`backend/catalog/keys.py`). A caller MAY state the key and is then
    held to it: a key that disagrees with the derivation is refused rather than
    accepted or silently replaced, because a caller that computed a different
    key believes it is writing a different object.
*   It REFUSES a supplied `payload_sha256`. The database derives that digest
    from the bytes it actually stores; PR1's comparison against a caller's hash
    made every ingestion path responsible for reproducing PostgreSQL's
    `jsonb::text` key ordering and separator style, which is a formatting
    coincidence rather than an integrity property.
*   It strips the PARENT KEY fields, which exist only so a child's key can be
    derived from its parent's identity rather than from a surrogate id. They
    are not columns and are never sent to the database.

Pure module: dict manipulation and hashing. No I/O, no clock, no randomness.
"""

from __future__ import annotations

from typing import Any, Mapping

from . import keys
from .contracts import stated_source_locator
from .keys import CatalogKeyError

#: The structural identity of a snapshot: what makes one retrieval that
#: retrieval and not another.
SNAPSHOT_IDENTITY = ("source_family", "resource_id", "upstream_version_kind",
                     "upstream_version", "content_sha256")

#: The identity fields of a candidate reading, beyond its parent record.
CANDIDATE_IDENTITY = ("manufacturer", "commercial_model", "model_year_start",
                      "model_year_end", "official_model_code", "trim",
                      "identity_dimensions")

#: Fields a caller supplies purely so a child's key can be derived from its
#: parent's identity. They are stripped before the payload reaches a database.
PARENT_KEY_FIELDS = ("snapshot_key", "record_key", "candidate_key")


class CatalogPayloadError(ValueError):
    """A payload that cannot be written, with a static, safe reason."""


def _require(payload: Mapping[str, Any], *fields: str) -> dict[str, Any]:
    missing = [field for field in fields
               if payload.get(field) is None
               or (isinstance(payload.get(field), str) and not payload[field].strip())]
    if missing:
        raise CatalogPayloadError("catalog payload is missing required identity fields")
    return {field: payload[field] for field in fields}


def _settle_key(payload: Mapping[str, Any], field: str, derived: str) -> dict[str, Any]:
    """Return the payload carrying the derived key, or refuse a conflicting one."""
    supplied = payload.get(field)
    if supplied is not None and supplied != derived:
        raise CatalogKeyError(f"catalog {field} does not match its trusted derivation")
    prepared = {name: value for name, value in payload.items()
                if name not in PARENT_KEY_FIELDS or name == field}
    prepared[field] = derived
    return prepared


def prepare_snapshot(payload: Mapping[str, Any]) -> dict[str, Any]:
    """One immutable retrieval, keyed by what it retrieved."""
    identity = _require(payload, *SNAPSHOT_IDENTITY)
    _require(payload, "retrieved_at")
    if payload.get("declared_record_count") is None:
        raise CatalogPayloadError("catalog payload is missing required identity fields")
    # Activation is a separate, evidenced decision; it is never an input.
    for field in ("activated_at", "stored_record_count"):
        if field in payload:
            raise CatalogPayloadError("catalog snapshot activation is not a caller-supplied field")
    return _settle_key(payload, "snapshot_key", keys.snapshot_key(**identity))


def prepare_raw_record(payload: Mapping[str, Any]) -> dict[str, Any]:
    """One upstream row, keyed inside the snapshot that captured it."""
    parent = _require(payload, "snapshot_key", "upstream_record_id")
    _require(payload, "snapshot_id", "resource_id")
    if not isinstance(payload.get("payload"), Mapping):
        raise CatalogPayloadError("a catalog raw record payload must be an object")
    if "payload_sha256" in payload:
        raise CatalogPayloadError("catalog raw record payload digest is derived, not supplied")
    # The capture position, when the retrieval had one. Validated here rather
    # than at the database boundary so an unknown field or a non-position value
    # is a local refusal with a readable reason, and so both repositories apply
    # exactly this rule.
    if payload.get("source_locator") is not None:
        try:
            stated_source_locator(payload["source_locator"])
        except ValueError as failure:
            raise CatalogPayloadError(str(failure)) from None
    return _settle_key(payload, "record_key", keys.raw_record_key(**parent))


def prepare_candidate(payload: Mapping[str, Any]) -> dict[str, Any]:
    """One reading of one raw record, keyed by the identity it reads."""
    _require(payload, "record_key", "snapshot_id", "raw_record_id",
             "manufacturer", "commercial_model")
    identity = {field: payload.get(field) for field in CANDIDATE_IDENTITY}
    identity["identity_dimensions"] = dict(identity.get("identity_dimensions") or {})
    if (identity["model_year_start"] is None) != (identity["model_year_end"] is None):
        raise CatalogPayloadError("a catalog model year range must be whole")
    derived = keys.candidate_key(record_key=payload["record_key"], **identity)
    return _settle_key(payload, "candidate_key", derived)


def prepare_evidence_link(payload: Mapping[str, Any]) -> dict[str, Any]:
    """One citation of one claim for one candidate.

    The locator and the source version are deliberately NOT accepted here:
    they are derived from the cited claim and source by the persistence layer,
    which is the whole point of the correction. A caller that states them is
    passing an assertion the database will check, so they are left in the
    payload untouched rather than stripped.
    """
    _require(payload, "candidate_key", "candidate_id", "source_id", "claim_id")
    derived = keys.evidence_link_key(
        candidate_key=payload["candidate_key"], source_id=payload["source_id"],
        claim_id=payload["claim_id"], verdict_id=payload.get("verdict_id"))
    return _settle_key(payload, "link_key", derived)


__all__ = ["CANDIDATE_IDENTITY", "PARENT_KEY_FIELDS", "SNAPSHOT_IDENTITY",
           "CatalogPayloadError", "prepare_candidate", "prepare_evidence_link",
           "prepare_raw_record", "prepare_snapshot"]

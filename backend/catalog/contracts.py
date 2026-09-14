"""The closed vocabularies and hard bounds of the durable catalog namespace.

ONE definition of each rule. The guarded RPCs and the table constraints in
`supabase/migrations/20260914200000_catalog_evidence_foundation.sql` apply
exactly these values, and `tests/test_catalog_migration_static.py` proves the
two copies cannot drift apart -- the same device migration
`20260828000200_source_evidence_fragments.sql` and
`backend/engines/swarm_v2/fragments.py` already use for the evidence bounds.

Nothing here reads a file, opens a connection, or fetches anything. These are
constants plus two pure predicates.
"""

from __future__ import annotations

from typing import Any, Mapping

#: Where a snapshot came from. Closed: a family this tuple does not name has no
#: reviewed trust posture, so it cannot be persisted at all.
#:
#: `legacy_reference` is the existing aggregated Yeda catalog. It is a
#: DISCOVERY aid -- candidate identities, aliases and comparison -- and is
#: never evidence. It is deliberately a source FAMILY here rather than a
#: parallel set of tables, so every rule that already governs a snapshot (the
#: activation gate, the append-only raw records, the idempotency identity)
#: governs it too, and there is exactly one place a reviewer has to look to see
#: what the catalog trusts.
CATALOG_SOURCE_FAMILIES = ("government", "manufacturer", "legacy_reference")

#: What the catalog is entitled to conclude from a snapshot's records.
#:
#: `evidence` -- the snapshot is a primary source that may support a verified
#: fact. `unverified` -- the snapshot may suggest a candidate to look for and
#: nothing else: it can never carry a verdict, can never verify a fact, and can
#: never override a Government or manufacturer statement.
CATALOG_TRUST_STATES = ("evidence", "unverified")

#: The trust state each family is PINNED to. Not a default and not a column the
#: caller chooses: the legacy catalog cannot be promoted to evidence by writing
#: a different value, because this mapping is a CHECK constraint in the
#: database. Changing it takes a reviewed migration, which is the point.
TRUST_STATE_BY_FAMILY: Mapping[str, str] = {
    "government": "evidence",
    "manufacturer": "evidence",
    "legacy_reference": "unverified",
}

#: A snapshot's completeness state. A snapshot is usable only once it is
#: `complete`; `pending` is still being appended to and `failed` is a capture
#: that did not establish what it claimed.
SNAPSHOT_VALIDATION_STATES = ("pending", "complete", "failed")

#: What a candidate identity may be. `ambiguous` is a first-class ANSWER, not a
#: staging state: a source that states two identities for one vehicle has said
#: something true, and forcing a resolution here would invent the fact the
#: source declined to state.
CANDIDATE_STATUSES = ("candidate", "ambiguous", "rejected", "ready_for_review")

#: The identity dimensions a candidate may carry BEYOND the four it always
#: states (manufacturer, commercial model, model year range, and -- when the
#: source states them -- official model code and trim). Closed: a dimension
#: this tuple does not name cannot be stored, so a new one takes a reviewed
#: migration instead of appearing in a payload.
#:
#: Every dimension is optional and is recorded ONLY when the source states it.
#: There is no "unknown" value and no empty string: an absent dimension is an
#: absent key, which is what keeps a guess from looking like a statement.
CANDIDATE_IDENTITY_DIMENSIONS = ("body_style", "drivetrain", "engine_code",
                                 "fuel_type", "generation", "market",
                                 "propulsion_technology", "transmission")

#: The durable bound on one stored raw record. A datastore row of the kind R5
#: pinned is 1-2 KB; this leaves generous headroom while making "store the
#: whole dataset in one row" impossible. Enforced in the database too, so no
#: backend release and no direct RPC call can exceed it.
MAX_RAW_PAYLOAD_CHARS = 16384

#: The durable bound on a snapshot's retrieval metadata (request/response
#: provenance). Metadata describes a retrieval; it is never a place to park
#: source content.
MAX_RETRIEVAL_METADATA_CHARS = 4096

#: The shape every idempotency identity in this namespace must have. Bounded
#: and ASCII so it is safe to compare, index and log; never derived from model
#: output.
IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$"

#: A SHA-256 content identity, lowercase hex.
CONTENT_SHA256_PATTERN = r"^[0-9a-f]{64}$"


def trust_state_for(source_family: str) -> str:
    """The pinned trust state of a family, or fail closed on an unknown one."""
    try:
        return TRUST_STATE_BY_FAMILY[source_family]
    except KeyError:
        raise ValueError("unknown catalog source family") from None


def is_evidence_family(source_family: str) -> bool:
    """Whether a family's snapshots may support a verified catalog fact.

    The single predicate the link path asks. `legacy_reference` answers False
    here forever, which is what makes "the old catalog never verifies a fact" a
    property of the code rather than a convention.
    """
    return TRUST_STATE_BY_FAMILY.get(source_family) == "evidence"


def stated_identity_dimensions(dimensions: Mapping[str, Any] | None) -> dict[str, str]:
    """The dimensions a source actually STATED, validated, or fail closed.

    An absent dimension is an absent key. A key present with an empty value, a
    non-string value, or a name outside the closed vocabulary is a refusal --
    never a silently dropped field, because a dropped field is how a guess
    becomes indistinguishable from a statement.
    """
    if dimensions is None:
        return {}
    if not isinstance(dimensions, Mapping):
        raise ValueError("catalog identity dimensions must be an object")
    stated: dict[str, str] = {}
    for name, value in dimensions.items():
        if name not in CANDIDATE_IDENTITY_DIMENSIONS:
            raise ValueError("unknown catalog identity dimension")
        if not isinstance(value, str) or not value.strip() or value.strip() != value:
            raise ValueError("a stated catalog identity dimension must be exact text")
        stated[name] = value
    return stated


__all__ = ["CANDIDATE_IDENTITY_DIMENSIONS", "CANDIDATE_STATUSES",
           "CATALOG_SOURCE_FAMILIES", "CATALOG_TRUST_STATES",
           "CONTENT_SHA256_PATTERN", "IDEMPOTENCY_KEY_PATTERN",
           "MAX_RAW_PAYLOAD_CHARS", "MAX_RETRIEVAL_METADATA_CHARS",
           "SNAPSHOT_VALIDATION_STATES", "TRUST_STATE_BY_FAMILY",
           "is_evidence_family", "stated_identity_dimensions", "trust_state_for"]

"""R4: explicit, append-only conflict resolution.

Before R4 a contradiction was permanent.  The engine grouped claims by
canonical scope, marked every claim in a scope holding two different values as
conflicting, and the verifier settled all of them as
`needs_review / unresolved conflict`.  Adding a better source therefore
changed nothing: it produced one more claim in the same open conflict, and the
run's active result stayed exactly as unusable as before.

This module is the missing decision.  It answers ONE question, deterministically:

    given the claims of one contradicting scope, and which of them are
    DECISIVELY supported, does a winner exist?

The rules are deliberately narrow:

*   A conflict may close only when exactly ONE value is decisively supported
    for the EXACT same identity and scope.  "Decisive" means two independent
    things at once: the claim matched its own source's structured fact under
    the deterministic comparison contract (.comparison), and that source is
    authoritative FOR THAT FIELD under the versioned policy below.
*   Source authority is FIELD-SPECIFIC.  A government registry proves
    regulatory identity and homologated technical data; it proves nothing
    about a market price or a reliability statistic, and an official model
    code proves nothing about either.  A source type this policy does not
    know is authoritative for nothing.
*   Ambiguity stays ambiguous.  Two decisive sources that disagree, or none at
    all, leave the conflict `unresolved`, and the claims stay
    `needs_review`.  Nothing here picks a winner to improve a completion rate.
*   Nothing is deleted.  A losing claim is marked `superseded` and keeps its
    row, its evidence and its place in history; the resolution is an
    APPEND-ONLY record of a decision, never an edit of the claims it decided.

Pure module: no database access, no provider call, no tool execution, no
global mutable state.
"""

from __future__ import annotations

from typing import Any, Iterable, Literal, Mapping, Sequence

from pydantic import Field, model_validator

from .comparison import ScopeIdentity, scope_identity_hash
from .contracts import StrictContract
from .normalization import canonical_value_key, normalize_field_key

# The version of the authority policy below.  Persisted with every resolution,
# so a durable decision always names the rules that produced it.
CONFLICT_POLICY_VERSION = "r4.authority.1"

#: The typed lifecycle of one contradicting scope.  `superseded` is the state
#: of a losing CLAIM inside a resolved conflict, never of the conflict itself.
RESOLUTION_STATES = ("unresolved", "resolved")
#: The typed state of ONE claim inside a decision.  `corroborating` is a real
#: outcome, not a leftover: a claim that already stated the winning value did
#: not lose, so it is neither the winner nor superseded and keeps its own
#: verdict.
CLAIM_RESOLUTION_STATES = ("unresolved", "resolved", "superseded", "corroborating")

RESOLUTION_REASONS = frozenset({
    "R4_CONFLICT_RESOLVED_BY_DECISIVE_SOURCE",
    "R4_CONFLICT_UNRESOLVED_NO_DECISIVE_SOURCE",
    "R4_CONFLICT_UNRESOLVED_AMBIGUOUS",
})

#: What KIND of statement a field makes.  A field this map does not name has
#: no family, so no source type can be authoritative for it and no conflict
#: over it can ever close automatically.  That is the fail-closed default.
_FIELD_FAMILIES: Mapping[str, str] = {
    # Who the vehicle officially IS.
    "model_code": "regulatory_identity",
    "official_model_code": "regulatory_identity",
    "commercial_model_code": "regulatory_identity",
    "registration_code": "regulatory_identity",
    "trim_code": "regulatory_identity",
    # What the vehicle technically IS, as homologated/published.
    "engine_displacement_cc": "technical_specification",
    "engine_code": "technical_specification",
    "transmission": "technical_specification",
    "fuel_type": "technical_specification",
    "power_kw": "technical_specification",
    "gross_weight_kg": "technical_specification",
    "seats": "technical_specification",
    # What the vehicle COSTS in a market.
    "list_price": "market_price",
    "market_price": "market_price",
    "price": "market_price",
    # How the vehicle BEHAVES over time.
    "reliability_score": "reliability",
    "failure_rate": "reliability",
    "recall_count": "reliability",
}

#: Which source TYPE is authoritative for which family.  The entries a
#: government registry does NOT have are the point of this table: presence in
#: an official registry, and possession of an official model code, say nothing
#: about reliability or about what an importer charges.
_SOURCE_TYPE_AUTHORITY: Mapping[str, frozenset[str]] = {
    "government_registry": frozenset({"regulatory_identity", "technical_specification"}),
    "manufacturer_specification": frozenset({"technical_specification",
                                             "regulatory_identity"}),
    "importer_price_list": frozenset({"market_price"}),
    "reliability_dataset": frozenset({"reliability"}),
}

FIELD_FAMILIES: Mapping[str, str] = {normalize_field_key(field): family
                                     for field, family in _FIELD_FAMILIES.items()}
SOURCE_TYPE_AUTHORITY: Mapping[str, frozenset[str]] = {
    normalize_field_key(source_type): families
    for source_type, families in _SOURCE_TYPE_AUTHORITY.items()}


def field_family(field: Any) -> str | None:
    """The statement family of a field, or None when the policy has no rule."""
    if not isinstance(field, str) or not field.strip():
        return None
    return FIELD_FAMILIES.get(normalize_field_key(field))


def is_authoritative(source_type: Any, field: Any) -> bool:
    """Whether a source of this TYPE is authoritative for THIS field.

    Fail-closed in both directions: an unknown source type is authoritative
    for nothing, and a field with no known family has no authoritative source
    type at all.
    """
    family = field_family(field)
    if family is None or not isinstance(source_type, str):
        return False
    return family in SOURCE_TYPE_AUTHORITY.get(normalize_field_key(source_type), frozenset())


class ConflictResolution(StrictContract):
    """The durable, append-only record of ONE conflict decision.

    It states the scope that contradicted itself, every claim that took part,
    the winner when there is one, the claims the winner supersedes, the static
    reason and the policy version the decision was made under.  No claim is
    edited and none is removed: this row is the decision, and the claims
    remain exactly as they were recorded.
    """

    scope_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    entity: str = Field(min_length=1, max_length=200)
    field: str = Field(min_length=1, max_length=200)
    state: Literal["unresolved", "resolved"]
    reason: str = Field(min_length=1, max_length=64)
    policy_version: str = Field(min_length=1, max_length=64)
    claim_ids: list[str] = Field(min_length=2, max_length=100)
    winning_claim_id: str | None = None
    superseded_claim_ids: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def _shape(self) -> "ConflictResolution":
        if self.reason not in RESOLUTION_REASONS:
            raise ValueError("resolution reason must come from the static allowlist")
        if len(set(self.claim_ids)) != len(self.claim_ids):
            raise ValueError("a conflict names each claim once")
        resolved = self.state == "resolved"
        if resolved != (self.winning_claim_id is not None):
            raise ValueError("a resolved conflict has exactly one winning claim")
        if self.winning_claim_id is not None and self.winning_claim_id not in set(self.claim_ids):
            raise ValueError("the winning claim must belong to the conflict")
        superseded = set(self.superseded_claim_ids)
        if len(superseded) != len(self.superseded_claim_ids):
            raise ValueError("a conflict supersedes each claim once")
        if not superseded <= set(self.claim_ids) - {self.winning_claim_id}:
            raise ValueError("only a losing claim of this conflict can be superseded")
        if resolved and not superseded:
            # A conflict exists because two DIFFERENT values were stated, so
            # closing it must supersede at least one of them. A "resolved"
            # decision that supersedes nothing would leave every contradicting
            # value active while calling the contradiction settled.
            raise ValueError("a resolved conflict supersedes at least one losing claim")
        if not resolved and superseded:
            raise ValueError("an unresolved conflict supersedes nothing")
        return self

    def state_of(self, claim_id: str) -> str:
        """The typed resolution state of ONE claim inside this conflict.

        Four outcomes, and the fourth matters: a claim that already stated the
        winning value CORROBORATES the decision. It did not lose, so it is
        never superseded, and it keeps whatever verdict its own evidence earns.
        """
        if claim_id == self.winning_claim_id:
            return "resolved"
        if claim_id in set(self.superseded_claim_ids):
            return "superseded"
        return "corroborating" if self.state == "resolved" else "unresolved"


def resolve_conflict_group(identity: ScopeIdentity, claims: Sequence[Any], *,
                           decisive_claim_ids: Iterable[str]) -> ConflictResolution:
    """Decide ONE contradicting scope from its claims and their support.

    `decisive_claim_ids` is the caller's already-computed set of claims that
    BOTH matched their own source's structured fact deterministically AND come
    from a source this policy calls authoritative for that field.  The winner
    is the single decisive VALUE, never the single decisive claim: two
    authoritative sources that agree resolve the conflict together, and the
    lowest claim id of that value is recorded as the winner so the decision is
    insertion-order independent.
    """
    ordered = sorted(claims, key=lambda item: item.claim_id)
    claim_ids = [item.claim_id for item in ordered]
    entity, field = ordered[0].entity, ordered[0].field
    decisive = [item for item in ordered if item.claim_id in set(decisive_claim_ids)]
    base = {"scope_hash": scope_identity_hash(identity), "entity": entity, "field": field,
            "policy_version": CONFLICT_POLICY_VERSION, "claim_ids": claim_ids}
    if not decisive:
        return ConflictResolution(state="unresolved",
                                  reason="R4_CONFLICT_UNRESOLVED_NO_DECISIVE_SOURCE", **base)
    values = {canonical_value_key(item.value) for item in decisive}
    if len(values) > 1:
        # Two authoritative sources contradicting each other is exactly the
        # case a completion rate would love to break the tie on.  It stays open.
        return ConflictResolution(state="unresolved",
                                  reason="R4_CONFLICT_UNRESOLVED_AMBIGUOUS", **base)
    winner = decisive[0].claim_id
    winning_value = canonical_value_key(decisive[0].value)
    # EVERY claim stating a different value loses; every other claim states the
    # winning value and is corroboration, not a loser, so it keeps its own
    # verdict and is neither superseded nor the winner. The conflict exists
    # because two different values were stated, so this list is never empty.
    superseded = [item.claim_id for item in ordered
                  if canonical_value_key(item.value) != winning_value]
    return ConflictResolution(state="resolved",
                              reason="R4_CONFLICT_RESOLVED_BY_DECISIVE_SOURCE",
                              winning_claim_id=winner, superseded_claim_ids=superseded, **base)


def resolve_conflicts(groups: Mapping[ScopeIdentity, Sequence[Any]], *,
                      decisive_claim_ids: Iterable[str]) -> tuple[ConflictResolution, ...]:
    """Decide every contradicting scope, in deterministic scope-hash order."""
    decisive = frozenset(decisive_claim_ids)
    resolutions = [resolve_conflict_group(identity, claims, decisive_claim_ids=decisive)
                   for identity, claims in groups.items()]
    return tuple(sorted(resolutions, key=lambda item: item.scope_hash))


def conflict_groups(references: Iterable[Any]) -> dict[ScopeIdentity, list[Any]]:
    """Group claims that contradict one another within one exact identity.

    Uses the R4 identity (scope PLUS the closed identity dimensions), so two
    claims that differ only by generation, engine, transmission or official
    code are two different variants and never a contradiction.
    """
    from .comparison import reference_identity  # local: keeps this module import-light

    by_identity: dict[ScopeIdentity, list[Any]] = {}
    for item in references:
        by_identity.setdefault(reference_identity(item), []).append(item)
    return {identity: claims for identity, claims in by_identity.items()
            if len(claims) > 1 and len({canonical_value_key(item.value)
                                        for item in claims}) > 1}


def parse_resolutions(raw: Any) -> tuple[ConflictResolution, ...]:
    """Rebuild resolutions from durable state, or fail closed.

    A checkpoint is durable data, not a trusted object graph: every stored
    resolution is revalidated through the contract that created it.
    """
    if raw is None:
        return ()
    if isinstance(raw, (str, bytes, bytearray)) or not isinstance(raw, (list, tuple)):
        raise ValueError("conflict resolutions must be a list")
    rebuilt: list[ConflictResolution] = []
    for item in raw:
        rebuilt.append(item if isinstance(item, ConflictResolution)
                       else ConflictResolution.model_validate(
                           {str(key): value for key, value in dict(item).items()}))
    return tuple(sorted(rebuilt, key=lambda entry: entry.scope_hash))


__all__ = ["CLAIM_RESOLUTION_STATES", "CONFLICT_POLICY_VERSION", "FIELD_FAMILIES",
           "RESOLUTION_REASONS", "RESOLUTION_STATES", "SOURCE_TYPE_AUTHORITY",
           "ConflictResolution", "conflict_groups", "field_family", "is_authoritative",
           "parse_resolutions", "resolve_conflict_group", "resolve_conflicts"]

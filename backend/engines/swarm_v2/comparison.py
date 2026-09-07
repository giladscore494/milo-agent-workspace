"""R4: the closed, versioned contract for comparing a claim to a source fact.

R3 carried a unit and a locator but deliberately never interpreted either.
The consequence was that verification of a STRUCTURED fact was still a model
judgement: a completion asserting "verified" for a 1,798 cc claim against a
source whose record says 1,600 cc was accepted, because nothing in the
backend could say the two numbers are simply not the same number.

This module is that missing decision, and it is deterministic code:

    claim (EvidenceReference)  +  the structured facts durably recorded from
    that claim's OWN source     ->  verified / rejected / needs_review

Three rules define the boundary and must never be relaxed:

1.  Nothing here consults a model, a network, a database or a tool.  Every
    function is a pure, total function of its arguments, so the same claim and
    the same durable facts always produce the same outcome, byte for byte.
2.  Equivalence is EXPLICIT, CLOSED and VERSIONED.  A unit converts only when
    `UNIT_CONVERSIONS` names an exact rational factor between two units of the
    same family; there is no fuzzy matching, no similarity, no rounding
    tolerance and no model-invented equivalence.  An unknown unit, a
    cross-family pair and a lossy pair (hp/kW, mpg/l per 100 km, °C/°F) are
    all "not convertible" and fail closed.
3.  The ORIGINAL value and unit are never rewritten.  Conversion happens
    inside the comparison, on exact rational arithmetic, and the claim and the
    fact keep the value and unit they were recorded with.

Identity is the other half of the contract.  Comparing a claim to a fact that
describes a different variant is not verification, so the comparison scope is
the COMPLETE normalized identity: entity, field, geography, market, the
time/model-year scope AND the closed identity dimensions
(`evidence_bounds.IDENTITY_DIMENSIONS`: generation, engine, transmission,
official/model code, trim, drivetrain, body style) whenever a record states
them.  Make + commercial model + an overlapping year is explicitly NOT
treated as identifying a variant.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Literal, Mapping, NamedTuple, Sequence

from .evidence_bounds import IDENTITY_DIMENSIONS, MAX_IDENTITY_DIMENSION_CHARS
from .normalization import (CanonicalScope, canonical_scope_key, canonical_value_key,
                            normalize_field_key)

# The version of THIS comparison contract.  It is persisted next to every
# verdict the contract decides, so a stored verdict always names the rules it
# was reached under and a later release can tell its own decisions from an
# older one's.  Bumping it is a deliberate act: it invalidates nothing
# retroactively, it simply stops older verdicts from being presented as
# decisions of the current contract.
#
# r4.structured.2 corrects the conflict/ambiguity semantics: equivalence is now
# a QUANTITY identity (`value_identity`) shared by every caller, so equal raw
# numbers in incompatible units no longer collapse and equivalent numbers in
# allowlisted units no longer contradict.
STRUCTURED_COMPARISON_VERSION = "r4.structured.2"
# The version of the unit/value equivalence allowlist below.  Separate from
# the comparison version on purpose: adding one allowlisted conversion is a
# smaller change than changing how identity or scope is compared.
UNIT_RULE_VERSION = "r4.units.1"

# The static, code-owned outcome codes.  Every one of them is safe for durable
# state, a run event and telemetry: none carries a value, a unit, a source
# excerpt or any provider material.
COMPARISON_REASONS = frozenset({
    "R4_STRUCTURED_MATCH",
    "R4_AMBIGUOUS_SUPPORT_EVIDENCE",
    "R4_VALUE_MISMATCH",
    "R4_VALUE_NOT_COMPARABLE",
    "R4_UNIT_MISSING",
    "R4_UNIT_NOT_CONVERTIBLE",
    "R4_FIELD_NOT_IN_SOURCE",
    "R4_SCOPE_MISMATCH",
    "R4_IDENTITY_MISMATCH",
    "R4_SOURCE_VERSION_MISMATCH",
    "R4_AMBIGUOUS_SOURCE_FACT",
    "R4_NO_STRUCTURED_FACT",
})

# --- the closed unit allowlist ----------------------------------------------
#
# `unit -> (family, numerator, denominator)`: an EXACT rational factor to that
# family's base unit.  Two units compare only when they share a family, and
# the comparison is done with `fractions.Fraction`, so no float rounding can
# make two different quantities look equal or two equal ones look different.
#
# Deliberately ABSENT, and therefore "not convertible":
#
#   hp / ps / bhp   metric and mechanical horsepower are different definitions
#                   and neither is an exact multiple of a watt;
#   mpg / l_100km   a reciprocal relationship, not a factor;
#   c / f           an offset scale, not a factor;
#   nm / lbft       an inexact factor.
#
# Each of those is a real conversion a person might expect; refusing them is
# the point.  A claim stated in a unit this table does not relate to the
# source's unit is never "close enough" -- it is unverified.
UNIT_CONVERSIONS: Mapping[str, tuple[str, int, int]] = {
    # volume (base: cubic centimetre)
    "cc": ("volume_cc", 1, 1),
    "cm3": ("volume_cc", 1, 1),
    "cm^3": ("volume_cc", 1, 1),
    "ml": ("volume_cc", 1, 1),
    "l": ("volume_cc", 1000, 1),
    # length (base: millimetre)
    "mm": ("length_mm", 1, 1),
    "cm": ("length_mm", 10, 1),
    "m": ("length_mm", 1000, 1),
    "km": ("length_mm", 1_000_000, 1),
    # mass (base: gram)
    "g": ("mass_g", 1, 1),
    "kg": ("mass_g", 1000, 1),
    "t": ("mass_g", 1_000_000, 1),
    # power (base: watt)
    "w": ("power_w", 1, 1),
    "kw": ("power_w", 1000, 1),
    # torque (base: newton metre)
    "nm": ("torque_nm", 1, 1),
    # speed (base: km/h)
    "km/h": ("speed_kmh", 1, 1),
    "kph": ("speed_kmh", 1, 1),
    # dimensionless proportion
    "%": ("ratio_pct", 1, 1),
}


def normalize_unit(value: Any) -> str | None:
    """Formatting-only normalization of a unit token.

    Case and surrounding whitespace are never meaning, so `CC`, ` cc ` and
    `cc` are one unit.  Nothing else is touched: `cc` and `cm3` are equal
    because `UNIT_CONVERSIONS` says so, never because a normalizer guessed it.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    text = value.strip().casefold()
    return text or None


def unit_factor(unit: str | None) -> tuple[str, Fraction] | None:
    """`(family, exact factor to the family base)` for an allowlisted unit."""
    if unit is None:
        return None
    rule = UNIT_CONVERSIONS.get(unit)
    if rule is None:
        return None
    family, numerator, denominator = rule
    return family, Fraction(numerator, denominator)


def _exact(value: int | float) -> Fraction:
    """An exact rational for a numeric evidence value.

    Floats go through their DECIMAL text, not their binary expansion, so a
    stored `1.6` is the number a person wrote rather than the nearest double.
    """
    return Fraction(value) if isinstance(value, int) else Fraction(str(value))


def _is_number(value: Any) -> bool:
    # `bool` is an `int` in Python and is never a measurement.
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def compare_values(claim_value: Any, claim_unit: Any, fact_value: Any,
                   fact_unit: Any) -> str:
    """Compare one claim value/unit pair to one source value/unit pair.

    Returns a static reason code; `R4_STRUCTURED_MATCH` is the ONLY accepting
    answer.  Total: every input shape maps to a code, and nothing raises.

    Numbers are compared as exact rationals after each side is converted to
    its family's base unit.  Non-numeric values are compared by their
    deterministic canonical identity, with the same formatting-only text
    normalization the scope contract uses -- never by similarity.
    """
    claim_normalized, fact_normalized = normalize_unit(claim_unit), normalize_unit(fact_unit)
    claim_numeric, fact_numeric = _is_number(claim_value), _is_number(fact_value)
    if claim_numeric != fact_numeric:
        # A number never equals a word, whatever the word says.
        return "R4_VALUE_NOT_COMPARABLE"
    if claim_numeric:
        if claim_normalized is None or fact_normalized is None:
            # "1798" is not a fact until it says cc.  A measurement without a
            # unit is unverifiable, not "probably the same unit".
            return "R4_UNIT_MISSING"
        if claim_normalized != fact_normalized:
            claim_rule, fact_rule = unit_factor(claim_normalized), unit_factor(fact_normalized)
            if claim_rule is None or fact_rule is None or claim_rule[0] != fact_rule[0]:
                return "R4_UNIT_NOT_CONVERTIBLE"
            factors: tuple[Fraction, Fraction] = (claim_rule[1], fact_rule[1])
        else:
            factors = (Fraction(1), Fraction(1))
        left, right = _exact(claim_value) * factors[0], _exact(fact_value) * factors[1]
        return "R4_STRUCTURED_MATCH" if left == right else "R4_VALUE_MISMATCH"
    # Non-numeric evidence carries no unit.  A unit on one side and not the
    # other, or two different units, is a different statement.
    if claim_normalized != fact_normalized:
        return "R4_UNIT_NOT_CONVERTIBLE"
    if isinstance(claim_value, str) and isinstance(fact_value, str):
        return ("R4_STRUCTURED_MATCH"
                if normalize_field_key(claim_value) == normalize_field_key(fact_value)
                else "R4_VALUE_MISMATCH")
    return ("R4_STRUCTURED_MATCH"
            if canonical_value_key(claim_value) == canonical_value_key(fact_value)
            else "R4_VALUE_MISMATCH")


class QuantityIdentity(NamedTuple):
    """The ONE code-owned identity of a stated value-and-unit.

    Two statements are THE SAME STATEMENT exactly when their identities are
    equal.  There is one definition and every caller uses it -- the source
    self-consistency check, conflict grouping, conflict resolution and the
    Evidence Board's durable conflict detection -- so no two of them can drift
    into disagreeing about what "the same value" means.

    Two shapes, and the difference is the whole point:

    *   `("quantity", family, exact)` -- a number stated in an ALLOWLISTED
        unit, converted to its family's base with exact rational arithmetic.
        `1600 cc` and `1.6 l` produce the identical tuple, so they can never
        contradict each other; `1600 cc` and `1600 l` produce different ones,
        so equal raw numbers in incompatible units can never collapse into
        agreement.
    *   `("opaque", unit, value)` -- anything the allowlist cannot relate: a
        missing unit, an unknown unit, a non-numeric value.  Two of these are
        equal ONLY when the stated unit token and the value are literally the
        same, so an unprovable equivalence is never asserted.  That is the
        fail-closed half: `1600` and `1600 cc` are different statements, and
        `1600 hp` and `1600 kw` are different statements, because nothing in
        the contract can prove otherwise.
    """

    kind: str
    unit: str
    value: str


def value_identity(value: Any, unit: Any = None) -> QuantityIdentity:
    """The deterministic identity of ONE stated value-and-unit.

    Total and pure: every input shape maps to an identity and nothing raises.

    This answers "are these the same statement", which is deliberately NOT the
    same question as `compare_values`, which answers "does this claim VERIFY
    against a source fact".  They agree everywhere except one documented case:
    two identical UNIT-LESS numbers have the same identity (they are literally
    the same statement, so they do not contradict) while `compare_values`
    refuses to verify either of them (`R4_UNIT_MISSING`: a measurement without
    a unit is unverifiable).  Failing to verify is not the same as
    contradicting, and conflating the two would invent contradictions that can
    never be closed.  tests/test_swarm_v2_r4_deterministic_verification.py
    pins the whole agreement matrix, including that one difference.
    """
    normalized = normalize_unit(unit)
    if _is_number(value):
        rule = unit_factor(normalized)
        if rule is not None:
            family, factor = rule
            base = _exact(value) * factor
            # `Fraction` normalizes to lowest terms, so the rendering is a
            # canonical identity for the quantity, not for how it was written.
            return QuantityIdentity("quantity", family, f"{base.numerator}/{base.denominator}")
        return QuantityIdentity("opaque", normalized or "", canonical_value_key(value))
    if isinstance(value, str):
        # The same formatting-only text normalization compare_values applies.
        return QuantityIdentity("opaque", normalized or "", normalize_field_key(value))
    return QuantityIdentity("opaque", normalized or "", canonical_value_key(value))


def same_value(left_value: Any, left_unit: Any, right_value: Any, right_unit: Any) -> bool:
    """Whether two stated values are the same statement under the contract."""
    return value_identity(left_value, left_unit) == value_identity(right_value, right_unit)


def normalize_identity(identity: Mapping[str, Any] | None) -> tuple[tuple[str, str], ...]:
    """The closed identity dimensions of a record, normalized and ordered.

    Unknown keys, empty values and over-long values are DROPPED rather than
    guessed at: an identity dimension only ever narrows a comparison, so a
    value nobody can validate must not be able to widen it either.  The result
    is sorted, so identity equality is independent of insertion order.
    """
    if not isinstance(identity, Mapping):
        return ()
    kept: list[tuple[str, str]] = []
    for dimension in IDENTITY_DIMENSIONS:
        raw = identity.get(dimension)
        if raw is None or isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
            continue
        text = normalize_field_key(str(raw))
        if not text or len(text) > MAX_IDENTITY_DIMENSION_CHARS:
            continue
        kept.append((dimension, text))
    return tuple(sorted(kept))


class ScopeIdentity(NamedTuple):
    """The COMPLETE comparison scope of one structured statement.

    `scope` is the existing canonical scope (entity, field, geography, market,
    time scope); `identity` is the closed set of variant dimensions the record
    stated.  Two statements describe the same thing only when both halves are
    equal, which is exactly why make + model + an overlapping year is not
    enough on its own.
    """

    scope: CanonicalScope
    identity: tuple[tuple[str, str], ...]


def scope_identity(*, entity: str, field: str, geography: str | None = None,
                   market: str | None = None, time_scope: Mapping[str, Any] | None = None,
                   identity: Mapping[str, Any] | None = None) -> ScopeIdentity:
    """The ONE identity contract: never re-create this shape elsewhere."""
    return ScopeIdentity(scope=canonical_scope_key(entity=entity, field=field,
                                                   geography=geography, market=market,
                                                   time_scope=time_scope),
                         identity=normalize_identity(identity))


def scope_identity_hash(identity: ScopeIdentity) -> str:
    """The bounded durable identity of ONE comparison scope.

    SHA-256 over the deterministic serialization of both halves, so a durable
    row can name a scope without re-implementing normalization and without
    storing an unbounded composite key.
    """
    encoded = json.dumps([list(identity.scope), [list(item) for item in identity.identity]],
                         separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


def reference_identity(reference: Any) -> ScopeIdentity:
    """The comparison scope of an EvidenceReference (a claim under judgement)."""
    return scope_identity(entity=reference.entity, field=reference.field,
                          geography=reference.geography, market=reference.market,
                          time_scope=reference.time_scope,
                          identity=getattr(reference, "identity", None))


@dataclass(frozen=True)
class StructuredSourceFact:
    """One structured fact durably recorded FROM a source, as R4 reads it.

    A structured source fact is a durable claim that carries an R3 evidence
    locator -- that is, one a trusted mapper read out of an exact location in
    a versioned source.  A claim with no locator is a statement someone made
    ABOUT a source, not a fact recorded FROM it, and is never a comparison
    authority here.

    `locator` is what binds a fact to the focused fragments that support it,
    so an accepted comparison can name the exact durable evidence rows behind
    it instead of asserting that support exists.
    """

    fact_id: str
    source_id: str
    task_id: str
    field: str
    entity: str
    value: Any
    unit: str | None = None
    geography: str | None = None
    market: str | None = None
    time_scope: Mapping[str, Any] | None = None
    identity: Mapping[str, Any] | None = None
    locator: str | None = None

    def __post_init__(self) -> None:
        if not (isinstance(self.fact_id, str) and self.fact_id):
            raise ValueError("a structured source fact requires a durable identity")
        if not (isinstance(self.source_id, str) and self.source_id):
            raise ValueError("a structured source fact requires its source")
        if not (isinstance(self.task_id, str) and self.task_id):
            raise ValueError("a structured source fact requires task provenance")
        if not (isinstance(self.field, str) and self.field):
            raise ValueError("a structured source fact requires a field")

    @property
    def scope_identity(self) -> ScopeIdentity:
        return scope_identity(entity=self.entity, field=self.field, geography=self.geography,
                              market=self.market, time_scope=self.time_scope,
                              identity=self.identity)


@dataclass(frozen=True)
class StructuredComparison:
    """The decided outcome of ONE deterministic structured comparison.

    `outcome` says what the caller must do; `reason` is the static code that
    becomes the durable verdict reason; `matched` is the fact the decision
    rests on, so the caller can bind the verdict to that fact's own durable
    evidence.  Nothing here carries source text.
    """

    outcome: Literal["match", "mismatch", "ambiguous", "not_comparable"]
    reason: str
    matched: StructuredSourceFact | None = None

    def __post_init__(self) -> None:
        if self.reason not in COMPARISON_REASONS:
            raise ValueError("comparison reason must come from the static allowlist")

    @property
    def is_decisive(self) -> bool:
        """Whether deterministic code settled this claim without a model call."""
        return self.outcome in {"match", "mismatch", "ambiguous"}


def compare_structured(reference: Any, facts: Sequence[StructuredSourceFact], *,
                       claim_source_version: str | None = None,
                       source_version: str | None = None) -> StructuredComparison:
    """Compare one claim to the structured facts of its OWN source.

    The caller supplies only the facts of the claim's own source; this
    function decides, in this order:

    1.  the source version the claim was read at must still be the version the
        durable source records (a claim with no recorded version is pre-R4/R3
        evidence and is not held to a version it never had);
    2.  the source must state the claim's FIELD at all;
    3.  the field statement must be about the same thing -- entity,
        geography, market, time scope and every stated identity dimension;
    4.  the source must not state two different values for that one identity;
    5.  the values must be equal under the closed, versioned unit rules.

    Anything the source simply does not describe structurally is
    `not_comparable`: deterministic code does not pretend to understand prose,
    and such a claim continues to the grounded model verifier unchanged.
    """
    if claim_source_version is not None and claim_source_version != source_version:
        # Evidence read at one version can never be validated against another.
        return StructuredComparison("mismatch", "R4_SOURCE_VERSION_MISMATCH")
    located = [fact for fact in facts if fact.locator]
    if not located:
        return StructuredComparison("not_comparable", "R4_NO_STRUCTURED_FACT")
    wanted = reference_identity(reference)
    same_field = [fact for fact in located
                  if fact.scope_identity.scope.field == wanted.scope.field]
    if not same_field:
        # The source is structured and simply does not state this field.  That
        # is a real negative answer, not a missing-evidence answer.
        return StructuredComparison("mismatch", "R4_FIELD_NOT_IN_SOURCE")
    candidates = [fact for fact in same_field if fact.scope_identity == wanted]
    if not candidates:
        # Distinguish "the same variant in a different scope" (wrong year,
        # wrong market) from "a different variant entirely" (a different
        # generation, engine, transmission or official code), because they are
        # different mistakes even though neither may verify.
        differing_identity = any(fact.scope_identity.scope == wanted.scope for fact in same_field)
        return StructuredComparison(
            "mismatch",
            "R4_IDENTITY_MISMATCH" if differing_identity else "R4_SCOPE_MISMATCH")
    distinct = {value_identity(fact.value, fact.unit) for fact in candidates}
    if len(distinct) > 1:
        # One source stating two DIFFERENT quantities for one identity is a
        # contradiction inside the source itself.  It is never resolved by
        # picking one: it stays for review.  Two EQUIVALENT statements of the
        # same quantity (1600 cc and 1.6 l) share one identity and are not a
        # contradiction, so a self-consistent source is never called ambiguous
        # for writing the same fact in two allowlisted units.
        return StructuredComparison("ambiguous", "R4_AMBIGUOUS_SOURCE_FACT")
    fact = min(candidates, key=lambda item: item.fact_id)
    reason = compare_values(reference.value, getattr(reference, "unit", None),
                            fact.value, fact.unit)
    return StructuredComparison("match" if reason == "R4_STRUCTURED_MATCH" else "mismatch",
                                reason, fact)


__all__ = ["COMPARISON_REASONS", "IDENTITY_DIMENSIONS", "STRUCTURED_COMPARISON_VERSION",
           "UNIT_CONVERSIONS", "UNIT_RULE_VERSION", "QuantityIdentity", "ScopeIdentity",
           "StructuredComparison", "StructuredSourceFact", "compare_structured",
           "compare_values", "normalize_identity", "normalize_unit", "reference_identity",
           "same_value", "scope_identity", "scope_identity_hash", "unit_factor",
           "value_identity"]

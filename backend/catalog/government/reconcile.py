"""Catalog PR3: ONE deterministic Government <-> legacy-reference comparison.

What the legacy side IS here
----------------------------

An unverified `legacy_reference` snapshot's candidate rows, or an explicit
bounded list of the same shape. That is the whole of it. The 7.3 MB aggregated
JSON is NOT imported, NOT read, NOT parsed and NOT loaded into a prompt by this
module or by anything it calls: a legacy row reaches this comparison only after
someone has already landed it as bounded catalog material through the same
guarded write path everything else uses.

And it can never verify anything. `is_evidence_family("legacy_reference")` is
False, the database pins the trust state to the family, and a
`legacy_reference` candidate can never carry a verdict -- so a comparison
result here is a WORK ITEM, never a fact and never a promotion. Every state
this module produces is something for the Commander to prioritise or for a
reviewer to look at; none of them writes a canonical row, and the promotion
transaction would refuse one built from a legacy source anyway.

Matching, in four tiers and no others
-------------------------------------

1.  an EXACT official model code both sides state;
2.  an EXACT normalized manufacturer and commercial model, with OVERLAPPING
    model years;
3.  an EXPLICIT reviewed alias rule, then the same year overlap;
4.  otherwise the pair is `ambiguous` or unmatched.

There is no fifth tier and no similarity score. `normalize_identity_text` is
case folding, Unicode NFKC and whitespace collapsing -- a stated, reversible
reading of the same text -- and nothing else: no transliteration, no stemming,
no edit distance, no token overlap, no prefix match. `NEW TUCSON` and `TUCSON`
do not match here, and `RAV4`, `RAV4 HEV` and `RAV4 PLUG-IN` stay three
commercial models, because similarity is not identity and deciding that two
names are one vehicle is a judgement with its own evidence requirement.

A tier that finds SEVERAL government candidates returns all of them as
`ambiguous`. It does not return the first.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

#: How many variants one comparison will consider on each side. A comparison is
#: a bounded read like every other read in this namespace; a larger set is a
#: refusal rather than a silently partial answer.
MAX_RECONCILED_VARIANTS = 5_000

#: Every state a comparison can produce, and what each one MEANS. Closed: a
#: state this tuple does not name cannot be produced, so a new one takes a
#: reviewed change rather than appearing in a result.
RECONCILIATION_STATES = ("matched", "ambiguous", "under_enriched",
                         "government_only", "legacy_only", "missing_years")

#: Every method a match may be made BY, in tier order. A result always states
#: which one settled it, so "why does this say matched" is answerable from the
#: result rather than from re-running the comparison.
RECONCILIATION_METHODS = ("official_model_code", "normalized_identity_and_year",
                          "reviewed_alias", "none")

#: The reviewed alias rules this release ships: NONE.
#:
#: An alias rule says two differently-written identities are one vehicle, which
#: is exactly the judgement this module refuses to make on its own. Shipping a
#: guessed one would be worse than shipping none, so the mechanism exists, is
#: tested, and carries no entries until a reviewer adds one.
REVIEWED_ALIAS_RULES: tuple["AliasRule", ...] = ()


class CatalogReconciliationError(ValueError):
    """A comparison that cannot be performed, with a static, safe reason."""


@dataclass(frozen=True)
class AliasRule:
    """One REVIEWED statement that two written identities are one vehicle.

    Explicit on both sides and exact after normalization. There is deliberately
    no pattern, no wildcard and no direction-free "these are similar": a rule
    says THIS legacy identity is THAT government identity, and a reviewer wrote
    it down.
    """

    legacy_manufacturer: str
    legacy_commercial_model: str
    government_manufacturer: str
    government_commercial_model: str
    reviewed_reason: str = ""


@dataclass(frozen=True)
class ReconcilableVariant:
    """One side's variant, in the only shape this comparison reads.

    Deliberately a plain structure rather than either side's own type: the
    government side comes from `catalog_candidate_variants` of an evidence
    snapshot and the legacy side from `catalog_candidate_variants` of a
    `legacy_reference` snapshot, and a comparison that took two different types
    would be a comparison with two different rules.
    """

    candidate_id: str
    candidate_key: str
    manufacturer: str
    commercial_model: str
    model_year_start: int
    model_year_end: int
    official_model_code: str | None = None
    trim: str | None = None
    identity_dimensions: Mapping[str, str] = field(default_factory=dict)
    status: str = "candidate"
    snapshot_key: str = ""

    @property
    def model_years(self) -> tuple[int, ...]:
        return tuple(range(self.model_year_start, self.model_year_end + 1))


@dataclass(frozen=True)
class CatalogMatch:
    """Two sides that this comparison RELATED, and how.

    `state` is `matched`, `ambiguous` or `under_enriched`. Every one names the
    exact candidates on both sides, so a reader never has to re-derive which
    rows a result is about.
    """

    state: str
    method: str
    manufacturer: str
    commercial_model: str
    model_years: tuple[int, ...]
    government_candidate_ids: tuple[str, ...]
    legacy_candidate_ids: tuple[str, ...]
    detail: tuple[str, ...] = ()


@dataclass(frozen=True)
class CatalogGap:
    """Something one side states and the other does not.

    `state` is `government_only`, `legacy_only` or `missing_years`. A gap is a
    structured WORK ITEM: it says what to look at, never what is true.
    """

    state: str
    method: str
    manufacturer: str
    commercial_model: str
    model_years: tuple[int, ...]
    government_candidate_ids: tuple[str, ...] = ()
    legacy_candidate_ids: tuple[str, ...] = ()
    detail: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReconciliationReport:
    """Every comparison result, in one deterministic order."""

    matches: tuple[CatalogMatch, ...]
    gaps: tuple[CatalogGap, ...]
    government_variant_count: int
    legacy_variant_count: int

    @property
    def results(self) -> tuple[Any, ...]:
        return (*self.matches, *self.gaps)

    def by_state(self, state: str) -> tuple[Any, ...]:
        return tuple(item for item in self.results if item.state == state)


def normalize_identity_text(value: Any) -> str:
    """The ONE normalization this comparison applies, stated exactly.

    Unicode NFKC, case folding, whitespace collapsed to single spaces, and the
    ends stripped. That is all of it. Two strings that differ by anything else
    -- a word, a hyphen, a digit, a transliteration -- are two identities here,
    because collapsing them would be deciding that two vehicles are one.
    """
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(text.split())


def _identity(variant: ReconcilableVariant) -> tuple[str, str]:
    return (normalize_identity_text(variant.manufacturer),
            normalize_identity_text(variant.commercial_model))


def _years_overlap(left: ReconcilableVariant, right: ReconcilableVariant) -> bool:
    return (left.model_year_start <= right.model_year_end
            and right.model_year_start <= left.model_year_end)


def variants_from_candidate_rows(rows: Iterable[Mapping[str, Any]], *,
                                 snapshot_key: str = "") -> tuple[ReconcilableVariant, ...]:
    """Read `catalog_candidate_variants` rows into the comparison's own shape.

    A row with no stated model year range is skipped: a comparison whose second
    tier is "overlapping years" cannot place a variant that states none, and
    guessing a range would invent the thing the source declined to state.
    """
    variants: list[ReconcilableVariant] = []
    for row in rows:
        if row.get("model_year_start") is None or row.get("model_year_end") is None:
            continue
        variants.append(ReconcilableVariant(
            candidate_id=str(row["id"]), candidate_key=str(row.get("candidate_key") or ""),
            manufacturer=str(row["manufacturer"]),
            commercial_model=str(row["commercial_model"]),
            model_year_start=int(row["model_year_start"]),
            model_year_end=int(row["model_year_end"]),
            official_model_code=row.get("official_model_code"), trim=row.get("trim"),
            identity_dimensions=dict(row.get("identity_dimensions") or {}),
            status=str(row.get("status") or "candidate"),
            snapshot_key=str(row.get("snapshot_key") or snapshot_key)))
    return tuple(variants)


def _alias_index(rules: Sequence[AliasRule]) -> dict[tuple[str, str], tuple[str, str]]:
    index: dict[tuple[str, str], tuple[str, str]] = {}
    for rule in rules:
        key = (normalize_identity_text(rule.legacy_manufacturer),
               normalize_identity_text(rule.legacy_commercial_model))
        target = (normalize_identity_text(rule.government_manufacturer),
                  normalize_identity_text(rule.government_commercial_model))
        if index.get(key, target) != target:
            # Two reviewed rules pointing one legacy identity at two different
            # government identities is a contradiction in the RULES, and there
            # is no conservative way to pick a side.
            raise CatalogReconciliationError("reviewed alias rules contradict each other")
        index[key] = target
    return index


def _under_enrichment(government: ReconcilableVariant,
                      legacy: ReconcilableVariant) -> tuple[str, ...]:
    """What the government row states that the legacy row does not.

    One direction only, deliberately: the register is the anchor for the
    identity fields it publishes, so a legacy row missing one is a gap to fill.
    The reverse -- a legacy row stating something the register does not -- is
    not enrichment, it is an unverified claim, and it is left where it is.
    """
    missing: list[str] = []
    if government.official_model_code and not legacy.official_model_code:
        missing.append("official_model_code")
    if government.trim and not legacy.trim:
        missing.append("trim")
    missing.extend(sorted(set(government.identity_dimensions) - set(legacy.identity_dimensions)))
    return tuple(missing)


def reconcile_catalog(government: Sequence[ReconcilableVariant],
                      legacy: Sequence[ReconcilableVariant], *,
                      alias_rules: Sequence[AliasRule] = REVIEWED_ALIAS_RULES
                      ) -> ReconciliationReport:
    """Compare two bounded sides deterministically, and settle nothing else.

    Every legacy variant is placed by the first tier that settles it, and a
    tier that finds more than one government candidate produces `ambiguous`
    with EVERY match listed -- never the first one. Government variants no
    legacy row reached are `government_only`; legacy variants no tier settled
    are `legacy_only`; a matched model whose government years the legacy side
    does not cover produces `missing_years`.

    Deterministic in the strongest sense the rules allow: the result is a
    function of the two input sets, and every output list is sorted by values
    the inputs carry, so two runs over the same material produce the same
    report byte for byte.
    """
    if len(government) > MAX_RECONCILED_VARIANTS or len(legacy) > MAX_RECONCILED_VARIANTS:
        raise CatalogReconciliationError("catalog reconciliation input exceeds the bound")
    aliases = _alias_index(alias_rules)

    by_code: dict[str, list[ReconcilableVariant]] = {}
    by_identity: dict[tuple[str, str], list[ReconcilableVariant]] = {}
    for variant in government:
        if variant.official_model_code:
            by_code.setdefault(variant.official_model_code, []).append(variant)
        by_identity.setdefault(_identity(variant), []).append(variant)

    matches: list[CatalogMatch] = []
    gaps: list[CatalogGap] = []
    reached: set[str] = set()
    matched_models: dict[tuple[str, str], list[ReconcilableVariant]] = {}

    for row in sorted(legacy, key=lambda item: (item.manufacturer, item.commercial_model,
                                                item.model_year_start, item.candidate_key)):
        found, method = _candidates_for(row, by_code, by_identity, aliases)
        if not found:
            gaps.append(CatalogGap(
                state="legacy_only", method="none", manufacturer=row.manufacturer,
                commercial_model=row.commercial_model, model_years=row.model_years,
                legacy_candidate_ids=(row.candidate_id,),
                detail=("the register states no variant this row could be",)))
            continue
        reached.update(item.candidate_id for item in found)
        identity = _identity(found[0])
        matched_models.setdefault(identity, []).append(row)
        ids = tuple(sorted(item.candidate_id for item in found))
        if len(found) > 1:
            # An ambiguity is an ANSWER. It is returned whole so a targeted
            # research task can be planned against it; it is never resolved by
            # picking one, and no amount of replay turns it into a match.
            matches.append(CatalogMatch(
                state="ambiguous", method=method, manufacturer=found[0].manufacturer,
                commercial_model=found[0].commercial_model, model_years=row.model_years,
                government_candidate_ids=ids, legacy_candidate_ids=(row.candidate_id,),
                detail=tuple(sorted(
                    f"{item.official_model_code or ''}|{item.trim or ''}" for item in found))))
            continue
        enrichment = _under_enrichment(found[0], row)
        matches.append(CatalogMatch(
            state="under_enriched" if enrichment else "matched", method=method,
            manufacturer=found[0].manufacturer, commercial_model=found[0].commercial_model,
            model_years=found[0].model_years, government_candidate_ids=ids,
            legacy_candidate_ids=(row.candidate_id,), detail=enrichment))

    for variant in sorted(government, key=lambda item: (item.manufacturer,
                                                        item.commercial_model,
                                                        item.model_year_start,
                                                        item.candidate_key)):
        if variant.candidate_id in reached:
            continue
        identity = _identity(variant)
        if identity in matched_models:
            # The model IS known to the legacy side; this model YEAR is not.
            covered = {year for row in matched_models[identity] for year in row.model_years}
            missing = tuple(year for year in variant.model_years if year not in covered)
            if missing:
                gaps.append(CatalogGap(
                    state="missing_years", method="normalized_identity_and_year",
                    manufacturer=variant.manufacturer,
                    commercial_model=variant.commercial_model, model_years=missing,
                    government_candidate_ids=(variant.candidate_id,),
                    legacy_candidate_ids=tuple(sorted(row.candidate_id
                                                      for row in matched_models[identity])),
                    detail=("the register states model years the legacy reference does not",)))
                continue
        gaps.append(CatalogGap(
            state="government_only", method="none", manufacturer=variant.manufacturer,
            commercial_model=variant.commercial_model, model_years=variant.model_years,
            government_candidate_ids=(variant.candidate_id,),
            detail=("the legacy reference states no variant this row could be",)))

    return ReconciliationReport(
        matches=tuple(matches), gaps=tuple(gaps),
        government_variant_count=len(government), legacy_variant_count=len(legacy))


def _candidates_for(row: ReconcilableVariant,
                    by_code: Mapping[str, list[ReconcilableVariant]],
                    by_identity: Mapping[tuple[str, str], list[ReconcilableVariant]],
                    aliases: Mapping[tuple[str, str], tuple[str, str]]
                    ) -> tuple[tuple[ReconcilableVariant, ...], str]:
    """The government candidates one legacy row reaches, by the FIRST tier that
    finds any. Tier order is the confidence order, and a later tier never
    overrules an earlier one."""
    if row.official_model_code:
        exact = tuple(item for item in by_code.get(row.official_model_code, ())
                      if _years_overlap(item, row))
        if exact:
            return exact, "official_model_code"
    identity = _identity(row)
    direct = tuple(item for item in by_identity.get(identity, ())
                   if _years_overlap(item, row))
    if direct:
        return direct, "normalized_identity_and_year"
    aliased = aliases.get(identity)
    if aliased is not None:
        by_alias = tuple(item for item in by_identity.get(aliased, ())
                         if _years_overlap(item, row))
        if by_alias:
            return by_alias, "reviewed_alias"
    return (), "none"


__all__ = ["MAX_RECONCILED_VARIANTS", "RECONCILIATION_METHODS", "RECONCILIATION_STATES",
           "REVIEWED_ALIAS_RULES", "AliasRule", "CatalogGap", "CatalogMatch",
           "CatalogReconciliationError", "ReconcilableVariant", "ReconciliationReport",
           "normalize_identity_text", "reconcile_catalog", "variants_from_candidate_rows"]

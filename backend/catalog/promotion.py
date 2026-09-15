"""Catalog PR3: building ONE canonical promotion out of verified evidence.

Where the enforcement lives, stated first
-----------------------------------------

Not here. Every rule this module applies is also a trigger or a constraint in
`supabase/migrations/20260916120000_catalog_field_level_promotion.sql`, and
those hold for EVERY writer -- including a direct `service_role` INSERT that
never came through this code. This module exists so a refusal is a readable,
static reason instead of a database exception, and so the in-memory repository
and PostgreSQL apply one rule rather than two.

What a promotion is
-------------------

One candidate, one canonical variant, and one provenance record per promoted
FACT. A canonical row may state a field only when a VERIFIED verdict on a claim
of an evidence source says that exact field has that exact value, at an exact
locator, for that exact candidate.

The chain each field is held to, end to end:

    candidate  ->  catalog_candidate_evidence_links  ->  claim  ->  verdict
                                                     ->  source ->  version + locator

A field with no link is not promoted. A link whose claim states a different
field, or the same field at a different value, is refused. A link of another
candidate, of a `legacy_reference` snapshot, or carrying a verdict that is not
exactly `verified`, is refused. None of that is a judgement call at promotion
time: it is the same set of facts, read twice, in two places.

What this module never does
---------------------------

It creates no claim and no verdict -- those come from the R3 acquisition path
and the R4 Verifier -- it consults no model, opens no socket, and holds no
credential. It cannot promote an ambiguous candidate, and it cannot resolve an
ambiguity: an ambiguous candidate stays ambiguous until new evidence changes
what the candidate IS.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from backend.errors import AppError

from .contracts import (CANONICAL_DIMENSION_PREFIX, CANONICAL_VARIANT_FIELDS,
                        is_evidence_family, stated_canonical_fields)

#: The one status a candidate may be promoted from. `candidate` is an unread
#: reading, `ambiguous` is a first-class answer that promotion must not
#: overrule, and `rejected` is a decision against it.
PROMOTABLE_CANDIDATE_STATUS = "ready_for_review"

#: Every refusal this layer can produce, with the safe message it carries. A
#: refusal names the PROPERTY that failed and never a row, a value or a SQL
#: message.
PROMOTION_REASONS: Mapping[str, str] = {
    "CATALOG_PROMOTION_CANDIDATE_NOT_READY":
        "that candidate is not ready for promotion",
    "CATALOG_PROMOTION_SOURCE_NOT_EVIDENCE":
        "an unverified catalog source can never support a canonical fact",
    "CATALOG_PROMOTION_FIELD_UNSUPPORTED":
        "a promoted field has no verified evidence of its own",
    "CATALOG_PROMOTION_FIELD_UNEXPECTED":
        "verified evidence was supplied for a field the canonical row does not state",
    "CATALOG_PROMOTION_VALUE_MISMATCH":
        "the verified claim states a different value for that field",
    "CATALOG_PROMOTION_LINK_CANDIDATE_MISMATCH":
        "an evidence link belongs to another candidate",
    "CATALOG_PROMOTION_LINK_UNVERIFIED":
        "an evidence link carries no verified verdict",
    "CATALOG_PROMOTION_CONFLICT_UNRESOLVED":
        "two verified sources disagree about that field and nothing has resolved it",
    "CATALOG_PROMOTION_REFUSED":
        "the durable catalog refused this promotion",
}


class CatalogPromotionError(ValueError):
    """A promotion refusal carrying ONLY a static, code-owned reason."""

    def __init__(self, reason_code: str):
        if reason_code not in PROMOTION_REASONS:
            raise ValueError("catalog promotion reason must come from the static allowlist")
        self.reason_code = reason_code
        self.safe_message = PROMOTION_REASONS[reason_code]
        super().__init__(self.safe_message)


@dataclass(frozen=True)
class FieldEvidence:
    """ONE promoted fact and the verified evidence link that supports it."""

    field_key: str
    value: Any
    evidence_link_id: str
    claim_id: str
    verdict_id: str
    source_id: str

    def as_promotion_entry(self) -> dict[str, Any]:
        return {"field_key": self.field_key, "value": self.value,
                "evidence_link_id": self.evidence_link_id}


@dataclass(frozen=True)
class PromotionPlan:
    """What a promotion WOULD write, assembled and checked before it is sent."""

    candidate_id: str
    candidate_key: str
    manufacturer: str
    commercial_model: str
    model_year_start: int
    model_year_end: int
    official_model_code: str | None
    trim: str | None
    identity_dimensions: Mapping[str, str]
    fields: tuple[FieldEvidence, ...]
    #: Dimensions the CANDIDATE states that no verified evidence supports, so
    #: the canonical row does not state them either. Reported rather than
    #: dropped: a canonical row that quietly lost a dimension the register
    #: published would be a smaller truth wearing a complete row's shape.
    unsupported_fields: tuple[str, ...] = ()

    def as_payload(self) -> dict[str, Any]:
        """The promotion payload, with every identity key left to be DERIVED.

        The canonical model key, the canonical variant key and the promotion
        key are all computed by `backend/catalog/payloads.prepare_promotion`
        from this payload's own structural fields, so no caller -- and nothing
        a model produced -- can name a canonical identity.
        """
        payload: dict[str, Any] = {
            "candidate_id": self.candidate_id, "candidate_key": self.candidate_key,
            "manufacturer": self.manufacturer, "commercial_model": self.commercial_model,
            "model_year_start": self.model_year_start,
            "model_year_end": self.model_year_end,
            "identity_dimensions": dict(self.identity_dimensions),
            "fields": [item.as_promotion_entry() for item in self.fields]}
        if self.official_model_code is not None:
            payload["official_model_code"] = self.official_model_code
        if self.trim is not None:
            payload["trim"] = self.trim
        return payload


@dataclass(frozen=True)
class PromotionOutcome:
    """The canonical variant a promotion established, and what it promoted."""

    variant: Mapping[str, Any]
    promoted_fields: tuple[str, ...]
    plan: PromotionPlan
    #: True when the promotion found the whole thing already durable and wrote
    #: nothing -- an exact replay, which is a no-op rather than a second row.
    replayed: bool = False

    @property
    def unsupported_fields(self) -> tuple[str, ...]:
        """What the candidate stated and the canonical row does NOT."""
        return self.plan.unsupported_fields


def _claim_value(claim: Mapping[str, Any]) -> Any:
    return claim.get("value")


def field_evidence_for(*, candidate: Mapping[str, Any], links: Sequence[Mapping[str, Any]],
                       claims: Mapping[str, Mapping[str, Any]],
                       verdicts: Mapping[str, Mapping[str, Any]]) -> tuple[FieldEvidence, ...]:
    """Read the field evidence a set of links actually supports, or refuse.

    Deterministic and total: every link is examined, and a link this function
    cannot account for is a refusal rather than a silently skipped row. Two
    links supporting the SAME field are allowed only when they agree on the
    value -- which is what "one field, one promoted value" means; a
    disagreement is an unresolved conflict and is refused here as one.
    """
    by_field: dict[str, FieldEvidence] = {}
    for link in links:
        if str(link.get("candidate_id")) != str(candidate.get("id")):
            raise CatalogPromotionError("CATALOG_PROMOTION_LINK_CANDIDATE_MISMATCH")
        verdict_id = link.get("verdict_id")
        if not verdict_id:
            raise CatalogPromotionError("CATALOG_PROMOTION_LINK_UNVERIFIED")
        verdict = verdicts.get(str(verdict_id))
        if verdict is None or verdict.get("verdict") != "verified":
            raise CatalogPromotionError("CATALOG_PROMOTION_LINK_UNVERIFIED")
        claim = claims.get(str(link.get("claim_id")))
        if claim is None:
            raise CatalogPromotionError("CATALOG_PROMOTION_FIELD_UNSUPPORTED")
        field_key = str(claim.get("field_key"))
        if field_key not in CANONICAL_VARIANT_FIELDS:
            # A verified claim about something the canonical catalog does not
            # store is perfectly legitimate evidence -- it is simply not a
            # promotable field, and it is skipped rather than refused.
            continue
        found = FieldEvidence(field_key=field_key, value=_claim_value(claim),
                              evidence_link_id=str(link["id"]), claim_id=str(claim["id"]),
                              verdict_id=str(verdict_id), source_id=str(link["source_id"]))
        held = by_field.get(field_key)
        if held is not None and held.value != found.value:
            raise CatalogPromotionError("CATALOG_PROMOTION_CONFLICT_UNRESOLVED")
        by_field.setdefault(field_key, found)
    return tuple(by_field[key] for key in sorted(by_field))


def build_promotion_plan(*, candidate: Mapping[str, Any], snapshot: Mapping[str, Any],
                         evidence: Sequence[FieldEvidence]) -> PromotionPlan:
    """Assemble the promotion, holding the row and its evidence to each other.

    The canonical row is built from the CANDIDATE -- which is what the register
    was read into -- and then every field it states is required to have its own
    verified evidence at exactly that value.

    The two kinds of field are treated differently, and the difference is
    structural rather than a preference:

    *   the IDENTITY fields (model year range, official model code, trim) are
        what the canonical variant key is derived from. Each is all-or-nothing:
        a stated one with no evidence is a REFUSAL, because omitting it would
        change which variant this is and could collapse two trims of one model
        year into one canonical row;
    *   an identity DIMENSION is a revisable fact about that variant. One with
        no evidence is simply not promoted, and it is listed in
        `unsupported_fields` so the gap travels with the result instead of
        disappearing. A later source that verifies it appends a revision.

    A field with evidence the row does not state is refused either way:
    provenance for a value that is not written down is provenance for nothing.
    """
    if str(candidate.get("status")) != PROMOTABLE_CANDIDATE_STATUS:
        raise CatalogPromotionError("CATALOG_PROMOTION_CANDIDATE_NOT_READY")
    if not is_evidence_family(str(snapshot.get("source_family"))):
        raise CatalogPromotionError("CATALOG_PROMOTION_SOURCE_NOT_EVIDENCE")

    dimensions = dict(candidate.get("identity_dimensions") or {})
    variant = {"model_year_start": candidate.get("model_year_start"),
               "model_year_end": candidate.get("model_year_end"),
               "official_model_code": candidate.get("official_model_code"),
               "trim": candidate.get("trim")}
    try:
        # The four IDENTITY fields, computed with no dimensions at all: they
        # are what the canonical variant key is derived from, so each one is
        # all-or-nothing. Omitting a stated trim for want of evidence would
        # collapse two trims of one model year into one canonical variant --
        # which is why an unsupported identity field is a REFUSAL below, and
        # an unsupported dimension is a report.
        identity_fields = stated_canonical_fields({**variant, "identity_dimensions": {}})
        dimension_fields = {key: value for key, value
                            in stated_canonical_fields({**variant,
                                                        "identity_dimensions": dimensions}).items()
                            if key.startswith(CANONICAL_DIMENSION_PREFIX)}
    except ValueError:
        raise CatalogPromotionError("CATALOG_PROMOTION_FIELD_UNSUPPORTED") from None

    supported = {item.field_key: item for item in evidence}
    if set(identity_fields) - set(supported):
        # An identity field the canonical row would state with nothing behind
        # it. This is the refusal that makes "every promoted field has exact
        # verified support" true rather than aspirational.
        raise CatalogPromotionError("CATALOG_PROMOTION_FIELD_UNSUPPORTED")
    promoted_dimensions = {key: value for key, value in dimension_fields.items()
                           if key in supported}
    unsupported = tuple(sorted(set(dimension_fields) - set(promoted_dimensions)))
    stated = {**identity_fields, **promoted_dimensions}
    if set(supported) - set(stated):
        # Evidence for a field the row does not state. Provenance for a value
        # that is not written down is provenance for nothing.
        raise CatalogPromotionError("CATALOG_PROMOTION_FIELD_UNEXPECTED")
    for field_key, value in stated.items():
        if supported[field_key].value != value:
            raise CatalogPromotionError("CATALOG_PROMOTION_VALUE_MISMATCH")

    return PromotionPlan(
        candidate_id=str(candidate["id"]), candidate_key=str(candidate["candidate_key"]),
        manufacturer=str(candidate["manufacturer"]),
        commercial_model=str(candidate["commercial_model"]),
        model_year_start=int(variant["model_year_start"]),
        model_year_end=int(variant["model_year_end"]),
        official_model_code=variant["official_model_code"], trim=variant["trim"],
        identity_dimensions={key[len(CANONICAL_DIMENSION_PREFIX):]: value
                             for key, value in promoted_dimensions.items()},
        fields=tuple(supported[key] for key in sorted(stated)),
        unsupported_fields=unsupported)


class CanonicalPromotion:
    """The trusted server path from verified evidence to a canonical row.

    Constructed by wiring that already holds the run's repository and its
    worker lease, exactly like the Evidence Board: every write it performs goes
    through the same lease-guarded, idempotent RPC as every other durable
    catalog write, and it holds no credentials and no model client of its own.

    It is deliberately NOT a Tool. A model may ask for research; it may not ask
    for a promotion, because there is no registered capability that performs
    one -- so `write_approved` and a `tool:write:<name>` capability never enter
    the picture at all.
    """

    def __init__(self, repository: Any, lease: Any) -> None:
        self._repository = repository
        self._lease = lease

    @property
    def _lease_kwargs(self) -> dict[str, Any]:
        return {"worker_id": self._lease.worker_id, "attempt": self._lease.attempt,
                "lease_token": self._lease.lease_token}

    def promote(self, plan: PromotionPlan) -> PromotionOutcome:
        """Submit ONE assembled promotion, atomically, or refuse.

        The repository call is the transaction: the canonical identity and
        every field's provenance are created together or neither survives. A
        refusal from the database collapses to one static reason here, because
        its message can quote a row.
        """
        try:
            variant = self._repository.promote_catalog_variant(
                self._lease.run_id, plan.as_payload(), **self._lease_kwargs)
        except AppError as failure:
            if failure.code == "RUN_LEASE_LOST":
                # A stale worker is an infrastructure outcome, never a
                # promotion refusal: it must reach the worker's lease handling
                # exactly as every other guarded write's does.
                raise
            raise CatalogPromotionError("CATALOG_PROMOTION_REFUSED") from None
        return PromotionOutcome(variant=variant,
                                promoted_fields=tuple(item.field_key for item in plan.fields),
                                plan=plan)

    def current_canonical(self, canonical_key: str) -> Mapping[str, Any] | None:
        """One canonical variant's CURRENT state, from the authoritative view."""
        return self._repository.get_canonical_catalog_variant(canonical_key)

    def field_provenance(self, variant_id: Any) -> list[Mapping[str, Any]]:
        """Every promoted fact of one canonical variant, oldest revision first."""
        return list(self._repository.list_canonical_field_provenance(variant_id))


__all__ = ["PROMOTABLE_CANDIDATE_STATUS", "PROMOTION_REASONS", "CanonicalPromotion",
           "CatalogPromotionError", "FieldEvidence", "PromotionOutcome", "PromotionPlan",
           "build_promotion_plan", "field_evidence_for"]

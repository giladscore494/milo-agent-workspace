"""The single, application-owned Swarm V2 product-outcome contract.

Technical execution finishing is NOT a product result. This module owns the
one deterministic decision that separates the five distinct meanings the
engine previously collapsed into ``status: complete``:

1. the engine executed correctly,
2. it produced a useful verified result,
3. it produced a useful result with unresolved items,
4. it produced no usable result at all,
5. a trusted source explicitly proved there is nothing to find.

Every value here is chosen by application code from a static allowlist after
all known information is available. The Commander, the Worker model, the
Verifier model, run input and arbitrary durable metadata never reach it: they
supply *evidence and verdicts*, never the product status, the result kind or
the durable run status. This module performs no I/O and holds no state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Literal, Mapping

from .contracts import StrictContract

# --- static vocabulary -------------------------------------------------------

#: The public product statuses. Deliberately unchanged from the existing
#: contract: no database status and no migration belong to this decision.
PRODUCT_STATUSES = frozenset({"complete", "partial_success"})

#: The narrow, statically allowlisted product-result classification.
RESULT_KINDS = frozenset({
    "usable_result",      # at least one usable verified field, nothing outstanding
    "partial_result",     # at least one usable verified field, items outstanding
    "no_usable_result",   # nothing usable was verified and nothing was disproved
    "not_found",          # a trusted typed signal proved there is no match
})

#: The only self-consistent pairings. A status and a result kind can never
#: contradict one another because only these four pairs can be constructed,
#: and the worker re-validates the pair before it maps a durable run status.
ALLOWED_OUTCOMES: frozenset[tuple[str, str]] = frozenset({
    ("complete", "usable_result"),
    ("complete", "not_found"),
    ("partial_success", "partial_result"),
    ("partial_success", "no_usable_result"),
})

#: Product status -> durable run status. The durable terminal vocabulary is
#: unchanged (backend.runtime.RUN_STATES); this is a lookup, never a guess.
DURABLE_RUN_STATUS: Mapping[str, str] = {
    "complete": "completed",
    "partial_success": "partial_success",
}

#: The bounded, static empty-result marker. It carries no prompt, provider
#: response, exception text, source fragment or reasoning -- only this code.
NO_USABLE_RESULT_CODE = "NO_USABLE_RESULT"

#: Static allowlist of trusted negative-result signals. A negative result is
#: an *application/tool* fact, so only codes named here may ever produce
#: ``not_found``.
TRUSTED_NEGATIVE_CODES = frozenset({"TRUSTED_SOURCE_NO_MATCH"})


class ProductOutcomeError(ValueError):
    """A safe, provider-neutral product-outcome contract violation."""


class TrustedNegativeResult(StrictContract):
    """A typed, trusted, tool-backed proof that no matching result exists.

    NOTHING IN THIS REPOSITORY CONSTRUCTS ONE YET. The R1 scope deliberately
    stops at defining the boundary: real vehicle tools and the tool-call
    contract land in later remediations, and until a registered tool can
    return a typed "no match" signal there is no trustworthy producer.
    ``finalize_product_outcome`` therefore always receives ``None`` from the
    engine, and ``not_found`` is unreachable in production. It is never
    inferred from missing evidence, an empty tool result, model prose or the
    absence of fields.
    """

    code: Literal["TRUSTED_SOURCE_NO_MATCH"]
    source_id: str
    task_id: str


@dataclass(frozen=True)
class ProductOutcome:
    """The decided outcome, before it is rendered as the public payload."""

    status: str
    result_kind: str


def _usable_field_count(fields: Mapping[str, Any]) -> int:
    """Count field keys that actually carry a verified entry.

    An empty mapping is not a useful result, and neither is a field key whose
    value is an empty collection: usefulness is the presence of at least one
    verified entry, never the presence of a key.
    """
    return sum(1 for value in fields.values()
               if isinstance(value, (list, tuple)) and len(value) > 0)


def decide_outcome(*, usable_fields: bool, has_blocking_items: bool,
                   trusted_negative: TrustedNegativeResult | None = None) -> ProductOutcome:
    """Return the one truthful outcome for a finished Swarm V2 run.

    | usable verified field | blocking/review items | trusted negative | outcome                         |
    | --------------------- | --------------------- | ---------------- | ------------------------------- |
    | yes                   | no                    | no               | complete / usable_result        |
    | yes                   | yes                   | any              | partial_success / partial_result|
    | no                    | no                    | yes              | complete / not_found            |
    | no                    | any                   | no               | partial_success / no_usable_result|

    A trusted negative alongside unresolved items is NOT a confirmed negative:
    the run could not finish establishing it, so it stays
    ``partial_success / no_usable_result``.
    """
    if usable_fields:
        return ProductOutcome("partial_success", "partial_result") if has_blocking_items \
            else ProductOutcome("complete", "usable_result")
    if trusted_negative is not None and not has_blocking_items:
        # Fail closed on anything that is not the typed signal. A mapping,
        # a string or a model-supplied lookalike is exactly what must never
        # be able to declare a confirmed negative.
        if not isinstance(trusted_negative, TrustedNegativeResult) or \
                trusted_negative.code not in TRUSTED_NEGATIVE_CODES:
            raise ProductOutcomeError("untrusted negative-result signal")
        return ProductOutcome("complete", "not_found")
    return ProductOutcome("partial_success", "no_usable_result")


def _with_marker(review: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the review list carrying exactly one static marker, last.

    Idempotent by construction rather than by a guard: any marker already
    present is dropped and one canonical marker is appended. A rebuild, a
    resume or a re-finalization therefore always produces the same single
    trailing marker, which is exactly the shape validate_product_outcome
    requires -- the finalizer and the validator cannot drift apart.
    """
    kept = [item for item in review
            if not (isinstance(item, Mapping) and item.get("code") == NO_USABLE_RESULT_CODE)]
    return [*kept, {"code": NO_USABLE_RESULT_CODE}]


def finalize_product_outcome(*, fields: Mapping[str, Any],
                             verdict_review: Iterable[Mapping[str, Any]] = (),
                             task_failures: Iterable[Mapping[str, Any]] = (),
                             coverage_gaps: Iterable[Mapping[str, Any]] = (),
                             conflict_claim_ids: Iterable[str] = (),
                             unverified_claim_ids: Iterable[str] = (),
                             trusted_negative: TrustedNegativeResult | None = None,
                             ) -> dict[str, Any]:
    """Assemble THE public Swarm V2 product output. One decision, one place.

    Callers supply only facts -- the verified fields, the review verdicts, the
    task failures, the coverage gaps, the conflicting claims and the claims
    that did not verify. The status, the result kind and the review marker are
    decided here and nowhere else, so ``status``, ``result_kind``, ``fields``
    and ``needs_review`` cannot contradict one another and no later stage has
    to mutate or re-guess the result.

    ``needs_review`` preserves its existing composition and order exactly:
    verdict review items first (already ordered by the builder), then task
    failures, then coverage gaps, with the static marker last when it applies.
    """
    review = [*verdict_review, *task_failures, *coverage_gaps]
    has_blocking_items = bool(review or list(conflict_claim_ids) or list(unverified_claim_ids))
    outcome = decide_outcome(usable_fields=_usable_field_count(fields) > 0,
                             has_blocking_items=has_blocking_items,
                             trusted_negative=trusted_negative)
    if outcome.result_kind == "no_usable_result":
        review = _with_marker(review)
    return {"status": outcome.status, "result_kind": outcome.result_kind,
            "fields": dict(fields), "needs_review": review}


def validate_product_outcome(result: Any) -> ProductOutcome:
    """Validate a Swarm V2 product outcome, or refuse it.

    The outer worker calls this instead of inspecting dictionary truthiness:
    a payload only maps to a durable run status when it is EXACTLY a payload
    finalize_product_outcome could have produced. Anything else -- a missing
    key, an unknown or non-text vocabulary entry, a status that contradicts
    the result kind, the verified fields or the review items -- is a contract
    violation, never a run that quietly succeeds.

    This function is TOTAL: every rejection is a ProductOutcomeError. It runs
    after the engine's exception boundary in backend/worker/main.py, so an
    incidental TypeError or KeyError here would escape execute_run and leave
    the run with no durable terminal classification at all -- bypassing the
    very SWARM_V2_OUTCOME_INVALID path that exists to catch this. Every value
    is therefore type-checked before it is used as a set member, a mapping key
    or an index.
    """
    if not isinstance(result, Mapping):
        raise ProductOutcomeError("product outcome must be a mapping")
    status, result_kind = result.get("status"), result.get("result_kind")
    # Types first. An unhashable value (`status: []`) would raise TypeError
    # from the allowlist lookup below rather than failing closed.
    if not isinstance(status, str) or not isinstance(result_kind, str):
        raise ProductOutcomeError("product outcome vocabulary must be text")
    if status not in PRODUCT_STATUSES or result_kind not in RESULT_KINDS:
        raise ProductOutcomeError("product outcome vocabulary is not allowlisted")
    if (status, result_kind) not in ALLOWED_OUTCOMES:
        raise ProductOutcomeError("product status contradicts the result kind")
    fields, review = result.get("fields"), result.get("needs_review")
    if not isinstance(fields, Mapping) or not isinstance(review, list) or \
            any(not isinstance(item, Mapping) for item in review):
        raise ProductOutcomeError("product outcome is structurally invalid")
    if (_usable_field_count(fields) > 0) != (result_kind in
                                             {"usable_result", "partial_result"}):
        raise ProductOutcomeError("product result kind contradicts the verified fields")
    # `complete` is reachable only with nothing outstanding, so a complete
    # outcome carrying review items contradicts its own status.
    if status == "complete" and review:
        raise ProductOutcomeError("a complete outcome cannot carry review items")
    markers = [index for index, item in enumerate(review)
               if item.get("code") == NO_USABLE_RESULT_CODE]
    if result_kind == "no_usable_result":
        # Exactly one marker, last, and byte-for-byte the static entry: no
        # extra keys can smuggle prose in beside the code.
        if markers != [len(review) - 1] or review[-1] != {"code": NO_USABLE_RESULT_CODE}:
            raise ProductOutcomeError(
                "no_usable_result requires exactly one trailing static marker")
    elif markers:
        raise ProductOutcomeError("the empty-result marker contradicts the result kind")
    # `partial_result` deliberately has NO review-item requirement: a rejected
    # claim makes a run partial without producing a review entry of its own.
    return ProductOutcome(status, result_kind)


def durable_run_status(result: Any) -> str:
    """Map a validated Swarm V2 product outcome to its durable run status."""
    return DURABLE_RUN_STATUS[validate_product_outcome(result).status]


__all__ = ["ALLOWED_OUTCOMES", "DURABLE_RUN_STATUS", "NO_USABLE_RESULT_CODE",
           "PRODUCT_STATUSES", "RESULT_KINDS", "TRUSTED_NEGATIVE_CODES",
           "ProductOutcome", "ProductOutcomeError", "TrustedNegativeResult",
           "decide_outcome", "durable_run_status", "finalize_product_outcome",
           "validate_product_outcome"]

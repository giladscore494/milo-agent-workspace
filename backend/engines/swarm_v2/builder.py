"""Deterministic final result assembly; this module has no write capability."""
from __future__ import annotations
from typing import Any, Iterable, Mapping
from .contracts import EvidenceReference, VerificationVerdict
from .outcome import TrustedNegativeResult, finalize_product_outcome


class FinalBuilder:
    """Assemble the ONE public Swarm V2 product output.

    The builder owns assembly, not judgement: it derives the verified fields
    and the review items from evidence and verdicts, then hands every known
    fact to the canonical outcome policy, which decides ``status`` and
    ``result_kind``. The engine passes the facts only IT knows -- task
    failures, coverage gaps and conflicting claims -- into this same call, so
    the final payload is constructed exactly once and never mutated afterwards
    by the engine or re-interpreted by the outer worker.
    """

    def build(self, evidence: Iterable[EvidenceReference],
              verdicts: Iterable[VerificationVerdict], *,
              task_failures: Iterable[Mapping[str, Any]] = (),
              coverage_gaps: Iterable[Mapping[str, Any]] = (),
              conflict_claim_ids: Iterable[str] = (),
              trusted_negative: TrustedNegativeResult | None = None) -> dict:
        verdict_list = list(verdicts)
        verdict_by_claim = {item.claim_id: item for item in verdict_list}
        fields, review = {}, []
        for item in sorted(evidence, key=lambda x: (x.field, x.claim_id)):
            verdict = verdict_by_claim.get(item.claim_id)
            trace = {"claim_id": item.claim_id, "source_id": item.source_id,
                     "run_id": item.run_id, "task_id": item.task_id,
                     "scope": {"entity": item.entity, "field": item.field,
                               "geography": item.geography, "market": item.market,
                               "time_scope": item.time_scope}}
            if verdict and verdict.verdict == "verified" and item.supported:
                fields.setdefault(item.field, []).append({"value": item.value, "provenance": trace})
            elif verdict and verdict.verdict == "needs_review":
                review.append({"field": item.field, "value": item.value,
                               "reason": verdict.reason, "provenance": trace})
        return finalize_product_outcome(
            fields=fields,
            verdict_review=sorted(review, key=lambda x: (x["field"], x["provenance"]["claim_id"])),
            task_failures=task_failures, coverage_gaps=coverage_gaps,
            conflict_claim_ids=conflict_claim_ids,
            # A rejected or needs_review verdict is an unresolved item even
            # when it produced no review entry of its own.
            unverified_claim_ids=[item.claim_id for item in verdict_list
                                  if item.verdict != "verified"],
            trusted_negative=trusted_negative)

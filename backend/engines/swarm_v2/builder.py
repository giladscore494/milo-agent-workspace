"""Deterministic final result assembly; this module has no write capability."""
from __future__ import annotations
from typing import Any, Iterable, Mapping
from .contracts import EvidenceReference, VerificationVerdict
from .current_verdict import current_verdict_by_claim
from .failures import NOT_DEGRADABLE, log_step_degraded
from .outcome import (DEGRADED_STEP_CODES, TrustedNegativeResult, degraded_review_item,
                      finalize_product_outcome)


class FinalBuilder:
    """Assemble the ONE public Swarm V2 product output.

    The builder owns assembly, not judgement: it derives the verified fields
    and the review items from evidence and verdicts, then hands every known
    fact to the canonical outcome policy, which decides ``status`` and
    ``result_kind``. The engine passes the facts only IT knows -- task
    failures, coverage gaps and conflicting claims -- into this same call, so
    the final payload is constructed exactly once and never mutated afterwards
    by the engine or re-interpreted by the outer worker.

    Phase 2: when the run has at least one typed register outcome
    (``candidate_outcomes``), the payload also carries the vehicle-centric
    view -- ``vehicles``, ``unresolved_groups`` and ``summary`` -- assembled
    by the pure `VehicleCatalogResultAssembler` from the SAME inputs. Those
    keys are additive: every existing key keeps its exact shape and value,
    and a payload without a register outcome is byte-for-byte what it was.
    """

    def build(self, evidence: Iterable[EvidenceReference],
              verdicts: Iterable[VerificationVerdict], *,
              task_failures: Iterable[Mapping[str, Any]] = (),
              coverage_gaps: Iterable[Mapping[str, Any]] = (),
              conflict_claim_ids: Iterable[str] = (),
              trusted_negative: TrustedNegativeResult | None = None,
              candidate_outcomes: Iterable[Mapping[str, Any]] = ()) -> dict:
        verdict_list = list(verdicts)
        # ONE verdict per claim, resolved by the shared rule rather than by
        # "whichever came last in the list": a stray second verdict must not
        # be able to raise a claim to `verified` by arriving later.
        verdict_by_claim = current_verdict_by_claim(verdict_list)
        evidence = list(evidence)
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
        outcomes = [dict(item) for item in candidate_outcomes]
        failures, gaps = list(task_failures), list(coverage_gaps)
        view: dict[str, Any] | None = None
        if outcomes:
            # PR-X: the vehicle-centric view is ADDITIVE, so a failure while
            # assembling it never costs the run its result. The view is simply
            # omitted -- every existing key keeps its value -- and the run
            # carries ASSEMBLER_FAILED, which makes it partial_success.
            try:
                # Imported here: the catalog package reads the engine's
                # contracts, so a module-level import would make the two
                # initialize each other.
                from backend.catalog.result.assembler import VehicleCatalogResultAssembler

                view = VehicleCatalogResultAssembler().assemble(
                    evidence=evidence, verdicts=verdict_list, candidate_outcomes=outcomes,
                    coverage_gaps=gaps, task_failures=failures)
            except NOT_DEGRADABLE:
                raise
            except Exception as exc:
                log_step_degraded(DEGRADED_STEP_CODES["ASSEMBLER_FAILED"], "ASSEMBLER_FAILED",
                                  type(exc).__name__)
                view = None
                gaps = [*gaps, degraded_review_item("ASSEMBLER_FAILED")]
        final = finalize_product_outcome(
            fields=fields,
            verdict_review=sorted(review, key=lambda x: (x["field"], x["provenance"]["claim_id"])),
            task_failures=failures, coverage_gaps=gaps,
            conflict_claim_ids=conflict_claim_ids,
            # A rejected or needs_review verdict is an unresolved item even
            # when it produced no review entry of its own.
            unverified_claim_ids=[item.claim_id for item in verdict_list
                                  if item.verdict != "verified"],
            trusted_negative=trusted_negative,
            candidate_outcomes=outcomes)
        if view is not None:
            final.update(view)
        return final

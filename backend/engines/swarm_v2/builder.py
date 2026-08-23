"""Deterministic final result assembly; this module has no write capability."""
from __future__ import annotations
from typing import Iterable
from .contracts import EvidenceReference, VerificationVerdict


class FinalBuilder:
    def build(self, evidence: Iterable[EvidenceReference], verdicts: Iterable[VerificationVerdict]) -> dict:
        verdict_by_claim = {item.claim_id: item for item in verdicts}
        fields, review = {}, []
        for item in sorted(evidence, key=lambda x: (x.field, x.claim_id)):
            verdict = verdict_by_claim.get(item.claim_id)
            trace = {"claim_id": item.claim_id, "source_id": item.source_id,
                     "run_id": item.run_id, "task_id": item.task_id}
            if verdict and verdict.verdict == "verified" and item.supported:
                fields.setdefault(item.field, []).append({"value": item.value, "provenance": trace})
            elif verdict and verdict.verdict == "needs_review":
                review.append({"field": item.field, "value": item.value,
                               "reason": verdict.reason, "provenance": trace})
        return {"status": "completed", "fields": fields,
                "needs_review": sorted(review, key=lambda x: (x["field"], x["provenance"]["claim_id"]))}

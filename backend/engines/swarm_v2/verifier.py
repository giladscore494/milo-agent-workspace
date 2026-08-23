"""Structured verification using the shared ModelGateway only."""
from __future__ import annotations
import json
from typing import Iterable
from .contracts import EvidenceReference, VerificationVerdict
from .model_gateway import ModelGateway


class Verifier:
    def __init__(self, *, gateway: ModelGateway, model: str):
        self._gateway, self._model = gateway, model

    def verify(self, evidence: Iterable[EvidenceReference], *, conflict_claim_ids: set[str] | None = None) -> list[VerificationVerdict]:
        items = sorted(evidence, key=lambda x: x.claim_id)
        conflicts = conflict_claim_ids or set()
        candidates = [item for item in items if item.supported and item.claim_id not in conflicts]
        verdicts = [VerificationVerdict(claim_id=item.claim_id, verdict="rejected", reason="unsupported claim")
                    for item in items if not item.supported]
        verdicts += [VerificationVerdict(claim_id=item.claim_id, verdict="needs_review", reason="unresolved conflict")
                     for item in items if item.claim_id in conflicts]
        if candidates:
            response = self._gateway.call(model=self._model, agent="verifier", phase="verification",
                messages=[{"role": "system", "content": "Return JSON {verdicts:[{claim_id,verdict,reason}]}; verdict is verified, needs_review, or rejected."},
                          {"role": "user", "content": json.dumps([x.model_dump(mode="json") for x in candidates], sort_keys=True)}],
                response_format={"type": "json_object"})
            raw = response if isinstance(response, dict) else json.loads(response.choices[0].message.content)
            parsed = [VerificationVerdict.model_validate(v) for v in raw.get("verdicts", [])]
            by_id = {v.claim_id: v for v in parsed}
            verdicts.extend(by_id.get(item.claim_id, VerificationVerdict(
                claim_id=item.claim_id, verdict="rejected", reason="verifier omitted claim")) for item in candidates)
        return sorted(verdicts, key=lambda x: x.claim_id)

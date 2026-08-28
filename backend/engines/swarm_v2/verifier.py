"""Structured verification using the shared ModelGateway only.

Verification is deterministic, bounded and resumable:

* unsupported and unresolved-conflict claims are settled locally and never
  reach a model call;
* the remaining candidates are partitioned into deterministic batches that
  respect BOTH a claim-count bound and a serialized-byte bound;
* batches are executed sequentially through the SAME ModelGateway, so every
  batch stays under the existing BudgetTracker, reservation, provider
  scheduling and cancellation authority;
* each completed batch is handed to the caller through one progress callback
  so durable progress belongs to the engine, not to this module.

This module performs no database access and never persists raw provider
material: only validated VerificationVerdict objects leave it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

from .contracts import EvidenceReference, VerificationVerdict
from .model_gateway import ModelGateway
from .evidence import safe_durable_value


# --- explicit, version-auditable bounds --------------------------------------
#
# One authoritative location; deliberately NOT environment-tunable and never
# model-authored. Measured against the serialized EvidenceReference shape:
# a small reference is ~210 B, a typical one ~420 B, and one filled to every
# identity field limit with a 500 B value is ~2.2 KB. 25 claims is therefore
# ~10 KB of typical payload, and the byte bound splits earlier whenever the
# references are unusually large.
MAX_VERIFIER_CLAIMS_PER_BATCH = 25
MAX_VERIFIER_BATCH_JSON_BYTES = 32_768

# Static, provider-neutral failure codes. Every verifier contract violation
# collapses into one of these: the offending payload, the decoder message and
# the schema diagnostic are dropped at the boundary that classifies them.
VERIFIER_REASONS = frozenset({
    "VERIFIER_CANDIDATE_TOO_LARGE",
    "VERIFIER_EVIDENCE_DUPLICATE_CLAIM",
    "VERIFIER_RESPONSE_INVALID",
    "VERIFIER_RESPONSE_DUPLICATE_CLAIM",
    "VERIFIER_RESPONSE_UNKNOWN_CLAIM",
    "VERIFIER_STATE_UNKNOWN_CLAIM",
    "VERIFIER_STATE_INVALID_VERDICT",
    "VERIFIER_STATE_INCOMPATIBLE_VERDICT",
})

UNSUPPORTED_VERDICT = ("rejected", "unsupported claim")
CONFLICT_VERDICT = ("needs_review", "unresolved conflict")
OMITTED_VERDICT = ("rejected", "verifier omitted claim")

_SYSTEM_PROMPT = (
    "Return JSON {verdicts:[{claim_id,verdict,reason}]}; verdict is verified, "
    "needs_review, or rejected. Return exactly one verdict for every claim_id "
    "in the request and never a claim_id that is not in the request."
)


class VerifierContractError(ValueError):
    """A verifier contract failure carrying ONLY a static code.

    Raw responses, invalid JSON, schema diagnostics and provider-generated
    claim identifiers never reach this exception, so its message is safe for
    durable state, run events and telemetry.
    """

    MESSAGES = {
        "VERIFIER_CANDIDATE_TOO_LARGE": "one evidence reference exceeds the verifier batch byte limit",
        "VERIFIER_EVIDENCE_DUPLICATE_CLAIM": "evidence contains a duplicate claim identity",
        "VERIFIER_RESPONSE_INVALID": "verifier response does not satisfy the verdict contract",
        "VERIFIER_RESPONSE_DUPLICATE_CLAIM": "verifier response repeats a claim identity",
        "VERIFIER_RESPONSE_UNKNOWN_CLAIM": "verifier response contains a claim outside its batch",
        "VERIFIER_STATE_UNKNOWN_CLAIM": "verifier checkpoint contains an unknown claim",
        "VERIFIER_STATE_INVALID_VERDICT": "verifier checkpoint contains a malformed verdict",
        "VERIFIER_STATE_INCOMPATIBLE_VERDICT": "verifier checkpoint contradicts a deterministic verdict",
    }

    def __init__(self, reason_code: str):
        if reason_code not in VERIFIER_REASONS:
            raise ValueError("verifier reason must come from the static allowlist")
        self.reason_code = reason_code
        self.safe_message = self.MESSAGES[reason_code]
        super().__init__(self.safe_message)


def serialize_verifier_candidates(candidates: Sequence[EvidenceReference]) -> str:
    """The ONE deterministic candidate serialization.

    Batch sizing and the actual request body are both built from this helper,
    so the enforced byte bound cannot drift from what is sent.
    """
    return json.dumps([item.model_dump(mode="json") for item in candidates],
                      sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def verifier_payload_bytes(candidates: Sequence[EvidenceReference]) -> int:
    """UTF-8 byte length of the exact payload the Verifier would send."""
    return len(serialize_verifier_candidates(candidates).encode("utf-8"))


def build_verifier_batches(
    candidates: Iterable[EvidenceReference], *,
    max_claims: int = MAX_VERIFIER_CLAIMS_PER_BATCH,
    max_serialized_bytes: int = MAX_VERIFIER_BATCH_JSON_BYTES,
) -> list[list[EvidenceReference]]:
    """Partition candidates into deterministic, doubly bounded batches.

    Pure: no model call, no database access, no global state. Candidates are
    sorted by claim_id and filled greedily in that order, so the same
    candidate set always yields the same batch membership and batch order.
    A candidate that cannot fit a batch on its own fails closed BEFORE any
    batch is executed.
    """
    ordered = sorted(candidates, key=lambda item: item.claim_id)
    batches: list[list[EvidenceReference]] = []
    current: list[EvidenceReference] = []
    for item in ordered:
        if verifier_payload_bytes([item]) > max_serialized_bytes:
            # Never truncated, split across calls, stripped of fields or sent
            # oversized anyway: an unbatchable reference is a contract failure.
            raise VerifierContractError("VERIFIER_CANDIDATE_TOO_LARGE")
        extended = [*current, item]
        if current and (len(extended) > max_claims or
                        verifier_payload_bytes(extended) > max_serialized_bytes):
            batches.append(current)
            current = [item]
        else:
            current = extended
    if current:
        batches.append(current)
    return batches


@dataclass(frozen=True)
class VerifierProgress:
    """One durable-progress unit handed to the engine.

    batch_index 0 carries the deterministic and resumed verdicts published
    before the first model-backed batch; 1..batch_count carry one completed
    model batch each.
    """

    verdicts: tuple[VerificationVerdict, ...]
    batch_index: int
    batch_count: int
    claim_count: int


@dataclass(frozen=True)
class VerificationPlan:
    """The deterministic shape of one verification pass."""

    settled: tuple[VerificationVerdict, ...]
    batches: tuple[tuple[EvidenceReference, ...], ...]


def _deterministic_verdicts(items: Sequence[EvidenceReference],
                            conflicts: frozenset[str]) -> dict[str, VerificationVerdict]:
    """Settle unsupported and unresolved-conflict claims locally.

    An unsupported claim is rejected even when its scope also conflicts:
    exactly one verdict per claim is required, and surfacing an unsupported
    value for review would leak evidence the board already refused to back.
    """
    settled: dict[str, VerificationVerdict] = {}
    for item in items:
        verdict, reason = (UNSUPPORTED_VERDICT if not item.supported else
                           CONFLICT_VERDICT if item.claim_id in conflicts else (None, None))
        if verdict is not None:
            settled[item.claim_id] = VerificationVerdict(
                claim_id=item.claim_id, verdict=verdict, reason=reason)
    return settled


def _resumed_verdicts(existing: Mapping[str, Any], known: frozenset[str],
                      deterministic: Mapping[str, VerificationVerdict]) -> dict[str, VerificationVerdict]:
    """Validate checkpointed verifier progress or fail closed.

    An empty map and a fully populated final map are both valid; incompatible
    progress is never silently discarded.
    """
    resumed: dict[str, VerificationVerdict] = {}
    for claim_id, raw in existing.items():
        if claim_id not in known:
            raise VerifierContractError("VERIFIER_STATE_UNKNOWN_CLAIM")
        if isinstance(raw, VerificationVerdict):
            verdict = raw
        else:
            try:
                verdict = VerificationVerdict.model_validate(raw)
            except (TypeError, ValueError):
                # `from None`: the validation message quotes stored material.
                raise VerifierContractError("VERIFIER_STATE_INVALID_VERDICT") from None
        if verdict.claim_id != claim_id:
            raise VerifierContractError("VERIFIER_STATE_INVALID_VERDICT")
        expected = deterministic.get(claim_id)
        if expected is not None and (verdict.verdict, verdict.reason) != (expected.verdict, expected.reason):
            raise VerifierContractError("VERIFIER_STATE_INCOMPATIBLE_VERDICT")
        resumed[claim_id] = verdict
    return resumed


def plan_verification(
    evidence: Iterable[EvidenceReference], *,
    conflict_claim_ids: Iterable[str] | None = None,
    existing_verdicts: Mapping[str, Any] | None = None,
    max_claims: int = MAX_VERIFIER_CLAIMS_PER_BATCH,
    max_serialized_bytes: int = MAX_VERIFIER_BATCH_JSON_BYTES,
) -> VerificationPlan:
    """Return the settled verdicts and the remaining model-backed batches.

    Pure and deterministic: the engine uses it to size the exact pre-verifier
    model-call requirement and the Verifier uses it to execute, so the two can
    never disagree about how many verifier calls a pass needs.
    """
    items = sorted(evidence, key=lambda item: item.claim_id)
    if len({item.claim_id for item in items}) != len(items):
        raise VerifierContractError("VERIFIER_EVIDENCE_DUPLICATE_CLAIM")
    conflicts = frozenset(conflict_claim_ids or ())
    deterministic = _deterministic_verdicts(items, conflicts)
    resumed = _resumed_verdicts(existing_verdicts or {},
                                frozenset(item.claim_id for item in items), deterministic)
    settled = {**deterministic, **resumed}
    candidates = [item for item in items if item.claim_id not in settled]
    batches = build_verifier_batches(candidates, max_claims=max_claims,
                                     max_serialized_bytes=max_serialized_bytes)
    return VerificationPlan(
        settled=tuple(settled[claim_id] for claim_id in sorted(settled)),
        batches=tuple(tuple(batch) for batch in batches),
    )


def parse_verifier_batch(content: Any, expected: Sequence[str]) -> list[VerificationVerdict]:
    """Map ONE batch response onto exactly the claim identities it was sent.

    A foreign or repeated claim identity is a provider contract violation and
    fails closed rather than being resolved by first-wins, last-wins or dict
    overwrite. Omission stays local: an expected claim the batch did not
    answer becomes the existing deterministic omission verdict.
    """
    if isinstance(content, Mapping):
        document: Any = dict(content)
    elif isinstance(content, (str, bytes, bytearray)):
        try:
            document = json.loads(content)
        except (TypeError, ValueError):
            # `from None`: the decoder exception carries the raw document.
            raise VerifierContractError("VERIFIER_RESPONSE_INVALID") from None
    else:
        raise VerifierContractError("VERIFIER_RESPONSE_INVALID")
    if not isinstance(document, Mapping):
        raise VerifierContractError("VERIFIER_RESPONSE_INVALID")
    entries = document.get("verdicts", [])
    if not isinstance(entries, list):
        raise VerifierContractError("VERIFIER_RESPONSE_INVALID")
    allowed, by_id = set(expected), {}
    for entry in entries:
        try:
            verdict = VerificationVerdict.model_validate(entry)
        except (TypeError, ValueError):
            # Dropped deliberately: the message quotes provider material.
            raise VerifierContractError("VERIFIER_RESPONSE_INVALID") from None
        if verdict.claim_id not in allowed:
            # The unknown identity is provider-generated and is NOT reported.
            raise VerifierContractError("VERIFIER_RESPONSE_UNKNOWN_CLAIM")
        if verdict.claim_id in by_id:
            raise VerifierContractError("VERIFIER_RESPONSE_DUPLICATE_CLAIM")
        by_id[verdict.claim_id] = verdict
    omitted, omitted_reason = OMITTED_VERDICT
    return [by_id[claim_id] if claim_id in by_id else
            VerificationVerdict(claim_id=claim_id, verdict=omitted, reason=omitted_reason)
            for claim_id in expected]


class Verifier:
    def __init__(self, *, gateway: ModelGateway, model: str):
        self._gateway, self._model = gateway, model

    def verify(self, evidence: Iterable[EvidenceReference], *,
               conflict_claim_ids: set[str] | None = None,
               existing_verdicts: Mapping[str, Any] | None = None,
               batch_completed: Callable[[VerifierProgress], None] | None = None,
               ) -> list[VerificationVerdict]:
        """Return exactly one verdict per evidence claim, sorted by claim_id.

        Batches run SEQUENTIALLY: one gateway call is in flight at a time
        whatever concurrency the ProviderScheduler allows. There is no
        verifier repair loop -- a batch that violates the verdict contract
        fails closed under the existing verifier failure semantics.
        """
        plan = plan_verification(evidence, conflict_claim_ids=conflict_claim_ids,
                                 existing_verdicts=existing_verdicts)
        verdicts = list(plan.settled)
        batch_count = len(plan.batches)
        if batch_count and batch_completed is not None:
            # Deterministic and resumed verdicts become durable BEFORE the
            # first paid batch, so every checkpoint holds one coherent map.
            batch_completed(VerifierProgress(verdicts=plan.settled, batch_index=0,
                                             batch_count=batch_count,
                                             claim_count=len(plan.settled)))
        for index, batch in enumerate(plan.batches, start=1):
            resolved = self._verify_batch(batch)
            verdicts.extend(resolved)
            if batch_completed is not None:
                batch_completed(VerifierProgress(verdicts=tuple(resolved), batch_index=index,
                                                 batch_count=batch_count,
                                                 claim_count=len(resolved)))
        return sorted(verdicts, key=lambda item: item.claim_id)

    def _verify_batch(self, batch: Sequence[EvidenceReference]) -> list[VerificationVerdict]:
        """One real semantic model call through the shared guarded gateway."""
        response = self._gateway.call(
            model=self._model, agent="verifier", phase="verification",
            messages=[{"role": "system", "content": _SYSTEM_PROMPT},
                      {"role": "user", "content": serialize_verifier_candidates(batch)}],
            response_format={"type": "json_object"})
        if isinstance(response, (dict, str, bytes, bytearray)):
            content: Any = response
        else:
            try:
                content = response.choices[0].message.content
            except (AttributeError, IndexError, TypeError):
                raise VerifierContractError("VERIFIER_RESPONSE_INVALID") from None
        resolved = parse_verifier_batch(content, [item.claim_id for item in batch])
        safe_durable_value([verdict.model_dump(mode="json") for verdict in resolved])
        return resolved

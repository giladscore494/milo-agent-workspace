"""Catalog PR3: the trusted server path from a Government read to a canonical fact.

What this connects
------------------

Every piece of the promotion existed before this module and none of them was
joined up, so a production run could research a vehicle, verify the register's
statement about it, and still leave the canonical catalog empty. This is the
join:

    Government tool result  (server-produced, Registry-validated)
      -> R3 evidence        (the registered mapper, through the Evidence Board)
      -> R4 verdict         (the Verifier, through the same Board)
      -> evidence LINK      (link_catalog_candidate_evidence_guarded)
      -> REVIEWED status    (record_catalog_candidate_guarded)
      -> promotion PLAN     (backend/catalog/promotion.py)
      -> canonical row      (promote_catalog_variant_guarded)

What a model can and cannot do here
-----------------------------------

A model can cause the FIRST arrow and nothing else. It may ask the Commander
for research, and a plan may call `catalog.government_vehicle.resolve_variant`
because that is a registered read capability. Everything after it is trusted
server code running in this worker's own wiring:

*   promotion is NOT a Tool. There is no registered capability that performs
    one, so `write_approved` and a `tool:write:<name>` grant never enter the
    picture -- a plan cannot request a promotion because there is nothing to
    request;
*   every input is server-produced. The candidate identity comes from the
    durable row the Tool read, not from anything a model wrote; the claims and
    verdicts come from the Board; the canonical keys are DERIVED by
    `backend/catalog/payloads.py`;
*   every write goes through the same lease-guarded RPC as every other durable
    catalog write, and the database re-checks the whole support chain for every
    writer regardless of what this module believes.

What it deliberately does NOT do
--------------------------------

**It does not schedule anything.** It runs once, at the end of a run that
already happened, over that run's own Government resolutions. It starts no
capture, opens no socket and holds no credential; the Government refresh
operation stays unscheduled and is not called from here.

**It cannot widen a run.** It reads only what the run already produced, it
promotes at most one canonical variant per candidate the run resolved, and it
never queries for more candidates to work on.

**It never fails the run.** A refusal is what the durable catalog is FOR: an
ambiguous candidate, a field with no verified evidence, an unresolved conflict
and a lost lease are all legitimate outcomes of a research run. Each one is
returned as a static reason code, and the run's own result is untouched -- with
one exception, stated in `CatalogPromotionPipeline.promote`: a lost lease is an
infrastructure outcome and is re-raised, because a stale worker must not keep
writing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from backend.errors import AppError

from .government.evidence import GOVERNMENT_TOOL_NAME, RESOLVE_VARIANT_OPERATION
from .promotion import (LEASE_FAILURE_CODES, PROMOTION_REASONS, CanonicalPromotion,
                        CatalogPromotionError, PromotionOutcome, PROMOTABLE_CANDIDATE_STATUS,
                        build_promotion_plan, field_evidence_for)

#: How many candidates one run may promote. A research run resolves a handful
#: of vehicles; a run that somehow resolved thousands must not turn into an
#: unbounded write loop at the end of it, so the ledger stops REMEMBERING past
#: this and the extras are simply never promoted.
MAX_PROMOTIONS_PER_RUN = 25

#: Refusals this layer adds to the ones `backend/catalog/promotion.py` owns.
PIPELINE_REASONS: Mapping[str, str] = {
    "CATALOG_PROMOTION_NO_VERIFIED_EVIDENCE":
        "no verified verdict was settled for that candidate's evidence",
    "CATALOG_PROMOTION_SNAPSHOT_UNUSABLE":
        "the snapshot that candidate was read from cannot support a canonical fact",
    "CATALOG_PROMOTION_REFUSED":
        PROMOTION_REASONS["CATALOG_PROMOTION_REFUSED"],
}


@dataclass(frozen=True)
class CandidateEvidence:
    """One unambiguous Government resolution, and what it wrote for it.

    `candidate` is the durable row the Tool read, as the server produced it --
    never a model's restatement of it. `claims` are the claim ids the
    registered mapper's bundle became, in write order.
    """

    candidate: Mapping[str, Any]
    snapshot_key: str
    source_id: str
    claim_ids: tuple[str, ...] = ()

    @property
    def candidate_id(self) -> str:
        return str(self.candidate["candidate_id"])


@dataclass(frozen=True)
class PromotionAttempt:
    """What the pipeline did about ONE candidate, promoted or refused."""

    candidate_id: str
    candidate_key: str
    outcome: PromotionOutcome | None = None
    reason_code: str | None = None

    @property
    def promoted(self) -> bool:
        return self.outcome is not None

    @property
    def safe_message(self) -> str:
        if self.reason_code is None:
            return ""
        return PROMOTION_REASONS.get(self.reason_code) \
            or PIPELINE_REASONS.get(self.reason_code, "the durable catalog refused this promotion")

    def as_event(self) -> dict[str, Any]:
        """The bounded, browser-safe shape a run event carries.

        Ids and static codes only: no SQL message, no row, no value and no
        model text can reach a run event through here.
        """
        payload: dict[str, Any] = {"candidate_key": self.candidate_key,
                                   "promoted": self.promoted}
        if self.outcome is not None:
            payload["canonical_key"] = str(self.outcome.variant["canonical_key"])
            payload["promoted_fields"] = list(self.outcome.promoted_fields)
            payload["unsupported_fields"] = list(self.outcome.unsupported_fields)
            payload["replayed"] = bool(self.outcome.replayed)
        if self.reason_code is not None:
            payload["reason"] = self.reason_code
        return payload


class CatalogEvidenceLedger:
    """What this run's Government reads and verdicts produced, observed in order.

    TWO trusted seams feed it and nothing else can reach it: the tool-result
    sink, which already receives the server-produced result and therefore knows
    WHICH durable candidate each resolution was about, and the verdict sink,
    which already receives each settled verdict. Both are wired in
    `backend/worker/main.py`.

    It performs no write of its own and holds no repository handle. It is a
    record of what happened, which is exactly what the promotion path needs and
    could not otherwise obtain: a claim row does not say which candidate it is
    evidence for, and the only place that is known is the tool result.
    """

    def __init__(self, sink: Any, *, verdict_sink: Any,
                 max_candidates: int = MAX_PROMOTIONS_PER_RUN) -> None:
        self._sink = sink
        self._verdict_sink = verdict_sink
        self._max = max(0, int(max_candidates))
        self._observed: dict[str, CandidateEvidence] = {}
        self._verdicts: dict[str, dict[str, Any]] = {}

    # --- the ToolResultSink seam --------------------------------------------

    def __call__(self, record: Any) -> None:
        """Persist the result's evidence exactly as before, then remember it."""
        acquired = self._sink.acquire(record)
        if acquired is None:
            return
        if getattr(record, "tool", None) != GOVERNMENT_TOOL_NAME \
                or getattr(record, "operation", None) != RESOLVE_VARIANT_OPERATION:
            return
        result = record.result if isinstance(record.result, Mapping) else {}
        variants = result.get("variants") or []
        # Belt and braces: the mapper already declines anything that is not a
        # single resolved row, so an ambiguous answer never gets here. An
        # ambiguous candidate stays ambiguous.
        if result.get("match_count") != 1 or len(variants) != 1:
            return
        candidate = variants[0]
        provenance = result.get("provenance") or {}
        candidate_id = str(candidate.get("candidate_id") or "")
        if not candidate_id:
            return
        claims = tuple(str(row["id"]) for row in acquired.claims)
        held = self._observed.get(candidate_id)
        if held is not None:
            # A later resolution of the same candidate adds its claims rather
            # than replacing them. The SOURCE stays the first one: a second
            # source's claims are linked separately by the promotion step,
            # which reads each claim's own source from the claim itself.
            self._observed[candidate_id] = CandidateEvidence(
                candidate=held.candidate, snapshot_key=held.snapshot_key,
                source_id=held.source_id,
                claim_ids=held.claim_ids + tuple(item for item in claims
                                                 if item not in held.claim_ids))
            return
        if len(self._observed) >= self._max:
            return
        self._observed[candidate_id] = CandidateEvidence(
            candidate=dict(candidate), snapshot_key=str(provenance.get("snapshot_key") or ""),
            source_id=str(acquired.source["id"]), claim_ids=claims)

    @property
    def sink(self) -> Any:
        return self._sink

    # --- the verdict seam ----------------------------------------------------

    def record_verdict(self, verdict: Any) -> dict[str, Any]:
        """Persist ONE settled verdict exactly as before, then remember it."""
        row = self._verdict_sink(verdict)
        if isinstance(row, Mapping) and row.get("claim_id") is not None:
            self._verdicts[str(row["claim_id"])] = dict(row)
        return row

    # --- what the promotion path reads --------------------------------------

    @property
    def observations(self) -> tuple[CandidateEvidence, ...]:
        """The observed candidates, in the order the run resolved them."""
        return tuple(self._observed.values())

    def verdict_for(self, claim_id: Any) -> Mapping[str, Any] | None:
        return self._verdicts.get(str(claim_id))


class CatalogPromotionPipeline:
    """One run's Government evidence, carried through to canonical rows.

    Constructed by the trusted wiring that already holds the repository and the
    run's worker lease, exactly like the Evidence Board and for the same
    reason: every write is lease-guarded, idempotent and append-only, and this
    object holds no credential and no model client.
    """

    def __init__(self, repository: Any, lease: Any) -> None:
        self._repository = repository
        self._lease = lease
        self._promotion = CanonicalPromotion(repository, lease)

    @property
    def _lease_kwargs(self) -> dict[str, Any]:
        return {"worker_id": self._lease.worker_id, "attempt": self._lease.attempt,
                "lease_token": self._lease.lease_token}

    def promote(self, ledger: CatalogEvidenceLedger) -> tuple[PromotionAttempt, ...]:
        """Link, review, plan and promote every candidate the run resolved.

        Total and bounded: every observation produces exactly one
        `PromotionAttempt`, promoted or refused with a static reason, and the
        ledger already bounds how many there can be.

        A LOST LEASE is the one thing that escapes. It means this worker is no
        longer the run's writer, so continuing would be a stale worker writing
        canonical facts; it propagates to the worker's own lease handling
        exactly as every other guarded write's does.
        """
        attempts: list[PromotionAttempt] = []
        for observation in ledger.observations:
            try:
                attempts.append(self._promote_one(observation, ledger))
            except AppError as failure:
                if failure.code in LEASE_FAILURE_CODES:
                    raise
                attempts.append(PromotionAttempt(
                    candidate_id=observation.candidate_id,
                    candidate_key=str(observation.candidate.get("candidate_key") or ""),
                    reason_code="CATALOG_PROMOTION_REFUSED"))
        return tuple(attempts)

    # --- one candidate -------------------------------------------------------

    def _promote_one(self, observation: CandidateEvidence,
                     ledger: CatalogEvidenceLedger) -> PromotionAttempt:
        candidate_key = str(observation.candidate.get("candidate_key") or "")
        snapshot = self._snapshot(observation)
        if snapshot is None:
            return PromotionAttempt(candidate_id=observation.candidate_id,
                                    candidate_key=candidate_key,
                                    reason_code="CATALOG_PROMOTION_SNAPSHOT_UNUSABLE")

        # 1. LINK. Only a claim whose verdict is exactly `verified` is cited:
        #    a `needs_review` or `rejected` verdict is a real answer, and it is
        #    an answer AGAINST promoting.
        links = self._link(observation, ledger)
        if not links:
            return PromotionAttempt(candidate_id=observation.candidate_id,
                                    candidate_key=candidate_key,
                                    reason_code="CATALOG_PROMOTION_NO_VERIFIED_EVIDENCE")

        # 2. PLAN, against the candidate as if it were reviewed. The plan is
        #    what decides whether the candidate IS reviewable: it refuses
        #    unless every identity field the canonical row would state has its
        #    own verified evidence at exactly that value. Building it first is
        #    what keeps `ready_for_review` from becoming a status this code
        #    writes hopefully -- the transition below happens only when a
        #    complete, evidenced promotion is already in hand.
        proposed = {**dict(observation.candidate), "id": observation.candidate_id,
                    "candidate_key": candidate_key, "status": PROMOTABLE_CANDIDATE_STATUS}
        try:
            evidence = field_evidence_for(
                candidate=proposed, links=links,
                claims={str(row["id"]): row for row in self._claims(links)},
                verdicts={str(row["id"]): row for row in self._verdicts(links)})
            plan = build_promotion_plan(candidate=proposed, snapshot=snapshot,
                                        evidence=evidence)
        except CatalogPromotionError as refusal:
            return PromotionAttempt(candidate_id=observation.candidate_id,
                                    candidate_key=candidate_key,
                                    reason_code=refusal.reason_code)

        # 3. REVIEW. The durable status transition, through the same guarded
        #    RPC that created the candidate: it holds the re-presented row to
        #    the identity already stored under that key, so this can move the
        #    status and can never quietly become a different vehicle.
        reviewed = self._review(observation, snapshot)
        if reviewed.get("status") != PROMOTABLE_CANDIDATE_STATUS:
            return PromotionAttempt(candidate_id=observation.candidate_id,
                                    candidate_key=candidate_key,
                                    reason_code="CATALOG_PROMOTION_CANDIDATE_NOT_READY")

        # 4. PROMOTE, atomically, through the lease-guarded RPC.
        try:
            outcome = self._promotion.promote(plan)
        except CatalogPromotionError as refusal:
            return PromotionAttempt(candidate_id=observation.candidate_id,
                                    candidate_key=candidate_key,
                                    reason_code=refusal.reason_code)
        return PromotionAttempt(candidate_id=observation.candidate_id,
                                candidate_key=candidate_key, outcome=outcome)

    def _snapshot(self, observation: CandidateEvidence) -> Mapping[str, Any] | None:
        """The snapshot the candidate was read from, or None when unusable."""
        if not observation.snapshot_key:
            return None
        try:
            return self._repository.find_active_catalog_snapshot(
                str(observation.candidate.get("source_family") or "government"),
                str(observation.candidate.get("resource_id") or ""),
                observation.snapshot_key)
        except AppError:
            return None

    def _link(self, observation: CandidateEvidence,
              ledger: CatalogEvidenceLedger) -> list[dict[str, Any]]:
        """One evidence link per VERIFIED claim, idempotent on a derived key."""
        linked: list[dict[str, Any]] = []
        for claim_id in observation.claim_ids:
            verdict = ledger.verdict_for(claim_id)
            if verdict is None or verdict.get("verdict") != "verified":
                continue
            row = self._repository.link_catalog_candidate_evidence(
                self._lease.run_id,
                {"candidate_id": observation.candidate_id,
                 "candidate_key": str(observation.candidate.get("candidate_key") or ""),
                 "source_id": str(verdict.get("source_id") or observation.source_id),
                 "claim_id": str(claim_id), "verdict_id": str(verdict["id"])},
                **self._lease_kwargs)
            linked.append({**dict(row), "_claim": claim_id, "_verdict": dict(verdict)})
        return linked

    @staticmethod
    def _claims(links: list[dict[str, Any]]) -> list[Mapping[str, Any]]:
        """The claims the links cite, as `field_evidence_for` reads them.

        Assembled from each link's own verdict row rather than re-queried: the
        verdict names the claim, the field and the value it settled, and a
        second read could only disagree with the evidence already cited.
        """
        return [{"id": link["_claim"], "field_key": link["_verdict"]["field_key"],
                 "value": link["_verdict"]["value"],
                 "source_id": link["source_id"]} for link in links]

    @staticmethod
    def _verdicts(links: list[dict[str, Any]]) -> list[Mapping[str, Any]]:
        return [link["_verdict"] for link in links]

    def _review(self, observation: CandidateEvidence,
                snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
        """Move the candidate to `ready_for_review`, through the guarded RPC."""
        candidate = observation.candidate
        payload = {
            "snapshot_id": str(snapshot["id"]),
            "raw_record_id": str(candidate["raw_record_id"]),
            "candidate_key": str(candidate["candidate_key"]),
            "manufacturer": candidate["manufacturer"],
            "commercial_model": candidate["commercial_model"],
            "model_year_start": candidate.get("model_year_start"),
            "model_year_end": candidate.get("model_year_end"),
            "official_model_code": candidate.get("official_model_code"),
            "trim": candidate.get("trim"),
            "identity_dimensions": dict(candidate.get("identity_dimensions") or {}),
            "status": PROMOTABLE_CANDIDATE_STATUS}
        return self._repository.record_catalog_candidate(self._lease.run_id, payload,
                                                         **self._lease_kwargs)


__all__ = ["MAX_PROMOTIONS_PER_RUN", "PIPELINE_REASONS", "CandidateEvidence",
           "CatalogEvidenceLedger", "CatalogPromotionPipeline", "PromotionAttempt"]

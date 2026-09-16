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

from dataclasses import dataclass
from typing import Any, Mapping

from backend.errors import AppError

from .government.evidence import GOVERNMENT_TOOL_NAME, RESOLVE_VARIANT_OPERATION
from .government.source import GOVERNMENT_SOURCE_FAMILY
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
    never a model's restatement of it. `claims` are the durable claim rows the
    registered mapper's bundle became, in write order, exactly as the Evidence
    Board returned them.
    """

    candidate: Mapping[str, Any]
    snapshot_key: str
    resource_id: str
    claims: tuple[Mapping[str, Any], ...] = ()

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
        claims = tuple(dict(row) for row in acquired.claims)
        held = self._observed.get(candidate_id)
        if held is not None:
            # A later resolution of the same candidate ADDS its claims rather
            # than replacing them, so a run that read one vehicle twice cites
            # both readings. Each claim carries its own source, so a second
            # source is linked correctly without being remembered separately.
            seen = {str(row["id"]) for row in held.claims}
            self._observed[candidate_id] = CandidateEvidence(
                candidate=held.candidate, snapshot_key=held.snapshot_key,
                resource_id=held.resource_id,
                claims=held.claims + tuple(row for row in claims
                                           if str(row["id"]) not in seen))
            return
        if len(self._observed) >= self._max:
            return
        self._observed[candidate_id] = CandidateEvidence(
            candidate=dict(candidate), snapshot_key=str(provenance.get("snapshot_key") or ""),
            resource_id=str(provenance.get("resource_id") or ""), claims=claims)

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
                    candidate_id=observation.candidate_id, candidate_key="",
                    reason_code="CATALOG_PROMOTION_REFUSED"))
        return tuple(attempts)

    # --- one candidate -------------------------------------------------------

    def _promote_one(self, observation: CandidateEvidence,
                     ledger: CatalogEvidenceLedger) -> PromotionAttempt:
        # 0. The DURABLE rows, read back by the server. The tool result names
        #    the candidate and the record it read; everything the promotion is
        #    built from is then read from the database rather than taken from
        #    the result, so the internal keys a promotion needs never have to
        #    appear in a model-visible tool output at all.
        snapshot = self._snapshot(observation)
        record = self._record(snapshot, observation)
        candidate = self._candidate(snapshot, observation)
        if snapshot is None or record is None or candidate is None:
            return PromotionAttempt(candidate_id=observation.candidate_id,
                                    candidate_key="",
                                    reason_code="CATALOG_PROMOTION_SNAPSHOT_UNUSABLE")
        candidate_key = str(candidate["candidate_key"])

        # 1. LINK. Only a claim whose verdict is exactly `verified` is cited:
        #    a `needs_review` or `rejected` verdict is a real answer, and it is
        #    an answer AGAINST promoting.
        links = self._link(candidate, observation, ledger)
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
        proposed = {**dict(candidate), "status": PROMOTABLE_CANDIDATE_STATUS}
        try:
            evidence = field_evidence_for(
                candidate=proposed, links=[link for link, _ in links],
                claims={str(row["id"]): row for row in observation.claims},
                verdicts={str(verdict["id"]): verdict for _, verdict in links})
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
        reviewed = self._review(candidate, snapshot, record)
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
        """The snapshot the candidate was read from, or None when unusable.

        Looked up by the family, the resource and the snapshot key the TOOL
        RESULT stated, through the repository's own active-snapshot read -- so
        an inactive, incomplete or `legacy_reference` snapshot simply does not
        come back and nothing is promoted from it.
        """
        if not observation.snapshot_key:
            return None
        try:
            return self._repository.find_active_catalog_snapshot(
                GOVERNMENT_SOURCE_FAMILY, observation.resource_id, observation.snapshot_key)
        except AppError:
            return None

    def _link(self, candidate: Mapping[str, Any], observation: CandidateEvidence,
              ledger: CatalogEvidenceLedger
              ) -> list[tuple[Mapping[str, Any], Mapping[str, Any]]]:
        """One evidence link per VERIFIED claim, idempotent on a derived key.

        Returns each link paired with the verdict it cites, which is what
        `field_evidence_for` reads to decide the promoted field set.
        """
        linked: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
        for claim in observation.claims:
            verdict = ledger.verdict_for(claim["id"])
            if verdict is None or verdict.get("verdict") != "verified":
                continue
            row = self._repository.link_catalog_candidate_evidence(
                self._lease.run_id,
                {"candidate_id": str(candidate["id"]),
                 "candidate_key": str(candidate["candidate_key"]),
                 "source_id": str(claim["source_id"]), "claim_id": str(claim["id"]),
                 "verdict_id": str(verdict["id"])},
                **self._lease_kwargs)
            linked.append((dict(row), dict(verdict)))
        return linked

    def _record(self, snapshot: Mapping[str, Any] | None,
                observation: CandidateEvidence) -> Mapping[str, Any] | None:
        """The captured upstream row the candidate is a reading of."""
        if snapshot is None:
            return None
        try:
            return self._repository.catalog_raw_record_by_upstream_id(
                snapshot["id"], str(observation.candidate["upstream_record_id"]),
                allow_incomplete=False)
        except AppError:
            return None

    def _candidate(self, snapshot: Mapping[str, Any] | None,
                   observation: CandidateEvidence) -> Mapping[str, Any] | None:
        """The durable candidate row, read back by its own stated identity.

        One bounded page, filtered by exactly the identity the resolution
        settled on -- which matched a single row, or the mapper would have
        declined it. The row is then matched by ID, so a filter that somehow
        returned more than one cannot pick the wrong one.
        """
        if snapshot is None:
            return None
        try:
            rows = self._repository.catalog_candidate_variant_page(
                snapshot["id"],
                manufacturer=observation.candidate["manufacturer"],
                commercial_model=observation.candidate["commercial_model"],
                model_year=observation.candidate.get("model_year_start"),
                official_model_code=observation.candidate.get("official_model_code"),
                trim=observation.candidate.get("trim"),
                identity_dimensions=dict(observation.candidate.get("identity_dimensions") or {})
                                    or None,
                limit=MAX_PROMOTIONS_PER_RUN, offset=0, allow_incomplete=False)
        except AppError:
            return None
        return next((dict(row) for row in rows
                     if str(row.get("id")) == observation.candidate_id), None)

    def _review(self, candidate: Mapping[str, Any], snapshot: Mapping[str, Any],
                record: Mapping[str, Any]) -> Mapping[str, Any]:
        """Move the candidate to `ready_for_review`, through the guarded RPC.

        The payload restates the durable row exactly, with one field changed.
        The RPC holds a re-presented candidate to the identity already stored
        under its key, so this can move the status and can never quietly
        become a different vehicle -- and `candidate_key` is DERIVED from the
        record key and that identity, so a drift would be refused rather than
        written.
        """
        payload = {
            "snapshot_key": str(snapshot["snapshot_key"]),
            "record_key": str(record["record_key"]),
            "snapshot_id": str(snapshot["id"]),
            "raw_record_id": str(record["id"]),
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

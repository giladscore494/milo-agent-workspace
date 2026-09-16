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

Where the fourth arrow gets its input, and why it matters
---------------------------------------------------------

From the DATABASE, every time. `catalog_run_pending_promotions` reconstructs
"which candidate is this claim evidence FOR" out of rows the server itself
wrote: the claim's own evidence locator names one captured upstream row, that
row belongs to one active snapshot of an `evidence` trust family, and the
candidate is the reading of that row whose identity scope is exactly the
claim's.

An earlier round kept that association in a LEDGER inside the worker process,
built as the tool results arrived. It was correct while the process lived and
lost the moment it did not. A worker that crashes after Swarm V2 has durably
persisted its evidence, its verdicts and its checkpoint is replaced by a worker
that restores the completed tasks and DOES NOT re-execute the Government tool --
there is nothing left to re-execute. The replacement's ledger was therefore
empty, it produced no promotion attempts at all, and a run whose evidence and
verdicts were entirely durable left the canonical catalog empty with nothing
anywhere saying why.

Deriving the association instead of remembering it closes that window. It also
removes the need to trust anything a process was holding: the derivation reads
claims, verdicts, sources, snapshots, raw records and candidates, and every one
of those was written by trusted server code through a lease-guarded RPC.

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
    durable row, not from anything a model wrote; the claims and verdicts come
    from the Board; the canonical keys are DERIVED by
    `backend/catalog/payloads.py`;
*   every write goes through the same lease-guarded RPC as every other durable
    catalog write, and the database re-checks the whole support chain for every
    writer regardless of what this module believes.

What it deliberately does NOT do
--------------------------------

**It does not schedule anything.** It runs at the end of a run that already
happened, over that run's own Government evidence. It starts no capture, opens
no socket and holds no credential; the Government refresh operation stays
unscheduled and is not called from here.

**It cannot widen a run.** The pending-promotion read is scoped to this run's
own claims and bounded to `MAX_PROMOTIONS_PER_RUN` candidates. It never queries
for work another run produced.

**It never overrules an ambiguity.** A candidate the ingestion left `ambiguous`
or `rejected` is excluded by the durable read and refused again here. Ambiguity
is a first-class answer in this schema, and a promotion may not settle one by
writing it down.

**It never fails the run over a REFUSAL.** A refusal is what the durable catalog
is FOR: a field with no verified evidence, an unresolved conflict and a
candidate the ingestion left ambiguous are all legitimate outcomes of a research
run. Each one is returned as a static reason code, and the run's own result is
untouched.

An INFRASTRUCTURE failure is the opposite thing and is never laundered into one.
Two of them escape this module, both stated where they happen:

*   **a lost lease** (`CatalogPromotionPipeline.promote`) -- this worker is no
    longer the run's writer, so it must stop writing;
*   **a failed pending-promotion read** (`CatalogPromotionPipeline.pending`) --
    nothing was learned, so "this run owes no promotion" is not a fact anyone
    here has. Swallowing it would let the worker finalize a run whose verified
    evidence is durable and whose canonical promotion never happened, and no
    later scheduler revisits a completed run.

Both propagate unchanged to the worker, which already treats a repository
`AppError` as an infrastructure outcome: the run is not marked complete, no
`run_completed` is emitted, and the next attempt re-derives the same work from
the same durable rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from backend.errors import AppError

from .contracts import MAX_PROMOTIONS_PER_RUN
from .government.evidence import GOVERNMENT_TOOL_NAME, RESOLVE_VARIANT_OPERATION
from .promotion import (LEASE_FAILURE_CODES, PROMOTION_REASONS, CanonicalPromotion,
                        CatalogPromotionError, PromotionOutcome, PROMOTABLE_CANDIDATE_STATUS,
                        build_promotion_plan, field_evidence_for)

#: The ONE tool operation whose evidence this path promotes, spelled exactly as
#: the registered mapper records it on every source it writes. The durable read
#: matches on it, so a claim from any other operation -- or from no tool at all
#: -- can never be picked up here.
PROMOTABLE_TOOL_OPERATION = f"{GOVERNMENT_TOOL_NAME}.{RESOLVE_VARIANT_OPERATION}"

#: The candidate statuses a promotion may act on. `ambiguous` and `rejected`
#: are deliberately absent: both are decisions the ingestion made, and a
#: promotion may not overrule one by writing a canonical row.
PROMOTABLE_CANDIDATE_STATUSES = ("candidate", PROMOTABLE_CANDIDATE_STATUS)

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
    """One candidate this run still has to promote, and the evidence for it.

    Assembled from `catalog_run_pending_promotions` and from nothing else, so
    every field is a durable server-written value. `claims` carries one entry
    per verified claim, each already paired with the verdict that verified it.
    """

    candidate: Mapping[str, Any]
    claims: tuple[Mapping[str, Any], ...] = ()

    @property
    def candidate_id(self) -> str:
        return str(self.candidate["candidate_id"])

    @property
    def candidate_key(self) -> str:
        return str(self.candidate["candidate_key"])

    @property
    def status(self) -> str:
        return str(self.candidate.get("status") or "")

    def as_candidate_row(self) -> dict[str, Any]:
        """The candidate as the promotion contracts read one.

        The durable read returns the candidate's own columns under their own
        names except for `id`, which it calls `candidate_id` so one row can
        carry a candidate and a claim without colliding.
        """
        return {"id": self.candidate_id,
                **{name: self.candidate.get(name) for name in
                   ("candidate_key", "status", "manufacturer", "commercial_model",
                    "model_year_start", "model_year_end", "official_model_code",
                    "trim")},
                "identity_dimensions": dict(self.candidate.get("identity_dimensions") or {})}


def group_pending_promotions(rows: Sequence[Mapping[str, Any]]
                             ) -> tuple[CandidateEvidence, ...]:
    """Group the durable read's (candidate, claim) rows by candidate.

    Order-preserving: the read already returns candidates in `candidate_key`
    order and their claims in field order, so a resumed worker walks exactly
    the sequence the crashed one would have.
    """
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    candidates: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        key = str(row["candidate_key"])
        candidates.setdefault(key, row)
        grouped.setdefault(key, []).append(
            {"id": row["claim_id"], "source_id": row["source_id"],
             "verdict_id": row["verdict_id"], "field_key": row["field_key"],
             "value": row["field_value"]})
    return tuple(CandidateEvidence(candidate=candidates[key], claims=tuple(claims))
                 for key, claims in grouped.items())


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


class CatalogPromotionPipeline:
    """One run's Government evidence, carried through to canonical rows.

    Constructed by the trusted wiring that already holds the repository and the
    run's worker lease, exactly like the Evidence Board and for the same
    reason: every write is lease-guarded, idempotent and append-only, and this
    object holds no credential and no model client.

    It holds NO state of its own between calls. Everything it acts on is read
    from the database at the moment it acts, which is what makes a replacement
    worker behave identically to the one it replaced.
    """

    def __init__(self, repository: Any, lease: Any, *,
                 limit: int = MAX_PROMOTIONS_PER_RUN) -> None:
        self._repository = repository
        self._lease = lease
        self._limit = max(0, int(limit))
        self._promotion = CanonicalPromotion(repository, lease)

    @property
    def _lease_kwargs(self) -> dict[str, Any]:
        return {"worker_id": self._lease.worker_id, "attempt": self._lease.attempt,
                "lease_token": self._lease.lease_token}

    def pending(self) -> tuple[CandidateEvidence, ...]:
        """What this run still has to promote, read from durable state.

        Three outcomes, and the whole point of this method is that they are
        three rather than one.

        **No catalog to promote INTO.** A repository that does not offer the
        read at all has no catalog schema behind it, so this run owes nothing
        and can owe nothing. That is a true statement about the deployment,
        not a guess about durable rows, and it fails no research run over a
        capability that run never needed.

        **A successful read that matched nothing.** The ordinary empty case:
        the database was asked and answered that this run owes no promotion.
        No attempt is made and nothing is written.

        **A read that FAILED.** Not an empty answer -- an ABSENCE of one. A
        database, RPC or infrastructure failure means nothing was learned, so
        "this run owes no promotion" is a claim about durable state this
        process is in no position to make. Returning `()` here would make the
        two indistinguishable to the caller, and the caller is
        `backend/worker/main.py`: it would carry on to `run_completed`, emit
        neither a catalog refusal nor an infrastructure failure, and mark the
        run complete -- permanently stranding a run whose verified evidence is
        durable and whose canonical promotion never happened, because no later
        scheduler revisits a completed run.

        So the `AppError` propagates UNCHANGED, into the same worker
        infrastructure path every other guarded repository failure takes: the
        run is not marked complete, and the next attempt derives exactly this
        same pending work from exactly these same durable rows.
        """
        read = getattr(self._repository, "catalog_run_pending_promotions", None)
        if not callable(read):
            return ()
        rows = read(self._lease.run_id, PROMOTABLE_TOOL_OPERATION, limit=self._limit)
        return group_pending_promotions(list(rows))

    def promote(self) -> tuple[PromotionAttempt, ...]:
        """Link, review, plan and promote everything this run still owes.

        Total and bounded: every pending candidate produces exactly one
        `PromotionAttempt`, promoted or refused with a static reason, and the
        durable read already bounds how many there can be.

        Idempotent end to end, which is what makes a resume safe: the evidence
        link, the reviewed status and the promotion itself are each idempotent
        on a derived key, so a candidate a previous attempt already promoted is
        re-read, re-planned and then written NOWHERE -- it comes back as a
        replay.

        TWO things escape, and neither is a decision about any candidate.

        A failure of the DURABLE READ escapes before a single attempt is made
        -- it is taken deliberately outside the loop below, because the handler
        inside it exists to turn one candidate's refusal into a reason code and
        a read that failed refuses nothing. See `pending`.

        A LOST LEASE escapes from an attempt. It means this worker is no longer
        the run's writer, so continuing would be a stale worker writing
        canonical facts; it propagates to the worker's own lease handling
        exactly as every other guarded write's does.
        """
        # Outside the try/except below, and that placement is the contract: an
        # unavailable read must never be reported as "this run owes nothing".
        outstanding = self.pending()
        attempts: list[PromotionAttempt] = []
        for pending in outstanding:
            try:
                attempts.append(self._promote_one(pending))
            except AppError as failure:
                if failure.code in LEASE_FAILURE_CODES:
                    raise
                attempts.append(PromotionAttempt(
                    candidate_id=pending.candidate_id, candidate_key=pending.candidate_key,
                    reason_code="CATALOG_PROMOTION_REFUSED"))
        return tuple(attempts)

    # --- one candidate -------------------------------------------------------

    def _promote_one(self, pending: CandidateEvidence) -> PromotionAttempt:
        candidate = pending.as_candidate_row()
        candidate_key = pending.candidate_key

        # 0. The candidate must be PROMOTABLE AT ALL. The durable read already
        #    excludes an `ambiguous` or `rejected` reading; refusing it again
        #    here is what keeps "an ambiguous candidate stays ambiguous" true
        #    of this module rather than of the query it happens to use.
        if pending.status not in PROMOTABLE_CANDIDATE_STATUSES:
            return PromotionAttempt(candidate_id=pending.candidate_id,
                                    candidate_key=candidate_key,
                                    reason_code="CATALOG_PROMOTION_CANDIDATE_NOT_READY")

        # 1. The SNAPSHOT, re-read through the repository's own active-snapshot
        #    lookup, so an inactive, incomplete or `legacy_reference` snapshot
        #    simply does not come back and nothing is promoted from it.
        snapshot = self._snapshot(pending)
        if snapshot is None:
            return PromotionAttempt(candidate_id=pending.candidate_id,
                                    candidate_key=candidate_key,
                                    reason_code="CATALOG_PROMOTION_SNAPSHOT_UNUSABLE")

        # 2. LINK, one per verified claim, idempotent on a derived key -- so a
        #    resume relinks onto the rows a previous attempt created instead of
        #    duplicating them.
        links = self._link(candidate, pending)
        if not links:
            return PromotionAttempt(candidate_id=pending.candidate_id,
                                    candidate_key=candidate_key,
                                    reason_code="CATALOG_PROMOTION_NO_VERIFIED_EVIDENCE")

        # 3. PLAN, against the candidate as if it were reviewed. The plan is
        #    what decides whether the candidate IS reviewable: it refuses
        #    unless every identity field the canonical row would state has its
        #    own verified evidence at exactly that value. Building it first is
        #    what keeps `ready_for_review` from becoming a status this code
        #    writes hopefully -- the transition below happens only when a
        #    complete, evidenced promotion is already in hand.
        proposed = {**candidate, "status": PROMOTABLE_CANDIDATE_STATUS}
        try:
            evidence = field_evidence_for(
                candidate=proposed, links=[link for link, _ in links],
                claims={str(row["id"]): row for row in pending.claims},
                verdicts={str(verdict["id"]): verdict for _, verdict in links})
            plan = build_promotion_plan(candidate=proposed, snapshot=snapshot,
                                        evidence=evidence)
        except CatalogPromotionError as refusal:
            return PromotionAttempt(candidate_id=pending.candidate_id,
                                    candidate_key=candidate_key,
                                    reason_code=refusal.reason_code)

        # 4. REVIEW. The durable status transition, through the same guarded
        #    RPC that created the candidate: it holds the re-presented row to
        #    the identity already stored under that key, so this can move the
        #    status and can never quietly become a different vehicle. Already
        #    `ready_for_review` from an earlier attempt is a no-op.
        reviewed = self._review(pending, snapshot)
        if reviewed.get("status") != PROMOTABLE_CANDIDATE_STATUS:
            return PromotionAttempt(candidate_id=pending.candidate_id,
                                    candidate_key=candidate_key,
                                    reason_code="CATALOG_PROMOTION_CANDIDATE_NOT_READY")

        # 5. PROMOTE, atomically, through the lease-guarded RPC.
        try:
            outcome = self._promotion.promote(plan)
        except CatalogPromotionError as refusal:
            return PromotionAttempt(candidate_id=pending.candidate_id,
                                    candidate_key=candidate_key,
                                    reason_code=refusal.reason_code)
        return PromotionAttempt(candidate_id=pending.candidate_id,
                                candidate_key=candidate_key, outcome=outcome)

    def _snapshot(self, pending: CandidateEvidence) -> Mapping[str, Any] | None:
        """The snapshot the candidate was read from, or None when unusable."""
        try:
            return self._repository.find_active_catalog_snapshot(
                str(pending.candidate["source_family"]),
                str(pending.candidate["resource_id"]),
                str(pending.candidate["snapshot_key"]))
        except AppError:
            return None

    def _link(self, candidate: Mapping[str, Any], pending: CandidateEvidence
              ) -> list[tuple[Mapping[str, Any], Mapping[str, Any]]]:
        """One evidence link per verified claim, paired with its verdict.

        The verdict is already known to be `verified` -- the durable read joins
        on it -- so this states the pairing `field_evidence_for` needs rather
        than deciding anything again.
        """
        linked: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
        for claim in pending.claims:
            row = self._repository.link_catalog_candidate_evidence(
                self._lease.run_id,
                {"candidate_id": str(candidate["id"]),
                 "candidate_key": str(candidate["candidate_key"]),
                 "source_id": str(claim["source_id"]), "claim_id": str(claim["id"]),
                 "verdict_id": str(claim["verdict_id"])},
                **self._lease_kwargs)
            linked.append((dict(row), {"id": claim["verdict_id"], "verdict": "verified"}))
        return linked

    def _review(self, pending: CandidateEvidence,
                snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
        """Move the candidate to `ready_for_review`, through the guarded RPC.

        The payload restates the durable row exactly, with one field changed.
        The RPC holds a re-presented candidate to the identity already stored
        under its key, so this can move the status and can never quietly become
        a different vehicle -- and `candidate_key` is DERIVED from the record
        key and that identity, so a drift would be refused rather than written.
        """
        row = pending.candidate
        payload = {
            "snapshot_key": str(snapshot["snapshot_key"]),
            "record_key": str(row["record_key"]),
            "snapshot_id": str(snapshot["id"]),
            "raw_record_id": str(row["raw_record_id"]),
            "candidate_key": pending.candidate_key,
            "manufacturer": row["manufacturer"],
            "commercial_model": row["commercial_model"],
            "model_year_start": row.get("model_year_start"),
            "model_year_end": row.get("model_year_end"),
            "official_model_code": row.get("official_model_code"),
            "trim": row.get("trim"),
            "identity_dimensions": dict(row.get("identity_dimensions") or {}),
            "status": PROMOTABLE_CANDIDATE_STATUS}
        return self._repository.record_catalog_candidate(self._lease.run_id, payload,
                                                         **self._lease_kwargs)


__all__ = ["MAX_PROMOTIONS_PER_RUN", "PIPELINE_REASONS", "PROMOTABLE_CANDIDATE_STATUSES",
           "PROMOTABLE_TOOL_OPERATION", "CandidateEvidence", "CatalogPromotionPipeline",
           "PromotionAttempt", "group_pending_promotions"]

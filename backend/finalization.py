"""The ONE authoritative way a run becomes terminal.

Before this module every terminal branch in ``backend/worker/main.py`` closed
the run itself, and each one invented its own semantics on the way out:

* the cancellation handler wrote ``cancelled`` directly;
* ``_persist_budget_terminal`` wrote the budget stop's terminal status;
* the Swarm V2 failure handler called ``mark_run_failed``;
* the Swarm V2 success path looked up a validated product outcome;
* the V1 path guessed from ``result["status"]`` with a truthiness fallback;
* the summary-checkpoint resume path called ``mark_run_complete``
  unconditionally -- so a V1 run whose final builder said ``partial_success``
  resumed into durable ``completed``;
* five pre-execution refusals each wrote their own ``failed``.

Eight branches, no shared rule, and no defence against two of them running:
whichever wrote last decided what the run "was". This module replaces all of
them with one mechanism that answers three questions in one place.

1. WHAT is the terminal state?  A :class:`TerminalClaim` carries a REASON, not
   a status. The status is derived here -- from the canonical
   :mod:`backend.product_outcome` for product claims, and from a static table
   for non-product ones. No caller picks a durable status.

2. WHO wins a race?  Terminal statuses are ordered by AUTHORITY, not by
   arrival. An operator cancellation outranks a safety-rail stop, which
   outranks a failure, which outranks ``partial_success``, which outranks
   ``completed``. The weakest claim any branch can make is "everything went
   fine", so a late ``completed`` can never bury a cancellation or a budget
   stop. Claims noted before the competing branch finishes take part in the
   same comparison, which is what makes the result independent of which thread
   got there first.

3. WHAT happens the second time?  Terminalization is idempotent. A repeated
   equivalent claim is a no-op that reports the decision already in force; a
   DIFFERENT later claim never overwrites it; and a run found already terminal
   in the database -- because a previous attempt, a replacement worker or the
   operator got there first -- is adopted rather than rewritten.

4. WHEN may the decision be CLAIMED?  Only once it has durably won. The
   terminal state is written first, under the lease and under a
   compare-and-set on the state the decision was taken under; the terminal
   event -- the only place the canonical ProductOutcome is recorded, and the
   place Stage D reads it from -- is recorded after that write has won, never
   before. Where the repository offers the atomic primitive
   (``finalize_run``, migration 20260920000200) the two commit in one
   transaction; otherwise the event follows the write, and a failure to record
   a product's evidence is surfaced as ``TerminalEvidenceUnavailable`` rather
   than hidden. So no ``run_completed`` and no ProductOutcome can exist for a
   decision that did not become the run's durable state, and a cancellation
   that lands between the decision and the write wins: the run is re-read
   once, decided again under the state that actually holds, and both the
   status and the event say ``cancelled``.

Fencing is unchanged and still the outer boundary: every durable write carries
the active lease, so a worker that lost its lease cannot terminalize at all.
This module adds the decision layer above that; it never widens what a lease
permits.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable, Mapping
from uuid import UUID

from backend.errors import AppError
from backend.product_outcome import (ProductOutcome, derive_product_outcome,
                                     not_produced_outcome, refused_outcome)
from backend.runtime import TERMINAL_STATES, RunEventRecord

#: How much authority each terminal status carries. A HIGHER number wins.
#:
#: The order is not a preference, it is what each status asserts:
#:
#: ``cancelled``        a human decided this run should stop. Nothing the
#:                      engine discovers afterwards outranks that.
#: ``timed_out`` /      a safety rail tripped. The run did not get to finish,
#: ``budget_exhausted`` so no claim about its product can be trusted over it.
#: ``failed``           the run broke. Truthful, and weaker than a deliberate
#:                      stop, because a stop is a decision and a failure is an
#:                      accident.
#: ``partial_success``  a product exists with something outstanding.
#: ``completed``        the strongest possible good news and therefore the
#:                      WEAKEST claim: it is the one every other status
#:                      contradicts, so it may never overwrite any of them.
TERMINAL_AUTHORITY: Mapping[str, int] = {
    "cancelled": 50,
    "timed_out": 40,
    "budget_exhausted": 40,
    "failed": 30,
    "partial_success": 20,
    "completed": 10,
}

#: Durable status -> the terminal event the finalizer emits for it. Budget and
#: timeout stops are absent deliberately: the budget tracker already emits the
#: CAUSE event (``budget_exhausted``, ``run_timed_out``, ``token_limit_reached``)
#: at the moment the rail trips, and a second event asserting the same terminal
#: would be a duplicate claim, not more evidence.
_TERMINAL_EVENT: Mapping[str, str] = {
    "completed": "run_completed",
    "partial_success": "run_partial_success",
    "failed": "run_failed",
    "cancelled": "run_cancelled",
}

#: The reasons a claim can exist. The reason is what the caller knows; the
#: status is what this module decides.
CLAIM_REASONS = ("product", "cancelled", "budget_stop", "failure", "refusal")

#: Reason -> durable status, for the reasons whose status does not depend on a
#: product outcome. ``product`` and ``budget_stop`` are absent: the first is
#: derived from the canonical outcome, the second from the stop itself.
_STATUS_OF_REASON: Mapping[str, str] = {
    "cancelled": "cancelled",
    "failure": "failed",
    "refusal": "failed",
}


class FinalizationUnavailable(AppError):
    """The run could not be terminalized durably.

    This is an INFRASTRUCTURE outcome, never a run result: it means the
    repository cannot express the decision, so the job must not report success
    and must remain retryable.
    """

    def __init__(self, message: str) -> None:
        super().__init__("RUN_FINALIZATION_UNAVAILABLE", message, 503)


class TerminalEvidenceUnavailable(AppError):
    """The run is durably terminal, but its terminal event could not be recorded.

    Raised only on the fallback (non-atomic) path, and only for a PRODUCT
    claim, whose canonical ProductOutcome lives on that event and nowhere
    else. The run's state is the truth and stays; what failed is the job's
    duty to record what it produced, and Stage D refuses a product-terminal
    run that carries no recorded outcome (fail closed), so this is surfaced
    rather than swallowed. A relaunch finds the run terminal and exits 0.
    """

    def __init__(self, message: str) -> None:
        super().__init__("RUN_EVIDENCE_UNAVAILABLE", message, 503)


#: Repository errors that mean "this write did not happen because the world
#: moved": a lease reclaimed, an attempt superseded, a status that is no longer
#: what we read, or a run that is gone. Each one is a reason to RE-READ and
#: find out who won, never a reason to retry the write.
_RACE_CODES = frozenset({
    "INVALID_RUN_TRANSITION", "RUN_TRANSITION_CONFLICT", "RUN_LEASE_LOST",
    "STALE_WORKER_WRITE", "RUN_NOT_FOUND",
})


@dataclass(frozen=True)
class TerminalClaim:
    """One branch's claim about how the run ended.

    A claim states a REASON and the evidence for it. It never states a durable
    status: that is derived, so two branches cannot describe the same ending in
    two vocabularies.
    """

    reason: str
    outcome: ProductOutcome
    output: Any = None
    error: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    #: Set only for ``budget_stop``: the terminal status the tripped rail
    #: itself names (``timed_out``, ``budget_exhausted`` or ``failed``).
    stop_status: str | None = None
    #: The tracker already emitted this claim's cause event.
    event_emitted: bool = False
    #: Extra STATIC keys for the terminal event payload (a bounded diagnostic
    #: classification, never raw model, provider or exception text).
    event_payload: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.reason not in CLAIM_REASONS:
            raise ValueError(f"unknown terminal claim reason {self.reason!r}")
        if self.reason == "budget_stop" and self.stop_status not in TERMINAL_AUTHORITY:
            raise ValueError("a budget stop must name an allowlisted terminal status")

    # -- constructors ---------------------------------------------------------

    @classmethod
    def product(cls, engine: str, output: Any, *,
                extra_blocking: Mapping[str, int] | None = None,
                event_payload: Mapping[str, Any] | None = None) -> "TerminalClaim":
        """The engine finished and produced (or failed to produce) a product.

        ``extra_blocking`` carries blocking facts the CALLER knows and the
        payload does not -- the failures a resumed run's checkpoint recorded,
        for instance. They are folded in as a floor: they can demote the
        outcome and never lift it.
        """
        outcome = derive_product_outcome(engine, output)
        if extra_blocking:
            outcome = outcome.demoted_with(extra_blocking)
        return cls(reason="product", outcome=outcome, output=output,
                   event_payload=event_payload)

    @classmethod
    def cancelled(cls, engine: str, *, code: str = "RUN_CANCELLED",
                  message: str = "Run cancelled", output: Any = None) -> "TerminalClaim":
        """A cooperative cancellation was observed and honoured."""
        outcome = (derive_product_outcome(engine, output) if output is not None
                   else not_produced_outcome(engine))
        return cls(reason="cancelled", outcome=outcome, output=output,
                   error={"code": code, "message": message})

    @classmethod
    def budget_stop(cls, engine: str, stop: Any, *,
                    usage: dict[str, Any] | None = None) -> "TerminalClaim":
        """A hard limit tripped: a timeout, a token/cost budget, a daily cap."""
        return cls(reason="budget_stop", outcome=not_produced_outcome(engine),
                   error={"code": getattr(stop, "code", "BUDGET_EXCEEDED"),
                          "message": getattr(stop, "message", "budget exceeded")},
                   usage=usage, stop_status=getattr(stop, "terminal_status", "failed"),
                   event_emitted=True)

    @classmethod
    def failure(cls, engine: str, code: str, message: str, *,
                event_payload: Mapping[str, Any] | None = None) -> "TerminalClaim":
        """The run broke in a way the engine could not absorb."""
        return cls(reason="failure", outcome=refused_outcome(
            engine, code="ENGINE_REPORTED_FAILURE"),
            error={"code": code, "message": message}, event_payload=event_payload)

    @classmethod
    def refusal(cls, engine: str, code: str, message: str, *,
                event_payload: Mapping[str, Any] | None = None) -> "TerminalClaim":
        """A gate declined to execute: configuration, policy, credentials."""
        return cls(reason="refusal", outcome=refused_outcome(engine),
                   error={"code": code, "message": message},
                   event_payload=event_payload)

    # -- derived --------------------------------------------------------------

    @property
    def durable_status(self) -> str:
        """The durable run status this claim implies. Derived, never declared."""
        if self.reason == "budget_stop":
            return str(self.stop_status)
        if self.reason == "product":
            return self.outcome.durable_product_status()
        return _STATUS_OF_REASON[self.reason]

    @property
    def authority(self) -> int:
        return TERMINAL_AUTHORITY[self.durable_status]

    @property
    def identity(self) -> tuple[Any, ...]:
        """What makes two claims THE SAME decision.

        Two finalizations are duplicates when they say the same thing about the
        same product: the same reason, the same durable status, the same error
        code and the same payload digest. The digest is why a re-finalization
        after a resume is recognisable as a repeat without storing the payload.
        """
        return (self.reason, self.durable_status,
                (self.error or {}).get("code"),
                self.outcome.semantic_status, self.outcome.payload.digest)


@dataclass(frozen=True)
class FinalizationResult:
    """What actually happened to the run, and who decided it."""

    status: str
    claim: TerminalClaim | None
    outcome: ProductOutcome | None
    #: This call performed the durable terminal write.
    wrote: bool = False
    #: This call repeated a decision already in force, unchanged.
    duplicate: bool = False
    #: This call's claim LOST to a decision already in force.
    superseded: bool = False
    #: The run was already terminal in the database when this call ran.
    already_terminal: bool = False
    #: The terminal event this decision owes (the canonical ProductOutcome, for
    #: a product) is durably recorded. Always true on the atomic path, where
    #: it commits with the transition; on the fallback path it can be false,
    #: and a product claim then raises TerminalEvidenceUnavailable.
    evidence_recorded: bool = True

    @property
    def idempotent(self) -> bool:
        """True when this call changed nothing because the decision was made."""
        return not self.wrote


@dataclass
class RunFinalizer:
    """The single authority that terminalizes ONE run.

    Construct one per ``execute_run``; every terminal branch calls it and no
    branch writes a terminal status itself. It is thread-safe because V1 runs
    its technical chunks concurrently and a budget rail can trip on any of
    those threads while the main thread is assembling a product.
    """

    repo: Any
    run_id: UUID
    engine: str
    lease_ctx: Mapping[str, Any]
    event_sink: Any = None
    observer: Callable[[str, dict[str, Any]], None] | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _decision: TerminalClaim | None = field(default=None, init=False, repr=False)
    _result: FinalizationResult | None = field(default=None, init=False, repr=False)
    _pending: list[TerminalClaim] = field(default_factory=list, init=False, repr=False)
    _sources: list[Callable[[], "TerminalClaim | None"]] = field(
        default_factory=list, init=False, repr=False)

    # -- public API -----------------------------------------------------------

    @property
    def decided(self) -> bool:
        """Has this run already been terminalized through this finalizer?"""
        return self._result is not None

    def note(self, claim: TerminalClaim) -> None:
        """Record a claim that is TRUE but is not being written yet.

        A budget rail trips inside the engine; cancellation is observed while
        tasks are still settling. Both are real terminal claims that exist
        before the branch which will eventually call :meth:`finalize`. Noting
        them means the final comparison includes them, so the run's terminal
        status reflects everything known about it rather than whichever branch
        reached the writer last.
        """
        with self._lock:
            if self._result is None:
                self._pending.append(claim)

    def add_claim_source(self, source: Callable[[], "TerminalClaim | None"]) -> None:
        """Register a live source of terminal claims, consulted at decision time.

        A budget rail can trip on a worker thread AFTER the main thread has
        looked at it and BEFORE the main thread writes a product. Asking the
        rail again inside the decision lock closes that window: the run's
        terminal status is decided from everything true at the moment it is
        decided, not from whichever branch reached the writer first.

        A source that raises is ignored -- it is a safety net, and a net that
        fails must not itself end the run.
        """
        with self._lock:
            self._sources.append(source)

    def finalize(self, claim: TerminalClaim) -> FinalizationResult:
        """Terminalize the run. Idempotent, race-safe and total."""
        with self._lock:
            if self._result is not None:
                return self._repeat(claim)
            winner = self._winner(claim)
            # ONE read decides both questions this needs answered: has another
            # legitimate path already terminalized the run, and has the run
            # accepted a cancellation this claim has to be reconciled with.
            observed = self._observed_status()
            adopted = self._adopt(observed)
            if adopted is not None:
                return adopted
            return self._commit(self._legalize(winner, observed), observed)

    def finalize_product(self, output: Any) -> FinalizationResult:
        """Convenience: finalize from an engine's final payload."""
        return self.finalize(TerminalClaim.product(self.engine, output))

    # -- internals ------------------------------------------------------------

    def _repeat(self, claim: TerminalClaim) -> FinalizationResult:
        """A second finalization after a decision is already in force."""
        decided = self._result
        assert decided is not None  # guarded by the caller
        same = (self._decision is not None
                and self._decision.identity == claim.identity)
        return FinalizationResult(status=decided.status, claim=self._decision,
                                  outcome=decided.outcome, wrote=False,
                                  duplicate=same, superseded=not same,
                                  already_terminal=decided.already_terminal,
                                  evidence_recorded=decided.evidence_recorded)

    def _winner(self, claim: TerminalClaim) -> TerminalClaim:
        """The highest-authority claim among everything known about this run.

        Ties keep the EARLIER claim: two rails tripping in the same run is not
        a reason for the outcome to depend on thread scheduling.
        """
        winner = claim
        for pending in self._claims():
            if pending.authority > winner.authority:
                winner = pending
        return winner

    def _claims(self) -> list[TerminalClaim]:
        """Every other claim known about this run, noted or live."""
        claims = list(self._pending)
        for source in self._sources:
            try:
                live = source()
            except Exception:
                continue
            if live is not None:
                claims.append(live)
        return claims

    def _observed_status(self) -> str | None:
        try:
            return str(self.repo.get_run(self.run_id).get("status") or "")
        except Exception:
            # A read failure says nothing about the run's state. The durable
            # write below is still fenced and still compare-and-set, so the
            # worst case is that the write itself reports the race.
            return None

    def _adopt(self, observed: str | None) -> FinalizationResult | None:
        """Defer to a terminal state another legitimate path already won."""
        if observed is None or observed not in TERMINAL_STATES:
            return None
        result = FinalizationResult(status=observed, claim=None, outcome=None,
                                    wrote=False, already_terminal=True)
        self._result = result
        return result

    def _legalize(self, claim: TerminalClaim, observed: str | None) -> TerminalClaim:
        """Reconcile a claim with a cancellation the run has already accepted.

        ``cancellation_requested`` admits exactly ``cancelled`` and ``failed``.
        A product that finished anyway is not discarded -- the payload is kept
        as the run's output -- but the run is recorded as ``cancelled``,
        because the decision to stop was made and honouring it is not something
        a late result gets to reverse. This used to raise INVALID_RUN_TRANSITION
        out of ``execute_run``, which Cloud Run read as a failed task and
        relaunched into a second paid execution.
        """
        if observed != "cancellation_requested":
            return claim
        if claim.durable_status in {"cancelled", "failed"}:
            return claim
        return TerminalClaim(
            reason="cancelled", outcome=claim.outcome, output=claim.output,
            error={"code": "RUN_CANCELLED_AFTER_RESULT",
                   "message": "Run cancelled; the result produced before "
                              "cancellation is preserved"},
            usage=claim.usage)

    def _commit(self, claim: TerminalClaim, observed: str | None,
                redecisions_left: int = 1) -> FinalizationResult:
        """Make the decision durable, then -- and only then -- claim it.

        The order is the whole point. The terminal state is written FIRST,
        under the lease and under a compare-and-set on the state the decision
        was taken under; the terminal event that claims the decision is
        recorded only once that write has won. So no `run_completed` and no
        ProductOutcome can ever exist for a decision that did not become the
        run's durable state. Where the repository offers the atomic primitive
        (`finalize_run`, migration 20260920000200) the two are one
        transaction; otherwise the event follows the write and a failure to
        record it is surfaced, never hidden.

        A rejected write means the world moved between the read and the
        write. The run is re-read ONCE: a terminal state another path won is
        adopted; a state that changed the legal target (a cancellation
        request) re-decides and commits under the new state; anything else
        means this worker no longer owns the run, and the error escapes.
        """
        status = claim.durable_status
        try:
            evidence_recorded = self._persist(claim, status, observed)
        except AppError as exc:
            if exc.code not in _RACE_CODES:
                raise
            current = self._observed_status()
            adopted = self._adopt(current)
            if adopted is not None:
                return adopted
            if redecisions_left > 0 and current is not None and current != observed:
                # The state moved under us but the run is still ours to
                # finish: decide again under the state that actually holds.
                # Bounded to one re-decision, so a run that keeps moving
                # cannot keep this worker writing.
                return self._commit(self._legalize(claim, current), current,
                                    redecisions_left - 1)
            # The write was rejected and the run is NOT terminal: this worker
            # no longer owns the run. Reporting an outcome now would be a stale
            # worker speaking for a run someone else is executing.
            raise
        result = FinalizationResult(status=status, claim=claim,
                                    outcome=claim.outcome, wrote=True,
                                    evidence_recorded=evidence_recorded)
        self._decision = claim
        self._result = result
        if not evidence_recorded and claim.reason == "product":
            # The state is durable and truthful; the evidence Stage D needs
            # is not. Never pretend otherwise -- and never undo the state.
            raise TerminalEvidenceUnavailable(
                "run is terminal but its product outcome could not be recorded")
        return result

    def _persist(self, claim: TerminalClaim, status: str,
                 observed: str | None) -> bool:
        """Write the terminal state, then record its event. Returns whether
        the event this decision owes is durably recorded."""
        event = self._terminal_event(claim, status)
        atomic = getattr(self.repo, "finalize_run", None)
        if callable(atomic):
            expected = observed if observed is not None else self._observed_status()
            if expected is None:
                raise FinalizationUnavailable(
                    "run state could not be read; refusing to finalize blind")
            atomic(self.run_id, status, expected, event, **self._lease_kwargs(),
                   **self._terminal_fields(claim))
            self._observe(event)
            return True
        # Fallback for repositories without the atomic primitive: the state
        # first, the event only after the state has won.
        self._write(claim, status)
        recorded = self._append_terminal_event(event)
        if recorded:
            self._observe(event)
        return recorded

    def _terminal_event(self, claim: TerminalClaim, status: str) -> dict[str, Any] | None:
        """The event this decision owes, or None when it owes none.

        Budget and timeout stops owe none: the tracker already emitted the
        cause event when the rail tripped.
        """
        event_type = _TERMINAL_EVENT.get(status)
        if claim.event_emitted or event_type is None:
            return None
        payload: dict[str, Any] = {}
        if claim.reason == "product":
            # The canonical outcome record is durable evidence about a product
            # the application itself assembled, and it is emitted ONLY for a
            # product claim. A failure or a refusal has no product to describe:
            # its payload stays the static code alone, so no fragment of a
            # payload the contract REFUSED -- not even its top-level key names
            # -- can ride out on the event that rejects it.
            payload["product_outcome"] = claim.outcome.as_record()
            if claim.outcome.result_kind is not None:
                payload["result_kind"] = claim.outcome.result_kind
        if claim.error:
            payload["code"] = claim.error["code"]
        if claim.event_payload:
            payload.update(dict(claim.event_payload))
        message = (claim.error or {}).get("message") or f"Run {status}"
        return {"type": event_type, "message": message, "payload": payload}

    def _observe(self, event: dict[str, Any] | None) -> None:
        """Tell the supervisor shadow about an event that is now durable."""
        if event is None or self.observer is None:
            return
        try:
            self.observer(event["type"], dict(event["payload"]))
        except Exception:
            # The observer is shadow-mode telemetry; it never alters a
            # decision that is already durable.
            pass

    def _append_terminal_event(self, event: dict[str, Any] | None) -> bool:
        """Record the terminal event after the state has won (fallback path).

        Idempotent and bounded: an append that raised may still have
        committed, so before the one retry the event stream is re-read, and
        without a way to re-read it there is no retry -- a duplicate terminal
        claim would be its own integrity problem.
        """
        if event is None:
            return True
        if self.event_sink is None:
            # No sink means this finalizer is not the evidence recorder
            # (harness use); the worker always supplies one.
            return True
        record = RunEventRecord(run_id=self.run_id, type=event["type"],
                                message=event["message"], payload=event["payload"])
        try:
            self.event_sink.emit(record)
            return True
        except Exception:
            pass
        if self._terminal_event_exists(event["type"]):
            return True
        if not callable(getattr(self.repo, "list_run_events", None)):
            return False
        try:
            self.event_sink.emit(record)
            return True
        except Exception:
            return self._terminal_event_exists(event["type"])

    def _terminal_event_exists(self, event_type: str) -> bool:
        lister = getattr(self.repo, "list_run_events", None)
        if not callable(lister):
            return False
        try:
            return any(item.get("event_type") == event_type
                       for item in lister(self.run_id))
        except Exception:
            return False

    def _terminal_fields(self, claim: TerminalClaim) -> dict[str, Any]:
        fields: dict[str, Any] = {"finished_at": datetime.now(UTC).isoformat()}
        if claim.output is not None:
            fields["output"] = claim.output
        fields["error"] = claim.error
        if claim.usage is not None:
            fields["usage"] = claim.usage
        return fields

    def _write(self, claim: TerminalClaim, status: str) -> None:
        """The terminal state through the legacy verbs (fallback path)."""
        transition = getattr(self.repo, "transition_run", None)
        if status == "completed":
            # Preserved as the repository's own completion verb; it is
            # ``transition_run(completed, ...)`` underneath.
            marker = getattr(self.repo, "mark_run_complete", None)
            if not callable(marker):
                raise FinalizationUnavailable("terminal run completion is unavailable")
            marker(self.run_id, claim.output, **self._lease_kwargs())
            return
        if status == "failed" and claim.output is None:
            marker = getattr(self.repo, "mark_run_failed", None)
            if not callable(marker):
                raise FinalizationUnavailable("terminal run failure is unavailable")
            error = claim.error or {"code": "RUN_FAILED", "message": "run failed"}
            marker(self.run_id, error["code"], error["message"], **self._lease_kwargs())
            return
        if not callable(transition):
            # Never silently downgrade to "completed" because the repository
            # cannot express this status: recording an unusable, cancelled or
            # stopped run as a success is the exact defect this module exists
            # to prevent.
            raise FinalizationUnavailable("terminal run transition is unavailable")
        transition(self.run_id, status,
                   expected_worker_id=self.lease_ctx.get("worker_id"),
                   expected_attempt=self.lease_ctx.get("attempt"),
                   expected_lease_token=self.lease_ctx.get("lease_token"),
                   **self._terminal_fields(claim))

    def _lease_kwargs(self) -> dict[str, Any]:
        return {"worker_id": self.lease_ctx.get("worker_id"),
                "attempt": self.lease_ctx.get("attempt"),
                "lease_token": self.lease_ctx.get("lease_token")}


__all__ = ["CLAIM_REASONS", "FinalizationResult", "FinalizationUnavailable",
           "RunFinalizer", "TERMINAL_AUTHORITY", "TerminalClaim",
           "TerminalEvidenceUnavailable"]

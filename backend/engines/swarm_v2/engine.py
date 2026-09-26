"""Bounded, resumable Swarm V2 orchestration behind the Engine protocol."""

import json
from typing import Any, Callable, Iterable, Mapping

from .builder import FinalBuilder
from .commander import Commander, CommanderPlanFailure
from .conflict_policy import ConflictResolution, conflict_groups
from .contracts import EvidenceReference, RemainingBudget, VerificationVerdict
from .current_verdict import current_verdict_by_claim
from .correction import (correction_allowance, correction_issues, correction_path_closed,
                         correction_summary)
from .evidence import safe_durable_value
from .executor import BoundedTaskExecutor
from .failures import (NOT_DEGRADABLE, SWARM_V2_COMPLETION_CRITERIA_UNMET,
                       SWARM_V2_MAX_REPLANS_EXCEEDED, SWARM_V2_REPLAN_REQUIRES_GAP,
                       SWARM_V2_REPLAN_REWRITES_COMPLETED, SWARM_V2_REQUIRED_TASK_FAILED,
                       SwarmExecutionFailure, log_step_degraded)
from .feasibility import envelope_supports_a_run, plan_worst_case
from .grounding import VERIFIER_GROUNDING_VERSION
from .outcome import (DEGRADED_STEP_CODES, degraded_review_item, finalize_product_outcome,
                      validate_product_outcome)
from .resolution import CANDIDATE_GAP_CODES, SOFT_GAP_CODES, unresolved_kinds
from .state import SwarmState
from .support import VERIFIER_CONTRACT_VERSION
from .verifier import Verifier, VerifierContractError, VerifierProgress
from .worker import TaskResult


#: PR-X: the Commander answered a replan, and the answer was refused by JSON
#: decoding, the decision contract or the plan firewall. Only these codes can
#: be REJECTED instead of failing the run: a completion failure, a transport
#: outcome, a budget stop or a cancellation keeps its own terminal handling.
REJECTABLE_REPLAN_CODES = frozenset({
    "COMMANDER_DECISION_INVALID",
    "COMMANDER_PLAN_JSON_INVALID",
    "COMMANDER_PLAN_SCHEMA_INVALID",
    "COMMANDER_PLAN_LIMIT_EXCEEDED",
})

#: The static review code a run carries when a replan decision was rejected.
COMMANDER_REPLAN_REJECTED = "COMMANDER_REPLAN_REJECTED"

#: The `state.replans` decision value that records a rejected replan.
REJECTED_DECISION = "REJECTED"

#: PR-X S3: the `state.replans` code of a replacement plan that does not fit
#: the remaining budget (the pre-flight `_check_feasible` refusal).
SWARM_V2_REPLAN_INFEASIBLE = "SWARM_V2_REPLAN_INFEASIBLE"


class VerificationBudgetInsufficient(ValueError):
    """The final verification's batches do not fit the remaining model calls.

    A ValueError with the historical message, so existing callers keep
    working; its own class lets the engine degrade it instead of failing.
    """


class SwarmV2Engine:
    workflow_key = "swarm_v2"

    def __init__(self, *, commander: Commander, executor: BoundedTaskExecutor | None = None,
                 verifier: Verifier | None = None, builder: FinalBuilder | None = None,
                 evidence_loader: Callable[[Mapping[str, TaskResult]], Iterable[EvidenceReference]] | None = None,
                 checkpoint_sink: Callable[[str, dict], None] | None = None,
                 event_sink: Callable[[str, dict], None] | None = None,
                 usage_snapshot: Callable[[], Mapping[str, Any]] | None = None,
                 remaining_budget: Callable[[], RemainingBudget] | None = None,
                 verdict_sink: Callable[[VerificationVerdict], None] | None = None,
                 resolution_sink: Callable[[ConflictResolution], None] | None = None,
                 ledger_sink: Callable[[str], None] | None = None):
        self._commander = commander
        self._executor, self._verifier = executor, verifier
        self._builder = builder or FinalBuilder()
        self._evidence_loader = evidence_loader or (lambda _: ())
        self._checkpoint_sink, self._event_sink = checkpoint_sink, event_sink
        self._usage_snapshot = usage_snapshot or (lambda: {})
        self._remaining_budget = remaining_budget or (lambda: RemainingBudget(
            cost_units=100_000, tool_calls=100, tasks=64, model_calls=100))
        # R4 durable sinks. The engine still holds NO repository handle: these
        # are injected by the trusted worker wiring that already owns the run's
        # lease-guarded Evidence Board, so a verdict, its support links and a
        # conflict decision are persisted through exactly the same guarded,
        # idempotent RPCs as every other evidence write. Both are optional: a
        # deployment that has not wired them keeps the checkpoint as the only
        # durable home of a verdict, exactly as before R4.
        self._verdict_sink, self._resolution_sink = verdict_sink, resolution_sink
        # The run's ExecutionUsageLedger, for the consumption only THIS engine
        # can see happen: a task execution ending, a replan being accepted, a
        # correction round starting. Each is reported with a static kind, at
        # the moment it happens and BEFORE the checkpoint that follows, so the
        # durable record is never behind the checkpoint. The engine holds no
        # counter of its own for any of them: `state.replans` and
        # `state.correction_rounds` bound the PLAN, the ledger is what the
        # run has SPENT, and a resume reads the latter from the database.
        self._ledger_sink = ledger_sink

    def _consume(self, kind: str) -> None:
        if self._ledger_sink is not None:
            self._ledger_sink(kind)

    @staticmethod
    def _canonical(value: Any) -> str:
        return json.dumps(safe_durable_value(value), sort_keys=True,
                          separators=(",", ":"), ensure_ascii=True)

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        # Vocabulary and payloads are deliberately narrow. Provider responses,
        # exception strings, credentials and reasoning never reach this sink.
        allowed = {"task_id", "status", "code", "tool", "call_id", "operation",
                   "graph_revision", "decision",
                   "claim_id", "source_id", "conflict_id", "verdict",
                   "batch_index", "batch_count", "claim_count",
                   "source_count", "missing_context_count",
                   # R4: identifiers and static codes only. A scope hash, a
                   # policy/contract version and a static reason carry no
                   # value, no unit, no source text and no provider material.
                   "scope_hash", "state", "reason", "policy_version",
                   "structured_count", "issue_count", "round",
                   # A bare boolean saying WHICH terminal answer closed the
                   # correction path, so a resumed run that finalizes at
                   # round 0 explains itself without carrying any finding.
                   "declined"}
        safe = {key: value for key, value in payload.items() if key in allowed and
                (value is None or isinstance(value, (str, int, float, bool)))}
        if self._event_sink:
            self._event_sink(kind, safe)

    def _save(self, state: SwarmState) -> None:
        if self._checkpoint_sink:
            # run_checkpoints.engine_version and .workflow_key are NOT NULL in
            # production; the durable checkpoint must carry both under their
            # column names or the guarded insert fails after a paid call.
            checkpoint = {"phase": "swarm_v2", "engine_version": state.engine_version,
                "workflow_key": state.workflow_key,
                "completed_tasks": list(state.completed_task_ids),
                "artifacts": {"swarm_state": state.model_dump(mode="json")},
                "token_usage": dict(state.usage_snapshot)}
            safe_durable_value(checkpoint)
            self._checkpoint_sink("swarm_v2", checkpoint)

    def _merge_evidence(self, state: SwarmState,
                        incoming: Iterable[EvidenceReference | Mapping[str, Any]],
                        completed_task_ids: set[str]) -> list[EvidenceReference]:
        """Merge checkpoint and live evidence without losing resume provenance.

        Two different rules for two different origins, on purpose:

        * a CHECKPOINTED reference was written by this method for a completed
          task, so one that names another run or a task that is not completed
          is a corrupt checkpoint, and the resume is refused;
        * a LIVE reference names a task that has not completed when the
          board recorded its claim during that task's tool phase -- before the
          task's model call, and while sibling tasks run on other threads --
          or when the task later failed. That evidence is simply NOT YET (or
          not) part of the run's evidence, which is stated over completed
          tasks only. It is left out here and picked up by the merge that
          follows the task's own completion. It is never a reason to fail a
          run that has already paid for the work: the previous rule raised on
          it, which turned every Government-read plan with two independent
          register reads into a deterministic failure on every resume.

        A live reference from another run is still a violation.
        """
        merged: dict[str, tuple[str, EvidenceReference]] = {}
        checkpointed = [(raw, True) for raw in state.evidence_references]
        live = [(raw, False) for raw in list(incoming)]
        for raw, from_checkpoint in [*checkpointed, *live]:
            item = raw if isinstance(raw, EvidenceReference) else EvidenceReference.model_validate(raw)
            payload = safe_durable_value(item.model_dump(mode="json"))
            if item.run_id != state.run_id:
                raise ValueError("incompatible evidence provenance")
            if item.task_id not in completed_task_ids:
                if from_checkpoint:
                    raise ValueError("incompatible evidence provenance")
                continue
            encoded = self._canonical(payload)
            previous = merged.get(item.claim_id)
            if previous is not None and previous[0] != encoded:
                raise ValueError("conflicting evidence reference identity")
            merged[item.claim_id] = (encoded, item)
        evidence = [merged[key][1] for key in sorted(merged)]
        state.evidence_references = [item.model_dump(mode="json") for item in evidence]
        return evidence

    def _check_feasible(self, plan: Any, completed: Mapping[str, TaskResult]) -> None:
        remaining = self._remaining_budget()
        by_id = {task.task_id: task for task in plan.graph.tasks}
        completed_specs = [by_id[task_id] for task_id in completed]
        pending = [task for task in plan.graph.tasks if task.task_id not in completed]
        available_cost = max(
            0, remaining.cost_units -
            sum(task.estimated_cost_units for task in completed_specs)
        )
        # Tool-call budgeting is charged against the EXACT planned call list:
        # `len(task.tools)` is both what the firewall approved and what the
        # worker executes, so the reservation can never disagree with the run.
        #
        # Tool calls and tasks are charged against what REMAINS, and what
        # remains is the provider's answer, not something reconstructed here
        # from the current plan's completed tasks. The worker derives it from
        # the run's cumulative ledger -- completed tasks, FAILED tasks, earlier
        # attempts and superseded plans all already subtracted -- so charging
        # completed tasks again here would double-count them, and rebuilding
        # it from `completed` alone would refund every failed execution.
        # `cost_units` stays plan-relative: it is the Commander's own estimate
        # of the plan, not a consumption the ledger records.
        available_tools = max(0, remaining.tool_calls)
        available_tasks = max(0, remaining.tasks)
        # The WORST case, not a floor: repairs, replan decisions, a verifier
        # batch and the correction round are all things this plan may really
        # need, and a preflight that ignores them is not a proof of anything.
        worst = plan_worst_case(len(pending), max_replans=plan.max_replans)
        # Model calls and agent steps are spent UNCONDITIONALLY as work
        # proceeds, so a plan whose worst case does not fit can strand the run
        # mid-way having already paid. Those are preconditions.
        #
        # Retries are deliberately NOT one. They are spent only when something
        # goes wrong, the retry limiter is its own fail-closed gate, and
        # requiring the worst case up front would refuse every plan on a
        # healthy deployment: 23 tasks can need 24 repairs in the worst case
        # against a configured allowance of 15. `worst.retries` and
        # `worst.provider_attempts` are computed for capacity planning
        # (see `backend.tier2_profile`), not as an admission test.
        if (sum(task.estimated_cost_units for task in pending) > available_cost or
                sum(len(task.tools) for task in pending) > available_tools or
                len(pending) > available_tasks or
                worst.model_calls > remaining.model_calls or
                worst.agent_steps > remaining.agent_steps):
            raise ValueError("plan exceeds remaining budget")

    @staticmethod
    def _coverage_gaps(plan: Any, results: Mapping[str, TaskResult],
                       evidence: Iterable[EvidenceReference],
                       resolutions: Mapping[str, Iterable[Mapping[str, Any]]] | None = None,
                       ) -> list[dict[str, str]]:
        """Every coverage gap of the completed tasks, hard and soft.

        PR-T: a typed unresolved register candidate (see .resolution) is a
        VALID answer for the task that asked. It is reported as its own SOFT
        gap -- `CANDIDATE_UNRESOLVED_AMBIGUOUS` / `_NOT_FOUND`, never a hard
        completion failure -- and it is what EXPLAINS that task's unmet
        evidence requirement: an ambiguous resolution quotes no single row, so
        it can never produce the evidence a resolved one would. An evidence
        shortfall with no typed explanation stays the hard
        `EVIDENCE_REQUIREMENTS_UNMET` it always was.
        """
        refs = list(evidence)
        outcomes = resolutions or {}
        gaps: list[dict[str, str]] = []
        for task in plan.graph.tasks:
            result = results.get(task.task_id)
            if result is None or result.status != "completed":
                continue
            output = dict(result.output or {})
            if not set(task.completion.required_outputs) <= set(output):
                gaps.append({"task_id": task.task_id, "code": "REQUIRED_OUTPUT_MISSING"})
                continue
            unresolved = unresolved_kinds(outcomes.get(task.task_id, ()))
            gaps.extend({"task_id": task.task_id, "code": CANDIDATE_GAP_CODES[kind]}
                        for kind in unresolved)
            if not task.completion.evidence_satisfied:
                continue
            eligible = [item for item in refs if item.task_id == task.task_id and
                        item.supported and item.confidence >= task.evidence.min_confidence]
            source_ids = {item.source_id for item in eligible}
            fields = {item.field for item in eligible}
            if (len(source_ids) < task.evidence.minimum_sources or
                    not set(task.evidence.required_fields) <= fields):
                if unresolved:
                    continue
                gaps.append({"task_id": task.task_id,
                             "code": "EVIDENCE_REQUIREMENTS_UNMET"})
        return gaps

    @staticmethod
    def _candidate_outcomes(state: SwarmState, plan: Any,
                            completed: Mapping[str, TaskResult]) -> list[dict[str, Any]]:
        """The typed per-candidate outcomes of the plan's completed tasks."""
        task_ids = {task.task_id for task in plan.graph.tasks} & set(completed)
        return sorted((dict(item) for task_id in sorted(task_ids)
                       for item in state.task_resolutions.get(task_id, ())),
                      key=lambda item: (str(item.get("task_id")), str(item.get("call_id"))))

    @staticmethod
    def _completed_tasks_unchanged(current: Any, replacement: Any,
                                   completed: Mapping[str, TaskResult]) -> bool:
        before = {task.task_id: task.model_dump(mode="json") for task in current.graph.tasks}
        after = {task.task_id: task.model_dump(mode="json") for task in replacement.graph.tasks}
        return all(task_id in after and before.get(task_id) == after[task_id]
                   for task_id in completed)

    @staticmethod
    def _replan_rejected_for_pass(state: SwarmState) -> bool:
        """Was THIS pass's pre-verification replan decision already REJECTED?

        The entry lives in the checkpoint, so a resume of the same pass (the
        same graph revision) finalizes instead of asking the Commander -- and
        paying -- again.
        """
        return any(entry.get("decision") == REJECTED_DECISION
                   and "correction_round" not in entry
                   and entry.get("graph_revision") == state.graph_revision
                   for entry in state.replans)

    @staticmethod
    def _correction_rejected(state: SwarmState) -> bool:
        """Was the run's correction-round decision REJECTED?

        Like a decline, that is the run's terminal answer about the verifier's
        findings: the correction path is closed for the rest of the run and
        across every resume, so the findings are never put to the Commander
        again through either door.
        """
        return any(entry.get("decision") == REJECTED_DECISION and "correction_round" in entry
                   for entry in state.replans)

    def _replan_or_reject(self, state: SwarmState, evidence: list[EvidenceReference], *,
                          correction_round: int | None, requested_model: str,
                          objective: str, summary: Mapping[str, Any]) -> Any:
        """Ask the Commander for its decision; an invalid answer never discards work.

        Run c4b8bb54 completed 11/11 tasks with evidence and then failed as a
        whole on COMMANDER_DECISION_INVALID from the post-execution replan. A
        decision the firewall refuses is now REJECTED -- treated exactly like
        "no further work" (None) -- whenever the run already holds evidence
        from a completed task. The rejection is checkpointed and charged as a
        replan, so it consumes the plan's allowance and can never loop, and
        the final result carries COMMANDER_REPLAN_REJECTED so the outcome is
        partial_success at best. With nothing usable the failure escapes
        exactly as before.
        """
        try:
            return self._commander.replan(requested_model=requested_model,
                                          objective=objective, summary=summary)
        except CommanderPlanFailure as failure:
            if failure.code not in REJECTABLE_REPLAN_CODES or not evidence:
                raise
            code = failure.code
        self._reject_replan(state, code, correction_round=correction_round,
                            exception_class="CommanderPlanFailure")
        return None

    def _reject_replan(self, state: SwarmState, code: str, *, correction_round: int | None,
                       exception_class: str) -> None:
        """Checkpoint ONE rejected replan decision and charge it as a replan.

        PR-X S3: also used for a well-formed decision that a replan RULE
        refuses (no gap, allowance spent, rewrites completed work, does not
        fit the remaining budget) once the run holds evidence -- those are
        refusals of the Commander's proposal, not of the run's own work.
        """
        entry: dict[str, Any] = {"decision": REJECTED_DECISION, "code": code,
                                 "graph_revision": state.graph_revision}
        if correction_round is not None:
            entry["correction_round"] = correction_round
        state.replans.append(entry)
        log_step_degraded(DEGRADED_STEP_CODES[COMMANDER_REPLAN_REJECTED],
                          COMMANDER_REPLAN_REJECTED, exception_class)
        self._consume("replan")
        state.usage_snapshot = dict(self._usage_snapshot())
        self._save(state)

    def _replan_refusal(self, plan: Any, replacement: Any,
                        completed: Mapping[str, TaskResult]) -> tuple[str, BaseException] | None:
        """The replan rule a replacement plan breaks, as (static code, the error)."""
        if not self._completed_tasks_unchanged(plan, replacement, completed):
            return (SWARM_V2_REPLAN_REWRITES_COMPLETED,
                    SwarmExecutionFailure(SWARM_V2_REPLAN_REWRITES_COMPLETED))
        try:
            self._check_feasible(replacement, completed)
        except ValueError as exc:
            return SWARM_V2_REPLAN_INFEASIBLE, exc
        return None

    def _degrade(self, state: SwarmState, code: str, exc: BaseException) -> None:
        """Record ONE degraded step: checkpointed, logged, reported in needs_review."""
        step = DEGRADED_STEP_CODES[code]
        log_step_degraded(step, code, type(exc).__name__)
        entry = {"step": step, "code": code, "graph_revision": state.graph_revision}
        if entry not in state.degraded_steps:
            state.degraded_steps.append(entry)

    @staticmethod
    def _step_degraded(state: SwarmState, code: str) -> bool:
        """Did THIS pass already degrade with `code` (so a resume must not pay again)?"""
        return any(entry.get("code") == code and entry.get("graph_revision") == state.graph_revision
                   for entry in state.degraded_steps)

    @staticmethod
    def _degraded_review(state: SwarmState) -> list[dict[str, str]]:
        """ONE static review item per degraded code, in first-seen order."""
        codes: list[str] = []
        if any(entry.get("decision") == REJECTED_DECISION for entry in state.replans):
            codes.append(COMMANDER_REPLAN_REJECTED)
        for entry in state.degraded_steps:
            code = entry.get("code")
            if code in DEGRADED_STEP_CODES and code not in codes:
                codes.append(code)
        return [degraded_review_item(code) for code in codes]

    def _run_verification(self, state: SwarmState, evidence: list[EvidenceReference],
                          conflict_ids: set[str]) -> list[VerificationVerdict]:
        """Verify every claim under an exact, resumable model-call budget.

        The plan is prepared ONCE -- source evidence resolved, missing-context
        claims settled, grounded batches partitioned -- and that same prepared
        plan both sizes this pre-flight check and is executed, so the run never
        starts a verifier sequence it already knows it cannot finish and the
        estimate can never disagree with what runs. Only the REMAINING batches
        are charged: batches whose verdicts are already in the checkpoint are
        not required again. BudgetTracker stays the hard per-call authority and
        a BudgetExceeded refusal is never laundered into a verdict.

        Under a version-0 checkpoint the prepared plan deliberately re-verifies
        model-backed verdicts that predate grounded verification. That costs
        real model calls, so it is charged here like any other remaining batch;
        the restored cumulative usage is never rewound to pay for it.
        """
        existing = dict(state.verifier_state)
        plan = self._verifier.prepare(
            evidence, conflict_claim_ids=conflict_ids, existing_verdicts=existing,
            grounding_version=state.verifier_grounding_version)
        if len(plan.batches) > self._remaining_budget().model_calls:
            raise VerificationBudgetInsufficient("verification exceeds remaining model-call budget")
        # Legacy ungrounded verdicts are dropped from durable state together
        # with the version bump, so no later resume can mistake one for
        # grounded progress. Under version 1 this is the same map plus the
        # deterministic and missing-context verdicts.
        state.verifier_state = {v.claim_id: v.model_dump(mode="json") for v in plan.settled}
        state.verifier_grounding_version = VERIFIER_GROUNDING_VERSION
        state.verifier_contract_version = VERIFIER_CONTRACT_VERSION
        self._record_resolutions(state, plan.resolutions)
        self._emit("grounding_context_resolved",
                   {"claim_count": sum(len(batch) for batch in plan.batches),
                    "source_count": plan.source_count,
                    "missing_context_count": plan.missing_context_count,
                    "structured_count": plan.structured_count})

        def record(progress: VerifierProgress) -> None:
            # Validated verdicts, the authoritative usage snapshot and the
            # checkpoint move together, so a resume never replays a batch
            # whose verdicts are already durable. There is NO exactly-once
            # guarantee across the provider-call -> checkpoint boundary: a
            # crash in that window replays that one batch (at-least-once),
            # which stays inside BudgetTracker's durable accounting.
            for verdict in progress.verdicts:
                state.verifier_state[verdict.claim_id] = verdict.model_dump(mode="json")
            self._persist_verdicts(progress.verdicts)
            state.usage_snapshot = dict(self._usage_snapshot())
            self._save(state)
            if progress.batch_index >= 1:
                self._emit("verification_batch_completed",
                           {"batch_index": progress.batch_index,
                            "batch_count": progress.batch_count,
                            "claim_count": progress.claim_count})

        verdicts = self._verifier.verify_prepared(plan, batch_completed=record)
        # ONE verdict per claim in durable state, by the shared resolution: a
        # checkpoint that stored two verdicts for one claim would restore
        # whichever the dict comprehension happened to keep.
        state.verifier_state = {claim_id: item.model_dump(mode="json") for claim_id, item
                                in current_verdict_by_claim(verdicts).items()}
        self._persist_verdicts(verdicts)
        state.usage_snapshot = dict(self._usage_snapshot())
        self._emit("verification_completed", {"status": "completed"})
        self._save(state)
        return plan.resolutions, verdicts

    def _verify_or_degrade(self, state: SwarmState, evidence: list[EvidenceReference],
                           conflict_ids: set[str]) -> tuple[Any, list[VerificationVerdict], bool]:
        """Final verification that never discards the run's paid work.

        Returns ``(resolutions, verdicts, degraded)``. With evidence in hand, a
        verifier failure -- an invalid verifier response, a provider failure
        of a verifier call, a pre-flight that cannot pay for the batches -- is
        NOT a run failure: the verdicts already settled and checkpointed are
        kept (and handed to the durable verdict sink), every other claim
        simply has no verdict (so it is never shown as verified), no verifier
        call is repeated, and the run carries
        VERIFIER_FAILED / VERIFICATION_BUDGET_INSUFFICIENT. The degradation is
        checkpointed, so a resume of the same pass does not pay again.

        Left fatal: the infrastructure faults, and a checkpoint whose stored
        verifier state contradicts its own evidence (VERIFIER_STATE_*), which
        is a corrupt resume -- finalizing from it would misrepresent the
        evidence.
        """
        codes = ("VERIFIER_FAILED", "VERIFICATION_BUDGET_INSUFFICIENT")
        if any(self._step_degraded(state, code) for code in codes):
            return (), self._settled_verdicts(state, evidence), True
        try:
            resolutions, verdicts = self._run_verification(state, evidence, conflict_ids)
            return resolutions, verdicts, False
        except NOT_DEGRADABLE:
            raise
        except VerifierContractError as exc:
            if not evidence or exc.reason_code.startswith("VERIFIER_STATE_"):
                raise
            self._degrade(state, "VERIFIER_FAILED", exc)
        except VerificationBudgetInsufficient as exc:
            if not evidence:
                raise
            self._degrade(state, "VERIFICATION_BUDGET_INSUFFICIENT", exc)
        except Exception as exc:
            if not evidence:
                raise
            self._degrade(state, "VERIFIER_FAILED", exc)
        verdicts = self._settled_verdicts(state, evidence)
        # The verdicts settled BEFORE the failing paid step (deterministic,
        # missing-context, earlier batches) are real decisions: they reach the
        # durable verdict sink like any other, through its idempotent write.
        self._persist_verdicts(verdicts)
        state.usage_snapshot = dict(self._usage_snapshot())
        self._save(state)
        return (), verdicts, True

    @staticmethod
    def _settled_verdicts(state: SwarmState, evidence: list[EvidenceReference],
                          ) -> list[VerificationVerdict]:
        """The grounded verdicts already durable for THIS evidence set, if any.

        Only verdicts of the current grounding contract count; anything that
        does not parse is dropped (the claim is then simply unverified).
        """
        if state.verifier_grounding_version != VERIFIER_GROUNDING_VERSION:
            return []
        claims = {item.claim_id for item in evidence}
        verdicts = []
        for claim_id, raw in sorted(state.verifier_state.items()):
            if claim_id not in claims:
                continue
            try:
                verdicts.append(VerificationVerdict.model_validate(raw))
            except Exception:
                continue
        return verdicts

    def _persist_verdicts(self, verdicts: Iterable[VerificationVerdict]) -> None:
        """Hand every settled verdict, with its support links, to the sink.

        Idempotency is by STABLE IDENTITY, not by bookkeeping: the sink writes
        through a lease-guarded RPC keyed on the run plus a key derived from
        the verdict's own content, so a resumed run, a re-verification after a
        correction round and a retried batch all replay onto the same durable
        row instead of appending a second one. This is deliberately NOT an
        exactly-once claim across the provider-call/checkpoint boundary: it is
        at-least-once delivery onto an idempotent write.
        """
        if self._verdict_sink is None:
            return
        for verdict in verdicts:
            self._verdict_sink(verdict)

    def _record_resolutions(self, state: SwarmState,
                            resolutions: tuple[ConflictResolution, ...]) -> None:
        """Make every conflict decision durable, visible and replayable.

        A resolution is a decision ABOUT claims, never an edit OF them: the
        losing claim keeps its row and its evidence, and this record is what
        says a field-authoritative source settled the scope against it.
        """
        state.conflict_resolutions = [item.model_dump(mode="json") for item in resolutions]
        for resolution in resolutions:
            if self._resolution_sink is not None:
                self._resolution_sink(resolution)
            self._emit("conflict_resolution_recorded",
                       {"scope_hash": resolution.scope_hash, "state": resolution.state,
                        "reason": resolution.reason,
                        "policy_version": resolution.policy_version,
                        "claim_count": len(resolution.claim_ids)})

    def _start_correction_round(self, state: SwarmState, plan: Any,
                                completed: Mapping[str, TaskResult], *,
                                evidence: list[EvidenceReference],
                                verdicts: list[VerificationVerdict],
                                resolutions: tuple[ConflictResolution, ...],
                                summary: Mapping[str, Any], requested_model: str,
                                objective: str) -> Any:
        """Spend the run's ONE bounded correction round, or return None.

        This is the only place a finding made during FINAL verification can
        still change the run, and it is deliberately not a loop:

        * the round is offered at most `correction.MAX_CORRECTION_ROUNDS`
          times for the whole run, and the count lives in the checkpoint so a
          resume cannot earn a second one;
        * it is refused outright when the task, tool-call, model-call, retry,
          cost or replan budget is unavailable;
        * it goes through the SAME Commander, PlanValidator, feasibility
          check, executor, checkpoint and cancellation path as every other
          round -- there is no second orchestrator and no open-ended agent
          loop;
        * the Commander may decline it, and a declined round is final: the
          decline is checkpointed, the issues become needs_review and the run
          finalizes under R1;
        * once the run has its terminal answer -- the round was ACCEPTED or it
          was DECLINED -- the ordinary task-adding replan path is closed for
          the rest of the run and across every resume (see
          `correction.correction_path_closed` and `run`), so the same findings
          can never produce a second research task through another door.

        Returning a plan means "execute this and verify again"; returning None
        means "finalize now".
        """
        if state.correction_declined or self._correction_rejected(state):
            # The Commander already looked at these findings and declined (or
            # its answer was REJECTED). A resume must not put the same
            # question a second time.
            return None
        issues = correction_issues(evidence, verdicts)
        if not issues:
            return None
        allowance = correction_allowance(
            rounds_used=state.correction_rounds, remaining=self._remaining_budget(),
            replans_used=len(state.replans), max_replans=plan.max_replans)
        if not allowance.allowed:
            self._emit("correction_round_blocked",
                       {"reason": allowance.reason, "issue_count": len(issues)})
            return None
        round_number = state.correction_rounds + 1
        self._emit("correction_round_started",
                   {"round": round_number, "issue_count": len(issues)})
        decision = self._replan_or_reject(
            state, evidence, correction_round=round_number,
            requested_model=requested_model, objective=objective,
            summary={**summary, "verification_findings":
                     correction_summary(issues, resolutions=resolutions)})
        if decision is None:
            return None
        if decision.decision not in {"ADD_TASKS", "REVISE_TASK"}:
            # The Commander looked at the findings and chose not to research
            # them. That is a terminal answer, not an invitation to ask again,
            # and it is CHECKPOINTED so a resume cannot re-open the question.
            state.correction_declined = True
            self._emit("correction_round_declined", {"decision": decision.decision})
            self._save(state)
            return None
        replacement = decision.plan
        assert replacement is not None
        refusal = self._replan_refusal(plan, replacement, completed)
        if refusal is not None:
            # PR-X S3: with evidence in hand the refused proposal is REJECTED
            # and the run finalizes with what it verified.
            code, error = refusal
            if not evidence:
                raise error
            self._reject_replan(state, code, correction_round=round_number,
                                exception_class=type(error).__name__)
            return None
        # A correction round IS a replan and is charged as one, so it consumes
        # the plan's own replan allowance alongside the one-round allowance.
        state.replans.append({"decision": decision.decision, "reason": decision.reason,
                              "correction_round": state.correction_rounds + 1})
        state.correction_rounds += 1
        state.graph_revision += 1
        state.approved_plan = replacement.model_dump(mode="json")
        # Durable FIRST, then checkpointed: the ledger records the round the
        # moment it is accepted, so a crash before the checkpoint cannot make
        # a resume believe the run still has its correction allowance.
        self._consume("correction_round")
        state.usage_snapshot = dict(self._usage_snapshot())
        self._emit("commander_replanned", {"decision": decision.decision,
                                           "graph_revision": state.graph_revision})
        self._save(state)
        return replacement

    def run(self, run: dict[str, Any]) -> dict[str, Any]:
        run_input = run.get("input") or {}
        objective = str(run_input.get("objective") or run_input.get("content") or "")
        requested_model = run_input.get("commander_model", "auto_best_available")
        checkpoint = run.get("checkpoint")
        if checkpoint:
            raw = ((checkpoint.get("artifacts") or {}).get("swarm_state")
                   if isinstance(checkpoint, dict) else None)
            if raw is None:
                raise ValueError("incompatible Swarm V2 checkpoint")
            state = SwarmState.resume(raw, run_id=str(run.get("id", "")))
            plan = self._commander.validate_saved_plan(state.approved_plan or {})
        else:
            # Refuse an envelope that could not pay for the cheapest possible
            # successful run BEFORE asking the Commander to plan. Planning
            # first would spend a real paid call -- and, when the derived plan
            # ceiling then rejects the result, a second one on a repair --
            # to discover something arithmetic already knew.
            if not envelope_supports_a_run(self._remaining_budget()):
                raise ValueError("run budget cannot support any plan")
            plan = self._commander.plan(requested_model=requested_model, objective=objective,
                context=run_input.get("context", {}))
            state = SwarmState(run_id=str(run.get("id", "")), objective=objective,
                               approved_plan=plan.model_dump(mode="json"),
                               usage_snapshot=dict(self._usage_snapshot()))
            self._emit("commander_plan_created", {"graph_revision": 1})
            self._save(state)
        if self._executor is None or self._verifier is None:
            return {"status": "plan_validated", "plan": plan.model_dump(mode="json")}

        completed = {task_id: TaskResult(task_id, "completed", output=output)
                     for task_id, output in state.task_outputs.items()
                     if task_id in state.completed_task_ids}
        self._check_feasible(plan, completed)
        while True:
            persisted_results = dict(completed)

            def persist(result: TaskResult) -> None:
                output = safe_durable_value(dict(result.output or {}))
                state.completed_task_ids.append(result.task_id)
                state.completed_task_ids.sort()
                state.task_outputs[result.task_id] = output
                if result.resolutions:
                    state.task_resolutions[result.task_id] = [
                        safe_durable_value(dict(item)) for item in result.resolutions]
                persisted_results[result.task_id] = result
                self._merge_evidence(
                    state, self._evidence_loader(dict(persisted_results)),
                    set(persisted_results),
                )
                self._consume("task_completed")
                state.usage_snapshot = dict(self._usage_snapshot())
                self._save(state)  # result, evidence and usage are durable together

            execution = self._executor.execute(plan.graph, completed=completed,
                event_sink=self._emit, task_completed=persist)
            # A task that ran and FAILED spent its tool calls and its model
            # calls; it is a consumption of the run, not a rewind. (A task
            # BLOCKED by a failed dependency never ran and spent nothing.)
            for task_id in sorted(execution.tasks):
                if execution.tasks[task_id].status == "failed":
                    self._consume("task_failed")
            completed = {key: value for key, value in execution.tasks.items()
                         if value.status == "completed"}
            evidence = self._merge_evidence(
                state, self._evidence_loader(execution.tasks), set(completed)
            )
            for item in evidence:
                self._emit("evidence_added", {"claim_id": item.claim_id,
                    "source_id": item.source_id, "task_id": item.task_id})

            # R4: contradiction is decided on the COMPLETE identity -- the
            # canonical scope PLUS the closed identity dimensions -- through
            # the one shared grouping the verifier also uses. Two claims that
            # differ only by generation, engine, transmission or official code
            # describe two different variants and were never a contradiction.
            #
            # PR-X S3: with evidence in hand a failure of this deterministic
            # step is not a run failure: every claim is still verified against
            # its OWN source, only the cross-source marking is missing, and
            # the run says so (CONFLICT_DETECTION_FAILED, partial_success).
            try:
                groups = conflict_groups(evidence)
                conflict_ids = {item.claim_id for claims in groups.values() for item in claims}
            except NOT_DEGRADABLE:
                raise
            except Exception as exc:
                if not evidence:
                    raise
                self._degrade(state, "CONFLICT_DETECTION_FAILED", exc)
                conflict_ids = set()
            for claim_id in sorted(conflict_ids):
                self._emit("conflict_found", {"claim_id": claim_id})

            failed = sorted(k for k, v in execution.tasks.items()
                            if v.status != "completed")
            try:
                gaps = self._coverage_gaps(plan, execution.tasks, evidence,
                                           state.task_resolutions)
            except NOT_DEGRADABLE:
                raise
            except Exception as exc:
                # PR-X S3: the coverage check could not run; nothing is
                # claimed about coverage, and the run says so.
                if not evidence:
                    raise
                self._degrade(state, "COVERAGE_CHECK_FAILED", exc)
                gaps = []
            unresolved = bool(failed or gaps or conflict_ids)
            summary = {"completed": sorted(completed), "failed": failed,
                "evidence": [{"claim_id": e.claim_id, "source_id": e.source_id,
                              "task_id": e.task_id, "field": e.field,
                              "confidence": e.confidence} for e in evidence],
                "conflicts": sorted(conflict_ids), "gaps": gaps,
                "remaining_budget": self._remaining_budget().model_dump(mode="json"),
                "decision_context": {
                    "all_tasks_completed": len(completed) == len(plan.graph.tasks),
                    "has_unresolved_issues": unresolved,
                    "valid_terminal_decision": (
                        "REQUEST_VERIFICATION" if unresolved else "FINISH"
                    ),
                }}
            # R4: the one bounded correction round is the LAST thing that may
            # add work to a run. Once the run has had its TERMINAL answer
            # about the verifier's findings -- the round was spent, or the
            # Commander declined it -- the ordinary pre-verification replan
            # path is closed: the run goes straight to re-verification and
            # finalization. Without this, the very same verifier-discovered
            # conflict could be handed back to the Commander here and earn a
            # second research task through the other door. Both answers live
            # in the CHECKPOINT (`correction_rounds` / `correction_declined`),
            # so a resume is closed exactly like the pass that recorded them:
            # resuming from a decline never asks the Commander anything again.
            # A round merely blocked by a budget decided nothing and is not
            # closed, and every replan BEFORE the correction round behaves
            # exactly as it did.
            if (correction_path_closed(rounds_used=state.correction_rounds,
                                       declined=state.correction_declined)
                    or self._correction_rejected(state)):
                self._emit("correction_round_finalizing",
                           {"round": state.correction_rounds,
                            "declined": state.correction_declined})
                decision = None
            elif self._replan_rejected_for_pass(state):
                # PR-X: this pass's decision was already REJECTED and
                # checkpointed; a resume never asks the Commander again for it.
                decision = None
            else:
                decision = self._replan_or_reject(
                    state, evidence, correction_round=None,
                    requested_model=requested_model, objective=objective, summary=summary)
            if decision is not None and decision.decision in {"ADD_TASKS", "REVISE_TASK"}:
                # PR-X S3: a well-formed proposal that breaks a replan rule is
                # a refusal of the PROPOSAL. With evidence in hand it is
                # REJECTED (checkpointed, charged, reported) and the run goes
                # on to verification with the work it has; with nothing usable
                # it fails with its own static code exactly as before.
                refusal: tuple[str, BaseException] | None = None
                if not (failed or gaps or conflict_ids):
                    refusal = (SWARM_V2_REPLAN_REQUIRES_GAP,
                               SwarmExecutionFailure(SWARM_V2_REPLAN_REQUIRES_GAP))
                elif len(state.replans) >= plan.max_replans:
                    refusal = (SWARM_V2_MAX_REPLANS_EXCEEDED,
                               SwarmExecutionFailure(SWARM_V2_MAX_REPLANS_EXCEEDED))
                else:
                    assert decision.plan is not None
                    refusal = self._replan_refusal(plan, decision.plan, completed)
                if refusal is not None:
                    code, error = refusal
                    if not evidence:
                        raise error
                    self._reject_replan(state, code, correction_round=None,
                                        exception_class=type(error).__name__)
                    decision = None
            if decision is not None and decision.decision in {"ADD_TASKS", "REVISE_TASK"}:
                replacement = decision.plan
                assert replacement is not None
                state.replans.append({"decision": decision.decision,
                                      "reason": decision.reason})
                state.graph_revision += 1
                state.approved_plan = replacement.model_dump(mode="json")
                self._consume("replan")
                state.usage_snapshot = dict(self._usage_snapshot())
                plan = replacement
                self._emit("commander_replanned", {"decision": decision.decision,
                                                    "graph_revision": state.graph_revision})
                self._save(state)
                continue

            by_id = {task.task_id: task for task in plan.graph.tasks}
            hard_failures = [task_id for task_id in failed
                             if not by_id[task_id].completion.allow_partial]
            hard_gaps = [gap for gap in gaps if gap["code"] not in SOFT_GAP_CODES
                         and not by_id[gap["task_id"]].completion.allow_partial]
            # PR-T: completed work is never discarded because ONE task has a
            # gap. Run 3c72bfbc completed 9/9 tasks with 40 claims and lost
            # all of it here because one task's typed ambiguity left its
            # evidence requirement unmet. A required task that failed, or a
            # hard gap, now fails the run ONLY when nothing usable exists: with
            # any accepted evidence the run is verified and finalized, and the
            # failure and the gap are listed in `needs_review`, which makes
            # the outcome `partial_success` -- never `complete`.
            if (hard_failures or hard_gaps) and not evidence:
                raise SwarmExecutionFailure(SWARM_V2_REQUIRED_TASK_FAILED if hard_failures
                                            else SWARM_V2_COMPLETION_CRITERIA_UNMET)

            resolutions, verdicts, verification_degraded = self._verify_or_degrade(
                state, evidence, conflict_ids)
            # A degraded verification settled nothing new, so there are no
            # trustworthy findings to research: the correction round is not
            # offered and no verifier step is paid for twice.
            correction = None if verification_degraded else self._start_correction_round(
                state, plan, completed, evidence=evidence, verdicts=verdicts,
                resolutions=resolutions, summary=summary,
                requested_model=requested_model, objective=objective)
            if correction is not None:
                plan = correction
                continue
            failures = [{"task_id": task_id,
                         "code": (result.error or {}).get("code", "TASK_FAILED")}
                        for task_id, result in sorted(execution.tasks.items())
                        if result.status != "completed"]
            try:
                candidate_outcomes = self._candidate_outcomes(state, plan, completed)
            except NOT_DEGRADABLE:
                raise
            except Exception as exc:
                if not evidence:
                    raise
                self._degrade(state, "CANDIDATE_OUTCOMES_FAILED", exc)
                candidate_outcomes = []
            # ONE canonical finalization. Everything the run knows -- verified
            # evidence, verdicts, task failures, coverage gaps and conflicts --
            # is handed to the builder in a single call, and the payload it
            # returns is never mutated here. The engine deliberately passes NO
            # trusted negative result: no registered tool can yet return a
            # typed "no match" signal, so `not_found` is unreachable and is
            # never inferred from an empty field set (see .outcome).
            return self._finalize(state, evidence, verdicts, failures=failures, gaps=gaps,
                                  conflict_ids=conflict_ids,
                                  candidate_outcomes=candidate_outcomes)

    def _finalize(self, state: SwarmState, evidence: list[EvidenceReference],
                  verdicts: list[VerificationVerdict], *, failures: list[dict[str, Any]],
                  gaps: list[dict[str, str]], conflict_ids: set[str],
                  candidate_outcomes: list[dict[str, Any]]) -> dict[str, Any]:
        """The canonical payload -- or, if it cannot be built, a reduced valid one.

        The payload is validated HERE, by the same contract the worker applies,
        so a builder defect is caught while the engine can still do something
        about it. With evidence in hand a failure is FINAL_BUILD_DEGRADED:

        1. the builder runs again without the optional register view
           (`candidate_outcomes` and the vehicle keys are omitted);
        2. if that fails too, the verified fields are omitted as well and the
           outcome is decided from the static review items alone
           (partial_success / no_usable_result).

        Neither step invents or promotes a value: every field shown is a
        verified claim, and the evidence itself stays durable on the board and
        in the checkpoint.
        """
        def build(outcomes: list[dict[str, Any]]) -> dict[str, Any]:
            final = safe_durable_value(self._builder.build(
                evidence, verdicts, task_failures=failures,
                coverage_gaps=[*gaps, *self._degraded_review(state)],
                conflict_claim_ids=sorted(conflict_ids), candidate_outcomes=outcomes))
            validate_product_outcome(final)
            return final

        try:
            return build(candidate_outcomes)
        except NOT_DEGRADABLE:
            raise
        except Exception as exc:
            if not evidence:
                raise
            self._degrade(state, "FINAL_BUILD_DEGRADED", exc)
        try:
            return build([])
        except NOT_DEGRADABLE:
            raise
        except Exception:
            pass
        review = [*self._degraded_review(state)]
        try:
            final = safe_durable_value(finalize_product_outcome(
                fields={}, task_failures=failures, coverage_gaps=[*gaps, *review]))
            validate_product_outcome(final)
            return final
        except NOT_DEGRADABLE:
            raise
        except Exception:
            # The static items alone: this cannot fail.
            return finalize_product_outcome(fields={}, coverage_gaps=review)

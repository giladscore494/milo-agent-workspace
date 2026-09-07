"""Bounded, resumable Swarm V2 orchestration behind the Engine protocol."""

import json
from typing import Any, Callable, Iterable, Mapping

from .builder import FinalBuilder
from .commander import Commander
from .conflict_policy import ConflictResolution, conflict_groups
from .contracts import EvidenceReference, RemainingBudget, VerificationVerdict
from .correction import (MAX_CORRECTION_ROUNDS, correction_allowance, correction_issues,
                         correction_summary)
from .evidence import safe_durable_value
from .executor import BoundedTaskExecutor
from .grounding import VERIFIER_GROUNDING_VERSION
from .state import SwarmState
from .support import VERIFIER_CONTRACT_VERSION
from .verifier import Verifier, VerifierProgress
from .worker import TaskResult


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
                 resolution_sink: Callable[[ConflictResolution], None] | None = None):
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
                   "structured_count", "issue_count", "round"}
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
        """Merge checkpoint and live evidence without losing resume provenance."""
        merged: dict[str, tuple[str, EvidenceReference]] = {}
        for raw in [*state.evidence_references, *list(incoming)]:
            item = raw if isinstance(raw, EvidenceReference) else EvidenceReference.model_validate(raw)
            payload = safe_durable_value(item.model_dump(mode="json"))
            if item.run_id != state.run_id or item.task_id not in completed_task_ids:
                raise ValueError("incompatible evidence provenance")
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
        available_tools = max(
            0, remaining.tool_calls - sum(len(task.tools) for task in completed_specs)
        )
        available_tasks = max(0, remaining.tasks - len(completed_specs))
        # The MINIMUM each pending task costs is one worker-model call; a
        # structurally invalid completion may add one bounded repair call
        # (see worker.MAX_WORKER_OUTPUT_MODEL_ATTEMPTS). This gate stays a
        # pre-flight floor deliberately -- BudgetTracker remains the sole
        # authority that refuses a call. Keep two slots for the next
        # Commander decision and at least one verifier batch. The EXACT
        # remaining verifier batch count is unknowable here (no evidence set
        # yet) and is checked separately in _run_verification.
        required_model_calls = len(pending) + 2
        if (sum(task.estimated_cost_units for task in pending) > available_cost or
                sum(len(task.tools) for task in pending) > available_tools or
                len(pending) > available_tasks or
                required_model_calls > remaining.model_calls):
            raise ValueError("plan exceeds remaining budget")

    @staticmethod
    def _coverage_gaps(plan: Any, results: Mapping[str, TaskResult],
                       evidence: Iterable[EvidenceReference]) -> list[dict[str, str]]:
        refs = list(evidence)
        gaps: list[dict[str, str]] = []
        for task in plan.graph.tasks:
            result = results.get(task.task_id)
            if result is None or result.status != "completed":
                continue
            output = dict(result.output or {})
            if not set(task.completion.required_outputs) <= set(output):
                gaps.append({"task_id": task.task_id, "code": "REQUIRED_OUTPUT_MISSING"})
                continue
            if not task.completion.evidence_satisfied:
                continue
            eligible = [item for item in refs if item.task_id == task.task_id and
                        item.supported and item.confidence >= task.evidence.min_confidence]
            source_ids = {item.source_id for item in eligible}
            fields = {item.field for item in eligible}
            if (len(source_ids) < task.evidence.minimum_sources or
                    not set(task.evidence.required_fields) <= fields):
                gaps.append({"task_id": task.task_id,
                             "code": "EVIDENCE_REQUIREMENTS_UNMET"})
        return gaps

    @staticmethod
    def _completed_tasks_unchanged(current: Any, replacement: Any,
                                   completed: Mapping[str, TaskResult]) -> bool:
        before = {task.task_id: task.model_dump(mode="json") for task in current.graph.tasks}
        after = {task.task_id: task.model_dump(mode="json") for task in replacement.graph.tasks}
        return all(task_id in after and before.get(task_id) == after[task_id]
                   for task_id in completed)

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
            raise ValueError("verification exceeds remaining model-call budget")
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
        state.verifier_state = {v.claim_id: v.model_dump(mode="json") for v in verdicts}
        self._persist_verdicts(verdicts)
        state.usage_snapshot = dict(self._usage_snapshot())
        self._emit("verification_completed", {"status": "completed"})
        self._save(state)
        return plan.resolutions, verdicts

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
        * once a round HAS been accepted, the ordinary task-adding replan path
          is closed for the rest of the run (see `run`), so the same findings
          can never produce a second research task through another door.

        Returning a plan means "execute this and verify again"; returning None
        means "finalize now".
        """
        if state.correction_declined:
            # The Commander already looked at these findings and declined. A
            # resume must not put the same question a second time.
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
        self._emit("correction_round_started",
                   {"round": state.correction_rounds + 1, "issue_count": len(issues)})
        decision = self._commander.replan(
            requested_model=requested_model, objective=objective,
            summary={**summary, "verification_findings":
                     correction_summary(issues, resolutions=resolutions)})
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
        if not self._completed_tasks_unchanged(plan, replacement, completed):
            raise ValueError("replan cannot revise or discard completed tasks")
        self._check_feasible(replacement, completed)
        # A correction round IS a replan and is charged as one, so it consumes
        # the plan's own replan allowance alongside the one-round allowance.
        state.replans.append({"decision": decision.decision, "reason": decision.reason,
                              "correction_round": state.correction_rounds + 1})
        state.correction_rounds += 1
        state.graph_revision += 1
        state.approved_plan = replacement.model_dump(mode="json")
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
                persisted_results[result.task_id] = result
                self._merge_evidence(
                    state, self._evidence_loader(dict(persisted_results)),
                    set(persisted_results),
                )
                state.usage_snapshot = dict(self._usage_snapshot())
                self._save(state)  # result, evidence and usage are durable together

            execution = self._executor.execute(plan.graph, completed=completed,
                event_sink=self._emit, task_completed=persist)
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
            groups = conflict_groups(evidence)
            conflict_ids = {item.claim_id for claims in groups.values() for item in claims}
            for claim_id in sorted(conflict_ids):
                self._emit("conflict_found", {"claim_id": claim_id})

            failed = sorted(k for k, v in execution.tasks.items()
                            if v.status != "completed")
            gaps = self._coverage_gaps(plan, execution.tasks, evidence)
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
            # add work to a run. Once it has been accepted, the ordinary
            # pre-verification replan path is closed: the correction plan is
            # executed exactly once and the run goes straight to
            # re-verification and finalization. Without this, the very same
            # verifier-discovered conflict could be handed back to the
            # Commander here and earn a second research task while
            # `correction_rounds` still read 1. The flag lives in the
            # checkpoint, so a resume mid-correction is closed too, and every
            # replan BEFORE the correction round behaves exactly as it did.
            if state.correction_rounds >= MAX_CORRECTION_ROUNDS:
                self._emit("correction_round_finalizing",
                           {"round": state.correction_rounds})
                decision = None
            else:
                decision = self._commander.replan(
                    requested_model=requested_model, objective=objective, summary=summary
                )
            if decision is not None and decision.decision in {"ADD_TASKS", "REVISE_TASK"}:
                if not (failed or gaps or conflict_ids):
                    raise ValueError("replan requires an unresolved gap or conflict")
                if len(state.replans) >= plan.max_replans:
                    raise ValueError("maximum replans exceeded")
                replacement = decision.plan
                assert replacement is not None
                if not self._completed_tasks_unchanged(plan, replacement, completed):
                    raise ValueError("replan cannot revise or discard completed tasks")
                self._check_feasible(replacement, completed)
                state.replans.append({"decision": decision.decision,
                                      "reason": decision.reason})
                state.graph_revision += 1
                state.approved_plan = replacement.model_dump(mode="json")
                state.usage_snapshot = dict(self._usage_snapshot())
                plan = replacement
                self._emit("commander_replanned", {"decision": decision.decision,
                                                    "graph_revision": state.graph_revision})
                self._save(state)
                continue

            by_id = {task.task_id: task for task in plan.graph.tasks}
            hard_failures = [task_id for task_id in failed
                             if not by_id[task_id].completion.allow_partial]
            hard_gaps = [gap for gap in gaps
                         if not by_id[gap["task_id"]].completion.allow_partial]
            if hard_failures:
                raise ValueError("required task execution failed")
            if hard_gaps:
                raise ValueError("completion criteria not satisfied")

            resolutions, verdicts = self._run_verification(state, evidence, conflict_ids)
            correction = self._start_correction_round(
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
            # ONE canonical finalization. Everything the run knows -- verified
            # evidence, verdicts, task failures, coverage gaps and conflicts --
            # is handed to the builder in a single call, and the payload it
            # returns is never mutated here. The engine deliberately passes NO
            # trusted negative result: no registered tool can yet return a
            # typed "no match" signal, so `not_found` is unreachable and is
            # never inferred from an empty field set (see .outcome).
            final = self._builder.build(evidence, verdicts, task_failures=failures,
                                        coverage_gaps=gaps,
                                        conflict_claim_ids=sorted(conflict_ids))
            return safe_durable_value(final)

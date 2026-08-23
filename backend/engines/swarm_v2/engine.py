"""Bounded, resumable Swarm V2 orchestration behind the Engine protocol."""

import json
from typing import Any, Callable, Iterable, Mapping

from .builder import FinalBuilder
from .commander import Commander
from .contracts import EvidenceReference, RemainingBudget
from .evidence import safe_durable_value
from .executor import BoundedTaskExecutor
from .state import SwarmState
from .verifier import Verifier
from .worker import TaskResult


class SwarmV2Engine:
    workflow_key = "swarm_v2"

    def __init__(self, *, commander: Commander, executor: BoundedTaskExecutor | None = None,
                 verifier: Verifier | None = None, builder: FinalBuilder | None = None,
                 evidence_loader: Callable[[Mapping[str, TaskResult]], Iterable[EvidenceReference]] | None = None,
                 checkpoint_sink: Callable[[str, dict], None] | None = None,
                 event_sink: Callable[[str, dict], None] | None = None,
                 usage_snapshot: Callable[[], Mapping[str, Any]] | None = None,
                 remaining_budget: Callable[[], RemainingBudget] | None = None):
        self._commander = commander
        self._executor, self._verifier = executor, verifier
        self._builder = builder or FinalBuilder()
        self._evidence_loader = evidence_loader or (lambda _: ())
        self._checkpoint_sink, self._event_sink = checkpoint_sink, event_sink
        self._usage_snapshot = usage_snapshot or (lambda: {})
        self._remaining_budget = remaining_budget or (lambda: RemainingBudget(
            cost_units=100_000, tool_calls=100, tasks=64, model_calls=100))

    @staticmethod
    def _canonical(value: Any) -> str:
        return json.dumps(safe_durable_value(value), sort_keys=True,
                          separators=(",", ":"), ensure_ascii=True)

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        # Vocabulary and payloads are deliberately narrow. Provider responses,
        # exception strings, credentials and reasoning never reach this sink.
        allowed = {"task_id", "status", "code", "tool", "graph_revision", "decision",
                   "claim_id", "source_id", "conflict_id", "verdict"}
        safe = {key: value for key, value in payload.items() if key in allowed and
                (value is None or isinstance(value, (str, int, float, bool)))}
        if self._event_sink:
            self._event_sink(kind, safe)

    def _save(self, state: SwarmState) -> None:
        if self._checkpoint_sink:
            checkpoint = {"phase": "swarm_v2", "version": state.engine_version,
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
        available_tools = max(
            0, remaining.tool_calls -
            sum(tool.max_calls for task in completed_specs for tool in task.tools)
        )
        available_tasks = max(0, remaining.tasks - len(completed_specs))
        # Each pending task performs one worker-model call. Keep two slots for
        # the next Commander decision and the verifier.
        required_model_calls = len(pending) + 2
        if (sum(task.estimated_cost_units for task in pending) > available_cost or
                sum(tool.max_calls for task in pending for tool in task.tools) > available_tools or
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

            values_by_scope: dict[tuple[str, str, str | None, str | None, str], set[str]] = {}
            for item in evidence:
                scope = (item.entity, item.field, item.geography, item.market,
                         self._canonical(item.time_scope))
                values_by_scope.setdefault(scope, set()).add(self._canonical(item.value))
            conflict_ids = {item.claim_id for item in evidence
                if len(values_by_scope[(item.entity, item.field, item.geography, item.market,
                    self._canonical(item.time_scope))]) > 1}
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
            decision = self._commander.replan(
                requested_model=requested_model, objective=objective, summary=summary
            )
            if decision.decision in {"ADD_TASKS", "REVISE_TASK"}:
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

            verdicts = self._verifier.verify(evidence, conflict_claim_ids=conflict_ids)
            state.verifier_state = {v.claim_id: v.model_dump(mode="json") for v in verdicts}
            state.usage_snapshot = dict(self._usage_snapshot())
            self._emit("verification_completed", {"status": "completed"})
            self._save(state)
            final = self._builder.build(evidence, verdicts)
            failures = [{"task_id": task_id,
                         "code": (result.error or {}).get("code", "TASK_FAILED")}
                        for task_id, result in sorted(execution.tasks.items())
                        if result.status != "completed"]
            non_verified = [v for v in verdicts if v.verdict != "verified"]
            if failures or gaps or conflict_ids or non_verified:
                final["status"] = "partial_success"
                final["needs_review"] = [*final["needs_review"], *failures, *gaps]
            return safe_durable_value(final)

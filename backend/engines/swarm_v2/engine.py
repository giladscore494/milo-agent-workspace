"""Bounded, resumable Swarm V2 orchestration behind the Engine protocol."""

import json
from typing import Any, Callable, Iterable, Mapping
from .commander import Commander
from .builder import FinalBuilder
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
            cost_units=100_000, tool_calls=100, tasks=64))

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

    def _check_feasible(self, plan: Any, completed: Mapping[str, TaskResult]) -> None:
        remaining = self._remaining_budget()
        pending = [task for task in plan.graph.tasks if task.task_id not in completed]
        if (sum(task.estimated_cost_units for task in pending) > remaining.cost_units or
                sum(tool.max_calls for task in pending for tool in task.tools) > remaining.tool_calls or
                len(pending) > remaining.tasks):
            raise ValueError("replacement plan exceeds remaining budget")

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
                               approved_plan=plan.model_dump(mode="json"))
            self._emit("commander_plan_created", {"graph_revision": 1})
            self._save(state)
        if self._executor is None or self._verifier is None:
            return {"status": "plan_validated", "plan": plan.model_dump(mode="json")}

        completed = {task_id: TaskResult(task_id, "completed", output=output)
                     for task_id, output in state.task_outputs.items()
                     if task_id in state.completed_task_ids}
        while True:
            def persist(result: TaskResult) -> None:
                output = safe_durable_value(dict(result.output or {}))
                state.completed_task_ids.append(result.task_id)
                state.completed_task_ids.sort()
                state.task_outputs[result.task_id] = output
                state.usage_snapshot = dict(self._usage_snapshot())
                self._save(state)  # completion is durable before another wave can start

            execution = self._executor.execute(plan.graph, completed=completed,
                event_sink=self._emit, task_completed=persist)
            completed = {key: value for key, value in execution.tasks.items() if value.status == "completed"}
            evidence = sorted(self._evidence_loader(execution.tasks), key=lambda x: x.claim_id)
            safe_evidence = safe_durable_value([item.model_dump(mode="json") for item in evidence])
            state.evidence_references = safe_evidence
            for item in evidence:
                self._emit("evidence_added", {"claim_id": item.claim_id, "source_id": item.source_id,
                                               "task_id": item.task_id})
            values_by_scope: dict[tuple[str, str, str | None, str | None, str], set[str]] = {}
            for item in evidence:
                scope = (item.entity, item.field, item.geography, item.market,
                         json.dumps(item.time_scope, sort_keys=True, separators=(",", ":")))
                values_by_scope.setdefault(scope, set()).add(repr(item.value))
            conflict_ids = {item.claim_id for item in evidence
                if len(values_by_scope[(item.entity, item.field, item.geography, item.market,
                    json.dumps(item.time_scope, sort_keys=True, separators=(",", ":")))]) > 1}
            for claim_id in sorted(conflict_ids):
                self._emit("conflict_found", {"claim_id": claim_id})
            summary = {"completed": sorted(completed),
                "failed": sorted(k for k, v in execution.tasks.items() if v.status != "completed"),
                "evidence": [{"claim_id": e.claim_id, "source_id": e.source_id,
                              "task_id": e.task_id, "field": e.field,
                              "confidence": e.confidence} for e in evidence],
                "conflicts": sorted(conflict_ids),
                "remaining_budget": self._remaining_budget().model_dump(mode="json")}
            decision = self._commander.replan(requested_model=requested_model, objective=objective, summary=summary)
            if decision.decision in {"ADD_TASKS", "REVISE_TASK"}:
                if len(state.replans) >= plan.max_replans:
                    raise ValueError("maximum replans exceeded")
                replacement = decision.plan
                assert replacement is not None
                if not set(completed) <= {t.task_id for t in replacement.graph.tasks}:
                    raise ValueError("replan cannot discard completed tasks")
                self._check_feasible(replacement, completed)
                state.replans.append({"decision": decision.decision, "reason": decision.reason})
                state.graph_revision += 1
                state.approved_plan = replacement.model_dump(mode="json")
                plan = replacement
                self._emit("commander_replanned", {"decision": decision.decision,
                                                    "graph_revision": state.graph_revision})
                self._save(state)
                continue
            verdicts = self._verifier.verify(evidence, conflict_claim_ids=conflict_ids)
            state.verifier_state = {v.claim_id: v.model_dump(mode="json") for v in verdicts}
            state.usage_snapshot = dict(self._usage_snapshot())
            self._emit("verification_completed", {"status": "completed"})
            self._save(state)
            final = self._builder.build(evidence, verdicts)
            failures = [{"task_id": task_id, "code": (result.error or {}).get("code", "TASK_FAILED")}
                        for task_id, result in sorted(execution.tasks.items())
                        if result.status != "completed"]
            if failures:
                final["status"] = "partial_success"
                final["needs_review"] = [*final["needs_review"], *failures]
            return final

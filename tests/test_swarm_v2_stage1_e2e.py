from __future__ import annotations

from copy import deepcopy

import pytest

from backend.engines.swarm_v2 import (
    BoundedTaskExecutor, Commander, CommanderModelResolver, EvidenceReference,
    FinalBuilder, PlanValidator, SwarmState, SwarmV2Engine, TaskResult, Verifier,
)
from backend.engines.swarm_v2.validation import PlanLimits
from test_swarm_v2 import plan, task


class Plans:
    def __init__(self, initial, decisions):
        self.initial, self.decisions = initial, iter(decisions)
        self.contexts = []
    def create_plan(self, **kwargs):
        self.contexts.append(kwargs["context"])
        return self.initial
    def create_replan(self, **kwargs):
        self.contexts.append(kwargs["summary"])
        return next(self.decisions)


class Worker:
    def __init__(self, calls): self.calls = calls
    def execute(self, spec, dependencies):
        self.calls.append(spec.task_id)
        return TaskResult(spec.task_id, "completed", {"answer": spec.task_id})


class VerifyGateway:
    def call(self, **kwargs):
        import json
        claims = json.loads(kwargs["messages"][1]["content"])
        return {"verdicts": [{"claim_id": c["claim_id"], "verdict": "verified", "reason": "supported"} for c in claims]}


def commander(client):
    return Commander(client=client, resolver=CommanderModelResolver(("fake",), {"fake"}),
        validator=PlanValidator(allowed_tools={"search"}, limits=PlanLimits(max_tasks=10, max_tool_calls=30)))


def evidence(results):
    refs = []
    for task_id, result in results.items():
        if result.status != "completed": continue
        field = "disputed" if task_id in {"a", "b"} else f"field_{task_id}"
        refs.append(EvidenceReference(claim_id=f"claim-{task_id}", source_id=f"source-{task_id}",
            run_id="run-1", task_id=task_id, field=field, value=result.output["answer"],
            confidence=.9, supported=True))
    return refs


def test_dynamic_parallel_replan_conflict_verification_final_and_events():
    initial = plan([task("a", "alpha"), task("b", "beta"), task("c", "combine", dependencies=["a", "b"])],
                   contexts={"c": ["a", "b"]})
    revised = plan([*initial["graph"]["tasks"], task("follow", "resolve conflict", dependencies=["a", "b"])],
                   contexts={"c": ["a", "b"], "follow": ["a", "b"]})
    client = Plans(initial, [
        {"decision": "ADD_TASKS", "plan": revised, "reason": "explicit evidence conflict"},
        {"decision": "REQUEST_VERIFICATION", "plan": None, "reason": "follow-up complete"},
    ])
    calls, events, checkpoints = [], [], []
    engine = SwarmV2Engine(commander=commander(client),
        executor=BoundedTaskExecutor(worker_factory=lambda: Worker(calls), max_active_workers=2),
        verifier=Verifier(gateway=VerifyGateway(), model="fake"), builder=FinalBuilder(),
        evidence_loader=evidence, event_sink=lambda kind, payload: events.append((kind, payload)),
        checkpoint_sink=lambda phase, value: checkpoints.append(deepcopy(value)))
    result = engine.run({"id": "run-1", "input": {"objective": "taxonomy neutral", "commander_model": "fake"}})
    assert sorted(calls) == ["a", "b", "c", "follow"]  # completed tasks did not rerun after replan
    assert result["status"] == "complete"
    assert "disputed" not in result["fields"]
    assert {x["field"] for x in result["needs_review"]} == {"disputed"}
    assert result == FinalBuilder().build(evidence({k: TaskResult(k, "completed", {"answer": k}) for k in calls}),
        Verifier(gateway=VerifyGateway(), model="fake").verify(evidence({k: TaskResult(k, "completed", {"answer": k}) for k in calls}), conflict_claim_ids={"claim-a", "claim-b"}))
    kinds = {kind for kind, _ in events}
    assert {"commander_plan_created", "task_ready", "task_started", "task_completed", "evidence_added",
            "conflict_found", "commander_replanned", "verification_completed"} <= kinds
    assert all("provider" not in str(payload).lower() and "exception" not in payload for _, payload in events)
    assert checkpoints[-1]["artifacts"]["swarm_state"]["completed_task_ids"] == ["a", "b", "c", "follow"]
    assert all(set(context) <= {"completed", "failed", "evidence", "conflicts", "remaining_budget"}
               for context in client.contexts[1:])


def test_resume_skips_completed_tasks_and_rejects_cross_workflow_or_version():
    initial = plan([task("a", "alpha"), task("b", "beta", dependencies=["a"])], contexts={"b": ["a"]})
    state = SwarmState(run_id="run-1", objective="resume", approved_plan=initial,
        completed_task_ids=["a"], task_outputs={"a": {"answer": "a"}})
    calls = []
    client = Plans(initial, [{"decision": "FINISH", "plan": None, "reason": "done"}])
    engine = SwarmV2Engine(commander=commander(client),
        executor=BoundedTaskExecutor(worker_factory=lambda: Worker(calls), max_active_workers=2),
        verifier=Verifier(gateway=VerifyGateway(), model="fake"), evidence_loader=evidence)
    engine.run({"id": "run-1", "input": {"commander_model": "fake"},
                "checkpoint": {"artifacts": {"swarm_state": state.model_dump(mode="json")}}})
    assert calls == ["b"]
    for change in ({"workflow_key": "vehicle_catalog_v1"}, {"engine_version": "swarm_v2.0"}, {"run_id": "other"}):
        raw = state.model_dump(mode="json") | change
        with pytest.raises(ValueError, match="incompatible"):
            SwarmState.resume(raw, run_id="run-1")


def test_builder_is_deterministic_traceable_and_excludes_unsupported_claims():
    supported = EvidenceReference(claim_id="c1", source_id="s1", run_id="r1", task_id="t1",
        field="answer", value={"x": 1}, confidence=.9, supported=True)
    unsupported = EvidenceReference(claim_id="c2", source_id="s2", run_id="r1", task_id="t2",
        field="unsafe", value="invented", confidence=.9, supported=False)
    gateway = VerifyGateway()
    verdicts = Verifier(gateway=gateway, model="fake").verify([unsupported, supported])
    first = FinalBuilder().build([unsupported, supported], verdicts)
    second = FinalBuilder().build([supported, unsupported], reversed(verdicts))
    assert first == second
    assert "unsafe" not in first["fields"]
    assert first["fields"]["answer"][0]["provenance"] == {
        "claim_id": "c1", "source_id": "s1", "run_id": "r1", "task_id": "t1"}


def test_conflicts_require_same_full_scope():
    def ref(claim, entity, geography, value):
        return EvidenceReference(claim_id=claim, source_id=f"s-{claim}", run_id="r", task_id="t",
            entity=entity, field="price", geography=geography, market="retail",
            time_scope={"year": 2026}, value=value, confidence=.9)
    different = [ref("c1", "one", "IL", 1), ref("c2", "two", "IL", 2)]
    same = [ref("c3", "shared", "IL", 1), ref("c4", "shared", "IL", 2)]
    client = Plans(plan([task("a", "a")]), [{"decision": "FINISH", "plan": None, "reason": "done"}])
    events = []
    engine = SwarmV2Engine(commander=commander(client),
        executor=BoundedTaskExecutor(worker_factory=lambda: Worker([]), max_active_workers=1),
        verifier=Verifier(gateway=VerifyGateway(), model="fake"),
        evidence_loader=lambda _: different + same, event_sink=lambda k, p: events.append((k, p)))
    result = engine.run({"id": "run-1", "input": {"objective": "scope", "commander_model": "fake"}})
    conflicts = {payload["claim_id"] for kind, payload in events if kind == "conflict_found"}
    assert conflicts == {"c3", "c4"}
    assert {item["provenance"]["claim_id"] for item in result["fields"]["price"]} == {"c1", "c2"}


def test_unsafe_task_output_has_zero_checkpoint_mutation():
    initial = plan([task("a", "a")])
    class UnsafeWorker:
        def execute(self, spec, dependencies):
            return TaskResult("a", "completed", {"answer": {"chain_of_thought": "secret sentinel"}})
    writes = []
    engine = SwarmV2Engine(commander=commander(Plans(initial, [])),
        executor=BoundedTaskExecutor(worker_factory=UnsafeWorker, max_active_workers=1),
        verifier=Verifier(gateway=VerifyGateway(), model="fake"),
        checkpoint_sink=lambda phase, value: writes.append(value))
    with pytest.raises(ValueError, match="unsafe evidence"):
        engine.run({"id": "run-1", "input": {"objective": "safe", "commander_model": "fake"}})
    assert len(writes) == 1  # initial safe plan only; unsafe completion made zero writes
    assert writes[0]["artifacts"]["swarm_state"]["completed_task_ids"] == []


def test_over_budget_replan_rejected_before_followup_execution():
    initial = plan([task("a", "a")])
    revised = plan([*initial["graph"]["tasks"], task("expensive", "expensive", cost=10)])
    client = Plans(initial, [{"decision": "ADD_TASKS", "plan": revised, "reason": "gap"}])
    calls = []
    from backend.engines.swarm_v2 import RemainingBudget
    engine = SwarmV2Engine(commander=commander(client),
        executor=BoundedTaskExecutor(worker_factory=lambda: Worker(calls), max_active_workers=1),
        verifier=Verifier(gateway=VerifyGateway(), model="fake"),
        remaining_budget=lambda: RemainingBudget(cost_units=0, tool_calls=0, tasks=0))
    with pytest.raises(ValueError, match="remaining budget"):
        engine.run({"id": "run-1", "input": {"objective": "budget", "commander_model": "fake"}})
    assert calls == ["a"]

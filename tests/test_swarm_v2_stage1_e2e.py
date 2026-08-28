from __future__ import annotations

from copy import deepcopy

import pytest

from backend.engines.swarm_v2 import (
    BoundedTaskExecutor, Commander, CommanderModelResolver, EvidenceReference,
    FinalBuilder, PlanValidator, ResolvedSourceEvidence, SourceFragment, SwarmState,
    SwarmV2Engine, TaskResult, Verifier,
)
from backend.engines.swarm_v2.fragments import MAX_FRAGMENT_CHARS, fragment_content_hash
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


def source_context(source_id: str, task_id: str, *, texts=()) -> ResolvedSourceEvidence:
    """One B2-shaped durable source context, built offline."""
    return ResolvedSourceEvidence(
        source_id=source_id, task_id=task_id, url=f"https://example.test/{source_id}",
        title="Evidence", domain="example.test", source_type="primary",
        source_strength="strong", source_date="2026-08",
        fragments=tuple(SourceFragment(fragment_index=index, content_hash=fragment_content_hash(text),
                                       text=text)
                        for index, text in enumerate(texts)))


class StubResolver:
    """Offline EvidenceResolver: durable-style source context, no repository.

    It mirrors RepositoryEvidenceResolver's contract -- one bounded context
    per REFERENCED source id -- while touching no database, no URL, no tool
    and no provider. `texts` supplies explicit fragments per source; any
    source without an entry gets one fragment quoting the claim's own value,
    and an entry of `()` is a source that captured no evidence at all.
    """

    def __init__(self, texts: dict[str, tuple[str, ...]] | None = None):
        self._texts = dict(texts or {})
        self.calls: list[list[str]] = []

    def resolve(self, evidence):
        references = list(evidence)
        self.calls.append(sorted({item.source_id for item in references}))
        contexts = {}
        for item in references:
            texts = self._texts.get(item.source_id)
            if texts is None:
                # Bounded exactly like a durable B2 fragment, whatever the
                # claim value's size: evidence is never a copy of the payload.
                texts = (f"The recorded value is {item.value}."[:MAX_FRAGMENT_CHARS],)
            contexts[item.source_id] = source_context(item.source_id, item.task_id, texts=texts)
        return contexts


class VerifyGateway:
    """Offline verifier model that cites one real supplied fragment hash."""

    def call(self, **kwargs):
        import json
        document = json.loads(kwargs["messages"][1]["content"])
        hashes = {source["source_id"]: [item["content_hash"] for item in source["fragments"]]
                  for source in document["sources"]}
        return {"verdicts": [{"claim_id": claim["claim_id"], "verdict": "verified",
                              "reason": "supported",
                              "supporting_fragment_hashes": hashes[claim["source_id"]][:1]}
                             for claim in document["claims"]]}


def commander(client):
    return Commander(client=client, resolver=CommanderModelResolver(("fake",), {"fake"}),
        validator=PlanValidator(allowed_tools={"search"}, limits=PlanLimits(max_tasks=10, max_tool_calls=30)))


def evidence(results):
    refs = []
    for task_id, result in results.items():
        if result.status != "completed": continue
        entity = "shared" if task_id in {"a", "b"} else task_id
        refs.append(EvidenceReference(claim_id=f"claim-{task_id}", source_id=f"source-{task_id}",
            run_id="run-1", task_id=task_id, entity=entity, field="answer",
            value=result.output["answer"], confidence=.9, supported=True))
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
        verifier=Verifier(gateway=VerifyGateway(), model="fake", resolver=StubResolver()), builder=FinalBuilder(),
        evidence_loader=evidence, event_sink=lambda kind, payload: events.append((kind, payload)),
        checkpoint_sink=lambda phase, value: checkpoints.append(deepcopy(value)))
    result = engine.run({"id": "run-1", "input": {"objective": "taxonomy neutral", "commander_model": "fake"}})
    assert sorted(calls) == ["a", "b", "c", "follow"]  # completed tasks did not rerun after replan
    assert result["status"] == "partial_success"
    assert {x["provenance"]["claim_id"] for x in result["needs_review"]} == {
        "claim-a", "claim-b"}
    assert {item["provenance"]["claim_id"] for item in result["fields"]["answer"]} == {
        "claim-c", "claim-follow"}
    kinds = {kind for kind, _ in events}
    assert {"commander_plan_created", "task_ready", "task_started", "task_completed", "evidence_added",
            "conflict_found", "commander_replanned", "verification_completed"} <= kinds
    assert all("provider" not in str(payload).lower() and "exception" not in payload for _, payload in events)
    assert checkpoints[-1]["artifacts"]["swarm_state"]["completed_task_ids"] == ["a", "b", "c", "follow"]
    assert all(set(context) <= {"completed", "failed", "evidence", "conflicts", "gaps",
                                        "remaining_budget", "decision_context"}
               for context in client.contexts[1:])
    assert client.contexts[1]["decision_context"] == {
        "all_tasks_completed": True,
        "has_unresolved_issues": True,
        "valid_terminal_decision": "REQUEST_VERIFICATION",
    }


def test_resume_skips_completed_tasks_and_preserves_checkpoint_evidence():
    initial = plan([task("done", "alpha"), task("next", "beta", dependencies=["done"])],
                   contexts={"next": ["done"]})
    saved_result = TaskResult("done", "completed", {"answer": "done"})
    saved_evidence = evidence({"done": saved_result})[0].model_dump(mode="json")
    state = SwarmState(run_id="run-1", objective="resume", approved_plan=initial,
        completed_task_ids=["done"], task_outputs={"done": {"answer": "done"}},
        evidence_references=[saved_evidence])
    calls, checkpoints = [], []
    client = Plans(initial, [{"decision": "FINISH", "plan": None, "reason": "done"}])
    engine = SwarmV2Engine(commander=commander(client),
        executor=BoundedTaskExecutor(worker_factory=lambda: Worker(calls), max_active_workers=2),
        verifier=Verifier(gateway=VerifyGateway(), model="fake", resolver=StubResolver()),
        evidence_loader=lambda results: evidence({
            key: value for key, value in results.items() if key == "next"}),
        checkpoint_sink=lambda phase, value: checkpoints.append(deepcopy(value)))
    result = engine.run({"id": "run-1", "input": {"commander_model": "fake"},
                "checkpoint": {"artifacts": {"swarm_state": state.model_dump(mode="json")}}})
    assert calls == ["next"]
    assert result["status"] == "complete"
    assert {item["provenance"]["claim_id"] for item in result["fields"]["answer"]} == {
        "claim-done", "claim-next"}
    assert {item["claim_id"] for item in
            checkpoints[-1]["artifacts"]["swarm_state"]["evidence_references"]} == {
        "claim-done", "claim-next"}
    assert client.contexts[-1]["decision_context"] == {
        "all_tasks_completed": True,
        "has_unresolved_issues": False,
        "valid_terminal_decision": "FINISH",
    }


def test_resume_rejects_cross_workflow_or_version():
    initial = plan([task("a", "alpha")])
    state = SwarmState(run_id="run-1", objective="resume", approved_plan=initial)
    for change in ({"workflow_key": "vehicle_catalog_v1"},
                   {"engine_version": "swarm_v2.0"}, {"run_id": "other"}):
        raw = state.model_dump(mode="json") | change
        with pytest.raises(ValueError, match="incompatible"):
            SwarmState.resume(raw, run_id="run-1")


def test_builder_is_deterministic_traceable_and_excludes_unsupported_claims():
    supported = EvidenceReference(claim_id="c1", source_id="s1", run_id="r1", task_id="t1",
        field="answer", value={"x": 1}, confidence=.9, supported=True)
    unsupported = EvidenceReference(claim_id="c2", source_id="s2", run_id="r1", task_id="t2",
        field="unsafe", value="invented", confidence=.9, supported=False)
    gateway = VerifyGateway()
    verdicts = Verifier(gateway=gateway, model="fake",
                       resolver=StubResolver()).verify([unsupported, supported])
    first = FinalBuilder().build([unsupported, supported], verdicts)
    second = FinalBuilder().build([supported, unsupported], reversed(verdicts))
    assert first == second
    assert "unsafe" not in first["fields"]
    assert first["fields"]["answer"][0]["provenance"] == {
        "claim_id": "c1", "source_id": "s1", "run_id": "r1", "task_id": "t1",
        "scope": {"entity": "general", "field": "answer", "geography": None,
                  "market": None, "time_scope": {}}}


def test_conflicts_require_same_full_scope():
    def ref(claim, entity, geography, value):
        return EvidenceReference(claim_id=claim, source_id=f"s-{claim}",
            run_id="run-1", task_id="a", entity=entity, field="price",
            geography=geography, market="retail", time_scope={"year": 2026},
            value=value, confidence=.9)
    different = [ref("c1", "one", "IL", 1), ref("c2", "two", "IL", 2)]
    same = [ref("c3", "shared", "IL", 1), ref("c4", "shared", "IL", 2)]
    planned = task("a", "a")
    planned["evidence"]["required_fields"] = ["price"]
    client = Plans(plan([planned]), [{"decision": "FINISH", "plan": None, "reason": "done"}])
    events = []
    engine = SwarmV2Engine(commander=commander(client),
        executor=BoundedTaskExecutor(worker_factory=lambda: Worker([]), max_active_workers=1),
        verifier=Verifier(gateway=VerifyGateway(), model="fake", resolver=StubResolver()),
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
        verifier=Verifier(gateway=VerifyGateway(), model="fake", resolver=StubResolver()),
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
        verifier=Verifier(gateway=VerifyGateway(), model="fake", resolver=StubResolver()),
        remaining_budget=lambda: (RemainingBudget(
            cost_units=100, tool_calls=30, tasks=10, model_calls=10)
            if not calls else RemainingBudget(
                cost_units=0, tool_calls=0, tasks=0, model_calls=0)))
    with pytest.raises(ValueError, match="remaining budget"):
        engine.run({"id": "run-1", "input": {"objective": "budget", "commander_model": "fake"}})
    assert calls == ["a"]


def test_logically_equal_json_values_do_not_create_false_conflicts():
    planned = task("a", "a")
    planned["evidence"]["required_fields"] = ["price"]
    refs = [
        EvidenceReference(claim_id="c1", source_id="s1", run_id="run-1",
            task_id="a", entity="same", field="price", value={"a": 1, "b": 2},
            confidence=.9),
        EvidenceReference(claim_id="c2", source_id="s2", run_id="run-1",
            task_id="a", entity="same", field="price", value={"b": 2, "a": 1},
            confidence=.9),
    ]
    events = []
    engine = SwarmV2Engine(
        commander=commander(Plans(plan([planned]), [
            {"decision": "FINISH", "plan": None, "reason": "done"}])),
        executor=BoundedTaskExecutor(worker_factory=lambda: Worker([]), max_active_workers=1),
        verifier=Verifier(gateway=VerifyGateway(), model="fake", resolver=StubResolver()),
        evidence_loader=lambda _: refs, event_sink=lambda kind, payload: events.append((kind, payload)),
    )
    result = engine.run({"id": "run-1", "input": {
        "objective": "canonical values", "commander_model": "fake"}})
    assert result["status"] == "complete"
    assert not [event for event in events if event[0] == "conflict_found"]


def test_replan_cannot_mutate_a_completed_task():
    initial = plan([task("a", "a")])
    changed = plan([task("a", "changed")])
    client = Plans(initial, [
        {"decision": "REVISE_TASK", "plan": changed, "reason": "missing evidence"}])
    calls = []
    engine = SwarmV2Engine(
        commander=commander(client),
        executor=BoundedTaskExecutor(worker_factory=lambda: Worker(calls), max_active_workers=1),
        verifier=Verifier(gateway=VerifyGateway(), model="fake", resolver=StubResolver()),
    )
    with pytest.raises(ValueError, match="cannot revise or discard"):
        engine.run({"id": "run-1", "input": {
            "objective": "immutable completion", "commander_model": "fake"}})
    assert calls == ["a"]


def test_finish_fails_closed_when_required_evidence_is_missing():
    initial = plan([task("a", "a")])
    engine = SwarmV2Engine(
        commander=commander(Plans(initial, [
            {"decision": "FINISH", "plan": None, "reason": "done"}])),
        executor=BoundedTaskExecutor(worker_factory=lambda: Worker([]), max_active_workers=1),
        verifier=Verifier(gateway=VerifyGateway(), model="fake", resolver=StubResolver()),
    )
    with pytest.raises(ValueError, match="completion criteria"):
        engine.run({"id": "run-1", "input": {
            "objective": "evidence required", "commander_model": "fake"}})

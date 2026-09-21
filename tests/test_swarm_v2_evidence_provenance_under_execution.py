"""Evidence recorded DURING a task must not fail the run before the task completes.

The Evidence Board records a claim the moment a trusted tool result is mapped
-- inside the worker's tool loop, BEFORE that task's model call, and while
sibling tasks run on other threads of the bounded executor. The engine's
evidence merge is stated over COMPLETED tasks. Production wiring handed the
engine every claim on the board regardless, and the engine treated a claim
for a task that had not completed as corrupt provenance and raised. Two
reachable triggers, both with the Government read tool registered:

* sequential: a task's register read succeeds, its claim is recorded, and the
  task then fails (schema-invalid output twice, oversized material, provider
  backpressure). `allow_partial` was supposed to make that a partial result;
  instead the run failed with SWARM_V2_EXECUTION_FAILED.
* parallel: task B's claim lands while B is still awaiting its model call;
  task A completes first, and persisting A merges evidence -- and finds B's.

Either way the run had already paid for the work, and every resume repeated
the failure. These tests drive the REAL engine, executor and Evidence Board
offline; nothing calls a provider.
"""

from __future__ import annotations

import threading
from uuid import uuid4

import pytest

from backend.engines.swarm_v2 import (BoundedTaskExecutor, EvidenceReference, FinalBuilder,
                                      SwarmV2Engine, TaskResult, Verifier)
from backend.engines.swarm_v2.evidence import EvidenceBoard, WorkerLease
from backend.engines.swarm_v2.state import SwarmState
from backend.schemas import ClaimCreate, SourceCreate
from backend.worker.main import evidence_of_completed_tasks
from test_swarm_v2 import plan, task
from test_swarm_v2_evidence import GuardedEvidenceRepository
from test_swarm_v2_stage1_e2e import Plans, StubResolver, VerifyGateway, commander
from tests.worker_fence import FENCE


def fresh_board():
    run_id = uuid4()
    lease = WorkerLease(run_id, "worker-1", 1, "lease-token")
    repo = GuardedEvidenceRepository(lease)
    return EvidenceBoard(repo, lease), repo, run_id


def record(board: EvidenceBoard, task_id: str, value: str) -> None:
    """What the trusted tool-result sink does for a mapped operation."""
    source = board.record_source(
        SourceCreate(**FENCE, agent="w", url=f"https://example.test/{task_id}", title=task_id.upper(),
                     domain="example.test", source_type="primary", source_strength="strong",
                     query="q", tool_operation="search.search"),
        task_key=task_id)
    board.record_claim(ClaimCreate(**FENCE, entity_key=f"vehicle:{task_id}", field_key="answer",
                                   value=value, source_id=source["id"],
                                   source_strength="strong", confidence=.9, agent="w"),
                       task_key=task_id)


def two_tasks(*, b_partial: bool) -> dict:
    ta, tb = task("a", "alpha"), task("b", "beta")
    ta["evidence"] = {"minimum_sources": 0, "required_fields": [], "min_confidence": 0.5}
    ta["completion"]["evidence_satisfied"] = False
    tb["evidence"] = {"minimum_sources": 0, "required_fields": [], "min_confidence": 0.5}
    tb["completion"]["evidence_satisfied"] = False
    tb["completion"]["allow_partial"] = b_partial
    return plan([ta, tb])


def engine_for(board, worker_factory, *, plan_dict, width, loader, checkpoints):
    """The real engine; `checkpoints` collects the evidence task ids the run
    made DURABLE at each save -- what the run actually kept, whatever the
    loader handed it."""

    def checkpoint_sink(_phase, checkpoint):
        state = checkpoint["artifacts"]["swarm_state"]
        checkpoints.append(sorted({item["task_id"] for item in state["evidence_references"]}))

    client = Plans(plan_dict, [{"decision": "FINISH", "plan": None, "reason": "done"}])
    return SwarmV2Engine(
        commander=commander(client),
        executor=BoundedTaskExecutor(worker_factory=worker_factory, max_active_workers=width),
        verifier=Verifier(gateway=VerifyGateway(), model="fake", resolver=StubResolver()),
        builder=FinalBuilder(), evidence_loader=loader, checkpoint_sink=checkpoint_sink)


def production_loader(board):
    return lambda results: evidence_of_completed_tasks(board, results)


def superseded_loader(board):
    """The wiring PR #102 shipped: every claim on the board, whatever its task did."""
    return lambda _results: [EvidenceReference.model_validate(item) for item in board.references()]


# =============================================================================
# 1. sequential: evidence, then failure
# =============================================================================

class RecordsThenFails:
    """Task b acquires evidence through the board and THEN fails."""

    def __init__(self, board):
        self._board = board

    def execute(self, spec, dependencies):
        if spec.task_id == "b":
            record(self._board, "b", "x")
            return TaskResult("b", "failed", error={"code": "WORKER_OUTPUT_SCHEMA_INVALID"})
        return TaskResult(spec.task_id, "completed", {"answer": spec.task_id})


@pytest.mark.parametrize("loader", [production_loader, superseded_loader],
                         ids=["production_loader", "superseded_unfiltered_loader"])
def test_a_task_that_records_evidence_and_then_fails_yields_a_partial_result(loader):
    """`allow_partial` means partial, not SWARM_V2_EXECUTION_FAILED.

    Parametrized over the superseded loader too: the engine alone now closes
    this, so a loader that hands back too much can no longer fail a run.
    """
    board, repo, run_id = fresh_board()
    checkpoints: list[list[str]] = []
    engine = engine_for(board, lambda: RecordsThenFails(board), plan_dict=two_tasks(b_partial=True),
                        width=1, loader=loader(board), checkpoints=checkpoints)
    result = engine.run({"id": str(run_id), "input": {"objective": "o", "commander_model": "fake"}})
    assert result["status"] == "partial_success", result
    # The failed task's evidence never became part of the run's evidence...
    assert checkpoints and all("b" not in tasks for tasks in checkpoints), checkpoints
    # ...but it was not destroyed: it is durable, and available to a later task.
    assert [row["task_key"] for row in repo.tables["claim"].values()] == ["b"]


def test_the_control_without_evidence_gives_the_same_partial_result():
    """The outcome must not depend on whether the failed task recorded evidence."""

    class Fails:
        def execute(self, spec, dependencies):
            if spec.task_id == "b":
                return TaskResult("b", "failed", error={"code": "WORKER_OUTPUT_SCHEMA_INVALID"})
            return TaskResult(spec.task_id, "completed", {"answer": spec.task_id})

    board, _repo, run_id = fresh_board()
    engine = engine_for(board, Fails, plan_dict=two_tasks(b_partial=True), width=1,
                        loader=production_loader(board), checkpoints=[])
    result = engine.run({"id": str(run_id), "input": {"objective": "o", "commander_model": "fake"}})
    assert result["status"] == "partial_success"


# =============================================================================
# 2. parallel: a sibling completes while this task's claim is on the board
# =============================================================================

class RecordsThenWaitsForSibling:
    """Task b records its claim, then does not complete until a has been PERSISTED;
    task a does not complete until b's claim is on the board.

    That pins the production interleaving with two active workers: b's
    register read is mapped and recorded, b is still awaiting its model call,
    and a finishes -- the engine persists a and merges evidence with b's
    claim on the board and b in flight.
    """

    def __init__(self, board, b_recorded: threading.Event, a_persisted: threading.Event):
        self._board, self._b_recorded, self._a_persisted = board, b_recorded, a_persisted

    def execute(self, spec, dependencies):
        if spec.task_id == "b":
            record(self._board, "b", "x")
            self._b_recorded.set()
            assert self._a_persisted.wait(timeout=10.0), "task a was never persisted"
            return TaskResult("b", "completed", {"answer": "b"})
        record(self._board, "a", "y")
        assert self._b_recorded.wait(timeout=10.0), "task b never recorded its claim"
        return TaskResult("a", "completed", {"answer": "a"})


@pytest.mark.parametrize("loader", [production_loader, superseded_loader],
                         ids=["production_loader", "superseded_unfiltered_loader"])
def test_a_sibling_completing_first_does_not_fail_the_run(loader):
    board, _repo, run_id = fresh_board()
    b_recorded, a_persisted = threading.Event(), threading.Event()
    checkpoints: list[list[str]] = []

    def loader_that_signals(results):
        refs = loader(board)(results)
        if "a" in results and results["a"].status == "completed":
            a_persisted.set()
        return refs

    engine = engine_for(board, lambda: RecordsThenWaitsForSibling(board, b_recorded, a_persisted),
                        plan_dict=two_tasks(b_partial=False), width=2,
                        loader=loader_that_signals, checkpoints=checkpoints)
    result = engine.run({"id": str(run_id), "input": {"objective": "o", "commander_model": "fake"}})
    assert result["status"] in {"complete", "partial_success"}, result
    # Persisting a kept a's evidence only, with b's claim already on the
    # board; once b completed, the run's evidence held both.
    assert ["a"] in checkpoints, checkpoints
    assert checkpoints[-1] == ["a", "b"], checkpoints


# =============================================================================
# 3. the engine's remaining strictness, stated
# =============================================================================

def reference(run_id: str, task_id: str, claim_id: str = "claim-1") -> dict:
    return {"claim_id": claim_id, "source_id": "source-1", "run_id": run_id,
            "task_id": task_id, "entity": "vehicle:1", "field": "answer", "value": "x",
            "confidence": 0.9, "supported": True}


def bare_engine() -> SwarmV2Engine:
    return SwarmV2Engine(commander=commander(Plans(two_tasks(b_partial=True), [])))


def test_a_checkpointed_reference_for_an_uncompleted_task_is_still_refused():
    """A checkpoint was written by the merge itself; disagreement is corruption."""
    state = SwarmState(run_id="run-1", objective="o", approved_plan={},
                       evidence_references=[reference("run-1", "b")])
    with pytest.raises(ValueError, match="incompatible evidence provenance"):
        bare_engine()._merge_evidence(state, [], {"a"})


def test_a_live_reference_from_another_run_is_still_refused():
    state = SwarmState(run_id="run-1", objective="o", approved_plan={})
    with pytest.raises(ValueError, match="incompatible evidence provenance"):
        bare_engine()._merge_evidence(state, [reference("run-2", "a")], {"a"})


def test_a_live_reference_for_an_uncompleted_task_is_left_out_not_refused():
    state = SwarmState(run_id="run-1", objective="o", approved_plan={})
    merged = bare_engine()._merge_evidence(
        state, [reference("run-1", "a", "claim-a"), reference("run-1", "b", "claim-b")], {"a"})
    assert [item.claim_id for item in merged] == ["claim-a"]
    assert [item["claim_id"] for item in state.evidence_references] == ["claim-a"]


# =============================================================================
# 4. the production loader and the board
# =============================================================================

def test_the_production_loader_reads_evidence_of_completed_tasks_only():
    board, _repo, _run_id = fresh_board()
    record(board, "a", "1")
    record(board, "b", "2")
    record(board, "c", "3")
    results = {"a": TaskResult("a", "completed", {"answer": "1"}),
               "b": TaskResult("b", "failed", error={"code": "TASK_FAILED"})}
    refs = evidence_of_completed_tasks(board, results)
    assert [item.task_id for item in refs] == ["a"]
    assert all(isinstance(item, EvidenceReference) for item in refs)
    assert evidence_of_completed_tasks(board, {}) == []


def test_references_can_be_asked_for_by_task_and_default_to_everything():
    board, _repo, _run_id = fresh_board()
    record(board, "a", "1")
    record(board, "b", "2")
    assert sorted(item["task_id"] for item in board.references()) == ["a", "b"]
    assert [item["task_id"] for item in board.references(task_ids={"b"})] == ["b"]
    assert board.references(task_ids=set()) == []


def test_concurrent_recording_never_breaks_a_reader():
    """Two workers recording while the engine thread reads: no RuntimeError."""
    board, _repo, _run_id = fresh_board()
    stop = threading.Event()
    failures: list[BaseException] = []

    def writer(label):
        n = 0
        while not stop.is_set() and n < 200:
            try:
                record(board, f"{label}{n}", "v")
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                failures.append(exc)
                return
            n += 1

    def reader():
        while not stop.is_set():
            try:
                board.references()
                board.persist_trace_summary()
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                failures.append(exc)
                return

    threads = [threading.Thread(target=writer, args=("x",)),
               threading.Thread(target=writer, args=("y",)),
               threading.Thread(target=reader)]
    [t.start() for t in threads]
    threads[0].join(); threads[1].join()
    stop.set(); threads[2].join()
    assert failures == [], failures
    assert len(board.references()) == 400

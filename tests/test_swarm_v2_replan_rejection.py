"""PR-X: an invalid replan decision never discards completed work.

Run c4b8bb54 completed 11/11 tasks with evidence, then failed the WHOLE run
with COMMANDER_DECISION_INVALID on the post-execution replan decision (one
`commander:replanning` call, ~12s). Every paid task result was discarded.

These tests replay that shape offline through the REAL engine, executor,
Commander + PlanValidator, Verifier and FinalBuilder: eleven tasks complete
with evidence and the ONE replan decision is refused by the firewall. The run
must reach verification and finalization with its evidence, as
partial_success, carrying the static COMMANDER_REPLAN_REJECTED review item --
and ask the Commander exactly once.
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from backend.budget import BudgetExceeded
from backend.engines.swarm_v2 import (BoundedTaskExecutor, Commander, CommanderModelResolver,
                                      EvidenceReference, FinalBuilder, PlanLimits, PlanValidator,
                                      SwarmState, SwarmV2Engine, TaskResult, Verifier,
                                      validate_product_outcome)
from backend.engines.swarm_v2.commander import CommanderPlanFailure
from backend.engines.swarm_v2.engine import COMMANDER_REPLAN_REJECTED
from backend.provider_streaming import PROVIDER_STREAM_INTERRUPTED, ProviderTransportFailure
from backend.runtime import CancellationRequested
from test_swarm_v2 import plan, task, tool_descriptors
from test_swarm_v2_stage1_e2e import Plans, StubResolver, VerifyGateway

RUN_ID = "run-c4b8bb54"
TASK_IDS = [f"t{index:02d}" for index in range(1, 12)]
REJECTED_ITEM = {"task_id": "commander_replan", "code": COMMANDER_REPLAN_REJECTED}


def eleven_task_plan(*, max_replans: int = 1) -> dict:
    return plan([task(task_id, f"research {task_id}") for task_id in TASK_IDS],
                max_replans=max_replans)


def invalid_nested_plan() -> dict:
    """A replacement plan whose one added task has a nested-invalid output_schema."""
    broken = task("t12", "follow up")
    broken["output_schema"] = {
        "type": "object",
        "properties": {"answer": {"type": "object",
                                  "properties": {"inner": {"type": "banana"}}}},
        "required": ["answer"], "additionalProperties": False,
    }
    return plan([*eleven_task_plan()["graph"]["tasks"], broken], max_replans=1)


#: The five invalid decisions of the replay. Each is refused by the firewall.
INVALID_DECISIONS = {
    "extra_top_level_key": {"decision": "FINISH", "plan": None, "reason": "done",
                            "confidential_note": "value that must never be logged"},
    "finish_with_plan": {"decision": "FINISH", "plan": eleven_task_plan(),
                         "reason": "all tasks completed"},
    "reason_600_chars": {"decision": "FINISH", "plan": None, "reason": "x" * 600},
    "add_tasks_nested_invalid_output_schema": {
        "decision": "ADD_TASKS", "plan": invalid_nested_plan(), "reason": "follow up"},
    "non_json_text": "I believe the research is complete; no further tasks are needed.",
    # Well-formed, but refused by the deterministic plan firewall (13 > 12 tasks).
    "add_tasks_over_plan_limits": {
        "decision": "ADD_TASKS", "reason": "more",
        "plan": plan([task(f"t{index:02d}", f"research t{index:02d}")
                      for index in range(1, 14)], max_replans=1)},
}


class Worker:
    def __init__(self, calls: list[str], *, fail: bool = False):
        self.calls, self.fail = calls, fail

    def execute(self, spec, dependencies):
        self.calls.append(spec.task_id)
        if self.fail:
            return TaskResult(spec.task_id, "failed", error={"code": "TASK_FAILED"})
        return TaskResult(spec.task_id, "completed", {"answer": f"value {spec.task_id}"})


def evidence(results):
    return [EvidenceReference(claim_id=f"claim-{task_id}", source_id=f"source-{task_id}",
                              run_id=RUN_ID, task_id=task_id, entity=f"entity-{task_id}",
                              field="answer", value=result.output["answer"], confidence=0.9)
            for task_id, result in results.items() if result.status == "completed"]


class Ledger:
    def __init__(self):
        self.kinds: list[str] = []

    def __call__(self, kind: str) -> None:
        self.kinds.append(kind)


def build(decisions, *, fail_tasks: bool = False, initial: dict | None = None):
    client = Plans(initial or eleven_task_plan(), decisions)
    calls, checkpoints, events, ledger = [], [], [], Ledger()
    commander = Commander(client=client, resolver=CommanderModelResolver(("fake",), {"fake"}),
                          validator=PlanValidator(allowed_tools=tool_descriptors("search"),
                                                  limits=PlanLimits(max_tasks=12,
                                                                    max_tool_calls=30,
                                                                    max_replans=1)))
    engine = SwarmV2Engine(
        commander=commander,
        executor=BoundedTaskExecutor(worker_factory=lambda: Worker(calls, fail=fail_tasks),
                                     max_active_workers=3),
        verifier=Verifier(gateway=VerifyGateway(), model="fake", resolver=StubResolver()),
        builder=FinalBuilder(), evidence_loader=evidence,
        checkpoint_sink=lambda _phase, value: checkpoints.append(deepcopy(value)),
        event_sink=lambda kind, payload: events.append((kind, payload)),
        ledger_sink=ledger)
    run = {"id": RUN_ID, "input": {"objective": "o", "commander_model": "fake"}}
    return engine, run, client, calls, checkpoints, events, ledger


# =============================================================================
# 1. the replay: 11 completed tasks + one invalid replan -> partial_success
# =============================================================================

@pytest.mark.parametrize("name", sorted(INVALID_DECISIONS))
def test_invalid_replan_after_eleven_completed_tasks_keeps_the_work(name):
    engine, run, client, calls, checkpoints, events, ledger = build([INVALID_DECISIONS[name]])
    result = engine.run(run)

    # Every task ran exactly once and the Commander was asked exactly once.
    assert sorted(calls) == TASK_IDS
    assert client.replans == 1
    # Verification and finalization ran over the kept evidence.
    assert "verification_completed" in [kind for kind, _ in events]
    assert {entry["provenance"]["claim_id"] for entry in result["fields"]["answer"]} == {
        f"claim-{task_id}" for task_id in TASK_IDS}
    # Honest outcome: never "complete" because the replan was ignored.
    assert result["status"] == "partial_success"
    assert result["result_kind"] == "partial_result"
    assert result["needs_review"].count(REJECTED_ITEM) == 1
    assert validate_product_outcome(result).status == "partial_success"
    # The rejection is checkpointed and consumes the replan allowance.
    state = checkpoints[-1]["artifacts"]["swarm_state"]
    rejected = [entry for entry in state["replans"] if entry["decision"] == "REJECTED"]
    assert len(rejected) == 1
    assert rejected[0]["code"] in {"COMMANDER_DECISION_INVALID", "COMMANDER_PLAN_JSON_INVALID",
                                   "COMMANDER_PLAN_SCHEMA_INVALID",
                                   "COMMANDER_PLAN_LIMIT_EXCEEDED"}
    assert set(rejected[0]) == {"decision", "code", "graph_revision"}
    assert len(state["replans"]) >= eleven_task_plan()["max_replans"]
    assert ledger.kinds.count("replan") == 1
    assert ledger.kinds.count("task_completed") == 11
    # No provider material reaches the checkpoint.
    assert "confidential_note" not in str(state) and "x" * 600 not in str(state)
    assert "I believe" not in str(state)


@pytest.mark.parametrize("name, code", [
    ("extra_top_level_key", "COMMANDER_DECISION_INVALID"),
    ("non_json_text", "COMMANDER_DECISION_INVALID"),
    ("add_tasks_over_plan_limits", "COMMANDER_PLAN_LIMIT_EXCEEDED"),
])
def test_the_static_refusal_code_is_what_the_replay_records(name, code):
    engine, run, *_rest, checkpoints, _events, _ledger = build([INVALID_DECISIONS[name]])
    engine.run(run)
    entry = checkpoints[-1]["artifacts"]["swarm_state"]["replans"][0]
    assert entry == {"decision": "REJECTED", "code": code, "graph_revision": 1}


def test_a_valid_finish_is_still_complete():
    engine, run, client, *_ = build([{"decision": "FINISH", "plan": None, "reason": "done"}])
    result = engine.run(run)
    assert result["status"] == "complete"
    assert REJECTED_ITEM not in result["needs_review"]
    assert client.replans == 1


# =============================================================================
# 2. resume: the REJECTED entry is in the checkpoint; the Commander is not asked
# =============================================================================

def test_a_resumed_run_does_not_ask_the_commander_again_for_the_rejected_pass():
    engine, run, client, _calls, checkpoints, _events, _ledger = build(
        [INVALID_DECISIONS["non_json_text"]])
    first = engine.run(run)
    rejection = next(item for item in checkpoints
                     if item["artifacts"]["swarm_state"]["replans"])
    assert not rejection["artifacts"]["swarm_state"]["verifier_state"]
    SwarmState.resume(rejection["artifacts"]["swarm_state"], run_id=RUN_ID)

    # A Commander that would FAIL the run if it were asked anything.
    resumed, _run, client2, calls2, _cp, _ev, ledger2 = build([], initial=eleven_task_plan())
    client2._final = None
    again = resumed.run({**run, "checkpoint": rejection})
    assert client2.replans == 0
    assert calls2 == []
    assert ledger2.kinds.count("replan") == 0
    assert again["status"] == "partial_success"
    assert again["needs_review"].count(REJECTED_ITEM) == 1
    assert again["fields"] == first["fields"]


# =============================================================================
# 3. nothing usable -> the run fails exactly as it did before PR-X
# =============================================================================

@pytest.mark.parametrize("name", sorted(INVALID_DECISIONS))
def test_invalid_replan_with_zero_completed_tasks_fails_as_today(name):
    engine, run, client, calls, checkpoints, _events, ledger = build(
        [INVALID_DECISIONS[name]], fail_tasks=True)
    with pytest.raises(CommanderPlanFailure) as failure:
        engine.run(run)
    assert failure.value.code in {"COMMANDER_DECISION_INVALID", "COMMANDER_PLAN_JSON_INVALID",
                                  "COMMANDER_PLAN_SCHEMA_INVALID",
                                  "COMMANDER_PLAN_LIMIT_EXCEEDED"}
    assert client.replans == 1
    assert "replan" not in ledger.kinds
    assert all(not item["artifacts"]["swarm_state"]["replans"] for item in checkpoints)


# =============================================================================
# 4. completion / transport / budget / cancellation during replan: unchanged
# =============================================================================

class Raising(Plans):
    def __init__(self, initial, error):
        super().__init__(initial, [])
        self._error = error

    def create_replan(self, **kwargs):
        self.replans += 1
        raise self._error


@pytest.mark.parametrize("error, expected", [
    (RuntimeError("provider said something"), CommanderPlanFailure),
    (CommanderPlanFailure("COMMANDER_COMPLETION_FAILED"), CommanderPlanFailure),
    (CommanderPlanFailure("COMMANDER_COMPLETION_SHAPE_INVALID"), CommanderPlanFailure),
    (ProviderTransportFailure(PROVIDER_STREAM_INTERRUPTED, role="commander:replanning"),
     ProviderTransportFailure),
    (BudgetExceeded("BUDGET_EXCEEDED", "budget", "budget_exceeded", "failed"), BudgetExceeded),
    (CancellationRequested("RUN_CANCELLED"), CancellationRequested),
])
def test_non_decision_failures_during_replan_keep_todays_behaviour(error, expected):
    engine, run, _client, calls, checkpoints, _events, ledger = build([])
    raising = Raising(eleven_task_plan(), error)
    engine._commander._client = raising
    with pytest.raises(expected) as failure:
        engine.run(run)
    if expected is CommanderPlanFailure:
        assert failure.value.code in {"COMMANDER_COMPLETION_FAILED",
                                      "COMMANDER_COMPLETION_SHAPE_INVALID"}
    assert sorted(calls) == TASK_IDS
    assert raising.replans == 1
    assert "replan" not in ledger.kinds
    assert all(not item["artifacts"]["swarm_state"]["replans"] for item in checkpoints)


# =============================================================================
# 5. the correction-round decision is covered the same way
# =============================================================================

def test_an_invalid_correction_round_decision_finalizes_with_the_evidence():
    """Two claims contradict each other, so final verification finds an issue
    and offers the ONE correction round; its decision is invalid."""
    initial = eleven_task_plan(max_replans=2)
    client = Plans(initial, [{"decision": "REQUEST_VERIFICATION", "plan": None,
                              "reason": "verify"},
                             INVALID_DECISIONS["reason_600_chars"]], final=None)
    calls, checkpoints, ledger = [], [], Ledger()

    def conflicting(results):
        refs = evidence(results)
        return [ref.model_copy(update={"entity": "shared"}) if ref.task_id in {"t01", "t02"}
                else ref for ref in refs]

    engine = SwarmV2Engine(
        commander=Commander(client=client,
                            resolver=CommanderModelResolver(("fake",), {"fake"}),
                            validator=PlanValidator(allowed_tools=tool_descriptors("search"),
                                                    limits=PlanLimits(max_tasks=12,
                                                                      max_tool_calls=30))),
        executor=BoundedTaskExecutor(worker_factory=lambda: Worker(calls), max_active_workers=3),
        verifier=Verifier(gateway=VerifyGateway(), model="fake", resolver=StubResolver()),
        evidence_loader=conflicting, ledger_sink=ledger,
        checkpoint_sink=lambda _phase, value: checkpoints.append(deepcopy(value)))
    run = {"id": RUN_ID, "input": {"objective": "o", "commander_model": "fake"}}
    result = engine.run(run)
    assert client.replans == 2
    assert result["status"] == "partial_success"
    assert result["needs_review"].count(REJECTED_ITEM) == 1
    state = checkpoints[-1]["artifacts"]["swarm_state"]
    assert state["replans"] == [{"decision": "REJECTED", "code": "COMMANDER_DECISION_INVALID",
                                 "graph_revision": 1, "correction_round": 1}]
    assert state["correction_rounds"] == 0
    assert ledger.kinds.count("replan") == 1

    # Resuming from the final checkpoint asks the Commander nothing at all.
    silent = Plans(initial, [], final=None)
    engine._commander._client = silent
    again = engine.run({**run, "checkpoint": checkpoints[-1]})
    assert silent.replans == 0
    assert again["needs_review"].count(REJECTED_ITEM) == 1

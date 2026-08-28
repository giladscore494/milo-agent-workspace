"""Resume must never hand back budget a run already durably spent.

A run records its cumulative usage in two durable places that advance at
different rates:

  * ``runs.usage`` is rewritten after EVERY settled provider call
    (BudgetTracker.settle_call -> usage_recorder -> update_run_usage);
  * a Swarm checkpoint is written only at task and verifier-batch boundaries.

A crash between the two therefore leaves the checkpoint STALER than the run
row, and restoring the checkpoint alone would refund the model calls, tokens
and cost recorded in between. These regressions pin the crash window shut and
prove the restored value is the one B5's feasibility gate actually spends.

Everything here is offline: the provider client is a process-local fake and
the repository is in-memory. No network, no paid call.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

import backend.worker.main as worker_main
from backend.budget import BudgetConfig, BudgetTracker, merge_usage_snapshots
from backend.testing.memory_repository import MemoryRepository
from test_swarm_v2_smoke_offline import (USER, PROJECT, FakeKimiCompletions, minimal_plan,
                                         patch_client, swarm_env)

WORKER = "worker-resume-1"


def seeded_run(repo: MemoryRepository) -> str:
    """A swarm_v2 run mid-flight: claimed once, then abandoned by a crash."""
    conversation = repo.create_conversation(UUID(PROJECT), "resume", UUID(USER))
    created = repo.create_message_and_run(
        UUID(conversation["id"]), "resume budget regression", {}, UUID(USER),
        str(uuid4()), "fingerprint")
    run_id = str(created["run"]["id"])
    repo.runs[run_id]["workflow_key"] = "swarm_v2"
    repo.set_launch_state(UUID(run_id), "launched")
    return run_id


def stale_checkpoint(repo: MemoryRepository, run_id: str, model_calls: int) -> None:
    """A durable checkpoint whose usage snapshot is one call behind."""
    repo.checkpoints.append({
        "run_id": run_id, "phase": "swarm_v2", "engine_version": "swarm_v2.1",
        "workflow_key": "swarm_v2", "completed_tasks": [],
        "artifacts": {"swarm_state": {
            "run_id": run_id, "objective": "resume budget regression",
            "engine_version": "swarm_v2.1", "workflow_key": "swarm_v2",
            "approved_plan": minimal_plan(), "usage_snapshot": snapshot(model_calls)}},
        "token_usage": snapshot(model_calls),
    })


def snapshot(model_calls: int, *, input_tokens: int = 0, output_tokens: int = 0,
             actual_cost: float = 0.0) -> dict:
    return {"model_calls": model_calls, "input_tokens": input_tokens,
            "output_tokens": output_tokens, "total_tokens": input_tokens + output_tokens,
            "estimated_cost": 0.0, "actual_cost": actual_cost, "retries": 0,
            "provider_backpressure_events": 0, "agent_steps": model_calls,
            "elapsed_seconds": 0.0}


def tracker_for(max_model_calls: int) -> BudgetTracker:
    """A fresh tracker with the run-level model-call ceiling under test."""
    return BudgetTracker(BudgetConfig(max_model_calls_per_run=max_model_calls,
                                      estimated_cost_per_call=0.01),
                         kill_switch=lambda: True)


def run_worker(monkeypatch, repo, run_id, tracker, completions=None):
    completions = completions or FakeKimiCompletions()
    patch_client(monkeypatch, completions)
    monkeypatch.setenv("WORKER_ID", WORKER)
    exit_code = worker_main.execute_run(UUID(run_id), repo, budget_tracker=tracker)
    return completions, exit_code


@pytest.fixture()
def offline(monkeypatch):
    swarm_env(monkeypatch)
    repo = MemoryRepository()
    repo.seed_user(USER)
    repo.seed_project(PROJECT, "resume-budget", "Resume Budget", [USER])
    repo.projects[PROJECT]["workflow_key"] = "swarm_v2"
    return repo


# --- the crash window --------------------------------------------------------

def test_resume_restores_the_newer_run_usage_not_the_stale_checkpoint(offline, monkeypatch):
    """checkpoint says 3 calls, the run row says 4: the 4th was paid for."""
    repo = offline
    run_id = seeded_run(repo)
    stale_checkpoint(repo, run_id, model_calls=3)
    repo.runs[run_id]["usage"] = snapshot(4, input_tokens=120, output_tokens=45,
                                          actual_cost=0.44)

    tracker = tracker_for(50)
    run_worker(monkeypatch, repo, run_id, tracker)

    assert tracker.model_calls >= 4          # the settled 4th call is not refunded
    assert tracker.input_tokens >= 120
    assert tracker.output_tokens >= 45
    assert tracker.actual_cost >= 0.44


def test_the_newer_usage_is_what_the_remaining_model_call_budget_is_computed_from(
        offline, monkeypatch):
    """The consequence that matters: capacity that WOULD suffice under the
    stale checkpoint is correctly refused under the newer run usage.

    The plan has two pending tasks, so the Swarm pre-flight needs 2 + 2 = 4
    remaining model calls. Against a ceiling of 7, restoring the checkpoint's
    3 would leave exactly 4 and the run would proceed; restoring the run
    row's 4 leaves 3, and the run is refused before it reaches the provider.
    """
    repo = offline
    run_id = seeded_run(repo)
    stale_checkpoint(repo, run_id, model_calls=3)
    repo.runs[run_id]["usage"] = snapshot(4)

    tracker = tracker_for(7)
    completions, _ = run_worker(monkeypatch, repo, run_id, tracker)

    assert tracker.model_calls == 4
    assert completions.calls == []           # nothing further was paid for
    run = repo.get_run(UUID(run_id))
    assert run["status"] == "failed"
    assert run["error"]["code"] == "SWARM_V2_EXECUTION_FAILED"


def test_the_same_run_proceeds_when_only_the_stale_checkpoint_usage_exists(
        offline, monkeypatch):
    """Sensitivity check for the test above: with the newer run usage absent,
    the identical setup has capacity and the run does reach the provider. The
    refusal above is therefore caused by the restored value, not by the
    fixture."""
    repo = offline
    run_id = seeded_run(repo)
    stale_checkpoint(repo, run_id, model_calls=3)
    repo.runs[run_id]["usage"] = {}          # nothing newer was ever recorded

    tracker = tracker_for(7)
    completions, _ = run_worker(monkeypatch, repo, run_id, tracker)

    assert tracker.model_calls == 3 + len(completions.calls)
    assert completions.calls, "the stale-usage run must have had capacity to spend"


def test_a_crash_before_any_checkpoint_still_restores_the_durable_run_usage(
        offline, monkeypatch):
    """There may be no checkpoint at all: a run can settle provider calls and
    die before its first checkpoint is written. The run row is still durable
    and must still be honoured."""
    repo = offline
    run_id = seeded_run(repo)
    repo.runs[run_id]["usage"] = snapshot(5, input_tokens=90, actual_cost=0.5)
    assert repo.latest_checkpoint(UUID(run_id), "swarm_v2") is None

    tracker = tracker_for(50)
    run_worker(monkeypatch, repo, run_id, tracker)

    assert tracker.model_calls >= 5
    assert tracker.actual_cost >= 0.5


def test_a_first_attempt_restores_nothing_and_starts_from_zero(offline, monkeypatch):
    """A run that never spent anything stores {} in both places; the merge is
    empty and no restore happens, so a fresh run is unaffected."""
    repo = offline
    run_id = seeded_run(repo)
    assert repo.get_run(UUID(run_id)).get("usage") in ({}, None)

    tracker = tracker_for(50)
    completions, exit_code = run_worker(monkeypatch, repo, run_id, tracker)

    assert exit_code == 0
    assert repo.get_run(UUID(run_id))["status"] == "completed"
    assert tracker.model_calls == len(completions.calls)


def test_the_worker_restores_exactly_the_merge_of_both_durable_snapshots(
        offline, monkeypatch):
    """Ties the worker's behaviour to the documented rule rather than to a
    hand-computed number."""
    repo = offline
    run_id = seeded_run(repo)
    stale_checkpoint(repo, run_id, model_calls=2)
    repo.runs[run_id]["usage"] = snapshot(7, input_tokens=33, output_tokens=11,
                                          actual_cost=0.7)
    expected = merge_usage_snapshots(repo.runs[run_id]["usage"], snapshot(2))

    tracker = tracker_for(50)
    run_worker(monkeypatch, repo, run_id, tracker)

    for name in ("model_calls", "input_tokens", "output_tokens", "retries",
                 "provider_backpressure_events"):
        assert getattr(tracker, name) >= expected[name]
    assert tracker.model_calls >= expected["model_calls"] == 7

"""The ExecutionUsageLedger: per-run usage is durable, monotonic and resume-safe.

Core invariant, stated over every cumulative dimension the ledger records:

    remaining_budget_after_resume <= remaining_budget_before_crash

A run may be executed by several worker processes over its life -- a crash
before or after a checkpoint, a Cloud Run retry, a lease reclaim by a
replacement worker, a V1 replay from phase state, a V2 resume through a
replan or a correction round -- and none of them may hand it back capacity it
already spent. These regressions pin every one of those paths shut.

Everything here is offline: the provider client is a process-local fake, the
repository is in-memory (`MemoryRepository` mirrors the guarded RPCs of
migration 20260920000100). No network, no paid call. The same invariants are
executed against real PostgreSQL in `tests/test_migrations_postgres.py`.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

import backend.worker.main as worker_main
from backend.budget import (BudgetConfig, BudgetExceeded, BudgetTracker,
                            build_guarded_client_factory)
from backend.errors import AppError
from backend.execution_usage import (LEDGER_AMOUNTS, LEDGER_COUNTERS, LEDGER_SNAPSHOT_FIELDS,
                                     PUBLIC_USAGE_FIELDS, merge_usage_snapshots,
                                     public_usage_projection, remaining_capacity,
                                     usage_is_not_below)
from backend.testing.memory_repository import MemoryRepository
from backend.worker.main import _is_definitive_lease_loss, execute_run
from test_swarm_v2_resume_budget import seeded_run, snapshot, stale_checkpoint
from test_swarm_v2_smoke_offline import (PROJECT, USER, FakeKimiCompletions, fake_kimi_client,
                                         kimi_response, minimal_plan, swarm_env)
from test_worker import WorkerRepo
from tests.run_factory import identity_kwargs


class Crash(BaseException):
    """A process death: not an Exception, so no handler can turn it into a
    handled run failure. Everything the worker made durable before it stays."""


def tracker_for(max_model_calls: int = 50, **limits) -> BudgetTracker:
    return BudgetTracker(BudgetConfig(max_model_calls_per_run=max_model_calls,
                                      estimated_cost_per_call=0.01, **limits),
                         kill_switch=lambda: True)


def expire_lease(repo: MemoryRepository, run_id: str) -> None:
    repo.runs[run_id]["lease_expires_at"] = "2000-01-01T00:00:00+00:00"


def patch_client(monkeypatch, completions) -> None:
    monkeypatch.setattr(
        worker_main, "build_guarded_client_factory",
        lambda tracker, **_kw: build_guarded_client_factory(
            tracker, inner_factory=lambda api_key, base_url: fake_kimi_client(completions)))


def run_worker(monkeypatch, repo, run_id, tracker, completions, worker="worker-ledger-1"):
    patch_client(monkeypatch, completions)
    monkeypatch.setenv("WORKER_ID", worker)
    return execute_run(UUID(run_id), repo, budget_tracker=tracker)


def assert_remaining_never_increases(before: dict, after: dict, config: BudgetConfig) -> None:
    """The invariant itself, on every enforceable limit the config sets."""
    remaining_before = remaining_capacity(before, config)
    remaining_after = remaining_capacity(after, config)
    for dimension, left_before in remaining_before.items():
        if left_before is None:
            continue
        assert remaining_after[dimension] <= left_before, dimension
    assert usage_is_not_below(after, before)


@pytest.fixture()
def offline(monkeypatch):
    swarm_env(monkeypatch)
    repo = MemoryRepository()
    repo.seed_user(USER)
    repo.seed_project(PROJECT, "usage-ledger", "Usage Ledger", [USER])
    repo.projects[PROJECT]["workflow_key"] = "swarm_v2"
    return repo


# --- 1. the ledger contract ------------------------------------------------

def full_ledger(**overrides) -> dict:
    ledger = {name: 0 for name in LEDGER_COUNTERS}
    ledger.update({name: 0.0 for name in LEDGER_AMOUNTS})
    ledger.update(overrides)
    ledger["total_tokens"] = ledger["input_tokens"] + ledger["output_tokens"]
    return ledger


def test_every_ledger_dimension_is_covered_by_the_monotonic_merge():
    """Each dimension is maximised independently, whichever record is ahead."""
    left = full_ledger(**{name: 5 for name in LEDGER_COUNTERS if name != "input_tokens"},
                       input_tokens=1, actual_cost=0.5)
    right = full_ledger(**{name: 2 for name in LEDGER_COUNTERS if name != "input_tokens"},
                        input_tokens=9, actual_cost=0.1, search_cost=0.7, elapsed_seconds=3.0)
    merged = merge_usage_snapshots(left, right)
    for name in LEDGER_COUNTERS:
        assert merged[name] == max(left[name], right[name]), name
    for name in LEDGER_AMOUNTS:
        assert merged[name] == pytest.approx(max(left[name], right[name])), name
    assert merged["total_tokens"] == merged["input_tokens"] + merged["output_tokens"]
    assert merge_usage_snapshots(right, left) == merged   # order-independent
    assert merge_usage_snapshots(merged, merged) == merged  # idempotent
    assert usage_is_not_below(merged, left) and usage_is_not_below(merged, right)


def test_the_public_projection_is_a_strict_subset_and_carries_no_ledger_only_field():
    ledger = full_ledger(model_calls=3, provider_attempts=4, tool_calls=2, replans=1,
                         ledger_version=7, schema_version=1)
    public = public_usage_projection(ledger)
    assert set(public) == PUBLIC_USAGE_FIELDS
    assert "tool_calls" not in public and "ledger_version" not in public
    assert PUBLIC_USAGE_FIELDS < LEDGER_SNAPSHOT_FIELDS


def test_remaining_capacity_is_computed_from_the_ledger_alone_and_never_negative():
    config = BudgetConfig(max_model_calls_per_run=10, max_total_tokens_per_run=100,
                          max_retries=2, max_cost_per_run=1.0)
    remaining = remaining_capacity(full_ledger(model_calls=4, input_tokens=70,
                                               output_tokens=50, retries=3,
                                               actual_cost=0.25), config)
    assert remaining["model_calls"] == 6
    assert remaining["total_tokens"] == 0          # overshoot is zero, not a debt
    assert remaining["retries"] == 0
    assert remaining["actual_cost"] == pytest.approx(0.75)
    assert remaining["agent_steps"] is None        # no limit configured


@pytest.mark.parametrize("corrupt", [
    {"tool_calls": -1}, {"replans": True}, {"search_cost": float("nan")},
    {"provider_attempts": "3"}, {"ledger_version": -1},
])
def test_a_corrupt_ledger_value_fails_closed_rather_than_being_refunded(corrupt):
    with pytest.raises(ValueError, match="budget snapshot"):
        merge_usage_snapshots(full_ledger(model_calls=2), corrupt)


# --- 2. the live ledger: every consumption is recorded as it happens --------

def recording_tracker(**limits):
    records: list[dict] = []

    def recorder(ledger):
        records.append(dict(ledger))
        return {"version": len(records), "ledger_version": len(records)}

    tracker = BudgetTracker(BudgetConfig(estimated_cost_per_call=0.01, **limits),
                            kill_switch=lambda: True, usage_recorder=recorder)
    return tracker, records


def test_every_consumption_is_made_durable_and_carries_the_full_ledger():
    tracker, records = recording_tracker()
    tracker.record_agent_step()
    tracker.record_retry()
    tracker.record_provider_backpressure()
    tracker.record_tool_call()
    tracker.record_task_result("completed")
    tracker.record_task_result("failed")
    tracker.record_task_result("blocked")           # never ran: not a consumption
    tracker.record_search(0.02)
    tracker.record_replan()
    tracker.record_replan(correction=True)
    tracker.before_call()                            # the admission is recorded ...
    tracker.after_call(input_tokens=10, output_tokens=4, cost=0.003)   # ... and the settlement
    assert len(records) == 11
    final = records[-1]
    assert set(final) == LEDGER_SNAPSHOT_FIELDS
    assert (final["agent_steps"], final["retries"], final["provider_backpressure_events"]) == (1, 1, 1)
    assert (final["tool_calls"], final["tasks_completed"], final["tasks_failed"]) == (1, 1, 1)
    assert (final["search_invocations"], final["search_cost"]) == (1, 0.02)
    assert (final["replans"], final["correction_rounds"]) == (2, 1)
    assert (final["model_calls"], final["provider_attempts"], final["provider_failures"]) == (1, 1, 0)
    assert final["total_tokens"] == 14
    # The database owns the write sequence number; the tracker carries it.
    assert tracker.ledger_version == 11
    # Records only ever advance: no record is below its predecessor.
    for earlier, later in zip(records, records[1:]):
        assert usage_is_not_below(later, earlier)


def test_a_provider_failure_stays_an_attempt_and_is_recorded_before_the_retry_is():
    """A raised provider call is settled as released: the attempt, the failure
    and the semantic retry are all durable, and none is refunded."""
    tracker, records = recording_tracker(max_retries=3)

    class Boom(Exception):
        pass

    class Completions:
        def __init__(self):
            self.calls = 0
        def create(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise Boom("provider exploded")
            return kimi_response('{"answer": "42"}')

    client = build_guarded_client_factory(
        tracker, inner_factory=lambda *_: fake_kimi_client(Completions()))("k", "u")
    with pytest.raises(Boom):
        client.chat.completions.create(model="kimi-k2.6", messages=[{"role": "user", "content": "x"}],
                                       max_tokens=16)
    client.chat.completions.create(model="kimi-k2.6", messages=[{"role": "user", "content": "x"}],
                                   max_tokens=16)
    final = tracker.ledger_snapshot()
    assert (final["model_calls"], final["provider_attempts"], final["provider_failures"]) == (2, 2, 1)
    assert final["retries"] == 1
    # admission 1, released settlement, retry, admission 2, settlement 2
    assert [r["provider_failures"] for r in records] == [0, 1, 1, 1, 1]
    assert [r["retries"] for r in records] == [0, 0, 1, 1, 1]
    assert [r["provider_attempts"] for r in records] == [1, 1, 1, 2, 2]


def test_restore_accepts_the_full_ledger_and_refuses_a_used_tracker():
    tracker = tracker_for()
    tracker.restore_snapshot(full_ledger(model_calls=2, provider_attempts=3, tool_calls=4,
                                         tasks_completed=1, replans=1, correction_rounds=1,
                                         search_invocations=2, search_cost=0.5,
                                         ledger_version=9, schema_version=1))
    assert (tracker.model_calls, tracker.provider_attempts, tracker.tool_calls) == (2, 3, 4)
    assert (tracker.replans, tracker.correction_rounds, tracker.search_cost) == (1, 1, 0.5)
    assert tracker.ledger_version == 9
    with pytest.raises(ValueError, match="fresh tracker"):
        tracker.restore_snapshot(full_ledger(model_calls=1))
    used = tracker_for()
    used.record_tool_call()
    with pytest.raises(ValueError, match="fresh tracker"):
        used.restore_snapshot(full_ledger(model_calls=1))


# --- 3. the durable record: merging, versioned, idempotent, lease-fenced ----

def claimed(repo: MemoryRepository, worker: str = "worker-A") -> tuple[str, dict]:
    run_id = seeded_run(repo)
    run = repo.claim_run(UUID(run_id), worker, lease_seconds=300)
    return run_id, {"worker_id": worker, "attempt": run["attempt"], "lease_token": run["lease_token"]}


def test_idempotent_duplicate_accounting_never_advances_the_version_or_the_counters(offline):
    run_id, lease = claimed(offline)
    ledger = full_ledger(model_calls=3, input_tokens=30, actual_cost=0.3, tool_calls=2)
    first = offline.record_run_usage(UUID(run_id), ledger, **lease)
    second = offline.record_run_usage(UUID(run_id), dict(ledger), **lease)
    third = offline.record_run_usage(UUID(run_id), dict(first["ledger"]), **lease)
    assert first["version"] == second["version"] == third["version"] == 1
    assert second["ledger"] == first["ledger"] == third["ledger"]
    assert first["ledger"]["ledger_version"] == 1
    assert offline.runs[run_id]["usage"]["model_calls"] == 3
    # A real advance moves the version exactly once.
    advanced = offline.record_run_usage(UUID(run_id), full_ledger(model_calls=4), **lease)
    assert advanced["version"] == 2 and advanced["ledger"]["model_calls"] == 4
    assert advanced["ledger"]["tool_calls"] == 2       # nothing else was refunded


def test_a_behind_the_times_record_can_never_lower_the_durable_ledger(offline):
    """Even under a VALID lease, a snapshot that is behind (an earlier thread,
    a caller that restored less than what was durable) merges monotonically."""
    run_id, lease = claimed(offline)
    offline.record_run_usage(UUID(run_id), full_ledger(model_calls=5, input_tokens=50,
                                                        actual_cost=0.5, retries=2), **lease)
    row = offline.record_run_usage(UUID(run_id), full_ledger(model_calls=2, input_tokens=10,
                                                              actual_cost=0.1, retries=0,
                                                              tool_calls=3), **lease)
    assert (row["ledger"]["model_calls"], row["ledger"]["input_tokens"]) == (5, 50)
    assert row["ledger"]["actual_cost"] == pytest.approx(0.5)
    assert row["ledger"]["retries"] == 2
    assert row["ledger"]["tool_calls"] == 3            # the one thing that advanced
    assert offline.runs[run_id]["usage"]["model_calls"] == 5
    # The legacy paths are monotonic too.
    offline.update_run_usage(UUID(run_id), snapshot(1), **lease)
    assert offline.runs[run_id]["usage"]["model_calls"] == 5
    offline.transition_run(UUID(run_id), "running", expected_worker_id=lease["worker_id"],
                           expected_attempt=lease["attempt"], expected_lease_token=lease["lease_token"],
                           usage=snapshot(1))
    assert offline.runs[run_id]["usage"]["model_calls"] == 5


def test_a_stale_worker_cannot_touch_the_ledger_after_its_lease_is_reclaimed(offline):
    run_id, lease_a = claimed(offline, "worker-A")
    offline.record_run_usage(UUID(run_id), full_ledger(model_calls=3), **lease_a)
    expire_lease(offline, run_id)
    run_b = offline.claim_run(UUID(run_id), "worker-B", lease_seconds=300)
    lease_b = {"worker_id": "worker-B", "attempt": run_b["attempt"], "lease_token": run_b["lease_token"]}
    assert lease_b["attempt"] == lease_a["attempt"] + 1
    before = offline.get_run_usage_ledger(UUID(run_id))
    for stale in (full_ledger(model_calls=99), full_ledger(model_calls=0)):
        with pytest.raises(AppError) as exc:
            offline.record_run_usage(UUID(run_id), stale, **lease_a)
        assert exc.value.code == "RUN_TRANSITION_CONFLICT"
    assert offline.get_run_usage_ledger(UUID(run_id)) == before
    assert offline.runs[run_id]["usage"]["model_calls"] == 3
    # B continues from the durable record, under its own lease.
    row = offline.record_run_usage(UUID(run_id), full_ledger(model_calls=4), **lease_b)
    assert row["ledger"]["model_calls"] == 4 and row["attempt"] == lease_b["attempt"]


# --- 4. the worker: crash windows, replacement, provider retry, V2 ----------

class CrashingCompletions(FakeKimiCompletions):
    """A provider fake that kills the process after N answered calls, and can
    raise a provider exception on a chosen call first."""

    def __init__(self, crash_after: int, *, fail_call: int | None = None, **kwargs):
        super().__init__(**kwargs)
        self.crash_after = crash_after
        self.fail_call = fail_call
        self.ledger_at_crash: dict | None = None
        self.tracker: BudgetTracker | None = None

    def create(self, **kwargs):
        if self.fail_call is not None and len(self.calls) + 1 == self.fail_call:
            self.calls.append(kwargs)
            raise RuntimeError("provider exploded")
        if len(self.calls) >= self.crash_after:
            # The ledger the process saw the instant it died; the durable
            # record is what the replacement worker must be held to.
            self.ledger_at_crash = self.tracker.ledger_snapshot() if self.tracker else None
            raise Crash()
        return super().create(**kwargs)


def crash_run(monkeypatch, repo, run_id, tracker, completions, worker="worker-A"):
    """Kill attempt 1 and return what the run is HELD TO: the durable ledger.

    Every consumption is recorded under the tracker lock before the lock is
    released, so the durable record can never be behind the dying process on
    any counter or amount -- and that is asserted here every time. Elapsed
    wall-clock is the one dimension the process necessarily reads later than
    its last write, so the invariant is stated over the durable record.
    """
    completions.tracker = tracker
    with pytest.raises(Crash):
        run_worker(monkeypatch, repo, run_id, tracker, completions, worker=worker)
    in_process = completions.ledger_at_crash
    durable = repo.get_run_usage_ledger(UUID(run_id))["ledger"]
    assert usage_is_not_below(durable, {k: v for k, v in in_process.items() if k != "elapsed_seconds"})
    return durable


def test_crash_before_any_checkpoint_resumes_from_the_durable_ledger_row(offline, monkeypatch):
    """The ledger row is written after every consumption, so a run can die
    before its first checkpoint AND before its run row caught up, and still
    be held to everything it spent."""
    repo = offline
    run_id = seeded_run(repo)
    at_crash = crash_run(monkeypatch, repo, run_id, tracker_for(), CrashingCompletions(1))
    # The answered plan call plus every task call ADMITTED before the death:
    # an admitted request is durable before it is sent, whether or not the
    # process lives to settle it.
    assert at_crash["model_calls"] >= 2
    assert repo.latest_checkpoint(UUID(run_id), "swarm_v2") is not None
    # Make the OTHER records stale on purpose: the ledger row is the only one
    # carrying the second (crashed) attempt.
    repo.runs[run_id]["usage"] = {}
    repo.checkpoints.clear()
    ledger_row = repo.get_run_usage_ledger(UUID(run_id))
    assert ledger_row["ledger"] == at_crash

    expire_lease(repo, run_id)
    resumed = tracker_for()
    run_worker(monkeypatch, repo, run_id, resumed, FakeKimiCompletions(), worker="worker-B")
    assert usage_is_not_below(resumed.ledger_snapshot(), at_crash)
    assert_remaining_never_increases(at_crash, resumed.ledger_snapshot(), resumed.config)
    assert resumed.ledger_version >= ledger_row["version"]


def test_crash_after_the_checkpoint_never_rewinds_to_the_checkpoint(offline, monkeypatch):
    """Two answered calls after the last checkpoint: the checkpoint says less,
    the ledger says more, and the resume restores the ledger."""
    repo = offline
    run_id = seeded_run(repo)
    at_crash = crash_run(monkeypatch, repo, run_id, tracker_for(), CrashingCompletions(3))
    checkpoint = repo.latest_checkpoint(UUID(run_id), "swarm_v2")
    assert checkpoint["token_usage"]["model_calls"] <= at_crash["model_calls"]
    expire_lease(repo, run_id)
    resumed = tracker_for()
    run_worker(monkeypatch, repo, run_id, resumed, FakeKimiCompletions(), worker="worker-B")
    assert resumed.model_calls >= at_crash["model_calls"]
    assert_remaining_never_increases(at_crash, resumed.ledger_snapshot(), resumed.config)


def test_a_stale_checkpoint_and_a_stale_run_row_are_both_below_the_ledger(offline, monkeypatch):
    """All three durable records exist and disagree; the resume takes the
    maximum of each dimension, so nothing any of them recorded is refunded."""
    repo = offline
    run_id = seeded_run(repo)
    stale_checkpoint(repo, run_id, model_calls=3)
    repo.runs[run_id]["usage"] = snapshot(4, input_tokens=120, output_tokens=45, actual_cost=0.44)
    run = repo.claim_run(UUID(run_id), "worker-A")
    repo.record_run_usage(UUID(run_id), full_ledger(model_calls=2, retries=5, tool_calls=6),
                          worker_id="worker-A", attempt=run["attempt"], lease_token=run["lease_token"])
    expire_lease(repo, run_id)
    resumed = tracker_for()
    run_worker(monkeypatch, repo, run_id, resumed, FakeKimiCompletions(), worker="worker-B")
    assert resumed.model_calls >= 4 and resumed.input_tokens >= 120 and resumed.actual_cost >= 0.44
    assert resumed.retries >= 5 and resumed.tool_calls >= 6


def test_worker_replacement_resumes_with_no_more_budget_than_the_crashed_worker_had(
        offline, monkeypatch):
    """The whole invariant, end to end: worker A dies mid-run, B reclaims the
    lease (attempt 2) and finishes. On every limited dimension B's remaining
    budget at resume is at most A's at the moment it died, and the durable
    record attributes the continuation to attempt 2."""
    repo = offline
    run_id = seeded_run(repo)
    config_limits = dict(max_model_calls_per_run=40, max_total_tokens_per_run=100_000,
                         max_retries=5, max_agent_steps=60)
    tracker_a = BudgetTracker(BudgetConfig(estimated_cost_per_call=0.01, **config_limits),
                              kill_switch=lambda: True)
    at_crash = crash_run(monkeypatch, repo, run_id, tracker_a, CrashingCompletions(2), worker="worker-A")
    assert repo.get_run(UUID(run_id))["status"] in {"starting", "running"}
    expire_lease(repo, run_id)

    tracker_b = BudgetTracker(BudgetConfig(estimated_cost_per_call=0.01, **config_limits),
                              kill_switch=lambda: True)
    restored: list[dict] = []
    original_restore = tracker_b.restore_snapshot
    monkeypatch.setattr(tracker_b, "restore_snapshot",
                        lambda snap: (restored.append(dict(snap)), original_restore(snap)))
    completions = FakeKimiCompletions()
    exit_code = run_worker(monkeypatch, repo, run_id, tracker_b, completions, worker="worker-B")
    assert exit_code == 0
    run = repo.get_run(UUID(run_id))
    assert run["status"] == "partial_success" and run["attempt"] == 2
    # B started from at least what A had spent, THEN paid for its own work.
    assert restored and usage_is_not_below(restored[0], at_crash)
    assert_remaining_never_increases(at_crash, restored[0], tracker_b.config)
    assert tracker_b.model_calls == restored[0]["model_calls"] + len(completions.calls)
    row = repo.get_run_usage_ledger(UUID(run_id))
    assert row["attempt"] == 2 and row["worker_id"] == "worker-B"
    assert row["ledger"] == {**tracker_b.ledger_snapshot(), "ledger_version": row["version"],
                             "elapsed_seconds": row["ledger"]["elapsed_seconds"]}
    assert run["usage"] == public_usage_projection(row["ledger"])


def test_a_provider_retry_before_the_crash_is_still_charged_after_the_resume(
        offline, monkeypatch):
    """Provider attempt, failure and semantic retry are cumulative dimensions:
    the replacement worker inherits all three."""
    repo = offline
    run_id = seeded_run(repo)
    tracker_a = tracker_for(max_retries=4)
    at_crash = crash_run(monkeypatch, repo, run_id, tracker_a,
                         CrashingCompletions(3, fail_call=2), worker="worker-A")
    assert at_crash["provider_failures"] == 1 and at_crash["retries"] == 1
    assert at_crash["provider_attempts"] == at_crash["model_calls"] >= 3
    expire_lease(repo, run_id)
    resumed = tracker_for(max_retries=4)
    run_worker(monkeypatch, repo, run_id, resumed, FakeKimiCompletions(), worker="worker-B")
    final = resumed.ledger_snapshot()
    assert final["provider_failures"] >= 1 and final["retries"] >= 1
    assert final["provider_attempts"] >= at_crash["provider_attempts"]
    assert_remaining_never_increases(at_crash, final, resumed.config)


def test_v2_replans_and_correction_rounds_are_cumulative_in_the_ledger_not_the_plan(
        offline, monkeypatch):
    """The checkpointed state bounds the PLAN; the ledger records what the run
    SPENT. A resume that carries a durable replan keeps it, and the engine
    reports each accepted replan and correction round to the ledger at the
    moment it happens -- never reconstructed from `len(state.replans)`."""
    repo = offline
    run_id = seeded_run(repo)
    stale_checkpoint(repo, run_id, model_calls=1)
    run = repo.claim_run(UUID(run_id), "worker-A")
    repo.record_run_usage(UUID(run_id), full_ledger(model_calls=1, replans=1, correction_rounds=1),
                          worker_id="worker-A", attempt=run["attempt"], lease_token=run["lease_token"])
    expire_lease(repo, run_id)
    resumed = tracker_for()
    run_worker(monkeypatch, repo, run_id, resumed, FakeKimiCompletions(), worker="worker-B")
    assert resumed.replans >= 1 and resumed.correction_rounds >= 1
    row = repo.get_run_usage_ledger(UUID(run_id))
    assert row["ledger"]["replans"] >= 1 and row["ledger"]["correction_rounds"] >= 1


def test_the_engine_reports_replans_and_task_results_to_the_ledger_before_checkpointing():
    """Engine-level: the ledger sink hears every task result and every
    accepted replan, and hears it BEFORE the checkpoint that follows."""
    from backend.engines.swarm_v2 import (BoundedTaskExecutor, RemainingBudget, SwarmV2Engine,
                                          Verifier)
    from test_swarm_v2_stage1_e2e import (Plans, StubResolver, VerifyGateway, Worker, commander,
                                          evidence)
    from test_swarm_v2 import plan, task

    class FlakyWorker(Worker):
        """Task 'b' fails on its first execution and succeeds on the second."""
        failed = False
        def execute(self, spec, dependencies):
            if spec.task_id == "b" and not FlakyWorker.failed:
                FlakyWorker.failed = True
                from backend.engines.swarm_v2 import TaskResult
                self.calls.append("b!")
                return TaskResult("b", "failed", error={"code": "TASK_FAILED", "message": "x"})
            return super().execute(spec, dependencies)

    FlakyWorker.failed = False

    initial = plan([task("a", "a"), task("b", "b")], max_replans=2)
    initial["graph"]["tasks"][1]["completion"]["allow_partial"] = True
    revised = plan([*initial["graph"]["tasks"]], max_replans=2)
    client = Plans(initial, [{"decision": "REVISE_TASK", "plan": revised, "reason": "retry b"},
                             {"decision": "FINISH", "plan": None, "reason": "done"}])
    calls: list[str] = []
    timeline: list[str] = []
    engine = SwarmV2Engine(
        commander=commander(client),
        executor=BoundedTaskExecutor(worker_factory=lambda: FlakyWorker(calls), max_active_workers=1),
        verifier=Verifier(gateway=VerifyGateway(), model="fake", resolver=StubResolver()),
        evidence_loader=evidence,
        remaining_budget=lambda: RemainingBudget(cost_units=1_000, tool_calls=30, tasks=10, model_calls=50),
        checkpoint_sink=lambda phase, cp: timeline.append("checkpoint"),
        ledger_sink=lambda kind: timeline.append(kind))
    engine.run({"id": "run-1", "input": {"objective": "ledger", "commander_model": "fake"}})
    assert timeline.count("task_completed") == 2
    assert timeline.count("task_failed") == 1
    assert timeline.count("replan") == 1
    # Consumption is reported before the checkpoint that makes it resumable.
    assert timeline.index("task_failed") < timeline.index("replan")
    assert timeline[timeline.index("replan") + 1] == "checkpoint"
    for index, entry in enumerate(timeline):
        if entry == "task_completed":
            assert timeline[index + 1] == "checkpoint"


def test_v2_remaining_tool_calls_and_tasks_come_from_the_ledger_not_the_current_plan(
        offline, monkeypatch):
    """A resumed run whose ledger already shows spent tool calls and task
    executions has that much less plan capacity -- the number the engine's
    feasibility gate is handed is ledger-derived, so a plan that would fit a
    fresh run is refused before any provider call."""
    from backend.runtime_policy import reviewed_first_run_policy

    limits = reviewed_first_run_policy().plan_limits()
    repo = offline
    run_id = seeded_run(repo)
    run = repo.claim_run(UUID(run_id), "worker-A")
    repo.record_run_usage(UUID(run_id), full_ledger(tasks_completed=limits.max_tasks - 1,
                                                     tasks_failed=1),
                          worker_id="worker-A", attempt=run["attempt"], lease_token=run["lease_token"])
    expire_lease(repo, run_id)
    resumed = tracker_for()
    completions = FakeKimiCompletions()
    run_worker(monkeypatch, repo, run_id, resumed, completions, worker="worker-B")
    assert resumed.tasks_completed + resumed.tasks_failed >= limits.max_tasks
    # Refused by the envelope pre-flight BEFORE the Commander was even asked
    # to plan: no provider call at all was paid for.
    assert completions.calls == []
    assert repo.get_run(UUID(run_id))["status"] == "failed"


def test_the_checkpoint_carries_the_consolidated_ledger_not_the_engine_local_count(
        offline, monkeypatch):
    """Whatever an engine writes as `token_usage` (V1 restarts its own token
    counters on every replay), the durable checkpoint is never below the
    ledger on any dimension."""
    repo = offline
    run_id = seeded_run(repo)
    run = repo.claim_run(UUID(run_id), "worker-A")
    repo.record_run_usage(UUID(run_id), full_ledger(model_calls=7, input_tokens=700, tool_calls=3),
                          worker_id="worker-A", attempt=run["attempt"], lease_token=run["lease_token"])
    expire_lease(repo, run_id)
    resumed = tracker_for()
    run_worker(monkeypatch, repo, run_id, resumed, FakeKimiCompletions(), worker="worker-B")
    for checkpoint in repo.checkpoints:
        usage = checkpoint["token_usage"]
        assert usage["model_calls"] >= 7 and usage["input_tokens"] >= 700 and usage["tool_calls"] >= 3
        assert set(usage) >= LEDGER_SNAPSHOT_FIELDS


# --- 5. V1: replay from phase state, charged on top of what was spent -------

class V1CrashingRepo(MemoryRepository):
    """A V1 (mock lifecycle) run whose first attempt dies while saving the
    checkpoint of its SECOND phase: one simulated model call is durable, the
    checkpoint of that phase is not the final one, so V1 must REPLAY."""

    def __init__(self):
        super().__init__()
        self.crash_on_phase: str | None = "technical"

    def save_checkpoint(self, checkpoint, worker_id=None, attempt=None, lease_token=None):
        if checkpoint.get("phase") == self.crash_on_phase:
            self.crash_on_phase = None
            raise Crash()
        return super().save_checkpoint(checkpoint, worker_id, attempt, lease_token)


@pytest.fixture()
def v1_mock(monkeypatch):
    monkeypatch.setenv("MILO_WORKER_ENGINE", "mock")
    monkeypatch.delenv("MILO_ENABLE_PAID_EXECUTION", raising=False)
    monkeypatch.setenv("MILO_MAX_MODEL_CALLS_PER_RUN", "10")
    repo = V1CrashingRepo()
    repo.seed_user(USER)
    repo.seed_project(PROJECT, "v1-replay", "V1 Replay", [USER])
    repo.projects[PROJECT]["workflow_key"] = "vehicle_catalog_v1"
    conversation = repo.create_conversation(UUID(PROJECT), "v1", UUID(USER))
    created = repo.create_message_and_run(UUID(conversation["id"]), "v1 replay regression", {},
                                          UUID(USER), str(uuid4()), "fp",
                                          **identity_kwargs(repo, conversation["id"]))
    run_id = str(created["run"]["id"])
    repo.runs[run_id]["workflow_key"] = "vehicle_catalog_v1"
    repo.set_launch_state(UUID(run_id), "launched")
    return repo, run_id


def test_v1_replay_is_charged_on_top_of_the_crashed_attempt(v1_mock, monkeypatch):
    """Attempt 1 settles two simulated calls and dies at the second checkpoint
    (only the first is a durable checkpoint, and it is not the final one).
    Attempt 2 finds no fast path, so V1 replays all three phases -- and the
    replay starts from the two calls already spent, not from zero."""
    repo, run_id = v1_mock
    monkeypatch.setenv("WORKER_ID", "worker-A")
    with pytest.raises(Crash):
        execute_run(UUID(run_id), repo)
    durable = repo.get_run_usage_ledger(UUID(run_id))["ledger"]
    assert durable["model_calls"] == 2 and durable["agent_steps"] == 2
    assert [c["phase"] for c in repo.checkpoints] == ["discovery"]
    assert repo.checkpoints[0]["token_usage"]["model_calls"] == 1   # consolidated from the ledger
    expire_lease(repo, run_id)

    monkeypatch.setenv("WORKER_ID", "worker-B")
    assert execute_run(UUID(run_id), repo) == 0
    run = repo.get_run(UUID(run_id))
    assert run["status"] == "completed" and run["attempt"] == 2
    assert run["usage"]["model_calls"] == 2 + 3
    assert run["usage"]["input_tokens"] == 5 * 1000
    final = repo.get_run_usage_ledger(UUID(run_id))["ledger"]
    assert final["model_calls"] == 5 and final["agent_steps"] == 5
    assert usage_is_not_below(final, durable)
    assert_remaining_never_increases(durable, final, BudgetConfig(max_model_calls_per_run=10))
    # The V1 output contract is unchanged: the two token counts, nothing else
    # from the ledger leaks into the run output.
    assert set(run["output"]) & LEDGER_SNAPSHOT_FIELDS <= {"input_tokens", "output_tokens"}


def test_v1_replay_cannot_spend_a_second_full_budget(v1_mock, monkeypatch):
    """With a ceiling of 4, attempt 1 spends 2. A replay that started from
    zero would fit its 3 calls; one that starts from 2 does not, and the run
    ends budget_exhausted instead of buying a fifth call."""
    repo, run_id = v1_mock
    monkeypatch.setenv("MILO_MAX_MODEL_CALLS_PER_RUN", "4")
    monkeypatch.setenv("WORKER_ID", "worker-A")
    with pytest.raises(Crash):
        execute_run(UUID(run_id), repo)
    expire_lease(repo, run_id)
    monkeypatch.setenv("WORKER_ID", "worker-B")
    assert execute_run(UUID(run_id), repo) == 0
    run = repo.get_run(UUID(run_id))
    assert run["status"] == "budget_exhausted"
    assert run["error"]["code"] == "MODEL_CALL_LIMIT_REACHED"
    assert run["usage"]["model_calls"] == 4


def test_v1_fast_path_from_the_final_checkpoint_spends_nothing_and_keeps_usage(v1_mock, monkeypatch):
    repo, run_id = v1_mock
    repo.crash_on_phase = None
    monkeypatch.setenv("WORKER_ID", "worker-A")
    assert execute_run(UUID(run_id), repo) == 0
    usage_after_first = dict(repo.get_run(UUID(run_id))["usage"])
    assert usage_after_first["model_calls"] == 3
    # Simulate a relaunch against a run that is somehow claimable again.
    repo.runs[run_id]["status"] = "queued"
    expire_lease(repo, run_id)
    monkeypatch.setenv("WORKER_ID", "worker-B")
    assert execute_run(UUID(run_id), repo) == 0
    run = repo.get_run(UUID(run_id))
    assert run["status"] == "completed"
    assert run["usage"] == usage_after_first
    assert run["output"]["input_tokens"] == 3000 and "tool_calls" not in run["output"]


# --- 6. heartbeat: transient failures do not end a resumable run ------------

@pytest.mark.parametrize("exc,definitive", [
    (AppError("RUN_LEASE_LOST", "lease held by another worker", 409), True),
    (AppError("RUN_TRANSITION_CONFLICT", "run lease is no longer held", 409), True),
    (AppError("RUN_NOT_FOUND", "run not found", 404), True),
    (AppError("REPOSITORY_ERROR", "guarded persistence operation failed", 502), False),
    (ConnectionError("reset by peer"), False),
    (TimeoutError(), False),
])
def test_only_an_ownership_answer_from_the_database_is_a_definitive_lease_loss(exc, definitive):
    assert _is_definitive_lease_loss(exc) is definitive


class HeartbeatRepo(WorkerRepo):
    def __init__(self, failure: Exception | None, *, workflow_key: str = "swarm_v2"):
        super().__init__()
        self.failure = failure
        self.workflow_key = workflow_key
        self.heartbeats = 0
    def get_project(self, project_id):
        return {"id": project_id, "workflow_key": self.workflow_key}
    def heartbeat(self, run_id, worker_id, lease_seconds=300, attempt=None, lease_token=None):
        # The worker's synchronous start-up heartbeat succeeds; the failure
        # begins mid-run, on the background thread's extensions.
        self.heartbeats += 1
        if self.failure is not None and self.heartbeats > 1:
            raise self.failure
        return super().heartbeat(run_id, worker_id, lease_seconds, attempt, lease_token)


class SlowFailingEngine:
    """Runs long enough for the heartbeat to fire, then fails."""
    workflow_key = "swarm_v2"
    def __init__(self, seconds: float):
        self.seconds = seconds
    def run(self, run):
        time.sleep(self.seconds)
        raise RuntimeError("engine failed after the heartbeat window")


def test_a_transient_heartbeat_failure_does_not_forfeit_the_lease(monkeypatch):
    """Under the old rule any heartbeat exception meant 'lease lost', and the
    engine failure below would have escaped as an unhandled crash. A
    transient failure leaves the lease in force, so the failure is handled
    under it -- and the run is left in a state a retry can resume."""
    monkeypatch.setenv("MILO_WORKER_HEARTBEAT_INTERVAL_SECONDS", "1")
    monkeypatch.setenv("MILO_WORKER_LEASE_SECONDS", "300")
    repo = HeartbeatRepo(AppError("REPOSITORY_ERROR", "guarded persistence operation failed", 502))
    assert execute_run(repo.run_id, repo, SlowFailingEngine(2.5)) == 0
    assert repo.heartbeats >= 2                       # it kept retrying, sooner than the interval
    assert repo.failed[1] == "SWARM_V2_EXECUTION_FAILED"


def test_a_definitive_lease_loss_still_fences_the_worker(monkeypatch):
    monkeypatch.setenv("MILO_WORKER_HEARTBEAT_INTERVAL_SECONDS", "1")
    repo = HeartbeatRepo(AppError("RUN_LEASE_LOST", "run lease is held by another worker", 409))
    with pytest.raises(RuntimeError, match="engine failed"):
        execute_run(repo.run_id, repo, SlowFailingEngine(2.5))
    assert repo.failed is None                        # a stale worker writes nothing


def test_a_lease_that_lapses_without_a_successful_heartbeat_is_lost(monkeypatch):
    """Transient failures are tolerated only while the last proven extension
    still holds; once it lapses the lease is treated as gone."""
    monkeypatch.setenv("MILO_WORKER_HEARTBEAT_INTERVAL_SECONDS", "1")
    monkeypatch.setenv("MILO_WORKER_LEASE_SECONDS", "2")
    repo = HeartbeatRepo(ConnectionError("reset by peer"))
    with pytest.raises(RuntimeError, match="engine failed"):
        execute_run(repo.run_id, repo, SlowFailingEngine(4.0))
    assert repo.failed is None

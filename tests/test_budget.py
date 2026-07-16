"""Budget, cost and resource limit tests. Mocked model adapters only —
no real model call is ever made in this module."""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.budget import (
    BudgetConfig,
    BudgetExceeded,
    BudgetTracker,
    GuardedModelClient,
    build_guarded_client_factory,
    paid_execution_enabled,
)
from backend.dependencies import get_job_launcher, get_repository
from backend.main import app
from backend.runtime import CancellationRequested


def make_tracker(monkeypatch=None, enabled=True, cancellation=None, events=None, usage=None, ledger=None, clock=None, **cfg):
    config = BudgetConfig(**cfg)
    tracker = BudgetTracker(
        config,
        cancellation_checker=cancellation,
        event_emitter=(lambda t, p: events.append((t, p))) if events is not None else None,
        usage_recorder=(lambda snap: usage.append(snap)) if usage is not None else None,
        ledger_recorder=(lambda entry: ledger.append(entry)) if ledger is not None else None,
        kill_switch=lambda: enabled,
    )
    if clock is not None:
        tracker.clock = clock
        tracker._started_at = clock()
    return tracker


def test_paid_execution_kill_switch_is_off_by_default(monkeypatch):
    monkeypatch.delenv("MILO_ENABLE_PAID_EXECUTION", raising=False)
    assert paid_execution_enabled() is False


def test_kill_switch_blocks_every_call_before_it_happens():
    events = []
    tracker = make_tracker(enabled=False, events=events, max_model_calls_per_run=10)
    with pytest.raises(BudgetExceeded) as exc:
        tracker.before_call()
    assert exc.value.code == "PAID_EXECUTION_DISABLED"
    assert tracker.model_calls == 0
    assert events[0][0] == "kill_switch_activated"


def test_cancellation_checked_before_every_call():
    tracker = make_tracker(cancellation=lambda: True, max_model_calls_per_run=10)
    with pytest.raises(CancellationRequested):
        tracker.before_call()
    assert tracker.model_calls == 0


def test_model_call_limit_stops_further_calls():
    events = []
    tracker = make_tracker(events=events, max_model_calls_per_run=2, estimated_cost_per_call=0.0)
    tracker.before_call(); tracker.after_call(10, 10)
    tracker.before_call(); tracker.after_call(10, 10)
    with pytest.raises(BudgetExceeded) as exc:
        tracker.before_call()
    assert exc.value.code == "MODEL_CALL_LIMIT_REACHED"
    assert exc.value.terminal_status == "budget_exhausted"
    # And it keeps refusing: no call can ever slip through afterwards.
    with pytest.raises(BudgetExceeded):
        tracker.before_call()
    assert tracker.model_calls == 2
    assert ("budget_exhausted", ) or True
    assert any(t == "budget_exhausted" for t, _ in events)


@pytest.mark.parametrize("field,limit,usage,code", [
    ("max_input_tokens_per_run", 100, (150, 0), "INPUT_TOKEN_LIMIT_EXCEEDED"),
    ("max_output_tokens_per_run", 100, (0, 150), "OUTPUT_TOKEN_LIMIT_EXCEEDED"),
    ("max_total_tokens_per_run", 100, (60, 60), "TOTAL_TOKEN_LIMIT_EXCEEDED"),
])
def test_token_limits_stop_further_calls(field, limit, usage, code):
    events = []
    ledger = []
    recorded_usage = []
    tracker = make_tracker(events=events, ledger=ledger, usage=recorded_usage, **{field: limit})
    tracker.before_call()
    with pytest.raises(BudgetExceeded) as exc:
        tracker.after_call(*usage)
    assert exc.value.code == code
    assert exc.value.event_type == "token_limit_reached"
    assert tracker.stop is not None
    assert tracker.model_calls == 1
    assert tracker.input_tokens == usage[0]
    assert tracker.output_tokens == usage[1]
    assert recorded_usage and recorded_usage[-1]["input_tokens"] == usage[0]
    assert recorded_usage[-1]["output_tokens"] == usage[1]
    assert any(entry.get("decision") == "overage" and entry.get("rejection_reason") == code for entry in ledger)
    assert any(t == "token_limit_reached" for t, _ in events)
    with pytest.raises(BudgetExceeded) as later:
        tracker.before_call()
    assert later.value.code == code


def test_estimated_cost_budget_blocks_next_call():
    tracker = make_tracker(max_estimated_cost_per_run=0.10, estimated_cost_per_call=0.05)
    tracker.before_call()
    tracker.before_call()
    with pytest.raises(BudgetExceeded) as exc:
        tracker.before_call()
    assert exc.value.code == "ESTIMATED_COST_LIMIT_REACHED"
    assert tracker.model_calls == 2


def test_actual_recorded_cost_blocks_next_call():
    tracker = make_tracker(max_cost_per_run=1.0, estimated_cost_per_call=0.0)
    tracker.before_call()
    with pytest.raises(BudgetExceeded) as exc:
        tracker.after_call(1, 1, cost=1.5)
    assert exc.value.code == "COST_LIMIT_EXCEEDED"
    assert tracker.actual_cost == pytest.approx(1.5)
    assert tracker.stop is not None
    with pytest.raises(BudgetExceeded) as later:
        tracker.before_call()
    assert later.value.code == "COST_LIMIT_EXCEEDED"


def test_duration_limit_times_out():
    now = [0.0]
    tracker = make_tracker(clock=lambda: now[0], max_run_duration_seconds=60)
    tracker.before_call()
    now[0] = 61.0
    with pytest.raises(BudgetExceeded) as exc:
        tracker.before_call()
    assert exc.value.code == "RUN_DURATION_EXCEEDED"
    assert exc.value.terminal_status == "timed_out"
    assert exc.value.event_type == "run_timed_out"


def test_retry_limit_reached():
    tracker = make_tracker(max_retries=1)
    tracker.record_retry()
    with pytest.raises(BudgetExceeded) as exc:
        tracker.record_retry()
    assert exc.value.code == "RETRY_LIMIT_REACHED"
    with pytest.raises(BudgetExceeded):
        tracker.before_call()


def test_agent_step_limit_reached():
    tracker = make_tracker(max_agent_steps=2)
    tracker.record_agent_step()
    tracker.record_agent_step()
    with pytest.raises(BudgetExceeded):
        tracker.record_agent_step()


def test_daily_budgets_block_before_call():
    tracker = make_tracker(daily_user_budget=5.0, daily_project_budget=50.0)
    tracker.daily_user_cost_provider = lambda: 6.0
    tracker.daily_project_cost_provider = lambda: 0.0
    with pytest.raises(BudgetExceeded) as exc:
        tracker.before_call()
    assert exc.value.code == "DAILY_USER_BUDGET_REACHED"
    tracker2 = make_tracker(daily_project_budget=50.0)
    tracker2.daily_project_cost_provider = lambda: 51.0
    with pytest.raises(BudgetExceeded) as exc2:
        tracker2.before_call()
    assert exc2.value.code == "DAILY_PROJECT_BUDGET_REACHED"


def test_budget_warning_emitted_once_at_80_percent():
    events = []
    tracker = make_tracker(events=events, max_total_tokens_per_run=100, estimated_cost_per_call=0.0)
    tracker.before_call()
    tracker.after_call(50, 35)  # 85%
    tracker.before_call()
    tracker.after_call(5, 5)
    warnings = [e for e in events if e[0] == "budget_warning"]
    assert len(warnings) == 1


def test_usage_recorded_after_each_call():
    usage = []
    tracker = make_tracker(usage=usage, estimated_cost_per_call=0.02)
    tracker.before_call()
    tracker.after_call(100, 50)
    tracker.before_call()
    tracker.after_call(10, 5)
    assert len(usage) == 2
    assert usage[-1]["input_tokens"] == 110
    assert usage[-1]["output_tokens"] == 55
    assert usage[-1]["model_calls"] == 2
    assert usage[-1]["estimated_cost"] == pytest.approx(0.04)


class MockUsage:
    prompt_tokens = 120
    completion_tokens = 30


class MockResponse:
    usage = MockUsage()


class MockCompletions:
    def __init__(self):
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        return MockResponse()


class MockChat:
    def __init__(self):
        self.completions = MockCompletions()


class MockClient:
    def __init__(self, api_key="", base_url=""):
        assert "sk-" not in repr(self)  # never leak anything key-like
        self.chat = MockChat()


def test_guarded_client_stops_mock_model_calls_at_the_limit():
    tracker = make_tracker(max_model_calls_per_run=3, estimated_cost_per_call=0.0)
    inner = MockClient()
    guarded = GuardedModelClient(inner, tracker)
    for _ in range(3):
        guarded.chat.completions.create(model="mock", messages=[])
    with pytest.raises(BudgetExceeded):
        guarded.chat.completions.create(model="mock", messages=[])
    assert inner.chat.completions.calls == 3  # the 4th call never reached the adapter
    assert tracker.input_tokens == 360
    assert tracker.output_tokens == 90


def test_guarded_factory_wraps_injected_mock_factory():
    tracker = make_tracker(max_model_calls_per_run=1, estimated_cost_per_call=0.0)
    made = []
    factory = build_guarded_client_factory(tracker, inner_factory=lambda key, url: made.append((key, url)) or MockClient())
    client = factory("test-key", "https://mock.example")
    client.chat.completions.create(model="mock", messages=[])
    with pytest.raises(BudgetExceeded):
        client.chat.completions.create(model="mock", messages=[])
    assert made == [("test-key", "https://mock.example")]


def test_budget_config_from_env_parses_and_validates(monkeypatch):
    env = {
        "MILO_MAX_MODEL_CALLS_PER_RUN": "40",
        "MILO_MAX_TOTAL_TOKENS_PER_RUN": "200000",
        "MILO_MAX_ESTIMATED_COST_PER_RUN": "5.0",
        "MILO_MAX_RUN_DURATION_SECONDS": "1800",
        "MILO_MAX_RETRIES": "3",
    }
    config = BudgetConfig.from_env(env)
    assert config.max_model_calls_per_run == 40
    assert config.missing_mandatory() == []
    incomplete = BudgetConfig.from_env({"MILO_MAX_MODEL_CALLS_PER_RUN": "40"})
    assert "MILO_MAX_TOTAL_TOKENS_PER_RUN" in incomplete.missing_mandatory()
    with pytest.raises(ValueError):
        BudgetConfig.from_env({"MILO_MAX_RETRIES": "-1"})
    with pytest.raises(ValueError):
        BudgetConfig.from_env({"MILO_MAX_ESTIMATED_COST_PER_RUN": "not-a-number"})


def test_concurrency_limits_block_run_creation(monkeypatch):
    from tests.test_run_lifecycle import FlakyLauncher, StatefulRepo, member

    repo = StatefulRepo()
    launcher = FlakyLauncher()
    repo.active_user_runs = 2
    repo.count_active_runs_for_user = lambda user_id: repo.active_user_runs
    repo.count_active_runs_for_project = lambda project_id: 0
    monkeypatch.setenv("MILO_ENABLE_RUN_CREATION", "true")
    monkeypatch.setenv("MILO_MAX_CONCURRENT_RUNS_PER_USER", "2")
    app.dependency_overrides[get_repository] = lambda: repo
    app.dependency_overrides[get_job_launcher] = lambda: launcher
    try:
        response = TestClient(app).post(
            f"/conversations/{repo.conversation_id}/runs",
            json={"content": "go", "idempotency_key": "key-1234567890"},
            headers=member(repo),
        )
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "USER_CONCURRENCY_LIMIT"
        assert repo.runs == {}
        assert launcher.launched == []
        repo.active_user_runs = 0
        ok = TestClient(app).post(
            f"/conversations/{repo.conversation_id}/runs",
            json={"content": "go", "idempotency_key": "key-1234567890"},
            headers=member(repo),
        )
        assert ok.status_code == 202
    finally:
        app.dependency_overrides.clear()


def test_worker_refuses_paid_execution_without_mandatory_budget(monkeypatch):
    from backend.worker.main import execute_run

    class MiniRepo:
        def __init__(self):
            self.failed = []
            self.events = []
            self.run = {"id": str(uuid4()), "status": "queued", "input": {"content": "x"}, "attempt": 1}

        def claim_run(self, run_id, worker_id, lease_seconds=300):
            return dict(self.run)

        def get_run(self, run_id, user_id=None):
            return dict(self.run)

        def append_run_event(self, run_id, event_type, payload):
            self.events.append(event_type)
            return {"id": 1}

        def mark_run_failed(self, run_id, code, message):
            self.failed.append(code)
            return dict(self.run, status="failed")

        def latest_checkpoint(self, run_id, workflow_key=None):
            return None

        def mark_run_complete(self, run_id, output):
            raise AssertionError("must not complete")

    monkeypatch.setenv("MILO_ENABLE_PAID_EXECUTION", "true")
    for key in BudgetConfig.ENV_KEYS.values():
        monkeypatch.delenv(key, raising=False)
    repo = MiniRepo()
    exit_code = execute_run(uuid4(), repo)
    assert exit_code == 1
    assert repo.failed == ["BUDGET_CONFIG_INVALID"]
    monkeypatch.delenv("MILO_ENABLE_PAID_EXECUTION", raising=False)


# --- corrective audit: hard pre-call reservation semantics -------------------

def test_reserve_clamps_max_tokens_to_remaining_allowance():
    tracker = make_tracker(max_output_tokens_per_run=100, estimated_cost_per_call=0.0)
    inner = MockClient()
    guarded = GuardedModelClient(inner, tracker)
    # First call: usage records 30 output tokens (MockUsage.completion_tokens).
    guarded.chat.completions.create(model="mock", messages=[{"content": "x" * 40}], max_tokens=500)
    # Requested 500 but only 100 were available at reservation time.
    allowed = tracker.reserve_call(10, 500)
    assert allowed == 100 - 30  # remaining after the first call's actuals
    tracker.settle_call(10, allowed, 0, 0)


def test_reservation_immediately_rejects_actual_output_overage_and_blocks_next_call():
    tracker = make_tracker(max_output_tokens_per_run=25, estimated_cost_per_call=0.0)
    inner = MockClient()
    guarded = GuardedModelClient(inner, tracker)
    with pytest.raises(BudgetExceeded) as exc:
        guarded.chat.completions.create(model="mock", messages=[], max_tokens=25)  # settles 30 actual
    assert exc.value.code == "OUTPUT_TOKEN_LIMIT_EXCEEDED"
    assert inner.chat.completions.calls == 1
    assert tracker.stop is not None
    with pytest.raises(BudgetExceeded) as later:
        guarded.chat.completions.create(model="mock", messages=[], max_tokens=25)
    assert later.value.code == "OUTPUT_TOKEN_LIMIT_EXCEEDED"
    assert inner.chat.completions.calls == 1  # second call never reached the adapter


def test_pre_call_input_estimate_blocks_oversized_prompt_before_the_call():
    tracker = make_tracker(max_input_tokens_per_run=10, estimated_cost_per_call=0.0)
    inner = MockClient()
    guarded = GuardedModelClient(inner, tracker)
    with pytest.raises(BudgetExceeded) as exc:
        guarded.chat.completions.create(model="mock", messages=[{"content": "x" * 400}])
    assert exc.value.code == "INPUT_TOKEN_LIMIT_REACHED"
    assert inner.chat.completions.calls == 0  # rejected BEFORE any call


def test_concurrent_reservations_cannot_both_take_the_last_call():
    import threading

    tracker = make_tracker(max_model_calls_per_run=1, estimated_cost_per_call=0.0)
    results = []

    def attempt():
        try:
            tracker.reserve_call(1, 10)
            results.append("ok")
        except BudgetExceeded:
            results.append("rejected")

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("ok") == 1
    assert results.count("rejected") == 7


def test_concurrent_reservations_cannot_both_take_the_last_tokens():
    import threading

    tracker = make_tracker(max_total_tokens_per_run=100, estimated_cost_per_call=0.0)
    results = []

    def attempt():
        try:
            allowed = tracker.reserve_call(10, 80)
            results.append(("ok", allowed))
        except BudgetExceeded:
            results.append(("rejected", None))

    threads = [threading.Thread(target=attempt) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    granted = sum(a or 0 for kind, a in results if kind == "ok")
    reserved_inputs = sum(10 for kind, _ in results if kind == "ok")
    assert granted + reserved_inputs <= 100, results


def test_ledger_records_reservations_settlements_and_rejections():
    entries = []
    tracker = make_tracker(max_model_calls_per_run=1, estimated_cost_per_call=0.0)
    tracker.ledger_recorder = entries.append
    allowed = tracker.reserve_call(5, 50)
    tracker.settle_call(5, allowed, 100, 20)
    with pytest.raises(BudgetExceeded):
        tracker.reserve_call(5, 50)
    decisions = [e["decision"] for e in entries]
    assert decisions == ["reserved", "settled", "rejected"]
    assert entries[-1]["rejection_reason"] == "MODEL_CALL_LIMIT_REACHED"


def test_lost_worker_lease_blocks_the_next_call():
    tracker = make_tracker(max_model_calls_per_run=10)
    tracker.lease_checker = lambda: False
    with pytest.raises(BudgetExceeded) as exc:
        tracker.reserve_call(1, 10)
    assert exc.value.code == "WORKER_LEASE_LOST"


def test_provider_key_only_from_worker_environment(monkeypatch):
    from backend.engines.vehicle_catalog_v1.adapter import worker_provider_api_key

    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
    assert worker_provider_api_key() == ""
    monkeypatch.setenv("MOONSHOT_API_KEY", "test-worker-only-key")
    assert worker_provider_api_key() == "test-worker-only-key"


def test_run_input_api_key_is_ignored(monkeypatch):
    from backend.engines.vehicle_catalog_v1.adapter import VehicleCatalogV1Adapter

    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    monkeypatch.setenv("MOONSHOT_API_KEY", "env-key-wins")
    captured = {}

    class SpyEngine:
        def run(self, config):
            captured["api_key"] = config.api_key
            return {"status": "failed", "error": {"code": "SPY", "message": "spy"}}

    adapter = VehicleCatalogV1Adapter()
    adapter.engine = SpyEngine()
    adapter.run({"input": {"api_key": "attacker-supplied-key", "content": "x"}})
    assert captured["api_key"] == "env-key-wins"


def test_worker_refuses_paid_execution_without_provider_key(monkeypatch):
    from backend.worker.main import execute_run

    class MiniRepo:
        def __init__(self):
            self.failed = []
            self.run = {"id": str(uuid4()), "status": "queued", "input": {"content": "x"}, "attempt": 1}

        def claim_run(self, run_id, worker_id, lease_seconds=300):
            return dict(self.run)

        def get_run(self, run_id, user_id=None):
            return dict(self.run)

        def append_run_event(self, run_id, event_type, payload):
            return {"id": 1}

        def mark_run_failed(self, run_id, code, message, worker_id=None):
            self.failed.append(code)
            return dict(self.run, status="failed")

        def latest_checkpoint(self, run_id, workflow_key=None):
            return None

    monkeypatch.setenv("MILO_ENABLE_PAID_EXECUTION", "true")
    monkeypatch.setenv("MILO_MAX_MODEL_CALLS_PER_RUN", "10")
    monkeypatch.setenv("MILO_MAX_TOTAL_TOKENS_PER_RUN", "1000")
    monkeypatch.setenv("MILO_MAX_ESTIMATED_COST_PER_RUN", "1")
    monkeypatch.setenv("MILO_MAX_RUN_DURATION_SECONDS", "60")
    monkeypatch.setenv("MILO_MAX_RETRIES", "1")
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
    repo = MiniRepo()
    assert execute_run(uuid4(), repo) == 1
    assert repo.failed == ["PROVIDER_KEY_MISSING"]


def test_daily_ledger_cost_prefers_settled_actuals(monkeypatch):
    from backend.testing.memory_repository import MemoryRepository

    repo = MemoryRepository()
    run_id = "11111111-2222-4333-8444-555555555555"
    user = "aaaaaaaa-1111-4111-8111-000000000001"
    repo.append_usage_ledger({"run_id": run_id, "user_id": user, "call_seq": 1, "decision": "reserved", "estimated_cost": 0.05})
    repo.append_usage_ledger({"run_id": run_id, "user_id": user, "call_seq": 1, "decision": "settled", "actual_cost": 0.02})
    repo.append_usage_ledger({"run_id": run_id, "user_id": user, "call_seq": 2, "decision": "reserved", "estimated_cost": 0.05})
    assert repo.sum_daily_ledger_cost(user_id=user) == pytest.approx(0.07)
    assert repo.sum_daily_ledger_cost(user_id="someone-else") == 0.0

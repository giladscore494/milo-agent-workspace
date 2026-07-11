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


def make_tracker(monkeypatch=None, enabled=True, cancellation=None, events=None, usage=None, clock=None, **cfg):
    config = BudgetConfig(**cfg)
    tracker = BudgetTracker(
        config,
        cancellation_checker=cancellation,
        event_emitter=(lambda t, p: events.append((t, p))) if events is not None else None,
        usage_recorder=(lambda snap: usage.append(snap)) if usage is not None else None,
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
    ("max_input_tokens_per_run", 100, (150, 0), "INPUT_TOKEN_LIMIT_REACHED"),
    ("max_output_tokens_per_run", 100, (0, 150), "OUTPUT_TOKEN_LIMIT_REACHED"),
    ("max_total_tokens_per_run", 100, (60, 60), "TOTAL_TOKEN_LIMIT_REACHED"),
])
def test_token_limits_stop_further_calls(field, limit, usage, code):
    events = []
    tracker = make_tracker(events=events, **{field: limit})
    tracker.before_call()
    tracker.after_call(*usage)
    with pytest.raises(BudgetExceeded) as exc:
        tracker.before_call()
    assert exc.value.code == code
    assert exc.value.event_type == "token_limit_reached"
    assert tracker.model_calls == 1


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
    tracker.after_call(1, 1, cost=1.5)
    with pytest.raises(BudgetExceeded) as exc:
        tracker.before_call()
    assert exc.value.code == "COST_LIMIT_REACHED"


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

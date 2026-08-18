"""Provider backpressure vs semantic retry separation across the engine
core and the budget guard. Mocked provider clients only — no real calls."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from backend.budget import BudgetConfig, BudgetTracker, GuardedModelClient
from backend.engines.vehicle_catalog_v1 import core
from backend.provider_scheduler import ProviderBackpressureExceeded, ProviderLimitsConfig, ProviderScheduler


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class FakeRateLimitError(Exception):
    def __init__(self, message="Error code: 429 - organization max RPM: 3"):
        super().__init__(message)
        self.status_code = 429


def make_response(content, finish_reason="stop", prompt_tokens=100, completion_tokens=50):
    usage = SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
    message = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(finish_reason=finish_reason, message=message)
    return SimpleNamespace(usage=usage, choices=[choice])


class ScriptedCompletions:
    """Yields scripted outcomes: exceptions raise, other values return."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else self.outcomes_exhausted()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def outcomes_exhausted(self):
        raise AssertionError("scripted provider client ran out of outcomes")


class ScriptedClient:
    def __init__(self, outcomes):
        self.chat = SimpleNamespace(completions=ScriptedCompletions(outcomes))


@pytest.fixture
def engine_globals():
    """Snapshot and restore every core injection the tests mutate."""
    saved = (
        core.MODEL_CLIENT_FACTORY, core.SLEEP_FN, core.AGENT_STEP_CALLBACK,
        core.RETRY_CALLBACK, core.PROVIDER_BACKPRESSURE_CALLBACK, core.PROVIDER_SCHEDULER,
    )
    yield core
    (core.MODEL_CLIENT_FACTORY, core.SLEEP_FN, core.AGENT_STEP_CALLBACK,
     core.RETRY_CALLBACK, core.PROVIDER_BACKPRESSURE_CALLBACK, core.PROVIDER_SCHEDULER) = saved


def install(engine_globals, outcomes, *, retries=None, backpressure=None, max_rate_limit_retries=2):
    client = ScriptedClient(outcomes)
    clock = FakeClock()

    def fake_sleep(seconds):
        clock.now += seconds

    engine_globals.MODEL_CLIENT_FACTORY = lambda api_key, base_url: client
    engine_globals.SLEEP_FN = fake_sleep
    engine_globals.AGENT_STEP_CALLBACK = None
    engine_globals.RETRY_CALLBACK = (lambda *a: retries.append(a)) if retries is not None else None
    engine_globals.PROVIDER_BACKPRESSURE_CALLBACK = (lambda *a: backpressure.append(a)) if backpressure is not None else None
    engine_globals.PROVIDER_SCHEDULER = ProviderScheduler(
        ProviderLimitsConfig(rpm_limit=None, max_rate_limit_retries=max_rate_limit_retries, max_backpressure_wait_seconds=600.0),
        sleep_fn=fake_sleep,
        clock=clock,
        rng=lambda: 0.0,
        backpressure_callback=engine_globals._notify_backpressure,
    )
    return client


DISCOVERY_JSON = json.dumps({
    "agent": "current_official_lineup_agent",
    "manufacturer": "Hyundai",
    "market": "Israel",
    "period": "2010 to June 2026",
    "models": [{"model_name_en": "Tucson", "source_url": "https://example.co.il/tucson"}],
})


def run_agent(api_key="test-key"):
    return core.run_discovery_agent(api_key, core.DISCOVERY_AGENTS[0], "Hyundai", "Israel", "2010 to June 2026")


def test_429_recovers_through_scheduler_without_consuming_semantic_retries(engine_globals):
    retries, backpressure = [], []
    # Two 429s, then a valid answer; the engine's forced-search follow-up
    # round consumes one more scripted response.
    client = install(engine_globals, [FakeRateLimitError(), FakeRateLimitError(), make_response(DISCOVERY_JSON), make_response(DISCOVERY_JSON)], retries=retries, backpressure=backpressure)
    result = run_agent()
    assert result["status"] == "success"
    assert client.chat.completions.calls == 4
    assert retries == []  # a 429 never touches the semantic retry callback
    assert [event[2] for event in backpressure] == ["provider_rate_limited", "provider_rate_limited"]
    # Accounting still runs: usage tokens from successful calls are kept.
    assert result["input_tokens"] == 200
    assert result["output_tokens"] == 100


def test_exhausted_backpressure_reports_provider_reason_not_semantic_exhaustion(engine_globals):
    retries, backpressure = [], []
    install(engine_globals, [FakeRateLimitError()] * 10, retries=retries, backpressure=backpressure, max_rate_limit_retries=2)
    result = run_agent()
    assert result["status"] == "failed"
    assert result["error"] == "API_CONCURRENCY_LIMIT"
    assert retries == []
    assert any(event[2] == "provider_rate_limited" for event in backpressure)


def test_technical_agent_backpressure_exhaustion_stays_partial(engine_globals):
    install(engine_globals, [FakeRateLimitError()] * 10, max_rate_limit_retries=1)
    result = core.run_technical_agent("test-key", core.TECHNICAL_AGENTS[0], "Hyundai", "Israel", "2010", [{"canonical_model_name": "Tucson"}])
    assert result["status"] == "partial"  # existing allow_partial contract preserved
    assert result["error"] == "API_CONCURRENCY_LIMIT"


def test_semantic_failure_still_consumes_the_retry_callback(engine_globals):
    retries, backpressure = [], []
    # Primary attempt (both rounds) returns invalid JSON → semantic retry via
    # the fallback prompt, which then succeeds.
    install(engine_globals, [make_response("this is not json"), make_response("still not json"), make_response(DISCOVERY_JSON), make_response(DISCOVERY_JSON)], retries=retries, backpressure=backpressure)
    result = run_agent()
    assert result["status"] == "success"
    assert result["used_fallback"] is True
    assert [r[2] for r in retries] == ["INVALID_JSON"]
    assert backpressure == []


def test_tool_rounds_and_retry_loop_go_through_the_shared_scheduler(engine_globals):
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "backend" / "engines" / "vehicle_catalog_v1" / "core.py").read_text(encoding="utf-8")
    # The raw provider call exists exactly once, inside the guarded path.
    assert source.count("client.chat.completions.create") == 1
    guarded = source.split("def _scheduled_provider_call", 1)[1].split("\ndef ", 1)[0]
    assert "client.chat.completions.create" in guarded
    # Both moonshot_chat rounds route through the guarded path.
    assert source.count("_scheduled_provider_call(client, kwargs, agent_name, phase_name)") == 2


# -- budget guard classification ----------------------------------------------


def make_tracker(**cfg):
    return BudgetTracker(BudgetConfig(**cfg), kill_switch=lambda: True)


class RateLimitedInner:
    def __init__(self, error):
        self.error = error
        self.chat = SimpleNamespace(completions=self)
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        raise self.error


def test_guarded_client_429_increments_backpressure_not_retries():
    tracker = make_tracker(max_retries=1, estimated_cost_per_call=0.0)
    guarded = GuardedModelClient(RateLimitedInner(FakeRateLimitError()), tracker)
    for _ in range(5):
        with pytest.raises(FakeRateLimitError):
            guarded.chat.completions.create(model="mock", messages=[])
    assert tracker.retries == 0
    assert tracker.provider_backpressure_events == 5
    assert tracker.stop is None  # never trips RETRY_LIMIT_REACHED
    assert tracker.snapshot()["provider_backpressure_events"] == 5


def test_guarded_client_non_rate_limit_error_still_consumes_retries():
    tracker = make_tracker(max_retries=10, estimated_cost_per_call=0.0)
    guarded = GuardedModelClient(RateLimitedInner(RuntimeError("connection reset")), tracker)
    with pytest.raises(RuntimeError):
        guarded.chat.completions.create(model="mock", messages=[])
    assert tracker.retries == 1
    assert tracker.provider_backpressure_events == 0


def test_guarded_client_429_releases_the_reservation():
    tracker = make_tracker(max_model_calls_per_run=10, estimated_cost_per_call=0.0)
    guarded = GuardedModelClient(RateLimitedInner(FakeRateLimitError()), tracker)
    with pytest.raises(FakeRateLimitError):
        guarded.chat.completions.create(model="mock", messages=[{"content": "x" * 40}], max_tokens=50)
    assert tracker.reserved_input_tokens == 0
    assert tracker.reserved_output_tokens == 0
    assert tracker.model_calls == 1  # the attempt still counts against call caps


def test_worker_refuses_invalid_provider_limit_configuration(monkeypatch):
    """Invalid provider limit configuration fails the run closed instead of
    degrading into unlimited capacity."""
    from uuid import uuid4
    from backend.worker.main import execute_run

    class MiniRepo:
        def __init__(self):
            self.failed = []
            self.run = {"id": str(uuid4()), "status": "queued", "input": {"content": "x"}, "attempt": 1}

        def claim_run(self, run_id, worker_id, lease_seconds=300):
            return dict(self.run)

        def get_run(self, run_id, user_id=None):
            return dict(self.run)

        def append_run_event(self, run_id, event_type, payload, worker_id=None, attempt=None, lease_token=None):
            return {"id": 1}

        def mark_run_failed(self, run_id, code, message, worker_id=None, attempt=None, lease_token=None):
            self.failed.append(code)
            return dict(self.run, status="failed")

        def latest_checkpoint(self, run_id, workflow_key=None):
            return None

        def mark_run_complete(self, run_id, output, worker_id=None, attempt=None, lease_token=None):
            raise AssertionError("must not complete")

    monkeypatch.delenv("MILO_WORKER_ENGINE", raising=False)
    monkeypatch.setenv("MILO_ENABLE_PAID_EXECUTION", "true")
    monkeypatch.setenv("MILO_MAX_MODEL_CALLS_PER_RUN", "10")
    monkeypatch.setenv("MILO_MAX_TOTAL_TOKENS_PER_RUN", "1000")
    monkeypatch.setenv("MILO_MAX_ESTIMATED_COST_PER_RUN", "1")
    monkeypatch.setenv("MILO_MAX_RUN_DURATION_SECONDS", "60")
    monkeypatch.setenv("MILO_MAX_RETRIES", "1")
    monkeypatch.setenv("MOONSHOT_API_KEY", "test-worker-only-key")
    monkeypatch.setenv("MILO_PROVIDER_RPM_LIMIT", "unlimited")
    repo = MiniRepo()
    assert execute_run(uuid4(), repo) == 1
    assert repo.failed == ["PROVIDER_LIMITS_CONFIG_INVALID"]


def test_hard_caps_still_bound_scheduler_retries_end_to_end(engine_globals):
    """Even under endless 429s, budget model-call caps stay enforced when the
    guarded client wraps the scripted provider."""
    tracker = make_tracker(max_model_calls_per_run=2, estimated_cost_per_call=0.0)
    from backend.budget import build_guarded_client_factory, BudgetExceeded

    install(engine_globals, [FakeRateLimitError()] * 10, max_rate_limit_retries=5)
    inner_client = ScriptedClient([FakeRateLimitError()] * 10)
    engine_globals.MODEL_CLIENT_FACTORY = build_guarded_client_factory(tracker, inner_factory=lambda key, url: inner_client)
    with pytest.raises(BudgetExceeded) as exc:
        run_agent()
    assert exc.value.code == "MODEL_CALL_LIMIT_REACHED"
    assert inner_client.chat.completions.calls == 2
    assert tracker.retries == 0
    assert tracker.provider_backpressure_events == 2

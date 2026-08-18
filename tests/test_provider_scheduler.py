"""Deterministic tests for the shared provider scheduler. Fake clocks and
mocked provider callables only — no real provider request is ever made."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from backend.provider_scheduler import (
    ProviderBackpressureExceeded,
    ProviderLimitsConfig,
    ProviderScheduler,
    estimate_request_tokens,
    is_provider_rate_limit_error,
    retry_after_seconds,
)
from backend.runtime import CancellationRequested


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class FakeSleeper:
    def __init__(self, clock):
        self.clock = clock
        self.sleeps = []

    def __call__(self, seconds):
        self.sleeps.append(seconds)
        self.clock.now += seconds


class FakeRateLimitError(Exception):
    def __init__(self, message="Error code: 429 - organization max RPM: 3", retry_after=None, headers=None):
        super().__init__(message)
        self.status_code = 429
        resolved_headers = headers
        if resolved_headers is None and retry_after is not None:
            resolved_headers = {"Retry-After": str(retry_after)}
        if resolved_headers is not None:
            self.response = SimpleNamespace(status_code=429, headers=resolved_headers)


def make_scheduler(clock=None, sleeper=None, cancellation=None, callback=None, rng=None, **cfg):
    clock = clock or FakeClock()
    sleeper = sleeper or FakeSleeper(clock)
    config = ProviderLimitsConfig(**cfg)
    scheduler = ProviderScheduler(
        config,
        sleep_fn=sleeper,
        clock=clock,
        rng=rng or (lambda: 0.0),
        cancellation_checker=cancellation,
        backpressure_callback=callback,
    )
    return scheduler, clock, sleeper


# -- classification -----------------------------------------------------------


def test_rate_limit_classification_matches_provider_signals():
    assert is_provider_rate_limit_error(FakeRateLimitError())
    assert is_provider_rate_limit_error(Exception("HTTP 429 too many requests"))
    assert is_provider_rate_limit_error(Exception("max organization concurrency reached"))
    assert is_provider_rate_limit_error(Exception("organization max RPM: 3"))
    assert is_provider_rate_limit_error(ProviderBackpressureExceeded("bound hit"))
    assert not is_provider_rate_limit_error(Exception("connection reset by peer"))
    assert not is_provider_rate_limit_error(ValueError("INVALID_JSON"))


def test_retry_after_parsing_valid_and_invalid():
    assert retry_after_seconds(FakeRateLimitError(retry_after=7)) == 7.0
    assert retry_after_seconds(FakeRateLimitError(retry_after="2.5")) == 2.5
    assert retry_after_seconds(FakeRateLimitError(retry_after="soon")) is None
    assert retry_after_seconds(FakeRateLimitError(retry_after="-3")) is None
    assert retry_after_seconds(FakeRateLimitError()) is None
    assert retry_after_seconds(Exception("no response attached")) is None


# -- configuration fails closed ----------------------------------------------


def test_config_defaults_stay_conservative():
    config = ProviderLimitsConfig.from_env({})
    assert config.max_concurrency == 2
    assert config.rpm_limit == 3
    assert config.tpm_limit is None
    assert config.max_rate_limit_retries == 5
    assert config.max_backpressure_wait_seconds > 0


def test_config_parses_explicit_tier_values():
    config = ProviderLimitsConfig.from_env({
        "MILO_PROVIDER_MAX_CONCURRENCY": "4",
        "MILO_PROVIDER_RPM_LIMIT": "500",
        "MILO_PROVIDER_TPM_LIMIT": "3000000",
    })
    assert (config.max_concurrency, config.rpm_limit, config.tpm_limit) == (4, 500, 3_000_000)


@pytest.mark.parametrize("key,value", [
    ("MILO_PROVIDER_MAX_CONCURRENCY", "0"),
    ("MILO_PROVIDER_MAX_CONCURRENCY", "-2"),
    ("MILO_PROVIDER_RPM_LIMIT", "unlimited"),
    ("MILO_PROVIDER_TPM_LIMIT", "-1"),
    ("MILO_PROVIDER_MAX_RATE_LIMIT_RETRIES", "many"),
    ("MILO_PROVIDER_MAX_BACKPRESSURE_WAIT_SECONDS", "0"),
])
def test_invalid_config_raises_instead_of_unlimited_capacity(key, value):
    with pytest.raises(ValueError):
        ProviderLimitsConfig.from_env({key: value})


# -- capacity scheduling ------------------------------------------------------


def test_rpm_capacity_is_respected():
    scheduler, clock, sleeper = make_scheduler(rpm_limit=2, max_backpressure_wait_seconds=600.0)
    calls = []
    for _ in range(3):
        scheduler.execute(lambda: calls.append(clock.now))
    # The first two calls run immediately; the third must wait for the
    # 60-second sliding window to free a slot.
    assert calls[0] == 0.0 and calls[1] == 0.0
    assert calls[2] >= 60.0
    assert sum(sleeper.sleeps) >= 60.0


def test_tpm_capacity_is_respected():
    scheduler, clock, _ = make_scheduler(rpm_limit=None, tpm_limit=1000, max_backpressure_wait_seconds=600.0)
    times = []
    scheduler.execute(lambda: times.append(clock.now), estimated_tokens=600)
    scheduler.execute(lambda: times.append(clock.now), estimated_tokens=600)
    assert times[0] == 0.0
    assert times[1] >= 60.0


def test_tpm_estimate_larger_than_limit_fails_closed():
    scheduler, _, _ = make_scheduler(rpm_limit=None, tpm_limit=100)
    with pytest.raises(ProviderBackpressureExceeded):
        scheduler.execute(lambda: None, estimated_tokens=500)


def test_configured_concurrency_is_respected_across_threads():
    config = ProviderLimitsConfig(max_concurrency=2, rpm_limit=None)
    scheduler = ProviderScheduler(config)
    lock = threading.Lock()
    state = {"active": 0, "max_active": 0}
    started = threading.Barrier(6)

    def call():
        with lock:
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
        threading.Event().wait(0.02)
        with lock:
            state["active"] -= 1

    def worker():
        started.wait()
        scheduler.execute(call)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert state["max_active"] <= 2


# -- 429 backpressure handling ------------------------------------------------


def test_429_enters_backpressure_and_eventually_succeeds():
    events = []
    scheduler, clock, sleeper = make_scheduler(callback=lambda *a: events.append(a), rpm_limit=None)
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise FakeRateLimitError()
        return "ok"

    assert scheduler.execute(flaky, agent="a1", phase="technical") == "ok"
    assert attempts["n"] == 3
    waits = [e for e in events if e[2] == "provider_rate_limited"]
    assert len(waits) == 2
    assert all(e[0] == "a1" and e[1] == "technical" for e in waits)
    # Exponential backoff: the second wait is longer than the first.
    assert sum(sleeper.sleeps) > 0
    assert waits[1][3] > waits[0][3]


def test_retry_after_is_honored_when_present_and_valid():
    scheduler, clock, sleeper = make_scheduler(rpm_limit=None)
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise FakeRateLimitError(retry_after=7)
        return "ok"

    assert scheduler.execute(flaky) == "ok"
    assert clock.now == pytest.approx(7.0)


def test_invalid_retry_after_falls_back_to_bounded_backoff():
    scheduler, clock, _ = make_scheduler(rpm_limit=None)
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise FakeRateLimitError(retry_after="not-a-number")
        return "ok"

    assert scheduler.execute(flaky) == "ok"
    assert clock.now == pytest.approx(2.0)  # backoff_base_seconds, zero jitter


def test_backpressure_is_bounded_never_unlimited():
    scheduler, _, _ = make_scheduler(rpm_limit=None, max_rate_limit_retries=3, max_backpressure_wait_seconds=600.0)
    attempts = {"n": 0}

    def always_429():
        attempts["n"] += 1
        raise FakeRateLimitError()

    with pytest.raises(ProviderBackpressureExceeded) as exc:
        scheduler.execute(always_429)
    assert attempts["n"] == 4  # initial + 3 bounded retries
    assert exc.value.attempts == 4
    assert is_provider_rate_limit_error(exc.value)


def test_backpressure_wait_bound_trips_before_retry_count():
    scheduler, _, _ = make_scheduler(rpm_limit=None, max_rate_limit_retries=50, max_backpressure_wait_seconds=5.0)

    def always_429():
        raise FakeRateLimitError(retry_after=10)

    with pytest.raises(ProviderBackpressureExceeded):
        scheduler.execute(always_429)


def test_backpressure_wait_is_cancellable():
    cancelled = {"flag": False}
    scheduler, _, _ = make_scheduler(cancellation=lambda: cancelled["flag"], rpm_limit=None)

    def fail_and_cancel():
        cancelled["flag"] = True
        raise FakeRateLimitError()

    with pytest.raises(CancellationRequested):
        scheduler.execute(fail_and_cancel)


def test_capacity_wait_is_cancellable():
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    cancelled = {"flag": False}

    def cancel_after_first_sleep(seconds):
        sleeper(seconds)
        cancelled["flag"] = True

    scheduler = ProviderScheduler(
        ProviderLimitsConfig(rpm_limit=1, max_backpressure_wait_seconds=600.0),
        sleep_fn=cancel_after_first_sleep,
        clock=clock,
        rng=lambda: 0.0,
        cancellation_checker=lambda: cancelled["flag"],
    )
    scheduler.execute(lambda: "first")
    with pytest.raises(CancellationRequested):
        scheduler.execute(lambda: "second")


def test_non_rate_limit_errors_propagate_unchanged():
    scheduler, _, sleeper = make_scheduler(rpm_limit=None)
    with pytest.raises(ValueError):
        scheduler.execute(lambda: (_ for _ in ()).throw(ValueError("INVALID_JSON")))
    assert sleeper.sleeps == []  # no backpressure wait for semantic errors


def test_estimate_request_tokens_counts_input_and_requested_output():
    assert estimate_request_tokens([{"content": "x" * 400}], 100) == 200
    assert estimate_request_tokens([], None) == 1

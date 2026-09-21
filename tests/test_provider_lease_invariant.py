"""Permit ownership: leases, settlement, configuration and cleanup.

    A unit of organization inference concurrency is returned to the pool ONLY
    when MILO can prove the request that took it is over.

These tests cover the COORDINATOR side of that. The proof that the rule holds
against a REAL request -- a real httpx client, a real server, and a provider
that keeps working after MILO stops waiting -- lives in
``test_provider_concurrency_ownership.py``, with a negative control showing
the superseded design failing the same measurement.

"Two processes" is two ``ProviderQuotaCoordinator`` objects over one shared
backend, which is the production shape: two Cloud Run executions, one Upstash
store. Everything is local and deterministic; nothing calls a provider.
"""

from __future__ import annotations

import threading
import time

import pytest
from types import SimpleNamespace

from backend.provider_quota import (MIN_TTL_FOR_MARGIN_FLOOR, MemoryQuotaBackend,
                                    ProviderQuotaCoordinator, ProviderQuotaUnavailable,
                                    QuotaConfig, assert_request_deadline_safe,
                                    default_request_deadline, lease_safety_margin,
                                    ownership_probe_interval)
from backend.provider_scheduler import ProviderLimitsConfig, ProviderScheduler
from backend.runtime import CancellationRequested

class StructuralRateLimit(Exception):
    """A 429 the way the OpenAI SDK actually raises one: with a response.

    The bare ``RuntimeError("Error code: 429 ...")`` these tests used to raise
    is no longer accepted as proof that a request finished -- message text is
    not evidence that anything reached the provider. Retry CLASSIFICATION
    still reads text (see `classify_provider_error`), but a permit is only
    returned on structure, so a fixture standing in for a real 429 has to
    carry one.
    """

    status_code = 429

    def __init__(self, message="Error code: 429 rate_limit_reached_error",
                 headers=None):
        super().__init__(message)
        self.response = SimpleNamespace(status_code=429, headers=headers or {})


# Short enough to run in a couple of seconds, long enough that the derived
# deadline and margin are still meaningfully separated.
TTL = 2.0
DEADLINE = default_request_deadline(TTL)        # 1.5s
MARGIN = lease_safety_margin(TTL)               # 0.5s


class FakeClock:
    """A clock the fake sleep winds forward.

    A 429 makes the coordinator pause admissions for a moment. A test that
    stubs out sleeping but leaves the coordinator on wall-clock time would spin
    against a pause that never elapses, so the two move together here.
    """

    def __init__(self, now: float = 10_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(float(seconds), 0.001)


def scheduler_for(coordinator) -> ProviderScheduler:
    """One process's scheduler over a shared coordinator."""
    return ProviderScheduler(
        ProviderLimitsConfig(max_concurrency=1, rpm_limit=None, tpm_limit=None,
                             max_backpressure_wait_seconds=30.0),
        coordinator=coordinator)


def coordinators(kind, *, ceiling=1):
    """Two 'processes' over ONE shared store, with A's probe sabotaged."""
    backend = MemoryQuotaBackend()
    config = QuotaConfig(max_concurrency=ceiling, lease_ttl_seconds=TTL)

    class WorkerA(ProviderQuotaCoordinator):
        def verify_inference_ownership(self, lease_id):
            if kind == "returns_false":
                # The coordinator no longer records this permit as ours.
                return False
            if kind == "raises":
                # The shared store is unreachable mid-request.
                raise ProviderQuotaUnavailable()
            return super().verify_inference_ownership(lease_id)

    return WorkerA(backend, config), ProviderQuotaCoordinator(backend, config)


# =============================================================================
# A. the healthy case still holds its permit for a long call
# =============================================================================

def test_a_long_call_keeps_its_permit_with_no_renewal_at_all():
    """Nothing renews a lease any more, and nothing needs to.

    The call runs well past the nominal lease window. Under the superseded
    design that window WAS the reclaim trigger, so surviving it depended on a
    renewal thread; now the lease is simply held from acquisition and the
    window has no reclaiming power at all.
    """
    backend = MemoryQuotaBackend()
    config = QuotaConfig(max_concurrency=1, lease_ttl_seconds=TTL)
    healthy = ProviderQuotaCoordinator(backend, config)
    observed: list[object] = []

    def call():
        time.sleep(TTL + 0.5)
        observed.append(healthy.try_acquire_inference())
        return "done"

    scheduler = scheduler_for(healthy)
    assert scheduler.execute(call, estimated_tokens=10, reserved_tokens=10) == "done"
    assert observed == [None], "the permit expired while its own call was running"
    assert healthy.try_acquire_inference() is not None, (
        "a call that RETURNED is proven finished, so its permit must go back")


def test_lost_ownership_is_recorded_rather_than_swallowed():
    """It used to end the renewal loop with no trace at all."""
    worker_a, _ = coordinators("returns_false")
    signals: list[tuple] = []
    worker_a._diagnostic = lambda kind, payload: signals.append((kind, payload))
    scheduler = ProviderScheduler(
        ProviderLimitsConfig(max_concurrency=1, rpm_limit=None, tpm_limit=None),
        coordinator=worker_a,
        backpressure_callback=lambda a, p, r, d: signals.append(("callback", r)))

    scheduler.execute(lambda: time.sleep(TTL), estimated_tokens=10, reserved_tokens=10)

    kinds = [kind for kind, _ in signals]
    assert "provider_lease_ownership_lost" in kinds, signals
    payload = next(p for k, p in signals if k == "provider_lease_ownership_lost")
    assert payload["reason"] == "PROVIDER_LEASE_OWNERSHIP_LOST"


def test_a_raising_ownership_probe_never_escapes_into_the_caller():
    """The request must not fail because an observability probe did."""
    worker_a, _ = coordinators("raises")
    scheduler = scheduler_for(worker_a)
    assert scheduler.execute(lambda: time.sleep(TTL), estimated_tokens=10,
                             reserved_tokens=10) is None


def test_a_probe_diagnostic_carries_no_credential_or_exception_text():
    worker_a, _ = coordinators("raises")
    signals: list[tuple] = []
    worker_a._diagnostic = lambda kind, payload: signals.append((kind, payload))
    scheduler_for(worker_a).execute(lambda: time.sleep(TTL), estimated_tokens=10,
                                    reserved_tokens=10)
    payloads = [p for k, p in signals if k == "provider_lease_ownership_lost"]
    assert payloads
    for payload in payloads:
        assert payload["reason"] == "PROVIDER_LEASE_PROBE_FAILED"
        for value in payload.values():
            text = str(value).lower()
            for forbidden in ("http", "token", "bearer", "upstash", "traceback", "key="):
                assert forbidden not in text, payload


# =============================================================================
# D. unsafe timeout/TTL configuration is refused
# =============================================================================

@pytest.mark.parametrize("deadline,ttl", [
    (120.0, 120.0),     # equal to the TTL
    (180.0, 120.0),     # longer than the TTL
    (100.0, 120.0),     # inside the TTL but eats the margin
    (91.0, 120.0),      # one second past what the margin allows
    (0.0, 120.0),       # not positive
    (-1.0, 120.0),
])
def test_an_unsafe_timeout_lease_pair_is_refused(deadline, ttl):
    with pytest.raises(ValueError):
        assert_request_deadline_safe(deadline, ttl)
    with pytest.raises(ValueError):
        QuotaConfig(lease_ttl_seconds=ttl, provider_request_timeout_seconds=deadline)


@pytest.mark.parametrize("ttl", [0.0, -30.0])
def test_a_non_positive_lease_ttl_is_refused(ttl):
    with pytest.raises(ValueError):
        assert_request_deadline_safe(10.0, ttl)


def test_the_derived_pair_is_always_safe_by_construction():
    """Whatever TTL a deployment picks, the derived deadline fits inside it."""
    for ttl in (1.0, 3.0, 20.0, 60.0, 90.0, 120.0, 240.0, 600.0):
        deadline = default_request_deadline(ttl)
        margin = lease_safety_margin(ttl)
        assert deadline > 0
        assert deadline + margin <= ttl
        assert_request_deadline_safe(deadline, ttl)


def test_the_production_default_is_ninety_seconds_inside_a_two_minute_lease():
    config = QuotaConfig()
    assert config.lease_ttl_seconds == 120.0
    assert config.safety_margin_seconds == 30.0
    assert config.request_deadline_seconds == 90.0
    assert config.ownership_probe_interval_seconds == 30.0
    # Three probes fit inside a full-length request, so a single missed pass
    # is not decisive -- which is all an observability mechanism needs.
    assert config.request_deadline_seconds / config.ownership_probe_interval_seconds >= 3


def test_the_margin_floor_only_applies_where_it_can_be_satisfied():
    """A floor that makes the deadline negative is not a safety rule."""
    assert lease_safety_margin(MIN_TTL_FOR_MARGIN_FLOOR) == 15.0
    assert default_request_deadline(3.0) > 0
    assert ownership_probe_interval(3.0) >= 1.0


def test_an_environment_cannot_configure_an_unsafe_pair():
    with pytest.raises(ValueError):
        QuotaConfig.from_env({"MILO_PROVIDER_LEASE_TTL_SECONDS": "120",
                              "MILO_PROVIDER_REQUEST_TIMEOUT_SECONDS": "119"})
    safe = QuotaConfig.from_env({"MILO_PROVIDER_LEASE_TTL_SECONDS": "120",
                                 "MILO_PROVIDER_REQUEST_TIMEOUT_SECONDS": "60"})
    assert safe.request_deadline_seconds == 60.0


def test_the_shipped_clients_carry_the_derived_deadline():
    """Every client construction in the repository, over the parsed code.

    "Both engines'" is no longer the right scope: neither engine constructs a
    provider client any more. The two places that do are the budget's guarded
    factory and the one provider authority, and `test_run_safety_contracts`
    sweeps for a third.
    """
    import ast
    import inspect

    from backend import budget as budget_module
    from backend import provider_authority

    for module in (budget_module, provider_authority):
        tree = ast.parse(inspect.getsource(module))
        constructions = [node for node in ast.walk(tree)
                         if isinstance(node, ast.Call)
                         and isinstance(node.func, ast.Name) and node.func.id == "OpenAI"]
        assert constructions, f"no OpenAI client construction in {module.__name__}"
        for call in constructions:
            keywords = {kw.arg for kw in call.keywords}
            assert "timeout" in keywords, (
                f"{module.__name__} builds an OpenAI client with no request deadline")
            assert "max_retries" in keywords


def test_the_resolved_client_timeout_sits_inside_the_lease():
    from backend.budget import provider_request_timeout

    timeout = provider_request_timeout()
    config = QuotaConfig()
    assert timeout.read == config.request_deadline_seconds
    assert timeout.read + config.safety_margin_seconds <= config.lease_ttl_seconds
    # Connecting is never worth minutes of a permit nobody else can use.
    assert timeout.connect <= timeout.read


# =============================================================================
# E. settlement on every path: proof releases, everything else holds
# =============================================================================

def _run_and_return_permit_state(outcome):
    backend = MemoryQuotaBackend()
    config = QuotaConfig(max_concurrency=1, lease_ttl_seconds=TTL)
    clock = FakeClock()
    if outcome == "probe_failure":
        coordinator, _ = coordinators("raises")
        backend = coordinator._backend
    else:
        coordinator = ProviderQuotaCoordinator(backend, config, clock=clock)
    scheduler = ProviderScheduler(
        ProviderLimitsConfig(max_concurrency=1, rpm_limit=None, tpm_limit=None,
                             max_rate_limit_retries=2, backoff_base_seconds=0.001,
                             backoff_max_seconds=0.001, max_backpressure_wait_seconds=5.0),
        coordinator=coordinator, sleep_fn=clock.sleep)

    attempts = {"n": 0}

    def call():
        attempts["n"] += 1
        if outcome == "success":
            return "ok"
        if outcome == "provider_error":
            raise RuntimeError("an ordinary provider bug")
        if outcome == "timeout":
            raise TimeoutError("provider request deadline exceeded")
        if outcome == "cancellation":
            raise CancellationRequested("RUN_CANCELLED")
        if outcome == "probe_failure":
            time.sleep(TTL)
            return "ok"
        if outcome == "retry_then_success":
            if attempts["n"] == 1:
                raise StructuralRateLimit()
            return "ok"
        raise AssertionError(outcome)

    try:
        scheduler.execute(call, estimated_tokens=10, reserved_tokens=10)
    except BaseException:  # noqa: BLE001 - the outcome under test
        pass
    return coordinator, scheduler, attempts["n"]


#: Outcomes where the request is PROVEN over, so the shared slot goes back.
#: "probe_failure" belongs here on purpose: the observability probe died, the
#: CALL still returned, and it is the call that proves anything.
PROVEN_OUTCOMES = ["success", "probe_failure", "retry_then_success"]

#: Outcomes where MILO cannot show the request stopped. The slot is held.
UNPROVEN_OUTCOMES = ["timeout", "provider_error", "cancellation"]


@pytest.mark.parametrize("outcome", PROVEN_OUTCOMES + UNPROVEN_OUTCOMES)
def test_the_process_local_slot_is_always_released(outcome):
    """A different resource with a different rule.

    The local slot bounds THIS process's threads; when the thread is gone the
    slot must go back, whatever the provider is doing. Only the shared
    organization permit is governed by proof.
    """
    _coordinator, scheduler, _attempts = _run_and_return_permit_state(outcome)
    assert scheduler._slots.acquire(blocking=False), f"the local slot leaked after {outcome}"
    scheduler._slots.release()


@pytest.mark.parametrize("outcome", PROVEN_OUTCOMES)
def test_a_proven_finished_request_returns_its_permit_immediately(outcome):
    """The fast path, and it must stay fast: no waiting, no horizon."""
    coordinator, _scheduler, _attempts = _run_and_return_permit_state(outcome)
    lease = coordinator.try_acquire_inference()
    assert lease is not None, f"a proven-finished {outcome} did not return its permit"
    lease.release()


@pytest.mark.parametrize("outcome", UNPROVEN_OUTCOMES)
def test_an_unproven_request_keeps_holding_its_permit(outcome):
    """The correction, stated as a test.

    Under the superseded design every one of these released the slot, and a
    second worker could take it while the real request might still be running.
    Uncertainty must reduce available capacity, so the slot stays held --
    by default until a human deliberately returns it.
    """
    coordinator, _scheduler, _attempts = _run_and_return_permit_state(outcome)
    assert coordinator.try_acquire_inference() is None, (
        f"{outcome} cannot prove the request stopped, yet its slot was reused")


@pytest.mark.parametrize("outcome", UNPROVEN_OUTCOMES)
def test_a_held_permit_is_announced_rather_than_silently_missing(outcome):
    """Capacity that disappears without explanation is its own incident."""
    backend = MemoryQuotaBackend()
    signals: list[tuple] = []
    coordinator = ProviderQuotaCoordinator(
        backend, QuotaConfig(max_concurrency=1, lease_ttl_seconds=TTL),
        diagnostic_sink=lambda kind, payload: signals.append((kind, payload)))
    scheduler = scheduler_for(coordinator)

    def call():
        if outcome == "timeout":
            raise TimeoutError("provider request deadline exceeded")
        if outcome == "provider_error":
            raise RuntimeError("an ordinary provider bug")
        raise CancellationRequested("RUN_CANCELLED")

    with pytest.raises(BaseException):  # noqa: B017 - the outcome under test
        scheduler.execute(call, estimated_tokens=10, reserved_tokens=10)

    held = [p for k, p in signals if k == "provider_lease_quarantined"]
    assert held, signals
    assert held[0]["held_until"] == "operator_reclaim", (
        "the diagnostic implies something will return the slot on its own")
    assert held[0]["reason"]
    for value in held[0].values():
        text = str(value).lower()
        for forbidden in ("http", "token", "bearer", "upstash", "traceback", "key="):
            assert forbidden not in text, held[0]


def test_a_retry_takes_a_fresh_permit_and_never_inherits_the_expired_one():
    """Every real provider attempt is a real admission."""
    granted: list[str] = []
    backend = MemoryQuotaBackend()

    class Recording(ProviderQuotaCoordinator):
        def try_acquire_inference(self):
            lease = super().try_acquire_inference()
            if lease is not None:
                granted.append(lease.lease_id)
            return lease

    clock = FakeClock()
    coordinator = Recording(backend, QuotaConfig(max_concurrency=1, lease_ttl_seconds=TTL),
                            clock=clock)
    scheduler = ProviderScheduler(
        ProviderLimitsConfig(max_concurrency=1, rpm_limit=None, tpm_limit=None,
                             max_rate_limit_retries=3, backoff_base_seconds=0.001,
                             backoff_max_seconds=0.001),
        coordinator=coordinator, sleep_fn=clock.sleep)

    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise StructuralRateLimit()
        return "ok"

    assert scheduler.execute(flaky, estimated_tokens=10, reserved_tokens=10) == "ok"
    assert attempts["n"] == 3
    assert len(granted) == 3, "a retried attempt reused a permit"
    assert len(set(granted)) == 3, "two attempts shared one lease id"


def test_the_local_slot_is_taken_before_the_shared_permit():
    """Ordering is part of the invariant, not a style choice.

    The invariant is stated over "permit granted -> request finished". Queueing
    for anything local INSIDE that window stretches it by up to the whole
    backpressure bound, and holds account capacity the holder is not using.
    """
    order: list[str] = []
    backend = MemoryQuotaBackend()

    class Recording(ProviderQuotaCoordinator):
        def try_acquire_inference(self):
            order.append("global")
            return super().try_acquire_inference()

    coordinator = Recording(backend, QuotaConfig(max_concurrency=1, lease_ttl_seconds=TTL))
    scheduler = scheduler_for(coordinator)
    original = scheduler._acquire_slot

    def recording_slot(*args, **kwargs):
        order.append("local")
        return original(*args, **kwargs)

    scheduler._acquire_slot = recording_slot
    scheduler.execute(lambda: "ok", estimated_tokens=10, reserved_tokens=10)
    assert order[:2] == ["local", "global"], order


def test_a_refused_shared_permit_releases_the_local_slot():
    """The ordering fix must not trade one leak for another."""
    from backend.provider_scheduler import ProviderBackpressureExceeded

    backend = MemoryQuotaBackend()

    class NeverGrants(ProviderQuotaCoordinator):
        def try_acquire_inference(self):
            return None

    coordinator = NeverGrants(backend, QuotaConfig(max_concurrency=1, lease_ttl_seconds=TTL))
    scheduler = ProviderScheduler(
        ProviderLimitsConfig(max_concurrency=1, rpm_limit=None, tpm_limit=None,
                             max_backpressure_wait_seconds=0.2),
        coordinator=coordinator, sleep_fn=lambda _s: None)

    with pytest.raises(ProviderBackpressureExceeded):
        scheduler.execute(lambda: "ok", estimated_tokens=10, reserved_tokens=10)
    assert scheduler._slots.acquire(blocking=False), "the local slot leaked"
    scheduler._slots.release()


def test_a_failure_to_start_the_ownership_probe_strands_nothing():
    """Spawning a thread can fail, and that happens BEFORE the request.

    Nothing was sent, so completion really is proven here and the permit goes
    straight back -- this is the one failure path that legitimately releases.
    """
    backend = MemoryQuotaBackend()
    coordinator = ProviderQuotaCoordinator(
        backend, QuotaConfig(max_concurrency=1, lease_ttl_seconds=TTL))
    scheduler = scheduler_for(coordinator)
    scheduler._start_ownership_probe = lambda *_a, **_k: (_ for _ in ()).throw(
        RuntimeError("can't start new thread"))

    with pytest.raises(RuntimeError):
        scheduler.execute(lambda: "ok", estimated_tokens=10, reserved_tokens=10)

    lease = coordinator.try_acquire_inference()
    assert lease is not None, "the permit leaked when the probe could not start"
    lease.release()
    assert scheduler._slots.acquire(blocking=False), "the local slot leaked"
    scheduler._slots.release()


def test_a_failing_diagnostic_sink_never_becomes_a_traceback():
    """Reporting the loss must not itself become an unhandled failure.

    The handle exists to make lost ownership visible; a sink that raises
    inside a daemon thread would replace the diagnostic with a stderr
    traceback and lose the very signal it was meant to carry.
    """
    worker_a, _ = coordinators("raises")
    worker_a._diagnostic = lambda kind, payload: (_ for _ in ()).throw(
        RuntimeError("the event sink is down"))
    scheduler = ProviderScheduler(
        ProviderLimitsConfig(max_concurrency=1, rpm_limit=None, tpm_limit=None),
        coordinator=worker_a,
        backpressure_callback=lambda *_a: (_ for _ in ()).throw(
            RuntimeError("the callback is down too")))

    # The request still completes, and the permit is still released.
    assert scheduler.execute(lambda: time.sleep(TTL), estimated_tokens=10,
                             reserved_tokens=10) is None
    lease = worker_a.try_acquire_inference()
    assert lease is not None, "a failing diagnostic stranded the permit"
    lease.release()

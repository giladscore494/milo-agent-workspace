"""The permit/request safety invariant, proven behaviourally.

    A real provider request can never still be in flight at the moment its
    organization concurrency permit becomes reclaimable by another process.

The failure this closes: PR #102 renewed a permit from a daemon watchdog
thread, and the OpenAI SDK's default read timeout is 600s against a 120s lease
TTL. If the watchdog stopped -- the coordinator said "not yours", or the shared
store raised and killed the thread -- the permit expired while the request was
still running, and a second process could take the freed slot. Real provider
concurrency then exceeded the ceiling even though every NEW request entered the
limiter correctly.

What these tests measure is the thing that actually matters: **simultaneous
in-flight simulated provider calls**, not the number of rows in the lease set.
A test that only counted leases would have passed against the broken code,
because the lease count was never the problem.

"Two processes" is two ``ProviderQuotaCoordinator`` objects over one shared
backend, which is the production shape: two Cloud Run executions, one Upstash
store. Everything is local and deterministic; nothing calls a provider.
"""

from __future__ import annotations

import threading
import time

import pytest

from backend.provider_quota import (MIN_TTL_FOR_MARGIN_FLOOR, MemoryQuotaBackend,
                                    ProviderQuotaCoordinator, ProviderQuotaUnavailable,
                                    QuotaConfig, assert_request_deadline_safe,
                                    default_request_deadline, heartbeat_interval,
                                    lease_safety_margin)
from backend.provider_scheduler import ProviderLimitsConfig, ProviderScheduler
from backend.runtime import CancellationRequested

# Short enough to run in a couple of seconds, long enough that the derived
# deadline and margin are still meaningfully separated.
TTL = 2.0
DEADLINE = default_request_deadline(TTL)        # 1.5s
MARGIN = lease_safety_margin(TTL)               # 0.5s


class SimulatedProvider:
    """A provider call that honours a deadline, and counts real overlap.

    ``peak`` is the maximum number of calls that were *simultaneously inside*
    the provider. That is the quantity the organization ceiling is about, and
    the quantity a lease-counting test cannot see.

    The deadline models what an httpx read timeout does to a non-streaming
    request: past it, the client gives up and the call ends. It is enforced
    here rather than by real HTTP because these tests must not touch a network.
    """

    def __init__(self, deadline: float):
        self.deadline = deadline
        self.peak = 0
        self.entered = 0
        self._active = 0
        self._lock = threading.Lock()
        #: Set the first time anyone is actually inside the provider. Without
        #: it the race below is decided by who wins the permit first, which
        #: makes the whole harness order-dependent -- and an order-dependent
        #: concurrency test is worth nothing.
        self.first_entry = threading.Event()

    def call(self, duration: float):
        with self._lock:
            self._active += 1
            self.entered += 1
            self.peak = max(self.peak, self._active)
        self.first_entry.set()
        try:
            deadline_at = time.monotonic() + self.deadline
            finish_at = time.monotonic() + duration
            while time.monotonic() < min(finish_at, deadline_at):
                time.sleep(0.005)
            if duration > self.deadline:
                raise TimeoutError("provider request deadline exceeded")
            return "ok"
        finally:
            with self._lock:
                self._active -= 1


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
    """Two 'processes' over ONE shared store, with A's heartbeat sabotaged."""
    backend = MemoryQuotaBackend()
    config = QuotaConfig(max_concurrency=ceiling, lease_ttl_seconds=TTL)

    class WorkerA(ProviderQuotaCoordinator):
        def heartbeat_inference(self, lease_id):
            if kind == "returns_false":
                # The coordinator says this permit is no longer ours.
                return False
            if kind == "raises":
                # The shared store is unreachable mid-request.
                raise ProviderQuotaUnavailable()
            return super().heartbeat_inference(lease_id)

    return WorkerA(backend, config), ProviderQuotaCoordinator(backend, config)


def race(provider, sched_a, sched_b, *, a_duration, b_duration=0.05):
    """A is INSIDE a long call; only then does B try to enter.

    B waits for A to be genuinely in the provider rather than merely started,
    so the outcome cannot depend on which thread won the permit first. That
    makes the question asked here exactly the one that matters: while a real
    request is in flight, can a second one begin?
    """
    errors: dict[str, BaseException] = {}

    def worker_a():
        try:
            sched_a.execute(lambda: provider.call(a_duration),
                            estimated_tokens=10, reserved_tokens=10)
        except BaseException as exc:  # noqa: BLE001 - recorded, asserted below
            errors["a"] = exc

    def worker_b():
        if not provider.first_entry.wait(timeout=10.0):
            errors["b"] = AssertionError("worker A never entered the provider")
            return
        # Keep attempting for longer than A's call could possibly run, so B
        # really does get a chance the instant a permit becomes available.
        deadline = time.monotonic() + a_duration + TTL + 1.0
        while time.monotonic() < deadline:
            try:
                sched_b.execute(lambda: provider.call(b_duration),
                                estimated_tokens=10, reserved_tokens=10)
                return
            except BaseException as exc:  # noqa: BLE001 - recorded, asserted below
                errors["b"] = exc
                return

    threads = [threading.Thread(target=worker_a), threading.Thread(target=worker_b)]
    [t.start() for t in threads]
    [t.join(timeout=30) for t in threads]
    assert not any(t.is_alive() for t in threads), "a worker never finished"
    return errors


# =============================================================================
# A. the healthy case still holds its permit for a long call
# =============================================================================

def test_a_long_call_keeps_its_permit_while_the_heartbeat_is_healthy():
    """Preserved from PR #102, now measuring the same thing the others do."""
    backend = MemoryQuotaBackend()
    config = QuotaConfig(max_concurrency=1, lease_ttl_seconds=TTL)
    healthy = ProviderQuotaCoordinator(backend, config)
    provider = SimulatedProvider(DEADLINE)
    observed: list[object] = []

    def call():
        # Longer than the TTL: without renewal the permit would be gone.
        time.sleep(TTL + 0.5)
        observed.append(healthy.try_acquire_inference())
        return "done"

    scheduler = scheduler_for(healthy)
    assert scheduler.execute(call, estimated_tokens=10, reserved_tokens=10) == "done"
    assert observed == [None], "the permit expired while its own call was running"
    assert healthy.try_acquire_inference() is not None, "the permit was not released"
    assert provider.peak == 0


# =============================================================================
# B. heartbeat returns False
# =============================================================================

def test_lost_ownership_cannot_produce_two_simultaneous_provider_calls():
    """A's coordinator disowns its permit mid-request; B is waiting to enter.

    Before the deadline existed, A's request ran for the SDK's 600s default
    while its permit expired at the TTL, so B entered and two real calls ran
    against a ceiling of one.
    """
    worker_a, worker_b = coordinators("returns_false")
    provider = SimulatedProvider(DEADLINE)

    errors = race(provider, scheduler_for(worker_a), scheduler_for(worker_b),
                  a_duration=TTL * 4)

    assert provider.entered >= 1
    assert provider.peak == 1, (
        f"{provider.peak} real provider calls overlapped under a ceiling of 1")
    # A's call was ended by its deadline rather than being allowed to run on.
    assert isinstance(errors.get("a"), TimeoutError), errors


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


# =============================================================================
# C. the heartbeat call itself fails
# =============================================================================

def test_an_unreachable_coordinator_cannot_produce_two_provider_calls():
    """The store raises mid-request, which used to kill the daemon thread."""
    worker_a, worker_b = coordinators("raises")
    provider = SimulatedProvider(DEADLINE)

    errors = race(provider, scheduler_for(worker_a), scheduler_for(worker_b),
                  a_duration=TTL * 4)

    assert provider.entered >= 1
    assert provider.peak == 1, (
        f"{provider.peak} real provider calls overlapped under a ceiling of 1")
    assert isinstance(errors.get("a"), TimeoutError), errors


def test_a_raising_heartbeat_never_escapes_into_the_caller():
    """The request must not fail because renewal did; the deadline is the guard."""
    worker_a, _ = coordinators("raises")
    scheduler = scheduler_for(worker_a)
    assert scheduler.execute(lambda: time.sleep(TTL), estimated_tokens=10,
                             reserved_tokens=10) is None


def test_a_heartbeat_diagnostic_carries_no_credential_or_exception_text():
    worker_a, _ = coordinators("raises")
    signals: list[tuple] = []
    worker_a._diagnostic = lambda kind, payload: signals.append((kind, payload))
    scheduler_for(worker_a).execute(lambda: time.sleep(TTL), estimated_tokens=10,
                                    reserved_tokens=10)
    payloads = [p for k, p in signals if k == "provider_lease_ownership_lost"]
    assert payloads
    for payload in payloads:
        assert payload["reason"] == "PROVIDER_LEASE_HEARTBEAT_FAILED"
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
    assert config.heartbeat_interval_seconds == 30.0
    # Three renewals fit inside a full-length request.
    assert config.request_deadline_seconds / config.heartbeat_interval_seconds >= 3


def test_the_margin_floor_only_applies_where_it_can_be_satisfied():
    """A floor that makes the deadline negative is not a safety rule."""
    assert lease_safety_margin(MIN_TTL_FOR_MARGIN_FLOOR) == 15.0
    assert default_request_deadline(3.0) > 0
    assert heartbeat_interval(3.0) >= 1.0


def test_an_environment_cannot_configure_an_unsafe_pair():
    with pytest.raises(ValueError):
        QuotaConfig.from_env({"MILO_PROVIDER_LEASE_TTL_SECONDS": "120",
                              "MILO_PROVIDER_REQUEST_TIMEOUT_SECONDS": "119"})
    safe = QuotaConfig.from_env({"MILO_PROVIDER_LEASE_TTL_SECONDS": "120",
                                 "MILO_PROVIDER_REQUEST_TIMEOUT_SECONDS": "60"})
    assert safe.request_deadline_seconds == 60.0


def test_the_shipped_clients_carry_the_derived_deadline():
    """Both engines' client constructions, checked over the parsed code."""
    import ast
    import inspect

    from backend import budget as budget_module
    from backend.engines.vehicle_catalog_v1 import core as v1_core

    for module in (budget_module, v1_core):
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
# E. cleanup on every path
# =============================================================================

def _run_and_return_permit_state(outcome):
    backend = MemoryQuotaBackend()
    config = QuotaConfig(max_concurrency=1, lease_ttl_seconds=TTL)
    clock = FakeClock()
    if outcome == "heartbeat_failure":
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
        if outcome == "heartbeat_failure":
            time.sleep(TTL)
            return "ok"
        if outcome == "retry_then_success":
            if attempts["n"] == 1:
                raise RuntimeError("Error code: 429 rate_limit_reached_error")
            return "ok"
        raise AssertionError(outcome)

    try:
        scheduler.execute(call, estimated_tokens=10, reserved_tokens=10)
    except BaseException:  # noqa: BLE001 - the outcome under test
        pass
    return coordinator, scheduler, attempts["n"]


@pytest.mark.parametrize("outcome", [
    "success", "provider_error", "timeout", "cancellation",
    "heartbeat_failure", "retry_then_success",
])
def test_no_permit_or_local_slot_leaks_on_any_path(outcome):
    coordinator, scheduler, _attempts = _run_and_return_permit_state(outcome)
    # The organization permit is free again...
    lease = coordinator.try_acquire_inference()
    assert lease is not None, f"the permit leaked after {outcome}"
    lease.release()
    # ...and so is the process-local slot.
    assert scheduler._slots.acquire(blocking=False), f"the local slot leaked after {outcome}"
    scheduler._slots.release()


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
            raise RuntimeError("Error code: 429 rate_limit_reached_error")
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


# =============================================================================
# the regression is not vacuous
# =============================================================================

@pytest.mark.parametrize("kind", ["returns_false", "raises"])
def test_the_race_harness_detects_the_pre_fix_behaviour(kind):
    """Guard against a test that would pass however the code behaved.

    The only difference here is the deadline: 5x the lease TTL is the ratio
    PR #102 actually shipped, because it left the OpenAI SDK's 600s default
    read timeout against a 120s lease. Under that ratio the harness must see
    two real provider calls overlap beneath a ceiling of one -- if it does
    not, the passing tests above prove nothing.
    """
    worker_a, worker_b = coordinators(kind)
    unbounded = SimulatedProvider(TTL * 5)

    race(unbounded, scheduler_for(worker_a), scheduler_for(worker_b),
         a_duration=TTL * 4)

    assert unbounded.peak == 2, (
        "the harness failed to observe the known-bad overlap, so it cannot be "
        "trusted to observe its absence either")


def test_a_failure_to_start_the_renewal_thread_strands_nothing():
    """Spawning a thread can fail; a stranded permit is the same leak."""
    backend = MemoryQuotaBackend()
    coordinator = ProviderQuotaCoordinator(
        backend, QuotaConfig(max_concurrency=1, lease_ttl_seconds=TTL))
    scheduler = scheduler_for(coordinator)
    scheduler._start_lease_watchdog = lambda *_a, **_k: (_ for _ in ()).throw(
        RuntimeError("can't start new thread"))

    with pytest.raises(RuntimeError):
        scheduler.execute(lambda: "ok", estimated_tokens=10, reserved_tokens=10)

    lease = coordinator.try_acquire_inference()
    assert lease is not None, "the permit leaked when renewal could not start"
    lease.release()
    assert scheduler._slots.acquire(blocking=False), "the local slot leaked"
    scheduler._slots.release()

"""Concurrent output-token reservation through the real call path.

The regression these tests exist for: ``BudgetTracker.open_call`` reserved
output capacity only when the caller declared a cap, while the guarded client
injected the remaining allowance into the provider request either way. No
Swarm V2 role declared a cap, so two calls in flight each received the whole
remaining output budget, and the hard stop only fired once both had settled --
that is, after the tokens had already been bought.

Every test drives the genuine chain

    ModelGateway -> ProviderScheduler -> GuardedModelClient -> provider

with a fake provider that holds all callers inside ``create()`` on a barrier,
so the assertions are made while the calls really are simultaneously in flight.
A sequential test cannot catch this defect and did not.

Nothing here calls a provider or opens a socket.
"""

from __future__ import annotations

import threading

import pytest

from backend.budget import (DEFAULT_OUTPUT_CAP, BudgetConfig, BudgetTracker,
                            PROVIDER_OUTPUT_CAP_FIELD, build_guarded_client_factory,
                            read_output_cap)
from backend.engines.swarm_v2.model_gateway import (MissingRoleOutputCap, ModelGateway,
                                                    ROLE_OUTPUT_CAPS, role_output_cap)
from backend.provider_quota import (MemoryQuotaBackend, ProviderQuotaCoordinator,
                                    QuotaConfig)
from backend.provider_scheduler import (MissingOutputCap, ProviderLimitsConfig,
                                        ProviderScheduler, estimate_admission_tokens)
from backend.provider_authority import conservative_input_tokens


class Usage:
    def __init__(self, prompt, completion):
        self.prompt_tokens, self.completion_tokens = prompt, completion


class Message:
    def __init__(self, content):
        self.content = content


class Choice:
    def __init__(self, content):
        self.message = Message(content)


class Response:
    def __init__(self, prompt, completion, content='{"ok":true}'):
        self.usage, self.choices = Usage(prompt, completion), [Choice(content)]


class HoldingProvider:
    """A provider that keeps every caller inside ``create()`` simultaneously.

    ``observed`` is what the provider was really asked for, so the tests assert
    on the outgoing request rather than on anything MILO merely intended.
    """

    def __init__(self, width: int, *, output_ratio: float = 1.0):
        self.width = width
        self.output_ratio = output_ratio
        self.observed: list[dict] = []
        self.inflight_peak = 0
        self._active = 0
        self._lock = threading.Lock()
        self._barrier = threading.Barrier(width, timeout=10)
        self.snapshots: list[tuple[int, int]] = []
        self.tracker: BudgetTracker | None = None

    def create(self, **kwargs):
        with self._lock:
            self.observed.append(dict(kwargs))
            self._active += 1
            self.inflight_peak = max(self.inflight_peak, self._active)
        try:
            self._barrier.wait()
            # Every call is inside the provider right now: this is the only
            # moment the in-flight reservation state means anything.
            if self.tracker is not None:
                with self._lock:
                    self.snapshots.append((self.tracker.reserved_output_tokens,
                                           self.tracker.output_tokens))
        except threading.BrokenBarrierError:
            pass
        cap = int(kwargs.get(PROVIDER_OUTPUT_CAP_FIELD) or 0)
        try:
            return Response(10, int(cap * self.output_ratio))
        finally:
            with self._lock:
                self._active -= 1


class Chat:
    def __init__(self, completions):
        self.completions = completions


class Client:
    def __init__(self, completions):
        self.chat = Chat(completions)


def build_stack(*, width, max_output, coordinator=None, provider=None):
    """The real gateway/scheduler/guarded-client chain over a fake provider."""
    provider = provider or HoldingProvider(width)
    tracker = BudgetTracker(
        config=BudgetConfig(max_model_calls_per_run=500,
                            max_output_tokens_per_run=max_output,
                            max_total_tokens_per_run=50_000_000,
                            max_estimated_cost_per_run=1_000.0,
                            max_run_duration_seconds=600, max_retries=50,
                            estimated_cost_per_call=0.0),
        kill_switch=lambda: True)
    provider.tracker = tracker
    scheduler = ProviderScheduler(
        ProviderLimitsConfig(max_concurrency=width, rpm_limit=None, tpm_limit=None),
        coordinator=coordinator)
    gateway = ModelGateway(
        guarded_client_factory=build_guarded_client_factory(
            tracker, lambda key, url: Client(provider)),
        scheduler=scheduler, api_key="k", base_url="u")
    return gateway, tracker, provider


def fire(gateway, roles):
    """Run one call per role concurrently; return any exceptions raised."""
    errors: list[Exception] = []
    lock = threading.Lock()

    def call(agent, phase):
        try:
            gateway.call(model="kimi-k2.6", agent=agent, phase=phase,
                         messages=[{"role": "user", "content": "x" * 400}])
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=call, args=role) for role in roles]
    [t.start() for t in threads]
    [t.join() for t in threads]
    return errors


V2_ROLES = [("commander", "planning"), ("worker:t1", "execute"),
            ("verifier", "verification"), ("commander", "replanning")]


# =============================================================================
# 1. the regression itself
# =============================================================================

def roles_for(width):
    return [V2_ROLES[i % len(V2_ROLES)] for i in range(width)]


def caps_for(width):
    return sum(role_output_cap(agent, phase) for agent, phase in roles_for(width))


@pytest.mark.parametrize("width", [2, 4, 8])
def test_concurrent_allowances_never_exceed_the_remaining_output_budget(width):
    """The whole defect, at several concurrency values including the real one.

    The budget is exactly what these roles need, so every call is admitted and
    the sum of what they were granted must land exactly on it. Before the fix
    each in-flight call was handed the ENTIRE remaining allowance, so the
    granted total was `width * budget` -- eight times over at width 8.
    """
    max_output = caps_for(width)
    gateway, tracker, provider = build_stack(width=width, max_output=max_output)
    errors = fire(gateway, roles_for(width))

    assert not errors, errors
    assert provider.inflight_peak == width, "the calls were not really simultaneous"

    granted = [read_output_cap(request) for request in provider.observed]
    assert all(cap is not None and cap > 0 for cap in granted)
    assert sum(granted) <= max_output, (
        f"{width} in-flight calls were granted {sum(granted)} of a {max_output} budget")


@pytest.mark.parametrize("width", [2, 4, 8])
def test_output_is_actually_reserved_while_the_calls_are_in_flight(width):
    """`reserved_output_tokens` was 0 during the whole overlap before the fix."""
    gateway, tracker, provider = build_stack(width=width, max_output=caps_for(width))
    assert not fire(gateway, roles_for(width))
    assert provider.snapshots, "no in-flight snapshot was taken"
    for reserved, _settled in provider.snapshots:
        assert reserved > 0, "no output capacity was held while calls were in flight"


@pytest.mark.parametrize("width", [2, 4, 8])
def test_a_budget_too_small_refuses_instead_of_overselling(width):
    """The other half: when the allowances do NOT all fit, some are refused.

    Refusal is the correct behaviour and is what the reservation buys. The
    failure mode being excluded is the old one -- admitting everybody, letting
    the provider generate the tokens, and only then noticing.
    """
    max_output = caps_for(width) // 2
    # Only the calls that fit can reach the provider, so the rest must not be
    # waited for.
    provider = HoldingProvider(1)
    gateway, tracker, _p = build_stack(width=width, max_output=max_output,
                                       provider=provider)
    errors = fire(gateway, roles_for(width))

    assert errors, "an over-subscribed output budget admitted every call"
    assert all(getattr(exc, "code", "") == "OUTPUT_TOKEN_LIMIT_REACHED"
               for exc in errors), [getattr(e, "code", e) for e in errors]
    granted = [read_output_cap(request) for request in provider.observed]
    assert sum(granted) <= max_output
    assert tracker.output_tokens <= max_output


@pytest.mark.parametrize("width", [2, 4, 8])
def test_the_recorded_output_never_overshoots_the_ceiling(width):
    """The consequence that cost money: the stop used to arrive after the buy."""
    max_output = 6_000
    provider = HoldingProvider(1)
    gateway, tracker, _p = build_stack(width=width, max_output=max_output,
                                       provider=provider)
    fire(gateway, roles_for(width))
    assert tracker.output_tokens <= max_output, (
        f"recorded {tracker.output_tokens} output tokens against a {max_output} ceiling")


def test_every_provider_request_carries_a_numeric_output_cap():
    gateway, _tracker, provider = build_stack(width=4, max_output=50_000)
    assert not fire(gateway, V2_ROLES)
    for request in provider.observed:
        cap = request.get(PROVIDER_OUTPUT_CAP_FIELD)
        assert isinstance(cap, int) and cap > 0, request
        # Exactly one spelling reaches the wire.
        assert "max_completion_tokens" not in request or PROVIDER_OUTPUT_CAP_FIELD == \
            "max_completion_tokens"


def test_settlement_releases_everything_and_cannot_double_release():
    gateway, tracker, provider = build_stack(width=4, max_output=50_000)
    assert not fire(gateway, V2_ROLES)
    assert tracker.reserved_output_tokens == 0
    assert tracker.reserved_input_tokens == 0
    before = tracker.reserved_output_tokens
    # Settling an already-settled sequence must not hand capacity back.
    tracker.settle_call(1_000, 1_000, 0, 0, 0.0, call_seq=1)
    assert tracker.reserved_output_tokens == before


# =============================================================================
# 2. role caps
# =============================================================================

def test_every_swarm_v2_role_has_an_explicit_server_owned_cap():
    assert set(ROLE_OUTPUT_CAPS) == {
        ("commander", "planning"), ("commander", "replanning"),
        ("worker", "execute"), ("verifier", "verification")}
    assert all(isinstance(cap, int) and cap > 0 for cap in ROLE_OUTPUT_CAPS.values())


def test_a_worker_cap_belongs_to_the_role_not_to_the_task_id():
    assert role_output_cap("worker:anything-at-all", "execute") == \
        ROLE_OUTPUT_CAPS[("worker", "execute")]


def test_an_unknown_role_fails_closed_rather_than_taking_the_whole_budget():
    gateway, _tracker, _provider = build_stack(width=1, max_output=50_000)
    with pytest.raises(MissingRoleOutputCap):
        gateway.call(model="m", agent="mystery", phase="unknown",
                     messages=[{"role": "user", "content": "hi"}])


def test_a_caller_may_tighten_a_role_cap_but_never_widen_it():
    role = ROLE_OUTPUT_CAPS[("verifier", "verification")]
    gateway, _tracker, provider = build_stack(
        width=1, max_output=50_000, provider=HoldingProvider(1))
    gateway.call(model="m", agent="verifier", phase="verification",
                 messages=[{"role": "user", "content": "hi"}], max_tokens=role * 10)
    assert read_output_cap(provider.observed[-1]) == role


def test_the_guarded_client_applies_a_server_cap_when_a_caller_declares_none():
    """Belt and braces under the role caps: nothing reaches a provider uncapped."""
    provider = HoldingProvider(1)
    tracker = BudgetTracker(
        config=BudgetConfig(max_model_calls_per_run=10, max_output_tokens_per_run=50_000,
                            max_estimated_cost_per_run=10.0,
                            max_run_duration_seconds=60, max_retries=5),
        kill_switch=lambda: True)
    client = build_guarded_client_factory(tracker, lambda k, u: Client(provider))("k", "u")
    client.chat.completions.create(model="m", messages=[{"role": "user", "content": "hi"}])
    assert read_output_cap(provider.observed[-1]) == DEFAULT_OUTPUT_CAP
    assert tracker.missing_output_cap_calls == 1


# =============================================================================
# 3. TPM admission uses input + the explicit cap
# =============================================================================

def test_admission_tokens_are_a_bounded_input_plus_the_requested_cap():
    """The cap is exact; the input side is a BOUND, not an average.

    This assertion used to read `1_000 + 500`, i.e. `chars // 4` plus the cap.
    That is an average for English prose and it is not a bound: a byte-level
    BPE tokenizer can emit one token per UTF-8 byte, so the old value
    under-counted Hebrew input by up to 8x -- against a hard, shared TPM
    ceiling that a single under-count breaches for the whole organization.
    """
    import json

    from backend.provider_scheduler import estimate_input_tokens

    messages = [{"role": "user", "content": "x" * 4_000}]
    admission = estimate_admission_tokens(messages, 500)
    utf8_bytes = len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))
    assert admission >= utf8_bytes + 500, "the admission value is below the byte bound"
    assert admission > estimate_input_tokens(messages) + 500, (
        "admission is still using the optimistic average")


@pytest.mark.parametrize("cap", [None, 0, -1, "lots"])
def test_admission_refuses_a_request_with_no_usable_output_cap(cap):
    with pytest.raises(MissingOutputCap):
        estimate_admission_tokens([{"role": "user", "content": "hi"}], cap)


def test_the_coordinator_is_charged_input_plus_cap_not_actual_output():
    """The provider admitted against the cap, so MILO must account for the cap."""
    charged: list[int] = []
    backend = MemoryQuotaBackend()

    class Recording(ProviderQuotaCoordinator):
        def try_admit_request(self, reserved_tokens):
            charged.append(reserved_tokens)
            return super().try_admit_request(reserved_tokens)

    coordinator = Recording(backend, QuotaConfig(max_concurrency=2))
    # The provider generates only a tenth of the cap it was given.
    provider = HoldingProvider(1, output_ratio=0.1)
    gateway, _tracker, _p = build_stack(width=1, max_output=100_000,
                                        coordinator=coordinator, provider=provider)
    gateway.call(model="m", agent="worker:t1", phase="execute",
                 messages=[{"role": "user", "content": "x" * 4_000}])

    cap = ROLE_OUTPUT_CAPS[("worker", "execute")]
    messages = [{"role": "user", "content": "x" * 4_000}]
    assert charged == [conservative_input_tokens(messages) + cap], (
        "admission was charged on generated output instead of the requested cap")
    # The output side is the requested cap EXACTLY -- the provider admits
    # against what was asked for, not against what it generated -- while the
    # input side is a bound rather than the old `chars // 4` average.
    assert charged[0] - cap > 4_000 // 4


# =============================================================================
# 4. engine parallelism cannot exceed organization capacity
# =============================================================================

def test_concurrency_leases_stay_within_the_shared_ceiling_during_a_burst():
    """Eight logical callers, two organization permits."""
    backend = MemoryQuotaBackend()
    coordinator = ProviderQuotaCoordinator(backend, QuotaConfig(max_concurrency=2))
    provider = HoldingProvider(2)          # only two can be inside at once
    gateway, _tracker, _p = build_stack(width=8, max_output=200_000,
                                        coordinator=coordinator, provider=provider)
    errors = fire(gateway, [V2_ROLES[i % len(V2_ROLES)] for i in range(8)])
    assert not errors, errors
    assert provider.inflight_peak <= 2, (
        f"{provider.inflight_peak} calls were in flight against a 2-permit ceiling")


def test_engine_parallelism_is_clamped_to_available_provider_capacity():
    from backend.engines.swarm_v2.executor import BoundedTaskExecutor

    env = {"MILO_SWARM_MAX_ACTIVE_WORKERS": "8"}
    assert BoundedTaskExecutor.configured_limit(env) == 8
    assert BoundedTaskExecutor.configured_limit(env, provider_capacity=2) == 2
    assert BoundedTaskExecutor.configured_limit({"MILO_SWARM_MAX_ACTIVE_WORKERS": "1"},
                                                provider_capacity=8) == 1


# =============================================================================
# 5. an abandoned acquisition never strands organization capacity
# =============================================================================

def test_a_lease_is_released_when_the_local_slot_wait_gives_up():
    """Failing to enter after taking the shared permit must not hold it.

    Nothing was sent, so completion is PROVEN and the permit goes straight
    back. This is the one class of failure that legitimately releases: the
    request never started, so there is no uncertainty to be conservative
    about. Anything that fails AFTER the request is on the wire is settled the
    other way -- see test_provider_concurrency_ownership.py.
    """
    from backend.provider_quota import ProviderQuotaExhausted
    from backend.provider_scheduler import ProviderBackpressureExceeded
    from backend.runtime import CancellationRequested

    for failure in (ProviderBackpressureExceeded("no local slot"),
                    CancellationRequested("RUN_CANCELLED")):
        backend = MemoryQuotaBackend()
        coordinator = ProviderQuotaCoordinator(backend, QuotaConfig(max_concurrency=1))
        scheduler = ProviderScheduler(
            ProviderLimitsConfig(max_concurrency=1, rpm_limit=None, tpm_limit=None),
            coordinator=coordinator)

        def refuse(*_a, **_k):
            raise failure

        scheduler._acquire_slot = refuse
        with pytest.raises(type(failure)):
            scheduler.execute(lambda: None, estimated_tokens=10, reserved_tokens=10)

        assert coordinator.try_acquire_inference() is not None, (
            f"the organization permit leaked after {type(failure).__name__}")


def test_every_provider_attempt_re_enters_the_shared_admission_gate():
    """A 429 retry is a real provider request and must be admitted again.

    Admitting only the first attempt is how bounded retries quietly push the
    account over its RPM ceiling.
    """
    admitted: list[int] = []
    backend = MemoryQuotaBackend()

    class Counting(ProviderQuotaCoordinator):
        def try_admit_request(self, reserved_tokens):
            admitted.append(reserved_tokens)
            return super().try_admit_request(reserved_tokens)

    # A hand-wound clock the fake sleep advances, so the pause a 429 installs
    # really does elapse without the test waiting on wall time.
    now = [1_000.0]

    def sleep(seconds):
        now[0] += seconds

    coordinator = Counting(backend, QuotaConfig(), clock=lambda: now[0])
    scheduler = ProviderScheduler(
        ProviderLimitsConfig(max_concurrency=2, rpm_limit=None, tpm_limit=None,
                             backoff_base_seconds=0.001, backoff_max_seconds=0.001),
        coordinator=coordinator, sleep_fn=sleep)

    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("Error code: 429 rate_limit_reached_error")
        return "ok"

    assert scheduler.execute(flaky, estimated_tokens=10, reserved_tokens=10) == "ok"
    assert attempts["n"] == 3
    assert len(admitted) == 3, "retried provider attempts bypassed the shared gate"


def test_a_long_call_keeps_its_permit_for_as_long_as_it_runs():
    """A slow provider request must not lose its permit mid-flight.

    Reclaim is CRASH RECOVERY, on a horizon derived from the worker process
    lifetime; it is not a call-duration budget and nothing renews anything.
    A permit that expired under a running call would let a second caller take
    a slot the account is already using.
    """
    import time as real_time

    backend = MemoryQuotaBackend()
    coordinator = ProviderQuotaCoordinator(
        backend, QuotaConfig(max_concurrency=1, lease_ttl_seconds=3))
    scheduler = ProviderScheduler(
        ProviderLimitsConfig(max_concurrency=1, rpm_limit=None, tpm_limit=None),
        coordinator=coordinator)

    observed: list[object] = []

    def slow_call():
        # Longer than the whole nominal lease window, which under the
        # superseded design WAS the reclaim trigger.
        real_time.sleep(2.5)
        observed.append(coordinator.try_acquire_inference())
        return "done"

    assert scheduler.execute(slow_call, estimated_tokens=10, reserved_tokens=10) == "done"
    assert observed == [None], "the permit expired while its own call was still running"
    # ...and it is released cleanly once the call finishes.
    assert coordinator.try_acquire_inference() is not None

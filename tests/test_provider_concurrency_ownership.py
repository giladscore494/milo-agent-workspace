"""Who owns a unit of organization concurrency, proven against a real request.

    A unit of organization inference concurrency is returned to the pool ONLY
    when MILO can prove the request that took it is over.

Independent review of PR #102 found that the previous correction did not
establish this, and it was right. That correction bounded how long MILO WAITS
(`backend.provider_transport`), which is a statement about MILO's thread and
not about the provider. When the deadline fires, the honest state of the
request is UNKNOWN -- nothing in the httpx or OpenAI contract stops a server
computing after its client disconnects -- and the old code answered "unknown"
by freeing the slot. A second worker could then take it while the first
request may still have been consuming account concurrency.

So the server here does the one thing the old tests' servers did not: it KEEPS
WORKING after the client hangs up. That is what makes the measurement real,
because the quantity under test is server-side concurrency -- how many
requests the provider is actually serving at once -- and not anything MILO
believes.

Every test carries its own control. `test_the_superseded_design_*` runs the
identical harness against the behaviour PR #102 shipped and REQUIRES the
failure to appear; a harness that cannot see the defect cannot certify its
absence.

Nothing here leaves the loopback interface and no provider is called.
"""

from __future__ import annotations

import http.server
import re
import threading
import time
from pathlib import Path

import pytest
from types import SimpleNamespace

from backend.provider_quota import (GUARANTEE_PROVEN_COMPLETION, GUARANTEE_TIMED_RECLAIM,
                                    WORKER_MAX_LIFETIME_SECONDS, LeaseRecoveryRefused,
                                    MemoryQuotaBackend, ProviderQuotaCoordinator,
                                    ProviderQuotaUnavailable, QuotaConfig,
                                    assert_abandoned_lease_reclaim_safe, lease_safety_margin,
                                    minimum_abandoned_lease_reclaim, resolve_coordinator)
from backend.provider_scheduler import (RATE_LIMIT_REACHED, ProviderBackpressureExceeded,
                                        ProviderLimitsConfig, ProviderScheduler,
                                        classify_provider_error, request_completion_is_proven)
from backend.provider_transport import (ProviderRequestDeadlineExceeded,
                                        build_deadline_http_client)

REPO = Path(__file__).resolve().parents[1]

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


#: MILO's total wall-clock deadline for one request.
DEADLINE = 1.5
#: How long the provider keeps working -- INCLUDING after the client leaves.
#: Comfortably longer than the deadline, so "MILO stopped waiting" and "the
#: request stopped" are genuinely different moments.
PROVIDER_WORK_SECONDS = 6.0
#: How long worker B is willing to queue before reporting a refusal.
B_PATIENCE = 3.0


class PersistentProvider:
    """A server that keeps computing after its client disconnects.

    This is the whole point. A server that stops when the client hangs up
    cannot distinguish the two designs, because both hang up. A real provider
    may finish the work it started, and the concurrency it consumes while
    doing so is invisible to MILO -- which is exactly why MILO must not
    reuse the slot.
    """

    def __init__(self, work_seconds: float = PROVIDER_WORK_SECONDS):
        self.peak = 0
        self.served = 0
        self._overlap_seconds = 0.0
        self._active = 0
        self._overlap_since: float | None = None
        self._lock = threading.Lock()
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler's name
                outer._enter()
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    started = time.monotonic()
                    # Trickle, so no gap ever reaches an inactivity timeout,
                    # and deliberately do NOT check whether the peer is still
                    # there: this request occupies the provider for its full
                    # duration no matter what MILO does.
                    while time.monotonic() - started < work_seconds:
                        try:
                            self.wfile.write(b"1\r\n \r\n")
                            self.wfile.flush()
                        except OSError:
                            pass  # the client is gone; the work is not
                        time.sleep(0.05)
                except Exception:  # noqa: BLE001 - the client leaving is the point
                    pass
                finally:
                    outer._exit()

            def log_message(self, *args):
                pass

        class Server(http.server.ThreadingHTTPServer):
            def handle_error(self, *args):
                # A client that walks away mid-response is the subject of
                # these tests, not an error to print a traceback about.
                pass

        self._server = Server(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/v1/chat/completions"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def active(self) -> int:
        with self._lock:
            return self._active

    @property
    def overlap_seconds(self) -> float:
        """Seconds during which two requests were being served AT ONCE.

        The quantity the organization ceiling is actually about -- and read
        LIVE, including an overlap still in progress. Handlers here outlive
        the clients that started them by design, so waiting for them to
        finish before measuring would mean never measuring at all.
        """
        with self._lock:
            open_overlap = (time.monotonic() - self._overlap_since
                            if self._overlap_since is not None else 0.0)
            return self._overlap_seconds + open_overlap

    def _enter(self):
        with self._lock:
            self._active += 1
            self.served += 1
            self.peak = max(self.peak, self._active)
            if self._active >= 2 and self._overlap_since is None:
                self._overlap_since = time.monotonic()

    def _exit(self):
        with self._lock:
            if self._active >= 2 and self._overlap_since is not None:
                self._overlap_seconds += time.monotonic() - self._overlap_since
                self._overlap_since = None
            self._active -= 1

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture()
def provider():
    with PersistentProvider() as server:
        yield server


# =============================================================================
# the two designs, as coordinators over one shared store
# =============================================================================


class SupersededOwnership(ProviderQuotaCoordinator):
    """What PR #102 shipped: an unproven outcome hands the slot back.

    Its scheduler ran `finally: lease.release()` on every path -- success,
    deadline, transport error alike -- so "MILO stopped waiting" was treated
    as "the request is over". Routing quarantine to `release_inference` issues
    the identical `ZREM` at the identical moment, so the shared store sees
    exactly the old behaviour and nothing else about the harness differs.
    """

    def quarantine_inference(self, lease_id, reason):
        self.release_inference(lease_id)


class TimedReclaim(ProviderQuotaCoordinator):
    """A deployment that opted into reclaiming abandoned leases on a clock.

    This is a SUPPORTED configuration, not a bug -- but it trades the proven
    guarantee for an assumption about provider-side behaviour that MILO cannot
    check, and the tests below show exactly what that assumption buys and
    costs. Scaled to the test's seconds.
    """

    def _abandoned_reclaim_ms(self):
        return int(self.config.lease_ttl_seconds * 1000)


def two_workers(coordinator_class=ProviderQuotaCoordinator, *, ceiling=1,
                probe="healthy"):
    """Two 'processes' over ONE shared store: two Cloud Run executions."""
    backend = MemoryQuotaBackend()
    config = QuotaConfig(max_concurrency=ceiling, lease_ttl_seconds=2.0,
                         worker_max_lifetime_seconds=3.0)

    class WorkerA(coordinator_class):
        def verify_inference_ownership(self, lease_id):
            if probe == "returns_false":
                return False
            if probe == "raises":
                raise ProviderQuotaUnavailable()
            return super().verify_inference_ownership(lease_id)

    return WorkerA(backend, config), coordinator_class(backend, config)


def scheduler_for(coordinator, *, patience=B_PATIENCE):
    return ProviderScheduler(
        ProviderLimitsConfig(max_concurrency=1, rpm_limit=None, tpm_limit=None,
                             max_backpressure_wait_seconds=patience),
        coordinator=coordinator)


def run_race(server, coordinator_class, probe, *, b_delay=0.0, b_patience=B_PATIENCE):
    """A's request outlives A's patience; B tries to take the freed slot.

    ``b_delay`` is how long B waits AFTER A is genuinely inside the provider
    before attempting admission, so a test can place B's attempt on either
    side of a horizon.
    """
    worker_a, worker_b = two_workers(coordinator_class, probe=probe)
    errors: dict[str, BaseException] = {}
    a_inside = threading.Event()

    def issue(label, scheduler, event=None):
        def call():
            if event is not None:
                event.set()
            with build_deadline_http_client(DEADLINE) as client:
                return client.post(server.url, json={})
        try:
            scheduler.execute(call, estimated_tokens=10, reserved_tokens=10)
        except BaseException as exc:  # noqa: BLE001 - recorded, asserted by caller
            errors[label] = exc

    def b_thread():
        # Only attempt once A is genuinely being served, so the outcome cannot
        # depend on which thread happened to win the permit first.
        if not a_inside.wait(timeout=10.0):
            errors["b"] = AssertionError("worker A never reached the provider")
            return
        while server.served < 1:
            time.sleep(0.01)
        time.sleep(b_delay)
        issue("b", scheduler_for(worker_b, patience=b_patience))

    threads = [threading.Thread(target=issue,
                                args=("a", scheduler_for(worker_a), a_inside)),
               threading.Thread(target=b_thread)]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]
    assert not any(t.is_alive() for t in threads), "a worker never finished"
    errors["_worker_b"] = worker_b
    return errors


# =============================================================================
# 1. the harness measures what it claims to
# =============================================================================

def test_the_provider_keeps_working_after_milo_gives_up(provider):
    """Without this, everything below is measuring the wrong thing."""
    started = time.monotonic()
    with build_deadline_http_client(DEADLINE) as client:
        with pytest.raises(ProviderRequestDeadlineExceeded):
            client.post(provider.url, json={})
    milo_stopped = time.monotonic() - started
    assert milo_stopped < DEADLINE + 1.0, f"MILO waited {milo_stopped:.2f}s"
    # MILO is gone; the provider is not. This gap is the entire problem.
    time.sleep(0.3)
    assert provider._active == 1, (
        "the provider stopped when its client did, so this harness cannot "
        "exercise the case where completion is genuinely unknown")


# =============================================================================
# 2. the real mechanism
# =============================================================================

@pytest.mark.parametrize("probe", ["healthy", "returns_false", "raises"])
def test_an_unproven_request_never_lets_a_second_worker_in(provider, probe):
    """The HIGH, closed.

    Worker A's deadline fires while the provider is still working, so A cannot
    prove its request stopped. Under a ceiling of ONE, worker B must not be
    admitted -- and the provider, which is the only honest witness, must never
    see two requests at once.

    Parametrized over the ownership probe because the invariant may not depend
    on it: healthy, disowned, and store-unreachable must all give the same
    answer.
    """
    errors = run_race(provider, ProviderQuotaCoordinator, probe)

    assert provider.served >= 1, "worker A never reached the provider"
    assert provider.peak == 1, (
        f"the provider served {provider.peak} MILO requests at once under a "
        "ceiling of 1")
    assert provider.overlap_seconds == 0.0, (
        f"{provider.overlap_seconds:.3f}s of concurrent service was observed")
    assert isinstance(errors.get("a"), ProviderRequestDeadlineExceeded), errors
    # B is refused, and says so, rather than quietly proceeding.
    assert isinstance(errors.get("b"), ProviderBackpressureExceeded), errors


@pytest.mark.parametrize("probe", ["healthy", "returns_false", "raises"])
def test_the_superseded_design_admits_a_second_worker(provider, probe):
    """The negative control. It MUST fail the assertion above.

    One override, `quarantine_inference` -> release, restores exactly what PR
    #102 did: treat "MILO stopped waiting" as "the request is over". If the
    harness cannot see the defect here, its silence above proves nothing.
    """
    run_race(provider, SupersededOwnership, probe)

    assert provider.peak == 2, (
        "the superseded design did not even produce two concurrent requests, "
        "so this harness cannot detect the failure it certifies the absence of")
    assert provider.overlap_seconds > 1.0, (
        f"only {provider.overlap_seconds:.3f}s of overlap under the known-bad "
        "design; that is a teardown tail, not the sustained overlap expected")


def test_a_fired_deadline_is_not_proof_that_the_request_stopped():
    """The classification the whole design rests on, stated directly."""
    proven, reason = request_completion_is_proven(
        ProviderRequestDeadlineExceeded(90.0, 91.2))
    assert proven is False
    assert reason == "PROVIDER_REQUEST_DEADLINE_EXCEEDED"


@pytest.mark.parametrize("exc,expected", [
    (None, True),                                    # the call returned
    (StructuralRateLimit(), True),                   # a real 429, with a response
    (RuntimeError("Error code: 429 rate_limit_reached_error"), False),
    (RuntimeError("max organization concurrency reached"), False),
    (TimeoutError("read timed out"), False),
    (RuntimeError("something nobody enumerated"), False),
    (ProviderRequestDeadlineExceeded(5.0, 5.1), False),
])
def test_only_structural_evidence_releases_a_slot(exc, expected):
    """The default is NO, and message text is never a yes."""
    assert request_completion_is_proven(exc)[0] is expected


@pytest.mark.parametrize("text", [
    "Error code: 429 rate_limit_reached_error",
    "max organization concurrency reached",
    "engine_overloaded_error",
    "http 429",
])
def test_a_text_only_429_is_quarantined_while_a_structural_one_releases(text):
    """The exact confusion review found, pinned from both sides.

    `classify_provider_error` recognises all of these, deliberately: for a
    RETRY decision, reading text permissively only ever costs a wait. For a
    CONCURRENCY decision the same permissiveness would let any exception whose
    message happened to contain "429" hand an organization slot to another
    worker. So the classifier still matches...
    """
    from backend.provider_scheduler import classify_provider_error

    text_only = RuntimeError(text)
    assert classify_provider_error(text_only) is not None

    # ...and settlement still refuses it.
    proven, reason = request_completion_is_proven(text_only)
    assert proven is False
    assert reason == "PROVIDER_REQUEST_OUTCOME_UNKNOWN"

    # The same failure WITH a response object is a real provider answer.
    assert request_completion_is_proven(StructuralRateLimit(text))[0] is True


def test_a_text_only_429_really_does_hold_the_slot_end_to_end():
    """Not just the predicate: the slot itself, through the scheduler."""
    backend = MemoryQuotaBackend()
    config = QuotaConfig(max_concurrency=1, lease_ttl_seconds=2.0,
                         worker_max_lifetime_seconds=3.0)
    coordinator = ProviderQuotaCoordinator(backend, config)
    scheduler = ProviderScheduler(
        ProviderLimitsConfig(max_concurrency=1, rpm_limit=None, tpm_limit=None,
                             max_rate_limit_retries=1, backoff_base_seconds=0.001,
                             backoff_max_seconds=0.001,
                             max_backpressure_wait_seconds=0.5),
        coordinator=coordinator, sleep_fn=lambda _s: None)

    with pytest.raises(BaseException):  # noqa: B017 - the outcome under test
        scheduler.execute(
            lambda: (_ for _ in ()).throw(
                RuntimeError("Error code: 429 rate_limit_reached_error")),
            estimated_tokens=10, reserved_tokens=10)

    assert coordinator.try_acquire_inference() is None, (
        "an exception whose only 429 evidence was its message text released "
        "an organization concurrency slot")


def test_a_bare_status_code_without_a_response_object_is_not_proof():
    """Any wrapper can set an attribute. Only a response object is evidence."""

    class LooksLikeAStatusError(Exception):
        status_code = 429

    assert classify_provider_error(LooksLikeAStatusError("x")) == RATE_LIMIT_REACHED
    assert request_completion_is_proven(LooksLikeAStatusError("x"))[0] is False


def test_milos_own_http_status_is_not_provider_proof():
    """`AppError.status_code` is MILO's API status, not the provider's."""
    from backend.errors import AppError

    assert request_completion_is_proven(AppError("PROVIDER_QUOTA_UNAVAILABLE", "x", 503))[0] is False


def test_a_response_object_without_an_integer_status_is_not_proof():
    class NoStatus(Exception):
        response = SimpleNamespace(status_code=None)

    class BoolStatus(Exception):
        response = SimpleNamespace(status_code=True)

    assert request_completion_is_proven(NoStatus("x"))[0] is False
    assert request_completion_is_proven(BoolStatus("x"))[0] is False


def test_a_deadline_wrapped_by_the_sdk_is_still_a_deadline():
    """The OpenAI SDK re-raises transport failures as APIConnectionError."""
    wrapper = RuntimeError("Connection error.")
    wrapper.__cause__ = ProviderRequestDeadlineExceeded(90.0, 91.2)
    proven, reason = request_completion_is_proven(wrapper)
    assert proven is False
    assert reason == "PROVIDER_REQUEST_DEADLINE_EXCEEDED"


def test_the_real_sdk_status_error_is_structural_proof():
    """The proof rule is written against the SDK's actual error shape."""
    import httpx
    import openai

    request = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
    response = httpx.Response(429, request=request,
                              text='{"error":{"type":"rate_limit_reached_error"}}')
    error = openai.RateLimitError("Error code: 429", response=response, body=None)
    assert classify_provider_error(error) == RATE_LIMIT_REACHED
    assert request_completion_is_proven(error)[0] is True
    # ...and its connection-error sibling, which carries no response, is not.
    assert request_completion_is_proven(openai.APIConnectionError(request=request))[0] is False


def _retrying_scheduler(coordinator, clock, *, ceiling_local=1, patience=5.0):
    return ProviderScheduler(
        ProviderLimitsConfig(max_concurrency=ceiling_local, rpm_limit=None, tpm_limit=None,
                             max_rate_limit_retries=3, backoff_base_seconds=0.001,
                             backoff_max_seconds=0.001, max_backpressure_wait_seconds=patience),
        coordinator=coordinator, sleep_fn=clock.sleep)


def test_a_text_only_429_backs_off_but_its_permit_stays_held():
    """Retry classification stays tolerant; settlement does not.

    Ceiling of TWO so the retry can proceed: the first attempt's permit stays
    held (its fate is unknown), the retry takes the SECOND unit, succeeds and
    releases that one. One lease remains held afterwards, and it is announced.
    """
    backend = MemoryQuotaBackend()
    signals: list[tuple] = []
    clock = Clock()
    coordinator = ProviderQuotaCoordinator(
        backend, QuotaConfig(max_concurrency=2, lease_ttl_seconds=2.0,
                             worker_max_lifetime_seconds=3.0),
        clock=clock, diagnostic_sink=lambda kind, payload: signals.append((kind, payload)))
    waits: list[str] = []
    scheduler = ProviderScheduler(
        ProviderLimitsConfig(max_concurrency=2, rpm_limit=None, tpm_limit=None,
                             max_rate_limit_retries=3, backoff_base_seconds=0.001,
                             backoff_max_seconds=0.001),
        coordinator=coordinator, sleep_fn=clock.sleep,
        backpressure_callback=lambda _a, _p, reason, _d: waits.append(reason))
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("Error code: 429 rate_limit_reached_error")
        return "ok"

    assert scheduler.execute(flaky, estimated_tokens=10, reserved_tokens=10) == "ok"
    assert attempts["n"] == 2 and "provider_rate_limited" in waits
    held = coordinator.held_inference_leases()
    assert len(held) == 1, held
    quarantined = [p for k, p in signals if k == "provider_lease_quarantined"]
    assert len(quarantined) == 1 and quarantined[0]["lease_id"] == held[0].lease_id


def test_a_retry_cannot_take_capacity_that_an_unproven_attempt_still_holds():
    """Ceiling of ONE. The text-only 429 quarantines the only unit, so the
    retry must be REFUSED rather than run under a slot whose previous request
    may still be executing. Retryable and finished are different questions."""
    backend = MemoryQuotaBackend()
    clock = Clock()
    coordinator = ProviderQuotaCoordinator(
        backend, QuotaConfig(max_concurrency=1, lease_ttl_seconds=2.0,
                             worker_max_lifetime_seconds=3.0), clock=clock)
    scheduler = _retrying_scheduler(coordinator, clock, patience=2.0)
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        raise RuntimeError("Error code: 429 rate_limit_reached_error")

    with pytest.raises(ProviderBackpressureExceeded):
        scheduler.execute(flaky, estimated_tokens=10, reserved_tokens=10)
    assert attempts["n"] == 1, "the retry ran under a permit whose request was unproven"
    assert len(coordinator.held_inference_leases()) == 1
    assert coordinator.try_acquire_inference() is None


def test_the_second_unit_is_counted_not_created():
    """Ceiling of TWO, two text-only 429s in a row: both units end up held and
    the THIRD attempt is refused. Every unit the provider might be using is
    counted; none is invented."""
    backend = MemoryQuotaBackend()
    clock = Clock()
    coordinator = ProviderQuotaCoordinator(
        backend, QuotaConfig(max_concurrency=2, lease_ttl_seconds=2.0,
                             worker_max_lifetime_seconds=3.0), clock=clock)
    scheduler = _retrying_scheduler(coordinator, clock, ceiling_local=2, patience=2.0)
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        raise RuntimeError("Error code: 429 rate_limit_reached_error")

    with pytest.raises(ProviderBackpressureExceeded):
        scheduler.execute(flaky, estimated_tokens=10, reserved_tokens=10)
    assert attempts["n"] == 2
    assert len(coordinator.held_inference_leases()) == 2


def test_a_second_worker_is_refused_after_the_process_lifetime_floor_through_the_scheduler():
    """The reclaim boundary, driven through the REAL scheduler rather than a
    manual quarantine: A's deadline fires and settles UNPROVEN, the provider
    keeps serving A's request past the process-lifetime floor, and B is
    admitted through its own scheduler only after that floor. The provider
    must never see two requests at once."""
    floor = minimum_abandoned_lease_reclaim(3.0)          # 3.75s
    with PersistentProvider(work_seconds=8.0) as server:
        errors = run_race(server, ProviderQuotaCoordinator, "healthy",
                          b_delay=floor + 0.5, b_patience=0.5)
        assert server.active >= 1, "the provider had already finished when B tried"
        assert server.peak == 1
        assert server.overlap_seconds == 0.0
        assert isinstance(errors.get("a"), ProviderRequestDeadlineExceeded), errors
        assert isinstance(errors.get("b"), ProviderBackpressureExceeded), errors
        held = errors["_worker_b"].held_inference_leases()
        assert len(held) == 1 and held[0].recovery_eligible is True, (
            "the slot is offered to a human, never taken by the next process")


def test_the_timed_reclaim_admits_a_second_worker_through_the_scheduler_too():
    """Negative control for the test above, same harness, one override."""
    with PersistentProvider(work_seconds=8.0) as server:
        errors = run_race(server, TimedReclaim, "healthy",
                          b_delay=2.0 + 0.5, b_patience=0.5)
        assert isinstance(errors.get("b"), ProviderRequestDeadlineExceeded), (
            f"under the timer B should have been ADMITTED and then hit its own deadline; got {errors}")
        assert server.peak == 2
        assert server.overlap_seconds > 1.0


def test_a_complete_http_response_is_proof_whatever_its_status():
    """Otherwise ordinary backpressure would quarantine slots for an hour."""

    class Response:
        status_code = 500

    class ApiError(Exception):
        response = Response()

    assert request_completion_is_proven(ApiError("server error"))[0] is True


def test_a_connect_failure_is_proof_that_nothing_was_sent():
    import httpx

    wrapper = RuntimeError("APIConnectionError")
    wrapper.__cause__ = httpx.ConnectError("connection refused")
    assert request_completion_is_proven(wrapper)[0] is True


# =============================================================================
# 3. the provider stays busy past ANY horizon; the slot stays held
# =============================================================================

def test_a_slot_is_not_reused_while_the_provider_is_still_busy_however_long(provider):
    """The reviewer's regression, and the one the previous design failed.

    Worker A's deadline fires at 1.5s; the provider keeps working until 6s.
    The test then waits past the point at which the superseded timed design
    WOULD have reclaimed the slot (the scaled 3.75s horizon) and keeps asking.
    Because nothing reclaims an unreleased lease by default, worker B is never
    admitted, and the provider -- the only witness that can see its own
    occupancy -- never serves two MILO requests at once.
    """
    worker_a, worker_b = two_workers()
    assert worker_a.config.reclaims_abandoned_leases is False
    superseded_horizon = worker_a.config.lease_ttl_seconds + 1.75   # 3.75s

    def a_request():
        with build_deadline_http_client(DEADLINE) as client:
            try:
                client.post(provider.url, json={})
            except Exception:  # noqa: BLE001 - A gives up; the provider does not
                pass

    lease = worker_a.try_acquire_inference()
    assert lease is not None
    threading.Thread(target=a_request, daemon=True).start()
    while provider.served < 1:
        time.sleep(0.02)
    lease.quarantine("PROVIDER_REQUEST_DEADLINE_EXCEEDED")

    started = time.monotonic()
    refusals = 0
    while time.monotonic() - started < superseded_horizon + 1.0:
        assert worker_b.try_acquire_inference() is None, (
            f"the slot was reclaimed {time.monotonic() - started:.2f}s after "
            "quarantine, while the provider was still serving the request")
        refusals += 1
        time.sleep(0.1)

    assert refusals > 10, "the loop did not actually keep asking"
    assert provider._active == 1, "the provider stopped; nothing was proven"
    assert provider.peak == 1
    assert provider.overlap_seconds == 0.0


def test_the_timed_reclaim_opt_in_does_admit_a_second_worker(provider):
    """The negative control, and an honest account of the opt-in.

    Identical harness, one configuration change: a deployment that enabled
    `MILO_PROVIDER_ABANDONED_LEASE_RECLAIM_SECONDS`. The slot comes back on
    the clock and worker B enters WHILE THE PROVIDER IS STILL WORKING. That is
    the guarantee being traded away, measured rather than described -- and it
    is why it is not the default.
    """
    worker_a, worker_b = two_workers(TimedReclaim)

    def a_request():
        with build_deadline_http_client(DEADLINE) as client:
            try:
                client.post(provider.url, json={})
            except Exception:  # noqa: BLE001 - A gives up; the provider does not
                pass

    assert worker_a.try_acquire_inference() is not None
    threading.Thread(target=a_request, daemon=True).start()
    while provider.served < 1:
        time.sleep(0.02)

    time.sleep(worker_a.config.lease_ttl_seconds + 0.3)
    stolen = worker_b.try_acquire_inference()
    assert stolen is not None, (
        "the opt-in timer did not reclaim, so this control cannot show what "
        "the default is protecting against")

    with build_deadline_http_client(DEADLINE) as client:
        try:
            client.post(provider.url, json={})
        except Exception:  # noqa: BLE001 - the measurement is server-side
            pass
    assert provider.peak == 2, (
        "the timed reclaim did not produce concurrent provider requests")


# =============================================================================
# 4. what returns a held slot: a proven release, or a human
# =============================================================================

class Clock:
    """A clock the fake sleep winds forward.

    A 429 pauses admissions for a moment, so a test that stubs out sleeping
    but leaves the coordinator on wall-clock time would spin against a pause
    that never elapses. The two move together here.
    """

    def __init__(self, now: float = 10_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(float(seconds), 0.001)


def test_a_crashed_worker_holds_its_slot_until_a_human_returns_it():
    """A process SIGKILLed mid-request settles nothing at all.

    Which is the design: quarantine is the absence of an action, so a worker
    that cannot run any code still holds its slot. Nothing gives it back on a
    clock, because nothing MILO can observe proves the provider stopped.
    """
    clock = Clock()
    backend = MemoryQuotaBackend()
    config = QuotaConfig(max_concurrency=1, lease_ttl_seconds=2.0,
                         worker_max_lifetime_seconds=3.0)
    dead = ProviderQuotaCoordinator(backend, config, clock=clock)
    survivor = ProviderQuotaCoordinator(backend, config, clock=clock)

    lease = dead.try_acquire_inference()
    assert lease is not None
    # ...and now the process dies. Nothing releases, nothing quarantines.

    for elapsed in (0.0, config.lease_ttl_seconds + 1,
                    minimum_abandoned_lease_reclaim(3.0) + 1, 86_400.0):
        clock.now = 10_000.0 + elapsed
        assert survivor.try_acquire_inference() is None, (
            f"the slot came back on its own after {elapsed:g}s")

    held = survivor.held_inference_leases()
    assert [item.lease_id for item in held] == [lease.lease_id]
    assert held[0].expires_at == float("inf"), (
        "the held lease carries a finite expiry, so something will eventually "
        "reclaim it without proof")
    assert held[0].acquired_at_ms == 10_000_000
    assert held[0].recovery_eligible is True

    # The manual half of the design.
    assert survivor.operator_reclaim_inference(
        lease.lease_id, reason="checked the provider console; request is gone")
    recovered = survivor.try_acquire_inference()
    assert recovered is not None
    # The zombie's late cleanup frees nothing: its id is gone from the store.
    assert lease.release() is False
    assert survivor.try_acquire_inference() is None
    recovered.release()


def test_an_operator_reclaim_is_refused_while_the_holders_process_could_be_alive():
    """The one thing MILO CAN check before a human takes a slot back.

    Below the process-lifetime floor the lease's own process may still be
    running and may still settle it on proof; reclaiming it would put two
    real requests under one admission. The refusal and the removal are one
    atomic store step, so the operator cannot race a live holder.
    """
    clock = Clock()
    backend = MemoryQuotaBackend()
    config = QuotaConfig(max_concurrency=1, lease_ttl_seconds=2.0,
                         worker_max_lifetime_seconds=3.0)
    floor = minimum_abandoned_lease_reclaim(3.0)          # 3.75s
    holder = ProviderQuotaCoordinator(backend, config, clock=clock)
    operator = ProviderQuotaCoordinator(backend, config, clock=clock)
    lease = holder.try_acquire_inference()

    clock.now = 10_000.0 + floor - 0.01
    assert operator.held_inference_leases()[0].recovery_eligible is False
    with pytest.raises(LeaseRecoveryRefused) as young:
        operator.operator_reclaim_inference(lease.lease_id, reason="impatient")
    assert young.value.code == "LEASE_TOO_YOUNG"
    assert operator.try_acquire_inference() is None, "a refused reclaim freed the slot"

    clock.now = 10_000.0 + floor + 0.01
    assert operator.held_inference_leases()[0].recovery_eligible is True
    assert operator.operator_reclaim_inference(lease.lease_id, reason="now eligible")
    with pytest.raises(LeaseRecoveryRefused) as gone:
        operator.operator_reclaim_inference(lease.lease_id, reason="again")
    assert gone.value.code == "LEASE_NOT_HELD"


def test_a_lease_of_unknown_age_is_refused_not_assumed_old():
    backend = MemoryQuotaBackend()
    config = QuotaConfig(max_concurrency=1, lease_ttl_seconds=2.0,
                         worker_max_lifetime_seconds=3.0)
    clock = Clock()
    coordinator = ProviderQuotaCoordinator(backend, config, clock=clock)
    lease = coordinator.try_acquire_inference()
    # A lease written by a store revision that recorded no acquisition time.
    backend._acquired["milo:pq:kimi-org:conc"].pop(lease.lease_id)
    clock.now += 10 * 86_400
    assert coordinator.held_inference_leases()[0].recovery_eligible is False
    with pytest.raises(LeaseRecoveryRefused) as refused:
        coordinator.operator_reclaim_inference(lease.lease_id, reason="surely old")
    assert refused.value.code == "LEASE_TOO_YOUNG"


def test_recovery_and_the_age_check_are_one_atomic_store_operation():
    from backend import provider_quota

    script = provider_quota._LUA_RECOVER
    assert "HGET" in script and "ZREM" in script and "return -1" in script
    assert script.index("HGET") < script.index("ZREM")


def test_the_acquisition_time_is_recorded_in_the_same_atomic_step_as_the_lease():
    from backend import provider_quota

    assert "HSET" in provider_quota._LUA_ACQUIRE
    assert provider_quota._LUA_ACQUIRE.index("ZADD") < provider_quota._LUA_ACQUIRE.index("HSET")


def test_nothing_in_milo_reclaims_a_held_lease_on_its_own():
    """The operator tool is the only caller. Checked over the source tree."""
    callers = []
    for path in sorted((REPO / "backend").rglob("*.py")):
        text = path.read_text()
        if "operator_reclaim_inference(" in text and path.name != "provider_quota.py":
            callers.append(str(path.relative_to(REPO)))
    assert callers == [], f"MILO code reclaims held leases on its own: {callers}"
    tool = (REPO / "scripts/release/provider_quota_leases.py").read_text()
    assert tool.count("operator_reclaim_inference(") == 1
    assert "--all" not in tool, "the operator tool must reclaim one lease per invocation"
    # And acquisition never calls into recovery: checked over the AST, so a
    # comment cannot trip it and a call cannot hide from it.
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(ProviderQuotaCoordinator.try_acquire_inference)))
    called = {node.func.attr for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    assert not called & {"operator_reclaim_inference", "recover_concurrency",
                         "release_concurrency", "release_inference"}, called


def test_an_operator_reclaim_announces_itself_and_demands_a_reason():
    """It asserts something MILO cannot check, so it leaves a record."""
    signals: list[tuple] = []
    backend = MemoryQuotaBackend()
    clock = Clock()
    coordinator = ProviderQuotaCoordinator(
        backend, QuotaConfig(max_concurrency=1), clock=clock,
        diagnostic_sink=lambda kind, payload: signals.append((kind, payload)))
    lease = coordinator.try_acquire_inference()
    clock.now += QuotaConfig().minimum_abandoned_lease_reclaim_seconds + 1

    with pytest.raises(ValueError) as blank:
        coordinator.operator_reclaim_inference(lease.lease_id, reason="   ")
    assert isinstance(blank.value, LeaseRecoveryRefused)
    assert blank.value.code == "REASON_REQUIRED"

    coordinator.operator_reclaim_inference(lease.lease_id, reason="drained by hand")
    kinds = [kind for kind, _ in signals]
    assert "provider_lease_operator_reclaimed" in kinds, signals
    payload = next(p for k, p in signals if k == "provider_lease_operator_reclaimed")
    assert payload["reason"] == "drained by hand"
    assert payload["reclaimed"] is True


def test_a_quarantine_says_whether_anything_will_ever_give_the_slot_back():
    """The operator burden has to be visible, or it is just missing capacity."""
    for reclaim, expected in ((None, "operator_reclaim"), (5000.0, "5000s")):
        signals: list[tuple] = []
        coordinator = ProviderQuotaCoordinator(
            MemoryQuotaBackend(),
            QuotaConfig(max_concurrency=1, abandoned_lease_reclaim_seconds=reclaim),
            diagnostic_sink=lambda kind, payload: signals.append((kind, payload)))
        coordinator.try_acquire_inference().quarantine("PROVIDER_REQUEST_OUTCOME_UNKNOWN")
        payload = next(p for k, p in signals if k == "provider_lease_quarantined")
        assert payload["held_until"] == expected
        assert payload["guarantee"] == (
            GUARANTEE_PROVEN_COMPLETION if reclaim is None else GUARANTEE_TIMED_RECLAIM)


# =============================================================================
# 4b. the guarantee is stated, and no timer is invented
# =============================================================================

def test_the_default_makes_the_guarantee_milo_can_actually_keep():
    config = QuotaConfig()
    assert config.abandoned_lease_reclaim_seconds is None
    assert config.reclaims_abandoned_leases is False
    assert config.concurrency_guarantee == GUARANTEE_PROVEN_COMPLETION


def test_the_worker_lifetime_matches_the_deployed_cloud_run_contract():
    """The number is read off the deployment, not chosen.

    It no longer licenses a reclaim -- it only floors one a deployment opts
    into. But if the job's task timeout ever moves, the floor must move with
    it, so the constant stays tied to the artifact it came from.
    """
    script = (REPO / "scripts/deploy/cloud-run.sh").read_text()
    timeouts = {int(v) for v in re.findall(r"--task-timeout[= ](\d+)", script)}
    assert timeouts, "the worker job no longer declares a task timeout"
    assert max(timeouts) == WORKER_MAX_LIFETIME_SECONDS


def test_the_release_plan_pins_the_same_task_timeout():
    plan = (REPO / "scripts/release/generate-deployment-plan.sh").read_text()
    assert f"--task-timeout {int(WORKER_MAX_LIFETIME_SECONDS)}" in plan


def test_the_process_lifetime_is_only_a_floor_never_a_licence():
    """3600+900 is the SHORTEST a timer may be, not a time it may run."""
    assert minimum_abandoned_lease_reclaim(3600.0) == 4500.0
    assert lease_safety_margin(3600.0) == 900.0
    # And by itself it enables nothing.
    assert QuotaConfig(worker_max_lifetime_seconds=3600.0).reclaims_abandoned_leases is False


def test_no_timer_at_all_is_always_accepted():
    """The configuration that makes no unverifiable assumption needs no check."""
    for lifetime in (1.0, 60.0, 3600.0, 7200.0):
        assert_abandoned_lease_reclaim_safe(None, lifetime)


@pytest.mark.parametrize("configured,lifetime", [
    (120.0, 3600.0),      # the superseded request-scale value
    (3600.0, 3600.0),     # the lifetime with no margin at all
    (4499.0, 3600.0),     # one second short of the margin
])
def test_a_timer_below_the_process_lifetime_is_refused(configured, lifetime):
    with pytest.raises(ValueError):
        assert_abandoned_lease_reclaim_safe(configured, lifetime)
    with pytest.raises(ValueError):
        QuotaConfig(worker_max_lifetime_seconds=lifetime,
                    abandoned_lease_reclaim_seconds=configured)


def test_opting_in_is_explicit_and_says_what_it_gives_up():
    config = QuotaConfig.from_env(
        {"MILO_PROVIDER_ABANDONED_LEASE_RECLAIM_SECONDS": "5400"})
    assert config.abandoned_lease_reclaim_seconds == 5400.0
    assert config.concurrency_guarantee == GUARANTEE_TIMED_RECLAIM
    # And an absent variable means absent, not a default timer.
    assert QuotaConfig.from_env({}).abandoned_lease_reclaim_seconds is None


@pytest.mark.parametrize("lifetime", ["120", "1800", "3599"])
def test_an_environment_cannot_shorten_the_worker_lifetime(lifetime):
    with pytest.raises(ValueError):
        QuotaConfig.from_env({"MILO_WORKER_MAX_LIFETIME_SECONDS": lifetime})


def test_production_refuses_a_short_timer_handed_in_directly():
    """`from_env` is not the only way a config reaches the coordinator."""
    unsafe = QuotaConfig(worker_max_lifetime_seconds=60.0,
                         abandoned_lease_reclaim_seconds=75.0)
    with pytest.raises(ValueError):
        resolve_coordinator(unsafe, env={
            "ENVIRONMENT": "production",
            "UPSTASH_REDIS_REST_URL": "https://example.invalid",
            "UPSTASH_REDIS_REST_TOKEN": "unused-in-this-test",
        })


def test_a_held_lease_is_stored_with_no_expiry_and_in_a_key_with_none_either():
    """Both halves, because either one alone would be an auto-reclaim.

    An infinite member score keeps the LEASE from expiring; persisting the key
    keeps the whole set -- every held lease in it -- from expiring underneath.
    A hygiene TTL on the key would have been a timed reclaim by the back door.
    """
    calls: list[list[str]] = []

    from backend.provider_quota import UpstashQuotaBackend

    backend = UpstashQuotaBackend(
        "https://example.invalid", "unused",
        http_post=lambda _url, body: calls.append(body) or {"result": [1, 1]})
    backend.acquire_concurrency("k", "lease", 1, None, 1_000)

    body = calls[0]
    script, nkeys = body[1], int(body[2])
    keys, args = body[3:3 + nkeys], body[3 + nkeys:]
    assert keys == ["k", "k:acquired"], keys
    assert args[2] == "+inf", f"the lease carries a finite score: {args[2]}"
    assert args[4] == "0", f"the key carries a TTL: {args[4]}"
    assert "PERSIST" in script, "nothing removes a stale TTL from the key"
    assert script.count("PERSIST") >= 2, "the acquisition-time hash can expire on its own"


def test_the_timer_is_refused_in_production_however_the_config_was_built():
    """One environment variable is not the separate review that trading the
    guarantee away would require. The worker builds its coordinator through
    `resolve_coordinator` and never runs `validate_production_config`, so the
    refusal lives on that path -- and in production config validation too."""
    from backend.production_config import validate

    env = {"ENVIRONMENT": "production",
           "UPSTASH_REDIS_REST_URL": "https://example.invalid",
           "UPSTASH_REDIS_REST_TOKEN": "unused-in-this-test",
           "MILO_PROVIDER_ABANDONED_LEASE_RECLAIM_SECONDS": "7200"}
    with pytest.raises(ValueError, match="forbidden in production"):
        resolve_coordinator(env=env)
    with pytest.raises(ValueError, match="forbidden in production"):
        resolve_coordinator(QuotaConfig(abandoned_lease_reclaim_seconds=7200.0), env=env)
    # Outside production the opt-in stays what it is: explicit and reported.
    assert resolve_coordinator(env={**env, "ENVIRONMENT": "local"}).config.reclaims_abandoned_leases
    report = validate({**env, "MILO_PROVIDER_ABANDONED_LEASE_RECLAIM_SECONDS": "7200"})
    assert any(getattr(item, "code", item) == "TIMED_LEASE_RECLAIM_IN_PRODUCTION"
               or "TIMED_LEASE_RECLAIM_IN_PRODUCTION" in str(item)
               for item in report.errors)


# =============================================================================
# 5. the ownership probe cannot extend a lease
# =============================================================================

def test_the_probe_has_no_way_to_extend_a_lease():
    """Structural, not a convention: there is no TTL argument to extend with.

    A probe that could re-stamp a lease could push a slot past the life of the
    process holding it, turning a wedged worker into a permanent leak.
    """
    import inspect

    from backend.provider_quota import QuotaBackend

    for target in (MemoryQuotaBackend.verify_concurrency, QuotaBackend.verify_concurrency):
        params = set(inspect.signature(target).parameters)
        assert "ttl_ms" not in params, target
    assert not hasattr(MemoryQuotaBackend, "heartbeat_concurrency")


def test_the_upstash_probe_script_performs_no_write():
    from backend import provider_quota

    script = provider_quota._LUA_VERIFY
    for write in ("ZADD", "PEXPIRE", "ZREM", "SET", "DEL"):
        assert write not in script, f"the ownership probe issues a {write}"


def test_probing_never_shortens_or_extends_a_held_lease():
    """Read-only in both directions.

    Under the default there is no expiry to extend, so the thing to pin is
    that probing changes nothing at all: the lease stays ours however often it
    is asked about, and stays HELD against everyone else.
    """
    clock = Clock()
    backend = MemoryQuotaBackend()
    config = QuotaConfig(max_concurrency=1, lease_ttl_seconds=2.0,
                         worker_max_lifetime_seconds=3.0)
    coordinator = ProviderQuotaCoordinator(backend, config, clock=clock)
    other = ProviderQuotaCoordinator(backend, config, clock=clock)

    lease = coordinator.try_acquire_inference()
    assert lease is not None
    for _ in range(10):
        clock.now += 600.0
        assert lease.verify_ownership() is True
        assert other.try_acquire_inference() is None

    # Under an opt-in timer the probe still cannot push the expiry out.
    timed = QuotaConfig(max_concurrency=1, worker_max_lifetime_seconds=3.0,
                        abandoned_lease_reclaim_seconds=3.75)
    clock2 = Clock()
    backend2 = MemoryQuotaBackend()
    owner = ProviderQuotaCoordinator(backend2, timed, clock=clock2)
    rival = ProviderQuotaCoordinator(backend2, timed, clock=clock2)
    held = owner.try_acquire_inference()
    for _ in range(5):
        clock2.now += 0.5
        assert held.verify_ownership() is True
    clock2.now = 10_000.0 + 3.76
    assert held.verify_ownership() is False
    assert rival.try_acquire_inference() is not None, (
        "probing extended the lease past the timer it was stamped with")


# =============================================================================
# 6. the fast path stays fast
# =============================================================================

def test_a_proven_finished_request_returns_its_slot_immediately():
    """The horizon must cost nothing on the path that actually runs."""
    backend = MemoryQuotaBackend()
    config = QuotaConfig(max_concurrency=1, lease_ttl_seconds=2.0,
                         worker_max_lifetime_seconds=3.0)
    coordinator = ProviderQuotaCoordinator(backend, config)
    scheduler = scheduler_for(coordinator)

    started = time.monotonic()
    for _ in range(20):
        assert scheduler.execute(lambda: "ok", estimated_tokens=10,
                                 reserved_tokens=10) == "ok"
    elapsed = time.monotonic() - started
    assert elapsed < 1.0, (
        f"20 proven-finished calls through a ceiling of ONE took {elapsed:.2f}s; "
        "the horizon is being paid on the success path")


def test_a_provider_429_releases_at_once_so_backpressure_still_works():
    """Quarantining an ordinary 429 would stall a run for over an hour."""
    backend = MemoryQuotaBackend()
    config = QuotaConfig(max_concurrency=1, lease_ttl_seconds=2.0,
                         worker_max_lifetime_seconds=3.0)
    clock = Clock()
    coordinator = ProviderQuotaCoordinator(backend, config, clock=clock)
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


# =============================================================================
# 7. retry semantics under the new settlement
# =============================================================================

def test_every_retry_settles_before_it_re_admits_and_takes_a_new_lease():
    """One attempt, one admission, one lease -- in that order.

    A retry that re-admitted before settling would hold two permits for one
    request; a retry that reused the lease would make a second real provider
    attempt invisible to the organization ceiling.
    """
    events: list[tuple[str, str]] = []
    backend = MemoryQuotaBackend()
    config = QuotaConfig(max_concurrency=1, lease_ttl_seconds=2.0,
                         worker_max_lifetime_seconds=3.0)

    class Recording(ProviderQuotaCoordinator):
        def try_acquire_inference(self):
            lease = super().try_acquire_inference()
            if lease is not None:
                events.append(("acquire", lease.lease_id))
            return lease

        def try_admit_request(self, reserved_tokens):
            events.append(("admit", str(reserved_tokens)))
            return super().try_admit_request(reserved_tokens)

        def release_inference(self, lease_id):
            events.append(("release", lease_id))
            return super().release_inference(lease_id)

    clock = Clock()
    coordinator = Recording(backend, config, clock=clock)
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

    acquired = [lease for kind, lease in events if kind == "acquire"]
    assert len(acquired) == 3, "a retried attempt reused a permit"
    assert len(set(acquired)) == 3, "two attempts shared one lease id"
    # Every attempt was admitted against RPM/TPM, not just the first.
    assert len([1 for kind, _ in events if kind == "admit"]) == 3
    kinds = [kind for kind, _ in events]
    for index in range(1, 3):
        settled_before = kinds.index("acquire", kinds.index("acquire") + 1)
        assert "release" in kinds[:settled_before], (
            "a retry took a second permit before settling the first")
        break


def test_the_sdk_never_retries_behind_the_settlement():
    """A hidden SDK retry would be a second real attempt on one lease.

    The engines no longer construct provider clients at all -- the one
    provider authority does, alongside the budget factory -- so those are the
    two places this has to hold, and a THIRD construction anywhere is itself
    the defect (see the sweep below).
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
            retries = {kw.arg: kw.value for kw in call.keywords}.get("max_retries")
            assert isinstance(retries, ast.Constant) and retries.value == 0, (
                f"{module.__name__} allows SDK retries behind the lease")


def test_no_engine_constructs_a_provider_client_of_its_own():
    """The sweep that makes the assertion above exhaustive.

    V1 used to carry its own fallback construction, "for local and test
    runs", with its own copy of the retry and timeout settings -- which is
    exactly the kind of copy that survives a later change to the real one.
    """
    import ast
    import inspect

    from backend.engines.swarm_v2 import model_gateway
    from backend.engines.vehicle_catalog_v1 import core as v1_core
    from backend.engines.vehicle_catalog_v1 import engine as v1_engine

    for module in (v1_core, v1_engine, model_gateway):
        tree = ast.parse(inspect.getsource(module))
        assert not [node for node in ast.walk(tree)
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "OpenAI"], (
            f"{module.__name__} builds a provider client of its own again")


def test_a_budget_refusal_never_quarantines_a_slot_it_did_not_use():
    """The guarded client sits BETWEEN the scheduler and the SDK.

    It can refuse before the request is sent (`open_call`) and after the
    response has been fully read (`settle_call`). Neither leaves a request in
    an unknown state, so neither may hold an organization slot for 75 minutes
    -- budget exhaustion is an ordinary terminal path, not an incident.
    """
    from backend.budget import BudgetConfig, BudgetExceeded, BudgetTracker, _GuardedCompletions

    class Inner:
        def create(self, **_kwargs):
            raise AssertionError("open_call should have refused first")

    tracker = BudgetTracker(BudgetConfig(max_model_calls_per_run=0))
    guarded = _GuardedCompletions(Inner(), tracker)
    with pytest.raises(BudgetExceeded) as refused:
        guarded.create(model="kimi", messages=[{"content": "hello"}], max_tokens=16)

    proven, _reason = request_completion_is_proven(refused.value)
    assert proven is True, (
        "a budget refusal raised before anything was sent was treated as an "
        "unknown request outcome")


def test_the_guarded_client_is_the_only_thing_between_execute_and_the_sdk():
    """The paid call is ONE lambda around `create`, in ONE place.

    Which is what makes the settlement taxonomy exhaustive: an exception
    escaping that lambda came from the guarded client or the SDK, never from
    MILO's own post-processing of a response.

    It used to be one site per engine -- two lambdas that happened to be
    written the same way. It is now one site for both, in the adapter.
    """
    import ast
    import inspect

    from backend import provider_authority
    from backend.engines.swarm_v2 import model_gateway
    from backend.engines.vehicle_catalog_v1 import core as v1_core

    tree = ast.parse(inspect.getsource(provider_authority))
    creates = [node for node in ast.walk(tree)
               if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
               and node.func.attr == "create"]
    assert len(creates) == 1, f"expected exactly one provider call site, found {len(creates)}"
    assert ast.unparse(creates[0]).endswith("chat.completions.create(**payload)")

    # What `execute` is handed is the function that holds that one call. It
    # is no longer a bare lambda, because a search reservation has to be taken
    # and settled around EACH attempt -- so the rule the bare lambda used to
    # enforce is enforced directly instead: everything in the callable that is
    # not the provider call declares which side of the request it ran on, and
    # the two behavioural tests below prove it.
    executes = [node for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "execute"]
    assert len(executes) == 1, f"expected exactly one execute call, found {len(executes)}"
    assert ast.unparse(executes[0].args[0]) == "attempt"

    # And neither engine has one of its own left to drift.
    for module in (model_gateway, v1_core):
        assert "chat.completions.create" not in inspect.getsource(module), (
            f"{module.__name__} calls the provider directly again")


def test_a_search_settlement_failure_does_not_change_what_is_known():
    """Accounting must not turn a proven answer into an unknown outcome.

    The adapter settles a search reservation inside the very callable the
    scheduler settles the organization permit on. If that settlement refuses
    -- a tripped search or cost ceiling -- its exception REPLACES whatever
    the request really was, and an unmarked replacement would hold a shared
    slot indefinitely over a request the provider demonstrably answered.
    """
    from types import SimpleNamespace

    from backend.budget import BudgetConfig, BudgetExceeded, BudgetTracker
    from backend.provider_authority import BUILTIN_WEB_SEARCH, ProviderAdapter
    from backend.provider_quota import (MemoryQuotaBackend, ProviderQuotaCoordinator,
                                        QuotaConfig)
    from backend.provider_scheduler import ProviderLimitsConfig, ProviderScheduler

    def searching_response(count):
        calls = [SimpleNamespace(id=str(i), function=SimpleNamespace(
            name=BUILTIN_WEB_SEARCH, arguments="{}")) for i in range(count)]
        return SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            choices=[SimpleNamespace(finish_reason="tool_calls", message=SimpleNamespace(
                content="", tool_calls=calls))])

    class Client:
        def __init__(self, response):
            self.response = response
            self.chat = SimpleNamespace(completions=self)

        def create(self, **kwargs):
            return self.response

    backend = MemoryQuotaBackend()
    coordinator = ProviderQuotaCoordinator(
        backend, QuotaConfig(max_concurrency=1, max_rpm=10, max_tpm=1_000_000))
    # A per-request maximum of 2 against a response that performs 5: the
    # response is read to completion, and THEN settlement refuses.
    tracker = BudgetTracker(
        BudgetConfig(estimated_cost_per_call=0.0, max_model_calls_per_run=10,
                     max_search_invocations_per_run=50,
                     max_builtin_searches_per_request=2),
        kill_switch=lambda: True)
    adapter = ProviderAdapter(
        ProviderScheduler(ProviderLimitsConfig(rpm_limit=None, tpm_limit=None),
                          coordinator=coordinator),
        tracker=tracker)
    request = {"model": "m", "messages": [{"role": "user", "content": "hi"}],
               "max_tokens": 100,
               "tools": [{"type": "builtin_function",
                          "function": {"name": BUILTIN_WEB_SEARCH}}]}

    with pytest.raises(BudgetExceeded):
        adapter.chat(request, client=Client(searching_response(5)))

    lease = coordinator.try_acquire_inference()
    assert lease is not None, (
        "an accounting refusal after a completed response quarantined the "
        "organization permit")
    lease.release()


def test_a_settlement_failure_does_not_change_what_is_known_about_a_429():
    """Accounting must not turn a provider answer into an unknown outcome.

    The guarded client settles its budget reservation inside the handler for a
    provider error. If that settlement refuses, its exception REPLACES the
    429 -- and an unmarked replacement would hold a shared slot for the whole
    indefinitely over a request the provider demonstrably answered.
    """
    from backend.budget import BudgetConfig, BudgetTracker, _GuardedCompletions

    class Inner:
        def create(self, **_kwargs):
            raise StructuralRateLimit()

    def refuse_to_settle(*_a, **_k):
        raise RuntimeError("the daily settler is down")

    tracker = BudgetTracker(BudgetConfig(max_model_calls_per_run=10))
    tracker.daily_settler = refuse_to_settle
    guarded = _GuardedCompletions(Inner(), tracker)

    with pytest.raises(BaseException) as raised:
        guarded.create(model="kimi", messages=[{"content": "hi"}], max_tokens=16)

    assert request_completion_is_proven(raised.value)[0] is True, (
        "a failed settlement erased the fact that the provider had answered")


def test_a_pause_key_carries_a_duration_not_an_absolute_deadline():
    """PX takes a DURATION; the absolute deadline was ~55,000 years of TTL.

    Nothing malfunctioned -- `pause_remaining` computed zero correctly once
    the moment passed -- so the only symptom was pause keys that never left
    the shared store. A limiter that quietly accumulates keys in the store it
    depends on is worth not shipping.
    """
    from backend.provider_quota import UpstashQuotaBackend

    calls: list[list[str]] = []
    backend = UpstashQuotaBackend(
        "https://example.invalid", "unused",
        http_post=lambda _url, body: calls.append(body) or {"result": 1})

    now_ms = 1_700_000_000_000
    backend.set_pause("milo:pq:test:pause:inference", now_ms + 30_000, now_ms)

    px = int(calls[0][-1])
    assert px == 30_000, f"PX was {px}, not the 30s pause duration"
    assert px < now_ms, "PX is an absolute timestamp, so the key outlives the pause"


def test_the_profile_states_the_effective_provider_parallelism():
    """A queueing width of 4 is not four simultaneous provider calls."""
    from backend.provider_scheduler import ProviderLimitsConfig
    from backend.tier2_profile import tier2_first_run_profile

    engines = tier2_first_run_profile()["active_profile"]["engine_active_concurrency"]
    assert engines["effective_simultaneous_provider_calls"] == (
        ProviderLimitsConfig().max_concurrency)
    assert (engines["effective_simultaneous_provider_calls"]
            < engines["vehicle_catalog_v1_technical_parallelism"]), (
        "the profile no longer distinguishes queueing width from provider "
        "concurrency, so the number can be read as a throughput estimate")


def test_the_profile_says_the_search_limiter_now_guards_the_v1_path():
    """The inverse of what this test asserted before the mediated path.

    V1's search used to happen INSIDE a chat call, through the provider's
    builtin tool, so the standalone QPS buckets guarded nothing any engine
    used and the profile said so. V1 now offers MILO's own function tool and
    performs each admitted search against the standalone endpoints, so those
    buckets pace the production path -- and the profile has to say THAT, or it
    is a document about a system that no longer exists.
    """
    import inspect

    from backend.engines.vehicle_catalog_v1 import core as v1_core
    from backend.standalone_search import request_offers_provider_executed_search
    from backend.tier2_profile import tier2_first_run_profile

    # V1 no longer BUILDS a provider-executed tool. The check is structural
    # rather than a text sweep: the module documents at length why the builtin
    # was removed, and prose naming a thing is not the same as code emitting
    # it. `tests/test_standalone_search.py` proves the same property from the
    # requests V1 really sends.
    assert '"builtin_function"' not in inspect.getsource(v1_core)
    assert not request_offers_provider_executed_search(
        {"tools": v1_core.WEB_SEARCH_TOOL})

    for endpoint in tier2_first_run_profile()["web_search_qps"].values():
        # Still true, and still worth saying: these buckets never paced the
        # builtin, which is why it could not be admitted one search at a time.
        assert endpoint["guards_the_builtin_web_search_path"] is False
        assert endpoint["builtin_web_search_offered_by_a_production_engine"] is False
        # And now the part that changed.
        assert endpoint["called_by_a_production_engine_today"] is True
        assert endpoint["guards_the_v1_production_search_path"] is True
        assert endpoint["run_volume_bound_admitted_before_execution"] is True

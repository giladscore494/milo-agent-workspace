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

from backend.provider_quota import (WORKER_MAX_LIFETIME_SECONDS, MemoryQuotaBackend,
                                    ProviderQuotaCoordinator, ProviderQuotaUnavailable,
                                    QuotaConfig, assert_reclaim_horizon_safe,
                                    lease_safety_margin, reclaim_horizon,
                                    resolve_coordinator)
from backend.provider_scheduler import (ProviderBackpressureExceeded, ProviderLimitsConfig,
                                        ProviderScheduler, request_completion_is_proven)
from backend.provider_transport import (ProviderRequestDeadlineExceeded,
                                        build_deadline_http_client)

REPO = Path(__file__).resolve().parents[1]

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


class RequestScaleReclaim(ProviderQuotaCoordinator):
    """The other half of the old design: reclaim on a REQUEST timescale.

    `try_acquire_inference` stamped the lease with `lease_ttl_seconds`, so a
    holder that never settled -- a killed process -- had its slot reclaimed
    while a 600s-capable request could still be running. Passing that same
    value here reproduces it, scaled to the test's seconds.
    """

    def _reclaim_horizon_ms(self):
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


def run_race(server, coordinator_class, probe):
    """A's request outlives A's patience; B tries to take the freed slot."""
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
        issue("b", scheduler_for(worker_b))

    threads = [threading.Thread(target=issue,
                                args=("a", scheduler_for(worker_a), a_inside)),
               threading.Thread(target=b_thread)]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]
    assert not any(t.is_alive() for t in threads), "a worker never finished"
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
    (None, True),                                             # the call returned
    (RuntimeError("Error code: 429 rate_limit_reached_error"), True),
    (TimeoutError("read timed out"), False),
    (RuntimeError("something nobody enumerated"), False),
    (ProviderRequestDeadlineExceeded(5.0, 5.1), False),
])
def test_only_a_positive_proof_releases_a_slot(exc, expected):
    """The default is NO. An outcome nobody thought about holds the slot."""
    assert request_completion_is_proven(exc)[0] is expected


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
# 3. crash recovery: the ONLY thing that reclaims an unreleased slot
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


def test_a_crashed_worker_is_neither_reclaimed_early_nor_leaked_forever():
    """A process SIGKILLed mid-request settles nothing at all.

    Which is the design: quarantine is the absence of an action, so a worker
    that cannot run any code still holds its slot. Recovery is the horizon,
    and only the horizon.
    """
    clock = Clock()
    backend = MemoryQuotaBackend()
    config = QuotaConfig(max_concurrency=1, lease_ttl_seconds=2.0,
                         worker_max_lifetime_seconds=3.0)
    horizon = config.reclaim_horizon_seconds        # 3.75s
    assert horizon > config.lease_ttl_seconds, "the test would prove nothing"

    dead = ProviderQuotaCoordinator(backend, config, clock=clock)
    survivor = ProviderQuotaCoordinator(backend, config, clock=clock)

    assert dead.try_acquire_inference() is not None
    # ...and now the process dies. Nothing releases, nothing quarantines.

    # Immediately: held.
    assert survivor.try_acquire_inference() is None

    # Past the NOMINAL request window, which used to be the reclaim trigger.
    clock.now += config.lease_ttl_seconds + 0.5
    assert survivor.try_acquire_inference() is None, (
        "the slot was reclaimed on a request timescale; the dead worker's "
        "process could still have been alive with the request in flight")

    # Just before the horizon: still held.
    clock.now = 10_000.0 + horizon - 0.01
    assert survivor.try_acquire_inference() is None

    # Past it: recovered, so a crash does not strand capacity for good.
    clock.now = 10_000.0 + horizon + 0.01
    recovered = survivor.try_acquire_inference()
    assert recovered is not None, "a crashed worker's slot was leaked forever"
    recovered.release()


def test_the_superseded_reclaim_lets_a_crashed_worker_be_overtaken(provider):
    """The negative control for crash recovery, over the real transport.

    `RequestScaleReclaim` stamps the lease with the request-sized window, as
    PR #102 did. Worker A is killed mid-request -- modelled by settling
    nothing -- and B waits out that window. The provider is still working, so
    B entering is two real concurrent requests.
    """
    worker_a, worker_b = two_workers(RequestScaleReclaim)
    assert worker_a.try_acquire_inference() is not None   # A holds it, then dies

    def a_request():
        with build_deadline_http_client(DEADLINE) as client:
            try:
                client.post(provider.url, json={})
            except Exception:  # noqa: BLE001 - A is the abandoned request
                pass

    threading.Thread(target=a_request, daemon=True).start()
    while provider.served < 1:
        time.sleep(0.02)

    # Wait out the request-sized window the superseded design reclaimed on.
    time.sleep(worker_a.config.lease_ttl_seconds + 0.3)
    stolen = worker_b.try_acquire_inference()
    assert stolen is not None, (
        "the superseded reclaim did not free the slot, so this control cannot "
        "detect the failure the real design is certified against")

    with build_deadline_http_client(DEADLINE) as client:
        try:
            client.post(provider.url, json={})
        except Exception:  # noqa: BLE001 - the measurement is server-side
            pass
    assert provider.peak == 2, (
        "the known-bad reclaim did not produce concurrent provider requests")


# =============================================================================
# 4. the horizon is DERIVED, and cannot be configured away
# =============================================================================

def test_the_worker_lifetime_matches_the_deployed_cloud_run_contract():
    """The number is read off the deployment, not chosen.

    If a future deployment lengthens the job's task timeout without moving
    this constant, the horizon silently stops covering the process -- so the
    constant is tied to the artifact it was derived from.
    """
    script = (REPO / "scripts/deploy/cloud-run.sh").read_text()
    timeouts = {int(v) for v in re.findall(r"--task-timeout[= ](\d+)", script)}
    assert timeouts, "the worker job no longer declares a task timeout"
    assert max(timeouts) == WORKER_MAX_LIFETIME_SECONDS, (
        f"the deployed Cloud Run task timeout is {max(timeouts)}s but the "
        f"reclaim horizon is derived from {WORKER_MAX_LIFETIME_SECONDS:g}s")


def test_the_release_plan_pins_the_same_task_timeout():
    plan = (REPO / "scripts/release/generate-deployment-plan.sh").read_text()
    assert f"--task-timeout {int(WORKER_MAX_LIFETIME_SECONDS)}" in plan


def test_the_production_horizon_is_the_lifetime_plus_its_margin():
    config = QuotaConfig()
    assert config.worker_max_lifetime_seconds == 3600.0
    assert lease_safety_margin(3600.0) == 900.0
    assert config.reclaim_horizon_seconds == 4500.0
    assert config.reclaim_horizon_seconds == reclaim_horizon(3600.0)


def test_the_horizon_is_not_the_run_duration_budget():
    """A budget MILO checks is not a guarantee the process is gone.

    `MILO_MAX_RUN_DURATION_SECONDS` is 1800s and is enforced cooperatively
    inside the worker, so a wedged process can sail past it. Deriving the
    horizon from it would be deriving a platform guarantee from a MILO
    intention.
    """
    assert QuotaConfig().reclaim_horizon_seconds > 1800.0


@pytest.mark.parametrize("lifetime", ["120", "1800", "3599"])
def test_an_environment_cannot_shorten_the_worker_lifetime(lifetime):
    with pytest.raises(ValueError):
        QuotaConfig.from_env({"MILO_WORKER_MAX_LIFETIME_SECONDS": lifetime})


def test_an_environment_may_declare_a_longer_lifetime():
    """Longer holds slots longer, which is the safe direction."""
    config = QuotaConfig.from_env({"MILO_WORKER_MAX_LIFETIME_SECONDS": "7200"})
    assert config.reclaim_horizon_seconds == reclaim_horizon(7200.0)
    assert config.reclaim_horizon_seconds > QuotaConfig().reclaim_horizon_seconds


@pytest.mark.parametrize("horizon,lifetime", [
    (120.0, 3600.0),      # the superseded request-scale value
    (3600.0, 3600.0),     # the lifetime with no margin at all
    (4499.0, 3600.0),     # one second short of the margin
])
def test_an_unsafe_horizon_is_refused(horizon, lifetime):
    with pytest.raises(ValueError):
        assert_reclaim_horizon_safe(horizon, lifetime)


def test_production_refuses_a_short_horizon_handed_in_directly():
    """`from_env` is not the only way a config reaches the coordinator."""
    unsafe = QuotaConfig(worker_max_lifetime_seconds=60.0)
    with pytest.raises(ValueError):
        resolve_coordinator(unsafe, env={
            "ENVIRONMENT": "production",
            "UPSTASH_REDIS_REST_URL": "https://example.invalid",
            "UPSTASH_REDIS_REST_TOKEN": "unused-in-this-test",
        })


def test_the_derived_horizon_is_safe_for_any_lifetime():
    for lifetime in (1.0, 3.0, 60.0, 900.0, 3600.0, 7200.0):
        assert_reclaim_horizon_safe(reclaim_horizon(lifetime), lifetime)


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


def test_repeated_probes_do_not_move_the_horizon():
    clock = Clock()
    backend = MemoryQuotaBackend()
    config = QuotaConfig(max_concurrency=1, lease_ttl_seconds=2.0,
                         worker_max_lifetime_seconds=3.0)
    coordinator = ProviderQuotaCoordinator(backend, config, clock=clock)
    other = ProviderQuotaCoordinator(backend, config, clock=clock)

    lease = coordinator.try_acquire_inference()
    assert lease is not None
    for _ in range(10):
        clock.now += 0.3
        assert lease.verify_ownership() is True

    clock.now = 10_000.0 + config.reclaim_horizon_seconds + 0.01
    assert lease.verify_ownership() is False
    assert other.try_acquire_inference() is not None, (
        "probing extended the lease past the horizon it was stamped with")


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
            raise RuntimeError("Error code: 429 rate_limit_reached_error")
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
            raise RuntimeError("Error code: 429 rate_limit_reached_error")
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
    """A hidden SDK retry would be a second real attempt on one lease."""
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
            retries = {kw.arg: kw.value for kw in call.keywords}.get("max_retries")
            assert isinstance(retries, ast.Constant) and retries.value == 0, (
                f"{module.__name__} allows SDK retries behind the lease")


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
    """Every paid call, in both engines, is one lambda around `create`.

    Which is what makes the settlement taxonomy exhaustive: an exception
    escaping that lambda came from the guarded client or the SDK, never from
    MILO's own post-processing of a response.
    """
    import ast
    import inspect

    from backend.engines.swarm_v2 import model_gateway
    from backend.engines.vehicle_catalog_v1 import core as v1_core

    sites = 0
    for module in (model_gateway, v1_core):
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "execute"):
                continue
            guarded = node.args[0]
            assert isinstance(guarded, ast.Lambda), (
                f"{module.__name__} passes something other than a bare lambda")
            assert isinstance(guarded.body, ast.Call), module.__name__
            assert ast.unparse(guarded.body).endswith("chat.completions.create(**request)") \
                or ast.unparse(guarded.body).endswith("chat.completions.create(**kwargs)"), (
                    f"{module.__name__} wraps more than the provider call")
            sites += 1
    assert sites == 2, f"expected one guarded call site per engine, found {sites}"


def test_a_settlement_failure_does_not_change_what_is_known_about_a_429():
    """Accounting must not turn a provider answer into an unknown outcome.

    The guarded client settles its budget reservation inside the handler for a
    provider error. If that settlement refuses, its exception REPLACES the
    429 -- and an unmarked replacement would hold a shared slot for the whole
    crash-recovery horizon over a request the provider demonstrably answered.
    """
    from backend.budget import BudgetConfig, BudgetTracker, _GuardedCompletions

    class Inner:
        def create(self, **_kwargs):
            raise RuntimeError("Error code: 429 rate_limit_reached_error")

    def refuse_to_settle(*_a, **_k):
        raise RuntimeError("the daily settler is down")

    tracker = BudgetTracker(BudgetConfig(max_model_calls_per_run=10))
    tracker.daily_settler = refuse_to_settle
    guarded = _GuardedCompletions(Inner(), tracker)

    with pytest.raises(BaseException) as raised:
        guarded.create(model="kimi", messages=[{"content": "hi"}], max_tokens=16)

    assert request_completion_is_proven(raised.value)[0] is True, (
        "a failed settlement erased the fact that the provider had answered")

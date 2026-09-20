"""The permit/request invariant, proven against the REAL transport.

Review of PR #102 established two things, and both were correct:

1. ``httpx.Timeout(read=...)`` bounds the wait for a *chunk*, not the duration
   of a request. A response that keeps producing bytes never trips it. On
   loopback, a server emitting one chunk every 0.2s ran for **30.1s** under a
   1.5s read timeout, and stopped only because the SERVER gave up.
2. The previous race regression proved nothing about that, because its
   simulated provider imposed its own wall-clock deadline -- it baked in the
   property it was supposed to demonstrate.

So these tests use a real ``httpx`` client, a real loopback HTTP server that
continuously produces chunks past the nominal deadline, and the real
``backend.provider_transport``. Concurrency is measured **server-side**: the
number of requests actually being served at once is the ground truth, not
anything the client believes.

Nothing here leaves the loopback interface and no provider is called.
"""

from __future__ import annotations

import http.server
import threading
import time

import pytest

from backend.provider_quota import (MemoryQuotaBackend, ProviderQuotaCoordinator,
                                    ProviderQuotaUnavailable, QuotaConfig,
                                    default_request_deadline)
from backend.provider_scheduler import ProviderLimitsConfig, ProviderScheduler
from backend.provider_transport import (ProviderRequestDeadlineExceeded,
                                        build_deadline_http_client)

TTL = 2.0
DEADLINE = default_request_deadline(TTL)     # 1.5s
#: Longer than the deadline, so a client that is NOT bounded keeps running.
TRICKLE_SECONDS = 6.0
#: Comfortably faster than any inactivity timeout under test.
CHUNK_INTERVAL = 0.05


def _peer_has_gone(connection) -> bool:
    """Has the client closed its end? A closed socket reads EOF immediately."""
    import select
    import socket as socket_module

    try:
        readable, _, _ = select.select([connection], [], [], 0)
        if not readable:
            return False
        return connection.recv(1, socket_module.MSG_PEEK) == b""
    except OSError:
        return True


class TrickleServer:
    """A server that is never silent, and counts who is being served.

    Two quantities, and the distinction matters:

    * ``peak`` -- the most handlers alive at once. A momentary 2 is NOT
      evidence of anything: when MILO hangs up, the server learns of it on its
      next poll, so a brief tail is this server's detection latency.
    * ``overlap_seconds`` -- how long two were alive TOGETHER. Sustained
      overlap is the real failure. Measured: ~0.003s with the deadline
      transport against ~4.0s with an ordinary client.
    """

    def __init__(self, trickle_seconds: float = TRICKLE_SECONDS):
        self.peak = 0
        self.served = 0
        #: Seconds during which TWO requests were being served at once.
        #: A momentary count of 2 is uninformative: when MILO hangs up, the
        #: server cannot know instantly, so a brief tail is this server's
        #: detection latency rather than MILO's behaviour. Sustained overlap
        #: is the real failure, and duration is what distinguishes them.
        self.overlap_seconds = 0.0
        self._active = 0
        self._overlap_since = None
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
                    # One byte at a time, forever: no gap ever reaches the
                    # read timeout, so only a TOTAL deadline can stop this.
                    while time.monotonic() - started < trickle_seconds:
                        if _peer_has_gone(self.connection):
                            # Stop counting this request as being served the
                            # moment the client hangs up. Without the explicit
                            # check, tiny writes sit in the kernel buffer and
                            # the handler lingers for seconds after the client
                            # is gone -- which would be measuring this test
                            # server's write buffering, not MILO's behaviour.
                            break
                        self.wfile.write(b"1\r\n \r\n")
                        self.wfile.flush()
                        time.sleep(CHUNK_INTERVAL)
                except Exception:  # noqa: BLE001 - the client hanging up is the point
                    pass
                finally:
                    outer._exit()

            def log_message(self, *args):
                pass

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/v1/chat/completions"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

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
                self.overlap_seconds += time.monotonic() - self._overlap_since
                self._overlap_since = None
            self._active -= 1

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture()
def trickle():
    with TrickleServer() as server:
        yield server


# =============================================================================
# 1. what the two mechanisms actually bound
# =============================================================================

def test_an_inactivity_timeout_does_not_bound_a_trickling_response(trickle):
    """The control. Without this, the next test proves nothing.

    This is the defect the review named: a plain client with the same numeric
    timeout runs for as long as the peer keeps talking.
    """
    import httpx

    started = time.monotonic()
    with httpx.Client(timeout=httpx.Timeout(DEADLINE, connect=DEADLINE)) as client:
        with pytest.raises(Exception):  # noqa: B017 - any end is the server's, not ours
            client.post(trickle.url, json={})
    elapsed = time.monotonic() - started
    assert elapsed > DEADLINE * 2, (
        f"the trickle ended after {elapsed:.2f}s, so this server does not "
        "actually defeat an inactivity timeout and the treatment below is vacuous")


def test_the_deadline_transport_bounds_the_same_trickling_response(trickle):
    """The treatment: a TOTAL wall-clock bound, enforced in transport."""
    started = time.monotonic()
    with build_deadline_http_client(DEADLINE) as client:
        with pytest.raises(ProviderRequestDeadlineExceeded):
            client.post(trickle.url, json={})
    elapsed = time.monotonic() - started
    assert elapsed < DEADLINE + 1.0, f"the deadline did not bound the request: {elapsed:.2f}s"


def test_the_deadline_also_covers_the_silent_header_phase(trickle):
    """A provider computing an answer sends nothing at all until it is done.

    That phase is genuinely silent, so the inactivity timeout is the right
    instrument for it -- but it must be set from the same number, or the two
    phases get bounded inconsistently.
    """
    import httpx

    class Silent(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):  # noqa: N802
            time.sleep(TRICKLE_SECONDS)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Silent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        started = time.monotonic()
        with build_deadline_http_client(DEADLINE) as client:
            with pytest.raises((httpx.ReadTimeout, httpx.TimeoutException,
                                ProviderRequestDeadlineExceeded)):
                client.post(f"http://127.0.0.1:{server.server_address[1]}/x", json={})
        assert time.monotonic() - started < DEADLINE + 1.0
    finally:
        server.shutdown()
        server.server_close()


def test_a_deadline_exceeded_is_not_mistaken_for_provider_backpressure():
    """Retrying it as though the provider asked us to wait would be wrong."""
    from backend.provider_scheduler import (classify_provider_error,
                                            is_provider_rate_limit_error)

    error = ProviderRequestDeadlineExceeded(90.0, 91.2)
    assert classify_provider_error(error) is None
    assert not is_provider_rate_limit_error(error)


# =============================================================================
# 2. the two-coordinator heartbeat-loss race, over the real transport
# =============================================================================

def coordinators(kind):
    """Two 'processes' over ONE shared store, with A's heartbeat sabotaged."""
    backend = MemoryQuotaBackend()
    config = QuotaConfig(max_concurrency=1, lease_ttl_seconds=TTL)

    class WorkerA(ProviderQuotaCoordinator):
        def heartbeat_inference(self, lease_id):
            if kind == "returns_false":
                return False
            if kind == "raises":
                raise ProviderQuotaUnavailable()
            return super().heartbeat_inference(lease_id)

    return WorkerA(backend, config), ProviderQuotaCoordinator(backend, config)


def scheduler_for(coordinator):
    return ProviderScheduler(
        ProviderLimitsConfig(max_concurrency=1, rpm_limit=None, tpm_limit=None,
                             max_backpressure_wait_seconds=30.0),
        coordinator=coordinator)


class ClientSideConcurrency:
    """How many requests MILO itself has outstanding, at once.

    This is the quantity MILO can actually guarantee. Whether a provider keeps
    computing after its client disconnects is outside any client's control and
    is why the organization ceiling is 80% of the provider's rather than 100%.
    """

    def __init__(self):
        self.peak = 0
        self._active = 0
        self._lock = threading.Lock()

    def __enter__(self):
        with self._lock:
            self._active += 1
            self.peak = max(self.peak, self._active)
        return self

    def __exit__(self, *exc):
        with self._lock:
            self._active -= 1


def run_race(server, client_factory, kind):
    """A holds a real request; B tries to enter while A is being served."""
    worker_a, worker_b = coordinators(kind)
    sched_a, sched_b = scheduler_for(worker_a), scheduler_for(worker_b)
    errors: dict[str, BaseException] = {}
    a_inside = threading.Event()
    milo_side = ClientSideConcurrency()

    def worker(label, scheduler, event=None):
        def call():
            if event is not None:
                event.set()
            with milo_side, client_factory() as client:
                return client.post(server.url, json={})
        try:
            scheduler.execute(call, estimated_tokens=10, reserved_tokens=10)
        except BaseException as exc:  # noqa: BLE001 - recorded, asserted by caller
            errors[label] = exc

    def worker_b_thread():
        # Only attempt once A is genuinely being served, so the result cannot
        # depend on which thread won the permit first.
        if not a_inside.wait(timeout=10.0):
            errors["b"] = AssertionError("worker A never reached the server")
            return
        worker("b", sched_b)

    threads = [threading.Thread(target=worker, args=("a", sched_a, a_inside)),
               threading.Thread(target=worker_b_thread)]
    [t.start() for t in threads]
    [t.join(timeout=40) for t in threads]
    assert not any(t.is_alive() for t in threads), "a worker never finished"
    return errors, milo_side


@pytest.mark.parametrize("kind", ["returns_false", "raises"])
def test_lost_ownership_cannot_produce_two_real_concurrent_requests(trickle, kind):
    """The HIGH from review, closed against the real mechanism.

    Worker A's permit renewal fails -- the coordinator disowns it, or the
    shared store is unreachable -- while A has a genuinely long-running
    request open. Worker B is waiting to enter under a ceiling of ONE.
    """
    errors, milo_side = run_race(
        trickle, lambda: build_deadline_http_client(DEADLINE), kind)

    assert trickle.served >= 1
    # What MILO owns, and the assertion that matters: never two of its own
    # requests outstanding at once, under a ceiling of one.
    assert milo_side.peak == 1, (
        f"MILO had {milo_side.peak} requests outstanding under a ceiling of 1")
    # And at the other end, no SUSTAINED overlap -- only the few milliseconds
    # between MILO closing its connection and the server noticing. Whether a
    # provider keeps computing after a client disconnects is outside any
    # client's control, which is why the ceiling is 80% and not 100%.
    assert trickle.overlap_seconds < 0.5, (
        f"two requests were served together for {trickle.overlap_seconds:.3f}s, "
        "which is sustained overlap rather than a teardown tail")
    assert isinstance(errors.get("a"), ProviderRequestDeadlineExceeded), errors


@pytest.mark.parametrize("kind", ["returns_false", "raises"])
def test_the_race_harness_detects_the_unbounded_client(trickle, kind):
    """Guard against a test that would pass however the client behaved.

    The only change is the client: an ordinary one with the same numeric
    timeout, which is what PR #102 shipped. Under it the harness MUST observe
    two requests being served at once -- if it cannot see the failure, it
    cannot be trusted to certify its absence.
    """
    import httpx

    _errors, milo_side = run_race(
        trickle,
        lambda: httpx.Client(timeout=httpx.Timeout(DEADLINE, connect=DEADLINE)),
        kind)

    assert milo_side.peak == 2, (
        "MILO did not even hold two requests open, so the harness is not "
        "exercising the failure it is meant to detect")
    assert trickle.overlap_seconds > 1.0, (
        f"only {trickle.overlap_seconds:.3f}s of overlap was observed under the "
        "known-bad client, so this harness cannot certify its absence either")


# =============================================================================
# 3. the permit is still released cleanly when the deadline fires
# =============================================================================

def test_a_deadline_releases_both_the_permit_and_the_local_slot(trickle):
    backend = MemoryQuotaBackend()
    coordinator = ProviderQuotaCoordinator(
        backend, QuotaConfig(max_concurrency=1, lease_ttl_seconds=TTL))
    scheduler = scheduler_for(coordinator)

    def call():
        with build_deadline_http_client(DEADLINE) as client:
            return client.post(trickle.url, json={})

    with pytest.raises(ProviderRequestDeadlineExceeded):
        scheduler.execute(call, estimated_tokens=10, reserved_tokens=10)

    lease = coordinator.try_acquire_inference()
    assert lease is not None, "the permit leaked after a deadline"
    lease.release()
    assert scheduler._slots.acquire(blocking=False), "the local slot leaked"
    scheduler._slots.release()


def test_the_shipped_clients_are_built_on_the_deadline_transport():
    """Both engines' constructions, checked over the parsed code.

    A `timeout=` keyword is no longer sufficient evidence: the review showed
    that an inactivity timeout does not bound a request at all.
    """
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
            assert "http_client" in keywords, (
                f"{module.__name__} builds a client with no total-deadline transport")
            assert "max_retries" in keywords


def test_the_deadline_client_carries_the_configured_deadline():
    from backend.budget import build_provider_http_client, resolved_request_deadline

    config = QuotaConfig()
    assert resolved_request_deadline() == config.request_deadline_seconds
    with build_provider_http_client() as client:
        assert client.timeout.read == config.request_deadline_seconds
        assert client.timeout.connect <= client.timeout.read

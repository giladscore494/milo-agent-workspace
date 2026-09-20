"""How long ONE provider request may keep a MILO thread, proven for real.

Review of PR #102 established two things, and both were correct:

1. ``httpx.Timeout(read=...)`` bounds the wait for a *chunk*, not the duration
   of a request. A response that keeps producing bytes never trips it. On
   loopback, a server emitting one chunk every 0.2s ran for **30.1s** under a
   1.5s read timeout, and stopped only because the SERVER gave up.
2. The race regression of the time proved nothing about that, because its
   simulated provider imposed its own wall-clock deadline -- it baked in the
   property it was supposed to demonstrate.

So these tests use a real ``httpx`` client, a real loopback HTTP server that
continuously produces chunks past the nominal deadline, and the real
``backend.provider_transport``.

SCOPE. What is proven here is a LIVENESS property: MILO stops waiting by a
known time. A later review established that this is not the organization
concurrency property and must not be mistaken for it -- a fired deadline says
nothing about whether the PROVIDER stopped. That property, and its negative
control, live in ``test_provider_concurrency_ownership.py``, measured
server-side against a provider that keeps working after its client leaves.

Nothing here leaves the loopback interface and no provider is called.
"""

from __future__ import annotations

import http.server
import threading
import time

import pytest

from backend.provider_quota import (MemoryQuotaBackend, ProviderQuotaCoordinator,
                                    QuotaConfig, default_request_deadline)
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
# 3. what a fired deadline settles, and what it deliberately does not
# =============================================================================

def test_a_deadline_frees_the_local_slot_but_keeps_the_shared_permit(trickle):
    """Two resources, two rules, and the difference is the correction.

    The process-local slot bounds THIS process's threads; the thread is gone,
    so it goes back. The organization permit stands for a request whose state
    is now unknown -- the provider may still be working -- so it is held to
    held, rather than being handed to someone else.
    """
    backend = MemoryQuotaBackend()
    coordinator = ProviderQuotaCoordinator(
        backend, QuotaConfig(max_concurrency=1, lease_ttl_seconds=TTL))
    scheduler = ProviderScheduler(
        ProviderLimitsConfig(max_concurrency=1, rpm_limit=None, tpm_limit=None,
                             max_backpressure_wait_seconds=30.0),
        coordinator=coordinator)

    def call():
        with build_deadline_http_client(DEADLINE) as client:
            return client.post(trickle.url, json={})

    with pytest.raises(ProviderRequestDeadlineExceeded):
        scheduler.execute(call, estimated_tokens=10, reserved_tokens=10)

    assert coordinator.try_acquire_inference() is None, (
        "a fired deadline handed the organization permit to the next caller, "
        "although it proves only that MILO stopped waiting")
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

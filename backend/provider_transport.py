"""A TOTAL wall-clock deadline for one provider request, enforced in transport.

Why this exists
---------------

A worker that waits forever makes no progress, so there has to be a real
ceiling on how long ONE request can keep a MILO thread. An httpx timeout is
not that ceiling.

What this is NOT is the organization-concurrency mechanism. Read the next
section carefully before relying on it for that: a fired deadline says MILO
stopped waiting, and says nothing about whether the provider stopped working.
``backend.provider_quota`` therefore treats a fired deadline as an UNPROVEN
outcome and keeps holding the permit, rather than returning it.

``httpx.Timeout(read=...)`` bounds the wait for *a chunk of data*, not the
duration of the request. A response that keeps producing bytes faster than the
read timeout never trips it, however long it goes on. Measured against a
loopback server emitting one chunk every 0.2s with ``read=1.0``::

    raised RemoteProtocolError after 20.1s   # and only because the SERVER
                                             # gave up; the client never did

So an inactivity timeout cannot establish "the request is over by T". This
transport does, by checking elapsed time against a deadline fixed when the
request starts, on every chunk the response yields.

What it guarantees, exactly
---------------------------

* No MILO thread is still awaiting this response after the deadline.
* The connection for it is closed rather than left to drain.

What it does NOT claim
----------------------

* It does not cancel the request **provider-side**. Nothing in the httpx or
  OpenAI contract offers that, and a server may keep computing after a client
  disconnects. This is the reason a fired deadline is not treated as proof of
  completion: the request's real state at that moment is UNKNOWN, and
  ``InferenceLease.quarantine`` keeps the slot rather than reusing it.
* It does not cancel from another thread. The deadline is enforced by the
  thread performing the request, on its own next read -- which is why it needs
  no cross-thread cancellation primitive to be correct. Equally, wrapping the
  call in ``future.result(timeout=)``, ``ThreadPoolExecutor``,
  ``thread.join(timeout=)`` or ``asyncio.wait_for`` would NOT add anything
  here: those return control to MILO while the socket and the provider-side
  work continue, and returning control is not termination.

How the whole lifecycle is covered
----------------------------------

A request is not one operation, it is five: acquiring a pooled connection,
connecting, writing the request, waiting for response headers, and reading
the body. httpx gives each its OWN timeout, and that is precisely how a
request escapes a total deadline while every individual timeout is honoured:
five sub-operations each allowed D seconds can run for 5D.

So the deadline is ALLOCATED, not repeated. At the start of each request this
transport computes one absolute deadline and divides the budget across the
phases so their SUM cannot exceed it, overriding whatever per-phase numbers
the client was built with (``request.extensions["timeout"]``, which is how
httpx hands timeouts to a transport). Then:

1. **Pool, connect, write.** Bounded by their allocated shares, which are
   small: waiting minutes for a TCP handshake is never useful.
2. **Waiting for response headers.** Genuinely silent -- the provider sends
   nothing while it computes -- so an inactivity timeout is the right
   instrument, and it gets whatever the earlier phases left. The clock is
   re-checked the moment headers arrive.
3. **Reading the body.** Not necessarily silent -- this is the trickle case --
   so the stream wrapper below bounds it by elapsed time against the SAME
   absolute deadline.

Together: total ≲ deadline, whatever the peer does, and no single phase can
borrow another's budget.

Per-request timing (PR-S)
-------------------------

The client's deadline is the CEILING any request may have -- the one the
organization lease window is validated against. A caller may tighten it for
the requests it makes with :func:`request_timing`: Swarm V2 sets each role's
total deadline there, plus a short inactivity window for its streamed calls,
which caps every read (header wait and chunk gaps alike). The body check runs
when a chunk arrives, so a stream that goes silent just before its deadline is
stopped by the inactivity window instead: the worst-case wall time of one
streamed request is ``total deadline + inactivity window``.
"""

from __future__ import annotations

import contextlib
import contextvars
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator


class ProviderRequestDeadlineExceeded(Exception):
    """One provider request hit its total wall-clock deadline.

    Deliberately NOT a rate-limit signal: the scheduler must not absorb it as
    backpressure and retry it as though the provider had asked us to wait.

    And deliberately NOT proof that the request is over. It means MILO gave
    up waiting; the provider may still be working.
    ``provider_scheduler.request_completion_is_proven`` classifies it as
    unproven, so the organization concurrency slot stays held.
    """

    def __init__(self, deadline_seconds: float, elapsed_seconds: float):
        super().__init__(
            f"provider request exceeded its {deadline_seconds:g}s total deadline")
        self.deadline_seconds = deadline_seconds
        self.elapsed_seconds = elapsed_seconds


#: The longest a STREAMING request may go without a single chunk -- including
#: the wait for response headers. A streamed reasoning model emits a chunk
#: every few tokens, so a minute of silence means the provider stopped
#: producing, not that it is thinking hard (PR-S, `backend.provider_streaming`).
STREAM_INACTIVITY_SECONDS = 60.0


@dataclass(frozen=True)
class RequestTiming:
    """How long ONE request may take, as the caller that built it decided.

    Read by the deadline transport on the thread that performs the request.
    It can only TIGHTEN the client's own deadline, never widen it: the
    client's deadline is the one the organization lease window was validated
    against (``assert_request_deadline_safe``). ``inactivity_seconds`` caps
    every read -- the header wait and each body chunk -- and is only set for a
    STREAMING request: a non-streaming reasoning request is legitimately
    silent until its whole answer exists.
    """

    total_deadline_seconds: float
    inactivity_seconds: float | None = None

    def __post_init__(self) -> None:
        if not float(self.total_deadline_seconds) > 0:
            raise ValueError("a request's total deadline must be positive")
        if self.inactivity_seconds is not None and not float(self.inactivity_seconds) > 0:
            raise ValueError("a request's inactivity window must be positive")


_timing: contextvars.ContextVar[RequestTiming | None] = contextvars.ContextVar(
    "milo_provider_request_timing", default=None)


@contextlib.contextmanager
def request_timing(total_deadline_seconds: float,
                   inactivity_seconds: float | None = STREAM_INACTIVITY_SECONDS,
                   ) -> Iterator[RequestTiming]:
    """Bound every provider request made on this thread inside this block."""
    timing = RequestTiming(float(total_deadline_seconds),
                           None if inactivity_seconds is None else float(inactivity_seconds))
    token = _timing.set(timing)
    try:
        yield timing
    finally:
        _timing.reset(token)


def current_request_timing() -> RequestTiming | None:
    return _timing.get()


def _import_httpx() -> Any:
    import httpx

    return httpx


#: The shares of one request's total deadline that the phases BEFORE the
#: header wait may consume, between them. They are deliberately small: the
#: silent header phase is where a provider legitimately spends time, and the
#: setup phases are where a request that is going nowhere should be abandoned
#: quickly. Each is also capped in absolute seconds, because a long deadline
#: is not a reason to wait five minutes for a TCP handshake.
_SETUP_PHASE_SHARES: tuple[tuple[str, float, float], ...] = (
    ("pool", 0.05, 5.0),
    ("connect", 0.10, 10.0),
    ("write", 0.10, 10.0),
)
#: The header wait never drops below this, however the shares fall out.
_MIN_READ_SECONDS = 0.001


def allocate_request_timeouts(deadline_seconds: float,
                              connect_timeout: float | None = None,
                              inactivity_seconds: float | None = None,
                              ) -> dict[str, float]:
    """Divide ONE absolute deadline across the phases of ONE request.

    The contract this function exists to keep is arithmetic, and a test
    asserts it directly::

        pool + connect + write + read <= deadline

    That is what makes the deadline ABSOLUTE rather than per-operation. A
    caller-supplied ``connect_timeout`` may tighten its phase, never widen it
    past its share. ``inactivity_seconds`` (a streaming request) caps ``read``,
    which httpx applies to the header wait AND to every body read -- the gap
    between two chunks.
    """
    deadline = float(deadline_seconds)
    if deadline <= 0:
        raise ValueError("provider request deadline must be positive")
    allocation: dict[str, float] = {}
    for name, share, cap in _SETUP_PHASE_SHARES:
        value = min(deadline * share, cap)
        if name == "connect" and connect_timeout is not None:
            value = min(value, float(connect_timeout))
        allocation[name] = max(_MIN_READ_SECONDS, value)
    setup = sum(allocation.values())
    read = deadline - setup
    if inactivity_seconds is not None:
        read = min(read, float(inactivity_seconds))
    allocation["read"] = max(_MIN_READ_SECONDS, read)
    return allocation


def build_deadline_transport(deadline_seconds: float, *, inner: Any = None,
                             connect_timeout: float | None = None,
                             clock: Callable[[], float] = time.monotonic) -> Any:
    """An httpx transport that enforces a TOTAL deadline per request."""
    httpx = _import_httpx()
    client_deadline = float(deadline_seconds)
    if client_deadline <= 0:
        raise ValueError("provider request deadline must be positive")
    # Validated once here so a bad connect timeout fails at build time.
    allocate_request_timeouts(client_deadline, connect_timeout)

    # Built here rather than at module scope because httpx asserts the stream
    # it is handed really is one of its own -- duck typing is rejected.
    class _DeadlineStream(httpx.SyncByteStream):
        """Checks the deadline on every chunk the response yields.

        The check happens BEFORE the chunk is handed on, so a stream that
        keeps producing data cannot walk past the deadline one chunk at a
        time -- which is the exact failure an inactivity timeout misses.
        """

        def __init__(self, wrapped: Any, deadline_at: float, started_at: float,
                     deadline: float):
            self._wrapped = wrapped
            self._deadline_at = deadline_at
            self._started_at = started_at
            self._deadline = deadline

        def __iter__(self) -> Iterator[bytes]:
            for chunk in self._wrapped:
                if clock() >= self._deadline_at:
                    # Close first: a connection still draining a response
                    # nobody is waiting for is the leak this exists to stop.
                    self.close()
                    raise ProviderRequestDeadlineExceeded(
                        self._deadline, clock() - self._started_at)
                yield chunk

        def close(self) -> None:
            closer = getattr(self._wrapped, "close", None)
            if callable(closer):
                closer()

    class DeadlineTransport(httpx.BaseTransport):
        def __init__(self) -> None:
            # retries=0: transport-level retries are as invisible to MILO's
            # accounting as the SDK-level ones, and would each get a fresh
            # deadline while the permit does not get a fresh lease.
            self._inner = inner if inner is not None else httpx.HTTPTransport(retries=0)

        def handle_request(self, request: Any) -> Any:
            started = clock()
            # PR-S: the caller's per-request timing (a Swarm V2 role's total
            # deadline and, for a stream, its inactivity window) may TIGHTEN
            # the client's deadline and never widen it. Resolved per request,
            # on the thread that performs it.
            timing = current_request_timing()
            deadline = client_deadline
            inactivity = None
            if timing is not None:
                deadline = min(client_deadline, float(timing.total_deadline_seconds))
                inactivity = timing.inactivity_seconds
            phase_timeouts = allocate_request_timeouts(deadline, connect_timeout,
                                                       inactivity)
            deadline_at = started + deadline
            # The allocation REPLACES whatever per-phase timeouts the client
            # was built with. Without this the client's numbers survive, each
            # phase gets the whole deadline, and a request that connects
            # slowly and then waits for headers runs for multiples of it --
            # every individual timeout honoured, the total deadline escaped.
            extensions = dict(getattr(request, "extensions", None) or {})
            configured = extensions.get("timeout")
            allocated = dict(phase_timeouts)
            if isinstance(configured, dict):
                # A caller may only ever be TIGHTER than the allocation.
                for name, value in configured.items():
                    if value is None or name not in allocated:
                        continue
                    try:
                        allocated[name] = min(allocated[name], float(value))
                    except (TypeError, ValueError):
                        continue
            extensions["timeout"] = allocated
            request.extensions = extensions
            response = self._inner.handle_request(request)
            elapsed = clock() - started
            if elapsed >= deadline:
                # Everything up to and including the headers consumed the
                # whole budget; there is no time left to read a body.
                response.close()
                raise ProviderRequestDeadlineExceeded(deadline, elapsed)
            response.stream = _DeadlineStream(response.stream, deadline_at, started,
                                              deadline)
            return response

        def close(self) -> None:
            closer = getattr(self._inner, "close", None)
            if callable(closer):
                closer()

    return DeadlineTransport()


def build_deadline_http_client(deadline_seconds: float, *, inner: Any = None,
                               connect_timeout: float | None = None,
                               clock: Callable[[], float] = time.monotonic) -> Any:
    """The httpx client every paid provider call is made through.

    The client's own timeouts are the OUTER bound, set from the same one
    number so nothing is configured looser than the deadline. The transport
    then allocates that number across the phases per request, so the sum of
    the sub-operations cannot exceed it either -- the client-level values are
    a ceiling on the allocation, never a second, independent budget.
    """
    httpx = _import_httpx()
    deadline = float(deadline_seconds)
    connect = min(10.0, deadline) if connect_timeout is None else float(connect_timeout)
    return httpx.Client(
        transport=build_deadline_transport(deadline, inner=inner,
                                           connect_timeout=connect_timeout,
                                           clock=clock),
        timeout=httpx.Timeout(deadline, connect=connect, read=deadline,
                              write=deadline, pool=deadline),
    )


__all__ = ["ProviderRequestDeadlineExceeded", "RequestTiming", "STREAM_INACTIVITY_SECONDS",
           "allocate_request_timeouts", "build_deadline_http_client",
           "build_deadline_transport", "current_request_timing", "request_timing"]

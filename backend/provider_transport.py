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

* No MILO thread is still awaiting this response after the deadline, ONCE
  the response headers have arrived.
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

How the two phases are covered, and how well
--------------------------------------------

1. **Reading the body.** Bounded by ELAPSED time. The stream wrapper below
   checks the deadline before handing on each chunk, so a response that keeps
   producing data cannot walk past the deadline one chunk at a time.
2. **Waiting for response headers.** Bounded only by INACTIVITY -- the
   client's ``read`` timeout -- because ``handle_request`` cannot return
   before the headers are complete, so the elapsed check below runs after that
   phase rather than during it.

The honest statement is therefore narrower than "total <= deadline":

    Once headers are in, the response is bounded by elapsed time. The header
    phase is bounded by silence, not by duration.

For the case this exists for that is enough: a provider computing an answer is
genuinely silent, so ``read`` bounds it correctly. But a peer that trickles
HEADER bytes evades it, and this was measured rather than assumed -- a server
emitting one padding header every 0.05s against a 1.5s deadline held the
caller for **6.16s**, and ``ProviderRequestDeadlineExceeded`` was raised only
when the headers finally completed. See
``test_a_trickled_header_phase_is_bounded_by_silence_not_by_elapsed_time``.

So during the header phase the deadline DETECTS an overrun; it does not
prevent one. That is a liveness limit -- a worker can block longer than the
number says -- and deliberately not a concurrency one: nothing reclaims a held
lease on a clock (see ``backend.provider_quota``), so a late detection cannot
let a second worker in. Closing that gap properly would need a bound the
transport does not have: httpx exposes no total-request deadline, and reaching
across threads to close the socket is the cross-thread cancellation this
module declines to claim.
"""

from __future__ import annotations

import time
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


def _import_httpx() -> Any:
    import httpx

    return httpx


def build_deadline_transport(deadline_seconds: float, *, inner: Any = None,
                             clock: Callable[[], float] = time.monotonic) -> Any:
    """An httpx transport that enforces a TOTAL deadline per request."""
    httpx = _import_httpx()
    deadline = float(deadline_seconds)
    if deadline <= 0:
        raise ValueError("provider request deadline must be positive")

    # Built here rather than at module scope because httpx asserts the stream
    # it is handed really is one of its own -- duck typing is rejected.
    class _DeadlineStream(httpx.SyncByteStream):
        """Checks the deadline on every chunk the response yields.

        The check happens BEFORE the chunk is handed on, so a stream that
        keeps producing data cannot walk past the deadline one chunk at a
        time -- which is the exact failure an inactivity timeout misses.
        """

        def __init__(self, wrapped: Any, deadline_at: float, started_at: float):
            self._wrapped = wrapped
            self._deadline_at = deadline_at
            self._started_at = started_at

        def __iter__(self) -> Iterator[bytes]:
            for chunk in self._wrapped:
                if clock() >= self._deadline_at:
                    # Close first: a connection still draining a response
                    # nobody is waiting for is the leak this exists to stop.
                    self.close()
                    raise ProviderRequestDeadlineExceeded(
                        deadline, clock() - self._started_at)
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
            deadline_at = started + deadline
            response = self._inner.handle_request(request)
            elapsed = clock() - started
            if elapsed >= deadline:
                # Headers alone consumed the whole budget; there is no time
                # left to read a body.
                response.close()
                raise ProviderRequestDeadlineExceeded(deadline, elapsed)
            response.stream = _DeadlineStream(response.stream, deadline_at, started)
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

    The inactivity timeouts and the total deadline are set from ONE number, so
    the silent phase and the chunked phase cannot be bounded inconsistently.
    """
    httpx = _import_httpx()
    deadline = float(deadline_seconds)
    connect = min(10.0, deadline) if connect_timeout is None else float(connect_timeout)
    return httpx.Client(
        transport=build_deadline_transport(deadline, inner=inner, clock=clock),
        timeout=httpx.Timeout(deadline, connect=connect, read=deadline,
                              write=deadline, pool=deadline),
    )


__all__ = ["ProviderRequestDeadlineExceeded", "build_deadline_http_client",
           "build_deadline_transport"]

"""A TOTAL wall-clock deadline for one provider request, enforced in transport.

Why this exists
---------------

The organization concurrency permit is only worth holding if the request it
admits cannot outlive it, so the safety argument needs a real ceiling on how
long ONE request can run. An httpx timeout is not that ceiling.

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
  disconnects. MILO's ceiling is 80% of the provider's precisely so that
  effects it cannot observe have headroom.
* It does not cancel from another thread. The deadline is enforced by the
  thread performing the request, on its own next read -- which is why it needs
  no cross-thread cancellation primitive to be correct.

How the two phases are covered
------------------------------

1. **Waiting for response headers.** Genuinely silent: the provider sends
   nothing while it computes. That is exactly the case an inactivity timeout
   bounds correctly, so the client's ``read`` timeout covers it, and this
   transport re-checks the clock the moment headers arrive.
2. **Reading the body.** Not necessarily silent -- this is the trickle case --
   so the stream wrapper below bounds it by elapsed time instead.

Together: total ≲ deadline, whatever the peer does.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Iterator


class ProviderRequestDeadlineExceeded(Exception):
    """One provider request hit its total wall-clock deadline.

    Deliberately NOT a rate-limit signal: the scheduler must not absorb it as
    backpressure and retry it as though the provider had asked us to wait.
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

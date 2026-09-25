"""A simulated Kimi chat endpoint on a SIMULATED clock (PR-S).

It sits BEHIND the real OpenAI SDK and the real deadline transport
(``backend.provider_transport``), as the inner httpx transport, so everything
between MILO and the wire is production code: request serialization, SSE
parsing, the per-request deadline allocation and the per-chunk deadline check.

It models the one property run 5145ca65 turned on: a reasoning model that is
NOT streaming is silent until its whole answer exists, while a STREAMING one
emits a chunk every few reasoning tokens. Time is a :class:`SimClock` the
provider advances as it "works", so a 160-second reasoning call runs in
milliseconds and the transport sees exactly the elapsed time it would in
production.

Non-streaming silence is modelled the way httpcore behaves: if the silent
wait exceeds the request's allocated ``read`` timeout, the clock advances by
that timeout and ``httpx.ReadTimeout`` is raised -- with no response object.

Nothing here opens a socket or calls a provider.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterator

import httpx

#: Text that only ever appears in REASONING deltas. It must never reach an
#: assembled answer, a ledger row, an event or a log line.
REASONING_SENTINEL = "REASONING-SENTINEL-must-never-persist"


class SimClock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


@dataclass
class StreamScript:
    """What ONE streamed completion does, in simulated time."""

    answer: str
    reasoning_chunks: int = 40
    #: Simulated seconds between two reasoning chunks.
    reasoning_interval: float = 4.0
    answer_chunks: int = 4
    finish_reason: str | None = "stop"
    #: "choice" -> usage on the final choice (Moonshot); "chunk" -> a trailing
    #: usage-only chunk with no choices (OpenAI include_usage); None -> none.
    usage_style: str | None = "choice"
    prompt_tokens: int = 7_000
    completion_tokens: int = 21_000
    reasoning_tokens: int = 20_000
    #: Raise this after N chunks instead of finishing (a dropped connection).
    drop_after: int | None = None
    #: Send the [DONE] terminator.
    done: bool = True
    #: After the final frame(s), go silent for the inactivity window and then
    #: time out instead of sending [DONE]: only a reader that keeps waiting
    #: past a finished, measured stream ever sees it.
    hang_after_final: bool = False
    #: Seconds of silence before the first byte when the request is NOT
    #: streaming: the whole reasoning time, then the whole answer at once.
    @property
    def silent_seconds(self) -> float:
        return self.reasoning_chunks * self.reasoning_interval


def _sse(obj: Any) -> bytes:
    return f"data: {json.dumps(obj)}\n\n".encode()


def _chunk(model: str, choices: list[dict], **extra: Any) -> dict:
    return {"id": "chatcmpl-sim", "object": "chat.completion.chunk", "created": 1,
            "model": model, "choices": choices, **extra}


def _usage(script: StreamScript) -> dict:
    return {"prompt_tokens": script.prompt_tokens,
            "completion_tokens": script.completion_tokens,
            "total_tokens": script.prompt_tokens + script.completion_tokens,
            "completion_tokens_details": {"reasoning_tokens": script.reasoning_tokens}}


class _SimStream(httpx.SyncByteStream):
    def __init__(self, script: StreamScript, model: str, clock: SimClock) -> None:
        self._script, self._model, self._clock = script, model, clock
        self.closed = False
        self.read_past_final = False

    def __iter__(self) -> Iterator[bytes]:
        script, model = self._script, self._model
        sent = 0

        def emit(obj: Any) -> bytes:
            nonlocal sent
            if script.drop_after is not None and sent >= script.drop_after:
                raise httpx.RemoteProtocolError("peer closed connection without sending complete message body")
            sent += 1
            return _sse(obj)

        for index in range(script.reasoning_chunks):
            self._clock.advance(script.reasoning_interval)
            yield emit(_chunk(model, [{"index": 0, "delta": {
                "role": "assistant", "content": None,
                "reasoning_content": f"{REASONING_SENTINEL} step {index} "},
                "finish_reason": None}]))
        pieces = _split(script.answer, script.answer_chunks)
        for piece in pieces:
            self._clock.advance(0.5)
            yield emit(_chunk(model, [{"index": 0, "delta": {"content": piece},
                                       "finish_reason": None}]))
        final_choice: dict = {"index": 0, "delta": {}, "finish_reason": script.finish_reason}
        if script.usage_style == "choice":
            final_choice["usage"] = _usage(script)
        yield emit(_chunk(model, [final_choice]))
        if script.usage_style == "chunk":
            yield emit(_chunk(model, [], usage=_usage(script)))
        if script.hang_after_final:
            self.read_past_final = True
            self._clock.advance(60.0)
            raise httpx.ReadTimeout("timed out")
        if script.done:
            yield b"data: [DONE]\n\n"

    def close(self) -> None:
        self.closed = True


def _split(text: str, parts: int) -> list[str]:
    parts = max(1, parts)
    size = max(1, -(-len(text) // parts))
    return [text[i:i + size] for i in range(0, len(text), size)]


@dataclass
class SimulatedKimi(httpx.BaseTransport):
    """The inner transport. One script per request, in order."""

    clock: SimClock
    scripts: list[StreamScript]
    #: An HTTP error status to answer with instead of a completion.
    status: int | None = None
    requests: list[dict] = field(default_factory=list)
    timeouts: list[dict] = field(default_factory=list)
    streams: list[_SimStream] = field(default_factory=list)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read() or b"{}")
        self.requests.append(body)
        allocated = dict((request.extensions or {}).get("timeout") or {})
        self.timeouts.append(allocated)
        model = body.get("model", "kimi-k3")
        if self.status is not None:
            return httpx.Response(self.status, request=request, json={
                "error": {"message": "PROVIDER-BODY-SENTINEL internal detail",
                          "type": "server_error"}})
        script = self.scripts.pop(0)
        if not body.get("stream"):
            # A non-streaming reasoning model says NOTHING until it is done.
            read = float(allocated.get("read") or 0)
            if script.silent_seconds > read:
                self.clock.advance(read)
                raise httpx.ReadTimeout("timed out", request=request)
            self.clock.advance(script.silent_seconds)
            completion = {"id": "chatcmpl-sim", "object": "chat.completion", "created": 1,
                          "model": model, "choices": [{"index": 0, "finish_reason": script.finish_reason,
                                                       "message": {"role": "assistant", "content": script.answer}}],
                          "usage": _usage(script)}
            return httpx.Response(200, request=request, json=completion)
        stream = _SimStream(script, model, self.clock)
        self.streams.append(stream)
        return httpx.Response(200, request=request, stream=stream,
                              headers={"content-type": "text/event-stream"})


__all__ = ["REASONING_SENTINEL", "SimClock", "SimulatedKimi", "StreamScript"]

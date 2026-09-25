"""Reasoning-safe transport: every Swarm V2 provider call is STREAMED (PR-S).

Why this exists
---------------

Run 5145ca65's Commander planning call (kimi-k3, effort ``high``, a 32,000
token cap, ``json_object``) was sent NON-streaming. A reasoning model that is
not streaming is SILENT while it thinks: the provider sends no headers and no
bytes until the whole answer exists. The deadline transport gives that silent
header wait whatever the setup phases leave of the request deadline --
``90 - (4.5 pool + 9 connect + 9 write) = 67.5s`` -- so the call died on the
header read timeout ~70s after it was sent, with no provider response object
at all. The lease was quarantined (``PROVIDER_REQUEST_OUTCOME_UNKNOWN``), the
ledger recorded zero tokens and the run failed as
``COMMANDER_COMPLETION_FAILED``, a code that said nothing about what happened.

A streamed reasoning call is never silent: the provider emits a chunk for
every few reasoning tokens. So the safe instruments become the right ones:

* a SHORT inactivity timeout between chunks (:data:`STREAM_INACTIVITY_SECONDS`)
  catches a provider that went quiet;
* a per-role TOTAL deadline (``RolePolicy.total_deadline_seconds``), enforced
  by ``backend.provider_transport`` on every chunk, bounds a provider that
  keeps talking forever.

What the stream is allowed to contribute
----------------------------------------

:func:`assemble_chat_stream` accumulates ``delta.content`` ONLY. Reasoning
deltas (``reasoning_content``) are never read, never stored and never
re-sent: the assembled completion has no field that could hold them.
``finish_reason`` and ``usage`` are taken from the final chunk(s) -- both the
OpenAI shape (a trailing chunk with ``usage`` and no choices) and Moonshot's
(``usage`` on the final choice) are read. ``stream_options`` is not sent (it
is not documented for the Kimi models in any source MILO could verify).
Reading stops as soon as the ``finish_reason`` and a usage block at or after
it have both arrived: nothing after them can change the answer or the charge,
and waiting for ``[DONE]`` only risks an inactivity timeout that would
quarantine a slot for a request that already finished.

When a request counts as FINISHED
---------------------------------

Only a stream that ENDS NORMALLY after a ``finish_reason`` proves the request
is over, and only then is the organization lease released. A stream that
drops -- a reset connection, a protocol error, an inactivity timeout, a fired
total deadline, or an end-of-stream with no ``finish_reason`` -- is exactly as
UNKNOWN as it was before: the lease stays quarantined
(``backend.provider_quota``'s invariant is untouched by this module).

Failure codes
-------------

A transport outcome is no longer laundered into a role's generic code. It is
named with one static code (:func:`transport_failure_code`) that reaches
``run.error.code``, the terminal run event and the structured log:

========================================  ======================================
``PROVIDER_REQUEST_DEADLINE_EXCEEDED``    the role's TOTAL deadline fired
``PROVIDER_STREAM_INACTIVITY_TIMEOUT``    no chunk for the inactivity window
``PROVIDER_STREAM_INTERRUPTED``           the stream broke or ended unfinished
``PROVIDER_CONNECTION_FAILED``            nothing was ever sent
``PROVIDER_HTTP_<status>``                the provider answered with an error
========================================  ======================================

The HTTP code carries the numeric status ONLY -- never the body, which can
quote the request.

Structured logging
------------------

:func:`log_provider_call` writes ONE JSON line per provider attempt to stdout,
which Cloud Logging ingests as a structured entry. It carries counts, timings,
static codes and an exception CLASS name. It never carries a prompt, a
completion, reasoning, a URL, a header or a key.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from backend.provider_authority import (_cause_chain, carries_a_complete_provider_response,
                                        failed_before_the_request_was_sent,
                                        fired_a_total_deadline)
from backend.provider_transport import (STREAM_INACTIVITY_SECONDS, RequestTiming,
                                        current_request_timing, request_timing)

# =============================================================================
# 1. TIMING lives with the transport that enforces it
# =============================================================================
# `RequestTiming`, `request_timing`, `current_request_timing` and
# `STREAM_INACTIVITY_SECONDS` are defined in `backend.provider_transport` and
# re-exported here.

# =============================================================================
# 2. ASSEMBLY: one streamed completion -> one completion-shaped object
# =============================================================================

class ProviderStreamInterrupted(Exception):
    """The stream ended without a ``finish_reason``.

    Deliberately carries no ``provider_request_completed`` marker: a stream
    that stops before the provider said it finished is UNKNOWN, and the
    organization lease stays quarantined.
    """

    def __init__(self) -> None:
        super().__init__("provider stream ended without a finish_reason")


@dataclass
class AssembledMessage:
    """The answer, and nothing else. There is no reasoning field on purpose."""

    content: str | None
    role: str = "assistant"
    tool_calls: Any = None


@dataclass
class AssembledChoice:
    message: AssembledMessage
    finish_reason: str | None
    index: int = 0


@dataclass
class AssembledCompletion:
    """A streamed completion, in the shape every role already reads.

    ``classify_completion``, ``read_usage`` and the guarded client read
    ``choices[0].message.content``, ``choices[0].finish_reason`` and
    ``usage`` -- exactly what this carries.
    """

    choices: list[AssembledChoice]
    usage: Any
    model: str | None = None
    id: str | None = None
    #: Monotonic time the first chunk arrived (any chunk, reasoning included).
    first_chunk_at: float | None = None
    chunk_count: int = 0
    streamed: bool = field(default=True, init=False)


def _get(container: Any, name: str) -> Any:
    if container is None:
        return None
    if isinstance(container, Mapping):
        return container.get(name)
    return getattr(container, name, None)


def _close(stream: Any) -> None:
    closer = getattr(stream, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:  # noqa: BLE001 - closing is best effort
            pass


def _is_not_a_stream(value: Any) -> bool:
    """A NON-streamed completion (``choices[*].message``), already read whole.

    A client that answers a streaming request with a finished completion --
    an offline adapter, a test gateway, a proxy that ignores ``stream`` -- has
    handed back a response that was read to completion, which is the proof
    the non-streaming path always settled on. It is passed through unchanged.
    Checked BEFORE iterating: an SDK completion model is itself iterable
    (it yields its fields), and must never be mistaken for a chunk stream.
    """
    if isinstance(value, (str, bytes, bytearray, dict)):
        return True
    if not hasattr(value, "__iter__"):
        # Not a stream at all: `create` returned an object, so the exchange
        # is over; its SHAPE is `classify_completion`'s to judge, exactly as
        # before PR-S.
        return True
    choices = getattr(value, "choices", None)
    if not isinstance(choices, (list, tuple)) or not choices:
        return False
    first = choices[0]
    return (getattr(first, "message", None) is not None
            and getattr(first, "delta", None) is None)


#: The clock first-chunk times are read from. A seam, so a simulated provider
#: can drive the transport, the adapter and this module from ONE clock.
_monotonic: Callable[[], float] = time.monotonic


def assemble_chat_stream(stream: Any, *,
                         clock: Callable[[], float] | None = None) -> AssembledCompletion:
    """Consume ONE streamed chat completion into a completion-shaped object.

    Raises :class:`ProviderStreamInterrupted` when the stream ends without a
    ``finish_reason``; any transport exception raised while reading is re-raised
    unchanged (the one classifier names it), annotated with when the first
    chunk arrived so the structured log can report it.
    """
    if isinstance(stream, AssembledCompletion) or _is_not_a_stream(stream):
        return stream
    clock = clock or (lambda: _monotonic())
    parts: list[str] = []
    finish_reason: str | None = None
    usage: Any = None
    model: str | None = None
    completion_id: str | None = None
    first_chunk_at: float | None = None
    chunks = 0
    try:
        for chunk in stream:
            if first_chunk_at is None:
                first_chunk_at = clock()
            chunks += 1
            model = model or _get(chunk, "model")
            completion_id = completion_id or _get(chunk, "id")
            chunk_usage = _get(chunk, "usage")
            if chunk_usage is not None:
                usage = chunk_usage
            usage_in_chunk = chunk_usage is not None
            for choice in (_get(chunk, "choices") or ()):
                index = _get(choice, "index")
                if index not in (None, 0):
                    # Swarm V2 never asks for n>1; anything else is not ours.
                    continue
                delta = _get(choice, "delta")
                # ONLY the answer. `reasoning_content` is never read.
                content = _get(delta, "content")
                if isinstance(content, str) and content:
                    parts.append(content)
                reason = _get(choice, "finish_reason")
                if reason:
                    finish_reason = str(reason)
                # Moonshot reports usage on the final choice.
                choice_usage = _get(choice, "usage")
                if choice_usage is not None:
                    usage = choice_usage
                    usage_in_chunk = True
            if finish_reason is not None and usage_in_chunk:
                # Finished AND measured (usage on the finishing chunk or a
                # later one; usage seen only BEFORE the finish_reason may be
                # partial and does not end the read). Stop here rather than
                # wait for [DONE]; `_close` below ends the HTTP response.
                break
    except BaseException as exc:
        try:
            exc.milo_first_chunk_at = first_chunk_at  # type: ignore[attr-defined]
            exc.milo_chunk_count = chunks  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - some exceptions refuse attributes
            pass
        raise
    finally:
        _close(stream)
    if finish_reason is None:
        interrupted = ProviderStreamInterrupted()
        interrupted.milo_first_chunk_at = first_chunk_at  # type: ignore[attr-defined]
        interrupted.milo_chunk_count = chunks  # type: ignore[attr-defined]
        raise interrupted
    content = "".join(parts)
    return AssembledCompletion(
        choices=[AssembledChoice(message=AssembledMessage(content=content),
                                 finish_reason=finish_reason)],
        usage=usage, model=model, id=completion_id,
        first_chunk_at=first_chunk_at, chunk_count=chunks)


def ensure_assembled(payload: Mapping[str, Any], response: Any) -> Any:
    """Assemble ``response`` when ``payload`` asked for a stream.

    A raw stream returned for a streaming request has proven NOTHING: the
    provider has sent headers, not an answer. Idempotent, so every layer that
    may be the first to see the response can call it.
    """
    if request_streams(payload) and not isinstance(response, AssembledCompletion):
        return assemble_chat_stream(response)
    return response


def request_streams(payload: Mapping[str, Any]) -> bool:
    return bool(payload.get("stream"))


# =============================================================================
# 3. STATIC TRANSPORT FAILURE CODES
# =============================================================================

PROVIDER_REQUEST_DEADLINE_EXCEEDED = "PROVIDER_REQUEST_DEADLINE_EXCEEDED"
PROVIDER_STREAM_INACTIVITY_TIMEOUT = "PROVIDER_STREAM_INACTIVITY_TIMEOUT"
PROVIDER_STREAM_INTERRUPTED = "PROVIDER_STREAM_INTERRUPTED"
PROVIDER_CONNECTION_FAILED = "PROVIDER_CONNECTION_FAILED"
PROVIDER_HTTP_PREFIX = "PROVIDER_HTTP_"

_STATIC_MESSAGES = {
    PROVIDER_REQUEST_DEADLINE_EXCEEDED: "the provider request exceeded its total deadline",
    PROVIDER_STREAM_INACTIVITY_TIMEOUT: "the provider stream went silent past the inactivity timeout",
    PROVIDER_STREAM_INTERRUPTED: "the provider stream ended before the provider finished",
    PROVIDER_CONNECTION_FAILED: "the provider could not be reached",
}

#: Exception class names (anywhere in the cause chain) that mean MILO stopped
#: waiting on a read. ConnectTimeout/PoolTimeout are deliberately absent: they
#: are "never sent" and are named before this set is consulted.
_TIMEOUT_CLASSES = frozenset({"ReadTimeout", "WriteTimeout", "TimeoutException",
                              "APITimeoutError"})
#: A connection that broke after the request was on the wire.
_INTERRUPTION_CLASSES = frozenset({"ProviderStreamInterrupted", "RemoteProtocolError",
                                   "ReadError", "WriteError", "NetworkError",
                                   "CloseError", "LocalProtocolError",
                                   "APIConnectionError", "APIError",
                                   "IncompleteRead", "ChunkedEncodingError"})


def is_transport_failure_code(code: Any) -> bool:
    if not isinstance(code, str):
        return False
    if code in _STATIC_MESSAGES:
        return True
    suffix = code[len(PROVIDER_HTTP_PREFIX):] if code.startswith(PROVIDER_HTTP_PREFIX) else ""
    return suffix.isdigit() and len(suffix) == 3 and 100 <= int(suffix) <= 599


def transport_failure_message(code: str) -> str:
    if code in _STATIC_MESSAGES:
        return _STATIC_MESSAGES[code]
    return f"the provider answered with HTTP {code[len(PROVIDER_HTTP_PREFIX):]}"


def _chain_class_names(exc: BaseException) -> set[str]:
    return {type(link).__name__ for link in _cause_chain(exc)}


def _provider_status(exc: BaseException) -> int | None:
    for link in _cause_chain(exc):
        if carries_a_complete_provider_response(link):
            status = getattr(getattr(link, "response", None), "status_code", None)
            if isinstance(status, int) and not isinstance(status, bool):
                return status
    return None


def transport_failure_code(exc: BaseException | None) -> str | None:
    """Name a provider TRANSPORT outcome with one static code, or None.

    None means the exception is not a transport outcome this module names
    (a budget stop, a cancellation, a scheduler verdict, a programming error)
    and must keep whatever handling it had. Order matters: a fired total
    deadline is named before anything a wrapper might add, and "never sent"
    before the timeout family, because a connect timeout is not a stalled
    stream.
    """
    if exc is None:
        return None
    if fired_a_total_deadline(exc):
        return PROVIDER_REQUEST_DEADLINE_EXCEEDED
    status = _provider_status(exc)
    if status is not None and 100 <= status <= 599:
        return f"{PROVIDER_HTTP_PREFIX}{status}"
    names = _chain_class_names(exc)
    if "ProviderStreamInterrupted" in names:
        return PROVIDER_STREAM_INTERRUPTED
    if failed_before_the_request_was_sent(exc):
        return PROVIDER_CONNECTION_FAILED
    if names & _TIMEOUT_CLASSES:
        return PROVIDER_STREAM_INACTIVITY_TIMEOUT
    if names & _INTERRUPTION_CLASSES:
        return PROVIDER_STREAM_INTERRUPTED
    return None


class ProviderTransportFailure(RuntimeError):
    """A provider request that failed in transport. Static code only.

    Raised by the Swarm V2 gateway AFTER the provider authority has settled
    the attempt (lease, ledger), so converting the exception changes nothing
    about accounting -- only what the run reports.
    """

    def __init__(self, code: str, *, role: str = "") -> None:
        if not is_transport_failure_code(code):
            raise ValueError("transport failure code must come from the static set")
        self.code = code
        self.role = str(role or "")[:64]
        self.safe_message = transport_failure_message(code)
        super().__init__(self.safe_message)


# =============================================================================
# 4. SANITIZED STRUCTURED LOGGING
# =============================================================================

def emit_structured_log(record: Mapping[str, Any], *, stream: Any = None) -> None:
    """Write ONE JSON line to stdout. Never raises: logging is not a failure."""
    target = stream if stream is not None else sys.stdout
    try:
        target.write(json.dumps(dict(record), sort_keys=True, separators=(",", ":"),
                                default=str) + "\n")
        target.flush()
    except Exception:  # noqa: BLE001 - a broken log sink must not fail a run
        pass


def _count(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def usage_counts(usage: Any) -> dict[str, int | None]:
    """Counts only, from either usage shape. Nothing else is read."""
    completion_details = _get(usage, "completion_tokens_details")
    prompt_details = _get(usage, "prompt_tokens_details")
    cached = _count(_get(prompt_details, "cached_tokens"))
    if cached is None:
        cached = _count(_get(usage, "cached_tokens"))
    return {
        "prompt_tokens": _count(_get(usage, "prompt_tokens")),
        "completion_tokens": _count(_get(usage, "completion_tokens")),
        "reasoning_tokens": _count(_get(completion_details, "reasoning_tokens")),
        "cached_tokens": cached,
    }


def _requested_effort(payload: Mapping[str, Any]) -> str | None:
    effort = payload.get("reasoning_effort")
    if isinstance(effort, str):
        return effort[:16]
    thinking = _get(_get(payload.get("extra_body"), "thinking"), "type")
    if isinstance(thinking, str):
        return f"thinking:{thinking[:16]}"
    return None


def _requested_cap(payload: Mapping[str, Any]) -> int | None:
    for name in ("max_completion_tokens", "max_tokens"):
        value = _count(payload.get(name))
        if value is not None:
            return value
    return None


def role_label(agent: str, phase: str) -> str:
    """``<agent kind>:<phase>``. The task id a worker agent carries is a
    model-chosen identifier and never reaches a log."""
    kind = str(agent or "").split(":", 1)[0][:32]
    return f"{kind}:{str(phase or '')[:32]}"


def log_provider_call(*, payload: Mapping[str, Any], agent: str, phase: str,
                      started_at: float, ended_at: float,
                      response: Any = None, exc: BaseException | None = None,
                      outcome: str = "", completion_proven: bool | None = None,
                      context: Mapping[str, Any] | None = None,
                      stream: Any = None) -> dict[str, Any]:
    """Log ONE provider attempt. Returns the record (tests read it).

    Never raises: an odd response shape or a broken sink is not a reason for a
    provider call to fail.
    """
    try:
        record = _call_record(payload=payload, agent=agent, phase=phase,
                              started_at=started_at, ended_at=ended_at,
                              response=response, exc=exc, outcome=outcome,
                              completion_proven=completion_proven, context=context)
    except Exception:  # noqa: BLE001 - observability must not fail the call
        record = {"severity": "WARNING", "event": "provider_call",
                  "message": "provider_call", "role": role_label(agent, phase),
                  "log_incomplete": True}
    emit_structured_log(record, stream=stream)
    return record


def _call_record(*, payload: Mapping[str, Any], agent: str, phase: str,
                 started_at: float, ended_at: float, response: Any,
                 exc: BaseException | None, outcome: str,
                 completion_proven: bool | None,
                 context: Mapping[str, Any] | None) -> dict[str, Any]:
    first_chunk_at = (getattr(response, "first_chunk_at", None) if exc is None
                      else getattr(exc, "milo_first_chunk_at", None))
    timing = current_request_timing()
    record: dict[str, Any] = {
        "severity": "INFO" if exc is None else "WARNING",
        "event": "provider_call",
        "message": "provider_call",
        "role": role_label(agent, phase),
        "model": str(payload.get("model") or "")[:64],
        "effort": _requested_effort(payload),
        "cap": _requested_cap(payload),
        "stream": request_streams(payload),
        "response_format": str(_get(payload.get("response_format"), "type") or "")[:32] or None,
        "total_deadline_s": None if timing is None else timing.total_deadline_seconds,
        "inactivity_s": None if timing is None else timing.inactivity_seconds,
        "time_to_first_chunk_ms": (None if first_chunk_at is None
                                   else int(round((first_chunk_at - started_at) * 1000))),
        "total_ms": int(round((ended_at - started_at) * 1000)),
        "outcome": str(outcome or ("success" if exc is None else "unknown_request_outcome"))[:48],
        "completion_proven": completion_proven,
    }
    if exc is None:
        choices = _get(response, "choices")
        first = choices[0] if isinstance(choices, (list, tuple)) and choices else None
        finish = _get(first, "finish_reason")
        record["finish_reason"] = str(finish)[:32] if finish is not None else None
        record["chunk_count"] = getattr(response, "chunk_count", None)
        usage = _get(response, "usage")
        record["usage_reported"] = usage is not None
        record.update(usage_counts(usage))
        record["code"] = None
        record["exception_class"] = None
    else:
        record["finish_reason"] = None
        record["chunk_count"] = getattr(exc, "milo_chunk_count", None)
        record["code"] = transport_failure_code(exc)
        # The CLASS name only -- never str(exc), which can quote a URL, a
        # header or a provider body.
        record["exception_class"] = type(exc).__name__[:64]
        cause = exc.__cause__ or exc.__context__
        record["cause_class"] = type(cause).__name__[:64] if cause is not None else None
    for key, value in (context or {}).items():
        if key in record:
            continue
        record[str(key)[:32]] = str(value)[:64] if value is not None else None
    return record


__all__ = [
    "AssembledChoice", "AssembledCompletion", "AssembledMessage",
    "PROVIDER_CONNECTION_FAILED", "PROVIDER_HTTP_PREFIX",
    "PROVIDER_REQUEST_DEADLINE_EXCEEDED", "PROVIDER_STREAM_INACTIVITY_TIMEOUT",
    "PROVIDER_STREAM_INTERRUPTED", "ProviderStreamInterrupted",
    "ProviderTransportFailure", "RequestTiming", "STREAM_INACTIVITY_SECONDS",
    "assemble_chat_stream", "current_request_timing", "emit_structured_log",
    "ensure_assembled",
    "is_transport_failure_code", "log_provider_call", "request_streams",
    "request_timing", "role_label", "transport_failure_code",
    "transport_failure_message", "usage_counts",
]

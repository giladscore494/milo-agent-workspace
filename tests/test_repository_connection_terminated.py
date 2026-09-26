"""PR-W S3: an HTTP/2 GOAWAY from Supabase is a TRANSIENT repository failure.

Attempt 1 of run 280fc9e5 exited 1 with
``REPOSITORY_ERROR: <ConnectionTerminated error_code:1, last_stream_id:131>``:
the server ended the multiplexed HTTP/2 connection (h2 ``ConnectionTerminated``)
under a request. httpcore raises that as ``RemoteProtocolError(event)``, httpx
re-raises it as ``httpx.RemoteProtocolError`` -- and when the mapping is not
applied (a lower layer, a chained cause) it arrives as the httpcore or h2 type.

Every one of those shapes now classifies as ``transient``. Retrying stays
EXACTLY where PR-U put it: only `IDEMPOTENT_GUARDED_RPCS` repeat a transient
failure; every other guarded RPC is still single-attempt.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import h2.events
import h2.exceptions
import httpcore
import httpx
import pytest
from postgrest.exceptions import APIError

from backend.errors import RepositoryFailure
from backend.repository.supabase import (IDEMPOTENT_GUARDED_RPCS, IDEMPOTENT_RPC_ATTEMPTS,
                                         SupabaseRepository, classify_repository_failure)

RUN = UUID("280fc9e5-0000-4000-8000-000000000001")
LEASE = {"worker_id": "worker-1", "attempt": 1, "lease_token": "lease-token"}
SOURCE = {"evidence_key": "src-1", "task_key": "t02", "url": "https://example.test/x"}
SOURCE_ROW = {"id": "c9f0bd57-35a2-4a51-9d1e-1f2c2b4e9a10", "evidence_key": "src-1"}


def _goaway() -> h2.events.ConnectionTerminated:
    event = h2.events.ConnectionTerminated()
    event.error_code = 1
    event.last_stream_id = 131
    return event


def _chained(outer: BaseException, cause: BaseException) -> BaseException:
    outer.__cause__ = cause
    return outer


def _goaway_shapes() -> list[BaseException]:
    return [
        httpx.RemoteProtocolError(str(_goaway())),
        httpcore.RemoteProtocolError(_goaway()),
        h2.exceptions.ProtocolError("connection terminated"),
        # The GOAWAY event itself as the only argument of an unmapped error.
        RuntimeError(_goaway()),
        # A wrapper whose CAUSE is the protocol error.
        _chained(RuntimeError("wrapped"), httpcore.RemoteProtocolError(_goaway())),
    ]


@pytest.mark.parametrize("exc", _goaway_shapes(), ids=lambda exc: type(exc).__name__)
def test_every_connection_terminated_shape_is_transient(exc):
    assert classify_repository_failure(exc) == "transient"


def test_the_exact_attempt_1_message_is_what_the_goaway_renders():
    assert str(httpcore.RemoteProtocolError(_goaway())).startswith(
        "<ConnectionTerminated error_code:1, last_stream_id:131")


@pytest.mark.parametrize("exc,expected", [
    (APIError({"message": "m", "code": "23505", "hint": None, "details": None}), "rejected"),
    (APIError({"message": "m", "code": "42501", "hint": None, "details": None}), "unavailable"),
    (APIError({"message": "m", "code": "503", "hint": None, "details": None}), "transient"),
    (RuntimeError("ConnectionTerminated in free text only"), "unavailable"),
    (ValueError("boom"), "unavailable"),
    (_chained(ValueError("outer"), KeyError("inner")), "unavailable"),
])
def test_other_failures_keep_their_class(exc, expected):
    """The class is read from TYPES and codes, never from message text."""
    assert classify_repository_failure(exc) == expected


def test_a_self_referencing_cause_chain_terminates():
    first, second = RuntimeError("a"), RuntimeError("b")
    first.__cause__, second.__cause__ = second, first
    assert classify_repository_failure(first) == "unavailable"


# --- the retry stays exactly where PR-U put it ---------------------------------

class _Call:
    def __init__(self, outcome: Any) -> None:
        self._outcome = outcome

    def execute(self) -> Any:
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return type("Result", (), {"data": self._outcome})()


class _ScriptedClient:
    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[str] = []

    def rpc(self, name: str, params: dict) -> _Call:
        self.calls.append(name)
        return _Call(self.outcomes.pop(0))


def _repository(client: _ScriptedClient, sleeps: list[float]) -> SupabaseRepository:
    repository = SupabaseRepository.__new__(SupabaseRepository)
    repository.client = client
    repository._retry_sleep = sleeps.append
    return repository


@pytest.mark.parametrize("exc", _goaway_shapes(), ids=lambda exc: type(exc).__name__)
def test_an_idempotent_guarded_rpc_is_retried_after_a_goaway(exc):
    assert "upsert_source_guarded" in IDEMPOTENT_GUARDED_RPCS
    client = _ScriptedClient(exc, [SOURCE_ROW])
    sleeps: list[float] = []
    assert _repository(client, sleeps).create_source(RUN, SOURCE, **LEASE) == SOURCE_ROW
    assert client.calls == ["upsert_source_guarded"] * 2
    assert len(sleeps) == 1


def test_an_idempotent_guarded_rpc_gives_up_after_its_attempts():
    client = _ScriptedClient(*[httpcore.RemoteProtocolError(_goaway())] * IDEMPOTENT_RPC_ATTEMPTS)
    with pytest.raises(RepositoryFailure) as failure:
        _repository(client, []).create_source(RUN, SOURCE, **LEASE)
    assert failure.value.failure_class == "transient"
    assert len(client.calls) == IDEMPOTENT_RPC_ATTEMPTS
    # The generic message: the GOAWAY text never becomes the durable message.
    assert "ConnectionTerminated" not in failure.value.message


@pytest.mark.parametrize("exc", _goaway_shapes(), ids=lambda exc: type(exc).__name__)
def test_a_non_idempotent_guarded_rpc_is_never_retried(exc):
    """append_run_event_guarded is a plain insert: classified, not repeated."""
    assert "append_run_event_guarded" not in IDEMPOTENT_GUARDED_RPCS
    client = _ScriptedClient(exc, [{"id": 1}])
    sleeps: list[float] = []
    with pytest.raises(RepositoryFailure) as failure:
        _repository(client, sleeps).append_run_event(
            RUN, "task_completed", {"message": "m"}, **LEASE)
    assert failure.value.failure_class == "transient"
    assert client.calls == ["append_run_event_guarded"]
    assert sleeps == []


def test_a_postgrest_answer_keeps_its_own_code_whatever_it_was_raised_during():
    """An APIError is the database's answer: its SQLSTATE decides, not its context."""
    answer = APIError({"message": "m", "code": "23505", "hint": None, "details": None})
    answer.__context__ = httpx.RemoteProtocolError("earlier blip")
    assert classify_repository_failure(answer) == "rejected"

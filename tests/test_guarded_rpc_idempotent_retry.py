"""PR-U S3: a transient failure of an idempotent evidence write is retried.

Run 6825eb96 (2026-09-26T00:08:40Z) logged ONE
``guarded rpc failed function=upsert_source_guarded cause=RemoteProtocolError
class=transient attempt=1/1 action=raise``; t02 failed TOOL_EXECUTION_FAILED ->
REPOSITORY_ERROR and the container exited 1. Only Cloud Run's maxRetries=1 saved
the run.

Every function in `IDEMPOTENT_GUARDED_RPCS` is idempotent by key in its SQL (the
U1 table in the PR-U description), so a transient failure there now gets up to
three attempts. Nothing else changes: a rejection, a lost lease, an HTTP 4xx and
every function outside the allowlist are raised after one attempt, exactly as
before.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx
import pytest
from postgrest.exceptions import APIError

from backend.catalog.write_diagnostics import CATALOG_INGESTION_RPCS
from backend.errors import AppError, RepositoryFailure
from backend.repository import supabase as supabase_module
from backend.repository.supabase import (IDEMPOTENT_GUARDED_RPCS, IDEMPOTENT_RPC_ATTEMPTS,
                                         IDEMPOTENT_RPC_BACKOFF_SECONDS, IDEMPOTENT_RPC_JITTER,
                                         SupabaseRepository)

RUN = UUID("6825eb96-1550-4464-8e8a-ecb1eca3e967")
LEASE = {"worker_id": "worker-1", "attempt": 1, "lease_token": "lease-token"}
SOURCE = {"evidence_key": "src-1", "task_key": "t02", "url": "https://example.test/x"}
SOURCE_ROW = {"id": "c9f0bd57-35a2-4a51-9d1e-1f2c2b4e9a10", "evidence_key": "src-1"}
URL_SENTINEL = "https://leaky.example.test/rest/v1/rpc?apikey=LEAKED"
SQL_SENTINEL = "insert into public.sources values ('SQL_SENTINEL')"


def _api_error(code: Any, message: str = SQL_SENTINEL) -> APIError:
    return APIError({"message": message, "code": code, "hint": None, "details": URL_SENTINEL})


class _Call:
    def __init__(self, outcome: Any) -> None:
        self._outcome = outcome

    def execute(self) -> Any:
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return type("Result", (), {"data": self._outcome})()


class _ScriptedClient:
    """`client.rpc(name, params).execute()` answers from a script, in order."""

    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, dict]] = []

    def rpc(self, name: str, params: dict) -> _Call:
        self.calls.append((name, params))
        return _Call(self.outcomes.pop(0))


def _repository(client: _ScriptedClient, sleeps: list[float]) -> SupabaseRepository:
    repository = SupabaseRepository.__new__(SupabaseRepository)
    repository.client = client
    repository._retry_sleep = sleeps.append
    return repository


def _log_lines(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records
            if record.name == "milo.repository"]


@pytest.fixture(autouse=True)
def _capture(caplog):
    caplog.set_level("WARNING", logger="milo.repository")


# =============================================================================
# the allowlist is exactly the U1 table
# =============================================================================

def test_the_allowlist_is_exactly_the_evidence_writes_proven_idempotent():
    assert IDEMPOTENT_GUARDED_RPCS == {
        "upsert_source_guarded", "create_claim_with_source_guarded",
        "create_conflict_guarded", "record_evidence_fragment_guarded",
        "create_tool_usage_guarded", "record_claim_verdict_guarded",
        "record_conflict_resolution_guarded", "patch_run_blackboard_evidence_guarded"}
    # The catalog ingestion writes keep their own, unchanged retry policy.
    assert not IDEMPOTENT_GUARDED_RPCS & CATALOG_INGESTION_RPCS
    for never in ("append_run_event_guarded", "save_checkpoint_guarded",
                  "append_usage_ledger_guarded", "record_run_usage_guarded",
                  "update_run_usage_guarded", "reserve_model_call_budget_guarded",
                  "settle_model_call_budget_guarded", "transition_run_worker_guarded",
                  "finalize_run_guarded", "heartbeat_run_guarded",
                  "upsert_run_blackboard_guarded", "create_agent_message_guarded",
                  "create_supervisor_decision_guarded", "promote_catalog_variant_guarded"):
        assert never not in IDEMPOTENT_GUARDED_RPCS
    assert (IDEMPOTENT_RPC_ATTEMPTS, IDEMPOTENT_RPC_BACKOFF_SECONDS) == (3, (0.5, 1.5))


# =============================================================================
# the 6825eb96 failure, retried
# =============================================================================

def test_a_transient_failure_then_success_takes_two_attempts(caplog):
    client = _ScriptedClient(httpx.RemoteProtocolError("peer closed " + URL_SENTINEL),
                             [SOURCE_ROW])
    sleeps: list[float] = []
    row = _repository(client, sleeps).create_source(RUN, SOURCE, **LEASE)
    assert row == SOURCE_ROW
    assert [name for name, _ in client.calls] == ["upsert_source_guarded"] * 2
    # The byte-identical parameters are re-sent: the same evidence_key.
    assert client.calls[0][1] == client.calls[1][1]
    (sleep,) = sleeps
    assert 0.5 * (1 - IDEMPOTENT_RPC_JITTER) <= sleep <= 0.5 * (1 + IDEMPOTENT_RPC_JITTER)
    (line,) = _log_lines(caplog)
    assert ("guarded rpc failed function=upsert_source_guarded cause=RemoteProtocolError "
            "code=none http_status=none class=transient attempt=1/3 action=retry") in line
    assert URL_SENTINEL not in line


def test_three_transient_failures_raise_exactly_what_one_did_before(caplog):
    client = _ScriptedClient(httpx.RemoteProtocolError(URL_SENTINEL),
                             httpx.ReadTimeout(URL_SENTINEL),
                             httpx.ConnectTimeout(URL_SENTINEL))
    sleeps: list[float] = []
    with pytest.raises(RepositoryFailure) as failure:
        _repository(client, sleeps).create_source(RUN, SOURCE, **LEASE)
    # Same error as today's single attempt: same code, message, status, class.
    assert (failure.value.code, failure.value.message, failure.value.status_code,
            failure.value.failure_class) == \
        ("REPOSITORY_ERROR", "guarded persistence operation failed", 502, "transient")
    assert len(client.calls) == 3
    assert len(sleeps) == 2
    for sleep, base in zip(sleeps, IDEMPOTENT_RPC_BACKOFF_SECONDS):
        assert base * (1 - IDEMPOTENT_RPC_JITTER) <= sleep <= base * (1 + IDEMPOTENT_RPC_JITTER)
    lines = _log_lines(caplog)
    assert len(lines) == 3
    assert "cause=RemoteProtocolError" in lines[0] and "attempt=1/3 action=retry" in lines[0]
    assert "cause=ReadTimeout" in lines[1] and "attempt=2/3 action=retry" in lines[1]
    assert "cause=ConnectTimeout" in lines[2] and "attempt=3/3 action=raise" in lines[2]
    assert URL_SENTINEL not in "\n".join(lines)


def test_the_backoff_is_jittered_around_the_fixed_bases():
    client = _ScriptedClient(_api_error("503"), _api_error("504"), [SOURCE_ROW])
    sleeps: list[float] = []
    repository = _repository(client, sleeps)
    bounds: list[tuple[float, float]] = []
    repository._retry_jitter = lambda low, high: bounds.append((low, high)) or high
    repository.create_source(RUN, SOURCE, **LEASE)
    assert bounds == [(0.8, 1.2), (0.8, 1.2)]
    assert sleeps == pytest.approx([0.6, 1.8])


@pytest.mark.parametrize("transient", [
    httpx.RemoteProtocolError("x"), httpx.ConnectTimeout("x"), httpx.ReadTimeout("x"),
    _api_error("502"), _api_error("503"), _api_error("504")])
def test_each_named_transient_shape_is_retried(transient):
    client = _ScriptedClient(transient, [SOURCE_ROW])
    assert _repository(client, []).create_source(RUN, SOURCE, **LEASE) == SOURCE_ROW
    assert len(client.calls) == 2


@pytest.mark.parametrize("method,function,payload", [
    ("create_claim", "create_claim_with_source_guarded", {"evidence_key": "c"}),
    ("create_conflict", "create_conflict_guarded", {"evidence_key": "k"}),
    ("record_evidence_fragment", "record_evidence_fragment_guarded", {"evidence_key": "f"}),
    ("create_tool_usage", "create_tool_usage_guarded", {"idempotency_key": "u"}),
    ("record_claim_verdict", "record_claim_verdict_guarded", {"evidence_key": "v"}),
    ("record_conflict_resolution", "record_conflict_resolution_guarded", {"evidence_key": "r"}),
    ("patch_run_blackboard_evidence", "patch_run_blackboard_evidence_guarded",
     {"known_entities": [], "claims_conflict_summaries": []}),
])
def test_every_allowlisted_evidence_write_is_retried(method, function, payload):
    client = _ScriptedClient(httpx.RemoteProtocolError("x"), [{"id": "row"}])
    assert getattr(_repository(client, []), method)(RUN, payload, **LEASE) == {"id": "row"}
    assert [name for name, _ in client.calls] == [function] * 2


# =============================================================================
# what is never retried
# =============================================================================

def test_a_rejection_is_one_attempt(caplog):
    client = _ScriptedClient(_api_error("22023"), [SOURCE_ROW])
    sleeps: list[float] = []
    with pytest.raises(RepositoryFailure) as failure:
        _repository(client, sleeps).create_source(RUN, SOURCE, **LEASE)
    assert failure.value.failure_class == "rejected"
    assert len(client.calls) == 1 and sleeps == []
    (line,) = _log_lines(caplog)
    assert "class=rejected attempt=1/3 action=raise" in line


def test_a_transient_failure_of_a_function_outside_the_allowlist_is_one_attempt(caplog):
    client = _ScriptedClient(httpx.RemoteProtocolError("x"), [{"id": "event"}])
    sleeps: list[float] = []
    with pytest.raises(RepositoryFailure) as failure:
        _repository(client, sleeps).append_run_event(RUN, "task_started", {}, **LEASE)
    assert failure.value.failure_class == "transient"
    assert [name for name, _ in client.calls] == ["append_run_event_guarded"]
    assert sleeps == []
    (line,) = _log_lines(caplog)
    assert "function=append_run_event_guarded" in line
    assert "class=transient attempt=1/1 action=raise" in line


@pytest.mark.parametrize("call", [
    lambda repo: repo.append_usage_ledger({"run_id": str(RUN), "call_seq": 1}, **LEASE),
    lambda repo: repo.save_checkpoint({"run_id": str(RUN), "phase": "p", "engine_version": "v",
                                       "workflow_key": "swarm_v2"}, **LEASE),
])
def test_other_non_idempotent_writes_stay_single_attempt(call):
    client = _ScriptedClient(httpx.RemoteProtocolError("x"), [{"id": "row"}])
    with pytest.raises(RepositoryFailure):
        call(_repository(client, []))
    assert len(client.calls) == 1


def test_a_lost_lease_is_never_retried(caplog):
    client = _ScriptedClient(_api_error("55000", "STALE_WORKER_WRITE: lease is not current"),
                             [SOURCE_ROW])
    sleeps: list[float] = []
    with pytest.raises(AppError) as failure:
        _repository(client, sleeps).create_source(RUN, SOURCE, **LEASE)
    assert failure.value.code == "RUN_LEASE_LOST"
    assert len(client.calls) == 1 and sleeps == []
    (line,) = _log_lines(caplog)
    assert "class=lease_lost attempt=1/3 action=raise" in line


def test_the_lease_is_rechecked_on_every_retry_and_a_loss_ends_it():
    """Each retry is the same lease-guarded call; the database's lease check
    decides, and a loss observed on the retry stops at once."""
    client = _ScriptedClient(httpx.RemoteProtocolError("x"),
                             _api_error("55000", "STALE_WORKER_WRITE: lease is not current"),
                             [SOURCE_ROW])
    sleeps: list[float] = []
    with pytest.raises(AppError) as failure:
        _repository(client, sleeps).create_source(RUN, SOURCE, **LEASE)
    assert failure.value.code == "RUN_LEASE_LOST"
    assert len(client.calls) == 2 and len(sleeps) == 1
    assert all(params["p_lease_token"] == "lease-token" for _, params in client.calls)


@pytest.mark.parametrize("status", ["408", "425", "429"])
def test_a_4xx_is_never_retried_even_when_classified_transient(status):
    client = _ScriptedClient(_api_error(status), [SOURCE_ROW])
    sleeps: list[float] = []
    with pytest.raises(RepositoryFailure) as failure:
        _repository(client, sleeps).create_source(RUN, SOURCE, **LEASE)
    assert failure.value.failure_class == "transient"
    assert len(client.calls) == 1 and sleeps == []


# =============================================================================
# end to end through supabase-py
# =============================================================================

class _Settings:
    supabase_url = "https://abcdefghijklmnopqrst.supabase.co"
    supabase_service_role_key = "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoic2VydmljZV9yb2xlIn0.x"


def _real_repository(*responses: httpx.Response) -> tuple[SupabaseRepository, list[float]]:
    repository = SupabaseRepository(_Settings())
    session = repository.client.postgrest.session
    answers = iter(responses)
    session._transport = httpx.MockTransport(lambda request: next(answers))
    session._mounts = {}
    sleeps: list[float] = []
    repository._retry_sleep = sleeps.append
    return repository, sleeps


def test_the_real_client_retries_a_504_and_returns_the_row(caplog):
    repository, sleeps = _real_repository(
        httpx.Response(504, text="<html>gateway " + URL_SENTINEL + "</html>"),
        httpx.Response(200, json=[SOURCE_ROW]))
    assert repository.create_source(RUN, SOURCE, **LEASE) == SOURCE_ROW
    assert len(sleeps) == 1
    (line,) = _log_lines(caplog)
    assert "http_status=504 class=transient attempt=1/3 action=retry" in line
    assert URL_SENTINEL not in line


def test_the_real_client_never_retries_a_429():
    repository, sleeps = _real_repository(
        httpx.Response(429, json={"code": "PGRST000", "message": SQL_SENTINEL,
                                  "details": URL_SENTINEL, "hint": None}),
        httpx.Response(200, json=[SOURCE_ROW]))
    with pytest.raises(RepositoryFailure) as failure:
        repository.create_source(RUN, SOURCE, **LEASE)
    assert failure.value.failure_class == "transient"
    assert sleeps == []


def test_the_module_seam_uses_a_real_uniform_jitter_by_default():
    assert SupabaseRepository._retry_jitter is supabase_module.random.uniform

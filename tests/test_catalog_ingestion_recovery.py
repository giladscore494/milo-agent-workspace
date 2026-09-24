"""Catalog ingestion recovery (incident 2026-09-24).

A scoped Toyota preparation wrote 6368 raw records one RPC per row, then a
single candidate write failed and the whole preparation stopped as
`CAPTURE_REPOSITORY_UNAVAILABLE`, leaving a PENDING snapshot owned by the
failed run. Its key is derived from content, so every later capture of the
same register content resolved to that row and was refused
(`GOV_SNAPSHOT_OWNED_BY_ANOTHER_RUN`). These tests hold the fix to its
contract:

* a failed repository write is classified from its SHAPE into a small static
  vocabulary, and nothing from its text reaches a report;
* only a TRANSIENT failure of an idempotent catalog ingestion write is
  retried, within a fixed budget, and never a lost lease or a rejection;
* one failed heartbeat is not a lost lease;
* activation marks a snapshot `failed` only when the database REFUSED it;
* ingestion writes in bounded batches;
* an orphaned pending snapshot is adopted by the next operator capture run and
  finished through the same idempotent writes -- the incident, replayed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from postgrest.exceptions import APIError

from backend.catalog import operator_capture as entrypoint
from backend.catalog.government import ingest as ingest_module
from backend.catalog.government import source as src
from backend.catalog.government.ingest import (GovernmentCatalogIngestor,
                                               GovernmentIngestionError)
from backend.engines.swarm_v2.evidence import WorkerLease
from backend.errors import AppError, RepositoryFailure
from backend.repository import supabase as supabase_module
from backend.repository.supabase import (CATALOG_WRITE_ATTEMPTS, SupabaseRepository,
                                         classify_repository_failure)
from backend.testing.government_capture import FixtureTransport
from backend.testing.memory_repository import MemoryRepository
from tests.test_catalog_operator_capture import (SUPABASE_URL, authorized_argv,
                                                 capture_env, prepare_run, run_main,
                                                 whole_resource_page, committed_records)

SQL_SENTINEL = "select secret_column from public.runs where token='LEAKED'"
URL_SENTINEL = "https://leaky.example.test/rest/v1/rpc?apikey=LEAKED"


@pytest.fixture(autouse=True)
def process_environment(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", SUPABASE_URL)


def _api_error(code: Any, message: str = SQL_SENTINEL) -> APIError:
    return APIError({"message": message, "code": code, "hint": None, "details": URL_SENTINEL})


# =============================================================================
# 1. classification: the shape decides, never the text
# =============================================================================

@pytest.mark.parametrize("failure,expected", [
    (httpx.ConnectError("connection refused " + URL_SENTINEL), "transient"),
    (httpx.ReadTimeout("read timed out"), "transient"),
    (httpx.RemoteProtocolError("server disconnected"), "transient"),
    (httpx.PoolTimeout("pool"), "transient"),
    (_api_error(502), "transient"),               # a gateway page, not PostgREST JSON
    (_api_error("503"), "transient"),
    (_api_error("429"), "transient"),
    (_api_error("504"), "transient"),
    (_api_error("PGRST001"), "transient"),        # PostgREST cannot reach the database
    (_api_error("PGRST003"), "transient"),        # connection pool timeout
    (_api_error("08006"), "transient"),           # connection failure
    (_api_error("40001"), "transient"),           # serialization failure
    (_api_error("40P01"), "transient"),           # deadlock
    (_api_error("57014"), "transient"),           # statement timeout
    (_api_error("53300"), "transient"),           # too many connections
    (_api_error("22023"), "rejected"),            # the catalog RPCs' own refusals
    (_api_error("23505"), "rejected"),            # a unique violation
    (_api_error("22P02"), "rejected"),
    (_api_error("42501"), "unavailable"),
    (_api_error("P0001"), "unavailable"),
    (_api_error(None), "unavailable"),
    (_api_error(404), "unavailable"),
    (ValueError("anything else"), "unavailable"),
])
def test_a_repository_failure_is_classified_from_its_shape(failure, expected):
    assert classify_repository_failure(failure) == expected


def test_a_repository_failure_class_is_allowlisted():
    with pytest.raises(ValueError):
        RepositoryFailure("because-the-database-said-so")
    failure = RepositoryFailure("transient")
    # Every existing caller that matches on the code or the message is unchanged.
    assert (failure.code, failure.message, failure.status_code) == \
        ("REPOSITORY_ERROR", "guarded persistence operation failed", 502)


# =============================================================================
# 2. the bounded retry of the catalog ingestion writes
# =============================================================================

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


LEASE = {"worker_id": "operator-capture-1", "attempt": 1, "lease_token": "token"}
RUN = UUID("bbff131a-4ba3-4e79-9796-a7edb7df314c")
ACTIVATION = {"snapshot_id": "701ea334-beb6-4e66-afe8-ca3df4be3d2d"}


def test_a_transient_failure_is_retried_within_the_budget_and_then_succeeds():
    row = {"id": ACTIVATION["snapshot_id"], "activated_at": "2026-09-24T00:00:00Z"}
    client = _ScriptedClient(httpx.ReadTimeout("t"), _api_error(503), [row])
    sleeps: list[float] = []
    assert _repository(client, sleeps).activate_catalog_snapshot(RUN, ACTIVATION, **LEASE) == row
    assert [name for name, _ in client.calls] == ["activate_catalog_snapshot_guarded"] * 3
    assert sleeps == [0.5, 1.0]


def test_the_retry_budget_is_fixed_and_then_the_failure_is_reported_as_transient():
    client = _ScriptedClient(*[httpx.ConnectError(URL_SENTINEL)] * CATALOG_WRITE_ATTEMPTS)
    sleeps: list[float] = []
    with pytest.raises(RepositoryFailure) as failure:
        _repository(client, sleeps).activate_catalog_snapshot(RUN, ACTIVATION, **LEASE)
    assert failure.value.failure_class == "transient"
    assert len(client.calls) == CATALOG_WRITE_ATTEMPTS == 4
    assert sleeps == [0.5, 1.0, 2.0]
    assert URL_SENTINEL not in failure.value.message


@pytest.mark.parametrize("outcome,code,failure_class", [
    (_api_error("55000", "STALE_WORKER_WRITE: lease is not current"), "RUN_LEASE_LOST", None),
    (_api_error("22023", "catalog snapshot is incomplete"), "REPOSITORY_ERROR", "rejected"),
    (_api_error("23505"), "REPOSITORY_ERROR", "rejected"),
    (_api_error("P0001"), "REPOSITORY_ERROR", "unavailable"),
])
def test_a_lost_lease_a_rejection_and_an_unknown_failure_are_never_retried(
        outcome, code, failure_class):
    client = _ScriptedClient(outcome)
    sleeps: list[float] = []
    with pytest.raises(AppError) as failure:
        _repository(client, sleeps).activate_catalog_snapshot(RUN, ACTIVATION, **LEASE)
    assert failure.value.code == code
    assert getattr(failure.value, "failure_class", None) == failure_class
    assert len(client.calls) == 1 and sleeps == []
    assert SQL_SENTINEL not in failure.value.message


def test_only_the_catalog_ingestion_writes_retry():
    """The heartbeat and every other guarded write keep their one attempt."""
    client = _ScriptedClient(httpx.ReadTimeout("t"))
    sleeps: list[float] = []
    with pytest.raises(RepositoryFailure):
        _repository(client, sleeps).heartbeat(RUN, "w", attempt=1, lease_token="t")
    assert len(client.calls) == 1 and sleeps == []


def _raw_record(index: int) -> dict[str, Any]:
    return {"snapshot_id": ACTIVATION["snapshot_id"], "snapshot_key": "cs1." + "a" * 32,
            "resource_id": src.WLTP_RESOURCE_ID, "upstream_record_id": str(42000 + index),
            "payload": {"_id": 42000 + index}, "source_locator": {"capture_index": index}}


def test_a_raw_record_batch_is_one_bounded_call_answered_row_for_row():
    rows = [{"id": str(uuid4())} for _ in range(3)]
    client = _ScriptedClient({"rows": rows, "inserted": 2, "already_present": 1})
    repository = _repository(client, [])
    assert repository.record_catalog_raw_records(RUN, [_raw_record(i) for i in range(3)],
                                                 **LEASE) == \
        {"rows": rows, "inserted": 2, "already_present": 1}
    (name, params), = client.calls
    assert name == "record_catalog_raw_records_batch_guarded"
    assert len(params["p_records"]) == 3
    assert all(record["record_key"].startswith("cr1.") for record in params["p_records"])
    # A short or inconsistent answer is never taken as a complete batch.
    for answer in ({"rows": rows[:2], "inserted": 2, "already_present": 0},
                   {"rows": rows, "inserted": 3, "already_present": 1},
                   rows):
        wrong = _repository(_ScriptedClient(answer), [])
        with pytest.raises(RepositoryFailure):
            wrong.record_catalog_raw_records(RUN, [_raw_record(i) for i in range(3)], **LEASE)


def test_the_batch_size_is_a_fixed_server_constant():
    from backend.catalog import contracts
    assert contracts.CATALOG_WRITE_BATCH_SIZE == 200
    assert contracts.MAX_CATALOG_WRITE_BATCH == 500
    # No environment variable or argument can move it: nothing reads one.
    source = Path(ingest_module.__file__).read_text(encoding="utf-8") + Path(
        contracts.__file__).read_text(encoding="utf-8")
    assert "os.environ" not in source and "getenv" not in source


def test_a_failed_guarded_rpc_logs_its_class_code_and_status_and_nothing_else(caplog):
    """Server logs name WHAT failed -- the chained cause's class, its PostgREST
    code and the HTTP status -- and never the message, details, hint or
    payload, which can quote SQL, URLs and credentials."""
    client = _ScriptedClient(*[_api_error("PGRST003")] * CATALOG_WRITE_ATTEMPTS)
    repository = _repository(client, [])
    import threading
    repository._http = threading.local()
    caplog.set_level("WARNING", logger="milo.repository")
    original = client.rpc

    def rpc_with_status(name, params):
        repository._http.status = 504
        return original(name, params)
    client.rpc = rpc_with_status
    with pytest.raises(RepositoryFailure):
        repository.activate_catalog_snapshot(RUN, ACTIVATION, **LEASE)
    lines = [record.getMessage() for record in caplog.records]
    assert len(lines) == CATALOG_WRITE_ATTEMPTS
    assert lines[0] == ("guarded rpc failed function=activate_catalog_snapshot_guarded "
                        "cause=APIError code=PGRST003 http_status=504 class=transient "
                        "attempt=1/4 action=retry")
    assert lines[-1].endswith("attempt=4/4 action=raise")
    rendered = "\n".join(lines)
    for secret in (SQL_SENTINEL, URL_SENTINEL, "LEAKED", "token", "{"):
        assert secret not in rendered


def test_a_transport_failure_is_logged_without_a_code():
    import logging

    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append
    logger = logging.getLogger("milo.repository")
    logger.addHandler(handler)
    try:
        client = _ScriptedClient(httpx.ConnectError("refused " + URL_SENTINEL))
        with pytest.raises(RepositoryFailure):
            _repository(client, []).heartbeat(RUN, "w", attempt=1, lease_token="t")
    finally:
        logger.removeHandler(handler)
    (line,) = [record.getMessage() for record in records]
    assert "cause=ConnectError code=none http_status=none class=transient" in line
    assert URL_SENTINEL not in line


@pytest.mark.parametrize("size", [0, 501])
def test_a_batch_outside_its_bound_is_refused_before_any_call(size):
    client = _ScriptedClient()
    with pytest.raises(AppError) as failure:
        _repository(client, []).record_catalog_raw_records(
            RUN, [_raw_record(i) for i in range(size)], **LEASE)
    assert failure.value.code == "CATALOG_BATCH_INVALID"
    assert client.calls == []


def test_an_adoption_refusal_is_one_static_code():
    client = _ScriptedClient(_api_error(
        "22023", "CATALOG_SNAPSHOT_ADOPTION_REFUSED: the owning run is live " + SQL_SENTINEL))
    snapshot = {"source_family": "government", "resource_id": src.WLTP_RESOURCE_ID,
                "upstream_version": "2026.09.1", "upstream_version_kind": "dataset_version",
                "content_sha256": "c" * 64, "retrieved_at": "2026-09-24T00:00:00Z",
                "declared_record_count": 6368, "retrieval_metadata": {}}
    with pytest.raises(AppError) as failure:
        _repository(client, []).adopt_catalog_snapshot(RUN, snapshot, **LEASE)
    assert failure.value.code == "CATALOG_SNAPSHOT_ADOPTION_REFUSED"
    assert SQL_SENTINEL not in failure.value.message
    assert len(client.calls) == 1


# =============================================================================
# 3. the operator report names the class, and never the text
# =============================================================================

@pytest.mark.parametrize("failure,reason", [
    (RepositoryFailure("transient"), "CAPTURE_REPOSITORY_TRANSIENT"),
    (RepositoryFailure("rejected"), "CAPTURE_REPOSITORY_REJECTED"),
    (RepositoryFailure("unavailable"), "CAPTURE_REPOSITORY_UNAVAILABLE"),
    (AppError("RUN_LEASE_LOST", "run lease is held by another worker", 409), "CAPTURE_LEASE_LOST"),
    (AppError("RUN_TRANSITION_CONFLICT", "run lease has expired", 409), "CAPTURE_LEASE_LOST"),
    (AppError("CATALOG_RECORD_IDEMPOTENCY_CONFLICT", SQL_SENTINEL, 409),
     "CAPTURE_REPOSITORY_REJECTED"),
    (AppError("REPOSITORY_ERROR", SQL_SENTINEL, 502), "CAPTURE_REPOSITORY_UNAVAILABLE"),
])
def test_the_capture_reports_a_static_reason_for_every_repository_failure(failure, reason):
    assert entrypoint._classify(failure) == reason
    assert reason in entrypoint.CAPTURE_REASONS
    assert SQL_SENTINEL not in entrypoint.safe_message(reason)


def test_every_repository_failure_class_has_a_capture_reason():
    from backend.errors import REPOSITORY_FAILURE_CLASSES
    assert set(entrypoint.REPOSITORY_FAILURE_REASONS) == set(REPOSITORY_FAILURE_CLASSES)
    assert set(entrypoint.REPOSITORY_FAILURE_REASONS.values()) <= set(entrypoint.CAPTURE_REASONS)


# =============================================================================
# 4. one failed heartbeat is not a lost lease
# =============================================================================

class _Heart:
    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)

    def heartbeat(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _supervisor(repository: Any, clock: _Clock) -> Any:
    lease = WorkerLease(run_id=RUN, worker_id="w", attempt=1, lease_token="t")
    return entrypoint._CaptureSupervisor(repository, lease, lease_seconds=300, interval=30.0,
                                         clock=clock)


def test_a_transient_heartbeat_failure_keeps_the_capture_going():
    clock = _Clock()
    supervisor = _supervisor(_Heart(RepositoryFailure("transient"), httpx.ReadTimeout("t"),
                                    {"status": "running"}), clock)
    assert supervisor.beat() is True and supervisor.should_stop() is False
    # A failed beat is retried sooner than the regular interval.
    assert supervisor._next_wait() == entrypoint._CaptureSupervisor.RETRY_INTERVAL_SECONDS
    clock.now += 5
    assert supervisor.beat() is True and supervisor.should_stop() is False
    clock.now += 5
    assert supervisor.beat() is True and supervisor.should_stop() is False
    assert supervisor._next_wait() == 30.0


def test_a_refused_heartbeat_is_a_lost_lease_at_once():
    supervisor = _supervisor(_Heart(AppError("RUN_LEASE_LOST", "lost", 409)), _Clock())
    assert supervisor.beat() is False
    assert supervisor.should_stop() and supervisor.stop_reason == "CAPTURE_LEASE_LOST"


def test_a_lease_that_can_no_longer_be_proved_is_lost():
    """No successful beat for the lease duration minus one interval: this
    process can no longer prove it holds the lease, so it stops."""
    clock = _Clock()
    supervisor = _supervisor(_Heart(*[httpx.ConnectError("x")] * 3), clock)
    clock.now += 200
    assert supervisor.beat() is True
    clock.now += 60                              # 260 s < 300 - 30
    assert supervisor.beat() is True
    clock.now += 10                              # 270 s: the margin is gone
    assert supervisor.beat() is False
    assert supervisor.stop_reason == "CAPTURE_LEASE_LOST"


def test_a_cancellation_is_still_observed_through_the_heartbeat():
    supervisor = _supervisor(_Heart({"status": "cancellation_requested"}), _Clock())
    assert supervisor.beat() is True
    assert supervisor.should_stop() and supervisor.stop_reason == "CAPTURE_CANCELLED"


# =============================================================================
# 5. activation marks `failed` only on a refusal
# =============================================================================

def _leased(repository: MemoryRepository, *, workflow_key: str = "operator_capture"
            ) -> WorkerLease:
    from tests.run_factory import identity_kwargs
    from tests.test_catalog_operator_capture import seed_conversation

    conversation_id, user_id = seed_conversation(repository)
    kwargs = identity_kwargs(repository, conversation_id,
                             workflow_key=None if workflow_key == "swarm_v2" else workflow_key)
    metadata = ({"milo_operation": "catalog.government.capture"}
                if workflow_key == "operator_capture" else {})
    run = repository.create_message_and_run(
        conversation_id, "capture", metadata, requested_by=user_id, idempotency_key=str(uuid4()),
        request_fingerprint="fp", **kwargs)["run"]
    worker = f"worker-{uuid4()}"
    claimed = repository.claim_run(UUID(run["id"]), worker)
    return WorkerLease(run_id=UUID(run["id"]), worker_id=worker,
                       attempt=int(claimed["attempt"]), lease_token=claimed["lease_token"])


def _client(records: list[dict[str, Any]]):
    from backend.catalog.government.client import DataGovClient
    return DataGovClient(FixtureTransport(bodies={0: whole_resource_page(records)}),
                         page_limit=entrypoint.CAPTURE_PAGE_LIMIT)


def _ingest(repository: Any, lease: WorkerLease, records: list[dict[str, Any]]):
    return GovernmentCatalogIngestor(repository, lease, client=_client(records)).ingest_resource(
        src.WLTP_RESOURCE_ID)


class _TransientActivation(MemoryRepository):
    def activate_catalog_snapshot(self, run_id, activation, **kwargs):
        if "validation_state" not in activation:
            raise RepositoryFailure("transient")
        return super().activate_catalog_snapshot(run_id, activation, **kwargs)


def test_a_transient_activation_failure_leaves_the_snapshot_pending_not_failed():
    """`failed` is terminal and the key is content-derived: marking it on a
    network blip would make that register content unactivatable for good."""
    repository = _TransientActivation()
    lease = _leased(repository)
    with pytest.raises(RepositoryFailure):
        _ingest(repository, lease, committed_records(5))
    snapshot = next(iter(repository.catalog_snapshots.values()))
    assert snapshot["validation_state"] == "pending" and snapshot["activated_at"] is None


# =============================================================================
# 6. ingestion writes in bounded batches
# =============================================================================

class _Counting(MemoryRepository):
    def __init__(self) -> None:
        super().__init__()
        self.batches: list[tuple[str, int]] = []
        self.singles = 0

    def record_catalog_raw_records(self, run_id, records, **kwargs):
        self.batches.append(("raw", len(records)))
        return super().record_catalog_raw_records(run_id, records, **kwargs)

    def record_catalog_candidates(self, run_id, candidates, **kwargs):
        self.batches.append(("candidate", len(candidates)))
        return super().record_catalog_candidates(run_id, candidates, **kwargs)

    def record_catalog_raw_record(self, *args, **kwargs):
        self.singles += 1
        return super().record_catalog_raw_record(*args, **kwargs)


def test_ingestion_writes_rows_in_bounded_batches_and_measures_them(monkeypatch):
    monkeypatch.setattr(ingest_module, "CATALOG_WRITE_BATCH_SIZE", 5)
    repository = _Counting()
    report = _ingest(repository, _leased(repository), committed_records(12))
    assert report.activated and report.stored_record_count == 12
    raw = [size for kind, size in repository.batches if kind == "raw"]
    assert raw == [5, 5, 2]
    assert all(size <= 5 for _kind, size in repository.batches)
    # The batch applies the single-row write to every row, in order.
    assert repository.singles == 12
    positions = sorted((row["source_locator"] or {}).get("capture_index")
                       for row in repository.catalog_raw_records.values())
    assert positions == list(range(12))
    metrics = report.ingestion
    assert (metrics["snapshot"]["calls"], metrics["raw"]["calls"],
            metrics["candidates"]["calls"], metrics["activate"]["calls"]) == (1, 3, 3, 1)
    assert (metrics["raw"]["inserted"], metrics["raw"]["already_present"]) == (12, 0)
    assert metrics["candidates"]["inserted"] == len(repository.catalog_candidates)
    assert metrics["total_calls"] == 8
    assert (metrics["adoption_seq"], metrics["previous_writer_run_id"]) == (0, "")
    assert all(metrics[phase]["seconds"] >= 0 for phase in ingest_module.INGESTION_PHASES)


# =============================================================================
# 7. adoption, at the ingestion level (memory repository parity)
# =============================================================================

def _expire(repository: MemoryRepository, run_id: Any, status: str) -> None:
    run = repository.runs[str(run_id)]
    run["status"] = status
    run["lease_expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()


class _FailingCandidates(MemoryRepository):
    """Candidate batches fail from the `fail_on`-th one: the 2026-09-24 shape,
    every raw record durable and only a PREFIX of the candidates."""
    fail = True
    fail_on = 1

    def __init__(self) -> None:
        super().__init__()
        self.candidate_batches = 0

    def record_catalog_candidates(self, run_id, candidates, **kwargs):
        self.candidate_batches += 1
        if self.fail and self.candidate_batches >= self.fail_on:
            raise RepositoryFailure("transient")
        return super().record_catalog_candidates(run_id, candidates, **kwargs)


def _candidate_identity(row: dict[str, Any]) -> tuple:
    return tuple(row[field] for field in ("id", "candidate_key", "raw_record_id", "manufacturer",
                                          "commercial_model", "model_year_start",
                                          "model_year_end", "official_model_code", "trim"))


def test_the_incident_replayed_in_the_memory_repository(monkeypatch):
    """All raw records and a prefix of the candidates durable, the writer
    failed with an expired lease: an operator capture run adopts, completes
    and activates. The pre-existing candidates keep their ids, keys and
    identity; the missing ones are added; nothing is counted twice; the
    snapshot's creator never changes; the adoption row and the run event are
    recorded."""
    monkeypatch.setattr(ingest_module, "CATALOG_WRITE_BATCH_SIZE", 5)
    repository = _FailingCandidates()
    repository.fail_on = 3                                  # 2 of 3 batches land
    records = committed_records(12)
    first = _leased(repository)
    with pytest.raises(RepositoryFailure):
        _ingest(repository, first, records)
    orphan = dict(next(iter(repository.catalog_snapshots.values())))
    before = {key: _candidate_identity(row) for key, row in repository.catalog_candidates.items()}
    assert orphan["stored_record_count"] == 12 and orphan["activated_at"] is None
    assert 0 < len(before) < 12

    repository.fail = False
    second = _leased(repository)
    # While the writer is live, nothing is adopted.
    with pytest.raises(GovernmentIngestionError) as refusal:
        _ingest(repository, second, records)
    assert refusal.value.reason_code == "GOV_SNAPSHOT_OWNED_BY_ANOTHER_RUN"
    assert repository.catalog_snapshot_adoptions == []

    _expire(repository, first.run_id, "failed")
    report = _ingest(repository, second, records)
    snapshot = next(iter(repository.catalog_snapshots.values()))
    assert report.activated and snapshot["activated_at"] is not None
    assert report.stored_record_count == snapshot["stored_record_count"] == 12
    assert len(repository.catalog_raw_records) == 12 and len(repository.catalog_snapshots) == 1
    # The creator is provenance and never moves; the adoption made `second`
    # the writer.
    assert snapshot["created_by_run_id"] == report.created_by_run_id == str(first.run_id)
    assert report.adopted_from_run_id == str(first.run_id) and report.adoption_seq == 1
    after = {key: _candidate_identity(row) for key, row in repository.catalog_candidates.items()}
    assert {key: after[key] for key in before} == before
    assert len(after) == 12
    metrics = report.ingestion
    assert (metrics["raw"]["inserted"], metrics["raw"]["already_present"]) == (0, 12)
    assert (metrics["candidates"]["inserted"], metrics["candidates"]["already_present"]) == \
        (12 - len(before), len(before))
    assert (metrics["adoption_seq"], metrics["previous_writer_run_id"]) == (1, str(first.run_id))
    (adoption,) = repository.catalog_snapshot_adoptions
    assert {key: adoption[key] for key in ("snapshot_id", "adoption_seq", "adopted_by_run_id",
                                           "previous_writer_run_id")} == \
        {"snapshot_id": snapshot["id"], "adoption_seq": 1,
         "adopted_by_run_id": str(second.run_id), "previous_writer_run_id": str(first.run_id)}
    (event,) = [event for event in repository.run_events
                if event["event_type"] == "catalog_snapshot_adopted"]
    assert event["run_id"] == str(second.run_id)
    assert event["payload"]["previous_writer_run_id"] == str(first.run_id)
    assert event["payload"]["adoption_seq"] == 1
    # The CREATOR is no longer the writer: the authority names the adopter.
    with pytest.raises(AppError) as refused:
        repository._catalog_write_authority(snapshot["id"], first.run_id, allow_decided=True)
    assert refused.value.message == "this catalog snapshot does not belong to this run"
    assert repository._catalog_write_authority(snapshot["id"], second.run_id,
                                               allow_decided=True)["id"] == snapshot["id"]


def test_the_write_authority_follows_the_latest_adoption():
    repository = _FailingCandidates()
    records = committed_records(4)
    first = _leased(repository)
    with pytest.raises(RepositoryFailure):
        _ingest(repository, first, records)
    _expire(repository, first.run_id, "failed")
    second = _leased(repository)
    with pytest.raises(RepositoryFailure):                 # second fails too
        _ingest(repository, second, records)
    _expire(repository, second.run_id, "cancelled")
    repository.fail = False
    third = _leased(repository)
    report = _ingest(repository, third, records)
    assert report.activated and report.adoption_seq == 2
    assert report.adopted_from_run_id == str(second.run_id)
    assert [(row["adoption_seq"], row["previous_writer_run_id"], row["adopted_by_run_id"])
            for row in repository.catalog_snapshot_adoptions] == [
        (1, str(first.run_id), str(second.run_id)), (2, str(second.run_id), str(third.run_id))]
    snapshot = next(iter(repository.catalog_snapshots.values()))
    assert snapshot["created_by_run_id"] == str(first.run_id)


def test_only_an_operator_capture_run_adopts():
    repository = _FailingCandidates()
    records = committed_records(4)
    first = _leased(repository)
    with pytest.raises(RepositoryFailure):
        _ingest(repository, first, records)
    _expire(repository, first.run_id, "failed")
    repository.fail = False
    paid = _leased(repository, workflow_key="swarm_v2")
    with pytest.raises(GovernmentIngestionError) as refusal:
        _ingest(repository, paid, records)
    assert refusal.value.reason_code == "GOV_SNAPSHOT_OWNED_BY_ANOTHER_RUN"
    assert repository.catalog_snapshot_adoptions == []


def test_a_failed_orphan_is_never_adopted():
    repository = _FailingCandidates()
    records = committed_records(4)
    owner = _leased(repository)
    with pytest.raises(RepositoryFailure):
        _ingest(repository, owner, records)
    snapshot = next(iter(repository.catalog_snapshots.values()))
    repository.activate_catalog_snapshot(owner.run_id, {"snapshot_id": snapshot["id"],
                                                        "validation_state": "failed"},
                                         worker_id=owner.worker_id, attempt=owner.attempt,
                                         lease_token=owner.lease_token)
    _expire(repository, owner.run_id, "failed")
    repository.fail = False
    with pytest.raises(GovernmentIngestionError) as refusal:
        _ingest(repository, _leased(repository), records)
    assert refusal.value.reason_code == "GOV_SNAPSHOT_OWNED_BY_ANOTHER_RUN"
    assert repository.catalog_snapshot_adoptions == []


# =============================================================================
# 8. the incident, replayed through the operator entrypoint
# =============================================================================

def test_the_incident_replayed_a_failed_capture_is_adopted_by_the_next_one(monkeypatch, capsys):
    """Run 1 lands every raw record, fails transiently on the candidates and
    reports exactly that. Run 2 -- a new prepared run of the next release --
    captures the same content, ADOPTS the pending snapshot, finishes it and
    activates it; the report names the run it adopted from."""
    repository = _FailingCandidates()
    records = committed_records(12)
    monkeypatch.setattr(entrypoint, "_open_transport",
                        lambda: FixtureTransport(bodies={0: whole_resource_page(records)}))
    monkeypatch.setattr(entrypoint, "_open_repository", lambda: repository)

    first = prepare_run(repository, capsys)
    status, document, stderr = run_main(authorized_argv(first), capture_env(), capsys)
    assert status == entrypoint.EXIT_FAILED
    assert document["reason_code"] == "CAPTURE_REPOSITORY_TRANSIENT"
    assert "CAPTURE_REPOSITORY_TRANSIENT" in stderr
    assert repository.runs[str(first)]["status"] == "failed"
    orphan = next(iter(repository.catalog_snapshots.values()))
    assert (orphan["stored_record_count"], orphan["activated_at"]) == (12, None)

    # The failed run's lease runs out (at most MILO_WORKER_LEASE_SECONDS).
    _expire(repository, first, "failed")
    repository.fail = False
    second = prepare_run(repository, capsys)
    status, document, _stderr = run_main(authorized_argv(second), capture_env(), capsys)
    assert status == entrypoint.EXIT_OK, document
    snapshot = document["capture"]["snapshot"]
    assert snapshot["adopted_from_run_id"] == str(first)
    assert snapshot["activated"] is True and snapshot["stored_record_count"] == 12
    assert snapshot["snapshot_key"] == orphan["snapshot_key"]
    assert len(repository.catalog_raw_records) == 12
    assert repository.runs[str(second)]["status"] == "completed"
    # The measurement the operator reads, counts and seconds only.
    ingestion = snapshot["ingestion"]
    assert (ingestion["adoption_seq"], ingestion["previous_writer_run_id"]) == (1, str(first))
    assert (ingestion["raw"]["inserted"], ingestion["raw"]["already_present"]) == (0, 12)
    assert ingestion["raw"]["calls"] == 1 and ingestion["activate"]["calls"] == 1
    assert ingestion["snapshot"]["calls"] == 2              # the snapshot write and the adoption
    assert ingestion["total_calls"] == sum(ingestion[phase]["calls"]
                                           for phase in ("snapshot", "raw", "candidates", "activate"))


# =============================================================================
# 9. the static guard: one write authority
# =============================================================================

def _check_migrations():
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "scripts" / "check_migrations.py"
    spec = importlib.util.spec_from_file_location("check_migrations", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_committed_migrations_decide_snapshot_ownership_in_one_place():
    module = _check_migrations()
    migrations = Path(__file__).resolve().parents[1] / "supabase" / "migrations"
    texts = {path.name: path.read_text(encoding="utf-8").lower()
             for path in migrations.glob("*.sql")}
    assert module.ownership_check_problems(texts) == []
    # The authority itself does hold the check it is the home of.
    recovery = texts["20260924000200_catalog_ingestion_recovery.sql"]
    assert "created_by_run_id is distinct from p_run_id" in recovery


@pytest.mark.parametrize("check", [
    "if v_snapshot.created_by_run_id is distinct from p_run_id then",
    "if v_snapshot.created_by_run_id <> p_run_id then",
    "if p_run_id is distinct from v_snapshot.created_by_run_id then",
    "where created_by_run_id = p_run_id",
])
def test_a_direct_ownership_check_outside_the_authority_is_refused(check):
    module = _check_migrations()
    text = ("create or replace function public.assert_snapshot_write_authority(p uuid)\n"
            "returns void language plpgsql as $$ begin "
            "if v_snapshot.created_by_run_id is distinct from p_run_id then null; end if; "
            "end; $$;\n"
            "create or replace function public.some_new_write(p uuid) returns void "
            f"language plpgsql as $$ begin {check} null; end if; end; $$;\n")
    problems = module.ownership_check_problems({"20260930000100_new.sql": text})
    assert len(problems) == 1 and "outside assert_snapshot_write_authority" in problems[0]
    # Migrations before the authority existed are history, not checked.
    assert module.ownership_check_problems({"20260915120000_old.sql": text}) == []


def test_the_real_client_reports_the_http_status_of_a_failed_rpc(caplog):
    """End to end through supabase-py: the response hook sees the HTTP status
    PostgREST answered, which its JSON error body does not carry."""
    class _Settings:
        supabase_url = "https://abcdefghijklmnopqrst.supabase.co"
        supabase_service_role_key = "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoic2VydmljZV9yb2xlIn0.x"

    repository = SupabaseRepository(_Settings())
    session = repository.client.postgrest.session
    answers = iter([httpx.Response(503, text="<html>gateway " + URL_SENTINEL + "</html>"),
                    httpx.Response(400, json={"code": "22023", "message": SQL_SENTINEL,
                                              "details": URL_SENTINEL, "hint": None})])
    session._transport = httpx.MockTransport(lambda request: next(answers))
    session._mounts = {}                  # no proxy between the client and the mock
    repository._retry_sleep = lambda _seconds: None
    caplog.set_level("WARNING", logger="milo.repository")
    with pytest.raises(RepositoryFailure) as failure:
        repository.activate_catalog_snapshot(RUN, ACTIVATION, **LEASE)
    assert failure.value.failure_class == "rejected"
    lines = [record.getMessage() for record in caplog.records]
    assert "cause=APIError code=503 http_status=503 class=transient attempt=1/4 action=retry" in lines[0]
    assert "cause=APIError code=22023 http_status=400 class=rejected attempt=2/4 action=raise" in lines[1]
    assert SQL_SENTINEL not in "\n".join(lines) and URL_SENTINEL not in "\n".join(lines)

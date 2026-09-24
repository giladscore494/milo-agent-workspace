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
    client = _ScriptedClient(rows)
    repository = _repository(client, [])
    assert repository.record_catalog_raw_records(RUN, [_raw_record(i) for i in range(3)],
                                                 **LEASE) == rows
    (name, params), = client.calls
    assert name == "record_catalog_raw_records_batch_guarded"
    assert len(params["p_records"]) == 3
    assert all(record["record_key"].startswith("cr1.") for record in params["p_records"])
    # A short answer is never taken as a complete batch.
    short = _repository(_ScriptedClient(rows[:2]), [])
    with pytest.raises(RepositoryFailure):
        short.record_catalog_raw_records(RUN, [_raw_record(i) for i in range(3)], **LEASE)


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


def test_ingestion_writes_rows_in_bounded_batches(monkeypatch):
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


# =============================================================================
# 7. adoption, at the ingestion level
# =============================================================================

def _expire(repository: MemoryRepository, run_id: Any, status: str) -> None:
    run = repository.runs[str(run_id)]
    run["status"] = status
    run["lease_expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()


class _FailingCandidates(MemoryRepository):
    fail = True

    def record_catalog_candidates(self, run_id, candidates, **kwargs):
        if self.fail:
            raise RepositoryFailure("transient")
        return super().record_catalog_candidates(run_id, candidates, **kwargs)


def test_an_orphan_is_adopted_and_finished_without_a_duplicate_row():
    repository = _FailingCandidates()
    records = committed_records(12)
    first = _leased(repository)
    with pytest.raises(RepositoryFailure):
        _ingest(repository, first, records)
    orphan = next(iter(repository.catalog_snapshots.values()))
    assert orphan["stored_record_count"] == 12 and orphan["activated_at"] is None

    repository.fail = False
    second = _leased(repository)
    # While the owner is live, nothing is adopted.
    with pytest.raises(GovernmentIngestionError) as refusal:
        _ingest(repository, second, records)
    assert refusal.value.reason_code == "GOV_SNAPSHOT_OWNED_BY_ANOTHER_RUN"

    _expire(repository, first.run_id, "failed")
    report = _ingest(repository, second, records)
    assert report.adopted_from_run_id == str(first.run_id)
    assert report.created_by_run_id == str(second.run_id)
    assert report.activated and report.stored_record_count == 12
    assert len(repository.catalog_raw_records) == 12
    assert len(repository.catalog_snapshots) == 1
    assert repository.catalog_snapshot_adoptions == [{
        **repository.catalog_snapshot_adoptions[0],
        "snapshot_id": orphan["id"], "previous_run_id": str(first.run_id),
        "adopted_by_run_id": str(second.run_id), "previous_run_status": "failed",
        "stored_record_count_at_adoption": 12}]


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

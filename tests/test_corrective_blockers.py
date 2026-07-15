from types import SimpleNamespace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from backend.budget import BudgetConfig, BudgetExceeded, BudgetTracker, build_guarded_client_factory
from backend.gateway_auth import verify_gateway_token
from backend.errors import AppError
from backend.model_pricing import calculate_model_cost
from backend.testing.memory_repository import MemoryRepository
from backend.worker.main import execute_run


class CompleteEngine:
    workflow_key = "vehicle_catalog_v1"
    def __init__(self):
        self.calls = 0
    def run(self, run):
        self.calls += 1
        return {"status": "success", "result": {"ok": True}}


def test_cancelled_before_start_emits_no_run_started_or_engine_call(monkeypatch):
    monkeypatch.setenv("MILO_WORKER_HEARTBEAT_INTERVAL_SECONDS", "1")
    repo = MemoryRepository()
    project_id = uuid4()
    repo.seed_project(str(project_id), "cancel-fixture", "Cancel fixture", [])
    conversation = repo.create_conversation(project_id, "Cancel fixture")
    run_id = uuid4()
    conversation_id = conversation["id"]
    repo.runs[str(run_id)] = {"id": str(run_id), "conversation_id": str(conversation_id), "status": "cancellation_requested", "input": {"content": "x"}, "attempt": 1}
    engine = CompleteEngine()
    assert execute_run(run_id, repo, engine=engine) == 0
    assert engine.calls == 0
    assert repo.runs[str(run_id)]["status"] == "cancelled"
    assert [e["event_type"] for e in repo.run_events].count("run_cancelled") == 1
    assert "run_started" not in [e["event_type"] for e in repo.run_events]


def _seed_memory_run(status="queued"):
    repo = MemoryRepository()
    project_id = uuid4()
    repo.seed_project(str(project_id), "lease-fixture", "Lease fixture", [])
    conversation = repo.create_conversation(project_id, "Lease fixture")
    run_id = uuid4()
    repo.runs[str(run_id)] = {"id": str(run_id), "conversation_id": conversation["id"], "status": status, "input": {"content": "x"}, "attempt": 1}
    return repo, run_id


def test_memory_repository_first_claim_assigns_token_and_guards_mutations():
    repo, run_id = _seed_memory_run("queued")
    claimed = repo.claim_run(run_id, "worker-a", lease_seconds=300)
    assert claimed["worker_id"] == "worker-a"
    assert claimed["attempt"] == 1
    assert claimed["lease_token"]
    assert repo.runs[str(run_id)]["lease_token"] == claimed["lease_token"]
    repo.heartbeat(run_id, "worker-a", attempt=claimed["attempt"], lease_token=claimed["lease_token"])
    with pytest.raises(AppError, match="lease token"):
        repo.heartbeat(run_id, "worker-a", attempt=claimed["attempt"], lease_token="wrong-token")
    with pytest.raises(AppError, match="lease token"):
        repo.transition_run(run_id, "running", expected_worker_id="worker-a", expected_attempt=claimed["attempt"], expected_lease_token="wrong-token")


def test_memory_repository_can_claim_cancellation_requested_run():
    repo, run_id = _seed_memory_run("cancellation_requested")
    claimed = repo.claim_run(run_id, "worker-a", lease_seconds=300)
    assert claimed["status"] == "cancellation_requested"
    assert claimed["lease_token"]


def test_memory_repository_reclaim_increments_attempt_and_invalidates_old_token():
    repo, run_id = _seed_memory_run("queued")
    first = repo.claim_run(run_id, "worker-a", lease_seconds=300)
    repo.runs[str(run_id)]["lease_expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    reclaimed = repo.claim_run(run_id, "worker-b", lease_seconds=300)
    assert reclaimed["worker_id"] == "worker-b"
    assert reclaimed["attempt"] == first["attempt"] + 1
    assert reclaimed["lease_token"] and reclaimed["lease_token"] != first["lease_token"]
    with pytest.raises(AppError):
        repo.transition_run(run_id, "running", expected_worker_id="worker-a", expected_attempt=first["attempt"], expected_lease_token=first["lease_token"])
    repo.transition_run(run_id, "running", expected_worker_id="worker-b", expected_attempt=reclaimed["attempt"], expected_lease_token=reclaimed["lease_token"])


def test_actual_cost_calculated_and_settled(monkeypatch):
    monkeypatch.setenv("MILO_ENABLE_PAID_EXECUTION", "true")
    tracker = BudgetTracker(BudgetConfig(max_model_calls_per_run=2, max_total_tokens_per_run=1000, max_estimated_cost_per_run=1, max_run_duration_seconds=60, max_retries=1), kill_switch=lambda: True)
    class Inner:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50))
    client = build_guarded_client_factory(tracker, lambda *_: Inner())("k", "u")
    client.chat.completions.create(model="kimi-k2.6", messages=[{"content":"hello"}], max_tokens=100)
    assert tracker.actual_cost == calculate_model_cost("kimi-k2.6", 100, 50)


@pytest.mark.parametrize("field,limit", [("max_output_tokens_per_run", 1), ("max_total_tokens_per_run", 2), ("max_cost_per_run", 0.000001)])
def test_final_call_actual_overage_sets_stop(field, limit):
    cfg = BudgetConfig(max_model_calls_per_run=2, max_estimated_cost_per_run=1, max_run_duration_seconds=60, max_retries=1, **{field: limit})
    tracker = BudgetTracker(cfg, kill_switch=lambda: True)
    tracker.reserve_call(1, 10)
    with pytest.raises(BudgetExceeded):
        tracker.settle_call(1, 10, input_tokens=2, output_tokens=10, cost=1.0)
    assert tracker.stop is not None


def test_retry_cap_blocks_repeated_provider_call(monkeypatch):
    tracker = BudgetTracker(BudgetConfig(max_model_calls_per_run=3, max_total_tokens_per_run=100, max_estimated_cost_per_run=1, max_run_duration_seconds=60, max_retries=0), kill_switch=lambda: True)
    calls = {"n": 0}
    class FailingCompletions:
        def create(self, **kwargs):
            calls["n"] += 1
            raise RuntimeError("boom")
    inner = SimpleNamespace(chat=SimpleNamespace(completions=FailingCompletions()))
    client = build_guarded_client_factory(tracker, lambda *_: inner)("k", "u")
    with pytest.raises(BudgetExceeded):
        client.chat.completions.create(model="kimi", messages=[{"content":"x"}], max_tokens=5)
    with pytest.raises(BudgetExceeded):
        client.chat.completions.create(model="kimi", messages=[{"content":"x"}], max_tokens=5)
    assert calls["n"] == 1


def test_gateway_fails_closed_without_explicit_dev_opt_in(monkeypatch):
    monkeypatch.delenv("MILO_GATEWAY_AUDIENCE", raising=False)
    monkeypatch.delenv("MILO_APPROVED_GATEWAY_IDENTITIES", raising=False)
    monkeypatch.delenv("MILO_ALLOW_INSECURE_DEV_IDENTITY", raising=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    with pytest.raises(AppError) as exc:
        verify_gateway_token(None, object())
    assert exc.value.code == "GATEWAY_AUTH_NOT_CONFIGURED"


def test_gateway_explicit_dev_opt_in_and_production_forbidden(monkeypatch):
    monkeypatch.delenv("MILO_GATEWAY_AUDIENCE", raising=False)
    monkeypatch.delenv("MILO_APPROVED_GATEWAY_IDENTITIES", raising=False)
    monkeypatch.setenv("MILO_ALLOW_INSECURE_DEV_IDENTITY", "true")
    monkeypatch.setenv("ENVIRONMENT", "test")
    assert verify_gateway_token(None, object()) is None
    monkeypatch.setenv("ENVIRONMENT", "production")
    with pytest.raises(AppError) as exc:
        verify_gateway_token(None, object())
    assert exc.value.code == "INSECURE_DEV_IDENTITY_FORBIDDEN"

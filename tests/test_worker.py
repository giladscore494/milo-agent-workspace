from uuid import uuid4
from datetime import UTC, datetime, timedelta
import pytest
from backend.errors import AppError
from backend.worker.main import execute_run, resolve_run_id


class WorkerRepo:
    def __init__(self):
        self.run_id = uuid4()
        self.worker_id = None
        self.attempt = 1
        self.lease_token = "test-lease-token"
        self.lease_expires_at = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
        self.failed = None
        self.completed = None
        self.events = []
        self.partial = None
    def get_run(self, run_id):
        return {"id": run_id, "status": "running" if self.worker_id else "queued", "input": {}, "worker_id": self.worker_id, "attempt": self.attempt, "lease_token": self.lease_token if self.worker_id else None, "lease_expires_at": self.lease_expires_at}
    def _assert_lease(self, worker_id=None, attempt=None, lease_token=None):
        if worker_id is not None and worker_id != self.worker_id:
            raise AppError("RUN_LEASE_LOST", "wrong worker", 409)
        if attempt is not None and attempt != self.attempt:
            raise AppError("RUN_LEASE_LOST", "wrong attempt", 409)
        if lease_token is not None and lease_token != self.lease_token:
            raise AppError("RUN_LEASE_LOST", "wrong lease token", 409)
        if datetime.fromisoformat(self.lease_expires_at) <= datetime.now(UTC):
            raise AppError("RUN_LEASE_LOST", "expired lease", 409)
    def claim_run(self, run_id, worker_id, lease_seconds=300):
        self.worker_id = worker_id
        self.lease_token = f"lease-{uuid4()}"
        self.lease_expires_at = (datetime.now(UTC) + timedelta(seconds=lease_seconds)).isoformat()
        return self.get_run(run_id)
    def heartbeat(self, run_id, worker_id, lease_seconds=300, attempt=None, lease_token=None):
        self._assert_lease(worker_id, attempt, lease_token)
        self.lease_expires_at = (datetime.now(UTC) + timedelta(seconds=lease_seconds)).isoformat()
        return self.get_run(run_id)
    def append_run_event(self, run_id, event_type, payload):
        self.events.append((run_id, event_type, payload)); return {"id": uuid4(), "run_id": run_id, "event_type": event_type, "payload": payload}
    def transition_run(self, run_id, status, expected_worker_id=None, expected_attempt=None, expected_lease_token=None, **fields):
        self._assert_lease(expected_worker_id, expected_attempt, expected_lease_token)
        if status == "partial_success":
            self.partial = (run_id, fields.get("output"))
        return {"id": run_id, "status": status, **fields}
    def save_checkpoint(self, checkpoint):
        self._assert_lease(checkpoint.get("worker_id"), checkpoint.get("attempt"), checkpoint.get("lease_token"))
        return checkpoint
    def mark_run_failed(self, run_id, code, message, worker_id=None):
        self._assert_lease(worker_id)
        self.failed = (run_id, code, message); return {"id": run_id, "status": "failed", "error": {"code": code, "message": message}}
    def mark_run_complete(self, run_id, output, worker_id=None):
        self._assert_lease(worker_id)
        self.completed = (run_id, output); return {"id": run_id, "status": "completed", "output": output}


def test_missing_run_id(monkeypatch):
    monkeypatch.delenv("RUN_ID", raising=False)
    with pytest.raises(AppError) as exc:
        resolve_run_id(None)
    assert exc.value.code == "MISSING_RUN_ID"


def test_worker_marks_complete_for_valid_partial_result():
    class FakeEngine:
        workflow_key = "vehicle_catalog_v1"
        def run(self, run):
            return {"status": "partial_success", "result": {"models": []}}
    repo = WorkerRepo()
    code = execute_run(repo.run_id, repo, FakeEngine())
    assert code == 0
    assert repo.partial[1]["status"] == "partial_success"
    event_types = [event[1] for event in repo.events]
    assert event_types[0] == "run_started"
    assert event_types[-1] == "run_partial_success"
    assert "supervisor_shadow_failed" not in event_types
    assert "run_failed" not in event_types
    assert "agent_failed" not in event_types


def test_worker_shadow_failure_is_observable_without_false_run_failure():
    class FailingSupervisorRepo(WorkerRepo):
        def create_supervisor_decision(self, run_id, decision):
            raise RuntimeError("supervisor decision storage unavailable")
    class FakeEngine:
        workflow_key = "vehicle_catalog_v1"
        def run(self, run):
            return {"status": "partial_success", "result": {"models": []}}
    repo = FailingSupervisorRepo()
    code = execute_run(repo.run_id, repo, FakeEngine())
    assert code == 0
    assert repo.partial[1]["status"] == "partial_success"
    event_types = [event[1] for event in repo.events]
    assert event_types[0] == "run_started"
    assert "run_partial_success" in event_types
    assert "supervisor_shadow_failed" in event_types
    assert "run_failed" not in event_types
    assert "agent_failed" not in event_types


def test_worker_marks_failed_for_invalid_engine_result():
    class FakeEngine:
        workflow_key = "vehicle_catalog_v1"
        def run(self, run):
            return {"status": "failed", "error": {"code": "X", "message": "bad"}}
    repo = WorkerRepo()
    code = execute_run(repo.run_id, repo, FakeEngine())
    assert code == 1
    assert repo.failed[1] == "X"

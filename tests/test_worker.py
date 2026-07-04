from uuid import uuid4
import pytest
from backend.errors import AppError
from backend.worker.main import execute_run, resolve_run_id


class WorkerRepo:
    def __init__(self):
        self.run_id = uuid4()
        self.failed = None
        self.completed = None
        self.events = []
    def get_run(self, run_id):
        return {"id": run_id, "status": "queued", "input": {}}
    def append_run_event(self, run_id, event_type, payload):
        self.events.append((run_id, event_type, payload)); return {"id": uuid4(), "run_id": run_id, "event_type": event_type, "payload": payload}
    def mark_run_failed(self, run_id, code, message):
        self.failed = (run_id, code, message); return {"id": run_id, "status": "failed", "error": {"code": code, "message": message}}
    def mark_run_complete(self, run_id, output):
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
    assert repo.completed[1]["status"] == "partial_success"
    event_types = [event[1] for event in repo.events]
    assert event_types[0] == "run_started"
    assert event_types[-1] == "run_partial_success"


def test_worker_marks_failed_for_invalid_engine_result():
    class FakeEngine:
        workflow_key = "vehicle_catalog_v1"
        def run(self, run):
            return {"status": "failed", "error": {"code": "X", "message": "bad"}}
    repo = WorkerRepo()
    code = execute_run(repo.run_id, repo, FakeEngine())
    assert code == 1
    assert repo.failed[1] == "X"

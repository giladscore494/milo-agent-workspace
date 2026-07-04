from uuid import uuid4
import pytest
from backend.errors import AppError
from backend.worker.main import execute_run, resolve_run_id


class WorkerRepo:
    def __init__(self):
        self.run_id = uuid4()
        self.failed = None
        self.events = []
    def get_run(self, run_id):
        return {"id": run_id, "status": "queued", "input": {}}
    def append_run_event(self, run_id, event_type, payload):
        self.events.append((run_id, event_type, payload)); return {"id": uuid4(), "run_id": run_id, "event_type": event_type, "payload": payload}
    def mark_run_failed(self, run_id, code, message):
        self.failed = (run_id, code, message); return {"id": run_id, "status": "failed", "error": {"code": code, "message": message}}


def test_missing_run_id(monkeypatch):
    monkeypatch.delenv("RUN_ID", raising=False)
    with pytest.raises(AppError) as exc:
        resolve_run_id(None)
    assert exc.value.code == "MISSING_RUN_ID"


def test_engine_not_integrated_fails_safely():
    repo = WorkerRepo()
    code = execute_run(repo.run_id, repo)
    assert code == 1
    assert repo.failed[1] == "ENGINE_NOT_INTEGRATED"
    assert repo.events[0][1] == "ENGINE_NOT_INTEGRATED"

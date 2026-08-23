from uuid import uuid4
from datetime import UTC, datetime, timedelta
import pytest
from backend.errors import AppError
from backend.worker.main import execute_run, resolve_run_id


class WorkerRepo:
    def __init__(self):
        self.run_id = uuid4()
        self.conversation_id = uuid4()
        self.project_id = uuid4()
        self.worker_id = None
        self.attempt = 1
        self.lease_token = "test-lease-token"
        self.lease_expires_at = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
        self.failed = None
        self.completed = None
        self.events = []
        self.partial = None
    def get_run(self, run_id):
        return {"id": run_id, "conversation_id": self.conversation_id, "status": "running" if self.worker_id else "queued", "input": {}, "worker_id": self.worker_id, "attempt": self.attempt, "lease_token": self.lease_token if self.worker_id else None, "lease_expires_at": self.lease_expires_at}
    def get_conversation(self, conversation_id):
        assert conversation_id == self.conversation_id
        return {"id": conversation_id, "project_id": self.project_id}
    def get_project(self, project_id):
        assert project_id == self.project_id
        return {"id": project_id, "workflow_key": "vehicle_catalog_v1"}
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
    def append_run_event(self, run_id, event_type, payload, worker_id=None, attempt=None, lease_token=None):
        self.events.append((run_id, event_type, payload)); return {"id": uuid4(), "run_id": run_id, "event_type": event_type, "payload": payload}
    def transition_run(self, run_id, status, expected_worker_id=None, expected_attempt=None, expected_lease_token=None, **fields):
        self._assert_lease(expected_worker_id, expected_attempt, expected_lease_token)
        if status == "partial_success":
            self.partial = (run_id, fields.get("output"))
        return {"id": run_id, "status": status, **fields}
    def save_checkpoint(self, checkpoint, worker_id=None, attempt=None, lease_token=None):
        self._assert_lease(worker_id, attempt, lease_token)
        return checkpoint
    def mark_run_failed(self, run_id, code, message, worker_id=None, attempt=None, lease_token=None):
        self._assert_lease(worker_id)
        self.failed = (run_id, code, message); return {"id": run_id, "status": "failed", "error": {"code": code, "message": message}}
    def mark_run_complete(self, run_id, output, worker_id=None, attempt=None, lease_token=None):
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


def test_worker_passes_active_lease_on_every_durable_write():
    """Every worker-originated durable write must carry the active lease
    (worker_id, attempt, lease_token). A repo that rejects lease-less writes
    proves the worker never writes without its lease context."""

    class LeaseRequiringRepo(WorkerRepo):
        def __init__(self):
            super().__init__()
            self.checkpoints = []
            self.usage = None

        def _require_full_lease(self, worker_id, attempt, lease_token, what):
            assert worker_id == self.worker_id, f"{what}: missing/wrong worker_id"
            assert attempt == self.attempt, f"{what}: missing/wrong attempt"
            assert lease_token == self.lease_token, f"{what}: missing/wrong lease_token"

        def append_run_event(self, run_id, event_type, payload, worker_id=None, attempt=None, lease_token=None):
            self._require_full_lease(worker_id, attempt, lease_token, f"event {event_type}")
            return super().append_run_event(run_id, event_type, payload, worker_id, attempt, lease_token)

        def save_checkpoint(self, checkpoint, worker_id=None, attempt=None, lease_token=None):
            self._require_full_lease(worker_id, attempt, lease_token, "checkpoint")
            self.checkpoints.append(checkpoint)
            return checkpoint

        def update_run_usage(self, run_id, usage, worker_id=None, attempt=None, lease_token=None):
            self._require_full_lease(worker_id, attempt, lease_token, "usage")
            self.usage = usage
            return {"id": run_id, "usage": usage}

        def mark_run_complete(self, run_id, output, worker_id=None, attempt=None, lease_token=None):
            self._require_full_lease(worker_id, attempt, lease_token, "complete")
            return super().mark_run_complete(run_id, output, worker_id, attempt, lease_token)

        def mark_run_failed(self, run_id, code, message, worker_id=None, attempt=None, lease_token=None):
            self._require_full_lease(worker_id, attempt, lease_token, "fail")
            return super().mark_run_failed(run_id, code, message, worker_id, attempt, lease_token)

    class CheckpointingEngine:
        workflow_key = "vehicle_catalog_v1"
        def __init__(self):
            self.checkpoint_sink = None
        def run(self, run):
            return {"status": "success", "result": {"models": []}}

    repo = LeaseRequiringRepo()
    code = execute_run(repo.run_id, repo, CheckpointingEngine())
    assert code == 0
    assert repo.completed is not None
    assert [event[1] for event in repo.events][0] == "run_started"


def test_stale_worker_every_mutation_rejected_via_memory_repository():
    """Unit-level mirror of the PostgreSQL stale-worker scenario: worker A's
    lease is reclaimed by worker B; each of A's writes raises, B's succeed."""
    from datetime import datetime, UTC, timedelta
    from backend.testing.memory_repository import MemoryRepository

    repo = MemoryRepository()
    repo.seed_user("aaaaaaaa-1111-4111-8111-000000000001")
    repo.seed_project("bbbbbbbb-1111-4111-8111-000000000001", "stale", "Stale", ["aaaaaaaa-1111-4111-8111-000000000001"])
    conversation = repo.create_conversation("bbbbbbbb-1111-4111-8111-000000000001", "stale run")
    created = repo.create_message_and_run(conversation["id"], "content", {}, "aaaaaaaa-1111-4111-8111-000000000001", "stale-key", "fp")
    run_id = created["run"]["id"]

    run_a = repo.claim_run(run_id, "worker-A")
    lease_a = {"worker_id": "worker-A", "attempt": run_a["attempt"], "lease_token": run_a["lease_token"]}
    # Live worker A writes succeed.
    repo.append_run_event(run_id, "run_started", {}, **lease_a)
    repo.save_checkpoint({"run_id": str(run_id), "phase": "fetch"}, **lease_a)
    repo.update_run_usage(run_id, {"calls": 1}, **lease_a)

    # Lease expires; worker B reclaims with a new attempt/token.
    repo.runs[str(run_id)]["lease_expires_at"] = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    run_b = repo.claim_run(run_id, "worker-B")
    lease_b = {"worker_id": "worker-B", "attempt": run_b["attempt"], "lease_token": run_b["lease_token"]}
    assert run_b["attempt"] == run_a["attempt"] + 1

    for call in [
        lambda: repo.append_run_event(run_id, "run_started", {}, **lease_a),
        lambda: repo.save_checkpoint({"run_id": str(run_id), "phase": "fetch"}, **lease_a),
        lambda: repo.update_run_usage(run_id, {"stale": True}, **lease_a),
        lambda: repo.heartbeat(run_id, "worker-A", attempt=lease_a["attempt"], lease_token=lease_a["lease_token"]),
        lambda: repo.mark_run_complete(run_id, {"stale": True}, **lease_a),
        lambda: repo.mark_run_failed(run_id, "STALE", "stale", **lease_a),
        lambda: repo.reserve_model_call_budget(run_id, 1, None, None, 0.01, None, None, **lease_a),
    ]:
        with pytest.raises(AppError):
            call()

    # Worker B continues successfully.
    repo.append_run_event(run_id, "run_started", {}, **lease_b)
    repo.transition_run(run_id, "running", expected_worker_id="worker-B", expected_attempt=lease_b["attempt"], expected_lease_token=lease_b["lease_token"])
    repo.mark_run_complete(run_id, {"models": []}, **lease_b)
    assert repo.get_run(run_id)["status"] == "completed"
    assert repo.get_run(run_id)["output"] == {"models": []}


def test_mock_engine_happy_path_completes_with_checkpoints(monkeypatch):
    """MILO_WORKER_ENGINE=mock runs the zero-cost lifecycle: events,
    checkpoints, simulated budget-tracked calls, successful completion —
    with no provider client ever constructed."""
    monkeypatch.setenv("MILO_WORKER_ENGINE", "mock")
    monkeypatch.delenv("MILO_ENABLE_PAID_EXECUTION", raising=False)

    class CheckpointCapturingRepo(WorkerRepo):
        def __init__(self):
            super().__init__()
            self.checkpoints = []
        def save_checkpoint(self, checkpoint, worker_id=None, attempt=None, lease_token=None):
            self._assert_lease(worker_id, attempt, lease_token)
            self.checkpoints.append(checkpoint)
            return checkpoint

    repo = CheckpointCapturingRepo()
    code = execute_run(repo.run_id, repo)
    assert code == 0
    assert repo.completed is not None
    phases = [c.get("phase") for c in repo.checkpoints]
    assert phases == ["discovery", "technical", "summary"]
    event_types = [event[1] for event in repo.events]
    assert "run_started" in event_types
    assert "phase_completed" in event_types
    assert event_types[-1] == "run_completed"


def test_mock_engine_budget_loop_trips_model_call_limit(monkeypatch):
    monkeypatch.setenv("MILO_WORKER_ENGINE", "mock")
    monkeypatch.setenv("MILO_MAX_MODEL_CALLS_PER_RUN", "3")
    monkeypatch.delenv("MILO_ENABLE_PAID_EXECUTION", raising=False)

    class BudgetRepo(WorkerRepo):
        def __init__(self):
            super().__init__()
            self.terminal = None
        def get_run(self, run_id):
            run = super().get_run(run_id)
            run["input"] = {"metadata": {"mock_scenario": "budget_loop", "mock_max_loop": 10}}
            return run
        def claim_run(self, run_id, worker_id, lease_seconds=300):
            super().claim_run(run_id, worker_id, lease_seconds)
            return self.get_run(run_id)
        def transition_run(self, run_id, status, expected_worker_id=None, expected_attempt=None, expected_lease_token=None, **fields):
            self._assert_lease(expected_worker_id, expected_attempt, expected_lease_token)
            if status in {"budget_exhausted", "failed", "timed_out"}:
                self.terminal = (status, fields.get("error"))
            return {"id": run_id, "status": status, **fields}

    repo = BudgetRepo()
    code = execute_run(repo.run_id, repo)
    assert code == 1
    assert repo.terminal is not None
    status, error = repo.terminal
    assert status == "budget_exhausted"
    assert error and "MODEL_CALL" in str(error.get("code", "")).upper()


def test_mock_engine_never_selected_without_env(monkeypatch):
    monkeypatch.delenv("MILO_WORKER_ENGINE", raising=False)
    from backend.worker import main as worker_main
    # The default path still uses the real adapter; nothing about the mock
    # module is imported at module import time.
    import sys
    sys.modules.pop("backend.worker.mock_engine", None)
    import importlib
    importlib.reload(worker_main)
    assert "backend.worker.mock_engine" not in sys.modules


def test_default_registry_routes_trusted_swarm_v2_and_completes(monkeypatch):
    """The production registry builds V2; no mock engine or metadata routing is used."""
    import json
    from types import SimpleNamespace
    from backend.budget import BudgetConfig, BudgetTracker
    from test_swarm_v2 import plan, task
    import backend.worker.main as worker_main

    planned_task = task("research", "neutral research")
    planned_task["tools"] = []
    initial = plan([planned_task])
    responses = [initial, {"answer": "done"},
                 {"decision": "FINISH", "plan": None, "reason": "complete"}]
    class Completions:
        def create(self, **kwargs):
            content = json.dumps(responses.pop(0))
            return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, cost=0),
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))])
    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    monkeypatch.setattr(worker_main, "build_guarded_client_factory", lambda tracker: lambda *_: client)
    monkeypatch.setenv("MILO_COMMANDER_MODEL_ALLOWLIST", "fake")
    monkeypatch.setenv("MILO_COMMANDER_MODEL", "fake")
    monkeypatch.setenv("MILO_SWARM_WORKER_MODEL", "fake")

    class SwarmRepo(WorkerRepo):
        def get_project(self, project_id):
            return {"id": project_id, "workflow_key": "swarm_v2"}
        def get_run(self, run_id):
            row = super().get_run(run_id)
            row["input"] = {"objective": "offline swarm", "commander_model": "fake"}
            return row
        def latest_checkpoint(self, run_id, workflow_key=None): return None
    repo = SwarmRepo()
    tracker = BudgetTracker(BudgetConfig(max_model_calls_per_run=10), kill_switch=lambda: True)
    assert worker_main.execute_run(repo.run_id, repo, budget_tracker=tracker) == 0
    assert repo.completed is not None
    assert repo.completed[1]["status"] == "complete"
    assert responses == []

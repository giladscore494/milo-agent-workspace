"""Idempotent run creation, launch safety and lifecycle validation tests."""

from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from backend.dependencies import get_job_launcher, get_repository
from backend.errors import AppError, NotFoundError
from backend.main import app
from backend.runtime import TERMINAL_STATES, VALID_TRANSITIONS, InvalidTransition, validate_transition


class StatefulRepo:
    """In-memory repository with real idempotency and launch-state records."""

    def __init__(self):
        self.project_id = uuid4()
        self.conversation_id = uuid4()
        self.user_id = uuid4()
        self.runs: dict[UUID, dict] = {}
        self.messages = []
        self.events = []
        self.invocations = []
        self.fail_run_insert = False

    def get_project(self, project_id, user_id=None):
        if user_id is not None and UUID(str(user_id)) != self.user_id:
            raise NotFoundError("project", str(project_id))
        return {"id": self.project_id, "slug": "p", "name": "P", "workflow_key": "vehicle_catalog_v1", "configuration": {}}

    def get_conversation(self, conversation_id, user_id=None):
        if user_id is not None and UUID(str(user_id)) != self.user_id:
            raise NotFoundError("conversation", str(conversation_id))
        if UUID(str(conversation_id)) != self.conversation_id:
            raise NotFoundError("conversation", str(conversation_id))
        return {"id": self.conversation_id, "project_id": self.project_id, "title": "t"}

    def create_user_message(self, conversation_id, content, metadata):
        self.messages.append({"conversation_id": conversation_id, "content": content, "metadata": metadata})
        return {"id": len(self.messages), "conversation_id": conversation_id, "role": "user", "content": content}

    def create_queued_run(self, conversation_id, user_message_id, content, metadata, requested_by=None, idempotency_key=None, request_fingerprint=None):
        if self.fail_run_insert:
            raise AppError("REPOSITORY_ERROR", "simulated database write failure", 502)
        if requested_by is not None and idempotency_key:
            existing = self.find_run_by_idempotency(conversation_id, requested_by, idempotency_key)
            if existing is not None:
                return existing
        run_id = uuid4()
        run = {
            "id": run_id, "conversation_id": conversation_id, "status": "queued",
            "launch_state": "pending", "requested_by": str(requested_by) if requested_by else None,
            "idempotency_key": idempotency_key, "request_fingerprint": request_fingerprint,
            "input": {"message_id": str(user_message_id), "content": content, "metadata": metadata},
        }
        self.runs[run_id] = run
        return dict(run)

    def find_run_by_idempotency(self, conversation_id, user_id, idempotency_key):
        for run in self.runs.values():
            if (str(run["conversation_id"]) == str(conversation_id)
                    and run.get("requested_by") == str(user_id)
                    and run.get("idempotency_key") == idempotency_key):
                return dict(run)
        return None

    def set_launch_state(self, run_id, state, error=None):
        run = self.runs[UUID(str(run_id))]
        run["launch_state"] = state
        if error is not None:
            run["launch_error"] = error
        return dict(run)

    def get_run(self, run_id, user_id=None):
        if user_id is not None and UUID(str(user_id)) != self.user_id:
            raise NotFoundError("run", str(run_id))
        run = self.runs.get(UUID(str(run_id)))
        if run is None:
            raise NotFoundError("run", str(run_id))
        return dict(run)

    def list_run_events(self, run_id, user_id=None):
        self.get_run(run_id, user_id)
        return [e for e in self.events if str(e["run_id"]) == str(run_id)]

    def request_cancellation(self, run_id, reason=None):
        run = self.runs[UUID(str(run_id))]
        run["status"] = "cancellation_requested"
        run["cancellation_reason"] = reason
        return dict(run)

    def append_run_event(self, run_id, event_type, payload):
        self.events.append({"run_id": run_id, "event_type": event_type, "payload": payload})
        return {"id": len(self.events)}

    def record_run_invocation(self, run_id, invocation):
        self.invocations.append({"run_id": run_id, **invocation})
        return {"id": len(self.invocations)}


class FlakyLauncher:
    def __init__(self, failures=0):
        self.failures = failures
        self.launched = []

    def launch(self, run_id):
        if self.failures > 0:
            self.failures -= 1
            raise AppError("JOB_LAUNCH_FAILED", "Cloud Run Job launch failed with HTTP 500", 502)
        self.launched.append(run_id)
        return {"mode": "test", "execution": f"exec-{len(self.launched)}"}


@pytest.fixture
def env(monkeypatch):
    repo = StatefulRepo()
    launcher = FlakyLauncher()
    monkeypatch.setenv("MILO_ENABLE_RUN_CREATION", "true")
    monkeypatch.setenv("MILO_ENABLE_RUN_CANCELLATION", "true")
    app.dependency_overrides[get_repository] = lambda: repo
    app.dependency_overrides[get_job_launcher] = lambda: launcher
    yield repo, launcher
    app.dependency_overrides.clear()


def member(repo):
    return {"x-milo-auth-user-id": str(repo.user_id)}


def create_run(repo, key="key-1234567890", content="go", metadata=None):
    body = {"content": content, "idempotency_key": key}
    if metadata is not None:
        body["metadata"] = metadata
    return TestClient(app).post(f"/conversations/{repo.conversation_id}/runs", json=body, headers=member(repo))


def test_same_key_and_payload_returns_original_run_without_second_launch(env):
    repo, launcher = env
    first = create_run(repo)
    second = create_run(repo)
    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["run_id"] == second.json()["run_id"]
    assert len(repo.runs) == 1
    assert len(repo.messages) == 1
    assert len(launcher.launched) == 1


def test_same_key_with_different_payload_returns_conflict(env):
    repo, launcher = env
    first = create_run(repo, content="original")
    conflict = create_run(repo, content="different payload")
    assert first.status_code == 202
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert len(repo.runs) == 1
    assert len(launcher.launched) == 1


def test_different_keys_create_distinct_runs(env):
    repo, launcher = env
    a = create_run(repo, key="key-aaaaaaaaaa")
    b = create_run(repo, key="key-bbbbbbbbbb")
    assert a.json()["run_id"] != b.json()["run_id"]
    assert len(launcher.launched) == 2


def test_launcher_exception_leaves_recoverable_state(env):
    repo, launcher = env
    launcher.failures = 1
    response = create_run(repo)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "JOB_LAUNCH_FAILED"
    run = next(iter(repo.runs.values()))
    assert run["status"] == "queued"
    assert run["launch_state"] == "launch_failed"
    assert any(e["event_type"] == "launch_failed" for e in repo.events)
    assert launcher.launched == []


def test_retry_after_failed_launch_relaunches_same_run(env):
    repo, launcher = env
    launcher.failures = 1
    failed = create_run(repo)
    assert failed.status_code == 502
    retried = create_run(repo)
    assert retried.status_code == 202
    assert len(repo.runs) == 1
    assert len(repo.messages) == 1  # no duplicate user message on retry
    assert len(launcher.launched) == 1
    run = next(iter(repo.runs.values()))
    assert run["launch_state"] == "launched"


def test_replay_while_launch_in_flight_does_not_double_launch(env):
    repo, launcher = env
    created = create_run(repo)
    run_id = UUID(created.json()["run_id"])
    repo.set_launch_state(run_id, "launching")
    replay = create_run(repo)
    assert replay.status_code == 202
    assert replay.json()["run_id"] == str(run_id)
    assert len(launcher.launched) == 1


def test_cancelled_before_launch_is_not_launched(env):
    repo, launcher = env
    launcher.failures = 1
    failed_launch = create_run(repo)
    assert failed_launch.status_code == 502
    run_id = next(iter(repo.runs))
    TestClient(app).post(f"/runs/{run_id}/cancel", json={"reason": "changed my mind"}, headers=member(repo))
    assert repo.runs[run_id]["status"] == "cancellation_requested"
    replay = create_run(repo)
    assert replay.status_code == 202
    assert replay.json()["status"] == "cancellation_requested"
    assert launcher.launched == []


def test_cancellation_is_idempotent_and_records_single_event(env):
    repo, launcher = env
    created = create_run(repo)
    run_id = created.json()["run_id"]
    c = TestClient(app)
    first = c.post(f"/runs/{run_id}/cancel", json={"reason": "stop"}, headers=member(repo))
    second = c.post(f"/runs/{run_id}/cancel", json={"reason": "stop again"}, headers=member(repo))
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["status"] == "cancellation_requested"
    cancellation_events = [e for e in repo.events if e["event_type"] == "cancellation_requested"]
    assert len(cancellation_events) == 1


def test_cancelling_finished_run_returns_conflict(env):
    repo, launcher = env
    created = create_run(repo)
    run_id = UUID(created.json()["run_id"])
    repo.runs[run_id]["status"] = "completed"
    response = TestClient(app).post(f"/runs/{run_id}/cancel", json={"reason": "late"}, headers=member(repo))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RUN_ALREADY_FINISHED"


def test_cancelling_already_cancelled_run_is_a_no_op(env):
    repo, launcher = env
    created = create_run(repo)
    run_id = UUID(created.json()["run_id"])
    repo.runs[run_id]["status"] = "cancelled"
    response = TestClient(app).post(f"/runs/{run_id}/cancel", json={}, headers=member(repo))
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"


def test_database_write_failure_never_launches(env):
    repo, launcher = env
    repo.fail_run_insert = True
    response = create_run(repo)
    assert response.status_code == 502
    assert launcher.launched == []
    assert repo.invocations == []


def test_malformed_request_bodies_are_rejected_without_mutation(env):
    repo, launcher = env
    c = TestClient(app)
    for body in ({}, {"content": ""}, {"content": "x", "idempotency_key": "short"}, {"content": "x", "idempotency_key": "k" * 200}):
        response = c.post(f"/conversations/{repo.conversation_id}/runs", json=body, headers=member(repo))
        assert response.status_code == 422, body
    assert repo.runs == {}
    assert repo.messages == []
    assert launcher.launched == []


def test_unauthorized_user_creates_nothing_and_never_launches(env):
    repo, launcher = env
    response = TestClient(app).post(
        f"/conversations/{repo.conversation_id}/runs",
        json={"content": "go", "idempotency_key": "key-1234567890"},
        headers={"x-milo-auth-user-id": str(uuid4())},
    )
    assert response.status_code == 404
    assert repo.runs == {}
    assert launcher.launched == []


def test_execution_disabled_returns_403_without_any_state(env, monkeypatch):
    repo, launcher = env
    monkeypatch.delenv("MILO_ENABLE_RUN_CREATION", raising=False)
    response = create_run(repo)
    assert response.status_code == 403
    assert repo.runs == {}
    assert launcher.launched == []


# --- lifecycle state machine -------------------------------------------------

def test_every_declared_state_is_reachable_or_initial():
    reachable = {target for targets in VALID_TRANSITIONS.values() for target in targets}
    assert reachable | {"queued"} == set(VALID_TRANSITIONS)


def test_terminal_states_allow_no_transitions():
    for state in TERMINAL_STATES:
        assert VALID_TRANSITIONS[state] == set()


@pytest.mark.parametrize("current,new", [
    ("completed", "running"), ("cancelled", "queued"), ("failed", "running"),
    ("timed_out", "running"), ("budget_exhausted", "running"),
    ("queued", "completed"), ("queued", "running"), ("launching", "completed"),
])
def test_invalid_transitions_raise(current, new):
    with pytest.raises(InvalidTransition):
        validate_transition(current, new)


@pytest.mark.parametrize("current,new", [
    ("queued", "launching"), ("launching", "starting"), ("starting", "running"),
    ("running", "completed"), ("running", "partial_success"), ("running", "timed_out"),
    ("running", "budget_exhausted"), ("running", "cancellation_requested"),
    ("cancellation_requested", "cancelled"), ("launching", "queued"),
    ("starting", "completed"),
])
def test_supported_transitions_are_valid(current, new):
    validate_transition(current, new)

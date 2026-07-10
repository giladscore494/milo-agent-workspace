from uuid import UUID, uuid4
import pytest
from fastapi.testclient import TestClient
from backend.dependencies import get_job_launcher, get_repository
from backend.errors import AppError, NotFoundError
from backend.main import app


class FakeRepo:
    def __init__(self, fail=False):
        self.project_id = uuid4()
        self.conversation_id = uuid4()
        self.message_id = uuid4()
        self.run_id = uuid4()
        self.user_id = uuid4()
        self.fail = fail
        self.created_runs = 0
        self.cancelled_runs = 0
        self.tool_grants = 0

    def _fail(self):
        if self.fail:
            raise AppError("REPOSITORY_ERROR", "mock failure", 502)

    def list_projects(self, user_id=None):
        self._fail(); return [self.project()] if user_id == self.user_id else []
    def project(self):
        return {"id": self.project_id, "slug": "milo-vehicle-catalog", "name": "MILO Vehicle Catalog", "workflow_key": "vehicle_catalog_v1", "configuration": {}}
    def get_project(self, project_id, user_id=None):
        self._fail()
        if (user_id is not None and user_id != self.user_id) or UUID(str(project_id)) != self.project_id: raise NotFoundError("project", str(project_id))
        return self.project()
    def create_conversation(self, project_id, title, user_id=None):
        self.get_project(project_id, user_id); return {"id": self.conversation_id, "project_id": self.project_id, "title": title}
    def get_conversation(self, conversation_id, user_id=None):
        self._fail()
        if (user_id is not None and user_id != self.user_id) or UUID(str(conversation_id)) != self.conversation_id: raise NotFoundError("conversation", str(conversation_id))
        return {"id": self.conversation_id, "project_id": self.project_id, "title": "t"}
    def create_user_message(self, conversation_id, content, metadata):
        self.get_conversation(conversation_id); return {"id": self.message_id, "conversation_id": conversation_id, "role": "user", "content": content, "metadata": metadata}
    def create_queued_run(self, conversation_id, user_message_id, content, metadata):
        self.created_runs += 1
        self.queued_message_id = user_message_id
        return {"id": self.run_id, "conversation_id": conversation_id, "status": "queued", "input": {"message_id": str(user_message_id), "content": content, "metadata": metadata}}
    def get_run(self, run_id):
        self._fail(); return {"id": run_id, "conversation_id": self.conversation_id, "status": "queued"}
    def list_run_events(self, run_id):
        self._fail(); return []
    def request_cancellation(self, run_id, reason=None):
        self.cancelled_runs += 1; return {"id": run_id, "status": "cancellation_requested"}
    def append_run_event(self, *args, **kwargs):
        return {"id": 1}
    def create_tool_grant(self, run_id, grant):
        self.tool_grants += 1; return {"id": str(uuid4()), "run_id": run_id, **grant}


class FakeLauncher:
    def __init__(self):
        self.launched = []
    def launch(self, run_id):
        self.launched.append(run_id)
        return {"mode": "test", "execution": "test"}


@pytest.fixture
def repo(monkeypatch):
    fake = FakeRepo()
    launcher = FakeLauncher()
    monkeypatch.delenv("MILO_ENABLE_RUN_CREATION", raising=False)
    monkeypatch.delenv("MILO_ENABLE_PROPOSAL_MUTATIONS", raising=False)
    monkeypatch.delenv("MILO_ENABLE_EXECUTION_CONTROL", raising=False)
    app.dependency_overrides[get_repository] = lambda: fake
    app.dependency_overrides[get_job_launcher] = lambda: launcher
    fake.launcher = launcher
    yield fake
    app.dependency_overrides.clear()


def test_health():
    assert TestClient(app).get("/health").json() == {"status": "ok"}


def test_project_retrieval(repo):
    client = TestClient(app)
    response = client.get(f"/projects/{repo.project_id}", headers={"x-milo-auth-user-id": str(repo.user_id)})
    assert response.status_code == 200
    assert response.json()["slug"] == "milo-vehicle-catalog"


def test_conversation_creation(repo):
    response = TestClient(app).post(f"/projects/{repo.project_id}/conversations", json={"title": "New"}, headers={"x-milo-auth-user-id": str(repo.user_id)})
    assert response.status_code == 201
    assert response.json()["project_id"] == str(repo.project_id)


def test_run_creation_disabled_by_default_does_not_queue_or_launch(repo):
    response = TestClient(app).post(f"/conversations/{repo.conversation_id}/runs", json={"content": "Build catalog"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "EXECUTION_SURFACE_DISABLED"
    assert repo.created_runs == 0
    assert repo.launcher.launched == []


def test_run_cancellation_disabled_by_default_does_not_mutate(repo):
    response = TestClient(app).post(f"/runs/{repo.run_id}/cancel", json={"reason": "stop"})
    assert response.status_code == 403
    assert repo.cancelled_runs == 0


def test_tool_grant_disabled_by_default_does_not_grant(repo):
    response = TestClient(app).post(f"/runs/{repo.run_id}/tool-grants", json={"agent": "a", "tool": "web", "granted_by": "u"})
    assert response.status_code == 403
    assert repo.tool_grants == 0


def test_repository_failures_are_structured():
    fake = FakeRepo(fail=True)
    app.dependency_overrides[get_repository] = lambda: fake
    try:
        response = TestClient(app).get("/projects", headers={"x-milo-auth-user-id": str(fake.user_id)})
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "REPOSITORY_ERROR"
    finally:
        app.dependency_overrides.clear()


def test_unauthenticated_project_access_returns_401(repo):
    response = TestClient(app).get(f"/projects/{repo.project_id}")
    assert response.status_code == 401


def test_user_without_membership_cannot_access_project_or_conversation(repo):
    other = str(uuid4())
    client = TestClient(app)
    assert client.get(f"/projects/{repo.project_id}", headers={"x-milo-auth-user-id": other}).status_code == 404
    assert client.get(f"/conversations/{repo.conversation_id}", headers={"x-milo-auth-user-id": other}).status_code == 404


def test_compat_api_entrypoint_uses_protected_app(repo):
    from backend.api import app as compat_app

    response = TestClient(compat_app).get(f"/projects/{repo.project_id}")
    assert response.status_code == 401

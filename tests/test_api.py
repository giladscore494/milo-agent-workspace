from datetime import UTC, datetime, timedelta
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
        self.proposal_id = uuid4()
        self.user_id = uuid4()
        self.fail = fail
        self.created_messages = 0
        self.created_runs = 0
        self.cancelled_runs = 0
        self.proposal_creations = 0
        self.proposal_updates = 0
        self.projects_from_proposals = 0
        self.tool_access_requests = 0
        self.tool_grants = 0
        self.tool_usage_rows = 0
        self.sources = 0
        self.claims = 0
        self.conflicts = 0
        self.appended_events = 0
        self.completed_runs = 0
        self.failed_runs = 0

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
        self.get_conversation(conversation_id)
        self.created_messages += 1
        return {"id": self.message_id, "conversation_id": conversation_id, "role": "user", "content": content, "metadata": metadata}
    def create_queued_run(self, conversation_id, user_message_id, content, metadata, **kwargs):
        self.created_runs += 1
        self.queued_message_id = user_message_id
        return {"id": self.run_id, "conversation_id": conversation_id, "status": "queued", "launch_state": "pending", "input": {"message_id": str(user_message_id), "content": content, "metadata": metadata}, **{k: str(v) for k, v in kwargs.items() if v is not None}}
    def find_run_by_idempotency(self, conversation_id, user_id, idempotency_key):
        return None
    def set_launch_state(self, run_id, state, error=None):
        self.launch_states = getattr(self, "launch_states", []) + [state]
        return {"id": run_id, "launch_state": state}
    def get_run(self, run_id, user_id=None):
        self._fail()
        if (user_id is not None and user_id != self.user_id) or UUID(str(run_id)) != self.run_id: raise NotFoundError("run", str(run_id))
        return {"id": run_id, "conversation_id": self.conversation_id, "status": "queued"}
    def list_run_events(self, run_id, user_id=None):
        self.get_run(run_id, user_id); return []
    def request_cancellation(self, run_id, reason=None):
        self.cancelled_runs += 1; return {"id": run_id, "status": "cancellation_requested"}
    def append_run_event(self, *args, **kwargs):
        self.appended_events += 1; return {"id": 1}
    def create_workflow_proposal(self, user_request, proposal, project_id=None, created_by=None):
        self.proposal_creations += 1; return {"id": self.proposal_id, "status": "approved", "user_request": user_request, "project_id": project_id, "created_by": created_by, **proposal}
    def get_workflow_proposal(self, proposal_id, user_id=None):
        self._fail()
        if (user_id is not None and user_id != self.user_id) or UUID(str(proposal_id)) != self.proposal_id: raise NotFoundError("workflow_proposal", str(proposal_id))
        return {"id": self.proposal_id, "status": "approved", "user_request": "r", "task_spec": {}, "draft": {}, "estimates": {}, "approved_at": datetime.now(UTC).isoformat(), "project_id": self.project_id, "created_by": self.user_id}
    def update_workflow_proposal(self, proposal_id, fields):
        self.proposal_updates += 1; return {**self.get_workflow_proposal(proposal_id), **fields}
    def create_project_from_proposal(self, proposal_id, slug, name, description, configuration, created_by=None):
        self.projects_from_proposals += 1; return {"id": uuid4(), "slug": slug, "name": name, "workflow_key": "chat_architect_v1", "configuration": configuration}
    def create_tool_access_request(self, run_id, request):
        self.tool_access_requests += 1; return {"id": str(uuid4()), "run_id": run_id, **request}
    def create_tool_grant(self, run_id, grant):
        self.tool_grants += 1; return {"id": str(uuid4()), "run_id": run_id, **grant}
    def create_tool_usage(self, run_id, usage):
        self.tool_usage_rows += 1; return {"id": str(uuid4()), "run_id": run_id, **usage}
    def create_source(self, run_id, source):
        self.sources += 1; return {"id": str(uuid4()), "run_id": run_id, **source}
    def create_claim(self, run_id, claim):
        self.claims += 1; return {"id": str(uuid4()), "run_id": run_id, **claim}
    def create_conflict(self, run_id, conflict):
        self.conflicts += 1; return {"id": str(uuid4()), "run_id": run_id, **conflict}
    def mark_run_complete(self, run_id, output):
        self.completed_runs += 1; return {"id": run_id, "conversation_id": self.conversation_id, "status": "completed", "output": output}
    def mark_run_failed(self, run_id, code, message):
        self.failed_runs += 1; return {"id": run_id, "conversation_id": self.conversation_id, "status": "failed", "error": {"code": code, "message": message}}

    def assert_no_mutations(self):
        assert self.created_messages == 0
        assert self.created_runs == 0
        assert self.cancelled_runs == 0
        assert self.proposal_creations == 0
        assert self.proposal_updates == 0
        assert self.projects_from_proposals == 0
        assert self.tool_access_requests == 0
        assert self.tool_grants == 0
        assert self.tool_usage_rows == 0
        assert self.sources == 0
        assert self.claims == 0
        assert self.conflicts == 0
        assert self.appended_events == 0
        assert self.completed_runs == 0
        assert self.failed_runs == 0


class FakeLauncher:
    def __init__(self):
        self.launched = []
    def launch(self, run_id):
        self.launched.append(run_id)
        return {"mode": "test", "execution": "test"}


EXECUTION_FLAGS = ["MILO_ENABLE_RUN_CREATION", "MILO_ENABLE_PROPOSAL_MUTATIONS", "MILO_ENABLE_RUN_CANCELLATION", "MILO_ENABLE_EXECUTION_CONTROL", "MILO_ENABLE_PROPOSAL_READS"]


@pytest.fixture
def repo(monkeypatch):
    fake = FakeRepo()
    launcher = FakeLauncher()
    for flag in EXECUTION_FLAGS:
        monkeypatch.delenv(flag, raising=False)
    app.dependency_overrides[get_repository] = lambda: fake
    app.dependency_overrides[get_job_launcher] = lambda: launcher
    fake.launcher = launcher
    yield fake
    app.dependency_overrides.clear()


def valid_tool_grant_body():
    return {
        "agent": "discovery",
        "tool": "web_search",
        "max_searches": 5,
        "max_rounds": 2,
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        "approver_policy": "manual",
    }


def test_health():
    assert TestClient(app).get("/health").json() == {"status": "ok"}


def test_health_remains_public_without_identity_headers(repo):
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_project_retrieval(repo):
    client = TestClient(app)
    response = client.get(f"/projects/{repo.project_id}", headers={"x-milo-auth-user-id": str(repo.user_id)})
    assert response.status_code == 200
    assert response.json()["slug"] == "milo-vehicle-catalog"


def test_conversation_creation(repo):
    response = TestClient(app).post(f"/projects/{repo.project_id}/conversations", json={"title": "New"}, headers={"x-milo-auth-user-id": str(repo.user_id)})
    assert response.status_code == 201
    assert response.json()["project_id"] == str(repo.project_id)


def test_run_creation_disabled_creates_no_message_run_or_launch(repo):
    response = TestClient(app).post(f"/conversations/{repo.conversation_id}/runs", json={"content": "Build catalog"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "EXECUTION_SURFACE_DISABLED"
    assert repo.created_messages == 0
    assert repo.created_runs == 0
    assert repo.launcher.launched == []
    repo.assert_no_mutations()


def test_proposal_run_creation_disabled_creates_no_message_run_or_launch(repo):
    body = {"conversation_id": str(repo.conversation_id), "content": "Go"}
    response = TestClient(app).post(f"/workflow-proposals/{repo.proposal_id}/runs", json=body)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "EXECUTION_SURFACE_DISABLED"
    assert repo.created_messages == 0
    assert repo.created_runs == 0
    assert repo.launcher.launched == []


def test_run_cancellation_disabled_by_default_does_not_mutate(repo):
    response = TestClient(app).post(f"/runs/{repo.run_id}/cancel", json={"reason": "stop"})
    assert response.status_code == 403
    assert repo.cancelled_runs == 0
    assert repo.appended_events == 0


def test_tool_grant_disabled_by_default_does_not_grant(repo):
    response = TestClient(app).post(f"/runs/{repo.run_id}/tool-grants", json=valid_tool_grant_body())
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "EXECUTION_SURFACE_DISABLED"
    assert repo.tool_grants == 0


def test_tool_access_request_and_usage_disabled_do_not_mutate(repo):
    client = TestClient(app)
    request_body = {"agent": "discovery", "tool": "web_search", "reason": "verify"}
    usage_body = {"grant_id": str(uuid4()), "agent": "discovery", "tool": "web_search", "operation": "search"}
    assert client.post(f"/runs/{repo.run_id}/tool-access-requests", json=request_body).status_code == 403
    assert client.post(f"/runs/{repo.run_id}/tool-usage", json=usage_body).status_code == 403
    assert repo.tool_access_requests == 0
    assert repo.tool_usage_rows == 0
    assert repo.appended_events == 0


def test_source_claim_and_conflict_recording_disabled_do_not_mutate(repo):
    client = TestClient(app)
    source = {"agent": "a", "url": "https://example.com", "title": "t", "domain": "example.com", "source_type": "web", "source_strength": "high", "query": "q", "tool_operation": "search"}
    claim = {"entity_key": "e", "field_key": "f", "value": 1, "source_id": str(uuid4()), "source_strength": "high", "confidence": 0.9, "agent": "a"}
    conflict = {"entity_key": "e", "field_key": "f", "claim_ids": [str(uuid4())]}
    assert client.post(f"/runs/{repo.run_id}/sources", json=source).status_code == 403
    assert client.post(f"/runs/{repo.run_id}/claims", json=claim).status_code == 403
    assert client.post(f"/runs/{repo.run_id}/conflicts", json=conflict).status_code == 403
    assert repo.sources == 0
    assert repo.claims == 0
    assert repo.conflicts == 0
    repo.assert_no_mutations()


def test_proposal_mutations_disabled_do_not_call_repository(repo):
    client = TestClient(app)
    assert client.post("/workflow-proposals", json={"user_request": "Research"}).status_code == 403
    assert client.post(f"/workflow-proposals/{repo.proposal_id}/approve", json={"reason": "ok"}).status_code == 403
    assert client.post(f"/workflow-proposals/{repo.proposal_id}/reject", json={"reason": "no"}).status_code == 403
    assert client.post(f"/workflow-proposals/{repo.proposal_id}/revise", json={"user_request": "Again"}).status_code == 403
    assert repo.proposal_creations == 0
    assert repo.proposal_updates == 0


def test_project_creation_from_proposal_disabled_is_not_called(repo):
    body = {"slug": "new-project", "name": "New Project"}
    response = TestClient(app).post(f"/workflow-proposals/{repo.proposal_id}/project", json=body)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "EXECUTION_SURFACE_DISABLED"
    assert repo.projects_from_proposals == 0


INVALID_BODY_ROUTES = [
    "/conversations/{conversation_id}/runs",
    "/workflow-proposals",
    "/workflow-proposals/{proposal_id}/approve",
    "/workflow-proposals/{proposal_id}/reject",
    "/workflow-proposals/{proposal_id}/revise",
    "/workflow-proposals/{proposal_id}/project",
    "/workflow-proposals/{proposal_id}/runs",
    "/runs/{run_id}/cancel",
    "/runs/{run_id}/tool-access-requests",
    "/runs/{run_id}/tool-grants",
    "/runs/{run_id}/tool-usage",
    "/runs/{run_id}/sources",
    "/runs/{run_id}/claims",
    "/runs/{run_id}/conflicts",
]


@pytest.mark.parametrize("template", INVALID_BODY_ROUTES)
@pytest.mark.parametrize("body", [b"", b"{not-json", b"{}"], ids=["empty", "malformed", "schema-invalid"])
def test_disabled_surfaces_return_403_before_body_validation(repo, template, body):
    path = template.format(conversation_id=repo.conversation_id, proposal_id=repo.proposal_id, run_id=repo.run_id)
    response = TestClient(app).post(path, content=body, headers={"content-type": "application/json"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "EXECUTION_SURFACE_DISABLED"
    repo.assert_no_mutations()
    assert repo.launcher.launched == []


def test_disabled_surfaces_return_403_even_for_invalid_path_ids(repo):
    response = TestClient(app).post("/runs/not-a-uuid/tool-grants", content=b"", headers={"content-type": "application/json"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "EXECUTION_SURFACE_DISABLED"


def test_run_read_requires_authenticated_identity(repo):
    client = TestClient(app)
    assert client.get(f"/runs/{repo.run_id}").status_code == 401
    assert client.get(f"/runs/{repo.run_id}/events").status_code == 401


def test_run_read_allows_project_members_only(repo):
    client = TestClient(app)
    member = {"x-milo-auth-user-id": str(repo.user_id)}
    stranger = {"x-milo-auth-user-id": str(uuid4())}
    assert client.get(f"/runs/{repo.run_id}", headers=member).status_code == 200
    assert client.get(f"/runs/{repo.run_id}/events", headers=member).status_code == 200
    assert client.get(f"/runs/{repo.run_id}", headers=stranger).status_code == 404
    assert client.get(f"/runs/{repo.run_id}/events", headers=stranger).status_code == 404


def test_workflow_proposal_read_disabled_by_default(repo):
    # workflow_proposals has no created_by/project relationship in the schema,
    # so the read stays default-disabled instead of being globally readable.
    response = TestClient(app).get(f"/workflow-proposals/{repo.proposal_id}")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "EXECUTION_SURFACE_DISABLED"


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


def test_unauthenticated_conversation_routes_return_401(repo):
    client = TestClient(app)
    assert client.get(f"/conversations/{repo.conversation_id}").status_code == 401
    assert client.post(f"/projects/{repo.project_id}/conversations", json={"title": "New"}).status_code == 401


def test_user_without_membership_cannot_access_project_or_conversation(repo):
    other = str(uuid4())
    client = TestClient(app)
    assert client.get(f"/projects/{repo.project_id}", headers={"x-milo-auth-user-id": other}).status_code == 404
    assert client.get(f"/conversations/{repo.conversation_id}", headers={"x-milo-auth-user-id": other}).status_code == 404


def test_compat_api_entrypoint_uses_protected_app(repo):
    from backend.api import app as compat_app

    assert compat_app is app
    client = TestClient(compat_app)
    assert client.get(f"/projects/{repo.project_id}").status_code == 401
    guarded = client.post(f"/runs/{repo.run_id}/tool-grants", content=b"", headers={"content-type": "application/json"})
    assert guarded.status_code == 403
    assert guarded.json()["error"]["code"] == "EXECUTION_SURFACE_DISABLED"

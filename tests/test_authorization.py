"""Exhaustive browser-route authorization tests.

Every execution flag is force-enabled here so these tests prove the
authorization layer alone (trusted identity + project membership) blocks
mutations and worker launches, independent of the execution kill switches.
"""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.dependencies import get_job_launcher, get_repository
from backend.main import app
from tests.test_api import EXECUTION_FLAGS, FakeLauncher, FakeRepo


@pytest.fixture
def repo(monkeypatch):
    fake = FakeRepo()
    launcher = FakeLauncher()
    for flag in EXECUTION_FLAGS:
        monkeypatch.setenv(flag, "true")
    app.dependency_overrides[get_repository] = lambda: fake
    app.dependency_overrides[get_job_launcher] = lambda: launcher
    fake.launcher = launcher
    yield fake
    app.dependency_overrides.clear()


def client():
    return TestClient(app)


def member_headers(repo):
    return {"x-milo-auth-user-id": str(repo.user_id)}


def stranger_headers():
    return {"x-milo-auth-user-id": str(uuid4())}


BROWSER_MUTATION_REQUESTS = [
    ("POST", "/projects/{project_id}/conversations", {"title": "t"}),
    ("POST", "/conversations/{conversation_id}/runs", {"content": "go"}),
    ("POST", "/workflow-proposals", {"user_request": "Create a current cited report", "project_id": "{project_id}"}),
    ("POST", "/workflow-proposals/{proposal_id}/approve", {}),
    ("POST", "/workflow-proposals/{proposal_id}/reject", {}),
    ("POST", "/workflow-proposals/{proposal_id}/revise", {"user_request": "Create another current cited report"}),
    ("POST", "/workflow-proposals/{proposal_id}/project", {"slug": "s", "name": "N"}),
    ("POST", "/workflow-proposals/{proposal_id}/runs", {"conversation_id": "{conversation_id}", "content": "go"}),
    ("POST", "/runs/{run_id}/cancel", {"reason": "stop"}),
]

BROWSER_READ_REQUESTS = [
    ("GET", "/projects/{project_id}", None),
    ("GET", "/conversations/{conversation_id}", None),
    ("GET", "/runs/{run_id}", None),
    ("GET", "/runs/{run_id}/events", None),
    ("GET", "/workflow-proposals/{proposal_id}", None),
]


def fill(template, repo):
    return template.format(project_id=repo.project_id, conversation_id=repo.conversation_id, run_id=repo.run_id, proposal_id=repo.proposal_id)


def fill_body(body, repo):
    if body is None:
        return None
    return {key: fill(value, repo) if isinstance(value, str) else value for key, value in body.items()}


@pytest.mark.parametrize("method,path,body", BROWSER_MUTATION_REQUESTS + BROWSER_READ_REQUESTS)
def test_unauthenticated_requests_are_rejected_without_any_mutation(repo, method, path, body):
    response = client().request(method, fill(path, repo), json=fill_body(body, repo))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"
    repo.assert_no_mutations()
    assert repo.launcher.launched == []


@pytest.mark.parametrize("method,path,body", BROWSER_MUTATION_REQUESTS + BROWSER_READ_REQUESTS)
def test_cross_user_requests_return_404_without_any_mutation(repo, method, path, body):
    response = client().request(method, fill(path, repo), json=fill_body(body, repo), headers=stranger_headers())
    assert response.status_code == 404, f"{method} {path}"
    repo.assert_no_mutations()
    assert repo.launcher.launched == []


@pytest.mark.parametrize("header_value", ["", "not-a-uuid", "123", "None"])
def test_malformed_identity_header_is_rejected(repo, header_value):
    response = client().get(f"/projects/{repo.project_id}", headers={"x-milo-auth-user-id": header_value})
    assert response.status_code == 401
    repo.assert_no_mutations()


def test_email_header_alone_never_authenticates(repo):
    response = client().get("/projects", headers={"x-milo-auth-user-email": "someone@example.com"})
    assert response.status_code == 401
    repo.assert_no_mutations()


def test_member_can_create_run_and_launch_happens_once(repo):
    response = client().post(f"/conversations/{repo.conversation_id}/runs", json={"content": "go"}, headers=member_headers(repo))
    assert response.status_code == 202
    assert repo.created_messages == 1
    assert repo.created_runs == 1
    assert len(repo.launcher.launched) == 1


def test_cross_user_run_creation_never_reaches_launcher(repo):
    response = client().post(f"/conversations/{repo.conversation_id}/runs", json={"content": "go"}, headers=stranger_headers())
    assert response.status_code == 404
    assert repo.created_messages == 0
    assert repo.created_runs == 0
    assert repo.launcher.launched == []


def test_member_cancellation_is_authorized_and_recorded(repo):
    response = client().post(f"/runs/{repo.run_id}/cancel", json={"reason": "stop"}, headers=member_headers(repo))
    assert response.status_code == 200
    assert repo.cancelled_runs == 1


def test_cross_user_cancellation_does_not_mutate(repo):
    response = client().post(f"/runs/{repo.run_id}/cancel", json={"reason": "stop"}, headers=stranger_headers())
    assert response.status_code == 404
    assert repo.cancelled_runs == 0
    assert repo.appended_events == 0


def test_worker_surfaces_reject_browser_users_even_with_flags_enabled(repo):
    # Even a legitimate project member must never write through the internal
    # worker mutation surfaces with browser identity headers. Worker auth is
    # deliberately unconfigured here, so the boundary fails closed (503);
    # tests/test_worker_auth.py covers the configured rejection paths.
    c = client()
    for path, body in (
        ("tool-access-requests", {"agent": "a", "tool": "web_search", "reason": "r"}),
        ("tool-usage", {"grant_id": str(uuid4()), "agent": "a", "tool": "web_search", "operation": "search"}),
        ("sources", {"agent": "a", "url": "https://example.com", "title": "t", "domain": "example.com", "source_type": "web", "source_strength": "high", "query": "q", "tool_operation": "search"}),
        ("claims", {"entity_key": "e", "field_key": "f", "value": 1, "source_id": str(uuid4()), "source_strength": "high", "confidence": 0.9, "agent": "a"}),
        ("conflicts", {"entity_key": "e", "field_key": "f", "claim_ids": [str(uuid4())]}),
    ):
        response = c.post(f"/runs/{repo.run_id}/{path}", json=body, headers=member_headers(repo))
        assert response.status_code in {401, 403, 503}, path
    for path, body in (
        ("events", {"event_type": "agent_progress", "message": "m"}),
        ("complete", {"output": {}}),
        ("fail", {"code": "X", "message": "m"}),
    ):
        response = c.post(f"/internal/runs/{repo.run_id}/{path}", json=body, headers=member_headers(repo))
        assert response.status_code in {401, 403, 503}, path
    repo.assert_no_mutations()


def test_stranger_sees_empty_project_list_not_other_users_projects(repo):
    response = client().get("/projects", headers=stranger_headers())
    assert response.status_code == 200
    assert response.json() == []

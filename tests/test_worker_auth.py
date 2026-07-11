"""Service-to-service authentication tests for worker mutation routes.

MILO_ENABLE_EXECUTION_CONTROL is force-enabled in most tests so they prove
the worker identity boundary rejects callers on its own: the feature flag
alone must never authorize a mutation.
"""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.dependencies import get_job_launcher, get_repository
from backend.main import app
from backend.worker_auth import get_token_verifier
from tests.test_api import FakeLauncher, FakeRepo, valid_tool_grant_body

AUDIENCE = "https://milo-api.example.internal"
APPROVED_SA = "milo-worker@example-project.iam.gserviceaccount.com"
UNAPPROVED_SA = "intruder@example-project.iam.gserviceaccount.com"


def google_claims(**overrides):
    claims = {
        "iss": "https://accounts.google.com",
        "aud": AUDIENCE,
        "email": APPROVED_SA,
        "email_verified": True,
        "sub": "1234567890",
    }
    claims.update(overrides)
    return claims


class FakeVerifier:
    """Deterministic stand-in for Google certificate verification."""

    TOKENS = {
        "valid-worker-token": google_claims(),
        "wrong-issuer-token": google_claims(iss="https://tokens.evil.example"),
        "claims-audience-mismatch-token": google_claims(aud="https://other-service.example"),
        "unapproved-sa-token": google_claims(email=UNAPPROVED_SA),
        "unverified-email-token": google_claims(email_verified=False),
        "missing-email-token": {k: v for k, v in google_claims().items() if k != "email"},
    }
    REJECT = {
        "expired-token": "Token expired",
        "malformed-token": "Wrong number of segments in token",
        "browser-supabase-token": "Could not verify token signature",
        "wrong-audience-token": "Token has wrong audience",
    }

    def verify(self, token, audience):
        assert audience == AUDIENCE
        if token in self.REJECT:
            raise ValueError(self.REJECT[token])
        if token in self.TOKENS:
            return dict(self.TOKENS[token])
        raise ValueError("Could not verify token signature")


@pytest.fixture
def repo(monkeypatch):
    fake = FakeRepo()
    launcher = FakeLauncher()
    monkeypatch.setenv("MILO_ENABLE_EXECUTION_CONTROL", "true")
    monkeypatch.setenv("MILO_WORKER_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("MILO_APPROVED_WORKER_IDENTITIES", f" {APPROVED_SA} , other-approved@example-project.iam.gserviceaccount.com")
    app.dependency_overrides[get_repository] = lambda: fake
    app.dependency_overrides[get_job_launcher] = lambda: launcher
    app.dependency_overrides[get_token_verifier] = lambda: FakeVerifier()
    fake.launcher = launcher
    yield fake
    app.dependency_overrides.clear()


def client():
    return TestClient(app)


def worker_headers(token):
    return {"X-Milo-Worker-Token": token}


def worker_routes(repo):
    return [
        (f"/runs/{repo.run_id}/tool-access-requests", {"agent": "a", "tool": "web_search", "reason": "r"}),
        (f"/runs/{repo.run_id}/tool-grants", valid_tool_grant_body()),
        (f"/runs/{repo.run_id}/tool-usage", {"grant_id": str(uuid4()), "agent": "a", "tool": "web_search", "operation": "search"}),
        (f"/runs/{repo.run_id}/sources", {"agent": "a", "url": "https://example.com", "title": "t", "domain": "example.com", "source_type": "web", "source_strength": "high", "query": "q", "tool_operation": "search"}),
        (f"/runs/{repo.run_id}/claims", {"entity_key": "e", "field_key": "f", "value": 1, "source_id": str(uuid4()), "source_strength": "high", "confidence": 0.9, "agent": "a"}),
        (f"/runs/{repo.run_id}/conflicts", {"entity_key": "e", "field_key": "f", "claim_ids": [str(uuid4())]}),
        (f"/internal/runs/{repo.run_id}/events", {"event_type": "agent_progress", "message": "m"}),
        (f"/internal/runs/{repo.run_id}/complete", {"output": {"status": "success"}}),
        (f"/internal/runs/{repo.run_id}/fail", {"code": "X", "message": "failed"}),
    ]


REJECTED_TOKEN_CASES = [
    (None, 401, "WORKER_AUTH_REQUIRED"),
    ("malformed-token", 401, "WORKER_AUTH_INVALID"),
    ("expired-token", 401, "WORKER_AUTH_INVALID"),
    ("wrong-issuer-token", 401, "WORKER_AUTH_INVALID"),
    ("wrong-audience-token", 401, "WORKER_AUTH_INVALID"),
    ("claims-audience-mismatch-token", 401, "WORKER_AUTH_INVALID"),
    ("browser-supabase-token", 401, "WORKER_AUTH_INVALID"),
    ("unverified-email-token", 401, "WORKER_AUTH_INVALID"),
    ("missing-email-token", 401, "WORKER_AUTH_INVALID"),
    ("unapproved-sa-token", 403, "WORKER_IDENTITY_NOT_APPROVED"),
]


@pytest.mark.parametrize("token,status,code", REJECTED_TOKEN_CASES)
def test_every_worker_route_rejects_bad_tokens_without_mutation(repo, token, status, code):
    c = client()
    headers = worker_headers(token) if token else {}
    for path, body in worker_routes(repo):
        response = c.post(path, json=body, headers=headers)
        assert response.status_code == status, f"{path} with {token}"
        assert response.json()["error"]["code"] == code
    repo.assert_no_mutations()


def test_spoofed_browser_identity_headers_are_ignored_on_worker_routes(repo):
    # A browser identity (even a real project member) plus spoofed internal
    # headers must never satisfy the worker boundary.
    c = client()
    headers = {
        "x-milo-auth-user-id": str(repo.user_id),
        "x-milo-auth-user-email": "member@example.com",
        "x-goog-authenticated-user-email": APPROVED_SA,
    }
    for path, body in worker_routes(repo):
        response = c.post(path, json=body, headers=headers)
        assert response.status_code == 401, path
        assert response.json()["error"]["code"] == "WORKER_AUTH_REQUIRED"
    repo.assert_no_mutations()


def test_valid_worker_identity_can_write_tool_and_evidence_records(repo):
    c = client()
    headers = worker_headers("valid-worker-token")
    for path, body in worker_routes(repo)[:6]:
        response = c.post(path, json=body, headers=headers)
        assert response.status_code == 201, path
    assert repo.tool_access_requests == 1
    assert repo.tool_grants == 1
    assert repo.tool_usage_rows == 1
    assert repo.sources == 1
    assert repo.claims == 1
    assert repo.conflicts == 1


def test_valid_worker_identity_can_append_events_and_finish_runs(repo):
    c = client()
    headers = worker_headers("valid-worker-token")
    event = c.post(f"/internal/runs/{repo.run_id}/events", json={"event_type": "agent_progress", "message": "step"}, headers=headers)
    assert event.status_code == 201
    unknown = c.post(f"/internal/runs/{repo.run_id}/events", json={"event_type": "not_a_real_event", "message": "x"}, headers=headers)
    assert unknown.status_code == 422
    done = c.post(f"/internal/runs/{repo.run_id}/complete", json={"output": {"status": "success"}}, headers=headers)
    assert done.status_code == 200
    failed = c.post(f"/internal/runs/{repo.run_id}/fail", json={"code": "ENGINE_FAILED", "message": "boom"}, headers=headers)
    assert failed.status_code == 200
    assert repo.completed_runs == 1
    assert repo.failed_runs == 1


def test_valid_worker_identity_with_execution_flag_disabled_is_rejected(repo, monkeypatch):
    monkeypatch.delenv("MILO_ENABLE_EXECUTION_CONTROL", raising=False)
    c = client()
    headers = worker_headers("valid-worker-token")
    for path, body in worker_routes(repo):
        response = c.post(path, json=body, headers=headers)
        assert response.status_code == 403, path
        assert response.json()["error"]["code"] == "EXECUTION_SURFACE_DISABLED"
    repo.assert_no_mutations()


def test_unconfigured_worker_auth_fails_closed_even_with_valid_token(repo, monkeypatch):
    for missing in ("MILO_WORKER_AUDIENCE", "MILO_APPROVED_WORKER_IDENTITIES"):
        monkeypatch.setenv("MILO_WORKER_AUDIENCE", AUDIENCE)
        monkeypatch.setenv("MILO_APPROVED_WORKER_IDENTITIES", APPROVED_SA)
        monkeypatch.delenv(missing, raising=False)
        response = client().post(f"/runs/{repo.run_id}/sources", json=worker_routes(repo)[3][1], headers=worker_headers("valid-worker-token"))
        assert response.status_code == 503, missing
        assert response.json()["error"]["code"] == "WORKER_AUTH_NOT_CONFIGURED"
    repo.assert_no_mutations()


def test_flag_alone_is_never_sufficient_authorization(repo):
    # No token, flag enabled: still rejected. This is the core invariant.
    response = client().post(f"/runs/{repo.run_id}/claims", json=worker_routes(repo)[4][1])
    assert response.status_code == 401
    repo.assert_no_mutations()

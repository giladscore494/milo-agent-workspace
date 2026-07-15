"""Trusted-gateway identity verification tests.

Identity headers must never be trusted merely because the caller reached
the private Cloud Run service: a verified, allowlisted gateway service
identity is required whenever gateway auth is configured, and production
fails closed when it is not.
"""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.dependencies import get_job_launcher, get_repository
from backend.gateway_auth import get_gateway_token_verifier
from backend.main import app
from tests.test_api import FakeLauncher, FakeRepo

GATEWAY_AUDIENCE = "https://milo-api.example.internal"
APPROVED_GATEWAY_SA = "milo-gateway@example-project.iam.gserviceaccount.com"
WORKER_SA = "milo-worker@example-project.iam.gserviceaccount.com"


def gateway_claims(**overrides):
    claims = {
        "iss": "https://accounts.google.com",
        "aud": GATEWAY_AUDIENCE,
        "email": APPROVED_GATEWAY_SA,
        "email_verified": True,
        "sub": "gw",
    }
    claims.update(overrides)
    return claims


class FakeGatewayVerifier:
    TOKENS = {
        "valid-gateway-token": gateway_claims(),
        "wrong-issuer-token": gateway_claims(iss="https://evil.example"),
        "claims-aud-mismatch-token": gateway_claims(aud="https://other.example"),
        "worker-identity-token": gateway_claims(email=WORKER_SA),
        "unverified-email-token": gateway_claims(email_verified=False),
    }
    REJECT = {
        "expired-token": "Token expired",
        "malformed-token": "Wrong number of segments in token",
        "browser-supabase-token": "Could not verify token signature",
        "wrong-audience-token": "Token has wrong audience",
    }

    def verify(self, token, audience):
        assert audience == GATEWAY_AUDIENCE
        if token in self.REJECT:
            raise ValueError(self.REJECT[token])
        if token in self.TOKENS:
            return dict(self.TOKENS[token])
        raise ValueError("Could not verify token signature")


@pytest.fixture
def repo(monkeypatch):
    fake = FakeRepo()
    launcher = FakeLauncher()
    monkeypatch.setenv("MILO_GATEWAY_AUDIENCE", GATEWAY_AUDIENCE)
    monkeypatch.setenv("MILO_APPROVED_GATEWAY_IDENTITIES", APPROVED_GATEWAY_SA)
    monkeypatch.setenv("MILO_ENABLE_RUN_CREATION", "true")
    app.dependency_overrides[get_repository] = lambda: fake
    app.dependency_overrides[get_job_launcher] = lambda: launcher
    app.dependency_overrides[get_gateway_token_verifier] = lambda: FakeGatewayVerifier()
    fake.launcher = launcher
    yield fake
    app.dependency_overrides.clear()


def client():
    return TestClient(app)


def identity_headers(repo, gateway_token=None):
    headers = {"x-milo-auth-user-id": str(repo.user_id)}
    if gateway_token:
        headers["X-Milo-Gateway-Token"] = gateway_token
    return headers


REJECTED_TOKENS = [
    (None, 401, "GATEWAY_AUTH_REQUIRED"),
    ("malformed-token", 401, "GATEWAY_AUTH_INVALID"),
    ("expired-token", 401, "GATEWAY_AUTH_INVALID"),
    ("wrong-issuer-token", 401, "GATEWAY_AUTH_INVALID"),
    ("wrong-audience-token", 401, "GATEWAY_AUTH_INVALID"),
    ("claims-aud-mismatch-token", 401, "GATEWAY_AUTH_INVALID"),
    ("browser-supabase-token", 401, "GATEWAY_AUTH_INVALID"),
    ("unverified-email-token", 401, "GATEWAY_AUTH_INVALID"),
    ("worker-identity-token", 403, "GATEWAY_IDENTITY_NOT_APPROVED"),
    ("unknown-token", 401, "GATEWAY_AUTH_INVALID"),
]


@pytest.mark.parametrize("token,status,code", REJECTED_TOKENS)
def test_identity_headers_rejected_without_verified_gateway(repo, token, status, code):
    response = client().get("/projects", headers=identity_headers(repo, token))
    assert response.status_code == status
    assert response.json()["error"]["code"] == code


def test_spoofed_headers_without_gateway_token_never_mutate_or_launch(repo):
    # Direct-to-Cloud-Run caller with spoofed internal headers.
    response = client().post(
        f"/conversations/{repo.conversation_id}/runs",
        json={"content": "go", "idempotency_key": "key-1234567890"},
        headers=identity_headers(repo),
    )
    assert response.status_code == 401
    repo.assert_no_mutations()
    assert repo.launcher.launched == []


def test_worker_identity_cannot_impersonate_browser_users(repo):
    response = client().post(
        f"/conversations/{repo.conversation_id}/runs",
        json={"content": "go", "idempotency_key": "key-1234567890"},
        headers=identity_headers(repo, "worker-identity-token"),
    )
    assert response.status_code == 403
    repo.assert_no_mutations()
    assert repo.launcher.launched == []


def test_approved_gateway_identity_allows_normal_authorization_flow(repo):
    c = client()
    ok = c.get("/projects", headers=identity_headers(repo, "valid-gateway-token"))
    assert ok.status_code == 200
    run = c.post(
        f"/conversations/{repo.conversation_id}/runs",
        json={"content": "go", "idempotency_key": "key-1234567890"},
        headers=identity_headers(repo, "valid-gateway-token"),
    )
    assert run.status_code == 202
    assert len(repo.launcher.launched) == 1
    # Membership authorization still runs after gateway verification.
    stranger = c.get("/projects", headers={"x-milo-auth-user-id": str(uuid4()), "X-Milo-Gateway-Token": "valid-gateway-token"})
    assert stranger.status_code == 200
    assert stranger.json() == []


def test_partial_configuration_fails_closed(repo, monkeypatch):
    monkeypatch.delenv("MILO_APPROVED_GATEWAY_IDENTITIES", raising=False)
    response = client().get("/projects", headers=identity_headers(repo, "valid-gateway-token"))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "GATEWAY_AUTH_NOT_CONFIGURED"


def test_production_without_gateway_auth_fails_closed(repo, monkeypatch):
    monkeypatch.delenv("MILO_GATEWAY_AUDIENCE", raising=False)
    monkeypatch.delenv("MILO_APPROVED_GATEWAY_IDENTITIES", raising=False)
    monkeypatch.delenv("MILO_ALLOW_INSECURE_DEV_IDENTITY", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "production")
    response = client().get("/projects", headers=identity_headers(repo))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "GATEWAY_AUTH_NOT_CONFIGURED"


def test_production_with_insecure_dev_identity_opt_in_is_forbidden(repo, monkeypatch):
    monkeypatch.delenv("MILO_GATEWAY_AUDIENCE", raising=False)
    monkeypatch.delenv("MILO_APPROVED_GATEWAY_IDENTITIES", raising=False)
    monkeypatch.setenv("MILO_ALLOW_INSECURE_DEV_IDENTITY", "true")
    monkeypatch.setenv("ENVIRONMENT", "production")
    response = client().get("/projects", headers=identity_headers(repo))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "INSECURE_DEV_IDENTITY_FORBIDDEN"


def test_missing_identity_header_with_valid_gateway_is_still_401(repo):
    response = client().get("/projects", headers={"X-Milo-Gateway-Token": "valid-gateway-token"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"

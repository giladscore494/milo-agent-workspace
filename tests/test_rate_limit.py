"""Backend shared rate limiter tests (memory + Upstash + fail-closed)."""

import pytest

from backend import rate_limit
from backend.rate_limit import (
    MemoryRateLimiter,
    RateLimitExceeded,
    RateLimiterUnavailable,
    UpstashRateLimiter,
    enforce_rate_limit,
    hash_identifier,
)


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    rate_limit.reset_for_tests()
    monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "local")
    yield
    rate_limit.reset_for_tests()


def test_identifiers_are_hashed_not_stored_raw():
    hashed = hash_identifier("supabase-access-token-secret")
    assert "secret" not in hashed
    assert len(hashed) == 32


def test_memory_limiter_expires_windows():
    now = [0.0]
    limiter = MemoryRateLimiter(clock=lambda: now[0])
    assert limiter.increment("k", 60)[0] == 1
    assert limiter.increment("k", 60)[0] == 2
    now[0] = 61.0
    assert limiter.increment("k", 60)[0] == 1


def test_memory_limiter_bounds_key_cardinality():
    limiter = MemoryRateLimiter(max_buckets=100)
    for i in range(500):
        limiter.increment(f"key-{i}", 60)
    assert len(limiter._buckets) <= 100


def test_enforce_raises_429_with_retry_after(monkeypatch):
    monkeypatch.setenv("MILO_RATE_LIMIT_CANCELLATION", "2")
    enforce_rate_limit("cancellation", "user-a")
    enforce_rate_limit("cancellation", "user-a")
    with pytest.raises(RateLimitExceeded) as exc:
        enforce_rate_limit("cancellation", "user-a")
    assert exc.value.status_code == 429
    assert int(exc.value.headers["Retry-After"]) >= 1


def test_user_isolation():
    for _ in range(5):
        enforce_rate_limit("run_creation_user", "user-a")
    with pytest.raises(RateLimitExceeded):
        enforce_rate_limit("run_creation_user", "user-a")
    # A different user is unaffected.
    enforce_rate_limit("run_creation_user", "user-b")


def test_project_isolation(monkeypatch):
    monkeypatch.setenv("MILO_RATE_LIMIT_RUN_CREATION_PROJECT", "1")
    enforce_rate_limit("run_creation_project", "project-a")
    with pytest.raises(RateLimitExceeded):
        enforce_rate_limit("run_creation_project", "project-a")
    enforce_rate_limit("run_creation_project", "project-b")


def test_production_without_shared_store_fails_closed(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    with pytest.raises(RateLimiterUnavailable) as exc:
        enforce_rate_limit("run_creation_user", "user-a")
    assert exc.value.status_code == 503


class FakeUpstashBackend:
    """Simulates a shared Redis reachable from multiple limiter instances."""

    def __init__(self):
        self.counters: dict[str, int] = {}
        self.down = False

    def post(self, url, commands):
        if self.down:
            raise ConnectionError("redis unreachable")
        key = commands[0][1]
        self.counters[key] = self.counters.get(key, 0) + 1
        return [
            {"result": self.counters[key]},
            {"result": 1},
            {"result": 30000},
        ]


def test_upstash_instances_share_state_across_processes(monkeypatch):
    monkeypatch.setenv("MILO_RATE_LIMIT_RUN_CREATION_USER", "3")
    backend = FakeUpstashBackend()
    instance_a = UpstashRateLimiter("https://redis.example", "token", http_post=backend.post)
    instance_b = UpstashRateLimiter("https://redis.example", "token", http_post=backend.post)
    enforce_rate_limit("run_creation_user", "user-a", limiter=instance_a)
    enforce_rate_limit("run_creation_user", "user-a", limiter=instance_b)
    enforce_rate_limit("run_creation_user", "user-a", limiter=instance_a)
    with pytest.raises(RateLimitExceeded):
        enforce_rate_limit("run_creation_user", "user-a", limiter=instance_b)


def test_upstash_unavailable_fails_closed():
    backend = FakeUpstashBackend()
    backend.down = True
    limiter = UpstashRateLimiter("https://redis.example", "token", http_post=backend.post)
    with pytest.raises(RateLimiterUnavailable):
        enforce_rate_limit("run_creation_user", "user-a", limiter=limiter)


def test_retry_after_derived_from_ttl():
    backend = FakeUpstashBackend()
    limiter = UpstashRateLimiter("https://redis.example", "token", http_post=backend.post)
    count, retry = limiter.increment("k", 60)
    assert count == 1
    assert retry == pytest.approx(30.0)


def test_worker_route_rate_limits_by_service_identity(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.dependencies import get_job_launcher, get_repository
    from backend.main import app
    from backend.worker_auth import get_token_verifier
    from tests.test_api import FakeLauncher, FakeRepo
    from tests.test_worker_auth import APPROVED_SA, AUDIENCE, FakeVerifier

    fake = FakeRepo()
    monkeypatch.setenv("MILO_ENABLE_EXECUTION_CONTROL", "true")
    monkeypatch.setenv("MILO_WORKER_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("MILO_APPROVED_WORKER_IDENTITIES", APPROVED_SA)
    monkeypatch.setenv("MILO_RATE_LIMIT_WORKER_MUTATIONS", "2")
    app.dependency_overrides[get_repository] = lambda: fake
    app.dependency_overrides[get_job_launcher] = lambda: FakeLauncher()
    app.dependency_overrides[get_token_verifier] = lambda: FakeVerifier()
    try:
        client = TestClient(app)
        headers = {"X-Milo-Worker-Token": "valid-worker-token"}
        body = {"event_type": "agent_progress", "message": "m"}
        assert client.post(f"/internal/runs/{fake.run_id}/events", json=body, headers=headers).status_code == 201
        assert client.post(f"/internal/runs/{fake.run_id}/events", json=body, headers=headers).status_code == 201
        limited = client.post(f"/internal/runs/{fake.run_id}/events", json=body, headers=headers)
        assert limited.status_code == 429
        assert limited.headers.get("retry-after")
    finally:
        app.dependency_overrides.clear()

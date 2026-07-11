"""Backend rate limiting with a shared-store production implementation.

Categories enforced at the API layer (the gateway additionally limits by IP
and per-user before requests reach this service):

- ``run_creation_user``   run creation per authenticated user;
- ``run_creation_project`` run creation per project;
- ``cancellation``        cancellation requests per user;
- ``worker_mutations``    internal worker mutation routes per service identity.

Production must use the shared Upstash Redis store
(``UPSTASH_REDIS_REST_URL`` / ``UPSTASH_REDIS_REST_TOKEN``). When the store
is required but unconfigured or unreachable, limited surfaces fail closed
(503) instead of running unmetered. Identifiers are hashed before being
used as keys; raw tokens are never stored.
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from backend.errors import AppError

DEFAULT_LIMITS: dict[str, tuple[int, int]] = {
    # category: (requests, window seconds)
    "run_creation_user": (5, 60),
    "run_creation_project": (20, 60),
    "cancellation": (10, 60),
    "worker_mutations": (600, 60),
}

ENV_KEYS: dict[str, str] = {
    "run_creation_user": "MILO_RATE_LIMIT_RUN_CREATION_USER",
    "run_creation_project": "MILO_RATE_LIMIT_RUN_CREATION_PROJECT",
    "cancellation": "MILO_RATE_LIMIT_CANCELLATION",
    "worker_mutations": "MILO_RATE_LIMIT_WORKER_MUTATIONS",
}


class RateLimitExceeded(AppError):
    def __init__(self, retry_after_seconds: int):
        super().__init__("RATE_LIMITED", "too many requests", 429)
        self.headers = {"Retry-After": str(max(1, retry_after_seconds))}


class RateLimiterUnavailable(AppError):
    def __init__(self) -> None:
        super().__init__("RATE_LIMITER_UNAVAILABLE", "shared rate limiter unavailable; request refused", 503)
        self.headers = {"Retry-After": "30"}


def hash_identifier(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _category_limit(category: str) -> tuple[int, int]:
    default_limit, default_window = DEFAULT_LIMITS[category]
    raw = os.getenv(ENV_KEYS[category], "").strip()
    if raw:
        try:
            value = int(raw)
            if value > 0:
                default_limit = value
        except ValueError:
            pass
    return default_limit, default_window


@dataclass
class MemoryRateLimiter:
    """Deterministic in-memory fixed-window limiter for tests/local dev."""

    clock: Callable[[], float] = time.monotonic
    max_buckets: int = 10_000
    _buckets: dict[str, tuple[int, float]] = field(default_factory=dict)

    def increment(self, key: str, window_seconds: int) -> tuple[int, float]:
        now = self.clock()
        if len(self._buckets) >= self.max_buckets:
            self._buckets = {k: v for k, v in self._buckets.items() if v[1] > now}
            while len(self._buckets) >= self.max_buckets:
                self._buckets.pop(next(iter(self._buckets)))
        count, reset_at = self._buckets.get(key, (0, 0.0))
        if reset_at <= now:
            count, reset_at = 0, now + window_seconds
        count += 1
        self._buckets[key] = (count, reset_at)
        return count, reset_at - now


class UpstashRateLimiter:
    """Shared fixed-window limiter over the Upstash Redis REST API."""

    def __init__(self, url: str, token: str, http_post: Callable[..., Any] | None = None):
        self.url = url.rstrip("/")
        self.token = token
        self._http_post = http_post

    def _post(self, commands: list[list[str]]) -> list[dict[str, Any]]:
        if self._http_post is not None:
            return self._http_post(f"{self.url}/pipeline", commands)
        import httpx

        response = httpx.post(
            f"{self.url}/pipeline",
            json=commands,
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=3.0,
        )
        response.raise_for_status()
        return response.json()

    def increment(self, key: str, window_seconds: int) -> tuple[int, float]:
        try:
            results = self._post([
                ["INCR", key],
                ["PEXPIRE", key, str(window_seconds * 1000), "NX"],
                ["PTTL", key],
            ])
            count = int(results[0]["result"])
            ttl_ms = int(results[2]["result"])
        except Exception as exc:
            raise RateLimiterUnavailable() from exc
        ttl = ttl_ms / 1000 if ttl_ms > 0 else float(window_seconds)
        return count, ttl


_memory_limiter = MemoryRateLimiter()
_shared_limiter: UpstashRateLimiter | None = None
_shared_resolved = False


def reset_for_tests() -> None:
    global _memory_limiter, _shared_limiter, _shared_resolved
    _memory_limiter = MemoryRateLimiter()
    _shared_limiter = None
    _shared_resolved = False


def _resolve_shared() -> UpstashRateLimiter | None:
    global _shared_limiter, _shared_resolved
    if not _shared_resolved:
        url = os.getenv("UPSTASH_REDIS_REST_URL", "").strip()
        token = os.getenv("UPSTASH_REDIS_REST_TOKEN", "").strip()
        _shared_limiter = UpstashRateLimiter(url, token) if url and token else None
        _shared_resolved = True
    return _shared_limiter


def _is_production() -> bool:
    return os.getenv("ENVIRONMENT", "local").strip().lower() == "production"


def enforce_rate_limit(category: str, identifier: str, limiter: Any | None = None) -> None:
    """Raise 429 (with Retry-After) or 503 when the request must not proceed."""
    limit, window_seconds = _category_limit(category)
    key = f"rl:{category}:{hash_identifier(identifier)}"
    active = limiter or _resolve_shared()
    if active is None:
        if _is_production():
            # Shared limiter is mandatory for these categories in production.
            raise RateLimiterUnavailable()
        active = _memory_limiter
    count, retry_after = active.increment(key, window_seconds)
    if count > limit:
        raise RateLimitExceeded(int(retry_after) or 1)

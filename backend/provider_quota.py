"""ONE organization-wide admission gate for every paid Kimi provider request.

Why this module exists
----------------------

Kimi inference limits (concurrency / RPM / TPM / TPD) are **not** scoped to an
API key and are shared across models: the official rate-limit and error
documentation describes them as account/organization level. Therefore
``vehicle_catalog_v1``, ``swarm_v2``, every run, every Cloud Run Job execution,
every worker process and every replica draw from ONE allowance.

A process-local ``BoundedSemaphore`` (which is all
``backend.provider_scheduler`` had) cannot express that: two Cloud Run
executions each admit up to their own local ceiling and the organization sees
the sum. This module is the shared coordinator that makes the ceiling real.

What it covers, atomically
--------------------------

1. global inference **concurrency leases** (owner + TTL + heartbeat);
2. global **RPM** rolling-window admission;
3. global **TPM** reservation/admission;
4. ``/v1/tools/search`` QPS;
5. ``/v1/tools/search_pro`` QPS.

Derivation of the ceilings
--------------------------

Account-specific Tier 2 evidence (see ``docs/production-readiness/
KIMI_TIER2_LIMITS.md`` for sources and dates) states chat/inference
concurrency 40, RPM 100, TPM 3,000,000, TPD Unlimited. Every finite MILO
ceiling is ``floor(provider_limit * 0.80)``. TPD has no provider-derived
number because the provider value is Unlimited; MILO's own token, daily,
cost, call, step, tool and duration budgets remain mandatory and are enforced
by ``backend.budget``.

The exact Tier 2 Web Search QPS was **not** independently recoverable from the
official tier table, so it is NOT invented: both search endpoints fall back to
a deliberately conservative 1 request per second each, globally. The two
endpoints are independent buckets (search QPS does not consume chat RPM/TPM/
concurrency and vice versa), but each bucket is shared by V1 and V2.

No burst allowance is assumed. Admission is a conservative rolling window;
being under MILO's 80% ceiling never makes a provider 429 impossible, because
the provider may throttle below nominal tier limits and because another
application on the same organization can consume capacity MILO cannot see.

Key scope
---------

Keys are derived from server-owned configuration only
(``MILO_PROVIDER_QUOTA_SCOPE``, default ``kimi-org``). A run, a plan, a task,
an event payload, request metadata or a browser client can never supply,
influence or override a limiter key: nothing in this module reads them.
"""

from __future__ import annotations

import math
import os
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from backend.errors import AppError

# --- authoritative provider evidence ----------------------------------------

#: Account-specific Kimi Tier 2 evidence. Changing any of these is a reviewed
#: configuration change: see the module docstring and the limits document.
KIMI_TIER2_PROVIDER_LIMITS: dict[str, int | None] = {
    "inference_concurrency": 40,
    "rpm": 100,
    "tpm": 3_000_000,
    # Unlimited: there is no provider number to take 80% of.
    "tpd": None,
}

#: Every finite MILO ceiling is floor(provider_limit * SAFETY_FACTOR).
SAFETY_FACTOR = 0.80


def milo_ceiling(provider_limit: int | None) -> int | None:
    """floor(provider_limit * 0.80), or None when the provider value is Unlimited."""
    if provider_limit is None:
        return None
    return int(math.floor(provider_limit * SAFETY_FACTOR))


MAX_INFERENCE_CONCURRENCY = milo_ceiling(KIMI_TIER2_PROVIDER_LIMITS["inference_concurrency"])  # 32
MAX_RPM = milo_ceiling(KIMI_TIER2_PROVIDER_LIMITS["rpm"])                                      # 80
MAX_TPM = milo_ceiling(KIMI_TIER2_PROVIDER_LIMITS["tpm"])                                      # 2_400_000
MAX_TPD = milo_ceiling(KIMI_TIER2_PROVIDER_LIMITS["tpd"])                                      # None

#: The rolling admission window both RPM and TPM are measured over.
WINDOW_SECONDS = 60

#: Web Search endpoints. Independent provider buckets; each shared by V1 + V2.
SEARCH_BASIC = "search"
SEARCH_PRO = "search_pro"
SEARCH_ENDPOINTS = (SEARCH_BASIC, SEARCH_PRO)

#: Temporary conservative fallback: the exact Tier 2 Web Search QPS was not
#: verifiable from the official tier table. Replace an endpoint's value with
#: floor(L * 0.80) ONLY after an authoritative L is verified for it.
SEARCH_QPS_FALLBACK: dict[str, int] = {SEARCH_BASIC: 1, SEARCH_PRO: 1}

#: True while the above are unverified fallbacks rather than derived ceilings.
SEARCH_QPS_VERIFIED: dict[str, bool] = {SEARCH_BASIC: False, SEARCH_PRO: False}


class ProviderQuotaUnavailable(AppError):
    """The shared coordinator is required but unconfigured or unreachable."""

    def __init__(self, detail: str = "shared provider quota coordinator unavailable") -> None:
        super().__init__("PROVIDER_QUOTA_UNAVAILABLE", detail, 503)


class ProviderQuotaExhausted(Exception):
    """Admission was refused because a shared ceiling is currently full.

    ``dimension`` names WHICH organization ceiling refused, so a caller can
    report the real reason instead of a generic wait, and ``retry_after`` is
    the coordinator's own estimate of when capacity frees up.
    """

    def __init__(self, dimension: str, retry_after: float, *, detail: str = ""):
        super().__init__(detail or f"organization {dimension} capacity is exhausted")
        self.dimension = dimension
        self.retry_after = max(0.0, float(retry_after))


# --- backend protocol --------------------------------------------------------


class QuotaBackend(Protocol):
    """Atomic primitives the coordinator needs. Every method must be atomic
    with respect to concurrent callers in OTHER processes, not merely other
    threads."""

    def acquire_concurrency(self, key: str, lease_id: str, limit: int,
                            ttl_ms: int, now_ms: int) -> tuple[bool, int]: ...

    def heartbeat_concurrency(self, key: str, lease_id: str, ttl_ms: int,
                              now_ms: int) -> bool: ...

    def release_concurrency(self, key: str, lease_id: str) -> bool: ...

    def admit_window(self, key: str, entry_id: str, limit: int, window_ms: int,
                     now_ms: int, weight: int) -> tuple[bool, int, float]: ...

    def admit_interval(self, key: str, interval_ms: int,
                       now_ms: int) -> tuple[bool, float]: ...

    def set_pause(self, key: str, until_ms: int) -> None: ...

    def pause_remaining(self, key: str, now_ms: int) -> float: ...


# --- deterministic in-process backend ---------------------------------------


class MemoryQuotaBackend:
    """Deterministic, thread-safe backend for tests and single-process local dev.

    It is a faithful model of the Redis semantics, so a test that proves a race
    is impossible here is testing the same algorithm production runs. It is NOT
    a production backend: it cannot coordinate across processes, and
    :func:`resolve_coordinator` refuses it in production.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._leases: dict[str, dict[str, int]] = {}
        self._windows: dict[str, list[tuple[int, str, int]]] = {}
        self._intervals: dict[str, int] = {}
        self._pauses: dict[str, int] = {}

    def acquire_concurrency(self, key, lease_id, limit, ttl_ms, now_ms):
        with self._lock:
            holders = self._leases.setdefault(key, {})
            for held, expiry in list(holders.items()):
                if expiry <= now_ms:
                    del holders[held]
            if len(holders) >= limit:
                return False, len(holders)
            holders[lease_id] = now_ms + ttl_ms
            return True, len(holders)

    def heartbeat_concurrency(self, key, lease_id, ttl_ms, now_ms):
        with self._lock:
            holders = self._leases.get(key, {})
            # Renew ONLY an unexpired lease this owner still holds. A lease that
            # already expired is gone: renewing it would resurrect capacity a
            # replacement may already hold.
            if holders.get(lease_id, 0) <= now_ms:
                holders.pop(lease_id, None)
                return False
            holders[lease_id] = now_ms + ttl_ms
            return True

    def release_concurrency(self, key, lease_id):
        with self._lock:
            # Keyed by the unique lease id, so an expired lease's late release
            # can never free a DIFFERENT owner's replacement lease.
            return self._leases.get(key, {}).pop(lease_id, None) is not None

    def admit_window(self, key, entry_id, limit, window_ms, now_ms, weight):
        with self._lock:
            entries = [e for e in self._windows.get(key, []) if e[0] > now_ms - window_ms]
            used = sum(e[2] for e in entries)
            if used + weight > limit:
                oldest = min(e[0] for e in entries) if entries else now_ms
                retry = max(0.0, (oldest + window_ms - now_ms) / 1000.0)
                self._windows[key] = entries
                return False, used, retry
            entries.append((now_ms, entry_id, weight))
            self._windows[key] = entries
            return True, used + weight, 0.0

    def admit_interval(self, key, interval_ms, now_ms):
        with self._lock:
            last = self._intervals.get(key, 0)
            if now_ms - last < interval_ms:
                return False, max(0.0, (last + interval_ms - now_ms) / 1000.0)
            self._intervals[key] = now_ms
            return True, 0.0

    def set_pause(self, key, until_ms):
        with self._lock:
            self._pauses[key] = max(self._pauses.get(key, 0), until_ms)

    def pause_remaining(self, key, now_ms):
        with self._lock:
            until = self._pauses.get(key, 0)
            return max(0.0, (until - now_ms) / 1000.0)


# --- Upstash Redis backend ---------------------------------------------------

# Each script is ONE atomic Redis execution. Expiries are set on every write so
# an abandoned key cannot leak capacity forever.

_LUA_ACQUIRE = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[4])
local active = redis.call('ZCARD', KEYS[1])
if active >= tonumber(ARGV[2]) then return {0, active} end
redis.call('ZADD', KEYS[1], ARGV[3], ARGV[1])
redis.call('PEXPIRE', KEYS[1], ARGV[5])
return {1, active + 1}
"""

_LUA_HEARTBEAT = """
local score = redis.call('ZSCORE', KEYS[1], ARGV[1])
if not score or tonumber(score) <= tonumber(ARGV[3]) then
  redis.call('ZREM', KEYS[1], ARGV[1])
  return 0
end
redis.call('ZADD', KEYS[1], ARGV[2], ARGV[1])
redis.call('PEXPIRE', KEYS[1], ARGV[4])
return 1
"""

_LUA_RELEASE = "return redis.call('ZREM', KEYS[1], ARGV[1])"

_LUA_ADMIT_WINDOW = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[4])
local used = 0
local entries = redis.call('ZRANGE', KEYS[1], 0, -1)
for _, member in ipairs(entries) do
  local sep = string.find(member, '|', 1, true)
  if sep then used = used + tonumber(string.sub(member, sep + 1)) end
end
local weight = tonumber(ARGV[5])
if used + weight > tonumber(ARGV[2]) then
  local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
  local retry = 0
  if oldest[2] then retry = tonumber(oldest[2]) + tonumber(ARGV[6]) - tonumber(ARGV[3]) end
  if retry < 0 then retry = 0 end
  return {0, used, retry}
end
redis.call('ZADD', KEYS[1], ARGV[3], ARGV[1] .. '|' .. ARGV[5])
redis.call('PEXPIRE', KEYS[1], ARGV[7])
return {1, used + weight, 0}
"""

_LUA_ADMIT_INTERVAL = """
local last = tonumber(redis.call('GET', KEYS[1]) or '0')
local now = tonumber(ARGV[1])
local interval = tonumber(ARGV[2])
if now - last < interval then return {0, last + interval - now} end
redis.call('SET', KEYS[1], ARGV[1], 'PX', ARGV[3])
return {1, 0}
"""


class UpstashQuotaBackend:
    """Production backend over the Upstash Redis REST API.

    Reuses the deployment's existing shared store and credential names. The
    token is read from the environment, never logged, and never travels in a
    key, an event payload or an error message.
    """

    def __init__(self, url: str, token: str, http_post: Callable[..., Any] | None = None,
                 timeout: float = 3.0):
        self.url = url.rstrip("/")
        self._token = token
        self._http_post = http_post
        self._timeout = timeout

    def _eval(self, script: str, keys: list[str], args: list[str]) -> Any:
        body = ["EVAL", script, str(len(keys)), *keys, *args]
        try:
            if self._http_post is not None:
                result = self._http_post(self.url, body)
            else:
                import httpx

                response = httpx.post(
                    self.url, json=body,
                    headers={"Authorization": f"Bearer {self._token}"},
                    timeout=self._timeout)
                response.raise_for_status()
                result = response.json()
        except ProviderQuotaUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - transport shapes vary; fail closed
            # Deliberately does not carry the exception text: a transport error
            # can quote a URL that embeds a credential.
            raise ProviderQuotaUnavailable() from exc
        if isinstance(result, dict):
            if "error" in result:
                raise ProviderQuotaUnavailable()
            return result.get("result")
        return result

    def acquire_concurrency(self, key, lease_id, limit, ttl_ms, now_ms):
        out = self._eval(_LUA_ACQUIRE, [key],
                         [lease_id, str(limit), str(now_ms + ttl_ms), str(now_ms),
                          str(ttl_ms * 4)])
        return bool(int(out[0])), int(out[1])

    def heartbeat_concurrency(self, key, lease_id, ttl_ms, now_ms):
        out = self._eval(_LUA_HEARTBEAT, [key],
                         [lease_id, str(now_ms + ttl_ms), str(now_ms), str(ttl_ms * 4)])
        return bool(int(out))

    def release_concurrency(self, key, lease_id):
        return bool(int(self._eval(_LUA_RELEASE, [key], [lease_id])))

    def admit_window(self, key, entry_id, limit, window_ms, now_ms, weight):
        out = self._eval(_LUA_ADMIT_WINDOW, [key],
                         [entry_id, str(limit), str(now_ms), str(now_ms - window_ms),
                          str(weight), str(window_ms), str(window_ms * 2)])
        return bool(int(out[0])), int(out[1]), max(0.0, float(out[2]) / 1000.0)

    def admit_interval(self, key, interval_ms, now_ms):
        out = self._eval(_LUA_ADMIT_INTERVAL, [key],
                         [str(now_ms), str(interval_ms), str(interval_ms * 4)])
        return bool(int(out[0])), max(0.0, float(out[1]) / 1000.0)

    def set_pause(self, key, until_ms):
        self._eval("redis.call('SET', KEYS[1], ARGV[1], 'PX', ARGV[2]); return 1",
                   [key], [str(until_ms), str(max(1, until_ms))])

    def pause_remaining(self, key, now_ms):
        raw = self._eval("return redis.call('GET', KEYS[1])", [key], [])
        try:
            until = int(raw) if raw else 0
        except (TypeError, ValueError):
            until = 0
        return max(0.0, (until - now_ms) / 1000.0)


# --- the coordinator ---------------------------------------------------------


@dataclass(frozen=True)
class QuotaConfig:
    """Numeric ceilings. Validated fail-closed; never silently widened."""

    max_concurrency: int = MAX_INFERENCE_CONCURRENCY
    max_rpm: int = MAX_RPM
    max_tpm: int = MAX_TPM
    search_qps: tuple[int, int] = (SEARCH_QPS_FALLBACK[SEARCH_BASIC],
                                   SEARCH_QPS_FALLBACK[SEARCH_PRO])
    lease_ttl_seconds: float = 120.0
    scope: str = "kimi-org"

    def __post_init__(self) -> None:
        for name, value in (("max_concurrency", self.max_concurrency),
                            ("max_rpm", self.max_rpm), ("max_tpm", self.max_tpm)):
            if value < 1:
                raise ValueError(f"{name} must be positive")
        # A configured value may TIGHTEN the organization ceiling; it may never
        # exceed it. An environment that tries is a misconfiguration, and the
        # safe reading of a misconfiguration is refusal, not the larger number.
        if self.max_concurrency > MAX_INFERENCE_CONCURRENCY:
            raise ValueError("max_concurrency exceeds the organization ceiling")
        if self.max_rpm > MAX_RPM:
            raise ValueError("max_rpm exceeds the organization ceiling")
        if self.max_tpm > MAX_TPM:
            raise ValueError("max_tpm exceeds the organization ceiling")
        for qps, endpoint in zip(self.search_qps, SEARCH_ENDPOINTS):
            if qps < 1:
                raise ValueError("search qps must be positive")
            if not SEARCH_QPS_VERIFIED[endpoint] and qps > SEARCH_QPS_FALLBACK[endpoint]:
                raise ValueError(
                    f"{endpoint} qps exceeds the conservative fallback while the "
                    "authoritative Tier 2 Web Search QPS is unverified")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "QuotaConfig":
        source = dict(os.environ if env is None else env)

        def _int(key: str, default: int) -> int:
            raw = (source.get(key) or "").strip()
            if not raw:
                return default
            try:
                value = int(raw)
            except ValueError:
                raise ValueError(f"{key} must be an integer") from None
            if value <= 0:
                raise ValueError(f"{key} must be positive")
            return value

        # Deliberately NOT the MILO_PROVIDER_* names: those configure ONE
        # process's active profile (how much of MILO's share this engine uses),
        # while these are the ORGANIZATION ceiling that every process shares.
        # Conflating them is how a per-process value silently became an
        # account-wide claim.
        return cls(
            max_concurrency=_int("MILO_ORG_MAX_CONCURRENCY", MAX_INFERENCE_CONCURRENCY),
            max_rpm=_int("MILO_ORG_RPM_LIMIT", MAX_RPM),
            max_tpm=_int("MILO_ORG_TPM_LIMIT", MAX_TPM),
            search_qps=(_int("MILO_SEARCH_BASIC_QPS", SEARCH_QPS_FALLBACK[SEARCH_BASIC]),
                        _int("MILO_SEARCH_PRO_QPS", SEARCH_QPS_FALLBACK[SEARCH_PRO])),
            lease_ttl_seconds=float(_int("MILO_PROVIDER_LEASE_TTL_SECONDS", 120)),
            scope=(source.get("MILO_PROVIDER_QUOTA_SCOPE") or "kimi-org").strip() or "kimi-org",
        )


@dataclass
class InferenceLease:
    """One held unit of organization inference concurrency.

    The id is unique per acquisition, so releasing an expired lease can never
    free a replacement holder's slot, and a double release is a no-op rather
    than a silent capacity leak.
    """

    lease_id: str
    coordinator: "ProviderQuotaCoordinator"
    released: bool = False

    def heartbeat(self) -> bool:
        if self.released:
            return False
        return self.coordinator.heartbeat_inference(self.lease_id)

    def release(self) -> bool:
        if self.released:
            return False
        self.released = True
        return self.coordinator.release_inference(self.lease_id)


class ProviderQuotaCoordinator:
    """The single shared admission authority for paid provider requests."""

    def __init__(self, backend: QuotaBackend, config: QuotaConfig | None = None, *,
                 clock: Callable[[], float] | None = None,
                 diagnostic_sink: Callable[[str, dict[str, Any]], None] | None = None):
        self._backend = backend
        self.config = config or QuotaConfig()
        self._clock = clock or time.time
        self._diagnostic = diagnostic_sink

    # -- keys (server-owned; nothing here reads request/plan/run data) --------
    def _key(self, dimension: str) -> str:
        return f"milo:pq:{self.config.scope}:{dimension}"

    def _now_ms(self) -> int:
        return int(self._clock() * 1000)

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        if self._diagnostic:
            self._diagnostic(kind, payload)

    # -- inference concurrency ------------------------------------------------
    def try_acquire_inference(self) -> InferenceLease | None:
        now = self._now_ms()
        paused = self._backend.pause_remaining(self._key("pause:inference"), now)
        if paused > 0:
            return None
        lease_id = uuid.uuid4().hex
        granted, _active = self._backend.acquire_concurrency(
            self._key("conc"), lease_id, self.config.max_concurrency,
            int(self.config.lease_ttl_seconds * 1000), now)
        return InferenceLease(lease_id, self) if granted else None

    def heartbeat_inference(self, lease_id: str) -> bool:
        return self._backend.heartbeat_concurrency(
            self._key("conc"), lease_id,
            int(self.config.lease_ttl_seconds * 1000), self._now_ms())

    def release_inference(self, lease_id: str) -> bool:
        return self._backend.release_concurrency(self._key("conc"), lease_id)

    # -- RPM / TPM ------------------------------------------------------------
    def try_admit_request(self, reserved_tokens: int) -> tuple[bool, str, float]:
        """Admit one request against RPM and TPM, or say which one refused.

        ``reserved_tokens`` MUST be ``estimated_input_tokens +
        explicit_max_completion_tokens``: Kimi admits a request against the
        requested completion cap, not against the output it later generates,
        so admitting on actual usage would systematically under-count.

        RPM is taken first and, when TPM then refuses, the RPM entry is NOT
        rolled back: the request was really offered to the window, and pruning
        it would let a caller retry-storm past the ceiling. The window ages it
        out naturally.
        """
        now = self._now_ms()
        for dimension in ("inference", "tpm"):
            paused = self._backend.pause_remaining(self._key(f"pause:{dimension}"), now)
            if paused > 0:
                return False, f"{dimension}_paused", paused
        if reserved_tokens < 1:
            raise ValueError("reserved_tokens must include an explicit output cap")
        if reserved_tokens > self.config.max_tpm:
            raise ProviderQuotaExhausted(
                "tpm", 0.0,
                detail="a single request's reserved tokens exceed the organization TPM ceiling")
        entry = uuid.uuid4().hex
        admitted, _used, retry = self._backend.admit_window(
            self._key("rpm"), entry, self.config.max_rpm,
            WINDOW_SECONDS * 1000, now, 1)
        if not admitted:
            return False, "rpm", retry
        admitted, _used, retry = self._backend.admit_window(
            self._key("tpm"), entry, self.config.max_tpm,
            WINDOW_SECONDS * 1000, now, reserved_tokens)
        if not admitted:
            return False, "tpm", retry
        return True, "", 0.0

    # -- Web Search QPS -------------------------------------------------------
    def try_admit_search(self, endpoint: str) -> tuple[bool, float]:
        """Admit one Web Search request on ONE endpoint's independent bucket."""
        if endpoint not in SEARCH_ENDPOINTS:
            raise ValueError("unknown search endpoint")
        now = self._now_ms()
        paused = self._backend.pause_remaining(self._key(f"pause:{endpoint}"), now)
        if paused > 0:
            return False, paused
        qps = self.config.search_qps[SEARCH_ENDPOINTS.index(endpoint)]
        interval_ms = max(1, int(1000 / qps))
        return self._backend.admit_interval(self._key(f"search:{endpoint}"), interval_ms, now)

    # -- provider feedback ----------------------------------------------------
    def pause(self, dimension: str, seconds: float, *, reason: str = "") -> None:
        """Stop admitting on one dimension for a while.

        This is the ONLY direction provider feedback may move a ceiling. A
        response header that appears to advertise MORE capacity never widens
        anything here: raising a ceiling requires authoritative verification
        and a reviewed configuration change.
        """
        seconds = max(0.0, float(seconds))
        if seconds <= 0:
            return
        self._backend.set_pause(self._key(f"pause:{dimension}"),
                                self._now_ms() + int(seconds * 1000))
        self._emit("provider_quota_paused",
                   {"dimension": dimension, "seconds": round(seconds, 3), "reason": reason})

    def record_rate_limit_signal(self, *, dimension: str, retry_after: float | None,
                                 reported_limit: int | None = None,
                                 reported_remaining: int | None = None,
                                 local_headroom: int | None = None) -> None:
        """Reconcile a provider 429 against MILO's own accounting.

        When MILO believes it still has substantial headroom but the provider
        refused, the most likely explanation is usage MILO cannot see: Kimi
        inference quota is shared across the organization and is not isolated
        per API key, so another application on the same account can consume it.
        That is surfaced as a limiter-drift diagnostic rather than hidden,
        because hiding it would make MILO look correct while the organization
        is over its limit.

        No credential and no response body travels here: only the dimension,
        the numeric header values the provider chose to publish, and MILO's own
        headroom number.
        """
        self.pause(dimension, retry_after or 1.0, reason="provider_rate_limit")
        payload: dict[str, Any] = {"dimension": dimension}
        if retry_after is not None:
            payload["retry_after"] = round(float(retry_after), 3)
        if reported_limit is not None:
            payload["reported_limit"] = int(reported_limit)
        if reported_remaining is not None:
            payload["reported_remaining"] = int(reported_remaining)
        if local_headroom is not None:
            payload["local_headroom"] = int(local_headroom)
            if local_headroom > 0:
                # MILO thought it could proceed and the provider disagreed.
                payload["drift"] = "external_or_provider_throttle"
                self._emit("provider_limiter_drift", payload)
                return
        self._emit("provider_rate_limited", payload)


# --- resolution --------------------------------------------------------------


def _is_production() -> bool:
    return os.getenv("ENVIRONMENT", "local").strip().lower() == "production"


def resolve_coordinator(config: QuotaConfig | None = None, *,
                        env: dict[str, str] | None = None,
                        diagnostic_sink: Callable[[str, dict[str, Any]], None] | None = None,
                        ) -> ProviderQuotaCoordinator:
    """Build the coordinator for this process, failing closed in production.

    Production MUST use the shared Upstash store: a process-local backend there
    would silently let two Cloud Run executions each admit a full ceiling,
    which is precisely the defect this module exists to remove. When the store
    is unconfigured in production the call raises rather than degrading into
    unmetered capacity.
    """
    source = dict(os.environ if env is None else env)
    url = (source.get("UPSTASH_REDIS_REST_URL") or "").strip()
    token = (source.get("UPSTASH_REDIS_REST_TOKEN") or "").strip()
    resolved = config or QuotaConfig.from_env(source)
    if url and token:
        backend: QuotaBackend = UpstashQuotaBackend(url, token)
    elif source.get("ENVIRONMENT", "local").strip().lower() == "production":
        raise ProviderQuotaUnavailable(
            "shared provider quota store is mandatory in production")
    else:
        backend = MemoryQuotaBackend()
    return ProviderQuotaCoordinator(backend, resolved, diagnostic_sink=diagnostic_sink)


__all__ = [
    "KIMI_TIER2_PROVIDER_LIMITS", "MAX_INFERENCE_CONCURRENCY", "MAX_RPM", "MAX_TPM",
    "MAX_TPD", "SAFETY_FACTOR", "SEARCH_BASIC", "SEARCH_PRO", "SEARCH_ENDPOINTS",
    "SEARCH_QPS_FALLBACK", "SEARCH_QPS_VERIFIED", "WINDOW_SECONDS",
    "InferenceLease", "MemoryQuotaBackend", "ProviderQuotaCoordinator",
    "ProviderQuotaExhausted", "ProviderQuotaUnavailable", "QuotaBackend",
    "QuotaConfig", "UpstashQuotaBackend", "milo_ceiling", "resolve_coordinator",
]

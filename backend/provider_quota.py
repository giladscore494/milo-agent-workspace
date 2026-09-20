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

1. global inference **concurrency leases**, held by default and returned
   only on proven completion (see the ownership invariant below);
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


# --- who owns a unit of organization concurrency, and when it comes back -----
#
# THE SAFETY INVARIANT
# --------------------
#
#     A unit of organization inference concurrency is returned to the pool
#     ONLY when MILO can prove the request that took it is over.
#
# The corollary, which is the whole design: **uncertainty must reduce MILO's
# available capacity, never increase it.** Not knowing whether a request
# finished is not a reason to reuse its slot; it is a reason to keep holding it.
#
# How it is established: by making HOLDING the default and RELEASING the
# deliberate act.
#
#     acquire  -> the lease is stamped with the CRASH-RECOVERY HORIZON below,
#                 not with a request-sized TTL.
#     release  -> removes the lease immediately, and is reached only from a
#                 PROVEN-FINISHED outcome.
#     anything else (a deadline firing, a transport error, a lost ownership
#                 probe, a process dying mid-request, a machine losing power)
#                 -> nobody removes the lease, so the slot stays held until
#                 the horizon.
#
# Note what that means mechanically: quarantine is not an operation that has
# to run. It is the absence of one. A process that is SIGKILLed between
# issuing a request and returning cannot fail to quarantine its slot, because
# quarantining is what happens when nothing happens. There is no failure mode
# in which "uncertain" degrades to "free".
#
# WHY THE PREVIOUS DESIGN WAS NOT ENOUGH
# --------------------------------------
#
# It reclaimed a slot at a REQUEST-sized TTL (120s) and argued safety from
#
#     provider_request_deadline + margin <= lease_ttl
#
# combined with a transport that enforces a total wall-clock deadline
# (`backend.provider_transport`). That argument bounds the MILO side and only
# the MILO side. It proves "no MILO thread is still awaiting this response",
# which is NOT the same statement as "this request is no longer consuming
# organization concurrency". Nothing in the httpx or OpenAI contract cancels
# an in-flight request provider-side, and a server may keep computing after a
# client disconnects. So at the moment the deadline fired, the honest state of
# the request was UNKNOWN -- and the old design answered "unknown" by freeing
# the slot, which is the wrong direction. A second worker could then take it
# while the first request was, for all MILO could tell, still running.
#
# Two things that are NOT the fix, and are deliberately not used here:
#
#   * changing httpx timeouts (read/connect/write/pool) or the retry count.
#     Those move when MILO stops waiting. They do not make the request stop.
#   * wrapping the call in `future.result(timeout=)`, `ThreadPoolExecutor`,
#     `thread.join(timeout=)` or `asyncio.wait_for(asyncio.to_thread(...))`.
#     Those return control to MILO while the underlying socket and the
#     provider-side work continue. Returning control is not termination, and
#     treating it as termination is exactly the error above.
#
# WHY NOTHING RECLAIMS AN UNRELEASED SLOT BY ITSELF
# -------------------------------------------------
#
# The obvious next move is a timer: hold the slot, but reclaim it eventually,
# so a crashed worker cannot strand capacity for good. A previous revision did
# exactly that, deriving the timer from the Cloud Run task timeout -- the
# platform really does kill the worker at `--task-timeout 3600`, and the
# worker installs no SIGTERM handler, so the MILO process and its socket are
# demonstrably gone by then.
#
# Review found the flaw, and it is the same species of error as the two before
# it. That evidence bounds MILO'S PROCESS. The quantity the ceiling is stated
# over is whether KIMI is still counting the request. Those are different
# propositions, and:
#
#     "MILO's process and socket are definitely dead"
#         does NOT entail
#     "the provider is definitely no longer counting this request in-flight"
#
# The provider's own documentation does not close the gap. It defines
# concurrency as "the maximum number of requests from you that we can process
# at the same time" and says concurrency is "released as requests finish" --
# but it never defines when a request finishes from the SERVER's side, never
# promises cancellation on client disconnect, and states no maximum
# server-side request lifetime. The one adjacent signal points the other way:
# HTTP 499 is logged for client disconnects, and 499 by definition means the
# client went away "while the server-side process is still running".
#
# So there is no authoritative provider-side bound to derive a timer from, and
# inventing one is exactly what this module must not do. A timer would not be
# a proof; it would be an assumption wearing a derivation's clothes.
#
# Hence the default: **nothing reclaims an unreleased slot automatically.**
# A lease that is not released by a proven-finished request is held until a
# human deliberately returns it (see `operator_reclaim_inference`). That makes
# the guarantee one MILO can actually keep:
#
#     MILO never admits more concurrent requests than it can prove have
#     finished -- whatever the provider does after a disconnect.
#
# THE COST, AND WHY IT IS THE RIGHT ONE
# -------------------------------------
#
# A worker that is SIGKILLed mid-request permanently consumes one of the 32
# slots until an operator reclaims it. That is a real operational burden and
# it is deliberate: the alternative is handing the slot to a second worker on
# an assumption nobody can check. `held_inference_leases` lists what is held
# and since when, and every quarantine emits `provider_lease_quarantined`, so
# the burden is visible and actionable rather than mysterious.
#
# An operator who has independently established a provider-side bound can opt
# into a timer with `MILO_PROVIDER_ABANDONED_LEASE_RECLAIM_SECONDS`. Doing so
# REPLACES the guarantee above with a weaker one, and says so: the value is
# floored at the process-lifetime bound, `QuotaConfig.concurrency_guarantee`
# reports which of the two is in force, and the first-run profile carries it.
# It is off unless a deployment sets it.

#: Fraction of a horizon held back as safety margin.
LEASE_SAFETY_MARGIN_RATIO = 0.25

#: Floor on that margin, so a short horizon cannot shrink it to nothing.
MIN_LEASE_SAFETY_MARGIN_SECONDS = 15.0

#: How many ownership probes fit inside one nominal lease window.
OWNERSHIP_PROBE_INTERVALS_PER_TTL = 4


#: The shortest horizon that can carry the absolute floor above and still leave
#: a request any time at all. Below it the proportional rule governs on its
#: own: forcing a 15s floor onto, say, a 3s window would make the derived
#: deadline NEGATIVE -- a rule that cannot be satisfied is not a safety rule.
MIN_TTL_FOR_MARGIN_FLOOR = MIN_LEASE_SAFETY_MARGIN_SECONDS / LEASE_SAFETY_MARGIN_RATIO


#: The longest a MILO process that can hold a provider socket may live, taken
#: from the deployed Cloud Run Job task timeout
#: (``scripts/deploy/cloud-run.sh`` ``--task-timeout 3600``; the worker
#: installs no SIGTERM handler, so it cannot outlive it).
#:
#: This does NOT bound provider-side execution, and nothing here treats it as
#: if it did. It is a FLOOR on the opt-in timer below: a deployment that
#: chooses to reclaim on a timer may not choose a value at which its own
#: process could still be alive with the request on the wire.
WORKER_MAX_LIFETIME_SECONDS = 3600.0

#: The two guarantees this module can be configured to make. The first is
#: proven; the second rests on an operator's own assumption about the provider.
GUARANTEE_PROVEN_COMPLETION = "no slot is reused until its request is proven finished"
GUARANTEE_TIMED_RECLAIM = (
    "no slot is reused until its request is proven finished OR the configured "
    "abandoned-lease reclaim elapses; the latter assumes a provider-side bound "
    "MILO cannot verify")


def lease_safety_margin(window_seconds: float) -> float:
    """The part of a window that is deliberately NOT available to a request.

    Proportional, with an absolute floor that applies once the window is long
    enough to carry it. The proportional term keeps the margin meaningful as
    the window changes; the floor stops a production-sized window from having
    a margin too small to absorb clock skew and pauses.
    """
    window = float(window_seconds)
    proportional = window * LEASE_SAFETY_MARGIN_RATIO
    if window >= MIN_TTL_FOR_MARGIN_FLOOR:
        return max(MIN_LEASE_SAFETY_MARGIN_SECONDS, proportional)
    return proportional


def minimum_abandoned_lease_reclaim(worker_max_lifetime_seconds: float) -> float:
    """The SHORTEST a timed reclaim may be set to, if one is enabled at all.

    Not a safe reclaim time -- there is no such thing, because no provider-side
    bound exists to derive one from (see the section above). This only rules
    out the clearly-wrong values: below it, a deployment would be reclaiming a
    slot while the process that took it could still be running, which is a
    defect on top of the assumption it is already making.
    """
    lifetime = float(worker_max_lifetime_seconds)
    return lifetime + lease_safety_margin(lifetime)


def default_request_deadline(lease_ttl_seconds: float) -> float:
    """The longest ONE provider request may keep a MILO thread waiting.

    This is a LIVENESS bound, not the concurrency-safety bound. It governs how
    long a worker blocks before it gives up and settles the lease; the slot's
    return is governed by whether that settlement could prove completion, not
    by this number. Derived from the nominal lease window so the two cannot
    drift into a pair where a request routinely outlives its own accounting.
    """
    return float(lease_ttl_seconds) - lease_safety_margin(lease_ttl_seconds)


def ownership_probe_interval(lease_ttl_seconds: float) -> float:
    """How often to re-read whether a held permit is still recorded as ours."""
    return max(1.0, float(lease_ttl_seconds) / OWNERSHIP_PROBE_INTERVALS_PER_TTL)


def assert_request_deadline_safe(request_deadline_seconds: float,
                                 lease_ttl_seconds: float) -> None:
    """The deadline must fit inside the nominal lease window, with margin.

    Refuses rather than clamping: a deployment that asked for a deadline it
    cannot have is a misconfiguration, and quietly shortening it would hide
    the fact that somebody believed a longer request was allowed.
    """
    deadline = float(request_deadline_seconds)
    ttl = float(lease_ttl_seconds)
    if not deadline > 0:
        raise ValueError("provider request timeout must be positive")
    if not ttl > 0:
        raise ValueError("provider lease TTL must be positive")
    margin = lease_safety_margin(ttl)
    if deadline + margin > ttl:
        raise ValueError(
            f"unsafe provider timeout/lease pair: a {deadline:g}s request "
            f"deadline plus a {margin:g}s safety margin exceeds the {ttl:g}s "
            "nominal lease window, so a request would routinely outlive the "
            "window its own accounting is stated over")


def assert_abandoned_lease_reclaim_safe(reclaim_seconds: float | None,
                                        worker_max_lifetime_seconds: float) -> None:
    """Check an OPT-IN timed reclaim. ``None`` -- the default -- always passes.

    ``None`` means nothing reclaims automatically, which needs no check: it is
    the configuration that makes no unverifiable assumption at all.
    """
    lifetime = float(worker_max_lifetime_seconds)
    if not lifetime > 0:
        raise ValueError("worker max lifetime must be positive")
    if reclaim_seconds is None:
        return
    configured = float(reclaim_seconds)
    required = minimum_abandoned_lease_reclaim(lifetime)
    if configured < required:
        raise ValueError(
            f"unsafe abandoned-lease reclaim: {configured:g}s is shorter than "
            f"the {required:g}s a {lifetime:g}s worker lifetime requires, so a "
            "permit could be reclaimed while the process holding it is still "
            "alive and its request may still be in flight")


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


class LeaseRecoveryRefused(ValueError):
    """An operator asked to reclaim a held lease and MILO said no.

    ``code`` is one of:

    * ``LEASE_NOT_HELD`` -- no lease with that id is held (already released,
      already reclaimed, or never existed);
    * ``LEASE_TOO_YOUNG`` -- the lease is younger than the process-lifetime
      floor (or its age is unknown), so the process that took it may still be
      alive and may still settle it itself;
    * ``REASON_REQUIRED`` -- a reclaim without a recorded reason is not offered.
    """

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code


#: Outcomes of :meth:`QuotaBackend.recover_concurrency`, decided atomically in
#: the store so an operator cannot race a live holder.
RECOVERY_REMOVED = 1
RECOVERY_NOT_HELD = 0
RECOVERY_TOO_YOUNG = -1


def _acquired_key(concurrency_key: str) -> str:
    """The companion hash recording when each held lease was taken."""
    return f"{concurrency_key}:acquired"


# --- backend protocol --------------------------------------------------------


class QuotaBackend(Protocol):
    """Atomic primitives the coordinator needs. Every method must be atomic
    with respect to concurrent callers in OTHER processes, not merely other
    threads."""

    def acquire_concurrency(self, key: str, lease_id: str, limit: int,
                            ttl_ms: int | None, now_ms: int) -> tuple[bool, int]: ...

    def verify_concurrency(self, key: str, lease_id: str, now_ms: int) -> bool: ...

    def held_concurrency(self, key: str,
                         now_ms: int) -> list[tuple[str, float, int]]: ...

    def release_concurrency(self, key: str, lease_id: str) -> bool: ...

    def recover_concurrency(self, key: str, lease_id: str,
                            acquired_before_ms: int) -> int: ...

    def admit_window(self, key: str, entry_id: str, limit: int, window_ms: int,
                     now_ms: int, weight: int) -> tuple[bool, int, float]: ...

    def admit_interval(self, key: str, interval_ms: int,
                       now_ms: int) -> tuple[bool, float]: ...

    def set_pause(self, key: str, until_ms: int, now_ms: int) -> None: ...

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
        #: key -> lease_id -> acquired_at_ms. Recorded beside every lease so an
        #: operator can see how old a held slot is, and so a reclaim can be
        #: refused while the process that took it could still be alive.
        self._acquired: dict[str, dict[str, int]] = {}
        self._windows: dict[str, list[tuple[int, str, int]]] = {}
        self._intervals: dict[str, int] = {}
        self._pauses: dict[str, int] = {}

    def acquire_concurrency(self, key, lease_id, limit, ttl_ms, now_ms):
        """``ttl_ms=None`` holds the lease with no expiry of any kind.

        That is the default, and it is the whole point: an unreleased lease
        must not come back on its own, because nothing MILO can observe proves
        the provider has stopped counting the request.
        """
        with self._lock:
            holders = self._leases.setdefault(key, {})
            acquired = self._acquired.setdefault(key, {})
            for held, expiry in list(holders.items()):
                if expiry <= now_ms:
                    del holders[held]
                    acquired.pop(held, None)
            if len(holders) >= limit:
                return False, len(holders)
            holders[lease_id] = math.inf if ttl_ms is None else now_ms + ttl_ms
            acquired[lease_id] = now_ms
            return True, len(holders)

    def held_concurrency(self, key, now_ms):
        with self._lock:
            acquired = self._acquired.get(key, {})
            return sorted((lease_id, expiry, acquired.get(lease_id, 0))
                          for lease_id, expiry in self._leases.get(key, {}).items()
                          if expiry > now_ms)

    def recover_concurrency(self, key, lease_id, acquired_before_ms):
        """An operator's reclaim, with the age check in the same critical section.

        ``RECOVERY_TOO_YOUNG`` covers an UNKNOWN acquisition time as well: a
        lease whose age cannot be shown is a lease whose process cannot be
        shown to be gone, and that is refused, not assumed.
        """
        with self._lock:
            holders = self._leases.get(key, {})
            if lease_id not in holders:
                self._acquired.get(key, {}).pop(lease_id, None)
                return RECOVERY_NOT_HELD
            acquired = self._acquired.get(key, {}).get(lease_id)
            if acquired is None or acquired > acquired_before_ms:
                return RECOVERY_TOO_YOUNG
            del holders[lease_id]
            self._acquired[key].pop(lease_id, None)
            return RECOVERY_REMOVED

    def verify_concurrency(self, key, lease_id, now_ms):
        """Read-only. Is this lease still recorded, and not past its horizon?

        There is deliberately no ``ttl_ms`` parameter: this cannot extend a
        lease, because extending one would push a slot past the life of the
        process holding it and turn a wedged worker into a permanent leak.
        """
        with self._lock:
            holders = self._leases.get(key, {})
            return holders.get(lease_id, 0) > now_ms

    def release_concurrency(self, key, lease_id):
        with self._lock:
            # Keyed by the unique lease id, so an expired lease's late release
            # can never free a DIFFERENT owner's replacement lease.
            self._acquired.get(key, {}).pop(lease_id, None)
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

    def set_pause(self, key, until_ms, now_ms=0):
        with self._lock:
            self._pauses[key] = max(self._pauses.get(key, 0), until_ms)

    def pause_remaining(self, key, now_ms):
        with self._lock:
            until = self._pauses.get(key, 0)
            return max(0.0, (until - now_ms) / 1000.0)


# --- Upstash Redis backend ---------------------------------------------------

# Each script is ONE atomic Redis execution. Expiries are set on every write so
# an abandoned key cannot leak capacity forever.

# ARGV[3] is the lease's score: '+inf' when nothing may reclaim it, otherwise
# the moment the configured timer allows it back.
#
# ARGV[5] is the KEY's own expiry, and it matters more than it looks. A PEXPIRE
# on a set full of held leases would be an auto-reclaim by the back door: the
# whole key would vanish and every held slot with it. So '0' means PERSIST --
# remove any expiry the key may be carrying from an earlier configuration --
# and the key then disappears only when its last lease is released, which
# Redis does for an empty sorted set on its own.
#
# KEYS[2] is a hash of lease id -> the moment it was acquired. It is written in
# the same atomic step as the lease, carries the same (absent) expiry, and is
# what lets an operator see how old a held slot is -- and what lets a reclaim
# be REFUSED while the process that took the lease could still be alive.
_LUA_ACQUIRE = """
local expired = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[4])
for _, member in ipairs(expired) do redis.call('HDEL', KEYS[2], member) end
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[4])
local active = redis.call('ZCARD', KEYS[1])
local keyttl = tonumber(ARGV[5])
if active > 0 then
  if keyttl > 0 then redis.call('PEXPIRE', KEYS[1], keyttl) redis.call('PEXPIRE', KEYS[2], keyttl)
  else redis.call('PERSIST', KEYS[1]) redis.call('PERSIST', KEYS[2]) end
end
if active >= tonumber(ARGV[2]) then return {0, active} end
redis.call('ZADD', KEYS[1], ARGV[3], ARGV[1])
redis.call('HSET', KEYS[2], ARGV[1], ARGV[4])
if keyttl > 0 then redis.call('PEXPIRE', KEYS[1], keyttl) redis.call('PEXPIRE', KEYS[2], keyttl)
else redis.call('PERSIST', KEYS[1]) redis.call('PERSIST', KEYS[2]) end
return {1, active + 1}
"""

# Read-only by construction: no ZADD, no PEXPIRE, no ZREM. A probe that could
# write could extend a lease, and extending one is the capacity leak this
# design removes.
_LUA_VERIFY = """
local score = redis.call('ZSCORE', KEYS[1], ARGV[1])
if not score or tonumber(score) <= tonumber(ARGV[2]) then return 0 end
return 1
"""

_LUA_RELEASE = """
redis.call('HDEL', KEYS[2], ARGV[1])
return redis.call('ZREM', KEYS[1], ARGV[1])
"""

# Read-only: every held lease with its expiry and the moment it was acquired.
_LUA_HELD = """
local rows = redis.call('ZRANGEBYSCORE', KEYS[1], ARGV[1], '+inf', 'WITHSCORES')
local out = {}
for i = 1, #rows, 2 do
  out[#out + 1] = rows[i]
  out[#out + 1] = rows[i + 1]
  out[#out + 1] = redis.call('HGET', KEYS[2], rows[i]) or '0'
end
return out
"""

# An operator's reclaim. The age check and the removal are ONE atomic
# execution, so an operator can never race a live holder: a lease that is too
# young -- or whose acquisition time is unknown -- is refused in the same step
# that would have removed it.
_LUA_RECOVER = """
if not redis.call('ZSCORE', KEYS[1], ARGV[1]) then
  redis.call('HDEL', KEYS[2], ARGV[1])
  return 0
end
local acquired = redis.call('HGET', KEYS[2], ARGV[1])
if not acquired or tonumber(acquired) > tonumber(ARGV[2]) then return -1 end
redis.call('HDEL', KEYS[2], ARGV[1])
return redis.call('ZREM', KEYS[1], ARGV[1])
"""

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
        # No timed reclaim -> an infinite score and a persisted key, so neither
        # the member nor the key it lives in can expire on its own.
        score = "+inf" if ttl_ms is None else str(now_ms + ttl_ms)
        key_ttl = "0" if ttl_ms is None else str(ttl_ms * 4)
        out = self._eval(_LUA_ACQUIRE, [key, _acquired_key(key)],
                         [lease_id, str(limit), score, str(now_ms), key_ttl])
        return bool(int(out[0])), int(out[1])

    def held_concurrency(self, key, now_ms):
        out = self._eval(_LUA_HELD, [key, _acquired_key(key)], [f"({now_ms}"])
        rows = list(out or [])
        return [(str(rows[i]), float(rows[i + 1]), int(float(rows[i + 2] or 0)))
                for i in range(0, len(rows) - 2, 3)]

    def recover_concurrency(self, key, lease_id, acquired_before_ms):
        return int(self._eval(_LUA_RECOVER, [key, _acquired_key(key)],
                              [lease_id, str(acquired_before_ms)]))

    def verify_concurrency(self, key, lease_id, now_ms):
        return bool(int(self._eval(_LUA_VERIFY, [key], [lease_id, str(now_ms)])))

    def release_concurrency(self, key, lease_id):
        return bool(int(self._eval(_LUA_RELEASE, [key, _acquired_key(key)], [lease_id])))

    def admit_window(self, key, entry_id, limit, window_ms, now_ms, weight):
        out = self._eval(_LUA_ADMIT_WINDOW, [key],
                         [entry_id, str(limit), str(now_ms), str(now_ms - window_ms),
                          str(weight), str(window_ms), str(window_ms * 2)])
        return bool(int(out[0])), int(out[1]), max(0.0, float(out[2]) / 1000.0)

    def admit_interval(self, key, interval_ms, now_ms):
        out = self._eval(_LUA_ADMIT_INTERVAL, [key],
                         [str(now_ms), str(interval_ms), str(interval_ms * 4)])
        return bool(int(out[0])), max(0.0, float(out[1]) / 1000.0)

    def set_pause(self, key, until_ms, now_ms):
        # PX takes a DURATION. Passing the absolute deadline left pause keys
        # resident for ~55,000 years: `pause_remaining` still returned zero, so
        # nothing malfunctioned, but the keys never went away.
        self._eval("redis.call('SET', KEYS[1], ARGV[1], 'PX', ARGV[2]); return 1",
                   [key], [str(until_ms), str(max(1, until_ms - now_ms))])

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
    #: The NOMINAL window one request is expected to occupy. It sizes the
    #: request deadline and the ownership-probe interval. It is NOT a reclaim
    #: trigger: nothing frees a slot at this age.
    lease_ttl_seconds: float = 120.0
    #: The longest ONE provider request may keep a MILO thread waiting. None
    #: means "derive it from the nominal window", which is what every
    #: production path does.
    provider_request_timeout_seconds: float | None = None
    #: The platform-enforced ceiling on how long a process that can hold a
    #: provider socket may live. It bounds MILO, NOT the provider, so it only
    #: floors the opt-in timer below -- it never licenses one.
    worker_max_lifetime_seconds: float = WORKER_MAX_LIFETIME_SECONDS
    #: OPT-IN. ``None`` (the default) means an unreleased lease is never
    #: reclaimed automatically: a slot comes back on a proven release or an
    #: explicit operator reclaim, and on nothing else. Setting it substitutes
    #: an assumption about provider-side behaviour for a proof, and downgrades
    #: :attr:`concurrency_guarantee` accordingly.
    abandoned_lease_reclaim_seconds: float | None = None
    scope: str = "kimi-org"

    @property
    def request_deadline_seconds(self) -> float:
        """The resolved per-request deadline for this configuration."""
        if self.provider_request_timeout_seconds is None:
            return default_request_deadline(self.lease_ttl_seconds)
        return float(self.provider_request_timeout_seconds)

    @property
    def reclaims_abandoned_leases(self) -> bool:
        return self.abandoned_lease_reclaim_seconds is not None

    @property
    def concurrency_guarantee(self) -> str:
        """Which of the two guarantees this configuration actually makes."""
        return (GUARANTEE_TIMED_RECLAIM if self.reclaims_abandoned_leases
                else GUARANTEE_PROVEN_COMPLETION)

    @property
    def minimum_abandoned_lease_reclaim_seconds(self) -> float:
        """The floor a timed reclaim would have to clear, were one enabled."""
        return minimum_abandoned_lease_reclaim(self.worker_max_lifetime_seconds)

    @property
    def ownership_probe_interval_seconds(self) -> float:
        return ownership_probe_interval(self.lease_ttl_seconds)

    @property
    def safety_margin_seconds(self) -> float:
        return lease_safety_margin(self.lease_ttl_seconds)

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
        # The safety invariant, checked wherever a configuration comes from --
        # defaults, environment, or a caller constructing one directly. An
        # unsafe pair cannot be built at all, so no later code has to remember
        # to re-check it before a paid call.
        assert_request_deadline_safe(self.request_deadline_seconds,
                                     self.lease_ttl_seconds)
        # And THE concurrency invariant: an unreleased permit must never be
        # reclaimable while the process that took it could still be alive.
        assert_abandoned_lease_reclaim_safe(self.abandoned_lease_reclaim_seconds,
                                            self.worker_max_lifetime_seconds)

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

        def _abandoned_reclaim() -> float | None:
            # Absent means absent. There is no default timer, because there is
            # no provider-side bound to derive one from; a deployment that
            # sets this is making an assumption of its own and saying so.
            key = "MILO_PROVIDER_ABANDONED_LEASE_RECLAIM_SECONDS"
            if not (source.get(key) or "").strip():
                return None
            return float(_int(key, 0))

        def _lifetime() -> float:
            # A deployment may only ever declare a LONGER process lifetime
            # (which holds slots longer and is therefore safe). Declaring a
            # shorter one asserts processes die sooner than the deployed Cloud
            # Run task timeout guarantees, and would shorten the reclaim
            # horizon below the life of the very process holding the slot.
            value = float(_int("MILO_WORKER_MAX_LIFETIME_SECONDS",
                               int(WORKER_MAX_LIFETIME_SECONDS)))
            if value < WORKER_MAX_LIFETIME_SECONDS:
                raise ValueError(
                    "MILO_WORKER_MAX_LIFETIME_SECONDS is below the deployed "
                    f"Cloud Run task timeout ({WORKER_MAX_LIFETIME_SECONDS:g}s); "
                    "a shorter lifetime would make an in-flight request's "
                    "permit reclaimable while its process is still running")
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
            worker_max_lifetime_seconds=_lifetime(),
            abandoned_lease_reclaim_seconds=_abandoned_reclaim(),
            provider_request_timeout_seconds=(
                float(_int("MILO_PROVIDER_REQUEST_TIMEOUT_SECONDS", 0)) or None
                if (source.get("MILO_PROVIDER_REQUEST_TIMEOUT_SECONDS") or "").strip()
                else None),
            scope=(source.get("MILO_PROVIDER_QUOTA_SCOPE") or "kimi-org").strip() or "kimi-org",
        )


#: A lease is settled exactly once, into one of these.
LEASE_RELEASED = "released"
LEASE_QUARANTINED = "quarantined"


@dataclass(frozen=True)
class HeldLease:
    """One held unit of organization concurrency, as an operator sees it."""

    lease_id: str
    #: ``inf`` unless a deployment opted into a timer.
    expires_at: float
    acquired_at_ms: int
    #: None when the store carries no acquisition time for it.
    age_seconds: float | None
    #: Older than the process-lifetime floor: the process that took it is
    #: provably gone. Says NOTHING about the provider.
    recovery_eligible: bool


@dataclass
class InferenceLease:
    """One held unit of organization inference concurrency.

    Three states, and the middle one is the point of this class:

    ``open``
        acquired; the request may be in flight. The store simply holds it.
    ``released``
        the request is PROVEN over, so the slot went back immediately.
    ``quarantined``
        the request's completion could not be proven. The slot is NOT
        returned -- by default, not until an operator deliberately reclaims
        it (see :meth:`ProviderQuotaCoordinator.operator_reclaim_inference`).

    The id is unique per acquisition, so settling an already-reclaimed lease
    can never free a replacement holder's slot, and a double settle is a no-op
    rather than a silent capacity leak.
    """

    lease_id: str
    coordinator: "ProviderQuotaCoordinator"
    outcome: str | None = None

    @property
    def released(self) -> bool:
        """Kept for callers that only ask 'is this slot back in the pool'."""
        return self.outcome == LEASE_RELEASED

    @property
    def settled(self) -> bool:
        return self.outcome is not None

    def verify_ownership(self) -> bool:
        """Read-only: is this lease still recorded as ours?

        Cannot extend anything. Nothing in the safety argument depends on the
        answer -- it exists so that losing ownership is visible rather than
        silent.
        """
        if self.settled:
            return False
        return self.coordinator.verify_inference_ownership(self.lease_id)

    def release(self) -> bool:
        """Return the slot NOW. Only legitimate for a PROVEN-FINISHED request.

        Callers that cannot prove completion must call :meth:`quarantine`
        instead; :meth:`settle` makes that choice explicit and is what the
        scheduler uses.
        """
        if self.settled:
            return False
        self.outcome = LEASE_RELEASED
        return self.coordinator.release_inference(self.lease_id)

    def quarantine(self, reason: str) -> bool:
        """Keep holding the slot: MILO cannot prove the request is over.

        Note what this does NOT do -- it does not touch the shared store. The
        lease was stamped as held when it was acquired, so holding it is the
        store's existing state and quarantining is simply declining to remove
        it. That is why a process killed mid-request
        cannot fail to quarantine: not acting IS quarantining.
        """
        if self.settled:
            return False
        self.outcome = LEASE_QUARANTINED
        self.coordinator.quarantine_inference(self.lease_id, reason)
        return True

    def settle(self, *, proven_finished: bool, reason: str = "") -> bool:
        """The single exit point. Proof releases; everything else holds."""
        if proven_finished:
            return self.release()
        return self.quarantine(reason or "PROVIDER_REQUEST_COMPLETION_UNPROVEN")


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
    def _abandoned_reclaim_ms(self) -> int | None:
        """How long an UNRELEASED lease is held for, or None for indefinitely.

        None is the default and the whole safety argument: nothing MILO can
        observe proves the provider stopped, so nothing returns the slot on a
        clock. A seam as well, so a regression can model a deployment that
        opted into a timer and show what it costs.
        """
        configured = self.config.abandoned_lease_reclaim_seconds
        return None if configured is None else int(configured * 1000)

    def try_acquire_inference(self) -> InferenceLease | None:
        now = self._now_ms()
        paused = self._backend.pause_remaining(self._key("pause:inference"), now)
        if paused > 0:
            return None
        lease_id = uuid.uuid4().hex
        granted, _active = self._backend.acquire_concurrency(
            # Held, with no expiry unless a deployment opted into one. Holding
            # is the default state of an acquired lease; only a proven-finished
            # release, or a deliberate operator reclaim, returns it.
            self._key("conc"), lease_id, self.config.max_concurrency,
            self._abandoned_reclaim_ms(), now)
        return InferenceLease(lease_id, self) if granted else None

    def verify_inference_ownership(self, lease_id: str) -> bool:
        """Read-only ownership probe. Cannot extend a lease."""
        return self._backend.verify_concurrency(
            self._key("conc"), lease_id, self._now_ms())

    def release_inference(self, lease_id: str) -> bool:
        """Return a slot to the pool. Callers must have PROVEN completion."""
        return self._backend.release_concurrency(self._key("conc"), lease_id)

    def quarantine_inference(self, lease_id: str, reason: str) -> None:
        """Record that a slot is held because completion could not be proven.

        Deliberately does NOT write to the shared store. The lease is already
        held; quarantine is the absence of a release, so there is no store
        round-trip to fail on a failure path (the store may be the very thing
        that broke). What this adds is the diagnostic, so a held slot is never
        mysterious: the reason travels with it, and so does whether anything
        will ever return it without a human.
        """
        configured = self.config.abandoned_lease_reclaim_seconds
        self._emit("provider_lease_quarantined", {
            "reason": str(reason or "")[:64],
            "lease_id": str(lease_id or "")[:64],
            "held_until": ("operator_reclaim" if configured is None
                           else f"{configured:g}s"),
            "guarantee": self.config.concurrency_guarantee,
        })

    def held_inference_leases(self) -> list["HeldLease"]:
        """Every slot currently held, oldest first. Read-only.

        What an operator looks at before deciding whether to reclaim one: an
        ``inf`` expiry is a slot nothing will ever return on its own, and
        ``recovery_eligible`` says whether the process that took it is
        provably gone -- which is necessary for a reclaim and never sufficient.
        """
        now = self._now_ms()
        floor_ms = int(self.config.minimum_abandoned_lease_reclaim_seconds * 1000)
        held = []
        for lease_id, expires_at, acquired_at in self._backend.held_concurrency(
                self._key("conc"), now):
            age = max(0, now - acquired_at) / 1000.0 if acquired_at else None
            held.append(HeldLease(
                lease_id=lease_id, expires_at=expires_at, acquired_at_ms=int(acquired_at),
                age_seconds=None if age is None else round(age, 3),
                recovery_eligible=bool(acquired_at) and (now - acquired_at) >= floor_ms))
        return sorted(held, key=lambda item: (item.acquired_at_ms or 0, item.lease_id))

    def operator_reclaim_inference(self, lease_id: str, *, reason: str) -> bool:
        """Return a held slot because a HUMAN says the request is over.

        The manual half of the fail-closed design, and the only thing besides
        a proven release that frees a slot by default. Calling this is an
        assertion by the operator -- MILO has no way to check it -- so it is
        deliberately separate from :meth:`release_inference`, demands a
        reason, and announces itself.

        It also refuses what MILO CAN check. A lease younger than the
        process-lifetime floor (``worker_max_lifetime + margin``) may belong
        to a process that is still alive and will still settle it on proof;
        reclaiming it would put two real requests under one admission. So
        that floor is a precondition here -- the age check and the removal
        are one atomic store operation -- and never a licence: past it, MILO
        can say the process is gone and can say nothing about the provider,
        which is exactly what the operator's reason records. A lease whose
        acquisition time is unknown is refused too.

        Never called by MILO itself: not by the scheduler, not by acquisition,
        not by a background thread, not by a deployment hook. Its only caller
        is the operator tool ``scripts/release/provider_quota_leases.py``.
        """
        recorded = " ".join(str(reason or "").split())
        recorded = "".join(ch for ch in recorded if ch.isprintable())[:200]
        if not recorded:
            raise LeaseRecoveryRefused(
                "REASON_REQUIRED",
                "an operator reclaim must record why the request is believed "
                "finished; MILO cannot verify it and will not record a blank")
        now = self._now_ms()
        cutoff = now - int(self.config.minimum_abandoned_lease_reclaim_seconds * 1000)
        outcome = self._backend.recover_concurrency(self._key("conc"), str(lease_id), cutoff)
        if outcome == RECOVERY_NOT_HELD:
            raise LeaseRecoveryRefused("LEASE_NOT_HELD", "no lease with that id is held")
        if outcome == RECOVERY_TOO_YOUNG:
            raise LeaseRecoveryRefused(
                "LEASE_TOO_YOUNG",
                "the lease is younger than the process-lifetime floor, or its age "
                "is unknown; the process that took it may still be alive")
        self._emit("provider_lease_operator_reclaimed", {
            "lease_id": str(lease_id or "")[:64],
            "reason": recorded,
            "reclaimed": True,
            "minimum_age_seconds": round(self.config.minimum_abandoned_lease_reclaim_seconds, 3),
        })
        return True

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
        now = self._now_ms()
        self._backend.set_pause(self._key(f"pause:{dimension}"),
                                now + int(seconds * 1000), now)
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
    if source.get("ENVIRONMENT", "local").strip().lower() == "production":
        # In production there is no timer at all. The reviewed model is that
        # unknown occupancy is returned by proof or by a human, never by a
        # clock, because no provider-side bound exists to derive one from --
        # and a single environment variable is not the separate review that
        # trading that guarantee away would require. The worker does not run
        # `validate_production_config`, so this is enforced here, on the path
        # that actually builds the coordinator, however the config was built.
        if resolved.abandoned_lease_reclaim_seconds is not None:
            raise ValueError(
                "MILO_PROVIDER_ABANDONED_LEASE_RECLAIM_SECONDS is forbidden in "
                "production: unknown provider occupancy is returned only by proven "
                "completion or an explicit operator reclaim, never on a timer")
        assert_abandoned_lease_reclaim_safe(
            resolved.abandoned_lease_reclaim_seconds, WORKER_MAX_LIFETIME_SECONDS)
    if url and token:
        backend: QuotaBackend = UpstashQuotaBackend(url, token)
    elif source.get("ENVIRONMENT", "local").strip().lower() == "production":
        raise ProviderQuotaUnavailable(
            "shared provider quota store is mandatory in production")
    else:
        backend = MemoryQuotaBackend()
    return ProviderQuotaCoordinator(backend, resolved, diagnostic_sink=diagnostic_sink)


__all__ = [
    "OWNERSHIP_PROBE_INTERVALS_PER_TTL", "LEASE_SAFETY_MARGIN_RATIO",
    "MIN_LEASE_SAFETY_MARGIN_SECONDS", "WORKER_MAX_LIFETIME_SECONDS",
    "LEASE_QUARANTINED", "LEASE_RELEASED",
    "GUARANTEE_PROVEN_COMPLETION", "GUARANTEE_TIMED_RECLAIM",
    "HeldLease", "LeaseRecoveryRefused",
    "RECOVERY_NOT_HELD", "RECOVERY_REMOVED", "RECOVERY_TOO_YOUNG",
    "assert_abandoned_lease_reclaim_safe", "assert_request_deadline_safe",
    "default_request_deadline", "ownership_probe_interval",
    "minimum_abandoned_lease_reclaim",
    "lease_safety_margin",
    "KIMI_TIER2_PROVIDER_LIMITS", "MAX_INFERENCE_CONCURRENCY", "MAX_RPM", "MAX_TPM",
    "MAX_TPD", "SAFETY_FACTOR", "SEARCH_BASIC", "SEARCH_PRO", "SEARCH_ENDPOINTS",
    "SEARCH_QPS_FALLBACK", "SEARCH_QPS_VERIFIED", "WINDOW_SECONDS",
    "InferenceLease", "MemoryQuotaBackend", "ProviderQuotaCoordinator",
    "ProviderQuotaExhausted", "ProviderQuotaUnavailable", "QuotaBackend",
    "QuotaConfig", "UpstashQuotaBackend", "milo_ceiling", "resolve_coordinator",
]

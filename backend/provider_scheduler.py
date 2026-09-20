"""Shared provider-side scheduling and backpressure handling for paid calls.

Every provider request a Worker makes (initial calls, tool-call rounds,
fallback attempts and summaries) must pass through one shared
:class:`ProviderScheduler` instance, which enforces:

1. a concurrency ceiling (slots);
2. an RPM ceiling (sliding 60s request window);
3. an optional TPM ceiling (sliding 60s estimated-token window);
4. bounded 429/backpressure retries that honor a valid ``Retry-After``
   header and otherwise use exponential backoff with jitter.

Provider backpressure (HTTP 429 / rate-limit responses) is a scheduling
concern, never a semantic model failure: it must not consume the run's
semantic retry allowance (``MILO_MAX_RETRIES``). When backpressure cannot
clear within the configured bound the scheduler raises
:class:`ProviderBackpressureExceeded` so callers report a specific
provider/backpressure reason instead of a false semantic retry exhaustion.

Limits are numeric deployment configuration validated fail-closed: an
invalid value raises ``ValueError`` and the worker refuses to run; it never
degrades into unlimited capacity. Defaults stay deliberately conservative
(the concurrency default matches the preserved engine limit and the RPM
default matches the strictest limit observed in production); higher tiers
must be configured explicitly per deployment.
"""

from __future__ import annotations

import os
import random
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

from backend.runtime import CancellationRequested


class ProviderBackpressureExceeded(Exception):
    """Provider backpressure did not clear within the configured bound."""

    def __init__(self, message: str, *, attempts: int = 0, waited_seconds: float = 0.0, last_error: Exception | None = None):
        super().__init__(message)
        self.attempts = attempts
        self.waited_seconds = waited_seconds
        self.last_error = last_error


def is_provider_rate_limit_error(exc: Any) -> bool:
    """Classify provider rate-limit/backpressure signals (never semantic)."""
    if isinstance(exc, ProviderBackpressureExceeded):
        return True
    text = str(exc or "").lower()
    status_code = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    return (
        status_code == 429
        or "http 429" in text
        or "error code: 429" in text
        or "max organization concurrency" in text
        or "organization max rpm" in text
        or "rate_limit_reached_error" in text
    )


# The distinct Kimi 429/5xx classes, which must NOT be treated alike.
RATE_LIMIT_REACHED = "rate_limit_reached_error"
ENGINE_OVERLOADED = "engine_overloaded_error"
EXCEEDED_CURRENT_QUOTA = "exceeded_current_quota_error"
SEARCH_RATE_LIMITED = "rate_limited"
SEARCH_RATE_LIMIT_UNAVAILABLE = "rate_limit_unavailable"


class ProviderQuotaExceeded(Exception):
    """``exceeded_current_quota_error``: the account is out of quota.

    This is NOT transient rate limiting and must never be retried as if it
    were: retrying cannot make quota appear, and a retry loop here only burns
    run duration before the same refusal.
    """


def classify_provider_error(exc: Any) -> str | None:
    """Name WHICH Kimi failure class an exception is, or None.

    The classes demand different responses -- reconcile the limiter, back off,
    or fail closed -- so collapsing them into one "rate limited" boolean is how
    a hard quota exhaustion turns into a retry storm.
    """
    text = str(exc or "").lower()
    status = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None)
    for marker in (EXCEEDED_CURRENT_QUOTA, ENGINE_OVERLOADED, RATE_LIMIT_REACHED,
                   SEARCH_RATE_LIMIT_UNAVAILABLE):
        if marker in text:
            return marker
    if "project qps limit exceeded" in text:
        return SEARCH_RATE_LIMITED
    if status == 429 or "http 429" in text or "error code: 429" in text:
        return RATE_LIMIT_REACHED
    if status == 503 or "overloaded" in text:
        return ENGINE_OVERLOADED
    if "max organization concurrency" in text or "organization max rpm" in text:
        return RATE_LIMIT_REACHED
    return None


#: httpx failures that happen BEFORE a request is on the wire. For these,
#: and only these, "it failed" really does prove "it is not running".
_NEVER_SENT = ("ConnectError", "ConnectTimeout", "PoolTimeout",
               "UnsupportedProtocol", "InvalidURL", "ProxyError")


def _cause_chain(exc: BaseException):
    """Walk ``__cause__``/``__context__`` once each: the SDK wraps transport errors."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _failed_before_the_request_was_sent(exc: BaseException) -> bool:
    return any(type(link).__name__ in _NEVER_SENT for link in _cause_chain(exc))


def _fired_a_total_deadline(exc: BaseException) -> bool:
    """The transport's deadline, whether raised bare or wrapped by the SDK."""
    return any(type(link).__name__ == "ProviderRequestDeadlineExceeded"
               for link in _cause_chain(exc))


def _carries_a_complete_provider_response(exc: BaseException) -> bool:
    """STRUCTURAL evidence that the provider answered.

    True only for an exception that carries a response OBJECT with an integer
    HTTP status code. That is the shape of the OpenAI SDK's ``APIStatusError``
    family, which the SDK raises only after ``response.read()`` -- so the
    exchange is over whatever the status says.

    A bare ``status_code`` attribute with no response object is deliberately
    NOT enough: MILO's own ``AppError`` carries one (an HTTP status for MILO's
    API, not the provider's), and any wrapper can set one. An attribute is a
    claim; a response is evidence.
    """
    response = getattr(exc, "response", None)
    if response is None:
        return False
    status = getattr(response, "status_code", None)
    if isinstance(status, bool) or not isinstance(status, int):
        return False
    return 100 <= status <= 599


def request_completion_is_proven(exc: BaseException | None) -> tuple[bool, str]:
    """Can MILO PROVE the request that held a permit is no longer running?

    This is the question the organization concurrency ceiling actually turns
    on, and it is not the same as "did MILO stop waiting". A permit may be
    returned to the shared pool only on a YES, so the default here is NO: an
    outcome this function does not recognise holds the slot rather than
    freeing it.

    Every YES is STRUCTURAL -- an object the transport or the SDK built, or a
    statement from code that ran on one side of the request. None of them is
    the TEXT of a message:

    * the call RETURNED. The response was read to completion, so the exchange
      is over.
    * the exception carries a provider response OBJECT with a status code.
      The provider produced a complete HTTP response -- 400, 429, 500 alike --
      so the exchange is over whatever the status says. A bare
      ``status_code`` attribute with no response object is not this. (This is what keeps ordinary
      backpressure fast: a real 429 releases immediately and the retry
      proceeds.)
    * the failure happened before anything was sent -- a connect timeout, a
      refused connection, an unusable URL. Nothing was ever started.
    * the exception carries ``provider_request_completed``, set by code that
      knows which side of the request it ran on (``backend.budget``).

    Everything else is NO. Most importantly
    :class:`~backend.provider_transport.ProviderRequestDeadlineExceeded` and
    read timeouts: those mean MILO stopped waiting, and nothing in the httpx
    or OpenAI contract turns that into the provider stopping work.

    WHY :func:`classify_provider_error` IS NOT CONSULTED HERE
    ---------------------------------------------------------

    A previous revision released the slot whenever that classifier recognised
    the exception, and review rightly called it out. The classifier matches
    raw message TEXT -- ``rate_limit_reached_error``, ``error code: 429``,
    ``overloaded`` -- deliberately, because for RETRY decisions a permissive
    reading is the safe direction: treating something as backpressure only
    costs a wait. For CONCURRENCY it is the opposite. Text is not evidence
    that a request reached the provider, let alone that it finished, so any
    exception whose message happened to contain "429" could free an
    organization slot. The two questions want opposite defaults, so they are
    answered by different code: the classifier stays permissive for retries,
    and settlement requires structure.
    """
    if exc is None:
        return True, ""
    # An explicit statement from code that KNOWS, because it sits on one side
    # of the request or the other: `backend.budget` marks a budget refusal
    # raised before the request was sent, or after the response was read.
    declared = getattr(exc, "provider_request_completed", None)
    if declared is not None:
        return bool(declared), "" if declared else "PROVIDER_REQUEST_OUTCOME_DECLARED_UNKNOWN"
    # Checked BEFORE the response test: the deadline exception never carries
    # a response, but the order makes the intent explicit -- a fired deadline
    # can never be talked into looking like an answer, however it is wrapped.
    if _fired_a_total_deadline(exc):
        return False, "PROVIDER_REQUEST_DEADLINE_EXCEEDED"
    if _carries_a_complete_provider_response(exc):
        return True, ""
    if _failed_before_the_request_was_sent(exc):
        return True, ""
    return False, "PROVIDER_REQUEST_OUTCOME_UNKNOWN"


def rate_limit_headers(exc: Any) -> dict[str, int]:
    """Read the numeric X-RateLimit-* values a 429 may publish.

    These are used to slow MILO down and to explain a refusal. They are NEVER
    used to widen a ceiling: a header advertising more capacity than the
    reviewed 80% configuration is ignored, because a capacity increase needs
    authoritative verification and a reviewed change, not a response header.
    """
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is None:
        return {}
    out: dict[str, int] = {}
    for name, key in (("X-RateLimit-Limit", "limit"),
                      ("X-RateLimit-Remaining", "remaining"),
                      ("X-RateLimit-Reset", "reset")):
        try:
            raw = headers.get(name)
            if raw is None:
                raw = headers.get(name.lower())
            if raw is None:
                continue
            out[key] = int(float(str(raw).strip()))
        except Exception:  # noqa: BLE001 - unknown header containers fail closed
            continue
    return out


def retry_after_seconds(exc: Any) -> float | None:
    """Extract a valid Retry-After value (seconds) from a provider error."""
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is None:
        return None
    try:
        raw = headers.get("Retry-After")
        if raw is None:
            raw = headers.get("retry-after")
    except Exception:  # noqa: BLE001 - unknown header container shapes fail closed to backoff
        return None
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    return value


# Conservative chars-per-token heuristic mirroring backend.budget.
_CHARS_PER_TOKEN = 4


def estimate_input_tokens(messages: Any) -> int:
    """Estimate ONLY the request/input side of a provider request."""
    try:
        total_chars = sum(len(str(m.get("content", ""))) if isinstance(m, dict) else len(str(m)) for m in (messages or []))
    except TypeError:
        total_chars = len(str(messages))
    return max(1, total_chars // _CHARS_PER_TOKEN)


def estimate_request_tokens(messages: Any, max_tokens: Any = None) -> int:
    """Estimate a request's total token footprint for TPM pacing."""
    return estimate_input_tokens(messages) + max(0, int(max_tokens or 0))


class MissingOutputCap(ValueError):
    """A provider request reached admission without an explicit output cap."""


def estimate_admission_tokens(messages: Any, max_completion_tokens: Any) -> int:
    """The EXACT value Kimi admits a request against, computed before the call.

    Kimi's rate limiter admits on request tokens PLUS ``max_completion_tokens``
    -- it does not wait to see how much output is actually generated. So the
    admission value is the input estimate plus the *requested cap*, and a
    caller that omitted a cap cannot be admitted at all: charging such a
    request as "input only" would systematically under-count the organization
    TPM window and is exactly how a shared ceiling gets breached.
    """
    if max_completion_tokens is None:
        raise MissingOutputCap(
            "provider admission requires an explicit max_completion_tokens")
    try:
        cap = int(max_completion_tokens)
    except (TypeError, ValueError):
        raise MissingOutputCap("max_completion_tokens must be an integer") from None
    if cap <= 0:
        raise MissingOutputCap("max_completion_tokens must be positive")
    return estimate_input_tokens(messages) + cap


def _positive_int(env: dict[str, str], key: str, default: int | None) -> int | None:
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{key} must be an integer, got {raw!r}") from None
    if value <= 0:
        raise ValueError(f"{key} must be positive, got {value}")
    return value


def _positive_float(env: dict[str, str], key: str, default: float | None) -> float | None:
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{key} must be a number, got {raw!r}") from None
    if value <= 0:
        raise ValueError(f"{key} must be positive, got {value}")
    return value


@dataclass(frozen=True)
class ProviderLimitsConfig:
    """Numeric provider ceilings/operating targets. Validated fail-closed."""

    # Matches the preserved engine limit (core.MAX_PARALLEL_KIMI_CALLS).
    max_concurrency: int = 2
    # The strictest limit observed in production (Attempt 6 saw
    # "organization max RPM: 3"); a higher provider tier must be configured
    # explicitly, never assumed.
    rpm_limit: int | None = 3
    tpm_limit: int | None = None
    max_rate_limit_retries: int = 5
    max_backpressure_wait_seconds: float = 240.0
    backoff_base_seconds: float = 2.0
    backoff_max_seconds: float = 30.0

    ENV_KEYS = {
        "max_concurrency": "MILO_PROVIDER_MAX_CONCURRENCY",
        "rpm_limit": "MILO_PROVIDER_RPM_LIMIT",
        "tpm_limit": "MILO_PROVIDER_TPM_LIMIT",
        "max_rate_limit_retries": "MILO_PROVIDER_MAX_RATE_LIMIT_RETRIES",
        "max_backpressure_wait_seconds": "MILO_PROVIDER_MAX_BACKPRESSURE_WAIT_SECONDS",
        "backoff_base_seconds": "MILO_PROVIDER_BACKOFF_BASE_SECONDS",
        "backoff_max_seconds": "MILO_PROVIDER_BACKOFF_MAX_SECONDS",
    }

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "ProviderLimitsConfig":
        source = dict(os.environ if env is None else env)
        defaults = cls()
        resolved = cls(
            max_concurrency=_positive_int(source, cls.ENV_KEYS["max_concurrency"], defaults.max_concurrency),
            rpm_limit=_positive_int(source, cls.ENV_KEYS["rpm_limit"], defaults.rpm_limit),
            tpm_limit=_positive_int(source, cls.ENV_KEYS["tpm_limit"], defaults.tpm_limit),
            max_rate_limit_retries=_positive_int(source, cls.ENV_KEYS["max_rate_limit_retries"], defaults.max_rate_limit_retries),
            max_backpressure_wait_seconds=_positive_float(source, cls.ENV_KEYS["max_backpressure_wait_seconds"], defaults.max_backpressure_wait_seconds),
            backoff_base_seconds=_positive_float(source, cls.ENV_KEYS["backoff_base_seconds"], defaults.backoff_base_seconds),
            backoff_max_seconds=_positive_float(source, cls.ENV_KEYS["backoff_max_seconds"], defaults.backoff_max_seconds),
        )
        resolved.assert_within_organization_ceiling()
        return resolved

    def assert_within_organization_ceiling(self) -> None:
        """Refuse a per-process profile that claims more than MILO's whole share.

        A single process configured above the organization ceiling is a
        misconfiguration whichever way it is read: either it believes it owns
        the account alone, or the ceiling moved without review. Both are
        refusals, never the larger number. (Production carried
        ``MILO_PROVIDER_RPM_LIMIT=350`` against an 80 RPM ceiling; this is the
        check that would have caught it.)
        """
        from backend.provider_quota import (MAX_INFERENCE_CONCURRENCY, MAX_RPM,
                                            MAX_TPM)

        for name, value, ceiling in (
            (self.ENV_KEYS["max_concurrency"], self.max_concurrency, MAX_INFERENCE_CONCURRENCY),
            (self.ENV_KEYS["rpm_limit"], self.rpm_limit, MAX_RPM),
            (self.ENV_KEYS["tpm_limit"], self.tpm_limit, MAX_TPM),
        ):
            if value is not None and ceiling is not None and value > ceiling:
                raise ValueError(
                    f"{name}={value} exceeds the organization ceiling {ceiling} "
                    "derived from verified Kimi Tier 2 limits at 80%")


BackpressureCallback = Callable[[str, str, str, float], None]

_WINDOW_SECONDS = 60.0
_WAIT_CHUNK_SECONDS = 1.0
# Cooperative concurrency-slot polling interval: short enough to notice a
# freed slot or a cancellation promptly, long enough to avoid busy polling.
_SLOT_POLL_SECONDS = 0.05


class _OwnershipProbe:
    """Handle for one request's read-only ownership probe.

    Renamed from a renewal watchdog because it no longer renews anything. A
    held lease has no expiry to renew, and under the opt-in reclaim timer
    re-stamping would push a slot past the life of the process holding it.
    All this does is notice, and say, when MILO can no longer show it owns a
    permit it is using.
    """

    def __init__(self) -> None:
        self.done = threading.Event()
        self.lost_reason: str | None = None
        self.thread: threading.Thread | None = None
        self.on_lost: Callable[[str], None] | None = None

    @property
    def ownership_proven(self) -> bool:
        """False once MILO can no longer show it holds this permit.

        Nothing in the concurrency invariant reads this. It is observability:
        the safety argument holds if this probe never runs, fails every time,
        or never manages to start its thread.
        """
        return self.lost_reason is None

    def mark_lost(self, reason: str) -> None:
        # The reason is recorded BEFORE anyone is told, and telling anyone can
        # never undo it: a reporting sink that raises must not turn "we lost
        # the permit" into an unhandled traceback from a daemon thread, which
        # is the same invisibility this whole handle exists to remove.
        if self.lost_reason is not None:
            return
        self.lost_reason = reason
        if self.on_lost is None:
            return
        try:
            self.on_lost(reason)
        except BaseException:  # noqa: BLE001 - a failed report is not a failure
            pass

    def stop(self) -> None:
        self.done.set()


class ProviderScheduler:
    """One shared scheduler guards every provider request in a Worker run."""

    def __init__(
        self,
        config: ProviderLimitsConfig,
        *,
        sleep_fn: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
        rng: Callable[[], float] | None = None,
        cancellation_checker: Callable[[], bool] | None = None,
        backpressure_callback: BackpressureCallback | None = None,
        coordinator: Any | None = None,
    ):
        self.config = config
        self._sleep = sleep_fn or time.sleep
        self._clock = clock or time.monotonic
        self._rng = rng or random.random
        self._cancellation_checker = cancellation_checker
        self._backpressure_callback = backpressure_callback
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(config.max_concurrency)
        self._request_starts: deque[float] = deque()
        self._token_starts: deque[tuple[float, int]] = deque()
        # The ORGANIZATION-wide gate. The process-local structures above stay as
        # a second, tighter guard (a process may be configured below its share),
        # but they are not the authority: only this coordinator can see the
        # other Cloud Run executions, worker processes and engines drawing on
        # the same Kimi account quota. Production wiring always supplies one.
        self._coordinator = coordinator

    # -- cooperative interruption --------------------------------------------
    def _check_cancelled(self) -> None:
        if self._cancellation_checker and self._cancellation_checker():
            raise CancellationRequested("RUN_CANCELLED")

    def _wait(self, seconds: float) -> None:
        remaining = float(seconds)
        while remaining > 0:
            self._check_cancelled()
            step = min(remaining, _WAIT_CHUNK_SECONDS)
            self._sleep(step)
            remaining -= step
        self._check_cancelled()

    # -- capacity accounting --------------------------------------------------
    def _prune(self, now: float) -> None:
        while self._request_starts and now - self._request_starts[0] >= _WINDOW_SECONDS:
            self._request_starts.popleft()
        while self._token_starts and now - self._token_starts[0][0] >= _WINDOW_SECONDS:
            self._token_starts.popleft()

    def _try_admit(self, estimated_tokens: int) -> float:
        """Admit the request (returning 0) or return the seconds to wait."""
        cfg = self.config
        with self._lock:
            now = self._clock()
            self._prune(now)
            delay = 0.0
            if cfg.rpm_limit is not None and len(self._request_starts) >= cfg.rpm_limit:
                delay = max(delay, _WINDOW_SECONDS - (now - self._request_starts[0]))
            if cfg.tpm_limit is not None and estimated_tokens > 0 and self._token_starts:
                used = sum(tokens for _, tokens in self._token_starts)
                if used + estimated_tokens > cfg.tpm_limit:
                    delay = max(delay, _WINDOW_SECONDS - (now - self._token_starts[0][0]))
            if delay > 0:
                return max(delay, 0.001)
            self._request_starts.append(now)
            if estimated_tokens > 0:
                self._token_starts.append((now, estimated_tokens))
            return 0.0

    def _admit(self, estimated_tokens: int, waited_total: float, agent: str, phase: str) -> float:
        """Wait for RPM/TPM capacity; return seconds waited. Bounded."""
        cfg = self.config
        if cfg.tpm_limit is not None and estimated_tokens > cfg.tpm_limit:
            raise ProviderBackpressureExceeded(
                f"estimated request tokens {estimated_tokens} exceed the configured TPM limit {cfg.tpm_limit}",
                waited_seconds=waited_total,
            )
        waited = 0.0
        while True:
            self._check_cancelled()
            delay = self._try_admit(estimated_tokens)
            if delay == 0.0:
                return waited
            if waited_total + waited + delay > cfg.max_backpressure_wait_seconds:
                raise ProviderBackpressureExceeded(
                    "provider capacity did not clear within the configured backpressure bound",
                    waited_seconds=round(waited_total + waited, 3),
                )
            if self._backpressure_callback:
                self._backpressure_callback(agent, phase, "provider_capacity_wait", delay)
            self._wait(delay)
            waited += delay

    def _acquire_slot(self, waited_total: float, agent: str, phase: str) -> float:
        """Acquire a concurrency slot cooperatively; return seconds waited.

        Bounded and cancellable like every other scheduler wait: slot
        waiting counts toward max_backpressure_wait_seconds and raises
        ProviderBackpressureExceeded when the bound is exhausted. On any
        raise no slot is held, so callers release only after success."""
        if self._slots.acquire(blocking=False):
            return 0.0
        cfg = self.config
        waited = 0.0
        notified = False
        while True:
            self._check_cancelled()
            if waited_total + waited + _SLOT_POLL_SECONDS > cfg.max_backpressure_wait_seconds:
                raise ProviderBackpressureExceeded(
                    "provider concurrency capacity did not clear within the configured backpressure bound",
                    waited_seconds=round(waited_total + waited, 3),
                )
            if not notified and self._backpressure_callback:
                self._backpressure_callback(agent, phase, "provider_concurrency_slot_wait", _SLOT_POLL_SECONDS)
                notified = True
            self._sleep(_SLOT_POLL_SECONDS)
            waited += _SLOT_POLL_SECONDS
            self._check_cancelled()
            if self._slots.acquire(blocking=False):
                return waited

    # -- organization-wide admission ------------------------------------------
    def _acquire_global(self, reserved_tokens: int, waited_total: float,
                        agent: str, phase: str) -> tuple[Any, float]:
        """Hold ONE organization concurrency lease with RPM+TPM admitted.

        Ordering is deliberate: take the concurrency lease first, then offer the
        request to the RPM and TPM windows. When a window refuses, the lease is
        released before waiting so a blocked caller never parks organization
        concurrency it is not using -- that is how a few waiting callers would
        otherwise deadlock every other engine out of the account.

        Both releases below are PRE-REQUEST: the lease is given up before
        anything is sent, so completion is proven and returning the slot at
        once is correct. Settlement on proof (``lease.settle``) governs only
        the path where a request has actually been issued.

        Bounded and cancellable like every other wait here.
        """
        cfg = self.config
        waited = 0.0
        while True:
            self._check_cancelled()
            lease = self._coordinator.try_acquire_inference()
            if lease is not None:
                try:
                    admitted, dimension, retry = self._coordinator.try_admit_request(
                        reserved_tokens)
                except Exception:
                    lease.release()
                    raise
                if admitted:
                    return lease, waited
                lease.release()
                delay = max(retry, _SLOT_POLL_SECONDS)
                reason = f"provider_{dimension}_wait"
            else:
                delay = _SLOT_POLL_SECONDS
                reason = "provider_concurrency_slot_wait"
            if waited_total + waited + delay > cfg.max_backpressure_wait_seconds:
                raise ProviderBackpressureExceeded(
                    "organization provider capacity did not clear within the "
                    "configured backpressure bound",
                    waited_seconds=round(waited_total + waited, 3))
            if self._backpressure_callback:
                self._backpressure_callback(agent, phase, reason, delay)
            self._wait(delay)
            waited += delay

    def _start_ownership_probe(self, lease: Any, agent: str,
                               phase: str) -> "_OwnershipProbe | None":
        """Watch whether ``lease`` is still recorded as ours, and say if not.

        PURELY OBSERVABILITY. It renews nothing, and the concurrency invariant
        does not reference it: a lease is HELD from the moment it is acquired,
        and only a PROVEN-FINISHED request -- or a deliberate operator reclaim
        -- returns it. So this may never run, fail on every pass, or fail to
        start its thread, and the ceiling still holds.

        What it buys is that losing ownership is not silent. It used to be
        silent twice over: a ``False`` return ended the loop with no record,
        and an exception from the shared store killed the daemon thread
        outright. Both now stop the loop deliberately, mark ownership as
        unproven and emit a bounded diagnostic.
        """
        if lease is None:
            return None
        config = getattr(getattr(lease, "coordinator", None), "config", None)
        interval = getattr(config, "ownership_probe_interval_seconds", None)
        if not interval:
            interval = max(1.0, getattr(config, "lease_ttl_seconds", 120.0) / 4.0)
        probe = _OwnershipProbe()

        def watch() -> None:
            while not probe.done.wait(interval):
                try:
                    still_ours = lease.verify_ownership()
                except BaseException:  # noqa: BLE001 - reported, never re-raised
                    # The shared store is unreachable, or refused. Re-raising
                    # here would only kill this daemon thread, which is what
                    # used to make the loss invisible.
                    probe.mark_lost("PROVIDER_LEASE_PROBE_FAILED")
                    return
                if not still_ours:
                    # The coordinator no longer records this permit as ours.
                    # Re-acquiring here would quietly take a SECOND permit for
                    # one request, so the loop ends and the loss is recorded.
                    probe.mark_lost("PROVIDER_LEASE_OWNERSHIP_LOST")
                    return

        probe.on_lost = lambda reason: self._report_lease_loss(reason, agent, phase)
        probe.thread = threading.Thread(target=watch, name="provider-lease-probe",
                                        daemon=True)
        probe.thread.start()
        return probe

    def _report_lease_loss(self, reason: str, agent: str, phase: str) -> None:
        """Announce lost permit ownership with a static code and nothing else.

        No URL, no credential, no provider body and no exception text: the
        reason is one of two constants, and agent/phase are server-chosen
        identifiers. A diagnostic about losing the limiter must not become the
        thing that leaks what the limiter is talking to.
        """
        if self._backpressure_callback:
            self._backpressure_callback(agent, phase, reason.lower(), 0.0)
        coordinator = self._coordinator
        emit = getattr(coordinator, "_emit", None) if coordinator is not None else None
        if callable(emit):
            emit("provider_lease_ownership_lost",
                 {"reason": reason, "agent": str(agent or "")[:64],
                  "phase": str(phase or "")[:64]})

    # -- the guarded call path -------------------------------------------------
    def execute(self, call: Callable[[], Any], *, estimated_tokens: int = 0, agent: str = "", phase: str = "",
                reserved_tokens: int | None = None) -> Any:
        """Run one provider request under capacity scheduling and bounded
        backpressure retries. Non-rate-limit exceptions propagate unchanged.

        ``reserved_tokens`` is the value the ORGANIZATION admits against
        (input estimate + explicit ``max_completion_tokens``). It defaults to
        ``estimated_tokens`` so existing callers keep working, but the guarded
        model client always computes it strictly.
        """
        cfg = self.config
        attempts = 0
        waited_total = 0.0
        admission_tokens = reserved_tokens if reserved_tokens is not None else estimated_tokens
        while True:
            waited_total += self._admit(estimated_tokens, waited_total, agent, phase)
            self._check_cancelled()
            # ORDER MATTERS. The process-local slot is taken FIRST and the
            # shared organization permit second, so the permit is acquired
            # immediately before the request goes out.
            #
            # The other way round, a caller could hold an account-wide permit
            # while queueing behind its own process -- starving every other
            # process of capacity it was not using, and stretching the window
            # the permit has to stay valid for by up to the whole backpressure
            # bound. The safety invariant is stated over "permit granted ->
            # request finished", so that window is exactly what must stay
            # small; local queueing is not allowed inside it.
            waited_total += self._acquire_slot(waited_total, agent, phase)
            lease = None
            probe = None
            # Default to NOT proven. Every path out of the call below either
            # establishes proof or leaves this alone, so an outcome nobody
            # thought about holds the slot instead of freeing it.
            proven_finished = False
            settle_reason = "PROVIDER_REQUEST_OUTCOME_UNKNOWN"
            try:
                if self._coordinator is not None:
                    lease, waited = self._acquire_global(
                        max(1, int(admission_tokens)), waited_total, agent, phase)
                    waited_total += waited
                # Observability only -- it renews nothing (see
                # _start_ownership_probe). Still started inside the same guard
                # as the acquisition: a thread spawn can fail, and a permit
                # stranded because of that would be a capacity leak by a
                # different route.
                probe = self._start_ownership_probe(lease, agent, phase)
            except BaseException:
                # Nothing was sent: acquiring a permit is the step before the
                # request, so completion IS proven here, and releasing is
                # correct rather than merely convenient.
                if lease is not None:
                    lease.release()
                self._slots.release()
                raise
            try:
                result = call()
            except BaseException as exc:  # noqa: BLE001 - classified below; others re-raise
                proven_finished, settle_reason = request_completion_is_proven(exc)
                if not isinstance(exc, Exception):
                    # KeyboardInterrupt / SystemExit: not ours to classify, and
                    # the settlement above has already been decided.
                    raise
                kind = classify_provider_error(exc)
                if kind == EXCEEDED_CURRENT_QUOTA:
                    # Not transient. Retrying cannot create quota, so this fails
                    # closed for the call instead of burning the retry budget.
                    raise ProviderQuotaExceeded(
                        "provider reported the current quota is exhausted") from exc
                if kind is None:
                    raise
                attempts += 1
                delay = retry_after_seconds(exc)
                if self._coordinator is not None:
                    headers = rate_limit_headers(exc)
                    # Reconcile the shared limiter and, when MILO believed it had
                    # headroom, surface the drift rather than hiding it: Kimi
                    # quota is shared across the organization and another
                    # application on the same account is invisible to us.
                    self._coordinator.record_rate_limit_signal(
                        dimension="inference" if kind != SEARCH_RATE_LIMITED else "search",
                        retry_after=delay if delay is not None else headers.get("reset"),
                        reported_limit=headers.get("limit"),
                        reported_remaining=headers.get("remaining"),
                        local_headroom=cfg.max_concurrency)
                if delay is None:
                    base = min(cfg.backoff_max_seconds, cfg.backoff_base_seconds * (2 ** (attempts - 1)))
                    delay = base * (1.0 + 0.25 * self._rng())
                if attempts > cfg.max_rate_limit_retries or waited_total + delay > cfg.max_backpressure_wait_seconds:
                    raise ProviderBackpressureExceeded(
                        f"provider backpressure did not clear after {attempts} rate-limited attempt(s)",
                        attempts=attempts,
                        waited_seconds=round(waited_total, 3),
                        last_error=exc,
                    ) from exc
            else:
                proven_finished, settle_reason = True, ""
                return result
            finally:
                if probe is not None:
                    probe.stop()
                self._slots.release()
                # Deterministic SETTLEMENT on every path -- success, provider
                # failure, backpressure exhaustion, quota refusal, cancellation
                # -- keyed by this acquisition's unique lease id, so it can
                # never free a replacement holder's slot.
                #
                # Settlement is not release. The shared permit goes back only
                # when this request is PROVEN over; when it is not, the slot
                # stays held until a human returns it. The local slot
                # above is a different thing and is always released: it bounds
                # this process's own threads, not organization concurrency.
                if lease is not None:
                    lease.settle(proven_finished=proven_finished,
                                 reason=settle_reason)
            # Only reached when a rate-limited attempt will be retried: wait
            # (Retry-After or backoff+jitter) and re-enter capacity scheduling.
            # The retry re-enters _acquire_global, so EVERY provider attempt is
            # admitted against the organization ceiling, not just the first.
            if self._backpressure_callback:
                self._backpressure_callback(agent, phase, "provider_rate_limited", delay)
            self._wait(delay)
            waited_total += delay

    # -- Web Search admission --------------------------------------------------
    def admit_search(self, endpoint: str, *, agent: str = "", phase: str = "",
                     max_wait_seconds: float | None = None) -> float:
        """Block until ONE Web Search request may be sent on ``endpoint``.

        Search QPS is a separate provider bucket per endpoint: it consumes no
        chat RPM/TPM/concurrency and chat consumes none of it. Basic and Pro are
        counted independently, and each bucket is shared by V1 and V2.
        """
        if self._coordinator is None:
            return 0.0
        bound = (self.config.max_backpressure_wait_seconds
                 if max_wait_seconds is None else max_wait_seconds)
        waited = 0.0
        while True:
            self._check_cancelled()
            admitted, retry = self._coordinator.try_admit_search(endpoint)
            if admitted:
                return waited
            delay = max(retry, _SLOT_POLL_SECONDS)
            if waited + delay > bound:
                raise ProviderBackpressureExceeded(
                    f"web search ({endpoint}) capacity did not clear within the bound",
                    waited_seconds=round(waited, 3))
            if self._backpressure_callback:
                self._backpressure_callback(agent, phase, f"search_{endpoint}_qps_wait", delay)
            self._wait(delay)
            waited += delay

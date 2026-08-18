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


def estimate_request_tokens(messages: Any, max_tokens: Any = None) -> int:
    """Estimate a request's total token footprint for TPM pacing."""
    try:
        total_chars = sum(len(str(m.get("content", ""))) if isinstance(m, dict) else len(str(m)) for m in (messages or []))
    except TypeError:
        total_chars = len(str(messages))
    estimated_output = int(max_tokens or 0)
    return max(1, total_chars // _CHARS_PER_TOKEN) + max(0, estimated_output)


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
        return cls(
            max_concurrency=_positive_int(source, cls.ENV_KEYS["max_concurrency"], defaults.max_concurrency),
            rpm_limit=_positive_int(source, cls.ENV_KEYS["rpm_limit"], defaults.rpm_limit),
            tpm_limit=_positive_int(source, cls.ENV_KEYS["tpm_limit"], defaults.tpm_limit),
            max_rate_limit_retries=_positive_int(source, cls.ENV_KEYS["max_rate_limit_retries"], defaults.max_rate_limit_retries),
            max_backpressure_wait_seconds=_positive_float(source, cls.ENV_KEYS["max_backpressure_wait_seconds"], defaults.max_backpressure_wait_seconds),
            backoff_base_seconds=_positive_float(source, cls.ENV_KEYS["backoff_base_seconds"], defaults.backoff_base_seconds),
            backoff_max_seconds=_positive_float(source, cls.ENV_KEYS["backoff_max_seconds"], defaults.backoff_max_seconds),
        )


BackpressureCallback = Callable[[str, str, str, float], None]

_WINDOW_SECONDS = 60.0
_WAIT_CHUNK_SECONDS = 1.0
# Cooperative concurrency-slot polling interval: short enough to notice a
# freed slot or a cancellation promptly, long enough to avoid busy polling.
_SLOT_POLL_SECONDS = 0.05


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

    # -- the guarded call path -------------------------------------------------
    def execute(self, call: Callable[[], Any], *, estimated_tokens: int = 0, agent: str = "", phase: str = "") -> Any:
        """Run one provider request under capacity scheduling and bounded
        backpressure retries. Non-rate-limit exceptions propagate unchanged."""
        cfg = self.config
        attempts = 0
        waited_total = 0.0
        while True:
            waited_total += self._admit(estimated_tokens, waited_total, agent, phase)
            self._check_cancelled()
            waited_total += self._acquire_slot(waited_total, agent, phase)
            try:
                return call()
            except Exception as exc:  # noqa: BLE001 - classified below; others re-raise
                if not is_provider_rate_limit_error(exc):
                    raise
                attempts += 1
                delay = retry_after_seconds(exc)
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
            finally:
                self._slots.release()
            # Only reached when a rate-limited attempt will be retried: wait
            # (Retry-After or backoff+jitter) and re-enter capacity scheduling.
            if self._backpressure_callback:
                self._backpressure_callback(agent, phase, "provider_rate_limited", delay)
            self._wait(delay)
            waited_total += delay

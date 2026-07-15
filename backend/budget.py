"""Hard server-side budget, cost and resource limits for paid execution.

Every paid model call must pass through :class:`BudgetTracker.before_call`,
which checks, in order:

1. the global paid-execution kill switch (``MILO_ENABLE_PAID_EXECUTION``,
   default OFF — no paid call can ever happen while it is off);
2. cooperative cancellation;
3. elapsed run duration;
4. model-call count;
5. token limits (input / output / total);
6. estimated remaining cost budget;
7. optional daily user / project budgets (via injected providers).

After every call :class:`BudgetTracker.after_call` records input tokens,
output tokens and estimated cost, persists the aggregate through the
injected recorder, and emits ``budget_warning`` events at 80% of any hard
limit. Once a hard limit trips, ``before_call`` keeps raising, so no
further model call is ever attempted.

The tracker never sees or stores API keys; configuration is numeric only.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from backend.runtime import CancellationRequested


def paid_execution_enabled() -> bool:
    return os.getenv("MILO_ENABLE_PAID_EXECUTION", "").strip().lower() in {"1", "true", "yes", "on"}


class BudgetExceeded(Exception):
    def __init__(self, code: str, message: str, event_type: str, terminal_status: str):
        super().__init__(message)
        self.code = code
        self.message = message
        self.event_type = event_type
        self.terminal_status = terminal_status


def _env_int(env: dict[str, str], key: str) -> int | None:
    raw = (env.get(key) or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{key} must be an integer, got {raw!r}") from None
    if value <= 0:
        raise ValueError(f"{key} must be positive, got {value}")
    return value


def _env_float(env: dict[str, str], key: str) -> float | None:
    raw = (env.get(key) or "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{key} must be a number, got {raw!r}") from None
    if value <= 0:
        raise ValueError(f"{key} must be positive, got {value}")
    return value


@dataclass(frozen=True)
class BudgetConfig:
    max_model_calls_per_run: int | None = None
    max_input_tokens_per_run: int | None = None
    max_output_tokens_per_run: int | None = None
    max_total_tokens_per_run: int | None = None
    max_estimated_cost_per_run: float | None = None
    max_cost_per_run: float | None = None
    max_run_duration_seconds: int | None = None
    max_agent_steps: int | None = None
    max_retries: int | None = None
    max_concurrent_runs_per_user: int | None = None
    max_concurrent_runs_per_project: int | None = None
    daily_user_budget: float | None = None
    daily_project_budget: float | None = None
    estimated_cost_per_call: float = 0.05

    # Paid execution may never be enabled without these.
    MANDATORY_FOR_PAID_EXECUTION = (
        "max_model_calls_per_run",
        "max_total_tokens_per_run",
        "max_estimated_cost_per_run",
        "max_run_duration_seconds",
        "max_retries",
    )

    ENV_KEYS = {
        "max_model_calls_per_run": "MILO_MAX_MODEL_CALLS_PER_RUN",
        "max_input_tokens_per_run": "MILO_MAX_INPUT_TOKENS_PER_RUN",
        "max_output_tokens_per_run": "MILO_MAX_OUTPUT_TOKENS_PER_RUN",
        "max_total_tokens_per_run": "MILO_MAX_TOTAL_TOKENS_PER_RUN",
        "max_estimated_cost_per_run": "MILO_MAX_ESTIMATED_COST_PER_RUN",
        "max_cost_per_run": "MILO_MAX_COST_PER_RUN",
        "max_run_duration_seconds": "MILO_MAX_RUN_DURATION_SECONDS",
        "max_agent_steps": "MILO_MAX_AGENT_STEPS",
        "max_retries": "MILO_MAX_RETRIES",
        "max_concurrent_runs_per_user": "MILO_MAX_CONCURRENT_RUNS_PER_USER",
        "max_concurrent_runs_per_project": "MILO_MAX_CONCURRENT_RUNS_PER_PROJECT",
        "daily_user_budget": "MILO_DAILY_USER_BUDGET",
        "daily_project_budget": "MILO_DAILY_PROJECT_BUDGET",
    }

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "BudgetConfig":
        source = dict(os.environ if env is None else env)
        ints = (
            "max_model_calls_per_run", "max_input_tokens_per_run", "max_output_tokens_per_run",
            "max_total_tokens_per_run", "max_run_duration_seconds", "max_agent_steps",
            "max_retries", "max_concurrent_runs_per_user", "max_concurrent_runs_per_project",
        )
        floats = ("max_estimated_cost_per_run", "max_cost_per_run", "daily_user_budget", "daily_project_budget")
        values: dict[str, Any] = {}
        for name in ints:
            values[name] = _env_int(source, cls.ENV_KEYS[name])
        for name in floats:
            values[name] = _env_float(source, cls.ENV_KEYS[name])
        estimated = _env_float(source, "MILO_ESTIMATED_COST_PER_CALL")
        if estimated is not None:
            values["estimated_cost_per_call"] = estimated
        return cls(**values)

    def missing_mandatory(self) -> list[str]:
        return [self.ENV_KEYS[name] for name in self.MANDATORY_FOR_PAID_EXECUTION if getattr(self, name) is None]


EventEmitter = Callable[[str, dict[str, Any]], None]
UsageRecorder = Callable[[dict[str, Any]], None]
LedgerRecorder = Callable[[dict[str, Any]], None]
CostProvider = Callable[[], float]

# Conservative chars-per-token heuristic for pre-call input estimation.
CHARS_PER_TOKEN = 4


def estimate_message_tokens(messages: Any) -> int:
    try:
        total_chars = sum(len(str(m.get("content", ""))) if isinstance(m, dict) else len(str(m)) for m in (messages or []))
    except TypeError:
        total_chars = len(str(messages))
    return max(1, total_chars // CHARS_PER_TOKEN)


@dataclass
class BudgetTracker:
    config: BudgetConfig
    cancellation_checker: Callable[[], bool] | None = None
    event_emitter: EventEmitter | None = None
    usage_recorder: UsageRecorder | None = None
    ledger_recorder: LedgerRecorder | None = None
    daily_user_cost_provider: CostProvider | None = None
    daily_project_cost_provider: CostProvider | None = None
    lease_checker: Callable[[], bool] | None = None
    clock: Callable[[], float] = time.monotonic
    kill_switch: Callable[[], bool] = staticmethod(paid_execution_enabled)

    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reserved_input_tokens: int = 0
    reserved_output_tokens: int = 0
    estimated_cost: float = 0.0
    actual_cost: float = 0.0
    retries: int = 0
    agent_steps: int = 0
    stop: BudgetExceeded | None = None
    _started_at: float = field(default=None, init=False)  # type: ignore[assignment]
    _warned: set = field(default_factory=set, init=False)
    _lock: Any = field(default=None, init=False)

    def __post_init__(self):
        import threading

        self._started_at = self.clock()
        self._lock = threading.Lock()

    # -- helpers -------------------------------------------------------------
    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.event_emitter:
            self.event_emitter(event_type, payload)

    def _stop(self, code: str, message: str, event_type: str, terminal_status: str) -> BudgetExceeded:
        exceeded = BudgetExceeded(code, message, event_type, terminal_status)
        if self.stop is None:
            self.stop = exceeded
            self._emit(event_type, {"message": message, "payload": {"code": code, **self.snapshot()}})
        return exceeded

    def snapshot(self) -> dict[str, Any]:
        return {
            "model_calls": self.model_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.input_tokens + self.output_tokens,
            "estimated_cost": round(self.estimated_cost, 6),
            "actual_cost": round(self.actual_cost, 6),
            "retries": self.retries,
            "agent_steps": self.agent_steps,
            "elapsed_seconds": round(self.clock() - self._started_at, 3),
        }

    def elapsed(self) -> float:
        return self.clock() - self._started_at

    def _warn_if_close(self, name: str, used: float, limit: float | None) -> None:
        if limit is None or limit <= 0 or name in self._warned:
            return
        if used >= 0.8 * limit:
            self._warned.add(name)
            self._emit("budget_warning", {"message": f"{name} at {used} of {limit} (>=80%)", "payload": {"limit": name, "used": used, "max": limit, **self.snapshot()}})

    def _ledger(self, decision: str, **fields: Any) -> None:
        if self.ledger_recorder:
            self.ledger_recorder({"decision": decision, "call_seq": self.model_calls, **fields})

    def _reject(self, code: str, message: str, event_type: str, terminal_status: str) -> BudgetExceeded:
        self._ledger("rejected", rejection_reason=code)
        return self._stop(code, message, event_type, terminal_status)

    # -- the hard gate --------------------------------------------------------
    def reserve_call(self, estimated_input_tokens: int = 0, requested_max_tokens: int | None = None) -> int | None:
        """Atomically reserve capacity for one model call BEFORE it happens.

        Checks (in order): paid-execution kill switch, worker lease,
        cancellation, elapsed time, model-call count, agent steps, retries,
        remaining input/output/total tokens (including the pre-call input
        estimate and in-flight reservations), remaining estimated cost, and
        daily budgets. Returns the output-token allowance the call may use
        (``max_tokens`` must be clamped to it), or None when no output cap
        applies. Raises BudgetExceeded when the call must not happen; the
        decision is persisted through the ledger recorder either way.
        Thread-safe: concurrent calls cannot both reserve the final
        remaining call, tokens or cost.
        """
        with self._lock:
            if self.stop is not None:
                raise self.stop
            if not self.kill_switch():
                raise self._reject("PAID_EXECUTION_DISABLED", "global paid-execution kill switch is off", "kill_switch_activated", "failed")
            if self.lease_checker is not None and not self.lease_checker():
                raise self._reject("WORKER_LEASE_LOST", "worker no longer holds the run lease", "run_failed", "failed")
            if self.cancellation_checker and self.cancellation_checker():
                raise CancellationRequested("RUN_CANCELLED")
            cfg = self.config
            estimated_input_tokens = max(0, int(estimated_input_tokens or 0))
            if cfg.max_run_duration_seconds is not None and self.elapsed() >= cfg.max_run_duration_seconds:
                raise self._reject("RUN_DURATION_EXCEEDED", f"run exceeded {cfg.max_run_duration_seconds}s", "run_timed_out", "timed_out")
            if cfg.max_model_calls_per_run is not None and self.model_calls >= cfg.max_model_calls_per_run:
                raise self._reject("MODEL_CALL_LIMIT_REACHED", f"run reached {cfg.max_model_calls_per_run} model calls", "budget_exhausted", "budget_exhausted")
            if cfg.max_agent_steps is not None and self.agent_steps > cfg.max_agent_steps:
                raise self._reject("AGENT_STEP_LIMIT_REACHED", "agent step limit reached", "budget_exhausted", "budget_exhausted")
            if cfg.max_retries is not None and self.retries > cfg.max_retries:
                raise self._reject("RETRY_LIMIT_REACHED", "retry limit reached", "retry_limit_reached", "failed")
            # Remaining-token math includes actuals, in-flight reservations
            # and this call's own input estimate.
            committed_input = self.input_tokens + self.reserved_input_tokens
            committed_output = self.output_tokens + self.reserved_output_tokens
            if cfg.max_input_tokens_per_run is not None and committed_input + estimated_input_tokens > cfg.max_input_tokens_per_run:
                raise self._reject("INPUT_TOKEN_LIMIT_REACHED", "input token limit reached", "token_limit_reached", "budget_exhausted")
            remaining_output = None
            if cfg.max_output_tokens_per_run is not None:
                remaining_output = cfg.max_output_tokens_per_run - committed_output
                if remaining_output <= 0:
                    raise self._reject("OUTPUT_TOKEN_LIMIT_REACHED", "output token limit reached", "token_limit_reached", "budget_exhausted")
            if cfg.max_total_tokens_per_run is not None:
                remaining_total = cfg.max_total_tokens_per_run - committed_input - committed_output - estimated_input_tokens
                remaining_output = remaining_total if remaining_output is None else min(remaining_output, remaining_total)
            if remaining_output is not None and remaining_output <= 0:
                raise self._reject("TOTAL_TOKEN_LIMIT_REACHED", "token budget exhausted before the call", "token_limit_reached", "budget_exhausted")
            if cfg.max_estimated_cost_per_run is not None and (self.estimated_cost + cfg.estimated_cost_per_call) > cfg.max_estimated_cost_per_run:
                raise self._reject("ESTIMATED_COST_LIMIT_REACHED", "estimated cost budget exhausted", "budget_exhausted", "budget_exhausted")
            if cfg.max_cost_per_run is not None and self.actual_cost >= cfg.max_cost_per_run:
                raise self._reject("COST_LIMIT_REACHED", "recorded cost budget exhausted", "budget_exhausted", "budget_exhausted")
            if cfg.daily_user_budget is not None and self.daily_user_cost_provider is not None and self.daily_user_cost_provider() >= cfg.daily_user_budget:
                raise self._reject("DAILY_USER_BUDGET_REACHED", "daily user budget exhausted", "budget_exhausted", "budget_exhausted")
            if cfg.daily_project_budget is not None and self.daily_project_cost_provider is not None and self.daily_project_cost_provider() >= cfg.daily_project_budget:
                raise self._reject("DAILY_PROJECT_BUDGET_REACHED", "daily project budget exhausted", "budget_exhausted", "budget_exhausted")
            allowed_output = remaining_output
            if requested_max_tokens is not None:
                allowed_output = requested_max_tokens if allowed_output is None else min(allowed_output, requested_max_tokens)
            # Reserve the capacity this call may consume. Output capacity is
            # only held as an in-flight reservation when the caller declared
            # a max_tokens request (the guarded client settles exactly that
            # amount back); the legacy before_call/after_call path reserves
            # input estimates only.
            self.model_calls += 1
            self.estimated_cost += cfg.estimated_cost_per_call
            self.reserved_input_tokens += estimated_input_tokens
            if requested_max_tokens is not None and allowed_output is not None:
                self.reserved_output_tokens += allowed_output
            self._ledger(
                "reserved",
                reserved_input_tokens=estimated_input_tokens,
                reserved_output_tokens=(allowed_output or 0) if requested_max_tokens is not None else 0,
                estimated_cost=round(cfg.estimated_cost_per_call, 6),
            )
            return allowed_output

    def settle_call(self, reserved_input_tokens: int = 0, reserved_output_tokens: int | None = None, input_tokens: int = 0, output_tokens: int = 0, cost: float | None = None) -> None:
        """Release the reservation and record actual usage after a call."""
        with self._lock:
            self.reserved_input_tokens = max(0, self.reserved_input_tokens - max(0, int(reserved_input_tokens or 0)))
            if reserved_output_tokens:
                self.reserved_output_tokens = max(0, self.reserved_output_tokens - int(reserved_output_tokens))
            self.input_tokens += max(0, int(input_tokens or 0))
            self.output_tokens += max(0, int(output_tokens or 0))
            if cost is not None and cost > 0:
                self.actual_cost += float(cost)
            cfg = self.config
            self._warn_if_close("model_calls", self.model_calls, cfg.max_model_calls_per_run)
            self._warn_if_close("total_tokens", self.input_tokens + self.output_tokens, cfg.max_total_tokens_per_run)
            self._warn_if_close("estimated_cost", self.estimated_cost, cfg.max_estimated_cost_per_run)
            self._warn_if_close("elapsed_seconds", self.elapsed(), cfg.max_run_duration_seconds)
            self._ledger(
                "settled",
                actual_input_tokens=int(input_tokens or 0),
                actual_output_tokens=int(output_tokens or 0),
                actual_cost=float(cost) if cost else None,
            )
            if self.usage_recorder:
                self.usage_recorder(self.snapshot())

    def before_call(self) -> None:
        """Backwards-compatible gate without token estimation."""
        self.reserve_call(0, None)

    def after_call(self, input_tokens: int = 0, output_tokens: int = 0, cost: float | None = None) -> None:
        self.settle_call(0, None, input_tokens, output_tokens, cost)

    def record_agent_step(self) -> None:
        with self._lock:
            self.agent_steps += 1
            if self.config.max_agent_steps is not None and self.agent_steps > self.config.max_agent_steps:
                raise self._reject("AGENT_STEP_LIMIT_REACHED", "agent step limit reached", "budget_exhausted", "budget_exhausted")

    def record_retry(self) -> None:
        with self._lock:
            self.retries += 1
            if self.config.max_retries is not None and self.retries > self.config.max_retries:
                raise self._reject("RETRY_LIMIT_REACHED", "retry limit reached", "retry_limit_reached", "failed")


class _GuardedCompletions:
    def __init__(self, inner: Any, tracker: BudgetTracker):
        self._inner = inner
        self._tracker = tracker

    def create(self, **kwargs: Any) -> Any:
        estimated_input = estimate_message_tokens(kwargs.get("messages"))
        requested_max = kwargs.get("max_tokens")
        # Hard pre-call gate: reserve capacity FIRST; the adapter is only
        # reached after the reservation succeeds, and max_tokens is clamped
        # to the remaining safe allowance.
        allowed_output = self._tracker.reserve_call(estimated_input, requested_max)
        reserved_output = allowed_output if requested_max is not None else None
        if allowed_output is not None:
            kwargs["max_tokens"] = allowed_output
        try:
            response = self._inner.create(**kwargs)
        except Exception:
            self._tracker.settle_call(estimated_input, reserved_output, 0, 0)
            self._tracker.record_retry()
            raise
        usage = getattr(response, "usage", None)
        self._tracker.settle_call(
            reserved_input_tokens=estimated_input,
            reserved_output_tokens=reserved_output,
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
        )
        return response


class _GuardedChat:
    def __init__(self, inner: Any, tracker: BudgetTracker):
        self.completions = _GuardedCompletions(inner.completions, tracker)


class GuardedModelClient:
    """Wraps an OpenAI-compatible client so every chat completion is gated."""

    def __init__(self, inner: Any, tracker: BudgetTracker):
        self._inner = inner
        self.chat = _GuardedChat(inner.chat, tracker)


def build_guarded_client_factory(tracker: BudgetTracker, inner_factory: Callable[[str, str], Any] | None = None) -> Callable[[str, str], Any]:
    """Produce a model_client_factory enforcing the budget gate.

    Preserves the existing MILO engine behavior: the wrapped client is the
    unchanged production client; only the call boundary is guarded.
    """

    def factory(api_key: str, base_url: str) -> Any:
        if inner_factory is not None:
            inner = inner_factory(api_key, base_url)
        else:
            from openai import OpenAI

            inner = OpenAI(api_key=api_key, base_url=base_url)
        return GuardedModelClient(inner, tracker)

    return factory

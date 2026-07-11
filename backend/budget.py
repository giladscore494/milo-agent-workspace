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
CostProvider = Callable[[], float]


@dataclass
class BudgetTracker:
    config: BudgetConfig
    cancellation_checker: Callable[[], bool] | None = None
    event_emitter: EventEmitter | None = None
    usage_recorder: UsageRecorder | None = None
    daily_user_cost_provider: CostProvider | None = None
    daily_project_cost_provider: CostProvider | None = None
    clock: Callable[[], float] = time.monotonic
    kill_switch: Callable[[], bool] = staticmethod(paid_execution_enabled)

    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost: float = 0.0
    actual_cost: float = 0.0
    retries: int = 0
    agent_steps: int = 0
    stop: BudgetExceeded | None = None
    _started_at: float = field(default=None, init=False)  # type: ignore[assignment]
    _warned: set = field(default_factory=set, init=False)

    def __post_init__(self):
        self._started_at = self.clock()

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

    # -- the hard gate --------------------------------------------------------
    def before_call(self) -> None:
        """Must be called immediately before every paid model call."""
        if self.stop is not None:
            raise self.stop
        if not self.kill_switch():
            raise self._stop("PAID_EXECUTION_DISABLED", "global paid-execution kill switch is off", "kill_switch_activated", "failed")
        if self.cancellation_checker and self.cancellation_checker():
            raise CancellationRequested("RUN_CANCELLED")
        cfg = self.config
        if cfg.max_run_duration_seconds is not None and self.elapsed() >= cfg.max_run_duration_seconds:
            raise self._stop("RUN_DURATION_EXCEEDED", f"run exceeded {cfg.max_run_duration_seconds}s", "run_timed_out", "timed_out")
        if cfg.max_model_calls_per_run is not None and self.model_calls >= cfg.max_model_calls_per_run:
            raise self._stop("MODEL_CALL_LIMIT_REACHED", f"run reached {cfg.max_model_calls_per_run} model calls", "budget_exhausted", "budget_exhausted")
        if cfg.max_input_tokens_per_run is not None and self.input_tokens >= cfg.max_input_tokens_per_run:
            raise self._stop("INPUT_TOKEN_LIMIT_REACHED", "input token limit reached", "token_limit_reached", "budget_exhausted")
        if cfg.max_output_tokens_per_run is not None and self.output_tokens >= cfg.max_output_tokens_per_run:
            raise self._stop("OUTPUT_TOKEN_LIMIT_REACHED", "output token limit reached", "token_limit_reached", "budget_exhausted")
        if cfg.max_total_tokens_per_run is not None and (self.input_tokens + self.output_tokens) >= cfg.max_total_tokens_per_run:
            raise self._stop("TOTAL_TOKEN_LIMIT_REACHED", "total token limit reached", "token_limit_reached", "budget_exhausted")
        if cfg.max_estimated_cost_per_run is not None and (self.estimated_cost + cfg.estimated_cost_per_call) > cfg.max_estimated_cost_per_run:
            raise self._stop("ESTIMATED_COST_LIMIT_REACHED", "estimated cost budget exhausted", "budget_exhausted", "budget_exhausted")
        if cfg.max_cost_per_run is not None and self.actual_cost >= cfg.max_cost_per_run:
            raise self._stop("COST_LIMIT_REACHED", "recorded cost budget exhausted", "budget_exhausted", "budget_exhausted")
        if cfg.daily_user_budget is not None and self.daily_user_cost_provider is not None and self.daily_user_cost_provider() >= cfg.daily_user_budget:
            raise self._stop("DAILY_USER_BUDGET_REACHED", "daily user budget exhausted", "budget_exhausted", "budget_exhausted")
        if cfg.daily_project_budget is not None and self.daily_project_cost_provider is not None and self.daily_project_cost_provider() >= cfg.daily_project_budget:
            raise self._stop("DAILY_PROJECT_BUDGET_REACHED", "daily project budget exhausted", "budget_exhausted", "budget_exhausted")
        self.model_calls += 1
        self.estimated_cost += cfg.estimated_cost_per_call

    def after_call(self, input_tokens: int = 0, output_tokens: int = 0, cost: float | None = None) -> None:
        self.input_tokens += max(0, int(input_tokens or 0))
        self.output_tokens += max(0, int(output_tokens or 0))
        if cost is not None and cost > 0:
            self.actual_cost += float(cost)
        cfg = self.config
        self._warn_if_close("model_calls", self.model_calls, cfg.max_model_calls_per_run)
        self._warn_if_close("total_tokens", self.input_tokens + self.output_tokens, cfg.max_total_tokens_per_run)
        self._warn_if_close("estimated_cost", self.estimated_cost, cfg.max_estimated_cost_per_run)
        self._warn_if_close("elapsed_seconds", self.elapsed(), cfg.max_run_duration_seconds)
        if self.usage_recorder:
            self.usage_recorder(self.snapshot())

    def record_agent_step(self) -> None:
        self.agent_steps += 1
        if self.config.max_agent_steps is not None and self.agent_steps > self.config.max_agent_steps:
            raise self._stop("AGENT_STEP_LIMIT_REACHED", "agent step limit reached", "budget_exhausted", "budget_exhausted")

    def record_retry(self) -> None:
        self.retries += 1
        if self.config.max_retries is not None and self.retries > self.config.max_retries:
            raise self._stop("RETRY_LIMIT_REACHED", "retry limit reached", "retry_limit_reached", "failed")


class _GuardedCompletions:
    def __init__(self, inner: Any, tracker: BudgetTracker):
        self._inner = inner
        self._tracker = tracker

    def create(self, **kwargs: Any) -> Any:
        self._tracker.before_call()
        try:
            response = self._inner.create(**kwargs)
        except Exception:
            self._tracker.record_retry()
            raise
        usage = getattr(response, "usage", None)
        self._tracker.after_call(
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

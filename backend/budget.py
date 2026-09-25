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

from backend.execution_usage import (LEDGER_AMOUNTS, LEDGER_COUNTERS, LEDGER_SCHEMA_VERSION,
                                     LEDGER_SNAPSHOT_FIELDS, PUBLIC_USAGE_FIELDS,
                                     merge_usage_snapshots, public_usage_projection,
                                     validate_usage_snapshot)
from backend.model_profiles import MODEL_PROFILE_UNKNOWN, UnknownModelProfile, get_profile
from backend.provider_authority import (ProviderOutcome, ProviderVerdict,
                                        classify_outcome)
from backend.runtime import CancellationRequested
from backend.runtime_policy import BUDGET as _POLICY_BUDGET_SURFACE
from backend.runtime_policy import dimensions_for as _policy_dimensions_for


def _policy_budget_dimensions():
    """The budget dimensions of the ONE canonical runtime policy.

    `backend.runtime_policy` is the authority for what a run's enforceable
    limits ARE; this module is the authority for how they are enforced. The
    env-variable names and the mandatory-for-paid set therefore come from
    there rather than being written down a second time.
    """
    return _policy_dimensions_for(_POLICY_BUDGET_SURFACE)


def paid_execution_enabled() -> bool:
    return os.getenv("MILO_ENABLE_PAID_EXECUTION", "").strip().lower() in {"1", "true", "yes", "on"}


# The cumulative dimensions of a run's durable usage record live in ONE
# place, `backend.execution_usage` (the ExecutionUsageLedger contract), and
# are re-exported here under their historical names. EVERY one of them only
# ever grows while a run executes, which is what makes a component-wise
# maximum of two records of the same run safe: it can never hand back
# capacity a record already shows as spent.
#
# `USAGE_SNAPSHOT_FIELDS` is the bounded PUBLIC shape written to `runs.usage`
# (`BudgetTracker.snapshot()`); `LEDGER_SNAPSHOT_FIELDS` is the full ledger
# (`BudgetTracker.ledger_snapshot()`), of which the public shape is a strict
# projection. `reserved_input_tokens` / `reserved_output_tokens` are in
# neither: they are in-flight reservations the previous process no longer
# owns, and they never enter a durable record at all.
CUMULATIVE_USAGE_COUNTERS = LEDGER_COUNTERS
CUMULATIVE_USAGE_AMOUNTS = LEDGER_AMOUNTS
USAGE_SNAPSHOT_FIELDS = PUBLIC_USAGE_FIELDS


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
    # --- search volume and price ---------------------------------------
    # Bounded by DEFAULT, unlike the ceilings above. Those are deliberately
    # None-until-configured because a paid deployment must state them
    # explicitly; search had no bound of any kind, in any posture, so the
    # safe default here is the reviewed number rather than "unlimited".
    max_search_invocations_per_run: int | None = 60
    #: The MAXIMUM searches one provider request may perform. Admission
    #: reserves this much before the request is dispatched, so a request is
    #: only ever sent when the run can pay for the worst case it could spend.
    max_builtin_searches_per_request: int = 4
    #: The price interface. 0.00 until a verified provider price exists; a
    #: configured value is charged to the run's recorded cost like any spend.
    search_cost_per_invocation: float = 0.0

    # DERIVED from the canonical runtime policy, never restated here.
    #
    # `MANDATORY_FOR_PAID_EXECUTION` used to be a hand-kept list of five
    # names, and it had fallen behind the profile it was supposed to
    # guarantee: neither `max_agent_steps` nor the recorded-cost cap was on
    # it, so a paid deployment could start with the two dimensions the
    # reviewed first-run profile advertises most loudly simply absent. The
    # registry derives membership instead -- a dimension is mandatory exactly
    # when leaving it unset lets the runtime operate WIDER than the reviewed
    # value -- so the set cannot fall behind the profile again.
    #
    # `MANDATORY_FOR_RUN_CREATION` is the separate, smaller floor for
    # UNPAID run creation, which spends nothing: it is the historical
    # five-value set and is deliberately not widened here.
    #: Only the dimensions a DEPLOYMENT may set. A policy dimension with no
    #: env key is not absent from the envelope -- it is a reviewed value that
    #: a deployment does not get to move, and it must not appear here as a
    #: name mapped to nothing.
    ENV_KEYS = {dimension.name: dimension.env_key
                for dimension in _policy_budget_dimensions()
                if dimension.env_key is not None}
    MANDATORY_FOR_PAID_EXECUTION = tuple(
        dimension.name for dimension in _policy_budget_dimensions()
        if dimension.mandatory_for_paid)
    MANDATORY_FOR_RUN_CREATION = (
        "max_model_calls_per_run",
        "max_total_tokens_per_run",
        "max_estimated_cost_per_run",
        "max_run_duration_seconds",
        "max_retries",
    )

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
        """Dimensions PAID execution may never start without."""
        return [self.ENV_KEYS[name] for name in self.MANDATORY_FOR_PAID_EXECUTION
                if getattr(self, name) is None]

    def missing_for_run_creation(self) -> list[str]:
        """The smaller floor for UNPAID run creation, which spends nothing."""
        return [self.ENV_KEYS[name] for name in self.MANDATORY_FOR_RUN_CREATION
                if getattr(self, name) is None]


EventEmitter = Callable[[str, dict[str, Any]], None]
#: Receives the FULL ledger snapshot after every recorded consumption. It may
#: return the durable record (or its ``ledger_version``) so the tracker can
#: carry the database's write sequence number; None is accepted.
UsageRecorder = Callable[[dict[str, Any]], Any]
LedgerRecorder = Callable[[dict[str, Any]], None]
CostProvider = Callable[[], float]
@dataclass(frozen=True)
class ModelCallReservation:
    """Private in-memory handle for a canonical database budget reservation."""

    id: str
    call_seq: int
    estimated_cost: float


DailyReservation = Callable[[float, int], ModelCallReservation | str | dict[str, Any] | None]
DailySettlement = Callable[[ModelCallReservation, float, str, str | None], Any]

# Conservative chars-per-token heuristic for pre-call input estimation.
CHARS_PER_TOKEN = 4

# --- the provider output-cap contract ---------------------------------------
#
# Kimi admits a request against request tokens PLUS the requested completion
# cap, so the cap must be a number the provider can see BEFORE the call, not
# something inferred afterwards from usage. These three helpers are the single
# place that knows how the cap is spelled on the wire, so a test can assert on
# the exact provider request and a future provider-side rename is one edit.
#
# `max_tokens` is what the Moonshot/Kimi OpenAI-compatible endpoint accepts
# today; `max_completion_tokens` is the newer OpenAI spelling. Callers may use
# either name and the wire field is emitted once, never both (sending both is
# rejected by some OpenAI-compatible servers).
PROVIDER_OUTPUT_CAP_FIELD = "max_tokens"
OUTPUT_CAP_ALIASES = ("max_completion_tokens", "max_tokens")

#: Server-owned cap for a caller that supplied none. It exists so that an
#: un-capped call FAILS SAFE to a bounded number instead of inheriting the
#: whole remaining run allowance; it is deliberately small, and a role that
#: needs more must declare it.
DEFAULT_OUTPUT_CAP = 1024


def read_output_cap(kwargs: dict[str, Any]) -> int | None:
    """Return the caller's requested output cap under either spelling."""
    for name in OUTPUT_CAP_ALIASES:
        value = kwargs.get(name)
        if value is None:
            continue
        try:
            cap = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be an integer") from None
        if cap <= 0:
            raise ValueError(f"{name} must be positive")
        return cap
    return None


def apply_output_cap(kwargs: dict[str, Any], cap: int) -> dict[str, Any]:
    """Put exactly ONE numeric output cap on the outgoing provider request."""
    for name in OUTPUT_CAP_ALIASES:
        kwargs.pop(name, None)
    kwargs[PROVIDER_OUTPUT_CAP_FIELD] = int(cap)
    return kwargs


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
    daily_user_reserver: DailyReservation | None = None
    daily_project_reserver: DailyReservation | None = None
    daily_settler: DailySettlement | None = None
    lease_checker: Callable[[], bool] | None = None
    clock: Callable[[], float] = time.monotonic
    kill_switch: Callable[[], bool] = staticmethod(paid_execution_enabled)

    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reserved_input_tokens: int = 0
    reserved_output_tokens: int = 0
    #: In-flight search capacity held for requests that have not settled yet.
    #: Counted against the ceiling alongside what has already been spent.
    reserved_search_invocations: int = 0
    estimated_cost: float = 0.0
    actual_cost: float = 0.0
    retries: int = 0
    provider_backpressure_events: int = 0
    agent_steps: int = 0
    # --- ledger-only dimensions (backend.execution_usage) ------------------
    # Durable and cumulative like everything above, but outside the bounded
    # public `runs.usage` contract: they reach the database through
    # `ledger_snapshot()`, never through `snapshot()`.
    provider_attempts: int = 0
    provider_failures: int = 0
    tool_calls: int = 0
    tasks_completed: int = 0
    tasks_failed: int = 0
    search_invocations: int = 0
    search_cost: float = 0.0
    replans: int = 0
    correction_rounds: int = 0
    #: The durable record's write sequence number, as last reported by the
    #: recorder. 0 until the first accepted durable write.
    ledger_version: int = 0
    #: Telemetry only: how many calls reached the gate with no declared cap.
    #: Deliberately NOT a cumulative snapshot field -- it is a code-health
    #: signal, not run capacity, and it must not gate a resume.
    missing_output_cap_calls: int = 0
    stop: BudgetExceeded | None = None
    _started_at: float = field(default=None, init=False)  # type: ignore[assignment]
    _warned: set = field(default_factory=set, init=False)
    _lock: Any = field(default=None, init=False)
    _reservations: dict[int, ModelCallReservation] = field(default_factory=dict, init=False)
    #: call_seq -> (reserved_input_tokens, reserved_output_tokens) still in
    #: flight. Popped by settle_call, so a reservation is released exactly once
    #: and by the amount that was actually taken.
    _open_reservations: dict[int, tuple[int, int]] = field(default_factory=dict, init=False)
    #: search reservations still in flight, keyed by their own sequence.
    #: IN-FLIGHT, never durable: like the token reservations, a reservation
    #: belongs to the process that took it, so a resumed worker starts with
    #: none rather than inheriting a phantom hold from a dead one.
    _open_search_reservations: dict[int, int] = field(default_factory=dict, init=False)
    _search_seq: int = field(default=0, init=False)

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
        """The bounded PUBLIC aggregate: exactly the `runs.usage` contract."""
        return public_usage_projection(self.ledger_snapshot())

    def ledger_snapshot(self) -> dict[str, Any]:
        """The FULL cumulative ledger of this run, as this process knows it.

        Every value is cumulative across attempts: a resumed tracker starts
        from the merged durable record (`restore_snapshot`) and only ever adds
        to it. In-flight reservations are deliberately absent.
        """
        return {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "ledger_version": self.ledger_version,
            "model_calls": self.model_calls,
            "provider_attempts": self.provider_attempts,
            "provider_failures": self.provider_failures,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.input_tokens + self.output_tokens,
            "estimated_cost": round(self.estimated_cost, 6),
            "actual_cost": round(self.actual_cost, 6),
            "retries": self.retries,
            "provider_backpressure_events": self.provider_backpressure_events,
            "agent_steps": self.agent_steps,
            "tool_calls": self.tool_calls,
            "tasks_completed": self.tasks_completed,
            "tasks_failed": self.tasks_failed,
            "search_invocations": self.search_invocations,
            "search_cost": round(self.search_cost, 6),
            "replans": self.replans,
            "correction_rounds": self.correction_rounds,
            "elapsed_seconds": round(self.clock() - self._started_at, 3),
        }

    def _record(self) -> None:
        """Make the current ledger durable through the injected recorder.

        Called after EVERY consumption, not only after a settled provider
        call, so a crash between two provider calls cannot lose the retry,
        agent step, tool call, task or replan recorded in between. A recorder
        failure propagates: a worker whose lease was reclaimed must not keep
        consuming against a record it can no longer write.
        """
        if not self.usage_recorder:
            return
        reported = self.usage_recorder(self.ledger_snapshot())
        version = reported.get("ledger_version") if isinstance(reported, dict) else reported
        if isinstance(version, bool) or not isinstance(version, int):
            return
        # The database only ever moves the sequence forward.
        self.ledger_version = max(self.ledger_version, version)

    def restore_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Restore a trusted durable ledger record before resume.

        A resumed worker must not regain per-run capacity on ANY cumulative
        dimension. The record may be the public `runs.usage` shape, a
        checkpoint's `token_usage`, the `run_execution_usage` row or their
        merge (`merge_usage_snapshots`); a field the record does not carry is
        restored as 0. In-flight reservations are deliberately not restored;
        the previous process no longer owns them.
        """
        if not isinstance(snapshot, dict):
            raise ValueError("budget snapshot must be an object")
        if set(snapshot) - LEDGER_SNAPSHOT_FIELDS:
            raise ValueError("budget snapshot contains unknown fields")
        parsed = validate_usage_snapshot(snapshot)
        with self._lock:
            if (any(getattr(self, name) for name in (*LEDGER_COUNTERS, *LEDGER_AMOUNTS)
                    if name != "elapsed_seconds")
                    or self.reserved_input_tokens or self.reserved_output_tokens
                    or self.reserved_search_invocations):
                raise ValueError("budget usage can only be restored into a fresh tracker")
            for name in LEDGER_COUNTERS:
                setattr(self, name, int(parsed.get(name, 0)))
            for name in LEDGER_AMOUNTS:
                if name != "elapsed_seconds":
                    setattr(self, name, float(parsed.get(name, 0.0)))
            self.ledger_version = int(parsed.get("ledger_version", 0))
            self._started_at = self.clock() - float(parsed.get("elapsed_seconds", 0.0))

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

    def refuse_call(self, code: str, message: str, *, event_type: str = "run_failed",
                    terminal_status: str = "failed") -> BudgetExceeded:
        """Refuse a call BEFORE admission, with a static reason code.

        For refusals that are decided outside the capacity gate but must be
        recorded and terminal exactly like one -- an unknown model profile is
        a configuration fault, and a run that cannot price its calls must not
        make any. The caller raises the returned exception.
        """
        with self._lock:
            if self.stop is not None:
                return self.stop
            return self._reject(code, message, event_type, terminal_status)

    def _normalize_reservation(self, raw: ModelCallReservation | str | dict[str, Any] | None, call_seq: int, estimated_cost: float) -> ModelCallReservation:
        if isinstance(raw, ModelCallReservation):
            return raw
        if isinstance(raw, str) and raw:
            return ModelCallReservation(raw, call_seq, estimated_cost)
        if isinstance(raw, dict):
            if raw.get("status") == "rejected":
                raise self._reject(str(raw.get("rejection_reason") or "DAILY_BUDGET_REACHED"), "daily model-call budget exhausted", "budget_exhausted", "budget_exhausted")
            rid = raw.get("id") or raw.get("reservation_id")
            if rid:
                return ModelCallReservation(str(rid), int(raw.get("call_seq") or call_seq), float(raw.get("estimated_cost") or estimated_cost))
        raise self._reject("BUDGET_RESERVATION_MISSING", "model-call budget reservation did not return an id", "budget_exhausted", "failed")

    # -- the hard gate --------------------------------------------------------
    def reserve_call(self, estimated_input_tokens: int = 0, requested_max_tokens: int | None = None) -> int | None:
        """Backwards-compatible reservation returning only the output allowance."""
        return self.open_call(estimated_input_tokens, requested_max_tokens)[1]

    def open_call(self, estimated_input_tokens: int = 0, requested_max_tokens: int | None = None) -> tuple[int, int | None]:
        """Atomically reserve capacity for one model call BEFORE it happens.

        Checks (in order): paid-execution kill switch, worker lease,
        cancellation, elapsed time, model-call count, agent steps, retries,
        remaining input/output/total tokens (including the pre-call input
        estimate and in-flight reservations), remaining estimated cost, and
        daily budgets. Returns ``(call_seq, allowed_output)``: the call's
        reservation sequence (which MUST be passed back to ``settle_call`` so
        concurrent calls settle their own reservations) and the output-token
        allowance the call may use (``max_tokens`` must be clamped to it), or
        None when no output cap applies. Raises BudgetExceeded when the call
        must not happen; the decision is persisted through the ledger
        recorder either way. Thread-safe: concurrent calls cannot both
        reserve the final remaining call, tokens or cost.
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
            next_call_seq = self.model_calls + 1
            reservation: ModelCallReservation | None = None
            if cfg.daily_user_budget is not None and self.daily_user_reserver is not None:
                reservation = self._normalize_reservation(self.daily_user_reserver(cfg.estimated_cost_per_call, next_call_seq), next_call_seq, cfg.estimated_cost_per_call)
            if cfg.daily_project_budget is not None and self.daily_project_reserver is not None:
                reservation = self._normalize_reservation(self.daily_project_reserver(cfg.estimated_cost_per_call, next_call_seq), next_call_seq, cfg.estimated_cost_per_call)
            allowed_output = remaining_output
            if requested_max_tokens is not None:
                allowed_output = requested_max_tokens if allowed_output is None else min(allowed_output, requested_max_tokens)
            # Reserve the capacity this call may consume. Output capacity is
            # held as an in-flight reservation WHENEVER an allowance exists --
            # including when the caller declared no cap of its own.
            #
            # It used to be held only for a declared cap, and the allowance was
            # still injected into the provider request either way. Two calls in
            # flight therefore each received the whole remaining allowance and
            # the ceiling was breached by a factor of the provider concurrency,
            # with the budget stop arriving only AFTER both had settled -- that
            # is, after the tokens were already bought. Reserving here is what
            # makes the ceiling a ceiling rather than an after-the-fact alarm.
            #
            # The amount is recorded against this call's sequence so settlement
            # releases exactly what THIS call reserved, whatever the caller
            # passes back, on every terminal path and exactly once.
            self.model_calls += 1
            # Every admitted request is an ATTEMPT whether or not it settles:
            # a provider exception, a 429 or a deadline still consumed it.
            self.provider_attempts += 1
            self.estimated_cost += cfg.estimated_cost_per_call
            self.reserved_input_tokens += estimated_input_tokens
            held_output = int(allowed_output) if allowed_output is not None else 0
            if held_output > 0:
                self.reserved_output_tokens += held_output
            self._open_reservations[next_call_seq] = (estimated_input_tokens, held_output)
            if reservation is not None:
                self._reservations[next_call_seq] = reservation
            self._ledger(
                "reserved",
                reserved_input_tokens=estimated_input_tokens,
                reserved_output_tokens=held_output,
                estimated_cost=round(cfg.estimated_cost_per_call, 6),
            )
            # The admission itself is durable BEFORE the request is sent. A
            # process that dies mid-request may already have been charged for
            # it; recording only at settlement would let the replacement
            # worker start one call short of what was really attempted.
            self._record()
            return next_call_seq, allowed_output

    def settle_call(self, reserved_input_tokens: int = 0, reserved_output_tokens: int | None = None, input_tokens: int = 0, output_tokens: int = 0, cost: float | None = None, status: str = "settled", rejection_reason: str | None = None, call_seq: int | None = None) -> None:
        """Release the reservation and record actual usage after a call.

        ``call_seq`` identifies WHICH reservation settles. Concurrent calls
        must pass the sequence returned by ``open_call``; the fallback to the
        current model-call counter is only correct for strictly sequential
        callers (the legacy before_call/after_call path)."""
        with self._lock:
            settled_seq = call_seq if call_seq is not None else self.model_calls
            reservation = self._reservations.pop(settled_seq, None)
            actual_cost = float(cost) if cost else 0.0
            if reservation is not None and self.daily_settler is not None:
                try:
                    self.daily_settler(reservation, actual_cost, status, rejection_reason)
                except Exception as exc:
                    self._reservations[settled_seq] = reservation
                    raise self._stop("BUDGET_SETTLEMENT_FAILED", "model-call budget settlement failed", "budget_exhausted", "failed") from exc
            # Release exactly what THIS call reserved. Popping the record is
            # what makes a double settlement a no-op instead of a refund: a
            # second release would hand back capacity the run never held, and
            # an over-released ceiling is indistinguishable from no ceiling.
            # The caller's own numbers are honoured only for callers that
            # predate the per-sequence record.
            held = self._open_reservations.pop(settled_seq, None)
            if held is not None:
                held_input, held_output = held
            else:
                held_input = max(0, int(reserved_input_tokens or 0))
                held_output = max(0, int(reserved_output_tokens or 0))
            self.reserved_input_tokens = max(0, self.reserved_input_tokens - held_input)
            if held_output:
                self.reserved_output_tokens = max(0, self.reserved_output_tokens - held_output)
            self.input_tokens += max(0, int(input_tokens or 0))
            self.output_tokens += max(0, int(output_tokens or 0))
            if cost is not None and cost > 0:
                self.actual_cost += actual_cost
            if status != "settled":
                # A released or rejected settlement is an attempt that bought
                # no response; it stays counted, it is never refunded.
                self.provider_failures += 1
            cfg = self.config
            self._warn_if_close("model_calls", self.model_calls, cfg.max_model_calls_per_run)
            self._warn_if_close("total_tokens", self.input_tokens + self.output_tokens, cfg.max_total_tokens_per_run)
            self._warn_if_close("estimated_cost", self.estimated_cost, cfg.max_estimated_cost_per_run)
            self._warn_if_close("elapsed_seconds", self.elapsed(), cfg.max_run_duration_seconds)
            self._ledger(
                "settled",
                call_seq=settled_seq,
                actual_input_tokens=int(input_tokens or 0),
                actual_output_tokens=int(output_tokens or 0),
                actual_cost=actual_cost if cost else None,
            )
            self._record()
            if cfg.max_input_tokens_per_run is not None and self.input_tokens > cfg.max_input_tokens_per_run:
                self._ledger("overage", call_seq=settled_seq, rejection_reason="INPUT_TOKEN_LIMIT_EXCEEDED")
                raise self._stop("INPUT_TOKEN_LIMIT_EXCEEDED", "actual input token limit exceeded", "token_limit_reached", "budget_exhausted")
            if cfg.max_output_tokens_per_run is not None and self.output_tokens > cfg.max_output_tokens_per_run:
                self._ledger("overage", call_seq=settled_seq, rejection_reason="OUTPUT_TOKEN_LIMIT_EXCEEDED")
                raise self._stop("OUTPUT_TOKEN_LIMIT_EXCEEDED", "actual output token limit exceeded", "token_limit_reached", "budget_exhausted")
            if cfg.max_total_tokens_per_run is not None and (self.input_tokens + self.output_tokens) > cfg.max_total_tokens_per_run:
                self._ledger("overage", call_seq=settled_seq, rejection_reason="TOTAL_TOKEN_LIMIT_EXCEEDED")
                raise self._stop("TOTAL_TOKEN_LIMIT_EXCEEDED", "actual total token limit exceeded", "token_limit_reached", "budget_exhausted")
            if cfg.max_cost_per_run is not None and self.actual_cost > cfg.max_cost_per_run:
                self._ledger("overage", call_seq=settled_seq, rejection_reason="COST_LIMIT_EXCEEDED")
                raise self._stop("COST_LIMIT_EXCEEDED", "actual cost limit exceeded", "budget_exhausted", "budget_exhausted")

    def before_call(self) -> None:
        """Backwards-compatible gate without token estimation."""
        self.reserve_call(0, None)

    def after_call(self, input_tokens: int = 0, output_tokens: int = 0, cost: float | None = None) -> None:
        self.settle_call(0, None, input_tokens, output_tokens, cost)

    def record_agent_step(self) -> None:
        with self._lock:
            self.agent_steps += 1
            self._record()
            if self.config.max_agent_steps is not None and self.agent_steps > self.config.max_agent_steps:
                raise self._reject("AGENT_STEP_LIMIT_REACHED", "agent step limit reached", "budget_exhausted", "budget_exhausted")

    def record_retry(self) -> None:
        with self._lock:
            self.retries += 1
            self._record()
            if self.config.max_retries is not None and self.retries > self.config.max_retries:
                raise self._reject("RETRY_LIMIT_REACHED", "retry limit reached", "retry_limit_reached", "failed")

    # -- ledger-only consumption ------------------------------------------
    # None of these enforces a limit of its own: the Swarm plan firewall and
    # feasibility gate bound tasks, tool calls and replans by SHAPE before
    # execution, and RuntimePolicy stays the authority for limits. What they
    # do is make the consumption DURABLE and cumulative, so a resumed run's
    # remaining tool-call, task and replan capacity is computed from what was
    # really spent rather than rebuilt from the current plan.
    def record_tool_call(self) -> None:
        """One registered Tool operation is about to be invoked."""
        with self._lock:
            self.tool_calls += 1
            self._record()

    def record_task_result(self, status: str) -> None:
        """One logical task execution ended with ``status``."""
        with self._lock:
            if status == "completed":
                self.tasks_completed += 1
            elif status == "failed":
                self.tasks_failed += 1
            else:
                return
            self._record()

    def reserve_search(self, count: int = 1) -> int:
        """Hold capacity for the MAXIMUM searches a request could perform.

        A CHECK is not a ceiling, and that is the whole reason this is a
        reservation. The first version of this gate admitted a
        search-enabled request against ONE invocation while the response it
        would produce can legitimately carry several: with one invocation
        left, the request was admitted, the provider ran and billed two
        searches, and MILO discovered it afterwards. Post-facto detection is
        not a ceiling -- the money is already spent by the time it fires.

        So a caller reserves the most the request could possibly spend
        BEFORE dispatch, and settlement reconciles that to what really
        happened. A reservation also makes the ceiling hold across concurrent
        workers, which a bare read-then-check never could: two requests could
        both see the same single remaining invocation and both be admitted.

        Returns the reservation's sequence number, which MUST be passed back
        to :meth:`settle_search` so each caller releases its own.
        """
        wanted = max(0, int(count or 0))
        if wanted == 0:
            return 0
        with self._lock:
            if self.stop is not None:
                raise self.stop
            cfg = self.config
            committed = self.search_invocations + self.reserved_search_invocations
            if (cfg.max_search_invocations_per_run is not None
                    and committed + wanted > cfg.max_search_invocations_per_run):
                raise self._reject("SEARCH_LIMIT_REACHED", "search invocation limit reached",
                                   "budget_exhausted", "budget_exhausted")
            self._search_seq += 1
            self.reserved_search_invocations += wanted
            self._open_search_reservations[self._search_seq] = wanted
            return self._search_seq

    def settle_search(self, reservation_seq: int, actual: int | None = None,
                      cost: float | None = None, *,
                      pre_execution: bool = False) -> None:
        """Release a search reservation and record what was really spent.

        ``actual`` is the number of searches the provider really performed.
        ``None`` means MILO CANNOT TELL -- an attempt that raised, or a
        response whose shape does not let the searches be counted -- and that
        fails closed: the whole reservation is charged. An unknown amount of
        provider spend is not zero spend, and resolving it to zero is how a
        ceiling stops being one.

        Releasing pops the record, so a second settlement is a no-op rather
        than a refund: an over-released ceiling is indistinguishable from no
        ceiling. Charging ``actual`` and releasing the hold happen together,
        so nothing is counted twice.

        Charging MORE than was reserved means the request performed more
        searches than the reviewed per-request maximum. The excess is still
        recorded -- it really happened and was really billed -- and the run
        then stops: a per-request bound that can be exceeded without
        consequence is a comment, not a bound.

        ``pre_execution`` is used ONLY by MILO-mediated standalone search,
        where the per-invocation price is known before the HTTP request is
        sent. In that mode a fixed-cost overage releases the in-flight hold
        and stops the run WITHOUT incrementing search usage, because no search
        has happened yet. Provider-executed residual builtin search must leave
        this False: by settlement time that work already happened and must be
        recorded truthfully even when it pushes the run over a cap.
        """
        cfg = self.config
        unit = float(cfg.search_cost_per_invocation if cost is None else cost)
        if unit < 0:
            raise ValueError("search cost cannot be negative")
        with self._lock:
            held = self._open_search_reservations.get(int(reservation_seq))
            if held is None:
                return
            charged = held if actual is None else max(0, int(actual))
            amount = unit * charged

            # Standalone search has a FIXED, server-owned price and has not
            # executed yet. Refuse atomically before turning the reservation
            # into durable usage if that known charge cannot fit. This keeps
            # the hard cost gate pre-execution without inventing a search in
            # the durable ledger that never happened.
            if (pre_execution and charged
                    and cfg.max_cost_per_run is not None
                    and self.actual_cost + amount > cfg.max_cost_per_run):
                self._open_search_reservations.pop(int(reservation_seq), None)
                self.reserved_search_invocations = max(
                    0, self.reserved_search_invocations - held)
                raise self._stop(
                    "COST_LIMIT_REACHED",
                    "recorded cost budget cannot admit another search",
                    "budget_exhausted", "budget_exhausted")

            self._open_search_reservations.pop(int(reservation_seq), None)
            self.reserved_search_invocations = max(
                0, self.reserved_search_invocations - held)
            if charged:
                self.search_invocations += charged
                self.search_cost += amount
                if amount:
                    self.actual_cost += amount
            # NO per-call `run_usage_ledger` row. That relation's `decision`
            # vocabulary is closed and enforced by a CHECK constraint
            # (reserved/settled/rejected/overage/released), and it has no
            # search columns, so a "search" row would be refused by the
            # database and would carry nothing if it were not. Search
            # consumption becomes durable the same way every other ledger
            # dimension does: `_record()` writes the whole ExecutionUsageLedger
            # snapshot, whose jsonb already carries `search_invocations` and
            # `search_cost` and merges them component-wise.
            self._record()
            if charged > held:
                raise self._stop(
                    "SEARCH_MULTIPLICITY_EXCEEDED",
                    "a provider request performed more searches than the reviewed maximum",
                    "budget_exhausted", "budget_exhausted")
            if (cfg.max_search_invocations_per_run is not None
                    and self.search_invocations > cfg.max_search_invocations_per_run):
                raise self._reject("SEARCH_LIMIT_EXCEEDED", "search invocation limit exceeded",
                                   "budget_exhausted", "budget_exhausted")
            if cfg.max_cost_per_run is not None and self.actual_cost > cfg.max_cost_per_run:
                raise self._stop("COST_LIMIT_EXCEEDED", "actual cost limit exceeded",
                                 "budget_exhausted", "budget_exhausted")

    def record_search(self, cost: float | None = None) -> None:
        """ONE search that has definitely happened, reserved and settled.

        The standalone-search path, and the historical spelling. It is
        expressed in terms of the reservation mechanism rather than beside
        it, so there is exactly one way search capacity is consumed.
        """
        self.settle_search(self.reserve_search(1), actual=1, cost=cost)

    def record_replan(self, *, correction: bool = False) -> None:
        """One Commander replan was ACCEPTED; a correction round is one too."""
        with self._lock:
            self.replans += 1
            if correction:
                self.correction_rounds += 1
            self._record()

    def note_missing_output_cap(self, model: str) -> None:
        """Record that a caller reached the gate without declaring a cap.

        It is not fatal -- the guarded client resolves an explicit server-owned
        cap so admission still has a number -- but it means some role is not
        declaring its own budget, and a silent fallback is how that stays
        unnoticed until it shows up as a truncated completion.
        """
        with self._lock:
            self.missing_output_cap_calls += 1
        self._emit("model_output_cap_missing",
                   {"message": "model call had no explicit output cap; a server cap was applied",
                    "payload": {"model": str(model or "")[:64],
                                "applied_cap": DEFAULT_OUTPUT_CAP}})

    def record_provider_backpressure(self) -> None:
        """Count a provider 429/backpressure event for telemetry only.

        Provider backpressure is paced and bounded by the shared provider
        scheduler; it must never consume the semantic retry allowance
        (max_retries), so this counter enforces no limit of its own."""
        with self._lock:
            self.provider_backpressure_events += 1
            self._record()


#: The ledger's own name for why a call settled without a response. One
#: mapping, derived from the ONE taxonomy, so the durable record and the
#: scheduler cannot describe the same attempt differently.
_SETTLEMENT_REASONS = {
    ProviderOutcome.RATE_LIMIT: "PROVIDER_RATE_LIMITED",
    ProviderOutcome.RETRYABLE_FAILURE: "PROVIDER_EXCEPTION",
    ProviderOutcome.NON_RETRYABLE_FAILURE: "PROVIDER_REFUSED",
    ProviderOutcome.TIMEOUT: "PROVIDER_DEADLINE_EXCEEDED",
    ProviderOutcome.CANCELLATION: "RUN_CANCELLED",
    ProviderOutcome.UNKNOWN: "PROVIDER_OUTCOME_UNKNOWN",
}


def _settlement_reason(verdict: ProviderVerdict) -> str:
    if verdict.is_quota_exhaustion:
        return "PROVIDER_QUOTA_EXHAUSTED"
    return _SETTLEMENT_REASONS.get(verdict.outcome, "PROVIDER_EXCEPTION")


class _GuardedCompletions:
    def __init__(self, inner: Any, tracker: BudgetTracker):
        self._inner = inner
        self._tracker = tracker

    def create(self, **kwargs: Any) -> Any:
        # FAIL CLOSED on price before anything else: a model with no
        # registered profile cannot be priced, so it cannot be held to any
        # dollar ceiling, and it is refused before admission, before the
        # provider and with a static code. It used to be priced at 0.0.
        try:
            profile = get_profile(kwargs.get("model", ""))
        except UnknownModelProfile:
            refusal = self._tracker.refuse_call(
                MODEL_PROFILE_UNKNOWN, "the model has no registered profile")
            refusal.provider_request_completed = True
            raise refusal from None
        estimated_input = estimate_message_tokens(kwargs.get("messages"))
        requested_max = read_output_cap(kwargs)
        if requested_max is None:
            # A caller that declared no cap is resolved to an explicit
            # server-owned number BEFORE admission rather than being handed the
            # whole remaining allowance. Every role is supposed to declare its
            # own cap, so this is a fail-safe, and it is announced instead of
            # being silent.
            requested_max = DEFAULT_OUTPUT_CAP
            self._tracker.note_missing_output_cap(kwargs.get("model", ""))
        # Hard pre-call gate: reserve capacity FIRST; the adapter is only
        # reached after the reservation succeeds, and the cap is clamped
        # to the remaining safe allowance. The reservation sequence travels
        # with this call so concurrent workers settle their own reservations.
        # MILO-side, BEFORE anything is sent. The scheduler settles the
        # organization concurrency permit on whether the provider request can
        # be proven finished, and an exception from here proves it never
        # started -- so it must be marked, or an ordinary budget refusal would
        # quarantine a shared slot it never used.
        try:
            call_seq, allowed_output = self._tracker.open_call(estimated_input, requested_max)
        except BaseException as exc:
            exc.provider_request_completed = True
            raise
        effective_cap = allowed_output if allowed_output is not None else requested_max
        reserved_output = effective_cap
        apply_output_cap(kwargs, effective_cap)
        try:
            response = self._inner.create(**kwargs)
        except Exception as exc:
            # THE SAME classification the scheduler settles on. It used to be
            # a different one, and the difference was a real defect: a 503
            # (`engine_overloaded_error`) was backpressure to the scheduler,
            # which paced and retried it, and a SEMANTIC failure to this
            # ledger, which charged it against `max_retries`. One provider
            # event was counted twice, and a run could die at
            # RETRY_LIMIT_REACHED without any model having misbehaved.
            verdict = classify_outcome(exc)
            try:
                self._tracker.settle_call(
                    estimated_input, reserved_output, 0, 0, 0.0, status="released",
                    rejection_reason=_settlement_reason(verdict),
                    call_seq=call_seq,
                )
            except BaseException as settlement:
                # Settling can itself refuse, and that refusal would REPLACE
                # the provider error the scheduler settles the concurrency
                # permit on. An accounting failure must not change what is
                # known about the request, so the original verdict is carried
                # across -- otherwise an ordinary 429 could start holding a
                # shared slot until a human reclaimed it.
                settlement.provider_request_completed = verdict.completion_proven
                raise
            if verdict.is_backpressure:
                # Provider backpressure -- 429 AND 503 alike -- is a
                # scheduling outcome, never a semantic model failure: it must
                # never consume the semantic retry allowance. The shared
                # provider scheduler bounds it.
                self._tracker.record_provider_backpressure()
            elif verdict.consumes_semantic_retry:
                self._tracker.record_retry()
            # Everything else -- a fired deadline, a cancellation, a hard
            # quota refusal, an outcome MILO cannot name -- is recorded as a
            # provider failure by `settle_call` above and charged to no
            # semantic allowance. A counter that fills up because the account
            # is out of money, or because MILO stopped waiting, tells nobody
            # anything true.
            raise
        usage = getattr(response, "usage", None)
        provider_cost = getattr(usage, "cost", None) or getattr(usage, "total_cost", None) or getattr(response, "cost", None)
        input_tokens = getattr(usage, "prompt_tokens", 0) or 0
        output_tokens = getattr(usage, "completion_tokens", 0) or 0
        if provider_cost is None:
            provider_cost = profile.usage_cost(input_tokens=input_tokens,
                                               output_tokens=output_tokens)
        # Likewise AFTER a response was read to completion: a budget refusal
        # raised here is about accounting, not about a request whose fate is
        # unknown, so it must not hold the permit either.
        try:
            self._tracker.settle_call(
                reserved_input_tokens=estimated_input,
                reserved_output_tokens=reserved_output,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost=float(provider_cost or 0),
                call_seq=call_seq,
            )
        except BaseException as exc:
            exc.provider_request_completed = True
            raise
        return response


class _GuardedChat:
    def __init__(self, inner: Any, tracker: BudgetTracker):
        self.completions = _GuardedCompletions(inner.completions, tracker)


class GuardedModelClient:
    """Wraps an OpenAI-compatible client so every chat completion is gated."""

    def __init__(self, inner: Any, tracker: BudgetTracker):
        self._inner = inner
        self.chat = _GuardedChat(inner.chat, tracker)


def resolved_request_deadline(deadline_seconds: float | None = None) -> float:
    """The total wall-clock deadline one provider request may take.

    Resolved from the organization quota configuration when not supplied, so a
    caller cannot accidentally build a client with no bound: an unbounded
    client is how one request wedges a worker for good. The value has already
    been validated by ``QuotaConfig``.

    This is a LIVENESS bound, not the concurrency one. The organization permit
    is settled on whether completion can be PROVEN -- see the ownership
    invariant in ``backend.provider_quota`` -- and a fired deadline is
    explicitly not such a proof.
    """
    if deadline_seconds is None:
        from backend.provider_quota import QuotaConfig

        deadline_seconds = QuotaConfig.from_env().request_deadline_seconds
    return float(deadline_seconds)


def provider_request_timeout(deadline_seconds: float | None = None) -> Any:
    """The httpx inactivity timeouts that accompany the total deadline.

    These alone are NOT the safety mechanism, and the distinction is the whole
    point: ``read`` bounds the gap between bytes, not the duration of the
    request. Measured on loopback, a server emitting one chunk every 0.2s ran
    for 30s under a 1.5s read timeout and stopped only because the SERVER gave
    up. The total bound comes from ``backend.provider_transport``.

    What these still do is cover the genuinely SILENT phase -- waiting for
    response headers while the provider computes -- which is exactly what an
    inactivity timeout bounds correctly. ``connect`` stays short: waiting
    minutes for a TCP handshake is never useful, and a slow connect should
    free the permit for someone else.
    """
    import httpx

    deadline = resolved_request_deadline(deadline_seconds)
    return httpx.Timeout(deadline, connect=min(10.0, deadline), read=deadline,
                         write=deadline, pool=deadline)


def build_provider_http_client(deadline_seconds: float | None = None) -> Any:
    """An httpx client that enforces a TOTAL deadline on every request."""
    from backend.provider_transport import build_deadline_http_client

    return build_deadline_http_client(resolved_request_deadline(deadline_seconds))


def build_guarded_client_factory(tracker: BudgetTracker, inner_factory: Callable[[str, str], Any] | None = None,
                                 request_deadline_seconds: float | None = None) -> Callable[[str, str], Any]:
    """Produce a model_client_factory enforcing the budget gate.

    Preserves the existing MILO engine behavior: the wrapped client is the
    unchanged production client; only the call boundary is guarded.
    """

    def factory(api_key: str, base_url: str) -> Any:
        if inner_factory is not None:
            inner = inner_factory(api_key, base_url)
        else:
            from openai import OpenAI

            # max_retries=0: see the note in vehicle_catalog_v1/core.py -- the
            # SDK's default of two silent retries would spend organization RPM
            # and concurrency that no MILO counter or shared limiter observes.
            #
            # http_client: the SDK's default read timeout is 600s, and a
            # read timeout would not have bounded the request anyway, because
            # it measures silence rather than duration. This client carries a
            # transport that enforces a TOTAL deadline, so a worker cannot
            # block on one request indefinitely.
            deadline = resolved_request_deadline(request_deadline_seconds)
            inner = OpenAI(api_key=api_key, base_url=base_url, max_retries=0,
                           http_client=build_provider_http_client(deadline),
                           timeout=provider_request_timeout(deadline))
        return GuardedModelClient(inner, tracker)

    return factory

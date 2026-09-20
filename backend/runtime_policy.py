"""The ONE canonical, machine-readable, server-owned runtime policy.

Why this module exists
----------------------

Before it, the same logical limit was written down in four or five places that
were maintained by hand and were free to disagree:

* ``backend/tier2_profile.py`` described the reviewed first paid run -- 23
  tasks, 56 agent steps, 24 tool calls, 1 replan, $3.00 -- but enforced
  nothing; it was a document;
* ``backend/budget.py`` enforced a run's model calls, tokens, cost and
  duration from the environment, and its mandatory-for-paid set named five of
  those dimensions, not the ones the reviewed profile advertised (neither
  ``max_agent_steps`` nor the recorded-cost cap was required);
* ``backend/engines/swarm_v2/validation.py`` admitted plans against
  ``PlanLimits`` defaults of 64 tasks, 3 replans and 100 tool calls, and
  ``from_envelope`` narrowed only ``max_tasks``, so production ran a reviewed
  "1 replan / 24 tool calls" profile with a firewall that admitted 3 and 100;
* ``scripts/release/stage-d/stage-d-env.sh`` kept its own hand-written copy of
  the whole envelope, which is how it came to pin
  ``MILO_PROVIDER_RPM_LIMIT=350`` against an organization ceiling of 80 --
  a value ``ProviderLimitsConfig.from_env`` refuses outright, so the pinned
  Stage D posture could not have started a worker at all.

Four surfaces describing four different effective safety envelopes is not a
set of bugs; it is one architectural defect. This module is the fix: every
enforceable limit of a run is declared ONCE, here, and every surface that
enforces, advertises, validates or verifies a limit derives it from this
declaration instead of restating it.

The shape of the declaration
----------------------------

Each dimension names:

* ``reviewed`` -- the value the controlled first paid run was reviewed and
  authorized at. This is a CEILING on what a deployment may configure, not a
  default a deployment inherits;
* ``runtime_default`` -- what the code actually uses when the deployment says
  nothing. This is what makes the mandatory set *derived* rather than
  declared: a dimension must be configured explicitly for paid execution
  exactly when leaving it out would let the runtime operate WIDER than the
  reviewed value. ``PlanLimits.max_replans`` defaults to 3 against a reviewed
  1, so it is mandatory; ``MILO_V1_TECHNICAL_PARALLELISM`` defaults to 1
  against a reviewed 4, so it is not. Nobody maintains that list by hand, so
  it cannot fall behind the profile again;
* ``direction`` -- which way "tighter" runs. Almost every dimension is an
  upper bound, so lower is tighter. ``estimated_cost_per_call`` is not: it is
  a RESERVATION rate, so a SMALLER value admits MORE calls before the
  estimated-cost ceiling trips, and getting its direction wrong would let a
  deployment widen its exposure while appearing to tighten it. The two
  provider backoff values are declared but bounded in neither direction: the
  number of real attempts and the maximum stall are already bounded by other
  dimensions, so a shorter backoff cannot widen any envelope and refusing one
  would only stop a legitimate deployment from pacing itself differently.

The rule, stated once
---------------------

A deployment may TIGHTEN any reviewed limit. It may never widen one
SILENTLY. An absent, unparseable, non-positive or wider-than-reviewed value in
the PAID posture is a refusal, never a fallback -- and the refusal happens at
configuration-validation time, before a run exists.

The rule binds exactly where money can be spent. An unpaid deployment cannot
make a provider call at all -- ``BudgetTracker``'s kill switch refuses every
one while ``MILO_ENABLE_PAID_EXECUTION`` is off -- so a wider value there is
RECORDED on the resolved policy (``relaxed``) rather than refused, and the
same configuration is refused the moment paid execution is armed. Narrowing a
zero-cost staging stack would protect nothing and break something.

Where the reviewed numbers come from
------------------------------------

Stage C Attempt 7 (run ``8b4a4277…``, terminal ``completed``, read read-only
from production on 2026-09-18) consumed 84 model calls, 277,882 input +
34,136 output tokens (312,018 in total), $0.252069 recorded, 32 agent steps,
934.235s, 0 retries and 0 backpressure events. The run-level caps are the smallest round value at
or above 1.75x that observation and never above the Stage C cap, with two
deliberate exceptions recorded on the dimensions themselves.

The provider and concurrency numbers come from the verified Kimi Tier 2
account evidence in ``backend/provider_quota.py`` and the V1 timeout post
mortem in ``backend/tier2_profile.py``: run ``3772fc84…`` timed out at 1800s
on throughput, not pacing, so the duration cap is NOT raised and a small
amount of real V1 parallelism is what changes instead.

What this module deliberately does NOT do
-----------------------------------------

It does not track consumption. It answers "what is the authoritative policy?"
and nothing else; how usage is accounted for durably against that policy, and
how a resume may never refund it, belongs to the execution usage ledger and is
a separate concern.

Import discipline: this module imports nothing from ``backend`` at module
scope, so every other configuration module can depend on it without a cycle.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

# The ONE spelling of "an operator turned this on" in the repository.
# `backend.production_config` and `backend.catalog.execution` re-export this
# so a flag cannot mean one thing to the validator and another to the worker.
TRUE_VALUES = frozenset({"1", "true", "yes", "on"})

#: Engine/workflow keys a policy can be resolved for. ``"*"`` means the
#: dimension binds whichever engine executes the run.
ENGINE_V1 = "vehicle_catalog_v1"
ENGINE_V2 = "swarm_v2"
ALL_ENGINES = (ENGINE_V1, ENGINE_V2)

# --- which way "tighter" runs ------------------------------------------------
LOWER_IS_TIGHTER = "lower_is_tighter"
HIGHER_IS_TIGHTER = "higher_is_tighter"
#: Declared and pinned, but not an exposure ceiling in either direction: a
#: pacing knob whose worst case is already bounded by OTHER dimensions. The
#: reviewed value is still what Stage D pins, so the reviewed posture stays
#: reproducible; a deployment may choose a different positive value without
#: that being a widening, because no envelope grows when it does.
DECLARED_NOT_BOUNDED = "declared_not_bounded"

#: The serialization contract of `RuntimePolicy.document()`. A consumer that
#: does not recognise it must refuse rather than guess.
POLICY_SCHEMA_VERSION = "milo-runtime-policy/1"

# --- how a dimension is spelled on the wire ---------------------------------
FMT_INT = "int"      # 1800, 240 -- printed without a decimal point
FMT_MONEY = "money"  # 3.00, 0.02 -- always two decimals

# --- the surfaces that consume a dimension ----------------------------------
BUDGET = "budget"                    # backend.budget.BudgetConfig
PLAN = "plan_limits"                 # swarm_v2.validation.PlanLimits
PROVIDER = "provider_limits"         # backend.provider_scheduler
ENGINE_PARALLELISM = "engine"        # V1 core / V2 executor
SEARCH = "search"                    # provider_quota search admission
POSTURE = "posture"                  # declared, enforced by operator process


class RuntimePolicyError(ValueError):
    """A runtime policy that cannot be honoured safely. Sanitized and bounded.

    The string form lists violation CODES only. Configured values never reach
    it: a refusal that echoes the configuration it refused is a log leak.
    """

    def __init__(self, violations: "Iterable[PolicyViolation]") -> None:
        self.violations = tuple(violations)
        self.codes = tuple(sorted({v.code for v in self.violations}))
        super().__init__(f"RUNTIME_POLICY_REFUSED[{','.join(self.codes)}]")


@dataclass(frozen=True)
class PolicyViolation:
    """One reason a resolved policy is refused. Message names no value."""

    code: str
    dimension: str
    message: str


@dataclass(frozen=True)
class PolicyDimension:
    """One enforceable limit of a run, declared exactly once."""

    name: str
    reviewed: int | float
    kind: type
    direction: str
    fmt: str
    env_key: str | None
    runtime_default: int | float | None
    enforced_by: str
    applies_to: tuple[str, ...]
    why: str

    @property
    def mandatory_for_paid(self) -> bool:
        """Must a paid deployment configure this dimension explicitly?

        DERIVED, never declared: a dimension is mandatory exactly when leaving
        it unset would let the runtime operate wider than the value the first
        paid run was reviewed at. That is what stops the mandatory set from
        drifting away from the profile it is supposed to guarantee.
        """
        if self.env_key is None:
            return False
        if self.runtime_default is None:
            return True
        return self.is_wider(self.runtime_default)

    def is_wider(self, value: int | float) -> bool:
        """Is ``value`` a LOOSER envelope than the reviewed one?"""
        if self.direction == DECLARED_NOT_BOUNDED:
            return False
        if self.direction == LOWER_IS_TIGHTER:
            return value > self.reviewed
        return value < self.reviewed

    def format(self, value: int | float | None) -> str:
        """The canonical wire spelling, so two surfaces cannot disagree."""
        if value is None:
            return "unbounded"
        if self.fmt == FMT_MONEY:
            return f"{float(value):.2f}"
        if float(value) != int(value):
            raise ValueError(f"{self.name} is declared integral")
        return str(int(value))

    @property
    def reviewed_text(self) -> str:
        return self.format(self.reviewed)


def _d(name: str, reviewed: int | float, *, kind: type = int,
       direction: str = LOWER_IS_TIGHTER, fmt: str = FMT_INT,
       env_key: str | None = None, runtime_default: int | float | None = None,
       enforced_by: str, applies_to: tuple[str, ...] = ALL_ENGINES,
       why: str) -> PolicyDimension:
    return PolicyDimension(name=name, reviewed=reviewed, kind=kind, direction=direction,
                           fmt=fmt, env_key=env_key, runtime_default=runtime_default,
                           enforced_by=enforced_by, applies_to=applies_to, why=why)


# =============================================================================
# THE REGISTRY. Every enforceable limit of a run, and nothing else.
# =============================================================================
POLICY_DIMENSIONS: tuple[PolicyDimension, ...] = (
    # --- run-level budget, enforced by backend.budget.BudgetTracker ---------
    _d("max_model_calls_per_run", 150, env_key="MILO_MAX_MODEL_CALLS_PER_RUN",
       enforced_by=BUDGET,
       why="1.75x the 84 calls Stage C Attempt 7 actually made, rounded down "
           "from the Stage C cap of 200"),
    _d("max_input_tokens_per_run", 500_000, env_key="MILO_MAX_INPUT_TOKENS_PER_RUN",
       enforced_by=BUDGET,
       why="1.75x the 277,882 input tokens Attempt 7 consumed"),
    _d("max_output_tokens_per_run", 120_000, env_key="MILO_MAX_OUTPUT_TOKENS_PER_RUN",
       enforced_by=BUDGET,
       why="3.5x rather than 1.75x the observed 34,136: output volume is the "
           "most variable dimension of the preserved pipeline and one small "
           "observation is a poor basis for a tight cap"),
    _d("max_total_tokens_per_run", 600_000, env_key="MILO_MAX_TOTAL_TOKENS_PER_RUN",
       enforced_by=BUDGET,
       why="deliberately below input+output (620,000) so the joint ceiling "
           "binds before either component does"),
    _d("max_estimated_cost_per_run", 3.00, kind=float, fmt=FMT_MONEY,
       env_key="MILO_MAX_ESTIMATED_COST_PER_RUN", enforced_by=BUDGET,
       why="exactly max_model_calls_per_run x estimated_cost_per_call, so the "
           "reservation ceiling admits the call cap and not one call more"),
    _d("max_cost_per_run", 1.00, kind=float, fmt=FMT_MONEY,
       env_key="MILO_MAX_COST_PER_RUN", enforced_by=BUDGET,
       why="the RECORDED-cost ceiling, tighter than the estimated one because "
           "the V1 evidence run cost $0.34 and no evidence says more is needed"),
    _d("max_run_duration_seconds", 1800, env_key="MILO_MAX_RUN_DURATION_SECONDS",
       enforced_by=BUDGET,
       why="NOT raised after run 3772fc84 timed out at 1800s: that timeout was "
           "throughput, not pacing, and the fix is V1 parallelism"),
    _d("max_agent_steps", 56, env_key="MILO_MAX_AGENT_STEPS", enforced_by=BUDGET,
       why="1.75x the 32 steps Attempt 7 used; every guarded gateway call "
           "records exactly one, so it is the tightest binding dimension on "
           "plan SHAPE"),
    _d("max_retries", 15, env_key="MILO_MAX_RETRIES", enforced_by=BUDGET,
       why="HELD, not tightened: Attempt 6 failed at RETRY_LIMIT_REACHED and 15 "
           "together with the provider envelope below is the pair that produced "
           "the successful Attempt 7"),
    _d("max_concurrent_runs_per_user", 1, env_key="MILO_MAX_CONCURRENT_RUNS_PER_USER",
       enforced_by=BUDGET,
       why="one controlled paid run means one, per user as well as in total"),
    _d("max_concurrent_runs_per_project", 1, env_key="MILO_MAX_CONCURRENT_RUNS_PER_PROJECT",
       enforced_by=BUDGET,
       why="one controlled paid run means one, per project as well as in total"),
    _d("daily_user_budget", 4.00, kind=float, fmt=FMT_MONEY,
       env_key="MILO_DAILY_USER_BUDGET", enforced_by=BUDGET,
       why="above the 3.00 reservation ceiling so a daily budget never fails "
           "the run before the per-run cap does"),
    _d("daily_project_budget", 4.00, kind=float, fmt=FMT_MONEY,
       env_key="MILO_DAILY_PROJECT_BUDGET", enforced_by=BUDGET,
       why="above the 3.00 reservation ceiling so a daily budget never fails "
           "the run before the per-run cap does"),
    _d("estimated_cost_per_call", 0.02, kind=float, direction=HIGHER_IS_TIGHTER,
       fmt=FMT_MONEY, env_key="MILO_ESTIMATED_COST_PER_CALL", runtime_default=0.05,
       enforced_by=BUDGET,
       why="a RESERVATION rate, not a ceiling: a SMALLER value admits MORE "
           "calls before the estimated-cost cap trips, so only raising it is a "
           "tightening"),

    # --- plan shape, enforced by the deterministic PlanValidator ------------
    _d("max_tasks", 23, env_key="MILO_MAX_TASKS_PER_RUN", runtime_default=64,
       enforced_by=PLAN, applies_to=(ENGINE_V2,),
       why="the largest plan the 56-agent-step envelope can finish under the "
           "worst case; the firewall used to admit 64 into it"),
    _d("max_tool_calls", 24, env_key="MILO_MAX_TOOL_CALLS_PER_RUN", runtime_default=100,
       enforced_by=PLAN, applies_to=(ENGINE_V2,),
       why="the aggregate planned-call ceiling for the run; the firewall used "
           "to admit 100 into a profile that advertised 24"),
    _d("max_replans", 1, env_key="MILO_MAX_REPLANS_PER_RUN", runtime_default=3,
       enforced_by=PLAN, applies_to=(ENGINE_V2,),
       why="one bounded replan, which the correction round also consumes; the "
           "firewall used to admit 3 into a profile that advertised 1"),
    _d("max_tool_calls_per_task", 4, runtime_default=4, enforced_by=PLAN,
       applies_to=(ENGINE_V2,),
       why="a low fixed per-task ceiling charged against the EXACT planned "
           "call list, independent of the aggregate ceiling"),
    _d("max_graph_depth", 12, runtime_default=12, enforced_by=PLAN,
       applies_to=(ENGINE_V2,), why="bounds dependency chaining"),
    _d("max_recursion_depth", 4, runtime_default=4, enforced_by=PLAN,
       applies_to=(ENGINE_V2,), why="bounds task self-similarity"),
    _d("max_cost_units", 100_000, runtime_default=100_000, enforced_by=PLAN,
       applies_to=(ENGINE_V2,),
       why="the plan's own declared cost units; not model-call slots"),

    # --- provider admission, enforced by ProviderLimitsConfig/the coordinator
    _d("provider_max_concurrency", 2, env_key="MILO_PROVIDER_MAX_CONCURRENCY",
       runtime_default=2, enforced_by=PROVIDER,
       why="the proven Attempt 7 value and the preserved V1 engine limit "
           "(core.MAX_PARALLEL_KIMI_CALLS); production drifted to 8"),
    _d("provider_rpm_limit", 40, env_key="MILO_PROVIDER_RPM_LIMIT", runtime_default=3,
       enforced_by=PROVIDER,
       why="half the organization ceiling of 80. Stage D pinned 350 here, "
           "which assert_within_organization_ceiling refuses outright -- the "
           "pinned posture could not have started a worker"),
    _d("provider_tpm_limit", 1_200_000, env_key="MILO_PROVIDER_TPM_LIMIT",
       enforced_by=PROVIDER,
       why="half the organization ceiling of 2,400,000; approaching a ceiling "
           "is not a goal"),
    _d("provider_max_rate_limit_retries", 5,
       env_key="MILO_PROVIDER_MAX_RATE_LIMIT_RETRIES", runtime_default=5,
       enforced_by=PROVIDER,
       why="so one semantic call is at most 1+5 real provider attempts, each "
           "of which re-enters the shared organization gate"),
    _d("provider_max_backpressure_wait_seconds", 240, kind=float,
       env_key="MILO_PROVIDER_MAX_BACKPRESSURE_WAIT_SECONDS", runtime_default=240.0,
       enforced_by=PROVIDER,
       why="a bounded stall: past it the task fails rather than silently "
           "consuming the run's duration"),
    _d("provider_backoff_base_seconds", 2, kind=float, direction=DECLARED_NOT_BOUNDED,
       env_key="MILO_PROVIDER_BACKOFF_BASE_SECONDS", runtime_default=2.0,
       enforced_by=PROVIDER,
       why="pacing, not exposure: the number of real attempts is already "
           "bounded by provider_max_rate_limit_retries and the stall by "
           "provider_max_backpressure_wait_seconds, and every attempt is "
           "re-admitted by the organization gate whatever the backoff, so a "
           "shorter one cannot widen any envelope"),
    _d("provider_backoff_max_seconds", 30, kind=float, direction=DECLARED_NOT_BOUNDED,
       env_key="MILO_PROVIDER_BACKOFF_MAX_SECONDS", runtime_default=30.0,
       enforced_by=PROVIDER,
       why="pacing, not exposure, for the same reason as the backoff base"),

    # --- engine parallelism -------------------------------------------------
    _d("v1_technical_parallelism", 4, env_key="MILO_V1_TECHNICAL_PARALLELISM",
       runtime_default=1, enforced_by=ENGINE_PARALLELISM, applies_to=(ENGINE_V1,),
       why="4 brings the technical phase that cost run 3772fc84 its completion "
           "from ~1621s to ~405s; it is not 32 because provider latency, not "
           "MILO's ceiling, is what the run waits on"),
    _d("v2_max_active_workers", 2, env_key="MILO_SWARM_MAX_ACTIVE_WORKERS",
       runtime_default=4, enforced_by=ENGINE_PARALLELISM, applies_to=(ENGINE_V2,),
       why="a queueing width, clamped to real provider capacity anyway; more "
           "logical workers than provider slots only burn run duration"),

    # --- standalone search endpoints ---------------------------------------
    _d("search_basic_qps", 1, runtime_default=1, enforced_by=SEARCH,
       why="CONSERVATIVE FALLBACK: the exact Tier 2 Web Search QPS was not "
           "recoverable from the official tier table and is not invented"),
    _d("search_pro_qps", 1, runtime_default=1, enforced_by=SEARCH,
       why="CONSERVATIVE FALLBACK: the exact Tier 2 Web Search QPS was not "
           "recoverable from the official tier table and is not invented"),

    # --- declared posture, enforced by operator process and Stage D --------
    _d("hard_monetary_cap_usd", 3.00, kind=float, fmt=FMT_MONEY, runtime_default=3.00,
       enforced_by=POSTURE,
       why="the money ceiling no per-run cap may exceed; kept well under $10"),
    _d("first_paid_run_execution_cap", 1, runtime_default=1, enforced_by=POSTURE,
       why="ONE authorized paid worker execution, and no automatic relaunch "
           "after a terminal failure"),
)

DIMENSIONS: Mapping[str, PolicyDimension] = {d.name: d for d in POLICY_DIMENSIONS}
POLICY_ENV_KEYS: Mapping[str, str] = {d.name: d.env_key for d in POLICY_DIMENSIONS
                                      if d.env_key is not None}
MANDATORY_FOR_PAID_EXECUTION: tuple[str, ...] = tuple(
    d.name for d in POLICY_DIMENSIONS if d.mandatory_for_paid)

# The dimensions each enforcement surface owns, so a surface can ask the
# registry what belongs to it rather than restating a list.
def dimensions_for(surface: str) -> tuple[PolicyDimension, ...]:
    return tuple(d for d in POLICY_DIMENSIONS if d.enforced_by == surface)


# --- catalog posture: a declared dimension with a coherence rule ------------
CATALOG_MASTER_FLAG = "MILO_ENABLE_CATALOG_EXECUTION"
CATALOG_READ_FLAG = "MILO_ENABLE_GOVERNMENT_CATALOG_READ"
CATALOG_PROMOTION_FLAG = "MILO_ENABLE_CATALOG_PROMOTION"
PAID_EXECUTION_FLAG = "MILO_ENABLE_PAID_EXECUTION"

#: The reviewed first-run catalog posture: everything off. Arming the catalog
#: is a separate, explicitly authorized operator decision, so a deployment
#: that turns it on is RECORDED rather than refused -- but an INCOHERENT
#: combination is refused wherever it is found.
REVIEWED_CATALOG_POSTURE = {"master": False, "government_read": False, "promotion": False}


def _flag(env: Mapping[str, str], name: str) -> bool:
    return (env.get(name) or "").strip().lower() in TRUE_VALUES


def paid_posture(env: Mapping[str, str]) -> bool:
    """Is paid execution armed? The same reading `backend.budget` uses."""
    return _flag(env, PAID_EXECUTION_FLAG)


def catalog_posture_violations(env: Mapping[str, str]) -> list[PolicyViolation]:
    """Catalog combinations that cannot be honoured, found at STARTUP.

    Promotion without read is the one that matters: a pipeline armed to write
    canonical facts about data the run is not allowed to READ is worse than a
    misconfiguration that fails closed, and there is no deployment that wants
    it. It used to surface only when the worker built its engine -- after a
    run had been created and a lease acquired -- so the contradiction was
    discovered by a run rather than by validation.

    The master kill switch is deliberately NOT part of the rule. Turning it
    off while capabilities stay armed is exactly what a kill switch is for --
    and not reading it here is also what keeps the V1 invariant intact: the
    V1 engine path never consults `catalog_execution_enabled`, and refusing a
    deployment whose catalog configuration CONTRADICTS ITSELF is a
    whole-deployment refusal, not V1 behaviour that depends on the switch.
    """
    if _flag(env, CATALOG_PROMOTION_FLAG) and not _flag(env, CATALOG_READ_FLAG):
        return [PolicyViolation(
            "POLICY_CATALOG_POSTURE_CONTRADICTORY", "catalog_posture",
            f"{CATALOG_PROMOTION_FLAG} requires {CATALOG_READ_FLAG}: canonical "
            "promotion may never be armed for data this deployment is not "
            "allowed to read")]
    return []


# =============================================================================
# Resolution
# =============================================================================

def _parse(dimension: PolicyDimension, raw: str) -> int | float:
    text = raw.strip()
    if dimension.kind is int:
        value: int | float = int(text)          # raises ValueError on garbage
    else:
        value = float(text)
        if not math.isfinite(value):
            raise ValueError("not finite")
    if value <= 0:
        raise ValueError("not positive")
    return value


@dataclass(frozen=True)
class RuntimePolicy:
    """The resolved, enforceable envelope of a run. Immutable and serializable."""

    engine: str
    paid: bool
    values: Mapping[str, int | float | None]
    #: dimension -> "deployment" | "runtime_default" | "unbounded" |
    #: "reviewed" | "refused" (the last only on a policy that was rejected)
    sources: Mapping[str, str]
    #: The REVIEWED catalog posture, which is all-off. A run's EFFECTIVE
    #: posture is resolved by `backend.catalog.execution.catalog_posture` and
    #: only the Swarm V2 factory asks for it -- deliberately, because the V1
    #: engine path must never consult the catalog switch at all. This module
    #: owns which catalog COMBINATIONS are legal, not what the posture is.
    reviewed_catalog_posture: Mapping[str, bool]
    tightened: tuple[str, ...]       # dimensions a deployment tightened
    #: Dimensions an UNPAID deployment set WIDER than the reviewed value.
    #: Recorded, never silent -- and never reachable in the paid posture,
    #: where the same value is a refusal.
    relaxed: tuple[str, ...]

    # --- reading ---------------------------------------------------------
    def __getitem__(self, name: str) -> int | float | None:
        return self.values[name]

    def get(self, name: str) -> int | float | None:
        """The resolved value, or None when the dimension is unbounded."""
        return self.values[name]

    def configured(self, name: str) -> bool:
        """Did the DEPLOYMENT set this, as opposed to inheriting a default?"""
        return self.sources.get(name) == "deployment"

    # --- the enforcement surfaces ---------------------------------------
    def budget_config(self, *, env: Mapping[str, str] | None = None) -> Any:
        """The canonical `BudgetConfig`.

        In the paid posture every budget dimension comes from this policy, so
        a generic default can never widen the reviewed envelope. In the unpaid
        posture -- where `BudgetTracker`'s kill switch makes a paid call
        impossible -- the environment is read exactly as it always was, so a
        development or test deployment is not retroactively given limits it
        never had.
        """
        from backend.budget import BudgetConfig

        if not self.paid:
            return BudgetConfig.from_env(dict(env) if env is not None else None)
        fields: dict[str, Any] = {}
        for dimension in dimensions_for(BUDGET):
            value = self.values[dimension.name]
            fields[dimension.name] = (None if value is None else
                                      int(value) if dimension.kind is int
                                      else float(value))
        # `estimated_cost_per_call` is a rate with a real code default rather
        # than an optional ceiling, so it is never None on the dataclass.
        if fields.get("estimated_cost_per_call") is None:
            fields.pop("estimated_cost_per_call")
        return BudgetConfig(**fields)

    def plan_limits(self, *, base: Any | None = None) -> Any:
        """The canonical `PlanLimits`, ONE instance for every V2 surface.

        The same object feeds the provider-visible planning policy, the
        deterministic `PlanValidator`, the feasibility check and execution, so
        "what the model was told" and "what the firewall enforces" cannot
        drift.

        It is narrowed twice and widened never: first to the reviewed plan
        shape, then to whatever this run's agent-step and model-call envelope
        can actually pay for.
        """
        from backend.engines.swarm_v2.validation import PlanLimits

        reference = base if base is not None else PlanLimits()
        budget = self.budget_config()
        if not self.paid:
            # Unpaid runs cannot spend: the legacy ceiling still applies, and
            # nothing here retroactively narrows a deployment that never had a
            # policy to begin with.
            return PlanLimits.from_envelope(
                max_agent_steps=budget.max_agent_steps,
                max_model_calls=budget.max_model_calls_per_run,
                base=reference)
        def shape(name: str) -> int | None:
            value = self.values[name]
            return None if value is None else int(value)

        return PlanLimits.from_envelope(
            max_agent_steps=budget.max_agent_steps,
            max_model_calls=budget.max_model_calls_per_run,
            max_tool_calls=shape("max_tool_calls"),
            max_tasks=shape("max_tasks"),
            max_replans=shape("max_replans"),
            max_tool_calls_per_task=shape("max_tool_calls_per_task"),
            max_graph_depth=shape("max_graph_depth"),
            max_recursion_depth=shape("max_recursion_depth"),
            max_cost_units=shape("max_cost_units"),
            base=reference)

    def provider_limits(self, *, env: Mapping[str, str] | None = None) -> Any:
        """The canonical `ProviderLimitsConfig`."""
        from backend.provider_scheduler import ProviderLimitsConfig

        if not self.paid:
            return ProviderLimitsConfig.from_env(dict(env) if env is not None else None)
        def opt_int(name: str) -> int | None:
            value = self.values[name]
            return None if value is None else int(value)

        defaults = ProviderLimitsConfig()
        resolved = ProviderLimitsConfig(
            max_concurrency=opt_int("provider_max_concurrency") or defaults.max_concurrency,
            rpm_limit=opt_int("provider_rpm_limit"),
            tpm_limit=opt_int("provider_tpm_limit"),
            max_rate_limit_retries=(opt_int("provider_max_rate_limit_retries")
                                    if self.values["provider_max_rate_limit_retries"] is not None
                                    else defaults.max_rate_limit_retries),
            max_backpressure_wait_seconds=float(
                self.values["provider_max_backpressure_wait_seconds"]
                if self.values["provider_max_backpressure_wait_seconds"] is not None
                else defaults.max_backpressure_wait_seconds),
            backoff_base_seconds=float(self.values["provider_backoff_base_seconds"]
                                       if self.values["provider_backoff_base_seconds"] is not None
                                       else defaults.backoff_base_seconds),
            backoff_max_seconds=float(self.values["provider_backoff_max_seconds"]
                                      if self.values["provider_backoff_max_seconds"] is not None
                                      else defaults.backoff_max_seconds))
        resolved.assert_within_organization_ceiling()
        return resolved

    @property
    def swarm_max_active_workers(self) -> int:
        return int(self.values["v2_max_active_workers"] or
                   DIMENSIONS["v2_max_active_workers"].reviewed)

    @property
    def v1_technical_parallelism(self) -> int:
        return int(self.values["v1_technical_parallelism"] or
                   DIMENSIONS["v1_technical_parallelism"].reviewed)

    @property
    def max_provider_attempts_per_call(self) -> int:
        return 1 + int(self.values["provider_max_rate_limit_retries"] or
                       DIMENSIONS["provider_max_rate_limit_retries"].reviewed)

    # --- deterministic serialization ------------------------------------
    def document(self) -> dict[str, Any]:
        """The whole policy as ONE deterministic, serializable document.

        This is what Stage D verifies against. It is generated, never
        transcribed: a second hand-written copy of an envelope is the defect
        this module exists to remove.
        """
        return {
            "schema_version": POLICY_SCHEMA_VERSION,
            "engine": self.engine,
            "paid": self.paid,
            "reviewed_catalog_posture": dict(sorted(self.reviewed_catalog_posture.items())),
            "dimensions": {
                d.name: {
                    "value": DIMENSIONS[d.name].format(self.values[d.name]),
                    "reviewed": d.reviewed_text,
                    "env_key": d.env_key,
                    "source": self.sources[d.name],
                    "direction": d.direction,
                    "mandatory_for_paid": d.mandatory_for_paid,
                    "enforced_by": d.enforced_by,
                    "applies_to": list(d.applies_to),
                }
                for d in POLICY_DIMENSIONS
            },
            "tightened_by_deployment": list(self.tightened),
            "relaxed_by_unpaid_deployment": list(self.relaxed),
        }

    def serialize(self) -> str:
        return json.dumps(self.document(), sort_keys=True, separators=(",", ":"))

    def fingerprint(self) -> str:
        """A stable digest of the whole envelope. Two surfaces that print the
        same fingerprint are provably talking about the same policy."""
        return hashlib.sha256(self.serialize().encode("utf-8")).hexdigest()

    def env_expectations(self, *, prefixes: tuple[str, ...] | None = None) -> dict[str, str]:
        """The exact environment a deployment must carry to BE this policy.

        Stage D pins and verifies this mapping instead of keeping its own
        transcription of the same numbers.
        """
        return {d.env_key: d.format(self.values[d.name])
                for d in POLICY_DIMENSIONS
                if d.env_key is not None
                and (prefixes is None or d.env_key.startswith(prefixes))}


#: Env prefixes, so a verifier can sweep for an unpinned policy variable
#: rather than trusting a hand-kept list of names.
CAP_ENV_PREFIXES = ("MILO_MAX_", "MILO_DAILY_", "MILO_ESTIMATED_COST")
PROVIDER_ENV_PREFIXES = ("MILO_PROVIDER_",)
ENGINE_ENV_PREFIXES = ("MILO_SWARM_MAX_", "MILO_V1_")


def policy_failure_code(error: "RuntimePolicyError") -> str:
    """The operator-facing code for a refused policy, named by SURFACE.

    A refusal that names only the provider envelope is still
    `PROVIDER_LIMITS_CONFIG_INVALID`, and one that names only the run budget
    is still `BUDGET_CONFIG_INVALID`: consolidating the AUTHORITY should not
    rename the failures operators and runbooks already know. Anything that
    spans surfaces, or is not about a single dimension at all, is a policy
    failure and says so.
    """
    surfaces = {DIMENSIONS[violation.dimension].enforced_by
                for violation in error.violations
                if violation.dimension in DIMENSIONS}
    if surfaces == {PROVIDER}:
        return "PROVIDER_LIMITS_CONFIG_INVALID"
    if surfaces == {BUDGET}:
        return "BUDGET_CONFIG_INVALID"
    return "RUNTIME_POLICY_INVALID"


def policy_violations(env: Mapping[str, str] | None = None, *,
                      engine: str = "*", paid: bool | None = None) -> list[PolicyViolation]:
    """Every reason this environment is not a legal runtime policy.

    Non-raising, so configuration validation can report all of them at once.
    """
    return _resolve(env, engine=engine, paid=paid)[1]


def resolve_runtime_policy(env: Mapping[str, str] | None = None, *,
                           engine: str = "*", paid: bool | None = None) -> RuntimePolicy:
    """The canonical policy for this deployment, or a refusal.

    Fails closed: an absent mandatory dimension, an unparseable or
    non-positive value, a value wider than the reviewed envelope, a broken
    cross-dimension invariant or a contradictory catalog posture all refuse in
    the paid posture rather than resolving to something plausible.
    """
    policy, violations = _resolve(env, engine=engine, paid=paid)
    if violations:
        raise RuntimePolicyError(violations)
    return policy


def _resolve(env: Mapping[str, str] | None,
             *, engine: str, paid: bool | None) -> tuple[RuntimePolicy, list[PolicyViolation]]:
    source = dict(os.environ if env is None else env)
    is_paid = paid_posture(source) if paid is None else bool(paid)
    violations: list[PolicyViolation] = []
    values: dict[str, int | float | None] = {}
    sources: dict[str, str] = {}
    tightened: list[str] = []
    relaxed: list[str] = []

    for dimension in POLICY_DIMENSIONS:
        raw = (source.get(dimension.env_key) or "").strip() if dimension.env_key else ""
        if raw:
            try:
                value = _parse(dimension, raw)
            except (ValueError, TypeError):
                violations.append(PolicyViolation(
                    "POLICY_VALUE_INVALID", dimension.name,
                    f"{dimension.env_key} is not a positive "
                    f"{dimension.kind.__name__} (value not shown)"))
                values[dimension.name] = dimension.reviewed
                sources[dimension.name] = "refused"
                continue
            sources[dimension.name] = "deployment"
            if dimension.is_wider(value):
                if is_paid:
                    violations.append(PolicyViolation(
                        "POLICY_WIDER_THAN_REVIEWED", dimension.name,
                        f"{dimension.env_key} is wider than the reviewed "
                        f"{dimension.reviewed_text}; a deployment may tighten a "
                        "reviewed limit and may never widen one"))
                    value = dimension.reviewed
                    sources[dimension.name] = "refused"
                else:
                    # An unpaid deployment cannot spend: the paid-execution
                    # kill switch refuses every provider call while it is off,
                    # so a wider value here buys nothing and costs nothing.
                    # It is RECORDED rather than refused -- the rule is that a
                    # deployment may never widen SILENTLY -- and arming paid
                    # execution on the same configuration is a refusal.
                    relaxed.append(dimension.name)
            elif value != dimension.reviewed:
                tightened.append(dimension.name)
            values[dimension.name] = value
            continue
        # Nothing configured: the runtime default is what would really apply.
        if dimension.mandatory_for_paid and is_paid:
            violations.append(PolicyViolation(
                "POLICY_DIMENSION_ABSENT", dimension.name,
                f"{dimension.env_key} is required for paid execution: without "
                f"it the runtime operates wider than the reviewed "
                f"{dimension.reviewed_text}"))
            values[dimension.name] = dimension.reviewed
            sources[dimension.name] = "refused"
            continue
        # `None` is the honest answer when nothing is configured and the code
        # has no default: the dimension really is UNBOUNDED at runtime. Writing
        # the reviewed value here instead would make the document claim a limit
        # the runtime does not enforce, which is the exact class of defect this
        # module exists to remove.
        fallback = dimension.runtime_default
        values[dimension.name] = fallback
        sources[dimension.name] = ("runtime_default" if fallback is not None
                                   else "unbounded")
        if fallback is not None and fallback != dimension.reviewed:
            tightened.append(dimension.name)

    violations.extend(catalog_posture_violations(source))
    if is_paid:
        # The cross-dimension invariants describe the PAID envelope. In the
        # unpaid posture the budget dimensions are genuinely unbounded unless
        # a deployment set them, so there is no combination to be inconsistent
        # about. The registry's own self-consistency is checked here too: it
        # is a property of the shipped code, so a deployment that tightens
        # correctly still refuses if the reviewed envelope itself is broken.
        violations.extend(_invariant_violations(values))
        violations.extend(reviewed_policy_violations())

    policy = RuntimePolicy(engine=engine, paid=is_paid, values=values, sources=sources,
                           reviewed_catalog_posture=dict(REVIEWED_CATALOG_POSTURE),
                           tightened=tuple(sorted(tightened)),
                           relaxed=tuple(sorted(relaxed)))
    return policy, violations


#: The Cloud Run worker job's own `--task-timeout`. The cooperative run
#: duration cap must stay strictly below it, or the process is killed before
#: MILO can record a terminal state.
WORKER_JOB_TIMEOUT_SECONDS = 3600


def _invariant_violations(values: Mapping[str, int | float | None]) -> list[PolicyViolation]:
    """Cross-dimension rules a DEPLOYMENT can break by being unsafe.

    Deliberately short. A tightening must never be refused, so a relationship
    that merely describes the SHAPE of the reviewed envelope -- "the joint
    token ceiling binds before input+output", "the plan ceiling fits the
    agent-step budget" -- does not belong here: a deployment that tightens one
    side of it is safer, not broken, and the runtime already narrows the plan
    ceiling to whatever envelope exists. Those relationships are checked
    against the REVIEWED values instead, by `reviewed_policy_violations`.

    What remains is what a deployment can get genuinely wrong: authorizing
    more money than the hard cap allows, and setting a cooperative run
    duration the worker process will not survive to enforce.
    """
    out: list[PolicyViolation] = []

    def fail(dimension: str, message: str) -> None:
        out.append(PolicyViolation("POLICY_INVARIANT_VIOLATED", dimension, message))

    hard_cap = values.get("hard_monetary_cap_usd")
    if hard_cap is not None:
        for name in ("max_estimated_cost_per_run", "max_cost_per_run"):
            value = values.get(name)
            if value is not None and float(value) > float(hard_cap):
                fail(name, f"{name} exceeds the hard monetary cap")
    duration = values.get("max_run_duration_seconds")
    if duration is not None and int(duration) >= WORKER_JOB_TIMEOUT_SECONDS:
        fail("max_run_duration_seconds",
             "the cooperative run duration cap must stay below the worker job "
             "timeout, or the process is killed before it can record a terminal state")
    return out


def reviewed_policy_violations() -> list[PolicyViolation]:
    """Is the REVIEWED registry internally consistent with itself?

    This is a property of the code this release ships, not of any deployment,
    so it is checked wherever the policy is validated and it can never refuse
    a deployment for tightening something. It is the check that would have
    caught a reviewed profile advertising 64-task plans inside a 56-agent-step
    budget, or a reviewed provider RPM above MILO's own organization ceiling.
    """
    values = {d.name: d.reviewed for d in POLICY_DIMENSIONS}
    out = _invariant_violations(values)

    def fail(dimension: str, message: str) -> None:
        out.append(PolicyViolation("POLICY_REVIEWED_INCONSISTENT", dimension, message))

    if int(values["max_total_tokens_per_run"]) > (int(values["max_input_tokens_per_run"]) +
                                                  int(values["max_output_tokens_per_run"])):
        fail("max_total_tokens_per_run",
             "the reviewed joint token ceiling must bind before input+output can")
    # The reviewed plan ceiling must be a shape the reviewed envelope can
    # actually finish. This is the exact relationship that let a 64-task plan
    # pass a firewall inside a 56-agent-step budget and then trip
    # AGENT_STEP_LIMIT_REACHED mid-run, after real money had been spent.
    from backend.engines.swarm_v2.feasibility import plan_worst_case

    worst = plan_worst_case(int(values["max_tasks"]), max_replans=int(values["max_replans"]))
    if worst.agent_steps > int(values["max_agent_steps"]):
        fail("max_tasks", "the reviewed plan ceiling's worst case needs more agent "
                          "steps than the reviewed envelope allows")
    if worst.model_calls > int(values["max_model_calls_per_run"]):
        fail("max_tasks", "the reviewed plan ceiling's worst case needs more model "
                          "calls than the reviewed envelope allows")
    # The reviewed provider envelope must be one the organization ceiling
    # admits: a reviewed value above it is refused by ProviderLimitsConfig at
    # worker startup, which is how a release toolkit came to pin a posture no
    # worker could have started under.
    from backend.provider_quota import MAX_INFERENCE_CONCURRENCY, MAX_RPM, MAX_TPM

    for name, ceiling in (("provider_max_concurrency", MAX_INFERENCE_CONCURRENCY),
                          ("provider_rpm_limit", MAX_RPM),
                          ("provider_tpm_limit", MAX_TPM)):
        if ceiling is not None and int(values[name]) > ceiling:
            fail(name, f"{name} exceeds MILO's organization ceiling, so the runtime "
                       "would refuse to start under the reviewed envelope")
    return out


def reviewed_first_run_policy(engine: str = "*") -> RuntimePolicy:
    """The reviewed envelope itself: every dimension at its authorized value.

    This is the document `backend.tier2_profile` publishes and the envelope
    Stage D pins. It is computed from the registry, so there is no second copy
    of it anywhere.
    """
    values = {d.name: d.reviewed for d in POLICY_DIMENSIONS}
    return RuntimePolicy(
        engine=engine, paid=True, values=values,
        sources={d.name: "reviewed" for d in POLICY_DIMENSIONS},
        reviewed_catalog_posture=dict(REVIEWED_CATALOG_POSTURE), tightened=(), relaxed=())


__all__ = [
    "ALL_ENGINES", "BUDGET", "CAP_ENV_PREFIXES", "DIMENSIONS", "ENGINE_ENV_PREFIXES",
    "ENGINE_PARALLELISM", "ENGINE_V1", "ENGINE_V2", "HIGHER_IS_TIGHTER",
    "DECLARED_NOT_BOUNDED", "LOWER_IS_TIGHTER", "MANDATORY_FOR_PAID_EXECUTION",
    "PLAN", "POLICY_DIMENSIONS",
    "POLICY_ENV_KEYS", "POLICY_SCHEMA_VERSION", "PROVIDER", "PROVIDER_ENV_PREFIXES",
    "POSTURE", "PolicyDimension", "PolicyViolation", "RuntimePolicy",
    "RuntimePolicyError", "SEARCH", "TRUE_VALUES", "WORKER_JOB_TIMEOUT_SECONDS",
    "catalog_posture_violations", "dimensions_for", "paid_posture",
    "REVIEWED_CATALOG_POSTURE", "policy_failure_code", "policy_violations",
    "resolve_runtime_policy",
    "reviewed_first_run_policy", "reviewed_policy_violations",
]

"""The ExecutionUsageLedger: ONE cumulative, monotonic usage record per run.

A run consumes capacity in many dimensions -- provider requests, tokens,
cost, semantic retries, backpressure events, agent steps, tool calls, task
executions, search invocations, replans and correction rounds -- and it may
be executed by more than one worker process over its lifetime: a crash, a
Cloud Run retry, a lease reclaim by a replacement worker, a V1 replay from
phase state or a V2 resume from a checkpoint. Every one of those is a chance
for the run to be handed back capacity it already spent.

This module is the single description of what the run's durable usage IS:

* :data:`LEDGER_COUNTERS` and :data:`LEDGER_AMOUNTS` are the cumulative
  dimensions. EVERY one of them only ever grows while a run executes. That is
  what makes a component-wise maximum of two records of the same run safe --
  it can never hand back capacity a record already shows as spent -- and it is
  the invariant the database enforces too (``merge_execution_usage`` and the
  ``run_execution_usage`` monotonic trigger, migration
  ``20260920000100_execution_usage_ledger.sql``).
* :func:`merge_usage_snapshots` is that maximum, applied to any number of
  durable records of one run (``runs.usage``, a checkpoint's ``token_usage``,
  the ``run_execution_usage`` row). It is what a resuming worker restores
  from, so the restored value is never lower than ANY durable source.
* :func:`public_usage_projection` is the bounded browser-facing view
  (``backend.schemas.RunUsage``): the ledger carries more dimensions than the
  public contract, and only the public ones are ever written to
  ``runs.usage`` or returned by ``GET /runs/{id}``.
* :func:`remaining_capacity` states what a run still has against a
  ``BudgetConfig`` -- the number the resume invariant is stated over:

      remaining_budget_after_resume <= remaining_budget_before_crash

``backend.budget.BudgetTracker`` is the live, in-process ledger: it enforces
the limits the canonical runtime policy sets and records every consumption
through the injected recorder, which in the worker is the lease-guarded,
versioned ``record_run_usage_guarded`` RPC. Nothing in this module talks to a
database or a provider.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Mapping

#: Bumped when a dimension is added or its meaning changes. A record from a
#: NEWER schema is still merged (unknown keys are ignored, known keys are
#: maximised), so a rollback can never brick a resume; it simply cannot
#: enforce a dimension it does not know about.
LEDGER_SCHEMA_VERSION = 2

#: Integer dimensions that only ever grow.
LEDGER_COUNTERS: tuple[str, ...] = (
    # provider requests admitted by the budget gate (each one is a model call)
    "model_calls",
    # provider requests actually attempted, INCLUDING the ones that raised
    "provider_attempts",
    # attempts that ended without a settled response (exception, 429, deadline)
    "provider_failures",
    "input_tokens",
    "output_tokens",
    # semantic retries: bounded repairs / fallbacks that spend max_retries
    "retries",
    # provider 429 / backpressure events (paced by the scheduler, not a retry)
    "provider_backpressure_events",
    # model-backed agent steps
    "agent_steps",
    # registered Tool operations invoked (Swarm V2 planned calls)
    "tool_calls",
    # logical task executions that completed / failed (Swarm V2)
    "tasks_completed",
    "tasks_failed",
    # internet searches the run really performed. V1 offers a model MILO's
    # own `web_search` function tool and performs each admitted invocation
    # itself, so this counter is what `max_search_invocations_per_run` is
    # taken against BEFORE each search runs -- not a total read afterwards.
    "search_invocations",
    # Commander replans, including the correction round (which IS a replan)
    "replans",
    # bounded R4 correction rounds started
    "correction_rounds",
    # --- PR-R reasoning-aware usage (schema_version 2) -----------------------
    # Sums over settled calls of what the provider REPORTED. A call whose
    # usage omitted a field adds nothing here; its per-call ledger row keeps
    # the field NULL, so "not reported" is never rewritten as zero where it
    # is recorded.
    # input tokens the provider served from the context cache (hit price)
    "cached_input_tokens",
    # input tokens the provider wrote to the context cache (write price)
    "cache_write_tokens",
    # provider-reported reasoning tokens (completion_tokens_details)
    "reasoning_tokens",
    # ESTIMATED reasoning tokens (completion - answer) for calls whose usage
    # did not report them; never mixed into `reasoning_tokens`
    "reasoning_tokens_estimated",
    # how many settled calls had their reasoning share estimated
    "reasoning_estimated_calls",
    # tokens of the final answer (`message.content`) only
    "answer_tokens",
)

#: Non-negative amounts that only ever grow. ``elapsed_seconds`` is wall-clock
#: rather than a counter, but a LARGER elapsed time leaves LESS run duration,
#: so the maximum stays the conservative direction for it too.
LEDGER_AMOUNTS: tuple[str, ...] = (
    "estimated_cost",
    "actual_cost",
    "search_cost",
    "elapsed_seconds",
)

#: Derived, never maximised on its own: recomputed from the merged components.
LEDGER_DERIVED: tuple[str, ...] = ("total_tokens",)

#: Bookkeeping carried by the durable row. ``ledger_version`` is the write
#: sequence number of the durable record (bumped by the database on every
#: accepted CHANGE, never on a duplicate); ``schema_version`` names the
#: dimension set the record was written under.
LEDGER_METADATA: tuple[str, ...] = ("ledger_version", "schema_version")

LEDGER_SNAPSHOT_FIELDS = frozenset(
    {*LEDGER_COUNTERS, *LEDGER_AMOUNTS, *LEDGER_DERIVED, *LEDGER_METADATA})

#: The bounded public contract of ``runs.usage`` / ``GET /runs/{id}``. It is a
#: strict subset of the ledger. PR-R widened it, as one closed decision across
#: ``backend.schemas.RunUsage``, ``frontend/lib/runUsage.ts``, the database
#: projection (migration 20260925000100) and the export envelope, by the
#: reasoning-aware token breakdown -- counts only, never reasoning text.
PUBLIC_USAGE_FIELDS = frozenset({
    "model_calls", "input_tokens", "output_tokens", "total_tokens",
    "estimated_cost", "actual_cost", "retries", "provider_backpressure_events",
    "agent_steps", "elapsed_seconds",
    "cached_input_tokens", "cache_write_tokens", "reasoning_tokens",
    "reasoning_tokens_estimated", "reasoning_estimated_calls", "answer_tokens",
})
assert PUBLIC_USAGE_FIELDS <= LEDGER_SNAPSHOT_FIELDS


def _counter(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("budget snapshot contains an invalid counter")
    return value


def _amount(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("budget snapshot contains an invalid amount")
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0:
        raise ValueError("budget snapshot contains an invalid amount")
    return numeric


def validate_usage_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Parse ONE record fail-closed, keeping only the fields it carries.

    Unknown keys are ignored rather than rejected (a future release's
    dimension must not brick a resume); a known key with an invalid value
    raises, because silently dropping a corrupt value is indistinguishable
    from refunding it. A declared ``total_tokens`` must equal the components.
    """
    if not isinstance(snapshot, Mapping):
        raise ValueError("budget snapshot must be an object")
    parsed: dict[str, Any] = {}
    for name in LEDGER_COUNTERS:
        if name in snapshot:
            parsed[name] = _counter(snapshot[name])
    for name in LEDGER_AMOUNTS:
        if name in snapshot:
            parsed[name] = _amount(snapshot[name])
    for name in LEDGER_METADATA:
        if name in snapshot:
            parsed[name] = _counter(snapshot[name])
    declared = snapshot.get("total_tokens")
    if declared is not None:
        if _counter(declared) != (_counter(snapshot.get("input_tokens", 0)) +
                                  _counter(snapshot.get("output_tokens", 0))):
            raise ValueError("budget snapshot token total is inconsistent")
        parsed["total_tokens"] = declared
    return parsed


def merge_usage_snapshots(*snapshots: Any) -> dict[str, Any]:
    """Fold durable usage records of ONE run into the most advanced of them.

    The merge is a component-wise maximum over every cumulative dimension, so
    the result is never lower than ANY input on any dimension, whichever
    source happens to be ahead: ``runs.usage`` is rewritten after every
    settled provider call, a checkpoint only at task and batch boundaries, and
    the ledger row after every recorded consumption -- a crash between any two
    of them leaves the others staler, and restoring one alone would refund
    what the run had already durably spent.

    Only the keys some input actually carries appear in the result (plus the
    recomputed ``total_tokens`` when tokens are present), so a record merges
    to itself and an empty or absent record contributes nothing. An empty
    result means there is nothing to restore.
    """
    merged: dict[str, Any] = {}
    contributed = False
    for snapshot in snapshots:
        if not isinstance(snapshot, Mapping) or not snapshot:
            continue
        contributed = True
        parsed = validate_usage_snapshot(snapshot)
        for name in (*LEDGER_COUNTERS, *LEDGER_METADATA):
            if name in parsed:
                merged[name] = max(merged.get(name, 0), parsed[name])
        for name in LEDGER_AMOUNTS:
            if name in parsed:
                merged[name] = max(merged.get(name, 0.0), parsed[name])
    if not contributed:
        return {}
    for name in LEDGER_COUNTERS:
        if name in merged:
            merged[name] = int(merged[name])
    for name in LEDGER_AMOUNTS:
        if name in merged:
            merged[name] = float(merged[name])
    if "input_tokens" in merged or "output_tokens" in merged:
        merged["total_tokens"] = int(merged.get("input_tokens", 0)) + int(merged.get("output_tokens", 0))
    return merged


def public_usage_projection(ledger: Mapping[str, Any]) -> dict[str, Any]:
    """The bounded ``runs.usage`` view of a ledger record."""
    return {name: ledger[name] for name in sorted(PUBLIC_USAGE_FIELDS) if name in ledger}


def usage_is_not_below(after: Mapping[str, Any], before: Mapping[str, Any]) -> bool:
    """True when ``after`` shows at least as much consumption as ``before`` on
    every cumulative dimension ``before`` carries. This is the monotonic
    invariant a resume must satisfy, stated over the ledger itself."""
    for name in (*LEDGER_COUNTERS, *LEDGER_AMOUNTS):
        if name in before and float(after.get(name, 0)) < float(before[name]):
            return False
    return True


#: Which ledger dimension each enforceable ``BudgetConfig`` limit is spent from.
LIMIT_DIMENSIONS: Mapping[str, str] = {
    "max_model_calls_per_run": "model_calls",
    "max_input_tokens_per_run": "input_tokens",
    "max_output_tokens_per_run": "output_tokens",
    "max_total_tokens_per_run": "total_tokens",
    "max_estimated_cost_per_run": "estimated_cost",
    "max_cost_per_run": "actual_cost",
    "max_run_duration_seconds": "elapsed_seconds",
    "max_agent_steps": "agent_steps",
    "max_retries": "retries",
}


def remaining_capacity(ledger: Mapping[str, Any], config: Any) -> dict[str, float | int | None]:
    """What a run still has against each enforceable limit, from its ledger.

    ``None`` for a limit the configuration does not set. Never negative: a
    run that overshot a limit has nothing left, not a debt. This is the value
    ``remaining_budget_after_resume <= remaining_budget_before_crash`` is
    stated over, and it is computed from the ledger ALONE so a caller cannot
    accidentally reconstruct it from a plan, a process-local counter or an
    event stream.
    """
    remaining: dict[str, float | int | None] = {}
    input_tokens = int(ledger.get("input_tokens", 0))
    output_tokens = int(ledger.get("output_tokens", 0))
    for limit_name, dimension in LIMIT_DIMENSIONS.items():
        limit = getattr(config, limit_name, None)
        if limit is None:
            remaining[dimension] = None
            continue
        if dimension == "total_tokens":
            used: float = input_tokens + output_tokens
        else:
            used = float(ledger.get(dimension, 0))
        remaining[dimension] = max(0, limit - used)
    return remaining


def ledger_dimensions() -> Iterable[str]:
    """Every cumulative dimension, in declaration order."""
    return (*LEDGER_COUNTERS, *LEDGER_AMOUNTS)

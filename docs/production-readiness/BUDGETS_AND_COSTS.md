# Budgets and costs

All `COMPLETED_IN_CODE`; the cap VALUES are
`REQUIRES_MANUAL_OPERATOR_CONFIGURATION` before any execution stage.

## Hard caps

`BudgetConfig` (`backend/budget.py`) reads per-run and daily caps from the
environment (names in [ENVIRONMENT_MATRIX.md](ENVIRONMENT_MATRIX.md)).
Paid execution may never be enabled without ALL of:

- `MILO_MAX_MODEL_CALLS_PER_RUN`
- `MILO_MAX_TOTAL_TOKENS_PER_RUN`
- `MILO_MAX_ESTIMATED_COST_PER_RUN`
- `MILO_MAX_RUN_DURATION_SECONDS`
- `MILO_MAX_RETRIES`

(`BudgetConfig.MANDATORY_FOR_PAID_EXECUTION`; enforced fail-closed by
`backend/production_config.py` — `PAID_WITHOUT_BUDGET` is an error in
production.) Additional caps: input/output token caps, agent-step cap,
per-user/per-project concurrency, `MILO_DAILY_USER_BUDGET`,
`MILO_DAILY_PROJECT_BUDGET`, `MILO_MAX_COST_PER_RUN`.

## Canonical reservation → settlement lifecycle

Migration `015` (`model_call_budget_reservations` +
`reserve_model_call_budget` / `settle_model_call_budget`) implements the
atomic pre-call reservation and post-call settlement path:

1. **Reserve** before every model call: atomically checks daily user and
   project budgets and records a reservation row; over-budget ⇒ the call is
   refused and the run stops with a budget decision.
2. **Settle** after the call with the actual cost (`settled`), or
   **release** on provider exception. A missing reservation id or a failed
   settlement fails closed — the run stops rather than running unmetered
   (`backend/budget.py`).
3. Orphan detection: reservations that never settle are surfaced by
   monitoring (see MONITORING_AND_INCIDENTS.md) and by the read-only query
   in the smoke tooling; none may remain silently orphaned.

Legacy daily-budget RPCs from migration `014` are deprecated with execute
privileges revoked, so production has exactly one canonical lifecycle.

## Actual usage and overage

`BudgetTracker` counts tokens and cost from provider responses, falling
back to `backend/model_pricing.py` for deterministic per-model cost when
the provider omits cost. Post-call overage checks emit append-only ledger
`overage` entries (`run_usage_ledger`, migration `013`) and stop the run on
actual-limit breach — caps are enforced on actuals, not only estimates.
Aggregate usage is persisted on the run (`runs.usage`, migration `010`).

## Ledger

`run_usage_ledger` (migration `013`) is append-only: every reservation,
settlement, release and overage leaves an auditable row. No deletes.

## Execution usage ledger: durable, monotonic, resume-safe

`RuntimePolicy` (`backend/runtime_policy.py`) is the authority for what a
run's limits ARE. The **ExecutionUsageLedger** is the single authority for
what a run has CONSUMED against them, and it is what every resume is held
to. Its contract lives in `backend/execution_usage.py`; `BudgetTracker` is
the live, in-process ledger; migration `20260920000100` is its durable home.

Core invariant, on every cumulative dimension:

    remaining_budget_after_resume <= remaining_budget_before_crash

**Dimensions** (all cumulative across attempts, replans and corrections):
model calls, provider attempts (admitted requests, including ones that
raised), provider failures, input/output/total tokens, estimated and
recorded cost, semantic retries, provider backpressure events, agent steps,
tool calls, task executions (completed / failed), search invocations and
search cost (really consumed: V1 performs each admitted internet search
itself, and `max_search_invocations_per_run` is taken BEFORE each one runs —
see PROVIDER_AUTHORITY.md; the per-invocation PRICE remains an interface at
0.00 until a verified provider price is configured), replans and
correction rounds, elapsed seconds.

**Durable record.** `run_execution_usage` holds one row per run: the full
ledger (`jsonb`), a `version` the database advances on every accepted
CHANGE (never on an idempotent replay), the last writing `attempt` and
worker. It is written only through `record_run_usage_guarded`, which
verifies the lease under the database clock (runs row `FOR UPDATE`, so
concurrent settles and a concurrent reclaim serialize), **merges** by
component-wise maximum (`merge_execution_usage`) so no accepted write can
lower a counter, and projects the bounded public aggregate into
`runs.usage` in the same transaction. A `BEFORE UPDATE` trigger enforces the
invariant at the storage boundary even against a direct service-path
write. `update_run_usage_guarded` and `transition_run_worker_guarded` keep
their signatures and merge instead of overwrite.

**Recording.** The tracker records after EVERY consumption -- the admission
of a provider request (before it is sent, so a process that dies
mid-request is still charged for it), its settlement, each retry,
backpressure event, agent step, tool call, task result, replan and
correction round -- not only after a settled call. A rejected write (stale
lease) propagates: a worker that cannot record consumption does not keep
consuming.

**Resume.** Before constructing any model path, for EVERY engine, the
worker restores the component-wise maximum of every durable record of the
run: the ledger row, `runs.usage` and the latest checkpoint's `token_usage`
(plus the Swarm V2 state's `usage_snapshot`). They advance at different
rates and a crash can leave any one of them staler; the maximum holds
whichever is ahead on each dimension, so nothing durably spent is refunded.

* **V1 (`vehicle_catalog_v1`)** has no partial-phase resume. From the final
  checkpoint it completes without spending (the fast path); from any other
  checkpoint it deliberately REPLAYS the pipeline -- and the replay is
  charged on top of everything earlier attempts consumed. A relaunched V1
  run cannot spend a second full budget: it trips the same limits at the
  same cumulative totals.
* **V2 (`swarm_v2`)** resumes from its versioned checkpoint. Its checkpoint
  bounds the PLAN (`replans`, `correction_rounds`, completed tasks); the
  ledger records what the run SPENT. Remaining tool-call and task capacity
  handed to the feasibility gate is `PlanLimits` minus the ledger, never a
  value rebuilt from the current plan's completed tasks -- a failed task, an
  earlier attempt and a superseded plan all stay spent.

Checkpoints carry the consolidated ledger as `token_usage` (the merge of
what the engine wrote and the tracker's ledger), so a checkpoint is never
below the run's durable usage. `runs.usage` remains exactly the
`backend.schemas.RunUsage` contract; ledger-only dimensions never reach the
browser.

Regression coverage: `tests/test_execution_usage_ledger.py` (offline) and
the `20260920000100` section of `tests/test_migrations_postgres.py` (real
PostgreSQL).

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

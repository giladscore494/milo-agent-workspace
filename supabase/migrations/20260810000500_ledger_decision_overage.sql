-- run_usage_ledger decision values: align the constraint with the code.
--
-- BudgetTracker appends ledger rows with decision 'overage' (post-call
-- actual-limit breaches: token/cost overage, BUDGETS_AND_COSTS.md §4) and
-- 'released' (provider-exception settlement), but migration 013's CHECK
-- only allowed ('reserved','settled','rejected'). The first live staging
-- run that tripped a token limit crashed on the constraint instead of
-- recording the overage — a schema/code mismatch invisible to unit tests
-- because the in-memory repository enforces no constraint.
--
-- Additive and data-preserving: the allowed set only widens (matching the
-- model_call_budget_reservations status set from migration 015); existing
-- rows all satisfy the new constraint.

alter table public.run_usage_ledger drop constraint if exists run_usage_ledger_decision_check;
alter table public.run_usage_ledger add constraint run_usage_ledger_decision_check
  check (decision in ('reserved','settled','rejected','overage','released'));

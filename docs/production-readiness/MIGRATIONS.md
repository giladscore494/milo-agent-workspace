# Migrations

Status: migration content `COMPLETED_IN_CODE`; production application is
`REQUIRES_MANUAL_OPERATOR_CONFIGURATION`. No operator script in
`scripts/release/` ever applies a migration. One pre-existing guarded
workflow exists (`.github/workflows/deploy-supabase-migrations.yml`): on
push to `main` it runs a dry-run preflight only (auto-apply requires the
repository variable `SUPABASE_MIGRATIONS_AUTO_APPLY=true`, which must stay
unset), and a manual `workflow_dispatch` apply additionally requires
typing `APPLY_PRODUCTION_MIGRATIONS`. Pull-request CI never touches
production.

## Order (apply strictly in this sequence)

| # | File | Adds |
| --- | --- | --- |
| 001 | `001_project_workspace.sql` | projects, conversations/messages reconciliation with the legacy baseline |
| 002 | `002_durable_runtime.sql` | durable run columns, run_events, run_checkpoints, worker_heartbeats, status model |
| 003 | `003_workflow_proposals.sql` | workflow_proposals |
| 004 | `004_supervisor_shadow_mode.sql` | supervisor_decisions (shadow only) |
| 005 | `005_internet_governance.sql` | tool_access_requests, tool_grants, tool_usage, sources, claims, conflicts |
| 006 | `006_deployment_hardening.sql` | stuck_runs view, run_invocations, hardening |
| 007 | `007_project_members.sql` | project_members + membership RLS |
| 008 | `008_workflow_proposal_ownership.sql` | proposal created_by/project_id + ownership RLS |
| 009 | `009_run_idempotency_lifecycle.sql` | requested_by, idempotency, launch_state |
| 010 | `010_run_usage.sql` | runs.usage aggregate |
| 011 | `011_proposal_ownership_protection.sql` | ownership tamper protection, atomic project+owner creation |
| 012 | `012_atomic_run_operations.sql` | create_message_and_run, launch CAS, launch_unknown, claim_run_lease, lease_token |
| 013 | `013_usage_ledger.sql` | append-only run_usage_ledger |
| 014 | `014_atomic_daily_budget_reservations.sql` | legacy daily RPCs (deprecated, execute revoked) |
| 015 | `015_atomic_model_call_budget_lifecycle.sql` | model_call_budget_reservations + reserve/settle RPCs, portable grants |
| ts | `20260706192500_grant_…_schema_privileges.sql` | service-role schema privileges (timestamped) |

All migrations are additive, idempotent and data-preserving. There are no
destructive down-migrations, by policy (`scripts/check_migrations.py`
forbids `drop table` and data deletes).

## Confirmed legacy baseline

The production Supabase project began with four pre-existing tables
(`conversations`, `messages`, `runs`, `run_events`) and an empty migration
history. Migrations `001`/`002` contain explicit reconciliation clauses
(column renames/backfills such as `sender_role → role`, `progress →
progress_percent`) whose presence is enforced by
`scripts/check_migrations.py`.

## Supported remote states

`scripts/release/check-migration-state.sh` classifies a remote schema
(read-only, operator-supplied connection) as one of:

- **empty-schema** — apply 001→015 in order;
- **legacy-baseline** — the confirmed baseline above; apply 001→015 in
  order (reconciliation clauses handle the existing rows);
- **partially-migrated** — apply only the missing tail, in order, after
  reviewing the reported markers;
- **fully-migrated** — nothing to apply; rerunning is safe (idempotent).

All four states (plus rerun/idempotency) are executably tested against
real PostgreSQL in CI (`tests/test_migrations_postgres.py`,
`MILO_REQUIRE_PG_TESTS=1`, zero skips allowed).

## Manual database sequence

1. `scripts/release/check-migration-state.sh --database-url-env
   MILO_READONLY_DB_URL --plan-output migration-plan.json`
2. Review the plan and hashes; confirm a database backup exists.
3. Apply each pending migration manually, in order, via `psql` or the
   Supabase SQL editor.
4. Re-run the state check; validate RLS and function permissions
   (validation queries in the PostgreSQL test suite mirror these checks).
5. Generate and review the membership backfill
   (`generate-membership-backfill.sh`), check row counts, apply manually,
   validate ownership.
6. Generate and review the proposal backfill
   (`generate-proposal-backfill.sh`), check row counts, apply manually,
   validate proposals.
7. Rerun read-only checks (`smoke-test-read-only.sh`).

## Rollback

Forward-only: stop execution, verify backup, write a corrective forward
migration, review manually, apply after explicit approval, re-verify RLS
and ownership. See [ROLLBACK.md](ROLLBACK.md) §Migrations.

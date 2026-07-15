# Supabase migration deployment

This runbook describes the safe production migration workflow for the `production` GitHub Environment. It is designed for a bootstrap state where production Supabase credentials already exist and the remote database already contains four tables from earlier work.

## Legacy baseline reconciliation

### Confirmed production state before first apply

- The production `public` schema contains exactly four legacy base tables: `conversations`, `messages`, `runs`, and `run_events`.
- The production migration history is empty: local migrations `001`–`006` exist in the repository, but the remote has no recorded migrations and none of them have ever been applied.
- Because nothing was ever applied, the unapplied migration files themselves were corrected before their first production application. No `supabase migration repair` is needed: repair only rewrites migration history, and there is no history to rewrite. The files were fixed at the source instead of marking anything as applied.

### Why reconciliation was required

The legacy tables were created by earlier work with shapes the current backend does not use:

- `messages` has `sender_role text not null` and a `bigint` primary key, while the backend inserts a `role` column and previously forced message IDs through `UUID(...)`.
- `runs` has `user_prompt text not null`, `result jsonb`, and `error_message text`, while the backend inserts `input jsonb` (never `user_prompt`) and updates `output`/`error`/`updated_at`. The `NOT NULL` on `user_prompt` would have rejected every new insert, and `updated_at` did not exist.
- `run_events` has `agent_name text` and an integer `progress` column (0–100), while the runtime writes `agent text`, `phase text`, and structured JSONB `progress`. The previous `ADD COLUMN IF NOT EXISTS progress jsonb` would have silently kept the incompatible integer column.
- Migration `006` creates the `stuck_runs` view over `runs.updated_at`, which did not exist in the legacy table.

### What the reconciled migrations do

- `001` renames `messages.sender_role` to `messages.role` when only `sender_role` exists (rename preserves all rows, the `NOT NULL`, and the role CHECK constraint). If both columns somehow exist, `role` is backfilled from `sender_role`, `role` becomes `NOT NULL`, and `sender_role` is kept but made nullable. If only `role` exists, nothing happens.
- `002` adds `runs.input` (jsonb, not null, default `'{}'`), `runs.output`, `runs.error`, and `runs.updated_at` (not null, default `now()`), then backfills: `input` from `user_prompt`, `output` from `result`, `error` from `error_message`. It drops the `NOT NULL` on `user_prompt` so new inserts work, adds a `runs` update trigger reusing `set_updated_at()`, and re-adds `runs_status_check` as `NOT VALID` so legacy status values never fail the migration (the constraint is validated only when existing data allows it).
- `002` renames the legacy integer `run_events.progress` to `progress_percent` (keeping its 0–100 CHECK), then creates the JSONB `progress` column plus `event_type`, `agent`, and `phase`. `agent_name` is kept.

### Preserved legacy columns

No table is dropped, no row is deleted, and no ID is converted. These legacy columns are intentionally preserved with their data: `messages.sender_role` (only in the both-columns edge case; normally it simply becomes `role`), `runs.user_prompt`, `runs.result`, `runs.error_message`, `runs.progress`, `runs.current_phase`, `runs.cancel_requested`, `run_events.agent_name`, and `run_events.progress_percent` (the renamed integer progress).

### ID types

- Message IDs remain `bigint`: production `messages.id` is a bigint identity primary key with existing rows, and converting the key would rewrite or discard existing IDs. The backend now passes message IDs through without UUID conversion and stores their string form inside the run `input` JSON.
- Run-event IDs remain `bigint`: production `run_events.id` is also a bigint identity primary key. The API response model (`backend.schemas.RunEvent.id`) now types this field as `int` instead of `UUID`, and the frontend `RunEvent.id` type is `number`. No run-event ID is converted or rewritten.
- Run IDs and conversation IDs remain `uuid`: production `runs.id` and `conversations.id` are already `uuid`, every new table from `002`–`006` references `runs(id) uuid`, and the backend generates and parses run IDs and conversation IDs as UUIDs. Only `messages.id` and `run_events.id` are bigint.

### Fixture correction history

Two earlier drafts of `tests/fixtures/legacy_baseline.sql` in this change were described as an exact reproduction of the confirmed production schema but were not:

- Draft 1 declared `run_events.id` as `uuid` instead of `bigint`, omitted the pre-existing `run_events.event_type text not null` column, used the wrong `messages.sender_role` CHECK list (missing `tool`), used the wrong foreign-key delete behavior on `messages.conversation_id`/`messages.run_id`, and omitted `conversations.updated_at`, exact `NOT NULL`/default values, and row-level security.
- Draft 2 fixed the above but still omitted `ON DELETE CASCADE` on `runs.conversation_id`, the confirmed `runs_progress_check` and `runs_status_check` constraints (by name), and the four confirmed non-primary indexes (`messages_conversation_id_created_at_idx`, `run_events_run_id_created_at_idx`, `runs_conversation_id_idx`, `runs_status_idx`). Its seed data also included a `runs.status` value (`legacy_error_state`) that the confirmed `runs_status_check` does not actually permit, so it was not representative of real production data.

The fixture now matches the confirmed production schema column-for-column, constraint-for-constraint, index-for-index, and its seed data uses only status values the confirmed `runs_status_check` allows. `tests/test_migrations_postgres.py::test_fixture_still_declares_run_events_id_bigint_and_event_type` guards the run_events shape against regressing.

### Confirmed baseline vs. synthetic defensive testing

`tests/test_migrations_postgres.py` draws a hard line between two kinds of tests:

- **Confirmed production-baseline tests** (`pre_migration_db`, `db` fixtures) apply the exact confirmed schema, including its real constraint names and indexes, and seed only data the confirmed `runs_status_check` genuinely permits. These prove migrations 001–006 upgrade real production data safely. The `ownership_db` fixture additionally seeds a legacy `workflow_proposals` row *before* migration `008` runs and then applies `008` twice, proving legacy proposals survive with NULL ownership, that reapplication is a no-op, and that the new RLS policies scope authenticated access to project members while the service path keeps full visibility.
- **Synthetic defensive edge-case tests** (`synthetic_invalid_status_db` fixture) start from that same confirmed baseline but then deliberately drop the confirmed `runs_status_check` and insert a status value that could never exist under it, purely to exercise migration 002's defensive `NOT VALID` handling for a hypothetical historical anomaly. Every fixture, docstring, and test name in that path is labeled `SYNTHETIC` and must never be read as describing real production state. Migration 002's `NOT VALID`-then-`VALIDATE` handling is kept specifically for this hypothetical case, even though the confirmed baseline's own data never triggers it.

### Post-merge dry-run and manual apply process

1. Merge the reconciliation pull request. Do not enable `SUPABASE_MIGRATIONS_AUTO_APPLY`.
2. Let the push-triggered workflow run on `main`. It validates the repository migrations, links the project, prints `supabase migration list`, and performs `supabase db push --linked --dry-run` only.
3. In the dry-run output, confirm that exactly `001`–`006` are listed as pending and that no errors are reported.
4. Run the workflow manually with `mode: apply` and `confirmation: APPLY_PRODUCTION_MIGRATIONS`.
5. Verify with `supabase migration list` that all six migrations are now recorded remotely.

### Required post-apply schema verification

Run these read-only checks (SQL editor or `psql`) after the first apply:

```sql
-- messages: role reconciled, data preserved
select count(*) from public.messages where role is null;              -- expect 0
select column_name from information_schema.columns
 where table_schema = 'public' and table_name = 'messages'
   and column_name in ('role', 'sender_role');                        -- expect role

-- runs: new columns exist and are backfilled
select count(*) from public.runs where input is null or updated_at is null;  -- expect 0
select is_nullable from information_schema.columns
 where table_schema = 'public' and table_name = 'runs'
   and column_name = 'user_prompt';                                   -- expect YES
select count(*) from public.runs
 where user_prompt is not null and input->>'content' is null;         -- expect 0

-- run_events: integer progress preserved, JSONB progress available
select data_type from information_schema.columns
 where table_schema = 'public' and table_name = 'run_events'
   and column_name in ('progress', 'progress_percent') order by column_name;
   -- expect jsonb (progress) and integer (progress_percent)

-- stuck_runs view resolves
select * from public.stuck_runs limit 1;
```

If any check fails, stop before enabling auto-apply and record the exact mismatch.

### Validation performed in the repository

- `scripts/check_migrations.py` performs **static text validation only**: it fails if the reconciliation clauses above disappear from the migration files.
- `tests/test_migrations_postgres.py` performs **actual PostgreSQL execution**: it creates an ephemeral PostgreSQL database, applies `tests/fixtures/legacy_baseline.sql` (the exact confirmed four-table legacy schema) with seeded legacy rows, executes migrations `001`–`006` in order, asserts data preservation and the final schema, and re-applies all migrations to prove they are rerun-safe. The module skips (never silently passes) when PostgreSQL server binaries are unavailable.

## Required GitHub Environment secrets

The `production` environment must contain these secrets:

- `SUPABASE_ACCESS_TOKEN`
- `SUPABASE_PROJECT_ID`
- `SUPABASE_DB_PASSWORD`

Never paste real values into workflow logs, documentation, commits, pull requests, or issue comments.

## Automatic apply gate

Push-triggered runs are dry-run-only unless the `production` GitHub Environment Variable `SUPABASE_MIGRATIONS_AUTO_APPLY` is explicitly set to:

```text
true
```

This variable is not a secret. The workflow does not create it and does not default it to `true`.

Deleting `SUPABASE_MIGRATIONS_AUTO_APPLY`, leaving it blank, or setting it to:

```text
false
```

returns all push-triggered runs to dry-run-only mode.

## Bootstrap process

Because the production secrets already exist and the remote database already has four tables, bootstrap must be controlled:

1. Merge the workflow pull request.
2. Let the first automatic run on `main` complete. It validates repository migrations, links to the Supabase project, displays `supabase migration list`, and runs `supabase db push --linked --dry-run` only.
3. Open GitHub Actions and review:
   - repository migration validation output,
   - `supabase migration list`,
   - dry-run output.
4. Do not enable automatic apply yet.
5. If the schema and migration history are compatible, run the workflow manually with:
   - `mode`: `apply`
   - `confirmation`: `APPLY_PRODUCTION_MIGRATIONS`
6. Verify migration history after the controlled manual apply.
7. Only after the first successful controlled deployment, optionally create the `production` Environment Variable:

```text
SUPABASE_MIGRATIONS_AUTO_APPLY=true
```

Future migration pushes to `main` may then apply automatically after validation, migration history display, and a successful dry-run.

## Manual dry-run and apply

Manual `dry-run` mode never applies migrations.

Manual `apply` mode is allowed only when the confirmation input exactly matches:

```text
APPLY_PRODUCTION_MIGRATIONS
```

If `apply` is selected without that exact confirmation, the workflow fails before the production apply command is run and prints the required confirmation text.

## Failure procedure for existing-table conflicts

If existing tables conflict with migrations, local and remote migration versions conflict, migration history has diverged, or the dry-run reports that an object already exists:

1. Do not use database reset.
2. Do not automatically use migration repair.
3. Inspect the remote schema.
4. Inspect `supabase migration list`.
5. Compare the remote schema and migration history against migrations `001` through `006`.
6. Record the exact mismatch before deciding whether a one-time migration history reconciliation is required.

The workflow intentionally does not execute `supabase migration repair`.

## Security boundaries

The migration workflow requires only the three production Supabase deployment secrets listed above. It must not need:

- `SUPABASE_SECRET_KEY`
- `SUPABASE_SERVICE_ROLE_KEY`
- `NEXT_PUBLIC_SUPABASE_ANON_KEY`
- `KIMI_API_KEY`
- Vercel secrets
- Google Cloud credentials

The workflow must not reset, drop, recreate, repair, or delete the production database.

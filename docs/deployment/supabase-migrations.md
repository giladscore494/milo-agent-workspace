# Supabase migration deployment

This runbook describes the safe production migration workflow for the `production` GitHub Environment. It is designed for a bootstrap state where production Supabase credentials already exist and the remote database already contains four tables from earlier work.

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

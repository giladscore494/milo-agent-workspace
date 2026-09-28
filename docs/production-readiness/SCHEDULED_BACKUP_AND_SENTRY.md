# Scheduled backup, restore test, CI on push, Dependabot, Sentry (PR-OBS)

Everything here is unattended once set up. None of it touches catalog data or
engine logic, and none of it replaces the manual, reviewed
[`backup-supabase-production.yml`](SUPABASE_BACKUP.md), which is unchanged and
remains the procedure before a migration.

## What runs, and when

| Workflow / job | Schedule (UTC) | Environment | Identity | Output |
| --- | --- | --- | --- | --- |
| `backup-supabase-scheduled.yml` / `backup` | daily 02:17 (`17 2 * * *`) + manual | `production-backup` (no reviewer, `main` only) | `milo-backup-writer` via WIF (pool `milo-github-backup`) — `roles/storage.objectCreator` on the backup bucket only | `gs://<bucket>/supabase/<UTC date>/milo-supabase-public-<stamp>-<run>.tar.gz.enc` + `.manifest.json`, create-only |
| `backup-supabase-scheduled.yml` / `restore-test` | monthly, 3rd, 04:41 (`41 4 3 * *`) + manual | `production-backup` | `milo-backup-reader` via WIF — `roles/storage.objectViewer` on the backup bucket only | counts only (`COUNT <table> <n>`, `PASS ...`) |
| `ci.yml` | every pull request **and every push to `main`** | none | none | the four mandatory jobs, unchanged |
| Dependabot | weekly, Monday 05:23 | — | — | ≤ 3 open PRs per ecosystem (pip `/backend`, npm `/frontend`, github-actions); minor+patch grouped; never auto-merged |

### The backup

* `pg_dump` (custom format) of schema `public`, **schema and data**, through the
  existing **read-only** role (`MILO_BACKUP_DB_URL`, from `~/.milo_ro_url`) —
  never the owner password.
* `pg_dump`'s major must **equal** the server's: the job reads the server
  version first, installs `postgresql-client-<major>` from the PostgreSQL apt
  repository (`scripts/ops/install-pg-client.sh`) and the tool refuses a
  mismatch with `FAIL PG_DUMP_VERSION_MISMATCH`.
* Encrypted exactly like the manual workflow: `openssl enc -aes-256-cbc -salt
  -pbkdf2 -iter 600000 -md sha256`, passphrase from the environment. The bundle
  is decrypted again and its checksums verified **before** the plaintext is
  deleted; the output directory then holds exactly the bundle and its manifest.
* Uploaded with `ifGenerationMatch=0` (create-only). The bucket's unlocked
  7-day retention policy means no backup can be deleted or replaced for 7 days;
  lifecycle deletes objects after 30 days.
* **Sequence data is not in the dump.** The read-only role holds SELECT on every
  table but not on sequences, and `pg_dump` reads a sequence's value with
  `SELECT last_value FROM <seq>`. Sequence *definitions* are kept; the restore
  sets every owned sequence to `max(column)+1`. The manifest says so.
* Roles, `auth` and `storage` are not in this backup (as in the manual one).

### The restore test

Downloads the newest manifest and the bundle it names (size and sha256 must
match), decrypts, verifies checksums, and restores into a `postgres:<major>`
service container (`MILO_BACKUP_PG_MAJOR`; the restore refuses a container
whose major differs from the manifest's). Supabase's own objects that `public`
references (`auth.users`, `auth.uid()`, the `anon` / `authenticated` /
`service_role` roles, `pgcrypto`) get inert stand-ins first. It then checks that
**every table the migrations create exists** and that `catalog_raw_records`,
`catalog_variant_coverage` and `runs` are **non-empty**, and prints the counts.

### Failures are visible

A failed step fails the job (red). When the `production-backup` secret
`SENTRY_DSN` is set, one event is sent (`scripts/ops/sentry_report.py`): the
workflow, job, run id and commit — never a log line or a secret.

## Operator steps, in order

All in Cloud Shell, from a checkout of `main`, with `gh auth login` done as a
repository admin and `~/.milo_ro_url` (mode 600) in place.

1. **Backup setup** (idempotent; prints only `PASS` / `FAIL`):

   ```bash
   bash scripts/ops/setup-backup.sh            # converge
   bash scripts/ops/setup-backup.sh --check    # verify only, any time
   ```

   Options: `--bucket NAME` (default `<project>-milo-supabase-backups`),
   `--pg-major N` (default: read through `~/.milo_ro_url`).
   It creates the bucket, the two service accounts and their bucket-level
   roles, the dedicated WIF pool/provider and bindings, the passphrase file,
   and the `production-backup` environment with its secrets and variables.

2. **Keep the passphrase.** `~/.milo_backup_passphrase` is the only readable
   copy of `MILO_BACKUP_PASSPHRASE` (GitHub secrets cannot be read back, and a
   Cloud Shell home is deleted after long inactivity). Copy it into the
   operator password manager now, next to — and labelled differently from —
   the manual workflow's `SUPABASE_BACKUP_PASSPHRASE`. Every scheduled backup
   needs it to be restored. If the file is ever lost while the secret exists,
   `setup-backup.sh` refuses to continue; `--rotate-passphrase` starts a new one
   (older backups then need the old passphrase).

3. **Sentry projects.** Create two projects in Sentry: `milo-backend`
   (platform Python) and `milo-frontend` (platform Next.js). With data
   scrubbing on and IP addresses not stored.

4. **Backend DSN** (Secret Manager, bound by the existing deploy):

   ```bash
   umask 077; printf '%s' '<milo-backend DSN>' > ~/.milo_sentry_dsn
   bash scripts/ops/setup-sentry.sh --dsn-file ~/.milo_sentry_dsn
   ```

   This creates the secret `SENTRY_DSN`, grants `secretAccessor` on it to the
   API, worker and capture identities (read from the operator configuration),
   stores the DSN as a new version, and sets the `production-backup` secret
   `SENTRY_DSN`. The **next deploy** binds it to the API and the worker
   (`scripts/deploy/cloud-run.sh`) and the next capture-job ensure binds it to
   the capture job — both only because the secret now has an enabled version
   (`MILO_OPTIONAL_RUNTIME_SECRETS`); without one they bind nothing and say so.

5. **Frontend DSN** (Vercel → Project → Settings → Environment Variables,
   Production): `NEXT_PUBLIC_SENTRY_DSN` = the `milo-frontend` DSN, then
   redeploy. Optional: `NEXT_PUBLIC_MILO_SENTRY_TRACES_SAMPLE_RATE` (≤ 0.05;
   empty = off). The site builds and runs without either.

6. **First backup, by hand:**

   ```bash
   gh workflow run backup-supabase-scheduled.yml --ref main -f job=backup
   gh run watch "$(gh run list --workflow backup-supabase-scheduled.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
   ```

   Expect `PASS backup created: ...` and `PASS uploaded gs://.../supabase/<date>/...`.

7. **Restore test, by hand** (after step 6):

   ```bash
   gh workflow run backup-supabase-scheduled.yml --ref main -f job=restore-test
   ```

   Expect three `COUNT` lines, each non-zero, and `PASS restore verified`.

## Sentry: what an event may carry

Off unless a DSN is configured (tests, CI and local never have one).
`send_default_pii` off; no request bodies, headers, cookies or query strings;
no local variables; no breadcrumbs; no log capture; exception **messages**
replaced by `[redacted]` (type and stack stay); no auto-enabled integrations
(so no OpenAI/httpx instrumentation). Never a model prompt, model output, tool
result or register payload. Every event: `release` = the deployed commit
(`MILO_RELEASE_SHA` / Vercel's commit SHA), tag `service`, and `run_id` when
there is one. Traces off by default, never above 0.05. The worker reports a
run it terminalized as `failed` exactly once (static error code only).
Backend: `backend/observability.py`; frontend: `frontend/lib/observability.ts`.

# One-command production bootstrap

`scripts/release/bootstrap-production.sh` replaces the manual, error-prone
production preparation process. Instead of hand-building
`milo-production.yaml`, service accounts, Secret Manager resources and their
per-secret IAM, Vercel production variables, Upstash metadata and Cloud Run
identity assignments, the operator runs **two commands**:

```bash
# 1. Read-only. Inspects live state and shows exactly what would change.
scripts/release/bootstrap-production.sh --plan --output-directory ~/milo-prod-out

# 2. Guarded. Performs the idempotent bootstrap and then re-audits.
scripts/release/bootstrap-production.sh --apply \
  --environment production --confirm-production-change \
  --release-sha "$(git rev-parse HEAD)" \
  --expected-project big-cabinet-457321-t7 \
  --expected-account <operator-email> \
  --output-directory ~/milo-prod-out \
  --supabase-key-env MILO_SUPABASE_KEY \
  --provider-key-env MILO_PROVIDER_KEY \
  --upstash-email-env MILO_UPSTASH_EMAIL \
  --upstash-apikey-env MILO_UPSTASH_APIKEY \
  --vercel-token-env MILO_VERCEL_TOKEN
```

Default mode (no flag) is `--plan` and is fully read-only.

## What is automated

In guarded apply mode the script idempotently:

- creates the dedicated **API**, **worker** and **gateway** service accounts if
  missing (never a service-account **key**), and verifies they are distinct;
- creates the required **Secret Manager** resources, adds one enabled version
  **only from hidden input**, and binds `roles/secretmanager.secretAccessor`
  **per-secret** to the correct single consumers (project-wide accessor is
  rejected);
- points the Cloud Run **API service** at the API SA and the Cloud Run
  **worker job** at the dedicated worker SA (configuration only — the worker
  job is **never executed**), keeping both **private**;
- discovers or creates a **dedicated production Upstash Redis** database via the
  official Upstash Developer API, stores its REST **token** in Secret Manager
  (never printed), and records only the non-secret REST **URL**;
- configures the required **Vercel production** environment variables
  (gateway/public/server values only) after proving the linked project's
  identity — never provider keys or Supabase service-role credentials;
- generates `milo-production.yaml` and non-secret metadata from the inspected /
  applied **live state**, plus `bootstrap-plan.json` / `bootstrap-apply.json`;
- runs the full read-only readiness audit and requires **consolidated
  blocked = 0** for success.

## Credentials the human still supplies (one-time)

Everything else is discovered automatically. The operator provides only these
secrets, and **only** as the *name* of an environment variable holding the
value (or via an invisible `--prompt-secrets` terminal prompt). A value is
never accepted as a normal CLI argument, and no value is ever printed, logged,
serialized or written into the repository:

| Secret | Flag | Purpose |
| --- | --- | --- |
| Supabase service-role / secret key | `--supabase-key-env` | Stored in Secret Manager (`milo-supabase-service-key`). |
| Provider (Kimi/Moonshot) API key | `--provider-key-env` | Stored in Secret Manager (`milo-provider-api-key`), worker-only. |
| Upstash management email | `--upstash-email-env` | Authenticates the Upstash Developer API for Redis discovery/creation. |
| Upstash management API key | `--upstash-apikey-env` | Same. |
| Vercel access token | `--vercel-token-env` | Authenticates the Vercel CLI for identity proof + variable upserts. |
| Read-only PostgreSQL URL (optional) | `--database-url-env` | Enables the migration-state audit check. |

## The apply guard

Every mutation requires **all** of:

- `--apply`
- `--environment production`
- `--expected-project` matching the active gcloud project
- `--expected-account` matching the active gcloud account
- `--release-sha` equal to the checked-out `HEAD` (full 40-char SHA)
- `--confirm-production-change`
- `MILO_OPERATOR_ACK=I_UNDERSTAND_THIS_CHANGES_PRODUCTION`
- a clean Git worktree
- non-placeholder inputs

If any check fails the script stops **before any mutation** and writes a
`guard-blocked` report. No partial apply is possible after a guard failure.

## Safety invariants (never weakened)

- The Cloud Run worker job is **never executed**.
- No **service-account key** is ever created.
- Every **execution flag** stays `false`; **paid execution** is never enabled.
- No **provider** (Kimi/Moonshot) call is ever made.
- Cloud Run resources stay **private** (no `--allow-unauthenticated`).
- Secret **values** never appear in stdout, stderr, JSON reports or artifacts.
- Permission/API errors are **never** classified as "resource missing".
- The bootstrap **does not deploy**. Vercel deployment happens only with the
  separate explicit `--deploy-vercel` flag under the same guard, and the
  Cloud Run deployment remains a distinct operator step
  (`generate-deployment-plan.sh`).

## Generated outputs

All artifacts are written to the private `--output-directory` (mode `700`),
which must be **outside** the Git worktree so secrets can never be committed:

- `milo-production.yaml` — generated manifest (non-secret metadata only);
- `milo-production.metadata.env` — non-secret metadata (secret **names** only);
- `bootstrap-plan.json` / `bootstrap-apply.json` — machine-readable reports,
  including a `recovery_steps` list on partial failure;
- `readiness.json` / `readiness.log` — the consolidated audit output.

The Redis REST **token** is verified through Secret Manager (the generated
manifest drives `check-secret-metadata.sh` to confirm the `redis_rest_token`
secret exists with the correct per-secret accessors), so the audit never
re-handles the token value and no persistent plaintext is created.

## Partial failure

If any phase fails, the script writes a `partial-failure` report with an
explicit `recovery_steps` list, exits non-zero and **never claims full
success**. Re-running `--apply` is idempotent: existing service accounts,
secrets, enabled versions and identities are left untouched.

## GitHub Actions

`.github/workflows/bootstrap-production.yml` runs the same script on
`workflow_dispatch` only (never on push/PR). The plan job is always available;
the apply job requires a typed confirmation input plus `production` Environment
approval, authenticates to Google Cloud through **Workload Identity
Federation** (no service-account keys), and uploads only the redacted reports.

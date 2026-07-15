# One-command production bootstrap and audit

`scripts/release/bootstrap-production.sh` replaces the manual, error-prone
production preparation process. It **adopts the operator's existing** Google
Secret Manager and Vercel resources instead of creating duplicates, configures
the live Cloud Run services, and verifies the result against **live state**.

```bash
# 1. Read-only. Inspects live state and shows exactly what would change.
scripts/release/bootstrap-production.sh --plan --output-directory ~/milo-prod-out

# 2. Guarded. Performs the idempotent bootstrap and then re-audits LIVE state.
scripts/release/bootstrap-production.sh --apply \
  --environment production --confirm-production-change \
  --release-sha "$(git rev-parse HEAD)" \
  --expected-project big-cabinet-457321-t7 \
  --expected-account <operator-email> \
  --output-directory ~/milo-prod-out \
  --wif-pool <pool-id> --wif-provider <provider-id> \
  --upstash-email-env MILO_UPSTASH_EMAIL \
  --upstash-apikey-env MILO_UPSTASH_APIKEY \
  --vercel-token-env MILO_VERCEL_TOKEN
```

Default mode (no flag) is `--plan` and is fully read-only.

## Adoption, not duplication

The default Secret Manager **resource names are the operator's existing ones**,
so the bootstrap reuses them rather than minting new ones:

| Logical secret | Resource name (default) | Override |
| --- | --- | --- |
| Supabase URL | `SUPABASE_URL` | `--supabase-url-secret` |
| Supabase server key | `SUPABASE_SECRET_KEY` | `--supabase-key-secret` |
| Provider key | `KIMI_API_KEY` | `--provider-key-secret` |
| Redis REST token | `UPSTASH_REDIS_REST_TOKEN` | `--redis-token-secret` |

Each resource is inspected and reported as one of four machine-readable states:
`REUSE_ENABLED`, `EXISTS_NO_ENABLED_VERSION`, `MISSING`, `INSPECTION_ERROR`.

**Inspect first, then prompt.** For every secret the tool: (1) describes the
resource; (2) lists enabled versions; (3) if an enabled version exists,
**adopts it without reading the payload and without prompting**; (4) if the
resource exists but has no enabled version, prompts only for *that* value; (5)
if the resource is missing, prompts only for *that* value before guarded
creation; (6) if inspection returns a permission/API error, returns
MANUAL/BLOCKED and **never prompts or creates blindly** (a failure is never
misread as "no enabled version").

Because the Supabase and provider secrets already exist in Secret Manager, you
do **not** copy those values into GitHub Environment secrets, and the runtime
Cloud Run services reference Secret Manager directly.

## What the guarded apply does (idempotently)

- Creates the distinct **API / worker / gateway** service accounts if missing
  (**never** a key) and enforces identity separation.
- **Adopts** existing Secret Manager resources; only creates/adds a version for
  a genuinely missing resource or one with no enabled version, and binds
  `roles/secretmanager.secretAccessor` **per-secret** to the single consumers.
- Discovers or creates a **dedicated production Upstash Redis** (management
  credentials pass through a chmod-600 curl config, never argv); stores the
  REST token in Secret Manager (never printed); records only the non-secret URL.
- **Configures the live Cloud Run API service and worker job** — not only their
  identities: env vars, Secret Manager references, `JOB_LAUNCHER=disabled`
  (API), every execution flag `false`, paid execution `false`, and nonzero
  budget caps, via `--update-env-vars` / `--update-secrets`. The worker job is
  **never executed**.
- **Adopts existing Vercel production variables** (inspects names, classifies
  each REUSE / CREATE / UPDATE) and sets only the managed vars —
  `GATEWAY_ALLOW_EXECUTION_ROUTES=false`,
  `NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI=false`, `UPSTASH_REDIS_REST_URL`,
  `UPSTASH_REDIS_REST_TOKEN` — with a real idempotent update path (remove +
  re-add for an existing variable). Never adds provider or Supabase server
  secrets to Vercel.
- **Verifies/adopts the Vercel→GCP Workload Identity Federation** chain when
  `--wif-pool` / `--wif-provider` are supplied (pool, provider/issuer, gateway
  `workloadIdentityUser` binding) and idempotently binds `roles/run.invoker`
  for the gateway SA on the API service (never `allUsers`).
- Generates the manifest + non-secret metadata, plus `bootstrap-plan.json` /
  `bootstrap-apply.json` in a **private operator directory outside the worktree**.

## The audit inspects LIVE configuration

After apply (and in `--audit-only`) the tool inspects the **live** Cloud Run
service/job describe output and verifies every required env var, Secret Manager
reference, execution flag (`false`), budget cap (nonzero), `JOB_LAUNCHER`, and
the runtime identities — then runs the consolidated read-only
`production-readiness.sh`. **`blocked = 0` is never claimed on the basis of the
self-generated manifest**: if the live configuration differs from the intended
state, the audit fails.

## Credentials the human still supplies (one-time)

Only the credentials that are **not** already in Secret Manager:

| Credential | Flag | Notes |
| --- | --- | --- |
| Upstash management email + API key | `--upstash-email-env` / `--upstash-apikey-env` | Redis discovery/creation. |
| Vercel access token | `--vercel-token-env` | Vercel identity proof + variable upserts. |
| Read-only PostgreSQL URL (optional) | `--database-url-env` | Migration-state audit. |

Supabase / provider secret **values** are supplied only if a resource is
genuinely missing or has no enabled version (`--supabase-key-env`,
`--provider-key-env`, `--supabase-url-env`, or `--prompt-secrets`).

## The apply guard

`--apply` requires **all** of: `--environment production`, `--expected-project`
matching the active gcloud project, `--expected-account` matching the active
gcloud account, `--release-sha` equal to `HEAD`, `--confirm-production-change`,
`MILO_OPERATOR_ACK=I_UNDERSTAND_THIS_CHANGES_PRODUCTION`, a clean worktree, and
non-placeholder inputs. A guard failure stops **before any mutation**.

## Safety invariants (never weakened)

- The worker job is **never executed**; execution + paid-execution flags stay
  off; no provider call; Cloud Run stays private; no service-account keys.
- Secret **values** never appear in stdout, stderr, JSON reports or artifacts,
  and never in a subprocess argv (Upstash auth uses a chmod-600 curl config).
- Permission/API errors are never classified as "missing" or "no version".
- Partial failure writes a `recovery_steps` plan, exits non-zero and never
  claims success. Re-running `--apply` is idempotent (adopted resources are
  left untouched).
- The bootstrap **never deploys**. Cloud Run and Vercel deployments remain
  distinct operator steps (`generate-deployment-plan.sh`).

## GitHub Actions

`.github/workflows/bootstrap-production.yml` runs the same script on
`workflow_dispatch` only. The plan job is always available; the apply job
requires a typed confirmation input plus `production` Environment approval, and
authenticates to Google Cloud through **Workload Identity Federation** (no
service-account keys). It supplies only the Upstash management credentials and
Vercel token as GitHub secrets — the Supabase / provider values are adopted
from Secret Manager and are **not** duplicated as GitHub secrets. Only redacted
reports are uploaded.

# Production activation runbook

Operational. Four commands, in order, from an authenticated operator shell.

Everything below uses the repository's canonical mechanisms. Nothing here is a
parallel deployment system, and no step re-implements the Government capture —
`scripts/catalog/government-production-capture.sh` runs the existing
`backend/catalog/operator_capture.py` entrypoint out of the existing worker
image, in a dedicated Cloud Run Job.

---

## 0. One-time, before the first run

```bash
gcloud auth login
gcloud config set project <GCP_PROJECT_ID>

cp config/production-operator.env.example config/production-operator.env
$EDITOR config/production-operator.env      # fill in the empty values
```

`config/production-operator.env` is git-ignored and holds **no secret values** —
only project/resource names, service-account emails and Secret Manager resource
*names*. Every command below reads it, which is why none of them need long
flag lists.

The entries with no default, which you must supply:

| Key | Where to get it |
| --- | --- |
| `GCP_PROJECT_NUMBER` | `gcloud projects describe <PROJECT_ID> --format='value(projectNumber)'` |
| `SUPABASE_PROJECT_REF` | Supabase dashboard → project settings (the `<ref>` in `https://<ref>.supabase.co`) |
| `GATEWAY_SERVICE_ACCOUNT` | the SA the Vercel gateway impersonates |
| `MILO_GATEWAY_AUDIENCE` | the audience the API requires of the gateway token |
| `MILO_APPROVED_GATEWAY_IDENTITIES` | that same SA email (comma-separated list) |
| `PRODUCTION_ORIGIN` | the exact browser origin, e.g. `https://milo.example.com` |
| `CAPTURE_CONVERSATION_ID` | an existing production conversation UUID |
| `CAPTURE_REQUESTED_BY` | your production user UUID |

If the project has never been provisioned, or you are unsure:

```bash
./scripts/deploy/gcp-bootstrap.sh --plan     # reports; changes nothing
./scripts/deploy/gcp-bootstrap.sh --apply    # creates only what is missing
```

It creates APIs, Artifact Registry, service accounts, secret *containers* and
the least-privilege IAM bindings. It never deletes anything and never adds a
secret value. Anything it prints as `NEEDS VALUE` is yours to populate once:

```bash
gcloud secrets versions add SUPABASE_URL --data-file=- --project=<PROJECT_ID>
```

---

## 1. The four commands

```bash
./scripts/deploy/production-preflight.sh
./scripts/catalog/government-production-capture.sh --all --enable-catalog-execution
DEPLOY_MODE=apply ./scripts/deploy/cloud-run.sh
./scripts/deploy/production-verify.sh
```

Or, the same sequence through the thin orchestrator:

```bash
./scripts/deploy/production-activate.sh --plan                            # read-only dry run first
./scripts/deploy/production-activate.sh --all --enable-catalog-execution  # then for real
```

Run `--plan` first. It performs the full preflight, prints the capture job
definition and the deployment plan, and mutates nothing.

### What each command does

**`production-preflight.sh`** — read-only. Proves gcloud is authenticated
against the expected project, the required APIs are enabled, the Cloud Run
service/job/registry and service accounts exist, every Secret Manager secret
exists with an enabled version, the capture identity can read the Supabase pair
and **cannot** read the provider key, the gateway binding is configured, no
worker execution is in flight, the migration head matches, and every
mandatory-for-paid RuntimePolicy dimension is bound. Failures name the exact
missing thing and the exact remediation command.

**`government-production-capture.sh --all --enable-catalog-execution`** —
creates (or updates) the bounded capture job, prepares the capture run, runs the
real capture against `data.gov.il`, waits for it to terminalize, then reads the
result back out of the database. Exits nonzero unless the snapshot is active,
`stored == declared`, and candidate variants exist.

`--enable-catalog-execution` is required and has no default. This repository
commits no enabled value for the catalog master switch — `check_unsafe_defaults.py`
enforces that — so turning it on is your explicit act, recorded in the command
you ran. It applies to the capture job only; the product worker never carries it.

**`cloud-run.sh`** — the existing canonical deployment. Resolves the release SHA
from the checked-out commit, builds and pushes both images tagged with that full
SHA, and binds `MILO_RELEASE_SHA` to the same value on **both** surfaces. It
refuses a short SHA, a wildcard CORS origin, a missing gateway identity, a
missing Supabase project pin, or any provider-key binding.

**`production-verify.sh`** — read-only, zero paid calls. Prints the release
identity, migration alignment, snapshot usability, queue readiness, policy
resolution, credential presence, API/worker liveness and the non-terminal run
count as `KEY=VALUE` lines.

---

## 2. Arming the website — a separate, deliberate step

The four commands above deploy and verify. They do **not** arm the website:
nothing in them enables run creation, paid execution or the Task Composer.

Ordering matters. Arm the backend first, then rebuild the frontend:

```bash
# Backend/runtime flags, on the deployed surfaces.
gcloud run services update <CLOUD_RUN_API_SERVICE> --region <REGION> \
  --update-env-vars MILO_ENABLE_RUN_CREATION=true,MILO_ENABLE_EXECUTION_CONTROL=true

gcloud run jobs update <CLOUD_RUN_WORKER_JOB> --region <REGION> \
  --update-env-vars MILO_ENABLE_PAID_EXECUTION=true,MILO_ENABLE_EXECUTION_CONTROL=true,MILO_ENABLE_CATALOG_EXECUTION=true,MILO_ENABLE_GOVERNMENT_CATALOG_READ=true,MILO_ENABLE_CATALOG_PROMOTION=false

# The worker also needs the provider credential, worker-only, at this point.
gcloud run jobs update <CLOUD_RUN_WORKER_JOB> --region <REGION> \
  --update-secrets KIMI_API_KEY=<PROVIDER_KEY_SECRET>:latest
```

Then the gateway and the UI, on Vercel:

| Variable | Kind | Value |
| --- | --- | --- |
| `CLOUD_RUN_API_URL` | runtime | the API service URL (`production-verify.sh` prints it) |
| `GCP_PROJECT_NUMBER`, `GCP_WORKLOAD_IDENTITY_POOL_ID`, `GCP_WORKLOAD_IDENTITY_POOL_PROVIDER_ID`, `GCP_SERVICE_ACCOUNT_EMAIL` | runtime | workload-identity federation for the gateway |
| `GATEWAY_ALLOW_EXECUTION_ROUTES` | runtime | `true` |
| `UPSTASH_REDIS_REST_URL`, `UPSTASH_REDIS_REST_TOKEN` | runtime | the shared rate-limit store |
| `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_ANON_KEY` | **build-time** | inlined into the browser bundle |
| `NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI` | **build-time** | `true` |

`NEXT_PUBLIC_*` values are inlined by Next.js at build time. Changing
`NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI` on an already-built deployment does
nothing to the served bundle — you must **redeploy the frontend** after setting
it, or the Task Composer keeps rendering *"Task submission is disabled until a
separately approved execution stage."*

Keep `MILO_ENABLE_CATALOG_PROMOTION=false`. Promotion requires read, read does
not imply promotion, and promotion is a separately authorized decision
([STAGED_ACTIVATION.md](STAGED_ACTIVATION.md)).

Then re-verify:

```bash
./scripts/deploy/production-verify.sh
```

---

## 3. Rollback

Every flag above is independently reversible, and none of it destroys data.

```bash
# Stop paid execution immediately (the kill switch).
gcloud run jobs update <WORKER_JOB> --region <REGION> \
  --update-env-vars MILO_ENABLE_PAID_EXECUTION=false

# Remove the provider credential entirely.
gcloud run jobs update <WORKER_JOB> --region <REGION> \
  --remove-secrets KIMI_API_KEY

# Close run creation at the API.
gcloud run services update <API_SERVICE> --region <REGION> \
  --update-env-vars MILO_ENABLE_RUN_CREATION=false
```

Disabling `MILO_ENABLE_CATALOG_EXECUTION` stops the worker reading candidates
and writing canonical facts; it deletes nothing, so re-enabling resumes from the
same durable state. Full detail in [ROLLBACK.md](ROLLBACK.md).

---

## 4. What the capture actually does

Pinned by `backend/catalog/government/source.py`; the operator tooling mirrors
these and `tests/test_production_operator_bundle.py` fails if they ever drift.

| Property | Value |
| --- | --- |
| Host (exact match) | `data.gov.il` |
| Package | `degem-rechev-wltp` |
| Resource (WLTP) | `142afde2-6228-49f9-8a29-9b6c3a0cbe40` |
| Page size / max accepted | 1000 / 1000 |
| Bounds | 200 pages, 120 000 records — fails closed **before** persisting |
| Response cap | 8 MiB, applied while reading |
| Timeouts | 10 s connect, 30 s read |
| Attempts | 3, retryable statuses only; deterministic refusals never retried |
| Credential | none — the dataset is public |

**Activation is last and gated.** A snapshot is born `pending` and the database
refuses activation unless it holds exactly as many records as the upstream
declared, so a prefix can never be activated. A crash, cancellation or lost
lease therefore leaves a non-active snapshot, which no reader will answer from.

**Re-running is safe.** Snapshot identity is derived from captured content, so
an identical re-capture collapses onto the same rows; changed content becomes a
new snapshot and never mutates the previous one; a failed refresh never replaces
the last valid active snapshot. The idempotency key returns the *same* prepared
run rather than creating a second one.

**There is no sample mode.** Because activation requires the full declared
record count, the smallest capture that yields a usable snapshot is the complete
WLTP resource. Do not invent a reduced mode; the application does not support one.

---

## 5. Why the first run still needs you

After section 2, the website is armed and the Task Composer is live — but no run
exists until a human submits one. That is deliberate: the first paid run is an
operator action, not an automated one. None of the tooling in this bundle
submits a task, and `production-verify.sh` reports
`PAID_CALLS_PERFORMED_BY_THIS_CHECK=NO` on every invocation.

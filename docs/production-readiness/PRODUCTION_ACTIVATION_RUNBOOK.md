# Production activation runbook

> **Superseded for the Mapping Plan → prepared batch → Swarm V2 path.** The
> gated, copy-paste rollout procedure is
> [SCOPED_BATCH_PRODUCTION_RUNBOOK.md](SCOPED_BATCH_PRODUCTION_RUNBOOK.md).
> Two things below changed: `production-activate.sh --all` now deploys (and so
> builds the worker image) **before** anything can capture, and stops before
> any capture; and readiness is proved for a NAMED, prepared plan revision
> (`production-verify.sh --gate prepared --work-scope-*`), never by "an active
> Government snapshot exists". Sections 0, 3 and 4 remain accurate background.

Operational. Four commands to deploy and verify, then one deliberate step to
open the website — from an authenticated operator shell.

**PR #111 wired the frontend to the canonical path; Production is still
locked.** Sections 0–1 bring the backend to a verified, usable state with the
website OFF. Section 2 is the separate decision that opens it.

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
| `MILO_WORKER_AUDIENCE` | audience the worker's ID token carries; **required** before Stage 2 or the API will not start |

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

## 1. Deploy and verify (website stays locked)

```bash
./scripts/deploy/production-preflight.sh
./scripts/deploy/production-verify.sh --gate database     # the exact migration set
DEPLOY_MODE=apply ./scripts/deploy/cloud-run.sh          # builds the worker image FIRST
./scripts/deploy/production-verify.sh --gate deployed
# only now may anything run the capture job, which runs that image:
./scripts/catalog/government-production-capture.sh --all --enable-catalog-execution   # optional whole register
```

Or, the same sequence through the thin orchestrator:

```bash
./scripts/deploy/production-activate.sh --plan    # read-only dry run first
./scripts/deploy/production-activate.sh --all     # preflight, database gate, deploy, verify — then STOP
```

`--all` deliberately **stops before** any capture, preparation or website
step. The scoped preparation needs a Mapping Plan a person authors in the
website first; see the new runbook. The capture script itself now refuses to
create or execute the capture job unless the release worker image exists and
the job runs exactly it.

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
result back out of the database. It verifies exactly the snapshot the capture's
own execution document names (`capture.active_snapshot_key`), never "the newest
Government snapshot" (which may be a scoped manufacturer capture). Exits nonzero
unless the capture reported `succeeded` and that snapshot exists, is the whole
register (not a scoped capture) of the pinned resource, is complete and active,
has `stored == declared`, and has candidate variants.

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

The website is **not** armed by section 1. PR #111 wired the frontend to the
canonical path, but Production is deliberately still locked behind gates on
four different surfaces. The full chain, with what each one does when it is
off, is generated from the repository:

```bash
python3 scripts/release/execution_gate_chain.py            # all stages
python3 scripts/release/execution_gate_chain.py --stage 2  # what Stage 2 opens
```

### Two gates that are easy to miss

Read these before anything else, because each produces a symptom that looks
like a product bug rather than a configuration gap:

1. **`JOB_LAUNCHER` defaults to `disabled`** and is not an `MILO_ENABLE_*` flag.
   Left alone, `build_job_launcher()` returns the no-op launcher: the run row
   **is** created and the website shows it, and **nothing ever executes it**.
   Run creation being on does not launch anything by itself. It must be
   `cloud_run`.
2. **`MILO_ENABLE_EXECUTION_CONTROL` requires `MILO_WORKER_AUDIENCE` and
   `MILO_APPROVED_WORKER_IDENTITIES`** on the API. With the flag on and either
   unset, `production_config.py` raises `WORKER_AUTH_AUDIENCE_MISSING` /
   `WORKER_ALLOWLIST_EMPTY` and **the API fails to start** — enabling the flag
   alone takes Production down rather than opening a route. Set all three in
   the same update; `website-execution-activate.sh` refuses to proceed without
   them.

### Stage 2, in one command

```bash
./scripts/deploy/website-execution-activate.sh --plan           # prints everything, changes nothing
./scripts/deploy/website-execution-activate.sh --apply-backend  # applies the Cloud Run half
```

It re-runs the Stage 1 gate first and **refuses** if the snapshot is unusable
or the release SHAs disagree — opening the composer over a release that would
refuse every run is a trap, not an activation. It never enables promotion and
never starts a run.

The Cloud Run half it applies:

| Surface | Set |
| --- | --- |
| API service | `MILO_ENABLE_RUN_CREATION`, `MILO_ENABLE_EXECUTION_CONTROL`, `MILO_ENABLE_RUN_CANCELLATION`, `JOB_LAUNCHER=cloud_run`, `MILO_WORKER_AUDIENCE`, `MILO_APPROVED_WORKER_IDENTITIES` |
| Worker job | `MILO_ENABLE_EXECUTION_CONTROL`, `MILO_ENABLE_PAID_EXECUTION`, `MILO_ENABLE_CATALOG_EXECUTION`, `MILO_ENABLE_GOVERNMENT_CATALOG_READ`, `MILO_ENABLE_CATALOG_PROMOTION=false`, plus the provider secret **worker-only** |

### The Vercel half — and why it needs a rebuild

The script **prints** these rather than running them: they need Vercel
credentials that must not live in this repository, and one of them is not an
environment change at all.

| Variable | Kind | How it takes effect |
| --- | --- | --- |
| `CLOUD_RUN_API_URL` | runtime | new deployment |
| `GCP_PROJECT_NUMBER`, `GCP_WORKLOAD_IDENTITY_POOL_ID`, `GCP_WORKLOAD_IDENTITY_POOL_PROVIDER_ID`, `GCP_SERVICE_ACCOUNT_EMAIL` | runtime | new deployment |
| `GATEWAY_ALLOW_EXECUTION_ROUTES` | runtime | new deployment |
| `UPSTASH_REDIS_REST_URL`, `UPSTASH_REDIS_REST_TOKEN` | runtime | new deployment |
| `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_ANON_KEY` | **build-time** | **REBUILD** |
| `NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI` | **build-time** | **REBUILD** |

> **`NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI` is inlined into the browser bundle
> by Next.js at build time.** Setting it in the Vercel dashboard does **not**
> change an already-built deployment. The Task Composer will keep rendering
> *"Task submission is disabled until a separately approved execution stage."*
> until the frontend is **rebuilt and redeployed** (`vercel --prod --force`).
> This is the single most likely thing to go wrong tomorrow.

### Confirm — without creating a run

```bash
./scripts/deploy/website-execution-check.sh
```

It reports four facts **separately**, because they fail in different layers:

```
FRONTEND_CODE_WIRED=YES          # this checkout has the canonical route
TASK_COMPOSER_VISIBLE=...        # read from the SERVED bundle
GATEWAY_EXECUTION_ENABLED=...    # Vercel runtime value
BACKEND_EXECUTION_ARMED=...      # every API + worker gate, read from Cloud Run
WEBSITE_EXECUTION_STAGE_ACTIVE=  # YES only when all four are satisfied
```

It never sends the run-creation `POST`. The gate is proved from configuration;
exercising it would create a run, and the first run is yours.

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

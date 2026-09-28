# Operating production without Cloud Shell (plan decision 23)

Six GitHub Actions workflows replace the Cloud Shell operator steps. Each one
wraps the canonical script for its step, unchanged, and writes a
`SUMMARY|<step>|<result>|<detail>` line per step to the job summary, so the
GitHub mobile app shows what happened without opening a log.

| Workflow | Script | Environment | What it does |
|---|---|---|---|
| `deploy.yml` (inputs `sha`, `restore_website_stage`) | `scripts/ops/deploy.sh` | `production` (reviewer) | the R block: CI green on that exact SHA, migrations fully applied, Vercel built that SHA with run starts closed, 0 live runs, Stage 2 reset, `production-activate.sh --all`, worker model env, worker contract (`MILO_CAPTURE_REPLAY=false`, no `KIMI_API_KEY`), deployed gate; then, only after all of that passed, step 11 turns the website's plan tools back on (below) |
| `website-stage.yml` (input `stage`, optional `sha`) | `scripts/ops/website-stage.sh` | `production` (reviewer) | the website's plan tools without a deploy: `plan-authoring` (Stage P), `web-preparation` (E', the Prepare button) or `both`; never Stage 2 |
| `kill-switch.yml` | `scripts/ops/kill-switch.sh` → `scripts/deploy/kill-switch.sh` | `production-kill-switch` (**no** reviewer) | the canonical emergency order; confirm with `KILL` |
| `capture-flag.yml` (input `on`/`off`) | `scripts/ops/capture-flag.sh` | `production` | `MILO_CAPTURE_REPLAY` on the worker job only; `on` refused unless 0 live runs; turn it off after one run |
| `gates.yml` (input `gate`, optional revision) | `scripts/ops/gates.sh` | `production` | read-only `production-verify.sh --gate <gate>` |
| `arm.yml` (inputs: plan id / revision / digest) | `scripts/ops/arm.sh` | `production` (reviewer) | `website-execution-activate.sh --apply-runtime-policy`, then `--apply-backend`, then the armed gate; confirm with `ARM` |

Every workflow is `workflow_dispatch` only, refuses to run from anything but
`main`, and is serialized (`milo-production-operations`; the kill switch has its
own group so it never waits behind a deploy). Every one has a `dry_run` input:
the script prints every command and calls nothing.

## The website's plan tools after a deploy

A Stage A deploy (`MILO_PERMANENT_MODE=false`) turns off
`MILO_ENABLE_WORK_SCOPE_MUTATIONS` (Stage P, editing a plan) and
`MILO_ENABLE_WORK_SCOPE_PREPARATION_REQUESTS` (E', the Prepare button).
`deploy.yml`'s `restore_website_stage` (default `both`) turns them back on as
step 11, reached only when steps 1-10 passed -- a failed deploy restores
nothing:

| Value | Canonical tools, in order |
|---|---|
| `plan-authoring` | `website-execution-activate.sh --apply-plan-authoring` |
| `web-preparation` | `government-production-capture.sh --ensure-job --enable-catalog-execution`, then `website-execution-activate.sh --apply-web-preparation` |
| `both` | the two above, in that order |
| `none` | nothing |

Each runs behind its own deployed gate and reads back that run creation, paid
execution and scoped preparation stay OFF on the API. Stage 2 is never touched:
paid execution stays with `arm.yml`. In permanent mode step 11 is skipped (the
live stage was kept). `website-stage.yml` runs the same steps on their own; it
checks out the release production runs (MILO_RELEASE_SHA on the API and the
worker, or the `sha` input), because both tools act on `git rev-parse HEAD`.
The Vercel half of Stage P is not changed by a deploy, so it needs nothing here.

## The credentials file of the auth step

`google-github-actions/auth` writes `gha-creds-<hash>.json` into the checkout.
`.gitignore`, `.dockerignore` and `.gcloudignore` exclude `gha-creds-*.json`, so
it neither makes `release:worktree-clean` dirty nor reaches the Cloud Build
source bucket (`.gcloudignore` is otherwise exactly gcloud's generated default:
`.gcloudignore`, `.git`, `.gitignore`, `#!include:.gitignore`). Every workflow
runs `git status --porcelain` right after authenticating and stops if anything
but an ignored file appeared. A release older than this change has none of
that, so `deploy.yml` cannot deploy it.

**Opening run starts on the website stays a person's step in Vercel**
(`GATEWAY_ALLOW_RUN_START_ROUTES`), after `arm.yml` passes the armed gate.

## One-time setup

### Google Cloud (Cloud Shell, once)

```bash
git clone https://github.com/giladscore494/milo-agent-workspace.git && cd milo-agent-workspace
cp config/production-operator.env.example config/production-operator.env   # fill in, as today
gcloud auth login && gcloud config set project big-cabinet-457321-t7
bash scripts/ops/setup-wif.sh --plan     # read-only: lists every CREATE / UPDATE / BIND
bash scripts/ops/setup-wif.sh --apply    # makes exactly those changes; re-run --plan: 0 changes
```

It creates the workload identity pool `milo-github`, the OIDC provider
`github-actions` whose attribute condition admits **only**
`assertion.repository == 'giladscore494/milo-agent-workspace' && assertion.ref ==
'refs/heads/main' && assertion.environment in ['production',
'production-kill-switch']`, and the deploy service account
`milo-github-deployer@<project>.iam.gserviceaccount.com` with these roles and
nothing else:

| Role | Where | Needed by |
|---|---|---|
| `roles/run.admin` | project | deploy / update the API service and jobs, their IAM bindings, kill switch, capture flag, arm, website stage (capture job `--ensure-job` and its run-with-overrides binding) |
| `roles/cloudbuild.builds.editor` | project | `gcloud builds submit` (both images) |
| `roles/artifactregistry.reader` | project | image describes (capture script `--ensure-job`, verify, preflight) |
| `roles/secretmanager.viewer` | project | preflight: secret metadata and IAM policy, never a value |
| `roles/iam.serviceAccountViewer` | project | preflight: service-account describes |
| `roles/serviceusage.serviceUsageConsumer` | project | builds submit / services list |
| `roles/logging.viewer` | project | build log streaming, capture execution documents |
| `roles/storage.admin` | the `gs://<project>_cloudbuild` bucket only | Cloud Build source upload |
| `roles/iam.serviceAccountUser` | the API, worker and capture service accounts only | deploying AS those identities |
| `roles/iam.workloadIdentityUser` | the deploy SA, for this repository's principals only | the keyless login |

**No JSON key exists anywhere.** If `gcloud builds submit` is refused for
`iam.serviceAccounts.actAs` on the project's default Cloud Build/compute
identity, grant `roles/iam.serviceAccountUser` on that one account to the
deploy SA; the script does not do that for you, because that identity is often
broad.

### GitHub (Settings, once)

- **Environments → `production`**: required reviewer = you; deployment branches
  = `main` only. Secret `MILO_READONLY_DB_URL` (the read-only connection string).
- **Environments → `production-kill-switch`**: **no** required reviewer;
  deployment branches = `main` only. Secret `VERCEL_TOKEN` (see Vercel below).
- **Repository variables**: `GCP_WORKLOAD_IDENTITY_PROVIDER`,
  `GCP_DEPLOY_SERVICE_ACCOUNT`, `GCP_PROJECT_ID` (printed by `setup-wif.sh`);
  `MILO_OPERATOR_CONFIG` (the contents of your `production-operator.env` --
  identifiers only; `READONLY_DATABASE_URL_ENV=MILO_READONLY_DB_URL`);
  `MILO_PERMANENT_MODE=false`.

### Vercel (separate; kill switch only)

The deploy only READS the website (`/api/deployment-status`), so it needs no
Vercel credential. The kill switch changes Vercel variables and redeploys, so it
needs: a Vercel access token scoped to the MILO project's team (secret
`VERCEL_TOKEN` in `production-kill-switch`), and repository variables
`VERCEL_ORG_ID`, `VERCEL_PROJECT_ID` (from `frontend/.vercel/project.json` after
`vercel link`) and `VERCEL_CLI_VERSION` (a pinned version, e.g. the one you run
locally).

## The read-only database URL, and its rotation

The role password is rotated per session today. After each rotation:

```text
GitHub → Settings → Environments → production → MILO_READONLY_DB_URL → Update
```

(or `gh secret set MILO_READONLY_DB_URL --env production` from a trusted
machine; never paste it into a workflow input, a commit or an issue). The old
value stops working at rotation, so a workflow run with a stale secret fails
closed at its first read (`migrations`, `live-runs` or a gate reads UNVERIFIED).
The value is only ever handed to psql through the environment of the one step
that needs it; `tests/test_ops_workflows.py` fails if any workflow or script
could print it.

The role must be able to read every public table (`check-migration-state.sh`
reports `READONLY_ROLE_LACKS_SELECT on <table>` otherwise), with default
privileges so new migrations' tables are readable too.

## Permanent operating mode

`MILO_PERMANENT_MODE` (repository variable) stays **`false`**. With `false`,
`deploy.yml` resets Stage 2: it removes the worker's provider key binding and
forces a Stage A deploy of API and worker; Stage 2 is re-armed deliberately
with `arm.yml`.

With `true`, `deploy.yml` deploys with `--preserve-stage`
(`DEPLOY_PRESERVE_STAGE=1` in `cloud-run.sh`): only the images and the release
identity change, no execution flag and no `JOB_LAUNCHER` is written, the worker
keeps its provider key, and the deploy fails unless every execution flag and
`JOB_LAUNCHER` reads back exactly as before. The API is still refused a provider
key, and `MILO_CAPTURE_REPLAY` is still pinned off.

**Rule:** switch it to `true` only after two or three consecutive clean runs
(each: armed through `arm.yml`, one batch run finished `completed` or
`partial_success`, gates `active` passing, nothing reconciled by hand).

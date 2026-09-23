# Production rollout: Mapping Plan → prepared batch → Swarm V2

This is the operator procedure for bringing Production to the point where a
person can start the **first paid batch run from the website**. It is gated:
each stage lists its commands, the evidence you should see, the condition that
means **stop**, and how to recover or roll back.

This document does not claim Production is live. Nothing in it has been run
against Production by the change that introduced it. Every command comes from
this repository. Three kinds of operation cannot be done from Google Cloud
alone, and the stage that needs each one says so:

| Operation | Where it runs |
| --- | --- |
| Supabase backup and migrations | GitHub Actions workflows (GitHub UI, or `gh` from any shell) |
| Plan authoring and the first run | The production website, signed in as a project member |
| Vercel environment variables and redeploy | Vercel dashboard or Vercel CLI |
| Everything else | Google Cloud Shell, in a checkout of the release commit |

## The path, and why it is gated like this

```
Mapping Plan (website, a person)          catalog_work_scopes / _revisions
  └─ revision N + digest                  immutable, digest = sha256(scope_text)
      └─ preparation (capture job, once)  catalog_work_scope_preparations/_units
          └─ per verified manufacturer:   a SCOPED Government snapshot
              └─ batches of ≤20           catalog_work_scope_batches/_queue_items
                  └─ "Start batch 1"      create_work_scope_batch_run → ONE Swarm V2 run
                      └─ worker           reads exactly that batch; refuses anything unbound
```

The rules below are enforced in code. The checks in this runbook prove them;
they do not assume them.

- With the Government read on, the worker refuses every Swarm V2 run that is
  not bound to a batch (`GOVERNMENT_BATCH_REQUIRED`). The API refuses to create
  one (`CATALOG_RUN_REQUIRES_MAPPING_PLAN`), and the website sends catalog work
  to the Mapping Plan instead of the task composer.
- **Readiness is proved for one named plan revision.** `production-verify.sh`
  and `work-scope-readiness.sh` check that revision's digest, its preparation,
  each prepared unit's scoped snapshot and the exact batch linkage. An active
  Government snapshot on its own is only reported as information.
- **Each flag is set only on the component that needs it.** The names are in
  `scripts/deploy/deployment-contract.sh`:

  | Flag | Stage A | Stage P | Stage 2 API | Stage 2 worker | Capture job |
  | --- | --- | --- | --- | --- | --- |
  | `MILO_ENABLE_WORK_SCOPE_MUTATIONS` | off | **API on** | on | off | — |
  | `MILO_ENABLE_WORK_SCOPE_BATCHES` | off | off | on | off | — |
  | `MILO_ENABLE_WORK_SCOPE_PREPARATION` | off | off | off | off | on for **one execution** only |
  | `MILO_ENABLE_RUN_CREATION` | off | off | on | — | pinned off |
  | `MILO_ENABLE_EXECUTION_CONTROL` | off | off | on | on | pinned off |
  | `MILO_ENABLE_RUN_CANCELLATION` | off | off | on | — | — |
  | `MILO_ENABLE_PAID_EXECUTION` | off | off | off | **on** | pinned off |
  | `MILO_ENABLE_CATALOG_EXECUTION` | off | off | on (routing mirror) | on | on (operator flag) |
  | `MILO_ENABLE_GOVERNMENT_CATALOG_READ` | off | off | on (routing mirror) | on | pinned off |
  | `MILO_ENABLE_CATALOG_PROMOTION` | off | off | off | off | pinned off |
  | `JOB_LAUNCHER` (API) | `disabled` | `disabled` | `cloud_run` | — | — |
  | `KIMI_API_KEY` secret | none | none | **never** | bound | never |

  The website has two separate gateway permissions (Vercel):

  | Vercel variable | Stage A | Stage P | Stage 2 arming (E.1–E.2) | Last step (E.3) |
  | --- | --- | --- | --- | --- |
  | `NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI` (build time) | off | on | on | on |
  | `GATEWAY_ALLOW_EXECUTION_ROUTES` (plan writes, pause/resume, cancel) | off | on | on | on |
  | `GATEWAY_ALLOW_RUN_START_ROUTES` (any run start, including "Start batch") | off | **off** | **off** | **on** |

  **Starting a run is the last permission the website gets.** Until E.3, the
  gateway refuses every run start (`403` before authentication), whatever the
  API allows. In E.1 the worker is armed and read back before the API, so the
  API never allows a start that a half-armed worker would receive. E.2 is a
  pre-open gate that requires run starts to be *proved* closed, and E.3 opens
  them.

  On the API, the two catalog flags build nothing. They only tell run creation
  that Swarm V2 runs read the catalog, so an ordinary run is refused before it
  exists instead of being launched only for the worker to refuse it.

## Manufacturer coverage: what can run today

The directory lists 39 manufacturers. The committed register evidence verifies
a Government-register spelling for **one** of them: `toyota → טויוטה`. Every
other manufacturer can be put in a plan, but preparation records it as
`register_unverified` and queues nothing for it. The Mapping Plan shows this
before preparation ("Can be prepared from the Government register: Toyota.")
and the readiness check shows it afterwards (`UNIT … state=register_unverified`).

Even Toyota can come back `vocabulary_insufficient`: if most of its in-range
register rows are ones the reviewed vocabulary cannot read, it queues nothing.
That is a real blocker. The fix is a reviewed vocabulary change, never a
looser normalization. Stage D stops on it.

---

## Stage A — read-only checks

Nothing in this stage changes anything.

### A.1 Cloud Shell and the release commit

```bash
# In Google Cloud Shell. The repository is private: authenticate git first
# (for example `gh auth login`, or a fine-grained read token).
git clone https://github.com/giladscore494/milo-agent-workspace.git
cd milo-agent-workspace
git fetch origin main
git checkout --detach origin/main
export RELEASE_SHA="$(git rev-parse HEAD)"; echo "$RELEASE_SHA"
git status --porcelain            # must print nothing

gcloud auth login
gcloud config set project big-cabinet-457321-t7
```

Every script builds, tags and checks `git rev-parse HEAD`, so **the checkout is
the release**. Keep this one checkout for the whole rollout.

Check CI for that exact commit in GitHub (Actions → `ci` → the run for
`$RELEASE_SHA`). All three jobs (`offline-checks`, `postgres-checks`, `e2e`)
must be green. From a shell with `gh` authenticated:

```bash
gh run list --workflow ci --commit "$RELEASE_SHA" --repo giladscore494/milo-agent-workspace
```

**Stop if** CI is not green for `$RELEASE_SHA`, or the worktree is dirty.

### A.2 Operator configuration and the read-only database URL

```bash
cp config/production-operator.env.example config/production-operator.env
"${EDITOR:-nano}" config/production-operator.env
```

Fill in every empty value. Section 0 of
[PRODUCTION_ACTIVATION_RUNBOOK.md](PRODUCTION_ACTIVATION_RUNBOOK.md) explains
where each one comes from. The file is git-ignored and holds no secret values.
The capture job needs `CAPTURE_CONVERSATION_ID` and `CAPTURE_REQUESTED_BY`, and
Stage 2 needs `MILO_WORKER_AUDIENCE`. `ALLOWED_CORS_ORIGINS` defaults to
`PRODUCTION_ORIGIN`.

The readiness checks need a **read-only** PostgreSQL connection string for a
role that bypasses row-level security, such as Supabase's
`supabase_read_only_user`. Every catalog table has RLS on and no policies. A
role without `BYPASSRLS` reads zero rows, and the tools report that as
`UNVERIFIED`, never as "empty". Get the string from Supabase with your own
authorized credential, then enter it without echoing it or saving it in shell
history:

```bash
read -rs MILO_READONLY_DB_URL && export MILO_READONLY_DB_URL
```

### A.3 The whole read-only plan

```bash
bash scripts/deploy/production-activate.sh --plan 2>&1 | tee "$HOME/stage-a-plan.txt"
```

This runs the preflight, the database gate, `DEPLOY_MODE=check cloud-run.sh`,
the capture plan, the gate chain and the activation plan. It changes nothing.

### A.4 Migration state: exactly three pending

```bash
bash scripts/release/check-migration-state.sh --database-url-env MILO_READONLY_DB_URL
bash scripts/deploy/work-scope-readiness.sh --schema-only
```

**Expected evidence** (from the last recorded audit):

- `remote schema classified as partially-migrated (38/41 …)`
- `3 local migration(s) not present in remote migration history:` naming
  `20260922000100 …catalog_work_scopes.sql`,
  `20260923000100 …catalog_work_scope_preparation.sql` and
  `20260924000100 …catalog_work_scope_batch_runs.sql`
- `WORK_SCOPE_SCHEMA=NO (missing tables: … missing RPCs: …)`

**Stop if** any other version is missing or unexpected, the history is not an
exact prefix (`drift`), or a marker disagrees. Those are drift. Do not apply
anything until they are explained.

### A.5 Cloud Run, images, IAM and live flags

```bash
gcloud run services describe milo-agent-api --region us-central1 \
  --format='value(spec.template.spec.containers[0].image)'
gcloud run jobs describe milo-agent-worker --region us-central1 \
  --format='value(spec.template.spec.template.spec.containers[0].image)'
gcloud artifacts docker images list \
  us-central1-docker.pkg.dev/big-cabinet-457321-t7/milo-agent/worker --include-tags \
  --filter="tags:${RELEASE_SHA}"          # empty until Stage C builds it
gcloud run services get-iam-policy milo-agent-api --region us-central1 --format=json
bash scripts/deploy/website-execution-check.sh
```

The website check prints every backend flag's actual value
(`Backend gate values:`). Before Stage C, expect
`WEBSITE_EXECUTION_STAGE_ACTIVE=DISABLED` or `UNVERIFIED`. Expect
`FRONTEND_RELEASE=UNVERIFIED` until a frontend with `/api/deployment-status`
is deployed.

**Stop if** `allUsers` or `allAuthenticatedUsers` appears in any IAM policy,
or any non-terminal run exists (`RUNS_QUIESCENT` in A.3).

### A.6 Two Stage A preflight rules that can clash with live configuration

Neither rule is to be disabled. Each has a safe fix.

1. **A live provider key.** `cloud-run.sh` refuses to deploy while
   `KIMI_API_KEY` or `MOONSHOT_API_KEY` is bound to the live API or worker. An
   earlier stage may have left one bound. Check its form, then remove it with
   the matching command (from
   [DEPLOYMENT.md](DEPLOYMENT.md#legacy-provider-bindings-the-deployment-stops-before-touching-anything)):

   ```bash
   gcloud run jobs describe milo-agent-worker --region us-central1 --format=json \
     | python3 -c 'import json,sys; [print(("secret " if (e.get("valueFrom") or {}).get("secretKeyRef") else "env ")+e["name"]) for e in json.load(sys.stdin)["spec"]["template"]["spec"]["template"]["spec"]["containers"][0].get("env",[]) if e["name"] in ("KIMI_API_KEY","MOONSHOT_API_KEY")]'
   gcloud run jobs update milo-agent-worker --region us-central1 --remove-secrets KIMI_API_KEY   # "secret" form
   gcloud run jobs update milo-agent-worker --region us-central1 --remove-env-vars KIMI_API_KEY  # "env" form
   ```

   Removing the binding only takes capability away. The secret itself is not
   touched, and Stage E binds it again deliberately.

2. **Unbound RuntimePolicy.** `production-preflight.sh` blocks
   (`runtime-policy:mandatory-bindings`) while any mandatory-for-paid
   RuntimePolicy value is missing from the live worker. `cloud-run.sh` does not
   bind them. Bind the reviewed values, computed from `backend/runtime_policy.py`
   (worker: the full envelope; API: the concurrency caps):

   ```bash
   bash scripts/deploy/website-execution-activate.sh --plan          # shows the exact values
   bash scripts/deploy/website-execution-activate.sh --apply-runtime-policy
   ```

   This step enables nothing. It sets the limits paid execution will run
   within, and reads each value back. Stage C's deploy keeps them, because it
   updates configuration without removing anything.

**Recovery:** re-run A.3 after each fix. It must now pass.

---

## Stage B — apply the three pending migrations

Migrations are applied only through the repository's GitHub Actions
workflows: `.github/workflows/backup-supabase-production.yml`, then
`.github/workflows/deploy-supabase-migrations.yml`. Each is bound to one
reviewed SHA and needs a typed confirmation. **Do not paste migration SQL into
the Supabase SQL editor.** A partial or hand-ordered apply leaves the history
and the schema out of step. Cloud Shell only verifies (B.4).

### B.1 Encrypted backup

GitHub → Actions → **Backup Supabase Production** → *Run workflow* on `main`,
with `expected_sha = $RELEASE_SHA` and
`confirmation = CREATE_ENCRYPTED_PRODUCTION_BACKUP`. Or with `gh`:

```bash
gh workflow run backup-supabase-production.yml --repo giladscore494/milo-agent-workspace \
  --ref main -f expected_sha="$RELEASE_SHA" -f confirmation=CREATE_ENCRYPTED_PRODUCTION_BACKUP
```

Verify the artifact as described in [SUPABASE_BACKUP.md](SUPABASE_BACKUP.md)
("Verify a backup before a migration"). **Stop if** the run is not green or
the backup does not verify.

### B.2 Dry run

**Deploy Supabase Migrations** → *Run workflow* with `mode = dry-run` and
`expected_sha = $RELEASE_SHA`:

```bash
gh workflow run deploy-supabase-migrations.yml --repo giladscore494/milo-agent-workspace \
  --ref main -f mode=dry-run -f expected_sha="$RELEASE_SHA"
```

**Expected evidence:** the step *Run mandatory production dry-run preflight*
(`supabase db push --linked --dry-run`) proposes **exactly** the three
migrations above, in that order, and nothing else. The run ends with
`Manual dry-run completed. No production migrations were applied.`

**Stop if** it proposes anything else, or fails. The workflow prints its
`SAFE_FAILURE_MESSAGE`. Compare the history with the repository before doing
anything.

### B.3 Apply

The same workflow with `mode = apply`, `expected_sha = $RELEASE_SHA` and
`confirmation = APPLY_PRODUCTION_MIGRATIONS`:

```bash
gh workflow run deploy-supabase-migrations.yml --repo giladscore494/milo-agent-workspace \
  --ref main -f mode=apply -f expected_sha="$RELEASE_SHA" -f confirmation=APPLY_PRODUCTION_MIGRATIONS
```

**Expected evidence:** *Apply production migrations* succeeds, and *Display
remote migration history after apply* lists all 41 versions.

### B.4 Verify in Cloud Shell (read-only)

```bash
bash scripts/release/check-migration-state.sh --database-url-env MILO_READONLY_DB_URL
bash scripts/deploy/work-scope-readiness.sh --schema-only
bash scripts/deploy/production-verify.sh --gate database
```

**Expected evidence:** `remote schema classified as fully-migrated (41/41 …)`,
then `WORK_SCOPE_SCHEMA=VERIFIED` (8 tables with RLS on, and 9 RPCs that only
`service_role` can execute; neither `anon` nor `authenticated` can), then
`DATABASE_READY=VERIFIED` and `RESULT: OK`.

The executable test suite applies the same three files to a database shaped
like Production (38/41) and asserts exactly this transition
(`tests/test_migrations_postgres.py::test_the_production_shaped_database_is_named_exactly_as_three_migrations_short`).

**Recovery / rollback.** All three migrations only add objects; they alter no
existing relation's rows. If the apply fails part way, re-run B.2: the history
shows exactly what was applied, and the next apply continues from there. The
live release does not call these objects until Stage C deploys the new code.
There is no down-migration (see [ROLLBACK.md](ROLLBACK.md) §Migrations).
Restore from the B.1 backup only as a last resort, and only with explicit
approval.

---

## Stage C — build and deploy the release with execution off

```bash
bash scripts/deploy/production-activate.sh --all 2>&1 | tee "$HOME/stage-c.txt"
```

What `--all` does, in this order, and nothing else:

1. `production-preflight.sh`: read-only.
2. `production-verify.sh --gate database`: stops unless the exact migration
   set is applied.
3. Checks whether this release is already deployed. If it is, the deploy is
   **skipped**, so a resumed run never resets later stages. `--force-redeploy`
   overrides this.
4. `DEPLOY_MODE=apply cloud-run.sh`, with targets taken from the operator
   config (a shell exporting a different project is refused). It builds and
   pushes `api:$RELEASE_SHA` and `worker:$RELEASE_SHA`, then deploys the worker
   job and the API at **Stage A**: every execution flag false,
   `JOB_LAUNCHER=disabled`, no provider key. It executes nothing.
5. `production-verify.sh --gate deployed`.

It never runs the capture job, prepares anything, starts a run or enables paid
execution.

**Expected evidence:** `Deployment complete. Worker job was deployed but not
executed.`, `CODE_DEPLOYED=VERIFIED`, `DATABASE_READY=VERIFIED` and
`RESULT: OK — every fact gate deployed requires is VERIFIED`. The image must
now exist:

```bash
gcloud artifacts docker images describe \
  "us-central1-docker.pkg.dev/big-cabinet-457321-t7/milo-agent/worker:${RELEASE_SHA}"
```

**Frontend.** Merging to `main` makes Vercel build a Production deployment of
the same commit (Production Branch: `main`), with the execution UI still off.
Confirm:

```bash
bash scripts/deploy/website-execution-check.sh
# FRONTEND_RELEASE=VERIFIED (built from $RELEASE_SHA)
# TASK_COMPOSER_VISIBLE=DISABLED   GATEWAY_EXECUTION_ENABLED=DISABLED
```

If `FRONTEND_RELEASE` is `UNVERIFIED` because the deployment states no commit,
make sure the Vercel project's *Automatically expose System Environment
Variables* setting is on (that is how `VERCEL_GIT_COMMIT_SHA` reaches the
server), then redeploy the release commit through the Vercel Git integration
(dashboard → Deployments → that commit → *Redeploy*). It is never overridden by
hand.

**Stop if** the preflight blocks (A.6 covers the two rules that can clash with
live configuration), or the build, the deploy or its post-deploy checks fail.

**Rollback:** [ROLLBACK.md](ROLLBACK.md) §Cloud Run API
(`gcloud run services update-traffic milo-agent-api --to-revisions <PREVIOUS_REVISION>=100`)
and §Cloud Run worker
(`gcloud run jobs update milo-agent-worker --image …/worker:<PREVIOUS_SHA>`).
Execution is off, so nothing is running that would need stopping.

---

## Stage D — author the plan, then capture and prepare it

### D.1 Stage P: plan authoring only

```bash
bash scripts/deploy/website-execution-activate.sh --apply-plan-authoring
```

This passes through the `deployed` gate, sets `MILO_ENABLE_WORK_SCOPE_MUTATIONS=true`
on the API **only**, and reads it back. It also proves that
`MILO_ENABLE_RUN_CREATION` and `MILO_ENABLE_WORK_SCOPE_BATCHES` are still
false. It then prints the Vercel half, which you apply in Vercel (this is not
a Google Cloud operation):

```bash
# In a checkout linked to the Vercel project (or in the Vercel dashboard →
# Settings → Environment Variables → Production). Replace an existing value
# with `vercel env rm NAME production --yes` first.
vercel env add GATEWAY_ALLOW_EXECUTION_ROUTES production        # true (plan writes)
vercel env add NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI production  # true (inlined at BUILD time)
vercel env add CLOUD_RUN_API_URL production                     # the API URL printed above
# Run starts stay CLOSED until E.3. If the variable exists, remove it:
vercel env rm GATEWAY_ALLOW_RUN_START_ROUTES production --yes
```

Then **rebuild without the build cache**: dashboard → Deployments → the
Production deployment of `$RELEASE_SHA` → *Redeploy*, with *Use existing Build
Cache* **unchecked**. From the CLI, in a clean checkout of `$RELEASE_SHA`, run
`vercel --prod --force`.

**Expected evidence** (`bash scripts/deploy/website-execution-check.sh`):
`TASK_COMPOSER_VISIBLE=VERIFIED`, `GATEWAY_EXECUTION_ENABLED=VERIFIED`,
**`GATEWAY_RUN_START_ENABLED=DISABLED`**, `GATEWAY_BACKEND_BINDING=VERIFIED`,
`FRONTEND_RELEASE=VERIFIED`, and `BACKEND_EXECUTION_ARMED=NO`. The last one is
expected, because run creation is still off. Nothing can start from the
website at this point: the gateway and the API both refuse. In every project,
Swarm V2 and Vehicle Catalog V1 alike, the composer says *"Starting runs is
turned off at the current activation stage"* and offers no task. The Mapping
Plan can be authored.

**Stop if** `GATEWAY_RUN_START_ENABLED` is anything but `DISABLED`.

### D.2 Author the plan in the website

Sign in as a member of the `swarm_v2` project → open (or create) a
conversation. The composer says *"Runs in this project … start from the
Mapping Plan"* or *"Starting runs is turned off"*: no ordinary task is offered.
Open **Mapping plan → Show** and type, for a deliberately small first plan:

> Map Toyota, starting with 2018+, up to 10 variants.

Set **Candidates per batch** to 10 and save. The panel must show:
*"Can be prepared from the Government register: Toyota."*

### D.3 Read the exact revision and digest

```bash
bash scripts/deploy/work-scope-readiness.sh --list
export WS_ID=<work_scope_id> WS_REV=<revision> WS_DIGEST=<digest>   # from that line
export WS_ARGS="--work-scope-id $WS_ID --work-scope-revision $WS_REV --work-scope-digest $WS_DIGEST"
```

### D.4 Scoped Government capture and preparation

```bash
bash scripts/deploy/production-activate.sh --prepare-work-scope $WS_ARGS \
  --enable-catalog-execution --enable-work-scope-preparation 2>&1 | tee "$HOME/stage-d.txt"
```

In order, this:

1. Checks readiness. If the revision is already prepared, it only verifies,
   because a revision is prepared exactly once. If the revision is stale or
   the schema is missing, it stops.
2. Runs `government-production-capture.sh --ensure-job`, which refuses unless
   `worker:$RELEASE_SHA` exists, then points the capture job at it.
3. Runs `--prepare` with a fresh attempt key, which yields one
   `operator_capture` run (not paid, and not a model run).
4. Runs `--prepare-work-scope --run-id …`. This is **one** execution with
   `MILO_ENABLE_WORK_SCOPE_PREPARATION=true`; the job definition keeps it
   false. For each verified manufacturer it performs a scoped capture of the
   pinned WLTP resource, then writes the queue and batches in one transaction.
5. Runs `production-verify.sh --gate prepared $WS_ARGS`.

**Expected evidence:**

```
WORK_SCOPE_PREPARATION_STATUS=succeeded
UNIT 1. toyota state=prepared queued=… snapshot=cs1.…
WORK_SCOPE_PLAN=VERIFIED      WORK_SCOPE_PREPARED=VERIFIED
EVIDENCE_READY=VERIFIED       BATCH_READY=VERIFIED
NEXT_BATCH_NUMBER=1  NEXT_BATCH_ID=…  NEXT_BATCH_SNAPSHOT_KEY=cs1.…
RESULT: OK — every fact gate prepared requires is VERIFIED.
```

These together link the plan revision, its source evidence, the scoped
snapshot and the exact batch.

**Stop conditions, and the decision path for Government egress:**

- `STOP: GOVERNMENT_SOURCE_UNREACHABLE (GOV_TRANSPORT_FAILED | GOV_HTTP_STATUS_UNEXPECTED …)`:
  the capture job could not read `data.gov.il` from Cloud Run. Record the
  execution name, reason code and time. **This repository has no alternate
  production import route.** `scripts/r5_capture_fixtures.py import-capture`
  writes *test fixtures* only and must never be used to create production
  evidence. Do not substitute cached, fixture or hand-built rows. Proceed only
  after the network path is fixed by a separately reviewed change (for
  example, an approved egress route for the capture job), then re-run D.4.
  Re-running is safe: an unchanged register reuses the same snapshot, and the
  new attempt gets a new key.
- `UNIT 1. toyota state=vocabulary_insufficient` with
  `EVIDENCE_READY=NO (no unit of this revision was prepared …)`: the reviewed
  vocabulary cannot read most of Toyota's register rows. **Stop.** The fix is
  a reviewed vocabulary change, never a looser normalization.
- `WORK_SCOPE_PLAN=NO` (stale revision, wrong digest, closed plan): re-read
  D.3. If the plan was revised, prepare the new head.
- The preparation was refused before its capture run was claimed (for
  example `WORK_SCOPE_PREPARATION_STALE`): the orchestrator prints the exact
  command to resume with **the same** prepared run (`--run-id`). Use it rather
  than re-running D.4, which would prepare another run. A queued
  `operator_capture` run otherwise shows up as `RUNS_QUIESCENT=NO` and can
  count against the operator's one-active-run cap.
- Any `UNVERIFIED`: the check could not see something, such as a missing
  `MILO_READONLY_DB_URL` or a role affected by RLS. Fix that access and re-run.
  It is never "good enough".

**Rollback.** Preparation writes only append-only rows and executes nothing.
To stop, do not continue to Stage E. A wrong plan is fixed by revising it in
the website (the new head is unprepared) and preparing that revision.

---

## Stage E — open the execution path, run starts last

### E.1 Backend (Cloud Run): the worker, then the API

```bash
bash scripts/deploy/website-execution-activate.sh --apply-backend $WS_ARGS
```

In this order, stopping at the first failure:

1. **Pre-check.** It runs `website-execution-check.sh` and requires
   `GATEWAY_RUN_START_ENABLED=DISABLED`, meaning the website provably refuses
   every run start. If run starts are open or cannot be proved closed, it
   refuses and changes nothing.
2. **Gate.** `production-verify.sh --gate prepared $WS_ARGS`. It changes
   nothing unless this passes.
3. **The worker.** It sets the worker flags and the reviewed RuntimePolicy,
   then `KIMI_API_KEY=<SECRET_PROVIDER_API_KEY>:latest` (on the worker only),
   then reads back every value and the secret binding. If this fails, the API
   is **not** touched. With the API's run creation still off, nothing can use
   the armed worker.
4. **The API.** It sets `JOB_LAUNCHER=cloud_run`, the worker identity, the
   concurrency caps and the API flags, then reads them back. Preparation,
   promotion and paid execution on the API stay pinned off.

After any of these steps, a click on *Start batch* or *Send task* is refused
at the gateway, because E.1 never touches `GATEWAY_ALLOW_RUN_START_ROUTES`. A
gcloud failure part way through leaves every later step undone. The website
still refuses every start, so re-run the command or use the rollback below.
Each stage and the partial-failure cases are covered by tests
(`tests/test_scoped_rollout_contract.py`, gateway tests in
`frontend/tests/gatewayPolicy.test.ts` and `gatewayRoute.test.ts`).

### E.2 The pre-open gate (read-only)

```bash
bash scripts/deploy/production-verify.sh --gate armed $WS_ARGS 2>&1 | tee "$HOME/stage-e2.txt"
```

**Expected evidence:** `RESULT: OK — every fact gate armed requires is
VERIFIED.` The verdict marks each required fact:

```
 * CODE_DEPLOYED          VERIFIED   [needs VERIFIED]
 * DATABASE_READY         VERIFIED   [needs VERIFIED]
 * EVIDENCE_READY         VERIFIED   [needs VERIFIED]
 * BATCH_READY            VERIFIED   [needs VERIFIED]
 * RUNS_QUIESCENT         VERIFIED   [needs VERIFIED]
 * GATEWAY_ENABLED        VERIFIED   [needs VERIFIED]
 * WEBSITE_ENABLED        VERIFIED   [needs VERIFIED]
 * PAID_EXECUTION_READY   VERIFIED   [needs VERIFIED]
 * RUN_START_PATH         DISABLED   [needs DISABLED]
```

`armed` is the gate **before** opening. It fails if run starts are already
open (`RUN_START_PATH=VERIFIED (needs DISABLED)`) or cannot be proved closed.
**Stop if** it does not pass.

### E.3 The last step: open run starts on the website (Vercel)

Only after E.2 passes, and not in Google Cloud Console:

```bash
vercel env add GATEWAY_ALLOW_RUN_START_ROUTES production   # true
```

This is a runtime value, so a new deployment picks it up without a rebuild.
Redeploy the release commit (dashboard → Deployments → the Production
deployment of `$RELEASE_SHA` → *Redeploy*). Then go straight to Stage F.

### Stage E rollback (emergency order, from [ROLLBACK.md](ROLLBACK.md))

Close the website first, then the backend:

```bash
# 1. Vercel: close run starts (remove GATEWAY_ALLOW_RUN_START_ROUTES, or set it
#    to false) and redeploy. From then on every start is refused at the gateway.
vercel env rm GATEWAY_ALLOW_RUN_START_ROUTES production --yes
# 2. Stop paid execution, then run creation and the launcher.
gcloud run jobs update milo-agent-worker --region us-central1 \
  --update-env-vars MILO_ENABLE_PAID_EXECUTION=false
gcloud run services update milo-agent-api --region us-central1 \
  --update-env-vars '^;^MILO_ENABLE_RUN_CREATION=false;MILO_ENABLE_WORK_SCOPE_BATCHES=false;JOB_LAUNCHER=disabled'
# 3. Remove the provider credential.
gcloud run jobs update milo-agent-worker --region us-central1 --remove-secrets KIMI_API_KEY
# 4. Optionally close plan writes too: set GATEWAY_ALLOW_EXECUTION_ROUTES=false in Vercel and redeploy.
```

A batch that is running keeps running until its run ends or is cancelled
("Cancel this batch" in the Mapping Plan uses the normal run cancellation).
Nothing is deleted.

To give Swarm V2 projects their ordinary composer back, turn the Government
read off on **both** the worker and the API
(`MILO_ENABLE_GOVERNMENT_CATALOG_READ=false`). With the read off, ordinary
Swarm V2 runs read no catalog.

A later **redeploy** (`cloud-run.sh apply`) returns both surfaces to Stage A.
Its preflight also refuses while the worker carries the provider key, so
redeploying at Stage 2 means going through A.6 and Stage E again, on purpose.

---

## Stage F — the post-open check (not paid, changes nothing)

```bash
bash scripts/deploy/production-verify.sh --gate active $WS_ARGS 2>&1 | tee "$HOME/stage-f.txt"
```

`active` is a check **after** E.3, not a gate before it. It requires run starts
to be open (`RUN_START_PATH=VERIFIED`). The pre-open gate is E.2's `armed`.

It makes no provider call and creates no run: its reads are describes, SQL
SELECTs and unauthenticated GETs. The website probe is a GET of an
execution-only gateway route with no credential, which the gateway answers
with 403 by policy when execution is closed and 401 at authentication when it
is open. It never sends a POST.

**Expected evidence:** every line of the verdict reads `VERIFIED`:

```
 * CODE_DEPLOYED          VERIFIED   code deployed
 * DATABASE_READY         VERIFIED   database ready (exact migrations, schema, grants)
 * EVIDENCE_READY         VERIFIED   evidence ready (scoped snapshots of the plan)
 * BATCH_READY            VERIFIED   batch ready (batch 1 linked, nothing live, not paused)
 * RUNS_QUIESCENT         VERIFIED
 * GATEWAY_ENABLED        VERIFIED   gateway enabled (behaviour + status agree; reaches the API)
 * WEBSITE_ENABLED        VERIFIED   website enabled (release build, execution UI on)
 * PAID_EXECUTION_READY   VERIFIED   paid-execution ready (flags, key on worker, quota, policy)
 * RUN_START_PATH         VERIFIED   the website now proxies run starts (opened in E.3)
RESULT: OK — every fact gate active requires is VERIFIED.
```

Also check in the browser, without clicking anything that starts a run: in
the plan's conversation, the composer offers **Open the Mapping Plan** and no
*Send task*, and the Mapping Plan's **Batches** section shows **Start batch 1**.

**Stop if** anything is `NO`, `DISABLED` or `UNVERIFIED`. Each line says what
is missing. If it fails, close run starts again (step 1 of the Stage E
rollback) before investigating. The first paid run is **not** a readiness
test.

---

## Stage G — the first paid run, from the website (operator action)

Do this only after Stage F is fully `VERIFIED`. It is a human decision, and
this change does not perform it.

1. Sign in to the production website as a member of the `swarm_v2` project
   and open the conversation holding the plan.
2. **Mapping plan → Show → Batches.** Check the details:
   *"Prepared from revision N: … candidates in … batches"*, with the next
   batch *"Batch 1 of … — Toyota, … candidates"*.
3. Click **Start batch 1**, read the confirmation (*"This starts ONE paid run
   for this batch only. Nothing else starts automatically."*), then click
   **Yes, start this batch**. Click it **once**. A second click, or a second
   tab, returns the same run and never creates another.
4. Watch the agents' state:
   - the **Swarm run** card (stage track and task list) and the **Live
     execution** panel, which shows the engine, phase, work and budget from
     durable events;
   - the inspector **Agents** tab, and **Costs** for usage against the reviewed
     caps (`$1.00` actual per run, `$3.00` estimated, `$4.00` daily);
   - the Mapping Plan's **Current** line (for example `running`). *"The worker
     for this batch has not been started"* or *"unresolved"* means an operator
     step is needed ([work-scope.md](../work-scope.md#continuation)); the
     website never relaunches it on its own.
5. When the run reaches a terminal state, read the **Final result** panel and
   the canonical verdict.
6. Click **Export → Export run (JSON)**, then **Download JSON**. Verify the
   file:

   ```bash
   python3 - "$HOME/Downloads/<exported file>.json" "$RELEASE_SHA" << 'PY'
   import json, sys
   doc = json.load(open(sys.argv[1]))
   assert doc["schema_version"] == "milo-run-export/1", doc["schema_version"]
   assert doc["engine"] == "swarm_v2" and doc["run_identity"]["workflow_key"] == "swarm_v2"
   assert doc["run_identity"]["release_sha"] == sys.argv[2], "not the release"
   assert doc["terminal_status"] in {"completed", "partial_success", "failed", "cancelled",
                                     "timed_out", "budget_exhausted"}
   print("run", doc["run_id"], doc["terminal_status"], doc["result_kind"],
         "government provenance:", bool(doc["government_provenance"]))
   PY
   ```

7. Confirm the Mapping Plan's progress. The batch is *Finished* (or *Stopped
   before finishing*, which makes it the next attempt). **Nothing else has
   started.** Later batches start only when a person clicks
   *Continue with batch N*.

Catalog promotion stays **off** in this rollout. A settled batch
(`completed` / `partial_success`) is never run again, and with promotion off
its candidates are counted as *unresolved*, not promoted. Turning promotion on
is a separate, separately authorized decision.

**Abort at any time:** use *Cancel this batch* in the Mapping Plan, then the
Stage E rollback.

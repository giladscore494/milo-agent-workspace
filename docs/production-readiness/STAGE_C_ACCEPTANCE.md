# Stage C acceptance report — one controlled paid smoke run

Status (2026-08-19): **STAGE C NOT PASSED — ATTEMPT 6 AUTHORIZATION
CONSUMED; ATTEMPT 7 PREPARED BUT NOT AUTHORIZED.** Attempt 6 reached real
paid Worker execution and **failed** (terminal `failed`,
`RETRY_LIMIT_REACHED` after repeated provider 429s during technical
enrichment). Production now truthfully holds **2 MILO runs** (Attempts 5
and 6). **2 Worker executions historically occurred** (Attempt 5
`milo-agent-worker-d2gfx`, Attempt 6 `milo-agent-worker-mcfrx`), but
**exactly 1 is currently visible in Cloud Run** — Attempt 5's execution
was explicitly deleted at `2026-08-18T02:00:48.774522Z` via
`google.cloud.run.v1.Executions.DeleteExecution` (proven by Cloud Audit
Logs). Attempt 6 consumed real
provider usage (25 model calls, 47,380 tokens, tracked cost $0.03976).
Earlier "zero runs / zero executions / zero provider calls /
authorization unconsumed" claims in this report are superseded and kept
below only as dated history.

Current facts:

- PR #50 is merged; the Attempt 7 **runtime release candidate** is its
  merge commit `88224bccc836f80f3dc1d173306a1aa63cddcc7a`, pinned as
  `STAGE_C_RELEASE_SHA` in `stage-c-env.sh`. (The release-preparation PR
  that updates the tooling deliberately does NOT re-pin the SHA to its
  own merge commit — the runtime pin must reference the reviewed runtime
  code, never the tooling PR itself.)
- The production Kimi organization is **operator-confirmed Tier 2**
  (concurrency 100 / RPM 500 / TPM 3,000,000 / TPD unlimited); the pinned
  worker-only operating envelope is deliberately below that ceiling (see
  the provider-envelope section).
- Production is **reported** fail-closed after the Attempt 6 kill-switch/
  cleanup cycle, but that posture MUST be re-verified live (read-only)
  before any deployment or enablement.
- **Attempt 7 is NOT authorized by this report or by merging the
  release-preparation PR.** It requires a fresh, separate, explicit
  operator authorization after: PR merge → CI green → image build →
  flags-off deployment → read-only posture verification. Exactly ONE new
  run / ONE new Worker execution over the pinned historical baseline is
  then allowed, under the fresh idempotency key
  `stage-c-smoke-attempt-7-20260819` (the consumed `stage-c-smoke-0001`
  key is never reused).
- Stage D remains unauthorized and blocked.

The complete operator toolkit is in
[`scripts/release/stage-c/`](../../scripts/release/stage-c/README.md).
(The original pre-toolkit status, kept for history: the first prepared
session was blocked on operator write access and executed nothing.)

Original report date: 2026-08-11/12. Production: GCP
`big-cabinet-457321-t7` (us-central1). Provider: Moonshot/Kimi
(`kimi-k2.6`). Release under test at that time:
`30b05bc45d6f9372261e4fac20cd983c69db971f` (merge of PR #42, Stage B
PASS); superseded for Attempt 7 by the pinned `88224bc…` candidate above.

## Why the run did not happen

The remote automation identity behind the production Google Cloud tooling
is **read-only for Cloud Run and Cloud Build**. Verified live (each denial
below produced no mutation):

| Attempted action (required by Stage C) | Result |
| --- | --- |
| `gcloud builds submit` (build release images) | `PERMISSION_DENIED: cloudbuild.builds.create` |
| `gcloud run jobs create` (disposable probe job) | `PERMISSION_DENIED: run.jobs.create` |
| `gcloud run jobs update` (label-only probe) | `PERMISSION_DENIED: run.jobs.update` |
| `gcloud run services update` (label-only probe) | `PERMISSION_DENIED: run.services.update` |

Every Stage C step is a production mutation (deploy release images, bind
the provider key to the worker, enable the two flags, create/launch the
run). With no write path, the correct behavior under the staged-activation
policy is to fail closed, prepare everything, and hand the exact procedure
to the human operator — the same division of labor Stage B used for its
IAM bindings ("IAM mutations are blocked for the automation identity").

All read paths worked (services/jobs/executions describe+list, secret IAM
policies, Cloud Build history), which is what made the full pre-flight
verification below possible.

## Pre-flight verification (read-only — all live, 2026-08-11)

| Check | Result | Evidence |
| --- | --- | --- |
| Stage B PASS merged to main | PASS | `origin/main` = `30b05bc…` (PR #42 merge) |
| Worker launcher IAM least-privilege | PASS | `gcloud run jobs get-iam-policy milo-agent-worker`: single binding `roles/run.jobsExecutorWithOverrides` → `milo-api-runtime@` only |
| Legacy `id-kimi-agent-runner@` launcher permission removed | PASS | absent from the job IAM policy |
| `KIMI_API_KEY` worker-only | PASS | `gcloud secrets get-iam-policy KIMI_API_KEY`: single `secretAccessor` binding → `milo-worker-runtime@` only |
| Legacy SA removed from production secrets | PASS | `SUPABASE_URL`/`SUPABASE_SECRET_KEY` accessors = `milo-api-runtime@` + `milo-worker-runtime@` only; `KIMI_API_KEY` = worker only; secret list = the 5 expected names |
| API service private, gateway-only | PASS | service IAM: `roles/run.invoker` → `milo-vercel-gateway@` only; no `allUsers` |
| Zero worker executions | PASS | `gcloud run jobs executions list --job=milo-agent-worker` → empty (before and after this session) |
| Execution fully disabled | PASS | API + worker env: all `MILO_ENABLE_*=false`, `JOB_LAUNCHER=disabled`, `GATEWAY_ALLOW_EXECUTION_ROUTES` not set on the backend |
| No provider key bound anywhere | PASS | neither the API service nor the worker job references `KIMI_API_KEY`/`MOONSHOT_API_KEY` |
| No test adapters in production | PASS | no `CLOUD_RUN_AUTH_MODE`/`MILO_E2E_INPROCESS_WORKER`/`MILO_WORKER_ENGINE` in either runtime |
| Project-level IAM enumeration | MANUAL | Cloud Resource Manager API is disabled in the project, so `projects get-iam-policy` is unavailable to this tooling; resource-level policies (job, service, all secrets) were fully enumerated instead and contain only the identities listed above |
| Production Supabase migration surface | PREPARED | the staging-scoped Supabase tooling cannot reach production by design; `stagec-db-probe --preflight` (toolkit step 5) verifies the `_v2`/guarded RPC surface and the attempt-aware reservation schema live before any run |

## Blocking deployment-consistency finding (must be fixed before the run)

*(Dated history, 2026-08-11/12 — resolved: later operator sessions built
and deployed the `30b05bc…` images via toolkit steps 1–2. For Attempt 7
the same rule applies to the new pinned candidate: steps 1–2 must build
and deploy `88224bc…` before anything is enabled.)*

Production still runs images `api:791f7af9…` / `worker:791f7af9…`
(built 2026-08-02), which **predate the Stage B corrective code** that
pairs with the production migrations `20260810000100`–`20260810000600`
already applied (SETOF/`_v2` RPC contract, DB-clock lease-guarded worker
writes, attempt-aware reservations, cross-run settle isolation). The
Stage B PASS evidence covers `30b05bc…`, not `791f7af9…`; executing the
paid run on the old images would test an unvalidated code/schema
combination and would likely fail mid-lifecycle. No image exists in the
production registry for `30b05bc…` (verified via
`gcloud artifacts docker tags list`). Toolkit steps 1–2 build and deploy
the correct release before anything is enabled.

## Stage C attempt 3 (2026-08-15) — STOPPED SAFELY before run creation

After PR #45 merged, the operator repeated the pre-paid validation path.
A live DB preflight passed: exact run counts were zero, the reservation/
lease/ledger schema was present, and all five required RPCs were exposed
with the expected signatures. The worker was then enabled with the exact
Stage C caps and worker-only `KIMI_API_KEY`; the API was enabled for run
creation/launcher, and `03b-verify-stage-c-posture.sh` passed.

**Before `05-execute-smoke.sh` was run**, review found that steps 5/6 read
Cloud Run logs with `value(textPayload)` only, while the Python probes
emit their structured records into `jsonPayload`. That could make a
successful probe invisible to the acceptance gates; the worker-log
secret scan had the same payload-type blind spot.

No setup/create step was executed, no MILO run existed, worker executions
remained zero, and no provider call/token/cost occurred. The operator
immediately applied the kill switch; after the initially interrupted API
update, read-only verification already showed fail-closed, and the full
step-7 cleanup then completed. API ended fail-closed
(`MILO_ENABLE_RUN_CREATION=false`, `JOB_LAUNCHER=disabled`,
`MILO_ENABLE_PAID_EXECUTION=false`); worker paid execution was false and
`KIMI_API_KEY` was unbound. Both disposable probes were deleted.

PR #46 corrected log collection to read both `jsonPayload` and
`textPayload`, emit only actual `stage_c_probe` records to stdout, and
scan both payload types for secret markers. CI, Repo Scan and Vercel
passed and PR #46 was merged as
`7127516a3f0487016d27e01063df4feaff08ef8e`.

## Post-PR #46 live validation (2026-08-15) — null log record found safely

With API/worker still fail-closed and worker executions still exactly
zero, the disposable probes were recreated. A standalone DB preflight
execution (`stagec-db-probe-24wvk`) completed successfully and its real
Cloud Logging payload reported `ok=true`, exact zero run counts, all five
RPCs present, and all required schema/ledger checks present.

The same live log response also exposed a second transport edge case:
`gcloud logging read --format=json(textPayload,jsonPayload)` returned an
array containing the valid `jsonPayload` probe record, the Cloud Run
system `textPayload` (`Container called exit(0).`), **and a trailing
JSON `null` record**. The merged renderer called `record.get(...)`
unconditionally, so a `null` element would raise `AttributeError`; its
then-existing `|| true` could additionally mask that renderer failure.

The current corrective therefore skips non-dictionary log records and
removes the renderer's `|| true`, so parsing/log-transport failures fail
closed rather than being swallowed. The real captured Cloud Logging
fixture was replayed locally against the corrected logic: exactly one
preflight probe record was emitted, `ok=true`, while the system line and
`null` were safely ignored.

No paid surface was enabled during this validation. No setup/create step
ran, no MILO run was created, worker executions remain zero, and there
were zero provider calls/tokens/cost. The two disposable probe jobs
currently remain present solely for the next preflight validation.

## Stage C attempt 2 (2026-08-13/14) — FAILED SAFELY at DB preflight

The operator re-ran the toolkit after the attempt-1 corrective (PR #44).
Launch invariants passed (release images, exact caps on both surfaces,
IAM, zero executions). The DB preflight then ran and **failed closed
before setup and before run creation** — correctly on the safety side,
but for a wrong reason:

| Preflight check | Result |
| --- | --- |
| `existing_runs` (exact count) | **0** |
| `existing_stage_c_runs` (exact count) | **0** |
| `reservations_attempt_column` / `runs_lease_columns` / `run_usage_ledger` | present |
| all five Stage C RPCs | **falsely reported MISSING** |

Root cause: the preflight tested RPC existence by POSTing an empty JSON
body and treating HTTP 404 as "function absent". All five Stage C RPCs
(`create_message_and_run_v2`, `transition_run_worker_guarded`,
`heartbeat_run_guarded`, `update_run_usage_guarded`,
`settle_model_call_budget_guarded`) have **required parameters** in the
release migrations (000400/000600), and PostgREST resolves a function by
name AND supplied argument keys — so an existing parameterized function
answers a bodyless probe with a function-resolution 404 (PGRST202). The
check produced a false negative for every one of them; a mismatched-
invocation 404 is not proof of absence. It was also unsafe by
construction: it invoked mutating RPCs merely to test existence.

Outcome: **no setup rows created, no MILO run created, zero worker
executions, zero provider calls, zero tokens, $0 provider cost.** The
operator applied the kill switch and completed the full step-7 cleanup:
API and worker restored fail-closed (`MILO_ENABLE_PAID_EXECUTION=false`,
`KIMI_API_KEY` unbound, `MILO_ENABLE_RUN_CREATION=false`,
`JOB_LAUNCHER=disabled`) and both disposable probe jobs deleted.

Corrective fix (this commit, no production mutation): the preflight RPC
check is now **non-mutating and signature-aware** — it GETs the PostgREST
OpenAPI document with the existing service-role credentials and requires
every Stage C RPC to be exposed as `/rpc/<name>` advertising every
required argument name from the release migrations; it never POSTs to an
RPC, and unavailable/malformed/ambiguous metadata fails closed
(`UNVERIFIED`), never assuming presence. All other preflight gates
(exact zero-run counts, idempotency-key count, column/ledger checks) are
unchanged. Stage C was NOT passed and must not be re-attempted until this
corrective is reviewed and merged.

## Stage C attempt 1 (2026-08-13) — FAILED SAFELY at DB preflight

The operator executed the toolkit through step 5. The **preflight DB probe
crashed before any MILO run existed**: `04-create-probes.sh` transported
the raw `probe_db.py` source inside `--set-env-vars` using `^@^` as the
gcloud dict delimiter, and the source contains the literal
`stage-c-smoke@invalid.milo`, so gcloud split `PROBE_SOURCE` at the `@`.
The deployed probe failed with
`SyntaxError: unterminated string literal` at `TEST_EMAIL`.
The fail-closed design held:

| Outcome | Value |
| --- | --- |
| MILO runs created | **0** |
| Worker executions | **0** |
| Provider calls / tokens / paid cost | **0 / 0 / $0** |
| Kill switch + cleanup | completed by the operator |

Restored posture (operator-confirmed): `MILO_ENABLE_PAID_EXECUTION=false`,
`KIMI_API_KEY` unbound from the worker, `MILO_ENABLE_RUN_CREATION=false`,
`JOB_LAUNCHER=disabled`, both disposable probe jobs deleted. Release
images remain deployed (signed-off code, inert while flags are off).

Corrective fix (this commit, no production mutation): probe-source
transport now uses a multi-character delimiter (`:::`) that is asserted
absent from BOTH probe sources before any `gcloud` call — a collision
fails closed; the sources still ship byte-for-byte, unchanged. The
`run_probe` helpers in steps 5/6 (same transport pattern, no live
collision in their current values) were hardened to the same checked
delimiter. Stage C must NOT be re-attempted (no rebuild, deploy,
re-enable, probe creation or run) until this corrective PR is reviewed
and merged.

## Corrective safety pass (2026-08-13, no production mutation)

A corrective review hardened the operator toolkit before any execution.
Nothing was deployed, enabled, bound, or executed in production. Fixes:

1. **Poll verdicts** — `probe_gateway.py` poll now distinguishes PASS from
   FAIL terminal states: only states in the acceptance policy (default:
   `completed` only) exit 0; every other terminal state exits non-zero and
   instructs the operator to run the kill switch.
2. **Preflight exact count** — `probe_db.py` preflight now uses a real
   server-side exact count (`Prefer: count=exact` / `Content-Range`), not
   the length of a `Range: 0-0` page, and fails closed unless the runs
   table holds exactly `STAGE_C_EXPECTED_PRIOR_RUNS` (default 0) rows and
   zero rows for the Stage C idempotency key.
3. **Evidence is now an executable acceptance gate** — `probe_db.py`
   evidence mode and `06-collect-evidence.sh` exit non-zero unless ALL
   criteria hold (one run, one execution, expected terminal state,
   attempt/claim/heartbeat invariants, zero dangling reservations,
   reservation/ledger/usage accounting consistency, cost/token/call caps,
   idempotent replay, zero secret markers). No warning-only checklists.
4. **Exact cap verification** — new `verify_caps.py` compares every cap in
   `STAGE_C_CAPS` for exact equality on BOTH worker and API immediately
   before run creation (and in `03b`), fails on missing, changed, or
   unexpected budget variables, and enforces the signed-off release images
   (the production-image blocker above) as an executable pre-run check.
5. **Cost ceiling corrected** — see "Cost ceiling" above: $3.00 bounds
   tracked token-derived cost only; provider-side web-search tool charges
   are untracked and bounded separately (conservative total ≤ $9.00).
6. **Authorized constants pinned** — `stage-c-env.sh` no longer accepts
   environment overrides: project (`big-cabinet-457321-t7`), region
   (`us-central1`), release SHA (`30b05bc…`), expected prior runs (0),
   acceptable terminal states (`completed`), plus the registry, repo URL,
   service/job/SA names, API URL, idempotency key and `STAGE_C_CAPS` are
   all pinned. An inherited shell value that conflicts with any pinned
   constant makes every step script refuse at source time (fail closed) —
   the authorization cannot be widened or redirected from the operator's
   shell; changing a constant requires a reviewed commit.

## Prepared procedure (operator-executable, in order)

`scripts/release/stage-c/` — see its README for the step table. Summary:

1. `01-build-images.sh` — Cloud Build clones the **public** repository at
   the pinned runtime `STAGE_C_RELEASE_SHA` (`88224bc…`, SHA verified
   in-build) and pushes full-SHA-tagged images.
2. `02-deploy-images.sh` — worker job then API service image update,
   flags untouched; verifies Ready + still fully disabled + the exact
   visible execution baseline (1 terminal execution, 0 active).
3. `03-enable-stage-c.md` (manual commands — by repository policy no
   committed script may enable an execution flag; verified afterwards by
   the read-only `03b-verify-stage-c-posture.sh`) — after re-verifying
   worker-only key IAM:
   worker = `MILO_ENABLE_PAID_EXECUTION=true` + caps + the pinned
   worker-only provider envelope (`STAGE_C_WORKER_PROVIDER_LIMITS`) +
   `KIMI_API_KEY=KIMI_API_KEY:latest` (worker runtime only, per
   authorization); API = `JOB_LAUNCHER=cloud_run` +
   `MILO_ENABLE_RUN_CREATION=true` + caps, and NO `MILO_PROVIDER_*`
   variable. The API **keeps** `MILO_ENABLE_PAID_EXECUTION=false` (it
   never holds the provider key; `production_config.py` forbids the paid
   flag without it; the worker is the paid-execution enforcement point).
   All other flags stay false. The Vercel gateway is untouched
   (`GATEWAY_ALLOW_EXECUTION_ROUTES` off), so no browser can reach run
   creation — public/general execution stays disabled throughout.
4. `04-create-probes.sh` — two disposable Cloud Run jobs:
   `stagec-db-probe` (runs as `milo-api-runtime@`; binds only the two
   Supabase secrets that identity already reads — no new IAM grants;
   migration preflight, test-data setup, post-run evidence) and
   `stagec-gw-probe` (runs as `milo-vercel-gateway@`, no secrets; mints
   its identity token from the metadata server like the real gateway).
5. `05-execute-smoke.sh` — refuses unless the visible Worker execution
   listing matches the pinned live baseline exactly (1 visible historical
   execution, terminal, zero active — `verify_executions.py`); re-verifies
   launch invariants (worker-only key IAM, launcher IAM, exact caps on
   both surfaces + exact provider limits on the worker only); DB
   preflight (exactly 2 prior runs, 0 rows under the Attempt 7 key);
   creates the operator test user (`stage-c-smoke@invalid.milo`),
   dedicated `stage-c-smoke` project (test user is the **only** member),
   and conversation; then executes exactly **one** new run through the
   canonical path
   API → launcher → Cloud Run worker → real provider → Supabase, with an
   immediate idempotent-replay check, and monitors to a terminal state.
6. `06-collect-evidence.sh <RUN_ID>` — full validation: claim/lease/
   heartbeat, event/checkpoint sequence, reservation settlement with zero
   dangling rows, ledger token/cost sums vs the run usage snapshot,
   exactly one new authorized run/execution over the pinned prior
   baseline — `total_runs=3` / visible `executions=2` (all terminal; no duplicate
   execution, no stale-worker writes) with exactly one run under the
   Attempt 7 key, verified by the requested Run ID — post-completion
   idempotent replay returns the same run, secret-marker scans over DB
   events and worker logs. Actual provider cost is additionally verified
   in the Moonshot console (external).
7. `07-post-smoke-posture.sh` — safest documented posture (ROLLBACK.md
   order): paid off + key binding removed, run creation off, launcher
   disabled, probe jobs deleted; release images remain (signed-off code);
   cap variables remain (inert while flags are off).
- `kill-switch.sh` — immediate fail-closed at any sign of trouble,
  including cancelling a running execution (authorization item 15).

## Smallest-safe caps for the single run

The preserved MILO pipeline (immutable behavior) is a full
Hyundai/Israel/2010–June-2026 swarm: 3 web discovery agents, a normalizer,
4 technical web agents over model chunks of 4, verifier chunks of 6, a
Python final builder and one Hebrew-summary call; every web-search tool
round is a separate guarded provider call, and run input cannot narrow the
scope (the run `input` carries only `content`/`metadata`; the engine uses
its preserved defaults). The caps below are the smallest values that
plausibly let that canonical pipeline reach a terminal result while
hard-bounding exposure; if any cap trips, the tracker fail-closes at a
controlled `budget_exhausted`/`timed_out` terminal.

| Variable | Value | Bound |
| --- | --- | --- |
| `MILO_MAX_MODEL_CALLS_PER_RUN` | 200 | hard call cap |
| `MILO_MAX_INPUT_TOKENS_PER_RUN` | 700000 | ≈ $0.42 input worst case |
| `MILO_MAX_OUTPUT_TOKENS_PER_RUN` | 250000 | ≈ $0.63 output worst case |
| `MILO_MAX_TOTAL_TOKENS_PER_RUN` | 900000 | joint token ceiling |
| `MILO_ESTIMATED_COST_PER_CALL` | 0.02 | reservation size |
| `MILO_MAX_ESTIMATED_COST_PER_RUN` | 4.00 | stops at 200 reservations |
| `MILO_MAX_COST_PER_RUN` | 3.00 | hard actual-cost cap |
| `MILO_MAX_RUN_DURATION_SECONDS` | 3300 | under the 3600s job timeout |
| `MILO_MAX_RETRIES` | 15 | fallback/retry allowance |
| `MILO_MAX_AGENT_STEPS` | 60 | agent-task cap |
| `MILO_MAX_CONCURRENT_RUNS_PER_USER` | 1 | single-run guarantee |
| `MILO_MAX_CONCURRENT_RUNS_PER_PROJECT` | 1 | single-run guarantee |
| `MILO_DAILY_USER_BUDGET` | 5.00 | activates the reservation ledger |
| `MILO_DAILY_PROJECT_BUDGET` | 5.00 | activates the reservation ledger |

### Acceptance policy for the terminal state

The ONLY terminal state that makes the smoke run a PASS is `completed`
(`STAGE_C_ACCEPTABLE_TERMINAL_STATES` in `stage-c-env.sh`). `failed`,
`cancelled`, `timed_out`, `budget_exhausted` and `partial_success` are
controlled fail-closed terminals: they prove the safety rails held, but
they FAIL the smoke test — the poll probe exits non-zero on them and
instructs the operator to run `kill-switch.sh` and investigate before any
further Stage C action.

### Cost ceiling — what $3.00 does and does NOT bound

**MILO `actual_cost` does not include provider-side web-search tool
charges.** The guarded client (`backend/budget.py`) takes a provider-sent
`usage.cost` field when present; Moonshot chat completions do not send
one, so `actual_cost` falls back to token-only pricing
(`backend/model_pricing.py`: kimi-k2.6 at $0.60/M input, $2.50/M output).
The pipeline's `$web_search` builtin tool is billed by Moonshot **per
search invocation, separately from tokens**, and those charges never enter
`actual_cost`, the reservation ledger, or the daily budgets. Therefore
`MILO_MAX_COST_PER_RUN=3.00` is a hard ceiling on **tracked token-derived
cost only — it is NOT a hard provider-billing ceiling.**

Conservative maximum total monetary exposure for this ONE run:

| Component | Bound | Basis |
| --- | --- | --- |
| Token-billed (tracked) | ≤ $3.00 hard; ≈ $1.05 by token caps | 700k input ($0.42) + 250k output ($0.63); tracker hard-stops at $3.00 |
| Web-search tool fees (UNTRACKED) | ≤ $6.00 | ≤ 200 guarded call rounds × ≤ 3 search invocations/round × $0.01/invocation (2× safety factor over the documented ≈ $0.005 fee) |
| **Conservative maximum total** | **≤ $9.00** | expected actual total well under $1 |

Operator obligations that no MILO cap can replace: verify the current
per-invocation web-search fee in the Moonshot console **before** the run
(recompute the table if it changed), and verify the **actual billed
total** (tokens AND tool fees) in the Moonshot console after the run —
step 6 reminds you. The daily budgets ($5.00) also see only tracked cost
and do not bound search fees.

Cancellation-path verification (runbook Stage C action 10) is satisfied by
the Stage B live evidence (pre-start and mid-execution cancellation on the
identical code path in staging); mock adapters are hard-forbidden in
production, so no production cancellation rehearsal is performed.

## Stage C attempt 4 (2026-08-17) — BLOCKED SAFELY at run creation

After PR #47 merged and the corrected Cloud Logging renderer passed live
DB preflight validation, the operator enabled the exact Stage C posture
and ran `05-execute-smoke.sh`.

Launch invariants passed: all 14 caps matched exactly, the API and worker
were still on the signed-off `30b05bc45d6f9372261e4fac20cd983c69db971f`
release, provider-secret access remained worker-only, and launcher IAM
remained restricted to the API runtime identity. The live DB preflight
also passed with `existing_runs=0` and `existing_stage_c_runs=0`, and the
disposable test user/project/conversation setup completed successfully.

The gateway create probe then failed with HTTP 502. The underlying
Postgres error was `42501: permission denied for function
create_message_and_run`. The backend calls the service-role-only
`create_message_and_run_v2` SETOF wrapper, but that wrapper is SECURITY
INVOKER and delegates to the base `create_message_and_run` function.
`service_role` had EXECUTE on the wrapper but not on that base dependency.

The operator immediately ran `kill-switch.sh` and then
`07-post-smoke-posture.sh`. Production returned fail-closed: API run
creation false, launcher disabled, API/worker paid execution false, and
the worker `KIMI_API_KEY` binding removed. Disposable probes were deleted.

No worker execution was created, Kimi was never invoked, no provider call
occurred, no tokens were consumed, and provider cost remained $0. There
is no evidence that a MILO run was created because the database rejected
the function invocation before execution. Therefore the single authorized
paid Stage C smoke remains unconsumed.

Root cause audit found two SECURITY INVOKER wrapper dependencies missing
`service_role` EXECUTE: `create_message_and_run(...)` and
`create_project_from_proposal_with_owner(...)`. The corrective is an
additive migration that keeps PUBLIC/anon/authenticated revoked while
granting EXECUTE only to `service_role`, plus regression coverage for the
wrapper dependency ACL contract.

## Stage C attempt 5 — FAILED SAFELY before any provider call

Run `58daa7de-76f5-4359-93e1-3a767c912c20` was created and a Worker
execution (`milo-agent-worker-d2gfx`) was launched. The Worker failed at
the `claim_run_lease` ACL before any Kimi request: **zero provider
calls, zero tokens, $0 provider cost for this attempt**. The Worker
execution reached a terminal state. It was subsequently **explicitly
deleted** at `2026-08-18T02:00:48.774522Z` via
`google.cloud.run.v1.Executions.DeleteExecution` (the successful
deletion is proven by Cloud Audit Logs), so it no longer appears in the
Cloud Run execution listing and is NOT counted in the pinned visible
execution baseline. The Attempt 5 database run row remains preserved and
is counted in the pinned database baseline.

## Stage C attempt 6 — REAL PAID EXECUTION, FAILED

Run `37912575-f9ce-4437-893d-7dfa45c53aa9` (idempotency key
`stage-c-smoke-0001`) was created and its Worker execution reached real
Kimi execution:

| Quantity | Value |
| --- | --- |
| Model calls | 25 |
| Total tokens | 47,380 |
| Actual tracked cost | $0.03976 |
| Checkpoints | through the normalizer |
| Failure point | technical enrichment, after repeated provider 429s |
| Terminal status | `failed` |
| Error | `RETRY_LIMIT_REACHED` |

The Attempt 6 Worker execution (`milo-agent-worker-mcfrx`) was terminal.
**The Attempt 6 authorization is consumed**: under the acceptance policy
only `completed` is a PASS, so Attempt 6 FAILED the smoke test, and a
further attempt requires fresh explicit operator authorization. The run
row, its execution and its usage accounting are preserved as historical
evidence; `milo-agent-worker-mcfrx` is the only Worker execution still
visible in Cloud Run (Attempt 5's was deleted on 2026-08-18 — see
above), and it is the pinned visible execution baseline.

Consequence for Attempt 7 preparation: the toolkit's former
empty-system assumptions (zero prior runs, zero prior Worker executions,
"refuse if any execution exists", "exactly one total run/execution") are
no longer valid. All preflight and evidence gates now verify a
one-run/one-execution increment over the exact pinned live baseline:
exactly 2 prior database runs and exactly 1 VISIBLE terminal prior
execution before run creation (zero active) — 2 executions historically
occurred, but Attempt 5's was deleted on 2026-08-18, so Cloud Run
exposes only Attempt 6's — exactly 3 database runs and 2 visible
executions after the one authorized launch, and zero-then-one rows under
the fresh Attempt 7 idempotency key.

## Production mutations / current posture

The original automation-identity session performed no writes, but the
subsequent authenticated operator sessions did perform the controlled
Stage C mutations documented above: release images were built/deployed,
disposable probes were created/deleted/recreated, tightly controlled
enable/kill/cleanup cycles were used, and — in Attempts 5 and 6 — one
MILO run and one Worker execution were created per attempt.

**Attempts 5 and 6 each created a MILO run and a Worker execution;
Attempt 6 reached real paid provider execution and failed.**

Reported execution posture after the Attempt 6 kill-switch/cleanup cycle
(MUST be re-verified live, read-only, before any Attempt 7 deployment or
enablement):

- API: `MILO_ENABLE_RUN_CREATION=false`,
  `JOB_LAUNCHER=disabled`, `MILO_ENABLE_PAID_EXECUTION=false`.
- Worker: `MILO_ENABLE_PAID_EXECUTION=false`.
- Worker `KIMI_API_KEY`: unbound.
- MILO runs: **2** (Attempts 5 and 6 — historical, preserved).
- Worker executions visible in Cloud Run: **1**, terminal (Attempt 6
  `milo-agent-worker-mcfrx`). 2 executions historically occurred;
  Attempt 5's `milo-agent-worker-d2gfx` was explicitly and successfully
  deleted at `2026-08-18T02:00:48.774522Z`
  (`google.cloud.run.v1.Executions.DeleteExecution`, Cloud Audit Logs)
  and is no longer visible.
- Runs under the Attempt 7 key `stage-c-smoke-attempt-7-20260819`: **0**.
- Runtime release candidate for Attempt 7 (pinned, to be built/deployed
  by steps 1–2 after authorization):
  `88224bccc836f80f3dc1d173306a1aa63cddcc7a` (merge of PR #50).
- Disposable `stagec-db-probe` and `stagec-gw-probe` jobs must be
  recreated before the next Stage C preflight.

## Provider call / token / cost accounting

Attempts 1–5 made **zero provider calls** (Attempt 5 failed at the
`claim_run_lease` ACL before any Kimi request). Attempt 6 made **25 real
model calls, 47,380 total tokens, actual tracked cost $0.03976**. The
Attempt 6 authorization is consumed; the one-run allowance of any future
Attempt 7 requires fresh explicit operator authorization.

## Provider operating envelope (Attempt 7, worker-only)

The production Kimi organization is operator-confirmed **Tier 2**:
concurrency 100, RPM 500, TPM 3,000,000, TPD unlimited. That
confirmation authorizes NO provider call and NO paid smoke. The pinned
V1 envelope (`STAGE_C_WORKER_PROVIDER_LIMITS` in `stage-c-env.sh`) is
deliberately below the Tier 2 ceiling:

| Variable | Pinned value | Tier 2 ceiling |
| --- | --- | --- |
| `MILO_PROVIDER_MAX_CONCURRENCY` | 2 (intentionally — preserved V1 engine parallelism; no V2 concurrency) | 100 |
| `MILO_PROVIDER_RPM_LIMIT` | 350 | 500 |
| `MILO_PROVIDER_TPM_LIMIT` | 2400000 | 3,000,000 |
| `MILO_PROVIDER_MAX_RATE_LIMIT_RETRIES` | 5 | — |
| `MILO_PROVIDER_MAX_BACKPRESSURE_WAIT_SECONDS` | 240 | — |
| `MILO_PROVIDER_BACKOFF_BASE_SECONDS` | 2 | — |
| `MILO_PROVIDER_BACKOFF_MAX_SECONDS` | 30 | — |

These settings are applied to the **Worker only** (the API receives no
`MILO_PROVIDER_*` variable; `verify_caps.py` enforces both directions).

## Verdict

- Stage C: **NOT PASSED.** Attempt 6 consumed its authorization on a real
  paid Worker execution that terminated `failed`
  (`RETRY_LIMIT_REACHED`); only `completed` counts as Stage C acceptance.
- This release-preparation PR only pins Attempt 7 tooling: runtime SHA
  `88224bc…`, fresh idempotency key, exact live baselines (2 database
  runs / 1 visible terminal execution) and the worker-only provider
  envelope. **Merging
  it authorizes nothing** — no build, deployment, secret binding, flag
  enablement, probe creation, run creation, provider call, Attempt 7 or
  Stage D.
- Before any Attempt 7 (after fresh explicit operator authorization):
  merge + green CI, build the pinned `88224bc…` images, deploy flags-off,
  verify the read-only posture AND the exact historical baselines live
  (`03b-verify-stage-c-posture.sh`, DB preflight), enable the exact
  Stage C worker/API posture, and only then run `05-execute-smoke.sh`
  **once**.
- Any failure after run creation consumes the one-run attempt: invoke the
  kill switch and do not create another paid run without fresh explicit
  authorization.
- Stage D remains blocked until a paid run reaches `completed`,
  `06-collect-evidence.sh` passes all acceptance invariants (including
  the exact post-run totals of 3 database runs and 2 visible
  executions), actual provider billing
  is checked, and step 7 restores the post-smoke fail-closed posture.

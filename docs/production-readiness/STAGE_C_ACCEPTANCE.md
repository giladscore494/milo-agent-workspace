# Stage C acceptance report — one controlled paid smoke run

Status: **BLOCKED_PENDING_OPERATOR_WRITE_ACCESS** — the paid smoke run was
**not executed**. Zero production mutations occurred, zero provider calls
were made, zero cost was incurred, and production remains in its exact
pre-Stage-C posture (re-verified read-only at the end of the session, see
"Production unchanged" below). Stage C is fully prepared: the complete
operator toolkit is in [`scripts/release/stage-c/`](../../scripts/release/stage-c/README.md).

Date: 2026-08-11/12. Production: GCP `big-cabinet-457321-t7`
(us-central1). Provider: Moonshot/Kimi (`kimi-k2.6`). Release under test:
`30b05bc45d6f9372261e4fac20cd983c69db971f` (merge of PR #42, Stage B PASS).

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

## Prepared procedure (operator-executable, in order)

`scripts/release/stage-c/` — see its README for the step table. Summary:

1. `01-build-images.sh` — Cloud Build clones the **public** repository at
   `30b05bc…` (SHA verified in-build) and pushes full-SHA-tagged images.
2. `02-deploy-images.sh` — worker job then API service image update,
   flags untouched; verifies Ready + still fully disabled + zero
   executions.
3. `03-enable-stage-c.md` (manual commands — by repository policy no
   committed script may enable an execution flag; verified afterwards by
   the read-only `03b-verify-stage-c-posture.sh`) — after re-verifying
   worker-only key IAM:
   worker = `MILO_ENABLE_PAID_EXECUTION=true` + caps +
   `KIMI_API_KEY=KIMI_API_KEY:latest` (worker runtime only, per
   authorization); API = `JOB_LAUNCHER=cloud_run` +
   `MILO_ENABLE_RUN_CREATION=true` + caps. The API **keeps**
   `MILO_ENABLE_PAID_EXECUTION=false` (it never holds the provider key;
   `production_config.py` forbids the paid flag without it; the worker is
   the paid-execution enforcement point). All other flags stay false. The
   Vercel gateway is untouched (`GATEWAY_ALLOW_EXECUTION_ROUTES` off), so
   no browser can reach run creation — public/general execution stays
   disabled throughout.
4. `04-create-probes.sh` — two disposable Cloud Run jobs:
   `stagec-db-probe` (runs as `milo-api-runtime@`; binds only the two
   Supabase secrets that identity already reads — no new IAM grants;
   migration preflight, test-data setup, post-run evidence) and
   `stagec-gw-probe` (runs as `milo-vercel-gateway@`, no secrets; mints
   its identity token from the metadata server like the real gateway).
5. `05-execute-smoke.sh` — refuses if any execution exists; re-verifies
   launch invariants (worker-only key IAM, launcher IAM, caps present);
   DB preflight; creates the operator test user
   (`stage-c-smoke@invalid.milo`), dedicated `stage-c-smoke` project
   (test user is the **only** member), and conversation; then executes
   exactly **one** run through the canonical path
   API → launcher → Cloud Run worker → real provider → Supabase, with an
   immediate idempotent-replay check, and monitors to a terminal state.
6. `06-collect-evidence.sh <RUN_ID>` — full validation: claim/lease/
   heartbeat, event/checkpoint sequence, reservation settlement with zero
   dangling rows, ledger token/cost sums vs the run usage snapshot,
   `total_runs=1` / `executions=1` (no duplicate execution, no
   stale-worker writes), post-completion idempotent replay returns the
   same run, secret-marker scans over DB events and worker logs.
   Actual provider cost is additionally verified in the Moonshot console
   (external).
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

## Production mutations performed by this session

**None.** Four write attempts were made and all were denied before any
change (table above). Re-verified after the session: worker job
generation still `7`, API service generation still `8` (revision
`milo-agent-api-00008-lx5`), worker executions still zero, no secret
bindings changed.

## Provider call / token / cost accounting

Zero provider calls, zero tokens, zero cost — no run was executed and no
provider credential was ever read or bound by this session.

## Verdict

- Stage C: **NOT EXECUTED — BLOCKED** on production write access for the
  automation identity. No invariant failed; production is fail-closed and
  unchanged.
- Remaining before the run can happen, in order: either (a) the operator
  runs `scripts/release/stage-c/01…07` from an authenticated shell
  (recommended; the scripts encode the full verification), or (b) the
  operator grants the automation identity the minimal write roles
  (`roles/cloudbuild.builds.editor`, `roles/run.developer` on the two
  Cloud Run resources + `roles/run.jobsExecutor` for probe execution,
  `iam.serviceAccountUser` on the three runtime SAs) and re-authorizes
  this session to continue Stage C end to end.
- Remaining before Stage D (after a PASS run): unchanged from the
  runbook — Stage C acceptance record completed with real run evidence,
  post-smoke posture restored, then the Stage D expansion gates
  (allowlist, budgets, monitoring, rollback rehearsal) each under new
  explicit approval.

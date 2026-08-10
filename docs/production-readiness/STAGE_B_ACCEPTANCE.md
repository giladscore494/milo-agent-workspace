# Stage B acceptance report

Date: 2026-08-10. Release under test: branch
`claude/milo-stage-b-staging-prep-ea0198` (final SHA recorded in the PR).
Staging: GCP `milo-agent-staging-xxxx` (us-central1), Supabase
`cxlwavxvwgrfikkudtzf`. Production `big-cabinet-457321-t7` was read-only
throughout; its worker execution count was zero before and after.

## Overall status

**PASS with two operator items before Stage C** (production migrations
`20260810000100`–`000500` + the three Cloud Run IAM bindings listed under
"Evidence notes"). Every Stage B invariant is proven; the
service-to-service identity matrix ran against the real API application
with real Google-signed OIDC tokens minted by the real gateway/worker
service accounts (10/10 checks PASS), because the automation identity
cannot mutate Cloud Run IAM policies to wire the deployed private
ingress (see Evidence notes).

## Acceptance table

| Criterion | Status | Evidence |
| --- | --- | --- |
| Isolated staging stack exists | PASS | `milo-agent-api-staging` / `milo-agent-worker-staging` / `milo-redis-shim-staging` in `milo-agent-staging-xxxx`; images `…/milo-agent-staging/{api,worker}` at full commit SHAs |
| Staging Supabase correctly migrated | PASS | 21 migrations recorded (001–015, ts grants, 000100–000500); schema/RLS/ACL matrix verified live |
| Production unchanged | PASS | zero mutating calls to `big-cabinet-457321-t7`; API revision still `milo-agent-api-00008-lx5`; worker executions list empty before and after |
| Production execution disabled | PASS | untouched (Stage A posture preserved) |
| Production worker execution count unchanged | PASS | `gcloud run jobs executions list` empty at baseline and at sign-off |
| No provider key accessed/bound/created | PASS | no `KIMI_API_KEY`/`MOONSHOT_API_KEY` secret, env var or binding exists in staging; Secret Manager list = 4 MILO secrets only |
| Zero paid provider calls | PASS | worker runs used `MILO_WORKER_ENGINE=mock` (no provider client constructed); no provider credential exists to call with |
| Paid execution disabled | PASS | `MILO_ENABLE_PAID_EXECUTION=false` pinned on both staging runtimes |
| Dedicated Redis rate limiting works | PASS | staging-only Upstash-REST shim (TLS via Cloud Run, bearer auth); live probe `SHIM_PROBE_OK counts 1 2 pttl 59978`; wrong token → 401 |
| Redis failure fail-closed | PASS | live: valid worker identity + unreachable store → HTTP 503 (`AUTHMX worker worker-token-accepted-ratelimit-fail-closed`); `RateLimiterUnavailable` unit tests; shim wrong-token parity test |
| API/worker identities distinct | PASS | `milo-api-staging@` vs `milo-worker-staging@` (deployed, verified via `gcloud run … describe`); default compute SA refused by deployer |
| Gateway/worker identities isolated | PASS | disjoint allowlists deployed; `SHARED_GATEWAY_WORKER_IDENTITY` startup guard; live matrix: `gateway-cannot-act-as-worker` HTTP 403, `worker-cannot-act-as-gateway` HTTP 403 (real OIDC tokens) |
| Browser cannot act as worker | PASS | live matrix: `browser-jwt-cannot-act-as-worker` HTTP 401 (wrong signer); forged identity headers HTTP 401; additionally the entire `/internal/*` surface returns 403 `EXECUTION_SURFACE_DISABLED` under the deployed Stage B posture (`MILO_ENABLE_EXECUTION_CONTROL=false`); gateway route allowlist refuses `/internal/*` (E2E suite) |
| Worker cannot act as gateway | PASS | live matrix: HTTP 403 (verified Google token, identity not in gateway allowlist) |
| Minimum API→worker IAM permission correct | PASS (code+docs) / operator item (live binding) | launcher sends `containerOverrides` (`backend/job_launcher.py`) ⇒ `roles/run.jobsExecutorWithOverrides`; docs corrected; the live binding on the staging job needs the operator (IAM mutations are blocked for the automation identity) |
| Mock lifecycle completes in staging | PASS | run `819e1014…`: completed, 12 events, 3 checkpoints, 3/3 reservations settled, heartbeats, blackboard, 4 shadow decisions, mock usage only |
| Cancellation works | PASS | pre-start: run `af9b6bd4…` → `cancelled` with `run_cancelled` event; mid-execution: run `bd95753e…` → `cancelled` after cooperative check |
| Idempotency works | PASS | same key ⇒ same run (`created=false`) live on staging, including against a `launch_unknown` run; concurrency-safe in the PG suite |
| Retry/budget limits work | PASS | model calls: `budget_exhausted/MODEL_CALL_LIMIT_REACHED` at exactly 25; tokens: `budget_exhausted/TOTAL_TOKEN_LIMIT_EXCEEDED` at 3600/3000 + `overage` ledger row; daily user budget: `DAILY_USER_BUDGET_REACHED` across runs; duration/retry gates covered by unit + isolated E2E suites |
| Stale-worker writes atomically rejected | PASS | migration `20260810000300`: every worker durable write lease-guarded (FOR SHARE); full scenario in PG suite + unit suite + live staging probe (SQLSTATE 55000) |
| Worker lease reclaim demonstrated | PASS | live twice: crashed attempt 1 → attempt 2 reclaim with new worker id completed the happy run |
| Events/checkpoints/terminal writes respect the lease | PASS | guarded RPCs + `mark_run_complete/failed` now carry (worker_id, attempt, lease_token); proven in PG suite and live |
| launch_unknown creates no duplicate execution | PASS | replay of a `launch_unknown` run returns the same run with state preserved; direct requeue matches 0 rows; only confirmed-not-launched → requeue transitions succeed (live) |
| Reconciliation tooling verified | PASS | script list mode run locally (read-only, MANUAL without a DB URL); its exact listing query and guarded resolution CTEs executed against staging |
| Staging cannot reference production | PASS | deployer hard-fails on the production project id; deployed env/images/SAs/secret refs enumerated — all staging-scoped; Supabase URL is the staging ref |
| Relevant tests green | PASS | 677 backend tests (591 unit + 86 PostgreSQL migration, zero skips); frontend 60 unit + secret/static checks + build + 15 isolated E2E; checkers (migrations/secret/unsafe-default) pass; shellcheck clean |

## Live-testing discoveries fixed during Stage B

Live staging execution surfaced three genuine defects invisible to the
offline suites, each fixed with code + migration + regression:

1. **postgrest-py cannot parse single-composite RPC responses** — writes
   committed but the client crashed afterwards. Fix: all HTTP-facing RPCs
   return SETOF (`20260810000300` restated, `20260810000400` `_v2`
   wrappers); regression pins `proretset` for every repository RPC.
2. **`run_usage_ledger` rejected `overage`/`released` decisions** the
   budget tracker writes (`20260810000500` widens the check; regression
   covers all five decisions).
3. **Checkpoint rows carried non-schema columns** (`worker_id`,
   `lease_token`) that PostgREST would reject — removed; lease context now
   travels as RPC parameters.

## Evidence notes and residual items

- **Identity matrix evidence (AUTHMX, 10/10 PASS):** a disposable
  app-layer instance of the API image (placeholder database credentials,
  deleted after the run) was driven by Cloud Run jobs running AS
  `milo-gateway-staging@` and `milo-worker-staging@`, minting real
  Google-signed OIDC tokens from the metadata server. Gateway: accepted
  (502 = auth passed, placeholder repo failed as designed), wrong
  audience 401, cannot-act-as-worker 403, forged headers 401. Worker:
  accepted-then-rate-limit-fail-closed 503, wrong audience 401,
  cannot-act-as-gateway 403, browser JWT 401. Both identities also
  probed the deployed private API and were rejected 403 at the Cloud Run
  ingress (private, no invoker bound).
- **Cloud Run IAM bindings (operator, before API-driven E2E/Stage C):**
  the automation tooling cannot mutate IAM policies, so three bindings
  remain for the operator; until then the deployed staging API is
  reachable by no identity (fail-closed):
  1. `roles/run.jobsExecutorWithOverrides` on `milo-agent-worker-staging`
     for `milo-api-staging@…` (API→worker launch);
  2. `roles/run.invoker` on `milo-agent-api-staging` for
     `milo-gateway-staging@…` and 3. for `milo-worker-staging@…`.
- **Production follow-up before Stage C:** apply migrations
  `20260810000100`–`20260810000500` via the manual procedure; until then
  the anon-EXECUTE gap and the ledger/PostgREST defects must be assumed
  present in production. The production launcher binding should also be
  verified to be `roles/run.jobsExecutorWithOverrides` (docs previously
  said `run.invoker`).
- **call_seq collision across attempts:** a reclaimed run whose earlier
  attempt reserved call_seq N fails closed (`BUDGET_SETTLEMENT_FAILED`)
  rather than double-spending — safe, but resumed paid runs will need a
  per-attempt seq or reservation reuse policy before Stage C.
- **Environment-limited:** `check-migration-state.sh`/
  `reconcile-launch-unknown.sh` remote modes need an operator psql URL
  (equivalent SQL executed via the management API instead); production
  Supabase has no inspection channel from this tooling.
- Staging migration history rows `20260810000300` reflect the
  pre-restatement content; the restated (SETOF) definitions were applied
  and verified — a fresh environment applying the repository files
  reaches the identical schema.

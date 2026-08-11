# Stage B acceptance report

Date: 2026-08-10 (corrective pre-merge pass: 2026-08-11). Release under
test: branch
`claude/milo-stage-b-staging-prep-ea0198` (final SHA recorded in the PR).
Staging: GCP `milo-agent-staging-xxxx` (us-central1), Supabase
`cxlwavxvwgrfikkudtzf`. Production `big-cabinet-457321-t7` was read-only
throughout; its worker execution count was zero before and after.

## Overall status

**PASS.** The operator applied the three staging Cloud Run IAM bindings
(verified read-only via `get-iam-policy` on 2026-08-11), and the real
end-to-end API-driven launch path then ran live and passed: a Cloud Run
job running as `milo-gateway-staging@` minted a real Google-signed OIDC
token for the deployed private API and drove the full path — `POST
/conversations/{id}/runs` (HTTP 202) → launcher (`cloud_run_job` mode,
`roles/run.jobsExecutorWithOverrides`) → worker job execution
`milo-agent-worker-staging-zth8n` → run `c7412f33…` `queued → running →
completed` with 12 lifecycle events, 3/3 reservations settled through
the attempt-aware guarded path, a DB-clock heartbeat row, and an
idempotent replay (same key → HTTP 202, same run id). See "Live
API-driven E2E". Production follow-up before Stage C: migrations
`20260810000100`–`000600`.

## Acceptance table

| Criterion | Status | Evidence |
| --- | --- | --- |
| Isolated staging stack exists | PASS | `milo-agent-api-staging` / `milo-agent-worker-staging` / `milo-redis-shim-staging` in `milo-agent-staging-xxxx`; images `…/milo-agent-staging/{api,worker}` at full commit SHAs |
| Staging Supabase correctly migrated | PASS | 22 migrations recorded (001–015, ts grants, 000100–000600); schema/RLS/ACL matrix verified live |
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
| Relevant tests green | PASS | 688 backend tests (596 unit + 92 PostgreSQL migration, zero skips); frontend 60 unit + secret/static checks + build + 15 isolated E2E; checkers (migrations/secret/unsafe-default) pass; shellcheck clean |
| API-driven launch E2E (API → launcher → Cloud Run Job) | PASS | live 2026-08-11 after operator IAM bindings: gateway-identity `POST /conversations/{id}/runs` 202 → execution `milo-agent-worker-staging-zth8n` → run `c7412f33…` completed; idempotent replay returned the same run; probe exit 0 |

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

## Corrective pre-merge pass (2026-08-11)

An independent review of PR #42 produced five findings; four were code
defects, fixed in migration `20260810000600` + repository/worker changes
(applied to staging and verified live), and the fifth is this report's
status change to PASS_PENDING_OPERATOR_E2E.

1. **Cross-run settlement isolation (fixed):**
   `settle_model_call_budget_guarded` previously verified the lease for
   `p_run_id` but settled by reservation id alone, so a valid lease for
   run A could settle run B's reservation. The guarded settle now performs
   the settlement itself in a single UPDATE whose WHERE clause binds the
   reservation to `(p_run_id, p_attempt, status='reserved')`; zero rows ⇒
   `RESERVATION_RUN_MISMATCH_OR_SETTLED` (SQLSTATE 55000). PG regression:
   two runs, valid lease for A, attempt to settle B's reservation →
   rejected, B's reservation unchanged
   (`test_settle_guard_rejects_cross_run_reservation`).
2. **Database-clock lease enforcement (fixed):** usage snapshots,
   heartbeats and terminal worker transitions previously compared
   `lease_expires_at` against an application-generated timestamp. All
   worker-originated run-row mutations now go through
   `update_run_usage_guarded` / `heartbeat_run_guarded` /
   `transition_run_worker_guarded`, whose predicates use PostgreSQL
   `now()` with the `(run_id, worker_id, attempt, lease_token)` invariant;
   control-plane (non-worker) transitions keep their existing service
   path. PG regression proves a skewed container clock cannot let an
   expired worker write
   (`test_db_clock_decides_lease_expiry_for_every_guarded_run_write`).
3. **Acceptance status (this document):** changed from unconditional PASS
   to **PASS_PENDING_OPERATOR_E2E** — the real API→launcher→Cloud Run Job
   path had not run live (blocked on the operator IAM bindings). Resolved
   on 2026-08-11: the operator applied the bindings and the live
   API-driven E2E passed (see "Live API-driven E2E"), returning the
   overall status to **PASS**.
4. **Staging dependency isolation (fixed):** `ENVIRONMENT=staging` now
   fail-closes at startup unless `MILO_EXPECTED_SUPABASE_PROJECT_REF` and
   `MILO_EXPECTED_REDIS_HOST` are set and match the configured
   `SUPABASE_URL` / Redis endpoint (`STAGING_DEPENDENCY_UNPINNED` /
   `STAGING_DEPENDENCY_MISMATCH`; dependency values are never echoed).
   The staging deployer pins both variables on every deploy and refuses
   Upstash-cloud hosts unless explicitly overridden.
5. **Per-attempt reservation identity (fixed):** reservation identity is
   now `(run_id, attempt, call_seq)` (attempt column + unique index in
   `20260810000600`); the guarded reserve path uses the lease's attempt.
   Conservative under crash/reclaim: earlier-attempt rows are never
   mutated or removed and `model_call_budget_committed` counts every
   reserved/settled/overage row regardless of attempt, so a possibly-spent
   provider call from a dead attempt never disappears from budget
   accounting (`test_attempt_aware_reservations_do_not_collide_and_stay_counted`).

Staging was re-synced after the fixes: migration `20260810000600` applied
and verified (schema, function ACL matrix, live SQLSTATE-55000 guard
probes), images rebuilt at the corrective SHA, and the API service and
worker job updated with the new images plus the dependency-pin
environment variables (the API revision became READY under the
fail-closed pin validation). A fresh directly-triggered mock happy run
(`1a518a6b…`, execution `milo-agent-worker-staging-r7l27`) completed
entirely through the new DB-clock guarded RPCs: terminal transition via
`transition_run_worker_guarded`, usage snapshot via
`update_run_usage_guarded`, heartbeat row via `heartbeat_run_guarded`,
and 3/3 attempt-1 reservations settled through the attempt-aware
reserve/settle path with zero dangling reservations.

## Live API-driven E2E (2026-08-11, after operator IAM bindings)

Precondition: all three Cloud Run IAM bindings verified read-only via
`get-iam-policy` (worker job: `roles/run.jobsExecutorWithOverrides` for
`milo-api-staging@` only; API service: `roles/run.invoker` for
`milo-gateway-staging@` and `milo-worker-staging@` only).

A disposable Cloud Run job (`python:3.12-slim`, deleted after the run)
executed AS `milo-gateway-staging@`, minted a real Google-signed OIDC
token (audience = the deployed API URL) from the metadata server, and
drove the deployed private staging API end to end:

1. `GET /health` → 200 (Cloud Run ingress admitted the gateway identity;
   app accepted the gateway token).
2. `POST /conversations/24022382…/runs` with
   `metadata.mock_scenario=happy` and idempotency key
   `e2e-api-launch-9f9ab9a-1` → **202**, run
   `c7412f33-f1a5-48d9-afe3-72b638b0bbc8`, `launch_state=launched`.
3. The API's launcher (running as `milo-api-staging@`, `cloud_run_job`
   mode) created worker execution **`milo-agent-worker-staging-zth8n`**
   with `RUN_ID` container overrides — the `run_invocations` row records
   the launch operation at the same second the execution was created.
4. Polled through the API: `queued → running → completed`; final usage
   `actual_cost=0.003` (3 mock calls), 12 lifecycle events
   (`run_created … run_completed`), 3/3 reservations settled through the
   attempt-aware guarded path, one DB-clock heartbeat row, zero dangling
   reservations.
5. Idempotent replay of the same `POST` → 202 with the SAME run id
   (`created=false` path), after completion.
6. Probe summary `E2E SUMMARY: PASS … final=completed`, container
   exit 0; execution succeeded (`succeededCount=1`).

Zero-cost invariants held throughout: `MILO_WORKER_ENGINE=mock`,
`MILO_ENABLE_PAID_EXECUTION=false`, no provider credential exists in the
staging project, and production was not touched.

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
- **Cloud Run IAM bindings — APPLIED (2026-08-11):** the automation
  tooling cannot mutate IAM policies, so the operator applied the three
  bindings; they were then independently verified read-only via
  `get-iam-policy`:
  1. `roles/run.jobsExecutorWithOverrides` on `milo-agent-worker-staging`
     for `milo-api-staging@…` (API→worker launch);
  2. `roles/run.invoker` on `milo-agent-api-staging` for
     `milo-gateway-staging@…` and 3. for `milo-worker-staging@…`.
- **Production follow-up before Stage C:** apply migrations
  `20260810000100`–`20260810000600` via the manual procedure; until then
  the anon-EXECUTE gap, the ledger/PostgREST defects, and the
  cross-run/clock/attempt hardening gaps must be assumed present in
  production. The production launcher binding should also be verified to
  be `roles/run.jobsExecutorWithOverrides` (docs previously said
  `run.invoker`).
- **call_seq collision across attempts:** RESOLVED by the corrective
  pass — reservation identity is `(run_id, attempt, call_seq)` as of
  `20260810000600`; see "Corrective pre-merge pass".
- **Environment-limited:** `check-migration-state.sh`/
  `reconcile-launch-unknown.sh` remote modes need an operator psql URL
  (equivalent SQL executed via the management API instead); production
  Supabase has no inspection channel from this tooling.
- Staging migration history rows `20260810000300` reflect the
  pre-restatement content; the restated (SETOF) definitions were applied
  and verified — a fresh environment applying the repository files
  reaches the identical schema.

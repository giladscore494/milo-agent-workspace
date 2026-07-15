# Staged production activation runbook

Four stages, each gated on explicit operator sign-off. There is
deliberately no one-command enable-all procedure anywhere in this
repository. All stages are `REQUIRES_MANUAL_OPERATOR_CONFIGURATION`.

## Stage A — Code deployment with execution disabled

Operator actions (in order):

1. apply approved authentication/ownership/lifecycle migrations manually
   ([MIGRATIONS.md](MIGRATIONS.md));
2. run post-migration validation (`check-migration-state.sh` re-run);
3. perform the membership backfill (generate → review → apply manually);
4. perform the proposal backfill where required;
5. deploy the worker job configuration — do **not** execute it;
6. deploy the private API;
7. configure the Vercel gateway (Connection 3);
8. keep every execution flag off (`MILO_ENABLE_*`,
   `GATEWAY_ALLOW_EXECUTION_ROUTES`);
9. keep paid execution off (`MILO_ENABLE_PAID_EXECUTION` unset/false);
10. verify authentication (sign-in on the production domain);
11. verify project membership (member reads succeed);
12. verify proposal ownership (unowned proposals are invisible);
13. verify the read-only workspace UI;
14. verify cross-user rejection;
15. run `scripts/release/smoke-test-read-only.sh`;
16. run `scripts/release/smoke-test-execution-disabled.sh`.

Acceptance: workspace reads operate; unauthorized reads/writes fail; no
worker execution occurred (`gcloud run jobs executions list` shows none);
no provider call occurred; no paid budget reservation exists; execution
remains disabled.

## Stage B — Infrastructure connection without paid execution

1. configure production Redis (Connection 6); 2. verify TLS;
3. configure the worker service identity (Connection 2);
4. configure API→worker invocation permission (Connection 4);
5. verify service-to-service token validation (worker route accepts the
   worker identity, rejects gateway/browser);
6. configure the mock/no-cost adapter only in an isolated staging stack —
   the test adapters (`CLOUD_RUN_AUTH_MODE=e2e-test`,
   `MILO_E2E_INPROCESS_WORKER`) are hard-forbidden in production
   configuration, so lifecycle rehearsal happens in staging;
7. keep the provider key absent or inaccessible;
8. keep paid execution off;
9. test the lifecycle with mocked dependencies only;
10. verify cancellation; 11. verify stale-worker rejection;
12. verify retry and budget blocking; 13. verify launch-state
    reconciliation tooling (`reconcile-launch-unknown.sh` list mode).

Acceptance: Redis-backed rate limits operate; browser cannot invoke worker
routes; gateway cannot impersonate the worker; worker cannot impersonate
the gateway; the mocked lifecycle succeeds; no real provider call occurs.

## Stage C — One controlled paid smoke run

Manual only; never executed as part of repository work or CI.

Prerequisites: Stages A and B signed off; provider API key entered
manually into `<PROVIDER_KEY_SECRET_NAME>` (worker-only access); strict
caps configured and verified (`MILO_MAX_COST_PER_RUN`, per-run token/call/
duration/retry caps, `MILO_DAILY_USER_BUDGET`,
`MILO_DAILY_PROJECT_BUDGET`); kill switch rehearsed
(`MILO_ENABLE_PAID_EXECUTION` off + secret binding removal);
operator-controlled test user and project exist; cost monitoring ready;
rollback commands prepared (`generate-rollback-plan.sh`).

Actions: 1. enable only the minimum run-creation surface
(`MILO_ENABLE_RUN_CREATION` plus `GATEWAY_ALLOW_EXECUTION_ROUTES`);
2. restrict access to the operator-controlled test user/project;
3. keep broad access disabled; 4. execute exactly one controlled run;
5. verify run-creation idempotency (retry returns the same run);
6. verify worker claim + heartbeat; 7. verify events; 8. verify
checkpoints; 9. verify the result; 10. verify the cancellation path with a
separate mock/controlled test; 11. verify token usage; 12. verify actual
cost; 13. verify reservation settlement; 14. verify no orphan reservation;
15. verify daily budget accounting; 16. disable execution immediately if
any invariant fails.

Acceptance record (no secret values): run ID, release SHA, image digests,
start/end time, model identifier, token totals, actual cost, budget
decision, terminal state, operator identity.

## Stage D — Gradual expansion

1. one project; 2. small allowlist; 3. limited daily budget; 4. monitored
expansion; 5. periodic security review; 6. periodic cost review;
7. periodic stale-run and launch-reconciliation review; 8. rollback
rehearsal; 9. wider access only after explicit approval.

Each expansion step raises limits explicitly and individually — never all
at once.

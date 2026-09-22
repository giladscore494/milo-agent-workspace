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
9b. keep catalog execution off (`MILO_ENABLE_CATALOG_EXECUTION` unset/false on
    the worker job). The Stage A deployment contract already pins it, so this
    is a verification step, not a change;
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
remains disabled; `MILO_ENABLE_CATALOG_EXECUTION` is `false` on the deployed
worker job (`gcloud run jobs describe --format json`), so no Government tool is
registered and no canonical promotion pipeline is constructed in any run.

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
8b. keep catalog execution off — the mocked lifecycle rehearsal needs no
    catalog capability, and Stage B is not the stage that authorizes one;
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

Actions: 0. leave `MILO_ENABLE_CATALOG_EXECUTION` **off**. Stage C is one
controlled paid run, not an authorization to write canonical catalog rows;
`scripts/release/stage-c/verify_caps.py` refuses the run if the flag is enabled
on either surface, and `parse_env_contract.py` keeps it `false` in the
smoke-active posture too; 1. enable only the minimum run-creation surface
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
decision, terminal state, operator identity, and the observed value of
`MILO_ENABLE_CATALOG_EXECUTION` on the worker revision (expected `false`).

### Catalog execution — a separate stage, separately authorized

Enabling `MILO_ENABLE_CATALOG_EXECUTION` is **not** part of Stage A, B, C or D
and is never a side effect of a release. It is its own decision, and it needs
its own explicit authorization, because it is what turns a durable Government
snapshot into canonical catalog writes.

Prerequisites before it may even be proposed:

1. Stages A and B signed off, and a Stage C acceptance record that shows the
   flag was `false` throughout;
2. a read-only inspection establishing what the target database actually holds
   (schema, functions, RLS, snapshot/candidate/canonical row counts) — an
   enabled catalog over an unverified schema is exactly the posture this flag
   exists to prevent;
3. the rollback rehearsed: set the flag `false` and verify per
   [ROLLBACK.md](ROLLBACK.md) §"Catalog execution";
4. monitoring for the two catalog events bound to a real system
   ([MONITORING_AND_INCIDENTS.md](MONITORING_AND_INCIDENTS.md)) — which remains
   operator work; this repository configures no alert.

Acceptance, when it is eventually authorized (no secret values): the worker
revision digest, the observed flag value, the run ID, the count of
`catalog_variant_promoted` and `catalog_promotion_refused` events, the distinct
refusal codes seen, and the canonical row count before and after. A refusal is
an operational outcome and does not fail that acceptance; an unexplained
promotion does.

### The first live Government capture — a further separate step

**As of 2026-09-16 the capture entrypoint exists in code and no live capture
has been executed.** `backend/catalog/operator_capture.py` (CODE-1) is an
operator-invoked controller that refuses by default; the same
`MILO_ENABLE_CATALOG_EXECUTION` flag gates it, so with the flag off it
constructs no transport, connects to no database, claims no run and captures,
ingests and activates nothing. Enabling the flag does **not** start a capture:
nothing invokes the entrypoint automatically, and it has no schedule.

Running it is its own step, after the flag stage above, and it needs:

1. **AUTH-1** — explicit, current authorization for one bounded live capture
   (outbound read-only, no model spend);
2. **OPERATOR-0's read-only schema report**, completed and read — the
   entrypoint requires an explicit acknowledgement of this and refuses without
   it;
3. `MILO_ENABLE_PAID_EXECUTION` off — a capture requires no model spend and
   must not be bundled with one;
4. a prepared operator capture run, because every durable catalog write is
   lease-guarded. Preparing one is a supported, server-side, operator-only
   command in the same entrypoint (`--prepare`); it takes atomic ownership of
   the run's launch so no model worker can ever execute it, and it captures
   nothing. **Preparing a run is not authorization to capture**, and capturing
   is not authorization to run MILO against the result.

The exact arguments, the prepared-run contract, the fixed resource and bounds,
the sanitized report fields and the stop conditions are in
[../catalog-code1-operator-capture.md](../catalog-code1-operator-capture.md).
Acceptance for the capture itself (no secret values): the outcome, the snapshot
key and id, the content checksum, the schema fingerprint, the declared and
stored record counts, the page count, the candidate and status counts, the
normalization contract and issue counts, and whether the snapshot activated.

## Stage D — Gradual expansion

1. one project; 2. small allowlist; 3. limited daily budget; 4. monitored
expansion; 5. periodic security review; 6. periodic cost review;
6b. periodic catalog review where catalog execution has been separately
authorized (refusal-code distribution, canonical row growth, any promotion
nobody expected);
7. periodic stale-run and launch-reconciliation review; 8. rollback
rehearsal; 9. wider access only after explicit approval.

Each expansion step raises limits explicitly and individually — never all
at once.

### Stage D is currently UNAUTHORIZED; expansion step 1 attempt 1 FAILED (`timed_out`), attempt 2 is PROPOSED

Stage C passing did not enable, authorize or schedule any Stage D activity:
the Stage C Attempt 7 authorization is **consumed**
([STAGE_C_ACCEPTANCE.md](STAGE_C_ACCEPTANCE.md)). Every Stage D step needs
its own fresh, separate, explicit operator authorization.

**Expansion step 1 — one bounded paid run — was executed ONCE (attempt 1,
2026-09-19, key `stage-d-expansion-1-20260918-01`, run `3772fc84…`) and
terminalized `timed_out`, which the acceptance policy counts as a controlled
fail-closed FAILURE; no acceptance record exists. Attempt 2 exists as a
reviewable PROPOSAL and has NOT been executed.** The request, the read-only discovered
production baselines, the caps derived from Stage C Attempt 7 evidence and
the remaining manual operator steps are in
[STAGE_D_AUTHORIZATION.md](STAGE_D_AUTHORIZATION.md); the operator toolkit
that would execute it is `scripts/release/stage-d/`, with its executable
safety proofs in `tests/test_stage_d_toolkit.py`. Neither the document nor
the toolkit is an authorization, and merging them changes no production
state.

Note what that first step deliberately does NOT do, against the "raises
limits" framing above: **it raises no limit at all.** The only thing it
expands is the number of authorized production runs, by exactly one, at the
current reviewed release. Every budget cap is held or tightened relative to
Stage C, and the worker-only provider envelope is restored to the Stage C
Attempt 7 values. Raising a limit is a later, separate step.

That toolkit also carries the reviewed resolution for the unused prepared
Government capture run
(`scripts/release/stage-d/resolve-government-capture.sh`). Preparing that
run was never authorization to capture, and a Stage D model run neither
executes it nor becomes one: the Stage D gates re-prove on every step that
it has not been claimed.

# Stage C operator toolkit — one controlled paid smoke run

Implements the Stage C procedure of
[`docs/production-readiness/STAGED_ACTIVATION.md`](../../../docs/production-readiness/STAGED_ACTIVATION.md)
as an exact, auditable command sequence. Every script is manual-only,
prints no secret values, and does exactly one step. Run them **in order**
from an operator-authenticated `gcloud` shell (the account that owns
`big-cabinet-457321-t7`). The remote automation identity is read-only in
production by design; these steps are why.

| Step | Script | Mutates | Purpose |
| --- | --- | --- | --- |
| 0 | (pre-flight already verified — see `STAGE_C_ACCEPTANCE.md`) | no | IAM/secret/flag/exec-count invariants |
| 1 | `01-build-images.sh` | registry only | build `api`+`worker` images at the release SHA via Cloud Build (public repo cloned at the exact commit, SHA verified in-build) |
| 2 | `02-deploy-images.sh` | worker job, API service | deploy release images, **flags unchanged/off**; verifies the exact historical execution baseline (2 terminal, 0 active) |
| 3 | `03-enable-stage-c.md` (**manual commands** — by policy no committed script enables execution flags) then `03b-verify-stage-c-posture.sh` (read-only) | worker job, API service | smallest-safe caps; worker: paid flag on + `KIMI_API_KEY` binding + pinned worker-only provider limits (`STAGE_C_WORKER_PROVIDER_LIMITS`); API: launcher + run creation on, **no** `MILO_PROVIDER_*` variable |
| 4 | `04-create-probes.sh` | creates 2 disposable jobs | `stagec-db-probe` (as `milo-api-runtime@`; DB checks/setup/evidence) and `stagec-gw-probe` (as `milo-vercel-gateway@`; drives the API) |
| 5 | `05-execute-smoke.sh` | one run | re-verifies launch invariants (release images + **exact** cap values on both surfaces and provider limits on the worker via `verify_caps.py`, IAM, exact terminal execution baseline via `verify_executions.py`), exact pinned prior-run DB preflight (2 historical runs, 0 rows under the Attempt 7 key), creates test data, executes exactly ONE run through API → launcher → worker → provider → Supabase; the poll exits non-zero (kill switch!) on any terminal state outside the acceptance policy (default: `completed` only) |
| 6 | `06-collect-evidence.sh` | no | **executable acceptance gate** — exits non-zero unless every criterion holds (exactly one new run/one new execution over the pinned baseline — post-run totals of 3/3, all executions terminal — terminal state, claim/heartbeat invariants, zero dangling reservations, ledger/usage accounting, cost/token/call caps, idempotent replay, zero secret markers) |
| 7 | `07-post-smoke-posture.sh` | worker job, API service, deletes probes | fail-closed posture: run creation off, launcher disabled, paid off, provider-key binding removed |
| any | `kill-switch.sh` | worker job, API service | immediate fail-closed (use at ANY sign of trouble) |

## Attempt 7 exact baselines (pinned in `stage-c-env.sh`)

Attempts 5 and 6 left real production history that the gates now verify
exactly — never an empty system, and never merely "some run exists":

| Quantity | Before Attempt 7 (preflight) | After Attempt 7 (evidence) |
| --- | --- | --- |
| Database runs (total) | exactly **2** (Attempt 5 `58daa7de…` + Attempt 6 `37912575…`) | exactly **3** |
| Runs under the Attempt 7 key `stage-c-smoke-attempt-7-20260819` | exactly **0** | exactly **1** (the new Run, verified by Run ID) |
| Worker executions (total) | exactly **2**, every one terminal, **0 active** | exactly **3**, every one terminal |
| New Runs/Worker executions allowed | — | exactly **one** of each |
| Stage C acceptance terminal state | — | `completed` **only** |

Historical runs and terminal executions are preserved evidence: they are
never deleted, cancelled, rewritten or hidden, and a historical failed run
can never satisfy the Attempt 7 acceptance (the new Run must carry the
fresh idempotency key and be verified by its Run ID). Count mismatches,
active executions and unparseable listings all fail closed. The live
operator preflight must fail if production does not match these exact
pinned counts.

Notes:

- The Vercel gateway is deliberately untouched: `GATEWAY_ALLOW_EXECUTION_ROUTES`
  stays off, so no browser can reach run creation while the backend flag is
  on; the only caller that passes gateway auth is `stagec-gw-probe`
  (running as the approved gateway service account), and membership
  authorization further restricts run creation to the dedicated
  `stage-c-smoke` test user/project/conversation.
- The API keeps `MILO_ENABLE_PAID_EXECUTION=false`: it never holds the
  provider key (production config validation forbids the paid flag without
  it); the worker is the only paid-execution enforcement point and the only
  identity able to read `KIMI_API_KEY`.
- Do **not** run a second paid run. `05-execute-smoke.sh` refuses to run
  unless the worker execution history matches the pinned baseline exactly
  (2 terminal historical executions, zero active) — any additional or
  active execution blocks the run.
- Caps are defined once in `stage-c-env.sh` (`STAGE_C_CAPS`); see
  `docs/production-readiness/STAGE_C_ACCEPTANCE.md` for the sizing
  rationale against the preserved pipeline shape. `verify_caps.py`
  enforces the exact values on both worker and API immediately before run
  creation and fails on any missing/changed/unexpected budget variable.
- The worker-only provider operating envelope is defined once in
  `stage-c-env.sh` (`STAGE_C_WORKER_PROVIDER_LIMITS`) and deliberately
  sits below the operator-confirmed Kimi Tier 2 account ceiling
  (concurrency 100 / RPM 500 / TPM 3,000,000 / TPD unlimited).
  `verify_caps.py` enforces the exact seven values on the worker, fails
  closed on missing/changed/unexpected `MILO_PROVIDER_*` variables there,
  and fails on ANY `MILO_PROVIDER_*` variable on the API.
- **Cost ceiling caveat**: `MILO_MAX_COST_PER_RUN=3.00` bounds tracked
  token-derived cost only. Moonshot's `$web_search` tool is billed per
  invocation outside MILO's accounting — conservative total exposure for
  the one run is ≤ $9.00 (see the cost-ceiling section of
  `STAGE_C_ACCEPTANCE.md`). Verify the actual billed total in the
  Moonshot console after the run.
- Acceptance policy: only `completed` is a PASS terminal state.
  `failed`/`cancelled`/`timed_out`/`budget_exhausted`/`partial_success`
  fail the smoke test — the poll and evidence gates exit non-zero and
  instruct you to run `kill-switch.sh`.

# Stage D operator toolkit — PROPOSED authorization for ONE bounded paid run

> ## STATUS: PROPOSED — NOT AUTHORIZED, NOT EXECUTED
>
> **Nothing in this directory has been run against production.** No run was
> created, no Worker execution was launched, no flag was changed, no secret
> was bound, no probe job was created, and no database row was written or
> deleted. Every number below was obtained **read-only**.
>
> **Merging this PR authorizes nothing.** The one bounded paid run described
> here is the first Stage D expansion step of
> [`STAGED_ACTIVATION.md`](../../../docs/production-readiness/STAGED_ACTIVATION.md),
> and executing it requires a **fresh, explicit, separate operator
> authorization**. Stage C passing does not supply it: the Stage C Attempt 7
> authorization is **consumed**, and
> [`STAGE_C_ACCEPTANCE.md`](../../../docs/production-readiness/STAGE_C_ACCEPTANCE.md)
> says so in terms.
>
> Full proposal, caps rationale, discovered baselines and the remaining
> manual operator steps:
> [`STAGE_D_AUTHORIZATION.md`](../../../docs/production-readiness/STAGE_D_AUTHORIZATION.md).

## What this expansion step is — and what it deliberately is not

`STAGED_ACTIVATION.md` describes Stage D as gradual expansion whose steps
"raise limits explicitly and individually — never all at once". **This first
step raises no limit at all.** The only thing it expands is the number of
authorized production runs, by exactly one, at the current reviewed release.
Every budget cap is held or **tightened** relative to Stage C; the provider
envelope is restored to the Stage C Attempt 7 values, which is a tightening
of what production carries today. Widening the allowlist, the daily budget
or the project count are separate later steps and are not proposed here.

| Dimension | Stage C (consumed) | This Stage D step |
| --- | --- | --- |
| Authorized paid runs | 1 (Attempt 7, spent) | exactly 1, new key |
| Projects | 1 (`Stage C smoke`) | 1 (`stage-d-smoke`, new) |
| Budget caps | baseline | **tightened, never raised** |
| Provider envelope | concurrency 2 | concurrency 2 (**restores** the live drift to 8) |
| Catalog execution | `false` throughout | `false` throughout |
| Browser/Vercel execution surface | disabled | disabled |
| Government capture | n/a | **never executed; invariant enforced** |

## Discovered production baselines (read-only, 2026-09-18)

Everything the gates pin was measured, not assumed.

| Quantity | Live value | After the one authorized run |
| --- | --- | --- |
| `public.runs` rows | **7** | exactly **8** |
| Rows under the Stage D key `stage-d-expansion-1-20260918-01` | **0** | exactly **1** |
| Visible Worker executions | **7**, every one terminal, **0 active** | exactly **8**, every one terminal |
| API image digest | `sha256:04275e81…` (accepted; serving revision runs it) | unchanged — never rebuilt |
| Worker image digest | `sha256:d3743e5a…` (accepted; tag resolves to it) | unchanged — never rebuilt |
| API `MILO_ENABLE_RUN_CREATION` / `JOB_LAUNCHER` | `false` / `disabled` | restored to `false` / `disabled` |
| Worker + API `MILO_ENABLE_PAID_EXECUTION` | `false` | restored to `false` |
| Worker + API `MILO_ENABLE_CATALOG_EXECUTION` | `false` | **`false` throughout** |
| `KIMI_API_KEY` bound to a runtime | **neither** | bound to the Worker only, then unbound |
| `KIMI_API_KEY` secret accessor IAM | `milo-worker-runtime@` only | unchanged |
| Worker job executor IAM | `milo-api-runtime@` only | unchanged |
| `MILO_PROVIDER_MAX_CONCURRENCY` (worker) | **8** (drift) | **2** (Attempt 7 value) |
| Disposable probe jobs | **absent** | created, then deleted **and proven absent** |

The seven existing run rows are `stage-c-smoke-0001` (failed),
`stage-c-smoke-attempt-7-20260819` (completed), four `swarm-v2-smoke-*`
rows, and the prepared Government capture
`catalog-government-capture-20260919-01` (queued). The seven executions are
`milo-agent-worker-{mcfrx,gggdc,dk4xv,gnj5d,fvfcb,2tckh,bw8kj}`.

A count **below** a pinned baseline fails exactly like a count above it: a
row or execution that vanished is as much a drift as one that appeared, and
no gate ever deletes or hides history to make an increment look right.

## The prepared Government capture run is an invariant, never a Stage D run

`555101dc-46f6-4048-bd67-efccbc98f528` (`status=queued`,
`launch_state=none`, `worker_id=NULL`, `started_at=NULL`, zero rows in every
trace table) is an operator-prepared capture run. Stage D **never executes
it, never claims it and never counts it as authorization to capture.**

Two repository facts already make it unreachable, and Stage D **proves**
them on every gate rather than assuming them:

* `try_acquire_launch` acquires only from `launch_state` `pending` or
  `launch_failed` (`backend/repository/supabase.py`), so a run resting in
  `none` can never be taken by the ordinary launcher; and
* the Worker resolves its target from the `RUN_ID` environment variable and
  never polls for queued rows (`backend/worker/main.py` `resolve_run_id`),
  so nothing sweeps it up.

`probe_db.py` re-reads the row in **preflight**, in **evidence** and in the
standalone **govcheck** mode, and fails closed unless it is still either
*prepared* (`queued`/`none`) or *retired* (`cancelled`/`none`) with no
worker, no lease, no start and zero trace rows. `probe_gateway.py` refuses
before any API call if the Stage D key or the run request would borrow the
capture's identity, and `06-collect-evidence.sh` refuses to accept that run
id at all.

`resolve-government-capture.sh` is the reviewed resolution. Default mode is
read-only and prints the exact SQL. Apply mode, behind the full operator
guard, retires the run with **two guarded compare-and-set statements in one
transaction** following the repository's own state machine
(`queued → cancellation_requested → cancelled`), each asserting
`row_count = 1` and rolling the whole transaction back otherwise. Both steps
share one transaction deliberately: `claim_run_lease` **can** acquire from
`cancellation_requested` but never from `cancelled`, so the intermediate
state is never externally visible. There is no unconditional `UPDATE`
anywhere, no run row is ever deleted, and the capture is never executed.

## Steps

Run them **in order** from an operator-authenticated `gcloud` shell (the
account that owns `big-cabinet-457321-t7`). Every script is manual-only,
prints no secret values, and does exactly one step. The remote automation
identity is read-only in production by design; these steps are why.

| Step | Script | Mutates | Purpose |
| --- | --- | --- | --- |
| 0 | `resolve-government-capture.sh` (read-only by default) | no (apply mode: one guarded CAS) | resolve the unused prepared capture run — retire it, or record an audited leave-prepared decision |
| 1 | `01-verify-release-images.sh` | **no** | prove, BY DIGEST, that production still serves the accepted release. Never builds, pushes, deploys or re-tags |
| 2 | `02-guarded-run.md` (**one pasteable manual block**) | drives steps 3–7 | arms an `EXIT`/`ERR`/`INT`/`TERM` cleanup trap **before the first mutation**, then runs enable → verify → probes → run → evidence → lockdown. Every exit path ends fail-closed with both probes proven absent |
| 3 | `03-enable-stage-d.md` (**manual commands**) then `03b-verify-stage-d-posture.sh` (read-only) | worker job, API service | strict caps; worker: paid flag on + `KIMI_API_KEY` binding + the pinned worker-only provider envelope; API: launcher + run creation on, **no** `MILO_PROVIDER_*` |
| 4 | `04-create-probes.sh` | creates 2 disposable jobs | `stage-d-db-probe` (as `milo-api-runtime@`) and `stage-d-gw-probe` (as `milo-vercel-gateway@`). Both run the **digest-pinned** probe image; the probe **sources are SHA-256 verified** before any gcloud call; the created templates are verified afterwards |
| 5 | `05-execute-run.sh` | one run | re-verifies every launch invariant **including the accepted digests**, runs the DB preflight and setup, creates exactly ONE run, polls; persists the run id to `state.json` before polling |
| 6 | `06-collect-evidence.sh` | no | **executable acceptance gate**; reads the run id from `state.json` |
| 7 | `07-post-run-lockdown.sh` | worker job, API service, deletes probes | kill switch → **terminalize the DATABASE run and prove it clean** → **prove the capture was never claimed** (both while the probe still exists) → delete both probes → **prove them absent** |
| any | `kill-switch.sh` | worker job, API service | immediate fail-closed (use at ANY sign of trouble) |

Steps 3–7 are normally driven by the single guarded block in step 2
rather than run one at a time; running them individually forfeits the
automatic cleanup that block provides.

### There is no build step, and that is deliberate

A smoke-run toolkit would normally build and deploy the release. Stage D
does neither, because **rebuilding this release is not byte-reproducible**:
`Dockerfile.api` and `Dockerfile.worker` start `FROM python:3.12-slim` (a
mutable upstream tag), `backend/requirements.txt` carries the unpinned
floor `openai>=1.30.0`, and there is no lockfile or `--require-hashes`.
A rebuild of commit `84cd8696…` can therefore produce different bytes,
and pushing them under the same `:<sha>` Artifact Registry tag would
**replace** the accepted image while every tag-based check still reported
success. "Re-proving" a release by rebuilding it is not a proof.

So the accepted release is identified by **digest**, not by tag:

| Image | Accepted digest |
| --- | --- |
| api | `sha256:04275e81995d7bbaf23d0e71e71c2ac83adf37f45eca8686ddb812050a18caa6` |
| worker | `sha256:d3743e5a8dabc3f663970abe83886ea91b030ad7b339e1178d0ab5efad8f64b5` |

`verify_images.py` checks that the registry tag still resolves to those
digests, that the immutable serving API revision runs the API digest, and
that the Worker job resolves to the Worker digest. **A digest mismatch
blocks Stage D and requires a separate reviewed release** — it is never
auto-repaired by re-pushing the tag.

This is not a hypothetical risk in this project. A Cloud Run *job* is not
a revision: its template holds a tag that is resolved afresh at every
execution, and the Worker tag has already resolved to several distinct
digests over time (execution `milo-agent-worker-bw8kj` ran
`sha256:2314852868a8…`). So the job check is point-in-time: step 5
re-verifies immediately before run creation, and the evidence gate
verifies the digest the authorized execution **actually ran**, which is
recorded on the execution and cannot be invalidated afterwards.

### The probe jobs are a privileged supply chain

`stage-d-db-probe` runs with `SUPABASE_SERVICE_ROLE_KEY` bound, so whatever
that job runs executes arbitrary code with service-role access to
production. Two inputs decide what it runs, and **both are pinned and
verified before any credentialed job exists**:

| Input | Pin | Checked |
| --- | --- | --- |
| Runtime image | `python@sha256:78387bc3…` (digest, never the mutable `python:3.12-slim` tag) | after creation, and again **before every execution** |
| `probe_db.py` source | `STAGE_D_PROBE_DB_SHA256` | before any gcloud call |
| `probe_gateway.py` source | `STAGE_D_PROBE_GW_SHA256` | before any gcloud call |

`verify_probe_jobs.py` also checks each job's service account and its
exact secret bindings (db probe: only the two Supabase secrets that
identity already accesses; gateway probe: **none**), and refuses a
provider-key alias on either.

An approved Artifact Registry mirror would be preferable to pulling a
privileged runtime from a public registry. None exists today — the
project has exactly one Artifact Registry repository, `milo-agent`, and
it is a STANDARD repository, not a REMOTE one (verified read-only). That
is a production mutation and is deliberately outside this PR. Switching
later is a **one-line** change to `STAGE_D_PROBE_IMAGE_REPO`, because
mirroring preserves the manifest digest: the pin stays byte-identical.

### Cancelling an execution is not closing a run

Cancelling a Cloud Run execution does **not** make the database run
terminal. The Worker installs no `SIGTERM` handler, so an interrupted run
can sit in `running` indefinitely — holding its lease, its
`MILO_MAX_CONCURRENT_RUNS_PER_{USER,PROJECT}` slot and its budget
reservations. A cleanup that only cancels the execution leaves all of
that behind.

So the lockdown terminalizes the **database** run too, through
`probe_db.py --terminalize`: identity-checked (it refuses any run not
carrying the authorized Stage D key, and can never touch the prepared
capture), using the repository's own supported lifecycle
(`active → cancellation_requested → cancelled`, each step a guarded
compare-and-set on the observed status), and releasing any dangling
reservation through the supported `settle_model_call_budget` RPC. Losing
a compare-and-set to a worker writing its own terminal result is fine and
is retried; what must hold is the **proof**:

* the run is terminal;
* zero active runs remain for its user **and** its project;
* zero reservations remain in status `reserved`.

`LOCKDOWN COMPLETE` is impossible unless those hold **and** the
Government-capture invariant is proven.

### Probe logs are attributed to one execution

Every probe execution is launched with `--async` so its name is captured
before completion, and its logs are filtered by
`labels."run.googleapis.com/execution_name"`. Filtering by job name alone
can match retained records from an older, deleted-and-recreated job of the
same name — a stale PASS satisfying a gate that is really looking at
nothing. A missing record for the current execution fails closed.

## The acceptance gate

`06-collect-evidence.sh` plus `probe_db.py --evidence` is a gate, not a
checklist. It exits non-zero unless **all** of the following hold:

* **terminal state** is in the acceptance policy (`completed` only);
* **model calls** actually happened (`usage.model_calls > 0`) with matching
  **tokens** and non-empty **reservations** — a run that made no provider
  call cannot pass as a paid-run proof;
* **cost**: the per-row-rounded ledger total, the `run.usage` snapshot and
  the **unrounded** reservation total are each within `MILO_MAX_COST_PER_RUN`,
  with no rounding tolerance ever loosening the cap;
* **reservations**: every one settled, **zero dangling**, reconciled
  one-to-one with the ledger by `call_seq` within the 6-decimal per-row
  rounding half-ulp;
* **heartbeat**: at least one, matching the claiming worker and attempt;
* **lease**: `lease_expires_at` present — the run held a real bounded lease;
* **claim invariants**: `attempt = 1`, `launch_state = launched`,
  `worker_id`/`started_at`/`finished_at`/`last_heartbeat_at` all present,
  exactly one `run_invocations` row;
* **idempotent replay**: a post-completion POST with the same key returns
  the SAME run id and creates no new run and no new Worker execution;
* **exact one-run/one-execution increment**: exactly `7 + 1 = 8` database
  rows, exactly **1** row under the Stage D key, and exactly `7 + 1 = 8`
  visible Worker executions, every one terminal with zero active;
* **the authorized execution RAN the accepted Worker digest** — read off
  the execution itself, so a tag moved afterwards cannot hide it;
* **the probe jobs were still the reviewed jobs** at every execution —
  pinned image digest, expected identity, expected secret bindings;
* **Government-capture invariant** still intact;
* **zero secret markers** in DB events and in worker logs.

## Notes

- **Caps are derived from Stage C Attempt 7 evidence and nothing is
  raised.** The derivation rule, the two documented exceptions
  (`MILO_MAX_OUTPUT_TOKENS_PER_RUN` keeps a wider margin;
  `MILO_MAX_RETRIES` is **held** at 15 because Stage C Attempt 6 failed at
  `RETRY_LIMIT_REACHED`) and the full before/after table live in
  `stage-d-env.sh` and in `STAGE_D_AUTHORIZATION.md`.
- **The provider envelope is worker-only.** `verify_caps.py` enforces the
  exact seven values on the Worker, fails closed on missing/changed/
  unexpected `MILO_PROVIDER_*` variables there, and fails on **any**
  `MILO_PROVIDER_*` variable on the API.
- **The API never holds the paid flag or the provider key.** Production
  config validation forbids the paid flag without a key; the Worker is the
  only paid-execution enforcement point and the only identity able to read
  `KIMI_API_KEY`.
- **The browser/Vercel execution surface stays disabled.**
  `GATEWAY_ALLOW_EXECUTION_ROUTES` is never turned on, so no browser can
  reach run creation while the backend flag is on. The only caller that
  passes gateway auth is `stage-d-gw-probe`, running as the
  operator-controlled approved gateway service account, and project
  membership then confines run creation to the dedicated `stage-d-smoke`
  user/project/conversation.
- **A dedicated project is not cosmetic.** `queued` is an active run state,
  so the prepared capture row counts against
  `MILO_MAX_CONCURRENT_RUNS_PER_{USER,PROJECT}=1` for *its* user and
  project. Stage D's own project also pins `workflow_key =
  vehicle_catalog_v1`, because the caps are derived from Stage C evidence
  about that pipeline; the setup probe refuses a forbidden project id or a
  mismatched workflow key.
- **Cost ceiling — read this before authorizing.**
  `MILO_MAX_COST_PER_RUN=1.00` is a hard cap on **tracked token-derived
  cost only**. Moonshot bills the builtin `$web_search` tool per
  invocation, outside MILO's accounting, and **nothing in this repository
  caps the number of invocations**: `core.py` bounds tool-echo rounds at
  `MAX_TOOL_ROUNDS = 15` but iterates every `message.tool_calls` entry
  within a round, and that count is the provider's to choose. Total spend
  is therefore **not bounded by this repository**. A verified hard
  spending/wallet ceiling on the provider account is a **prerequisite**
  of the authorization. The official per-call fee is currently $0.005 and
  the legacy `$web_search` tool is announced for retirement on
  2026-10-20; re-check both before the run, and verify the actual billed
  total after it. See `STAGE_D_AUTHORIZATION.md` §3.6.
- **Acceptance policy**: only `completed` is a PASS.
  `failed`/`cancelled`/`timed_out`/`budget_exhausted`/`partial_success`
  fail the run — the poll and evidence gates exit non-zero and instruct
  you to run `kill-switch.sh`.
- **The consumed Stage C constants are never sourced, reused or edited.**
  `scripts/release/stage-c/` stays exactly as it is, as the audited Stage C
  record. Stage D has its own `STAGE_D_*` namespace, its own fresh key and
  its own live baselines.

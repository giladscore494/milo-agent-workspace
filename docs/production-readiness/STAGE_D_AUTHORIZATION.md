# Stage D expansion step 1 — PROPOSED authorization for one bounded paid run

> ## STATUS: PROPOSED. NOT AUTHORIZED. NOT EXECUTED.
>
> **Nothing in this proposal has been executed against production.** No run
> was created, no Worker execution was launched, no flag was changed, no
> secret was bound, no probe job was created, no Cloud Run job or service
> was mutated, and no database row was written, updated or deleted. Every
> number in this document was obtained **read-only** on 2026-09-18.
>
> **Merging this PR authorizes nothing.** This document is a *request* for
> one bounded paid production run, together with the toolkit that would
> execute it. Running it requires a **fresh, explicit, separate operator
> authorization**. Stage C passing does not supply that authorization: the
> Stage C Attempt 7 authorization is **consumed**, and
> [`STAGE_C_ACCEPTANCE.md`](STAGE_C_ACCEPTANCE.md) states in terms that
> Stage D remains unauthorized.
>
> **This is not a record of a completed stage.** No acceptance record
> exists for this stage and none may be written until the run has actually
> happened and the executable gate has passed. The results tables below are
> deliberately empty.

- **Classification:** `REQUIRES_MANUAL_OPERATOR_CONFIGURATION`
- **Toolkit:** [`scripts/release/stage-d/`](../../scripts/release/stage-d/)
  (see its [`README.md`](../../scripts/release/stage-d/README.md))
- **Executable safety proofs:** `tests/test_stage_d_toolkit.py`
- **Runbook context:** [`STAGED_ACTIVATION.md`](STAGED_ACTIVATION.md), Stage D
- **Pinned release:** `84cd8696119c24662a954d0f0e23195268dab23f`
- **Production project / region:** `big-cabinet-457321-t7` / `us-central1`
- **Proposed run identity:** `stage-d-expansion-1-20260918-01` (fresh; zero
  pre-existing rows)

## 1. What this expansion step is — and what it deliberately is not

`STAGED_ACTIVATION.md` describes Stage D as gradual expansion whose steps
"raise limits explicitly and individually — never all at once". **This
first step raises no limit at all.**

The only dimension it expands is *the number of authorized production runs*,
by exactly one, at the current reviewed release. Every budget cap is held or
**tightened** relative to Stage C. The provider envelope is **restored** to
the Stage C Attempt 7 values, which is a tightening of what production
carries today. Widening the allowlist, raising the daily budget, and adding
projects are separate later steps and are **not** proposed here.

| Dimension | Stage C (consumed) | This step |
| --- | --- | --- |
| Authorized paid runs | 1 (Attempt 7, spent) | exactly 1, new key |
| Projects | 1 (`Stage C smoke`) | 1 (`stage-d-smoke`, new) |
| Budget caps | baseline | **tightened, never raised** |
| Provider envelope | concurrency 2 | concurrency 2 (**restores** the live drift to 8) |
| Catalog execution | `false` throughout | `false` throughout |
| Browser / Vercel execution surface | disabled | disabled |
| Government capture | n/a | **never executed; invariant enforced** |

Explicitly **out of scope**, and not authorized by this document: enabling
`MILO_ENABLE_CATALOG_EXECUTION`; executing the prepared Government capture;
enabling `GATEWAY_ALLOW_EXECUTION_ROUTES` or any browser execution surface;
a second paid run; any change to the Stage C toolkit or its consumed
constants.

## 2. Discovered production baselines (read-only, 2026-09-18)

Every pinned value was **measured**, not assumed. Method: the read-only
Supabase production connection for database facts, and `gcloud … describe` /
`… list` for Cloud Run, IAM and Secret Manager facts. No mutating command
was issued.

### 2.1 Database — `public.runs` holds exactly **7** rows

| Run ID | Status | Idempotency key |
| --- | --- | --- |
| `37912575-f9ce-4437-893d-7dfa45c53aa9` | `failed` | `stage-c-smoke-0001` |
| `8b4a4277-fdf0-41b2-8515-d7e1d50e441b` | `completed` | `stage-c-smoke-attempt-7-20260819` |
| `0d44d491-bc40-404e-9642-a5b8f77f3441` | `cancelled` | `swarm-v2-smoke-20260824-04c1094` |
| `986ac9ec-a423-4da7-81d3-4a84ffabc181` | `failed` | `swarm-v2-smoke-attempt-2-20260824-04c1094` |
| `0b1b7329-3a88-4155-b422-5e89bf5e01bc` | `failed` | `swarm-v2-smoke-20260824-4fecdfe-01` |
| `5bd80a2e-ae7b-4c8c-aa0d-624ec28931ec` | `completed` | `swarm-v2-smoke-20260825-4dbdcd6-01` |
| `555101dc-46f6-4048-bd67-efccbc98f528` | `queued` | `catalog-government-capture-20260919-01` |

Rows under the proposed key `stage-d-expansion-1-20260918-01`: **0**.

### 2.2 Cloud Run — exactly **7** Worker executions, every one terminal, **0 active**

`milo-agent-worker-mcfrx`, `-gggdc`, `-dk4xv`, `-gnj5d`, `-fvfcb`, `-2tckh`,
`-bw8kj`.

### 2.3 Runtime posture

| Fact | Live value |
| --- | --- |
| API image digest (accepted) | `sha256:04275e81995d7bbaf23d0e71e71c2ac83adf37f45eca8686ddb812050a18caa6` |
| Worker image digest (accepted) | `sha256:d3743e5a8dabc3f663970abe83886ea91b030ad7b339e1178d0ab5efad8f64b5` |
| Serving API revision runs the accepted API digest | yes (`milo-agent-api-00080-nm8`) |
| Worker job image reference | the **mutable tag** `:84cd8696…`, which currently resolves to the accepted digest |
| API URL / ready revision | `https://milo-agent-api-beplbca7yq-uc.a.run.app` / `milo-agent-api-00080-nm8` |
| `MILO_ENABLE_RUN_CREATION` (API) | `false` |
| `JOB_LAUNCHER` (API, Worker) | `disabled` |
| `MILO_ENABLE_PAID_EXECUTION` (both) | `false` |
| `MILO_ENABLE_PROPOSAL_MUTATIONS` / `_READS` / `_RUN_CANCELLATION` / `_EXECUTION_CONTROL` | `false` |
| `MILO_ENABLE_CATALOG_EXECUTION` (both) | `false` |
| `KIMI_API_KEY` / `MOONSHOT_API_KEY` bound to a runtime | **neither**, in any form |
| `KIMI_API_KEY` secret accessor IAM | `milo-worker-runtime@…` only (exactly one binding) |
| Worker job `roles/run.jobsExecutorWithOverrides` | `milo-api-runtime@…` only |
| Worker job `timeoutSeconds` / `maxRetries` / `taskCount` | `3600` / `1` / `1` |
| Cloud Run jobs present | `milo-agent-worker` only — **no stale probe jobs** |
| `MILO_PROVIDER_MAX_CONCURRENCY` (Worker) | **`8`** — drift from the Attempt 7 value of `2` |

**Both surfaces already serve the accepted release**, so Stage D neither
builds nor deploys anything — see §2.6.

### 2.4 One drift found, and it is a tightening to fix

The Worker carries `MILO_PROVIDER_MAX_CONCURRENCY=8`, introduced by the
later swarm-v2 smoke work. Stage C Attempt 7 succeeded under `2`. This
proposal restores `2`, and `verify_caps.py` refuses the run while the live
value is anything else. Nothing else in the live posture deviates from what
the proposal requires.

### 2.5 Expected post-run baselines

| Quantity | Before | After exactly one authorized run |
| --- | --- | --- |
| `public.runs` rows | **7** | exactly **8** |
| Rows under `stage-d-expansion-1-20260918-01` | **0** | exactly **1** |
| Visible Worker executions | **7**, all terminal, 0 active | exactly **8**, all terminal, 0 active |

A count **below** a pinned baseline fails exactly like a count above it: a
row or execution that vanished is as much a drift as one that appeared. No
gate ever deletes or hides history to make an increment look right.

### 2.6 Why Stage D verifies digests and never rebuilds

An earlier revision of this proposal carried a build step and a deploy
step, described as idempotent no-ops that would "re-prove" the pinned
release. **That was wrong and has been removed.** Rebuilding this release
is not byte-reproducible:

- `Dockerfile.api` and `Dockerfile.worker` both start `FROM
  python:3.12-slim`, a **mutable** upstream tag that is re-published;
- `backend/requirements.txt` pins most packages but carries
  `openai>=1.30.0`, an **unpinned floor** that resolves to whatever is
  newest at build time;
- there is no lockfile and no `--require-hashes`, so transitive
  dependencies float as well.

A rebuild of commit `84cd8696…` can therefore produce different image
bytes. Pushed under the same `:<sha>` Artifact Registry tag, those bytes
would **replace** the accepted, Stage-A-accepted image — while every
tag-based check continued to report success. A rebuild is a new release
wearing the old label, and "re-proving" a release by rebuilding it proves
nothing.

So the accepted release is identified by **digest**:

| Image | Accepted digest |
| --- | --- |
| api | `sha256:04275e81995d7bbaf23d0e71e71c2ac83adf37f45eca8686ddb812050a18caa6` |
| worker | `sha256:d3743e5a8dabc3f663970abe83886ea91b030ad7b339e1178d0ab5efad8f64b5` |

`01-verify-release-images.sh` and `verify_images.py` verify, read-only,
that the registry tag still resolves to those digests, that the immutable
serving API revision runs the API digest, and that the Worker job
resolves to the Worker digest. **A tag match alone is never acceptance,
and a digest mismatch blocks Stage D** — it means production is no longer
serving the accepted release, which requires a separate reviewed release.
Nothing in the toolkit rebuilds, pushes, deploys or moves a tag.

**This risk is already realised in this project.** A Cloud Run *service*
resolves the tag once, at revision creation, and the revision is
immutable — which is why the API check is strong. A Cloud Run *job* is
not a revision: its template holds the tag and resolves it afresh at
**every execution**. The Worker tag has already resolved to several
distinct digests over this project's history — Worker execution
`milo-agent-worker-bw8kj` (2026-08-24) ran
`sha256:2314852868a8…`, which is not the accepted digest. The Worker
check is therefore point-in-time, and the toolkit says so and closes the
window: `05-execute-run.sh` re-verifies immediately before run creation,
and the evidence gate verifies the digest the authorized execution
**actually ran**, read off the execution record itself, where a tag moved
afterwards cannot hide it.

### 2.7 The probe jobs are a privileged supply chain

`stage-d-db-probe` runs with `SUPABASE_SERVICE_ROLE_KEY` bound. Whatever
image and source that job runs therefore executes arbitrary code with
service-role access to production, so both are pinned:

| Input | Pin |
| --- | --- |
| Probe runtime image | `docker.io/library/python@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea` |
| `probe_db.py` | `STAGE_D_PROBE_DB_SHA256` |
| `probe_gateway.py` | `STAGE_D_PROBE_GW_SHA256` |

An earlier revision created both probes from the **mutable** tag
`python:3.12-slim`, which Docker Hub re-publishes: the job could have
begun executing different code with those credentials between one
execution and the next, with nothing noticing. It also transported
whatever probe source happened to be on disk, so a dirty or unreviewed
checkout would have shipped unreviewed privileged code.

Now the sources are hash-verified **before any gcloud call**, both jobs
are created from the digest, the created templates are verified, and the
image, identity and secret **references** are re-verified **immediately
before every probe execution**. The references are checked, not just the
env names — `SUPABASE_URL` must be backed by `SUPABASE_URL:latest` and
`SUPABASE_SERVICE_ROLE_KEY` by `SUPABASE_SECRET_KEY:latest`, with no
extras and none at all on the gateway probe — because the name is only a
label and the reference is what is actually read. The repository is
spelled canonically (`docker.io/library/python`) and both sides are
normalised before comparison, so Cloud Run's own rewriting cannot fail the
check spuriously while the digest stays exactly enforced. — a Cloud Run job template can be updated between
creation and execution, and the credentials are what make that worth
checking.

**Mirror posture.** An approved Artifact Registry mirror is preferable to
pulling a privileged runtime from a public registry. None exists today:
the project has exactly one Artifact Registry repository, `milo-agent`,
and it is a STANDARD repository, not a REMOTE one (verified read-only
2026-09-19). Creating one is a production mutation and is deliberately
outside this PR. Switching to it later is a one-line reviewed change to
`STAGE_D_PROBE_IMAGE_REPO`, because mirroring preserves the manifest
digest — the pin above stays byte-identical either way.

### 2.8 Cancelling an execution does not close a database run

Cancelling a Cloud Run execution does **not** make the database run
terminal: `backend/worker/main.py` installs no `SIGTERM` handler. An
interrupted run can therefore sit in `running` indefinitely, holding its
lease, its `MILO_MAX_CONCURRENT_RUNS_PER_{USER,PROJECT}` slot and its
budget reservations. An earlier revision's cleanup cancelled the
execution and stopped there, and never read the recorded `run_id` at all.

The lockdown now terminalizes the database run as well, while the db
probe still exists. It is identity-checked — it refuses any run not
carrying the authorized Stage D key, and refuses the prepared Government
capture outright — and it uses the repository's own supported lifecycle
(`active → cancellation_requested → cancelled`, each step a guarded
compare-and-set on the observed status, so a worker writing its own
terminal result is never overwritten) plus the supported service-role
`settle_model_call_budget` RPC to release any dangling reservation.

It then PROVES: the run is terminal; zero active runs remain for its user
and for its project; zero reservations remain in status `reserved`.
`LOCKDOWN COMPLETE` is impossible unless those hold **and** the
Government-capture invariant is proven.

### 2.9 The run id can go missing, and an absent id is not "no run"

`05-execute-run.sh` creates the run inside the gateway probe and writes
`run_id` to `state.json` only after parsing the probe's structured output.
If the shell, the Cloud Shell session, the pipeline or probe-log retrieval
dies in between, the run **exists** but the automatic cleanup is handed an
empty id. An earlier revision then merely *counted* active rows under the
key and failed without terminalizing anything — and the lockdown went on
to delete the credentialed db probe, leaving an active run holding its
lease, its concurrency slot and its budget reservations, with no reader
left to fix it.

`terminalize` now recovers instead of assuming:

| Rows under the pinned idempotency key | Behaviour |
| --- | --- |
| 0 | proved no-run verdict |
| 1 | **recovered**, then the full identity gate, terminalization, reservation cleanup and proofs |
| >1 | fails closed, having mutated nothing |

A recovered row is checked as strictly as a recorded one: the idempotency
key, the run-request metadata marker (`stage: stage-d-smoke`), the
`user_id` and `conversation_id` that `state.json` recorded **before** the
run was created, an explicit refusal of the prepared Government capture,
and a refusal of anything carrying the operator-capture marker. Dangling
reservations are released even when the recovered run is already terminal,
because a finished run can still hold reserved budget.

### 2.10 The preflight verifies the cleanup RPC

`terminalize` releases reservations through
`settle_model_call_budget(p_reservation_id, p_actual_cost, p_status,
p_rejection_reason)`. Verified read-only against production on 2026-09-19:
the function exists with exactly that signature, is `SECURITY DEFINER`,
`service_role` may execute it, and `anon`/`authenticated` may not.

The preflight now requires it alongside the guarded RPCs, so a missing
function, a changed signature or a probe that cannot reach it refuses the
run **before any production enable** — a cleanup that cannot release a
reservation would leave the daily budget held against a run that will
never finish.

## 3. Proposed caps — derived from Stage C Attempt 7 evidence

### 3.1 The evidence base (read from production, read-only)

Stage C Attempt 7, run `8b4a4277-fdf0-41b2-8515-d7e1d50e441b`, terminal
`completed`, actually consumed:

| Quantity | Observed |
| --- | --- |
| Model calls | **84** |
| Input tokens | 277,882 |
| Output tokens | 34,136 |
| Total tokens | **312,018** |
| Tracked `actual_cost` | **$0.252069** (unrounded reservations $0.2520692) |
| `estimated_cost` | $1.68 (84 × $0.02) |
| Agent steps | 32 |
| Elapsed | 934.235 s |
| Retries / backpressure events | 0 / 0 |
| Reservations | 84, all settled, **0 dangling** |

### 3.2 Derivation rule

**Cap = the smallest round value ≥ 1.75 × the Attempt 7 observation, and
never above the Stage C cap.** Two deliberate exceptions are documented
below. Nothing is raised.

| Variable | Stage C | A7 actual | **Stage D** | Change |
| --- | --- | --- | --- | --- |
| `MILO_MAX_MODEL_CALLS_PER_RUN` | 200 | 84 | **150** | −25% |
| `MILO_MAX_INPUT_TOKENS_PER_RUN` | 700000 | 277,882 | **500000** | −29% |
| `MILO_MAX_OUTPUT_TOKENS_PER_RUN` | 250000 | 34,136 | **120000** | −52% |
| `MILO_MAX_TOTAL_TOKENS_PER_RUN` | 900000 | 312,018 | **600000** | −33% |
| `MILO_MAX_ESTIMATED_COST_PER_RUN` | 4.00 | 1.68 | **3.00** | −25% |
| `MILO_MAX_COST_PER_RUN` | 3.00 | 0.252069 | **1.00** | −67% |
| `MILO_MAX_RUN_DURATION_SECONDS` | 3300 | 934.235 | **1800** | −45% |
| `MILO_MAX_RETRIES` | 15 | 0 | **15** | **held — see below** |
| `MILO_MAX_AGENT_STEPS` | 60 | 32 | **56** | −7% |
| `MILO_MAX_CONCURRENT_RUNS_PER_USER` | 1 | — | **1** | held |
| `MILO_MAX_CONCURRENT_RUNS_PER_PROJECT` | 1 | — | **1** | held |
| `MILO_DAILY_USER_BUDGET` | 5.00 | — | **4.00** | −20% |
| `MILO_DAILY_PROJECT_BUDGET` | 5.00 | — | **4.00** | −20% |
| `MILO_ESTIMATED_COST_PER_CALL` | 0.02 | — | **0.02** | held (reservation size, not a limit) |

### 3.3 The two documented exceptions

**`MILO_MAX_OUTPUT_TOKENS_PER_RUN` keeps a 3.5× margin rather than 1.75×.**
Output volume is the most variable dimension of the preserved pipeline
(verifier chunks plus the Hebrew summary), and 34,136 is a single small
observation. A 1.75× cap on it would be a likely false `budget_exhausted` —
which would waste the one authorization and prove nothing. It is still a
52% tightening.

**`MILO_MAX_RETRIES` is held at 15, not tightened, although Attempt 7 used
0.** Stage C Attempt 6 **FAILED at `RETRY_LIMIT_REACHED`** after repeated
provider 429s. Fifteen retries *together with* the worker-only provider
envelope is the pair that produced the successful Attempt 7. Tightening the
retry allowance would reintroduce the Attempt 6 failure mode for no exposure
benefit: the cost caps, not the retry count, bound spend.

### 3.4 Structural invariants the numbers preserve

- `MILO_MAX_ESTIMATED_COST_PER_RUN` = 150 × 0.02 = **3.00 exactly**, so the
  estimated-cost ceiling admits exactly the 150 reservations the call cap
  allows and not one more (`backend/budget.py` rejects when
  `estimated_cost + estimated_cost_per_call > cap`).
- `MILO_MAX_TOTAL_TOKENS_PER_RUN` (600000) sits just under input+output
  (620000), so the joint ceiling binds first — the same relationship Stage C
  used.
- The daily budgets (4.00) stay **above** the 3.00 estimated-reservation
  ceiling, so a daily budget can never fail the run before the per-run cap
  does.
- `MILO_MAX_RUN_DURATION_SECONDS` (1800) stays far below the Worker job's
  `timeoutSeconds` of 3600.

Each of these is asserted by a test in `tests/test_stage_d_toolkit.py`,
including a per-cap assertion that **no Stage D cap exceeds its Stage C
counterpart** and that no cap Stage C pinned has been silently dropped.

### 3.5 Worker-only provider envelope (unchanged from Attempt 7)

| Variable | Pinned | Tier 2 ceiling |
| --- | --- | --- |
| `MILO_PROVIDER_MAX_CONCURRENCY` | 2 (preserved V1 parallelism; no V2 concurrency) | 100 |
| `MILO_PROVIDER_RPM_LIMIT` | 350 | 500 |
| `MILO_PROVIDER_TPM_LIMIT` | 2400000 | 3,000,000 |
| `MILO_PROVIDER_MAX_RATE_LIMIT_RETRIES` | 5 | — |
| `MILO_PROVIDER_MAX_BACKPRESSURE_WAIT_SECONDS` | 240 | — |
| `MILO_PROVIDER_BACKOFF_BASE_SECONDS` | 2 | — |
| `MILO_PROVIDER_BACKOFF_MAX_SECONDS` | 30 | — |

Applied to the **Worker only**; `verify_caps.py` fails on any
`MILO_PROVIDER_*` variable found on the API. The Tier 2 confirmation
authorizes no provider call.

### 3.6 Cost ceiling — what the caps bound, and what they do NOT

There are two different kinds of spend here, and only one of them is
capped by anything in this repository.

**Tracked, token-derived cost — HARD-CAPPED at $1.00.**
`MILO_MAX_COST_PER_RUN` is enforced by `backend/budget.py` before every
model call and is verified after the run against three independent views
(the per-row-rounded ledger total, the `run.usage` snapshot and the
unrounded reservation total). Attempt 7's comparable run cost $0.252069.
This is a real ceiling.

**Provider-side `$web_search` tool fees — NOT CAPPED BY MILO AT ALL.**
Moonshot bills the builtin `$web_search` tool per invocation, separately
from tokens. Those charges never enter `actual_cost`, the reservation
ledger or the daily budgets, and **no MILO cap bounds the number of
invocations.**

An earlier revision of this proposal claimed a conservative total
exposure of "≤ $5.50", derived from an assumed maximum of three search
invocations per model response. **That claim was wrong and has been
withdrawn.** The assumption is not established by the provider
documentation and is not enforced by the runtime:

- `backend/engines/vehicle_catalog_v1/core.py` bounds the number of
  tool-echo *rounds* per model call at `MAX_TOOL_ROUNDS = 15`;
- within each round it iterates **every** entry of
  `message.tool_calls` (`core.py:554` and `core.py:601`) and echoes each
  one back. The number of `tool_calls` in a single response is chosen by
  the provider and is **not bounded by the runtime**;
- so the per-run invocation count is `rounds × tool_calls_per_round`, and
  only the first factor has a ceiling. There is no arithmetic that turns
  that into a dollar bound.

**Current official provider pricing (verify before authorizing).** Kimi's
documentation currently states **$0.005 per legacy `$web_search` call**,
and states that the legacy `$web_search` tool is **retired on
2026-10-20**. Both figures are the provider's and must be re-checked
against the console immediately before the run — this document is not a
pricing source, and the retirement date falls close enough to this
proposal to matter.

| Component | Bound | Basis |
| --- | --- | --- |
| Token-billed (tracked) | **≤ $1.00, hard** | enforced by the budget tracker and verified three ways after the run |
| `$web_search` tool fees (untracked) | **UNBOUNDED by MILO** | billed per invocation at the provider's stated $0.005; invocations per run are not capped by the runtime |
| Total | **not bounded by this repository** | see the mandatory control below |

**Mandatory control before authorization.** Because the repository cannot
bound the second row, the bound must come from the provider account. A
**verified hard spending/wallet ceiling on the Moonshot account** is a
**prerequisite** of this authorization, not an optional precaution. The
operator must confirm the configured ceiling, and its value, before
granting the authorization, and record it in §9.

**No runtime change is proposed here.** Adding an enforceable
per-run web-search invocation cap would mean changing
`backend/engines/vehicle_catalog_v1/core.py` — that is a runtime change
to the preserved pipeline, it would invalidate the pinned accepted image
digests, and it must be proposed and reviewed as its own release. It is
deliberately **not** bundled into this authorization request. Until such
a cap exists, the provider-account ceiling is the only enforceable bound
on tool-fee exposure.

## 4. The prepared Government capture run — an invariant, never a Stage D run

`555101dc-46f6-4048-bd67-efccbc98f528`
(`catalog-government-capture-20260919-01`) is an operator-prepared capture
run: `status=queued`, `launch_state=none`, `worker_id=NULL`,
`started_at=NULL`, `attempt=1`, and **zero rows** in `run_events`,
`run_usage_ledger`, `model_call_budget_reservations`, `worker_heartbeats`,
`run_invocations`, `run_checkpoints` and `run_blackboards`. Its
`input.metadata.milo_operation` is `catalog.government.capture`, the marker
`operator_capture.py --prepare` writes.

**Stage D never executes it, never claims it, and never counts it as
authorization for a Government capture.** Preparing a run is not
authorization to capture (`STAGED_ACTIVATION.md`), and this proposal does
not become one.

### 4.1 Why it is already unreachable — and why Stage D still proves it

- `try_acquire_launch` (`backend/repository/supabase.py`) acquires only from
  `launch_state` `pending` or `launch_failed`, so a run resting in `none`
  can never be taken by the ordinary launcher.
- The Worker resolves its target from the `RUN_ID` environment variable and
  never polls for queued rows (`backend/worker/main.py` `resolve_run_id`),
  so nothing sweeps it up.
- `MILO_ENABLE_CATALOG_EXECUTION` stays `false`, so the Government tool is
  not registered in any run.

Stage D treats all three as **invariants to prove**, not assumptions.
`probe_db.py` re-reads the row in `preflight`, in `evidence` and in a
standalone `govcheck` mode, and fails closed unless it is still either
*prepared* (`queued`/`none`) or *retired* (`cancelled`/`none`) with no
worker, no lease, no start, `attempt = 1` and zero trace rows.
`probe_gateway.py` refuses before any API call if the Stage D key or the run
request would borrow the capture's identity, and `06-collect-evidence.sh`
refuses to accept that run id at all.

### 4.2 Two facts that argue for retiring it

1. `claim_run_lease` (migration 012) predicates its CAS on status, worker
   and lease expiry **only** — not on `launch_state`. A `queued` row is
   therefore claimable by anything that calls the RPC with that run id. A
   `cancelled` row is not: terminal states are outside that `WHERE` clause
   entirely. Retiring converts a convention into an enforced database fact.
2. `queued` is an **active** run state (`ACTIVE_RUN_STATES`), so the row
   counts against `MILO_MAX_CONCURRENT_RUNS_PER_USER`/`_PER_PROJECT` = 1 for
   its user and project indefinitely — a standing invitation for a future
   operator to "clear it" in a hurry, unguarded.

### 4.3 The reviewed resolution

`scripts/release/stage-d/resolve-government-capture.sh`. Default mode is
**read-only** and prints the exact SQL. Apply mode, behind the full
protected operator guard (`--apply --environment production
--expected-project … --expected-account … --expected-sha …
--confirm-production-change` plus
`MILO_OPERATOR_ACK=I_UNDERSTAND_THIS_CHANGES_PRODUCTION`), offers two
reviewed outcomes:

- **`retire`** (recommended) — two **guarded compare-and-set** statements in
  **one transaction**, following the repository's own state machine
  (`backend/runtime.py` `VALID_TRANSITIONS`: `queued →
  cancellation_requested → cancelled`; a direct `queued → cancelled` is not
  a supported transition and is deliberately not forged). Each statement
  restates the full expected pre-state in its `WHERE` clause and asserts
  `row_count = 1`; anything else raises and rolls the whole transaction
  back, leaving the row untouched. Both steps share one transaction on
  purpose: `claim_run_lease` **can** acquire from `cancellation_requested`,
  so that intermediate state is never allowed to become externally visible.
- **`leave-prepared`** — no database mutation; an audited operator decision
  to leave the run prepared for a future capture under its own AUTH-1
  authorization.

There is **no unconditional `UPDATE`** anywhere, no run row is ever deleted,
no other run is touched, and the capture is never executed. This is not
merely asserted: `tests/test_stage_d_toolkit.py` **executes the emitted SQL
against a real ephemeral PostgreSQL** with production's actual `runs`
constraints and proves it applies exactly once, refuses and fully rolls back
on twelve different drifted pre-states, leaves other rows untouched, and
deletes nothing.

## 5. Safety posture held throughout

| Control | Posture |
| --- | --- |
| `MILO_ENABLE_CATALOG_EXECUTION` | `false` on both surfaces, verified before the run and restored by the lockdown |
| Vercel / browser execution surface | untouched; `GATEWAY_ALLOW_EXECUTION_ROUTES` never enabled |
| Run-creation caller | only `stage-d-gw-probe`, running as the operator-controlled approved gateway service account |
| Authorized user/project | the dedicated `stage-d-smoke` identity; before any write the setup probe refuses a forbidden project id, a non-`vehicle_catalog_v1` workflow key, an unexpected configuration, any membership other than exactly the test user **as `owner`**, or any active run for that user or project |
| Provider secret | Worker-only, as a Secret Manager binding, never a literal, never on the API |
| Paid-execution enforcement | the Worker alone; the API keeps `MILO_ENABLE_PAID_EXECUTION=false` |
| Probe jobs | disposable, deleted by the lockdown and **proven absent** |
| Kill switch | available at every step; the lockdown runs it and verifies every postcondition |

## 6. Procedure (operator, manual, in order)

| Step | Script | Mutates |
| --- | --- | --- |
| 0 | `resolve-government-capture.sh` | no by default; apply mode does one guarded CAS |
| 1 | `01-verify-release-images.sh` | **no** — digest verification only |
| 2 | `02-guarded-run.md` (one pasteable manual block) | drives 3–7 under an armed cleanup trap |
| 3 | `03-enable-stage-d.md` (manual) → `03b-verify-stage-d-posture.sh` | flags, caps, envelope, secret binding |
| 4 | `04-create-probes.sh` | creates 2 disposable jobs |
| 5 | `05-execute-run.sh` | **the one run** |
| 6 | `06-collect-evidence.sh` | no (executable acceptance gate) |
| 7 | `07-post-run-lockdown.sh` | kill switch + capture proof + probe deletion, all verified |
| any | `kill-switch.sh` | immediate fail-closed |

Steps 3–7 are driven by the single guarded block in step 2. Its cleanup
trap is armed **before the first mutation** and fires on `EXIT`, `ERR`,
`INT` and `TERM`, so every exit path — success, failure, a failed enable
command, a half-created probe pair, Ctrl-C during the poll — ends with
the kill switch applied and both probe jobs deleted **and proven absent**.
The run id and working directory are persisted to `state.json` as they
come into existence, so neither the evidence gate nor the cleanup depends
on an operator copying an id out of a terminal.

## 7. The evidence gate

The run is acceptable **only** if `06-collect-evidence.sh` exits zero. It
verifies: terminal state `completed`; `usage.model_calls > 0` with matching
tokens and a non-empty reservation set; all three cost views (ledger,
`run.usage`, unrounded reservations) within `MILO_MAX_COST_PER_RUN` with no
rounding tolerance; every reservation settled with **zero dangling** and a
one-to-one `call_seq` reconciliation against the ledger; at least one
heartbeat matching the claiming worker and attempt; a real bounded lease;
`attempt = 1`, `launch_state = launched`, exactly one `run_invocations` row;
an idempotent replay returning the **same** run id with no new run and no
new Worker execution; the probe jobs still being the reviewed jobs at
every execution; exactly **8** database rows and exactly **1** under
the Stage D key; exactly **8** visible Worker executions, all terminal, zero
active; **the digest the authorized execution actually ran equals the
accepted Worker digest**; the Government-capture invariant intact; and zero
secret markers in database events and worker logs.

Acceptance policy: **`completed` only**. `failed`, `cancelled`, `timed_out`,
`budget_exhausted` and `partial_success` are controlled fail-closed
terminals — they prove the safety rails held, but they FAIL this step.

## 8. Remaining manual operator steps

None of these can be performed by repository automation:

1. **Grant the authorization.** A human operator must decide that this one
   bounded paid run is authorized, and record that decision. Nothing in this
   PR constitutes it.
2. **Choose the Government-capture resolution** (`retire` or
   `leave-prepared`) and run step 0 with the full operator guard.
3. **Verify a hard provider-account spending/wallet ceiling is configured**
   on the Moonshot account, and record its value. This is a PREREQUISITE,
   not a precaution: MILO caps tracked token cost only, and nothing in
   this repository bounds `$web_search` tool fees (§3.6). Also re-check
   the current per-invocation fee (officially $0.005) and the announced
   2026-10-20 retirement of the legacy `$web_search` tool.
4. **Run steps 1–7** from an authenticated `gcloud` shell owning
   `big-cabinet-457321-t7`. Step 3 is typed by hand: by policy no committed
   script enables an execution flag.
5. **Restore `MILO_PROVIDER_MAX_CONCURRENCY` to 2** as part of step 3; the
   run is refused while the live value is 8.
6. **Verify the actual billed total** (tokens *and* tool fees) in the
   Moonshot console after the run.
7. **Record the outcome** in §9 below — including a failure, if that is what
   happens.

## 9. Results — EMPTY (the run has not happened)

| Field | Value |
| --- | --- |
| Authorization granted by / date | *(not granted)* |
| Run ID | *(none — no run was created)* |
| Worker execution name | *(none — no execution was launched)* |
| Start / end time | *(n/a)* |
| Model identifier | *(n/a)* |
| Model calls / tokens / tracked cost | *(n/a)* |
| Terminal state | *(n/a)* |
| Evidence gate verdict | *(not run)* |
| `MILO_ENABLE_CATALOG_EXECUTION` observed on the worker revision | *(n/a — expected `false`)* |
| Government capture posture after the step | *(n/a — expected prepared or retired, never claimed)* |
| Provider-account spending/wallet ceiling (value, verified by) | *(not verified — required before authorization)* |
| Moonshot console billed total | *(n/a)* |
| Post-run lockdown verdict | *(not run)* |
| Operator identity | *(n/a)* |

## 10. Verdict

- **Stage D expansion step 1 is PROPOSED and UNAUTHORIZED.** It has not
  been executed and this document records no outcome.
- The toolkit, the caps, the baselines and the Government-capture
  resolution are reviewable now; the run is not.
- Merging this PR changes no production state and grants no authorization.
  Stage C's consumed authorization and its retained toolkit are untouched.

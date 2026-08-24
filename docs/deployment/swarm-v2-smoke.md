# Swarm V2 controlled production smoke

Controller: `scripts/release/swarm-v2-smoke/run-swarm-smoke.sh`

The previous smoke attempt failed before reaching the engine because its
ad-hoc IAM preflight piped `gcloud --format=json` output into a Python
heredoc — the pipe and the heredoc competed for stdin. The committed
controller makes that impossible: every gcloud JSON output is written to a
temp file (one `mktemp -d` workspace, removed by an `EXIT` trap) and parsed
by a committed helper that takes the file as an argument. None of the
helpers read stdin; `tests/test_release_tooling_swarm_smoke.py` pins this.

## Verified environment contract

`parse_env_contract.py` encodes the deployed API/Worker contract, verified
read-only against `big-cabinet-457321-t7` / `us-central1` on 2026-08-24:

- Commander and Swarm worker model `kimi-k2.6` (allowlist `kimi-k2.6`),
  base URL `https://api.moonshot.ai/v1` — worker-only; the API has none of
  these variables and never receives a provider key.
- `MILO_SWARM_MAX_ACTIVE_WORKERS=8`, `MILO_PROVIDER_MAX_CONCURRENCY=8`.
- `MILO_MAX_COST_PER_RUN=3.00` (actual-cost cap),
  `MILO_MAX_ESTIMATED_COST_PER_RUN=4.00`, `MILO_MAX_MODEL_CALLS_PER_RUN=200`.
- One active run per user and per project
  (`MILO_MAX_CONCURRENT_RUNS_PER_USER/PROJECT=1`).
- All execution flags `false` at rest; `JOB_LAUNCHER=disabled`;
  provider keys bound to NOTHING at rest.
- Worker job: `taskCount=1`, `maxRetries=1`, `timeoutSeconds=3600`.

The same test module cross-checks every `MILO_*` name in the contract
against the names the backend actually reads (`BudgetConfig.ENV_KEYS`,
`ProviderLimitsConfig.ENV_KEYS`, worker `os.getenv` calls), so contract and
code cannot silently drift.

## Modes

| Mode | Mutates? | Purpose |
| --- | --- | --- |
| `preflight [--smoke-active]` | no | serving-revision resolution + env contract + IAM + secret IAM + admission closure |
| `execute` | YES (paid) | one worker execution for `SMOKE_RUN_ID`, then monitors to a terminal state |
| `monitor <execution>` | no | resume monitoring an execution |
| `kill` | YES | the complete canonical fail-closed shutdown (delegates to `scripts/release/stage-c/kill-switch.sh`) |
| `post-verify` | no | at-rest posture: flags off, provider key unbound, zero active executions |

`kill` is never a single-execution cancel: it reuses the hardened Stage C
kill switch verbatim, which disables all six API execution flags, sets
`JOB_LAUNCHER=disabled`, disables Worker paid execution, removes both
provider-key aliases (secret and literal) from API and Worker, cancels
every active Worker execution with a bounded settle loop, and
independently verifies each postcondition — including that the serving
API revision is fail-closed — before claiming success. The target passes
through the `STAGE_C_*` pins, which refuse any conflict with the
authorized production constants.

## Serving-revision verification

`preflight` and `post-verify` never trust the API service template:
they resolve `status.latestReadyRevisionName`, describe exactly that
revision, require it to receive 100% of traffic, and evaluate the
environment contract (flags, launcher, provider aliases, runtime service
account) against that revision's containers. Additional preflight gates:

- `MILO_ENABLE_EXECUTION_CONTROL=true` without a concrete
  `MILO_WORKER_AUDIENCE` and an explicit, non-wildcard
  `MILO_APPROVED_WORKER_IDENTITIES` fails closed.
- The worker job and serving API revision must run as the pinned runtime
  service accounts (`milo-worker-runtime@…`, `milo-api-runtime@…`).
- The `KIMI_API_KEY` Secret Manager policy may grant
  `roles/secretmanager.secretAccessor` to the worker runtime identity
  only; any other accessor fails, in every posture.

`execute` refuses to run without
`SMOKE_ACK=I_UNDERSTAND_THIS_EXECUTES_ONE_PAID_PRODUCTION_RUN`, a
lowercase-UUID `SMOKE_RUN_ID`, and a clean `--smoke-active` preflight. The
controller never edits flags or secrets itself: opening and closing the
smoke window are explicit operator actions per
`docs/production-readiness/STAGED_ACTIVATION.md`.

## Terminal semantics the smoke relies on

- Every handled Swarm V2 terminal outcome (completed, classified Commander
  failure, task/verification failure, cancellation, timeout, budget
  exhausted) is durably persisted under the active lease before the worker
  exits 0, so Cloud Run records success and never retries.
- Persistence or lease failures escape with a non-zero exit and are never
  reported as handled; Cloud Run's single retry then re-claims after lease
  expiry.
- A retry that finds the run already in a terminal state exits 0 without
  touching the run — a durably finalized run can never produce a
  `RUN_ALREADY_CLAIMED` failure loop.

## Automatic closure and semantic acceptance

`execute` and standalone `monitor` arm the canonical PR #67 shutdown
before they can proceed. Their EXIT/INT/TERM/HUP guard runs it after a clean
success and after every failure, timeout, interruption or Cloud Shell
disconnect. The original failure code is preserved unless shutdown itself
is incomplete, which is reported as a critical failure.

Cloud Run task exit code zero is not a positive-smoke verdict: handled
Swarm failures intentionally exit zero after durable finalization. After
the execution settles, the controller reads only the sanitized run and
latest-checkpoint fields from Supabase and accepts exactly a
`completed` Swarm V2 run at the expected attempt, with 1–200 model calls,
actual cost at or below USD 3.00, and a compatible `swarm_v2.1`
checkpoint. Other terminal states fail the smoke and still trigger the
automatic shutdown.

Kimi IAM is evaluated across both the secret resource policy and inherited
project IAM. During an active smoke the pinned Worker accessor is required;
any other effective accessor fails closed.

## Known historical dangling reservation (requires separate recovery)

Run `0d44d491-bc40-404e-9642-a5b8f77f3441` left budget reservation
`8b05de80-fa01-4614-bec1-37f72ca63acc` in `status=reserved`
(`estimated_cost=0.02`, `actual_cost=null`, `settled_at=null`). It predates
the current settlement guarantees and is deliberately NOT mutated, settled
or migrated by any code change: a separate, explicitly authorized
production recovery operation must settle or void this one reservation
before the next smoke acceptance, because the acceptance gate requires
zero dangling reservations. Do not add a migration that silently cleans
historical reservations.

## Commander plan repair and failure classification

A Commander completion rejected by JSON decoding, the strict contract or
the deterministic plan firewall gets exactly ONE in-run semantic repair
attempt: a second guarded model call through the same ModelGateway,
BudgetTracker reservation/settlement and ProviderScheduler, counted as one
semantic retry (provider 429 backpressure remains distinct and consumes no
retry). The repair prompt carries only a static allowlisted reason code —
never the rejected plan or raw validation text. If the second response is
also invalid the run fails closed with the stable public error code, and
the `run_failed` event additionally carries the bounded
`validation_reason` code. Worker attempt stays 1; provider/infrastructure
errors, cancellation and budget stops are never repaired.

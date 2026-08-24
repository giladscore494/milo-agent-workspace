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
| `preflight [--smoke-active]` | no | env contract + IAM + admission closure (zero active executions) |
| `execute` | YES (paid) | one worker execution for `SMOKE_RUN_ID`, then monitors to a terminal state |
| `monitor <execution>` | no | resume monitoring an execution |
| `kill <execution>` | YES | cancel the active smoke execution |
| `post-verify` | no | at-rest posture: flags off, provider key unbound, zero active executions |

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

# Monitoring and incident preparation

Status: guidance `COMPLETED_IN_CODE` (signals exist in logs/tables);
configuring a real monitoring system is
`REQUIRES_MANUAL_OPERATOR_CONFIGURATION`. No real email addresses,
project IDs or pager identities appear here — bind alerts to the operator
group chosen in the manifest copy.

## Recommended signals

| Signal | Source | Severity | Operator response |
| --- | --- | --- | --- |
| API request errors (5xx rate) | Cloud Run metrics | HIGH | check revision digest, roll back API if regression |
| Authorization failures (403/404 spikes) | API logs | MEDIUM | possible probing; review origins and identities |
| Gateway verification failures (`GATEWAY_AUTH_*`) | API logs | HIGH | audience/allowlist drift or token abuse; verify Connection 3 |
| Worker claim conflicts | `claim_run_lease` rejections | LOW | expected under retry; investigate if sustained |
| Lease loss events | worker logs | MEDIUM | worker starvation or clock issues; check job resources |
| Stale heartbeats (`worker_heartbeats`, `stuck_runs` view) | DB | HIGH | run stuck; cancel/reclaim per RUN_LIFECYCLE.md |
| `launch_unknown` count > 0 | `runs.launch_state` | HIGH | reconcile with `reconcile-launch-unknown.sh`; never auto-relaunch |
| Lost launches (`queued` + `launching`, unclaimed, quiet > 15 min) | `runs.launch_state`, `updated_at` | HIGH | reconcile with `reconcile-launch-unknown.sh` after checking Cloud Run; never auto-relaunch |
| Never-launched runs that cannot launch again (`pending` / `launch_failed` of a revised Mapping Plan) | `runs.launch_state`, `updated_at` | MEDIUM | `reconcile-launch-unknown.sh --resolution retire-not-launched`; never auto-relaunch or auto-retire |
| Launch reconciliation age (oldest unresolved) | same | MEDIUM→HIGH with age | operator review SLA |
| Run duration approaching `MILO_MAX_RUN_DURATION_SECONDS` | run rows/events | MEDIUM | investigate before hard stop triggers |
| Cancellation latency | events timeline | MEDIUM | worker heartbeat interval too long? |
| Failed settlements | `run_usage_ledger` | CRITICAL | budget integrity; execution off until explained |
| Orphan reservations (reserved, never settled) | `model_call_budget_reservations` | CRITICAL | execution off; reconcile ledger |
| Budget overages (`overage` ledger entries) | `run_usage_ledger` | HIGH | verify caps; consider lowering |
| Actual provider cost (external console) | provider console | HIGH | compare with ledger totals daily during Stage C/D |
| Retry exhaustion | run failures with attempts = cap | MEDIUM | systemic provider issue? |
| Redis failures / 503 `RATE_LIMITER_UNAVAILABLE` | gateway+API logs | HIGH | store outage — limited surfaces fail closed; restore store |
| Rate-limit rejections (429 rate) | gateway+API logs | LOW→MEDIUM | tune limits or investigate abuse |
| Provider errors | worker logs | MEDIUM | provider degradation; retries capped |
| Held provider leases (`provider_lease_quarantined` events; `provider_quota_leases.py list`) | run events + shared store | MEDIUM→HIGH as they accumulate | each one is capacity MILO will not use until a human recovers it; never auto-reclaimed. Investigate the cause; recover ONE at a time with `scripts/release/provider_quota_leases.py recover` only after verifying provider-side completion (KIMI_TIER2_LIMITS.md §8a) |
| Migration drift | `check-migration-state.sh` in scheduled audit | HIGH | unexpected remote objects/missing markers — investigate before any deploy |
| RLS denials from service paths | DB logs | HIGH | misconfigured policy or credential misuse |
| Unexpected public access (Cloud Run IAM change) | audit logs on `allUsers` bindings | CRITICAL | remove binding immediately; incident review |
| Secret-access denials | Secret Manager audit logs | HIGH | binding drift or intrusion attempt |
| `catalog_promotion_refused` rate (share of catalog outcomes in a run) | `run_events` | LOW→MEDIUM | expected while the catalog is sparse; investigate a SUSTAINED high share (see below) |
| The SAME refusal code repeating across unrelated runs | `run_events` | MEDIUM | a systematic evidence, conflict or snapshot defect rather than a per-vehicle gap |
| `catalog_variant_promoted` with `replayed=true` on runs that are not resumes | `run_events` | MEDIUM | promotion is idempotent, so a replay is normal after a resume; a replay without one means a re-derived candidate |
| Zero catalog events on runs where the catalog is ENABLED and a snapshot exists | `run_events` | MEDIUM | the path is not being reached; check `MILO_ENABLE_CATALOG_EXECUTION` on the worker revision |
| Worker `AppError` from the pending-promotion read (run left retryable, NOT terminal) | worker logs, `runs` | HIGH | an infrastructure failure, never a refusal — the run is deliberately not finalized; restore the read |

## Severity definitions

- **CRITICAL** — money or data integrity at risk: kill switches first
  (paid execution off, run creation off, launcher disabled), investigate
  second.
- **HIGH** — security boundary or availability degraded: respond within
  the operating day; disable the affected surface if in doubt.
- **MEDIUM** — investigate within days; no immediate flag change.
- **LOW** — trend review.

## Kill switches (verified order)

1. `MILO_ENABLE_PAID_EXECUTION` off — no provider spend;
2. `MILO_ENABLE_RUN_CREATION` + `GATEWAY_ALLOW_RUN_START_ROUTES` (and
   `GATEWAY_ALLOW_EXECUTION_ROUTES`) off — no new work;
3. `JOB_LAUNCHER=disabled` — no worker launches;
4. remove worker provider-secret binding — no provider access at all;
5. Cloud Run traffic to a known-good revision — full code rollback.

**Independent catalog kill switch.** `MILO_ENABLE_CATALOG_EXECUTION=false` on
the worker job closes the catalog path ON ITS OWN, without stopping the product
and without a code rollback. It is not part of the ordered escalation above: it
is the NARROW response to a catalog-specific defect, and the exact command and
its verification evidence are in [ROLLBACK.md](ROLLBACK.md). Disabling it
deletes and mutates no catalog row. The same flag closes the CODE-1 operator
capture entrypoint, which refuses before opening a socket or a database
connection while it is off.

## Government capture signals (CODE-1)

**The capture is operator-invoked and has no schedule**, so there is nothing to
alert on until somebody runs it. As of 2026-09-16 none has been run. When one
is (AUTH-1, after OPERATOR-0), the entrypoint's own sanitized report is the
record — it is bounded, deterministic and carries no raw row, response body,
URL, credential or lease material.

| Signal | Where | Severity | Response |
| --- | --- | --- | --- |
| Exit status `2` with a `CAPTURE_*` reason | the entrypoint's stdout/stderr | LOW | a prerequisite refusal: nothing was constructed, nothing was contacted, nothing changed. Read the code and fix the invocation |
| Exit status `1` with a `GOV_*` reason | the entrypoint's report | MEDIUM | the capture reached `data.gov.il` and was refused by a bound or a consistency check. No snapshot was activated; the previous one still answers |
| `CAPTURE_LEASE_LOST` or `CAPTURE_CANCELLED` | the entrypoint's report | MEDIUM | the capture stopped mid-flight (a write or heartbeat refused for a stale lease, no provable heartbeat for the lease duration, or a cancellation). Activation is the last step and is gated on complete persistence, so the snapshot is non-active and invisible to readers |
| `CAPTURE_REPOSITORY_TRANSIENT` | the entrypoint's report | HIGH | a guarded catalog write or activation failed on the network, a timeout, HTTP 408/425/429/5xx or a transient SQLSTATE, after its bounded retry. Infrastructure, not data — restore the write path, then re-run; the pending snapshot is adopted by the next operator capture run once this run's lease expires |
| `CAPTURE_REPOSITORY_REQUEST_TOO_LARGE` | the entrypoint's report | HIGH | a catalog write batch drew HTTP 413 (request body too large) even after being split down to 25 rows; nothing of it was written. The gateway's limit is not documented and was not assumed — escalate with the `code=413` log lines, which name the run, snapshot, phase and batch |
| `CAPTURE_REPOSITORY_REJECTED` | the entrypoint's report | HIGH | the database refused a catalog write's content (SQLSTATE 22/23 or a repository idempotency/ownership refusal). Never retried: a code or data defect to escalate, not to re-run |
| `CAPTURE_REPOSITORY_UNAVAILABLE` | the entrypoint's report | HIGH | a guarded catalog write or activation failed for an unclassified reason. Inspect the Supabase logs at that time before re-running |
| `activated: false` on a `changed` outcome | the entrypoint's report | HIGH | the capture was written in full and the database's completeness gate refused activation. Investigate before re-running; the snapshot is terminal and not activatable afterwards |
| `normalization_issue_count` far above the previous capture's | the entrypoint's report | MEDIUM | the register's shape may have moved. The rows are durable either way; what changed is how many could be read into an identity |

None of these is bound to an alerting system by this repository, exactly like
the two catalog events above.

## Catalog signals — what the two events mean

The catalog path emits exactly two event types, from trusted server code in
`backend/worker/main.py` carrying the bounded payload
`PromotionAttempt.as_event()` builds (ids, counts, booleans and static reason
codes — never a SQL message, a row, an evidence fragment or model text):

| Event | Meaning |
| --- | --- |
| `catalog_variant_promoted` | one candidate's verified, supported fields were written to a canonical variant under this run's lease. `replayed=true` means an earlier attempt had already written it and this one changed nothing — the promotion is idempotent |
| `catalog_promotion_refused` | one candidate was NOT promoted, with a static reason code. This is an OPERATIONAL CATALOG OUTCOME, not a failed run |

**A refusal is not a failure.** A field with no verified evidence, an
unresolved conflict and a candidate the ingestion left `ambiguous` are all
legitimate outcomes of a research run — that is what the durable catalog is
for. The run's own result is untouched, and a run whose every candidate was
refused can still be a completely successful run.

**An infrastructure failure is not a refusal, and never becomes one.** A lost
lease or a failed pending-promotion read raises out of the worker: NO catalog
event is emitted, the run is not marked complete, and the attempt stays
retryable. If you see catalog refusals you are looking at decisions; if you see
a worker `AppError` and a non-terminal run you are looking at an outage. Do not
read one as the other.

### Raw developer telemetry vs. typed operator status

Two different things, deliberately:

- **raw telemetry** — every event, recognised or not, is appended to the Run
  Inspector's event stream with its type and message. That has existed since
  Catalog PR3 and is unchanged. It is for a developer reading a specific run;
- **typed operator status** — the bounded "Catalog status" panel in the Run
  Inspector (`frontend/lib/catalogStatus.ts`): promoted/refused/replayed counts,
  the latest refusal as static allowlisted text, and a bounded recent-action
  list. It renders only for a project whose trusted `workflow_key` is
  `swarm_v2`, and only once a catalog event has been observed.

Neither is an alert. **Binding these signals to a real monitoring system
remains operator work** — this repository creates no dashboard, no alert policy
and no notification channel, and nothing here has been verified against a
deployed environment.

### When to investigate

- a refusal rate that stays high across runs, rather than tracking how sparse
  the catalog currently is;
- the SAME reason code repeating across unrelated vehicles or runs — that
  points at a systematic evidence, conflict or snapshot defect rather than a
  per-vehicle gap;
- `CATALOG_PROMOTION_SNAPSHOT_UNUSABLE` at any sustained rate — the snapshot
  behind the candidates cannot support canonical facts at all;
- any refusal at all while `MILO_ENABLE_CATALOG_EXECUTION` is supposed to be
  OFF. That is a posture defect: with the flag off no catalog event can be
  emitted, so one existing means the deployed worker revision does not carry
  the configuration you believe it does.

## Incident response skeleton

Detect (signal above) → freeze (kill-switch order) → snapshot evidence
(revision digests, ledger rows, logs — no secret values) → diagnose →
forward-fix or roll back per [ROLLBACK.md](ROLLBACK.md) → verify with
smoke tests → write up with the Stage C acceptance-record fields.

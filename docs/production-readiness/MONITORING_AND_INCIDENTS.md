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
| Migration drift | `check-migration-state.sh` in scheduled audit | HIGH | unexpected remote objects/missing markers — investigate before any deploy |
| RLS denials from service paths | DB logs | HIGH | misconfigured policy or credential misuse |
| Unexpected public access (Cloud Run IAM change) | audit logs on `allUsers` bindings | CRITICAL | remove binding immediately; incident review |
| Secret-access denials | Secret Manager audit logs | HIGH | binding drift or intrusion attempt |

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
2. `MILO_ENABLE_RUN_CREATION` + `GATEWAY_ALLOW_EXECUTION_ROUTES` off — no
   new work;
3. `JOB_LAUNCHER=disabled` — no worker launches;
4. remove worker provider-secret binding — no provider access at all;
5. Cloud Run traffic to a known-good revision — full code rollback.

## Incident response skeleton

Detect (signal above) → freeze (kill-switch order) → snapshot evidence
(revision digests, ledger rows, logs — no secret values) → diagnose →
forward-fix or roll back per [ROLLBACK.md](ROLLBACK.md) → verify with
smoke tests → write up with the Stage C acceptance-record fields.

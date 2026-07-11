# Production readiness — persistent status

- **Branch:** `claude/production-readiness-j0hhni`
- **PR:** #26 (do not create a new PR; do not merge)
- **Head SHA:** 738db1c (audit baseline — updated at end of corrective run)

## Phase status

| Phase | Area | Status |
| --- | --- | --- |
| 1 | Proposal ownership + browser-route authorization | PARTIAL / REQUIRES AUDIT |
| 2 | Worker service-to-service authentication | PARTIAL / REQUIRES AUDIT |
| 3 | Idempotent run creation + lifecycle | PARTIAL / REQUIRES AUDIT |
| 4 | Budget, cost and resource limits | PARTIAL / REQUIRES AUDIT |
| 5 | Shared-store rate limiting | PARTIAL / REQUIRES AUDIT |
| 6 | Frontend execution UI + polling | PARTIAL / REQUIRES AUDIT |
| 7 | Playwright E2E suite | PARTIAL / REQUIRES AUDIT |
| 8 | Production configuration validation | PARTIAL / REQUIRES AUDIT |
| 9 | Migration and operator tooling | NOT STARTED |
| 10 | Deployment and rollback preparation | NOT STARTED |
| 11 | Final documentation | NOT STARTED |

## Audit findings (corrective run in progress)

1. **No gateway identity verification** — the backend trusts
   `x-milo-auth-user-id` from any caller able to invoke Cloud Run; there is
   no `MILO_GATEWAY_AUDIENCE` / `MILO_APPROVED_GATEWAY_IDENTITIES` boundary.
2. **Provider key read from run input** — `vehicle_catalog_v1/adapter.py`
   reads `api_key` from `run.input`, i.e. from data that originated in
   requests, instead of worker-only environment/Secret Manager variables.
3. **Non-atomic run creation** — user message and run are separate writes;
   idempotency uses find-then-insert; concurrent duplicates race.
4. **Non-atomic launch ownership** — read-then-update launch state; two
   concurrent requests can both call `JobLauncher.launch()`; no
   `launch_unknown` reconciliation state for uncertain responses.
5. **Non-atomic worker lease** — `claim_run` is read-then-update; stale
   workers can overwrite newer state; no CAS on terminal writes.
6. **Budget checks are post-hoc** — token/cost limits are only compared to
   already-recorded usage; no pre-call reservation, no `max_tokens`
   clamping, no concurrency safety, no persistent decision ledger, daily
   budgets unwired in the worker.
7. **Proposal ownership columns unprotected** — RLS update policy lets an
   authenticated member rewrite `created_by`/`project_id`; project creation
   from proposal + owner membership is two non-atomic writes.
8. **Project rate limit keyed by conversation id**, not project id.
9. **Client IP taken from browser-controlled `x-forwarded-for`.**
10. **Frontend reducer state leaks across run switches**; no stale-response
    guard.
11. **PostgreSQL tests can skip silently**; no dedicated CI job that fails
    on skips.
12. **`scripts/release/` pre-exempted** from the unsafe-default scanner.
13. **Config validation gaps** — no rejection of missing gateway auth,
    shared gateway/worker identities, or test adapters in production.

## Corrections applied in this run

(recorded as commits land — see PR #26)

## Test results

(recorded at end of run)

## Unresolved risks

(recorded at end of run)

## Next task

**Phase 9 — Migration and operator tooling** (next run; not started here).

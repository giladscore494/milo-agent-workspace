# Production readiness — persistent status

- **Branch:** `claude/production-readiness-j0hhni`
- **PR:** #26 (single PR; not merged)
- **Head SHA:** tip of this branch (the `audit:` commit); updated every run

## Phase status

| Phase | Area | Status |
| --- | --- | --- |
| 1 | Proposal ownership + browser-route authorization | CORRECTED (audit fixes applied) |
| 2 | Worker service-to-service authentication | CORRECTED (gateway/worker separation added) |
| 3 | Idempotent run creation + lifecycle | CORRECTED (transaction-safe + atomic launch/lease) |
| 4 | Budget, cost and resource limits | CORRECTED (hard pre-call reservation + ledger) |
| 5 | Shared-store rate limiting | CORRECTED (project keys + trusted IP) |
| 6 | Frontend execution UI + polling | CORRECTED (run-state isolation) |
| 7 | Playwright E2E suite | CORRECTED (identity + isolation coverage; 31 tests) |
| 8 | Production configuration validation | CORRECTED (gateway auth, identity separation, test-adapter bans) |
| 9 | Migration and operator tooling | NOT STARTED |
| 10 | Deployment and rollback preparation | NOT STARTED |
| 11 | Final documentation | NOT STARTED |

## Corrections applied in the Phases 1–8 corrective audit

1. **Trusted gateway identity** (`fix: authenticate trusted browser gateway identity`)
   — `backend/gateway_auth.py` verifies a Google-signed `X-Milo-Gateway-Token`
   (signature/issuer/audience/expiry/verified email/allowlist;
   `MILO_GATEWAY_AUDIENCE` + `MILO_APPROVED_GATEWAY_IDENTITIES`, separate
   from worker auth) before any `x-milo-auth-*` header is trusted.
   Production fails closed when unconfigured. Worker identities cannot
   impersonate browser users; gateway identities cannot call worker routes.
2. **Protected proposal ownership + atomic project creation**
   — column-scoped UPDATE grants exclude `created_by`/`project_id`;
   `create_project_from_proposal_with_owner()` commits project + owner
   membership in one transaction (no orphan projects). Migration 011.
3. **Atomic run creation/admission** — `create_message_and_run()` (migration
   012) creates the user message + run with idempotent replay and
   per-user/per-project admission under advisory locks in one transaction;
   proven with real concurrent PostgreSQL sessions.
4. **Atomic launch ownership** — compare-and-set to `launching`; exactly one
   launcher call per run; uncertain launch responses park the run as
   `launch_unknown` and are never auto-relaunched; definite failures stay
   retryable.
5. **Atomic worker lease** — `claim_run_lease()` single-statement CAS; one
   active holder, expiry reclaim with attempt increment, heartbeat
   ownership, stale-worker writes blocked by `expected_worker_id` + status
   CAS; cancellation stays visible through claims.
6. **Worker-only provider credentials** — API key read only from
   `KIMI_API_KEY`/`MOONSHOT_API_KEY` worker env; run-input keys provably
   ignored; paid execution fails closed without a key.
7. **Hard pre-call budgets** — thread-safe `reserve_call()` checks kill
   switch, lease, cancellation, time, calls, agent steps, retries,
   estimated input tokens, remaining input/output/total tokens (incl.
   in-flight reservations), estimated/actual cost and daily budgets BEFORE
   the call; clamps `max_tokens`; concurrent calls cannot double-spend.
8. **Usage ledger** — append-only `run_usage_ledger` (migration 013) with
   decimal-safe costs, per-decision rows, DB trigger forbidding
   update/delete for every role; daily user/project budget queries prefer
   settled actuals.
9. **Project rate-limit key** — run creation limits by real project id.
10. **Trusted client IP** — platform-set headers preferred; strict IP
    validation; malformed values collapse to one bucket.
11. **Frontend run isolation** — reducer reset + generation guard on run
    switch/sign-out; late responses from a previous run are dropped; all
    six terminal states stop polling.
12. **E2E identity coverage** — 31 tests across two stacks, both running
    with real gateway verification (no bare header trust anywhere in E2E).
13. **Guaranteed PostgreSQL CI** — dedicated `postgres-checks` job with
    `MILO_REQUIRE_PG_TESTS=1` (missing binaries fail, any skip fails).
14. **Config/static safety** — production rejects missing gateway auth,
    shared gateway/worker identities and test adapters; `scripts/release/`
    is no longer exempt from unsafe-default scanning.

## Migrations

- `008` proposal ownership (+ column-scoped update grant correction)
- `009` run idempotency + lifecycle columns
- `010` run usage aggregate
- `011` proposal ownership protection + atomic project creation (NEW)
- `012` atomic run operations: create_message_and_run, launch_unknown, claim_run_lease (NEW)
- `013` append-only usage ledger (NEW)

All additive/idempotent/data-preserving; none applied to production.

## Exact test results (this corrective run, local)

- `pytest -q tests --ignore=MILO-main-original/MILO-main/test_websearch.py`
  with `MILO_REQUIRE_PG_TESTS=1`: **382 passed, 0 failed, 0 skipped**
  (includes the ephemeral-PostgreSQL suite: 82 executable migration,
  RLS and concurrency tests).
- `python scripts/check_migrations.py` — passed.
- `python scripts/secret_scan.py` — passed.
- `python scripts/check_unsafe_defaults.py` — passed.
- Frontend: `tsc --noEmit` clean; `vitest` **60 passed**; `next build`
  succeeded; `test:static` and `test:secrets` passed.
- Playwright E2E: **31 passed** (13 disabled-stack, 18 enabled-stack),
  0 failed, 0 skipped.
- Docker builds: **not runnable in this sandbox** (no Docker daemon);
  built by the CI `offline-checks` job on the pushed head.

## Unresolved risks

- `launch_unknown` reconciliation is manual by design (operator tooling
  arrives in Phase 9); such runs are never auto-relaunched.
- The `authenticated` DB role retains legacy-era table grants from
  migration 007 (reads scoped by RLS); the backend itself uses the service
  role. Revisit during Phase 9/10 review.
- Daily-budget aggregation reads up to 2000 ledger rows per check; fine at
  current scale, revisit before broad access.
- Docker builds and the new CI jobs are validated by CI, not locally.

## Execution-safety confirmation

All execution flags remain **default-off**. No deployment, IAM mutation,
production migration apply, secret read/write, real Cloud Run worker
execution or paid model API call occurred in this run.

## Next task

**Phase 9 — Migration and operator tooling** (next run). Phases 9–11 are
not complete and not started.

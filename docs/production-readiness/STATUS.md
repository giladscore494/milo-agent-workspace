# Production readiness — persistent status

- **Current child branch:** `codex/child-fix-phases-1-8` (local sandbox branch; remote push unavailable here because no `origin` remote is configured).
- **Child PR:** not created from this sandbox; required base is `codex/fix-remaining-blockers-in-phases-18-g2320x` (PR #28 head branch).
- **Base branch:** `codex/fix-remaining-blockers-in-phases-18-g2320x`.
- **Actual local head SHA:** see `git rev-parse HEAD` for this checkout.
- **Remote head SHA / CI:** not verified in this sandbox because the checkout has no configured GitHub remote.

## Phase status

| Phase | Area | Status |
| --- | --- | --- |
| 1 | Proposal ownership + browser-route authorization | CORRECTIVE WORK IN PROGRESS; not marked green until child PR CI passes |
| 2 | Worker service-to-service authentication | CORRECTIVE WORK IN PROGRESS; active lease token hardening added locally |
| 3 | Idempotent run creation + lifecycle | CORRECTIVE WORK IN PROGRESS; cancellation fixture repaired locally |
| 4 | Budget, cost and resource limits | CORRECTIVE WORK IN PROGRESS; canonical reservation settlement path added locally |
| 5 | Shared-store rate limiting | CORRECTIVE WORK IN PROGRESS; existing security posture preserved |
| 6 | Frontend execution UI + polling | CORRECTIVE WORK IN PROGRESS; existing launch-state exposure preserved |
| 7 | Playwright E2E suite | NOT VERIFIED LOCALLY in this sandbox |
| 8 | Production configuration validation | CORRECTIVE WORK IN PROGRESS; existing validation preserved |
| 9 | Migration and operator tooling | NOT STARTED |
| 10 | Deployment and rollback preparation | NOT STARTED |
| 11 | Final documentation | NOT STARTED |

## Current corrective scope

This child corrective change set targets the remaining Phases 1–8 blockers on top of PR #28 only. It does not start Phases 9–11 and does not retarget `main`, the Claude branch, PR #26, or PR #27.

Implemented locally in this checkout:

1. Migration `015` now uses portable guarded grants: `PUBLIC` is always revoked, `authenticated` is revoked only when present, and `service_role` is granted only when present.
2. The PostgreSQL Supabase auth shim creates realistic `anon`, `authenticated`, and `service_role` roles idempotently.
3. Migration `014` legacy daily-budget RPCs are documented as deprecated and have execute privileges revoked so production has one canonical model-call reservation lifecycle.
4. Worker lease claims now include an execution nonce (`lease_token`) in the run row, and repository mutation paths accept worker id, attempt, and lease-token guards.
5. Browser-safe run responses remove `lease_token` while continuing to expose `launch_state`, `launch_error_class`, and `launch_reconciliation_required`.
6. The guarded budget client stores the canonical reservation id in a private in-memory `ModelCallReservation`, settles successful calls, releases failed provider calls, rejects missing reservation ids, and fails closed on settlement failure.
7. The cancellation-before-start test fixture now builds a valid project → conversation → run graph instead of referencing a nonexistent conversation.

## Migrations

- `008` proposal ownership.
- `009` run idempotency + lifecycle columns.
- `010` run usage aggregate.
- `011` proposal ownership protection + atomic project creation.
- `012` atomic run operations: `create_message_and_run`, launch CAS, `launch_unknown`, `claim_run_lease`, and `lease_token` ownership nonce.
- `013` append-only usage ledger.
- `014` deprecated legacy daily budget RPCs with execution revoked.
- `015` canonical model-call budget reservation/settlement lifecycle with portable least-privilege grants.

All migrations remain intended to be additive, idempotent, and data preserving; production application is not performed by this status document.

## Exact local validation from this sandbox

- `pytest -q tests/test_migrations_postgres.py` — **passed with skips**: 1 passed, 70 skipped. PostgreSQL skip count: 70 (PostgreSQL server binaries unavailable without `MILO_REQUIRE_PG_TESTS`).
- `MILO_REQUIRE_PG_TESTS=1 pytest -q tests/test_migrations_postgres.py` — **failed due to environment**: PostgreSQL server binaries unavailable; 70 setup errors, 1 passed, 0 skips. This is an environment limitation, not a green PostgreSQL result.
- `pytest -q tests/test_corrective_blockers.py tests/test_budget.py tests/test_worker.py tests/test_run_lifecycle.py tests/test_api.py tests/test_authorization.py tests/test_gateway_auth.py tests/test_workflow_proposals.py` — **failed during collection due to environment**: `ModuleNotFoundError: No module named 'fastapi'`; 8 collection errors.
- `python -m pip install -r backend/requirements.txt -q` — **failed due to environment/network**: package index tunnel returned `403 Forbidden` for FastAPI.
- `python scripts/check_migrations.py` — **passed**: `migration check passed (static text validation only)`.
- `python -m py_compile backend/*.py backend/repository/*.py backend/testing/*.py backend/worker/*.py` — **passed**.

## CI and Docker

- GitHub Actions: not verified from this sandbox; no GitHub remote is configured in the checkout.
- Docker builds: not run in this sandbox.
- Required child PR checks must still be verified on the pushed child branch before Phases 1–8 can be called fully corrected.

## Remaining risks

- Required PostgreSQL tests have not executed locally with zero skips because PostgreSQL server binaries are unavailable here.
- Backend and E2E tests have not executed locally because Python dependencies are unavailable and package installation is blocked by the environment.
- CI status, remote head SHA, child PR number, and final remote commit list must be filled in after the child branch is pushed and GitHub checks complete.
- Launch reconciliation for `launch_unknown` remains an operator process until later-phase tooling.

## Execution-safety confirmation

All execution flags remain default-off. No production deployment, IAM mutation, production migration application, secret read/write, real Cloud Run worker execution, or paid model API call occurred in this local corrective pass.

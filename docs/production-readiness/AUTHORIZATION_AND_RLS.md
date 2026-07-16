# Authorization, ownership and RLS

## Project membership — `COMPLETED_IN_CODE`

`public.project_members` (migration `007`) is the authorization spine:
`(project_id, user_id, role)` with roles `owner | admin | member | viewer`.
Every browser-facing read/write path resolves the verified user (gateway
token first — see [AUTHENTICATION.md](AUTHENTICATION.md)) and requires
membership of the target project (`backend/auth.py`,
`tests/test_authorization.py`). Cross-user access returns 403/404, never
data.

Projects are created atomically with their owner row
(`create_project_from_proposal_with_owner`, migration `011`), so no project
can exist without an owner going forward. Legacy projects without owners
are backfilled manually (below).

## Proposal ownership and lifecycle — `COMPLETED_IN_CODE`

`public.workflow_proposals` carries `created_by` and `project_id`
(migration `008`); RLS requires non-NULL ownership plus membership for
every browser operation, and ownership is protected against tampering
(migration `011`). Proposal mutations additionally sit behind
`MILO_ENABLE_PROPOSAL_MUTATIONS` / `MILO_ENABLE_PROPOSAL_READS`
(default off). Proposal creation requires an `idempotency_key`
(`backend/schemas.py`).

## RLS — `COMPLETED_IN_CODE`

All browser-reachable tables enable row level security; policies are
membership-based. The service-role connection used by the API/worker
bypasses RLS by design, which is why it is server-only and why application
authorization (membership checks) runs on every browser-facing route
regardless. Executable RLS validation runs against real PostgreSQL in CI
(`tests/test_migrations_postgres.py`, zero skips enforced).

## Ownership backfills — `REQUIRES_MANUAL_OPERATOR_CONFIGURATION`

Legacy rows created before migrations `007`/`008` may lack ownership.
Generate (never auto-apply) the corrective SQL with:

- `scripts/release/generate-membership-backfill.sh` — real project/user
  UUIDs from an operator-supplied mapping; rejects placeholders, duplicate
  owners, ownerless projects.
- `scripts/release/generate-proposal-backfill.sh` — proposal→owner/project
  mapping; rejects orphans and conflicts; updates only NULL-ownership rows.

Apply manually per [MIGRATIONS.md](MIGRATIONS.md), then validate with the
queries embedded in the generated SQL.

## Worker-route authorization — `COMPLETED_IN_CODE`

Internal routes (`/internal/runs/...`) accept only verified worker
identities plus the active lease token; browser identities and the gateway
identity are rejected (`backend/worker_auth.py`,
`tests/test_worker_auth.py`). The gateway additionally refuses to proxy
`/internal/*` at all (route allowlist).

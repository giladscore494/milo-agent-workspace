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

Every `public` table enables row level security explicitly in the
migrations. Browser-reachable tables carry membership-based policies; all
other tables are service-path only and carry RLS with zero policies
(deny-all for browser roles). Migration
`20260810000200_enable_rls_on_service_only_tables.sql` added the explicit
enablement for the service-only tables from migrations 002/004/005/015 —
previously that invariant silently depended on an environment-specific
`ensure_rls` event trigger (a platform guardrail some Supabase projects
install, not part of this repository). The service-role connection used by
the API/worker bypasses RLS by design, which is why it is server-only and
why application authorization (membership checks) runs on every
browser-facing route regardless. Executable RLS validation runs against
real PostgreSQL in CI (`tests/test_migrations_postgres.py`, zero skips
enforced), including a guard that every public table has RLS with no
external trigger present.

## Service-only RPC ACLs — `COMPLETED_IN_CODE`

Supabase grants EXECUTE on public-schema functions to `anon`,
`authenticated` and `service_role` through default privileges. The service
RPC migrations (011/012/014/015) revoked `public` and `authenticated` but
not `anon`, so every service RPC — including the SECURITY DEFINER budget
functions — remained anonymously callable via PostgREST on a real Supabase
project. Migration `20260810000100_revoke_anon_execute_on_service_rpcs.sql`
revokes `anon` (and re-asserts `authenticated`/`public`) on all eight
service RPCs and removes the schema-level default EXECUTE grants for
`anon`/`authenticated`, so the existing per-function
`revoke ... from public` convention is now sufficient for future
functions. Regression coverage: `tests/test_migrations_postgres.py`
replicates Supabase's default function privileges in its shim and asserts
no non-trigger public function is executable by `anon`.

**Production follow-up (required before Stage C):** both hardening
migrations are applied to staging only. Production must receive them via
the manual migration procedure, and the anon-EXECUTE gap should be assumed
present in production until then.

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

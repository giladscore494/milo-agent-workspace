# Auth/UI hardening — production readiness

Status of the Auth/UI hardening stage (PR #24). This stage ships an
authenticated, read-mostly workspace. **No execution surface may be enabled
before a separate, explicitly approved execution stage.**

## Architecture flow

```
Browser (Next.js on Vercel)
  │  Supabase email/password session (official @supabase/supabase-js,
  │  persisted + auto-refreshed by the library; no custom session storage)
  ▼
Next.js gateway  /api/gateway/[...path]   (server-side only)
  1. Reject anything outside the allowlist (see below); both run-creation
     POST routes are explicitly blocked with 403.
  2. Per-instance in-memory rate limit (429 + Retry-After).
  3. Validate the Supabase access token against GET {SUPABASE_URL}/auth/v1/user
     BEFORE any Cloud Run credential is requested; auth failure never
     contacts Cloud Run.
  4. Strip/ignore all browser-supplied x-milo-* headers; regenerate
     x-milo-auth-user-id / x-milo-auth-user-email from the validated user only.
  5. Fetch a Google Cloud Run ID token; upstream Authorization carries only
     that ID token.
  ▼
FastAPI backend (private Cloud Run service)
  1. ExecutionSurfaceGuardMiddleware rejects disabled execution surfaces
     (403 EXECUTION_SURFACE_DISABLED) before routing and body validation.
  2. Browser-facing routes require the internal identity headers and verify
     project membership through the repository (project_members).
  ▼
Supabase Postgres (service-role key; RLS is defense in depth for the
authenticated role once migration 007 is applied)
```

Gateway allowlist: `GET /health` (public), `GET /projects`,
`GET /projects/{uuid}`, `POST /projects/{uuid}/conversations`,
`GET /conversations/{uuid}`. Everything else is blocked at the gateway.

## Backend execution kill switches

All three flags are **disabled by default** and are not set in repository
defaults, Dockerfiles, CI, Vercel or Cloud Run configuration:

| Flag | Gates |
| --- | --- |
| `MILO_ENABLE_RUN_CREATION` | `POST /conversations/{id}/runs`, `POST /workflow-proposals/{id}/runs` |
| `MILO_ENABLE_PROPOSAL_MUTATIONS` | `POST /workflow-proposals` and `/approve`, `/reject`, `/revise`, `/project` |
| `MILO_ENABLE_EXECUTION_CONTROL` | `POST /runs/{id}/cancel`, `/tool-access-requests`, `/tool-grants`, `/tool-usage`, `/sources`, `/claims`, `/conflicts` |

A fourth default-off flag, `MILO_ENABLE_PROPOSAL_READS`, gates
`GET /workflow-proposals/{id}`: the `workflow_proposals` table has no
`created_by` user or project foreign key, so the route cannot be
membership-scoped yet and stays disabled instead of globally readable.
`GET /runs/{id}` and `GET /runs/{id}/events` require authenticated identity
plus project membership (runs → conversations → projects → project_members).

The guard runs as ASGI middleware before request-body validation, so a
disabled surface returns 403 even for empty or invalid bodies and performs no
repository mutation and no `JobLauncher.launch()`.

**Worker service-to-service authentication:** the internal worker mutation
routes (`tool-access-requests`, `tool-grants`, `tool-usage`, `sources`,
`claims`, `conflicts`, `cancel`) must stay disabled until a separate
service-to-service authorization model exists (e.g. verified Cloud Run
service identity via ID-token audience checks). Enabling
`MILO_ENABLE_EXECUTION_CONTROL` alone is NOT sufficient: with only the flag,
any authenticated caller of the private API could write execution records.

## Supabase Auth settings still required manually

- Enable the Email provider; decide whether sign-ups are open or invite-only
  (recommended: disable public sign-ups and create users from the dashboard).
- Configure Site URL and redirect URLs for the Vercel production and preview
  domains.
- Confirm email confirmations / password policy per your security bar.
- Create the real end users in Supabase Auth (no fabricated IDs anywhere).

## Required Vercel environment variables (public values only)

- `NEXT_PUBLIC_SUPABASE_URL` — the project URL.
- `NEXT_PUBLIC_SUPABASE_ANON_KEY` — the anon/publishable key (never the
  service-role/secret key).
- Cloud Run gateway settings already in use by the deployment
  (`CLOUD_RUN_SERVICE_URL` and the OIDC/service-account configuration used by
  `lib/server/cloudRunAuth.ts`).
- Optional: `GATEWAY_RATE_LIMIT_REQUESTS`, `GATEWAY_RATE_LIMIT_WINDOW_MS`
  (finite positive numbers; invalid values fall back to 60 requests / 60 s).

No secret values belong in this document or in any `NEXT_PUBLIC_*` variable.

## Migration + membership backfill order

1. Review `supabase/migrations/007_project_members.sql` (unapplied,
   non-destructive, idempotent). Apply it through the controlled migration
   workflow — never from a developer machine.
2. Existing projects intentionally have **no members** after apply; they are
   invisible to browser users and untouched for the service path.
3. Backfill memberships with real `auth.users` IDs only (template in the
   migration header). Do not fabricate user UUIDs and do not auto-assign
   ownership.
4. Only then can each real user see their projects in the workspace.

## Browser E2E checklist (after deploy)

1. Unauthenticated visit shows only the login screen and explanatory text.
2. Sign in with a real Supabase user; session survives a page refresh and
   token refresh (leave the tab open past expiry).
3. Projects list loads through `/api/gateway/projects`; loading, error and
   empty states render; a user with no memberships sees the empty state.
4. Selecting an authorized project and creating a conversation succeeds;
   the conversation id/title render safely.
5. No proposal, run, retry, resume, cancel, worker, Kimi or tool-grant
   control exists anywhere in the UI.
6. Direct gateway probes: `POST /api/gateway/conversations/{id}/runs` → 403;
   any non-allowlisted path → 403; spoofed `x-milo-auth-user-id` header is
   ignored; requests without a valid token → 401 and Cloud Run is not hit.
7. Sign out returns to the login screen and invalidates the session.

## Known limitations / deferred items

- **Rate limiter** is per warm serverless instance and in-memory (bounded
  map, stale-bucket expiry, Retry-After). It is a fail-safe, not a global
  quota. Upstash/Redis or another shared store is required before broad
  public access.
- **Realtime/polling for runs is disabled**; the run console is a static
  read-only shell until an authenticated Realtime channel is designed.
- **Worker mutation routes** need service-to-service auth (above).
- **Budget/cost caps:** before enabling any Kimi/Moonshot execution, hard
  budget limits, per-run token caps and a kill-path must exist. Paid API
  calls remain forbidden without explicit authorization.
- **`workflow_proposals` ownership schema** (created_by/project link) is
  missing; proposal reads and mutations stay disabled until it exists.
- **No execution flag may be enabled before a separate approved stage.**

## Rollback of the Auth/UI deployment

1. Vercel: promote the previous production deployment (Instant Rollback).
   The gateway and UI are stateless; no data migration is involved.
2. Cloud Run: the backend in this stage only adds guards; rolling back to
   the previous revision restores prior behavior. No flags need unsetting
   because none were set.
3. Database: migration 007 is additive. Prefer leaving it in place (RLS only
   affects the `authenticated` role, which the backend does not use). If it
   must be reverted, drop the policies and `public.project_members`
   explicitly in a reviewed follow-up migration — never with an automated
   down-migration against production.
4. Supabase Auth users/memberships can remain; they grant no access once the
   frontend rollback removes the authenticated UI.

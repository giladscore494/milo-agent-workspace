# Smoke testing

Two operator smoke suites live in `scripts/release/`; both default to
safe behavior, support `--json-output`, and never silently skip a check
(missing inputs degrade to explicit MANUAL findings).

## Read-only smoke test — `COMPLETED_IN_CODE`

`scripts/release/smoke-test-read-only.sh --base-url <gateway-url> …`

Verifies, with operator-supplied identities (token env-var NAMES only,
values never printed): health endpoint; authenticated identity flow;
project listing; project-membership authorization; conversation read; run
read; event polling; proposal read; cross-user access rejection; and
worker-route rejection from a browser identity (the gateway refuses to
proxy `/internal/*`).

It never creates a run, never triggers a worker, never calls a provider,
never mutates a project and never applies migrations. The only POST it
sends targets a worker route with a browser identity, whose rejection is
the assertion.

## Execution-disabled smoke test — `COMPLETED_IN_CODE`

`scripts/release/smoke-test-execution-disabled.sh --base-url <url>
--env-file <metadata> --user-token-env <NAME> --conversation-id <uuid> …`

Proves the production-like deployment stays safe while execution is
disabled: paid-execution flag off (metadata); **authenticated** run
creation blocked (HTTP 403 carrying the execution-disabled application
classification — `EXECUTION_SURFACE_DISABLED` / the gateway safety-policy
message); read-only surface functional; no secret material in responses;
cancellation behavior stable per the staged state; no new model-call budget
reservation (optional read-only DB assertion); plus the exact manual
command proving no Cloud Run worker job execution occurred.

Because the gateway refuses run creation with HTTP 403 **before** it
validates the Supabase token, a non-empty but bogus token would also receive
that 403. The script therefore first performs an **authenticated ownership
read** — `GET /api/gateway/conversations/<conversation-id>` — and requires
HTTP `200` (proving the token is valid AND owns the conversation) before it
even sends the run-creation request:

- read `200` → authentication + ownership proven; continue to the
  run-creation probe;
- read `401` → `BLOCKED` (invalid/expired token); run creation **not**
  attempted;
- read `403`/`404` → `BLOCKED` (conversation not owned/accessible); run
  creation **not** attempted;
- any other read status → `BLOCKED` (prerequisite not proven);
- missing token or conversation id → `MANUAL` (never `PASS`).

Only after the read proves auth+ownership does the run-creation posture get
evaluated. It is reported `PASS` **only** when the subsequent schema-valid
`RunCreate` returns HTTP `403` carrying the execution-disabled classification
(`EXECUTION_SURFACE_DISABLED` / the gateway safety-policy message). A generic
`403` is not sufficient; an authenticated `2xx` is a critical `BLOCKED`
(execution is not actually disabled). The probe still creates no run,
triggers no worker, calls no provider and reserves no budget.

The `no-secret-returned` health check is likewise fail-closed: it requires a
successful `curl`, HTTP `200`, and a non-empty body before scanning for
secret-looking material. A transport failure, a non-200 status, or an empty
body is `BLOCKED`, never a false `PASS`.

## Exact smoke-test order (Stage A)

1. `smoke-test-execution-disabled.sh --env-file <metadata>` (flag posture);
2. `smoke-test-read-only.sh --base-url <PRODUCTION_VERCEL_URL> …`;
3. `smoke-test-execution-disabled.sh --base-url <PRODUCTION_VERCEL_URL>
   --env-file <metadata> [--database-url-env MILO_READONLY_DB_URL]`.

## CI usage

CI exercises both scripts against a local mock HTTP endpoint (mocked
`curl` in `tests/test_release_tooling.py` and the strict mocks in
`tests/test_release_tooling_cli.py`, which distinguish an unauthenticated
401 from an authenticated execution-disabled 403 and reject any authenticated
2xx) — real production mode always requires explicit operator-supplied URLs
and identities. The isolated
Playwright E2E suite (`frontend/e2e`, mocked auth/worker/provider with
gateway verification active and paid execution disabled) covers the
browser-level equivalents: authenticated read flow, unauthorized
rejection, proposal flow, idempotent run creation, cancellation, event
polling, stale-UI prevention, worker-route isolation, launch-state UI and
execution-disabled behavior.

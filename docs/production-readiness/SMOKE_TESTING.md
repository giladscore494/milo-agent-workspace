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
--env-file <metadata> …`

Proves the production-like deployment stays safe while execution is
disabled: paid-execution flag off (metadata); run creation blocked by the
staged gateway policy (403 before any side effect); read-only surface
functional; no secret material in responses; cancellation behavior stable
per the staged state; no new model-call budget reservation (optional
read-only DB assertion); plus the exact manual command proving no Cloud
Run worker job execution occurred.

## Exact smoke-test order (Stage A)

1. `smoke-test-execution-disabled.sh --env-file <metadata>` (flag posture);
2. `smoke-test-read-only.sh --base-url <PRODUCTION_VERCEL_URL> …`;
3. `smoke-test-execution-disabled.sh --base-url <PRODUCTION_VERCEL_URL>
   --env-file <metadata> [--database-url-env MILO_READONLY_DB_URL]`.

## CI usage

CI exercises both scripts against a local mock HTTP endpoint (mocked
`curl` in `tests/test_release_tooling.py`) — real production mode always
requires explicit operator-supplied URLs and identities. The isolated
Playwright E2E suite (`frontend/e2e`, mocked auth/worker/provider with
gateway verification active and paid execution disabled) covers the
browser-level equivalents: authenticated read flow, unauthorized
rejection, proposal flow, idempotent run creation, cancellation, event
polling, stale-UI prevention, worker-route isolation, launch-state UI and
execution-disabled behavior.

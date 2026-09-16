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
disabled: paid-execution flag off, run-creation flag off, gateway execution
routes off, and **catalog execution off** (`MILO_ENABLE_CATALOG_EXECUTION`) —
all read from the deployment metadata; **authenticated** run
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

## Catalog posture in the Stage A smoke

`MILO_ENABLE_CATALOG_EXECUTION` is checked with the other execution flags and
must be off. Off means the catalog capability is ABSENT from trusted Swarm V2
wiring — no Government tool registered, no `catalog:government:read` scope
granted, no promotion pipeline constructed — rather than present and idle, so a
deployment whose database already holds a usable Government snapshot stays inert
for chat runs. A run in this posture can emit neither `catalog_variant_promoted`
nor `catalog_promotion_refused`; seeing either one while the flag is supposed to
be off means the deployed worker revision does not carry the configuration the
metadata describes, and is `BLOCKED`, not a curiosity.

Enabling it is never part of Stage A, B, C or D — see STAGED_ACTIVATION.md
§"Catalog execution — a separate stage, separately authorized".

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
polling, stale-UI prevention, worker-route isolation, launch-state UI,
execution-disabled behavior, and the Swarm V2 **final-result** surface —
a terminal run whose product payload is built by the shipped
`FinalBuilder`/`finalize_product_outcome` and mapped by the shipped
`durable_run_status`, asserted for the usable / partial / empty outcomes,
refresh reconstruction, mobile width, keyboard operation and the
`vehicle_catalog_v1` control (see `docs/swarm-v2-final-result.md`).

Stage F5 adds to that suite: every one of the six durable terminal states
asserted as a literal status; the `launch_unknown` path driven through the
production `JobLaunchUncertain` condition at a test-only launcher seam (parked
run, safe error, idempotent replay returning the same run, exactly one launch
attempt, no automatic relaunch); sign-out clearing the browser's stored
active-run keys; and a secret scan over **every** script the running server
serves, including a check that only the approved `NEXT_PUBLIC_*` variables
appear. The full acceptance matrix, with the classification and evidence for
each requirement, is [FRONTEND_ACCEPTANCE.md](FRONTEND_ACCEPTANCE.md).

## Operator UI verification before a release

These scripts and the E2E suite prove behavior against an isolated stack. They
cannot see the deployed browser. [FRONTEND_PRE_RELEASE.md](FRONTEND_PRE_RELEASE.md)
is the short operator pass that can: disabled-by-default posture, session
behavior, project/conversation isolation, run creation and idempotency,
refresh/reconnect, cancellation and focus, every terminal state,
`launch_unknown`, the Final Result variants, keyboard and mobile layout, and a
served-page/bundle secret inspection — plus its own rollback and stop
conditions. Run it after the Stage A smoke tests above and record the result
with the release evidence.

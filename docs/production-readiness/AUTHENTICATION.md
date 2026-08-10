# Authentication

## Browser → Supabase — `COMPLETED_IN_CODE`

The browser authenticates against Supabase using only the public values
`NEXT_PUBLIC_SUPABASE_URL` and `NEXT_PUBLIC_SUPABASE_ANON_KEY`
(`frontend/lib/supabaseClient.ts`). Service-role material never enters
browser configuration or the bundle (enforced by
`frontend/scripts/no-secret-bundle-check.mjs`, `npm run test:secrets`, and
`scripts/check_unsafe_defaults.py`). Session tokens are never logged.

## Browser → gateway — `COMPLETED_IN_CODE`

Every `/api/gateway/*` request must carry the user's Supabase access token.
The gateway validates it server-side (`frontend/lib/server/supabaseAuth.ts`)
before proxying; `/health` is the only unauthenticated read. API
authorization never trusts bare browser claims: the backend only honors
`x-milo-auth-*` headers after verifying the gateway token.

## Gateway → private Cloud Run API — `COMPLETED_IN_CODE`

Short-lived credential flow (`frontend/lib/server/cloudRunAuth.ts`):

1. `getVercelOidcToken()` obtains the Vercel-issued OIDC token (no stored
   secret);
2. Google STS exchanges it via the Workload Identity Federation pool
   (`GCP_PROJECT_NUMBER`, `GCP_WORKLOAD_IDENTITY_POOL_ID`,
   `GCP_WORKLOAD_IDENTITY_POOL_PROVIDER_ID`);
3. `iamcredentials.generateIdToken` mints an ID token for
   `GCP_SERVICE_ACCOUNT_EMAIL` with audience = `CLOUD_RUN_API_URL`.

The token is sent as both the `Authorization` bearer (Cloud Run IAM) and
`X-Milo-Gateway-Token` (application verification). No long-lived key file
exists anywhere in this flow. The browser never sees the token: headers
are constructed server-side from scratch.

Verification on the API (`backend/gateway_auth.py`): signature against
Google certificates, issuer, audience == `MILO_GATEWAY_AUDIENCE`, expiry,
verified email ∈ `MILO_APPROVED_GATEWAY_IDENTITIES`. Unconfigured →
**fail closed 503**. `MILO_ALLOW_INSECURE_DEV_IDENTITY` is a test-only
escape and is rejected in production (`backend/production_config.py`).

Revocation: remove the identity from `MILO_APPROVED_GATEWAY_IDENTITIES`
(immediate at the application layer) and/or remove the gateway service
account's `roles/run.invoker` binding and the workload-identity-pool
provider (infrastructure layer).

## Worker mutation path and worker-facing API routes — `COMPLETED_IN_CODE`

The canonical worker mutation path is **worker → Supabase directly** with
the server-only service-role key (`backend/worker/main.py`,
`backend/repository/supabase.py`): the worker claims a lease and then
performs every durable write — status transitions, heartbeats, events,
checkpoints, usage, shadow-supervisor rows and budget
reservations/settlements — under that lease. Each write is validated
against (run id, worker id, attempt, lease token, unexpired lease)
atomically in the database (migrations `012` and `20260810000300`), so a
stale or superseded worker is rejected at the database boundary, not by
an application-side read-then-write check.

Separately, the API's `/internal/*` routes are a worker-facing surface:
callers present a Google OIDC identity token (minted by the metadata
server, no key file) as `X-Milo-Worker-Token` with audience
`MILO_WORKER_AUDIENCE`; the API verifies signature, issuer, audience,
expiry and allowlist membership (`MILO_APPROVED_WORKER_IDENTITIES`,
empty ⇒ fail closed) — `backend/worker_auth.py`. Browser Supabase tokens
can never pass (wrong signer). The shipping worker does not use these
routes for its mutations; they remain OIDC-guarded so that no
identity-less path onto worker semantics exists through the API.

Identity separation is validated at startup: an identity approved for both
gateway and worker roles is a configuration error
(`SHARED_GATEWAY_WORKER_IDENTITY`).

## External identity resources — `REQUIRES_MANUAL_OPERATOR_CONFIGURATION`

Workload identity pool/provider, gateway/API/worker service accounts and
their IAM bindings are created manually per
[MANUAL_SERVICE_CONNECTIONS.md](MANUAL_SERVICE_CONNECTIONS.md)
(Connections 3 and 4) and verified read-only with
`scripts/release/check-gcp-resources.sh`.

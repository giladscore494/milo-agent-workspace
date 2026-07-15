# Shared rate limiting

Status: `COMPLETED_IN_CODE`; the production Redis instance is
`REQUIRES_MANUAL_OPERATOR_CONFIGURATION` (Connection 6 in
[MANUAL_SERVICE_CONNECTIONS.md](MANUAL_SERVICE_CONNECTIONS.md)).

## Two layers

1. **Gateway (Vercel server, `frontend/lib/server/rateLimit.ts`)** —
   per-IP pre-auth limits (`unauthenticated`, `auth_pressure`) and per-user
   post-auth limits (`authenticated`, `polling`, `run_creation`,
   `cancellation`). Limits are tuned with
   `GATEWAY_RATE_LIMIT_<CATEGORY>_REQUESTS` / `_WINDOW_MS` (prefixes:
   `GATEWAY_RATE_LIMIT_UNAUTH`, `_AUTH_PRESSURE`, `_AUTHENTICATED`,
   `_POLLING`, `_RUN_CREATION`, `_CANCELLATION`).
2. **API (`backend/rate_limit.py`)** — categories `run_creation_user`,
   `run_creation_project`, `cancellation`, `worker_mutations`, tuned with
   `MILO_RATE_LIMIT_*`.

## Store and failure mode

Both layers use the same Upstash Redis REST store
(`UPSTASH_REDIS_REST_URL` / `UPSTASH_REDIS_REST_TOKEN`). In production,
mutation-class surfaces **fail closed (503 + Retry-After)** when the store
is unconfigured or unreachable — requests are refused rather than running
unmetered (`RateLimiterUnavailable`; the gateway returns the same 503
behavior). `PROD_MEMORY_RATE_LIMITER` is a startup error in production
without the store.

## Key structure and privacy

Identifiers (user ids, IPs, tokens) are SHA-256 hashed before use as key
material; keys look like `rl:<category>:<hash>` — no private data in the
keyspace. Environment isolation comes from a dedicated database per
environment (recorded as `MILO_REDIS_LOGICAL_ENVIRONMENT` in operator
metadata and validated by `scripts/release/check-redis-config.sh`); dev and
production must never share an instance.

## Operations

- Verify: `scripts/release/check-redis-config.sh --env-file <metadata>`
  (TLS required; optional single read-only `--allow-network` PING).
- Rotate: issue a new token in the provider console, update the Vercel
  server env and the Cloud Run secret reference, redeploy, then revoke the
  old token.
- Disable safely: production cannot silently drop to in-memory limiting;
  to take Redis out of service, first disable execution surfaces
  (flags off), accept 503 on limited routes, or point at a replacement
  instance. Never flush the production keyspace.

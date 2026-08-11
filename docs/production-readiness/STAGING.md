# Isolated Stage B staging environment

Purpose: rehearse the complete MILO run lifecycle with mocked, zero-cost
model dependencies in an environment that shares nothing with production.
Production (`big-cabinet-457321-t7`) is read-only during Stage B and its
worker execution count must not change.

## Resources and naming

| Concern | Production | Staging |
| --- | --- | --- |
| GCP project | `big-cabinet-457321-t7` | `milo-agent-staging-xxxx` |
| Region | `us-central1` | `us-central1` |
| Artifact Registry | `milo-agent` | `milo-agent-staging` |
| API service | `milo-agent-api` | `milo-agent-api-staging` |
| Worker job | `milo-agent-worker` | `milo-agent-worker-staging` |
| API runtime SA | `milo-api-runtime@…` | `milo-api-staging@…` |
| Worker runtime SA | `milo-worker-runtime@…` | `milo-worker-staging@…` |
| Gateway identity | Vercel WIF SA | `milo-gateway-staging@…` |
| Supabase | production project | `cxlwavxvwgrfikkudtzf` |
| Redis | Upstash (dedicated prod DB) | `milo-redis-shim-staging` Cloud Run service (`scripts/staging/redis-shim`, Upstash-REST-compatible, single instance, TLS via Cloud Run) |
| Provider key | Stage C only | **never present** |

Every staging name carries a `-staging` suffix and lives in the staging
project, so no staging resource can be mistaken for production. The
deployment script refuses to run if any configured value references the
production project id, and refuses the default Compute Engine service
account as a runtime identity.

## Posture (Stage B)

- `ENVIRONMENT=staging`; `MILO_WORKER_ENGINE=mock` on the worker
  (forbidden in production — `TEST_ADAPTER_IN_PRODUCTION`).
- `MILO_ENABLE_RUN_CREATION=true`, `MILO_ENABLE_RUN_CANCELLATION=true`,
  `JOB_LAUNCHER=cloud_run` — the mocked lifecycle needs them; every other
  execution flag stays `false`.
- `MILO_ENABLE_PAID_EXECUTION=false` always; no `KIMI_API_KEY` /
  `MOONSHOT_API_KEY` secret, binding or value exists anywhere in the
  staging project.
- Small budget caps (`MILO_MAX_*`, `MILO_DAILY_*`) so the mock engine can
  trip every limit cheaply.
- API→worker launch permission: `roles/run.jobsExecutorWithOverrides` on
  the one staging job, granted to the staging API identity only (the
  launcher sends `containerOverrides`; see
  [MANUAL_SERVICE_CONNECTIONS.md](MANUAL_SERVICE_CONNECTIONS.md)
  Connection 4).
- API is private (`--no-allow-unauthenticated`); invokers are the staging
  gateway and worker identities only.

## Deploying

```
DEPLOY_MODE=check scripts/deploy/staging-cloud-run.sh   # plan only
DEPLOY_MODE=apply scripts/deploy/staging-cloud-run.sh   # deploy
```

The script builds both images from the current commit (full-SHA tags),
deploys the worker job first (never executes it), grants the launcher
binding, deploys the private API, then pins `MILO_GATEWAY_AUDIENCE` to
the deployed API URL and `MILO_WORKER_AUDIENCE` to a distinct
staging-scoped value so wrong-audience rejection is testable.

Secrets (`SUPABASE_URL`, `SUPABASE_SECRET_KEY`, `UPSTASH_REDIS_REST_URL`,
`UPSTASH_REDIS_REST_TOKEN`) must exist in the staging project's Secret
Manager with enabled versions before `apply`; the script verifies
metadata only and never reads or prints a payload.

## What staging must never contain

- any production Supabase URL/key, Redis credential, service account,
  Cloud Run URL or image reference;
- a provider credential of any kind;
- `MILO_ENABLE_PAID_EXECUTION=true`.

`scripts/deploy/staging-cloud-run.sh` enforces the project-id and
provider-key rules mechanically; the Stage B acceptance report records an
explicit staging/production isolation comparison for the rest.

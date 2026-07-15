> **ARCHIVED (historical).** This document predates Phases 1–11 and may contain stale claims (e.g. in-memory rate limiting, pre-gateway auth, earlier migration coverage). The authoritative, current documentation is [`docs/production-readiness/`](../production-readiness/README.md). Where this file contradicts that set, that set wins.

# Cloud Run production deployment notes

This document describes the production Cloud Run deployment flow and the remaining frontend gateway requirement. It is intentionally operational documentation only; do not deploy from automated agent environments.

## Corrected build flow

`scripts/deploy/cloud-run.sh` builds immutable commit-SHA-tagged images through explicit Cloud Build configs:

1. Build and push the worker image with `scripts/deploy/cloudbuild-worker.yaml`, using `Dockerfile.worker` and the `_WORKER_IMAGE` substitution.
2. Build and push the API image with `scripts/deploy/cloudbuild-api.yaml`, using `Dockerfile.api` and the `_API_IMAGE` substitution.
3. Deploy the private Cloud Run worker job before the API service.
4. Grant the runtime service account `roles/run.jobsExecutorWithOverrides` on only the `milo-agent-worker` Cloud Run job so the API can launch the worker with a `RUN_ID` execution override.
5. Deploy the private API service with `--no-allow-unauthenticated` and print only the final service URL.

The script uses the Git commit short SHA in both image names and does not rely on mutable `latest` tags.

## Safe modes

Use check mode for read-only validation:

```bash
DEPLOY_MODE=check ALLOWED_CORS_ORIGINS=https://example.vercel.app scripts/deploy/cloud-run.sh
```

Check mode validates prerequisites and prints the intended images and targets. It does not build, deploy, modify IAM, execute the worker job, connect to Supabase data paths, or call Kimi/Moonshot.

A production deployment requires explicit apply mode:

```bash
DEPLOY_MODE=apply ALLOWED_CORS_ORIGINS=https://example.vercel.app scripts/deploy/cloud-run.sh
```

Do not use `*` in `ALLOWED_CORS_ORIGINS`. Multiple origins must remain comma-separated, for example `https://app.example.com,https://admin.example.com`; the deployment script uses gcloud alternate delimiter syntax so those commas are preserved as a single API environment-variable value. The API remains private even when CORS is configured.

## Preflight behavior

Before any build occurs, the script verifies:

- an active gcloud account exists;
- the configured project is accessible;
- `run.googleapis.com`, `cloudbuild.googleapis.com`, `artifactregistry.googleapis.com`, and `secretmanager.googleapis.com` are enabled;
- the runtime service account exists;
- the Artifact Registry repository exists;
- the `KIMI_API_KEY`, `SUPABASE_URL`, and `SUPABASE_SECRET_KEY` Secret Manager secrets exist;
- `ALLOWED_CORS_ORIGINS` is present, non-empty, and contains no wildcard origin.

The script does not create or replace secrets and never prints secret values.

## IAM requirements

The API launches the worker through the Cloud Run v2 jobs `:run` endpoint and supplies an execution override containing the `RUN_ID` environment variable. That override requires the `run.jobs.runWithOverrides` permission. The deployment script therefore grants only this job-scoped execution binding after the worker job exists:

```text
Cloud Run Job: milo-agent-worker
Member: serviceAccount:id-kimi-agent-runner@big-cabinet-457321-t7.iam.gserviceaccount.com
Role: roles/run.jobsExecutorWithOverrides
Permission required from role: run.jobs.runWithOverrides
Scope: the milo-agent-worker job only
```

The runtime service account also needs Secret Manager Secret Accessor on each required secret. Grant these as per-secret IAM bindings, not project-wide access:

```text
KIMI_API_KEY: roles/secretmanager.secretAccessor for the runtime service account
SUPABASE_URL: roles/secretmanager.secretAccessor for the runtime service account
SUPABASE_SECRET_KEY: roles/secretmanager.secretAccessor for the runtime service account
```

`roles/run.invoker` is insufficient for this implementation because it includes `run.jobs.run` but not `run.jobs.runWithOverrides`. Do not grant Owner, Editor, Cloud Run Admin, or project-wide Secret Manager access for this deployment.

## Worker service-to-service authentication (manual IAM + env configuration)

The internal worker mutation routes (`/runs/{id}/tool-*`, `/runs/{id}/sources|claims|conflicts`, `/internal/runs/{id}/events|complete|fail`) require a Google-signed OIDC identity token in the `X-Milo-Worker-Token` header, verified by `backend/worker_auth.py` (signature, issuer, audience, expiration, verified service-account email, explicit allowlist). Browser identity headers are never consulted on these routes and `MILO_ENABLE_EXECUTION_CONTROL` alone never authorizes a call. The boundary fails closed (HTTP 503) until both env values below are configured.

Manual operator configuration (documented only — **no IAM change is applied by this repository**):

```text
1. Create a dedicated worker service account (do not reuse the API runtime SA):
   gcloud iam service-accounts create milo-worker --display-name="MILO worker job"

2. Run the worker job as that service account (worker job deploy flag):
   --service-account=milo-worker@<PROJECT_ID>.iam.gserviceaccount.com

3. Allow the worker SA to call the private API service (service-scoped, not project-wide):
   gcloud run services add-iam-policy-binding milo-agent-api \
     --member=serviceAccount:milo-worker@<PROJECT_ID>.iam.gserviceaccount.com \
     --role=roles/run.invoker --region=<REGION>

4. Configure the API service environment:
   MILO_WORKER_AUDIENCE=<https URL of the milo-agent-api Cloud Run service>
   MILO_APPROVED_WORKER_IDENTITIES=milo-worker@<PROJECT_ID>.iam.gserviceaccount.com

5. The worker mints its token from the metadata server (no key files):
   GET http://metadata/computeMetadata/v1/instance/service-accounts/default/identity?audience=<MILO_WORKER_AUDIENCE>
   and sends it as X-Milo-Worker-Token.
```

Both env values are empty by default, so worker mutations stay unusable until an operator configures them deliberately.

## Supabase server-side key policy

The backend supports Supabase's modern server-side secret API keys with the `sb_secret_` prefix through the pinned official `supabase==2.27.2` Python client. Keep the production Secret Manager secret named `SUPABASE_SECRET_KEY` populated with the modern server-side key, and keep the Cloud Run mapping compatible with the existing deployment script: `SUPABASE_SERVICE_ROLE_KEY=SUPABASE_SECRET_KEY:latest`. The application also accepts `SUPABASE_SECRET_KEY` directly for local and future runtime configurations, while preserving `SUPABASE_SERVICE_ROLE_KEY` as a backward-compatible alias.

Do not re-enable, restore, or depend on legacy JWT service-role API keys. Server-side Supabase keys must remain only in Google Secret Manager or equivalent protected backend secret stores; never commit them, print them, place them in frontend configuration, or send them to browser bundles. The frontend may use only public Supabase configuration such as an anon or publishable key, and must never receive `SUPABASE_SECRET_KEY` or `SUPABASE_SERVICE_ROLE_KEY`.

## Trusted browser gateway (implemented)

The browser never calls the private Cloud Run API directly. The Next.js
server-side gateway (`/api/gateway/[...path]`):

1. validates the Supabase access token against `GET {SUPABASE_URL}/auth/v1/user`;
2. discards every browser-supplied internal header and regenerates
   `x-milo-auth-user-id` / `x-milo-auth-user-email` from the validated user;
3. obtains a Google-signed ID token for the Cloud Run audience and sends it
   both as the Cloud Run `Authorization` bearer and as
   `X-Milo-Gateway-Token`.

The backend (`backend/gateway_auth.py`) verifies `X-Milo-Gateway-Token`
(signature, issuer, audience, expiration, verified service-account email,
explicit allowlist) BEFORE trusting any identity header — reaching the
private service is never sufficient. Manual configuration on the API
service (no IAM change is applied by this repository):

```text
MILO_GATEWAY_AUDIENCE=<https URL of the milo-agent-api Cloud Run service>
MILO_APPROVED_GATEWAY_IDENTITIES=<the Vercel gateway's Google service account email>
```

The gateway service account must be distinct from the worker service
account: shared identities are rejected by
`backend/production_config.py`, worker identities cannot mint browser
users, and gateway identities cannot call worker mutation routes.
Production fails closed (503) when gateway auth is unconfigured. Do not
make the Cloud Run API unauthenticated merely to make the frontend work.

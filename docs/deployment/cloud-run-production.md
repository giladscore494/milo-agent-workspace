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

## Frontend security gap

The frontend currently calls `NEXT_PUBLIC_API_URL` directly from the browser. The production Cloud Run API is intentionally private, so Vercel must not be pointed directly at the private Cloud Run service URL.

Before browser end-to-end production use, implement a secure authenticated gateway or server-side proxy that can authenticate users and call the private Cloud Run API from a trusted server identity. Do not make the Cloud Run API unauthenticated merely to make the frontend work.

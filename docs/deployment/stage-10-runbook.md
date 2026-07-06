# Stage 10 production deployment and rollback runbook

This repository now treats the browser as a control plane only: the API persists a queued run, invokes a private Cloud Run Job with `RUN_ID`, and returns immediately. The worker claims the durable run, heartbeats its lease, resumes from checkpoints, writes events/messages/tasks, and marks final status.

## Infrastructure targets

- Google Cloud project: `big-cabinet-457321-t7`
- Runtime service account: `id-kimi-agent-runner@big-cabinet-457321-t7.iam.gserviceaccount.com`
- Artifact Registry repository: `milo-agent`
- Cloud Run service: `milo-agent-api`
- Cloud Run job: `milo-agent-worker`
- Secret Manager references only: `KIMI_API_KEY`, `SUPABASE_URL`, `SUPABASE_SECRET_KEY` (modern `sb_secret_` server-side key)
- Frontend: Vercel Next.js project with `NEXT_PUBLIC_API_URL` pointing to the authenticated API gateway/proxy URL.

## Security and IAM

Do not create service-account JSON keys. Deploy using ADC or workload identity. Grant the runtime service account only:

- `roles/secretmanager.secretAccessor` on the three required secrets.
- Permission to run only the `milo-agent-worker` job, via a custom role or the narrowest Cloud Run job executor role available in the project.
- No Owner or Editor role.

Keep the Cloud Run API private (`--no-allow-unauthenticated`). Grant invoker only to the identity used by the frontend backend/proxy or authorized operators. Keep the worker as a private Cloud Run Job and invoke it only from the API identity.

## Deploy

1. Apply Supabase migrations in order from `supabase/migrations/` and verify RLS is enabled. The migrations idempotently seed the MILO project and add run invocation/stuck-run tracking.
2. Configure production CORS with exact Vercel preview and production origins in `ALLOWED_CORS_ORIGINS`; do not use `*`.
3. Run offline CI checks. Do not run live Kimi tests or `test_websearch.py` by default.
4. Run `scripts/deploy/cloud-run.sh` from an authenticated Google Cloud shell or CI identity.
5. Configure Vercel environment variables:
   - `NEXT_PUBLIC_API_URL`: public backend URL/proxy URL only.
   - Never expose `SUPABASE_SECRET_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, `KIMI_API_KEY`, or service role values to client bundles. Do not re-enable legacy JWT Supabase service-role keys; keep the modern server-side key only in Secret Manager and mapped into private Cloud Run services/jobs.

## Cost controls

- API max instances: 10, timeout: 300s.
- Worker job parallelism: 1, tasks: 1, max retries: 1, timeout: 3600s.
- Runtime duplicate prevention uses `runs.idempotency_key` and active-run lookup before insert.
- Kimi token/search limits remain governed by workflow proposals, internet grants, and engine constants.
- Create a Google Cloud budget alert for the project before any paid smoke test.

## Observability

Use structured run events and logs with `run_id`, `task_id`, and `agent_key` where available. Never log secret values. `public.stuck_runs` identifies runs with expired leases. Health is available at `/health`; readiness requires repository access through normal API reads.

## Rollback

1. Disable new user starts at the frontend or API gateway.
2. Redeploy the previous API and worker image tags from Artifact Registry.
3. Keep migrations forward-only; if rollback requires schema behavior changes, add a new corrective migration rather than deleting data.
4. For stuck runs, inspect `stuck_runs`, verify no active execution is heartbeating, then re-invoke the worker with the same `RUN_ID` to resume from checkpoints.

## Paid smoke test policy

A real Kimi smoke test requires explicit user approval, a strict token/search cap, and recorded cost. Never run `MILO-main-original/MILO-main/test_websearch.py` automatically.

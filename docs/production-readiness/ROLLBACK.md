# Rollback runbook

Exact forward-safe procedures per component. Generate the parameterized
command sequence with `scripts/release/generate-rollback-plan.sh
--previous-sha <FULL_SHA>`. First action in every incident: execution
flags off (order below). Everything here is manual; nothing rolls back
automatically.

## Execution flags — emergency order

1. `MILO_ENABLE_PAID_EXECUTION` off;
2. `MILO_ENABLE_RUN_CREATION` off (and `GATEWAY_ALLOW_EXECUTION_ROUTES`
   off);
3. worker launch off (`JOB_LAUNCHER=disabled`);
4. worker route access restricted (verify
   `MILO_APPROVED_WORKER_IDENTITIES`);
5. revoke the worker's provider-secret binding if necessary;
6. the API remains read-only where safe.

Flags are individual by design; there is no disable-all script either —
each step is explicit and auditable.

## Vercel

1. `vercel ls <VERCEL_PROJECT_NAME>` — identify the previous successful
   deployment; 2. `vercel inspect <PREVIOUS_URL>` — inspect environment
   differences; 3. `vercel promote <PREVIOUS_URL>` — promote manually;
4. restore previous server env values (`vercel env add … production`);
5. `cd frontend && npm run test:secrets` against the promoted build —
   verify the browser bundle contains no secret; 6. rerun
   `smoke-test-read-only.sh`.

## Cloud Run API

1. execution flags off first (above); 2. `gcloud run revisions list
--service <CLOUD_RUN_API_SERVICE> --region <GCP_REGION>` — identify the
previous revision; 3. verify its image digest equals
`milo-api:<PREVIOUS_SHA>`; 4. `gcloud run services update-traffic …
--to-revisions <PREVIOUS_REVISION>=100` — move traffic explicitly;
5. verify private IAM (no `allUsers`); 6. verify health through the
gateway; 7. **preserve the failed revision** for investigation — do not
delete it.

## Cloud Run worker

1. stop new launches (`JOB_LAUNCHER=disabled`); 2. disable run creation;
3. `gcloud run jobs update <CLOUD_RUN_WORKER_JOB> --image
…/milo-worker:<PREVIOUS_SHA>` — previous immutable image; 4. do **not**
execute the job to test; 5. verify service account and secret mappings
(`jobs describe`); 6. already-running executions: cancel their runs
through the API path or let leases expire — stale workers are rejected by
lease-token checks, so a superseded execution cannot corrupt state.

## Migrations

No destructive automated down-migration exists, by policy. Procedure:
1. stop execution (flags); 2. take/verify a backup; 3. inspect state
(`check-migration-state.sh`); 4. write corrective **forward** SQL;
5. review manually; 6. apply only after explicit approval; 7. verify RLS
and ownership afterwards (PostgreSQL suite expectations / read-only smoke
tests).

## Environment variables

Export metadata/names only (`vercel env ls`, `gcloud run services
describe --format 'value(spec.template.spec.containers[0].env)'`).
Maintain the approved versioned manifest copy
(`config/production.example.yaml` schema — names/references, never
values). Restore prior names and secret references, redeploy only after
review, then verify flags remain off
(`smoke-test-execution-disabled.sh`).

## Redis

1. if shared rate limiting is unavailable, execution surfaces already
fail closed — additionally disable new execution; 2. preserve the
production keyspace — never `FLUSHDB`/`FLUSHALL`; 3. rotate the credential
if compromised (Upstash console → Vercel env + secret version → revoke
old); 4. restore the previous endpoint reference; 5. verify project/user
limits recover (`check-redis-config.sh --allow-network`).

## Provider access

1. `MILO_ENABLE_PAID_EXECUTION` off; 2. remove the worker's
`secretAccessor` binding on `<PROVIDER_KEY_SECRET_NAME>`; 3. rotate the
provider key manually in the provider console if compromised; 4. verify no
other service has access (`gcloud secrets get-iam-policy`); 5. inspect
usage and cost in the provider console (external).

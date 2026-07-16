# Manual service connections

Exact operator guide for connecting the external services. Every command
is a TEMPLATE: replace the `<PLACEHOLDERS>` (values live in the operator's
approved manifest copy, never in this repository):

```text
<GCP_PROJECT_ID> <GCP_REGION> <ARTIFACT_REGISTRY_REPOSITORY>
<CLOUD_RUN_API_SERVICE> <CLOUD_RUN_WORKER_JOB>
<API_SERVICE_ACCOUNT_EMAIL> <WORKER_SERVICE_ACCOUNT_EMAIL>
<GATEWAY_IDENTITY_EMAIL> <SUPABASE_PROJECT_REF> <VERCEL_PROJECT_NAME>
<REDIS_HOST> <REDIS_PORT> <RELEASE_SHA>
```

Global security warnings: never print or paste a secret value into a
terminal command that gets logged; never grant `roles/owner`,
`roles/editor` or project-wide `roles/secretmanager.secretAccessor`; never
use a wildcard principal; all of these connections are
`REQUIRES_MANUAL_OPERATOR_CONFIGURATION`.

---

## Connection 1 — Supabase → Cloud Run API

- **Prerequisites:** Supabase project `<SUPABASE_PROJECT_REF>`; secret
  created in Secret Manager; API service account exists.
- **Identity used:** `<API_SERVICE_ACCOUNT_EMAIL>` (API runtime).
- **Source → destination:** Cloud Run API → Supabase (PostgREST).
- **Authentication mechanism:** `SUPABASE_URL` + service-role key. The key
  is server-only: Secret Manager → Cloud Run secret reference. It never
  enters Vercel variables or frontend bundles (enforced by bundle checks),
  and RLS plus application membership authorization still govern every
  browser-originated operation.
- **Variables:** `SUPABASE_URL` (env), `SUPABASE_SERVICE_ROLE_KEY`
  (secret reference). Entered on the Cloud Run API service only.
- **Read-only verification (metadata only, key never printed):**

      gcloud secrets describe <SUPABASE_SERVICE_KEY_SECRET_NAME> --project <GCP_PROJECT_ID>
      gcloud run services describe <CLOUD_RUN_API_SERVICE> --region <GCP_REGION> \
        --format 'value(spec.template.spec.containers[0].env)'   # names + secret refs only

- **Mutation template (grant access, secret level only):**

      gcloud secrets add-iam-policy-binding <SUPABASE_SERVICE_KEY_SECRET_NAME> \
        --project <GCP_PROJECT_ID> \
        --member serviceAccount:<API_SERVICE_ACCOUNT_EMAIL> \
        --role roles/secretmanager.secretAccessor

- **Rollback template:** same command with `remove-iam-policy-binding`.
- **Expected result:** API `/health` reports healthy; reads work through
  the gateway; `check-secret-metadata.sh` shows the API SA as a consumer.
- **Common failure modes:** secret not referenced in the service spec
  (startup crash: missing `SUPABASE_SERVICE_ROLE_KEY`); binding applied at
  project level (forbidden — rebind at secret level); wrong Supabase URL
  (connection errors in logs).

## Connection 2 — Supabase → Cloud Run worker

- **Prerequisites:** Connection 1 done; worker service account exists and
  is distinct from the API SA and browser identities.
- **Identity used:** `<WORKER_SERVICE_ACCOUNT_EMAIL>` (worker runtime).
- **Source → destination:** Cloud Run worker job → Supabase.
- **Authentication mechanism:** same server-only secret pattern as
  Connection 1, but bound separately to the worker SA. Worker credentials
  are never shared with Vercel. Worker mutations remain doubly protected:
  Google-verified service identity **and** the active execution lease
  (worker id + attempt + lease token).
- **Variables:** `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` (secret
  reference), plus provider/Redis secrets per Connections 6–7. Entered on
  the Cloud Run worker job only.
- **Read-only verification:**

      gcloud run jobs describe <CLOUD_RUN_WORKER_JOB> --region <GCP_REGION> \
        --format 'value(spec.template.spec.template.spec.serviceAccountName)'
      gcloud secrets get-iam-policy <SUPABASE_SERVICE_KEY_SECRET_NAME> --project <GCP_PROJECT_ID>

- **Mutation template:** `gcloud secrets add-iam-policy-binding … --member
  serviceAccount:<WORKER_SERVICE_ACCOUNT_EMAIL> --role
  roles/secretmanager.secretAccessor` (secret level, per required secret
  only — never project-wide).
- **Rollback:** `remove-iam-policy-binding` (this is also the provider
  kill switch for Connection 7).
- **Expected result:** worker job metadata shows the worker SA; secret IAM
  lists exactly the intended consumers.
- **Failure modes:** worker job accidentally using the API SA (identity
  separation check in `check-gcp-resources.sh` blocks this); secret
  granted project-wide (blocked by `check-secret-metadata.sh`).

## Connection 3 — Vercel gateway → private Cloud Run API

This documents the mechanism the repository actually implements
(`frontend/lib/server/cloudRunAuth.ts`): **short-lived Vercel OIDC →
Google Workload Identity Federation → service-account ID token**. There is
no long-lived Google key anywhere in the flow; do not introduce one.

- **Prerequisites:** Vercel project `<VERCEL_PROJECT_NAME>` with OIDC
  issuance enabled; GCP APIs `iamcredentials.googleapis.com` and
  `sts.googleapis.com` enabled; dedicated gateway service account
  `<GATEWAY_IDENTITY_EMAIL>` (distinct from the worker identity).
- **Identity used:** `<GATEWAY_IDENTITY_EMAIL>` via federation; no stored
  credential.
- **Source → destination:** Vercel server runtime → private Cloud Run API.
- **Authentication mechanism:** the gateway mints a Google-signed ID token
  with audience `<CLOUD_RUN_API_URL>`, sends it as the `Authorization`
  bearer (Cloud Run IAM) and as `X-Milo-Gateway-Token` (application
  check). The API verifies issuer, audience (`MILO_GATEWAY_AUDIENCE`),
  expiry, signature and allowlisted identity
  (`MILO_APPROVED_GATEWAY_IDENTITIES`); unconfigured ⇒ 503 fail-closed.
  The browser never sees the token and cannot set trusted identity
  headers (the gateway rebuilds headers from scratch).
- **Variables:** Vercel server env: `CLOUD_RUN_API_URL`,
  `GCP_PROJECT_NUMBER`, `GCP_WORKLOAD_IDENTITY_POOL_ID`,
  `GCP_WORKLOAD_IDENTITY_POOL_PROVIDER_ID`, `GCP_SERVICE_ACCOUNT_EMAIL`.
  Cloud Run API env: `MILO_GATEWAY_AUDIENCE` (= the API service URL),
  `MILO_APPROVED_GATEWAY_IDENTITIES` (= `<GATEWAY_IDENTITY_EMAIL>`).
- **Mutation templates (create the federation, one-time):**

      gcloud iam workload-identity-pools create <POOL_ID> --location global --project <GCP_PROJECT_ID>
      gcloud iam workload-identity-pools providers create-oidc <PROVIDER_ID> \
        --location global --workload-identity-pool <POOL_ID> --project <GCP_PROJECT_ID> \
        --issuer-uri https://oidc.vercel.com/<VERCEL_TEAM_SLUG> \
        --attribute-mapping google.subject=assertion.sub \
        --attribute-condition "assertion.aud=='https://vercel.com/<VERCEL_TEAM_SLUG>'"
      gcloud iam service-accounts add-iam-policy-binding <GATEWAY_IDENTITY_EMAIL> \
        --project <GCP_PROJECT_ID> --role roles/iam.workloadIdentityUser \
        --member "principalSet://iam.googleapis.com/projects/<GCP_PROJECT_NUMBER>/locations/global/workloadIdentityPools/<POOL_ID>/attribute.project/<VERCEL_PROJECT_ID>"
      gcloud run services add-iam-policy-binding <CLOUD_RUN_API_SERVICE> \
        --region <GCP_REGION> --project <GCP_PROJECT_ID> \
        --member serviceAccount:<GATEWAY_IDENTITY_EMAIL> --role roles/run.invoker

- **Read-only verification / tests:**

      gcloud iam workload-identity-pools providers describe <PROVIDER_ID> \
        --location global --workload-identity-pool <POOL_ID>
      # accepted gateway call (through the deployed gateway):
      curl -s <PRODUCTION_VERCEL_URL>/api/gateway/health          # expect 200
      # direct browser rejection (bypassing the gateway):
      curl -s -o /dev/null -w '%{http_code}\n' <CLOUD_RUN_API_URL>/health   # expect 401/403
      # server env-var NAMES only (values never read); identity is fail-closed:
      scripts/release/check-vercel-config.sh --project <VERCEL_PROJECT_NAME> \
        --vercel-cwd frontend --token-env VERCEL_TOKEN

  `check-vercel-config.sh` proves the linked Vercel project identity **before**
  inspecting any variable: it reads `projectId`/`orgId` from
  `frontend/.vercel/project.json`, resolves the project with `vercel project
  inspect <name>`, and requires the resolved project ID (and team/org where the
  CLI reports it) to match the linked file. A missing/malformed link file, a
  failed inspection, a resolved ID that differs, or an org that differs are all
  `BLOCKED` — a human-readable banner alone is never accepted. It lists only
  variable NAMES (never values) and never runs `vercel link`, deploy, promote,
  or any env mutation.

- **Revocation / rollback:** remove the identity from
  `MILO_APPROVED_GATEWAY_IDENTITIES` (immediate), remove the
  `run.invoker` binding, and/or delete the pool provider.
- **Failure modes:** audience mismatch (`GATEWAY_AUTH_INVALID` 401 —
  `MILO_GATEWAY_AUDIENCE` must equal the exact service URL); missing
  Vercel OIDC (gateway 502 with `Missing required environment variable`);
  allowlist empty (503 `GATEWAY_AUTH_NOT_CONFIGURED`).
- **Security warnings:** gateway identity must never appear in
  `MILO_APPROVED_WORKER_IDENTITIES` (startup error), must hold no Secret
  Manager access and no job-invoker permission unless explicitly required.

## Connection 4 — Cloud Run API → Cloud Run worker job

- **Prerequisites:** worker job deployed (never executed during setup);
  separate API and worker SAs.
- **Identity used:** `<API_SERVICE_ACCOUNT_EMAIL>` (launcher).
- **Source → destination:** API → Cloud Run Jobs Run API (job execution).
- **Authentication mechanism:** the API's own runtime identity calls
  `jobs.run` on exactly one job. The API cannot impersonate arbitrary
  service accounts (no `serviceAccountUser` grants). The worker receives
  the run ID as an argument/env override — never browser-supplied secrets.
  Launch results are recorded via CAS; uncertain responses persist
  `launch_unknown` and are never auto-relaunched.
- **Variables:** API env: `JOB_LAUNCHER` (`disabled` until Stage B),
  `GCP_PROJECT_ID`, `GCP_REGION`, `CLOUD_RUN_WORKER_JOB`.
- **Worker job creation template:** see step 5 of
  `scripts/release/generate-deployment-plan.sh` output.
- **Invocation permission template:**

      gcloud run jobs add-iam-policy-binding <CLOUD_RUN_WORKER_JOB> \
        --region <GCP_REGION> --project <GCP_PROJECT_ID> \
        --member serviceAccount:<API_SERVICE_ACCOUNT_EMAIL> --role roles/run.invoker

- **Read-only verification:**

      gcloud run jobs get-iam-policy <CLOUD_RUN_WORKER_JOB> --region <GCP_REGION>
      # expect: only the API SA; no allUsers; not the gateway identity.

- **Negative authorization test:** attempt `gcloud run jobs run` while
  authenticated as the gateway identity or a browser-derived credential —
  expect `PERMISSION_DENIED`. (Never run the job as a positive test.)
- **Rollback:** `remove-iam-policy-binding` (same shape) and
  `JOB_LAUNCHER=disabled`.
- **Failure modes:** job public (blocked by `check-gcp-resources.sh`);
  launcher enabled while Stage A (config check error); double launch
  (prevented by CAS + idempotent creation).

## Connection 5 — Cloud Run → Secret Manager (per-secret access matrix)

| Secret (placeholder name) | Consuming service | Service account | IAM role | Env mapping | Rotation | Rollback/revocation |
| --- | --- | --- | --- | --- | --- | --- |
| `<SUPABASE_SERVICE_KEY_SECRET_NAME>` | API + worker | API SA, worker SA | `roles/secretmanager.secretAccessor` (secret-level) | `SUPABASE_SERVICE_ROLE_KEY` | rotate in Supabase, `gcloud secrets versions add`, redeploy | disable version / remove binding |
| `<PROVIDER_KEY_SECRET_NAME>` | worker only | worker SA | secretAccessor (secret-level) | `KIMI_API_KEY` or `MOONSHOT_API_KEY` | rotate in provider console, add new version | remove worker binding (kill switch) |
| `<REDIS_TOKEN_SECRET_NAME>` | API + worker | API SA, worker SA | secretAccessor (secret-level) | `UPSTASH_REDIS_REST_TOKEN` | rotate in Upstash, add version, update Vercel too | disable version / remove binding |
| gateway credential | — none — | — | — | — | n/a: the gateway uses federation, no stored secret | revoke via allowlist + invoker binding |
| signing/verification material | — none — | — | — | — | n/a: Google-signed tokens verified against public certs | n/a |

Rules: no project-wide accessor grant (BLOCKED by
`check-secret-metadata.sh`); no owner/editor roles; every grant at the
individual secret level; verification is always metadata-only.

`check-secret-metadata.sh` parses the IAM policy **structurally** and only
counts members of the exact `roles/secretmanager.secretAccessor` binding: a
service account that appears only under `viewer`/`admin`/metadata roles never
satisfies (or pollutes) consumer validation. It also distinguishes a genuine
`NOT_FOUND` (BLOCKED: missing secret / no enabled version) from a
permission/API failure (MANUAL: inspection could not be performed) — a failed
`gcloud` call is never reported as "secret missing", "no enabled version", or
a silently-passed consumer check. The same not-found-vs-permission distinction
applies to `check-gcp-resources.sh` for Artifact Registry describe, service
account describe, and the project IAM policy.

## Connection 6 — API and worker → Redis

Actual implementation: Upstash **REST** protocol
(`UPSTASH_REDIS_REST_URL` / `UPSTASH_REDIS_REST_TOKEN`) in
`backend/rate_limit.py` and `frontend/lib/server/rateLimit.ts`. There is
no `redis://` connection string in the codebase; `<REDIS_HOST>`/
`<REDIS_PORT>` apply only if a provider console asks for them.

- **Prerequisites:** dedicated production database (never shared with
  dev); TLS (https endpoint) mandatory.
- **Credential storage:** Vercel server env (gateway) and Secret Manager
  reference (API/worker). Never in the browser bundle.
- **Timeout/retry:** single short-timeout REST call per check; on failure
  production **fails closed** (503 + Retry-After) on limited surfaces —
  requests are refused, never unmetered (see
  [RATE_LIMITING.md](RATE_LIMITING.md)).
- **Key structure:** `rl:<category>:<sha256(identifier)>` — hashed, no
  private data; environment isolation by dedicated database, recorded as
  `MILO_REDIS_LOGICAL_ENVIRONMENT` in operator metadata.
- **Health verification:** `scripts/release/check-redis-config.sh
  --env-file <metadata> [--allow-network]` (single read-only PING).
- **Rotation:** new token in Upstash console → update Vercel env + secret
  version → redeploy → revoke old token.
- **Disable safely:** flags off first; limited surfaces 503; never flush
  the production keyspace.
- **Failure modes:** http (not https) endpoint (BLOCKED); shared dev/prod
  database (isolation label mismatch BLOCKED); missing token (fail-closed
  503 in production).

## Connection 7 — Worker → Kimi/Moonshot provider

- **Credential variable accepted by the code:** `KIMI_API_KEY` or
  `MOONSHOT_API_KEY` (`backend/production_config.py`; the engine reads the
  same names). Worker-only secret injection (Connection 5 matrix). No
  provider key in run input, API responses, logs or frontend.
- **Staging:** provider access stays disabled until Stage C: key absent,
  `MILO_ENABLE_PAID_EXECUTION` off. Enabling paid execution without the
  key AND all mandatory budget caps is a startup error in production.
- **One controlled smoke run only** at Stage C (see
  [STAGED_ACTIVATION.md](STAGED_ACTIVATION.md)); never a real provider
  request during repository preparation or CI.
- **Read-only verification:** `gcloud secrets get-iam-policy
  <PROVIDER_KEY_SECRET_NAME>` — worker SA only.
- **Revocation/rollback:** set `MILO_ENABLE_PAID_EXECUTION` off → remove
  the worker's secret binding → rotate the key in the provider console →
  verify no other accessor → inspect usage/cost in the provider console.

## Connection 8 — Vercel → Supabase authentication

Actual implementation: the browser client uses only
`NEXT_PUBLIC_SUPABASE_URL` and `NEXT_PUBLIC_SUPABASE_ANON_KEY`
(`frontend/lib/supabaseClient.ts`); the gateway independently validates
the user's access token server-side (`frontend/lib/server/supabaseAuth.ts`)
and the API additionally verifies the gateway token before trusting any
identity header — browser claims alone are never sufficient.

- Only public/anonymous values enter browser configuration; service-role
  material is banned from `NEXT_PUBLIC_*` (three independent checks).
- **Manual Supabase dashboard actions:** set the Site URL to the exact
  production domain; add explicit redirect URLs (production domain, plus
  intentionally-chosen preview domains only — wildcard previews are a
  deliberate decision to make, not a default); confirm the anon key in
  Vercel matches `<SUPABASE_PROJECT_REF>`.
- Session tokens are never logged (gateway logs contain no auth headers).
- **Verification:** sign-in round-trip on the production domain; sign-in
  from a non-allowlisted domain must fail.
- **Rollback:** restore the previous redirect-URL list; rotate the anon
  key if leaked (public but rotatable).

## Connection 9 — Domain and CORS

- **Production Vercel domain:** `<PRODUCTION_VERCEL_ORIGIN>` — the only
  browser origin the API should accept.
- **Private Cloud Run origin:** `<CLOUD_RUN_API_URL>` — not
  browser-reachable; CORS there is defense in depth (the gateway is the
  only legitimate caller).
- **Configuration:** API env `ALLOWED_CORS_ORIGINS=<PRODUCTION_VERCEL_ORIGIN>`
  (comma-separated for multiple explicit origins). Preview-origin policy:
  previews go through their own preview gateway; do NOT add preview
  wildcards to the API. Wildcards are rejected at startup
  (`CORS_WILDCARD`), by the config checker and by the manifest validator —
  never add one as a temporary workaround.
- **Credential policy:** CORS responses do not include credentials;
  authentication is bearer-token based through the gateway.
- **Verification:** `check-production-config.sh --env-file <metadata>`;
  browser preflight from the production origin succeeds, from another
  origin fails.
- **Rollback:** restore the previous explicit origin list and redeploy
  the API revision (see [ROLLBACK.md](ROLLBACK.md)).

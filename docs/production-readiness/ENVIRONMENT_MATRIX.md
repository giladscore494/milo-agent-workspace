# Environment matrix

Every production variable found in code, with no values. Verified against
source by `scripts/release/check-production-config.sh` (the check WARNs if
this inventory drifts from code). Legend — Component: `browser` (Next.js
bundle), `vercel` (Vercel server runtime/gateway), `api` (Cloud Run API),
`worker` (Cloud Run worker job), `test` (local/CI only). Req(prod) =
required in production; Req(exec-off) = required while execution is
disabled (Stage A).

| Variable | Component | Browser-visible | Secret | Req (prod) | Req (exec-off) | Source of value | Validation | Safe default | Rotation / rollback |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `NEXT_PUBLIC_SUPABASE_URL` | browser | yes | no | yes | yes | Supabase project settings | `check-production-config.sh`; bundle check | none | update Vercel env, redeploy |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | browser | yes | no (public anon) | yes | yes | Supabase project settings | bundle secret check (`npm run test:secrets`) | none | rotate anon key in Supabase, update Vercel env |
| `NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI` | browser | yes | no | no | no | operator (Stage C+) | `check_unsafe_defaults.py` | off | unset to hide execution UI |
| `CLOUD_RUN_API_URL` | vercel | no | no | yes | yes | Cloud Run service URL | placeholder check; gateway smoke | none | restore previous URL, redeploy Vercel |
| `GCP_PROJECT_NUMBER` | vercel | no | no | yes | yes | GCP project | gateway auth flow | none | n/a (project constant) |
| `GCP_WORKLOAD_IDENTITY_POOL_ID` | vercel | no | no | yes | yes | manual WIF setup | gateway auth flow | none | recreate pool; update env |
| `GCP_WORKLOAD_IDENTITY_POOL_PROVIDER_ID` | vercel | no | no | yes | yes | manual WIF setup | gateway auth flow | none | recreate provider; update env |
| `GCP_SERVICE_ACCOUNT_EMAIL` | vercel | no | no | yes | yes | gateway SA email | must be in `MILO_APPROVED_GATEWAY_IDENTITIES` | none | swap SA; update allowlist + env |
| `GATEWAY_ALLOW_EXECUTION_ROUTES` | vercel | no | no | no | must be off | operator (Stage B/C) | `check_unsafe_defaults.py`; smoke tests | off | unset (routes 403) |
| `GATEWAY_RATE_LIMIT_<CAT>_REQUESTS` / `_WINDOW_MS` | vercel | no | no | no (defaults) | no | operator tuning | code defaults | built-in limits | unset restores defaults |
| `UPSTASH_REDIS_REST_URL` | vercel+api+worker | no | no | yes | yes | Upstash console | `check-redis-config.sh` (TLS) | none (fail closed) | point at replacement instance |
| `UPSTASH_REDIS_REST_TOKEN` | vercel+api+worker | no | **yes** | yes | yes | Upstash console → Vercel env / Secret Manager | name-only checks; never printed | none (fail closed) | rotate in console; update env+secret; revoke old |
| `SUPABASE_URL` | api+worker | no | no | yes | yes | Supabase project settings | `check-production-config.sh` | none (startup fails) | restore previous value |
| `SUPABASE_SERVICE_ROLE_KEY` (alias `SUPABASE_SECRET_KEY`) | api+worker | no | **yes** | yes | yes | Supabase → Secret Manager | name-only checks; bundle scan; never printed | none (startup fails) | rotate in Supabase; update secret version; revoke old |
| `ENVIRONMENT` | api+worker | no | no | yes (`production`) | yes | deployment config | `production_config.validate` | `local` | n/a |
| `ALLOWED_CORS_ORIGINS` | api | no | no | yes (explicit) | yes | production Vercel domain(s) | wildcard rejected (`CORS_WILDCARD`) | localhost only | restore previous origin list |
| `JOB_LAUNCHER` | api | no | no | yes (`disabled` at Stage A) | must be `disabled` | operator | config check | `disabled` | set `disabled` (kill switch) |
| `GCP_PROJECT_ID` / `GCP_REGION` / `CLOUD_RUN_WORKER_JOB` | api | no | no | yes when launcher enabled | no | manifest | `check-gcp-resources.sh` | code defaults (overridden in prod) | restore previous values |
| `RATE_LIMIT_PER_MINUTE` | api | no | no | no | no | operator tuning | code default | 60 | unset |
| `MILO_GATEWAY_AUDIENCE` | api | no | no | yes | yes | Cloud Run API URL | fail-closed 503 when missing | none (503) | restore previous audience |
| `MILO_APPROVED_GATEWAY_IDENTITIES` | api | no | no | yes | yes | gateway SA email(s) | fail-closed; disjoint from worker list | none (503) | remove identity to revoke gateway |
| `MILO_WORKER_AUDIENCE` | api | no | no | yes before Stage B | no | Cloud Run API URL | fail-closed on worker routes | none (worker routes 503) | restore |
| `MILO_APPROVED_WORKER_IDENTITIES` | api | no | no | yes before Stage B | no | worker SA email | fail-closed; disjoint from gateway list | none (reject all) | remove identity to revoke worker |
| `MILO_RATE_LIMIT_RUN_CREATION_USER` / `_PROJECT` / `MILO_RATE_LIMIT_CANCELLATION` / `_WORKER_MUTATIONS` | api | no | no | no (defaults) | no | operator tuning | code defaults | built-in limits | unset restores defaults |
| `MILO_ENABLE_RUN_CREATION` | api | no | no | must be off until Stage C | must be off | operator (staged) | `check_unsafe_defaults.py`; smoke tests | off | set off (kill switch #2) |
| `MILO_ENABLE_PROPOSAL_MUTATIONS` / `_PROPOSAL_READS` / `_RUN_CANCELLATION` / `_EXECUTION_CONTROL` | api | no | no | staged | must be off | operator (staged) | same | off | set off |
| `MILO_ENABLE_PAID_EXECUTION` | api+worker | no | no | must be off until Stage C | must be off | operator (Stage C) | fail-closed without budgets+key | off | set off (kill switch #1) |
| `MILO_ENABLE_CATALOG_EXECUTION` | worker | no | no | must be off until separately authorized | must be off | operator (separate authorization; never a release side effect) | `check_unsafe_defaults.py`; `check-production-config.sh`; `parse_env_contract.py`; Stage A deployment contract; `verify_caps.py` | off | set off — the independent catalog kill switch; no code rollback needed; deletes/mutates no catalog row |
| `MILO_DAILY_USER_BUDGET` / `MILO_DAILY_PROJECT_BUDGET` | api+worker | no | no | yes before Stage C | no | operator | nonzero numeric check | none (reserve refuses) | lower/restore values |
| `MILO_MAX_COST_PER_RUN` / `_ESTIMATED_COST_PER_RUN` / `_MODEL_CALLS_PER_RUN` / `_INPUT_TOKENS_PER_RUN` / `_OUTPUT_TOKENS_PER_RUN` / `_TOTAL_TOKENS_PER_RUN` / `_AGENT_STEPS` / `_RETRIES` / `_RUN_DURATION_SECONDS` / `_CONCURRENT_RUNS_PER_USER` / `_CONCURRENT_RUNS_PER_PROJECT` | api+worker | no | no | mandatory subset before paid execution | no | operator | `PAID_WITHOUT_BUDGET` fail-closed | none (paid exec refused) | restore previous caps |
| `MILO_ESTIMATED_COST_PER_CALL` | api+worker | no | no | no | no | operator | code default | 0.05 | unset |
| `KIMI_API_KEY` / `MOONSHOT_API_KEY` | worker | no | **yes** | Stage C only | must be absent | provider console → Secret Manager (worker-only) | name-only check; never printed | absent (paid exec refused) | rotate in provider console; update secret; revoke old |
| `MILO_WORKER_LEASE_SECONDS` / `MILO_WORKER_HEARTBEAT_INTERVAL_SECONDS` | worker | no | no | no (defaults) | no | operator tuning | code defaults | built-in | unset |
| `MILO_ALLOW_INSECURE_DEV_IDENTITY` | test | no | no | forbidden | forbidden | never in production | `INSECURE_DEV_IDENTITY_IN_PRODUCTION` error | off | remove immediately |
| `CLOUD_RUN_AUTH_MODE` | test | no | no | forbidden (`e2e-test`) | forbidden | never in production | `TEST_ADAPTER_IN_PRODUCTION` error; hard-disabled in prod builds | unset | remove immediately |
| `MILO_E2E_INPROCESS_WORKER` | test | no | no | forbidden | forbidden | never in production | `TEST_ADAPTER_IN_PRODUCTION` error | off | remove immediately |
| `MILO_WORKER_ENGINE` | worker (staging only) | no | no | forbidden | forbidden | isolated staging only (`mock` = zero-cost lifecycle engine) | `TEST_ADAPTER_IN_PRODUCTION` error | unset | remove immediately |
| `MILO_EXPECTED_SUPABASE_PROJECT_REF` | api+worker | no | no | yes | yes | approved manifest `supabase.project_ref`; bound by the deployment to BOTH the API service and the worker job | fail-closed startup: required in production (`PRODUCTION_DEPENDENCY_UNPINNED`), rejected if malformed (`PRODUCTION_DEPENDENCY_MALFORMED`) or if `SUPABASE_URL` is not exactly that project's hosted root URL — path, query, fragment, port, credentials and suffix-smuggled hosts all refused (`PRODUCTION_DEPENDENCY_MISMATCH`); same contract in staging under `STAGING_DEPENDENCY_*` | none (startup fails) | update the manifest ref, redeploy both resources |
| `MILO_EXPECTED_REDIS_HOST` | api+worker (staging only) | no | no | n/a | n/a | required when `ENVIRONMENT=staging`: runtime refuses any other Redis endpoint | fail-closed startup | unset | n/a |
| `MILO_REQUIRE_PG_TESTS` | test | no | no | n/a (CI only) | n/a | CI | CI job | unset | n/a |
| `NEXT_PUBLIC_API_URL` | deprecated | yes | no | no | no | legacy CI env only | inventory marks deprecated | unset | remove from CI when convenient |

Notes:

- **`MILO_ENABLE_CATALOG_EXECUTION` also gates the CODE-1 operator capture
  entrypoint** (`backend/catalog/operator_capture.py`). With the flag off, unset,
  empty or malformed, that entrypoint constructs no transport, connects to no
  database, claims no run and captures, ingests and activates nothing. It is the
  same flag and the same true-value parser — there is no second catalog switch.
  See [../catalog-code1-operator-capture.md](../catalog-code1-operator-capture.md).
- **`MILO_ENABLE_CATALOG_EXECUTION` is worker-only and is never browser
  controlled.** It gates the catalog capability INSIDE a Swarm V2 run that is
  already happening: the `GovernmentVehicleTool` registration, the
  `catalog:government:read` scope, the Government evidence mapper and the
  `CatalogPromotionPipeline` (`backend/catalog/execution.py`,
  `backend/worker/main.py`). Off means the capability is ABSENT from trusted
  wiring, not hidden. Unset, `false` and any unrecognised value all mean off.
  It creates no run, opens no execution route, authorizes no paid call, starts
  no capture and schedules nothing, and every existing kill switch remains
  authoritative above it. There is deliberately no `NEXT_PUBLIC_` twin.
- **`MILO_EXPECTED_SUPABASE_PROJECT_REF` pins each runtime to ONE Supabase
  project.** It was previously staging-only, so a production runtime handed
  the wrong `SUPABASE_URL` — a stale secret version, a restored snapshot's
  project, a copy-paste — started and wrote to it. Production now refuses the
  same way staging always has: the ref is required, must be a well-formed
  hosted project ref, and `SUPABASE_URL` must be exactly that project's API
  base URL — `https://<ref>.supabase.co`, optionally with one trailing slash,
  and nothing else. A path, query, fragment, explicit or malformed port,
  embedded credential, pooler/database URL or suffix-smuggled host is refused
  even though it carries the expected hostname. It is required on the **worker job as well
  as the API service** — the worker holds the same Supabase credentials and
  performs the durable writes. The value is non-secret and comes from the
  approved manifest's `supabase.project_ref`; the concrete production ref is
  operator configuration and is deliberately not in this repository. No
  validation failure ever echoes the expected ref, the observed host, or the
  URL.
- **No server secret uses the `NEXT_PUBLIC_` prefix** — enforced by
  `scripts/check_unsafe_defaults.py`, `backend/production_config.py`
  (`PUBLIC_CONTAINS_SECRET`) and the frontend bundle secret check.
- Secret values are entered only in Secret Manager (API/worker) or the
  Vercel server environment (gateway) — never in this repository, never in
  the manifest, never in CI.
- The authoritative validator is `scripts/release/check-production-config.sh
  --env-file <metadata>`; it re-derives this inventory from code on every
  run.

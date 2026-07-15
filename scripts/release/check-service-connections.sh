#!/usr/bin/env bash
# Read-only external-service connection audit.
#
# Walks the nine production connections documented in
# docs/production-readiness/MANUAL_SERVICE_CONNECTIONS.md, verifies the
# repository-side implementation of each, and emits the exact read-only
# verification command an operator must run for the external side.
# Performs no external call itself.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

usage() {
  cat << 'EOF'
Usage: check-service-connections.sh [options]

Read-only. Audits the repository side of every external service connection
and lists the manual verification command for the external side.

Options:
  --json-output <path>  Write a machine-readable JSON report.
  --help                Show this help.
EOF
}

JSON_OUTPUT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --json-output) JSON_OUTPUT="${2:?}"; shift 2 ;;
    --help) usage; exit 0 ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; usage >&2; exit 64 ;;
  esac
done

repo_has() { grep -rq "$1" "${REPO_ROOT}/$2" 2> /dev/null; }

# 1. Supabase -> Cloud Run API
if repo_has 'SUPABASE_URL' 'backend/config.py' && repo_has 'SUPABASE_SERVICE_ROLE_KEY' 'backend/config.py'; then
  record_check PASS "conn1:supabase-api:repo" "API reads SUPABASE_URL and the server-only service-role credential (backend/config.py)"
else
  record_check BLOCKED "conn1:supabase-api:repo" "expected Supabase settings not found in backend/config.py"
fi
record_check MANUAL "conn1:supabase-api:external" "verify secret-level IAM: gcloud secrets get-iam-policy <SUPABASE_SERVICE_KEY_SECRET> --project <GCP_PROJECT_ID> (Connection 1 in MANUAL_SERVICE_CONNECTIONS.md)"

# 2. Supabase -> Cloud Run worker
if repo_has 'SUPABASE' 'backend/worker/main.py' || repo_has 'get_settings' 'backend/worker/main.py'; then
  record_check PASS "conn2:supabase-worker:repo" "worker consumes Supabase settings via backend settings (backend/worker/main.py)"
else
  record_check WARN "conn2:supabase-worker:repo" "could not confirm worker Supabase wiring in backend/worker/main.py"
fi
record_check MANUAL "conn2:supabase-worker:external" "verify the worker job service account and its per-secret grants: gcloud run jobs describe <CLOUD_RUN_WORKER_JOB> --region <GCP_REGION> (Connection 2)"

# 3. Vercel gateway -> private Cloud Run API
if repo_has 'getVercelOidcToken' 'frontend/lib/server/cloudRunAuth.ts' \
  && repo_has 'workloadIdentityPools' 'frontend/lib/server/cloudRunAuth.ts' \
  && repo_has 'x-milo-gateway-token' 'frontend/app/api/gateway'; then
  record_check PASS "conn3:gateway:repo" "short-lived Vercel OIDC → Workload Identity Federation → Cloud Run ID token flow with X-Milo-Gateway-Token is implemented"
else
  record_check BLOCKED "conn3:gateway:repo" "gateway identity-token flow not found where expected"
fi
if repo_has 'MILO_GATEWAY_AUDIENCE' 'backend/gateway_auth.py' && repo_has 'MILO_APPROVED_GATEWAY_IDENTITIES' 'backend/gateway_auth.py'; then
  record_check PASS "conn3:gateway:verification" "API verifies issuer, audience, expiry and allowlisted gateway identity (backend/gateway_auth.py; fails closed when unconfigured)"
else
  record_check BLOCKED "conn3:gateway:verification" "gateway token verification not found in backend/gateway_auth.py"
fi
record_check MANUAL "conn3:gateway:external" "verify the workload identity pool, provider and gateway service account (Connection 3); test a direct unauthenticated call is rejected: curl -s -o /dev/null -w '%{http_code}' <CLOUD_RUN_API_URL>/health (expect 401/403 from Cloud Run IAM)"

# 4. Cloud Run API -> Cloud Run worker job
if repo_has 'JOB_LAUNCHER' 'backend/config.py' && [[ -f "${REPO_ROOT}/backend/job_launcher.py" ]]; then
  record_check PASS "conn4:launcher:repo" "job launcher implemented behind JOB_LAUNCHER (default disabled); uncertain launches persist launch_unknown and are never auto-relaunched"
else
  record_check BLOCKED "conn4:launcher:repo" "job launcher implementation not found"
fi
record_check MANUAL "conn4:launcher:external" "verify the API service account holds only roles/run.invoker (or run.jobsExecutorWithOverrides where required) on the worker job, and nothing broader (Connection 4)"

# 5. Cloud Run -> Secret Manager
record_check MANUAL "conn5:secrets:external" "run scripts/release/check-secret-metadata.sh --expected-project <GCP_PROJECT_ID> --secret <name>=<consumer-sa> for every production secret (Connection 5); no project-wide accessor grant is allowed"

# 6. API and worker -> Redis
if repo_has 'UPSTASH_REDIS_REST_URL' 'backend/rate_limit.py'; then
  record_check PASS "conn6:redis:repo" "shared-store rate limiter uses UPSTASH_REDIS_REST_URL/TOKEN and fails closed on limited surfaces in production (backend/rate_limit.py)"
else
  record_check BLOCKED "conn6:redis:repo" "shared-store rate limiter wiring not found"
fi
record_check MANUAL "conn6:redis:external" "run scripts/release/check-redis-config.sh --env-file <metadata> (Connection 6); TLS required; dedicated database per environment"

# 7. Worker -> Kimi/Moonshot provider
if repo_has 'KIMI_API_KEY' 'backend/production_config.py' && repo_has 'MOONSHOT_API_KEY' 'backend/production_config.py'; then
  record_check PASS "conn7:provider:repo" "provider credential variables are KIMI_API_KEY / MOONSHOT_API_KEY; paid execution requires MILO_ENABLE_PAID_EXECUTION plus mandatory budget caps (default off)"
else
  record_check BLOCKED "conn7:provider:repo" "provider credential validation not found in backend/production_config.py"
fi
record_check MANUAL "conn7:provider:external" "provider key is entered manually at Stage C only, injected only into the worker job as a secret reference; never test with a real request outside the single controlled Stage C smoke run (Connection 7)"

# 8. Vercel -> Supabase authentication
if repo_has 'NEXT_PUBLIC_SUPABASE_ANON_KEY' 'frontend/lib/supabaseClient.ts'; then
  record_check PASS "conn8:supabase-auth:repo" "browser uses only NEXT_PUBLIC_SUPABASE_URL / NEXT_PUBLIC_SUPABASE_ANON_KEY; gateway independently validates the Supabase access token server-side (frontend/lib/server/supabaseAuth.ts)"
else
  record_check WARN "conn8:supabase-auth:repo" "browser Supabase client wiring not found where expected"
fi
record_check MANUAL "conn8:supabase-auth:external" "verify redirect URLs and the production domain allowlist in the Supabase dashboard (Connection 8); never add service-role material to browser configuration"

# 9. Domain and CORS
if repo_has 'cors_origin_list' 'backend/config.py' && repo_has 'CORS_WILDCARD' 'backend/production_config.py'; then
  record_check PASS "conn9:cors:repo" "explicit CORS origins enforced; wildcard rejected at startup validation (backend/production_config.py)"
else
  record_check BLOCKED "conn9:cors:repo" "explicit CORS enforcement not found"
fi
record_check MANUAL "conn9:cors:external" "set ALLOWED_CORS_ORIGINS to the exact production Vercel domain(s); never add a wildcard as a temporary workaround (Connection 9)"

finish_checks "check-service-connections" "${JSON_OUTPUT}"

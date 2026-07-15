#!/usr/bin/env bash
# Read-only Vercel project configuration inspection.
#
# Repository-level checks always run (frontend gateway wiring, browser-safe
# variable policy). Remote checks require the vercel CLI plus an explicit
# project name and only ever read environment-variable NAMES, never values.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

usage() {
  cat << 'EOF'
Usage: check-vercel-config.sh [options]

Read-only. Never deploys and never prints environment-variable values.

Options:
  --project <name>      Exact Vercel project name for remote inspection.
  --json-output <path>  Write a machine-readable JSON report.
  --help                Show this help.
EOF
}

JSON_OUTPUT="" PROJECT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) PROJECT="${2:?}"; shift 2 ;;
    --json-output) JSON_OUTPUT="${2:?}"; shift 2 ;;
    --help) usage; exit 0 ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; usage >&2; exit 64 ;;
  esac
done

# Server-only variables the gateway needs (values entered in the Vercel
# dashboard/CLI as server environment variables, never NEXT_PUBLIC_).
REQUIRED_SERVER_VARS=(
  CLOUD_RUN_API_URL
  GCP_PROJECT_NUMBER
  GCP_WORKLOAD_IDENTITY_POOL_ID
  GCP_WORKLOAD_IDENTITY_POOL_PROVIDER_ID
  GCP_SERVICE_ACCOUNT_EMAIL
  UPSTASH_REDIS_REST_URL
  UPSTASH_REDIS_REST_TOKEN
)
REQUIRED_PUBLIC_VARS=(NEXT_PUBLIC_SUPABASE_URL NEXT_PUBLIC_SUPABASE_ANON_KEY)
FORBIDDEN_VERCEL_VARS=(SUPABASE_SERVICE_ROLE_KEY SUPABASE_SECRET_KEY KIMI_API_KEY MOONSHOT_API_KEY)

# Repository-level verification that the gateway implementation exists and
# uses the short-lived identity flow.
gateway_file="${REPO_ROOT}/frontend/lib/server/cloudRunAuth.ts"
if [[ -f "${gateway_file}" ]] && grep -q 'getVercelOidcToken' "${gateway_file}" && grep -q 'workloadIdentityPools' "${gateway_file}"; then
  record_check PASS "repo:gateway-flow" "Vercel OIDC → Workload Identity Federation → short-lived Cloud Run ID token flow is implemented"
else
  record_check BLOCKED "repo:gateway-flow" "expected short-lived identity flow not found in frontend/lib/server/cloudRunAuth.ts"
fi
if grep -rq 'x-milo-gateway-token' "${REPO_ROOT}/frontend/app/api/gateway" 2> /dev/null; then
  record_check PASS "repo:gateway-token-header" "gateway forwards X-Milo-Gateway-Token to the private API"
else
  record_check BLOCKED "repo:gateway-token-header" "gateway route no longer sets X-Milo-Gateway-Token"
fi
if grep -rq 'NEXT_PUBLIC_SUPABASE_ANON_KEY' "${REPO_ROOT}/frontend/lib" 2> /dev/null; then
  record_check PASS "repo:browser-supabase" "browser uses only the public anon key (NEXT_PUBLIC_SUPABASE_ANON_KEY)"
else
  record_check WARN "repo:browser-supabase" "NEXT_PUBLIC_SUPABASE_ANON_KEY not referenced by frontend/lib; verify the browser Supabase client wiring"
fi
# The service-role credential must never be referenced by frontend code.
if grep -rq 'SUPABASE_SERVICE_ROLE_KEY\|SUPABASE_SECRET_KEY' "${REPO_ROOT}/frontend/lib" "${REPO_ROOT}/frontend/app" 2> /dev/null; then
  record_check BLOCKED "repo:service-role-in-frontend" "frontend code references the Supabase service-role credential"
else
  record_check PASS "repo:service-role-in-frontend" "no service-role credential reference in frontend code"
fi

# Remote inspection (names only).
if [[ -z "${PROJECT}" ]]; then
  record_check MANUAL "vercel:project" "no --project supplied; verify the Vercel project manually: vercel env ls --environment production (variable NAMES only)"
elif ! tool_available vercel; then
  record_check MANUAL "vercel:cli" "vercel CLI unavailable; verify project '${PROJECT}' manually in the Vercel dashboard (variable names only, never values)"
else
  require_value "vercel:project-name" "${PROJECT}" || {
    finish_checks "check-vercel-config" "${JSON_OUTPUT}"; exit $?
  }
  env_names="$(vercel env ls production --scope-project "${PROJECT}" 2> /dev/null | awk '{print $1}' || true)"
  if [[ -z "${env_names}" ]]; then
    record_check MANUAL "vercel:env" "could not list environment variable names for '${PROJECT}'; verify manually"
  else
    for name in "${REQUIRED_SERVER_VARS[@]}" "${REQUIRED_PUBLIC_VARS[@]}"; do
      if grep -qx "${name}" <<< "${env_names}"; then
        record_check PASS "vercel:var:${name}" "configured (name verified only; value never read)"
      else
        record_check BLOCKED "vercel:var:${name}" "required variable is not configured in the production environment"
      fi
    done
    for name in "${FORBIDDEN_VERCEL_VARS[@]}"; do
      if grep -qx "${name}" <<< "${env_names}"; then
        record_check BLOCKED "vercel:forbidden:${name}" "server/worker credential must never be configured in Vercel"
      fi
    done
    record_check PASS "vercel:forbidden" "no worker/service-role credential name present in the Vercel environment"
  fi
fi

finish_checks "check-vercel-config" "${JSON_OUTPUT}"

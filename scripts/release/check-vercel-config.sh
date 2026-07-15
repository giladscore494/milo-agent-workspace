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

Remote inspection uses only supported Vercel CLI syntax:
  vercel env ls production [--scope <team>] [--token <token>]
It reads variable NAMES only (the `vercel env ls` value column is always
masked as "Encrypted"; this tool never even parses that column) and it
operates against the project LINKED in the inspected directory. Project
identity is confirmed against --project from the command's own banner so a
different project is never inspected by accident.

Options:
  --project <name>      Exact Vercel project name expected to be linked in
                        the inspected directory (required for remote checks).
  --scope <team>        Vercel team/account scope (account scope is distinct
                        from project identity; both are verified).
  --token-env <NAME>    Name of an environment variable holding a Vercel
                        access token (value never printed or logged). When
                        omitted the CLI's existing login session is used.
  --vercel-cwd <path>   Directory linked to the Vercel project (default:
                        frontend/). Must contain .vercel/project.json.
  --json-output <path>  Write a machine-readable JSON report.
  --help                Show this help.
EOF
}

JSON_OUTPUT="" PROJECT="" SCOPE="" TOKEN_ENV="" VERCEL_CWD=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) PROJECT="${2:?}"; shift 2 ;;
    --scope) SCOPE="${2:?}"; shift 2 ;;
    --token-env) TOKEN_ENV="${2:?}"; shift 2 ;;
    --vercel-cwd) VERCEL_CWD="${2:?}"; shift 2 ;;
    --json-output) JSON_OUTPUT="${2:?}"; shift 2 ;;
    --help) usage; exit 0 ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; usage >&2; exit 64 ;;
  esac
done
[[ -z "${VERCEL_CWD}" ]] && VERCEL_CWD="${REPO_ROOT}/frontend"

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

# Remote inspection (names only) using supported CLI syntax.
remote_vercel_inspection() {
  if [[ -z "${PROJECT}" ]]; then
    record_check MANUAL "vercel:project" "no --project supplied; verify the Vercel project manually: vercel env ls production (variable NAMES only)"
    return 0
  fi
  if ! tool_available vercel; then
    record_check MANUAL "vercel:cli" "vercel CLI unavailable; verify project '${PROJECT}' manually in the Vercel dashboard (variable names only, never values)"
    return 0
  fi
  require_value "vercel:project-name" "${PROJECT}" || return 0

  # Project identity comes from the linked directory, never from an invented
  # per-command flag. Refuse to inspect an unlinked directory (that would
  # silently target the wrong — or no — project).
  local link_file="${VERCEL_CWD}/.vercel/project.json"
  if [[ ! -f "${link_file}" ]]; then
    record_check BLOCKED "vercel:link" "no linked Vercel project in ${VERCEL_CWD} (.vercel/project.json missing). Prerequisite: run 'vercel link --project ${PROJECT}' in that directory first; this tool refuses to inspect an unlinked project."
    return 0
  fi

  # Resolve the token without ever printing it.
  local token=""
  if [[ -n "${TOKEN_ENV}" ]]; then
    token="${!TOKEN_ENV:-}"
    if [[ -z "${token}" ]]; then
      record_check BLOCKED "vercel:token" "--token-env ${TOKEN_ENV} is set but the variable is empty (value is never printed). Prerequisite: export a valid Vercel token in ${TOKEN_ENV}."
      return 0
    fi
  fi

  local -a base_args=()
  [[ -n "${SCOPE}" ]] && base_args+=(--scope "${SCOPE}")
  [[ -n "${token}" ]] && base_args+=(--token "${token}")

  milo_tmpdir_init
  local who_out="${_MILO_TMPDIR}/vercel-whoami"
  local who_status=0
  ( cd "${VERCEL_CWD}" && vercel whoami "${base_args[@]}" ) > "${who_out}" 2>&1 || who_status=$?
  if [[ "${who_status}" -ne 0 ]]; then
    record_check BLOCKED "vercel:auth" "Vercel authentication failed (whoami exit ${who_status}). Prerequisite: run 'vercel login' or supply a valid token via --token-env. This is NOT treated as an empty environment."
    return 0
  fi
  record_check PASS "vercel:auth" "authenticated Vercel session (account scope confirmed; identity value not printed)"

  # Supported invocation: `vercel env ls production`. Inspect the PRODUCTION
  # environment specifically. Capture the exit status and output; never
  # discard the exit status and reinterpret failure as an empty result.
  local env_out="${_MILO_TMPDIR}/vercel-env-ls"
  local env_status=0
  ( cd "${VERCEL_CWD}" && vercel env ls production "${base_args[@]}" ) > "${env_out}" 2>&1 || env_status=$?
  if [[ "${env_status}" -ne 0 ]]; then
    if grep -qiE 'not authorized|forbidden|permission|access denied' "${env_out}"; then
      record_check BLOCKED "vercel:env-list" "listing production environment variables was denied (exit ${env_status}); the token/scope lacks access to project '${PROJECT}'."
    elif grep -qiE 'not linked|no project|could not find project|link' "${env_out}"; then
      record_check BLOCKED "vercel:env-list" "Vercel reported the directory is not linked to a project (exit ${env_status}); run 'vercel link --project ${PROJECT}' first."
    else
      record_check BLOCKED "vercel:env-list" "'vercel env ls production' failed (exit ${env_status}); see the Vercel error. This tool refuses to classify a failed listing as an empty environment."
    fi
    return 0
  fi

  # Confirm project identity from the command's own banner so a different
  # project can never be inspected by accident. Modern Vercel prints a line
  # such as "> Environment Variables found in Project <name>" or
  # "found for <team>/<project>".
  local banner_project=""
  banner_project="$(grep -iE 'environment variables found' "${env_out}" \
    | sed -nE 's/.*[Pp]roject[[:space:]]+"?([A-Za-z0-9._-]+)"?.*/\1/p;s#.*found for[[:space:]]+[A-Za-z0-9._-]+/([A-Za-z0-9._-]+).*#\1#p' \
    | head -n1)"
  if [[ -n "${banner_project}" && "${banner_project}" != "${PROJECT}" ]]; then
    record_check BLOCKED "vercel:wrong-project" "'vercel env ls' reported project '${banner_project}' but --project '${PROJECT}' was expected; refusing to inspect a different project."
    return 0
  fi
  if [[ -n "${banner_project}" ]]; then
    record_check PASS "vercel:project" "linked project identity confirmed as '${PROJECT}'"
  else
    record_check WARN "vercel:project" "could not confirm the project name from CLI output; verify manually that '${VERCEL_CWD}' is linked to '${PROJECT}'"
  fi

  # Parse variable NAMES only. Env-var names are the leading column and match
  # a strict identifier shape; the value column ("Encrypted") is never parsed.
  local env_names
  env_names="$(awk '{print $1}' "${env_out}" | grep -E '^[A-Z][A-Z0-9_]*$' || true)"

  if [[ -z "${env_names}" ]]; then
    record_check WARN "vercel:env-empty" "production environment lists zero variables; every required variable below is therefore reported missing (this is an empty environment, distinct from an auth/link failure above)"
  fi

  local name
  for name in "${REQUIRED_SERVER_VARS[@]}" "${REQUIRED_PUBLIC_VARS[@]}"; do
    if grep -qx "${name}" <<< "${env_names}"; then
      record_check PASS "vercel:var:${name}" "configured (name verified only; value never read)"
    else
      record_check BLOCKED "vercel:var:${name}" "required variable is not configured in the production environment"
    fi
  done
  local forbidden_found=0
  for name in "${FORBIDDEN_VERCEL_VARS[@]}"; do
    if grep -qx "${name}" <<< "${env_names}"; then
      record_check BLOCKED "vercel:forbidden:${name}" "server/worker credential must never be configured in Vercel"
      forbidden_found=1
    fi
  done
  if [[ "${forbidden_found}" -eq 0 ]]; then
    record_check PASS "vercel:forbidden" "no worker/service-role credential name present in the Vercel environment"
  fi
}

remote_vercel_inspection

finish_checks "check-vercel-config" "${JSON_OUTPUT}"

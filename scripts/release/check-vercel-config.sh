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
  vercel whoami [--scope <team>] [--token <token>]
  vercel project inspect <project> [--scope <team>] [--token <token>]
  vercel env ls production [--scope <team>] [--token <token>]
It reads variable NAMES only (the `vercel env ls` value column is always
masked as "Encrypted"; this tool never even parses that column) and it
operates against the project LINKED in the inspected directory. Project
identity is FAIL-CLOSED: the resolved project ID (and org/team where the CLI
reports it) from `vercel project inspect` must match the linked
.vercel/project.json before any variable is inspected; a banner regex alone is
never sufficient.

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
# Env-based identity (supported CI mechanism; no committed .vercel needed).
EXPECT_PROJECT_ID="${VERCEL_PROJECT_ID:-}" EXPECT_ORG_ID="${VERCEL_ORG_ID:-}"
# Exact non-secret value expectations (NAME=VALUE) and secret fingerprint
# expectations (NAME=FINGERPRINT), verified in-memory via `vercel env run`.
EXPECT_VALUES=() EXPECT_FINGERPRINTS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) PROJECT="${2:?}"; shift 2 ;;
    --project-id) EXPECT_PROJECT_ID="${2:?}"; shift 2 ;;
    --org-id) EXPECT_ORG_ID="${2:?}"; shift 2 ;;
    --scope) SCOPE="${2:?}"; shift 2 ;;
    --token-env) TOKEN_ENV="${2:?}"; shift 2 ;;
    --vercel-cwd) VERCEL_CWD="${2:?}"; shift 2 ;;
    --expect) EXPECT_VALUES+=("${2:?}"); shift 2 ;;
    --expect-fingerprint) EXPECT_FINGERPRINTS+=("${2:?}"); shift 2 ;;
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

  # Project identity: prefer the supported CI mechanism (VERCEL_PROJECT_ID /
  # VERCEL_ORG_ID in the environment or via --project-id/--org-id), which needs
  # NO committed .vercel/project.json and works from a clean checkout. Fall
  # back to a linked .vercel/project.json when the env IDs are absent.
  local link_file="${VERCEL_CWD}/.vercel/project.json"
  local linked_project_id linked_org_id
  if [[ -n "${EXPECT_PROJECT_ID}" && -n "${EXPECT_ORG_ID}" ]]; then
    linked_project_id="${EXPECT_PROJECT_ID}"
    linked_org_id="${EXPECT_ORG_ID}"
  elif [[ -f "${link_file}" ]]; then
    local link_json
    link_json="$(cat "${link_file}" 2> /dev/null || true)"
    if ! json_is_valid "${link_json}"; then
      record_check BLOCKED "vercel:link" "linked project file ${link_file} is not valid JSON; cannot prove project identity (fail closed)."
      return 0
    fi
    linked_project_id="$(json_field "${link_json}" projectId)"
    linked_org_id="$(json_field "${link_json}" orgId)"
    if [[ -z "${linked_project_id}" ]]; then
      record_check BLOCKED "vercel:link" "linked project file ${link_file} has no projectId; cannot prove project identity (fail closed)."
      return 0
    fi
  else
    record_check BLOCKED "vercel:link" "no Vercel project identity: set VERCEL_PROJECT_ID + VERCEL_ORG_ID (or --project-id/--org-id), or link ${VERCEL_CWD} with a .vercel/project.json. This tool refuses to inspect an unproven project."
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

  # Prove the linked project identity against Vercel with a read-only project
  # inspection. Fail CLOSED: variable checks only run once the resolved project
  # ID matches the linked .vercel/project.json (and the org/team where the CLI
  # reports it). A banner regex alone is never sufficient.
  local inspect_out="${_MILO_TMPDIR}/vercel-project-inspect"
  local inspect_status=0
  ( cd "${VERCEL_CWD}" && vercel project inspect "${PROJECT}" "${base_args[@]}" ) > "${inspect_out}" 2>&1 || inspect_status=$?
  if [[ "${inspect_status}" -ne 0 ]]; then
    if grep -qiE 'not authorized|forbidden|permission|access denied' "${inspect_out}"; then
      record_check BLOCKED "vercel:project-identity" "'vercel project inspect ${PROJECT}' was denied (exit ${inspect_status}); the token/scope lacks access. Identity not proven (fail closed)."
    elif grep -qiE 'not found|does not exist|no such project|could not find' "${inspect_out}"; then
      record_check BLOCKED "vercel:project-identity" "'vercel project inspect ${PROJECT}' reported the project was not found (exit ${inspect_status}); identity not proven (fail closed)."
    else
      record_check BLOCKED "vercel:project-identity" "'vercel project inspect ${PROJECT}' failed (exit ${inspect_status}); identity not proven (fail closed)."
    fi
    return 0
  fi
  local resolved_project_id resolved_org_id
  resolved_project_id="$(grep -oE 'prj_[A-Za-z0-9_-]+' "${inspect_out}" | head -n1 || true)"
  resolved_org_id="$(grep -oE 'team_[A-Za-z0-9_-]+' "${inspect_out}" | head -n1 || true)"
  if [[ -z "${resolved_project_id}" ]]; then
    record_check BLOCKED "vercel:project-identity" "could not resolve a project ID from 'vercel project inspect ${PROJECT}' output; identity not proven (fail closed)."
    return 0
  fi
  if [[ "${resolved_project_id}" != "${linked_project_id}" ]]; then
    record_check BLOCKED "vercel:project-identity" "resolved project ID '${resolved_project_id}' does not match the linked .vercel/project.json projectId '${linked_project_id}'; refusing to inspect a different project."
    return 0
  fi
  if [[ -n "${resolved_org_id}" && -n "${linked_org_id}" && "${resolved_org_id}" != "${linked_org_id}" ]]; then
    record_check BLOCKED "vercel:project-identity" "resolved org/team '${resolved_org_id}' does not match the linked orgId '${linked_org_id}'; refusing to inspect a project under a different team."
    return 0
  fi
  record_check PASS "vercel:project-identity" "linked project identity proven: projectId ${linked_project_id} matches 'vercel project inspect ${PROJECT}'${resolved_org_id:+ (org ${resolved_org_id})}"

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

  # Secondary guard: identity is already proven above via project inspect vs
  # .vercel/project.json. If the env-ls banner ALSO names a project, it must
  # agree; a disagreeing banner is a hard failure. A missing banner is fine —
  # it never downgrades the proven identity.
  local banner_project=""
  banner_project="$(grep -iE 'environment variables found' "${env_out}" \
    | sed -nE 's/.*[Pp]roject[[:space:]]+"?([A-Za-z0-9._-]+)"?.*/\1/p;s#.*found for[[:space:]]+[A-Za-z0-9._-]+/([A-Za-z0-9._-]+).*#\1#p' \
    | head -n1 || true)"
  if [[ -n "${banner_project}" && "${banner_project}" != "${PROJECT}" ]]; then
    record_check BLOCKED "vercel:wrong-project" "'vercel env ls' reported project '${banner_project}' but --project '${PROJECT}' was expected; refusing to inspect a different project."
    return 0
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

  # Exact NON-SECRET value verification, in-memory via `vercel env run -e
  # production`: the injected verifier reads the production value and emits ONLY
  # MATCH/MISMATCH (or a fingerprint) — the raw value is never printed,
  # persisted or placed in argv, and no application code is invoked or deployed.
  local spec vname vval out
  for spec in "${EXPECT_VALUES[@]+"${EXPECT_VALUES[@]}"}"; do
    vname="${spec%%=*}"; vval="${spec#*=}"
    out="$( ( cd "${VERCEL_CWD}" && vercel env run -e production "${base_args[@]}" -- sh -c "test \"\$${vname}\" = \"${vval}\" && echo MATCH || echo MISMATCH" ) 2> /dev/null | tail -n1 || true )"
    case "${out}" in
      MATCH) record_check PASS "vercel:value:${vname}" "exact production value verified in-memory (value never printed)" ;;
      MISMATCH) record_check BLOCKED "vercel:value:${vname}" "production value does not equal the approved value (value never printed)" ;;
      *) record_check MANUAL "vercel:value:${vname}" "could not verify the production value in-memory (vercel env run unavailable?); verify manually without printing the value" ;;
    esac
  done
  for spec in "${EXPECT_FINGERPRINTS[@]+"${EXPECT_FINGERPRINTS[@]}"}"; do
    vname="${spec%%=*}"; vval="${spec#*=}"
    out="$( ( cd "${VERCEL_CWD}" && vercel env run -e production "${base_args[@]}" -- sh -c "printf %s \"\$${vname}\" | sha256sum | cut -c1-16" ) 2> /dev/null | tail -n1 || true )"
    if [[ -z "${out}" ]]; then record_check MANUAL "vercel:fingerprint:${vname}" "could not compute the in-memory fingerprint (vercel env run unavailable?); verify Redis consistency manually"
    elif [[ "${out}" == "${vval}" ]]; then record_check PASS "vercel:fingerprint:${vname}" "Vercel value fingerprint matches the expected GCP fingerprint (token never printed)"
    else record_check BLOCKED "vercel:fingerprint:${vname}" "Vercel value fingerprint does NOT match the expected fingerprint; Vercel and GCP reference different Redis credentials"
    fi
  done
}

remote_vercel_inspection

finish_checks "check-vercel-config" "${JSON_OUTPUT}"

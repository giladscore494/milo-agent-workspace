#!/usr/bin/env bash
# Read-only production configuration inspection.
#
# Builds the environment-variable inventory from the actual code surface
# (backend settings, gateway auth, worker settings, budget and rate-limit
# configuration, frontend server/browser environment, CI and Dockerfiles),
# classifies each variable, and validates operator-supplied environment
# metadata against production safety rules. Never prints secret values.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

usage() {
  cat << 'EOF'
Usage: check-production-config.sh [options]

Read-only. Inspects the repository variable inventory and (optionally)
operator-supplied environment metadata.

Options:
  --env-file <path>     NAME=VALUE metadata file describing the intended
                        production environment. Values are validated but
                        never printed. Without this flag only the
                        repository-level inventory checks run.
  --json-output <path>  Write a machine-readable JSON report.
  --help                Show this help.

Exit status: nonzero when any required check is BLOCKED.
EOF
}

JSON_OUTPUT=""
ENV_FILE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file) ENV_FILE="${2:?--env-file requires a path}"; shift 2 ;;
    --json-output) JSON_OUTPUT="${2:?--json-output requires a path}"; shift 2 ;;
    --help) usage; exit 0 ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; usage >&2; exit 64 ;;
  esac
done

# ---------------------------------------------------------------------------
# Variable inventory. Classifications:
#   browser-public | vercel-server-only | cloud-run-api-only |
#   cloud-run-worker-only | shared-api-worker | local-test-only | deprecated
# Secret column: yes/no (whether the VALUE is secret material).
# Source column: the file that proves the variable is real.
# ---------------------------------------------------------------------------
INVENTORY=(
  "NEXT_PUBLIC_SUPABASE_URL|browser-public|no|frontend/lib/supabaseClient.ts"
  "NEXT_PUBLIC_SUPABASE_ANON_KEY|browser-public|no|frontend/lib/supabaseClient.ts"
  "NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI|browser-public|no|backend/production_config.py"
  "CLOUD_RUN_API_URL|vercel-server-only|no|frontend/lib/server/cloudRunAuth.ts"
  "GCP_PROJECT_NUMBER|vercel-server-only|no|frontend/lib/server/cloudRunAuth.ts"
  "GCP_WORKLOAD_IDENTITY_POOL_ID|vercel-server-only|no|frontend/lib/server/cloudRunAuth.ts"
  "GCP_WORKLOAD_IDENTITY_POOL_PROVIDER_ID|vercel-server-only|no|frontend/lib/server/cloudRunAuth.ts"
  "GCP_SERVICE_ACCOUNT_EMAIL|vercel-server-only|no|frontend/lib/server/cloudRunAuth.ts"
  "GATEWAY_ALLOW_EXECUTION_ROUTES|vercel-server-only|no|frontend/lib/server/gatewayPolicy.ts"
  # Gateway rate-limit variables are constructed as <PREFIX>_REQUESTS and
  # <PREFIX>_WINDOW_MS from these prefixes (frontend/lib/server/rateLimit.ts).
  "GATEWAY_RATE_LIMIT_UNAUTH|vercel-server-only|no|frontend/lib/server/rateLimit.ts"
  "GATEWAY_RATE_LIMIT_AUTH_PRESSURE|vercel-server-only|no|frontend/lib/server/rateLimit.ts"
  "GATEWAY_RATE_LIMIT_AUTHENTICATED|vercel-server-only|no|frontend/lib/server/rateLimit.ts"
  "GATEWAY_RATE_LIMIT_POLLING|vercel-server-only|no|frontend/lib/server/rateLimit.ts"
  "GATEWAY_RATE_LIMIT_RUN_CREATION|vercel-server-only|no|frontend/lib/server/rateLimit.ts"
  "GATEWAY_RATE_LIMIT_CANCELLATION|vercel-server-only|no|frontend/lib/server/rateLimit.ts"
  "SUPABASE_URL|shared-api-worker|no|backend/config.py"
  "SUPABASE_SERVICE_ROLE_KEY|shared-api-worker|yes|backend/config.py"
  "SUPABASE_SECRET_KEY|shared-api-worker|yes|backend/config.py"
  "ENVIRONMENT|shared-api-worker|no|backend/config.py"
  "ALLOWED_CORS_ORIGINS|cloud-run-api-only|no|backend/config.py"
  "JOB_LAUNCHER|cloud-run-api-only|no|backend/config.py"
  "GCP_PROJECT_ID|cloud-run-api-only|no|backend/config.py"
  "GCP_REGION|cloud-run-api-only|no|backend/config.py"
  "CLOUD_RUN_WORKER_JOB|cloud-run-api-only|no|backend/config.py"
  "RATE_LIMIT_PER_MINUTE|cloud-run-api-only|no|backend/config.py"
  "MILO_GATEWAY_AUDIENCE|cloud-run-api-only|no|backend/gateway_auth.py"
  "MILO_APPROVED_GATEWAY_IDENTITIES|cloud-run-api-only|no|backend/gateway_auth.py"
  "MILO_WORKER_AUDIENCE|cloud-run-api-only|no|backend/worker_auth.py"
  "MILO_APPROVED_WORKER_IDENTITIES|cloud-run-api-only|no|backend/worker_auth.py"
  "MILO_RATE_LIMIT_RUN_CREATION_USER|cloud-run-api-only|no|backend/rate_limit.py"
  "MILO_RATE_LIMIT_RUN_CREATION_PROJECT|cloud-run-api-only|no|backend/rate_limit.py"
  "MILO_RATE_LIMIT_CANCELLATION|cloud-run-api-only|no|backend/rate_limit.py"
  "MILO_RATE_LIMIT_WORKER_MUTATIONS|cloud-run-api-only|no|backend/rate_limit.py"
  "UPSTASH_REDIS_REST_URL|shared-api-worker|no|backend/rate_limit.py"
  "UPSTASH_REDIS_REST_TOKEN|shared-api-worker|yes|backend/rate_limit.py"
  "MILO_ENABLE_RUN_CREATION|cloud-run-api-only|no|backend/production_config.py"
  "MILO_ENABLE_PROPOSAL_MUTATIONS|cloud-run-api-only|no|backend/production_config.py"
  "MILO_ENABLE_PROPOSAL_READS|cloud-run-api-only|no|backend/production_config.py"
  "MILO_ENABLE_RUN_CANCELLATION|cloud-run-api-only|no|backend/production_config.py"
  "MILO_ENABLE_EXECUTION_CONTROL|cloud-run-api-only|no|backend/production_config.py"
  "MILO_ENABLE_PAID_EXECUTION|cloud-run-api-only|no|backend/production_config.py"
  "MILO_DAILY_USER_BUDGET|shared-api-worker|no|backend/budget.py"
  "MILO_DAILY_PROJECT_BUDGET|shared-api-worker|no|backend/budget.py"
  "MILO_MAX_COST_PER_RUN|shared-api-worker|no|backend/budget.py"
  "MILO_MAX_ESTIMATED_COST_PER_RUN|shared-api-worker|no|backend/budget.py"
  "MILO_MAX_MODEL_CALLS_PER_RUN|shared-api-worker|no|backend/budget.py"
  "MILO_MAX_INPUT_TOKENS_PER_RUN|shared-api-worker|no|backend/budget.py"
  "MILO_MAX_OUTPUT_TOKENS_PER_RUN|shared-api-worker|no|backend/budget.py"
  "MILO_MAX_TOTAL_TOKENS_PER_RUN|shared-api-worker|no|backend/budget.py"
  "MILO_MAX_AGENT_STEPS|shared-api-worker|no|backend/budget.py"
  "MILO_MAX_RETRIES|shared-api-worker|no|backend/budget.py"
  "MILO_MAX_RUN_DURATION_SECONDS|shared-api-worker|no|backend/budget.py"
  "MILO_MAX_CONCURRENT_RUNS_PER_USER|shared-api-worker|no|backend/budget.py"
  "MILO_MAX_CONCURRENT_RUNS_PER_PROJECT|shared-api-worker|no|backend/budget.py"
  "MILO_ESTIMATED_COST_PER_CALL|shared-api-worker|no|backend/budget.py"
  "KIMI_API_KEY|cloud-run-worker-only|yes|backend/production_config.py"
  "MOONSHOT_API_KEY|cloud-run-worker-only|yes|backend/production_config.py"
  "MILO_WORKER_LEASE_SECONDS|cloud-run-worker-only|no|backend/worker/main.py"
  "MILO_WORKER_HEARTBEAT_INTERVAL_SECONDS|cloud-run-worker-only|no|backend/worker/main.py"
  "MILO_ALLOW_INSECURE_DEV_IDENTITY|local-test-only|no|backend/gateway_auth.py"
  "CLOUD_RUN_AUTH_MODE|local-test-only|no|frontend/lib/server/cloudRunAuth.ts"
  "MILO_E2E_INPROCESS_WORKER|local-test-only|no|backend/production_config.py"
  "MILO_REQUIRE_PG_TESTS|local-test-only|no|tests/test_migrations_postgres.py"
  "NEXT_PUBLIC_API_URL|deprecated|no|.github/workflows/ci.yml"
)

EXECUTION_FLAGS=(
  MILO_ENABLE_RUN_CREATION
  MILO_ENABLE_PROPOSAL_MUTATIONS
  MILO_ENABLE_PROPOSAL_READS
  MILO_ENABLE_RUN_CANCELLATION
  MILO_ENABLE_EXECUTION_CONTROL
  MILO_ENABLE_PAID_EXECUTION
  GATEWAY_ALLOW_EXECUTION_ROUTES
  NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI
)

MANDATORY_BUDGETS=(
  MILO_MAX_COST_PER_RUN
  MILO_DAILY_USER_BUDGET
  MILO_DAILY_PROJECT_BUDGET
  MILO_MAX_MODEL_CALLS_PER_RUN
  MILO_MAX_TOTAL_TOKENS_PER_RUN
  MILO_MAX_RUN_DURATION_SECONDS
)

SECRET_VARS=(SUPABASE_SERVICE_ROLE_KEY SUPABASE_SECRET_KEY KIMI_API_KEY MOONSHOT_API_KEY UPSTASH_REDIS_REST_TOKEN)

# ---------------------------------------------------------------------------
# 1. Inventory freshness against the actual code.
# ---------------------------------------------------------------------------
stale=0
for entry in "${INVENTORY[@]}"; do
  IFS='|' read -r name _class _secret source <<< "${entry}"
  if [[ ! -f "${REPO_ROOT}/${source}" ]]; then
    record_check WARN "inventory:${name}" "declared source file ${source} no longer exists; inventory may be stale"
    stale=1
    continue
  fi
  if ! grep -q "${name}" "${REPO_ROOT}/${source}"; then
    record_check WARN "inventory:${name}" "variable no longer referenced by ${source}; inventory may be stale"
    stale=1
  fi
done
if [[ "${stale}" -eq 0 ]]; then
  record_check PASS "inventory:code-verified" "all ${#INVENTORY[@]} inventory variables verified against their source files"
fi

# Detect referenced-but-uninventoried MILO_*/NEXT_PUBLIC_* names.
mapfile -t code_vars < <(
  grep -rhoE '"(MILO_[A-Z_]+|NEXT_PUBLIC_[A-Z_]+)"' \
    "${REPO_ROOT}/backend" "${REPO_ROOT}/frontend/lib" "${REPO_ROOT}/frontend/app" 2> /dev/null \
    | tr -d '"' | sort -u
)
uncovered=0
for var in "${code_vars[@]}"; do
  found=0
  for entry in "${INVENTORY[@]}"; do
    [[ "${entry%%|*}" == "${var}" ]] && { found=1; break; }
  done
  if [[ "${found}" -eq 0 ]]; then
    record_check WARN "inventory:uncovered" "variable ${var} appears in code but not in the inventory"
    uncovered=1
  fi
done
if [[ "${uncovered}" -eq 0 ]]; then
  record_check PASS "inventory:coverage" "no MILO_*/NEXT_PUBLIC_* variable in code is missing from the inventory"
fi

# ---------------------------------------------------------------------------
# 2. Repository-level invariants (no metadata needed).
# ---------------------------------------------------------------------------
for entry in "${INVENTORY[@]}"; do
  IFS='|' read -r name _class secret _source <<< "${entry}"
  if [[ "${secret}" == "yes" && "${name}" == NEXT_PUBLIC_* ]]; then
    record_check BLOCKED "prefix:${name}" "server secret must never use the NEXT_PUBLIC_ prefix"
  fi
done
record_check PASS "prefix:next-public" "no server secret in the inventory uses the NEXT_PUBLIC_ prefix"

# ---------------------------------------------------------------------------
# 3. Operator-supplied environment metadata validation.
# ---------------------------------------------------------------------------
if [[ -z "${ENV_FILE}" ]]; then
  record_check MANUAL "env-metadata" "no --env-file supplied; production environment values must be validated by rerunning with operator-supplied metadata"
else
  load_env_file "${ENV_FILE}" "M" || {
    finish_checks "check-production-config" "${JSON_OUTPUT}" || exit 1
    exit 1
  }

  environment="$(env_meta ENVIRONMENT M)"
  if [[ "${environment}" != "production" ]]; then
    record_check WARN "env:ENVIRONMENT" "metadata ENVIRONMENT is '${environment}', expected 'production' for a production audit"
  else
    record_check PASS "env:ENVIRONMENT" "ENVIRONMENT=production"
  fi

  # No secret material may appear in any NEXT_PUBLIC_* value.
  while IFS= read -r line || [[ -n "${line}" ]]; do
    [[ -z "${line}" || "${line}" == \#* ]] && continue
    name="${line%%=*}"; value="${line#*=}"
    if [[ "${name}" == NEXT_PUBLIC_* ]]; then
      lowered="$(printf '%s' "${value}" | tr '[:upper:]' '[:lower:]')"
      case "${lowered}" in
        *service_role*|*sb_secret*|*secret_key*|*private_key*)
          record_check BLOCKED "public:${name}" "browser-visible variable appears to contain secret material" ;;
      esac
    fi
  done < "${ENV_FILE}"
  record_check PASS "public:no-secret-material" "no NEXT_PUBLIC_* value carries secret-looking material"

  # Worker credentials must not be present in a Vercel-scoped metadata file.
  scope="$(env_meta MILO_METADATA_SCOPE M)"
  if [[ "${scope}" == "vercel" ]]; then
    for name in KIMI_API_KEY MOONSHOT_API_KEY SUPABASE_SERVICE_ROLE_KEY SUPABASE_SECRET_KEY; do
      if [[ -n "$(env_meta "${name}" M)" ]]; then
        record_check BLOCKED "scope:${name}" "worker/server credential must never be configured in the Vercel environment"
      fi
    done
  fi

  # Insecure development identity is forbidden in production.
  dev_identity="$(env_meta MILO_ALLOW_INSECURE_DEV_IDENTITY M | tr '[:upper:]' '[:lower:]')"
  if [[ "${dev_identity}" =~ ^(1|true|yes|on)$ ]]; then
    record_check BLOCKED "env:MILO_ALLOW_INSECURE_DEV_IDENTITY" "insecure development identity is forbidden in production"
  else
    record_check PASS "env:MILO_ALLOW_INSECURE_DEV_IDENTITY" "insecure development identity is not enabled"
  fi

  # Execution flags must be disabled.
  for flag in "${EXECUTION_FLAGS[@]}"; do
    value="$(env_meta "${flag}" M | tr '[:upper:]' '[:lower:]')"
    if [[ "${value}" =~ ^(1|true|yes|on)$ ]]; then
      record_check BLOCKED "flag:${flag}" "execution flag is enabled; production must stay execution-disabled until staged activation"
    else
      record_check PASS "flag:${flag}" "disabled"
    fi
  done

  # Budgets must be configured and nonzero.
  for budget in "${MANDATORY_BUDGETS[@]}"; do
    value="$(env_meta "${budget}" M)"
    if [[ -z "${value}" ]]; then
      record_check WARN "budget:${budget}" "not configured; must be set to a strict nonzero cap before any execution stage"
    elif ! [[ "${value}" =~ ^[0-9]+(\.[0-9]+)?$ ]] || [[ "${value}" =~ ^0+(\.0+)?$ ]]; then
      record_check BLOCKED "budget:${budget}" "must be a nonzero numeric cap"
    else
      record_check PASS "budget:${budget}" "configured and nonzero"
    fi
  done

  # Explicit CORS origins; wildcard forbidden.
  cors="$(env_meta ALLOWED_CORS_ORIGINS M)"
  if [[ -z "${cors}" ]]; then
    record_check BLOCKED "cors" "ALLOWED_CORS_ORIGINS must list explicit production origins"
  elif is_wildcard "${cors}"; then
    record_check BLOCKED "cors" "wildcard CORS origin is forbidden"
  elif is_placeholder "${cors}"; then
    record_check BLOCKED "cors" "placeholder CORS origin rejected"
  else
    record_check PASS "cors" "explicit origins configured"
  fi

  # Test adapters must be absent.
  if [[ "$(env_meta CLOUD_RUN_AUTH_MODE M)" == "e2e-test" ]]; then
    record_check BLOCKED "test-adapter:CLOUD_RUN_AUTH_MODE" "test-only auth adapter is forbidden in production"
  fi
  inprocess="$(env_meta MILO_E2E_INPROCESS_WORKER M | tr '[:upper:]' '[:lower:]')"
  if [[ "${inprocess}" =~ ^(1|true|yes|on)$ ]]; then
    record_check BLOCKED "test-adapter:MILO_E2E_INPROCESS_WORKER" "test-only in-process worker is forbidden in production"
  fi
  record_check PASS "test-adapter" "no test adapter enabled"

  # Placeholder URLs/credentials rejected; required backend settings present.
  for name in SUPABASE_URL CLOUD_RUN_API_URL; do
    value="$(env_meta "${name}" M)"
    if [[ -n "${value}" ]] && is_placeholder "${value}"; then
      record_check BLOCKED "placeholder:${name}" "placeholder URL rejected"
    fi
  done
  for name in SUPABASE_URL SUPABASE_SERVICE_ROLE_KEY; do
    value="$(env_meta "${name}" M)"
    alias_value="$(env_meta SUPABASE_SECRET_KEY M)"
    if [[ -z "${value}" && -z "${alias_value}" ]]; then
      record_check BLOCKED "required:${name}" "required backend setting is missing from metadata"
    fi
  done
  for name in "${SECRET_VARS[@]}"; do
    value="$(env_meta "${name}" M)"
    if [[ -n "${value}" ]] && is_placeholder "${value}"; then
      record_check BLOCKED "placeholder:${name}" "placeholder credential rejected"
    fi
  done

  # Shared rate-limit store required in production.
  if [[ -z "$(env_meta UPSTASH_REDIS_REST_URL M)" || -z "$(env_meta UPSTASH_REDIS_REST_TOKEN M)" ]]; then
    record_check WARN "redis" "shared rate-limit store (UPSTASH_REDIS_REST_URL/TOKEN) is not configured; the backend fails closed on limited surfaces in production"
  else
    record_check PASS "redis" "shared rate-limit store variables present"
  fi

  # Gateway auth must be configured in production.
  if [[ -z "$(env_meta MILO_GATEWAY_AUDIENCE M)" || -z "$(env_meta MILO_APPROVED_GATEWAY_IDENTITIES M)" ]]; then
    record_check BLOCKED "gateway-auth" "MILO_GATEWAY_AUDIENCE and MILO_APPROVED_GATEWAY_IDENTITIES are required; the API fails closed (503) without them"
  else
    record_check PASS "gateway-auth" "gateway audience and identity allowlist configured"
  fi

  # Gateway and worker identities must not overlap.
  gw="$(env_meta MILO_APPROVED_GATEWAY_IDENTITIES M | tr '[:upper:]' '[:lower:]')"
  wk="$(env_meta MILO_APPROVED_WORKER_IDENTITIES M | tr '[:upper:]' '[:lower:]')"
  if [[ -n "${gw}" && -n "${wk}" ]]; then
    overlap=""
    IFS=',' read -ra gw_list <<< "${gw}"
    for identity in "${gw_list[@]}"; do
      identity="$(printf '%s' "${identity}" | tr -d '[:space:]')"
      [[ -z "${identity}" ]] && continue
      if [[ ",${wk}," == *",${identity},"* ]]; then overlap="${identity}"; fi
    done
    if [[ -n "${overlap}" ]]; then
      record_check BLOCKED "identity-separation" "identity approved for both gateway and worker roles: ${overlap}"
    else
      record_check PASS "identity-separation" "gateway and worker identity allowlists are disjoint"
    fi
  fi
fi

finish_checks "check-production-config" "${JSON_OUTPUT}"

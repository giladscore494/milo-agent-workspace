#!/usr/bin/env bash
# Top-level read-only production readiness audit.
#
# Orchestrates every check script, aggregates their JSON reports, and
# produces a terminal summary plus a machine-readable report with exact
# blocking reasons and the manual-action list. Never performs a
# deployment, migration, or any other mutation, and never prints a secret.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

usage() {
  cat << 'EOF'
Usage: production-readiness.sh [options]

Read-only orchestrator for the complete production readiness audit.

Options:
  --env-file <path>           NAME=VALUE production environment metadata
                              (passed to check-production-config.sh and
                              check-redis-config.sh; values never printed).
  --manifest <path>           Production manifest (default:
                              config/production.example.yaml).
  --expected-project <id>     Exact GCP project for remote checks.
  --expected-account <email>  Expected operator identity.
  --region <region>           Cloud Run / Artifact Registry region.
  --repository <name>         Artifact Registry repository.
  --api-service <name>        Cloud Run API service name.
  --worker-job <name>         Cloud Run worker job name.
  --api-sa <email>            Expected API service account.
  --worker-sa <email>         Expected worker service account.
  --vercel-project <name>     Vercel project name.
  --vercel-scope <team>       Vercel team/account scope (distinct from the
                              project identity).
  --vercel-token-env <NAME>   Env var holding a Vercel token (never printed).
  --database-url-env <NAME>   Env var with a READ-ONLY DB connection for
                              migration-state and launch-unknown checks.
  --redis-expected-environment <name>
                              Logical Redis environment to assert isolation
                              against (e.g. production).
  --redis-allow-network       Permit ONE read-only Redis PING probe (off by
                              default; never used in CI).
  --json-output <path>        Write the aggregated JSON report.
  --help                      Show this help.

Every unavailable input degrades to an explicit MANUAL finding; nothing is
silently skipped. Consolidated totals equal the sum of every top-level and
nested check, and the exit code is nonzero when any consolidated finding is
BLOCKED.
EOF
}

JSON_OUTPUT="" ENV_FILE="" MANIFEST="${REPO_ROOT}/config/production.example.yaml"
EXPECTED_PROJECT="" EXPECTED_ACCOUNT="" REGION="" REPOSITORY=""
API_SERVICE="" WORKER_JOB="" API_SA="" WORKER_SA="" VERCEL_PROJECT="" DB_URL_ENV=""
VERCEL_SCOPE="" VERCEL_TOKEN_ENV="" REDIS_EXPECTED_ENVIRONMENT="" REDIS_ALLOW_NETWORK=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file) ENV_FILE="${2:?}"; shift 2 ;;
    --manifest) MANIFEST="${2:?}"; shift 2 ;;
    --expected-project) EXPECTED_PROJECT="${2:?}"; shift 2 ;;
    --expected-account) EXPECTED_ACCOUNT="${2:?}"; shift 2 ;;
    --region) REGION="${2:?}"; shift 2 ;;
    --repository) REPOSITORY="${2:?}"; shift 2 ;;
    --api-service) API_SERVICE="${2:?}"; shift 2 ;;
    --worker-job) WORKER_JOB="${2:?}"; shift 2 ;;
    --api-sa) API_SA="${2:?}"; shift 2 ;;
    --worker-sa) WORKER_SA="${2:?}"; shift 2 ;;
    --vercel-project) VERCEL_PROJECT="${2:?}"; shift 2 ;;
    --vercel-scope) VERCEL_SCOPE="${2:?}"; shift 2 ;;
    --vercel-token-env) VERCEL_TOKEN_ENV="${2:?}"; shift 2 ;;
    --database-url-env) DB_URL_ENV="${2:?}"; shift 2 ;;
    --redis-expected-environment) REDIS_EXPECTED_ENVIRONMENT="${2:?}"; shift 2 ;;
    --redis-allow-network) REDIS_ALLOW_NETWORK=1; shift ;;
    --json-output) JSON_OUTPUT="${2:?}"; shift 2 ;;
    --help) usage; exit 0 ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; usage >&2; exit 64 ;;
  esac
done

milo_tmpdir_init
tmpdir="${_MILO_TMPDIR}"

# ---------------------------------------------------------------------------
# Git and toolchain state.
# ---------------------------------------------------------------------------
if git_worktree_clean; then
  record_check PASS "git:worktree" "worktree is clean"
else
  record_check WARN "git:worktree" "worktree has uncommitted changes; audits should run from a clean checkout of the release SHA"
fi
record_check PASS "git:branch" "current branch: $(git_current_branch)"
head_sha="$(git_head_sha)"
if is_full_sha "${head_sha}"; then
  record_check PASS "git:sha" "full commit SHA: ${head_sha}"
else
  record_check BLOCKED "git:sha" "could not resolve the full commit SHA"
fi

for tool in git python3 docker gcloud vercel psql curl; do
  if tool_available "${tool}"; then
    record_check PASS "tool:${tool}" "available"
  else
    record_check MANUAL "tool:${tool}" "not available; checks depending on it degrade to MANUAL"
  fi
done

# ---------------------------------------------------------------------------
# Manifest, image-tag policy and repository invariants.
# ---------------------------------------------------------------------------
if [[ -f "${MANIFEST}" ]] && tool_available python3; then
  if python3 "${SCRIPT_DIR}/validate_production_manifest.py" --manifest "${MANIFEST}" --mode plan > /dev/null 2>&1; then
    record_check PASS "manifest:schema" "manifest schema valid: ${MANIFEST}"
  else
    record_check BLOCKED "manifest:schema" "manifest failed schema validation: ${MANIFEST}"
  fi
else
  record_check MANUAL "manifest:schema" "manifest or python3 unavailable; validate the manifest manually"
fi
record_check PASS "image-tag-policy" "deployment plans require full-SHA immutable image tags (generate-deployment-plan.sh rejects latest/prod/stable/branch tags)"

for static_check in check_migrations.py secret_scan.py check_unsafe_defaults.py; do
  if tool_available python3; then
    if (cd "${REPO_ROOT}" && python3 "scripts/${static_check}" > /dev/null 2>&1); then
      record_check PASS "static:${static_check}" "passed"
    else
      record_check BLOCKED "static:${static_check}" "failed; run scripts/${static_check} for details"
    fi
  else
    record_check MANUAL "static:${static_check}" "python3 unavailable"
  fi
done

# ---------------------------------------------------------------------------
# Sub-audits. Each produces its own JSON report; the aggregator later folds
# every nested check into the consolidated totals, so this orchestrator does
# NOT re-record a per-audit roll-up (that would double-count). A sub-audit
# that crashes before producing a report, or produces invalid JSON, becomes a
# blocking finding here.
# ---------------------------------------------------------------------------
SUBREPORTS=()
run_subaudit() { # NAME SCRIPT ARGS...
  local name="$1" script="$2"
  shift 2
  local report="${tmpdir}/sub-${name}.json"
  local log="${tmpdir}/sub-${name}.log"
  local status=0
  "${SCRIPT_DIR}/${script}" --json-output "${report}" "$@" > "${log}" 2>&1 || status=$?
  if [[ ! -f "${report}" ]]; then
    record_check BLOCKED "audit:${name}:missing-report" "sub-audit ${name} (${script}) exited ${status} without producing a JSON report; see sub-${name}.log"
    return 0
  fi
  if ! json_is_valid "$(cat "${report}")"; then
    record_check BLOCKED "audit:${name}:corrupt-report" "sub-audit ${name} (${script}) produced invalid JSON; treated as a blocking finding"
    return 0
  fi
  # Surface blocking lines from this sub-audit in the terminal.
  if [[ "${status}" -ne 0 ]]; then
    grep '\[BLOCKED\]' "${log}" | sed "s/^/    (${name}) /" || true
  fi
  SUBREPORTS+=("${name}=${report}")
}

config_args=()
[[ -n "${ENV_FILE}" ]] && config_args+=(--env-file "${ENV_FILE}")
run_subaudit "production-config" "check-production-config.sh" "${config_args[@]+"${config_args[@]}"}"

migration_args=()
[[ -n "${DB_URL_ENV}" ]] && migration_args+=(--database-url-env "${DB_URL_ENV}")
run_subaudit "migration-state" "check-migration-state.sh" "${migration_args[@]+"${migration_args[@]}"}"

run_subaudit "service-connections" "check-service-connections.sh"

gcp_args=()
[[ -n "${EXPECTED_PROJECT}" ]] && gcp_args+=(--expected-project "${EXPECTED_PROJECT}")
[[ -n "${EXPECTED_ACCOUNT}" ]] && gcp_args+=(--expected-account "${EXPECTED_ACCOUNT}")
[[ -n "${REGION}" ]] && gcp_args+=(--region "${REGION}")
[[ -n "${REPOSITORY}" ]] && gcp_args+=(--repository "${REPOSITORY}")
[[ -n "${API_SERVICE}" ]] && gcp_args+=(--api-service "${API_SERVICE}")
[[ -n "${WORKER_JOB}" ]] && gcp_args+=(--worker-job "${WORKER_JOB}")
[[ -n "${API_SA}" ]] && gcp_args+=(--api-sa "${API_SA}")
[[ -n "${WORKER_SA}" ]] && gcp_args+=(--worker-sa "${WORKER_SA}")
if [[ -n "${EXPECTED_PROJECT}" ]]; then
  run_subaudit "gcp-resources" "check-gcp-resources.sh" "${gcp_args[@]+"${gcp_args[@]}"}"

  # Derive concrete Secret Manager expectations (secret name -> intended
  # consumer service-account emails) from the manifest. The placeholder
  # template emits nothing, so Secret Manager is never falsely reported as
  # verified against a template.
  secret_args=(--expected-project "${EXPECTED_PROJECT}")
  secret_expectations=0
  if [[ -f "${MANIFEST}" ]] && tool_available python3; then
    while IFS= read -r spec; do
      [[ -z "${spec}" ]] && continue
      secret_args+=(--secret "${spec}")
      secret_expectations=$((secret_expectations + 1))
    done < <(python3 "${SCRIPT_DIR}/validate_production_manifest.py" --manifest "${MANIFEST}" --emit-secret-consumers 2> /dev/null || true)
  fi
  if [[ "${secret_expectations}" -eq 0 ]]; then
    record_check MANUAL "audit:secret-metadata" "the manifest (${MANIFEST}) supplies no concrete secret name + consumer entries (placeholder template?); a completed operator manifest is required to verify Secret Manager. NO Secret Manager verification was performed."
  else
    run_subaudit "secret-metadata" "check-secret-metadata.sh" "${secret_args[@]}"
  fi
else
  record_check MANUAL "audit:gcp-resources" "no --expected-project supplied; GCP resource, API, IAM and Secret Manager checks require the exact production project ID"
fi

vercel_args=()
[[ -n "${VERCEL_PROJECT}" ]] && vercel_args+=(--project "${VERCEL_PROJECT}")
[[ -n "${VERCEL_SCOPE}" ]] && vercel_args+=(--scope "${VERCEL_SCOPE}")
[[ -n "${VERCEL_TOKEN_ENV}" ]] && vercel_args+=(--token-env "${VERCEL_TOKEN_ENV}")
run_subaudit "vercel-config" "check-vercel-config.sh" "${vercel_args[@]+"${vercel_args[@]}"}"

redis_args=()
[[ -n "${ENV_FILE}" ]] && redis_args+=(--env-file "${ENV_FILE}")
[[ -n "${REDIS_EXPECTED_ENVIRONMENT}" ]] && redis_args+=(--expected-environment "${REDIS_EXPECTED_ENVIRONMENT}")
[[ "${REDIS_ALLOW_NETWORK}" -eq 1 ]] && redis_args+=(--allow-network)
run_subaudit "redis-config" "check-redis-config.sh" "${redis_args[@]+"${redis_args[@]}"}"

# Explicitly state the Redis posture in the final report.
redis_report="${tmpdir}/sub-redis-config.json"
if [[ -f "${redis_report}" ]] && tool_available python3; then
  redis_posture="$(python3 - "${redis_report}" << 'PY'
import json, sys
try:
    checks = {c["name"]: c["status"] for c in json.load(open(sys.argv[1])).get("checks", [])}
except Exception:
    print("unknown"); sys.exit()
iso = checks.get("redis:isolation")
probe = checks.get("redis:probe")
if iso == "BLOCKED":
    print("incorrectly shared with another environment")
elif probe == "PASS":
    print("live-probed successfully (single read-only PING)")
elif probe == "BLOCKED":
    print("inaccessible (live probe attempted and failed)")
elif probe == "NOT_APPLICABLE":
    print("statically checked only (network probe not enabled)")
else:
    print("statically checked only")
PY
)"
  record_check NOT_APPLICABLE "redis:posture" "Redis was ${redis_posture}"
fi

# ---------------------------------------------------------------------------
# Rollback prerequisites and known manual blockers.
# ---------------------------------------------------------------------------
record_check MANUAL "rollback:prerequisites" "record the previous known-good release SHA in the manifest (release.rollback_sha) and verify its images still exist in Artifact Registry before any deployment"
record_check MANUAL "manual-blockers" "review the manual-action list in docs/production-readiness/MANUAL_SERVICE_CONNECTIONS.md and STAGED_ACTIVATION.md; external service configuration is never performed by this tool"

# ---------------------------------------------------------------------------
# Consolidated aggregate. Totals equal the sum of every top-level and nested
# check; the exit code is nonzero when the consolidated blocked total > 0.
# ---------------------------------------------------------------------------
printf '\nThis audit is read-only: no deployment, no migration, no IAM change, no secret access.\n\n'

# Persist this orchestrator's own top-level checks so the aggregator can fold
# them in alongside the sub-reports.
toplevel_report="${tmpdir}/toplevel.json"
write_json_report "${toplevel_report}" "production-readiness-toplevel" > /dev/null

if ! tool_available python3; then
  # Without python3 the accurate aggregator cannot run; fall back to the
  # top-level-only summary but make the degradation explicit.
  printf 'WARNING: python3 unavailable; consolidated aggregation across sub-reports was not performed.\n'
  finish_checks "production-readiness" "${JSON_OUTPUT}"
  exit $?
fi

agg_target="${JSON_OUTPUT:-${tmpdir}/aggregate.json}"
agg_args=(--top-level "${toplevel_report}" --head-sha "${head_sha}" --branch "$(git_current_branch)" --output "${agg_target}")
for entry in "${SUBREPORTS[@]+"${SUBREPORTS[@]}"}"; do
  agg_args+=(--sub-report "${entry}")
done
agg_status=0
python3 "${SCRIPT_DIR}/aggregate_reports.py" "${agg_args[@]}" || agg_status=$?

if [[ -n "${JSON_OUTPUT}" ]]; then
  printf '\nAggregated JSON report written to %s\n' "${JSON_OUTPUT}"
fi
if [[ "${agg_status}" -ne 0 ]]; then
  printf 'RESULT: BLOCKED (consolidated blocking findings; see the list above)\n'
else
  printf 'RESULT: OK (no consolidated blocking findings)\n'
fi
exit "${agg_status}"

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
  --database-url-env <NAME>   Env var with a READ-ONLY DB connection for
                              migration-state and launch-unknown checks.
  --json-output <path>        Write the aggregated JSON report.
  --help                      Show this help.

Every unavailable input degrades to an explicit MANUAL finding; nothing is
silently skipped.
EOF
}

JSON_OUTPUT="" ENV_FILE="" MANIFEST="${REPO_ROOT}/config/production.example.yaml"
EXPECTED_PROJECT="" EXPECTED_ACCOUNT="" REGION="" REPOSITORY=""
API_SERVICE="" WORKER_JOB="" API_SA="" WORKER_SA="" VERCEL_PROJECT="" DB_URL_ENV=""
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
    --database-url-env) DB_URL_ENV="${2:?}"; shift 2 ;;
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
# Sub-audits (each produces its own JSON merged into the final report).
# ---------------------------------------------------------------------------
run_subaudit() { # NAME SCRIPT ARGS...
  local name="$1" script="$2"
  shift 2
  local report="${tmpdir}/${name}.json"
  local status=0
  "${SCRIPT_DIR}/${script}" --json-output "${report}" "$@" > "${tmpdir}/${name}.log" 2>&1 || status=$?
  local blocked manual
  blocked="$(grep -c '"status": "BLOCKED"' "${report}" 2> /dev/null || true)"
  manual="$(grep -c '"status": "MANUAL"' "${report}" 2> /dev/null || true)"
  if [[ "${status}" -eq 0 ]]; then
    record_check PASS "audit:${name}" "no blocking findings (${manual:-0} manual item(s)); details in ${name}.json"
  else
    record_check BLOCKED "audit:${name}" "${blocked:-?} blocking finding(s); details follow"
    # Surface the exact blocking reasons in the terminal summary.
    grep '\[BLOCKED\]' "${tmpdir}/${name}.log" | sed 's/^/    /' || true
  fi
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
  run_subaudit "secret-metadata" "check-secret-metadata.sh" --expected-project "${EXPECTED_PROJECT}"
else
  record_check MANUAL "audit:gcp-resources" "no --expected-project supplied; GCP resource, API, IAM and Secret Manager checks require the exact production project ID"
fi

vercel_args=()
[[ -n "${VERCEL_PROJECT}" ]] && vercel_args+=(--project "${VERCEL_PROJECT}")
run_subaudit "vercel-config" "check-vercel-config.sh" "${vercel_args[@]+"${vercel_args[@]}"}"

redis_args=()
[[ -n "${ENV_FILE}" ]] && redis_args+=(--env-file "${ENV_FILE}")
run_subaudit "redis-config" "check-redis-config.sh" "${redis_args[@]+"${redis_args[@]}"}"

if [[ -n "${DB_URL_ENV}" ]]; then
  run_subaudit "launch-unknown" "reconcile-launch-unknown.sh" --database-url-env "${DB_URL_ENV}"
else
  record_check MANUAL "audit:launch-unknown" "no --database-url-env supplied; unresolved launch_unknown records require a read-only connection or manual query"
fi

# ---------------------------------------------------------------------------
# Rollback prerequisites and known manual blockers.
# ---------------------------------------------------------------------------
record_check MANUAL "rollback:prerequisites" "record the previous known-good release SHA in the manifest (release.rollback_sha) and verify its images still exist in Artifact Registry before any deployment"
record_check MANUAL "manual-blockers" "review the manual-action list in docs/production-readiness/MANUAL_SERVICE_CONNECTIONS.md and STAGED_ACTIVATION.md; external service configuration is never performed by this tool"

# ---------------------------------------------------------------------------
# Aggregate JSON.
# ---------------------------------------------------------------------------
if [[ -n "${JSON_OUTPUT}" ]]; then
  agg="$(mktemp "${tmpdir}/aggregate.XXXXXX")"
  {
    printf '{\n  "script": "production-readiness",\n'
    printf '  "head_sha": "%s",\n' "${head_sha}"
    printf '  "branch": "%s",\n' "$(git_current_branch)"
    printf '  "sub_reports": {\n'
    first=1
    for report in "${tmpdir}"/*.json; do
      [[ -f "${report}" ]] || continue
      name="$(basename "${report}" .json)"
      [[ "${first}" -eq 0 ]] && printf ',\n'
      first=0
      printf '    "%s": ' "${name}"
      cat "${report}"
    done
    printf '\n  },\n'
    printf '  "summary": {"pass": %d, "warn": %d, "blocked": %d, "manual": %d, "not_applicable": %d}\n}\n' \
      "${_MILO_PASS_COUNT}" "${_MILO_WARN_COUNT}" "${_MILO_BLOCKED_COUNT}" "${_MILO_MANUAL_COUNT}" "${_MILO_NA_COUNT}"
  } > "${agg}"
  mv "${agg}" "${JSON_OUTPUT}"
  printf 'Aggregated JSON report written to %s\n' "${JSON_OUTPUT}"
fi

printf '\nThis audit is read-only: no deployment, no migration, no IAM change, no secret access.\n'
finish_checks "production-readiness" ""

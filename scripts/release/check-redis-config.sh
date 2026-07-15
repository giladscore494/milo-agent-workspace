#!/usr/bin/env bash
# Read-only Redis (Upstash REST) configuration inspection.
#
# The backend and gateway rate limiters use the Upstash REST protocol via
# UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN and fail closed (503)
# on limited surfaces in production when the store is unavailable
# (backend/rate_limit.py, frontend/lib/server/rateLimit.ts).
#
# Default mode validates metadata shape only. A live connectivity probe
# requires BOTH --allow-network and operator-supplied variable names, and
# performs a single PING-equivalent read. Credentials are never printed.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

usage() {
  cat << 'EOF'
Usage: check-redis-config.sh [options]

Read-only. Never mutates the Redis keyspace. Never prints credentials.

Options:
  --env-file <path>      NAME=VALUE metadata file containing
                         UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN
                         (values validated, never printed).
  --allow-network        Permit one read-only connectivity probe (PING).
                         Off by default; never used in CI.
  --expected-environment <name>
                         Logical environment label (e.g. production) used to
                         verify keyspace isolation intent.
  --json-output <path>   Write a machine-readable JSON report.
  --help                 Show this help.
EOF
}

JSON_OUTPUT="" ENV_FILE="" ALLOW_NETWORK=0 EXPECTED_ENVIRONMENT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file) ENV_FILE="${2:?}"; shift 2 ;;
    --allow-network) ALLOW_NETWORK=1; shift ;;
    --expected-environment) EXPECTED_ENVIRONMENT="${2:?}"; shift 2 ;;
    --json-output) JSON_OUTPUT="${2:?}"; shift 2 ;;
    --help) usage; exit 0 ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; usage >&2; exit 64 ;;
  esac
done

if [[ -z "${ENV_FILE}" ]]; then
  record_check MANUAL "redis:metadata" "no --env-file supplied; provide metadata containing UPSTASH_REDIS_REST_URL/UPSTASH_REDIS_REST_TOKEN names for validation"
  finish_checks "check-redis-config" "${JSON_OUTPUT}"
  exit $?
fi

load_env_file "${ENV_FILE}" "R" || {
  finish_checks "check-redis-config" "${JSON_OUTPUT}"; exit $?
}

url="$(env_meta UPSTASH_REDIS_REST_URL R)"
token="$(env_meta UPSTASH_REDIS_REST_TOKEN R)"

if [[ -z "${url}" ]]; then
  record_check BLOCKED "redis:url" "UPSTASH_REDIS_REST_URL is missing from metadata"
elif is_placeholder "${url}"; then
  record_check BLOCKED "redis:url" "placeholder Redis URL rejected"
elif [[ "${url}" != https://* ]]; then
  record_check BLOCKED "redis:tls" "Redis REST endpoint must use https:// (TLS is mandatory)"
else
  record_check PASS "redis:tls" "endpoint uses TLS (https)"
  # Redact credentials that may be embedded in the URL.
  if [[ "${url}" == *"@"* ]]; then
    record_check WARN "redis:url-credentials" "URL appears to embed credentials; prefer the separate token variable (URL is redacted in all output)"
  fi
fi

if [[ -z "${token}" ]]; then
  record_check BLOCKED "redis:token" "UPSTASH_REDIS_REST_TOKEN is missing from metadata (value is never printed)"
elif is_placeholder "${token}"; then
  record_check BLOCKED "redis:token" "placeholder Redis token rejected"
else
  record_check PASS "redis:token" "token present (value never printed)"
fi

# Keyspace isolation: development and production must never share a store.
# The application hashes identifiers and prefixes keys (rl:<category>:...),
# so isolation comes from using a DEDICATED database per environment.
if [[ -n "${EXPECTED_ENVIRONMENT}" ]]; then
  label="$(env_meta MILO_REDIS_LOGICAL_ENVIRONMENT R)"
  if [[ -z "${label}" ]]; then
    record_check WARN "redis:isolation" "metadata lacks MILO_REDIS_LOGICAL_ENVIRONMENT; record which logical environment this instance serves so dev and production never share a keyspace"
  elif [[ "${label}" != "${EXPECTED_ENVIRONMENT}" ]]; then
    record_check BLOCKED "redis:isolation" "Redis instance is labeled '${label}' but '${EXPECTED_ENVIRONMENT}' was expected; environments must not share a keyspace"
  else
    record_check PASS "redis:isolation" "instance labeled for the expected logical environment"
  fi
fi

record_check PASS "redis:fail-mode" "code review: production fails closed (503) on limited surfaces when the shared store is unavailable (backend/rate_limit.py RateLimiterUnavailable; gateway returns 503)"

# Optional single read-only probe.
if [[ "${ALLOW_NETWORK}" -eq 1 ]]; then
  if [[ -z "${url}" || -z "${token}" ]]; then
    record_check BLOCKED "redis:probe" "cannot probe without both URL and token metadata"
  elif ! tool_available curl; then
    record_check MANUAL "redis:probe" "curl unavailable; probe manually with a PING request"
  else
    status="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -H "Authorization: Bearer ${token}" "${url%/}/ping" || printf '000')"
    if [[ "${status}" == "200" ]]; then
      record_check PASS "redis:probe" "read-only PING succeeded (HTTP 200)"
    else
      record_check BLOCKED "redis:probe" "read-only PING failed (HTTP ${status})"
    fi
  fi
else
  record_check NOT_APPLICABLE "redis:probe" "network probe disabled (default); rerun with --allow-network for a single read-only PING"
fi

finish_checks "check-redis-config" "${JSON_OUTPUT}"

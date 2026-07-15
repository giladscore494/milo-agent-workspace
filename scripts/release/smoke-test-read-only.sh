#!/usr/bin/env bash
# Read-only smoke test against a deployed (or mocked) gateway.
#
# Exercises ONLY safe read operations and expected rejections. It never
# creates a run, never triggers a worker, never calls a provider, never
# mutates a project and never applies migrations. Tokens are supplied via
# environment-variable NAMES and are never printed.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

usage() {
  cat << 'EOF'
Usage: smoke-test-read-only.sh --base-url <gateway-url> [options]

Read-only. Safe reads and expected rejections only.

Options:
  --base-url <url>          Gateway base URL (e.g. the Vercel deployment,
                            path prefix /api/gateway is appended). In CI
                            this is a local mock; production runs require
                            an explicit operator-supplied URL.
  --user-token-env <NAME>   Env var holding user A's Supabase access token.
  --other-user-token-env <NAME>
                            Env var holding user B's token (cross-user
                            rejection check).
  --project-id <uuid>       A project user A can read.
  --other-project-id <uuid> A project user A must NOT be able to read.
  --conversation-id <uuid>  A conversation user A can read.
  --run-id <uuid>           A run user A can read.
  --proposal-id <uuid>      A proposal user A can read.
  --json-output <path>      Write a machine-readable JSON report.
  --help                    Show this help.

Checks without supplied identifiers are reported as MANUAL, never silently
skipped.
EOF
}

JSON_OUTPUT="" BASE_URL="" USER_TOKEN_ENV="" OTHER_TOKEN_ENV=""
PROJECT_ID="" OTHER_PROJECT_ID="" CONVERSATION_ID="" RUN_ID="" PROPOSAL_ID=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --base-url) BASE_URL="${2:?}"; shift 2 ;;
    --user-token-env) USER_TOKEN_ENV="${2:?}"; shift 2 ;;
    --other-user-token-env) OTHER_TOKEN_ENV="${2:?}"; shift 2 ;;
    --project-id) PROJECT_ID="${2:?}"; shift 2 ;;
    --other-project-id) OTHER_PROJECT_ID="${2:?}"; shift 2 ;;
    --conversation-id) CONVERSATION_ID="${2:?}"; shift 2 ;;
    --run-id) RUN_ID="${2:?}"; shift 2 ;;
    --proposal-id) PROPOSAL_ID="${2:?}"; shift 2 ;;
    --json-output) JSON_OUTPUT="${2:?}"; shift 2 ;;
    --help) usage; exit 0 ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; usage >&2; exit 64 ;;
  esac
done

if [[ -z "${BASE_URL}" ]]; then
  record_check BLOCKED "base-url" "--base-url is required (operator-supplied; never guessed)"
  finish_checks "smoke-test-read-only" "${JSON_OUTPUT}"
  exit $?
fi
if is_placeholder "${BASE_URL}"; then
  record_check BLOCKED "base-url" "placeholder base URL rejected"
  finish_checks "smoke-test-read-only" "${JSON_OUTPUT}"
  exit $?
fi
if ! require_tool curl "HTTP smoke checks"; then
  finish_checks "smoke-test-read-only" "${JSON_OUTPUT}"
  exit $?
fi

GATEWAY="${BASE_URL%/}/api/gateway"

http_code() { # METHOD PATH [TOKEN_ENV]
  local method="$1" path="$2" token_env="${3:-}"
  local -a auth=()
  if [[ -n "${token_env}" && -n "${!token_env:-}" ]]; then
    auth=(-H "Authorization: Bearer ${!token_env}")
  fi
  curl -s -o /dev/null -w '%{http_code}' --max-time 20 -X "${method}" "${auth[@]}" "${GATEWAY}${path}" || printf '000'
}

expect() { # NAME METHOD PATH TOKEN_ENV EXPECTED_CODES DESCRIPTION
  local name="$1" method="$2" path="$3" token_env="$4" expected="$5" desc="$6"
  local code
  code="$(http_code "${method}" "${path}" "${token_env}")"
  if [[ ",${expected}," == *",${code},"* ]]; then
    record_check PASS "${name}" "${desc} (HTTP ${code})"
  else
    record_check BLOCKED "${name}" "${desc}: expected HTTP ${expected}, got ${code}"
  fi
}

# 1. Health endpoint (unauthenticated read).
expect "health" GET "/health" "" "200" "health endpoint responds"

# 2. Authenticated identity flow + project listing.
if [[ -n "${USER_TOKEN_ENV}" && -n "${!USER_TOKEN_ENV:-}" ]]; then
  expect "auth:projects" GET "/projects" "${USER_TOKEN_ENV}" "200" "authenticated project listing succeeds"
  expect "auth:required" GET "/projects" "" "401,403" "unauthenticated project listing is rejected"
else
  record_check MANUAL "auth" "supply --user-token-env to verify the authenticated identity flow and project listing"
fi

# 3. Project membership authorization + resource reads.
if [[ -n "${PROJECT_ID}" && -n "${USER_TOKEN_ENV}" ]]; then
  is_uuid "${PROJECT_ID}" || { record_check BLOCKED "project-id" "malformed project id"; finish_checks "smoke-test-read-only" "${JSON_OUTPUT}"; exit $?; }
  expect "read:project" GET "/projects/${PROJECT_ID}/conversations" "${USER_TOKEN_ENV}" "200" "member project conversation read succeeds"
else
  record_check MANUAL "read:project" "supply --project-id and --user-token-env to verify membership-authorized reads"
fi
if [[ -n "${CONVERSATION_ID}" && -n "${USER_TOKEN_ENV}" ]]; then
  expect "read:conversation" GET "/conversations/${CONVERSATION_ID}" "${USER_TOKEN_ENV}" "200" "conversation read succeeds"
else
  record_check MANUAL "read:conversation" "supply --conversation-id to verify conversation reads"
fi
if [[ -n "${RUN_ID}" && -n "${USER_TOKEN_ENV}" ]]; then
  expect "read:run" GET "/runs/${RUN_ID}" "${USER_TOKEN_ENV}" "200" "run read succeeds"
  expect "read:events" GET "/runs/${RUN_ID}/events" "${USER_TOKEN_ENV}" "200" "event polling succeeds"
else
  record_check MANUAL "read:run" "supply --run-id to verify run reads and event polling"
fi
if [[ -n "${PROPOSAL_ID}" && -n "${USER_TOKEN_ENV}" ]]; then
  expect "read:proposal" GET "/workflow-proposals/${PROPOSAL_ID}" "${USER_TOKEN_ENV}" "200,403" "proposal read responds per configured staged flags"
else
  record_check MANUAL "read:proposal" "supply --proposal-id to verify proposal reads"
fi

# 4. Cross-user access rejection.
if [[ -n "${OTHER_PROJECT_ID}" && -n "${USER_TOKEN_ENV}" ]]; then
  expect "cross-user" GET "/projects/${OTHER_PROJECT_ID}/conversations" "${USER_TOKEN_ENV}" "403,404" "cross-user project read is rejected"
elif [[ -n "${OTHER_TOKEN_ENV}" && -n "${PROJECT_ID}" ]]; then
  expect "cross-user" GET "/projects/${PROJECT_ID}/conversations" "${OTHER_TOKEN_ENV}" "403,404" "other user's read of this project is rejected"
else
  record_check MANUAL "cross-user" "supply --other-project-id (or --other-user-token-env) to verify cross-user rejection"
fi

# 5. Worker-route rejection from a browser identity: the gateway policy
# must refuse to proxy internal worker routes at all.
expect "worker-route" POST "/internal/runs/00000000-0000-0000-0000-000000000000/heartbeat" "${USER_TOKEN_ENV}" "401,403,404" "worker route is not reachable through the browser gateway"

printf '\nThis smoke test performed only reads and expected rejections: no run creation, no worker trigger, no provider call, no project mutation, no migration.\n'
finish_checks "smoke-test-read-only" "${JSON_OUTPUT}"

#!/usr/bin/env bash
# Execution-disabled smoke test.
#
# Proves that a production-like deployment remains safe while execution is
# disabled: run creation is blocked at the gateway, no worker job is
# invoked, no provider call happens, no budget reservation is created and
# no secret is returned. Uses mock/fake endpoints in CI; a real
# production-mode run requires explicit operator-supplied URLs/identities.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

usage() {
  cat << 'EOF'
Usage: smoke-test-execution-disabled.sh --base-url <gateway-url> [options]

Verifies the execution-disabled posture. The run-creation attempt uses a
syntactically valid but intentionally rejected request: the gateway safety
policy must refuse it BEFORE any backend, worker, provider, or budget
side effect can occur.

A real run-creation posture assertion requires an AUTHENTICATED test user
(--user-token-env with a populated variable) and a test conversation owned
by that user (--conversation-id). A bare 401 without a supplied token does
NOT prove that execution routes are disabled for authenticated users, so it
is never accepted as a PASS.

Options:
  --base-url <url>            Gateway base URL (mock in CI; explicit
                              operator-supplied URL in production mode).
  --env-file <path>           NAME=VALUE metadata proving flag posture
                              (validated with check-production-config.sh
                              semantics for execution flags).
  --user-token-env <NAME>     Env var holding a VALID authenticated test user
                              token (value never printed). Required to assert
                              the run-creation posture.
  --conversation-id <uuid>    Test conversation owned by the test user, used
                              to build a schema-valid run-creation request.
                              Required to assert the run-creation posture.
  --run-id <uuid>             Existing run id for the idempotent-cancellation
                              probe (only meaningful where cancellation is
                              enabled by the staged state; otherwise the
                              rejection itself is the assertion).
  --database-url-env <NAME>   READ-ONLY connection for the no-new-reservation
                              assertion (optional).
  --json-output <path>        Write a machine-readable JSON report.
  --help                      Show this help.
EOF
}

JSON_OUTPUT="" BASE_URL="" ENV_FILE="" USER_TOKEN_ENV="" CONVERSATION_ID="" RUN_ID="" DB_URL_ENV=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --base-url) BASE_URL="${2:?}"; shift 2 ;;
    --env-file) ENV_FILE="${2:?}"; shift 2 ;;
    --user-token-env) USER_TOKEN_ENV="${2:?}"; shift 2 ;;
    --conversation-id) CONVERSATION_ID="${2:?}"; shift 2 ;;
    --run-id) RUN_ID="${2:?}"; shift 2 ;;
    --database-url-env) DB_URL_ENV="${2:?}"; shift 2 ;;
    --json-output) JSON_OUTPUT="${2:?}"; shift 2 ;;
    --help) usage; exit 0 ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; usage >&2; exit 64 ;;
  esac
done

# 1. Flag posture from metadata.
if [[ -n "${ENV_FILE}" ]]; then
  load_env_file "${ENV_FILE}" "X" || {
    finish_checks "smoke-test-execution-disabled" "${JSON_OUTPUT}"; exit $?
  }
  for flag in MILO_ENABLE_PAID_EXECUTION MILO_ENABLE_RUN_CREATION GATEWAY_ALLOW_EXECUTION_ROUTES; do
    value="$(env_meta "${flag}" X | tr '[:upper:]' '[:lower:]')"
    if [[ "${value}" =~ ^(1|true|yes|on)$ ]]; then
      record_check BLOCKED "flag:${flag}" "must be off for the execution-disabled posture"
    else
      record_check PASS "flag:${flag}" "off"
    fi
  done
else
  record_check MANUAL "flags" "supply --env-file to prove the paid-execution and run-creation flags are off in the deployed configuration"
fi

if [[ -z "${BASE_URL}" ]]; then
  record_check MANUAL "http" "no --base-url supplied; HTTP posture checks require an explicit operator-supplied gateway URL (mock allowed in CI)"
  finish_checks "smoke-test-execution-disabled" "${JSON_OUTPUT}"
  exit $?
fi
if is_placeholder "${BASE_URL}"; then
  record_check BLOCKED "base-url" "placeholder base URL rejected"
  finish_checks "smoke-test-execution-disabled" "${JSON_OUTPUT}"
  exit $?
fi
if ! require_tool curl "HTTP posture checks"; then
  finish_checks "smoke-test-execution-disabled" "${JSON_OUTPUT}"
  exit $?
fi

GATEWAY="${BASE_URL%/}/api/gateway"

req_code() { # METHOD PATH [BODY] [BODY_OUT]
  local method="$1" path="$2" body="${3:-}" out="${4:-/dev/null}"
  local -a args=(-s -o "${out}" -w '%{http_code}' --max-time 20 -X "${method}")
  if [[ -n "${USER_TOKEN_ENV}" && -n "${!USER_TOKEN_ENV:-}" ]]; then
    args+=(-H "Authorization: Bearer ${!USER_TOKEN_ENV}")
  fi
  if [[ -n "${body}" ]]; then
    args+=(-H 'content-type: application/json' -d "${body}")
  fi
  curl "${args[@]}" "${GATEWAY}${path}" || printf '000'
}

# 2. Run creation must be blocked for an AUTHENTICATED user.
#
# The proof only holds if we present a valid authenticated test user AND a
# schema-valid run-creation request against a conversation that user owns,
# and then observe the execution-disabled response (HTTP 403 carrying the
# application's execution-disabled classification). A 401 (auth failure) or a
# missing token proves nothing about the posture for authenticated users, and
# a 2xx means execution is NOT disabled.
milo_tmpdir_init
if [[ -z "${USER_TOKEN_ENV}" || -z "${!USER_TOKEN_ENV:-}" ]]; then
  record_check MANUAL "run-creation-blocked" "no populated --user-token-env supplied; the authenticated run-creation posture cannot be proven (a bare 401 is not proof). Provide a valid authenticated test user token to assert PASS."
elif [[ -z "${CONVERSATION_ID}" ]]; then
  record_check MANUAL "run-creation-blocked" "no --conversation-id supplied; a schema-valid run-creation request requires a test conversation owned by the test user. Cannot assert PASS."
elif ! is_uuid "${CONVERSATION_ID}"; then
  record_check BLOCKED "run-creation-blocked" "malformed --conversation-id (must be a UUID); refusing to assert the run-creation posture from an invalid request"
else
  body_file="${_MILO_TMPDIR}/run-creation-body"
  # Schema-valid RunCreate body: content (min length 1) + idempotency_key
  # (8..128 chars). metadata defaults to {}. The request is intentionally
  # well-formed so a rejection can only come from the execution-disabled
  # policy, never from input validation.
  create_body='{"content":"execution-disabled smoke probe","idempotency_key":"smoke-disabled-0001"}'
  code="$(req_code POST "/conversations/${CONVERSATION_ID}/runs" "${create_body}" "${body_file}")"
  body_text="$(cat "${body_file}" 2> /dev/null || true)"
  if [[ "${code}" == "403" ]]; then
    # Require the execution-disabled classification, not just any 403.
    if grep -qiE 'EXECUTION_SURFACE_DISABLED|disabled by the gateway safety policy|run creation is disabled|is disabled' <<< "${body_text}"; then
      record_check PASS "run-creation-blocked" "authenticated run creation was refused with HTTP 403 and the expected execution-disabled classification; no worker, provider or budget side effect can occur"
    else
      record_check BLOCKED "run-creation-blocked" "authenticated run creation returned HTTP 403 but the body did not carry the expected execution-disabled classification (EXECUTION_SURFACE_DISABLED / gateway safety policy); posture not proven"
    fi
  elif [[ "${code}" == "401" ]]; then
    record_check BLOCKED "run-creation-blocked" "authentication failed (HTTP 401) with the supplied token; a generic auth failure does NOT prove the execution-disabled posture. Supply a valid authenticated test user token."
  elif [[ "${code}" == "200" || "${code}" == "201" || "${code}" == "202" ]]; then
    record_check BLOCKED "run-creation-blocked" "authenticated run creation SUCCEEDED (HTTP ${code}); execution is NOT disabled — a run may have been created. This is a critical posture failure."
  else
    record_check BLOCKED "run-creation-blocked" "unexpected response to authenticated run creation (HTTP ${code}); the execution-disabled posture is not proven"
  fi
fi

# 3. Read-only UI backing routes must remain functional.
code="$(req_code GET "/health")"
if [[ "${code}" == "200" ]]; then
  record_check PASS "read-only-functional" "health read remains functional (HTTP 200)"
else
  record_check BLOCKED "read-only-functional" "health read failed (HTTP ${code})"
fi

# 4. No secret is returned by the health surface.
if tool_available curl; then
  milo_tmpdir_init
  body_file="${_MILO_TMPDIR}/health-body"
  curl -s --max-time 20 "${GATEWAY}/health" -o "${body_file}" || true
  if grep -qiE 'service_role|api[_-]?key|bearer|password|secret' "${body_file}" 2> /dev/null; then
    record_check BLOCKED "no-secret-returned" "health response contains secret-looking material"
  else
    record_check PASS "no-secret-returned" "health response contains no secret-looking material"
  fi
fi

# 5. Cancellation idempotency (only where the staged state enables it).
if [[ -n "${RUN_ID}" ]]; then
  if ! is_uuid "${RUN_ID}"; then
    record_check BLOCKED "cancel:run-id" "malformed run id"
  else
    c1="$(req_code POST "/runs/${RUN_ID}/cancel")"
    c2="$(req_code POST "/runs/${RUN_ID}/cancel")"
    if [[ "${c1}" == "403" && "${c2}" == "403" ]]; then
      record_check PASS "cancel:staged-off" "cancellation is refused while the staged gateway state keeps execution routes off (HTTP 403, stable across retries)"
    elif [[ "${c1}" == "${c2}" && ( "${c1}" == "200" || "${c1}" == "202" ) ]]; then
      record_check PASS "cancel:idempotent" "cancellation is idempotent (HTTP ${c1} on repeat)"
    else
      record_check BLOCKED "cancel" "unexpected cancellation behavior (first HTTP ${c1}, repeat HTTP ${c2})"
    fi
  fi
else
  record_check MANUAL "cancel" "supply --run-id to probe cancellation idempotency where the staged state applies"
fi

# 6. No new budget reservation (optional read-only DB assertion).
if [[ -n "${DB_URL_ENV}" && -n "${!DB_URL_ENV:-}" ]] && tool_available psql; then
  count="$(psql -X -A -t -v ON_ERROR_STOP=1 "${!DB_URL_ENV}" -c "select count(*) from public.model_call_budget_reservations where created_at > now() - interval '10 minutes'" 2> /dev/null || printf 'ERR')"
  if [[ "${count}" == "0" ]]; then
    record_check PASS "no-budget-reservation" "no model-call budget reservation was created in the last 10 minutes"
  elif [[ "${count}" == "ERR" ]]; then
    record_check MANUAL "no-budget-reservation" "could not query reservations read-only; verify manually"
  else
    record_check BLOCKED "no-budget-reservation" "${count} recent reservation(s) found while execution is supposed to be disabled"
  fi
else
  record_check MANUAL "no-budget-reservation" "supply --database-url-env (read-only) to assert no budget reservation was created"
fi

record_check MANUAL "no-worker-invocation" "verify externally that no Cloud Run worker job execution occurred: gcloud run jobs executions list --job <CLOUD_RUN_WORKER_JOB> --region <GCP_REGION> (expect no new executions)"

printf '\nRun-creation posture is PASS only when an authenticated user + test conversation produced an HTTP 403 with the execution-disabled classification; otherwise it is reported MANUAL/BLOCKED above. Reads-functional, no-secret-returned and no-budget-reservation are asserted independently; worker-job stillness is verified via the listed manual command.\n'
finish_checks "smoke-test-execution-disabled" "${JSON_OUTPUT}"

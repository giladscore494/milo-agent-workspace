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
by that user (--conversation-id). Because the gateway refuses run creation
BEFORE it validates the token, the script FIRST performs an authenticated
read (GET /conversations/<id>) and requires HTTP 200 to prove token validity
and ownership; only then does it send the run-creation request. A bare 401
(or any non-200 read) is never accepted as a PASS.

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

# http_probe METHOD PATH BODY_OUT [BODY]
# Sets PROBE_CODE (HTTP status, "000" on transport failure) and PROBE_CURL_RC
# (curl's own exit status) so callers can distinguish a transport/network
# failure from an HTTP error. The response body is written to BODY_OUT.
PROBE_CODE="" PROBE_CURL_RC=0
http_probe() {
  local method="$1" path="$2" out="$3" body="${4:-}"
  local -a args=(-s -o "${out}" -w '%{http_code}' --max-time 20 -X "${method}")
  if [[ -n "${USER_TOKEN_ENV}" && -n "${!USER_TOKEN_ENV:-}" ]]; then
    args+=(-H "Authorization: Bearer ${!USER_TOKEN_ENV}")
  fi
  if [[ -n "${body}" ]]; then
    args+=(-H 'content-type: application/json' -d "${body}")
  fi
  : > "${out}"
  PROBE_CURL_RC=0
  PROBE_CODE="$(curl "${args[@]}" "${GATEWAY}${path}")" || PROBE_CURL_RC=$?
  if [[ -z "${PROBE_CODE}" ]]; then
    PROBE_CODE="000"
  fi
  return 0
}

# 2. Run creation must be blocked for an AUTHENTICATED, OWNING user.
#
# The gateway refuses run creation with HTTP 403 BEFORE it validates the
# Supabase token, so a random non-empty token would also receive that 403.
# To make the run-creation probe meaningful we FIRST prove authentication and
# ownership with a read the gateway only answers 200 for a valid token that
# owns the conversation:
#
#     GET /api/gateway/conversations/<conversation-id>  (expect 200)
#
# Only after that authenticated read succeeds do we send the schema-valid
# run-creation request and require the execution-disabled 403.
milo_tmpdir_init
run_creation_precondition_met=0
if [[ -z "${USER_TOKEN_ENV}" || -z "${!USER_TOKEN_ENV:-}" ]]; then
  record_check MANUAL "run-creation-blocked" "no populated --user-token-env supplied; the authenticated run-creation posture cannot be proven (a bare 401 is not proof). Provide a valid authenticated test user token to assert PASS."
elif [[ -z "${CONVERSATION_ID}" ]]; then
  record_check MANUAL "run-creation-blocked" "no --conversation-id supplied; a schema-valid run-creation request requires a test conversation owned by the test user. Cannot assert PASS."
elif ! is_uuid "${CONVERSATION_ID}"; then
  record_check BLOCKED "run-creation-blocked" "malformed --conversation-id (must be a UUID); refusing to assert the run-creation posture from an invalid request (no HTTP request sent)"
else
  # 2a. Authenticated ownership/read precondition.
  read_body="${_MILO_TMPDIR}/conversation-read-body"
  http_probe GET "/conversations/${CONVERSATION_ID}" "${read_body}"
  if [[ "${PROBE_CURL_RC}" -ne 0 ]]; then
    record_check BLOCKED "auth-precondition" "the authenticated conversation read could not be performed (curl transport error); authentication/ownership not proven, run creation not attempted"
  elif [[ "${PROBE_CODE}" == "200" ]]; then
    record_check PASS "auth-precondition" "authenticated conversation read returned HTTP 200 for the supplied token and conversation; token validity and ownership are proven before the run-creation probe"
    run_creation_precondition_met=1
  elif [[ "${PROBE_CODE}" == "401" ]]; then
    record_check BLOCKED "auth-precondition" "authenticated conversation read returned HTTP 401; the supplied token is invalid or expired. Run creation NOT attempted."
  elif [[ "${PROBE_CODE}" == "403" || "${PROBE_CODE}" == "404" ]]; then
    record_check BLOCKED "auth-precondition" "authenticated conversation read returned HTTP ${PROBE_CODE}; the conversation is not owned by / accessible to the test user. Run creation NOT attempted."
  else
    record_check BLOCKED "auth-precondition" "authenticated conversation read returned HTTP ${PROBE_CODE}; the authentication/ownership prerequisite is not proven. Run creation NOT attempted."
  fi

  # 2b. Run-creation-disabled probe (only after the read proved auth+ownership).
  if [[ "${run_creation_precondition_met}" -eq 1 ]]; then
    body_file="${_MILO_TMPDIR}/run-creation-body"
    # Schema-valid RunCreate body: content (min length 1) + idempotency_key
    # (8..128 chars). metadata defaults to {}. The request is intentionally
    # well-formed so a rejection can only come from the execution-disabled
    # policy, never from input validation.
    create_body='{"content":"execution-disabled smoke probe","idempotency_key":"smoke-disabled-0001"}'
    http_probe POST "/conversations/${CONVERSATION_ID}/runs" "${body_file}" "${create_body}"
    code="${PROBE_CODE}"
    body_text="$(cat "${body_file}" 2> /dev/null || true)"
    if [[ "${code}" == "403" ]]; then
      # Require the execution-disabled classification, not just any 403.
      if grep -qiE 'EXECUTION_SURFACE_DISABLED|disabled by the gateway safety policy|run creation is disabled|is disabled' <<< "${body_text}"; then
        record_check PASS "run-creation-blocked" "authenticated (proven) run creation was refused with HTTP 403 and the expected execution-disabled classification; no worker, provider or budget side effect can occur"
      else
        record_check BLOCKED "run-creation-blocked" "run creation returned HTTP 403 but the body did not carry the expected execution-disabled classification (EXECUTION_SURFACE_DISABLED / gateway safety policy); a generic 403 is not sufficient"
      fi
    elif [[ "${code}" == "401" ]]; then
      record_check BLOCKED "run-creation-blocked" "run creation returned HTTP 401 even though the conversation read authenticated; posture not proven"
    elif [[ "${code}" == "200" || "${code}" == "201" || "${code}" == "202" ]]; then
      record_check BLOCKED "run-creation-blocked" "authenticated run creation SUCCEEDED (HTTP ${code}); execution is NOT disabled — a run may have been created. This is a critical posture failure."
    else
      record_check BLOCKED "run-creation-blocked" "unexpected response to authenticated run creation (HTTP ${code}); the execution-disabled posture is not proven"
    fi
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
#
# A PASS here must mean "we actually received a 200 health body and it was
# clean" — never "the request failed / returned nothing, so we found no
# secret". Capture the curl exit status and HTTP status explicitly.
health_body="${_MILO_TMPDIR}/health-body"
http_probe GET "/health" "${health_body}"
if [[ "${PROBE_CURL_RC}" -ne 0 ]]; then
  record_check BLOCKED "no-secret-returned" "the health request failed (curl transport error); cannot assert the response is secret-free"
elif [[ "${PROBE_CODE}" != "200" ]]; then
  record_check BLOCKED "no-secret-returned" "health returned HTTP ${PROBE_CODE}; cannot assert a clean secret-free body from a non-200 response"
elif [[ ! -s "${health_body}" ]]; then
  record_check BLOCKED "no-secret-returned" "health returned HTTP 200 but an empty body; cannot assert it is secret-free"
elif grep -qiE 'service_role|api[_-]?key|bearer|password|secret' "${health_body}"; then
  record_check BLOCKED "no-secret-returned" "health response contains secret-looking material"
else
  record_check PASS "no-secret-returned" "health response is HTTP 200 with a non-empty, secret-free body"
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

printf '\nRun-creation posture is PASS only when an authenticated conversation read returned HTTP 200 (proving token validity + ownership) AND the subsequent run-creation request returned HTTP 403 with the execution-disabled classification; otherwise it is reported MANUAL/BLOCKED above. Reads-functional, no-secret-returned (requires a real 200 body) and no-budget-reservation are asserted independently; worker-job stillness is verified via the listed manual command.\n'
finish_checks "smoke-test-execution-disabled" "${JSON_OUTPUT}"

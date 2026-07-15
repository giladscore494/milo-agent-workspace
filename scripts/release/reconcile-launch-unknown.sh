#!/usr/bin/env bash
# Safe operator tooling for launch_unknown reconciliation.
#
# Default mode LISTS unresolved launch_unknown runs only (safe identifiers
# and classification, never raw provider responses) and generates the four
# suggested operator command templates per run:
#   1. mark confirmed launched
#   2. mark confirmed not launched (eligible for manual requeue)
#   3. requeue after operator verification
#   4. leave unresolved
#
# A run is NEVER relaunched merely because the original response was
# uncertain. Every mutation requires the full protected apply mode and is
# idempotent (guarded by the current launch_state).

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

usage() {
  cat << 'EOF'
Usage: reconcile-launch-unknown.sh [options]

Default: list-only. Displays safe identifiers (run id, created_at, attempt,
status, launch_state) and suggested command templates. Raw provider
responses are never exposed and no run is ever automatically relaunched.

Options:
  --database-url-env <NAME>       Environment variable holding a READ-ONLY
                                  connection string for listing. Never
                                  accepted on the command line.
  --run-id <uuid>                 Scope the plan to one run.
  --resolution <kind>             One of: confirmed-launched,
                                  confirmed-not-launched, requeue,
                                  leave-unresolved. Required with --apply.
  --json-output <path>            Write a machine-readable JSON report.

Apply mode (mutation; requires ALL of the following and is NOT used in CI):
  --apply
  --environment production
  --expected-project <exact-project-id>
  --expected-account <exact-operator-identity>
  --expected-sha <full-commit-sha>
  --confirm-production-change
  plus environment variable MILO_OPERATOR_ACK=I_UNDERSTAND_THIS_CHANGES_PRODUCTION
  and --database-url-env pointing at a connection authorized for the update.

  --audit-file <path>             Where to append the secret-free audit
                                  record (default: ./launch-reconciliation-audit.log)
  --help                          Show this help.
EOF
}

JSON_OUTPUT="" DB_URL_ENV="" RUN_ID="" RESOLUTION="" AUDIT_FILE="launch-reconciliation-audit.log"
APPLY_MODE=0 APPLY_ENVIRONMENT="" EXPECTED_PROJECT="" EXPECTED_ACCOUNT="" EXPECTED_SHA="" CONFIRM_PRODUCTION_CHANGE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --database-url-env) DB_URL_ENV="${2:?}"; shift 2 ;;
    --run-id) RUN_ID="${2:?}"; shift 2 ;;
    --resolution) RESOLUTION="${2:?}"; shift 2 ;;
    --json-output) JSON_OUTPUT="${2:?}"; shift 2 ;;
    --apply) APPLY_MODE=1; shift ;;
    --environment) APPLY_ENVIRONMENT="${2:?}"; shift 2 ;;
    --expected-project) EXPECTED_PROJECT="${2:?}"; shift 2 ;;
    --expected-account) EXPECTED_ACCOUNT="${2:?}"; shift 2 ;;
    --expected-sha) EXPECTED_SHA="${2:?}"; shift 2 ;;
    --confirm-production-change) CONFIRM_PRODUCTION_CHANGE=1; shift ;;
    --audit-file) AUDIT_FILE="${2:?}"; shift 2 ;;
    --help) usage; exit 0 ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; usage >&2; exit 64 ;;
  esac
done

if [[ -n "${RUN_ID}" ]] && ! is_uuid "${RUN_ID}"; then
  record_check BLOCKED "run-id" "malformed run id (must be a UUID); identifiers are never invented"
  finish_checks "reconcile-launch-unknown" "${JSON_OUTPUT}"
  exit $?
fi

# ---------------------------------------------------------------------------
# Listing (read-only).
# ---------------------------------------------------------------------------
if [[ -z "${DB_URL_ENV}" ]]; then
  record_check MANUAL "list" "no --database-url-env supplied; list unresolved runs manually with: select id, created_at, attempt, status, launch_state from public.runs where launch_state = 'launch_unknown' order by created_at;"
elif ! tool_available psql; then
  record_check MANUAL "list" "psql unavailable; run the listing query manually (read-only)"
else
  db_url="${!DB_URL_ENV:-}"
  if [[ -z "${db_url}" ]]; then
    record_check BLOCKED "list:connection" "environment variable ${DB_URL_ENV} is empty"
  else
    where="launch_state = 'launch_unknown'"
    if [[ -n "${RUN_ID}" ]]; then
      where="${where} and id = '${RUN_ID}'::uuid"
    fi
    rows="$(psql -X -A -t -v ON_ERROR_STOP=1 "${db_url}" \
      -c "select id || ' | ' || created_at || ' | attempt=' || attempt || ' | status=' || status from public.runs where ${where} order by created_at" 2> /dev/null || printf 'CONNECTION_FAILED')"
    if [[ "${rows}" == "CONNECTION_FAILED" ]]; then
      record_check BLOCKED "list:connection" "unable to connect via ${DB_URL_ENV} (connection string never printed)"
    elif [[ -z "${rows}" ]]; then
      record_check PASS "list" "no unresolved launch_unknown runs found"
    else
      count="$(wc -l <<< "${rows}" | tr -d ' ')"
      record_check WARN "list" "${count} unresolved launch_unknown run(s) require operator review"
      printf '\nUnresolved launch_unknown runs (safe identifiers only):\n%s\n' "${rows}"
    fi
  fi
fi

# ---------------------------------------------------------------------------
# Suggested command templates (always printed; placeholders when no run id).
# ---------------------------------------------------------------------------
rid="${RUN_ID:-<RUN_UUID>}"
cat << EOF

Suggested operator resolutions for run ${rid} (verify against provider/Cloud
Run job execution logs FIRST; the original uncertain response is never a
reason to relaunch):

1. Mark confirmed launched (operator verified an execution actually started):
   $0 --run-id ${rid} --resolution confirmed-launched --apply --environment production \\
     --expected-project <GCP_PROJECT_ID> --expected-account <OPERATOR_EMAIL> \\
     --expected-sha <FULL_RELEASE_SHA> --confirm-production-change --database-url-env <DB_URL_ENV>

2. Mark confirmed not launched (operator verified NO execution started;
   the run becomes eligible for manual requeue):
   $0 --run-id ${rid} --resolution confirmed-not-launched --apply ... (same guards)

3. Requeue after operator verification (only after resolution 2):
   $0 --run-id ${rid} --resolution requeue --apply ... (same guards)

4. Leave unresolved (explicitly documented decision; no mutation):
   $0 --run-id ${rid} --resolution leave-unresolved

EOF

# ---------------------------------------------------------------------------
# Apply mode (never used in CI or during repository preparation).
# ---------------------------------------------------------------------------
if [[ "${APPLY_MODE}" -eq 1 ]]; then
  case "${RESOLUTION}" in
    confirmed-launched|confirmed-not-launched|requeue) ;;
    leave-unresolved)
      record_check PASS "resolution" "leave-unresolved requires no mutation; decision recorded in audit file"
      write_audit_record "${AUDIT_FILE}" "reconcile-launch-unknown" "leave-unresolved run=${RUN_ID:-unspecified}"
      finish_checks "reconcile-launch-unknown" "${JSON_OUTPUT}"
      exit $?
      ;;
    *)
      record_check BLOCKED "resolution" "--apply requires --resolution (confirmed-launched | confirmed-not-launched | requeue | leave-unresolved)"
      finish_checks "reconcile-launch-unknown" "${JSON_OUTPUT}"
      exit $?
      ;;
  esac
  if [[ -z "${RUN_ID}" ]]; then
    record_check BLOCKED "apply:run-id" "--apply requires an explicit --run-id; bulk mutation is not supported"
    finish_checks "reconcile-launch-unknown" "${JSON_OUTPUT}"
    exit $?
  fi
  if ! apply_guard; then
    finish_checks "reconcile-launch-unknown" "${JSON_OUTPUT}"
    exit $?
  fi
  if [[ -z "${DB_URL_ENV}" || -z "${!DB_URL_ENV:-}" ]]; then
    record_check BLOCKED "apply:connection" "--database-url-env with a populated variable is required in apply mode"
    finish_checks "reconcile-launch-unknown" "${JSON_OUTPUT}"
    exit $?
  fi
  case "${RESOLUTION}" in
    confirmed-launched)
      sql="update public.runs set launch_state = 'launched' where id = '${RUN_ID}'::uuid and launch_state = 'launch_unknown';" ;;
    confirmed-not-launched)
      sql="update public.runs set launch_state = 'launch_failed' where id = '${RUN_ID}'::uuid and launch_state = 'launch_unknown';" ;;
    requeue)
      sql="update public.runs set launch_state = 'pending' where id = '${RUN_ID}'::uuid and launch_state = 'launch_failed';" ;;
  esac
  printf '\nAction plan (idempotent; guarded by current launch_state):\n  %s\n' "${sql}"
  write_audit_record "${AUDIT_FILE}" "reconcile-launch-unknown" "resolution=${RESOLUTION} run=${RUN_ID}"
  if ! tool_available psql; then
    record_check BLOCKED "apply:psql" "psql unavailable; apply aborted before any mutation"
    finish_checks "reconcile-launch-unknown" "${JSON_OUTPUT}"
    exit $?
  fi
  affected="$(psql -X -A -t -v ON_ERROR_STOP=1 "${!DB_URL_ENV}" -c "${sql} select 1;" > /dev/null 2>&1 && printf 'ok' || printf 'failed')"
  if [[ "${affected}" == "ok" ]]; then
    record_check PASS "apply:${RESOLUTION}" "resolution applied for run ${RUN_ID} (idempotent; re-running is a no-op)"
  else
    record_check BLOCKED "apply:${RESOLUTION}" "mutation failed; state unchanged"
  fi
fi

finish_checks "reconcile-launch-unknown" "${JSON_OUTPUT}"

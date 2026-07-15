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
  # psql must exist BEFORE anything is written — never write an audit record
  # for a mutation that could not even be attempted.
  if ! tool_available psql; then
    record_check BLOCKED "apply:psql" "psql unavailable; apply aborted before any mutation and before any audit record"
    finish_checks "reconcile-launch-unknown" "${JSON_OUTPUT}"
    exit $?
  fi
  db_url="${!DB_URL_ENV}"
  milo_tmpdir_init

  # Map each resolution to its required current launch_state, the guarded
  # target state, and whether the run's status/lease also gate the change.
  required_launch_state="" target_launch_state="" require_queued_status=0 require_no_lease=0
  case "${RESOLUTION}" in
    confirmed-launched)
      required_launch_state="launch_unknown"; target_launch_state="launched" ;;
    confirmed-not-launched)
      required_launch_state="launch_unknown"; target_launch_state="launch_failed" ;;
    requeue)
      # Requeue is only ever valid from an explicitly failed launch, and only
      # while the run is still queued with no active worker lease. It returns
      # the run to the safe 'pending' launch state; it never launches the
      # worker. launch_unknown is NEVER auto-relaunched (that path is blocked
      # below because its required_launch_state is launch_failed, not
      # launch_unknown).
      required_launch_state="launch_failed"; target_launch_state="pending"
      require_queued_status=1; require_no_lease=1 ;;
  esac

  # 1) Read current state (read-only). A DB error here is BLOCKED and no
  #    mutation is attempted.
  read_err="${_MILO_TMPDIR}/reconcile-read.err"
  read_sql="select launch_state || '|' || status || '|' || (case when lease_token is not null and lease_expires_at is not null and lease_expires_at > now() then 'active' else 'none' end) from public.runs where id = '${RUN_ID}'::uuid;"
  read_status=0
  read_out="$(psql -X -A -t -v ON_ERROR_STOP=1 "${db_url}" -c "${read_sql}" 2> "${read_err}")" || read_status=$?
  if [[ "${read_status}" -ne 0 ]]; then
    record_check BLOCKED "apply:db" "database read failed before any mutation (connection string never printed); no audit record written"
    finish_checks "reconcile-launch-unknown" "${JSON_OUTPUT}"
    exit $?
  fi
  read_out="$(printf '%s' "${read_out}" | tr -d '[:space:]')"
  if [[ -z "${read_out}" ]]; then
    record_check BLOCKED "apply:run-missing" "run ${RUN_ID} not found; zero matching rows, no mutation performed, no audit record written"
    finish_checks "reconcile-launch-unknown" "${JSON_OUTPUT}"
    exit $?
  fi
  cur_launch="${read_out%%|*}"
  rest="${read_out#*|}"
  cur_status="${rest%%|*}"
  cur_lease="${rest##*|}"

  # 2) Idempotency: if the run is already in the target state, this is a safe
  #    no-op — never a fresh "mutation applied" PASS and never a new audit
  #    record.
  if [[ "${cur_launch}" == "${target_launch_state}" ]]; then
    record_check NOT_APPLICABLE "apply:${RESOLUTION}" "run ${RUN_ID} is already in launch_state '${target_launch_state}'; idempotent no-op (no mutation, no audit record)"
    finish_checks "reconcile-launch-unknown" "${JSON_OUTPUT}"
    exit $?
  fi

  # 3) State guards — fail closed on any invalid precondition.
  if [[ "${cur_launch}" != "${required_launch_state}" ]]; then
    record_check BLOCKED "apply:state" "run ${RUN_ID} has launch_state '${cur_launch}'; '${RESOLUTION}' requires '${required_launch_state}'. No mutation performed, no audit record written."
    finish_checks "reconcile-launch-unknown" "${JSON_OUTPUT}"
    exit $?
  fi
  if [[ "${require_queued_status}" -eq 1 && "${cur_status}" != "queued" ]]; then
    record_check BLOCKED "apply:status" "run ${RUN_ID} status is '${cur_status}'; requeue only operates on a still-'queued' run and must never touch completed/failed/cancelled/progressed runs. No mutation performed."
    finish_checks "reconcile-launch-unknown" "${JSON_OUTPUT}"
    exit $?
  fi
  if [[ "${require_no_lease}" -eq 1 && "${cur_lease}" == "active" ]]; then
    record_check BLOCKED "apply:lease" "run ${RUN_ID} holds an active worker lease; requeue refuses to act while a worker may still be executing. No mutation performed."
    finish_checks "reconcile-launch-unknown" "${JSON_OUTPUT}"
    exit $?
  fi

  # 4) Guarded mutation with RETURNING. The data-modifying CTE performs the
  #    UPDATE; the outer select reports the affected row count plus the
  #    resulting state. The WHERE clause re-checks every guard atomically so a
  #    state change between the read and the write cannot slip through.
  lease_guard=""
  [[ "${require_no_lease}" -eq 1 ]] && lease_guard=" and (lease_token is null or lease_expires_at is null or lease_expires_at <= now())"
  status_guard=""
  [[ "${require_queued_status}" -eq 1 ]] && status_guard=" and status = 'queued'"
  update_sql="with upd as (update public.runs set launch_state = '${target_launch_state}' where id = '${RUN_ID}'::uuid and launch_state = '${required_launch_state}'${status_guard}${lease_guard} returning launch_state, status) select count(*) || '|' || coalesce(max(launch_state), '') || '|' || coalesce(max(status), '') from upd;"
  printf '\nMutation (guarded UPDATE ... RETURNING; exactly one row required):\n  %s\n' "${update_sql}"
  upd_err="${_MILO_TMPDIR}/reconcile-update.err"
  upd_status=0
  upd_out="$(psql -X -A -t -v ON_ERROR_STOP=1 "${db_url}" -c "${update_sql}" 2> "${upd_err}")" || upd_status=$?
  if [[ "${upd_status}" -ne 0 ]]; then
    record_check BLOCKED "apply:db" "the guarded UPDATE failed (database error); state unchanged, no audit record written"
    finish_checks "reconcile-launch-unknown" "${JSON_OUTPUT}"
    exit $?
  fi
  upd_out="$(printf '%s' "${upd_out}" | tr -d '[:space:]')"
  affected="${upd_out%%|*}"
  rest2="${upd_out#*|}"
  new_launch="${rest2%%|*}"
  new_status="${rest2##*|}"
  case "${affected}" in
    1) : ;;
    0)
      record_check BLOCKED "apply:${RESOLUTION}" "zero rows updated (a concurrent change no longer satisfies the guard); state unchanged, no audit record written"
      finish_checks "reconcile-launch-unknown" "${JSON_OUTPUT}"
      exit $?
      ;;
    *)
      record_check BLOCKED "apply:${RESOLUTION}" "unexpected affected-row count '${affected}' (a UUID primary key must match at most one row); failing closed, no audit record written"
      finish_checks "reconcile-launch-unknown" "${JSON_OUTPUT}"
      exit $?
      ;;
  esac

  # 5) Validate the resulting state before recording success.
  if [[ "${new_launch}" != "${target_launch_state}" ]]; then
    record_check BLOCKED "apply:${RESOLUTION}" "post-update launch_state is '${new_launch}', expected '${target_launch_state}'; failing closed, no audit record written"
    finish_checks "reconcile-launch-unknown" "${JSON_OUTPUT}"
    exit $?
  fi

  # 6) Only now — guards passed, mutation succeeded, exactly one row, state
  #    validated — write the secret-free audit record.
  audit_ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  umask 077
  printf '%s script=reconcile-launch-unknown run=%s resolution=%s prev_launch_state=%s new_launch_state=%s run_status=%s operator=%s project=%s sha=%s\n' \
    "${audit_ts}" "${RUN_ID}" "${RESOLUTION}" "${cur_launch}" "${new_launch}" "${new_status}" \
    "${EXPECTED_ACCOUNT}" "${EXPECTED_PROJECT}" "$(git_head_sha)" >> "${AUDIT_FILE}"
  record_check PASS "apply:${RESOLUTION}" "run ${RUN_ID}: exactly one row updated ${cur_launch} -> ${new_launch} (status ${new_status}); audit appended to ${AUDIT_FILE}"
fi

finish_checks "reconcile-launch-unknown" "${JSON_OUTPUT}"

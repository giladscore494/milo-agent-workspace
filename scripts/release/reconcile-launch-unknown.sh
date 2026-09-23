#!/usr/bin/env bash
# Safe operator tooling for launch reconciliation: launch_unknown runs, and
# LOST launches (queued + launching, quiet for at least --min-quiet-seconds).
#
# Default mode LISTS unresolved launch_unknown runs and lost launches only
# (safe identifiers and classification, never raw provider responses) and
# generates the four suggested operator command templates per run:
#   1. mark confirmed launched
#   2. mark confirmed not launched (eligible for manual requeue)
#   3. requeue after operator verification
#   4. leave unresolved
#
# A run is NEVER relaunched merely because the original response was
# uncertain. Every mutation requires the full protected apply mode and is
# idempotent (guarded by the current launch_state).
#
# A LOST launch is what the API leaves behind if its process dies after the
# launch compare-and-set took ownership (launch_state 'launching') and before
# it recorded launched, launch_failed or launch_unknown. It is an unresolved
# launch, exactly like launch_unknown -- a worker may or may not have been
# started -- and it takes the same decisions 1, 2 and 4, only after the
# operator has checked Cloud Run for an execution of the run. They go through
# the database's own guard, public.reconcile_lost_launch (migration
# 20260924000100), which proves under the run's row lock that the run is
# still queued, that no worker ever claimed it (no worker, no lease, never
# started) and that the row has been quiet for at least --min-quiet-seconds
# (never under 900; the launch request times out after 15 s). Decision 2 also
# requires that nothing but the API ever wrote about the run, and moves it to
# launch_failed: the existing requeue path, where the same run can be launched
# again only through the launch compare-and-set. launch_unknown keeps the
# guarded updates below, unchanged.

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
  --min-quiet-seconds <N>         How long a queued + launching run must have
                                  been quiet to count as a LOST launch
                                  (default 1800, never under 900). Listing
                                  uses it, and the database enforces it again
                                  on every lost-launch decision.
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
MIN_QUIET_SECONDS=1800
# The shortest quiet period reconcile_lost_launch ever accepts.
MIN_QUIET_FLOOR=900
APPLY_MODE=0 APPLY_ENVIRONMENT="" EXPECTED_PROJECT="" EXPECTED_ACCOUNT="" EXPECTED_SHA="" CONFIRM_PRODUCTION_CHANGE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --database-url-env) DB_URL_ENV="${2:?}"; shift 2 ;;
    --run-id) RUN_ID="${2:?}"; shift 2 ;;
    --resolution) RESOLUTION="${2:?}"; shift 2 ;;
    --min-quiet-seconds) MIN_QUIET_SECONDS="${2:?}"; shift 2 ;;
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
if [[ ! "${MIN_QUIET_SECONDS}" =~ ^[0-9]{1,7}$ ]] || (( 10#${MIN_QUIET_SECONDS} < MIN_QUIET_FLOOR )); then
  record_check BLOCKED "min-quiet-seconds" "--min-quiet-seconds must be a whole number of seconds, at least ${MIN_QUIET_FLOOR}: a launch that may still be in flight is never reconciled"
  finish_checks "reconcile-launch-unknown" "${JSON_OUTPUT}"
  exit $?
fi
MIN_QUIET_SECONDS="$((10#${MIN_QUIET_SECONDS}))"

# ---------------------------------------------------------------------------
# Listing (read-only).
# ---------------------------------------------------------------------------
if [[ -z "${DB_URL_ENV}" ]]; then
  record_check MANUAL "list" "no --database-url-env supplied; list unresolved runs manually with: select id, created_at, attempt, status, launch_state from public.runs where launch_state = 'launch_unknown' or (status = 'queued' and launch_state = 'launching' and updated_at <= now() - make_interval(secs => ${MIN_QUIET_SECONDS})) order by created_at;"
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
    if [[ "${rows}" != "CONNECTION_FAILED" ]]; then
      # LOST launches: queued + launching, quiet for the threshold. Read-only.
      lost_where="status = 'queued' and launch_state = 'launching' and updated_at <= now() - make_interval(secs => ${MIN_QUIET_SECONDS})"
      if [[ -n "${RUN_ID}" ]]; then
        lost_where="${lost_where} and id = '${RUN_ID}'::uuid"
      fi
      lost_rows="$(psql -X -A -t -v ON_ERROR_STOP=1 "${db_url}" \
        -c "select id || ' | ' || created_at || ' | attempt=' || attempt || ' | quiet_for=' || floor(extract(epoch from now() - updated_at))::bigint || 's | worker=' || coalesce(worker_id, 'none') from public.runs where ${lost_where} order by created_at" 2> /dev/null || printf 'CONNECTION_FAILED')"
      if [[ "${lost_rows}" == "CONNECTION_FAILED" ]]; then
        record_check BLOCKED "list:lost-launch" "unable to list lost launches via ${DB_URL_ENV} (connection string never printed)"
      elif [[ -z "${lost_rows}" ]]; then
        record_check PASS "list:lost-launch" "no lost launches (queued + launching, quiet for at least ${MIN_QUIET_SECONDS}s)"
      else
        lost_count="$(wc -l <<< "${lost_rows}" | tr -d ' ')"
        record_check WARN "list:lost-launch" "${lost_count} lost launch(es) (queued + launching, quiet for at least ${MIN_QUIET_SECONDS}s) hold their runs, and any Mapping Plan batch, and require operator review"
        printf '\nLost launches (safe identifiers only):\n%s\n' "${lost_rows}"
      fi
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

A LOST launch (queued + launching, quiet for at least --min-quiet-seconds)
takes decisions 1, 2 and 4 the same way, after the same Cloud Run check: look
for an execution of the worker job whose RUN_ID is ${rid}. The database proves
that no worker ever claimed the run before it accepts 1 or 2, and 2 moves the
run to launch_failed: the same run is then launched again only by a person
(the Mapping Plan's "Launch batch", or a replay of the original request), or
after decision 3. Nothing launches it automatically.

4. Leave unresolved (explicitly documented operator decision; no database
   mutation, but the SAME operator identity guard is required so the audit
   record is trustworthy; a read-only --database-url-env optionally
   revalidates the run is still launch_unknown, or a lost launch):
   $0 --run-id ${rid} --resolution leave-unresolved --apply --environment production \\
     --expected-project <GCP_PROJECT_ID> --expected-account <OPERATOR_EMAIL> \\
     --expected-sha <FULL_RELEASE_SHA> --confirm-production-change [--database-url-env <RO_DB_URL_ENV>]

EOF

# ---------------------------------------------------------------------------
# Apply mode (never used in CI or during repository preparation).
# ---------------------------------------------------------------------------
if [[ "${APPLY_MODE}" -eq 1 ]]; then
  case "${RESOLUTION}" in
    confirmed-launched|confirmed-not-launched|requeue|leave-unresolved) ;;
    *)
      record_check BLOCKED "resolution" "--apply requires --resolution (confirmed-launched | confirmed-not-launched | requeue | leave-unresolved)"
      finish_checks "reconcile-launch-unknown" "${JSON_OUTPUT}"
      exit $?
      ;;
  esac
  # An explicit run id and the full operator identity guard are required for
  # EVERY apply-mode decision, including leave-unresolved: it is a recorded
  # operator decision even though it mutates nothing. No record is written if
  # the guard fails.
  if [[ -z "${RUN_ID}" ]]; then
    record_check BLOCKED "apply:run-id" "--apply requires an explicit --run-id; bulk mutation is not supported and an unidentified decision is never recorded"
    finish_checks "reconcile-launch-unknown" "${JSON_OUTPUT}"
    exit $?
  fi
  if ! apply_guard; then
    finish_checks "reconcile-launch-unknown" "${JSON_OUTPUT}"
    exit $?
  fi

  # leave-unresolved: no database mutation, so it does NOT require a writable
  # connection. It still requires the operator identity guard above, and it
  # optionally revalidates the live state read-only when a DB URL is supplied.
  if [[ "${RESOLUTION}" == "leave-unresolved" ]]; then
    db_verified="not-verified(no-db-url)"
    if [[ -n "${DB_URL_ENV}" && -n "${!DB_URL_ENV:-}" ]]; then
      if ! tool_available psql; then
        record_check MANUAL "leave-unresolved:db" "psql unavailable; live launch_state was NOT revalidated (decision still recorded with db_verified=psql-unavailable)"
        db_verified="not-verified(psql-unavailable)"
      else
        milo_tmpdir_init
        lu_err="${_MILO_TMPDIR}/leave-read.err"
        lu_status=0
        lu_state="$(psql -X -A -t -v ON_ERROR_STOP=1 "${!DB_URL_ENV}" -c "select launch_state from public.runs where id = '${RUN_ID}'::uuid;" 2> "${lu_err}")" || lu_status=$?
        if [[ "${lu_status}" -ne 0 ]]; then
          record_check BLOCKED "leave-unresolved:db" "read-only revalidation failed (database error); no decision recorded"
          finish_checks "reconcile-launch-unknown" "${JSON_OUTPUT}"
          exit $?
        fi
        lu_state="$(printf '%s' "${lu_state}" | tr -d '[:space:]')"
        if [[ -z "${lu_state}" ]]; then
          record_check BLOCKED "leave-unresolved:db" "run ${RUN_ID} not found; refusing to record a leave-unresolved decision for a non-existent run"
          finish_checks "reconcile-launch-unknown" "${JSON_OUTPUT}"
          exit $?
        fi
        if [[ "${lu_state}" != "launch_unknown" && "${lu_state}" != "launching" ]]; then
          record_check BLOCKED "leave-unresolved:db" "run ${RUN_ID} launch_state is '${lu_state}', not 'launch_unknown' or 'launching'; leave-unresolved only applies to an unresolved run. No decision recorded."
          finish_checks "reconcile-launch-unknown" "${JSON_OUTPUT}"
          exit $?
        fi
        db_verified="verified-${lu_state}"
      fi
    fi
    # Write the decision record ONLY after the guard (and any DB revalidation)
    # passed. Repeated leave-unresolved decisions each append one audit line —
    # the record is an operator decision log, not a database mutation.
    audit_ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    umask 077
    printf '%s script=reconcile-launch-unknown run=%s resolution=leave-unresolved operator=%s project=%s expected_sha=%s git_sha=%s db_verified=%s\n' \
      "${audit_ts}" "${RUN_ID}" "${EXPECTED_ACCOUNT}" "${EXPECTED_PROJECT}" "${EXPECTED_SHA}" "$(git_head_sha)" "${db_verified}" >> "${AUDIT_FILE}"
    record_check PASS "resolution" "leave-unresolved decision recorded for run ${RUN_ID} (no database mutation; db_verified=${db_verified})"
    finish_checks "reconcile-launch-unknown" "${JSON_OUTPUT}"
    exit $?
  fi

  # Mutation resolutions require a writable connection and psql.
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

  # A LOST launch: decisions 1 and 2 go through the database's own guard,
  # reconcile_lost_launch, which re-proves everything under the run's row lock.
  if [[ "${cur_launch}" == "launching" && "${cur_status}" == "queued" \
        && ( "${RESOLUTION}" == "confirmed-launched" || "${RESOLUTION}" == "confirmed-not-launched" ) ]]; then
    if [[ "${cur_lease}" == "active" ]]; then
      record_check BLOCKED "apply:lease" "run ${RUN_ID} holds an active worker lease: a worker was started, so this is not a lost launch. No mutation performed, no audit record written."
      finish_checks "reconcile-launch-unknown" "${JSON_OUTPUT}"
      exit $?
    fi
    lost_outcome="launched" target_launch_state="launched"
    if [[ "${RESOLUTION}" == "confirmed-not-launched" ]]; then
      lost_outcome="not_launched" target_launch_state="launch_failed"
    fi
    printf '\nGuarded decision (public.reconcile_lost_launch; exactly one run, re-proven under its row lock):\n  run=%s outcome=%s min_quiet_seconds=%s\n' \
      "${RUN_ID}" "${lost_outcome}" "${MIN_QUIET_SECONDS}"
    lost_err="${_MILO_TMPDIR}/reconcile-lost-launch.err"
    lost_status=0
    lost_out="$(psql -X -A -t -v ON_ERROR_STOP=1 -v run_id="${RUN_ID}" -v outcome="${lost_outcome}" \
      -v quiet="${MIN_QUIET_SECONDS}" -v operator="${EXPECTED_ACCOUNT}" \
      "${db_url}" 2> "${lost_err}" << 'SQL'
select (r->>'reconciled') || '|' || (r->>'launch_state') || '|' || (r->>'status')
  from (select public.reconcile_lost_launch(:'run_id'::uuid, :'outcome', :'quiet'::integer, :'operator') as r) as decided;
SQL
    )" || lost_status=$?
    if [[ "${lost_status}" -ne 0 ]]; then
      lost_code="$(grep -oE 'LOST_LAUNCH_[A-Z_]+' "${lost_err}" | head -1 || true)"
      if [[ -n "${lost_code}" ]]; then
        record_check BLOCKED "apply:refused" "the database refused '${RESOLUTION}' for run ${RUN_ID}: ${lost_code}. The run is unchanged and no audit record was written."
      else
        record_check BLOCKED "apply:db" "the guarded decision failed (database error; connection string never printed); state unchanged, no audit record written"
      fi
      finish_checks "reconcile-launch-unknown" "${JSON_OUTPUT}"
      exit $?
    fi
    lost_out="$(printf '%s' "${lost_out}" | tr -d '[:space:]')"
    lost_changed="${lost_out%%|*}"
    lost_rest="${lost_out#*|}"
    new_launch="${lost_rest%%|*}"
    new_status="${lost_rest##*|}"
    if [[ "${lost_changed}" == "false" && "${new_launch}" == "${target_launch_state}" ]]; then
      record_check NOT_APPLICABLE "apply:${RESOLUTION}" "run ${RUN_ID} is already in launch_state '${target_launch_state}'; idempotent no-op (no mutation, no audit record)"
      finish_checks "reconcile-launch-unknown" "${JSON_OUTPUT}"
      exit $?
    fi
    if [[ "${lost_changed}" != "true" || "${new_launch}" != "${target_launch_state}" || "${new_status}" != "queued" ]]; then
      record_check BLOCKED "apply:${RESOLUTION}" "unexpected answer from the database ('${lost_out}'); failing closed, no audit record written"
      finish_checks "reconcile-launch-unknown" "${JSON_OUTPUT}"
      exit $?
    fi
    # Only now -- guard passed, the database proved and changed exactly this
    # run -- write the secret-free audit record.
    audit_ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    umask 077
    printf '%s script=reconcile-launch-unknown run=%s resolution=%s prev_launch_state=launching new_launch_state=%s run_status=%s operator=%s project=%s sha=%s min_quiet_seconds=%s\n' \
      "${audit_ts}" "${RUN_ID}" "${RESOLUTION}" "${new_launch}" "${new_status}" \
      "${EXPECTED_ACCOUNT}" "${EXPECTED_PROJECT}" "$(git_head_sha)" "${MIN_QUIET_SECONDS}" >> "${AUDIT_FILE}"
    record_check PASS "apply:${RESOLUTION}" "run ${RUN_ID}: lost launch reconciled launching -> ${new_launch} (status ${new_status}); nothing was launched; audit appended to ${AUDIT_FILE}"
    finish_checks "reconcile-launch-unknown" "${JSON_OUTPUT}"
    exit $?
  fi

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
    accepted="'${required_launch_state}'"
    [[ "${required_launch_state}" == "launch_unknown" ]] && accepted="'launch_unknown', or 'launching' on a still-queued run (a lost launch)"
    record_check BLOCKED "apply:state" "run ${RUN_ID} has launch_state '${cur_launch}' (status '${cur_status}'); '${RESOLUTION}' requires ${accepted}. No mutation performed, no audit record written."
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

#!/usr/bin/env bash
# Safe, reviewed resolution for the UNUSED prepared Government capture run.
#
#   run_id          555101dc-46f6-4048-bd67-efccbc98f528
#   idempotency_key catalog-government-capture-20260919-01
#   live posture    status=queued launch_state=none worker_id=NULL
#                   started_at=NULL lease_token=NULL attempt=1
#                   zero run_events / ledger / reservations / heartbeats /
#                   invocations / checkpoints / blackboards
#   (verified read-only against production 2026-09-18)
#
# WHAT THIS SCRIPT NEVER DOES
# ---------------------------
#   * It NEVER executes, launches, claims or resumes the capture. It makes
#     no outbound Government request, constructs no transport, and touches
#     no catalog table. `MILO_ENABLE_CATALOG_EXECUTION` stays false.
#   * It NEVER issues an unconditional UPDATE. Every write carries the full
#     expected pre-state in its WHERE clause and asserts an affected-row
#     count of exactly 1; anything else raises and rolls the transaction
#     back, leaving the row untouched.
#   * It NEVER deletes a run row, and it never touches any run other than
#     the one pinned id.
#
# WHY A RESOLUTION IS OWED AT ALL
# -------------------------------
# Today the run is safe by construction, and Stage D proves that rather
# than assuming it:
#   * `try_acquire_launch` acquires only from launch_state 'pending' or
#     'launch_failed' (backend/repository/supabase.py), so a run resting in
#     'none' can never be taken by the ordinary launcher; and
#   * the Worker resolves its target from the RUN_ID environment variable
#     and never polls for queued rows (backend/worker/main.py
#     resolve_run_id), so nothing sweeps it up.
# But two facts still argue for retiring it:
#   1. `claim_run_lease` (migration 012) predicates its CAS on status,
#      worker and lease expiry ONLY — not on launch_state. A 'queued' row
#      is therefore claimable by anything that calls the RPC with that run
#      id. A 'cancelled' row is not: terminal states are outside that
#      WHERE clause entirely. Retiring converts a convention into an
#      enforced database fact.
#   2. 'queued' is an ACTIVE run state (ACTIVE_RUN_STATES), so the row
#      counts against MILO_MAX_CONCURRENT_RUNS_PER_{USER,PROJECT}=1 for its
#      user and project forever — which is a standing invitation for a
#      future operator to "clear it" in a hurry, unguarded.
#
# HOW THE RETIREMENT IS DONE (the supported cancellation path)
# ------------------------------------------------------------
# Two guarded compare-and-set statements inside ONE transaction, following
# the repository's own state machine (backend/runtime.py VALID_TRANSITIONS:
# queued -> cancellation_requested -> cancelled; a direct queued ->
# cancelled is NOT a supported transition and is deliberately not forged).
# Both steps run in one transaction on purpose: `claim_run_lease` CAN
# acquire from 'cancellation_requested', so that intermediate state is
# never allowed to become externally visible.
#
# Usage: default mode is READ-ONLY and prints the plan. Mutation requires
# the full protected apply guard and is NEVER used in CI.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
source "${SCRIPT_DIR}/../lib/common.sh"
# shellcheck source=stage-d-env.sh
source "${SCRIPT_DIR}/stage-d-env.sh"

RUN_ID="${STAGE_D_GOV_CAPTURE_RUN_ID}"
CAPTURE_KEY="${STAGE_D_GOV_CAPTURE_KEY}"

usage() {
  cat << 'EOF'
Usage: resolve-government-capture.sh [options]

Default: READ-ONLY. Reads the prepared Government capture run's current
posture and prints the exact guarded SQL that apply mode would run. No
mutation, no capture, no outbound request.

Options:
  --database-url-env <NAME>       Environment variable holding a connection
                                  string (read-only is enough for the
                                  default mode). Never accepted on the
                                  command line.
  --resolution <kind>             One of: retire, leave-prepared.
                                  Required with --apply.
  --json-output <path>            Write a machine-readable JSON report.

Resolutions:
  retire          Guarded compare-and-set queued -> cancellation_requested
                  -> cancelled in ONE transaction. The run becomes
                  terminal, so claim_run_lease can never acquire it again.
                  The row, its history and the catalog are otherwise
                  untouched. THE CAPTURE IS NEVER EXECUTED.
  leave-prepared  No database mutation. Records an explicit, audited
                  operator decision to leave the run prepared, for the case
                  where a live capture is genuinely intended later under
                  its own AUTH-1 authorization.

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
                                  record (default: ./government-capture-resolution-audit.log)
  --help                          Show this help.
EOF
}

JSON_OUTPUT="" DB_URL_ENV="" RESOLUTION="" AUDIT_FILE="government-capture-resolution-audit.log"
APPLY_MODE=0 APPLY_ENVIRONMENT="" EXPECTED_PROJECT="" EXPECTED_ACCOUNT="" EXPECTED_SHA="" CONFIRM_PRODUCTION_CHANGE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --database-url-env) DB_URL_ENV="${2:?}"; shift 2 ;;
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

# The pinned identity is a constant, never an argument: this script can only
# ever act on the one reviewed run.
if ! is_uuid "${RUN_ID}"; then
  record_check BLOCKED "run-id" "pinned Government capture run id is not a UUID; identifiers are never invented"
  finish_checks "resolve-government-capture" "${JSON_OUTPUT}"
  exit $?
fi

# ---------------------------------------------------------------------------
# The exact guarded SQL. Printed in every mode so a reviewer reads the same
# text the operator executes.
#
# Every WHERE clause restates the FULL expected pre-state, and every step
# asserts row_count = 1. A raise inside the DO block aborts the transaction,
# so a concurrent change between the read and the write leaves the row
# exactly as it was — there is no partial application and no window in
# which 'cancellation_requested' (which claim_run_lease CAN acquire) is
# visible to another session.
# ---------------------------------------------------------------------------
CANCELLATION_REASON="stage-d: unused prepared Government capture retired; capture never authorized and never executed"

retire_sql() {
  cat << EOF
begin;
do \$\$
declare
  v_rows integer;
begin
  -- Step 1 (supported transition queued -> cancellation_requested).
  -- Guarded on the FULL prepared pre-state: identity, never-launched,
  -- never-claimed, never-started.
  update public.runs set
    status                    = 'cancellation_requested',
    cancellation_requested_at = now(),
    cancellation_reason       = '${CANCELLATION_REASON}'
  where id              = '${RUN_ID}'::uuid
    and idempotency_key = '${CAPTURE_KEY}'
    and status          = 'queued'
    and launch_state    = 'none'
    and worker_id       is null
    and lease_token     is null
    and lease_expires_at is null
    and started_at      is null
    and finished_at     is null
    and attempt         = 1;
  get diagnostics v_rows = row_count;
  if v_rows <> 1 then
    raise exception
      'STAGE_D_GOV_GUARD_1: expected exactly 1 row in the prepared pre-state, matched % — rolling back, the run is unchanged', v_rows;
  end if;

  -- Step 2 (supported transition cancellation_requested -> cancelled).
  -- Re-guarded on never-claimed. 'cancelled' is terminal and therefore
  -- outside claim_run_lease's acquirable set: after this the run can never
  -- be claimed by any worker.
  update public.runs set
    status      = 'cancelled',
    finished_at = now()
  where id           = '${RUN_ID}'::uuid
    and status       = 'cancellation_requested'
    and launch_state = 'none'
    and worker_id    is null
    and lease_token  is null
    and started_at   is null;
  get diagnostics v_rows = row_count;
  if v_rows <> 1 then
    raise exception
      'STAGE_D_GOV_GUARD_2: expected exactly 1 row to reach cancelled, matched % — rolling back, the run is unchanged', v_rows;
  end if;
end
\$\$;
commit;
EOF
}

verify_sql() {
  printf "select status || '|' || launch_state || '|' || coalesce(worker_id, 'none') || '|' || coalesce(started_at::text, 'none') from public.runs where id = '%s'::uuid;" "${RUN_ID}"
}

read_state_sql() {
  printf "select status || '|' || launch_state || '|' || coalesce(worker_id, 'none') || '|' || coalesce(started_at::text, 'none') || '|' || coalesce(idempotency_key, 'none') || '|' || attempt from public.runs where id = '%s'::uuid;" "${RUN_ID}"
}

# ---------------------------------------------------------------------------
# Read-only posture read (both modes).
# ---------------------------------------------------------------------------
current_state=""
if [[ -z "${DB_URL_ENV}" ]]; then
  record_check MANUAL "read" "no --database-url-env supplied; read the current posture manually (read-only): $(read_state_sql)"
elif ! tool_available psql; then
  record_check MANUAL "read" "psql unavailable; read the current posture manually (read-only)"
else
  db_url="${!DB_URL_ENV:-}"
  if [[ -z "${db_url}" ]]; then
    record_check BLOCKED "read:connection" "environment variable ${DB_URL_ENV} is empty"
  else
    milo_tmpdir_init
    read_status=0
    current_state="$(psql -X -A -t -v ON_ERROR_STOP=1 "${db_url}" -c "$(read_state_sql)" 2> "${_MILO_TMPDIR}/gov-read.err")" || read_status=$?
    current_state="$(printf '%s' "${current_state}" | tr -d '[:space:]')"
    if [[ "${read_status}" -ne 0 ]]; then
      record_check BLOCKED "read" "database read failed (connection string never printed); no mutation attempted"
      current_state=""
    elif [[ -z "${current_state}" ]]; then
      record_check BLOCKED "read:missing" "run ${RUN_ID} not found; Stage D deletes no run row, so its absence is unexplained drift"
    else
      case "${current_state}" in
        "queued|none|none|none|${CAPTURE_KEY}|1")
          record_check PASS "read" "run ${RUN_ID} is PREPARED and unclaimed (queued/none, no worker, no lease, no start)" ;;
        "cancelled|none|none|none|${CAPTURE_KEY}|1")
          record_check NOT_APPLICABLE "read" "run ${RUN_ID} is already RETIRED (cancelled/none) — nothing to resolve" ;;
        *)
          record_check BLOCKED "read:posture" "run ${RUN_ID} is in an unexpected posture '${current_state}' — expected the prepared or retired posture; refusing to act" ;;
      esac
    fi
  fi
fi

# ---------------------------------------------------------------------------
# The plan (always printed).
# ---------------------------------------------------------------------------
cat << EOF

Prepared Government capture run ${RUN_ID}
  idempotency_key : ${CAPTURE_KEY}
  THE CAPTURE IS NEVER EXECUTED BY THIS SCRIPT.

Resolution 1 — retire (recommended): make the run terminally unclaimable
with a guarded compare-and-set. After it, claim_run_lease can no longer
acquire the run at all, and the row stops counting against the
concurrent-run caps. No catalog data is read, written or deleted.

$(retire_sql)

Verify afterwards (read-only); expect exactly 'cancelled|none|none|none':
  $(verify_sql)

Resolution 2 — leave-prepared: no database mutation. Use this ONLY when a
live Government capture is genuinely intended later, under its own AUTH-1
authorization and the separate MILO_ENABLE_CATALOG_EXECUTION decision
(docs/production-readiness/STAGED_ACTIVATION.md). Preparing a run is not
authorization to capture, and leaving it prepared does not become one.

Apply either with the full operator guard, e.g.:
  $0 --resolution retire --apply --environment production \\
    --expected-project ${STAGE_D_PROJECT} --expected-account <OPERATOR_EMAIL> \\
    --expected-sha <FULL_COMMIT_SHA> --confirm-production-change \\
    --database-url-env <DB_URL_ENV>

EOF

# ---------------------------------------------------------------------------
# Apply mode (never used in CI or during repository preparation).
# ---------------------------------------------------------------------------
if [[ "${APPLY_MODE}" -eq 1 ]]; then
  case "${RESOLUTION}" in
    retire|leave-prepared) ;;
    *)
      record_check BLOCKED "resolution" "--apply requires --resolution (retire | leave-prepared)"
      finish_checks "resolve-government-capture" "${JSON_OUTPUT}"
      exit $?
      ;;
  esac
  if ! apply_guard; then
    finish_checks "resolve-government-capture" "${JSON_OUTPUT}"
    exit $?
  fi

  # leave-prepared: no database mutation, so it does NOT require a writable
  # connection. It still requires the operator identity guard above and a
  # successfully read, expected posture — an unverified decision is never
  # recorded.
  if [[ "${RESOLUTION}" == "leave-prepared" ]]; then
    if [[ -z "${current_state}" ]]; then
      record_check BLOCKED "leave-prepared" "the current posture was not read successfully; no decision recorded"
      finish_checks "resolve-government-capture" "${JSON_OUTPUT}"
      exit $?
    fi
    if [[ "${current_state}" != "queued|none|none|none|${CAPTURE_KEY}|1" ]]; then
      record_check BLOCKED "leave-prepared" "run is '${current_state}', not the prepared posture; leave-prepared only applies to a still-prepared run. No decision recorded."
      finish_checks "resolve-government-capture" "${JSON_OUTPUT}"
      exit $?
    fi
    write_audit_record "${AUDIT_FILE}" "resolve-government-capture" \
      "run=${RUN_ID} resolution=leave-prepared prev_state=${current_state} new_state=${current_state} mutated=no"
    record_check PASS "resolution" "leave-prepared decision recorded for run ${RUN_ID} (no database mutation); audit appended to ${AUDIT_FILE}"
    finish_checks "resolve-government-capture" "${JSON_OUTPUT}"
    exit $?
  fi

  # retire: requires a writable connection and psql.
  if [[ -z "${DB_URL_ENV}" || -z "${!DB_URL_ENV:-}" ]]; then
    record_check BLOCKED "apply:connection" "--database-url-env with a populated variable is required in apply mode"
    finish_checks "resolve-government-capture" "${JSON_OUTPUT}"
    exit $?
  fi
  # psql must exist BEFORE anything is written — never write an audit record
  # for a mutation that could not even be attempted.
  if ! tool_available psql; then
    record_check BLOCKED "apply:psql" "psql unavailable; apply aborted before any mutation and before any audit record"
    finish_checks "resolve-government-capture" "${JSON_OUTPUT}"
    exit $?
  fi
  db_url="${!DB_URL_ENV}"
  milo_tmpdir_init

  # Idempotency: already retired is a safe no-op, never a fresh "mutation
  # applied" PASS and never a new audit record.
  if [[ "${current_state}" == "cancelled|none|none|none|${CAPTURE_KEY}|1" ]]; then
    record_check NOT_APPLICABLE "apply:retire" "run ${RUN_ID} is already retired (cancelled/none); idempotent no-op (no mutation, no audit record)"
    finish_checks "resolve-government-capture" "${JSON_OUTPUT}"
    exit $?
  fi
  # Anything other than the exact prepared posture stops here. The guarded
  # SQL would refuse anyway — this is the cheap early refusal, and it keeps
  # a surprising state from being papered over.
  if [[ "${current_state}" != "queued|none|none|none|${CAPTURE_KEY}|1" ]]; then
    record_check BLOCKED "apply:state" "run ${RUN_ID} is '${current_state:-unreadable}', not the prepared posture 'queued|none|none|none|${CAPTURE_KEY}|1'. No mutation performed, no audit record written."
    finish_checks "resolve-government-capture" "${JSON_OUTPUT}"
    exit $?
  fi

  # The guarded transaction. Each statement re-checks every precondition
  # atomically, so a change between the read above and the write cannot
  # slip through; a failed assertion rolls the whole transaction back.
  upd_status=0
  retire_sql | psql -X -A -t -v ON_ERROR_STOP=1 "${db_url}" > "${_MILO_TMPDIR}/gov-retire.out" 2> "${_MILO_TMPDIR}/gov-retire.err" || upd_status=$?
  if [[ "${upd_status}" -ne 0 ]]; then
    record_check BLOCKED "apply:retire" "the guarded transaction did not commit (guard assertion or database error); the run is UNCHANGED, no audit record written. Re-read the posture before retrying."
    printf '\nTransaction stderr (secret-free):\n'
    redact_line "$(cat "${_MILO_TMPDIR}/gov-retire.err")"
    printf '\n'
    finish_checks "resolve-government-capture" "${JSON_OUTPUT}"
    exit $?
  fi

  # Validate the resulting state before recording success. A committed
  # transaction is not by itself proof that the row reached the intended
  # terminal posture.
  verify_status=0
  new_state="$(psql -X -A -t -v ON_ERROR_STOP=1 "${db_url}" -c "$(verify_sql)" 2> "${_MILO_TMPDIR}/gov-verify.err")" || verify_status=$?
  new_state="$(printf '%s' "${new_state}" | tr -d '[:space:]')"
  if [[ "${verify_status}" -ne 0 || "${new_state}" != "cancelled|none|none|none" ]]; then
    record_check BLOCKED "apply:retire" "post-transaction state is '${new_state:-unreadable}', expected 'cancelled|none|none|none'; failing closed, no audit record written"
    finish_checks "resolve-government-capture" "${JSON_OUTPUT}"
    exit $?
  fi

  # Only now — guards passed, transaction committed, state validated — write
  # the secret-free audit record.
  write_audit_record "${AUDIT_FILE}" "resolve-government-capture" \
    "run=${RUN_ID} resolution=retire prev_state=${current_state} new_state=${new_state} capture_executed=no"
  record_check PASS "apply:retire" "run ${RUN_ID} retired: queued/none -> cancelled/none via the guarded compare-and-set; the capture was NOT executed; audit appended to ${AUDIT_FILE}"
fi

finish_checks "resolve-government-capture" "${JSON_OUTPUT}"

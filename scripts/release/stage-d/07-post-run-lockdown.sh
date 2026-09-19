#!/usr/bin/env bash
# Stage D step 7: IMMEDIATE post-run lockdown.
#
# Ordering is deliberate and load-bearing. Everything that needs the db
# probe happens WHILE IT STILL EXISTS; the probe is the only credentialed
# database reader here, so checking after deleting it would be checking
# with nothing.
#
#   1. kill switch — flags off, provider keys unbound, zero ACTIVE CLOUD
#      RUN EXECUTIONS, every postcondition verified;
#   2. terminalize the DATABASE run — cancelling a Cloud Run execution does
#      NOT make the database run terminal (the Worker installs no SIGTERM
#      handler), so an interrupted run can sit in `running` forever holding
#      its lease, its concurrency slot and its budget reservations. This
#      step drives it terminal through guarded, identity-checked
#      transitions and proves: terminal run, zero active runs for its user
#      and project, zero reservations left in status 'reserved';
#   3. Government-capture proof — the prepared capture must still be
#      unclaimed;
#   4. delete both probes — unconditionally, even if 2 or 3 failed: a
#      failed check is never a reason to leave a credentialed probe job
#      standing;
#   5. prove both probes absent from a fresh listing;
#   6. verdict — LOCKDOWN COMPLETE only if EVERY proof above succeeded.
#
# Both probe executions are launched asynchronously and their logs are
# filtered by the EXACT execution name, so a retained record from an older
# deleted-and-recreated job of the same name can never satisfy a gate.
#
# Safe and idempotent to rerun.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
# shellcheck source=stage-d-env.sh
source ./stage-d-env.sh
# shellcheck source=probe_exec.sh
source ./probe_exec.sh

lockdown_failures=0
note_failure() {
  lockdown_failures=$((lockdown_failures + 1))
  echo "LOCKDOWN CRITICAL: $1" >&2
}

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

echo "== 1. Kill switch (paid off, catalog off, run creation off, launcher disabled, provider keys unbound, zero active executions)"
if ! ./kill-switch.sh; then
  note_failure "kill-switch.sh did not complete — production may NOT be fully fail-closed"
fi

# What the cleanup knows about the run, from the machine-readable state.
recorded_run_id=""
if [ -n "${STAGE_D_WORKDIR:-}" ] && [ -r "${STAGE_D_WORKDIR}/state.json" ]; then
  recorded_run_id="$(python3 ./state_file.py "${STAGE_D_WORKDIR}/state.json" read run_id)"
fi

# Is the db probe still present to ask? Absence is not an error by itself
# (a rerun after a completed lockdown is normal), but it decides whether
# an unprovable check is fatal.
db_probe_present=0
probe_listing="$(gcloud run jobs list --project="${STAGE_D_PROJECT}" --region="${STAGE_D_REGION}" \
  --format='value(metadata.name)' 2>/dev/null)" || probe_listing="__LISTING_FAILED__"
if [ "${probe_listing}" != "__LISTING_FAILED__" ] && printf '%s\n' "${probe_listing}" | grep -qx "${STAGE_D_DB_PROBE_JOB}"; then
  db_probe_present=1
fi

echo "== 2. Terminalize the DATABASE run and prove the database is clean"
run_terminal_proven=0
if [ "${db_probe_present}" -eq 1 ]; then
  terminalize_status=0
  execute_probe_attributed "${STAGE_D_DB_PROBE_JOB}" \
    "STAGE_D_MODE=terminalize" \
    "STAGE_D_RUN_ID=${recorded_run_id}" \
    "STAGE_D_IDEMPOTENCY_KEY=${STAGE_D_IDEMPOTENCY_KEY}" \
    "STAGE_D_GOV_CAPTURE_RUN_ID=${STAGE_D_GOV_CAPTURE_RUN_ID}" \
    > "${WORK}/terminalize.log" || terminalize_status=$?
  cat "${WORK}/terminalize.log"
  if probe_verdict "${WORK}/terminalize.log" terminalize; then
    run_terminal_proven=1
    echo "OK: the Stage D database run is terminal; zero active runs and zero dangling reservations remain"
  else
    note_failure "the database run could not be proven terminal and clean (probe exit=${terminalize_status}) — the run may still hold its lease, concurrency slot and budget reservations"
  fi
elif [ -z "${recorded_run_id}" ]; then
  echo "NOT APPLICABLE: no run id is recorded and the db probe is already absent, so there is no"
  echo "                database run to terminalize and no credentialed reader remains."
else
  note_failure "run ${recorded_run_id} was created but the db probe is gone, so the database run cannot be terminalized or proven clean — UNVERIFIED"
fi

echo "== 3. Prepared Government capture run — PROVEN never claimed"
capture_proven=0
if [ "${db_probe_present}" -eq 1 ]; then
  govcheck_status=0
  execute_probe_attributed "${STAGE_D_DB_PROBE_JOB}" \
    "STAGE_D_MODE=govcheck" \
    "STAGE_D_GOV_CAPTURE_RUN_ID=${STAGE_D_GOV_CAPTURE_RUN_ID}" \
    "STAGE_D_GOV_CAPTURE_KEY=${STAGE_D_GOV_CAPTURE_KEY}" \
    "STAGE_D_IDEMPOTENCY_KEY=${STAGE_D_IDEMPOTENCY_KEY}" \
    > "${WORK}/govcheck.log" || govcheck_status=$?
  cat "${WORK}/govcheck.log"
  if probe_verdict "${WORK}/govcheck.log" govcheck; then
    capture_proven=1
    echo "OK: the Government capture run is proven prepared-and-unclaimed or retired-and-unclaimed"
  else
    note_failure "the Government capture posture check FAILED or produced no record for this execution (probe exit=${govcheck_status}) — investigate immediately"
  fi
elif [ -n "${STAGE_D_READONLY_DATABASE_URL_ENV:-}" ] && [ -n "${!STAGE_D_READONLY_DATABASE_URL_ENV:-}" ] && command -v psql > /dev/null 2>&1; then
  gov_state="$(psql -X -A -t -v ON_ERROR_STOP=1 "${!STAGE_D_READONLY_DATABASE_URL_ENV}" \
    -c "select status || '|' || launch_state || '|' || coalesce(worker_id, 'none') || '|' || coalesce(started_at::text, 'none') from public.runs where id = '${STAGE_D_GOV_CAPTURE_RUN_ID}'::uuid;" 2> /dev/null | tr -d '[:space:]')" \
    || gov_state="READ_FAILED"
  case "${gov_state}" in
    "queued|none|none|none")
      capture_proven=1; echo "OK: the Government capture run is still PREPARED and unclaimed" ;;
    "cancelled|none|none|none")
      capture_proven=1; echo "OK: the Government capture run is terminally RETIRED and unclaimed" ;;
    *)
      note_failure "the Government capture run is '${gov_state:-unreadable}' — expected prepared or retired and unclaimed" ;;
  esac
elif [ -z "${recorded_run_id}" ]; then
  # The ONE documented exception. No run was created, so nothing could have
  # been launched and nothing could have claimed the capture; and with the
  # probe already gone there is no credentialed reader left to ask. Stated,
  # never silently skipped, and it does NOT count as a proof.
  echo "NOT APPLICABLE: no run was created and the db probe is already absent, so nothing could have"
  echo "                claimed the capture and no credentialed reader remains."
  echo "                To verify anyway, set STAGE_D_READONLY_DATABASE_URL_ENV and rerun."
else
  note_failure "run ${recorded_run_id} was created but the Government capture posture cannot be checked (the db probe is gone and no read-only database URL was supplied) — UNVERIFIED"
fi

echo "== 4. Delete the disposable probe jobs"
for job in "${STAGE_D_DB_PROBE_JOB}" "${STAGE_D_GW_PROBE_JOB}"; do
  gcloud run jobs delete "${job}" \
    --project="${STAGE_D_PROJECT}" --region="${STAGE_D_REGION}" --quiet \
    || echo "note: deleting ${job} failed (may already be absent); the postcondition verifies."
done

echo "== 5. PROVE both probe jobs are absent (missing cleanup fails this step)"
final_listing="$(gcloud run jobs list --project="${STAGE_D_PROJECT}" --region="${STAGE_D_REGION}" \
  --format='value(metadata.name)' 2>/dev/null)" || final_listing="__LISTING_FAILED__"
if [ "${final_listing}" = "__LISTING_FAILED__" ]; then
  note_failure "could not list Cloud Run jobs — probe-job absence is UNVERIFIED; failing closed"
else
  for job in "${STAGE_D_DB_PROBE_JOB}" "${STAGE_D_GW_PROBE_JOB}"; do
    if printf '%s\n' "${final_listing}" | grep -qx "${job}"; then
      note_failure "disposable probe job ${job} still EXISTS after cleanup — delete it before closing Stage D"
    else
      echo "OK: ${job} is absent"
    fi
  done
fi

echo "== 6. Verdict"
if [ "${lockdown_failures}" -gt 0 ]; then
  echo "STAGE D LOCKDOWN INCOMPLETE: ${lockdown_failures} critical step(s) failed — production may NOT be fully locked down. Investigate and re-run immediately." >&2
  exit 1
fi
if [ "${capture_proven}" -ne 1 ] || { [ -n "${recorded_run_id}" ] && [ "${run_terminal_proven}" -ne 1 ]; }; then
  # Flags are off and the probes are gone, but a required database proof
  # was not obtained. Say exactly that instead of claiming a complete
  # lockdown.
  echo "STAGE D LOCKDOWN PARTIAL: fail-closed posture verified and both probes proven absent, but a required"
  echo "database proof was NOT obtained (see steps 2 and 3). Verify before closing Stage D:"
  if [ -n "${recorded_run_id}" ]; then
    echo "  select status, worker_id, lease_expires_at from public.runs where id = '${recorded_run_id}';"
    echo "    expected: a terminal status"
    echo "  select count(*) from public.model_call_budget_reservations"
    echo "   where run_id = '${recorded_run_id}' and status = 'reserved';   expected: 0"
  fi
  echo "  select status, launch_state, worker_id, started_at from public.runs where id = '${STAGE_D_GOV_CAPTURE_RUN_ID}';"
  echo "    expected: (queued, none, NULL, NULL) or (cancelled, none, NULL, NULL)"
  exit 2
fi
echo "STAGE D LOCKDOWN COMPLETE: fail-closed posture verified, database run terminal with zero active runs and zero"
echo "dangling reservations, Government capture proven unclaimed, both disposable probes deleted and proven absent."
echo "Record every mutation in docs/production-readiness/STAGE_D_AUTHORIZATION.md."

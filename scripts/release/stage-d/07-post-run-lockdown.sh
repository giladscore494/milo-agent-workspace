#!/usr/bin/env bash
# Stage D step 7: IMMEDIATE post-run lockdown. Returns production to the
# safest documented posture, PROVES the prepared Government capture was
# never claimed, removes every disposable resource, and PROVES that too.
#
# Ordering is deliberate and load-bearing:
#   1. kill switch — flags off, provider keys unbound, zero active
#      executions, every postcondition verified;
#   2. Government-capture proof — run through the db-probe WHILE IT STILL
#      EXISTS. The probe is the only credentialed database reader here, so
#      checking after deleting it would be checking with nothing;
#   3. delete both probes — unconditionally, even if step 2 failed: a
#      failed check is never a reason to leave a credentialed probe job
#      standing;
#   4. prove both probes absent from a fresh listing;
#   5. verdict — LOCKDOWN COMPLETE only if every step above was PROVEN.
#
# "LOCKDOWN COMPLETE" is never printed on an unverified capture posture.
# A missing read-only check is a BLOCKING failure, not a MANUAL note —
# the one documented exception is stated and enforced in step 2.
#
# Safe and idempotent to rerun.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
# shellcheck source=stage-d-env.sh
source ./stage-d-env.sh

lockdown_failures=0
note_failure() {
  lockdown_failures=$((lockdown_failures + 1))
  echo "LOCKDOWN CRITICAL: $1" >&2
}

echo "== 1. Kill switch (paid off, catalog off, run creation off, launcher disabled, provider keys unbound, zero active executions)"
# One authoritative fail-closed implementation, reused rather than
# re-spelled: kill-switch.sh verifies every postcondition itself and exits
# non-zero if any of them is unproven.
if ! ./kill-switch.sh; then
  note_failure "kill-switch.sh did not complete — production may NOT be fully fail-closed"
fi

echo "== 2. Prepared Government capture run — PROVEN never claimed (before the probe is deleted)"
# Does a run exist that could conceivably have touched anything? The only
# way the capture could be claimed is a Worker launched for some run, so
# the recorded run id decides whether an unavailable check is fatal.
recorded_run_id=""
if [ -n "${STAGE_D_WORKDIR:-}" ] && [ -r "${STAGE_D_WORKDIR}/state.json" ]; then
  recorded_run_id="$(python3 ./state_file.py "${STAGE_D_WORKDIR}/state.json" read run_id)"
fi

# Is the db probe still present to ask?
db_probe_present=0
probe_listing="$(gcloud run jobs list --project="${STAGE_D_PROJECT}" --region="${STAGE_D_REGION}" \
  --format='value(metadata.name)' 2>/dev/null)" || probe_listing="__LISTING_FAILED__"
if [ "${probe_listing}" != "__LISTING_FAILED__" ] && printf '%s\n' "${probe_listing}" | grep -qx "${STAGE_D_DB_PROBE_JOB}"; then
  db_probe_present=1
fi

capture_proven=0
if [ "${db_probe_present}" -eq 1 ]; then
  govcheck_log="$(mktemp)"
  if gcloud run jobs execute "${STAGE_D_DB_PROBE_JOB}" \
      --project="${STAGE_D_PROJECT}" --region="${STAGE_D_REGION}" \
      --update-env-vars="^:::^STAGE_D_MODE=govcheck:::STAGE_D_GOV_CAPTURE_RUN_ID=${STAGE_D_GOV_CAPTURE_RUN_ID}:::STAGE_D_GOV_CAPTURE_KEY=${STAGE_D_GOV_CAPTURE_KEY}:::STAGE_D_IDEMPOTENCY_KEY=${STAGE_D_IDEMPOTENCY_KEY}" \
      --wait > /dev/null 2>&1; then
    # The job's exit status alone is not the verdict: read the structured
    # record, exactly as the evidence gate does.
    gcloud logging read \
      "resource.type=cloud_run_job AND resource.labels.job_name=${STAGE_D_DB_PROBE_JOB}" \
      --project="${STAGE_D_PROJECT}" --format='json(textPayload,jsonPayload)' \
      --order=desc --limit=50 > "${govcheck_log}" 2>/dev/null || true
    if python3 ./govcheck_verdict.py < "${govcheck_log}"; then
      capture_proven=1
      echo "OK: the Government capture run is proven prepared-and-unclaimed or retired-and-unclaimed"
    else
      note_failure "the Government capture posture check FAILED — investigate immediately, the capture may have been claimed"
    fi
  else
    note_failure "the Government capture posture check could not be executed — the posture is UNVERIFIED"
  fi
  rm -f "${govcheck_log}"
elif [ -n "${STAGE_D_READONLY_DATABASE_URL_ENV:-}" ] && [ -n "${!STAGE_D_READONLY_DATABASE_URL_ENV:-}" ] && command -v psql > /dev/null 2>&1; then
  # Fallback: the operator's own read-only connection.
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
  # The ONE documented exception. No run was ever created, so nothing
  # could have been launched and nothing could have claimed the capture;
  # and with the probe already gone there is no credentialed reader left
  # to ask. This is stated, not silently skipped, and it does NOT count as
  # a proof — the verdict below downgrades accordingly.
  echo "NOT APPLICABLE: no run was created (state.json records no run id) and the db probe is already absent,"
  echo "                so nothing could have claimed the capture and no credentialed reader remains."
  echo "                To verify anyway, set STAGE_D_READONLY_DATABASE_URL_ENV and rerun."
else
  note_failure "run ${recorded_run_id} was created but the Government capture posture cannot be checked (the db probe is gone and no read-only database URL was supplied) — UNVERIFIED"
fi

echo "== 3. Delete the disposable probe jobs"
# Unconditional: a failed capture check is never a reason to leave a
# credentialed probe job standing.
for job in "${STAGE_D_DB_PROBE_JOB}" "${STAGE_D_GW_PROBE_JOB}"; do
  gcloud run jobs delete "${job}" \
    --project="${STAGE_D_PROJECT}" --region="${STAGE_D_REGION}" --quiet \
    || echo "note: deleting ${job} failed (may already be absent); the postcondition verifies."
done

echo "== 4. PROVE both probe jobs are absent (missing cleanup fails this step)"
# Never infer absence from the delete command's exit status. List what
# Cloud Run actually holds and fail closed if either probe is still there
# OR if the listing itself could not be obtained.
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

echo "== 5. Verdict"
if [ "${lockdown_failures}" -gt 0 ]; then
  echo "STAGE D LOCKDOWN INCOMPLETE: ${lockdown_failures} critical step(s) failed — production may NOT be fully locked down. Investigate and re-run immediately." >&2
  exit 1
fi
if [ "${capture_proven}" -ne 1 ]; then
  # Flags are off and the probes are gone, but the capture posture was not
  # PROVEN. Say exactly that instead of claiming a complete lockdown.
  echo "STAGE D LOCKDOWN PARTIAL: fail-closed posture verified and both probes proven absent, but the Government"
  echo "capture posture was NOT proven (see step 2). Verify it before closing Stage D:"
  echo "  select status, launch_state, worker_id, started_at from public.runs where id = '${STAGE_D_GOV_CAPTURE_RUN_ID}';"
  echo "  expected: (queued, none, NULL, NULL) or (cancelled, none, NULL, NULL)"
  exit 2
fi
echo "STAGE D LOCKDOWN COMPLETE: fail-closed posture verified, Government capture proven unclaimed, both disposable probes deleted and proven absent."
echo "Record every mutation in docs/production-readiness/STAGE_D_AUTHORIZATION.md."

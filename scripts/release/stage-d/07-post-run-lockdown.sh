#!/usr/bin/env bash
# Stage D step 7: IMMEDIATE post-run lockdown. Returns production to the
# safest documented posture, removes every disposable resource, and then
# PROVES both. The release images stay deployed (they are the signed-off
# release); budget-cap variables stay set (inert while flags are off).
#
# Unlike a bare kill switch this step also deletes the disposable probe
# jobs and VERIFIES THEY ARE GONE. Missing cleanup is a failure, not a
# warning: a surviving probe job is a standing, credentialed path into
# production data, and Stage C's history shows exactly how a skipped
# cleanup step leaves stale probes behind.
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

echo "== 2. Delete the disposable probe jobs"
for job in "${STAGE_D_DB_PROBE_JOB}" "${STAGE_D_GW_PROBE_JOB}"; do
  # A delete failure is tolerated here (the usual cause is "already
  # absent"); step 3 independently proves absence and is authoritative.
  gcloud run jobs delete "${job}" \
    --project="${STAGE_D_PROJECT}" --region="${STAGE_D_REGION}" --quiet \
    || echo "note: deleting ${job} failed (may already be absent); the postcondition verifies."
done

echo "== 3. PROVE both probe jobs are absent (missing cleanup fails this step)"
# Never infer absence from the delete command's exit status. List what
# Cloud Run actually holds and fail closed if either probe is still there
# OR if the listing itself could not be obtained.
listing="$(mktemp)"
trap 'rm -f "${listing}"' EXIT
if ! gcloud run jobs list --project="${STAGE_D_PROJECT}" --region="${STAGE_D_REGION}" \
    --format='value(metadata.name)' > "${listing}"; then
  note_failure "could not list Cloud Run jobs — probe-job absence is UNVERIFIED; failing closed"
else
  for job in "${STAGE_D_DB_PROBE_JOB}" "${STAGE_D_GW_PROBE_JOB}"; do
    if grep -qx "${job}" "${listing}"; then
      note_failure "disposable probe job ${job} still EXISTS after cleanup — delete it before closing Stage D"
    else
      echo "OK: ${job} is absent"
    fi
  done
fi

echo "== 4. Prepared Government capture run still untouched (read-only)"
# The capture run must be exactly as Stage D found it (or terminally
# retired by resolve-government-capture.sh). This check needs database
# access, which the deleted db-probe provided — so it runs against the
# operator's own read-only connection instead, and says so plainly when it
# cannot run rather than claiming a pass it did not obtain.
if [ -n "${STAGE_D_READONLY_DATABASE_URL_ENV:-}" ] && [ -n "${!STAGE_D_READONLY_DATABASE_URL_ENV:-}" ] && command -v psql > /dev/null 2>&1; then
  gov_state="$(psql -X -A -t -v ON_ERROR_STOP=1 "${!STAGE_D_READONLY_DATABASE_URL_ENV}" \
    -c "select status || '|' || launch_state || '|' || coalesce(worker_id, 'none') || '|' || coalesce(started_at::text, 'none') from public.runs where id = '${STAGE_D_GOV_CAPTURE_RUN_ID}'::uuid;" 2> /dev/null | tr -d '[:space:]')" \
    || gov_state="READ_FAILED"
  case "${gov_state}" in
    "queued|none|none|none")  echo "OK: Government capture run is still PREPARED and unclaimed" ;;
    "cancelled|none|none|none") echo "OK: Government capture run is terminally RETIRED and unclaimed" ;;
    READ_FAILED|"")           note_failure "could not read the Government capture run — its posture is UNVERIFIED; failing closed" ;;
    *)                        note_failure "Government capture run is in an unexpected posture '${gov_state}' — investigate immediately" ;;
  esac
else
  echo "MANUAL: set STAGE_D_READONLY_DATABASE_URL_ENV to the name of an env var holding a READ-ONLY"
  echo "        connection string (and have psql available) to verify automatically. Otherwise run:"
  echo "          select status, launch_state, worker_id, started_at from public.runs"
  echo "           where id = '${STAGE_D_GOV_CAPTURE_RUN_ID}';"
  echo "        Expected: (queued, none, NULL, NULL) or (cancelled, none, NULL, NULL). Anything else"
  echo "        means the capture was claimed — investigate immediately."
fi

echo "== 5. Verdict"
if [ "${lockdown_failures}" -gt 0 ]; then
  echo "STAGE D LOCKDOWN INCOMPLETE: ${lockdown_failures} critical step(s) failed — production may NOT be fully locked down. Investigate and re-run immediately." >&2
  exit 1
fi
echo "STAGE D LOCKDOWN COMPLETE: fail-closed posture verified, both disposable probes deleted and proven absent."
echo "Record every mutation in docs/production-readiness/STAGE_D_AUTHORIZATION.md."

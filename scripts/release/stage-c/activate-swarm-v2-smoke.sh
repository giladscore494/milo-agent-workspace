#!/usr/bin/env bash
# Manual Swarm V2 smoke activation.  This script never creates a run or a
# Worker execution.  It establishes a known-good fail-closed API revision
# before narrowly connecting the Worker to its provider.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
# shellcheck source=stage-c-env.sh
source ./stage-c-env.sh

PROVIDER_SECRET_ALIASES=(KIMI_API_KEY MOONSHOT_API_KEY)
cleanup_started=0
original_status=0
cleanup() {
  original_status=$?
  trap - EXIT ERR INT TERM
  [ "${cleanup_started}" -eq 0 ] || exit "${original_status}"
  cleanup_started=1
  if [ "${original_status}" -ne 0 ]; then
    ./kill-switch.sh || echo "SWARM SMOKE CLEANUP FAILED (original status ${original_status})" >&2
    if [ -n "${SWARM_SMOKE_PROBE_JOB:-}" ]; then
      gcloud run jobs delete "${SWARM_SMOKE_PROBE_JOB}" --project="${STAGE_C_PROJECT}" \
        --region="${STAGE_C_REGION}" --quiet || echo "probe cleanup failed (original status ${original_status})" >&2
    fi
  fi
  exit "${original_status}"
}
# Protection is armed before even the first read-only preflight, and therefore
# necessarily before every mutation.
trap cleanup EXIT ERR INT TERM

# Execution control is not part of this smoke's real path: run creation launches
# the job and the Worker persists lifecycle/evidence through its guarded
# repository lease.  Internal HTTP mutation routes are not used.  Refuse an
# operator attempt to widen the surface; if widened in a future audited flow,
# both auth settings must first be present and non-empty.
if [ "${SWARM_SMOKE_ENABLE_EXECUTION_CONTROL:-false}" != false ]; then
  api_json="$(gcloud run services describe "${STAGE_C_API_SERVICE}" --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" --format=json)"
  python3 -c 'import json,sys
e=json.loads(sys.stdin.read())["spec"]["template"]["spec"]["containers"][0].get("env",[])
v={x["name"]:x.get("value","").strip() for x in e}
assert v.get("MILO_WORKER_AUDIENCE"), "MILO_WORKER_AUDIENCE missing"
assert v.get("MILO_APPROVED_WORKER_IDENTITIES"), "MILO_APPROVED_WORKER_IDENTITIES missing"' <<<"${api_json}"
  echo "REFUSED: execution control is not required for the Swarm V2 smoke" >&2
  exit 2
fi

flags_off="MILO_ENABLE_RUN_CREATION=false,MILO_ENABLE_PROPOSAL_MUTATIONS=false,MILO_ENABLE_PROPOSAL_READS=false,MILO_ENABLE_RUN_CANCELLATION=false,MILO_ENABLE_EXECUTION_CONTROL=false,MILO_ENABLE_PAID_EXECUTION=false,JOB_LAUNCHER=disabled"

# Create a fresh, fully specified fail-closed revision without traffic first.
gcloud run services update "${STAGE_C_API_SERVICE}" --project="${STAGE_C_PROJECT}" \
  --region="${STAGE_C_REGION}" --no-traffic --update-env-vars="${flags_off}"
for alias in "${PROVIDER_SECRET_ALIASES[@]}"; do
  gcloud run services update "${STAGE_C_API_SERVICE}" --project="${STAGE_C_PROJECT}" \
    --region="${STAGE_C_REGION}" --no-traffic --remove-secrets="${alias}" 2>/dev/null || true
  gcloud run services update "${STAGE_C_API_SERVICE}" --project="${STAGE_C_PROJECT}" \
    --region="${STAGE_C_REGION}" --no-traffic --remove-env-vars="${alias}" 2>/dev/null || true
done
baseline_revision="$(gcloud run services describe "${STAGE_C_API_SERVICE}" --project="${STAGE_C_PROJECT}" \
  --region="${STAGE_C_REGION}" --format='value(status.latestReadyRevisionName)')"
test -n "${baseline_revision}"
gcloud run services update-traffic "${STAGE_C_API_SERVICE}" --project="${STAGE_C_PROJECT}" \
  --region="${STAGE_C_REGION}" --to-revisions="${baseline_revision}=100"
./kill-switch.sh

# Worker first, but any later failure invokes the complete kill switch.
gcloud run jobs update "${STAGE_C_WORKER_JOB}" --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" \
  --update-env-vars="MILO_ENABLE_PAID_EXECUTION=${STAGE_C_ON},${STAGE_C_CAPS},${STAGE_C_WORKER_PROVIDER_LIMITS}" \
  --update-secrets="KIMI_API_KEY=KIMI_API_KEY:latest"
gcloud run services update "${STAGE_C_API_SERVICE}" --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" \
  --update-env-vars="JOB_LAUNCHER=cloud_run,MILO_ENABLE_RUN_CREATION=${STAGE_C_ON},MILO_ENABLE_EXECUTION_CONTROL=false,${STAGE_C_CAPS}"
./03b-verify-stage-c-posture.sh
echo "OK: minimum Swarm V2 smoke surface activated; no run or execution created."

#!/usr/bin/env bash
# Stage C step 6: full post-run validation evidence. Read-only except for
# probe-job executions. Usage: ./06-collect-evidence.sh <RUN_ID>
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
# shellcheck source=stage-c-env.sh
source ./stage-c-env.sh

RUN_ID="${1:?usage: 06-collect-evidence.sh <RUN_ID>}"

run_probe() {
  local job="$1"; shift
  local env_overrides=""
  for kv in "$@"; do env_overrides+="${env_overrides:+,}${kv}"; done
  local exec_name
  exec_name="$(gcloud run jobs execute "${job}" \
    --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" \
    --update-env-vars="${env_overrides}" \
    --wait --format='value(metadata.name)')"
  gcloud logging read \
    "resource.type=cloud_run_job AND resource.labels.job_name=${job} AND labels.\"run.googleapis.com/execution_name\"=${exec_name}" \
    --project="${STAGE_C_PROJECT}" --format='value(textPayload)' --order=asc \
    | grep -v '^$' || true
}

echo "== DB evidence (lifecycle, budget, ledger, leak scan)"
run_probe "${STAGE_C_DB_PROBE_JOB}" "STAGE_C_MODE=evidence" "STAGE_C_RUN_ID=${RUN_ID}" | tee /tmp/stagec-evidence.log

echo "== Post-completion idempotent replay (must return the same run, no new execution)"
USER_ID="$(python3 -c 'import json;print(json.loads(open("/tmp/stagec-setup.log").readlines()[-1])["user_id"])')"
CONVERSATION_ID="$(python3 -c 'import json;print(json.loads(open("/tmp/stagec-setup.log").readlines()[-1])["conversation_id"])')"
run_probe "${STAGE_C_GW_PROBE_JOB}" \
  "STAGE_C_MODE=replay" "STAGE_C_RUN_ID=${RUN_ID}" "STAGE_C_USER_ID=${USER_ID}" "STAGE_C_CONVERSATION_ID=${CONVERSATION_ID}" \
  | tee /tmp/stagec-replay.log

echo "== Execution count must still be exactly 1"
gcloud run jobs executions list --job="${STAGE_C_WORKER_JOB}" \
  --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" \
  --format='table(metadata.name,status.succeededCount,status.failedCount)'

echo "== Worker log secret-leak scan (counts only; no values printed)"
gcloud logging read \
  "resource.type=cloud_run_job AND resource.labels.job_name=${STAGE_C_WORKER_JOB}" \
  --project="${STAGE_C_PROJECT}" --format='value(textPayload)' --limit=5000 \
  | grep -c -E 'sk-[A-Za-z0-9]|KIMI_API_KEY=|sb_secret' \
  && echo "WARNING: possible secret markers in worker logs — inspect manually" \
  || echo "OK: no secret markers in worker logs"

echo
echo "Validation checklist to record in STAGE_C_ACCEPTANCE.md:"
echo "  - run status/terminal transition, attempt=1, worker claim + heartbeats"
echo "  - event_types sequence (run_created … terminal), checkpoint phases"
echo "  - reservations_by_status: settled only, dangling_reservations=0"
echo "  - ledger decisions + token sums vs run.usage snapshot"
echo "  - reserved_cost_sum vs settled_cost_sum vs MILO_MAX_COST_PER_RUN"
echo "  - actual provider cost (verify in the Moonshot console, external)"
echo "  - total_runs=1, executions=1, replay ok=true"
echo "  - secret_marker_hits empty in DB events and worker logs"

#!/usr/bin/env bash
# Stage C emergency kill switch — run at ANY sign of trouble (invariant
# failure, unexpected cost, anomalous behavior). Applies ROLLBACK.md
# emergency order: paid off → run creation off → launcher off → provider
# key binding removed → cancel any running worker execution.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
# shellcheck source=stage-c-env.sh
source ./stage-c-env.sh

gcloud run jobs update "${STAGE_C_WORKER_JOB}" \
  --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" \
  --update-env-vars="MILO_ENABLE_PAID_EXECUTION=false" \
  --remove-secrets="KIMI_API_KEY" &

gcloud run services update "${STAGE_C_API_SERVICE}" \
  --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" \
  --update-env-vars="MILO_ENABLE_RUN_CREATION=false,JOB_LAUNCHER=disabled" &
wait

for execution in $(gcloud run jobs executions list --job="${STAGE_C_WORKER_JOB}" \
    --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" \
    --filter="status.completionTime=null" --format='value(metadata.name)'); do
  echo "Cancelling running execution ${execution}"
  gcloud run jobs executions cancel "${execution}" \
    --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" --quiet || true
done

echo "KILL SWITCH APPLIED: paid off, key unbound, run creation off, launcher disabled."

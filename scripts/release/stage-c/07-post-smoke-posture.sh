#!/usr/bin/env bash
# Stage C step 7: return production to the safest documented post-smoke
# posture (ROLLBACK.md emergency flag order) and remove disposable
# resources. The release images stay deployed (they are the signed-off
# Stage B code); budget-cap variables stay set (inert while flags are off).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
# shellcheck source=stage-c-env.sh
source ./stage-c-env.sh

echo "== 1. Paid execution OFF + provider-key binding removed (worker)"
gcloud run jobs update "${STAGE_C_WORKER_JOB}" \
  --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" \
  --update-env-vars="MILO_ENABLE_PAID_EXECUTION=false" \
  --remove-secrets="KIMI_API_KEY"

echo "== 2. Run creation OFF + launcher disabled (API)"
gcloud run services update "${STAGE_C_API_SERVICE}" \
  --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" \
  --update-env-vars="MILO_ENABLE_RUN_CREATION=false,JOB_LAUNCHER=disabled"

echo "== 3. Delete disposable probe jobs"
gcloud run jobs delete "${STAGE_C_DB_PROBE_JOB}" --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" --quiet || true
gcloud run jobs delete "${STAGE_C_GW_PROBE_JOB}" --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" --quiet || true

echo "== 4. Verify fail-closed posture"
gcloud run services describe "${STAGE_C_API_SERVICE}" \
  --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" --format=json \
  | python3 -c '
import json, sys
env = {e["name"]: e.get("value") for e in json.load(sys.stdin)["spec"]["template"]["spec"]["containers"][0]["env"] if "value" in e}
assert env.get("MILO_ENABLE_RUN_CREATION") == "false"
assert env.get("JOB_LAUNCHER") == "disabled"
assert env.get("MILO_ENABLE_PAID_EXECUTION") == "false"
print("OK: API fail-closed")
'
gcloud run jobs describe "${STAGE_C_WORKER_JOB}" \
  --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" --format=json \
  | python3 -c '
import json, sys
c = json.load(sys.stdin)["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]
env = {e["name"]: e.get("value") for e in c["env"] if "value" in e}
secret_refs = {e["name"] for e in c["env"] if "valueFrom" in e}
assert env.get("MILO_ENABLE_PAID_EXECUTION") == "false"
assert "KIMI_API_KEY" not in secret_refs, "provider key still bound!"
print("OK: worker fail-closed, provider key unbound")
'
echo "Post-smoke posture restored. Record every mutation in STAGE_C_ACCEPTANCE.md."

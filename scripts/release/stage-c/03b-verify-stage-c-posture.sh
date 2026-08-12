#!/usr/bin/env bash
# Stage C step 3b: READ-ONLY verification of the enabled Stage C posture
# (run after the manual commands in 03-enable-stage-c.md). Asserts the
# minimum surface and nothing more. Mutates nothing.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
# shellcheck source=stage-c-env.sh
source ./stage-c-env.sh

echo "== Provider secret IAM (worker-only)"
gcloud secrets get-iam-policy KIMI_API_KEY --project="${STAGE_C_PROJECT}" --format=json \
  | python3 -c 'import json,sys; p=json.load(sys.stdin); a=[m for b in p["bindings"] if b["role"]=="roles/secretmanager.secretAccessor" for m in b["members"]]; assert a==["serviceAccount:'"${STAGE_C_WORKER_SA}"'"], a; print("OK: worker-only")'

echo "== API posture"
gcloud run services describe "${STAGE_C_API_SERVICE}" \
  --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" --format=json \
  | python3 -c '
import json, sys
env = {e["name"]: e.get("value") for e in json.load(sys.stdin)["spec"]["template"]["spec"]["containers"][0]["env"] if "value" in e}
assert env.get("MILO_ENABLE_RUN_CREATION") == "true", "run creation not enabled yet (see 03-enable-stage-c.md)"
assert env.get("MILO_ENABLE_PAID_EXECUTION") == "false", "the API must NEVER carry the paid flag"
assert env.get("JOB_LAUNCHER") == "cloud_run", env.get("JOB_LAUNCHER")
for k in ("MILO_ENABLE_PROPOSAL_MUTATIONS","MILO_ENABLE_PROPOSAL_READS","MILO_ENABLE_RUN_CANCELLATION","MILO_ENABLE_EXECUTION_CONTROL"):
    assert env.get(k) == "false", (k, env.get(k))
print("OK: API surface minimal (run creation + launcher only)")
'

echo "== Worker posture"
gcloud run jobs describe "${STAGE_C_WORKER_JOB}" \
  --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" --format=json \
  | python3 -c '
import json, sys
c = json.load(sys.stdin)["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]
env = {e["name"]: e.get("value") for e in c["env"] if "value" in e}
secret_refs = {e["name"] for e in c["env"] if "valueFrom" in e}
assert env.get("MILO_ENABLE_PAID_EXECUTION") == "true", "paid flag not enabled yet"
assert "KIMI_API_KEY" in secret_refs, "provider key not bound to the worker"
missing = [k for k in ("MILO_MAX_MODEL_CALLS_PER_RUN","MILO_MAX_TOTAL_TOKENS_PER_RUN","MILO_MAX_ESTIMATED_COST_PER_RUN","MILO_MAX_COST_PER_RUN","MILO_MAX_RUN_DURATION_SECONDS","MILO_MAX_RETRIES","MILO_DAILY_USER_BUDGET","MILO_DAILY_PROJECT_BUDGET") if not env.get(k)]
assert not missing, f"caps missing on worker: {missing}"
assert env.get("MILO_MAX_COST_PER_RUN") == "3.00", env.get("MILO_MAX_COST_PER_RUN")
print("OK: worker — paid enabled, key bound (worker only), caps active")
'
echo "Stage C posture verified. Proceed to 04-create-probes.sh."

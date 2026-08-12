#!/usr/bin/env bash
# Stage C step 5: execute exactly ONE controlled paid run and monitor it to
# a terminal state. Refuses to run if any worker execution already exists.
#
# Sequence:
#   1. re-verify launch invariants (secret binding, IAM, caps, zero execs);
#   2. db-probe preflight (migration surface) + setup (test user/project/
#      conversation) — capture USER_ID / CONVERSATION_ID from its log;
#   3. gw-probe create (POST run + immediate idempotent replay check);
#   4. gw-probe poll until terminal (also watch the worker execution).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
# shellcheck source=stage-c-env.sh
source ./stage-c-env.sh

run_probe() { # job [KEY=VALUE ...] — execute, wait, print the execution log
  local job="$1"; shift
  local env_overrides=""
  for kv in "$@"; do env_overrides+="${env_overrides:+,}${kv}"; done
  local exec_name
  exec_name="$(gcloud run jobs execute "${job}" \
    --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" \
    ${env_overrides:+--update-env-vars="${env_overrides}"} \
    --wait --format='value(metadata.name)')"
  gcloud logging read \
    "resource.type=cloud_run_job AND resource.labels.job_name=${job} AND labels.\"run.googleapis.com/execution_name\"=${exec_name}" \
    --project="${STAGE_C_PROJECT}" --format='value(textPayload)' --order=asc \
    | grep -v '^$' || true
}

echo "== 1. Launch invariants"
executions="$(gcloud run jobs executions list --job="${STAGE_C_WORKER_JOB}" \
  --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" --format='value(metadata.name)' | sed '/^$/d' | wc -l)"
test "${executions}" = "0" || { echo "REFUSING: ${executions} worker execution(s) already exist — a paid run may already have happened"; exit 1; }
gcloud run jobs describe "${STAGE_C_WORKER_JOB}" \
  --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" --format=json \
  | python3 -c '
import json, sys
c = json.load(sys.stdin)["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]
env = {e["name"]: e.get("value") for e in c["env"] if "value" in e}
secret_refs = {e["name"] for e in c["env"] if "valueFrom" in e}
assert env.get("MILO_ENABLE_PAID_EXECUTION") == "true", "run 03-enable-stage-c.sh first"
assert "KIMI_API_KEY" in secret_refs, "provider key not bound to the worker"
missing = [k for k in ("MILO_MAX_MODEL_CALLS_PER_RUN","MILO_MAX_TOTAL_TOKENS_PER_RUN","MILO_MAX_ESTIMATED_COST_PER_RUN","MILO_MAX_COST_PER_RUN","MILO_MAX_RUN_DURATION_SECONDS","MILO_MAX_RETRIES","MILO_DAILY_USER_BUDGET","MILO_DAILY_PROJECT_BUDGET") if not env.get(k)]
assert not missing, f"caps missing on worker: {missing}"
print("OK: worker caps + paid flag + key binding active")
'
gcloud secrets get-iam-policy KIMI_API_KEY --project="${STAGE_C_PROJECT}" --format=json \
  | python3 -c 'import json,sys; p=json.load(sys.stdin); a=[m for b in p["bindings"] if b["role"]=="roles/secretmanager.secretAccessor" for m in b["members"]]; assert a==["serviceAccount:'"${STAGE_C_WORKER_SA}"'"], a; print("OK: provider secret worker-only")'
gcloud run jobs get-iam-policy "${STAGE_C_WORKER_JOB}" --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" --format=json \
  | python3 -c 'import json,sys; p=json.load(sys.stdin); b=p.get("bindings",[]); assert b==[{"members":["serviceAccount:'"${STAGE_C_API_SA}"'"],"role":"roles/run.jobsExecutorWithOverrides"}], b; print("OK: launcher IAM unchanged (API SA only)")'

echo "== 2. DB preflight + test-data setup"
run_probe "${STAGE_C_DB_PROBE_JOB}" "STAGE_C_MODE=preflight" | tee /tmp/stagec-preflight.log
grep -q '"ok": true' /tmp/stagec-preflight.log || { echo "BLOCKED: preflight failed"; exit 1; }
run_probe "${STAGE_C_DB_PROBE_JOB}" "STAGE_C_MODE=setup" | tee /tmp/stagec-setup.log
USER_ID="$(python3 -c 'import json;print(json.loads(open("/tmp/stagec-setup.log").readlines()[-1])["user_id"])')"
CONVERSATION_ID="$(python3 -c 'import json;print(json.loads(open("/tmp/stagec-setup.log").readlines()[-1])["conversation_id"])')"
echo "test user=${USER_ID} conversation=${CONVERSATION_ID}"

echo "== 3. Create the ONE run (with immediate idempotent replay)"
run_probe "${STAGE_C_GW_PROBE_JOB}" \
  "STAGE_C_MODE=create" "STAGE_C_USER_ID=${USER_ID}" "STAGE_C_CONVERSATION_ID=${CONVERSATION_ID}" \
  | tee /tmp/stagec-create.log
RUN_ID="$(python3 -c 'import json;print(json.loads(open("/tmp/stagec-create.log").readlines()[-1])["run_id"])')"
echo "RUN_ID=${RUN_ID}"

echo "== 4. Monitor to terminal state"
run_probe "${STAGE_C_GW_PROBE_JOB}" \
  "STAGE_C_MODE=poll" "STAGE_C_RUN_ID=${RUN_ID}" "STAGE_C_USER_ID=${USER_ID}" "STAGE_C_CONVERSATION_ID=${CONVERSATION_ID}" \
  | tee /tmp/stagec-poll.log

echo "== Worker execution record"
gcloud run jobs executions list --job="${STAGE_C_WORKER_JOB}" \
  --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" \
  --format='table(metadata.name,status.startTime,status.completionTime,status.succeededCount,status.failedCount)'

echo
echo "Run ${RUN_ID} reached a terminal state. Next: ./06-collect-evidence.sh ${RUN_ID}"
echo "If ANY invariant failed or cost exceeded the cap: ./kill-switch.sh"

#!/usr/bin/env bash
# Stage C step 5: execute exactly ONE controlled paid run and monitor it to
# a terminal state. Refuses to run if any worker execution already exists.
#
# Sequence:
#   1. re-verify launch invariants: release images + EXACT cap values on
#      both worker and API (verify_caps.py), secret binding, IAM, zero
#      executions;
#   2. db-probe preflight (migration surface + exact zero-prior-run count)
#      + setup (test user/project/conversation) — capture USER_ID /
#      CONVERSATION_ID from its log;
#   3. gw-probe create (POST run + immediate idempotent replay check);
#   4. gw-probe poll until terminal — exits non-zero unless the terminal
#      state is in the acceptance policy; any other outcome means: RUN THE
#      KILL SWITCH (./kill-switch.sh).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
# shellcheck source=stage-c-env.sh
source ./stage-c-env.sh

fail() { echo "STAGE C FAIL: $1"; echo "Operator action: ./kill-switch.sh, then record the failure in STAGE_C_ACCEPTANCE.md."; exit 1; }

run_probe() { # job [KEY=VALUE ...] — execute, wait, print the execution log
  local job="$1"; shift
  # ^@^ delimiter so values may contain commas (e.g. STAGE_C_CAPS).
  local env_overrides=""
  for kv in "$@"; do env_overrides+="${env_overrides:+@}${kv}"; done
  local exec_name
  exec_name="$(gcloud run jobs execute "${job}" \
    --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" \
    ${env_overrides:+--update-env-vars="^@^${env_overrides}"} \
    --wait --format='value(metadata.name)')"
  gcloud logging read \
    "resource.type=cloud_run_job AND resource.labels.job_name=${job} AND labels.\"run.googleapis.com/execution_name\"=${exec_name}" \
    --project="${STAGE_C_PROJECT}" --format='value(textPayload)' --order=asc \
    | grep -v '^$' || true
}

echo "== 1. Launch invariants (images, exact caps on BOTH surfaces, IAM, zero executions)"
executions="$(gcloud run jobs executions list --job="${STAGE_C_WORKER_JOB}" \
  --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" --format='value(metadata.name)' | sed '/^$/d' | wc -l)"
test "${executions}" = "0" || fail "${executions} worker execution(s) already exist — a paid run may already have happened"

worker_json="$(mktemp)"; api_json="$(mktemp)"
trap 'rm -f "${worker_json}" "${api_json}"' EXIT
gcloud run jobs describe "${STAGE_C_WORKER_JOB}" \
  --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" --format=json > "${worker_json}"
gcloud run services describe "${STAGE_C_API_SERVICE}" \
  --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" --format=json > "${api_json}"
# Every Stage C cap compared against the exact expected value from
# stage-c-env.sh, on worker AND API, immediately before run creation; also
# enforces the signed-off release images (production-image blocker) and
# the exact flag posture. Fails on any missing/changed/unexpected value.
python3 ./verify_caps.py --worker-json "${worker_json}" --api-json "${api_json}" \
  || fail "cap/image/posture verification failed — do NOT create the run"

gcloud secrets get-iam-policy KIMI_API_KEY --project="${STAGE_C_PROJECT}" --format=json \
  | python3 -c 'import json,sys; p=json.load(sys.stdin); a=[m for b in p["bindings"] if b["role"]=="roles/secretmanager.secretAccessor" for m in b["members"]]; assert a==["serviceAccount:'"${STAGE_C_WORKER_SA}"'"], a; print("OK: provider secret worker-only")'
gcloud run jobs get-iam-policy "${STAGE_C_WORKER_JOB}" --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" --format=json \
  | python3 -c 'import json,sys; p=json.load(sys.stdin); b=p.get("bindings",[]); assert b==[{"members":["serviceAccount:'"${STAGE_C_API_SA}"'"],"role":"roles/run.jobsExecutorWithOverrides"}], b; print("OK: launcher IAM unchanged (API SA only)")'

echo "== 2. DB preflight (exact zero-prior-run count) + test-data setup"
run_probe "${STAGE_C_DB_PROBE_JOB}" "STAGE_C_MODE=preflight" \
  "STAGE_C_EXPECTED_PRIOR_RUNS=${STAGE_C_EXPECTED_PRIOR_RUNS}" \
  "STAGE_C_IDEMPOTENCY_KEY=${STAGE_C_IDEMPOTENCY_KEY}" \
  | tee /tmp/stagec-preflight.log
grep -q '"ok": true' /tmp/stagec-preflight.log || fail "preflight failed (see log above) — the DB is not in the expected pre-run state"
run_probe "${STAGE_C_DB_PROBE_JOB}" "STAGE_C_MODE=setup" | tee /tmp/stagec-setup.log
USER_ID="$(python3 -c 'import json;print(json.loads(open("/tmp/stagec-setup.log").readlines()[-1])["user_id"])')"
CONVERSATION_ID="$(python3 -c 'import json;print(json.loads(open("/tmp/stagec-setup.log").readlines()[-1])["conversation_id"])')"
echo "test user=${USER_ID} conversation=${CONVERSATION_ID}"

echo "== 3. Create the ONE run (with immediate idempotent replay)"
run_probe "${STAGE_C_GW_PROBE_JOB}" \
  "STAGE_C_MODE=create" "STAGE_C_USER_ID=${USER_ID}" "STAGE_C_CONVERSATION_ID=${CONVERSATION_ID}" \
  "STAGE_C_IDEMPOTENCY_KEY=${STAGE_C_IDEMPOTENCY_KEY}" \
  | tee /tmp/stagec-create.log
RUN_ID="$(python3 -c 'import json;print(json.loads(open("/tmp/stagec-create.log").readlines()[-1])["run_id"])')"
echo "RUN_ID=${RUN_ID}"

echo "== 4. Monitor to terminal state (PASS states only: ${STAGE_C_ACCEPTABLE_TERMINAL_STATES})"
poll_status=0
run_probe "${STAGE_C_GW_PROBE_JOB}" \
  "STAGE_C_MODE=poll" "STAGE_C_RUN_ID=${RUN_ID}" "STAGE_C_USER_ID=${USER_ID}" "STAGE_C_CONVERSATION_ID=${CONVERSATION_ID}" \
  "STAGE_C_ACCEPTABLE_TERMINAL_STATES=${STAGE_C_ACCEPTABLE_TERMINAL_STATES}" \
  | tee /tmp/stagec-poll.log || poll_status=$?

# Belt-and-braces: even if the probe job's exit code is lost, PASS requires
# an explicit acceptable terminal verdict in the poll log.
python3 - <<'PY' || poll_status=1
import json, sys
for line in open("/tmp/stagec-poll.log"):
    line = line.strip()
    if not line.startswith("{"):
        continue
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        continue
    if "terminal" in record and record.get("terminal"):
        sys.exit(0 if record.get("acceptable") is True else 1)
sys.exit(1)
PY

echo "== Worker execution record"
gcloud run jobs executions list --job="${STAGE_C_WORKER_JOB}" \
  --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" \
  --format='table(metadata.name,status.startTime,status.completionTime,status.succeededCount,status.failedCount)'

if [ "${poll_status}" -ne 0 ]; then
  fail "run ${RUN_ID} did not reach an acceptable terminal state (policy: ${STAGE_C_ACCEPTABLE_TERMINAL_STATES})"
fi

echo
echo "Run ${RUN_ID} reached an ACCEPTABLE terminal state."
echo "Next (mandatory acceptance gate): ./06-collect-evidence.sh ${RUN_ID}"

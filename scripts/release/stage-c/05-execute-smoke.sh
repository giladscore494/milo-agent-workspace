#!/usr/bin/env bash
# Stage C step 5: execute exactly ONE controlled paid run and monitor it to
# a terminal state. Refuses to run unless the Worker execution history
# matches the pinned Attempt 5/6 baseline EXACTLY: exactly
# STAGE_C_EXPECTED_PRIOR_EXECUTIONS historical executions, every one
# terminal, zero active — a new active execution before authorization, a
# count mismatch, or an unverifiable listing all block the run.
#
# Sequence:
#   1. re-verify launch invariants: release images + EXACT cap and
#      worker-only provider-limit values (verify_caps.py), secret binding,
#      IAM, exact terminal execution baseline;
#   2. db-probe preflight (migration surface + exact pinned prior-run
#      baseline + zero rows under the Attempt 7 idempotency key)
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
  # Multi-character gcloud env-var delimiter so values may contain commas
  # (e.g. STAGE_C_CAPS). Collision fails closed BEFORE any gcloud call —
  # a value containing the delimiter would be split in transport (the
  # attempt-1 probe-source failure mode).
  local delim=":::"
  local env_overrides=""
  for kv in "$@"; do
    if [[ "${kv}" == *"${delim}"* ]]; then
      echo "STAGE C REFUSED: env override '${kv%%=*}' contains the delimiter '${delim}'" >&2
      exit 1
    fi
    env_overrides+="${env_overrides:+${delim}}${kv}"
  done
  local exec_name
  exec_name="$(gcloud run jobs execute "${job}" \
    --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" \
    ${env_overrides:+--update-env-vars="^${delim}^${env_overrides}"} \
    --wait --format='value(metadata.name)')"
  gcloud logging read \
    "resource.type=cloud_run_job AND resource.labels.job_name=${job} AND labels.\"run.googleapis.com/execution_name\"=${exec_name}" \
    --project="${STAGE_C_PROJECT}" --format='json(textPayload,jsonPayload)' --order=asc \
    | python3 -c '
import json, sys
for record in json.load(sys.stdin):
    if not isinstance(record, dict):
        continue

    payload = record.get("jsonPayload")
    if payload is not None:
        if isinstance(payload, dict) and "stage_c_probe" in payload:
            print(json.dumps(payload, sort_keys=True))
        else:
            print(json.dumps(payload, sort_keys=True), file=sys.stderr)
        continue

    text = record.get("textPayload")
    if not text:
        continue

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        print(text, file=sys.stderr)
        continue

    if isinstance(parsed, dict) and "stage_c_probe" in parsed:
        print(json.dumps(parsed, sort_keys=True))
    else:
        print(text, file=sys.stderr)
'
}

echo "== 1. Launch invariants (images, exact caps + worker provider limits, IAM, exact terminal execution baseline)"
# Exactly the pinned historical executions (Attempts 5+6), every one
# terminal, zero active. More or fewer executions than the baseline, any
# active execution, or an unparseable listing fails closed BEFORE run
# creation — this proves the coming launch will be a one-execution
# increment. Historical terminal executions are never cancelled.
gcloud run jobs executions list --job="${STAGE_C_WORKER_JOB}" \
  --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" --format=json \
  | python3 ./verify_executions.py --expected-total "${STAGE_C_EXPECTED_PRIOR_EXECUTIONS}" \
  || fail "worker execution posture does not match the pinned historical baseline (expected exactly ${STAGE_C_EXPECTED_PRIOR_EXECUTIONS} terminal executions, zero active) — an unexpected or active execution blocks the run"

worker_json="$(mktemp)"; api_json="$(mktemp)"
trap 'rm -f "${worker_json}" "${api_json}"' EXIT
gcloud run jobs describe "${STAGE_C_WORKER_JOB}" \
  --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" --format=json > "${worker_json}"
gcloud run services describe "${STAGE_C_API_SERVICE}" \
  --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" --format=json > "${api_json}"
# Every Stage C cap compared against the exact expected value from
# stage-c-env.sh, on worker AND API, and every worker-only provider limit
# (STAGE_C_WORKER_PROVIDER_LIMITS) exact on the Worker, immediately before
# run creation; also enforces the pinned release images (production-image
# blocker) and the exact flag/provider-secret posture. Fails on any
# missing/changed/unexpected value, including any MILO_PROVIDER_* variable
# on the API.
python3 ./verify_caps.py --worker-json "${worker_json}" --api-json "${api_json}" \
  || fail "cap/image/posture verification failed — do NOT create the run"

gcloud secrets get-iam-policy KIMI_API_KEY --project="${STAGE_C_PROJECT}" --format=json \
  | python3 -c 'import json,sys; p=json.load(sys.stdin); a=[m for b in p["bindings"] if b["role"]=="roles/secretmanager.secretAccessor" for m in b["members"]]; assert a==["serviceAccount:'"${STAGE_C_WORKER_SA}"'"], a; print("OK: provider secret worker-only")'
gcloud run jobs get-iam-policy "${STAGE_C_WORKER_JOB}" --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" --format=json \
  | python3 -c 'import json,sys; p=json.load(sys.stdin); b=p.get("bindings",[]); assert b==[{"members":["serviceAccount:'"${STAGE_C_API_SA}"'"],"role":"roles/run.jobsExecutorWithOverrides"}], b; print("OK: launcher IAM unchanged (API SA only)")'

echo "== 2. DB preflight (exact pinned prior-run baseline + zero Attempt 7 key rows) + test-data setup"
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

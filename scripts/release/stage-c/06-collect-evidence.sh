#!/usr/bin/env bash
# Stage C step 6: EXECUTABLE ACCEPTANCE GATE over the completed run.
# Read-only except for probe-job executions. Exits non-zero if ANY
# acceptance criterion fails:
#   - exactly one authorized run / one worker execution;
#   - expected terminal state;
#   - attempt/claim/heartbeat invariants;
#   - zero dangling reservations;
#   - reservation/ledger/run-usage accounting consistency;
#   - tracked cost <= configured cap; token/call caps respected;
#   - post-completion idempotent replay returns the same run;
#   - zero secret-marker hits (DB events AND worker logs).
# Usage: ./06-collect-evidence.sh <RUN_ID>
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
# shellcheck source=stage-c-env.sh
source ./stage-c-env.sh

RUN_ID="${1:?usage: 06-collect-evidence.sh <RUN_ID>}"

fail() {
  echo "STAGE C ACCEPTANCE GATE: FAIL — $1"
  echo "Operator action: treat the run as FAILED. Run ./kill-switch.sh (if not already fail-closed),"
  echo "then ./07-post-smoke-posture.sh, and record the failure in STAGE_C_ACCEPTANCE.md."
  exit 1
}

run_probe() {
  local job="$1"; shift
  # Multi-character gcloud env-var delimiter so values may contain commas
  # (e.g. STAGE_C_CAPS). Collision fails closed BEFORE any gcloud call.
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
    --update-env-vars="^${delim}^${env_overrides}" \
    --wait --format='value(metadata.name)')"
  gcloud logging read \
    "resource.type=cloud_run_job AND resource.labels.job_name=${job} AND labels.\"run.googleapis.com/execution_name\"=${exec_name}" \
    --project="${STAGE_C_PROJECT}" --format='value(textPayload)' --order=asc \
    | grep -v '^$' || true
}

# A probe's PASS/FAIL is read from its structured log line, not just the
# job exit code, so a lost exit status can never turn into a silent PASS.
probe_ok() { # logfile probe_name
  python3 - "$1" "$2" <<'PY'
import json, sys
ok = False
for line in open(sys.argv[1]):
    line = line.strip()
    if not line.startswith("{"):
        continue
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        continue
    if record.get("stage_c_probe") == sys.argv[2] and "ok" in record:
        ok = record["ok"] is True
sys.exit(0 if ok else 1)
PY
}

echo "== 1. DB acceptance gate (lifecycle, budget, ledger, caps, leak scan)"
probe_status=0
run_probe "${STAGE_C_DB_PROBE_JOB}" "STAGE_C_MODE=evidence" "STAGE_C_RUN_ID=${RUN_ID}" \
  "STAGE_C_CAPS=${STAGE_C_CAPS}" \
  "STAGE_C_EXPECTED_TERMINAL_STATES=${STAGE_C_ACCEPTABLE_TERMINAL_STATES}" \
  "STAGE_C_IDEMPOTENCY_KEY=${STAGE_C_IDEMPOTENCY_KEY}" \
  | tee /tmp/stagec-evidence.log || probe_status=$?
probe_ok /tmp/stagec-evidence.log evidence \
  || fail "DB evidence gate reported failures (see 'failures' in the log above; probe exit=${probe_status})"

echo "== 2. Post-completion idempotent replay (must return the same run, no new execution)"
USER_ID="$(python3 -c 'import json;print(json.loads(open("/tmp/stagec-setup.log").readlines()[-1])["user_id"])')"
CONVERSATION_ID="$(python3 -c 'import json;print(json.loads(open("/tmp/stagec-setup.log").readlines()[-1])["conversation_id"])')"
replay_status=0
run_probe "${STAGE_C_GW_PROBE_JOB}" \
  "STAGE_C_MODE=replay" "STAGE_C_RUN_ID=${RUN_ID}" "STAGE_C_USER_ID=${USER_ID}" "STAGE_C_CONVERSATION_ID=${CONVERSATION_ID}" \
  "STAGE_C_IDEMPOTENCY_KEY=${STAGE_C_IDEMPOTENCY_KEY}" \
  | tee /tmp/stagec-replay.log || replay_status=$?
probe_ok /tmp/stagec-replay.log replay \
  || fail "idempotent replay did NOT return the same run (probe exit=${replay_status})"

echo "== 3. Worker execution count must be exactly 1"
executions="$(gcloud run jobs executions list --job="${STAGE_C_WORKER_JOB}" \
  --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" --format='value(metadata.name)' | sed '/^$/d' | wc -l)"
gcloud run jobs executions list --job="${STAGE_C_WORKER_JOB}" \
  --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" \
  --format='table(metadata.name,status.startTime,status.completionTime,status.succeededCount,status.failedCount)'
test "${executions}" = "1" || fail "worker execution count is ${executions}, expected exactly 1"

echo "== 4. Worker log secret-marker scan (counts only; no values printed)"
hits="$(gcloud logging read \
  "resource.type=cloud_run_job AND resource.labels.job_name=${STAGE_C_WORKER_JOB}" \
  --project="${STAGE_C_PROJECT}" --format='value(textPayload)' --limit=5000 \
  | grep -c -E 'sk-[A-Za-z0-9]|KIMI_API_KEY=|sb_secret' || true)"
test "${hits}" = "0" || fail "${hits} secret-marker hit(s) in worker logs"

echo
echo "STAGE C ACCEPTANCE GATE: PASS — all criteria verified for run ${RUN_ID}."
echo "Remaining manual step: verify the actual billed total (tokens AND web-search"
echo "tool fees) in the Moonshot console — MILO actual_cost excludes provider-side"
echo "tool charges (see STAGE_C_ACCEPTANCE.md, cost ceiling section)."
echo "Record the evidence in STAGE_C_ACCEPTANCE.md, then run ./07-post-smoke-posture.sh."

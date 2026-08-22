#!/usr/bin/env bash
# Stage C step 6: EXECUTABLE ACCEPTANCE GATE over the completed run.
# Read-only except for probe-job executions. Exits non-zero if ANY
# acceptance criterion fails:
#   - exactly one new authorized run / one new worker execution over the
#     pinned prior baseline (Attempt 7: totals of exactly
#     STAGE_C_EXPECTED_PRIOR_RUNS+1 database runs and
#     STAGE_C_EXPECTED_PRIOR_EXECUTIONS+1 VISIBLE executions — a second
#     new run or execution cannot pass unnoticed, and the historical
#     Attempt 6 database row never satisfies the new run's acceptance);
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

# Bounded wait for ONE named probe execution to reach a terminal state.
# Returns 0 (succeeded), 1 (failed) or 2 (never proved terminal in time).
# The verdict comes from execution_state.py over the structured describe
# JSON — never from a lost gcloud exit status.
PROBE_WAIT_TIMEOUT_SECONDS="${PROBE_WAIT_TIMEOUT_SECONDS:-1800}"
PROBE_POLL_INTERVAL_SECONDS="${PROBE_POLL_INTERVAL_SECONDS:-15}"

wait_for_probe_execution() { # exec_name
  local exec_name="$1" waited=0 state
  while :; do
    state="$(gcloud run jobs executions describe "${exec_name}" \
      --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" --format=json \
      | python3 ./execution_state.py)" || state="running"
    case "${state}" in
      succeeded) return 0 ;;
      failed) return 1 ;;
    esac
    if (( waited >= PROBE_WAIT_TIMEOUT_SECONDS )); then
      echo "STAGE C: probe execution '${exec_name}' did not verifiably reach a terminal state within ${PROBE_WAIT_TIMEOUT_SECONDS}s — failing closed" >&2
      return 2
    fi
    sleep "${PROBE_POLL_INTERVAL_SECONDS}"
    waited=$((waited + PROBE_POLL_INTERVAL_SECONDS))
  done
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
  # Launch WITHOUT --wait: the previous `--wait --format=value(metadata.name)`
  # form lost the execution name whenever the probe completed nonzero (the
  # evidence gate's own fail-closed exit!), and the empty name then produced
  # an invalid empty `execution_name=` logging filter — discarding the
  # structured failure evidence exactly when it was needed. --async prints
  # the created execution's name BEFORE completion, so a later nonzero
  # completion can never lose it.
  local exec_name=""
  exec_name="$(gcloud run jobs execute "${job}" \
    --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" \
    --update-env-vars="^${delim}^${env_overrides}" \
    --async --format='value(metadata.name)')" || exec_name=""
  if [[ -z "${exec_name}" ]]; then
    echo "STAGE C REFUSED: could not establish the execution name for probe job '${job}' — its logs cannot be attributed or retrieved; failing closed" >&2
    return 1
  fi
  echo "probe execution: ${exec_name}" >&2
  local exec_status=0
  wait_for_probe_execution "${exec_name}" || exec_status=$?
  # Structured log retrieval runs UNCONDITIONALLY for the named execution —
  # a probe that completed nonzero is precisely the one whose structured
  # failure record the gate must surface.
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
  return "${exec_status}"
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
  "STAGE_C_EXPECTED_PRIOR_RUNS=${STAGE_C_EXPECTED_PRIOR_RUNS}" \
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

echo "== 3. Worker execution total must be exactly baseline+1, all terminal"
# One-execution increment over the pinned VISIBLE baseline (Attempt 6's
# execution — the only one Cloud Run still exposes; Attempt 5's was
# deleted 2026-08-18): exactly STAGE_C_EXPECTED_PRIOR_EXECUTIONS+1
# executions in total, every one terminal, zero active. A second new
# execution, a still-active execution or an unparseable listing fails the
# gate closed. Structured JSON, not a line count, decides terminal state.
expected_total_executions=$((STAGE_C_EXPECTED_PRIOR_EXECUTIONS + 1))
gcloud run jobs executions list --job="${STAGE_C_WORKER_JOB}" \
  --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" \
  --format='table(metadata.name,status.startTime,status.completionTime,status.succeededCount,status.failedCount)'
gcloud run jobs executions list --job="${STAGE_C_WORKER_JOB}" \
  --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" --format=json \
  | python3 ./verify_executions.py --expected-total "${expected_total_executions}" \
  || fail "worker execution posture is not exactly ${expected_total_executions} terminal executions (pinned baseline ${STAGE_C_EXPECTED_PRIOR_EXECUTIONS} + the one authorized Attempt 7 launch)"

echo "== 4. Worker log secret-marker scan (counts only; no values printed)"
hits="$(gcloud logging read \
  "resource.type=cloud_run_job AND resource.labels.job_name=${STAGE_C_WORKER_JOB}" \
  --project="${STAGE_C_PROJECT}" --format='json(textPayload,jsonPayload)' --limit=5000 \
  | grep -c -E 'sk-[A-Za-z0-9]|KIMI_API_KEY=|sb_secret' || true)"
test "${hits}" = "0" || fail "${hits} secret-marker hit(s) in worker logs"

echo
echo "STAGE C ACCEPTANCE GATE: PASS — all criteria verified for run ${RUN_ID}."
echo "Remaining manual step: verify the actual billed total (tokens AND web-search"
echo "tool fees) in the Moonshot console — MILO actual_cost excludes provider-side"
echo "tool charges (see STAGE_C_ACCEPTANCE.md, cost ceiling section)."
echo "Record the evidence in STAGE_C_ACCEPTANCE.md, then run ./07-post-smoke-posture.sh."

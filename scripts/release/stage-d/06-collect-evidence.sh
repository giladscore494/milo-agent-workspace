#!/usr/bin/env bash
# Stage D step 6: EXECUTABLE ACCEPTANCE GATE over the completed run.
# Read-only except for probe-job executions. Exits non-zero if ANY
# acceptance criterion fails:
#   - exactly one new authorized run / one new worker execution over the
#     pinned prior baseline (post-run totals of exactly
#     STAGE_D_EXPECTED_PRIOR_RUNS+1 database runs and
#     STAGE_D_EXPECTED_PRIOR_EXECUTIONS + the policy's authorized
#     increment VISIBLE executions — a second
#     new run or execution cannot pass unnoticed, and no historical row
#     can satisfy the new run's acceptance);
#   - expected terminal state;
#   - SEMANTIC acceptance of the product result: a technically clean
#     execution whose canonical ProductOutcome is unusable, refused or
#     absent does NOT pass (see semantic_acceptance.py);
#   - model calls actually happened, with tokens and tracked cost;
#   - attempt/claim/heartbeat/lease invariants;
#   - zero dangling reservations;
#   - reservation/ledger/run-usage accounting consistency;
#   - tracked cost <= configured cap; token/call caps respected;
#   - post-completion idempotent replay returns the same run;
#   - the prepared Government capture run is STILL untouched;
#   - the authorized Worker execution RAN the accepted release digest;
#   - zero secret-marker hits (DB events AND worker logs).
# Usage: STAGE_D_WORKDIR=<dir> ./06-collect-evidence.sh [RUN_ID]
#   STAGE_D_WORKDIR is the directory 05-execute-run.sh printed. The run id
#   is read from its state.json, so it never has to be copied by hand; an
#   explicit argument is accepted only if it AGREES with that record.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
# shellcheck source=stage-d-env.sh
source ./stage-d-env.sh
# shellcheck source=probe_exec.sh
source ./probe_exec.sh

STAGE_D_WORKDIR="${STAGE_D_WORKDIR:?STAGE_D_WORKDIR must point at the directory 05-execute-run.sh printed (it holds state.json and setup.log)}"
test -r "${STAGE_D_WORKDIR}/setup.log" \
  || { echo "STAGE D REFUSED: ${STAGE_D_WORKDIR}/setup.log is missing — the replay cannot reuse the authorized test identity; failing closed" >&2; exit 1; }

# The run id comes from the machine-readable state 05-execute-run.sh wrote,
# so the gate can never be pointed at a mistyped or stale id. An explicit
# argument is still accepted, but it must AGREE with the recorded state.
RECORDED_RUN_ID="$(python3 ./state_file.py "${STAGE_D_WORKDIR}/state.json" read run_id)"
RUN_ID="${1:-${RECORDED_RUN_ID}}"
if [ -z "${RUN_ID}" ]; then
  echo "STAGE D REFUSED: no run id — ${STAGE_D_WORKDIR}/state.json records none and none was given; failing closed" >&2
  exit 1
fi
if [ -n "${RECORDED_RUN_ID}" ] && [ "${RUN_ID}" != "${RECORDED_RUN_ID}" ]; then
  echo "STAGE D REFUSED: the run id given (${RUN_ID}) disagrees with the recorded authorized run (${RECORDED_RUN_ID}); failing closed" >&2
  exit 1
fi

# The gate must never be pointed at the prepared Government capture run.
if [ "${RUN_ID}" = "${STAGE_D_GOV_CAPTURE_RUN_ID}" ]; then
  echo "STAGE D REFUSED: ${RUN_ID} is the prepared Government capture run — Stage D never executes or accepts it" >&2
  exit 1
fi

fail() {
  echo "STAGE D ACCEPTANCE GATE: FAIL — $1"
  echo "Operator action: treat the run as FAILED. Run ./kill-switch.sh (if not already fail-closed),"
  echo "then ./07-post-run-lockdown.sh, and record the failure in docs/production-readiness/STAGE_D_AUTHORIZATION.md."
  exit 1
}

# Bounded wait for ONE named probe execution to reach a terminal state.
# Returns 0 (succeeded), 1 (failed) or 2 (never proved terminal in time).
# The verdict comes from execution_state.py over the structured describe
# JSON — never from a lost gcloud exit status.
PROBE_WAIT_TIMEOUT_SECONDS="${PROBE_WAIT_TIMEOUT_SECONDS:-1800}"
PROBE_POLL_INTERVAL_SECONDS="${PROBE_POLL_INTERVAL_SECONDS:-15}"
# Cloud Logging ingestion can lag a just-completed execution by a few
# seconds; the structured-log fetch retries BOUNDED times, never forever.
PROBE_LOG_RETRIES="${PROBE_LOG_RETRIES:-10}"
PROBE_LOG_RETRY_DELAY_SECONDS="${PROBE_LOG_RETRY_DELAY_SECONDS:-10}"

wait_for_probe_execution() { # exec_name
  # The db probe holds SUPABASE_SERVICE_ROLE_KEY, and a Cloud Run job
  # template can be updated between creation and execution, so the
  # pinned image digest, identity and secret bindings are re-verified
  # immediately before every execution.
  verify_probe_job "${job}" || return 1
  local exec_name="$1" waited=0 state
  while :; do
    state="$(gcloud run jobs executions describe "${exec_name}" \
      --project="${STAGE_D_PROJECT}" --region="${STAGE_D_REGION}" --format=json \
      | python3 ./execution_state.py)" || state="running"
    case "${state}" in
      succeeded) return 0 ;;
      failed) return 1 ;;
    esac
    if (( waited >= PROBE_WAIT_TIMEOUT_SECONDS )); then
      echo "STAGE D: probe execution '${exec_name}' did not verifiably reach a terminal state within ${PROBE_WAIT_TIMEOUT_SECONDS}s — failing closed" >&2
      return 2
    fi
    sleep "${PROBE_POLL_INTERVAL_SECONDS}"
    waited=$((waited + PROBE_POLL_INTERVAL_SECONDS))
  done
}

run_probe() {
  local job="$1"; shift
  # Multi-character gcloud env-var delimiter so values may contain commas
  # (e.g. STAGE_D_CAPS). Collision fails closed BEFORE any gcloud call.
  local delim=":::"
  local env_overrides=""
  for kv in "$@"; do
    if [[ "${kv}" == *"${delim}"* ]]; then
      echo "STAGE D REFUSED: env override '${kv%%=*}' contains the delimiter '${delim}'" >&2
      exit 1
    fi
    env_overrides+="${env_overrides:+${delim}}${kv}"
  done
  # Launch WITHOUT --wait: a `--wait --format=value(metadata.name)` form
  # loses the execution name whenever the probe completes nonzero (the
  # evidence gate's own fail-closed exit!), and an empty name then
  # produces an invalid empty `execution_name=` logging filter — discarding
  # the structured failure evidence exactly when it is needed. --async
  # prints the created execution's name BEFORE completion, so a later
  # nonzero completion can never lose it.
  local exec_name=""
  exec_name="$(gcloud run jobs execute "${job}" \
    --project="${STAGE_D_PROJECT}" --region="${STAGE_D_REGION}" \
    --update-env-vars="^${delim}^${env_overrides}" \
    --async --format='value(metadata.name)')" || exec_name=""
  if [[ -z "${exec_name}" ]]; then
    echo "STAGE D REFUSED: could not establish the execution name for probe job '${job}' — its logs cannot be attributed or retrieved; failing closed" >&2
    return 1
  fi
  echo "probe execution: ${exec_name}" >&2
  local exec_status=0
  wait_for_probe_execution "${exec_name}" || exec_status=$?
  # Structured log retrieval runs UNCONDITIONALLY for the named execution —
  # a probe that completed nonzero is precisely the one whose structured
  # failure record the gate must surface. Bounded ingestion retries: the
  # fetch repeats until a structured probe record has been ingested, at
  # most PROBE_LOG_RETRIES times; whatever was retrieved is then rendered
  # (a still-missing record fails the gate closed via probe_ok).
  local raw_log log_attempt=0
  raw_log="$(mktemp)"
  while :; do
    gcloud logging read \
      "resource.type=cloud_run_job AND resource.labels.job_name=${job} AND labels.\"run.googleapis.com/execution_name\"=${exec_name}" \
      --project="${STAGE_D_PROJECT}" --format='json(textPayload,jsonPayload)' --order=asc \
      > "${raw_log}" || true
    if grep -q 'stage_d_probe' "${raw_log}"; then
      break
    fi
    log_attempt=$((log_attempt + 1))
    if (( log_attempt >= PROBE_LOG_RETRIES )); then
      echo "STAGE D: no structured probe record ingested for '${exec_name}' after ${PROBE_LOG_RETRIES} bounded retries" >&2
      break
    fi
    sleep "${PROBE_LOG_RETRY_DELAY_SECONDS}"
  done
  python3 -c '
import json, sys
for record in json.load(sys.stdin):
    if not isinstance(record, dict):
        continue

    payload = record.get("jsonPayload")
    if payload is not None:
        if isinstance(payload, dict) and "stage_d_probe" in payload:
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

    if isinstance(parsed, dict) and "stage_d_probe" in parsed:
        print(json.dumps(parsed, sort_keys=True))
    else:
        print(text, file=sys.stderr)
' < "${raw_log}"
  rm -f "${raw_log}"
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
    if record.get("stage_d_probe") == sys.argv[2] and "ok" in record:
        ok = record["ok"] is True
sys.exit(0 if ok else 1)
PY
}

echo "== 1. DB acceptance gate (lifecycle, budget, ledger, caps, leak scan, Government-capture invariant)"
probe_status=0
run_probe "${STAGE_D_DB_PROBE_JOB}" "STAGE_D_MODE=evidence" "STAGE_D_RUN_ID=${RUN_ID}" \
  "STAGE_D_CAPS=${STAGE_D_CAPS}" \
  "STAGE_D_EXPECTED_TERMINAL_STATES=${STAGE_D_ACCEPTABLE_TERMINAL_STATES}" \
  "STAGE_D_IDEMPOTENCY_KEY=${STAGE_D_IDEMPOTENCY_KEY}" \
  "STAGE_D_EXPECTED_PRIOR_RUNS=${STAGE_D_EXPECTED_PRIOR_RUNS}" \
  "STAGE_D_GOV_CAPTURE_RUN_ID=${STAGE_D_GOV_CAPTURE_RUN_ID}" \
  "STAGE_D_GOV_CAPTURE_KEY=${STAGE_D_GOV_CAPTURE_KEY}" \
  | tee "${STAGE_D_WORKDIR}/evidence.log" || probe_status=$?
probe_ok "${STAGE_D_WORKDIR}/evidence.log" evidence \
  || fail "DB evidence gate reported failures (see 'failures' in the log above; probe exit=${probe_status})"

echo "== 2. Post-completion idempotent replay (must return the same run, no new execution)"
USER_ID="$(python3 -c 'import json,sys;print(json.loads(open(sys.argv[1]).readlines()[-1])["user_id"])' "${STAGE_D_WORKDIR}/setup.log")"
CONVERSATION_ID="$(python3 -c 'import json,sys;print(json.loads(open(sys.argv[1]).readlines()[-1])["conversation_id"])' "${STAGE_D_WORKDIR}/setup.log")"
replay_status=0
run_probe "${STAGE_D_GW_PROBE_JOB}" \
  "STAGE_D_MODE=replay" "STAGE_D_RUN_ID=${RUN_ID}" "STAGE_D_USER_ID=${USER_ID}" "STAGE_D_CONVERSATION_ID=${CONVERSATION_ID}" \
  "STAGE_D_IDEMPOTENCY_KEY=${STAGE_D_IDEMPOTENCY_KEY}" \
  "STAGE_D_GOV_CAPTURE_RUN_ID=${STAGE_D_GOV_CAPTURE_RUN_ID}" \
  "STAGE_D_GOV_CAPTURE_KEY=${STAGE_D_GOV_CAPTURE_KEY}" \
  | tee "${STAGE_D_WORKDIR}/replay.log" || replay_status=$?
probe_ok "${STAGE_D_WORKDIR}/replay.log" replay \
  || fail "idempotent replay did NOT return the same run (probe exit=${replay_status})"

echo "== 3. Worker execution total must be exactly baseline+1, all terminal"
# One-execution increment over the pinned VISIBLE baseline: exactly
# STAGE_D_EXPECTED_PRIOR_EXECUTIONS + the policy's authorized increment
# executions in total, every one
# terminal, zero active. A second new execution, a still-active execution
# or an unparseable listing fails the gate closed. Structured JSON, not a
# line count, decides terminal state.
# The increment comes from the canonical runtime policy
# (first_paid_run_execution_cap), not from a literal here: Stage D and the
# policy must not be able to authorize different numbers of paid executions.
expected_total_executions=$((STAGE_D_EXPECTED_PRIOR_EXECUTIONS + STAGE_D_AUTHORIZED_EXECUTION_INCREMENT))
gcloud run jobs executions list --job="${STAGE_D_WORKER_JOB}" \
  --project="${STAGE_D_PROJECT}" --region="${STAGE_D_REGION}" \
  --format='table(metadata.name,status.startTime,status.completionTime,status.succeededCount,status.failedCount)'
gcloud run jobs executions list --job="${STAGE_D_WORKER_JOB}" \
  --project="${STAGE_D_PROJECT}" --region="${STAGE_D_REGION}" --format=json \
  | python3 ./verify_executions.py --expected-total "${expected_total_executions}" \
      --baseline "${STAGE_D_EXPECTED_PRIOR_EXECUTIONS}" \
  || fail "worker execution posture is not exactly ${expected_total_executions} terminal executions (pinned baseline ${STAGE_D_EXPECTED_PRIOR_EXECUTIONS} + the ${STAGE_D_AUTHORIZED_EXECUTION_INCREMENT} execution(s) the runtime policy authorizes)"

echo "== 4. The authorized execution must have RUN the accepted release digest"
# The strongest image proof available: a Cloud Run execution records the
# digest it resolved, so this cannot be invalidated by the tag moving
# afterwards. It closes the window left by the job template referencing a
# mutable tag.
exec_registry_json="$(mktemp)"; exec_api_json="$(mktemp)"
exec_api_rev_json="$(mktemp)"; exec_job_json="$(mktemp)"; exec_exec_json="$(mktemp)"
trap 'rm -f "${exec_registry_json}" "${exec_api_json}" "${exec_api_rev_json}" "${exec_job_json}" "${exec_exec_json}"' EXIT
latest_execution="$(gcloud run jobs describe "${STAGE_D_WORKER_JOB}" \
  --project="${STAGE_D_PROJECT}" --region="${STAGE_D_REGION}" \
  --format='value(status.latestCreatedExecution.name)')" || latest_execution=""
test -n "${latest_execution}" \
  || fail "could not establish the latest Worker execution name — the digest it ran cannot be proved"
gcloud run jobs executions describe "${latest_execution}" \
  --project="${STAGE_D_PROJECT}" --region="${STAGE_D_REGION}" --format=json > "${exec_exec_json}"
gcloud artifacts docker images list "${STAGE_D_REGISTRY}" \
  --project="${STAGE_D_PROJECT}" --include-tags \
  --filter="tags:${STAGE_D_RELEASE_SHA}" --format=json > "${exec_registry_json}"
gcloud run services describe "${STAGE_D_API_SERVICE}" \
  --project="${STAGE_D_PROJECT}" --region="${STAGE_D_REGION}" --format=json > "${exec_api_json}"
exec_ready_revision="$(python3 -c '
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    status = json.load(fh).get("status") or {}
name = status.get("latestReadyRevisionName") or ""
if not name:
    raise SystemExit("the API service has no latest ready revision")
print(name)
' "${exec_api_json}")" || fail "could not establish the serving API revision"
gcloud run revisions describe "${exec_ready_revision}" \
  --project="${STAGE_D_PROJECT}" --region="${STAGE_D_REGION}" --format=json > "${exec_api_rev_json}"
gcloud run jobs describe "${STAGE_D_WORKER_JOB}" \
  --project="${STAGE_D_PROJECT}" --region="${STAGE_D_REGION}" --format=json > "${exec_job_json}"
python3 ./verify_images.py \
  --registry-json "${exec_registry_json}" \
  --api-service-json "${exec_api_json}" \
  --api-revision-json "${exec_api_rev_json}" \
  --worker-job-json "${exec_job_json}" \
  --execution-json "${exec_exec_json}" \
  || fail "the authorized Worker execution ${latest_execution} did not run the accepted release digest"

echo "== 5. SEMANTIC acceptance: the PRODUCT result, not the process exit"
# Everything above this line answers a technical question: did the execution
# terminate cleanly, exactly once, inside its caps, against the accepted
# digest. None of them asks whether the run produced anything worth having.
#
# This gate asks that, of the one canonical authority
# (backend/product_outcome.py), using the ProductOutcome the worker itself
# recorded when it finalized the run. A run that is `completed` with an
# unusable, refused or unrecorded outcome fails HERE -- which is the whole
# point: "the worker executed successfully" and "the product result was
# semantically acceptable" are different statements, and Stage D now requires
# both.
python3 ./semantic_acceptance.py < "${STAGE_D_WORKDIR}/evidence.log" \
  || fail "the run's PRODUCT result was not semantically acceptable (see the stage_d_semantic_gate verdict above); a clean execution is not a product"

echo "== 6. Worker log secret-marker scan (counts only; no values printed)"
hits="$(gcloud logging read \
  "resource.type=cloud_run_job AND resource.labels.job_name=${STAGE_D_WORKER_JOB}" \
  --project="${STAGE_D_PROJECT}" --format='json(textPayload,jsonPayload)' --limit=5000 \
  | grep -c -E 'sk-[A-Za-z0-9]|KIMI_API_KEY=|sb_secret' || true)"
test "${hits}" = "0" || fail "${hits} secret-marker hit(s) in worker logs"

echo
echo "STAGE D ACCEPTANCE GATE: PASS — all criteria verified for run ${RUN_ID}."
echo "Remaining manual step: verify the actual billed total (tokens AND web-search"
echo "tool fees) in the Moonshot console — MILO actual_cost excludes provider-side"
echo "tool charges (see STAGE_D_AUTHORIZATION.md, cost ceiling section)."
echo "Record the evidence in STAGE_D_AUTHORIZATION.md, then run ./07-post-run-lockdown.sh."

#!/usr/bin/env bash
# Stage D step 5: execute exactly ONE bounded paid run and monitor it to a
# terminal state. Refuses to run unless the VISIBLE Worker execution
# listing matches the pinned live baseline EXACTLY: exactly
# STAGE_D_EXPECTED_PRIOR_EXECUTIONS visible executions, every one terminal,
# zero active — a new active execution before authorization, a count
# mismatch in either direction, or an unverifiable listing all block the
# run.
#
# Sequence:
#   1. re-verify launch invariants: release images + EXACT cap and
#      worker-only provider-envelope values (verify_caps.py), secret
#      binding, IAM, exact terminal execution baseline;
#   2. db-probe preflight (migration surface + exact pinned prior-run
#      baseline + zero rows under the Stage D key + the Government-capture
#      invariant) then setup (dedicated test user/project/conversation) —
#      capture USER_ID / CONVERSATION_ID from its log;
#   3. gw-probe create (POST run + immediate idempotent replay check);
#   4. gw-probe poll until terminal — exits non-zero unless the terminal
#      state is in the acceptance policy; any other outcome means: RUN THE
#      KILL SWITCH (./kill-switch.sh).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
# shellcheck source=stage-d-env.sh
source ./stage-d-env.sh

# The working directory is the machine-readable handoff between this step,
# the evidence gate and the cleanup trap. Nothing downstream may depend on
# an operator copying an id out of a terminal.
STAGE_D_WORKDIR="${STAGE_D_WORKDIR:-$(mktemp -d)}"
mkdir -p "${STAGE_D_WORKDIR}"
STAGE_D_STATE_FILE="${STAGE_D_WORKDIR}/state.json"
echo "Stage D working directory: ${STAGE_D_WORKDIR}"

# write_state KEY VALUE — merge one key into state.json atomically.
write_state() {
  python3 ./state_file.py "${STAGE_D_STATE_FILE}" write "$1" "$2"
}

write_state stage_d_workdir "${STAGE_D_WORKDIR}"
write_state idempotency_key "${STAGE_D_IDEMPOTENCY_KEY}"

fail() { echo "STAGE D FAIL: $1"; echo "Operator action: ./kill-switch.sh, then record the failure in docs/production-readiness/STAGE_D_AUTHORIZATION.md."; exit 1; }

run_probe() { # job [KEY=VALUE ...] — execute, wait, print the execution log
  local job="$1"; shift
  # Multi-character gcloud env-var delimiter so values may contain commas
  # (e.g. STAGE_D_CAPS). Collision fails closed BEFORE any gcloud call —
  # a value containing the delimiter would be split in transport.
  local delim=":::"
  local env_overrides=""
  for kv in "$@"; do
    if [[ "${kv}" == *"${delim}"* ]]; then
      echo "STAGE D REFUSED: env override '${kv%%=*}' contains the delimiter '${delim}'" >&2
      exit 1
    fi
    env_overrides+="${env_overrides:+${delim}}${kv}"
  done
  local exec_name
  exec_name="$(gcloud run jobs execute "${job}" \
    --project="${STAGE_D_PROJECT}" --region="${STAGE_D_REGION}" \
    ${env_overrides:+--update-env-vars="^${delim}^${env_overrides}"} \
    --wait --format='value(metadata.name)')"
  gcloud logging read \
    "resource.type=cloud_run_job AND resource.labels.job_name=${job} AND labels.\"run.googleapis.com/execution_name\"=${exec_name}" \
    --project="${STAGE_D_PROJECT}" --format='json(textPayload,jsonPayload)' --order=asc \
    | python3 -c '
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
'
}

echo "== 1. Launch invariants (accepted release DIGESTS, exact caps + worker provider envelope, IAM, exact terminal execution baseline)"
# Exactly the pinned live executions, every one terminal, zero active.
# More or fewer executions than the baseline, any active execution, or an
# unparseable listing fails closed BEFORE run creation — this proves the
# coming launch will be a one-execution increment. Historical terminal
# executions are never cancelled or deleted.
gcloud run jobs executions list --job="${STAGE_D_WORKER_JOB}" \
  --project="${STAGE_D_PROJECT}" --region="${STAGE_D_REGION}" --format=json \
  | python3 ./verify_executions.py --expected-total "${STAGE_D_EXPECTED_PRIOR_EXECUTIONS}" \
  || fail "worker execution posture does not match the pinned live baseline (expected exactly ${STAGE_D_EXPECTED_PRIOR_EXECUTIONS} terminal executions, zero active) — an unexpected or active execution blocks the run"

worker_json="$(mktemp)"; api_json="$(mktemp)"
registry_json="$(mktemp)"; api_revision_json="$(mktemp)"
trap 'rm -f "${worker_json}" "${api_json}" "${registry_json}" "${api_revision_json}"' EXIT
gcloud run jobs describe "${STAGE_D_WORKER_JOB}" \
  --project="${STAGE_D_PROJECT}" --region="${STAGE_D_REGION}" --format=json > "${worker_json}"
gcloud run services describe "${STAGE_D_API_SERVICE}" \
  --project="${STAGE_D_PROJECT}" --region="${STAGE_D_REGION}" --format=json > "${api_json}"

# Re-verify the ACCEPTED RELEASE DIGESTS immediately before run creation.
# Step 1 proved them, but the Worker job may reference a mutable tag that
# Cloud Run resolves afresh at every execution, so the proof is repeated
# at the last possible moment. Stage D never rebuilds or re-tags: a
# mismatch here means the accepted release is gone and the run is refused.
gcloud artifacts docker images list "${STAGE_D_REGISTRY}" \
  --project="${STAGE_D_PROJECT}" --include-tags \
  --filter="tags:${STAGE_D_RELEASE_SHA}" --format=json > "${registry_json}"
ready_revision="$(python3 -c '
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    status = json.load(fh).get("status") or {}
name = status.get("latestReadyRevisionName") or ""
if not name:
    raise SystemExit("the API service has no latest ready revision")
print(name)
' "${api_json}")" || fail "could not establish the serving API revision"
gcloud run revisions describe "${ready_revision}" \
  --project="${STAGE_D_PROJECT}" --region="${STAGE_D_REGION}" --format=json > "${api_revision_json}"
python3 ./verify_images.py \
  --registry-json "${registry_json}" \
  --api-service-json "${api_json}" \
  --api-revision-json "${api_revision_json}" \
  --worker-job-json "${worker_json}" \
  || fail "accepted release digests no longer match — do NOT create the run; this requires a separate reviewed release"
# Every Stage D cap compared against the exact expected value from
# stage-d-env.sh, on worker AND API, and every worker-only provider limit
# (STAGE_D_WORKER_PROVIDER_LIMITS) exact on the Worker, immediately before
# run creation; also enforces the pinned release images (production-image
# blocker) and the exact flag/provider-secret posture, including
# MILO_ENABLE_CATALOG_EXECUTION=false on both surfaces. Fails on any
# missing/changed/unexpected value, including any MILO_PROVIDER_* variable
# on the API and the live MILO_PROVIDER_MAX_CONCURRENCY=8 drift.
python3 ./verify_caps.py --worker-json "${worker_json}" --api-json "${api_json}" \
  || fail "cap/image/posture verification failed — do NOT create the run"

gcloud secrets get-iam-policy KIMI_API_KEY --project="${STAGE_D_PROJECT}" --format=json \
  | python3 -c 'import json,sys; p=json.load(sys.stdin); a=[m for b in p["bindings"] if b["role"]=="roles/secretmanager.secretAccessor" for m in b["members"]]; assert a==["serviceAccount:'"${STAGE_D_WORKER_SA}"'"], a; print("OK: provider secret worker-only")'
gcloud run jobs get-iam-policy "${STAGE_D_WORKER_JOB}" --project="${STAGE_D_PROJECT}" --region="${STAGE_D_REGION}" --format=json \
  | python3 -c 'import json,sys; p=json.load(sys.stdin); b=p.get("bindings",[]); assert b==[{"members":["serviceAccount:'"${STAGE_D_API_SA}"'"],"role":"roles/run.jobsExecutorWithOverrides"}], b; print("OK: launcher IAM unchanged (API SA only)")'

echo "== 2. DB preflight (exact pinned prior-run baseline + zero Stage D key rows + Government-capture invariant) + test-data setup"
run_probe "${STAGE_D_DB_PROBE_JOB}" "STAGE_D_MODE=preflight" \
  "STAGE_D_EXPECTED_PRIOR_RUNS=${STAGE_D_EXPECTED_PRIOR_RUNS}" \
  "STAGE_D_IDEMPOTENCY_KEY=${STAGE_D_IDEMPOTENCY_KEY}" \
  "STAGE_D_GOV_CAPTURE_RUN_ID=${STAGE_D_GOV_CAPTURE_RUN_ID}" \
  "STAGE_D_GOV_CAPTURE_KEY=${STAGE_D_GOV_CAPTURE_KEY}" \
  | tee "${STAGE_D_WORKDIR}/preflight.log"
grep -q '"ok": true' "${STAGE_D_WORKDIR}/preflight.log" || fail "preflight failed (see log above) — the DB is not in the expected pre-run state"
run_probe "${STAGE_D_DB_PROBE_JOB}" "STAGE_D_MODE=setup" \
  "STAGE_D_PROJECT_SLUG=${STAGE_D_PROJECT_SLUG}" \
  "STAGE_D_TEST_EMAIL=${STAGE_D_TEST_EMAIL}" \
  "STAGE_D_WORKFLOW_KEY=${STAGE_D_WORKFLOW_KEY}" \
  "STAGE_D_FORBIDDEN_PROJECT_IDS=${STAGE_D_FORBIDDEN_PROJECT_IDS}" \
  | tee "${STAGE_D_WORKDIR}/setup.log"
grep -q '"ok": true' "${STAGE_D_WORKDIR}/setup.log" || fail "test-data setup failed (see log above)"
USER_ID="$(python3 -c 'import json,sys;print(json.loads(open(sys.argv[1]).readlines()[-1])["user_id"])' "${STAGE_D_WORKDIR}/setup.log")"
CONVERSATION_ID="$(python3 -c 'import json,sys;print(json.loads(open(sys.argv[1]).readlines()[-1])["conversation_id"])' "${STAGE_D_WORKDIR}/setup.log")"
write_state user_id "${USER_ID}"
write_state conversation_id "${CONVERSATION_ID}"
echo "test user=${USER_ID} conversation=${CONVERSATION_ID}"

echo "== 3. Create the ONE run (with immediate idempotent replay)"
run_probe "${STAGE_D_GW_PROBE_JOB}" \
  "STAGE_D_MODE=create" "STAGE_D_USER_ID=${USER_ID}" "STAGE_D_CONVERSATION_ID=${CONVERSATION_ID}" \
  "STAGE_D_IDEMPOTENCY_KEY=${STAGE_D_IDEMPOTENCY_KEY}" \
  "STAGE_D_GOV_CAPTURE_RUN_ID=${STAGE_D_GOV_CAPTURE_RUN_ID}" \
  "STAGE_D_GOV_CAPTURE_KEY=${STAGE_D_GOV_CAPTURE_KEY}" \
  | tee "${STAGE_D_WORKDIR}/create.log"
RUN_ID="$(python3 -c 'import json,sys;print(json.loads(open(sys.argv[1]).readlines()[-1])["run_id"])' "${STAGE_D_WORKDIR}/create.log")"
# Persisted BEFORE the poll: a cleanup triggered mid-run must still be
# able to name the run that was created.
write_state run_id "${RUN_ID}"
echo "RUN_ID=${RUN_ID}"
test "${RUN_ID}" != "${STAGE_D_GOV_CAPTURE_RUN_ID}" \
  || fail "run creation returned the prepared Government capture run id — Stage D never executes the capture"

echo "== 4. Monitor to terminal state (PASS states only: ${STAGE_D_ACCEPTABLE_TERMINAL_STATES})"
poll_status=0
run_probe "${STAGE_D_GW_PROBE_JOB}" \
  "STAGE_D_MODE=poll" "STAGE_D_RUN_ID=${RUN_ID}" "STAGE_D_USER_ID=${USER_ID}" "STAGE_D_CONVERSATION_ID=${CONVERSATION_ID}" \
  "STAGE_D_IDEMPOTENCY_KEY=${STAGE_D_IDEMPOTENCY_KEY}" \
  "STAGE_D_ACCEPTABLE_TERMINAL_STATES=${STAGE_D_ACCEPTABLE_TERMINAL_STATES}" \
  | tee "${STAGE_D_WORKDIR}/poll.log" || poll_status=$?

# Belt-and-braces: even if the probe job's exit code is lost, PASS requires
# an explicit acceptable terminal verdict in the poll log.
python3 - "${STAGE_D_WORKDIR}/poll.log" <<'PY' || poll_status=1
import json, sys
for line in open(sys.argv[1]):
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
gcloud run jobs executions list --job="${STAGE_D_WORKER_JOB}" \
  --project="${STAGE_D_PROJECT}" --region="${STAGE_D_REGION}" \
  --format='table(metadata.name,status.startTime,status.completionTime,status.succeededCount,status.failedCount)'

if [ "${poll_status}" -ne 0 ]; then
  fail "run ${RUN_ID} did not reach an acceptable terminal state (policy: ${STAGE_D_ACCEPTABLE_TERMINAL_STATES})"
fi

echo
echo "Run ${RUN_ID} reached an ACCEPTABLE terminal state."
echo "Next (mandatory acceptance gate) — the run id is read from state.json,"
echo "so it never has to be copied by hand:"
echo "  STAGE_D_WORKDIR=${STAGE_D_WORKDIR} ./06-collect-evidence.sh"

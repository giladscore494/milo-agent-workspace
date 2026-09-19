#!/usr/bin/env bash
# Shared probe-execution helpers for the Stage D toolkit. Sourced, never
# run directly. Assumes stage-d-env.sh has already been sourced and that
# the caller's working directory is this one.
#
# Two properties every probe execution needs, and neither is optional
# because stage-d-db-probe holds SUPABASE_SERVICE_ROLE_KEY:
#
#   1. THE JOB IS STILL WHAT WAS REVIEWED. A Cloud Run job template can be
#      updated between creation and execution, so the pinned image digest,
#      the service account and the secret bindings are re-verified
#      immediately BEFORE every execution, not just once at creation.
#
#   2. THE LOGS BELONG TO THIS EXECUTION. Filtering Cloud Logging by job
#      NAME alone can match retained records from an older, deleted and
#      recreated job of the same name — so a stale PASS could satisfy a
#      gate that is really looking at nothing. Every execution is launched
#      with --async so its name is captured before completion, and its
#      logs are filtered by that exact execution name.

# verify_probe_job JOB — re-verify one probe job template. Fails closed.
verify_probe_job() {
  local job="$1" describe_json flag rc=0
  case "${job}" in
    "${STAGE_D_DB_PROBE_JOB}") flag="--db-json" ;;
    "${STAGE_D_GW_PROBE_JOB}") flag="--gw-json" ;;
    *)
      echo "STAGE D REFUSED: '${job}' is not a Stage D probe job" >&2
      return 1 ;;
  esac
  describe_json="$(mktemp)"
  if ! gcloud run jobs describe "${job}" \
      --project="${STAGE_D_PROJECT}" --region="${STAGE_D_REGION}" --format=json > "${describe_json}"; then
    rm -f "${describe_json}"
    echo "STAGE D REFUSED: could not describe probe job '${job}' — its image and credentials cannot be verified; failing closed" >&2
    return 1
  fi
  python3 ./verify_probe_jobs.py "${flag}" "${describe_json}" > /dev/null || rc=$?
  if [ "${rc}" -ne 0 ]; then
    # Re-run without suppression so the operator sees exactly what differs.
    python3 ./verify_probe_jobs.py "${flag}" "${describe_json}" >&2 || true
    rm -f "${describe_json}"
    echo "STAGE D REFUSED: probe job '${job}' is not what was reviewed — refusing to execute it" >&2
    return 1
  fi
  rm -f "${describe_json}"
  return 0
}

# render_probe_records — read Cloud Logging JSON on stdin and print the
# structured Stage D probe records to stdout, everything else to stderr.
render_probe_records() {
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
'
}

# execute_probe_attributed JOB [KEY=VALUE ...] — verify the job, launch it
# asynchronously, wait for THAT execution to terminate, and print only the
# structured records Cloud Logging attributes to THAT execution name.
#
# Returns the execution's own exit status; the caller still decides PASS
# from the structured verdict, never from this status alone.
execute_probe_attributed() {
  local job="$1"; shift
  local delim=":::" env_overrides="" kv
  for kv in "$@"; do
    if [[ "${kv}" == *"${delim}"* ]]; then
      echo "STAGE D REFUSED: env override '${kv%%=*}' contains the delimiter '${delim}'" >&2
      return 1
    fi
    env_overrides+="${env_overrides:+${delim}}${kv}"
  done

  verify_probe_job "${job}" || return 1

  local exec_name=""
  exec_name="$(gcloud run jobs execute "${job}" \
    --project="${STAGE_D_PROJECT}" --region="${STAGE_D_REGION}" \
    ${env_overrides:+--update-env-vars="^${delim}^${env_overrides}"} \
    --async --format='value(metadata.name)')" || exec_name=""
  if [ -z "${exec_name}" ]; then
    echo "STAGE D REFUSED: could not establish the execution name for probe job '${job}' — its logs cannot be attributed; failing closed" >&2
    return 1
  fi
  echo "probe execution: ${exec_name}" >&2

  local waited=0 state exec_status=2
  local timeout="${PROBE_WAIT_TIMEOUT_SECONDS:-1800}"
  local interval="${PROBE_POLL_INTERVAL_SECONDS:-15}"
  while :; do
    state="$(gcloud run jobs executions describe "${exec_name}" \
      --project="${STAGE_D_PROJECT}" --region="${STAGE_D_REGION}" --format=json \
      | python3 ./execution_state.py)" || state="running"
    if [ "${state}" = "succeeded" ]; then exec_status=0; break; fi
    if [ "${state}" = "failed" ]; then exec_status=1; break; fi
    if (( waited >= timeout )); then
      echo "STAGE D: probe execution '${exec_name}' did not verifiably reach a terminal state within ${timeout}s — failing closed" >&2
      exec_status=2
      break
    fi
    sleep "${interval}"
    waited=$((waited + interval))
  done

  # Logs are filtered by the EXACT execution name, so a retained record
  # from an older job of the same name can never satisfy the caller.
  local raw_log attempt=0
  local retries="${PROBE_LOG_RETRIES:-10}"
  local retry_delay="${PROBE_LOG_RETRY_DELAY_SECONDS:-10}"
  raw_log="$(mktemp)"
  while :; do
    gcloud logging read \
      "resource.type=cloud_run_job AND resource.labels.job_name=${job} AND labels.\"run.googleapis.com/execution_name\"=${exec_name}" \
      --project="${STAGE_D_PROJECT}" --format='json(textPayload,jsonPayload)' --order=asc \
      > "${raw_log}" || true
    if grep -q 'stage_d_probe' "${raw_log}"; then break; fi
    attempt=$((attempt + 1))
    if (( attempt >= retries )); then
      echo "STAGE D: no structured probe record ingested for '${exec_name}' after ${retries} bounded retries" >&2
      break
    fi
    sleep "${retry_delay}"
  done
  render_probe_records < "${raw_log}"
  rm -f "${raw_log}"
  return "${exec_status}"
}

# probe_verdict LOGFILE MODE — exit 0 only if the newest structured record
# for MODE reports ok:true. A missing record fails closed.
probe_verdict() {
  python3 - "$1" "$2" <<'PY'
import json, sys
ok = None
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
if ok is None:
    print(f"no structured {sys.argv[2]!r} record was produced by this execution — failing closed", file=sys.stderr)
    sys.exit(1)
sys.exit(0 if ok else 1)
PY
}

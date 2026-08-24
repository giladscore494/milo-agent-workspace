#!/usr/bin/env bash
# Swarm V2 controlled-smoke controller.
#
# Replaces the ad-hoc controller whose IAM preflight failed when piped JSON
# and a Python heredoc competed for stdin. Structural rules in this script:
#   * every gcloud JSON output is written to a TEMP FILE and parsed by a
#     committed Python helper that takes the file as an ARGUMENT — no
#     parser in this repository reads stdin;
#   * one temp workspace, created with mktemp -d and removed by an EXIT
#     trap on every path (success, failure, ctrl-C);
#   * every variable expansion is quoted; delimiters never collide with
#     values (validated before use).
#
# Modes:
#   preflight        read-only checks only (default). Safe anywhere. The
#                    API contract is verified against the ACTUAL serving
#                    revision (latestReadyRevisionName + 100% traffic),
#                    never against the service template alone.
#   execute          launches ONE worker execution for SMOKE_RUN_ID and
#                    monitors it to a terminal state. Requires the operator
#                    acknowledgement below. This SPENDS REAL MONEY.
#   kill             the COMPLETE canonical fail-closed shutdown: delegates
#                    to scripts/release/stage-c/kill-switch.sh, which
#                    disables all six API execution flags, sets
#                    JOB_LAUNCHER=disabled, disables Worker paid execution,
#                    removes every provider-key alias from API and Worker,
#                    cancels ALL active Worker executions, and independently
#                    verifies every postcondition (including the serving
#                    API revision) before claiming success.
#   post-verify      final posture verification after the window closes.
#
# Outside `kill`, the controller never edits environment variables or
# flags itself: flag changes for the smoke window are explicit operator
# actions documented in docs/production-readiness/STAGED_ACTIVATION.md.
# preflight/post-verify verify the posture; they do not create it.
set -euo pipefail

SMOKE_PROJECT="${SMOKE_PROJECT:-big-cabinet-457321-t7}"
SMOKE_REGION="${SMOKE_REGION:-us-central1}"
SMOKE_API_SERVICE="${SMOKE_API_SERVICE:-milo-agent-api}"
SMOKE_WORKER_JOB="${SMOKE_WORKER_JOB:-milo-agent-worker}"
# Pinned runtime identities (must match scripts/release/stage-c/stage-c-env.sh).
SMOKE_API_SA="${SMOKE_API_SA:-milo-api-runtime@big-cabinet-457321-t7.iam.gserviceaccount.com}"
SMOKE_WORKER_SA="${SMOKE_WORKER_SA:-milo-worker-runtime@big-cabinet-457321-t7.iam.gserviceaccount.com}"
SMOKE_GATEWAY_SA="${SMOKE_GATEWAY_SA:-milo-vercel-gateway@big-cabinet-457321-t7.iam.gserviceaccount.com}"
SMOKE_PROVIDER_SECRET="${SMOKE_PROVIDER_SECRET:-KIMI_API_KEY}"
SMOKE_SUPABASE_URL_SECRET="${SMOKE_SUPABASE_URL_SECRET:-SUPABASE_URL}"
SMOKE_SUPABASE_KEY_SECRET="${SMOKE_SUPABASE_KEY_SECRET:-SUPABASE_SECRET_KEY}"
SMOKE_RUN_ID="${SMOKE_RUN_ID:-}"
SMOKE_EXPECTED_ATTEMPT="${SMOKE_EXPECTED_ATTEMPT:-1}"
SMOKE_MAX_MODEL_CALLS="${SMOKE_MAX_MODEL_CALLS:-200}"
SMOKE_MAX_ACTUAL_COST="${SMOKE_MAX_ACTUAL_COST:-3.00}"
SMOKE_MONITOR_TIMEOUT_SECONDS="${SMOKE_MONITOR_TIMEOUT_SECONDS:-3600}"
SMOKE_POLL_SECONDS="${SMOKE_POLL_SECONDS:-20}"
SMOKE_ACK_EXPECTED="I_UNDERSTAND_THIS_EXECUTES_ONE_PAID_PRODUCTION_RUN"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KILL_SWITCH="${HERE}/../stage-c/kill-switch.sh"
MODE="${1:-preflight}"

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/swarm-v2-smoke.XXXXXX")"
SMOKE_SAFETY_ARMED=0
SMOKE_SHUTDOWN_RUNNING=0

cleanup() { rm -rf "${WORKDIR}"; }

# Any execute/monitor exit closes the paid window. This includes a clean
# success, semantic failure, command error, timeout, Ctrl-C, SIGTERM and
# Cloud Shell disconnect (SIGHUP). The original failure code is preserved
# unless the emergency shutdown itself is incomplete.
shutdown_guard() {
  local rc=$?
  trap - EXIT INT TERM HUP
  if [[ "${SMOKE_SAFETY_ARMED}" == "1" && "${SMOKE_SHUTDOWN_RUNNING}" == "0" ]]; then
    SMOKE_SHUTDOWN_RUNNING=1
    echo "== automatic canonical fail-closed shutdown =="
    if ! canonical_shutdown; then
      echo "SWARM SMOKE CRITICAL: automatic kill switch incomplete; production posture is not proven safe" >&2
      rc=1
    fi
  fi
  cleanup
  exit "${rc}"
}
trap shutdown_guard EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

fail() {
  echo "SWARM SMOKE FAIL: $1" >&2
  echo "Operator action: '$0 kill' performs the complete canonical fail-closed shutdown; record the failure before retrying." >&2
  exit 1
}

require_tool() { command -v "$1" >/dev/null 2>&1 || fail "required tool missing: $1"; }

describe_to_file() { # kind name outfile
  local kind="$1" name="$2" outfile="$3"
  if [[ "${kind}" == "service" ]]; then
    gcloud run services describe "${name}" --project="${SMOKE_PROJECT}" \
      --region="${SMOKE_REGION}" --format=json >"${outfile}" \
      || fail "cannot describe service ${name}"
  else
    gcloud run jobs describe "${name}" --project="${SMOKE_PROJECT}" \
      --region="${SMOKE_REGION}" --format=json >"${outfile}" \
      || fail "cannot describe job ${name}"
  fi
}

active_execution_count() {
  gcloud run jobs executions list --job="${SMOKE_WORKER_JOB}" \
    --project="${SMOKE_PROJECT}" --region="${SMOKE_REGION}" --format=json \
    >"${WORKDIR}/executions.json" || fail "cannot list worker executions"
  python3 "${HERE}/parse_executions.py" active-count "${WORKDIR}/executions.json"
}

preflight() { # $1: posture flag ("" or --smoke-active)
  local posture="${1:-}"
  require_tool gcloud
  require_tool python3

  echo "== Swarm V2 smoke preflight (${posture:-at-rest}) =="
  describe_to_file job "${SMOKE_WORKER_JOB}" "${WORKDIR}/worker.json"
  describe_to_file service "${SMOKE_API_SERVICE}" "${WORKDIR}/api.json"

  # The API contract is judged on the ACTUAL serving revision: resolve
  # status.latestReadyRevisionName, describe exactly that revision, and
  # require it to receive 100% of traffic. The service template alone is
  # never authoritative — a safe template can hide an unsafe live revision.
  local ready_revision
  ready_revision="$(python3 "${HERE}/parse_serving_state.py" resolve "${WORKDIR}/api.json")" \
    || fail "API has no latest ready revision"
  gcloud run revisions describe "${ready_revision}" \
    --project="${SMOKE_PROJECT}" --region="${SMOKE_REGION}" --format=json \
    >"${WORKDIR}/api-revision.json" || fail "cannot describe serving revision ${ready_revision}"
  python3 "${HERE}/parse_serving_state.py" verify "${WORKDIR}/api.json" "${WORKDIR}/api-revision.json" \
    || fail "serving revision/traffic verification"

  # Contract parity (models, caps, concurrency, flags, secret bindings,
  # execution-control identity gating) plus the pinned runtime service
  # accounts, on the worker job and on the SERVING API revision.
  if [[ -n "${posture}" ]]; then
    python3 "${HERE}/parse_env_contract.py" worker "${WORKDIR}/worker.json" "${posture}" \
      --service-account "${SMOKE_WORKER_SA}" || fail "worker env contract"
    python3 "${HERE}/parse_env_contract.py" api "${WORKDIR}/api-revision.json" "${posture}" \
      --service-account "${SMOKE_API_SA}" || fail "api serving-revision env contract"
  else
    python3 "${HERE}/parse_env_contract.py" worker "${WORKDIR}/worker.json" \
      --service-account "${SMOKE_WORKER_SA}" || fail "worker env contract"
    python3 "${HERE}/parse_env_contract.py" api "${WORKDIR}/api-revision.json" \
      --service-account "${SMOKE_API_SA}" || fail "api serving-revision env contract"
  fi

  # IAM preflight: policy JSON goes to a file; the parser takes the file as
  # an argument. Nothing here reads stdin.
  gcloud run services get-iam-policy "${SMOKE_API_SERVICE}" \
    --project="${SMOKE_PROJECT}" --region="${SMOKE_REGION}" --format=json \
    >"${WORKDIR}/api-iam.json" || fail "cannot read API IAM policy"
  python3 "${HERE}/parse_iam.py" "${WORKDIR}/api-iam.json" \
    --required-invoker "${SMOKE_GATEWAY_SA}" --forbid-public || fail "API IAM policy"

  # Kimi Secret Manager access must remain Worker-only in every posture:
  # any accessor other than the pinned worker runtime identity fails.
  gcloud secrets get-iam-policy "${SMOKE_PROVIDER_SECRET}" \
    --project="${SMOKE_PROJECT}" --format=json \
    >"${WORKDIR}/provider-secret-iam.json" || fail "cannot read provider secret IAM policy"
  # Secret-level get-iam-policy omits inherited project grants. Inspect both
  # policies so a project-wide secretAccessor cannot silently reach Kimi.
  gcloud projects get-iam-policy "${SMOKE_PROJECT}" --format=json \
    >"${WORKDIR}/project-iam.json" || fail "cannot read project IAM policy"
  local accessor_requirement=()
  if [[ -n "${posture}" ]]; then
    accessor_requirement=(--require-allowed-accessor)
  fi
  python3 "${HERE}/parse_iam.py" "${WORKDIR}/provider-secret-iam.json" \
    --secret-accessor-only "${SMOKE_WORKER_SA}" \
    --inherited-policy-file "${WORKDIR}/project-iam.json" \
    "${accessor_requirement[@]}" || fail "effective provider secret IAM policy (must be worker-only)"

  # Admission closure: no other worker execution may be active.
  local active
  active="$(active_execution_count)"
  [[ "${active}" == "0" ]] || fail "expected zero active worker executions, found ${active}"
  echo "preflight OK"
}

semantic_verify_run() {
  require_tool gcloud
  require_tool curl
  local supabase_url supabase_key auth_config
  gcloud secrets versions access latest --secret="${SMOKE_SUPABASE_URL_SECRET}" \
    --project="${SMOKE_PROJECT}" >"${WORKDIR}/supabase-url" \
    || fail "cannot access Supabase URL secret"
  gcloud secrets versions access latest --secret="${SMOKE_SUPABASE_KEY_SECRET}" \
    --project="${SMOKE_PROJECT}" >"${WORKDIR}/supabase-key" \
    || fail "cannot access Supabase service key secret"
  chmod 600 "${WORKDIR}/supabase-url" "${WORKDIR}/supabase-key"
  supabase_url="$(tr -d '\r\n' <"${WORKDIR}/supabase-url")"
  supabase_key="$(tr -d '\r\n' <"${WORKDIR}/supabase-key")"
  [[ "${supabase_url}" =~ ^https://[A-Za-z0-9.-]+\.supabase\.co/?$ ]] \
    || fail "Supabase URL secret has an unexpected shape"
  [[ -n "${supabase_key}" ]] || fail "Supabase service key secret is empty"

  # Keep the credential out of argv/process listings: curl reads headers
  # from a mode-600 temporary config that the EXIT trap always removes.
  auth_config="${WORKDIR}/supabase-curl.conf"
  umask 077
  {
    printf 'header = "apikey: %s"\n' "${supabase_key}"
    printf 'header = "Authorization: Bearer %s"\n' "${supabase_key}"
  } >"${auth_config}"
  unset supabase_key

  curl --config "${auth_config}" --silent --show-error --fail \
    --connect-timeout 10 --max-time 30 --get \
    "${supabase_url%/}/rest/v1/runs" \
    --data-urlencode "id=eq.${SMOKE_RUN_ID}" \
    --data-urlencode "select=id,status,attempt,usage,finished_at" \
    >"${WORKDIR}/run-state.json" || fail "cannot read sanitized durable run state"
  curl --config "${auth_config}" --silent --show-error --fail \
    --connect-timeout 10 --max-time 30 --get \
    "${supabase_url%/}/rest/v1/run_checkpoints" \
    --data-urlencode "run_id=eq.${SMOKE_RUN_ID}" \
    --data-urlencode "select=run_id,engine_version,workflow_key,phase,attempt,created_at" \
    --data-urlencode "order=created_at.desc" --data-urlencode "limit=1" \
    >"${WORKDIR}/checkpoint-state.json" || fail "cannot read sanitized checkpoint state"
  unset supabase_url

  python3 "${HERE}/parse_run_state.py" \
    "${WORKDIR}/run-state.json" "${WORKDIR}/checkpoint-state.json" \
    --run-id "${SMOKE_RUN_ID}" \
    --expected-attempt "${SMOKE_EXPECTED_ATTEMPT}" \
    --max-model-calls "${SMOKE_MAX_MODEL_CALLS}" \
    --max-actual-cost "${SMOKE_MAX_ACTUAL_COST}" \
    || fail "durable Swarm V2 run did not satisfy positive-smoke acceptance"
}

execute() {
  # The operator has already opened the smoke window before calling execute.
  # Arm first so even invalid input or a failed preflight closes it.
  SMOKE_SAFETY_ARMED=1
  [[ -n "${SMOKE_RUN_ID}" ]] || fail "SMOKE_RUN_ID must be set to the queued run's UUID"
  [[ "${SMOKE_RUN_ID}" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] \
    || fail "SMOKE_RUN_ID is not a lowercase UUID"
  [[ "${SMOKE_ACK:-}" == "${SMOKE_ACK_EXPECTED}" ]] \
    || fail "set SMOKE_ACK=${SMOKE_ACK_EXPECTED} to authorize a paid execution"

  preflight --smoke-active

  echo "== launching one worker execution for run ${SMOKE_RUN_ID} =="
  local exec_name
  exec_name="$(gcloud run jobs execute "${SMOKE_WORKER_JOB}" \
    --project="${SMOKE_PROJECT}" --region="${SMOKE_REGION}" \
    --update-env-vars="RUN_ID=${SMOKE_RUN_ID}" \
    --format='value(metadata.name)')" || fail "job execution request failed"
  [[ -n "${exec_name}" ]] || fail "execution name was empty"
  printf '%s\n' "${exec_name}" >"${WORKDIR}/execution-name"
  echo "execution: ${exec_name}"
  monitor "${exec_name}"
}

monitor() { # execution-name
  local exec_name="$1" waited=0
  echo "== monitoring ${exec_name} (timeout ${SMOKE_MONITOR_TIMEOUT_SECONDS}s) =="
  while true; do
    gcloud run jobs executions describe "${exec_name}" \
      --project="${SMOKE_PROJECT}" --region="${SMOKE_REGION}" --format=json \
      >"${WORKDIR}/execution.json" || fail "cannot describe execution ${exec_name}"
    local verdict
    verdict="$(python3 "${HERE}/parse_executions.py" verdict "${WORKDIR}/execution.json")" \
      || fail "cannot parse execution status"
    case "${verdict}" in
      succeeded)
        echo "execution completed: task exit code 0; verifying durable semantic outcome"
        semantic_verify_run
        echo "semantic smoke PASS: durable run is completed with compatible Swarm V2 checkpoint and bounded usage"
        return 0
        ;;
      failed:*)
        fail "execution finished non-zero (${verdict}); inspect the run row and logs, then run '$0 post-verify'"
        ;;
      running) ;;
      *) fail "unrecognized monitor verdict: ${verdict}" ;;
    esac
    if (( waited >= SMOKE_MONITOR_TIMEOUT_SECONDS )); then
      fail "monitor timeout after ${waited}s; run '$0 kill' for the complete fail-closed shutdown"
    fi
    sleep "${SMOKE_POLL_SECONDS}"
    waited=$(( waited + SMOKE_POLL_SECONDS ))
  done
}

canonical_shutdown() {
  [[ -f "${KILL_SWITCH}" ]] || {
    echo "canonical kill switch not found at ${KILL_SWITCH}" >&2
    return 1
  }
  STAGE_C_PROJECT="${SMOKE_PROJECT}" \
  STAGE_C_REGION="${SMOKE_REGION}" \
  STAGE_C_API_SERVICE="${SMOKE_API_SERVICE}" \
  STAGE_C_WORKER_JOB="${SMOKE_WORKER_JOB}" \
    bash "${KILL_SWITCH}"
}

kill_switch() {
  echo "== canonical fail-closed shutdown (stage-c kill-switch) =="
  if ! canonical_shutdown; then
    fail "canonical kill switch reported an incomplete fail-closed state"
  fi
  echo "kill switch complete; production is verified at rest."
}

post_verify() {
  preflight ""
  echo "post-smoke posture OK: flags at rest, provider key unbound, no active executions"
}

case "${MODE}" in
  preflight)   preflight "${2:-}" ;;
  execute)     execute ;;
  monitor)     SMOKE_SAFETY_ARMED=1; monitor "${2:?usage: $0 monitor <execution-name>}" ;;
  kill)        kill_switch ;;
  post-verify) post_verify ;;
  *)
    echo "usage: $0 {preflight [--smoke-active]|execute|monitor <execution>|kill|post-verify}" >&2
    exit 2
    ;;
esac

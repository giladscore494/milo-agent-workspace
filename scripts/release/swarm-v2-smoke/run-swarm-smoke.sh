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
#   preflight        read-only checks only (default). Safe anywhere.
#   execute          launches ONE worker execution for SMOKE_RUN_ID and
#                    monitors it to a terminal state. Requires the operator
#                    acknowledgement below. This SPENDS REAL MONEY.
#   kill             cancels the active smoke execution (kill switch).
#   post-verify      final posture verification after the window closes.
#
# The controller never edits environment variables or flags itself: flag
# changes for the smoke window are explicit operator actions documented in
# docs/production-readiness/STAGED_ACTIVATION.md. preflight/post-verify
# verify the posture; they do not create it.
set -euo pipefail

SMOKE_PROJECT="${SMOKE_PROJECT:-big-cabinet-457321-t7}"
SMOKE_REGION="${SMOKE_REGION:-us-central1}"
SMOKE_API_SERVICE="${SMOKE_API_SERVICE:-milo-agent-api}"
SMOKE_WORKER_JOB="${SMOKE_WORKER_JOB:-milo-agent-worker}"
SMOKE_WORKER_SA="${SMOKE_WORKER_SA:-}"
SMOKE_GATEWAY_SA="${SMOKE_GATEWAY_SA:-milo-vercel-gateway@big-cabinet-457321-t7.iam.gserviceaccount.com}"
SMOKE_RUN_ID="${SMOKE_RUN_ID:-}"
SMOKE_MONITOR_TIMEOUT_SECONDS="${SMOKE_MONITOR_TIMEOUT_SECONDS:-3600}"
SMOKE_POLL_SECONDS="${SMOKE_POLL_SECONDS:-20}"
SMOKE_ACK_EXPECTED="I_UNDERSTAND_THIS_EXECUTES_ONE_PAID_PRODUCTION_RUN"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="${1:-preflight}"

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/swarm-v2-smoke.XXXXXX")"
cleanup() { rm -rf "${WORKDIR}"; }
trap cleanup EXIT INT TERM

fail() {
  echo "SWARM SMOKE FAIL: $1" >&2
  echo "Operator action: '$0 kill' cancels an active execution; record the failure before retrying." >&2
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

  # Contract parity (models, caps, concurrency, flags, secret bindings).
  if [[ -n "${posture}" ]]; then
    python3 "${HERE}/parse_env_contract.py" worker "${WORKDIR}/worker.json" "${posture}" || fail "worker env contract"
    python3 "${HERE}/parse_env_contract.py" api "${WORKDIR}/api.json" "${posture}" || fail "api env contract"
  else
    python3 "${HERE}/parse_env_contract.py" worker "${WORKDIR}/worker.json" || fail "worker env contract"
    python3 "${HERE}/parse_env_contract.py" api "${WORKDIR}/api.json" || fail "api env contract"
  fi

  # IAM preflight: policy JSON goes to a file; the parser takes the file as
  # an argument. Nothing here reads stdin.
  gcloud run services get-iam-policy "${SMOKE_API_SERVICE}" \
    --project="${SMOKE_PROJECT}" --region="${SMOKE_REGION}" --format=json \
    >"${WORKDIR}/api-iam.json" || fail "cannot read API IAM policy"
  python3 "${HERE}/parse_iam.py" "${WORKDIR}/api-iam.json" \
    --required-invoker "${SMOKE_GATEWAY_SA}" --forbid-public || fail "API IAM policy"

  # Admission closure: no other worker execution may be active.
  local active
  active="$(active_execution_count)"
  [[ "${active}" == "0" ]] || fail "expected zero active worker executions, found ${active}"
  echo "preflight OK"
}

execute() {
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
        echo "execution completed: task exit code 0 (terminal run state persisted by the worker)"
        return 0
        ;;
      failed:*)
        fail "execution finished non-zero (${verdict}); inspect the run row and logs, then run '$0 post-verify'"
        ;;
      running) ;;
      *) fail "unrecognized monitor verdict: ${verdict}" ;;
    esac
    if (( waited >= SMOKE_MONITOR_TIMEOUT_SECONDS )); then
      fail "monitor timeout after ${waited}s; run '$0 kill' to cancel ${exec_name}"
    fi
    sleep "${SMOKE_POLL_SECONDS}"
    waited=$(( waited + SMOKE_POLL_SECONDS ))
  done
}

kill_switch() {
  local exec_name="${2:-${SMOKE_EXECUTION_NAME:-}}"
  [[ -n "${exec_name}" ]] || fail "pass the execution name: '$0 kill <execution-name>' or set SMOKE_EXECUTION_NAME"
  echo "== cancelling ${exec_name} =="
  gcloud run jobs executions cancel "${exec_name}" \
    --project="${SMOKE_PROJECT}" --region="${SMOKE_REGION}" --quiet \
    || fail "cancel request failed; cancel manually from the console"
  echo "cancelled. Now: disable MILO_ENABLE_PAID_EXECUTION / MILO_ENABLE_RUN_CREATION per the staged-activation runbook, then run '$0 post-verify'."
}

post_verify() {
  preflight ""
  echo "post-smoke posture OK: flags at rest, provider key unbound, no active executions"
}

case "${MODE}" in
  preflight)   preflight "${2:-}" ;;
  execute)     execute ;;
  monitor)     monitor "${2:?usage: $0 monitor <execution-name>}" ;;
  kill)        kill_switch "$@" ;;
  post-verify) post_verify ;;
  *)
    echo "usage: $0 {preflight [--smoke-active]|execute|monitor <execution>|kill <execution>|post-verify}" >&2
    exit 2
    ;;
esac

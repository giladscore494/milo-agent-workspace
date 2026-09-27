#!/usr/bin/env bash
# Turn the replay capture (MILO_CAPTURE_REPLAY, backend/replay_capture.py) on
# or off on the WORKER JOB ONLY -- the `capture-flag` workflow.
#
#   on   refused unless 0 non-terminal runs exist: the capture keeps the NEXT
#        run's inert provider outputs, so it is turned on between runs only.
#   off  always allowed.
#
# The API service, the capture job and every other flag are untouched. The
# value is assembled at run time from the argument (never committed enabled:
# scripts/check_unsafe_defaults.py). The job summary says to turn it OFF after
# one run. --dry-run prints the commands and calls nothing.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

STATE=""
usage() {
  printf 'Usage: capture-flag.sh on|off [--dry-run] [--operator-config <path>]\n'
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    on | off) STATE="$1"; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --operator-config) MILO_OPERATOR_CONFIG_PATH="${2:?}"; shift 2 ;;
    --help) usage; exit 0 ;;
    *) printf 'FAIL: unknown argument %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done
[[ -n "$STATE" ]] || { usage >&2; exit 2; }
ops_load_config

VALUE="false"
[[ "$STATE" == "on" ]] && VALUE="true"
summary_header "Replay capture ${STATE} (worker job ${WORKER_JOB} only)"

if [[ "$STATE" == "on" ]]; then
  ops_require_zero_live_runs "1 live-runs"
else
  summary "1 live-runs" SKIPPED "turning the capture off is always allowed"
fi

ops_run gcloud run jobs update "$WORKER_JOB" --region "$REGION" --project "$PROJECT_ID" \
  --update-env-vars "${MILO_REPLAY_CAPTURE_FLAG_NAME}=${VALUE}"
if [[ "$DRY_RUN" -eq 1 ]]; then
  summary "2 worker-flag" DRY-RUN "would set ${MILO_REPLAY_CAPTURE_FLAG_NAME}=${VALUE} on the worker job only"
else
  facts="$(ops_worker_json | ops_container_facts)" || ops_fail "the worker job could not be read back" "2 worker-flag"
  grep -qxF "env"$'\t'"${MILO_REPLAY_CAPTURE_FLAG_NAME}"$'\t'"${VALUE}" <<< "$facts" \
    || ops_fail "${MILO_REPLAY_CAPTURE_FLAG_NAME} did not read back as ${VALUE}" "2 worker-flag"
  summary "2 worker-flag" PASS "${MILO_REPLAY_CAPTURE_FLAG_NAME}=${VALUE} on ${WORKER_JOB}"
fi
if [[ "$STATE" == "on" ]]; then
  summary_note "Turn the replay capture OFF after ONE run: run the capture-flag workflow with state=off."
fi
exit "$OPS_FAILED"

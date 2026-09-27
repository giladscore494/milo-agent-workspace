#!/usr/bin/env bash
# The production kill switch from a workflow -- the `kill-switch` workflow
# (emergency: no reviewer wait, still this repository and main only). It wraps
# scripts/deploy/kill-switch.sh, the ONE canonical emergency order, unchanged:
# the acknowledgement is supplied here because the workflow's own confirmation
# input already required it. --dry-run is the canonical script's dry run.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

DEPLOYMENT="" SCOPE_ARGS=()
usage() {
  printf 'Usage: kill-switch.sh --vercel-deployment <url> [--order-only|--remaining-only] [--dry-run]\n'
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --vercel-deployment) DEPLOYMENT="${2:?}"; shift 2 ;;
    --order-only | --remaining-only) SCOPE_ARGS+=("$1"); shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --operator-config) MILO_OPERATOR_CONFIG_PATH="${2:?}"; shift 2 ;;
    --help) usage; exit 0 ;;
    *) printf 'FAIL: unknown argument %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done
ops_load_config
summary_header "Kill switch"
switch=(bash "${REPO_ROOT}/scripts/deploy/kill-switch.sh" --operator-config "$CONFIG_PATH" "${SCOPE_ARGS[@]}")
if [[ "$DRY_RUN" -eq 1 ]]; then
  "${switch[@]}" --dry-run
  summary "kill-switch" DRY-RUN "the canonical emergency order was printed; nothing was changed"
  exit 0
fi
[[ "$DEPLOYMENT" =~ ^https://[A-Za-z0-9.-]+$ ]] \
  || ops_fail "--vercel-deployment must be the https URL of the current production deployment" "kill-switch"
status=0
MILO_OPERATOR_ACK="I_UNDERSTAND_THIS_CHANGES_PRODUCTION" \
  "${switch[@]}" --apply --vercel-deployment "$DEPLOYMENT" || status=$?
if [[ "$status" -eq 0 ]]; then
  summary "kill-switch" PASS "every step succeeded and every read-back flag is closed"
else
  summary "kill-switch" FAIL "exit ${status}: a step failed or a flag did not read back closed (above)"
fi
exit "$status"

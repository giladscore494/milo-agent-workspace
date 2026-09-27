#!/usr/bin/env bash
# Arm Stage 2 for ONE prepared plan revision -- the `arm` workflow (reviewer
# required). The canonical activation script, in its own order:
#
#   1. website-execution-activate.sh --apply-runtime-policy   (caps; enables nothing)
#   2. website-execution-activate.sh --apply-backend <rev>    (checks run starts
#      are CLOSED and the prepared gate, then arms the worker, then the API)
#   3. production-verify.sh --gate armed <rev>                (the pre-open gate)
#
# It never opens run starts on the website (the LAST step, in Vercel, stays a
# person's) and never starts a run. --dry-run prints the commands and calls
# nothing.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

WS_ID="" WS_REV="" WS_DIGEST=""
usage() {
  printf 'Usage: arm.sh --work-scope-id <uuid> --work-scope-revision <n> --work-scope-digest <hex> [--dry-run]\n'
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --work-scope-id) WS_ID="${2:?}"; shift 2 ;;
    --work-scope-revision) WS_REV="${2:?}"; shift 2 ;;
    --work-scope-digest) WS_DIGEST="${2:?}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --operator-config) MILO_OPERATOR_CONFIG_PATH="${2:?}"; shift 2 ;;
    --help) usage; exit 0 ;;
    *) printf 'FAIL: unknown argument %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done
[[ "$WS_ID" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] \
  || { printf 'FAIL: --work-scope-id must be the plan'"'"'s lowercase UUID\n' >&2; exit 2; }
[[ "$WS_REV" =~ ^[1-9][0-9]{0,8}$ ]] || { printf 'FAIL: --work-scope-revision must be a whole number\n' >&2; exit 2; }
[[ "$WS_DIGEST" =~ ^[0-9a-f]{64}$ ]] || { printf 'FAIL: --work-scope-digest must be 64 hex characters\n' >&2; exit 2; }
ops_load_config
WS_ARGS=(--work-scope-id "$WS_ID" --work-scope-revision "$WS_REV" --work-scope-digest "$WS_DIGEST")
activate=(bash "${REPO_ROOT}/scripts/deploy/website-execution-activate.sh" --operator-config "$CONFIG_PATH")
summary_header "Arm Stage 2 for revision ${WS_REV} of ${WS_ID:0:8}"

if [[ "$DRY_RUN" -eq 1 ]]; then
  ops_run "${activate[@]}" --apply-runtime-policy
  ops_run "${activate[@]}" --apply-backend "${WS_ARGS[@]}"
  ops_run bash "${REPO_ROOT}/scripts/deploy/production-verify.sh" --operator-config "$CONFIG_PATH" --gate armed "${WS_ARGS[@]}"
  summary "arm" DRY-RUN "would apply the runtime policy, arm the worker then the API, and run the armed gate"
  exit 0
fi
"${activate[@]}" --apply-runtime-policy || ops_fail "the runtime policy was not applied (above)" "1 runtime-policy"
summary "1 runtime-policy" PASS "reviewed caps bound and read back"
"${activate[@]}" --apply-backend "${WS_ARGS[@]}" || ops_fail "Stage 2 was not applied (above); the website still refuses every run start" "2 backend"
summary "2 backend" PASS "worker then API armed and read back"
bash "${REPO_ROOT}/scripts/deploy/production-verify.sh" --operator-config "$CONFIG_PATH" --gate armed "${WS_ARGS[@]}" \
  || ops_fail "the armed gate did not pass (above); do NOT open run starts" "3 armed-gate"
summary "3 armed-gate" PASS "RUN_START_PATH proved DISABLED and everything else VERIFIED"
summary_note "The last step is a person's: open run starts in Vercel (GATEWAY_ALLOW_RUN_START_ROUTES), then run the gates workflow with gate=active."
exit "$OPS_FAILED"

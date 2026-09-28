#!/usr/bin/env bash
# Which release production runs -- for the `website-stage` workflow, whose
# canonical tools act on `git rev-parse HEAD` and must therefore run from the
# DEPLOYED commit, not from wherever main has moved since.
#
#   --sha <40-hex>  use that commit (the tools' own deployed gate still proves
#                   production runs it);
#   (no --sha)      read MILO_RELEASE_SHA from the API service and the worker
#                   job (read-only); they must agree.
#
# Writes `sha=<commit>` to $GITHUB_OUTPUT. --dry-run prints the reads, calls
# nothing and answers the checkout's HEAD.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

SHA=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sha) SHA="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --operator-config) MILO_OPERATOR_CONFIG_PATH="${2:?}"; shift 2 ;;
    --help) printf 'Usage: deployed-release.sh [--sha <40-hex>] [--dry-run]\n'; exit 0 ;;
    *) printf 'FAIL: unknown argument %s\n' "$1" >&2; exit 2 ;;
  esac
done
if [[ -n "$SHA" && ! "$SHA" =~ ^[0-9a-f]{40}$ ]]; then
  printf 'FAIL: sha must be empty or exactly 40 lowercase hexadecimal characters\n' >&2
  exit 2
fi
ops_load_config
summary_header "Release to act on"

release_of() {
  # release_of services|jobs NAME -- MILO_RELEASE_SHA of that resource.
  gcloud run "$1" describe "$2" --region "$REGION" --project "$PROJECT_ID" --format=json \
    | ops_container_facts | awk -F '\t' '$1 == "env" && $2 == "MILO_RELEASE_SHA" { print $3 }'
}

if [[ -n "$SHA" ]]; then
  summary "0 release" PASS "the sha input ${SHA:0:12}; the deployed gate proves production runs it"
elif [[ "$DRY_RUN" -eq 1 ]]; then
  ops_run gcloud run services describe "$API_SERVICE" --region "$REGION" --project "$PROJECT_ID" --format=json
  ops_run gcloud run jobs describe "$WORKER_JOB" --region "$REGION" --project "$PROJECT_ID" --format=json
  SHA="$(git -C "$REPO_ROOT" rev-parse HEAD)"
  summary "0 release" DRY-RUN "would read MILO_RELEASE_SHA from ${API_SERVICE} and ${WORKER_JOB}; the dry run uses ${SHA:0:12}"
else
  api="$(release_of services "$API_SERVICE")" || ops_fail "the API service could not be read" "0 release"
  worker="$(release_of jobs "$WORKER_JOB")" || ops_fail "the worker job could not be read" "0 release"
  [[ "$api" =~ ^[0-9a-f]{40}$ ]] || ops_fail "the API service carries no MILO_RELEASE_SHA; pass the sha input" "0 release"
  [[ "$api" == "$worker" ]] \
    || ops_fail "the API runs ${api:0:12} but the worker runs ${worker:0:12}; deploy one release first" "0 release"
  SHA="$api"
  summary "0 release" PASS "production runs ${SHA:0:12} (API and worker)"
fi
if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  printf 'sha=%s\n' "$SHA" >> "$GITHUB_OUTPUT"
fi
printf 'RELEASE_SHA=%s\n' "$SHA"

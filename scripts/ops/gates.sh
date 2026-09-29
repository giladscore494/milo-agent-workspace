#!/usr/bin/env bash
# Read-only: one production-verify.sh gate -- the `gates` workflow. Writes each
# fact as a SUMMARY| line. Changes nothing, makes no paid call, creates no run.
# --dry-run prints the command and calls nothing.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

GATE="" WS_ARGS=()
usage() {
  printf 'Usage: gates.sh --gate database|deployed|prepared|armed|active [--work-scope-id <uuid> --work-scope-revision <n> --work-scope-digest <hex>] [--dry-run]\n'
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --gate) GATE="${2:?}"; shift 2 ;;
    --work-scope-id) [[ -z "${2:-}" ]] || WS_ARGS+=("$1" "$2"); shift 2 ;;
    --work-scope-revision) [[ -z "${2:-}" ]] || WS_ARGS+=("$1" "$2"); shift 2 ;;
    --work-scope-digest) [[ -z "${2:-}" ]] || WS_ARGS+=("$1" "$2"); shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --operator-config) MILO_OPERATOR_CONFIG_PATH="${2:?}"; shift 2 ;;
    --help) usage; exit 0 ;;
    *) printf 'FAIL: unknown argument %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done
case "$GATE" in database | deployed | prepared | armed | active) ;; *) usage >&2; exit 2 ;; esac
if [[ "${#WS_ARGS[@]}" -ne 0 && "${#WS_ARGS[@]}" -ne 6 ]]; then
  printf 'FAIL: name all three of --work-scope-id, --work-scope-revision and --work-scope-digest, or none\n' >&2
  exit 2
fi
ops_load_config
summary_header "Gate ${GATE} (read-only)"
command=(bash "${REPO_ROOT}/scripts/deploy/production-verify.sh" --operator-config "$CONFIG_PATH"
         --gate "$GATE" "${WS_ARGS[@]}")
if [[ "$DRY_RUN" -eq 1 ]]; then
  ops_run "${command[@]}"
  summary "gate ${GATE}" DRY-RUN "would run production-verify.sh --gate ${GATE}"
  exit 0
fi
status=0
output="$("${command[@]}" 2>&1)" || status=$?
printf '%s\n' "$output"
# Each fact, as the verifier stated it (NAME=VALUE (detail)).
while IFS= read -r line; do
  name="${line%%=*}"
  rest="${line#*=}"
  summary "$name" "${rest%% *}" "$(sed -n 's/^[A-Z]* *//p' <<< "$rest")"
done < <(grep -E '^(CODE_DEPLOYED|DATABASE_READY|EVIDENCE_READY|BATCH_READY|RUNS_QUIESCENT|GATEWAY_ENABLED|RUN_START_PATH|WEBSITE_ENABLED|PAID_EXECUTION_READY)=' <<< "$output" || true)

# PR-D1: REGISTER_COVERAGE -- the register directory version, units and rows
# captured of the directory's totals, the database size against the API's
# capacity threshold, and the unverified register snapshots. Read-only (the
# read-only role, public.catalog_register_coverage()); informational, except
# FAIL when the database is above the threshold. The URL is read from the
# named variable and never printed; psql's stderr is dropped because it can
# quote the connection target.
coverage_json="" coverage_status=0
db_url="${!DB_URL_ENV:-}"
if [[ -n "$db_url" ]]; then
  coverage_json="$(psql "$db_url" -X -A -t -v ON_ERROR_STOP=1 \
    -c 'select public.catalog_register_coverage()::text;' 2> /dev/null)" || coverage_json=""
fi
service_json="$(mktemp)"
trap 'rm -f "$service_json"' EXIT
gcloud run services describe "$API_SERVICE" --region "$REGION" --project "$PROJECT_ID" \
  --format=json > "$service_json" 2> /dev/null || : > "$service_json"
service_args=()
[[ -s "$service_json" ]] && service_args=(--service-json "$service_json")
coverage_line="$(cd "$REPO_ROOT" && python3 -m backend.catalog.register.coverage "${service_args[@]}" \
  <<< "${coverage_json:-null}")" || coverage_status=$?
printf '%s\n' "$coverage_line"
coverage_rest="${coverage_line#REGISTER_COVERAGE=}"
summary "REGISTER_COVERAGE" "${coverage_rest%% *}" "${coverage_rest#* }"
if [[ "$coverage_status" -ne 0 && "$status" -eq 0 ]]; then
  status=1
  summary "gate ${GATE}" FAIL "the database is above the register capacity threshold (REGISTER_COVERAGE)"
elif [[ "$status" -eq 0 ]]; then
  summary "gate ${GATE}" PASS "every fact the gate requires is VERIFIED"
else
  summary "gate ${GATE}" FAIL "exit ${status}; see the facts above"
fi
exit "$status"

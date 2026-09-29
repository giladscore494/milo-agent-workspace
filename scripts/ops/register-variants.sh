#!/usr/bin/env bash
# Catalog variant backfill (PR-L1, L1-4) -- the `register-variants` workflow.
#
#   --snapshot-key <key>   builds the deterministic catalog variants of ONE
#                          active, count-verified, whole-tozar Government
#                          snapshot: the capture job runs
#                          `python -m backend.catalog.register.variants
#                          --snapshot-key <key>` (service role), which reads
#                          the snapshot's stored rows in bounded batches and
#                          writes them through public.record_catalog_variants.
#
# Idempotent: a snapshot already built under the current mapper version is
# answered `unchanged` without reading a row; a partial build is completed.
# $0: no Government request, no model, no run. A newly captured register unit
# is built by the capture job itself; this is for snapshots that were already
# active (the Toyota snapshot Prepare captured) and for a build that failed.
# --dry-run prints the command and calls nothing. Prints the snapshot key,
# counts and static codes -- never a URL, a key or a register value.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

SNAPSHOT_KEY=""
usage() {
  cat << 'EOF'
Usage: register-variants.sh --snapshot-key <key> [--dry-run] [--operator-config <path>]

Builds the catalog variants of one active Government snapshot (idempotent).
EOF
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --snapshot-key) SNAPSHOT_KEY="${2-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --operator-config) MILO_OPERATOR_CONFIG_PATH="${2:?}"; shift 2 ;;
    --help) usage; exit 0 ;;
    *) printf 'FAIL: unknown argument %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done
[[ "$SNAPSHOT_KEY" =~ ^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$ ]] \
  || { printf 'REFUSED CATALOG_VARIANT_REQUEST_INVALID: --snapshot-key must be one snapshot key\n' >&2; exit 2; }

ops_load_config
summary_header "Catalog variants: build ${SNAPSHOT_KEY} (deterministic, \$0)"
ARGS="--args=-m,backend.catalog.register.variants,--snapshot-key,${SNAPSHOT_KEY}"

if [[ "$DRY_RUN" -eq 1 ]]; then
  ops_run gcloud run jobs execute "<CLOUD_RUN_CAPTURE_JOB>" --region "$REGION" --project "$PROJECT_ID" "$ARGS" --async
  summary "register-variants" DRY-RUN "would build the variants of ${SNAPSHOT_KEY}"
  summary_note "DRY RUN: nothing was called and nothing was changed."
  exit 0
fi

milo_require_op CLOUD_RUN_CAPTURE_JOB || exit 2
CAPTURE_JOB="$(milo_op CLOUD_RUN_CAPTURE_JOB)"
milo_require_gcloud_context "$PROJECT_ID" || exit 2
# The capture job's image and identity (service-role database access), with
# its arguments replaced for this one execution.
gcloud_status=0
execution="$(gcloud run jobs execute "$CAPTURE_JOB" --region "$REGION" --project "$PROJECT_ID" \
  "$ARGS" --async --format='value(metadata.name)')" || gcloud_status=$?
if [[ ! "$execution" =~ ^[a-z]([-a-z0-9]{0,126}[a-z0-9])?$ ]]; then
  ops_fail "gcloud run jobs execute (exit ${gcloud_status}) named no execution. Check: gcloud run jobs executions list --job ${CAPTURE_JOB} --region ${REGION} --project ${PROJECT_ID}" "register-variants"
fi
printf 'execution: %s\n' "$execution"

poll_seconds="${MILO_VARIANTS_POLL_SECONDS:-10}" deadline="${MILO_VARIANTS_WAIT_SECONDS:-3600}" waited=0 state="running"
while :; do
  state="$(gcloud run jobs executions describe "$execution" --region "$REGION" --project "$PROJECT_ID" \
    --format='value(status.conditions[0].type,status.conditions[0].status)' 2> /dev/null || true)"
  case "$state" in
    Completed*True) state="succeeded"; break ;;
    Completed*False) state="failed"; break ;;
  esac
  (( waited < deadline )) || ops_fail "execution ${execution} did not finish within ${deadline}s" "register-variants"
  sleep "$poll_seconds"
  waited=$(( waited + poll_seconds ))
done

# The build's own line (BUILT / REFUSED / FAILED ...), read back from the
# execution's log: the snapshot key, counts and static codes only.
outcome=""
for _attempt in 1 2 3 4 5 6; do
  outcome="$(gcloud logging read \
    "resource.type=cloud_run_job AND labels.\"run.googleapis.com/execution_name\"=${execution}" \
    --project "$PROJECT_ID" --format='value(textPayload)' --limit 400 2> /dev/null \
    | grep -E '^(BUILT|REFUSED|FAILED) ' | head -n 1 || true)"
  [[ -n "$outcome" ]] && break
  sleep "$poll_seconds"
done
printf '%s\n' "${outcome:-<no outcome line in the execution log>}"
if [[ "$state" == "succeeded" && "$outcome" == BUILT\ * ]]; then
  summary "register-variants" PASS "${outcome#BUILT }"
  exit 0
fi
summary "register-variants" FAIL "execution ${execution} ${state}: ${outcome:-no outcome line}"
exit 1

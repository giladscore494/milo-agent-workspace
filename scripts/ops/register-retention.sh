#!/usr/bin/env bash
# Register snapshot retention (PR-D1, D1-5 / O22) -- the `register-retention`
# workflow. DRY-RUN FIRST:
#
#   --list  (default) read-only, as the read-only database role: every
#           prunable register snapshot with its raw rows and estimated bytes,
#           the totals, and the DIGEST of that exact list
#           (public.catalog_register_prunable_snapshots / _prune_digest).
#   --apply --confirm PRUNE --digest <hex>
#           prunes EXACTLY the list that digest names: the list is read again
#           and must still carry that digest, then the capture job runs
#           `python -m backend.catalog.register.prune --apply` (service role),
#           which recomputes the list, and `public.prune_register_snapshots`
#           recomputes it once more under a table lock. Any difference refuses
#           and nothing is deleted.
#
# Always kept (the rule is the database's): per tozar the active snapshot and
# the one before it, its current variant build, and every snapshot referenced
# by evidence, claims, runs, work-scope revisions or a `register` ledger row.
# A pruned snapshot's variants go with it; an old mapper version's variant
# rows are prunable once the current mapper's build of the snapshot is
# complete. Unscoped (whole-register)
# snapshots are never candidates. Prune deletes DATABASE ROWS ONLY: no archive
# object is ever touched (the capture identity cannot delete one anyway).
# --dry-run prints the commands and calls nothing. Prints snapshot keys,
# counts, bytes and the digest -- never a URL, a key or a value.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

MODE="list" CONFIRM="" DIGEST=""
usage() {
  cat << 'EOF'
Usage: register-retention.sh [--list | --apply --confirm PRUNE --digest <hex>] [--dry-run]
                             [--operator-config <path>]

--list (default) is read-only and prints the prunable list and its digest.
--apply prunes exactly that list (database rows only, never an archive object).
EOF
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --list) MODE="list"; shift ;;
    --apply) MODE="apply"; shift ;;
    --confirm) CONFIRM="${2-}"; shift 2 ;;
    --digest) DIGEST="${2-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --operator-config) MILO_OPERATOR_CONFIG_PATH="${2:?}"; shift 2 ;;
    --help) usage; exit 0 ;;
    *) printf 'FAIL: unknown argument %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done
if [[ "$MODE" == "list" && ( -n "$CONFIRM" || -n "$DIGEST" ) ]]; then
  printf 'FAIL: --confirm and --digest belong to --apply\n' >&2
  exit 2
fi
if [[ "$MODE" == "apply" ]]; then
  [[ "$CONFIRM" == "PRUNE" ]] \
    || { printf 'REFUSED CATALOG_PRUNE_NOT_CONFIRMED: --confirm must be exactly PRUNE\n' >&2; exit 2; }
  [[ "$DIGEST" =~ ^[0-9a-f]{64}$ ]] \
    || { printf 'REFUSED CATALOG_PRUNE_REQUEST_INVALID: --digest must be the 64-hex digest the dry-run printed\n' >&2; exit 2; }
fi

ops_load_config
summary_header "Register retention: ${MODE} (database rows only; archive objects untouched)"

# One statement, so the rows and the digest describe the same list.
# PR-L1b: old-mapper variant builds are listed too, and the digest covers both.
LIST_SQL="with p as (select * from public.catalog_register_prunable_snapshots()),
     b as (select * from public.catalog_register_prunable_variant_builds())
select 'PRUNABLE ' || snapshot_key || ' rows=' || raw_rows || ' estimated_bytes=' || estimated_bytes
  from p
union all
select 'PRUNABLE-VARIANTS ' || snapshot_key || ' mapper_version=' || mapper_version || ' rows=' || variant_rows
       || ' estimated_bytes=' || estimated_bytes
  from b
union all
select 'TOTAL snapshots=' || (select count(*) from p) || ' rows=' || (select coalesce(sum(raw_rows), 0) from p)
       || ' estimated_bytes=' || ((select coalesce(sum(estimated_bytes), 0) from p)
                                  + (select coalesce(sum(estimated_bytes), 0) from b))
       || ' variant_builds=' || (select count(*) from b)
union all
select 'DIGEST ' || public.catalog_register_prune_digest(
         coalesce((select array_agg(snapshot_key) from p), '{}') || coalesce((select array_agg(item) from b), '{}'));"

# read_list -- the list, as the read-only role. The URL is read from the named
# variable and never printed; psql's stderr is dropped because it can quote
# the connection target.
read_list() {
  local url="${!DB_URL_ENV:-}"
  [[ -n "$url" ]] || { printf 'FAIL: %s is not set: the read-only database URL is required\n' "$DB_URL_ENV" >&2; return 1; }
  psql "$url" -X -A -t -v ON_ERROR_STOP=1 -c "$LIST_SQL" 2> /dev/null
}

if [[ "$DRY_RUN" -eq 1 ]]; then
  ops_run psql "\$${DB_URL_ENV}" -X -A -t -v ON_ERROR_STOP=1 -c "select ... from public.catalog_register_prunable_snapshots()"
  if [[ "$MODE" == "apply" ]]; then
    ops_run gcloud run jobs execute "<CLOUD_RUN_CAPTURE_JOB>" --region "$REGION" --project "$PROJECT_ID" \
      "--args=-m,backend.catalog.register.prune,--apply,--confirm,PRUNE,--digest,${DIGEST}" --async
  fi
  detail="would read the prunable list (read-only)"
  [[ "$MODE" == "apply" ]] && detail+=" and prune exactly the list digest ${DIGEST}"
  summary "register-retention ${MODE}" DRY-RUN "$detail"
  summary_note "DRY RUN: nothing was called and nothing was changed."
  exit 0
fi

listing="$(read_list)" || ops_fail "the prunable list could not be read (read-only role)" "register-retention list"
printf '%s\n' "$listing"
current="$(sed -n 's/^DIGEST //p' <<< "$listing")"
totals="$(sed -n 's/^TOTAL //p' <<< "$listing")"
[[ "$current" =~ ^[0-9a-f]{64}$ ]] || ops_fail "the prunable list carried no digest" "register-retention list"
summary "register-retention list" PASS "${totals}; digest ${current}"

if [[ "$MODE" == "list" ]]; then
  summary_note "Dry run only: nothing was deleted. To prune exactly this list, dispatch register-retention with mode=apply, confirm=PRUNE and digest=${current}."
  exit 0
fi

if [[ "$current" != "$DIGEST" ]]; then
  summary "register-retention apply" FAIL "CATALOG_PRUNE_DIGEST_MISMATCH: the prunable list is not the one that digest names; nothing was deleted"
  printf 'REFUSED CATALOG_PRUNE_DIGEST_MISMATCH: run the dry-run again and apply its digest\n' >&2
  exit 1
fi
if [[ "$totals" == snapshots=0\ * && "$totals" == *" variant_builds=0" ]]; then
  summary "register-retention apply" PASS "nothing is prunable; nothing was deleted"
  exit 0
fi

milo_require_op CLOUD_RUN_CAPTURE_JOB || exit 2
CAPTURE_JOB="$(milo_op CLOUD_RUN_CAPTURE_JOB)"
milo_require_gcloud_context "$PROJECT_ID" || exit 2
# The prune runs on the capture job's image and identity (service-role
# database access), with its arguments replaced for this one execution.
gcloud_status=0
execution="$(gcloud run jobs execute "$CAPTURE_JOB" --region "$REGION" --project "$PROJECT_ID" \
  "--args=-m,backend.catalog.register.prune,--apply,--confirm,PRUNE,--digest,${DIGEST}" \
  --async --format='value(metadata.name)')" || gcloud_status=$?
if [[ ! "$execution" =~ ^[a-z]([-a-z0-9]{0,126}[a-z0-9])?$ ]]; then
  ops_fail "gcloud run jobs execute (exit ${gcloud_status}) named no execution. Check: gcloud run jobs executions list --job ${CAPTURE_JOB} --region ${REGION} --project ${PROJECT_ID}" "register-retention apply"
fi
printf 'execution: %s\n' "$execution"

poll_seconds="${MILO_RETENTION_POLL_SECONDS:-10}" deadline="${MILO_RETENTION_WAIT_SECONDS:-3600}" waited=0 state="running"
while :; do
  state="$(gcloud run jobs executions describe "$execution" --region "$REGION" --project "$PROJECT_ID" \
    --format='value(status.conditions[0].type,status.conditions[0].status)' 2> /dev/null || true)"
  case "$state" in
    Completed*True) state="succeeded"; break ;;
    Completed*False) state="failed"; break ;;
  esac
  (( waited < deadline )) || ops_fail "execution ${execution} did not finish within ${deadline}s" "register-retention apply"
  sleep "$poll_seconds"
  waited=$(( waited + poll_seconds ))
done

# The prune's own lines (PRUNED / REFUSED / FAILED ...), read back from the
# execution's log: snapshot keys, counts and static codes only. Repeated for up
# to MILO_RETENTION_LOG_WAIT_SECONDS (Cloud Logging lags the execution, P49);
# no outcome line is a FAIL.
log_wait="${MILO_RETENTION_LOG_WAIT_SECONDS:-120}" log_poll="${MILO_RETENTION_LOG_POLL_SECONDS:-5}"
outcome="$(ops_execution_outcome "$execution" '^(PRUNED|REFUSED|FAILED) ' "$log_wait" "$log_poll" || true)"
printf '%s\n' "${outcome:-<no outcome line in the execution log after ${log_wait}s>}"
if [[ "$state" == "succeeded" && "$outcome" == PRUNED\ * ]]; then
  summary "register-retention apply" PASS "${outcome#PRUNED }"
  summary_note "Pruned exactly the list digest ${DIGEST}. Archive objects were not touched."
  exit 0
fi
summary "register-retention apply" FAIL "execution ${execution} ${state}: ${outcome:-no outcome line in its log after ${log_wait}s}"
exit 1

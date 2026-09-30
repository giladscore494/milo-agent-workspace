#!/usr/bin/env bash
# register-vacuum.sh -- PR-L2: hand the space compaction freed back to the plan.
#
# Compaction (PR-L2) removes the register payloads and the compacted
# candidates' identity, and a superseded snapshot keeps only its referenced
# rows; PostgreSQL reuses that space for new rows, but pg_database_size -- what
# the plan limit and the capture gate count -- drops only when the two tables
# are rewritten: VACUUM (FULL, ANALYZE) of public.catalog_raw_records (with its
# TOAST table) and public.catalog_candidate_variants.
#
#   --sizes (default)  read-only: the two tables' sizes, pg_database_size,
#                      what is live (runs, register captures) and, per table,
#                      has_table_privilege(current_user, table, 'MAINTAIN')
#                      as PASS/FAIL
#   --apply --confirm VACUUM
#                      refuses unless the role reads back MAINTAIN on both
#                      tables and no run and no register capture is non-
#                      terminal (a capture, a Prepare and a product run are
#                      all runs), then
#                      VACUUM (FULL, ANALYZE) one table at a time and the sizes
#                      after
#
# VACUUM FULL holds ACCESS EXCLUSIVE on the table while it rewrites it
# (seconds for tens of MB): every read and write of that table waits. It waits
# at most 5 s for the lock (lock_timeout), so it never queues behind a long
# transaction while holding everything else up. It runs as the release
# read-only role (MILO_READONLY_DB_URL), which may MAINTAIN exactly these two
# tables (migration 20261002000100, PostgreSQL 17) -- no owner password. It
# cannot run inside a transaction, so never through an RPC.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/ops/common.sh
source "${SCRIPT_DIR}/common.sh"

MODE="sizes" CONFIRM=""
TABLES=(catalog_raw_records catalog_candidate_variants)
usage() {
  cat << 'EOF'
Usage: register-vacuum.sh [--sizes | --apply --confirm VACUUM] [--dry-run] [--operator-config <path>]

--sizes (default) is read-only: sizes and what is live.
--apply rewrites catalog_raw_records and catalog_candidate_variants (VACUUM
(FULL, ANALYZE)), refused while any run or register capture is live.
EOF
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sizes) MODE="sizes"; shift ;;
    --apply) MODE="apply"; shift ;;
    --confirm) CONFIRM="${2:?--confirm needs a value}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --operator-config) MILO_OPERATOR_CONFIG_PATH="${2:?}"; shift 2 ;;
    --help) usage; exit 0 ;;
    *) printf 'FAIL: unknown argument %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done
if [[ "$MODE" == "sizes" && -n "$CONFIRM" ]]; then
  printf 'FAIL: --confirm belongs to --apply\n' >&2
  exit 2
fi
if [[ "$MODE" == "apply" && "$CONFIRM" != "VACUUM" ]]; then
  printf 'REFUSED CATALOG_VACUUM_NOT_CONFIRMED: --confirm must be exactly VACUUM\n' >&2
  exit 2
fi

ops_load_config
summary_header "Register space reclamation: ${MODE} (VACUUM (FULL, ANALYZE) of ${TABLES[*]})"

SIZES_SQL="select 'SIZE ' || c.relname || ' total=' || pg_total_relation_size(c.oid) || ' heap=' || pg_relation_size(c.oid)
       || ' toast=' || coalesce(pg_total_relation_size(nullif(c.reltoastrelid, 0)), 0)
  from pg_class c
 where c.oid in ('public.catalog_raw_records'::regclass, 'public.catalog_candidate_variants'::regclass)
union all
-- MAINTAIN is a PostgreSQL 17 privilege: an older server has none to read (FAIL).
select 'MAINTAIN ' || c.relname || '='
       || case when current_setting('server_version_num')::integer < 170000 then 'FAIL'
               when has_table_privilege(current_user, c.oid, 'MAINTAIN') then 'PASS' else 'FAIL' end
  from pg_class c
 where c.oid in ('public.catalog_raw_records'::regclass, 'public.catalog_candidate_variants'::regclass)
union all
select 'DATABASE bytes=' || pg_database_size(current_database())
union all
select 'LIVE runs=' || (b->>'live_runs') || ' register_captures=' || (b->>'live_register_groups')
  from public.catalog_register_maintenance_blockers() b;"

db_url() {
  local url="${!DB_URL_ENV:-}"
  [[ -n "$url" ]] || { printf 'FAIL: %s is not set: the read-only database URL is required\n' "$DB_URL_ENV" >&2; return 1; }
  printf '%s' "$url"
}
read_sizes() {
  local url
  url="$(db_url)" || return 1
  psql "$url" -X -A -t -v ON_ERROR_STOP=1 -c "$SIZES_SQL" 2> /dev/null
}

if [[ "$DRY_RUN" -eq 1 ]]; then
  ops_run psql "\$${DB_URL_ENV}" -X -A -t -v ON_ERROR_STOP=1 -c "select ... sizes, MAINTAIN read-back, pg_database_size, catalog_register_maintenance_blockers()"
  if [[ "$MODE" == "apply" ]]; then
    for table in "${TABLES[@]}"; do
      ops_run psql "\$${DB_URL_ENV}" -X -v ON_ERROR_STOP=1 -c "set lock_timeout = '5s'" -c "vacuum (full, analyze) public.${table}"
    done
  fi
  summary "register-vacuum ${MODE}" DRY-RUN "would read the sizes (read-only)$([[ "$MODE" == "apply" ]] && printf ' and rewrite %s' "${TABLES[*]}")"
  summary_note "DRY RUN: nothing was called and nothing was changed."
  exit 0
fi

before="$(read_sizes)" || ops_fail "the sizes could not be read (read-only role)" "register-vacuum sizes"
printf '%s\n' "$before"
database_before="$(sed -n 's/^DATABASE bytes=//p' <<< "$before")"
live="$(sed -n 's/^LIVE //p' <<< "$before")"
[[ "$database_before" =~ ^[0-9]+$ && "$live" =~ ^runs=([0-9]+)\ register_captures=([0-9]+)$ ]] \
  || ops_fail "the sizes carried no database size or live counts" "register-vacuum sizes"
live_runs="${BASH_REMATCH[1]}" live_captures="${BASH_REMATCH[2]}"
summary "register-vacuum sizes" PASS "pg_database_size ${database_before} bytes; live runs ${live_runs}, register captures ${live_captures}"
maintain_ok=1
for table in "${TABLES[@]}"; do
  if grep -qx "MAINTAIN ${table}=PASS" <<< "$before"; then
    summary "register-vacuum maintain ${table}" PASS "the read-only role may MAINTAIN public.${table}"
  else
    maintain_ok=0
    summary "register-vacuum maintain ${table}" FAIL "the read-only role may not MAINTAIN public.${table} (migration 20261002000100 grants it on PostgreSQL 17)"
  fi
done

if [[ "$MODE" == "sizes" ]]; then
  summary_note "Read-only: nothing was changed. To rewrite the two tables, dispatch register-retention with operation=vacuum-full, mode=apply and confirm=VACUUM while nothing is live."
  exit 0
fi

if (( ! maintain_ok )); then
  printf 'REFUSED CATALOG_VACUUM_NOT_PERMITTED: the read-only role lacks MAINTAIN on a table; nothing was rewritten\n' >&2
  exit 1
fi
if (( live_runs > 0 || live_captures > 0 )); then
  summary "register-vacuum apply" FAIL "CATALOG_VACUUM_BLOCKED: ${live_runs} live run(s), ${live_captures} live register capture(s); nothing was rewritten"
  printf 'REFUSED CATALOG_VACUUM_BLOCKED: wait until no run and no register capture is live\n' >&2
  exit 1
fi
url="$(db_url)"
for table in "${TABLES[@]}"; do
  # The timeout rides the connection itself (PGOPTIONS), so it holds even if
  # the two commands reached different backends; a pooler that refuses it
  # refuses the VACUUM too (use the direct or session-mode connection).
  if ! PGOPTIONS="-c lock_timeout=5s" psql "$url" -X -q -v ON_ERROR_STOP=1 -c "set lock_timeout = '5s'" \
       -c "vacuum (full, analyze) public.${table}" > /dev/null 2>&1; then
    summary "register-vacuum apply" FAIL "VACUUM (FULL, ANALYZE) public.${table} did not complete (a lock not granted within 5 s, no MAINTAIN privilege, or a pooler that refuses the connection's lock_timeout); tables before it were rewritten"
    exit 1
  fi
  printf 'VACUUMED public.%s\n' "$table"
done
after="$(read_sizes)" || ops_fail "the sizes after could not be read" "register-vacuum apply"
printf '%s\n' "$after"
database_after="$(sed -n 's/^DATABASE bytes=//p' <<< "$after")"
summary "register-vacuum apply" PASS "pg_database_size ${database_before} -> ${database_after} bytes"
exit 0

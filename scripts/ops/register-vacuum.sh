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
#   --sizes (default)  read-only: the two tables' sizes, pg_database_size, what
#                      is live (runs, register captures) and, per table,
#                      whether the owner connection owns it (PASS/FAIL)
#   --apply --confirm VACUUM
#                      refuses unless the owner connection owns both tables;
#                      then, BEFORE EACH TABLE, refuses while any run or
#                      register capture is not terminal and while
#                      pg_database_size + the table's size x 1.1 > 450 MB (the
#                      rewrite copies the table first), then VACUUM (FULL,
#                      ANALYZE) of that table; the sizes after
#
# Reads run as the release read-only role ($MILO_READONLY_DB_URL). The rewrite
# runs as the tables' OWNER, with the migrations' own credential (the
# `production` environment's SUPABASE_DB_PASSWORD and SUPABASE_PROJECT_ID, as
# deploy-supabase-migrations.yml): the read-only role holds no MAINTAIN. The
# owner connection takes the read-only URL's host -- a Supabase pooler in
# SESSION mode (port 5432, user postgres.<project ref>), or the direct host
# (user postgres) -- and the password through PGPASSWORD only: it is never on
# a command line and never printed. VACUUM FULL holds ACCESS EXCLUSIVE on the
# table while it rewrites it; it waits at most 5 s for the lock (lock_timeout,
# set in the owner's session: a session-mode or direct connection keeps it).
# It cannot run inside a transaction, so never through an RPC.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/ops/common.sh
source "${SCRIPT_DIR}/common.sh"

MODE="sizes" CONFIRM=""
TABLES=(catalog_raw_records catalog_candidate_variants)
#: The rewrite needs room for a copy of the table: refused above this (bytes).
HEADROOM_LIMIT_BYTES=450000000
usage() {
  cat << 'EOF'
Usage: register-vacuum.sh [--sizes | --apply --confirm VACUUM] [--dry-run] [--operator-config <path>]

--sizes (default) is read-only: sizes, what is live, and the owner read-back.
--apply rewrites catalog_raw_records and catalog_candidate_variants (VACUUM
(FULL, ANALYZE)) as their owner, each refused while any run or register
capture is live or without the headroom for its copy.
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
summary_header "Register space reclamation: ${MODE} (VACUUM (FULL, ANALYZE) of ${TABLES[*]}, as their owner)"

SIZES_SQL="select 'SIZE ' || c.relname || ' total=' || pg_total_relation_size(c.oid) || ' heap=' || pg_relation_size(c.oid)
       || ' toast=' || coalesce(pg_total_relation_size(nullif(c.reltoastrelid, 0)), 0)
  from pg_class c
 where c.oid in ('public.catalog_raw_records'::regclass, 'public.catalog_candidate_variants'::regclass)
union all
select 'DATABASE bytes=' || pg_database_size(current_database())
union all
select 'LIVE runs=' || (b->>'live_runs') || ' register_captures=' || (b->>'live_register_groups')
  from public.catalog_register_maintenance_blockers() b;"
# The owner may rewrite a table (VACUUM FULL needs ownership or MAINTAIN).
OWNER_SQL="select 'OWNER ' || c.relname || '='
       || case when pg_has_role(current_user, c.relowner, 'MEMBER') then 'PASS' else 'FAIL' end
  from pg_class c
 where c.oid in ('public.catalog_raw_records'::regclass, 'public.catalog_candidate_variants'::regclass);"
gate_sql() {
  printf "select 'GATE runs=' || (b->>'live_runs') || ' register_captures=' || (b->>'live_register_groups')
       || ' database=' || pg_database_size(current_database())
       || ' table=' || pg_total_relation_size('public.%s'::regclass)
  from public.catalog_register_maintenance_blockers() b;" "$1"
}

db_url() {
  local url="${!DB_URL_ENV:-}"
  [[ -n "$url" ]] || { printf 'FAIL: %s is not set: the read-only database URL is required\n' "$DB_URL_ENV" >&2; return 1; }
  printf '%s' "$url"
}
read_only() {
  local -a pg_env=()
  local pair
  db_url > /dev/null || return 1
  # The URL carries the read-only password: it is split here, from the
  # environment, into libpq's own variables -- never on psql's argv (where any
  # process listing shows it) -- and exported by the shell (a builtin, no
  # argv), the password as PGPASSWORD. Every URL parameter libpq would honour
  # maps to its variable; one this cannot map (or a multi-host URL) refuses
  # rather than connecting differently from `psql "$url"`. A user or database
  # the URL leaves out stays libpq's default.
  mapfile -t pg_env < <(python3 - "$DB_URL_ENV" << 'PY'
import os, sys
from urllib.parse import parse_qs, unquote, urlsplit
url = urlsplit(os.environ[sys.argv[1]])
if url.scheme not in ("postgres", "postgresql") or not url.hostname or "," in url.netloc:
    sys.exit(1)
names = {"sslmode": "PGSSLMODE", "sslrootcert": "PGSSLROOTCERT", "sslcert": "PGSSLCERT", "sslkey": "PGSSLKEY",
         "connect_timeout": "PGCONNECT_TIMEOUT", "application_name": "PGAPPNAME", "options": "PGOPTIONS",
         "target_session_attrs": "PGTARGETSESSIONATTRS", "channel_binding": "PGCHANNELBINDING"}
env = {"PGHOST": url.hostname}
if url.port:
    env["PGPORT"] = str(url.port)
if url.username:
    env["PGUSER"] = unquote(url.username)
if url.password:
    env["PGPASSWORD"] = unquote(url.password)
if url.path.lstrip("/"):
    env["PGDATABASE"] = unquote(url.path.lstrip("/"))
for key, values in parse_qs(url.query, keep_blank_values=True).items():
    if key not in names or len(values) != 1:
        sys.exit(1)
    env[names[key]] = values[0]
if any("\n" in value or "\0" in value for value in env.values()):
    sys.exit(1)
for name, value in env.items():
    print(f"{name}={value}")
PY
  )
  [[ "${#pg_env[@]}" -gt 0 ]] || return 1
  (
    for pair in "${pg_env[@]}"; do export "${pair?}"; done
    psql -X -A -t -v ON_ERROR_STOP=1 -c "$1" 2> /dev/null
  )
}
# The owner's connection: the read-only URL's host, session mode on a Supabase
# pooler; "HOST PORT USER DATABASE" (no secret) or nonzero.
owner_target() {
  [[ -n "${SUPABASE_DB_PASSWORD:-}" && "${SUPABASE_PROJECT_ID:-}" =~ ^[a-z0-9]{20}$ ]] || return 1
  db_url > /dev/null || return 1
  # The URL (with its password) is read from the environment, never argv.
  python3 - "$DB_URL_ENV" "$SUPABASE_PROJECT_ID" << 'PY'
import os, sys
from urllib.parse import urlsplit
url, ref = urlsplit(os.environ[sys.argv[1]]), sys.argv[2]
host, database = url.hostname or "", (url.path or "/postgres").lstrip("/") or "postgres"
if host.endswith(".pooler.supabase.com"):
    print(host, 5432, f"postgres.{ref}", database)
elif host == f"db.{ref}.supabase.co":
    print(host, url.port or 5432, "postgres", database)
else:
    sys.exit(1)
PY
}
owner_psql() {
  local host port user database
  read -r host port user database <<< "$OWNER_TARGET"
  PGHOST="$host" PGPORT="$port" PGUSER="$user" PGDATABASE="$database" PGSSLMODE=require \
    PGPASSWORD="$SUPABASE_DB_PASSWORD" psql -X -A -t -v ON_ERROR_STOP=1 "$@"
}

if [[ "$DRY_RUN" -eq 1 ]]; then
  ops_run psql "\$${DB_URL_ENV}" -X -A -t -v ON_ERROR_STOP=1 -c "select ... sizes, pg_database_size, catalog_register_maintenance_blockers()"
  ops_run psql "(owner: \$SUPABASE_DB_PASSWORD via PGPASSWORD, session mode)" -X -A -t -c "select ... OWNER read-back"
  if [[ "$MODE" == "apply" ]]; then
    for table in "${TABLES[@]}"; do
      ops_run psql "\$${DB_URL_ENV}" -X -A -t -c "select ... live runs, captures, pg_database_size, ${table} size"
      ops_run psql "(owner)" -X -v ON_ERROR_STOP=1 -c "set lock_timeout = '5s'" -c "vacuum (full, analyze) public.${table}"
    done
  fi
  summary "register-vacuum ${MODE}" DRY-RUN "would read the sizes (read-only)$([[ "$MODE" == "apply" ]] && printf ' and rewrite %s as their owner' "${TABLES[*]}")"
  summary_note "DRY RUN: nothing was called and nothing was changed."
  exit 0
fi

before="$(read_only "$SIZES_SQL")" || ops_fail "the sizes could not be read (read-only role)" "register-vacuum sizes"
printf '%s\n' "$before"
database_before="$(sed -n 's/^DATABASE bytes=//p' <<< "$before")"
live="$(sed -n 's/^LIVE //p' <<< "$before")"
[[ "$database_before" =~ ^[0-9]+$ && "$live" =~ ^runs=([0-9]+)\ register_captures=([0-9]+)$ ]] \
  || ops_fail "the sizes carried no database size or live counts" "register-vacuum sizes"
summary "register-vacuum sizes" PASS "pg_database_size ${database_before} bytes; live runs ${BASH_REMATCH[1]}, register captures ${BASH_REMATCH[2]}"

owner_ok=1
if OWNER_TARGET="$(owner_target 2> /dev/null)" && owners="$(owner_psql -c "$OWNER_SQL" 2> /dev/null)"; then
  printf '%s\n' "$owners"
else
  owners=""
  summary "register-vacuum owner" FAIL "the owner connection could not be made (SUPABASE_DB_PASSWORD / SUPABASE_PROJECT_ID, a Supabase host in ${DB_URL_ENV})"
fi
for table in "${TABLES[@]}"; do
  if grep -qx "OWNER ${table}=PASS" <<< "$owners"; then
    summary "register-vacuum owner ${table}" PASS "the owner connection owns public.${table}"
  else
    owner_ok=0
    summary "register-vacuum owner ${table}" FAIL "the owner connection does not own public.${table}"
  fi
done

if [[ "$MODE" == "sizes" ]]; then
  summary_note "Read-only: nothing was changed. To rewrite the two tables, dispatch register-retention with operation=vacuum-full, mode=apply and confirm=VACUUM while nothing is live."
  exit 0
fi

if (( ! owner_ok )); then
  printf 'REFUSED CATALOG_VACUUM_NOT_PERMITTED: the owner connection does not own both tables; nothing was rewritten\n' >&2
  exit 1
fi
for table in "${TABLES[@]}"; do
  # Re-checked before EACH table: nothing live, and room for the copy.
  gate="$(read_only "$(gate_sql "$table")")" \
    || ops_fail "the gate before public.${table} could not be read; tables before it were rewritten" "register-vacuum apply"
  printf '%s\n' "$gate"
  [[ "$gate" =~ ^GATE\ runs=([0-9]+)\ register_captures=([0-9]+)\ database=([0-9]+)\ table=([0-9]+)$ ]] \
    || ops_fail "the gate before public.${table} is unreadable; tables before it were rewritten" "register-vacuum apply"
  runs="${BASH_REMATCH[1]}" captures="${BASH_REMATCH[2]}" database="${BASH_REMATCH[3]}" size="${BASH_REMATCH[4]}"
  if (( runs > 0 || captures > 0 )); then
    summary "register-vacuum apply" FAIL "CATALOG_VACUUM_BLOCKED before public.${table}: ${runs} live run(s), ${captures} live register capture(s); tables before it were rewritten"
    printf 'REFUSED CATALOG_VACUUM_BLOCKED: wait until no run and no register capture is live\n' >&2
    exit 1
  fi
  needed=$(( database + size * 11 / 10 ))
  if (( needed > HEADROOM_LIMIT_BYTES )); then
    summary "register-vacuum apply" FAIL "CATALOG_VACUUM_NO_HEADROOM before public.${table}: pg_database_size ${database} + ${size} x 1.1 = ${needed} > ${HEADROOM_LIMIT_BYTES} bytes; tables before it were rewritten"
    printf 'REFUSED CATALOG_VACUUM_NO_HEADROOM: the rewrite of public.%s needs %s bytes (limit %s)\n' "$table" "$needed" "$HEADROOM_LIMIT_BYTES" >&2
    exit 1
  fi
  summary "register-vacuum headroom ${table}" PASS "pg_database_size ${database} + ${size} x 1.1 = ${needed} <= ${HEADROOM_LIMIT_BYTES} bytes"
  if ! owner_psql -q -c "set lock_timeout = '5s'" -c "vacuum (full, analyze) public.${table}" > /dev/null 2>&1; then
    summary "register-vacuum apply" FAIL "VACUUM (FULL, ANALYZE) public.${table} did not complete (a lock not granted within 5 s, or a pooler that refuses the connection's lock_timeout); tables before it were rewritten"
    exit 1
  fi
  printf 'VACUUMED public.%s\n' "$table"
done
after="$(read_only "$SIZES_SQL")" || ops_fail "the sizes after could not be read" "register-vacuum apply"
printf '%s\n' "$after"
database_after="$(sed -n 's/^DATABASE bytes=//p' <<< "$after")"
summary "register-vacuum apply" PASS "pg_database_size ${database_before} -> ${database_after} bytes"
exit 0

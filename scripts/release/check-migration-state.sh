#!/usr/bin/env bash
# Safe migration-state inspection.
#
# Local mode (default, fully offline): validates every migration filename,
# rejects duplicate or malformed migration versions, computes stable content
# hashes, and prints the ordered migration plan.
#
# Remote mode (only with an explicit operator-supplied read-only
# connection): classifies the remote schema as one of
#   empty-schema | legacy-baseline | partially-migrated | fully-migrated
# or fails closed (drift / unrecognized) when a safe ordered state cannot be
# proven. Never applies a migration. Never prints or stores the database
# password.
#
# The authoritative comparison is the COMPLETE local migration set (3-digit
# and 14-digit timestamped alike) against the remote applied history in
# supabase_migrations.schema_migrations. Object markers are secondary
# evidence only: they can add a drift finding, never establish that a
# database is fully migrated. The comparison itself lives in the pure,
# unit-tested helper scripts/release/migration_state.py.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

MIGRATIONS_DIR="${REPO_ROOT}/supabase/migrations"
STATE_HELPER="${SCRIPT_DIR}/migration_state.py"

usage() {
  cat << 'EOF'
Usage: check-migration-state.sh [options]

Read-only. Never applies a migration. Never creates destructive
down-migrations.

Options:
  --database-url-env <NAME>  Name of an environment variable holding a
                             READ-ONLY PostgreSQL connection string for the
                             remote inspection. The URL itself is never
                             accepted on the command line and never printed.
  --json-output <path>       Write a machine-readable JSON report.
  --plan-output <path>       Write the ordered migration plan (paths and
                             hashes) as JSON.
  --help                     Show this help.

Without --database-url-env the script runs in fully offline local-only
mode and reports remote state as MANUAL.
EOF
}

JSON_OUTPUT=""
PLAN_OUTPUT=""
DB_URL_ENV=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --database-url-env) DB_URL_ENV="${2:?--database-url-env requires a variable name}"; shift 2 ;;
    --json-output) JSON_OUTPUT="${2:?--json-output requires a path}"; shift 2 ;;
    --plan-output) PLAN_OUTPUT="${2:?--plan-output requires a path}"; shift 2 ;;
    --help) usage; exit 0 ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; usage >&2; exit 64 ;;
  esac
done

# ---------------------------------------------------------------------------
# Local inspection.
# ---------------------------------------------------------------------------
if [[ ! -d "${MIGRATIONS_DIR}" ]]; then
  record_check BLOCKED "local:migrations-dir" "migrations directory not found: supabase/migrations"
  finish_checks "check-migration-state" "${JSON_OUTPUT}"
  exit 1
fi

mapfile -t files < <(cd "${MIGRATIONS_DIR}" && ls -1 -- *.sql 2> /dev/null | LC_ALL=C sort)
if [[ "${#files[@]}" -eq 0 ]]; then
  record_check BLOCKED "local:migrations" "no migration files found"
  finish_checks "check-migration-state" "${JSON_OUTPUT}"
  exit 1
fi
record_check PASS "local:count" "${#files[@]} migration files found"

# The complete ordered local set — 3-digit and 14-digit timestamped versions
# together. This is the ONLY local side of the comparison; a subset of object
# markers can never stand in for it. The helper fails closed on a malformed
# filename or a duplicate version anywhere in the directory.
if ! tool_available python3; then
  record_check BLOCKED "local:versions" "python3 is required to validate migration versions; a safe ordered state cannot be proven without it"
  finish_checks "check-migration-state" "${JSON_OUTPUT}"
  exit 1
fi
local_error=""
if ! local_set="$(python3 "${STATE_HELPER}" local --migrations-dir "${MIGRATIONS_DIR}" 2>&1)"; then
  local_error="${local_set}"
  record_check BLOCKED "local:versions" "${local_error}"
  finish_checks "check-migration-state" "${JSON_OUTPUT}"
  exit 1
fi
mapfile -t ordered_versions < <(printf '%s\n' "${local_set}" | awk -F'\t' 'NF{print $1}')
mapfile -t ordered_files < <(printf '%s\n' "${local_set}" | awk -F'\t' 'NF{print $2}')
record_check PASS "local:versions" "${#ordered_versions[@]} migration versions parsed, unique and strictly ordered (3-digit and timestamped)"

# Stable content hashes (sha256).
hash_tool=""
if tool_available sha256sum; then hash_tool="sha256sum"; elif tool_available shasum; then hash_tool="shasum -a 256"; fi
declare -A hashes=()
if [[ -n "${hash_tool}" ]]; then
  for f in "${files[@]}"; do
    hashes["${f}"]="$(${hash_tool} "${MIGRATIONS_DIR}/${f}" | awk '{print $1}')"
  done
  record_check PASS "local:hashes" "stable content hashes computed for all migrations"
else
  record_check MANUAL "local:hashes" "no sha256 tool available; content hashes must be computed manually"
fi

# Ordered migration plan.
printf '\nOrdered migration plan:\n'
for i in "${!ordered_files[@]}"; do
  printf '  %2d. %-14s %s %s\n' "$((i + 1))" "${ordered_versions[${i}]}" "${ordered_files[${i}]}" "${hashes[${ordered_files[${i}]}]:-}"
done

if [[ -n "${PLAN_OUTPUT}" ]]; then
  tmp="$(mktemp "${TMPDIR:-/tmp}/milo-plan.XXXXXX")"
  chmod 600 "${tmp}"
  {
    printf '{\n  "migrations": [\n'
    last=$(( ${#ordered_files[@]} - 1 ))
    for i in "${!ordered_files[@]}"; do
      printf '    {"order": %d, "version": "%s", "file": "%s", "sha256": "%s"}' \
        "$((i + 1))" "$(json_escape "${ordered_versions[${i}]}")" \
        "$(json_escape "${ordered_files[${i}]}")" "${hashes[${ordered_files[${i}]}]:-}"
      [[ "${i}" -lt "${last}" ]] && printf ','
      printf '\n'
    done
    printf '  ]\n}\n'
  } > "${tmp}"
  mv "${tmp}" "${PLAN_OUTPUT}"
  record_check PASS "local:plan" "ordered migration plan written to ${PLAN_OUTPUT}"
fi

# Static content safety checks (defer to scripts/check_migrations.py).
if [[ -f "${REPO_ROOT}/scripts/check_migrations.py" ]]; then
  if (cd "${REPO_ROOT}" && python3 scripts/check_migrations.py > /dev/null 2>&1); then
    record_check PASS "local:static-safety" "scripts/check_migrations.py passed (no destructive clauses, baseline reconciliation intact)"
  else
    record_check BLOCKED "local:static-safety" "scripts/check_migrations.py failed"
  fi
else
  record_check MANUAL "local:static-safety" "scripts/check_migrations.py not found; static migration safety must be verified manually"
fi

# ---------------------------------------------------------------------------
# Remote inspection (explicit read-only connection only).
# ---------------------------------------------------------------------------
# Every statement below is a SELECT against catalog views or the Supabase
# migration-history table. Nothing is created, altered, dropped or written.
LEGACY_BASELINE_TABLES=(conversations messages runs run_events)

if [[ -z "${DB_URL_ENV}" ]]; then
  record_check MANUAL "remote:state" "no --database-url-env supplied; remote migration state requires an operator-supplied read-only connection (offline local-only mode)"
else
  db_url="${!DB_URL_ENV:-}"
  if [[ -z "${db_url}" ]]; then
    record_check BLOCKED "remote:connection" "environment variable ${DB_URL_ENV} is empty; supply a read-only connection string in it"
  elif ! tool_available psql; then
    record_check MANUAL "remote:psql" "psql is unavailable; remote migration state must be inspected manually"
  else
    run_sql() {
      # -X: no psqlrc; -A -t: unaligned tuples only; SELECT statements only.
      psql -X -A -t -v ON_ERROR_STOP=1 "${db_url}" -c "$1" 2> /dev/null
    }
    if ! run_sql "select 1" > /dev/null; then
      record_check BLOCKED "remote:connection" "unable to connect with the connection provided in ${DB_URL_ENV} (connection string is never printed)"
    else
      record_check PASS "remote:connection" "read-only connection established via ${DB_URL_ENV}"

      milo_tmpdir_init
      observation="$(milo_tmpdir)/observation.tsv"
      : > "${observation}"

      # 1. Applied migration history — the authoritative applied side.
      history_exists="$(run_sql "select 1 from information_schema.tables where table_schema='supabase_migrations' and table_name='schema_migrations'")"
      if [[ -n "${history_exists}" ]]; then
        printf 'history_available\t1\n' >> "${observation}"
        while IFS= read -r version; do
          [[ -n "${version}" ]] || continue
          printf 'applied\t%s\n' "${version}" >> "${observation}"
        done < <(run_sql "select version from supabase_migrations.schema_migrations order by version")
      else
        printf 'history_available\t0\n' >> "${observation}"
      fi

      # 2. Public schema shape.
      table_count="$(run_sql "select count(*) from information_schema.tables where table_schema='public'" | tr -d '[:space:]')"
      printf 'public_table_count\t%s\n' "${table_count:-0}" >> "${observation}"
      for t in "${LEGACY_BASELINE_TABLES[@]}"; do
        if [[ -n "$(run_sql "select 1 from information_schema.tables where table_schema='public' and table_name='${t}'")" ]]; then
          printf 'legacy_baseline\t%s\n' "${t}" >> "${observation}"
        fi
      done

      # 3. Secondary object markers. These NEVER establish that a migration
      #    is applied; they only expose a history row whose object is absent.
      while IFS=$'\t' read -r mversion mkind mobj; do
        [[ -n "${mversion}" ]] || continue
        case "${mkind}" in
          table) q="select 1 from information_schema.tables where table_schema='public' and table_name='${mobj}'" ;;
          view) q="select 1 from information_schema.views where table_schema='public' and table_name='${mobj}'" ;;
          column) q="select 1 from information_schema.columns where table_schema='public' and table_name='${mobj%%.*}' and column_name='${mobj##*.}'" ;;
          function) q="select 1 from pg_proc p join pg_namespace n on n.oid=p.pronamespace where n.nspname='public' and p.proname='${mobj}'" ;;
          *) continue ;;
        esac
        if [[ -n "$(run_sql "${q}")" ]]; then
          printf 'marker\t%s\t1\n' "${mversion}" >> "${observation}"
        else
          printf 'marker\t%s\t0\n' "${mversion}" >> "${observation}"
        fi
      done < <(python3 "${STATE_HELPER}" markers --migrations-dir "${MIGRATIONS_DIR}")

      # 4. Classify. The helper owns every decision.
      if ! classification="$(python3 "${STATE_HELPER}" classify --migrations-dir "${MIGRATIONS_DIR}" --observation "${observation}" 2>&1)"; then
        record_check BLOCKED "remote:state" "migration-state comparison failed: ${classification}"
      else
        state="$(json_field "${classification}" "state")"
        blocked="$(json_field "${classification}" "blocked")"
        applied_count="$(json_field "${classification}" "applied_count")"
        local_total="$(json_field "${classification}" "local_total")"
        summary="$(json_field "${classification}" "summary")"

        if [[ "${blocked}" == "true" ]]; then
          record_check BLOCKED "remote:state" "remote schema classified as ${state}: ${summary}"
        else
          record_check PASS "remote:state" "remote schema classified as ${state} (${applied_count}/${local_total} local migrations applied): ${summary}"
        fi

        # Report EVERY pending migration, timestamped ones included.
        mapfile -t pending < <(
          MILO_CLASSIFICATION="${classification}" python3 -c 'import json, os
report = json.loads(os.environ["MILO_CLASSIFICATION"])
for entry in report.get("missing", []):
    print(entry["version"], entry["file"])
'
        )
        if [[ "${#pending[@]}" -gt 0 ]]; then
          record_check WARN "remote:missing" "${#pending[@]} local migration(s) not present in remote migration history: ${pending[*]}"
        elif [[ "${state}" == "fully-migrated" ]]; then
          record_check PASS "remote:missing" "no local migration is missing from remote migration history"
        fi

        mapfile -t unexpected < <(
          MILO_CLASSIFICATION="${classification}" python3 -c 'import json, os
report = json.loads(os.environ["MILO_CLASSIFICATION"])
for version in report.get("unexpected", []):
    print(version)
'
        )
        if [[ "${#unexpected[@]}" -gt 0 ]]; then
          record_check BLOCKED "remote:unexpected-version" "remote migration history records versions with no local migration file: ${unexpected[*]}"
        fi

        mapfile -t disagreements < <(
          MILO_CLASSIFICATION="${classification}" python3 -c 'import json, os
report = json.loads(os.environ["MILO_CLASSIFICATION"])
for finding in report.get("marker_disagreements", []):
    print(finding)
'
        )
        if [[ "${#disagreements[@]}" -gt 0 ]]; then
          record_check BLOCKED "remote:history-object-disagreement" "${disagreements[*]}"
        fi
      fi

      # Remote objects that no local migration creates (advisory only).
      unexpected_tables="$(run_sql "select string_agg(table_name, ',') from information_schema.tables where table_schema='public' and table_name like 'milo_%'")"
      if [[ -n "${unexpected_tables}" ]]; then
        record_check WARN "remote:unexpected" "remote tables with no matching local migration: ${unexpected_tables}"
      fi
    fi
  fi
fi

printf '\nReminder: this tool NEVER applies migrations. Apply manually per docs/production-readiness/MIGRATIONS.md.\n'
finish_checks "check-migration-state" "${JSON_OUTPUT}"

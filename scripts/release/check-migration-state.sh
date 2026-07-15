#!/usr/bin/env bash
# Safe migration-state inspection.
#
# Local mode (default, fully offline): validates migration file naming
# order, duplicate numbers, and stable content hashes, and prints the
# ordered migration plan.
#
# Remote mode (only with an explicit operator-supplied read-only
# connection): classifies the remote schema as one of
#   empty-schema | legacy-baseline | partially-migrated | fully-migrated
# and reports missing/unexpected migrations. Never applies a migration.
# Never prints or stores the database password.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

MIGRATIONS_DIR="${REPO_ROOT}/supabase/migrations"

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

# Naming order and duplicate detection for NNN_*.sql files. Timestamped
# files (e.g. 20260706192500_*.sql) sort after numeric ones and are allowed.
declare -A seen_numbers=()
prev_number=-1
order_ok=1
numbered=()
for f in "${files[@]}"; do
  if [[ "${f}" =~ ^([0-9]{3})_ ]]; then
    num="${BASH_REMATCH[1]}"
    numbered+=("${f}")
    if [[ -n "${seen_numbers[${num}]:-}" ]]; then
      record_check BLOCKED "local:duplicate" "duplicate migration number ${num}: ${seen_numbers[${num}]} and ${f}"
      order_ok=0
    fi
    seen_numbers["${num}"]="${f}"
    if (( 10#${num} <= prev_number )); then
      record_check BLOCKED "local:order" "migration numbering is not strictly increasing at ${f}"
      order_ok=0
    fi
    prev_number=$((10#${num}))
  elif [[ ! "${f}" =~ ^[0-9]{14}_ ]]; then
    record_check WARN "local:naming" "unrecognized migration filename pattern: ${f}"
  fi
done
if [[ "${order_ok}" -eq 1 ]]; then
  record_check PASS "local:order" "numeric migrations are strictly increasing with no duplicates"
fi

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
idx=1
for f in "${files[@]}"; do
  printf '  %2d. %s %s\n' "${idx}" "${f}" "${hashes[${f}]:-}"
  idx=$((idx + 1))
done

if [[ -n "${PLAN_OUTPUT}" ]]; then
  tmp="$(mktemp "${TMPDIR:-/tmp}/milo-plan.XXXXXX")"
  chmod 600 "${tmp}"
  {
    printf '{\n  "migrations": [\n'
    last=$(( ${#files[@]} - 1 ))
    for i in "${!files[@]}"; do
      printf '    {"order": %d, "file": "%s", "sha256": "%s"}' \
        "$((i + 1))" "$(json_escape "${files[${i}]}")" "${hashes[${files[${i}]}]:-}"
      [[ "${i}" -lt "${last}" ]] && printf ','
      printf '\n'
    done
    printf '  ]\n}\n'
  } > "${tmp}"
  mv "${tmp}" "${PLAN_OUTPUT}"
  record_check PASS "local:plan" "ordered migration plan written to ${PLAN_OUTPUT}"
fi

# Static content safety checks (defer to scripts/check_migrations.py).
if tool_available python3 && [[ -f "${REPO_ROOT}/scripts/check_migrations.py" ]]; then
  if (cd "${REPO_ROOT}" && python3 scripts/check_migrations.py > /dev/null 2>&1); then
    record_check PASS "local:static-safety" "scripts/check_migrations.py passed (no destructive clauses, baseline reconciliation intact)"
  else
    record_check BLOCKED "local:static-safety" "scripts/check_migrations.py failed"
  fi
else
  record_check MANUAL "local:static-safety" "python3 unavailable; run scripts/check_migrations.py manually"
fi

# ---------------------------------------------------------------------------
# Remote inspection (explicit read-only connection only).
# ---------------------------------------------------------------------------
# Marker objects created by each numeric migration; used to classify the
# remote state without a migration-history table.
MARKERS=(
  "001_project_workspace.sql|table|projects"
  "002_durable_runtime.sql|table|run_checkpoints"
  "003_workflow_proposals.sql|table|workflow_proposals"
  "004_supervisor_shadow_mode.sql|table|supervisor_decisions"
  "005_internet_governance.sql|table|tool_access_requests"
  "006_deployment_hardening.sql|view|stuck_runs"
  "007_project_members.sql|table|project_members"
  "008_workflow_proposal_ownership.sql|column|workflow_proposals.project_id"
  "009_run_idempotency_lifecycle.sql|column|runs.launch_state"
  "010_run_usage.sql|column|runs.usage"
  "011_proposal_ownership_protection.sql|function|create_project_from_proposal_with_owner"
  "012_atomic_run_operations.sql|function|claim_run_lease"
  "013_usage_ledger.sql|table|run_usage_ledger"
  "014_atomic_daily_budget_reservations.sql|function|reserve_daily_user_budget"
  "015_atomic_model_call_budget_lifecycle.sql|table|model_call_budget_reservations"
)
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
      # -X: no psqlrc; -A -t: unaligned tuples only; read-only queries only.
      psql -X -A -t -v ON_ERROR_STOP=1 "${db_url}" -c "$1" 2> /dev/null
    }
    if ! run_sql "select 1" > /dev/null; then
      record_check BLOCKED "remote:connection" "unable to connect with the connection provided in ${DB_URL_ENV} (connection string is never printed)"
    else
      record_check PASS "remote:connection" "read-only connection established via ${DB_URL_ENV}"
      applied=()
      missing=()
      for marker in "${MARKERS[@]}"; do
        IFS='|' read -r mig kind obj <<< "${marker}"
        case "${kind}" in
          table) q="select 1 from information_schema.tables where table_schema='public' and table_name='${obj}'" ;;
          view) q="select 1 from information_schema.views where table_schema='public' and table_name='${obj}'" ;;
          column) q="select 1 from information_schema.columns where table_schema='public' and table_name='${obj%%.*}' and column_name='${obj##*.}'" ;;
          function) q="select 1 from pg_proc p join pg_namespace n on n.oid=p.pronamespace where n.nspname='public' and p.proname='${obj}'" ;;
        esac
        if [[ -n "$(run_sql "${q}")" ]]; then
          applied+=("${mig}")
        else
          missing+=("${mig}")
        fi
      done

      baseline_present=0
      for t in "${LEGACY_BASELINE_TABLES[@]}"; do
        if [[ -n "$(run_sql "select 1 from information_schema.tables where table_schema='public' and table_name='${t}'")" ]]; then
          baseline_present=$((baseline_present + 1))
        fi
      done
      table_count="$(run_sql "select count(*) from information_schema.tables where table_schema='public'")"

      if [[ "${table_count}" == "0" ]]; then
        state="empty-schema"
      elif [[ "${#applied[@]}" -eq 0 && "${baseline_present}" -eq "${#LEGACY_BASELINE_TABLES[@]}" ]]; then
        state="legacy-baseline"
      elif [[ "${#missing[@]}" -eq 0 ]]; then
        state="fully-migrated"
      elif [[ "${#applied[@]}" -gt 0 ]]; then
        state="partially-migrated"
      else
        state="unrecognized"
      fi
      record_check PASS "remote:state" "remote schema classified as ${state} (${#applied[@]}/${#MARKERS[@]} migration markers present; legacy baseline tables present: ${baseline_present}/${#LEGACY_BASELINE_TABLES[@]})"
      if [[ "${state}" == "partially-migrated" ]]; then
        record_check WARN "remote:missing" "migrations without markers (pending or partially applied): ${missing[*]}"
      fi
      if [[ "${state}" == "unrecognized" ]]; then
        record_check BLOCKED "remote:state-unrecognized" "remote schema matches neither empty, legacy baseline, partial, nor fully migrated state; manual review required"
      fi
      # Unexpected remote objects that no local migration creates.
      unexpected="$(run_sql "select string_agg(table_name, ',') from information_schema.tables where table_schema='public' and table_name like 'milo_%'")"
      if [[ -n "${unexpected}" ]]; then
        record_check WARN "remote:unexpected" "remote tables with no matching local migration: ${unexpected}"
      fi
    fi
  fi
fi

printf '\nReminder: this tool NEVER applies migrations. Apply manually per docs/production-readiness/MIGRATIONS.md.\n'
finish_checks "check-migration-state" "${JSON_OUTPUT}"

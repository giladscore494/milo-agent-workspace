#!/usr/bin/env bash
# Membership backfill SQL generator (dry-run by default; never executes).
#
# Consumes an explicit operator-supplied mapping file of REAL project and
# user IDs and emits reviewable SQL for public.project_members, including
# transaction boundaries, expected row counts and validation queries.
# Rejects malformed or placeholder UUIDs, duplicate rows, duplicate owners
# and projects without an owner. Never invents an identity and never
# deletes data (rollback is by forward correction).

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

usage() {
  cat << 'EOF'
Usage: generate-membership-backfill.sh --input <mapping.json> --output <plan.sql> [options]

Generates SQL only. NEVER connects to a database and NEVER applies anything.

Input format (see scripts/release/templates/membership-backfill.example.json):
  {"memberships": [{"project_id": "<uuid>", "user_id": "<uuid>", "role": "owner|admin|member|viewer"}]}

Options:
  --input <path>        Operator-supplied mapping file (required).
  --output <path>       Where to write the generated SQL plan (required).
  --json-output <path>  Write a machine-readable JSON report.
  --help                Show this help.
EOF
}

JSON_OUTPUT="" INPUT="" OUTPUT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --input) INPUT="${2:?}"; shift 2 ;;
    --output) OUTPUT="${2:?}"; shift 2 ;;
    --json-output) JSON_OUTPUT="${2:?}"; shift 2 ;;
    --help) usage; exit 0 ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; usage >&2; exit 64 ;;
  esac
done

if [[ -z "${INPUT}" || -z "${OUTPUT}" ]]; then
  printf -- '--input and --output are required\n' >&2
  usage >&2
  exit 64
fi
if [[ ! -f "${INPUT}" ]]; then
  record_check BLOCKED "input" "mapping file not found: ${INPUT}"
  finish_checks "generate-membership-backfill" "${JSON_OUTPUT}"
  exit $?
fi
if ! require_tool python3 "JSON validation"; then
  finish_checks "generate-membership-backfill" "${JSON_OUTPUT}"
  exit $?
fi

tmp_sql="$(mktemp "${TMPDIR:-/tmp}/milo-membership.XXXXXX")"
chmod 600 "${tmp_sql}"
trap 'rm -f "${tmp_sql}"' EXIT

set +e
python3 - "${INPUT}" "${tmp_sql}" << 'PYEOF'
import json, re, sys

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
PLACEHOLDER_UUID = "00000000-0000-0000-0000-000000000000"
ROLES = {"owner", "admin", "member", "viewer"}

path, out_path = sys.argv[1], sys.argv[2]
try:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
except (json.JSONDecodeError, OSError) as exc:
    sys.exit(f"BLOCKED input file is not valid JSON: {exc}")

rows = data.get("memberships")
if not isinstance(rows, list) or not rows:
    sys.exit("BLOCKED input must contain a non-empty 'memberships' list")

errors = []
seen = set()
owners = {}
projects = set()
for i, row in enumerate(rows):
    if not isinstance(row, dict):
        errors.append(f"row {i}: not an object")
        continue
    pid = str(row.get("project_id", "")).strip()
    uid = str(row.get("user_id", "")).strip()
    role = str(row.get("role", "")).strip()
    for label, value in (("project_id", pid), ("user_id", uid)):
        if not UUID_RE.match(value):
            errors.append(f"row {i}: malformed {label}: {value!r}")
        elif value.lower() == PLACEHOLDER_UUID or "<" in value:
            errors.append(f"row {i}: placeholder {label} rejected; supply the real identifier")
    if role not in ROLES:
        errors.append(f"row {i}: invalid role {role!r} (allowed: {sorted(ROLES)})")
    key = (pid.lower(), uid.lower())
    if key in seen:
        errors.append(f"row {i}: duplicate membership row for project {pid} / user {uid}")
    seen.add(key)
    projects.add(pid.lower())
    if role == "owner":
        owners.setdefault(pid.lower(), []).append(uid.lower())

for pid, owner_list in owners.items():
    if len(owner_list) > 1:
        errors.append(f"project {pid}: duplicate ownership rows ({len(owner_list)} owners)")
for pid in sorted(projects):
    if pid not in owners:
        errors.append(f"project {pid}: no owner row supplied; every project needs exactly one owner")

if errors:
    sys.exit("BLOCKED validation failed:\n  " + "\n  ".join(errors))

lines = [
    "-- MILO membership backfill plan (generated; review before manual apply)",
    "-- Forward-only: inserts/updates only, never deletes.",
    "-- Rollback is by forward correction: generate and apply a corrected plan;",
    "-- do NOT delete membership rows to undo this one.",
    f"-- Expected affected rows: {len(rows)}",
    "begin;",
]
for row in rows:
    pid = row["project_id"].strip()
    uid = row["user_id"].strip()
    role = row["role"].strip()
    lines.append(
        "insert into public.project_members (project_id, user_id, role)\n"
        f"  values ('{pid}'::uuid, '{uid}'::uuid, '{role}')\n"
        "  on conflict (project_id, user_id) do update set role = excluded.role;"
    )
lines += [
    "commit;",
    "",
    "-- Validation queries (run after apply):",
    f"-- expect {len(projects)} project(s) each with exactly one owner:",
    "select project_id, count(*) filter (where role = 'owner') as owner_count",
    "  from public.project_members group by project_id having count(*) filter (where role = 'owner') <> 1;",
    "-- expect zero rows above; then confirm the total:",
    f"select count(*) as membership_rows from public.project_members;  -- expect >= {len(rows)}",
]
with open(out_path, "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines) + "\n")
print(f"OK rows={len(rows)} projects={len(projects)}")
PYEOF
py_status=$?
set -e

if [[ "${py_status}" -ne 0 ]]; then
  record_check BLOCKED "validation" "input mapping rejected (see stderr above); no SQL was generated"
  finish_checks "generate-membership-backfill" "${JSON_OUTPUT}"
  exit $?
fi

mv "${tmp_sql}" "${OUTPUT}"
trap - EXIT
record_check PASS "generate" "membership backfill SQL written to ${OUTPUT} (dry-run only; apply manually per docs/production-readiness/MIGRATIONS.md)"
record_check MANUAL "apply" "review the SQL, verify row counts, then apply manually inside a transaction with a verified backup; this script never executes SQL"
finish_checks "generate-membership-backfill" "${JSON_OUTPUT}"

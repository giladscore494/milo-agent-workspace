#!/usr/bin/env bash
# Workflow-proposal ownership backfill SQL generator (dry-run by default).
#
# Consumes explicit proposal-to-user/project mappings and emits reviewable,
# deterministic, forward-only SQL for public.workflow_proposals ownership
# columns (created_by, project_id). Rejects orphan proposals, conflicting
# owners and malformed UUIDs. Never executes anything.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

usage() {
  cat << 'EOF'
Usage: generate-proposal-backfill.sh --input <mapping.json> --output <plan.sql> [options]

Generates SQL only. NEVER connects to a database and NEVER applies anything.

Input format (see scripts/release/templates/proposal-backfill.example.json):
  {"proposals": [{"proposal_id": "<uuid>", "created_by": "<uuid>", "project_id": "<uuid>"}]}

Every proposal must map to exactly one owner and one project (no orphans,
no conflicts). Updates only rows whose ownership is still NULL, preserving
any ownership already recorded.

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
  finish_checks "generate-proposal-backfill" "${JSON_OUTPUT}"
  exit $?
fi
if ! require_tool python3 "JSON validation"; then
  finish_checks "generate-proposal-backfill" "${JSON_OUTPUT}"
  exit $?
fi

tmp_sql="$(mktemp "${TMPDIR:-/tmp}/milo-proposal.XXXXXX")"
chmod 600 "${tmp_sql}"
trap 'rm -f "${tmp_sql}"' EXIT

set +e
python3 - "${INPUT}" "${tmp_sql}" << 'PYEOF'
import json, re, sys

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
PLACEHOLDER_UUID = "00000000-0000-0000-0000-000000000000"

path, out_path = sys.argv[1], sys.argv[2]
try:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
except (json.JSONDecodeError, OSError) as exc:
    sys.exit(f"BLOCKED input file is not valid JSON: {exc}")

rows = data.get("proposals")
if not isinstance(rows, list) or not rows:
    sys.exit("BLOCKED input must contain a non-empty 'proposals' list")

errors = []
mapping = {}
for i, row in enumerate(rows):
    if not isinstance(row, dict):
        errors.append(f"row {i}: not an object")
        continue
    prop = str(row.get("proposal_id", "")).strip()
    user = str(row.get("created_by", "")).strip()
    project = str(row.get("project_id", "")).strip()
    for label, value in (("proposal_id", prop), ("created_by", user), ("project_id", project)):
        if not UUID_RE.match(value):
            errors.append(f"row {i}: malformed or missing {label}: {value!r} (orphan proposals are rejected)")
        elif value.lower() == PLACEHOLDER_UUID or "<" in value:
            errors.append(f"row {i}: placeholder {label} rejected; supply the real identifier")
    key = prop.lower()
    if key in mapping and mapping[key] != (user.lower(), project.lower()):
        errors.append(f"row {i}: conflicting owners for proposal {prop}")
    elif key in mapping:
        errors.append(f"row {i}: duplicate mapping for proposal {prop}")
    mapping[key] = (user.lower(), project.lower())

if errors:
    sys.exit("BLOCKED validation failed:\n  " + "\n  ".join(errors))

# Deterministic output ordering.
ordered = sorted((prop, owner, project) for prop, (owner, project) in mapping.items())
lines = [
    "-- MILO workflow-proposal ownership backfill plan (generated; review before manual apply)",
    "-- Forward-only corrective behavior: only rows with NULL ownership are updated;",
    "-- existing ownership is never overwritten and nothing is deleted.",
    f"-- Expected affected rows: at most {len(ordered)}",
    "begin;",
]
for prop, owner, project in ordered:
    lines.append(
        "update public.workflow_proposals\n"
        f"   set created_by = '{owner}'::uuid,\n"
        f"       project_id = '{project}'::uuid\n"
        f" where id = '{prop}'::uuid\n"
        "   and created_by is null\n"
        "   and project_id is null;"
    )
lines += [
    "commit;",
    "",
    "-- Validation queries (run after apply):",
    "-- expect zero unowned proposals among the mapped set:",
    "select count(*) as unowned from public.workflow_proposals where created_by is null or project_id is null;",
    f"-- expect the mapped proposals to be owned ({len(ordered)} ids):",
    "select id, created_by, project_id from public.workflow_proposals where id in (",
    ",\n".join(f"  '{prop}'::uuid" for prop, _, _ in ordered),
    ");",
]
with open(out_path, "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines) + "\n")
print(f"OK proposals={len(ordered)}")
PYEOF
py_status=$?
set -e

if [[ "${py_status}" -ne 0 ]]; then
  record_check BLOCKED "validation" "input mapping rejected (see stderr above); no SQL was generated"
  finish_checks "generate-proposal-backfill" "${JSON_OUTPUT}"
  exit $?
fi

mv "${tmp_sql}" "${OUTPUT}"
trap - EXIT
record_check PASS "generate" "proposal ownership backfill SQL written to ${OUTPUT} (dry-run only)"
record_check MANUAL "apply" "review the SQL and expected counts, then apply manually; this script never executes SQL"
finish_checks "generate-proposal-backfill" "${JSON_OUTPUT}"

#!/usr/bin/env bash
# ONE-TIME, in Cloud Shell (PR-D1): the register archive bucket.
#
#   --plan   (default) read-only: prints every change --apply would make, each
#            marked CREATE / UPDATE / BIND / OK (already in place).
#   --apply  makes exactly those changes. Idempotent: every step describes
#            first and changes only what is missing, so a re-run is a no-op.
#   --check  read-only, one machine line for the deploy preflight:
#              PASS <detail>        the bucket and the grant are in place
#              PARTIAL <detail>     the bucket's own posture is verified
#                                   (exists, us-central1, uniform access,
#                                   public access prevention enforced) but
#                                   this identity cannot read IAM -- the
#                                   deployer's case: it holds no IAM read on
#                                   the bucket or the project, by design.
#                                   The operator verifies the IAM half from
#                                   Cloud Shell (this --check, run there).
#              GAP <detail>         not set up yet (register capture refuses)
#              FAIL <detail>        a posture violation (public access, a
#                                   delete-capable role for an app identity)
#              UNREADABLE <detail>  this identity cannot read the bucket
#
# What it sets up:
#   1. the bucket REGISTER_ARCHIVE_BUCKET in us-central1, with uniform
#      bucket-level access and public access prevention ENFORCED;
#   2. on THAT bucket only, for the capture job's identity
#      (CAPTURE_SERVICE_ACCOUNT, or WORKER_SERVICE_ACCOUNT when the
#      configuration names no separate capture identity -- exactly what
#      government-production-capture.sh runs the job as):
#        roles/storage.objectCreator  create an object (never overwrite: the
#                                     job writes with ifGenerationMatch=0)
#        roles/storage.objectViewer   read an object's metadata back, so an
#                                     upload whose answer was lost (or a crash
#                                     before the database record) can be
#                                     verified by its recorded sha256 rather
#                                     than wedge the snapshot
#      Neither can delete or overwrite anything.
# No identity of the application (API, worker, capture) is given, or may hold,
# any delete-capable role on the bucket or the project: --check reports one as
# FAIL. Prune (scripts/ops/register-retention.sh) deletes database rows only,
# never an object. It creates no key, reads no object content, and prints no
# secret and no account address (identities are named by their config key).

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

MODE="plan"
REGISTER_ARCHIVE_LOCATION="us-central1"
REGISTER_ARCHIVE_ROLES=("roles/storage.objectCreator" "roles/storage.objectViewer")
# Roles on the bucket that can delete or overwrite an object. None may be held
# by an application identity.
DELETE_CAPABLE_ROLES=(roles/storage.admin roles/storage.objectAdmin roles/storage.objectUser
                      roles/storage.legacyBucketOwner roles/storage.legacyBucketWriter
                      roles/owner roles/editor)
# The same, held on the PROJECT, reaches every bucket.

usage() {
  cat << 'EOF'
Usage: setup-register-archive.sh [--plan | --apply | --check] [--operator-config <path>]

One-time Cloud Shell setup of the register archive bucket and the capture
identity's create-only grant on it. --plan (default) changes nothing.
EOF
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --plan) MODE="plan"; shift ;;
    --apply) MODE="apply"; shift ;;
    --check) MODE="check"; shift ;;
    --operator-config) MILO_OPERATOR_CONFIG_PATH="${2:?}"; shift 2 ;;
    --help) usage; exit 0 ;;
    *) printf 'FAIL: unknown argument %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done

ops_load_config
BUCKET="$(milo_op REGISTER_ARCHIVE_BUCKET)"
CAPTURE_SA="$(milo_op CAPTURE_SERVICE_ACCOUNT)" CAPTURE_KEY="CAPTURE_SERVICE_ACCOUNT"
[[ -n "$CAPTURE_SA" ]] || { CAPTURE_SA="$(milo_op WORKER_SERVICE_ACCOUNT)"; CAPTURE_KEY="WORKER_SERVICE_ACCOUNT"; }
CAPTURE_LABEL="the capture identity (${CAPTURE_KEY})"
APP_IDENTITIES=()
for key in API_SERVICE_ACCOUNT WORKER_SERVICE_ACCOUNT CAPTURE_SERVICE_ACCOUNT; do
  value="$(milo_op "$key")"
  [[ -z "$value" ]] || APP_IDENTITIES+=("serviceAccount:${value}")
done

if [[ -z "$BUCKET" ]]; then
  if [[ "$MODE" == "check" ]]; then
    printf 'GAP REGISTER_ARCHIVE_BUCKET is not configured; register capture refuses (CATALOG_ARCHIVE_NOT_CONFIGURED)\n'
    exit 0
  fi
  printf 'FAIL: REGISTER_ARCHIVE_BUCKET is empty in %s. Choose a new, globally unique bucket name.\n' "$CONFIG_PATH" >&2
  exit 2
fi
# Bucket naming rules (lowercase letters, digits, - _ .; 3-63 characters).
if [[ ! "$BUCKET" =~ ^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$ ]]; then
  printf 'FAIL: REGISTER_ARCHIVE_BUCKET is not a valid bucket name\n' >&2
  exit 2
fi
if [[ -z "$CAPTURE_SA" ]]; then
  printf 'FAIL: neither CAPTURE_SERVICE_ACCOUNT nor WORKER_SERVICE_ACCOUNT is configured in %s.\n' "$CONFIG_PATH" >&2
  exit 2
fi
CAPTURE_MEMBER="serviceAccount:${CAPTURE_SA}"
URL="gs://${BUCKET}"

# The bucket's posture as three words: LOCATION UNIFORM PAP (lowercase).
POSTURE_PY='
import json, sys
doc = json.load(sys.stdin)
if isinstance(doc, list):
    doc = doc[0] if doc else {}
uniform = doc.get("uniform_bucket_level_access")
if isinstance(uniform, dict):
    uniform = uniform.get("enabled")
print(str(doc.get("location") or "").lower(), str(bool(uniform)).lower(),
      str(doc.get("public_access_prevention") or "inherited").lower())
'
# Policy facts: "HAS <role>" per archive role MEMBER holds; "DELETE <role>"
# per app identity holding a delete-capable role; "PUBLIC <role>" for
# allUsers / allAuthenticatedUsers. Members are compared, never printed.
POLICY_PY='
import json, sys
doc = json.load(sys.stdin)
member = sys.argv[1]
roles = set(sys.argv[2].split(","))
app = set(sys.argv[3].split(",")) if sys.argv[3] else set()
delete_roles = set(sys.argv[4].split(","))
out = []
for binding in doc.get("bindings") or []:
    members = set(binding.get("members") or [])
    if binding.get("role") in roles and member in members and not binding.get("condition"):
        out.append("HAS " + binding["role"])
    if binding.get("role") in delete_roles and members & app:
        out.append("DELETE " + binding["role"])
    if members & {"allUsers", "allAuthenticatedUsers"}:
        out.append("PUBLIC " + str(binding.get("role")))
print("\n".join(out))
'
roles_csv="$(IFS=,; printf '%s' "${REGISTER_ARCHIVE_ROLES[*]}")"
app_csv="$(IFS=,; printf '%s' "${APP_IDENTITIES[*]}")"
delete_csv="$(IFS=,; printf '%s' "${DELETE_CAPABLE_ROLES[*]}")"

bucket_json() { gcloud storage buckets describe "$URL" --project "$PROJECT_ID" --format=json 2> /dev/null; }
# policy_facts: the facts, or exit 3 when an IAM read was DENIED to this
# identity (the deployer's case) and 1 on any other failure. gcloud's stderr
# is only matched, never shown (it can quote an account address).
policy_facts() {
  local policy project err
  err="$(mktemp)"
  if ! policy="$(gcloud storage buckets get-iam-policy "$URL" --project "$PROJECT_ID" --format=json 2> "$err")" \
     || ! project="$(gcloud projects get-iam-policy "$PROJECT_ID" --format=json 2> "$err")"; then
    if grep -qE 'PERMISSION_DENIED|HTTPError 403|does not have [a-z.]*getIamPolicy' "$err"; then
      rm -f "$err"; return 3
    fi
    rm -f "$err"; return 1
  fi
  rm -f "$err"
  python3 -c "$POLICY_PY" "$CAPTURE_MEMBER" "$roles_csv" "$app_csv" "$delete_csv" <<< "$policy"
  python3 -c "$POLICY_PY" "" "" "$app_csv" "$delete_csv" <<< "$project" | sed -n 's/^DELETE /PROJECT_DELETE /p'
}
holds_all() {  # holds_all FACTS -- the member holds every archive role
  local role
  for role in "${REGISTER_ARCHIVE_ROLES[@]}"; do grep -qxF "HAS ${role}" <<< "$1" || return 1; done
}

# ---------------------------------------------------------------------------
# --check (the deploy preflight): read-only, one line.
# ---------------------------------------------------------------------------
if [[ "$MODE" == "check" ]]; then
  status=0
  json="$(bucket_json)" || status=$?
  if [[ "$status" -ne 0 ]]; then
    # Not found and not permitted look the same to a describe; neither is
    # proof the bucket is missing, so the preflight reports it as a gap.
    printf 'UNREADABLE the bucket %s could not be described by this identity (missing, or no storage.buckets.get)\n' "$BUCKET"
    exit 0
  fi
  read -r location uniform pap <<< "$(python3 -c "$POSTURE_PY" <<< "$json")"
  if [[ "$pap" != "enforced" ]]; then
    printf 'FAIL bucket %s is not closed to the public (public access prevention %s)\n' "$BUCKET" "$pap"
    exit 0
  fi
  facts_status=0
  facts="$(policy_facts)" || facts_status=$?
  if [[ "$facts_status" -ne 0 && "$facts_status" -ne 3 ]]; then
    printf 'UNREADABLE the IAM policy of %s or of the project could not be read\n' "$BUCKET"
    exit 0
  fi
  if [[ "$facts_status" -eq 3 ]]; then
    # IAM read DENIED (the deployer): what a describe proves is checked; the
    # IAM half is the operator's to verify (public access prevention already
    # rules out a public grant).
    if [[ "$uniform" != "true" || "$location" != "$REGISTER_ARCHIVE_LOCATION" ]]; then
      printf 'GAP bucket %s is not in %s with uniform access (location %s, uniform %s)\n' \
        "$BUCKET" "$REGISTER_ARCHIVE_LOCATION" "${location:-unknown}" "$uniform"
    else
      printf 'PARTIAL bucket %s: %s, uniform access, public access prevention enforced; its IAM (the capture grant, no delete-capable role) is not readable by this identity. Verify from Cloud Shell: bash scripts/ops/setup-register-archive.sh --check\n' \
        "$BUCKET" "$REGISTER_ARCHIVE_LOCATION"
    fi
    exit 0
  fi
  if grep -q '^PUBLIC ' <<< "$facts"; then
    printf 'FAIL bucket %s is not closed to the public (public access prevention %s)\n' "$BUCKET" "$pap"
  elif grep -qE '^(PROJECT_)?DELETE ' <<< "$facts"; then
    printf 'FAIL an application identity holds a delete-capable role on %s or the project (%s)\n' "$BUCKET" \
      "$(grep -E '^(PROJECT_)?DELETE ' <<< "$facts" | cut -d' ' -f2 | sort -u | paste -sd, -)"
  elif [[ "$uniform" != "true" || "$location" != "$REGISTER_ARCHIVE_LOCATION" ]]; then
    printf 'GAP bucket %s is not in %s with uniform access (location %s, uniform %s)\n' \
      "$BUCKET" "$REGISTER_ARCHIVE_LOCATION" "${location:-unknown}" "$uniform"
  elif ! holds_all "$facts"; then
    printf 'GAP %s does not hold %s on %s\n' "$CAPTURE_LABEL" "${REGISTER_ARCHIVE_ROLES[*]}" "$BUCKET"
  else
    printf 'PASS bucket %s: %s, uniform access, public access prevention enforced; %s holds %s\n' \
      "$BUCKET" "$REGISTER_ARCHIVE_LOCATION" "$CAPTURE_LABEL" "${REGISTER_ARCHIVE_ROLES[*]}"
  fi
  exit 0
fi

# ---------------------------------------------------------------------------
# --plan / --apply
# ---------------------------------------------------------------------------
# Its own lines name the operator's account: not shown (no address is printed).
milo_require_gcloud_context "$PROJECT_ID" > /dev/null || exit 2
printf '== Register archive: %s (project %s) ==\n' "$URL" "$PROJECT_ID"

step() {  # step VERB DETAIL CMD... -- print, and run only in --apply
  local verb="$1" detail="$2"
  shift 2
  printf '%-7s %s\n' "$verb" "$detail"
  if [[ "$MODE" == "apply" ]]; then
    # gcloud's own output can quote an account address: never shown.
    "$@" > /dev/null 2>&1 || { printf 'FAIL: %s did not apply\n' "$detail" >&2; exit 1; }
  fi
}

if json="$(bucket_json)"; then
  read -r location uniform pap <<< "$(python3 -c "$POSTURE_PY" <<< "$json")"
  if [[ "$location" != "$REGISTER_ARCHIVE_LOCATION" ]]; then
    # A bucket's location is fixed at creation; nothing here moves data.
    printf 'FAIL: %s exists in %s, not %s. Choose another REGISTER_ARCHIVE_BUCKET.\n' \
      "$URL" "${location:-unknown}" "$REGISTER_ARCHIVE_LOCATION" >&2
    exit 1
  fi
  printf 'OK      bucket %s exists in %s\n' "$URL" "$REGISTER_ARCHIVE_LOCATION"
  if [[ "$uniform" == "true" && "$pap" == "enforced" ]]; then
    printf 'OK      uniform bucket-level access and public access prevention are on\n'
  else
    step UPDATE "uniform bucket-level access and public access prevention on ${URL}" \
      gcloud storage buckets update "$URL" --project "$PROJECT_ID" \
      --uniform-bucket-level-access --public-access-prevention
  fi
else
  step CREATE "bucket ${URL} in ${REGISTER_ARCHIVE_LOCATION}, uniform access, public access prevention" \
    gcloud storage buckets create "$URL" --project "$PROJECT_ID" --location "$REGISTER_ARCHIVE_LOCATION" \
    --uniform-bucket-level-access --public-access-prevention
fi

facts=""
if [[ "$MODE" == "apply" ]] || bucket_json > /dev/null; then
  facts="$(policy_facts)" || { printf 'FAIL: the IAM policy of %s could not be read\n' "$URL" >&2; exit 1; }
fi
for role in "${REGISTER_ARCHIVE_ROLES[@]}"; do
  if grep -qxF "HAS ${role}" <<< "$facts"; then
    printf 'OK      %s holds %s on %s\n' "$CAPTURE_LABEL" "$role" "$URL"
  else
    step BIND "${role} for ${CAPTURE_LABEL} on ${URL} only" \
      gcloud storage buckets add-iam-policy-binding "$URL" --project "$PROJECT_ID" \
      --member "$CAPTURE_MEMBER" --role "$role"
  fi
done
if grep -qE '^(PROJECT_)?DELETE ' <<< "$facts"; then
  # Never removed automatically: a binding someone else made is theirs to
  # explain. It is reported, and --check (the preflight) reports it as FAIL.
  printf 'FAIL: an application identity holds a delete-capable role on %s or the project (%s). Remove it\n' "$URL" \
    "$(grep -E '^(PROJECT_)?DELETE ' <<< "$facts" | cut -d' ' -f2 | sort -u | paste -sd, -)" >&2
  printf '      (gcloud storage buckets / projects remove-iam-policy-binding ... --member <identity> --role <role>).\n' >&2
  exit 1
fi

if [[ "$MODE" == "apply" ]]; then
  result="$(bash "$0" --check --operator-config "$CONFIG_PATH")"
  printf '%s\n' "$result"
  [[ "$result" == PASS\ * ]] || { printf 'FAIL: the archive did not read back as set up (above)\n' >&2; exit 1; }
  printf '\nNext: government-production-capture.sh --ensure-job (the job then carries MILO_REGISTER_ARCHIVE_BUCKET).\n'
else
  printf '\nPLAN ONLY -- nothing was changed. Re-run with --apply.\n'
fi

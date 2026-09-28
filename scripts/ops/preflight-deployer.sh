#!/usr/bin/env bash
# Preflight AS THE DEPLOYER (deploy.yml input preflight_as_deployer): run every
# read-only gcloud call the deploy makes, as the identity the workflow
# authenticated as (milo-github-deployer, through Workload Identity
# Federation), and report EVERY missing API, permission and resource at once
# -- not the first one, as a deploy that stops at it would.
#
# A deploy run reaches Cloud Build and stops at whatever the deployer lacks
# there; each fix costs another run. This answers all of it in one run:
#
#   * the read-only calls themselves (projects describe, services list,
#     service-accounts / repositories / images / secrets / Cloud Run
#     describes, IAM policy reads, executions list, the Cloud Build source
#     bucket list, builds list, logging read), each classified on failure as
#     a DISABLED API, a MISSING PERMISSION (named when gcloud names it) or a
#     MISSING RESOURCE;
#   * testIamPermissions probes -- read-only by definition -- for what no
#     read-only call can prove: cloudbuild.builds.create and the rest of the
#     project permissions the deploy uses, the async build path's own
#     (cloudbuild.builds.get to poll a build, logging.logEntries.list to read
#     a failed build's log, artifactregistry.dockerimages.get for its image), iam.serviceAccounts.actAs on the
#     build identity and each runtime identity, uploads to the build source
#     bucket, and that the deployer can NOT act as the Compute Engine default
#     service account.
#
# It never builds, deploys, binds, enables or changes anything, and reads no
# secret value (secrets are described, never accessed). The access token the
# probes use is handed to curl on stdin, never on a command line and never
# printed. --dry-run prints every call and makes none.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

SHA=""
usage() {
  cat << 'EOF'
Usage: preflight-deployer.sh --sha <40-hex> [--dry-run] [--operator-config <path>]

Runs every read-only gcloud call the deploy makes, plus testIamPermissions
probes, as the active identity, and reports every missing API, permission and
resource at once. Changes nothing. --dry-run calls nothing.
EOF
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sha) SHA="${2:?}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --operator-config) MILO_OPERATOR_CONFIG_PATH="${2:?}"; shift 2 ;;
    --help) usage; exit 0 ;;
    *) printf 'FAIL: unknown argument %s\n' "$1" >&2; exit 2 ;;
  esac
done
[[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || { printf 'FAIL: --sha must be a full 40-character lowercase SHA\n' >&2; exit 2; }

ops_load_config
milo_require_op ARTIFACT_REGISTRY_REPOSITORY API_SERVICE_ACCOUNT WORKER_SERVICE_ACCOUNT \
  CLOUD_BUILD_SERVICE_ACCOUNT || exit 2
BUILD_SA="$(milo_op CLOUD_BUILD_SERVICE_ACCOUNT)"
if ! problem="$(milo_build_service_account_problem "$BUILD_SA")"; then
  printf 'FAIL: %s\n' "$problem" >&2
  exit 2
fi
REPOSITORY="$(milo_op ARTIFACT_REGISTRY_REPOSITORY)"
CAPTURE_JOB="$(milo_op CLOUD_RUN_CAPTURE_JOB)"
BUILD_BUCKET_NAME="${PROJECT_ID}_cloudbuild"
API_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/${MILO_API_IMAGE_REPO}:${SHA}"
WORKER_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/${MILO_WORKER_IMAGE_REPO}:${SHA}"
RUNTIME_ACCOUNTS=("$(milo_op API_SERVICE_ACCOUNT)" "$(milo_op WORKER_SERVICE_ACCOUNT)")
[[ -n "$(milo_op CAPTURE_SERVICE_ACCOUNT)" ]] && RUNTIME_ACCOUNTS+=("$(milo_op CAPTURE_SERVICE_ACCOUNT)")

# The APIs the deploy reaches (scripts/ops/setup-wif.sh enables them).
REQUIRED_APIS=(cloudresourcemanager.googleapis.com iam.googleapis.com iamcredentials.googleapis.com
               sts.googleapis.com run.googleapis.com cloudbuild.googleapis.com
               artifactregistry.googleapis.com serviceusage.googleapis.com
               secretmanager.googleapis.com logging.googleapis.com)
# Project permissions the deploy exercises, including the mutations no
# read-only call can prove (probed with testIamPermissions, never used).
PROJECT_PERMISSIONS=(
  resourcemanager.projects.get serviceusage.services.list serviceusage.services.use
  cloudbuild.builds.create cloudbuild.builds.get cloudbuild.builds.list
  storage.buckets.list
  artifactregistry.repositories.get artifactregistry.dockerimages.get
  run.services.get run.services.update run.services.getIamPolicy
  run.jobs.get run.jobs.create run.jobs.update run.jobs.getIamPolicy run.jobs.setIamPolicy
  run.executions.list run.operations.get
  secretmanager.secrets.get secretmanager.secrets.getIamPolicy secretmanager.versions.list
  iam.serviceAccounts.get logging.logEntries.list
)

MISSING_APIS=() MISSING_PERMISSIONS=() MISSING_RESOURCES=() FORBIDDEN=() OTHER=()
add_unique() {
  # add_unique ARRAY_NAME VALUE
  local -n list="$1"
  local item
  for item in "${list[@]}"; do [[ "$item" == "$2" ]] && return 0; done
  list+=("$2")
}

# classify ERROR_FILE -> "API <name>" | "PERMISSION <name>" | "PERMISSION -" |
# "NOT_FOUND" | "OTHER <first line>". gcloud and Google APIs name the disabled
# service and, usually, the denied permission.
classify() {
  python3 - "$1" << 'PY'
import re, sys
text = open(sys.argv[1], encoding="utf-8", errors="replace").read()
flat = " ".join(text.split())
if re.search(r"SERVICE_DISABLED|has not been used in project|it is disabled|API \[[^\]]+\] not enabled", flat):
    for pattern in (r"service: ([a-z0-9-]+\.googleapis\.com)", r"/apis/api/([a-z0-9-]+\.googleapis\.com)",
                    r"API \[([a-z0-9-]+\.googleapis\.com)\]", r"\b((?!type\.)[a-z0-9-]+\.googleapis\.com)\b"):
        match = re.search(pattern, flat)
        if match:
            print("API " + match.group(1)); sys.exit(0)
    print("API -"); sys.exit(0)
if re.search(r"PERMISSION_DENIED|does not have|[Pp]ermission .* denied|\b403\b|forbidden", flat):
    # The explicit forms first ("Permission 'x.y.z' denied", "does not have
    # x.y.z access"), then any x.y.z that is neither a host nor gcloud's own
    # "(gcloud.group.command)" prefix.
    named = re.search(r"[Pp]ermission ['\"]?([a-z][a-zA-Z]*(?:\.[a-zA-Z]+){2,})['\"]? denied", flat) \
        or re.search(r"does not have ([a-z][a-zA-Z]*(?:\.[a-zA-Z]+){2,}) access", flat)
    if named:
        print("PERMISSION " + named.group(1)); sys.exit(0)
    for candidate in re.findall(r"\b([a-z][a-zA-Z]*\.[a-z][a-zA-Z]*\.[a-z][a-zA-Z]*)\b", flat):
        parts = candidate.split(".")
        if parts[0] == "gcloud" or parts[-1] in ("com", "net", "org", "io", "dev", "app") or parts[1] in (
                "googleapis", "gserviceaccount", "google"):
            continue
        print("PERMISSION " + candidate); sys.exit(0)
    print("PERMISSION -"); sys.exit(0)
if re.search(r"NOT_FOUND|not found|[Nn]ot [Ff]ound|does not exist|\b404\b", flat):
    print("NOT_FOUND"); sys.exit(0)
first = next((line.strip() for line in text.splitlines() if line.strip()), "no output")
print("OTHER " + first[:200])
PY
}

ERR="$(mktemp)"
OUT="$(mktemp)"
trap 'rm -f "$ERR" "$OUT"' EXIT

# check LABEL MODE CMD... -- run one read-only call; MODE is `required` (the
# resource must exist) or `optional` (absence is fine: the call proved the
# deployer may look). Never stops: every failure is recorded and the next
# check runs. CHECK_OK is 1 when the call succeeded (its stdout is in $OUT).
CHECK_OK=0
check() {
  local label="$1" mode="$2" kind detail status=0
  shift 2
  CHECK_OK=0
  if [[ "$DRY_RUN" -eq 1 ]]; then
    ops_run "$@"
    summary "$label" DRY-RUN "read-only"
    return 0
  fi
  "$@" > "$OUT" 2> "$ERR" || status=$?
  if [[ "$status" -eq 0 ]]; then
    CHECK_OK=1
    summary "$label" PASS "read-only call succeeded"
    return 0
  fi
  read -r kind detail <<< "$(classify "$ERR")"
  case "$kind" in
    API)
      [[ "$detail" == "-" ]] && detail="an API gcloud did not name (${label})"
      add_unique MISSING_APIS "${detail}"
      summary "$label" FAIL "API disabled: ${detail}" ;;
    PERMISSION)
      if [[ "$detail" == "-" ]]; then
        add_unique MISSING_PERMISSIONS "unnamed permission for: ${label}"
        summary "$label" FAIL "permission denied (gcloud named no permission)"
      else
        add_unique MISSING_PERMISSIONS "${detail} (${label})"
        summary "$label" FAIL "missing permission ${detail}"
      fi ;;
    NOT_FOUND)
      if [[ "$mode" == "optional" ]]; then
        summary "$label" INFO "does not exist yet; the deployer may look"
      else
        add_unique MISSING_RESOURCES "$label"
        summary "$label" FAIL "not found"
      fi ;;
    *)
      add_unique OTHER "${label}: ${detail}"
      summary "$label" FAIL "${detail}" ;;
  esac
  return 0
}

# probe LABEL URL METHOD EXPECT PERMISSION... -- testIamPermissions (read-only).
# EXPECT `held`: every permission must be granted; `absent`: none may be.
TOKEN=""
probe() {
  local label="$1" url="$2" method="$3" expect="$4" body response code verdict
  shift 4
  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf 'DRY-RUN: curl -X %s %s (testIamPermissions: %s)\n' "$method" "$url" "$*"
    summary "$label" DRY-RUN "would require the permission(s) ${expect}: $*"
    return 0
  fi
  if [[ -z "$TOKEN" ]]; then
    add_unique OTHER "${label}: no access token (gcloud auth print-access-token failed)"
    summary "$label" FAIL "no access token"
    return 0
  fi
  body="$(python3 -c 'import json, sys; print(json.dumps({"permissions": sys.argv[1:]}))' "$@")"
  if [[ "$method" == "GET" ]]; then
    response="$(printf 'Authorization: Bearer %s\n' "$TOKEN" \
      | curl -sS -X GET -H @- -w '\n%{http_code}' "$url" 2> "$ERR" || true)"
  else
    response="$(printf 'Authorization: Bearer %s\n' "$TOKEN" \
      | curl -sS -X POST -H @- -H 'Content-Type: application/json' --data "$body" \
          -w '\n%{http_code}' "$url" 2> "$ERR" || true)"
  fi
  code="${response##*$'\n'}"
  printf '%s' "${response%$'\n'*}" > "$OUT"
  if [[ "$code" != "200" ]]; then
    cat "$OUT" >> "$ERR"
    local kind detail
    read -r kind detail <<< "$(classify "$ERR")"
    case "$kind" in
      API)
        [[ "$detail" == "-" ]] && detail="an API the probe did not name (${label})"
        add_unique MISSING_APIS "$detail"; summary "$label" FAIL "API disabled: ${detail}" ;;
      *) add_unique OTHER "${label}: HTTP ${code:-none} ${kind} ${detail}"
         summary "$label" FAIL "testIamPermissions answered HTTP ${code:-none}" ;;
    esac
    return 0
  fi
  verdict="$(python3 - "$OUT" "$expect" "$@" << 'PY'
import json, sys
held = set(json.load(open(sys.argv[1])).get("permissions") or [])
expect, asked = sys.argv[2], sys.argv[3:]
wrong = [p for p in asked if (p not in held) == (expect == "held")]
print(" ".join(wrong))
PY
)"
  if [[ -z "$verdict" ]]; then
    summary "$label" PASS "$([[ "$expect" == held ]] && printf 'holds' || printf 'does not hold') $*"
  elif [[ "$expect" == "held" ]]; then
    local permission
    for permission in $verdict; do add_unique MISSING_PERMISSIONS "${permission} (${label})"; done
    summary "$label" FAIL "missing permission(s): ${verdict}"
  else
    add_unique FORBIDDEN "${label}: holds ${verdict}"
    summary "$label" FAIL "must NOT hold: ${verdict}"
  fi
}

summary_header "Preflight as the deployer for ${SHA:0:12} (read-only: nothing is built, deployed or changed)"

# --- who is asking ----------------------------------------------------------
if [[ "$DRY_RUN" -eq 1 ]]; then
  ops_run gcloud auth list --filter=status:ACTIVE --format='value(account)'
  summary "identity" DRY-RUN "the active account (the deployer, in the workflow)"
  PROJECT_NUMBER="<PROJECT_NUMBER>"
else
  account="$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2> /dev/null | head -n 1 || true)"
  if [[ -z "$account" ]]; then
    add_unique OTHER "identity: no active gcloud account"
    summary "identity" FAIL "no active gcloud account"
  elif [[ "$account" == *.gserviceaccount.com ]]; then
    summary "identity" PASS "running as ${account}"
  else
    summary "identity" INFO "running as ${account}, a user account: this proves nothing about the deployer"
  fi
  PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)' 2> /dev/null || true)"
fi

# --- the read-only calls the deploy makes -------------------------------------
check "projects-describe" required gcloud projects describe "$PROJECT_ID" --format='value(projectId)'
# `services list` answers with the enabled set; a missing API is recorded by name.
check "services-list" required gcloud services list --enabled --project "$PROJECT_ID" --format='value(config.name)'
if [[ "$CHECK_OK" -eq 1 ]]; then
  for api in "${REQUIRED_APIS[@]}"; do
    if ! grep -qxF "$api" "$OUT"; then
      add_unique MISSING_APIS "$api"
      summary "api:${api}" FAIL "not enabled"
    fi
  done
fi
for account in "${RUNTIME_ACCOUNTS[@]}" "$BUILD_SA"; do
  check "service-account:${account%@*}" required \
    gcloud iam service-accounts describe "$account" --project "$PROJECT_ID" --format='value(email)'
done
check "artifact-repository" required \
  gcloud artifacts repositories describe "$REPOSITORY" --location "$REGION" --project "$PROJECT_ID"
check "api-image" optional \
  gcloud artifacts docker images describe "$API_IMAGE" --project "$PROJECT_ID" --format='value(image_summary.digest)'
check "worker-image" optional \
  gcloud artifacts docker images describe "$WORKER_IMAGE" --project "$PROJECT_ID" --format='value(image_summary.digest)'
for key in SECRET_SUPABASE_URL SECRET_SUPABASE_SERVICE_KEY SECRET_REDIS_URL SECRET_REDIS_TOKEN; do
  secret="$(milo_op "$key")"
  [[ -n "$secret" ]] || continue
  check "secret:${secret}" required gcloud secrets describe "$secret" --project "$PROJECT_ID" --format='value(name)'
  check "secret-versions:${secret}" required gcloud secrets versions list "$secret" --project "$PROJECT_ID" \
    --filter='state:ENABLED' --format='value(name)'
done
for key in SECRET_SUPABASE_URL SECRET_SUPABASE_SERVICE_KEY; do
  secret="$(milo_op "$key")"
  [[ -n "$secret" ]] || continue
  check "secret-policy:${secret}" required gcloud secrets get-iam-policy "$secret" --project "$PROJECT_ID" --format=json
done
if [[ -n "$(milo_op SECRET_PROVIDER_API_KEY)" ]]; then
  check "secret:$(milo_op SECRET_PROVIDER_API_KEY)" optional \
    gcloud secrets describe "$(milo_op SECRET_PROVIDER_API_KEY)" --project "$PROJECT_ID" --format='value(name)'
fi
check "api-service" required gcloud run services describe "$API_SERVICE" --region "$REGION" --project "$PROJECT_ID" --format=json
check "worker-job" required gcloud run jobs describe "$WORKER_JOB" --region "$REGION" --project "$PROJECT_ID" --format=json
if [[ -n "$CAPTURE_JOB" ]]; then
  check "capture-job" optional gcloud run jobs describe "$CAPTURE_JOB" --region "$REGION" --project "$PROJECT_ID" --format=json
fi
check "api-service-policy" required \
  gcloud run services get-iam-policy "$API_SERVICE" --region "$REGION" --project "$PROJECT_ID" --format=json
check "worker-job-policy" required \
  gcloud run jobs get-iam-policy "$WORKER_JOB" --region "$REGION" --project "$PROJECT_ID" --format=json
check "worker-executions" required \
  gcloud run jobs executions list --job "$WORKER_JOB" --region "$REGION" --project "$PROJECT_ID" --limit 1 \
  --format='value(metadata.name)'
# gcloud builds submit proves its default source bucket belongs to the project
# with a project-level bucket list (storage.buckets.list), then uploads to it.
check "build-source-bucket-list" required \
  gcloud storage buckets list --project "$PROJECT_ID" --filter="name:${BUILD_BUCKET_NAME}" --format='value(name)'
if [[ "$CHECK_OK" -eq 1 ]] && ! grep -qF "$BUILD_BUCKET_NAME" "$OUT"; then
  add_unique MISSING_RESOURCES "build-source-bucket gs://${BUILD_BUCKET_NAME}"
  summary "build-source-bucket" FAIL "gs://${BUILD_BUCKET_NAME} is not listed in the project"
fi
check "cloud-build-list" required \
  gcloud builds list --project "$PROJECT_ID" --region "$REGION" --limit 1 --format='value(id)'
# The same read cloud-run.sh makes for a build that did not succeed.
check "build-logs-read" required \
  gcloud logging read "resource.type=build" --project "$PROJECT_ID" --order=desc --limit 1 --freshness 1d \
  --format='value(textPayload)'

# --- testIamPermissions: what no read-only call can prove ---------------------
if [[ "$DRY_RUN" -eq 0 ]]; then
  TOKEN="$(gcloud auth print-access-token 2> /dev/null || true)"
fi
probe "permissions:project" \
  "https://cloudresourcemanager.googleapis.com/v1/projects/${PROJECT_ID}:testIamPermissions" POST held \
  "${PROJECT_PERMISSIONS[@]}"
# The async build path (cloud-run.sh): submit, poll `gcloud builds describe`,
# read the log of a build that did not succeed, prove the image exists.
probe "permissions:build-wait" \
  "https://cloudresourcemanager.googleapis.com/v1/projects/${PROJECT_ID}:testIamPermissions" POST held \
  cloudbuild.builds.create cloudbuild.builds.get logging.logEntries.list artifactregistry.dockerimages.get
probe "act-as:${BUILD_SA%@*} (build identity)" \
  "https://iam.googleapis.com/v1/projects/-/serviceAccounts/${BUILD_SA}:testIamPermissions" POST held \
  iam.serviceAccounts.actAs
for account in "${RUNTIME_ACCOUNTS[@]}"; do
  probe "act-as:${account%@*}" \
    "https://iam.googleapis.com/v1/projects/-/serviceAccounts/${account}:testIamPermissions" POST held \
    iam.serviceAccounts.actAs
done
probe "build-source-bucket-upload" \
  "https://storage.googleapis.com/storage/v1/b/${BUILD_BUCKET_NAME}/iam/testPermissions?permissions=storage.buckets.get&permissions=storage.objects.create" \
  GET held storage.buckets.get storage.objects.create
if [[ "$PROJECT_NUMBER" =~ ^[0-9]+$ || "$DRY_RUN" -eq 1 ]]; then
  compute_sa="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
  probe "act-as:compute-default (must be refused)" \
    "https://iam.googleapis.com/v1/projects/-/serviceAccounts/${compute_sa}:testIamPermissions" POST absent \
    iam.serviceAccounts.actAs
else
  add_unique OTHER "act-as:compute-default: the project number could not be read, so the Compute default SA could not be probed"
  summary "act-as:compute-default (must be refused)" FAIL "project number unreadable"
fi

# --- one report, everything at once --------------------------------------------
if [[ "$DRY_RUN" -eq 1 ]]; then
  summary_note "DRY RUN: nothing was called and nothing was changed."
  exit 0
fi
report() {
  local title="$1"
  shift
  [[ $# -gt 0 ]] || return 0
  printf '\n%s (%s):\n' "$title" "$#"
  printf '  - %s\n' "$@"
}
printf '\n== Preflight report ==\n'
report "MISSING API" "${MISSING_APIS[@]}"
report "MISSING PERMISSION" "${MISSING_PERMISSIONS[@]}"
report "MISSING RESOURCE" "${MISSING_RESOURCES[@]}"
report "MUST NOT HOLD" "${FORBIDDEN[@]}"
report "OTHER FAILURE" "${OTHER[@]}"
total=$(( ${#MISSING_APIS[@]} + ${#MISSING_PERMISSIONS[@]} + ${#MISSING_RESOURCES[@]} + ${#FORBIDDEN[@]} + ${#OTHER[@]} ))
if [[ "$total" -eq 0 ]]; then
  summary "preflight" PASS "every read-only call and permission probe succeeded; nothing was changed"
  summary_note "Preflight clean: the deploy's reads and the build submit / actAs permissions all hold. Nothing was changed."
  exit 0
fi
summary "preflight" FAIL "${#MISSING_APIS[@]} API(s): ${MISSING_APIS[*]:-none}; ${#MISSING_PERMISSIONS[@]} permission(s); ${#MISSING_RESOURCES[@]} resource(s); ${#FORBIDDEN[@]} forbidden; ${#OTHER[@]} other"
summary_note "Fix: scripts/ops/setup-wif.sh --plan, then --apply, in Cloud Shell (it enables the APIs, creates the build identity and grants the roles above; secrets and the image repository are scripts/deploy/gcp-bootstrap.sh's). Nothing was changed by this preflight."
exit 1

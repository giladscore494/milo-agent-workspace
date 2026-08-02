#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID=${PROJECT_ID:-big-cabinet-457321-t7}
REGION=${REGION:-us-central1}
REPOSITORY=${REPOSITORY:-milo-agent}
API_SERVICE_ACCOUNT=${API_SERVICE_ACCOUNT:-milo-api-runtime@big-cabinet-457321-t7.iam.gserviceaccount.com}
WORKER_SERVICE_ACCOUNT=${WORKER_SERVICE_ACCOUNT:-milo-worker-runtime@big-cabinet-457321-t7.iam.gserviceaccount.com}
DEPLOY_MODE=${DEPLOY_MODE:-check}
JOB_LAUNCHER_MODE=${JOB_LAUNCHER_MODE:-disabled}
API_SERVICE=${API_SERVICE:-milo-agent-api}
WORKER_JOB=${WORKER_JOB:-milo-agent-worker}
# Immutable image identity: the FULL 40-character commit SHA. Short SHAs are
# ambiguous (they can collide and they can be extended by future objects), so
# they are never used as a production image tag.
RELEASE_SHA=$(git rev-parse HEAD)
API_IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/$REPOSITORY/api:$RELEASE_SHA"
WORKER_IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/$REPOSITORY/worker:$RELEASE_SHA"
REQUIRED_APIS=(run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com)
REQUIRED_SECRETS=(KIMI_API_KEY SUPABASE_URL SUPABASE_SECRET_KEY UPSTASH_REDIS_REST_URL UPSTASH_REDIS_REST_TOKEN)
ENV_VAR_DELIMITER="@"

# Stage A posture: every execution flag is pinned OFF by the deployment. Later
# stages are enabled deliberately by an operator, never as a side effect of a
# release. The list mirrors backend/production_config.py EXECUTION_FLAGS.
STAGE_A_EXECUTION_FLAGS=(
  MILO_ENABLE_RUN_CREATION=false
  MILO_ENABLE_PROPOSAL_MUTATIONS=false
  MILO_ENABLE_PROPOSAL_READS=false
  MILO_ENABLE_RUN_CANCELLATION=false
  MILO_ENABLE_EXECUTION_CONTROL=false
  MILO_ENABLE_PAID_EXECUTION=false
)
STAGE_A_FLAG_NAMES=()
for _flag in "${STAGE_A_EXECUTION_FLAGS[@]}"; do
  STAGE_A_FLAG_NAMES+=("${_flag%%=*}")
done
unset _flag

# Environment variables this release owns. Anything else already configured on
# the service or job is preserved: the deploy uses --update-env-vars /
# --update-secrets, never the destructive --set-* variants.
API_ENV_VARS=(
  "ENVIRONMENT=production"
  "JOB_LAUNCHER=$JOB_LAUNCHER_MODE"
  "GCP_PROJECT_ID=$PROJECT_ID"
  "GCP_REGION=$REGION"
  "CLOUD_RUN_WORKER_JOB=$WORKER_JOB"
  "ALLOWED_CORS_ORIGINS=${ALLOWED_CORS_ORIGINS:-}"
  "${STAGE_A_EXECUTION_FLAGS[@]}"
)
WORKER_ENV_VARS=(
  "ENVIRONMENT=production"
  "GCP_PROJECT_ID=$PROJECT_ID"
  "GCP_REGION=$REGION"
  "${STAGE_A_EXECUTION_FLAGS[@]}"
)
# KIMI_API_KEY is bound to the worker only. The API never receives a provider
# key: it neither calls providers nor needs the ability to.
API_SECRETS=(
  "SUPABASE_URL=SUPABASE_URL:latest"
  "SUPABASE_SERVICE_ROLE_KEY=SUPABASE_SECRET_KEY:latest"
  "UPSTASH_REDIS_REST_URL=UPSTASH_REDIS_REST_URL:latest"
  "UPSTASH_REDIS_REST_TOKEN=UPSTASH_REDIS_REST_TOKEN:latest"
)
WORKER_SECRETS=(
  "SUPABASE_URL=SUPABASE_URL:latest"
  "SUPABASE_SERVICE_ROLE_KEY=SUPABASE_SECRET_KEY:latest"
  "KIMI_API_KEY=KIMI_API_KEY:latest"
  "UPSTASH_REDIS_REST_URL=UPSTASH_REDIS_REST_URL:latest"
  "UPSTASH_REDIS_REST_TOKEN=UPSTASH_REDIS_REST_TOKEN:latest"
)

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "Required command '$1' was not found."
}

join_by() {
  local sep="$1"
  shift
  local out="" item
  for item in "$@"; do
    if [[ -z "$out" ]]; then
      out="$item"
    else
      out="$out$sep$item"
    fi
  done
  printf '%s' "$out"
}

# gcloud dict flags accept an alternate delimiter via the ^DELIM^ prefix, which
# keeps comma-containing values (ALLOWED_CORS_ORIGINS) intact.
delimited_env_arg() {
  printf '^%s^%s' "$ENV_VAR_DELIMITER" "$(join_by "$ENV_VAR_DELIMITER" "$@")"
}

require_full_release_sha() {
  if [[ ! "$RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]]; then
    fail "Release SHA '$RELEASE_SHA' is not a full 40-character commit SHA. Image tags must be immutable full SHAs."
  fi
}

require_allowed_cors_origins() {
  if [[ -z "${ALLOWED_CORS_ORIGINS:-}" ]]; then
    fail "ALLOWED_CORS_ORIGINS must be set to one or more explicit origins before deployment. Do not use '*'."
  fi
  IFS=',' read -ra origins <<<"$ALLOWED_CORS_ORIGINS"
  for origin in "${origins[@]}"; do
    origin="${origin//[[:space:]]/}"
    if [[ -z "$origin" ]]; then
      fail "ALLOWED_CORS_ORIGINS contains an empty origin."
    fi
    if [[ "$origin" == "*" ]]; then
      fail "ALLOWED_CORS_ORIGINS must not contain '*'. Use explicit origins only."
    fi
    if [[ "$origin" == *"$ENV_VAR_DELIMITER"* ]]; then
      fail "ALLOWED_CORS_ORIGINS must not contain the gcloud env-var delimiter '$ENV_VAR_DELIMITER'."
    fi
  done
}

preflight() {
  require_command git
  require_command gcloud
  require_full_release_sha
  require_allowed_cors_origins

  local account
  account=$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null | head -n 1 || true)
  [[ -n "$account" ]] || fail "No active gcloud account found. Run 'gcloud auth login' with the intended operator identity."

  gcloud projects describe "$PROJECT_ID" --format='value(projectId)' >/dev/null || \
    fail "Project '$PROJECT_ID' is not accessible to the active gcloud account."

  for api in "${REQUIRED_APIS[@]}"; do
    local state
    state=$(gcloud services list --enabled --project "$PROJECT_ID" --filter="config.name:$api" --format='value(config.name)' 2>/dev/null || true)
    [[ "$state" == "$api" ]] || fail "Required API '$api' is not enabled for project '$PROJECT_ID'."
  done

  if [[ "$API_SERVICE_ACCOUNT" == "$WORKER_SERVICE_ACCOUNT" ]]; then
    fail "API_SERVICE_ACCOUNT and WORKER_SERVICE_ACCOUNT must be distinct identities."
  fi

  gcloud iam service-accounts describe "$API_SERVICE_ACCOUNT" --project "$PROJECT_ID" --format='value(email)' >/dev/null || \
    fail "API runtime service account '$API_SERVICE_ACCOUNT' does not exist or is not accessible."

  gcloud iam service-accounts describe "$WORKER_SERVICE_ACCOUNT" --project "$PROJECT_ID" --format='value(email)' >/dev/null || \
    fail "Worker runtime service account '$WORKER_SERVICE_ACCOUNT' does not exist or is not accessible."

  gcloud artifacts repositories describe "$REPOSITORY" --location "$REGION" --project "$PROJECT_ID" >/dev/null || \
    fail "Artifact Registry repository '$REPOSITORY' does not exist in region '$REGION'. Create it before deployment."

  for secret in "${REQUIRED_SECRETS[@]}"; do
    gcloud secrets describe "$secret" --project "$PROJECT_ID" --format='value(name)' >/dev/null || \
      fail "Required Secret Manager secret '$secret' does not exist or is not accessible."
  done

  if [[ "$DEPLOY_MODE" == "apply" ]]; then
    # Post-deployment verification parses gcloud JSON with python3.
    require_command python3
  fi
}

print_targets() {
  cat <<TARGETS
Deployment mode: $DEPLOY_MODE
Job launcher mode: $JOB_LAUNCHER_MODE
Release SHA (full): $RELEASE_SHA
Project: $PROJECT_ID
Region: $REGION
Artifact Registry repository: $REPOSITORY
API service: $API_SERVICE
Worker job: $WORKER_JOB
API runtime service account: $API_SERVICE_ACCOUNT
Worker runtime service account: $WORKER_SERVICE_ACCOUNT
API image: $API_IMAGE
Worker image: $WORKER_IMAGE
Stage A execution flags: ${STAGE_A_FLAG_NAMES[*]} (all false)
Env/secret update mode: --update-env-vars / --update-secrets (non-destructive)
Cloud Build configs:
  API: scripts/deploy/cloudbuild-api.yaml
  Worker: scripts/deploy/cloudbuild-worker.yaml
TARGETS
}

# ---------------------------------------------------------------------------
# read-only inspection helpers (names and references only; never values of
# secrets, and only the explicitly allowlisted non-secret flag values)
# ---------------------------------------------------------------------------
CONTAINER_REPORT_PY='
import json
import sys

allow_values = set(sys.argv[1:])
try:
    doc = json.load(sys.stdin)
except Exception:
    doc = {}


def find_containers(node):
    if isinstance(node, dict):
        value = node.get("containers")
        if isinstance(value, list) and value:
            return value
        for child in node.values():
            found = find_containers(child)
            if found:
                return found
    elif isinstance(node, list):
        for child in node:
            found = find_containers(child)
            if found:
                return found
    return []


def find_service_account(node):
    if isinstance(node, dict):
        for key in ("serviceAccountName", "serviceAccount"):
            value = node.get(key)
            if isinstance(value, str) and value:
                return value
        for child in node.values():
            found = find_service_account(child)
            if found:
                return found
    elif isinstance(node, list):
        for child in node:
            found = find_service_account(child)
            if found:
                return found
    return ""


spec = doc.get("spec", doc) if isinstance(doc, dict) else {}
containers = find_containers(spec)
container = containers[0] if containers else {}
print("image\t%s" % (container.get("image") or ""))
print("service-account\t%s" % find_service_account(spec))
for entry in container.get("env") or []:
    if not isinstance(entry, dict):
        continue
    name = entry.get("name") or ""
    ref = ((entry.get("valueFrom") or {}).get("secretKeyRef") or {})
    if ref:
        print("secret\t%s\t%s:%s" % (name, ref.get("secret") or "", ref.get("key") or ""))
    else:
        print("env\t%s" % name)
        if name in allow_values:
            print("flag\t%s\t%s" % (name, entry.get("value") or ""))
'

describe_json() {
  local kind="$1" name="$2" out=""
  case "$kind" in
    service) out=$(gcloud run services describe "$name" --project "$PROJECT_ID" --region "$REGION" --format=json 2>/dev/null || true) ;;
    job) out=$(gcloud run jobs describe "$name" --project "$PROJECT_ID" --region "$REGION" --format=json 2>/dev/null || true) ;;
    *) fail "describe_json: unknown resource kind '$kind'." ;;
  esac
  [[ -n "$out" ]] || out='{}'
  printf '%s' "$out"
}

# binding_report KIND NAME — one record per line, tab separated:
#   image\t<image>          service-account\t<email>
#   env\t<NAME>             secret\t<ENV_NAME>\t<SECRET>:<VERSION>
#   flag\t<NAME>\t<VALUE>   (only for JOB_LAUNCHER / MILO_ENABLE_* flags)
binding_report() {
  describe_json "$1" "$2" | python3 -c "$CONTAINER_REPORT_PY" JOB_LAUNCHER "${STAGE_A_FLAG_NAMES[@]}"
}

report_field() {
  # report_field REPORT KIND -> the value column of the single-valued record
  printf '%s\n' "$1" | awk -F'\t' -v kind="$2" '$1 == kind { print $2; exit }'
}

binding_names() {
  # Stable identity of every binding: "env<TAB>NAME" / "secret<TAB>NAME".
  printf '%s\n' "$1" | awk -F'\t' '$1 == "env" || $1 == "secret" { print $1 "\t" $2 }' | sort -u
}

has_binding() {
  printf '%s\n' "$1" | grep -Fxq "$2"
}

assert_bindings_preserved() {
  local label="$1" before="$2" after="$3" line missing=""
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    has_binding "$after" "$line" || missing+="  ${line//$'\t'/ }"$'\n'
  done <<<"$before"
  if [[ -n "$missing" ]]; then
    fail "$label lost pre-existing environment/secret bindings; the update was destructive:"$'\n'"$missing"
  fi
  echo "  preserved bindings: OK (no pre-existing env var or secret binding was dropped)"
}

verify_image_digest() {
  local label="$1" report="$2" expected_image="$3" deployed digest
  deployed=$(report_field "$report" image)
  digest=$(gcloud artifacts docker images describe "$expected_image" --project "$PROJECT_ID" \
    --format='value(image_summary.digest)' 2>/dev/null || true)
  if [[ "$deployed" == *"@"* ]]; then
    [[ -n "$digest" && "$deployed" == "${expected_image%:*}@$digest" ]] || \
      fail "$label runs image '$deployed', which does not resolve to the release digest of '$expected_image'."
  else
    [[ "$deployed" == "$expected_image" ]] || \
      fail "$label runs image '$deployed' instead of the release image '$expected_image'."
  fi
  [[ "$deployed" == *"$RELEASE_SHA"* || -n "$digest" ]] || \
    fail "$label image could not be tied to release SHA $RELEASE_SHA."
  echo "  image: $deployed"
  echo "  release SHA: $RELEASE_SHA"
  echo "  registry digest: ${digest:-<unresolved>}"
}

verify_service_account() {
  local label="$1" report="$2" expected="$3" actual
  actual=$(report_field "$report" service-account)
  [[ "$actual" == "$expected" ]] || \
    fail "$label runs as '$actual' instead of the expected runtime identity '$expected'."
  echo "  service account: $actual"
}

verify_env_names() {
  local label="$1" report="$2"
  shift 2
  local names name
  names=$(binding_names "$report")
  for name in "$@"; do
    has_binding "$names" "$(printf 'env\t%s' "$name")" || \
      fail "$label is missing required environment variable '$name'."
  done
  echo "  environment variable names: $(printf '%s\n' "$report" | awk -F'\t' '$1 == "env" { printf "%s ", $2 }')"
}

verify_secret_refs() {
  local label="$1" report="$2"
  shift 2
  local expected
  for expected in "$@"; do
    printf '%s\n' "$report" | awk -F'\t' '$1 == "secret" { print $2 "=" $3 }' | grep -Fxq "$expected" || \
      fail "$label is missing the expected secret reference '$expected'."
  done
  echo "  secret references: $(printf '%s\n' "$report" | awk -F'\t' '$1 == "secret" { printf "%s->%s ", $2, $3 }')"
}

verify_secret_absent() {
  local label="$1" report="$2" name="$3"
  if printf '%s\n' "$report" | awk -F'\t' '$1 == "secret" { print $2 }' | grep -Fxq "$name"; then
    fail "$label must not be bound to secret '$name'."
  fi
  echo "  secret '$name' absent: OK"
}

verify_stage_a_flags() {
  local label="$1" report="$2" kind name value
  while IFS=$'\t' read -r kind name value; do
    [[ "$kind" == "flag" ]] || continue
    case "$name" in
      JOB_LAUNCHER)
        [[ "$value" == "$JOB_LAUNCHER_MODE" ]] || \
          fail "$label has JOB_LAUNCHER='$value' instead of '$JOB_LAUNCHER_MODE'."
        ;;
      MILO_ENABLE_*)
        [[ "$value" == "false" ]] || \
          fail "$label has execution flag $name='$value'; Stage A requires false."
        ;;
    esac
  done <<<"$report"
  echo "  Stage A flags: JOB_LAUNCHER=$JOB_LAUNCHER_MODE, ${STAGE_A_FLAG_NAMES[*]} all false"
}

verify_no_public_access() {
  local kind="$1" name="$2" policy
  case "$kind" in
    service) policy=$(gcloud run services get-iam-policy "$name" --project "$PROJECT_ID" --region "$REGION" --format=json) ;;
    job) policy=$(gcloud run jobs get-iam-policy "$name" --project "$PROJECT_ID" --region "$REGION" --format=json) ;;
  esac
  if printf '%s' "$policy" | grep -qE '"(allUsers|allAuthenticatedUsers)"'; then
    fail "$kind '$name' grants access to allUsers/allAuthenticatedUsers. Production Cloud Run resources must stay private."
  fi
  echo "  IAM: private (no allUsers / allAuthenticatedUsers)"
}

worker_execution_count() {
  gcloud run jobs executions list --job "$WORKER_JOB" --project "$PROJECT_ID" --region "$REGION" \
    --format='value(metadata.name)' 2>/dev/null | grep -c '.' || true
}

verify_no_worker_execution() {
  local before="$1" after="$2"
  [[ "$before" == "$after" ]] || \
    fail "Worker job executions changed during deployment ($before -> $after). Deployment must never execute the worker."
  echo "  worker executions: $after (unchanged; deployment executed nothing)"
}

case "$DEPLOY_MODE" in
  check|apply) ;;
  *) fail "DEPLOY_MODE must be 'check' or 'apply'. Default is 'check'." ;;
esac

case "$JOB_LAUNCHER_MODE" in
  disabled|cloud_run) ;;
  *) fail "JOB_LAUNCHER_MODE must be 'disabled' or 'cloud_run'. Default is 'disabled'." ;;
esac

preflight
print_targets

if [[ "$DEPLOY_MODE" == "apply" && "$JOB_LAUNCHER_MODE" == "cloud_run" ]]; then
  echo "WARNING: JOB_LAUNCHER_MODE=cloud_run — the API will be deployed with the Cloud Run job launcher ENABLED. This is an explicit operator override of the safe default (disabled)." >&2
fi

if [[ "$DEPLOY_MODE" == "check" ]]; then
  echo "Check mode complete: prerequisites validated. No build, deploy, IAM change, worker execution, or paid API call was performed."
  exit 0
fi

# DEPLOY_MODE=apply is the only mode that builds, deploys, and grants the narrow Cloud Run jobs executor-with-overrides binding.

# Snapshot the live configuration first so the post-deploy verification can
# prove that no pre-existing environment variable or secret binding was lost.
API_BINDINGS_BEFORE=$(binding_names "$(binding_report service "$API_SERVICE")")
WORKER_BINDINGS_BEFORE=$(binding_names "$(binding_report job "$WORKER_JOB")")
WORKER_EXECUTIONS_BEFORE=$(worker_execution_count)

gcloud builds submit --project "$PROJECT_ID" --region "$REGION" --config scripts/deploy/cloudbuild-worker.yaml --substitutions "_WORKER_IMAGE=$WORKER_IMAGE" .
gcloud builds submit --project "$PROJECT_ID" --region "$REGION" --config scripts/deploy/cloudbuild-api.yaml --substitutions "_API_IMAGE=$API_IMAGE" .

# The worker job is deployed BEFORE the API and is never executed here.
gcloud run jobs deploy "$WORKER_JOB" --project "$PROJECT_ID" --region "$REGION" --image "$WORKER_IMAGE" \
  --service-account "$WORKER_SERVICE_ACCOUNT" --cpu 2 --memory 2Gi --task-timeout 3600 --max-retries 1 --parallelism 1 --tasks 1 \
  --update-env-vars "$(delimited_env_arg "${WORKER_ENV_VARS[@]}")" \
  --update-secrets "$(join_by ',' "${WORKER_SECRETS[@]}")"

# The API identity launches worker executions with overrides, so grant the executor binding to the API account, not the worker account.
gcloud run jobs add-iam-policy-binding "$WORKER_JOB" --project "$PROJECT_ID" --region "$REGION" \
  --member "serviceAccount:$API_SERVICE_ACCOUNT" --role roles/run.jobsExecutorWithOverrides >/dev/null

gcloud run deploy "$API_SERVICE" --project "$PROJECT_ID" --region "$REGION" --image "$API_IMAGE" \
  --service-account "$API_SERVICE_ACCOUNT" --no-allow-unauthenticated --port 8080 --cpu 1 --memory 1Gi --timeout 300 --max-instances 10 \
  --update-env-vars "$(delimited_env_arg "${API_ENV_VARS[@]}")" \
  --update-secrets "$(join_by ',' "${API_SECRETS[@]}")"

# ---------------------------------------------------------------------------
# post-deployment verification (read-only; no execution, no IAM change)
# ---------------------------------------------------------------------------
API_REPORT=$(binding_report service "$API_SERVICE")
WORKER_REPORT=$(binding_report job "$WORKER_JOB")

echo "Verifying worker job '$WORKER_JOB':"
verify_image_digest "Worker job '$WORKER_JOB'" "$WORKER_REPORT" "$WORKER_IMAGE"
verify_service_account "Worker job '$WORKER_JOB'" "$WORKER_REPORT" "$WORKER_SERVICE_ACCOUNT"
verify_env_names "Worker job '$WORKER_JOB'" "$WORKER_REPORT" ENVIRONMENT GCP_PROJECT_ID GCP_REGION "${STAGE_A_FLAG_NAMES[@]}"
verify_secret_refs "Worker job '$WORKER_JOB'" "$WORKER_REPORT" "${WORKER_SECRETS[@]}"
verify_stage_a_flags "Worker job '$WORKER_JOB'" "$WORKER_REPORT"
assert_bindings_preserved "Worker job '$WORKER_JOB'" "$WORKER_BINDINGS_BEFORE" "$(binding_names "$WORKER_REPORT")"
verify_no_public_access job "$WORKER_JOB"

echo "Verifying API service '$API_SERVICE':"
verify_image_digest "API service '$API_SERVICE'" "$API_REPORT" "$API_IMAGE"
verify_service_account "API service '$API_SERVICE'" "$API_REPORT" "$API_SERVICE_ACCOUNT"
verify_env_names "API service '$API_SERVICE'" "$API_REPORT" ENVIRONMENT JOB_LAUNCHER GCP_PROJECT_ID GCP_REGION CLOUD_RUN_WORKER_JOB ALLOWED_CORS_ORIGINS "${STAGE_A_FLAG_NAMES[@]}"
verify_secret_refs "API service '$API_SERVICE'" "$API_REPORT" "${API_SECRETS[@]}"
verify_secret_absent "API service '$API_SERVICE'" "$API_REPORT" KIMI_API_KEY
verify_secret_absent "API service '$API_SERVICE'" "$API_REPORT" MOONSHOT_API_KEY
verify_stage_a_flags "API service '$API_SERVICE'" "$API_REPORT"
assert_bindings_preserved "API service '$API_SERVICE'" "$API_BINDINGS_BEFORE" "$(binding_names "$API_REPORT")"
verify_no_public_access service "$API_SERVICE"

echo "Verifying that the deployment executed nothing:"
verify_no_worker_execution "$WORKER_EXECUTIONS_BEFORE" "$(worker_execution_count)"

service_url=$(gcloud run services describe "$API_SERVICE" --project "$PROJECT_ID" --region "$REGION" --format='value(status.url)')
echo "API service URL: $service_url"
echo "Deployment complete. Worker job was deployed but not executed."

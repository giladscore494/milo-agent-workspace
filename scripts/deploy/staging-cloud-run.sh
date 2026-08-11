#!/usr/bin/env bash
# Isolated Stage B staging deployment (STAGING PROJECT ONLY).
#
# Deploys the MILO API service and worker job into the dedicated staging GCP
# project with a zero-cost posture: the mock lifecycle engine
# (MILO_WORKER_ENGINE=mock), no provider key anywhere, paid execution off,
# run creation/cancellation enabled so the mocked lifecycle can be exercised
# end to end. Shares the image/flag contract with the production deployer
# (deployment-contract.sh) but is intentionally a separate script: it can
# NEVER deploy to production — the production project id is hard-refused.
#
# Usage:
#   DEPLOY_MODE=check scripts/deploy/staging-cloud-run.sh   # default, no mutation
#   DEPLOY_MODE=apply scripts/deploy/staging-cloud-run.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deployment-contract.sh
source "$SCRIPT_DIR/deployment-contract.sh"

PRODUCTION_PROJECT_ID="big-cabinet-457321-t7"

PROJECT_ID=${STAGING_PROJECT_ID:-milo-agent-staging-xxxx}
REGION=${STAGING_REGION:-us-central1}
REPOSITORY=${STAGING_REPOSITORY:-milo-agent-staging}
API_SERVICE=${STAGING_API_SERVICE:-milo-agent-api-staging}
WORKER_JOB=${STAGING_WORKER_JOB:-milo-agent-worker-staging}
API_SERVICE_ACCOUNT=${STAGING_API_SERVICE_ACCOUNT:-milo-api-staging@${PROJECT_ID}.iam.gserviceaccount.com}
WORKER_SERVICE_ACCOUNT=${STAGING_WORKER_SERVICE_ACCOUNT:-milo-worker-staging@${PROJECT_ID}.iam.gserviceaccount.com}
GATEWAY_SERVICE_ACCOUNT=${STAGING_GATEWAY_SERVICE_ACCOUNT:-milo-gateway-staging@${PROJECT_ID}.iam.gserviceaccount.com}
DEPLOY_MODE=${DEPLOY_MODE:-check}
STAGING_SUPABASE_PROJECT_REF=${STAGING_SUPABASE_PROJECT_REF:-cxlwavxvwgrfikkudtzf}
STAGING_REDIS_SERVICE=${STAGING_REDIS_SERVICE:-milo-redis-shim-staging}
# Resolved in apply mode from the deployed shim service; may be pre-set.
STAGING_REDIS_HOST=${STAGING_REDIS_HOST:-}

RELEASE_SHA=$(git rev-parse HEAD)
API_IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/$REPOSITORY/$MILO_API_IMAGE_REPO:$RELEASE_SHA"
WORKER_IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/$REPOSITORY/$MILO_WORKER_IMAGE_REPO:$RELEASE_SHA"
ENV_VAR_DELIMITER="$MILO_ENV_VAR_DELIMITER"

REQUIRED_SECRETS=(SUPABASE_URL SUPABASE_SECRET_KEY UPSTASH_REDIS_REST_URL UPSTASH_REDIS_REST_TOKEN)

fail() { echo "ERROR: $*" >&2; exit 1; }

join_by() {
  local sep="$1"; shift
  local out="" item
  for item in "$@"; do
    if [[ -z "$out" ]]; then out="$item"; else out="$out$sep$item"; fi
  done
  printf '%s' "$out"
}

delimited_env_arg() { printf '^%s^%s' "$ENV_VAR_DELIMITER" "$(join_by "$ENV_VAR_DELIMITER" "$@")"; }

# ---------------------------------------------------------------------------
# Hard staging isolation: this script can never touch production.
# ---------------------------------------------------------------------------
require_staging_isolation() {
  [[ "$PROJECT_ID" != "$PRODUCTION_PROJECT_ID" ]] || fail "refusing to run: PROJECT_ID is the production project"
  local value
  for value in "$API_SERVICE_ACCOUNT" "$WORKER_SERVICE_ACCOUNT" "$GATEWAY_SERVICE_ACCOUNT" "$API_IMAGE" "$WORKER_IMAGE" "$API_SERVICE" "$WORKER_JOB"; do
    [[ "$value" != *"$PRODUCTION_PROJECT_ID"* ]] || fail "staging configuration references the production project: $value"
  done
  [[ "$API_SERVICE_ACCOUNT" != *"-compute@developer.gserviceaccount.com" ]] || fail "the default compute service account must not be the API runtime identity"
  [[ "$WORKER_SERVICE_ACCOUNT" != *"-compute@developer.gserviceaccount.com" ]] || fail "the default compute service account must not be the worker runtime identity"
  [[ "$API_SERVICE_ACCOUNT" != "$WORKER_SERVICE_ACCOUNT" ]] || fail "API and worker runtime identities must be distinct"
  [[ "$GATEWAY_SERVICE_ACCOUNT" != "$WORKER_SERVICE_ACCOUNT" ]] || fail "gateway and worker identities must be distinct"
  [[ "$RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]] || fail "release SHA must be the full 40-character commit SHA"
}

# ---------------------------------------------------------------------------
# Staging environment posture: Stage B — execution enabled, zero cost.
# ---------------------------------------------------------------------------
# Small budget caps so the mock engine can trip every limit cheaply.
STAGING_BUDGET_ENV=(
  "MILO_MAX_MODEL_CALLS_PER_RUN=${MILO_MAX_MODEL_CALLS_PER_RUN:-25}"
  "MILO_MAX_INPUT_TOKENS_PER_RUN=${MILO_MAX_INPUT_TOKENS_PER_RUN:-100000}"
  "MILO_MAX_OUTPUT_TOKENS_PER_RUN=${MILO_MAX_OUTPUT_TOKENS_PER_RUN:-50000}"
  "MILO_MAX_TOTAL_TOKENS_PER_RUN=${MILO_MAX_TOTAL_TOKENS_PER_RUN:-120000}"
  "MILO_MAX_ESTIMATED_COST_PER_RUN=${MILO_MAX_ESTIMATED_COST_PER_RUN:-1.0}"
  "MILO_MAX_COST_PER_RUN=${MILO_MAX_COST_PER_RUN:-1.0}"
  "MILO_MAX_RUN_DURATION_SECONDS=${MILO_MAX_RUN_DURATION_SECONDS:-600}"
  "MILO_MAX_RETRIES=${MILO_MAX_RETRIES:-3}"
  "MILO_MAX_AGENT_STEPS=${MILO_MAX_AGENT_STEPS:-50}"
  "MILO_DAILY_USER_BUDGET=${MILO_DAILY_USER_BUDGET:-5.0}"
  "MILO_DAILY_PROJECT_BUDGET=${MILO_DAILY_PROJECT_BUDGET:-5.0}"
  "MILO_ESTIMATED_COST_PER_CALL=${MILO_ESTIMATED_COST_PER_CALL:-0.001}"
)

API_ENV_VARS=(
  "ENVIRONMENT=staging"
  "JOB_LAUNCHER=cloud_run"
  "GCP_PROJECT_ID=$PROJECT_ID"
  "GCP_REGION=$REGION"
  "CLOUD_RUN_WORKER_JOB=$WORKER_JOB"
  "ALLOWED_CORS_ORIGINS=${ALLOWED_CORS_ORIGINS:-https://staging.milo.invalid}"
  "MILO_APPROVED_GATEWAY_IDENTITIES=$GATEWAY_SERVICE_ACCOUNT"
  "MILO_APPROVED_WORKER_IDENTITIES=$WORKER_SERVICE_ACCOUNT"
  # Stage B staging posture: mocked lifecycle on, everything paid stays off.
  "MILO_ENABLE_RUN_CREATION=true"
  "MILO_ENABLE_RUN_CANCELLATION=true"
  "MILO_ENABLE_PROPOSAL_MUTATIONS=false"
  "MILO_ENABLE_PROPOSAL_READS=false"
  "MILO_ENABLE_EXECUTION_CONTROL=false"
  "MILO_ENABLE_PAID_EXECUTION=false"
  "${STAGING_BUDGET_ENV[@]}"
)
WORKER_ENV_VARS=(
  "ENVIRONMENT=staging"
  "GCP_PROJECT_ID=$PROJECT_ID"
  "GCP_REGION=$REGION"
  "MILO_WORKER_ENGINE=mock"
  "MILO_ENABLE_PAID_EXECUTION=false"
  "MILO_WORKER_LEASE_SECONDS=${MILO_WORKER_LEASE_SECONDS:-120}"
  "MILO_WORKER_HEARTBEAT_INTERVAL_SECONDS=${MILO_WORKER_HEARTBEAT_INTERVAL_SECONDS:-15}"
  "${STAGING_BUDGET_ENV[@]}"
)
API_SECRETS=(
  "SUPABASE_URL=SUPABASE_URL:latest"
  "SUPABASE_SERVICE_ROLE_KEY=SUPABASE_SECRET_KEY:latest"
  "UPSTASH_REDIS_REST_URL=UPSTASH_REDIS_REST_URL:latest"
  "UPSTASH_REDIS_REST_TOKEN=UPSTASH_REDIS_REST_TOKEN:latest"
)
WORKER_SECRETS=("${API_SECRETS[@]}")

# No provider key may ever be bound in staging (Stage B is zero-cost).
require_no_provider_key_in_staging_config() {
  local name entry
  for name in "${MILO_PROVIDER_KEY_ENV_NAMES[@]}"; do
    for entry in "${API_ENV_VARS[@]}" "${WORKER_ENV_VARS[@]}" "${API_SECRETS[@]}" "${WORKER_SECRETS[@]}"; do
      [[ "$entry" != "$name="* ]] || fail "provider key $name must not appear in staging configuration"
    done
  done
}

ensure_service_account() {
  local email="$1" display="$2"
  local account_id="${email%%@*}"
  if ! gcloud iam service-accounts describe "$email" --project "$PROJECT_ID" >/dev/null 2>&1; then
    echo "Creating service account $email"
    gcloud iam service-accounts create "$account_id" --project "$PROJECT_ID" --display-name "$display"
  fi
}

ensure_repository() {
  if ! gcloud artifacts repositories describe "$REPOSITORY" --project "$PROJECT_ID" --location "$REGION" >/dev/null 2>&1; then
    echo "Creating Artifact Registry repository $REPOSITORY"
    gcloud artifacts repositories create "$REPOSITORY" --project "$PROJECT_ID" --location "$REGION" --repository-format docker \
      --description "MILO staging images (Stage B)"
  fi
}

require_secret_with_version() {
  local secret="$1"
  gcloud secrets describe "$secret" --project "$PROJECT_ID" >/dev/null 2>&1 \
    || fail "Secret Manager secret '$secret' does not exist in $PROJECT_ID. Create it (metadata only here; values are entered by the operator)."
  local versions
  versions=$(gcloud secrets versions list "$secret" --project "$PROJECT_ID" --filter="state=enabled" --format="value(name)" | head -1)
  [[ -n "$versions" ]] || fail "Secret '$secret' has no enabled version in $PROJECT_ID. The operator must add the staging value."
}

grant_secret_access() {
  local secret="$1" member="$2"
  gcloud secrets add-iam-policy-binding "$secret" --project "$PROJECT_ID" \
    --member "serviceAccount:$member" --role roles/secretmanager.secretAccessor >/dev/null
}

main() {
  require_staging_isolation
  require_no_provider_key_in_staging_config

  echo "Staging deployment plan:"
  echo "  project:        $PROJECT_ID (production $PRODUCTION_PROJECT_ID untouched)"
  echo "  region:         $REGION"
  echo "  api service:    $API_SERVICE (identity $API_SERVICE_ACCOUNT)"
  echo "  worker job:     $WORKER_JOB (identity $WORKER_SERVICE_ACCOUNT)"
  echo "  gateway id:     $GATEWAY_SERVICE_ACCOUNT (no secrets, no job access)"
  echo "  images:         $API_IMAGE / $WORKER_IMAGE"
  echo "  posture:        ENVIRONMENT=staging, mock engine, paid execution OFF, no provider key"

  if [[ "$DEPLOY_MODE" != "apply" ]]; then
    echo "DEPLOY_MODE=check — no mutation performed. Set DEPLOY_MODE=apply to deploy."
    exit 0
  fi

  ensure_repository
  ensure_service_account "$API_SERVICE_ACCOUNT" "MILO staging API runtime"
  ensure_service_account "$WORKER_SERVICE_ACCOUNT" "MILO staging worker runtime"
  ensure_service_account "$GATEWAY_SERVICE_ACCOUNT" "MILO staging gateway identity"

  local secret
  for secret in "${REQUIRED_SECRETS[@]}"; do
    require_secret_with_version "$secret"
    grant_secret_access "$secret" "$API_SERVICE_ACCOUNT"
    grant_secret_access "$secret" "$WORKER_SERVICE_ACCOUNT"
  done

  # Runtime dependency pins (fail-closed in backend/production_config.py):
  # the staging runtime refuses to start against any Supabase project or
  # Redis endpoint other than the ones pinned here.
  if [[ -z "$STAGING_REDIS_HOST" ]]; then
    redis_url=$(gcloud run services describe "$STAGING_REDIS_SERVICE" --project "$PROJECT_ID" --region "$REGION" --format "value(status.url)") \
      || fail "cannot resolve the staging Redis service '$STAGING_REDIS_SERVICE'; deploy it first or set STAGING_REDIS_HOST"
    STAGING_REDIS_HOST="${redis_url#https://}"
    [[ -n "$STAGING_REDIS_HOST" ]] || fail "staging Redis host resolution returned empty"
  fi
  [[ "$STAGING_REDIS_HOST" != *"upstash.io"* || -n "${STAGING_ALLOW_UPSTASH:-}" ]] \
    || fail "STAGING_REDIS_HOST resolves to an Upstash endpoint; set STAGING_ALLOW_UPSTASH=1 only for a dedicated STAGING Upstash database, never the production one"
  DEPENDENCY_PINS=(
    "MILO_EXPECTED_SUPABASE_PROJECT_REF=$STAGING_SUPABASE_PROJECT_REF"
    "MILO_EXPECTED_REDIS_HOST=$STAGING_REDIS_HOST"
  )

  gcloud builds submit --project "$PROJECT_ID" --region "$REGION" --config scripts/deploy/cloudbuild-worker.yaml --substitutions "_WORKER_IMAGE=$WORKER_IMAGE" .
  gcloud builds submit --project "$PROJECT_ID" --region "$REGION" --config scripts/deploy/cloudbuild-api.yaml --substitutions "_API_IMAGE=$API_IMAGE" .

  # Worker job first (never executed by this script).
  if gcloud run jobs describe "$WORKER_JOB" --project "$PROJECT_ID" --region "$REGION" >/dev/null 2>&1; then
    gcloud run jobs update "$WORKER_JOB" --project "$PROJECT_ID" --region "$REGION" \
      --image "$WORKER_IMAGE" --service-account "$WORKER_SERVICE_ACCOUNT" \
      --update-env-vars "$(delimited_env_arg "${WORKER_ENV_VARS[@]}" "${DEPENDENCY_PINS[@]}")" \
      --update-secrets "$(delimited_env_arg "${WORKER_SECRETS[@]}")" \
      --cpu 1 --memory 1Gi --task-timeout 900 --max-retries 1 --parallelism 1 --tasks 1
  else
    gcloud run jobs create "$WORKER_JOB" --project "$PROJECT_ID" --region "$REGION" \
      --image "$WORKER_IMAGE" --service-account "$WORKER_SERVICE_ACCOUNT" \
      --set-env-vars "$(delimited_env_arg "${WORKER_ENV_VARS[@]}" "${DEPENDENCY_PINS[@]}")" \
      --set-secrets "$(delimited_env_arg "${WORKER_SECRETS[@]}")" \
      --cpu 1 --memory 1Gi --task-timeout 900 --max-retries 1 --parallelism 1 --tasks 1
  fi

  # Minimum launch permission for the actual launcher implementation
  # (jobs.run WITH containerOverrides): executor-with-overrides, scoped to
  # this one job, granted to the API identity only.
  gcloud run jobs add-iam-policy-binding "$WORKER_JOB" --project "$PROJECT_ID" --region "$REGION" \
    --member "serviceAccount:$API_SERVICE_ACCOUNT" --role roles/run.jobsExecutorWithOverrides >/dev/null

  # API service (private).
  if gcloud run services describe "$API_SERVICE" --project "$PROJECT_ID" --region "$REGION" >/dev/null 2>&1; then
    gcloud run deploy "$API_SERVICE" --project "$PROJECT_ID" --region "$REGION" \
      --image "$API_IMAGE" --service-account "$API_SERVICE_ACCOUNT" --no-allow-unauthenticated \
      --update-env-vars "$(delimited_env_arg "${API_ENV_VARS[@]}" "${DEPENDENCY_PINS[@]}")" \
      --update-secrets "$(delimited_env_arg "${API_SECRETS[@]}")" \
      --port 8080 --cpu 1 --memory 1Gi --timeout 300 --max-instances 3
  else
    gcloud run deploy "$API_SERVICE" --project "$PROJECT_ID" --region "$REGION" \
      --image "$API_IMAGE" --service-account "$API_SERVICE_ACCOUNT" --no-allow-unauthenticated \
      --set-env-vars "$(delimited_env_arg "${API_ENV_VARS[@]}" "${DEPENDENCY_PINS[@]}")" \
      --set-secrets "$(delimited_env_arg "${API_SECRETS[@]}")" \
      --port 8080 --cpu 1 --memory 1Gi --timeout 300 --max-instances 3
  fi

  local api_url
  api_url=$(gcloud run services describe "$API_SERVICE" --project "$PROJECT_ID" --region "$REGION" --format "value(status.url)")
  [[ -n "$api_url" ]] || fail "could not resolve the staging API URL"

  # Audiences: gateway audience = the API URL; worker audience is a distinct
  # staging-scoped value so wrong-audience rejection is testable.
  gcloud run services update "$API_SERVICE" --project "$PROJECT_ID" --region "$REGION" \
    --update-env-vars "$(delimited_env_arg "MILO_GATEWAY_AUDIENCE=$api_url" "MILO_WORKER_AUDIENCE=$api_url/internal-worker")"

  # Cloud Run IAM: only the gateway and worker identities may invoke the API.
  gcloud run services add-iam-policy-binding "$API_SERVICE" --project "$PROJECT_ID" --region "$REGION" \
    --member "serviceAccount:$GATEWAY_SERVICE_ACCOUNT" --role roles/run.invoker >/dev/null
  gcloud run services add-iam-policy-binding "$API_SERVICE" --project "$PROJECT_ID" --region "$REGION" \
    --member "serviceAccount:$WORKER_SERVICE_ACCOUNT" --role roles/run.invoker >/dev/null

  echo "Staging deployment complete."
  echo "  API URL: $api_url"
  echo "  Gateway audience: $api_url"
  echo "  Worker audience:  $api_url/internal-worker"
  echo "  Reminder: the worker job was deployed but NOT executed."
}

main "$@"

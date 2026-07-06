#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID=${PROJECT_ID:-big-cabinet-457321-t7}
REGION=${REGION:-us-central1}
REPOSITORY=${REPOSITORY:-milo-agent}
SERVICE_ACCOUNT=${SERVICE_ACCOUNT:-id-kimi-agent-runner@big-cabinet-457321-t7.iam.gserviceaccount.com}
DEPLOY_MODE=${DEPLOY_MODE:-check}
API_SERVICE=${API_SERVICE:-milo-agent-api}
WORKER_JOB=${WORKER_JOB:-milo-agent-worker}
SHORT_SHA=$(git rev-parse --short HEAD)
API_IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/$REPOSITORY/api:$SHORT_SHA"
WORKER_IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/$REPOSITORY/worker:$SHORT_SHA"
REQUIRED_APIS=(run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com)
REQUIRED_SECRETS=(KIMI_API_KEY SUPABASE_URL SUPABASE_SECRET_KEY)
ENV_VAR_DELIMITER="@"

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "Required command '$1' was not found."
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

  gcloud iam service-accounts describe "$SERVICE_ACCOUNT" --project "$PROJECT_ID" --format='value(email)' >/dev/null || \
    fail "Runtime service account '$SERVICE_ACCOUNT' does not exist or is not accessible."

  gcloud artifacts repositories describe "$REPOSITORY" --location "$REGION" --project "$PROJECT_ID" >/dev/null || \
    fail "Artifact Registry repository '$REPOSITORY' does not exist in region '$REGION'. Create it before deployment."

  for secret in "${REQUIRED_SECRETS[@]}"; do
    gcloud secrets describe "$secret" --project "$PROJECT_ID" --format='value(name)' >/dev/null || \
      fail "Required Secret Manager secret '$secret' does not exist or is not accessible."
  done
}

print_targets() {
  cat <<TARGETS
Deployment mode: $DEPLOY_MODE
Project: $PROJECT_ID
Region: $REGION
Artifact Registry repository: $REPOSITORY
API service: $API_SERVICE
Worker job: $WORKER_JOB
Runtime service account: $SERVICE_ACCOUNT
API image: $API_IMAGE
Worker image: $WORKER_IMAGE
Cloud Build configs:
  API: scripts/deploy/cloudbuild-api.yaml
  Worker: scripts/deploy/cloudbuild-worker.yaml
TARGETS
}

case "$DEPLOY_MODE" in
  check|apply) ;;
  *) fail "DEPLOY_MODE must be 'check' or 'apply'. Default is 'check'." ;;
esac

preflight
print_targets

if [[ "$DEPLOY_MODE" == "check" ]]; then
  echo "Check mode complete: prerequisites validated. No build, deploy, IAM change, worker execution, or paid API call was performed."
  exit 0
fi

# DEPLOY_MODE=apply is the only mode that builds, deploys, and grants the narrow Cloud Run jobs executor-with-overrides binding.
gcloud builds submit --project "$PROJECT_ID" --region "$REGION" --config scripts/deploy/cloudbuild-worker.yaml --substitutions "_WORKER_IMAGE=$WORKER_IMAGE" .
gcloud builds submit --project "$PROJECT_ID" --region "$REGION" --config scripts/deploy/cloudbuild-api.yaml --substitutions "_API_IMAGE=$API_IMAGE" .

gcloud run jobs deploy "$WORKER_JOB" --project "$PROJECT_ID" --region "$REGION" --image "$WORKER_IMAGE" \
  --service-account "$SERVICE_ACCOUNT" --cpu 2 --memory 2Gi --task-timeout 3600 --max-retries 1 --parallelism 1 --tasks 1 \
  --set-env-vars ENVIRONMENT=production,GCP_PROJECT_ID="$PROJECT_ID",GCP_REGION="$REGION" \
  --set-secrets SUPABASE_URL=SUPABASE_URL:latest,SUPABASE_SERVICE_ROLE_KEY=SUPABASE_SECRET_KEY:latest,KIMI_API_KEY=KIMI_API_KEY:latest

gcloud run jobs add-iam-policy-binding "$WORKER_JOB" --project "$PROJECT_ID" --region "$REGION" \
  --member "serviceAccount:$SERVICE_ACCOUNT" --role roles/run.jobsExecutorWithOverrides >/dev/null

gcloud run deploy "$API_SERVICE" --project "$PROJECT_ID" --region "$REGION" --image "$API_IMAGE" \
  --service-account "$SERVICE_ACCOUNT" --no-allow-unauthenticated --port 8080 --cpu 1 --memory 1Gi --timeout 300 --max-instances 10 \
  --set-env-vars "^${ENV_VAR_DELIMITER}^ENVIRONMENT=production${ENV_VAR_DELIMITER}JOB_LAUNCHER=cloud_run${ENV_VAR_DELIMITER}GCP_PROJECT_ID=$PROJECT_ID${ENV_VAR_DELIMITER}GCP_REGION=$REGION${ENV_VAR_DELIMITER}CLOUD_RUN_WORKER_JOB=$WORKER_JOB${ENV_VAR_DELIMITER}ALLOWED_CORS_ORIGINS=$ALLOWED_CORS_ORIGINS" \
  --set-secrets SUPABASE_URL=SUPABASE_URL:latest,SUPABASE_SERVICE_ROLE_KEY=SUPABASE_SECRET_KEY:latest,KIMI_API_KEY=KIMI_API_KEY:latest

service_url=$(gcloud run services describe "$API_SERVICE" --project "$PROJECT_ID" --region "$REGION" --format='value(status.url)')
echo "API service URL: $service_url"
echo "Deployment complete. Worker job was deployed but not executed."

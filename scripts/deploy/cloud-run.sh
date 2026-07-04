#!/usr/bin/env bash
set -euo pipefail
PROJECT_ID=${PROJECT_ID:-big-cabinet-457321-t7}
REGION=${REGION:-us-central1}
REPOSITORY=${REPOSITORY:-milo-agent}
SERVICE_ACCOUNT=${SERVICE_ACCOUNT:-id-kimi-agent-runner@big-cabinet-457321-t7.iam.gserviceaccount.com}
API_IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/$REPOSITORY/api:$(git rev-parse --short HEAD)"
WORKER_IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/$REPOSITORY/worker:$(git rev-parse --short HEAD)"

gcloud artifacts repositories describe "$REPOSITORY" --location "$REGION" --project "$PROJECT_ID" >/dev/null || \
  gcloud artifacts repositories create "$REPOSITORY" --repository-format=docker --location "$REGION" --project "$PROJECT_ID"

gcloud builds submit --project "$PROJECT_ID" --tag "$API_IMAGE" -f Dockerfile.api .
gcloud builds submit --project "$PROJECT_ID" --tag "$WORKER_IMAGE" -f Dockerfile.worker .

gcloud run deploy milo-agent-api --project "$PROJECT_ID" --region "$REGION" --image "$API_IMAGE" \
  --service-account "$SERVICE_ACCOUNT" --no-allow-unauthenticated --port 8080 --cpu 1 --memory 1Gi --timeout 300 --max-instances 10 \
  --set-env-vars ENVIRONMENT=production,JOB_LAUNCHER=cloud_run,GCP_PROJECT_ID="$PROJECT_ID",GCP_REGION="$REGION",CLOUD_RUN_WORKER_JOB=milo-agent-worker \
  --set-secrets SUPABASE_URL=SUPABASE_URL:latest,SUPABASE_SERVICE_ROLE_KEY=SUPABASE_SECRET_KEY:latest,KIMI_API_KEY=KIMI_API_KEY:latest

gcloud run jobs deploy milo-agent-worker --project "$PROJECT_ID" --region "$REGION" --image "$WORKER_IMAGE" \
  --service-account "$SERVICE_ACCOUNT" --cpu 2 --memory 2Gi --task-timeout 3600 --max-retries 1 --parallelism 1 --tasks 1 \
  --set-env-vars ENVIRONMENT=production,GCP_PROJECT_ID="$PROJECT_ID",GCP_REGION="$REGION" \
  --set-secrets SUPABASE_URL=SUPABASE_URL:latest,SUPABASE_SERVICE_ROLE_KEY=SUPABASE_SECRET_KEY:latest,KIMI_API_KEY=KIMI_API_KEY:latest

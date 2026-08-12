#!/usr/bin/env bash
# Stage C step 4: create the two DISPOSABLE probe jobs (deleted in step 7).
#   stagec-db-probe — runs as the API runtime SA; binds only the two
#     Supabase secrets that identity already accesses (no new IAM grants).
#   stagec-gw-probe — runs as the approved gateway SA; NO secrets.
# The probe sources are shipped via an env var and executed with python3 -c;
# neither job image is custom.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
# shellcheck source=stage-c-env.sh
source ./stage-c-env.sh

DB_SOURCE="$(cat probe_db.py)"
GW_SOURCE="$(cat probe_gateway.py)"

echo "== Creating ${STAGE_C_DB_PROBE_JOB} (SA: ${STAGE_C_API_SA})"
gcloud run jobs create "${STAGE_C_DB_PROBE_JOB}" \
  --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" \
  --image=python:3.12-slim \
  --service-account="${STAGE_C_API_SA}" \
  --max-retries=0 --task-timeout=600 \
  --set-secrets="SUPABASE_URL=SUPABASE_URL:latest,SUPABASE_SERVICE_ROLE_KEY=SUPABASE_SECRET_KEY:latest" \
  --set-env-vars="^@^PROBE_SOURCE=${DB_SOURCE}@STAGE_C_MODE=preflight" \
  --command=python3 --args=-c,'import os; exec(os.environ["PROBE_SOURCE"])'

echo "== Creating ${STAGE_C_GW_PROBE_JOB} (SA: ${STAGE_C_GATEWAY_SA}, no secrets)"
gcloud run jobs create "${STAGE_C_GW_PROBE_JOB}" \
  --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" \
  --image=python:3.12-slim \
  --service-account="${STAGE_C_GATEWAY_SA}" \
  --max-retries=0 --task-timeout=3600 \
  --set-env-vars="^@^PROBE_SOURCE=${GW_SOURCE}@STAGE_C_MODE=create@STAGE_C_API_URL=${STAGE_C_API_URL}@STAGE_C_USER_ID=pending@STAGE_C_CONVERSATION_ID=pending" \
  --command=python3 --args=-c,'import os; exec(os.environ["PROBE_SOURCE"])'

echo "OK: probe jobs created (disposable; deleted by 07-post-smoke-posture.sh)."

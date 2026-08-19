#!/usr/bin/env bash
# Stage C step 4: create the two DISPOSABLE probe jobs (deleted in step 7).
#   stagec-db-probe — runs as the API runtime SA; binds only the two
#     Supabase secrets that identity already accesses (no new IAM grants).
#   stagec-gw-probe — runs as the approved gateway SA; NO secrets.
# The probe sources are shipped byte-for-byte via an env var and executed
# with python3 -c; neither job image is custom.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
# shellcheck source=stage-c-env.sh
source ./stage-c-env.sh

# $(cat) would strip trailing newlines; the sentinel keeps the transported
# source byte-for-byte identical to the file on disk.
DB_SOURCE="$(cat probe_db.py; printf x)"; DB_SOURCE="${DB_SOURCE%x}"
GW_SOURCE="$(cat probe_gateway.py; printf x)"; GW_SOURCE="${GW_SOURCE%x}"

# gcloud env-var dict delimiter (the ^DELIM^ prefix form). It MUST NOT
# occur anywhere in a transported value: the raw Python sources ride
# inside PROBE_SOURCE, and a collision makes gcloud split the source
# mid-string. Stage C attempt 1 failed exactly this way — the previous
# '^@^' delimiter split probe_db.py at the '@' in stage-c-smoke@invalid.milo
# and the deployed probe died with "SyntaxError: unterminated string
# literal" at TEST_EMAIL (safely, before any run existed). Multi-character
# and collision-checked here; fail closed BEFORE any gcloud create.
STAGE_C_ENV_DELIM=":::"
for source_var in DB_SOURCE GW_SOURCE; do
  if [[ "${!source_var}" == *"${STAGE_C_ENV_DELIM}"* ]]; then
    echo "STAGE C REFUSED: ${source_var} contains the env-var delimiter '${STAGE_C_ENV_DELIM}' — the probe source would be split in transport; choose a different delimiter" >&2
    exit 1
  fi
done

echo "== Creating ${STAGE_C_DB_PROBE_JOB} (SA: ${STAGE_C_API_SA})"
gcloud run jobs create "${STAGE_C_DB_PROBE_JOB}" \
  --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" \
  --image=python:3.12-slim \
  --service-account="${STAGE_C_API_SA}" \
  --max-retries=0 --task-timeout=600 \
  --set-secrets="SUPABASE_URL=SUPABASE_URL:latest,SUPABASE_SERVICE_ROLE_KEY=SUPABASE_SECRET_KEY:latest" \
  --set-env-vars="^${STAGE_C_ENV_DELIM}^PROBE_SOURCE=${DB_SOURCE}${STAGE_C_ENV_DELIM}STAGE_C_MODE=preflight${STAGE_C_ENV_DELIM}STAGE_C_EXPECTED_PRIOR_RUNS=${STAGE_C_EXPECTED_PRIOR_RUNS}${STAGE_C_ENV_DELIM}STAGE_C_IDEMPOTENCY_KEY=${STAGE_C_IDEMPOTENCY_KEY}" \
  --command=python3 --args=-c,'import os; exec(os.environ["PROBE_SOURCE"])'

echo "== Creating ${STAGE_C_GW_PROBE_JOB} (SA: ${STAGE_C_GATEWAY_SA}, no secrets)"
gcloud run jobs create "${STAGE_C_GW_PROBE_JOB}" \
  --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" \
  --image=python:3.12-slim \
  --service-account="${STAGE_C_GATEWAY_SA}" \
  --max-retries=0 --task-timeout=3600 \
  --set-env-vars="^${STAGE_C_ENV_DELIM}^PROBE_SOURCE=${GW_SOURCE}${STAGE_C_ENV_DELIM}STAGE_C_MODE=create${STAGE_C_ENV_DELIM}STAGE_C_API_URL=${STAGE_C_API_URL}${STAGE_C_ENV_DELIM}STAGE_C_USER_ID=pending${STAGE_C_ENV_DELIM}STAGE_C_CONVERSATION_ID=pending" \
  --command=python3 --args=-c,'import os; exec(os.environ["PROBE_SOURCE"])'

echo "OK: probe jobs created (disposable; deleted by 07-post-smoke-posture.sh)."

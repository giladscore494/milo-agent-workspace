#!/usr/bin/env bash
# Stage C step 4: create the two DISPOSABLE probe jobs (deleted in step 7
# / recovery cleanup).
#   stagec-db-probe — runs as the API runtime SA; binds only the two
#     Supabase secrets that identity already accesses (no new IAM grants).
#   stagec-gw-probe — runs as the approved gateway SA; NO secrets.
# The probe sources are transported as DETERMINISTIC gzip+base64
# (PROBE_SOURCE_GZIP_B64) and reconstructed to the exact original UTF-8
# bytes inside python:3.12-slim before exec. Raw transport is no longer
# used: Cloud Run caps a single env value at 32,768 characters and
# probe_db.py raw is >35k — evidence recovery attempt 1 failed safely on
# exactly that limit. Neither job image is custom.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
# shellcheck source=stage-c-env.sh
source ./stage-c-env.sh

# Cloud Run rejects a single env value longer than 32,768 characters.
CLOUD_RUN_ENV_VALUE_MAX=32768

# Deterministic compressed transport: gzip with a pinned zero mtime and
# no filename (identical source bytes always yield the identical value),
# then unwrapped base64. The encoder round-trips its own output before
# printing anything, so a corrupt encoding can never be shipped.
encode_probe_source() { # file -> stdout: base64(gzip(bytes)), no newline
  python3 -c '
import base64, gzip, sys
with open(sys.argv[1], "rb") as fh:
    raw = fh.read()
encoded = base64.b64encode(gzip.compress(raw, compresslevel=9, mtime=0)).decode("ascii")
if gzip.decompress(base64.b64decode(encoded)) != raw:
    raise SystemExit("round-trip mismatch for " + sys.argv[1])
sys.stdout.write(encoded)
' "$1"
}

DB_SOURCE_GZIP_B64="$(encode_probe_source probe_db.py)"
GW_SOURCE_GZIP_B64="$(encode_probe_source probe_gateway.py)"

# In-container bootstrap: reconstruct the EXACT UTF-8 bytes, then exec.
# Deliberately comma-free (gcloud --args splits on commas) and delimiter-free.
PROBE_BOOTSTRAP='import base64;import gzip;import os;exec(gzip.decompress(base64.b64decode(os.environ["PROBE_SOURCE_GZIP_B64"])).decode("utf-8"))'

# gcloud env-var dict delimiter (the ^DELIM^ prefix form). It MUST NOT
# occur anywhere in a transported value: a collision makes gcloud split
# the value mid-string (Stage C attempt 1 failed exactly this way when
# the then-raw probe source was split at '@' by a '^@^' delimiter).
# base64 output cannot contain ':' at all, but every transported value is
# still checked — and every value is also checked against the Cloud Run
# per-value size limit. BOTH checks fail closed BEFORE any gcloud
# mutation (evidence recovery attempt 1 discovered the size limit only
# at the gcloud create call; this guard front-runs it).
STAGE_C_ENV_DELIM=":::"

check_transport_value() { # LABEL VALUE
  local label="$1" value="$2"
  if [[ "${value}" == *"${STAGE_C_ENV_DELIM}"* ]]; then
    echo "STAGE C REFUSED: ${label} contains the env-var delimiter '${STAGE_C_ENV_DELIM}' — it would be split in transport; refusing before any gcloud mutation" >&2
    exit 1
  fi
  if [ "${#value}" -gt "${CLOUD_RUN_ENV_VALUE_MAX}" ]; then
    echo "STAGE C REFUSED: ${label} is ${#value} characters — exceeds the Cloud Run env-value limit of ${CLOUD_RUN_ENV_VALUE_MAX}; refusing before any gcloud mutation" >&2
    exit 1
  fi
}

check_transport_value "db PROBE_SOURCE_GZIP_B64" "${DB_SOURCE_GZIP_B64}"
check_transport_value "gw PROBE_SOURCE_GZIP_B64" "${GW_SOURCE_GZIP_B64}"
check_transport_value "STAGE_C_MODE (db)" "preflight"
check_transport_value "STAGE_C_EXPECTED_PRIOR_RUNS" "${STAGE_C_EXPECTED_PRIOR_RUNS}"
check_transport_value "STAGE_C_IDEMPOTENCY_KEY" "${STAGE_C_IDEMPOTENCY_KEY}"
check_transport_value "STAGE_C_MODE (gw)" "create"
check_transport_value "STAGE_C_API_URL" "${STAGE_C_API_URL}"
check_transport_value "STAGE_C_USER_ID" "pending"
check_transport_value "STAGE_C_CONVERSATION_ID" "pending"

echo "== Creating ${STAGE_C_DB_PROBE_JOB} (SA: ${STAGE_C_API_SA})"
gcloud run jobs create "${STAGE_C_DB_PROBE_JOB}" \
  --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" \
  --image=python:3.12-slim \
  --service-account="${STAGE_C_API_SA}" \
  --max-retries=0 --task-timeout=600 \
  --set-secrets="SUPABASE_URL=SUPABASE_URL:latest,SUPABASE_SERVICE_ROLE_KEY=SUPABASE_SECRET_KEY:latest" \
  --set-env-vars="^${STAGE_C_ENV_DELIM}^PROBE_SOURCE_GZIP_B64=${DB_SOURCE_GZIP_B64}${STAGE_C_ENV_DELIM}STAGE_C_MODE=preflight${STAGE_C_ENV_DELIM}STAGE_C_EXPECTED_PRIOR_RUNS=${STAGE_C_EXPECTED_PRIOR_RUNS}${STAGE_C_ENV_DELIM}STAGE_C_IDEMPOTENCY_KEY=${STAGE_C_IDEMPOTENCY_KEY}" \
  --command=python3 --args=-c,"${PROBE_BOOTSTRAP}"

echo "== Creating ${STAGE_C_GW_PROBE_JOB} (SA: ${STAGE_C_GATEWAY_SA}, no secrets)"
gcloud run jobs create "${STAGE_C_GW_PROBE_JOB}" \
  --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" \
  --image=python:3.12-slim \
  --service-account="${STAGE_C_GATEWAY_SA}" \
  --max-retries=0 --task-timeout=3600 \
  --set-env-vars="^${STAGE_C_ENV_DELIM}^PROBE_SOURCE_GZIP_B64=${GW_SOURCE_GZIP_B64}${STAGE_C_ENV_DELIM}STAGE_C_MODE=create${STAGE_C_ENV_DELIM}STAGE_C_API_URL=${STAGE_C_API_URL}${STAGE_C_ENV_DELIM}STAGE_C_USER_ID=pending${STAGE_C_ENV_DELIM}STAGE_C_CONVERSATION_ID=pending" \
  --command=python3 --args=-c,"${PROBE_BOOTSTRAP}"

echo "OK: probe jobs created (disposable; compressed transport; deleted by 07-post-smoke-posture.sh or the recovery cleanup)."

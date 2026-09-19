#!/usr/bin/env bash
# Stage D step 4: create the two DISPOSABLE probe jobs (deleted and proven
# absent by 07-post-run-lockdown.sh).
#
#   stage-d-db-probe — runs as the API runtime SA; binds only the two
#     Supabase secrets that identity already accesses (no new IAM grants).
#   stage-d-gw-probe — runs as the approved gateway SA; NO secrets.
#
# TWO SUPPLY-CHAIN INPUTS ARE PINNED, and both are checked BEFORE any
# gcloud mutation, because the db probe holds SUPABASE_SERVICE_ROLE_KEY
# and therefore executes code with service-role access to production:
#
#   1. THE RUNTIME IMAGE, by digest. Never a tag. `python:3.12-slim` is a
#      mutable upstream tag that Docker Hub re-publishes, so a tag-created
#      job could begin running different code with those credentials
#      between one execution and the next.
#   2. THE PROBE SOURCE, by SHA-256. This script transports whatever
#      probe_db.py / probe_gateway.py are on disk; a dirty or unreviewed
#      checkout would otherwise ship unreviewed privileged code.
#
# After creation both job templates are re-verified against the pinned
# image, service account and secret bindings (verify_probe_jobs.py), and
# every later probe execution re-verifies them again.
#
# The probe sources are transported as DETERMINISTIC gzip+base64
# (PROBE_SOURCE_GZIP_B64) and reconstructed to the exact original UTF-8
# bytes inside the pinned image before exec.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
# shellcheck source=stage-d-env.sh
source ./stage-d-env.sh

# Cloud Run rejects a single env value longer than 32,768 characters.
CLOUD_RUN_ENV_VALUE_MAX=32768

# The one image both probes may run. Digest form only.
PROBE_IMAGE="${STAGE_D_PROBE_IMAGE_REPO}@${STAGE_D_PROBE_IMAGE_DIGEST}"

# -- Gate 1: the probe SOURCE must be exactly what was reviewed. This runs
# before anything else, so an unreviewed checkout never reaches a
# credentialed job.
verify_probe_source() { # FILE EXPECTED_SHA256
  local file="$1" expected="$2" actual
  if [ -z "${expected}" ] || [ "${expected}" = "__PROBE_DB_SHA256__" ] || [ "${expected}" = "__PROBE_GW_SHA256__" ]; then
    echo "STAGE D REFUSED: no reviewed SHA-256 is pinned for ${file} — the privileged probe source is unverified; refusing before any gcloud mutation" >&2
    exit 1
  fi
  actual="$(python3 -c '
import hashlib, sys
with open(sys.argv[1], "rb") as fh:
    print(hashlib.sha256(fh.read()).hexdigest())
' "${file}")"
  if [ "${actual}" != "${expected}" ]; then
    echo "STAGE D REFUSED: ${file} has SHA-256 ${actual} but the reviewed pin is ${expected} — the probe source on disk is NOT the reviewed source (dirty checkout, bad merge or tampering); refusing before any gcloud mutation" >&2
    exit 1
  fi
  echo "OK: ${file} matches its reviewed SHA-256 ${expected}"
}

echo "== Probe source integrity (before any mutation)"
verify_probe_source probe_db.py "${STAGE_D_PROBE_DB_SHA256}"
verify_probe_source probe_gateway.py "${STAGE_D_PROBE_GW_SHA256}"

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
# the value mid-string. base64 output cannot contain ':' at all, but every
# transported value is still checked — and every value is also checked
# against the Cloud Run per-value size limit. BOTH checks fail closed
# BEFORE any gcloud mutation.
STAGE_D_ENV_DELIM=":::"

check_transport_value() { # LABEL VALUE
  local label="$1" value="$2"
  if [[ "${value}" == *"${STAGE_D_ENV_DELIM}"* ]]; then
    echo "STAGE D REFUSED: ${label} contains the env-var delimiter '${STAGE_D_ENV_DELIM}' — it would be split in transport; refusing before any gcloud mutation" >&2
    exit 1
  fi
  if [ "${#value}" -gt "${CLOUD_RUN_ENV_VALUE_MAX}" ]; then
    echo "STAGE D REFUSED: ${label} is ${#value} characters — exceeds the Cloud Run env-value limit of ${CLOUD_RUN_ENV_VALUE_MAX}; refusing before any gcloud mutation" >&2
    exit 1
  fi
}

check_transport_value "db PROBE_SOURCE_GZIP_B64" "${DB_SOURCE_GZIP_B64}"
check_transport_value "gw PROBE_SOURCE_GZIP_B64" "${GW_SOURCE_GZIP_B64}"
check_transport_value "STAGE_D_MODE (db)" "preflight"
check_transport_value "STAGE_D_EXPECTED_PRIOR_RUNS" "${STAGE_D_EXPECTED_PRIOR_RUNS}"
check_transport_value "STAGE_D_IDEMPOTENCY_KEY" "${STAGE_D_IDEMPOTENCY_KEY}"
check_transport_value "STAGE_D_GOV_CAPTURE_RUN_ID" "${STAGE_D_GOV_CAPTURE_RUN_ID}"
check_transport_value "STAGE_D_GOV_CAPTURE_KEY" "${STAGE_D_GOV_CAPTURE_KEY}"
check_transport_value "STAGE_D_PROJECT_SLUG" "${STAGE_D_PROJECT_SLUG}"
check_transport_value "STAGE_D_TEST_EMAIL" "${STAGE_D_TEST_EMAIL}"
check_transport_value "STAGE_D_WORKFLOW_KEY" "${STAGE_D_WORKFLOW_KEY}"
check_transport_value "STAGE_D_FORBIDDEN_PROJECT_IDS" "${STAGE_D_FORBIDDEN_PROJECT_IDS}"
check_transport_value "STAGE_D_MODE (gw)" "create"
check_transport_value "STAGE_D_API_URL" "${STAGE_D_API_URL}"
check_transport_value "STAGE_D_USER_ID" "pending"
check_transport_value "STAGE_D_CONVERSATION_ID" "pending"

echo "== Creating ${STAGE_D_DB_PROBE_JOB} (SA: ${STAGE_D_API_SA}, image pinned by digest)"
gcloud run jobs create "${STAGE_D_DB_PROBE_JOB}" \
  --project="${STAGE_D_PROJECT}" --region="${STAGE_D_REGION}" \
  --image="${PROBE_IMAGE}" \
  --service-account="${STAGE_D_API_SA}" \
  --max-retries=0 --task-timeout=600 \
  --set-secrets="SUPABASE_URL=SUPABASE_URL:latest,SUPABASE_SERVICE_ROLE_KEY=SUPABASE_SECRET_KEY:latest" \
  --set-env-vars="^${STAGE_D_ENV_DELIM}^PROBE_SOURCE_GZIP_B64=${DB_SOURCE_GZIP_B64}${STAGE_D_ENV_DELIM}STAGE_D_MODE=preflight${STAGE_D_ENV_DELIM}STAGE_D_EXPECTED_PRIOR_RUNS=${STAGE_D_EXPECTED_PRIOR_RUNS}${STAGE_D_ENV_DELIM}STAGE_D_IDEMPOTENCY_KEY=${STAGE_D_IDEMPOTENCY_KEY}${STAGE_D_ENV_DELIM}STAGE_D_GOV_CAPTURE_RUN_ID=${STAGE_D_GOV_CAPTURE_RUN_ID}${STAGE_D_ENV_DELIM}STAGE_D_GOV_CAPTURE_KEY=${STAGE_D_GOV_CAPTURE_KEY}${STAGE_D_ENV_DELIM}STAGE_D_PROJECT_SLUG=${STAGE_D_PROJECT_SLUG}${STAGE_D_ENV_DELIM}STAGE_D_TEST_EMAIL=${STAGE_D_TEST_EMAIL}${STAGE_D_ENV_DELIM}STAGE_D_WORKFLOW_KEY=${STAGE_D_WORKFLOW_KEY}${STAGE_D_ENV_DELIM}STAGE_D_FORBIDDEN_PROJECT_IDS=${STAGE_D_FORBIDDEN_PROJECT_IDS}" \
  --command=python3 --args=-c,"${PROBE_BOOTSTRAP}"

echo "== Creating ${STAGE_D_GW_PROBE_JOB} (SA: ${STAGE_D_GATEWAY_SA}, no secrets, image pinned by digest)"
gcloud run jobs create "${STAGE_D_GW_PROBE_JOB}" \
  --project="${STAGE_D_PROJECT}" --region="${STAGE_D_REGION}" \
  --image="${PROBE_IMAGE}" \
  --service-account="${STAGE_D_GATEWAY_SA}" \
  --max-retries=0 --task-timeout=3600 \
  --set-env-vars="^${STAGE_D_ENV_DELIM}^PROBE_SOURCE_GZIP_B64=${GW_SOURCE_GZIP_B64}${STAGE_D_ENV_DELIM}STAGE_D_MODE=create${STAGE_D_ENV_DELIM}STAGE_D_API_URL=${STAGE_D_API_URL}${STAGE_D_ENV_DELIM}STAGE_D_USER_ID=pending${STAGE_D_ENV_DELIM}STAGE_D_CONVERSATION_ID=pending${STAGE_D_ENV_DELIM}STAGE_D_IDEMPOTENCY_KEY=${STAGE_D_IDEMPOTENCY_KEY}${STAGE_D_ENV_DELIM}STAGE_D_GOV_CAPTURE_RUN_ID=${STAGE_D_GOV_CAPTURE_RUN_ID}${STAGE_D_ENV_DELIM}STAGE_D_GOV_CAPTURE_KEY=${STAGE_D_GOV_CAPTURE_KEY}${STAGE_D_ENV_DELIM}STAGE_D_TEST_EMAIL=${STAGE_D_TEST_EMAIL}" \
  --command=python3 --args=-c,"${PROBE_BOOTSTRAP}"

echo "== Post-creation verification: pinned image digest, identity and secret bindings"
db_json="$(mktemp)"; gw_json="$(mktemp)"
trap 'rm -f "${db_json}" "${gw_json}"' EXIT
gcloud run jobs describe "${STAGE_D_DB_PROBE_JOB}" \
  --project="${STAGE_D_PROJECT}" --region="${STAGE_D_REGION}" --format=json > "${db_json}"
gcloud run jobs describe "${STAGE_D_GW_PROBE_JOB}" \
  --project="${STAGE_D_PROJECT}" --region="${STAGE_D_REGION}" --format=json > "${gw_json}"
python3 ./verify_probe_jobs.py --db-json "${db_json}" --gw-json "${gw_json}" \
  || { echo "STAGE D REFUSED: the created probe jobs are not what was reviewed — run ./07-post-run-lockdown.sh to remove them" >&2; exit 1; }

echo "OK: probe jobs created from the pinned digest ${PROBE_IMAGE}, sources hash-verified, templates verified."

#!/usr/bin/env bash
# Stage C step 3b: READ-ONLY verification of the enabled Stage C posture
# (run after the manual commands in 03-enable-stage-c.md). Asserts the
# minimum surface and nothing more. Mutates nothing.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
# shellcheck source=stage-c-env.sh
source ./stage-c-env.sh

echo "== Provider secret IAM (worker-only)"
gcloud secrets get-iam-policy KIMI_API_KEY --project="${STAGE_C_PROJECT}" --format=json \
  | python3 -c 'import json,sys; p=json.load(sys.stdin); a=[m for b in p["bindings"] if b["role"]=="roles/secretmanager.secretAccessor" for m in b["members"]]; assert a==["serviceAccount:'"${STAGE_C_WORKER_SA}"'"], a; print("OK: worker-only")'

echo "== Exact cap/image/flag/provider-limit posture on BOTH surfaces (verify_caps.py)"
worker_json="$(mktemp)"; api_json="$(mktemp)"
trap 'rm -f "${worker_json}" "${api_json}"' EXIT
gcloud run jobs describe "${STAGE_C_WORKER_JOB}" \
  --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" --format=json > "${worker_json}"
gcloud run services describe "${STAGE_C_API_SERVICE}" \
  --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" --format=json > "${api_json}"
# Every cap in STAGE_C_CAPS compared for exact equality on worker AND API;
# every provider limit in STAGE_C_WORKER_PROVIDER_LIMITS exact on the
# Worker ONLY (any MILO_PROVIDER_* on the API fails); also verifies the
# pinned release images and the full flag/provider-secret posture.
python3 ./verify_caps.py --worker-json "${worker_json}" --api-json "${api_json}"

echo "== Exact historical Worker-execution baseline (read-only)"
# Attempt 7 preparation: exactly the pinned prior terminal executions
# (Attempts 5+6) must exist and NONE may be active/unverifiable before
# any run is created. Historical terminal executions are never cancelled.
gcloud run jobs executions list --job="${STAGE_C_WORKER_JOB}" \
  --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" --format=json \
  | python3 ./verify_executions.py --expected-total "${STAGE_C_EXPECTED_PRIOR_EXECUTIONS}"

echo "Stage C posture verified. Proceed to 04-create-probes.sh."

#!/usr/bin/env bash
# Stage D step 3b: READ-ONLY verification of the enabled Stage D posture
# (run after the manual commands in 03-enable-stage-d.md). Asserts the
# minimum surface and nothing more. Mutates nothing.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
# shellcheck source=stage-d-env.sh
source ./stage-d-env.sh

echo "== Provider secret IAM (worker-only)"
gcloud secrets get-iam-policy KIMI_API_KEY --project="${STAGE_D_PROJECT}" --format=json \
  | python3 -c 'import json,sys; p=json.load(sys.stdin); a=[m for b in p["bindings"] if b["role"]=="roles/secretmanager.secretAccessor" for m in b["members"]]; assert a==["serviceAccount:'"${STAGE_D_WORKER_SA}"'"], a; print("OK: worker-only")'

echo "== The runtime policy is the reviewed one AND is identical to the release's"
python3 ./policy_envelope.py binding

echo "== Exact cap/image/flag/provider-envelope posture on BOTH surfaces (verify_caps.py)"
worker_json="$(mktemp)"; api_json="$(mktemp)"
trap 'rm -f "${worker_json}" "${api_json}"' EXIT
gcloud run jobs describe "${STAGE_D_WORKER_JOB}" \
  --project="${STAGE_D_PROJECT}" --region="${STAGE_D_REGION}" --format=json > "${worker_json}"
gcloud run services describe "${STAGE_D_API_SERVICE}" \
  --project="${STAGE_D_PROJECT}" --region="${STAGE_D_REGION}" --format=json > "${api_json}"
# Every cap in STAGE_D_CAPS compared for exact equality on worker AND API;
# every provider limit in STAGE_D_WORKER_PROVIDER_LIMITS exact on the
# Worker ONLY (any MILO_PROVIDER_* on the API fails, and the live
# MILO_PROVIDER_MAX_CONCURRENCY=8 drift fails until it is restored to 2);
# also verifies the pinned release images and the full flag/provider-secret
# posture, including MILO_ENABLE_CATALOG_EXECUTION=false on both surfaces.
python3 ./verify_caps.py --worker-json "${worker_json}" --api-json "${api_json}"

echo "== Exact visible Worker-execution baseline (read-only)"
# Exactly the pinned VISIBLE prior terminal executions must exist and NONE
# may be active/unverifiable before any run is created. This read-only
# check never cancels or deletes an execution.
gcloud run jobs executions list --job="${STAGE_D_WORKER_JOB}" \
  --project="${STAGE_D_PROJECT}" --region="${STAGE_D_REGION}" --format=json \
  | python3 ./verify_executions.py --expected-total "${STAGE_D_EXPECTED_PRIOR_EXECUTIONS}"

echo "Stage D posture verified. Proceed to 04-create-probes.sh."

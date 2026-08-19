#!/usr/bin/env bash
# Stage C step 2: deploy the release images. FLAGS STAY OFF — this step
# changes images only (worker job before API, per DEPLOYMENT.md), using
# non-destructive update forms. Verify the API revision becomes Ready and
# execution surfaces still refuse before proceeding.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
# shellcheck source=stage-c-env.sh
source ./stage-c-env.sh

echo "== Worker job image -> ${STAGE_C_RELEASE_SHA}"
gcloud run jobs update "${STAGE_C_WORKER_JOB}" \
  --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" \
  --image="${STAGE_C_REGISTRY}/worker:${STAGE_C_RELEASE_SHA}"

echo "== API service image -> ${STAGE_C_RELEASE_SHA}"
gcloud run services update "${STAGE_C_API_SERVICE}" \
  --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" \
  --image="${STAGE_C_REGISTRY}/api:${STAGE_C_RELEASE_SHA}"

echo "== Post-deploy verification"
ready="$(gcloud run services describe "${STAGE_C_API_SERVICE}" \
  --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" \
  --format='value(status.conditions[0].status)')"
test "${ready}" = "True" || { echo "BLOCKED: API service not Ready"; exit 1; }

# Exact historical execution baseline: exactly the pinned prior terminal
# executions (Attempts 5+6), none active/unverifiable. Deploying with a
# different count, or with anything still running, fails closed. The
# historical terminal executions are evidence and stay untouched.
gcloud run jobs executions list --job="${STAGE_C_WORKER_JOB}" \
  --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" --format=json \
  | python3 ./verify_executions.py --expected-total "${STAGE_C_EXPECTED_PRIOR_EXECUTIONS}" \
  || { echo "BLOCKED: worker execution posture does not match the pinned historical baseline (${STAGE_C_EXPECTED_PRIOR_EXECUTIONS} terminal, 0 active)"; exit 1; }

flags="$(gcloud run services describe "${STAGE_C_API_SERVICE}" \
  --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" --format=json \
  | python3 -c 'import json,sys; env={e["name"]:e.get("value") for e in json.load(sys.stdin)["spec"]["template"]["spec"]["containers"][0]["env"] if "value" in e}; print(env.get("MILO_ENABLE_RUN_CREATION"), env.get("MILO_ENABLE_PAID_EXECUTION"), env.get("JOB_LAUNCHER"))')"
test "${flags}" = "false false disabled" || { echo "BLOCKED: flags changed unexpectedly: ${flags}"; exit 1; }

echo "OK: release images deployed, execution still fully disabled, execution baseline matches (${STAGE_C_EXPECTED_PRIOR_EXECUTIONS} historical terminal, 0 active)."

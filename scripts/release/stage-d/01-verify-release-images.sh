#!/usr/bin/env bash
# Stage D step 1: READ-ONLY verification that production still serves the
# EXACT accepted release images, by DIGEST.
#
# This step replaces the build and deploy steps a smoke-run toolkit would
# normally carry. Stage D deliberately has neither.
#
# Why there is no rebuild here
# ----------------------------
# Rebuilding this release is not byte-reproducible: Dockerfile.api and
# Dockerfile.worker start FROM the mutable base tag `python:3.12-slim`,
# backend/requirements.txt carries the unpinned floor `openai>=1.30.0`,
# and there is no lockfile or --require-hashes. A rebuild of commit
# 84cd8696… can therefore produce different bytes, and pushing them under
# the same `:<sha>` Artifact Registry tag would REPLACE the accepted image
# while every tag-based check still reported success. "Re-proving" a
# release by rebuilding it is not a proof; it is a silent new release.
#
# So this step reads and compares. It never builds, never pushes, never
# deploys and never moves a tag. A digest mismatch BLOCKS Stage D and
# requires a separate reviewed release — it is never auto-repaired here.
#
# Mutates nothing.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
# shellcheck source=stage-d-env.sh
source ./stage-d-env.sh

registry_json="$(mktemp)"; api_service_json="$(mktemp)"
api_revision_json="$(mktemp)"; worker_job_json="$(mktemp)"
trap 'rm -f "${registry_json}" "${api_service_json}" "${api_revision_json}" "${worker_job_json}"' EXIT

echo "== Artifact Registry: what the release tag resolves to right now"
gcloud artifacts docker images list "${STAGE_D_REGISTRY}" \
  --project="${STAGE_D_PROJECT}" --include-tags \
  --filter="tags:${STAGE_D_RELEASE_SHA}" --format=json > "${registry_json}"

echo "== API service + its SERVING revision (immutable; carries the resolved digest)"
gcloud run services describe "${STAGE_D_API_SERVICE}" \
  --project="${STAGE_D_PROJECT}" --region="${STAGE_D_REGION}" --format=json > "${api_service_json}"
ready_revision="$(python3 -c '
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    status = json.load(fh).get("status") or {}
name = status.get("latestReadyRevisionName") or ""
if not name:
    raise SystemExit("the API service has no latest ready revision")
print(name)
' "${api_service_json}")"
gcloud run revisions describe "${ready_revision}" \
  --project="${STAGE_D_PROJECT}" --region="${STAGE_D_REGION}" --format=json > "${api_revision_json}"

echo "== Worker job template"
gcloud run jobs describe "${STAGE_D_WORKER_JOB}" \
  --project="${STAGE_D_PROJECT}" --region="${STAGE_D_REGION}" --format=json > "${worker_job_json}"

echo "== Digest comparison against the pinned accepted release"
python3 ./verify_images.py \
  --registry-json "${registry_json}" \
  --api-service-json "${api_service_json}" \
  --api-revision-json "${api_revision_json}" \
  --worker-job-json "${worker_job_json}"

echo
echo "OK: production serves the accepted release digests."
echo "  api    ${STAGE_D_API_IMAGE_DIGEST}"
echo "  worker ${STAGE_D_WORKER_IMAGE_DIGEST}"
echo "Nothing was built, pushed, deployed or re-tagged."
echo "Next: the guarded operator block in 02-guarded-run.md."

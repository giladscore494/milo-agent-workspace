#!/usr/bin/env bash
# Stage D step 1: build the release images at the exact pinned SHA.
# Registry-only mutation; no runtime change. Manual operator execution only.
#
# As of the 2026-09-18 read-only discovery both production surfaces ALREADY
# serve `…/worker:84cd8696…` and `…/api:84cd8696…`, so this step and step 2
# are expected to be idempotent no-ops that re-prove the pin. Run them
# anyway: the proof is the point, and a drifted registry must be caught
# here rather than by verify_caps.py after a flag has been enabled.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
# shellcheck source=stage-d-env.sh
source ./stage-d-env.sh

config="$(mktemp)"
trap 'rm -f "${config}"' EXIT
cat > "${config}" <<YAML
timeout: 1800s
steps:
  - id: clone
    name: gcr.io/cloud-builders/git
    args: ["clone", "${STAGE_D_REPO_URL}", "/workspace/src"]
  - id: checkout
    name: gcr.io/cloud-builders/git
    dir: /workspace/src
    args: ["checkout", "${STAGE_D_RELEASE_SHA}"]
  - id: verify-sha
    name: gcr.io/cloud-builders/git
    dir: /workspace/src
    entrypoint: bash
    args: ["-c", "test \"\$(git rev-parse HEAD)\" = \"${STAGE_D_RELEASE_SHA}\" && echo SHA_VERIFIED"]
  - id: build-api
    name: gcr.io/cloud-builders/docker
    dir: /workspace/src
    args: ["build", "--file", "Dockerfile.api", "--tag", "${STAGE_D_REGISTRY}/api:${STAGE_D_RELEASE_SHA}", "."]
  - id: build-worker
    name: gcr.io/cloud-builders/docker
    dir: /workspace/src
    args: ["build", "--file", "Dockerfile.worker", "--tag", "${STAGE_D_REGISTRY}/worker:${STAGE_D_RELEASE_SHA}", "."]
images:
  - ${STAGE_D_REGISTRY}/api:${STAGE_D_RELEASE_SHA}
  - ${STAGE_D_REGISTRY}/worker:${STAGE_D_RELEASE_SHA}
YAML

gcloud builds submit --no-source \
  --config="${config}" \
  --project="${STAGE_D_PROJECT}" --region="${STAGE_D_REGION}"

echo "Built and pushed:"
echo "  ${STAGE_D_REGISTRY}/api:${STAGE_D_RELEASE_SHA}"
echo "  ${STAGE_D_REGISTRY}/worker:${STAGE_D_RELEASE_SHA}"

#!/usr/bin/env bash
# Stage C step 1: build the release images at the exact signed-off SHA.
# Registry-only mutation; no runtime change. Manual operator execution only.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
# shellcheck source=stage-c-env.sh
source ./stage-c-env.sh

config="$(mktemp)"
trap 'rm -f "${config}"' EXIT
cat > "${config}" <<YAML
timeout: 1800s
steps:
  - id: clone
    name: gcr.io/cloud-builders/git
    args: ["clone", "${STAGE_C_REPO_URL}", "/workspace/src"]
  - id: checkout
    name: gcr.io/cloud-builders/git
    dir: /workspace/src
    args: ["checkout", "${STAGE_C_RELEASE_SHA}"]
  - id: verify-sha
    name: gcr.io/cloud-builders/git
    dir: /workspace/src
    entrypoint: bash
    args: ["-c", "test \"\$(git rev-parse HEAD)\" = \"${STAGE_C_RELEASE_SHA}\" && echo SHA_VERIFIED"]
  - id: build-api
    name: gcr.io/cloud-builders/docker
    dir: /workspace/src
    args: ["build", "--file", "Dockerfile.api", "--tag", "${STAGE_C_REGISTRY}/api:${STAGE_C_RELEASE_SHA}", "."]
  - id: build-worker
    name: gcr.io/cloud-builders/docker
    dir: /workspace/src
    args: ["build", "--file", "Dockerfile.worker", "--tag", "${STAGE_C_REGISTRY}/worker:${STAGE_C_RELEASE_SHA}", "."]
images:
  - ${STAGE_C_REGISTRY}/api:${STAGE_C_RELEASE_SHA}
  - ${STAGE_C_REGISTRY}/worker:${STAGE_C_RELEASE_SHA}
YAML

gcloud builds submit --no-source \
  --config="${config}" \
  --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}"

echo "Built and pushed:"
echo "  ${STAGE_C_REGISTRY}/api:${STAGE_C_RELEASE_SHA}"
echo "  ${STAGE_C_REGISTRY}/worker:${STAGE_C_RELEASE_SHA}"

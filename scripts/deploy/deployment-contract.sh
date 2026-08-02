#!/usr/bin/env bash
# shellcheck disable=SC2034  # every value here is consumed by the sourcing tool
# Canonical deployment contract — the single source of truth shared by every
# deployment tool in this repository:
#
#   scripts/deploy/cloud-run.sh              (executable deployment)
#   scripts/release/generate-deployment-plan.sh  (operator command plan)
#
# This file is SOURCED, never executed. It deploys nothing, contacts nothing
# and contains no secret values — only names, image repository paths and the
# Stage A posture. Both tools must emit the same image paths and the same
# variable/secret bindings; tests/test_deployment_tool_alignment.py fails if
# they ever drift.

# ---------------------------------------------------------------------------
# Image identity
# ---------------------------------------------------------------------------
# Artifact Registry repository paths for the two images. These are the
# EXISTING production paths (…/<ARTIFACT_REGISTRY_REPOSITORY>/api and
# …/<ARTIFACT_REGISTRY_REPOSITORY>/worker). Changing either value is an
# image-repository migration: it orphans every previously pushed image and
# every rollback target, so it must be a deliberate, documented change — not
# a side effect of editing one tool.
MILO_API_IMAGE_REPO="api"
MILO_WORKER_IMAGE_REPO="worker"

# ---------------------------------------------------------------------------
# Stage A posture
# ---------------------------------------------------------------------------
# Every execution flag is pinned OFF by the deployment itself. The list
# mirrors backend/production_config.py EXECUTION_FLAGS. Later stages are
# enabled deliberately by an operator, never as a side effect of a release.
MILO_STAGE_A_EXECUTION_FLAGS=(
  MILO_ENABLE_RUN_CREATION=false
  MILO_ENABLE_PROPOSAL_MUTATIONS=false
  MILO_ENABLE_PROPOSAL_READS=false
  MILO_ENABLE_RUN_CANCELLATION=false
  MILO_ENABLE_EXECUTION_CONTROL=false
  MILO_ENABLE_PAID_EXECUTION=false
)

MILO_STAGE_A_FLAG_NAMES=()
for _milo_flag in "${MILO_STAGE_A_EXECUTION_FLAGS[@]}"; do
  MILO_STAGE_A_FLAG_NAMES+=("${_milo_flag%%=*}")
done
unset _milo_flag

# Provider API keys. At Stage A they are bound to NOTHING — not to the API
# service and not to the worker job. Stage A performs no provider call, so a
# reachable provider credential is pure blast radius: it can only be spent by
# a mistake. The key is introduced by a separate, explicit Stage C operator
# action (docs/production-readiness/STAGED_ACTIVATION.md), together with the
# budget caps and the rehearsed kill switch that make paid execution safe.
# Deployment verification FAILS if either name appears on either resource.
MILO_PROVIDER_KEY_ENV_NAMES=(KIMI_API_KEY MOONSHOT_API_KEY)

# ---------------------------------------------------------------------------
# Required bindings
# ---------------------------------------------------------------------------
# Plain environment variable names that must be present on each resource
# during Stage A, beyond the execution flags above.
#
# MILO_GATEWAY_AUDIENCE and MILO_APPROVED_GATEWAY_IDENTITIES are required
# even though Stage A is execution-disabled: backend/production_config.py
# fails startup in production without them (GATEWAY_AUTH_MISSING), because
# without a verified gateway identity the API would be trusting bare browser
# headers on its read-only routes. They are deployed with approved values,
# never left to whatever happens to be on the service already.
MILO_API_REQUIRED_ENV_NAMES=(
  ENVIRONMENT
  JOB_LAUNCHER
  GCP_PROJECT_ID
  GCP_REGION
  CLOUD_RUN_WORKER_JOB
  ALLOWED_CORS_ORIGINS
  MILO_GATEWAY_AUDIENCE
  MILO_APPROVED_GATEWAY_IDENTITIES
)
MILO_WORKER_REQUIRED_ENV_NAMES=(
  ENVIRONMENT
  GCP_PROJECT_ID
  GCP_REGION
)

# Secret-backed environment variable names. Both tools bind exactly these,
# to the same environment names, at the same version. The Secret Manager
# RESOURCE name behind each is deployment-specific (concrete defaults in
# cloud-run.sh, manifest placeholders in the generated plan), so only the
# environment name and version are shared here.
MILO_API_SECRET_ENV_NAMES=(
  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY
  UPSTASH_REDIS_REST_URL
  UPSTASH_REDIS_REST_TOKEN
)
MILO_WORKER_SECRET_ENV_NAMES=(
  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY
  UPSTASH_REDIS_REST_URL
  UPSTASH_REDIS_REST_TOKEN
)
MILO_SECRET_VERSION="latest"

# gcloud dict flags accept an alternate delimiter via the ^DELIM^ prefix,
# which keeps comma-containing values (CORS origins, identity allowlists)
# intact as a single value.
#
# ';' and not '@': MILO_APPROVED_GATEWAY_IDENTITIES is a list of service
# account emails, so every single one of its values contains an '@'. An '@'
# delimiter would split the allowlist mid-address and deploy garbage. ';'
# appears in neither an email address nor an https origin, and both tools
# quote the argument, so the shell never sees it as a command separator.
MILO_ENV_VAR_DELIMITER=";"

# milo_contains NEEDLE ITEM... — success when NEEDLE is one of the items.
milo_contains() {
  local needle="$1" item
  shift
  for item in "$@"; do
    [[ "$item" == "$needle" ]] && return 0
  done
  return 1
}

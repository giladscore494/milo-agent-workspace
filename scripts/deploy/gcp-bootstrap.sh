#!/usr/bin/env bash
# Idempotent GCP infrastructure bootstrap for the MILO production project.
#
# ONLY infrastructure: API enablement, Artifact Registry, service accounts,
# the IAM bindings those identities need, and empty Secret Manager containers.
# It deploys no code, creates no Cloud Run service or job (cloud-run.sh owns
# those), adds no secret VERSION, and prints no secret value.
#
# Default mode is --plan: it reports what exists and what it would create, and
# changes nothing. --apply performs only the missing operations, so running it
# against an already-provisioned project is a no-op that reports as much.
#
# The human still has to put the secret VALUES in once. This script creates
# the containers so that step is a single `gcloud secrets versions add` per
# secret with no guessing about names or replication policy.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
MILO_REPO_ROOT="$REPO_ROOT"
# shellcheck source=operator-config.sh
source "${SCRIPT_DIR}/operator-config.sh"

MODE="plan" MILO_OPERATOR_CONFIG_PATH=""

usage() {
  cat << 'EOF'
Usage: gcp-bootstrap.sh [--plan|--apply] [options]

Idempotent infrastructure bootstrap. Default --plan changes nothing.

Options:
  --plan                    Report only (default).
  --apply                   Create what is missing. Never deletes or modifies
                            anything that already exists.
  --operator-config <path>  Operator identifier file.
  --help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --plan) MODE="plan"; shift ;;
    --apply) MODE="apply"; shift ;;
    --operator-config) MILO_OPERATOR_CONFIG_PATH="${2:?}"; shift 2 ;;
    --help) usage; exit 0 ;;
    *) printf 'FAIL: unknown argument %s\n' "$1" >&2; exit 2 ;;
  esac
done

CONFIG_PATH="$(milo_operator_config_path "$REPO_ROOT" "$MILO_OPERATOR_CONFIG_PATH")"
milo_load_operator_config "$CONFIG_PATH" || exit 2
milo_require_op GCP_PROJECT_ID GCP_REGION ARTIFACT_REGISTRY_REPOSITORY \
  API_SERVICE_ACCOUNT WORKER_SERVICE_ACCOUNT \
  SECRET_SUPABASE_URL SECRET_SUPABASE_SERVICE_KEY \
  SECRET_REDIS_URL SECRET_REDIS_TOKEN || exit 2

PROJECT_ID="$(milo_op GCP_PROJECT_ID)"
REGION="$(milo_op GCP_REGION)"
milo_require_gcloud_context "$PROJECT_ID" || exit 2

PENDING=0
would() {
  PENDING=$((PENDING + 1))
  printf '  WOULD: %s\n' "$1"
}
did() { printf '  DONE:  %s\n' "$1"; }
have() { printf '  OK:    %s\n' "$1"; }

run_or_plan() {
  local description="$1"
  shift
  if [[ "$MODE" == "apply" ]]; then
    if "$@" > /dev/null 2>&1; then
      did "$description"
    else
      printf '  FAIL:  %s\n' "$description" >&2
      return 1
    fi
  else
    would "$description"
  fi
}

printf 'GCP bootstrap (%s) — project %s, region %s\n\n' "$MODE" "$PROJECT_ID" "$REGION"

printf 'APIs\n'
REQUIRED_APIS=(run.googleapis.com cloudbuild.googleapis.com
               artifactregistry.googleapis.com secretmanager.googleapis.com
               iamcredentials.googleapis.com sts.googleapis.com)
ENABLED="$(gcloud services list --enabled --project "$PROJECT_ID" \
  --format='value(config.name)' 2> /dev/null || true)"
for api in "${REQUIRED_APIS[@]}"; do
  if grep -qx "$api" <<< "$ENABLED"; then
    have "$api enabled"
  else
    run_or_plan "enable $api" gcloud services enable "$api" --project "$PROJECT_ID"
  fi
done

printf '\nArtifact Registry\n'
REPOSITORY="$(milo_op ARTIFACT_REGISTRY_REPOSITORY)"
if gcloud artifacts repositories describe "$REPOSITORY" --location "$REGION" \
     --project "$PROJECT_ID" > /dev/null 2>&1; then
  have "repository $REPOSITORY exists in $REGION"
else
  run_or_plan "create Docker repository $REPOSITORY in $REGION" \
    gcloud artifacts repositories create "$REPOSITORY" --repository-format=docker \
      --location "$REGION" --project "$PROJECT_ID" \
      --description="MILO API and worker images"
fi

printf '\nService accounts\n'
ensure_sa() {
  local email="$1" label="$2" account
  [[ -n "$email" ]] || { printf '  SKIP:  %s not configured\n' "$label"; return 0; }
  account="${email%%@*}"
  if gcloud iam service-accounts describe "$email" --project "$PROJECT_ID" > /dev/null 2>&1; then
    have "$label ($email) exists"
  else
    run_or_plan "create service account $email" \
      gcloud iam service-accounts create "$account" --project "$PROJECT_ID" \
        --display-name "$label"
  fi
}
ensure_sa "$(milo_op API_SERVICE_ACCOUNT)" "MILO API runtime"
ensure_sa "$(milo_op WORKER_SERVICE_ACCOUNT)" "MILO worker runtime"
ensure_sa "$(milo_op CAPTURE_SERVICE_ACCOUNT)" "MILO catalog capture"

printf '\nSecret containers (names only; values are added by a human, once)\n'
ensure_secret() {
  local name="$1"
  [[ -n "$name" ]] || return 0
  if gcloud secrets describe "$name" --project "$PROJECT_ID" > /dev/null 2>&1; then
    local versions
    versions="$(gcloud secrets versions list "$name" --project "$PROJECT_ID" \
      --filter='state:ENABLED' --format='value(name)' 2> /dev/null | wc -l | tr -d ' ')"
    if [[ "${versions:-0}" -gt 0 ]]; then
      have "$name exists with ${versions} enabled version(s)"
    else
      printf '  NEEDS VALUE: %s exists but has no enabled version.\n' "$name"
      printf '               gcloud secrets versions add %s --data-file=- --project=%s\n' \
        "$name" "$PROJECT_ID"
    fi
  else
    run_or_plan "create secret container $name" \
      gcloud secrets create "$name" --replication-policy=automatic --project "$PROJECT_ID"
    printf '  NEEDS VALUE: gcloud secrets versions add %s --data-file=- --project=%s\n' \
      "$name" "$PROJECT_ID"
  fi
}
for key in SECRET_SUPABASE_URL SECRET_SUPABASE_SERVICE_KEY SECRET_REDIS_URL SECRET_REDIS_TOKEN; do
  ensure_secret "$(milo_op "$key")"
done
printf '  NOTE:  %s is created only when paid execution is being armed.\n' \
  "$(milo_op SECRET_PROVIDER_API_KEY)"

printf '\nIAM — secret access\n'
# Least privilege: each identity gets accessor on exactly the secrets it reads.
# The capture identity deliberately gets the Supabase pair and NOT the provider
# key: a capture spends no model money, so a reachable provider credential on
# it would be blast radius with no purpose.
bind_accessor() {
  local secret="$1" member="$2"
  [[ -n "$secret" && -n "$member" ]] || return 0
  if gcloud secrets get-iam-policy "$secret" --project "$PROJECT_ID" --format=json 2> /dev/null \
       | grep -q "serviceAccount:${member}"; then
    have "$member can read $secret"
  else
    run_or_plan "grant secretAccessor on $secret to $member" \
      gcloud secrets add-iam-policy-binding "$secret" \
        --member "serviceAccount:${member}" \
        --role roles/secretmanager.secretAccessor --project "$PROJECT_ID"
  fi
}
for key in SECRET_SUPABASE_URL SECRET_SUPABASE_SERVICE_KEY SECRET_REDIS_URL SECRET_REDIS_TOKEN; do
  bind_accessor "$(milo_op "$key")" "$(milo_op API_SERVICE_ACCOUNT)"
  bind_accessor "$(milo_op "$key")" "$(milo_op WORKER_SERVICE_ACCOUNT)"
done
for key in SECRET_SUPABASE_URL SECRET_SUPABASE_SERVICE_KEY; do
  bind_accessor "$(milo_op "$key")" "$(milo_op CAPTURE_SERVICE_ACCOUNT)"
done

printf '\n'
if [[ "$MODE" == "plan" ]]; then
  if [[ "$PENDING" -eq 0 ]]; then
    printf 'RESULT: infrastructure is already provisioned. Nothing to do.\n'
  else
    printf 'RESULT: %d operation(s) pending. Re-run with --apply to perform them.\n' "$PENDING"
  fi
else
  printf 'RESULT: bootstrap applied. Secret VALUES (if any were flagged NEEDS VALUE) are still yours to add.\n'
fi

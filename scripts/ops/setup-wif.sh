#!/usr/bin/env bash
# ONE-TIME, in Cloud Shell: let THIS repository's GitHub Actions operate
# production with Workload Identity Federation -- no service-account key,
# anywhere, ever.
#
#   --plan   (default) read-only: prints every change --apply would make,
#            each marked CREATE / UPDATE / BIND / OK (already in place).
#   --apply  makes exactly those changes. Idempotent: every step describes
#            first and changes only what is missing, so a re-run is a no-op.
#
# What it sets up:
#   1. the IAM Credentials and STS APIs;
#   2. a workload identity pool and a GitHub OIDC provider whose ATTRIBUTE
#      CONDITION admits only this repository, on main, in the `production` or
#      `production-kill-switch` GitHub environment -- a token from a fork, a
#      branch, a pull request or any other repository is refused by Google
#      before any role is consulted;
#   3. a deploy service account with the MINIMUM roles the deploy scripts use
#      (listed below, each with the command that needs it), and
#      `iam.serviceAccountUser` on the three runtime identities only;
#   4. `roles/iam.workloadIdentityUser` on that account for principals of this
#      repository only.
# It prints the two NON-secret values the workflows read as repository
# variables (GCP_WORKLOAD_IDENTITY_PROVIDER, GCP_DEPLOY_SERVICE_ACCOUNT).
# It creates no key and never touches Cloud Run, a secret or the database.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

MODE="plan"
GITHUB_REPOSITORY_NAME="${MILO_GITHUB_REPOSITORY:-giladscore494/milo-agent-workspace}"
POOL_ID="milo-github"
PROVIDER_ID="github-actions"
DEPLOY_ACCOUNT_ID="milo-github-deployer"
ALLOWED_ENVIRONMENTS=("production" "production-kill-switch")

usage() {
  cat << 'EOF'
Usage: setup-wif.sh [--plan | --apply] [--operator-config <path>]

One-time Cloud Shell setup of Workload Identity Federation for this
repository's production workflows. --plan (default) changes nothing.
EOF
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --plan) MODE="plan"; shift ;;
    --apply) MODE="apply"; shift ;;
    --operator-config) MILO_OPERATOR_CONFIG_PATH="${2:?}"; shift 2 ;;
    --help) usage; exit 0 ;;
    *) printf 'FAIL: unknown argument %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done
[[ "$GITHUB_REPOSITORY_NAME" =~ ^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$ ]] \
  || { printf 'FAIL: MILO_GITHUB_REPOSITORY must be owner/repo\n' >&2; exit 2; }
ops_load_config
milo_require_op API_SERVICE_ACCOUNT WORKER_SERVICE_ACCOUNT ARTIFACT_REGISTRY_REPOSITORY || exit 2
milo_require_gcloud_context "$PROJECT_ID" || exit 2

PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
[[ "$PROJECT_NUMBER" =~ ^[0-9]+$ ]] || { printf 'FAIL: the project number could not be read\n' >&2; exit 1; }
DEPLOY_SA="${DEPLOY_ACCOUNT_ID}@${PROJECT_ID}.iam.gserviceaccount.com"
POOL_NAME="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_ID}"
PROVIDER_NAME="${POOL_NAME}/providers/${PROVIDER_ID}"
PRINCIPALS="principalSet://iam.googleapis.com/${POOL_NAME}/attribute.repository/${GITHUB_REPOSITORY_NAME}"
CAPTURE_SA="$(milo_op CAPTURE_SERVICE_ACCOUNT)"
REPOSITORY="$(milo_op ARTIFACT_REGISTRY_REPOSITORY)"

environments_cel="$(printf "'%s', " "${ALLOWED_ENVIRONMENTS[@]}")"
CONDITION="assertion.repository == '${GITHUB_REPOSITORY_NAME}' && assertion.ref == 'refs/heads/main' && assertion.environment in [${environments_cel%, }]"
MAPPING="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref,attribute.environment=assertion.environment"

# The minimum project roles, and the command in this repository that needs each.
PROJECT_ROLES=(
  "roles/run.admin"                        # cloud-run.sh deploy / jobs update / IAM binding on the jobs; kill switch; arm; capture flag
  "roles/cloudbuild.builds.editor"         # cloud-run.sh: gcloud builds submit (both images)
  "roles/artifactregistry.reader"          # images describe (capture script, verify, preflight)
  "roles/secretmanager.viewer"             # preflight: secrets describe / get-iam-policy (metadata only, never a value)
  "roles/iam.serviceAccountViewer"         # preflight / cloud-run.sh: service-accounts describe
  "roles/serviceusage.serviceUsageConsumer" # gcloud builds submit / services list against the project
  "roles/logging.viewer"                   # builds submit log streaming; capture execution documents
)
# Runtime identities the deployer deploys AS (iam.serviceAccounts.actAs), each
# bound on that account only -- never project-wide.
ACT_AS=("$(milo_op API_SERVICE_ACCOUNT)" "$(milo_op WORKER_SERVICE_ACCOUNT)")
[[ -n "$CAPTURE_SA" ]] && ACT_AS+=("$CAPTURE_SA")

CHANGES=0
step() {
  # step KIND DESCRIPTION CMD... -- KIND is CREATE / UPDATE / BIND.
  local kind="$1" description="$2"
  shift 2
  CHANGES=$((CHANGES + 1))
  printf '%-6s %s\n' "$kind" "$description"
  if [[ "$MODE" == "apply" ]]; then
    "$@" > /dev/null
  else
    printf '       '
    printf ' %q' "$@"
    printf '\n'
  fi
}
ok() { printf 'OK     %s\n' "$1"; }

printf '%s Workload Identity Federation for %s in %s (project number %s)\n\n' \
  "$([[ "$MODE" == "apply" ]] && printf APPLY || printf PLAN)" "$GITHUB_REPOSITORY_NAME" "$PROJECT_ID" "$PROJECT_NUMBER"

# 1. APIs.
for api in iamcredentials.googleapis.com sts.googleapis.com; do
  if [[ "$(gcloud services list --enabled --project "$PROJECT_ID" --filter="config.name:${api}" \
          --format='value(config.name)' 2> /dev/null)" == "$api" ]]; then
    ok "API ${api} enabled"
  else
    step CREATE "enable ${api}" gcloud services enable "$api" --project "$PROJECT_ID"
  fi
done

# 2. Pool and provider.
if gcloud iam workload-identity-pools describe "$POOL_ID" --location global --project "$PROJECT_ID" > /dev/null 2>&1; then
  ok "pool ${POOL_ID}"
else
  step CREATE "workload identity pool ${POOL_ID}" gcloud iam workload-identity-pools create "$POOL_ID" \
    --location global --project "$PROJECT_ID" --display-name "MILO GitHub Actions"
fi
current_condition="$(gcloud iam workload-identity-pools providers describe "$PROVIDER_ID" \
  --workload-identity-pool "$POOL_ID" --location global --project "$PROJECT_ID" \
  --format='value(attributeCondition)' 2> /dev/null || printf '%s' '<missing>')"
provider_args=(--workload-identity-pool "$POOL_ID" --location global --project "$PROJECT_ID"
               --issuer-uri "https://token.actions.githubusercontent.com"
               --attribute-mapping "$MAPPING" --attribute-condition "$CONDITION")
if [[ "$current_condition" == "<missing>" ]]; then
  step CREATE "OIDC provider ${PROVIDER_ID} (condition: ${CONDITION})" \
    gcloud iam workload-identity-pools providers create-oidc "$PROVIDER_ID" "${provider_args[@]}"
elif [[ "$current_condition" != "$CONDITION" ]]; then
  step UPDATE "OIDC provider ${PROVIDER_ID} condition -> ${CONDITION}" \
    gcloud iam workload-identity-pools providers update-oidc "$PROVIDER_ID" "${provider_args[@]}"
else
  ok "provider ${PROVIDER_ID} admits only ${GITHUB_REPOSITORY_NAME}, main, ${ALLOWED_ENVIRONMENTS[*]}"
fi

# 3. The deploy service account and its minimum roles.
if gcloud iam service-accounts describe "$DEPLOY_SA" --project "$PROJECT_ID" > /dev/null 2>&1; then
  ok "service account ${DEPLOY_SA}"
else
  step CREATE "service account ${DEPLOY_SA} (no key is ever created)" \
    gcloud iam service-accounts create "$DEPLOY_ACCOUNT_ID" --project "$PROJECT_ID" \
    --display-name "MILO GitHub Actions deployer"
fi
project_policy="$(gcloud projects get-iam-policy "$PROJECT_ID" --format=json 2> /dev/null || printf '{}')"
has_member() {
  # has_member POLICY_JSON ROLE MEMBER
  python3 -c '
import json, sys
policy = json.loads(sys.argv[1] or "{}")
sys.exit(0 if any(b.get("role") == sys.argv[2] and sys.argv[3] in (b.get("members") or [])
                  for b in policy.get("bindings") or []) else 1)' "$1" "$2" "$3"
}
for role in "${PROJECT_ROLES[@]}"; do
  if has_member "$project_policy" "$role" "serviceAccount:${DEPLOY_SA}"; then
    ok "project role ${role}"
  else
    step BIND "project role ${role} -> ${DEPLOY_SA}" gcloud projects add-iam-policy-binding "$PROJECT_ID" \
      --member "serviceAccount:${DEPLOY_SA}" --role "$role" --condition None
  fi
done
for account in "${ACT_AS[@]}"; do
  policy="$(gcloud iam service-accounts get-iam-policy "$account" --project "$PROJECT_ID" --format=json 2> /dev/null || printf '{}')"
  if has_member "$policy" roles/iam.serviceAccountUser "serviceAccount:${DEPLOY_SA}"; then
    ok "act as ${account}"
  else
    step BIND "roles/iam.serviceAccountUser on ${account} (that account only)" \
      gcloud iam service-accounts add-iam-policy-binding "$account" --project "$PROJECT_ID" \
      --member "serviceAccount:${DEPLOY_SA}" --role roles/iam.serviceAccountUser
  fi
done

# 3b. Cloud Build's source staging bucket: `gcloud builds submit` uploads the
#     source there. Bound on THAT bucket only, never project-wide storage.
BUILD_BUCKET="gs://${PROJECT_ID}_cloudbuild"
if gcloud storage buckets describe "$BUILD_BUCKET" > /dev/null 2>&1; then
  bucket_policy="$(gcloud storage buckets get-iam-policy "$BUILD_BUCKET" --format=json 2> /dev/null || printf '{}')"
  if has_member "$bucket_policy" roles/storage.admin "serviceAccount:${DEPLOY_SA}"; then
    ok "build source bucket ${BUILD_BUCKET}"
  else
    step BIND "roles/storage.admin on ${BUILD_BUCKET} only (Cloud Build source uploads)" \
      gcloud storage buckets add-iam-policy-binding "$BUILD_BUCKET" \
      --member "serviceAccount:${DEPLOY_SA}" --role roles/storage.admin
  fi
else
  printf 'NOTE   %s does not exist yet: run one Cloud Shell deploy first (it is created by the\n' "$BUILD_BUCKET"
  printf '       first gcloud builds submit), then re-run this script to bind it.\n'
fi

# 4. Only this repository's principals may impersonate the deployer.
policy="$(gcloud iam service-accounts get-iam-policy "$DEPLOY_SA" --project "$PROJECT_ID" --format=json 2> /dev/null || printf '{}')"
if has_member "$policy" roles/iam.workloadIdentityUser "$PRINCIPALS"; then
  ok "workloadIdentityUser for ${GITHUB_REPOSITORY_NAME}"
else
  step BIND "roles/iam.workloadIdentityUser on ${DEPLOY_SA} for ${GITHUB_REPOSITORY_NAME} only" \
    gcloud iam service-accounts add-iam-policy-binding "$DEPLOY_SA" --project "$PROJECT_ID" \
    --member "$PRINCIPALS" --role roles/iam.workloadIdentityUser
fi

printf '\nGitHub repository variables (Settings -> Secrets and variables -> Actions -> Variables):\n'
printf '  GCP_WORKLOAD_IDENTITY_PROVIDER=%s\n' "$PROVIDER_NAME"
printf '  GCP_DEPLOY_SERVICE_ACCOUNT=%s\n' "$DEPLOY_SA"
printf '  GCP_PROJECT_ID=%s\n' "$PROJECT_ID"
printf '\nArtifact Registry repository used by the deploy: %s (read through roles/artifactregistry.reader)\n' "$REPOSITORY"
if [[ "$MODE" == "plan" ]]; then
  printf '\nPLAN: %s change(s) listed above; nothing was changed. Re-run with --apply.\n' "$CHANGES"
else
  printf '\nAPPLIED: %s change(s). Re-running --plan now lists none.\n' "$CHANGES"
fi

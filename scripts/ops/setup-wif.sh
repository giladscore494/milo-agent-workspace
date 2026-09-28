#!/usr/bin/env bash
# ONE-TIME, in Cloud Shell: let THIS repository's GitHub Actions operate
# production with Workload Identity Federation -- no service-account key,
# anywhere, ever.
#
#   --plan   (default) read-only: prints every change --apply would make,
#            each marked CREATE / UPDATE / BIND / UNBIND / OK (already in
#            place).
#   --apply  makes exactly those changes. Idempotent: every step describes
#            first and changes only what is missing, so a re-run is a no-op.
#
# What it sets up (a fresh project needs no step by hand):
#   1. every API the keyless deploy reaches: Cloud Resource Manager (a service
#      account's `gcloud projects describe`), IAM, IAM Credentials, STS, Cloud
#      Run, Cloud Build, Artifact Registry, Service Usage and Cloud Logging
#      (where the builds log);
#   2. a workload identity pool and a GitHub OIDC provider whose ATTRIBUTE
#      CONDITION admits only this repository, on main, in the `production` or
#      `production-kill-switch` GitHub environment -- a token from a fork, a
#      branch, a pull request or any other repository is refused by Google
#      before any role is consulted;
#   3. a deploy service account with the MINIMUM roles the deploy scripts use
#      (listed below, each with the command that needs it), and
#      `iam.serviceAccountUser` on the three runtime identities only;
#   4. `roles/iam.workloadIdentityUser` on that account for principals of this
#      repository only;
#   5. the dedicated BUILD identity (CLOUD_BUILD_SERVICE_ACCOUNT, e.g.
#      milo-cloudbuild@) that both image builds run as, with exactly three
#      bindings: artifactregistry.writer on the image repository only,
#      storage.objectViewer on the Cloud Build source bucket only, and
#      logging.logWriter on the project -- and the deployer's actAs on THAT
#      account only. Never on the Compute Engine default service account
#      (Cloud Build's default identity here): a deployer binding on it, or a
#      project-wide serviceAccountUser, is planned as UNBIND.
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
milo_require_op API_SERVICE_ACCOUNT WORKER_SERVICE_ACCOUNT ARTIFACT_REGISTRY_REPOSITORY \
  CLOUD_BUILD_SERVICE_ACCOUNT || exit 2
BUILD_SA="$(milo_op CLOUD_BUILD_SERVICE_ACCOUNT)"
if ! problem="$(milo_build_service_account_problem "$BUILD_SA")"; then
  printf 'FAIL: %s\n' "$problem" >&2
  exit 2
fi
# This script CREATES the build identity, so it must live in this project.
if [[ "$BUILD_SA" != *"@${PROJECT_ID}.iam.gserviceaccount.com" ]]; then
  printf 'FAIL: CLOUD_BUILD_SERVICE_ACCOUNT must be a service account of %s (NAME@%s.iam.gserviceaccount.com)\n' \
    "$PROJECT_ID" "$PROJECT_ID" >&2
  exit 2
fi
milo_require_gcloud_context "$PROJECT_ID" || exit 2

PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
[[ "$PROJECT_NUMBER" =~ ^[0-9]+$ ]] || { printf 'FAIL: the project number could not be read\n' >&2; exit 1; }
DEPLOY_SA="${DEPLOY_ACCOUNT_ID}@${PROJECT_ID}.iam.gserviceaccount.com"
POOL_NAME="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_ID}"
PROVIDER_NAME="${POOL_NAME}/providers/${PROVIDER_ID}"
PRINCIPALS="principalSet://iam.googleapis.com/${POOL_NAME}/attribute.repository/${GITHUB_REPOSITORY_NAME}"
# Cloud Build's default identity in this project. The deployer never acts as it.
COMPUTE_DEFAULT_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
CAPTURE_SA="$(milo_op CAPTURE_SERVICE_ACCOUNT)"
REPOSITORY="$(milo_op ARTIFACT_REGISTRY_REPOSITORY)"

environments_cel="$(printf "'%s', " "${ALLOWED_ENVIRONMENTS[@]}")"
CONDITION="assertion.repository == '${GITHUB_REPOSITORY_NAME}' && assertion.ref == 'refs/heads/main' && assertion.environment in [${environments_cel%, }]"
MAPPING="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref,attribute.environment=assertion.environment"

# The minimum project roles, and the command in this repository that needs each.
# website-stage.sh (and deploy.sh step 11) needs nothing beyond them:
# --ensure-job is `gcloud run jobs create|update` of the capture job AS the
# capture identity (run.admin + actAs below) after an images describe
# (artifactregistry.reader); the capture-job binding is `gcloud run jobs
# add-iam-policy-binding` (run.jobs.setIamPolicy, in run.admin); Stage P / E'
# are `gcloud run services update` of the API AS its identity (run.admin +
# actAs). The deployer never reads a secret value: the capture job's secrets
# are read by the capture identity at run time.
PROJECT_ROLES=(
  "roles/run.admin"                        # cloud-run.sh deploy / jobs update / IAM binding on the jobs; kill switch; arm; capture flag; website stage (capture job ensure + its run-with-overrides binding)
  "roles/cloudbuild.builds.editor"         # cloud-run.sh: gcloud builds submit (both images)
  "roles/artifactregistry.reader"          # cloud-run.sh + preflight: docker tags list (the built image's exact tag); images describe (capture script --ensure-job, verify). No Container Analysis role, ever
  "roles/secretmanager.viewer"             # preflight: secrets describe / get-iam-policy (metadata only, never a value)
  "roles/iam.serviceAccountViewer"         # preflight / cloud-run.sh: service-accounts describe
  "roles/serviceusage.serviceUsageConsumer" # gcloud builds submit / services list against the project
  "roles/logging.viewer"                   # builds submit log streaming; capture execution documents
  "roles/storage.bucketViewer"             # gcloud builds submit: proves the default source bucket belongs to the project (storage.buckets.list)
)
# Runtime identities the deployer deploys AS (iam.serviceAccounts.actAs), each
# bound on that account only -- never project-wide. The capture job runs as
# CAPTURE_SERVICE_ACCOUNT, or, when none is configured, as the worker identity
# (government-production-capture.sh), which is already in this list.
ACT_AS=("$(milo_op API_SERVICE_ACCOUNT)" "$(milo_op WORKER_SERVICE_ACCOUNT)")
[[ -n "$CAPTURE_SA" ]] && ACT_AS+=("$CAPTURE_SA")
# ...and the build identity, whose builds `gcloud builds submit --service-account` starts.
ACT_AS+=("$BUILD_SA")
for account in "${ACT_AS[@]}"; do
  case "$account" in
    "$COMPUTE_DEFAULT_SA" | *@developer.gserviceaccount.com | *@cloudbuild.gserviceaccount.com)
      printf 'FAIL: %s is a Google-managed default identity; the deployer never acts as it\n' "$account" >&2
      exit 2 ;;
  esac
done
# The build identity's bindings -- exactly these, each on the narrowest resource.
BUILD_PROJECT_ROLES=("roles/logging.logWriter")   # build logs (CLOUD_LOGGING_ONLY)
BUILD_REPOSITORY_ROLE="roles/artifactregistry.writer" # push both images to ${REPOSITORY} only
BUILD_BUCKET_ROLE="roles/storage.objectViewer"       # read the uploaded source only

CHANGES=0
step() {
  # step KIND DESCRIPTION CMD... -- KIND is CREATE / UPDATE / BIND / UNBIND.
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

# The image repository is gcp-bootstrap.sh's (it also enables the APIs it
# needs); the build identity is bound on it, so --apply changes nothing unless
# it exists.
if [[ "$MODE" == "apply" ]] && ! gcloud artifacts repositories describe "$REPOSITORY" --location "$REGION" \
     --project "$PROJECT_ID" > /dev/null 2>&1; then
  printf 'FAIL: Artifact Registry repository %s does not exist in %s: run scripts/deploy/gcp-bootstrap.sh --apply first. Nothing was changed.\n' \
    "$REPOSITORY" "$REGION" >&2
  exit 1
fi

# 1. APIs. cloudresourcemanager: a SERVICE ACCOUNT's `gcloud projects describe`
#    needs it (a user account does not). serviceusage: `gcloud services list`
#    and every quota-checked call.
REQUIRED_APIS=(cloudresourcemanager.googleapis.com iam.googleapis.com iamcredentials.googleapis.com
               sts.googleapis.com run.googleapis.com cloudbuild.googleapis.com
               artifactregistry.googleapis.com serviceusage.googleapis.com
               logging.googleapis.com)   # build logs: CLOUD_LOGGING_ONLY
for api in "${REQUIRED_APIS[@]}"; do
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
# The build identity exists before anything is bound on or to it.
if gcloud iam service-accounts describe "$BUILD_SA" --project "$PROJECT_ID" > /dev/null 2>&1; then
  ok "build service account ${BUILD_SA}"
else
  step CREATE "build service account ${BUILD_SA} (no key is ever created)" \
    gcloud iam service-accounts create "${BUILD_SA%@*}" --project "$PROJECT_ID" \
    --display-name "MILO Cloud Build (image builds only)"
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
#     source there (the deployer) and the build reads it (the build identity).
#     Bound on THAT bucket only, never project-wide storage. A fresh project
#     has no bucket until the first build; it is created here so that first
#     build can be the keyless one.
BUILD_BUCKET="gs://${PROJECT_ID}_cloudbuild"
if gcloud storage buckets describe "$BUILD_BUCKET" > /dev/null 2>&1; then
  ok "build source bucket ${BUILD_BUCKET}"
  bucket_policy="$(gcloud storage buckets get-iam-policy "$BUILD_BUCKET" --format=json 2> /dev/null || printf '{}')"
else
  step CREATE "build source bucket ${BUILD_BUCKET} (Cloud Build's default; uniform access)" \
    gcloud storage buckets create "$BUILD_BUCKET" --project "$PROJECT_ID" --location us \
    --uniform-bucket-level-access
  bucket_policy='{}'
fi
if has_member "$bucket_policy" roles/storage.admin "serviceAccount:${DEPLOY_SA}"; then
  ok "deployer uploads to ${BUILD_BUCKET}"
else
  step BIND "roles/storage.admin on ${BUILD_BUCKET} only (Cloud Build source uploads)" \
    gcloud storage buckets add-iam-policy-binding "$BUILD_BUCKET" \
    --member "serviceAccount:${DEPLOY_SA}" --role roles/storage.admin
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

# 5. The build identity's bindings (the account itself is created in 3, before
#    the deployer's actAs on it): both image builds run as it (cloud-run.sh
#    --service-account), never as Cloud Build's default, which here is the
#    Compute Engine default service account.
for role in "${BUILD_PROJECT_ROLES[@]}"; do
  if has_member "$project_policy" "$role" "serviceAccount:${BUILD_SA}"; then
    ok "build identity project role ${role}"
  else
    step BIND "project role ${role} -> ${BUILD_SA}" gcloud projects add-iam-policy-binding "$PROJECT_ID" \
      --member "serviceAccount:${BUILD_SA}" --role "$role" --condition None
  fi
done
repository_policy="$(gcloud artifacts repositories get-iam-policy "$REPOSITORY" --location "$REGION" \
  --project "$PROJECT_ID" --format=json 2> /dev/null || printf '{}')"
if has_member "$repository_policy" "$BUILD_REPOSITORY_ROLE" "serviceAccount:${BUILD_SA}"; then
  ok "build identity ${BUILD_REPOSITORY_ROLE} on repository ${REPOSITORY}"
else
  step BIND "${BUILD_REPOSITORY_ROLE} on repository ${REPOSITORY} only -> ${BUILD_SA} (push both images)" \
    gcloud artifacts repositories add-iam-policy-binding "$REPOSITORY" --location "$REGION" \
    --project "$PROJECT_ID" --member "serviceAccount:${BUILD_SA}" --role "$BUILD_REPOSITORY_ROLE"
fi
if has_member "$bucket_policy" "$BUILD_BUCKET_ROLE" "serviceAccount:${BUILD_SA}"; then
  ok "build identity ${BUILD_BUCKET_ROLE} on ${BUILD_BUCKET}"
else
  step BIND "${BUILD_BUCKET_ROLE} on ${BUILD_BUCKET} only -> ${BUILD_SA} (read the uploaded source)" \
    gcloud storage buckets add-iam-policy-binding "$BUILD_BUCKET" \
    --member "serviceAccount:${BUILD_SA}" --role "$BUILD_BUCKET_ROLE"
fi
# Exactly these: any OTHER project role on the build identity is reported, not
# silently kept (removing it is the owner's decision).
extra_build_roles="$(python3 -c '
import json, sys
policy = json.loads(sys.argv[1] or "{}")
member, allowed = "serviceAccount:" + sys.argv[2], set(sys.argv[3:])
print(" ".join(sorted({b.get("role", "") for b in policy.get("bindings") or []
                       if member in (b.get("members") or []) and b.get("role") not in allowed})))' \
  "$project_policy" "$BUILD_SA" "${BUILD_PROJECT_ROLES[@]}")"
if [[ -n "$extra_build_roles" ]]; then
  printf 'WARN   %s also holds project role(s) %s: the build identity needs only %s here. Remove them:\n' \
    "$BUILD_SA" "$extra_build_roles" "${BUILD_PROJECT_ROLES[*]}"
  printf '       gcloud projects remove-iam-policy-binding %s --member serviceAccount:%s --role <ROLE> --all\n' \
    "$PROJECT_ID" "$BUILD_SA"
fi

# 6. Never the Compute Engine default service account: the deployer may not
#    act as it -- not through a binding on that account, not through a
#    project-wide serviceAccountUser (which reaches every account). Either one,
#    if present, is removed for the deployer ONLY.
compute_policy="$(gcloud iam service-accounts get-iam-policy "$COMPUTE_DEFAULT_SA" --project "$PROJECT_ID" \
  --format=json 2> /dev/null || printf '{}')"
for role in roles/iam.serviceAccountUser roles/iam.serviceAccountTokenCreator; do
  if has_member "$compute_policy" "$role" "serviceAccount:${DEPLOY_SA}"; then
    step UNBIND "${role} on ${COMPUTE_DEFAULT_SA} (the Compute default SA) <- ${DEPLOY_SA}" \
      gcloud iam service-accounts remove-iam-policy-binding "$COMPUTE_DEFAULT_SA" --project "$PROJECT_ID" \
      --member "serviceAccount:${DEPLOY_SA}" --role "$role"
  fi
  if has_member "$project_policy" "$role" "serviceAccount:${DEPLOY_SA}"; then
    step UNBIND "project-wide ${role} <- ${DEPLOY_SA} (it reaches the Compute default SA)" \
      gcloud projects remove-iam-policy-binding "$PROJECT_ID" \
      --member "serviceAccount:${DEPLOY_SA}" --role "$role" --all
  fi
done
ok "the deployer cannot act as ${COMPUTE_DEFAULT_SA} through any binding this script can see"

printf '\nGitHub repository variables (Settings -> Secrets and variables -> Actions -> Variables):\n'
printf '  GCP_WORKLOAD_IDENTITY_PROVIDER=%s\n' "$PROVIDER_NAME"
printf '  GCP_DEPLOY_SERVICE_ACCOUNT=%s\n' "$DEPLOY_SA"
printf '  GCP_PROJECT_ID=%s\n' "$PROJECT_ID"
printf '\nOperator configuration (MILO_OPERATOR_CONFIG and config/production-operator.env):\n'
printf '  CLOUD_BUILD_SERVICE_ACCOUNT=%s\n' "$BUILD_SA"
printf '\nArtifact Registry repository used by the deploy: %s (the deployer reads it through\n' "$REPOSITORY"
printf 'roles/artifactregistry.reader; only %s writes to it)\n' "$BUILD_SA"
if [[ "$MODE" == "plan" ]]; then
  printf '\nPLAN: %s change(s) listed above; nothing was changed. Re-run with --apply.\n' "$CHANGES"
else
  printf '\nAPPLIED: %s change(s). Re-running --plan now lists none.\n' "$CHANGES"
fi

#!/usr/bin/env bash
# Deployment plan generator (plan only; never deploys, builds or pushes).
#
# Emits the exact, strictly ordered command plan for a staged production
# deployment with immutable commit-SHA image tags, worker-before-API
# ordering, private-ingress verification and execution disabled throughout.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

usage() {
  cat << 'EOF'
Usage: generate-deployment-plan.sh --release-sha <full-sha> [options]

Generates a command plan only. Executes nothing. Deploys nothing.

Options:
  --release-sha <sha>    Full 40-character immutable release commit SHA
                         (mutable tags such as latest/prod/stable/branch
                         names are rejected).
  --manifest <path>      Production manifest (default:
                         config/production.example.yaml) providing the
                         placeholder identifiers used in the plan.
  --output <path>        Write the plan as markdown (default: stdout only).
  --json-output <path>   Write a machine-readable JSON report.
  --help                 Show this help.
EOF
}

JSON_OUTPUT="" RELEASE_SHA="" MANIFEST="${REPO_ROOT}/config/production.example.yaml" OUTPUT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --release-sha) RELEASE_SHA="${2:?}"; shift 2 ;;
    --manifest) MANIFEST="${2:?}"; shift 2 ;;
    --output) OUTPUT="${2:?}"; shift 2 ;;
    --json-output) JSON_OUTPUT="${2:?}"; shift 2 ;;
    --help) usage; exit 0 ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; usage >&2; exit 64 ;;
  esac
done

for mutable in latest prod stable main master; do
  if [[ "${RELEASE_SHA}" == "${mutable}" ]]; then
    record_check BLOCKED "release-sha" "mutable image identifier '${mutable}' rejected; use the full commit SHA"
    finish_checks "generate-deployment-plan" "${JSON_OUTPUT}"
    exit $?
  fi
done
if ! is_full_sha "${RELEASE_SHA}"; then
  record_check BLOCKED "release-sha" "--release-sha must be the full 40-character commit SHA (immutable tag policy)"
  finish_checks "generate-deployment-plan" "${JSON_OUTPUT}"
  exit $?
fi
record_check PASS "release-sha" "immutable release SHA accepted"

if [[ ! -f "${REPO_ROOT}/Dockerfile.api" || ! -f "${REPO_ROOT}/Dockerfile.worker" ]]; then
  record_check BLOCKED "dockerfiles" "Dockerfile.api and Dockerfile.worker must both exist (API and worker images are built separately)"
  finish_checks "generate-deployment-plan" "${JSON_OUTPUT}"
  exit $?
fi
record_check PASS "dockerfiles" "separate API and worker Dockerfiles present"

if [[ -f "${MANIFEST}" ]] && tool_available python3; then
  if python3 "${SCRIPT_DIR}/validate_production_manifest.py" --manifest "${MANIFEST}" --mode plan > /dev/null 2>&1; then
    record_check PASS "manifest" "manifest schema validation passed (${MANIFEST})"
  else
    record_check BLOCKED "manifest" "manifest failed schema validation: ${MANIFEST}"
    finish_checks "generate-deployment-plan" "${JSON_OUTPUT}"
    exit $?
  fi
fi

SHA="${RELEASE_SHA}"
plan="$(cat << EOF
# MILO staged deployment plan — release ${SHA}

Generated command plan. Every command is a TEMPLATE for a human operator:
replace each <PLACEHOLDER> with the value recorded in the approved
production manifest. Nothing below is executed by this script. Execution
flags stay OFF for the entire plan (Stage A posture).

## 0. Prerequisites (verify, do not skip)

    scripts/release/production-readiness.sh --json-output readiness.json
    scripts/release/check-migration-state.sh --plan-output migration-plan.json
    scripts/release/check-production-config.sh --env-file <APPROVED_ENV_METADATA>

Blockers from any of the above stop the deployment.

## 1. Verify migration readiness

    scripts/release/check-migration-state.sh --database-url-env MILO_READONLY_DB_URL

Remote state must be a supported state (empty schema, confirmed legacy
baseline, or a reviewed partial state) and migrations are applied MANUALLY
per docs/production-readiness/MIGRATIONS.md before continuing.

## 2. Verify execution flags are off

    scripts/release/smoke-test-execution-disabled.sh --env-file <APPROVED_ENV_METADATA>

## 3. Build both immutable images (local build; nothing pushed)

    docker build -f Dockerfile.api \\
      --label org.opencontainers.image.revision=${SHA} \\
      --label org.opencontainers.image.title=milo-api \\
      -t <GCP_REGION>-docker.pkg.dev/<GCP_PROJECT_ID>/<ARTIFACT_REGISTRY_REPOSITORY>/milo-api:${SHA} .

    docker build -f Dockerfile.worker \\
      --label org.opencontainers.image.revision=${SHA} \\
      --label org.opencontainers.image.title=milo-worker \\
      -t <GCP_REGION>-docker.pkg.dev/<GCP_PROJECT_ID>/<ARTIFACT_REGISTRY_REPOSITORY>/milo-worker:${SHA} .

## 4. Push immutable images (manual operator action)

    docker push <GCP_REGION>-docker.pkg.dev/<GCP_PROJECT_ID>/<ARTIFACT_REGISTRY_REPOSITORY>/milo-api:${SHA}
    docker push <GCP_REGION>-docker.pkg.dev/<GCP_PROJECT_ID>/<ARTIFACT_REGISTRY_REPOSITORY>/milo-worker:${SHA}

## 5. Deploy or update the PRIVATE worker job FIRST (never execute it)

    gcloud run jobs deploy <CLOUD_RUN_WORKER_JOB> \\
      --image <GCP_REGION>-docker.pkg.dev/<GCP_PROJECT_ID>/<ARTIFACT_REGISTRY_REPOSITORY>/milo-worker:${SHA} \\
      --region <GCP_REGION> --project <GCP_PROJECT_ID> \\
      --service-account <WORKER_SERVICE_ACCOUNT_EMAIL> \\
      --set-secrets SUPABASE_SERVICE_ROLE_KEY=<SUPABASE_SERVICE_KEY_SECRET>:latest \\
      --set-env-vars ENVIRONMENT=production,SUPABASE_URL=<SUPABASE_URL> \\
      --max-retries 0 --task-timeout 3600

## 6. Verify worker job configuration WITHOUT executing it

    gcloud run jobs describe <CLOUD_RUN_WORKER_JOB> --region <GCP_REGION> --project <GCP_PROJECT_ID> \\
      --format 'value(spec.template.spec.template.spec.serviceAccountName, spec.template.spec.template.spec.containers[0].image)'
    gcloud run jobs executions list --job <CLOUD_RUN_WORKER_JOB> --region <GCP_REGION>   # expect: no new executions

## 7. Deploy or update the PRIVATE Cloud Run API

    gcloud run deploy <CLOUD_RUN_API_SERVICE> \\
      --image <GCP_REGION>-docker.pkg.dev/<GCP_PROJECT_ID>/<ARTIFACT_REGISTRY_REPOSITORY>/milo-api:${SHA} \\
      --region <GCP_REGION> --project <GCP_PROJECT_ID> \\
      --service-account <API_SERVICE_ACCOUNT_EMAIL> \\
      --no-allow-unauthenticated --ingress all \\
      --revision-suffix rel-${SHA:0:12} \\
      --set-secrets SUPABASE_SERVICE_ROLE_KEY=<SUPABASE_SERVICE_KEY_SECRET>:latest \\
      --set-env-vars ENVIRONMENT=production,SUPABASE_URL=<SUPABASE_URL>,ALLOWED_CORS_ORIGINS=<PRODUCTION_ORIGINS>,MILO_GATEWAY_AUDIENCE=<CLOUD_RUN_API_URL>,MILO_APPROVED_GATEWAY_IDENTITIES=<GATEWAY_IDENTITY_EMAIL>,JOB_LAUNCHER=disabled

Note: authentication is enforced by --no-allow-unauthenticated (Cloud Run
IAM) plus the application-level verified gateway token; ingress stays
reachable only to authorized identities.

## 8. Verify API revision

    gcloud run services describe <CLOUD_RUN_API_SERVICE> --region <GCP_REGION> --project <GCP_PROJECT_ID> \\
      --format 'value(status.latestReadyRevisionName, spec.template.spec.containers[0].image)'

The image digest must match the pushed milo-api:${SHA} digest:

    gcloud artifacts docker images describe <GCP_REGION>-docker.pkg.dev/<GCP_PROJECT_ID>/<ARTIFACT_REGISTRY_REPOSITORY>/milo-api:${SHA} --format 'value(image_summary.digest)'

## 9. Verify private ingress and invoker policy

    gcloud run services get-iam-policy <CLOUD_RUN_API_SERVICE> --region <GCP_REGION>   # expect: no allUsers
    gcloud run jobs get-iam-policy <CLOUD_RUN_WORKER_JOB> --region <GCP_REGION>        # expect: no allUsers
    curl -s -o /dev/null -w '%{http_code}\n' <CLOUD_RUN_API_URL>/health                 # expect: 401/403 (private)

## 10. Configure Vercel server environment (names in ENVIRONMENT_MATRIX.md)

    vercel env add CLOUD_RUN_API_URL production
    vercel env add GCP_PROJECT_NUMBER production
    vercel env add GCP_WORKLOAD_IDENTITY_POOL_ID production
    vercel env add GCP_WORKLOAD_IDENTITY_POOL_PROVIDER_ID production
    vercel env add GCP_SERVICE_ACCOUNT_EMAIL production
    vercel env add UPSTASH_REDIS_REST_URL production
    vercel env add UPSTASH_REDIS_REST_TOKEN production

## 11. Deploy Vercel

    vercel deploy --prod

## 12. Stage A read-only validation

    scripts/release/smoke-test-read-only.sh --base-url <PRODUCTION_VERCEL_URL> --user-token-env MILO_SMOKE_USER_TOKEN ...
    scripts/release/smoke-test-execution-disabled.sh --base-url <PRODUCTION_VERCEL_URL> --env-file <APPROVED_ENV_METADATA>

## 13. Later activation stages

Only after explicit operator approval, per
docs/production-readiness/STAGED_ACTIVATION.md (Stages B, C, D). This plan
never enables an execution flag.
EOF
)"

printf '%s\n' "${plan}"
if [[ -n "${OUTPUT}" ]]; then
  printf '%s\n' "${plan}" > "${OUTPUT}"
  record_check PASS "plan" "deployment plan written to ${OUTPUT}"
else
  record_check PASS "plan" "deployment plan generated (stdout)"
fi
record_check MANUAL "execute" "every command above is executed manually by the operator in the listed order; this script never deploys"

finish_checks "generate-deployment-plan" "${JSON_OUTPUT}"

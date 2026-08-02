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
# Stage A: every execution flag is pinned OFF by the deployment itself
# (mirrors backend/production_config.py EXECUTION_FLAGS). The '@' delimiter
# prefix keeps comma-containing values (CORS origins, identity lists) intact.
STAGE_A_FLAGS='JOB_LAUNCHER=disabled@MILO_ENABLE_RUN_CREATION=false@MILO_ENABLE_PROPOSAL_MUTATIONS=false@MILO_ENABLE_PROPOSAL_READS=false@MILO_ENABLE_RUN_CANCELLATION=false@MILO_ENABLE_EXECUTION_CONTROL=false@MILO_ENABLE_PAID_EXECUTION=false'
plan="$(cat << EOF
# MILO staged deployment plan — release ${SHA}

Generated command plan. Every command is a TEMPLATE for a human operator:
replace each <PLACEHOLDER> with the value recorded in the approved
production manifest. Nothing below is executed by this script. Execution
flags stay OFF for the entire plan (Stage A posture).

Two rules apply to every command below:

- **Immutable identity.** Image tags are always the full 40-character
  commit SHA \`${SHA}\`. Short SHAs and mutable tags are never used.
  (\`--revision-suffix\` is a Cloud Run *revision name*, not an image
  reference; it is shortened only because revision names are length
  limited.)
- **Non-destructive configuration.** Only \`--update-env-vars\` and
  \`--update-secrets\` are used. The destructive \`--set-env-vars\` /
  \`--set-secrets\` forms would delete every production variable and secret
  binding not repeated in the command, so they are forbidden here — as are
  \`--clear-env-vars\` and \`--clear-secrets\`. Variables this release does
  not own (gateway audience, worker identities, budget caps, rate limits)
  keep their current values.

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

## 5. Record the CURRENT worker/API configuration (proof of preservation)

Capture the live variable names and secret references before changing
anything, so step 9 can prove that nothing was dropped. Names and secret
references only — never secret values.

    gcloud run jobs describe <CLOUD_RUN_WORKER_JOB> --region <GCP_REGION> --project <GCP_PROJECT_ID> \\
      --format 'value(spec.template.spec.template.spec.containers[0].env[].name)' | tr ';' '\n' | sort > worker-env-before.txt
    gcloud run services describe <CLOUD_RUN_API_SERVICE> --region <GCP_REGION> --project <GCP_PROJECT_ID> \\
      --format 'value(spec.template.spec.containers[0].env[].name)' | tr ';' '\n' | sort > api-env-before.txt

## 6. Deploy or update the PRIVATE worker job FIRST (never execute it)

The provider key (\`KIMI_API_KEY\`) is bound to the WORKER ONLY and only
from Stage C onwards; the API never receives it. At Stage A the worker
carries no provider key at all.

    gcloud run jobs deploy <CLOUD_RUN_WORKER_JOB> \\
      --image <GCP_REGION>-docker.pkg.dev/<GCP_PROJECT_ID>/<ARTIFACT_REGISTRY_REPOSITORY>/milo-worker:${SHA} \\
      --region <GCP_REGION> --project <GCP_PROJECT_ID> \\
      --service-account <WORKER_SERVICE_ACCOUNT_EMAIL> \\
      --update-secrets SUPABASE_SERVICE_ROLE_KEY=<SUPABASE_SERVICE_KEY_SECRET>:latest \\
      --update-env-vars '^@^ENVIRONMENT=production@SUPABASE_URL=<SUPABASE_URL>@${STAGE_A_FLAGS#JOB_LAUNCHER=disabled@}' \\
      --max-retries 0 --task-timeout 3600

\`gcloud run jobs deploy\` only creates/updates the job definition. Do NOT
run \`gcloud run jobs execute\` at any point in this plan.

## 7. Verify worker job configuration WITHOUT executing it

    gcloud run jobs describe <CLOUD_RUN_WORKER_JOB> --region <GCP_REGION> --project <GCP_PROJECT_ID> \\
      --format 'value(spec.template.spec.template.spec.serviceAccountName, spec.template.spec.template.spec.containers[0].image)'
    gcloud run jobs describe <CLOUD_RUN_WORKER_JOB> --region <GCP_REGION> --project <GCP_PROJECT_ID> \\
      --format 'value(spec.template.spec.template.spec.containers[0].env[].name)'                              # variable NAMES
    gcloud run jobs describe <CLOUD_RUN_WORKER_JOB> --region <GCP_REGION> --project <GCP_PROJECT_ID> \\
      --format 'value(spec.template.spec.template.spec.containers[0].env[].valueFrom.secretKeyRef.secret)'     # secret REFERENCES
    gcloud run jobs executions list --job <CLOUD_RUN_WORKER_JOB> --region <GCP_REGION>   # expect: no new executions

Preservation proof — the update must add variables, never remove them:

    gcloud run jobs describe <CLOUD_RUN_WORKER_JOB> --region <GCP_REGION> --project <GCP_PROJECT_ID> \\
      --format 'value(spec.template.spec.template.spec.containers[0].env[].name)' | tr ';' '\n' | sort > worker-env-after.txt
    comm -23 worker-env-before.txt worker-env-after.txt   # expect: EMPTY (nothing was dropped)

Expected: image tag = ${SHA}, service account = <WORKER_SERVICE_ACCOUNT_EMAIL>
(never the API account), every execution flag present and \`false\`, and an
execution list identical to the one from before the deployment.

## 8. Deploy or update the PRIVATE Cloud Run API

    gcloud run deploy <CLOUD_RUN_API_SERVICE> \\
      --image <GCP_REGION>-docker.pkg.dev/<GCP_PROJECT_ID>/<ARTIFACT_REGISTRY_REPOSITORY>/milo-api:${SHA} \\
      --region <GCP_REGION> --project <GCP_PROJECT_ID> \\
      --service-account <API_SERVICE_ACCOUNT_EMAIL> \\
      --no-allow-unauthenticated --ingress all \\
      --revision-suffix rel-${SHA:0:12} \\
      --update-secrets SUPABASE_SERVICE_ROLE_KEY=<SUPABASE_SERVICE_KEY_SECRET>:latest \\
      --update-env-vars '^@^ENVIRONMENT=production@SUPABASE_URL=<SUPABASE_URL>@ALLOWED_CORS_ORIGINS=<PRODUCTION_ORIGINS>@MILO_GATEWAY_AUDIENCE=<CLOUD_RUN_API_URL>@MILO_APPROVED_GATEWAY_IDENTITIES=<GATEWAY_IDENTITY_EMAIL>@${STAGE_A_FLAGS}'

Note: authentication is enforced by --no-allow-unauthenticated (Cloud Run
IAM) plus the application-level verified gateway token; ingress stays
reachable only to authorized identities. The API service account differs
from the worker service account and the API is never given the provider
key.

## 9. Verify API revision, identity, variable names and secret references

    gcloud run services describe <CLOUD_RUN_API_SERVICE> --region <GCP_REGION> --project <GCP_PROJECT_ID> \\
      --format 'value(status.latestReadyRevisionName, spec.template.spec.containers[0].image, spec.template.spec.serviceAccountName)'
    gcloud run services describe <CLOUD_RUN_API_SERVICE> --region <GCP_REGION> --project <GCP_PROJECT_ID> \\
      --format 'value(spec.template.spec.containers[0].env[].name)' | tr ';' '\n' | sort > api-env-after.txt
    gcloud run services describe <CLOUD_RUN_API_SERVICE> --region <GCP_REGION> --project <GCP_PROJECT_ID> \\
      --format 'value(spec.template.spec.containers[0].env[].valueFrom.secretKeyRef.secret)'                   # secret REFERENCES

Preservation proof — the deployment must add variables, never remove them:

    comm -23 api-env-before.txt api-env-after.txt    # expect: EMPTY (nothing was dropped)

\`KIMI_API_KEY\` / \`MOONSHOT_API_KEY\` must NOT appear in the API secret
references. The image digest must match the pushed milo-api:${SHA} digest:

    gcloud artifacts docker images describe <GCP_REGION>-docker.pkg.dev/<GCP_PROJECT_ID>/<ARTIFACT_REGISTRY_REPOSITORY>/milo-api:${SHA} --format 'value(image_summary.digest)'
    gcloud artifacts docker images describe <GCP_REGION>-docker.pkg.dev/<GCP_PROJECT_ID>/<ARTIFACT_REGISTRY_REPOSITORY>/milo-worker:${SHA} --format 'value(image_summary.digest)'

## 10. Verify private ingress and invoker policy

    gcloud run services get-iam-policy <CLOUD_RUN_API_SERVICE> --region <GCP_REGION>   # expect: no allUsers
    gcloud run jobs get-iam-policy <CLOUD_RUN_WORKER_JOB> --region <GCP_REGION>        # expect: no allUsers
    curl -s -o /dev/null -w '%{http_code}\n' <CLOUD_RUN_API_URL>/health                 # expect: 401/403 (private)

## 11. Configure Vercel server environment (names in ENVIRONMENT_MATRIX.md)

    vercel env add CLOUD_RUN_API_URL production
    vercel env add GCP_PROJECT_NUMBER production
    vercel env add GCP_WORKLOAD_IDENTITY_POOL_ID production
    vercel env add GCP_WORKLOAD_IDENTITY_POOL_PROVIDER_ID production
    vercel env add GCP_SERVICE_ACCOUNT_EMAIL production
    vercel env add UPSTASH_REDIS_REST_URL production
    vercel env add UPSTASH_REDIS_REST_TOKEN production

## 12. Deploy Vercel

    vercel deploy --prod

## 13. Stage A read-only validation

    scripts/release/smoke-test-read-only.sh --base-url <PRODUCTION_VERCEL_URL> --user-token-env MILO_SMOKE_USER_TOKEN ...
    scripts/release/smoke-test-execution-disabled.sh --base-url <PRODUCTION_VERCEL_URL> --env-file <APPROVED_ENV_METADATA>

## 14. Later activation stages

Only after explicit operator approval, per
docs/production-readiness/STAGED_ACTIVATION.md (Stages B, C, D). This plan
never enables an execution flag.
EOF
)"

# ---------------------------------------------------------------------------
# self-checks: the emitted plan must never contain an unsafe command
# ---------------------------------------------------------------------------
plan_blocked=0
# Only the indented command templates are checked; the surrounding prose
# names the forbidden flags on purpose.
plan_commands="$(grep -E '^[[:space:]]{4,}' <<< "${plan}" || true)"

if grep -Eq -- '--(set|clear)-(env-vars|secrets)' <<< "${plan_commands}"; then
  record_check BLOCKED "env-update-mode" "plan contains a destructive --set-/--clear- env or secret flag; only --update-env-vars/--update-secrets may be used"
  plan_blocked=1
else
  record_check PASS "env-update-mode" "plan uses only non-destructive --update-env-vars/--update-secrets (existing variables and secret bindings are preserved)"
fi

# Image references must always carry the full 40-character SHA.
bad_tag=0
while IFS= read -r ref; do
  [[ "${ref}" == "milo-api:${SHA}" || "${ref}" == "milo-worker:${SHA}" ]] || bad_tag=1
done < <(grep -Eo 'milo-(api|worker):[A-Za-z0-9_.-]+' <<< "${plan}" || true)
if [[ "${bad_tag}" -eq 1 ]]; then
  record_check BLOCKED "image-tags" "plan contains an image reference that is not tagged with the full release SHA ${SHA}"
  plan_blocked=1
else
  record_check PASS "image-tags" "every API and worker image reference uses the full 40-character release SHA"
fi

missing_flags=""
for flag in JOB_LAUNCHER=disabled MILO_ENABLE_RUN_CREATION=false MILO_ENABLE_PROPOSAL_MUTATIONS=false \
  MILO_ENABLE_PROPOSAL_READS=false MILO_ENABLE_RUN_CANCELLATION=false MILO_ENABLE_EXECUTION_CONTROL=false \
  MILO_ENABLE_PAID_EXECUTION=false; do
  grep -Fq -- "${flag}" <<< "${plan}" || missing_flags="${missing_flags} ${flag}"
done
if [[ -n "${missing_flags}" ]]; then
  record_check BLOCKED "stage-a-flags" "plan omits required Stage A variables:${missing_flags}"
  plan_blocked=1
else
  record_check PASS "stage-a-flags" "plan pins JOB_LAUNCHER=disabled and every MILO_ENABLE_* execution flag to false"
fi

# The provider key is worker-only: it must never appear in an API command.
api_deploy_block="$(sed -n '/gcloud run deploy /,/^$/p' <<< "${plan_commands}")"
if grep -Eq 'KIMI_API_KEY|MOONSHOT_API_KEY' <<< "${api_deploy_block}"; then
  record_check BLOCKED "provider-key-scope" "plan binds a provider API key to the Cloud Run API service; provider keys are worker-only"
  plan_blocked=1
else
  record_check PASS "provider-key-scope" "no provider API key is bound to the API service (worker-only)"
fi

# Only indented command lines count; the prose explicitly forbidding the
# command must not trip the check.
if grep -Eq 'gcloud run jobs execute' <<< "${plan_commands}"; then
  record_check BLOCKED "no-worker-execution" "plan contains a worker execution command; deployment must never execute the worker"
  plan_blocked=1
else
  record_check PASS "no-worker-execution" "plan never executes the worker job (deploy and describe only)"
fi

worker_deploy_line="$(grep -n 'gcloud run jobs deploy ' <<< "${plan}" | head -n 1 | cut -d: -f1)"
api_deploy_line="$(grep -n 'gcloud run deploy ' <<< "${plan}" | head -n 1 | cut -d: -f1)"
if [[ -n "${worker_deploy_line}" && -n "${api_deploy_line}" && "${worker_deploy_line}" -lt "${api_deploy_line}" ]]; then
  record_check PASS "deploy-order" "worker job is deployed before the API service"
else
  record_check BLOCKED "deploy-order" "the worker job must be deployed before the API service"
  plan_blocked=1
fi

if [[ "${plan_blocked}" -eq 1 ]]; then
  finish_checks "generate-deployment-plan" "${JSON_OUTPUT}"
  exit $?
fi

printf '%s\n' "${plan}"
if [[ -n "${OUTPUT}" ]]; then
  printf '%s\n' "${plan}" > "${OUTPUT}"
  record_check PASS "plan" "deployment plan written to ${OUTPUT}"
else
  record_check PASS "plan" "deployment plan generated (stdout)"
fi
record_check MANUAL "execute" "every command above is executed manually by the operator in the listed order; this script never deploys"

finish_checks "generate-deployment-plan" "${JSON_OUTPUT}"

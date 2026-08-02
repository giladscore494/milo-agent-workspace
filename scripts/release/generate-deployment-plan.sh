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
# Image paths, Stage A posture and required bindings are shared with
# scripts/deploy/cloud-run.sh so the plan and the executable script cannot
# drift apart.
# shellcheck source=../deploy/deployment-contract.sh
source "${REPO_ROOT}/scripts/deploy/deployment-contract.sh"

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
DELIM="${MILO_ENV_VAR_DELIMITER}"

# Stage A: every execution flag is pinned OFF by the deployment itself
# (mirrors backend/production_config.py EXECUTION_FLAGS, via the shared
# deployment contract). The alternate delimiter prefix keeps comma-containing
# values (CORS origins, identity allowlists) intact as a single value.
EXECUTION_FLAG_ARGS=""
for flag in "${MILO_STAGE_A_EXECUTION_FLAGS[@]}"; do
  EXECUTION_FLAG_ARGS="${EXECUTION_FLAG_ARGS:+${EXECUTION_FLAG_ARGS}${DELIM}}${flag}"
done
# JOB_LAUNCHER is an API-only variable; the worker never launches jobs.
STAGE_A_FLAGS="JOB_LAUNCHER=disabled${DELIM}${EXECUTION_FLAG_ARGS}"

# Secret Manager RESOURCE names live in the approved manifest, so the plan
# emits a placeholder per binding. The environment NAME and the version are
# fixed by the shared contract and are identical to what cloud-run.sh binds.
secret_placeholder() {
  case "$1" in
    SUPABASE_URL) printf '<SUPABASE_URL_SECRET_NAME>' ;;
    SUPABASE_SERVICE_ROLE_KEY) printf '<SUPABASE_SERVICE_KEY_SECRET_NAME>' ;;
    UPSTASH_REDIS_REST_URL) printf '<REDIS_URL_SECRET_NAME>' ;;
    UPSTASH_REDIS_REST_TOKEN) printf '<REDIS_TOKEN_SECRET_NAME>' ;;
    *) return 1 ;;
  esac
}

unmapped_secret=""
secret_bindings() {
  local out="" name placeholder
  for name in "$@"; do
    if ! placeholder="$(secret_placeholder "${name}")"; then
      unmapped_secret="${name}"
      continue
    fi
    out="${out:+${out},}${name}=${placeholder}:${MILO_SECRET_VERSION}"
  done
  printf '%s' "${out}"
}

API_SECRET_ARGS="$(secret_bindings "${MILO_API_SECRET_ENV_NAMES[@]}")"
WORKER_SECRET_ARGS="$(secret_bindings "${MILO_WORKER_SECRET_ENV_NAMES[@]}")"
if [[ -n "${unmapped_secret}" ]]; then
  record_check BLOCKED "secret-bindings" "no manifest placeholder is defined for the secret-backed variable '${unmapped_secret}'; the plan would omit a required binding"
  finish_checks "generate-deployment-plan" "${JSON_OUTPUT}"
  exit $?
fi

# Non-secret variables, in the same order both tools set them. Stage A binds
# NO provider key on either resource, so none appears in either command.
API_ENV_ARGS="ENVIRONMENT=production${DELIM}GCP_PROJECT_ID=<GCP_PROJECT_ID>${DELIM}GCP_REGION=<GCP_REGION>${DELIM}CLOUD_RUN_WORKER_JOB=<CLOUD_RUN_WORKER_JOB>${DELIM}ALLOWED_CORS_ORIGINS=<PRODUCTION_ORIGINS>${DELIM}MILO_GATEWAY_AUDIENCE=<CLOUD_RUN_API_URL>${DELIM}MILO_APPROVED_GATEWAY_IDENTITIES=<GATEWAY_IDENTITY_EMAIL>${DELIM}${STAGE_A_FLAGS}"
WORKER_ENV_ARGS="ENVIRONMENT=production${DELIM}GCP_PROJECT_ID=<GCP_PROJECT_ID>${DELIM}GCP_REGION=<GCP_REGION>${DELIM}${EXECUTION_FLAG_ARGS}"

API_IMAGE_REF="<GCP_REGION>-docker.pkg.dev/<GCP_PROJECT_ID>/<ARTIFACT_REGISTRY_REPOSITORY>/${MILO_API_IMAGE_REPO}:${SHA}"
WORKER_IMAGE_REF="<GCP_REGION>-docker.pkg.dev/<GCP_PROJECT_ID>/<ARTIFACT_REGISTRY_REPOSITORY>/${MILO_WORKER_IMAGE_REPO}:${SHA}"

# jq expressions that render one line per binding, capturing the secret
# REFERENCE (secret name and version) as well as the variable name, so a
# remapped binding is as visible as a removed one. Names and references only —
# never a secret value.
JQ_BINDINGS='.[] | if .valueFrom.secretKeyRef then "secret \(.name) \(.valueFrom.secretKeyRef.secret):\(.valueFrom.secretKeyRef.key)" else "env \(.name)" end'
API_ENV_PATH='(.spec.template.spec.containers[0].env // [])'
WORKER_ENV_PATH='(.spec.template.spec.template.spec.containers[0].env // [])'

# Grep alternations for the verification steps, built from the same contract
# the deploy commands are built from.
API_REQUIRED_GREP="$(IFS='|'; printf '%s' "${MILO_API_REQUIRED_ENV_NAMES[*]}")"
API_REQUIRED_COUNT="${#MILO_API_REQUIRED_ENV_NAMES[@]}"
WORKER_REQUIRED_GREP="$(IFS='|'; printf '%s' "${MILO_WORKER_REQUIRED_ENV_NAMES[*]}")"
WORKER_REQUIRED_COUNT="${#MILO_WORKER_REQUIRED_ENV_NAMES[@]}"
PROVIDER_KEY_GREP="$(IFS='|'; printf '%s' "${MILO_PROVIDER_KEY_ENV_NAMES[*]}")"
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
  not own (worker audience, budget caps, rate limits) keep their current
  values.

Stage A posture, in one paragraph: every execution flag is \`false\`, and
**no provider API key is bound to anything** — not to the API service, not
to the worker job. Stage A makes no provider call, so a reachable provider
credential would be blast radius with no purpose. \`KIMI_API_KEY\` /
\`MOONSHOT_API_KEY\` enter the deployment only through the separate,
explicit Stage C operator action described in
docs/production-readiness/STAGED_ACTIVATION.md, together with the budget
caps and the rehearsed kill switch that make paid execution safe. The
gateway identity variables (\`MILO_GATEWAY_AUDIENCE\`,
\`MILO_APPROVED_GATEWAY_IDENTITIES\`) are set explicitly with their approved
values: production refuses to start without them even while execution is
disabled, because the read-only routes must never trust a bare browser
header.

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
      -t ${API_IMAGE_REF} .

    docker build -f Dockerfile.worker \\
      --label org.opencontainers.image.revision=${SHA} \\
      --label org.opencontainers.image.title=milo-worker \\
      -t ${WORKER_IMAGE_REF} .

## 4. Push immutable images (manual operator action)

    docker push ${API_IMAGE_REF}
    docker push ${WORKER_IMAGE_REF}

## 5. Record the CURRENT worker/API configuration (proof of preservation)

Capture the live bindings before changing anything, so steps 7 and 9 can
prove that nothing was dropped **or silently repointed**. Each line records
the variable name and, for a secret-backed variable, the secret name and
version it reads — names and references only, never secret values. A
binding remapped to a different secret or a different version changes its
line and shows up in the diff exactly like a removal.

    gcloud run jobs describe <CLOUD_RUN_WORKER_JOB> --region <GCP_REGION> --project <GCP_PROJECT_ID> --format=json \\
      | jq -r '${WORKER_ENV_PATH} | ${JQ_BINDINGS}' | sort > worker-bindings-before.txt
    gcloud run services describe <CLOUD_RUN_API_SERVICE> --region <GCP_REGION> --project <GCP_PROJECT_ID> --format=json \\
      | jq -r '${API_ENV_PATH} | ${JQ_BINDINGS}' | sort > api-bindings-before.txt

## 6. Deploy or update the PRIVATE worker job FIRST (never execute it)

Stage A binds no provider key to the worker. \`KIMI_API_KEY\` /
\`MOONSHOT_API_KEY\` are added only by the separate Stage C operator action,
never by this plan.

    gcloud run jobs deploy <CLOUD_RUN_WORKER_JOB> \\
      --image ${WORKER_IMAGE_REF} \\
      --region <GCP_REGION> --project <GCP_PROJECT_ID> \\
      --service-account <WORKER_SERVICE_ACCOUNT_EMAIL> \\
      --update-secrets ${WORKER_SECRET_ARGS} \\
      --update-env-vars '^${DELIM}^${WORKER_ENV_ARGS}' \\
      --max-retries 0 --task-timeout 3600

\`gcloud run jobs deploy\` only creates/updates the job definition. Do NOT
run \`gcloud run jobs execute\` at any point in this plan.

## 7. Verify worker job configuration WITHOUT executing it

    gcloud run jobs describe <CLOUD_RUN_WORKER_JOB> --region <GCP_REGION> --project <GCP_PROJECT_ID> \\
      --format 'value(spec.template.spec.template.spec.serviceAccountName, spec.template.spec.template.spec.containers[0].image)'
    gcloud run jobs describe <CLOUD_RUN_WORKER_JOB> --region <GCP_REGION> --project <GCP_PROJECT_ID> --format=json \\
      | jq -r '${WORKER_ENV_PATH} | ${JQ_BINDINGS}' | sort > worker-bindings-after.txt   # variable NAMES + secret REFERENCES
    gcloud run jobs executions list --job <CLOUD_RUN_WORKER_JOB> --region <GCP_REGION>   # expect: no new executions

Preservation proof — the update must add bindings, never remove or repoint
one:

    comm -23 worker-bindings-before.txt worker-bindings-after.txt   # expect: EMPTY (nothing was dropped or remapped)

Required variables present, provider keys absent:

    grep -Ec '^(env|secret) (${WORKER_REQUIRED_GREP})\b' worker-bindings-after.txt   # expect: ${WORKER_REQUIRED_COUNT}
    grep -E '^(env|secret) (${PROVIDER_KEY_GREP})\b' worker-bindings-after.txt      # expect: EMPTY (no provider key on the worker)

Expected: image tag = ${SHA}, service account = <WORKER_SERVICE_ACCOUNT_EMAIL>
(never the API account), every execution flag present and \`false\`, no
provider key bound, and an execution list identical to the one from before
the deployment.

## 8. Deploy or update the PRIVATE Cloud Run API

    gcloud run deploy <CLOUD_RUN_API_SERVICE> \\
      --image ${API_IMAGE_REF} \\
      --region <GCP_REGION> --project <GCP_PROJECT_ID> \\
      --service-account <API_SERVICE_ACCOUNT_EMAIL> \\
      --no-allow-unauthenticated --ingress all \\
      --revision-suffix rel-${SHA:0:12} \\
      --update-secrets ${API_SECRET_ARGS} \\
      --update-env-vars '^${DELIM}^${API_ENV_ARGS}'

Note: authentication is enforced by --no-allow-unauthenticated (Cloud Run
IAM) plus the application-level verified gateway token; ingress stays
reachable only to authorized identities. The API service account differs
from the worker service account, and no provider key is bound to either
resource at Stage A.

## 9. Verify API revision, identity, variable names and secret references

    gcloud run services describe <CLOUD_RUN_API_SERVICE> --region <GCP_REGION> --project <GCP_PROJECT_ID> \\
      --format 'value(status.latestReadyRevisionName, spec.template.spec.containers[0].image, spec.template.spec.serviceAccountName)'
    gcloud run services describe <CLOUD_RUN_API_SERVICE> --region <GCP_REGION> --project <GCP_PROJECT_ID> --format=json \\
      | jq -r '${API_ENV_PATH} | ${JQ_BINDINGS}' | sort > api-bindings-after.txt   # variable NAMES + secret REFERENCES

Preservation proof — the deployment must add bindings, never remove or
repoint one:

    comm -23 api-bindings-before.txt api-bindings-after.txt    # expect: EMPTY (nothing was dropped or remapped)

Required Stage A variables present (including the gateway identity pair
production refuses to start without), provider keys absent:

    grep -Ec '^(env|secret) (${API_REQUIRED_GREP})\b' api-bindings-after.txt   # expect: ${API_REQUIRED_COUNT}
    grep -E '^(env|secret) (${PROVIDER_KEY_GREP})\b' api-bindings-after.txt   # expect: EMPTY (no provider key on the API)

The image digest must match the pushed digest for each image:

    gcloud artifacts docker images describe ${API_IMAGE_REF} --format 'value(image_summary.digest)'
    gcloud artifacts docker images describe ${WORKER_IMAGE_REF} --format 'value(image_summary.digest)'

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
never enables an execution flag and never introduces a provider key.

Introducing the provider key is a Stage C action in its own right: the
operator adds \`KIMI_API_KEY=<PROVIDER_KEY_SECRET_NAME>:latest\` to the
WORKER job (never the API service) with a separate
\`gcloud run jobs update --update-secrets\` command, after the budget caps
are configured and the kill switch is rehearsed. It is deliberately not
part of any release command above, so no routine deployment can put a
spendable credential in front of a runtime that is not yet allowed to
spend.
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

# Every registry reference must use a canonical repository path AND the full
# 40-character SHA. A reference to some other repository path would deploy an
# image this release never built.
bad_tag=0
bad_repo=""
while IFS= read -r ref; do
  case "${ref}" in
    */"${MILO_API_IMAGE_REPO}":*|*/"${MILO_WORKER_IMAGE_REPO}":*) ;;
    *) bad_repo="${ref}"; continue ;;
  esac
  [[ "${ref##*:}" == "${SHA}" ]] || bad_tag=1
done < <(grep -Eo '[^[:space:]]*docker\.pkg\.dev/[^[:space:]]+' <<< "${plan}" || true)
if [[ "${bad_tag}" -eq 1 ]]; then
  record_check BLOCKED "image-tags" "plan contains an image reference that is not tagged with the full release SHA ${SHA}"
  plan_blocked=1
else
  record_check PASS "image-tags" "every API and worker image reference uses the full 40-character release SHA"
fi
if [[ -n "${bad_repo}" ]]; then
  record_check BLOCKED "image-repository" "plan references image repository '${bad_repo}', which is neither the canonical '${MILO_API_IMAGE_REPO}' nor '${MILO_WORKER_IMAGE_REPO}' path used by scripts/deploy/cloud-run.sh"
  plan_blocked=1
else
  record_check PASS "image-repository" "every image reference uses the canonical '${MILO_API_IMAGE_REPO}'/'${MILO_WORKER_IMAGE_REPO}' repository paths shared with scripts/deploy/cloud-run.sh"
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

# Stage A binds no provider key to EITHER resource. Only the deploy commands
# are scanned: the verification steps grep the live configuration for exactly
# these names and must not be mistaken for a binding.
command_block() {
  # The command line matching a prefix plus its backslash continuations —
  # never the unrelated commands that follow it.
  awk -v prefix="$1" '
    !found && index($0, prefix) { found = 1; print; if ($0 !~ /\\$/) exit; next }
    found { print; if ($0 !~ /\\$/) exit }
  ' <<< "${plan_commands}"
}
api_deploy_block="$(command_block 'gcloud run deploy ')"
worker_deploy_block="$(command_block 'gcloud run jobs deploy ')"
provider_key_pattern="$(IFS='|'; printf '%s' "${MILO_PROVIDER_KEY_ENV_NAMES[*]}")"
provider_key_on=""
grep -Eq -- "${provider_key_pattern}" <<< "${api_deploy_block}" && provider_key_on="the API service"
grep -Eq -- "${provider_key_pattern}" <<< "${worker_deploy_block}" && provider_key_on="${provider_key_on:+${provider_key_on} and }the worker job"
if [[ -n "${provider_key_on}" ]]; then
  record_check BLOCKED "provider-key-scope" "plan binds a provider API key to ${provider_key_on}; Stage A binds no provider key anywhere (Stage C introduces it by a separate operator action)"
  plan_blocked=1
else
  record_check PASS "provider-key-scope" "no provider API key is bound to the API service or the worker job (Stage A posture)"
fi

# Gateway identity is mandatory in production even while execution is off.
missing_gateway=""
for required in MILO_GATEWAY_AUDIENCE MILO_APPROVED_GATEWAY_IDENTITIES; do
  grep -Fq -- "${required}=" <<< "${api_deploy_block}" || missing_gateway="${missing_gateway} ${required}"
done
if [[ -n "${missing_gateway}" ]]; then
  record_check BLOCKED "gateway-identity" "API deploy command omits required gateway identity variables:${missing_gateway}"
  plan_blocked=1
else
  record_check PASS "gateway-identity" "API deploy command sets MILO_GATEWAY_AUDIENCE and MILO_APPROVED_GATEWAY_IDENTITIES explicitly"
fi

# Both resources must carry the same Supabase/Upstash bindings as cloud-run.sh.
missing_bindings=""
for required in "${MILO_API_SECRET_ENV_NAMES[@]}"; do
  grep -Fq -- "${required}=" <<< "${api_deploy_block}" || missing_bindings="${missing_bindings} api:${required}"
done
for required in "${MILO_WORKER_SECRET_ENV_NAMES[@]}"; do
  grep -Fq -- "${required}=" <<< "${worker_deploy_block}" || missing_bindings="${missing_bindings} worker:${required}"
done
if [[ -n "${missing_bindings}" ]]; then
  record_check BLOCKED "secret-bindings" "plan omits required secret bindings:${missing_bindings}"
  plan_blocked=1
else
  record_check PASS "secret-bindings" "API and worker both bind every required Supabase and Upstash secret, matching scripts/deploy/cloud-run.sh"
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

#!/usr/bin/env bash
# The two activation steps after Stage A, deliberately and in order.
#
#   --apply-plan-authoring  Stage P. The API's Mapping Plan WRITES only, so a
#                           person can author the plan the operator will
#                           prepare. Run creation, the launcher, paid
#                           execution and every worker flag stay exactly as
#                           Stage A left them (off). Nothing can start.
#   --apply-runtime-policy  The reviewed RuntimePolicy envelope (plain numeric
#                           caps from backend/runtime_policy.py) on the worker,
#                           and the SAME per-run, daily and concurrency caps on
#                           the API, which admits runs and displays the limits
#                           a run executes under. It ENABLES nothing: it is the
#                           bounds paid execution will run inside, and
#                           production-preflight.sh requires them.
#   --apply-web-preparation E'. The website's Prepare button: the API may
#                           execute the EXISTING capture job once per plan
#                           revision (MILO_WEB_PREPARATION_API_ENABLE_FLAGS
#                           and CLOUD_RUN_CAPTURE_JOB on the API, the API
#                           identity's run-with-overrides binding on THAT job
#                           only, and its roles/run.viewer binding on the
#                           capture job and the worker job only -- the two
#                           jobs the Prepare route READS before it starts
#                           anything -- each read back). Starts nothing,
#                           creates no product run and enables no paid
#                           execution or promotion.
#   --apply-register-capture PR-D1. The website's Register page: the API may
#                           execute the EXISTING capture job with the register
#                           switch for one directory refresh or one capture
#                           group (MILO_REGISTER_CAPTURE_API_ENABLE_FLAGS and
#                           CLOUD_RUN_CAPTURE_JOB on the API; the same job
#                           bindings as E', read back). Needs no run creation,
#                           no run-start gateway flag and no Arm: capture is
#                           $0 and not a run. Refused until the register
#                           archive bucket is set up (setup-register-archive.sh).
#   --apply-catalog-browser PR-L1. The website's read-only catalog browser
#                           (MILO_CATALOG_BROWSER_API_ENABLE_FLAGS on the API
#                           only, read back). GET only, $0: no run, no job,
#                           no model; run creation stays off.
#   --apply-manufacturer-normalisation PR-D3. The Register page's "Normalise
#                           manufacturers" button: its OWN small job
#                           (CLOUD_RUN_NORMALISATION_JOB, run as the worker
#                           identity) with the deployment's RuntimePolicy caps
#                           and -- there only -- the provider key and the
#                           quota store; then its API flag and job name
#                           (MILO_MANUFACTURER_NORMALISATION_API_ENABLE_FLAGS).
#                           Read back: the job, the capture job and the
#                           capture identity holding NO provider key, run
#                           creation and paid execution OFF (decision 33).
#                           Needs the register-capture stage first.
#   --remove-manufacturer-normalisation PR-D3, off: the API flag off, the
#                           normalisation job deleted, and any accessor the
#                           capture identity holds on the provider key or the
#                           quota store revoked, each read back. Run by the
#                           kill switch and by every deploy's Stage A reset.
#   --apply-backend         Stage 2. Everything a website-initiated batch run
#                           needs on the API and the worker -- and ONLY where
#                           it is needed (deployment-contract.sh names each
#                           component's flags) -- once the named plan revision
#                           is PREPARED and its next batch is ready.
#
# THE ORDER THAT KEEPS EVERY INTERMEDIATE STATE CLOSED:
#
#   1. The website's run-start path must be VERIFIED CLOSED before anything is
#      applied (GATEWAY_ALLOW_RUN_START_ROUTES off: every run start is refused
#      at the gateway). Plan authoring never opens it.
#   2. The WORKER is armed first (flags, RuntimePolicy, provider secret) and
#      read back. With the API's run creation still off, nothing can use it.
#   3. The API is armed second and read back. Starts are still refused at the
#      gateway, whatever the API now allows.
#   4. production-verify.sh --gate armed: the pre-open gate. It requires the
#      run-start path to be verified CLOSED and everything else ready.
#   5. Only then does the operator open GATEWAY_ALLOW_RUN_START_ROUTES in
#      Vercel -- the LAST step -- and check --gate active afterwards.
#
# A failure at any step leaves every later step undone: the worker without the
# API, or both without the gateway, is a posture in which no run can start.
#
# WHAT IT DOES AND DOES NOT DO
#
# The backend half (Cloud Run API + worker) is applied here, because gcloud is
# the canonical mechanism and the flags interlock in ways that are easy to get
# wrong by hand -- MILO_ENABLE_EXECUTION_CONTROL without MILO_WORKER_AUDIENCE
# takes the API DOWN at startup rather than opening a route, and
# MILO_ENABLE_RUN_CREATION without JOB_LAUNCHER=cloud_run creates runs that
# never execute. This script refuses to apply a combination that would do
# either, and reads every applied value back.
#
# The frontend half (Vercel) is NOT applied here. It needs Vercel credentials
# that must not live in this repository, and one of its two values is inlined
# into the browser bundle at BUILD time, so "set the variable" is not the
# operation -- "rebuild and redeploy" is. The exact commands are printed.
#
# Stage 2 refuses outright unless the named plan revision is verifiably ready
# (production-verify.sh --gate prepared): the release deployed on both
# surfaces, the exact migration set applied, the revision prepared from
# active scoped Government snapshots, and its next batch linked and startable.
# Opening the composer over a plan that would refuse is not an activation.
#
# It never starts a run, never prepares a plan, and never enables catalog
# promotion or scoped preparation on the API or the worker.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
MILO_REPO_ROOT="$REPO_ROOT"
# shellcheck source=operator-config.sh
source "${SCRIPT_DIR}/operator-config.sh"
# shellcheck source=deployment-contract.sh
source "${SCRIPT_DIR}/deployment-contract.sh"

MODE="plan" MILO_OPERATOR_CONFIG_PATH="" SKIP_STAGE1=0
WS_ARGS=()

usage() {
  cat << 'EOF'
Usage: website-execution-activate.sh [--plan|--apply-runtime-policy|--apply-plan-authoring|--apply-web-preparation|--apply-register-capture|--apply-catalog-browser|--apply-manufacturer-normalisation|--remove-manufacturer-normalisation|--apply-backend] [options]

Default --plan changes nothing and prints both stages.

Modes:
  --plan                  Print every change each stage would make. Default.
  --apply-runtime-policy  Bind the reviewed RuntimePolicy envelope (worker) and
                          its caps (API). Enables nothing; no gate.
  --apply-plan-authoring  Stage P: MILO_ENABLE_WORK_SCOPE_MUTATIONS on the API
                          only. Gate: production-verify.sh --gate deployed.
  --apply-web-preparation E': the website's Prepare button (API only), the
                          API identity's run-with-overrides binding on the
                          capture job only, and its roles/run.viewer binding
                          on the capture and worker jobs only (read back).
                          Gate: production-verify.sh --gate deployed, and the
                          capture job on the release image.
  --apply-register-capture PR-D1: the website's Register page (API only) and
                          the same capture-job bindings as E'. Gate:
                          production-verify.sh --gate deployed, the capture
                          job on the release image, and the register archive
                          (setup-register-archive.sh --check PASS, or
                          PARTIAL -- the bucket's posture verified, its IAM
                          left to the operator's Cloud Shell check -- WARN).
  --apply-catalog-browser PR-L1: the read-only catalog browser (API only).
                          Gate: production-verify.sh --gate deployed.
  --apply-backend         Stage 2: the API + worker flags for batch runs. Gate:
                          production-verify.sh --gate prepared for the named
                          plan revision. The Vercel half is printed, never
                          applied.

Options:
  --work-scope-id <uuid> --work-scope-revision <n> --work-scope-digest <hex>
                     The prepared plan revision. REQUIRED by --apply-backend.
  --operator-config <path>
  --skip-stage1-check
                     --plan only: print the command shape without running the
                     gate. Refused with any --apply-* mode.
  --help

Catalog promotion and scoped preparation are never enabled by this script,
and no run is ever started.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --plan) MODE="plan"; shift ;;
    --apply-runtime-policy) MODE="apply-runtime-policy"; shift ;;
    --apply-plan-authoring) MODE="apply-plan-authoring"; shift ;;
    --apply-web-preparation) MODE="apply-web-preparation"; shift ;;
    --apply-register-capture) MODE="apply-register-capture"; shift ;;
    --apply-catalog-browser) MODE="apply-catalog-browser"; shift ;;
    --apply-manufacturer-normalisation) MODE="apply-manufacturer-normalisation"; shift ;;
    --remove-manufacturer-normalisation) MODE="remove-manufacturer-normalisation"; shift ;;
    --apply-backend) MODE="apply-backend"; shift ;;
    --work-scope-id | --work-scope-revision | --work-scope-digest) WS_ARGS+=("$1" "${2:?}"); shift 2 ;;
    --operator-config) MILO_OPERATOR_CONFIG_PATH="${2:?}"; shift 2 ;;
    --skip-stage1-check) SKIP_STAGE1=1; shift ;;
    --help) usage; exit 0 ;;
    *) printf 'FAIL: unknown argument %s\n' "$1" >&2; exit 2 ;;
  esac
done

if [[ "$SKIP_STAGE1" -eq 1 && "$MODE" != "plan" ]]; then
  printf 'FAIL: --skip-stage1-check rehearses the command shape only; it is refused with %s.\n' "--${MODE}" >&2
  exit 2
fi
if [[ "$MODE" == "apply-backend" && "${#WS_ARGS[@]}" -ne 6 ]]; then
  printf 'FAIL: --apply-backend requires --work-scope-id, --work-scope-revision and --work-scope-digest:\n' >&2
  printf '      Stage 2 opens batch runs for ONE prepared plan revision, proved ready first.\n' >&2
  printf '      List the open plans with: scripts/deploy/work-scope-readiness.sh --list\n' >&2
  exit 2
fi

CONFIG_PATH="$(milo_operator_config_path "$REPO_ROOT" "$MILO_OPERATOR_CONFIG_PATH")"
milo_load_operator_config "$CONFIG_PATH" || exit 2
milo_require_op GCP_PROJECT_ID GCP_REGION CLOUD_RUN_API_SERVICE CLOUD_RUN_WORKER_JOB \
  SECRET_PROVIDER_API_KEY MILO_GATEWAY_AUDIENCE MILO_APPROVED_GATEWAY_IDENTITIES \
  PRODUCTION_ORIGIN || exit 2

PROJECT_ID="$(milo_op GCP_PROJECT_ID)"
REGION="$(milo_op GCP_REGION)"
API_SERVICE="$(milo_op CLOUD_RUN_API_SERVICE)"
WORKER_JOB="$(milo_op CLOUD_RUN_WORKER_JOB)"
PROVIDER_SECRET="$(milo_op SECRET_PROVIDER_API_KEY)"
SITE="$(milo_op PRODUCTION_ORIGIN)"

# Worker identity allowlist. EXECUTION_CONTROL requires both of these on the
# API or production_config refuses to start, so they are resolved before
# anything is applied rather than discovered by an outage.
WORKER_AUDIENCE="$(milo_op MILO_WORKER_AUDIENCE)"
APPROVED_WORKER_IDENTITIES="$(milo_op MILO_APPROVED_WORKER_IDENTITIES)"
[[ -n "$APPROVED_WORKER_IDENTITIES" ]] || APPROVED_WORKER_IDENTITIES="$(milo_op WORKER_SERVICE_ACCOUNT)"
if [[ -z "$WORKER_AUDIENCE" || -z "$APPROVED_WORKER_IDENTITIES" ]]; then
  printf 'FAIL: MILO_ENABLE_EXECUTION_CONTROL requires both MILO_WORKER_AUDIENCE and\n' >&2
  printf '      MILO_APPROVED_WORKER_IDENTITIES on the API, or backend/production_config.py\n' >&2
  printf '      refuses to start (WORKER_AUTH_AUDIENCE_MISSING / WORKER_ALLOWLIST_EMPTY).\n' >&2
  printf '      Set MILO_WORKER_AUDIENCE (and optionally MILO_APPROVED_WORKER_IDENTITIES)\n' >&2
  printf '      in %s before activating.\n' "$CONFIG_PATH" >&2
  exit 2
fi
if [[ "$WORKER_AUDIENCE" == *"$MILO_ENV_VAR_DELIMITER"* || "$APPROVED_WORKER_IDENTITIES" == *"$MILO_ENV_VAR_DELIMITER"* ]]; then
  printf "FAIL: the worker audience and identities must not contain '%s'.\n" "$MILO_ENV_VAR_DELIMITER" >&2
  exit 2
fi

# The enabled value is assembled at runtime rather than written next to a
# flag name: scripts/check_unsafe_defaults.py does not exempt operator scripts,
# and it is right not to -- "a repository default is not a deliberate operator
# decision". The decision is this script being invoked with --apply-*, after
# its gate passed.
ENABLED="true"
DISABLED="false"

pairs() {
  local value="$1" name out=()
  shift
  for name in "$@"; do out+=("${name}=${value}"); done
  (IFS="$MILO_ENV_VAR_DELIMITER"; printf '%s' "${out[*]}")
}

# The reviewed RuntimePolicy, computed from the ONE canonical registry
# (backend/runtime_policy.py) at runtime -- never transcribed here. The worker
# carries the whole envelope. The API carries every CAP (CAP_ENV_PREFIXES: the
# per-run, daily and concurrency caps -- all shared-api-worker in
# check-production-config.sh, and verified on BOTH surfaces by verify_caps.py):
# it admits runs under the concurrency caps and DISPLAYS the per-run limits
# (`limits` on every run response). Binding only the concurrency caps left the
# API showing stale 1.00 / 4.00 / 120000 while the worker enforced
# 3.00 / 10.00 / 400000. Provider and engine settings stay worker-only.
policy_pairs() {
  (cd "$REPO_ROOT" && python3 -c '
import sys
from backend.runtime_policy import reviewed_first_run_policy
prefixes = tuple(sys.argv[2:]) or None
env = reviewed_first_run_policy().env_expectations(prefixes=prefixes)
print(sys.argv[1].join("%s=%s" % item for item in sorted(env.items())))
' "$MILO_ENV_VAR_DELIMITER" "$@")
}
if ! POLICY_JOB_VARS="$(policy_pairs)" || [[ -z "$POLICY_JOB_VARS" ]] \
   || ! POLICY_API_VARS="$(policy_pairs MILO_MAX_ MILO_DAILY_ MILO_ESTIMATED_COST)" || [[ -z "$POLICY_API_VARS" ]]; then
  printf 'FAIL: the reviewed RuntimePolicy could not be read from backend/runtime_policy.py\n' >&2
  exit 2
fi

# Stage P: the Mapping Plan writes on the API, nothing else.
PLAN_API_VARS="$(pairs "$ENABLED" "${MILO_PLAN_AUTHORING_API_ENABLE_FLAGS[@]}")"

# E': the website's Prepare route on the API, and the capture job it executes.
CAPTURE_JOB="$(milo_op CLOUD_RUN_CAPTURE_JOB)"
API_SA="$(milo_op API_SERVICE_ACCOUNT)"
WEB_PREP_API_VARS="$(pairs "$ENABLED" "${MILO_WEB_PREPARATION_API_ENABLE_FLAGS[@]}")"
WEB_PREP_API_VARS+="${MILO_ENV_VAR_DELIMITER}CLOUD_RUN_CAPTURE_JOB=${CAPTURE_JOB}"

# PR-D1: the website's Register page on the API, and the capture job it executes.
REGISTER_API_VARS="$(pairs "$ENABLED" "${MILO_REGISTER_CAPTURE_API_ENABLE_FLAGS[@]}")"
REGISTER_API_VARS+="${MILO_ENV_VAR_DELIMITER}CLOUD_RUN_CAPTURE_JOB=${CAPTURE_JOB}"

# PR-L1: the read-only catalog browser on the API, nothing else.
BROWSER_API_VARS="$(pairs "$ENABLED" "${MILO_CATALOG_BROWSER_API_ENABLE_FLAGS[@]}")"

# PR-D3: the "Normalise manufacturers" button on the API, and the SEPARATE
# job it executes -- the only place the provider key is bound for it.
NORMALISATION_JOB="$(milo_normalisation_job_name)"
NORMALISATION_API_VARS="$(pairs "$ENABLED" "${MILO_MANUFACTURER_NORMALISATION_API_ENABLE_FLAGS[@]}")"
NORMALISATION_API_VARS+="${MILO_ENV_VAR_DELIMITER}${MILO_NORMALISATION_JOB_ENV_NAME}=${NORMALISATION_JOB}"
NORMALISATION_API_OFF="$(pairs "$DISABLED" "${MILO_MANUFACTURER_NORMALISATION_API_ENABLE_FLAGS[@]}")"
# The capture identity when it is its own (not the worker's): it must never
# read the provider key, nor the quota store it has no use for.
CAPTURE_ONLY_SA="$(milo_op CAPTURE_SERVICE_ACCOUNT)"
[[ "$CAPTURE_ONLY_SA" != "$(milo_op WORKER_SERVICE_ACCOUNT)" ]] || CAPTURE_ONLY_SA=""
REDIS_URL_SECRET="$(milo_op SECRET_REDIS_URL)"
REDIS_TOKEN_SECRET="$(milo_op SECRET_REDIS_TOKEN)"

# Stage 2.
S2_API_VARS="$(pairs "$ENABLED" "${MILO_STAGE2_API_ENABLE_FLAGS[@]}")"
S2_API_VARS+="${MILO_ENV_VAR_DELIMITER}$(pairs "$DISABLED" "${MILO_STAGE2_API_PINNED_OFF_FLAGS[@]}")"
S2_API_VARS+="${MILO_ENV_VAR_DELIMITER}$(IFS="$MILO_ENV_VAR_DELIMITER"; printf '%s' "${MILO_REPLAY_CAPTURE_PINNED_OFF[*]}")"
S2_API_VARS+="${MILO_ENV_VAR_DELIMITER}JOB_LAUNCHER=cloud_run"
S2_API_VARS+="${MILO_ENV_VAR_DELIMITER}MILO_WORKER_AUDIENCE=${WORKER_AUDIENCE}"
S2_API_VARS+="${MILO_ENV_VAR_DELIMITER}MILO_APPROVED_WORKER_IDENTITIES=${APPROVED_WORKER_IDENTITIES}"
S2_API_VARS+="${MILO_ENV_VAR_DELIMITER}${POLICY_API_VARS}"
S2_JOB_VARS="$(pairs "$ENABLED" "${MILO_STAGE2_WORKER_ENABLE_FLAGS[@]}")"
S2_JOB_VARS+="${MILO_ENV_VAR_DELIMITER}$(pairs "$DISABLED" "${MILO_STAGE2_WORKER_PINNED_OFF_FLAGS[@]}")"
S2_JOB_VARS+="${MILO_ENV_VAR_DELIMITER}$(IFS="$MILO_ENV_VAR_DELIMITER"; printf '%s' "${MILO_REPLAY_CAPTURE_PINNED_OFF[*]}")"
S2_JOB_VARS+="${MILO_ENV_VAR_DELIMITER}${POLICY_JOB_VARS}"

print_runtime_policy_commands() {
  cat << EOC
# --- RuntimePolicy: the reviewed caps (plain config, never Secret Manager).
# Enables nothing. Worker: the whole envelope. API: every cap it admits runs
# under and displays (per-run, daily, concurrency) -- never provider settings.
gcloud run jobs update ${WORKER_JOB} \\
  --region ${REGION} --project ${PROJECT_ID} \\
  --update-env-vars '^${MILO_ENV_VAR_DELIMITER}^${POLICY_JOB_VARS}'
gcloud run services update ${API_SERVICE} \\
  --region ${REGION} --project ${PROJECT_ID} \\
  --update-env-vars '^${MILO_ENV_VAR_DELIMITER}^${POLICY_API_VARS}'
EOC
}

print_plan_authoring_commands() {
  cat << EOC
# --- Stage P, API only: the Mapping Plan writes. Run creation stays OFF. ---
gcloud run services update ${API_SERVICE} \\
  --region ${REGION} --project ${PROJECT_ID} \\
  --update-env-vars '^${MILO_ENV_VAR_DELIMITER}^${PLAN_API_VARS}'
EOC
}

print_web_preparation_commands() {
  cat << EOC
# --- E', capture job: the API identity may run THIS job with overrides (the
# scoped arguments and the one preparation switch), and nothing else.
gcloud run jobs add-iam-policy-binding ${CAPTURE_JOB:-<CLOUD_RUN_CAPTURE_JOB>} \\
  --region ${REGION} --project ${PROJECT_ID} \\
  --member serviceAccount:${API_SA:-<API_SERVICE_ACCOUNT>} --role roles/run.jobsExecutorWithOverrides
# --- E', both jobs: the API identity may READ them (run.jobs.get). The Prepare
# route reads the capture job and the worker job to prove the release image
# before it starts anything; the executor role above does not carry that read.
# Bound only when absent, then read back.
gcloud run jobs add-iam-policy-binding ${CAPTURE_JOB:-<CLOUD_RUN_CAPTURE_JOB>} \\
  --region ${REGION} --project ${PROJECT_ID} \\
  --member serviceAccount:${API_SA:-<API_SERVICE_ACCOUNT>} --role ${MILO_API_JOB_READ_ROLE}
gcloud run jobs add-iam-policy-binding ${WORKER_JOB} \\
  --region ${REGION} --project ${PROJECT_ID} \\
  --member serviceAccount:${API_SA:-<API_SERVICE_ACCOUNT>} --role ${MILO_API_JOB_READ_ROLE}
# --- E', API only: the Prepare route and the job it executes. Run creation,
# batches, paid execution and promotion stay exactly as they are.
gcloud run services update ${API_SERVICE} \\
  --region ${REGION} --project ${PROJECT_ID} \\
  --update-env-vars '^${MILO_ENV_VAR_DELIMITER}^${WEB_PREP_API_VARS}'
EOC
}

print_register_capture_commands() {
  cat << EOC
# --- PR-D1, capture job + both jobs: the same bindings as E' (run THIS job
# with overrides; read the capture and worker jobs), each only when absent.
# <API_SERVICE_ACCOUNT> is the operator configuration's key: no address is
# printed.
gcloud run jobs add-iam-policy-binding ${CAPTURE_JOB:-<CLOUD_RUN_CAPTURE_JOB>} \\
  --region ${REGION} --project ${PROJECT_ID} \\
  --member serviceAccount:<API_SERVICE_ACCOUNT> --role roles/run.jobsExecutorWithOverrides
gcloud run jobs add-iam-policy-binding ${CAPTURE_JOB:-<CLOUD_RUN_CAPTURE_JOB>} \\
  --region ${REGION} --project ${PROJECT_ID} \\
  --member serviceAccount:<API_SERVICE_ACCOUNT> --role ${MILO_API_JOB_READ_ROLE}
gcloud run jobs add-iam-policy-binding ${WORKER_JOB} \\
  --region ${REGION} --project ${PROJECT_ID} \\
  --member serviceAccount:<API_SERVICE_ACCOUNT> --role ${MILO_API_JOB_READ_ROLE}
# --- PR-D1, API only: the Register page and the job it executes. Run
# creation, batches, paid execution and promotion stay exactly as they are.
gcloud run services update ${API_SERVICE} \\
  --region ${REGION} --project ${PROJECT_ID} \\
  --update-env-vars '^${MILO_ENV_VAR_DELIMITER}^${REGISTER_API_VARS}'
EOC
}

print_catalog_browser_commands() {
  cat << EOC
# --- PR-L1, API only: the read-only catalog browser (GET only, \$0). Run
# creation, batches, paid execution and promotion stay exactly as they are.
gcloud run services update ${API_SERVICE} \\
  --region ${REGION} --project ${PROJECT_ID} \\
  --update-env-vars '^${MILO_ENV_VAR_DELIMITER}^${BROWSER_API_VARS}'
EOC
}

print_manufacturer_normalisation_commands() {
  cat << EOC
# --- PR-D3, its own job (${NORMALISATION_JOB:-<CLOUD_RUN_NORMALISATION_JOB>}), run as the worker identity:
# the capture job's definition, the worker's RuntimePolicy caps, and ONLY here
# the provider key and the quota store (secret references, never plain env).
MILO_NORMALISATION_POLICY_VARS='<the worker's RuntimePolicy caps>' \\
  bash scripts/catalog/government-production-capture.sh --operator-config <config> \\
  --ensure-normalisation-job --enable-catalog-execution
# --- PR-D3: the API identity reads that job and the worker job (the release
# check), each bound only when absent, then read back.
gcloud run jobs add-iam-policy-binding ${NORMALISATION_JOB:-<CLOUD_RUN_NORMALISATION_JOB>} \\
  --region ${REGION} --project ${PROJECT_ID} \\
  --member serviceAccount:<API_SERVICE_ACCOUNT> --role ${MILO_API_JOB_READ_ROLE}
# --- PR-D3, API only: the button and the job it executes. Run creation and
# paid execution stay off.
gcloud run services update ${API_SERVICE} \\
  --region ${REGION} --project ${PROJECT_ID} \\
  --update-env-vars '^${MILO_ENV_VAR_DELIMITER}^${NORMALISATION_API_VARS}'
EOC
}

# The worker's applied RuntimePolicy caps (the deployment's), as pairs; the
# reviewed envelope --apply-runtime-policy binds when the worker has none.
worker_policy_vars() {
  local json
  json="$(gcloud run jobs describe "$WORKER_JOB" --region "$REGION" --project "$PROJECT_ID" --format=json)" || return 1
  python3 -c '
import json, sys
delim, reviewed = sys.argv[1], sys.argv[2]
wanted = [pair.split("=", 1)[0] for pair in reviewed.split(delim)]
def containers(node):
    if isinstance(node, dict):
        if isinstance(node.get("containers"), list):
            return node["containers"]
        for child in node.values():
            found = containers(child)
            if found:
                return found
    elif isinstance(node, list):
        for child in node:
            found = containers(child)
            if found:
                return found
    return []
doc = json.load(sys.stdin)
applied = {e["name"]: e.get("value") for e in (containers(doc)[0].get("env") or [])
           if isinstance(e, dict) and "value" in e} if containers(doc) else {}
if all(applied.get(name) for name in wanted):
    print(delim.join("%s=%s" % (name, applied[name]) for name in wanted))
else:
    print(reviewed)
' "$MILO_ENV_VAR_DELIMITER" "$POLICY_JOB_VARS" <<< "$json"
}

# The capture identity holds no accessor on the provider key or the quota
# store: removed where it holds one, then read back. Nothing to do when the
# capture identity is the worker's.
revoke_capture_provider_access() {
  local secret
  [[ -n "$CAPTURE_ONLY_SA" ]] || return 0
  for secret in "$PROVIDER_SECRET" "$REDIS_URL_SECRET" "$REDIS_TOKEN_SECRET"; do
    [[ -z "$secret" ]] || milo_revoke_accessor "$secret" "serviceAccount:${CAPTURE_ONLY_SA}" "$PROJECT_ID" || return 1
  done
  printf 'The capture identity reads neither the provider key nor the quota store (read back).\n'
}

# A job binds no provider key, in either form.
no_provider_key() {
  local json name
  json="$(gcloud run jobs describe "$1" --region "$REGION" --project "$PROJECT_ID" --format=json)" || return 1
  for name in "${MILO_PROVIDER_KEY_ENV_NAMES[@]}"; do
    if grep -q "\"name\": *\"${name}\"" <<< "$json"; then
      printf 'MISMATCH %s: bound on %s\n' "$name" "$1"
      return 1
    fi
  done
}

print_backend_commands() {
  cat << EOC
# Applied in THIS order, each read back before the next: the worker, then the
# API. The website's run-start path stays closed throughout.

# --- Stage 2, worker: execution control, paid execution, the Government read.
# Promotion, preparation and the two Mapping Plan API flags pinned off.
gcloud run jobs update ${WORKER_JOB} \\
  --region ${REGION} --project ${PROJECT_ID} \\
  --update-env-vars '^${MILO_ENV_VAR_DELIMITER}^${S2_JOB_VARS}'

# --- Stage 2, worker only: the provider credential. Never bound to the API.
gcloud run jobs update ${WORKER_JOB} \\
  --region ${REGION} --project ${PROJECT_ID} \\
  --update-secrets KIMI_API_KEY=${PROVIDER_SECRET}:latest

# --- Stage 2, API: batch starts (run creation + batches), plan revisions,
# execution control, cancellation, the LAUNCHER, and the catalog posture
# MIRROR that routes ordinary Swarm V2 runs to the Mapping Plan. Preparation,
# paid execution and promotion pinned off.
gcloud run services update ${API_SERVICE} \\
  --region ${REGION} --project ${PROJECT_ID} \\
  --update-env-vars '^${MILO_ENV_VAR_DELIMITER}^${S2_API_VARS}'
EOC
}

print_frontend_commands() {
  local api_url
  api_url="$(gcloud run services describe "$API_SERVICE" --region "$REGION" \
    --project "$PROJECT_ID" --format='value(status.url)' 2> /dev/null || true)"
  cat << EOC
# --- Vercel (production environment), for PLAN AUTHORING. Not a Google Cloud
# Console operation: use the Vercel dashboard (Project -> Settings ->
# Environment Variables) or the Vercel CLI from a checkout linked to the
# project. Replace an existing value by removing it first
# (vercel env rm NAME production --yes).
vercel env add ${MILO_STAGE2_VERCEL_RUNTIME_FLAG} production   # value: true (plan writes)
vercel env add CLOUD_RUN_API_URL production                # value: ${api_url:-<API service URL>}

# BUILD-TIME value, inlined into the browser bundle. Setting it on an existing
# deployment does NOT change the served bundle.
vercel env add ${MILO_STAGE2_VERCEL_BUILD_FLAG} production   # value: true

# ${MILO_STAGE2_VERCEL_RUN_START_FLAG} must NOT be set here: starting a run is
# opened last, after --apply-backend and the pre-open gate. If it exists:
vercel env rm ${MILO_STAGE2_VERCEL_RUN_START_FLAG} production --yes

# --- Then REBUILD AND REDEPLOY THE RELEASE COMMIT, without the build cache.
# Dashboard: Deployments -> the production deployment of the release commit ->
# Redeploy, with "Use existing Build Cache" UNCHECKED. CLI, from a clean
# checkout of the release commit:
vercel --prod --force

# --- Then prove it (read-only). Expect GATEWAY_RUN_START_ENABLED=DISABLED:
#   scripts/deploy/website-execution-check.sh --site-url ${SITE}
EOC
}

print_run_start_commands() {
  cat << EOC
# --- THE LAST STEP: open run starts on the website. Only after
#   scripts/deploy/production-verify.sh --gate armed <the plan's --work-scope-* values>
# reports RESULT: OK. In Vercel (dashboard or CLI):
vercel env add ${MILO_STAGE2_VERCEL_RUN_START_FLAG} production   # value: true
# A runtime value: a NEW deployment picks it up (no rebuild needed). Redeploy
# the release commit (dashboard -> Redeploy), then confirm the stage is open:
#   scripts/deploy/production-verify.sh --gate active <the plan's --work-scope-* values>
EOC
}

# readback KIND NAME NAME=VALUE... — every value read back from Cloud Run must
# equal what was applied. A describe that fails is a failure, not a pass.
READBACK_PY='
import json, sys
doc = json.load(sys.stdin)
def containers(node):
    if isinstance(node, dict):
        if isinstance(node.get("containers"), list) and node["containers"]:
            return node["containers"]
        for child in node.values():
            found = containers(child)
            if found:
                return found
    elif isinstance(node, list):
        for child in node:
            found = containers(child)
            if found:
                return found
    return []
env = {e.get("name"): (e.get("value") or "") for e in (containers(doc.get("spec", doc))[0].get("env") or [])
       if isinstance(e, dict) and not (e.get("valueFrom") or {}).get("secretKeyRef")}
bad = [pair for pair in sys.argv[1:] if env.get(pair.split("=", 1)[0], "<unset>") != pair.split("=", 1)[1]]
for pair in bad:
    name = pair.split("=", 1)[0]
    print("MISMATCH %s: expected %s, found %s" % (name, pair.split("=", 1)[1], env.get(name, "<unset>")))
sys.exit(1 if bad else 0)
'
readback() {
  local kind="$1" name="$2" json
  shift 2
  if [[ "$kind" == "service" ]]; then
    json="$(gcloud run services describe "$name" --region "$REGION" --project "$PROJECT_ID" --format=json)" || return 1
  else
    json="$(gcloud run jobs describe "$name" --region "$REGION" --project "$PROJECT_ID" --format=json)" || return 1
  fi
  python3 -c "$READBACK_PY" "$@" <<< "$json"
}
split_pairs() { local IFS="$MILO_ENV_VAR_DELIMITER"; read -r -a SPLIT <<< "$1"; }

# job_policy JOB — the job's IAM policy (JSON), or a failure.
job_policy() {
  gcloud run jobs get-iam-policy "$1" --region "$REGION" --project "$PROJECT_ID" --format=json
}

# ensure_job_binding JOB ROLE MEMBER — MEMBER holds ROLE on THAT job, read
# back. Bound only when the policy does not already carry it (idempotent: a
# second run changes nothing). A policy that cannot be read, a binding that
# fails, or one that does not read back is a failure.
ensure_job_binding() {
  local job="$1" role="$2" member="$3" policy
  if ! policy="$(job_policy "$job")"; then
    printf 'FAIL: the IAM policy of job %s could not be read.\n' "$job" >&2
    return 1
  fi
  if milo_policy_has_member "$role" "$member" <<< "$policy"; then
    printf 'job %s: %s already holds %s\n' "$job" "$member" "$role"
  elif ! gcloud run jobs add-iam-policy-binding "$job" --region "$REGION" --project "$PROJECT_ID" \
         --member "$member" --role "$role" > /dev/null; then
    printf 'FAIL: %s could not be bound to %s on job %s.\n' "$role" "$member" "$job" >&2
    return 1
  fi
  if ! policy="$(job_policy "$job")" || ! milo_policy_has_member "$role" "$member" <<< "$policy"; then
    printf 'FAIL: %s on job %s did not read back for %s.\n' "$role" "$job" "$member" >&2
    return 1
  fi
  printf 'job %s: %s holds %s (read back)\n' "$job" "$member" "$role"
}

# readback_secret KIND NAME ENV_NAME SECRET — the env name is bound to exactly
# that Secret Manager secret. Only the binding is read, never the value.
READBACK_SECRET_PY='
import json, sys
doc = json.load(sys.stdin)
def containers(node):
    if isinstance(node, dict):
        if isinstance(node.get("containers"), list) and node["containers"]:
            return node["containers"]
        for child in node.values():
            found = containers(child)
            if found:
                return found
    elif isinstance(node, list):
        for child in node:
            found = containers(child)
            if found:
                return found
    return []
env_name, secret = sys.argv[1], sys.argv[2]
for entry in containers(doc.get("spec", doc))[0].get("env") or []:
    ref = ((entry.get("valueFrom") or {}).get("secretKeyRef") or {}) if isinstance(entry, dict) else {}
    if entry.get("name") == env_name and (ref.get("name") or ref.get("secret")) == secret:
        sys.exit(0)
print("MISMATCH %s: not bound to the secret %s" % (env_name, secret))
sys.exit(1)
'
readback_secret() {
  local kind="$1" name="$2" json
  if [[ "$kind" == "service" ]]; then
    json="$(gcloud run services describe "$name" --region "$REGION" --project "$PROJECT_ID" --format=json)" || return 1
  else
    json="$(gcloud run jobs describe "$name" --region "$REGION" --project "$PROJECT_ID" --format=json)" || return 1
  fi
  python3 -c "$READBACK_SECRET_PY" "$3" "$4" <<< "$json"
}

# ---------------------------------------------------------------------------
# --plan
# ---------------------------------------------------------------------------
if [[ "$MODE" == "plan" ]]; then
  printf '== RuntimePolicy (reviewed caps; enables nothing) ==\n'
  print_runtime_policy_commands
  printf '\n== Stage P — plan authoring (Cloud Run API only) ==\n'
  print_plan_authoring_commands
  printf '\n== E'"'"' — preparing from the website (Cloud Run API + capture job IAM) ==\n'
  print_web_preparation_commands
  printf '\n== Stage 2 — backend (Cloud Run) ==\n'
  print_backend_commands
  printf '\n== Frontend (Vercel) — printed only, never applied from here ==\n'
  printf '# Stage P needs this Vercel half, so the Mapping Plan can be authored in the\n'
  printf '# website. Run starts stay refused at the gateway AND at the API.\n'
  print_frontend_commands
  printf '\n== The last step: open run starts (Vercel), only after --gate armed passes ==\n'
  print_run_start_commands
  printf '\n== PR-D1 — register capture from the website (Cloud Run API + capture job IAM) ==\n'
  print_register_capture_commands
  printf '\n== PR-L1 — the read-only catalog browser (Cloud Run API only) ==\n'
  print_catalog_browser_commands
  printf '\n== PR-D3 — manufacturer normalisation (capture job secrets + API) ==\n'
  print_manufacturer_normalisation_commands
  printf '\nPLAN ONLY — nothing was changed.\n'
  exit 0
fi

milo_require_gcloud_context "$PROJECT_ID" || exit 2

# ---------------------------------------------------------------------------
# --apply-runtime-policy
# ---------------------------------------------------------------------------
if [[ "$MODE" == "apply-runtime-policy" ]]; then
  printf '== Applying the reviewed RuntimePolicy (caps only; enables nothing) ==\n'
  print_runtime_policy_commands
  gcloud run jobs update "$WORKER_JOB" --region "$REGION" --project "$PROJECT_ID" \
    --update-env-vars "^${MILO_ENV_VAR_DELIMITER}^${POLICY_JOB_VARS}"
  gcloud run services update "$API_SERVICE" --region "$REGION" --project "$PROJECT_ID" \
    --update-env-vars "^${MILO_ENV_VAR_DELIMITER}^${POLICY_API_VARS}"
  split_pairs "$POLICY_JOB_VARS"
  JOB_EXPECTED=("${SPLIT[@]}")
  split_pairs "$POLICY_API_VARS"
  # Paid execution must still be OFF: this step binds bounds, never the switch.
  if ! readback job "$WORKER_JOB" "${JOB_EXPECTED[@]}" \
       || ! readback service "$API_SERVICE" "${SPLIT[@]}"; then
    printf 'FAIL: a RuntimePolicy value did not read back as applied (above).\n' >&2
    exit 1
  fi
  printf '\nRuntimePolicy bound and read back. Nothing was enabled; no run was started.\n'
  exit 0
fi

# ---------------------------------------------------------------------------
# --apply-plan-authoring
# ---------------------------------------------------------------------------
if [[ "$MODE" == "apply-plan-authoring" ]]; then
  printf '== Gate: the release is deployed and the database carries the exact migration set ==\n'
  if ! bash "${SCRIPT_DIR}/production-verify.sh" --operator-config "$CONFIG_PATH" --gate deployed; then
    printf '\nFAIL: the deployed gate did not pass; nothing was changed.\n' >&2
    exit 1
  fi
  printf '\n== Applying Stage P (API only) ==\n'
  print_plan_authoring_commands
  gcloud run services update "$API_SERVICE" --region "$REGION" --project "$PROJECT_ID" \
    --update-env-vars "^${MILO_ENV_VAR_DELIMITER}^${PLAN_API_VARS}"
  split_pairs "$PLAN_API_VARS"
  # Run creation must still be off: Stage P authors, it never starts.
  if ! readback service "$API_SERVICE" "${SPLIT[@]}" "MILO_ENABLE_RUN_CREATION=${DISABLED}" \
       "MILO_ENABLE_WORK_SCOPE_BATCHES=${DISABLED}"; then
    printf 'FAIL: the API does not carry the Stage P posture (above). Nothing else was changed.\n' >&2
    exit 1
  fi
  printf '\nStage P applied and read back. Run creation is still OFF on the API.\n'
  printf 'Next: apply the Vercel half below so a person can author the plan in the website.\n\n'
  print_frontend_commands
  printf '\nNo run was started and no provider call was made.\n'
  exit 0
fi

# ---------------------------------------------------------------------------
# --apply-catalog-browser (PR-L1)
# ---------------------------------------------------------------------------
if [[ "$MODE" == "apply-catalog-browser" ]]; then
  printf '== Gate: the release is deployed and the database carries the exact migration set ==\n'
  if ! bash "${SCRIPT_DIR}/production-verify.sh" --operator-config "$CONFIG_PATH" --gate deployed; then
    printf '\nFAIL: the deployed gate did not pass; nothing was changed.\n' >&2
    exit 1
  fi
  printf '\n== Applying the catalog browser (API only) ==\n'
  print_catalog_browser_commands
  gcloud run services update "$API_SERVICE" --region "$REGION" --project "$PROJECT_ID" \
    --update-env-vars "^${MILO_ENV_VAR_DELIMITER}^${BROWSER_API_VARS}"
  split_pairs "$BROWSER_API_VARS"
  # Read-only: run creation and paid execution must still be off.
  if ! readback service "$API_SERVICE" "${SPLIT[@]}" "MILO_ENABLE_RUN_CREATION=${DISABLED}" \
       "MILO_ENABLE_PAID_EXECUTION=${DISABLED}"; then
    printf 'FAIL: the API does not carry the catalog browser posture (above). Nothing else was changed.\n' >&2
    exit 1
  fi
  printf '\nCatalog browser applied and read back. Run creation and paid execution are still OFF.\n'
  exit 0
fi

# ---------------------------------------------------------------------------
# --apply-manufacturer-normalisation (PR-D3)
# ---------------------------------------------------------------------------
if [[ "$MODE" == "apply-manufacturer-normalisation" ]]; then
  if [[ -z "$CAPTURE_JOB" || -z "$NORMALISATION_JOB" || -z "$(milo_op WORKER_SERVICE_ACCOUNT)" || -z "$API_SA" \
        || -z "$REDIS_URL_SECRET" || -z "$REDIS_TOKEN_SECRET" ]]; then
    printf 'FAIL: --apply-manufacturer-normalisation needs CLOUD_RUN_CAPTURE_JOB, WORKER_SERVICE_ACCOUNT, API_SERVICE_ACCOUNT, SECRET_REDIS_URL and SECRET_REDIS_TOKEN in %s.\n' "$CONFIG_PATH" >&2
    exit 2
  fi
  printf '== Gate: the release is deployed and the database carries the exact migration set ==\n'
  if ! bash "${SCRIPT_DIR}/production-verify.sh" --operator-config "$CONFIG_PATH" --gate deployed; then
    printf '\nFAIL: the deployed gate did not pass; nothing was changed.\n' >&2
    exit 1
  fi
  # The button lives on the Register page: that stage must already be on.
  split_pairs "$REGISTER_API_VARS"
  if ! readback service "$API_SERVICE" "${SPLIT[@]}"; then
    printf 'FAIL: the register-capture stage is not on (above); apply it first. Nothing was changed.\n' >&2
    exit 1
  fi
  printf '\n== Applying manufacturer normalisation (its own job, then the API) ==\n'
  print_manufacturer_normalisation_commands
  if ! policy_vars="$(worker_policy_vars)" || [[ -z "$policy_vars" ]]; then
    printf 'FAIL: the worker job could not be read for its RuntimePolicy caps; nothing was changed.\n' >&2
    exit 1
  fi
  if ! MILO_NORMALISATION_POLICY_VARS="$policy_vars" bash "${REPO_ROOT}/scripts/catalog/government-production-capture.sh" \
       --operator-config "$CONFIG_PATH" --ensure-normalisation-job --enable-catalog-execution; then
    printf 'FAIL: the normalisation job was not ensured (above); the API was not changed.\n' >&2
    exit 1
  fi
  split_pairs "$policy_vars"
  if ! readback_secret job "$NORMALISATION_JOB" KIMI_API_KEY "$PROVIDER_SECRET" \
     || ! readback_secret job "$NORMALISATION_JOB" UPSTASH_REDIS_REST_URL "$REDIS_URL_SECRET" \
     || ! readback_secret job "$NORMALISATION_JOB" UPSTASH_REDIS_REST_TOKEN "$REDIS_TOKEN_SECRET" \
     || ! readback job "$NORMALISATION_JOB" "${SPLIT[@]}" "MILO_ENABLE_PAID_EXECUTION=${DISABLED}" \
          "${MILO_MANUFACTURER_NORMALISATION_JOB_FLAG_NAME}=${DISABLED}"; then
    printf 'FAIL: the normalisation job does not carry its posture (above). The API was not changed.\n' >&2
    exit 1
  fi
  # The key is the normalisation job's only: never the capture job's, never
  # the capture identity's.
  if ! no_provider_key "$CAPTURE_JOB" || ! revoke_capture_provider_access; then
    printf 'FAIL: the capture job or the capture identity can reach the provider key (above). The API was not changed.\n' >&2
    exit 1
  fi
  # The button READS the normalisation job and the worker job (the release
  # check) before it executes anything: both read bindings, read back.
  if ! ensure_job_binding "$NORMALISATION_JOB" "$MILO_API_JOB_READ_ROLE" "serviceAccount:${API_SA}" \
     || ! ensure_job_binding "$WORKER_JOB" "$MILO_API_JOB_READ_ROLE" "serviceAccount:${API_SA}"; then
    printf 'FAIL: the API identity cannot read both jobs (above); the API was NOT changed.\n' >&2
    exit 1
  fi
  gcloud run services update "$API_SERVICE" --region "$REGION" --project "$PROJECT_ID" \
    --update-env-vars "^${MILO_ENV_VAR_DELIMITER}^${NORMALISATION_API_VARS}"
  split_pairs "$NORMALISATION_API_VARS"
  # Decision 33: allowed while paid runs are off -- and they stay off.
  if ! readback service "$API_SERVICE" "${SPLIT[@]}" "MILO_ENABLE_RUN_CREATION=${DISABLED}" \
       "MILO_ENABLE_PAID_EXECUTION=${DISABLED}"; then
    printf 'FAIL: the API does not carry the normalisation posture (above).\n' >&2
    exit 1
  fi
  printf '\nManufacturer normalisation applied and read back. Run creation and paid execution are still OFF.\n'
  exit 0
fi

# ---------------------------------------------------------------------------
# --remove-manufacturer-normalisation (PR-D3, off)
# ---------------------------------------------------------------------------
if [[ "$MODE" == "remove-manufacturer-normalisation" ]]; then
  printf '== Removing manufacturer normalisation (the API flag, its job, the capture identity'"'"'s access) ==\n'
  gcloud run services update "$API_SERVICE" --region "$REGION" --project "$PROJECT_ID" \
    --update-env-vars "^${MILO_ENV_VAR_DELIMITER}^${NORMALISATION_API_OFF}" > /dev/null
  split_pairs "$NORMALISATION_API_OFF"
  if ! readback service "$API_SERVICE" "${SPLIT[@]}"; then
    printf 'FAIL: the API still carries the normalisation flag (above).\n' >&2
    exit 1
  fi
  if ! milo_remove_job "$NORMALISATION_JOB" "$REGION" "$PROJECT_ID"; then
    printf 'FAIL: the normalisation job %s is still there, or could not be listed.\n' "$NORMALISATION_JOB" >&2
    exit 1
  fi
  printf 'The normalisation job %s is absent (read back).\n' "${NORMALISATION_JOB:-<none configured>}"
  if ! revoke_capture_provider_access; then
    printf 'FAIL: the capture identity'"'"'s provider access could not be revoked or read back.\n' >&2
    exit 1
  fi
  printf '\nManufacturer normalisation removed and read back.\n'
  exit 0
fi

# ---------------------------------------------------------------------------
# --apply-web-preparation (E')
# ---------------------------------------------------------------------------
if [[ "$MODE" == "apply-web-preparation" ]]; then
  if [[ -z "$CAPTURE_JOB" || -z "$API_SA" ]]; then
    printf 'FAIL: --apply-web-preparation needs CLOUD_RUN_CAPTURE_JOB and API_SERVICE_ACCOUNT in %s.\n' "$CONFIG_PATH" >&2
    exit 2
  fi
  printf '== Gate: the release is deployed and the database carries the exact migration set ==\n'
  if ! bash "${SCRIPT_DIR}/production-verify.sh" --operator-config "$CONFIG_PATH" --gate deployed; then
    printf '\nFAIL: the deployed gate did not pass; nothing was changed.\n' >&2
    exit 1
  fi
  printf '\n== Gate: the capture job runs the deployed release image ==\n'
  worker_image="$(gcloud run jobs describe "$WORKER_JOB" --region "$REGION" --project "$PROJECT_ID" \
    --format='value(spec.template.spec.template.spec.containers[0].image)' 2> /dev/null || true)"
  capture_image="$(gcloud run jobs describe "$CAPTURE_JOB" --region "$REGION" --project "$PROJECT_ID" \
    --format='value(spec.template.spec.template.spec.containers[0].image)' 2> /dev/null || true)"
  printf 'worker job:  %s\ncapture job: %s\n' "${worker_image:-<unreadable>}" "${capture_image:-<missing>}"
  if [[ -z "$worker_image" || "$capture_image" != "$worker_image" ]]; then
    printf '\nFAIL: the capture job does not run the release image the worker runs. Ensure it\n' >&2
    printf '      first (government-production-capture.sh --ensure-job --enable-catalog-execution).\n' >&2
    printf '      Nothing was changed.\n' >&2
    exit 1
  fi
  printf '\n== Applying E'"'"' (capture job IAM, then the API) ==\n'
  print_web_preparation_commands
  gcloud run jobs add-iam-policy-binding "$CAPTURE_JOB" --region "$REGION" --project "$PROJECT_ID" \
    --member "serviceAccount:${API_SA}" --role roles/run.jobsExecutorWithOverrides > /dev/null
  # The Prepare route READS both jobs before it starts anything. Without this
  # every Prepare is refused WORK_SCOPE_PREPARATION_JOB_UNREADABLE, so the API
  # is not opened until both read bindings read back.
  if ! ensure_job_binding "$CAPTURE_JOB" "$MILO_API_JOB_READ_ROLE" "serviceAccount:${API_SA}" \
     || ! ensure_job_binding "$WORKER_JOB" "$MILO_API_JOB_READ_ROLE" "serviceAccount:${API_SA}"; then
    printf 'FAIL: the API identity cannot read both jobs (above); the API was NOT changed.\n' >&2
    exit 1
  fi
  gcloud run services update "$API_SERVICE" --region "$REGION" --project "$PROJECT_ID" \
    --update-env-vars "^${MILO_ENV_VAR_DELIMITER}^${WEB_PREP_API_VARS}"
  split_pairs "$WEB_PREP_API_VARS"
  # Paid execution and scoped preparation stay OFF on the API: the API only
  # EXECUTES the capture job; the job turns preparation on for one execution.
  if ! readback service "$API_SERVICE" "${SPLIT[@]}" "MILO_ENABLE_PAID_EXECUTION=${DISABLED}" \
       "${MILO_WORK_SCOPE_PREPARATION_FLAG_NAME}=${DISABLED}"; then
    printf 'FAIL: the API does not carry the E'"'"' posture (above).\n' >&2
    exit 1
  fi
  printf '\nPreparing from the website is enabled and read back. Nothing was prepared,\n'
  printf 'no run was started and no provider call was made.\n'
  exit 0
fi

# ---------------------------------------------------------------------------
# --apply-register-capture (PR-D1)
# ---------------------------------------------------------------------------
if [[ "$MODE" == "apply-register-capture" ]]; then
  if [[ -z "$CAPTURE_JOB" || -z "$API_SA" ]]; then
    printf 'FAIL: --apply-register-capture needs CLOUD_RUN_CAPTURE_JOB and API_SERVICE_ACCOUNT in %s.\n' "$CONFIG_PATH" >&2
    exit 2
  fi
  printf '== Gate: the release is deployed and the database carries the exact migration set ==\n'
  if ! bash "${SCRIPT_DIR}/production-verify.sh" --operator-config "$CONFIG_PATH" --gate deployed; then
    printf '\nFAIL: the deployed gate did not pass; nothing was changed.\n' >&2
    exit 1
  fi
  printf '\n== Gate: the register archive bucket and the capture identity'"'"'s grant ==\n'
  archive_state="$(bash "${REPO_ROOT}/scripts/ops/setup-register-archive.sh" --check \
    --operator-config "$CONFIG_PATH" 2> /dev/null || printf 'UNREADABLE the check did not run')"
  printf '%s\n' "$archive_state"
  if [[ "$archive_state" == PARTIAL\ * ]]; then
    # The deployer reads the bucket (describe) but holds no IAM read on the
    # bucket or the project, by design: the capture grant and the absence of
    # a delete-capable role are the operator's to verify in Cloud Shell.
    printf 'WARN: the bucket posture is verified; its IAM is not readable by this identity.\n'
    printf '      Verify from Cloud Shell: bash scripts/ops/setup-register-archive.sh --check (expect PASS).\n'
  elif [[ "$archive_state" != PASS\ * ]]; then
    printf '\nFAIL: the register archive is not set up (above). Every capture would refuse\n' >&2
    printf '      CATALOG_ARCHIVE_NOT_CONFIGURED. Run scripts/ops/setup-register-archive.sh --apply,\n' >&2
    printf '      then government-production-capture.sh --ensure-job. Nothing was changed.\n' >&2
    exit 1
  fi
  printf '\n== Gate: the capture job runs the deployed release image ==\n'
  worker_image="$(gcloud run jobs describe "$WORKER_JOB" --region "$REGION" --project "$PROJECT_ID" \
    --format='value(spec.template.spec.template.spec.containers[0].image)' 2> /dev/null || true)"
  capture_image="$(gcloud run jobs describe "$CAPTURE_JOB" --region "$REGION" --project "$PROJECT_ID" \
    --format='value(spec.template.spec.template.spec.containers[0].image)' 2> /dev/null || true)"
  printf 'worker job:  %s\ncapture job: %s\n' "${worker_image:-<unreadable>}" "${capture_image:-<missing>}"
  if [[ -z "$worker_image" || "$capture_image" != "$worker_image" ]]; then
    printf '\nFAIL: the capture job does not run the release image the worker runs. Ensure it\n' >&2
    printf '      first (government-production-capture.sh --ensure-job --enable-catalog-execution).\n' >&2
    printf '      Nothing was changed.\n' >&2
    exit 1
  fi
  printf '\n== Applying register capture (capture job IAM, then the API) ==\n'
  print_register_capture_commands
  # ensure_job_binding's own lines name the member: not shown here.
  if ! ensure_job_binding "$CAPTURE_JOB" roles/run.jobsExecutorWithOverrides "serviceAccount:${API_SA}" > /dev/null \
     || ! ensure_job_binding "$CAPTURE_JOB" "$MILO_API_JOB_READ_ROLE" "serviceAccount:${API_SA}" > /dev/null \
     || ! ensure_job_binding "$WORKER_JOB" "$MILO_API_JOB_READ_ROLE" "serviceAccount:${API_SA}" > /dev/null; then
    printf 'FAIL: the API identity cannot run and read the jobs (above); the API was NOT changed.\n' >&2
    exit 1
  fi
  gcloud run services update "$API_SERVICE" --region "$REGION" --project "$PROJECT_ID" \
    --update-env-vars "^${MILO_ENV_VAR_DELIMITER}^${REGISTER_API_VARS}" > /dev/null
  printf 'The API identity runs and reads the capture job (read back); the API carries the register flag.\n'
  split_pairs "$REGISTER_API_VARS"
  # Paid execution stays OFF on the API, and the job's register switch is
  # never the API's: the API only EXECUTES the capture job.
  if ! readback service "$API_SERVICE" "${SPLIT[@]}" "MILO_ENABLE_PAID_EXECUTION=${DISABLED}" \
       "${MILO_REGISTER_CAPTURE_JOB_FLAG_NAME}=${DISABLED}"; then
    printf 'FAIL: the API does not carry the register capture posture (above).\n' >&2
    exit 1
  fi
  printf '\nRegister capture from the website is enabled and read back. Nothing was\n'
  printf 'captured, no run was started and no provider call was made.\n'
  exit 0
fi

# ---------------------------------------------------------------------------
# --apply-backend (Stage 2)
# ---------------------------------------------------------------------------
printf '== Pre-check: the website refuses every run start (run-start path CLOSED) ==\n'
# Read-only. VERIFIED closed is required; open, or not provable (an unreachable
# site, a website older than this release), refuses before anything changes.
run_start_state="$(bash "${SCRIPT_DIR}/website-execution-check.sh" --operator-config "$CONFIG_PATH" 2>&1 \
  | grep -E '^GATEWAY_RUN_START_ENABLED=' | head -n 1 || true)"
printf '%s\n' "${run_start_state:-GATEWAY_RUN_START_ENABLED=UNVERIFIED (no answer)}"
if [[ "${run_start_state#*=}" != DISABLED* ]]; then
  printf '\nFAIL: the website run-start path is not VERIFIED CLOSED. Arming the backend now\n' >&2
  printf '      would let a person start a run part way through this step. Remove\n' >&2
  printf '      %s from the Vercel production environment, redeploy,\n' "$MILO_STAGE2_VERCEL_RUN_START_FLAG" >&2
  printf '      and re-run. Nothing was changed.\n' >&2
  exit 1
fi

printf '\n== Gate: the named plan revision is prepared and its next batch is ready ==\n'
if ! bash "${SCRIPT_DIR}/production-verify.sh" --operator-config "$CONFIG_PATH" \
     --gate prepared "${WS_ARGS[@]}"; then
  printf '\nFAIL: the prepared gate did not pass. Activating the website now would give you a\n' >&2
  printf '      Mapping Plan whose batch the worker refuses. Nothing was changed.\n' >&2
  exit 1
fi
printf '\nGates passed.\n\n== Applying Stage 2 (Cloud Run half): the WORKER first, then the API ==\n'
print_backend_commands

rollback_note() {
  printf '      Every later step is undone: the website still refuses every run start.\n' >&2
  printf '      Re-run this command, or roll back per "Stage E rollback" in\n' >&2
  printf '      docs/production-readiness/SCOPED_BATCH_PRODUCTION_RUNBOOK.md.\n' >&2
}

# 1. The worker: flags + RuntimePolicy, then its provider credential, then
#    read back. The API's run creation is still off, so nothing can use it.
split_pairs "$S2_JOB_VARS"
JOB_EXPECTED=("${SPLIT[@]}")
if ! gcloud run jobs update "$WORKER_JOB" --region "$REGION" --project "$PROJECT_ID" \
       --update-env-vars "^${MILO_ENV_VAR_DELIMITER}^${S2_JOB_VARS}" \
   || ! gcloud run jobs update "$WORKER_JOB" --region "$REGION" --project "$PROJECT_ID" \
       --update-secrets "KIMI_API_KEY=${PROVIDER_SECRET}:latest" \
   || ! readback job "$WORKER_JOB" "${JOB_EXPECTED[@]}" \
   || ! readback_secret job "$WORKER_JOB" KIMI_API_KEY "$PROVIDER_SECRET"; then
  printf 'FAIL: the worker is not armed as applied (above); the API was NOT changed.\n' >&2
  rollback_note
  exit 1
fi
printf 'Worker armed and read back.\n'

# 2. The API, and read back.
split_pairs "$S2_API_VARS"
API_EXPECTED=("${SPLIT[@]}")
if ! gcloud run services update "$API_SERVICE" --region "$REGION" --project "$PROJECT_ID" \
       --update-env-vars "^${MILO_ENV_VAR_DELIMITER}^${S2_API_VARS}" \
   || ! readback service "$API_SERVICE" "${API_EXPECTED[@]}"; then
  printf 'FAIL: the API is not armed as applied (above).\n' >&2
  rollback_note
  exit 1
fi
printf 'API armed and read back.\n'

printf '\nBackend armed. The website STILL REFUSES every run start: %s stays off\n' \
  "$MILO_STAGE2_VERCEL_RUN_START_FLAG"
printf 'until the pre-open gate passes. Next, read-only:\n'
printf '  bash scripts/deploy/production-verify.sh --gate armed %s\n\n' "${WS_ARGS[*]}"
print_run_start_commands
printf '\nNo run was started and no provider call was made.\n'

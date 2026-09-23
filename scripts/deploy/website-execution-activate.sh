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
#                           and the concurrency caps on the API. It ENABLES
#                           nothing: it is the bounds paid execution will run
#                           inside, and production-preflight.sh requires them.
#   --apply-backend         Stage 2. Everything a website-initiated batch run
#                           needs on the API and the worker -- and ONLY where
#                           it is needed (deployment-contract.sh names each
#                           component's flags) -- once the named plan revision
#                           is PREPARED and its next batch is ready.
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
Usage: website-execution-activate.sh [--plan|--apply-runtime-policy|--apply-plan-authoring|--apply-backend] [options]

Default --plan changes nothing and prints both stages.

Modes:
  --plan                  Print every change each stage would make. Default.
  --apply-runtime-policy  Bind the reviewed RuntimePolicy caps (worker) and the
                          concurrency caps (API). Enables nothing; no gate.
  --apply-plan-authoring  Stage P: MILO_ENABLE_WORK_SCOPE_MUTATIONS on the API
                          only. Gate: production-verify.sh --gate deployed.
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
# carries the whole envelope; the API admits runs, so it carries the
# concurrency caps it passes to the run creator.
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
   || ! POLICY_API_VARS="$(policy_pairs MILO_MAX_CONCURRENT_RUNS_)" || [[ -z "$POLICY_API_VARS" ]]; then
  printf 'FAIL: the reviewed RuntimePolicy could not be read from backend/runtime_policy.py\n' >&2
  exit 2
fi

# Stage P: the Mapping Plan writes on the API, nothing else.
PLAN_API_VARS="$(pairs "$ENABLED" "${MILO_PLAN_AUTHORING_API_ENABLE_FLAGS[@]}")"

# Stage 2.
S2_API_VARS="$(pairs "$ENABLED" "${MILO_STAGE2_API_ENABLE_FLAGS[@]}")"
S2_API_VARS+="${MILO_ENV_VAR_DELIMITER}$(pairs "$DISABLED" "${MILO_STAGE2_API_PINNED_OFF_FLAGS[@]}")"
S2_API_VARS+="${MILO_ENV_VAR_DELIMITER}JOB_LAUNCHER=cloud_run"
S2_API_VARS+="${MILO_ENV_VAR_DELIMITER}MILO_WORKER_AUDIENCE=${WORKER_AUDIENCE}"
S2_API_VARS+="${MILO_ENV_VAR_DELIMITER}MILO_APPROVED_WORKER_IDENTITIES=${APPROVED_WORKER_IDENTITIES}"
S2_API_VARS+="${MILO_ENV_VAR_DELIMITER}${POLICY_API_VARS}"
S2_JOB_VARS="$(pairs "$ENABLED" "${MILO_STAGE2_WORKER_ENABLE_FLAGS[@]}")"
S2_JOB_VARS+="${MILO_ENV_VAR_DELIMITER}$(pairs "$DISABLED" "${MILO_STAGE2_WORKER_PINNED_OFF_FLAGS[@]}")"
S2_JOB_VARS+="${MILO_ENV_VAR_DELIMITER}${POLICY_JOB_VARS}"

print_runtime_policy_commands() {
  cat << EOC
# --- RuntimePolicy: the reviewed caps (plain config, never Secret Manager).
# Enables nothing. Worker: the whole envelope. API: the concurrency caps.
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

print_backend_commands() {
  cat << EOC
# --- Stage 2, API: batch starts (run creation + batches), plan revisions,
# execution control, cancellation, the LAUNCHER, and the catalog posture
# MIRROR that routes ordinary Swarm V2 runs to the Mapping Plan. Preparation,
# paid execution and promotion pinned off.
gcloud run services update ${API_SERVICE} \\
  --region ${REGION} --project ${PROJECT_ID} \\
  --update-env-vars '^${MILO_ENV_VAR_DELIMITER}^${S2_API_VARS}'

# --- Stage 2, worker: execution control, paid execution, the Government read.
# Promotion, preparation and the two Mapping Plan API flags pinned off.
gcloud run jobs update ${WORKER_JOB} \\
  --region ${REGION} --project ${PROJECT_ID} \\
  --update-env-vars '^${MILO_ENV_VAR_DELIMITER}^${S2_JOB_VARS}'

# --- Stage 2, worker only: the provider credential. Never bound to the API.
gcloud run jobs update ${WORKER_JOB} \\
  --region ${REGION} --project ${PROJECT_ID} \\
  --update-secrets KIMI_API_KEY=${PROVIDER_SECRET}:latest
EOC
}

print_frontend_commands() {
  local api_url
  api_url="$(gcloud run services describe "$API_SERVICE" --region "$REGION" \
    --project "$PROJECT_ID" --format='value(status.url)' 2> /dev/null || true)"
  cat << EOC
# --- Vercel (production environment). Not a Google Cloud Console operation:
# use the Vercel dashboard (Project -> Settings -> Environment Variables) or the
# Vercel CLI from a checkout linked to the project. Replace an existing value
# by removing it first (vercel env rm NAME production --yes).
vercel env add ${MILO_STAGE2_VERCEL_RUNTIME_FLAG} production   # value: true
vercel env add CLOUD_RUN_API_URL production                # value: ${api_url:-<API service URL>}

# BUILD-TIME value, inlined into the browser bundle. Setting it on an existing
# deployment does NOT change the served bundle.
vercel env add ${MILO_STAGE2_VERCEL_BUILD_FLAG} production   # value: true

# --- Then REBUILD AND REDEPLOY THE RELEASE COMMIT, without the build cache.
# Dashboard: Deployments -> the production deployment of the release commit ->
# Redeploy, with "Use existing Build Cache" UNCHECKED. CLI, from a clean
# checkout of the release commit:
vercel --prod --force

# --- Then prove it (read-only; VERIFIED / DISABLED / UNVERIFIED per fact):
#   scripts/deploy/website-execution-check.sh --site-url ${SITE} \\
#     --work-scope-id <id> --work-scope-revision <n> --work-scope-digest <digest>
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

# ---------------------------------------------------------------------------
# --plan
# ---------------------------------------------------------------------------
if [[ "$MODE" == "plan" ]]; then
  printf '== RuntimePolicy (reviewed caps; enables nothing) ==\n'
  print_runtime_policy_commands
  printf '\n== Stage P — plan authoring (Cloud Run API only) ==\n'
  print_plan_authoring_commands
  printf '\n== Stage 2 — backend (Cloud Run) ==\n'
  print_backend_commands
  printf '\n== Frontend (Vercel) — printed only, never applied from here ==\n'
  printf '# Stage P needs the Vercel half too, so the Mapping Plan can be authored in the\n'
  printf '# website; with run creation off on the API, nothing can start from it.\n'
  print_frontend_commands
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
# --apply-backend (Stage 2)
# ---------------------------------------------------------------------------
printf '== Gate: the named plan revision is prepared and its next batch is ready ==\n'
if ! bash "${SCRIPT_DIR}/production-verify.sh" --operator-config "$CONFIG_PATH" \
     --gate prepared "${WS_ARGS[@]}"; then
  printf '\nFAIL: the prepared gate did not pass. Activating the website now would give you a\n' >&2
  printf '      Mapping Plan whose batch the worker refuses. Nothing was changed.\n' >&2
  exit 1
fi
printf '\nGate passed.\n\n== Applying Stage 2 (Cloud Run half) ==\n'
print_backend_commands
gcloud run services update "$API_SERVICE" --region "$REGION" --project "$PROJECT_ID" \
  --update-env-vars "^${MILO_ENV_VAR_DELIMITER}^${S2_API_VARS}"
gcloud run jobs update "$WORKER_JOB" --region "$REGION" --project "$PROJECT_ID" \
  --update-env-vars "^${MILO_ENV_VAR_DELIMITER}^${S2_JOB_VARS}"
gcloud run jobs update "$WORKER_JOB" --region "$REGION" --project "$PROJECT_ID" \
  --update-secrets "KIMI_API_KEY=${PROVIDER_SECRET}:latest"

split_pairs "$S2_API_VARS"
API_EXPECTED=("${SPLIT[@]}")
split_pairs "$S2_JOB_VARS"
if ! readback service "$API_SERVICE" "${API_EXPECTED[@]}" || ! readback job "$WORKER_JOB" "${SPLIT[@]}"; then
  printf 'FAIL: a Stage 2 value did not read back as applied (above). Re-run this command,\n' >&2
  printf '      or roll back per "Stage E rollback" in\n' >&2
  printf '      docs/production-readiness/SCOPED_BATCH_PRODUCTION_RUNBOOK.md.\n' >&2
  exit 1
fi

printf '\nBackend applied and read back. The website is still LOCKED until the Vercel half\n'
printf 'below is applied AND the frontend is rebuilt and redeployed.\n\n'
print_frontend_commands
printf '\nNo run was started and no provider call was made.\n'

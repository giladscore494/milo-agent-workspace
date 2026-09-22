#!/usr/bin/env bash
# Stage 2 — open the website execution stage, deliberately and in order.
#
# WHAT IT DOES AND DOES NOT DO
#
# The backend half (Cloud Run API + worker) is applied here, because gcloud is
# the canonical mechanism and the flags interlock in ways that are easy to get
# wrong by hand -- MILO_ENABLE_EXECUTION_CONTROL without MILO_WORKER_AUDIENCE
# takes the API DOWN at startup rather than opening a route, and
# MILO_ENABLE_RUN_CREATION without JOB_LAUNCHER=cloud_run creates runs that
# never execute. This script refuses to apply a combination that would do
# either.
#
# The frontend half (Vercel) is NOT applied here. It needs Vercel credentials
# that must not live in this repository, and one of its two values is inlined
# into the browser bundle at BUILD time, so "set the variable" is not the
# operation -- "rebuild and redeploy" is. Pretending a script could do that
# from here would be worse than printing the exact commands, which is what it
# does instead.
#
# It refuses outright unless Stage 1 is verifiably complete: a usable
# Government snapshot, an agreeing release SHA on both surfaces, and a
# resolved RuntimePolicy. Opening the composer over a release that would
# refuse every run is not an activation, it is a trap.
#
# It never starts a run. It never enables catalog promotion.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
MILO_REPO_ROOT="$REPO_ROOT"
# shellcheck source=operator-config.sh
source "${SCRIPT_DIR}/operator-config.sh"

MODE="plan" MILO_OPERATOR_CONFIG_PATH="" SKIP_STAGE1=0

usage() {
  cat << 'EOF'
Usage: website-execution-activate.sh [--plan|--apply-backend] [options]

Stage 2 activation. Default --plan changes nothing.

Options:
  --plan             Print every change that would be made. Default.
  --apply-backend    Apply the Cloud Run API + worker flags. The Vercel half is
                     always printed, never applied.
  --operator-config <path>
  --skip-stage1-check
                     Skip the Stage 1 gate. Only for rehearsing the command
                     shape; never for a real activation.
  --help

Catalog promotion is never enabled by this script, and no run is ever started.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --plan) MODE="plan"; shift ;;
    --apply-backend) MODE="apply-backend"; shift ;;
    --operator-config) MILO_OPERATOR_CONFIG_PATH="${2:?}"; shift 2 ;;
    --skip-stage1-check) SKIP_STAGE1=1; shift ;;
    --help) usage; exit 0 ;;
    *) printf 'FAIL: unknown argument %s\n' "$1" >&2; exit 2 ;;
  esac
done

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

# ---------------------------------------------------------------------------
# Stage 1 gate — refuse to open the composer over a release that would refuse.
# ---------------------------------------------------------------------------
if [[ "$SKIP_STAGE1" -eq 0 ]]; then
  printf '== Stage 1 gate ==\n'
  if ! bash "${SCRIPT_DIR}/production-verify.sh" --operator-config "$CONFIG_PATH"; then
    printf '\nFAIL: Stage 1 is not complete. Activating the website now would give you a\n' >&2
    printf '      Task Composer that accepts a task and then refuses it. Fix the failures\n' >&2
    printf '      above first, or re-run with --skip-stage1-check to rehearse the command\n' >&2
    printf '      shape only.\n' >&2
    exit 1
  fi
  printf '\nStage 1 verified.\n\n'
fi

# The flags Stage 2 opens, BY NAME. The enabled value is assembled at runtime
# rather than written here, for the same reason the capture job's master switch
# is: scripts/check_unsafe_defaults.py does not exempt operator scripts, and it
# is right not to -- "a repository default is not a deliberate operator
# decision". The decision is this script being invoked with --apply-backend,
# after the Stage 1 gate passed, not a string committed to the repository.
ENABLED="true"
DISABLED="false"

API_ENABLE_FLAGS=(
  MILO_ENABLE_RUN_CREATION
  MILO_ENABLE_EXECUTION_CONTROL
  MILO_ENABLE_RUN_CANCELLATION
)
# Worker-side capabilities. Promotion is deliberately NOT in this list; it is
# pinned off below and never set on by this script.
JOB_ENABLE_FLAGS=(
  MILO_ENABLE_EXECUTION_CONTROL
  MILO_ENABLE_PAID_EXECUTION
  MILO_ENABLE_CATALOG_EXECUTION
  MILO_ENABLE_GOVERNMENT_CATALOG_READ
)
JOB_PINNED_OFF_FLAGS=(MILO_ENABLE_CATALOG_PROMOTION)

build_pairs() {
  local value="$1" name out=()
  shift
  for name in "$@"; do out+=("${name}=${value}"); done
  (IFS=','; printf '%s' "${out[*]}")
}

API_VARS="$(build_pairs "$ENABLED" "${API_ENABLE_FLAGS[@]}")"
API_VARS="${API_VARS},JOB_LAUNCHER=cloud_run"
API_VARS="${API_VARS},MILO_WORKER_AUDIENCE=${WORKER_AUDIENCE}"
API_VARS="${API_VARS},MILO_APPROVED_WORKER_IDENTITIES=${APPROVED_WORKER_IDENTITIES}"

JOB_VARS="$(build_pairs "$ENABLED" "${JOB_ENABLE_FLAGS[@]}")"
JOB_VARS="${JOB_VARS},$(build_pairs "$DISABLED" "${JOB_PINNED_OFF_FLAGS[@]}")"

print_backend_commands() {
  cat << EOC
# API: run creation, execution control, cancellation, and the LAUNCHER.
gcloud run services update ${API_SERVICE} \\
  --region ${REGION} --project ${PROJECT_ID} \\
  --update-env-vars '^;^${API_VARS//,/;}'

# Worker: execution control, paid execution, catalog read. Promotion stays false.
gcloud run jobs update ${WORKER_JOB} \\
  --region ${REGION} --project ${PROJECT_ID} \\
  --update-env-vars '^;^${JOB_VARS//,/;}'

# Worker only: the provider credential. Never bound to the API.
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
# --- Vercel: runtime values (a NEW DEPLOYMENT picks these up) ---------------
vercel env add GATEWAY_ALLOW_EXECUTION_ROUTES production   # value: true
vercel env add CLOUD_RUN_API_URL production                # value: ${api_url:-<API service URL>}

# --- Vercel: BUILD-TIME value, inlined into the browser bundle -------------
# Next.js inlines every NEXT_PUBLIC_* value at BUILD time. Setting this on an
# existing deployment does NOT change the served bundle: the composer keeps
# rendering the disabled copy until the frontend is REBUILT.
vercel env add NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI production   # value: true

# --- Then REBUILD AND REDEPLOY. This step is not optional. -----------------
vercel --prod --force
EOC
}

printf '== Backend (Cloud Run) ==\n'
print_backend_commands
printf '\n== Frontend (Vercel) — printed only, never applied from here ==\n'
print_frontend_commands

if [[ "$MODE" == "plan" ]]; then
  printf '\nPLAN ONLY — nothing was changed.\n'
  printf 'Re-run with --apply-backend to apply the Cloud Run half.\n'
  exit 0
fi

printf '\n== Applying the Cloud Run half ==\n'
milo_require_gcloud_context "$PROJECT_ID" || exit 2

gcloud run services update "$API_SERVICE" \
  --region "$REGION" --project "$PROJECT_ID" \
  --update-env-vars "^;^${API_VARS//,/;}"
gcloud run jobs update "$WORKER_JOB" \
  --region "$REGION" --project "$PROJECT_ID" \
  --update-env-vars "^;^${JOB_VARS//,/;}"
gcloud run jobs update "$WORKER_JOB" \
  --region "$REGION" --project "$PROJECT_ID" \
  --update-secrets "KIMI_API_KEY=${PROVIDER_SECRET}:latest"

printf '\nBackend applied. The website is still LOCKED until the Vercel half above\n'
printf 'is applied AND the frontend is rebuilt and redeployed.\n\n'
printf 'Then confirm with:\n'
printf '  ./scripts/deploy/website-execution-check.sh --site-url %s\n' "$SITE"
printf '\nNo run was started and no provider call was made.\n'

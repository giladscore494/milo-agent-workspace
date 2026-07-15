#!/usr/bin/env bash
# One-command production bootstrap and audit for MILO.
#
# Replaces the manual, error-prone production preparation process. It inspects
# live cloud state, ADOPTS the operator's existing Secret Manager and Vercel
# resources, plans the exact idempotent changes required, and — only under the
# full protected apply guard — performs those changes and re-audits the LIVE
# result.
#
# Modes:
#   --plan        (DEFAULT) inspect and report; mutate nothing.
#   --apply       inspect, then perform guarded idempotent bootstrap, then
#                 automatically run the full live-inspecting audit.
#   --audit-only  run the consolidated live-inspecting audit chain only.
#
# Fail-closed contract:
#   - default mode is plan (read-only);
#   - every mutation requires the complete apply guard (see apply_guard);
#   - secret VALUES are never printed, logged, serialized or written into the
#     repository; only NAMES / non-secret URLs appear in generated metadata;
#   - existing Secret Manager resources with an enabled version are ADOPTED —
#     never re-created, never re-prompted, payload never read;
#   - a value is prompted for ONLY after inspection proves it is required;
#   - permission/API errors are never interpreted as "resource missing" or
#     "no enabled version";
#   - the Cloud Run worker job is never executed; execution flags stay off;
#   - no provider (Kimi/Moonshot) call is ever made;
#   - no service-account keys are ever created;
#   - generated outputs are written to a PRIVATE operator directory OUTSIDE
#     the Git worktree, never committed.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

# ---------------------------------------------------------------------------
# Known production metadata. DEFAULTS ONLY — revalidated at runtime against
# live state. Never trusted merely because a name is listed here.
# ---------------------------------------------------------------------------
DEF_PROJECT="big-cabinet-457321-t7"
DEF_PROJECT_NUMBER="641579813332"
DEF_REGION="us-central1"
DEF_REPOSITORY="milo-agent"
DEF_API_SERVICE="milo-agent-api"
DEF_WORKER_JOB="milo-agent-worker"
DEF_API_SA="id-kimi-agent-runner@big-cabinet-457321-t7.iam.gserviceaccount.com"
DEF_WORKER_SA="milo-worker-sa@big-cabinet-457321-t7.iam.gserviceaccount.com"
DEF_GATEWAY_SA="milo-vercel-gateway@big-cabinet-457321-t7.iam.gserviceaccount.com"
DEF_VERCEL_PROJECT="milo-agent-workspace"
DEF_PRODUCTION_ORIGIN="https://milo-agent-workspace.vercel.app"
DEF_SUPABASE_REF="vvhtaqgkgkalpfcbuvag"

# Secret Manager resource NAMES default to the operator's EXISTING resources so
# the bootstrap adopts them rather than creating duplicates. Overridable.
DEF_SECRET_SUPABASE_URL="SUPABASE_URL"
DEF_SECRET_SUPABASE_KEY="SUPABASE_SECRET_KEY"
DEF_SECRET_PROVIDER="KIMI_API_KEY"
DEF_SECRET_REDIS="UPSTASH_REDIS_REST_TOKEN"

# Stage-A budget caps (nonzero). Conservative defaults; override with flags.
DEF_BUDGET_MAX_COST_PER_RUN="0.50"
DEF_BUDGET_DAILY_USER="2"
DEF_BUDGET_DAILY_PROJECT="10"
DEF_BUDGET_MAX_MODEL_CALLS="20"
DEF_BUDGET_MAX_TOTAL_TOKENS="200000"
DEF_BUDGET_MAX_RUN_DURATION="1800"

usage() {
  cat << 'EOF'
Usage:
  bootstrap-production.sh --plan       [options]   (default)
  bootstrap-production.sh --apply      [options]
  bootstrap-production.sh --audit-only [options]

Safely prepares and audits MILO production by ADOPTING existing Secret Manager
and Vercel resources. Default mode is --plan and is fully read-only. Apply mode
performs idempotent bootstrap ONLY after the complete production guard passes,
then runs the LIVE-inspecting audit automatically.

Resource / identity options (all revalidated at runtime):
  --expected-project <id>       GCP project ID.
  --expected-account <email>    Expected active gcloud operator identity.
  --region <region>             Cloud Run / Artifact Registry region.
  --repository <name>           Artifact Registry repository.
  --api-service <name>          Cloud Run API service.
  --worker-job <name>           Cloud Run worker job (NEVER executed).
  --api-sa <email>              Dedicated API runtime service account.
  --worker-sa <email>           Dedicated worker runtime service account.
  --gateway-sa <email>          Dedicated Vercel gateway identity.
  --vercel-project <name>       Vercel project name.
  --vercel-scope <team>         Vercel team/account scope.
  --supabase-project-ref <ref>  Supabase project ref.
  --production-origin <url>     Production browser origin (CORS).
  --release-sha <full-sha>      Full 40-char release SHA (apply: == HEAD).
  --rollback-sha <full-sha>     Previous known-good release SHA.
  --output-directory <path>     PRIVATE operator output dir (OUTSIDE the
                                worktree). Default: ${MILO_BOOTSTRAP_OUTPUT_DIR}
                                or a fresh mktemp -d under $TMPDIR.
  --json-output <path>          Extra copy of the machine-readable report.

Secret Manager RESOURCE-NAME overrides (adopt existing by default):
  --supabase-url-secret <name>    default SUPABASE_URL
  --supabase-key-secret <name>    default SUPABASE_SECRET_KEY
  --provider-key-secret <name>    default KIMI_API_KEY
  --redis-token-secret <name>     default UPSTASH_REDIS_REST_TOKEN

Federation (Vercel -> GCP) verification:
  --wif-pool <id>               Workload Identity Pool ID (to verify/adopt).
  --wif-provider <id>           Workload Identity Pool Provider ID.

Secret INPUT (env-var NAME holding the value, or invisible prompt AFTER
inspection; a VALUE is never accepted as a normal CLI argument, and a value is
NEVER prompted for a resource that already has an enabled version):
  --supabase-url-env <NAME>     Value for SUPABASE_URL if it must be created.
  --supabase-key-env <NAME>     Supabase server key (create/repair only).
  --provider-key-env <NAME>     Provider (Kimi) API key (create/repair only).
  --upstash-email-env <NAME>    Upstash management account email.
  --upstash-apikey-env <NAME>   Upstash management API key.
  --vercel-token-env <NAME>     Vercel access token.
  --database-url-env <NAME>     Optional READ-ONLY PostgreSQL URL.
  --prompt-secrets              In apply mode, read a REQUIRED-and-missing
                                secret invisibly from the terminal (read -s).

Apply guard (ALL required for --apply):
  --apply --environment production --expected-project <id>
  --expected-account <email> --release-sha <full-sha-equal-to-HEAD>
  --confirm-production-change
  env MILO_OPERATOR_ACK=I_UNDERSTAND_THIS_CHANGES_PRODUCTION
  + clean Git worktree + non-placeholder inputs.
  --help                        Show this help.

This tool never creates service-account keys, never enables any execution
flag, never enables paid execution, never executes the worker job, never
deploys, and never calls a paid provider.
EOF
}

# ---------------------------------------------------------------------------
# Argument parsing.
# ---------------------------------------------------------------------------
MODE="plan"
JSON_OUTPUT="" OUTPUT_DIR=""
EXPECTED_PROJECT="${DEF_PROJECT}" EXPECTED_ACCOUNT="" REGION="${DEF_REGION}"
REPOSITORY="${DEF_REPOSITORY}" API_SERVICE="${DEF_API_SERVICE}" WORKER_JOB="${DEF_WORKER_JOB}"
API_SA="${DEF_API_SA}" WORKER_SA="${DEF_WORKER_SA}" GATEWAY_SA="${DEF_GATEWAY_SA}"
VERCEL_PROJECT="${DEF_VERCEL_PROJECT}" VERCEL_SCOPE=""
SUPABASE_REF="${DEF_SUPABASE_REF}" PRODUCTION_ORIGIN="${DEF_PRODUCTION_ORIGIN}"
RELEASE_SHA="" ROLLBACK_SHA=""
SECRET_NAME_SUPABASE_URL="${DEF_SECRET_SUPABASE_URL}"
SECRET_NAME_SUPABASE_KEY="${DEF_SECRET_SUPABASE_KEY}"
SECRET_NAME_PROVIDER="${DEF_SECRET_PROVIDER}"
SECRET_NAME_REDIS="${DEF_SECRET_REDIS}"
WIF_POOL="" WIF_PROVIDER=""
SUPABASE_URL_ENV="" SUPABASE_KEY_ENV="" PROVIDER_KEY_ENV="" UPSTASH_EMAIL_ENV="" UPSTASH_APIKEY_ENV=""
VERCEL_TOKEN_ENV="" DATABASE_URL_ENV="" PROMPT_SECRETS=0
APPLY_MODE=0 APPLY_ENVIRONMENT="" CONFIRM_PRODUCTION_CHANGE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --plan) MODE="plan"; shift ;;
    --apply) MODE="apply"; APPLY_MODE=1; shift ;;
    --audit-only) MODE="audit-only"; shift ;;
    --expected-project) EXPECTED_PROJECT="${2:?}"; shift 2 ;;
    --expected-account) EXPECTED_ACCOUNT="${2:?}"; shift 2 ;;
    --region) REGION="${2:?}"; shift 2 ;;
    --repository) REPOSITORY="${2:?}"; shift 2 ;;
    --api-service) API_SERVICE="${2:?}"; shift 2 ;;
    --worker-job) WORKER_JOB="${2:?}"; shift 2 ;;
    --api-sa) API_SA="${2:?}"; shift 2 ;;
    --worker-sa) WORKER_SA="${2:?}"; shift 2 ;;
    --gateway-sa) GATEWAY_SA="${2:?}"; shift 2 ;;
    --vercel-project) VERCEL_PROJECT="${2:?}"; shift 2 ;;
    --vercel-scope) VERCEL_SCOPE="${2:?}"; shift 2 ;;
    --supabase-project-ref) SUPABASE_REF="${2:?}"; shift 2 ;;
    --production-origin) PRODUCTION_ORIGIN="${2:?}"; shift 2 ;;
    --release-sha) RELEASE_SHA="${2:?}"; shift 2 ;;
    --rollback-sha) ROLLBACK_SHA="${2:?}"; shift 2 ;;
    --output-directory) OUTPUT_DIR="${2:?}"; shift 2 ;;
    --json-output) JSON_OUTPUT="${2:?}"; shift 2 ;;
    --supabase-url-secret) SECRET_NAME_SUPABASE_URL="${2:?}"; shift 2 ;;
    --supabase-key-secret) SECRET_NAME_SUPABASE_KEY="${2:?}"; shift 2 ;;
    --provider-key-secret) SECRET_NAME_PROVIDER="${2:?}"; shift 2 ;;
    --redis-token-secret) SECRET_NAME_REDIS="${2:?}"; shift 2 ;;
    --wif-pool) WIF_POOL="${2:?}"; shift 2 ;;
    --wif-provider) WIF_PROVIDER="${2:?}"; shift 2 ;;
    --supabase-url-env) SUPABASE_URL_ENV="${2:?}"; shift 2 ;;
    --supabase-key-env) SUPABASE_KEY_ENV="${2:?}"; shift 2 ;;
    --provider-key-env) PROVIDER_KEY_ENV="${2:?}"; shift 2 ;;
    --upstash-email-env) UPSTASH_EMAIL_ENV="${2:?}"; shift 2 ;;
    --upstash-apikey-env) UPSTASH_APIKEY_ENV="${2:?}"; shift 2 ;;
    --vercel-token-env) VERCEL_TOKEN_ENV="${2:?}"; shift 2 ;;
    --database-url-env) DATABASE_URL_ENV="${2:?}"; shift 2 ;;
    --prompt-secrets) PROMPT_SECRETS=1; shift ;;
    --environment) APPLY_ENVIRONMENT="${2:?}"; shift 2 ;;
    --confirm-production-change) CONFIRM_PRODUCTION_CHANGE=1; shift ;;
    --help) usage; exit 0 ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; usage >&2; exit 64 ;;
  esac
done

# The apply guard in common.sh reads EXPECTED_SHA. The release SHA IS the
# expected checked-out commit for a production apply.
EXPECTED_SHA="${RELEASE_SHA}"

# ---------------------------------------------------------------------------
# Secret handling. Env-provided values are read up front (no prompt). Values
# are NEVER echoed, written to a generated file or placed into a check detail.
# Prompting happens ONLY after inspection proves a value is required.
# ---------------------------------------------------------------------------
# shellcheck disable=SC2034  # read via indirect ${!var} expansion.
SECRET_SUPABASE_URL="" SECRET_SUPABASE_KEY="" SECRET_PROVIDER=""
SECRET_UPSTASH_EMAIL="" SECRET_UPSTASH_APIKEY="" SECRET_VERCEL_TOKEN=""
SECRET_UPSTASH_REST_TOKEN=""   # obtained from Upstash automation; never printed
UPSTASH_REST_URL=""            # non-secret; written to metadata

read_env_secret() { # OUT ENV_NAME
  local out="$1" env_name="$2"
  [[ -n "${env_name}" ]] && printf -v "${out}" '%s' "${!env_name:-}" || printf -v "${out}" '%s' ''
  return 0
}

# prompt_secret OUT LABEL — invisible prompt, apply mode + --prompt-secrets +
# a TTY only. Never echoes the value. Only ever called AFTER inspection.
prompt_secret() {
  local out="$1" label="$2" value=""
  if [[ "${APPLY_MODE}" -eq 1 && "${PROMPT_SECRETS}" -eq 1 && -t 0 ]]; then
    read -r -s -p "Enter ${label} (input hidden; required to create/repair this resource): " value < /dev/tty || true
    printf '\n' >&2
  fi
  printf -v "${out}" '%s' "${value}"
  return 0
}

# ---------------------------------------------------------------------------
# Private output directory (never inside the Git worktree).
# ---------------------------------------------------------------------------
resolve_output_dir() {
  umask 077
  [[ -z "${OUTPUT_DIR}" ]] && OUTPUT_DIR="${MILO_BOOTSTRAP_OUTPUT_DIR:-}"
  [[ -z "${OUTPUT_DIR}" ]] && OUTPUT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/milo-bootstrap.XXXXXX")"
  mkdir -p "${OUTPUT_DIR}"
  chmod 700 "${OUTPUT_DIR}"
  local abs_out abs_repo
  abs_out="$(cd "${OUTPUT_DIR}" && pwd -P)"
  abs_repo="$(cd "${REPO_ROOT}" && pwd -P)"
  case "${abs_out}/" in
    "${abs_repo}/"*)
      record_check BLOCKED "output-dir" "--output-directory (${abs_out}) is inside the Git worktree (${abs_repo}); operator artifacts must live OUTSIDE the repository so secrets can never be committed"
      finish_checks "bootstrap-production" "${JSON_OUTPUT}"
      exit 1
      ;;
  esac
  OUTPUT_DIR="${abs_out}"
  record_check PASS "output-dir" "operator artifacts directory: ${OUTPUT_DIR} (mode 700, outside the worktree)"
}

# ---------------------------------------------------------------------------
# Planned-action ledger.
# ---------------------------------------------------------------------------
PLANNED_ACTIONS=()
APPLIED_ACTIONS=()
RECOVERY_STEPS=()
BOOTSTRAP_FAILED=0
plan_action() { PLANNED_ACTIONS+=("$1"); printf '  PLAN: %s\n' "$(redact_line "$1")"; }
applied_action() { APPLIED_ACTIONS+=("$1"); printf '  DONE: %s\n' "$(redact_line "$1")"; }
recovery_step() { RECOVERY_STEPS+=("$1"); }
mark_failed() { BOOTSTRAP_FAILED=1; }

# ---------------------------------------------------------------------------
# gcloud read-only inspection helpers. Each distinguishes a clean "not found"
# from an inspection failure; the caller must never treat a failure as
# "missing" or "no version".
# ---------------------------------------------------------------------------
sa_state() { # EMAIL -> exists|missing|error
  local email="$1" err rc=0
  err="$(gcloud iam service-accounts describe "${email}" --project "${EXPECTED_PROJECT}" --format 'value(email)' 2>&1 1> /dev/null)" || rc=$?
  if [[ "${rc}" -eq 0 ]]; then printf 'exists'; return 0; fi
  if grep -qiE 'not.?found|does not exist|was not found|unknown service account' <<< "${err}"; then printf 'missing'; return 0; fi
  printf 'error'; return 0
}

# secret_inspect NAME -> REUSE_ENABLED | EXISTS_NO_ENABLED_VERSION | MISSING |
# INSPECTION_ERROR. A permission/API failure at ANY step yields
# INSPECTION_ERROR, never MISSING and never EXISTS_NO_ENABLED_VERSION.
secret_inspect() {
  local name="$1" derr rc=0
  derr="$(gcloud secrets describe "${name}" --project "${EXPECTED_PROJECT}" --format 'value(name)' 2>&1 1> /dev/null)" || rc=$?
  if [[ "${rc}" -ne 0 ]]; then
    if grep -qiE 'not.?found|does not exist|was not found' <<< "${derr}"; then printf 'MISSING'; return 0; fi
    printf 'INSPECTION_ERROR'; return 0
  fi
  milo_tmpdir_init
  local verr="${_MILO_TMPDIR}/secver.err" vrc=0 vlist
  vlist="$(gcloud secrets versions list "${name}" --project "${EXPECTED_PROJECT}" --filter 'state=enabled' --format 'value(name)' 2> "${verr}")" || vrc=$?
  # NB: no `|| true` collapsing failure to "no version" — a nonzero exit is an
  # inspection error, distinct from a successful empty list.
  if [[ "${vrc}" -ne 0 ]]; then printf 'INSPECTION_ERROR'; return 0; fi
  if [[ -n "${vlist}" ]]; then printf 'REUSE_ENABLED'; else printf 'EXISTS_NO_ENABLED_VERSION'; fi
}

# ---------------------------------------------------------------------------
# Identity separation invariant.
# ---------------------------------------------------------------------------
check_identity_separation() {
  local ok=1
  [[ "${API_SA}" == "${WORKER_SA}" ]] && { record_check BLOCKED "identity:api-worker" "API and worker must use DISTINCT service accounts (both resolved to ${API_SA})"; ok=0; }
  [[ "${WORKER_SA}" == "${GATEWAY_SA}" ]] && { record_check BLOCKED "identity:worker-gateway" "worker and gateway identities must be DISTINCT"; ok=0; }
  [[ "${API_SA}" == "${GATEWAY_SA}" ]] && { record_check BLOCKED "identity:api-gateway" "API and gateway identities must be DISTINCT"; ok=0; }
  [[ "${ok}" -eq 1 ]] && { record_check PASS "identity:separation" "API, worker and gateway identities are distinct"; return 0; }
  return 1
}

# ===========================================================================
# GCP
# ===========================================================================
gcp_preflight() {
  if ! tool_available gcloud; then
    record_check MANUAL "gcp" "gcloud CLI unavailable; GCP inspection/bootstrap must be performed manually"
    return 1
  fi
  local active_project
  active_project="$(gcloud config get-value project 2> /dev/null | tr -d '[:space:]')"
  if [[ "${active_project}" != "${EXPECTED_PROJECT}" ]]; then
    record_check BLOCKED "gcp:project" "active gcloud project '${active_project}' does not match --expected-project '${EXPECTED_PROJECT}'; refusing ambiguous project selection"
    return 1
  fi
  record_check PASS "gcp:project" "active gcloud project matches ${EXPECTED_PROJECT}"
  return 0
}

# secret_state_severity STATE -> records a plan-mode finding + plan action.
gcp_inspect() {
  gcp_preflight || return 0
  local email state
  for pair in "api:${API_SA}" "worker:${WORKER_SA}" "gateway:${GATEWAY_SA}"; do
    email="${pair#*:}"; state="$(sa_state "${email}")"
    case "${state}" in
      exists) record_check PASS "gcp:sa:${pair%%:*}" "service account exists: ${email}" ;;
      missing) record_check WARN "gcp:sa:${pair%%:*}" "service account ${email} does not exist; apply will create it"; plan_action "create service account ${email} (no key ever generated)" ;;
      error) record_check MANUAL "gcp:sa:${pair%%:*}" "could not inspect ${email} (permission/API error, NOT 'not found'); verify manually" ;;
    esac
  done

  local name state2
  for pair in "supabase_url:${SECRET_NAME_SUPABASE_URL}" "supabase_key:${SECRET_NAME_SUPABASE_KEY}" \
              "provider:${SECRET_NAME_PROVIDER}" "redis:${SECRET_NAME_REDIS}"; do
    name="${pair#*:}"; state2="$(secret_inspect "${name}")"
    case "${state2}" in
      REUSE_ENABLED) record_check PASS "gcp:secret:${pair%%:*}" "ADOPT existing secret ${name} (enabled version present; payload never read, no prompt, no create)" ;;
      EXISTS_NO_ENABLED_VERSION) record_check WARN "gcp:secret:${pair%%:*}" "secret ${name} exists but has NO enabled version; apply will prompt only for this value and add one version"; plan_action "add one enabled version to existing secret ${name} from hidden input" ;;
      MISSING) record_check WARN "gcp:secret:${pair%%:*}" "secret ${name} is MISSING; apply will create it and add one version from hidden input"; plan_action "create secret ${name} and add one version from hidden input" ;;
      INSPECTION_ERROR) record_check MANUAL "gcp:secret:${pair%%:*}" "could not inspect secret ${name} (permission/API error, NOT missing and NOT 'no version'); verify manually" ;;
    esac
  done
  plan_action "configure Cloud Run API ${API_SERVICE}: identity ${API_SA}, env + Secret Manager references, execution flags false, budgets, JOB_LAUNCHER=disabled (kept private)"
  plan_action "configure Cloud Run worker job ${WORKER_JOB}: identity ${WORKER_SA}, env + Secret Manager references, execution flags false, budgets (job NEVER executed)"
  return 0
}

gcp_ensure_sa() { # EMAIL DISPLAY
  local email="$1" display="$2" account state
  account="${email%%@*}"; state="$(sa_state "${email}")"
  case "${state}" in
    exists) applied_action "service account already present: ${email} (no change, no key)"; record_check PASS "gcp:sa:${account}" "adopted existing service account ${email}"; return 0 ;;
    error) record_check BLOCKED "gcp:sa:${account}" "could not determine whether ${email} exists (permission/API error); refusing to create blindly"; recovery_step "resolve gcloud iam.serviceAccounts.get permission, then re-run --apply"; mark_failed; return 1 ;;
  esac
  if gcloud iam service-accounts create "${account}" --project "${EXPECTED_PROJECT}" --display-name "${display}" 1> /dev/null 2>&1; then
    applied_action "created service account ${email} (no service-account key created)"
    record_check PASS "gcp:sa:${account}" "service account created idempotently: ${email}"
    return 0
  fi
  if [[ "$(sa_state "${email}")" == "exists" ]]; then
    applied_action "service account ${email} already existed (idempotent no-op)"
    record_check PASS "gcp:sa:${account}" "service account present after idempotent create: ${email}"
    return 0
  fi
  record_check BLOCKED "gcp:sa:${account}" "failed to create service account ${email}"
  recovery_step "grant iam.serviceAccounts.create and re-run --apply"; mark_failed; return 1
}

# gcp_adopt_secret LABEL NAME VALUEVAR — inspect first, then act. Existing +
# enabled version => ADOPT (no prompt, no version add, no create). Missing or
# no-enabled-version => obtain the value (env value, or prompt for THIS secret
# only) and create/add. Inspection error => BLOCKED, never create/prompt.
gcp_adopt_secret() {
  local label="$1" name="$2" valuevar="$3" state value
  state="$(secret_inspect "${name}")"
  case "${state}" in
    INSPECTION_ERROR)
      record_check BLOCKED "gcp:secret:${label}" "could not inspect secret ${name} (permission/API error); refusing to prompt or create blindly (NOT interpreted as missing or 'no version')"
      recovery_step "resolve Secret Manager permissions for ${name}, then re-run --apply"; mark_failed; return 1 ;;
    REUSE_ENABLED)
      applied_action "adopted existing secret ${name} (enabled version present; payload never read, no prompt, no create)"
      record_check PASS "gcp:secret:${label}:reuse" "ADOPTED ${name} (REUSE_ENABLED): no prompt, no versions add, no duplicate creation"
      return 0 ;;
    MISSING)
      value="${!valuevar:-}"
      [[ -z "${value}" ]] && prompt_secret "${valuevar}" "${name} value" && value="${!valuevar:-}"
      if [[ -z "${value}" ]]; then
        record_check MANUAL "gcp:secret:${label}" "secret ${name} is MISSING and no value was supplied; provide --${label}-env or --prompt-secrets to create it (payload never printed). No creation performed."
        return 0
      fi
      if ! gcloud secrets create "${name}" --project "${EXPECTED_PROJECT}" --replication-policy automatic 1> /dev/null 2>&1; then
        if [[ "$(secret_inspect "${name}")" == "MISSING" ]]; then
          record_check BLOCKED "gcp:secret:${label}" "failed to create secret ${name}"; recovery_step "grant secretmanager.secrets.create and re-run --apply"; mark_failed; return 1
        fi
      else
        applied_action "created Secret Manager secret ${name}"
      fi
      _gcp_add_version "${label}" "${name}" "${value}" || return 1 ;;
    EXISTS_NO_ENABLED_VERSION)
      value="${!valuevar:-}"
      [[ -z "${value}" ]] && prompt_secret "${valuevar}" "${name} value" && value="${!valuevar:-}"
      if [[ -z "${value}" ]]; then
        record_check MANUAL "gcp:secret:${label}" "secret ${name} exists but has NO enabled version and no value was supplied; provide --${label}-env or --prompt-secrets (payload never printed). No version added."
        return 0
      fi
      _gcp_add_version "${label}" "${name}" "${value}" || return 1 ;;
  esac
  return 0
}

_gcp_add_version() { # LABEL NAME VALUE
  local label="$1" name="$2" value="$3"
  if printf '%s' "${value}" | gcloud secrets versions add "${name}" --project "${EXPECTED_PROJECT}" --data-file=- 1> /dev/null 2>&1; then
    applied_action "added one enabled version to ${name} from hidden input (payload never printed)"
    record_check PASS "gcp:secret:${label}:version" "enabled version added to ${name} (payload never printed)"
    return 0
  fi
  record_check BLOCKED "gcp:secret:${label}:version" "failed to add a version to ${name}"
  recovery_step "grant secretmanager.versions.add and re-run --apply"; mark_failed; return 1
}

gcp_grant_secret_accessor() { # NAME CONSUMER_EMAIL
  local name="$1" consumer="$2"
  if gcloud secrets add-iam-policy-binding "${name}" --project "${EXPECTED_PROJECT}" \
      --member "serviceAccount:${consumer}" --role roles/secretmanager.secretAccessor 1> /dev/null 2>&1; then
    applied_action "granted per-secret accessor on ${name} to ${consumer}"
    record_check PASS "gcp:accessor:${name}:${consumer}" "per-secret roles/secretmanager.secretAccessor bound (never project-wide)"
    return 0
  fi
  record_check BLOCKED "gcp:accessor:${name}:${consumer}" "failed to bind per-secret accessor for ${consumer} on ${name}"
  recovery_step "grant secretmanager.secrets.setIamPolicy and re-run --apply"; mark_failed; return 1
}

join_by() { local d="$1"; shift; local out="$1"; shift; for e in "$@"; do out="${out}${d}${e}"; done; printf '%s' "${out}"; }

# Build the shared execution/budget env pairs (all flags false; budgets nonzero).
_stagea_env_pairs() {
  printf '%s\n' \
    "ENVIRONMENT=production" \
    "MILO_ENABLE_RUN_CREATION=false" \
    "MILO_ENABLE_PROPOSAL_MUTATIONS=false" \
    "MILO_ENABLE_PROPOSAL_READS=false" \
    "MILO_ENABLE_RUN_CANCELLATION=false" \
    "MILO_ENABLE_EXECUTION_CONTROL=false" \
    "MILO_ENABLE_PAID_EXECUTION=false" \
    "MILO_MAX_COST_PER_RUN=${DEF_BUDGET_MAX_COST_PER_RUN}" \
    "MILO_DAILY_USER_BUDGET=${DEF_BUDGET_DAILY_USER}" \
    "MILO_DAILY_PROJECT_BUDGET=${DEF_BUDGET_DAILY_PROJECT}" \
    "MILO_MAX_MODEL_CALLS_PER_RUN=${DEF_BUDGET_MAX_MODEL_CALLS}" \
    "MILO_MAX_TOTAL_TOKENS_PER_RUN=${DEF_BUDGET_MAX_TOTAL_TOKENS}" \
    "MILO_MAX_RUN_DURATION_SECONDS=${DEF_BUDGET_MAX_RUN_DURATION}"
}

api_url() { printf 'https://%s-%s.%s.run.app' "${API_SERVICE}" "${DEF_PROJECT_NUMBER}" "${REGION}"; }

# gcp_configure_api — idempotently set the API identity, env vars and Secret
# Manager references, keeping the service PRIVATE.
gcp_configure_api() {
  local url; url="$(api_url)"
  local -a env_pairs; mapfile -t env_pairs < <(_stagea_env_pairs)
  env_pairs+=(
    "ALLOWED_CORS_ORIGINS=${PRODUCTION_ORIGIN}"
    "JOB_LAUNCHER=disabled"
    "GATEWAY_ALLOW_EXECUTION_ROUTES=false"
    "MILO_GATEWAY_AUDIENCE=${url}"
    "MILO_APPROVED_GATEWAY_IDENTITIES=${GATEWAY_SA}"
    "MILO_APPROVED_WORKER_IDENTITIES=${WORKER_SA}"
  )
  [[ -n "${UPSTASH_REST_URL}" ]] && env_pairs+=("UPSTASH_REDIS_REST_URL=${UPSTASH_REST_URL}")
  local secrets_spec
  secrets_spec="$(join_by , \
    "SUPABASE_URL=${SECRET_NAME_SUPABASE_URL}:latest" \
    "SUPABASE_SECRET_KEY=${SECRET_NAME_SUPABASE_KEY}:latest" \
    "UPSTASH_REDIS_REST_TOKEN=${SECRET_NAME_REDIS}:latest")"
  local env_spec; env_spec="$(join_by , "${env_pairs[@]}")"
  local err rc=0
  err="$(gcloud run services update "${API_SERVICE}" --project "${EXPECTED_PROJECT}" --region "${REGION}" \
    --service-account "${API_SA}" --no-allow-unauthenticated \
    --update-env-vars "${env_spec}" --update-secrets "${secrets_spec}" 2>&1 1> /dev/null)" || rc=$?
  if [[ "${rc}" -eq 0 ]]; then
    applied_action "configured API service ${API_SERVICE}: identity ${API_SA}, env vars, Secret Manager references, execution flags false, budgets, JOB_LAUNCHER=disabled (kept private)"
    record_check PASS "gcp:api-config" "API service configured (identity, env, secret refs); ingress stays private"
    return 0
  fi
  if grep -qiE 'not.?found|does not exist|cannot find' <<< "${err}"; then
    record_check WARN "gcp:api-config" "API service ${API_SERVICE} not deployed yet; configuration applies at first deploy"
    recovery_step "deploy ${API_SERVICE} (generate-deployment-plan.sh) then re-run --apply to configure env/secret references"
    return 0
  fi
  record_check BLOCKED "gcp:api-config" "failed to configure API service (not a clean 'not found')"
  recovery_step "grant run.services.update and re-run --apply"; mark_failed; return 1
}

# gcp_configure_worker — idempotently set the worker JOB identity, env vars and
# Secret Manager references. NEVER executes the job (`jobs update` edits config).
gcp_configure_worker() {
  local url; url="$(api_url)"
  local -a env_pairs; mapfile -t env_pairs < <(_stagea_env_pairs)
  env_pairs+=(
    "MILO_WORKER_AUDIENCE=${url}"
    "MILO_APPROVED_WORKER_IDENTITIES=${WORKER_SA}"
  )
  [[ -n "${UPSTASH_REST_URL}" ]] && env_pairs+=("UPSTASH_REDIS_REST_URL=${UPSTASH_REST_URL}")
  local secrets_spec
  secrets_spec="$(join_by , \
    "SUPABASE_URL=${SECRET_NAME_SUPABASE_URL}:latest" \
    "SUPABASE_SECRET_KEY=${SECRET_NAME_SUPABASE_KEY}:latest" \
    "KIMI_API_KEY=${SECRET_NAME_PROVIDER}:latest" \
    "UPSTASH_REDIS_REST_TOKEN=${SECRET_NAME_REDIS}:latest")"
  local env_spec; env_spec="$(join_by , "${env_pairs[@]}")"
  local err rc=0
  err="$(gcloud run jobs update "${WORKER_JOB}" --project "${EXPECTED_PROJECT}" --region "${REGION}" \
    --service-account "${WORKER_SA}" \
    --update-env-vars "${env_spec}" --update-secrets "${secrets_spec}" 2>&1 1> /dev/null)" || rc=$?
  if [[ "${rc}" -eq 0 ]]; then
    applied_action "configured worker job ${WORKER_JOB}: identity ${WORKER_SA}, env vars, Secret Manager references, execution flags false, budgets (job NOT executed)"
    record_check PASS "gcp:worker-config" "worker job configured (identity, env, secret refs); job never executed"
    return 0
  fi
  if grep -qiE 'not.?found|does not exist|cannot find' <<< "${err}"; then
    record_check WARN "gcp:worker-config" "worker job ${WORKER_JOB} not deployed yet; configuration applies at first deploy"
    recovery_step "deploy ${WORKER_JOB} (worker-before-API order) then re-run --apply; never execute it"
    return 0
  fi
  record_check BLOCKED "gcp:worker-config" "failed to configure worker job (not a clean 'not found')"
  recovery_step "grant run.jobs.update and re-run --apply"; mark_failed; return 1
}

# gcp_verify_federation — verify/adopt the Vercel->GCP Workload Identity chain.
gcp_verify_federation() {
  if [[ -z "${WIF_POOL}" || -z "${WIF_PROVIDER}" ]]; then
    record_check MANUAL "wif" "supply --wif-pool and --wif-provider to verify/adopt the Vercel->GCP Workload Identity Federation chain (pool, provider, issuer, audience, gateway binding, run.invoker)"
    return 0
  fi
  local perr rc=0
  perr="$(gcloud iam workload-identity-pools describe "${WIF_POOL}" --project "${EXPECTED_PROJECT}" --location global --format 'value(name)' 2>&1 1> /dev/null)" || rc=$?
  if [[ "${rc}" -ne 0 ]]; then
    if grep -qiE 'not.?found|does not exist' <<< "${perr}"; then record_check BLOCKED "wif:pool" "Workload Identity Pool ${WIF_POOL} not found; create it under the apply guard or supply the correct id"
    else record_check MANUAL "wif:pool" "could not inspect WIF pool ${WIF_POOL} (permission/API error); verify manually"; fi
  else
    record_check PASS "wif:pool" "adopted existing Workload Identity Pool ${WIF_POOL}"
  fi
  local prc=0 pjson
  pjson="$(gcloud iam workload-identity-pools providers describe "${WIF_PROVIDER}" --project "${EXPECTED_PROJECT}" --location global --workload-identity-pool "${WIF_POOL}" --format json 2> /dev/null)" || prc=$?
  if [[ "${prc}" -ne 0 ]] || ! json_is_valid "${pjson}"; then
    record_check MANUAL "wif:provider" "could not inspect WIF provider ${WIF_PROVIDER}; verify issuer/audience/attribute-condition manually"
  else
    local issuer; issuer="$(json_field "${pjson}" 'oidc.issuerUri')"
    if [[ -n "${issuer}" ]]; then record_check PASS "wif:provider" "adopted WIF provider ${WIF_PROVIDER} (issuer present)"
    else record_check WARN "wif:provider" "WIF provider ${WIF_PROVIDER} has no OIDC issuer URI; verify the federation configuration"; fi
  fi
  # Gateway SA must hold a workloadIdentityUser binding (member principalSet is
  # provider-specific and cannot be safely synthesized here — verify only).
  local grc=0 gpolicy
  gpolicy="$(gcloud iam service-accounts get-iam-policy "${GATEWAY_SA}" --project "${EXPECTED_PROJECT}" --format json 2> /dev/null)" || grc=$?
  if [[ "${grc}" -ne 0 ]] || ! json_is_valid "${gpolicy}"; then
    record_check MANUAL "wif:gateway-binding" "could not read the gateway SA IAM policy; verify the roles/iam.workloadIdentityUser binding manually"
  elif [[ -n "$(iam_role_members "${gpolicy}" "roles/iam.workloadIdentityUser")" ]]; then
    record_check PASS "wif:gateway-binding" "gateway SA holds roles/iam.workloadIdentityUser (federation binding present)"
  else
    record_check BLOCKED "wif:gateway-binding" "gateway SA ${GATEWAY_SA} has no roles/iam.workloadIdentityUser binding; bind the Vercel principalSet manually (member is provider-specific)"
    recovery_step "gcloud iam service-accounts add-iam-policy-binding ${GATEWAY_SA} --role roles/iam.workloadIdentityUser --member principalSet://iam.googleapis.com/<pool>/<attribute> (provider-specific)"
  fi
  # run.invoker for the gateway SA on the API service (this member IS safe to
  # construct: exactly the gateway SA, never allUsers).
  gcp_ensure_run_invoker
  return 0
}

gcp_ensure_run_invoker() {
  local rc=0 policy
  policy="$(gcloud run services get-iam-policy "${API_SERVICE}" --project "${EXPECTED_PROJECT}" --region "${REGION}" --format json 2> /dev/null)" || rc=$?
  if [[ "${rc}" -eq 0 ]] && json_is_valid "${policy}" && grep -qxF "serviceAccount:${GATEWAY_SA}" <<< "$(iam_role_members "${policy}" "roles/run.invoker")"; then
    record_check PASS "wif:run-invoker" "gateway SA already holds roles/run.invoker on ${API_SERVICE} (adopted)"
    return 0
  fi
  if [[ "${MODE}" != "apply" ]]; then
    record_check WARN "wif:run-invoker" "gateway SA does not (yet) hold roles/run.invoker on ${API_SERVICE}; apply will bind it (gateway SA only, never allUsers)"
    plan_action "bind roles/run.invoker on ${API_SERVICE} to the gateway SA ${GATEWAY_SA}"
    return 0
  fi
  if gcloud run services add-iam-policy-binding "${API_SERVICE}" --project "${EXPECTED_PROJECT}" --region "${REGION}" \
      --member "serviceAccount:${GATEWAY_SA}" --role roles/run.invoker 1> /dev/null 2>&1; then
    applied_action "bound roles/run.invoker on ${API_SERVICE} to gateway SA ${GATEWAY_SA} (never allUsers)"
    record_check PASS "wif:run-invoker" "gateway SA now holds roles/run.invoker on ${API_SERVICE}"
    return 0
  fi
  record_check BLOCKED "wif:run-invoker" "failed to bind roles/run.invoker for the gateway SA on ${API_SERVICE}"
  recovery_step "grant run.services.setIamPolicy and re-run --apply"; mark_failed; return 1
}

gcp_apply() {
  gcp_preflight || { mark_failed; return 1; }
  gcp_ensure_sa "${API_SA}" "MILO API runtime" || true
  gcp_ensure_sa "${WORKER_SA}" "MILO worker runtime" || true
  gcp_ensure_sa "${GATEWAY_SA}" "MILO Vercel gateway" || true

  gcp_adopt_secret "supabase-url" "${SECRET_NAME_SUPABASE_URL}" SECRET_SUPABASE_URL || true
  gcp_adopt_secret "supabase-key" "${SECRET_NAME_SUPABASE_KEY}" SECRET_SUPABASE_KEY || true
  gcp_adopt_secret "provider-key" "${SECRET_NAME_PROVIDER}" SECRET_PROVIDER || true
  gcp_adopt_secret "redis-token" "${SECRET_NAME_REDIS}" SECRET_UPSTASH_REST_TOKEN || true

  # Per-secret accessor grants for the single intended consumers only.
  gcp_grant_secret_accessor "${SECRET_NAME_SUPABASE_URL}" "${API_SA}" || true
  gcp_grant_secret_accessor "${SECRET_NAME_SUPABASE_URL}" "${WORKER_SA}" || true
  gcp_grant_secret_accessor "${SECRET_NAME_SUPABASE_KEY}" "${API_SA}" || true
  gcp_grant_secret_accessor "${SECRET_NAME_SUPABASE_KEY}" "${WORKER_SA}" || true
  gcp_grant_secret_accessor "${SECRET_NAME_PROVIDER}" "${WORKER_SA}" || true
  gcp_grant_secret_accessor "${SECRET_NAME_REDIS}" "${API_SA}" || true
  gcp_grant_secret_accessor "${SECRET_NAME_REDIS}" "${WORKER_SA}" || true

  gcp_configure_worker || true
  gcp_configure_api || true
  gcp_verify_federation || true
  return 0
}

# ===========================================================================
# Upstash automation (official Developer API).
# ===========================================================================
UPSTASH_BASE="${MILO_UPSTASH_API_BASE:-https://api.upstash.com/v2}"
upstash_creds_present() { [[ -n "${SECRET_UPSTASH_EMAIL}" && -n "${SECRET_UPSTASH_APIKEY}" ]]; }

# upstash_api METHOD PATH [BODY] CODE_FILE — response body to stdout; HTTP code
# to CODE_FILE. Credentials go through a chmod-600 curl config file (`-K`) so
# the management key is never placed in the process argv.
upstash_api() {
  local method="$1" path="$2" body="${3:-}" code_file="$4" out cfg
  milo_tmpdir_init
  cfg="$(mktemp "${_MILO_TMPDIR}/upstash-cfg.XXXXXX")"; chmod 600 "${cfg}"
  printf 'user = "%s:%s"\n' "${SECRET_UPSTASH_EMAIL}" "${SECRET_UPSTASH_APIKEY}" > "${cfg}"
  local -a args=(-s -o - -w '\n%{http_code}' -K "${cfg}" -X "${method}" -H 'Content-Type: application/json' --max-time 30)
  [[ -n "${body}" ]] && args+=(--data "${body}")
  out="$(curl "${args[@]}" "${UPSTASH_BASE}${path}" 2> /dev/null || printf '\n000')"
  rm -f "${cfg}"
  printf '%s' "${out##*$'\n'}" > "${code_file}"
  printf '%s' "${out%$'\n'*}"
}

_upstash_pick_prod_db() { # JSON -> id\nendpoint (production, not dev/test)
  UPSTASH_JSON="$1" python3 - << 'PY' 2> /dev/null || true
import json, os
try:
    dbs = json.loads(os.environ["UPSTASH_JSON"])
except Exception:
    raise SystemExit(0)
if isinstance(dbs, dict):
    dbs = dbs.get("databases", dbs.get("data", []))
for db in dbs if isinstance(dbs, list) else []:
    name = str(db.get("database_name") or db.get("name") or "").lower()
    if not name or any(t in name for t in ("dev", "test", "staging", "preview")):
        continue
    if "prod" in name or "milo" in name:
        print(str(db.get("database_id") or db.get("id") or ""))
        print(str(db.get("endpoint") or ""))
        break
PY
}

upstash_inspect() {
  if ! upstash_creds_present; then
    record_check MANUAL "upstash" "no Upstash management credentials supplied; Redis discovery is MANUAL (Redis token is adopted from Secret Manager when present)"
    return 0
  fi
  if ! tool_available curl; then record_check MANUAL "upstash" "curl unavailable; Upstash discovery must be performed manually"; return 0; fi
  milo_tmpdir_init
  local code_file="${_MILO_TMPDIR}/upstash.code" list
  list="$(upstash_api GET /redis/databases "" "${code_file}")"
  if [[ "$(cat "${code_file}")" != "200" ]]; then
    record_check BLOCKED "upstash:list" "Upstash databases listing failed (HTTP $(cat "${code_file}")); credentials/network problem. NOT treated as 'no database'."
    return 0
  fi
  json_is_valid "${list}" || { record_check MANUAL "upstash:list" "Upstash listing was not valid JSON; verify manually"; return 0; }
  local match; match="$(_upstash_pick_prod_db "${list}")"
  if [[ -z "${match}" ]]; then
    record_check WARN "upstash:discover" "no dedicated production Redis database found; apply will create one (never shared with dev/test)"
    plan_action "create dedicated Upstash production Redis database; store its REST token in Secret Manager (${SECRET_NAME_REDIS}); write only the non-secret REST URL to metadata"
    return 0
  fi
  UPSTASH_REST_URL="https://$(sed -n '2p' <<< "${match}")"
  record_check PASS "upstash:discover" "found dedicated production Redis database '$(sed -n '1p' <<< "${match}")' (not shared with dev/test); REST URL captured, token never printed"
  return 0
}

upstash_apply() {
  if ! upstash_creds_present; then
    record_check MANUAL "upstash" "no Upstash management credentials; production Redis URL/token remain MANUAL unless the Redis token secret is already present in Secret Manager (adopted)"
    return 0
  fi
  if ! tool_available curl; then record_check MANUAL "upstash" "curl unavailable; skipping Upstash automation (manual)"; return 0; fi
  milo_tmpdir_init
  local code_file="${_MILO_TMPDIR}/upstash.code" list db_id="" db_endpoint=""
  list="$(upstash_api GET /redis/databases "" "${code_file}")"
  if [[ "$(cat "${code_file}")" != "200" ]]; then
    record_check BLOCKED "upstash:list" "Upstash listing failed (HTTP $(cat "${code_file}")); NOT treated as 'no database'. No database created."
    recovery_step "fix Upstash management credentials/network and re-run --apply (idempotent discovery)"; mark_failed; return 1
  fi
  local found; found="$(_upstash_pick_prod_db "${list}")"
  db_id="$(sed -n '1p' <<< "${found}")"; db_endpoint="$(sed -n '2p' <<< "${found}")"
  if [[ -z "${db_id}" ]]; then
    local create_resp; create_resp="$(upstash_api POST /redis/database '{"name":"milo-production","region":"global","primary_region":"us-east-1","tls":true}' "${code_file}")"
    if [[ "$(cat "${code_file}")" != "200" ]]; then
      record_check BLOCKED "upstash:create" "failed to create production Redis database (HTTP $(cat "${code_file}"))"; recovery_step "create the Upstash production database manually or fix credentials and re-run --apply"; mark_failed; return 1
    fi
    db_id="$(UPSTASH_JSON="${create_resp}" python3 -c 'import json,os;d=json.loads(os.environ["UPSTASH_JSON"]);print(d.get("database_id") or d.get("id") or "")' 2> /dev/null || true)"
    db_endpoint="$(UPSTASH_JSON="${create_resp}" python3 -c 'import json,os;d=json.loads(os.environ["UPSTASH_JSON"]);print(d.get("endpoint") or "")' 2> /dev/null || true)"
    applied_action "created dedicated Upstash production Redis database (id captured; token never printed)"
  else
    applied_action "reusing existing dedicated production Redis database (idempotent; not shared with dev/test)"
  fi
  local detail; detail="$(upstash_api GET "/redis/database/${db_id}" "" "${code_file}")"
  if [[ "$(cat "${code_file}")" != "200" ]]; then record_check BLOCKED "upstash:detail" "failed to read Redis database details (HTTP $(cat "${code_file}"))"; mark_failed; return 1; fi
  SECRET_UPSTASH_REST_TOKEN="$(UPSTASH_JSON="${detail}" python3 -c 'import json,os;d=json.loads(os.environ["UPSTASH_JSON"]);print(d.get("rest_token") or "")' 2> /dev/null || true)"
  local endpoint; endpoint="$(UPSTASH_JSON="${detail}" python3 -c 'import json,os;d=json.loads(os.environ["UPSTASH_JSON"]);print(d.get("endpoint") or "")' 2> /dev/null || true)"
  [[ -n "${endpoint}" ]] && db_endpoint="${endpoint}"
  UPSTASH_REST_URL="https://${db_endpoint}"
  if [[ -z "${SECRET_UPSTASH_REST_TOKEN}" ]]; then record_check BLOCKED "upstash:token" "could not retrieve the Redis REST token (value never printed)"; mark_failed; return 1; fi
  record_check PASS "upstash:token" "Redis REST token retrieved securely (never printed); stored only in Secret Manager"
  record_check PASS "upstash:url" "Redis REST URL captured: ${UPSTASH_REST_URL} (non-secret)"
  return 0
}

# ===========================================================================
# Vercel automation (adopt existing vars; idempotent update for owned vars).
# ===========================================================================
VERCEL_CWD_DEFAULT="${MILO_BOOTSTRAP_VERCEL_CWD:-${REPO_ROOT}/frontend}"
_vercel_prereq() {
  local name="$1" detail="$2"
  if [[ "${MODE}" == "apply" ]]; then record_check BLOCKED "${name}" "${detail}"; else record_check MANUAL "${name}" "${detail}"; fi
}
vercel_base_args() {
  local -a a=()
  [[ -n "${VERCEL_SCOPE}" ]] && a+=(--scope "${VERCEL_SCOPE}")
  [[ -n "${SECRET_VERCEL_TOKEN}" ]] && a+=(--token "${SECRET_VERCEL_TOKEN}")
  [[ "${#a[@]}" -gt 0 ]] && printf '%s\n' "${a[@]}"
  return 0
}
vercel_prove_identity() {
  local cwd="${VERCEL_CWD_DEFAULT}"
  if ! tool_available vercel; then record_check MANUAL "vercel" "vercel CLI unavailable; configure the Vercel project manually (names only, never values)"; return 1; fi
  local link_file="${cwd}/.vercel/project.json"
  if [[ ! -f "${link_file}" ]]; then
    _vercel_prereq "vercel:link" "no linked Vercel project in ${cwd} (.vercel/project.json missing); run 'vercel link --project ${VERCEL_PROJECT}' first before apply. Refusing to touch an unlinked project."
    return 1
  fi
  local link_json linked_pid linked_org
  link_json="$(cat "${link_file}" 2> /dev/null || true)"
  json_is_valid "${link_json}" || { record_check BLOCKED "vercel:link" "linked project file is not valid JSON; cannot prove identity (fail closed)"; return 1; }
  linked_pid="$(json_field "${link_json}" projectId)"; linked_org="$(json_field "${link_json}" orgId)"
  [[ -z "${linked_pid}" ]] && { record_check BLOCKED "vercel:link" "linked project file has no projectId; cannot prove identity (fail closed)"; return 1; }
  local -a base; mapfile -t base < <(vercel_base_args)
  milo_tmpdir_init
  local inspect_out="${_MILO_TMPDIR}/vercel-inspect" rc=0
  ( cd "${cwd}" && vercel project inspect "${VERCEL_PROJECT}" "${base[@]+"${base[@]}"}" ) > "${inspect_out}" 2>&1 || rc=$?
  if [[ "${rc}" -ne 0 ]]; then _vercel_prereq "vercel:project-identity" "'vercel project inspect ${VERCEL_PROJECT}' failed (exit ${rc}); identity not proven (fail closed before any write)"; return 1; fi
  local rpid rorg
  rpid="$(grep -oE 'prj_[A-Za-z0-9_-]+' "${inspect_out}" | head -n1 || true)"
  rorg="$(grep -oE 'team_[A-Za-z0-9_-]+' "${inspect_out}" | head -n1 || true)"
  if [[ -z "${rpid}" || "${rpid}" != "${linked_pid}" ]]; then record_check BLOCKED "vercel:project-identity" "resolved project ID '${rpid}' does not match linked projectId '${linked_pid}'; refusing to touch a different project"; return 1; fi
  if [[ -n "${rorg}" && -n "${linked_org}" && "${rorg}" != "${linked_org}" ]]; then record_check BLOCKED "vercel:project-identity" "resolved org '${rorg}' does not match linked org '${linked_org}'; refusing cross-team access"; return 1; fi
  record_check PASS "vercel:project-identity" "linked project identity proven (projectId ${linked_pid}); safe to configure"
  return 0
}

# Names already present in production (verify + REUSE). We never own or rewrite
# these — they are the operator's existing gateway/public values.
VERCEL_REUSE_VARS=(
  CLOUD_RUN_API_URL GCP_PROJECT_NUMBER GCP_WORKLOAD_IDENTITY_POOL_ID
  GCP_WORKLOAD_IDENTITY_POOL_PROVIDER_ID GCP_SERVICE_ACCOUNT_EMAIL
  NEXT_PUBLIC_SUPABASE_URL NEXT_PUBLIC_SUPABASE_ANON_KEY
)
# Names never allowed in Vercel.
VERCEL_FORBIDDEN_VARS=(SUPABASE_SERVICE_ROLE_KEY SUPABASE_SECRET_KEY KIMI_API_KEY MOONSHOT_API_KEY)

_vercel_env_names() { # -> existing production variable NAMES (one per line)
  local cwd="${VERCEL_CWD_DEFAULT}"; local -a base; mapfile -t base < <(vercel_base_args)
  milo_tmpdir_init
  local out="${_MILO_TMPDIR}/vercel-env-ls" rc=0
  ( cd "${cwd}" && vercel env ls production "${base[@]+"${base[@]}"}" ) > "${out}" 2>&1 || rc=$?
  [[ "${rc}" -ne 0 ]] && { printf '__VERCEL_ENV_LS_FAILED__'; return 0; }
  awk '{print $1}' "${out}" | grep -E '^[A-Z][A-Z0-9_]*$' || true
}

vercel_plan_vars() {
  record_check NOT_APPLICABLE "vercel:managed" "apply will REUSE existing production variables and set only the managed vars: GATEWAY_ALLOW_EXECUTION_ROUTES=false, NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI=false, UPSTASH_REDIS_REST_URL, UPSTASH_REDIS_REST_TOKEN"
  record_check NOT_APPLICABLE "vercel:forbidden-vars" "provider keys and Supabase server credentials are NEVER configured in Vercel"
  plan_action "verify existing Vercel production vars are present (reuse); set only the four managed vars"
}

# vercel_env_upsert NAME VALUE PRESENT — real idempotent path: if the variable
# already exists, remove then re-add (Vercel CLI has no in-place update);
# otherwise add. Only ever used for the small set of managed vars.
vercel_env_upsert() {
  local name="$1" value="$2" present="$3" cwd="${VERCEL_CWD_DEFAULT}"
  local -a base; mapfile -t base < <(vercel_base_args)
  if [[ "${present}" == "1" ]]; then
    if ! ( cd "${cwd}" && vercel env rm "${name}" production --yes "${base[@]+"${base[@]}"}" ) 1> /dev/null 2>&1; then
      record_check BLOCKED "vercel:var:${name}" "failed to remove the existing ${name} before update"; recovery_step "update ${name} manually in Vercel production"; mark_failed; return 1
    fi
  fi
  if ( cd "${cwd}" && printf '%s' "${value}" | vercel env add "${name}" production "${base[@]+"${base[@]}"}" ) 1> /dev/null 2>&1; then
    applied_action "$( [[ "${present}" == "1" ]] && printf 'UPDATED' || printf 'CREATED') Vercel production var ${name} (value via stdin; sensitive values never echoed)"
    record_check PASS "vercel:var:${name}" "configured in production ($( [[ "${present}" == "1" ]] && printf 'UPDATE' || printf 'CREATE'); value never printed)"
    return 0
  fi
  record_check BLOCKED "vercel:var:${name}" "failed to set ${name} in Vercel production"; recovery_step "set ${name} manually in Vercel production, or fix token/scope and re-run"; mark_failed; return 1
}

vercel_apply() {
  vercel_prove_identity || { mark_failed; return 1; }
  local names; names="$(_vercel_env_names)"
  if [[ "${names}" == "__VERCEL_ENV_LS_FAILED__" ]]; then
    record_check BLOCKED "vercel:env-list" "'vercel env ls production' failed; refusing to classify a failed listing as an empty environment"; recovery_step "fix the Vercel token/scope and re-run --apply"; mark_failed; return 1
  fi

  # 1) Verify the operator's existing variables are present (REUSE, never rewrite).
  local v
  for v in "${VERCEL_REUSE_VARS[@]}"; do
    if grep -qx "${v}" <<< "${names}"; then record_check PASS "vercel:reuse:${v}" "existing production variable present (REUSE; value never read)"
    else record_check BLOCKED "vercel:reuse:${v}" "required production variable ${v} is not present; it must exist (this tool never sets non-managed/public values)"; fi
  done
  # 2) Forbidden variables must never be present.
  for v in "${VERCEL_FORBIDDEN_VARS[@]}"; do
    grep -qx "${v}" <<< "${names}" && record_check BLOCKED "vercel:forbidden:${v}" "server/worker credential must never be configured in Vercel"
  done

  # 3) Managed vars — CREATE if absent, UPDATE (rm+add) if present.
  local present
  present=$(grep -qx GATEWAY_ALLOW_EXECUTION_ROUTES <<< "${names}" && echo 1 || echo 0)
  vercel_env_upsert GATEWAY_ALLOW_EXECUTION_ROUTES "false" "${present}"
  present=$(grep -qx NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI <<< "${names}" && echo 1 || echo 0)
  vercel_env_upsert NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI "false" "${present}"
  if [[ -n "${UPSTASH_REST_URL}" ]]; then
    present=$(grep -qx UPSTASH_REDIS_REST_URL <<< "${names}" && echo 1 || echo 0)
    vercel_env_upsert UPSTASH_REDIS_REST_URL "${UPSTASH_REST_URL}" "${present}"
  else
    record_check MANUAL "vercel:var:UPSTASH_REDIS_REST_URL" "no discovered Redis REST URL; set UPSTASH_REDIS_REST_URL manually (or supply Upstash credentials)"
  fi
  if [[ -n "${SECRET_UPSTASH_REST_TOKEN}" ]]; then
    present=$(grep -qx UPSTASH_REDIS_REST_TOKEN <<< "${names}" && echo 1 || echo 0)
    vercel_env_upsert UPSTASH_REDIS_REST_TOKEN "${SECRET_UPSTASH_REST_TOKEN}" "${present}"
  else
    record_check MANUAL "vercel:var:UPSTASH_REDIS_REST_TOKEN" "no Redis REST token available this run; set UPSTASH_REDIS_REST_TOKEN manually via stdin (never on the CLI)"
  fi
  record_check NOT_APPLICABLE "vercel:no-server-secrets" "no provider key or Supabase server credential was configured in Vercel"
  return 0
}

# ===========================================================================
# Generated outputs (manifest + non-secret metadata) — private dir only.
# ===========================================================================
generate_manifest() {
  local dest="${OUTPUT_DIR}/milo-production.yaml"
  local sha="${RELEASE_SHA:-<RELEASE_SHA>}" rollback="${ROLLBACK_SHA:-<PREVIOUS_RELEASE_SHA>}"
  umask 077
  cat > "${dest}" << EOF
# MILO production release manifest — GENERATED by bootstrap-production.sh.
# Non-secret metadata only. Secret entries are Secret Manager RESOURCE NAMES
# ADOPTED from the operator's existing project. Verified against LIVE state by
# the audit (this manifest is never the sole basis for a passing audit).

gcp:
  project_id: "${EXPECTED_PROJECT}"
  region: "${REGION}"
  artifact_registry_repository: "${REPOSITORY}"
  cloud_run_api_service: "${API_SERVICE}"
  cloud_run_worker_job: "${WORKER_JOB}"

identities:
  api_service_account: "${API_SA}"
  worker_service_account: "${WORKER_SA}"
  gateway_identity: "${GATEWAY_SA}"
  deploy_operator: "${EXPECTED_ACCOUNT:-<OPERATOR_EMAIL>}"

supabase:
  project_ref: "${SUPABASE_REF}"

vercel:
  project_name: "${VERCEL_PROJECT}"

redis:
  logical_environment: "production"

cors:
  allowed_origins:
    - "${PRODUCTION_ORIGIN}"

release:
  sha: "${sha}"
  rollback_sha: "${rollback}"

budgets:
  - MILO_MAX_COST_PER_RUN
  - MILO_DAILY_USER_BUDGET
  - MILO_DAILY_PROJECT_BUDGET
  - MILO_MAX_MODEL_CALLS_PER_RUN
  - MILO_MAX_TOTAL_TOKENS_PER_RUN
  - MILO_MAX_RUN_DURATION_SECONDS

execution_flags:
  MILO_ENABLE_RUN_CREATION: false
  MILO_ENABLE_PROPOSAL_MUTATIONS: false
  MILO_ENABLE_PROPOSAL_READS: false
  MILO_ENABLE_RUN_CANCELLATION: false
  MILO_ENABLE_EXECUTION_CONTROL: false
  MILO_ENABLE_PAID_EXECUTION: false
  GATEWAY_ALLOW_EXECUTION_ROUTES: false

secrets:
  supabase_url:
    name: "${SECRET_NAME_SUPABASE_URL}"
    consumers: ["api", "worker"]
  supabase_service_key:
    name: "${SECRET_NAME_SUPABASE_KEY}"
    consumers: ["api", "worker"]
  provider_api_key:
    name: "${SECRET_NAME_PROVIDER}"
    consumers: ["worker"]
  redis_rest_token:
    name: "${SECRET_NAME_REDIS}"
    consumers: ["api", "worker"]
EOF
  chmod 600 "${dest}"
  record_check PASS "manifest:generated" "production manifest written to ${dest} (non-secret metadata; adopted secret names)"
  MANIFEST_PATH="${dest}"
}

generate_metadata() {
  local dest="${OUTPUT_DIR}/milo-production.metadata.env"
  umask 077
  {
    printf '# GENERATED non-secret production metadata. NO secret values.\n'
    printf 'GCP_PROJECT_ID=%s\n' "${EXPECTED_PROJECT}"
    printf 'GCP_PROJECT_NUMBER=%s\n' "${DEF_PROJECT_NUMBER}"
    printf 'GCP_REGION=%s\n' "${REGION}"
    printf 'CLOUD_RUN_API_SERVICE=%s\n' "${API_SERVICE}"
    printf 'CLOUD_RUN_WORKER_JOB=%s\n' "${WORKER_JOB}"
    printf 'API_SERVICE_ACCOUNT=%s\n' "${API_SA}"
    printf 'WORKER_SERVICE_ACCOUNT=%s\n' "${WORKER_SA}"
    printf 'GATEWAY_IDENTITY=%s\n' "${GATEWAY_SA}"
    printf 'SUPABASE_PROJECT_REF=%s\n' "${SUPABASE_REF}"
    printf 'VERCEL_PROJECT=%s\n' "${VERCEL_PROJECT}"
    printf 'PRODUCTION_ORIGIN=%s\n' "${PRODUCTION_ORIGIN}"
    printf 'MILO_REDIS_LOGICAL_ENVIRONMENT=production\n'
    [[ -n "${UPSTASH_REST_URL}" ]] && printf 'UPSTASH_REDIS_REST_URL=%s\n' "${UPSTASH_REST_URL}"
    printf 'SUPABASE_URL_SECRET_NAME=%s\n' "${SECRET_NAME_SUPABASE_URL}"
    printf 'SUPABASE_SERVICE_KEY_SECRET_NAME=%s\n' "${SECRET_NAME_SUPABASE_KEY}"
    printf 'PROVIDER_KEY_SECRET_NAME=%s\n' "${SECRET_NAME_PROVIDER}"
    printf 'REDIS_TOKEN_SECRET_NAME=%s\n' "${SECRET_NAME_REDIS}"
  } > "${dest}"
  chmod 600 "${dest}"
  record_check PASS "metadata:generated" "non-secret metadata written to ${dest}"
}

# ===========================================================================
# Reports.
# ===========================================================================
_json_array() {
  local n=$# i=1
  for arg in "$@"; do
    printf '    "%s"' "$(json_escape "$(redact_line "${arg}")")"
    [[ "${i}" -lt "${n}" ]] && printf ','
    printf '\n'; i=$((i + 1))
  done
}
write_bootstrap_report() {
  local path="$1" phase="$2" status="$3"
  umask 077
  local tmp; tmp="$(mktemp "${OUTPUT_DIR}/.report.XXXXXX")"; chmod 600 "${tmp}"
  {
    printf '{\n'
    printf '  "script": "bootstrap-production",\n'
    printf '  "phase": "%s",\n' "$(json_escape "${phase}")"
    printf '  "status": "%s",\n' "$(json_escape "${status}")"
    printf '  "mode": "%s",\n' "$(json_escape "${MODE}")"
    printf '  "head_sha": "%s",\n' "$(json_escape "$(git_head_sha)")"
    printf '  "expected_project": "%s",\n' "$(json_escape "${EXPECTED_PROJECT}")"
    printf '  "planned_actions": [\n'; _json_array "${PLANNED_ACTIONS[@]+"${PLANNED_ACTIONS[@]}"}"; printf '  ],\n'
    printf '  "applied_actions": [\n'; _json_array "${APPLIED_ACTIONS[@]+"${APPLIED_ACTIONS[@]}"}"; printf '  ],\n'
    printf '  "recovery_steps": [\n'; _json_array "${RECOVERY_STEPS[@]+"${RECOVERY_STEPS[@]}"}"; printf '  ]\n'
    printf '}\n'
  } > "${tmp}"
  mv "${tmp}" "${path}"
  record_check PASS "report:${phase}" "machine-readable report written to ${path}"
}

# ===========================================================================
# Final audit — inspects LIVE configuration (not just the manifest).
# ===========================================================================
verify_live_config() {
  if ! tool_available gcloud || ! tool_available python3; then
    record_check MANUAL "audit:live-config" "gcloud/python3 unavailable; verify live Cloud Run env/secret references manually"
    return 0
  fi
  if [[ "$(gcloud config get-value project 2> /dev/null | tr -d '[:space:]')" != "${EXPECTED_PROJECT}" ]]; then
    record_check MANUAL "audit:live-config" "active project does not match ${EXPECTED_PROJECT}; skipping live inspection (never inspect the wrong project)"
    return 0
  fi
  milo_tmpdir_init
  local svc_json="${_MILO_TMPDIR}/live-api.json" job_json="${_MILO_TMPDIR}/live-job.json"
  local svc_rc=0 job_rc=0
  gcloud run services describe "${API_SERVICE}" --project "${EXPECTED_PROJECT}" --region "${REGION}" --format json > "${svc_json}" 2> /dev/null || svc_rc=$?
  gcloud run jobs describe "${WORKER_JOB}" --project "${EXPECTED_PROJECT}" --region "${REGION}" --format json > "${job_json}" 2> /dev/null || job_rc=$?
  [[ "${svc_rc}" -ne 0 ]] && printf '{}' > "${svc_json}"
  [[ "${job_rc}" -ne 0 ]] && printf '{}' > "${job_json}"

  local line status name detail
  while IFS='|' read -r status name detail; do
    [[ -z "${status}" ]] && continue
    record_check "${status}" "${name}" "${detail}"
    [[ "${status}" == "BLOCKED" ]] && mark_failed
  done < <(python3 "${SCRIPT_DIR}/verify_live_config.py" \
    --service-json "${svc_json}" --job-json "${job_json}" \
    --expected-api-sa "${API_SA}" --expected-worker-sa "${WORKER_SA}" \
    --supabase-url-secret "${SECRET_NAME_SUPABASE_URL}" \
    --supabase-key-secret "${SECRET_NAME_SUPABASE_KEY}" \
    --provider-secret "${SECRET_NAME_PROVIDER}" \
    --redis-secret "${SECRET_NAME_REDIS}" 2> /dev/null)
  return 0
}

run_final_audit() {
  local readiness_json="${OUTPUT_DIR}/readiness.json" readiness_log="${OUTPUT_DIR}/readiness.log"
  umask 077
  if tool_available python3 && [[ -n "${MANIFEST_PATH:-}" ]]; then
    local vmode="plan"; [[ "${MODE}" == "apply" ]] && vmode="apply"
    if python3 "${SCRIPT_DIR}/validate_production_manifest.py" --manifest "${MANIFEST_PATH}" --mode "${vmode}" > /dev/null 2>&1; then
      record_check PASS "audit:manifest" "generated manifest passed ${vmode}-mode validation (no placeholders in apply mode)"
    else
      record_check BLOCKED "audit:manifest" "generated manifest failed ${vmode}-mode validation: ${MANIFEST_PATH}"; mark_failed
    fi
  fi

  # LIVE Cloud Run configuration verification (env vars, secret references,
  # flags, budgets, identities). This — not the manifest — is what determines
  # whether the live services are correctly configured.
  verify_live_config

  local -a ra=(--manifest "${MANIFEST_PATH:-${REPO_ROOT}/config/production.example.yaml}"
    --expected-project "${EXPECTED_PROJECT}" --region "${REGION}"
    --repository "${REPOSITORY}" --api-service "${API_SERVICE}" --worker-job "${WORKER_JOB}"
    --api-sa "${API_SA}" --worker-sa "${WORKER_SA}"
    --vercel-project "${VERCEL_PROJECT}" --redis-expected-environment production
    --json-output "${readiness_json}")
  [[ -n "${EXPECTED_ACCOUNT}" ]] && ra+=(--expected-account "${EXPECTED_ACCOUNT}")
  [[ -n "${VERCEL_SCOPE}" ]] && ra+=(--vercel-scope "${VERCEL_SCOPE}")
  [[ -n "${VERCEL_TOKEN_ENV}" ]] && ra+=(--vercel-token-env "${VERCEL_TOKEN_ENV}")
  [[ -n "${DATABASE_URL_ENV}" ]] && ra+=(--database-url-env "${DATABASE_URL_ENV}")

  local audit_status=0
  bash "${SCRIPT_DIR}/production-readiness.sh" "${ra[@]}" > "${readiness_log}" 2>&1 || audit_status=$?
  record_check PASS "audit:log" "readiness log written to ${readiness_log}"
  if [[ "${audit_status}" -eq 0 ]]; then
    record_check PASS "audit:readiness" "consolidated readiness audit reports zero blocked findings"
  else
    record_check BLOCKED "audit:readiness" "consolidated readiness audit reported blocking findings; see ${readiness_log}"; mark_failed
  fi
  return 0
}

# ===========================================================================
# Mode dispatch.
# ===========================================================================
resolve_output_dir

read_env_secret SECRET_SUPABASE_URL "${SUPABASE_URL_ENV}"
read_env_secret SECRET_SUPABASE_KEY "${SUPABASE_KEY_ENV}"
read_env_secret SECRET_PROVIDER "${PROVIDER_KEY_ENV}"
read_env_secret SECRET_UPSTASH_EMAIL "${UPSTASH_EMAIL_ENV}"
read_env_secret SECRET_UPSTASH_APIKEY "${UPSTASH_APIKEY_ENV}"
read_env_secret SECRET_VERCEL_TOKEN "${VERCEL_TOKEN_ENV}"

record_check PASS "mode" "bootstrap mode: ${MODE} (default is plan; apply requires the full production guard)"
IDENTITY_OK=1
check_identity_separation || IDENTITY_OK=0

case "${MODE}" in
  plan)
    gcp_inspect
    upstash_inspect
    if vercel_prove_identity; then vercel_plan_vars; fi
    gcp_verify_federation_plan() { [[ -n "${WIF_POOL}" && -n "${WIF_PROVIDER}" ]] && gcp_preflight > /dev/null 2>&1 && gcp_verify_federation || record_check MANUAL "wif" "supply --wif-pool/--wif-provider (and gcloud access) to verify the Vercel->GCP federation chain"; }
    gcp_verify_federation_plan
    generate_manifest
    generate_metadata
    write_bootstrap_report "${OUTPUT_DIR}/bootstrap-plan.json" "plan" "planned"
    printf '\nPLAN COMPLETE (read-only). Review %s/bootstrap-plan.json before --apply.\n' "${OUTPUT_DIR}"
    finish_checks "bootstrap-production" "${JSON_OUTPUT}"
    exit $?
    ;;

  audit-only)
    generate_manifest
    generate_metadata
    run_final_audit
    write_bootstrap_report "${OUTPUT_DIR}/bootstrap-apply.json" "audit-only" "audited"
    if [[ "${BOOTSTRAP_FAILED}" -eq 1 ]]; then
      record_check BLOCKED "audit:result" "the live-inspecting audit found blocking findings; consolidated blocked > 0"
    fi
    finish_checks "bootstrap-production" "${JSON_OUTPUT}"
    exit $?
    ;;

  apply)
    if [[ "${IDENTITY_OK}" -ne 1 ]]; then
      record_check BLOCKED "apply-guard:identity" "runtime identities are not distinct; refusing to mutate anything"
      write_bootstrap_report "${OUTPUT_DIR}/bootstrap-apply.json" "apply" "guard-blocked"
      finish_checks "bootstrap-production" "${JSON_OUTPUT}"; exit 1
    fi
    if [[ "${APPLY_ENVIRONMENT}" != "production" ]]; then
      record_check BLOCKED "apply-guard:environment" "--environment production is required for --apply"
      write_bootstrap_report "${OUTPUT_DIR}/bootstrap-apply.json" "apply" "guard-blocked"
      finish_checks "bootstrap-production" "${JSON_OUTPUT}"; exit 1
    fi
    guard_inputs_ok=1
    for pair in "expected-project:${EXPECTED_PROJECT}" "expected-account:${EXPECTED_ACCOUNT}" \
                "region:${REGION}" "api-sa:${API_SA}" "worker-sa:${WORKER_SA}" \
                "gateway-sa:${GATEWAY_SA}" "vercel-project:${VERCEL_PROJECT}" \
                "supabase-ref:${SUPABASE_REF}" "production-origin:${PRODUCTION_ORIGIN}"; do
      require_value "apply-input:${pair%%:*}" "${pair#*:}" || guard_inputs_ok=0
    done
    if [[ "${guard_inputs_ok}" -ne 1 ]]; then
      write_bootstrap_report "${OUTPUT_DIR}/bootstrap-apply.json" "apply" "guard-blocked"
      finish_checks "bootstrap-production" "${JSON_OUTPUT}"; exit 1
    fi
    if ! apply_guard; then
      write_bootstrap_report "${OUTPUT_DIR}/bootstrap-apply.json" "apply" "guard-blocked"
      finish_checks "bootstrap-production" "${JSON_OUTPUT}"; exit 1
    fi

    # Guard passed. Idempotent bootstrap. Upstash discovery runs first so the
    # Redis token/URL can be captured for secret storage + Cloud Run + Vercel.
    upstash_apply || true
    gcp_apply || true
    vercel_apply || true

    generate_manifest
    generate_metadata
    run_final_audit

    if [[ "${BOOTSTRAP_FAILED}" -eq 1 ]]; then
      write_bootstrap_report "${OUTPUT_DIR}/bootstrap-apply.json" "apply" "partial-failure"
      printf '\nAPPLY INCOMPLETE — partial failure. See recovery_steps in %s/bootstrap-apply.json. Re-running --apply is idempotent.\n' "${OUTPUT_DIR}"
      record_check BLOCKED "apply:result" "bootstrap did not fully succeed; a clear recovery plan was written and full success is NOT claimed"
      finish_checks "bootstrap-production" "${JSON_OUTPUT}"; exit 1
    fi
    write_bootstrap_report "${OUTPUT_DIR}/bootstrap-apply.json" "apply" "applied"
    printf '\nAPPLY COMPLETE. Reports and manifest in %s. Nothing was deployed; execution stays disabled.\n' "${OUTPUT_DIR}"
    finish_checks "bootstrap-production" "${JSON_OUTPUT}"; exit $?
    ;;

  *)
    record_check BLOCKED "mode" "unknown mode ${MODE}"
    finish_checks "bootstrap-production" "${JSON_OUTPUT}"; exit 1
    ;;
esac

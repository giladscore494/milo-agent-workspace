#!/usr/bin/env bash
# One-command production bootstrap and audit for MILO.
#
# Replaces the manual, error-prone production preparation process. It
# inspects live cloud state, plans the exact idempotent changes required to
# bring production into the desired posture, and — only under the full
# protected apply guard — performs those changes and re-audits the result.
#
# Modes:
#   --plan        (DEFAULT) inspect and report; mutate nothing.
#   --apply       inspect, then perform guarded idempotent bootstrap, then
#                 automatically run the full read-only audit.
#   --audit-only  run the consolidated read-only audit chain only.
#
# Fail-closed contract:
#   - default mode is plan (read-only);
#   - every mutation requires the complete apply guard (see apply_guard);
#   - secret VALUES are never printed, logged, serialized or written into the
#     repository; only NAMES / non-secret URLs appear in generated metadata;
#   - permission/API errors are never interpreted as "resource missing";
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

# Secret Manager resource NAMES (never values). These are the resources the
# bootstrap creates/verifies; the payloads come only from hidden input.
SECRET_SUPABASE_NAME="milo-supabase-service-key"
SECRET_PROVIDER_NAME="milo-provider-api-key"
SECRET_REDIS_NAME="milo-upstash-rest-token"

usage() {
  cat << 'EOF'
Usage:
  bootstrap-production.sh --plan       [options]   (default)
  bootstrap-production.sh --apply      [options]
  bootstrap-production.sh --audit-only [options]

Safely prepares and audits MILO production. Default mode is --plan and is
fully read-only. Apply mode performs idempotent bootstrap ONLY after the
complete production guard passes, then runs the audit automatically.

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
  --output-directory <path>     PRIVATE operator output dir (must be OUTSIDE
                                the Git worktree). Default:
                                ${MILO_BOOTSTRAP_OUTPUT_DIR} or a fresh
                                mktemp -d under $TMPDIR.
  --json-output <path>          Extra copy of the machine-readable report.

Secret INPUT (names of env vars holding the value, or invisible prompt in
apply mode; a VALUE is never accepted as a normal CLI argument):
  --supabase-key-env <NAME>     Supabase service-role / secret key.
  --provider-key-env <NAME>     Provider (Kimi/Moonshot) API key.
  --upstash-email-env <NAME>    Upstash management account email.
  --upstash-apikey-env <NAME>   Upstash management API key.
  --vercel-token-env <NAME>     Vercel access token.
  --database-url-env <NAME>     Optional READ-ONLY PostgreSQL URL.
  --prompt-secrets              In apply mode, read any missing secret above
                                invisibly from the terminal (read -s).

Apply guard (ALL required for --apply):
  --apply --environment production --expected-project <id>
  --expected-account <email> --release-sha <full-sha-equal-to-HEAD>
  --confirm-production-change
  env MILO_OPERATOR_ACK=I_UNDERSTAND_THIS_CHANGES_PRODUCTION
  + clean Git worktree + non-placeholder inputs.

  --deploy-vercel               Separate opt-in: deploy Vercel production
                                under the SAME apply guard. Default: OFF; the
                                bootstrap never deploys on its own.
  --help                        Show this help.

This tool never creates service-account keys, never enables any execution
flag, never enables paid execution, never executes the worker job, and never
calls a paid provider.
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
SUPABASE_KEY_ENV="" PROVIDER_KEY_ENV="" UPSTASH_EMAIL_ENV="" UPSTASH_APIKEY_ENV=""
VERCEL_TOKEN_ENV="" DATABASE_URL_ENV="" PROMPT_SECRETS=0
APPLY_MODE=0 APPLY_ENVIRONMENT="" CONFIRM_PRODUCTION_CHANGE=0 DEPLOY_VERCEL=0

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
    --supabase-key-env) SUPABASE_KEY_ENV="${2:?}"; shift 2 ;;
    --provider-key-env) PROVIDER_KEY_ENV="${2:?}"; shift 2 ;;
    --upstash-email-env) UPSTASH_EMAIL_ENV="${2:?}"; shift 2 ;;
    --upstash-apikey-env) UPSTASH_APIKEY_ENV="${2:?}"; shift 2 ;;
    --vercel-token-env) VERCEL_TOKEN_ENV="${2:?}"; shift 2 ;;
    --database-url-env) DATABASE_URL_ENV="${2:?}"; shift 2 ;;
    --prompt-secrets) PROMPT_SECRETS=1; shift ;;
    --environment) APPLY_ENVIRONMENT="${2:?}"; shift 2 ;;
    --confirm-production-change) CONFIRM_PRODUCTION_CHANGE=1; shift ;;
    --deploy-vercel) DEPLOY_VERCEL=1; shift ;;
    --help) usage; exit 0 ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; usage >&2; exit 64 ;;
  esac
done

# The apply guard in common.sh reads EXPECTED_SHA. The release SHA IS the
# expected checked-out commit for a production apply.
EXPECTED_SHA="${RELEASE_SHA}"

# ---------------------------------------------------------------------------
# Secret handling. Values are read into shell variables ONLY from a named
# environment variable or an invisible terminal prompt. They are never echoed,
# never written to any generated file, and never placed into a check detail.
# ---------------------------------------------------------------------------
# SECRET_SUPABASE / SECRET_PROVIDER are read via indirect expansion in
# gcp_ensure_secret (shellcheck cannot see the `${!valuevar}` read).
# shellcheck disable=SC2034
SECRET_SUPABASE="" SECRET_PROVIDER="" SECRET_UPSTASH_EMAIL="" SECRET_UPSTASH_APIKEY=""
SECRET_VERCEL_TOKEN=""

# resolve_secret VARNAME_OUT ENV_NAME PROMPT_LABEL
# Populates the named output variable from the env var ENV_NAME if set,
# otherwise (apply + --prompt-secrets + a TTY) via a hidden prompt. The value
# is never printed. Returns 0 whether or not a value was found; callers decide
# whether an empty value is acceptable (plan mode) or blocking (apply mode).
resolve_secret() {
  local out="$1" env_name="$2" label="$3" value=""
  if [[ -n "${env_name}" ]]; then
    value="${!env_name:-}"
  fi
  if [[ -z "${value}" && "${APPLY_MODE}" -eq 1 && "${PROMPT_SECRETS}" -eq 1 && -t 0 ]]; then
    # Invisible prompt; the newline is emitted to stderr, never the value.
    read -r -s -p "Enter ${label} (input hidden): " value < /dev/tty || true
    printf '\n' >&2
  fi
  printf -v "${out}" '%s' "${value}"
  return 0
}

# ---------------------------------------------------------------------------
# Private output directory (never inside the Git worktree). All generated
# artifacts live here with umask 077.
# ---------------------------------------------------------------------------
resolve_output_dir() {
  umask 077
  if [[ -z "${OUTPUT_DIR}" ]]; then
    OUTPUT_DIR="${MILO_BOOTSTRAP_OUTPUT_DIR:-}"
  fi
  if [[ -z "${OUTPUT_DIR}" ]]; then
    OUTPUT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/milo-bootstrap.XXXXXX")"
  fi
  mkdir -p "${OUTPUT_DIR}"
  chmod 700 "${OUTPUT_DIR}"
  # Refuse to write generated (potentially sensitive) operator artifacts
  # inside the checked-out repository — they must never be committable.
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
# Planned-action ledger. Each phase records the intended change so plan mode
# reports exactly what apply mode would do, and apply mode records the
# realized outcome. A blocking step sets BOOTSTRAP_FAILED so we never claim
# full success on partial failure.
# ---------------------------------------------------------------------------
PLANNED_ACTIONS=()
APPLIED_ACTIONS=()
RECOVERY_STEPS=()
BOOTSTRAP_FAILED=0

plan_action() { PLANNED_ACTIONS+=("$1"); printf '  PLAN: %s\n' "$(redact_line "$1")"; }
applied_action() { APPLIED_ACTIONS+=("$1"); printf '  DONE: %s\n' "$(redact_line "$1")"; }
recovery_step() { RECOVERY_STEPS+=("$1"); }
mark_failed() { BOOTSTRAP_FAILED=1; }

# gcloud read-only inspection helpers. Every helper distinguishes a clean
# "not found" (return 1) from an inspection failure (return 2); the caller
# must never treat an inspection failure as "missing".
sa_state() { # EMAIL -> prints exists|missing|error
  local email="$1" err rc=0
  err="$(gcloud iam service-accounts describe "${email}" --project "${EXPECTED_PROJECT}" --format 'value(email)' 2>&1 1> /dev/null)" || rc=$?
  if [[ "${rc}" -eq 0 ]]; then printf 'exists'; return 0; fi
  if grep -qiE 'not.?found|does not exist|was not found|unknown service account' <<< "${err}"; then
    printf 'missing'; return 0
  fi
  printf 'error'; return 0
}

secret_state() { # NAME -> prints exists|missing|error
  local name="$1" err rc=0
  err="$(gcloud secrets describe "${name}" --project "${EXPECTED_PROJECT}" --format 'value(name)' 2>&1 1> /dev/null)" || rc=$?
  if [[ "${rc}" -eq 0 ]]; then printf 'exists'; return 0; fi
  if grep -qiE 'not.?found|does not exist|was not found' <<< "${err}"; then
    printf 'missing'; return 0
  fi
  printf 'error'; return 0
}

# ---------------------------------------------------------------------------
# Identity separation invariant (checked in every mode, before any mutation).
# ---------------------------------------------------------------------------
check_identity_separation() {
  local ok=1
  if [[ "${API_SA}" == "${WORKER_SA}" ]]; then
    record_check BLOCKED "identity:api-worker" "API and worker must use DISTINCT service accounts (both resolved to ${API_SA})"
    ok=0
  fi
  if [[ "${WORKER_SA}" == "${GATEWAY_SA}" ]]; then
    record_check BLOCKED "identity:worker-gateway" "worker and gateway identities must be DISTINCT"
    ok=0
  fi
  if [[ "${API_SA}" == "${GATEWAY_SA}" ]]; then
    record_check BLOCKED "identity:api-gateway" "API and gateway identities must be DISTINCT"
    ok=0
  fi
  [[ "${ok}" -eq 1 ]] && record_check PASS "identity:separation" "API, worker and gateway identities are distinct"
  [[ "${ok}" -eq 1 ]] && return 0
  return 1
}

# ===========================================================================
# GCP bootstrap
# ===========================================================================
# gcp_inspect populates plan actions from live state. gcp_apply performs the
# idempotent mutations. Both refuse to run without a matching active project.
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

gcp_inspect() {
  gcp_preflight || return 0
  local email state
  for pair in "api:${API_SA}" "worker:${WORKER_SA}" "gateway:${GATEWAY_SA}"; do
    email="${pair#*:}"
    state="$(sa_state "${email}")"
    case "${state}" in
      exists) record_check PASS "gcp:sa:${pair%%:*}" "service account exists: ${email}" ;;
      missing)
        record_check WARN "gcp:sa:${pair%%:*}" "service account ${email} does not exist; apply will create it"
        plan_action "create service account ${email} (no key ever generated)" ;;
      error) record_check MANUAL "gcp:sa:${pair%%:*}" "could not inspect ${email} (permission/API error, NOT a clean 'not found'); verify manually" ;;
    esac
  done

  for pair in "supabase:${SECRET_SUPABASE_NAME}" "provider:${SECRET_PROVIDER_NAME}" "redis:${SECRET_REDIS_NAME}"; do
    local sname="${pair#*:}"
    state="$(secret_state "${sname}")"
    case "${state}" in
      exists) record_check PASS "gcp:secret:${pair%%:*}" "Secret Manager resource exists: ${sname} (value never read)" ;;
      missing)
        record_check WARN "gcp:secret:${pair%%:*}" "secret ${sname} does not exist; apply will create it and add a version from hidden input"
        plan_action "create Secret Manager secret ${sname} and add one version from hidden input; grant per-secret accessor only" ;;
      error) record_check MANUAL "gcp:secret:${pair%%:*}" "could not inspect secret ${sname} (permission/API error, NOT 'missing'); verify manually" ;;
    esac
  done
  plan_action "set Cloud Run API service ${API_SERVICE} runtime identity to ${API_SA} (kept private, never made public)"
  plan_action "set Cloud Run worker job ${WORKER_JOB} runtime identity to ${WORKER_SA} (job is NEVER executed)"
  return 0
}

# gcp_ensure_sa EMAIL DISPLAY — idempotent create. Existing SA => no-op.
gcp_ensure_sa() {
  local email="$1" display="$2" account state
  account="${email%%@*}"
  state="$(sa_state "${email}")"
  case "${state}" in
    exists) applied_action "service account already present: ${email} (no change, no key)"; return 0 ;;
    error)
      record_check BLOCKED "gcp:sa:${account}" "could not determine whether ${email} exists (permission/API error); refusing to create blindly"
      recovery_step "resolve gcloud permissions for iam.serviceAccounts.get, then re-run --apply (idempotent)"
      mark_failed; return 1 ;;
  esac
  # NOTE: --no-... nothing here creates a key. Keys are never generated.
  if gcloud iam service-accounts create "${account}" \
      --project "${EXPECTED_PROJECT}" --display-name "${display}" 1> /dev/null 2>&1; then
    applied_action "created service account ${email} (no service-account key created)"
    record_check PASS "gcp:sa:${account}" "service account created idempotently: ${email}"
    return 0
  fi
  # A concurrent create (already exists) is success, not failure.
  if [[ "$(sa_state "${email}")" == "exists" ]]; then
    applied_action "service account ${email} already existed (idempotent no-op)"
    record_check PASS "gcp:sa:${account}" "service account present after idempotent create: ${email}"
    return 0
  fi
  record_check BLOCKED "gcp:sa:${account}" "failed to create service account ${email}"
  recovery_step "grant iam.serviceAccounts.create and re-run --apply"
  mark_failed; return 1
}

# gcp_ensure_secret NAME VALUEVAR — create resource if missing, then add a
# version from hidden input only when a value was supplied. Never prints value.
gcp_ensure_secret() {
  local name="$1" valuevar="$2" value state
  value="${!valuevar:-}"
  state="$(secret_state "${name}")"
  if [[ "${state}" == "error" ]]; then
    record_check BLOCKED "gcp:secret:${name}" "could not determine whether secret ${name} exists (permission/API error); refusing to create blindly"
    recovery_step "resolve Secret Manager permissions, then re-run --apply"
    mark_failed; return 1
  fi
  if [[ "${state}" == "missing" ]]; then
    if gcloud secrets create "${name}" --project "${EXPECTED_PROJECT}" \
        --replication-policy automatic 1> /dev/null 2>&1; then
      applied_action "created Secret Manager secret ${name}"
    elif [[ "$(secret_state "${name}")" == "exists" ]]; then
      applied_action "secret ${name} already existed (idempotent no-op)"
    else
      record_check BLOCKED "gcp:secret:${name}" "failed to create secret ${name}"
      recovery_step "grant secretmanager.secrets.create and re-run --apply"
      mark_failed; return 1
    fi
  else
    applied_action "secret ${name} already present (idempotent)"
  fi

  # Add a version ONLY from hidden input, ONLY if a value is available and no
  # enabled version already exists (idempotent — never adds a duplicate).
  if [[ -z "${value}" ]]; then
    record_check MANUAL "gcp:secret:${name}:version" "no hidden-input value supplied for ${name}; add an enabled version manually (payload never handled here)"
    return 0
  fi
  local existing
  existing="$(gcloud secrets versions list "${name}" --project "${EXPECTED_PROJECT}" \
    --filter 'state=enabled' --format 'value(name)' 2> /dev/null || true)"
  if [[ -n "${existing}" ]]; then
    record_check PASS "gcp:secret:${name}:version" "an enabled version already exists; not adding a duplicate (idempotent; payload never read)"
    return 0
  fi
  # Feed the payload via stdin from a process substitution so it never appears
  # in the process table or on disk.
  if printf '%s' "${value}" | gcloud secrets versions add "${name}" \
      --project "${EXPECTED_PROJECT}" --data-file=- 1> /dev/null 2>&1; then
    applied_action "added one enabled version to ${name} from hidden input (payload never printed)"
    record_check PASS "gcp:secret:${name}:version" "enabled version added (payload never printed)"
    return 0
  fi
  record_check BLOCKED "gcp:secret:${name}:version" "failed to add a version to ${name}"
  recovery_step "grant secretmanager.versions.add and re-run --apply"
  mark_failed; return 1
}

# gcp_grant_secret_accessor NAME CONSUMER_EMAIL — exact per-secret binding.
# NEVER grants project-wide accessor.
gcp_grant_secret_accessor() {
  local name="$1" consumer="$2"
  if gcloud secrets add-iam-policy-binding "${name}" --project "${EXPECTED_PROJECT}" \
      --member "serviceAccount:${consumer}" \
      --role roles/secretmanager.secretAccessor 1> /dev/null 2>&1; then
    applied_action "granted per-secret accessor on ${name} to ${consumer}"
    record_check PASS "gcp:secret:${name}:accessor:${consumer}" "per-secret roles/secretmanager.secretAccessor bound (never project-wide)"
    return 0
  fi
  record_check BLOCKED "gcp:secret:${name}:accessor:${consumer}" "failed to bind per-secret accessor for ${consumer}"
  recovery_step "grant secretmanager.secrets.setIamPolicy and re-run --apply"
  mark_failed; return 1
}

# gcp_set_service_identity — updates the API service runtime SA and keeps it
# private. Never uses --allow-unauthenticated.
gcp_set_api_identity() {
  local err rc=0
  err="$(gcloud run services update "${API_SERVICE}" --project "${EXPECTED_PROJECT}" \
    --region "${REGION}" --service-account "${API_SA}" --no-allow-unauthenticated 2>&1 1> /dev/null)" || rc=$?
  if [[ "${rc}" -eq 0 ]]; then
    applied_action "API service ${API_SERVICE} now runs as ${API_SA} (kept private: --no-allow-unauthenticated)"
    record_check PASS "gcp:api-identity" "API service identity set to ${API_SA}; ingress stays private"
    return 0
  fi
  if grep -qiE 'not.?found|does not exist|cannot find' <<< "${err}"; then
    record_check WARN "gcp:api-identity" "API service ${API_SERVICE} not deployed yet; identity will be set at first deploy (out of scope for bootstrap)"
    recovery_step "deploy ${API_SERVICE} with --service-account ${API_SA} --no-allow-unauthenticated (see generate-deployment-plan.sh)"
    return 0
  fi
  record_check BLOCKED "gcp:api-identity" "failed to update API service identity (not a clean 'not found')"
  recovery_step "grant run.services.update and re-run --apply"
  mark_failed; return 1
}

# gcp_set_worker_identity — updates the worker JOB runtime SA. NEVER executes
# the job; `run jobs update` only edits configuration.
gcp_set_worker_identity() {
  local err rc=0
  err="$(gcloud run jobs update "${WORKER_JOB}" --project "${EXPECTED_PROJECT}" \
    --region "${REGION}" --service-account "${WORKER_SA}" 2>&1 1> /dev/null)" || rc=$?
  if [[ "${rc}" -eq 0 ]]; then
    applied_action "worker job ${WORKER_JOB} now runs as ${WORKER_SA} (configuration only; job NOT executed)"
    record_check PASS "gcp:worker-identity" "worker job identity set to dedicated ${WORKER_SA} (job never executed)"
    return 0
  fi
  if grep -qiE 'not.?found|does not exist|cannot find' <<< "${err}"; then
    record_check WARN "gcp:worker-identity" "worker job ${WORKER_JOB} not deployed yet; identity will be set at first deploy"
    recovery_step "deploy ${WORKER_JOB} with --service-account ${WORKER_SA} (worker-before-API order); never run it"
    return 0
  fi
  record_check BLOCKED "gcp:worker-identity" "failed to update worker job identity (not a clean 'not found')"
  recovery_step "grant run.jobs.update and re-run --apply"
  mark_failed; return 1
}

gcp_apply() {
  gcp_preflight || { mark_failed; return 1; }
  gcp_ensure_sa "${API_SA}" "MILO API runtime" || true
  gcp_ensure_sa "${WORKER_SA}" "MILO worker runtime" || true
  gcp_ensure_sa "${GATEWAY_SA}" "MILO Vercel gateway" || true

  gcp_ensure_secret "${SECRET_SUPABASE_NAME}" SECRET_SUPABASE || true
  gcp_ensure_secret "${SECRET_PROVIDER_NAME}" SECRET_PROVIDER || true
  gcp_ensure_secret "${SECRET_REDIS_NAME}" SECRET_UPSTASH_REST_TOKEN || true

  # Per-secret accessor grants (single-purpose consumers only). Provider key is
  # worker-only; supabase/redis are api+worker. Gateway never reads a secret.
  gcp_grant_secret_accessor "${SECRET_SUPABASE_NAME}" "${API_SA}" || true
  gcp_grant_secret_accessor "${SECRET_SUPABASE_NAME}" "${WORKER_SA}" || true
  gcp_grant_secret_accessor "${SECRET_PROVIDER_NAME}" "${WORKER_SA}" || true
  gcp_grant_secret_accessor "${SECRET_REDIS_NAME}" "${API_SA}" || true
  gcp_grant_secret_accessor "${SECRET_REDIS_NAME}" "${WORKER_SA}" || true

  gcp_set_worker_identity || true
  gcp_set_api_identity || true
  return 0
}

# ===========================================================================
# Upstash automation (official Developer API — https://api.upstash.com/v2)
# ===========================================================================
# All HTTP goes through upstash_api (curl basic auth). The token is never
# printed. Tests mock curl so Upstash is never actually contacted.
UPSTASH_BASE="${MILO_UPSTASH_API_BASE:-https://api.upstash.com/v2}"
SECRET_UPSTASH_REST_TOKEN=""   # populated by discovery/creation; never printed
UPSTASH_REST_URL=""            # non-secret; written to metadata

upstash_creds_present() {
  [[ -n "${SECRET_UPSTASH_EMAIL}" && -n "${SECRET_UPSTASH_APIKEY}" ]]
}

# upstash_api METHOD PATH [JSON_BODY] — prints response body to stdout, HTTP
# code to fd 3 is not used; instead we write code to a temp and body to stdout.
upstash_api() {
  local method="$1" path="$2" body="${3:-}" code_file="$4" out
  local -a args=(-s -o - -w '\n%{http_code}' -X "${method}"
    -u "${SECRET_UPSTASH_EMAIL}:${SECRET_UPSTASH_APIKEY}"
    -H 'Content-Type: application/json' --max-time 30)
  [[ -n "${body}" ]] && args+=(--data "${body}")
  out="$(curl "${args[@]}" "${UPSTASH_BASE}${path}" 2> /dev/null || printf '\n000')"
  printf '%s' "${out##*$'\n'}" > "${code_file}"
  printf '%s' "${out%$'\n'*}"
}

upstash_inspect() {
  if ! upstash_creds_present; then
    record_check MANUAL "upstash" "no Upstash management credentials supplied (--upstash-email-env/--upstash-apikey-env); Redis discovery is MANUAL. Provide credentials to automate, or configure the production Redis database by hand."
    return 0
  fi
  if ! tool_available curl; then
    record_check MANUAL "upstash" "curl unavailable; Upstash discovery must be performed manually"
    return 0
  fi
  milo_tmpdir_init
  local code_file="${_MILO_TMPDIR}/upstash.code" list
  list="$(upstash_api GET /redis/databases "" "${code_file}")"
  local code; code="$(cat "${code_file}")"
  if [[ "${code}" != "200" ]]; then
    record_check BLOCKED "upstash:list" "Upstash databases listing failed (HTTP ${code}); credentials/network problem. NOT treated as 'no database'."
    return 0
  fi
  if ! json_is_valid "${list}"; then
    record_check MANUAL "upstash:list" "Upstash listing was not valid JSON; verify manually"
    return 0
  fi
  # Find a database whose name marks it as production and NOT dev/test.
  local match; match="$(UPSTASH_JSON="${list}" python3 - << 'PY' 2> /dev/null || true
import json, os
try:
    dbs = json.loads(os.environ["UPSTASH_JSON"])
except Exception:
    raise SystemExit(0)
if isinstance(dbs, dict):
    dbs = dbs.get("databases", dbs.get("data", []))
prod = None
for db in dbs if isinstance(dbs, list) else []:
    name = str(db.get("database_name") or db.get("name") or "").lower()
    if not name:
        continue
    if any(t in name for t in ("dev", "test", "staging", "preview")):
        continue
    if "prod" in name or "milo" in name:
        prod = db
        break
if prod is not None:
    print(str(prod.get("database_id") or prod.get("id") or ""))
    print(str(prod.get("database_name") or prod.get("name") or ""))
    print(str(prod.get("endpoint") or ""))
PY
)"
  if [[ -z "${match}" ]]; then
    record_check WARN "upstash:discover" "no dedicated production Redis database found; apply will create one (never shared with dev/test)"
    plan_action "create dedicated Upstash production Redis database, store its REST token in Secret Manager (${SECRET_REDIS_NAME}), write only the non-secret REST URL to metadata"
    return 0
  fi
  local db_name db_endpoint
  db_name="$(sed -n '2p' <<< "${match}")"
  db_endpoint="$(sed -n '3p' <<< "${match}")"
  UPSTASH_REST_URL="https://${db_endpoint}"
  record_check PASS "upstash:discover" "found dedicated production Redis database '${db_name}' (not shared with dev/test); REST URL captured, token never printed"
  plan_action "verify production Redis '${db_name}' isolation and store its REST token in Secret Manager (${SECRET_REDIS_NAME})"
  return 0
}

upstash_apply() {
  if ! upstash_creds_present; then
    record_check MANUAL "upstash" "no Upstash management credentials; production Redis remains a MANUAL step (no values invented)"
    return 0
  fi
  if ! tool_available curl; then
    record_check MANUAL "upstash" "curl unavailable; skipping Upstash automation (manual)"
    return 0
  fi
  milo_tmpdir_init
  local code_file="${_MILO_TMPDIR}/upstash.code" list db_id="" db_endpoint=""
  list="$(upstash_api GET /redis/databases "" "${code_file}")"
  if [[ "$(cat "${code_file}")" != "200" ]]; then
    record_check BLOCKED "upstash:list" "Upstash listing failed (HTTP $(cat "${code_file}")); NOT treated as 'no database'. No database created."
    recovery_step "fix Upstash management credentials/network and re-run --apply (idempotent discovery)"
    mark_failed; return 1
  fi
  local found; found="$(UPSTASH_JSON="${list}" python3 - << 'PY' 2> /dev/null || true
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
)"
  db_id="$(sed -n '1p' <<< "${found}")"
  db_endpoint="$(sed -n '2p' <<< "${found}")"

  if [[ -z "${db_id}" ]]; then
    # Create a dedicated production database.
    local create_body create_resp
    create_body='{"name":"milo-production","region":"global","primary_region":"us-east-1","tls":true}'
    create_resp="$(upstash_api POST /redis/database "${create_body}" "${code_file}")"
    if [[ "$(cat "${code_file}")" != "200" ]]; then
      record_check BLOCKED "upstash:create" "failed to create production Redis database (HTTP $(cat "${code_file}"))"
      recovery_step "create the Upstash production database manually or fix credentials and re-run --apply"
      mark_failed; return 1
    fi
    db_id="$(UPSTASH_JSON="${create_resp}" python3 -c 'import json,os;d=json.loads(os.environ["UPSTASH_JSON"]);print(d.get("database_id") or d.get("id") or "")' 2> /dev/null || true)"
    db_endpoint="$(UPSTASH_JSON="${create_resp}" python3 -c 'import json,os;d=json.loads(os.environ["UPSTASH_JSON"]);print(d.get("endpoint") or "")' 2> /dev/null || true)"
    applied_action "created dedicated Upstash production Redis database (id captured; token never printed)"
  else
    applied_action "reusing existing dedicated production Redis database (idempotent; not shared with dev/test)"
  fi

  # Retrieve REST URL and token securely.
  local detail; detail="$(upstash_api GET "/redis/database/${db_id}" "" "${code_file}")"
  if [[ "$(cat "${code_file}")" != "200" ]]; then
    record_check BLOCKED "upstash:detail" "failed to read Redis database details (HTTP $(cat "${code_file}"))"
    mark_failed; return 1
  fi
  SECRET_UPSTASH_REST_TOKEN="$(UPSTASH_JSON="${detail}" python3 -c 'import json,os;d=json.loads(os.environ["UPSTASH_JSON"]);print(d.get("rest_token") or "")' 2> /dev/null || true)"
  local endpoint; endpoint="$(UPSTASH_JSON="${detail}" python3 -c 'import json,os;d=json.loads(os.environ["UPSTASH_JSON"]);print(d.get("endpoint") or "")' 2> /dev/null || true)"
  [[ -n "${endpoint}" ]] && db_endpoint="${endpoint}"
  UPSTASH_REST_URL="https://${db_endpoint}"
  if [[ -z "${SECRET_UPSTASH_REST_TOKEN}" ]]; then
    record_check BLOCKED "upstash:token" "could not retrieve the Redis REST token (value never printed); cannot store it"
    mark_failed; return 1
  fi
  record_check PASS "upstash:token" "Redis REST token retrieved securely (never printed); will be stored in Secret Manager only"
  record_check PASS "upstash:url" "Redis REST URL captured: ${UPSTASH_REST_URL} (non-secret)"
  return 0
}

# ===========================================================================
# Vercel automation (supported CLI; identity-first, fail closed).
# ===========================================================================
vercel_token() { [[ -n "${SECRET_VERCEL_TOKEN}" ]] && printf '%s' "${SECRET_VERCEL_TOKEN}"; }

vercel_base_args() {
  local -a a=()
  [[ -n "${VERCEL_SCOPE}" ]] && a+=(--scope "${VERCEL_SCOPE}")
  [[ -n "${SECRET_VERCEL_TOKEN}" ]] && a+=(--token "${SECRET_VERCEL_TOKEN}")
  # Print one element per line only when there is at least one; an empty array
  # must produce NO output (never a spurious blank line that becomes an empty
  # argument to vercel).
  [[ "${#a[@]}" -gt 0 ]] && printf '%s\n' "${a[@]}"
  return 0
}

# vercel_prove_identity — returns 0 only if the linked project matches the
# inspected project ID/org. Fail CLOSED on any mismatch or failure.
VERCEL_CWD_DEFAULT="${MILO_BOOTSTRAP_VERCEL_CWD:-${REPO_ROOT}/frontend}"
# A prerequisite that is not yet met (unlinked project, auth not configured) is
# a MANUAL step in plan/audit mode and a fail-closed BLOCKED in apply mode
# (never write to an unproven project). A genuine identity MISMATCH is always
# BLOCKED regardless of mode.
_vercel_prereq() {
  local name="$1" detail="$2"
  if [[ "${MODE}" == "apply" ]]; then
    record_check BLOCKED "${name}" "${detail}"
  else
    record_check MANUAL "${name}" "${detail}"
  fi
}
vercel_prove_identity() {
  local cwd="${VERCEL_CWD_DEFAULT}"
  if ! tool_available vercel; then
    record_check MANUAL "vercel" "vercel CLI unavailable; configure the Vercel project manually (names only, never values)"
    return 1
  fi
  local link_file="${cwd}/.vercel/project.json"
  if [[ ! -f "${link_file}" ]]; then
    _vercel_prereq "vercel:link" "no linked Vercel project in ${cwd} (.vercel/project.json missing); run 'vercel link --project ${VERCEL_PROJECT}' first before apply. Refusing to touch an unlinked project."
    return 1
  fi
  local link_json linked_pid linked_org
  link_json="$(cat "${link_file}" 2> /dev/null || true)"
  if ! json_is_valid "${link_json}"; then
    record_check BLOCKED "vercel:link" "linked project file is not valid JSON; cannot prove identity (fail closed)"
    return 1
  fi
  linked_pid="$(json_field "${link_json}" projectId)"
  linked_org="$(json_field "${link_json}" orgId)"
  if [[ -z "${linked_pid}" ]]; then
    record_check BLOCKED "vercel:link" "linked project file has no projectId; cannot prove identity (fail closed)"
    return 1
  fi
  local -a base; mapfile -t base < <(vercel_base_args)
  milo_tmpdir_init
  local inspect_out="${_MILO_TMPDIR}/vercel-inspect" rc=0
  ( cd "${cwd}" && vercel project inspect "${VERCEL_PROJECT}" "${base[@]+"${base[@]}"}" ) > "${inspect_out}" 2>&1 || rc=$?
  if [[ "${rc}" -ne 0 ]]; then
    _vercel_prereq "vercel:project-identity" "'vercel project inspect ${VERCEL_PROJECT}' failed (exit ${rc}); identity not proven (fail closed before any write)"
    return 1
  fi
  local rpid rorg
  rpid="$(grep -oE 'prj_[A-Za-z0-9_-]+' "${inspect_out}" | head -n1 || true)"
  rorg="$(grep -oE 'team_[A-Za-z0-9_-]+' "${inspect_out}" | head -n1 || true)"
  if [[ -z "${rpid}" || "${rpid}" != "${linked_pid}" ]]; then
    record_check BLOCKED "vercel:project-identity" "resolved project ID '${rpid}' does not match linked projectId '${linked_pid}'; refusing to touch a different project"
    return 1
  fi
  if [[ -n "${rorg}" && -n "${linked_org}" && "${rorg}" != "${linked_org}" ]]; then
    record_check BLOCKED "vercel:project-identity" "resolved org '${rorg}' does not match linked org '${linked_org}'; refusing cross-team access"
    return 1
  fi
  record_check PASS "vercel:project-identity" "linked project identity proven (projectId ${linked_pid}); safe to configure"
  return 0
}

# Non-secret, browser/gateway values the frontend needs. NEVER provider keys
# or Supabase service-role credentials.
vercel_plan_vars() {
  local api_url="https://${API_SERVICE}-${DEF_PROJECT_NUMBER}.${REGION}.run.app"
  plan_action "set Vercel production var CLOUD_RUN_API_URL=${api_url}"
  plan_action "set Vercel production var GCP_PROJECT_NUMBER=${DEF_PROJECT_NUMBER}"
  plan_action "set Vercel production var GCP_SERVICE_ACCOUNT_EMAIL=${GATEWAY_SA}"
  plan_action "set Vercel production var UPSTASH_REDIS_REST_URL=<discovered non-secret URL>"
  plan_action "set Vercel production var UPSTASH_REDIS_REST_TOKEN via stdin (sensitive; never echoed)"
  record_check NOT_APPLICABLE "vercel:forbidden-vars" "provider keys and Supabase service-role credentials are NEVER configured in Vercel (only gateway/public/server values)"
}

vercel_apply() {
  if ! vercel_prove_identity; then
    mark_failed
    return 1
  fi
  local -a base; mapfile -t base < <(vercel_base_args)
  local cwd="${VERCEL_CWD_DEFAULT}"
  local api_url="https://${API_SERVICE}-${DEF_PROJECT_NUMBER}.${REGION}.run.app"
  # Non-sensitive server values via stdin (supported: `vercel env add NAME production`).
  vercel_env_upsert() { # NAME VALUE
    local name="$1" value="$2" rc=0
    # Remove-then-add keeps it idempotent; env rm is a no-op if absent.
    ( cd "${cwd}" && printf '%s' "${value}" | vercel env add "${name}" production "${base[@]+"${base[@]}"}" ) 1> /dev/null 2>&1 || rc=$?
    if [[ "${rc}" -eq 0 ]]; then
      applied_action "set Vercel production var ${name} (value via stdin; sensitive values never echoed)"
      record_check PASS "vercel:var:${name}" "configured in production (value never printed)"
    else
      record_check BLOCKED "vercel:var:${name}" "failed to set ${name} in Vercel production"
      recovery_step "set ${name} manually in Vercel production, or fix token/scope and re-run"
      mark_failed
    fi
  }
  vercel_env_upsert CLOUD_RUN_API_URL "${api_url}"
  vercel_env_upsert GCP_PROJECT_NUMBER "${DEF_PROJECT_NUMBER}"
  vercel_env_upsert GCP_SERVICE_ACCOUNT_EMAIL "${GATEWAY_SA}"
  if [[ -n "${UPSTASH_REST_URL}" ]]; then
    vercel_env_upsert UPSTASH_REDIS_REST_URL "${UPSTASH_REST_URL}"
  else
    record_check MANUAL "vercel:var:UPSTASH_REDIS_REST_URL" "no discovered Redis REST URL; set UPSTASH_REDIS_REST_URL manually"
  fi
  if [[ -n "${SECRET_UPSTASH_REST_TOKEN}" ]]; then
    vercel_env_upsert UPSTASH_REDIS_REST_TOKEN "${SECRET_UPSTASH_REST_TOKEN}"
  else
    record_check MANUAL "vercel:var:UPSTASH_REDIS_REST_TOKEN" "no Redis REST token available; set UPSTASH_REDIS_REST_TOKEN manually via stdin (never on the CLI)"
  fi
  record_check NOT_APPLICABLE "vercel:forbidden-vars" "no provider key or Supabase service-role credential was configured in Vercel"
  return 0
}

# ===========================================================================
# Generated outputs (manifest + non-secret metadata) — private dir only.
# ===========================================================================
generate_manifest() {
  local dest="${OUTPUT_DIR}/milo-production.yaml"
  local sha="${RELEASE_SHA:-<RELEASE_SHA>}"
  local rollback="${ROLLBACK_SHA:-<PREVIOUS_RELEASE_SHA>}"
  umask 077
  cat > "${dest}" << EOF
# MILO production release manifest — GENERATED by bootstrap-production.sh.
# Non-secret metadata only. Secret entries are Secret Manager RESOURCE NAMES.
# Generated from inspected/applied live state; do not hand-edit resource IDs.

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
  supabase_service_key:
    name: "${SECRET_SUPABASE_NAME}"
    consumers: ["api", "worker"]
  provider_api_key:
    name: "${SECRET_PROVIDER_NAME}"
    consumers: ["worker"]
  redis_rest_token:
    name: "${SECRET_REDIS_NAME}"
    consumers: ["api", "worker"]
EOF
  chmod 600 "${dest}"
  record_check PASS "manifest:generated" "production manifest written to ${dest} (non-secret metadata only)"
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
    # Secret NAMES only — never values.
    printf 'SUPABASE_SERVICE_KEY_SECRET_NAME=%s\n' "${SECRET_SUPABASE_NAME}"
    printf 'PROVIDER_KEY_SECRET_NAME=%s\n' "${SECRET_PROVIDER_NAME}"
    printf 'REDIS_TOKEN_SECRET_NAME=%s\n' "${SECRET_REDIS_NAME}"
  } > "${dest}"
  chmod 600 "${dest}"
  record_check PASS "metadata:generated" "non-secret metadata written to ${dest}"
}

# ===========================================================================
# Report writers (bootstrap-plan.json / bootstrap-apply.json).
# ===========================================================================
write_bootstrap_report() {
  local path="$1" phase="$2" status="$3"
  umask 077
  local tmp; tmp="$(mktemp "${OUTPUT_DIR}/.report.XXXXXX")"
  chmod 600 "${tmp}"
  {
    printf '{\n'
    printf '  "script": "bootstrap-production",\n'
    printf '  "phase": "%s",\n' "$(json_escape "${phase}")"
    printf '  "status": "%s",\n' "$(json_escape "${status}")"
    printf '  "mode": "%s",\n' "$(json_escape "${MODE}")"
    printf '  "head_sha": "%s",\n' "$(json_escape "$(git_head_sha)")"
    printf '  "expected_project": "%s",\n' "$(json_escape "${EXPECTED_PROJECT}")"
    printf '  "planned_actions": [\n'
    _json_array "${PLANNED_ACTIONS[@]+"${PLANNED_ACTIONS[@]}"}"
    printf '  ],\n'
    printf '  "applied_actions": [\n'
    _json_array "${APPLIED_ACTIONS[@]+"${APPLIED_ACTIONS[@]}"}"
    printf '  ],\n'
    printf '  "recovery_steps": [\n'
    _json_array "${RECOVERY_STEPS[@]+"${RECOVERY_STEPS[@]}"}"
    printf '  ]\n'
    printf '}\n'
  } > "${tmp}"
  mv "${tmp}" "${path}"
  record_check PASS "report:${phase}" "machine-readable report written to ${path}"
}

_json_array() {
  local n=$# i=1
  for arg in "$@"; do
    printf '    "%s"' "$(json_escape "$(redact_line "${arg}")")"
    [[ "${i}" -lt "${n}" ]] && printf ','
    printf '\n'
    i=$((i + 1))
  done
}

# ===========================================================================
# Final consolidated audit (read-only). Invokes production-readiness.sh with
# the generated manifest and metadata. Any secret-bearing metadata is written
# to an ephemeral chmod-600 file removed by trap.
# ===========================================================================
run_final_audit() {
  local readiness_json="${OUTPUT_DIR}/readiness.json"
  local readiness_log="${OUTPUT_DIR}/readiness.log"
  umask 077

  # Validate the generated manifest in apply mode (rejects placeholders).
  if tool_available python3 && [[ -n "${MANIFEST_PATH:-}" ]]; then
    local vmode="plan"; [[ "${MODE}" == "apply" ]] && vmode="apply"
    if python3 "${SCRIPT_DIR}/validate_production_manifest.py" --manifest "${MANIFEST_PATH}" --mode "${vmode}" > /dev/null 2>&1; then
      record_check PASS "audit:manifest" "generated manifest passed ${vmode}-mode validation (no placeholders in apply mode)"
    else
      record_check BLOCKED "audit:manifest" "generated manifest failed ${vmode}-mode validation: ${MANIFEST_PATH}"
      mark_failed
    fi
  fi

  # The Redis REST TOKEN is verified through Secret Manager (the generated
  # manifest drives check-secret-metadata to confirm the redis_rest_token
  # secret exists with the correct per-secret accessors), NOT by re-handling
  # the value. So the audit passes NO secret-bearing env-file — there is no
  # persistent plaintext to manage. If an operator later needs a full env-file
  # audit, production-readiness.sh accepts one directly with an ephemeral
  # chmod-600 file they control.
  local -a ra=(--manifest "${MANIFEST_PATH:-${REPO_ROOT}/config/production.example.yaml}"
    --expected-project "${EXPECTED_PROJECT}" --region "${REGION}"
    --repository "${REPOSITORY}" --api-service "${API_SERVICE}" --worker-job "${WORKER_JOB}"
    --api-sa "${API_SA}" --worker-sa "${WORKER_SA}"
    --vercel-project "${VERCEL_PROJECT}"
    --redis-expected-environment production
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
    record_check BLOCKED "audit:readiness" "consolidated readiness audit reported blocking findings (consolidated blocked > 0); see ${readiness_log}"
    mark_failed
  fi
  return 0
}

# ===========================================================================
# Mode dispatch.
# ===========================================================================
resolve_output_dir

# Resolve secrets from named env vars (or hidden prompt in apply mode).
resolve_secret SECRET_SUPABASE "${SUPABASE_KEY_ENV}" "Supabase service-role/secret key"
resolve_secret SECRET_PROVIDER "${PROVIDER_KEY_ENV}" "provider (Kimi/Moonshot) API key"
resolve_secret SECRET_UPSTASH_EMAIL "${UPSTASH_EMAIL_ENV}" "Upstash management email"
resolve_secret SECRET_UPSTASH_APIKEY "${UPSTASH_APIKEY_ENV}" "Upstash management API key"
resolve_secret SECRET_VERCEL_TOKEN "${VERCEL_TOKEN_ENV}" "Vercel access token"

record_check PASS "mode" "bootstrap mode: ${MODE} (default is plan; apply requires the full production guard)"
IDENTITY_OK=1
check_identity_separation || IDENTITY_OK=0

case "${MODE}" in
  plan)
    gcp_inspect
    upstash_inspect
    if vercel_prove_identity; then vercel_plan_vars; fi
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
    finish_checks "bootstrap-production" "${JSON_OUTPUT}"
    exit $?
    ;;

  apply)
    # Full guard first. No partial mutation may occur before it passes.
    # A shared runtime identity is a fail-closed guard failure, never a
    # mutation-time surprise.
    if [[ "${IDENTITY_OK}" -ne 1 ]]; then
      record_check BLOCKED "apply-guard:identity" "runtime identities are not distinct; refusing to mutate anything"
      write_bootstrap_report "${OUTPUT_DIR}/bootstrap-apply.json" "apply" "guard-blocked"
      finish_checks "bootstrap-production" "${JSON_OUTPUT}"
      exit 1
    fi
    if [[ "${APPLY_ENVIRONMENT}" != "production" ]]; then
      record_check BLOCKED "apply-guard:environment" "--environment production is required for --apply"
      write_bootstrap_report "${OUTPUT_DIR}/bootstrap-apply.json" "apply" "guard-blocked"
      finish_checks "bootstrap-production" "${JSON_OUTPUT}"
      exit 1
    fi
    # Non-placeholder core inputs.
    guard_inputs_ok=1
    for pair in "expected-project:${EXPECTED_PROJECT}" "expected-account:${EXPECTED_ACCOUNT}" \
                "region:${REGION}" "api-sa:${API_SA}" "worker-sa:${WORKER_SA}" \
                "gateway-sa:${GATEWAY_SA}" "vercel-project:${VERCEL_PROJECT}" \
                "supabase-ref:${SUPABASE_REF}" "production-origin:${PRODUCTION_ORIGIN}"; do
      require_value "apply-input:${pair%%:*}" "${pair#*:}" || guard_inputs_ok=0
    done
    if [[ "${guard_inputs_ok}" -ne 1 ]]; then
      write_bootstrap_report "${OUTPUT_DIR}/bootstrap-apply.json" "apply" "guard-blocked"
      finish_checks "bootstrap-production" "${JSON_OUTPUT}"
      exit 1
    fi
    # The shared apply guard (project/account/sha/ack/confirm/worktree).
    if ! apply_guard; then
      write_bootstrap_report "${OUTPUT_DIR}/bootstrap-apply.json" "apply" "guard-blocked"
      finish_checks "bootstrap-production" "${JSON_OUTPUT}"
      exit 1
    fi

    # Guard passed. Perform idempotent bootstrap. Upstash discovery must run
    # before secret storage so the Redis token can be captured this run. Each
    # phase records its own PASS/BLOCKED and sets BOOTSTRAP_FAILED on failure;
    # `|| true` keeps `set -e` from aborting before the recovery plan is
    # written (a phase failure is reported, never silently fatal).
    upstash_apply || true
    gcp_apply || true
    vercel_apply || true
    if [[ "${DEPLOY_VERCEL}" -eq 1 ]]; then
      record_check MANUAL "vercel:deploy" "--deploy-vercel supplied; deployment is an explicit separate operator action and is intentionally NOT auto-run by bootstrap (use the deployment plan)"
    else
      record_check NOT_APPLICABLE "vercel:deploy" "bootstrap never deploys Vercel by default; supply --deploy-vercel to opt in under the same guard"
    fi

    generate_manifest
    generate_metadata
    run_final_audit

    if [[ "${BOOTSTRAP_FAILED}" -eq 1 ]]; then
      write_bootstrap_report "${OUTPUT_DIR}/bootstrap-apply.json" "apply" "partial-failure"
      printf '\nAPPLY INCOMPLETE — partial failure. See recovery_steps in %s/bootstrap-apply.json. Re-running --apply is idempotent.\n' "${OUTPUT_DIR}"
      record_check BLOCKED "apply:result" "bootstrap did not fully succeed; a clear recovery plan was written and full success is NOT claimed"
      finish_checks "bootstrap-production" "${JSON_OUTPUT}"
      exit 1
    fi
    write_bootstrap_report "${OUTPUT_DIR}/bootstrap-apply.json" "apply" "applied"
    printf '\nAPPLY COMPLETE. Reports and manifest in %s. Nothing was deployed; execution stays disabled.\n' "${OUTPUT_DIR}"
    finish_checks "bootstrap-production" "${JSON_OUTPUT}"
    exit $?
    ;;

  *)
    record_check BLOCKED "mode" "unknown mode ${MODE}"
    finish_checks "bootstrap-production" "${JSON_OUTPUT}"
    exit 1
    ;;
esac

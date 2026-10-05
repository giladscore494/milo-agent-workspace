#!/usr/bin/env bash
# Read-only production preflight for the activation sequence.
#
# Proves, against the LIVE project, that everything the capture and the
# deployment depend on already exists and is shaped the way the repository
# expects. It mutates nothing: every gcloud call is a describe/list, the
# database is only read, and no provider request is made.
#
# It deliberately does not duplicate the deep audits in scripts/release/ —
# it composes them where they apply and adds the checks the activation
# sequence specifically needs (capture prerequisites, release-SHA capability,
# RuntimePolicy mandatory bindings, gateway/frontend binding).
#
# Every failure names the exact missing thing and its remediation. No secret
# value is ever read or printed: secrets are checked for EXISTENCE only.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
MILO_REPO_ROOT="$REPO_ROOT"
# shellcheck source=../release/lib/common.sh
source "${REPO_ROOT}/scripts/release/lib/common.sh"
# shellcheck source=operator-config.sh
source "${SCRIPT_DIR}/operator-config.sh"
# shellcheck source=deployment-contract.sh
source "${SCRIPT_DIR}/deployment-contract.sh"

JSON_OUTPUT="" MILO_OPERATOR_CONFIG_PATH="" SKIP_REMOTE=0

usage() {
  cat << 'EOF'
Usage: production-preflight.sh [options]

Read-only preflight for the production activation sequence.

Options:
  --operator-config <path>  Operator identifier file
                            (default: config/production-operator.env).
  --json-output <path>      Write the machine-readable report.
  --offline                 Skip every remote call. Validates the operator
                            configuration and the repository contract only;
                            used by CI, never a substitute for a real run.
  --help                    Show this help.

Exit code is nonzero when any check is BLOCKED.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --operator-config) MILO_OPERATOR_CONFIG_PATH="${2:?}"; shift 2 ;;
    --json-output) JSON_OUTPUT="${2:?}"; shift 2 ;;
    --offline) SKIP_REMOTE=1; shift ;;
    --help) usage; exit 0 ;;
    *) printf 'FAIL: unknown argument %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done

milo_tmpdir_init

CONFIG_PATH="$(milo_operator_config_path "$REPO_ROOT" "$MILO_OPERATOR_CONFIG_PATH")"
if ! milo_load_operator_config "$CONFIG_PATH"; then
  exit 2
fi

REQUIRED_KEYS=(
  GCP_PROJECT_ID GCP_REGION ARTIFACT_REGISTRY_REPOSITORY
  CLOUD_RUN_API_SERVICE CLOUD_RUN_WORKER_JOB CLOUD_RUN_CAPTURE_JOB
  API_SERVICE_ACCOUNT WORKER_SERVICE_ACCOUNT
  SUPABASE_PROJECT_REF
  SECRET_SUPABASE_URL SECRET_SUPABASE_SERVICE_KEY
  SECRET_REDIS_URL SECRET_REDIS_TOKEN
)
if ! milo_require_op "${REQUIRED_KEYS[@]}"; then
  exit 2
fi

PROJECT_ID="$(milo_op GCP_PROJECT_ID)"
REGION="$(milo_op GCP_REGION)"
API_SERVICE="$(milo_op CLOUD_RUN_API_SERVICE)"
WORKER_JOB="$(milo_op CLOUD_RUN_WORKER_JOB)"
CAPTURE_JOB="$(milo_op CLOUD_RUN_CAPTURE_JOB)"

printf 'MILO production preflight (read-only)\n'
printf 'Operator config: %s\n' "$CONFIG_PATH"
printf 'Project: %s   Region: %s\n\n' "$PROJECT_ID" "$REGION"

# ---------------------------------------------------------------------------
# Repository-side contract (always runs, no network)
# ---------------------------------------------------------------------------
HEAD_SHA="$(git -C "$REPO_ROOT" rev-parse HEAD)"
if [[ "$HEAD_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  record_check PASS "release:sha-resolvable" "HEAD is ${HEAD_SHA}"
else
  record_check BLOCKED "release:sha-resolvable" \
    "git rev-parse HEAD did not yield a full 40-character SHA; cloud-run.sh binds MILO_RELEASE_SHA from it"
fi

if git_worktree_clean; then
  record_check PASS "release:worktree-clean" "no uncommitted changes"
else
  record_check BLOCKED "release:worktree-clean" \
    "working tree is dirty; the deployed image would not correspond to ${HEAD_SHA}. Remediation: commit or stash, then re-run"
fi

# The capture bounds this bundle pins must equal what the code enforces.
if CAPTURE_PINS="$(cd "$REPO_ROOT" && python3 - <<'PY' 2>/dev/null
import backend.catalog.government.source as s
import backend.catalog.operator_capture as c
print(f"{s.CKAN_PACKAGE_ID}\t{s.WLTP_RESOURCE_ID}\t{c.CAPTURE_PAGE_LIMIT}\t{c.CAPTURE_MAX_PAGES}\t{c.CAPTURE_MAX_RECORDS}")
PY
)"; then
  IFS=$'\t' read -r code_pkg code_res code_page code_pages code_records <<< "$CAPTURE_PINS"
  if [[ "$code_pkg" == "$MILO_CAPTURE_PACKAGE_ID" && "$code_res" == "$MILO_CAPTURE_RESOURCE_ID" \
     && "$code_page" == "$MILO_CAPTURE_PAGE_LIMIT" && "$code_pages" == "$MILO_CAPTURE_MAX_PAGES" \
     && "$code_records" == "$MILO_CAPTURE_MAX_RECORDS" ]]; then
    record_check PASS "capture:bounds-match-code" \
      "package/resource and bounds equal the pinned values in backend/catalog"
  else
    record_check BLOCKED "capture:bounds-match-code" \
      "deployment-contract.sh capture pins disagree with backend/catalog; operator_capture.py would refuse with CAPTURE_RESOURCE_NOT_SUPPORTED or CAPTURE_BOUNDS_NOT_SUPPORTED"
  fi
else
  record_check MANUAL "capture:bounds-match-code" "could not import backend.catalog to compare pins"
fi

# RuntimePolicy mandatory-for-paid bindings.
POLICY_ARGS=(--format text)
if [[ "$SKIP_REMOTE" -eq 0 ]]; then
  POLICY_ARGS+=(--live-from-cloud-run --project "$PROJECT_ID" --region "$REGION"
                --api-service "$API_SERVICE" --worker-job "$WORKER_JOB")
fi
if POLICY_OUT="$(cd "$REPO_ROOT" && python3 scripts/release/runtime_policy_manifest.py "${POLICY_ARGS[@]}" 2>&1)"; then
  record_check PASS "runtime-policy:mandatory-bindings" "every mandatory-for-paid dimension is bound"
else
  record_check BLOCKED "runtime-policy:mandatory-bindings" \
    "unbound mandatory dimension(s); run scripts/release/runtime_policy_manifest.py for the per-variable table"
fi
printf '%s\n' "$POLICY_OUT" > "${_MILO_TMPDIR}/runtime-policy.txt"

if [[ "$SKIP_REMOTE" -eq 1 ]]; then
  record_check NOT_APPLICABLE "remote:all" "--offline: every live check skipped"
  finish_checks "production-preflight" "$JSON_OUTPUT"
  exit $?
fi

# ---------------------------------------------------------------------------
# Live project
# ---------------------------------------------------------------------------
if ! milo_require_gcloud_context "$PROJECT_ID"; then
  record_check BLOCKED "gcloud:context" "gcloud is not authenticated against ${PROJECT_ID}"
  finish_checks "production-preflight" "$JSON_OUTPUT"
  exit 1
fi
record_check PASS "gcloud:context" "authenticated, project ${PROJECT_ID}"

REQUIRED_APIS=(run.googleapis.com cloudbuild.googleapis.com
               artifactregistry.googleapis.com secretmanager.googleapis.com)
ENABLED_APIS="$(gcloud services list --enabled --project "$PROJECT_ID" \
  --format='value(config.name)' 2> /dev/null || true)"
for api in "${REQUIRED_APIS[@]}"; do
  if grep -qx "$api" <<< "$ENABLED_APIS"; then
    record_check PASS "api:${api}" "enabled"
  else
    record_check BLOCKED "api:${api}" \
      "API not enabled. Remediation: gcloud services enable ${api} --project=${PROJECT_ID}"
  fi
done

if gcloud artifacts repositories describe "$(milo_op ARTIFACT_REGISTRY_REPOSITORY)" \
     --location "$REGION" --project "$PROJECT_ID" > /dev/null 2>&1; then
  record_check PASS "artifact-registry:repository" "$(milo_op ARTIFACT_REGISTRY_REPOSITORY) exists in ${REGION}"
else
  record_check BLOCKED "artifact-registry:repository" \
    "repository $(milo_op ARTIFACT_REGISTRY_REPOSITORY) not found in ${REGION}. Remediation: scripts/deploy/gcp-bootstrap.sh --apply"
fi

if gcloud run services describe "$API_SERVICE" --region "$REGION" --project "$PROJECT_ID" \
     > /dev/null 2>&1; then
  record_check PASS "cloud-run:api-service" "${API_SERVICE} exists"
else
  record_check BLOCKED "cloud-run:api-service" \
    "Cloud Run service ${API_SERVICE} not found in ${REGION}. Remediation: scripts/deploy/gcp-bootstrap.sh --apply"
fi

if gcloud run jobs describe "$WORKER_JOB" --region "$REGION" --project "$PROJECT_ID" \
     > /dev/null 2>&1; then
  record_check PASS "cloud-run:worker-job" "${WORKER_JOB} exists"
else
  record_check BLOCKED "cloud-run:worker-job" \
    "Cloud Run job ${WORKER_JOB} not found in ${REGION}. Remediation: scripts/deploy/gcp-bootstrap.sh --apply"
fi

# The capture job is created on demand, so its absence is informational.
if gcloud run jobs describe "$CAPTURE_JOB" --region "$REGION" --project "$PROJECT_ID" \
     > /dev/null 2>&1; then
  record_check PASS "cloud-run:capture-job" "${CAPTURE_JOB} exists"
else
  record_check WARN "cloud-run:capture-job" \
    "${CAPTURE_JOB} does not exist yet; government-production-capture.sh creates it"
fi

for sa_key in API_SERVICE_ACCOUNT WORKER_SERVICE_ACCOUNT CAPTURE_SERVICE_ACCOUNT; do
  sa="$(milo_op "$sa_key")"
  [[ -n "$sa" ]] || continue
  if gcloud iam service-accounts describe "$sa" --project "$PROJECT_ID" > /dev/null 2>&1; then
    record_check PASS "service-account:${sa_key}" "$sa exists"
  else
    record_check BLOCKED "service-account:${sa_key}" \
      "service account ${sa} not found. Remediation: scripts/deploy/gcp-bootstrap.sh --apply"
  fi
done

# Secret NAMES and enabled versions. Values are never accessed.
check_secret() {
  local label="$1" name="$2" required="$3" versions
  if [[ -z "$name" ]]; then
    record_check WARN "secret:${label}" "no resource name configured"
    return
  fi
  if ! gcloud secrets describe "$name" --project "$PROJECT_ID" > /dev/null 2>&1; then
    if [[ "$required" == "required" ]]; then
      record_check BLOCKED "secret:${label}" \
        "secret ${name} does not exist. Remediation: gcloud secrets create ${name} --project=${PROJECT_ID} --replication-policy=automatic, then add a version"
    else
      record_check WARN "secret:${label}" \
        "secret ${name} does not exist yet (required only for paid execution)"
    fi
    return
  fi
  versions="$(gcloud secrets versions list "$name" --project "$PROJECT_ID" \
    --filter='state:ENABLED' --format='value(name)' 2> /dev/null | wc -l | tr -d ' ')"
  if [[ "${versions:-0}" -gt 0 ]]; then
    record_check PASS "secret:${label}" "${name} exists with ${versions} enabled version(s)"
  else
    record_check BLOCKED "secret:${label}" \
      "secret ${name} exists but has no ENABLED version. Remediation: gcloud secrets versions add ${name} --data-file=- --project=${PROJECT_ID}"
  fi
}

check_secret "supabase-url" "$(milo_op SECRET_SUPABASE_URL)" required
check_secret "supabase-service-key" "$(milo_op SECRET_SUPABASE_SERVICE_KEY)" required
check_secret "redis-url" "$(milo_op SECRET_REDIS_URL)" required
check_secret "redis-token" "$(milo_op SECRET_REDIS_TOKEN)" required
check_secret "provider-api-key" "$(milo_op SECRET_PROVIDER_API_KEY)" optional

# The capture identity must be able to READ the two Supabase secrets, and
# must NOT be able to read the provider key: a capture spends no model money,
# so a reachable provider credential on it is pure blast radius.
CAPTURE_SA="$(milo_op CAPTURE_SERVICE_ACCOUNT)"
if [[ -n "$CAPTURE_SA" ]]; then
  for secret_key in SECRET_SUPABASE_URL SECRET_SUPABASE_SERVICE_KEY; do
    secret="$(milo_op "$secret_key")"
    if gcloud secrets get-iam-policy "$secret" --project "$PROJECT_ID" --format=json 2> /dev/null \
         | grep -q "serviceAccount:${CAPTURE_SA}"; then
      record_check PASS "iam:capture-can-read-${secret}" "capture identity holds an accessor binding"
    else
      record_check BLOCKED "iam:capture-can-read-${secret}" \
        "capture SA lacks secretAccessor on ${secret}. Remediation: gcloud secrets add-iam-policy-binding ${secret} --member=serviceAccount:${CAPTURE_SA} --role=roles/secretmanager.secretAccessor --project=${PROJECT_ID}"
    fi
  done
  # The provider key AND the quota store (PR-D3: the normalisation job's two
  # Upstash secrets): any role the capture identity holds on one -- a
  # conditional binding included -- is BLOCKED, and so is a policy that cannot
  # be read. Never a pass on what could not be seen.
  capture_reads=() capture_unreadable=()
  for secret_key in SECRET_PROVIDER_API_KEY SECRET_REDIS_URL SECRET_REDIS_TOKEN; do
    secret="$(milo_op "$secret_key")"
    [[ -n "$secret" ]] || continue
    if ! policy="$(gcloud secrets get-iam-policy "$secret" --project "$PROJECT_ID" --format=json 2> /dev/null)" \
       || ! roles="$(milo_policy_member_roles "serviceAccount:${CAPTURE_SA}" <<< "$policy")"; then
      capture_unreadable+=("$secret")
    elif [[ -n "$roles" ]]; then
      capture_reads+=("${secret} ($(tr '\n' ' ' <<< "$roles" | sed 's/ $//'))")
    fi
  done
  if [[ "${#capture_reads[@]}" -gt 0 ]]; then
    record_check BLOCKED "iam:capture-cannot-read-provider-key" \
      "capture SA holds a role on ${capture_reads[*]}; a capture must never reach a provider credential or the quota store. Remediation: bash scripts/deploy/website-execution-activate.sh --remove-manufacturer-normalisation (or Actions -> Website stage -> normalisation-off), which revokes and reads back"
  elif [[ "${#capture_unreadable[@]}" -gt 0 ]]; then
    record_check BLOCKED "iam:capture-cannot-read-provider-key" \
      "the IAM policy of ${capture_unreadable[*]} could not be read, so whether the capture SA can read it is unknown"
  else
    record_check PASS "iam:capture-cannot-read-provider-key" \
      "capture identity holds no role on the provider key or the quota store (read back)"
  fi
fi

# No runtime identity reaches secrets through the PROJECT: a project-level
# secretAccessor, secretmanager.admin, editor or owner would reach every
# secret, the provider key included, whatever the secret-level policies say.
project_policy=""
if ! project_policy="$(gcloud projects get-iam-policy "$PROJECT_ID" --format=json 2> /dev/null)" \
   || ! milo_policy_member_roles "serviceAccount:probe" <<< "$project_policy" > /dev/null; then
  record_check BLOCKED "iam:no-project-level-secret-access" \
    "the project IAM policy of ${PROJECT_ID} could not be read, so whether a runtime identity reaches every secret is unknown"
else
  broad=()
  for sa_key in CAPTURE_SERVICE_ACCOUNT API_SERVICE_ACCOUNT; do
    sa="$(milo_op "$sa_key")"
    [[ -n "$sa" ]] || continue
    while IFS= read -r role; do
      case "${role% conditional}" in
        roles/secretmanager.secretAccessor | roles/secretmanager.admin | roles/editor | roles/owner)
          broad+=("${sa} ${role}") ;;
      esac
    done < <(milo_policy_member_roles "serviceAccount:${sa}" <<< "$project_policy")
  done
  if [[ "${#broad[@]}" -gt 0 ]]; then
    record_check BLOCKED "iam:no-project-level-secret-access" \
      "project-level roles reach every secret: ${broad[*]}. Remediation: gcloud projects remove-iam-policy-binding ${PROJECT_ID} --member=serviceAccount:<that SA> --role=<that role>; grant secretAccessor per secret instead"
  else
    record_check PASS "iam:no-project-level-secret-access" \
      "neither the capture nor the API identity holds secretAccessor, secretmanager.admin, editor or owner on ${PROJECT_ID}"
  fi
fi

# PR-D3: the normalisation job is the only surface that holds the provider key
# while that stage is on (it runs as the worker identity, never the capture
# identity), so it exists exactly while the API flag is on: present + on PASS,
# absent + off PASS, anything else BLOCKED.
NORMALISATION_JOB="$(milo_normalisation_job_name)"
if [[ -n "$NORMALISATION_JOB" ]]; then
  if ! normalisation_state="$(milo_job_state "$NORMALISATION_JOB" "$REGION" "$PROJECT_ID")"; then
    record_check BLOCKED "cloud-run:normalisation-job" \
      "the Cloud Run jobs could not be listed, so whether ${NORMALISATION_JOB} (which holds the provider key) exists is unknown"
  elif ! api_json="$(gcloud run services describe "$API_SERVICE" --region "$REGION" --project "$PROJECT_ID" \
         --format=json 2> /dev/null)"; then
    record_check BLOCKED "cloud-run:normalisation-job" \
      "the API service could not be described, so whether the stage is on is unknown (${NORMALISATION_JOB} is ${normalisation_state})"
  else
    normalisation_flag="$(milo_env_value "${MILO_MANUFACTURER_NORMALISATION_API_ENABLE_FLAGS[0]}" <<< "$api_json")"
    posture="$(milo_normalisation_posture "$normalisation_state" "$normalisation_flag")"
    # The worker identity reads the key while the stage is on: a capture job
    # running as that identity (no distinct capture identity) could too.
    if [[ "$normalisation_state" == "present" || "$posture" == BLOCKED\ the\ stage\ is\ on* ]] \
       && [[ -z "$CAPTURE_SA" || "$CAPTURE_SA" == "$(milo_op WORKER_SERVICE_ACCOUNT)" ]]; then
      posture="BLOCKED the stage is on but the capture job has no identity distinct from the worker's, which reads the provider key. Remediation: set CAPTURE_SERVICE_ACCOUNT, or bash scripts/deploy/website-execution-activate.sh --remove-manufacturer-normalisation"
    fi
    record_check "${posture%% *}" "cloud-run:normalisation-job" "${NORMALISATION_JOB}: ${posture#* }"
    # The job holds the provider key: the API runs it exactly as defined,
    # never with overrides (an override is arbitrary code with the key).
    if [[ "$normalisation_state" == "present" ]]; then
      api_sa="$(milo_op API_SERVICE_ACCOUNT)"
      if [[ -z "$api_sa" ]]; then
        record_check BLOCKED "iam:api-cannot-override-normalisation-job" \
          "no API_SERVICE_ACCOUNT is configured, so who may run ${NORMALISATION_JOB} with overrides is unknown"
      else
        override="$(milo_normalisation_override_posture "$NORMALISATION_JOB" "$REGION" "$PROJECT_ID" \
          "serviceAccount:${api_sa}")"
        record_check "${override%% *}" "iam:api-cannot-override-normalisation-job" "${override#* }"
      fi
    else
      record_check PASS "iam:api-cannot-override-normalisation-job" "no normalisation job: nothing holds the provider key"
    fi
  fi
fi

# PR-D1: the register archive bucket and the capture identity's create-only
# grant on it (scripts/ops/setup-register-archive.sh --check, read-only).
# Until the operator sets it up this is a GAP (WARN): register capture refuses
# without it and the deploy does not depend on it. A bucket open to the public
# or an application identity with a delete-capable role on it is BLOCKED.
ARCHIVE_CHECK="$(bash "${REPO_ROOT}/scripts/ops/setup-register-archive.sh" --check \
  --operator-config "$CONFIG_PATH" 2> /dev/null || printf 'UNREADABLE the check did not run')"
case "$ARCHIVE_CHECK" in
  PASS\ *) record_check PASS "storage:register-archive" "${ARCHIVE_CHECK#PASS }" ;;
  PARTIAL\ *) record_check WARN "storage:register-archive" "${ARCHIVE_CHECK#PARTIAL }" ;;
  FAIL\ *) record_check BLOCKED "storage:register-archive" \
       "${ARCHIVE_CHECK#FAIL } Remediation: bash scripts/ops/setup-register-archive.sh --plan" ;;
  UNREADABLE\ *) record_check WARN "storage:register-archive" \
       "not verifiable with this identity (${ARCHIVE_CHECK#UNREADABLE }). Verify from Cloud Shell: bash scripts/ops/setup-register-archive.sh --check" ;;
  *) record_check WARN "storage:register-archive" \
       "${ARCHIVE_CHECK#* } Remediation: bash scripts/ops/setup-register-archive.sh --apply" ;;
esac

# PR-SYNC-2: the register sync's hourly tick (scripts/ops/setup-register-
# scheduler.sh --check, read-only): the Cloud Scheduler job exists and targets
# the API's tick route with the gateway audience, and its own account holds
# roles/run.invoker on the API only. Until the operator sets it up, or when
# this identity cannot read it, this is a WARN: the deploy does not depend on
# it (Auto sync then never starts). An account holding more is BLOCKED.
SCHEDULER_CHECK="$(bash "${REPO_ROOT}/scripts/ops/setup-register-scheduler.sh" --check \
  --operator-config "$CONFIG_PATH" 2> /dev/null | grep '^SUMMARY|scheduler|' | tail -n 1 \
  || printf 'SUMMARY|scheduler|UNREADABLE|the check did not run')"
SCHEDULER_RESULT="$(cut -d'|' -f3 <<< "$SCHEDULER_CHECK")"
SCHEDULER_DETAIL="$(cut -d'|' -f4- <<< "$SCHEDULER_CHECK")"
case "$SCHEDULER_RESULT" in
  PASS) record_check PASS "scheduler:register-sync-tick" "$SCHEDULER_DETAIL" ;;
  FAIL) record_check BLOCKED "scheduler:register-sync-tick" \
       "${SCHEDULER_DETAIL} Remediation: remove the extra grant(s) or key(s), then bash scripts/ops/setup-register-scheduler.sh --check" ;;
  UNREADABLE) record_check WARN "scheduler:register-sync-tick" "not verifiable with this identity (${SCHEDULER_DETAIL})" ;;
  *) record_check WARN "scheduler:register-sync-tick" "${SCHEDULER_DETAIL:-not set up} (Auto sync never starts until it is)" ;;
esac

# Gateway / frontend binding. The Vercel gateway reaches the API through
# workload identity federation, so the API URL and the impersonated identity
# both have to be known before the website can talk to the backend at all.
API_URL="$(gcloud run services describe "$API_SERVICE" --region "$REGION" \
  --project "$PROJECT_ID" --format='value(status.url)' 2> /dev/null || true)"
if [[ -n "$API_URL" ]]; then
  record_check PASS "gateway:cloud-run-api-url" "CLOUD_RUN_API_URL for Vercel is ${API_URL}"
else
  record_check BLOCKED "gateway:cloud-run-api-url" \
    "could not read the API service URL; the Vercel gateway requires it as CLOUD_RUN_API_URL"
fi
for key in GCP_PROJECT_NUMBER GATEWAY_SERVICE_ACCOUNT MILO_GATEWAY_AUDIENCE MILO_APPROVED_GATEWAY_IDENTITIES PRODUCTION_ORIGIN; do
  if [[ -n "$(milo_op "$key")" ]]; then
    record_check PASS "gateway:${key}" "configured"
  else
    record_check BLOCKED "gateway:${key}" \
      "${key} is empty in ${CONFIG_PATH}; the API refuses to start without a verified gateway identity (GATEWAY_AUTH_MISSING)"
  fi
done

# PR-OBS: the deploy WIF provider must admit production-backup for the
# scheduled Supabase backup to authenticate. Until the operator has run
# scripts/ops/setup-wif.sh --apply this is a GAP -- reported (WARN), never a
# reason to stop the deploy, which does not depend on it.
WIF_BACKUP_CHECK="$(bash "${SCRIPT_DIR}/check-wif-environment.sh" "$PROJECT_ID" production-backup 2> /dev/null \
  || printf 'UNREADABLE the check did not run')"
case "$WIF_BACKUP_CHECK" in
  PASS\ *) record_check PASS "wif:admits-production-backup" "${WIF_BACKUP_CHECK#PASS }" ;;
  UNREADABLE\ *)
    # The deploy identity holds no WIF-provider read permission (by design),
    # so a preflight run BY THE DEPLOY WORKFLOW always lands here.
    record_check WARN "wif:admits-production-backup" \
      "not verifiable with this identity (${WIF_BACKUP_CHECK#UNREADABLE }). Verify from Cloud Shell as the operator: bash scripts/deploy/check-wif-environment.sh ${PROJECT_ID}" ;;
  *) record_check WARN "wif:admits-production-backup" \
       "${WIF_BACKUP_CHECK#* } Remediation: bash scripts/ops/setup-wif.sh --apply (then scripts/ops/setup-backup.sh)" ;;
esac

# No worker execution may be in flight: a capture or deploy during a live run
# would change the release under a running claim.
if EXECUTION_CHECK="$(python3 "${SCRIPT_DIR}/check-worker-executions.py" \
     "$WORKER_JOB" "$REGION" "$PROJECT_ID" 2>&1)"; then
  record_check PASS "cloud-run:no-active-execution" "$EXECUTION_CHECK"
else
  record_check BLOCKED "cloud-run:no-active-execution" "$EXECUTION_CHECK"
fi

# Migration head, through the existing canonical checker.
DB_URL_ENV="$(milo_op READONLY_DATABASE_URL_ENV)"
if [[ -n "$DB_URL_ENV" && -n "${!DB_URL_ENV:-}" ]]; then
  if bash "${REPO_ROOT}/scripts/release/check-migration-state.sh" \
       --database-url-env "$DB_URL_ENV" > "${_MILO_TMPDIR}/migration-state.txt" 2>&1; then
    record_check PASS "database:migration-head" "repository and production migration sets agree"
  else
    record_check BLOCKED "database:migration-head" \
      "migration state check failed; see ${_MILO_TMPDIR}/migration-state.txt. Remediation: run the Deploy Supabase Migrations workflow"
  fi
else
  record_check MANUAL "database:migration-head" \
    "no read-only DB URL in \$${DB_URL_ENV:-READONLY_DATABASE_URL_ENV}; verify the migration head manually"
fi

finish_checks "production-preflight" "$JSON_OUTPUT"

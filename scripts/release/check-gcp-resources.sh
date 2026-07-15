#!/usr/bin/env bash
# Read-only Google Cloud resource inspection.
#
# Verifies (metadata only, never secret values):
#   - active gcloud account and selected project vs. operator expectations;
#   - required APIs;
#   - Artifact Registry repository;
#   - Cloud Run API service and worker job metadata;
#   - service accounts and identity separation;
#   - project-level IAM red flags (owner/editor/broad secret accessor).
#
# Refuses ambiguous project selection: --expected-project is mandatory for
# every remote call and must match the active gcloud configuration.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

usage() {
  cat << 'EOF'
Usage: check-gcp-resources.sh [options]

Read-only. Performs no mutation of any Google Cloud resource.

Options:
  --expected-project <id>     Exact Google Cloud project ID (required for
                              any remote inspection; ambiguous selection is
                              refused).
  --expected-account <email>  Expected active operator identity.
  --region <region>           Cloud Run / Artifact Registry region.
  --repository <name>         Artifact Registry repository name.
  --api-service <name>        Cloud Run API service name.
  --worker-job <name>         Cloud Run worker job name.
  --api-sa <email>            Expected API runtime service account.
  --worker-sa <email>         Expected worker runtime service account.
  --json-output <path>        Write a machine-readable JSON report.
  --help                      Show this help.

Without gcloud on PATH every remote check is reported as MANUAL.
EOF
}

JSON_OUTPUT=""
EXPECTED_PROJECT="" EXPECTED_ACCOUNT="" REGION="" REPOSITORY=""
API_SERVICE="" WORKER_JOB="" API_SA="" WORKER_SA=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --expected-project) EXPECTED_PROJECT="${2:?}"; shift 2 ;;
    --expected-account) EXPECTED_ACCOUNT="${2:?}"; shift 2 ;;
    --region) REGION="${2:?}"; shift 2 ;;
    --repository) REPOSITORY="${2:?}"; shift 2 ;;
    --api-service) API_SERVICE="${2:?}"; shift 2 ;;
    --worker-job) WORKER_JOB="${2:?}"; shift 2 ;;
    --api-sa) API_SA="${2:?}"; shift 2 ;;
    --worker-sa) WORKER_SA="${2:?}"; shift 2 ;;
    --json-output) JSON_OUTPUT="${2:?}"; shift 2 ;;
    --help) usage; exit 0 ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; usage >&2; exit 64 ;;
  esac
done

REQUIRED_APIS=(
  run.googleapis.com
  artifactregistry.googleapis.com
  secretmanager.googleapis.com
  iamcredentials.googleapis.com
  sts.googleapis.com
)

if [[ -n "${API_SA}" && -n "${WORKER_SA}" && "${API_SA}" == "${WORKER_SA}" ]]; then
  record_check BLOCKED "identity:shared-api-worker" "API and worker must use separate service accounts"
fi

if ! tool_available gcloud; then
  record_check MANUAL "gcloud" "gcloud CLI is unavailable; all Google Cloud resource checks must be performed manually (see docs/production-readiness/MANUAL_SERVICE_CONNECTIONS.md)"
  finish_checks "check-gcp-resources" "${JSON_OUTPUT}"
  exit $?
fi

if [[ -z "${EXPECTED_PROJECT}" ]]; then
  record_check BLOCKED "project:expected" "--expected-project is required; refusing ambiguous project selection"
  finish_checks "check-gcp-resources" "${JSON_OUTPUT}"
  exit $?
fi
require_value "project:expected-value" "${EXPECTED_PROJECT}" || {
  finish_checks "check-gcp-resources" "${JSON_OUTPUT}"; exit $?
}

active_project="$(gcloud config get-value project 2> /dev/null | tr -d '[:space:]')"
active_account="$(gcloud config get-value account 2> /dev/null | tr -d '[:space:]')"

if [[ "${active_project}" != "${EXPECTED_PROJECT}" ]]; then
  record_check BLOCKED "project:active" "active gcloud project '${active_project}' does not match --expected-project '${EXPECTED_PROJECT}'; refusing ambiguous project selection"
  finish_checks "check-gcp-resources" "${JSON_OUTPUT}"
  exit $?
fi
record_check PASS "project:active" "active gcloud project matches ${EXPECTED_PROJECT}"

if [[ -n "${EXPECTED_ACCOUNT}" ]]; then
  if [[ "${active_account}" != "${EXPECTED_ACCOUNT}" ]]; then
    record_check BLOCKED "account:active" "active gcloud account '${active_account}' does not match --expected-account"
  else
    record_check PASS "account:active" "active gcloud account matches expected operator identity"
  fi
else
  record_check WARN "account:active" "no --expected-account supplied; active account is '${active_account}'"
fi

# Required APIs.
enabled_apis="$(gcloud services list --enabled --project "${EXPECTED_PROJECT}" --format 'value(config.name)' 2> /dev/null || true)"
if [[ -z "${enabled_apis}" ]]; then
  record_check MANUAL "apis" "could not list enabled services (missing permission?); verify required APIs manually: ${REQUIRED_APIS[*]}"
else
  for api in "${REQUIRED_APIS[@]}"; do
    if grep -qx "${api}" <<< "${enabled_apis}"; then
      record_check PASS "api:${api}" "enabled"
    else
      record_check BLOCKED "api:${api}" "required API is not enabled (manual operator action; this tool never enables APIs)"
    fi
  done
fi

# Artifact Registry.
if [[ -n "${REPOSITORY}" && -n "${REGION}" ]]; then
  if gcloud artifacts repositories describe "${REPOSITORY}" --location "${REGION}" --project "${EXPECTED_PROJECT}" --format 'value(name)' > /dev/null 2>&1; then
    record_check PASS "artifact-registry:${REPOSITORY}" "repository exists in ${REGION}"
  else
    record_check BLOCKED "artifact-registry:${REPOSITORY}" "repository not found in ${REGION} (manual creation required; this tool never creates resources)"
  fi
else
  record_check MANUAL "artifact-registry" "supply --repository and --region to verify the Artifact Registry repository"
fi

milo_tmpdir_init

# Cloud Run API service metadata.
# The Knative-style v1 export (gcloud run services describe --format json)
# stores the runtime identity at spec.template.spec.serviceAccountName.
if [[ -n "${API_SERVICE}" && -n "${REGION}" ]]; then
  svc_err="${_MILO_TMPDIR}/api-service.err"
  svc_status=0
  svc_json="$(gcloud run services describe "${API_SERVICE}" --region "${REGION}" --project "${EXPECTED_PROJECT}" --format json 2> "${svc_err}")" || svc_status=$?
  if [[ "${svc_status}" -ne 0 ]]; then
    if grep -qiE 'not.?found|does not exist|cannot find' "${svc_err}"; then
      record_check WARN "cloud-run:api" "service ${API_SERVICE} not found in ${REGION}; expected before first deployment, BLOCKING afterwards"
    else
      record_check MANUAL "cloud-run:api" "could not describe API service ${API_SERVICE} (permission or API error, not a clean 'not found'); verify manually"
    fi
  elif ! json_is_valid "${svc_json}"; then
    record_check MANUAL "cloud-run:api" "API service description was not valid JSON (missing python3 parser?); verify manually"
  else
    svc_sa="$(json_field "${svc_json}" 'spec.template.spec.serviceAccountName')"
    if [[ -z "${svc_sa}" ]]; then
      record_check BLOCKED "cloud-run:api-sa-explicit" "API service ${API_SERVICE} has no explicit runtime service account; it would fall back to the default Compute Engine service account. Set --service-account <API_SERVICE_ACCOUNT_EMAIL>."
    else
      record_check PASS "cloud-run:api" "service ${API_SERVICE} exists (runtime service account: ${svc_sa})"
      if [[ -n "${API_SA}" && "${svc_sa}" != "${API_SA}" ]]; then
        record_check BLOCKED "cloud-run:api-sa" "API service runs as '${svc_sa}', expected '${API_SA}'"
      elif [[ -n "${API_SA}" ]]; then
        record_check PASS "cloud-run:api-sa" "API service runtime service account matches the expected manifest value"
      fi
    fi
    # Unauthenticated invoker must not be granted.
    api_policy="$(gcloud run services get-iam-policy "${API_SERVICE}" --region "${REGION}" --project "${EXPECTED_PROJECT}" --format json 2> /dev/null || true)"
    if [[ -n "${api_policy}" ]]; then
      if grep -q 'allUsers\|allAuthenticatedUsers' <<< "${api_policy}"; then
        record_check BLOCKED "cloud-run:api-public" "API service grants allUsers/allAuthenticatedUsers invoker; it must remain private"
      else
        record_check PASS "cloud-run:api-private" "no allUsers/allAuthenticatedUsers invoker binding on the API service"
      fi
    else
      record_check MANUAL "cloud-run:api-iam" "could not read API service IAM policy; verify manually that no allUsers invoker exists"
    fi
  fi
else
  record_check MANUAL "cloud-run:api" "supply --api-service and --region to verify the Cloud Run API service"
fi

# Cloud Run worker job metadata (never executed).
# A Cloud Run *Job* nests one more Execution level than a Service. The
# Knative-style v1 export (gcloud run jobs describe --format json) stores the
# runtime identity at spec.template.spec.template.spec.serviceAccountName
# (ExecutionTemplateSpec -> ExecutionSpec -> TaskTemplateSpec -> TaskSpec).
# The previous spec.template.template.spec.serviceAccountName path was wrong
# (it dropped the ExecutionSpec level) and always resolved to empty, which was
# then misreported as "job not found".
if [[ -n "${WORKER_JOB}" && -n "${REGION}" ]]; then
  job_err="${_MILO_TMPDIR}/worker-job.err"
  job_status=0
  job_json="$(gcloud run jobs describe "${WORKER_JOB}" --region "${REGION}" --project "${EXPECTED_PROJECT}" --format json 2> "${job_err}")" || job_status=$?
  if [[ "${job_status}" -ne 0 ]]; then
    if grep -qiE 'not.?found|does not exist|cannot find' "${job_err}"; then
      record_check WARN "cloud-run:worker-job" "job ${WORKER_JOB} not found in ${REGION}; expected before first deployment, BLOCKING afterwards"
    else
      record_check MANUAL "cloud-run:worker-job" "could not describe worker job ${WORKER_JOB} (permission or API error, not a clean 'not found'); verify manually"
    fi
  elif ! json_is_valid "${job_json}"; then
    record_check MANUAL "cloud-run:worker-job" "worker job description was not valid JSON (missing python3 parser?); verify manually"
  else
    record_check PASS "cloud-run:worker-job" "job ${WORKER_JOB} exists; this tool never executes it"
    job_sa="$(json_field "${job_json}" 'spec.template.spec.template.spec.serviceAccountName')"
    if [[ -z "${job_sa}" ]]; then
      # Exists but no explicit SA: blocking. The worker must never silently
      # inherit the default Compute Engine service account.
      record_check BLOCKED "cloud-run:worker-sa-explicit" "worker job ${WORKER_JOB} exists but has NO explicit service account; it would silently run as the default Compute Engine service account. Set --service-account <WORKER_SERVICE_ACCOUNT_EMAIL> on the job."
    else
      record_check PASS "cloud-run:worker-sa-explicit" "worker job has an explicit runtime service account: ${job_sa}"
      if [[ -n "${WORKER_SA}" && "${job_sa}" != "${WORKER_SA}" ]]; then
        record_check BLOCKED "cloud-run:worker-sa" "worker job runs as '${job_sa}', expected '${WORKER_SA}'"
      elif [[ -n "${WORKER_SA}" ]]; then
        record_check PASS "cloud-run:worker-sa" "worker job runtime service account matches the expected manifest value"
      fi
      if [[ -n "${API_SA}" && "${job_sa}" == "${API_SA}" ]]; then
        record_check BLOCKED "cloud-run:shared-identity" "worker job shares the API runtime service account; identities must be separate"
      fi
    fi
  fi
else
  record_check MANUAL "cloud-run:worker-job" "supply --worker-job and --region to verify the Cloud Run worker job"
fi

# Service accounts.
for sa in "${API_SA}" "${WORKER_SA}"; do
  [[ -z "${sa}" ]] && continue
  if gcloud iam service-accounts describe "${sa}" --project "${EXPECTED_PROJECT}" --format 'value(email)' > /dev/null 2>&1; then
    record_check PASS "service-account:${sa}" "exists"
  else
    record_check BLOCKED "service-account:${sa}" "service account not found (manual creation required; this tool never creates identities)"
  fi
done

# Project-level IAM red flags.
policy="$(gcloud projects get-iam-policy "${EXPECTED_PROJECT}" --format json 2> /dev/null || true)"
if [[ -z "${policy}" ]]; then
  record_check MANUAL "iam:project-policy" "could not read project IAM policy; verify manually that no owner/editor or project-wide secretAccessor grants exist for runtime identities"
else
  for role in roles/owner roles/editor; do
    if grep -q "\"${role}\"" <<< "${policy}"; then
      record_check WARN "iam:${role}" "project has ${role} bindings; runtime and CI identities must never hold owner/editor"
    fi
  done
  if grep -q '"roles/secretmanager.secretAccessor"' <<< "${policy}"; then
    record_check BLOCKED "iam:broad-secret-accessor" "project-wide Secret Manager accessor grant found; secret access must be granted per-secret only"
  else
    record_check PASS "iam:no-broad-secret-accessor" "no project-wide Secret Manager accessor grant"
  fi
  if grep -q '"allUsers"\|"allAuthenticatedUsers"' <<< "${policy}"; then
    record_check BLOCKED "iam:wildcard-principal" "project IAM policy contains a wildcard principal"
  else
    record_check PASS "iam:no-wildcard-principal" "no wildcard principal in project IAM policy"
  fi
fi

finish_checks "check-gcp-resources" "${JSON_OUTPUT}"

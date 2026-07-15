#!/usr/bin/env bash
# Read-only Secret Manager metadata inspection.
#
# Verifies that expected secret NAMES exist and that access is granted at
# the secret level to the correct single consumer. Never reads, prints, or
# creates a secret value.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

usage() {
  cat << 'EOF'
Usage: check-secret-metadata.sh [options]

Read-only. Never accesses secret VALUES — names, versions and IAM only.

Options:
  --expected-project <id>   Exact Google Cloud project ID (required for
                            remote inspection).
  --secret <name=consumer>  Expected secret and its sole consuming service
                            account (repeatable), e.g.
                            --secret milo-supabase-service-key=api-sa@p.iam.gserviceaccount.com
  --json-output <path>      Write a machine-readable JSON report.
  --help                    Show this help.
EOF
}

JSON_OUTPUT="" EXPECTED_PROJECT=""
SECRET_SPECS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --expected-project) EXPECTED_PROJECT="${2:?}"; shift 2 ;;
    --secret) SECRET_SPECS+=("${2:?}"); shift 2 ;;
    --json-output) JSON_OUTPUT="${2:?}"; shift 2 ;;
    --help) usage; exit 0 ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; usage >&2; exit 64 ;;
  esac
done

if ! tool_available gcloud; then
  record_check MANUAL "gcloud" "gcloud CLI unavailable; verify secret names and per-secret IAM manually (never read secret values)"
  finish_checks "check-secret-metadata" "${JSON_OUTPUT}"
  exit $?
fi

if [[ -z "${EXPECTED_PROJECT}" ]]; then
  record_check BLOCKED "project:expected" "--expected-project is required; refusing ambiguous project selection"
  finish_checks "check-secret-metadata" "${JSON_OUTPUT}"
  exit $?
fi
require_value "project:expected-value" "${EXPECTED_PROJECT}" || {
  finish_checks "check-secret-metadata" "${JSON_OUTPUT}"; exit $?
}
active_project="$(gcloud config get-value project 2> /dev/null | tr -d '[:space:]')"
if [[ "${active_project}" != "${EXPECTED_PROJECT}" ]]; then
  record_check BLOCKED "project:active" "active gcloud project '${active_project}' does not match --expected-project"
  finish_checks "check-secret-metadata" "${JSON_OUTPUT}"
  exit $?
fi

existing="$(gcloud secrets list --project "${EXPECTED_PROJECT}" --format 'value(name)' 2> /dev/null || true)"
if [[ -z "${existing}" ]]; then
  record_check MANUAL "secrets:list" "could not list secrets (missing permission or none exist); verify names manually"
fi

if [[ "${#SECRET_SPECS[@]}" -eq 0 ]]; then
  record_check MANUAL "secrets:expected" "no --secret expectations supplied; provide name=consumer pairs to verify per-secret access"
fi

for spec in "${SECRET_SPECS[@]}"; do
  name="${spec%%=*}"
  consumer="${spec#*=}"
  if [[ -z "${name}" || -z "${consumer}" || "${name}" == "${spec}" ]]; then
    record_check BLOCKED "secret:spec" "malformed --secret specification (expected name=consumer): ${spec}"
    continue
  fi
  require_value "secret:name:${name}" "${name}" || continue
  require_value "secret:consumer:${name}" "${consumer}" || continue
  if [[ -n "${existing}" ]] && ! grep -q "secrets/${name}$\|^${name}$" <<< "${existing}"; then
    record_check BLOCKED "secret:${name}" "secret name not found in project (manual creation required; this tool never creates secrets)"
    continue
  fi
  record_check PASS "secret:${name}" "secret exists (value never read)"
  policy="$(gcloud secrets get-iam-policy "${name}" --project "${EXPECTED_PROJECT}" --format json 2> /dev/null || true)"
  if [[ -z "${policy}" ]]; then
    record_check MANUAL "secret:${name}:iam" "could not read secret IAM policy; verify manually that only ${consumer} holds roles/secretmanager.secretAccessor"
    continue
  fi
  if grep -q "serviceAccount:${consumer}" <<< "${policy}"; then
    record_check PASS "secret:${name}:consumer" "expected consumer ${consumer} is bound at secret level"
  else
    record_check WARN "secret:${name}:consumer" "expected consumer ${consumer} is not yet bound (manual grant required at SECRET level, never project-wide)"
  fi
  if grep -q '"allUsers"\|"allAuthenticatedUsers"' <<< "${policy}"; then
    record_check BLOCKED "secret:${name}:wildcard" "secret IAM policy contains a wildcard principal"
  fi
  # Any accessor other than the declared consumer is a finding.
  extra="$(grep -o 'serviceAccount:[^"]*' <<< "${policy}" | sort -u | grep -v "serviceAccount:${consumer}" || true)"
  if [[ -n "${extra}" ]]; then
    record_check WARN "secret:${name}:extra-accessors" "additional identities bound on this secret: ${extra//$'\n'/, } — confirm each is intentional"
  fi
done

finish_checks "check-secret-metadata" "${JSON_OUTPUT}"

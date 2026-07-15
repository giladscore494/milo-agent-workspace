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
  --secret <name=consumer[,consumer]>
                            Expected secret and its intended consuming service
                            account(s), comma-separated (repeatable), e.g.
                            --secret milo-supabase-key=api-sa@p.iam.gserviceaccount.com,worker-sa@p.iam.gserviceaccount.com
                            Every listed consumer must be bound at the secret
                            level; any other accessor is flagged.
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
  # No expectations supplied means NO Secret Manager verification is possible.
  # Never let this be mistaken for a passing audit.
  record_check MANUAL "secrets:expected" "no --secret expectations supplied; provide name=consumer[,consumer] pairs (typically derived from a completed manifest) to verify per-secret access. No Secret Manager verification was performed."
fi

# Project-wide Secret Manager accessor is a red flag regardless of any
# individual secret: access must be granted per-secret only.
project_policy="$(gcloud projects get-iam-policy "${EXPECTED_PROJECT}" --format json 2> /dev/null || true)"
if [[ -n "${project_policy}" ]]; then
  if grep -q '"roles/secretmanager.secretAccessor"' <<< "${project_policy}"; then
    record_check BLOCKED "secrets:project-wide-accessor" "project-wide roles/secretmanager.secretAccessor grant found; Secret Manager access must be granted per-secret only, never project-wide"
  else
    record_check PASS "secrets:project-wide-accessor" "no project-wide Secret Manager accessor grant"
  fi
fi

for spec in "${SECRET_SPECS[@]}"; do
  name="${spec%%=*}"
  consumers_csv="${spec#*=}"
  if [[ -z "${name}" || -z "${consumers_csv}" || "${name}" == "${spec}" ]]; then
    record_check BLOCKED "secret:spec" "malformed --secret specification (expected name=consumer[,consumer]): ${spec}"
    continue
  fi
  require_value "secret:name:${name}" "${name}" || continue
  # Split and validate the intended consumer list.
  IFS=',' read -r -a consumers <<< "${consumers_csv}"
  consumer_bad=0
  for consumer in "${consumers[@]}"; do
    require_value "secret:consumer:${name}" "${consumer}" || consumer_bad=1
  done
  [[ "${consumer_bad}" -eq 1 ]] && continue

  if [[ -z "${existing}" ]]; then
    # Could not list secrets: existence is UNCONFIRMED. Never claim a PASS
    # here — that would be a false "resource present" result.
    record_check MANUAL "secret:${name}" "could not list secrets to confirm '${name}' exists; verify existence manually (value never read)"
  elif ! grep -q "secrets/${name}$\|^${name}$" <<< "${existing}"; then
    record_check BLOCKED "secret:${name}" "secret name not found in project (manual creation required; this tool never creates secrets)"
    continue
  else
    record_check PASS "secret:${name}" "secret exists (value never read)"
  fi

  # Enabled versions must exist (payloads are never accessed).
  enabled_versions="$(gcloud secrets versions list "${name}" --project "${EXPECTED_PROJECT}" --filter 'state=enabled' --format 'value(name)' 2> /dev/null || true)"
  if [[ -z "${enabled_versions}" ]]; then
    record_check BLOCKED "secret:${name}:version" "secret has no enabled version (a consumer cannot resolve :latest); add an enabled version (payload never inspected here)"
  else
    record_check PASS "secret:${name}:version" "at least one enabled version exists (payload never read)"
  fi

  policy="$(gcloud secrets get-iam-policy "${name}" --project "${EXPECTED_PROJECT}" --format json 2> /dev/null || true)"
  if [[ -z "${policy}" ]]; then
    record_check MANUAL "secret:${name}:iam" "could not read secret IAM policy; verify manually that only the intended consumers hold roles/secretmanager.secretAccessor"
    continue
  fi
  if grep -q '"allUsers"\|"allAuthenticatedUsers"' <<< "${policy}"; then
    record_check BLOCKED "secret:${name}:wildcard" "secret IAM policy contains a wildcard principal (allUsers/allAuthenticatedUsers)"
  fi
  # Every intended consumer must be bound at the secret level.
  for consumer in "${consumers[@]}"; do
    if grep -q "serviceAccount:${consumer}" <<< "${policy}"; then
      record_check PASS "secret:${name}:consumer:${consumer}" "intended consumer ${consumer} is bound at secret level"
    else
      record_check BLOCKED "secret:${name}:consumer:${consumer}" "intended consumer ${consumer} is NOT bound at secret level (grant roles/secretmanager.secretAccessor at the SECRET level, never project-wide)"
    fi
  done
  # Any service-account accessor NOT in the intended set is unexpected.
  bound="$(grep -o 'serviceAccount:[^"]*' <<< "${policy}" | sort -u || true)"
  if [[ -n "${bound}" ]]; then
    while IFS= read -r principal; do
      [[ -z "${principal}" ]] && continue
      sa="${principal#serviceAccount:}"
      is_expected=0
      for consumer in "${consumers[@]}"; do
        [[ "${sa}" == "${consumer}" ]] && is_expected=1 && break
      done
      if [[ "${is_expected}" -eq 0 ]]; then
        record_check WARN "secret:${name}:extra-accessor" "accessor ${sa} is bound on secret ${name} but is not an intended consumer — confirm it is intentional or remove it"
      fi
    done <<< "${bound}"
  fi
done

finish_checks "check-secret-metadata" "${JSON_OUTPUT}"

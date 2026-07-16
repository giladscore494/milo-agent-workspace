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

milo_tmpdir_init

# List secrets. A permission/API failure must NOT be conflated with "no
# secrets exist" — capture the exit status and stderr separately.
list_err="${_MILO_TMPDIR}/secrets-list.err"
list_status=0
existing_readable=1
existing="$(gcloud secrets list --project "${EXPECTED_PROJECT}" --format 'value(name)' 2> "${list_err}")" || list_status=$?
if [[ "${list_status}" -ne 0 ]]; then
  existing_readable=0
  if grep -qiE 'permission|forbidden|denied|not authorized|unauthenticated|has not been used|is disabled|SERVICE_DISABLED' "${list_err}"; then
    record_check MANUAL "secrets:list" "could not list secrets (permission/API error, NOT 'no secrets'); verify names manually (value never read)"
  else
    record_check MANUAL "secrets:list" "could not list secrets (inspection error); verify names manually (value never read)"
  fi
fi

if [[ "${#SECRET_SPECS[@]}" -eq 0 ]]; then
  # No expectations supplied means NO Secret Manager verification is possible.
  # Never let this be mistaken for a passing audit.
  record_check MANUAL "secrets:expected" "no --secret expectations supplied; provide name=consumer[,consumer] pairs (typically derived from a completed manifest) to verify per-secret access. No Secret Manager verification was performed."
fi

# Project-wide Secret Manager accessor is a red flag regardless of any
# individual secret: access must be granted per-secret only. Parse the policy
# structurally and only consider the accessor role; an unreadable policy is an
# explicit MANUAL, never a silent skip.
proj_err="${_MILO_TMPDIR}/project-iam.err"
proj_status=0
project_policy="$(gcloud projects get-iam-policy "${EXPECTED_PROJECT}" --format json 2> "${proj_err}")" || proj_status=$?
if [[ "${proj_status}" -ne 0 ]]; then
  record_check MANUAL "secrets:project-wide-accessor" "could not read project IAM policy (permission/API error); manually verify no project-wide roles/secretmanager.secretAccessor grant exists"
elif ! json_is_valid "${project_policy}"; then
  record_check MANUAL "secrets:project-wide-accessor" "project IAM policy was not valid JSON; verify manually that no project-wide roles/secretmanager.secretAccessor grant exists"
else
  proj_accessors="$(iam_role_members "${project_policy}" "roles/secretmanager.secretAccessor")"
  if [[ -n "${proj_accessors}" ]]; then
    record_check BLOCKED "secrets:project-wide-accessor" "project-wide roles/secretmanager.secretAccessor grant found (members: ${proj_accessors//$'\n'/, }); Secret Manager access must be granted per-secret only, never project-wide"
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

  if [[ "${existing_readable}" -eq 0 ]]; then
    # Could not list secrets (permission/API error): existence is UNCONFIRMED.
    # Never claim a PASS or a "missing" here.
    record_check MANUAL "secret:${name}" "could not confirm '${name}' exists because the secrets list was unreadable (permission/API error); verify existence manually (value never read)"
  elif grep -q "secrets/${name}$\|^${name}$" <<< "${existing}"; then
    record_check PASS "secret:${name}" "secret exists (value never read)"
  else
    # The list succeeded and the name is genuinely absent.
    record_check BLOCKED "secret:${name}" "secret name not found in project (manual creation required; this tool never creates secrets)"
    continue
  fi

  # Enabled versions must exist. Distinguish "command succeeded, zero enabled
  # versions" (BLOCKED) from "command failed" (MANUAL) — never claim "no
  # enabled version" from a failed command.
  ver_err="${_MILO_TMPDIR}/secret-versions.err"
  ver_status=0
  enabled_versions="$(gcloud secrets versions list "${name}" --project "${EXPECTED_PROJECT}" --filter 'state=enabled' --format 'value(name)' 2> "${ver_err}")" || ver_status=$?
  if [[ "${ver_status}" -ne 0 ]]; then
    if grep -qiE 'not.?found|does not exist' "${ver_err}"; then
      record_check BLOCKED "secret:${name}:version" "secret not found when listing versions (manual creation required)"
    else
      record_check MANUAL "secret:${name}:version" "could not list secret versions (permission/API error, not a clean 'not found'); verify manually that an enabled version exists (payload never read)"
    fi
  elif [[ -z "${enabled_versions}" ]]; then
    record_check BLOCKED "secret:${name}:version" "secret has no enabled version (a consumer cannot resolve :latest); add an enabled version (payload never inspected here)"
  else
    record_check PASS "secret:${name}:version" "at least one enabled version exists (payload never read)"
  fi

  # Per-secret IAM. Consumer validation requires a successfully read AND parsed
  # policy — never PASS/BLOCK a consumer from an unreadable or malformed policy.
  iam_err="${_MILO_TMPDIR}/secret-iam.err"
  iam_status=0
  policy="$(gcloud secrets get-iam-policy "${name}" --project "${EXPECTED_PROJECT}" --format json 2> "${iam_err}")" || iam_status=$?
  if [[ "${iam_status}" -ne 0 ]]; then
    record_check MANUAL "secret:${name}:iam" "could not read secret IAM policy (permission/API error); cannot validate consumers (never assumed present or absent)"
    continue
  fi
  if ! json_is_valid "${policy}"; then
    record_check MANUAL "secret:${name}:iam" "secret IAM policy was not valid JSON; cannot validate consumers (never PASS without a parsed policy)"
    continue
  fi

  # Consider ONLY members of the exact secret-accessor role. A service account
  # that appears only under viewer/admin/metadata roles must not satisfy — or
  # pollute — accessor validation.
  accessor_members="$(iam_role_members "${policy}" "roles/secretmanager.secretAccessor")"

  # Wildcard principal in the accessor binding.
  if grep -qxE 'allUsers|allAuthenticatedUsers' <<< "${accessor_members}"; then
    record_check BLOCKED "secret:${name}:wildcard" "secret grants roles/secretmanager.secretAccessor to a wildcard principal (allUsers/allAuthenticatedUsers)"
  fi

  # Every intended consumer must hold the accessor role (exact member match).
  for consumer in "${consumers[@]}"; do
    if grep -qxF "serviceAccount:${consumer}" <<< "${accessor_members}"; then
      record_check PASS "secret:${name}:consumer:${consumer}" "intended consumer ${consumer} holds roles/secretmanager.secretAccessor at the secret level"
    else
      record_check BLOCKED "secret:${name}:consumer:${consumer}" "intended consumer ${consumer} does NOT hold roles/secretmanager.secretAccessor on this secret (a binding under any other role does not count)"
    fi
  done

  # Any service-account holding the accessor role but NOT in the intended set
  # is an unexpected accessor. Members under other roles are ignored here.
  while IFS= read -r member; do
    [[ -z "${member}" ]] && continue
    case "${member}" in
      serviceAccount:*) sa="${member#serviceAccount:}" ;;
      *) continue ;;
    esac
    is_expected=0
    for consumer in "${consumers[@]}"; do
      [[ "${sa}" == "${consumer}" ]] && is_expected=1 && break
    done
    if [[ "${is_expected}" -eq 0 ]]; then
      record_check WARN "secret:${name}:extra-accessor" "accessor ${sa} holds roles/secretmanager.secretAccessor on secret ${name} but is not an intended consumer — confirm it is intentional or remove it"
    fi
  done <<< "${accessor_members}"
done

finish_checks "check-secret-metadata" "${JSON_OUTPUT}"

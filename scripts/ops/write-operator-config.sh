#!/usr/bin/env bash
# Write the operator identifiers (NON-secret: project, region, resource names,
# service-account emails, Secret Manager resource NAMES) from the repository
# variable MILO_OPERATOR_CONFIG to a file outside the checkout, validated line
# by line exactly as scripts/deploy/operator-config.sh reads it, and export
# MILO_OPERATOR_CONFIG to the later workflow steps. Prints no value.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
# shellcheck source=../deploy/operator-config.sh
source "${REPO_ROOT}/scripts/deploy/operator-config.sh"

text="${MILO_OPERATOR_CONFIG_TEXT:-}"
[[ -n "$text" ]] || { printf 'FAIL: the repository variable MILO_OPERATOR_CONFIG is empty\n' >&2; exit 2; }
target="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/production-operator.env"
printf '%s\n' "$text" > "$target"
chmod 600 "$target"
milo_load_operator_config "$target" > /dev/null || exit 2
milo_require_op GCP_PROJECT_ID GCP_REGION CLOUD_RUN_API_SERVICE CLOUD_RUN_WORKER_JOB \
  READONLY_DATABASE_URL_ENV || exit 2
# The workflows bind the read-only database URL under exactly this name.
if [[ "$(milo_op READONLY_DATABASE_URL_ENV)" != "MILO_READONLY_DB_URL" ]]; then
  printf 'FAIL: READONLY_DATABASE_URL_ENV must be MILO_READONLY_DB_URL (the workflows bind that secret name)\n' >&2
  exit 2
fi
# Secret VALUES never belong here: refuse anything that looks like one.
if grep -qiE '(postgres(ql)?://|password|sb_secret|service_role|BEGIN [A-Z ]*PRIVATE KEY)' "$target"; then
  printf 'FAIL: MILO_OPERATOR_CONFIG must hold identifiers only, never a secret value\n' >&2
  rm -f "$target"
  exit 2
fi
if [[ -n "${GITHUB_ENV:-}" ]]; then
  printf 'MILO_OPERATOR_CONFIG=%s\n' "$target" >> "$GITHUB_ENV"
fi
printf 'Operator configuration written (%s keys).\n' "$(grep -cE '^[A-Za-z_][A-Za-z0-9_]*=' "$target")"

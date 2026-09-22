#!/usr/bin/env bash
# shellcheck disable=SC2034  # values here are consumed by the sourcing tool
# Shared operator configuration loader for the production activation bundle.
#
# This file is SOURCED, never executed. It reads the operator's non-secret
# identifier file (config/production-operator.env by default), validates that
# the entries a given tool needs are present, and exposes them as shell
# variables. It contacts nothing and holds no secret value.
#
# The point is ergonomics with a hard edge: an operator fills one file in
# once, every tool in the bundle reads the same file, and a tool that needs an
# identifier the file does not carry says exactly which key is missing instead
# of falling back to a default that would aim it at the wrong project.

MILO_OPERATOR_CONFIG_DEFAULT="config/production-operator.env"

# milo_operator_config_path REPO_ROOT [EXPLICIT_PATH]
# Resolution order: explicit argument, MILO_OPERATOR_CONFIG, repo default.
milo_operator_config_path() {
  local repo_root="$1" explicit="${2:-}"
  if [[ -n "$explicit" ]]; then
    printf '%s' "$explicit"
  elif [[ -n "${MILO_OPERATOR_CONFIG:-}" ]]; then
    printf '%s' "$MILO_OPERATOR_CONFIG"
  else
    printf '%s' "$repo_root/$MILO_OPERATOR_CONFIG_DEFAULT"
  fi
}

# milo_load_operator_config PATH
# Reads NAME=VALUE lines into MILO_OP_<NAME>. Rejects malformed lines rather
# than skipping them: a typo that silently produced an empty identifier would
# aim a deployment somewhere nobody chose.
milo_load_operator_config() {
  local path="$1" line name value
  if [[ ! -f "$path" ]]; then
    printf 'FAIL: operator configuration not found: %s\n' "$path" >&2
    printf '      Create it with: cp %s %s\n' \
      "config/production-operator.env.example" "$MILO_OPERATOR_CONFIG_DEFAULT" >&2
    return 1
  fi
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ -z "$line" || "$line" == \#* ]] && continue
    if [[ ! "$line" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]]; then
      printf 'FAIL: malformed line in %s (expected NAME=VALUE): %s\n' "$path" "$line" >&2
      return 1
    fi
    name="${line%%=*}"
    value="${line#*=}"
    printf -v "MILO_OP_${name}" '%s' "$value"
  done < "$path"
  return 0
}

# milo_op NAME — echo the loaded value ('' when unset or empty).
milo_op() {
  local var="MILO_OP_$1"
  printf '%s' "${!var:-}"
}

# milo_require_op NAME... — fail with the exact missing keys, all at once.
# Reporting them one per run would make an operator re-run the tool five
# times to learn five things it already knew on the first pass.
milo_require_op() {
  local name missing=()
  for name in "$@"; do
    [[ -n "$(milo_op "$name")" ]] || missing+=("$name")
  done
  if [[ ${#missing[@]} -gt 0 ]]; then
    printf 'FAIL: operator configuration is incomplete. Missing value(s):\n' >&2
    for name in "${missing[@]}"; do
      printf '  - %s\n' "$name" >&2
    done
    printf 'Remediation: set them in %s\n' \
      "$(milo_operator_config_path "${MILO_REPO_ROOT:-.}" "${MILO_OPERATOR_CONFIG_PATH:-}")" >&2
    return 1
  fi
  return 0
}

# milo_require_gcloud_context EXPECTED_PROJECT
# Proves the ambient gcloud really is authenticated and really is pointed at
# the expected project. Both are checked because either one being wrong sends
# every later command at the wrong target, and the failure would otherwise
# surface as a confusing per-resource "not found".
milo_require_gcloud_context() {
  local expected="$1" account active
  if ! command -v gcloud > /dev/null 2>&1; then
    printf 'FAIL: gcloud is not installed or not on PATH\n' >&2
    printf '      Remediation: install the Google Cloud CLI, then `gcloud auth login`\n' >&2
    return 1
  fi
  account="$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2> /dev/null || true)"
  if [[ -z "$account" ]]; then
    printf 'FAIL: no active gcloud account\n' >&2
    printf '      Remediation: gcloud auth login\n' >&2
    return 1
  fi
  active="$(gcloud config get-value project 2> /dev/null || true)"
  if [[ "$active" != "$expected" ]]; then
    printf 'FAIL: gcloud project is %s, expected %s\n' "${active:-<unset>}" "$expected" >&2
    printf '      Remediation: gcloud config set project %s\n' "$expected" >&2
    return 1
  fi
  printf 'gcloud account: %s\n' "$account"
  printf 'gcloud project: %s\n' "$active"
  return 0
}

#!/usr/bin/env bash
# Shared helpers for MILO release/operator tooling.
#
# Every consumer sources this file and inherits:
#   - strict mode (set -euo pipefail);
#   - structured check recording (PASS/WARN/BLOCKED/MANUAL/NOT_APPLICABLE);
#   - JSON report generation (--json-output support);
#   - credential redaction for URLs and known secret variable names;
#   - placeholder/wildcard rejection;
#   - the protected apply-mode guard.
#
# Read-only by contract: nothing in this library mutates external services.
# Apply-mode helpers only VALIDATE preconditions; the calling script owns
# any mutation and must pass every guard first.

set -euo pipefail

MILO_RELEASE_LIB_VERSION="1.0.0"
MILO_OPERATOR_ACK_EXPECTED="I_UNDERSTAND_THIS_CHANGES_PRODUCTION"

# ---------------------------------------------------------------------------
# check recording
# ---------------------------------------------------------------------------
_MILO_CHECK_NAMES=()
_MILO_CHECK_STATUSES=()
_MILO_CHECK_DETAILS=()
_MILO_BLOCKED_COUNT=0
_MILO_MANUAL_COUNT=0
_MILO_WARN_COUNT=0
_MILO_PASS_COUNT=0
_MILO_NA_COUNT=0

# record_check STATUS NAME DETAIL
record_check() {
  local status="$1" name="$2" detail="${3:-}"
  case "${status}" in
    PASS) _MILO_PASS_COUNT=$((_MILO_PASS_COUNT + 1)) ;;
    WARN) _MILO_WARN_COUNT=$((_MILO_WARN_COUNT + 1)) ;;
    BLOCKED) _MILO_BLOCKED_COUNT=$((_MILO_BLOCKED_COUNT + 1)) ;;
    MANUAL) _MILO_MANUAL_COUNT=$((_MILO_MANUAL_COUNT + 1)) ;;
    NOT_APPLICABLE) _MILO_NA_COUNT=$((_MILO_NA_COUNT + 1)) ;;
    *)
      printf 'internal error: unknown check status %s\n' "${status}" >&2
      exit 70
      ;;
  esac
  _MILO_CHECK_NAMES+=("${name}")
  _MILO_CHECK_STATUSES+=("${status}")
  _MILO_CHECK_DETAILS+=("${detail}")
  printf '[%s] %s' "${status}" "${name}"
  if [[ -n "${detail}" ]]; then
    printf ' — %s' "$(redact_line "${detail}")"
  fi
  printf '\n'
}

json_escape() {
  # Minimal JSON string escaping without external dependencies.
  local s="$1"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  s="${s//$'\n'/\\n}"
  s="${s//$'\r'/\\r}"
  s="${s//$'\t'/\\t}"
  printf '%s' "${s}"
}

# write_json_report PATH SCRIPT_NAME
write_json_report() {
  local path="$1" script_name="$2"
  local tmp
  tmp="$(mktemp "${TMPDIR:-/tmp}/milo-report.XXXXXX")"
  # shellcheck disable=SC2064
  trap "rm -f '${tmp}'" RETURN
  chmod 600 "${tmp}"
  {
    printf '{\n'
    printf '  "script": "%s",\n' "$(json_escape "${script_name}")"
    printf '  "library_version": "%s",\n' "${MILO_RELEASE_LIB_VERSION}"
    printf '  "mode": "%s",\n' "${MILO_MODE:-check}"
    printf '  "summary": {"pass": %d, "warn": %d, "blocked": %d, "manual": %d, "not_applicable": %d},\n' \
      "${_MILO_PASS_COUNT}" "${_MILO_WARN_COUNT}" "${_MILO_BLOCKED_COUNT}" "${_MILO_MANUAL_COUNT}" "${_MILO_NA_COUNT}"
    printf '  "checks": [\n'
    local i last=$(( ${#_MILO_CHECK_NAMES[@]} - 1 ))
    for i in "${!_MILO_CHECK_NAMES[@]}"; do
      printf '    {"status": "%s", "name": "%s", "detail": "%s"}' \
        "${_MILO_CHECK_STATUSES[${i}]}" \
        "$(json_escape "${_MILO_CHECK_NAMES[${i}]}")" \
        "$(json_escape "$(redact_line "${_MILO_CHECK_DETAILS[${i}]}")")"
      if [[ "${i}" -lt "${last}" ]]; then printf ','; fi
      printf '\n'
    done
    printf '  ]\n'
    printf '}\n'
  } > "${tmp}"
  mv "${tmp}" "${path}"
  trap - RETURN
  printf 'JSON report written to %s\n' "${path}"
}

# finish_checks SCRIPT_NAME [JSON_OUTPUT_PATH]
# Prints the summary, optionally writes the JSON report, and exits nonzero
# when any required check is BLOCKED.
finish_checks() {
  local script_name="$1" json_output="${2:-}"
  printf '\nSummary: %d PASS, %d WARN, %d BLOCKED, %d MANUAL, %d NOT_APPLICABLE\n' \
    "${_MILO_PASS_COUNT}" "${_MILO_WARN_COUNT}" "${_MILO_BLOCKED_COUNT}" "${_MILO_MANUAL_COUNT}" "${_MILO_NA_COUNT}"
  if [[ -n "${json_output}" ]]; then
    write_json_report "${json_output}" "${script_name}"
  fi
  if [[ "${_MILO_BLOCKED_COUNT}" -gt 0 ]]; then
    printf 'RESULT: BLOCKED (%d blocking finding(s))\n' "${_MILO_BLOCKED_COUNT}"
    return 1
  fi
  printf 'RESULT: OK (no blocking findings)\n'
  return 0
}

# ---------------------------------------------------------------------------
# redaction
# ---------------------------------------------------------------------------
# redact_line TEXT — remove credentials embedded in URLs and values of
# secret-looking KEY=VALUE pairs. Never rely on this as the only barrier:
# scripts must not put secret values into detail strings in the first place.
redact_line() {
  local text="${1:-}"
  printf '%s' "${text}" | sed -E \
    -e 's#(://)[^/@[:space:]]+(:[^/@[:space:]]*)?@#\1[REDACTED]@#g' \
    -e 's#([A-Za-z_]*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)[A-Za-z_]*[[:space:]]*[=:][[:space:]]*)[^[:space:]]+#\1[REDACTED]#Ig' \
    -e 's#([Bb]earer[[:space:]]+)[A-Za-z0-9._-]+#\1[REDACTED]#g'
}

# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------
is_placeholder() {
  local value="${1:-}"
  case "${value}" in
    ''|\<*\>|*changeme*|*CHANGEME*|*placeholder*|*PLACEHOLDER*|*example.com*|*your-project*|*TODO*|*FIXME*)
      return 0 ;;
  esac
  if [[ "${value}" == "00000000-0000-0000-0000-000000000000" ]]; then
    return 0
  fi
  return 1
}

is_wildcard() {
  local value="${1:-}"
  [[ "${value}" == "*" || "${value}" == *'*'* ]]
}

# require_value NAME VALUE — BLOCKED and nonzero return on empty/placeholder/wildcard.
require_value() {
  local name="$1" value="${2:-}"
  if [[ -z "${value}" ]]; then
    record_check BLOCKED "${name}" "required value is empty"
    return 1
  fi
  if is_placeholder "${value}"; then
    record_check BLOCKED "${name}" "placeholder value rejected: ${value}"
    return 1
  fi
  if is_wildcard "${value}"; then
    record_check BLOCKED "${name}" "wildcard value rejected"
    return 1
  fi
  return 0
}

is_uuid() {
  [[ "${1:-}" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]
}

is_full_sha() {
  [[ "${1:-}" =~ ^[0-9a-f]{40}$ ]]
}

# tool_available NAME — success when the command exists on PATH.
tool_available() {
  command -v "$1" > /dev/null 2>&1
}

# ---------------------------------------------------------------------------
# structured JSON extraction
# ---------------------------------------------------------------------------
# json_field JSON DOTTED_PATH — extract a scalar/subtree from JSON using a
# controlled Python parser (never fragile CLI-table string parsing).
#   - dotted path segments traverse object keys; numeric segments index arrays;
#   - a null or absent value prints the empty string and returns 0;
#   - a scalar prints its string form; an object/array prints compact JSON;
#   - invalid JSON returns 3; no parser available returns 2.
# The caller distinguishes "field is empty/absent" (return 0, empty output)
# from "input was not valid JSON" (return 3) — the two must never be conflated.
json_field() {
  local json="$1" path="$2"
  if ! tool_available python3; then
    return 2
  fi
  JSON_FIELD_PATH="${path}" python3 - "$json" << 'PY'
import json
import os
import sys

raw = sys.argv[1]
try:
    obj = json.loads(raw)
except Exception:
    sys.exit(3)

cur = obj
for part in os.environ["JSON_FIELD_PATH"].split("."):
    if part == "":
        continue
    if isinstance(cur, list):
        try:
            idx = int(part)
        except ValueError:
            print("", end="")
            sys.exit(0)
        if 0 <= idx < len(cur):
            cur = cur[idx]
        else:
            print("", end="")
            sys.exit(0)
    elif isinstance(cur, dict) and part in cur:
        cur = cur[part]
    else:
        print("", end="")
        sys.exit(0)

if cur is None:
    print("", end="")
elif isinstance(cur, (dict, list)):
    print(json.dumps(cur, separators=(",", ":")), end="")
elif isinstance(cur, bool):
    print("true" if cur else "false", end="")
else:
    print(cur, end="")
PY
}

# json_is_valid JSON — success only when the argument parses as JSON.
json_is_valid() {
  local json="$1"
  if ! tool_available python3; then
    return 2
  fi
  printf '%s' "${json}" | python3 -c 'import json,sys; json.load(sys.stdin)' > /dev/null 2>&1
}

# require_tool NAME PURPOSE — records MANUAL when the tool is unavailable.
require_tool() {
  local name="$1" purpose="${2:-required tooling}"
  if tool_available "${name}"; then
    return 0
  fi
  record_check MANUAL "tool:${name}" "command-line tool '${name}' is unavailable; ${purpose} must be verified manually"
  return 1
}

# ---------------------------------------------------------------------------
# temp files
# ---------------------------------------------------------------------------
_MILO_TMPDIR=""
# milo_tmpdir_init must be called directly (NOT inside a command
# substitution: the cleanup trap would fire when the subshell exits). It
# sets _MILO_TMPDIR and registers cleanup on script exit.
milo_tmpdir_init() {
  if [[ -z "${_MILO_TMPDIR}" ]]; then
    _MILO_TMPDIR="$(mktemp -d "${TMPDIR:-/tmp}/milo-release.XXXXXX")"
    chmod 700 "${_MILO_TMPDIR}"
    # shellcheck disable=SC2064
    trap "rm -rf '${_MILO_TMPDIR}'" EXIT
  fi
}
milo_tmpdir() {
  milo_tmpdir_init
  printf '%s' "${_MILO_TMPDIR}"
}

# ---------------------------------------------------------------------------
# git helpers
# ---------------------------------------------------------------------------
git_worktree_clean() {
  [[ -z "$(git status --porcelain 2> /dev/null)" ]]
}

git_head_sha() {
  git rev-parse HEAD 2> /dev/null || printf 'unknown'
}

git_current_branch() {
  git rev-parse --abbrev-ref HEAD 2> /dev/null || printf 'unknown'
}

# ---------------------------------------------------------------------------
# apply-mode guard
# ---------------------------------------------------------------------------
# Mutation-capable scripts call apply_guard AFTER parsing:
#   APPLY_MODE (0/1), APPLY_ENVIRONMENT, EXPECTED_PROJECT, EXPECTED_ACCOUNT,
#   EXPECTED_SHA, CONFIRM_PRODUCTION_CHANGE (0/1)
# plus the MILO_OPERATOR_ACK environment variable.
#
# The guard NEVER mutates anything. It validates every acknowledgment and
# every identity precondition, and stops at the first failure. Verification
# of the active Google account/project uses read-only `gcloud config` calls.
apply_guard() {
  if [[ "${APPLY_MODE:-0}" != "1" ]]; then
    record_check BLOCKED "apply-guard" "internal error: apply_guard called outside --apply mode"
    return 1
  fi
  if [[ "${APPLY_ENVIRONMENT:-}" != "production" ]]; then
    record_check BLOCKED "apply-guard:environment" "--environment production is required for apply mode"
    return 1
  fi
  if [[ "${CONFIRM_PRODUCTION_CHANGE:-0}" != "1" ]]; then
    record_check BLOCKED "apply-guard:confirm" "--confirm-production-change is required for apply mode"
    return 1
  fi
  if [[ "${MILO_OPERATOR_ACK:-}" != "${MILO_OPERATOR_ACK_EXPECTED}" ]]; then
    record_check BLOCKED "apply-guard:ack" "environment acknowledgment MILO_OPERATOR_ACK=${MILO_OPERATOR_ACK_EXPECTED} is required"
    return 1
  fi
  require_value "apply-guard:expected-project" "${EXPECTED_PROJECT:-}" || return 1
  require_value "apply-guard:expected-account" "${EXPECTED_ACCOUNT:-}" || return 1
  if ! is_full_sha "${EXPECTED_SHA:-}"; then
    record_check BLOCKED "apply-guard:expected-sha" "--expected-sha must be the full 40-character commit SHA"
    return 1
  fi
  if ! git_worktree_clean; then
    record_check BLOCKED "apply-guard:worktree" "apply mode refuses to run from a dirty Git worktree"
    return 1
  fi
  local head
  head="$(git_head_sha)"
  if [[ "${head}" != "${EXPECTED_SHA}" ]]; then
    record_check BLOCKED "apply-guard:sha" "checked-out commit ${head} does not equal the intended release SHA ${EXPECTED_SHA}"
    return 1
  fi
  if ! tool_available gcloud; then
    record_check BLOCKED "apply-guard:gcloud" "gcloud is required to verify the active account and project before apply"
    return 1
  fi
  local active_account active_project
  active_account="$(gcloud config get-value account 2> /dev/null | tr -d '[:space:]')"
  active_project="$(gcloud config get-value project 2> /dev/null | tr -d '[:space:]')"
  if [[ "${active_account}" != "${EXPECTED_ACCOUNT}" ]]; then
    record_check BLOCKED "apply-guard:account" "active Google account '${active_account}' does not match --expected-account"
    return 1
  fi
  if [[ "${active_project}" != "${EXPECTED_PROJECT}" ]]; then
    record_check BLOCKED "apply-guard:project" "active Google Cloud project '${active_project}' does not match --expected-project"
    return 1
  fi
  record_check PASS "apply-guard" "all apply-mode acknowledgments and identity preconditions verified"
  return 0
}

# write_audit_record PATH SCRIPT ACTION — append a secret-free audit line.
write_audit_record() {
  local path="$1" script="$2" action="$3"
  local ts
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  umask 077
  printf '%s script=%s sha=%s branch=%s account=%s project=%s action=%s\n' \
    "${ts}" "${script}" "$(git_head_sha)" "$(git_current_branch)" \
    "${EXPECTED_ACCOUNT:-n/a}" "${EXPECTED_PROJECT:-n/a}" "${action}" >> "${path}"
}

# ---------------------------------------------------------------------------
# env-file loading (metadata only; values may be empty/omitted)
# ---------------------------------------------------------------------------
# load_env_file PATH PREFIX — reads NAME=VALUE lines into PREFIX_<NAME> shell
# variables. Rejects malformed lines. Values are treated as operator-supplied
# metadata and are never printed unredacted.
load_env_file() {
  local path="$1" prefix="$2" line name value
  if [[ ! -f "${path}" ]]; then
    record_check BLOCKED "env-file" "environment metadata file not found: ${path}"
    return 1
  fi
  while IFS= read -r line || [[ -n "${line}" ]]; do
    [[ -z "${line}" || "${line}" == \#* ]] && continue
    if [[ ! "${line}" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]]; then
      record_check BLOCKED "env-file" "malformed line in ${path} (expected NAME=VALUE)"
      return 1
    fi
    name="${line%%=*}"
    value="${line#*=}"
    printf -v "${prefix}_${name}" '%s' "${value}"
  done < "${path}"
  return 0
}

# env_meta NAME PREFIX — echo the loaded value ('' when unset).
env_meta() {
  local var="$2_$1"
  printf '%s' "${!var:-}"
}

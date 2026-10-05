#!/usr/bin/env bash
# shellcheck disable=SC2034  # values here are consumed by the sourcing script
# Shared helpers for the operate-from-a-phone scripts (scripts/ops/*.sh), which
# the GitHub Actions workflows under .github/workflows/ run.
#
# This file is SOURCED, never executed. It owns no production logic: every
# script under scripts/ops/ wraps the EXISTING canonical tool for its step
# (production-activate.sh, production-verify.sh, kill-switch.sh,
# website-execution-activate.sh, check-migration-state.sh, ...) and adds only
# three things a workflow needs:
#
#   * a DRY RUN (--dry-run): every command is printed and NOTHING external is
#     called -- no gcloud, no psql, no network. It is the plan, exactly.
#   * SUMMARY| lines -- `SUMMARY|<STEP>|<RESULT>|<detail>` on stdout and as a
#     row of the job summary ($GITHUB_STEP_SUMMARY) -- so the phone shows what
#     happened without anyone opening a log.
#   * a hard rule on secrets: a value is NEVER printed. The read-only database
#     URL is read from the variable the operator configuration names
#     (READONLY_DATABASE_URL_ENV) and handed to psql as an argument only; no
#     script here turns on shell tracing, echoes an environment value, or prints a
#     connection string, a token or a key. tests/test_ops_workflows.py runs
#     every script with sentinel secrets and fails if one ever appears.

OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${OPS_DIR}/../.." && pwd)"
MILO_REPO_ROOT="$REPO_ROOT"
# shellcheck source=../deploy/operator-config.sh
source "${REPO_ROOT}/scripts/deploy/operator-config.sh"
# shellcheck source=../deploy/deployment-contract.sh
source "${REPO_ROOT}/scripts/deploy/deployment-contract.sh"

DRY_RUN=0
MILO_OPERATOR_CONFIG_PATH="${MILO_OPERATOR_CONFIG_PATH:-}"
OPS_FAILED=0

# The run statuses that are terminal (backend/runtime.py TERMINAL_STATES).
OPS_TERMINAL_STATUSES="'completed','partial_success','failed','cancelled','timed_out','budget_exhausted'"

# summary STEP RESULT [DETAIL] — one machine-readable line, and one job-summary
# row. RESULT is PASS / FAIL / SKIPPED / DRY-RUN / INFO. DETAIL is authored
# text or a static code, never a value read from a secret.
summary() {
  local step="$1" result="$2" detail="${3:-}"
  printf 'SUMMARY|%s|%s|%s\n' "$step" "$result" "$detail"
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    printf '| %s | %s | %s |\n' "$step" "$result" "${detail//|//}" >> "$GITHUB_STEP_SUMMARY"
  fi
  [[ "$result" == "FAIL" ]] && OPS_FAILED=1
  return 0
}

summary_header() {
  local title="$1"
  printf '== %s ==\n' "$title"
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    printf '### %s\n\n| Step | Result | Detail |\n|---|---|---|\n' "$title" >> "$GITHUB_STEP_SUMMARY"
  fi
}

# summary_note TEXT — a sentence under the table (a reminder, never a value).
summary_note() {
  printf 'NOTE: %s\n' "$1"
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    printf '\n> %s\n' "$1" >> "$GITHUB_STEP_SUMMARY"
  fi
}

ops_fail() {
  summary "${2:-STOP}" FAIL "$1"
  printf 'FAIL: %s\n' "$1" >&2
  exit 1
}

# ops_run CMD... — execute, or in a dry run print it (shell-quoted) and call
# nothing. Arguments are printed as given, so a caller never passes a secret
# VALUE here: the database URL travels through the environment, by name.
ops_run() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf 'DRY-RUN:'
    printf ' %q' "$@"
    printf '\n'
    return 0
  fi
  "$@"
}

# ops_load_config — the operator identifiers (non-secret). A workflow writes
# them from the repository variable MILO_OPERATOR_CONFIG
# (scripts/ops/write-operator-config.sh) and exports MILO_OPERATOR_CONFIG.
ops_load_config() {
  CONFIG_PATH="$(milo_operator_config_path "$REPO_ROOT" "$MILO_OPERATOR_CONFIG_PATH")"
  milo_load_operator_config "$CONFIG_PATH" || exit 2
  milo_require_op GCP_PROJECT_ID GCP_REGION CLOUD_RUN_API_SERVICE CLOUD_RUN_WORKER_JOB || exit 2
  PROJECT_ID="$(milo_op GCP_PROJECT_ID)"
  REGION="$(milo_op GCP_REGION)"
  API_SERVICE="$(milo_op CLOUD_RUN_API_SERVICE)"
  WORKER_JOB="$(milo_op CLOUD_RUN_WORKER_JOB)"
  DB_URL_ENV="$(milo_op READONLY_DATABASE_URL_ENV)"
  DB_URL_ENV="${DB_URL_ENV:-MILO_READONLY_DB_URL}"
  if [[ ! "$DB_URL_ENV" =~ ^[A-Z_][A-Z0-9_]*$ ]]; then
    printf 'FAIL: READONLY_DATABASE_URL_ENV must name an environment variable.\n' >&2
    exit 2
  fi
}

# PR-SYNC-2: the register sync's Cloud Scheduler identity -- one fixed account
# per project, created by setup-register-scheduler.sh, written onto the API by
# deploy.sh (MILO_REGISTER_SCHEDULER_IDENTITY). Never typed by hand.
ops_register_scheduler_account_id() {
  printf 'milo-register-scheduler'
}
ops_register_scheduler_identity() {
  printf '%s@%s.iam.gserviceaccount.com' "$(ops_register_scheduler_account_id)" "$PROJECT_ID"
}

# ops_live_runs — the number of non-terminal runs, on stdout. The same
# statement production-verify.sh's RUNS_QUIESCENT runs, read-only. The URL is
# read from the named variable and never printed; psql's stderr is dropped
# because it can quote the connection target.
ops_live_runs() {
  local url="${!DB_URL_ENV:-}" count
  [[ -n "$url" ]] || return 1
  count="$(psql "$url" -X -A -t -v ON_ERROR_STOP=1 -c \
    "select count(*) from public.runs where status not in (${OPS_TERMINAL_STATUSES});" 2> /dev/null)" \
    || return 1
  count="${count//[[:space:]]/}"
  [[ "$count" =~ ^[0-9]+$ ]] || return 1
  printf '%s' "$count"
}

# ops_require_zero_live_runs STEP — PASS with 0 live runs, FAIL otherwise.
ops_require_zero_live_runs() {
  local step="$1" live
  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf 'DRY-RUN: psql "$%s" -c "select count(*) from public.runs where status not in (...terminal...)"\n' "$DB_URL_ENV"
    summary "$step" DRY-RUN "would require 0 non-terminal runs"
    return 0
  fi
  if ! live="$(ops_live_runs)"; then
    ops_fail "the runs table could not be read with the read-only connection in \$${DB_URL_ENV}" "$step"
  fi
  if [[ "$live" -ne 0 ]]; then
    ops_fail "${live} non-terminal run(s) exist; nothing was changed" "$step"
  fi
  summary "$step" PASS "0 non-terminal runs"
}

# ops_execution_outcome EXECUTION PATTERN WAIT_SECONDS POLL_SECONDS — the
# capture job execution's own outcome line (the newest line of its Cloud
# Logging entries matching the extended regex PATTERN), on stdout.
#
# Cloud Logging makes an entry readable some time AFTER the execution reports
# Completed, so a single read right after completion can find nothing. This
# reads again every POLL_SECONDS until the line appears or WAIT_SECONDS have
# been waited (a bounded wait; POLL_SECONDS 0 counts as 1 so it always ends).
# Returns 1 with nothing on stdout when no outcome line appeared: the caller
# FAILS its step on that, never passes. A read that itself failed is reported
# on stderr by its gcloud exit status only (the entries are not printed).
ops_execution_outcome() {
  local execution="$1" pattern="$2" wait="$3" poll="$4" waited=0 entries status line
  while :; do
    status=0
    entries="$(gcloud logging read \
      "resource.type=cloud_run_job AND labels.\"run.googleapis.com/execution_name\"=${execution}" \
      --project "$PROJECT_ID" --format='value(textPayload)' --limit 400 2> /dev/null)" || status=$?
    if [[ "$status" -eq 0 ]]; then
      line="$(grep -E "$pattern" <<< "$entries" | head -n 1 || true)"
      if [[ -n "$line" ]]; then
        printf '%s\n' "$line"
        return 0
      fi
    else
      printf 'gcloud logging read exited %s (after %ss)\n' "$status" "$waited" >&2
    fi
    (( waited < wait )) || return 1
    sleep "$poll"
    waited=$(( waited + (poll > 0 ? poll : 1) ))
  done
}

# ops_worker_env_json — the worker job's container env as JSON (names, plain
# values and secret REFERENCES; a secret value is never in a describe).
ops_worker_json() {
  gcloud run jobs describe "$WORKER_JOB" --region "$REGION" --project "$PROJECT_ID" --format=json
}

# ops_container_facts < JSON — "env NAME VALUE" / "secret NAME" lines for the
# first container. VALUES only for an allowlist of non-secret flags and model
# names; every other variable, and every secret-backed one, by NAME only.
ops_container_facts() {
  python3 -c '
import json, re, sys
ALLOWED = re.compile(r"^(MILO_ENABLE_[A-Z_]+|MILO_CAPTURE_REPLAY|JOB_LAUNCHER|MILO_RELEASE_SHA"
                     r"|MILO_COMMANDER_MODEL|MILO_COMMANDER_MODEL_ALLOWLIST|MILO_SWARM_WORKER_MODEL)$")
doc = json.load(sys.stdin)
def containers(node):
    if isinstance(node, dict):
        if isinstance(node.get("containers"), list) and node["containers"]:
            return node["containers"]
        for child in node.values():
            found = containers(child)
            if found:
                return found
    elif isinstance(node, list):
        for child in node:
            found = containers(child)
            if found:
                return found
    return []
found = containers(doc.get("spec", doc)) or [{}]
for entry in found[0].get("env") or []:
    if not isinstance(entry, dict):
        continue
    if (entry.get("valueFrom") or {}).get("secretKeyRef"):
        print("secret\t%s" % entry.get("name"))
    elif ALLOWED.match(str(entry.get("name") or "")):
        print("env\t%s\t%s" % (entry.get("name"), entry.get("value") or ""))
    else:
        # A plain variable outside the allowlist is reported by NAME only: a
        # legacy plain-text provider key must never reach a log.
        print("env\t%s\t<not read>" % entry.get("name"))
'
}

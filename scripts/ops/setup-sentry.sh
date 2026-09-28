#!/usr/bin/env bash
# ONE-TIME and idempotent, in Cloud Shell: the backend's Sentry DSN (PR-OBS,
# OBS-5). Error reporting stays OFF until this has stored a DSN.
#
#   (default)  converge, then verify.
#   --check    verify only; changes nothing.
#
# Every line it prints is `PASS ...` or `FAIL ...`; it exits non-zero if any
# FAIL was printed. The DSN is read from a file and never printed or put on a
# command line.
#
#   1. The Secret Manager secret SENTRY_DSN (automatic replication).
#   2. roles/secretmanager.secretAccessor on THAT secret only, for the three
#      runtime identities named in the operator configuration
#      (API_SERVICE_ACCOUNT, WORKER_SERVICE_ACCOUNT, CAPTURE_SERVICE_ACCOUNT --
#      the capture job runs as the worker identity when the last is empty).
#      Granted BEFORE a version exists: a deploy binds SENTRY_DSN only once it
#      has an enabled version (MILO_OPTIONAL_RUNTIME_SECRETS in
#      scripts/deploy/deployment-contract.sh), and Cloud Run refuses a binding
#      its identity cannot read.
#   3. With --dsn-file (default ~/.milo_sentry_dsn, mode 600): a new secret
#      version when the file differs from the latest one, and the GitHub
#      environment secret SENTRY_DSN of production-backup (the scheduled
#      backup's failure report), set from STDIN.
#   The next deploy (and the next capture-job ensure) binds it; nothing here
#   deploys or restarts anything.
#
# Options: --dsn-file PATH, --no-github, --operator-config PATH, --check
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

MODE="apply"
SECRET_NAME="SENTRY_DSN"
DSN_FILE="${HOME}/.milo_sentry_dsn"
GITHUB=1
GITHUB_REPOSITORY_NAME="${MILO_GITHUB_REPOSITORY:-giladscore494/milo-agent-workspace}"
BACKUP_ENVIRONMENT="production-backup"

usage() {
  sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --check) MODE="check"; shift ;;
    --dsn-file) DSN_FILE="${2:?}"; shift 2 ;;
    --no-github) GITHUB=0; shift ;;
    --operator-config) MILO_OPERATOR_CONFIG_PATH="${2:?}"; shift 2 ;;
    --help) usage; exit 0 ;;
    *) printf 'FAIL unknown argument %s\n' "$1"; exit 2 ;;
  esac
done

FAILURES=0
pass() { printf 'PASS %s\n' "$1"; }
fail_line() { printf 'FAIL %s\n' "$1"; FAILURES=$((FAILURES + 1)); }
applying() { [[ "$MODE" == "apply" ]]; }
quiet() { "$@" > /dev/null 2>&1; }

# ops_load_config exits on a bad configuration; probe it in a subshell first so
# the failure is a FAIL line, not silence.
( ops_load_config ) > /dev/null 2>&1 || { printf 'FAIL the operator configuration could not be loaded\n'; exit 2; }
ops_load_config > /dev/null 2>&1
milo_require_op API_SERVICE_ACCOUNT WORKER_SERVICE_ACCOUNT > /dev/null 2>&1 \
  || { printf 'FAIL the operator configuration names no API_SERVICE_ACCOUNT / WORKER_SERVICE_ACCOUNT\n'; exit 2; }
command -v gcloud > /dev/null 2>&1 || { printf 'FAIL gcloud is required\n'; exit 2; }
[[ -n "$(gcloud config get-value account 2> /dev/null)" ]] || { printf 'FAIL no active gcloud account\n'; exit 2; }

CAPTURE_SA="$(milo_op CAPTURE_SERVICE_ACCOUNT)"
declare -A IDENTITIES=(
  [api]="$(milo_op API_SERVICE_ACCOUNT)"
  [worker]="$(milo_op WORKER_SERVICE_ACCOUNT)"
  [capture]="${CAPTURE_SA:-$(milo_op WORKER_SERVICE_ACCOUNT)}"
)

has_member() {
  python3 -c '
import json, sys
policy = json.loads(sys.argv[1] or "{}")
sys.exit(0 if any(b.get("role") == sys.argv[2] and sys.argv[3] in (b.get("members") or [])
                  for b in policy.get("bindings") or []) else 1)' "$1" "$2" "$3"
}

# 1. The secret container.
if ! quiet gcloud secrets describe "$SECRET_NAME" --project "$PROJECT_ID" && applying; then
  quiet gcloud secrets create "$SECRET_NAME" --replication-policy=automatic --project "$PROJECT_ID" || true
fi
if quiet gcloud secrets describe "$SECRET_NAME" --project "$PROJECT_ID"; then
  pass "Secret Manager secret ${SECRET_NAME} exists"
else
  fail_line "Secret Manager secret ${SECRET_NAME} is missing"
fi

# 2. Accessor on this secret only, per runtime identity.
secret_policy() { gcloud secrets get-iam-policy "$SECRET_NAME" --project "$PROJECT_ID" --format=json 2> /dev/null || printf '{}'; }
for surface in api worker capture; do
  member="serviceAccount:${IDENTITIES[$surface]}"
  if ! has_member "$(secret_policy)" roles/secretmanager.secretAccessor "$member" && applying; then
    quiet gcloud secrets add-iam-policy-binding "$SECRET_NAME" --project "$PROJECT_ID" \
      --member "$member" --role roles/secretmanager.secretAccessor || true
  fi
  if has_member "$(secret_policy)" roles/secretmanager.secretAccessor "$member"; then
    pass "the ${surface} runtime identity can read ${SECRET_NAME} (this secret only)"
  else
    fail_line "the ${surface} runtime identity cannot read ${SECRET_NAME}"
  fi
done

# 3. The DSN value.
latest_sha() {
  gcloud secrets versions access latest --secret "$SECRET_NAME" --project "$PROJECT_ID" 2> /dev/null \
    | tr -d '\r\n' | sha256sum | awk '{print $1}'
}
enabled_versions() {
  gcloud secrets versions list "$SECRET_NAME" --project "$PROJECT_ID" --filter='state:ENABLED' \
    --format='value(name)' 2> /dev/null | wc -l
}
if [[ -s "$DSN_FILE" ]]; then
  if [[ "$(stat -c '%a' "$DSN_FILE")" != "600" ]]; then
    fail_line "the DSN file is not mode 600 (chmod 600 it)"
  elif ! tr -d '\r\n' < "$DSN_FILE" | grep -Eq '^https://[^/@[:space:]]+@[^/[:space:]]+/[0-9]+$'; then
    fail_line "the DSN file does not hold a Sentry DSN (https://<key>@<host>/<project id>)"
  else
    file_sha="$(tr -d '\r\n' < "$DSN_FILE" | sha256sum | awk '{print $1}')"
    if [[ "$(latest_sha)" != "$file_sha" ]] && applying; then
      tr -d '\r\n' < "$DSN_FILE" | quiet gcloud secrets versions add "$SECRET_NAME" --data-file=- \
        --project "$PROJECT_ID" || true
    fi
    if [[ "$(latest_sha)" == "$file_sha" ]]; then
      pass "${SECRET_NAME} latest version matches the DSN file (value not shown); the next deploy binds it"
    else
      fail_line "${SECRET_NAME} latest version does not match the DSN file"
    fi
    if [[ "$GITHUB" == "1" ]]; then
      if applying && command -v gh > /dev/null 2>&1; then
        tr -d '\r\n' < "$DSN_FILE" | quiet gh secret set SENTRY_DSN --env "$BACKUP_ENVIRONMENT" \
          --repo "$GITHUB_REPOSITORY_NAME" || true
      fi
      if gh secret list --env "$BACKUP_ENVIRONMENT" --repo "$GITHUB_REPOSITORY_NAME" --json name \
           --jq '.[].name' 2> /dev/null | grep -qx SENTRY_DSN; then
        pass "GitHub environment ${BACKUP_ENVIRONMENT} secret SENTRY_DSN is set (value not shown)"
      else
        fail_line "GitHub environment ${BACKUP_ENVIRONMENT} has no SENTRY_DSN secret (run setup-backup.sh first, then re-run)"
      fi
    fi
  fi
elif [[ "$(enabled_versions)" -gt 0 ]]; then
  pass "${SECRET_NAME} has an enabled version; no DSN file given, nothing changed"
else
  pass "${SECRET_NAME} has no version: error reporting stays OFF until a DSN is stored (--dsn-file)"
fi

if [[ "$FAILURES" -gt 0 ]]; then
  printf 'FAIL %d check(s) failed\n' "$FAILURES"
  exit 1
fi
pass "Sentry DSN setup complete (${MODE})"

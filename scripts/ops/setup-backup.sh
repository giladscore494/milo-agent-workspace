#!/usr/bin/env bash
# ONE-TIME and idempotent, in Cloud Shell (as a project owner, with `gh`
# logged in as a repository admin): everything the scheduled Supabase backup
# (.github/workflows/backup-supabase-scheduled.yml) needs. PR-OBS, OBS-1/OBS-2.
#
#   (default)  converge: create or correct what is missing, then verify.
#   --check    verify only; changes nothing.
#
# Every line it prints is `PASS ...` or `FAIL ...`; it exits non-zero if any
# FAIL was printed. It never prints a secret, a connection string, the
# passphrase or an e-mail address (service accounts are named by account id).
#
# What it sets up, each at the narrowest scope:
#   1. APIs: Cloud Storage, IAM, IAM Credentials, STS.
#   2. The backup bucket (--bucket, default <project>-milo-supabase-backups):
#      the configured region (us-central1), uniform bucket-level access,
#      public access prevention enforced, lifecycle DELETE at 30 days, and an
#      UNLOCKED 7-day retention policy (a backup cannot be removed early; the
#      policy is never locked by this script).
#   3. Two service accounts, no keys:
#        milo-backup-writer  roles/storage.objectCreator on THAT bucket only
#                            (create; no read, no overwrite, no delete)
#        milo-backup-reader  roles/storage.objectViewer on THAT bucket only
#                            (the monthly restore test)
#   4. Workload Identity: the EXISTING pool milo-github / provider
#      github-actions (scripts/ops/setup-wif.sh), whose attribute condition
#      pins this repository and refs/heads/main and must admit the environment
#      production-backup -- this script only READS that condition (run
#      setup-wif.sh --apply first) and never changes the pool or provider.
#      Each backup account's workloadIdentityUser goes to the principalSet of
#      that environment only (attribute.environment/production-backup), never
#      to the repository-wide principalSet the deployer uses.
#   5. The backup passphrase: a NEW random value in ~/.milo_backup_passphrase
#      (mode 600), created once and never printed. It is NOT the manual
#      workflow's SUPABASE_BACKUP_PASSPHRASE. Keep a copy in the operator's
#      password manager: GitHub secrets cannot be read back, and every backup
#      in the bucket needs it to be restored.
#   6. The GitHub environment production-backup: no required reviewer,
#      deployments from main only; its secrets MILO_BACKUP_DB_URL (from
#      ~/.milo_ro_url, the read-only role), MILO_BACKUP_PASSPHRASE,
#      MILO_BACKUP_WRITER_SA, MILO_BACKUP_READER_SA -- each set with
#      `gh secret set --env production-backup` reading STDIN, never argv --
#      and its variables MILO_BACKUP_BUCKET and MILO_BACKUP_PG_MAJOR (the
#      provider is the repository variable GCP_WORKLOAD_IDENTITY_PROVIDER).
#
# Options:
#   --bucket NAME            backup bucket (default <project>-milo-supabase-backups)
#   --pg-major N             the server's PostgreSQL major (default: read with
#                            psql through ~/.milo_ro_url)
#   --rotate-passphrase      replace the passphrase secret with the local file
#                            even when the local file had to be created now
#   --operator-config PATH   operator configuration (GCP_PROJECT_ID, GCP_REGION)
#   --check                  verify only
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

MODE="apply"
GITHUB_REPOSITORY_NAME="${MILO_GITHUB_REPOSITORY:-giladscore494/milo-agent-workspace}"
ENVIRONMENT_NAME="production-backup"
# The existing pool and provider (scripts/ops/setup-wif.sh), read only.
POOL_ID="milo-github"
PROVIDER_ID="github-actions"
WRITER_ID="milo-backup-writer"
READER_ID="milo-backup-reader"
RETENTION_SECONDS=604800   # 7 days
LIFECYCLE_DELETE_DAYS=30
PASSPHRASE_FILE="${HOME}/.milo_backup_passphrase"
RO_URL_FILE="${HOME}/.milo_ro_url"
BUCKET=""
PG_MAJOR=""
ROTATE=0

usage() {
  sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --check) MODE="check"; shift ;;
    --bucket) BUCKET="${2:?}"; shift 2 ;;
    --pg-major) PG_MAJOR="${2:?}"; shift 2 ;;
    --rotate-passphrase) ROTATE=1; shift ;;
    --operator-config) MILO_OPERATOR_CONFIG_PATH="${2:?}"; shift 2 ;;
    --help) usage; exit 0 ;;
    *) printf 'FAIL unknown argument %s\n' "$1"; exit 2 ;;
  esac
done

FAILURES=0
pass() { printf 'PASS %s\n' "$1"; }
fail_line() { printf 'FAIL %s\n' "$1"; FAILURES=$((FAILURES + 1)); }
applying() { [[ "$MODE" == "apply" ]]; }
# quiet CMD... -- run with every output discarded (a gcloud or gh message can
# quote an account e-mail or a value).
quiet() { "$@" > /dev/null 2>&1; }

[[ "$GITHUB_REPOSITORY_NAME" =~ ^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$ ]] \
  || { printf 'FAIL MILO_GITHUB_REPOSITORY must be owner/repo\n'; exit 2; }
# ops_load_config exits on a bad configuration; probe it in a subshell first so
# the failure is a FAIL line, not silence.
( ops_load_config ) > /dev/null 2>&1 || { printf 'FAIL the operator configuration could not be loaded (GCP_PROJECT_ID, GCP_REGION)\n'; exit 2; }
ops_load_config > /dev/null 2>&1
BUCKET="${BUCKET:-${PROJECT_ID}-milo-supabase-backups}"
[[ "$BUCKET" =~ ^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$ ]] || { printf 'FAIL --bucket is not a valid bucket name\n'; exit 2; }
for tool in gcloud gh python3 openssl; do
  command -v "$tool" > /dev/null 2>&1 || { printf 'FAIL %s is required\n' "$tool"; exit 2; }
done
[[ -n "$(gcloud config get-value account 2> /dev/null)" ]] || { printf 'FAIL no active gcloud account\n'; exit 2; }
quiet gh auth status || { printf 'FAIL gh is not logged in (gh auth login)\n'; exit 2; }
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)' 2> /dev/null || true)"
[[ "$PROJECT_NUMBER" =~ ^[0-9]+$ ]] || { printf 'FAIL project %s is not readable\n' "$PROJECT_ID"; exit 2; }

WRITER_SA="${WRITER_ID}@${PROJECT_ID}.iam.gserviceaccount.com"
READER_SA="${READER_ID}@${PROJECT_ID}.iam.gserviceaccount.com"
POOL_NAME="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_ID}"
PRINCIPAL="principalSet://iam.googleapis.com/${POOL_NAME}/attribute.environment/${ENVIRONMENT_NAME}"
has_member() {
  # has_member POLICY_JSON ROLE MEMBER
  python3 -c '
import json, sys
policy = json.loads(sys.argv[1] or "{}")
sys.exit(0 if any(b.get("role") == sys.argv[2] and sys.argv[3] in (b.get("members") or [])
                  for b in policy.get("bindings") or []) else 1)' "$1" "$2" "$3"
}
other_roles() {
  # other_roles POLICY_JSON MEMBER ALLOWED_ROLE -> the member's other roles
  python3 -c '
import json, sys
policy = json.loads(sys.argv[1] or "{}")
print(" ".join(sorted({b.get("role", "") for b in policy.get("bindings") or []
                       if sys.argv[2] in (b.get("members") or []) and b.get("role") != sys.argv[3]})))' "$1" "$2" "$3"
}

# 1. APIs ---------------------------------------------------------------------
for api in storage.googleapis.com iam.googleapis.com iamcredentials.googleapis.com sts.googleapis.com; do
  if [[ "$(gcloud services list --enabled --project "$PROJECT_ID" --filter="config.name:${api}" \
          --format='value(config.name)' 2> /dev/null)" != "$api" ]] && applying; then
    quiet gcloud services enable "$api" --project "$PROJECT_ID" || true
  fi
  if [[ "$(gcloud services list --enabled --project "$PROJECT_ID" --filter="config.name:${api}" \
          --format='value(config.name)' 2> /dev/null)" == "$api" ]]; then
    pass "API ${api} enabled"
  else
    fail_line "API ${api} is not enabled"
  fi
done

# 2. The bucket ------------------------------------------------------------------
LIFECYCLE_FILE="$(mktemp)"
trap 'rm -f "$LIFECYCLE_FILE"' EXIT
printf '{"rule": [{"action": {"type": "Delete"}, "condition": {"age": %d}}]}\n' "$LIFECYCLE_DELETE_DAYS" > "$LIFECYCLE_FILE"
bucket_json() { gcloud storage buckets describe "gs://${BUCKET}" --format=json 2> /dev/null || true; }
bucket_problems() {
  # bucket_problems JSON -> one word per property that is not as required
  python3 -c '
import json, sys
raw, region, retention, age = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
try:
    b = json.loads(raw)
except ValueError:
    print("missing"); sys.exit()
def pick(*names):
    for name in names:
        if name in b:
            return b[name]
    return None
problems = []
if str(pick("location") or "").lower() != region.lower():
    problems.append("location")
ubla = pick("uniform_bucket_level_access", "uniformBucketLevelAccess")
if isinstance(ubla, dict):
    ubla = ubla.get("enabled")
if ubla is not True:
    problems.append("uniform-access")
if str(pick("public_access_prevention", "publicAccessPrevention") or "").lower() != "enforced":
    problems.append("public-access-prevention")
policy = pick("retention_policy", "retentionPolicy") or {}
period = policy.get("retentionPeriod", policy.get("retention_period"))
if str(period) != str(retention):
    problems.append("retention")
if policy.get("isLocked") or policy.get("is_locked"):
    problems.append("retention-locked")
rules = (pick("lifecycle_config", "lifecycle") or {}).get("rule") or []
if not any((r.get("action") or {}).get("type") == "Delete" and (r.get("condition") or {}).get("age") == age
           and set(r.get("condition") or {}) == {"age"} for r in rules):
    problems.append("lifecycle")
print(" ".join(problems))' "$1" "$REGION" "$RETENTION_SECONDS" "$LIFECYCLE_DELETE_DAYS"
}
problems="$(bucket_problems "$(bucket_json)")"
if [[ "$problems" == "missing" ]] && applying; then
  quiet gcloud storage buckets create "gs://${BUCKET}" --project "$PROJECT_ID" --location "$REGION" \
    --uniform-bucket-level-access --public-access-prevention \
    --retention-period "${RETENTION_SECONDS}s" --lifecycle-file "$LIFECYCLE_FILE" || true
  problems="$(bucket_problems "$(bucket_json)")"
fi
if [[ -n "$problems" && "$problems" != "missing" && "$problems" != *location* && "$problems" != *retention-locked* ]] && applying; then
  quiet gcloud storage buckets update "gs://${BUCKET}" --uniform-bucket-level-access \
    --public-access-prevention --retention-period "${RETENTION_SECONDS}s" --lifecycle-file "$LIFECYCLE_FILE" || true
  problems="$(bucket_problems "$(bucket_json)")"
fi
if [[ -z "$problems" ]]; then
  pass "bucket gs://${BUCKET}: ${REGION}, uniform access, public access prevented, delete after ${LIFECYCLE_DELETE_DAYS} days, unlocked ${RETENTION_SECONDS}s retention"
else
  fail_line "bucket gs://${BUCKET} is not as required (${problems})"
fi

# 3. Service accounts and their bucket-level roles ----------------------------------
for account_id in "$WRITER_ID" "$READER_ID"; do
  email="${account_id}@${PROJECT_ID}.iam.gserviceaccount.com"
  if ! quiet gcloud iam service-accounts describe "$email" --project "$PROJECT_ID" && applying; then
    quiet gcloud iam service-accounts create "$account_id" --project "$PROJECT_ID" \
      --display-name "MILO scheduled Supabase backup (${account_id#milo-backup-})" || true
  fi
  if quiet gcloud iam service-accounts describe "$email" --project "$PROJECT_ID"; then
    pass "service account ${account_id} exists (no key)"
  else
    fail_line "service account ${account_id} is missing"
  fi
done
bucket_policy() { gcloud storage buckets get-iam-policy "gs://${BUCKET}" --format=json 2> /dev/null || printf '{}'; }
project_policy="$(gcloud projects get-iam-policy "$PROJECT_ID" --format=json 2> /dev/null || printf '{}')"
for pair in "${WRITER_ID}:roles/storage.objectCreator" "${READER_ID}:roles/storage.objectViewer"; do
  account_id="${pair%%:*}"
  role="${pair#*:}"
  member="serviceAccount:${account_id}@${PROJECT_ID}.iam.gserviceaccount.com"
  if ! has_member "$(bucket_policy)" "$role" "$member" && applying; then
    quiet gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" --member "$member" --role "$role" || true
  fi
  policy="$(bucket_policy)"
  if has_member "$policy" "$role" "$member"; then
    pass "${account_id} holds ${role} on gs://${BUCKET} only"
  else
    fail_line "${account_id} lacks ${role} on gs://${BUCKET}"
  fi
  extra="$(other_roles "$policy" "$member" "$role")"
  project_extra="$(other_roles "$project_policy" "$member" "")"
  if [[ -n "$extra" || -n "$project_extra" ]]; then
    fail_line "${account_id} holds more than ${role} (bucket: ${extra:-none}; project: ${project_extra:-none}); remove it"
  else
    pass "${account_id} holds no other role on the bucket or the project"
  fi
done

# 4. Workload Identity: the existing provider must admit production-backup --
#    read only; setup-wif.sh owns the condition.
provider_condition="$(gcloud iam workload-identity-pools providers describe "$PROVIDER_ID" \
  --workload-identity-pool "$POOL_ID" --location global --project "$PROJECT_ID" \
  --format='value(attributeCondition)' 2> /dev/null || true)"
if python3 -c '
import re, sys
condition, repository, environment = sys.argv[1:4]
q = chr(39)
# The canonical form setup-wif.sh writes, and nothing else: a fullmatch, so
# an appended `||`, a negation or a loosened clause can never pass.
name = "[A-Za-z0-9_-]+"
pattern = ("assertion\\.repository == " + q + re.escape(repository) + q
           + " && assertion\\.ref == " + q + "refs/heads/main" + q
           + " && assertion\\.environment in \\[((?:" + q + name + q + ", )*" + q + name + q + ")\\]")
match = re.fullmatch(pattern, condition)
listed = re.findall(q + "(" + name + ")" + q, match.group(1)) if match else []
sys.exit(0 if match and environment in listed else 1)' "$provider_condition" "$GITHUB_REPOSITORY_NAME" "$ENVIRONMENT_NAME"; then
  pass "provider ${PROVIDER_ID} admits ${ENVIRONMENT_NAME} (still pinned to ${GITHUB_REPOSITORY_NAME} and refs/heads/main)"
else
  fail_line "provider ${PROVIDER_ID} does not admit ${ENVIRONMENT_NAME}: run scripts/ops/setup-wif.sh --apply first"
fi
for account_id in "$WRITER_ID" "$READER_ID"; do
  email="${account_id}@${PROJECT_ID}.iam.gserviceaccount.com"
  sa_policy() { gcloud iam service-accounts get-iam-policy "$email" --project "$PROJECT_ID" --format=json 2> /dev/null || printf '{}'; }
  if ! has_member "$(sa_policy)" roles/iam.workloadIdentityUser "$PRINCIPAL" && applying; then
    quiet gcloud iam service-accounts add-iam-policy-binding "$email" --project "$PROJECT_ID" \
      --member "$PRINCIPAL" --role roles/iam.workloadIdentityUser || true
  fi
  if has_member "$(sa_policy)" roles/iam.workloadIdentityUser "$PRINCIPAL"; then
    pass "${account_id}: workloadIdentityUser for principalSet attribute.environment/${ENVIRONMENT_NAME} of pool ${POOL_ID}"
  else
    fail_line "${account_id}: the ${ENVIRONMENT_NAME} principal cannot impersonate it"
  fi
  # Least privilege on the account ITSELF: nobody else may impersonate it or
  # mint its tokens (e.g. the repository-wide principalSet the deployer uses).
  if python3 -c '
import json, sys
policy = json.loads(sys.argv[1] or "{}")
pairs = {(b.get("role"), m) for b in policy.get("bindings") or [] for m in b.get("members") or []}
sys.exit(0 if pairs <= {("roles/iam.workloadIdentityUser", sys.argv[2])} else 1)' "$(sa_policy)" "$PRINCIPAL"; then
    pass "${account_id}: nobody else holds a role on the account itself"
  else
    fail_line "${account_id}: its own IAM policy grants more than workloadIdentityUser to the ${ENVIRONMENT_NAME} principalSet; remove the extra member(s)"
  fi
done

# 5. The passphrase (never printed) ----------------------------------------------------
created_passphrase=0
if [[ ! -s "$PASSPHRASE_FILE" ]] && applying; then
  (umask 077 && openssl rand -base64 48 | tr -d '\n' > "$PASSPHRASE_FILE")
  created_passphrase=1
fi
if [[ -s "$PASSPHRASE_FILE" && "$(stat -c '%a' "$PASSPHRASE_FILE")" == "600" \
      && "$(wc -c < "$PASSPHRASE_FILE")" -ge 32 ]]; then
  pass "backup passphrase file ~/.milo_backup_passphrase present, mode 600 (not printed; keep a copy in the password manager)"
else
  fail_line "the passphrase file ~/.milo_backup_passphrase is missing, not mode 600, or shorter than 32 characters"
fi
if [[ -s "$RO_URL_FILE" && "$(stat -c '%a' "$RO_URL_FILE")" == "600" ]]; then
  pass "read-only database URL file ~/.milo_ro_url present, mode 600 (not printed)"
else
  fail_line "the read-only URL file ~/.milo_ro_url is missing or not mode 600"
fi

# 6. The server major (restore-test container) -------------------------------------------
if [[ -z "$PG_MAJOR" && -s "$RO_URL_FILE" ]] && command -v psql > /dev/null 2>&1; then
  # The URL reaches psql through libpq environment variables (supabase_backup.py
  # pg_env), never a command line.
  detected="$(MILO_BACKUP_DB_URL="$(tr -d '\r\n' < "$RO_URL_FILE")" \
    python3 "${SCRIPT_DIR}/supabase_backup.py" server-major 2> /dev/null || true)"
  [[ "$detected" =~ ^[0-9]{2}$ ]] && PG_MAJOR="$detected"
fi
if [[ "$PG_MAJOR" =~ ^[0-9]{2}$ ]]; then
  pass "server PostgreSQL major ${PG_MAJOR}"
else
  fail_line "the server's PostgreSQL major is unknown: pass --pg-major N"
fi

# 7. The GitHub environment, its secrets and variables -------------------------------------
repo_api="repos/${GITHUB_REPOSITORY_NAME}/environments/${ENVIRONMENT_NAME}"
environment_problems() {
  local env_json branches_json
  env_json="$(gh api "$repo_api" 2> /dev/null || printf '{}')"
  branches_json="$(gh api "${repo_api}/deployment-branch-policies" 2> /dev/null || printf '{}')"
  python3 -c '
import json, sys
env, branches = json.loads(sys.argv[1] or "{}"), json.loads(sys.argv[2] or "{}")
if not env.get("name"):
    print("missing"); sys.exit()
problems = []
if any(rule.get("type") == "required_reviewers" for rule in env.get("protection_rules") or []):
    problems.append("required-reviewer")
policy = env.get("deployment_branch_policy") or {}
if not policy.get("custom_branch_policies"):
    problems.append("branch-policy")
names = sorted((p.get("name"), p.get("type", "branch")) for p in branches.get("branch_policies") or [])
if names != [("main", "branch")]:
    problems.append("branches")
print(" ".join(problems))' "$env_json" "$branches_json"
}
if [[ -n "$(environment_problems)" ]] && applying; then
  printf '{"wait_timer": 0, "reviewers": null, "deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}}' \
    | quiet gh api -X PUT "$repo_api" --input - || true
  existing="$(gh api "${repo_api}/deployment-branch-policies" --jq '.branch_policies[] | "\(.id) \(.name)"' 2> /dev/null || true)"
  while read -r policy_id policy_name; do
    if [[ -n "${policy_id:-}" && "$policy_name" != "main" ]]; then
      quiet gh api -X DELETE "${repo_api}/deployment-branch-policies/${policy_id}" || true
    fi
  done <<< "$existing"
  grep -q ' main$' <<< "$existing" \
    || quiet gh api -X POST "${repo_api}/deployment-branch-policies" -f name=main -f type=branch || true
fi
problems="$(environment_problems)"
if [[ -z "$problems" ]]; then
  pass "GitHub environment ${ENVIRONMENT_NAME}: no required reviewer, deployments from main only"
else
  fail_line "GitHub environment ${ENVIRONMENT_NAME} is not as required (${problems})"
fi

secret_names() { gh secret list --env "$ENVIRONMENT_NAME" --repo "$GITHUB_REPOSITORY_NAME" --json name --jq '.[].name' 2> /dev/null || true; }
set_secret_from_stdin() {
  # set_secret_from_stdin NAME < value -- the value never reaches argv or output
  quiet gh secret set "$1" --env "$ENVIRONMENT_NAME" --repo "$GITHUB_REPOSITORY_NAME"
}
if applying; then
  had_passphrase=0
  grep -qx MILO_BACKUP_PASSPHRASE <<< "$(secret_names)" && had_passphrase=1
  if [[ "$created_passphrase" == "1" && "$had_passphrase" == "1" && "$ROTATE" != "1" ]]; then
    fail_line "MILO_BACKUP_PASSPHRASE already exists in ${ENVIRONMENT_NAME} but ~/.milo_backup_passphrase had to be created now: restore the file from the password manager (existing backups need it), or re-run with --rotate-passphrase to start a new passphrase"
    rm -f "$PASSPHRASE_FILE"
  elif [[ -s "$PASSPHRASE_FILE" ]]; then
    tr -d '\r\n' < "$PASSPHRASE_FILE" | set_secret_from_stdin MILO_BACKUP_PASSPHRASE || true
  fi
  [[ -s "$RO_URL_FILE" ]] && { tr -d '\r\n' < "$RO_URL_FILE" | set_secret_from_stdin MILO_BACKUP_DB_URL || true; }
  printf '%s' "$WRITER_SA" | set_secret_from_stdin MILO_BACKUP_WRITER_SA || true
  printf '%s' "$READER_SA" | set_secret_from_stdin MILO_BACKUP_READER_SA || true
  for pair in "MILO_BACKUP_BUCKET=${BUCKET}" "MILO_BACKUP_PG_MAJOR=${PG_MAJOR}"; do
    [[ -n "${pair#*=}" ]] && { quiet gh variable set "${pair%%=*}" --env "$ENVIRONMENT_NAME" \
      --repo "$GITHUB_REPOSITORY_NAME" --body "${pair#*=}" || true; }
  done
fi
present="$(secret_names)"
for name in MILO_BACKUP_DB_URL MILO_BACKUP_PASSPHRASE MILO_BACKUP_WRITER_SA MILO_BACKUP_READER_SA; do
  if grep -qx "$name" <<< "$present"; then
    pass "environment secret ${name} is set (value not shown)"
  else
    fail_line "environment secret ${name} is not set in ${ENVIRONMENT_NAME}"
  fi
done
variables="$(gh variable list --env "$ENVIRONMENT_NAME" --repo "$GITHUB_REPOSITORY_NAME" --json name,value \
  --jq '.[] | "\(.name)=\(.value)"' 2> /dev/null || true)"
for pair in "MILO_BACKUP_BUCKET=${BUCKET}" "MILO_BACKUP_PG_MAJOR=${PG_MAJOR}"; do
  if [[ -n "${pair#*=}" ]] && grep -qxF "$pair" <<< "$variables"; then
    pass "environment variable ${pair%%=*} is set"
  else
    fail_line "environment variable ${pair%%=*} is missing or differs in ${ENVIRONMENT_NAME}"
  fi
done

if [[ "$FAILURES" -gt 0 ]]; then
  printf 'FAIL %d check(s) failed%s\n' "$FAILURES" "$([[ "$MODE" == "check" ]] && printf '; run without --check to converge' || true)"
  exit 1
fi
pass "scheduled backup setup complete (${MODE})"

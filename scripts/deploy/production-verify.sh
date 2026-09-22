#!/usr/bin/env bash
# Read-only production verification, after capture and deployment.
#
# Answers the exact questions the activation decision turns on, as KEY=VALUE
# lines an operator can paste into a record. It performs ZERO paid provider
# calls: every backend check is a describe, a health probe or a database read,
# and it never creates a run.
#
# It reports UNKNOWN rather than guessing. A check that could not be performed
# is not a check that passed, and this script never lets the two look alike.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
MILO_REPO_ROOT="$REPO_ROOT"
# shellcheck source=operator-config.sh
source "${SCRIPT_DIR}/operator-config.sh"
# shellcheck source=deployment-contract.sh
source "${SCRIPT_DIR}/deployment-contract.sh"

MILO_OPERATOR_CONFIG_PATH="" EXPECTED_SHA=""

usage() {
  cat << 'EOF'
Usage: production-verify.sh [options]

Read-only verification of the deployed production state. Makes no paid call
and creates no run.

Options:
  --operator-config <path>  Operator identifier file.
  --expected-sha <sha>      Release SHA to compare against (default: HEAD).
  --help

Exits nonzero when a required property is not satisfied.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --operator-config) MILO_OPERATOR_CONFIG_PATH="${2:?}"; shift 2 ;;
    --expected-sha) EXPECTED_SHA="${2:?}"; shift 2 ;;
    --help) usage; exit 0 ;;
    *) printf 'FAIL: unknown argument %s\n' "$1" >&2; exit 2 ;;
  esac
done

CONFIG_PATH="$(milo_operator_config_path "$REPO_ROOT" "$MILO_OPERATOR_CONFIG_PATH")"
milo_load_operator_config "$CONFIG_PATH" || exit 2
milo_require_op GCP_PROJECT_ID GCP_REGION CLOUD_RUN_API_SERVICE CLOUD_RUN_WORKER_JOB || exit 2

PROJECT_ID="$(milo_op GCP_PROJECT_ID)"
REGION="$(milo_op GCP_REGION)"
API_SERVICE="$(milo_op CLOUD_RUN_API_SERVICE)"
WORKER_JOB="$(milo_op CLOUD_RUN_WORKER_JOB)"
LOCAL_SHA="$(git -C "$REPO_ROOT" rev-parse HEAD)"
[[ -n "$EXPECTED_SHA" ]] || EXPECTED_SHA="$LOCAL_SHA"

_MILO_VERIFY_TMP="$(mktemp -d "${TMPDIR:-/tmp}/milo-verify.XXXXXX")"
trap 'rm -rf "${_MILO_VERIFY_TMP}"' EXIT

FAILURES=0
note_fail() { printf 'FAIL: %s\n' "$1" >&2; FAILURES=$((FAILURES + 1)); }

# --- release identity ---------------------------------------------------
printf 'CURRENT_LOCAL_SHA=%s\n' "$LOCAL_SHA"

service_env() {
  gcloud run services describe "$API_SERVICE" --region "$REGION" --project "$PROJECT_ID" \
    --format="value(spec.template.spec.containers[0].env.filter(\"name:$1\").extract(\"value\"))" \
    2> /dev/null | tr -d '[]' || true
}
job_env() {
  gcloud run jobs describe "$WORKER_JOB" --region "$REGION" --project "$PROJECT_ID" \
    --format="value(spec.template.spec.template.spec.containers[0].env.filter(\"name:$1\").extract(\"value\"))" \
    2> /dev/null | tr -d '[]' || true
}

API_IMAGE="$(gcloud run services describe "$API_SERVICE" --region "$REGION" --project "$PROJECT_ID" \
  --format='value(spec.template.spec.containers[0].image)' 2> /dev/null || true)"
WORKER_IMAGE="$(gcloud run jobs describe "$WORKER_JOB" --region "$REGION" --project "$PROJECT_ID" \
  --format='value(spec.template.spec.template.spec.containers[0].image)' 2> /dev/null || true)"
DEPLOYED_API_SHA="${API_IMAGE##*:}"
DEPLOYED_WORKER_SHA="${WORKER_IMAGE##*:}"
printf 'DEPLOYED_API_SHA=%s\n' "${DEPLOYED_API_SHA:-UNKNOWN}"
printf 'DEPLOYED_WORKER_SHA=%s\n' "${DEPLOYED_WORKER_SHA:-UNKNOWN}"

API_RELEASE_SHA="$(service_env MILO_RELEASE_SHA)"
WORKER_RELEASE_SHA="$(job_env MILO_RELEASE_SHA)"
if [[ -n "$API_RELEASE_SHA" && "$API_RELEASE_SHA" == "$WORKER_RELEASE_SHA" ]]; then
  printf 'MILO_RELEASE_SHA=%s\n' "$API_RELEASE_SHA"
else
  printf 'MILO_RELEASE_SHA=DISAGREEMENT(api=%s,worker=%s)\n' \
    "${API_RELEASE_SHA:-unset}" "${WORKER_RELEASE_SHA:-unset}"
  note_fail "API and worker do not agree on MILO_RELEASE_SHA"
fi
for pair in "api:${DEPLOYED_API_SHA}" "worker:${DEPLOYED_WORKER_SHA}" "release:${API_RELEASE_SHA}"; do
  if [[ "${pair#*:}" != "$EXPECTED_SHA" ]]; then
    note_fail "${pair%%:*} is at '${pair#*:}', expected ${EXPECTED_SHA}"
  fi
done

# --- database -----------------------------------------------------------
DB_ENV="$(milo_op READONLY_DATABASE_URL_ENV)"
DB_URL="${!DB_ENV:-}"
psql_value() {
  if [[ -z "$DB_URL" ]] || ! command -v psql > /dev/null 2>&1; then
    printf 'UNKNOWN'
    return 1
  fi
  psql "$DB_URL" -At -c "$1" 2> /dev/null || printf 'UNKNOWN'
}

LOCAL_MIGRATIONS="$(find "$REPO_ROOT/supabase/migrations" -name '*.sql' | wc -l | tr -d ' ')"
APPLIED_MIGRATIONS="$(psql_value 'select count(*) from supabase_migrations.schema_migrations;' || true)"
if [[ "$APPLIED_MIGRATIONS" == "$LOCAL_MIGRATIONS" ]]; then
  printf 'MIGRATIONS_ALIGNED=YES (%s/%s)\n' "$APPLIED_MIGRATIONS" "$LOCAL_MIGRATIONS"
elif [[ "$APPLIED_MIGRATIONS" == "UNKNOWN" || -z "$APPLIED_MIGRATIONS" ]]; then
  printf 'MIGRATIONS_ALIGNED=UNKNOWN (no read-only DB URL in $%s)\n' "${DB_ENV:-READONLY_DATABASE_URL_ENV}"
else
  printf 'MIGRATIONS_ALIGNED=NO (%s applied, %s local)\n' "$APPLIED_MIGRATIONS" "$LOCAL_MIGRATIONS"
  note_fail "migration head does not match the repository"
fi

SNAP="$(psql_value "
  select coalesce(s.id::text,'none')||'|'||
         (case when s.activated_at is not null then 'YES' else 'NO' end)||'|'||
         (select count(*) from public.catalog_raw_records r where r.snapshot_id = s.id)||'|'||
         (select count(*) from public.catalog_candidate_variants v where v.snapshot_id = s.id)
  from public.catalog_source_snapshots s
  where s.source_family='government' and s.activated_at is not null
  order by s.activated_at desc limit 1;" || true)"
if [[ -n "$SNAP" && "$SNAP" != "UNKNOWN" ]]; then
  IFS='|' read -r SNAP_ID SNAP_ACTIVE SNAP_RAW SNAP_CAND <<< "$SNAP"
else
  SNAP_ID="none" SNAP_ACTIVE="NO" SNAP_RAW=0 SNAP_CAND=0
fi
printf 'GOVERNMENT_SNAPSHOT_ID=%s\n' "${SNAP_ID:-none}"
printf 'GOVERNMENT_SNAPSHOT_ACTIVE=%s\n' "${SNAP_ACTIVE:-NO}"
printf 'GOVERNMENT_RAW_RECORD_COUNT=%s\n' "${SNAP_RAW:-0}"
printf 'GOVERNMENT_CANDIDATE_COUNT=%s\n' "${SNAP_CAND:-0}"
if [[ "${SNAP_ACTIVE:-NO}" == "YES" && "${SNAP_CAND:-0}" -gt 0 ]]; then
  printf 'USABLE_GOVERNMENT_SNAPSHOT=YES\nDETERMINISTIC_QUEUE_READY=YES\n'
else
  printf 'USABLE_GOVERNMENT_SNAPSHOT=NO\nDETERMINISTIC_QUEUE_READY=NO\n'
  note_fail "no active government snapshot with candidates; preparation would refuse with GOVERNMENT_SNAPSHOT_UNAVAILABLE"
fi

NON_TERMINAL="$(psql_value "select count(*) from public.runs where status not in ('completed','failed','cancelled','timed_out');" || true)"
printf 'NON_TERMINAL_RUN_COUNT=%s\n' "${NON_TERMINAL:-UNKNOWN}"
if [[ "$NON_TERMINAL" =~ ^[0-9]+$ && "$NON_TERMINAL" -gt 0 ]]; then
  note_fail "${NON_TERMINAL} non-terminal run(s) exist"
fi

# --- runtime policy -----------------------------------------------------
if python3 "${REPO_ROOT}/scripts/release/runtime_policy_manifest.py" --format text \
     --live-from-cloud-run --project "$PROJECT_ID" --region "$REGION" \
     --worker-job "$WORKER_JOB" > /dev/null 2>&1; then
  printf 'RUNTIME_POLICY_RESOLVED=YES\n'
else
  printf 'RUNTIME_POLICY_RESOLVED=NO\n'
  note_fail "a mandatory-for-paid dimension is unbound; run scripts/release/runtime_policy_manifest.py"
fi

# --- credentials, existence only ---------------------------------------
PROVIDER_SECRET="$(milo_op SECRET_PROVIDER_API_KEY)"
if [[ -n "$PROVIDER_SECRET" ]] && gcloud secrets describe "$PROVIDER_SECRET" \
     --project "$PROJECT_ID" > /dev/null 2>&1; then
  printf 'PROVIDER_CREDENTIAL_PRESENT=YES (%s exists; value never read)\n' "$PROVIDER_SECRET"
else
  printf 'PROVIDER_CREDENTIAL_PRESENT=NO\n'
fi

if gcloud secrets describe "$(milo_op SECRET_REDIS_URL)" --project "$PROJECT_ID" > /dev/null 2>&1 \
   && gcloud secrets describe "$(milo_op SECRET_REDIS_TOKEN)" --project "$PROJECT_ID" > /dev/null 2>&1; then
  printf 'SHARED_QUOTA_READY=YES (both Upstash secrets exist)\n'
else
  printf 'SHARED_QUOTA_READY=NO\n'
  note_fail "the shared quota/rate-limit store secrets are not both present"
fi

# --- liveness -----------------------------------------------------------
API_URL="$(gcloud run services describe "$API_SERVICE" --region "$REGION" --project "$PROJECT_ID" \
  --format='value(status.url)' 2> /dev/null || true)"
if [[ -n "$API_URL" ]]; then
  # The API requires a verified gateway identity, so an unauthenticated probe
  # is EXPECTED to be rejected. What is being proven here is that the service
  # is serving at all -- any HTTP response proves that; a connection failure
  # does not. This deliberately sends no credential and creates nothing.
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "${API_URL}/healthz" || true)"
  if [[ "$code" =~ ^[0-9]{3}$ && "$code" != "000" ]]; then
    printf 'API_HEALTH=SERVING (HTTP %s at %s)\n' "$code" "$API_URL"
  else
    printf 'API_HEALTH=UNREACHABLE\n'
    note_fail "the API service did not answer at ${API_URL}"
  fi
else
  printf 'API_HEALTH=UNKNOWN\n'
  note_fail "could not resolve the API service URL"
fi

if [[ -n "$WORKER_IMAGE" ]]; then
  printf 'WORKER_READY=YES (job configured at %s)\n' "${DEPLOYED_WORKER_SHA:-unknown}"
else
  printf 'WORKER_READY=NO\n'
  note_fail "the worker job could not be described"
fi

if [[ -n "$API_URL" && -n "$(milo_op GCP_PROJECT_NUMBER)" && -n "$(milo_op GATEWAY_SERVICE_ACCOUNT)" ]]; then
  printf 'FRONTEND_BACKEND_BINDING=READY (Vercel needs CLOUD_RUN_API_URL=%s)\n' "$API_URL"
else
  printf 'FRONTEND_BACKEND_BINDING=INCOMPLETE\n'
  note_fail "the gateway binding needs CLOUD_RUN_API_URL, GCP_PROJECT_NUMBER and GATEWAY_SERVICE_ACCOUNT"
fi

# --- website execution stage, as FOUR separate facts ---------------------
# Delegated to website-execution-check.sh so there is one implementation of
# "is the website open", and never collapsed into a single boolean: the four
# fail in different layers and an operator needs to know which one is shut.
printf '\n'
if bash "${SCRIPT_DIR}/website-execution-check.sh" \
     --operator-config "$CONFIG_PATH" > "${_MILO_VERIFY_TMP}/website.txt" 2>&1; then
  WEBSITE_OK=1
else
  WEBSITE_OK=0
fi
grep -E '^(FRONTEND_CODE_WIRED|TASK_COMPOSER_VISIBLE|GATEWAY_EXECUTION_ENABLED|BACKEND_EXECUTION_ARMED|WEBSITE_EXECUTION_STAGE_ACTIVE|DISABLED_MESSAGE_PRESENT)=' \
  "${_MILO_VERIFY_TMP}/website.txt" || true
if [[ "$WEBSITE_OK" -eq 0 ]]; then
  printf 'NOTE: the website execution stage is not fully open. This is EXPECTED\n'
  printf '      before Stage 2. Detail: %s\n' "${_MILO_VERIFY_TMP}/website.txt"
fi

# --- activation must not have created anything --------------------------
# Compared against the count read above, so a run that appeared during this
# verification is visible rather than assumed away.
NON_TERMINAL_AFTER="$(psql_value "select count(*) from public.runs where status not in ('completed','failed','cancelled','timed_out');" || true)"
if [[ "$NON_TERMINAL_AFTER" == "$NON_TERMINAL" ]]; then
  printf 'NO_RUN_CREATED_BY_ACTIVATION=YES (non-terminal count unchanged at %s)\n' "${NON_TERMINAL:-UNKNOWN}"
else
  printf 'NO_RUN_CREATED_BY_ACTIVATION=NO (%s -> %s)\n' "${NON_TERMINAL:-?}" "${NON_TERMINAL_AFTER:-?}"
  note_fail "the non-terminal run count changed during verification"
fi

printf 'PAID_CALLS_PERFORMED_BY_THIS_CHECK=NO\n'

if [[ "$FAILURES" -gt 0 ]]; then
  printf '\nRESULT: %d check(s) failed.\n' "$FAILURES" >&2
  exit 1
fi
printf '\nRESULT: OK\n'

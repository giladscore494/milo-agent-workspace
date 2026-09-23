#!/usr/bin/env bash
# Read-only production verification of the Mapping Plan -> prepared batch ->
# Swarm V2 execution path.
#
# Answers the exact questions each activation decision turns on, as separate
# facts an operator can paste into a record. It performs ZERO paid provider
# calls: every check is a describe, a read-only SQL SELECT, or an
# unauthenticated GET of the website, and it never creates a run.
#
# THE FACTS (each VERIFIED, NO, DISABLED or UNVERIFIED -- never a guess):
#
#   CODE_DEPLOYED          API + worker run the release image, agree on
#                          MILO_RELEASE_SHA, and the worker image exists
#   DATABASE_READY         the EXACT local migration set is applied (not a
#                          count), and the path's tables/RPCs/grants exist
#   EVIDENCE_READY         the named plan revision was prepared from active,
#                          complete, fully normalized SCOPED Government
#                          snapshots of verified register spellings
#   BATCH_READY            its batches are linked exactly and the next one can
#                          start (no live batch, not paused)
#   RUNS_QUIESCENT         no non-terminal run exists
#   GATEWAY_ENABLED        the running gateway proxies execution routes
#   WEBSITE_ENABLED        the served build has the execution UI and is the release
#   PAID_EXECUTION_READY   the backend is armed, the provider credential is
#                          bound to the worker, quota + RuntimePolicy resolve
#
# A generic "some active Government snapshot exists" is NOT readiness for a
# scoped batch and is reported only as REGISTER_SNAPSHOTS_ACTIVE (informational).
#
# GATES (--gate): which facts must be VERIFIED for exit 0.
#   database   DATABASE_READY (before a deploy: the code needs this schema)
#   deployed   CODE_DEPLOYED, DATABASE_READY
#   prepared   deployed + EVIDENCE_READY, BATCH_READY, RUNS_QUIESCENT
#   active     prepared + GATEWAY_ENABLED, WEBSITE_ENABLED, PAID_EXECUTION_READY

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
MILO_REPO_ROOT="$REPO_ROOT"
# shellcheck source=operator-config.sh
source "${SCRIPT_DIR}/operator-config.sh"
# shellcheck source=deployment-contract.sh
source "${SCRIPT_DIR}/deployment-contract.sh"

MILO_OPERATOR_CONFIG_PATH="" EXPECTED_SHA="" GATE="active"
WS_ARGS=()

usage() {
  cat << 'EOF'
Usage: production-verify.sh [options]

Read-only verification of the deployed production state. Makes no paid call
and creates no run.

Options:
  --gate database|deployed|prepared|active
                     Which facts must be VERIFIED for exit 0 (default: active).
                     database: the exact migration set and the path's schema,
                     before anything is deployed.
  --work-scope-id <uuid> --work-scope-revision <n> --work-scope-digest <hex>
                     The Mapping Plan revision the first batch run will use.
                     Required for EVIDENCE_READY / BATCH_READY to be VERIFIED.
  --expected-sha <sha>
                     Release SHA to compare against (default: HEAD).
  --operator-config <path>
  --help

Exits 0 only when every fact the gate requires is VERIFIED.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --operator-config) MILO_OPERATOR_CONFIG_PATH="${2:?}"; shift 2 ;;
    --expected-sha) EXPECTED_SHA="${2:?}"; shift 2 ;;
    --gate) GATE="${2:?}"; shift 2 ;;
    --work-scope-id | --work-scope-revision | --work-scope-digest) WS_ARGS+=("$1" "${2:?}"); shift 2 ;;
    --help) usage; exit 0 ;;
    *) printf 'FAIL: unknown argument %s\n' "$1" >&2; exit 2 ;;
  esac
done
case "$GATE" in
  database) REQUIRED=(DATABASE_READY) ;;
  deployed) REQUIRED=(CODE_DEPLOYED DATABASE_READY) ;;
  prepared) REQUIRED=(CODE_DEPLOYED DATABASE_READY EVIDENCE_READY BATCH_READY RUNS_QUIESCENT) ;;
  active) REQUIRED=(CODE_DEPLOYED DATABASE_READY EVIDENCE_READY BATCH_READY RUNS_QUIESCENT
                    GATEWAY_ENABLED WEBSITE_ENABLED PAID_EXECUTION_READY) ;;
  *) printf 'FAIL: --gate must be database, deployed, prepared or active\n' >&2; exit 2 ;;
esac

CONFIG_PATH="$(milo_operator_config_path "$REPO_ROOT" "$MILO_OPERATOR_CONFIG_PATH")"
milo_load_operator_config "$CONFIG_PATH" || exit 2
milo_require_op GCP_PROJECT_ID GCP_REGION CLOUD_RUN_API_SERVICE CLOUD_RUN_WORKER_JOB \
  ARTIFACT_REGISTRY_REPOSITORY || exit 2

PROJECT_ID="$(milo_op GCP_PROJECT_ID)"
REGION="$(milo_op GCP_REGION)"
API_SERVICE="$(milo_op CLOUD_RUN_API_SERVICE)"
WORKER_JOB="$(milo_op CLOUD_RUN_WORKER_JOB)"
LOCAL_SHA="$(git -C "$REPO_ROOT" rev-parse HEAD)"
[[ -n "$EXPECTED_SHA" ]] || EXPECTED_SHA="$LOCAL_SHA"
WORKER_IMAGE_EXPECTED="${REGION}-docker.pkg.dev/${PROJECT_ID}/$(milo_op ARTIFACT_REGISTRY_REPOSITORY)/${MILO_WORKER_IMAGE_REPO}:${EXPECTED_SHA}"

_MILO_VERIFY_TMP="$(mktemp -d "${TMPDIR:-/tmp}/milo-verify.XXXXXX")"
trap 'rm -rf "${_MILO_VERIFY_TMP}"' EXIT

declare -A FACTS=()
fact() {
  FACTS["$1"]="$2"
  printf '%s=%s%s\n' "$1" "$2" "${3:+ (${3})}"
}

printf 'GATE=%s\nEXPECTED_RELEASE_SHA=%s\nCURRENT_LOCAL_SHA=%s\n\n' "$GATE" "$EXPECTED_SHA" "$LOCAL_SHA"

# --- CODE_DEPLOYED ------------------------------------------------------
describe_value() {
  # describe_value service|job FORMAT -> the value, or nonzero on failure
  if [[ "$1" == "service" ]]; then
    gcloud run services describe "$API_SERVICE" --region "$REGION" --project "$PROJECT_ID" \
      --format="$2" 2> /dev/null
  else
    gcloud run jobs describe "$WORKER_JOB" --region "$REGION" --project "$PROJECT_ID" \
      --format="$2" 2> /dev/null
  fi
}
env_format() {
  if [[ "$1" == "service" ]]; then
    printf 'value(spec.template.spec.containers[0].env.filter("name:%s").extract("value"))' "$2"
  else
    printf 'value(spec.template.spec.template.spec.containers[0].env.filter("name:%s").extract("value"))' "$2"
  fi
}

if ! command -v gcloud > /dev/null 2>&1; then
  fact CODE_DEPLOYED UNVERIFIED "gcloud is unavailable"
elif ! API_IMAGE="$(describe_value service 'value(spec.template.spec.containers[0].image)')" \
     || ! WORKER_IMAGE="$(describe_value job 'value(spec.template.spec.template.spec.containers[0].image)')"; then
  fact CODE_DEPLOYED UNVERIFIED "the API service or the worker job could not be described"
else
  API_RELEASE="$(describe_value service "$(env_format service MILO_RELEASE_SHA)" | tr -d '[]' || true)"
  WORKER_RELEASE="$(describe_value job "$(env_format job MILO_RELEASE_SHA)" | tr -d '[]' || true)"
  printf 'DEPLOYED_API_IMAGE=%s\nDEPLOYED_WORKER_IMAGE=%s\n' "${API_IMAGE:-<none>}" "${WORKER_IMAGE:-<none>}"
  printf 'API_MILO_RELEASE_SHA=%s\nWORKER_MILO_RELEASE_SHA=%s\n' "${API_RELEASE:-<unset>}" "${WORKER_RELEASE:-<unset>}"
  problems=""
  [[ "${API_IMAGE##*:}" == "$EXPECTED_SHA" ]] || problems+="the API image is not tagged ${EXPECTED_SHA}; "
  [[ "${WORKER_IMAGE##*:}" == "$EXPECTED_SHA" ]] || problems+="the worker image is not tagged ${EXPECTED_SHA}; "
  [[ "$API_RELEASE" == "$EXPECTED_SHA" && "$WORKER_RELEASE" == "$EXPECTED_SHA" ]] \
    || problems+="MILO_RELEASE_SHA is not ${EXPECTED_SHA} on both surfaces; "
  if [[ -n "$problems" ]]; then
    fact CODE_DEPLOYED NO "${problems%; }"
  elif ! gcloud artifacts docker images describe "$WORKER_IMAGE_EXPECTED" --project "$PROJECT_ID" \
         > /dev/null 2>&1; then
    fact CODE_DEPLOYED UNVERIFIED "the worker image ${WORKER_IMAGE_EXPECTED} could not be described in Artifact Registry"
  else
    fact CODE_DEPLOYED VERIFIED "API and worker run ${EXPECTED_SHA}; the worker image exists"
  fi
fi

# --- DATABASE_READY: the EXACT migration set, then the path's schema ------
DB_ENV="$(milo_op READONLY_DATABASE_URL_ENV)"
state_status=0
bash "${REPO_ROOT}/scripts/release/check-migration-state.sh" --database-url-env "${DB_ENV:-MILO_READONLY_DB_URL}" \
  --json-output "${_MILO_VERIFY_TMP}/migrations.json" > "${_MILO_VERIFY_TMP}/migrations.txt" 2>&1 || state_status=$?
MIGRATION_STATE="$(python3 - "${_MILO_VERIFY_TMP}/migrations.json" << 'PY' 2> /dev/null || true
import json, re, sys
try:
    report = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    print("UNVERIFIED|the migration-state report could not be read"); sys.exit(0)
checks = {c.get("name"): c for c in report.get("checks", [])}
state = checks.get("remote:state") or {}
missing = (checks.get("remote:missing") or {}).get("detail", "")
detail = state.get("detail", "")
match = re.search(r"classified as ([a-z-]+)", detail)
if state.get("status") == "PASS" and match and match.group(1) == "fully-migrated" \
        and report.get("summary", {}).get("blocked", 1) == 0:
    print("VERIFIED|" + detail)
elif match or state.get("status") == "BLOCKED":
    print("NO|" + (missing or detail))
else:
    print("UNVERIFIED|" + (detail or "no read-only database connection; remote migration state was not inspected"))
PY
)"
[[ -n "$MIGRATION_STATE" ]] || MIGRATION_STATE="UNVERIFIED|check-migration-state.sh produced no report (exit ${state_status})"
grep -E '^\[(PASS|WARN|BLOCKED|MANUAL)\] remote:' "${_MILO_VERIFY_TMP}/migrations.txt" | sed 's/^/  /' || true
schema_status=0
bash "${SCRIPT_DIR}/work-scope-readiness.sh" --operator-config "$CONFIG_PATH" --schema-only \
  > "${_MILO_VERIFY_TMP}/schema.txt" 2>&1 || schema_status=$?
grep -E '^(DATABASE_READ|WORK_SCOPE_SCHEMA)=' "${_MILO_VERIFY_TMP}/schema.txt" | sed 's/^/  /' || true
case "${MIGRATION_STATE%%|*}|${schema_status}" in
  "VERIFIED|0") fact DATABASE_READY VERIFIED "every local migration applied; the batch path's tables, RPCs and grants are in place" ;;
  NO\|* | *\|1) fact DATABASE_READY NO "${MIGRATION_STATE#*|}" ;;
  *) fact DATABASE_READY UNVERIFIED "${MIGRATION_STATE#*|}" ;;
esac

# --- EVIDENCE_READY / BATCH_READY: the named plan revision -----------------
if [[ "${#WS_ARGS[@]}" -eq 0 ]]; then
  fact EVIDENCE_READY UNVERIFIED "no plan revision named (--work-scope-id/--work-scope-revision/--work-scope-digest); a generic snapshot is never readiness"
  fact BATCH_READY UNVERIFIED "no plan revision named"
else
  scope_status=0
  bash "${SCRIPT_DIR}/work-scope-readiness.sh" --operator-config "$CONFIG_PATH" "${WS_ARGS[@]}" \
    > "${_MILO_VERIFY_TMP}/scope.txt" 2>&1 || scope_status=$?
  grep -vE '^(EVIDENCE_READY|BATCH_READY|WORK_SCOPE_READINESS)=' "${_MILO_VERIFY_TMP}/scope.txt" | sed 's/^/  /' || true
  for name in EVIDENCE_READY BATCH_READY; do
    line="$(grep -E "^${name}=" "${_MILO_VERIFY_TMP}/scope.txt" | head -n 1 || true)"
    value="${line#*=}"
    value="${value%% *}"
    detail="${line#* (}"
    detail="${detail%)}"
    [[ "$line" == *" ("* ]] || detail=""
    case "$value" in
      VERIFIED | NO) fact "$name" "$value" "$detail" ;;
      *) fact "$name" UNVERIFIED "${detail:-the scoped readiness check did not reach this fact (exit ${scope_status})}" ;;
    esac
  done
fi

# --- informational: the whole-register snapshot (NOT batch readiness) ----
DB_URL=""
[[ -n "$DB_ENV" && "$DB_ENV" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] && DB_URL="${!DB_ENV:-}"
psql_value() {
  [[ -n "$DB_URL" ]] && command -v psql > /dev/null 2>&1 || return 1
  psql "$DB_URL" -X -At -v ON_ERROR_STOP=1 -c "$1" 2> /dev/null
}
if register="$(psql_value "select count(*) from public.catalog_source_snapshots s
     where s.source_family = 'government' and s.activated_at is not null
       and not (s.retrieval_metadata ? 'capture_scope');")"; then
  printf 'REGISTER_SNAPSHOTS_ACTIVE=%s (informational only: a batch run pins its own scoped snapshot, never the register)\n' "$register"
else
  printf 'REGISTER_SNAPSHOTS_ACTIVE=UNVERIFIED (informational only)\n'
fi

# --- RUNS_QUIESCENT -------------------------------------------------------
if NON_TERMINAL="$(psql_value "select count(*) from public.runs where status not in ('completed','partial_success','failed','cancelled','timed_out','budget_exhausted');")" \
   && [[ "$NON_TERMINAL" =~ ^[0-9]+$ ]]; then
  if [[ "$NON_TERMINAL" -eq 0 ]]; then
    fact RUNS_QUIESCENT VERIFIED "no non-terminal run"
  else
    # Name them (at most five): a queued operator_capture run is a prepared
    # capture that was never claimed -- resume it by its id, never prepare
    # another; a product run is resolved through the run lifecycle tools.
    listing="$(psql_value "select string_agg(format('%s(%s,%s,%s)', id, coalesce(run_identity->>'workflow_key','legacy'), status, coalesce(launch_state,'')), ' ')
         from (select id, run_identity, status, launch_state from public.runs
                where status not in ('completed','partial_success','failed','cancelled','timed_out','budget_exhausted')
                order by created_at limit 5) r;" || true)"
    fact RUNS_QUIESCENT NO "${NON_TERMINAL} non-terminal run(s): ${listing:-unlisted}; resolve them before opening the website"
  fi
else
  NON_TERMINAL=""
  fact RUNS_QUIESCENT UNVERIFIED "the runs table could not be read with the read-only connection"
fi

# --- GATEWAY / WEBSITE / backend, from the one website check ------------
# Its exit status only says "not every fact VERIFIED"; each fact is read below.
bash "${SCRIPT_DIR}/website-execution-check.sh" --operator-config "$CONFIG_PATH" \
  --expected-sha "$EXPECTED_SHA" "${WS_ARGS[@]}" > "${_MILO_VERIFY_TMP}/website.txt" 2>&1 || true
sed 's/^/  /' "${_MILO_VERIFY_TMP}/website.txt"
website_fact() {
  local line value
  line="$(grep -E "^$1=" "${_MILO_VERIFY_TMP}/website.txt" | head -n 1 || true)"
  value="${line#*=}"
  printf '%s' "${value%% *}"
}
gateway="$(website_fact GATEWAY_EXECUTION_ENABLED)"
binding="$(website_fact GATEWAY_BACKEND_BINDING)"
case "${gateway}|${binding}" in
  "VERIFIED|VERIFIED") fact GATEWAY_ENABLED VERIFIED "execution routes on, and the gateway reaches the API" ;;
  DISABLED\|*) fact GATEWAY_ENABLED DISABLED "GATEWAY_ALLOW_EXECUTION_ROUTES is off" ;;
  *\|NO) fact GATEWAY_ENABLED NO "the gateway cannot reach the API" ;;
  *) fact GATEWAY_ENABLED UNVERIFIED "gateway=${gateway:-?} binding=${binding:-?}" ;;
esac
ui="$(website_fact TASK_COMPOSER_VISIBLE)"
release="$(website_fact FRONTEND_RELEASE)"
case "${ui}|${release}" in
  "VERIFIED|VERIFIED") fact WEBSITE_ENABLED VERIFIED "the served build is the release, with the execution UI on" ;;
  DISABLED\|*) fact WEBSITE_ENABLED DISABLED "the served build has the execution UI off (rebuild required)" ;;
  *\|NO) fact WEBSITE_ENABLED NO "the served build is not the release commit" ;;
  *) fact WEBSITE_ENABLED UNVERIFIED "ui=${ui:-?} release=${release:-?}" ;;
esac

# --- PAID_EXECUTION_READY -------------------------------------------------
backend="$(website_fact BACKEND_EXECUTION_ARMED)"
paid_problems="" paid_unverified=""
case "$backend" in
  VERIFIED) ;;
  NO | DISABLED) paid_problems+="the API/worker are not armed for Stage 2; " ;;
  *) paid_unverified+="the backend flags could not be read; " ;;
esac
if python3 "${REPO_ROOT}/scripts/release/runtime_policy_manifest.py" --format text \
     --live-from-cloud-run --project "$PROJECT_ID" --region "$REGION" \
     --worker-job "$WORKER_JOB" > /dev/null 2>&1; then
  printf 'RUNTIME_POLICY_RESOLVED=YES\n'
else
  printf 'RUNTIME_POLICY_RESOLVED=NO_OR_UNREADABLE\n'
  paid_problems+="a mandatory-for-paid RuntimePolicy dimension is unbound or unreadable (scripts/release/runtime_policy_manifest.py); "
fi
PROVIDER_SECRET="$(milo_op SECRET_PROVIDER_API_KEY)"
if [[ -n "$PROVIDER_SECRET" ]] && gcloud secrets describe "$PROVIDER_SECRET" --project "$PROJECT_ID" > /dev/null 2>&1; then
  printf 'PROVIDER_CREDENTIAL_PRESENT=YES (%s exists; value never read)\n' "$PROVIDER_SECRET"
else
  printf 'PROVIDER_CREDENTIAL_PRESENT=UNVERIFIED\n'
  paid_unverified+="the provider secret could not be described; "
fi
if gcloud secrets describe "$(milo_op SECRET_REDIS_URL)" --project "$PROJECT_ID" > /dev/null 2>&1 \
   && gcloud secrets describe "$(milo_op SECRET_REDIS_TOKEN)" --project "$PROJECT_ID" > /dev/null 2>&1; then
  printf 'SHARED_QUOTA_READY=YES (both Upstash secrets exist)\n'
else
  printf 'SHARED_QUOTA_READY=UNVERIFIED\n'
  paid_unverified+="the shared quota/rate-limit secrets could not both be described; "
fi
if [[ -n "$paid_problems" ]]; then
  fact PAID_EXECUTION_READY NO "${paid_problems%; }"
elif [[ -n "$paid_unverified" ]]; then
  fact PAID_EXECUTION_READY UNVERIFIED "${paid_unverified%; }"
else
  fact PAID_EXECUTION_READY VERIFIED "backend armed, provider credential bound to the worker only, quota and RuntimePolicy resolved"
fi

# --- this check must not have created anything --------------------------
if [[ -n "${NON_TERMINAL}" ]]; then
  after="$(psql_value "select count(*) from public.runs where status not in ('completed','partial_success','failed','cancelled','timed_out','budget_exhausted');" || true)"
  if [[ "$after" == "$NON_TERMINAL" ]]; then
    printf 'NO_RUN_CREATED_BY_VERIFICATION=YES (non-terminal count unchanged at %s)\n' "$NON_TERMINAL"
  else
    printf 'NO_RUN_CREATED_BY_VERIFICATION=NO (%s -> %s)\n' "$NON_TERMINAL" "${after:-?}"
    FACTS[RUNS_QUIESCENT]="NO"
  fi
fi
printf 'PAID_CALLS_PERFORMED_BY_THIS_CHECK=NO\n'

# --- the verdict ----------------------------------------------------------
printf '\n== VERDICT (gate: %s) ==\n' "$GATE"
missing=()
for name in CODE_DEPLOYED DATABASE_READY EVIDENCE_READY BATCH_READY RUNS_QUIESCENT \
            GATEWAY_ENABLED WEBSITE_ENABLED PAID_EXECUTION_READY; do
  value="${FACTS[$name]:-UNVERIFIED}"
  marker=" "
  if milo_contains "$name" "${REQUIRED[@]}"; then
    marker="*"
    [[ "$value" == "VERIFIED" ]] || missing+=("${name}=${value}")
  fi
  printf ' %s %-22s %s\n' "$marker" "$name" "$value"
done
printf '   (* required by this gate)\n'
if [[ "${#missing[@]}" -gt 0 ]]; then
  printf '\nRESULT: NOT READY for gate %s: %s\n' "$GATE" "${missing[*]}" >&2
  exit 1
fi
printf '\nRESULT: OK — every fact gate %s requires is VERIFIED.\n' "$GATE"

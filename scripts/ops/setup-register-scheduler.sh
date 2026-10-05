#!/usr/bin/env bash
# ONE-TIME, in Cloud Shell (PR-SYNC-2): the register sync's hourly tick.
#   --plan (default)  read-only: every change --apply would make (ENABLE /
#                     CREATE / BIND / UPDATE), or OK when already in place.
#   --apply           exactly those changes (idempotent), then --check.
#   --check           read-only, for the deploy preflight; it ends with
#                     SUMMARY|scheduler|PASS|GAP|FAIL|UNREADABLE|<detail>.
# Sets up: the Cloud Scheduler API; the account milo-register-scheduler (no
# keys: a user-managed key is FAIL); roles/run.invoker for it on the API service
# ONLY (any other grant on the project or another service is FAIL); the job
# milo-register-sync-tick: hourly at :07 UTC, POST <API URL>/internal/register/
# sync-tick, OIDC token as that account for MILO_GATEWAY_AUDIENCE, 60 s attempt
# deadline, no retries. Prints no secret and no human account's address.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

MODE="plan" JOB_NAME="milo-register-sync-tick" ROUTE="/internal/register/sync-tick"
SCHEDULE="7 * * * *" TIME_ZONE="Etc/UTC" DEADLINE="60s" INVOKER="roles/run.invoker"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --plan | --apply | --check) MODE="${1#--}"; shift ;;
    --operator-config) MILO_OPERATOR_CONFIG_PATH="${2:?}"; shift 2 ;;
    *) printf 'Usage: setup-register-scheduler.sh [--plan | --apply | --check] [--operator-config <path>]\n' >&2
       exit 2 ;;
  esac
done

ops_load_config
AUDIENCE="$(milo_op MILO_GATEWAY_AUDIENCE)"
SA_ID="$(ops_register_scheduler_account_id)"
SA="$(ops_register_scheduler_identity)"
MEMBER="serviceAccount:${SA}"
if [[ -z "$AUDIENCE" ]]; then
  [[ "$MODE" == "check" ]] && { summary scheduler GAP "MILO_GATEWAY_AUDIENCE is not configured"; exit 0; }
  printf 'FAIL: MILO_GATEWAY_AUDIENCE is empty in %s (the tick token is minted for it).\n' "$CONFIG_PATH" >&2
  exit 2
fi

# What differs between the job and what it must be, as words (then "paused").
DRIFT_PY='
import json, sys
doc, (uri, account, audience, schedule, deadline) = json.load(sys.stdin), sys.argv[1:]
target = doc.get("httpTarget") or {}
token = target.get("oidcToken") or {}
checks = {"schedule": doc.get("schedule") == schedule, "time-zone": doc.get("timeZone") in ("Etc/UTC", "UTC"),
          "url": target.get("uri") == uri, "method": target.get("httpMethod") == "POST",
          "token-identity": token.get("serviceAccountEmail") == account, "audience": token.get("audience") == audience,
          "deadline": doc.get("attemptDeadline") == deadline,
          "retries": int((doc.get("retryConfig") or {}).get("retryCount") or 0) == 0,
          "paused": doc.get("state") != "PAUSED"}
print(" ".join(name for name, ok in checks.items() if not ok))
'
# Roles MEMBER holds in an IAM policy document, one per line (never printed with the member).
ROLES_PY='
import json, sys
for binding in json.load(sys.stdin).get("bindings") or []:
    if sys.argv[1] in (binding.get("members") or []):
        print(binding.get("role"))
'
denied() { grep -qE 'PERMISSION_DENIED|HTTPError 403' "$1"; }
api_url() {
  gcloud run services describe "$API_SERVICE" --region "$REGION" --project "$PROJECT_ID" \
    --format='value(status.url)' 2> /dev/null
}
job_drift() {  # job_drift ERRFILE URI -- the drift words; exit 1 when the job cannot be described
  local json
  json="$(gcloud scheduler jobs describe "$JOB_NAME" --location "$REGION" --project "$PROJECT_ID" \
    --format=json 2> "$1")" || return 1
  python3 -c "$DRIFT_PY" "$2" "$SA" "$AUDIENCE" "$SCHEDULE" "$DEADLINE" <<< "$json"
}
roles_on() {  # roles_on ERRFILE SERVICE -- exit 2 when the policy cannot be read
  local policy
  policy="$(gcloud run services get-iam-policy "$2" --region "$REGION" --project "$PROJECT_ID" \
    --format=json 2> "$1")" || return 2
  python3 -c "$ROLES_PY" "$MEMBER" <<< "$policy"
}
extra_grants() {  # every grant but invoker on the API, as scope:role
  local policy name
  policy="$(gcloud projects get-iam-policy "$PROJECT_ID" --format=json 2> /dev/null)" || return 1
  python3 -c "$ROLES_PY" "$MEMBER" <<< "$policy" | sed 's/^/project:/'
  for name in $(gcloud run services list --region "$REGION" --project "$PROJECT_ID" \
                  --format='value(metadata.name)' 2> /dev/null); do
    roles_on /dev/null "$name" > "$TMP.roles" || return 1
    [[ "$name" != "$API_SERVICE" ]] || sed -i "\|^${INVOKER}\$|d" "$TMP.roles"
    sed "s|^|service/${name}:|" "$TMP.roles"
  done
}
TMP="$(mktemp)"
trap 'rm -f "$TMP" "$TMP.roles"' EXIT

if [[ "$MODE" == "check" ]]; then
  verdict() { summary scheduler "$1" "$2"; exit 0; }
  verify="Verify from Cloud Shell: bash scripts/ops/setup-register-scheduler.sh --check"
  url="$(api_url || true)"
  [[ -n "$url" ]] || verdict UNREADABLE "the API service URL is not readable by this identity. ${verify}"
  if ! drift="$(job_drift "$TMP" "${url}${ROUTE}")"; then
    denied "$TMP" && verdict UNREADABLE "the job ${JOB_NAME} is not readable by this identity. ${verify}"
    verdict GAP "the job ${JOB_NAME} does not exist. Run: bash scripts/ops/setup-register-scheduler.sh --apply"
  fi
  [[ -z "$drift" ]] && summary job PASS "${JOB_NAME}: hourly at :07 UTC, POST ${url}${ROUTE}, OIDC as ${SA_ID}" \
    || summary job GAP "${JOB_NAME} differs in: ${drift}"
  status=0
  roles="$(roles_on "$TMP" "$API_SERVICE")" || status=$?
  [[ "$status" -eq 0 ]] || verdict UNREADABLE "the IAM policy of ${API_SERVICE} is not readable by this identity. ${verify}"
  grep -qxF "$INVOKER" <<< "$roles" && summary invoker PASS "${SA_ID} holds ${INVOKER} on ${API_SERVICE}" \
    || { summary invoker GAP "${SA_ID} does not hold ${INVOKER} on ${API_SERVICE}"; drift="${drift} invoker"; }
  extra="$(extra_grants)" || verdict UNREADABLE "the project or service IAM policies are not readable by this identity. ${verify}"
  keys="$(gcloud iam service-accounts keys list --iam-account "$SA" --project "$PROJECT_ID" --managed-by user \
    --format='value(name)' 2> /dev/null | grep -c . || true)"
  [[ -z "$extra" ]] || verdict FAIL "${SA_ID} holds more than ${INVOKER} on ${API_SERVICE}: $(paste -sd, - <<< "$extra")"
  [[ "${keys:-0}" -eq 0 ]] || verdict FAIL "${SA_ID} has ${keys} user-managed key(s); it must have none"
  [[ -z "${drift// /}" ]] || verdict GAP "the tick is not fully set up (above). Run: bash scripts/ops/setup-register-scheduler.sh --apply"
  verdict PASS "the tick reaches ${API_SERVICE} hourly as ${SA_ID}, which holds ${INVOKER} on it only"
fi

milo_require_gcloud_context "$PROJECT_ID" > /dev/null || exit 2  # its lines name the operator's account
summary_header "Register sync tick: ${JOB_NAME} (project ${PROJECT_ID}, ${MODE})"
step() {  # step NAME VERB DETAIL CMD... -- print, and run only in --apply
  local name="$1" verb="$2" detail="$3"
  shift 3
  printf '%-7s %s\n' "$verb" "$detail"
  [[ "$MODE" == "apply" ]] || { summary "$name" DRY-RUN "would ${verb}: ${detail}"; return 0; }
  # gcloud's own output can quote an account address: never shown.
  "$@" > /dev/null 2>&1 || { summary "$name" FAIL "${detail} did not apply"; exit 1; }
  summary "$name" PASS "${verb}: ${detail}"
}
ok() { printf 'OK      %s\n' "$2"; summary "$1" PASS "$2"; }

if [[ -n "$(gcloud services list --enabled --project "$PROJECT_ID" \
              --filter=config.name=cloudscheduler.googleapis.com --format='value(config.name)' 2> /dev/null)" ]]; then
  ok api "cloudscheduler.googleapis.com is enabled"
else
  step api ENABLE "cloudscheduler.googleapis.com" gcloud services enable cloudscheduler.googleapis.com --project "$PROJECT_ID"
fi
if gcloud iam service-accounts describe "$SA" --project "$PROJECT_ID" > /dev/null 2>&1; then
  ok account "service account ${SA_ID} exists"
else
  step account CREATE "service account ${SA_ID} (no keys)" \
    gcloud iam service-accounts create "$SA_ID" --project "$PROJECT_ID" --display-name "MILO register sync scheduler"
fi
if (roles_on "$TMP" "$API_SERVICE" || true) | grep -qxF "$INVOKER"; then
  ok invoker "${SA_ID} holds ${INVOKER} on ${API_SERVICE}"
else
  step invoker BIND "${INVOKER} for ${SA_ID} on ${API_SERVICE} only" gcloud run services add-iam-policy-binding \
    "$API_SERVICE" --region "$REGION" --project "$PROJECT_ID" --member "$MEMBER" --role "$INVOKER"
fi
url="$(api_url || true)"
[[ -n "$url" ]] || { summary job FAIL "the URL of ${API_SERVICE} could not be read"; exit 1; }
[[ "$AUDIENCE" == "$url" ]] || printf 'WARN    MILO_GATEWAY_AUDIENCE is not the API URL: Cloud Run admits the token only if the service lists it as a custom audience\n'
flags=(--location "$REGION" --project "$PROJECT_ID" --schedule "$SCHEDULE" --time-zone "$TIME_ZONE"
  --uri "${url}${ROUTE}" --http-method POST --oidc-service-account-email "$SA" --oidc-token-audience "$AUDIENCE"
  --attempt-deadline "$DEADLINE" --max-retry-attempts 0)
wanted="${JOB_NAME}: hourly at :07 UTC, POST ${url}${ROUTE}, OIDC as ${SA_ID}, ${DEADLINE}, no retries"
if ! drift="$(job_drift "$TMP" "${url}${ROUTE}")"; then
  step job CREATE "$wanted" gcloud scheduler jobs create http "$JOB_NAME" "${flags[@]}"
elif [[ -n "$drift" ]]; then
  step job UPDATE "${JOB_NAME} (${drift})" gcloud scheduler jobs update http "$JOB_NAME" "${flags[@]}"
  [[ "$drift" != *paused* || "$MODE" != "apply" ]] \
    || gcloud scheduler jobs resume "$JOB_NAME" --location "$REGION" --project "$PROJECT_ID" > /dev/null 2>&1
else
  ok job "$wanted"
fi

if [[ "$MODE" == "apply" ]]; then
  result="$(bash "$0" --check --operator-config "$CONFIG_PATH")"
  printf '%s\n' "$result"
  grep -q '^SUMMARY|scheduler|PASS|' <<< "$result" || { summary setup FAIL "the tick did not read back as set up (above)"; exit 1; }
  summary setup PASS "set up; next: turn Auto sync on in the Register page"
else
  printf '\nPLAN ONLY -- nothing was changed. Re-run with --apply.\n'
  summary setup INFO "plan only; nothing was changed"
fi

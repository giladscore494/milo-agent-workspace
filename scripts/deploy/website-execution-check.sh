#!/usr/bin/env bash
# Read-only proof of where the website execution chain is currently open.
#
# It reports SEPARATE facts, deliberately never collapsed into one "frontend
# ready" boolean, because they fail in different places for different reasons
# and an operator debugging one needs to know which:
#
#   FRONTEND_CODE_WIRED        the checkout contains the canonical route
#   FRONTEND_RELEASE           the deployed website was built from the release
#   TASK_COMPOSER_VISIBLE      the SERVED build has the execution UI on
#   GATEWAY_EXECUTION_ENABLED  the running gateway proxies execution routes
#   GATEWAY_BACKEND_BINDING    the gateway reaches the Cloud Run API
#   BACKEND_EXECUTION_ARMED    every API + worker gate, read from Cloud Run
#   MAPPING_PLAN_BATCH_PATH    the named plan revision's next batch is ready
#
# THREE ANSWERS, NEVER TWO. Each fact is VERIFIED (proved from an actual value
# or the deployed website's observable behaviour), DISABLED (proved off) or
# UNVERIFIED (not proved either way: no access, a failed request, an answer
# that did not match the contract, two sources that disagree). A misconfigured
# value is NO. Only when EVERY fact is VERIFIED is the stage active; missing
# access, a failed request or an inconclusive probe never becomes a YES.
#
# IT NEVER SENDS A POST, a request body, a credential or a run-creation
# request. The gateway is probed with ONE unauthenticated GET of an
# execution-only route (a proposal read, nil id): with execution routes OFF the
# gateway answers 403 by policy before authentication; with them ON it gets as
# far as authentication and answers 401. Neither can create or read anything.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
MILO_REPO_ROOT="$REPO_ROOT"
# shellcheck source=operator-config.sh
source "${SCRIPT_DIR}/operator-config.sh"
# shellcheck source=deployment-contract.sh
source "${SCRIPT_DIR}/deployment-contract.sh"

MILO_OPERATOR_CONFIG_PATH="" SITE_URL="" SKIP_REMOTE=0 EXPECTED_SHA=""
WS_ARGS=()

usage() {
  cat << 'EOF'
Usage: website-execution-check.sh [options]

Read-only. Reports each website-execution fact separately as VERIFIED,
DISABLED, NO or UNVERIFIED, and never sends a POST or a credential.

Options:
  --operator-config <path>  Operator identifier file.
  --site-url <url>          Production website origin (default: PRODUCTION_ORIGIN).
  --expected-sha <sha>      Release the website must be built from (default: HEAD).
  --work-scope-id <uuid> --work-scope-revision <n> --work-scope-digest <hex>
                            The Mapping Plan revision whose next batch the first
                            run will start. Without them MAPPING_PLAN_BATCH_PATH
                            is UNVERIFIED and the stage is not active.
  --offline                 Repository-side checks only; make no network call.
  --help

Exits 0 only when every fact is VERIFIED.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --operator-config) MILO_OPERATOR_CONFIG_PATH="${2:?}"; shift 2 ;;
    --site-url) SITE_URL="${2:?}"; shift 2 ;;
    --expected-sha) EXPECTED_SHA="${2:?}"; shift 2 ;;
    --work-scope-id | --work-scope-revision | --work-scope-digest) WS_ARGS+=("$1" "${2:?}"); shift 2 ;;
    --offline) SKIP_REMOTE=1; shift ;;
    --help) usage; exit 0 ;;
    *) printf 'FAIL: unknown argument %s\n' "$1" >&2; exit 2 ;;
  esac
done

CONFIG_PATH="$(milo_operator_config_path "$REPO_ROOT" "$MILO_OPERATOR_CONFIG_PATH")"
milo_load_operator_config "$CONFIG_PATH" || exit 2
[[ -n "$SITE_URL" ]] || SITE_URL="$(milo_op PRODUCTION_ORIGIN)"
SITE_URL="${SITE_URL%/}"
[[ -n "$EXPECTED_SHA" ]] || EXPECTED_SHA="$(git -C "$REPO_ROOT" rev-parse HEAD 2> /dev/null || true)"
PROJECT_ID="$(milo_op GCP_PROJECT_ID)"
REGION="$(milo_op GCP_REGION)"
API_SERVICE="$(milo_op CLOUD_RUN_API_SERVICE)"
WORKER_JOB="$(milo_op CLOUD_RUN_WORKER_JOB)"
PROVIDER_SECRET="$(milo_op SECRET_PROVIDER_API_KEY)"

_MILO_CHECK_TMP="$(mktemp -d "${TMPDIR:-/tmp}/milo-website.XXXXXX")"
trap 'rm -rf "${_MILO_CHECK_TMP}"' EXIT

declare -A FACTS=()
fact() {
  # fact NAME VALUE [detail] — VALUE is VERIFIED | DISABLED | NO | UNVERIFIED
  FACTS["$1"]="$2"
  printf '%s=%s%s\n' "$1" "$2" "${3:+ (${3})}"
}

# ---------------------------------------------------------------------------
# 1. FRONTEND_CODE_WIRED — a property of this checkout, always checkable.
# ---------------------------------------------------------------------------
WIRED=VERIFIED
grep -q "/runs" "${REPO_ROOT}/frontend/lib/api.ts" 2> /dev/null || WIRED=NO
[[ -f "${REPO_ROOT}/frontend/components/conversation/TaskComposer.tsx" ]] || WIRED=NO
grep -q "catalog_batch_required" "${REPO_ROOT}/frontend/components/conversation/TaskComposer.tsx" 2> /dev/null || WIRED=NO
[[ -f "${REPO_ROOT}/frontend/app/api/gateway/[...path]/route.ts" ]] || WIRED=NO
[[ -f "${REPO_ROOT}/frontend/app/api/deployment-status/route.ts" ]] || WIRED=NO
grep -q "executionRoutesEnabled" "${REPO_ROOT}/frontend/lib/server/gatewayPolicy.ts" 2> /dev/null || WIRED=NO
fact FRONTEND_CODE_WIRED "$WIRED"

if [[ "$SKIP_REMOTE" -eq 1 ]]; then
  for name in FRONTEND_RELEASE TASK_COMPOSER_VISIBLE GATEWAY_EXECUTION_ENABLED \
              GATEWAY_BACKEND_BINDING BACKEND_EXECUTION_ARMED MAPPING_PLAN_BATCH_PATH; do
    fact "$name" UNVERIFIED "--offline"
  done
  printf 'WEBSITE_EXECUTION_STAGE_ACTIVE=UNVERIFIED (--offline)\n'
  exit 1
fi

# http GET URL -> writes body to $_MILO_CHECK_TMP/body, echoes the HTTP code
# ("000" on transport failure). GET only, no body, no credential, bounded.
http_get() {
  local code
  code="$(curl -sS -o "${_MILO_CHECK_TMP}/body" -w '%{http_code}' --max-time 20 \
    -H 'accept: application/json' "$1" 2> /dev/null)" || code="000"
  [[ "$code" =~ ^[0-9]{3}$ ]] || code="000"
  printf '%s' "$code"
}

# json_get FILE KEY -> the value as JSON text ('true', 'false', '"x"', 'null'),
# or nothing when the document is not a JSON object carrying KEY.
json_get() {
  python3 - "$1" "$2" << 'PY' 2> /dev/null || true
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        doc = json.load(handle)
except Exception:
    sys.exit(0)
if isinstance(doc, dict) and sys.argv[2] in doc:
    print(json.dumps(doc[sys.argv[2]]))
PY
}

# ---------------------------------------------------------------------------
# 2. What the SERVED website says it is: /api/deployment-status.
#
# NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI is inlined at BUILD time; the route
# references it literally, so it reports the value the served bundle was built
# with. A Vercel variable changed without a rebuild does not move it.
# ---------------------------------------------------------------------------
STATUS_UI="" STATUS_GATEWAY="" STATUS_SHA=""
if [[ -z "$SITE_URL" ]]; then
  fact FRONTEND_RELEASE UNVERIFIED "no --site-url and no PRODUCTION_ORIGIN"
  fact TASK_COMPOSER_VISIBLE UNVERIFIED "no website origin to read"
else
  code="$(http_get "${SITE_URL}/api/deployment-status")"
  contract="$(json_get "${_MILO_CHECK_TMP}/body" contract)"
  if [[ "$code" == "200" && "$contract" == '"milo-website-deployment/1"' ]]; then
    STATUS_UI="$(json_get "${_MILO_CHECK_TMP}/body" execution_ui)"
    STATUS_GATEWAY="$(json_get "${_MILO_CHECK_TMP}/body" gateway_execution_routes)"
    STATUS_SHA="$(json_get "${_MILO_CHECK_TMP}/body" commit_sha)"
    STATUS_SHA="${STATUS_SHA//\"/}"
  fi
  if [[ -z "$STATUS_UI" ]]; then
    fact FRONTEND_RELEASE UNVERIFIED "GET ${SITE_URL}/api/deployment-status answered HTTP ${code} without the deployment-status contract; the website predates this release, is unreachable or is behind deployment protection"
    fact TASK_COMPOSER_VISIBLE UNVERIFIED "the served build's execution UI value could not be read"
  else
    if [[ "$STATUS_SHA" =~ ^[0-9a-f]{40}$ && "$STATUS_SHA" == "$EXPECTED_SHA" ]]; then
      fact FRONTEND_RELEASE VERIFIED "built from ${STATUS_SHA}"
    elif [[ "$STATUS_SHA" =~ ^[0-9a-f]{40}$ ]]; then
      fact FRONTEND_RELEASE NO "the website was built from ${STATUS_SHA}, not the release ${EXPECTED_SHA}; redeploy the release commit"
    else
      fact FRONTEND_RELEASE UNVERIFIED "the deployment states no Git commit (VERCEL_GIT_COMMIT_SHA); deploy the release through the Vercel Git integration so the commit is recorded"
    fi
    case "$STATUS_UI" in
      true) fact TASK_COMPOSER_VISIBLE VERIFIED "the served build has NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI on" ;;
      false) fact TASK_COMPOSER_VISIBLE DISABLED "the served build has the execution UI OFF; it is inlined at BUILD time, so set it and REBUILD + REDEPLOY" ;;
      *) fact TASK_COMPOSER_VISIBLE UNVERIFIED "the deployment status did not state a boolean" ;;
    esac
  fi
fi

# ---------------------------------------------------------------------------
# 3. GATEWAY_EXECUTION_ENABLED — the running gateway's value, from TWO sources
#    that must agree: the deployment status above, and the gateway's own
#    observable behaviour on one unauthenticated GET of an execution-only route.
# ---------------------------------------------------------------------------
PROBE=UNVERIFIED
if [[ -n "$SITE_URL" ]]; then
  code="$(http_get "${SITE_URL}/api/gateway/workflow-proposals/00000000-0000-4000-8000-000000000000")"
  error="$(json_get "${_MILO_CHECK_TMP}/body" error)"
  if [[ "$code" == "403" && "$error" == '"This API route is not allowed by the gateway policy."' ]]; then
    PROBE=DISABLED
  elif [[ "$code" == "401" && "$error" == '"Authentication required."' ]]; then
    PROBE=VERIFIED
  fi
  printf 'GATEWAY_PROBE=%s (HTTP %s)\n' "$PROBE" "$code"
fi
case "${STATUS_GATEWAY}|${PROBE}" in
  "true|VERIFIED") fact GATEWAY_EXECUTION_ENABLED VERIFIED "status and behaviour agree" ;;
  "false|DISABLED" | "|DISABLED" | "false|UNVERIFIED")
    fact GATEWAY_EXECUTION_ENABLED DISABLED "GATEWAY_ALLOW_EXECUTION_ROUTES is off on the running gateway; set it on the Vercel production environment and redeploy" ;;
  "true|DISABLED" | "false|VERIFIED")
    fact GATEWAY_EXECUTION_ENABLED UNVERIFIED "the deployment status and the gateway's behaviour disagree" ;;
  *) fact GATEWAY_EXECUTION_ENABLED UNVERIFIED "neither the deployment status nor the gateway's behaviour proved the value" ;;
esac

# ---------------------------------------------------------------------------
# 4. GATEWAY_BACKEND_BINDING — the gateway can mint its identity and reach the
#    API: GET /api/gateway/health is a SAFE route that needs no user.
# ---------------------------------------------------------------------------
if [[ -z "$SITE_URL" ]]; then
  fact GATEWAY_BACKEND_BINDING UNVERIFIED "no website origin to read"
else
  code="$(http_get "${SITE_URL}/api/gateway/health")"
  status="$(json_get "${_MILO_CHECK_TMP}/body" status)"
  if [[ "$code" == "200" && "$status" == '"ok"' ]]; then
    fact GATEWAY_BACKEND_BINDING VERIFIED "the gateway reached the API's /health"
  elif [[ "$code" == "502" ]]; then
    fact GATEWAY_BACKEND_BINDING NO "the gateway could not reach the API (HTTP 502): check CLOUD_RUN_API_URL, workload identity and the API's invoker binding"
  else
    fact GATEWAY_BACKEND_BINDING UNVERIFIED "GET /api/gateway/health answered HTTP ${code}"
  fi
fi

# ---------------------------------------------------------------------------
# 5. BACKEND_EXECUTION_ARMED — every API and worker gate, read from Cloud Run.
#    A describe that fails is UNVERIFIED, never "unset".
# ---------------------------------------------------------------------------
# describe KIND NAME -> "name<TAB>value" per plain env var, "secret<TAB>name"
# per secret-backed one (the secret's value is never read). Nonzero when the
# resource could not be described.
ENV_REPORT_PY='
import json, sys
try:
    doc = json.load(sys.stdin)
except Exception:
    sys.exit(3)
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
found = containers(doc.get("spec", doc) if isinstance(doc, dict) else {})
if not found:
    sys.exit(3)
for entry in found[0].get("env") or []:
    if not isinstance(entry, dict) or not entry.get("name"):
        continue
    if (entry.get("valueFrom") or {}).get("secretKeyRef"):
        print("secret\t%s" % entry["name"])
    else:
        print("env\t%s\t%s" % (entry["name"], entry.get("value") or ""))
'
describe_env() {
  local kind="$1" name="$2" json
  if [[ "$kind" == "service" ]]; then
    json="$(gcloud run services describe "$name" --region "$REGION" --project "$PROJECT_ID" --format=json 2> /dev/null)" || return 1
  else
    json="$(gcloud run jobs describe "$name" --region "$REGION" --project "$PROJECT_ID" --format=json 2> /dev/null)" || return 1
  fi
  python3 -c "$ENV_REPORT_PY" <<< "$json"
}
env_value() { awk -F'\t' -v n="$2" '$1 == "env" && $2 == n { print $3; exit }' <<< "$1"; }
has_secret() { awk -F'\t' -v n="$2" '$1 == "secret" && $2 == n { found = 1 } END { exit !found }' <<< "$1"; }

BACKEND_PROBLEMS=""
expect_value() {
  # expect_value SCOPE REPORT NAME EXPECTED
  local value
  value="$(env_value "$2" "$3")"
  printf '  %-40s %-7s %s\n' "$3" "$1" "${value:-<unset>}"
  [[ "${value,,}" == "$4" ]] || BACKEND_PROBLEMS+="${3} is '${value:-<unset>}' on the ${1}, expected ${4}; "
}

if ! command -v gcloud > /dev/null 2>&1 || [[ -z "$PROJECT_ID" || -z "$API_SERVICE" || -z "$WORKER_JOB" ]]; then
  fact BACKEND_EXECUTION_ARMED UNVERIFIED "gcloud or the Cloud Run identifiers are unavailable"
elif ! API_REPORT="$(describe_env service "$API_SERVICE")"; then
  fact BACKEND_EXECUTION_ARMED UNVERIFIED "the API service ${API_SERVICE} could not be described"
elif ! JOB_REPORT="$(describe_env job "$WORKER_JOB")"; then
  fact BACKEND_EXECUTION_ARMED UNVERIFIED "the worker job ${WORKER_JOB} could not be described"
else
  printf 'Backend gate values:\n'
  for name in "${MILO_STAGE2_API_ENABLE_FLAGS[@]}"; do expect_value api "$API_REPORT" "$name" true; done
  for name in "${MILO_STAGE2_API_PINNED_OFF_FLAGS[@]}"; do expect_value api "$API_REPORT" "$name" false; done
  for name in "${MILO_STAGE2_WORKER_ENABLE_FLAGS[@]}"; do expect_value job "$JOB_REPORT" "$name" true; done
  for name in "${MILO_STAGE2_WORKER_PINNED_OFF_FLAGS[@]}"; do expect_value job "$JOB_REPORT" "$name" false; done
  # JOB_LAUNCHER is not a boolean and is the gate most easily missed: with it
  # 'disabled' the run row is created and shown, but nothing ever executes it.
  expect_value api "$API_REPORT" JOB_LAUNCHER cloud_run
  # Enabling EXECUTION_CONTROL without these takes the API down at startup.
  for required in MILO_WORKER_AUDIENCE MILO_APPROVED_WORKER_IDENTITIES; do
    if [[ -z "$(env_value "$API_REPORT" "$required")" ]]; then
      BACKEND_PROBLEMS+="${required} is unset on the API; "
    fi
  done
  # The provider credential: bound to the worker as a SECRET, never to the API.
  if [[ -n "$PROVIDER_SECRET" ]] && has_secret "$JOB_REPORT" KIMI_API_KEY; then
    printf '  %-40s %-7s %s\n' KIMI_API_KEY job "<secret binding>"
  else
    BACKEND_PROBLEMS+="the worker carries no KIMI_API_KEY secret binding; "
  fi
  for name in "${MILO_PROVIDER_KEY_ENV_NAMES[@]}"; do
    if has_secret "$API_REPORT" "$name" || [[ -n "$(env_value "$API_REPORT" "$name")" ]]; then
      BACKEND_PROBLEMS+="the API carries ${name}; it must never hold a provider credential; "
    fi
  done
  if [[ -n "$BACKEND_PROBLEMS" ]]; then
    fact BACKEND_EXECUTION_ARMED NO "${BACKEND_PROBLEMS%; }"
  else
    fact BACKEND_EXECUTION_ARMED VERIFIED "every Stage 2 API and worker gate has its exact value"
  fi
fi

# ---------------------------------------------------------------------------
# 6. MAPPING_PLAN_BATCH_PATH — the plan revision the first run will start.
# ---------------------------------------------------------------------------
if [[ "${#WS_ARGS[@]}" -eq 0 ]]; then
  fact MAPPING_PLAN_BATCH_PATH UNVERIFIED "no --work-scope-id/--work-scope-revision/--work-scope-digest named; the batch path is not proved"
else
  status=0
  bash "${SCRIPT_DIR}/work-scope-readiness.sh" --operator-config "$CONFIG_PATH" "${WS_ARGS[@]}" \
    > "${_MILO_CHECK_TMP}/scope.txt" 2>&1 || status=$?
  grep -E '^(EVIDENCE_READY|BATCH_READY|NEXT_BATCH_[A-Z_]+)=' "${_MILO_CHECK_TMP}/scope.txt" | sed 's/^/  /' || true
  case "$status" in
    0) fact MAPPING_PLAN_BATCH_PATH VERIFIED "the named revision is prepared and its next batch is ready" ;;
    1) fact MAPPING_PLAN_BATCH_PATH NO "$(grep -E '=NO' "${_MILO_CHECK_TMP}/scope.txt" | head -n 1)" ;;
    *) fact MAPPING_PLAN_BATCH_PATH UNVERIFIED "$(grep -E '=UNVERIFIED' "${_MILO_CHECK_TMP}/scope.txt" | head -n 1)" ;;
  esac
fi

# ---------------------------------------------------------------------------
# Only every fact VERIFIED means the stage is active.
# ---------------------------------------------------------------------------
ALL_VERIFIED=1 ANY_DISABLED=0
for name in FRONTEND_CODE_WIRED FRONTEND_RELEASE TASK_COMPOSER_VISIBLE GATEWAY_EXECUTION_ENABLED \
            GATEWAY_BACKEND_BINDING BACKEND_EXECUTION_ARMED MAPPING_PLAN_BATCH_PATH; do
  value="${FACTS[$name]:-UNVERIFIED}"
  if [[ "$value" != "VERIFIED" ]]; then ALL_VERIFIED=0; fi
  if [[ "$value" == "DISABLED" || "$value" == "NO" ]]; then ANY_DISABLED=1; fi
done
if [[ "$ALL_VERIFIED" -eq 1 ]]; then
  printf 'WEBSITE_EXECUTION_STAGE_ACTIVE=VERIFIED\n'
  printf '\nRESULT: OK\n'
  exit 0
fi
if [[ "$ANY_DISABLED" -eq 1 ]]; then
  printf 'WEBSITE_EXECUTION_STAGE_ACTIVE=DISABLED\n'
else
  printf 'WEBSITE_EXECUTION_STAGE_ACTIVE=UNVERIFIED\n'
fi
printf '\nRESULT: the website execution stage is NOT proved active. See scripts/release/execution_gate_chain.py for the full chain.\n' >&2
exit 1

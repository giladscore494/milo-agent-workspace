#!/usr/bin/env bash
# The production kill switch: the ONE canonical emergency order of
# docs/production-readiness/ROLLBACK.md ("Execution flags — emergency order"),
# as commands.
#
#   1. Vercel: GATEWAY_ALLOW_RUN_START_ROUTES=false, then redeploy
#   2. MILO_ENABLE_PAID_EXECUTION=false (worker job and API service)
#   3. API: MILO_ENABLE_RUN_CREATION=false, MILO_ENABLE_WORK_SCOPE_BATCHES=false,
#      JOB_LAUNCHER=disabled
#   4. MILO_ENABLE_GOVERNMENT_CATALOG_READ=false (worker job and API service)
#   5. Remove the provider API key from the worker (KIMI_API_KEY and its
#      MOONSHOT_API_KEY alias, whether bound as a secret or as a plain env var)
#   6. AFTER the order: every other flag the activation opened, read from
#      deployment-contract.sh (so the list cannot drift from what is opened):
#      the remaining Stage P / Stage 2 API and worker flags; the contract's
#      pinned-off flags re-asserted false on both (MILO_ENABLE_CATALOG_PROMOTION,
#      MILO_ENABLE_WORK_SCOPE_PREPARATION, ...); the Government capture job's
#      master flag, when that job exists; and the Vercel
#      GATEWAY_ALLOW_EXECUTION_ROUTES and NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI.
#      Step 6 closes run cancellation and the website's execution routes too,
#      so cancel runs that are still executing BEFORE it (or use
#      --order-only, cancel, then --remaining-only).
#
# DRY RUN BY DEFAULT: without --apply it prints every command and calls
# neither gcloud nor vercel. --apply additionally requires
# MILO_OPERATOR_ACK=I_UNDERSTAND_THIS_CHANGES_PRODUCTION and the URL of the
# current production Vercel deployment (--vercel-deployment), because step 1 is
# not in force until that deployment is redeployed. Every vercel command runs in
# the directory linked to the Vercel project (--vercel-cwd, default frontend/,
# as scripts/release/check-vercel-config.sh); --apply refuses before any change
# unless that directory is linked (.vercel/project.json) and `vercel whoami`
# succeeds.
#
# In apply mode every step runs in order; a step that fails is reported and
# the later steps still run (each one independently reduces exposure), then
# the API service and worker job are read back and every flag is checked.
# Exit 0 only when every step succeeded and every read-back value is closed.
#
# It never cancels an execution, never deletes anything, and changes nothing
# but the flags and the worker's provider-key binding named above. Setting a
# flag that is already false to false is a no-op, so re-running is safe.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
MILO_REPO_ROOT="$REPO_ROOT"
# shellcheck source=operator-config.sh
source "${SCRIPT_DIR}/operator-config.sh"
# shellcheck source=deployment-contract.sh
source "${SCRIPT_DIR}/deployment-contract.sh"

ACK_VALUE="I_UNDERSTAND_THIS_CHANGES_PRODUCTION"
MODE="dry-run"
SCOPE="all"
VERCEL_DEPLOYMENT=""
VERCEL_CWD=""
VERCEL_SCOPE=""
MILO_OPERATOR_CONFIG_PATH=""

usage() {
  cat << 'EOF'
Usage: kill-switch.sh [--dry-run | --apply] [options]

Default --dry-run prints the canonical emergency order as commands and
changes nothing (no gcloud or vercel call).

Options:
  --dry-run                  Print the commands only. Default.
  --apply                    Execute them. Requires
                             MILO_OPERATOR_ACK=I_UNDERSTAND_THIS_CHANGES_PRODUCTION
                             and --vercel-deployment.
  --vercel-deployment URL    The current production Vercel deployment, redeployed
                             after each Vercel change (step 1 and step 6).
  --vercel-cwd PATH          Directory linked to the Vercel project (default
                             frontend/). Must contain .vercel/project.json.
  --vercel-scope TEAM        Vercel team scope, passed to every vercel command.
  --order-only               Steps 1-5 only (keeps run cancellation open so runs
                             still executing can be cancelled).
  --remaining-only           Step 6 only (after --order-only and the cancellations).
  --operator-config PATH     Operator identifiers (default
                             config/production-operator.env).
  -h, --help                 Show this help.

Order: ROLLBACK.md "Execution flags — emergency order". Never executed by CI.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) MODE="dry-run"; shift ;;
    --apply) MODE="apply"; shift ;;
    --vercel-deployment) VERCEL_DEPLOYMENT="${2:?--vercel-deployment needs a URL}"; shift 2 ;;
    --vercel-cwd) VERCEL_CWD="${2:?--vercel-cwd needs a path}"; shift 2 ;;
    --vercel-scope) VERCEL_SCOPE="${2:?--vercel-scope needs a team}"; shift 2 ;;
    --order-only | --remaining-only)
      wanted="order"
      [[ "$1" == "--remaining-only" ]] && wanted="remaining"
      if [[ "$SCOPE" != "all" && "$SCOPE" != "$wanted" ]]; then
        printf 'FAIL: --order-only and --remaining-only are exclusive.\n' >&2
        exit 2
      fi
      SCOPE="$wanted"; shift ;;
    --operator-config) MILO_OPERATOR_CONFIG_PATH="${2:?--operator-config needs a path}"; shift 2 ;;
    -h | --help) usage; exit 0 ;;
    *) printf 'FAIL: unknown argument: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done

CONFIG_PATH="$(milo_operator_config_path "$REPO_ROOT" "$MILO_OPERATOR_CONFIG_PATH")"
milo_load_operator_config "$CONFIG_PATH" || exit 2
milo_require_op GCP_PROJECT_ID GCP_REGION CLOUD_RUN_API_SERVICE CLOUD_RUN_WORKER_JOB || exit 2
PROJECT_ID="$(milo_op GCP_PROJECT_ID)"
REGION="$(milo_op GCP_REGION)"
API_SERVICE="$(milo_op CLOUD_RUN_API_SERVICE)"
WORKER_JOB="$(milo_op CLOUD_RUN_WORKER_JOB)"
# Optional: the Government capture job exists only once an operator created it
# (government-production-capture.sh --ensure-job).
CAPTURE_JOB="$(milo_op CLOUD_RUN_CAPTURE_JOB)"

if [[ "$MODE" == "apply" ]]; then
  if [[ "${MILO_OPERATOR_ACK:-}" != "$ACK_VALUE" ]]; then
    printf 'FAIL: --apply changes production. Set MILO_OPERATOR_ACK=%s to confirm.\n' "$ACK_VALUE" >&2
    exit 2
  fi
  if [[ -z "$VERCEL_DEPLOYMENT" ]]; then
    printf 'FAIL: --apply needs --vercel-deployment <current production deployment URL>:\n' >&2
    printf '      a Vercel variable change is in force only after that deployment is redeployed.\n' >&2
    exit 2
  fi
  milo_require_gcloud_context "$PROJECT_ID" || exit 2
  if ! command -v vercel > /dev/null 2>&1; then
    printf 'FAIL: the Vercel CLI is not on PATH.\n' >&2
    exit 2
  fi
  if [[ ! -f "${VERCEL_CWD:-${REPO_ROOT}/frontend}/.vercel/project.json" ]]; then
    printf 'FAIL: %s is not linked to a Vercel project (.vercel/project.json missing).\n' "${VERCEL_CWD:-${REPO_ROOT}/frontend}" >&2
    printf "      Run 'vercel link' in that directory, or pass --vercel-cwd. Nothing was changed.\n" >&2
    exit 2
  fi
fi
[[ -z "$VERCEL_CWD" ]] && VERCEL_CWD="${REPO_ROOT}/frontend"
VERCEL_ARGS=()
[[ -n "$VERCEL_SCOPE" ]] && VERCEL_ARGS=(--scope "$VERCEL_SCOPE")
if [[ "$MODE" == "apply" ]] && ! (cd "$VERCEL_CWD" && vercel whoami "${VERCEL_ARGS[@]+"${VERCEL_ARGS[@]}"}") > /dev/null 2>&1; then
  printf "FAIL: 'vercel whoami' failed in %s: log in (vercel login) first. Nothing was changed.\n" "$VERCEL_CWD" >&2
  exit 2
fi

DELIM="$MILO_ENV_VAR_DELIMITER"
CLOSED="false"
PROVIDER_KEY_NAMES=(KIMI_API_KEY MOONSHOT_API_KEY)

# --- The flag sets, derived from the deployment contract --------------------
# Steps 2-4 close these by name, in the order; step 6 closes whatever else the
# activation opened on each surface.
ORDER_API_FLAGS=(MILO_ENABLE_PAID_EXECUTION MILO_ENABLE_RUN_CREATION
  MILO_ENABLE_WORK_SCOPE_BATCHES MILO_ENABLE_GOVERNMENT_CATALOG_READ)
ORDER_WORKER_FLAGS=(MILO_ENABLE_PAID_EXECUTION MILO_ENABLE_GOVERNMENT_CATALOG_READ)

in_list() {
  local needle="$1" item
  shift
  for item in "$@"; do [[ "$item" == "$needle" ]] && return 0; done
  return 1
}

unique_minus() {
  # unique_minus "<excluded...>" -- <names...>: names not excluded, first-seen order.
  local excluded=() seen=() name
  while [[ $# -gt 0 && "$1" != "--" ]]; do excluded+=("$1"); shift; done
  shift
  for name in "$@"; do
    in_list "$name" "${excluded[@]}" && continue
    in_list "$name" "${seen[@]+"${seen[@]}"}" && continue
    seen+=("$name")
  done
  printf '%s\n' "${seen[@]+"${seen[@]}"}"
}

# The enable arrays first, then the pinned-off arrays, then every flag Stage A
# pins off (MILO_STAGE_A_FLAG_NAMES, which also carries the dormant proposal
# flags), so a flag drifted open by any path is closed too.
mapfile -t REMAINING_API_FLAGS < <(unique_minus "${ORDER_API_FLAGS[@]}" -- \
  "${MILO_PLAN_AUTHORING_API_ENABLE_FLAGS[@]}" "${MILO_STAGE2_API_ENABLE_FLAGS[@]}" \
  "${MILO_STAGE2_API_PINNED_OFF_FLAGS[@]}" "${MILO_STAGE_A_FLAG_NAMES[@]}")
mapfile -t REMAINING_WORKER_FLAGS < <(unique_minus "${ORDER_WORKER_FLAGS[@]}" -- \
  "${MILO_STAGE2_WORKER_ENABLE_FLAGS[@]}" "${MILO_STAGE2_WORKER_PINNED_OFF_FLAGS[@]}" \
  "${MILO_STAGE_A_FLAG_NAMES[@]}")
CAPTURE_FLAG="$MILO_CAPTURE_MASTER_FLAG_NAME"
REMAINING_VERCEL_FLAGS=("$MILO_STAGE2_VERCEL_RUNTIME_FLAG" "$MILO_STAGE2_VERCEL_BUILD_FLAG")

pairs_closed() {
  local name out=()
  for name in "$@"; do out+=("${name}=${CLOSED}"); done
  (IFS="$DELIM"; printf '%s' "${out[*]}")
}

# --- Execution --------------------------------------------------------------
FAILED_STEPS=()

show() {
  local arg line="+"
  for arg in "$@"; do line+=" $(printf '%q' "$arg")"; done
  printf '%s\n' "$line"
}

run() {
  # run STEP CMD... — print, then (apply) execute; record the step on failure.
  local step="$1"
  shift
  show "$@"
  [[ "$MODE" == "apply" ]] || return 0
  if ! "$@"; then
    printf 'STEP %s FAILED: %s\n' "$step" "$*" >&2
    FAILED_STEPS+=("$step")
  fi
}

# Every vercel command runs in the linked project directory, with the scope.
vercel_in_cwd() {
  (cd "$VERCEL_CWD" && vercel "$@" "${VERCEL_ARGS[@]+"${VERCEL_ARGS[@]}"}")
}

run_vercel() {
  # run_vercel STEP ARGS... — like run, for a vercel command.
  local step="$1"
  shift
  show vercel "$@" "${VERCEL_ARGS[@]+"${VERCEL_ARGS[@]}"}"
  [[ "$MODE" == "apply" ]] || return 0
  if ! vercel_in_cwd "$@"; then
    printf 'STEP %s FAILED: vercel %s\n' "$step" "$*" >&2
    FAILED_STEPS+=("$step")
  fi
}

vercel_close() {
  # Closed = absent or "false": the gateway opens only on the value true.
  # `env rm` removes the whole record, so a value shared with the preview or
  # development targets is removed there too (only ever in the closing direction).
  local step="$1" name="$2"
  show vercel env rm "$name" production --yes "${VERCEL_ARGS[@]+"${VERCEL_ARGS[@]}"}"
  printf '+ printf false | vercel env add %s production%s\n' "$name" "${VERCEL_SCOPE:+ --scope $VERCEL_SCOPE}"
  [[ "$MODE" == "apply" ]] || return 0
  vercel_in_cwd env rm "$name" production --yes > /dev/null 2>&1 || true
  if ! printf 'false' | vercel_in_cwd env add "$name" production; then
    printf 'STEP %s FAILED: vercel env add %s production\n' "$step" "$name" >&2
    FAILED_STEPS+=("$step")
  fi
}

gcloud_job() { run "$1" gcloud run jobs update "$WORKER_JOB" --region "$REGION" --project "$PROJECT_ID" "${@:2}"; }
gcloud_api() { run "$1" gcloud run services update "$API_SERVICE" --region "$REGION" --project "$PROJECT_ID" "${@:2}"; }

describe_worker() {
  gcloud run jobs describe "$WORKER_JOB" --region "$REGION" --project "$PROJECT_ID" --format=json
}
describe_capture() {
  gcloud run jobs describe "$CAPTURE_JOB" --region "$REGION" --project "$PROJECT_ID" --format=json
}
describe_api() {
  gcloud run services describe "$API_SERVICE" --region "$REGION" --project "$PROJECT_ID" --format=json
}

# key_binding_form JSON NAME -> "secret", "env" or "" (not bound)
key_binding_form() {
  python3 -c '
import json, sys
doc = json.loads(sys.argv[1]); name = sys.argv[2]
def containers(node):
    if isinstance(node, dict):
        if isinstance(node.get("containers"), list):
            yield from node["containers"]
        for value in node.values():
            yield from containers(value)
    elif isinstance(node, list):
        for value in node:
            yield from containers(value)
form = ""
for container in containers(doc):
    for env in container.get("env") or []:
        if env.get("name") == name:
            form = "secret" if env.get("valueFrom") else "env"
print(form)
' "$1" "$2"
}

step_header() { printf '\n## %s\n' "$1"; }

printf '# MILO kill switch — mode: %s, scope: %s\n' "$MODE" "$SCOPE"
printf '# API service %s, worker job %s, project %s, region %s\n' "$API_SERVICE" "$WORKER_JOB" "$PROJECT_ID" "$REGION"
printf '# Order: docs/production-readiness/ROLLBACK.md "Execution flags — emergency order"\n'
printf '# vercel commands run in: %s\n' "$VERCEL_CWD"

if [[ "$SCOPE" != "remaining" ]]; then
  step_header "1. Vercel: close run starts (GATEWAY_ALLOW_RUN_START_ROUTES=false), then redeploy"
  vercel_close 1 "$MILO_STAGE2_VERCEL_RUN_START_FLAG"
  run_vercel 1 redeploy "${VERCEL_DEPLOYMENT:-<CURRENT_PRODUCTION_DEPLOYMENT_URL>}"

  step_header "2. Paid execution off (worker job and API service)"
  gcloud_job 2 --update-env-vars "MILO_ENABLE_PAID_EXECUTION=${CLOSED}"
  gcloud_api 2 --update-env-vars "MILO_ENABLE_PAID_EXECUTION=${CLOSED}"

  step_header "3. API: run creation, batches and the launcher off"
  gcloud_api 3 --update-env-vars "^${DELIM}^MILO_ENABLE_RUN_CREATION=${CLOSED}${DELIM}MILO_ENABLE_WORK_SCOPE_BATCHES=${CLOSED}${DELIM}JOB_LAUNCHER=disabled"

  step_header "4. Government catalog read off (worker job and API service)"
  gcloud_job 4 --update-env-vars "MILO_ENABLE_GOVERNMENT_CATALOG_READ=${CLOSED}"
  gcloud_api 4 --update-env-vars "MILO_ENABLE_GOVERNMENT_CATALOG_READ=${CLOSED}"

  step_header "5. Remove the provider API key from the worker"
  if [[ "$MODE" == "apply" ]]; then
    if worker_json="$(describe_worker)"; then
      for key in "${PROVIDER_KEY_NAMES[@]}"; do
        form="$(key_binding_form "$worker_json" "$key")" || form="unreadable"
        case "$form" in
          secret) gcloud_job 5 --remove-secrets "$key" ;;
          env) gcloud_job 5 --remove-env-vars "$key" ;;
          "") printf '%s is not bound on the worker.\n' "$key" ;;
          *)
            printf 'STEP 5 FAILED: could not read how %s is bound on the worker\n' "$key" >&2
            FAILED_STEPS+=(5) ;;
        esac
      done
    else
      printf 'STEP 5 FAILED: could not describe the worker job\n' >&2
      FAILED_STEPS+=(5)
    fi
  else
    for key in "${PROVIDER_KEY_NAMES[@]}"; do
      printf '# if %s is bound as a secret:\n' "$key"
      show gcloud run jobs update "$WORKER_JOB" --region "$REGION" --project "$PROJECT_ID" --remove-secrets "$key"
      printf '# if %s is a plain env var:\n' "$key"
      show gcloud run jobs update "$WORKER_JOB" --region "$REGION" --project "$PROJECT_ID" --remove-env-vars "$key"
    done
  fi
  printf '\nA worker execution that is already running keeps the configuration it\n'
  printf 'started with: cancel its run (run or batch cancellation) BEFORE step 6.\n'
fi

if [[ "$SCOPE" != "order" ]]; then
  step_header "6. After the order: every other flag the activation opened"
  if [[ ${#REMAINING_API_FLAGS[@]} -gt 0 ]]; then
    gcloud_api 6 --update-env-vars "^${DELIM}^$(pairs_closed "${REMAINING_API_FLAGS[@]}")"
  fi
  if [[ ${#REMAINING_WORKER_FLAGS[@]} -gt 0 ]]; then
    gcloud_job 6 --update-env-vars "^${DELIM}^$(pairs_closed "${REMAINING_WORKER_FLAGS[@]}")"
  fi
  CAPTURE_PRESENT=0
  if [[ -z "$CAPTURE_JOB" ]]; then
    printf '# CLOUD_RUN_CAPTURE_JOB is not configured: no capture job to close.\n'
  elif [[ "$MODE" != "apply" ]]; then
    printf '# if the capture job %s exists:\n' "$CAPTURE_JOB"
    show gcloud run jobs update "$CAPTURE_JOB" --region "$REGION" --project "$PROJECT_ID" \
      --update-env-vars "${CAPTURE_FLAG}=${CLOSED}"
  elif describe_capture > /dev/null 2>&1; then
    CAPTURE_PRESENT=1
    run 6 gcloud run jobs update "$CAPTURE_JOB" --region "$REGION" --project "$PROJECT_ID" \
      --update-env-vars "${CAPTURE_FLAG}=${CLOSED}"
  else
    printf 'The capture job %s does not exist: nothing to close.\n' "$CAPTURE_JOB"
  fi
  for name in "${REMAINING_VERCEL_FLAGS[@]}"; do
    vercel_close 6 "$name"
  done
  run_vercel 6 redeploy "${VERCEL_DEPLOYMENT:-<CURRENT_PRODUCTION_DEPLOYMENT_URL>}"
  printf '%s is inlined at BUILD time: the redeploy above rebuilds with it, so the\n' "$MILO_STAGE2_VERCEL_BUILD_FLAG"
  printf 'UI hides once that build is live (it hides UI; it is not a security boundary).\n'
fi

if [[ "$MODE" != "apply" ]]; then
  printf '\nDRY RUN: nothing was changed. Re-run with --apply (see --help) to execute.\n'
  exit 0
fi

# --- Read back ----------------------------------------------------------------
step_header "Read back"
expect_api=()
expect_worker=()
if [[ "$SCOPE" != "remaining" ]]; then
  expect_api+=("${ORDER_API_FLAGS[@]}")
  expect_worker+=("${ORDER_WORKER_FLAGS[@]}")
fi
if [[ "$SCOPE" != "order" ]]; then
  expect_api+=("${REMAINING_API_FLAGS[@]}")
  expect_worker+=("${REMAINING_WORKER_FLAGS[@]}")
fi

verify_json() {
  # verify_json LABEL JSON CHECK_LAUNCHER CHECK_KEYS FLAGS...
  local label="$1" json="$2" launcher="$3" keys="$4"
  shift 4
  python3 -c '
import json, sys
label, doc, launcher, keys = sys.argv[1], json.loads(sys.argv[2]), sys.argv[3], sys.argv[4]
flags = sys.argv[5:]
def containers(node):
    if isinstance(node, dict):
        if isinstance(node.get("containers"), list):
            yield from node["containers"]
        for value in node.values():
            yield from containers(value)
    elif isinstance(node, list):
        for value in node:
            yield from containers(value)
env = {}
for container in containers(doc):
    for item in container.get("env") or []:
        env[item.get("name")] = item
bad = []
for flag in flags:
    value = (env.get(flag) or {}).get("value", "")
    if str(value).strip().lower() in {"1", "true", "yes", "on"}:
        bad.append(f"{flag}={value}")
if launcher == "1" and (env.get("JOB_LAUNCHER") or {}).get("value") != "disabled":
    bad.append("JOB_LAUNCHER=%s" % (env.get("JOB_LAUNCHER") or {}).get("value"))
if keys == "1":
    for name in ("KIMI_API_KEY", "MOONSHOT_API_KEY"):
        if name in env:
            bad.append(f"{name} still bound")
# A service whose traffic is pinned to an older revision does not serve the
# revision this update created, so a closed template proves nothing there.
traffic = (doc.get("status") or {}).get("traffic") if isinstance(doc, dict) else None
if isinstance(traffic, list) and traffic and not any(
        entry.get("latestRevision") and int(entry.get("percent") or 0) == 100 for entry in traffic):
    bad.append("traffic is not 100% on the latest revision, so the closed configuration is not "
               "serving (gcloud run services update-traffic --to-latest)")
for problem in bad:
    print(f"NOT CLOSED ({label}): {problem}")
print(f"{label}: {len(flags)} flag(s) checked, {len(bad)} problem(s)")
sys.exit(1 if bad else 0)
' "$label" "$json" "$launcher" "$keys" "$@"
}

verified=1
if api_json="$(describe_api)"; then
  launcher=0
  [[ "$SCOPE" != "remaining" ]] && launcher=1
  verify_json "api" "$api_json" "$launcher" 0 "${expect_api[@]}" || verified=0
else
  printf 'READ-BACK FAILED: could not describe the API service\n' >&2
  verified=0
fi
if worker_json="$(describe_worker)"; then
  keys=0
  [[ "$SCOPE" != "remaining" ]] && keys=1
  verify_json "worker" "$worker_json" 0 "$keys" "${expect_worker[@]}" || verified=0
else
  printf 'READ-BACK FAILED: could not describe the worker job\n' >&2
  verified=0
fi
if [[ "$SCOPE" != "order" && "${CAPTURE_PRESENT:-0}" -eq 1 ]]; then
  if capture_json="$(describe_capture)"; then
    verify_json "capture" "$capture_json" 0 0 "$CAPTURE_FLAG" || verified=0
  else
    printf 'READ-BACK FAILED: could not describe the capture job\n' >&2
    verified=0
  fi
fi
printf 'Vercel values cannot be read back by the CLI; confirm with\n'
printf '  scripts/deploy/website-execution-check.sh --site-url <PRODUCTION_ORIGIN>\n'
printf '(expect GATEWAY_RUN_START_ENABLED=DISABLED).\n'

if [[ ${#FAILED_STEPS[@]} -gt 0 || "$verified" -ne 1 ]]; then
  printf '\nRESULT: INCOMPLETE — failed step(s): %s; read-back %s\n' \
    "${FAILED_STEPS[*]:-none}" "$([[ $verified -eq 1 ]] && echo OK || echo FAILED)" >&2
  exit 1
fi
printf '\nRESULT: OK — every step applied and every read-back value is closed.\n'

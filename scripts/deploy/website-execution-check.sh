#!/usr/bin/env bash
# Read-only proof of where the website execution chain is currently open.
#
# It reports FOUR SEPARATE facts, deliberately never collapsed into one
# "frontend ready" boolean, because they fail in different places for
# different reasons and an operator debugging one needs to know which:
#
#   FRONTEND_CODE_WIRED    the repository contains the canonical route
#   TASK_COMPOSER_VISIBLE  the browser bundle renders an interactive composer
#   GATEWAY_EXECUTION_ENABLED  the gateway will proxy the run-creation POST
#   BACKEND_EXECUTION_ARMED    the API+worker would accept and execute it
#
# The first is a property of the checked-out code. The other three are
# properties of what is DEPLOYED, and this script makes no claim about them it
# cannot substantiate: a value it could not read is UNKNOWN, never NO.
#
# IT NEVER SENDS THE RUN-CREATION POST. Proving the gateway gate by actually
# creating a run would create a run; the gate is proved from configuration
# instead. The first POST that creates a paid run is a human action.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
MILO_REPO_ROOT="$REPO_ROOT"
# shellcheck source=operator-config.sh
source "${SCRIPT_DIR}/operator-config.sh"

MILO_OPERATOR_CONFIG_PATH="" SITE_URL="" SKIP_REMOTE=0

usage() {
  cat << 'EOF'
Usage: website-execution-check.sh [options]

Read-only. Reports the four website-execution facts separately and never
sends a run-creation request.

Options:
  --operator-config <path>  Operator identifier file.
  --site-url <url>          Production website origin (default: PRODUCTION_ORIGIN).
  --offline                 Repository-side checks only; make no network call.
  --help

Exits nonzero when a fact required for the first run is NO.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --operator-config) MILO_OPERATOR_CONFIG_PATH="${2:?}"; shift 2 ;;
    --site-url) SITE_URL="${2:?}"; shift 2 ;;
    --offline) SKIP_REMOTE=1; shift ;;
    --help) usage; exit 0 ;;
    *) printf 'FAIL: unknown argument %s\n' "$1" >&2; exit 2 ;;
  esac
done

CONFIG_PATH="$(milo_operator_config_path "$REPO_ROOT" "$MILO_OPERATOR_CONFIG_PATH")"
milo_load_operator_config "$CONFIG_PATH" || exit 2
[[ -n "$SITE_URL" ]] || SITE_URL="$(milo_op PRODUCTION_ORIGIN)"
PROJECT_ID="$(milo_op GCP_PROJECT_ID)"
REGION="$(milo_op GCP_REGION)"
API_SERVICE="$(milo_op CLOUD_RUN_API_SERVICE)"
WORKER_JOB="$(milo_op CLOUD_RUN_WORKER_JOB)"

FAILURES=0
fail_fact() { printf 'FAIL: %s\n' "$1" >&2; FAILURES=$((FAILURES + 1)); }

# ---------------------------------------------------------------------------
# 1. FRONTEND_CODE_WIRED — a property of this checkout, always checkable.
# ---------------------------------------------------------------------------
WIRED=YES
grep -q "conversations/\${conversationId}/runs" "${REPO_ROOT}/frontend/lib/api.ts" 2>/dev/null \
  || grep -q "/runs" "${REPO_ROOT}/frontend/lib/api.ts" 2>/dev/null || WIRED=NO
[[ -f "${REPO_ROOT}/frontend/components/conversation/TaskComposer.tsx" ]] || WIRED=NO
[[ -f "${REPO_ROOT}/frontend/app/api/gateway/[...path]/route.ts" ]] || WIRED=NO
grep -q "executionRoutesEnabled" "${REPO_ROOT}/frontend/lib/server/gatewayPolicy.ts" 2>/dev/null || WIRED=NO
printf 'FRONTEND_CODE_WIRED=%s\n' "$WIRED"
[[ "$WIRED" == "YES" ]] || fail_fact "the canonical frontend run path is not present in this checkout"

if [[ "$SKIP_REMOTE" -eq 1 ]]; then
  printf 'TASK_COMPOSER_VISIBLE=UNKNOWN (--offline)\n'
  printf 'GATEWAY_EXECUTION_ENABLED=UNKNOWN (--offline)\n'
  printf 'BACKEND_EXECUTION_ARMED=UNKNOWN (--offline)\n'
  printf 'WEBSITE_EXECUTION_STAGE_ACTIVE=UNKNOWN (--offline)\n'
  exit $((FAILURES > 0 ? 1 : 0))
fi

# ---------------------------------------------------------------------------
# 2. TASK_COMPOSER_VISIBLE — a property of the SERVED bundle.
#
# NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI is inlined by Next.js at build time, so
# the only honest way to know its value is to look at what is being served.
# The disabled copy is a literal string in the bundle when the flag was false
# at build time, which is exactly why an env change without a rebuild does not
# move this fact.
# ---------------------------------------------------------------------------
DISABLED_COPY="Task submission is disabled until a separately approved execution stage."
COMPOSER=UNKNOWN
if [[ -n "$SITE_URL" ]]; then
  PAGE="$(curl -sS --max-time 25 "$SITE_URL" 2>/dev/null || true)"
  if [[ -z "$PAGE" ]]; then
    COMPOSER=UNKNOWN
    printf 'NOTE: could not fetch %s (site unreachable or requires auth).\n' "$SITE_URL"
  elif grep -qF "$DISABLED_COPY" <<< "$PAGE"; then
    COMPOSER=NO
  else
    # Absence of the disabled copy on an authenticated-only page is not proof
    # the composer renders, so this stays a weaker claim than a NO.
    COMPOSER=LIKELY_YES
  fi
fi
printf 'TASK_COMPOSER_VISIBLE=%s\n' "$COMPOSER"
printf 'DISABLED_MESSAGE_PRESENT=%s\n' "$([[ "$COMPOSER" == "NO" ]] && echo YES || echo NO_OR_UNKNOWN)"
[[ "$COMPOSER" == "NO" ]] && fail_fact \
  "the served bundle still carries the disabled copy — NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI was false at BUILD time; a Vercel env change alone will not fix this, the frontend must be REBUILT and REDEPLOYED"

# ---------------------------------------------------------------------------
# 3. GATEWAY_EXECUTION_ENABLED — server-side runtime value on Vercel.
#
# Proved from configuration, never by sending the POST. If the Vercel CLI is
# authenticated the value is read from the linked project; otherwise this is
# reported as UNKNOWN with the exact command to run.
# ---------------------------------------------------------------------------
GATEWAY=UNKNOWN
if command -v vercel > /dev/null 2>&1; then
  VERCEL_ENV_NAMES="$(vercel env ls production 2>/dev/null || true)"
  if grep -q "GATEWAY_ALLOW_EXECUTION_ROUTES" <<< "$VERCEL_ENV_NAMES"; then
    # Presence of the NAME is all that is read here; the value is not printed.
    GATEWAY=CONFIGURED_VALUE_UNVERIFIED
  else
    GATEWAY=NO
  fi
fi
printf 'GATEWAY_EXECUTION_ENABLED=%s\n' "$GATEWAY"
if [[ "$GATEWAY" == "UNKNOWN" ]]; then
  printf 'NOTE: vercel CLI unavailable. Verify with:\n'
  printf '      vercel env ls production | grep GATEWAY_ALLOW_EXECUTION_ROUTES\n'
elif [[ "$GATEWAY" == "NO" ]]; then
  fail_fact "GATEWAY_ALLOW_EXECUTION_ROUTES is not set on the Vercel production environment; the gateway rejects POST /conversations/{id}/runs before it reaches the API"
fi

# ---------------------------------------------------------------------------
# 4. BACKEND_EXECUTION_ARMED — every API and worker gate, read from Cloud Run.
# ---------------------------------------------------------------------------
BACKEND=UNKNOWN
if command -v gcloud > /dev/null 2>&1 && [[ -n "$PROJECT_ID" ]]; then
  api_env() {
    gcloud run services describe "$API_SERVICE" --region "$REGION" --project "$PROJECT_ID" \
      --format="value(spec.template.spec.containers[0].env.filter(\"name:$1\").extract(\"value\"))" \
      2> /dev/null | tr -d '[]' || true
  }
  job_env() {
    gcloud run jobs describe "$WORKER_JOB" --region "$REGION" --project "$PROJECT_ID" \
      --format="value(spec.template.spec.template.spec.containers[0].env.filter(\"name:$1\").extract(\"value\"))" \
      2> /dev/null | tr -d '[]' || true
  }
  BACKEND=YES
  check_on() {
    local scope="$1" name="$2" value
    if [[ "$scope" == "api" ]]; then value="$(api_env "$name")"; else value="$(job_env "$name")"; fi
    printf '  %-40s %-8s %s\n' "$name" "$scope" "${value:-<unset>}"
    if [[ "${value,,}" != "true" ]]; then
      BACKEND=NO
      fail_fact "${name} is '${value:-<unset>}' on the ${scope}; it must be true for the first run"
    fi
  }
  printf 'Backend gate values:\n'
  check_on api MILO_ENABLE_RUN_CREATION
  check_on api MILO_ENABLE_EXECUTION_CONTROL
  check_on job MILO_ENABLE_EXECUTION_CONTROL
  check_on job MILO_ENABLE_PAID_EXECUTION
  check_on job MILO_ENABLE_CATALOG_EXECUTION
  check_on job MILO_ENABLE_GOVERNMENT_CATALOG_READ

  # JOB_LAUNCHER is not a boolean and is the gate most easily missed: with it
  # 'disabled' the run row is created and shown, but nothing ever executes it.
  LAUNCHER="$(api_env JOB_LAUNCHER)"
  printf '  %-40s %-8s %s\n' "JOB_LAUNCHER" "api" "${LAUNCHER:-<unset>}"
  if [[ "$LAUNCHER" != "cloud_run" ]]; then
    BACKEND=NO
    fail_fact "JOB_LAUNCHER is '${LAUNCHER:-<unset>}' on the API; it must be 'cloud_run' or the run is created but never executed"
  fi

  # Enabling EXECUTION_CONTROL without these takes the API down at startup.
  for required in MILO_WORKER_AUDIENCE MILO_APPROVED_WORKER_IDENTITIES; do
    value="$(api_env "$required")"
    printf '  %-40s %-8s %s\n' "$required" "api" "${value:+<set>}"
    if [[ -z "$value" ]]; then
      BACKEND=NO
      fail_fact "${required} is unset on the API; with MILO_ENABLE_EXECUTION_CONTROL on, production_config fails startup without it"
    fi
  done

  # Promotion must stay off.
  PROMOTION="$(job_env MILO_ENABLE_CATALOG_PROMOTION)"
  printf '  %-40s %-8s %s\n' "MILO_ENABLE_CATALOG_PROMOTION" "job" "${PROMOTION:-<unset>}"
  if [[ "${PROMOTION,,}" == "true" ]]; then
    BACKEND=NO
    fail_fact "MILO_ENABLE_CATALOG_PROMOTION is true; it must stay false for this activation"
  fi
fi
printf 'BACKEND_EXECUTION_ARMED=%s\n' "$BACKEND"

# ---------------------------------------------------------------------------
# Only all four together mean the stage is active.
# ---------------------------------------------------------------------------
if [[ "$WIRED" == "YES" && "$COMPOSER" == "LIKELY_YES" \
      && "$GATEWAY" == "CONFIGURED_VALUE_UNVERIFIED" && "$BACKEND" == "YES" ]]; then
  printf 'WEBSITE_EXECUTION_STAGE_ACTIVE=YES\n'
else
  printf 'WEBSITE_EXECUTION_STAGE_ACTIVE=NO\n'
fi

if [[ "$FAILURES" -gt 0 ]]; then
  printf '\nRESULT: %d gate(s) not open. See scripts/release/execution_gate_chain.py for the full chain.\n' \
    "$FAILURES" >&2
  exit 1
fi
printf '\nRESULT: OK\n'

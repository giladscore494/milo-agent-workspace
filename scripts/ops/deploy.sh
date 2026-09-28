#!/usr/bin/env bash
# Deploy ONE release to production -- the operator R block, as one script the
# `deploy` workflow runs (.github/workflows/deploy.yml). Every step is the
# canonical tool for it; this script only orders them and stops at the first
# failure:
#
#   1. the checkout IS the release (HEAD == --sha)
#   2. CI is green on that exact SHA (the four mandatory `ci` jobs)
#   3. migrations: the database carries the COMPLETE local migration set
#      (check-migration-state.sh, read-only)
#   4. the website (Vercel) was built from that SHA and refuses run starts
#      (website-execution-check.sh, read-only)
#   5. 0 live runs (the RUNS_QUIESCENT statement, read-only)
#   6. Stage 2 reset -- unless permanent operating mode: the worker's provider
#      key binding is removed (RUNBOOK A.6) and the deploy is FORCED, so API and
#      worker come back at Stage A
#   7. production-activate.sh --all (preflight, database gate, deploy,
#      deployed gate); in permanent mode with --preserve-stage
#   8. model env: the reviewed worker model names, set and read back
#   9. worker contract: MILO_CAPTURE_REPLAY=false, and no provider key bound
#      (permanent mode: the key is expected and reported, never removed)
#  10. production-verify.sh --gate deployed for that SHA
#  11. only once 1-10 PASSED, and not in permanent mode: turn the website's
#      plan tools back on (--restore-website-stage, scripts/ops/website-stage.sh)
#      -- Stage P and/or E', which the Stage A deploy turned off. A failed
#      deploy stops before it and restores nothing.
#
# It never starts a run, never prepares a plan and never arms Stage 2. Each
# step writes a SUMMARY| line and a job-summary row. --dry-run prints every
# command and calls nothing.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

SHA="" PERMANENT="false" RESTORE_STAGE="none" RESTORED="none" TAB=$'\t'
REQUIRED_CI_JOBS=(offline-checks frontend-and-docker postgres-checks e2e)

usage() {
  cat << 'EOF'
Usage: deploy.sh --sha <40-hex> [--permanent-mode true|false] [--dry-run]
                 [--restore-website-stage none|plan-authoring|web-preparation|both]
                 [--operator-config <path>]

Deploys the checked-out release after proving CI, migrations, the website and
quiescence, then verifies the deployed gate and, after a successful deploy,
turns the named website plan tools back on. --dry-run calls nothing.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --sha) SHA="${2:?}"; shift 2 ;;
    --permanent-mode) PERMANENT="${2:?}"; shift 2 ;;
    --restore-website-stage) RESTORE_STAGE="${2?}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --operator-config) MILO_OPERATOR_CONFIG_PATH="${2:?}"; shift 2 ;;
    --help) usage; exit 0 ;;
    *) printf 'FAIL: unknown argument %s\n' "$1" >&2; exit 2 ;;
  esac
done
[[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || { printf 'FAIL: --sha must be a full 40-character lowercase SHA\n' >&2; exit 2; }
case "$PERMANENT" in true | false) ;; *) printf 'FAIL: --permanent-mode must be true or false\n' >&2; exit 2 ;; esac
case "$RESTORE_STAGE" in
  none | plan-authoring | web-preparation | both) ;;
  *) printf 'FAIL: --restore-website-stage must be none, plan-authoring, web-preparation or both\n' >&2; exit 2 ;;
esac

ops_load_config
CONFIG_ARG=(--operator-config "$CONFIG_PATH")
summary_header "Deploy ${SHA:0:12} (permanent mode: ${PERMANENT}; restore website stage: ${RESTORE_STAGE})"

# 1. The checkout is the release: every tool builds and tags `git rev-parse HEAD`.
head_sha="$(git -C "$REPO_ROOT" rev-parse HEAD)"
[[ "$head_sha" == "$SHA" ]] || ops_fail "the checkout is ${head_sha:0:12}, not the requested release" "1 release-checkout"
[[ -z "$(git -C "$REPO_ROOT" status --porcelain --untracked-files=no)" ]] \
  || ops_fail "the checkout has local changes" "1 release-checkout"
summary "1 release-checkout" PASS "HEAD is the requested SHA and clean"

# 2. CI green on that exact SHA, from the GitHub API (read-only, GITHUB_TOKEN).
ci_status() {
  python3 - "$SHA" "${REQUIRED_CI_JOBS[@]}" << 'PY'
import json, os, sys, urllib.request
sha, required = sys.argv[1], sys.argv[2:]
repo = os.environ.get("GITHUB_REPOSITORY", "")
token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
api = os.environ.get("GITHUB_API_URL", "https://api.github.com")
if not repo or not token:
    print("UNVERIFIED no GitHub repository or token in the environment"); sys.exit(0)
request = urllib.request.Request(
    f"{api}/repos/{repo}/commits/{sha}/check-runs?per_page=100",
    headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
try:
    with urllib.request.urlopen(request, timeout=30) as response:
        runs = json.load(response).get("check_runs") or []
except Exception as exc:
    print(f"UNVERIFIED the check runs could not be read ({type(exc).__name__})"); sys.exit(0)
latest = {}
for run in runs:
    name = run.get("name")
    if name in required and (name not in latest or run.get("id", 0) > latest[name].get("id", 0)):
        latest[name] = run
missing = [name for name in required if name not in latest]
red = [f"{name}={latest[name].get('conclusion') or latest[name].get('status')}"
       for name in required if name in latest and latest[name].get("conclusion") != "success"]
if missing or red:
    print("NO " + "; ".join([*(f"{name} has no run" for name in missing), *red]))
else:
    print("VERIFIED " + ", ".join(required))
PY
}
if [[ "$DRY_RUN" -eq 1 ]]; then
  printf 'DRY-RUN: GET /repos/$GITHUB_REPOSITORY/commits/%s/check-runs (require %s)\n' "$SHA" "${REQUIRED_CI_JOBS[*]}"
  summary "2 ci-green" DRY-RUN "would require ${REQUIRED_CI_JOBS[*]} green on ${SHA:0:12}"
else
  ci="$(ci_status)"
  [[ "$ci" == VERIFIED* ]] || ops_fail "CI is not green on ${SHA:0:12}: ${ci#* } (dispatch ci.yml on main first)" "2 ci-green"
  summary "2 ci-green" PASS "${ci#VERIFIED }"
fi

# 3. Migrations: the COMPLETE local set is applied (never a count).
if [[ "$DRY_RUN" -eq 1 ]]; then
  ops_run bash "${REPO_ROOT}/scripts/release/check-migration-state.sh" --database-url-env "$DB_URL_ENV"
  summary "3 migrations" DRY-RUN "would require fully-migrated and no BLOCKED finding"
else
  report="$(mktemp)"
  migration_status=0
  bash "${REPO_ROOT}/scripts/release/check-migration-state.sh" --database-url-env "$DB_URL_ENV" \
    --json-output "$report" > "${report}.log" 2>&1 || migration_status=$?
  grep -E '^\[(PASS|WARN|BLOCKED|MANUAL)\] remote:' "${report}.log" || true
  if [[ "$migration_status" -ne 0 ]] || ! grep -q 'remote schema classified as fully-migrated' "${report}.log"; then
    rm -f "$report" "${report}.log"
    ops_fail "the database is not fully migrated, or the read-only role cannot prove it (above); apply migrations with the Deploy Supabase Migrations workflow" "3 migrations"
  fi
  summary "3 migrations" PASS "$(grep -o 'fully-migrated ([0-9]*/[0-9]*' "${report}.log" | head -n 1))"
  rm -f "$report" "${report}.log"
fi

# 4. The website was built from this SHA; run starts are closed (permanent
#    mode: reported, since an operating website may have them open).
if [[ "$DRY_RUN" -eq 1 ]]; then
  ops_run bash "${REPO_ROOT}/scripts/deploy/website-execution-check.sh" "${CONFIG_ARG[@]}" --expected-sha "$SHA"
  summary "4 website" DRY-RUN "would require FRONTEND_RELEASE=VERIFIED and GATEWAY_RUN_START_ENABLED=DISABLED"
else
  website="$(bash "${REPO_ROOT}/scripts/deploy/website-execution-check.sh" "${CONFIG_ARG[@]}" \
    --expected-sha "$SHA" 2>&1 || true)"
  grep -E '^(FRONTEND_RELEASE|GATEWAY_RUN_START_ENABLED)=' <<< "$website" || true
  grep -q '^FRONTEND_RELEASE=VERIFIED' <<< "$website" \
    || ops_fail "Vercel has not built ${SHA:0:12} (FRONTEND_RELEASE is not VERIFIED)" "4 website"
  if ! grep -q '^GATEWAY_RUN_START_ENABLED=DISABLED' <<< "$website"; then
    if [[ "$PERMANENT" == "true" ]]; then
      summary "4 website" INFO "run starts are not proved closed; permanent mode keeps the website's posture"
    else
      ops_fail "the website does not prove run starts closed (GATEWAY_RUN_START_ENABLED is not DISABLED)" "4 website"
    fi
  fi
  summary "4 website" PASS "built from ${SHA:0:12}"
fi

# 5. Quiescent: a deploy never replaces a runtime under a live run.
ops_require_zero_live_runs "5 live-runs"

# 6. Stage 2 reset (the default). The Stage A deploy below pins every flag
#    off; the provider key is the one binding it does not remove (it is
#    non-destructive), so it is removed here -- RUNBOOK A.6, both forms.
if [[ "$PERMANENT" == "true" ]]; then
  summary "6 stage2-reset" SKIPPED "permanent operating mode: the live stage is kept (--preserve-stage)"
elif [[ "$DRY_RUN" -eq 1 ]]; then
  for name in "${MILO_PROVIDER_KEY_ENV_NAMES[@]}"; do
    ops_run gcloud run jobs update "$WORKER_JOB" --region "$REGION" --project "$PROJECT_ID" --remove-secrets "$name"
  done
  summary "6 stage2-reset" DRY-RUN "would remove any provider key from the worker and force a Stage A deploy"
else
  facts="$(ops_worker_json | ops_container_facts)" || ops_fail "the worker job could not be read" "6 stage2-reset"
  removed=()
  for name in "${MILO_PROVIDER_KEY_ENV_NAMES[@]}"; do
    if grep -qxF "secret${TAB}${name}" <<< "$facts"; then
      gcloud run jobs update "$WORKER_JOB" --region "$REGION" --project "$PROJECT_ID" --remove-secrets "$name" > /dev/null
      removed+=("$name")
    elif grep -q "^env${TAB}${name}${TAB}" <<< "$facts"; then
      gcloud run jobs update "$WORKER_JOB" --region "$REGION" --project "$PROJECT_ID" --remove-env-vars "$name" > /dev/null
      removed+=("$name")
    fi
  done
  summary "6 stage2-reset" PASS "provider key binding(s) removed: ${removed[*]:-none}; the deploy returns both surfaces to Stage A"
fi

# 7. The deploy itself, through the one orchestrator.
activate=(bash "${REPO_ROOT}/scripts/deploy/production-activate.sh" --all "${CONFIG_ARG[@]}")
if [[ "$PERMANENT" == "true" ]]; then
  activate+=(--preserve-stage)
else
  activate+=(--force-redeploy)
fi
if [[ "$DRY_RUN" -eq 1 ]]; then
  ops_run "${activate[@]}"
  summary "7 activate" DRY-RUN "would run production-activate.sh --all"
else
  "${activate[@]}" || ops_fail "production-activate.sh --all stopped (above)" "7 activate"
  summary "7 activate" PASS "preflight, database gate, deploy and deployed gate"
fi

# 8. The reviewed worker model names (not flags, not secrets), read back.
models="$(IFS="$MILO_ENV_VAR_DELIMITER"; printf '%s' "${MILO_REVIEWED_WORKER_MODEL_ENV[*]}")"
if [[ "$DRY_RUN" -eq 1 ]]; then
  ops_run gcloud run jobs update "$WORKER_JOB" --region "$REGION" --project "$PROJECT_ID" \
    --update-env-vars "^${MILO_ENV_VAR_DELIMITER}^${models}"
  summary "8 model-env" DRY-RUN "would set ${MILO_REVIEWED_WORKER_MODEL_ENV[*]}"
else
  gcloud run jobs update "$WORKER_JOB" --region "$REGION" --project "$PROJECT_ID" \
    --update-env-vars "^${MILO_ENV_VAR_DELIMITER}^${models}" > /dev/null \
    || ops_fail "the worker model env could not be set" "8 model-env"
  facts="$(ops_worker_json | ops_container_facts)" || ops_fail "the worker job could not be read back" "8 model-env"
  for pair in "${MILO_REVIEWED_WORKER_MODEL_ENV[@]}"; do
    grep -qxF "env${TAB}${pair%%=*}${TAB}${pair#*=}" <<< "$facts" \
      || ops_fail "${pair%%=*} did not read back as ${pair#*=}" "8 model-env"
  done
  summary "8 model-env" PASS "${MILO_REVIEWED_WORKER_MODEL_ENV[*]}"
fi

# 9. The worker contract: the replay capture off; no provider key at Stage A.
if [[ "$DRY_RUN" -eq 1 ]]; then
  ops_run gcloud run jobs describe "$WORKER_JOB" --region "$REGION" --project "$PROJECT_ID" --format=json
  summary "9 worker-contract" DRY-RUN "would require ${MILO_REPLAY_CAPTURE_FLAG_NAME}=false and no ${MILO_PROVIDER_KEY_ENV_NAMES[*]}"
else
  facts="$(ops_worker_json | ops_container_facts)" || ops_fail "the worker job could not be read" "9 worker-contract"
  grep -qxF "env${TAB}${MILO_REPLAY_CAPTURE_FLAG_NAME}${TAB}false" <<< "$facts" \
    || ops_fail "${MILO_REPLAY_CAPTURE_FLAG_NAME} is not false on the worker" "9 worker-contract"
  bound=()
  for name in "${MILO_PROVIDER_KEY_ENV_NAMES[@]}"; do
    grep -qE "^(secret|env)${TAB}${name}(${TAB}|$)" <<< "$facts" && bound+=("$name")
  done
  if [[ "${#bound[@]}" -gt 0 && "$PERMANENT" != "true" ]]; then
    ops_fail "a provider key is bound to the worker at Stage A: ${bound[*]}" "9 worker-contract"
  fi
  summary "9 worker-contract" PASS "${MILO_REPLAY_CAPTURE_FLAG_NAME}=false; provider key bound: ${bound[*]:-none}"
fi

# 10. The deployed gate, for this exact SHA.
if [[ "$DRY_RUN" -eq 1 ]]; then
  ops_run bash "${REPO_ROOT}/scripts/deploy/production-verify.sh" "${CONFIG_ARG[@]}" --gate deployed --expected-sha "$SHA"
  summary "10 deployed-gate" DRY-RUN "would require CODE_DEPLOYED and DATABASE_READY VERIFIED"
else
  bash "${REPO_ROOT}/scripts/deploy/production-verify.sh" "${CONFIG_ARG[@]}" --gate deployed \
    --expected-sha "$SHA" || ops_fail "the deployed gate did not pass (above)" "10 deployed-gate"
  summary "10 deployed-gate" PASS "CODE_DEPLOYED and DATABASE_READY VERIFIED for ${SHA:0:12}"
fi

# 11. The website's plan tools, which the Stage A deploy turned off. Reached
#     only when every step above passed (each failure exits); never Stage 2.
if [[ "$OPS_FAILED" -ne 0 ]]; then
  ops_fail "an earlier step failed; the website stage is not restored" "11 website-stage"
elif [[ "$RESTORE_STAGE" == "none" ]]; then
  summary "11 website-stage" SKIPPED "restore_website_stage=none: plan authoring and the Prepare button stay off"
elif [[ "$PERMANENT" == "true" ]]; then
  summary "11 website-stage" SKIPPED "permanent operating mode: the live stage was kept (--preserve-stage)"
else
  restore=(bash "${SCRIPT_DIR}/website-stage.sh" --stage "$RESTORE_STAGE" "${CONFIG_ARG[@]}" --step-prefix 11 --no-header)
  [[ "$DRY_RUN" -eq 1 ]] && restore+=(--dry-run)
  "${restore[@]}" || ops_fail "deployed ${SHA:0:12}, but the website stage ${RESTORE_STAGE} was not restored (above); run the website-stage workflow" "11 website-stage"
  RESTORED="$RESTORE_STAGE"
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
  summary_note "DRY RUN: nothing was called and nothing was changed."
  exit 0
fi
summary_note "Deployed ${SHA:0:12}; website plan tools turned back on: ${RESTORED}. No run was started, nothing was prepared and Stage 2 was not armed."
exit "$OPS_FAILED"

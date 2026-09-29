#!/usr/bin/env bash
# Turn the website's plan tools back on -- the `website-stage` workflow, and
# step 11 of deploy.sh after a SUCCESSFUL deploy. A Stage A deploy pins both
# off, and without this nothing but Cloud Shell turns them on again:
#
#   plan-authoring   Stage P: MILO_ENABLE_WORK_SCOPE_MUTATIONS on the API
#                    (website-execution-activate.sh --apply-plan-authoring)
#   web-preparation  E', the Prepare button: the capture job on the release
#                    image (government-production-capture.sh --ensure-job
#                    --enable-catalog-execution), then
#                    MILO_ENABLE_WORK_SCOPE_PREPARATION_REQUESTS on the API,
#                    the API identity's run-with-overrides binding on that job
#                    only, and its roles/run.viewer binding on the capture and
#                    worker jobs only, read back -- the Prepare route reads
#                    both before it starts anything
#                    (website-execution-activate.sh --apply-web-preparation)
#   both             the two above, in that order
#   register-capture PR-D1, the Register page: the capture job on the release
#                    image (government-production-capture.sh --ensure-job
#                    --enable-catalog-execution, which also sets the archive
#                    bucket on the job), then MILO_ENABLE_REGISTER_CAPTURE on
#                    the API with the same job bindings as E', read back
#                    (website-execution-activate.sh --apply-register-capture).
#                    Refused until scripts/ops/setup-register-archive.sh has
#                    run. Not part of `both`: it is its own decision.
#   all              `both`, then the Register page (the capture job is
#                    ensured once)
#   none             nothing
#
# Every step is the canonical tool, unchanged, behind its own gate
# (production-verify.sh --gate deployed; E' also requires the capture job on
# the worker's image). The checkout must be the DEPLOYED release: both tools
# read `git rev-parse HEAD`.
#
# It never touches Stage 2: no run creation, no batches, no paid execution, no
# provider key, no runtime policy -- that is arm.yml's alone. It prepares no
# plan and starts no run. --dry-run prints the commands and calls nothing.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

STAGE="" STEP_PREFIX="" HEADER=1
usage() {
  cat << 'EOF'
Usage: website-stage.sh --stage plan-authoring|web-preparation|both|register-capture|all|none [--dry-run]
                        [--operator-config <path>] [--step-prefix <n>] [--no-header]

Turns the website's plan authoring (Stage P) and/or its Prepare button (E'),
or its Register page (register-capture; all = both + register-capture), on
through the canonical activation tools. Never Stage 2. --dry-run calls nothing.
EOF
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --stage) STAGE="${2?}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --operator-config) MILO_OPERATOR_CONFIG_PATH="${2:?}"; shift 2 ;;
    --step-prefix) STEP_PREFIX="${2:?}"; shift 2 ;;
    --no-header) HEADER=0; shift ;;
    --help) usage; exit 0 ;;
    *) printf 'FAIL: unknown argument %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done
case "$STAGE" in
  plan-authoring | web-preparation | both | register-capture | all | none) ;;
  *) printf 'FAIL: --stage must be plan-authoring, web-preparation, both, register-capture, all or none\n' >&2; usage >&2; exit 2 ;;
esac
[[ -z "$STEP_PREFIX" || "$STEP_PREFIX" =~ ^[0-9]{1,2}$ ]] \
  || { printf 'FAIL: --step-prefix must be a step number\n' >&2; exit 2; }

ops_load_config
# What each canonical tool refuses to run without, checked here so a dry run
# already says so.
if [[ "$STAGE" != "none" ]]; then
  milo_require_op SECRET_PROVIDER_API_KEY MILO_GATEWAY_AUDIENCE MILO_APPROVED_GATEWAY_IDENTITIES \
    PRODUCTION_ORIGIN MILO_WORKER_AUDIENCE || exit 2
fi
if [[ "$STAGE" == "web-preparation" || "$STAGE" == "both" || "$STAGE" == "register-capture" || "$STAGE" == "all" ]]; then
  milo_require_op CLOUD_RUN_CAPTURE_JOB API_SERVICE_ACCOUNT ARTIFACT_REGISTRY_REPOSITORY \
    SUPABASE_PROJECT_REF SECRET_SUPABASE_URL SECRET_SUPABASE_SERVICE_KEY || exit 2
fi
if [[ "$STAGE" == "register-capture" || "$STAGE" == "all" ]]; then
  milo_require_op REGISTER_ARCHIVE_BUCKET || exit 2
fi

# step_name NUMBER LETTER NAME -- "2 capture-job" alone, "11b capture-job" in a deploy.
step_name() {
  if [[ -n "$STEP_PREFIX" ]]; then printf '%s%s %s' "$STEP_PREFIX" "$2" "$3"; else printf '%s %s' "$1" "$3"; fi
}
activate=(bash "${REPO_ROOT}/scripts/deploy/website-execution-activate.sh" --operator-config "$CONFIG_PATH")
capture=(bash "${REPO_ROOT}/scripts/catalog/government-production-capture.sh" --operator-config "$CONFIG_PATH")
release="$(git -C "$REPO_ROOT" rev-parse HEAD)"
[[ "$HEADER" -eq 1 ]] && summary_header "Website stage: ${STAGE} (release ${release:0:12})"

if [[ "$STAGE" == "none" ]]; then
  summary "$(step_name 1 a website-stage)" SKIPPED "stage=none: the website's plan tools were left as they are"
  exit 0
fi

# run_step STEP DETAIL FAILURE CMD... -- the canonical tool, or its dry-run line.
run_step() {
  local step="$1" detail="$2" failure="$3"
  shift 3
  if [[ "$DRY_RUN" -eq 1 ]]; then
    ops_run "$@"
    summary "$step" DRY-RUN "would apply: ${detail}"
    return 0
  fi
  "$@" || ops_fail "$failure" "$step"
  summary "$step" PASS "$detail"
}

if [[ "$STAGE" == "plan-authoring" || "$STAGE" == "both" || "$STAGE" == "all" ]]; then
  run_step "$(step_name 1 a plan-authoring)" \
    "Stage P: MILO_ENABLE_WORK_SCOPE_MUTATIONS on the API; run creation read back OFF" \
    "Stage P was not applied (above); the website cannot edit a plan" \
    "${activate[@]}" --apply-plan-authoring
fi
if [[ "$STAGE" == "web-preparation" || "$STAGE" == "both" || "$STAGE" == "all" ]]; then
  run_step "$(step_name 2 b capture-job)" \
    "the capture job on the release image ${release:0:12}" \
    "the capture job was not ensured (above); the Prepare button stays off" \
    "${capture[@]}" --ensure-job --enable-catalog-execution
  run_step "$(step_name 3 c web-preparation)" \
    "E' (the Prepare button): the API identity reads the capture and worker jobs (${MILO_API_JOB_READ_ROLE}, read back); MILO_ENABLE_WORK_SCOPE_PREPARATION_REQUESTS on the API; paid execution and preparation read back OFF" \
    "E' was not applied (above); the Prepare button stays off" \
    "${activate[@]}" --apply-web-preparation
fi
if [[ "$STAGE" == "register-capture" ]]; then
  run_step "$(step_name 2 b capture-job)" \
    "the capture job on the release image ${release:0:12}, with the register archive bucket" \
    "the capture job was not ensured (above); the Register page stays off" \
    "${capture[@]}" --ensure-job --enable-catalog-execution
fi
if [[ "$STAGE" == "register-capture" || "$STAGE" == "all" ]]; then
  # `all`: the capture job was ensured once, above (step 2).
  register_step=(3 c)
  [[ "$STAGE" == "all" ]] && register_step=(4 d)
  run_step "$(step_name "${register_step[0]}" "${register_step[1]}" register-capture)" \
    "PR-D1 (the Register page): the register archive gate (PASS; or PARTIAL = WARN: its IAM is verified from Cloud Shell, setup-register-archive.sh --check); the API identity runs and reads the capture job; MILO_ENABLE_REGISTER_CAPTURE on the API; paid execution read back OFF" \
    "register capture was not applied (above); the Register page stays off" \
    "${activate[@]}" --apply-register-capture
fi

# Inside a deploy (--no-header) the deploy writes the closing note.
if [[ "$HEADER" -eq 0 ]]; then
  :
elif [[ "$DRY_RUN" -eq 1 ]]; then
  summary_note "DRY RUN: nothing was called and nothing was changed."
else
  summary_note "The website's plan tools are on. Stage 2 was not touched: no run can start, nothing was prepared and no provider call was made."
fi
exit "$OPS_FAILED"

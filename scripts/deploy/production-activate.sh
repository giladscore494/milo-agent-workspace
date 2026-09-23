#!/usr/bin/env bash
# Thin orchestration over the activation bundle, in the ONE safe order.
#
# It owns no logic of its own: every step below is the canonical tool for that
# step, invoked with the operator's configuration. It exists so the operator
# types one command per intent instead of five with matching flags.
#
# THE ORDER, and why it is this order:
#
#   1. preflight          read-only: the project, IAM, secrets, RuntimePolicy
#   2. database gate      read-only: the EXACT migration set is applied and the
#                         batch path's tables/RPCs/grants exist. The release's
#                         code calls them, so nothing deploys before this holds.
#                         Migrations are applied by the Deploy Supabase
#                         Migrations workflow, never from here.
#   3. deploy             builds and pushes BOTH images, then deploys API and
#                         worker at Stage A (every execution flag false, no
#                         provider key). Skipped when this release is already
#                         deployed, so a resumed sequence never resets a later
#                         stage's flags. The worker image the capture job runs
#                         exists only after this step.
#   4. verify (deployed)  read-only
#   ---- --all stops here: a person authors the Mapping Plan in the website
#        (Stage P) before anything can be prepared ----
#   5. prepare-work-scope the named plan revision: ensure the capture job on the
#                         release image, prepare an operator capture run, run
#                         the scoped preparation, verify. Skipped (verify only)
#                         when that revision is already prepared.
#   6. verify (prepared)  read-only, for the named revision
#   7. website            Stage 2 is applied by website-execution-activate.sh
#                         --apply-backend, a separate decision; this prints it.
#
# It never starts a MILO run, never enables paid execution, catalog promotion
# or the website execution stage, and never runs a capture before the release
# worker image exists. The first failing step stops the sequence.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
MILO_REPO_ROOT="$REPO_ROOT"
# shellcheck source=operator-config.sh
source "${SCRIPT_DIR}/operator-config.sh"

DO_PREFLIGHT=0 DO_DB_GATE=0 DO_CAPTURE=0 DO_DEPLOY=0 DO_VERIFY=0 DO_PREPARE=0 DO_WEBSITE=0
PLAN_ONLY=0 ENABLE_CATALOG=0 ENABLE_PREPARATION=0 FORCE_REDEPLOY=0
MILO_OPERATOR_CONFIG_PATH="" CONFIG_ARG=()
WS_ARGS=()

usage() {
  cat << 'EOF'
Usage: production-activate.sh [steps] [options]

Steps (combine freely; they always run in the order below):
  --plan          Read-only. Preflight, deployment check and every plan. No mutation.
  --preflight     Read-only preflight.
  --database-gate Read-only: the exact migration set and the batch path's schema.
  --deploy        Build + push both images, deploy API + worker at Stage A.
                  Skipped when this release is already deployed.
  --verify        Read-only verification (gate "deployed", or "prepared" when
                  a plan revision is named).
  --capture       OPTIONAL whole-register capture (requires
                  --enable-catalog-execution). Not needed by batch runs.
  --prepare-work-scope
                  Prepare the named plan revision (requires the three
                  --work-scope-* values, --enable-catalog-execution and
                  --enable-work-scope-preparation).
  --website       Print the Stage 2 activation (never applied from here).
  --all           preflight, database gate, deploy, verify — then STOP.

Options:
  --work-scope-id <uuid> --work-scope-revision <n> --work-scope-digest <hex>
  --enable-catalog-execution      The capture job's master switch value.
  --enable-work-scope-preparation The scoped-preparation switch, for ONE execution.
  --force-redeploy                Deploy even when this release is deployed
                                  (returns API and worker to Stage A).
  --operator-config <path>
  --help

This never launches a paid run. See
docs/production-readiness/SCOPED_BATCH_PRODUCTION_RUNBOOK.md.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --plan) PLAN_ONLY=1; shift ;;
    --preflight) DO_PREFLIGHT=1; shift ;;
    --database-gate) DO_DB_GATE=1; shift ;;
    --capture) DO_CAPTURE=1; shift ;;
    --deploy) DO_DEPLOY=1; shift ;;
    --verify) DO_VERIFY=1; shift ;;
    --prepare-work-scope) DO_PREPARE=1; shift ;;
    --website) DO_WEBSITE=1; shift ;;
    --all) DO_PREFLIGHT=1; DO_DB_GATE=1; DO_DEPLOY=1; DO_VERIFY=1; shift ;;
    --enable-catalog-execution) ENABLE_CATALOG=1; shift ;;
    --enable-work-scope-preparation) ENABLE_PREPARATION=1; shift ;;
    --force-redeploy) FORCE_REDEPLOY=1; shift ;;
    --work-scope-id | --work-scope-revision | --work-scope-digest) WS_ARGS+=("$1" "${2:?}"); shift 2 ;;
    --operator-config) MILO_OPERATOR_CONFIG_PATH="${2:?}"; CONFIG_ARG=(--operator-config "${2:?}"); shift 2 ;;
    --help) usage; exit 0 ;;
    *) printf 'FAIL: unknown argument %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ "$PLAN_ONLY" -eq 0 && "$DO_PREFLIGHT" -eq 0 && "$DO_DB_GATE" -eq 0 && "$DO_CAPTURE" -eq 0 \
      && "$DO_DEPLOY" -eq 0 && "$DO_VERIFY" -eq 0 && "$DO_PREPARE" -eq 0 && "$DO_WEBSITE" -eq 0 ]]; then
  printf 'FAIL: choose at least one step (or --plan)\n' >&2
  usage >&2
  exit 2
fi
if [[ "$DO_CAPTURE" -eq 1 && "$ENABLE_CATALOG" -eq 0 ]]; then
  printf 'FAIL: --capture requires --enable-catalog-execution\n' >&2
  exit 2
fi
if [[ "$DO_PREPARE" -eq 1 ]]; then
  if [[ "${#WS_ARGS[@]}" -ne 6 ]]; then
    printf 'FAIL: --prepare-work-scope requires --work-scope-id, --work-scope-revision and --work-scope-digest\n' >&2
    printf '      (list the open plans with scripts/deploy/work-scope-readiness.sh --list)\n' >&2
    exit 2
  fi
  if [[ "$ENABLE_CATALOG" -eq 0 || "$ENABLE_PREPARATION" -eq 0 ]]; then
    printf 'FAIL: --prepare-work-scope requires --enable-catalog-execution and --enable-work-scope-preparation\n' >&2
    exit 2
  fi
fi
if [[ "$DO_WEBSITE" -eq 1 && ( "$DO_CAPTURE" -eq 1 || "$DO_PREPARE" -eq 1 || "$DO_DEPLOY" -eq 1 ) ]]; then
  printf 'FAIL: --website may not be combined with --deploy, --capture or --prepare-work-scope.\n' >&2
  printf '      Land and verify the prepared batch first, read the verify output, then\n' >&2
  printf '      activate separately.\n' >&2
  exit 2
fi

CONFIG_PATH="$(milo_operator_config_path "$REPO_ROOT" "$MILO_OPERATOR_CONFIG_PATH")"
milo_load_operator_config "$CONFIG_PATH" || exit 2

step() { printf '\n========== %s ==========\n' "$1"; }

# cloud-run.sh reads its targets from the environment. They are exported here
# FROM the operator configuration, so the deploy aims at exactly the project
# every other tool in this bundle reads -- and a shell that already carries a
# DIFFERENT value is refused rather than silently deploying elsewhere.
export_deploy_env() {
  local pair name key value current
  for pair in PROJECT_ID:GCP_PROJECT_ID REGION:GCP_REGION REPOSITORY:ARTIFACT_REGISTRY_REPOSITORY \
              API_SERVICE:CLOUD_RUN_API_SERVICE WORKER_JOB:CLOUD_RUN_WORKER_JOB \
              API_SERVICE_ACCOUNT:API_SERVICE_ACCOUNT WORKER_SERVICE_ACCOUNT:WORKER_SERVICE_ACCOUNT \
              MILO_GATEWAY_AUDIENCE:MILO_GATEWAY_AUDIENCE \
              MILO_APPROVED_GATEWAY_IDENTITIES:MILO_APPROVED_GATEWAY_IDENTITIES \
              MILO_EXPECTED_SUPABASE_PROJECT_REF:SUPABASE_PROJECT_REF \
              ALLOWED_CORS_ORIGINS:ALLOWED_CORS_ORIGINS; do
    name="${pair%%:*}"
    key="${pair#*:}"
    value="$(milo_op "$key")"
    if [[ "$name" == "ALLOWED_CORS_ORIGINS" && -z "$value" ]]; then
      value="$(milo_op PRODUCTION_ORIGIN)"
    fi
    if [[ -z "$value" ]]; then
      printf 'FAIL: the operator configuration has no %s (needed by the deploy as %s)\n' "$key" "$name" >&2
      return 1
    fi
    current="${!name:-}"
    if [[ -n "$current" && "$current" != "$value" ]]; then
      printf 'FAIL: the shell exports %s=%s but the operator configuration says %s. Unset it.\n' \
        "$name" "$current" "$value" >&2
      return 1
    fi
    export "${name}=${value}"
  done
  # Stage A: the launcher stays disabled. A shell that exported another mode
  # does not get to deploy it through this bundle.
  export JOB_LAUNCHER_MODE=disabled
}

release_deployed() {
  # 0 when API and worker already run this exact release.
  bash "${SCRIPT_DIR}/production-verify.sh" "${CONFIG_ARG[@]}" --gate deployed 2> /dev/null \
    | grep -q '^CODE_DEPLOYED=VERIFIED'
}

if [[ "$PLAN_ONLY" -eq 1 ]]; then
  step "preflight (read-only)"
  bash "${SCRIPT_DIR}/production-preflight.sh" "${CONFIG_ARG[@]}" || true
  step "database gate (read-only)"
  bash "${SCRIPT_DIR}/production-verify.sh" "${CONFIG_ARG[@]}" --gate database || true
  step "deployment check (read-only)"
  if export_deploy_env; then
    DEPLOY_MODE=check bash "${SCRIPT_DIR}/cloud-run.sh" || true
  fi
  step "Government capture plan (read-only)"
  bash "${REPO_ROOT}/scripts/catalog/government-production-capture.sh" --plan "${CONFIG_ARG[@]}" || true
  step "execution gate chain (reference)"
  python3 "${REPO_ROOT}/scripts/release/execution_gate_chain.py" || true
  step "activation plan (read-only)"
  bash "${SCRIPT_DIR}/website-execution-activate.sh" --plan --skip-stage1-check "${CONFIG_ARG[@]}" || true
  printf '\nPLAN COMPLETE — nothing was mutated.\n'
  exit 0
fi

if [[ "$DO_PREFLIGHT" -eq 1 ]]; then
  step "1. preflight (read-only)"
  bash "${SCRIPT_DIR}/production-preflight.sh" "${CONFIG_ARG[@]}"
fi

if [[ "$DO_DB_GATE" -eq 1 ]]; then
  step "2. database gate (read-only)"
  if ! bash "${SCRIPT_DIR}/production-verify.sh" "${CONFIG_ARG[@]}" --gate database; then
    printf '\nSTOP: the database does not carry the exact migration set this release calls.\n' >&2
    printf '      Apply the pending migrations through the Deploy Supabase Migrations workflow\n' >&2
    printf '      (dry-run, then apply), then re-run. Nothing was deployed.\n' >&2
    exit 1
  fi
fi

if [[ "$DO_DEPLOY" -eq 1 ]]; then
  step "3. deploy: build + push both images, API + worker at Stage A"
  if [[ "$FORCE_REDEPLOY" -eq 0 ]] && release_deployed; then
    printf 'SKIPPED: API and worker already run %s. A redeploy would return both to\n' \
      "$(git -C "$REPO_ROOT" rev-parse HEAD)"
    printf '         Stage A; pass --force-redeploy only if that is what you intend.\n'
  else
    export_deploy_env
    DEPLOY_MODE=apply bash "${SCRIPT_DIR}/cloud-run.sh"
  fi
fi

if [[ "$DO_VERIFY" -eq 1 ]]; then
  if [[ "${#WS_ARGS[@]}" -gt 0 && "$DO_PREPARE" -eq 0 ]]; then
    step "verify (read-only, gate: prepared)"
    bash "${SCRIPT_DIR}/production-verify.sh" "${CONFIG_ARG[@]}" --gate prepared "${WS_ARGS[@]}"
  else
    step "4. verify (read-only, gate: deployed)"
    bash "${SCRIPT_DIR}/production-verify.sh" "${CONFIG_ARG[@]}" --gate deployed
  fi
fi

attempt_key() { printf '%s-%s' "$1" "$(date -u +%Y%m%dT%H%M%SZ)"; }

if [[ "$DO_CAPTURE" -eq 1 ]]; then
  step "optional: whole-register Government capture"
  # The capture script itself refuses before touching the job when the
  # release worker image does not exist yet.
  bash "${REPO_ROOT}/scripts/catalog/government-production-capture.sh" --all \
    --enable-catalog-execution --idempotency-key "$(attempt_key register)" "${CONFIG_ARG[@]}"
fi

if [[ "$DO_PREPARE" -eq 1 ]]; then
  step "5. prepare the named plan revision"
  readiness_status=0
  bash "${SCRIPT_DIR}/work-scope-readiness.sh" "${CONFIG_ARG[@]}" "${WS_ARGS[@]}" \
    > "${TMPDIR:-/tmp}/milo-prepare-readiness.$$" 2>&1 || readiness_status=$?
  readiness="$(cat "${TMPDIR:-/tmp}/milo-prepare-readiness.$$")"
  rm -f "${TMPDIR:-/tmp}/milo-prepare-readiness.$$"
  printf '%s\n' "$readiness"
  if grep -q '^WORK_SCOPE_PREPARED=VERIFIED' <<< "$readiness"; then
    printf '\nSKIPPED: this revision is already prepared (a revision is prepared exactly once).\n'
  elif grep -qE '^(WORK_SCOPE_PLAN|WORK_SCOPE_SCHEMA)=NO' <<< "$readiness"; then
    printf '\nSTOP: the named revision cannot be prepared (above). Nothing was executed.\n' >&2
    exit 1
  else
    [[ "$readiness_status" -ne 3 ]] || printf '\nNOTE: readiness is UNVERIFIED from here; the preparation itself is idempotent in the database.\n'
    capture=("${REPO_ROOT}/scripts/catalog/government-production-capture.sh" "${CONFIG_ARG[@]}")
    bash "${capture[@]}" --ensure-job --enable-catalog-execution
    ws_id=""
    for index in "${!WS_ARGS[@]}"; do
      [[ "${WS_ARGS[$index]}" == "--work-scope-id" ]] && ws_id="${WS_ARGS[$((index + 1))]}"
    done
    prepared="$(bash "${capture[@]}" --prepare --enable-catalog-execution \
      --idempotency-key "$(attempt_key "work-scope-${ws_id:0:8}")" | tee /dev/stderr \
      | awk -F= '$1 == "PREPARED_RUN_ID" { print $2 }' | tail -n 1)"
    if [[ ! "$prepared" =~ ^[0-9a-f-]{36}$ ]]; then
      printf 'FAIL: no operator capture run was prepared; nothing was captured.\n' >&2
      exit 1
    fi
    if ! bash "${capture[@]}" --prepare-work-scope --enable-catalog-execution \
         --enable-work-scope-preparation --run-id "$prepared" "${WS_ARGS[@]}"; then
      # A refusal BEFORE the entrypoint claims the run leaves it queued and
      # reusable. Resume with THAT run rather than preparing another: a new
      # one per attempt would pile up non-terminal runs (and count against the
      # operator's active-run cap).
      printf '\nFAIL: the preparation did not succeed (above). If it was refused before the\n' >&2
      printf '      capture run was claimed, resume with the SAME prepared run:\n' >&2
      printf '        bash scripts/catalog/government-production-capture.sh --prepare-work-scope \\\n' >&2
      printf '          --run-id %s %s \\\n' "$prepared" "${WS_ARGS[*]}" >&2
      printf '          --enable-catalog-execution --enable-work-scope-preparation\n' >&2
      exit 1
    fi
  fi
  step "6. verify (read-only, gate: prepared)"
  bash "${SCRIPT_DIR}/production-verify.sh" "${CONFIG_ARG[@]}" --gate prepared "${WS_ARGS[@]}"
fi

if [[ "$DO_WEBSITE" -eq 1 ]]; then
  step "7. Stage 2 activation plan (printed; apply with website-execution-activate.sh --apply-backend)"
  bash "${SCRIPT_DIR}/website-execution-activate.sh" --plan --skip-stage1-check "${CONFIG_ARG[@]}"
fi

printf '\nDONE. No run was started, nothing paid was enabled and no provider call was made.\n'
if [[ "$DO_PREPARE" -eq 0 && "$DO_WEBSITE" -eq 0 ]]; then
  printf 'Next (see docs/production-readiness/SCOPED_BATCH_PRODUCTION_RUNBOOK.md):\n'
  printf '  Stage P  website-execution-activate.sh --apply-plan-authoring, then author the plan\n'
  printf '  Stage D  production-activate.sh --prepare-work-scope --work-scope-id ... \\\n'
  printf '             --enable-catalog-execution --enable-work-scope-preparation\n'
fi

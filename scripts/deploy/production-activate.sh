#!/usr/bin/env bash
# Thin orchestration over the activation bundle.
#
# It owns no logic of its own: every step below is the canonical tool for that
# step, invoked with the operator's configuration. It exists so the operator
# types one command per intent instead of five with matching flags.
#
# It deliberately does NOT hide failures. Each step's output is passed through
# and the first failing step stops the sequence, because a capture that failed
# is not a reason to deploy and a deployment that failed is not a reason to
# verify.
#
# It never starts a MILO run, never enables catalog promotion, and never
# enables the website execution stage. Arming the website is a separate,
# explicit operator decision documented in the runbook.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

DO_PREFLIGHT=0 DO_CAPTURE=0 DO_DEPLOY=0 DO_VERIFY=0 DO_WEBSITE=0 PLAN_ONLY=0
CONFIG_ARG=() ENABLE_CATALOG=0

usage() {
  cat << 'EOF'
Usage: production-activate.sh [steps] [options]

Steps (combine freely; --plan shows what each would do and changes nothing):
  --plan        Read-only. Preflight plus the capture plan. No mutation.
  --preflight   Read-only preflight only.
  --capture     Government capture (requires --enable-catalog-execution).
  --deploy      Deploy the current commit to API + worker.
  --verify      Read-only post-deployment verification.
  --website     Stage 2: print/apply the website execution activation.
  --all         preflight, capture, deploy, verify — in that order.
                Deliberately STOPS before --website: opening the composer is a
                separate decision made after reading the verify output.

Options:
  --enable-catalog-execution  Required by --capture and --all. Supplies the
                              catalog master switch value for the capture job.
  --operator-config <path>    Operator identifier file.
  --help

This never launches a paid run. After it succeeds, arming the website is a
separate step: see docs/production-readiness/PRODUCTION_ACTIVATION_RUNBOOK.md.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --plan) PLAN_ONLY=1; shift ;;
    --preflight) DO_PREFLIGHT=1; shift ;;
    --capture) DO_CAPTURE=1; shift ;;
    --deploy) DO_DEPLOY=1; shift ;;
    --verify) DO_VERIFY=1; shift ;;
    --website) DO_WEBSITE=1; shift ;;
    --all) DO_PREFLIGHT=1; DO_CAPTURE=1; DO_DEPLOY=1; DO_VERIFY=1; shift ;;
    --enable-catalog-execution) ENABLE_CATALOG=1; shift ;;
    --operator-config) CONFIG_ARG=(--operator-config "${2:?}"); shift 2 ;;
    --help) usage; exit 0 ;;
    *) printf 'FAIL: unknown argument %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ "$PLAN_ONLY" -eq 0 && "$DO_PREFLIGHT" -eq 0 && "$DO_CAPTURE" -eq 0 \
      && "$DO_DEPLOY" -eq 0 && "$DO_VERIFY" -eq 0 && "$DO_WEBSITE" -eq 0 ]]; then
  printf 'FAIL: choose at least one step (or --plan)\n' >&2
  usage >&2
  exit 2
fi
if [[ "$DO_CAPTURE" -eq 1 && "$ENABLE_CATALOG" -eq 0 ]]; then
  printf 'FAIL: --capture requires --enable-catalog-execution\n' >&2
  exit 2
fi

step() { printf '\n========== %s ==========\n' "$1"; }

if [[ "$PLAN_ONLY" -eq 1 ]]; then
  step "preflight (read-only)"
  bash "${SCRIPT_DIR}/production-preflight.sh" "${CONFIG_ARG[@]}"
  step "government capture plan (read-only)"
  bash "${REPO_ROOT}/scripts/catalog/government-production-capture.sh" --plan "${CONFIG_ARG[@]}"
  step "deployment plan (read-only)"
  printf 'DEPLOY_MODE=check bash scripts/deploy/cloud-run.sh\n'
  DEPLOY_MODE=check bash "${SCRIPT_DIR}/cloud-run.sh"
  step "execution gate chain (reference)"
  python3 "${REPO_ROOT}/scripts/release/execution_gate_chain.py"
  step "website activation plan (read-only)"
  bash "${SCRIPT_DIR}/website-execution-activate.sh" --plan --skip-stage1-check "${CONFIG_ARG[@]}" || true
  printf '\nPLAN COMPLETE — nothing was mutated.\n'
  exit 0
fi

if [[ "$DO_PREFLIGHT" -eq 1 ]]; then
  step "preflight"
  bash "${SCRIPT_DIR}/production-preflight.sh" "${CONFIG_ARG[@]}"
fi

if [[ "$DO_CAPTURE" -eq 1 ]]; then
  step "government capture"
  bash "${REPO_ROOT}/scripts/catalog/government-production-capture.sh" \
    --all --enable-catalog-execution "${CONFIG_ARG[@]}"
fi

if [[ "$DO_DEPLOY" -eq 1 ]]; then
  step "deploy (API + worker)"
  DEPLOY_MODE=apply bash "${SCRIPT_DIR}/cloud-run.sh"
fi

if [[ "$DO_VERIFY" -eq 1 ]]; then
  step "verify"
  bash "${SCRIPT_DIR}/production-verify.sh" "${CONFIG_ARG[@]}"
fi

# Stage 2 is never bundled with the capture. They are separate decisions made
# at separate times: the capture lands data, the website activation exposes a
# paid surface to a human. website-execution-activate.sh re-runs the Stage 1
# gate itself and refuses if it does not pass, so this ordering is enforced
# rather than merely documented.
if [[ "$DO_WEBSITE" -eq 1 ]]; then
  if [[ "$DO_CAPTURE" -eq 1 ]]; then
    printf 'FAIL: --website may not be combined with --capture. Land and verify the\n' >&2
    printf '      snapshot first, read the verify output, then activate separately.\n' >&2
    exit 2
  fi
  step "website execution activation (Stage 2)"
  bash "${SCRIPT_DIR}/website-execution-activate.sh" "${CONFIG_ARG[@]}"
  step "website verification"
  bash "${SCRIPT_DIR}/website-execution-check.sh" "${CONFIG_ARG[@]}" || true
fi

printf '\nDONE. No run was started and no provider call was made.\n'
if [[ "$DO_WEBSITE" -eq 0 ]]; then
  printf 'Next: read the verify output, then arm the website with --website\n'
  printf '      (see docs/production-readiness/PRODUCTION_ACTIVATION_RUNBOOK.md).\n'
else
  printf 'The website stage is open only once the Vercel half above is applied AND\n'
  printf 'the frontend is rebuilt. The first paid run remains yours to start.\n'
fi

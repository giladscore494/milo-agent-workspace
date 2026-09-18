#!/usr/bin/env bash
# Stage D step 2: deploy the release images. FLAGS STAY OFF — this step
# changes images only (worker job before API, per DEPLOYMENT.md), using
# non-destructive update forms. Idempotent: production already serves the
# pinned SHA, so the updates are expected to be no-ops.
#
# Two read-only gates run BEFORE the first production mutation and are
# REPEATED after deployment:
#   1. the exact VISIBLE Worker-execution baseline (exactly
#      STAGE_D_EXPECTED_PRIOR_EXECUTIONS terminal executions, zero
#      active/unverifiable — structured JSON via verify_executions.py);
#   2. the fail-closed posture: worker and API MILO_ENABLE_PAID_EXECUTION
#      must be the LITERAL value "false" — present, exact, and never a
#      Secret Manager binding (no absent-flag fallback); API run creation
#      false and launcher disabled; catalog execution false on BOTH
#      surfaces; and NEITHER surface carries KIMI_API_KEY or
#      MOONSHOT_API_KEY as a literal env value or a Secret Manager binding.
# A failed pre-gate means nothing is mutated; a failed post-gate means the
# deploy is BLOCKED before proceeding to step 3. Names/flags only — secret
# VALUES are never read or printed.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
# shellcheck source=stage-d-env.sh
source ./stage-d-env.sh

# Every provider-key alias the Worker accepts (see kill-switch.sh). Both
# must be absent from BOTH surfaces while Stage D is not enabled.
PROVIDER_SECRET_ALIASES=("KIMI_API_KEY" "MOONSHOT_API_KEY")

verify_execution_baseline() { # LABEL
  echo "== ${1}: exact execution baseline (${STAGE_D_EXPECTED_PRIOR_EXECUTIONS} terminal, 0 active)"
  # Exactly the pinned VISIBLE prior terminal executions; none
  # active/unverifiable. Count mismatches (in either direction) and
  # unparseable listings fail closed. This gate never mutates executions.
  gcloud run jobs executions list --job="${STAGE_D_WORKER_JOB}" \
    --project="${STAGE_D_PROJECT}" --region="${STAGE_D_REGION}" --format=json \
    | python3 ./verify_executions.py --expected-total "${STAGE_D_EXPECTED_PRIOR_EXECUTIONS}" \
    || { echo "BLOCKED (${1}): worker execution posture does not match the pinned live baseline (${STAGE_D_EXPECTED_PRIOR_EXECUTIONS} terminal, 0 active)"; exit 1; }
}

verify_fail_closed_posture() { # LABEL — names/flags only; no secret values
  echo "== ${1}: fail-closed flag/provider-secret posture"
  gcloud run jobs describe "${STAGE_D_WORKER_JOB}" \
    --project="${STAGE_D_PROJECT}" --region="${STAGE_D_REGION}" --format=json \
    | python3 -c '
import json, sys
aliases = sys.argv[1:]
container = json.load(sys.stdin)["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]
env = container.get("env") or []
values = {e["name"]: e.get("value") for e in env if "value" in e}
secret_refs = {e["name"] for e in env if "valueFrom" in e}
# The paid flag must be a LITERAL, exactly "false" — never a secret
# binding (an operator cannot audit a bound value by name) and never
# absent-with-fallback.
assert "MILO_ENABLE_PAID_EXECUTION" not in secret_refs, "worker MILO_ENABLE_PAID_EXECUTION is supplied via a secret binding"
assert values.get("MILO_ENABLE_PAID_EXECUTION") == "false", "worker MILO_ENABLE_PAID_EXECUTION is not the literal value false"
assert values.get("MILO_ENABLE_CATALOG_EXECUTION") == "false", "worker MILO_ENABLE_CATALOG_EXECUTION is not the literal value false"
for alias in aliases:
    assert alias not in secret_refs, f"provider secret {alias} bound to the worker"
    assert alias not in values, f"provider variable {alias} present on the worker"
print("OK: worker fail-closed, catalog execution off, no provider alias present")
' "${PROVIDER_SECRET_ALIASES[@]}" \
    || { echo "BLOCKED (${1}): worker is not fail-closed (paid flag / catalog flag / provider alias)"; exit 1; }
  gcloud run services describe "${STAGE_D_API_SERVICE}" \
    --project="${STAGE_D_PROJECT}" --region="${STAGE_D_REGION}" --format=json \
    | python3 -c '
import json, sys
aliases = sys.argv[1:]
container = json.load(sys.stdin)["spec"]["template"]["spec"]["containers"][0]
env = container.get("env") or []
values = {e["name"]: e.get("value") for e in env if "value" in e}
secret_refs = {e["name"] for e in env if "valueFrom" in e}
assert values.get("MILO_ENABLE_RUN_CREATION") == "false", "MILO_ENABLE_RUN_CREATION is not false"
assert values.get("JOB_LAUNCHER") == "disabled", "JOB_LAUNCHER is not disabled"
# Same literal-only rule as the worker: no binding, no fallback.
assert "MILO_ENABLE_PAID_EXECUTION" not in secret_refs, "API MILO_ENABLE_PAID_EXECUTION is supplied via a secret binding"
assert values.get("MILO_ENABLE_PAID_EXECUTION") == "false", "API MILO_ENABLE_PAID_EXECUTION is not the literal value false"
assert values.get("MILO_ENABLE_CATALOG_EXECUTION") == "false", "API MILO_ENABLE_CATALOG_EXECUTION is not the literal value false"
for alias in aliases:
    assert alias not in secret_refs, f"provider secret {alias} bound to the API"
    assert alias not in values, f"provider variable {alias} present on the API"
print("OK: API fail-closed, catalog execution off, no provider alias present")
' "${PROVIDER_SECRET_ALIASES[@]}" \
    || { echo "BLOCKED (${1}): API is not fail-closed (run creation / launcher / paid flag / catalog flag / provider alias)"; exit 1; }
}

# -- Pre-mutation gates: NOTHING is deployed unless both pass.
verify_execution_baseline "pre-deploy"
verify_fail_closed_posture "pre-deploy"

echo "== Worker job image -> ${STAGE_D_RELEASE_SHA}"
gcloud run jobs update "${STAGE_D_WORKER_JOB}" \
  --project="${STAGE_D_PROJECT}" --region="${STAGE_D_REGION}" \
  --image="${STAGE_D_REGISTRY}/worker:${STAGE_D_RELEASE_SHA}"

echo "== API service image -> ${STAGE_D_RELEASE_SHA}"
gcloud run services update "${STAGE_D_API_SERVICE}" \
  --project="${STAGE_D_PROJECT}" --region="${STAGE_D_REGION}" \
  --image="${STAGE_D_REGISTRY}/api:${STAGE_D_RELEASE_SHA}"

echo "== Post-deploy verification"
ready="$(gcloud run services describe "${STAGE_D_API_SERVICE}" \
  --project="${STAGE_D_PROJECT}" --region="${STAGE_D_REGION}" \
  --format='value(status.conditions[0].status)')"
test "${ready}" = "True" || { echo "BLOCKED: API service not Ready"; exit 1; }

# -- Post-deploy gates: the image update must not have changed the
# execution baseline or the fail-closed posture.
verify_execution_baseline "post-deploy"
verify_fail_closed_posture "post-deploy"

echo "OK: release images deployed, execution still fully disabled, execution baseline matches (${STAGE_D_EXPECTED_PRIOR_EXECUTIONS} live terminal, 0 active)."

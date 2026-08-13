#!/usr/bin/env bash
# Shared Stage C parameters. Sourced by every step script. No secrets here.
#
# AUTHORIZED CONSTANTS — the Stage C authorization covers ONE exact
# production target and ONE exact release. Every value below is pinned:
# an inherited shell value that conflicts with a pinned constant makes
# Stage C refuse to proceed (fail closed) instead of silently widening or
# redirecting the authorization. Changing a constant requires editing this
# file in a reviewed commit, never the operator's environment.

stage_c_refuse() {
  echo "STAGE C REFUSED: $1" >&2
  exit 1
}

stage_c_pin() { # VAR AUTHORIZED_VALUE — export VAR, failing closed on conflict
  local var="$1" authorized="$2" current
  current="${!var-}"
  if [ -n "${current}" ] && [ "${current}" != "${authorized}" ]; then
    stage_c_refuse "inherited environment override ${var}='${current}' conflicts with the authorized value '${authorized}' — unset it; the authorization cannot be redirected from the shell"
  fi
  export "${var}=${authorized}"
}

# The one authorized production target and release.
stage_c_pin STAGE_C_PROJECT "big-cabinet-457321-t7"
stage_c_pin STAGE_C_REGION "us-central1"
stage_c_pin STAGE_C_RELEASE_SHA "30b05bc45d6f9372261e4fac20cd983c69db971f"
stage_c_pin STAGE_C_REPO_URL "https://github.com/giladscore494/milo-agent-workspace.git"
stage_c_pin STAGE_C_REGISTRY "us-central1-docker.pkg.dev/${STAGE_C_PROJECT}/milo-agent"
stage_c_pin STAGE_C_API_SERVICE "milo-agent-api"
stage_c_pin STAGE_C_WORKER_JOB "milo-agent-worker"
stage_c_pin STAGE_C_API_SA "milo-api-runtime@${STAGE_C_PROJECT}.iam.gserviceaccount.com"
stage_c_pin STAGE_C_WORKER_SA "milo-worker-runtime@${STAGE_C_PROJECT}.iam.gserviceaccount.com"
stage_c_pin STAGE_C_GATEWAY_SA "milo-vercel-gateway@${STAGE_C_PROJECT}.iam.gserviceaccount.com"
stage_c_pin STAGE_C_API_URL "https://milo-agent-api-beplbca7yq-uc.a.run.app"
stage_c_pin STAGE_C_DB_PROBE_JOB "stagec-db-probe"
stage_c_pin STAGE_C_GW_PROBE_JOB "stagec-gw-probe"

# The ONE authorized run identity and its acceptance policy.
stage_c_pin STAGE_C_IDEMPOTENCY_KEY "stage-c-smoke-0001"
# Exact number of runs that may already exist before the authorized run is
# created (preflight fails closed on any other exact count).
stage_c_pin STAGE_C_EXPECTED_PRIOR_RUNS "0"
# The ONLY terminal state that counts as a PASS. failed / cancelled /
# timed_out / budget_exhausted / partial_success are controlled fail-closed
# terminals: they prove the safety rails but FAIL the smoke test, trigger a
# non-zero probe exit and require the kill switch + investigation.
stage_c_pin STAGE_C_ACCEPTABLE_TERMINAL_STATES "completed"

# Flag-enable value referenced ONLY by the manual operator commands in
# 03-enable-stage-c.md; no committed line pairs an execution-flag name with
# an enabled value (policy: scripts/check_unsafe_defaults.py, and
# STAGED_ACTIVATION.md's no-one-command-enable rule — enabling remains a
# deliberate manual operator action).
export STAGE_C_ON="true"

# Smallest-safe caps for the single controlled run (rationale in
# docs/production-readiness/STAGE_C_ACCEPTANCE.md). Comma-separated for
# gcloud --update-env-vars. Pinned: an inherited STAGE_C_CAPS cannot loosen
# a cap.
stage_c_pin STAGE_C_CAPS "MILO_MAX_MODEL_CALLS_PER_RUN=200,MILO_MAX_INPUT_TOKENS_PER_RUN=700000,MILO_MAX_OUTPUT_TOKENS_PER_RUN=250000,MILO_MAX_TOTAL_TOKENS_PER_RUN=900000,MILO_MAX_ESTIMATED_COST_PER_RUN=4.00,MILO_MAX_COST_PER_RUN=3.00,MILO_MAX_RUN_DURATION_SECONDS=3300,MILO_MAX_RETRIES=15,MILO_MAX_AGENT_STEPS=60,MILO_MAX_CONCURRENT_RUNS_PER_USER=1,MILO_MAX_CONCURRENT_RUNS_PER_PROJECT=1,MILO_DAILY_USER_BUDGET=5.00,MILO_DAILY_PROJECT_BUDGET=5.00,MILO_ESTIMATED_COST_PER_CALL=0.02"

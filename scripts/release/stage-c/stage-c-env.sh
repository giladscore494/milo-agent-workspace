#!/usr/bin/env bash
# Shared Stage C parameters. Sourced by every step script. No secrets here.
export STAGE_C_PROJECT="${STAGE_C_PROJECT:-big-cabinet-457321-t7}"
export STAGE_C_REGION="${STAGE_C_REGION:-us-central1}"
export STAGE_C_RELEASE_SHA="${STAGE_C_RELEASE_SHA:-30b05bc45d6f9372261e4fac20cd983c69db971f}"
export STAGE_C_REPO_URL="${STAGE_C_REPO_URL:-https://github.com/giladscore494/milo-agent-workspace.git}"
export STAGE_C_REGISTRY="${STAGE_C_REGISTRY:-us-central1-docker.pkg.dev/${STAGE_C_PROJECT}/milo-agent}"
export STAGE_C_API_SERVICE="${STAGE_C_API_SERVICE:-milo-agent-api}"
export STAGE_C_WORKER_JOB="${STAGE_C_WORKER_JOB:-milo-agent-worker}"
export STAGE_C_API_SA="milo-api-runtime@${STAGE_C_PROJECT}.iam.gserviceaccount.com"
export STAGE_C_WORKER_SA="milo-worker-runtime@${STAGE_C_PROJECT}.iam.gserviceaccount.com"
export STAGE_C_GATEWAY_SA="milo-vercel-gateway@${STAGE_C_PROJECT}.iam.gserviceaccount.com"
export STAGE_C_API_URL="${STAGE_C_API_URL:-https://milo-agent-api-beplbca7yq-uc.a.run.app}"
export STAGE_C_DB_PROBE_JOB="stagec-db-probe"
export STAGE_C_GW_PROBE_JOB="stagec-gw-probe"

# The ONE authorized run identity and its acceptance policy.
export STAGE_C_IDEMPOTENCY_KEY="${STAGE_C_IDEMPOTENCY_KEY:-stage-c-smoke-0001}"
# Exact number of runs that may already exist before the authorized run is
# created (preflight fails closed on any other exact count).
export STAGE_C_EXPECTED_PRIOR_RUNS="${STAGE_C_EXPECTED_PRIOR_RUNS:-0}"
# The ONLY terminal state(s) that count as a PASS. failed / cancelled /
# timed_out / budget_exhausted / partial_success are controlled fail-closed
# terminals: they prove the safety rails but FAIL the smoke test, trigger a
# non-zero probe exit and require the kill switch + investigation.
export STAGE_C_ACCEPTABLE_TERMINAL_STATES="${STAGE_C_ACCEPTABLE_TERMINAL_STATES:-completed}"

# Flag-enable value referenced ONLY by the manual operator commands in
# 03-enable-stage-c.md; no committed line pairs an execution-flag name with
# an enabled value (policy: scripts/check_unsafe_defaults.py, and
# STAGED_ACTIVATION.md's no-one-command-enable rule — enabling remains a
# deliberate manual operator action).
export STAGE_C_ON="true"

# Smallest-safe caps for the single controlled run (rationale in
# docs/production-readiness/STAGE_C_ACCEPTANCE.md). Comma-separated for
# gcloud --update-env-vars.
export STAGE_C_CAPS="MILO_MAX_MODEL_CALLS_PER_RUN=200,MILO_MAX_INPUT_TOKENS_PER_RUN=700000,MILO_MAX_OUTPUT_TOKENS_PER_RUN=250000,MILO_MAX_TOTAL_TOKENS_PER_RUN=900000,MILO_MAX_ESTIMATED_COST_PER_RUN=4.00,MILO_MAX_COST_PER_RUN=3.00,MILO_MAX_RUN_DURATION_SECONDS=3300,MILO_MAX_RETRIES=15,MILO_MAX_AGENT_STEPS=60,MILO_MAX_CONCURRENT_RUNS_PER_USER=1,MILO_MAX_CONCURRENT_RUNS_PER_PROJECT=1,MILO_DAILY_USER_BUDGET=5.00,MILO_DAILY_PROJECT_BUDGET=5.00,MILO_ESTIMATED_COST_PER_CALL=0.02"

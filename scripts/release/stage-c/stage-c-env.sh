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
#
# STAGE_C_RELEASE_SHA is the RUNTIME release: the merge commit of the
# reviewed PR #50 runtime changes that Cloud Build clones and checks out
# to build the api/worker images. It is deliberately NOT the merge commit
# of the release-preparation PR that edits this tooling — pointing the pin
# at the tooling PR's own merge would be a self-referential pin and would
# ship unreviewed-at-pin-time runtime code.
stage_c_pin STAGE_C_PROJECT "big-cabinet-457321-t7"
stage_c_pin STAGE_C_REGION "us-central1"
stage_c_pin STAGE_C_RELEASE_SHA "88224bccc836f80f3dc1d173306a1aa63cddcc7a"
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

# The ONE authorized run identity and its acceptance policy. Attempt 7
# uses a fresh, unambiguous idempotency key — `stage-c-smoke-0001` was the
# Attempt 5/6 identity and is never reused; only zero pre-existing rows
# under the new key are acceptable.
stage_c_pin STAGE_C_IDEMPOTENCY_KEY "stage-c-smoke-attempt-7-20260819"
# Exact HISTORICAL baseline that must already exist before the Attempt 7
# run is created (preflight fails closed on any other exact count):
#   - 2 prior database runs: Attempt 5 (58daa7de-…, failed at the
#     claim_run_lease ACL before any provider call) and Attempt 6
#     (37912575-…, real paid execution, terminal `failed` after
#     RETRY_LIMIT_REACHED). Neither is deleted, rewritten or hidden.
#   - 2 prior Worker executions (both terminal): Attempt 5's and
#     Attempt 6's. Attempt 6's authorization is consumed.
# Evidence gates verify a ONE-run/ONE-execution increment over these
# exact baselines (expected post-run totals: 3 and 3), never an empty
# system and never "some run exists".
stage_c_pin STAGE_C_EXPECTED_PRIOR_RUNS "2"
stage_c_pin STAGE_C_EXPECTED_PRIOR_EXECUTIONS "2"
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

# WORKER-ONLY provider operating envelope (Attempt 7). The production Kimi
# organization is operator-confirmed Tier 2 (concurrency 100 / RPM 500 /
# TPM 3,000,000 / TPD unlimited); this envelope is deliberately far below
# that ceiling. Concurrency stays 2 on purpose — it matches the preserved
# V1 engine parallelism (core.MAX_PARALLEL_KIMI_CALLS) and no V2
# concurrency is introduced. Kept SEPARATE from STAGE_C_CAPS because
# STAGE_C_CAPS is intentionally applied and verified on BOTH the API and
# the Worker, while provider scheduling configuration belongs to the
# Worker alone (verify_caps.py fails on any MILO_PROVIDER_* variable on
# the API). Pinned: an inherited override cannot loosen the envelope.
stage_c_pin STAGE_C_WORKER_PROVIDER_LIMITS "MILO_PROVIDER_MAX_CONCURRENCY=2,MILO_PROVIDER_RPM_LIMIT=350,MILO_PROVIDER_TPM_LIMIT=2400000,MILO_PROVIDER_MAX_RATE_LIMIT_RETRIES=5,MILO_PROVIDER_MAX_BACKPRESSURE_WAIT_SECONDS=240,MILO_PROVIDER_BACKOFF_BASE_SECONDS=2,MILO_PROVIDER_BACKOFF_MAX_SECONDS=30"

#!/usr/bin/env bash
# Shared Stage D parameters. Sourced by every step script. No secrets here.
#
# STATUS: PROPOSED authorization. Nothing in this directory has been
# executed against production. Merging it authorizes nothing — the one
# bounded paid run it describes requires a fresh, explicit, separate
# operator authorization (STAGED_ACTIVATION.md, Stage D).
#
# AUTHORIZED CONSTANTS — the proposed Stage D authorization covers ONE
# exact production target, ONE exact release and exactly ONE new paid run.
# Every value below is pinned: an inherited shell value that conflicts with
# a pinned constant makes Stage D refuse to proceed (fail closed) instead of
# silently widening or redirecting the authorization. Changing a constant
# requires editing this file in a reviewed commit, never the operator's
# environment.
#
# The consumed Stage C constants (scripts/release/stage-c/stage-c-env.sh)
# are NEVER sourced, reused or edited from here. Stage C's Attempt 7
# authorization is spent; this file is a separate, self-contained
# authorization surface with its own namespace (STAGE_D_*), its own fresh
# idempotency key and its own live baselines.

stage_d_refuse() {
  echo "STAGE D REFUSED: $1" >&2
  exit 1
}

stage_d_pin() { # VAR AUTHORIZED_VALUE — export VAR, failing closed on conflict
  local var="$1" authorized="$2" current
  current="${!var-}"
  if [ -n "${current}" ] && [ "${current}" != "${authorized}" ]; then
    stage_d_refuse "inherited environment override ${var}='${current}' conflicts with the authorized value '${authorized}' — unset it; the authorization cannot be redirected from the shell"
  fi
  export "${var}=${authorized}"
}

# ---------------------------------------------------------------------------
# The one authorized production target and release.
#
# STAGE_D_RELEASE_SHA is the reviewed main commit that production ALREADY
# serves on both surfaces. It names the release; the digests below ARE the
# release. Stage D verifies both and builds neither. The SHA is
# deliberately NOT the merge commit of this preparation PR: pinning the
# pin's own merge would be self-referential and would ship runtime code
# that was unreviewed at pin time.
# ---------------------------------------------------------------------------
stage_d_pin STAGE_D_PROJECT "big-cabinet-457321-t7"
stage_d_pin STAGE_D_REGION "us-central1"
stage_d_pin STAGE_D_RELEASE_SHA "84cd8696119c24662a954d0f0e23195268dab23f"
stage_d_pin STAGE_D_REGISTRY "us-central1-docker.pkg.dev/${STAGE_D_PROJECT}/milo-agent"

# ---------------------------------------------------------------------------
# The IMMUTABLE accepted image digests — the real identity of the release.
#
# Stage D NEVER rebuilds and NEVER redeploys. It verifies these digests
# read-only and BLOCKS on any mismatch, because a rebuild of the same Git
# SHA is NOT guaranteed to reproduce the same image bytes in this
# repository:
#
#   * Dockerfile.api and Dockerfile.worker both start FROM the MUTABLE base
#     tag `python:3.12-slim`, which upstream re-publishes;
#   * backend/requirements.txt pins most packages but carries
#     `openai>=1.30.0`, an unpinned floor that resolves to whatever is
#     newest at build time;
#   * there is no lockfile and no --require-hashes, so transitive
#     dependencies float too.
#
# So `docker build` at commit 84cd8696… today can produce different bytes
# than the accepted build did, and pushing them would silently move the
# mutable Artifact Registry tag `:84cd8696…` onto a DIFFERENT image while
# every tag-based check still "passed". This is not hypothetical here: the
# Worker tag has already resolved to several distinct digests over this
# project's history (e.g. execution milo-agent-worker-bw8kj ran
# sha256:2314852868a8…, which is NOT the digest below).
#
# A tag match is therefore NOT acceptance. The digest is.
#
# Verified read-only against Artifact Registry on 2026-09-18: the tag
# 84cd8696119c24662a954d0f0e23195268dab23f currently resolves to exactly
# these digests, and the serving API revision milo-agent-api-00080-nm8
# runs the pinned API digest.
#
# Changing either digest is a NEW RELEASE and requires its own review. It
# is never a Stage D action.
# ---------------------------------------------------------------------------
stage_d_pin STAGE_D_API_IMAGE_DIGEST "sha256:04275e81995d7bbaf23d0e71e71c2ac83adf37f45eca8686ddb812050a18caa6"
stage_d_pin STAGE_D_WORKER_IMAGE_DIGEST "sha256:d3743e5a8dabc3f663970abe83886ea91b030ad7b339e1178d0ab5efad8f64b5"
stage_d_pin STAGE_D_API_SERVICE "milo-agent-api"
stage_d_pin STAGE_D_WORKER_JOB "milo-agent-worker"
stage_d_pin STAGE_D_API_SA "milo-api-runtime@${STAGE_D_PROJECT}.iam.gserviceaccount.com"
stage_d_pin STAGE_D_WORKER_SA "milo-worker-runtime@${STAGE_D_PROJECT}.iam.gserviceaccount.com"
stage_d_pin STAGE_D_GATEWAY_SA "milo-vercel-gateway@${STAGE_D_PROJECT}.iam.gserviceaccount.com"
stage_d_pin STAGE_D_API_URL "https://milo-agent-api-beplbca7yq-uc.a.run.app"
stage_d_pin STAGE_D_DB_PROBE_JOB "stage-d-db-probe"
stage_d_pin STAGE_D_GW_PROBE_JOB "stage-d-gw-probe"

# ---------------------------------------------------------------------------
# The PRIVILEGED probe runtime — pinned by digest, never by tag.
#
# stage-d-db-probe runs with SUPABASE_SERVICE_ROLE_KEY bound, so whatever
# image it runs executes arbitrary code with service-role access to
# production. An earlier revision created both probes from the MUTABLE tag
# `python:3.12-slim`, which Docker Hub re-publishes: the job could have
# begun executing different code with those credentials between one
# execution and the next, with nothing noticing.
#
# The digest below is `python:3.12-slim` as it resolved on 2026-09-19,
# read-only, from the registry API. It is an OCI image index carrying a
# linux/amd64 manifest, which is what Cloud Run pulls.
#
# The repository is spelled CANONICALLY (docker.io/library/python, not
# the shorthand `python`) so that Cloud Run's own normalisation of the
# stored template cannot make the exact-template check fail spuriously.
# verify_probe_jobs.py normalises both sides anyway, so the digest — not
# the spelling — is what is enforced.
#
# MIRROR POSTURE. An approved Artifact Registry mirror is preferable to
# pulling a privileged runtime from a public registry. No mirror exists
# today: the project has exactly one Artifact Registry repository,
# `milo-agent`, and it is a STANDARD repository, not a REMOTE one
# (verified read-only 2026-09-19). Creating one is a production mutation
# and is deliberately outside this PR. Switching to it later is a
# ONE-LINE reviewed change to STAGE_D_PROBE_IMAGE_REPO and nothing else,
# because mirroring preserves the manifest digest — the pin below stays
# byte-identical either way. Until then the digest pin is what makes the
# public pull safe.
# ---------------------------------------------------------------------------
stage_d_pin STAGE_D_PROBE_IMAGE_REPO "docker.io/library/python"
stage_d_pin STAGE_D_PROBE_IMAGE_DIGEST "sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea"

# ---------------------------------------------------------------------------
# The reviewed probe SOURCE, pinned by content hash.
#
# 04-create-probes.sh transports whatever probe_db.py / probe_gateway.py
# happen to be on disk into a job that holds production credentials. A
# dirty working tree, a bad merge or an edited checkout would therefore
# ship unreviewed code with service-role access. The encoder compares
# these SHA-256 hashes BEFORE any gcloud mutation and refuses on any
# mismatch.
#
# Regenerate deliberately, in a reviewed commit, after an intended change:
#   sha256sum scripts/release/stage-d/probe_db.py scripts/release/stage-d/probe_gateway.py
# ---------------------------------------------------------------------------
stage_d_pin STAGE_D_PROBE_DB_SHA256 "fab71a9e3af6108c4475359cde20e27f035967e56a46bf1dfe3517a45967fa62"
stage_d_pin STAGE_D_PROBE_GW_SHA256 "359d7cbfc7195f9fee333480af0f7fe1ae09ca8ce339a0a7942bfe823dde4bce"

# ---------------------------------------------------------------------------
# The ONE authorized run identity.
#
# A brand-new key. Every key production has ever seen is consumed history
# and is never reused: stage-c-smoke-0001 (Attempt 5/6),
# stage-c-smoke-attempt-7-20260819 (Attempt 7 — Stage C PASSED, consumed),
# the four swarm-v2-smoke-* keys, and the Government capture key. Only zero
# pre-existing rows under the Stage D key are acceptable.
# ---------------------------------------------------------------------------
stage_d_pin STAGE_D_IDEMPOTENCY_KEY "stage-d-expansion-1-20260918-01"

# ---------------------------------------------------------------------------
# Exact LIVE baselines that must already hold before the Stage D run is
# created (preflight fails closed on any other exact count). Both were
# discovered read-only against production on 2026-09-18:
#
#   DATABASE — public.runs holds exactly 7 rows:
#     37912575…  failed     stage-c-smoke-0001                     (Stage C A6)
#     8b4a4277…  completed  stage-c-smoke-attempt-7-20260819       (Stage C A7)
#     0d44d491…  cancelled  swarm-v2-smoke-20260824-04c1094
#     986ac9ec…  failed     swarm-v2-smoke-attempt-2-20260824-04c1094
#     0b1b7329…  failed     swarm-v2-smoke-20260824-4fecdfe-01
#     5bd80a2e…  completed  swarm-v2-smoke-20260825-4dbdcd6-01
#     555101dc…  queued     catalog-government-capture-20260919-01 (PREPARED
#                           Government capture — NOT a Stage D run; see the
#                           Government-capture section below)
#
#   CLOUD RUN — exactly 7 Worker executions, EVERY ONE terminal, zero
#   active: milo-agent-worker-{mcfrx,gggdc,dk4xv,gnj5d,fvfcb,2tckh,bw8kj}.
#
# The gates verify a ONE-run/ONE-execution increment over these exact live
# baselines (expected post-run totals: 8 database runs and 8 visible
# terminal executions) — never an empty system, and never "some run
# exists". A count of 6 is a violation exactly like a count of 8: a row
# that vanished is as much a drift as a row that appeared.
# ---------------------------------------------------------------------------
stage_d_pin STAGE_D_EXPECTED_PRIOR_RUNS "7"
stage_d_pin STAGE_D_EXPECTED_PRIOR_EXECUTIONS "7"

# The ONLY terminal state that counts as a PASS. failed / cancelled /
# timed_out / budget_exhausted / partial_success are controlled fail-closed
# terminals: they prove the safety rails but FAIL the run, trigger a
# non-zero probe exit and require the kill switch + investigation.
stage_d_pin STAGE_D_ACCEPTABLE_TERMINAL_STATES "completed"

# ---------------------------------------------------------------------------
# The prepared Government capture run — an invariant, never a Stage D run.
#
# run_id 555101dc-46f6-4048-bd67-efccbc98f528 is an operator-prepared
# capture run (input.metadata.milo_operation = catalog.government.capture)
# resting in launch_state 'none'. That state is UNACQUIRABLE by the
# ordinary launcher (backend/repository/supabase.py try_acquire_launch
# acquires only from 'pending'/'launch_failed'), and the Worker resolves
# its target from the RUN_ID environment variable rather than polling for
# queued rows (backend/worker/main.py resolve_run_id), so no model Worker
# can reach it by accident. Stage D treats those two facts as an INVARIANT
# TO PROVE, not an assumption: every gate re-reads this row and fails
# closed unless it is still untouched (or terminally retired by
# resolve-government-capture.sh).
#
# Preparing that run is NOT authorization to capture. Stage D never
# executes the capture, never enables MILO_ENABLE_CATALOG_EXECUTION, and
# never launches a Worker against this run id.
# ---------------------------------------------------------------------------
stage_d_pin STAGE_D_GOV_CAPTURE_RUN_ID "555101dc-46f6-4048-bd67-efccbc98f528"
stage_d_pin STAGE_D_GOV_CAPTURE_KEY "catalog-government-capture-20260919-01"
# The rest of that row's identity, verified read-only 2026-09-18. The
# retirement transaction asserts ALL of it — id, key, the operator-capture
# marker, the owning conversation and the requesting user — before it
# changes anything, so a row that merely shares the id cannot be retired
# by mistake.
stage_d_pin STAGE_D_GOV_CAPTURE_OPERATION "catalog.government.capture"
stage_d_pin STAGE_D_GOV_CAPTURE_CONVERSATION_ID "79ee2539-511c-4485-b470-c5539a22eba8"
stage_d_pin STAGE_D_GOV_CAPTURE_REQUESTED_BY "35e3c271-e2f0-44f1-b69a-066f13121e56"

# ---------------------------------------------------------------------------
# The dedicated Stage D test identity.
#
# Deliberately its own project/user, separate from the Stage C smoke
# project (dc6f505b…, "Stage C smoke") AND from the Government capture's
# project (677db6c2…, "MILO Vehicle Catalog", user 35e3c271…). Two reasons,
# both load-bearing:
#
#   1. 'queued' is an ACTIVE run state
#      (backend/repository/supabase.py ACTIVE_RUN_STATES), so the prepared
#      capture row counts against MILO_MAX_CONCURRENT_RUNS_PER_USER=1 and
#      MILO_MAX_CONCURRENT_RUNS_PER_PROJECT=1 for ITS user and project.
#      Creating the Stage D run there would be refused by the concurrency
#      cap — or, worse, would make an operator want to clear the capture
#      row to get past it.
#   2. The workflow key must be vehicle_catalog_v1, because every Stage D
#      cap below is derived from Stage C Attempt 7 evidence, which is
#      evidence about THAT pipeline. A swarm_v2 project would invalidate
#      the derivation.
# ---------------------------------------------------------------------------
stage_d_pin STAGE_D_PROJECT_SLUG "stage-d-smoke"
stage_d_pin STAGE_D_TEST_EMAIL "stage-d-smoke@invalid.milo"
stage_d_pin STAGE_D_WORKFLOW_KEY "vehicle_catalog_v1"
stage_d_pin STAGE_D_FORBIDDEN_PROJECT_IDS "677db6c2-b44c-41c1-b4e1-b51229d697df,dc6f505b-9ee9-45be-986c-a2fe81b2b953,bd217f0c-29ae-4ccc-85ce-3f1a6543e30d"

# Flag-enable value referenced ONLY by the manual operator commands in
# 03-enable-stage-d.md; no committed line pairs an execution-flag name with
# an enabled value (policy: scripts/check_unsafe_defaults.py, and
# STAGED_ACTIVATION.md's no-one-command-enable rule — enabling remains a
# deliberate manual operator action).
export STAGE_D_ON="true"

# ---------------------------------------------------------------------------
# Strict caps, DERIVED FROM the successful Stage C Attempt 7 evidence.
#
# Attempt 7 (run 8b4a4277…, terminal `completed`, verified read-only
# against production 2026-09-18) actually consumed:
#   84 model calls · 277,882 input + 34,136 output = 312,018 tokens ·
#   tracked actual_cost $0.252069 · estimated_cost $1.68 · 32 agent steps ·
#   934.235s elapsed · 0 retries · 0 backpressure events ·
#   84 reservations, 84 settled, 0 dangling.
#
# Derivation rule: cap = smallest round value >= 1.75x the Attempt 7
# observation, and NEVER above the Stage C cap. Every value below is
# therefore lower than or equal to Stage C's; NOTHING is increased.
# Two deliberate exceptions to the 1.75x rule, both documented:
#
#   MILO_MAX_OUTPUT_TOKENS_PER_RUN keeps a 3.5x margin instead of 1.75x.
#   Output volume is the most variable dimension of the preserved pipeline
#   (verifier chunks plus the Hebrew summary), and Attempt 7's 34,136 is a
#   single small observation — a 1.75x cap on it would be a likely
#   false `budget_exhausted`, which wastes the one authorization.
#
#   MILO_MAX_RETRIES is HELD at 15, not tightened, although Attempt 7 used
#   0. Stage C Attempt 6 FAILED at RETRY_LIMIT_REACHED after repeated
#   provider 429s; 15 retries TOGETHER WITH the worker-only provider
#   envelope below is the pair that produced the successful Attempt 7.
#   Tightening the retry allowance would reintroduce the Attempt 6 failure
#   mode for no exposure benefit — the cost caps, not the retry count,
#   bound spend.
#
# | Variable                          | Stage C | A7 actual | Stage D | delta |
# | MILO_MAX_MODEL_CALLS_PER_RUN      |     200 |        84 |     150 |  -25% |
# | MILO_MAX_INPUT_TOKENS_PER_RUN     |  700000 |   277,882 |  500000 |  -29% |
# | MILO_MAX_OUTPUT_TOKENS_PER_RUN    |  250000 |    34,136 |  120000 |  -52% |
# | MILO_MAX_TOTAL_TOKENS_PER_RUN     |  900000 |   312,018 |  600000 |  -33% |
# | MILO_MAX_ESTIMATED_COST_PER_RUN   |    4.00 |      1.68 |    3.00 |  -25% |
# | MILO_MAX_COST_PER_RUN             |    3.00 |  0.252069 |    1.00 |  -67% |
# | MILO_MAX_RUN_DURATION_SECONDS     |    3300 |   934.235 |    1800 |  -45% |
# | MILO_MAX_RETRIES                  |      15 |         0 |      15 |  hold |
# | MILO_MAX_AGENT_STEPS              |      60 |        32 |      56 |   -7% |
# | MILO_MAX_CONCURRENT_RUNS_PER_*    |       1 |         - |       1 |  hold |
# | MILO_DAILY_{USER,PROJECT}_BUDGET  |    5.00 |         - |    4.00 |  -20% |
# | MILO_ESTIMATED_COST_PER_CALL      |    0.02 |         - |    0.02 |  hold |
#
# Structural invariants the numbers above preserve:
#   * MILO_MAX_ESTIMATED_COST_PER_RUN = 150 x 0.02 = 3.00 exactly, so the
#     estimated-cost ceiling admits exactly the 150 reservations the call
#     cap allows and not one more (backend/budget.py checks
#     estimated_cost + estimated_cost_per_call > cap).
#   * MILO_MAX_TOTAL_TOKENS_PER_RUN (600000) sits just under input+output
#     (620000), so the joint ceiling binds first — the same relationship
#     Stage C used (900000 < 700000+250000).
#   * The daily budgets (4.00) stay ABOVE the 3.00 estimated-reservation
#     ceiling, so a daily budget can never fail the run before the
#     per-run cap does.
#   * MILO_MAX_RUN_DURATION_SECONDS (1800) stays far below the Cloud Run
#     worker job timeoutSeconds of 3600.
#
# Comma-separated for gcloud --update-env-vars. Pinned: an inherited
# STAGE_D_CAPS cannot loosen a cap.
# ---------------------------------------------------------------------------
stage_d_pin STAGE_D_CAPS "MILO_MAX_MODEL_CALLS_PER_RUN=150,MILO_MAX_INPUT_TOKENS_PER_RUN=500000,MILO_MAX_OUTPUT_TOKENS_PER_RUN=120000,MILO_MAX_TOTAL_TOKENS_PER_RUN=600000,MILO_MAX_ESTIMATED_COST_PER_RUN=3.00,MILO_MAX_COST_PER_RUN=1.00,MILO_MAX_RUN_DURATION_SECONDS=1800,MILO_MAX_RETRIES=15,MILO_MAX_AGENT_STEPS=56,MILO_MAX_CONCURRENT_RUNS_PER_USER=1,MILO_MAX_CONCURRENT_RUNS_PER_PROJECT=1,MILO_DAILY_USER_BUDGET=4.00,MILO_DAILY_PROJECT_BUDGET=4.00,MILO_ESTIMATED_COST_PER_CALL=0.02"

# ---------------------------------------------------------------------------
# WORKER-ONLY provider operating envelope.
#
# Byte-for-byte the envelope Stage C Attempt 7 ran under and succeeded with
# (0 retries, 0 backpressure events). It is NOT re-derived and NOT widened:
# it is the proven-good configuration, and the production Kimi organization
# is operator-confirmed Tier 2 (concurrency 100 / RPM 500 / TPM 3,000,000 /
# TPD unlimited), so the envelope sits far below the account ceiling.
#
# MILO_PROVIDER_MAX_CONCURRENCY=2 is a TIGHTENING of the current live
# value. Production currently carries MILO_PROVIDER_MAX_CONCURRENCY=8 on
# the Worker (drift introduced by the later swarm-v2 smoke work, verified
# read-only 2026-09-18). Stage D restores the Attempt 7 value of 2, which
# matches the preserved V1 engine parallelism
# (vehicle_catalog_v1/core.MAX_PARALLEL_KIMI_CALLS); no V2 concurrency is
# introduced, and 03b/verify_caps.py refuse the run while the live value is
# anything other than 2.
#
# Kept SEPARATE from STAGE_D_CAPS because STAGE_D_CAPS is deliberately
# applied and verified on BOTH the API and the Worker, while provider
# scheduling belongs to the Worker alone (verify_caps.py fails on any
# MILO_PROVIDER_* variable found on the API). Pinned: an inherited
# override cannot loosen the envelope.
# ---------------------------------------------------------------------------
stage_d_pin STAGE_D_WORKER_PROVIDER_LIMITS "MILO_PROVIDER_MAX_CONCURRENCY=2,MILO_PROVIDER_RPM_LIMIT=350,MILO_PROVIDER_TPM_LIMIT=2400000,MILO_PROVIDER_MAX_RATE_LIMIT_RETRIES=5,MILO_PROVIDER_MAX_BACKPRESSURE_WAIT_SECONDS=240,MILO_PROVIDER_BACKOFF_BASE_SECONDS=2,MILO_PROVIDER_BACKOFF_MAX_SECONDS=30"

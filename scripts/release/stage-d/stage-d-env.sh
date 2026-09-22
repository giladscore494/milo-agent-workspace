#!/usr/bin/env bash
# Shared Stage D parameters. Sourced by every step script. No secrets here.
#
# STATUS: attempt 1 of expansion step 1 WAS executed on 2026-09-19 under the
# key stage-d-expansion-1-20260918-01 and terminalized `timed_out` (a
# controlled fail-closed terminal, not a pass; see STAGE_D_AUTHORIZATION.md
# §9.1). Attempt 2 is PROPOSED and has NOT been executed. Merging this
# authorizes nothing — the one bounded paid run it describes requires a
# fresh, explicit, separate operator authorization (STAGED_ACTIVATION.md,
# Stage D) against a NEW reviewed release.
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

# Where this file lives, so the generated envelope below can find the
# canonical policy regardless of the operator's working directory.
STAGE_D_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export STAGE_D_DIR

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
stage_d_pin STAGE_D_PROBE_DB_SHA256 "558cfc799b0c670feadb73605a4509143df3f04561ded831ca46b93c78dd62de"
stage_d_pin STAGE_D_PROBE_GW_SHA256 "359d7cbfc7195f9fee333480af0f7fe1ae09ca8ce339a0a7942bfe823dde4bce"

# ---------------------------------------------------------------------------
# The ONE authorized run identity.
#
# A brand-new key. Every key production has ever seen is consumed history
# and is never reused: stage-c-smoke-0001 (Attempt 5/6),
# stage-c-smoke-attempt-7-20260819 (Attempt 7 — Stage C PASSED, consumed),
# the four swarm-v2-smoke-* keys, the Government capture key, and
# stage-d-expansion-1-20260918-01 (Stage D expansion step 1, attempt 1 —
# EXECUTED 2026-09-19 as run 3772fc84-420c-4a66-9e79-d58649d4e9b4, terminal
# `timed_out` after 1808s / 113 model calls / $0.337535; a controlled
# fail-closed terminal, NOT a pass; consumed). Only zero pre-existing rows
# under the Stage D key are acceptable, which is why attempt 2 needs its own
# key rather than the consumed one.
# ---------------------------------------------------------------------------
stage_d_pin STAGE_D_IDEMPOTENCY_KEY "stage-d-expansion-1-attempt-2-20260922-01"

# ---------------------------------------------------------------------------
# Exact LIVE baselines that must already hold before the Stage D run is
# created (preflight fails closed on any other exact count). Both were
# re-discovered read-only against production on 2026-09-22, after Stage D
# expansion step 1 attempt 1 had been executed and had terminalized:
#
#   DATABASE — public.runs holds exactly 8 rows, every one TERMINAL:
#     37912575…  failed     stage-c-smoke-0001                     (Stage C A6)
#     8b4a4277…  completed  stage-c-smoke-attempt-7-20260819       (Stage C A7)
#     0d44d491…  cancelled  swarm-v2-smoke-20260824-04c1094
#     986ac9ec…  failed     swarm-v2-smoke-attempt-2-20260824-04c1094
#     0b1b7329…  failed     swarm-v2-smoke-20260824-4fecdfe-01
#     5bd80a2e…  completed  swarm-v2-smoke-20260825-4dbdcd6-01
#     555101dc…  cancelled  catalog-government-capture-20260919-01 (PREPARED
#                           Government capture, RETIRED via
#                           resolve-government-capture.sh — NOT a Stage D
#                           run; see the Government-capture section below)
#     3772fc84…  timed_out  stage-d-expansion-1-20260918-01        (Stage D
#                           step 1 attempt 1, 2026-09-19 — consumed)
#
#   CLOUD RUN — exactly 8 Worker executions, EVERY ONE terminal, zero
#   active: milo-agent-worker-{mcfrx,gggdc,dk4xv,gnj5d,fvfcb,2tckh,bw8kj,
#   xmd2m}. xmd2m is attempt 1's execution (retriedCount=1: the task exited
#   1 on the recorded timeout and Cloud Run's one retry found the run
#   already terminal and exited 0 — see tests/test_worker.py).
#
# The previous pin (7/7, discovered 2026-09-18) was correct for attempt 1
# and is now stale by exactly that one consumed run and its one execution.
# The gates verify a ONE-run/ONE-execution increment over these exact live
# baselines (expected post-run totals: 9 database runs and 9 visible
# terminal executions) — never an empty system, and never "some run
# exists". A count of 7 is a violation exactly like a count of 9: a row
# that vanished is as much a drift as a row that appeared.
# ---------------------------------------------------------------------------
stage_d_pin STAGE_D_EXPECTED_PRIOR_RUNS "8"
stage_d_pin STAGE_D_EXPECTED_PRIOR_EXECUTIONS "8"

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
# The OPERATING ENVELOPE — generated, never transcribed.
#
# Every cap, provider limit and engine-parallelism value below is READ from
# the ONE canonical runtime policy (`backend/runtime_policy.py`) through
# `policy_envelope.py`. Stage D no longer keeps its own copy of the envelope,
# for a concrete reason: the copy it used to keep had drifted into a posture
# the runtime refuses. It pinned MILO_PROVIDER_RPM_LIMIT=350 against an
# organization ceiling of 80, and `ProviderLimitsConfig.from_env` raises on
# exactly that — so the pinned Stage D posture could not have started a
# worker, and nothing in the toolkit could see it, because the toolkit was
# verifying its own transcription rather than the runtime's policy.
#
# Where the numbers come from is documented once, on the dimensions
# themselves in `backend/runtime_policy.py`: the run-level caps are the
# smallest round value at or above 1.75x the Stage C Attempt 7 observation
# and never above the Stage C cap (run 8b4a4277…, terminal `completed`,
# 84 model calls · 277,882 + 34,136 tokens · $0.252069 · 32 agent steps ·
# 934.235s · 0 retries · 0 backpressure), with the two deliberate exceptions
# — a 3.5x margin on output tokens and a HELD 15 retries — recorded there.
#
# Changing a value is a reviewed edit to the policy registry, which changes
# it for the runtime, for production configuration validation, for the
# Swarm V2 plan firewall, for the first-run profile and for Stage D at the
# same time. It cannot be changed for one of them alone.
#
# Fail closed: if the policy cannot be read, Stage D refuses rather than
# proceeding with an empty or partial envelope. Pinned with stage_d_pin, so
# an inherited shell override still cannot loosen anything.
# ---------------------------------------------------------------------------
stage_d_policy() { # SELECTOR — print one generated envelope group
  local rendered
  rendered="$(python3 "${STAGE_D_DIR}/policy_envelope.py" "$1")" || \
    stage_d_refuse "could not read the canonical runtime policy (policy_envelope.py $1) — refusing to proceed on a partial envelope"
  [ -n "${rendered}" ] || \
    stage_d_refuse "the canonical runtime policy produced an EMPTY ${1} group — failing closed"
  printf '%s' "${rendered}"
}

# Applied and verified on BOTH the API and the Worker.
stage_d_pin STAGE_D_CAPS "$(stage_d_policy caps)"

# WORKER-ONLY provider operating envelope. Kept separate from STAGE_D_CAPS
# because provider scheduling belongs to the Worker alone: verify_caps.py
# fails on any MILO_PROVIDER_* variable found on the API.
stage_d_pin STAGE_D_WORKER_PROVIDER_LIMITS "$(stage_d_policy provider-limits)"

# WORKER-ONLY engine parallelism. Newly pinned: MILO_SWARM_MAX_ACTIVE_WORKERS
# was never pinned by this toolkit at all, and its code default of 4 is WIDER
# than the reviewed width of 2 — so a paid worker could have run a Swarm V2
# plan at twice the authorized queueing width with every Stage D check
# passing. The canonical policy makes it mandatory for paid execution, which
# is what makes leaving it unpinned impossible rather than merely unwise.
stage_d_pin STAGE_D_WORKER_ENGINE_LIMITS "$(stage_d_policy engine-limits)"

# How many NEW paid worker executions this authorization covers, read from
# the canonical policy's first_paid_run_execution_cap. 06-collect-evidence.sh
# used to compute `baseline + 1` in shell arithmetic, which was a second
# authority for a rule the policy already states.
stage_d_pin STAGE_D_AUTHORIZED_EXECUTION_INCREMENT "$(stage_d_policy execution-increment)"

# The digest of the whole policy document, which policy_envelope.py will only
# print when it matches the reviewed fingerprint pinned there as a literal --
# the same kind of reviewed constant as the accepted image digests. So this
# value cannot be produced at all by a checkout whose policy has drifted, and
# verify_caps.py additionally proves that policy is byte-for-byte the one at
# STAGE_D_RELEASE_SHA before any run is created. The checkout does NOT have
# to be the release commit: a reviewed authorization commit references a
# release without being it.
stage_d_pin STAGE_D_POLICY_FINGERPRINT "$(stage_d_policy fingerprint)"

# The identity dimensions every run of this release must carry, generated from
# the same policy document. This closes the last open link in the release
# chain: Stage D proved the accepted source, the policy bytes, the image
# digests and the executing job, and said NOTHING about the run -- a run
# recorded no policy, no release and no engine of its own, and re-derived its
# engine from a project row at claim time. `probe_db.py`'s evidence gate now
# compares the authorized run's PERSISTED identity (runs.run_identity,
# migration 20260921000200) with this pin, and refuses a run that is not a run
# of this release.
#
# It carries STAGE_D_RELEASE_SHA, which is the ACCEPTED RELEASE, not this
# checkout's HEAD: a reviewed authorization commit references a release
# without being it, exactly as the policy binding already allows.
stage_d_pin STAGE_D_EXPECTED_RUN_IDENTITY "$(stage_d_policy run-identity)"

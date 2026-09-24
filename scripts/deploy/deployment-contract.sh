#!/usr/bin/env bash
# shellcheck disable=SC2034  # every value here is consumed by the sourcing tool
# Canonical deployment contract — the single source of truth shared by every
# deployment tool in this repository:
#
#   scripts/deploy/cloud-run.sh              (executable deployment)
#   scripts/release/generate-deployment-plan.sh  (operator command plan)
#
# This file is SOURCED, never executed. It deploys nothing, contacts nothing
# and contains no secret values — only names, image repository paths and the
# Stage A posture. Both tools must emit the same image paths and the same
# variable/secret bindings; tests/test_deployment_tool_alignment.py fails if
# they ever drift.

# ---------------------------------------------------------------------------
# Image identity
# ---------------------------------------------------------------------------
# Artifact Registry repository paths for the two images. These are the
# EXISTING production paths (…/<ARTIFACT_REGISTRY_REPOSITORY>/api and
# …/<ARTIFACT_REGISTRY_REPOSITORY>/worker). Changing either value is an
# image-repository migration: it orphans every previously pushed image and
# every rollback target, so it must be a deliberate, documented change — not
# a side effect of editing one tool.
MILO_API_IMAGE_REPO="api"
MILO_WORKER_IMAGE_REPO="worker"

# ---------------------------------------------------------------------------
# Stage A posture
# ---------------------------------------------------------------------------
# Every execution flag is pinned OFF by the deployment itself. The list
# mirrors backend/production_config.py EXECUTION_FLAGS. Later stages are
# enabled deliberately by an operator, never as a side effect of a release.
MILO_STAGE_A_EXECUTION_FLAGS=(
  MILO_ENABLE_RUN_CREATION=false
  MILO_ENABLE_PROPOSAL_MUTATIONS=false
  MILO_ENABLE_PROPOSAL_READS=false
  MILO_ENABLE_RUN_CANCELLATION=false
  MILO_ENABLE_EXECUTION_CONTROL=false
  MILO_ENABLE_PAID_EXECUTION=false
  MILO_ENABLE_CATALOG_EXECUTION=false
  MILO_ENABLE_GOVERNMENT_CATALOG_READ=false
  MILO_ENABLE_CATALOG_PROMOTION=false
  MILO_ENABLE_WORK_SCOPE_MUTATIONS=false
  MILO_ENABLE_WORK_SCOPE_BATCHES=false
  MILO_ENABLE_WORK_SCOPE_PREPARATION=false
)

MILO_STAGE_A_FLAG_NAMES=()
for _milo_flag in "${MILO_STAGE_A_EXECUTION_FLAGS[@]}"; do
  MILO_STAGE_A_FLAG_NAMES+=("${_milo_flag%%=*}")
done
unset _milo_flag

# Provider API keys. At Stage A they are bound to NOTHING — not to the API
# service and not to the worker job. Stage A performs no provider call, so a
# reachable provider credential is pure blast radius: it can only be spent by
# a mistake. The key is introduced by a separate, explicit Stage C operator
# action (docs/production-readiness/STAGED_ACTIVATION.md), together with the
# budget caps and the rehearsed kill switch that make paid execution safe.
# Deployment verification FAILS if either name appears on either resource.
MILO_PROVIDER_KEY_ENV_NAMES=(KIMI_API_KEY MOONSHOT_API_KEY)

# ---------------------------------------------------------------------------
# Required bindings
# ---------------------------------------------------------------------------
# Plain environment variable names that must be present on each resource
# during Stage A, beyond the execution flags above.
#
# MILO_GATEWAY_AUDIENCE and MILO_APPROVED_GATEWAY_IDENTITIES are required
# even though Stage A is execution-disabled: backend/production_config.py
# fails startup in production without them (GATEWAY_AUTH_MISSING), because
# without a verified gateway identity the API would be trusting bare browser
# headers on its read-only routes. They are deployed with approved values,
# never left to whatever happens to be on the service already.
#
# MILO_EXPECTED_SUPABASE_PROJECT_REF pins BOTH runtimes to the approved
# production Supabase project. Staging has refused a wrong Supabase target
# since it was built; production did not, so a runtime handed the wrong
# SUPABASE_URL — a stale secret version, a restored snapshot's project, a
# copy-paste — started and wrote to it. backend/production_config.py now
# fails startup closed without it (PRODUCTION_DEPENDENCY_UNPINNED), which is
# only a real guarantee if the deployment actually binds it, on the worker as
# well as the API: the worker holds the same Supabase credentials and does the
# durable writes. The VALUE is non-secret operator configuration taken from
# the approved manifest's `supabase.project_ref`; it is never hard-coded in
# this repository.
MILO_API_REQUIRED_ENV_NAMES=(
  ENVIRONMENT
  JOB_LAUNCHER
  GCP_PROJECT_ID
  GCP_REGION
  CLOUD_RUN_WORKER_JOB
  ALLOWED_CORS_ORIGINS
  MILO_GATEWAY_AUDIENCE
  MILO_APPROVED_GATEWAY_IDENTITIES
  MILO_EXPECTED_SUPABASE_PROJECT_REF
)
MILO_WORKER_REQUIRED_ENV_NAMES=(
  ENVIRONMENT
  GCP_PROJECT_ID
  GCP_REGION
  MILO_EXPECTED_SUPABASE_PROJECT_REF
)

# The one name both deployment tools bind for the Supabase target pin, and the
# manifest placeholder the generated plan renders for its value.
MILO_SUPABASE_PROJECT_REF_ENV_NAME="MILO_EXPECTED_SUPABASE_PROJECT_REF"
MILO_SUPABASE_PROJECT_REF_PLACEHOLDER="<SUPABASE_PROJECT_REF>"

# Secret-backed environment variable names. Both tools bind exactly these,
# to the same environment names, at the same version. The Secret Manager
# RESOURCE name behind each is deployment-specific (concrete defaults in
# cloud-run.sh, manifest placeholders in the generated plan), so only the
# environment name and version are shared here.
MILO_API_SECRET_ENV_NAMES=(
  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY
  UPSTASH_REDIS_REST_URL
  UPSTASH_REDIS_REST_TOKEN
)
MILO_WORKER_SECRET_ENV_NAMES=(
  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY
  UPSTASH_REDIS_REST_URL
  UPSTASH_REDIS_REST_TOKEN
)
MILO_SECRET_VERSION="latest"

# gcloud dict flags accept an alternate delimiter via the ^DELIM^ prefix,
# which keeps comma-containing values (CORS origins, identity allowlists)
# intact as a single value.
#
# ';' and not '@': MILO_APPROVED_GATEWAY_IDENTITIES is a list of service
# account emails, so every single one of its values contains an '@'. An '@'
# delimiter would split the allowlist mid-address and deploy garbage. ';'
# appears in neither an email address nor an https origin, and both tools
# quote the argument, so the shell never sees it as a command separator.
MILO_ENV_VAR_DELIMITER=";"

# milo_contains NEEDLE ITEM... — success when NEEDLE is one of the items.
milo_contains() {
  local needle="$1" item
  shift
  for item in "$@"; do
    [[ "$item" == "$needle" ]] && return 0
  done
  return 1
}

# ---------------------------------------------------------------------------
# Government capture job
# ---------------------------------------------------------------------------
# The operator capture runs the EXISTING entrypoint
# (backend/catalog/operator_capture.py) out of the EXISTING worker image. It
# is a separate Cloud Run Job purely so its posture is separate: the product
# worker must never carry the catalog master switch, and the capture must
# never carry a provider credential. Nothing here re-implements the capture.
MILO_CAPTURE_ENTRYPOINT_MODULE="backend.catalog.operator_capture"

# The capture reads data.gov.il and writes the snapshot through the same
# service-role repository the worker uses, so it needs the Supabase pair and
# nothing else. Redis is a rate-limit store for the API and gateway; a capture
# takes no HTTP traffic, so it binds neither.
MILO_CAPTURE_SECRET_ENV_NAMES=(
  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY
)
MILO_CAPTURE_REQUIRED_ENV_NAMES=(
  ENVIRONMENT
  GCP_PROJECT_ID
  GCP_REGION
  MILO_EXPECTED_SUPABASE_PROJECT_REF
)

# The capture's master switch, BY NAME ONLY.
#
# CODE-2 requires MILO_ENABLE_CATALOG_EXECUTION for the capture to construct
# anything at all. This repository deliberately does not carry its enabled
# VALUE anywhere, and scripts/check_unsafe_defaults.py enforces that against
# operator scripts too ("a repository default is not a deliberate operator
# decision"). So the name lives here and the value is supplied by the
# operator, once, on the capture command line
# (government-production-capture.sh --enable-catalog-execution). Without that
# explicit argument the capture job is never created and never executed.
#
# It is scoped to this job rather than the product worker on purpose: the
# capture job creates no MILO run, registers no Government tool and builds no
# promotion pipeline, so the master switch being on inside it grants exactly
# the capture and nothing else.
MILO_CAPTURE_MASTER_FLAG_NAME="MILO_ENABLE_CATALOG_EXECUTION"

# The rest of the capture's posture is pinned OFF by the job definition.
#
# MILO_ENABLE_PAID_EXECUTION=false is not decoration. operator_capture.py
# refuses with CAPTURE_PAID_EXECUTION_ENABLED when it is on, so a capture can
# never be bundled with model spend even by an operator who wanted to.
#
# MILO_ENABLE_CATALOG_PROMOTION=false keeps canonical promotion a separate,
# separately authorized decision; a capture lands raw records and candidates,
# never canonical facts.
MILO_CAPTURE_PINNED_OFF_FLAGS=(
  MILO_ENABLE_PAID_EXECUTION=false
  MILO_ENABLE_CATALOG_PROMOTION=false
  MILO_ENABLE_GOVERNMENT_CATALOG_READ=false
  MILO_ENABLE_RUN_CREATION=false
  MILO_ENABLE_EXECUTION_CONTROL=false
  MILO_ENABLE_WORK_SCOPE_PREPARATION=false
)

# Scoped catalog PR2: the scoped-preparation switch, BY NAME ONLY. The job
# definition pins it false (above). `government-production-capture.sh
# --prepare-work-scope --enable-work-scope-preparation` turns it on for ONE
# execution with --update-env-vars, so a plan is only ever prepared by a
# recorded, explicit operator command and never by the job's standing posture.
MILO_WORK_SCOPE_PREPARATION_FLAG_NAME="MILO_ENABLE_WORK_SCOPE_PREPARATION"

# The pinned upstream identity and bounds the capture is allowed to use. These
# MUST equal the values backend/catalog/government/source.py pins, because
# operator_capture.py refuses any other value
# (CAPTURE_RESOURCE_NOT_SUPPORTED / CAPTURE_BOUNDS_NOT_SUPPORTED).
# tests/test_production_operator_bundle.py asserts the equality, so this can
# never drift from the code it mirrors.
MILO_CAPTURE_PACKAGE_ID="degem-rechev-wltp"
MILO_CAPTURE_RESOURCE_ID="142afde2-6228-49f9-8a29-9b6c3a0cbe40"
MILO_CAPTURE_PAGE_LIMIT="1000"
MILO_CAPTURE_MAX_PAGES="200"
MILO_CAPTURE_MAX_RECORDS="120000"

# The two acknowledgements, matched EXACTLY by the entrypoint.
MILO_CAPTURE_EGRESS_ACK="I ACKNOWLEDGE LIVE GOVERNMENT EGRESS"
MILO_CAPTURE_SCHEMA_ACK="I ACKNOWLEDGE OPERATOR-0 SCHEMA REPORT REVIEWED"

# ---------------------------------------------------------------------------
# Activation stages AFTER Stage A — which flag belongs on which component
# ---------------------------------------------------------------------------
# NAMES ONLY. The enabled value is assembled at runtime by the one script that
# applies each stage (website-execution-activate.sh), never committed here:
# scripts/check_unsafe_defaults.py forbids any committed enabled value, and a
# repository default is not a deliberate operator decision.
#
# Every list is read by the applying script AND by the read-only checks
# (website-execution-check.sh, production-verify.sh), so "what the stage sets"
# and "what the check requires" cannot drift apart.
#
# Stage P — plan authoring. The Mapping Plan's two WRITES only, on the API.
# A plan is a draft: it creates no run, launches nothing and reaches no
# Government source. Everything else stays exactly as Stage A left it; in
# particular run creation stays OFF, so the website can author a plan but
# cannot start anything.
MILO_PLAN_AUTHORING_API_ENABLE_FLAGS=(
  MILO_ENABLE_WORK_SCOPE_MUTATIONS
)

# Stage 2 (website execution), API service.
#
#   RUN_CREATION + WORK_SCOPE_BATCHES  starting ONE prepared batch
#                                      (POST /work-scopes/{id}/runs needs both)
#   WORK_SCOPE_MUTATIONS               revising the plan
#   EXECUTION_CONTROL                  the worker's mutation surface; needs
#                                      MILO_WORKER_AUDIENCE + an allowlist
#   RUN_CANCELLATION                   "Cancel this batch"
#   CATALOG_EXECUTION + GOVERNMENT_CATALOG_READ
#                                      NOT a capability on the API: the API
#                                      constructs no Government tool and reads
#                                      no register. They mirror the worker's
#                                      posture so run creation knows Swarm V2
#                                      runs read the catalog and refuses an
#                                      ordinary (unbound) one before it exists
#                                      (CATALOG_RUN_REQUIRES_MAPPING_PLAN)
#                                      instead of launching a run the worker
#                                      would refuse (GOVERNMENT_BATCH_REQUIRED).
MILO_STAGE2_API_ENABLE_FLAGS=(
  MILO_ENABLE_RUN_CREATION
  MILO_ENABLE_EXECUTION_CONTROL
  MILO_ENABLE_RUN_CANCELLATION
  MILO_ENABLE_WORK_SCOPE_MUTATIONS
  MILO_ENABLE_WORK_SCOPE_BATCHES
  MILO_ENABLE_CATALOG_EXECUTION
  MILO_ENABLE_GOVERNMENT_CATALOG_READ
)
# Pinned OFF on the API at Stage 2. Preparation is the capture job's alone
# (one explicit execution); the API never calls a provider; promotion is a
# separate, separately authorized decision.
MILO_STAGE2_API_PINNED_OFF_FLAGS=(
  MILO_ENABLE_WORK_SCOPE_PREPARATION
  MILO_ENABLE_PAID_EXECUTION
  MILO_ENABLE_CATALOG_PROMOTION
)

# Stage 2, product worker job. The worker reads the batch binding and the
# batch's one scoped snapshot; it neither authors plans nor starts batches, so
# the two Mapping Plan API flags are not its concern and stay as Stage A set
# them (false).
MILO_STAGE2_WORKER_ENABLE_FLAGS=(
  MILO_ENABLE_EXECUTION_CONTROL
  MILO_ENABLE_PAID_EXECUTION
  MILO_ENABLE_CATALOG_EXECUTION
  MILO_ENABLE_GOVERNMENT_CATALOG_READ
)
MILO_STAGE2_WORKER_PINNED_OFF_FLAGS=(
  MILO_ENABLE_CATALOG_PROMOTION
  MILO_ENABLE_WORK_SCOPE_PREPARATION
  MILO_ENABLE_WORK_SCOPE_MUTATIONS
  MILO_ENABLE_WORK_SCOPE_BATCHES
)

# The Vercel half of Stage 2, BY NAME. NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI is
# inlined at BUILD time (a rebuild is required); GATEWAY_ALLOW_EXECUTION_ROUTES
# is read by the running gateway.
MILO_STAGE2_VERCEL_BUILD_FLAG="NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI"
MILO_STAGE2_VERCEL_RUNTIME_FLAG="GATEWAY_ALLOW_EXECUTION_ROUTES"
# STARTING a run through the website is a separate gateway permission, opened
# LAST: after the worker and then the API are armed and read back, and after
# the pre-open gate (production-verify.sh --gate armed) proves every start is
# still refused. Plan authoring never sets it, so no intermediate posture can
# start a run from the website.
MILO_STAGE2_VERCEL_RUN_START_FLAG="GATEWAY_ALLOW_RUN_START_ROUTES"

# The database surface the Mapping Plan -> prepared batch -> Swarm V2 path
# calls, created by the three scoped-catalog migrations
# (20260922000100, 20260923000100, 20260924000100) and by the ingestion
# recovery migration (20260924000200: the batched raw-record and candidate
# writes a preparation's scoped capture lands through, and the adoption of an
# orphaned pending snapshot). Each must exist and be EXECUTE-able by
# service_role and by neither anon nor authenticated.
# tests/test_scoped_rollout_contract.py holds this list to the migrations and
# to the repository's own RPC calls.
MILO_WORK_SCOPE_RPCS=(
  create_work_scope
  revise_work_scope
  catalog_canonical_manufacturer_coverage
  prepare_work_scope_queue
  work_scope_batch_for_run
  bind_work_scope_batch_run
  create_work_scope_batch_run
  work_scope_progress
  set_work_scope_paused
  record_catalog_raw_records_batch_guarded
  record_catalog_candidates_batch_guarded
  adopt_catalog_snapshot_guarded
)
MILO_WORK_SCOPE_TABLES=(
  catalog_work_scopes
  catalog_work_scope_revisions
  catalog_work_scope_preparations
  catalog_work_scope_units
  catalog_work_scope_batches
  catalog_work_scope_queue_items
  catalog_work_scope_batch_runs
  catalog_work_scope_controls
  catalog_snapshot_adoptions
)
# The migrations that create that surface, named in the readiness remedy.
MILO_WORK_SCOPE_MIGRATIONS="20260922000100, 20260923000100, 20260924000100, 20260924000200"

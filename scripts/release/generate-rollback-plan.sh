#!/usr/bin/env bash
# Rollback plan generator (plan only; never mutates anything).
#
# Emits the exact forward-safe rollback command sequence for every external
# component: Vercel, Cloud Run API, Cloud Run worker, migrations,
# environment variables, Redis, execution flags and provider access.
# Mirrors docs/production-readiness/ROLLBACK.md.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"
# Canonical image repository paths, shared with scripts/deploy/cloud-run.sh
# and generate-deployment-plan.sh: a rollback must name the same images the
# deployment pushed.
# shellcheck source=../deploy/deployment-contract.sh
source "${SCRIPT_DIR}/../deploy/deployment-contract.sh"

usage() {
  cat << 'EOF'
Usage: generate-rollback-plan.sh --previous-sha <full-sha> [options]

Generates a command plan only. Executes nothing. Rolls back nothing.

Options:
  --previous-sha <sha>   Full 40-character commit SHA of the last known-good
                         release (the rollback target image tag).
  --output <path>        Write the plan as markdown (default: stdout only).
  --json-output <path>   Write a machine-readable JSON report.
  --help                 Show this help.
EOF
}

JSON_OUTPUT="" PREVIOUS_SHA="" OUTPUT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --previous-sha) PREVIOUS_SHA="${2:?}"; shift 2 ;;
    --output) OUTPUT="${2:?}"; shift 2 ;;
    --json-output) JSON_OUTPUT="${2:?}"; shift 2 ;;
    --help) usage; exit 0 ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; usage >&2; exit 64 ;;
  esac
done

if ! is_full_sha "${PREVIOUS_SHA}"; then
  record_check BLOCKED "previous-sha" "--previous-sha must be the full 40-character commit SHA of the rollback target"
  finish_checks "generate-rollback-plan" "${JSON_OUTPUT}"
  exit $?
fi
record_check PASS "previous-sha" "immutable rollback target SHA accepted"

SHA="${PREVIOUS_SHA}"
plan="$(cat << EOF
# MILO rollback plan — target release ${SHA}

Forward-safe rollback templates. Every command is executed MANUALLY by the
operator; nothing here runs automatically. First action in every incident:
turn execution flags off (see step 0).

## 0. Emergency execution-flag order (always first)

The canonical order and its exact commands are in
docs/production-readiness/ROLLBACK.md ("Execution flags — emergency order"):

1. Vercel: GATEWAY_ALLOW_RUN_START_ROUTES=false, then redeploy
2. MILO_ENABLE_PAID_EXECUTION=false (worker job and API service)
3. API: MILO_ENABLE_RUN_CREATION=false, MILO_ENABLE_WORK_SCOPE_BATCHES=false,
   JOB_LAUNCHER=disabled
4. MILO_ENABLE_GOVERNMENT_CATALOG_READ=false (worker job and API service)
5. Remove the provider API key from the worker (--remove-secrets KIMI_API_KEY)

Scripted: scripts/deploy/kill-switch.sh prints exactly this order (dry run by
default) and executes it with --apply (MILO_OPERATOR_ACK and
--vercel-deployment required), then closes the remaining flags and reads the
result back.

### Catalog-only incident — the narrow rollback

A defect in the catalog path does NOT require the full order above and does
not require a code rollback. Disable the catalog capability on its own:

    gcloud run jobs update <CLOUD_RUN_WORKER_JOB> \\
      --project <GCP_PROJECT_ID> --region <GCP_REGION> \\
      --update-env-vars MILO_ENABLE_CATALOG_EXECUTION=false

Verification evidence — describe the job and read its worker container env:

    gcloud run jobs describe <CLOUD_RUN_WORKER_JOB> \\
      --project <GCP_PROJECT_ID> --region <GCP_REGION> --format json

It must show MILO_ENABLE_CATALOG_EXECUTION=false, and the next swarm_v2 run
must emit neither catalog_variant_promoted nor catalog_promotion_refused.

Subsequent runs then register no Government tool, grant no catalog scope and
construct no promotion pipeline. Runs already in flight finish under the
configuration they started with. This DELETES AND MUTATES NO catalog row:
snapshots, candidates and canonical variants are left exactly as they are, so
re-enabling resumes from the same durable state.

The same flag closes the operator Government capture entrypoint
(backend/catalog/operator_capture.py). While it is off, an invocation refuses
before constructing a transport or a repository: no request reaches
data.gov.il, no run is claimed, and nothing is captured, ingested or activated.
It prevents the NEXT capture and undoes no previous one. A capture already in
flight holds a lease and is stopped by cancelling its run, after which
activation cannot occur — activation is the last step and is gated on complete
persistence, so an interrupted capture leaves a non-active snapshot that no
reader reads.

Flags are changed by updating the Cloud Run service env (see below); each flag
is explicit, and scripts/deploy/kill-switch.sh covers the whole emergency order
(see docs/production-readiness/ROLLBACK.md).

## 1. Vercel

    vercel ls <VERCEL_PROJECT_NAME>                       # identify previous successful deployment
    vercel inspect <PREVIOUS_DEPLOYMENT_URL>              # inspect environment differences
    vercel promote <PREVIOUS_DEPLOYMENT_URL>              # promote previous deployment manually
    # restore previous server environment values via: vercel env add <NAME> production
    cd frontend && npm run test:secrets                   # verify browser bundle contains no secret
    scripts/release/smoke-test-read-only.sh --base-url <PRODUCTION_VERCEL_URL> ...

## 2. Cloud Run API (execution flags off FIRST — step 0)

    gcloud run revisions list --service <CLOUD_RUN_API_SERVICE> --region <GCP_REGION>   # identify previous revision
    gcloud run revisions describe <PREVIOUS_REVISION> --region <GCP_REGION> \\
      --format 'value(spec.containers[0].image)'          # verify digest matches ${MILO_API_IMAGE_REPO}:${SHA}
    gcloud run services update-traffic <CLOUD_RUN_API_SERVICE> --region <GCP_REGION> \\
      --to-revisions <PREVIOUS_REVISION>=100              # move traffic explicitly
    gcloud run services get-iam-policy <CLOUD_RUN_API_SERVICE> --region <GCP_REGION>    # verify private IAM (no allUsers)
    curl -s <PRODUCTION_VERCEL_URL>/api/gateway/health    # verify health via gateway
    # preserve the failed revision for investigation — do NOT delete it.

## 3. Cloud Run worker

    # stop new launches first: JOB_LAUNCHER=disabled and run-creation off (step 0)
    gcloud run jobs update <CLOUD_RUN_WORKER_JOB> --region <GCP_REGION> \\
      --image <GCP_REGION>-docker.pkg.dev/<GCP_PROJECT_ID>/<ARTIFACT_REGISTRY_REPOSITORY>/${MILO_WORKER_IMAGE_REPO}:${SHA}
    # do NOT execute the job to "test" the rollback.
    gcloud run jobs describe <CLOUD_RUN_WORKER_JOB> --region <GCP_REGION> \\
      --format 'value(spec.template.spec.template.spec.serviceAccountName)'   # verify SA + secret mappings
    gcloud run jobs executions list --job <CLOUD_RUN_WORKER_JOB> --region <GCP_REGION>
    # already-running executions: let leases expire or cancel the runs via the
    # API cancellation path; stale workers are rejected by lease-token checks.

## 4. Migrations (forward-only)

    # NO destructive automated down-migration exists, by design.
    # 1. stop execution (step 0); 2. take/verify backup; 3. inspect state:
    scripts/release/check-migration-state.sh --database-url-env MILO_READONLY_DB_URL
    # 4. write corrective FORWARD migration SQL; 5. review manually;
    # 6. apply only after explicit approval; 7. re-verify RLS and ownership:
    #    rerun tests/test_migrations_postgres.py expectations against staging.

## 5. Environment variables

    # metadata/names only; values live in Secret Manager / Vercel / Cloud Run
    vercel env ls production                                    # names only
    gcloud run services describe <CLOUD_RUN_API_SERVICE> --region <GCP_REGION> \\
      --format 'value(spec.template.spec.containers[0].env)'    # names + refs only
    # restore prior names/references from the approved versioned manifest
    # (config/production.example.yaml schema; never contains values),
    # redeploy only after review, then verify flags remain off:
    scripts/release/smoke-test-execution-disabled.sh --env-file <APPROVED_ENV_METADATA>

## 6. Redis

    # if the shared rate-limit store is unavailable, execution surfaces
    # already fail closed; additionally disable new execution (step 0).
    # PRESERVE the production keyspace — never FLUSHDB/FLUSHALL.
    # rotate the credential if compromised (provider dashboard), then update:
    #   Vercel: UPSTASH_REDIS_REST_TOKEN; Cloud Run: secret reference.
    scripts/release/check-redis-config.sh --env-file <metadata> --allow-network

## 7. Provider access

    # disable paid execution and remove the worker's provider key (step 0,
    # items 2 and 5), rotate the provider key manually in the provider
    # console if compromised, verify no other service has access:
    gcloud secrets get-iam-policy <PROVIDER_KEY_SECRET>
    # inspect usage and cost in the provider console.
EOF
)"

printf '%s\n' "${plan}"
if [[ -n "${OUTPUT}" ]]; then
  printf '%s\n' "${plan}" > "${OUTPUT}"
  record_check PASS "plan" "rollback plan written to ${OUTPUT}"
else
  record_check PASS "plan" "rollback plan generated (stdout)"
fi
record_check MANUAL "execute" "every command above is executed manually by the operator; this script never mutates anything"

finish_checks "generate-rollback-plan" "${JSON_OUTPUT}"

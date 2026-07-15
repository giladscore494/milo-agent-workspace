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

1. MILO_ENABLE_PAID_EXECUTION off
2. MILO_ENABLE_RUN_CREATION off (and GATEWAY_ALLOW_EXECUTION_ROUTES off)
3. JOB_LAUNCHER=disabled (worker launch off)
4. Restrict worker route access (verify MILO_APPROVED_WORKER_IDENTITIES)
5. Revoke worker access to the provider secret if necessary:
       gcloud secrets remove-iam-policy-binding <PROVIDER_KEY_SECRET> \\
         --member serviceAccount:<WORKER_SERVICE_ACCOUNT_EMAIL> \\
         --role roles/secretmanager.secretAccessor
6. API remains read-only where safe.

Flags are changed by updating the Cloud Run service env (see below) — there
is no enable-all or disable-all script by design; each flag is explicit.

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
      --format 'value(spec.containers[0].image)'          # verify digest matches milo-api:${SHA}
    gcloud run services update-traffic <CLOUD_RUN_API_SERVICE> --region <GCP_REGION> \\
      --to-revisions <PREVIOUS_REVISION>=100              # move traffic explicitly
    gcloud run services get-iam-policy <CLOUD_RUN_API_SERVICE> --region <GCP_REGION>    # verify private IAM (no allUsers)
    curl -s <PRODUCTION_VERCEL_URL>/api/gateway/health    # verify health via gateway
    # preserve the failed revision for investigation — do NOT delete it.

## 3. Cloud Run worker

    # stop new launches first: JOB_LAUNCHER=disabled and run-creation off (step 0)
    gcloud run jobs update <CLOUD_RUN_WORKER_JOB> --region <GCP_REGION> \\
      --image <GCP_REGION>-docker.pkg.dev/<GCP_PROJECT_ID>/<ARTIFACT_REGISTRY_REPOSITORY>/milo-worker:${SHA}
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

    # disable paid execution (step 0), revoke worker access to the provider
    # secret (step 0.5), rotate the provider key manually in the provider
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

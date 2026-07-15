# Production readiness — persistent status

- **Scope:** Corrective pass on the Phase 9–11 operator/release tooling after
  a live read-only Google Cloud Shell inspection exposed production-operator
  defects that the mocked Phase 9–11 CI did not catch.
- **Corrective branch:** `claude/fix-operator-tooling-audit-ry0s6f`
- **Corrective PR:** #__PR__ (base: `claude/production-readiness-j0hhni`;
  does **not** target `main`).
- **Base:** `claude/production-readiness-j0hhni` at
  `2df9e74a910ec47d30910933f3bf2837237392f9` (the merge commit of PR #31,
  "Phases 9–11 operator tooling …").
- **Corrective head SHA:** `__HEAD_SHA__`.

## What was wrong (found only by the real read-only Cloud Shell audit)

The Phase 9–11 CI was green because its mocks were too permissive. A real
Cloud Shell inspection surfaced these defects, all now corrected:

1. **Cloud Run Job service-account path** — the checker and generated
   verification commands read `spec.template.template.spec.serviceAccountName`,
   which is wrong for a Cloud Run **Job** (missing the ExecutionSpec level).
   It always resolved empty and a real existing job was misreported as "job
   not found". Corrected to `spec.template.spec.template.spec.serviceAccountName`
   via structured `--format json` parsing, and an existing job with **no**
   explicit SA is now a blocking finding (it must not silently use the default
   Compute Engine service account).
2. **Vercel CLI** — used the unsupported `vercel env ls production
   --scope-project`. Replaced with supported syntax operating on the linked
   project, names-only parsing, and explicit classification of auth failure /
   unlinked / wrong project / empty environment (never `2>/dev/null || true`).
3. **launch_unknown reconciliation** — wrote the audit record before the
   mutation and treated a successful `psql` process as success even when the
   `UPDATE` changed zero rows. Now uses `UPDATE … RETURNING`, requires exactly
   one affected row, enforces state/status/lease guards, and writes the audit
   record only after a validated one-row success.
4. **Execution-disabled smoke test** — accepted a bare 401 as proof. Now
   requires an authenticated user + owned conversation and an execution-disabled
   403 with the application classification; any authenticated 2xx is BLOCKED.
5. **Secret Manager** — the orchestrator invoked the checker with no expected
   secrets/consumers. It now derives `name=consumer` expectations from the
   manifest (mapping api/worker/gateway to the exact SA emails) and never
   claims verification when no concrete expectations exist.
6. **Aggregate report** — top-level totals did not sum nested findings. A
   dedicated aggregator now produces true consolidated totals, a
   `blocking_findings` list, a de-duplicated `manual_actions_remaining` list,
   valid JSON even when a sub-audit fails, and a nonzero exit when blocked > 0.
7. **Redis** — the orchestrator gained `--redis-expected-environment` and
   `--redis-allow-network`, and the report states whether Redis was statically
   checked, live-probed, not probed, inaccessible, or incorrectly shared.

## Corrective validation (this checkout)

- `pytest -q tests --ignore=MILO-main-original/MILO-main/test_websearch.py`
  — **468 passed** (includes the 29 permissive release-tooling tests and the
  44 new strict CLI regression tests in `tests/test_release_tooling_cli.py`).
- `MILO_REQUIRE_PG_TESTS=1 pytest -q tests/test_migrations_postgres.py`
  — **71 passed, 0 skipped** (real ephemeral PostgreSQL).
- `python scripts/check_migrations.py` — passed.
- `python scripts/secret_scan.py` — passed.
- `python scripts/check_unsafe_defaults.py` — passed.
- `shellcheck -x -S warning -P scripts/release scripts/release/*.sh
  scripts/release/lib/common.sh` — clean.
- `python3 scripts/release/validate_production_manifest.py --manifest
  config/production.example.yaml --mode plan` — passed.
- Frontend: `npm ci` OK; `npx tsc --noEmit` OK; `npm test -- --run` —
  **60 passed**; `npm run build` OK; `npm run test:static` OK;
  `npm run test:secrets` OK.
- Docker image builds (`Dockerfile.api`, `Dockerfile.worker`): not runnable in
  this sandbox (no Docker daemon); built and verified by the `offline-checks`
  CI job. No Dockerfile changed in this PR.
- Isolated Playwright E2E: enforced by the `e2e` CI job (mocked
  auth/worker/provider); no frontend runtime changed in this PR.

## Execution-safety confirmation

This corrective PR changes **tooling, tests and documentation only**. No
production deployment, IAM change, Secret Manager change, production
migration, backfill, Redis mutation, worker execution, or paid provider call
occurred. Every operator script still defaults to check/plan/dry-run; mutation
still requires the full protected apply mode, exercised only against mocks.

## Deployment posture

**Production deployment remains BLOCKED** until this corrective PR is merged
into `claude/production-readiness-j0hhni` **and** a real read-only audit report
is produced from authenticated operator tooling against the live project:

    scripts/release/production-readiness.sh \
      --json-output readiness.json \
      --manifest <COMPLETED_OPERATOR_MANIFEST> \
      --env-file <APPROVED_ENV_METADATA> \
      --expected-project <GCP_PROJECT_ID> \
      --expected-account "$(gcloud config get-value account)" \
      --region <GCP_REGION> --repository <ARTIFACT_REGISTRY_REPOSITORY> \
      --api-service <CLOUD_RUN_API_SERVICE> --worker-job <CLOUD_RUN_WORKER_JOB> \
      --api-sa <API_SERVICE_ACCOUNT_EMAIL> --worker-sa <WORKER_SERVICE_ACCOUNT_EMAIL> \
      --vercel-project <VERCEL_PROJECT_NAME> \
      --database-url-env MILO_READONLY_DB_URL \
      --redis-expected-environment production

No document in this set claims the live production environment passed; that
statement may only be made after the command above produces a report with zero
consolidated blocking findings.

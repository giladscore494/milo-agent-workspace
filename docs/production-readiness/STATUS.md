# Production readiness — persistent status

- **Scope:** Corrective pass on the Phase 9–11 operator/release tooling after
  a live read-only Google Cloud Shell inspection exposed production-operator
  defects that the mocked Phase 9–11 CI did not catch.
- **Corrective branch:** `claude/fix-operator-tooling-audit-ry0s6f`
- **Corrective PR:**
  [#33](https://github.com/giladscore494/milo-agent-workspace/pull/33)
  (base: `claude/production-readiness-j0hhni`; does **not** target `main`).
- **Base:** `claude/production-readiness-j0hhni` at
  `2df9e74a910ec47d30910933f3bf2837237392f9` (the merge commit of PR #31,
  "Phases 9–11 operator tooling …").
- **Corrective head SHA (round 1):** `77b7ee525bf4b0560b8a61c57c8f8d2af79236b9`.
- **Corrective head SHA (round 2, final-review blockers):** committed atop
  round 1 on the same branch/PR (see the follow-up commit; CI reruns on the
  final head before any merge decision).

## Production bootstrap automation (new)

- **Feature branch:** `claude/bootstrap-production-oneshot`
- **PR:**
  [#34](https://github.com/giladscore494/milo-agent-workspace/pull/34)
  (base: `claude/production-readiness-j0hhni`; does **not** target `main`;
  not merged).
- **What:** `scripts/release/bootstrap-production.sh` — a nearly one-command
  `--plan` / `--apply` / `--audit-only` bootstrap that replaces the manual
  construction of `milo-production.yaml`, service accounts, Secret Manager
  resources and per-secret IAM, Vercel production variables, Upstash metadata
  and Cloud Run identity assignments. Default mode is `--plan` (read-only).
- **Adopts existing production state.** Default secret resource names are the
  operator's existing Secret Manager resources (`SUPABASE_URL`,
  `SUPABASE_SECRET_KEY`, `KIMI_API_KEY`, `UPSTASH_REDIS_REST_TOKEN`); secrets
  are inspected (states `REUSE_ENABLED` / `EXISTS_NO_ENABLED_VERSION` /
  `MISSING` / `INSPECTION_ERROR`) and adopted without prompting or re-creating
  when an enabled version exists. Existing Vercel production variables are
  reused (REUSE/CREATE/UPDATE classification). The GitHub workflow no longer
  requires duplicating Supabase/provider values as GitHub secrets.
- **Guarded apply** creates the distinct API/worker/gateway service accounts
  (never keys), adopts/creates Secret Manager resources with per-secret
  accessor grants, discovers/creates a dedicated Upstash production Redis
  (token stored in Secret Manager, never printed), **configures the live Cloud
  Run API/worker env vars + Secret Manager references** (flags false, budgets
  nonzero, `JOB_LAUNCHER=disabled`; worker job **never executed**), sets Vercel
  managed vars idempotently, verifies/adopts the Vercel→GCP Workload Identity
  Federation chain and binds `roles/run.invoker`, and runs a **live-inspecting**
  audit that requires consolidated blocked = 0 (never trusting the generated
  manifest alone). `--deploy-vercel` was removed (the bootstrap never deploys).
- **Also added:** `.github/workflows/bootstrap-production.yml`
  (`workflow_dispatch` only, `production` environment, Workload Identity
  Federation, redacted artifacts only), `scripts/release/verify_live_config.py`,
  `tests/test_bootstrap_production.py` (31 strict tests), and
  `docs/production-readiness/BOOTSTRAP.md`.
- **No production mutation or deployment was performed** during
  implementation or testing; all cloud calls in tests are strictly mocked and
  Upstash is served by a mock fixture.

## Bootstrap hardening round — audit-only + fail-closed corrections

On top of the exact-value hardening, a further review round tightened
`bootstrap-production.sh` and its helpers (tooling/tests/docs only; no
production mutation):

- `check-vercel-config.sh` never passes `--token` on the command line — the
  token is exported as `VERCEL_TOKEN` in the subprocess environment only; the
  mock rejects any `--token` argv.
- `--audit-only` is now a complete fail-closed audit: it runs exact WIF
  verification, the Upstash/Redis consistency check (when management creds are
  supplied), and an in-memory cross-provider Redis fingerprint comparison, and
  it is read-only (never creates a database/secret/version, never rotates, never
  configures Cloud Run/Vercel). Missing WIF/Vercel/Redis-consistency evidence is
  BLOCKED (MANUAL only in `--plan`).
- Redis-dependent mutations are gated: Cloud Run and Redis Vercel variables are
  updated only after reconciliation proves an exact positive numeric Secret
  Manager version (no `:latest` fallback); partial failure leaves the existing
  Redis wiring unchanged.
- WIF `attributeMapping` is compared as the complete dictionary (expressions +
  no missing/extra); all seven WIF inputs are required together.
- Upstash validation is fail-closed on missing/null `state`/`tls`/`platform`/
  `region` and restricts endpoints to documented `*.upstash.io` hosts (no path/
  query/userinfo/foreign host).
- Exact Vercel value/fingerprint verification is mandatory in apply/audit-only
  (`--strict-values`): an unprovable value is BLOCKED, not MANUAL.
- Small fixes: the workflow plan job runs only when `mode == plan`; non-finite
  budgets (`NaN`/`Infinity`) are rejected via `math.isfinite`.

## Bootstrap contract round — real-CLI/API compatibility + audit contract

A further review of PR #34 found that the bootstrap could pass its mocked
tests while being incompatible with the REAL pinned CLIs/APIs. All six
blockers are fixed on the same branch (tooling, tests, workflow and docs only;
no production mutation):

1. **Vercel CLI pin was incompatible.** `vercel@39.3.0` has no `env update`
   and no `env run` (verified against the real CLI: its `env` subcommands are
   only add/list/pull/remove). The pin now lives in
   `scripts/release/VERCEL_CLI_VERSION` (single source of truth) and is
   **56.2.1**, verified against the real binary to support `env ls/add`,
   in-place `env update --yes` (stdin value) and `env run --environment`. A
   `vercel_cli_contract` preflight (version must be exact numeric x.y.z;
   required subcommands/flags must exist; `--version`/`--help` only, never a
   deploy) runs before ANY Vercel mutation or verification and is BLOCKED in
   apply/audit-only on failure. CI installs the pin and runs
   `tests/test_contract_vercel_cli.py` against the real CLI.
2. **`vercel env run` verification could be forged by local overrides.** The
   real CLI overlays local `.env` files and the parent process environment
   over the downloaded production records. Verification now runs from a
   freshly-created EMPTY private `--cwd` (no `.env` overlay possible) with the
   verified name scrubbed from the subprocess environment (`env -u NAME`) and
   the name/expected value passed only via dedicated verifier variables;
   CLI-identity passthrough names are refused outright. Override-scenario
   tests prove a hostile caller environment neither reaches the CLI subprocess
   nor flips a MISMATCH.
3. **Audit-only contract is now explicit.** `--audit-only` requires exactly
   one Redis evidence source: `--audit-metadata` (metadata-assisted audit
   against the stored non-secret metadata; file validated fail-closed and live
   Secret Manager version + in-memory fingerprint must match it — see the
   truthful-contract round below for the exact credential requirements) or
   Upstash management credentials (credentialed deep audit, GET-only); with
   neither, `audit:contract` is BLOCKED — never a silent degrade. Tests cover
   metadata validity, absence, malformation and live mismatch.
4. **Upstash validator expected undocumented response fields.** `platform` is
   a create-REQUEST parameter that the API never returns, so every real
   response would have been false-BLOCKED. The validator now uses ONLY
   documented response fields (`database_id`/`database_name`/`state`/`tls`/
   `region`/`primary_region`/`endpoint`/`rest_token`), accepts the documented
   list shape (bare JSON array), drops undocumented aliases, and
   `tests/test_contract_upstash_api.py` pins the parser to the documented
   schema (plus an opt-in strictly read-only GET against the real API).
5. **WIF inspection is fail-closed via `evidence_missing`.** A permission/API
   failure inspecting the WIF pool (and unavailable live-config/Vercel
   evidence) is now BLOCKED in apply/audit-only (MANUAL only in plan), with
   tests for permission/API failures in both modes.
6. **IAM mutations are re-verified.** After `run.invoker` and per-secret
   accessor bindings, the live policy is re-read and must contain the exact
   expected binding (no broad principals, no unrelated run.invoker members);
   a mutation that did not take effect, cannot be re-read, or coexists with a
   broad principal is BLOCKED. Stateful mocks return distinct before/after
   policies to prove the re-read.

**Validation of this round (this checkout):**
`tests/test_bootstrap_production.py` — **120 passed** (36 new tests for the
six blockers). Full backend + release tooling
(`pytest -q tests --ignore=MILO-main-original/MILO-main/test_websearch.py`)
— **643 passed, 6 skipped** (the 6 skips are exactly the opt-in real-CLI /
live-Upstash contract tests, which additionally passed against the real
`vercel@56.2.1` binary with `MILO_REQUIRE_VERCEL_CLI_CONTRACT=1` — **7
passed**; offline contract tests **16 passed**). PostgreSQL suite
(`MILO_REQUIRE_PG_TESTS=1`) — **71 passed, 0 skipped**. `shellcheck -x -S
warning` clean; `secret_scan.py` / `check_unsafe_defaults.py` /
`check_migrations.py` pass; read-only `production-readiness.sh` passes;
workflow YAML validates. No real GCP / Vercel / Upstash / Supabase / Redis /
provider resource was accessed or mutated: the only real external calls were
`vercel --version` / `--help` against a locally installed CLI (read-only,
no login, no deploy).

## Bootstrap truthful-contract round — metadata trust + operational audit

A further review of PR #34 (head `289e3597`) surfaced five blockers about the
honesty and operational robustness of the audit path. All are fixed on the
same branch (tooling, tests, workflow and docs only; no production mutation):

1. **The audit contract is stated truthfully.** The former "zero-secret" name
   was wrong: the metadata path reads the pinned Redis Secret Manager payload
   (once, in memory, for the fingerprint proof) and requires a Vercel token.
   It is now the **metadata-assisted CREDENTIALED audit** everywhere (script
   contract, findings, usage, docs, tests), with the required credentials
   documented explicitly: gcloud (control-plane + one
   `secretmanager.versions.access` read) and the Vercel token; NO Upstash
   management credential.
2. **Metadata is trustworthy.** `milo-production.metadata.env` now carries
   `MILO_METADATA_SCHEMA_VERSION=2`, `MILO_BOOTSTRAP_STATUS=applied`,
   `MILO_ENVIRONMENT`, the full `MILO_BOOTSTRAP_SHA`, `GCP_PROJECT_ID`, a
   `MILO_METADATA_GENERATED_AT` reconciliation timestamp and the Redis
   identity fields. It is written ONLY by `--apply`, AFTER the final live
   audit, and ONLY on full success; a partial failure deletes any candidate
   file and records `metadata:withheld`; plan/audit-only never write it. The
   audit BLOCKS on: non-`applied` status, missing/short/mismatching release
   SHA, wrong project, wrong environment, missing/unparseable/future/stale
   timestamp (`--audit-metadata-max-age-hours`, default 720), and any
   missing/malformed Redis field.
3. **Database identity is verified against live state.** The guarded apply
   writes `MILO_BOOTSTRAP_SHA` (alongside the existing `MILO_REDIS_DB_ID` /
   `MILO_REDIS_TOKEN_FINGERPRINT` / `MILO_REDIS_SECRET_VERSION`) into the
   live Cloud Run env; `verify_live_config.py` now verifies db id,
   fingerprint, pinned version and bootstrap SHA EXACTLY on both the API
   service and the worker job, and the Vercel audit verifies the managed
   `MILO_REDIS_TOKEN_FINGERPRINT` variable plus the token fingerprint
   itself. Regression tests prove a wrong database id (stored or live)
   blocks the audit.
4. **Metadata is preserved operationally.** The apply job uploads the
   successful metadata as the durable `bootstrap-production-metadata`
   artifact, gated on `success()` (and the bootstrap itself never leaves the
   file behind on failure, so failed metadata can never be uploaded). A new
   `audit-only` workflow mode downloads that artifact by apply-run id and
   runs the complete fail-closed audit with no mutations.
5. **IAM inspection failures block BEFORE mutation.** In
   `gcp_ensure_run_invoker`, a failed/denied/malformed initial policy read is
   BLOCKED (apply/audit-only) before `add-iam-policy-binding` is ever issued;
   the post-mutation re-read and exact-member verification are kept. Tests
   cover permission failure, API failure and malformed JSON, proving no
   mutation command is issued.

A fresh end-to-end adversarial scan of the whole PR (beyond the listed
blockers) surfaced four additional issues, all fixed in the same round:
the audit-only workflow job lacked the `actions: read` permission required
for the cross-run artifact download (would fail only in the real GitHub
runtime, not in tests); `generate_metadata` was not atomic (a crash mid-write
could leave a partial file already stamped `applied` — now mktemp + mv);
the apply success gate now also requires a zero global BLOCKED count (not
only the internal failure flag) before writing metadata or claiming success;
and `gcp_grant_secret_accessor` gained the same fail-closed PRE-mutation
policy read as `run.invoker` (unreadable/malformed/broad pre-state → BLOCKED
with no binding issued), with regression tests for all reachable cases.

**Validation of this round (this checkout):**
`tests/test_bootstrap_production.py` — **159 passed** (39 new tests: metadata
provenance/staleness/binding, live Redis/release identity drift, IAM
pre-mutation blocks, workflow gating). Bootstrap + release tooling —
**271 passed**. Full backend
(`pytest -q tests --ignore=MILO-main-original/MILO-main/test_websearch.py`)
— **682 passed, 6 skipped** (exactly the opt-in real-CLI / live-Upstash
contract tests, which passed against the real `vercel@56.2.1` with
`MILO_REQUIRE_VERCEL_CLI_CONTRACT=1` — 7 passed). PostgreSQL suite
(`MILO_REQUIRE_PG_TESTS=1`) — **71 passed, 0 skipped**. Frontend —
`tsc --noEmit` clean, vitest **60 passed**, static + secret checks pass.
Isolated Playwright E2E — **31 passed** (full suite; the sandbox browser
was bridged to the pinned Playwright version). `shellcheck -x -S warning`
clean; secret scan / unsafe-default scan / migration check pass; read-only
`production-readiness.sh` passes; workflow YAML validates. No real GCP /
Vercel / Upstash / Supabase / Redis / provider resource was accessed or
mutated.

## Stabilization round — post-merge regression fix + adversarial hardening

Merging PR #35 into the PR #34 branch introduced one CI regression and a
clean-room adversarial review of the merged result produced three
defense-in-depth hardenings (tooling, workflow, tests and docs only; no
production access):

1. **Regression fixed (the `offline-checks` failure).** The early
   `apply:pre-mutation-gate` exit added by PR #35 deleted any candidate
   metadata file but did not record the `metadata:withheld` finding (nor the
   `APPLY INCOMPLETE` operator message / `apply:result` finding) that the
   ordinary partial-failure path records, so
   `test_metadata_withheld_on_partial_failure` failed. The early gate now
   reports the identical partial-failure contract; the test was not weakened.
2. **Explicit global pre-mutation gate.** Creating the Upstash database is
   the first external mutation an apply can perform. Today every earlier
   blocking path exits before that point (proven by regression tests); the
   create path now additionally refuses to POST while any blocking finding is
   already recorded, so the invariant survives future reordering
   (`upstash:create-gate`).
3. **Metadata artifact provenance.** The audit-only workflow no longer trusts
   a downloaded artifact merely because it has the expected filename: before
   the download, the `metadata_run_id` run must be a successful,
   `workflow_dispatch`-triggered run of this repository's
   bootstrap-production workflow whose head SHA equals the audited commit
   (Actions API under `actions: read`). The bootstrap's content validation
   and mandatory live-state verification remain as independent layers.
4. **Audit-mode separation proven.** New regression tests prove the deep
   audit issues only `GET` Upstash requests and never creates a missing
   database, and that the metadata-assisted audit never calls the Upstash
   management API even when management credentials are (wrongly) supplied.

## Round 2 — six final-review blockers corrected

A second review of the corrective PR surfaced six more production-audit
correctness blockers, all now fixed on the same PR #33 branch (tooling, tests
and docs only — no production mutation):

1. **Execution-disabled smoke test did not prove authentication.** The gateway
   returns the run-creation 403 before validating the token, so a random
   non-empty token still got the expected 403. The script now first performs
   an authenticated read `GET /conversations/<id>` and requires HTTP 200
   (token valid + owns the conversation) before the run-creation probe;
   401→BLOCKED (invalid token), 403/404→BLOCKED (not owned), missing→MANUAL.
   The curl mock now judges token validity by the token VALUE, not header
   presence.
2. **Secret Manager consumer checks ignored the IAM role.** Consumer/extra/
   wildcard checks now parse the policy structurally (`iam_role_members`) and
   only consider members of the exact `roles/secretmanager.secretAccessor`
   binding; an SA under viewer/admin/metadata roles never satisfies or
   pollutes accessor validation.
3. **Vercel project identity was not fail-closed.** Identity is now proven by
   reading `projectId`/`orgId` from `.vercel/project.json` and matching them
   against `vercel project inspect`; a missing/malformed link file, failed
   inspection, differing project ID, or differing org is BLOCKED (not a WARN),
   before any variable is inspected.
4. **Missing vs permission/API failures conflated.** Artifact Registry
   describe, service-account describe, secret versions list, secret listing,
   and project/secret IAM policy reads now capture stdout+stderr+exit and only
   classify a clean NOT_FOUND as "missing"; permission/API/network failures are
   MANUAL/BLOCKED inspection failures, never a false "resource missing" / "no
   enabled version" / silently-passed consumer check.
5. **`leave-unresolved` bypassed the operator guard.** It now requires an
   explicit run id and the full `apply_guard` identity checks (it needs no
   writable DB), optionally revalidates read-only that the run is still
   `launch_unknown`, and writes the enriched audit record only after the guard
   passes.
6. **`no-secret-returned` health check could false-PASS.** It now requires a
   successful curl, HTTP 200, and a non-empty body before scanning; a
   transport failure, non-200, or empty body is BLOCKED.

These are covered by new strict tests in `tests/test_release_tooling_cli.py`
(now 83 tests) plus new fixtures.

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
  — **507 passed** (includes the 29 permissive release-tooling tests and the
  83 strict CLI regression tests in `tests/test_release_tooling_cli.py`, which
  now cover the round-2 blockers as well).
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

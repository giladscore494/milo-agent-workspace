# Final acceptance — Phases 9–11 production-readiness audit

Consolidated classification of every major item. Classifications:
`COMPLETED_IN_CODE` | `REQUIRES_MANUAL_OPERATOR_CONFIGURATION` |
`INTENTIONALLY_DEFERRED` | `BLOCKED`.

## Classification table

| Item | Classification | Reference |
| --- | --- | --- |
| Gateway authentication (fail-closed, short-lived tokens) | COMPLETED_IN_CODE | AUTHENTICATION.md |
| Worker service-to-service authentication | COMPLETED_IN_CODE | AUTHENTICATION.md |
| Active worker lease (token + attempt) enforcement | COMPLETED_IN_CODE | RUN_LIFECYCLE.md |
| Ownership, membership, RLS | COMPLETED_IN_CODE | AUTHORIZATION_AND_RLS.md |
| Proposal lifecycle + ownership protection | COMPLETED_IN_CODE | AUTHORIZATION_AND_RLS.md |
| Idempotent run/proposal creation | COMPLETED_IN_CODE | RUN_LIFECYCLE.md |
| Launch lifecycle + launch_unknown reconciliation tooling | COMPLETED_IN_CODE | RUN_LIFECYCLE.md, `reconcile-launch-unknown.sh` |
| Cancellation (idempotent) | COMPLETED_IN_CODE | RUN_LIFECYCLE.md |
| Checkpoints and events | COMPLETED_IN_CODE | RUN_LIFECYCLE.md |
| Supervisor shadow mode | COMPLETED_IN_CODE (shadow only) | ARCHITECTURE.md |
| Supervisor active autonomy | INTENTIONALLY_DEFERRED | below |
| Hard budgets, token/duration/retry caps | COMPLETED_IN_CODE | BUDGETS_AND_COSTS.md |
| Canonical reservation → settlement lifecycle | COMPLETED_IN_CODE | BUDGETS_AND_COSTS.md |
| Actual-cost accounting + ledger | COMPLETED_IN_CODE | BUDGETS_AND_COSTS.md |
| Shared-store rate limiting (fail-closed) | COMPLETED_IN_CODE | RATE_LIMITING.md |
| Polling + optional Realtime | COMPLETED_IN_CODE | ARCHITECTURE.md |
| Production configuration validation (fail-closed) | COMPLETED_IN_CODE | ENVIRONMENT_MATRIX.md |
| Migrations 001–015 (additive, idempotent, tested) | COMPLETED_IN_CODE | MIGRATIONS.md |
| Operator tooling (`scripts/release/`, read-only default) | COMPLETED_IN_CODE (corrected against real Cloud Shell audit) | README.md |
| Cloud Run **Job** service-account inspection (correct describe path, structured JSON) | COMPLETED_IN_CODE | `check-gcp-resources.sh`, `tests/test_release_tooling_cli.py` |
| Vercel production env inspection (supported CLI syntax, names only) | COMPLETED_IN_CODE | `check-vercel-config.sh` |
| launch_unknown reconciliation (RETURNING, one-row, audit-after-success) | COMPLETED_IN_CODE | `reconcile-launch-unknown.sh` |
| Authenticated execution-disabled smoke test | COMPLETED_IN_CODE | `smoke-test-execution-disabled.sh` |
| Consolidated readiness aggregate (true nested totals) | COMPLETED_IN_CODE | `aggregate_reports.py` |
| Authenticated ownership read before run-creation probe | COMPLETED_IN_CODE | `smoke-test-execution-disabled.sh` |
| Secret Manager consumer validation by exact accessor role | COMPLETED_IN_CODE | `check-secret-metadata.sh`, `lib/common.sh` (`iam_role_members`) |
| Vercel project identity fail-closed (ID/org vs `.vercel/project.json`) | COMPLETED_IN_CODE | `check-vercel-config.sh` |
| GCP not-found vs permission/API failure classification | COMPLETED_IN_CODE | `check-gcp-resources.sh`, `check-secret-metadata.sh` |
| Guarded `leave-unresolved` operator decision (identity guard + optional read-only revalidation) | COMPLETED_IN_CODE | `reconcile-launch-unknown.sh` |
| Fail-closed `no-secret-returned` health check | COMPLETED_IN_CODE | `smoke-test-execution-disabled.sh` |
| Membership/proposal backfill generators | COMPLETED_IN_CODE (generation) | MIGRATIONS.md |
| Deployment/rollback command plans, IAM matrix | COMPLETED_IN_CODE (plans) | DEPLOYMENT.md, ROLLBACK.md |
| Release manifest + schema validation | COMPLETED_IN_CODE | `config/production.example.yaml` |
| CI validation of release tooling (dry-run + secret safety) | COMPLETED_IN_CODE | `tests/test_release_tooling.py` |
| GCP project/APIs/Artifact Registry/service accounts/WIF pool | REQUIRES_MANUAL_OPERATOR_CONFIGURATION | below |
| Secret Manager secrets + per-secret IAM | REQUIRES_MANUAL_OPERATOR_CONFIGURATION | below |
| Production Supabase migration application + backfills | REQUIRES_MANUAL_OPERATOR_CONFIGURATION | below |
| Production Redis instance | REQUIRES_MANUAL_OPERATOR_CONFIGURATION | below |
| Vercel production env + deployment | REQUIRES_MANUAL_OPERATOR_CONFIGURATION | below |
| Image build/push + Cloud Run deployment | REQUIRES_MANUAL_OPERATOR_CONFIGURATION | below |
| Stage C provider key + one controlled paid smoke run | REQUIRES_MANUAL_OPERATOR_CONFIGURATION | STAGED_ACTIVATION.md |
| External monitoring/alerting system | REQUIRES_MANUAL_OPERATOR_CONFIGURATION | MONITORING_AND_INCIDENTS.md |
| Short-lived credential flow for gateway | COMPLETED_IN_CODE (already federation-based; no long-lived key to migrate) | AUTHENTICATION.md |
| Automated launch_unknown reconciliation | INTENTIONALLY_DEFERRED | below |
| Supabase Realtime as primary event channel | INTENTIONALLY_DEFERRED | below |

## Catalog, Swarm V2 and frontend stages

Added by CODE-2, which found the table above silently omitted four merged
stages (gap-audit row `OPS-09`). Every row below is a repository claim:
`COMPLETED_IN_CODE` means implemented and tested here, and says nothing about
what is deployed or enabled in any production environment.

| Item | Classification | Reference |
| --- | --- | --- |
| Swarm V2 engine: Commander, plan firewall, model gateway, bounded workers, budgets | COMPLETED_IN_CODE | ARCHITECTURE.md, `backend/engines/swarm_v2/` |
| Trusted engine routing from `project.workflow_key` (never from a payload) | COMPLETED_IN_CODE | `backend/worker/engine.py` |
| Swarm V2 checkpoint/resume and durable terminal persistence | COMPLETED_IN_CODE | `backend/engines/swarm_v2/`; `tests/test_swarm_v2_resume_budget.py`, `tests/test_swarm_v2_smoke_offline.py` (the historical production smoke record is `docs/deployment/swarm-v2-smoke.md`) |
| Bounded read-only Government vehicle Tool (no transport, no credential) | COMPLETED_IN_CODE | `backend/tools/government_vehicle.py` |
| Registered-operation evidence mapping (one allowlisted operation) | COMPLETED_IN_CODE | `backend/engines/swarm_v2/evidence_mapping.py` |
| Lease-guarded, idempotent field-level canonical promotion | COMPLETED_IN_CODE | `backend/catalog/pipeline.py` |
| Catalog migrations (bounded candidate queries, field-level promotion) | COMPLETED_IN_CODE | MIGRATIONS.md |
| **Independent catalog execution kill switch (`MILO_ENABLE_CATALOG_EXECUTION`, default off)** | COMPLETED_IN_CODE | ENVIRONMENT_MATRIX.md, `backend/catalog/execution.py` |
| **Fail-closed catalog capability gating in trusted worker wiring** | COMPLETED_IN_CODE | `backend/worker/main.py`, `tests/test_catalog_execution_flag.py` |
| **Typed recognition for `catalog_variant_promoted` / `catalog_promotion_refused`** | COMPLETED_IN_CODE | `backend/runtime.py`, `frontend/lib/eventVocabulary.ts` |
| **Bounded operator-facing catalog status projection** | COMPLETED_IN_CODE | `frontend/lib/catalogStatus.ts`, `components/inspector/RunInspector.tsx` |
| **Catalog monitoring signals, rollback procedure and staged-activation position** | COMPLETED_IN_CODE (documentation) | MONITORING_AND_INCIDENTS.md, ROLLBACK.md, STAGED_ACTIVATION.md |
| Catalog alerting bound to a real monitoring system | REQUIRES_MANUAL_OPERATOR_CONFIGURATION | MONITORING_AND_INCIDENTS.md |
| Enabling catalog execution in a deployed environment | REQUIRES_MANUAL_OPERATOR_CONFIGURATION (separate explicit authorization) | STAGED_ACTIVATION.md |
| **Guarded operator Government capture entrypoint (CODE-1)** | COMPLETED_IN_CODE — refuses by default; **no live capture has been executed** | `backend/catalog/operator_capture.py`, `tests/test_catalog_operator_capture.py`, `../catalog-code1-operator-capture.md` |
| Performing the first live Government capture | REQUIRES_MANUAL_OPERATOR_CONFIGURATION (OPERATOR-0 first, then AUTH-1) | `../catalog-code1-operator-capture.md`, `../roadmap/MILO_GAP_AUDIT_2026-09-16.md` (OPERATOR-3) |
| A durable Government snapshot in the target database | **not claimed either way** — unobserved from this repository | `../roadmap/MILO_GAP_AUDIT_2026-09-16.md` (S3-02b) |
| **Bounded membership-authorized catalog read/review API and UI (CODE-3)** | COMPLETED_IN_CODE — read-only; **no production database has been inspected and no live snapshot has been reviewed** | `backend/catalog/review.py`, `tests/test_catalog_review_surface.py`, `../catalog-code3-review-surface.md` |
| **CODE-3 reads stay available with catalog execution disabled** | COMPLETED_IN_CODE | ROLLBACK.md, `frontend/e2e/disabled.catalog-review.spec.ts` |
| Canonical field-level provenance detail route | INTENTIONALLY_DEFERRED (out of CODE-3 scope) | `../catalog-code3-review-surface.md` (§Remaining operator work) |
| Reviewing a real canonical row or a real `ready_for_review` candidate | **not claimed either way** — requires OPERATOR-0, then a live capture | `../roadmap/MILO_GAP_AUDIT_2026-09-16.md` (S3-02b, OPERATOR-3) |
| Manufacturer/importer source and targeted-Web evidence tool | INTENTIONALLY_DEFERRED (test-only implementations) | `../roadmap/MILO_GAP_AUDIT_2026-09-16.md` |
| Reconciliation / refresh scheduling and a `legacy_reference` producer | INTENTIONALLY_DEFERRED (no production caller or schedule) | `../roadmap/MILO_GAP_AUDIT_2026-09-16.md` |
| F4 typed Final Result rendering for Swarm V2, with V1 isolated | COMPLETED_IN_CODE | FRONTEND_ACCEPTANCE.md |
| F5 client state ownership, safe error classification, hostile event handling | COMPLETED_IN_CODE | FRONTEND_ACCEPTANCE.md |
| Event recognition before projection (unknown types stay inert) | COMPLETED_IN_CODE | `frontend/lib/eventVocabulary.ts`, FRONTEND_ACCEPTANCE.md |
| Operator UI verification pass against a live browser | REQUIRES_MANUAL_OPERATOR_CONFIGURATION | FRONTEND_PRE_RELEASE.md |

**Nothing in this section claims a deployment.** No catalog dashboard, alert,
deployment or production verification is created by the work these rows
describe; where a row says an alert or an activation is needed, it is operator
work that has not been performed.

## Operator-tooling status and the honest meaning of "not BLOCKED"

The Phase 9–11 operator tooling was found to contain real defects during a
live read-only Google Cloud Shell inspection that the mocked CI suite did not
catch. Those defects (Cloud Run **Job** SA path, Vercel CLI syntax, zero-row
mutation "success", 401-as-execution-disabled proof, inaccurate aggregate
totals, Secret Manager checks without concrete expectations) are corrected in
the corrective PR recorded in [STATUS.md](STATUS.md) and covered by new strict
CLI regression tests (`tests/test_release_tooling_cli.py`).

No **code** item is `BLOCKED` after that corrective pass. This is **not** a
statement that the live production environment passed: every external service
above remains `REQUIRES_MANUAL_OPERATOR_CONFIGURATION` and is only confirmed
by running the authenticated read-only audit against the real project.
**Production deployment itself remains BLOCKED** until the corrective PR is
merged and a real read-only audit report is produced from authenticated
operator tooling. This document never claims an external service was verified
when only repository wiring was checked.

## Manual items — required details

For every `REQUIRES_MANUAL_OPERATOR_CONFIGURATION` item: **responsible
operator** = the human deployment operator recorded as
`identities.deploy_operator` in the manifest copy; **evidence to capture**
= the JSON report of the relevant check script plus command output with
secrets redacted.

| Item | External service | Prerequisite | Command template / UI action | Validation | Rollback |
| --- | --- | --- | --- | --- | --- |
| GCP resources | Google Cloud | approved manifest | MANUAL_SERVICE_CONNECTIONS.md Conn. 3–5 templates | `check-gcp-resources.sh` | remove created bindings/resources |
| Secrets + IAM | Secret Manager | SAs exist | Conn. 5 matrix | `check-secret-metadata.sh` | disable version / remove binding |
| Migrations + backfills | Supabase | backup verified | MIGRATIONS.md sequence | `check-migration-state.sh`; validation queries | forward corrective SQL |
| Redis | Upstash | dedicated prod DB | Conn. 6 | `check-redis-config.sh` | restore endpoint; rotate token |
| Vercel env + deploy | Vercel | Conn. 3 done | deployment plan steps 10–11 | `check-vercel-config.sh`; smoke tests | `vercel promote <previous>` |
| Images + Cloud Run | Artifact Registry / Cloud Run | CI green on release SHA | `generate-deployment-plan.sh` output | revision digest verification | ROLLBACK.md |
| Stage C smoke run | provider | Stages A+B signed off | STAGED_ACTIVATION.md Stage C | acceptance record | kill-switch order |
| Monitoring | operator's system | signals list | MONITORING_AND_INCIDENTS.md | alert test | n/a |
| Catalog alert binding | operator's system | catalog signals list | MONITORING_AND_INCIDENTS.md §Catalog signals | alert test | n/a |
| Enabling catalog execution | Cloud Run worker job | Stages A+B signed off; read-only schema inspection; rollback rehearsed | STAGED_ACTIVATION.md §Catalog execution | flag value on the worker revision; catalog event counts | `MILO_ENABLE_CATALOG_EXECUTION=false` (ROLLBACK.md §Catalog execution) |

## Deferred items — required details

| Item | Reason | Risk | Safe current behavior | Condition to implement |
| --- | --- | --- | --- | --- |
| Supervisor active autonomy | decision quality unvalidated | wrong automated interventions | shadow mode records decisions without acting | review of accumulated shadow decisions + explicit approval |
| Automated launch_unknown reconciliation | requires trustworthy external evidence correlation | double execution / double spend if wrong | runs park as launch_unknown; operator resolves with audited tooling | proven reconciliation signal from Cloud Run execution API + operator sign-off |
| Realtime as primary event channel | polling is sufficient and simpler to reason about | none (polling active) | polling with optional Realtime enhancement | scale needs + Realtime authorization review |

## Exact manual operator sequence

### Before deployment

1. choose and record the approved release SHA (manifest `release.sha`);
2. review all CI checks on that SHA; 3. review the migration plan;
4. confirm a database backup; 5. verify the GCP account
(`gcloud config get-value account`); 6. verify the GCP project;
7. verify the Supabase project ref; 8. verify the Vercel project;
9. verify the production Redis instance; 10. verify service-account
separation; 11. verify required APIs; 12. verify Artifact Registry;
13. verify secret resource names; 14. verify secret-level IAM; 15. verify
all execution flags off, `MILO_ENABLE_CATALOG_EXECUTION` included;
16. verify the rollback revision/image
(`release.rollback_sha`); 17. obtain change approval.

(Steps 5–15 are automated read-only by
`scripts/release/production-readiness.sh` with full arguments.)

### External resources to create/configure manually

Command templates for all of these are in
MANUAL_SERVICE_CONNECTIONS.md and the generated deployment plan: Artifact
Registry repository; API service account; worker service account; gateway
identity (+ WIF pool/provider); launcher IAM; secret-level access; private
Cloud Run worker job; private Cloud Run API; Redis credential/reference;
Vercel server environment; Supabase redirect URLs; explicit CORS origins.

### Database sequence

MIGRATIONS.md §Manual database sequence (16 steps: inspect → plan →
backup → apply in order → validate records/RLS/functions → membership
backfill generate/review/apply/validate → proposal backfill
generate/review/apply/validate → rerun read-only checks).

### Deployment sequence

DEPLOYMENT.md §Strict deployment order (build both immutable images →
push manually → worker job first (never execute) → verify → private API →
verify image/identity/IAM → Vercel configure + deploy → Stage A smoke
tests).

### Activation sequence

STAGED_ACTIVATION.md (complete Stage A → complete Stage B → explicit
approval for Stage C → provider key entered manually → verify strict
budgets → allowlist one operator test project → one controlled smoke run →
review cost/events → disable immediately on failure → Stage D only after
approval).

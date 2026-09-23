# MILO production readiness — authoritative documentation set

This directory is the single authoritative source for the production
architecture, operator tooling, deployment preparation and staged
activation of the MILO agent workspace. It reflects the repository as of
Phases 1–11. Where an older document under `docs/` contradicts this set,
this set wins and the older document carries an archive banner.

## Navigation

| Document | Contents |
| --- | --- |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Final architecture, trust boundaries, diagrams |
| [AUTHENTICATION.md](AUTHENTICATION.md) | Browser, gateway and worker authentication |
| [AUTHORIZATION_AND_RLS.md](AUTHORIZATION_AND_RLS.md) | Ownership, membership, RLS |
| [RUN_LIFECYCLE.md](RUN_LIFECYCLE.md) | Run/launch lifecycle, leases, idempotency, cancellation |
| [RUN_FINALIZATION.md](RUN_FINALIZATION.md) | The one finalizer, the canonical ProductOutcome, terminal races, Stage D semantic acceptance |
| [EVIDENCE_AUTHORITY.md](EVIDENCE_AUTHORITY.md) | The canonical evidence model, the CURRENT verdict rule, V1's evidence flow and the promotion gates |
| [RUN_IDENTITY.md](RUN_IDENTITY.md) | The immutable run identity, full worker persistence fencing, the canonical event registry, export identity and the release binding chain |
| [BUDGETS_AND_COSTS.md](BUDGETS_AND_COSTS.md) | Hard budgets, reservations, settlement, costs |
| [RATE_LIMITING.md](RATE_LIMITING.md) | Shared-store rate limiting (gateway + API) |
| [PROVIDER_AUTHORITY.md](PROVIDER_AUTHORITY.md) | The one provider adapter: outcome taxonomy, retry/admission, token admission, MILO-mediated standalone search, absolute deadline |
| [ENVIRONMENT_MATRIX.md](ENVIRONMENT_MATRIX.md) | Every production variable, classified |
| [MIGRATIONS.md](MIGRATIONS.md) | Migration order, states, backfills |
| [MANUAL_SERVICE_CONNECTIONS.md](MANUAL_SERVICE_CONNECTIONS.md) | The nine external service connections |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Immutable images, deployment order, IAM matrix |
| [STAGED_ACTIVATION.md](STAGED_ACTIVATION.md) | Stages A–D activation runbook |
| [SCOPED_BATCH_PRODUCTION_RUNBOOK.md](SCOPED_BATCH_PRODUCTION_RUNBOOK.md) | Gated Production rollout of the Mapping Plan → prepared batch → Swarm V2 path, to the first website-initiated paid batch |
| [SMOKE_TESTING.md](SMOKE_TESTING.md) | Read-only and execution-disabled smoke tests |
| [FRONTEND_ACCEPTANCE.md](FRONTEND_ACCEPTANCE.md) | Stage F5 frontend acceptance matrix (A–G), classifications and evidence |
| [FRONTEND_PRE_RELEASE.md](FRONTEND_PRE_RELEASE.md) | Operator UI verification pass before a release is accepted |
| [MONITORING_AND_INCIDENTS.md](MONITORING_AND_INCIDENTS.md) | Signals, alerts, incident response, kill switches |
| [ROLLBACK.md](ROLLBACK.md) | Forward-safe rollback for every component |
| [FINAL_ACCEPTANCE.md](FINAL_ACCEPTANCE.md) | Phases 9–11 acceptance audit and classifications |
| [STATUS.md](STATUS.md) | Live branch/PR/test status |
| [STAGE_B_ACCEPTANCE.md](STAGE_B_ACCEPTANCE.md) | Stage B acceptance record |
| [STAGE_C_ACCEPTANCE.md](STAGE_C_ACCEPTANCE.md) | Stage C acceptance record — **PASSED 2026-08-22; the one-run authorization is consumed** |
| [STAGE_D_AUTHORIZATION.md](STAGE_D_AUTHORIZATION.md) | Stage D expansion step 1 — **attempt 1 executed 2026-09-19 and FAILED (`timed_out`); attempt 2 PROPOSED, NOT authorized and NOT executed** |
| [../catalog-code1-operator-capture.md](../catalog-code1-operator-capture.md) | CODE-1: the guarded operator Government capture entrypoint — arguments, prerequisites, stop conditions, report contract and rollback. Implemented in code; **no live capture has been executed** |
| [OPERATOR_2_CONTROL_PLANE_READ_ONLY_REVIEW_2026-09-21.md](OPERATOR_2_CONTROL_PLANE_READ_ONLY_REVIEW_2026-09-21.md) | Read-only Production review after Consoles 1-6: required migration/RPC inventory, ACL/RLS posture, catalog state, run baseline. **Four runtime-required migrations were unapplied at review time — applied 2026-09-22, see OPERATOR_3** |
| [OPERATOR_3_PRODUCTION_ALIGNMENT_2026-09-22.md](OPERATOR_3_PRODUCTION_ALIGNMENT_2026-09-22.md) | Production migration alignment to Console 6 (38/38, head `20260921000200`): SHA-bound backup / dry-run / apply run ids, the 45/45 derived RPC inventory re-proven from `pg_proc`, run identity, ACLs, and the stale-release prerequisite for any future paid run |
| [../roadmap/MILO_GAP_AUDIT_2026-09-16.md](../roadmap/MILO_GAP_AUDIT_2026-09-16.md) | Gap audit after Catalog PR3 / F4 / F5: what is production-connected, what exists but is not activated, what is fixture-only, and what remains missing |

Operator tooling lives in `scripts/release/` (read-only by default; see
`scripts/release/production-readiness.sh --help`). The non-secret release
manifest template is `config/production.example.yaml`, validated by
`scripts/release/validate_production_manifest.py`.

> **Corrective pass (real Cloud Shell audit).** A follow-up corrective PR
> hardened this tooling against real Cloud Run / Vercel / Supabase / Redis
> behavior after a live read-only Cloud Shell inspection exposed defects the
> earlier mocked CI did not catch (wrong Cloud Run **Job** service-account
> path, unsupported Vercel CLI syntax, a mutation "success" that changed zero
> rows, a bare 401 accepted as execution-disabled proof, an inaccurate
> aggregate total, and Secret Manager checks that ran without concrete
> expectations). The tooling is now `COMPLETED_IN_CODE`, but **external
> production state is only ever confirmed by running the authenticated
> read-only audit against the real project** — the tooling never claims a
> service was verified when only repository wiring was checked. See
> [STATUS.md](STATUS.md) and [FINAL_ACCEPTANCE.md](FINAL_ACCEPTANCE.md).

## Completion classifications

Every major item in this documentation set is classified as exactly one of:

- `COMPLETED_IN_CODE` — implemented and tested in this repository;
- `REQUIRES_MANUAL_OPERATOR_CONFIGURATION` — the repository provides the
  exact command template/validation, but a human with real production
  access must perform it;
- `INTENTIONALLY_DEFERRED` — deliberately not implemented yet, with reason,
  risk and the condition for future implementation;
- `BLOCKED` — cannot proceed until a named prerequisite is resolved.

The consolidated classification table is in
[FINAL_ACCEPTANCE.md](FINAL_ACCEPTANCE.md).

[FRONTEND_ACCEPTANCE.md](FRONTEND_ACCEPTANCE.md) uses a different set
(`IMPLEMENTED_AND_PROVEN`, `IMPLEMENTED_TEST_GAP`, `CONFIRMED_DEFECT`, plus the
manual/deferred/out-of-scope labels above) because it answers a different
question: whether a browser behaviour is proven by a test that exercises the
production path, rather than whether an external service is configured. Where it
records a live-posture question — Cloud Run privacy, the real Vercel
environment — it defers to this set and to the operator audit in
[SMOKE_TESTING.md](SMOKE_TESTING.md).

## Non-negotiable safety invariants

1. Every execution flag defaults to OFF; there is deliberately no
   enable-all command anywhere in the repository.
2. Operator scripts default to check/plan/dry-run; any mutation requires
   the full protected apply mode (`--apply --environment production
   --expected-project … --expected-account … --expected-sha …
   --confirm-production-change` plus
   `MILO_OPERATOR_ACK=I_UNDERSTAND_THIS_CHANGES_PRODUCTION`).
3. Image tags are immutable full commit SHAs; `latest`/`prod`/`stable`/
   branch tags are rejected.
4. The Cloud Run API and worker job are private; API and worker use
   separate service accounts; gateway and worker identities never overlap.
5. Secrets live in Secret Manager (or Vercel server env for the gateway),
   are granted per-secret, and are never printed by any tool in this
   repository.
6. Migrations are applied manually, forward-only, and never destructively.

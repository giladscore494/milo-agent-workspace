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
| [BUDGETS_AND_COSTS.md](BUDGETS_AND_COSTS.md) | Hard budgets, reservations, settlement, costs |
| [RATE_LIMITING.md](RATE_LIMITING.md) | Shared-store rate limiting (gateway + API) |
| [ENVIRONMENT_MATRIX.md](ENVIRONMENT_MATRIX.md) | Every production variable, classified |
| [MIGRATIONS.md](MIGRATIONS.md) | Migration order, states, backfills |
| [MANUAL_SERVICE_CONNECTIONS.md](MANUAL_SERVICE_CONNECTIONS.md) | The nine external service connections |
| [BOOTSTRAP.md](BOOTSTRAP.md) | One-command production bootstrap and audit (`bootstrap-production.sh`) |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Immutable images, deployment order, IAM matrix |
| [STAGED_ACTIVATION.md](STAGED_ACTIVATION.md) | Stages A–D activation runbook |
| [SMOKE_TESTING.md](SMOKE_TESTING.md) | Read-only and execution-disabled smoke tests |
| [MONITORING_AND_INCIDENTS.md](MONITORING_AND_INCIDENTS.md) | Signals, alerts, incident response, kill switches |
| [ROLLBACK.md](ROLLBACK.md) | Forward-safe rollback for every component |
| [FINAL_ACCEPTANCE.md](FINAL_ACCEPTANCE.md) | Phases 9–11 acceptance audit and classifications |
| [STATUS.md](STATUS.md) | Live branch/PR/test status |

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

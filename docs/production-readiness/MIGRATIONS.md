# Migrations

Status: migration content `COMPLETED_IN_CODE`; production application is
`REQUIRES_MANUAL_OPERATOR_CONFIGURATION`. No operator script in
`scripts/release/` ever applies a migration. One pre-existing guarded
workflow exists (`.github/workflows/deploy-supabase-migrations.yml`): on
push to `main` it runs a dry-run preflight only (auto-apply requires the
repository variable `SUPABASE_MIGRATIONS_AUTO_APPLY=true`, which must stay
unset), and a manual `workflow_dispatch` apply additionally requires
typing `APPLY_PRODUCTION_MIGRATIONS`. Pull-request CI never touches
production.

## Order (apply strictly in this sequence)

| # | File | Adds |
| --- | --- | --- |
| 001 | `001_project_workspace.sql` | projects, conversations/messages reconciliation with the legacy baseline |
| 002 | `002_durable_runtime.sql` | durable run columns, run_events, run_checkpoints, worker_heartbeats, status model |
| 003 | `003_workflow_proposals.sql` | workflow_proposals |
| 004 | `004_supervisor_shadow_mode.sql` | supervisor_decisions (shadow only) |
| 005 | `005_internet_governance.sql` | tool_access_requests, tool_grants, tool_usage, sources, claims, conflicts |
| 006 | `006_deployment_hardening.sql` | stuck_runs view, run_invocations, hardening |
| 007 | `007_project_members.sql` | project_members + membership RLS |
| 008 | `008_workflow_proposal_ownership.sql` | proposal created_by/project_id + ownership RLS |
| 009 | `009_run_idempotency_lifecycle.sql` | requested_by, idempotency, launch_state |
| 010 | `010_run_usage.sql` | runs.usage aggregate |
| 011 | `011_proposal_ownership_protection.sql` | ownership tamper protection, atomic project+owner creation |
| 012 | `012_atomic_run_operations.sql` | create_message_and_run, launch CAS, launch_unknown, claim_run_lease, lease_token |
| 013 | `013_usage_ledger.sql` | append-only run_usage_ledger |
| 014 | `014_atomic_daily_budget_reservations.sql` | legacy daily RPCs (deprecated, execute revoked) |
| 015 | `015_atomic_model_call_budget_lifecycle.sql` | model_call_budget_reservations + reserve/settle RPCs, portable grants |
| ts | `20260706192500_grant_…_schema_privileges.sql` | service-role schema privileges (timestamped) |
| ts | `20260810000100_revoke_anon_execute_on_service_rpcs.sql` | revoke anon EXECUTE on all service-only RPCs + default-privilege hardening |
| ts | `20260810000200_enable_rls_on_service_only_tables.sql` | explicit RLS on service-only tables from 002/004/005/015 |
| ts | `20260810000300_lease_guarded_worker_writes.sql` | assert_worker_lease + lease-guarded RPCs for every worker durable write |
| ts | `20260810000400_setof_rpc_returns.sql` | SETOF `_v2` wrappers for historical RPCs (PostgREST client compatibility) |
| ts | `20260810000500_ledger_decision_overage.sql` | run_usage_ledger decision check widened to overage/released |
| ts | `20260810000600_corrective_lease_and_attempt_hardening.sql` | attempt-aware reservation identity (run_id, attempt, call_seq); cross-run-safe guarded settle; DB-clock guarded usage/heartbeat/worker-transition RPCs |
| ts | `20260818000100_stage_c_service_role_rpc_acl.sql` | service_role EXECUTE on the create_message_and_run / create_project_from_proposal_with_owner base RPCs (Stage C Attempt 4 corrective) |
| ts | `20260818000200_claim_run_lease_service_role_acl.sql` | service_role EXECUTE on claim_run_lease (Stage C Attempt 5 corrective; full worker-path ACL contract now enforced by `tests/test_worker_rpc_acl_postgres.py`) |
| ts | `20260914200000_catalog_evidence_foundation.sql` | Catalog PR1: the durable catalog namespace — `catalog_source_snapshots`, `catalog_raw_records`, `catalog_candidate_variants`, `catalog_candidate_evidence_links` plus the **empty** canonical `catalog_models` / `catalog_model_variants`, their lease-guarded RPCs, append-only triggers, RLS and least-privilege grants |
| ts | `20260915120000_catalog_integrity_corrections.sql` | Catalog PR1 corrective round: derived evidence-link provenance (verified verdicts only, locator/version read from the cited claim and source), required `claim_id`, terminal `failed` snapshots, creating-run snapshot ownership, derived payload digests, domain-separated identity keys with natural uniqueness, composite cross-table foreign keys and their indexes, and fully immutable canonical rows |
| ts | `20260915180000_catalog_raw_record_source_locator.sql` | Catalog PR2: one generic `source_locator` jsonb column on `catalog_raw_records` (closed key vocabulary `capture_index` / `page_index` / `page_number` / `page_offset`, bounded, position-unique per snapshot) so a stored record states WHERE in a paginated retrieval it came from; the raw-record RPC carries and replay-checks it |
| ts | `20260916090000_catalog_bounded_candidate_queries.sql` | Catalog PR3: six bounded, fixed-order, exactly-totalled READ functions over `catalog_candidate_variants` / `catalog_raw_records`, each gated on an active, complete, USABLE snapshot, plus three indexes created with the same `collate "C"` the functions order by. Generic over the catalog relations — nothing in the file names `data.gov.il`, CKAN, a Government field or a vehicle |
| ts | `20260916120000_catalog_field_level_promotion.sql` | Catalog PR3: `catalog_canonical_field_provenance` (append-only, ONE row per promoted canonical FACT), the BEFORE-INSERT support-chain trigger, the two DEFERRED coverage triggers, derived canonical keys and natural uniqueness, the `catalog_canonical_field_current` / `catalog_canonical_variant_current` read model, and `promote_catalog_variant_guarded`. Grants `service_role` `INSERT` on the canonical pair and on the provenance relation, and nothing else |

All migrations are additive, idempotent and data-preserving. There are no
destructive down-migrations, by policy (`scripts/check_migrations.py`
forbids `drop table` and data deletes).

### Catalog PR1 — migration and rollback impact

`20260914200000_catalog_evidence_foundation.sql` is purely additive: it
creates six new relations, one immutable predicate, five lease-guarded RPCs
and their triggers, and touches no existing table, column, RPC, index, policy
or grant. It **seeds nothing** — no row is inserted by the migration, and the
canonical relations (`catalog_models`, `catalog_model_variants`) are created
empty and are `SELECT`-only for `service_role`, so no write path that exists
today can put a row in either. The existing aggregated Yeda catalog is
deliberately **not** imported; it is representable only as a
`legacy_reference` snapshot whose trust state is pinned to `unverified`.

### Catalog PR2 — migration and rollback impact

`20260915180000_catalog_raw_record_source_locator.sql` is forward-only and
additive: it adds ONE nullable-by-default `jsonb` column to the existing
`public.catalog_raw_records`, one immutable predicate that closes its key
vocabulary, two CHECK constraints, one partial unique index, and it redefines
`record_catalog_raw_record_guarded` in place with the same name, signature and
lease posture. It creates no table, alters no other relation, backfills
nothing, and leaves the relation's append-only trigger untouched — so a locator
is written exactly once with its row and can never be revised.

The column is GENERIC rather than Government-specific: `page_offset`,
`page_index`, `page_number` and `capture_index` describe any paginated
retrieval, and nothing in the file names `data.gov.il`, CKAN, a Government
field or a vehicle. Catalog PR2 adds **no** Government-specific table.

The column exists because a snapshot's `retrieval_metadata` records the page
plan while an individual row could not say which page it came out of, so
"traceable to the exact page and record" would otherwise be a claim rather than
a stored fact.

Rollback: the relation is still empty, so reverting means not using the column.
A forward migration could drop it; nothing reads it outside
`backend/catalog/government/`.

### Catalog PR3 — migration and rollback impact

`20260916090000_catalog_bounded_candidate_queries.sql` is purely additive and
READ-ONLY in effect: six functions, three `create index if not exists`, and an
`EXECUTE` grant to `service_role` after revoking `PUBLIC`/`anon`/`authenticated`.
It creates no table, alters no relation, changes no existing function and
backfills nothing. Rollback: the functions are unused if nothing calls them; a
forward migration could drop them.

`20260916120000_catalog_field_level_promotion.sql` creates ONE table
(`catalog_canonical_field_provenance`), two views, four functions, four
triggers and eleven indexes, and tightens the two canonical relations that are
still EMPTY: their `canonical_key` checks become derived-identity patterns
(`^cm1\.[0-9a-f]{32}$` / `^cv1\.[0-9a-f]{32}$`) and `catalog_models` gains a
natural-uniqueness constraint on `(manufacturer, commercial_model)`. Every
tightening is a no-op against existing data by construction, because both
relations hold zero rows.

The one privilege change is deliberate and narrow: `service_role` gains
`INSERT` on `catalog_models`, `catalog_model_variants` and the new provenance
relation. `UPDATE` and `DELETE` stay revoked on all three, and the two
DEFERRED constraint triggers make a canonical row without complete verified
per-field provenance impossible to COMMIT for any writer — which is what makes
the grant safe. Both views are created with `security_invoker = true` and have
`REVOKE ALL` applied to `service_role` before the single `SELECT` grant,
because a view is a new object and Supabase default privileges would otherwise
hand it every privilege.

Rerun safety: both files are part of the ordered catalog set the executable
test replays twice (`test_catalog_migration_applies_and_is_rerun_safe`). Every
`create` is `if not exists` or `create or replace`, every `add constraint` is
preceded by a drop of both its own name and the PR1 name it replaces, and every
trigger is dropped before it is created.

Rollback: the canonical relations are empty before this PR and nothing in a
release promotes into them, so reverting means not calling the promotion RPC. A
forward corrective migration could revoke the `INSERT` again; nothing else in
the schema depends on it.

### The corrective round — migration and rollback impact

`20260915120000_catalog_integrity_corrections.sql` is forward-only and does not
rewrite the migration it corrects. It operates on relations that are still
empty, so every tightening it applies — `claim_id` becoming `NOT NULL`, the key
domain/shape checks, the natural uniqueness indexes, the composite foreign keys
— is a no-op against existing data by construction. It redefines three RPC
bodies and two trigger functions in place, and adds eight indexes.

Rerun safety is a property of the **ordered set**: replaying the foundation
migration alone would restore the function bodies this one corrected, so the
executable test replays both files in order
(`test_catalog_migration_applies_and_is_rerun_safe`). Each `add constraint` is
paired with drops of both its own name and the PR1 name it replaces, and the
composite foreign keys are dropped before the unique constraints they depend on
and re-added afterwards.

Rollback: the catalog is still empty, so reverting means not using these
relations. A corrective forward migration could restore the looser PR1
definitions, but doing so would reintegrate the defects this round fixed.

Rollback is the usual forward-only story, and it is unusually cheap here:
because nothing reads or writes these relations yet, reverting means simply
not using them. If the schema itself must go, the corrective forward
migration drops the six relations, the five RPCs and the predicate — an
operation that loses no data while the catalog is empty. No existing
behaviour, row or contract depends on this migration.

## Confirmed legacy baseline

The production Supabase project began with four pre-existing tables
(`conversations`, `messages`, `runs`, `run_events`) and an empty migration
history. Migrations `001`/`002` contain explicit reconciliation clauses
(column renames/backfills such as `sender_role → role`, `progress →
progress_percent`) whose presence is enforced by
`scripts/check_migrations.py`.

## Supported remote states

`scripts/release/check-migration-state.sh` classifies a remote schema
(read-only, operator-supplied connection) as one of:

- **empty-schema** — apply 001→015 in order;
- **legacy-baseline** — the confirmed baseline above; apply 001→015 in
  order (reconciliation clauses handle the existing rows);
- **partially-migrated** — apply only the missing tail, in order, after
  reviewing the reported markers;
- **fully-migrated** — nothing to apply; rerunning is safe (idempotent).

All four states (plus rerun/idempotency) are executably tested against
real PostgreSQL in CI (`tests/test_migrations_postgres.py`,
`MILO_REQUIRE_PG_TESTS=1`, zero skips allowed).

## Manual database sequence

1. `scripts/release/check-migration-state.sh --database-url-env
   MILO_READONLY_DB_URL --plan-output migration-plan.json`
2. Review the plan and hashes; create and verify an encrypted pre-migration backup via [SUPABASE_BACKUP.md](SUPABASE_BACKUP.md), and confirm its matching passphrase remains available in the approved operator secret store.
3. Apply each pending migration manually, in order, via `psql` or the
   Supabase SQL editor.
4. Re-run the state check; validate RLS and function permissions
   (validation queries in the PostgreSQL test suite mirror these checks).
5. Generate and review the membership backfill
   (`generate-membership-backfill.sh`), check row counts, apply manually,
   validate ownership.
6. Generate and review the proposal backfill
   (`generate-proposal-backfill.sh`), check row counts, apply manually,
   validate proposals.
7. Rerun read-only checks (`smoke-test-read-only.sh`).

## Rollback

Forward-only: stop execution, verify backup, write a corrective forward
migration, review manually, apply after explicit approval, re-verify RLS
and ownership. See [ROLLBACK.md](ROLLBACK.md) §Migrations.

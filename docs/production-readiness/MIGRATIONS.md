# Migrations

Status: migration content `COMPLETED_IN_CODE`; production application is
`REQUIRES_MANUAL_OPERATOR_CONFIGURATION`. No operator script in
`scripts/release/` ever applies a migration. One pre-existing guarded
workflow exists (`.github/workflows/deploy-supabase-migrations.yml`): on
push to `main` it runs a dry-run preflight only (auto-apply requires the
repository variable `SUPABASE_MIGRATIONS_AUTO_APPLY=true`, which must stay
unset). A manual `workflow_dispatch` must always supply `expected_sha` —
the full 40-character lowercase SHA of the reviewed commit — and the run
refuses to continue unless `GITHUB_SHA` equals it. A manual **apply**
additionally requires `mode=apply` and typing `APPLY_PRODUCTION_MIGRATIONS`;
all four gates are checked before the Supabase CLI is installed and before the
project is linked, and each one fails the run rather than skipping a step. No
repository or environment variable can relax the manual SHA gate.

The Supabase credentials are declared on the individual steps that use them,
never at job scope, so an unauthorized dispatch is refused **before any
production credential exists in a step environment at all** — a job-level
`env:` would otherwise place them in front of the gate even though no step
there reads them. `SUPABASE_PROJECT_ID` reaches only the two steps whose
scripts name it; after `supabase link` the CLI resolves the project from the
workspace. Pull-request CI never touches production.

## Order (apply strictly in this sequence)

This table is the authoritative strict apply order and is **executably
checked**: `tests/test_migration_state_verification.py` fails if any file in
`supabase/migrations/` is missing from it, listed twice, listed under a name
that is not a real file, or listed out of canonical order. Adding a
migration without adding its row here breaks the build rather than silently
producing an incomplete sequence.

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
| ts | `20260706192500_grant_service_role_schema_privileges.sql` | service-role schema privileges (timestamped) |
| ts | `20260810000100_revoke_anon_execute_on_service_rpcs.sql` | revoke anon EXECUTE on all service-only RPCs + default-privilege hardening |
| ts | `20260810000200_enable_rls_on_service_only_tables.sql` | explicit RLS on service-only tables from 002/004/005/015 |
| ts | `20260810000300_lease_guarded_worker_writes.sql` | assert_worker_lease + lease-guarded RPCs for every worker durable write |
| ts | `20260810000400_setof_rpc_returns.sql` | SETOF `_v2` wrappers for historical RPCs (PostgREST client compatibility) |
| ts | `20260810000500_ledger_decision_overage.sql` | run_usage_ledger decision check widened to overage/released |
| ts | `20260810000600_corrective_lease_and_attempt_hardening.sql` | attempt-aware reservation identity (run_id, attempt, call_seq); cross-run-safe guarded settle; DB-clock guarded usage/heartbeat/worker-transition RPCs |
| ts | `20260818000100_stage_c_service_role_rpc_acl.sql` | service_role EXECUTE on the create_message_and_run / create_project_from_proposal_with_owner base RPCs (Stage C Attempt 4 corrective) |
| ts | `20260818000200_claim_run_lease_service_role_acl.sql` | service_role EXECUTE on claim_run_lease (Stage C Attempt 5 corrective; full worker-path ACL contract now enforced by `tests/test_worker_rpc_acl_postgres.py`) |
| ts | `20260823000100_lease_guarded_evidence_writes.sql` | S1-PR4: retry-safe, lease-guarded Evidence Board writes — `idempotency_key`/`task_key` provenance columns on `tool_usage`, and `evidence_key`/`task_key` on `sources`, `claims` and `conflicts`, with per-run partial unique indexes; the `*_guarded` RPCs (`create_tool_usage_guarded`, `create_source_guarded`, `create_claim_with_source_guarded`, `create_conflict_guarded`) that take and validate the full run lease, reject unsafe payloads and make a replayed write idempotent rather than duplicated |
| ts | `20260828000100_canonical_scope_conflict_identity.sql` | B1 correction: `claims.canonical_scope_hash` + `claims.scope_normalization_version`, the trusted canonical scope identity computed by the backend (never recomputed in SQL) and stored beside the untouched original scope fields, so `create_conflict_guarded` compares canonical identity instead of raw text. Fail-closed on unknown, cross-run, mixed-scope, mixed-version, single-value or identity-less claim groups; legacy claims keep a NULL identity and can never join a canonical-scope conflict group |
| ts | `20260828000200_source_evidence_fragments.sql` | B2: `source_evidence_fragments` — the first durable home for the actual source TEXT a claim rests on (`fragment_text`, `content_hash`, `fragment_index`), with the character/count bounds enforced in the database so no backend release or direct RPC call can store a whole page. A service-only relation with RLS and zero policies: fragment text is verifier-internal and never becomes browser payload via a run event |
| ts | `20260902000100_r3_versioned_focused_evidence.sql` | R3: WHICH version of a source was read and WHERE inside it the quote came from — `sources.source_version_kind`/`source_version_id`, `claims.evidence_locator`, `source_evidence_fragments.fragment_type`/`locator_key`, plus three immutable predicate functions holding one SQL definition of the shape rules that BOTH the guarded RPCs and the table CHECK constraints call, so a direct insert is held to the same contract. Additive and nullable: pre-R3 rows keep NULLs and are never retro-invalidated |
| ts | `20260907000100_r4_deterministic_verification.sql` | R4: deterministic verification made durable — `claims.identity_scope` (closed identity dimensions), and the new service-only relations `claim_verdicts` (one verdict per claim per run), `claim_verdict_supports` (the exact fragments behind it) and `conflict_resolutions` (one typed, append-only decision per contradicting scope). New relations rather than columns on `claims`/`conflicts`, because those are already browser-visible through run events and verdict support is verifier-internal provenance |
| ts | `20260914200000_catalog_evidence_foundation.sql` | Catalog PR1: the durable catalog namespace — `catalog_source_snapshots`, `catalog_raw_records`, `catalog_candidate_variants`, `catalog_candidate_evidence_links` plus the **empty** canonical `catalog_models` / `catalog_model_variants`, their lease-guarded RPCs, append-only triggers, RLS and least-privilege grants |
| ts | `20260915120000_catalog_integrity_corrections.sql` | Catalog PR1 corrective round: derived evidence-link provenance (verified verdicts only, locator/version read from the cited claim and source), required `claim_id`, terminal `failed` snapshots, creating-run snapshot ownership, derived payload digests, domain-separated identity keys with natural uniqueness, composite cross-table foreign keys and their indexes, and fully immutable canonical rows |
| ts | `20260915180000_catalog_raw_record_source_locator.sql` | Catalog PR2: one generic `source_locator` jsonb column on `catalog_raw_records` (closed key vocabulary `capture_index` / `page_index` / `page_number` / `page_offset`, bounded, position-unique per snapshot) so a stored record states WHERE in a paginated retrieval it came from; the raw-record RPC carries and replay-checks it |
| ts | `20260916090000_catalog_bounded_candidate_queries.sql` | Catalog PR3: seven bounded, fixed-order, exactly-totalled READ functions over `catalog_candidate_variants` / `catalog_raw_records`, each gated on an active, complete, USABLE snapshot, plus three indexes created with the same `collate "C"` the functions order by. Every paged function returns a COUNT ROW when its page is empty, so an offset past the last matching row states the real total instead of zero. `catalog_snapshot_candidate_diff` compares two whole snapshots INSIDE the database and returns exact added/changed/removed counts with a bounded delta list. Generic over the catalog relations — nothing in the file names `data.gov.il`, CKAN, a Government field or a vehicle |
| ts | `20260916120000_catalog_field_level_promotion.sql` | Catalog PR3: `catalog_canonical_field_provenance` (append-only, ONE row per promoted canonical FACT, carrying the entity, market, time and identity scope the fact was read under), the BEFORE-INSERT support-chain trigger — which also holds each fact to the candidate's VEHICLE, model-year scope, market scope, identity scope and source-record locator, and binds the link, source, claim, verdict and provenance row to ONE run — the two DEFERRED coverage triggers, derived canonical keys and natural uniqueness, the `catalog_canonical_field_current` / `catalog_canonical_variant_current` read model, `catalog_run_pending_promotions` (the bounded READ that derives what one run still has to promote, so a replaced worker resumes from durable rows rather than from a lost process), and `promote_catalog_variant_guarded`. Grants `service_role` `INSERT` on the canonical pair and on the provenance relation, and nothing else |
| ts | `20260920000100_execution_usage_ledger.sql` | ExecutionUsageLedger: `run_execution_usage` (ONE durable, versioned, monotonic cumulative usage record per run), `merge_execution_usage` (component-wise maximum), `execution_usage_public_projection`, the `BEFORE UPDATE` trigger that refuses any write lowering a counter, and `record_run_usage_guarded` (lease under the database clock with the runs row `FOR UPDATE`, merge never overwrite, `version` advances only on a real change, `runs.usage` projected in the same transaction). `update_run_usage_guarded` and `transition_run_worker_guarded` keep their signatures and become monotonic. Service-path only; RLS with no policies on the new table. ORDER MATTERS: `20260810000600` redefines those two writers with their old overwrite bodies, so if it is ever re-run, re-apply `20260920000100` after it (rerun-safe; proven by `test_reapplying_the_corrective_migration_out_of_order_needs_the_ledger_migration_again`) |
| ts | `20260920000200_atomic_run_finalization.sql` | Atomic run finalization: `finalize_run_guarded` performs the run's TERMINAL transition and its terminal event insert in ONE transaction — lease under the database clock, attempt and token, and a MANDATORY compare-and-set on the status the decision was taken under — so a terminal event can never exist for a decision that did not durably win, and a terminal run can never be left without the evidence its event carries (the canonical ProductOutcome Stage D reads). Terminal statuses only; `runs.usage` merges monotonically. Service-path only. Depends on `merge_execution_usage` from `20260920000100`, which orders before it |
| ts | `20260921000100_current_verdict_authority.sql` | R5: CURRENT verdict authority — `claim_current_verdict_id` / `claim_current_verdict_state` / `claim_current_verdict_states` / `claim_verdict_is_current_support` resolve which verdict of an append-only history is TRUE NOW (latest by `created_at`, a non-`verified` row winning an exact tie, then id; `invalidated` / `superseded` / `contested` ahead of it; a `verified` citing no durable support row is `unsupported`), plus two BEFORE-INSERT triggers holding every writer to it — an evidence link may not cite a non-current verdict, and canonical field provenance may not be written from one — and `catalog_run_pending_promotions` rewritten to resolve current truth instead of joining "a verified verdict exists". Additive and forward-only: no verdict already durable is changed, only which of them is read as current. Mirrors `backend/engines/swarm_v2/current_verdict.py` |
| ts | `20260921000200_immutable_run_identity.sql` | Console 6 control-plane boundary. `runs.run_identity` stays nullable only for historical rows, while every NEW run must be born with a complete immutable identity including a full release SHA. `create_message_and_run_v3` commits the user message, run and identity atomically and re-checks the trusted project workflow; the superseded `bind_run_identity`, `create_message_and_run` and `create_message_and_run_v2` RPCs are removed. `runs_forbid_identity_rewrite` rejects every post-INSERT identity change (including legacy NULL→value retrofit), and `claim_run_lease` refuses identity-less legacy rows. `transition_run_worker_guarded` is redefined as NON-TERMINAL so `finalize_run_guarded` remains the only worker terminalization primitive. The migration also adds lease-guarded tool-access, tool-grant and per-call usage-ledger writers. Service-path only; forward-only and data-preserving, but deliberately not purely additive because obsolete RPC definitions are dropped. |
| ts | `20260922000100_catalog_work_scopes.sql` | Scoped catalog PR1: the canonical, server-owned mapping PLAN. `catalog_work_scopes` (one row per plan, its head revision and digest, at most one OPEN plan per conversation by a partial unique index) and `catalog_work_scope_revisions` (append-only, numbered without gaps, the canonical record stored as TEXT with `digest = sha256(text)` and `scope = text::jsonb` held by CHECK constraints, and the record's shape and hard bounds -- batch size at most 20, limit at most 2000 -- held by `catalog_work_scope_record_valid`). `create_work_scope` and `revise_work_scope` derive the project and digest, re-check membership and the trusted `swarm_v2` workflow, and refuse a revision whose expected head revision or digest is not the current one (`WORK_SCOPE_STALE`). `catalog_canonical_manufacturer_coverage` is a bounded read of exact canonical variant counts per register marque plus the catalog total. Drafts only: no status but `draft`, no snapshot column, no relation to `runs`. Service-path only; additive and forward-only. Mirrors `backend/catalog/scope/` |

All migrations are forward-only and data-preserving. Most are additive; Console 6 deliberately removes only superseded RPC definitions so there is one run-creation authority. There are no destructive table/data down-migrations, by policy (`scripts/check_migrations.py` forbids `drop table` and data deletes).

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

### Scoped catalog PR1 (work scopes) — migration and rollback impact

`20260922000100_catalog_work_scopes.sql` is purely additive: two new relations,
eleven functions, four triggers (one of them a DEFERRED constraint trigger that
holds a plan's head to a revision that exists with that digest), four indexes
-- one of them `catalog_models_manufacturer_idx` on an existing relation -- and
explicit privileges. It alters no existing relation, redefines no existing
function and backfills nothing.

Privileges: both relations have RLS on with no policies; `PUBLIC`, `anon` and
`authenticated` have nothing. `service_role` gets `SELECT, INSERT` on both and
`UPDATE` on `catalog_work_scopes` only -- the head advances, a revision never
changes -- and the triggers bound what that one `UPDATE` may touch. Every
function is `EXECUTE` for `service_role` alone.

Rerun safety: every `create` is `if not exists` or `create or replace`, and
every trigger is dropped before it is created. The executable suite applies the
file a second time over a populated schema.

Rollback: nothing executes from a plan in this release -- there is no
preparation, queue or batch, and no relation to `runs` -- so turning
`MILO_ENABLE_WORK_SCOPE_MUTATIONS` off (the default) leaves the relations inert.
A forward corrective migration could drop them; no other relation, function or
row depends on them.

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
(read-only, operator-supplied connection) against the **complete** ordered
migration set in the table above — every 3-digit migration and every
14-digit timestamped one. The authoritative applied history is
`supabase_migrations.schema_migrations`.

- **empty-schema** — no public schema objects and no applied history; apply
  the whole ordered set above, in order;
- **legacy-baseline** — exactly the confirmed four-table baseline below with
  no applied history; apply the whole ordered set in order (the
  reconciliation clauses in 001/002 handle the existing rows);
- **partially-migrated** — the applied history is an exact ordered PREFIX of
  the set above and a tail remains. Every pending migration is reported by
  version AND filename; apply only that tail, in order;
- **fully-migrated** — every migration version in the table above is present
  in the remote applied history and no drift condition holds. Nothing to
  apply; rerunning is safe (idempotent);
- **drift / unrecognized (BLOCKED)** — the tool refuses to guess. A gap or
  non-prefix history, a remote version with no local migration file, a
  duplicate history row, a populated schema with absent or empty history, an
  uninspectable history past the supported baseline, or a history row whose
  migration's object is provably missing all fail closed.

An **incomplete inspection is never classified.** Every read the tool makes
is status-checked, and a query that fails is recorded as a blocking finding
rather than folded into an observation. "Could not read the migration
history" and "the migration history is empty" are different states: an empty
applied history over the four baseline tables legitimately classifies a
database as `legacy-baseline`, whose documented remedy is to apply the whole
ordered set, so a `SELECT` that merely failed must never be able to produce
that answer. The same holds for the object markers — a probe that failed is
not an observed absence.

Object markers are **secondary evidence only**. They can add a drift finding
— "history says this migration is applied, but the object it creates is not
there" — and can never establish that a database is fully migrated. A
database is fully migrated when the history says every local version is
applied, and never because a subset of objects happens to exist.

The classification itself lives in the pure helper
`scripts/release/migration_state.py` and is unit-tested in
`tests/test_migration_state_verification.py`, including a fixture
reproducing the real production history (applied through `20260823000100`,
eleven timestamped migrations pending) that must classify as
`partially-migrated` and must never classify as `fully-migrated`. The
underlying states (plus rerun/idempotency) are also executably tested
against real PostgreSQL in CI (`tests/test_migrations_postgres.py`,
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

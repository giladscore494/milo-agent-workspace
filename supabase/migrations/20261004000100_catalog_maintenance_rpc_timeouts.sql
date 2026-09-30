-- PR-OPS1 (P50): the maintenance RPCs whose cost scales with a whole register
-- snapshot run under their own statement timeout, not the API's 8 s.
--
-- Production: every service-role RPC arrives through PostgREST as the
-- `authenticator` role, whose settings are statement_timeout = 8s and
-- lock_timeout = 8s (service_role has none of its own; read-only check,
-- 2026-09-30). compact_register_snapshot's dry-run of the 6,379-row Toyota
-- snapshot failed there with SQLSTATE 57014; the 5,279-row Audi one passed.
-- The capture job's automatic compaction after every register capture calls
-- the same RPC, so every larger tozar stayed uncompacted and the capacity gate
-- priced its rows at the uncompacted 5,500 B.
--
-- How the setting reaches the statement
-- -------------------------------------
-- PostgreSQL arms a statement's timer when the statement STARTS; a function's
-- own `SET statement_timeout` clause changes the setting only after that, too
-- late for the statement already running. PostgREST (v12+, `db-hoisted-tx-
-- settings`, default "statement_timeout, plan_filter.statement_cost_limit,
-- default_transaction_isolation") reads the called function's settings and
-- applies the hoisted ones as transaction-scoped settings BEFORE the main
-- query ("Hoisted Function Settings", docs/references/transactions.rst of
-- PostgREST v14.5; production runs PostgREST 14.5 -- the application_name of
-- its connections -- on PostgreSQL 17.6). So the timeout below takes effect
-- only for the function PostgREST calls: it is set on each RPC ENTRY POINT,
-- never on a helper it calls.
--
-- lock_timeout is not hoisted. PostgreSQL applies a function's own
-- lock_timeout to every lock wait inside it, which is what the two functions
-- that take table locks on purpose need: compact_register_snapshot (its apply
-- already sets 5 s before its LOCK TABLE) and prune_register_snapshots take
-- SHARE ROW EXCLUSIVE on the register tables, and every reader of those tables
-- queues behind a waiting lock -- so they give up after 5 s instead of the
-- role's 8 s. The other four take only row locks and keep the role's 8 s.
--
-- The functions, and why each (measured on ephemeral PostgreSQL 16, every
-- migration applied, a Toyota-shaped snapshot: open plan + batch + terminal
-- run; production measured ~2.5x slower -- Toyota's 6,379-row dry-run passed
-- 8 s there, 3.2 s here at 6,500 rows):
--   * compact_register_snapshot -- reads, and on apply rewrites, every row
--     of the snapshot (typed-mismatch check per row): dry-run 3.2 s / 7.7 s,
--     apply 4.1 s / 9.3 s at 6,500 / 12,000 rows.
--   * record_register_snapshot_archive_from_database -- re-checks the whole
--     snapshot (catalog_register_snapshot_archivable) before recording.
--   * catalog_register_snapshot_archivable -- counts and ranges every row's
--     capture index.
--   * prune_register_snapshots -- deletes whole snapshots, their candidates
--     and variants, and re-points the ledger.
--   * prepare_work_scope_queue -- evaluates the coverage decision of every
--     candidate of each unit's snapshot (catalog_work_scope_coverage_decisions,
--     twice, through work_scope_preparation_summary): 5.0 s / 9.4 s.
--     catalog_work_scope_coverage_decisions itself is NOT given a setting:
--     it is never called by PostgREST (only from here), so a setting on it
--     could not reach the statement's timer, and any SET clause would stop
--     the planner from inlining it (20261002000200 relies on that).
--   * record_catalog_variants -- the batch that completes a build checks and
--     measures the whole snapshot: 1.9 s / 3.8 s for its last 500-row batch.
-- Not listed (bounded per call, measured at 12,000 rows): the archive line
-- check (one 500-line page, 0.08 s), the candidate trees (0.06 s), the
-- snapshot diff (0.15 s) and the byte measurement a unit status write takes
-- (0.13 s). A superseded snapshot's compaction is compact_register_snapshot.
--
-- 300 s is the ceiling of an unattended maintenance call, not its budget: the
-- client's own HTTP timeout still bounds how long a caller waits, and each
-- function is idempotent (a repeated call answers `unchanged` / refuses as
-- before). Nothing else changes: no body, grant or role setting.
--
-- A later `create or replace function` of any of these resets its settings;
-- tests/test_register_rpc_timeouts.py fails unless the newest definition of
-- each one still carries exactly these.
--
-- Forward-only and rerun-safe.

alter function public.compact_register_snapshot(text, boolean, text, uuid)
  set statement_timeout = '300s';
alter function public.compact_register_snapshot(text, boolean, text, uuid)
  set lock_timeout = '5s';

alter function public.prune_register_snapshots(text[], text)
  set statement_timeout = '300s';
alter function public.prune_register_snapshots(text[], text)
  set lock_timeout = '5s';

alter function public.record_register_snapshot_archive_from_database(uuid, text, bigint, text, integer)
  set statement_timeout = '300s';

alter function public.catalog_register_snapshot_archivable(uuid)
  set statement_timeout = '300s';

alter function public.prepare_work_scope_queue(uuid, text, integer, text, jsonb)
  set statement_timeout = '300s';

alter function public.record_catalog_variants(uuid, text, jsonb)
  set statement_timeout = '300s';

-- Read back: each function carries exactly its settings (search_path is the
-- one each already had), and the coverage decisions still carry none.
do $$
declare
  v_expected jsonb := jsonb_build_object(
    'public.compact_register_snapshot(text,boolean,text,uuid)',
      '["lock_timeout=5s", "search_path=pg_catalog", "statement_timeout=300s"]'::jsonb,
    'public.prune_register_snapshots(text[],text)',
      '["lock_timeout=5s", "search_path=pg_catalog", "statement_timeout=300s"]'::jsonb,
    'public.record_register_snapshot_archive_from_database(uuid,text,bigint,text,integer)',
      '["search_path=pg_catalog", "statement_timeout=300s"]'::jsonb,
    'public.catalog_register_snapshot_archivable(uuid)',
      '["search_path=pg_catalog", "statement_timeout=300s"]'::jsonb,
    'public.prepare_work_scope_queue(uuid,text,integer,text,jsonb)',
      '["search_path=pg_catalog", "statement_timeout=300s"]'::jsonb,
    'public.record_catalog_variants(uuid,text,jsonb)',
      '["search_path=pg_catalog", "statement_timeout=300s"]'::jsonb);
  v_fn text;
  v_actual jsonb;
begin
  for v_fn in select jsonb_object_keys(v_expected) loop
    select coalesce(jsonb_agg(c order by c), '[]'::jsonb) into v_actual
      from pg_proc p, unnest(coalesce(p.proconfig, '{}'::text[])) c
     where p.oid = v_fn::regprocedure;
    if v_actual <> v_expected->v_fn then
      raise exception 'PR-OPS1: % carries % (expected %)', v_fn, v_actual, v_expected->v_fn;
    end if;
  end loop;
  if (select proconfig from pg_proc
       where oid = 'public.catalog_work_scope_coverage_decisions(uuid,integer,integer,boolean)'::regprocedure)
     is not null then
    raise exception 'PR-OPS1: catalog_work_scope_coverage_decisions must stay inlinable (no SET clause)';
  end if;
end $$;

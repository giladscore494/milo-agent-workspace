-- Scoped catalog PR2: scoped Government preparation, a durable queue, and
-- bounded batches.
--
-- What this adds
-- --------------
--
-- A Mapping Plan (`catalog_work_scopes`, migration 20260922000100) says WHAT to
-- map. This migration is where one exact revision of it becomes WORK:
--
--   capture_scope declaration     a scoped Government snapshot says which
--                                 register marque it holds, and the database
--                                 holds the declaration to the query it
--                                 recorded (`catalog_capture_scope_consistent`)
--   catalog_work_scope_preparations
--                                 ONE per plan revision, immutable
--   catalog_work_scope_units      one per plan unit, in priority order: what
--                                 preparing that manufacturer found
--   catalog_work_scope_batches    bounded batches (1..20 candidates), each of
--                                 ONE unit and therefore ONE snapshot
--   catalog_work_scope_queue_items
--                                 the deterministic queue, position by position
--   catalog_work_scope_batch_runs which run executes which batch, append-only
--
--   prepare_work_scope_queue()    lease-guarded; an OPERATOR CAPTURE run only
--   bind_work_scope_batch_run()   the batch <-> run compare-and-set
--   work_scope_batch_for_run()    the worker's read of its exact batch
--
-- Where Government preparation runs, and where it cannot
-- ------------------------------------------------------
--
-- `prepare_work_scope_queue` asserts the caller's lease AND that the leased
-- run is an `operator_capture` run. The scoped captures that feed it happen in
-- the capture Cloud Run job (`backend/catalog/operator_capture.py`), which is
-- the only place Government transport exists. A paid Swarm V2 run can hold a
-- lease, but it can never prepare: preparation is outside the paid batch-run
-- clock by construction, not by convention.
--
-- Deterministic materialization
-- -----------------------------
--
-- For each unit, in the plan's priority order, the queue takes the unit
-- snapshot's candidates whose status is `candidate` and whose model years lie
-- inside the plan's range, in the catalog's one canonical order (the order
-- `catalog_candidate_variant_page` uses: codepoint text order, then years,
-- then the candidate key), until the plan's `max_items` is spent. Each unit's
-- items are cut into batches of the plan's `batch_size`; a batch never spans
-- two units, so a batch run pins exactly one snapshot. The same revision over
-- the same snapshots always materializes the same positions and batches, and a
-- revision is prepared at most once.
--
-- The normalization gate
-- ----------------------
--
-- A unit whose in-range candidates are MOSTLY `ambiguous` -- rows the reviewed
-- vocabulary could not read -- is recorded `vocabulary_insufficient` and queues
-- nothing. Batch runs over a manufacturer the catalog cannot read would spend
-- money on rows promotion refuses, so the gap is stated per unit instead.
-- Nothing about normalization is loosened here.
--
-- Additive and forward-only: one CHECK constraint and one index on
-- `catalog_source_snapshots` (satisfied by every existing row, which declares
-- no scope), five new relations and new functions. Rerun-safe.

-- ---------------------------------------------------------------------------
-- 1. The capture-scope declaration, held consistent by the database.
-- ---------------------------------------------------------------------------
--
-- Mirrors `backend/catalog/government/capture_scope.py`. A snapshot that
-- declares no scope is unconstrained here. One that does must declare ONE
-- register marque, and the declaration must be exactly the query the snapshot
-- recorded: `query` holds only `filters`, that text parses to the declared
-- filters, and `scope_key` is the SHA-256 of that very text.
create or replace function public.catalog_capture_scope_consistent(p_metadata jsonb)
returns boolean
language plpgsql
stable
set search_path = pg_catalog
as $$
declare
  v_scope jsonb;
  v_filters jsonb;
  v_query jsonb;
  v_text text;
  v_parsed jsonb;
  v_marque text;
begin
  if jsonb_typeof(p_metadata) is distinct from 'object' or not (p_metadata ? 'capture_scope') then
    return true;
  end if;
  v_scope := p_metadata->'capture_scope';
  if jsonb_typeof(v_scope) is distinct from 'object'
     or (select array_agg(k order by k collate "C") from jsonb_object_keys(v_scope) as k)
          is distinct from array['contract', 'filters', 'scope_key'] then
    return false;
  end if;
  if v_scope->>'contract' is distinct from 'gov.capture_scope.1' then
    return false;
  end if;
  v_filters := v_scope->'filters';
  if jsonb_typeof(v_filters) is distinct from 'object'
     or (select array_agg(k order by k collate "C") from jsonb_object_keys(v_filters) as k)
          is distinct from array['tozar']
     or jsonb_typeof(v_filters->'tozar') is distinct from 'string' then
    return false;
  end if;
  v_marque := v_filters->>'tozar';
  if char_length(v_marque) not between 1 and 120
     or v_marque ~ '(^[[:space:]]|[[:space:]]$)' or v_marque ~ '[[:cntrl:]]' then
    return false;
  end if;
  v_query := p_metadata->'query';
  if jsonb_typeof(v_query) is distinct from 'object'
     or (select array_agg(k order by k collate "C") from jsonb_object_keys(v_query) as k)
          is distinct from array['filters']
     or jsonb_typeof(v_query->'filters') is distinct from 'string' then
    return false;
  end if;
  v_text := v_query->>'filters';
  begin
    v_parsed := v_text::jsonb;
  exception when invalid_text_representation then
    return false;
  end;
  if v_parsed is distinct from v_filters then
    return false;
  end if;
  return v_scope->>'scope_key' = encode(sha256(convert_to(v_text, 'UTF8')), 'hex');
end;
$$;

alter table public.catalog_source_snapshots
  drop constraint if exists catalog_source_snapshots_capture_scope_consistent;
alter table public.catalog_source_snapshots
  add constraint catalog_source_snapshots_capture_scope_consistent
  check (public.catalog_capture_scope_consistent(retrieval_metadata));

-- The scoped listing filters on the declaration's key.
create index if not exists catalog_source_snapshots_capture_scope_idx
  on public.catalog_source_snapshots(
    source_family, resource_id, ((retrieval_metadata->'capture_scope'->>'scope_key')),
    activated_at desc);

-- ---------------------------------------------------------------------------
-- 2. The relations.
-- ---------------------------------------------------------------------------

create table if not exists public.catalog_work_scope_preparations (
  id uuid primary key default gen_random_uuid(),
  work_scope_id uuid not null references public.catalog_work_scopes(id) on delete restrict,
  revision integer not null,
  scope_digest text not null,
  -- The operator capture run whose lease wrote it. Provenance, not ownership.
  prepared_by_run_id uuid not null references public.runs(id) on delete restrict,
  unit_count integer not null,
  prepared_unit_count integer not null,
  queued_item_count integer not null,
  batch_count integer not null,
  created_at timestamptz not null default now(),
  constraint catalog_work_scope_preparations_revision_fk
    foreign key (work_scope_id, revision)
    references public.catalog_work_scope_revisions(work_scope_id, revision) on delete restrict,
  constraint catalog_work_scope_preparations_digest_shape
    check (scope_digest ~ '^[0-9a-f]{64}$'),
  constraint catalog_work_scope_preparations_counts
    check (unit_count between 1 and 64
           and prepared_unit_count between 0 and unit_count
           and queued_item_count between 0 and 2000
           and batch_count between 0 and queued_item_count
           and (queued_item_count = 0) = (batch_count = 0))
);
-- One preparation per revision: a revision's queue is decided once.
create unique index if not exists catalog_work_scope_preparations_revision_uidx
  on public.catalog_work_scope_preparations(work_scope_id, revision);

create table if not exists public.catalog_work_scope_units (
  id uuid primary key default gen_random_uuid(),
  preparation_id uuid not null
    references public.catalog_work_scope_preparations(id) on delete restrict,
  work_scope_id uuid not null references public.catalog_work_scopes(id) on delete restrict,
  revision integer not null,
  priority integer not null,
  unit_key text not null,
  -- The verified register spelling the unit was captured by, or NULL when the
  -- directory holds none for it (`register_unverified`).
  register_marque text,
  state text not null,
  reason_code text,
  snapshot_id uuid references public.catalog_source_snapshots(id) on delete restrict,
  snapshot_key text,
  capture_scope_key text,
  -- Counted by the database over the snapshot's candidates inside the plan's
  -- model years: readable (not `ambiguous`), `ambiguous`, and queueable
  -- (`candidate`). `queued_count` is how many of the queueable made the queue.
  readable_count integer not null default 0,
  ambiguous_count integer not null default 0,
  eligible_count integer not null default 0,
  queued_count integer not null default 0,
  created_at timestamptz not null default now(),
  constraint catalog_work_scope_units_state
    check (state in ('prepared', 'register_unverified', 'snapshot_unusable',
                     'vocabulary_insufficient')),
  constraint catalog_work_scope_units_key_shape check (unit_key ~ '^[a-z][a-z0-9_]{0,39}$'),
  constraint catalog_work_scope_units_priority check (priority between 1 and 64),
  constraint catalog_work_scope_units_marque_when_verified
    check ((state = 'register_unverified') = (register_marque is null)),
  constraint catalog_work_scope_units_marque_bounded
    check (register_marque is null or char_length(register_marque) between 1 and 120),
  constraint catalog_work_scope_units_snapshot_when_captured
    check ((state = 'register_unverified') = (snapshot_id is null)
           and (snapshot_id is null) = (snapshot_key is null)
           and (snapshot_id is null) = (capture_scope_key is null)),
  constraint catalog_work_scope_units_reason
    check ((state = 'prepared') = (reason_code is null)
           and (reason_code is null or reason_code ~ '^[A-Z][A-Z0-9_]{2,79}$')),
  constraint catalog_work_scope_units_counts
    check (readable_count >= 0 and ambiguous_count >= 0
           and eligible_count between 0 and readable_count
           and queued_count between 0 and eligible_count
           and (state = 'prepared' or queued_count = 0))
);
create unique index if not exists catalog_work_scope_units_priority_uidx
  on public.catalog_work_scope_units(preparation_id, priority);
create unique index if not exists catalog_work_scope_units_key_uidx
  on public.catalog_work_scope_units(preparation_id, unit_key);

create table if not exists public.catalog_work_scope_batches (
  id uuid primary key default gen_random_uuid(),
  preparation_id uuid not null
    references public.catalog_work_scope_preparations(id) on delete restrict,
  work_scope_id uuid not null references public.catalog_work_scopes(id) on delete restrict,
  revision integer not null,
  scope_digest text not null,
  batch_number integer not null,
  unit_id uuid not null references public.catalog_work_scope_units(id) on delete restrict,
  unit_key text not null,
  snapshot_id uuid not null references public.catalog_source_snapshots(id) on delete restrict,
  snapshot_key text not null,
  item_count integer not null,
  first_position integer not null,
  created_at timestamptz not null default now(),
  constraint catalog_work_scope_batches_number check (batch_number between 1 and 2000),
  -- The hard server maximum of one batch run, held here too.
  constraint catalog_work_scope_batches_items check (item_count between 1 and 20),
  constraint catalog_work_scope_batches_first check (first_position between 1 and 2000),
  constraint catalog_work_scope_batches_digest_shape check (scope_digest ~ '^[0-9a-f]{64}$')
);
create unique index if not exists catalog_work_scope_batches_number_uidx
  on public.catalog_work_scope_batches(preparation_id, batch_number);
create index if not exists catalog_work_scope_batches_scope_idx
  on public.catalog_work_scope_batches(work_scope_id, revision, batch_number);

create table if not exists public.catalog_work_scope_queue_items (
  id uuid primary key default gen_random_uuid(),
  preparation_id uuid not null
    references public.catalog_work_scope_preparations(id) on delete restrict,
  batch_id uuid not null references public.catalog_work_scope_batches(id) on delete restrict,
  position integer not null,
  batch_position integer not null,
  unit_key text not null,
  snapshot_id uuid not null references public.catalog_source_snapshots(id) on delete restrict,
  candidate_id uuid not null references public.catalog_candidate_variants(id) on delete restrict,
  candidate_key text not null,
  created_at timestamptz not null default now(),
  constraint catalog_work_scope_queue_items_position check (position between 1 and 2000),
  constraint catalog_work_scope_queue_items_batch_position check (batch_position between 1 and 20)
);
create unique index if not exists catalog_work_scope_queue_items_position_uidx
  on public.catalog_work_scope_queue_items(preparation_id, position);
-- A candidate is queued at most once per preparation.
create unique index if not exists catalog_work_scope_queue_items_candidate_uidx
  on public.catalog_work_scope_queue_items(preparation_id, candidate_id);
create unique index if not exists catalog_work_scope_queue_items_batch_uidx
  on public.catalog_work_scope_queue_items(batch_id, batch_position);

create table if not exists public.catalog_work_scope_batch_runs (
  id uuid primary key default gen_random_uuid(),
  batch_id uuid not null references public.catalog_work_scope_batches(id) on delete restrict,
  work_scope_id uuid not null references public.catalog_work_scopes(id) on delete restrict,
  run_id uuid not null references public.runs(id) on delete restrict,
  attempt integer not null,
  bound_by uuid not null,
  bound_at timestamptz not null default now(),
  constraint catalog_work_scope_batch_runs_attempt check (attempt between 1 and 100)
);
-- A run executes at most ONE batch, ever.
create unique index if not exists catalog_work_scope_batch_runs_run_uidx
  on public.catalog_work_scope_batch_runs(run_id);
create unique index if not exists catalog_work_scope_batch_runs_attempt_uidx
  on public.catalog_work_scope_batch_runs(batch_id, attempt);
create index if not exists catalog_work_scope_batch_runs_scope_idx
  on public.catalog_work_scope_batch_runs(work_scope_id, bound_at desc);

-- Every foreign key has a leading index, like every other catalog relation
-- (`test_every_catalog_foreign_key_column_is_indexed`).
create index if not exists catalog_work_scope_preparations_run_idx
  on public.catalog_work_scope_preparations(prepared_by_run_id);
create index if not exists catalog_work_scope_units_scope_idx
  on public.catalog_work_scope_units(work_scope_id, revision);
create index if not exists catalog_work_scope_units_snapshot_idx
  on public.catalog_work_scope_units(snapshot_id);
create index if not exists catalog_work_scope_batches_unit_idx
  on public.catalog_work_scope_batches(unit_id);
create index if not exists catalog_work_scope_batches_snapshot_idx
  on public.catalog_work_scope_batches(snapshot_id);
create index if not exists catalog_work_scope_queue_items_candidate_idx
  on public.catalog_work_scope_queue_items(candidate_id);
create index if not exists catalog_work_scope_queue_items_snapshot_idx
  on public.catalog_work_scope_queue_items(snapshot_id);

-- ---------------------------------------------------------------------------
-- 3. Immutability, held by the database.
-- ---------------------------------------------------------------------------
--
-- A preparation, its units, its batches, its queue and every binding are
-- records of decisions already made. None is ever rewritten or removed, by any
-- role. Progress is DERIVED from the bound runs' own durable state, so no row
-- here ever needs to change for a batch to move forward.
create or replace function public.forbid_work_scope_preparation_mutation() returns trigger
language plpgsql
set search_path = pg_catalog
as $$
begin
  raise exception 'WORK_SCOPE_PREPARATION_IMMUTABLE' using errcode = '55000';
end;
$$;

do $$
declare relation text;
begin
  foreach relation in array array[
    'catalog_work_scope_preparations', 'catalog_work_scope_units',
    'catalog_work_scope_batches', 'catalog_work_scope_queue_items',
    'catalog_work_scope_batch_runs'
  ] loop
    execute format('drop trigger if exists %I on public.%I', relation || '_append_only', relation);
    execute format('create trigger %I before update or delete on public.%I '
                   'for each row execute function public.forbid_work_scope_preparation_mutation()',
                   relation || '_append_only', relation);
  end loop;
end $$;

-- ---------------------------------------------------------------------------
-- 4. Preparation: one exact revision becomes a durable queue.
-- ---------------------------------------------------------------------------
--
-- `p_preparation` is what the operator capture decided per unit, and nothing
-- the database can decide itself:
--
--   {"work_scope_id": uuid, "revision": n, "scope_digest": hex,
--    "units": [{"unit_key", "priority", "state", "register_marque",
--               "snapshot_id", "reason_code"}, ...]}
--
-- `state` is `captured` (a usable scoped snapshot was landed), `snapshot_unusable`
-- (it landed and cannot be read; `reason_code` says why) or
-- `register_unverified` (the directory holds no verified spelling, so nothing
-- was captured). The database re-derives everything else: the plan's units,
-- years, limit and batch size come from the stored revision, every count comes
-- from the snapshot's own rows, and `captured` becomes `prepared` or
-- `vocabulary_insufficient` here.
create or replace function public.prepare_work_scope_queue(
  p_run_id uuid, p_worker_id text, p_attempt integer, p_lease_token text,
  p_preparation jsonb
) returns jsonb
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_scope_id uuid;
  v_revision integer;
  v_digest text;
  v_plan public.catalog_work_scopes;
  v_rev public.catalog_work_scope_revisions;
  v_workflow text;
  v_existing public.catalog_work_scope_preparations;
  v_units jsonb;
  v_unit jsonb;
  v_plan_units jsonb;
  v_count integer;
  v_from integer;
  v_to integer;
  v_max integer;
  v_size integer;
  v_i integer;
  v_state text;
  v_marque text;
  v_snapshot public.catalog_source_snapshots;
  v_reason text;
  v_readable integer;
  v_ambiguous integer;
  v_eligible integer;
  v_take integer;
  v_budget integer;
  v_decided jsonb := '[]'::jsonb;
  v_prepared integer := 0;
  v_queued integer := 0;
  v_batches integer := 0;
  v_preparation public.catalog_work_scope_preparations;
  v_unit_row public.catalog_work_scope_units;
  v_ids uuid[];
  v_keys text[];
  v_position integer := 0;
  v_batch_number integer := 0;
  v_batch_id uuid;
  v_in_batch integer;
  v_b integer;
  v_j integer;
  v_submitted jsonb;
  v_stored jsonb;
begin
  perform public.assert_worker_lease(p_run_id, p_worker_id, p_attempt, p_lease_token);
  -- Only an operator capture run prepares. A paid product run holds a lease
  -- too, and must never be able to reach Government preparation.
  if not exists (select 1 from public.runs r where r.id = p_run_id
                  and r.run_identity->>'workflow_key' = 'operator_capture') then
    raise exception 'WORK_SCOPE_PREPARATION_RUN_INVALID' using errcode = '42501';
  end if;
  if p_preparation is null or jsonb_typeof(p_preparation) <> 'object'
     or (select array_agg(k order by k collate "C") from jsonb_object_keys(p_preparation) as k)
          is distinct from array['revision', 'scope_digest', 'units', 'work_scope_id']
     or jsonb_typeof(p_preparation->'revision') <> 'number'
     or p_preparation->>'revision' !~ '^[0-9]{1,9}$'
     or jsonb_typeof(p_preparation->'scope_digest') <> 'string'
     or p_preparation->>'scope_digest' !~ '^[0-9a-f]{64}$'
     or jsonb_typeof(p_preparation->'units') <> 'array'
     or jsonb_typeof(p_preparation->'work_scope_id') <> 'string' then
    raise exception 'WORK_SCOPE_PREPARATION_INVALID' using errcode = '22023';
  end if;
  begin
    v_scope_id := (p_preparation->>'work_scope_id')::uuid;
  exception when invalid_text_representation then
    raise exception 'WORK_SCOPE_PREPARATION_INVALID' using errcode = '22023';
  end;
  v_revision := (p_preparation->>'revision')::integer;
  v_digest := p_preparation->>'scope_digest';
  v_units := p_preparation->'units';

  -- The plan row lock serializes preparation with revisions and bindings.
  select * into v_plan from public.catalog_work_scopes where id = v_scope_id for update;
  if v_plan.id is null then
    raise exception 'WORK_SCOPE_NOT_FOUND' using errcode = 'P0002';
  end if;
  select p.workflow_key into v_workflow from public.projects p where p.id = v_plan.project_id;
  if v_workflow is distinct from 'swarm_v2' then
    raise exception 'WORK_SCOPE_WORKFLOW_UNSUPPORTED' using errcode = '22023';
  end if;
  if v_plan.closed_at is not null then
    raise exception 'WORK_SCOPE_NOT_EDITABLE' using errcode = '55000';
  end if;
  -- A stale digest fails closed: only the head revision is ever prepared.
  if v_plan.head_revision is distinct from v_revision
     or v_plan.head_digest is distinct from v_digest then
    raise exception 'WORK_SCOPE_STALE' using errcode = '40001';
  end if;
  select * into v_rev from public.catalog_work_scope_revisions
   where work_scope_id = v_scope_id and revision = v_revision;
  if v_rev.id is null or v_rev.digest is distinct from v_digest then
    raise exception 'WORK_SCOPE_STALE' using errcode = '40001';
  end if;
  v_plan_units := v_rev.scope->'units';
  v_count := jsonb_array_length(v_plan_units);
  v_from := nullif(v_rev.scope->'model_years'->>'from', '')::integer;
  v_to := nullif(v_rev.scope->'model_years'->>'to', '')::integer;
  v_max := (v_rev.scope->>'max_items')::integer;
  v_size := (v_rev.scope->>'batch_size')::integer;

  -- The submission must name exactly the revision's units, in its order.
  if jsonb_array_length(v_units) <> v_count then
    raise exception 'WORK_SCOPE_PREPARATION_INVALID' using errcode = '22023';
  end if;

  -- A revision is prepared once. The same submission again is a replay and
  -- answers with what was stored; a different one is refused.
  select * into v_existing from public.catalog_work_scope_preparations
   where work_scope_id = v_scope_id and revision = v_revision;
  if v_existing.id is not null then
    select coalesce(jsonb_agg(jsonb_build_object(
             'unit_key', u.unit_key, 'snapshot_id', u.snapshot_id::text,
             'register_marque', u.register_marque,
             'captured', u.state in ('prepared', 'vocabulary_insufficient'))
             order by u.priority), '[]'::jsonb)
      into v_stored from public.catalog_work_scope_units u
     where u.preparation_id = v_existing.id;
    select coalesce(jsonb_agg(jsonb_build_object(
             'unit_key', e->>'unit_key', 'snapshot_id', e->>'snapshot_id',
             'register_marque', e->>'register_marque',
             'captured', e->>'state' = 'captured')
             order by (e->>'priority')::integer), '[]'::jsonb)
      into v_submitted from jsonb_array_elements(v_units) as e;
    if v_stored is distinct from v_submitted then
      raise exception 'WORK_SCOPE_ALREADY_PREPARED' using errcode = '23505';
    end if;
    return public.work_scope_preparation_summary(v_existing.id, true);
  end if;

  -- Pass 1: decide every unit, and count, before writing anything.
  v_budget := v_max;
  for v_i in 0 .. v_count - 1 loop
    v_unit := v_units->v_i;
    if jsonb_typeof(v_unit) <> 'object'
       or (select array_agg(k order by k collate "C") from jsonb_object_keys(v_unit) as k)
            is distinct from array['priority', 'reason_code', 'register_marque',
                                   'snapshot_id', 'state', 'unit_key']
       or v_unit->>'unit_key' is distinct from v_plan_units->>v_i
       or jsonb_typeof(v_unit->'priority') <> 'number'
       or v_unit->>'priority' is distinct from (v_i + 1)::text then
      raise exception 'WORK_SCOPE_PREPARATION_INVALID' using errcode = '22023';
    end if;
    v_state := v_unit->>'state';
    v_marque := case when jsonb_typeof(v_unit->'register_marque') = 'string'
                     then v_unit->>'register_marque' end;
    v_readable := 0; v_ambiguous := 0; v_eligible := 0; v_take := 0; v_reason := null;
    v_snapshot := null;
    if v_state = 'register_unverified' then
      if jsonb_typeof(v_unit->'register_marque') <> 'null'
         or jsonb_typeof(v_unit->'snapshot_id') <> 'null'
         or jsonb_typeof(v_unit->'reason_code') <> 'null' then
        raise exception 'WORK_SCOPE_PREPARATION_INVALID' using errcode = '22023';
      end if;
      v_reason := 'WORK_SCOPE_REGISTER_UNVERIFIED';
    elsif v_state in ('captured', 'snapshot_unusable') then
      if v_marque is null or char_length(v_marque) not between 1 and 120
         or jsonb_typeof(v_unit->'snapshot_id') <> 'string' then
        raise exception 'WORK_SCOPE_PREPARATION_INVALID' using errcode = '22023';
      end if;
      begin
        select * into v_snapshot from public.catalog_source_snapshots
         where id = (v_unit->>'snapshot_id')::uuid;
      exception when invalid_text_representation then
        raise exception 'WORK_SCOPE_PREPARATION_INVALID' using errcode = '22023';
      end;
      -- The snapshot must be an ACTIVE Government WLTP capture that declares
      -- exactly this unit's register marque.
      if v_snapshot.id is null or v_snapshot.source_family <> 'government'
         or v_snapshot.resource_id <> '142afde2-6228-49f9-8a29-9b6c3a0cbe40'
         or v_snapshot.activated_at is null
         or v_snapshot.retrieval_metadata->'capture_scope'->'filters'->>'tozar'
              is distinct from v_marque then
        raise exception 'WORK_SCOPE_UNIT_SNAPSHOT_INVALID' using errcode = '22023';
      end if;
      -- Two units never share one snapshot: a candidate is queued once.
      if v_decided @> jsonb_build_array(jsonb_build_object('snapshot_id', v_snapshot.id)) then
        raise exception 'WORK_SCOPE_PREPARATION_INVALID' using errcode = '22023';
      end if;
      if v_state = 'snapshot_unusable' then
        if jsonb_typeof(v_unit->'reason_code') <> 'string'
           or v_unit->>'reason_code' not in (
             'GOV_PROJECTION_SNAPSHOT_INCOMPLETE', 'GOV_PROJECTION_SNAPSHOT_NOT_READ',
             'GOV_PROJECTION_RESOURCE_NOT_NORMALIZED', 'GOV_PROJECTION_SNAPSHOT_STATE_INVALID') then
          raise exception 'WORK_SCOPE_PREPARATION_INVALID' using errcode = '22023';
        end if;
        v_reason := v_unit->>'reason_code';
      else
        if jsonb_typeof(v_unit->'reason_code') <> 'null' then
          raise exception 'WORK_SCOPE_PREPARATION_INVALID' using errcode = '22023';
        end if;
        -- The catalog's own readability gate, applied by the database.
        begin
          perform public.catalog_readable_snapshot(v_snapshot.id, false);
        exception when others then
          raise exception 'WORK_SCOPE_UNIT_SNAPSHOT_INVALID' using errcode = '22023';
        end;
        select count(*) filter (where c.status <> 'ambiguous'),
               count(*) filter (where c.status = 'ambiguous'),
               count(*) filter (where c.status = 'candidate')
          into v_readable, v_ambiguous, v_eligible
          from public.catalog_candidate_variants c
         where c.snapshot_id = v_snapshot.id
           and (v_from is null or c.model_year_start >= v_from)
           and (v_to is null or c.model_year_end <= v_to);
        if v_ambiguous > v_readable then
          v_state := 'vocabulary_insufficient';
          v_reason := 'WORK_SCOPE_VOCABULARY_INSUFFICIENT';
        else
          v_state := 'prepared';
          v_take := least(v_eligible, v_budget);
          v_budget := v_budget - v_take;
          v_prepared := v_prepared + 1;
          v_queued := v_queued + v_take;
          v_batches := v_batches + (v_take + v_size - 1) / v_size;
        end if;
      end if;
    else
      raise exception 'WORK_SCOPE_PREPARATION_INVALID' using errcode = '22023';
    end if;
    v_decided := v_decided || jsonb_build_object(
      'priority', v_i + 1, 'unit_key', v_unit->>'unit_key', 'register_marque', v_marque,
      'state', v_state, 'reason_code', v_reason,
      'snapshot_id', v_snapshot.id, 'snapshot_key', v_snapshot.snapshot_key,
      'capture_scope_key', v_snapshot.retrieval_metadata->'capture_scope'->>'scope_key',
      'readable', v_readable, 'ambiguous', v_ambiguous, 'eligible', v_eligible,
      'take', v_take);
  end loop;

  -- Pass 2: write the decision, all of it or none of it.
  insert into public.catalog_work_scope_preparations
    (work_scope_id, revision, scope_digest, prepared_by_run_id, unit_count,
     prepared_unit_count, queued_item_count, batch_count)
  values (v_scope_id, v_revision, v_digest, p_run_id, v_count, v_prepared, v_queued, v_batches)
  returning * into v_preparation;

  for v_i in 0 .. v_count - 1 loop
    v_unit := v_decided->v_i;
    insert into public.catalog_work_scope_units
      (preparation_id, work_scope_id, revision, priority, unit_key, register_marque, state,
       reason_code, snapshot_id, snapshot_key, capture_scope_key, readable_count,
       ambiguous_count, eligible_count, queued_count)
    values (v_preparation.id, v_scope_id, v_revision, (v_unit->>'priority')::integer,
            v_unit->>'unit_key', v_unit->>'register_marque', v_unit->>'state',
            v_unit->>'reason_code', (v_unit->>'snapshot_id')::uuid, v_unit->>'snapshot_key',
            v_unit->>'capture_scope_key', (v_unit->>'readable')::integer,
            (v_unit->>'ambiguous')::integer, (v_unit->>'eligible')::integer,
            (v_unit->>'take')::integer)
    returning * into v_unit_row;
    v_take := (v_unit->>'take')::integer;
    if v_take = 0 then
      continue;
    end if;
    -- The canonical candidate order, the one every catalog read uses.
    select array_agg(q.id order by q.ord), array_agg(q.candidate_key order by q.ord)
      into v_ids, v_keys
      from (select c.id, c.candidate_key,
                   row_number() over (order by c.manufacturer collate "C",
                                      c.commercial_model collate "C",
                                      c.model_year_start, c.model_year_end,
                                      coalesce(c.official_model_code, '') collate "C",
                                      coalesce(c.trim, '') collate "C",
                                      c.candidate_key collate "C") as ord
              from public.catalog_candidate_variants c
             where c.snapshot_id = v_unit_row.snapshot_id and c.status = 'candidate'
               and (v_from is null or c.model_year_start >= v_from)
               and (v_to is null or c.model_year_end <= v_to)) as q
     where q.ord <= v_take;
    if coalesce(array_length(v_ids, 1), 0) <> v_take then
      raise exception 'WORK_SCOPE_PREPARATION_INVALID' using errcode = '22023';
    end if;
    v_b := 0;
    while v_b * v_size < v_take loop
      v_in_batch := least(v_size, v_take - v_b * v_size);
      v_batch_number := v_batch_number + 1;
      insert into public.catalog_work_scope_batches
        (preparation_id, work_scope_id, revision, scope_digest, batch_number, unit_id,
         unit_key, snapshot_id, snapshot_key, item_count, first_position)
      values (v_preparation.id, v_scope_id, v_revision, v_digest, v_batch_number,
              v_unit_row.id, v_unit_row.unit_key, v_unit_row.snapshot_id,
              v_unit_row.snapshot_key, v_in_batch, v_position + 1)
      returning id into v_batch_id;
      for v_j in 1 .. v_in_batch loop
        v_position := v_position + 1;
        insert into public.catalog_work_scope_queue_items
          (preparation_id, batch_id, position, batch_position, unit_key, snapshot_id,
           candidate_id, candidate_key)
        values (v_preparation.id, v_batch_id, v_position, v_j, v_unit_row.unit_key,
                v_unit_row.snapshot_id, v_ids[v_b * v_size + v_j], v_keys[v_b * v_size + v_j]);
      end loop;
      v_b := v_b + 1;
    end loop;
  end loop;
  return public.work_scope_preparation_summary(v_preparation.id, false);
end;
$$;

-- The bounded summary a preparation answers with: its counts, its units, and
-- its batches (never its queue rows, which the worker reads by batch).
create or replace function public.work_scope_preparation_summary(
  p_preparation_id uuid, p_replayed boolean
) returns jsonb
language sql
stable
set search_path = pg_catalog
as $$
  select jsonb_build_object(
    'replayed', p_replayed,
    'preparation', to_jsonb(p),
    'units', coalesce((select jsonb_agg(to_jsonb(u) order by u.priority)
                         from public.catalog_work_scope_units u
                        where u.preparation_id = p.id), '[]'::jsonb),
    'batches', coalesce((select jsonb_agg(jsonb_build_object(
                           'id', b.id, 'batch_number', b.batch_number,
                           'unit_key', b.unit_key, 'snapshot_key', b.snapshot_key,
                           'item_count', b.item_count, 'first_position', b.first_position)
                           order by b.batch_number)
                         from public.catalog_work_scope_batches b
                        where b.preparation_id = p.id), '[]'::jsonb))
    from public.catalog_work_scope_preparations p
   where p.id = p_preparation_id;
$$;

-- ---------------------------------------------------------------------------
-- 5. Binding a batch to the run that executes it.
-- ---------------------------------------------------------------------------
--
-- The compare-and-set every batch run passes through (scoped catalog PR3 is
-- its caller). Under the plan's row lock it refuses:
--
--   * a batch of a revision that is not the plan's head (WORK_SCOPE_STALE) --
--     a stale revision never launches;
--   * a closed plan, a project that is no longer Swarm V2, a binder who is not
--     a member, a run of another conversation or another engine, or a run that
--     is already terminal;
--   * a second LIVE batch run anywhere in the plan
--     (WORK_SCOPE_BATCH_IN_PROGRESS): one batch at a time, and nothing starts
--     the next one automatically;
--   * a batch whose run already COMPLETED (WORK_SCOPE_BATCH_ALREADY_COMPLETED);
--   * a run already bound to a different batch (WORK_SCOPE_BATCH_RUN_TAKEN).
--
-- Binding the same run to the same batch again returns the existing binding,
-- so a retried request is idempotent rather than a duplicate batch run.
create or replace function public.bind_work_scope_batch_run(
  p_batch_id uuid, p_run_id uuid, p_expected_revision integer, p_expected_digest text,
  p_bound_by uuid
) returns jsonb
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_batch public.catalog_work_scope_batches;
  v_plan public.catalog_work_scopes;
  v_workflow text;
  v_run record;
  v_existing public.catalog_work_scope_batch_runs;
  v_attempt integer;
  v_binding public.catalog_work_scope_batch_runs;
begin
  if p_batch_id is null or p_run_id is null or p_bound_by is null then
    raise exception 'WORK_SCOPE_BATCH_RUN_INVALID' using errcode = '22023';
  end if;
  select * into v_batch from public.catalog_work_scope_batches where id = p_batch_id;
  if v_batch.id is null then
    raise exception 'WORK_SCOPE_BATCH_NOT_FOUND' using errcode = 'P0002';
  end if;
  select * into v_plan from public.catalog_work_scopes where id = v_batch.work_scope_id for update;
  -- Absent and not-a-member are ONE answer.
  if not exists (select 1 from public.project_members m
                  where m.project_id = v_plan.project_id and m.user_id = p_bound_by) then
    raise exception 'WORK_SCOPE_BATCH_NOT_FOUND' using errcode = 'P0002';
  end if;
  select p.workflow_key into v_workflow from public.projects p where p.id = v_plan.project_id;
  if v_workflow is distinct from 'swarm_v2' then
    raise exception 'WORK_SCOPE_WORKFLOW_UNSUPPORTED' using errcode = '22023';
  end if;
  if v_plan.closed_at is not null then
    raise exception 'WORK_SCOPE_NOT_EDITABLE' using errcode = '55000';
  end if;
  if v_plan.head_revision is distinct from v_batch.revision
     or v_plan.head_digest is distinct from v_batch.scope_digest
     or p_expected_revision is distinct from v_batch.revision
     or p_expected_digest is distinct from v_batch.scope_digest then
    raise exception 'WORK_SCOPE_STALE' using errcode = '40001';
  end if;
  select r.id, r.conversation_id, r.status, r.run_identity->>'workflow_key' as workflow_key
    into v_run from public.runs r where r.id = p_run_id;
  if v_run.id is null or v_run.conversation_id is distinct from v_plan.conversation_id
     or v_run.workflow_key is distinct from 'swarm_v2' then
    raise exception 'WORK_SCOPE_BATCH_RUN_INVALID' using errcode = '22023';
  end if;
  select * into v_existing from public.catalog_work_scope_batch_runs where run_id = p_run_id;
  if v_existing.id is not null then
    if v_existing.batch_id = p_batch_id then
      return to_jsonb(v_existing) || jsonb_build_object('replayed', true);
    end if;
    raise exception 'WORK_SCOPE_BATCH_RUN_TAKEN' using errcode = '23505';
  end if;
  if v_run.status in ('completed', 'partial_success', 'failed', 'cancelled', 'timed_out',
                      'budget_exhausted') then
    raise exception 'WORK_SCOPE_BATCH_RUN_INVALID' using errcode = '22023';
  end if;
  if exists (select 1 from public.catalog_work_scope_batch_runs br
               join public.runs r on r.id = br.run_id
              where br.work_scope_id = v_plan.id
                and r.status not in ('completed', 'partial_success', 'failed', 'cancelled',
                                     'timed_out', 'budget_exhausted')) then
    raise exception 'WORK_SCOPE_BATCH_IN_PROGRESS' using errcode = '55000';
  end if;
  if exists (select 1 from public.catalog_work_scope_batch_runs br
               join public.runs r on r.id = br.run_id
              where br.batch_id = p_batch_id and r.status = 'completed') then
    raise exception 'WORK_SCOPE_BATCH_ALREADY_COMPLETED' using errcode = '55000';
  end if;
  select coalesce(max(attempt), 0) + 1 into v_attempt
    from public.catalog_work_scope_batch_runs where batch_id = p_batch_id;
  insert into public.catalog_work_scope_batch_runs
    (batch_id, work_scope_id, run_id, attempt, bound_by)
  values (p_batch_id, v_plan.id, p_run_id, v_attempt, p_bound_by)
  returning * into v_binding;
  return to_jsonb(v_binding) || jsonb_build_object('replayed', false);
end;
$$;

-- ---------------------------------------------------------------------------
-- 6. The worker's read of its exact batch.
-- ---------------------------------------------------------------------------
--
-- NULL when the run is bound to no batch. Otherwise the binding, the batch and
-- its items in batch order, each with the candidate's own identity text -- the
-- ONLY work a batch run is handed, so a resumed attempt re-reads the same rows.
create or replace function public.work_scope_batch_for_run(p_run_id uuid)
returns jsonb
language sql
stable
set search_path = pg_catalog
as $$
  select jsonb_build_object(
    'binding', to_jsonb(br),
    'batch', to_jsonb(b),
    'items', coalesce((select jsonb_agg(jsonb_build_object(
                         'position', i.position, 'batch_position', i.batch_position,
                         'candidate_id', i.candidate_id, 'candidate_key', i.candidate_key,
                         'manufacturer', c.manufacturer, 'commercial_model', c.commercial_model,
                         'model_year_start', c.model_year_start,
                         'model_year_end', c.model_year_end,
                         'official_model_code', c.official_model_code, 'trim', c.trim,
                         'status', c.status, 'snapshot_id', c.snapshot_id)
                         order by i.batch_position)
                       from public.catalog_work_scope_queue_items i
                       join public.catalog_candidate_variants c on c.id = i.candidate_id
                      where i.batch_id = b.id), '[]'::jsonb))
    from public.catalog_work_scope_batch_runs br
    join public.catalog_work_scope_batches b on b.id = br.batch_id
   where br.run_id = p_run_id;
$$;

-- ---------------------------------------------------------------------------
-- 7. RLS and privileges: service-path only.
-- ---------------------------------------------------------------------------
alter table public.catalog_work_scope_preparations enable row level security;
alter table public.catalog_work_scope_units enable row level security;
alter table public.catalog_work_scope_batches enable row level security;
alter table public.catalog_work_scope_queue_items enable row level security;
alter table public.catalog_work_scope_batch_runs enable row level security;

do $$
declare fn text; relation text;
begin
  foreach fn in array array[
    'public.catalog_capture_scope_consistent(jsonb)',
    'public.forbid_work_scope_preparation_mutation()',
    'public.prepare_work_scope_queue(uuid,text,integer,text,jsonb)',
    'public.work_scope_preparation_summary(uuid,boolean)',
    'public.bind_work_scope_batch_run(uuid,uuid,integer,text,uuid)',
    'public.work_scope_batch_for_run(uuid)'
  ] loop
    execute format('revoke execute on function %s from public', fn);
    if exists (select 1 from pg_roles where rolname='anon') then
      execute format('revoke execute on function %s from anon', fn);
    end if;
    if exists (select 1 from pg_roles where rolname='authenticated') then
      execute format('revoke execute on function %s from authenticated', fn);
    end if;
    if exists (select 1 from pg_roles where rolname='service_role') then
      execute format('grant execute on function %s to service_role', fn);
    end if;
  end loop;

  foreach relation in array array[
    'public.catalog_work_scope_preparations', 'public.catalog_work_scope_units',
    'public.catalog_work_scope_batches', 'public.catalog_work_scope_queue_items',
    'public.catalog_work_scope_batch_runs'
  ] loop
    execute format('revoke all on table %s from public', relation);
    if exists (select 1 from pg_roles where rolname='anon') then
      execute format('revoke all on table %s from anon', relation);
    end if;
    if exists (select 1 from pg_roles where rolname='authenticated') then
      execute format('revoke all on table %s from authenticated', relation);
    end if;
    if exists (select 1 from pg_roles where rolname='service_role') then
      execute format('grant select, insert on table %s to service_role', relation);
      execute format('revoke update, delete on table %s from service_role', relation);
    end if;
  end loop;
end $$;

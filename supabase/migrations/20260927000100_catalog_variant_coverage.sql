-- PR-Z: the variant coverage ledger and pre-run filtering (plan decision 19,
-- section 3.4, stage C3).
--
-- Why
-- ---
--
-- A Mapping Plan revision is prepared ONCE into a queue, and nothing stopped a
-- later revision -- or a second plan -- from queueing again the variants an
-- earlier paid run already enriched, or already found unresolvable. This
-- migration makes "never pay twice for the same variant at the same level" a
-- property the database holds:
--
--   catalog_variant_coverage      ONE row per (variant identity key, level):
--                                 what the latest finished run established
--   catalog_work_scope_unit_coverage
--                                 per prepared unit of a preparation: what the
--                                 ledger left out of its queue (counts AND
--                                 register ids), immutable like the preparation
--
--   catalog_variant_identity_key()   the variant's identity, hashed: the
--                                    register's marque, commercial model,
--                                    model years, official model code, trim
--                                    and every identity dimension, exactly as
--                                    the reviewed normalization stored them.
--                                    NOT `_id`: the register reuses ids across
--                                    captures. A duplicate-identity group
--                                    shares one key.
--   catalog_variant_content_sha256() the stored register row, minus `_id`
--   catalog_variant_coverage_decision()
--                                    the ONE filtering rule
--   prepare_work_scope_queue()       restated: filters BEFORE the plan's
--                                    limit is spent (point a, the queue build)
--   catalog_variant_coverage_for_batch()
--                                    the worker's read at run preparation
--                                    (point b)
--   record_catalog_variant_coverage_guarded()
--                                    the finalize path's idempotent write,
--                                    accepted only from the lease identity that
--                                    finalized the run `completed` or
--                                    `partial_success`
--   rebuild_catalog_variant_coverage() / catalog_variant_coverage_runs()
--                                    the operator backfill
--                                    (scripts/catalog/backfill_variant_coverage.py)
--   work_scope_unit_coverage()       the Mapping Plan's per-unit counts
--
-- Mirrors `backend/catalog/coverage.py` (the key, the rule, the ranks) and
-- `backend/catalog/government/vocabulary.VOCABULARY_VERSION`.
--
-- What it never does
-- ------------------
--
-- It changes no source value and no existing row: the ledger is an INDEX over
-- run history, rebuildable at any time, and a run's evidence, verdicts and
-- output stay exactly as they were written. Additive and forward-only: two new
-- relations, new functions, and three restated ones -- the plan record check
-- (which additionally admits the optional `"include_unresolved": true`, so
-- every stored revision still satisfies it), the queue build and its summary
-- (same signatures, same answers while the ledger is empty). Rerun-safe.

-- ---------------------------------------------------------------------------
-- 1. The rule's pure parts.
-- ---------------------------------------------------------------------------

-- The reviewed vocabulary's version (`vocabulary.VOCABULARY_VERSION`). A row a
-- run left unresolved is queued again once this changes.
create or replace function public.catalog_vocabulary_version()
returns text
language sql
immutable
set search_path = pg_catalog
as $$
  select 'gov.wltp.vocabulary.2'::text;
$$;

-- One identity column, length-prefixed so no value can imitate a separator;
-- `-` when absent.
create or replace function public.catalog_variant_identity_token(p_value text)
returns text
language sql
immutable
set search_path = pg_catalog
as $$
  select case when p_value is null then '-' else char_length(p_value)::text || ':' || p_value end;
$$;

-- `coverage.variant_identity_text`, byte for byte.
create or replace function public.catalog_variant_identity_text(
  p_manufacturer text, p_commercial_model text, p_model_year_start integer,
  p_model_year_end integer, p_official_model_code text, p_trim text, p_dimensions jsonb
) returns text
language sql
immutable
set search_path = pg_catalog
as $$
  select 'milo-variant-identity/1'
         || '|' || public.catalog_variant_identity_token(p_manufacturer)
         || '|' || public.catalog_variant_identity_token(p_commercial_model)
         || '|' || public.catalog_variant_identity_token(p_model_year_start::text)
         || '|' || public.catalog_variant_identity_token(p_model_year_end::text)
         || '|' || public.catalog_variant_identity_token(p_official_model_code)
         || '|' || public.catalog_variant_identity_token(p_trim)
         || coalesce((select string_agg('|' || public.catalog_variant_identity_token(d.key)
                                        || '=' || public.catalog_variant_identity_token(d.value #>> '{}'),
                                        '' order by d.key collate "C")
                        from jsonb_each(coalesce(p_dimensions, '{}'::jsonb)) as d(key, value)), '');
$$;

create or replace function public.catalog_variant_identity_key(
  p_manufacturer text, p_commercial_model text, p_model_year_start integer,
  p_model_year_end integer, p_official_model_code text, p_trim text, p_dimensions jsonb
) returns text
language sql
immutable
set search_path = pg_catalog
as $$
  select encode(sha256(convert_to(public.catalog_variant_identity_text(
           p_manufacturer, p_commercial_model, p_model_year_start, p_model_year_end,
           p_official_model_code, p_trim, p_dimensions), 'UTF8')), 'hex');
$$;

-- The stored register row's content, minus the datastore's own `_id`.
-- Storage-local like `payload_sha256`: only ever compared with a hash this
-- same database computed.
create or replace function public.catalog_variant_content_sha256(p_payload jsonb)
returns text
language sql
immutable
set search_path = pg_catalog
as $$
  select encode(sha256(convert_to((p_payload - '_id')::text, 'UTF8')), 'hex');
$$;

-- How strongly a status settles a variant (`coverage.STATUS_RANK`).
create or replace function public.catalog_variant_coverage_rank(p_status text)
returns integer
language sql
immutable
set search_path = pg_catalog
as $$
  select case p_status when 'enriched' then 4
                       when 'unresolved_ambiguous' then 3
                       when 'unresolved_not_found' then 3
                       when 'pending' then 2
                       when 'failed' then 1
                       else 0 end;
$$;

-- The ONE filtering rule (`coverage.coverage_decision`):
--   enriched with the same content                 -> excluded_already_enriched
--   unresolved_* with the same content AND the same
--   vocabulary, and no include_unresolved           -> excluded_known_unresolved
--   anything else (failed, pending, changed, none)  -> queue
create or replace function public.catalog_variant_coverage_decision(
  p_status text, p_recorded_content text, p_recorded_vocabulary text, p_content text,
  p_include_unresolved boolean
) returns text
language sql
immutable
set search_path = pg_catalog
as $$
  select case
    when p_status = 'enriched' and p_recorded_content = p_content
      then 'excluded_already_enriched'
    when p_status in ('unresolved_ambiguous', 'unresolved_not_found')
         and not coalesce(p_include_unresolved, false)
         and p_recorded_content = p_content
         and p_recorded_vocabulary = public.catalog_vocabulary_version()
      then 'excluded_known_unresolved'
    else 'queue' end;
$$;

-- ---------------------------------------------------------------------------
-- 2. The relations.
-- ---------------------------------------------------------------------------

create table if not exists public.catalog_variant_coverage (
  id uuid primary key default gen_random_uuid(),
  variant_identity_key text not null,
  level text not null,
  status text not null,
  -- The run whose durable data the row was derived from. RESTRICT: the row is
  -- an index over that run and must never outlive it silently.
  last_run_id uuid not null references public.runs(id) on delete restrict,
  snapshot_key text not null,
  content_sha256 text not null,
  -- The reviewed vocabulary in force when the row was written; an unresolved
  -- variant is queued again once it moves.
  vocabulary_version text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint catalog_variant_coverage_key_shape check (variant_identity_key ~ '^[0-9a-f]{64}$'),
  constraint catalog_variant_coverage_level check (level in ('register')),
  constraint catalog_variant_coverage_status
    check (status in ('enriched', 'unresolved_ambiguous', 'unresolved_not_found', 'failed',
                      'pending')),
  constraint catalog_variant_coverage_snapshot_bounded
    check (char_length(snapshot_key) between 1 and 200),
  constraint catalog_variant_coverage_content_shape check (content_sha256 ~ '^[0-9a-f]{64}$'),
  constraint catalog_variant_coverage_vocabulary_bounded
    check (char_length(vocabulary_version) between 1 and 80)
);
-- One row per variant and level: the ledger never pays twice for either.
create unique index if not exists catalog_variant_coverage_key_level_uidx
  on public.catalog_variant_coverage(variant_identity_key, level);
create index if not exists catalog_variant_coverage_run_idx
  on public.catalog_variant_coverage(last_run_id);

create table if not exists public.catalog_work_scope_unit_coverage (
  id uuid primary key default gen_random_uuid(),
  preparation_id uuid not null
    references public.catalog_work_scope_preparations(id) on delete restrict,
  unit_id uuid not null references public.catalog_work_scope_units(id) on delete restrict,
  unit_key text not null,
  level text not null,
  vocabulary_version text not null,
  include_unresolved boolean not null,
  excluded_already_enriched integer not null,
  excluded_known_unresolved integer not null,
  -- [{"upstream_record_id", "reason"}], sorted by the register's own id: every
  -- candidate the ledger left out of this unit's queue.
  excluded_records jsonb not null,
  created_at timestamptz not null default now(),
  constraint catalog_work_scope_unit_coverage_level check (level in ('register')),
  constraint catalog_work_scope_unit_coverage_counts
    check (excluded_already_enriched >= 0 and excluded_known_unresolved >= 0),
  constraint catalog_work_scope_unit_coverage_records
    check (jsonb_typeof(excluded_records) = 'array'
           and jsonb_array_length(excluded_records)
               = excluded_already_enriched + excluded_known_unresolved
           and jsonb_array_length(excluded_records) <= 20000),
  constraint catalog_work_scope_unit_coverage_vocabulary_bounded
    check (char_length(vocabulary_version) between 1 and 80)
);
create unique index if not exists catalog_work_scope_unit_coverage_unit_uidx
  on public.catalog_work_scope_unit_coverage(unit_id);
create index if not exists catalog_work_scope_unit_coverage_preparation_idx
  on public.catalog_work_scope_unit_coverage(preparation_id);

-- A preparation's record is a decision already made: never rewritten.
drop trigger if exists catalog_work_scope_unit_coverage_append_only
  on public.catalog_work_scope_unit_coverage;
create trigger catalog_work_scope_unit_coverage_append_only
  before update or delete on public.catalog_work_scope_unit_coverage
  for each row execute function public.forbid_work_scope_preparation_mutation();

-- ---------------------------------------------------------------------------
-- 3. The plan record: one optional key.
-- ---------------------------------------------------------------------------

create or replace function public.catalog_work_scope_record_valid(p_scope jsonb)
returns boolean
language plpgsql
immutable
set search_path = pg_catalog
as $$
declare
  v_years jsonb;
  v_from integer;
  v_to integer;
begin
  if p_scope is null or jsonb_typeof(p_scope) <> 'object' then
    return false;
  end if;
  -- PR-Z: `include_unresolved` is the ONE optional key, and it is stated only
  -- as `true` (absent means false), so every record written before it keeps
  -- its canonical text and digest.
  if (select array_agg(k order by k collate "C") from jsonb_object_keys(p_scope) as k
       where k <> 'include_unresolved')
       is distinct from array['batch_size', 'contract', 'directory_version', 'max_items',
                              'model_years', 'source', 'units'] then
    return false;
  end if;
  if p_scope ? 'include_unresolved' and p_scope->'include_unresolved' is distinct from 'true'::jsonb then
    return false;
  end if;
  if p_scope->>'contract' is distinct from public.catalog_work_scope_contract_version()
     or p_scope->'source' is distinct from jsonb_build_object(
          'family', 'government', 'package_id', 'degem-rechev-wltp',
          'resource_id', '142afde2-6228-49f9-8a29-9b6c3a0cbe40') then
    return false;
  end if;
  if jsonb_typeof(p_scope->'directory_version') <> 'string'
     or char_length(p_scope->>'directory_version') not between 1 and 80 then
    return false;
  end if;
  if jsonb_typeof(p_scope->'batch_size') <> 'number'
     or p_scope->>'batch_size' !~ '^[0-9]{1,4}$'
     or (p_scope->>'batch_size')::integer not between 1 and 20 then
    return false;
  end if;
  if jsonb_typeof(p_scope->'max_items') <> 'number'
     or p_scope->>'max_items' !~ '^[0-9]{1,6}$'
     or (p_scope->>'max_items')::integer not between 1 and 2000 then
    return false;
  end if;
  if jsonb_typeof(p_scope->'units') <> 'array'
     or jsonb_array_length(p_scope->'units') not between 1 and 64
     or exists (select 1 from jsonb_array_elements(p_scope->'units') as u(value)
                 where jsonb_typeof(u.value) <> 'string'
                    or u.value #>> '{}' !~ '^[a-z][a-z0-9_]{0,39}$')
     or (select count(distinct u.value) from jsonb_array_elements(p_scope->'units') as u(value))
          <> jsonb_array_length(p_scope->'units') then
    return false;
  end if;
  v_years := p_scope->'model_years';
  if jsonb_typeof(v_years) <> 'object'
     or (select array_agg(k order by k collate "C") from jsonb_object_keys(v_years) as k)
          is distinct from array['from', 'to'] then
    return false;
  end if;
  if jsonb_typeof(v_years->'from') not in ('null', 'number')
     or jsonb_typeof(v_years->'to') not in ('null', 'number') then
    return false;
  end if;
  if jsonb_typeof(v_years->'from') = 'number' then
    if v_years->>'from' !~ '^[0-9]{4}$' then return false; end if;
    v_from := (v_years->>'from')::integer;
    if v_from not between 1900 and 2100 then return false; end if;
  end if;
  if jsonb_typeof(v_years->'to') = 'number' then
    if v_years->>'to' !~ '^[0-9]{4}$' then return false; end if;
    v_to := (v_years->>'to')::integer;
    if v_to not between 1900 and 2100 then return false; end if;
  end if;
  if v_from is not null and v_to is not null and v_from > v_to then
    return false;
  end if;
  return true;
end;
$$;

-- ---------------------------------------------------------------------------
-- 4. Filtering at the queue build (point a).
-- ---------------------------------------------------------------------------
--
-- Every queueable (`candidate`, in the plan's years) row of one snapshot, with
-- its register id and what the ledger decides for it at the `register` level.
create or replace function public.catalog_work_scope_coverage_decisions(
  p_snapshot_id uuid, p_from integer, p_to integer, p_include_unresolved boolean
) returns table (candidate_id uuid, upstream_record_id text, decision text)
language sql
stable
set search_path = pg_catalog
as $$
  select c.id, r.upstream_record_id,
         public.catalog_variant_coverage_decision(cv.status, cv.content_sha256,
                                                  cv.vocabulary_version,
                                                  public.catalog_variant_content_sha256(r.payload),
                                                  p_include_unresolved)
    from public.catalog_candidate_variants c
    join public.catalog_raw_records r on r.id = c.raw_record_id
    left join public.catalog_variant_coverage cv
      on cv.level = 'register'
     and cv.variant_identity_key = public.catalog_variant_identity_key(
           c.manufacturer, c.commercial_model, c.model_year_start, c.model_year_end,
           c.official_model_code, c.trim, c.identity_dimensions)
   where c.snapshot_id = p_snapshot_id and c.status = 'candidate'
     and (p_from is null or c.model_year_start >= p_from)
     and (p_to is null or c.model_year_end <= p_to);
$$;

-- The queue build, restated with ONE change: a queueable candidate the ledger
-- already settles is left out, and counted, before the plan's limit is spent.
-- Same signature, same lease guard, same answers while the ledger is empty.
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
  -- PR-Z: the coverage ledger's part of the decision.
  v_include boolean;
  v_vocabulary text := public.catalog_vocabulary_version();
  v_enriched integer;
  v_unresolved integer;
  v_excluded jsonb;
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
  -- PR-Z: an explicit request on the revision itself to queue again what the
  -- ledger records as unresolved.
  v_include := coalesce(v_rev.scope->'include_unresolved' = 'true'::jsonb, false);

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
    v_enriched := 0; v_unresolved := 0; v_excluded := '[]'::jsonb;
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
          -- PR-Z: never pay twice. A queueable candidate the coverage ledger
          -- already settles at this level -- enriched from the same content,
          -- or known unresolved under the same content and vocabulary -- is
          -- left out BEFORE the plan's limit is spent, and counted with its
          -- register id. `eligible_count` keeps counting every queueable row.
          select count(*) filter (where d.decision = 'excluded_already_enriched'),
                 count(*) filter (where d.decision = 'excluded_known_unresolved'),
                 coalesce(jsonb_agg(jsonb_build_object('upstream_record_id', d.upstream_record_id,
                                                       'reason', d.decision)
                                    order by d.upstream_record_id collate "C")
                            filter (where d.decision <> 'queue'), '[]'::jsonb)
            into v_enriched, v_unresolved, v_excluded
            from public.catalog_work_scope_coverage_decisions(v_snapshot.id, v_from, v_to,
                                                               v_include) as d;
          v_take := least(v_eligible - v_enriched - v_unresolved, v_budget);
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
      'take', v_take, 'enriched', v_enriched, 'unresolved', v_unresolved,
      'excluded', v_excluded);
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
    -- PR-Z: what the ledger left out of this unit, counts AND register ids,
    -- recorded with the preparation and immutable like it.
    if v_unit_row.state = 'prepared' then
      insert into public.catalog_work_scope_unit_coverage
        (preparation_id, unit_id, unit_key, level, vocabulary_version, include_unresolved,
         excluded_already_enriched, excluded_known_unresolved, excluded_records)
      values (v_preparation.id, v_unit_row.id, v_unit_row.unit_key, 'register', v_vocabulary,
              v_include, (v_unit->>'enriched')::integer, (v_unit->>'unresolved')::integer,
              v_unit->'excluded');
    end if;
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
               and (v_to is null or c.model_year_end <= v_to)
               -- PR-Z: only what the ledger does not already settle.
               and c.id in (select d.candidate_id
                              from public.catalog_work_scope_coverage_decisions(
                                     v_unit_row.snapshot_id, v_from, v_to, v_include) as d
                             where d.decision = 'queue')) as q
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

-- The summary, restated to carry each prepared unit's ledger counts.
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
    'units', coalesce((select jsonb_agg(to_jsonb(u) || jsonb_build_object(
                                 -- PR-Z: the ledger's exclusions, as counts; the
                                 -- register ids stay in the durable row.
                                 'coverage', (select jsonb_build_object(
                                                'level', uc.level,
                                                'vocabulary_version', uc.vocabulary_version,
                                                'include_unresolved', uc.include_unresolved,
                                                'excluded_already_enriched',
                                                uc.excluded_already_enriched,
                                                'excluded_known_unresolved',
                                                uc.excluded_known_unresolved)
                                                from public.catalog_work_scope_unit_coverage uc
                                               where uc.unit_id = u.id))
                                 order by u.priority)
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
-- 5. Filtering at run preparation (point b).
-- ---------------------------------------------------------------------------
--
-- NULL when the batch does not exist. Otherwise every item of the batch, in
-- batch order, with its register id, identity key, content hash, the ledger's
-- row at that level (if any) and the ONE rule's decision -- under the
-- `include_unresolved` of the batch's own plan revision. Bounded by the batch
-- (at most 20 items).
create or replace function public.catalog_variant_coverage_for_batch(
  p_batch_id uuid, p_level text
) returns jsonb
language plpgsql
stable
set search_path = pg_catalog
as $$
declare
  v_batch public.catalog_work_scope_batches;
  v_include boolean;
begin
  if p_level is distinct from 'register' then
    raise exception 'CATALOG_COVERAGE_INVALID' using errcode = '22023';
  end if;
  select * into v_batch from public.catalog_work_scope_batches where id = p_batch_id;
  if v_batch.id is null then
    return null;
  end if;
  select coalesce(rev.scope->'include_unresolved' = 'true'::jsonb, false) into v_include
    from public.catalog_work_scope_revisions rev
   where rev.work_scope_id = v_batch.work_scope_id and rev.revision = v_batch.revision;
  return jsonb_build_object(
    'batch_id', v_batch.id,
    'level', p_level,
    'include_unresolved', coalesce(v_include, false),
    'vocabulary_version', public.catalog_vocabulary_version(),
    'items', coalesce((
      select jsonb_agg(jsonb_build_object(
               'batch_position', i.batch_position, 'candidate_id', c.id,
               'candidate_key', c.candidate_key, 'upstream_record_id', r.upstream_record_id,
               'variant_identity_key', k.identity_key, 'content_sha256', k.content,
               'status', cv.status, 'last_run_id', cv.last_run_id,
               'decision', public.catalog_variant_coverage_decision(
                             cv.status, cv.content_sha256, cv.vocabulary_version, k.content,
                             coalesce(v_include, false)))
               order by i.batch_position)
        from public.catalog_work_scope_queue_items i
        join public.catalog_candidate_variants c on c.id = i.candidate_id
        join public.catalog_raw_records r on r.id = c.raw_record_id
        cross join lateral (
          select public.catalog_variant_identity_key(
                   c.manufacturer, c.commercial_model, c.model_year_start, c.model_year_end,
                   c.official_model_code, c.trim, c.identity_dimensions) as identity_key,
                 public.catalog_variant_content_sha256(r.payload) as content) as k
        left join public.catalog_variant_coverage cv
          on cv.variant_identity_key = k.identity_key and cv.level = p_level
       where i.batch_id = v_batch.id), '[]'::jsonb));
end;
$$;

-- ---------------------------------------------------------------------------
-- 6. Writing the ledger.
-- ---------------------------------------------------------------------------
--
-- `p_entries` is [{"candidate_id": uuid, "status": s}, ...] (1..200): which
-- candidate of the run's pinned snapshot earned which status, as derived from
-- the run's durable output by `backend/catalog/coverage.derive_coverage`.
-- Everything else is the database's own: the run must have FINISHED
-- `completed` or `partial_success`, it must execute a Mapping Plan batch, every
-- candidate must be a row of that batch's snapshot, and the identity key, the
-- content hash, the snapshot key and the vocabulary version are derived here
-- from the stored rows.
--
-- The upsert is idempotent and never weakens what it knows: for the SAME
-- content a weaker status never replaces a stronger one (a later failure does
-- not erase an enrichment); a changed register row replaces whatever was
-- recorded. Applying the same run twice changes nothing.
create or replace function public.catalog_variant_coverage_apply(
  p_run_id uuid, p_level text, p_entries jsonb
) returns jsonb
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_status text;
  v_snapshot_id uuid;
  v_snapshot_key text;
  v_count integer;
  v_written integer;
begin
  if p_level is distinct from 'register' then
    raise exception 'CATALOG_COVERAGE_INVALID' using errcode = '22023';
  end if;
  if p_entries is null or jsonb_typeof(p_entries) <> 'array'
     or jsonb_array_length(p_entries) not between 1 and 200
     or exists (select 1 from jsonb_array_elements(p_entries) as e
                 where jsonb_typeof(e) <> 'object'
                    or (select array_agg(k order by k collate "C") from jsonb_object_keys(e) as k)
                         is distinct from array['candidate_id', 'status']
                    or jsonb_typeof(e->'candidate_id') <> 'string'
                    or e->>'candidate_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                    or e->>'status' is null
                    or e->>'status' not in ('enriched', 'unresolved_ambiguous',
                                            'unresolved_not_found', 'failed', 'pending')) then
    raise exception 'CATALOG_COVERAGE_INVALID' using errcode = '22023';
  end if;
  select r.status into v_status from public.runs r where r.id = p_run_id;
  if v_status is null or v_status not in ('completed', 'partial_success') then
    raise exception 'CATALOG_COVERAGE_RUN_NOT_FINISHED' using errcode = '55000';
  end if;
  select b.snapshot_id, b.snapshot_key into v_snapshot_id, v_snapshot_key
    from public.catalog_work_scope_batch_runs br
    join public.catalog_work_scope_batches b on b.id = br.batch_id
   where br.run_id = p_run_id;
  if v_snapshot_id is null then
    raise exception 'CATALOG_COVERAGE_RUN_UNBOUND' using errcode = '22023';
  end if;
  select count(*) into v_count
    from (select distinct (e->>'candidate_id')::uuid as id
            from jsonb_array_elements(p_entries) as e) as wanted
    join public.catalog_candidate_variants c
      on c.id = wanted.id and c.snapshot_id = v_snapshot_id;
  if v_count <> (select count(distinct e->>'candidate_id') from jsonb_array_elements(p_entries) as e) then
    raise exception 'CATALOG_COVERAGE_CANDIDATE_INVALID' using errcode = '22023';
  end if;

  -- One row per identity key: a duplicate group is ONE variant, settled by
  -- the strongest status any of its rows earned.
  insert into public.catalog_variant_coverage as cv
    (variant_identity_key, level, status, last_run_id, snapshot_key, content_sha256,
     vocabulary_version)
  select distinct on (x.identity_key)
         x.identity_key, p_level, x.status, p_run_id, v_snapshot_key, x.content,
         public.catalog_vocabulary_version()
    from (select public.catalog_variant_identity_key(
                   c.manufacturer, c.commercial_model, c.model_year_start, c.model_year_end,
                   c.official_model_code, c.trim, c.identity_dimensions) as identity_key,
                 public.catalog_variant_content_sha256(r.payload) as content,
                 e->>'status' as status
            from jsonb_array_elements(p_entries) as e
            join public.catalog_candidate_variants c on c.id = (e->>'candidate_id')::uuid
            join public.catalog_raw_records r on r.id = c.raw_record_id) as x
   order by x.identity_key, public.catalog_variant_coverage_rank(x.status) desc,
            x.status collate "C", x.content collate "C"
  on conflict (variant_identity_key, level) do update
     set status = excluded.status,
         last_run_id = excluded.last_run_id,
         snapshot_key = excluded.snapshot_key,
         content_sha256 = excluded.content_sha256,
         vocabulary_version = excluded.vocabulary_version,
         updated_at = now()
   where cv.content_sha256 is distinct from excluded.content_sha256
      or public.catalog_variant_coverage_rank(excluded.status)
           > public.catalog_variant_coverage_rank(cv.status)
      or (public.catalog_variant_coverage_rank(excluded.status)
            = public.catalog_variant_coverage_rank(cv.status)
          and (cv.status, cv.last_run_id, cv.snapshot_key, cv.vocabulary_version)
              is distinct from (excluded.status, excluded.last_run_id, excluded.snapshot_key,
                                excluded.vocabulary_version));
  get diagnostics v_written = row_count;
  return jsonb_build_object(
    'run_id', p_run_id, 'level', p_level, 'snapshot_key', v_snapshot_key,
    'entries', jsonb_array_length(p_entries), 'written', v_written);
end;
$$;

-- The finalize path's write. Accepted only from the lease identity that
-- finalized the run: the run's own worker, attempt and lease token, AFTER its
-- terminal state is durable (the apply step requires `completed` or
-- `partial_success`). A stale or foreign worker is refused like every other
-- stale write.
create or replace function public.record_catalog_variant_coverage_guarded(
  p_run_id uuid, p_worker_id text, p_attempt integer, p_lease_token text, p_level text,
  p_entries jsonb
) returns jsonb
language plpgsql
set search_path = pg_catalog
as $$
begin
  if p_worker_id is null or p_attempt is null or p_lease_token is null
     or not exists (select 1 from public.runs r
                     where r.id = p_run_id and r.worker_id = p_worker_id
                       and r.attempt = p_attempt and r.lease_token = p_lease_token) then
    raise exception 'STALE_WORKER_WRITE: coverage write rejected; run % is not held by worker %, attempt %',
      p_run_id, p_worker_id, p_attempt using errcode = '55000';
  end if;
  return public.catalog_variant_coverage_apply(p_run_id, p_level, p_entries);
end;
$$;

-- The operator backfill's write: the same derivation, from a finished run's
-- history, with no lease (the run is long over). Service role only.
create or replace function public.rebuild_catalog_variant_coverage(
  p_run_id uuid, p_level text, p_entries jsonb
) returns jsonb
language plpgsql
set search_path = pg_catalog
as $$
begin
  return public.catalog_variant_coverage_apply(p_run_id, p_level, p_entries);
end;
$$;

-- The backfill's listing: finished (`completed` / `partial_success`) runs that
-- executed a Mapping Plan batch, oldest first, one keyset page (1..50) at a
-- time. Replaying them in the order they finished leaves the ledger as the
-- finalize path would have.
create or replace function public.catalog_variant_coverage_runs(
  p_after_finished_at timestamptz, p_after_run_id uuid, p_limit integer
) returns table (run_id uuid, finished_at timestamptz, status text)
language sql
stable
set search_path = pg_catalog
as $$
  select r.id, r.finished_at, r.status
    from public.runs r
    join public.catalog_work_scope_batch_runs br on br.run_id = r.id
   where r.status in ('completed', 'partial_success')
     and r.finished_at is not null
     and (p_after_finished_at is null
          or (r.finished_at, r.id) > (p_after_finished_at, coalesce(p_after_run_id,
                                      '00000000-0000-0000-0000-000000000000'::uuid)))
   order by r.finished_at, r.id
   limit greatest(1, least(coalesce(p_limit, 50), 50));
$$;

-- The Mapping Plan's per-unit counts of one preparation (never the register
-- ids, which stay in the durable row). Empty for a preparation written before
-- this migration: the website then renders the unit without them.
create or replace function public.work_scope_unit_coverage(p_preparation_id uuid)
returns table (unit_key text, level text, include_unresolved boolean,
               excluded_already_enriched integer, excluded_known_unresolved integer)
language sql
stable
set search_path = pg_catalog
as $$
  select uc.unit_key, uc.level, uc.include_unresolved, uc.excluded_already_enriched,
         uc.excluded_known_unresolved
    from public.catalog_work_scope_unit_coverage uc
   where uc.preparation_id = p_preparation_id
   order by uc.unit_key collate "C";
$$;

-- ---------------------------------------------------------------------------
-- 7. RLS and privileges: service-path only. The owner reads the counts
--    through the API (the membership-gated plan progress), never directly.
-- ---------------------------------------------------------------------------
alter table public.catalog_variant_coverage enable row level security;
alter table public.catalog_work_scope_unit_coverage enable row level security;

do $$
declare fn text; relation text;
begin
  foreach fn in array array[
    'public.catalog_vocabulary_version()',
    'public.catalog_variant_identity_token(text)',
    'public.catalog_variant_identity_text(text,text,integer,integer,text,text,jsonb)',
    'public.catalog_variant_identity_key(text,text,integer,integer,text,text,jsonb)',
    'public.catalog_variant_content_sha256(jsonb)',
    'public.catalog_variant_coverage_rank(text)',
    'public.catalog_variant_coverage_decision(text,text,text,text,boolean)',
    'public.catalog_work_scope_record_valid(jsonb)',
    'public.catalog_work_scope_coverage_decisions(uuid,integer,integer,boolean)',
    'public.prepare_work_scope_queue(uuid,text,integer,text,jsonb)',
    'public.work_scope_preparation_summary(uuid,boolean)',
    'public.catalog_variant_coverage_for_batch(uuid,text)',
    'public.catalog_variant_coverage_apply(uuid,text,jsonb)',
    'public.record_catalog_variant_coverage_guarded(uuid,text,integer,text,text,jsonb)',
    'public.rebuild_catalog_variant_coverage(uuid,text,jsonb)',
    'public.catalog_variant_coverage_runs(timestamptz,uuid,integer)',
    'public.work_scope_unit_coverage(uuid)'
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
    'public.catalog_variant_coverage', 'public.catalog_work_scope_unit_coverage'
  ] loop
    execute format('revoke all on table %s from public', relation);
    if exists (select 1 from pg_roles where rolname='anon') then
      execute format('revoke all on table %s from anon', relation);
    end if;
    if exists (select 1 from pg_roles where rolname='authenticated') then
      execute format('revoke all on table %s from authenticated', relation);
    end if;
  end loop;
  if exists (select 1 from pg_roles where rolname='service_role') then
    -- The ledger is upserted, never deleted; a preparation's record is
    -- append-only.
    execute 'grant select, insert, update on table public.catalog_variant_coverage to service_role';
    execute 'revoke delete on table public.catalog_variant_coverage from service_role';
    execute 'grant select, insert on table public.catalog_work_scope_unit_coverage to service_role';
    execute 'revoke update, delete on table public.catalog_work_scope_unit_coverage from service_role';
  end if;
end $$;

-- P27 (PR-HYG): a placeholder source record is left out when the Mapping
-- Plan's queue is BUILT, not only when a batch run is prepared.
--
-- PR-U's ONE placeholder rule (backend/catalog/government/preparation.py
-- `PLACEHOLDER_IDENTITY` / `is_placeholder_identity`): a candidate whose
-- commercial model or official model code is a single digit repeated three
-- or more times ("111", "11111111") is not a vehicle. Until now it was
-- queued, spent a slot of the plan's limit, and was dropped from the run
-- (EXCLUDED_PLACEHOLDER_SOURCE_RECORD) only at batch preparation. Now the
-- queue build leaves it out: it is not eligible, it is never queued, and it
-- is recorded with its register id under `excluded_placeholder_source_record`
-- and counted (`excluded_placeholder`), beside the ledger's exclusions. Batch
-- preparation keeps its own check unchanged (plans prepared before this).
--
-- `catalog_work_scope_coverage_decisions`, `prepare_work_scope_queue` and
-- `work_scope_preparation_summary` are restated from 20260927000100 with only
-- the lines marked P27 changed. Existing preparations, queues and ledger rows
-- are untouched. Forward-only and rerun-safe; `create or replace` keeps every
-- grant.

-- The rule, once. Mirrors `preparation.is_placeholder_identity` (candidate
-- columns are already trimmed-exact).
create or replace function public.catalog_is_placeholder_identity(p_commercial_model text,
                                                                  p_official_model_code text)
returns boolean
language sql
immutable
set search_path = pg_catalog
as $$
  select coalesce(p_commercial_model ~ '^([0-9])\1{2,}$', false)
      or coalesce(p_official_model_code ~ '^([0-9])\1{2,}$', false)
$$;

alter table public.catalog_work_scope_unit_coverage
  add column if not exists excluded_placeholder integer not null default 0;
alter table public.catalog_work_scope_unit_coverage
  drop constraint if exists catalog_work_scope_unit_coverage_placeholder_count;
alter table public.catalog_work_scope_unit_coverage
  add constraint catalog_work_scope_unit_coverage_placeholder_count check (excluded_placeholder >= 0);
alter table public.catalog_work_scope_unit_coverage
  drop constraint if exists catalog_work_scope_unit_coverage_records;
alter table public.catalog_work_scope_unit_coverage
  add constraint catalog_work_scope_unit_coverage_records
    check (jsonb_typeof(excluded_records) = 'array'
           and jsonb_array_length(excluded_records)
               = excluded_already_enriched + excluded_known_unresolved + excluded_placeholder
           and jsonb_array_length(excluded_records) <= 20000);

create or replace function public.catalog_work_scope_coverage_decisions(
  p_snapshot_id uuid, p_from integer, p_to integer, p_include_unresolved boolean
) returns table (candidate_id uuid, upstream_record_id text, decision text)
language sql
stable
set search_path = pg_catalog
as $$
  select c.id, r.upstream_record_id,
         -- P27: a placeholder source record is never queued, whatever the ledger says.
         case when public.catalog_is_placeholder_identity(c.commercial_model, c.official_model_code)
              then 'excluded_placeholder_source_record'
              else public.catalog_variant_coverage_decision(cv.status, cv.content_sha256,
                                                            cv.vocabulary_version,
                                                            public.catalog_variant_content_sha256(r.payload),
                                                            p_include_unresolved) end
    from public.catalog_candidate_variants c
    join public.catalog_raw_records r on r.id = c.raw_record_id
    left join public.catalog_variant_coverage cv
      on cv.level = 'register'
     and cv.variant_identity_key = public.catalog_variant_identity_key(
           c.manufacturer, c.commercial_model, c.model_year_start, c.model_year_end,
           c.official_model_code, c.trim, r.payload->>'tozeret_cd', r.payload->>'degem_cd',
           r.payload->>'sug_degem', c.identity_dimensions)
   where c.snapshot_id = p_snapshot_id and c.status = 'candidate'
     and (p_from is null or c.model_year_start >= p_from)
     and (p_to is null or c.model_year_end <= p_to);
$$;

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
  v_placeholder integer;
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
    v_enriched := 0; v_unresolved := 0; v_placeholder := 0; v_excluded := '[]'::jsonb;
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
               -- P27: a placeholder is not queueable, so it is not eligible.
               count(*) filter (where c.status = 'candidate' and not public.catalog_is_placeholder_identity(c.commercial_model, c.official_model_code))
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
                 count(*) filter (where d.decision = 'excluded_placeholder_source_record'),
                 coalesce(jsonb_agg(jsonb_build_object('upstream_record_id', d.upstream_record_id,
                                                       'reason', d.decision)
                                    order by d.upstream_record_id collate "C")
                            filter (where d.decision <> 'queue'), '[]'::jsonb)
            into v_enriched, v_unresolved, v_placeholder, v_excluded
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
      'placeholder', v_placeholder, 'excluded', v_excluded);
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
         excluded_already_enriched, excluded_known_unresolved, excluded_placeholder,
         excluded_records)
      values (v_preparation.id, v_unit_row.id, v_unit_row.unit_key, 'register', v_vocabulary,
              v_include, (v_unit->>'enriched')::integer, (v_unit->>'unresolved')::integer,
              (v_unit->>'placeholder')::integer, v_unit->'excluded');
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
                                                uc.excluded_known_unresolved,
                                                'excluded_placeholder',
                                                uc.excluded_placeholder)
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

do $$
begin
  execute 'revoke execute on function public.catalog_is_placeholder_identity(text,text) from public';
  if exists (select 1 from pg_roles where rolname = 'anon') then
    execute 'revoke execute on function public.catalog_is_placeholder_identity(text,text) from anon';
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    execute 'revoke execute on function public.catalog_is_placeholder_identity(text,text) from authenticated';
  end if;
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    execute 'grant execute on function public.catalog_is_placeholder_identity(text,text) to service_role';
  end if;
end $$;

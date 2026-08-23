-- S1-PR4: retry-safe, lease-guarded Evidence Board writes.
--
-- Evidence workers may write only through these RPCs.  Each operation takes
-- and validates the complete run lease before touching durable state.  The
-- small provenance/idempotency columns below are compatible additions to the
-- existing evidence tables; the tables remain the canonical evidence store.

alter table public.tool_usage add column if not exists idempotency_key text;
alter table public.tool_usage add column if not exists task_key text;
alter table public.sources add column if not exists evidence_key text;
alter table public.sources add column if not exists task_key text;
alter table public.claims add column if not exists evidence_key text;
alter table public.claims add column if not exists task_key text;
alter table public.conflicts add column if not exists evidence_key text;
alter table public.conflicts add column if not exists task_key text;

create unique index if not exists tool_usage_run_idempotency_uidx
  on public.tool_usage(run_id, idempotency_key) where idempotency_key is not null;
create unique index if not exists sources_run_evidence_uidx
  on public.sources(run_id, evidence_key) where evidence_key is not null;
create unique index if not exists claims_run_evidence_uidx
  on public.claims(run_id, evidence_key) where evidence_key is not null;
create unique index if not exists conflicts_run_evidence_uidx
  on public.conflicts(run_id, evidence_key) where evidence_key is not null;

create or replace function public.create_tool_usage_guarded(
  p_run_id uuid, p_worker_id text, p_attempt integer, p_lease_token text,
  p_usage jsonb
) returns setof public.tool_usage
language plpgsql
set search_path = pg_catalog
as $$
declare v_row public.tool_usage; v_grant public.tool_grants;
begin
  perform public.assert_worker_lease(p_run_id, p_worker_id, p_attempt, p_lease_token);
  if p_usage::text ~* '"(chain_of_thought|provider_detail|raw_error|api_key|secret|password|authorization|credentials|exception|lease_token|token)"[[:space:]]*:'
     or lower(p_usage::text) like '%secret sentinel%' then
    raise exception 'unsafe evidence payload rejected' using errcode = '22023';
  end if;
  if nullif(p_usage->>'idempotency_key', '') is null then
    raise exception 'invalid tool usage: idempotency_key is required' using errcode = '22023';
  end if;
  select * into v_grant from public.tool_grants
    where id = (p_usage->>'grant_id')::uuid
      and run_id = p_run_id
      and agent = p_usage->>'agent'
      and tool = p_usage->>'tool'
    for key share;
  if v_grant.id is null then
    raise exception 'invalid tool grant' using errcode = '23503';
  end if;
  insert into public.tool_usage
    (run_id, grant_id, agent, tool, operation, query, url, status, error, idempotency_key, task_key)
  values (p_run_id, (p_usage->>'grant_id')::uuid, p_usage->>'agent', p_usage->>'tool',
    p_usage->>'operation', p_usage->>'query', p_usage->>'url', p_usage->>'status',
    p_usage->'error', p_usage->>'idempotency_key', p_usage->>'task_key')
  on conflict (run_id, idempotency_key) where idempotency_key is not null do nothing
  returning * into v_row;
  if v_row is null then
    select * into v_row from public.tool_usage
      where run_id = p_run_id and idempotency_key = p_usage->>'idempotency_key';
  end if;
  return next v_row;
end;
$$;

-- Patch only Evidence Board-owned blackboard fields.  Unlike the general
-- blackboard upsert this can never reset planning/execution state when an
-- evidence worker persists a summary.
create or replace function public.patch_run_blackboard_evidence_guarded(
  p_run_id uuid, p_worker_id text, p_attempt integer, p_lease_token text,
  p_summary jsonb
) returns setof public.run_blackboards
language plpgsql
set search_path = pg_catalog
as $$
declare v_row public.run_blackboards;
begin
  perform public.assert_worker_lease(p_run_id, p_worker_id, p_attempt, p_lease_token);
  insert into public.run_blackboards
    (run_id, goal, known_entities, claims_conflict_summaries)
  values (p_run_id, '', coalesce(p_summary->'known_entities', '[]'::jsonb),
          coalesce(p_summary->'claims_conflict_summaries', '[]'::jsonb))
  on conflict (run_id) do update set
    known_entities = excluded.known_entities,
    claims_conflict_summaries = excluded.claims_conflict_summaries,
    updated_at = now()
  returning * into v_row;
  return next v_row;
end;
$$;

create or replace function public.upsert_source_guarded(
  p_run_id uuid, p_worker_id text, p_attempt integer, p_lease_token text,
  p_source jsonb
) returns setof public.sources
language plpgsql
set search_path = pg_catalog
as $$
declare v_row public.sources;
begin
  perform public.assert_worker_lease(p_run_id, p_worker_id, p_attempt, p_lease_token);
  if p_source::text ~* '"(chain_of_thought|provider_detail|raw_error|api_key|secret|password|authorization|credentials|exception|lease_token|token)"[[:space:]]*:'
     or lower(p_source::text) like '%secret sentinel%' then
    raise exception 'unsafe evidence payload rejected' using errcode = '22023';
  end if;
  if nullif(p_source->>'evidence_key', '') is null or nullif(p_source->>'task_key', '') is null then
    raise exception 'invalid source: evidence_key and task_key are required' using errcode = '22023';
  end if;
  insert into public.sources
    (run_id, agent, url, title, domain, source_type, source_strength, source_date,
     retrieved_at, query, tool_operation, evidence_key, task_key)
  values (p_run_id, p_source->>'agent', p_source->>'url', p_source->>'title',
    p_source->>'domain', p_source->>'source_type', p_source->>'source_strength',
    p_source->>'source_date', coalesce((p_source->>'retrieved_at')::timestamptz, now()),
    p_source->>'query', p_source->>'tool_operation', p_source->>'evidence_key', p_source->>'task_key')
  on conflict (run_id, evidence_key) where evidence_key is not null do nothing
  returning * into v_row;
  if v_row is null then
    select * into v_row from public.sources
      where run_id = p_run_id and evidence_key = p_source->>'evidence_key';
  end if;
  return next v_row;
end;
$$;

-- Claim creation and its mandatory source link are one PostgreSQL statement
-- transaction.  Any missing/cross-run source or link failure aborts the claim.
create or replace function public.create_claim_with_source_guarded(
  p_run_id uuid, p_worker_id text, p_attempt integer, p_lease_token text,
  p_claim jsonb
) returns setof public.claims
language plpgsql
set search_path = pg_catalog
as $$
declare v_row public.claims; v_source public.sources;
begin
  perform public.assert_worker_lease(p_run_id, p_worker_id, p_attempt, p_lease_token);
  if p_claim::text ~* '"(chain_of_thought|provider_detail|raw_error|api_key|secret|password|authorization|credentials|exception|lease_token|token)"[[:space:]]*:'
     or lower(p_claim::text) like '%secret sentinel%' then
    raise exception 'unsafe evidence payload rejected' using errcode = '22023';
  end if;
  if nullif(p_claim->>'evidence_key', '') is null or nullif(p_claim->>'task_key', '') is null then
    raise exception 'invalid claim: evidence_key and task_key are required' using errcode = '22023';
  end if;
  select * into v_source from public.sources
    where id = (p_claim->>'source_id')::uuid and run_id = p_run_id for key share;
  if v_source.id is null then
    raise exception 'invalid claim source' using errcode = '23503';
  end if;
  insert into public.claims
    (run_id, entity_key, field_key, value, unit, time_scope, geography, market,
     source_id, source_strength, confidence, agent, status, evidence_key, task_key)
  values (p_run_id, p_claim->>'entity_key', p_claim->>'field_key', p_claim->'value',
    p_claim->>'unit', coalesce(p_claim->'time_scope', '{}'::jsonb), p_claim->>'geography',
    p_claim->>'market', v_source.id, p_claim->>'source_strength',
    (p_claim->>'confidence')::numeric, p_claim->>'agent',
    coalesce(p_claim->>'status', 'active'), p_claim->>'evidence_key', p_claim->>'task_key')
  on conflict (run_id, evidence_key) where evidence_key is not null do nothing
  returning * into v_row;
  if v_row is null then
    select * into v_row from public.claims
      where run_id = p_run_id and evidence_key = p_claim->>'evidence_key';
    if v_row.source_id <> v_source.id then
      raise exception 'idempotency key belongs to a different source' using errcode = '22023';
    end if;
  end if;
  insert into public.source_claim_links(source_id, claim_id)
    values (v_source.id, v_row.id) on conflict do nothing;
  return next v_row;
end;
$$;

create or replace function public.create_conflict_guarded(
  p_run_id uuid, p_worker_id text, p_attempt integer, p_lease_token text,
  p_conflict jsonb
) returns setof public.conflicts
language plpgsql
set search_path = pg_catalog
as $$
declare v_row public.conflicts; v_ids uuid[]; v_count integer; v_scopes integer; v_values integer;
begin
  perform public.assert_worker_lease(p_run_id, p_worker_id, p_attempt, p_lease_token);
  if p_conflict::text ~* '"(chain_of_thought|provider_detail|raw_error|api_key|secret|password|authorization|credentials|exception|lease_token|token)"[[:space:]]*:'
     or lower(p_conflict::text) like '%secret sentinel%' or lower(coalesce(p_conflict->>'rationale', '')) like '%chain of thought%' then
    raise exception 'unsafe evidence payload rejected' using errcode = '22023';
  end if;
  if nullif(p_conflict->>'evidence_key', '') is null or nullif(p_conflict->>'task_key', '') is null then
    raise exception 'invalid conflict: evidence_key and task_key are required' using errcode = '22023';
  end if;
  select array_agg(value::uuid order by value::text) into v_ids
    from jsonb_array_elements_text(p_conflict->'claim_ids') value;
  if coalesce(cardinality(v_ids), 0) < 2 then
    raise exception 'a conflict requires at least two claims' using errcode = '22023';
  end if;
  select count(*), count(distinct row(entity_key, field_key, geography, market, time_scope)), count(distinct value)
    into v_count, v_scopes, v_values from public.claims
    where run_id = p_run_id and id = any(v_ids)
      and entity_key = p_conflict->>'entity_key' and field_key = p_conflict->>'field_key';
  if v_count <> cardinality(v_ids) or v_scopes <> 1 or v_values < 2 then
    raise exception 'conflict claims must exist, share one scope, and contradict' using errcode = '22023';
  end if;
  insert into public.conflicts
    (run_id, entity_key, field_key, claim_ids, outcome, rationale, evidence_key, task_key)
  values (p_run_id, p_conflict->>'entity_key', p_conflict->>'field_key', v_ids,
    coalesce(p_conflict->>'outcome', 'unresolved_needs_review'), p_conflict->>'rationale',
    p_conflict->>'evidence_key', p_conflict->>'task_key')
  on conflict (run_id, evidence_key) where evidence_key is not null do nothing
  returning * into v_row;
  if v_row is null then
    select * into v_row from public.conflicts
      where run_id = p_run_id and evidence_key = p_conflict->>'evidence_key';
  end if;
  return next v_row;
end;
$$;

do $$
declare fn text;
begin
  foreach fn in array array[
    'public.create_tool_usage_guarded(uuid,text,integer,text,jsonb)',
    'public.upsert_source_guarded(uuid,text,integer,text,jsonb)',
    'public.create_claim_with_source_guarded(uuid,text,integer,text,jsonb)',
    'public.create_conflict_guarded(uuid,text,integer,text,jsonb)',
    'public.patch_run_blackboard_evidence_guarded(uuid,text,integer,text,jsonb)'
  ] loop
    execute format('revoke execute on function %s from public', fn);
    if exists (select 1 from pg_roles where rolname='anon') then execute format('revoke execute on function %s from anon', fn); end if;
    if exists (select 1 from pg_roles where rolname='authenticated') then execute format('revoke execute on function %s from authenticated', fn); end if;
    if exists (select 1 from pg_roles where rolname='service_role') then execute format('grant execute on function %s to service_role', fn); end if;
  end loop;
end $$;

-- B1 correction: the durable conflict firewall must validate the SAME
-- canonical scope identity as the trusted backend implementation
-- (backend/engines/swarm_v2/normalization.py, SCOPE_NORMALIZATION_VERSION).
--
-- Previously create_conflict_guarded compared raw entity_key/field_key/
-- geography/market/time_scope text, so claims that the backend correctly
-- grouped as one canonical scope ("Toyota Corolla 2020" vs
-- "toyota_corolla_2020") were rejected at the durable boundary.
--
-- PostgreSQL deliberately does NOT re-implement the normalization algorithm
-- (Unicode NFKC / casefold / separator handling); that would create a second
-- independent scope definition.  Instead the trusted backend computes a
-- bounded SHA-256 canonical scope identity at claim persistence time, the
-- claim row stores it next to the UNTOUCHED original scope fields, and the
-- conflict RPC validates equality of that stored identity.  The firewall
-- stays fail-closed: unknown claims, cross-run claims, mixed canonical
-- scopes, mixed normalization versions, single-value groups, and claims
-- without a canonical identity are all rejected.
--
-- Legacy claims created before this migration keep a NULL canonical
-- identity.  They are never backfilled with a guessed scope and can never
-- join a canonical-scope conflict group (fail-closed), preserving historical
-- provenance exactly as written.

alter table public.claims add column if not exists canonical_scope_hash text;
alter table public.claims add column if not exists scope_normalization_version integer;

-- Claim creation: unchanged guarantees (lease, unsafe-payload rejection,
-- same-run source, evidence/task keys, idempotency, atomic source link) plus
-- the required trusted canonical identity for every new claim.
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
  -- The canonical identity is computed only by trusted backend code from the
  -- structured scope fields; the database verifies shape and bounds, never
  -- recomputes or infers it.
  if coalesce(p_claim->>'canonical_scope_hash', '') !~ '^[0-9a-f]{64}$'
     or coalesce(p_claim->>'scope_normalization_version', '') !~ '^[1-9][0-9]{0,3}$' then
    raise exception 'invalid claim: trusted canonical scope identity is required' using errcode = '22023';
  end if;
  select * into v_source from public.sources
    where id = (p_claim->>'source_id')::uuid and run_id = p_run_id for key share;
  if v_source.id is null then
    raise exception 'invalid claim source' using errcode = '23503';
  end if;
  insert into public.claims
    (run_id, entity_key, field_key, value, unit, time_scope, geography, market,
     source_id, source_strength, confidence, agent, status, evidence_key, task_key,
     canonical_scope_hash, scope_normalization_version)
  values (p_run_id, p_claim->>'entity_key', p_claim->>'field_key', p_claim->'value',
    p_claim->>'unit', coalesce(p_claim->'time_scope', '{}'::jsonb), p_claim->>'geography',
    p_claim->>'market', v_source.id, p_claim->>'source_strength',
    (p_claim->>'confidence')::numeric, p_claim->>'agent',
    coalesce(p_claim->>'status', 'active'), p_claim->>'evidence_key', p_claim->>'task_key',
    p_claim->>'canonical_scope_hash', (p_claim->>'scope_normalization_version')::integer)
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

-- Conflict creation: scope eligibility now uses the stored trusted canonical
-- identity instead of raw scope text.  Raw entity_key/field_key on the
-- conflict row remain representative provenance and must belong to one of
-- the referenced claims.
create or replace function public.create_conflict_guarded(
  p_run_id uuid, p_worker_id text, p_attempt integer, p_lease_token text,
  p_conflict jsonb
) returns setof public.conflicts
language plpgsql
set search_path = pg_catalog
as $$
declare v_row public.conflicts; v_ids uuid[]; v_count integer; v_scopes integer;
        v_untrusted integer; v_values integer;
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
  select count(*),
         count(distinct row(canonical_scope_hash, scope_normalization_version)),
         count(*) filter (where canonical_scope_hash is null
                          or canonical_scope_hash !~ '^[0-9a-f]{64}$'
                          or scope_normalization_version is null
                          or scope_normalization_version < 1),
         count(distinct value)
    into v_count, v_scopes, v_untrusted, v_values from public.claims
    where run_id = p_run_id and id = any(v_ids);
  if v_untrusted > 0 then
    -- Fail closed: legacy/pre-canonical claims are never grouped under a
    -- guessed scope identity.
    raise exception 'conflict claims require a trusted canonical scope identity' using errcode = '22023';
  end if;
  if v_count <> cardinality(v_ids) or v_scopes <> 1 or v_values < 2 then
    raise exception 'conflict claims must exist, share one scope, and contradict' using errcode = '22023';
  end if;
  if not exists (select 1 from public.claims
    where run_id = p_run_id and id = any(v_ids)
      and entity_key = p_conflict->>'entity_key' and field_key = p_conflict->>'field_key') then
    raise exception 'conflict provenance must match a referenced claim' using errcode = '22023';
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

-- CREATE OR REPLACE preserves existing grants, but the service-only ACL for
-- the replaced worker-path functions is re-asserted explicitly rather than
-- assumed.
do $$
declare fn text;
begin
  foreach fn in array array[
    'public.create_claim_with_source_guarded(uuid,text,integer,text,jsonb)',
    'public.create_conflict_guarded(uuid,text,integer,text,jsonb)'
  ] loop
    execute format('revoke execute on function %s from public', fn);
    if exists (select 1 from pg_roles where rolname='anon') then execute format('revoke execute on function %s from anon', fn); end if;
    if exists (select 1 from pg_roles where rolname='authenticated') then execute format('revoke execute on function %s from authenticated', fn); end if;
    if exists (select 1 from pg_roles where rolname='service_role') then execute format('grant execute on function %s to service_role', fn); end if;
  end loop;
end $$;

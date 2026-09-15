-- Catalog PR2: a raw record states WHERE in the capture it came from.
--
-- What was missing
-- ----------------
--
-- `public.catalog_raw_records` preserved the upstream row's own identity
-- (`upstream_record_id`), the resource it belongs to and its exact payload,
-- but not the POSITION it occupied in the retrieval that captured it. A
-- snapshot's `retrieval_metadata` records the page plan -- which offsets were
-- requested, at what page size, how many rows each page returned -- so the
-- capture as a whole is reconstructible, while an individual row could not
-- say which of those pages it came out of or where inside it.
--
-- That position is what makes a stored record checkable against the response
-- it was read from. Without it a reviewer can re-fetch the page a snapshot
-- names and still not know which row of it this is, so "traceable to the exact
-- page and record" would be a claim rather than a stored fact.
--
-- What this adds, and what it deliberately does not
-- -------------------------------------------------
--
-- ONE nullable-by-default jsonb column on the EXISTING relation, with a closed
-- key vocabulary and a small bound. It is GENERIC: `page_offset`,
-- `page_index`, `page_number` and `capture_index` describe any paginated
-- retrieval, and nothing here names `data.gov.il`, CKAN, a Government field or
-- a vehicle. No Government-specific table is created, no existing column,
-- constraint, index, policy, grant or RPC signature changes, and nothing is
-- backfilled -- the relation is still empty on this branch.
--
-- Additive and forward-only. The append-only trigger on this table is
-- unchanged and still refuses every UPDATE and DELETE, so a locator is written
-- exactly once with its row and can never be revised afterwards.

-- ---------------------------------------------------------------------------
-- 1. The closed locator vocabulary, as an IMMUTABLE predicate.
-- ---------------------------------------------------------------------------
--
-- Same device as `public.catalog_identity_dimensions_valid`: a CHECK may not
-- contain a subquery, so the rule lives in a pure function that reads no table.
-- It mirrors RAW_RECORD_LOCATOR_KEYS in backend/catalog/contracts.py and is
-- pinned against that tuple by tests/test_catalog_migration_static.py.
create or replace function public.catalog_source_locator_valid(p_locator jsonb)
returns boolean
language sql
immutable
set search_path = pg_catalog
as $$
  -- Strictly boolean, never NULL: a CHECK treats NULL as passing, so a helper
  -- that could yield NULL would admit the very rows it exists to refuse.
  select coalesce(
    p_locator is not null
    and jsonb_typeof(p_locator) = 'object'
    and not exists (
      select 1 from jsonb_each(p_locator) as entry(key, value)
      where entry.key not in ('capture_index', 'page_index', 'page_number', 'page_offset')
         -- A position is a NON-NEGATIVE WHOLE NUMBER. A string, a float, a
         -- boolean, an object and a negative value are all refusals: a
         -- position that cannot be compared is not a position.
         or jsonb_typeof(entry.value) <> 'number'
         or (entry.value #>> '{}') !~ '^[0-9]+$'
         or (entry.value #>> '{}')::numeric > 2147483647
    ), false)
$$;

-- ---------------------------------------------------------------------------
-- 2. The column.
-- ---------------------------------------------------------------------------

alter table public.catalog_raw_records
  add column if not exists source_locator jsonb not null default '{}'::jsonb;

alter table public.catalog_raw_records
  drop constraint if exists catalog_raw_records_locator_bounded,
  add constraint catalog_raw_records_locator_bounded
    check (char_length(source_locator::text) <= 256);

alter table public.catalog_raw_records
  drop constraint if exists catalog_raw_records_locator_allowlisted,
  add constraint catalog_raw_records_locator_allowlisted
    check (public.catalog_source_locator_valid(source_locator));

-- A position belongs to ONE row. Two records of one snapshot claiming the same
-- place in the capture would make the locator meaningless as provenance, so
-- the schema refuses it rather than the ingestion path remembering not to.
-- Partial: a record stored without a locator states no position and therefore
-- collides with nothing.
create unique index if not exists catalog_raw_records_snapshot_position_uidx
  on public.catalog_raw_records (snapshot_id, ((source_locator->>'capture_index')::integer))
  where source_locator ? 'capture_index';

-- ---------------------------------------------------------------------------
-- 3. The guarded write path carries it.
-- ---------------------------------------------------------------------------
--
-- Replaces the function body established by
-- `20260915120000_catalog_integrity_corrections.sql`, with the same name,
-- signature and lease posture. Every refusal that file added is preserved
-- verbatim -- the derived digest, the owning-run check, the terminal `failed`
-- state, the immutable active snapshot, the resource match and the replay
-- conflict -- and the locator joins the REPLAY IDENTITY, so a replay that
-- moves a row to a different position in the capture fails closed exactly as a
-- replay that changes its payload does.
create or replace function public.record_catalog_raw_record_guarded(
  p_run_id uuid, p_worker_id text, p_attempt integer, p_lease_token text,
  p_record jsonb
) returns setof public.catalog_raw_records
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_row public.catalog_raw_records;
  v_snapshot public.catalog_source_snapshots;
  v_key text; v_upstream text; v_payload jsonb; v_hash text; v_locator jsonb;
begin
  perform public.assert_worker_lease(p_run_id, p_worker_id, p_attempt, p_lease_token);
  -- NOTE: the credential/reasoning marker screen the other catalog RPCs apply
  -- is deliberately NOT applied here. This payload is SOURCE CONTENT captured
  -- verbatim from an upstream register, not backend-authored metadata, and a
  -- keyword screen over it would silently drop legitimate upstream rows whose
  -- own field names happen to collide. What bounds it instead is structural:
  -- object shape, the durable size bound, and a digest this function derives.
  v_key := nullif(p_record->>'record_key', '');
  v_upstream := nullif(p_record->>'upstream_record_id', '');
  v_payload := p_record->'payload';
  if v_key is null or v_upstream is null
     or v_payload is null or jsonb_typeof(v_payload) <> 'object' then
    raise exception 'invalid catalog raw record: identity or payload is missing' using errcode = '22023';
  end if;
  -- The digest is DERIVED, never supplied.
  if p_record ? 'payload_sha256' then
    raise exception 'catalog raw record payload digest is derived, not supplied' using errcode = '22023';
  end if;
  if char_length(v_payload::text) > 16384 then
    raise exception 'invalid catalog raw record: payload exceeds the durable bound' using errcode = '22023';
  end if;
  v_hash := encode(sha256(convert_to(v_payload::text, 'UTF8')), 'hex');

  -- The locator is OPTIONAL and, when present, exact. A record captured by a
  -- path that has no pagination states none; a record that states one is held
  -- to the closed vocabulary rather than trusted.
  v_locator := coalesce(p_record->'source_locator', '{}'::jsonb);
  if not public.catalog_source_locator_valid(v_locator) then
    raise exception 'invalid catalog raw record source locator' using errcode = '22023';
  end if;

  -- The owning snapshot is locked FOR UPDATE, so the stored-record counter
  -- below is exact under concurrency. Lock order is unchanged: public.runs
  -- (FOR SHARE, inside assert_worker_lease) and only then the object written.
  select * into v_snapshot from public.catalog_source_snapshots
    where id = (p_record->>'snapshot_id')::uuid for update;
  if v_snapshot.id is null then
    raise exception 'invalid catalog raw record snapshot' using errcode = '23503';
  end if;
  -- A snapshot belongs to the run that opened it.
  if v_snapshot.created_by_run_id is distinct from p_run_id then
    raise exception 'this catalog snapshot does not belong to this run' using errcode = '22023';
  end if;
  -- Both decided states are terminal.
  if v_snapshot.validation_state = 'failed' then
    raise exception 'a failed catalog snapshot is terminal' using errcode = '22023';
  end if;
  if v_snapshot.activated_at is not null then
    raise exception 'an active catalog snapshot is immutable' using errcode = '22023';
  end if;
  if nullif(p_record->>'resource_id', '') is distinct from v_snapshot.resource_id then
    raise exception 'catalog raw record resource mismatch' using errcode = '22023';
  end if;

  select * into v_row from public.catalog_raw_records
    where snapshot_id = v_snapshot.id and record_key = v_key;
  if v_row.id is null then
    insert into public.catalog_raw_records
      (snapshot_id, resource_id, upstream_record_id, payload, payload_sha256, record_key,
       source_locator)
    values (v_snapshot.id, v_snapshot.resource_id, v_upstream, v_payload, v_hash, v_key,
            v_locator)
    on conflict (snapshot_id, record_key) do nothing
    returning * into v_row;
    if v_row.id is not null then
      update public.catalog_source_snapshots
        set stored_record_count = stored_record_count + 1
        where id = v_snapshot.id;
      return next v_row;
      return;
    end if;
    select * into v_row from public.catalog_raw_records
      where snapshot_id = v_snapshot.id and record_key = v_key;
  end if;

  -- Replay: identical content AND identical position collapse onto the
  -- existing row; anything else under the same identity fails closed.
  if v_row.upstream_record_id is distinct from v_upstream
     or v_row.payload_sha256 is distinct from v_hash
     or v_row.payload is distinct from v_payload
     or v_row.source_locator is distinct from v_locator then
    raise exception 'catalog raw record idempotency conflict' using errcode = '22023';
  end if;
  return next v_row;
end;
$$;

-- ---------------------------------------------------------------------------
-- 4. Privileges for the new predicate.
-- ---------------------------------------------------------------------------
--
-- Explicit, exactly like every other object in this namespace: a constraint
-- helper is not a browser surface, so anon/authenticated lose the EXECUTE
-- Supabase default privileges would otherwise grant. The RPC above keeps the
-- grants its own migration established -- `create or replace function` does
-- not reset them -- and they are re-stated here so a reviewer does not have to
-- take that on trust.
do $$
declare fn text;
begin
  foreach fn in array array[
    'public.catalog_source_locator_valid(jsonb)',
    'public.record_catalog_raw_record_guarded(uuid,text,integer,text,jsonb)'
  ] loop
    execute format('revoke execute on function %s from public', fn);
    if exists (select 1 from pg_roles where rolname='anon') then execute format('revoke execute on function %s from anon', fn); end if;
    if exists (select 1 from pg_roles where rolname='authenticated') then execute format('revoke execute on function %s from authenticated', fn); end if;
    if exists (select 1 from pg_roles where rolname='service_role') then execute format('grant execute on function %s to service_role', fn); end if;
  end loop;
  -- The relation's own posture is unchanged and re-asserted: append-only for
  -- the service path, nothing at all for the browser roles.
  if exists (select 1 from pg_roles where rolname='service_role') then
    execute 'revoke update, delete on table public.catalog_raw_records from service_role';
  end if;
end $$;

-- Catalog ingestion recovery: adopt an orphaned pending snapshot, and write
-- raw records and candidates in bounded, lease-guarded batches.
--
-- The incident this answers (2026-09-24)
-- --------------------------------------
--
-- A scoped Toyota preparation captured 6 368 register rows in 8 s, then spent
-- ~20 min writing raw records one RPC per row and failed on candidate #3 580
-- with a repository error. The snapshot was never activated. Because a
-- snapshot's key is DERIVED from what was captured
-- (`backend/catalog/keys.py::snapshot_key`), every later capture of the same
-- register content resolves to that same row -- which belongs to the failed
-- run, and which every guarded write refuses to touch for any other run
-- (`created_by_run_id` is immutable, 20260914200000 / 20260915120000, and the
-- raw-record and activation RPCs check ownership). The content was wedged.
--
-- Why ADOPTION rather than "mark it failed and allow a new snapshot"
-- ------------------------------------------------------------------
--
-- A new snapshot of the same content is impossible without weakening identity:
-- `catalog_source_snapshots_key_uidx` is unique on the content-derived key and
-- `catalog_source_snapshots_natural_uidx` is unique on (family, resource,
-- version kind, version, content_sha256). Marking the orphan `failed` would
-- make that content PERMANENTLY unactivatable -- `failed` is terminal -- so
-- the register rows could never be prepared again until the register itself
-- changed. Adoption keeps every identity rule and every immutability rule and
-- adds exactly one, audited, transition.
--
-- What adoption is, exactly
-- -------------------------
--
-- `catalog_snapshot_adoptions` is an append-only record of "run B took over
-- the unfinished snapshot S from run A". A BEFORE-INSERT trigger -- which
-- holds for every writer, not only for the RPC -- admits a row only when ALL
-- of these hold, under row locks:
--
--   * S is PENDING: not activated and not failed (both are terminal);
--   * A is S's current owner, and A is terminal as `failed`, `cancelled` or
--     `timed_out` with no live lease;
--   * B is an `operator_capture` run holding a live lease, and B is not A.
--
-- The snapshot trigger is restated so `created_by_run_id` may change ONLY to
-- the adopter named by an adoption row written IN THE SAME TRANSACTION for
-- exactly that (snapshot, previous owner, adopter). Every other column stays
-- immutable, an active or failed snapshot stays frozen, and the stored count
-- still never decreases. The previous owner stays on the adoption row forever.
--
-- `adopt_catalog_snapshot_guarded` is the service path: it asserts the lease,
-- holds the caller to the snapshot's identity (key, resource, version, content
-- digest, declared count), its declared capture scope and its normalization
-- summary, and is idempotent -- a replay by the adopter returns the row. After
-- adoption the adopter continues through the EXISTING idempotent writes (a raw
-- record or candidate already stored collapses onto its key; a missing one is
-- written) and activation still passes the existing completeness gate.
--
-- Batches
-- -------
--
-- `record_catalog_raw_records_batch_guarded` and
-- `record_catalog_candidates_batch_guarded` take 1..500 rows of ONE snapshot,
-- assert the lease, and apply the unchanged single-row RPC to each row in
-- order, inside one transaction: the same validation and the same
-- idempotency, by construction, and all or nothing. The candidate batch also
-- requires the snapshot to be the caller's own and still pending, which the
-- single-row candidate RPC never checked. The single-row RPCs are unchanged.
--
-- Additive and forward-only: one new relation, one restated trigger function
-- (same trigger, same name), three new RPCs. Rerun-safe. service_role only.

-- ---------------------------------------------------------------------------
-- 1. The adoption record.
-- ---------------------------------------------------------------------------

create table if not exists public.catalog_snapshot_adoptions (
  id uuid primary key default gen_random_uuid(),
  snapshot_id uuid not null references public.catalog_source_snapshots(id) on delete restrict,
  previous_run_id uuid not null references public.runs(id) on delete restrict,
  adopted_by_run_id uuid not null references public.runs(id) on delete restrict,
  -- Stamped by the trigger from the rows themselves, never taken from a caller.
  previous_run_status text not null,
  stored_record_count_at_adoption integer not null,
  adopted_at timestamptz not null default now(),
  adoption_txid bigint not null default txid_current(),
  constraint catalog_snapshot_adoptions_distinct_runs
    check (previous_run_id <> adopted_by_run_id),
  constraint catalog_snapshot_adoptions_previous_terminal
    check (previous_run_status in ('failed', 'cancelled', 'timed_out')),
  constraint catalog_snapshot_adoptions_count_non_negative
    check (stored_record_count_at_adoption >= 0)
);

-- A previous owner hands a snapshot over once.
create unique index if not exists catalog_snapshot_adoptions_snapshot_previous_uidx
  on public.catalog_snapshot_adoptions(snapshot_id, previous_run_id);
create index if not exists catalog_snapshot_adoptions_previous_idx
  on public.catalog_snapshot_adoptions(previous_run_id);
create index if not exists catalog_snapshot_adoptions_adopter_idx
  on public.catalog_snapshot_adoptions(adopted_by_run_id);

-- Every condition, for every writer, under row locks. Lock order matches the
-- guarded writes: public.runs first, then the snapshot.
create or replace function public.catalog_check_snapshot_adoption() returns trigger
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_previous public.runs;
  v_adopter public.runs;
  v_snapshot public.catalog_source_snapshots;
begin
  select * into v_previous from public.runs where id = new.previous_run_id for share;
  select * into v_adopter from public.runs where id = new.adopted_by_run_id for share;
  if v_previous.id is null or v_adopter.id is null then
    raise exception 'CATALOG_SNAPSHOT_ADOPTION_REFUSED: unknown run' using errcode = '23503';
  end if;
  if v_previous.id = v_adopter.id then
    raise exception 'CATALOG_SNAPSHOT_ADOPTION_REFUSED: a run cannot adopt from itself'
      using errcode = '22023';
  end if;
  -- The adopter: an operator capture run, alive and leased.
  if v_adopter.run_identity->>'workflow_key' is distinct from 'operator_capture'
     or v_adopter.status not in ('starting', 'running')
     or v_adopter.lease_expires_at is null or v_adopter.lease_expires_at <= now() then
    raise exception 'CATALOG_SNAPSHOT_ADOPTION_REFUSED: only a live operator capture run adopts'
      using errcode = '22023';
  end if;
  -- The previous owner: finished unsuccessfully, and holding no live lease.
  if v_previous.status not in ('failed', 'cancelled', 'timed_out')
     or (v_previous.lease_expires_at is not null and v_previous.lease_expires_at > now()) then
    raise exception 'CATALOG_SNAPSHOT_ADOPTION_REFUSED: the owning run is live or did not fail'
      using errcode = '22023';
  end if;
  select * into v_snapshot from public.catalog_source_snapshots
    where id = new.snapshot_id for update;
  if v_snapshot.id is null then
    raise exception 'CATALOG_SNAPSHOT_ADOPTION_REFUSED: unknown snapshot' using errcode = '23503';
  end if;
  if v_snapshot.created_by_run_id is distinct from new.previous_run_id then
    raise exception 'CATALOG_SNAPSHOT_ADOPTION_REFUSED: the snapshot is not owned by that run'
      using errcode = '22023';
  end if;
  -- Both decided states are terminal and can never change owner.
  if v_snapshot.activated_at is not null or v_snapshot.validation_state <> 'pending' then
    raise exception 'CATALOG_SNAPSHOT_ADOPTION_REFUSED: only a pending snapshot is adopted'
      using errcode = '22023';
  end if;
  new.previous_run_status := v_previous.status;
  new.stored_record_count_at_adoption := v_snapshot.stored_record_count;
  new.adopted_at := now();
  new.adoption_txid := txid_current();
  return new;
end;
$$;

drop trigger if exists catalog_snapshot_adoptions_checked on public.catalog_snapshot_adoptions;
create trigger catalog_snapshot_adoptions_checked
  before insert on public.catalog_snapshot_adoptions
  for each row execute function public.catalog_check_snapshot_adoption();

-- The record is an audit trail: never rewritten, never removed.
drop trigger if exists catalog_snapshot_adoptions_append_only on public.catalog_snapshot_adoptions;
create trigger catalog_snapshot_adoptions_append_only
  before update or delete on public.catalog_snapshot_adoptions
  for each row execute function public.forbid_catalog_source_mutation();

-- ---------------------------------------------------------------------------
-- 2. The snapshot trigger, restated with exactly one audited exception.
-- ---------------------------------------------------------------------------
--
-- Identical to 20260915120000's body except that `created_by_run_id` may move
-- to the adopter an adoption row of THIS transaction names. The terminal
-- checks come first, so an active or failed snapshot never changes owner.
create or replace function public.forbid_catalog_snapshot_rewrite() returns trigger
language plpgsql as $$
begin
  if tg_op = 'DELETE' then
    raise exception 'catalog source material is append-only';
  end if;
  if old.validation_state = 'failed' then
    raise exception 'a failed catalog snapshot is terminal';
  end if;
  if old.activated_at is not null then
    raise exception 'an active catalog snapshot is immutable';
  end if;
  if new.id is distinct from old.id
     or new.source_family is distinct from old.source_family
     or new.trust_state is distinct from old.trust_state
     or new.resource_id is distinct from old.resource_id
     or new.upstream_version is distinct from old.upstream_version
     or new.upstream_version_kind is distinct from old.upstream_version_kind
     or new.content_sha256 is distinct from old.content_sha256
     or new.retrieved_at is distinct from old.retrieved_at
     or new.retrieval_metadata is distinct from old.retrieval_metadata
     or new.declared_record_count is distinct from old.declared_record_count
     or new.snapshot_key is distinct from old.snapshot_key
     or new.created_at is distinct from old.created_at then
    raise exception 'catalog snapshot identity is immutable';
  end if;
  if new.created_by_run_id is distinct from old.created_by_run_id
     and not exists (
       select 1 from public.catalog_snapshot_adoptions a
        where a.snapshot_id = old.id
          and a.previous_run_id = old.created_by_run_id
          and a.adopted_by_run_id = new.created_by_run_id
          and a.adoption_txid = txid_current()) then
    raise exception 'catalog snapshot identity is immutable';
  end if;
  if new.stored_record_count < old.stored_record_count then
    raise exception 'catalog snapshot record count cannot decrease';
  end if;
  return new;
end;
$$;

-- ---------------------------------------------------------------------------
-- 3. The adoption RPC.
-- ---------------------------------------------------------------------------

create or replace function public.adopt_catalog_snapshot_guarded(
  p_run_id uuid, p_worker_id text, p_attempt integer, p_lease_token text,
  p_snapshot jsonb
) returns setof public.catalog_source_snapshots
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_row public.catalog_source_snapshots;
  v_key text; v_family text; v_resource text; v_version text; v_kind text; v_hash text;
  v_declared integer; v_metadata jsonb; v_field text;
begin
  perform public.assert_worker_lease(p_run_id, p_worker_id, p_attempt, p_lease_token);
  if not exists (select 1 from public.runs r where r.id = p_run_id
                  and r.run_identity->>'workflow_key' = 'operator_capture') then
    raise exception 'CATALOG_SNAPSHOT_ADOPTION_REFUSED: only an operator capture run adopts'
      using errcode = '22023';
  end if;
  if p_snapshot is null or jsonb_typeof(p_snapshot) <> 'object' then
    raise exception 'invalid catalog snapshot adoption' using errcode = '22023';
  end if;
  if p_snapshot::text ~* '"(chain_of_thought|provider_detail|raw_error|api_key|secret|password|authorization|credentials|exception|lease_token|token)"[[:space:]]*:' then
    raise exception 'unsafe catalog payload rejected' using errcode = '22023';
  end if;
  if p_snapshot ? 'activated_at' or p_snapshot ? 'stored_record_count' then
    raise exception 'catalog snapshot activation is not a caller-supplied field' using errcode = '22023';
  end if;
  v_key := nullif(p_snapshot->>'snapshot_key', '');
  v_family := nullif(p_snapshot->>'source_family', '');
  v_resource := nullif(p_snapshot->>'resource_id', '');
  v_version := nullif(p_snapshot->>'upstream_version', '');
  v_kind := nullif(p_snapshot->>'upstream_version_kind', '');
  v_hash := nullif(p_snapshot->>'content_sha256', '');
  v_metadata := coalesce(p_snapshot->'retrieval_metadata', '{}'::jsonb);
  begin
    v_declared := (p_snapshot->>'declared_record_count')::integer;
  exception when others then
    raise exception 'invalid catalog snapshot adoption' using errcode = '22023';
  end;
  if v_key is null or v_family is null or v_resource is null or v_version is null
     or v_kind is null or v_hash is null or v_declared is null
     or jsonb_typeof(v_metadata) <> 'object' then
    raise exception 'invalid catalog snapshot adoption' using errcode = '22023';
  end if;

  select * into v_row from public.catalog_source_snapshots where snapshot_key = v_key for update;
  if v_row.id is null then
    raise exception 'CATALOG_SNAPSHOT_ADOPTION_REFUSED: unknown snapshot' using errcode = '22023';
  end if;
  -- The same retrieval, exactly: the snapshot RPC's own replay invariant.
  if v_row.source_family is distinct from v_family
     or v_row.resource_id is distinct from v_resource
     or v_row.upstream_version is distinct from v_version
     or v_row.upstream_version_kind is distinct from v_kind
     or v_row.content_sha256 is distinct from v_hash
     or v_row.declared_record_count is distinct from v_declared then
    raise exception 'catalog snapshot idempotency conflict' using errcode = '22023';
  end if;
  -- The same declared scope and the same content-derived reading of it: a
  -- scoped capture never adopts an unscoped one or another marque's, and a
  -- capture read under another normalization contract never adopts either.
  foreach v_field in array array['capture_scope', 'capture_contract', 'page_chain_sha256',
                                  'normalization_contract', 'normalized_record_count',
                                  'normalization_issue_count', 'normalization_issues',
                                  'normalization_issue_records'] loop
    if v_row.retrieval_metadata->v_field is distinct from v_metadata->v_field then
      raise exception 'CATALOG_SNAPSHOT_ADOPTION_REFUSED: the capture does not match the snapshot'
        using errcode = '22023';
    end if;
  end loop;

  -- Already this run's own (an earlier adoption, or its own capture): replay.
  if v_row.created_by_run_id = p_run_id then
    return next v_row;
    return;
  end if;
  -- The trigger on the adoption record decides; the snapshot trigger admits
  -- the owner change only because that record now exists in this transaction.
  insert into public.catalog_snapshot_adoptions
    (snapshot_id, previous_run_id, adopted_by_run_id, previous_run_status,
     stored_record_count_at_adoption)
  values (v_row.id, v_row.created_by_run_id, p_run_id, 'failed', 0);
  update public.catalog_source_snapshots set created_by_run_id = p_run_id
    where id = v_row.id returning * into v_row;
  return next v_row;
end;
$$;

-- ---------------------------------------------------------------------------
-- 4. Bounded batches over the unchanged single-row writes.
-- ---------------------------------------------------------------------------

create or replace function public.record_catalog_raw_records_batch_guarded(
  p_run_id uuid, p_worker_id text, p_attempt integer, p_lease_token text,
  p_records jsonb
) returns setof public.catalog_raw_records
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_record jsonb;
  v_row public.catalog_raw_records;
begin
  perform public.assert_worker_lease(p_run_id, p_worker_id, p_attempt, p_lease_token);
  if p_records is null or jsonb_typeof(p_records) <> 'array'
     or jsonb_array_length(p_records) not between 1 and 500 then
    raise exception 'invalid catalog raw record batch: 1 to 500 records' using errcode = '22023';
  end if;
  if (select count(distinct e->>'snapshot_id') from jsonb_array_elements(p_records) as e) <> 1
     or exists (select 1 from jsonb_array_elements(p_records) as e
                 where jsonb_typeof(e) <> 'object' or nullif(e->>'snapshot_id', '') is null) then
    raise exception 'invalid catalog raw record batch: one snapshot per batch' using errcode = '22023';
  end if;
  for v_record in
    select e from jsonb_array_elements(p_records) with ordinality as t(e, n) order by n
  loop
    select * into v_row from public.record_catalog_raw_record_guarded(
      p_run_id, p_worker_id, p_attempt, p_lease_token, v_record);
    return next v_row;
  end loop;
end;
$$;

create or replace function public.record_catalog_candidates_batch_guarded(
  p_run_id uuid, p_worker_id text, p_attempt integer, p_lease_token text,
  p_candidates jsonb
) returns setof public.catalog_candidate_variants
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_candidate jsonb;
  v_row public.catalog_candidate_variants;
  v_snapshot public.catalog_source_snapshots;
  v_snapshot_id uuid;
begin
  perform public.assert_worker_lease(p_run_id, p_worker_id, p_attempt, p_lease_token);
  if p_candidates is null or jsonb_typeof(p_candidates) <> 'array'
     or jsonb_array_length(p_candidates) not between 1 and 500 then
    raise exception 'invalid catalog candidate batch: 1 to 500 candidates' using errcode = '22023';
  end if;
  if (select count(distinct e->>'snapshot_id') from jsonb_array_elements(p_candidates) as e) <> 1
     or exists (select 1 from jsonb_array_elements(p_candidates) as e
                 where jsonb_typeof(e) <> 'object' or nullif(e->>'snapshot_id', '') is null) then
    raise exception 'invalid catalog candidate batch: one snapshot per batch' using errcode = '22023';
  end if;
  v_snapshot_id := (p_candidates->0->>'snapshot_id')::uuid;
  select * into v_snapshot from public.catalog_source_snapshots
    where id = v_snapshot_id for update;
  if v_snapshot.id is null then
    raise exception 'invalid catalog candidate batch snapshot' using errcode = '23503';
  end if;
  -- Stricter than the single-row RPC: an ingestion writes readings only onto
  -- its OWN capture, and only while that capture is still being written.
  if v_snapshot.created_by_run_id is distinct from p_run_id then
    raise exception 'this catalog snapshot does not belong to this run' using errcode = '22023';
  end if;
  if v_snapshot.validation_state = 'failed' then
    raise exception 'a failed catalog snapshot is terminal' using errcode = '22023';
  end if;
  if v_snapshot.activated_at is not null then
    raise exception 'an active catalog snapshot is immutable' using errcode = '22023';
  end if;
  for v_candidate in
    select e from jsonb_array_elements(p_candidates) with ordinality as t(e, n) order by n
  loop
    select * into v_row from public.record_catalog_candidate_guarded(
      p_run_id, p_worker_id, p_attempt, p_lease_token, v_candidate);
    return next v_row;
  end loop;
end;
$$;

-- ---------------------------------------------------------------------------
-- 5. RLS and privileges: service-path only.
-- ---------------------------------------------------------------------------
alter table public.catalog_snapshot_adoptions enable row level security;

do $$
declare fn text;
begin
  foreach fn in array array[
    'public.catalog_check_snapshot_adoption()',
    'public.forbid_catalog_snapshot_rewrite()',
    'public.adopt_catalog_snapshot_guarded(uuid,text,integer,text,jsonb)',
    'public.record_catalog_raw_records_batch_guarded(uuid,text,integer,text,jsonb)',
    'public.record_catalog_candidates_batch_guarded(uuid,text,integer,text,jsonb)'
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

  execute 'revoke all on table public.catalog_snapshot_adoptions from public';
  if exists (select 1 from pg_roles where rolname='anon') then
    execute 'revoke all on table public.catalog_snapshot_adoptions from anon';
  end if;
  if exists (select 1 from pg_roles where rolname='authenticated') then
    execute 'revoke all on table public.catalog_snapshot_adoptions from authenticated';
  end if;
  if exists (select 1 from pg_roles where rolname='service_role') then
    execute 'grant select, insert on table public.catalog_snapshot_adoptions to service_role';
    execute 'revoke update, delete on table public.catalog_snapshot_adoptions from service_role';
  end if;
end $$;

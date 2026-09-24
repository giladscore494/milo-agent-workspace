-- Catalog ingestion recovery: ONE snapshot write authority, the adoption of an
-- orphaned pending snapshot, and bounded batched writes.
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
-- run, and which every guarded write refused to touch for any other run. The
-- content was wedged.
--
-- Why ADOPTION rather than "mark it failed and allow a new snapshot"
-- ------------------------------------------------------------------
--
-- A second snapshot of the same content is impossible without weakening
-- identity: `catalog_source_snapshots_key_uidx` is unique on the
-- content-derived key and `catalog_source_snapshots_natural_uidx` on the
-- content digest. Marking the orphan `failed` would make that content
-- PERMANENTLY unactivatable -- `failed` is terminal. Adoption keeps every
-- identity rule and every immutability rule.
--
-- The write-authority contract
-- ----------------------------
--
-- `created_by_run_id` stays exactly what it was: IMMUTABLE provenance of the
-- run that opened the snapshot (`forbid_catalog_snapshot_rewrite` is not
-- touched). Who may WRITE a pending snapshot is a separate, derived fact:
--
--   the current writer = the adopter of the latest adoption row, or, when
--                        there is none, `created_by_run_id`.
--
-- `assert_snapshot_write_authority(snapshot, run)` is the ONE place that
-- decides it. It locks the snapshot FOR UPDATE, refuses a run that is not the
-- current writer with the SAME message every guarded write already raised
-- ('this catalog snapshot does not belong to this run'), and refuses a failed
-- or an active snapshot with the messages they already raised. The raw-record
-- write, activation (both its `complete` and its `failed` path) and the new
-- batch writes all call it; `scripts/check_migrations.py` refuses a direct
-- `created_by_run_id` ownership comparison anywhere else in this or a later
-- migration.
--
-- Adoption
-- --------
--
-- `catalog_snapshot_adoptions` is append-only: (snapshot, adoption_seq) and
-- (snapshot, adopter) are unique. A BEFORE-INSERT trigger -- which holds for
-- every writer, not only for the RPC -- numbers the row itself and admits it
-- only when ALL of these hold, under row locks:
--
--   * the snapshot is PENDING: not activated and not failed;
--   * `previous_writer_run_id` IS the current writer, and that run ended
--     `failed`, `cancelled` or `timed_out` and holds no live lease;
--   * the adopter is a live, leased `operator_capture` run.
--
-- `adopt_catalog_snapshot_guarded` asserts the lease, holds the caller to the
-- snapshot's identity, its declared capture scope and its normalization
-- summary, writes the adoption row AND a `catalog_snapshot_adopted` run event
-- in one transaction, and is idempotent for the current writer. The adopter
-- then continues through the existing idempotent writes, and activation still
-- passes the completeness gate.
--
-- Batches
-- -------
--
-- `record_catalog_raw_records_batch_guarded` and
-- `record_catalog_candidates_batch_guarded` take 1..500 rows of ONE snapshot
-- the caller may write, assert the lease and the write authority, and apply
-- the unchanged single-row RPC to each row in order inside one transaction:
-- the same validation and the same idempotency, by construction, and all or
-- nothing. Each answers `{rows, inserted, already_present}`.
--
-- Compatibility with the release that is still serving
-- ----------------------------------------------------
--
-- Every existing RPC keeps its signature, its messages and, for its existing
-- callers, its behaviour: for a snapshot nobody adopted the current writer IS
-- `created_by_run_id`, so the restated raw-record and activation writes accept
-- and refuse exactly what they did. `record_catalog_candidate_guarded` is not
-- touched (the promotion pipeline revises candidate status through it).
--
-- Additive and forward-only. Rerun-safe. service_role only.

-- ---------------------------------------------------------------------------
-- 1. The adoption record.
-- ---------------------------------------------------------------------------

create table if not exists public.catalog_snapshot_adoptions (
  snapshot_id uuid not null references public.catalog_source_snapshots(id) on delete restrict,
  adoption_seq integer not null,
  adopted_by_run_id uuid not null references public.runs(id) on delete restrict,
  previous_writer_run_id uuid not null references public.runs(id) on delete restrict,
  adopted_at timestamptz not null default now(),
  constraint catalog_snapshot_adoptions_seq_positive check (adoption_seq >= 1),
  constraint catalog_snapshot_adoptions_distinct_runs
    check (adopted_by_run_id <> previous_writer_run_id)
);

create unique index if not exists catalog_snapshot_adoptions_seq_uidx
  on public.catalog_snapshot_adoptions(snapshot_id, adoption_seq);
create unique index if not exists catalog_snapshot_adoptions_adopter_uidx
  on public.catalog_snapshot_adoptions(snapshot_id, adopted_by_run_id);
create index if not exists catalog_snapshot_adoptions_adopter_idx
  on public.catalog_snapshot_adoptions(adopted_by_run_id);
create index if not exists catalog_snapshot_adoptions_previous_idx
  on public.catalog_snapshot_adoptions(previous_writer_run_id);

-- The run that may write a snapshot now: the latest adopter, else its creator.
create or replace function public.catalog_snapshot_current_writer(p_snapshot_id uuid)
returns uuid
language sql
stable
set search_path = pg_catalog
as $$
  select coalesce(
    (select a.adopted_by_run_id from public.catalog_snapshot_adoptions a
      where a.snapshot_id = p_snapshot_id order by a.adoption_seq desc limit 1),
    (select s.created_by_run_id from public.catalog_source_snapshots s
      where s.id = p_snapshot_id))
$$;

-- ---------------------------------------------------------------------------
-- 2. THE write authority.
-- ---------------------------------------------------------------------------
--
-- `p_allow_decided` exists for activation alone: re-declaring the decision a
-- snapshot already carries is an idempotent replay there, so it needs the
-- authority check without the "still pending" check. Every other caller uses
-- the two-argument form.
create or replace function public.assert_snapshot_write_authority(
  p_snapshot_id uuid, p_run_id uuid, p_allow_decided boolean default false
) returns public.catalog_source_snapshots
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_snapshot public.catalog_source_snapshots;
  v_adopter uuid;
begin
  select * into v_snapshot from public.catalog_source_snapshots
    where id = p_snapshot_id for update;
  if v_snapshot.id is null then
    raise exception 'invalid catalog snapshot' using errcode = '23503';
  end if;
  select a.adopted_by_run_id into v_adopter from public.catalog_snapshot_adoptions a
    where a.snapshot_id = p_snapshot_id order by a.adoption_seq desc limit 1;
  -- A snapshot belongs to its CURRENT writer: the run that opened it, until
  -- an adoption hands it to another run.
  if (v_adopter is null and v_snapshot.created_by_run_id is distinct from p_run_id)
     or (v_adopter is not null and v_adopter is distinct from p_run_id) then
    raise exception 'this catalog snapshot does not belong to this run' using errcode = '22023';
  end if;
  if not coalesce(p_allow_decided, false) then
    -- Both decided states are terminal.
    if v_snapshot.validation_state = 'failed' then
      raise exception 'a failed catalog snapshot is terminal' using errcode = '22023';
    end if;
    if v_snapshot.activated_at is not null then
      raise exception 'an active catalog snapshot is immutable' using errcode = '22023';
    end if;
  end if;
  return v_snapshot;
end;
$$;

-- ---------------------------------------------------------------------------
-- 3. The adoption trigger: every condition, for every writer, under locks.
-- ---------------------------------------------------------------------------
create or replace function public.catalog_check_snapshot_adoption() returns trigger
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_previous public.runs;
  v_adopter public.runs;
  v_snapshot public.catalog_source_snapshots;
begin
  select * into v_previous from public.runs where id = new.previous_writer_run_id for share;
  select * into v_adopter from public.runs where id = new.adopted_by_run_id for share;
  if v_previous.id is null or v_adopter.id is null then
    raise exception 'CATALOG_SNAPSHOT_ADOPTION_REFUSED: unknown run' using errcode = '23503';
  end if;
  -- The adopter: an operator capture run, alive and leased.
  if v_adopter.run_identity->>'workflow_key' is distinct from 'operator_capture'
     or v_adopter.status not in ('starting', 'running')
     or v_adopter.lease_expires_at is null or v_adopter.lease_expires_at <= now() then
    raise exception 'CATALOG_SNAPSHOT_ADOPTION_REFUSED: only a live operator capture run adopts'
      using errcode = '22023';
  end if;
  -- The previous writer: finished unsuccessfully, and holding no live lease.
  if v_previous.status not in ('failed', 'cancelled', 'timed_out')
     or (v_previous.lease_expires_at is not null and v_previous.lease_expires_at > now()) then
    raise exception 'CATALOG_SNAPSHOT_ADOPTION_REFUSED: the writing run is live or did not fail'
      using errcode = '22023';
  end if;
  select * into v_snapshot from public.catalog_source_snapshots
    where id = new.snapshot_id for update;
  if v_snapshot.id is null then
    raise exception 'CATALOG_SNAPSHOT_ADOPTION_REFUSED: unknown snapshot' using errcode = '23503';
  end if;
  if public.catalog_snapshot_current_writer(v_snapshot.id)
       is distinct from new.previous_writer_run_id then
    raise exception 'CATALOG_SNAPSHOT_ADOPTION_REFUSED: that run is not the snapshot''s writer'
      using errcode = '22023';
  end if;
  -- Both decided states are terminal and never change writer.
  if v_snapshot.activated_at is not null or v_snapshot.validation_state <> 'pending' then
    raise exception 'CATALOG_SNAPSHOT_ADOPTION_REFUSED: only a pending snapshot is adopted'
      using errcode = '22023';
  end if;
  -- Numbered here, never by a caller: the next in the snapshot's sequence.
  select coalesce(max(a.adoption_seq), 0) + 1 into new.adoption_seq
    from public.catalog_snapshot_adoptions a where a.snapshot_id = new.snapshot_id;
  new.adopted_at := now();
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
-- 4. The existing writes, restated over the authority. Same signatures, same
--    messages, same behaviour for every snapshot nobody adopted.
-- ---------------------------------------------------------------------------

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
  -- verbatim from an upstream register, not backend-authored metadata.
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
  -- Only the snapshot's current writer appends, and only while it is pending.
  v_snapshot := public.assert_snapshot_write_authority(v_snapshot.id, p_run_id);
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

create or replace function public.activate_catalog_snapshot_guarded(
  p_run_id uuid, p_worker_id text, p_attempt integer, p_lease_token text,
  p_activation jsonb
) returns setof public.catalog_source_snapshots
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_snapshot public.catalog_source_snapshots;
  v_state text;
begin
  perform public.assert_worker_lease(p_run_id, p_worker_id, p_attempt, p_lease_token);
  v_state := coalesce(nullif(p_activation->>'validation_state', ''), 'complete');
  if v_state not in ('complete', 'failed') then
    raise exception 'invalid catalog snapshot validation state' using errcode = '22023';
  end if;
  select * into v_snapshot from public.catalog_source_snapshots
    where id = (p_activation->>'snapshot_id')::uuid for update;
  if v_snapshot.id is null then
    raise exception 'invalid catalog snapshot' using errcode = '23503';
  end if;
  -- Only the current writer decides the capture -- on the `complete` path,
  -- on the `failed` path and on an idempotent replay of either.
  v_snapshot := public.assert_snapshot_write_authority(v_snapshot.id, p_run_id, true);

  -- `failed` is TERMINAL. Re-declaring the same failure is an idempotent
  -- no-op; anything else is refused.
  if v_snapshot.validation_state = 'failed' then
    if v_state <> 'failed' then
      raise exception 'a failed catalog snapshot is terminal' using errcode = '22023';
    end if;
    return next v_snapshot;
    return;
  end if;
  -- An active snapshot is equally settled: replaying `complete` is a no-op.
  if v_snapshot.activated_at is not null then
    if v_state <> 'complete' then
      raise exception 'an active catalog snapshot is immutable' using errcode = '22023';
    end if;
    return next v_snapshot;
    return;
  end if;
  if v_state = 'failed' then
    update public.catalog_source_snapshots set validation_state = 'failed'
      where id = v_snapshot.id returning * into v_snapshot;
    return next v_snapshot;
    return;
  end if;
  -- The completeness gate.
  if v_snapshot.stored_record_count <> v_snapshot.declared_record_count then
    raise exception 'catalog snapshot is incomplete' using errcode = '22023';
  end if;
  update public.catalog_source_snapshots
    set validation_state = 'complete', activated_at = now()
    where id = v_snapshot.id returning * into v_snapshot;
  return next v_snapshot;
end;
$$;

-- ---------------------------------------------------------------------------
-- 5. The adoption RPC.
-- ---------------------------------------------------------------------------

create or replace function public.adopt_catalog_snapshot_guarded(
  p_run_id uuid, p_worker_id text, p_attempt integer, p_lease_token text,
  p_snapshot jsonb
) returns jsonb
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_row public.catalog_source_snapshots;
  v_adoption public.catalog_snapshot_adoptions;
  v_writer uuid;
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
  -- The same declared scope and the same content-derived reading of it.
  foreach v_field in array array['capture_scope', 'capture_contract', 'page_chain_sha256',
                                  'normalization_contract', 'normalized_record_count',
                                  'normalization_issue_count', 'normalization_issues',
                                  'normalization_issue_records'] loop
    if v_row.retrieval_metadata->v_field is distinct from v_metadata->v_field then
      raise exception 'CATALOG_SNAPSHOT_ADOPTION_REFUSED: the capture does not match the snapshot'
        using errcode = '22023';
    end if;
  end loop;

  v_writer := public.catalog_snapshot_current_writer(v_row.id);
  if v_writer = p_run_id then
    -- Already this run's to write (it adopted it, or it opened it): a replay.
    select * into v_adoption from public.catalog_snapshot_adoptions a
      where a.snapshot_id = v_row.id and a.adopted_by_run_id = p_run_id;
  else
    -- The trigger decides and numbers; its refusal is the answer.
    insert into public.catalog_snapshot_adoptions
      (snapshot_id, adoption_seq, adopted_by_run_id, previous_writer_run_id)
    values (v_row.id, 1, p_run_id, v_writer)
    returning * into v_adoption;
    -- Durable on the adopting run too, in the same transaction.
    insert into public.run_events (run_id, event_type, message, payload)
    values (p_run_id, 'catalog_snapshot_adopted', 'adopted an orphaned pending catalog snapshot',
            jsonb_build_object('snapshot_id', v_row.id, 'snapshot_key', v_row.snapshot_key,
                               'adoption_seq', v_adoption.adoption_seq,
                               'previous_writer_run_id', v_adoption.previous_writer_run_id,
                               'stored_record_count', v_row.stored_record_count,
                               'declared_record_count', v_row.declared_record_count));
  end if;
  return jsonb_build_object(
    'snapshot', to_jsonb(v_row),
    'adoption', case when v_adoption.snapshot_id is null then null else jsonb_build_object(
      'adoption_seq', v_adoption.adoption_seq,
      'adopted_by_run_id', v_adoption.adopted_by_run_id,
      'previous_writer_run_id', v_adoption.previous_writer_run_id,
      'adopted_at', v_adoption.adopted_at) end);
end;
$$;

-- ---------------------------------------------------------------------------
-- 6. Bounded batches over the unchanged single-row writes.
-- ---------------------------------------------------------------------------

create or replace function public.record_catalog_raw_records_batch_guarded(
  p_run_id uuid, p_worker_id text, p_attempt integer, p_lease_token text,
  p_records jsonb
) returns jsonb
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_record jsonb;
  v_row public.catalog_raw_records;
  v_snapshot_id uuid;
  v_rows jsonb := '[]'::jsonb;
  v_present integer := 0;
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
  v_snapshot_id := (p_records->0->>'snapshot_id')::uuid;
  perform public.assert_snapshot_write_authority(v_snapshot_id, p_run_id);
  for v_record in
    select e from jsonb_array_elements(p_records) with ordinality as t(e, n) order by n
  loop
    if exists (select 1 from public.catalog_raw_records r
                where r.snapshot_id = v_snapshot_id
                  and r.record_key = nullif(v_record->>'record_key', '')) then
      v_present := v_present + 1;
    end if;
    select * into v_row from public.record_catalog_raw_record_guarded(
      p_run_id, p_worker_id, p_attempt, p_lease_token, v_record);
    v_rows := v_rows || to_jsonb(v_row);
  end loop;
  return jsonb_build_object('rows', v_rows,
                            'inserted', jsonb_array_length(v_rows) - v_present,
                            'already_present', v_present);
end;
$$;

create or replace function public.record_catalog_candidates_batch_guarded(
  p_run_id uuid, p_worker_id text, p_attempt integer, p_lease_token text,
  p_candidates jsonb
) returns jsonb
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_candidate jsonb;
  v_row public.catalog_candidate_variants;
  v_snapshot_id uuid;
  v_rows jsonb := '[]'::jsonb;
  v_present integer := 0;
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
  -- Stricter than the single-row candidate write (which the promotion
  -- pipeline uses to revise a status): an ingestion writes readings only onto
  -- a capture it may write, and only while that capture is pending.
  perform public.assert_snapshot_write_authority(v_snapshot_id, p_run_id);
  for v_candidate in
    select e from jsonb_array_elements(p_candidates) with ordinality as t(e, n) order by n
  loop
    if exists (select 1 from public.catalog_candidate_variants c
                where c.snapshot_id = v_snapshot_id
                  and c.candidate_key = nullif(v_candidate->>'candidate_key', '')) then
      v_present := v_present + 1;
    end if;
    select * into v_row from public.record_catalog_candidate_guarded(
      p_run_id, p_worker_id, p_attempt, p_lease_token, v_candidate);
    v_rows := v_rows || to_jsonb(v_row);
  end loop;
  return jsonb_build_object('rows', v_rows,
                            'inserted', jsonb_array_length(v_rows) - v_present,
                            'already_present', v_present);
end;
$$;

-- ---------------------------------------------------------------------------
-- 7. RLS and privileges: service-path only.
-- ---------------------------------------------------------------------------
alter table public.catalog_snapshot_adoptions enable row level security;

do $$
declare fn text;
begin
  foreach fn in array array[
    'public.catalog_snapshot_current_writer(uuid)',
    'public.assert_snapshot_write_authority(uuid,uuid,boolean)',
    'public.catalog_check_snapshot_adoption()',
    'public.record_catalog_raw_record_guarded(uuid,text,integer,text,jsonb)',
    'public.activate_catalog_snapshot_guarded(uuid,text,integer,text,jsonb)',
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

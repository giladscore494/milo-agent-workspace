-- B2: durable bounded evidence fragments.
--
-- A source row is METADATA (url/title/domain/type/query).  Nothing durable
-- has ever held the actual source text a claim rests on, so a later grounded
-- verifier could only re-fetch the page or trust model memory.  This
-- migration adds the smallest durable relation that fixes that:
--
--   claim.source_id -> public.sources -> public.source_evidence_fragments
--
-- Why a NEW relation instead of a column on public.sources:
--
--   * public.sources is already part of an existing browser contract.  The
--     API writes a source and immediately appends a `source_recorded` run
--     event carrying the WHOLE source row (backend/main.py), and the browser
--     reconstructs sources from public.run_events, which is one of the eight
--     browser-visible tables.  A fragment_text column on public.sources
--     would therefore become browser payload the moment it was written.
--   * Fragments are verifier-internal evidence.  They live in a service-only
--     relation with RLS and zero policies, exactly like public.claims and
--     public.run_usage_ledger, and no run event ever carries fragment text.
--
-- Additive and forward-only: no existing table, column, RPC, row or index is
-- modified or rewritten.  Sources and claims written before this migration
-- stay exactly as they are and remain valid forever; a source with no
-- fragment is a source whose grounding context is simply unavailable, which
-- B5 must distinguish from a fabricated one.  Nothing is backfilled.

create table if not exists public.source_evidence_fragments (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references public.runs(id) on delete cascade,
  source_id uuid not null references public.sources(id) on delete restrict,
  task_key text not null,
  evidence_key text not null,
  fragment_text text not null,
  content_hash text not null,
  fragment_index integer not null default 0,
  created_at timestamptz not null default now(),
  -- The hard bounds live in the database too, so no backend release and no
  -- direct RPC call can ever store a full page.  These three numbers mirror
  -- MAX_FRAGMENT_CHARS / MAX_FRAGMENTS_PER_SOURCE /
  -- MAX_FRAGMENT_TOTAL_CHARS_PER_SOURCE in
  -- backend/engines/swarm_v2/fragments.py; tests/test_evidence_migration_static.py
  -- proves the two definitions cannot drift apart.
  constraint source_evidence_fragments_text_bounded
    check (char_length(fragment_text) between 1 and 400),
  constraint source_evidence_fragments_index_bounded
    check (fragment_index between 0 and 3),
  constraint source_evidence_fragments_hash_shape
    check (content_hash ~ '^[0-9a-f]{64}$')
);

-- Retry/resume identity: one logical fragment per run.  The backend derives
-- evidence_key from stable provenance only (source + task + the final bounded
-- text's content hash), so an exact replay collapses onto this row.
create unique index if not exists source_evidence_fragments_run_evidence_uidx
  on public.source_evidence_fragments(run_id, evidence_key);
-- The future verifier read path: fragments of the requested sources in a
-- deterministic order.
create index if not exists source_evidence_fragments_source_order_idx
  on public.source_evidence_fragments(source_id, fragment_index, content_hash);

alter table public.source_evidence_fragments enable row level security;
-- No policies: browser roles (PUBLIC / anon / authenticated) have no access
-- at all.  Only the trusted service path reads and appends.

-- Captured evidence is an audit record: once a fragment is durable it can
-- never be rewritten or silently removed, by any role, exactly like
-- public.run_usage_ledger (migration 013).
create or replace function public.forbid_evidence_fragment_mutation() returns trigger
language plpgsql as $$
begin
  raise exception 'source_evidence_fragments is append-only';
end;
$$;

drop trigger if exists source_evidence_fragments_append_only on public.source_evidence_fragments;
create trigger source_evidence_fragments_append_only
  before update or delete on public.source_evidence_fragments
  for each row execute function public.forbid_evidence_fragment_mutation();

-- The only write path: same lease-guarded, fail-closed posture as
-- upsert_source_guarded / create_claim_with_source_guarded /
-- create_conflict_guarded.
create or replace function public.record_evidence_fragment_guarded(
  p_run_id uuid, p_worker_id text, p_attempt integer, p_lease_token text,
  p_fragment jsonb
) returns setof public.source_evidence_fragments
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_row public.source_evidence_fragments;
  v_source public.sources;
  v_text text; v_hash text; v_index integer;
  v_count integer; v_total integer;
begin
  perform public.assert_worker_lease(p_run_id, p_worker_id, p_attempt, p_lease_token);
  if p_fragment::text ~* '"(chain_of_thought|provider_detail|raw_error|api_key|secret|password|authorization|credentials|exception|lease_token|token)"[[:space:]]*:'
     or lower(p_fragment::text) like '%secret sentinel%' then
    raise exception 'unsafe evidence payload rejected' using errcode = '22023';
  end if;
  if nullif(p_fragment->>'evidence_key', '') is null or nullif(p_fragment->>'task_key', '') is null then
    raise exception 'invalid evidence fragment: evidence_key and task_key are required' using errcode = '22023';
  end if;
  v_text := p_fragment->>'fragment_text';
  v_hash := p_fragment->>'content_hash';
  v_index := (p_fragment->>'fragment_index')::integer;
  if v_text is null or char_length(btrim(v_text)) = 0 then
    raise exception 'invalid evidence fragment: fragment_text must not be empty' using errcode = '22023';
  end if;
  if char_length(v_text) > 400 then
    raise exception 'invalid evidence fragment: fragment_text exceeds the durable bound' using errcode = '22023';
  end if;
  if v_index is null or v_index < 0 or v_index > 3 then
    raise exception 'invalid evidence fragment: fragment_index is outside the durable bound' using errcode = '22023';
  end if;
  -- A FINITE credential / hidden-reasoning marker set, mirroring
  -- _FRAGMENT_SECRET_MARKERS in backend/engines/swarm_v2/evidence.py.  It is
  -- deliberately mechanical: quoted source prose that merely reasons is
  -- legitimate evidence and is never rejected for its wording.
  if lower(v_text) ~ '(-----begin|-----end|api_key=|apikey=|aws_secret_access_key|authorization:|client_secret|lease_token|password=|private_key|refresh_token|secret_key|x-api-key|chain of thought|hidden reasoning|secret sentinel)' then
    raise exception 'unsafe evidence fragment rejected' using errcode = '22023';
  end if;
  -- PostgreSQL recomputes the content hash from the durable text, so the
  -- stored hash can never disagree with what is stored next to it.  SHA-256
  -- is an identity/deduplication device only; no similarity or embedding is
  -- involved anywhere in this path.
  if coalesce(v_hash, '') !~ '^[0-9a-f]{64}$'
     or encode(sha256(convert_to(v_text, 'UTF8')), 'hex') <> v_hash then
    raise exception 'invalid evidence fragment: content hash does not match the bounded text' using errcode = '22023';
  end if;

  -- Source-bound only: the fragment must attach to a real source of THIS run.
  select * into v_source from public.sources
    where id = (p_fragment->>'source_id')::uuid and run_id = p_run_id for key share;
  if v_source.id is null then
    raise exception 'invalid evidence fragment source' using errcode = '23503';
  end if;

  -- Exact replay: return the existing durable row instead of a duplicate, and
  -- never let one source's idempotency key be reused for another source.
  select * into v_row from public.source_evidence_fragments
    where run_id = p_run_id and evidence_key = p_fragment->>'evidence_key';
  if v_row.id is not null then
    if v_row.source_id <> v_source.id then
      raise exception 'idempotency key belongs to a different source' using errcode = '22023';
    end if;
    return next v_row;
    return;
  end if;

  -- Per-source hard limits: small, relevant verification context, never
  -- archival completeness.  Checked only for a genuinely new fragment, so a
  -- retry can always replay what is already durable.
  select count(*), coalesce(sum(char_length(fragment_text)), 0) into v_count, v_total
    from public.source_evidence_fragments where source_id = v_source.id;
  if v_count >= 4 then
    raise exception 'evidence fragment count limit reached for this source' using errcode = '22023';
  end if;
  if v_total + char_length(v_text) > 1200 then
    raise exception 'evidence fragment character budget exhausted for this source' using errcode = '22023';
  end if;

  insert into public.source_evidence_fragments
    (run_id, source_id, task_key, evidence_key, fragment_text, content_hash, fragment_index)
  values (p_run_id, v_source.id, p_fragment->>'task_key', p_fragment->>'evidence_key',
    v_text, v_hash, v_index)
  on conflict (run_id, evidence_key) do nothing
  returning * into v_row;
  if v_row is null then
    select * into v_row from public.source_evidence_fragments
      where run_id = p_run_id and evidence_key = p_fragment->>'evidence_key';
    if v_row.source_id <> v_source.id then
      raise exception 'idempotency key belongs to a different source' using errcode = '22023';
    end if;
  end if;
  return next v_row;
end;
$$;

-- Service-path-only posture, identical to every other evidence RPC.  The
-- table grants are explicit rather than relying on a point-in-time
-- `grant ... on all tables` or on default privileges (see migrations
-- 20260818000100/20260818000200 for why that class of gap must be closed in
-- the migration that introduces the object).
do $$
declare fn text;
begin
  foreach fn in array array[
    'public.record_evidence_fragment_guarded(uuid,text,integer,text,jsonb)'
  ] loop
    execute format('revoke execute on function %s from public', fn);
    if exists (select 1 from pg_roles where rolname='anon') then execute format('revoke execute on function %s from anon', fn); end if;
    if exists (select 1 from pg_roles where rolname='authenticated') then execute format('revoke execute on function %s from authenticated', fn); end if;
    if exists (select 1 from pg_roles where rolname='service_role') then execute format('grant execute on function %s to service_role', fn); end if;
  end loop;
  execute 'revoke all on table public.source_evidence_fragments from public';
  if exists (select 1 from pg_roles where rolname='anon') then
    execute 'revoke all on table public.source_evidence_fragments from anon';
  end if;
  if exists (select 1 from pg_roles where rolname='authenticated') then
    execute 'revoke all on table public.source_evidence_fragments from authenticated';
  end if;
  if exists (select 1 from pg_roles where rolname='service_role') then
    -- Read and append only: the append-only trigger already blocks rewrites,
    -- and the service path has no reason to hold UPDATE/DELETE either.
    execute 'grant select, insert on table public.source_evidence_fragments to service_role';
    execute 'revoke update, delete on table public.source_evidence_fragments from service_role';
  end if;
end $$;

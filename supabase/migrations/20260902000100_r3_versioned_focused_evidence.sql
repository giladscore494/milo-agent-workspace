-- R3: versioned sources, located fragments, located facts.
--
-- Until now durable evidence could say WHAT was quoted and WHEN we looked
-- (sources.retrieved_at, source_evidence_fragments.content_hash) but never
-- WHICH VERSION of the source was read, and never WHERE inside it the quoted
-- text came from.  Two consequences were unavoidable:
--
--   * evidence acquired from two different versions of the same source
--     collapsed onto one row, because the version was not part of identity;
--   * two identical sentences read from two different records (or two
--     different fields) shared one content hash, so the second one silently
--     disappeared into the first.
--
-- This migration adds the smallest durable columns that fix both, and moves
-- the validation of the new contract INTO the guarded RPCs so a direct RPC
-- call cannot bypass what the backend contracts enforce:
--
--   public.sources                    + source_version_kind, source_version_id
--   public.claims                     + evidence_locator
--   public.source_evidence_fragments  + fragment_type, locator_key
--
-- Additive, nullable and forward-only.  No existing table, row or index is
-- rewritten and nothing is backfilled: every source, claim and fragment
-- written before R3 keeps a NULL for the new columns, stays readable exactly
-- as it is, and is simply not complete R3 evidence.  The R3 requirements
-- ("a new R3 source without a version, or a fragment without a locator, is
-- rejected") are enforced on NEW R3 evidence only -- a fragment that carries
-- an R3 locator must be bound to a versioned source -- so historical records
-- can never be retro-invalidated.
--
-- The COMPLETE contract is validated here, not only its outline.  Three
-- helper functions hold one SQL definition each of the rules the Python
-- contracts apply (backend/engines/swarm_v2/evidence_bounds.py and
-- evidence_contracts.py; tests/test_evidence_migration_static.py pins the
-- expressions against the Python constants), and BOTH the guarded RPCs and
-- the table CHECK constraints call them, so a direct insert that bypasses the
-- RPC is held to exactly the same shape:
--
--   r3_source_version_valid  every version kind has its own identifier rule
--                            (content_sha256 is exactly 64 lowercase hex;
--                            git_commit 7-64 lowercase hex; dataset_version
--                            and document_revision a bounded token)
--   r3_canonical_locator     a locator is the CANONICAL rendering of one of
--                            the two closed shapes: a six-element JSON array
--                            [kind, record_id, field_path, section, start,
--                            end] whose elements satisfy that kind's bounds
--                            and whose compact re-rendering equals the stored
--                            text byte for byte.  Nothing is evaluated: the
--                            text is parsed, bounded, re-rendered and
--                            compared, never run.
--   r3_focus_valid           the fragment type is the one its locator kind
--                            allows: structured_projection <-> record_field,
--                            verbatim_excerpt <-> document_span
--
-- and a LOCATED CLAIM must be backed by focused evidence of its own source:
-- the claim's locator must already be a fragment locator of the same durable
-- source in the same run (the R3 write order persists fragments before
-- claims), so an arbitrary, missing or cross-source locator cannot be
-- attached to a versioned source.
--
-- Rerun-safe: every column, function and constraint is added or replaced
-- idempotently.  The three CHECK constraints are dropped and re-added by name
-- so a database that applied an earlier draft of this migration converges on
-- the current definition (every existing row holds NULL in the new columns,
-- so re-validation is trivially satisfied), and the service-only ACL of each
-- replaced or added function is re-asserted rather than assumed.
--
-- Every existing guarantee of the replaced functions is carried forward
-- verbatim: the worker lease assertion, the unsafe-payload rejection, the
-- required evidence/task keys, the trusted canonical scope identity and its
-- one sanctioned pre-canonical upgrade-on-replay
-- (20260828000100), the same-run source binding, the atomic
-- source_claim_links insert, the per-source fragment quota under the source
-- row lock, the hash recomputation and the single replay invariant
-- (20260828000200).

alter table public.sources add column if not exists source_version_kind text;
alter table public.sources add column if not exists source_version_id text;
alter table public.claims add column if not exists evidence_locator text;
alter table public.source_evidence_fragments add column if not exists fragment_type text;
alter table public.source_evidence_fragments add column if not exists locator_key text;

-- ONE SQL definition of each R3 shape rule.  These are pure functions of
-- their arguments (IMMUTABLE, so a CHECK constraint may call them); they
-- read no table and are the same expressions the Python contracts apply.
-- The regular expressions are written in the POSIX subset PostgreSQL's `~`
-- and Python's `re` read identically and are pinned against
-- backend/engines/swarm_v2/evidence_bounds.py by the static drift test.
create or replace function public.r3_source_version_valid(p_kind text, p_identifier text)
returns boolean
language sql
immutable
set search_path = pg_catalog
as $$
  -- Strictly boolean, never NULL: a CHECK constraint treats NULL as passing,
  -- so a helper that could yield NULL would silently admit the very rows it
  -- exists to refuse.
  select coalesce(p_kind is not null and p_identifier is not null and case p_kind
    when 'content_sha256' then p_identifier ~ '^[0-9a-f]{64}$'
    when 'git_commit' then p_identifier ~ '^[0-9a-f]{7,64}$'
    when 'dataset_version' then p_identifier ~ '^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$'
    when 'document_revision' then p_identifier ~ '^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$'
    else false
  end, false)
$$;

-- Returns the parsed locator when, and only when, the text is the canonical
-- rendering of a valid locator; NULL otherwise.  The canonical rendering is
-- the compact JSON the backend emits (no spaces, unicode verbatim), rebuilt
-- here element by element: record ids and path segments are restricted to a
-- safe ASCII alphabet so quoting them is exact, offsets are re-rendered as
-- integers (so `612.0` and `1e3` are non-canonical), and the section heading
-- is re-rendered with to_jsonb, whose text form matches Python's
-- json.dumps(ensure_ascii=False) for the printable, whitespace-normalized
-- text the backend allows there.
create or replace function public.r3_canonical_locator(p_locator text)
returns jsonb
language plpgsql
immutable
set search_path = pg_catalog
as $$
declare
  v jsonb; v_kind text; v_record text; v_path jsonb; v_section text;
  v_start numeric; v_end numeric; v_len integer; v_canonical text;
begin
  if p_locator is null or char_length(p_locator) < 1 or char_length(p_locator) > 800 then
    return null;
  end if;
  begin
    v := p_locator::jsonb;
  exception when others then
    return null;
  end;
  if jsonb_typeof(v) <> 'array' or jsonb_array_length(v) <> 6 then
    return null;
  end if;
  if jsonb_typeof(v->0) <> 'string' or jsonb_typeof(v->1) <> 'string'
     or jsonb_typeof(v->2) <> 'array'
     or jsonb_typeof(v->3) not in ('string', 'null')
     or jsonb_typeof(v->4) not in ('number', 'null')
     or jsonb_typeof(v->5) not in ('number', 'null') then
    return null;
  end if;
  v_kind := v->>0;
  v_record := v->>1;
  v_path := v->2;
  v_len := jsonb_array_length(v_path);
  if v_kind not in ('document_span', 'record_field') then
    return null;
  end if;
  if v_record !~ '^[A-Za-z0-9_][A-Za-z0-9_.:@-]{0,127}$' then
    return null;
  end if;
  -- Every path element is a LITERAL object key: no `$`, `*`, `[`, `?`, quote
  -- or `..` can pass, so JSONPath/filter/wildcard syntax is malformed here.
  if exists (select 1 from jsonb_array_elements(v_path) as element
             where jsonb_typeof(element) <> 'string'
                or (element #>> '{}') !~ '^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$') then
    return null;
  end if;
  if v_kind = 'record_field' then
    if v_len < 1 or v_len > 6 then
      return null;
    end if;
    if jsonb_typeof(v->3) <> 'null' or jsonb_typeof(v->4) <> 'null' or jsonb_typeof(v->5) <> 'null' then
      return null;
    end if;
  else
    if v_len <> 0 then
      return null;
    end if;
    if jsonb_typeof(v->4) <> 'number' or jsonb_typeof(v->5) <> 'number' then
      return null;
    end if;
    v_start := (v->>4)::numeric;
    v_end := (v->>5)::numeric;
    if v_start <> trunc(v_start) or v_end <> trunc(v_end) then
      return null;
    end if;
    -- A span is bounded exactly as a durable fragment is: it can never cover
    -- a page.
    if v_start < 0 or v_start >= v_end or v_end > 1000000 or v_end - v_start > 400 then
      return null;
    end if;
    if jsonb_typeof(v->3) = 'string' then
      v_section := v->>3;
      if char_length(v_section) < 1 or char_length(v_section) > 120
         or v_section ~ '[[:cntrl:]]'
         or v_section <> btrim(regexp_replace(v_section, '[[:space:]]+', ' ', 'g')) then
        return null;
      end if;
    end if;
  end if;
  v_canonical := '["' || v_kind || '","' || v_record || '",['
    || coalesce((select string_agg('"' || (segment.element #>> '{}') || '"', ',' order by segment.ordinality)
                 from jsonb_array_elements(v_path) with ordinality as segment(element, ordinality)), '')
    || '],'
    || case when jsonb_typeof(v->3) = 'null' then 'null' else to_jsonb(v_section)::text end
    || ','
    || case when jsonb_typeof(v->4) = 'null' then 'null' else trunc(v_start)::bigint::text end
    || ','
    || case when jsonb_typeof(v->5) = 'null' then 'null' else trunc(v_end)::bigint::text end
    || ']';
  if v_canonical <> p_locator then
    return null;
  end if;
  return v;
end;
$$;

-- The fragment type is a function of the locator shape, never a free choice.
create or replace function public.r3_focus_valid(p_fragment_type text, p_locator text)
returns boolean
language plpgsql
immutable
set search_path = pg_catalog
as $$
declare v_kind text;
begin
  if p_fragment_type is null or p_locator is null then
    return false;
  end if;
  v_kind := public.r3_canonical_locator(p_locator)->>0;
  -- A non-canonical locator has no kind.  Return FALSE explicitly: comparing
  -- NULL below would yield NULL, and a CHECK constraint treats NULL as
  -- passing -- exactly the bypass this helper exists to close.
  if v_kind is null then
    return false;
  end if;
  return (p_fragment_type = 'structured_projection' and v_kind = 'record_field')
      or (p_fragment_type = 'verbatim_excerpt' and v_kind = 'document_span');
end;
$$;

-- The same rules as table constraints, so the durable shape holds even for a
-- direct insert that bypasses the guarded RPC.  Dropped and re-added by name:
-- every existing row holds NULL in all five columns, so re-validation is
-- trivially satisfied, and a database that applied an earlier draft of this
-- migration converges on the current definition.
alter table public.sources drop constraint if exists sources_version_pairing;
alter table public.sources add constraint sources_version_pairing check (
  (source_version_kind is null) = (source_version_id is null)
  and (source_version_kind is null
       or public.r3_source_version_valid(source_version_kind, source_version_id)));
alter table public.claims drop constraint if exists claims_evidence_locator_bounded;
alter table public.claims drop constraint if exists claims_evidence_locator_canonical;
alter table public.claims add constraint claims_evidence_locator_canonical check (
  evidence_locator is null or public.r3_canonical_locator(evidence_locator) is not null);
alter table public.source_evidence_fragments drop constraint if exists source_evidence_fragments_focus_pairing;
alter table public.source_evidence_fragments add constraint source_evidence_fragments_focus_pairing check (
  (fragment_type is null) = (locator_key is null)
  and (fragment_type is null or public.r3_focus_valid(fragment_type, locator_key)));

-- Source persistence: unchanged guarantees plus the optional trusted version.
create or replace function public.upsert_source_guarded(
  p_run_id uuid, p_worker_id text, p_attempt integer, p_lease_token text,
  p_source jsonb
) returns setof public.sources
language plpgsql
set search_path = pg_catalog
as $$
declare v_row public.sources; v_kind text; v_identifier text;
begin
  perform public.assert_worker_lease(p_run_id, p_worker_id, p_attempt, p_lease_token);
  if p_source::text ~* '"(chain_of_thought|provider_detail|raw_error|api_key|secret|password|authorization|credentials|exception|lease_token|token)"[[:space:]]*:'
     or lower(p_source::text) like '%secret sentinel%' then
    raise exception 'unsafe evidence payload rejected' using errcode = '22023';
  end if;
  if nullif(p_source->>'evidence_key', '') is null or nullif(p_source->>'task_key', '') is null then
    raise exception 'invalid source: evidence_key and task_key are required' using errcode = '22023';
  end if;
  -- The source version is all-or-nothing and comes from the trusted adapter.
  -- A half-specified version is a caller bug, never something to guess at.
  v_kind := nullif(p_source->>'source_version_kind', '');
  v_identifier := nullif(p_source->>'source_version_id', '');
  if (v_kind is null) <> (v_identifier is null) then
    raise exception 'invalid source: a source version requires both a kind and an identifier' using errcode = '22023';
  end if;
  -- Kind-specific: a content_sha256 IS 64 lowercase hex characters, a
  -- git_commit IS 7-64 of them, and the token kinds are bounded tokens.
  if v_kind is not null and not public.r3_source_version_valid(v_kind, v_identifier) then
    raise exception 'invalid source: unknown or malformed source version' using errcode = '22023';
  end if;
  insert into public.sources
    (run_id, agent, url, title, domain, source_type, source_strength, source_date,
     retrieved_at, query, tool_operation, evidence_key, task_key,
     source_version_kind, source_version_id)
  values (p_run_id, p_source->>'agent', p_source->>'url', p_source->>'title',
    p_source->>'domain', p_source->>'source_type', p_source->>'source_strength',
    p_source->>'source_date', coalesce((p_source->>'retrieved_at')::timestamptz, now()),
    p_source->>'query', p_source->>'tool_operation', p_source->>'evidence_key', p_source->>'task_key',
    v_kind, v_identifier)
  on conflict (run_id, evidence_key) where evidence_key is not null do nothing
  returning * into v_row;
  if v_row is null then
    select * into v_row from public.sources
      where run_id = p_run_id and evidence_key = p_source->>'evidence_key';
    -- An evidence_key may only be replayed for the SAME version.  The backend
    -- already folds the version into the key, so reaching this means a caller
    -- reused a key across versions; merging them would erase the distinction
    -- between two versions of one source.
    if v_row.source_version_kind is distinct from v_kind
       or v_row.source_version_id is distinct from v_identifier then
      raise exception 'source version identity conflict' using errcode = '22023';
    end if;
  end if;
  return next v_row;
end;
$$;

-- Claim persistence: unchanged guarantees (lease, unsafe payloads, evidence
-- and task keys, trusted canonical scope identity plus its single sanctioned
-- pre-canonical upgrade-on-replay, same-run source, atomic link) plus the
-- optional evidence locator and the unit rule that comes with it.
create or replace function public.create_claim_with_source_guarded(
  p_run_id uuid, p_worker_id text, p_attempt integer, p_lease_token text,
  p_claim jsonb
) returns setof public.claims
language plpgsql
set search_path = pg_catalog
as $$
declare v_row public.claims; v_source public.sources; v_locator text;
begin
  perform public.assert_worker_lease(p_run_id, p_worker_id, p_attempt, p_lease_token);
  if p_claim::text ~* '"(chain_of_thought|provider_detail|raw_error|api_key|secret|password|authorization|credentials|exception|lease_token|token)"[[:space:]]*:'
     or lower(p_claim::text) like '%secret sentinel%' then
    raise exception 'unsafe evidence payload rejected' using errcode = '22023';
  end if;
  if nullif(p_claim->>'evidence_key', '') is null or nullif(p_claim->>'task_key', '') is null then
    raise exception 'invalid claim: evidence_key and task_key are required' using errcode = '22023';
  end if;
  if coalesce(p_claim->>'canonical_scope_hash', '') !~ '^[0-9a-f]{64}$'
     or coalesce(p_claim->>'scope_normalization_version', '') !~ '^[1-9][0-9]{0,3}$' then
    raise exception 'invalid claim: trusted canonical scope identity is required' using errcode = '22023';
  end if;
  v_locator := nullif(p_claim->>'evidence_locator', '');
  if v_locator is not null then
    if char_length(v_locator) > 800 then
      raise exception 'invalid claim: evidence locator exceeds the durable bound' using errcode = '22023';
    end if;
    -- The locator must be the canonical rendering of one of the two closed
    -- locator shapes; an arbitrary string is not a location.
    if public.r3_canonical_locator(v_locator) is null then
      raise exception 'invalid claim: evidence locator is not a canonical bounded location' using errcode = '22023';
    end if;
    -- An R3-qualified fact states its unit.  "1798" is not a fact until it
    -- says cc.  Gated on the locator so no legacy unit-less claim is ever
    -- retro-invalidated; R3 carries the unit and never interprets it.
    if jsonb_typeof(p_claim->'value') = 'number' and nullif(p_claim->>'unit', '') is null then
      raise exception 'invalid claim: a numeric located fact requires an explicit unit' using errcode = '22023';
    end if;
  end if;
  select * into v_source from public.sources
    where id = (p_claim->>'source_id')::uuid and run_id = p_run_id for key share;
  if v_source.id is null then
    raise exception 'invalid claim source' using errcode = '23503';
  end if;
  if v_locator is not null and v_source.source_version_kind is null then
    -- R3 evidence may only rest on a source whose version was captured.
    raise exception 'invalid claim: a located fact requires a versioned source' using errcode = '22023';
  end if;
  -- A located fact is only a fact because focused evidence at that exact
  -- location supports it: the locator must already be a fragment locator of
  -- THIS durable source in THIS run.  A locator that exists only on another
  -- source, only in another run, or nowhere at all cannot be attached here.
  if v_locator is not null and not exists (
       select 1 from public.source_evidence_fragments f
        where f.run_id = p_run_id and f.source_id = v_source.id and f.locator_key = v_locator) then
    raise exception 'invalid claim: a located fact must be backed by focused evidence of its own source' using errcode = '22023';
  end if;
  insert into public.claims
    (run_id, entity_key, field_key, value, unit, time_scope, geography, market,
     source_id, source_strength, confidence, agent, status, evidence_key, task_key,
     canonical_scope_hash, scope_normalization_version, evidence_locator)
  values (p_run_id, p_claim->>'entity_key', p_claim->>'field_key', p_claim->'value',
    p_claim->>'unit', coalesce(p_claim->'time_scope', '{}'::jsonb), p_claim->>'geography',
    p_claim->>'market', v_source.id, p_claim->>'source_strength',
    (p_claim->>'confidence')::numeric, p_claim->>'agent',
    coalesce(p_claim->>'status', 'active'), p_claim->>'evidence_key', p_claim->>'task_key',
    p_claim->>'canonical_scope_hash', (p_claim->>'scope_normalization_version')::integer,
    v_locator)
  on conflict (run_id, evidence_key) where evidence_key is not null do nothing
  returning * into v_row;
  if v_row is null then
    select * into v_row from public.claims
      where run_id = p_run_id and evidence_key = p_claim->>'evidence_key'
      for update;
    if v_row.source_id <> v_source.id then
      raise exception 'idempotency key belongs to a different source' using errcode = '22023';
    end if;
    -- The locator is part of a claim's provenance, so a replay may never
    -- move a stored fact to a different record or field.
    if v_row.evidence_locator is distinct from v_locator then
      raise exception 'claim evidence locator mismatch' using errcode = '22023';
    end if;
    if v_row.canonical_scope_hash is not null and v_row.scope_normalization_version is not null then
      if v_row.canonical_scope_hash is distinct from p_claim->>'canonical_scope_hash'
         or v_row.scope_normalization_version is distinct from (p_claim->>'scope_normalization_version')::integer then
        raise exception 'claim canonical scope identity mismatch' using errcode = '22023';
      end if;
    elsif v_row.canonical_scope_hash is null and v_row.scope_normalization_version is null then
      if v_row.entity_key is distinct from p_claim->>'entity_key'
         or v_row.field_key is distinct from p_claim->>'field_key'
         or v_row.value is distinct from p_claim->'value'
         or v_row.unit is distinct from p_claim->>'unit'
         or v_row.time_scope is distinct from coalesce(p_claim->'time_scope', '{}'::jsonb)
         or v_row.geography is distinct from p_claim->>'geography'
         or v_row.market is distinct from p_claim->>'market'
         or v_row.source_strength is distinct from p_claim->>'source_strength'
         or v_row.confidence is distinct from (p_claim->>'confidence')::numeric
         or v_row.agent is distinct from p_claim->>'agent'
         or v_row.status is distinct from coalesce(p_claim->>'status', 'active')
         or v_row.task_key is distinct from p_claim->>'task_key' then
        raise exception 'idempotent claim replay does not match the stored claim' using errcode = '22023';
      end if;
      update public.claims
        set canonical_scope_hash = p_claim->>'canonical_scope_hash',
            scope_normalization_version = (p_claim->>'scope_normalization_version')::integer
        where id = v_row.id
        returning * into v_row;
    else
      raise exception 'claim canonical scope state is invalid' using errcode = '22023';
    end if;
  end if;
  insert into public.source_claim_links(source_id, claim_id)
    values (v_source.id, v_row.id) on conflict do nothing;
  return next v_row;
end;
$$;

-- Fragment persistence: unchanged guarantees (lease, unsafe payloads, empty
-- and oversized text, index bound, hash recomputation, source binding under
-- FOR UPDATE, task lineage, replay-before-quota, the single replay
-- invariant) plus the optional focused-evidence provenance.
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
  v_key text; v_task text; v_type text; v_locator text;
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
  v_key := p_fragment->>'evidence_key';
  v_task := p_fragment->>'task_key';
  v_text := p_fragment->>'fragment_text';
  v_hash := p_fragment->>'content_hash';
  v_index := (p_fragment->>'fragment_index')::integer;
  v_type := nullif(p_fragment->>'fragment_type', '');
  v_locator := nullif(p_fragment->>'locator_key', '');
  if v_text is null or char_length(btrim(v_text)) = 0 then
    raise exception 'invalid evidence fragment: fragment_text must not be empty' using errcode = '22023';
  end if;
  if char_length(v_text) > 400 then
    raise exception 'invalid evidence fragment: fragment_text exceeds the durable bound' using errcode = '22023';
  end if;
  if v_index is null or v_index < 0 or v_index > 3 then
    raise exception 'invalid evidence fragment: fragment_index is outside the durable bound' using errcode = '22023';
  end if;
  -- R3 focus provenance is all-or-nothing: a focused fragment states BOTH
  -- what kind of evidence it is and exactly where it came from.  A legacy
  -- fragment states neither and stays valid forever.
  if (v_type is null) <> (v_locator is null) then
    raise exception 'invalid evidence fragment: a focused fragment requires both a type and a locator' using errcode = '22023';
  end if;
  if v_type is not null and (
       v_type not in ('structured_projection', 'verbatim_excerpt')
       or char_length(v_locator) > 800) then
    raise exception 'invalid evidence fragment: unknown fragment type or oversized locator' using errcode = '22023';
  end if;
  if v_type is not null then
    -- The locator must be the canonical rendering of one of the two closed
    -- locator shapes, and the fragment type must be the one that shape allows.
    if public.r3_canonical_locator(v_locator) is null then
      raise exception 'invalid evidence fragment: locator is not a canonical bounded location' using errcode = '22023';
    end if;
    if not public.r3_focus_valid(v_type, v_locator) then
      raise exception 'invalid evidence fragment: fragment type does not match the locator kind' using errcode = '22023';
    end if;
  end if;
  if lower(v_text) ~ '(-----begin|-----end|api_key=|apikey=|aws_secret_access_key|authorization:|client_secret|lease_token|password=|private_key|refresh_token|secret_key|x-api-key|chain of thought|hidden reasoning|secret sentinel)' then
    raise exception 'unsafe evidence fragment rejected' using errcode = '22023';
  end if;
  if coalesce(v_hash, '') !~ '^[0-9a-f]{64}$'
     or encode(sha256(convert_to(v_text, 'UTF8')), 'hex') <> v_hash then
    raise exception 'invalid evidence fragment: content hash does not match the bounded text' using errcode = '22023';
  end if;

  select * into v_source from public.sources
    where id = (p_fragment->>'source_id')::uuid and run_id = p_run_id for update;
  if v_source.id is null then
    raise exception 'invalid evidence fragment source' using errcode = '23503';
  end if;

  if v_source.task_key is distinct from v_task then
    raise exception 'evidence fragment task provenance mismatch' using errcode = '22023';
  end if;

  -- A located fragment is R3 evidence, and R3 evidence may only attach to a
  -- source whose version was captured: "a new R3 source without a version, or
  -- a fragment without a locator, is rejected".
  if v_locator is not null and v_source.source_version_kind is null then
    raise exception 'invalid evidence fragment: a focused fragment requires a versioned source' using errcode = '22023';
  end if;

  select * into v_row from public.source_evidence_fragments
    where run_id = p_run_id and evidence_key = v_key;

  if v_row.id is null then
    select count(*), coalesce(sum(char_length(fragment_text)), 0) into v_count, v_total
      from public.source_evidence_fragments where source_id = v_source.id;
    if v_count >= 4 then
      raise exception 'evidence fragment count limit reached for this source' using errcode = '22023';
    end if;
    if v_total + char_length(v_text) > 1200 then
      raise exception 'evidence fragment character budget exhausted for this source' using errcode = '22023';
    end if;

    insert into public.source_evidence_fragments
      (run_id, source_id, task_key, evidence_key, fragment_text, content_hash, fragment_index,
       fragment_type, locator_key)
    values (p_run_id, v_source.id, v_task, v_key, v_text, v_hash, v_index, v_type, v_locator)
    on conflict (run_id, evidence_key) do nothing
    returning * into v_row;
    if v_row.id is not null then
      return next v_row;
      return;
    end if;
    select * into v_row from public.source_evidence_fragments
      where run_id = p_run_id and evidence_key = v_key;
  end if;

  -- ONE replay invariant, now including the focus provenance: an existing row
  -- may only be returned when it is the SAME logical fragment, read from the
  -- same place, in the same form.  fragment_index stays outside the identity
  -- exactly as before, and a replay never rewrites it.
  if v_row.source_id is distinct from v_source.id
     or v_row.task_key is distinct from v_task
     or v_row.fragment_text is distinct from v_text
     or v_row.content_hash is distinct from v_hash
     or v_row.fragment_type is distinct from v_type
     or v_row.locator_key is distinct from v_locator then
    raise exception 'evidence fragment idempotency conflict' using errcode = '22023';
  end if;
  return next v_row;
end;
$$;

-- CREATE OR REPLACE preserves existing grants, but the service-only ACL for
-- the replaced worker-path functions is re-asserted explicitly rather than
-- assumed (see 20260818000100/20260818000200 for why that class of gap must
-- be closed in the migration that touches the object).
do $$
declare fn text;
begin
  foreach fn in array array[
    'public.r3_source_version_valid(text,text)',
    'public.r3_canonical_locator(text)',
    'public.r3_focus_valid(text,text)',
    'public.upsert_source_guarded(uuid,text,integer,text,jsonb)',
    'public.create_claim_with_source_guarded(uuid,text,integer,text,jsonb)',
    'public.record_evidence_fragment_guarded(uuid,text,integer,text,jsonb)'
  ] loop
    execute format('revoke execute on function %s from public', fn);
    if exists (select 1 from pg_roles where rolname='anon') then execute format('revoke execute on function %s from anon', fn); end if;
    if exists (select 1 from pg_roles where rolname='authenticated') then execute format('revoke execute on function %s from authenticated', fn); end if;
    if exists (select 1 from pg_roles where rolname='service_role') then execute format('grant execute on function %s to service_role', fn); end if;
  end loop;
  -- The fragment relation keeps its service-only, read-and-append posture:
  -- the new columns are internal evidence provenance and never browser payload.
  execute 'revoke all on table public.source_evidence_fragments from public';
  if exists (select 1 from pg_roles where rolname='anon') then
    execute 'revoke all on table public.source_evidence_fragments from anon';
  end if;
  if exists (select 1 from pg_roles where rolname='authenticated') then
    execute 'revoke all on table public.source_evidence_fragments from authenticated';
  end if;
  if exists (select 1 from pg_roles where rolname='service_role') then
    execute 'grant select, insert on table public.source_evidence_fragments to service_role';
    execute 'revoke update, delete on table public.source_evidence_fragments from service_role';
  end if;
end $$;

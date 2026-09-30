-- PR-D3: manufacturer name normalisation (owner decisions 2, 14, 15, 33).
--
-- The register's own `tozar` strings never change (decision 2). A
-- normalisation maps each EXACT source tozar to a canonical manufacturer:
--
--   * catalog_manufacturer_normalization_versions / _entries: append-only.
--     A version is the WHOLE active mapping (the previous version's entries
--     carried forward, plus what the owner approved); the newest version is
--     the active one. Every entry keeps its provenance: a code-owned
--     deterministic rule id, or the model proposal it came from (with its
--     confidence) -- and who approved it, when. Nothing is active until the
--     owner approves (decision 15).
--   * catalog_manufacturer_normalization_proposals: the ONE guarded model
--     call's input (every source name still unmapped, with its row count,
--     tozeret_nm and up to three sample kinuy_mishari) and its validated
--     output (groups: canonical name, members, confidence high|low, reason).
--     A proposal is requested from the website (decision 14) and executed by
--     the EXISTING capture job under an operator capture run -- the lease and
--     budget anchor the gateway's per-run and daily caps need; never a product
--     run. Its request / trigger / liveness record is a register capture
--     group of the new kind 'normalisation' (one live at a time).
--   * record_manufacturer_normalization_proposal validates the output AGAIN
--     (every member is an input name, no name in two groups, a closed
--     confidence, bounded text): the database never holds an invented name.
--   * approve_manufacturer_normalization: a compare-and-set on the active
--     version; a model entry must be exactly a group of a `proposed`
--     proposal, a rule entry must name a code-owned rule, and every source
--     name must be in the latest register directory.
--
-- Filters and "Add to plan" keep working on the exact source values: no
-- existing table, function or Mapping Plan contract is changed beyond the
-- group kind. Forward-only and rerun-safe.

-- ---------------------------------------------------------------------------
-- 1. A register capture group may be a normalisation request.
-- ---------------------------------------------------------------------------
alter table public.catalog_register_capture_groups
  drop constraint if exists catalog_register_capture_groups_kind_check;
alter table public.catalog_register_capture_groups
  add constraint catalog_register_capture_groups_kind_check
    check (kind in ('capture', 'directory', 'normalisation'));
alter table public.catalog_register_capture_groups
  drop constraint if exists catalog_register_capture_groups_check;
alter table public.catalog_register_capture_groups
  add constraint catalog_register_capture_groups_check
    check ((kind = 'capture' and register_version is not null and register_version ~ '^[0-9a-f]{64}$')
           or (kind in ('directory', 'normalisation') and register_version is null));

-- ---------------------------------------------------------------------------
-- 2. Tables.
-- ---------------------------------------------------------------------------
create table if not exists public.catalog_manufacturer_normalization_proposals (
  id uuid primary key default gen_random_uuid(),
  group_id uuid not null unique references public.catalog_register_capture_groups(id) on delete restrict,
  requested_by uuid not null,
  -- [{"name": <exact tozar>, "rows": n, "tozeret_nm": [..<=3], "samples": [..<=3]}]
  input jsonb not null check (jsonb_typeof(input) = 'array' and jsonb_array_length(input) between 1 and 2000
                              and char_length(input::text) <= 1000000),
  input_sha256 text not null check (input_sha256 ~ '^[0-9a-f]{64}$'),
  status text not null default 'requested' check (status in ('requested', 'proposed', 'refused')),
  groups jsonb check (groups is null or (jsonb_typeof(groups) = 'array' and char_length(groups::text) <= 1000000)),
  reason_code text check (reason_code is null or reason_code ~ '^[A-Z][A-Z0-9_]{2,79}$'),
  model text check (model is null or model ~ '^[a-z0-9][a-z0-9.-]{0,63}$'),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check ((status = 'proposed') = (groups is not null)),
  check ((status = 'refused') = (reason_code is not null))
);
create index if not exists catalog_manufacturer_normalization_proposals_created_idx
  on public.catalog_manufacturer_normalization_proposals (created_at desc);

create table if not exists public.catalog_manufacturer_normalization_versions (
  id uuid primary key default gen_random_uuid(),
  version integer not null unique check (version >= 1),
  approved_by uuid not null,
  entry_count integer not null check (entry_count >= 0),
  created_at timestamptz not null default now()
);

create table if not exists public.catalog_manufacturer_normalization_entries (
  version_id uuid not null references public.catalog_manufacturer_normalization_versions(id) on delete restrict,
  source_tozar text not null check (char_length(source_tozar) between 1 and 200),
  canonical_name text not null
    check (char_length(canonical_name) between 1 and 120 and btrim(canonical_name) = canonical_name),
  provenance text not null check (provenance in ('rule', 'model')),
  rule_id text check (rule_id is null or rule_id in ('R1_SPELLING', 'R2_TOZERET_CD')),
  proposal_id uuid references public.catalog_manufacturer_normalization_proposals(id) on delete restrict,
  confidence text check (confidence is null or confidence in ('high', 'low')),
  approved_by uuid not null,
  approved_at timestamptz not null,
  primary key (version_id, source_tozar),
  check ((provenance = 'rule' and rule_id is not null and proposal_id is null)
         or (provenance = 'model' and proposal_id is not null and rule_id is null and confidence is not null))
);
create index if not exists catalog_manufacturer_normalization_entries_proposal_idx
  on public.catalog_manufacturer_normalization_entries (proposal_id);

drop trigger if exists catalog_manufacturer_normalization_versions_append_only
  on public.catalog_manufacturer_normalization_versions;
create trigger catalog_manufacturer_normalization_versions_append_only
  before update or delete on public.catalog_manufacturer_normalization_versions
  for each row execute function public.forbid_catalog_register_rewrite();
drop trigger if exists catalog_manufacturer_normalization_entries_append_only
  on public.catalog_manufacturer_normalization_entries;
create trigger catalog_manufacturer_normalization_entries_append_only
  before update or delete on public.catalog_manufacturer_normalization_entries
  for each row execute function public.forbid_catalog_register_rewrite();

-- ---------------------------------------------------------------------------
-- 3. The model output contract, checked in the database too.
-- ---------------------------------------------------------------------------
-- [{"canonical": text, "members": [input names], "confidence": "high"|"low",
--   "reason": text}], every member an input name, none in two groups.
create or replace function public.catalog_normalization_groups_valid(p_groups jsonb, p_input jsonb)
returns boolean
language sql
immutable
set search_path = pg_catalog
as $$
  select coalesce(
    jsonb_typeof(p_groups) = 'array'
    and not exists (
      select 1 from jsonb_array_elements(p_groups) g
       where jsonb_typeof(g) <> 'object'
          or (select array_agg(k order by k) from jsonb_object_keys(g) k)
             is distinct from array['canonical', 'confidence', 'members', 'reason']
          or jsonb_typeof(g->'canonical') <> 'string'
          or char_length(g->>'canonical') not between 1 and 120 or btrim(g->>'canonical') <> g->>'canonical'
          or g->>'confidence' not in ('high', 'low')
          or jsonb_typeof(g->'reason') <> 'string' or char_length(g->>'reason') > 300
          or jsonb_typeof(g->'members') <> 'array' or jsonb_array_length(g->'members') < 1
          or exists (select 1 from jsonb_array_elements(g->'members') m
                      where jsonb_typeof(m) <> 'string'
                         or not exists (select 1 from jsonb_array_elements(p_input) i
                                         where i->>'name' = m #>> '{}')))
    and (select count(*) = count(distinct m #>> '{}')
           from jsonb_array_elements(p_groups) g, jsonb_array_elements(g->'members') m),
    false)
$$;

-- ---------------------------------------------------------------------------
-- 4. Request (the website), record (the capture job), approve (the owner).
-- ---------------------------------------------------------------------------

-- One live normalisation at a time (the group liveness rule, as a directory
-- refresh). A live one answers `existing` and writes nothing.
create or replace function public.request_manufacturer_normalization(
  p_requested_by uuid, p_grace_seconds integer, p_input jsonb
) returns jsonb
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_group public.catalog_register_capture_groups%rowtype;
  v_proposal public.catalog_manufacturer_normalization_proposals%rowtype;
begin
  if p_requested_by is null or p_grace_seconds is null or p_grace_seconds not between 300 and 86400
     or p_input is null or jsonb_typeof(p_input) <> 'array' or jsonb_array_length(p_input) not between 1 and 2000
     or exists (select 1 from jsonb_array_elements(p_input) i
                 where jsonb_typeof(i) <> 'object' or jsonb_typeof(i->'name') <> 'string')
     or (select count(*) <> count(distinct i->>'name') from jsonb_array_elements(p_input) i) then
    raise exception 'CATALOG_NORMALIZATION_REQUEST_INVALID: invalid normalisation request' using errcode = '22023';
  end if;
  perform pg_advisory_xact_lock(hashtext('public.catalog_manufacturer_normalization'));
  select * into v_group from public.catalog_register_capture_groups
   where kind = 'normalisation' order by claimed_at desc, id desc limit 1;
  if found and not public.catalog_register_group_stale(v_group.id, make_interval(secs => p_grace_seconds)) then
    select * into v_proposal from public.catalog_manufacturer_normalization_proposals where group_id = v_group.id;
    return jsonb_build_object('decision', 'existing', 'group', to_jsonb(v_group),
                              'proposal', to_jsonb(v_proposal) - 'input');
  end if;
  insert into public.catalog_register_capture_groups (kind, register_version, requested_by, expected_rows)
  values ('normalisation', null, p_requested_by, 0)
  returning * into v_group;
  insert into public.catalog_manufacturer_normalization_proposals (group_id, requested_by, input, input_sha256)
  values (v_group.id, p_requested_by, p_input, encode(sha256(convert_to(p_input::text, 'UTF8')), 'hex'))
  returning * into v_proposal;
  return jsonb_build_object('decision', 'claimed', 'group', to_jsonb(v_group),
                            'proposal', to_jsonb(v_proposal) - 'input');
end;
$$;

-- The capture job's outcome, once, under its run's lease: `proposed` with the
-- validated groups, or `refused` with a static code.
create or replace function public.record_manufacturer_normalization_proposal(
  p_run_id uuid, p_worker_id text, p_attempt integer, p_lease_token text,
  p_proposal_id uuid, p_status text, p_groups jsonb, p_reason_code text, p_model text
) returns jsonb
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_proposal public.catalog_manufacturer_normalization_proposals%rowtype;
begin
  perform public.assert_worker_lease(p_run_id, p_worker_id, p_attempt, p_lease_token);
  select p.* into v_proposal from public.catalog_manufacturer_normalization_proposals p
    join public.catalog_register_capture_groups g on g.id = p.group_id
   where p.id = p_proposal_id and g.run_id = p_run_id
   for update of p;
  if not found then
    raise exception 'CATALOG_NORMALIZATION_NOT_THIS_RUN: that proposal is not this run''s' using errcode = '42501';
  end if;
  if v_proposal.status <> 'requested' then
    if v_proposal.status = p_status and v_proposal.groups is not distinct from p_groups
       and v_proposal.reason_code is not distinct from p_reason_code then
      return to_jsonb(v_proposal) - 'input';
    end if;
    raise exception 'CATALOG_NORMALIZATION_ALREADY_RECORDED: that proposal already has its outcome'
      using errcode = '40001';
  end if;
  if p_status not in ('proposed', 'refused')
     or (p_status = 'proposed' and (p_reason_code is not null
                                    or not public.catalog_normalization_groups_valid(p_groups, v_proposal.input)))
     or (p_status = 'refused' and (p_groups is not null or p_reason_code is null)) then
    raise exception 'CATALOG_NORMALIZATION_OUTPUT_INVALID: the proposal breaks the output contract'
      using errcode = '22023';
  end if;
  update public.catalog_manufacturer_normalization_proposals
     set status = p_status, groups = p_groups, reason_code = p_reason_code, model = p_model, updated_at = now()
   where id = p_proposal_id
  returning * into v_proposal;
  return to_jsonb(v_proposal) - 'input';
end;
$$;

-- The active mapping: the newest version's entries.
create or replace function public.catalog_manufacturer_normalization_current()
returns jsonb
language sql
stable
set search_path = pg_catalog
as $$
  select jsonb_build_object(
    'version', coalesce((select max(version) from public.catalog_manufacturer_normalization_versions), 0),
    'entries', coalesce((
      select jsonb_agg(jsonb_build_object(
               'source_tozar', e.source_tozar, 'canonical_name', e.canonical_name,
               'provenance', e.provenance, 'rule_id', e.rule_id, 'proposal_id', e.proposal_id,
               'confidence', e.confidence, 'approved_at', e.approved_at)
             order by e.source_tozar collate "C")
        from public.catalog_manufacturer_normalization_entries e
        join public.catalog_manufacturer_normalization_versions v on v.id = e.version_id
       where v.version = (select max(version) from public.catalog_manufacturer_normalization_versions)),
      '[]'::jsonb))
$$;

-- The owner's approval: ONE new version = the active entries not re-mapped +
-- the approved ones. A compare-and-set on the active version number.
-- p_entries: [{"source_tozar", "canonical_name", "provenance": "rule"|"model",
--              "rule_id" | "proposal_id"}]
create or replace function public.approve_manufacturer_normalization(
  p_approved_by uuid, p_expected_version integer, p_entries jsonb
) returns jsonb
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_current integer;
  v_version public.catalog_manufacturer_normalization_versions%rowtype;
  v_directory uuid;
  v_count integer;
begin
  if p_approved_by is null or p_expected_version is null or p_entries is null
     or jsonb_typeof(p_entries) <> 'array' or jsonb_array_length(p_entries) not between 1 and 2000
     or exists (select 1 from jsonb_array_elements(p_entries) e
                 where jsonb_typeof(e) <> 'object' or jsonb_typeof(e->'source_tozar') <> 'string'
                    or jsonb_typeof(e->'canonical_name') <> 'string'
                    or e->>'provenance' not in ('rule', 'model'))
     or (select count(*) <> count(distinct e->>'source_tozar') from jsonb_array_elements(p_entries) e) then
    raise exception 'CATALOG_NORMALIZATION_APPROVAL_INVALID: invalid approval' using errcode = '22023';
  end if;
  perform pg_advisory_xact_lock(hashtext('public.catalog_manufacturer_normalization_versions'));
  select coalesce(max(version), 0) into v_current from public.catalog_manufacturer_normalization_versions;
  if v_current <> p_expected_version then
    raise exception 'CATALOG_NORMALIZATION_VERSION_STALE: the active normalisation changed' using errcode = '40001';
  end if;
  select id into v_directory from public.catalog_register_directory_versions
   order by created_at desc, id desc limit 1;
  -- Every source name is a tozar of the latest directory; a rule entry names a
  -- code-owned rule; a model entry is exactly a group of a proposed proposal.
  if exists (
    select 1 from jsonb_array_elements(p_entries) e
     where not exists (select 1 from public.catalog_register_directory_units u
                        where u.version_id = v_directory and u.tozar = e->>'source_tozar')
        or (e->>'provenance' = 'rule' and (coalesce(e->>'rule_id', '') not in ('R1_SPELLING', 'R2_TOZERET_CD')
                                           or e ? 'proposal_id'))
        or (e->>'provenance' = 'model' and (e ? 'rule_id' or not exists (
              select 1 from public.catalog_manufacturer_normalization_proposals p,
                            jsonb_array_elements(p.groups) g
               where p.id::text = e->>'proposal_id' and p.status = 'proposed'
                 and g->>'canonical' = e->>'canonical_name'
                 and g->'members' @> jsonb_build_array(e->>'source_tozar'))))) then
    raise exception 'CATALOG_NORMALIZATION_APPROVAL_INVALID: an entry has no valid provenance' using errcode = '22023';
  end if;
  -- The whole new mapping: the active entries not re-mapped, plus the approved ones.
  select count(*) + jsonb_array_length(p_entries) into v_count
    from public.catalog_manufacturer_normalization_entries e
    join public.catalog_manufacturer_normalization_versions v on v.id = e.version_id and v.version = v_current
   where not exists (select 1 from jsonb_array_elements(p_entries) n where n->>'source_tozar' = e.source_tozar);
  insert into public.catalog_manufacturer_normalization_versions (version, approved_by, entry_count)
  values (v_current + 1, p_approved_by, v_count)
  returning * into v_version;
  insert into public.catalog_manufacturer_normalization_entries
    (version_id, source_tozar, canonical_name, provenance, rule_id, proposal_id, confidence, approved_by, approved_at)
  select v_version.id, e.source_tozar, e.canonical_name, e.provenance, e.rule_id, e.proposal_id, e.confidence,
         e.approved_by, e.approved_at
    from public.catalog_manufacturer_normalization_entries e
    join public.catalog_manufacturer_normalization_versions v on v.id = e.version_id and v.version = v_current
   where not exists (select 1 from jsonb_array_elements(p_entries) n where n->>'source_tozar' = e.source_tozar)
  union all
  select v_version.id, n->>'source_tozar', n->>'canonical_name', n->>'provenance', n->>'rule_id',
         (n->>'proposal_id')::uuid,
         (select g->>'confidence' from public.catalog_manufacturer_normalization_proposals p,
                 jsonb_array_elements(p.groups) g
           where n->>'provenance' = 'model' and p.id::text = n->>'proposal_id'
             and g->'members' @> jsonb_build_array(n->>'source_tozar') limit 1),
         p_approved_by, now()
    from jsonb_array_elements(p_entries) n;
  return jsonb_build_object('version', v_current + 1, 'entry_count', v_count);
end;
$$;

-- Per captured tozar, the register's own evidence: its rows, its distinct
-- manufacturer codes, up to three plant names and three commercial models
-- (the served variant builds only).
create or replace function public.catalog_manufacturer_evidence()
returns jsonb
language sql
stable
set search_path = pg_catalog
as $$
  select coalesce(jsonb_agg(jsonb_build_object(
           'tozar', x.tozar, 'rows', x.rows, 'tozeret_cd', x.codes,
           'tozeret_nm', x.plants, 'samples', x.samples) order by x.tozar collate "C"), '[]'::jsonb)
    from (
      select v.tozar, count(*) as rows,
             coalesce(jsonb_agg(distinct v.tozeret_cd) filter (where v.tozeret_cd is not null), '[]'::jsonb) as codes,
             to_jsonb((array_agg(distinct v.tozeret_nm) filter (where v.tozeret_nm is not null))[1:3]) as plants,
             to_jsonb((array_agg(distinct v.kinuy_mishari) filter (where v.kinuy_mishari is not null))[1:3]) as samples
        from public.catalog_variants_current v
       group by v.tozar) x
$$;

-- ---------------------------------------------------------------------------
-- 5. RLS and privileges: service-path only; the read-only roles read.
-- ---------------------------------------------------------------------------
alter table public.catalog_manufacturer_normalization_proposals enable row level security;
alter table public.catalog_manufacturer_normalization_versions enable row level security;
alter table public.catalog_manufacturer_normalization_entries enable row level security;

do $$
declare
  fn text;
  tbl text;
  ro record;
  v_writes text[] := array[
    'public.request_manufacturer_normalization(uuid,integer,jsonb)',
    'public.record_manufacturer_normalization_proposal(uuid,text,integer,text,uuid,text,jsonb,text,text)',
    'public.approve_manufacturer_normalization(uuid,integer,jsonb)'];
  v_reads text[] := array[
    'public.catalog_normalization_groups_valid(jsonb,jsonb)',
    'public.catalog_manufacturer_normalization_current()',
    'public.catalog_manufacturer_evidence()'];
  v_tables text[] := array[
    'public.catalog_manufacturer_normalization_proposals',
    'public.catalog_manufacturer_normalization_versions',
    'public.catalog_manufacturer_normalization_entries'];
begin
  foreach fn in array v_writes || v_reads loop
    execute format('revoke execute on function %s from public', fn);
    if exists (select 1 from pg_roles where rolname = 'anon') then
      execute format('revoke execute on function %s from anon', fn);
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
      execute format('revoke execute on function %s from authenticated', fn);
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
      execute format('grant execute on function %s to service_role', fn);
    end if;
  end loop;
  foreach tbl in array v_tables loop
    execute format('revoke all on table %s from public', tbl);
    if exists (select 1 from pg_roles where rolname = 'anon') then
      execute format('revoke all on table %s from anon', tbl);
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
      execute format('revoke all on table %s from authenticated', tbl);
    end if;
  end loop;
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    -- Written only through the RPCs above; never deleted.
    execute 'grant select, insert, update on table public.catalog_manufacturer_normalization_proposals to service_role';
    execute 'grant select, insert on table public.catalog_manufacturer_normalization_versions to service_role';
    execute 'grant select, insert on table public.catalog_manufacturer_normalization_entries to service_role';
    foreach tbl in array v_tables loop
      execute format('revoke delete, truncate on table %s from service_role', tbl);
    end loop;
  end if;
  -- The read-only release roles, exactly as 20260929000100 selects them.
  for ro in
    select r.rolname from pg_roles r
     where r.rolcanlogin and not r.rolsuper
       and r.rolname not in ('postgres', 'service_role', 'authenticator', 'anon', 'authenticated')
       and (r.rolname = 'supabase_read_only_user'
            or (r.rolbypassrls and r.rolname like 'milo\_release\_readonly\_%')
            or (r.rolbypassrls and pg_has_role(r.oid, 'pg_read_all_data', 'MEMBER')))
  loop
    foreach tbl in array v_tables loop
      execute format('grant select on table %s to %I', tbl, ro.rolname);
    end loop;
    foreach fn in array v_reads loop
      execute format('grant execute on function %s to %I', fn, ro.rolname);
    end loop;
  end loop;
end $$;

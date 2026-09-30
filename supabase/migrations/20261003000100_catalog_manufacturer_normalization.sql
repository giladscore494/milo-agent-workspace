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
--     its OWN normalisation job (the capture job's definition, run as the
--     worker identity, the only job holding the provider key) under an
--     operator capture run -- the lease and
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
--   * The normalisation job is executed WITHOUT overrides (its definition is
--     fixed): it reads the single requested proposal
--     (requested_manufacturer_normalization) and claims it together with its
--     run's lease (claim_manufacturer_normalization -- the existing
--     claim_run_lease CAS, under the proposal lock), so two executions never
--     both claim it.
--   * A proposal moves once: requested -> proposed (its groups re-validated
--     against its input by a trigger) or requested -> refused; nothing else
--     is ever updated, so validated groups cannot be rewritten afterwards.
--   * Rule provenance (R1_SPELLING / R2_TOZERET_CD) is trusted from the
--     service layer: the database checks the rule id and that every member is
--     a tozar of the latest directory, but does not recompute the rule (R1's
--     key is Unicode NFKC + case folding + category stripping, which SQL
--     cannot restate exactly). Model provenance is checked in full.
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
-- A name carries no Unicode format character (category Cf: bidi overrides and
-- isolates, zero-width characters, the soft hyphen, the BOM, tag characters):
-- they make two different names read the same. The class restates Python's
-- unicodedata category Cf (backend/catalog/register/normalization.py).
create or replace function public.catalog_normalization_has_format_char(p_text text)
returns boolean
language sql
immutable
set search_path = pg_catalog
as $$
  select p_text ~ ('[' || U&'\00AD\0600-\0605\061C\06DD\070F\0890\0891\08E2\180E\200B-\200F\202A-\202E'
                   || U&'\2060-\2064\2066-\206F\FEFF\FFF9-\FFFB'
                   || U&'\+0110BD\+0110CD\+013430-\+01343F\+01BCA0-\+01BCA3\+01D173-\+01D17A'
                   || U&'\+0E0001\+0E0020-\+0E007F' || ']')
$$;

-- [{"canonical": text, "members": [input names], "confidence": "high"|"low",
--   "reason": text}], every member an input name, none in two groups.
create or replace function public.catalog_normalization_groups_valid(p_groups jsonb, p_input jsonb)
returns boolean
language sql
immutable
set search_path = pg_catalog
as $$
  -- Each shape test is evaluated only for an object of the right key set
  -- (CASE, not an OR chain the planner may reorder), and a NULL is a failure.
  select case when jsonb_typeof(p_groups) <> 'array' then false else coalesce(
    not exists (
      select 1 from jsonb_array_elements(p_groups) g
       where case
               when jsonb_typeof(g) <> 'object' then true
               when (select array_agg(k order by k) from jsonb_object_keys(g) k)
                    is distinct from array['canonical', 'confidence', 'members', 'reason'] then true
               when jsonb_typeof(g->'canonical') <> 'string' or jsonb_typeof(g->'reason') <> 'string'
                    or jsonb_typeof(g->'members') <> 'array' then true
               else not coalesce(
                 char_length(g->>'canonical') between 1 and 120
                 -- no leading/trailing whitespace (Python's strip) and no control character
                 and g->>'canonical' !~ '^[[:space:]]|[[:space:]]$' and g->>'canonical' !~ '[[:cntrl:]]'
                 and not public.catalog_normalization_has_format_char(g->>'canonical')
                 and g->>'confidence' in ('high', 'low')
                 and char_length(g->>'reason') <= 300 and g->>'reason' !~ '[[:cntrl:]]'
                 and jsonb_array_length(g->'members') >= 1
                 and not exists (select 1 from jsonb_array_elements(g->'members') m
                                  where jsonb_typeof(m) <> 'string'
                                     or public.catalog_normalization_has_format_char(m #>> '{}')
                                     or not exists (select 1 from jsonb_array_elements(p_input) i
                                                     where i->>'name' = m #>> '{}')), false)
             end)
    and (select count(*) = count(distinct m #>> '{}')
           from jsonb_array_elements(p_groups) g, jsonb_array_elements(g->'members') m
          where jsonb_typeof(g) = 'object' and jsonb_typeof(g->'members') = 'array'),
    false) end
$$;

-- ---------------------------------------------------------------------------
-- 4. Request (the website), record (the capture job), approve (the owner).
-- ---------------------------------------------------------------------------

-- One live normalisation at a time (the group liveness rule, as a directory
-- refresh). A live one answers `existing` and writes nothing. A press never
-- spends twice on the same question: when the newest `proposed` proposal was
-- made from the SAME input (input_sha256), it is answered (`reused`) and no
-- model call starts; and a new request waits out a cooldown after the last one
-- (`cooldown`, with the seconds left) -- 600 s, owned here and restated as
-- backend/catalog/register/normalization.py REQUEST_COOLDOWN_SECONDS.
create or replace function public.request_manufacturer_normalization(
  p_requested_by uuid, p_grace_seconds integer, p_input jsonb
) returns jsonb
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_group public.catalog_register_capture_groups%rowtype;
  v_proposal public.catalog_manufacturer_normalization_proposals%rowtype;
  v_sha text;
  v_cooldown constant integer := 600;
begin
  if p_requested_by is null or p_grace_seconds is null or p_grace_seconds not between 300 and 86400
     or p_input is null or jsonb_typeof(p_input) <> 'array' or jsonb_array_length(p_input) not between 1 and 2000
     or exists (select 1 from jsonb_array_elements(p_input) i
                 where jsonb_typeof(i) <> 'object' or jsonb_typeof(i->'name') <> 'string'
                    or char_length(i->>'name') not between 1 and 200
                    -- the evidence is bounded per string, not only per list
                    or exists (select 1 from jsonb_array_elements(
                                 coalesce(i->'tozeret_nm', '[]'::jsonb) || coalesce(i->'samples', '[]'::jsonb)) v
                                where jsonb_typeof(v) <> 'string' or char_length(v #>> '{}') > 120))
     or (select count(*) <> count(distinct i->>'name') from jsonb_array_elements(p_input) i) then
    raise exception 'CATALOG_NORMALIZATION_REQUEST_INVALID: invalid normalisation request' using errcode = '22023';
  end if;
  v_sha := encode(sha256(convert_to(p_input::text, 'UTF8')), 'hex');
  perform pg_advisory_xact_lock(hashtext('public.catalog_manufacturer_normalization'));
  select * into v_group from public.catalog_register_capture_groups
   where kind = 'normalisation' order by claimed_at desc, id desc limit 1;
  if found and not public.catalog_register_group_stale(v_group.id, make_interval(secs => p_grace_seconds)) then
    select * into v_proposal from public.catalog_manufacturer_normalization_proposals where group_id = v_group.id;
    return jsonb_build_object('decision', 'existing', 'group', to_jsonb(v_group),
                              'proposal', to_jsonb(v_proposal) - 'input');
  end if;
  -- The same question already has an answer: no second model call.
  select * into v_proposal from public.catalog_manufacturer_normalization_proposals
   where status = 'proposed' order by created_at desc, id desc limit 1;
  if found and v_proposal.input_sha256 = v_sha then
    return jsonb_build_object('decision', 'reused', 'group', null, 'proposal', to_jsonb(v_proposal) - 'input');
  end if;
  if v_group.id is not null and v_group.claimed_at > now() - make_interval(secs => v_cooldown) then
    select * into v_proposal from public.catalog_manufacturer_normalization_proposals where group_id = v_group.id;
    return jsonb_build_object('decision', 'cooldown', 'group', to_jsonb(v_group),
                              'proposal', to_jsonb(v_proposal) - 'input',
                              'retry_after_seconds',
                              ceil(extract(epoch from v_group.claimed_at + make_interval(secs => v_cooldown) - now()))::integer);
  end if;
  insert into public.catalog_register_capture_groups (kind, register_version, requested_by, expected_rows)
  values ('normalisation', null, p_requested_by, 0)
  returning * into v_group;
  insert into public.catalog_manufacturer_normalization_proposals (group_id, requested_by, input, input_sha256)
  values (v_group.id, p_requested_by, p_input, v_sha)
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

-- A proposal moves ONCE, and only forward: requested -> proposed (its groups
-- valid against its own input) or requested -> refused. Nothing else of it is
-- ever updated -- the validated groups cannot be rewritten below the RPCs.
create or replace function public.catalog_normalization_proposal_transition()
returns trigger
language plpgsql
set search_path = pg_catalog
as $$
begin
  if old.status <> 'requested' or new.status not in ('proposed', 'refused')
     or new.id is distinct from old.id or new.group_id is distinct from old.group_id
     or new.requested_by is distinct from old.requested_by or new.input is distinct from old.input
     or new.input_sha256 is distinct from old.input_sha256 or new.created_at is distinct from old.created_at
     or (new.status = 'proposed' and not public.catalog_normalization_groups_valid(new.groups, old.input)) then
    raise exception 'CATALOG_NORMALIZATION_PROPOSAL_IMMUTABLE: a proposal moves once, from requested to proposed or refused'
      using errcode = '42501';
  end if;
  return new;
end;
$$;
drop trigger if exists catalog_manufacturer_normalization_proposals_transition
  on public.catalog_manufacturer_normalization_proposals;
create trigger catalog_manufacturer_normalization_proposals_transition
  before update on public.catalog_manufacturer_normalization_proposals
  for each row execute function public.catalog_normalization_proposal_transition();

-- The normalisation job's work, read: the newest normalisation request, while
-- it is still `requested` and the API has recorded its run -- {id, run_id} --
-- else null (the job then starts nothing).
create or replace function public.requested_manufacturer_normalization()
returns jsonb
language sql
stable
set search_path = pg_catalog
as $$
  select case when p.status = 'requested' and g.run_id is not null
              then jsonb_build_object('id', p.id, 'run_id', g.run_id) end
    from public.catalog_register_capture_groups g
    join public.catalog_manufacturer_normalization_proposals p on p.group_id = g.id
   where g.kind = 'normalisation'
   order by g.claimed_at desc, g.id desc
   limit 1
$$;

-- ...and claimed: under the proposal lock, still exactly that request, and its
-- run's lease through the existing claim_run_lease CAS (the fencing the
-- outcome's record_manufacturer_normalization_proposal asserts). A second
-- execution finds the lease held (CATALOG_NORMALIZATION_ALREADY_CLAIMED) or the
-- proposal answered (CATALOG_NORMALIZATION_NOT_REQUESTED).
create or replace function public.claim_manufacturer_normalization(
  p_proposal_id uuid, p_run_id uuid, p_worker_id text, p_lease_seconds integer
) returns setof public.runs
language plpgsql
-- claim_run_lease (migration 012) pins no search_path and draws its lease token
-- from pgcrypto's gen_random_bytes, which lives in `public` here and in
-- `extensions` on Supabase: both are on this (security invoker) function's path.
set search_path = pg_catalog, public, extensions
as $$
declare
  v_run public.runs%rowtype;
begin
  perform pg_advisory_xact_lock(hashtext('public.catalog_manufacturer_normalization'));
  if p_proposal_id is null or p_run_id is null
     or public.requested_manufacturer_normalization()
        is distinct from jsonb_build_object('id', p_proposal_id, 'run_id', p_run_id) then
    raise exception 'CATALOG_NORMALIZATION_NOT_REQUESTED: that normalisation is not the requested one'
      using errcode = '40001';
  end if;
  select * into v_run from public.claim_run_lease(p_run_id, p_worker_id, p_lease_seconds);
  if not found then
    raise exception 'CATALOG_NORMALIZATION_ALREADY_CLAIMED: another execution holds this normalisation'
      using errcode = '40001';
  end if;
  return next v_run;
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
                    or public.catalog_normalization_has_format_char(e->>'canonical_name')
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
-- The owner may reject any pending group instead: an append-only record,
-- keyed by the group exactly as it was proposed (its provenance, canonical
-- name and members). A rejected group is no longer pending; a different group
-- (a new proposal, a changed rule answer) is a new decision.
create table if not exists public.catalog_manufacturer_normalization_rejections (
  id uuid primary key default gen_random_uuid(),
  group_key text not null unique check (group_key ~ '^[0-9a-f]{64}$'),
  canonical_name text not null check (char_length(canonical_name) between 1 and 200),
  members text[] not null check (cardinality(members) between 1 and 2000),
  rule_id text check (rule_id is null or rule_id in ('R1_SPELLING', 'R2_TOZERET_CD')),
  proposal_id uuid references public.catalog_manufacturer_normalization_proposals(id) on delete restrict,
  rejected_by uuid not null,
  rejected_at timestamptz not null default now(),
  check ((rule_id is null) <> (proposal_id is null))
);
create index if not exists catalog_manufacturer_normalization_rejections_proposal_idx
  on public.catalog_manufacturer_normalization_rejections (proposal_id);
drop trigger if exists catalog_manufacturer_normalization_rejections_append_only
  on public.catalog_manufacturer_normalization_rejections;
create trigger catalog_manufacturer_normalization_rejections_append_only
  before update or delete on public.catalog_manufacturer_normalization_rejections
  for each row execute function public.forbid_catalog_register_rewrite();

create or replace function public.reject_manufacturer_normalization_group(p_rejected_by uuid, p_group jsonb)
returns jsonb
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_members text[];
  v_rule text;
  v_proposal uuid;
  v_key text;
  v_row public.catalog_manufacturer_normalization_rejections%rowtype;
  v_directory uuid;
begin
  if p_rejected_by is null or jsonb_typeof(p_group) is distinct from 'object'
     or jsonb_typeof(p_group->'canonical') is distinct from 'string'
     or jsonb_typeof(p_group->'members') is distinct from 'array'
     or jsonb_array_length(p_group->'members') not between 1 and 2000
     or exists (select 1 from jsonb_array_elements(p_group->'members') m where jsonb_typeof(m) <> 'string')
     or (nullif(p_group->>'rule_id', '') is null) = (nullif(p_group->>'proposal_id', '') is null) then
    raise exception 'CATALOG_NORMALIZATION_REJECTION_INVALID: invalid rejection' using errcode = '22023';
  end if;
  select array_agg(m order by m collate "C") into v_members
    from (select distinct m #>> '{}' as m from jsonb_array_elements(p_group->'members') m) x;
  if cardinality(v_members) <> jsonb_array_length(p_group->'members') then
    raise exception 'CATALOG_NORMALIZATION_REJECTION_INVALID: a name appears twice' using errcode = '22023';
  end if;
  v_rule := p_group->>'rule_id';
  if nullif(p_group->>'proposal_id', '') is not null then
    begin
      v_proposal := (p_group->>'proposal_id')::uuid;
    exception when others then
      raise exception 'CATALOG_NORMALIZATION_REJECTION_INVALID: invalid proposal' using errcode = '22023';
    end;
  end if;
  select id into v_directory from public.catalog_register_directory_versions
   order by created_at desc, id desc limit 1;
  -- Exactly a group the server proposed: a code-owned rule over tozars of the
  -- latest directory, or a group of a proposed model proposal, as it stands.
  if (v_rule is not null and (v_rule not in ('R1_SPELLING', 'R2_TOZERET_CD')
                              or exists (select 1 from unnest(v_members) m
                                          where not exists (select 1 from public.catalog_register_directory_units u
                                                             where u.version_id = v_directory and u.tozar = m))))
     or (v_proposal is not null and not exists (
           select 1 from public.catalog_manufacturer_normalization_proposals p, jsonb_array_elements(p.groups) g
            where p.id = v_proposal and p.status = 'proposed' and g->>'canonical' = p_group->>'canonical'
              and (select array_agg(m #>> '{}' order by m #>> '{}' collate "C")
                     from jsonb_array_elements(g->'members') m) = v_members)) then
    raise exception 'CATALOG_NORMALIZATION_REJECTION_INVALID: the group is not a pending proposal'
      using errcode = '22023';
  end if;
  v_key := encode(sha256(convert_to(jsonb_build_object(
             'canonical', p_group->>'canonical', 'members', to_jsonb(v_members), 'rule_id', v_rule,
             'proposal_id', v_proposal)::text, 'UTF8')), 'hex');
  insert into public.catalog_manufacturer_normalization_rejections
    (group_key, canonical_name, members, rule_id, proposal_id, rejected_by)
  values (v_key, p_group->>'canonical', v_members, v_rule, v_proposal, p_rejected_by)
  on conflict (group_key) do nothing;
  select * into v_row from public.catalog_manufacturer_normalization_rejections where group_key = v_key;
  return to_jsonb(v_row);
end;
$$;

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
alter table public.catalog_manufacturer_normalization_rejections enable row level security;

do $$
declare
  fn text;
  tbl text;
  ro record;
  v_writes text[] := array[
    'public.request_manufacturer_normalization(uuid,integer,jsonb)',
    'public.record_manufacturer_normalization_proposal(uuid,text,integer,text,uuid,text,jsonb,text,text)',
    'public.claim_manufacturer_normalization(uuid,uuid,text,integer)',
    'public.requested_manufacturer_normalization()',
    'public.approve_manufacturer_normalization(uuid,integer,jsonb)',
    'public.reject_manufacturer_normalization_group(uuid,jsonb)'];
  v_reads text[] := array[
    'public.catalog_normalization_groups_valid(jsonb,jsonb)',
    'public.catalog_normalization_has_format_char(text)',
    'public.catalog_manufacturer_normalization_current()',
    'public.catalog_manufacturer_evidence()'];
  v_tables text[] := array[
    'public.catalog_manufacturer_normalization_proposals',
    'public.catalog_manufacturer_normalization_versions',
    'public.catalog_manufacturer_normalization_entries',
    'public.catalog_manufacturer_normalization_rejections'];
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
    execute 'grant select, insert on table public.catalog_manufacturer_normalization_rejections to service_role';
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

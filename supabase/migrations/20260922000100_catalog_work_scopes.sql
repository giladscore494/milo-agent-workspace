-- Catalog work scopes: the ONE canonical, server-owned mapping PLAN.
--
-- What this adds
-- --------------
--
-- A work scope says which marques to map and in what order, which model
-- years, how many candidates at most, and how many candidates one batch run
-- may take (`backend/catalog/scope/contract.py`). A person states it either by
-- typing an instruction or by editing the Mapping Plan; both are only inputs,
-- and both are reduced by the backend to the same canonical record before
-- anything is stored here. This migration is where that record becomes
-- durable:
--
--   public.catalog_work_scopes            one row per plan, and its HEAD
--   public.catalog_work_scope_revisions   every revision, append-only
--   public.create_work_scope()            revision 1, atomically with the plan
--   public.revise_work_scope()            revision n+1, compare-and-set on the
--                                         head revision AND digest
--   public.catalog_canonical_manufacturer_coverage()
--                                         the bounded coverage READ the
--                                         Mapping Plan shows per marque
--
-- What this deliberately does not add
-- -----------------------------------
--
-- No Government read, no queue, no batch and no run. A plan here is a DRAFT
-- and nothing in the schema can execute one: there is no status but `draft`,
-- no column naming a snapshot, and no relation to `runs`. Preparing the
-- register and launching batches are later, separately reviewed migrations.
--
-- The digest is derived HERE
-- --------------------------
--
-- The backend renders the record as canonical text (sorted keys, compact
-- separators, ASCII) and sends the TEXT. The database stores that text,
-- derives `digest = sha256(text)` itself, and a CHECK constraint holds every
-- row to it -- so a revision whose digest disagrees with its plan cannot exist
-- by any write path, including a direct service-role INSERT. `scope` is the
-- same record as jsonb, held equal to the text by a second CHECK, so SQL can
-- read it without re-parsing and the two can never say different things.
--
-- Service-path only, like every other catalog relation: RLS on with no
-- policies, no browser grants, and EXECUTE on the functions for `service_role`
-- alone. The API authorizes membership before calling, and the two write
-- functions re-check it against `project_members` anyway, so a caller that
-- names a conversation it cannot see is refused by the database too.
--
-- Additive and forward-only: two new relations, one new index on
-- `catalog_models`, and new functions. Nothing existing is altered. Rerun-safe.

-- ---------------------------------------------------------------------------
-- 1. The contract, the digest and the record shape, as functions.
-- ---------------------------------------------------------------------------
--
-- Each mirrors a definition in `backend/catalog/scope/contract.py` and is
-- pinned against it by `tests/test_work_scope_migration_static.py`.
create or replace function public.catalog_work_scope_contract_version()
returns text
language sql
immutable
set search_path = pg_catalog
as $$
  select 'milo-work-scope/1'::text;
$$;

create or replace function public.catalog_work_scope_text_digest(p_text text)
returns text
language sql
stable
set search_path = pg_catalog
as $$
  select encode(sha256(convert_to(p_text, 'UTF8')), 'hex');
$$;

-- Whether one stored record is a well-formed `milo-work-scope/1` plan. The
-- SHAPE only: that every unit names a real directory entry is the backend's
-- reviewed directory, not something SQL can know. What SQL CAN hold is the hard
-- bounds -- above all the batch maximum of 20 -- so no path can store a plan
-- that exceeds them.
create or replace function public.catalog_work_scope_record_valid(p_scope jsonb)
returns boolean
language plpgsql
immutable
set search_path = pg_catalog
as $$
declare
  v_years jsonb;
  v_from integer;
  v_to integer;
begin
  if p_scope is null or jsonb_typeof(p_scope) <> 'object' then
    return false;
  end if;
  if (select array_agg(k order by k collate "C") from jsonb_object_keys(p_scope) as k)
       is distinct from array['batch_size', 'contract', 'directory_version', 'max_items',
                              'model_years', 'source', 'units'] then
    return false;
  end if;
  if p_scope->>'contract' is distinct from public.catalog_work_scope_contract_version()
     or p_scope->'source' is distinct from jsonb_build_object(
          'family', 'government', 'package_id', 'degem-rechev-wltp',
          'resource_id', '142afde2-6228-49f9-8a29-9b6c3a0cbe40') then
    return false;
  end if;
  if jsonb_typeof(p_scope->'directory_version') <> 'string'
     or char_length(p_scope->>'directory_version') not between 1 and 80 then
    return false;
  end if;
  if jsonb_typeof(p_scope->'batch_size') <> 'number'
     or p_scope->>'batch_size' !~ '^[0-9]{1,4}$'
     or (p_scope->>'batch_size')::integer not between 1 and 20 then
    return false;
  end if;
  if jsonb_typeof(p_scope->'max_items') <> 'number'
     or p_scope->>'max_items' !~ '^[0-9]{1,6}$'
     or (p_scope->>'max_items')::integer not between 1 and 2000 then
    return false;
  end if;
  if jsonb_typeof(p_scope->'units') <> 'array'
     or jsonb_array_length(p_scope->'units') not between 1 and 64
     or exists (select 1 from jsonb_array_elements(p_scope->'units') as u(value)
                 where jsonb_typeof(u.value) <> 'string'
                    or u.value #>> '{}' !~ '^[a-z][a-z0-9_]{0,39}$')
     or (select count(distinct u.value) from jsonb_array_elements(p_scope->'units') as u(value))
          <> jsonb_array_length(p_scope->'units') then
    return false;
  end if;
  v_years := p_scope->'model_years';
  if jsonb_typeof(v_years) <> 'object'
     or (select array_agg(k order by k collate "C") from jsonb_object_keys(v_years) as k)
          is distinct from array['from', 'to'] then
    return false;
  end if;
  if jsonb_typeof(v_years->'from') not in ('null', 'number')
     or jsonb_typeof(v_years->'to') not in ('null', 'number') then
    return false;
  end if;
  if jsonb_typeof(v_years->'from') = 'number' then
    if v_years->>'from' !~ '^[0-9]{4}$' then return false; end if;
    v_from := (v_years->>'from')::integer;
    if v_from not between 1900 and 2100 then return false; end if;
  end if;
  if jsonb_typeof(v_years->'to') = 'number' then
    if v_years->>'to' !~ '^[0-9]{4}$' then return false; end if;
    v_to := (v_years->>'to')::integer;
    if v_to not between 1900 and 2100 then return false; end if;
  end if;
  if v_from is not null and v_to is not null and v_from > v_to then
    return false;
  end if;
  return true;
end;
$$;

-- ---------------------------------------------------------------------------
-- 2. The plan and its revisions.
-- ---------------------------------------------------------------------------
create table if not exists public.catalog_work_scopes (
  id uuid primary key default gen_random_uuid(),
  -- Derived from the conversation by `create_work_scope`, never supplied.
  project_id uuid not null references public.projects(id) on delete restrict,
  conversation_id uuid not null references public.conversations(id) on delete restrict,
  -- An audit fact, deliberately not a foreign key: the rows it describes are
  -- immutable, and `on delete set null` would be a rewrite.
  created_by uuid not null,
  status text not null default 'draft',
  head_revision integer not null,
  head_digest text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  closed_at timestamptz,
  constraint catalog_work_scopes_status_check check (status in ('draft')),
  constraint catalog_work_scopes_head_revision_positive check (head_revision >= 1),
  constraint catalog_work_scopes_head_digest_shape check (head_digest ~ '^[0-9a-f]{64}$'),
  constraint catalog_work_scopes_closed_after_created
    check (closed_at is null or closed_at >= created_at)
);
-- One OPEN plan per conversation: the conversation's Mapping Plan is never
-- ambiguous, and two concurrent "create" requests cannot both win.
create unique index if not exists catalog_work_scopes_open_conversation_uidx
  on public.catalog_work_scopes(conversation_id) where closed_at is null;
create index if not exists catalog_work_scopes_project_idx
  on public.catalog_work_scopes(project_id, created_at desc);

create table if not exists public.catalog_work_scope_revisions (
  id uuid primary key default gen_random_uuid(),
  work_scope_id uuid not null references public.catalog_work_scopes(id) on delete restrict,
  revision integer not null,
  scope_text text not null,
  scope jsonb not null,
  digest text not null,
  input_kind text not null,
  instruction text,
  notes jsonb not null default '[]'::jsonb,
  created_by uuid not null,
  created_at timestamptz not null default now(),
  constraint catalog_work_scope_revisions_revision_positive check (revision >= 1),
  constraint catalog_work_scope_revisions_text_bounded
    check (char_length(scope_text) between 2 and 4000),
  constraint catalog_work_scope_revisions_scope_is_text check (scope = scope_text::jsonb),
  constraint catalog_work_scope_revisions_digest_derived
    check (digest = public.catalog_work_scope_text_digest(scope_text)),
  constraint catalog_work_scope_revisions_record_valid
    check (public.catalog_work_scope_record_valid(scope)),
  constraint catalog_work_scope_revisions_input_kind
    check (input_kind in ('instruction', 'edit')),
  -- An instruction revision carries the words it was read from; an edit has
  -- none. So a revision can always say which input produced it.
  constraint catalog_work_scope_revisions_instruction_matches_kind
    check ((input_kind = 'instruction') = (instruction is not null)),
  constraint catalog_work_scope_revisions_instruction_bounded
    check (instruction is null or char_length(instruction) between 1 and 500),
  constraint catalog_work_scope_revisions_notes_bounded
    check (jsonb_typeof(notes) = 'array' and char_length(notes::text) <= 4000)
);
create unique index if not exists catalog_work_scope_revisions_scope_revision_uidx
  on public.catalog_work_scope_revisions(work_scope_id, revision);

-- The coverage read joins canonical models by the exact register marque.
create index if not exists catalog_models_manufacturer_idx
  on public.catalog_models(manufacturer);

-- ---------------------------------------------------------------------------
-- 3. Immutability, held by the database.
-- ---------------------------------------------------------------------------

-- A revision is a record of what was asked for and what it became. It is
-- never rewritten and never removed, by any role.
create or replace function public.forbid_work_scope_revision_mutation() returns trigger
language plpgsql
set search_path = pg_catalog
as $$
begin
  raise exception 'WORK_SCOPE_REVISION_IMMUTABLE' using errcode = '55000';
end;
$$;

drop trigger if exists catalog_work_scope_revisions_append_only
  on public.catalog_work_scope_revisions;
create trigger catalog_work_scope_revisions_append_only
  before update or delete on public.catalog_work_scope_revisions
  for each row execute function public.forbid_work_scope_revision_mutation();

-- Revisions are numbered 1, 2, 3 ... per plan, with no gap and no fork.
create or replace function public.work_scope_revision_in_sequence() returns trigger
language plpgsql
set search_path = pg_catalog
as $$
begin
  if new.revision <> coalesce((select max(r.revision) from public.catalog_work_scope_revisions r
                                where r.work_scope_id = new.work_scope_id), 0) + 1 then
    raise exception 'WORK_SCOPE_REVISION_OUT_OF_SEQUENCE' using errcode = '23514';
  end if;
  return new;
end;
$$;

drop trigger if exists catalog_work_scope_revisions_in_sequence
  on public.catalog_work_scope_revisions;
create trigger catalog_work_scope_revisions_in_sequence
  before insert on public.catalog_work_scope_revisions
  for each row execute function public.work_scope_revision_in_sequence();

-- A plan's identity never changes, it is never deleted, and its head only
-- ever advances one revision at a time.
create or replace function public.forbid_work_scope_rewrite() returns trigger
language plpgsql
set search_path = pg_catalog
as $$
begin
  if tg_op = 'DELETE' then
    raise exception 'WORK_SCOPE_IMMUTABLE' using errcode = '55000';
  end if;
  if new.id is distinct from old.id
     or new.project_id is distinct from old.project_id
     or new.conversation_id is distinct from old.conversation_id
     or new.created_by is distinct from old.created_by
     or new.created_at is distinct from old.created_at
     or old.closed_at is not null then
    raise exception 'WORK_SCOPE_IMMUTABLE' using errcode = '55000';
  end if;
  if new.head_revision not in (old.head_revision, old.head_revision + 1) then
    raise exception 'WORK_SCOPE_IMMUTABLE' using errcode = '55000';
  end if;
  return new;
end;
$$;

drop trigger if exists catalog_work_scopes_identity_immutable on public.catalog_work_scopes;
create trigger catalog_work_scopes_identity_immutable
  before update or delete on public.catalog_work_scopes
  for each row execute function public.forbid_work_scope_rewrite();

-- The HEAD always names a revision that exists, with that revision's digest.
-- Deferred to commit, because `create_work_scope` must insert the plan before
-- the revision that references it.
create or replace function public.work_scope_head_is_a_revision() returns trigger
language plpgsql
set search_path = pg_catalog
as $$
begin
  if not exists (select 1 from public.catalog_work_scope_revisions r
                  where r.work_scope_id = new.id and r.revision = new.head_revision
                    and r.digest = new.head_digest) then
    raise exception 'WORK_SCOPE_HEAD_INVALID' using errcode = '23514';
  end if;
  return null;
end;
$$;

drop trigger if exists catalog_work_scopes_head_is_a_revision on public.catalog_work_scopes;
create constraint trigger catalog_work_scopes_head_is_a_revision
  after insert or update on public.catalog_work_scopes
  deferrable initially deferred
  for each row execute function public.work_scope_head_is_a_revision();

-- ---------------------------------------------------------------------------
-- 4. The two writers.
-- ---------------------------------------------------------------------------
--
-- `p_revision` carries exactly what the backend decided: `scope_text` (the
-- canonical record), `input_kind`, `instruction` and `notes`. The digest is
-- derived from the text here; the project is derived from the conversation
-- here; membership and the trusted project workflow are re-checked here.
-- Every refusal names itself, so the backend can map it without reading SQL.

create or replace function public.work_scope_revision_row(
  p_work_scope_id uuid, p_revision_number integer, p_created_by uuid, p_revision jsonb
) returns public.catalog_work_scope_revisions
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_text text;
  v_row public.catalog_work_scope_revisions;
begin
  if p_revision is null or jsonb_typeof(p_revision) <> 'object'
     or jsonb_typeof(p_revision->'scope_text') is distinct from 'string'
     or jsonb_typeof(p_revision->'input_kind') is distinct from 'string'
     or coalesce(jsonb_typeof(p_revision->'instruction'), 'null') not in ('null', 'string')
     or coalesce(jsonb_typeof(p_revision->'notes'), 'array') <> 'array' then
    raise exception 'WORK_SCOPE_REVISION_INVALID' using errcode = '22023';
  end if;
  v_text := p_revision->>'scope_text';
  begin
    insert into public.catalog_work_scope_revisions
      (work_scope_id, revision, scope_text, scope, digest, input_kind, instruction,
       notes, created_by)
    values (p_work_scope_id, p_revision_number, v_text, v_text::jsonb,
            public.catalog_work_scope_text_digest(v_text), p_revision->>'input_kind',
            p_revision->>'instruction', coalesce(p_revision->'notes', '[]'::jsonb),
            p_created_by)
    returning * into v_row;
  exception
    when check_violation or invalid_text_representation or not_null_violation then
      raise exception 'WORK_SCOPE_REVISION_INVALID' using errcode = '22023';
  end;
  return v_row;
end;
$$;

create or replace function public.create_work_scope(
  p_conversation_id uuid, p_created_by uuid, p_revision jsonb
) returns jsonb
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_project_id uuid;
  v_workflow text;
  v_scope public.catalog_work_scopes;
  v_revision public.catalog_work_scope_revisions;
  v_digest text;
begin
  if p_created_by is null then
    raise exception 'WORK_SCOPE_REVISION_INVALID' using errcode = '22023';
  end if;
  select c.project_id, p.workflow_key into v_project_id, v_workflow
    from public.conversations c join public.projects p on p.id = c.project_id
   where c.id = p_conversation_id;
  -- Absent and not-a-member are ONE answer, so the refusal discloses nothing.
  if v_project_id is null or not exists (
       select 1 from public.project_members m
        where m.project_id = v_project_id and m.user_id = p_created_by) then
    raise exception 'WORK_SCOPE_CONVERSATION_NOT_FOUND' using errcode = 'P0002';
  end if;
  if v_workflow is distinct from 'swarm_v2' then
    raise exception 'WORK_SCOPE_WORKFLOW_UNSUPPORTED' using errcode = '22023';
  end if;
  if jsonb_typeof(p_revision->'scope_text') is distinct from 'string' then
    raise exception 'WORK_SCOPE_REVISION_INVALID' using errcode = '22023';
  end if;
  v_digest := public.catalog_work_scope_text_digest(p_revision->>'scope_text');
  begin
    insert into public.catalog_work_scopes
      (project_id, conversation_id, created_by, status, head_revision, head_digest)
    values (v_project_id, p_conversation_id, p_created_by, 'draft', 1, v_digest)
    returning * into v_scope;
  exception
    when unique_violation then
      raise exception 'WORK_SCOPE_OPEN_EXISTS' using errcode = '23505';
  end;
  v_revision := public.work_scope_revision_row(v_scope.id, 1, p_created_by, p_revision);
  return jsonb_build_object('work_scope', to_jsonb(v_scope), 'revision', to_jsonb(v_revision));
end;
$$;

create or replace function public.revise_work_scope(
  p_work_scope_id uuid, p_expected_revision integer, p_expected_digest text,
  p_created_by uuid, p_revision jsonb
) returns jsonb
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_scope public.catalog_work_scopes;
  v_workflow text;
  v_revision public.catalog_work_scope_revisions;
begin
  if p_created_by is null then
    raise exception 'WORK_SCOPE_REVISION_INVALID' using errcode = '22023';
  end if;
  -- The row lock is the serialization point: two revisions of one plan can
  -- never both be written against the same head.
  select * into v_scope from public.catalog_work_scopes where id = p_work_scope_id for update;
  if v_scope.id is null or not exists (
       select 1 from public.project_members m
        where m.project_id = v_scope.project_id and m.user_id = p_created_by) then
    raise exception 'WORK_SCOPE_NOT_FOUND' using errcode = 'P0002';
  end if;
  select p.workflow_key into v_workflow from public.projects p where p.id = v_scope.project_id;
  if v_workflow is distinct from 'swarm_v2' then
    raise exception 'WORK_SCOPE_WORKFLOW_UNSUPPORTED' using errcode = '22023';
  end if;
  if v_scope.closed_at is not null or v_scope.status <> 'draft' then
    raise exception 'WORK_SCOPE_NOT_EDITABLE' using errcode = '55000';
  end if;
  -- A stale digest fails closed: the caller must name EXACTLY the head it saw.
  if p_expected_revision is distinct from v_scope.head_revision
     or p_expected_digest is distinct from v_scope.head_digest then
    raise exception 'WORK_SCOPE_STALE' using errcode = '40001';
  end if;
  v_revision := public.work_scope_revision_row(v_scope.id, v_scope.head_revision + 1,
                                               p_created_by, p_revision);
  update public.catalog_work_scopes
     set head_revision = v_revision.revision, head_digest = v_revision.digest,
         updated_at = now()
   where id = v_scope.id
   returning * into v_scope;
  return jsonb_build_object('work_scope', to_jsonb(v_scope), 'revision', to_jsonb(v_revision));
end;
$$;

-- ---------------------------------------------------------------------------
-- 5. The coverage READ.
-- ---------------------------------------------------------------------------
--
-- For each requested register marque, the exact number of canonical variants
-- the catalog holds under it -- zero included -- plus ONE row whose
-- `manufacturer` is NULL carrying the catalog-wide total, so variants no
-- requested marque accounts for are visible as a difference. Bounded by the
-- request (at most 64 marques, each 1-120 characters) and ordered with
-- `collate "C"` like every other catalog read.
create or replace function public.catalog_canonical_manufacturer_coverage(p_manufacturers text[])
returns table(manufacturer text, canonical_variants bigint)
language plpgsql
stable
set search_path = pg_catalog
as $$
begin
  if p_manufacturers is null or cardinality(p_manufacturers) > 64
     or exists (select 1 from unnest(p_manufacturers) as m(name)
                 where m.name is null or char_length(m.name) not between 1 and 120) then
    raise exception 'catalog coverage request is out of bounds' using errcode = '22023';
  end if;
  return query
    select t.manufacturer, t.canonical_variants
      from (
        select requested.name as manufacturer, count(v.id)::bigint as canonical_variants
          from (select distinct m.name from unnest(p_manufacturers) as m(name)) as requested
          left join public.catalog_models cm on cm.manufacturer = requested.name
          left join public.catalog_model_variants v on v.model_id = cm.id
         group by requested.name
        union all
        select null::text, (select count(*) from public.catalog_model_variants)::bigint
      ) as t
     order by t.manufacturer collate "C" nulls last;
end;
$$;

-- ---------------------------------------------------------------------------
-- 6. RLS and privileges: service-path only.
-- ---------------------------------------------------------------------------
alter table public.catalog_work_scopes enable row level security;
alter table public.catalog_work_scope_revisions enable row level security;
-- No policies: browser roles have no access at all, exactly like every other
-- catalog relation.

do $$
declare fn text; relation text;
begin
  foreach fn in array array[
    'public.catalog_work_scope_contract_version()',
    'public.catalog_work_scope_text_digest(text)',
    'public.catalog_work_scope_record_valid(jsonb)',
    'public.forbid_work_scope_revision_mutation()',
    'public.work_scope_revision_in_sequence()',
    'public.forbid_work_scope_rewrite()',
    'public.work_scope_head_is_a_revision()',
    'public.work_scope_revision_row(uuid,integer,uuid,jsonb)',
    'public.create_work_scope(uuid,uuid,jsonb)',
    'public.revise_work_scope(uuid,integer,text,uuid,jsonb)',
    'public.catalog_canonical_manufacturer_coverage(text[])'
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

  foreach relation in array array[
    'public.catalog_work_scopes', 'public.catalog_work_scope_revisions'
  ] loop
    execute format('revoke all on table %s from public', relation);
    if exists (select 1 from pg_roles where rolname='anon') then
      execute format('revoke all on table %s from anon', relation);
    end if;
    if exists (select 1 from pg_roles where rolname='authenticated') then
      execute format('revoke all on table %s from authenticated', relation);
    end if;
    if exists (select 1 from pg_roles where rolname='service_role') then
      execute format('grant select, insert on table %s to service_role', relation);
      execute format('revoke delete on table %s from service_role', relation);
    end if;
  end loop;
  if exists (select 1 from pg_roles where rolname='service_role') then
    -- The head advances; a revision never changes. The triggers above bound
    -- what the one UPDATE grant may touch.
    execute 'grant update on table public.catalog_work_scopes to service_role';
    execute 'revoke update on table public.catalog_work_scope_revisions from service_role';
  end if;
end $$;

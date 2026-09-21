-- Immutable run identity, and the last unfenced worker-side write paths.
--
-- PART 1 -- WHAT A RUN IS, RECORDED ONCE
--
-- A run had no identity of its own. Every surface that needed to know what a
-- run WAS re-derived the answer later, from whatever was to hand: the worker
-- read the PROJECT's current `workflow_key` at claim time, the export
-- projection read the caller's own request metadata and otherwise defaulted to
-- the literal 'vehicle_catalog_v1', and the browser rendered a historical run
-- as whatever its project is today. So a project switched from V1 to V2
-- between a run's creation and its launch -- or between its first attempt and
-- a retry -- changed what that run was, and a V2 run could export as V1.
--
-- `runs.run_identity` is the durable answer, written in the SAME transaction that creates the run: the workflow key, the reviewed
-- engine version, the runtime policy version and fingerprint, the release SHA
-- the API was serving, and the event-vocabulary version the run's stream
-- speaks. `backend/run_identity.py` owns its shape; this migration owns the
-- two properties only the database can guarantee:
--
--   SET AT CREATION -- `create_message_and_run_v3` inserts the row and identity
--                atomically after checking the trusted project workflow.
--   NEVER REWRITTEN -- `runs_forbid_identity_rewrite` refuses ANY update that
--                changes a non-null `run_identity`, whatever path it arrives
--                through: an RPC, a direct service-role table write, or a
--                future writer nobody has written yet. An application guard is
--                advisory against a second writer; a trigger is not.
--
-- The column is NULLABLE on purpose. Production already holds runs created
-- before this release, including the prepared Government capture run, and a
-- NOT NULL column would either refuse the migration or require inventing an
-- identity for a run whose identity was never recorded -- which is exactly the
-- guessing this change exists to stop. An unpinned legacy run is readable and
-- resumable; it is simply not exportable and not release-authorizable, and the
-- code says so rather than defaulting it to V1.
--
-- PART 2 -- THE LAST UNFENCED WORKER WRITES
--
-- Every other worker-side durable mutation already travels through a
-- lease-guarded RPC (migrations 20260810000300 / 000600 / 20260823000100 /
-- 20260920000100 / 20260920000200). Three did not, and each one could mutate
-- run-owned state on behalf of a worker that no longer holds the lease:
--
--   * `tool_access_requests` -- inserted directly by the API route;
--   * `tool_grants`          -- inserted directly, AND it updates the
--                               referenced request's status to 'granted',
--                               so an unfenced grant mutated two tables;
--   * `run_usage_ledger`     -- appended directly by the worker's ledger
--                               recorder. Every OTHER usage write (runs.usage,
--                               run_execution_usage) is fenced; the per-call
--                               ledger row that the daily budget is summed
--                               from was not, so a replaced worker could keep
--                               charging a run it no longer owned.
--
-- All three now have guarded writers taking the same `run + attempt + worker
-- + lease` contract as every other guarded write, through the SAME
-- `assert_worker_lease`. No second fencing implementation is introduced.
--
-- Additive, idempotent, data-preserving. No table is dropped, no row deleted.
-- Service-path only, per the repository convention.

-- ---------------------------------------------------------------------------
-- 1) The identity column.
-- ---------------------------------------------------------------------------
alter table public.runs add column if not exists run_identity jsonb;

comment on column public.runs.run_identity is
  'Immutable identity established before execution (backend/run_identity.py): '
  'workflow key, engine version, runtime policy version/fingerprint, release '
  'SHA and event-registry version. Set once, never rewritten. NULL only for '
  'runs created before this column existed; later retrofit is forbidden.';

-- A stored identity must at least be an object naming the run it belongs to.
-- The record's full shape is validated in `backend/run_identity.py`, which is
-- where the policy fingerprint and the engine registry live; the constraint
-- here is the part the database can enforce on every path into the column.
-- TWO blocks, not one, and that is load-bearing. `ADD ... NOT VALID` governs
-- every future write without inspecting a single existing row, so it cannot
-- fail on legacy data and must not be wrapped in a handler that would discard
-- it. VALIDATE is the separate, retrospective step: it reads every row, and it
-- is the only part that a hypothetical malformed legacy row could fail. If
-- ADD and VALIDATE shared one block, a failed VALIDATE would roll the ADD back
-- with it and leave NO constraint at all -- the opposite of what the handler
-- is there to protect.
alter table public.runs drop constraint if exists runs_run_identity_shape_check;
alter table public.runs add constraint runs_run_identity_shape_check check (
  run_identity is null or (
    jsonb_typeof(run_identity) = 'object'
    and run_identity->>'identity_version' = 'milo-run-identity/1'
    and nullif(run_identity->>'policy_version', '') is not null
    and (run_identity->>'policy_fingerprint') ~ '^[0-9a-f]{64}

do $$
begin
  -- Every pre-existing row has run_identity IS NULL and therefore satisfies
  -- this, so validation is expected to succeed. If some row somehow cannot,
  -- losing the RETROSPECTIVE proof is acceptable; refusing to migrate
  -- production is not. The constraint added above stays in force either way.
  alter table public.runs validate constraint runs_run_identity_shape_check;
exception when others then
  null;
end $$;

-- ---------------------------------------------------------------------------
-- 2) Immutability, enforced where it actually holds.
-- ---------------------------------------------------------------------------
create or replace function public.runs_forbid_identity_rewrite()
returns trigger
language plpgsql
as $$
begin
  -- Identity is an INSERT-time fact. Any UPDATE that changes it is forbidden,
  -- including NULL -> value on a legacy row: retrofitting one later would
  -- invent history rather than preserve it.
  if new.run_identity is distinct from old.run_identity then
    raise exception 'RUN_IDENTITY_IMMUTABLE: run % identity cannot be changed after creation', old.id
      using errcode = '55000';
  end if;
  return new;
end;
$$;

drop trigger if exists runs_forbid_identity_rewrite on public.runs;
create trigger runs_forbid_identity_rewrite
  before update on public.runs
  for each row
  execute function public.runs_forbid_identity_rewrite();

-- Existing rows may legitimately be NULL because they predate this migration.
-- New rows may not. A BEFORE INSERT trigger gives exactly that distinction
-- without making harmless maintenance updates to legacy terminal rows fail.
create or replace function public.runs_require_identity_on_insert()
returns trigger
language plpgsql
as $
begin
  if new.run_identity is null then
    raise exception 'RUN_IDENTITY_REQUIRED: new runs must be born with immutable identity'
      using errcode = '23514';
  end if;
  return new;
end;
$;

drop trigger if exists runs_require_identity_on_insert on public.runs;
create trigger runs_require_identity_on_insert
  before insert on public.runs
  for each row
  execute function public.runs_require_identity_on_insert();

-- ---------------------------------------------------------------------------
-- 3) Atomic message + run creation WITH immutable identity.
-- ---------------------------------------------------------------------------
-- Identity is no longer a post-insert binder. It is part of the same
-- transaction that creates the run, so there is no durable queued row whose
-- engine can be decided later. The function verifies the caller-supplied
-- identity against the trusted conversation -> project workflow INSIDE the
-- transaction before inserting it.
--
-- Idempotent replay returns the already-created run unchanged, even if the
-- project's workflow has since changed. A historical run is defined by its
-- own identity, never by today's project setting.
drop function if exists public.bind_run_identity(uuid, jsonb);

create or replace function public.create_message_and_run_v3(
  p_run_id uuid,
  p_run_identity jsonb,
  p_conversation_id uuid,
  p_content text,
  p_metadata jsonb,
  p_requested_by uuid,
  p_idempotency_key text,
  p_request_fingerprint text,
  p_max_user_active integer default null,
  p_max_project_active integer default null
) returns jsonb
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_project uuid;
  v_workflow_key text;
  v_existing public.runs;
  v_message_id bigint;
  v_run public.runs;
  v_active integer;
begin
  select c.project_id, p.workflow_key
    into v_project, v_workflow_key
    from public.conversations c
    join public.projects p on p.id = c.project_id
   where c.id = p_conversation_id;
  if v_project is null then
    raise exception 'CONVERSATION_NOT_FOUND';
  end if;

  -- Serialize same-user and same-project admission exactly as the existing
  -- atomic creator does.
  perform pg_advisory_xact_lock(hashtext('milo_run_user_' || p_requested_by::text));
  perform pg_advisory_xact_lock(hashtext('milo_run_project_' || v_project::text));

  -- Replay is decided before consulting today's workflow: the existing row's
  -- immutable identity is the answer.
  if p_idempotency_key is not null then
    select * into v_existing from public.runs
     where conversation_id = p_conversation_id
       and requested_by = p_requested_by
       and idempotency_key = p_idempotency_key;
    if found then
      return jsonb_build_object('run', to_jsonb(v_existing), 'created', false);
    end if;
  end if;

  if p_run_identity is null or jsonb_typeof(p_run_identity) <> 'object' then
    raise exception 'RUN_IDENTITY_INVALID: identity must be an object'
      using errcode = '22023';
  end if;
  if (p_run_identity->>'run_id') is distinct from p_run_id::text then
    raise exception 'RUN_IDENTITY_INVALID: identity names a different run'
      using errcode = '22023';
  end if;
  if p_run_identity->>'workflow_key' = 'operator_capture' then
    if coalesce(p_metadata->>'milo_operation', '') <> 'catalog.government.capture' then
      raise exception 'RUN_IDENTITY_INVALID: operator capture identity requires the capture operation marker'
        using errcode = '22023';
    end if;
  elsif (p_run_identity->>'workflow_key') is distinct from v_workflow_key then
    raise exception 'RUN_IDENTITY_WORKFLOW_DRIFT: project workflow changed before creation'
      using errcode = '40001';
  end if;

  if p_max_user_active is not null then
    select count(*) into v_active from public.runs
     where requested_by = p_requested_by
       and status in ('queued','launching','starting','running','waiting','cancellation_requested');
    if v_active >= p_max_user_active then
      raise exception 'USER_CONCURRENCY_LIMIT';
    end if;
  end if;

  if p_max_project_active is not null then
    select count(*) into v_active from public.runs r
      join public.conversations c on c.id = r.conversation_id
     where c.project_id = v_project
       and r.status in ('queued','launching','starting','running','waiting','cancellation_requested');
    if v_active >= p_max_project_active then
      raise exception 'PROJECT_CONCURRENCY_LIMIT';
    end if;
  end if;

  insert into public.messages (conversation_id, role, content, metadata)
  values (p_conversation_id, 'user', p_content, coalesce(p_metadata, '{}'::jsonb))
  returning id into v_message_id;

  insert into public.runs
    (id, conversation_id, status, launch_state, requested_by,
     idempotency_key, request_fingerprint, input, run_identity)
  values (
    p_run_id, p_conversation_id, 'queued', 'pending', p_requested_by,
    p_idempotency_key, p_request_fingerprint,
    jsonb_build_object(
      'message_id', v_message_id::text,
      'content', p_content,
      'metadata', coalesce(p_metadata, '{}'::jsonb)
    ),
    p_run_identity
  )
  returning * into v_run;

  return jsonb_build_object('run', to_jsonb(v_run), 'created', true);
end;
$$;

revoke execute on function public.create_message_and_run_v3(
  uuid, jsonb, uuid, text, jsonb, uuid, text, text, integer, integer
) from public;
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    revoke execute on function public.create_message_and_run_v3(
      uuid, jsonb, uuid, text, jsonb, uuid, text, text, integer, integer
    ) from anon;
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    revoke execute on function public.create_message_and_run_v3(
      uuid, jsonb, uuid, text, jsonb, uuid, text, text, integer, integer
    ) from authenticated;
  end if;
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant execute on function public.create_message_and_run_v3(
      uuid, jsonb, uuid, text, jsonb, uuid, text, text, integer, integer
    ) to service_role;
  end if;
end $$;

-- ---------------------------------------------------------------------------
-- 4) The three remaining unfenced worker writes.
-- ---------------------------------------------------------------------------
create or replace function public.create_tool_access_request_guarded(
  p_run_id uuid, p_worker_id text, p_attempt integer, p_lease_token text,
  p_request jsonb
) returns setof public.tool_access_requests
language plpgsql
set search_path = pg_catalog
as $$
declare v_row public.tool_access_requests;
begin
  perform public.assert_worker_lease(p_run_id, p_worker_id, p_attempt, p_lease_token);
  insert into public.tool_access_requests (run_id, agent, tool, reason, scope, requested_limits, trigger)
  values (p_run_id, p_request->>'agent', p_request->>'tool', p_request->>'reason',
          coalesce(p_request->'scope', '{}'::jsonb),
          coalesce(p_request->'requested_limits', '{}'::jsonb),
          p_request->'trigger')
  returning * into v_row;
  return next v_row;
  return;
end;
$$;

create or replace function public.create_tool_grant_guarded(
  p_run_id uuid, p_worker_id text, p_attempt integer, p_lease_token text,
  p_grant jsonb
) returns setof public.tool_grants
language plpgsql
set search_path = pg_catalog
as $$
declare v_row public.tool_grants; v_request_id uuid;
begin
  perform public.assert_worker_lease(p_run_id, p_worker_id, p_attempt, p_lease_token);
  v_request_id := nullif(p_grant->>'request_id', '')::uuid;
  -- Both writes are in ONE function body and therefore one transaction: the
  -- request can never be marked granted for a grant that was not inserted.
  -- The request must belong to THIS run, so a grant cannot reach across runs.
  if v_request_id is not null then
    update public.tool_access_requests
       set status = 'granted'
     where id = v_request_id and run_id = p_run_id;
    if not found then
      raise exception 'TOOL_GRANT_INVALID: request % does not belong to run %', v_request_id, p_run_id
        using errcode = '23503';
    end if;
  end if;
  insert into public.tool_grants
    (run_id, request_id, agent, tool, max_searches, max_rounds, domains, expires_at, approver_policy)
  values (p_run_id, v_request_id, p_grant->>'agent', p_grant->>'tool',
          (p_grant->>'max_searches')::integer, (p_grant->>'max_rounds')::integer,
          case when p_grant ? 'domains' and jsonb_typeof(p_grant->'domains') = 'array'
               then (select array_agg(value::text) from jsonb_array_elements_text(p_grant->'domains') as t(value))
               else null end,
          (p_grant->>'expires_at')::timestamptz, p_grant->>'approver_policy')
  returning * into v_row;
  return next v_row;
  return;
end;
$$;

create or replace function public.append_usage_ledger_guarded(
  p_run_id uuid, p_worker_id text, p_attempt integer, p_lease_token text,
  p_entry jsonb
) returns setof public.run_usage_ledger
language plpgsql
set search_path = pg_catalog
as $$
declare v_row public.run_usage_ledger;
begin
  perform public.assert_worker_lease(p_run_id, p_worker_id, p_attempt, p_lease_token);
  -- The run id comes from the FENCED argument, never from the payload: an
  -- entry naming another run would otherwise charge a run this lease does not
  -- own.
  insert into public.run_usage_ledger
    (run_id, project_id, user_id, provider, model, call_seq, decision, rejection_reason,
     reserved_input_tokens, reserved_output_tokens, actual_input_tokens, actual_output_tokens,
     estimated_cost, actual_cost)
  values (p_run_id,
          nullif(p_entry->>'project_id', '')::uuid,
          nullif(p_entry->>'user_id', '')::uuid,
          coalesce(nullif(p_entry->>'provider', ''), 'moonshot'),
          coalesce(nullif(p_entry->>'model', ''), 'kimi'),
          -- The NOT NULL DEFAULT columns keep their defaults when the entry
          -- does not state them. The unfenced Python path built its payload
          -- from the keys that were present, so an absent value meant "use
          -- the column default"; passing an explicit NULL here instead would
          -- turn that into a constraint violation on the first reservation
          -- the worker records.
          coalesce(nullif(p_entry->>'call_seq', '')::integer, 0),
          nullif(p_entry->>'decision', ''),
          nullif(p_entry->>'rejection_reason', ''),
          coalesce(nullif(p_entry->>'reserved_input_tokens', '')::integer, 0),
          coalesce(nullif(p_entry->>'reserved_output_tokens', '')::integer, 0),
          nullif(p_entry->>'actual_input_tokens', '')::integer,
          nullif(p_entry->>'actual_output_tokens', '')::integer,
          nullif(p_entry->>'estimated_cost', '')::numeric,
          nullif(p_entry->>'actual_cost', '')::numeric)
  returning * into v_row;
  return next v_row;
  return;
end;
$$;

-- ---------------------------------------------------------------------------
-- 5) Service-path-only ACLs, per the repository convention.
-- ---------------------------------------------------------------------------
do $$
declare fn text;
begin
  foreach fn in array array[
    'public.create_tool_access_request_guarded(uuid, text, integer, text, jsonb)',
    'public.create_tool_grant_guarded(uuid, text, integer, text, jsonb)',
    'public.append_usage_ledger_guarded(uuid, text, integer, text, jsonb)'
  ]
  loop
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
end $$;

    and nullif(run_identity->>'event_registry_version', '') is not null
    and run_identity ? 'release_sha'
    and (
      run_identity->>'release_sha' = ''
      or (run_identity->>'release_sha') ~ '^[0-9a-f]{40}

do $$
begin
  -- Every pre-existing row has run_identity IS NULL and therefore satisfies
  -- this, so validation is expected to succeed. If some row somehow cannot,
  -- losing the RETROSPECTIVE proof is acceptable; refusing to migrate
  -- production is not. The constraint added above stays in force either way.
  alter table public.runs validate constraint runs_run_identity_shape_check;
exception when others then
  null;
end $$;

-- ---------------------------------------------------------------------------
-- 2) Immutability, enforced where it actually holds.
-- ---------------------------------------------------------------------------
create or replace function public.runs_forbid_identity_rewrite()
returns trigger
language plpgsql
as $$
begin
  -- `is distinct from` so a null-to-value first write passes and a
  -- value-to-null erasure does not: dropping an identity is a rewrite too.
  if old.run_identity is not null and new.run_identity is distinct from old.run_identity then
    raise exception 'RUN_IDENTITY_IMMUTABLE: run % already has an identity and it cannot be changed', old.id
      using errcode = '55000';
  end if;
  return new;
end;
$$;

drop trigger if exists runs_forbid_identity_rewrite on public.runs;
create trigger runs_forbid_identity_rewrite
  before update on public.runs
  for each row
  execute function public.runs_forbid_identity_rewrite();

-- ---------------------------------------------------------------------------
-- 3) The set-once binder.
-- ---------------------------------------------------------------------------
-- Called by the API immediately after the run row exists and BEFORE any worker
-- launch, so the identity is established before execution. It is deliberately
-- NOT a worker-lease-guarded RPC: at bind time no worker has claimed the run,
-- and making the API present a lease it cannot hold would mean no identity
-- could be bound at all.
create or replace function public.bind_run_identity(
  p_run_id uuid,
  p_identity jsonb
) returns setof public.runs
language plpgsql
as $$
declare
  v_row public.runs;
  v_current jsonb;
begin
  if p_identity is null or jsonb_typeof(p_identity) <> 'object' then
    raise exception 'RUN_IDENTITY_INVALID: a run identity must be an object'
      using errcode = '22023';
  end if;
  if (p_identity->>'run_id') is distinct from p_run_id::text then
    raise exception 'RUN_IDENTITY_INVALID: the identity names a different run'
      using errcode = '22023';
  end if;

  -- One statement, so two concurrent binds cannot both see a null column.
  update public.runs
     set run_identity = p_identity,
         updated_at = now()
   where id = p_run_id
     and run_identity is null
   returning * into v_row;
  if v_row.id is not null then
    return next v_row;
    return;
  end if;

  -- Either the run does not exist, or it is already bound. Only an IDENTICAL
  -- re-bind is a no-op; a different record is a rewrite and is refused here
  -- exactly as the trigger would refuse it.
  select run_identity into v_current from public.runs where id = p_run_id;
  if not found then
    raise exception 'RUN_NOT_FOUND: run % does not exist', p_run_id
      using errcode = '22023';
  end if;
  if v_current is distinct from p_identity then
    raise exception 'RUN_IDENTITY_IMMUTABLE: run % already has a different identity', p_run_id
      using errcode = '55000';
  end if;
  select * into v_row from public.runs where id = p_run_id;
  return next v_row;
  return;
end;
$$;

-- ---------------------------------------------------------------------------
-- 4) The three remaining unfenced worker writes.
-- ---------------------------------------------------------------------------
create or replace function public.create_tool_access_request_guarded(
  p_run_id uuid, p_worker_id text, p_attempt integer, p_lease_token text,
  p_request jsonb
) returns setof public.tool_access_requests
language plpgsql
set search_path = pg_catalog
as $$
declare v_row public.tool_access_requests;
begin
  perform public.assert_worker_lease(p_run_id, p_worker_id, p_attempt, p_lease_token);
  insert into public.tool_access_requests (run_id, agent, tool, reason, scope, requested_limits, trigger)
  values (p_run_id, p_request->>'agent', p_request->>'tool', p_request->>'reason',
          coalesce(p_request->'scope', '{}'::jsonb),
          coalesce(p_request->'requested_limits', '{}'::jsonb),
          p_request->'trigger')
  returning * into v_row;
  return next v_row;
  return;
end;
$$;

create or replace function public.create_tool_grant_guarded(
  p_run_id uuid, p_worker_id text, p_attempt integer, p_lease_token text,
  p_grant jsonb
) returns setof public.tool_grants
language plpgsql
set search_path = pg_catalog
as $$
declare v_row public.tool_grants; v_request_id uuid;
begin
  perform public.assert_worker_lease(p_run_id, p_worker_id, p_attempt, p_lease_token);
  v_request_id := nullif(p_grant->>'request_id', '')::uuid;
  -- Both writes are in ONE function body and therefore one transaction: the
  -- request can never be marked granted for a grant that was not inserted.
  -- The request must belong to THIS run, so a grant cannot reach across runs.
  if v_request_id is not null then
    update public.tool_access_requests
       set status = 'granted'
     where id = v_request_id and run_id = p_run_id;
    if not found then
      raise exception 'TOOL_GRANT_INVALID: request % does not belong to run %', v_request_id, p_run_id
        using errcode = '23503';
    end if;
  end if;
  insert into public.tool_grants
    (run_id, request_id, agent, tool, max_searches, max_rounds, domains, expires_at, approver_policy)
  values (p_run_id, v_request_id, p_grant->>'agent', p_grant->>'tool',
          (p_grant->>'max_searches')::integer, (p_grant->>'max_rounds')::integer,
          case when p_grant ? 'domains' and jsonb_typeof(p_grant->'domains') = 'array'
               then (select array_agg(value::text) from jsonb_array_elements_text(p_grant->'domains') as t(value))
               else null end,
          (p_grant->>'expires_at')::timestamptz, p_grant->>'approver_policy')
  returning * into v_row;
  return next v_row;
  return;
end;
$$;

create or replace function public.append_usage_ledger_guarded(
  p_run_id uuid, p_worker_id text, p_attempt integer, p_lease_token text,
  p_entry jsonb
) returns setof public.run_usage_ledger
language plpgsql
set search_path = pg_catalog
as $$
declare v_row public.run_usage_ledger;
begin
  perform public.assert_worker_lease(p_run_id, p_worker_id, p_attempt, p_lease_token);
  -- The run id comes from the FENCED argument, never from the payload: an
  -- entry naming another run would otherwise charge a run this lease does not
  -- own.
  insert into public.run_usage_ledger
    (run_id, project_id, user_id, provider, model, call_seq, decision, rejection_reason,
     reserved_input_tokens, reserved_output_tokens, actual_input_tokens, actual_output_tokens,
     estimated_cost, actual_cost)
  values (p_run_id,
          nullif(p_entry->>'project_id', '')::uuid,
          nullif(p_entry->>'user_id', '')::uuid,
          coalesce(nullif(p_entry->>'provider', ''), 'moonshot'),
          coalesce(nullif(p_entry->>'model', ''), 'kimi'),
          -- The NOT NULL DEFAULT columns keep their defaults when the entry
          -- does not state them. The unfenced Python path built its payload
          -- from the keys that were present, so an absent value meant "use
          -- the column default"; passing an explicit NULL here instead would
          -- turn that into a constraint violation on the first reservation
          -- the worker records.
          coalesce(nullif(p_entry->>'call_seq', '')::integer, 0),
          nullif(p_entry->>'decision', ''),
          nullif(p_entry->>'rejection_reason', ''),
          coalesce(nullif(p_entry->>'reserved_input_tokens', '')::integer, 0),
          coalesce(nullif(p_entry->>'reserved_output_tokens', '')::integer, 0),
          nullif(p_entry->>'actual_input_tokens', '')::integer,
          nullif(p_entry->>'actual_output_tokens', '')::integer,
          nullif(p_entry->>'estimated_cost', '')::numeric,
          nullif(p_entry->>'actual_cost', '')::numeric)
  returning * into v_row;
  return next v_row;
  return;
end;
$$;

-- ---------------------------------------------------------------------------
-- 5) Service-path-only ACLs, per the repository convention.
-- ---------------------------------------------------------------------------
do $$
declare fn text;
begin
  foreach fn in array array[
    'public.bind_run_identity(uuid, jsonb)',
    'public.create_tool_access_request_guarded(uuid, text, integer, text, jsonb)',
    'public.create_tool_grant_guarded(uuid, text, integer, text, jsonb)',
    'public.append_usage_ledger_guarded(uuid, text, integer, text, jsonb)'
  ]
  loop
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
end $$;

    )
    and (
      (run_identity->>'workflow_key' = 'vehicle_catalog_v1'
       and run_identity->>'engine_version' = 'vehicle_catalog_v1.stage3')
      or
      (run_identity->>'workflow_key' = 'swarm_v2'
       and run_identity->>'engine_version' = 'swarm_v2.1')
      or
      (run_identity->>'workflow_key' = 'operator_capture'
       and run_identity->>'engine_version' = 'operator_capture.1')
    )
    and (run_identity->>'run_id')::uuid = id
  )
) not valid;

do $$
begin
  -- Every pre-existing row has run_identity IS NULL and therefore satisfies
  -- this, so validation is expected to succeed. If some row somehow cannot,
  -- losing the RETROSPECTIVE proof is acceptable; refusing to migrate
  -- production is not. The constraint added above stays in force either way.
  alter table public.runs validate constraint runs_run_identity_shape_check;
exception when others then
  null;
end $$;

-- ---------------------------------------------------------------------------
-- 2) Immutability, enforced where it actually holds.
-- ---------------------------------------------------------------------------
create or replace function public.runs_forbid_identity_rewrite()
returns trigger
language plpgsql
as $$
begin
  -- `is distinct from` so a null-to-value first write passes and a
  -- value-to-null erasure does not: dropping an identity is a rewrite too.
  if old.run_identity is not null and new.run_identity is distinct from old.run_identity then
    raise exception 'RUN_IDENTITY_IMMUTABLE: run % already has an identity and it cannot be changed', old.id
      using errcode = '55000';
  end if;
  return new;
end;
$$;

drop trigger if exists runs_forbid_identity_rewrite on public.runs;
create trigger runs_forbid_identity_rewrite
  before update on public.runs
  for each row
  execute function public.runs_forbid_identity_rewrite();

-- ---------------------------------------------------------------------------
-- 3) The set-once binder.
-- ---------------------------------------------------------------------------
-- Called by the API immediately after the run row exists and BEFORE any worker
-- launch, so the identity is established before execution. It is deliberately
-- NOT a worker-lease-guarded RPC: at bind time no worker has claimed the run,
-- and making the API present a lease it cannot hold would mean no identity
-- could be bound at all.
create or replace function public.bind_run_identity(
  p_run_id uuid,
  p_identity jsonb
) returns setof public.runs
language plpgsql
as $$
declare
  v_row public.runs;
  v_current jsonb;
begin
  if p_identity is null or jsonb_typeof(p_identity) <> 'object' then
    raise exception 'RUN_IDENTITY_INVALID: a run identity must be an object'
      using errcode = '22023';
  end if;
  if (p_identity->>'run_id') is distinct from p_run_id::text then
    raise exception 'RUN_IDENTITY_INVALID: the identity names a different run'
      using errcode = '22023';
  end if;

  -- One statement, so two concurrent binds cannot both see a null column.
  update public.runs
     set run_identity = p_identity,
         updated_at = now()
   where id = p_run_id
     and run_identity is null
   returning * into v_row;
  if v_row.id is not null then
    return next v_row;
    return;
  end if;

  -- Either the run does not exist, or it is already bound. Only an IDENTICAL
  -- re-bind is a no-op; a different record is a rewrite and is refused here
  -- exactly as the trigger would refuse it.
  select run_identity into v_current from public.runs where id = p_run_id;
  if not found then
    raise exception 'RUN_NOT_FOUND: run % does not exist', p_run_id
      using errcode = '22023';
  end if;
  if v_current is distinct from p_identity then
    raise exception 'RUN_IDENTITY_IMMUTABLE: run % already has a different identity', p_run_id
      using errcode = '55000';
  end if;
  select * into v_row from public.runs where id = p_run_id;
  return next v_row;
  return;
end;
$$;

-- ---------------------------------------------------------------------------
-- 4) The three remaining unfenced worker writes.
-- ---------------------------------------------------------------------------
create or replace function public.create_tool_access_request_guarded(
  p_run_id uuid, p_worker_id text, p_attempt integer, p_lease_token text,
  p_request jsonb
) returns setof public.tool_access_requests
language plpgsql
set search_path = pg_catalog
as $$
declare v_row public.tool_access_requests;
begin
  perform public.assert_worker_lease(p_run_id, p_worker_id, p_attempt, p_lease_token);
  insert into public.tool_access_requests (run_id, agent, tool, reason, scope, requested_limits, trigger)
  values (p_run_id, p_request->>'agent', p_request->>'tool', p_request->>'reason',
          coalesce(p_request->'scope', '{}'::jsonb),
          coalesce(p_request->'requested_limits', '{}'::jsonb),
          p_request->'trigger')
  returning * into v_row;
  return next v_row;
  return;
end;
$$;

create or replace function public.create_tool_grant_guarded(
  p_run_id uuid, p_worker_id text, p_attempt integer, p_lease_token text,
  p_grant jsonb
) returns setof public.tool_grants
language plpgsql
set search_path = pg_catalog
as $$
declare v_row public.tool_grants; v_request_id uuid;
begin
  perform public.assert_worker_lease(p_run_id, p_worker_id, p_attempt, p_lease_token);
  v_request_id := nullif(p_grant->>'request_id', '')::uuid;
  -- Both writes are in ONE function body and therefore one transaction: the
  -- request can never be marked granted for a grant that was not inserted.
  -- The request must belong to THIS run, so a grant cannot reach across runs.
  if v_request_id is not null then
    update public.tool_access_requests
       set status = 'granted'
     where id = v_request_id and run_id = p_run_id;
    if not found then
      raise exception 'TOOL_GRANT_INVALID: request % does not belong to run %', v_request_id, p_run_id
        using errcode = '23503';
    end if;
  end if;
  insert into public.tool_grants
    (run_id, request_id, agent, tool, max_searches, max_rounds, domains, expires_at, approver_policy)
  values (p_run_id, v_request_id, p_grant->>'agent', p_grant->>'tool',
          (p_grant->>'max_searches')::integer, (p_grant->>'max_rounds')::integer,
          case when p_grant ? 'domains' and jsonb_typeof(p_grant->'domains') = 'array'
               then (select array_agg(value::text) from jsonb_array_elements_text(p_grant->'domains') as t(value))
               else null end,
          (p_grant->>'expires_at')::timestamptz, p_grant->>'approver_policy')
  returning * into v_row;
  return next v_row;
  return;
end;
$$;

create or replace function public.append_usage_ledger_guarded(
  p_run_id uuid, p_worker_id text, p_attempt integer, p_lease_token text,
  p_entry jsonb
) returns setof public.run_usage_ledger
language plpgsql
set search_path = pg_catalog
as $$
declare v_row public.run_usage_ledger;
begin
  perform public.assert_worker_lease(p_run_id, p_worker_id, p_attempt, p_lease_token);
  -- The run id comes from the FENCED argument, never from the payload: an
  -- entry naming another run would otherwise charge a run this lease does not
  -- own.
  insert into public.run_usage_ledger
    (run_id, project_id, user_id, provider, model, call_seq, decision, rejection_reason,
     reserved_input_tokens, reserved_output_tokens, actual_input_tokens, actual_output_tokens,
     estimated_cost, actual_cost)
  values (p_run_id,
          nullif(p_entry->>'project_id', '')::uuid,
          nullif(p_entry->>'user_id', '')::uuid,
          coalesce(nullif(p_entry->>'provider', ''), 'moonshot'),
          coalesce(nullif(p_entry->>'model', ''), 'kimi'),
          -- The NOT NULL DEFAULT columns keep their defaults when the entry
          -- does not state them. The unfenced Python path built its payload
          -- from the keys that were present, so an absent value meant "use
          -- the column default"; passing an explicit NULL here instead would
          -- turn that into a constraint violation on the first reservation
          -- the worker records.
          coalesce(nullif(p_entry->>'call_seq', '')::integer, 0),
          nullif(p_entry->>'decision', ''),
          nullif(p_entry->>'rejection_reason', ''),
          coalesce(nullif(p_entry->>'reserved_input_tokens', '')::integer, 0),
          coalesce(nullif(p_entry->>'reserved_output_tokens', '')::integer, 0),
          nullif(p_entry->>'actual_input_tokens', '')::integer,
          nullif(p_entry->>'actual_output_tokens', '')::integer,
          nullif(p_entry->>'estimated_cost', '')::numeric,
          nullif(p_entry->>'actual_cost', '')::numeric)
  returning * into v_row;
  return next v_row;
  return;
end;
$$;

-- ---------------------------------------------------------------------------
-- 5) Service-path-only ACLs, per the repository convention.
-- ---------------------------------------------------------------------------
do $$
declare fn text;
begin
  foreach fn in array array[
    'public.bind_run_identity(uuid, jsonb)',
    'public.create_tool_access_request_guarded(uuid, text, integer, text, jsonb)',
    'public.create_tool_grant_guarded(uuid, text, integer, text, jsonb)',
    'public.append_usage_ledger_guarded(uuid, text, integer, text, jsonb)'
  ]
  loop
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
end $$;

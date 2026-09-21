-- Immutable run identity, atomic run creation, and the last unfenced worker writes.
--
-- Console 6 rule: a run is born with ONE immutable identity. Existing legacy
-- rows may remain NULL as historical records, but no new run may be inserted
-- without identity and no UPDATE may add/change/remove identity later.
--
-- Product execution may use only vehicle_catalog_v1 / swarm_v2 identities.
-- operator_capture is a distinct non-product control-plane identity so the
-- reviewed Government capture flow can use the same durable run/lease model
-- without ever becoming routable as a model workflow.

-- ---------------------------------------------------------------------------
-- 1) Identity column + full future-write shape.
-- ---------------------------------------------------------------------------
alter table public.runs add column if not exists run_identity jsonb;

comment on column public.runs.run_identity is
  'Immutable INSERT-time identity (backend/run_identity.py): run/workflow/engine, '
  'runtime policy version/fingerprint, release SHA, and event-registry '
  'version/fingerprint. NULL only for legacy rows created before Console 6.';

alter table public.runs drop constraint if exists runs_run_identity_shape_check;
alter table public.runs add constraint runs_run_identity_shape_check check (
  run_identity is null or (
    jsonb_typeof(run_identity) = 'object'
    and run_identity->>'identity_version' = 'milo-run-identity/1'
    and nullif(run_identity->>'policy_version', '') is not null
    and (run_identity->>'policy_fingerprint') ~ '^[0-9a-f]{64}$'
    and nullif(run_identity->>'event_registry_version', '') is not null
    and (run_identity->>'event_registry_fingerprint') ~ '^[0-9a-f]{64}$'
    and run_identity ? 'release_sha'
    and (run_identity->>'release_sha') ~ '^[0-9a-f]{40}$'
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
  alter table public.runs validate constraint runs_run_identity_shape_check;
exception when others then
  -- Future writes are still governed by the NOT VALID constraint. Existing
  -- legacy NULL rows are expected to validate; an unexpected historical row
  -- does not justify dropping future-write protection.
  null;
end $$;

-- ---------------------------------------------------------------------------
-- 2) Identity is immutable after INSERT, including legacy NULL -> value.
-- ---------------------------------------------------------------------------
create or replace function public.runs_forbid_identity_rewrite()
returns trigger
language plpgsql
set search_path = pg_catalog
as $$
begin
  if new.run_identity is distinct from old.run_identity then
    raise exception 'RUN_IDENTITY_IMMUTABLE: run % identity cannot change after creation', old.id
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

-- Every NEW run must already carry identity, and product identity must agree
-- with the trusted conversation -> project workflow on every INSERT path.
create or replace function public.runs_require_identity_on_insert()
returns trigger
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_workflow_key text;
begin
  if new.run_identity is null then
    raise exception 'RUN_IDENTITY_REQUIRED: new runs must be born with immutable identity'
      using errcode = '23514';
  end if;

  select p.workflow_key
    into v_workflow_key
    from public.conversations c
    join public.projects p on p.id = c.project_id
   where c.id = new.conversation_id;

  if v_workflow_key is null then
    raise exception 'RUN_IDENTITY_INVALID: run conversation has no trusted project workflow'
      using errcode = '23514';
  end if;

  if new.run_identity->>'workflow_key' = 'operator_capture' then
    if coalesce(new.input->'metadata'->>'milo_operation', '') <> 'catalog.government.capture' then
      raise exception 'RUN_IDENTITY_INVALID: operator capture identity requires the capture marker'
        using errcode = '23514';
    end if;
  elsif (new.run_identity->>'workflow_key') is distinct from v_workflow_key then
    raise exception 'RUN_IDENTITY_WORKFLOW_DRIFT: identity does not match trusted project workflow'
      using errcode = '23514';
  end if;

  return new;
end;
$$;

drop trigger if exists runs_require_identity_on_insert on public.runs;
create trigger runs_require_identity_on_insert
  before insert on public.runs
  for each row
  execute function public.runs_require_identity_on_insert();

-- Remove superseded run-identity/run-creation primitives if a preview/staging
-- database received an earlier form of this still-unmerged sequence. The old
-- creators cannot satisfy the INSERT-time identity trigger and must not remain
-- as dead alternate authorities beside V3.
drop function if exists public.bind_run_identity(uuid, jsonb);
drop function if exists public.create_message_and_run(
  uuid, text, jsonb, uuid, text, text, integer, integer
);
drop function if exists public.create_message_and_run_v2(
  uuid, text, jsonb, uuid, text, text, integer, integer
);
drop function if exists public.create_message_and_run_v3(
  uuid, jsonb, uuid, text, jsonb, uuid, text, text, integer, integer
);

-- ---------------------------------------------------------------------------
-- 3) Atomic user message + run + immutable identity.
-- ---------------------------------------------------------------------------
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
)
-- SETOF, not jsonb. The pinned supabase-py/postgrest-py client validates an
-- RPC response body as a LIST (`APIResponse.data: List[JSON]`) and rejects a
-- bare JSON object -- client-side, AFTER this transaction has committed the
-- message, the run and its immutable identity. A scalar return would therefore
-- create the run and then report failure to the caller, which is the exact
-- split-brain `test_every_http_facing_rpc_returns_a_set` exists to prevent.
returns setof jsonb
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

  perform pg_advisory_xact_lock(hashtext('milo_run_user_' || p_requested_by::text));
  perform pg_advisory_xact_lock(hashtext('milo_run_project_' || v_project::text));

  if p_request_fingerprint is null or btrim(p_request_fingerprint) = '' then
    raise exception 'IDEMPOTENCY_FINGERPRINT_REQUIRED'
      using errcode = '22023';
  end if;

  -- Idempotent replay returns the original immutable run before consulting
  -- today's project workflow; a later project edit cannot redefine history.
  -- The key is only idempotent for the SAME logical request. Enforce that in
  -- the transaction itself so every caller (website, operator capture, future
  -- service code) gets one contract instead of relying on an API-layer check.
  if p_idempotency_key is not null then
    select * into v_existing
      from public.runs
     where conversation_id = p_conversation_id
       and requested_by = p_requested_by
       and idempotency_key = p_idempotency_key;
    if found then
      if v_existing.request_fingerprint is null
         or v_existing.request_fingerprint is distinct from p_request_fingerprint then
        raise exception 'IDEMPOTENCY_CONFLICT'
          using errcode = '23505';
      end if;
      return next jsonb_build_object('run', to_jsonb(v_existing), 'created', false);
      return;
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
      raise exception 'RUN_IDENTITY_INVALID: operator capture identity requires the capture marker'
        using errcode = '22023';
    end if;
  elsif (p_run_identity->>'workflow_key') is distinct from v_workflow_key then
    raise exception 'RUN_IDENTITY_WORKFLOW_DRIFT: project workflow changed before creation'
      using errcode = '40001';
  end if;

  if p_max_user_active is not null then
    select count(*) into v_active
      from public.runs
     where requested_by = p_requested_by
       and status in ('queued','launching','starting','running','waiting','cancellation_requested');
    if v_active >= p_max_user_active then
      raise exception 'USER_CONCURRENCY_LIMIT';
    end if;
  end if;

  if p_max_project_active is not null then
    select count(*) into v_active
      from public.runs r
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

  return next jsonb_build_object('run', to_jsonb(v_run), 'created', true);
  return;
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
-- 5) Lease acquisition consumes immutable identity too. Legacy rows with no
--    identity are history and cannot become executable merely because a
--    service-role caller invokes the lease primitive directly.
-- ---------------------------------------------------------------------------
create or replace function public.claim_run_lease(
  p_run_id uuid,
  p_worker_id text,
  p_lease_seconds integer default 300
) returns setof public.runs
language sql
as $$
  update public.runs set
    status = case when status = 'cancellation_requested' then status else 'starting' end,
    attempt = case
      when worker_id is not null and worker_id <> p_worker_id
           and lease_expires_at is not null and lease_expires_at < now()
      then coalesce(attempt, 1) + 1
      else coalesce(attempt, 1)
    end,
    worker_id = p_worker_id,
    lease_token = encode(gen_random_bytes(32), 'hex'),
    lease_expires_at = now() + make_interval(secs => p_lease_seconds),
    started_at = coalesce(started_at, now()),
    updated_at = now()
  where id = p_run_id
    and run_identity is not null
    and (
      (status in ('queued','launching','waiting','cancellation_requested')
        and (worker_id is null or worker_id = p_worker_id
             or lease_expires_at is null or lease_expires_at < now()))
      or (status in ('starting','running')
        and (worker_id = p_worker_id or lease_expires_at is null or lease_expires_at < now()))
    )
  returning *;
$$;

-- ---------------------------------------------------------------------------
-- 6) Terminalization authority: the general worker transition RPC is
--    deliberately NON-TERMINAL. Console 3's finalize_run_guarded is the only
--    worker primitive allowed to commit a terminal status + terminal evidence
--    atomically.
-- ---------------------------------------------------------------------------
create or replace function public.transition_run_worker_guarded(
  p_run_id uuid,
  p_status text,
  p_expected_status text,
  p_worker_id text,
  p_attempt integer,
  p_lease_token text,
  p_output jsonb default null,
  p_error jsonb default null,
  p_clear_error boolean default false,
  p_usage jsonb default null,
  p_started_at timestamptz default null,
  p_finished_at timestamptz default null
) returns setof public.runs
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_row public.runs;
begin
  if p_status in ('completed', 'partial_success', 'failed', 'cancelled',
                  'timed_out', 'budget_exhausted') then
    raise exception 'CANONICAL_FINALIZER_REQUIRED: terminal status % must use finalize_run_guarded', p_status
      using errcode = '55000';
  end if;

  -- Console 6 fencing is the full run + attempt + worker + lease contract.
  -- NULL attempt/token must never weaken this into "check only the fields that
  -- happened to be supplied". The canonical assertion refuses missing,
  -- expired or replaced ownership before the mutation, and the exact WHERE
  -- below closes the concurrent-reclaim window in the write itself.
  perform public.assert_worker_lease(
    p_run_id, p_worker_id, p_attempt, p_lease_token
  );

  update public.runs
     set status = p_status,
         output = coalesce(p_output, output),
         error = case when p_clear_error then null else coalesce(p_error, error) end,
         usage = case when p_usage is null then usage
                      else public.merge_execution_usage(usage, p_usage) end,
         started_at = coalesce(p_started_at, started_at),
         finished_at = coalesce(p_finished_at, finished_at),
         updated_at = now()
   where id = p_run_id
     and worker_id = p_worker_id
     and attempt = p_attempt
     and lease_token = p_lease_token
     and lease_expires_at > now()
     and (p_expected_status is null or status = p_expected_status)
   returning * into v_row;

  if v_row is null then
    raise exception 'STALE_WORKER_WRITE: transition to % rejected; lease (worker %, attempt %) is not current for run %',
      p_status, p_worker_id, p_attempt, p_run_id
      using errcode = '55000';
  end if;

  return next v_row;
  return;
end;
$$;

-- ---------------------------------------------------------------------------
-- 7) Service-path-only ACLs, per the repository convention.
-- ---------------------------------------------------------------------------
do $$
declare fn text;
begin
  foreach fn in array array[
    'public.create_tool_access_request_guarded(uuid, text, integer, text, jsonb)',
    'public.create_tool_grant_guarded(uuid, text, integer, text, jsonb)',
    'public.append_usage_ledger_guarded(uuid, text, integer, text, jsonb)',
    'public.claim_run_lease(uuid, text, integer)',
    'public.transition_run_worker_guarded(uuid, text, text, text, integer, text, jsonb, jsonb, boolean, jsonb, timestamptz, timestamptz)'
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

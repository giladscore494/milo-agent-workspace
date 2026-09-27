-- E': prepare ONE Mapping Plan revision from the website (plan decision 23).
--
-- Why
-- ---
--
-- A revision is prepared by executing the EXISTING capture Cloud Run job with
-- the scoped entrypoint arguments (`backend/capture_invocation.py`). Until now
-- only an operator in Cloud Shell could do that. The API's Prepare route now
-- does exactly the same execution, and this migration is what makes that
-- route safe to call twice, from two browsers, at the same moment:
--
--   catalog_work_scope_preparation_requests
--                    ONE row per (plan, revision): who asked, which attempt,
--                    the operator capture run it prepared, and whether the job
--                    execution was triggered. The UNIQUE (plan, revision) row,
--                    claimed under the plan's row lock, is what lets exactly
--                    one caller trigger a capture; every other caller is
--                    answered with that row.
--   request_work_scope_preparation()
--                    the claim: stale / prepared / existing / claimed
--   record_work_scope_preparation_trigger()
--                    the claimer's compare-and-set of what it did (the run it
--                    made, then the trigger's outcome), for ITS attempt only
--   work_scope_preparation_state()
--                    the durable facts a status is derived from: the plan's
--                    head, the preparation, the request, the capture run's
--                    status (never its logs), and the known-unresolved count
--                    of the plan's latest preparation
--
-- What it never does
-- ------------------
--
-- It prepares nothing: the queue is still written only by
-- `prepare_work_scope_queue`, under an operator capture run's lease, inside
-- the capture job. It starts no batch, creates no product run, reads no
-- Government source and calls no model. Additive and forward-only: one new
-- relation, three new functions. Rerun-safe.

-- ---------------------------------------------------------------------------
-- 1. The relation.
-- ---------------------------------------------------------------------------

create table if not exists public.catalog_work_scope_preparation_requests (
  id uuid primary key default gen_random_uuid(),
  work_scope_id uuid not null references public.catalog_work_scopes(id) on delete restrict,
  revision integer not null,
  scope_digest text not null,
  -- Moves only by a retry the claim allows (a trigger that failed, a run that
  -- ended without preparing, or one that never started within the grace).
  attempt integer not null default 1,
  -- An audit fact, not a foreign key, exactly like the plan's `created_by`.
  requested_by uuid not null,
  -- The operator capture run THIS attempt prepares with. Null until the
  -- claimer has made (or reused) it.
  run_id uuid references public.runs(id) on delete restrict,
  trigger_state text not null default 'claimed',
  -- The Cloud Run execution (or its operation) the trigger answered with.
  execution_name text,
  claimed_at timestamptz not null default now(),
  triggered_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint catalog_work_scope_preparation_requests_revision_fk
    foreign key (work_scope_id, revision)
    references public.catalog_work_scope_revisions(work_scope_id, revision) on delete restrict,
  constraint catalog_work_scope_preparation_requests_digest_shape
    check (scope_digest ~ '^[0-9a-f]{64}$'),
  constraint catalog_work_scope_preparation_requests_attempt
    check (attempt between 1 and 1000),
  constraint catalog_work_scope_preparation_requests_trigger_state
    check (trigger_state in ('claimed', 'triggered', 'trigger_failed', 'trigger_unknown')),
  constraint catalog_work_scope_preparation_requests_triggered_has_run
    check (trigger_state in ('claimed', 'trigger_failed') or run_id is not null),
  constraint catalog_work_scope_preparation_requests_execution_shape
    check (execution_name is null
           or (char_length(execution_name) between 1 and 300
               and execution_name ~ '^[A-Za-z0-9][A-Za-z0-9/._-]*$'))
);
-- ONE request per revision: the idempotency identity of the Prepare route.
-- A revision is immutable, so (plan, revision) names its digest too.
create unique index if not exists catalog_work_scope_preparation_requests_revision_uidx
  on public.catalog_work_scope_preparation_requests(work_scope_id, revision);
create index if not exists catalog_work_scope_preparation_requests_run_idx
  on public.catalog_work_scope_preparation_requests(run_id);

-- ---------------------------------------------------------------------------
-- 2. The claim.
-- ---------------------------------------------------------------------------
--
-- Under the PLAN's row lock, so two callers for one plan are serialized and
-- exactly one of them can be answered `claimed` for an attempt:
--
--   stale     the plan is closed, or (revision, digest) is not its head
--   prepared  the revision already has its preparation
--   existing  a request exists and is in flight (or failed in a way a retry
--             cannot fix): the caller is answered with it, nothing starts
--   claimed   the caller now owns attempt N and alone may trigger it
--
-- A request is RETRIED (attempt + 1, claimed again) only when its attempt can
-- no longer produce the preparation:
--   * its trigger definitely failed;
--   * its claimer never recorded a trigger within the grace;
--   * its run ended (any terminal status) and no preparation exists;
--   * its run was never claimed by a capture within the grace after the
--     trigger (the execution never reached the entrypoint's claim).
-- A run that was claimed and is still live is never retried here: a second
-- capture beside a live one is exactly what this table exists to prevent.
-- The retry keeps a run that is still the operator's and was never claimed,
-- so a retry re-triggers THAT run rather than piling up a second one.
create or replace function public.request_work_scope_preparation(
  p_work_scope_id uuid, p_revision integer, p_digest text, p_requested_by uuid,
  p_grace_seconds integer
) returns jsonb
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_plan public.catalog_work_scopes%rowtype;
  v_request public.catalog_work_scope_preparation_requests%rowtype;
  v_run public.runs%rowtype;
  v_grace interval;
  v_retry boolean := false;
  v_keep_run boolean := false;
begin
  if p_work_scope_id is null or p_requested_by is null or p_revision is null
     or p_revision < 1 or p_digest is null or p_digest !~ '^[0-9a-f]{64}$'
     or p_grace_seconds is null or p_grace_seconds not between 300 and 86400 then
    raise exception 'WORK_SCOPE_PREPARATION_REQUEST_INVALID: invalid preparation request'
      using errcode = '22023';
  end if;
  v_grace := make_interval(secs => p_grace_seconds);

  select * into v_plan from public.catalog_work_scopes where id = p_work_scope_id for update;
  if not found then
    raise exception 'WORK_SCOPE_NOT_FOUND: mapping plan not found' using errcode = 'P0002';
  end if;
  if v_plan.closed_at is not null or v_plan.head_revision <> p_revision
     or v_plan.head_digest <> p_digest then
    return jsonb_build_object('decision', 'stale', 'request', null);
  end if;
  if exists (select 1 from public.catalog_work_scope_preparations p
              where p.work_scope_id = p_work_scope_id and p.revision = p_revision) then
    return jsonb_build_object('decision', 'prepared', 'request', null);
  end if;

  select * into v_request from public.catalog_work_scope_preparation_requests r
   where r.work_scope_id = p_work_scope_id and r.revision = p_revision
   for update;
  if not found then
    insert into public.catalog_work_scope_preparation_requests
      (work_scope_id, revision, scope_digest, requested_by)
    values (p_work_scope_id, p_revision, p_digest, p_requested_by)
    returning * into v_request;
    return jsonb_build_object('decision', 'claimed', 'request', to_jsonb(v_request));
  end if;

  if v_request.run_id is not null then
    select * into v_run from public.runs where id = v_request.run_id;
  end if;
  if v_request.trigger_state = 'trigger_failed' then
    v_retry := true;
  elsif v_request.trigger_state = 'claimed' then
    v_retry := v_request.claimed_at < now() - v_grace;
  elsif v_run.id is null then
    v_retry := false;
  elsif v_run.status in ('completed', 'partial_success', 'failed', 'cancelled', 'timed_out',
                         'budget_exhausted') then
    v_retry := true;
  elsif v_run.status = 'queued' and v_run.worker_id is null
        and coalesce(v_request.triggered_at, v_request.claimed_at) < now() - v_grace then
    v_retry := true;
  end if;
  if not v_retry then
    return jsonb_build_object('decision', 'existing', 'request', to_jsonb(v_request));
  end if;

  -- The run is kept only while it is still the operator's, queued and never
  -- claimed: re-triggering it is safe, because a capture must win
  -- `claim_run_lease` on it before it may do anything.
  v_keep_run := v_run.id is not null and v_run.status = 'queued' and v_run.worker_id is null
                and v_run.launch_state = 'none';
  update public.catalog_work_scope_preparation_requests
     set attempt = attempt + 1,
         requested_by = p_requested_by,
         run_id = case when v_keep_run then run_id end,
         trigger_state = 'claimed',
         execution_name = null,
         claimed_at = now(),
         triggered_at = null,
         updated_at = now()
   where id = v_request.id
  returning * into v_request;
  return jsonb_build_object('decision', 'claimed', 'request', to_jsonb(v_request));
end;
$$;

-- The claimer's compare-and-set: accepted only for the attempt it claimed and
-- only while that attempt is still `claimed`. `claimed` again binds the run it
-- made (before the job is executed, so a crash leaves the run findable);
-- `triggered` / `trigger_unknown` / `trigger_failed` record the outcome once.
create or replace function public.record_work_scope_preparation_trigger(
  p_request_id uuid, p_attempt integer, p_run_id uuid, p_trigger_state text,
  p_execution_name text
) returns jsonb
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_request public.catalog_work_scope_preparation_requests%rowtype;
  v_plan public.catalog_work_scopes%rowtype;
  v_run public.runs%rowtype;
begin
  if p_request_id is null or p_attempt is null
     or p_trigger_state is null
     or p_trigger_state not in ('claimed', 'triggered', 'trigger_failed', 'trigger_unknown')
     or (p_trigger_state in ('claimed', 'triggered', 'trigger_unknown') and p_run_id is null)
     or (p_execution_name is not null
         and (char_length(p_execution_name) not between 1 and 300
              or p_execution_name !~ '^[A-Za-z0-9][A-Za-z0-9/._-]*$')) then
    raise exception 'WORK_SCOPE_PREPARATION_REQUEST_INVALID: invalid trigger record'
      using errcode = '22023';
  end if;
  select * into v_request from public.catalog_work_scope_preparation_requests
   where id = p_request_id for update;
  if not found or v_request.attempt <> p_attempt or v_request.trigger_state <> 'claimed' then
    raise exception 'WORK_SCOPE_PREPARATION_REQUEST_STALE: the preparation attempt moved on'
      using errcode = '40001';
  end if;
  if p_run_id is not null then
    -- Only an operator capture run of the plan's own conversation.
    select * into v_plan from public.catalog_work_scopes where id = v_request.work_scope_id;
    select * into v_run from public.runs where id = p_run_id;
    if v_run.id is null or v_run.conversation_id <> v_plan.conversation_id
       or coalesce(v_run.run_identity->>'workflow_key', '') <> 'operator_capture'
       or (v_request.run_id is not null and v_request.run_id <> p_run_id) then
      raise exception 'WORK_SCOPE_PREPARATION_REQUEST_INVALID: not this plan''s capture run'
        using errcode = '22023';
    end if;
  end if;
  update public.catalog_work_scope_preparation_requests
     set run_id = coalesce(p_run_id, run_id),
         trigger_state = p_trigger_state,
         execution_name = case when p_trigger_state = 'claimed' then execution_name
                               else p_execution_name end,
         triggered_at = case when p_trigger_state in ('triggered', 'trigger_unknown') then now()
                             else triggered_at end,
         updated_at = now()
   where id = v_request.id
  returning * into v_request;
  return to_jsonb(v_request);
end;
$$;

-- ---------------------------------------------------------------------------
-- 3. The facts a status is derived from. Bounded: one plan, one revision, one
--    request, one run, one preparation, and the per-unit counts (at most 64
--    rows) of the plan's latest preparation. Never a log, a SQL message or an
--    execution document.
-- ---------------------------------------------------------------------------
create or replace function public.work_scope_preparation_state(
  p_work_scope_id uuid, p_revision integer, p_digest text
) returns jsonb
language plpgsql
stable
set search_path = pg_catalog
as $$
declare
  v_plan public.catalog_work_scopes%rowtype;
  v_request public.catalog_work_scope_preparation_requests%rowtype;
  v_run public.runs%rowtype;
  v_preparation public.catalog_work_scope_preparations%rowtype;
  v_latest public.catalog_work_scope_preparations%rowtype;
  v_unresolved integer;
begin
  select * into v_plan from public.catalog_work_scopes where id = p_work_scope_id;
  if not found then
    return null;
  end if;
  select * into v_preparation from public.catalog_work_scope_preparations p
   where p.work_scope_id = p_work_scope_id and p.revision = p_revision
     and p.scope_digest = p_digest;
  select * into v_request from public.catalog_work_scope_preparation_requests r
   where r.work_scope_id = p_work_scope_id and r.revision = p_revision
     and r.scope_digest = p_digest;
  if v_request.run_id is not null then
    select * into v_run from public.runs where id = v_request.run_id;
  end if;
  select * into v_latest from public.catalog_work_scope_preparations p
   where p.work_scope_id = p_work_scope_id
   order by p.revision desc limit 1;
  if v_latest.id is not null then
    select coalesce(sum(uc.excluded_known_unresolved), 0)::integer into v_unresolved
      from public.catalog_work_scope_unit_coverage uc
     where uc.preparation_id = v_latest.id;
  end if;
  return jsonb_build_object(
    'work_scope_id', v_plan.id,
    'head_revision', v_plan.head_revision,
    'head_digest', v_plan.head_digest,
    'closed', v_plan.closed_at is not null,
    'revision', p_revision,
    'digest', p_digest,
    'preparation', case when v_preparation.id is null then null else jsonb_build_object(
      'id', v_preparation.id, 'revision', v_preparation.revision,
      'unit_count', v_preparation.unit_count,
      'prepared_unit_count', v_preparation.prepared_unit_count,
      'queued_item_count', v_preparation.queued_item_count,
      'batch_count', v_preparation.batch_count,
      'created_at', v_preparation.created_at) end,
    'request', case when v_request.id is null then null else jsonb_build_object(
      'id', v_request.id, 'attempt', v_request.attempt,
      'trigger_state', v_request.trigger_state,
      'run_id', v_request.run_id,
      'claimed_seconds', greatest(0, floor(extract(epoch from now() - v_request.claimed_at)))::bigint,
      'triggered_seconds', case when v_request.triggered_at is null then null
        else greatest(0, floor(extract(epoch from now() - v_request.triggered_at)))::bigint end)
      end,
    'run', case when v_run.id is null then null else jsonb_build_object(
      'status', v_run.status,
      'launch_state', v_run.launch_state,
      'claimed', v_run.worker_id is not null,
      'lease_expired_seconds', case when v_run.lease_expires_at is null
                                      or v_run.lease_expires_at > now() then null
        else floor(extract(epoch from now() - v_run.lease_expires_at))::bigint end,
      -- The static reason code a finished capture recorded, never its message.
      'error_code', case when coalesce(v_run.error->>'code', '') ~ '^[A-Z][A-Z0-9_]{2,79}$'
                         then v_run.error->>'code' end) end,
    'known_unresolved', case when v_latest.id is null then null else jsonb_build_object(
      'revision', v_latest.revision, 'count', v_unresolved) end);
end;
$$;

-- ---------------------------------------------------------------------------
-- 4. RLS and privileges: service-path only. A member reads the status through
--    the API (membership-gated), never directly.
-- ---------------------------------------------------------------------------
alter table public.catalog_work_scope_preparation_requests enable row level security;

do $$
declare fn text;
begin
  foreach fn in array array[
    'public.request_work_scope_preparation(uuid,integer,text,uuid,integer)',
    'public.record_work_scope_preparation_trigger(uuid,integer,uuid,text,text)',
    'public.work_scope_preparation_state(uuid,integer,text)'
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

  execute 'revoke all on table public.catalog_work_scope_preparation_requests from public';
  if exists (select 1 from pg_roles where rolname='anon') then
    execute 'revoke all on table public.catalog_work_scope_preparation_requests from anon';
  end if;
  if exists (select 1 from pg_roles where rolname='authenticated') then
    execute 'revoke all on table public.catalog_work_scope_preparation_requests from authenticated';
  end if;
  if exists (select 1 from pg_roles where rolname='service_role') then
    -- Claimed and moved on by the RPCs; never deleted.
    execute 'grant select, insert, update on table public.catalog_work_scope_preparation_requests to service_role';
    execute 'revoke delete on table public.catalog_work_scope_preparation_requests from service_role';
  end if;
end $$;

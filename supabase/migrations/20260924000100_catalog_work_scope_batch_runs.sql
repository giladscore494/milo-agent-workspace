-- Scoped catalog PR3: batch runs, continuation and progress.
--
-- What this adds
-- --------------
--
-- Migration 20260923000100 decided WHAT one plan revision's work is (units, a
-- deterministic queue, bounded batches) and added the compare-and-set that binds
-- a batch to a run. Nothing called it. This migration is where a batch becomes a
-- RUN, through the one run creator every other run uses, and where the plan's
-- progress is read back from durable state:
--
--   catalog_work_scope_controls     pause / resume of a plan, append-only
--   set_work_scope_paused()         the one writer of a control
--   create_work_scope_batch_run()   the message, the queued run, its immutable
--                                   identity AND the batch binding, in ONE
--                                   transaction
--   work_scope_progress()           the plan's progress, derived from the bound
--                                   runs' own durable status and events
--   reconcile_lost_launch()         an operator's guarded decision on a launch
--                                   that was lost before any worker claimed it
--   retire_unlaunched_run()         an operator's guarded retirement of a run no
--                                   worker was ever started for
--
-- and it restates `bind_work_scope_batch_run` with the continuation rules below.
--
-- Continuation, as the database holds it
-- --------------------------------------
--
-- * ONE batch at a time per plan, and nothing starts the next one: a person
--   does, through the Mapping Plan, one batch per request.
-- * Batches run in batch order. The only batch that may start is the NEXT one:
--   the lowest-numbered batch of the head revision's preparation that is not
--   SETTLED. Nothing is skipped silently.
-- * A batch is SETTLED once one of its runs finished its work: `completed`, or
--   `partial_success` (the run finished and some candidates stay unresolved --
--   running the same candidates again would research them twice). It is never
--   bound again.
-- * A batch whose run was INTERRUPTED (failed, cancelled, timed out, stopped by
--   its budget) did not finish its work. It stays the next batch, and its next
--   run is the next attempt of the same batch.
-- * A paused plan starts nothing. A batch already running is not stopped by a
--   pause; the existing run cancellation does that.
-- * A stale revision never starts: the caller names the head revision AND its
--   digest, and both are checked under the plan's row lock.
--
-- Progress is derived, never stored
-- ---------------------------------
--
-- Nothing in this migration writes a counter. A batch's state comes from the
-- durable status of the runs bound to it, and its promoted / refused counts from
-- those runs' own `catalog_variant_promoted` / `catalog_promotion_refused`
-- events, matched to the batch's queue items by candidate key. A crash between
-- two writes can therefore never leave progress claiming work the database does
-- not hold.
--
-- Additive and forward-only: one new relation, new functions, and a restated
-- `bind_work_scope_batch_run` (same signature). Rerun-safe.

-- ---------------------------------------------------------------------------
-- 1. Pause / resume: an append-only control history.
-- ---------------------------------------------------------------------------
--
-- The plan's CURRENT control is its latest row: `pause` holds the plan, and
-- `resume` (or no row at all) releases it. The history alternates, starting
-- with `pause`, and is numbered 1, 2, 3 ... without a gap -- held by a trigger
-- on every path, not only by the writer below.
create table if not exists public.catalog_work_scope_controls (
  id uuid primary key default gen_random_uuid(),
  work_scope_id uuid not null references public.catalog_work_scopes(id) on delete restrict,
  sequence integer not null,
  action text not null,
  -- An audit fact, like `created_by` on a plan: deliberately not a foreign key.
  requested_by uuid not null,
  created_at timestamptz not null default now(),
  constraint catalog_work_scope_controls_action check (action in ('pause', 'resume')),
  constraint catalog_work_scope_controls_sequence check (sequence between 1 and 100000)
);
create unique index if not exists catalog_work_scope_controls_sequence_uidx
  on public.catalog_work_scope_controls(work_scope_id, sequence);

create or replace function public.work_scope_control_in_sequence() returns trigger
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_last public.catalog_work_scope_controls;
begin
  select * into v_last from public.catalog_work_scope_controls
   where work_scope_id = new.work_scope_id
   order by sequence desc limit 1;
  if new.sequence is distinct from coalesce(v_last.sequence, 0) + 1
     or new.action = coalesce(v_last.action, 'resume') then
    raise exception 'WORK_SCOPE_CONTROL_OUT_OF_SEQUENCE' using errcode = '55000';
  end if;
  return new;
end;
$$;

drop trigger if exists catalog_work_scope_controls_in_sequence
  on public.catalog_work_scope_controls;
create trigger catalog_work_scope_controls_in_sequence
  before insert on public.catalog_work_scope_controls
  for each row execute function public.work_scope_control_in_sequence();

-- A control is a record of a decision already made: never rewritten or removed.
drop trigger if exists catalog_work_scope_controls_append_only
  on public.catalog_work_scope_controls;
create trigger catalog_work_scope_controls_append_only
  before update or delete on public.catalog_work_scope_controls
  for each row execute function public.forbid_work_scope_preparation_mutation();

-- Is the plan paused right now? One definition, used by every function here.
create or replace function public.work_scope_paused(p_work_scope_id uuid)
returns boolean
language sql
stable
set search_path = pg_catalog
as $$
  select coalesce((select c.action = 'pause'
                     from public.catalog_work_scope_controls c
                    where c.work_scope_id = p_work_scope_id
                    order by c.sequence desc limit 1), false);
$$;

-- The one writer. Membership is re-checked here, under the plan's row lock. A
-- request for the state the plan is already in writes nothing and says so.
create or replace function public.set_work_scope_paused(
  p_work_scope_id uuid, p_paused boolean, p_requested_by uuid
) returns jsonb
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_plan public.catalog_work_scopes;
  v_workflow text;
  v_last public.catalog_work_scope_controls;
  v_row public.catalog_work_scope_controls;
begin
  if p_work_scope_id is null or p_paused is null or p_requested_by is null then
    raise exception 'WORK_SCOPE_CONTROL_INVALID' using errcode = '22023';
  end if;
  select * into v_plan from public.catalog_work_scopes where id = p_work_scope_id for update;
  -- Absent and not-a-member are ONE answer.
  if v_plan.id is null or not exists (
       select 1 from public.project_members m
        where m.project_id = v_plan.project_id and m.user_id = p_requested_by) then
    raise exception 'WORK_SCOPE_NOT_FOUND' using errcode = 'P0002';
  end if;
  select p.workflow_key into v_workflow from public.projects p where p.id = v_plan.project_id;
  if v_workflow is distinct from 'swarm_v2' then
    raise exception 'WORK_SCOPE_WORKFLOW_UNSUPPORTED' using errcode = '22023';
  end if;
  if v_plan.closed_at is not null then
    raise exception 'WORK_SCOPE_NOT_EDITABLE' using errcode = '55000';
  end if;
  select * into v_last from public.catalog_work_scope_controls
   where work_scope_id = v_plan.id order by sequence desc limit 1;
  if (coalesce(v_last.action, 'resume') = 'pause') = p_paused then
    return jsonb_build_object('changed', false, 'paused', p_paused,
                              'control', case when v_last.id is null then null
                                              else to_jsonb(v_last) end);
  end if;
  insert into public.catalog_work_scope_controls (work_scope_id, sequence, action, requested_by)
  values (v_plan.id, coalesce(v_last.sequence, 0) + 1,
          case when p_paused then 'pause' else 'resume' end, p_requested_by)
  returning * into v_row;
  return jsonb_build_object('changed', true, 'paused', p_paused, 'control', to_jsonb(v_row));
end;
$$;

-- ---------------------------------------------------------------------------
-- 2. The binding, restated with the continuation rules.
-- ---------------------------------------------------------------------------
--
-- Same signature and same refusals as 20260923000100, plus:
--
--   * a paused plan binds nothing (WORK_SCOPE_PAUSED);
--   * only the NEXT batch binds (WORK_SCOPE_BATCH_NOT_NEXT): the lowest-numbered
--     batch of its preparation that is not settled;
--   * a SETTLED batch -- one whose run `completed` or ended `partial_success` --
--     is never bound again (WORK_SCOPE_BATCH_ALREADY_COMPLETED).
--
-- Binding the same run to the same batch again still returns the existing
-- binding, checked before any of the rules above, so a retried request is the
-- same binding rather than a refusal.
create or replace function public.bind_work_scope_batch_run(
  p_batch_id uuid, p_run_id uuid, p_expected_revision integer, p_expected_digest text,
  p_bound_by uuid
) returns jsonb
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_batch public.catalog_work_scope_batches;
  v_plan public.catalog_work_scopes;
  v_workflow text;
  v_run record;
  v_existing public.catalog_work_scope_batch_runs;
  v_next uuid;
  v_attempt integer;
  v_binding public.catalog_work_scope_batch_runs;
begin
  if p_batch_id is null or p_run_id is null or p_bound_by is null then
    raise exception 'WORK_SCOPE_BATCH_RUN_INVALID' using errcode = '22023';
  end if;
  select * into v_batch from public.catalog_work_scope_batches where id = p_batch_id;
  if v_batch.id is null then
    raise exception 'WORK_SCOPE_BATCH_NOT_FOUND' using errcode = 'P0002';
  end if;
  select * into v_plan from public.catalog_work_scopes where id = v_batch.work_scope_id for update;
  -- Absent and not-a-member are ONE answer.
  if not exists (select 1 from public.project_members m
                  where m.project_id = v_plan.project_id and m.user_id = p_bound_by) then
    raise exception 'WORK_SCOPE_BATCH_NOT_FOUND' using errcode = 'P0002';
  end if;
  select p.workflow_key into v_workflow from public.projects p where p.id = v_plan.project_id;
  if v_workflow is distinct from 'swarm_v2' then
    raise exception 'WORK_SCOPE_WORKFLOW_UNSUPPORTED' using errcode = '22023';
  end if;
  if v_plan.closed_at is not null then
    raise exception 'WORK_SCOPE_NOT_EDITABLE' using errcode = '55000';
  end if;
  if v_plan.head_revision is distinct from v_batch.revision
     or v_plan.head_digest is distinct from v_batch.scope_digest
     or p_expected_revision is distinct from v_batch.revision
     or p_expected_digest is distinct from v_batch.scope_digest then
    raise exception 'WORK_SCOPE_STALE' using errcode = '40001';
  end if;
  select r.id, r.conversation_id, r.status, r.run_identity->>'workflow_key' as workflow_key
    into v_run from public.runs r where r.id = p_run_id;
  if v_run.id is null or v_run.conversation_id is distinct from v_plan.conversation_id
     or v_run.workflow_key is distinct from 'swarm_v2' then
    raise exception 'WORK_SCOPE_BATCH_RUN_INVALID' using errcode = '22023';
  end if;
  select * into v_existing from public.catalog_work_scope_batch_runs where run_id = p_run_id;
  if v_existing.id is not null then
    if v_existing.batch_id = p_batch_id then
      return to_jsonb(v_existing) || jsonb_build_object('replayed', true);
    end if;
    raise exception 'WORK_SCOPE_BATCH_RUN_TAKEN' using errcode = '23505';
  end if;
  if v_run.status in ('completed', 'partial_success', 'failed', 'cancelled', 'timed_out',
                      'budget_exhausted') then
    raise exception 'WORK_SCOPE_BATCH_RUN_INVALID' using errcode = '22023';
  end if;
  if public.work_scope_paused(v_plan.id) then
    raise exception 'WORK_SCOPE_PAUSED' using errcode = '55000';
  end if;
  if exists (select 1 from public.catalog_work_scope_batch_runs br
               join public.runs r on r.id = br.run_id
              where br.work_scope_id = v_plan.id
                and r.status not in ('completed', 'partial_success', 'failed', 'cancelled',
                                     'timed_out', 'budget_exhausted')) then
    raise exception 'WORK_SCOPE_BATCH_IN_PROGRESS' using errcode = '55000';
  end if;
  if exists (select 1 from public.catalog_work_scope_batch_runs br
               join public.runs r on r.id = br.run_id
              where br.batch_id = p_batch_id
                and r.status in ('completed', 'partial_success')) then
    raise exception 'WORK_SCOPE_BATCH_ALREADY_COMPLETED' using errcode = '55000';
  end if;
  select b.id into v_next
    from public.catalog_work_scope_batches b
   where b.preparation_id = v_batch.preparation_id
     and not exists (select 1 from public.catalog_work_scope_batch_runs br
                       join public.runs r on r.id = br.run_id
                      where br.batch_id = b.id
                        and r.status in ('completed', 'partial_success'))
   order by b.batch_number
   limit 1;
  if v_next is distinct from p_batch_id then
    raise exception 'WORK_SCOPE_BATCH_NOT_NEXT' using errcode = '55000';
  end if;
  select coalesce(max(attempt), 0) + 1 into v_attempt
    from public.catalog_work_scope_batch_runs where batch_id = p_batch_id;
  insert into public.catalog_work_scope_batch_runs
    (batch_id, work_scope_id, run_id, attempt, bound_by)
  values (p_batch_id, v_plan.id, p_run_id, v_attempt, p_bound_by)
  returning * into v_binding;
  return to_jsonb(v_binding) || jsonb_build_object('replayed', false);
end;
$$;

-- ---------------------------------------------------------------------------
-- 3. Starting a batch: one transaction, through the one run creator.
-- ---------------------------------------------------------------------------
--
-- The API's "Start batch" is THIS function, and nothing else creates a batch
-- run. It composes the existing creator and the existing binding in ONE
-- transaction, so there is never a batch run without its binding, nor a binding
-- without its run:
--
--   1. the plan's row lock (the serialization point every plan write takes);
--   2. an idempotent replay of the same request returns the original run and
--      binding BEFORE anything below is consulted, like the run creator itself
--      -- except that a replay the caller would LAUNCH (a queued run whose
--      launch never happened or definitely failed) is refused when its batch
--      is stale or the plan is paused or closed;
--   3. every continuation rule is checked, in the order a person would want to
--      hear about it -- stale, paused, a batch already running, settled, not
--      next -- and a refusal writes nothing;
--   4. `create_message_and_run_v3` writes the message, the queued run and its
--      immutable identity, with its own replay, drift and concurrency checks;
--   5. `bind_work_scope_batch_run` binds that run to the batch, re-checking
--      everything under the same lock.
--
-- The request names the batch it means (`p_batch_id`, from the progress read).
-- The name is a precondition, never a choice: the database decides which batch
-- is next, and a request for any other one is refused.
--
-- A second request for the batch that is ALREADY running -- a double click with
-- a fresh idempotency key, two tabs -- is not a second run: it answers with the
-- running one (`created: false`). The caller's launch compare-and-set then
-- decides whether that run still needs launching, exactly once.
--
-- Returns SETOF, like the run creator it wraps: the creation commits here, and
-- the answer must be readable by the pinned client afterwards.
create or replace function public.create_work_scope_batch_run(
  p_work_scope_id uuid,
  p_batch_id uuid,
  p_expected_revision integer,
  p_expected_digest text,
  p_run_id uuid,
  p_run_identity jsonb,
  p_content text,
  p_metadata jsonb,
  p_requested_by uuid,
  p_idempotency_key text,
  p_request_fingerprint text,
  p_max_user_active integer default null,
  p_max_project_active integer default null
) returns setof jsonb
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_plan public.catalog_work_scopes;
  v_workflow text;
  v_batch public.catalog_work_scope_batches;
  v_existing public.runs;
  v_binding public.catalog_work_scope_batch_runs;
  v_live record;
  v_next uuid;
  v_created jsonb;
  v_bound jsonb;
begin
  if p_work_scope_id is null or p_batch_id is null or p_run_id is null
     or p_requested_by is null then
    raise exception 'WORK_SCOPE_BATCH_RUN_INVALID' using errcode = '22023';
  end if;
  -- A batch start is always idempotent: the key is what makes a retried
  -- request the same request.
  if p_idempotency_key is null or btrim(p_idempotency_key) = ''
     or p_request_fingerprint is null or btrim(p_request_fingerprint) = '' then
    raise exception 'WORK_SCOPE_BATCH_IDEMPOTENCY_REQUIRED' using errcode = '22023';
  end if;

  select * into v_plan from public.catalog_work_scopes where id = p_work_scope_id for update;
  -- Absent and not-a-member are ONE answer.
  if v_plan.id is null or not exists (
       select 1 from public.project_members m
        where m.project_id = v_plan.project_id and m.user_id = p_requested_by) then
    raise exception 'WORK_SCOPE_NOT_FOUND' using errcode = 'P0002';
  end if;

  -- The same request again: the original answer, whatever has happened since
  -- -- unless answering would launch the run (below).
  select * into v_existing from public.runs
   where conversation_id = v_plan.conversation_id
     and requested_by = p_requested_by
     and idempotency_key = p_idempotency_key;
  if found then
    select * into v_binding from public.catalog_work_scope_batch_runs
     where run_id = v_existing.id;
    if v_existing.request_fingerprint is distinct from p_request_fingerprint
       or v_binding.id is null or v_binding.work_scope_id is distinct from v_plan.id then
      raise exception 'IDEMPOTENCY_CONFLICT' using errcode = '23505';
    end if;
    -- A replayed run whose launch never happened, or definitely failed, is
    -- launched by the caller: that is a start, so it obeys the start rules as
    -- they stand NOW. A stale revision never launches; a paused or closed plan
    -- starts nothing. A run already launched (or in any other state) is only
    -- reported, never launched again, so it is returned as it is.
    if v_existing.status = 'queued'
       and v_existing.launch_state in ('pending', 'launch_failed') then
      if v_plan.closed_at is not null then
        raise exception 'WORK_SCOPE_NOT_EDITABLE' using errcode = '55000';
      end if;
      if exists (select 1 from public.catalog_work_scope_batches b
                  where b.id = v_binding.batch_id
                    and (b.revision is distinct from v_plan.head_revision
                         or b.scope_digest is distinct from v_plan.head_digest)) then
        raise exception 'WORK_SCOPE_STALE' using errcode = '40001';
      end if;
      if public.work_scope_paused(v_plan.id) then
        raise exception 'WORK_SCOPE_PAUSED' using errcode = '55000';
      end if;
    end if;
    return next jsonb_build_object('run', to_jsonb(v_existing), 'binding', to_jsonb(v_binding),
                                   'created', false);
    return;
  end if;

  select p.workflow_key into v_workflow from public.projects p where p.id = v_plan.project_id;
  if v_workflow is distinct from 'swarm_v2' then
    raise exception 'WORK_SCOPE_WORKFLOW_UNSUPPORTED' using errcode = '22023';
  end if;
  if p_run_identity is null or jsonb_typeof(p_run_identity) <> 'object'
     or p_run_identity->>'workflow_key' is distinct from 'swarm_v2' then
    raise exception 'WORK_SCOPE_BATCH_RUN_INVALID' using errcode = '22023';
  end if;
  if v_plan.closed_at is not null then
    raise exception 'WORK_SCOPE_NOT_EDITABLE' using errcode = '55000';
  end if;
  -- A stale digest fails closed.
  if p_expected_revision is distinct from v_plan.head_revision
     or p_expected_digest is distinct from v_plan.head_digest then
    raise exception 'WORK_SCOPE_STALE' using errcode = '40001';
  end if;
  select * into v_batch from public.catalog_work_scope_batches
   where id = p_batch_id and work_scope_id = v_plan.id;
  if v_batch.id is null then
    raise exception 'WORK_SCOPE_BATCH_NOT_FOUND' using errcode = 'P0002';
  end if;
  if v_batch.revision is distinct from v_plan.head_revision
     or v_batch.scope_digest is distinct from v_plan.head_digest then
    raise exception 'WORK_SCOPE_STALE' using errcode = '40001';
  end if;
  if public.work_scope_paused(v_plan.id) then
    raise exception 'WORK_SCOPE_PAUSED' using errcode = '55000';
  end if;

  -- One batch at a time. A request for the batch already running answers with
  -- that run instead of creating a second one.
  select br.*, r.status as run_status into v_live
    from public.catalog_work_scope_batch_runs br
    join public.runs r on r.id = br.run_id
   where br.work_scope_id = v_plan.id
     and r.status not in ('completed', 'partial_success', 'failed', 'cancelled', 'timed_out',
                          'budget_exhausted')
   order by br.bound_at desc
   limit 1;
  if v_live.id is not null then
    if v_live.batch_id is distinct from p_batch_id then
      raise exception 'WORK_SCOPE_BATCH_IN_PROGRESS' using errcode = '55000';
    end if;
    select * into v_existing from public.runs where id = v_live.run_id;
    select * into v_binding from public.catalog_work_scope_batch_runs where id = v_live.id;
    return next jsonb_build_object('run', to_jsonb(v_existing), 'binding', to_jsonb(v_binding),
                                   'created', false);
    return;
  end if;

  if exists (select 1 from public.catalog_work_scope_batch_runs br
               join public.runs r on r.id = br.run_id
              where br.batch_id = p_batch_id
                and r.status in ('completed', 'partial_success')) then
    raise exception 'WORK_SCOPE_BATCH_ALREADY_COMPLETED' using errcode = '55000';
  end if;
  select b.id into v_next
    from public.catalog_work_scope_batches b
   where b.preparation_id = v_batch.preparation_id
     and not exists (select 1 from public.catalog_work_scope_batch_runs br
                       join public.runs r on r.id = br.run_id
                      where br.batch_id = b.id
                        and r.status in ('completed', 'partial_success'))
   order by b.batch_number
   limit 1;
  if v_next is distinct from p_batch_id then
    raise exception 'WORK_SCOPE_BATCH_NOT_NEXT' using errcode = '55000';
  end if;

  -- The one run creator: message, queued run and immutable identity, with its
  -- own identity, drift and concurrency checks.
  select c into v_created
    from public.create_message_and_run_v3(
           p_run_id, p_run_identity, v_plan.conversation_id, p_content, p_metadata,
           p_requested_by, p_idempotency_key, p_request_fingerprint,
           p_max_user_active, p_max_project_active) as c
   limit 1;
  if v_created is null or not coalesce((v_created->>'created')::boolean, false)
     or v_created->'run'->>'id' is distinct from p_run_id::text then
    -- The replay above ran under the same lock, so this cannot be a replay; a
    -- creator that did not create is refused rather than bound.
    raise exception 'WORK_SCOPE_BATCH_RUN_INVALID' using errcode = '22023';
  end if;
  v_bound := public.bind_work_scope_batch_run(p_batch_id, p_run_id, p_expected_revision,
                                              p_expected_digest, p_requested_by);
  return next jsonb_build_object('run', v_created->'run',
                                 'binding', v_bound - 'replayed',
                                 'created', true);
  return;
end;
$$;

-- ---------------------------------------------------------------------------
-- 4. Progress: derived from durable state, bounded.
-- ---------------------------------------------------------------------------
--
-- NULL for a plan that does not exist. Otherwise:
--
--   head          the plan's head revision and digest, and whether it is closed
--   paused        the current control, and its latest row
--   live          the ONE batch run of the plan that is not terminal, whatever
--                 revision its batch belongs to (a plan may have been revised
--                 while a batch ran)
--   preparation   the HEAD revision's preparation, or null when that revision
--                 is not prepared: its counts; one row per unit (at most 64)
--                 with its batches and item outcomes; the next batch; the five
--                 most recently started batches; and the totals
--
-- Per batch, from the runs bound to it:
--   state         completed | partial (settled) | active | interrupted | pending
--   promoted      candidates of the batch with a `catalog_variant_promoted`
--                 event (`promoted: true`) from one of its runs
--   refused       candidates with a `catalog_promotion_refused` event and never
--                 promoted
--   unresolved    for a SETTLED batch, the candidates neither promoted nor
--                 refused
--
-- Counts and codes only: no candidate text, no register text, no event payload
-- leaves this function.
create or replace function public.work_scope_progress(p_work_scope_id uuid)
returns jsonb
language plpgsql
stable
set search_path = pg_catalog
as $$
declare
  v_plan public.catalog_work_scopes;
  v_control public.catalog_work_scope_controls;
  v_live jsonb;
  v_prep public.catalog_work_scope_preparations;
  v_preparation jsonb;
begin
  select * into v_plan from public.catalog_work_scopes where id = p_work_scope_id;
  if v_plan.id is null then
    return null;
  end if;
  select * into v_control from public.catalog_work_scope_controls
   where work_scope_id = v_plan.id order by sequence desc limit 1;

  select jsonb_build_object(
           'batch_id', b.id, 'batch_number', b.batch_number, 'revision', b.revision,
           'unit_key', b.unit_key, 'item_count', b.item_count, 'attempt', br.attempt,
           'run_id', r.id, 'run_status', r.status, 'launch_state', r.launch_state,
           'bound_at', br.bound_at)
    into v_live
    from public.catalog_work_scope_batch_runs br
    join public.runs r on r.id = br.run_id
    join public.catalog_work_scope_batches b on b.id = br.batch_id
   where br.work_scope_id = v_plan.id
     and r.status not in ('completed', 'partial_success', 'failed', 'cancelled', 'timed_out',
                          'budget_exhausted')
   order by br.bound_at desc
   limit 1;

  select * into v_prep from public.catalog_work_scope_preparations
   where work_scope_id = v_plan.id and revision = v_plan.head_revision;

  if v_prep.id is not null then
    with bound as (
      select br.batch_id, br.attempt, br.bound_at, r.id as run_id, r.status
        from public.catalog_work_scope_batch_runs br
        join public.runs r on r.id = br.run_id
        join public.catalog_work_scope_batches b on b.id = br.batch_id
       where b.preparation_id = v_prep.id
    ), outcomes as (
      -- Every promotion event of every run bound to this preparation, matched
      -- to the queue by candidate key: a candidate promoted by any of the
      -- plan's batch runs is promoted, whichever batch queued it.
      select i.batch_id, i.candidate_key,
             bool_or(e.event_type = 'catalog_variant_promoted'
                     and e.payload->>'promoted' = 'true') as promoted,
             bool_or(e.event_type = 'catalog_promotion_refused') as refused
        from bound
        join public.run_events e
          on e.run_id = bound.run_id
         and e.event_type in ('catalog_variant_promoted', 'catalog_promotion_refused')
        join public.catalog_work_scope_queue_items i
          on i.preparation_id = v_prep.id
         and i.candidate_key = e.payload->>'candidate_key'
       group by i.batch_id, i.candidate_key
    ), per_outcome as (
      select o.batch_id,
             count(*) filter (where o.promoted) as promoted,
             count(*) filter (where o.refused and not o.promoted) as refused
        from outcomes o
       group by o.batch_id
    ), per_batch as (
      select b.id, b.batch_number, b.unit_key, b.item_count, b.first_position,
             count(bound.run_id)::integer as attempts,
             coalesce(bool_or(bound.status = 'completed'), false) as any_completed,
             coalesce(bool_or(bound.status = 'partial_success'), false) as any_partial,
             coalesce(bool_or(bound.status not in ('completed', 'partial_success', 'failed',
                                                   'cancelled', 'timed_out',
                                                   'budget_exhausted')), false) as any_live,
             max(bound.bound_at) as last_bound_at,
             (array_agg(bound.run_id order by bound.attempt desc))[1] as last_run_id,
             (array_agg(bound.status order by bound.attempt desc))[1] as last_run_status
        from public.catalog_work_scope_batches b
        left join bound on bound.batch_id = b.id
       where b.preparation_id = v_prep.id
       group by b.id
    ), batches as (
      select pb.*,
             case when pb.any_completed then 'completed'
                  when pb.any_partial then 'partial'
                  when pb.any_live then 'active'
                  when pb.attempts > 0 then 'interrupted'
                  else 'pending' end as state,
             coalesce(po.promoted, 0)::integer as promoted,
             coalesce(po.refused, 0)::integer as refused
        from per_batch pb
        left join per_outcome po on po.batch_id = pb.id
    ), batch_view as (
      select b.*,
             (b.state in ('completed', 'partial')) as settled,
             case when b.state in ('completed', 'partial')
                  then b.item_count - b.promoted - b.refused else 0 end as unresolved
        from batches b
    )
    select jsonb_build_object(
      'id', v_prep.id,
      'revision', v_prep.revision,
      'created_at', v_prep.created_at,
      'unit_count', v_prep.unit_count,
      'prepared_unit_count', v_prep.prepared_unit_count,
      'queued_item_count', v_prep.queued_item_count,
      'batch_count', v_prep.batch_count,
      'units', coalesce((
        select jsonb_agg(jsonb_build_object(
                 'priority', u.priority, 'unit_key', u.unit_key, 'state', u.state,
                 'reason_code', u.reason_code, 'readable_count', u.readable_count,
                 'ambiguous_count', u.ambiguous_count, 'eligible_count', u.eligible_count,
                 'queued_count', u.queued_count,
                 'batch_count', (select count(*) from batch_view x where x.unit_key = u.unit_key),
                 'settled_batches', (select count(*) from batch_view x
                                      where x.unit_key = u.unit_key and x.settled),
                 'active', exists (select 1 from batch_view x
                                    where x.unit_key = u.unit_key and x.state = 'active'),
                 'promoted', (select coalesce(sum(x.promoted), 0) from batch_view x
                               where x.unit_key = u.unit_key),
                 'refused', (select coalesce(sum(x.refused), 0) from batch_view x
                              where x.unit_key = u.unit_key),
                 'unresolved', (select coalesce(sum(x.unresolved), 0) from batch_view x
                                 where x.unit_key = u.unit_key))
                 order by u.priority)
          from public.catalog_work_scope_units u
         where u.preparation_id = v_prep.id), '[]'::jsonb),
      'next', (select jsonb_build_object(
                        'batch_id', x.id, 'batch_number', x.batch_number,
                        'unit_key', x.unit_key, 'item_count', x.item_count,
                        'first_position', x.first_position, 'state', x.state,
                        'attempts', x.attempts)
                 from batch_view x where not x.settled
                order by x.batch_number limit 1),
      'recent', coalesce((
        select jsonb_agg(jsonb_build_object(
                 'batch_id', y.id, 'batch_number', y.batch_number, 'unit_key', y.unit_key,
                 'item_count', y.item_count, 'state', y.state, 'attempts', y.attempts,
                 'run_id', y.last_run_id, 'run_status', y.last_run_status,
                 'promoted', y.promoted, 'refused', y.refused, 'unresolved', y.unresolved)
                 order by y.last_bound_at desc)
          from (select * from batch_view x where x.attempts > 0
                 order by x.last_bound_at desc limit 5) y), '[]'::jsonb),
      'batches', jsonb_build_object(
        'total', (select count(*) from batch_view),
        'settled', (select count(*) from batch_view x where x.settled),
        'active', (select count(*) from batch_view x where x.state = 'active'),
        'interrupted', (select count(*) from batch_view x where x.state = 'interrupted')),
      'items', jsonb_build_object(
        'total', v_prep.queued_item_count,
        'promoted', (select coalesce(sum(x.promoted), 0) from batch_view x),
        'refused', (select coalesce(sum(x.refused), 0) from batch_view x),
        'unresolved', (select coalesce(sum(x.unresolved), 0) from batch_view x)))
      into v_preparation;
  end if;

  return jsonb_build_object(
    'work_scope_id', v_plan.id,
    'revision', v_plan.head_revision,
    'digest', v_plan.head_digest,
    'closed', v_plan.closed_at is not null,
    'paused', coalesce(v_control.action = 'pause', false),
    'control', case when v_control.id is null then null
                    else jsonb_build_object('sequence', v_control.sequence,
                                            'action', v_control.action,
                                            'created_at', v_control.created_at) end,
    'live', v_live,
    'preparation', v_preparation);
end;
$$;

-- ---------------------------------------------------------------------------
-- 5. A lost launch: reconciled by an operator, never relaunched by itself.
-- ---------------------------------------------------------------------------
--
-- The API's launch step takes launch ownership with a compare-and-set (queued +
-- pending/launch_failed -> launching), calls the launcher, and then records
-- `launched`, `launch_failed` or `launch_unknown`. If the API process dies
-- between the compare-and-set and that record, the run rests at `queued` +
-- `launching`: the compare-and-set takes only pending and launch_failed, so
-- nothing launches it again; no worker holds its lease; and -- for a batch
-- run -- it is the plan's one live batch, so the plan is held. `launching` does
-- NOT mean "not launched": the launcher may have started a worker just before
-- the process died.
--
-- A stale `launching` run is therefore an UNRESOLVED launch, like
-- `launch_unknown`, and it is resolved the same way: by an operator who checks
-- Cloud Run for an execution of the run and then applies a decision through
-- the guarded reconciliation tool (scripts/release/reconcile-launch-unknown.sh),
-- which calls this function for a lost launch. `launch_unknown` keeps the
-- tool's own guarded updates, unchanged.
--
-- `reconcile_lost_launch` never guesses. Under the run's row lock it requires:
--
--   * the posture a lost launch leaves: the run `queued` with launch
--     `launching`, and NO worker ever claimed it -- no worker, no lease, never
--     started. A claimed run is not lost, whatever its launch label says, and
--     a lease is never taken over;
--   * the row quiet for at least the caller's threshold, and never for less
--     than 15 minutes -- the launch request itself times out after 15 s -- so
--     it cannot race a launch request still in flight;
--   * for `not_launched`, also that nothing but the API ever wrote about the
--     run: no launcher record (a recorded execution means the launch
--     happened), no checkpoint, heartbeat, usage, reservation or blackboard
--     row, and no event beyond the API's own `launch_failed`.
--
-- `launched` records what the operator found -- an execution of the run exists
-- -- and the worker claims and runs it as usual. `not_launched` moves the run
-- to `launch_failed`, the state the launch step already reads as "no worker
-- was started": the SAME run can be launched again, and only through the launch
-- compare-and-set -- from the Mapping Plan by a person, or after the tool's
-- existing `requeue`. Nothing here launches anything, and whatever the lost
-- launch did, at most one worker ever executes the run: `claim_run_lease`
-- grants one live lease.
create or replace function public.reconcile_lost_launch(
  p_run_id uuid,
  p_outcome text,
  p_min_quiet_seconds integer,
  p_operator text
) returns jsonb
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_run public.runs;
  v_target text;
  v_rows integer;
  -- The shortest quiet period ever accepted, whatever the caller asks for.
  v_floor constant integer := 900;
begin
  if p_run_id is null or p_outcome is null or p_outcome not in ('launched', 'not_launched')
     or p_operator is null or btrim(p_operator) = '' or char_length(p_operator) > 200 then
    raise exception 'LOST_LAUNCH_INVALID' using errcode = '22023';
  end if;
  if p_min_quiet_seconds is null or p_min_quiet_seconds < v_floor then
    raise exception 'LOST_LAUNCH_THRESHOLD_TOO_SHORT' using errcode = '22023';
  end if;
  v_target := case p_outcome when 'launched' then 'launched' else 'launch_failed' end;

  select * into v_run from public.runs where id = p_run_id for update;
  if v_run.id is null then
    raise exception 'LOST_LAUNCH_NOT_FOUND' using errcode = 'P0002';
  end if;
  -- Already where the decision leads: the same answer, and nothing written.
  if v_run.launch_state = v_target then
    return jsonb_build_object('reconciled', false, 'run_id', v_run.id,
                              'status', v_run.status, 'launch_state', v_run.launch_state);
  end if;
  -- The posture a lost launch leaves, and only that.
  if v_run.status is distinct from 'queued' or v_run.launch_state is distinct from 'launching' then
    raise exception 'LOST_LAUNCH_WRONG_STATE' using errcode = '55000';
  end if;
  -- No worker ever claimed it, and no lease is held.
  if v_run.worker_id is not null or v_run.lease_token is not null
     or v_run.lease_expires_at is not null or v_run.started_at is not null
     or v_run.finished_at is not null then
    raise exception 'LOST_LAUNCH_CLAIMED' using errcode = '55000';
  end if;
  -- Quiet for long enough that no launch request can still be in flight.
  if v_run.updated_at > now() - make_interval(secs => p_min_quiet_seconds) then
    raise exception 'LOST_LAUNCH_NOT_QUIET' using errcode = '55000';
  end if;
  -- "Not launched" only when nothing but the API ever wrote about the run.
  if p_outcome = 'not_launched' and (
       exists (select 1 from public.run_invocations where run_id = v_run.id)
    or exists (select 1 from public.run_checkpoints where run_id = v_run.id)
    or exists (select 1 from public.worker_heartbeats where run_id = v_run.id)
    or exists (select 1 from public.run_usage_ledger where run_id = v_run.id)
    or exists (select 1 from public.model_call_budget_reservations where run_id = v_run.id)
    or exists (select 1 from public.run_blackboards where run_id = v_run.id)
    or exists (select 1 from public.run_events e
                where e.run_id = v_run.id and e.event_type <> 'launch_failed')) then
    raise exception 'LOST_LAUNCH_TRACED' using errcode = '55000';
  end if;

  update public.runs
     set launch_state = v_target,
         launch_error = case when p_outcome = 'not_launched' then jsonb_build_object(
           'code', 'RUN_LAUNCH_LOST',
           'message', 'launch ownership was taken but its outcome was never recorded; '
                      || 'an operator verified that no worker was started',
           'reconciled_by', p_operator) else launch_error end
   where id = v_run.id and status = 'queued' and launch_state = 'launching'
     and worker_id is null and lease_token is null and started_at is null;
  get diagnostics v_rows = row_count;
  if v_rows <> 1 then
    raise exception 'LOST_LAUNCH_CHANGED' using errcode = '40001';
  end if;
  if p_outcome = 'not_launched' then
    -- The run's own history says why it can be launched again. Who decided is
    -- in the operator's audit record, not in an event every member can read.
    insert into public.run_events (run_id, event_type, message, payload)
    values (v_run.id, 'launch_failed',
            'The worker launch was never confirmed and an operator verified that no worker '
            || 'was started; the run remains queued and can be launched again',
            jsonb_build_object('recoverable', true, 'reconciled', true,
                               'previous_launch_state', 'launching'));
  end if;
  return jsonb_build_object('reconciled', true, 'run_id', v_run.id, 'status', v_run.status,
                            'launch_state', v_target, 'previous_launch_state', 'launching');
end;
$$;

-- ---------------------------------------------------------------------------
-- 6. A run no worker was ever started for: retired by an operator.
-- ---------------------------------------------------------------------------
--
-- A queued run whose launch never happened (`pending`) or definitely failed
-- (`launch_failed`, including a lost launch an operator reconciled as not
-- launched) has no worker, and none is started unless it is launched again. At
-- the plan's head revision the Mapping Plan launches it again as the same run.
-- Once the plan is revised past its batch it can never be launched (a stale
-- revision never launches), nothing finalizes it, and a cancellation request
-- is refused for exactly that reason -- so it holds the plan, and its
-- requester's and project's run slot, for good.
--
-- `retire_unlaunched_run` is the operator's guarded way out, and it never
-- guesses. Under the run's row lock it requires:
--
--   * a launch KNOWN not to have started a worker: `pending` or
--     `launch_failed`, with the run `queued` (or `cancellation_requested`, a
--     request no worker will ever finish). Never `launching` or
--     `launch_unknown` -- an operator reconciles those first -- and never
--     `launched`;
--   * that no worker ever claimed it: no worker, no lease, never started;
--   * that no execution or paid work exists for it: no launcher record, no
--     checkpoint, heartbeat, usage, reservation or blackboard row, and no event
--     beyond the API's own `launch_failed` / `cancellation_requested`;
--   * the row quiet for at least the caller's threshold, never under 15
--     minutes.
--
-- It then ends the run the way the canonical finalizer (`finalize_run_guarded`,
-- migration 20260920000200) ends a cancellation: through the supported
-- transitions queued -> cancellation_requested -> cancelled, each re-guarded,
-- with the terminal state and its `run_cancelled` event in ONE transaction. The
-- run's error and the event carry the same static code, `RUN_NOT_LAUNCHED`, and
-- no ProductOutcome, because there is no product. A terminal run can never be
-- claimed, so a worker started for it anyway exits without executing. Nothing
-- is relaunched: a batch it held becomes `interrupted`, and the plan continues
-- only when a person starts its next batch.
create or replace function public.retire_unlaunched_run(
  p_run_id uuid,
  p_min_quiet_seconds integer,
  p_operator text
) returns jsonb
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_run public.runs;
  v_rows integer;
  -- The shortest quiet period ever accepted, whatever the caller asks for.
  v_floor constant integer := 900;
  v_message constant text := 'No worker was ever started for this run; an operator retired it';
begin
  if p_run_id is null or p_operator is null or btrim(p_operator) = ''
     or char_length(p_operator) > 200 then
    raise exception 'UNLAUNCHED_RUN_INVALID' using errcode = '22023';
  end if;
  if p_min_quiet_seconds is null or p_min_quiet_seconds < v_floor then
    raise exception 'UNLAUNCHED_RUN_THRESHOLD_TOO_SHORT' using errcode = '22023';
  end if;

  select * into v_run from public.runs where id = p_run_id for update;
  if v_run.id is null then
    raise exception 'UNLAUNCHED_RUN_NOT_FOUND' using errcode = 'P0002';
  end if;
  -- Already retired this way: the same answer, and nothing written again.
  if v_run.status = 'cancelled' and v_run.error->>'code' = 'RUN_NOT_LAUNCHED' then
    return jsonb_build_object('retired', false, 'run_id', v_run.id, 'status', v_run.status);
  end if;
  -- A launch known not to have started a worker, and only that.
  if v_run.status not in ('queued', 'cancellation_requested')
     or v_run.launch_state not in ('pending', 'launch_failed') then
    raise exception 'UNLAUNCHED_RUN_WRONG_STATE' using errcode = '55000';
  end if;
  -- No worker ever claimed it, and no lease is held.
  if v_run.worker_id is not null or v_run.lease_token is not null
     or v_run.lease_expires_at is not null or v_run.started_at is not null
     or v_run.finished_at is not null then
    raise exception 'UNLAUNCHED_RUN_CLAIMED' using errcode = '55000';
  end if;
  -- No execution and no paid work: nothing but the API ever wrote about it.
  if exists (select 1 from public.run_invocations where run_id = v_run.id)
     or exists (select 1 from public.run_checkpoints where run_id = v_run.id)
     or exists (select 1 from public.worker_heartbeats where run_id = v_run.id)
     or exists (select 1 from public.run_usage_ledger where run_id = v_run.id)
     or exists (select 1 from public.model_call_budget_reservations where run_id = v_run.id)
     or exists (select 1 from public.run_blackboards where run_id = v_run.id)
     or exists (select 1 from public.run_events e
                 where e.run_id = v_run.id
                   and e.event_type not in ('launch_failed', 'cancellation_requested')) then
    raise exception 'UNLAUNCHED_RUN_TRACED' using errcode = '55000';
  end if;
  -- Quiet for long enough that no request about it can still be in flight.
  if v_run.updated_at > now() - make_interval(secs => p_min_quiet_seconds) then
    raise exception 'UNLAUNCHED_RUN_NOT_QUIET' using errcode = '55000';
  end if;

  -- The supported transitions, each re-guarded on the never-launched posture.
  if v_run.status = 'queued' then
    update public.runs
       set status = 'cancellation_requested',
           cancellation_requested_at = now(),
           cancellation_reason = 'retired by an operator: no worker was ever started'
     where id = v_run.id and status = 'queued'
       and launch_state in ('pending', 'launch_failed')
       and worker_id is null and lease_token is null and started_at is null;
    get diagnostics v_rows = row_count;
    if v_rows <> 1 then
      raise exception 'UNLAUNCHED_RUN_CHANGED' using errcode = '40001';
    end if;
  end if;
  update public.runs
     set status = 'cancelled',
         error = jsonb_build_object('code', 'RUN_NOT_LAUNCHED', 'message', v_message),
         finished_at = now()
   where id = v_run.id and status = 'cancellation_requested'
     and launch_state in ('pending', 'launch_failed')
     and worker_id is null and lease_token is null and started_at is null;
  get diagnostics v_rows = row_count;
  if v_rows <> 1 then
    raise exception 'UNLAUNCHED_RUN_CHANGED' using errcode = '40001';
  end if;
  -- The terminal event, in the same transaction, shaped as the canonical
  -- finalizer shapes a cancellation's: its message and the static code. Who
  -- decided is in the operator's audit record, not in a member-readable event.
  insert into public.run_events (run_id, event_type, message, payload)
  values (v_run.id, 'run_cancelled', v_message, jsonb_build_object('code', 'RUN_NOT_LAUNCHED'));
  return jsonb_build_object('retired', true, 'run_id', v_run.id, 'status', 'cancelled',
                            'previous_status', v_run.status);
end;
$$;

-- ---------------------------------------------------------------------------
-- 7. RLS and privileges: service-path only.
-- ---------------------------------------------------------------------------
alter table public.catalog_work_scope_controls enable row level security;

do $$
declare fn text;
begin
  foreach fn in array array[
    'public.work_scope_control_in_sequence()',
    'public.work_scope_paused(uuid)',
    'public.set_work_scope_paused(uuid,boolean,uuid)',
    'public.bind_work_scope_batch_run(uuid,uuid,integer,text,uuid)',
    'public.create_work_scope_batch_run(uuid,uuid,integer,text,uuid,jsonb,text,jsonb,uuid,text,text,integer,integer)',
    'public.work_scope_progress(uuid)',
    'public.reconcile_lost_launch(uuid,text,integer,text)',
    'public.retire_unlaunched_run(uuid,integer,text)'
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

  execute 'revoke all on table public.catalog_work_scope_controls from public';
  if exists (select 1 from pg_roles where rolname='anon') then
    execute 'revoke all on table public.catalog_work_scope_controls from anon';
  end if;
  if exists (select 1 from pg_roles where rolname='authenticated') then
    execute 'revoke all on table public.catalog_work_scope_controls from authenticated';
  end if;
  if exists (select 1 from pg_roles where rolname='service_role') then
    execute 'grant select, insert on table public.catalog_work_scope_controls to service_role';
    execute 'revoke update, delete on table public.catalog_work_scope_controls from service_role';
  end if;
end $$;

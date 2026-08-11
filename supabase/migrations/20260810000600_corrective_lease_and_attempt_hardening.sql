-- Corrective pre-merge hardening (PR #42 review findings 1, 2 and 5).
--
-- 1) Cross-run settlement isolation: settle_model_call_budget_guarded
--    verified the worker lease against p_run_id but delegated settlement by
--    reservation id alone, so a valid lease for run A could settle a
--    reservation belonging to run B. The guarded settle now performs the
--    settlement itself in a single UPDATE whose WHERE clause binds the
--    reservation to (p_run_id, p_attempt) atomically.
--
-- 2) Database-clock lease enforcement: worker-originated run-row mutations
--    (usage snapshot, heartbeat, terminal transitions) previously compared
--    lease_expires_at against an application-generated timestamp. Container
--    clock skew must not decide lease validity, so those writes move into
--    guarded RPCs whose predicates use PostgreSQL now() with the
--    (run_id, worker_id, attempt, lease_token) invariant. Control-plane
--    (non-worker) transitions keep their existing service path.
--
-- 5) Per-attempt reservation identity: reservations were unique on
--    (run_id, call_seq), so a reclaimed attempt colliding with an earlier
--    attempt's reservation failed closed and blocked resumed runs. The
--    identity becomes (run_id, attempt, call_seq). Conservative under
--    crash/reclaim: earlier-attempt rows are never mutated or removed, and
--    model_call_budget_committed keeps counting every reserved/settled/
--    overage row regardless of attempt, so a possibly-spent provider call
--    from a dead attempt never disappears from budget accounting.
--
-- Additive and rerun-safe. All functions stay service-path only.

-- ---------------------------------------------------------------------------
-- 5) Attempt-aware reservation identity.
-- ---------------------------------------------------------------------------
alter table public.model_call_budget_reservations add column if not exists attempt integer not null default 1;
alter table public.model_call_budget_reservations drop constraint if exists model_call_budget_reservations_run_id_call_seq_key;
create unique index if not exists model_call_budget_reservations_run_attempt_seq_uidx
  on public.model_call_budget_reservations(run_id, attempt, call_seq);

-- Base reservation function (unchanged signature/return so the _v2 wrapper
-- and SQL callers keep working): the conflict target follows the new
-- identity; rows created through the base path belong to attempt 1.
create or replace function public.reserve_model_call_budget(
  p_run_id uuid,
  p_call_seq integer,
  p_user_id uuid,
  p_project_id uuid,
  p_estimated_cost numeric,
  p_daily_user_limit numeric,
  p_daily_project_limit numeric,
  p_provider text default 'moonshot',
  p_model text default 'kimi'
) returns public.model_call_budget_reservations
language plpgsql
security definer
set search_path = public
as $$
begin
  return public.reserve_model_call_budget_for_attempt(
    p_run_id, 1, p_call_seq, p_user_id, p_project_id, p_estimated_cost,
    p_daily_user_limit, p_daily_project_limit, p_provider, p_model
  );
end;
$$;

-- Canonical attempt-aware reservation implementation.
create or replace function public.reserve_model_call_budget_for_attempt(
  p_run_id uuid,
  p_attempt integer,
  p_call_seq integer,
  p_user_id uuid,
  p_project_id uuid,
  p_estimated_cost numeric,
  p_daily_user_limit numeric,
  p_daily_project_limit numeric,
  p_provider text default 'moonshot',
  p_model text default 'kimi'
) returns public.model_call_budget_reservations
language plpgsql
security definer
set search_path = public
as $$
declare
  v_day date := (now() at time zone 'utc')::date;
  v_user_spend numeric := 0;
  v_project_spend numeric := 0;
  v_row public.model_call_budget_reservations;
begin
  if p_run_id is null or p_attempt is null or p_attempt <= 0 or p_call_seq is null or p_call_seq <= 0 or p_estimated_cost is null or p_estimated_cost < 0 then
    raise exception 'invalid model-call budget reservation' using errcode = '22023';
  end if;
  if p_user_id is not null then
    perform pg_advisory_xact_lock(hashtext('milo:user-budget:' || p_user_id::text || ':' || v_day::text));
  end if;
  if p_project_id is not null then
    perform pg_advisory_xact_lock(hashtext('milo:project-budget:' || p_project_id::text || ':' || v_day::text));
  end if;

  select user_committed, project_committed into v_user_spend, v_project_spend
  from public.model_call_budget_committed(p_user_id, p_project_id, v_day);

  if p_daily_user_limit is not null and p_user_id is not null and v_user_spend + p_estimated_cost > p_daily_user_limit then
    insert into public.model_call_budget_reservations(run_id, attempt, call_seq, user_id, project_id, budget_date, provider, model, estimated_cost, status, rejection_reason)
    values (p_run_id, p_attempt, p_call_seq, p_user_id, p_project_id, v_day, p_provider, p_model, p_estimated_cost, 'rejected', 'DAILY_USER_BUDGET_REACHED')
    on conflict (run_id, attempt, call_seq) do update set status = 'rejected', rejection_reason = 'DAILY_USER_BUDGET_REACHED'
    returning * into v_row;
    return v_row;
  end if;
  if p_daily_project_limit is not null and p_project_id is not null and v_project_spend + p_estimated_cost > p_daily_project_limit then
    insert into public.model_call_budget_reservations(run_id, attempt, call_seq, user_id, project_id, budget_date, provider, model, estimated_cost, status, rejection_reason)
    values (p_run_id, p_attempt, p_call_seq, p_user_id, p_project_id, v_day, p_provider, p_model, p_estimated_cost, 'rejected', 'DAILY_PROJECT_BUDGET_REACHED')
    on conflict (run_id, attempt, call_seq) do update set status = 'rejected', rejection_reason = 'DAILY_PROJECT_BUDGET_REACHED'
    returning * into v_row;
    return v_row;
  end if;

  insert into public.model_call_budget_reservations(run_id, attempt, call_seq, user_id, project_id, budget_date, provider, model, estimated_cost, status)
  values (p_run_id, p_attempt, p_call_seq, p_user_id, p_project_id, v_day, p_provider, p_model, p_estimated_cost, 'reserved')
  on conflict (run_id, attempt, call_seq) do nothing
  returning * into v_row;
  if v_row is null then
    select * into v_row from public.model_call_budget_reservations where run_id = p_run_id and attempt = p_attempt and call_seq = p_call_seq;
  end if;
  return v_row;
end;
$$;

-- Guarded reservation: the lease attempt IS the reservation attempt.
drop function if exists public.reserve_model_call_budget_guarded(uuid, integer, uuid, uuid, numeric, numeric, numeric, text, integer, text, text, text);
create function public.reserve_model_call_budget_guarded(
  p_run_id uuid,
  p_call_seq integer,
  p_user_id uuid,
  p_project_id uuid,
  p_estimated_cost numeric,
  p_daily_user_limit numeric,
  p_daily_project_limit numeric,
  p_worker_id text,
  p_attempt integer,
  p_lease_token text,
  p_provider text default 'moonshot',
  p_model text default 'kimi'
) returns setof public.model_call_budget_reservations
language plpgsql
as $$
begin
  perform public.assert_worker_lease(p_run_id, p_worker_id, p_attempt, p_lease_token);
  return next public.reserve_model_call_budget_for_attempt(
    p_run_id, p_attempt, p_call_seq, p_user_id, p_project_id, p_estimated_cost,
    p_daily_user_limit, p_daily_project_limit, p_provider, p_model
  );
  return;
end;
$$;

-- ---------------------------------------------------------------------------
-- 1) Cross-run settlement isolation: settlement is bound to the SAME run
--    and attempt as the lease, in one atomic UPDATE.
-- ---------------------------------------------------------------------------
drop function if exists public.settle_model_call_budget_guarded(uuid, numeric, uuid, text, integer, text, text, text);
create function public.settle_model_call_budget_guarded(
  p_reservation_id uuid,
  p_actual_cost numeric,
  p_run_id uuid,
  p_worker_id text,
  p_attempt integer,
  p_lease_token text,
  p_status text default 'settled',
  p_rejection_reason text default null
) returns setof public.model_call_budget_reservations
language plpgsql
as $$
declare
  v_row public.model_call_budget_reservations;
begin
  perform public.assert_worker_lease(p_run_id, p_worker_id, p_attempt, p_lease_token);
  if p_reservation_id is null or p_actual_cost is null or p_actual_cost < 0 then
    raise exception 'invalid model-call budget settlement' using errcode = '22023';
  end if;
  if p_status not in ('settled','released','overage') then
    raise exception 'invalid model-call settlement status' using errcode = '22023';
  end if;
  update public.model_call_budget_reservations
     set actual_cost = p_actual_cost,
         status = p_status,
         rejection_reason = p_rejection_reason,
         settled_at = now()
   where id = p_reservation_id
     and run_id = p_run_id
     and attempt = p_attempt
     and status = 'reserved'
   returning * into v_row;
  if v_row is null then
    raise exception 'RESERVATION_RUN_MISMATCH_OR_SETTLED: reservation % is not a reserved entry of run % attempt %', p_reservation_id, p_run_id, p_attempt
      using errcode = '55000';
  end if;
  return next v_row;
  return;
end;
$$;

-- ---------------------------------------------------------------------------
-- 2) Database-clock guarded run-row mutations. Every predicate uses
--    PostgreSQL now(); a container with a skewed clock cannot extend or
--    outlive its lease. Null-tolerant predicates mirror the previous
--    conditional application-side filters, but expiry is always DB-decided.
-- ---------------------------------------------------------------------------
create or replace function public.update_run_usage_guarded(
  p_run_id uuid,
  p_worker_id text,
  p_attempt integer,
  p_lease_token text,
  p_usage jsonb
) returns setof public.runs
language plpgsql
as $$
declare
  v_row public.runs;
begin
  update public.runs
     set usage = coalesce(p_usage, '{}'::jsonb),
         updated_at = now()
   where id = p_run_id
     and worker_id = p_worker_id
     and (p_attempt is null or attempt = p_attempt)
     and (p_lease_token is null or lease_token = p_lease_token)
     and lease_expires_at > now()
   returning * into v_row;
  if v_row is null then
    raise exception 'STALE_WORKER_WRITE: usage write rejected; lease (worker %, attempt %) is not current for run %', p_worker_id, p_attempt, p_run_id
      using errcode = '55000';
  end if;
  return next v_row;
  return;
end;
$$;

create or replace function public.heartbeat_run_guarded(
  p_run_id uuid,
  p_worker_id text,
  p_attempt integer,
  p_lease_token text,
  p_lease_seconds integer default 300
) returns setof public.runs
language plpgsql
as $$
declare
  v_row public.runs;
begin
  update public.runs
     set last_heartbeat_at = now(),
         lease_expires_at = now() + make_interval(secs => greatest(1, coalesce(p_lease_seconds, 300))),
         updated_at = now()
   where id = p_run_id
     and worker_id = p_worker_id
     and (p_attempt is null or attempt = p_attempt)
     and (p_lease_token is null or lease_token = p_lease_token)
     and lease_expires_at > now()
     and status in ('starting','running','cancellation_requested')
   returning * into v_row;
  if v_row is null then
    raise exception 'STALE_WORKER_WRITE: heartbeat rejected; lease (worker %, attempt %) is not current for run %', p_worker_id, p_attempt, p_run_id
      using errcode = '55000';
  end if;
  insert into public.worker_heartbeats (run_id, worker_id, attempt, heartbeat_at, lease_expires_at)
  values (p_run_id, p_worker_id, coalesce(v_row.attempt, 1), now(), v_row.lease_expires_at);
  return next v_row;
  return;
end;
$$;

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
as $$
declare
  v_row public.runs;
begin
  update public.runs
     set status = p_status,
         output = coalesce(p_output, output),
         error = case when p_clear_error then null else coalesce(p_error, error) end,
         usage = coalesce(p_usage, usage),
         started_at = coalesce(p_started_at, started_at),
         finished_at = coalesce(p_finished_at, finished_at),
         updated_at = now()
   where id = p_run_id
     and worker_id = p_worker_id
     and (p_attempt is null or attempt = p_attempt)
     and (p_lease_token is null or lease_token = p_lease_token)
     and lease_expires_at > now()
     and (p_expected_status is null or status = p_expected_status)
   returning * into v_row;
  if v_row is null then
    raise exception 'STALE_WORKER_WRITE: transition to % rejected; lease (worker %, attempt %) is not current for run %', p_status, p_worker_id, p_attempt, p_run_id
      using errcode = '55000';
  end if;
  return next v_row;
  return;
end;
$$;

-- Service-path-only ACLs for every function introduced or recreated here.
do $$
declare
  fn text;
begin
  foreach fn in array array[
    'public.reserve_model_call_budget_for_attempt(uuid, integer, integer, uuid, uuid, numeric, numeric, numeric, text, text)',
    'public.reserve_model_call_budget_guarded(uuid, integer, uuid, uuid, numeric, numeric, numeric, text, integer, text, text, text)',
    'public.settle_model_call_budget_guarded(uuid, numeric, uuid, text, integer, text, text, text)',
    'public.update_run_usage_guarded(uuid, text, integer, text, jsonb)',
    'public.heartbeat_run_guarded(uuid, text, integer, text, integer)',
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

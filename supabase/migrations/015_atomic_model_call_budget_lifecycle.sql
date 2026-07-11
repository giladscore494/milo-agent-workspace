-- Additive model-call budget lifecycle. One canonical reservation row is used
-- for both user/day and project/day budgets, preventing user/project double
-- counting and preventing partial success when either limit fails.

create table if not exists public.model_call_budget_reservations (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references public.runs(id) on delete cascade,
  call_seq integer not null,
  user_id uuid,
  project_id uuid,
  budget_date date not null default ((now() at time zone 'utc')::date),
  provider text not null default 'moonshot',
  model text not null default 'kimi',
  estimated_cost numeric not null default 0,
  actual_cost numeric,
  status text not null default 'reserved' check (status in ('reserved','settled','released','rejected','overage')),
  rejection_reason text,
  created_at timestamptz not null default now(),
  settled_at timestamptz,
  unique (run_id, call_seq)
);

create index if not exists model_call_budget_reservations_user_day_idx
  on public.model_call_budget_reservations(user_id, budget_date, status)
  where user_id is not null;

create index if not exists model_call_budget_reservations_project_day_idx
  on public.model_call_budget_reservations(project_id, budget_date, status)
  where project_id is not null;

create or replace function public.model_call_budget_committed(p_user_id uuid, p_project_id uuid, p_budget_date date)
returns table(user_committed numeric, project_committed numeric)
language sql
security definer
set search_path = public
as $$
  select
    coalesce(sum(case when user_id = p_user_id then coalesce(actual_cost, estimated_cost, 0) else 0 end), 0)::numeric as user_committed,
    coalesce(sum(case when project_id = p_project_id then coalesce(actual_cost, estimated_cost, 0) else 0 end), 0)::numeric as project_committed
  from public.model_call_budget_reservations
  where budget_date = p_budget_date
    and status in ('reserved','settled','overage')
    and ((p_user_id is not null and user_id = p_user_id) or (p_project_id is not null and project_id = p_project_id));
$$;

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
declare
  v_day date := (now() at time zone 'utc')::date;
  v_user_spend numeric := 0;
  v_project_spend numeric := 0;
  v_row public.model_call_budget_reservations;
begin
  if p_run_id is null or p_call_seq is null or p_call_seq <= 0 or p_estimated_cost is null or p_estimated_cost < 0 then
    raise exception 'invalid model-call budget reservation' using errcode = '22023';
  end if;
  -- Deterministic lock order: user/day first, then project/day.
  if p_user_id is not null then
    perform pg_advisory_xact_lock(hashtext('milo:user-budget:' || p_user_id::text || ':' || v_day::text));
  end if;
  if p_project_id is not null then
    perform pg_advisory_xact_lock(hashtext('milo:project-budget:' || p_project_id::text || ':' || v_day::text));
  end if;

  select user_committed, project_committed into v_user_spend, v_project_spend
  from public.model_call_budget_committed(p_user_id, p_project_id, v_day);

  if p_daily_user_limit is not null and p_user_id is not null and v_user_spend + p_estimated_cost > p_daily_user_limit then
    insert into public.model_call_budget_reservations(run_id, call_seq, user_id, project_id, budget_date, provider, model, estimated_cost, status, rejection_reason)
    values (p_run_id, p_call_seq, p_user_id, p_project_id, v_day, p_provider, p_model, p_estimated_cost, 'rejected', 'DAILY_USER_BUDGET_REACHED')
    on conflict (run_id, call_seq) do update set status = 'rejected', rejection_reason = 'DAILY_USER_BUDGET_REACHED'
    returning * into v_row;
    return v_row;
  end if;
  if p_daily_project_limit is not null and p_project_id is not null and v_project_spend + p_estimated_cost > p_daily_project_limit then
    insert into public.model_call_budget_reservations(run_id, call_seq, user_id, project_id, budget_date, provider, model, estimated_cost, status, rejection_reason)
    values (p_run_id, p_call_seq, p_user_id, p_project_id, v_day, p_provider, p_model, p_estimated_cost, 'rejected', 'DAILY_PROJECT_BUDGET_REACHED')
    on conflict (run_id, call_seq) do update set status = 'rejected', rejection_reason = 'DAILY_PROJECT_BUDGET_REACHED'
    returning * into v_row;
    return v_row;
  end if;

  insert into public.model_call_budget_reservations(run_id, call_seq, user_id, project_id, budget_date, provider, model, estimated_cost, status)
  values (p_run_id, p_call_seq, p_user_id, p_project_id, v_day, p_provider, p_model, p_estimated_cost, 'reserved')
  on conflict (run_id, call_seq) do nothing
  returning * into v_row;
  if v_row is null then
    select * into v_row from public.model_call_budget_reservations where run_id = p_run_id and call_seq = p_call_seq;
  end if;
  return v_row;
end;
$$;

create or replace function public.settle_model_call_budget(
  p_reservation_id uuid,
  p_actual_cost numeric,
  p_status text default 'settled',
  p_rejection_reason text default null
) returns public.model_call_budget_reservations
language plpgsql
security definer
set search_path = public
as $$
declare
  v_row public.model_call_budget_reservations;
begin
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
     and status = 'reserved'
   returning * into v_row;
  if v_row is null then
    raise exception 'reservation already settled or missing' using errcode = '23505';
  end if;
  return v_row;
end;
$$;

revoke execute on function public.model_call_budget_committed(uuid, uuid, date) from public, authenticated;
revoke execute on function public.reserve_model_call_budget(uuid, integer, uuid, uuid, numeric, numeric, numeric, text, text) from public, authenticated;
revoke execute on function public.settle_model_call_budget(uuid, numeric, text, text) from public, authenticated;
grant execute on function public.model_call_budget_committed(uuid, uuid, date) to service_role;
grant execute on function public.reserve_model_call_budget(uuid, integer, uuid, uuid, numeric, numeric, numeric, text, text) to service_role;
grant execute on function public.settle_model_call_budget(uuid, numeric, text, text) to service_role;

-- Additive, idempotent RPCs for atomic daily user/project budget reservation.
create index if not exists usage_ledger_user_budget_day_idx
  on public.run_usage_ledger (user_id, ((created_at at time zone 'utc')::date), decision)
  where user_id is not null;

create index if not exists usage_ledger_project_budget_day_idx
  on public.run_usage_ledger (project_id, ((created_at at time zone 'utc')::date), decision)
  where project_id is not null;

create or replace function public.reserve_daily_user_budget(
  p_run_id uuid,
  p_user_id uuid,
  p_amount numeric,
  p_daily_limit numeric,
  p_provider text default 'moonshot',
  p_model text default 'kimi'
) returns public.run_usage_ledger
language plpgsql
security definer
set search_path = public
as $$
declare
  v_day date := (now() at time zone 'utc')::date;
  v_spend numeric := 0;
  v_row public.run_usage_ledger;
begin
  if p_user_id is null or p_amount is null or p_amount < 0 or p_daily_limit is null or p_daily_limit <= 0 then
    raise exception 'invalid daily user budget reservation' using errcode = '22023';
  end if;
  perform pg_advisory_xact_lock(hashtext('milo:user-budget:' || p_user_id::text || ':' || v_day::text));
  select coalesce(sum(coalesce(actual_cost, estimated_cost, 0)), 0) into v_spend
    from public.run_usage_ledger
    where user_id = p_user_id
      and (created_at at time zone 'utc')::date = v_day
      and decision in ('reserved','settled');
  if v_spend + p_amount > p_daily_limit then
    insert into public.run_usage_ledger(run_id, user_id, provider, model, decision, rejection_reason, estimated_cost)
    values (p_run_id, p_user_id, p_provider, p_model, 'rejected', 'DAILY_USER_BUDGET_REACHED', p_amount)
    returning * into v_row;
    return v_row;
  end if;
  insert into public.run_usage_ledger(run_id, user_id, provider, model, decision, estimated_cost)
  values (p_run_id, p_user_id, p_provider, p_model, 'reserved', p_amount)
  returning * into v_row;
  return v_row;
end;
$$;

create or replace function public.reserve_daily_project_budget(
  p_run_id uuid,
  p_project_id uuid,
  p_amount numeric,
  p_daily_limit numeric,
  p_provider text default 'moonshot',
  p_model text default 'kimi'
) returns public.run_usage_ledger
language plpgsql
security definer
set search_path = public
as $$
declare
  v_day date := (now() at time zone 'utc')::date;
  v_spend numeric := 0;
  v_row public.run_usage_ledger;
begin
  if p_project_id is null or p_amount is null or p_amount < 0 or p_daily_limit is null or p_daily_limit <= 0 then
    raise exception 'invalid daily project budget reservation' using errcode = '22023';
  end if;
  perform pg_advisory_xact_lock(hashtext('milo:project-budget:' || p_project_id::text || ':' || v_day::text));
  select coalesce(sum(coalesce(actual_cost, estimated_cost, 0)), 0) into v_spend
    from public.run_usage_ledger
    where project_id = p_project_id
      and (created_at at time zone 'utc')::date = v_day
      and decision in ('reserved','settled');
  if v_spend + p_amount > p_daily_limit then
    insert into public.run_usage_ledger(run_id, project_id, provider, model, decision, rejection_reason, estimated_cost)
    values (p_run_id, p_project_id, p_provider, p_model, 'rejected', 'DAILY_PROJECT_BUDGET_REACHED', p_amount)
    returning * into v_row;
    return v_row;
  end if;
  insert into public.run_usage_ledger(run_id, project_id, provider, model, decision, estimated_cost)
  values (p_run_id, p_project_id, p_provider, p_model, 'reserved', p_amount)
  returning * into v_row;
  return v_row;
end;
$$;

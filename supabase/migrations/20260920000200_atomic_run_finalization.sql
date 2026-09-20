-- Atomic run finalization: a run's terminal transition and its terminal event
-- commit in ONE transaction, or neither does.
--
-- Why. A run became terminal through two separate lease-guarded writes: the
-- runs-row transition (`transition_run_worker_guarded`) and the terminal
-- event append (`append_run_event_guarded`). Each write is fenced by the
-- lease, but NOTHING tied the two to each other, so the event could win while
-- the transition lost. Concretely: a worker reads `running` and decides
-- `completed`; an operator requests cancellation; the `run_completed` event --
-- carrying the canonical ProductOutcome -- is appended (the lease is still
-- valid, and an event append checks the lease, not the decision); the
-- `completed` transition is then rejected because the row is no longer
-- `running`. The database is left with a terminal-looking `run_completed`
-- event for a decision that never won, and Stage D reads the ProductOutcome
-- from exactly those events (scripts/release/stage-d/probe_db.py).
--
-- What. `finalize_run_guarded` performs both writes inside one function body,
-- so they are one transaction:
--   * the runs row is updated under the SAME predicates as
--     `transition_run_worker_guarded` -- lease under the database clock, the
--     attempt and token, and a compare-and-set on `p_expected_status` -- and
--     that CAS is MANDATORY here: a finalization is always a decision taken
--     under an observed state, and it is rejected the moment that state has
--     moved (`STALE_WORKER_WRITE`, errcode 55000, the existing contract);
--   * only after the transition has won is the terminal event inserted; and
--     because both are in one transaction, an event insert that fails rolls
--     the transition back with it. A terminal event therefore exists ONLY for
--     the decision that durably won, and a terminal run can never be left
--     without the evidence its terminal event carries.
-- `runs.usage` merges monotonically exactly as the transition writer does.
--
-- Only TERMINAL statuses are accepted: this function exists to make a run's
-- ending atomic, not to replace the general transition writer.
--
-- Additive, idempotent, data-preserving. No table is dropped, no row deleted.
-- Service-path only per the repository convention. Depends on
-- `merge_execution_usage` from `20260920000100`, which orders before it.

create or replace function public.finalize_run_guarded(
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
  p_finished_at timestamptz default null,
  p_event_type text default null,
  p_event_message text default null,
  p_event_payload jsonb default '{}'::jsonb
) returns setof public.runs
language plpgsql
as $$
declare
  v_row public.runs;
begin
  if p_status is null or p_status not in
       ('completed', 'partial_success', 'failed', 'cancelled', 'timed_out', 'budget_exhausted') then
    raise exception 'RUN_FINALIZATION_INVALID: % is not a terminal run status', p_status
      using errcode = '22023';
  end if;
  if p_expected_status is null then
    raise exception 'RUN_FINALIZATION_INVALID: finalization requires the observed status it was decided under'
      using errcode = '22023';
  end if;
  if p_event_type is not null and p_event_payload is not null
     and jsonb_typeof(p_event_payload) <> 'object' then
    raise exception 'RUN_FINALIZATION_INVALID: a terminal event payload must be an object'
      using errcode = '22023';
  end if;

  -- The transition. Lease validity is decided by the database clock, and the
  -- compare-and-set on the observed status is what makes a decision taken
  -- under a state that has since moved (a cancellation request, a reclaim)
  -- lose here rather than overwrite it. The UPDATE evaluates its predicates
  -- on the latest committed row under the row lock, so a concurrent
  -- `cancellation_requested` that commits first is seen.
  update public.runs
     set status = p_status,
         output = coalesce(p_output, output),
         error = case when p_clear_error then null else coalesce(p_error, error) end,
         usage = case when p_usage is null then usage
                      else public.merge_execution_usage(usage, p_usage) end,
         finished_at = coalesce(p_finished_at, finished_at, now()),
         updated_at = now()
   where id = p_run_id
     and worker_id = p_worker_id
     and (p_attempt is null or attempt = p_attempt)
     and (p_lease_token is null or lease_token = p_lease_token)
     and lease_expires_at > now()
     and status = p_expected_status
   returning * into v_row;
  if v_row is null then
    raise exception 'STALE_WORKER_WRITE: finalization to % rejected; lease (worker %, attempt %) is not current or run % is no longer %',
      p_status, p_worker_id, p_attempt, p_run_id, p_expected_status
      using errcode = '55000';
  end if;

  -- The terminal event, AFTER the transition has won and in the same
  -- transaction: it can never describe a decision that did not become the
  -- run's durable state, and a failure here takes the transition down with it.
  if p_event_type is not null then
    insert into public.run_events (run_id, event_type, message, payload)
    values (p_run_id, p_event_type, p_event_message, coalesce(p_event_payload, '{}'::jsonb));
  end if;

  return next v_row;
  return;
end;
$$;

-- Service-path-only ACLs, per the repository convention.
do $$
declare
  fn text;
begin
  foreach fn in array array[
    'public.finalize_run_guarded(uuid, text, text, text, integer, text, jsonb, jsonb, boolean, jsonb, timestamptz, text, text, jsonb)'
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

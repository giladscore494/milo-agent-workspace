-- Lease-guarded worker writes: every worker-originated durable write is
-- rejected atomically at the database boundary when the writing worker's
-- lease (run_id, worker_id, attempt, lease_token) is no longer current.
--
-- Before this migration only runs-row transitions carried lease predicates;
-- run_events inserts, checkpoints, blackboard upserts, agent messages,
-- supervisor decisions and budget reservation/settlement RPCs accepted
-- writes from stale (reclaimed) workers. Each guarded function below takes
-- FOR SHARE on the runs row while validating the lease, so a concurrent
-- lease reclaim (claim_run_lease's UPDATE) serializes against the write:
-- the check and the insert commit atomically or not at all.
--
-- Additive, idempotent, data-preserving. All functions are service-path
-- only per the repository convention (revoke public/anon/authenticated,
-- grant service_role).

drop function if exists public.assert_worker_lease(uuid, text, integer, text);
create function public.assert_worker_lease(
  p_run_id uuid,
  p_worker_id text,
  p_attempt integer,
  p_lease_token text
) returns void
language plpgsql
as $$
begin
  perform 1 from public.runs
    where id = p_run_id
      and worker_id = p_worker_id
      and attempt = p_attempt
      and lease_token = p_lease_token
      and lease_expires_at > now()
    for share;
  if not found then
    raise exception 'STALE_WORKER_WRITE: lease (worker %, attempt %) is not current for run %', p_worker_id, p_attempt, p_run_id
      using errcode = '55000';
  end if;
end;
$$;

drop function if exists public.append_run_event_guarded(uuid, text, integer, text, text, text, text, text, jsonb, jsonb);
create function public.append_run_event_guarded(
  p_run_id uuid,
  p_worker_id text,
  p_attempt integer,
  p_lease_token text,
  p_event_type text,
  p_message text default null,
  p_agent text default null,
  p_phase text default null,
  p_progress jsonb default null,
  p_payload jsonb default '{}'::jsonb
) returns setof public.run_events
language plpgsql
as $$
declare
  v_row public.run_events;
begin
  perform public.assert_worker_lease(p_run_id, p_worker_id, p_attempt, p_lease_token);
  insert into public.run_events (run_id, event_type, message, agent, phase, progress, payload)
  values (p_run_id, p_event_type, p_message, p_agent, p_phase, p_progress, coalesce(p_payload, '{}'::jsonb))
  returning * into v_row;
  return next v_row;
  return;
end;
$$;

drop function if exists public.save_checkpoint_guarded(uuid, text, integer, text, text, text, text, jsonb, jsonb, jsonb, jsonb, jsonb);
create function public.save_checkpoint_guarded(
  p_run_id uuid,
  p_worker_id text,
  p_attempt integer,
  p_lease_token text,
  p_engine_version text,
  p_workflow_key text,
  p_phase text,
  p_completed_tasks jsonb default '[]'::jsonb,
  p_artifacts jsonb default '{}'::jsonb,
  p_failures jsonb default '[]'::jsonb,
  p_token_usage jsonb default '{}'::jsonb,
  p_last_event jsonb default null
) returns setof public.run_checkpoints
language plpgsql
as $$
declare
  v_row public.run_checkpoints;
begin
  perform public.assert_worker_lease(p_run_id, p_worker_id, p_attempt, p_lease_token);
  insert into public.run_checkpoints (run_id, engine_version, workflow_key, phase, completed_tasks, artifacts, failures, token_usage, last_event, attempt)
  values (
    p_run_id, p_engine_version, p_workflow_key, p_phase,
    coalesce(p_completed_tasks, '[]'::jsonb), coalesce(p_artifacts, '{}'::jsonb),
    coalesce(p_failures, '[]'::jsonb), coalesce(p_token_usage, '{}'::jsonb),
    p_last_event, p_attempt
  )
  returning * into v_row;
  return next v_row;
  return;
end;
$$;

drop function if exists public.upsert_run_blackboard_guarded(uuid, text, integer, text, jsonb);
create function public.upsert_run_blackboard_guarded(
  p_run_id uuid,
  p_worker_id text,
  p_attempt integer,
  p_lease_token text,
  p_blackboard jsonb
) returns setof public.run_blackboards
language plpgsql
as $$
declare
  v_row public.run_blackboards;
begin
  perform public.assert_worker_lease(p_run_id, p_worker_id, p_attempt, p_lease_token);
  insert into public.run_blackboards (
    run_id, goal, approved_plan, known_entities, completed_tasks, active_agents,
    open_questions, missing_fields, claims_conflict_summaries, artifacts,
    remaining_budget, completion_score, updated_at
  )
  values (
    p_run_id,
    coalesce(p_blackboard->>'goal', ''),
    coalesce(p_blackboard->'approved_plan', '{}'::jsonb),
    coalesce(p_blackboard->'known_entities', '[]'::jsonb),
    coalesce(p_blackboard->'completed_tasks', '[]'::jsonb),
    coalesce(p_blackboard->'active_agents', '[]'::jsonb),
    coalesce(p_blackboard->'open_questions', '[]'::jsonb),
    coalesce(p_blackboard->'missing_fields', '[]'::jsonb),
    coalesce(p_blackboard->'claims_conflict_summaries', '[]'::jsonb),
    coalesce(p_blackboard->'artifacts', '{}'::jsonb),
    coalesce(p_blackboard->'remaining_budget', '{}'::jsonb),
    coalesce((p_blackboard->>'completion_score')::numeric, 0),
    now()
  )
  on conflict (run_id) do update set
    goal = excluded.goal,
    approved_plan = excluded.approved_plan,
    known_entities = excluded.known_entities,
    completed_tasks = excluded.completed_tasks,
    active_agents = excluded.active_agents,
    open_questions = excluded.open_questions,
    missing_fields = excluded.missing_fields,
    claims_conflict_summaries = excluded.claims_conflict_summaries,
    artifacts = excluded.artifacts,
    remaining_budget = excluded.remaining_budget,
    completion_score = excluded.completion_score,
    updated_at = now()
  returning * into v_row;
  return next v_row;
  return;
end;
$$;

drop function if exists public.create_agent_message_guarded(uuid, text, integer, text, jsonb);
create function public.create_agent_message_guarded(
  p_run_id uuid,
  p_worker_id text,
  p_attempt integer,
  p_lease_token text,
  p_message jsonb
) returns setof public.agent_messages
language plpgsql
as $$
declare
  v_row public.agent_messages;
begin
  perform public.assert_worker_lease(p_run_id, p_worker_id, p_attempt, p_lease_token);
  insert into public.agent_messages (id, run_id, message_type, sender, recipient, task_key, payload, read_at)
  values (
    coalesce(nullif(p_message->>'id', '')::uuid, gen_random_uuid()),
    p_run_id,
    p_message->>'message_type',
    p_message->>'sender',
    p_message->>'recipient',
    nullif(p_message->>'task_key', ''),
    coalesce(p_message->'payload', '{}'::jsonb),
    nullif(p_message->>'read_at', '')::timestamptz
  )
  returning * into v_row;
  return next v_row;
  return;
end;
$$;

drop function if exists public.create_supervisor_decision_guarded(uuid, text, integer, text, jsonb);
create function public.create_supervisor_decision_guarded(
  p_run_id uuid,
  p_worker_id text,
  p_attempt integer,
  p_lease_token text,
  p_decision jsonb
) returns setof public.supervisor_decisions
language plpgsql
as $$
declare
  v_row public.supervisor_decisions;
begin
  perform public.assert_worker_lease(p_run_id, p_worker_id, p_attempt, p_lease_token);
  insert into public.supervisor_decisions (run_id, mode, input, assessment, proposed_commands, next_wake_condition, rationale_summary, evaluation_report)
  values (
    p_run_id,
    'shadow',
    coalesce(p_decision->'input', '{}'::jsonb),
    coalesce(p_decision->>'assessment', ''),
    coalesce(p_decision->'proposed_commands', '[]'::jsonb),
    coalesce(p_decision->'next_wake_condition', '{}'::jsonb),
    coalesce(p_decision->>'rationale_summary', ''),
    coalesce(p_decision->'evaluation_report', '{}'::jsonb)
  )
  returning * into v_row;
  return next v_row;
  return;
end;
$$;

-- Budget reservation/settlement lease guards live in
-- 20260810000600_corrective_lease_and_attempt_hardening.sql, which owns
-- reserve_model_call_budget_guarded / settle_model_call_budget_guarded with
-- attempt-aware, run-bound semantics. They are deliberately NOT defined
-- here so re-running this file can never revert them.

-- Service-path only: revoke browser access, grant the trusted backend.
do $$
declare
  fn text;
begin
  foreach fn in array array[
    'public.assert_worker_lease(uuid, text, integer, text)',
    'public.append_run_event_guarded(uuid, text, integer, text, text, text, text, text, jsonb, jsonb)',
    'public.save_checkpoint_guarded(uuid, text, integer, text, text, text, text, jsonb, jsonb, jsonb, jsonb, jsonb)',
    'public.upsert_run_blackboard_guarded(uuid, text, integer, text, jsonb)',
    'public.create_agent_message_guarded(uuid, text, integer, text, jsonb)',
    'public.create_supervisor_decision_guarded(uuid, text, integer, text, jsonb)'
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

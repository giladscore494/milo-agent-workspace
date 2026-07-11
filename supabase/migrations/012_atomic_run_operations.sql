-- Atomic run operations: transactional message+run creation with
-- concurrency admission, launch-ownership reconciliation state, and an
-- atomic worker lease.
--
-- Additive, idempotent, data-preserving.

-- 1) launch_unknown: reconciliation state for uncertain launch responses
--    (e.g. a network timeout after the launch request was sent). A run in
--    launch_unknown is never automatically relaunched.
do $$
begin
  alter table public.runs drop constraint if exists runs_launch_state_check;
  alter table public.runs add constraint runs_launch_state_check check (launch_state in (
    'none','pending','launching','launched','launch_failed','launch_unknown'
  ));
end $$;

-- 2) Transactional user-message + run creation with idempotent replay and
--    concurrency admission. Advisory transaction locks serialize admission
--    per requesting user and per project, so concurrent requests cannot
--    both pass a count-then-insert check. Service-path only.
create or replace function public.create_message_and_run(
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
as $$
declare
  v_project uuid;
  v_existing public.runs;
  v_message_id bigint;
  v_run public.runs;
  v_active integer;
begin
  select project_id into v_project from public.conversations where id = p_conversation_id;
  if v_project is null then
    raise exception 'CONVERSATION_NOT_FOUND';
  end if;

  -- Serialize same-user and same-project admission.
  perform pg_advisory_xact_lock(hashtext('milo_run_user_' || p_requested_by::text));
  perform pg_advisory_xact_lock(hashtext('milo_run_project_' || v_project::text));

  if p_idempotency_key is not null then
    select * into v_existing from public.runs
     where conversation_id = p_conversation_id
       and requested_by = p_requested_by
       and idempotency_key = p_idempotency_key;
    if found then
      return jsonb_build_object('run', to_jsonb(v_existing), 'created', false);
    end if;
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

  insert into public.runs (conversation_id, status, launch_state, requested_by, idempotency_key, request_fingerprint, input)
  values (
    p_conversation_id, 'queued', 'pending', p_requested_by, p_idempotency_key, p_request_fingerprint,
    jsonb_build_object('message_id', v_message_id::text, 'content', p_content, 'metadata', coalesce(p_metadata, '{}'::jsonb))
  )
  returning * into v_run;

  return jsonb_build_object('run', to_jsonb(v_run), 'created', true);
end;
$$;

revoke execute on function public.create_message_and_run(uuid, text, jsonb, uuid, text, text, integer, integer) from public;
revoke execute on function public.create_message_and_run(uuid, text, jsonb, uuid, text, text, integer, integer) from authenticated;

-- 3) Atomic worker lease claim: a single compare-and-set statement. Only
--    one caller can hold an unexpired lease; expired leases are reclaimable
--    with an incremented attempt; a pre-claim cancellation request stays
--    visible (status is preserved). Returns no row when the lease is held.
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
    lease_expires_at = now() + make_interval(secs => p_lease_seconds),
    started_at = coalesce(started_at, now()),
    updated_at = now()
  where id = p_run_id
    and (
      (status in ('queued','launching','waiting','cancellation_requested')
        and (worker_id is null or worker_id = p_worker_id
             or lease_expires_at is null or lease_expires_at < now()))
      or (status in ('starting','running')
        and (worker_id = p_worker_id or lease_expires_at is null or lease_expires_at < now()))
    )
  returning *;
$$;

revoke execute on function public.claim_run_lease(uuid, text, integer) from public;
revoke execute on function public.claim_run_lease(uuid, text, integer) from authenticated;

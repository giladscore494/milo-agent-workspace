-- Execution usage ledger: ONE durable, monotonic, versioned cumulative usage
-- record per run, and monotonic writes everywhere `runs.usage` is touched.
--
-- Why. A run's usage used to live in `runs.usage`, REWRITTEN from the worker's
-- in-process snapshot after every settled provider call, and in a checkpoint's
-- `token_usage`, written at task and batch boundaries. Both writes were plain
-- overwrites: a lease-valid snapshot that was merely BEHIND (an earlier thread,
-- a worker that restored less than what was durable) could lower a counter,
-- and a resumed V1 worker restored nothing at all. Every one of those hands a
-- run capacity it already spent.
--
-- What. `run_execution_usage` holds the full cumulative ledger of a run
-- (`backend/execution_usage.py` names the dimensions: model calls, provider
-- attempts and failures, tokens, cost, semantic retries, backpressure events,
-- agent steps, tool calls, task executions, search invocations and cost,
-- replans, correction rounds). It is written ONLY through
-- `record_run_usage_guarded`, which
--   * verifies the writing worker's lease atomically, taking the runs row
--     FOR UPDATE so a concurrent reclaim and concurrent settles serialize;
--   * MERGES rather than overwrites: `merge_execution_usage` is a
--     component-wise maximum, so no accepted write can ever reduce a counter,
--     a retried write is a no-op, and argument order cannot matter;
--   * VERSIONS the record: `version` advances on every accepted CHANGE and
--     never on a duplicate, so a caller can tell an idempotent replay from a
--     real advance and a stale writer can never overwrite a newer record;
--   * projects the bounded public shape into `runs.usage` in the SAME
--     transaction, also by merge, so the browser contract stays exactly what
--     it was and stays monotonic.
-- The table's BEFORE UPDATE trigger enforces the invariant even against a
-- direct service-path write: no numeric dimension may go down.
--
-- `update_run_usage_guarded` and `transition_run_worker_guarded` keep their
-- signatures (no ACL, probe or repository change) and become monotonic too.
--
-- Additive, idempotent, data-preserving. No table is dropped, no row deleted.
-- Every function is service-path only per the repository convention.

create table if not exists public.run_execution_usage (
  run_id uuid primary key references public.runs(id) on delete cascade,
  schema_version integer not null default 1,
  version bigint not null default 0,
  attempt integer not null default 1,
  worker_id text,
  ledger jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.run_execution_usage enable row level security;
-- No policies: browser roles have no access at all; only the trusted
-- service path reads and writes, and it writes through the guarded RPC.

-- ---------------------------------------------------------------------------
-- The monotonic merge. Every ledger dimension is cumulative by contract, so
-- the merge of two records of ONE run is the component-wise maximum over
-- every numeric key either record carries. `total_tokens` is derived and is
-- recomputed from the merged components rather than maximised on its own.
-- A non-numeric value (none exists today) is taken from the incoming record.
-- ---------------------------------------------------------------------------
create or replace function public.merge_execution_usage(p_current jsonb, p_incoming jsonb)
returns jsonb
language plpgsql
immutable
as $$
declare
  v_current jsonb := coalesce(p_current, '{}'::jsonb);
  v_incoming jsonb := coalesce(p_incoming, '{}'::jsonb);
  v_merged jsonb := '{}'::jsonb;
  v_key text;
  v_a jsonb;
  v_b jsonb;
begin
  if jsonb_typeof(v_current) <> 'object' or jsonb_typeof(v_incoming) <> 'object' then
    raise exception 'USAGE_LEDGER_INVALID: usage records must be objects' using errcode = '22023';
  end if;
  for v_key in
    select distinct key from (
      select key from jsonb_object_keys(v_current) as key
      union all
      select key from jsonb_object_keys(v_incoming) as key
    ) keys
  loop
    if v_key = 'total_tokens' then
      continue;
    end if;
    v_a := v_current -> v_key;
    v_b := v_incoming -> v_key;
    if v_a is not null and v_b is not null
       and jsonb_typeof(v_a) = 'number' and jsonb_typeof(v_b) = 'number' then
      if (v_a::text)::numeric < 0 or (v_b::text)::numeric < 0 then
        raise exception 'USAGE_LEDGER_INVALID: negative usage value for %', v_key using errcode = '22023';
      end if;
      v_merged := v_merged || jsonb_build_object(
        v_key, case when (v_a::text)::numeric >= (v_b::text)::numeric then v_a else v_b end);
    elsif v_b is not null then
      if jsonb_typeof(v_b) = 'number' and (v_b::text)::numeric < 0 then
        raise exception 'USAGE_LEDGER_INVALID: negative usage value for %', v_key using errcode = '22023';
      end if;
      v_merged := v_merged || jsonb_build_object(v_key, v_b);
    else
      if jsonb_typeof(v_a) = 'number' and (v_a::text)::numeric < 0 then
        raise exception 'USAGE_LEDGER_INVALID: negative usage value for %', v_key using errcode = '22023';
      end if;
      v_merged := v_merged || jsonb_build_object(v_key, v_a);
    end if;
  end loop;
  if v_merged ? 'input_tokens' or v_merged ? 'output_tokens' then
    v_merged := v_merged || jsonb_build_object('total_tokens',
      coalesce((v_merged ->> 'input_tokens')::numeric, 0) + coalesce((v_merged ->> 'output_tokens')::numeric, 0));
  end if;
  return v_merged;
end;
$$;

-- The bounded public `runs.usage` projection of a ledger record. The key set
-- is `backend.schemas.RunUsage` / `backend.execution_usage.PUBLIC_USAGE_FIELDS`
-- and is deliberately NOT widened by this migration.
create or replace function public.execution_usage_public_projection(p_ledger jsonb)
returns jsonb
language sql
immutable
as $$
  select coalesce(
    (select jsonb_object_agg(key, value)
       from jsonb_each(coalesce(p_ledger, '{}'::jsonb))
      where key in ('model_calls', 'input_tokens', 'output_tokens', 'total_tokens',
                    'estimated_cost', 'actual_cost', 'retries',
                    'provider_backpressure_events', 'agent_steps', 'elapsed_seconds')),
    '{}'::jsonb);
$$;

-- The invariant at the storage boundary: an UPDATE may never lower a numeric
-- dimension of the ledger, and a changed ledger must carry a higher version.
create or replace function public.run_execution_usage_enforce_monotonic() returns trigger
language plpgsql as $$
declare
  v_key text;
  v_old jsonb;
  v_new jsonb;
begin
  for v_key in select key from jsonb_object_keys(old.ledger) as key loop
    if v_key = 'total_tokens' then
      continue;
    end if;
    v_old := old.ledger -> v_key;
    v_new := new.ledger -> v_key;
    if jsonb_typeof(v_old) = 'number' then
      if v_new is null or jsonb_typeof(v_new) <> 'number'
         or (v_new::text)::numeric < (v_old::text)::numeric then
        raise exception 'USAGE_LEDGER_NOT_MONOTONIC: % may not decrease for run %', v_key, old.run_id
          using errcode = '55000';
      end if;
    end if;
  end loop;
  if new.ledger is distinct from old.ledger and new.version <= old.version then
    raise exception 'USAGE_LEDGER_NOT_MONOTONIC: a changed ledger must advance the version for run %', old.run_id
      using errcode = '55000';
  end if;
  if new.version < old.version then
    raise exception 'USAGE_LEDGER_NOT_MONOTONIC: version may not decrease for run %', old.run_id
      using errcode = '55000';
  end if;
  new.updated_at := now();
  return new;
end;
$$;

drop trigger if exists run_execution_usage_monotonic on public.run_execution_usage;
create trigger run_execution_usage_monotonic
  before update on public.run_execution_usage
  for each row execute function public.run_execution_usage_enforce_monotonic();

-- ---------------------------------------------------------------------------
-- The ONE write path. Lease-guarded, merging, versioned, idempotent.
-- ---------------------------------------------------------------------------
drop function if exists public.record_run_usage_guarded(uuid, text, integer, text, jsonb);
create function public.record_run_usage_guarded(
  p_run_id uuid,
  p_worker_id text,
  p_attempt integer,
  p_lease_token text,
  p_ledger jsonb
) returns setof public.run_execution_usage
language plpgsql
as $$
declare
  v_incoming jsonb;
  v_existing public.run_execution_usage;
  v_merged jsonb;
  v_row public.run_execution_usage;
  v_schema integer;
begin
  -- The lease check and the lock order are ONE decision. Concurrent settles
  -- of the same run (Swarm V2 executes tasks on several threads) each write
  -- the ledger row AND the runs row, so every writer takes the runs row FOR
  -- UPDATE first and the ledger row second. `assert_worker_lease`'s FOR SHARE
  -- would let two writers hold the runs row together and then deadlock when
  -- the first tries to upgrade it for the `runs.usage` projection below.
  -- The predicate is the same (run, worker, attempt, token, DB-clock expiry).
  perform 1 from public.runs
    where id = p_run_id
      and worker_id = p_worker_id
      and attempt = p_attempt
      and lease_token = p_lease_token
      and lease_expires_at > now()
    for update;
  if not found then
    raise exception 'STALE_WORKER_WRITE: lease (worker %, attempt %) is not current for run %', p_worker_id, p_attempt, p_run_id
      using errcode = '55000';
  end if;
  if p_ledger is null or jsonb_typeof(p_ledger) <> 'object' then
    raise exception 'USAGE_LEDGER_INVALID: ledger must be an object' using errcode = '22023';
  end if;
  -- The write sequence number is owned by the database, never by the caller.
  v_incoming := p_ledger - 'ledger_version';
  v_schema := greatest(1, coalesce((v_incoming ->> 'schema_version')::integer, 1));

  insert into public.run_execution_usage (run_id, schema_version, version, attempt, worker_id, ledger)
  values (p_run_id, v_schema, 0, coalesce(p_attempt, 1), p_worker_id, '{}'::jsonb)
  on conflict (run_id) do nothing;
  select * into v_existing from public.run_execution_usage where run_id = p_run_id for update;

  v_merged := public.merge_execution_usage(v_existing.ledger, v_incoming);
  if v_merged is distinct from v_existing.ledger or v_existing.version = 0 then
    v_merged := v_merged || jsonb_build_object('ledger_version', v_existing.version + 1,
                                               'schema_version', greatest(v_schema, v_existing.schema_version));
    update public.run_execution_usage
       set ledger = v_merged,
           version = v_existing.version + 1,
           schema_version = greatest(v_schema, v_existing.schema_version),
           attempt = greatest(v_existing.attempt, coalesce(p_attempt, 1)),
           worker_id = p_worker_id,
           updated_at = now()
     where run_id = p_run_id
     returning * into v_row;
  else
    -- An idempotent replay: nothing advanced, so the version stays put.
    v_row := v_existing;
  end if;

  -- The public aggregate is a projection of the ledger, merged in the same
  -- transaction under the same lease, so it can never be lower than what the
  -- ledger shows and never carries a field outside the public contract.
  update public.runs
     set usage = public.merge_execution_usage(usage, public.execution_usage_public_projection(v_row.ledger)),
         updated_at = now()
   where id = p_run_id
     and worker_id = p_worker_id
     and attempt = p_attempt
     and lease_token = p_lease_token
     and lease_expires_at > now();
  if not found then
    raise exception 'STALE_WORKER_WRITE: usage write rejected; lease (worker %, attempt %) is not current for run %', p_worker_id, p_attempt, p_run_id
      using errcode = '55000';
  end if;
  return next v_row;
  return;
end;
$$;

-- ---------------------------------------------------------------------------
-- Existing usage writers become monotonic. Same signatures, same lease
-- predicates (database clock), same STALE_WORKER_WRITE contract.
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
     set usage = public.merge_execution_usage(usage, coalesce(p_usage, '{}'::jsonb)),
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
         usage = case when p_usage is null then usage
                      else public.merge_execution_usage(usage, p_usage) end,
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

-- Service-path-only ACLs for every function introduced or recreated here,
-- and for the table (RLS with no policies already denies browser roles; the
-- explicit revoke keeps that true even if a policy is ever added).
do $$
declare
  fn text;
begin
  foreach fn in array array[
    'public.merge_execution_usage(jsonb, jsonb)',
    'public.execution_usage_public_projection(jsonb)',
    'public.run_execution_usage_enforce_monotonic()',
    'public.record_run_usage_guarded(uuid, text, integer, text, jsonb)',
    'public.update_run_usage_guarded(uuid, text, integer, text, jsonb)',
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

  execute 'revoke all on table public.run_execution_usage from public';
  if exists (select 1 from pg_roles where rolname = 'anon') then
    execute 'revoke all on table public.run_execution_usage from anon';
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    execute 'revoke all on table public.run_execution_usage from authenticated';
  end if;
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    execute 'grant select, insert, update on table public.run_execution_usage to service_role';
  end if;
end $$;

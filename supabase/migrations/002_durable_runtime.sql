-- Stage 3 durable run runtime: events, checkpoints, leases, heartbeats, cancellation.
--
-- Includes legacy baseline reconciliation for public.runs and
-- public.run_events, which exist in production with a pre-migration shape
-- (user_prompt/result/error_message on runs; a bigint identity id,
-- event_type, agent_name, and an integer progress column on run_events).
-- Legacy columns and their data are preserved; only shapes required by the
-- current backend are added. run_events.id remains bigint; it is never
-- converted to uuid.

alter table public.runs add column if not exists attempt integer not null default 1;
alter table public.runs add column if not exists started_at timestamptz;
alter table public.runs add column if not exists finished_at timestamptz;
alter table public.runs add column if not exists last_heartbeat_at timestamptz;
alter table public.runs add column if not exists worker_id text;
alter table public.runs add column if not exists lease_expires_at timestamptz;
alter table public.runs add column if not exists cancellation_requested_at timestamptz;
alter table public.runs add column if not exists cancellation_reason text;

-- Columns the current backend reads/writes. Added nullable first so the
-- legacy backfill below can run before defaults and NOT NULL are enforced.
alter table public.runs add column if not exists input jsonb;
alter table public.runs add column if not exists output jsonb;
alter table public.runs add column if not exists error jsonb;
alter table public.runs add column if not exists updated_at timestamptz;

-- Backfill new columns from legacy columns without deleting anything.
do $$
begin
  if exists (
    select 1 from information_schema.columns
    where table_schema = 'public' and table_name = 'runs' and column_name = 'user_prompt'
  ) then
    update public.runs
      set input = jsonb_build_object('content', user_prompt)
      where input is null and user_prompt is not null;
    -- The backend no longer writes user_prompt; its legacy NOT NULL must not
    -- block new inserts. The column and its data are preserved.
    alter table public.runs alter column user_prompt drop not null;
  end if;

  if exists (
    select 1 from information_schema.columns
    where table_schema = 'public' and table_name = 'runs' and column_name = 'result'
  ) then
    update public.runs
      set output = result
      where output is null and result is not null;
  end if;

  if exists (
    select 1 from information_schema.columns
    where table_schema = 'public' and table_name = 'runs' and column_name = 'error_message'
  ) then
    update public.runs
      set error = jsonb_build_object('code', 'LEGACY_ERROR', 'message', error_message)
      where error is null and error_message is not null;
  end if;
end $$;

update public.runs set input = '{}'::jsonb where input is null;
alter table public.runs alter column input set default '{}'::jsonb;
alter table public.runs alter column input set not null;

update public.runs
  set updated_at = coalesce(finished_at, started_at, created_at, now())
  where updated_at is null;
alter table public.runs alter column updated_at set default now();
alter table public.runs alter column updated_at set not null;

-- Reuses public.set_updated_at() created in migration 001.
drop trigger if exists runs_set_updated_at on public.runs;
create trigger runs_set_updated_at
before update on public.runs
for each row execute function public.set_updated_at();

-- Legacy rows may hold status values outside the new state machine, so the
-- constraint is added NOT VALID and validated only when existing data allows
-- it. New and updated rows are always checked.
do $$
begin
  alter table public.runs drop constraint if exists runs_status_check;
  alter table public.runs add constraint runs_status_check check (status in (
    'queued','starting','running','waiting','completed','partial_success','failed',
    'cancellation_requested','cancelled'
  )) not valid;
  begin
    alter table public.runs validate constraint runs_status_check;
  exception when check_violation then
    raise notice 'runs_status_check left NOT VALID: legacy status values present';
  end;
end $$;

-- Legacy baseline reconciliation: production run_events has an integer
-- progress column (0-100), while the runtime emits structured JSONB
-- progress. The integer column is renamed to progress_percent so its values
-- are preserved, then the JSONB column is created.
do $$
declare
  progress_type text;
  has_progress_percent boolean;
begin
  select data_type into progress_type
  from information_schema.columns
  where table_schema = 'public' and table_name = 'run_events' and column_name = 'progress';

  select exists (
    select 1 from information_schema.columns
    where table_schema = 'public' and table_name = 'run_events' and column_name = 'progress_percent'
  ) into has_progress_percent;

  if progress_type is not null and progress_type <> 'jsonb' then
    if not has_progress_percent then
      -- Rename preserves values and carries any existing CHECK constraint
      -- (its expression is rewritten to the new column name).
      alter table public.run_events rename column progress to progress_percent;
    else
      -- Unexpected partial state: both the legacy integer progress and
      -- progress_percent exist. Preserve every value and both columns.
      update public.run_events
        set progress_percent = progress
        where progress_percent is null and progress is not null;
      alter table public.run_events rename column progress to progress_percent_legacy;
    end if;
  end if;

  -- Retain a 0-100 guard on progress_percent even if the legacy column had
  -- no CHECK constraint. NOT VALID so pre-existing rows are never rejected.
  if exists (
    select 1 from information_schema.columns
    where table_schema = 'public' and table_name = 'run_events' and column_name = 'progress_percent'
  ) and not exists (
    select 1
    from pg_constraint c
    join pg_attribute a on a.attrelid = c.conrelid and a.attnum = any (c.conkey)
    where c.conrelid = 'public.run_events'::regclass
      and c.contype = 'c'
      and a.attname = 'progress_percent'
  ) then
    alter table public.run_events add constraint run_events_progress_percent_check
      check (progress_percent between 0 and 100) not valid;
  end if;
end $$;

-- agent_name is the legacy attribution column and is intentionally kept.
-- event_type and message already exist in the confirmed production
-- baseline (id remains its existing bigint identity); these two lines are
-- defensive no-ops there and only add the columns in environments where
-- they are missing.
alter table public.run_events add column if not exists event_type text;
alter table public.run_events add column if not exists message text;
alter table public.run_events add column if not exists agent text;
alter table public.run_events add column if not exists phase text;
alter table public.run_events add column if not exists progress jsonb;

create table if not exists public.run_checkpoints (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references public.runs(id) on delete cascade,
  engine_version text not null,
  workflow_key text not null,
  phase text not null,
  completed_tasks jsonb not null default '[]'::jsonb,
  artifacts jsonb not null default '{}'::jsonb,
  failures jsonb not null default '[]'::jsonb,
  token_usage jsonb not null default '{}'::jsonb,
  last_event jsonb,
  attempt integer not null default 1,
  created_at timestamptz not null default now()
);
create index if not exists run_checkpoints_run_created_idx on public.run_checkpoints(run_id, created_at desc);

create table if not exists public.worker_heartbeats (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references public.runs(id) on delete cascade,
  worker_id text not null,
  attempt integer not null,
  heartbeat_at timestamptz not null default now(),
  lease_expires_at timestamptz not null,
  metadata jsonb not null default '{}'::jsonb
);
create index if not exists worker_heartbeats_run_idx on public.worker_heartbeats(run_id, heartbeat_at desc);
create index if not exists runs_lease_idx on public.runs(status, lease_expires_at);

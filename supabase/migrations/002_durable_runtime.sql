-- Stage 3 durable run runtime: events, checkpoints, leases, heartbeats, cancellation.

alter table public.runs add column if not exists attempt integer not null default 1;
alter table public.runs add column if not exists started_at timestamptz;
alter table public.runs add column if not exists finished_at timestamptz;
alter table public.runs add column if not exists last_heartbeat_at timestamptz;
alter table public.runs add column if not exists worker_id text;
alter table public.runs add column if not exists lease_expires_at timestamptz;
alter table public.runs add column if not exists cancellation_requested_at timestamptz;
alter table public.runs add column if not exists cancellation_reason text;

alter table public.runs drop constraint if exists runs_status_check;
alter table public.runs add constraint runs_status_check check (status in (
  'queued','starting','running','waiting','completed','partial_success','failed',
  'cancellation_requested','cancelled'
));

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

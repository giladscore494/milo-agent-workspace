-- Stage 10 deployment hardening: idempotent invocations, least-privilege RLS docs hooks, and stuck-run observability.

alter table public.runs add column if not exists idempotency_key text;
create index if not exists runs_active_idempotency_idx
  on public.runs(conversation_id, idempotency_key)
  where status in ('queued','starting','running','waiting','cancellation_requested') and idempotency_key is not null;

create table if not exists public.run_invocations (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references public.runs(id) on delete cascade,
  launcher text not null,
  execution_name text,
  payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);
create index if not exists run_invocations_run_idx on public.run_invocations(run_id, created_at desc);
alter table public.run_invocations enable row level security;

create or replace view public.stuck_runs as
select id, conversation_id, status, worker_id, last_heartbeat_at, lease_expires_at, updated_at
from public.runs
where status in ('starting','running','waiting')
  and coalesce(lease_expires_at, updated_at) < now() - interval '10 minutes';

-- Stage 5 chat-driven workflow proposals, critiques, estimates, and approval gating.

create table if not exists public.workflow_proposals (
  id uuid primary key default gen_random_uuid(),
  status text not null check (status in ('approved','revision_required','rejected')),
  user_request text not null,
  task_spec jsonb not null default '{}'::jsonb,
  draft jsonb not null default '{}'::jsonb,
  critiques jsonb not null default '[]'::jsonb,
  estimates jsonb not null default '{}'::jsonb,
  repair_count integer not null default 0,
  compiled_at timestamptz,
  approved_at timestamptz,
  rejected_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists workflow_proposals_status_idx on public.workflow_proposals(status, created_at desc);

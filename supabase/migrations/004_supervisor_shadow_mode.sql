-- Stage 6 supervisor shadow mode: durable agent messaging, blackboard, and reviewable decisions.

create table if not exists public.agent_instances (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references public.runs(id) on delete cascade,
  agent_key text not null,
  status text not null default 'idle',
  task_key text,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists agent_instances_run_idx on public.agent_instances(run_id, agent_key);

create table if not exists public.agent_tasks (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references public.runs(id) on delete cascade,
  task_key text not null,
  assigned_agent text,
  status text not null default 'pending',
  input jsonb not null default '{}'::jsonb,
  output jsonb not null default '{}'::jsonb,
  error jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create unique index if not exists agent_tasks_run_task_key_idx on public.agent_tasks(run_id, task_key);

create table if not exists public.task_dependencies (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references public.runs(id) on delete cascade,
  task_key text not null,
  depends_on_task_key text not null,
  created_at timestamptz not null default now()
);
create index if not exists task_dependencies_run_task_idx on public.task_dependencies(run_id, task_key);

create table if not exists public.agent_messages (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references public.runs(id) on delete cascade,
  message_type text not null check (message_type in ('task_assigned','task_started','progress','finding','artifact_created','missing_information','conflict_found','request_help','request_context','propose_subtask','task_completed','task_failed','review_result','user_instruction','decision')),
  sender text not null,
  recipient text not null,
  task_key text,
  payload jsonb not null default '{}'::jsonb,
  read_at timestamptz,
  created_at timestamptz not null default now()
);
create index if not exists agent_messages_run_unread_idx on public.agent_messages(run_id, recipient, read_at, created_at);

create table if not exists public.run_blackboards (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references public.runs(id) on delete cascade,
  goal text not null,
  approved_plan jsonb not null default '{}'::jsonb,
  known_entities jsonb not null default '[]'::jsonb,
  completed_tasks jsonb not null default '[]'::jsonb,
  active_agents jsonb not null default '[]'::jsonb,
  open_questions jsonb not null default '[]'::jsonb,
  missing_fields jsonb not null default '[]'::jsonb,
  claims_conflict_summaries jsonb not null default '[]'::jsonb,
  artifacts jsonb not null default '{}'::jsonb,
  remaining_budget jsonb not null default '{}'::jsonb,
  completion_score numeric not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create unique index if not exists run_blackboards_run_idx on public.run_blackboards(run_id);
create index if not exists run_blackboards_run_updated_idx on public.run_blackboards(run_id, updated_at desc);

create table if not exists public.supervisor_decisions (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references public.runs(id) on delete cascade,
  mode text not null default 'shadow' check (mode = 'shadow'),
  input jsonb not null default '{}'::jsonb,
  assessment text not null,
  proposed_commands jsonb not null default '[]'::jsonb,
  next_wake_condition jsonb not null default '{}'::jsonb,
  rationale_summary text not null,
  evaluation_report jsonb not null default '{}'::jsonb,
  executed_at timestamptz,
  created_at timestamptz not null default now(),
  constraint supervisor_shadow_never_executes check (executed_at is null)
);
create index if not exists supervisor_decisions_run_created_idx on public.supervisor_decisions(run_id, created_at desc);

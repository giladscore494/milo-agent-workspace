-- Stage 8: Internet governance, sources, claims, and conflicts.

create table if not exists public.tool_access_requests (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references public.runs(id) on delete cascade,
  agent text not null,
  tool text not null,
  reason text not null,
  scope jsonb not null default '{}'::jsonb,
  requested_limits jsonb not null default '{}'::jsonb,
  trigger jsonb,
  status text not null default 'pending' check (status in ('pending','granted','denied','expired','revoked')),
  created_at timestamptz not null default now()
);
create index if not exists tool_access_requests_run_agent_idx on public.tool_access_requests(run_id, agent, created_at desc);

create table if not exists public.tool_grants (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references public.runs(id) on delete cascade,
  request_id uuid references public.tool_access_requests(id) on delete set null,
  agent text not null,
  tool text not null,
  max_searches integer not null check (max_searches >= 0),
  max_rounds integer not null check (max_rounds >= 0),
  domains text[],
  expires_at timestamptz not null,
  approver_policy text not null,
  revoked_at timestamptz,
  created_at timestamptz not null default now()
);
create index if not exists tool_grants_run_agent_tool_idx on public.tool_grants(run_id, agent, tool, expires_at desc);

create table if not exists public.tool_usage (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references public.runs(id) on delete cascade,
  grant_id uuid not null references public.tool_grants(id) on delete restrict,
  agent text not null,
  tool text not null,
  operation text not null,
  query text,
  url text,
  status text not null check (status in ('succeeded','failed','blocked','timed_out')),
  error jsonb,
  created_at timestamptz not null default now()
);
create index if not exists tool_usage_run_agent_tool_idx on public.tool_usage(run_id, agent, tool, created_at desc);

create table if not exists public.sources (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references public.runs(id) on delete cascade,
  agent text not null,
  url text not null,
  title text not null,
  domain text not null,
  source_type text not null,
  source_strength text not null,
  source_date text,
  retrieved_at timestamptz not null default now(),
  query text not null,
  tool_operation text not null,
  created_at timestamptz not null default now()
);
create index if not exists sources_run_domain_idx on public.sources(run_id, domain);

create table if not exists public.claims (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references public.runs(id) on delete cascade,
  entity_key text not null,
  field_key text not null,
  value jsonb not null,
  unit text,
  time_scope jsonb not null default '{}'::jsonb,
  geography text,
  market text,
  source_id uuid not null references public.sources(id) on delete restrict,
  source_strength text not null,
  confidence numeric not null check (confidence >= 0 and confidence <= 1),
  agent text not null,
  status text not null default 'active' check (status in ('active','rejected','superseded','needs_review')),
  created_at timestamptz not null default now()
);
create index if not exists claims_scope_idx on public.claims(run_id, entity_key, field_key, geography, market);

create table if not exists public.source_claim_links (
  source_id uuid not null references public.sources(id) on delete cascade,
  claim_id uuid not null references public.claims(id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (source_id, claim_id)
);

create table if not exists public.conflicts (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references public.runs(id) on delete cascade,
  entity_key text not null,
  field_key text not null,
  claim_ids uuid[] not null,
  outcome text not null default 'unresolved_needs_review' check (outcome in ('resolved_as_different_scope','resolved_value_selected','unresolved_needs_review','rejected_claim','user_decision')),
  rationale text,
  created_at timestamptz not null default now(),
  resolved_at timestamptz
);
create index if not exists conflicts_run_field_idx on public.conflicts(run_id, entity_key, field_key, outcome);

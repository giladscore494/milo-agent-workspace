-- Append-only auditable usage/cost ledger for paid model calls.
--
-- Additive and data-preserving. One row per budget decision (reserved /
-- settled / rejected). Monetary values use decimal-safe numeric columns.
-- No secrets, tokens or prompts are ever stored here.

create table if not exists public.run_usage_ledger (
  id bigint generated always as identity primary key,
  run_id uuid not null references public.runs(id) on delete cascade,
  project_id uuid,
  user_id uuid,
  provider text,
  model text,
  call_seq integer not null default 0,
  decision text not null check (decision in ('reserved','settled','rejected')),
  rejection_reason text,
  reserved_input_tokens integer not null default 0,
  reserved_output_tokens integer not null default 0,
  actual_input_tokens integer,
  actual_output_tokens integer,
  estimated_cost numeric(12, 6),
  actual_cost numeric(12, 6),
  created_at timestamptz not null default now()
);

create index if not exists run_usage_ledger_run_idx on public.run_usage_ledger(run_id, id);
create index if not exists run_usage_ledger_user_daily_idx on public.run_usage_ledger(user_id, created_at) where user_id is not null;
create index if not exists run_usage_ledger_project_daily_idx on public.run_usage_ledger(project_id, created_at) where project_id is not null;

alter table public.run_usage_ledger enable row level security;
-- No policies: browser roles have no access at all; only the trusted
-- service path reads and appends.

-- Append-only enforcement applies to every role including the service
-- path: the ledger is an audit record and can never be rewritten.
create or replace function public.forbid_usage_ledger_mutation() returns trigger
language plpgsql as $$
begin
  raise exception 'run_usage_ledger is append-only';
end;
$$;

drop trigger if exists run_usage_ledger_append_only on public.run_usage_ledger;
create trigger run_usage_ledger_append_only
  before update or delete on public.run_usage_ledger
  for each row execute function public.forbid_usage_ledger_mutation();

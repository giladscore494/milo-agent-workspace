-- Aggregate usage accounting per run (tokens, model calls, estimated cost).
-- Additive and data-preserving; legacy rows keep an empty usage object.

alter table public.runs add column if not exists usage jsonb not null default '{}'::jsonb;

create index if not exists runs_requested_by_created_at_idx
  on public.runs(requested_by, created_at)
  where requested_by is not null;

-- Stage 1 project workspace foundation. Safe to rerun; never deletes data.

create extension if not exists pgcrypto;

create table if not exists public.projects (
  id uuid primary key default gen_random_uuid(),
  slug text not null unique,
  name text not null,
  description text,
  workflow_key text not null,
  configuration jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

insert into public.projects (slug, name, description, workflow_key, configuration)
values (
  'milo-vehicle-catalog',
  'MILO Vehicle Catalog',
  'Preserved MILO vehicle catalog workflow for Hyundai Israel coverage from 2010 through June 2026.',
  'vehicle_catalog_v1',
  '{"manufacturer":"Hyundai","market":"Israel","period":{"from":"2010","to":"June 2026"}}'::jsonb
)
on conflict (slug) do update set
  name = excluded.name,
  description = excluded.description,
  workflow_key = excluded.workflow_key,
  configuration = excluded.configuration,
  updated_at = now();

alter table public.conversations add column if not exists project_id uuid;

update public.conversations
set project_id = (select id from public.projects where slug = 'milo-vehicle-catalog')
where project_id is null;

alter table public.conversations
  drop constraint if exists conversations_project_id_fkey;

alter table public.conversations
  add constraint conversations_project_id_fkey
  foreign key (project_id) references public.projects(id);

create index if not exists conversations_project_id_idx on public.conversations(project_id);

alter table public.conversations alter column project_id set not null;

alter table public.projects enable row level security;

create or replace function public.set_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists projects_set_updated_at on public.projects;
create trigger projects_set_updated_at
before update on public.projects
for each row execute function public.set_updated_at();

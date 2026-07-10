-- Auth/UI hardening stage: project membership model for browser-facing access.
-- Safe deployment/backfill strategy:
-- 1. Apply this migration in a controlled Supabase migration deployment.
-- 2. Existing projects are intentionally left without members until an operator maps
--    each project to real Supabase auth.users IDs.
-- 3. Backfill with real users only, for example:
--      insert into public.project_members (project_id, user_id, role)
--      select '<project_uuid>'::uuid, '<real_auth_user_uuid>'::uuid, 'owner'
--      where exists (select 1 from auth.users where id = '<real_auth_user_uuid>'::uuid)
--      on conflict (project_id, user_id) do update set role = excluded.role;
--    Do not fabricate user IDs.

create table if not exists public.project_members (
  project_id uuid not null references public.projects(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  role text not null default 'member',
  created_at timestamptz not null default now(),
  primary key (project_id, user_id),
  constraint project_members_role_check check (role in ('owner', 'admin', 'member', 'viewer'))
);

create index if not exists project_members_user_id_idx on public.project_members(user_id);
create index if not exists project_members_project_id_idx on public.project_members(project_id);

alter table public.projects enable row level security;
alter table public.conversations enable row level security;
alter table public.messages enable row level security;
alter table public.runs enable row level security;
alter table public.run_events enable row level security;
alter table public.project_members enable row level security;

do $$
begin
  if not exists (select 1 from pg_policies where schemaname = 'public' and tablename = 'project_members' and policyname = 'members can read their memberships') then
    create policy "members can read their memberships" on public.project_members for select to authenticated using (user_id = auth.uid());
  end if;

  if not exists (select 1 from pg_policies where schemaname = 'public' and tablename = 'projects' and policyname = 'members can read projects') then
    create policy "members can read projects" on public.projects for select to authenticated using (exists (select 1 from public.project_members pm where pm.project_id = projects.id and pm.user_id = auth.uid()));
  end if;

  if not exists (select 1 from pg_policies where schemaname = 'public' and tablename = 'conversations' and policyname = 'members can read conversations') then
    create policy "members can read conversations" on public.conversations for select to authenticated using (exists (select 1 from public.project_members pm where pm.project_id = conversations.project_id and pm.user_id = auth.uid()));
  end if;

  if not exists (select 1 from pg_policies where schemaname = 'public' and tablename = 'conversations' and policyname = 'members can create conversations') then
    create policy "members can create conversations" on public.conversations for insert to authenticated with check (exists (select 1 from public.project_members pm where pm.project_id = conversations.project_id and pm.user_id = auth.uid()));
  end if;

  if not exists (select 1 from pg_policies where schemaname = 'public' and tablename = 'messages' and policyname = 'members can read messages') then
    create policy "members can read messages" on public.messages for select to authenticated using (exists (select 1 from public.conversations c join public.project_members pm on pm.project_id = c.project_id where c.id = messages.conversation_id and pm.user_id = auth.uid()));
  end if;

  if not exists (select 1 from pg_policies where schemaname = 'public' and tablename = 'runs' and policyname = 'members can read runs') then
    create policy "members can read runs" on public.runs for select to authenticated using (exists (select 1 from public.conversations c join public.project_members pm on pm.project_id = c.project_id where c.id = runs.conversation_id and pm.user_id = auth.uid()));
  end if;

  if not exists (select 1 from pg_policies where schemaname = 'public' and tablename = 'run_events' and policyname = 'members can read run events') then
    create policy "members can read run events" on public.run_events for select to authenticated using (exists (select 1 from public.runs r join public.conversations c on c.id = r.conversation_id join public.project_members pm on pm.project_id = c.project_id where r.id = run_events.run_id and pm.user_id = auth.uid()));
  end if;
end $$;

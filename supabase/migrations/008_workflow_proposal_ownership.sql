-- Workflow proposal ownership: created_by user and project scoping.
--
-- Additive, idempotent, data-preserving. Existing proposal rows are kept
-- intact and are intentionally NOT auto-assigned to any user or project:
-- legacy rows keep NULL ownership until an operator backfills them with
-- real identifiers. Browser-facing access requires non-NULL ownership plus
-- project membership, so unowned legacy proposals stay invisible to
-- authenticated users (service_role retains maintenance access).
--
-- Operator-controlled backfill template (real IDs only, never fabricated):
--   update public.workflow_proposals p
--      set created_by = '<real_auth_user_uuid>'::uuid,
--          project_id = '<real_project_uuid>'::uuid,
--          updated_at = now()
--    where p.id = '<real_proposal_uuid>'::uuid
--      and p.created_by is null
--      and exists (select 1 from auth.users u where u.id = '<real_auth_user_uuid>'::uuid)
--      and exists (select 1 from public.projects pr where pr.id = '<real_project_uuid>'::uuid);
-- Do not fabricate user IDs. See scripts/release/generate-proposal-backfill.sh.

alter table public.workflow_proposals
  add column if not exists created_by uuid references auth.users(id) on delete set null;

alter table public.workflow_proposals
  add column if not exists project_id uuid references public.projects(id) on delete set null;

create index if not exists workflow_proposals_created_by_idx
  on public.workflow_proposals(created_by, created_at desc);

create index if not exists workflow_proposals_project_id_idx
  on public.workflow_proposals(project_id, created_at desc);

alter table public.workflow_proposals enable row level security;

do $$
begin
  if not exists (select 1 from pg_policies where schemaname = 'public' and tablename = 'workflow_proposals' and policyname = 'project members can read proposals') then
    create policy "project members can read proposals" on public.workflow_proposals
      for select to authenticated
      using (
        project_id is not null
        and exists (
          select 1 from public.project_members pm
          where pm.project_id = workflow_proposals.project_id
            and pm.user_id = auth.uid()
        )
      );
  end if;

  if not exists (select 1 from pg_policies where schemaname = 'public' and tablename = 'workflow_proposals' and policyname = 'project members can create own proposals') then
    create policy "project members can create own proposals" on public.workflow_proposals
      for insert to authenticated
      with check (
        created_by = auth.uid()
        and project_id is not null
        and exists (
          select 1 from public.project_members pm
          where pm.project_id = workflow_proposals.project_id
            and pm.user_id = auth.uid()
        )
      );
  end if;

  if not exists (select 1 from pg_policies where schemaname = 'public' and tablename = 'workflow_proposals' and policyname = 'project members can update proposals') then
    create policy "project members can update proposals" on public.workflow_proposals
      for update to authenticated
      using (
        project_id is not null
        and exists (
          select 1 from public.project_members pm
          where pm.project_id = workflow_proposals.project_id
            and pm.user_id = auth.uid()
        )
      )
      with check (
        project_id is not null
        and exists (
          select 1 from public.project_members pm
          where pm.project_id = workflow_proposals.project_id
            and pm.user_id = auth.uid()
        )
      );
  end if;
end $$;

-- Least-privilege grants for the browser-facing authenticated role; RLS
-- policies above still constrain every row. No delete grant is issued.
grant select, insert, update on public.workflow_proposals to authenticated;

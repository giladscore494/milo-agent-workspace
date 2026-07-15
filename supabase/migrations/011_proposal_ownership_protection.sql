-- Proposal ownership hardening: protected ownership columns and atomic
-- project-from-proposal creation.
--
-- Additive, idempotent, data-preserving. Legacy proposals keep NULL
-- ownership until explicit operator backfill (template in migration 008).

-- 1) Browser users must never modify ownership fields. Replace the blanket
--    UPDATE grant from migration 008 with column-level grants that exclude
--    created_by and project_id (and the compiled timestamps the backend
--    owns). RLS policies from 008 still row-scope every statement.
revoke update on public.workflow_proposals from authenticated;
grant update (status, user_request, task_spec, draft, critiques, estimates,
              repair_count, approved_at, rejected_at, updated_at)
  on public.workflow_proposals to authenticated;

-- 2) Atomic project creation from an approved proposal: the project row and
--    the initial owner membership commit together, so no orphan project can
--    remain if the membership insert fails. Service-path only (the backend
--    calls it with the already-authorized creator); no execute grant is
--    issued to authenticated.
create or replace function public.create_project_from_proposal_with_owner(
  p_proposal_id uuid,
  p_slug text,
  p_name text,
  p_description text,
  p_configuration jsonb,
  p_owner uuid
) returns public.projects
language plpgsql
as $$
declare
  v_project public.projects;
begin
  insert into public.projects (slug, name, description, workflow_key, configuration)
  values (
    p_slug,
    p_name,
    p_description,
    'chat_architect_v1',
    coalesce(p_configuration, '{}'::jsonb) || jsonb_build_object('proposal_id', p_proposal_id::text)
  )
  returning * into v_project;

  if p_owner is not null then
    -- FK to auth.users validates the owner; any failure aborts the whole
    -- transaction including the project insert above.
    insert into public.project_members (project_id, user_id, role)
    values (v_project.id, p_owner, 'owner');
  end if;

  return v_project;
end;
$$;

revoke execute on function public.create_project_from_proposal_with_owner(uuid, text, text, text, jsonb, uuid) from public;
revoke execute on function public.create_project_from_proposal_with_owner(uuid, text, text, text, jsonb, uuid) from authenticated;

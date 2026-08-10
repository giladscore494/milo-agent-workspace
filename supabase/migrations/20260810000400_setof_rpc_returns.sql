-- PostgREST client compatibility: SETOF wrappers for historical RPCs.
--
-- The pinned supabase-py client (postgrest-py) parses RPC responses through
-- an APIResponse model whose data field is a LIST; a function returning a
-- single composite row (or a bare jsonb object) produces a JSON object
-- body, fails that validation, and surfaces as an opaque APIError AFTER the
-- database write has already committed. This broke the first live staging
-- worker run and would equally break the API's create_message_and_run
-- path. Plain-PostgreSQL tests and the in-memory repository cannot see the
-- problem because they bypass PostgREST.
--
-- Historical migrations (011/012/014/015) must stay rerun-safe, so their
-- functions keep their original names and return types. This migration adds
-- additive `_v2` wrappers returning SETOF (an array over PostgREST) and the
-- repository now calls only the `_v2` names. The originals remain for SQL
-- callers and remain service-path-only. Lease-guarded functions from
-- 20260810000300 already return SETOF and need no wrapper.

create or replace function public.create_message_and_run_v2(
  p_conversation_id uuid,
  p_content text,
  p_metadata jsonb,
  p_requested_by uuid,
  p_idempotency_key text,
  p_request_fingerprint text,
  p_max_user_active integer default null,
  p_max_project_active integer default null
) returns setof jsonb
language plpgsql
as $$
begin
  return next public.create_message_and_run(
    p_conversation_id, p_content, p_metadata, p_requested_by,
    p_idempotency_key, p_request_fingerprint, p_max_user_active, p_max_project_active
  );
  return;
end;
$$;

create or replace function public.create_project_from_proposal_with_owner_v2(
  p_proposal_id uuid,
  p_slug text,
  p_name text,
  p_description text,
  p_configuration jsonb,
  p_owner uuid
) returns setof public.projects
language plpgsql
as $$
begin
  return next public.create_project_from_proposal_with_owner(
    p_proposal_id, p_slug, p_name, p_description, p_configuration, p_owner
  );
  return;
end;
$$;

create or replace function public.reserve_model_call_budget_v2(
  p_run_id uuid,
  p_call_seq integer,
  p_user_id uuid,
  p_project_id uuid,
  p_estimated_cost numeric,
  p_daily_user_limit numeric,
  p_daily_project_limit numeric,
  p_provider text default 'moonshot',
  p_model text default 'kimi'
) returns setof public.model_call_budget_reservations
language plpgsql
as $$
begin
  return next public.reserve_model_call_budget(
    p_run_id, p_call_seq, p_user_id, p_project_id, p_estimated_cost,
    p_daily_user_limit, p_daily_project_limit, p_provider, p_model
  );
  return;
end;
$$;

create or replace function public.settle_model_call_budget_v2(
  p_reservation_id uuid,
  p_actual_cost numeric,
  p_status text default 'settled',
  p_rejection_reason text default null
) returns setof public.model_call_budget_reservations
language plpgsql
as $$
begin
  return next public.settle_model_call_budget(p_reservation_id, p_actual_cost, p_status, p_rejection_reason);
  return;
end;
$$;

-- Service-path-only ACLs for the new wrappers.
do $$
declare
  fn text;
begin
  foreach fn in array array[
    'public.create_message_and_run_v2(uuid, text, jsonb, uuid, text, text, integer, integer)',
    'public.create_project_from_proposal_with_owner_v2(uuid, text, text, text, jsonb, uuid)',
    'public.reserve_model_call_budget_v2(uuid, integer, uuid, uuid, numeric, numeric, numeric, text, text)',
    'public.settle_model_call_budget_v2(uuid, numeric, text, text)'
  ]
  loop
    execute format('revoke execute on function %s from public', fn);
    if exists (select 1 from pg_roles where rolname = 'anon') then
      execute format('revoke execute on function %s from anon', fn);
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
      execute format('revoke execute on function %s from authenticated', fn);
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
      execute format('grant execute on function %s to service_role', fn);
    end if;
  end loop;
end $$;

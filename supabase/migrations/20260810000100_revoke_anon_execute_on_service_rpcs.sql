-- Revoke anonymous EXECUTE on every service-only RPC.
--
-- Supabase grants EXECUTE on public-schema functions to anon, authenticated
-- and service_role through default privileges. Earlier migrations revoke
-- EXECUTE from public and authenticated but never from anon, so on a real
-- Supabase project every service-path RPC — including the SECURITY DEFINER
-- budget functions from migrations 014/015 — remained callable by the anon
-- role via PostgREST. Plain PostgreSQL test environments may not define the
-- anon role, so each revoke is guarded and becomes a no-op there.
--
-- Additive, idempotent, data-preserving: privileges only, no schema or data
-- changes, and no grant that did not already exist is created.

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    revoke execute on function public.create_project_from_proposal_with_owner(uuid, text, text, text, jsonb, uuid) from anon;
    revoke execute on function public.create_message_and_run(uuid, text, jsonb, uuid, text, text, integer, integer) from anon;
    revoke execute on function public.claim_run_lease(uuid, text, integer) from anon;
    revoke execute on function public.reserve_daily_user_budget(uuid, uuid, numeric, numeric, text, text) from anon;
    revoke execute on function public.reserve_daily_project_budget(uuid, uuid, numeric, numeric, text, text) from anon;
    revoke execute on function public.model_call_budget_committed(uuid, uuid, date) from anon;
    revoke execute on function public.reserve_model_call_budget(uuid, integer, uuid, uuid, numeric, numeric, numeric, text, text) from anon;
    revoke execute on function public.settle_model_call_budget(uuid, numeric, text, text) from anon;
  end if;

  -- Re-assert the earlier authenticated/public revocations so this migration
  -- alone leaves every service-only RPC browser-inaccessible even if a prior
  -- grant was reintroduced out of band.
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    revoke execute on function public.create_project_from_proposal_with_owner(uuid, text, text, text, jsonb, uuid) from authenticated;
    revoke execute on function public.create_message_and_run(uuid, text, jsonb, uuid, text, text, integer, integer) from authenticated;
    revoke execute on function public.claim_run_lease(uuid, text, integer) from authenticated;
    revoke execute on function public.reserve_daily_user_budget(uuid, uuid, numeric, numeric, text, text) from authenticated;
    revoke execute on function public.reserve_daily_project_budget(uuid, uuid, numeric, numeric, text, text) from authenticated;
    revoke execute on function public.model_call_budget_committed(uuid, uuid, date) from authenticated;
    revoke execute on function public.reserve_model_call_budget(uuid, integer, uuid, uuid, numeric, numeric, numeric, text, text) from authenticated;
    revoke execute on function public.settle_model_call_budget(uuid, numeric, text, text) from authenticated;
  end if;

  revoke execute on function public.create_project_from_proposal_with_owner(uuid, text, text, text, jsonb, uuid) from public;
  revoke execute on function public.create_message_and_run(uuid, text, jsonb, uuid, text, text, integer, integer) from public;
  revoke execute on function public.claim_run_lease(uuid, text, integer) from public;
  revoke execute on function public.reserve_daily_user_budget(uuid, uuid, numeric, numeric, text, text) from public;
  revoke execute on function public.reserve_daily_project_budget(uuid, uuid, numeric, numeric, text, text) from public;
  revoke execute on function public.model_call_budget_committed(uuid, uuid, date) from public;
  revoke execute on function public.reserve_model_call_budget(uuid, integer, uuid, uuid, numeric, numeric, numeric, text, text) from public;
  revoke execute on function public.settle_model_call_budget(uuid, numeric, text, text) from public;
end $$;

-- Secure-by-default for FUTURE functions: remove the schema-level default
-- EXECUTE grants for anon and authenticated, so a new function no longer
-- receives direct browser-role EXECUTE automatically. Note the built-in
-- PostgreSQL default (EXECUTE to PUBLIC) is global and cannot be removed by
-- a per-schema statement, so every new service function must still follow
-- the repository convention of `revoke execute ... from public` — enforced
-- by tests/test_migrations_postgres.py. After this migration, that existing
-- convention is sufficient: no residual direct anon grant survives it.
-- service_role default EXECUTE is intentionally left in place.
do $$
begin
  execute 'alter default privileges for role postgres in schema public revoke execute on functions from public';
  if exists (select 1 from pg_roles where rolname = 'anon') then
    execute 'alter default privileges for role postgres in schema public revoke execute on functions from anon';
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    execute 'alter default privileges for role postgres in schema public revoke execute on functions from authenticated';
  end if;
end $$;

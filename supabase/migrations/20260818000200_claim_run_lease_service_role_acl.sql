-- Stage C corrective (Attempt 5): service_role EXECUTE on claim_run_lease.
--
-- Migration 012 created public.claim_run_lease(uuid, text, integer) and
-- revoked EXECUTE from public/authenticated (20260810000100 later revoked
-- anon), but no migration ever granted EXECUTE to service_role. In the
-- production project — which does not extend Supabase's default function
-- privileges to service_role — the worker's very first durable operation
-- (repo.claim_run -> claim_run_lease) therefore failed with SQLSTATE 42501
-- before any provider call. Every other worker-path RPC already carries an
-- explicit grant (see tests/test_worker_rpc_acl_postgres.py, which derives
-- the full worker RPC closure and proves this was the only gap).
--
-- Additive, idempotent, rerun-safe, data-preserving.

begin;

do $migration$
declare
  fn text := 'public.claim_run_lease(uuid, text, integer)';
begin
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
end
$migration$;

commit;

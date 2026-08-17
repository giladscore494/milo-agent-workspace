-- Stage C corrective: service_role-only EXECUTE on base RPC dependencies.
-- The SETOF wrappers are SECURITY INVOKER, so service_role must also have
-- EXECUTE on the underlying base functions they call.

begin;

do $migration$
declare
  fn text;
begin
  foreach fn in array array[
    'public.create_message_and_run(uuid, text, jsonb, uuid, text, text, integer, integer)',
    'public.create_project_from_proposal_with_owner(uuid, text, text, text, jsonb, uuid)'
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
end
$migration$;

commit;

-- Persist the backend service_role privileges required by the private API and worker.
-- RLS remains enabled for browser-facing roles; service_role is used only by trusted backend runtimes.
-- Plain PostgreSQL test environments do not define Supabase's service_role, so this migration
-- intentionally becomes a no-op there while applying the grants in Supabase.

begin;

do $migration$
begin
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        execute 'grant usage on schema public to service_role';
        execute 'grant select, insert, update, delete on all tables in schema public to service_role';
        execute 'grant usage, select, update on all sequences in schema public to service_role';
        execute 'alter default privileges for role postgres in schema public grant select, insert, update, delete on tables to service_role';
        execute 'alter default privileges for role postgres in schema public grant usage, select, update on sequences to service_role';
    end if;
end
$migration$;

commit;

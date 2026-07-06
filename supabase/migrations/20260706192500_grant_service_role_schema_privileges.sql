-- Persist the backend service_role privileges required by the private API and worker.
-- RLS remains enabled for browser-facing roles; service_role is used only by trusted backend runtimes.

begin;

grant usage on schema public to service_role;

grant select, insert, update, delete
on all tables in schema public
 to service_role;

grant usage, select, update
on all sequences in schema public
 to service_role;

alter default privileges for role postgres in schema public
grant select, insert, update, delete on tables to service_role;

alter default privileges for role postgres in schema public
grant usage, select, update on sequences to service_role;

commit;

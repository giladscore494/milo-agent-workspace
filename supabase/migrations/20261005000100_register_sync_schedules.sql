-- PR-SYNC-2: the register sync's schedule -- ONE row per project.
--
-- The owner's switch on the Register page (who turned it on, the project and
-- the conversation every scheduled sync is recorded under) and the state the
-- hourly tick keeps (`backend/catalog/register/autosync.py`): why it paused
-- and when, the consecutive throttled and failed syncs, the newest finished
-- sync already counted, when the owner last resumed it, and the last tick.
-- The sync itself (PR-SYNC-1) is unchanged; the tick only decides WHEN to
-- start one through the API's existing single-flight `request_sync`.
--
-- Service-path only: RLS on with no policies, every privilege revoked from
-- public / anon / authenticated, and the service role may select, insert and
-- update (never delete or truncate). No other table, function or grant
-- changes. Additive, forward-only and rerun-safe.

create table if not exists public.register_sync_schedules (
  project_id uuid primary key references public.projects(id) on delete cascade,
  enabled boolean not null default false,
  enabled_by uuid,
  conversation_id uuid references public.conversations(id) on delete cascade,
  enabled_at timestamptz,
  paused_reason text,
  paused_at timestamptz,
  resumed_at timestamptz,
  consecutive_throttles integer not null default 0,
  consecutive_failures integer not null default 0,
  counted_run_id uuid,
  last_tick jsonb,
  updated_at timestamptz not null default now(),
  constraint register_sync_schedules_switch_complete
    check (not enabled or (enabled_by is not null and conversation_id is not null)),
  constraint register_sync_schedules_paused_reason
    check (paused_reason is null or paused_reason in ('SYNC_PAUSED_CAPACITY', 'SYNC_PAUSED_FAILING')),
  constraint register_sync_schedules_counters
    check (consecutive_throttles >= 0 and consecutive_failures >= 0),
  constraint register_sync_schedules_last_tick
    check (last_tick is null or jsonb_typeof(last_tick) = 'object')
);

alter table public.register_sync_schedules enable row level security;

do $$
begin
  revoke all on table public.register_sync_schedules from public;
  if exists (select 1 from pg_roles where rolname = 'anon') then
    revoke all on table public.register_sync_schedules from anon;
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    revoke all on table public.register_sync_schedules from authenticated;
  end if;
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant select, insert, update on table public.register_sync_schedules to service_role;
    revoke delete, truncate on table public.register_sync_schedules from service_role;
  end if;
end $$;

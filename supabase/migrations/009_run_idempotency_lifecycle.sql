-- Run idempotency + explicit launch lifecycle.
--
-- Additive, idempotent, data-preserving. Legacy runs keep NULL
-- requested_by / idempotency metadata and a 'none' launch_state; nothing is
-- rewritten or deleted.

alter table public.runs add column if not exists requested_by uuid references auth.users(id) on delete set null;
alter table public.runs add column if not exists request_fingerprint text;
alter table public.runs add column if not exists launch_state text not null default 'none';
alter table public.runs add column if not exists launched_at timestamptz;
alter table public.runs add column if not exists launch_error jsonb;

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'runs_launch_state_check') then
    alter table public.runs add constraint runs_launch_state_check check (launch_state in (
      'none','pending','launching','launched','launch_failed'
    ));
  end if;
end $$;

-- Idempotency scope: (conversation, requesting user, client idempotency key)
-- across ALL run statuses, so a replayed request keeps returning the
-- original run even after it finishes. Legacy rows (NULL key or user) are
-- unaffected by the partial unique index.
create unique index if not exists runs_user_conversation_idempotency_uidx
  on public.runs(conversation_id, requested_by, idempotency_key)
  where idempotency_key is not null and requested_by is not null;

create index if not exists runs_requested_by_idx on public.runs(requested_by, created_at desc);

-- Expand the supported run lifecycle with launching / timed_out /
-- budget_exhausted. Same defensive NOT VALID pattern as migration 002 so
-- hypothetical legacy status values never fail the migration; new and
-- updated rows are always checked.
do $$
begin
  alter table public.runs drop constraint if exists runs_status_check;
  alter table public.runs add constraint runs_status_check check (status in (
    'queued','launching','starting','running','waiting','completed','partial_success','failed',
    'cancellation_requested','cancelled','timed_out','budget_exhausted'
  )) not valid;
  begin
    alter table public.runs validate constraint runs_status_check;
  exception when check_violation then
    raise notice 'runs_status_check left NOT VALID: legacy status values present';
  end;
end $$;

-- PR-R: reasoning-aware usage accounting (MILO_V2_REASONING_BUDGET_PR_SPEC.md 4.5).
--
-- A reasoning model spends part of its output on thinking. The usage record
-- used to carry one output number, so a truncated answer and a long answer
-- were indistinguishable, cached input was priced as uncached and cache
-- writes were not visible at all. This migration is ADDITIVE and
-- data-preserving:
--
-- 1) `run_usage_ledger` gains six NULLABLE per-call columns. NULL means the
--    provider did not report the value -- never zero. Nothing here stores
--    reasoning TEXT: `reasoning_content` is never read, persisted or
--    re-prompted; only token counts are recorded.
--
--      cached_input_tokens        prompt_tokens_details.cached_tokens
--      cache_write_tokens         prompt_tokens_details.cache_write_tokens
--      reasoning_tokens           completion_tokens_details.reasoning_tokens
--      reasoning_tokens_estimated completion_tokens - answer_tokens, ONLY when
--                                 reasoning_tokens was not reported
--      reasoning_estimated        true when the reasoning share is estimated
--      answer_tokens              tokens of message.content only
--
-- 2) `append_usage_ledger_guarded` writes them. Same signature, same lease
--    fence, same ACL (create or replace keeps the grants; they are
--    re-asserted below per the repository convention).
--
-- 3) `execution_usage_public_projection` is widened by the run-level sums of
--    the same breakdown -- the closed browser contract
--    (backend.schemas.RunUsage / frontend/lib/runUsage.ts), widened as one
--    decision with backend.execution_usage.PUBLIC_USAGE_FIELDS.
--
-- The cumulative ledger (`run_execution_usage.ledger`) needs no change: its
-- merge and monotonic trigger are defined over every numeric key a record
-- carries, so the new counters merge and are protected exactly like the old.

-- Fail fast rather than queue behind a long transaction holding a lock on the
-- ledger (every settled model call appends to it): a blocked ALTER would in
-- turn block every append queued behind it.
set local lock_timeout = '5s';

alter table public.run_usage_ledger
  add column if not exists cached_input_tokens integer,
  add column if not exists cache_write_tokens integer,
  add column if not exists reasoning_tokens integer,
  add column if not exists reasoning_tokens_estimated integer,
  add column if not exists reasoning_estimated boolean,
  add column if not exists answer_tokens integer;

do $$
begin
  if not exists (select 1 from pg_constraint
                  where conname = 'run_usage_ledger_reasoning_counts_nonnegative') then
    alter table public.run_usage_ledger
      add constraint run_usage_ledger_reasoning_counts_nonnegative check (
        coalesce(cached_input_tokens, 0) >= 0
        and coalesce(cache_write_tokens, 0) >= 0
        and coalesce(reasoning_tokens, 0) >= 0
        and coalesce(reasoning_tokens_estimated, 0) >= 0
        and coalesce(answer_tokens, 0) >= 0) not valid;
  end if;
end $$;

-- Added NOT VALID (no table scan under the ACCESS EXCLUSIVE lock), then
-- validated separately under a SHARE UPDATE EXCLUSIVE lock that does not
-- block appends. Existing rows carry only NULLs in the new columns, so the
-- validation cannot fail; re-running it on an already valid constraint is a
-- no-op.
alter table public.run_usage_ledger
  validate constraint run_usage_ledger_reasoning_counts_nonnegative;

create or replace function public.append_usage_ledger_guarded(
  p_run_id uuid, p_worker_id text, p_attempt integer, p_lease_token text,
  p_entry jsonb
) returns setof public.run_usage_ledger
language plpgsql
set search_path = pg_catalog
as $$
declare v_row public.run_usage_ledger;
begin
  perform public.assert_worker_lease(p_run_id, p_worker_id, p_attempt, p_lease_token);
  -- The run id comes from the FENCED argument, never from the payload: an
  -- entry naming another run would otherwise charge a run this lease does not
  -- own.
  insert into public.run_usage_ledger
    (run_id, project_id, user_id, provider, model, call_seq, decision, rejection_reason,
     reserved_input_tokens, reserved_output_tokens, actual_input_tokens, actual_output_tokens,
     estimated_cost, actual_cost,
     cached_input_tokens, cache_write_tokens, reasoning_tokens,
     reasoning_tokens_estimated, reasoning_estimated, answer_tokens)
  values (p_run_id,
          nullif(p_entry->>'project_id', '')::uuid,
          nullif(p_entry->>'user_id', '')::uuid,
          coalesce(nullif(p_entry->>'provider', ''), 'moonshot'),
          coalesce(nullif(p_entry->>'model', ''), 'kimi'),
          -- The NOT NULL DEFAULT columns keep their defaults when the entry
          -- does not state them (see 20260921000200).
          coalesce(nullif(p_entry->>'call_seq', '')::integer, 0),
          nullif(p_entry->>'decision', ''),
          nullif(p_entry->>'rejection_reason', ''),
          coalesce(nullif(p_entry->>'reserved_input_tokens', '')::integer, 0),
          coalesce(nullif(p_entry->>'reserved_output_tokens', '')::integer, 0),
          nullif(p_entry->>'actual_input_tokens', '')::integer,
          nullif(p_entry->>'actual_output_tokens', '')::integer,
          nullif(p_entry->>'estimated_cost', '')::numeric,
          nullif(p_entry->>'actual_cost', '')::numeric,
          -- PR-R: absent stays NULL. A JSON null and a missing key are the
          -- same fact: the provider did not report it.
          nullif(p_entry->>'cached_input_tokens', '')::integer,
          nullif(p_entry->>'cache_write_tokens', '')::integer,
          nullif(p_entry->>'reasoning_tokens', '')::integer,
          nullif(p_entry->>'reasoning_tokens_estimated', '')::integer,
          nullif(p_entry->>'reasoning_estimated', '')::boolean,
          nullif(p_entry->>'answer_tokens', '')::integer)
  returning * into v_row;
  return next v_row;
  return;
end;
$$;

create or replace function public.execution_usage_public_projection(p_ledger jsonb)
returns jsonb
language sql
immutable
as $$
  select coalesce(
    (select jsonb_object_agg(key, value)
       from jsonb_each(coalesce(p_ledger, '{}'::jsonb))
      where key in ('model_calls', 'input_tokens', 'output_tokens', 'total_tokens',
                    'estimated_cost', 'actual_cost', 'retries',
                    'provider_backpressure_events', 'agent_steps', 'elapsed_seconds',
                    -- PR-R reasoning-aware breakdown: counts only.
                    'cached_input_tokens', 'cache_write_tokens', 'reasoning_tokens',
                    'reasoning_tokens_estimated', 'reasoning_estimated_calls',
                    'answer_tokens')),
    '{}'::jsonb);
$$;

-- Service-path-only ACLs, per the repository convention (re-asserted).
do $$
declare fn text;
begin
  foreach fn in array array[
    'public.append_usage_ledger_guarded(uuid, text, integer, text, jsonb)'
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

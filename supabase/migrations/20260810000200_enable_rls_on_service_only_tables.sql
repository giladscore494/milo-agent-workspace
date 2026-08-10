-- Explicit row level security on every service-path-only table.
--
-- The repository security model exposes exactly eight tables to the browser
-- (projects, project_members, conversations, messages, runs, run_events,
-- workflow_proposals via migrations 007/008, run_invocations via 006) and
-- treats every other table as service-path only. Migrations 002/004/005/015
-- created service-only tables without an explicit `enable row level
-- security`, which was masked in environments that install an external
-- `ensure_rls` event trigger (a platform guardrail, not part of this
-- repository). A production security invariant must not depend on an
-- environment-specific trigger, so RLS is enabled here explicitly.
--
-- No policies are added: RLS with zero policies denies browser roles
-- entirely, exactly like migration 013's run_usage_ledger. The trusted
-- backend uses service_role, which bypasses RLS, so the service path is
-- unaffected. Additive, idempotent, data-preserving.

-- migration 002 tables
alter table public.run_checkpoints enable row level security;
alter table public.worker_heartbeats enable row level security;

-- migration 004 tables
alter table public.agent_instances enable row level security;
alter table public.agent_tasks enable row level security;
alter table public.task_dependencies enable row level security;
alter table public.agent_messages enable row level security;
alter table public.run_blackboards enable row level security;
alter table public.supervisor_decisions enable row level security;

-- migration 005 tables
alter table public.tool_access_requests enable row level security;
alter table public.tool_grants enable row level security;
alter table public.tool_usage enable row level security;
alter table public.sources enable row level security;
alter table public.claims enable row level security;
alter table public.source_claim_links enable row level security;
alter table public.conflicts enable row level security;

-- migration 015 table
alter table public.model_call_budget_reservations enable row level security;

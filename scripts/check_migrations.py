"""Offline static migration check.

Validates that the migration files still contain the safety and
legacy-baseline reconciliation clauses required for the production Supabase
project (four pre-existing tables, empty migration history). This is text
matching only; executable validation lives in tests/test_migrations_postgres.py.
"""

from pathlib import Path

MIGRATIONS_DIR = Path("supabase/migrations")

REQUIRED_GLOBAL = [
    "enable row level security",
    "on conflict",
    "run_invocations",
]

# Reconciliation clauses that must never disappear before the first
# production apply. Keyed by migration filename.
REQUIRED_PER_FILE = {
    "001_project_workspace.sql": [
        # messages.sender_role -> messages.role reconciliation
        "sender_role",
        "rename column sender_role to role",
        "update public.messages set role = sender_role where role is null",
    ],
    "002_durable_runtime.sql": [
        # runs input/output/error/updated_at reconciliation
        "add column if not exists input jsonb",
        "add column if not exists output jsonb",
        "add column if not exists error jsonb",
        "add column if not exists updated_at timestamptz",
        "jsonb_build_object('content', user_prompt)",
        "set output = result",
        "error_message",
        "alter column user_prompt drop not null",
        "alter column input set not null",
        "alter column updated_at set not null",
        "runs_set_updated_at",
        # runs status check must not fail on legacy rows
        "not valid",
        # run_events integer progress preservation
        "rename column progress to progress_percent",
        "add column if not exists progress jsonb",
        "add column if not exists agent text",
        "add column if not exists phase text",
        "add column if not exists event_type text",
    ],
    "006_deployment_hardening.sql": [
        # stuck_runs depends on runs.updated_at added by 002
        "updated_at",
        "stuck_runs",
    ],
    "009_run_idempotency_lifecycle.sql": [
        # additive idempotency/lifecycle columns; status check stays defensive
        "add column if not exists requested_by",
        "add column if not exists launch_state",
        "create unique index if not exists runs_user_conversation_idempotency_uidx",
        "not valid",
    ],
    "20260810000100_revoke_anon_execute_on_service_rpcs.sql": [
        # anon must never regain EXECUTE on service-only RPCs
        "from anon",
        "revoke execute on function public.reserve_model_call_budget",
        "revoke execute on function public.settle_model_call_budget",
        "revoke execute on function public.model_call_budget_committed",
        "revoke execute on function public.reserve_daily_user_budget",
        "revoke execute on function public.reserve_daily_project_budget",
        "revoke execute on function public.create_message_and_run",
        "revoke execute on function public.claim_run_lease",
        "revoke execute on function public.create_project_from_proposal_with_owner",
        "alter default privileges for role postgres in schema public revoke execute on functions",
    ],
    "20260810000200_enable_rls_on_service_only_tables.sql": [
        # explicit RLS must not depend on an environment ensure_rls trigger
        "run_checkpoints enable row level security",
        "worker_heartbeats enable row level security",
        "agent_instances enable row level security",
        "agent_tasks enable row level security",
        "task_dependencies enable row level security",
        "agent_messages enable row level security",
        "run_blackboards enable row level security",
        "supervisor_decisions enable row level security",
        "tool_access_requests enable row level security",
        "tool_grants enable row level security",
        "tool_usage enable row level security",
        "sources enable row level security",
        "claims enable row level security",
        "source_claim_links enable row level security",
        "conflicts enable row level security",
        "model_call_budget_reservations enable row level security",
    ],
    "20260921000200_immutable_run_identity.sql": [
        # New runs are born with one identity in the same transaction as the
        # run row; legacy NULL identities can never be retrofitted later.
        "add column if not exists run_identity jsonb",
        "event_registry_fingerprint",
        "create or replace function public.runs_forbid_identity_rewrite()",
        "create trigger runs_forbid_identity_rewrite",
        "create or replace function public.runs_require_identity_on_insert()",
        "create trigger runs_require_identity_on_insert",
        "drop function if exists public.bind_run_identity(uuid, jsonb)",
        "create or replace function public.create_message_and_run_v3(",
        # The three worker writes that had no lease fence at all.
        "create or replace function public.create_tool_access_request_guarded(",
        "create or replace function public.create_tool_grant_guarded(",
        "create or replace function public.append_usage_ledger_guarded(",
        "perform public.assert_worker_lease(",
    ],
}

REQUIRED_PER_FILE["20260924000100_catalog_work_scope_batch_runs.sql"] = [
    # A batch run is created and bound in ONE transaction, through the one run
    # creator, and only the NEXT batch of an unpaused head revision starts.
    "enable row level security",
    "create or replace function public.create_work_scope_batch_run(",
    "from public.create_message_and_run_v3(",
    "public.bind_work_scope_batch_run(p_batch_id, p_run_id",
    "raise exception 'work_scope_stale'",
    "raise exception 'work_scope_paused'",
    "raise exception 'work_scope_batch_not_next'",
    "raise exception 'work_scope_batch_in_progress'",
    "create trigger catalog_work_scope_controls_append_only",
    "create trigger catalog_work_scope_controls_in_sequence",
    "for update",
    # A lost launch is reconciled only by an operator's guarded decision, and
    # only while no worker ever claimed the run and nothing is in flight.
    "create or replace function public.reconcile_lost_launch(",
    "raise exception 'lost_launch_claimed'",
    "raise exception 'lost_launch_not_quiet'",
    "raise exception 'lost_launch_traced'",
    # A run no worker was ever started for is retired only by an operator's
    # guarded decision, with its terminal event, never while anything ran.
    "create or replace function public.retire_unlaunched_run(",
    "raise exception 'unlaunched_run_claimed'",
    "raise exception 'unlaunched_run_traced'",
    "'run_cancelled', v_message, jsonb_build_object('code', 'run_not_launched')",
]
REQUIRED_PER_FILE["20260924000200_catalog_ingestion_recovery.sql"] = [
    # An orphaned pending snapshot changes owner only through an audited,
    # append-only adoption row of the same transaction, and only when its
    # owner is over; the batches reuse the unchanged single-row writes.
    "enable row level security",
    "create table if not exists public.catalog_snapshot_adoptions (",
    "create trigger catalog_snapshot_adoptions_checked",
    "create trigger catalog_snapshot_adoptions_append_only",
    "v_previous.status not in ('failed', 'cancelled', 'timed_out')",
    "v_adopter.run_identity->>'workflow_key' is distinct from 'operator_capture'",
    "a.adoption_txid = txid_current()",
    "create or replace function public.adopt_catalog_snapshot_guarded(",
    "create or replace function public.record_catalog_raw_records_batch_guarded(",
    "create or replace function public.record_catalog_candidates_batch_guarded(",
    "public.record_catalog_raw_record_guarded(",
    "public.record_catalog_candidate_guarded(",
    "not between 1 and 500",
    "perform public.assert_worker_lease(",
    "for update",
]
REQUIRED_PER_FILE["20260923000100_catalog_work_scope_preparation.sql"] = [
    # A scoped snapshot's declaration is held to the query it recorded, and the
    # preparation it feeds is written once, by an operator capture run only.
    "enable row level security",
    "constraint catalog_source_snapshots_capture_scope_consistent",
    "raise exception 'work_scope_preparation_run_invalid'",
    "raise exception 'work_scope_stale'",
    # A mostly-ambiguous manufacturer is stated, never queued.
    "work_scope_vocabulary_insufficient",
    "raise exception 'work_scope_batch_in_progress'",
    "create unique index if not exists catalog_work_scope_batch_runs_run_uidx",
    "forbid_work_scope_preparation_mutation",
    "for update",
]
REQUIRED_PER_FILE["20260922000100_catalog_work_scopes.sql"] = [
    # A plan's digest is derived by the database from its canonical text, and
    # no path can store a revision that disagrees with it or rewrite one.
    "enable row level security",
    "constraint catalog_work_scope_revisions_digest_derived",
    "constraint catalog_work_scope_revisions_scope_is_text",
    "constraint catalog_work_scope_revisions_record_valid",
    "create trigger catalog_work_scope_revisions_append_only",
    "create constraint trigger catalog_work_scopes_head_is_a_revision",
    "create unique index if not exists catalog_work_scopes_open_conversation_uidx",
    # A stale head fails closed inside the database, under a row lock.
    "raise exception 'work_scope_stale'",
    "for update",
]

FORBIDDEN_EVERYWHERE = [
    "drop table",
    "delete from public.conversations",
    "delete from public.messages",
    "delete from public.runs",
    "delete from public.run_events",
]


def main() -> None:
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        raise SystemExit("migration check failed; no migration files found")
    texts = {path.name: path.read_text().lower() for path in files}
    combined = "\n".join(texts.values())

    problems = []
    for item in REQUIRED_GLOBAL:
        if item not in combined:
            problems.append(f"missing globally: {item!r}")
    for name, clauses in REQUIRED_PER_FILE.items():
        if name not in texts:
            problems.append(f"missing migration file: {name}")
            continue
        for clause in clauses:
            if clause not in texts[name]:
                problems.append(f"{name}: missing required clause {clause!r}")
    for name, text in texts.items():
        for clause in FORBIDDEN_EVERYWHERE:
            if clause in text:
                problems.append(f"{name}: forbidden clause {clause!r}")

    if problems:
        raise SystemExit("migration check failed;\n  " + "\n  ".join(problems))
    print("migration check passed (static text validation only)")


if __name__ == "__main__":
    main()

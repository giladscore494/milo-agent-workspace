"""Offline static migration check.

Validates that the migration files still contain the safety and
legacy-baseline reconciliation clauses required for the production Supabase
project (four pre-existing tables, empty migration history). This is text
matching only; executable validation lives in tests/test_migrations_postgres.py.
"""

import re
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
    # ONE write authority: the current writer is the latest adopter, else the
    # creator, whose `created_by_run_id` is never rewritten; every write that
    # decides who may write a snapshot asks it.
    "enable row level security",
    "create or replace function public.assert_snapshot_write_authority(",
    "'this catalog snapshot does not belong to this run'",
    "create table if not exists public.catalog_snapshot_adoptions (",
    "create unique index if not exists catalog_snapshot_adoptions_seq_uidx",
    "create unique index if not exists catalog_snapshot_adoptions_adopter_uidx",
    "create trigger catalog_snapshot_adoptions_checked",
    "create trigger catalog_snapshot_adoptions_append_only",
    "v_previous.status not in ('failed', 'cancelled', 'timed_out')",
    "v_adopter.run_identity->>'workflow_key' is distinct from 'operator_capture'",
    "create or replace function public.record_catalog_raw_record_guarded(",
    "create or replace function public.activate_catalog_snapshot_guarded(",
    "public.assert_snapshot_write_authority(v_snapshot.id, p_run_id, true)",
    "create or replace function public.adopt_catalog_snapshot_guarded(",
    "'catalog_snapshot_adopted'",
    "create or replace function public.record_catalog_raw_records_batch_guarded(",
    "create or replace function public.record_catalog_candidates_batch_guarded(",
    # The batches are SET-BASED: the lease and the authority once per batch,
    # a bulk insert, the stored-record counter moved once by the number
    # inserted, and one replay check that fails the whole batch.
    "v_snapshot := public.assert_snapshot_write_authority(",
    "not between 1 and 500",
    "perform public.assert_worker_lease(",
    "get diagnostics v_inserted = row_count",
    "set stored_record_count = stored_record_count + v_inserted",
    "raise exception 'catalog raw record idempotency conflict'",
    "raise exception 'catalog candidate idempotency conflict'",
]
REQUIRED_PER_FILE["20260927000100_catalog_variant_coverage.sql"] = [
    # PR-Z: ONE ledger row per variant and level, service-path only; the queue
    # build filters through it BEFORE the plan's limit is spent; its write is
    # the finalizing lease's only, from a FINISHED run, and never weakens.
    "enable row level security",
    "create table if not exists public.catalog_variant_coverage (",
    "create unique index if not exists catalog_variant_coverage_key_level_uidx",
    "create or replace function public.catalog_variant_identity_key(",
    "create or replace function public.catalog_variant_coverage_decision(",
    "create or replace function public.prepare_work_scope_queue(",
    "v_take := least(v_eligible - v_enriched - v_unresolved, v_budget);",
    "raise exception 'catalog_coverage_run_not_finished'",
    "stale_worker_write: coverage write rejected",
    "public.catalog_variant_coverage_rank(excluded.status)",
    "create trigger catalog_work_scope_unit_coverage_append_only",
    # The paid-work claim: ONE owner per variant and level, claimed with the
    # row locked BEFORE the ledger is read, released only by settlement, and
    # never taken from a FINISHED run.
    "create table if not exists public.catalog_variant_reservations (",
    "create unique index if not exists catalog_variant_reservations_key_level_uidx",
    "on conflict (variant_identity_key, level) do nothing;",
    "for update;",
    "when r.status in ('completed', 'partial_success') then 'settling'",
    "delete from public.catalog_variant_reservations\n   where run_id = p_run_id and level = p_level;",
    "create or replace function public.catalog_variant_reservations_settling(",
    # PR-Z2: the key carries the Government's registration identifiers
    # verbatim, and a key whose rows differ is a collision -- recorded
    # `failed`, never settled by picking one of them.
    "select 'milo-variant-identity/2'",
    "r.payload->>'tozeret_cd', r.payload->>'degem_cd',",
    "then 'catalog_coverage_key_collision' end as reason_code",
    # PR-Z3: the bounded variant page narrows by the register's own
    # identifiers in the database, verbatim, on its existing raw-record join,
    # and stays ONE function.
    "drop function if exists public.catalog_candidate_variant_page(",
    "or r.payload->>'degem_cd' = p_register_model_code)",
    "or r.payload->>'sug_degem' = p_vehicle_type_code)",
]
REQUIRED_PER_FILE["20260928000100_catalog_work_scope_preparation_requests.sql"] = [
    # E': ONE web preparation request per (plan, revision), claimed under the
    # PLAN's row lock, moved on only by a compare-and-set on its own attempt,
    # and service-path only.
    "enable row level security",
    "create table if not exists public.catalog_work_scope_preparation_requests (",
    "create unique index if not exists catalog_work_scope_preparation_requests_revision_uidx",
    "select * into v_plan from public.catalog_work_scopes where id = p_work_scope_id for update;",
    "if not found or v_request.attempt <> p_attempt or v_request.trigger_state <> 'claimed' then",
    "coalesce(v_run.run_identity->>'workflow_key', '') <> 'operator_capture'",
    "revoke delete on table public.catalog_work_scope_preparation_requests from service_role",
]
REQUIRED_PER_FILE["20260929000100_catalog_register_capture.sql"] = [
    # PR-D1: register capture is service-path only (RLS, no policies), its
    # directory and archives are append-only, the capacity guard refuses with
    # its numbers before anything is written, the prune refuses any list but
    # the one its digest names, and the read-only role is granted explicitly.
    "enable row level security",
    "create table if not exists public.catalog_register_capture_units (",
    "create unique index if not exists catalog_register_capture_units_version_tozar_uidx",
    "forbid_catalog_register_rewrite",
    "raise exception 'catalog_capacity_threshold_exceeded: current=% projected=% limit=%'",
    "raise exception 'catalog_prune_digest_mismatch",
    "lock table public.catalog_source_snapshots, public.catalog_raw_records,",
    "revoke delete, truncate on table %s from service_role",
    "pg_has_role(r.oid, 'pg_read_all_data', 'member')",
    "execute format('grant execute on function %s to %i', fn, ro.rolname);",
]
REQUIRED_PER_FILE["20260930000100_catalog_variants.sql"] = [
    # PR-L1: variants are service-path only (RLS, no policies), append-only,
    # can never be orphaned by a prune (restrict FKs and the keep-set), carry
    # a closed equipment key list, and the read-only role is granted
    # explicitly.
    "enable row level security",
    "create table if not exists public.catalog_variants (",
    "create unique index if not exists catalog_variants_record_uidx",
    "forbid_catalog_variant_rewrite",
    "references public.catalog_raw_records (snapshot_id, upstream_record_id) on delete restrict",
    "check (public.catalog_variant_equipment_valid(equipment))",
    "and not exists (select 1 from public.catalog_variants x where x.snapshot_id = sc.id)",
    "revoke delete, truncate on table public.catalog_variants from service_role",
    "pg_has_role(r.oid, 'pg_read_all_data', 'member')",
]
REQUIRED_PER_FILE["20261001000100_catalog_variant_retention.sql"] = [
    # PR-L1b: a superseded snapshot is pruned WITH its variants (the variants'
    # trigger suspended only inside the digest-bound prune and re-enabled), the
    # current build of a tozar is always kept, only rank 1 is built, and the
    # compact equipment keeps the closed-list check.
    "alter table public.catalog_variants disable trigger catalog_variants_append_only;",
    "alter table public.catalog_variants enable trigger catalog_variants_append_only;",
    "catalog_prune_digest_mismatch",
    "where x.snapshot_key = sc.snapshot_key and x.level = 'register')",
    "and sc.id not in (select distinct on (b.tozar) b.snapshot_id",
    "catalog_variant_snapshot_superseded",
    "not public.catalog_variant_equipment_valid(coalesce(t.equipment, '{}'::jsonb))",
    "pg_has_role(r.oid, 'pg_read_all_data', 'member')",
]
REQUIRED_PER_FILE["20261002000100_catalog_register_compaction.sql"] = [
    # PR-L2: the raw payload leaves the database only through the one guarded
    # compaction (append-only trigger suspended there only and re-enabled),
    # a new record still needs its payload, and every reader that took facts
    # from the payload reads them through the two helpers.
    "alter table public.catalog_raw_records disable trigger catalog_raw_records_append_only;",
    "alter table public.catalog_raw_records enable trigger catalog_raw_records_append_only;",
    "catalog_raw_record_payload_required",
    "security definer",
    "catalog_compaction_typed_mismatch",
    "public.catalog_raw_record_code(r, 'tozeret_cd')",
    "public.catalog_raw_record_content_sha256(r)",
    "pg_has_role(r.oid, 'pg_read_all_data', 'member')",
]
REQUIRED_PER_FILE["20261002000200_catalog_register_compaction_decisions.sql"] = [
    # PR-L2 follow-up: a compacted row's codes and content hash come from its
    # variant row joined once; the decisions read nothing else differently.
    "create or replace function public.catalog_candidate_register_reading(p_snapshot_id uuid)",
    "from public.catalog_candidate_register_reading(p_snapshot_id) x",
    "v.tozeret_cd::text, v.degem_cd::text, v.sug_degem, v.content_sha256",
    "and public.catalog_snapshot_not_archived(p_snapshot_id)",
]
REQUIRED_PER_FILE["20261003000100_catalog_manufacturer_normalization.sql"] = [
    # PR-D3: the source tozar never changes; the normalisation is versioned
    # and append-only, the model's output is validated again in the database,
    # and a model entry must be exactly a proposed group.
    "create trigger catalog_manufacturer_normalization_entries_append_only",
    "public.catalog_normalization_groups_valid(p_groups, v_proposal.input)",
    "perform public.assert_worker_lease(p_run_id, p_worker_id, p_attempt, p_lease_token);",
    "catalog_normalization_version_stale",
    "enable row level security",
    "pg_has_role(r.oid, 'pg_read_all_data', 'member')",
]
REQUIRED_PER_FILE["20261004000100_catalog_maintenance_rpc_timeouts.sql"] = [
    # PR-OPS1 (P50): the whole-snapshot RPCs carry their own statement timeout
    # (PostgREST hoists it before the call), the two table-locking ones a 5 s
    # lock timeout; nothing role- or database-wide changes.
    "alter function public.compact_register_snapshot(text, boolean, text, uuid)\n  set statement_timeout = '300s';",
    "alter function public.compact_register_snapshot(text, boolean, text, uuid)\n  set lock_timeout = '5s';",
    "alter function public.prune_register_snapshots(text[], text)\n  set lock_timeout = '5s';",
    "alter function public.prepare_work_scope_queue(uuid, text, integer, text, jsonb)\n  set statement_timeout = '300s';",
]
REQUIRED_PER_FILE["20260930000200_catalog_work_scope_placeholder_exclusion.sql"] = [
    # P27: the queue build leaves PR-U's placeholder records out (never
    # eligible, never queued), records each with its reason and counts them.
    "create or replace function public.catalog_is_placeholder_identity(",
    "then 'excluded_placeholder_source_record'",
    "count(*) filter (where c.status = 'candidate' and not public.catalog_is_placeholder_identity(",
    "= excluded_already_enriched + excluded_known_unresolved + excluded_placeholder",
    "perform public.assert_worker_lease(",
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

# ONE snapshot write authority (20260924000200). From that migration on, a
# direct ownership comparison of a snapshot's `created_by_run_id` against the
# calling run may appear ONLY inside `assert_snapshot_write_authority`: every
# guarded write asks that function, which knows about adoptions. A second,
# hand-written check elsewhere would silently refuse an adopter (or, written
# the other way round, admit a run the authority refuses).
OWNERSHIP_AUTHORITY_SINCE = "20260924000200"
OWNERSHIP_AUTHORITY_FUNCTION = "assert_snapshot_write_authority"
_OWNERSHIP_CHECK = re.compile(
    r"created_by_run_id\s+is\s+(?:not\s+)?distinct\s+from\s+p_run_id"
    r"|created_by_run_id\s*(?:=|<>|!=)\s*p_run_id"
    r"|p_run_id\s+is\s+(?:not\s+)?distinct\s+from\s+[a-z_.]*created_by_run_id"
    r"|p_run_id\s*(?:=|<>|!=)\s*[a-z_.]*created_by_run_id")
_FUNCTION = re.compile(
    r"create\s+(?:or\s+replace\s+)?function\s+public\.([a-z0-9_]+)\s*\(.*?\$\$(.*?)\$\$",
    re.DOTALL)


def ownership_check_problems(texts: dict[str, str]) -> list[str]:
    """Every direct snapshot-ownership comparison outside the one authority,
    in any migration at or after `OWNERSHIP_AUTHORITY_SINCE`. `texts` maps a
    migration filename to its lowercased text."""
    problems = []
    for name, text in sorted(texts.items()):
        if name.split("_", 1)[0] < OWNERSHIP_AUTHORITY_SINCE:
            continue
        allowed = [(match.start(2), match.end(2)) for match in _FUNCTION.finditer(text)
                   if match.group(1) == OWNERSHIP_AUTHORITY_FUNCTION]
        for check in _OWNERSHIP_CHECK.finditer(text):
            if not any(start <= check.start() < end for start, end in allowed):
                problems.append(f"{name}: a direct created_by_run_id ownership check outside "
                                f"{OWNERSHIP_AUTHORITY_FUNCTION}: {check.group(0)!r}")
    return problems


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
    problems.extend(ownership_check_problems(texts))
    for name, text in texts.items():
        for clause in FORBIDDEN_EVERYWHERE:
            if clause in text:
                problems.append(f"{name}: forbidden clause {clause!r}")

    if problems:
        raise SystemExit("migration check failed;\n  " + "\n  ".join(problems))
    print("migration check passed (static text validation only)")


if __name__ == "__main__":
    main()

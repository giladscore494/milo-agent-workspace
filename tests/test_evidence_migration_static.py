from pathlib import Path


MIGRATION = Path("supabase/migrations/20260823000100_lease_guarded_evidence_writes.sql")
RPCS = (
    "create_tool_usage_guarded",
    "upsert_source_guarded",
    "create_claim_with_source_guarded",
    "create_conflict_guarded",
    "patch_run_blackboard_evidence_guarded",
)


def test_evidence_migration_is_rerun_safe_guarded_and_service_only():
    sql = MIGRATION.read_text().lower()
    assert sql.count("add column if not exists") == 8
    assert sql.count("create unique index if not exists") == 4
    for rpc in RPCS:
        assert f"create or replace function public.{rpc}" in sql
        signature = f"public.{rpc}(uuid,text,integer,text,jsonb)"
        assert f"revoke execute on function %s from public" in sql
        assert signature in sql
    assert sql.count("perform public.assert_worker_lease") == 5
    assert "grant execute on function %s to service_role" in sql
    assert "revoke execute on function %s from anon" in sql
    assert "revoke execute on function %s from authenticated" in sql
    assert sql.count("set search_path = pg_catalog") == 5


def test_claim_and_link_are_atomic_and_conflicts_are_scope_checked():
    sql = MIGRATION.read_text().lower()
    claim_rpc = sql.split("create or replace function public.create_claim_with_source_guarded", 1)[1]
    claim_rpc = claim_rpc.split("create or replace function public.create_conflict_guarded", 1)[0]
    assert "insert into public.claims" in claim_rpc
    assert "insert into public.source_claim_links" in claim_rpc
    assert "invalid claim source" in claim_rpc
    conflict_rpc = sql.split("create or replace function public.create_conflict_guarded", 1)[1]
    assert "entity_key, field_key, geography, market, time_scope" in conflict_rpc
    assert "count(distinct value)" in conflict_rpc
    assert "share one scope, and contradict" in conflict_rpc


def test_migration_rejects_sensitive_or_reasoning_payloads():
    sql = MIGRATION.read_text().lower()
    for marker in ("chain_of_thought", "provider_detail", "raw_error", "secret sentinel"):
        assert marker in sql

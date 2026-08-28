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


CANONICAL_MIGRATION = Path(
    "supabase/migrations/20260828000100_canonical_scope_conflict_identity.sql")


def test_canonical_scope_migration_is_additive_guarded_and_service_only():
    sql = CANONICAL_MIGRATION.read_text().lower()
    assert sql.count("add column if not exists") == 2
    assert "canonical_scope_hash" in sql and "scope_normalization_version" in sql
    for rpc in ("create_claim_with_source_guarded", "create_conflict_guarded"):
        assert f"create or replace function public.{rpc}" in sql
        assert f"public.{rpc}(uuid,text,integer,text,jsonb)" in sql
    assert sql.count("perform public.assert_worker_lease") == 2
    assert sql.count("set search_path = pg_catalog") == 2
    assert "grant execute on function %s to service_role" in sql
    assert "revoke execute on function %s from anon" in sql
    assert "revoke execute on function %s from authenticated" in sql
    for marker in ("chain_of_thought", "provider_detail", "raw_error", "secret sentinel"):
        assert marker in sql
    assert "drop table" not in sql and "delete from" not in sql


def test_canonical_scope_migration_upgrades_only_exact_replays_never_bulk():
    sql = CANONICAL_MIGRATION.read_text().lower()
    # Exactly one UPDATE exists: the single-row upgrade-on-replay that
    # populates ONLY the canonical identity columns for one verified claim id.
    # No bulk backfill of historical rows is permitted in this migration.
    assert sql.count("update public.claims") == 1
    upgrade = sql.split("update public.claims", 1)[1].split("returning", 1)[0]
    assert "where id = v_row.id" in upgrade
    assert "canonical_scope_hash" in upgrade and "scope_normalization_version" in upgrade
    for original in ("entity_key", "field_key", "value", "time_scope", "geography",
                     "market", "evidence_key", "task_key", "source_id"):
        assert f"{original} =" not in upgrade.replace("where id =", "")
    # The replay path fails closed on mismatch and on half-populated state.
    assert "canonical scope identity mismatch" in sql
    assert "does not match the stored claim" in sql
    assert "canonical scope state is invalid" in sql


def test_canonical_scope_migration_validates_trusted_identity_not_raw_text():
    sql = CANONICAL_MIGRATION.read_text().lower()
    conflict_rpc = sql.split("create or replace function public.create_conflict_guarded", 1)[1]
    # Conflict eligibility must come from the backend-computed canonical
    # identity, never from a second SQL normalization of the raw scope text.
    assert "count(distinct row(canonical_scope_hash, scope_normalization_version))" in conflict_rpc
    assert "require a trusted canonical scope identity" in conflict_rpc
    for forbidden in ("lower(", "regexp_replace", "replace(", "normalize("):
        assert forbidden not in conflict_rpc.split("insert into public.conflicts")[0].replace(
            "lower(p_conflict::text)", "").replace("lower(coalesce(p_conflict->>'rationale', ''))", "")
    claim_rpc = sql.split("create or replace function public.create_claim_with_source_guarded", 1)[1]
    claim_rpc = claim_rpc.split("create or replace function public.create_conflict_guarded", 1)[0]
    assert "'^[0-9a-f]{64}$'" in claim_rpc
    assert "trusted canonical scope identity is required" in claim_rpc

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


FRAGMENT_MIGRATION = Path(
    "supabase/migrations/20260828000200_source_evidence_fragments.sql")


def test_fragment_migration_is_additive_forward_only_and_never_backfills():
    sql = FRAGMENT_MIGRATION.read_text().lower()
    assert "create table if not exists public.source_evidence_fragments" in sql
    # Additive only: no existing table, column, row or index is touched.
    for destructive in ("drop table", "drop column", "delete from", "truncate",
                        "alter column", "update public."):
        assert destructive not in sql
    assert "alter table public.sources" not in sql
    assert "alter table public.claims" not in sql
    # Rerun-safe by repository convention.
    assert sql.count("create table if not exists") == 1
    assert sql.count("create unique index if not exists") == 1
    assert sql.count("create index if not exists") == 1
    assert "drop trigger if exists source_evidence_fragments_append_only" in sql
    assert "create or replace function public.forbid_evidence_fragment_mutation" in sql


def test_fragment_relation_is_source_bound_run_bound_and_append_only():
    sql = FRAGMENT_MIGRATION.read_text().lower()
    table = sql.split("create table if not exists public.source_evidence_fragments", 1)[1]
    table = table.split(");", 1)[0]
    assert "run_id uuid not null references public.runs(id)" in table
    assert "source_id uuid not null references public.sources(id)" in table
    for column in ("task_key text not null", "evidence_key text not null",
                   "fragment_text text not null", "content_hash text not null",
                   "fragment_index integer not null"):
        assert column in table
    # Deterministic retry identity, and append-only durability.
    assert "source_evidence_fragments(run_id, evidence_key)" in sql
    assert "before update or delete on public.source_evidence_fragments" in sql
    assert "source_evidence_fragments is append-only" in sql


def test_fragment_rpc_is_lease_guarded_service_only_and_returns_a_set():
    sql = FRAGMENT_MIGRATION.read_text().lower()
    rpc = "record_evidence_fragment_guarded"
    assert f"create or replace function public.{rpc}" in sql
    assert "returns setof public.source_evidence_fragments" in sql
    assert f"public.{rpc}(uuid,text,integer,text,jsonb)" in sql
    assert sql.count("perform public.assert_worker_lease") == 1
    assert sql.count("set search_path = pg_catalog") == 1
    assert "revoke execute on function %s from public" in sql
    assert "revoke execute on function %s from anon" in sql
    assert "revoke execute on function %s from authenticated" in sql
    assert "grant execute on function %s to service_role" in sql
    # The table itself stays off the browser surface and out of reach of
    # anything but a service-path read/append.
    assert "revoke all on table public.source_evidence_fragments from public" in sql
    assert "revoke all on table public.source_evidence_fragments from anon" in sql
    assert "revoke all on table public.source_evidence_fragments from authenticated" in sql
    assert "grant select, insert on table public.source_evidence_fragments to service_role" in sql
    assert "revoke update, delete on table public.source_evidence_fragments from service_role" in sql
    assert "enable row level security" in sql
    assert "create policy" not in sql


def test_fragment_rpc_enforces_source_binding_safety_and_hash_integrity():
    sql = FRAGMENT_MIGRATION.read_text().lower()
    rpc = sql.split("create or replace function public.record_evidence_fragment_guarded", 1)[1]
    assert "from public.sources" in rpc and "run_id = p_run_id" in rpc
    assert "invalid evidence fragment source" in rpc
    # Per-source quota admission is serialized on the durable source row, so
    # concurrent writers cannot both pass a pre-insert count/budget check.
    assert "and run_id = p_run_id for update;" in rpc
    assert "p_run_id for key share" not in rpc  # the weaker lock is gone
    # Lineage: task -> source -> fragment.  Same run is not enough provenance.
    assert "v_source.task_key is distinct from v_task" in rpc
    assert "evidence fragment task provenance mismatch" in rpc
    # ONE replay invariant, reached by both the pre-insert and the
    # lost-the-race path; the error is static and leaks no evidence text.
    assert rpc.count("evidence fragment idempotency conflict") == 1
    for field in ("v_row.source_id is distinct from v_source.id",
                  "v_row.task_key is distinct from v_task",
                  "v_row.fragment_text is distinct from v_text",
                  "v_row.content_hash is distinct from v_hash"):
        assert field in rpc
    # fragment_index is deliberately outside the fragment's logical identity
    # and a replay must never rewrite the stored position.
    assert "v_row.fragment_index" not in rpc
    # Quota is evaluated only for a genuinely new fragment, so an exact replay
    # still succeeds once the source is at its budget.
    replay_first = rpc.index("select * into v_row from public.source_evidence_fragments")
    assert replay_first < rpc.index("v_count >= 4")
    # The database recomputes the hash from the durable text; no embedding or
    # similarity is involved anywhere.
    assert "encode(sha256(convert_to(v_text, 'utf8')), 'hex') <> v_hash" in rpc
    assert "'^[0-9a-f]{64}$'" in rpc
    assert "create extension" not in sql  # no similarity/embedding machinery
    for marker in ("chain_of_thought", "provider_detail", "raw_error", "secret sentinel",
                   "chain of thought", "hidden reasoning", "lease_token", "private_key"):
        assert marker in rpc
    assert "must not be empty" in rpc


def test_fragment_hard_limits_match_the_backend_constants_exactly():
    """The SQL literals and the Python constants are one contract."""
    from backend.engines.swarm_v2.fragments import (MAX_FRAGMENT_CHARS,
                                                    MAX_FRAGMENTS_PER_SOURCE,
                                                    MAX_FRAGMENT_TOTAL_CHARS_PER_SOURCE)

    sql = FRAGMENT_MIGRATION.read_text().lower()
    assert (f"check (char_length(fragment_text) between 1 and {MAX_FRAGMENT_CHARS})") in sql
    assert (f"check (fragment_index between 0 and {MAX_FRAGMENTS_PER_SOURCE - 1})") in sql
    assert f"char_length(v_text) > {MAX_FRAGMENT_CHARS}" in sql
    assert f"v_index > {MAX_FRAGMENTS_PER_SOURCE - 1}" in sql
    assert f"v_count >= {MAX_FRAGMENTS_PER_SOURCE}" in sql
    assert (f"v_total + char_length(v_text) > "
            f"{MAX_FRAGMENT_TOTAL_CHARS_PER_SOURCE}") in sql


def test_fragment_migration_adds_no_browser_surface():
    """public.sources keeps its meaning; fragments never become browser payload."""
    sql = FRAGMENT_MIGRATION.read_text().lower()
    assert "fragment_text" not in Path("backend/main.py").read_text()
    assert "source_evidence_fragments" not in Path("backend/main.py").read_text()
    assert "to anon" not in sql and "to authenticated" not in sql

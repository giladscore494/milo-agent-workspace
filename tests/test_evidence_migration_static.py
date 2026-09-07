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


R3_MIGRATION = Path("supabase/migrations/20260902000100_r3_versioned_focused_evidence.sql")
R3_RPCS = ("upsert_source_guarded", "create_claim_with_source_guarded",
           "record_evidence_fragment_guarded")


def test_r3_migration_is_additive_nullable_rerun_safe_and_never_backfills():
    sql = R3_MIGRATION.read_text().lower()
    assert sql.count("add column if not exists") == 5
    for column in ("public.sources add column if not exists source_version_kind text",
                   "public.sources add column if not exists source_version_id text",
                   "public.claims add column if not exists evidence_locator text",
                   "public.source_evidence_fragments add column if not exists fragment_type text",
                   "public.source_evidence_fragments add column if not exists locator_key text"):
        assert f"alter table {column}" in sql
    # Nothing is rewritten, dropped, defaulted or backfilled: historical rows
    # keep a NULL in every new column and stay valid forever.
    for destructive in ("drop table", "drop column", "delete from", "truncate",
                        "alter column", "update public.sources",
                        "update public.source_evidence_fragments", "not null default",
                        "create table"):
        assert destructive not in sql
    # The single UPDATE is the inherited pre-canonical upgrade-on-replay, which
    # still touches only the two canonical identity columns of one claim id.
    assert sql.count("update public.claims") == 1
    upgrade = sql.split("update public.claims", 1)[1].split("returning", 1)[0]
    assert "where id = v_row.id" in upgrade
    assert "canonical_scope_hash" in upgrade and "scope_normalization_version" in upgrade
    assert "evidence_locator" not in upgrade
    # Constraints are dropped and re-added BY NAME, so a re-apply converges on
    # the current definition instead of silently keeping an earlier draft's.
    for name in ("sources_version_pairing", "claims_evidence_locator_canonical",
                 "source_evidence_fragments_focus_pairing"):
        assert f"drop constraint if exists {name}" in sql
        assert sql.count(f"add constraint {name}") == 1
    assert "drop constraint if exists claims_evidence_locator_bounded" in sql  # the earlier draft
    assert "drop constraint" in sql and sql.count("drop constraint if exists") == 4


R3_HELPERS = ("r3_source_version_valid(text,text)", "r3_canonical_locator(text)",
              "r3_focus_valid(text,text)")


def test_r3_rpcs_stay_lease_guarded_service_only_and_return_a_set():
    sql = R3_MIGRATION.read_text().lower()
    for rpc in R3_RPCS:
        assert f"create or replace function public.{rpc}" in sql
        assert f"public.{rpc}(uuid,text,integer,text,jsonb)" in sql
    # The three shape helpers are pure, read no table, and follow the SAME
    # service-only ACL convention as every other public function.
    for helper in R3_HELPERS:
        assert f"create or replace function public.{helper.split('(')[0]}" in sql
        assert f"'public.{helper}'" in sql
    assert sql.count("perform public.assert_worker_lease") == 3
    assert sql.count("set search_path = pg_catalog") == 6
    assert sql.count("\nimmutable\n") == 3   # the three helpers, and only them
    assert sql.count("returns setof public.sources") == 1
    assert sql.count("returns setof public.claims") == 1
    assert sql.count("returns setof public.source_evidence_fragments") == 1
    assert "revoke execute on function %s from public" in sql
    assert "revoke execute on function %s from anon" in sql
    assert "revoke execute on function %s from authenticated" in sql
    assert "grant execute on function %s to service_role" in sql
    # The fragment relation keeps its exact service-only, append-only posture.
    assert "grant select, insert on table public.source_evidence_fragments to service_role" in sql
    assert "revoke update, delete on table public.source_evidence_fragments from service_role" in sql
    assert "revoke all on table public.source_evidence_fragments from anon" in sql
    assert "revoke all on table public.source_evidence_fragments from authenticated" in sql
    assert "create policy" not in sql and "to anon" not in sql and "to authenticated" not in sql
    for marker in ("chain_of_thought", "provider_detail", "raw_error", "secret sentinel"):
        assert marker in sql


def test_r3_rpcs_carry_forward_every_inherited_evidence_guarantee():
    """CREATE OR REPLACE rewrites a whole function: nothing may be dropped."""
    sql = R3_MIGRATION.read_text().lower()
    source_rpc = sql.split("create or replace function public.upsert_source_guarded", 1)[1]
    source_rpc = source_rpc.split("create or replace function public.create_claim", 1)[0]
    assert "on conflict (run_id, evidence_key)" in source_rpc
    assert "evidence_key and task_key are required" in source_rpc

    claim_rpc = sql.split("create or replace function public.create_claim_with_source_guarded", 1)[1]
    claim_rpc = claim_rpc.split("create or replace function public.record_evidence_fragment", 1)[0]
    for inherited in ("trusted canonical scope identity is required",
                      "'^[0-9a-f]{64}$'", "invalid claim source",
                      "insert into public.source_claim_links",
                      "idempotency key belongs to a different source",
                      "claim canonical scope identity mismatch",
                      "idempotent claim replay does not match the stored claim",
                      "claim canonical scope state is invalid"):
        assert inherited in claim_rpc

    fragment_rpc = sql.split("create or replace function public.record_evidence_fragment_guarded", 1)[1]
    fragment_rpc = fragment_rpc.split("do $$", 1)[0]
    for inherited in ("and run_id = p_run_id for update;",
                      "v_source.task_key is distinct from v_task",
                      "evidence fragment task provenance mismatch",
                      "invalid evidence fragment source",
                      "encode(sha256(convert_to(v_text, 'utf8')), 'hex') <> v_hash",
                      "must not be empty", "fragment_text exceeds the durable bound",
                      "-----begin", "private_key", "hidden reasoning"):
        assert inherited in fragment_rpc
    assert fragment_rpc.count("evidence fragment idempotency conflict") == 1
    # Replay is still resolved BEFORE the quota, and position is still outside
    # the fragment's logical identity.
    replay_first = fragment_rpc.index("select * into v_row from public.source_evidence_fragments")
    assert replay_first < fragment_rpc.index("v_count >= 4")
    assert "v_row.fragment_index" not in fragment_rpc


def test_r3_rpcs_validate_the_new_contract_in_sql_not_only_in_pydantic():
    sql = R3_MIGRATION.read_text().lower()
    # Both halves of every all-or-nothing pair, in SQL.
    assert "(v_kind is null) <> (v_identifier is null)" in sql
    assert "(v_type is null) <> (v_locator is null)" in sql
    # A located fact and a focused fragment both require a versioned source.
    assert sql.count("v_source.source_version_kind is null") == 2
    assert "a located fact requires a versioned source" in sql
    assert "a focused fragment requires a versioned source" in sql
    # A numeric located fact must state its unit; a legacy claim is untouched.
    assert "jsonb_typeof(p_claim->'value') = 'number'" in sql
    assert "a numeric located fact requires an explicit unit" in sql
    # Version and locator are part of identity: a replay may never move them.
    assert "source version identity conflict" in sql
    assert "claim evidence locator mismatch" in sql
    assert "v_row.locator_key is distinct from v_locator" in sql
    assert "v_row.fragment_type is distinct from v_type" in sql
    # The COMPLETE shape, in SQL: kind-specific versions, canonical locators,
    # fragment-type/locator-kind pairing, and a located claim backed by
    # focused evidence of its own source in its own run.
    assert "not public.r3_source_version_valid(v_kind, v_identifier)" in sql
    assert sql.count("public.r3_canonical_locator(v_locator) is null") == 2   # claim + fragment
    assert "not public.r3_focus_valid(v_type, v_locator)" in sql
    assert "fragment type does not match the locator kind" in sql
    assert "locator is not a canonical bounded location" in sql
    backing = sql.split("'invalid claim: a located fact must be backed by focused evidence of its own source'", 1)[0]
    backing = backing[backing.rindex("if v_locator is not null and not exists"):]
    assert "f.run_id = p_run_id and f.source_id = v_source.id and f.locator_key = v_locator" in backing
    # The table constraints apply the SAME helpers, so a direct insert that
    # bypasses the RPC is held to the same shape.
    assert "public.r3_source_version_valid(source_version_kind, source_version_id)" in sql
    assert "public.r3_canonical_locator(evidence_locator) is not null" in sql
    assert "public.r3_focus_valid(fragment_type, locator_key)" in sql
    # A locator is parsed, bounded, re-rendered and compared -- never run.
    canonical = sql.split("create or replace function public.r3_canonical_locator", 1)[1]
    canonical = canonical.split("create or replace function public.r3_focus_valid", 1)[0]
    assert "if v_canonical <> p_locator then" in canonical
    assert "jsonb_array_length(v) <> 6" in canonical
    for forbidden in ("jsonb_path_query", "jsonb_path_exists", "@?", "@@", "execute "):
        assert forbidden not in canonical


def test_r3_hard_limits_match_the_backend_constants_exactly():
    """The SQL literals and the Python bounds/patterns are one contract."""
    from backend.engines.swarm_v2.evidence_bounds import (
        FRAGMENT_TYPE_BY_LOCATOR_KIND, FRAGMENT_TYPES, LOCATOR_RECORD_ID_PATTERN,
        LOCATOR_SEGMENT_PATTERN, MAX_DOCUMENT_OFFSET, MAX_LOCATOR_KEY_CHARS,
        MAX_LOCATOR_PATH_SEGMENTS, MAX_LOCATOR_SECTION_CHARS, SOURCE_VERSION_KINDS,
        SOURCE_VERSION_PATTERNS)
    from backend.engines.swarm_v2.fragments import MAX_FRAGMENT_CHARS

    sql = R3_MIGRATION.read_text()   # case-sensitive: the patterns carry [A-Z]
    lowered = sql.lower()
    # Every version kind's identifier rule is the SAME expression in SQL.
    version_fn = sql.split("public.r3_source_version_valid(p_kind text, p_identifier text)", 1)[1]
    version_fn = version_fn.split("$$;", 1)[0]
    for kind, pattern in SOURCE_VERSION_PATTERNS.items():
        assert f"when '{kind}' then p_identifier ~ '{pattern}'" in version_fn
    assert set(SOURCE_VERSION_KINDS) == set(SOURCE_VERSION_PATTERNS)
    assert version_fn.count("when '") == len(SOURCE_VERSION_KINDS)
    # The locator alphabet, the closed kind set and every bound, verbatim.
    locator_fn = sql.split("create or replace function public.r3_canonical_locator", 1)[1]
    locator_fn = locator_fn.split("create or replace function public.r3_focus_valid", 1)[0]
    assert f"v_record !~ '{LOCATOR_RECORD_ID_PATTERN}'" in locator_fn
    assert f"(element #>> '{{}}') !~ '{LOCATOR_SEGMENT_PATTERN}'" in locator_fn
    assert f"char_length(p_locator) > {MAX_LOCATOR_KEY_CHARS}" in locator_fn
    assert f"v_len < 1 or v_len > {MAX_LOCATOR_PATH_SEGMENTS}" in locator_fn
    assert f"v_end > {MAX_DOCUMENT_OFFSET} or v_end - v_start > {MAX_FRAGMENT_CHARS}" in locator_fn
    assert f"char_length(v_section) > {MAX_LOCATOR_SECTION_CHARS}" in locator_fn
    assert "v_kind not in ('document_span', 'record_field')" in locator_fn
    # The fragment-type/locator-kind pairing is the same closed map.
    focus_fn = sql.split("create or replace function public.r3_focus_valid", 1)[1].split("$$;", 1)[0]
    for kind, fragment_type in FRAGMENT_TYPE_BY_LOCATOR_KIND.items():
        assert f"(p_fragment_type = '{fragment_type}' and v_kind = '{kind}')" in focus_fn
    # Both boolean helpers are strictly boolean: a NULL result would pass a
    # CHECK constraint and silently admit the rows they exist to refuse.
    assert "if v_kind is null then\n    return false;" in focus_fn
    assert "select coalesce(p_kind is not null and p_identifier is not null and case p_kind" in sql
    assert "end, false)" in sql
    types = ", ".join(f"'{kind}'" for kind in sorted(FRAGMENT_TYPES))
    assert lowered.count(f"in ({types})") == 1       # the fragment RPC's early allowlist
    # Once in the claim RPC and once in the fragment RPC: both bound the
    # locator in SQL rather than trusting the backend contract alone.
    assert lowered.count(f"char_length(v_locator) > {MAX_LOCATOR_KEY_CHARS}") == 2
    sql = lowered
    # The three B2 fragment bounds are REUSED unchanged, never redefined.
    from backend.engines.swarm_v2.fragments import (MAX_FRAGMENT_CHARS,
                                                    MAX_FRAGMENTS_PER_SOURCE,
                                                    MAX_FRAGMENT_TOTAL_CHARS_PER_SOURCE)
    assert f"char_length(v_text) > {MAX_FRAGMENT_CHARS}" in sql
    assert f"v_index > {MAX_FRAGMENTS_PER_SOURCE - 1}" in sql
    assert f"v_count >= {MAX_FRAGMENTS_PER_SOURCE}" in sql
    assert f"v_total + char_length(v_text) > {MAX_FRAGMENT_TOTAL_CHARS_PER_SOURCE}" in sql


def test_r3_adds_no_browser_surface_and_no_production_tool():
    """R3 provenance is internal evidence, and no real source is connected."""
    api = Path("backend/main.py").read_text()
    for internal in ("source_version_kind", "source_version_id", "evidence_locator",
                     "locator_key", "fragment_type", "source_evidence_fragments"):
        assert internal not in api
    # The R3 columns are added by the trusted board, never by the worker-facing
    # HTTP schemas whose rows are echoed into browser-visible run events.
    schemas = Path("backend/schemas.py").read_text()
    for internal in ("source_version_kind", "source_version_id", "evidence_locator"):
        assert internal not in schemas
    worker = Path("backend/worker/main.py").read_text()
    assert "tools = ToolRegistry()" in worker            # production registry stays empty
    assert "deliberately left unwired" in worker         # and so does the acquisition sink
    mapping = Path("backend/engines/swarm_v2/evidence_mapping.py").read_text()
    assert "PRODUCTION_EVIDENCE_MAPPERS = EvidenceMapperRegistry()" in mapping

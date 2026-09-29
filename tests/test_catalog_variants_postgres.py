"""PR-L1: migration 20260930000100 (catalog variants) on ephemeral PostgreSQL.

Every migration applied, into a database that already has the production-
shaped read-only role (LOGIN, BYPASSRLS, NOT in pg_read_all_data). A snapshot
of REAL committed register rows is built through `record_catalog_variants`
exactly as the Python mapper produces them. It must: store every rule's
typed value; derive the identity key and content hash exactly as the Python
does; be idempotent; write a new mapper version beside the old rows and never
over them; refuse an update or delete, an unknown equipment key and an
ineligible snapshot; never let a prune orphan a variant; write the ledger at
`identity` and `government_fields` (a changed content from a newer snapshot
replaces, an older snapshot never takes it back, a key collision fails the
key) and leave `register` rows alone; serve the discovery tree paged; and let
the read-only role read it and write nothing. L1-6: bytes per variant row.
"""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import uuid
from pathlib import Path

import pytest

from backend.catalog import coverage as catalog_coverage
from backend.catalog.government.normalize import read_wltp_record
from backend.catalog.register import variants as mapper
from tests.test_catalog_variants import fixture_rows
from tests.test_migrations_postgres import (
    BASELINE,
    MIGRATIONS,
    SEED_LEGACY_ROWS,
    SUPABASE_AUTH_SHIM,
    EphemeralPostgres,
    _catalog_candidate_json,
    _catalog_record_json,
    _catalog_snapshot_json,
    _require_pg_bin,
    _rpc_as_service,
    _ws_world,
    _wsp_capture_run,
    _wsp_metadata,
)

VARIANTS_PG_PORT = "54998"
#: By name, not by position: later migrations apply after it.
VARIANTS_MIGRATION = next(m for m in MIGRATIONS if m.name == "20260930000100_catalog_variants.sql")
#: PR-L1b restates PR-L1's functions and the variant storage (also by name).
RETENTION_MIGRATION = next(m for m in MIGRATIONS if m.name == "20261001000100_catalog_variant_retention.sql")
RESOURCE = "142afde2-6228-49f9-8a29-9b6c3a0cbe40"
TOYOTA = "טויוטה"
RELEASE_RO = "milo_release_readonly_4b1d9e7c2a60"
RO_ROLE = "supabase_read_only_user"
NOT_A_READER = "milo_not_a_reader_test"
RELATIONS = ("catalog_variant_builds", "catalog_variants", "catalog_variants_current")
READS = ("catalog_variant_mapper_version()", "catalog_variant_build_state(uuid,text)",
         "catalog_browser_manufacturers(text,integer,integer,integer,text,integer,integer)",
         "catalog_browser_models(text,text,integer,integer,integer,text,integer,integer)",
         "catalog_browser_years(text,text,text,integer,integer,integer,text,integer,integer)",
         "catalog_browser_variants(text,text,integer,text,integer,integer,integer,text,integer,integer)",
         "catalog_browser_facets(text)",
         # PR-L1b.
         "catalog_variant_equipment(integer,integer,text[])", "catalog_variant_current_snapshot(text)",
         "catalog_register_measured_bytes(uuid)", "catalog_register_prunable_variant_builds()",
         "catalog_register_prunable_list()")
WRITE = "record_catalog_variants(uuid,text,jsonb)"


@pytest.fixture(scope="module")
def vdb():
    server = EphemeralPostgres(_require_pg_bin(), port=VARIANTS_PG_PORT)
    server.start()
    try:
        server.create_database()
        server.psql(f"create role {RELEASE_RO} login bypassrls; grant usage on schema public to {RELEASE_RO}; "
                    f"alter default privileges for role postgres in schema public "
                    f"grant select on tables to {RELEASE_RO}")
        server.psql(file=BASELINE)
        server.psql(sql=SEED_LEGACY_ROWS)
        server.psql(sql=SUPABASE_AUTH_SHIM)
        server.psql(f"create role {RO_ROLE} login bypassrls; grant pg_read_all_data to {RO_ROLE}; "
                    f"create role {NOT_A_READER} login bypassrls")
        assert VARIANTS_MIGRATION in MIGRATIONS
        for migration in MIGRATIONS:
            server.psql(file=migration)
        yield server
    finally:
        server.stop()


def _json(value) -> str:
    return "$j$" + json.dumps(value, ensure_ascii=False) + "$j$::jsonb"


def _snapshot(db, rows: list[dict], label: str, *, marque: str | None = TOYOTA,
              archived: bool = True) -> dict:
    """An ACTIVE snapshot of `rows` with one candidate per row, as ingestion
    writes it (PR-V's reading); archived like a register capture."""
    _user, _project, conversation = _ws_world(db)
    run_id, args = _wsp_capture_run(db, conversation)
    payload = json.loads(_catalog_snapshot_json(f"var-{label}", declared=len(rows)))
    payload["retrieval_metadata"] = _wsp_metadata(marque, count=len(rows))
    snapshot = _rpc_as_service(db, f"select id from public.record_catalog_snapshot_guarded({args}, "
                                   f"{_json(payload)})")
    for index, row in enumerate(rows):
        record = json.loads(_catalog_record_json(snapshot, f"var-{label}-r{index}", upstream=str(row["_id"]),
                                                 payload=row, locator={"page_index": 0, "page_number": 1,
                                                                       "page_offset": index,
                                                                       "capture_index": index}))
        record_id = _rpc_as_service(db, f"select id from public.record_catalog_raw_record_guarded({args}, "
                                        f"{_json(record)})")
        reading = read_wltp_record(row)
        candidate = json.loads(_catalog_candidate_json(
            snapshot, record_id, f"var-{label}-c{index}", status=reading.status, make=reading.manufacturer,
            model=reading.commercial_model, years=(reading.model_year_start, reading.model_year_end),
            code=reading.official_model_code, trim=reading.trim, dimensions=reading.identity_dimensions))
        _rpc_as_service(db, f"select id from public.record_catalog_candidate_guarded({args}, {_json(candidate)})")
    _rpc_as_service(db, f"select id from public.activate_catalog_snapshot_guarded({args}, "
                        f"'{json.dumps({'snapshot_id': snapshot})}'::jsonb)")
    key = db.psql(f"select snapshot_key from public.catalog_source_snapshots where id='{snapshot}'")
    if archived:
        _rpc_as_service(db, f"select public.record_register_snapshot_archive({args}, '{snapshot}', "
                            f"'gs://milo-test-archive/register/{RESOURCE}/{'0' * 16}/{key}.jsonl.gz', 10, "
                            f"'{'a' * 64}', {len(rows)})")
    # The writer run ends, as a capture does: a live writer keeps its snapshot.
    db.psql(f"update public.runs set status = 'completed' where id = '{run_id}'")
    return {"id": snapshot, "key": key, "run_id": run_id, "rows": rows}


def _psql_file(db, sql: str) -> str:
    """A statement too large for one command-line argument (a 500-row batch)."""
    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False, encoding="utf-8") as handle:
        handle.write(sql)
    try:
        return db.psql(file=Path(handle.name))
    finally:
        Path(handle.name).unlink()


def _build(db, snapshot: dict, rows: list[dict] | None = None, version: str = mapper.MAPPER_VERSION) -> dict:
    mapped = [mapper.map_record(row) for row in (snapshot["rows"] if rows is None else rows)]
    return json.loads(_psql_file(db, f"set role service_role; select public.record_catalog_variants("
                                     f"'{snapshot['id']}', '{version}', {_json(mapped)}); reset role;"))


def _refusal(db, sql: str) -> str:
    with pytest.raises(AssertionError) as refused:
        _rpc_as_service(db, sql)
    return str(refused.value)


def _variant(db, snapshot: dict, upstream: str, columns: str) -> str:
    return db.psql(f"select {columns} from public.catalog_variants where snapshot_id='{snapshot['id']}' "
                   f"and upstream_record_id='{upstream}' and mapper_version='{mapper.MAPPER_VERSION}'")


@pytest.fixture(scope="module")
def built(vdb):
    snapshot = _snapshot(vdb, fixture_rows(), "base")
    answer = _build(vdb, snapshot)
    assert answer["complete"] is True and answer["inserted"] == 16 and answer["built_rows"] == 16
    return snapshot


# -- 1. applies, rerun-safe, privileges ------------------------------------------------

def test_the_migration_is_rerun_safe_and_pins_the_mapper(vdb):
    # Re-applied as the ordered set from PR-L1 on (a later migration restates
    # its functions), exactly as the full-suite rerun does.
    for migration in MIGRATIONS[MIGRATIONS.index(VARIANTS_MIGRATION):]:
        vdb.psql(file=migration)
    assert vdb.psql("select public.catalog_variant_mapper_version()") == mapper.MAPPER_VERSION
    assert vdb.psql("select count(*) from pg_policies where tablename like 'catalog\\_variant%'") == "0"
    assert vdb.psql("select pg_get_constraintdef(oid) from pg_constraint "
                    "where conname = 'catalog_variant_coverage_level'") == \
        "CHECK ((level = ANY (ARRAY['register'::text, 'identity'::text, 'government_fields'::text])))"


@pytest.mark.parametrize("role", [RELEASE_RO, RO_ROLE])
def test_the_read_only_roles_read_everything_and_write_nothing(vdb, built, role):
    for relation in RELATIONS:
        assert vdb.psql(f"select has_table_privilege('{role}', 'public.{relation}', 'SELECT')") == "t"
        for write in ("INSERT", "UPDATE", "DELETE"):
            assert vdb.psql(f"select has_table_privilege('{role}', 'public.{relation}', '{write}')") == "f"
    for function in READS:
        assert vdb.psql(f"select has_function_privilege('{role}', 'public.{function}', 'EXECUTE')") == "t"
    assert vdb.psql(f"select has_function_privilege('{role}', 'public.{WRITE}', 'EXECUTE')") == "f"
    # The reads run AS the role (the production role is not in pg_read_all_data).
    page = json.loads(vdb.psql(f"set role {role}; select public.catalog_browser_manufacturers("
                               "null, null, null, null, null, 10, 0); reset role"))
    assert {"tozar": TOYOTA, "variants": 16} in page["items"]
    variants = json.loads(vdb.psql(f"set role {role}; select public.catalog_browser_variants("
                                   f"'{TOYOTA}', '4RUNNER', 2026, null, null, null, null, null, 5, 0); reset role"))
    assert variants["total"] == 5 and variants["items"][0]["coverage"]["identity"]["status"] == "enriched"
    by_id = {item["upstream_record_id"]: item for item in variants["items"]}
    assert by_id["37425"]["equipment"] == mapper.map_record(record(37425))["equipment"]
    assert not {"equipment_stated", "equipment_on", "equipment_sources"} & set(by_id["37425"])
    listing = json.loads(vdb.psql(f"set role {role}; select public.catalog_register_prunable_list(); reset role"))
    assert set(listing) == {"snapshots", "variant_builds", "digest"}
    assert json.loads(vdb.psql(f"set role {role}; select public.catalog_browser_facets('{TOYOTA}'); "
                               "reset role"))["year_min"] == 2022
    with pytest.raises(AssertionError) as refused:
        vdb.psql(f"set role {role}; select public.record_catalog_variants('{built['id']}', "
                 f"'{mapper.MAPPER_VERSION}', '[]'::jsonb)")
    assert "permission denied" in str(refused.value)


def test_the_equipment_key_list_is_the_mappers_and_round_trips(vdb):
    assert vdb.psql("select array_to_json(public.catalog_variant_equipment_keys())") == \
        json.dumps(list(mapper.EQUIPMENT_FIELDS), separators=(",", ":"))
    for row in fixture_rows():
        document = mapper.map_record(row)["equipment"]
        assert json.loads(vdb.psql(
            f"select public.catalog_variant_equipment(public.catalog_variant_equipment_mask({_json(document)}, false), "
            f"public.catalog_variant_equipment_mask({_json(document)}, true), "
            f"public.catalog_variant_equipment_source_texts({_json(document)}))")) == document


def test_the_production_release_role_is_production_shaped(vdb):
    assert vdb.psql(f"select rolcanlogin, rolbypassrls, rolsuper, pg_has_role('{RELEASE_RO}', "
                    f"'pg_read_all_data', 'MEMBER') from pg_roles where rolname = '{RELEASE_RO}'") == "t|t|f|f"


def test_browsers_and_other_roles_get_nothing(vdb):
    for role in ("anon", "authenticated", NOT_A_READER):
        for relation in RELATIONS:
            assert vdb.psql(f"select has_table_privilege('{role}', 'public.{relation}', 'SELECT')") == "f"
        for function in READS + (WRITE,):
            assert vdb.psql(f"select has_function_privilege('{role}', 'public.{function}', 'EXECUTE')") == "f"
    assert vdb.psql(f"select has_function_privilege('service_role', 'public.{WRITE}', 'EXECUTE')") == "t"
    for privilege in ("DELETE", "TRUNCATE"):
        assert vdb.psql(f"select has_table_privilege('service_role', 'public.catalog_variants', "
                        f"'{privilege}')") == "f"


# -- 2. the rows ----------------------------------------------------------------------------

def test_record_37425_is_stored_typed_with_every_rule(vdb, built):
    assert _variant(vdb, built, "37425", "tozar, kinuy_mishari, shnat_yitzur, degem_cd, sug_degem, "
                                        "vehicle_segment, mishkal_kolel, automatic_ind, sug_tkina_nm, "
                                        "sug_mamir_cd is null and sug_mamir_nm is null, madad_yarok, "
                                        "kamut_co2_city, norm_fuel_type, parse_issues") == \
        f"{TOYOTA}|4RUNNER|2026|{record(37425)['degem_cd']}|P|private|3100|1|אמריקאית|t|" \
        f"{record_value(37425, 'madad_yarok')}|" \
        f"{record_value(37425, 'kamut_CO2_city')}|petrol|[]"
    equipment = json.loads(_variant(vdb, built, "37425", "public.catalog_variant_equipment("
                                                         "equipment_stated, equipment_on, equipment_sources)"))
    assert equipment == mapper.map_record(record(37425))["equipment"]


def record(upstream: int) -> dict:
    (row,) = [r for r in fixture_rows() if r["_id"] == upstream]
    return row


def record_value(upstream: int, field: str):
    value = record(upstream)[field]
    return int(value) if isinstance(value, float) and value.is_integer() and field != "madad_yarok" else value


def test_the_identity_key_hash_and_archive_line_are_the_pythons(vdb, built):
    for index, row in enumerate(built["rows"]):
        candidate = json.loads(vdb.psql(
            "select to_jsonb(c) from public.catalog_candidate_variants c join public.catalog_raw_records r "
            f"on r.id = c.raw_record_id where r.snapshot_id='{built['id']}' "
            f"and r.upstream_record_id='{row['_id']}'"))
        key, line = _variant(vdb, built, str(row["_id"]), "variant_identity_key, archive_line").split("|")
        assert key == catalog_coverage.candidate_identity_key(candidate, row)
        assert int(line) == index + 1
        assert _variant(vdb, built, str(row["_id"]), "content_sha256") == vdb.psql(
            "select public.catalog_variant_content_sha256(payload) from public.catalog_raw_records "
            f"where snapshot_id='{built['id']}' and upstream_record_id='{row['_id']}'")


def test_a_rebuild_is_a_no_op(vdb, built):
    before = vdb.psql("select md5(string_agg(to_jsonb(v)::text, ',' order by v.id)) from public.catalog_variants v")
    ledger = vdb.psql("select md5(string_agg(to_jsonb(c)::text, ',' order by c.id)) "
                      "from public.catalog_variant_coverage c")
    answer = _build(vdb, built)
    assert answer["inserted"] == 0 and answer["ledger_written"] == 0 and answer["complete"] is True
    assert vdb.psql("select md5(string_agg(to_jsonb(v)::text, ',' order by v.id)) "
                    "from public.catalog_variants v") == before
    assert vdb.psql("select md5(string_agg(to_jsonb(c)::text, ',' order by c.id)) "
                    "from public.catalog_variant_coverage c") == ledger


def test_variants_are_append_only_and_a_new_mapper_version_writes_beside_them(vdb, built):
    for statement in (f"update public.catalog_variants set mishkal_kolel = 1 where snapshot_id='{built['id']}'",
                      f"delete from public.catalog_variants where snapshot_id='{built['id']}'"):
        with pytest.raises(AssertionError) as refused:
            vdb.psql(statement)
        assert "CATALOG_VARIANT_IMMUTABLE" in str(refused.value)
    old = vdb.psql("select md5(string_agg(to_jsonb(v)::text, ',' order by v.id)) from public.catalog_variants v "
                   f"where mapper_version = '{mapper.MAPPER_VERSION}'")
    assert "CATALOG_VARIANT_MAPPER_MISMATCH" in _refusal(
        vdb, f"select public.record_catalog_variants('{built['id']}', 'gov.wltp.variant-mapper.2', '[]'::jsonb)")
    vdb.psql("create or replace function public.catalog_variant_mapper_version() returns text language sql "
             "immutable set search_path = pg_catalog as $$ select 'gov.wltp.variant-mapper.2'::text $$")
    try:
        answer = _build(vdb, built, version="gov.wltp.variant-mapper.2")
        assert answer["inserted"] == 16 and answer["complete"] is True
    finally:
        vdb.psql(f"create or replace function public.catalog_variant_mapper_version() returns text language sql "
                 f"immutable set search_path = pg_catalog as $$ select '{mapper.MAPPER_VERSION}'::text $$")
    assert vdb.psql("select md5(string_agg(to_jsonb(v)::text, ',' order by v.id)) from public.catalog_variants v "
                    f"where mapper_version = '{mapper.MAPPER_VERSION}'") == old
    assert vdb.psql(f"select count(*) from public.catalog_variants where snapshot_id='{built['id']}' "
                    "and mapper_version='gov.wltp.variant-mapper.2'") == "16"


def test_bounded_batches_and_a_partial_build_is_not_served(vdb):
    rows = [dict(r, _id=r["_id"] + 700000, tozar="בדיקה") for r in fixture_rows()]
    snapshot = _snapshot(vdb, rows, "partial", marque="בדיקה")
    first = _build(vdb, snapshot, rows[:10])
    assert first["complete"] is False and first["built_rows"] == 10
    page = json.loads(vdb.psql("select public.catalog_browser_manufacturers(null,null,null,null,null,100,0)"))
    assert "בדיקה" not in {i["tozar"] for i in page["items"]}
    assert _build(vdb, snapshot, rows[10:])["complete"] is True
    page = json.loads(vdb.psql("select public.catalog_browser_manufacturers(null,null,null,null,null,100,0)"))
    assert {"tozar": "בדיקה", "variants": 16} in page["items"]
    too_many = [{"upstream_record_id": str(i)} for i in range(501)]
    assert "CATALOG_VARIANT_ROWS_INVALID" in _refusal(
        vdb, f"select public.record_catalog_variants('{snapshot['id']}', '{mapper.MAPPER_VERSION}', "
             f"{_json(too_many)})")
    assert "CATALOG_VARIANT_ROWS_INVALID" in _refusal(
        vdb, f"select public.record_catalog_variants('{snapshot['id']}', '{mapper.MAPPER_VERSION}', "
             f"{_json([{'upstream_record_id': 'not-in-this-snapshot'}])})")


def test_an_unknown_equipment_key_and_a_bad_parse_issue_are_refused(vdb):
    rows = [dict(record(37425), _id=880001, tozar="ציוד")]
    snapshot = _snapshot(vdb, rows, "equipment", marque="ציוד")
    bad = mapper.map_record(rows[0])
    bad["equipment"] = {"mazgan_ind": 1}
    assert "CATALOG_VARIANT_EQUIPMENT_KEY_UNKNOWN" in _refusal(
        vdb, f"select public.record_catalog_variants('{snapshot['id']}', '{mapper.MAPPER_VERSION}', "
             f"{_json([bad])})")
    bad = mapper.map_record(rows[0])
    bad["parse_issues"] = [{"field": "x", "reason": "guessed"}]
    assert "catalog_variants_parse_issues_check" in _refusal(
        vdb, f"select public.record_catalog_variants('{snapshot['id']}', '{mapper.MAPPER_VERSION}', "
             f"{_json([bad])})")


def test_an_unscoped_snapshot_is_not_built(vdb):
    rows = [dict(record(37425), _id=890001)]
    snapshot = _snapshot(vdb, rows, "unscoped", marque=None, archived=False)
    assert "CATALOG_VARIANT_SNAPSHOT_INELIGIBLE" in _refusal(
        vdb, f"select public.record_catalog_variants('{snapshot['id']}', '{mapper.MAPPER_VERSION}', '[]'::jsonb)")


# -- 3. retention -------------------------------------------------------------------------------

def test_a_prune_can_never_orphan_a_variant(vdb, built):
    # Directly: the raw records and the snapshot are held by the foreign keys.
    for statement in (
            "alter table public.catalog_raw_records disable trigger catalog_raw_records_append_only; "
            f"delete from public.catalog_raw_records where snapshot_id='{built['id']}'",
            "alter table public.catalog_source_snapshots disable trigger catalog_source_snapshots_append_only; "
            f"delete from public.catalog_source_snapshots where id='{built['id']}'"):
        with pytest.raises(AssertionError) as refused:
            vdb.psql(statement)
        assert "violates foreign key constraint" in str(refused.value)
    assert vdb.psql(f"select count(*) from public.catalog_variants where snapshot_id='{built['id']}'") == "32"


def _prunable(db) -> dict:
    return json.loads(db.psql("select public.catalog_register_prunable_list()"))


def _prune(db, listing: dict) -> dict:
    items = [row["snapshot_key"] for row in listing["snapshots"]] + [b["item"] for b in listing["variant_builds"]]
    return json.loads(_rpc_as_service(db, f"select public.prune_register_snapshots("
                                          f"{_text_array(items)}, '{listing['digest']}')"))


def _text_array(items: list[str]) -> str:
    return "array[" + ", ".join("'" + item.replace("'", "''") + "'" for item in items) + "]::text[]"


def _count(db, table: str, snapshot: dict) -> int:
    column = "id" if table == "catalog_source_snapshots" else "snapshot_id"
    return int(db.psql(f"select count(*) from public.{table} where {column}='{snapshot['id']}'"))


APPEND_ONLY_TRIGGERS = ("catalog_variants_append_only", "catalog_raw_records_append_only",
                        "catalog_source_snapshots_append_only", "catalog_candidate_variants_identity_immutable")


def test_a_superseded_snapshot_is_pruned_with_its_variants_and_referenced_ones_never_are(vdb):
    """Four activations of one tozar, each built while active. The two oldest
    are superseded: the unreferenced one is pruned WITH its variants and
    build; one named by a `register` ledger row is kept. A tozar whose current
    build is its third activation keeps it. Ledger rows at the variant levels
    that name the pruned snapshot stay exactly as they were (history)."""
    marque, other = "שימור", "נוכחי"
    row = dict(record(37425), _id=900001, tozar=marque)
    gone = dict(record(37439), _id=900002, tozar=marque)     # only the oldest states it
    snapshots = []
    for index in range(4):
        rows = [dict(row, madad_yarok=float(index))] + ([gone] if index == 0 else [])
        snapshots.append(_snapshot(vdb, rows, f"keep-{index}", marque=marque))
        _build(vdb, snapshots[-1])
    oldest, referenced = snapshots[0], snapshots[1]
    vdb.psql("insert into public.catalog_variant_coverage (variant_identity_key, level, status, last_run_id, "
             f"snapshot_key, content_sha256, vocabulary_version) values ('{'e' * 64}', 'register', 'enriched', "
             f"'{referenced['run_id']}', '{referenced['key']}', '{'0' * 64}', 'gov.wltp.vocabulary.2')")
    current = _snapshot(vdb, [dict(row, _id=900101, tozar=other)], "current-0", marque=other)
    _build(vdb, current)
    for index in (1, 2):   # two later activations, never built
        _snapshot(vdb, [dict(row, _id=900101, tozar=other, madad_yarok=float(index))], f"current-{index}",
                  marque=other)
    history_key = _variant(vdb, oldest, "900002", "variant_identity_key")
    history = vdb.psql(f"select to_jsonb(c) from public.catalog_variant_coverage c "
                       f"where variant_identity_key='{history_key}' order by level")
    assert oldest["key"] in history

    listing = _prunable(vdb)
    listed = {entry["snapshot_key"]: entry for entry in listing["snapshots"]}
    assert oldest["key"] in listed
    assert referenced["key"] not in listed and current["key"] not in listed
    assert not {s["key"] for s in snapshots[2:]} & set(listed)
    variant_bytes = int(vdb.psql(f"select sum(pg_column_size(v.*)) from public.catalog_variants v "
                                 f"where snapshot_id='{oldest['id']}'"))
    raw_bytes = int(vdb.psql(f"select sum(pg_column_size(r.*)) from public.catalog_raw_records r "
                             f"where snapshot_id='{oldest['id']}'"))
    assert listed[oldest["key"]]["estimated_bytes"] > raw_bytes + variant_bytes

    assert "CATALOG_PRUNE_DIGEST_MISMATCH" in _refusal(
        vdb, f"select public.prune_register_snapshots({_text_array([oldest['key']])}, '{'0' * 64}')")
    answer = _prune(vdb, listing)
    assert answer["variants"] >= 2 and answer["variant_builds"] >= 1 and answer["snapshots"] >= 1
    for table in ("catalog_variants", "catalog_variant_builds", "catalog_candidate_variants",
                  "catalog_raw_records", "catalog_source_snapshots"):
        assert _count(vdb, table, oldest) == 0, table
        assert _count(vdb, table, referenced) > 0 and _count(vdb, table, current) > 0, table
    # History: never repointed, never deleted.
    assert vdb.psql(f"select to_jsonb(c) from public.catalog_variant_coverage c "
                    f"where variant_identity_key='{history_key}' order by level") == history
    # The suspended triggers are back, and they refuse again.
    assert vdb.psql("select string_agg(distinct tgenabled::text, ',') from pg_trigger "
                    f"where tgname in ({', '.join(repr(t) for t in APPEND_ONLY_TRIGGERS)})") == "O"
    with pytest.raises(AssertionError) as refused:
        vdb.psql(f"delete from public.catalog_variants where snapshot_id='{referenced['id']}'")
    assert "CATALOG_VARIANT_IMMUTABLE" in str(refused.value)
    # Browsing is unchanged: the current build of each tozar.
    page = json.loads(vdb.psql(f"select public.catalog_browser_models('{marque}', null, null, null, null, null, "
                               "10, 0)"))
    assert page["total"] == 1 and page["items"][0]["variants"] == 1


def test_old_mapper_version_rows_are_pruned_once_the_current_build_is_complete(vdb):
    marque, older = "גרסה", "gov.wltp.variant-mapper.0"
    snapshot = _snapshot(vdb, [dict(record(37425), _id=930001, tozar=marque)], "mapper-old", marque=marque)
    vdb.psql("create or replace function public.catalog_variant_mapper_version() returns text language sql "
             f"immutable set search_path = pg_catalog as $$ select '{older}'::text $$")
    try:
        _build(vdb, snapshot, version=older)
    finally:
        vdb.psql(f"create or replace function public.catalog_variant_mapper_version() returns text language sql "
                 f"immutable set search_path = pg_catalog as $$ select '{mapper.MAPPER_VERSION}'::text $$")
    item = f"{snapshot['key']} {older}"
    assert item not in {b["item"] for b in _prunable(vdb)["variant_builds"]}   # no current build yet
    _build(vdb, snapshot)
    listing = _prunable(vdb)
    (entry,) = [b for b in listing["variant_builds"] if b["item"] == item]
    assert entry["variant_rows"] == 1 and entry["estimated_bytes"] > 0
    assert snapshot["key"] not in {s["snapshot_key"] for s in listing["snapshots"]}
    assert listing["digest"] == hashlib.sha256("".join(
        f"{k}\n" for k in sorted([s["snapshot_key"] for s in listing["snapshots"]]
                                 + [b["item"] for b in listing["variant_builds"]],
                                 key=lambda k: k.encode())).encode()).hexdigest()
    # The operator's read-only listing (register-retention.sh) names the same
    # items and digest the prune checks.
    script = (Path(__file__).resolve().parents[1] / "scripts/ops/register-retention.sh").read_text()
    lines = vdb.psql(script.split('LIST_SQL="', 1)[1].split('"\n', 1)[0]).splitlines()
    assert f"DIGEST {listing['digest']}" in lines
    assert f"PRUNABLE-VARIANTS {snapshot['key']} mapper_version={older} rows=1 " \
           f"estimated_bytes={entry['estimated_bytes']}" in lines
    _prune(vdb, listing)
    assert vdb.psql(f"select mapper_version, count(*) from public.catalog_variants "
                    f"where snapshot_id='{snapshot['id']}' group by 1") == f"{mapper.MAPPER_VERSION}|1"
    assert vdb.psql(f"select string_agg(mapper_version, ',') from public.catalog_variant_builds "
                    f"where snapshot_id='{snapshot['id']}'") == mapper.MAPPER_VERSION


def test_only_the_active_snapshot_of_a_tozar_is_built(vdb):
    marque = "מוחלף"
    rows = [dict(record(37425), _id=940001, tozar=marque)]
    superseded = _snapshot(vdb, rows, "superseded-a", marque=marque)
    active = _snapshot(vdb, [dict(rows[0], madad_yarok=1.0)], "superseded-b", marque=marque)
    assert "CATALOG_VARIANT_SNAPSHOT_SUPERSEDED" in _refusal(
        vdb, f"select public.record_catalog_variants('{superseded['id']}', '{mapper.MAPPER_VERSION}', '[]'::jsonb)")
    assert _count(vdb, "catalog_variant_builds", superseded) == 0
    assert _build(vdb, active)["complete"] is True


def test_a_complete_build_re_measures_its_captured_units(vdb, built):
    group = vdb.psql("insert into public.catalog_register_capture_groups (register_version, requested_by, "
                     f"expected_rows) values ('{'c' * 64}', '{uuid.uuid4()}', 16) returning id")
    vdb.psql("insert into public.catalog_register_capture_units (group_id, register_version, tozar, expected_rows, "
             "status, snapshot_id, snapshot_key, api_total, captured_rows, count_verified, measured_bytes, "
             f"measurement_method) values ('{group}', '{'c' * 64}', '{TOYOTA}', 16, 'captured', '{built['id']}', "
             f"'{built['key']}', 16, 16, true, 1, 'pg_column_size(raw_records+candidates)')")
    _build(vdb, built)
    measured, method = vdb.psql(f"select measured_bytes, measurement_method from public.catalog_register_capture_units "
                                f"where group_id='{group}'").split("|")
    assert method == "pg_column_size(raw_records+candidates+variants+ledger)"
    assert int(measured) == int(vdb.psql(f"select public.catalog_register_measured_bytes('{built['id']}')"))
    assert int(measured) > int(vdb.psql(f"select public.catalog_register_snapshot_bytes('{built['id']}')")) > int(
        vdb.psql(f"select sum(pg_column_size(r.*)) from public.catalog_raw_records r where snapshot_id='{built['id']}'"))


# -- 4. the coverage ledger ------------------------------------------------------------------------

def test_the_ledger_has_both_levels_for_every_key_and_register_rows_are_untouched(vdb, built):
    keys = vdb.psql(f"select count(distinct variant_identity_key) from public.catalog_variants "
                    f"where snapshot_id='{built['id']}'")
    assert vdb.psql(f"select level, count(*), min(status), max(status) from public.catalog_variant_coverage "
                    f"where snapshot_key='{built['key']}' group by level order by level").splitlines() == [
        f"government_fields|{keys}|enriched|enriched", f"identity|{keys}|enriched|enriched"]
    key = _variant(vdb, built, "37425", "variant_identity_key")
    vdb.psql("insert into public.catalog_variant_coverage (variant_identity_key, level, status, last_run_id, "
             "snapshot_key, content_sha256, vocabulary_version) values "
             f"('{key}', 'register', 'pending', '{built['run_id']}', 'cs1.register', '{'0' * 64}', "
             "'gov.wltp.vocabulary.2') on conflict do nothing")
    before = vdb.psql(f"select to_jsonb(c) from public.catalog_variant_coverage c "
                      f"where variant_identity_key='{key}' and level='register'")
    vdb.psql(f"delete from public.catalog_variant_coverage where variant_identity_key='{key}' "
             "and level in ('identity', 'government_fields')")
    _build(vdb, built)
    assert vdb.psql(f"select to_jsonb(c) from public.catalog_variant_coverage c "
                    f"where variant_identity_key='{key}' and level='register'") == before
    assert vdb.psql(f"select count(*) from public.catalog_variant_coverage where variant_identity_key='{key}' "
                    "and level in ('identity', 'government_fields') and status='enriched'") == "2"


def test_a_changed_content_from_a_newer_snapshot_replaces_and_an_older_one_never_takes_it_back(vdb):
    marque = "תוכן"
    original = dict(record(37425), _id=910001, tozar=marque)
    old = _snapshot(vdb, [original], "content-old", marque=marque)
    _build(vdb, old)
    key = _variant(vdb, old, "910001", "variant_identity_key")
    first = vdb.psql(f"select content_sha256, snapshot_key from public.catalog_variant_coverage "
                     f"where variant_identity_key='{key}' and level='identity'")
    changed = dict(original, madad_yarok=999.0)
    new = _snapshot(vdb, [changed], "content-new", marque=marque)
    _build(vdb, new)
    content, snapshot_key = vdb.psql(f"select content_sha256, snapshot_key from public.catalog_variant_coverage "
                                     f"where variant_identity_key='{key}' and level='identity'").split("|")
    assert _variant(vdb, new, "910001", "variant_identity_key") == key
    assert (content, snapshot_key) != tuple(first.split("|")) and snapshot_key == new["key"]
    # The older snapshot is superseded: it is never built again, so it can
    # never take the row back.
    assert "CATALOG_VARIANT_SNAPSHOT_SUPERSEDED" in _refusal(
        vdb, f"select public.record_catalog_variants('{old['id']}', '{mapper.MAPPER_VERSION}', '[]'::jsonb)")
    assert vdb.psql(f"select snapshot_key from public.catalog_variant_coverage "
                    f"where variant_identity_key='{key}' and level='identity'") == new["key"]


def test_a_key_collision_fails_the_key_at_both_levels(vdb):
    marque = "התנגשות"
    one = dict(record(37425), _id=920001, tozar=marque)
    two = dict(one, _id=920002, madad_yarok=123.0)
    snapshot = _snapshot(vdb, [one, two], "collision", marque=marque)
    _build(vdb, snapshot, [one])
    _build(vdb, snapshot, [two])
    key = _variant(vdb, snapshot, "920001", "variant_identity_key")
    assert _variant(vdb, snapshot, "920002", "variant_identity_key") == key
    rows = vdb.psql(f"select level, status, reason_code from public.catalog_variant_coverage "
                    f"where variant_identity_key='{key}' order by level").splitlines()
    assert rows == ["government_fields|failed|CATALOG_COVERAGE_KEY_COLLISION",
                    "identity|failed|CATALOG_COVERAGE_KEY_COLLISION"]


# -- 5. the discovery tree -------------------------------------------------------------------------

def test_the_tree_is_paged_and_stable(vdb, built):
    models = json.loads(vdb.psql(f"select public.catalog_browser_models('{TOYOTA}', null, null, null, null, null, "
                                 "1, 1)"))
    assert models["total"] == 2 and [m["kinuy_mishari"] for m in models["items"]] == ["RAV4"]
    assert models["items"][0] == {"kinuy_mishari": "RAV4", "variants": 11, "year_min": 2022, "year_max": 2026}
    years = json.loads(vdb.psql(f"select public.catalog_browser_years('{TOYOTA}', 'RAV4', null, 2024, null, "
                                "null, null, 10, 0)"))
    assert [y["shnat_yitzur"] for y in years["items"]] == [2026, 2025, 2024]
    pages = [json.loads(vdb.psql(f"select public.catalog_browser_variants('{TOYOTA}', '4RUNNER', 2026, null, "
                                 f"null, null, null, null, 2, {offset})")) for offset in (0, 2, 4)]
    ids = [i["upstream_record_id"] for page in pages for i in page["items"]]
    assert sorted(ids) == ["37309", "37345", "37350", "37425", "37439"] and len(set(ids)) == 5
    assert "snapshot_id" not in pages[0]["items"][0]
    facets = json.loads(vdb.psql(f"select public.catalog_browser_facets('{TOYOTA}')"))
    assert facets["segments"] == [{"value": "private", "variants": 16}]
    assert facets["fuels"][0]["delek_cd"] == 1 and facets["year_min"] == 2022
    for bad in ("catalog_browser_manufacturers(null,null,null,null,null,0,0)",
                "catalog_browser_manufacturers(null,null,null,null,null,101,0)",
                "catalog_browser_models(null,null,null,null,null,null,10,0)"):
        assert "CATALOG_BROWSER_QUERY_INVALID" in _refusal(vdb, f"select public.{bad}")


# -- 6. L1-6: the size of a variant row -------------------------------------------------------------

def test_bytes_per_variant_row_and_the_full_register_projection(vdb, capsys):
    """5,000 rows derived from the committed fixtures (ids and a few values
    varied so every row is distinct), built through the real RPC; measured
    with pg_total_relation_size (table + TOAST + indexes) and pg_column_size."""
    marque, count = "מדידה", 5000
    base = fixture_rows()
    rows = []
    for index in range(count):
        row = copy.deepcopy(base[index % len(base)])
        row.update(_id=1_000_000 + index, tozar=marque, kinuy_mishari=f"{row['kinuy_mishari']}-{index % 40}",
                   degem_cd=row["degem_cd"] + index)
        rows.append(row)
    snapshot = _bulk_snapshot(vdb, rows, marque)
    sizes_before = _sizes(vdb)
    for start in range(0, count, mapper.BUILD_BATCH_ROWS):
        _build(vdb, snapshot, rows[start:start + mapper.BUILD_BATCH_ROWS])
    vdb.psql("vacuum analyze public.catalog_variants")
    vdb.psql("vacuum analyze public.catalog_variant_coverage")
    sizes_after = _sizes(vdb)
    variants = (sizes_after["variants"] - sizes_before["variants"]) / count
    ledger = (sizes_after["ledger"] - sizes_before["ledger"]) / count
    heap = float(vdb.psql(f"select avg(pg_column_size(v.*)) from public.catalog_variants v "
                          f"where snapshot_id='{snapshot['id']}'"))
    equipment = float(vdb.psql(f"select avg(pg_column_size(equipment_stated) + pg_column_size(equipment_on) "
                               f"+ coalesce(pg_column_size(equipment_sources), 0)) from public.catalog_variants "
                               f"where snapshot_id='{snapshot['id']}'"))
    projection_mb = (variants + ledger) * 100_000 / 1_000_000
    with capsys.disabled():
        print(f"\nL1-6 bytes per variant: row heap {heap:.0f} (equipment masks + sources {equipment:.0f}); "
              f"table+toast+indexes {variants:.0f}; ledger (2 levels, table+indexes) {ledger:.0f}; "
              f"projection for 100,000 rows {variants * 100_000 / 1e6:.1f} MB variants + "
              f"{ledger * 100_000 / 1e6:.1f} MB ledger = {projection_mb:.1f} MB")
    # PR-L1b: the equipment document became two masks and a five-text array;
    # the bound is the measurement (975 B/row, table + TOAST + indexes; the
    # ledger 996) + 15%, so a regression fails here.
    assert variants <= VARIANT_ROW_BYTES * 1.15 and ledger <= LEDGER_ROW_BYTES * 1.15


#: Measured by the test above (PostgreSQL 16) after PR-L1b's compaction.
VARIANT_ROW_BYTES, LEDGER_ROW_BYTES = 975, 996


def _sizes(db) -> dict[str, int]:
    variants, ledger = db.psql("select pg_total_relation_size('public.catalog_variants'), "
                               "pg_total_relation_size('public.catalog_variant_coverage')").split("|")
    return {"variants": int(variants), "ledger": int(ledger)}


def _bulk_snapshot(db, rows: list[dict], marque: str) -> dict:
    """The same shape `_snapshot` writes, in bulk SQL (5,000 RPC round trips
    would take minutes): an active scoped snapshot, its raw records and one
    candidate per row from PR-V's reading."""
    _user, _project, conversation = _ws_world(db)
    run_id, args = _wsp_capture_run(db, conversation)
    payload = json.loads(_catalog_snapshot_json(f"var-bulk-{uuid.uuid4().hex[:8]}", declared=len(rows)))
    payload["retrieval_metadata"] = _wsp_metadata(marque, count=len(rows))
    snapshot = _rpc_as_service(db, f"select id from public.record_catalog_snapshot_guarded({args}, "
                                   f"{_json(payload)})")
    records, candidates = [], []
    for index, row in enumerate(rows):
        reading = read_wltp_record(row)
        records.append({"upstream_record_id": str(row["_id"]), "payload": row,
                        "record_key": "cr1." + hashlib.md5(f"{snapshot}:r{index}".encode()).hexdigest(),
                        "source_locator": {"page_index": 0, "page_number": 1, "page_offset": index,
                                           "capture_index": index}})
        candidates.append({"upstream_record_id": str(row["_id"]), "manufacturer": reading.manufacturer,
                           "commercial_model": reading.commercial_model, "year": reading.model_year_start,
                           "code": reading.official_model_code, "trim": reading.trim,
                           "dimensions": reading.identity_dimensions, "status": reading.status,
                           "candidate_key": "cc1." + hashlib.md5(f"{snapshot}:c{index}".encode()).hexdigest()})
    _psql_file(db, f"""
      insert into public.catalog_raw_records (snapshot_id, resource_id, upstream_record_id, payload,
                                              payload_sha256, record_key, source_locator)
      select '{snapshot}', '{RESOURCE}', r.upstream_record_id, r.payload,
             encode(sha256(convert_to(r.payload::text, 'UTF8')), 'hex'), r.record_key, r.source_locator
        from jsonb_to_recordset({_json(records)}) as r(upstream_record_id text, payload jsonb, record_key text,
                                                      source_locator jsonb);
      insert into public.catalog_candidate_variants (snapshot_id, raw_record_id, manufacturer, commercial_model,
             model_year_start, model_year_end, official_model_code, trim, identity_dimensions, status,
             candidate_key)
      select '{snapshot}', r.id, c.manufacturer, c.commercial_model, c.year, c.year, c.code, c.trim,
             c.dimensions, c.status, c.candidate_key
        from jsonb_to_recordset({_json(candidates)}) as c(upstream_record_id text, manufacturer text,
             commercial_model text, year integer, code text, trim text, dimensions jsonb, status text,
             candidate_key text)
        join public.catalog_raw_records r on r.snapshot_id = '{snapshot}'
                                         and r.upstream_record_id = c.upstream_record_id;
      update public.catalog_source_snapshots set stored_record_count = {len(rows)} where id = '{snapshot}';""")
    _rpc_as_service(db, f"select id from public.activate_catalog_snapshot_guarded({args}, "
                        f"'{json.dumps({'snapshot_id': snapshot})}'::jsonb)")
    return {"id": snapshot, "rows": rows}


# -- 7. the tree reads by snapshot, never the whole table (>= 100,000 rows) --------------------------

def test_the_tree_for_one_tozar_uses_an_index_at_100k_rows(vdb, built):
    """200 tozars x 500 variants, each its own current build. Every browser
    read for one tozar -- custom and generic plans alike (seven calls in one
    session) -- reads catalog_variants through an index, never a Seq Scan
    (auto_explain logs every nested statement's plan, analysed: the facets'
    all-tozars branch is planned but gated by a one-time filter, never run)."""
    tozars, per = 200, 500
    template = built["id"]
    scopes = [{"t": t, "tozar": f"אינדקס-{t}", "metadata": _wsp_metadata(f"אינדקס-{t}", count=per)}
              for t in range(1, tozars + 1)]
    _psql_file(vdb, f"""
      create temporary table bulk as
        select gen_random_uuid() as id, b.t, b.tozar, b.metadata
          from jsonb_to_recordset({_json(scopes)}) as b(t integer, tozar text, metadata jsonb);
      insert into public.catalog_source_snapshots (id, created_by_run_id, source_family, trust_state, resource_id,
             upstream_version, upstream_version_kind, content_sha256, retrieved_at, retrieval_metadata,
             declared_record_count, stored_record_count, validation_state, activated_at, snapshot_key)
      select b.id, s.created_by_run_id, s.source_family, s.trust_state, s.resource_id, s.upstream_version,
             s.upstream_version_kind, encode(sha256(convert_to('bulk-' || b.t, 'UTF8')), 'hex'), s.retrieved_at,
             b.metadata,
             {per}, {per}, 'complete', now(), 'cs1.' || md5('bulk-' || b.t)
        from bulk b, public.catalog_source_snapshots s where s.id = '{template}';
      insert into public.catalog_raw_records (snapshot_id, resource_id, upstream_record_id, payload, payload_sha256,
                                              record_key, source_locator)
      select b.id, '{RESOURCE}', (b.t * 1000 + r)::text, jsonb_build_object('_id', b.t * 1000 + r),
             encode(sha256(convert_to((b.t * 1000 + r)::text, 'UTF8')), 'hex'), 'cr1.' || md5(b.t || ':' || r),
             jsonb_build_object('page_index', 0, 'page_number', 1, 'page_offset', r, 'capture_index', r)
        from bulk b, generate_series(1, {per}) r;
      insert into public.catalog_variant_builds (snapshot_id, mapper_version, snapshot_key, tozar, activated_at,
                                                 expected_rows, built_rows, completed_at)
      select b.id, '{mapper.MAPPER_VERSION}', 'cs1.' || md5('bulk-' || b.t), b.tozar, now(), {per}, {per}, now() from bulk b;
      insert into public.catalog_variants (snapshot_id, snapshot_key, upstream_record_id, content_sha256,
             mapper_version, vehicle_segment, tozar, kinuy_mishari, shnat_yitzur, delek_cd, merkav, degem_nm)
      select b.id, 'cs1.' || md5('bulk-' || b.t), (b.t * 1000 + r)::text, md5(r::text) || md5(b.t::text), '{mapper.MAPPER_VERSION}',
             'private', b.tozar, 'MODEL-' || (r % 20), 2010 + r % 15, 1, 'SUV', 'D' || r
        from bulk b, generate_series(1, {per}) r;
      analyze public.catalog_variants; analyze public.catalog_variant_builds;""")
    assert int(vdb.psql("select count(*) from public.catalog_variants")) >= tozars * per
    tozar = "אינדקס-77"
    calls = [f"public.catalog_browser_models('{tozar}', null, null, null, null, null, 50, 0)",
             f"public.catalog_browser_years('{tozar}', 'MODEL-3', null, null, null, null, null, 50, 0)",
             f"public.catalog_browser_variants('{tozar}', 'MODEL-3', 2013, null, null, null, null, null, 50, 0)",
             f"public.catalog_browser_facets('{tozar}')"]
    for call in calls:
        plans = _explained(vdb, call, times=7)
        scans = [line for line in plans.splitlines() if "catalog_variants " in line and "Scan" in line
                 and "never executed" not in line]
        assert scans, (call, plans)
        assert not [line for line in scans if "Seq Scan" in line], (call, plans)
        assert json.loads(vdb.psql(f"select {call}"))


def _explained(db, call: str, *, times: int) -> str:
    """Every nested statement's plan, as auto_explain logs it to the client."""
    import subprocess

    script = ("load 'auto_explain'; set auto_explain.log_min_duration = 0; "
              "set auto_explain.log_nested_statements = on; set auto_explain.log_analyze = on; "
              "set auto_explain.log_timing = off; set auto_explain.log_level = notice; "
              "set client_min_messages = notice; " + f"select {call}; " * times)
    result = subprocess.run(["psql", "-h", db.dir, "-p", db.port, "-U", "postgres", "-d", "milo", "-X", "-q",
                             "-v", "ON_ERROR_STOP=1", "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stderr

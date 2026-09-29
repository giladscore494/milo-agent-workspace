"""PR-D1: migration 20260929000100 (register capture) on ephemeral PostgreSQL.

Applied on top of the 45 migrations before it, into a database that already
has the read-only role, it must: create its relations; grant the read-only
role SELECT and the read functions EXECUTE, and nothing that writes; keep
every write service-path only; compute the directory version byte for byte
as the Python does; refuse a capture over the capacity threshold with its
exact numbers and write nothing; refuse a group over the cap; answer a second
request for the same (tozar, version) with the existing one; refuse a
captured unit without an active, count-verified, archived snapshot; map every
archived row to its line; and never select a referenced snapshot for pruning,
refusing any list whose digest is not the current one.
"""

from __future__ import annotations

import json
import re
import uuid

import pytest

from backend.catalog.government.directory import DirectoryUnit, register_version
from backend.catalog.register.retention import prune_digest
from tests.test_migrations_postgres import (
    BASELINE,
    MIGRATIONS,
    SEED_LEGACY_ROWS,
    SUPABASE_AUTH_SHIM,
    EphemeralPostgres,
    _catalog_record_json,
    _catalog_snapshot_json,
    _require_pg_bin,
    _rpc_as_service,
    _ws_world,
    _wsp_capture_run,
    _wsp_metadata,
)

REGISTER_PG_PORT = "54997"
RESOURCE = "142afde2-6228-49f9-8a29-9b6c3a0cbe40"
TOYOTA = "טויוטה"
LEXUS = "לקסוס"
RO_ROLE = "supabase_read_only_user"
OTHER_READER = "milo_other_reader_test"
NOT_A_READER = "milo_not_a_reader_test"
TABLES = ("catalog_register_directory_versions", "catalog_register_directory_units",
          "catalog_register_capture_groups", "catalog_register_capture_units",
          "catalog_register_snapshot_archives", "catalog_register_archive_lines")
READ_FUNCTIONS = ("catalog_register_version(text,jsonb)", "catalog_register_database_bytes()",
                  "catalog_register_snapshot_bytes(uuid)", "catalog_register_prunable_snapshots()",
                  "catalog_register_prune_digest(text[])", "catalog_register_coverage()",
                  "catalog_register_latest_directory()", "catalog_register_unit_states()",
                  "catalog_register_prunable_list()")
WRITE_FUNCTIONS = ("record_register_directory(text,timestamptz,jsonb)",
                   "request_register_capture(text,text[],uuid,integer,bigint,integer,integer)",
                   "record_register_capture_trigger(uuid,uuid,text,text)",
                   "record_register_unit_status(uuid,text,integer,text,uuid,text,text,uuid,integer,integer,boolean)",
                   "record_register_snapshot_archive(uuid,text,integer,text,uuid,text,bigint,text,integer)",
                   "prune_register_snapshots(text[],text)")


def _register_migration():
    (migration,) = [m for m in MIGRATIONS if m.name.startswith("20260929000100")]
    return migration


@pytest.fixture(scope="module")
def regdb():
    server = EphemeralPostgres(_require_pg_bin(), port=REGISTER_PG_PORT)
    server.start()
    try:
        server.create_database()
        server.psql(file=BASELINE)
        server.psql(sql=SEED_LEGACY_ROWS)
        server.psql(sql=SUPABASE_AUTH_SHIM)
        # The read-only roles exist BEFORE the migration, as in production.
        server.psql(f"create role {RO_ROLE} login bypassrls; grant pg_read_all_data to {RO_ROLE}; "
                    f"create role {OTHER_READER} login bypassrls; grant pg_read_all_data to {OTHER_READER}; "
                    f"create role {NOT_A_READER} login bypassrls")
        before = [m for m in MIGRATIONS if m != _register_migration()]
        assert len(before) == 45 and MIGRATIONS[-1] == _register_migration()
        for migration in before:
            server.psql(file=migration)
        assert server.psql("select to_regclass('public.catalog_register_capture_units') is null") == "t"
        server.psql(file=_register_migration())
        yield server
    finally:
        server.stop()


def _refusal(db, sql: str) -> str:
    with pytest.raises(AssertionError) as refused:
        _rpc_as_service(db, sql)
    return str(refused.value)


def _directory(db, units: dict[str, int]) -> str:
    body = json.dumps([{"tozar": t, "expected_rows": n} for t, n in units.items()], ensure_ascii=False)
    answer = json.loads(_rpc_as_service(
        db, f"select public.record_register_directory('{RESOURCE}', now(), $j${body}$j$::jsonb)"))
    return answer["version"]["register_version"]


def _request(db, version: str, tozars: list[str], *, cap: int = 10000, limit: int = 10**12,
             per_row: int = 3500) -> str:
    array = "array[" + ",".join(f"$t${t}$t$" for t in tozars) + "]::text[]"
    return f"select public.request_register_capture('{version}', {array}, '{uuid.uuid4()}', {cap}, {limit}, {per_row}, 900)"


def _scoped_snapshot(db, args: str, label: str, count: int, marque: str = TOYOTA) -> tuple[str, str]:
    payload = json.loads(_catalog_snapshot_json(f"reg-{label}", declared=count))
    payload["retrieval_metadata"] = _wsp_metadata(marque, count=count)
    snapshot = _rpc_as_service(db, "select id from public.record_catalog_snapshot_guarded("
                                   f"{args}, $j${json.dumps(payload)}$j$::jsonb)")
    for index in range(count):
        record = _catalog_record_json(snapshot, f"reg-{label}-rec-{index}", upstream=str(5000 + index),
                                      payload={"_id": 5000 + index, "tozar": marque},
                                      locator={"page_index": 0, "page_number": 1, "page_offset": 0,
                                               "capture_index": index})
        _rpc_as_service(db, "select id from public.record_catalog_raw_record_guarded("
                            f"{args}, $j${record}$j$::jsonb)")
    _rpc_as_service(db, f"select id from public.activate_catalog_snapshot_guarded({args},"
                        f"'{json.dumps({'snapshot_id': snapshot})}'::jsonb)")
    key = db.psql(f"select snapshot_key from public.catalog_source_snapshots where id='{snapshot}'")
    return snapshot, key


# -- 1. applies on 45, grants ---------------------------------------------------

def test_the_migration_applies_on_45_and_is_rerun_safe(regdb):
    regdb.psql(file=_register_migration())
    for table in TABLES:
        assert regdb.psql(f"select to_regclass('public.{table}') is not null") == "t"
    assert regdb.psql("select count(*) from pg_policies where tablename like 'catalog\\_register%'") == "0"


@pytest.mark.parametrize("role", [RO_ROLE, OTHER_READER])
def test_the_read_only_role_may_read_everything_and_write_nothing(regdb, role):
    for table in TABLES:
        assert regdb.psql(f"select has_table_privilege('{role}', 'public.{table}', 'SELECT')") == "t"
        for write in ("INSERT", "UPDATE", "DELETE"):
            assert regdb.psql(f"select has_table_privilege('{role}', 'public.{table}', '{write}')") == "f"
    for function in READ_FUNCTIONS:
        assert regdb.psql(f"select has_function_privilege('{role}', 'public.{function}', 'EXECUTE')") == "t"
    for function in WRITE_FUNCTIONS:
        assert regdb.psql(f"select has_function_privilege('{role}', 'public.{function}', 'EXECUTE')") == "f"
    coverage = json.loads(regdb.psql(f"set role {role}; select public.catalog_register_coverage(); reset role"))
    assert set(coverage) == {"register_version", "units_total", "rows_total", "units_captured", "rows_captured",
                             "unverified_snapshots", "database_bytes"}


def test_browsers_and_other_roles_get_nothing(regdb):
    for role in ("anon", "authenticated", NOT_A_READER):
        for table in TABLES:
            assert regdb.psql(f"select has_table_privilege('{role}', 'public.{table}', 'SELECT')") == "f"
        for function in READ_FUNCTIONS + WRITE_FUNCTIONS:
            assert regdb.psql(f"select has_function_privilege('{role}', 'public.{function}', 'EXECUTE')") == "f"
    for function in READ_FUNCTIONS + WRITE_FUNCTIONS:
        assert regdb.psql(f"select has_function_privilege('service_role', 'public.{function}', 'EXECUTE')") == "t"
    for table in TABLES[:-1]:
        for privilege in ("DELETE", "TRUNCATE"):
            assert regdb.psql(f"select has_table_privilege('service_role', 'public.{table}', '{privilege}')") == "f"


# -- 2. the directory -------------------------------------------------------------

def test_the_database_version_is_the_python_version(regdb):
    units = {TOYOTA: 28, LEXUS: 5000, "טויוטה ": 3}
    body = json.dumps([{"tozar": t, "expected_rows": n} for t, n in units.items()], ensure_ascii=False)
    database = regdb.psql(f"select public.catalog_register_version('{RESOURCE}', $j${body}$j$::jsonb)")
    assert database == register_version(RESOURCE, [DirectoryUnit(t, n) for t, n in units.items()])


def test_a_directory_version_is_append_only_and_new_only_on_change(regdb):
    units = {f"dir-{uuid.uuid4().hex[:6]}": 3}
    version = _directory(regdb, units)
    body = json.dumps([{"tozar": t, "expected_rows": n} for t, n in units.items()])
    again = json.loads(_rpc_as_service(
        regdb, f"select public.record_register_directory('{RESOURCE}', now(), $j${body}$j$::jsonb)"))
    assert again["decision"] == "unchanged" and again["version"]["register_version"] == version
    with pytest.raises(AssertionError):
        regdb.psql(f"delete from public.catalog_register_directory_versions where register_version='{version}'")


# -- 3. requests: capacity, cap, idempotency ------------------------------------------

def test_the_capacity_guard_states_its_numbers_and_writes_nothing(regdb):
    version = _directory(regdb, {TOYOTA: 28, LEXUS: 1000})
    groups = regdb.psql("select count(*) from public.catalog_register_capture_groups")
    text = _refusal(regdb, _request(regdb, version, [LEXUS], limit=1000, per_row=3500))
    match = re.search(r"CATALOG_CAPACITY_THRESHOLD_EXCEEDED: current=(\d+) projected=(\d+) limit=(\d+)", text)
    assert match, text
    current, projected, limit = (int(v) for v in match.groups())
    assert projected == current + 1000 * 3500 and limit == 1000
    assert abs(current - int(regdb.psql("select pg_database_size(current_database())"))) < 10_000_000
    assert regdb.psql("select count(*) from public.catalog_register_capture_groups") == groups


def test_a_group_over_the_cap_is_refused_and_a_large_tozar_alone_is_not(regdb):
    big, small = f"big-{uuid.uuid4().hex[:6]}", f"small-{uuid.uuid4().hex[:6]}"
    version = _directory(regdb, {big: 10_001, small: 5})
    assert "CATALOG_REGISTER_GROUP_TOO_LARGE" in _refusal(regdb, _request(regdb, version, [big, small]))
    assert json.loads(_rpc_as_service(regdb, _request(regdb, version, [big])))["decision"] == "claimed"


def test_a_second_request_for_the_same_tozar_and_version_starts_nothing(regdb):
    tozar = f"idem-{uuid.uuid4().hex[:6]}"
    version = _directory(regdb, {tozar: 5})
    first = json.loads(_rpc_as_service(regdb, _request(regdb, version, [tozar])))
    again = json.loads(_rpc_as_service(regdb, _request(regdb, version, [tozar])))
    assert first["decision"] == "claimed" and again["decision"] == "existing"
    assert again["units"][0]["group_id"] == first["group"]["id"]
    assert "CATALOG_REGISTER_VERSION_STALE" in _refusal(regdb, _request(regdb, "0" * 64, [tozar]))


# -- 4. capture: archive before "captured"; line mapping ------------------------------

def test_a_captured_unit_needs_an_active_counted_archived_snapshot(regdb):
    _user, _project, conversation = _ws_world(regdb)
    run_id, args = _wsp_capture_run(regdb, conversation)
    version = _directory(regdb, {TOYOTA: 3})
    claim = json.loads(_rpc_as_service(regdb, _request(regdb, version, [TOYOTA])))
    group, unit = claim["group"]["id"], claim["units"][0]["id"]
    _rpc_as_service(regdb, f"select public.record_register_capture_trigger('{group}', '{run_id}', 'claimed', null)")
    snapshot, key = _scoped_snapshot(regdb, args, f"cap-{uuid.uuid4().hex[:6]}", 3)
    status = (f"select public.record_register_unit_status({args}, '{unit}', 'captured', null, '{snapshot}', "
              "3, 3, true)")
    assert "CATALOG_REGISTER_CAPTURE_UNVERIFIED" in _refusal(regdb, status)
    uri = f"gs://milo-test-archive/register/{RESOURCE}/{'a' * 16}/{key}.jsonl.gz"
    archive = f"select public.record_register_snapshot_archive({args}, '{snapshot}', '{uri}', 100, '{'b' * 64}', %d)"
    assert "CATALOG_CAPTURE_COUNT_MISMATCH" in _refusal(regdb, archive % 2)
    _rpc_as_service(regdb, archive % 3)
    assert json.loads(_rpc_as_service(regdb, archive % 3))["sha256"] == "b" * 64  # idempotent
    conflict = archive.replace("'" + "b" * 64 + "'", "'" + "c" * 64 + "'") % 3
    assert "CATALOG_ARCHIVE_CONFLICT" in _refusal(regdb, conflict)
    # A mismatched count is refused even with the archive.
    assert "CATALOG_REGISTER_CAPTURE_UNVERIFIED" in _refusal(regdb, status.replace("3, 3, true", "3, 2, true"))
    captured = json.loads(_rpc_as_service(regdb, status))
    assert captured["status"] == "captured" and captured["count_verified"] is True
    assert captured["measured_bytes"] > 0
    assert captured["measurement_method"] == "pg_column_size(raw_records+candidates)"
    lines = regdb.psql(f"select string_agg((r.source_locator->>'capture_index') || '>' || l.archive_line, ',' "
                       f"order by l.archive_line) from public.catalog_register_archive_lines l "
                       f"join public.catalog_raw_records r on r.id = l.raw_record_id where l.snapshot_key = '{key}'")
    assert lines == "0>1,1>2,2>3"
    coverage = json.loads(regdb.psql("select public.catalog_register_coverage()"))
    assert coverage["units_captured"] >= 1


# -- 5. retention -----------------------------------------------------------------------

def test_prune_never_selects_the_kept_or_referenced_and_refuses_a_stale_digest(regdb):
    marque = f"פרון-{uuid.uuid4().hex[:4]}"
    _user, _project, conversation = _ws_world(regdb)
    run_id, args = _wsp_capture_run(regdb, conversation)
    snaps = [_scoped_snapshot(regdb, args, f"prune-{marque}-{i}", 1, marque) for i in range(5)]
    # While the writer run is live, nothing it wrote is prunable.
    prunable = lambda: regdb.psql(  # noqa: E731
        f"select coalesce(string_agg(snapshot_key, ',' order by snapshot_key), '') "
        f"from public.catalog_register_prunable_snapshots() where tozar = $t${marque}$t$")
    assert prunable() == ""
    regdb.psql(f"update public.runs set status = 'completed' where id = '{run_id}'")
    # Referenced by a run checkpoint: kept.
    regdb.psql("insert into public.run_checkpoints (run_id, engine_version, workflow_key, phase, artifacts) "
               f"values ('{run_id}', 'v', 'swarm_v2', 'p', "
               f"'{json.dumps({'government': {'snapshot_key': snaps[1][1]}})}'::jsonb)")
    # The two latest activations (3 and 4) are kept; 1 is referenced.
    expected = sorted([snaps[0][1], snaps[2][1]])
    assert prunable() == ",".join(expected)
    everything = regdb.psql("select coalesce(array_agg(snapshot_key order by snapshot_key collate \"C\")::text, '{}') "
                            "from public.catalog_register_prunable_snapshots()")
    keys = [k.strip('"') for k in everything.strip("{}").split(",") if k]
    digest = prune_digest(keys)
    assert regdb.psql(f"select public.catalog_register_prune_digest('{everything}'::text[])") == digest
    array = "array[" + ",".join(f"'{k}'" for k in keys) + "]::text[]"
    assert "CATALOG_PRUNE_DIGEST_MISMATCH" in _refusal(
        regdb, f"select public.prune_register_snapshots({array}, '{'0' * 64}')")
    result = json.loads(_rpc_as_service(regdb, f"select public.prune_register_snapshots({array}, '{digest}')"))
    assert result["snapshots"] == len(keys)
    remaining = regdb.psql("select string_agg(snapshot_key, ',' order by snapshot_key) from "
                           f"public.catalog_source_snapshots where snapshot_key in "
                           f"('{snaps[1][1]}','{snaps[3][1]}','{snaps[4][1]}')")
    assert remaining.count(",") == 2
    # The append-only triggers are back on after the prune: a plain delete is refused.
    with pytest.raises(AssertionError):
        regdb.psql(f"delete from public.catalog_source_snapshots where snapshot_key='{snaps[4][1]}'")
    assert regdb.psql("select count(*) from pg_trigger where not tgenabled = 'O' and tgname in ("
                      "'catalog_source_snapshots_append_only', 'catalog_raw_records_append_only', "
                      "'catalog_candidate_variants_identity_immutable')") == "0"


# -- 6. review fixes ----------------------------------------------------------------

def test_a_register_that_reverts_is_the_current_version_again(regdb):
    a = {f"rev-{uuid.uuid4().hex[:6]}": 3}
    b = {**a, f"rev-{uuid.uuid4().hex[:6]}": 1}
    version_a = _directory(regdb, a)
    version_b = _directory(regdb, b)
    assert _directory(regdb, a) == version_a != version_b
    latest = json.loads(_rpc_as_service(regdb, "select public.catalog_register_latest_directory()"))
    assert latest["version"]["register_version"] == version_a
    assert [u["tozar"] for u in latest["units"]] == list(a)


def test_the_capacity_guard_counts_rows_still_in_flight(regdb):
    first, second = f"fl-{uuid.uuid4().hex[:6]}", f"fl-{uuid.uuid4().hex[:6]}"
    version = _directory(regdb, {first: 1000, second: 10})
    current = int(regdb.psql("select pg_database_size(current_database())"))
    # A limit that fits either request alone, but not both.
    limit = current + 1005 * 3500
    inflight = int(regdb.psql("select coalesce(sum(expected_rows), 0) from public.catalog_register_capture_units "
                              "where status in ('requested', 'capturing')"))
    limit += inflight * 3500
    assert json.loads(_rpc_as_service(regdb, _request(regdb, version, [first], limit=limit)))["decision"] == "claimed"
    text = _refusal(regdb, _request(regdb, version, [second], limit=limit))
    assert "CATALOG_CAPACITY_THRESHOLD_EXCEEDED" in text


def test_the_page_reads_are_single_documents(regdb):
    states = json.loads(_rpc_as_service(regdb, "select public.catalog_register_unit_states()"))
    assert isinstance(states, list) and len({s["tozar"] for s in states}) == len(states)
    listing = json.loads(_rpc_as_service(regdb, "select public.catalog_register_prunable_list()"))
    assert set(listing) == {"snapshots", "digest"}
    assert listing["digest"] == prune_digest(s["snapshot_key"] for s in listing["snapshots"])


def test_an_archive_uri_must_name_its_snapshot(regdb):
    _user, _project, conversation = _ws_world(regdb)
    _run_id, args = _wsp_capture_run(regdb, conversation)
    snapshot, key = _scoped_snapshot(regdb, args, f"uri-{uuid.uuid4().hex[:6]}", 1)
    wrong = f"gs://milo-test-archive/register/{RESOURCE}/{'a' * 16}/cs1.other.jsonl.gz"
    assert "does not name this snapshot" in _refusal(
        regdb, f"select public.record_register_snapshot_archive({args}, '{snapshot}', '{wrong}', 100, '{'b' * 64}', 1)")

"""PR-OPS1 (P50): the maintenance RPCs finish under PostgREST's 8 s role timeout.

Production runs every service-role RPC through PostgREST 14.5 as the
`authenticator` role (statement_timeout = 8s, lock_timeout = 8s; service_role
has no setting of its own). compact_register_snapshot's dry-run of the
6,379-row Toyota snapshot -- referenced by an open plan, its batch and a
terminal batch run -- failed there with SQLSTATE 57014.

Migration 20261004000100 gives the whole-snapshot RPCs their own
`statement_timeout` (and the two that lock tables a 5 s `lock_timeout`).
Here, on ephemeral PostgreSQL, every call is made the way PostgREST makes it
(`postgrest_call`):

* a session of a login role whose OWN settings are the production
  authenticator's (statement_timeout 8s, lock_timeout 8s);
* per request, one transaction: a first statement that switches to
  service_role and applies the called function's HOISTED settings as
  transaction-scoped settings (PostgREST's `db-hoisted-tx-settings`, whose
  default is the three names in HOISTED), then the RPC as its own statement.

It proves: a Toyota-shaped snapshot of more than 6,500 rows is prepared,
dry-run, archived from its rows and compacted within that session; the same
call WITHOUT the hoist is cancelled (the function's own SET cannot reach a
timer PostgreSQL armed when the statement started -- the production failure),
and WITH it passes; the table-locking RPCs give up a lock wait after their
own 5 s, not the role's 8 s.
"""

from __future__ import annotations

import copy
import json
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

from backend.catalog.register import archive as arc
from backend.catalog.register import compaction as cmp_module
from backend.catalog.register import variants as mapper
from tests.test_catalog_variants import fixture_rows
from tests.test_catalog_variants_postgres import RELEASE_RO, RO_ROLE, _build, _bulk_snapshot
from tests.test_migrations_postgres import (BASELINE, MIGRATIONS, SEED_LEGACY_ROWS, SUPABASE_AUTH_SHIM, WSP_TOYOTA,
                                            EphemeralPostgres, _require_pg_bin, _ws_create, _ws_scope, _ws_world,
                                            _wsb_finish, _wsb_start, _wsp_capture_run, _wsp_units)
from tests.test_register_compaction_postgres import RESOURCE

TIMEOUTS_PG_PORT = "54989"
#: The production connection role, and its settings (read-only check, 2026-09-30).
AUTHENTICATOR = "milo_test_authenticator"
AUTHENTICATOR_SETTINGS = {"statement_timeout": "8s", "lock_timeout": "8s"}
#: PostgREST's `db-hoisted-tx-settings` default (v12+; v14.5 docs, configuration.rst).
HOISTED = ("statement_timeout", "plan_filter.statement_cost_limit", "default_transaction_isolation")
#: More than 6,500 rows, as Toyota's 6,379 and larger.
TOYOTA_ROWS = 6_600
#: The maintenance RPCs and exactly the settings migration 20261004000100 gives them.
MAINTENANCE_RPCS = {
    "public.compact_register_snapshot(text,boolean,text,uuid)":
        ["lock_timeout=5s", "search_path=pg_catalog", "statement_timeout=300s"],
    "public.prune_register_snapshots(text[],text)":
        ["lock_timeout=5s", "search_path=pg_catalog", "statement_timeout=300s"],
    "public.record_register_snapshot_archive_from_database(uuid,text,bigint,text,integer)":
        ["search_path=pg_catalog", "statement_timeout=300s"],
    "public.catalog_register_snapshot_archivable(uuid)": ["search_path=pg_catalog", "statement_timeout=300s"],
    "public.prepare_work_scope_queue(uuid,text,integer,text,jsonb)":
        ["search_path=pg_catalog", "statement_timeout=300s"],
    "public.record_catalog_variants(uuid,text,jsonb)": ["search_path=pg_catalog", "statement_timeout=300s"],
}


@pytest.fixture(scope="module")
def tdb():
    server = EphemeralPostgres(_require_pg_bin(), port=TIMEOUTS_PG_PORT)
    server.start()
    try:
        server.create_database()
        server.psql(f"create role {RELEASE_RO} login bypassrls; grant usage on schema public to {RELEASE_RO}; "
                    f"alter default privileges for role postgres in schema public "
                    f"grant select on tables to {RELEASE_RO}")
        server.psql(file=BASELINE)
        server.psql(sql=SEED_LEGACY_ROWS)
        server.psql(sql=SUPABASE_AUTH_SHIM)
        server.psql(f"create role {RO_ROLE} login bypassrls; grant pg_read_all_data to {RO_ROLE}")
        for migration in MIGRATIONS:
            server.psql(file=migration)
        settings = "; ".join(f"alter role {AUTHENTICATOR} set {name} = '{value}'"
                             for name, value in AUTHENTICATOR_SETTINGS.items())
        server.psql(f"create role {AUTHENTICATOR} login noinherit; grant service_role to {AUTHENTICATOR}; "
                    f"{settings}")
        yield server
    finally:
        server.stop()


class Refused(AssertionError):
    """The database refused the request: `sqlstate` is its SQLSTATE."""

    def __init__(self, sqlstate: str, stderr: str) -> None:
        super().__init__(f"{sqlstate}: {stderr}")
        self.sqlstate = sqlstate


def hoisted_settings(db, signature: str) -> list[tuple[str, str]]:
    """The called function's settings PostgREST applies before the call."""
    config = db.psql(f"select coalesce(array_to_json(proconfig), '[]') from pg_proc "
                     f"where oid = '{signature}'::regprocedure")
    pairs = [item.split("=", 1) for item in json.loads(config)]
    return [(name, value) for name, value in pairs if name in HOISTED]


def postgrest_call(db, signature: str, call: str, *, hoist: bool = True,
                   session: dict[str, str] | None = None) -> tuple[str, float]:
    """ONE request as PostgREST 14.5 makes it: a session of the authenticator
    role (its own settings apply at connect, as in production), then one
    transaction -- the role switch and the function's hoisted settings, then
    the RPC as a statement of its own. `session` overrides a setting at session
    level (the control). Returns (the RPC's output, its seconds)."""
    settings = ["set_config('role', 'service_role', true)"]
    if hoist:
        settings += [f"set_config('{name}', '{value}', true)" for name, value in hoisted_settings(db, signature)]
    script = "".join(f"set {name} = '{value}';\n" for name, value in (session or {}).items())
    script += ("begin;\n\\o /dev/null\n"
               f"select {', '.join(settings)};\n"
               "\\o\n"
               "select extract(epoch from clock_timestamp())::text;\n"
               f"{call};\n"
               "select extract(epoch from clock_timestamp())::text;\n"
               "commit;\n")
    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False, encoding="utf-8") as handle:
        handle.write(script)
    try:
        # -f: psql sends each statement on its own, as PostgREST does (a
        # statement's timer starts when THAT statement starts).
        result = subprocess.run(
            ["psql", "-h", db.dir, "-p", db.port, "-U", AUTHENTICATOR, "-d", "milo", "-v", "ON_ERROR_STOP=1",
             "-v", "VERBOSITY=verbose", "-X", "-q", "-t", "-A", "-f", handle.name],
            capture_output=True, text=True)
    finally:
        Path(handle.name).unlink()
    if result.returncode != 0:
        # VERBOSITY=verbose: "ERROR:  57014: canceling statement due to statement timeout".
        found = re.search(r"ERROR:\s+([0-9A-Z]{5}):", result.stderr)
        raise Refused(found.group(1) if found else "", result.stderr)
    started, output, finished = result.stdout.strip().split("\n")
    return output, float(finished) - float(started)


def _rows(count: int) -> list[dict[str, Any]]:
    base = fixture_rows()
    rows = []
    for index in range(count):
        row = copy.deepcopy(base[index % len(base)])
        row.update(_id=7_000_000 + index, tozar=WSP_TOYOTA, kinuy_mishari=f"{row['kinuy_mishari']}-{index % 40}",
                   degem_cd=row["degem_cd"] + 7_000_000 + index)
        rows.append(row)
    return rows


@pytest.fixture(scope="module")
def toyota(tdb) -> dict[str, Any]:
    """An ACTIVE, fully built Toyota snapshot of TOYOTA_ROWS rows with no
    archive (as Toyota's Prepare snapshot has none): its writer run ended."""
    rows = _rows(TOYOTA_ROWS)
    snapshot = _bulk_snapshot(tdb, rows, WSP_TOYOTA)
    sid = snapshot["id"]
    snapshot["key"] = tdb.psql(f"select snapshot_key from public.catalog_source_snapshots where id = '{sid}'")
    tdb.psql("update public.runs set status = 'completed' where id = "
             f"(select created_by_run_id from public.catalog_source_snapshots where id = '{sid}')")
    for first in range(0, TOYOTA_ROWS, mapper.BUILD_BATCH_ROWS):
        _build(tdb, snapshot, rows[first:first + mapper.BUILD_BATCH_ROWS])
    tdb.psql("analyze")
    return snapshot


def _compact_call(key: str, apply: bool, sha: str | None = None) -> str:
    return (f"select public.compact_register_snapshot('{key}', {str(apply).lower()}, "
            f"{f'{chr(39)}{sha}{chr(39)}' if sha else 'null'}, null)")


COMPACT = "public.compact_register_snapshot(text,boolean,text,uuid)"
PRUNE = "public.prune_register_snapshots(text[],text)"


def test_each_maintenance_rpc_carries_exactly_its_settings(tdb):
    for signature, expected in MAINTENANCE_RPCS.items():
        config = tdb.psql(f"select coalesce(array_to_json(array(select c from unnest(proconfig) c order by c)), '[]') "
                          f"from pg_proc where oid = '{signature}'::regprocedure")
        assert json.loads(config) == expected, signature
        # What PostgREST hoists from it: the statement timeout, nothing else.
        assert hoisted_settings(tdb, signature) == [("statement_timeout", "300s")], signature
    # Called only from inside prepare_work_scope_queue: no setting (it stays inlinable).
    assert tdb.psql("select proconfig is null from pg_proc where oid = "
                    "'public.catalog_work_scope_coverage_decisions(uuid,integer,integer,boolean)'::regprocedure") == "t"
    # The production role settings, unchanged: the fix is per function only.
    assert tdb.psql(f"select array_to_string(rolconfig, ',') from pg_roles where rolname = '{AUTHENTICATOR}'") \
        == "statement_timeout=8s,lock_timeout=8s"
    assert tdb.psql("select rolconfig is null from pg_roles where rolname = 'service_role'") == "t"


def test_without_the_hoist_the_function_setting_cannot_save_the_call(tdb, toyota):
    """The production failure, reproduced at a small scale: a session timeout
    the dry-run cannot meet cancels it (57014) even though the function says
    300 s -- PostgreSQL armed the timer when the statement started. Hoisted
    the way PostgREST hoists it, the same call under the same session passes."""
    session = {"statement_timeout": "100ms"}
    with pytest.raises(Refused) as cancelled:
        postgrest_call(tdb, COMPACT, _compact_call(toyota["key"], False), hoist=False, session=session)
    assert cancelled.value.sqlstate == "57014", str(cancelled.value)
    answer, seconds = postgrest_call(tdb, COMPACT, _compact_call(toyota["key"], False), session=session)
    assert json.loads(answer)["code"] == "CATALOG_COMPACTION_ARCHIVE_MISSING"
    assert seconds > 0.1, "the call really outlived the session timeout"


def test_a_toyota_shaped_snapshot_is_prepared_archived_and_compacted_under_8s_role_settings(tdb, toyota, capsys):
    """Every call as PostgREST makes it, under the authenticator's 8 s: Prepare
    over the snapshot, the dry-run that failed in production (no archive yet:
    ARCHIVE_MISSING, which says every other check passed), the archive written
    from its rows, the dry-run again (READY) and the apply (COMPACTED) -- with
    Toyota's references in place: an open plan, its batch and a terminal run."""
    sid, key = toyota["id"], toyota["key"]
    timings: dict[str, float] = {}
    # Prepare (lease-guarded, as the capture job calls it).
    user, _project, conversation = _ws_world(tdb)
    scope = _ws_scope(max_items=10, batch_size=5, model_year_from=2018)
    plan = _ws_create(tdb, conversation, user, scope)["work_scope"]["id"]
    capture_run, args = _wsp_capture_run(tdb, conversation)
    units = [dict(unit, register_marque=WSP_TOYOTA) if unit["snapshot_id"] else unit for unit in _wsp_units(sid)]
    body = json.dumps({"work_scope_id": plan, "revision": 1, "scope_digest": scope.digest(), "units": units})
    prepared, timings["prepare_work_scope_queue"] = postgrest_call(
        tdb, "public.prepare_work_scope_queue(uuid,text,integer,text,jsonb)",
        f"select public.prepare_work_scope_queue({args}, $j${body}$j$::jsonb)")
    batches = [batch["id"] for batch in json.loads(prepared)["batches"]]
    assert batches
    # Toyota's references: the plan stays open, its batch ran to a terminal state.
    world = {"plan": plan, "digest": scope.digest(), "user": user}
    run = _wsb_start(tdb, world, batches[0], key=f"ops1-{key[-8:]}")["run"]["id"]
    _wsb_finish(tdb, run, "partial_success")
    _wsb_finish(tdb, capture_run, "completed")
    assert tdb.psql(f"select closed_at is null from public.catalog_work_scopes where id = '{plan}'") == "t"

    answer, timings["compact dry-run (no archive)"] = postgrest_call(tdb, COMPACT, _compact_call(key, False))
    assert (json.loads(answer)["status"], json.loads(answer)["code"]) == ("refused", "CATALOG_COMPACTION_ARCHIVE_MISSING")
    # The archive from the stored rows, as compaction.archive_from_database writes it.
    expected, timings["catalog_register_snapshot_archivable"] = postgrest_call(
        tdb, "public.catalog_register_snapshot_archivable(uuid)",
        f"select public.catalog_register_snapshot_archivable('{sid}')")
    assert int(expected) == TOYOTA_ROWS
    lines = [arc.canonical_line(row) for row in toyota["rows"]]
    page = cmp_module.LINE_CHECK_PAGE
    for first in range(0, TOYOTA_ROWS, page):
        literal = "array[" + ", ".join("$l$" + line.rstrip("\n") + "$l$" for line in lines[first:first + page]) + "]"
        mismatched, _seconds = postgrest_call(
            tdb, "public.catalog_raw_record_lines_mismatched(uuid,integer,text[])",
            f"select public.catalog_raw_record_lines_mismatched('{sid}', {first}, {literal})")
        assert mismatched == "0"
    obj = arc.build(toyota["rows"])
    uri = f"gs://milo-test-archive/register/{RESOURCE}/{'5' * 16}/{key}.jsonl.gz"
    _recorded, timings["record_register_snapshot_archive_from_database"] = postgrest_call(
        tdb, "public.record_register_snapshot_archive_from_database(uuid,text,bigint,text,integer)",
        f"select public.record_register_snapshot_archive_from_database('{sid}', '{uri}', {obj.byte_size}, "
        f"'{obj.sha256}', {obj.line_count})")
    answer, timings["compact dry-run"] = postgrest_call(tdb, COMPACT, _compact_call(key, False))
    assert json.loads(answer)["status"] == "ready" and json.loads(answer)["raw_rows"] == TOYOTA_ROWS
    answer, timings["compact apply"] = postgrest_call(tdb, COMPACT, _compact_call(key, True, obj.sha256))
    assert json.loads(answer)["status"] == "compacted"
    assert json.loads(answer)["payloads_removed"] == TOYOTA_ROWS
    assert tdb.psql(f"select count(*) from public.catalog_raw_records where snapshot_id = '{sid}' "
                    "and payload is not null") == "0"
    with capsys.disabled():
        print(f"\nPR-OPS1 timings ({TOYOTA_ROWS:,} rows, under the 8 s role settings, s): "
              + ", ".join(f"{name} {seconds:.2f}" for name, seconds in timings.items()))


@pytest.mark.parametrize("signature,call", [
    (PRUNE, f"select public.prune_register_snapshots('{{}}'::text[], '{'0' * 64}')"),
    (COMPACT, None),
])
def test_a_table_locking_rpc_gives_up_a_lock_wait_after_its_own_5s(tdb, toyota, signature, call):
    """lock_timeout is not hoisted; PostgreSQL applies the function's own to
    every lock wait inside it: a held conflicting lock refuses the call with
    55P03 after 5 s, not the role's 8 s."""
    call = call or _compact_call(toyota["key"], True, "0" * 64)
    holder = subprocess.Popen(
        ["psql", "-h", tdb.dir, "-p", tdb.port, "-U", "postgres", "-d", "milo", "-X", "-q", "-c",
         "begin; lock table public.catalog_raw_records in exclusive mode; select pg_sleep(30); commit;"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 10
        while tdb.psql("select count(*) from pg_locks l join pg_class c on c.oid = l.relation "
                       "where c.relname = 'catalog_raw_records' and l.mode = 'ExclusiveLock' and l.granted") != "1":
            assert time.monotonic() < deadline, "the holder never took its lock"
            time.sleep(0.05)
        started = time.monotonic()
        with pytest.raises(Refused) as refused:
            postgrest_call(tdb, signature, call)
        waited = time.monotonic() - started
    finally:
        holder.kill()
        holder.wait()
    assert refused.value.sqlstate == "55P03", str(refused.value)
    assert 4.5 <= waited < 7.5, waited

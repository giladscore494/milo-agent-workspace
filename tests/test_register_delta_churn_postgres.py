"""PR-DELTA-0: MEASUREMENT ONLY -- the MVCC churn of re-capturing a drifted tozar.

No runtime code and no migration: this module measures what the current
register path writes when ONE drifted 1,000-row tozar is re-captured whole
(PR-D1 capture, PR-L1 build, PR-L2 active + superseded compaction), against
what a delta capture (docs/production-readiness/REGISTER_DELTA_CAPTURE.md,
option A) would write for the same drift: only the changed rows, through the
same build and the same compaction, plus a narrow retirement row per removed
content. It prints the figures the design document quotes; the assertions
only keep the measurement honest (the drift is detected exactly, and the two
paths are compared on the same counters).

The cluster is ephemeral (the PR-L2 pattern: every migration applied, rows
written by the same bulk helper, the build through `record_catalog_variants`,
compaction through `compact_register_snapshot`). Autovacuum is off in this
cluster so the dead-tuple counts are exactly what each path leaves behind.
The retirement table is a probe created in this throwaway database only; it is
not a migration.
"""

from __future__ import annotations

import copy
import json

import pytest

from backend.catalog.register import variants as mapper
from tests.test_catalog_variants import fixture_rows
from tests.test_catalog_variants_postgres import RELEASE_RO, RO_ROLE, _build, _bulk_snapshot, _json, _psql_file
from tests.test_migrations_postgres import (BASELINE, MIGRATIONS, SEED_LEGACY_ROWS, SUPABASE_AUTH_SHIM,
                                            EphemeralPostgres, _require_pg_bin)
from tests.test_register_compaction_postgres import RESOURCE, _compact, _settle_capture_runs

DELTA_PG_PORT = "54987"
TABLES = ("catalog_raw_records", "catalog_candidate_variants", "catalog_variants", "catalog_variant_coverage")
PROBE = "delta_retirements_probe"
#: The tozar size the brief asks for, and a drift shaped like production's
#: (net growth, plus one changed and one removed row).
ROWS, ADDED, CHANGED, REMOVED = 1000, 8, 1, 1
#: Whole re-captures replayed with a plain VACUUM between them (autovacuum's
#: effect), to see where the file size settles.
CYCLES = 4


@pytest.fixture(scope="module")
def ddb():
    server = EphemeralPostgres(_require_pg_bin(), port=DELTA_PG_PORT)
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
        server.psql("alter system set autovacuum = off")
        server.psql("select pg_reload_conf()")
        server.psql(f"create table public.{PROBE} (retired_by_snapshot_id uuid not null, "
                    "snapshot_id uuid not null, upstream_record_id text not null, content_sha256 text not null, "
                    "primary key (snapshot_id, upstream_record_id))")
        yield server
    finally:
        server.stop()


def _rows(marque: str, count: int, first_id: int) -> list[dict]:
    """`count` register rows of one tozar derived from the committed fixtures,
    each its own identity (its own `degem_cd`), as the PR-L2 byte test does."""
    base, rows = fixture_rows(), []
    for index in range(count):
        row = copy.deepcopy(base[index % len(base)])
        row.update(_id=first_id + index, tozar=marque, kinuy_mishari=f"{row['kinuy_mishari']}-{index % 40}",
                   degem_cd=row["degem_cd"] + first_id + index)
        rows.append(row)
    return rows


def _drifted(rows: list[dict], marque: str, first_new_id: int) -> list[dict]:
    """The register after the drift: ADDED new rows, CHANGED rows with a new
    value, REMOVED rows gone (the last ones), the rest untouched."""
    fresh = [copy.deepcopy(row) for row in rows[:len(rows) - REMOVED]]
    for row in fresh[:CHANGED]:
        row["koah_sus"] = (row.get("koah_sus") or 0) + 1
    return fresh + _rows(marque, ADDED, first_new_id)


def _capture(db, rows: list[dict], marque: str) -> dict:
    """One capture as the register path leaves it: active, built, archived
    (line count = rows), its writer run ended -- then compacted (active)."""
    snapshot = _bulk_snapshot(db, rows, marque)
    snapshot["key"] = db.psql(f"select snapshot_key from public.catalog_source_snapshots where id='{snapshot['id']}'")
    for first in range(0, len(rows), mapper.BUILD_BATCH_ROWS):
        _build(db, snapshot, rows[first:first + mapper.BUILD_BATCH_ROWS])
    run = db.psql(f"select created_by_run_id from public.catalog_source_snapshots where id='{snapshot['id']}'")
    db.psql("insert into public.catalog_register_snapshot_archives (snapshot_id, snapshot_key, gcs_uri, byte_size, "
            f"sha256, line_count, recorded_by_run_id) values ('{snapshot['id']}', '{snapshot['key']}', "
            f"'gs://milo-test-archive/register/{RESOURCE}/{'3' * 16}/{snapshot['key']}.jsonl.gz', 10, "
            f"'{'b' * 64}', {len(rows)}, '{run}'); update public.runs set status = 'completed' where id = '{run}'")
    assert _compact(db, snapshot["key"])["status"] == "compacted"
    return snapshot


def _counters(db) -> dict[str, tuple[int, ...]]:
    """(inserted, updated, deleted, dead) per table. Every psql call is its own
    backend, which flushes its statistics when it exits."""
    names = ", ".join(f"'{t}'" for t in (*TABLES, PROBE))
    out = db.psql("select relname, n_tup_ins, n_tup_upd, n_tup_del, n_dead_tup from pg_stat_user_tables "
                  f"where schemaname = 'public' and relname in ({names})")
    return {line.split("|")[0]: tuple(int(x) for x in line.split("|")[1:]) for line in out.splitlines()}


def _bytes(db, *, full: bool) -> int:
    for table in (*TABLES, PROBE):
        db.psql(f"vacuum {'(full, analyze)' if full else '(analyze)'} public.{table}")
    return sum(int(db.psql(f"select pg_total_relation_size('public.{t}')")) for t in (*TABLES, PROBE))


def _wal(db) -> int:
    return int(db.psql("select pg_current_wal_insert_lsn() - '0/0'::pg_lsn"))


def _measure(db, label: str, work) -> dict:
    """Run `work` from a rewritten (VACUUM FULL) state and report what it left."""
    _settle_capture_runs(db)
    live_before = _bytes(db, full=True)
    db.psql("checkpoint")
    before, wal = _counters(db), _wal(db)
    work()
    wal = _wal(db) - wal
    after = _counters(db)
    delta = {t: tuple(a - b for a, b in zip(after.get(t, (0,) * 4), before.get(t, (0,) * 4)))
             for t in (*TABLES, PROBE)}
    file_after_plain_vacuum = _bytes(db, full=False)
    live_after = _bytes(db, full=True)
    return {"label": label, "per_table": delta, "wal": wal,
            "written": sum(d[0] + d[1] for d in delta.values()),
            "dead": sum(d[3] for d in delta.values()),
            "file_growth": file_after_plain_vacuum - live_before, "live_growth": live_after - live_before}


def _diff(db, snapshot_id: str, fresh: list[dict]) -> dict:
    """Q1's detection, from what is STORED: the multiset of the active
    snapshot's variant content hashes (payload minus `_id`) against the fresh
    rows' hashes, duplicates counted. Nothing reads a payload or an `_id`."""
    out = _psql_file(db, f"""
      with fresh as (select public.catalog_variant_content_sha256(p) h, count(*) n
                       from jsonb_array_elements({_json(fresh)}) p group by 1),
           stored as (select v.content_sha256 h, count(*) n from public.catalog_variants v
                       where v.snapshot_id = '{snapshot_id}'
                         and v.mapper_version = public.catalog_variant_mapper_version() group by 1)
      select coalesce(sum(greatest(coalesce(f.n, 0) - coalesce(s.n, 0), 0)), 0) || '|' ||
             coalesce(sum(greatest(coalesce(s.n, 0) - coalesce(f.n, 0), 0)), 0) || '|' ||
             coalesce(sum(least(coalesce(f.n, 0), coalesce(s.n, 0))), 0)
        from fresh f full join stored s on s.h = f.h""")
    added, retired, kept = (int(x) for x in out.split("|"))
    return {"added": added, "retired": retired, "kept": kept}


def test_delta_capture_churn_against_a_whole_recapture(ddb, capsys):
    whole_marque, delta_marque, cycle_marque = "מדידה-מלא", "מדידה-דלתא", "מדידה-מחזור"
    whole_base = _capture(ddb, _rows(whole_marque, ROWS, 3_000_000), whole_marque)
    delta_rows = _rows(delta_marque, ROWS, 4_000_000)
    delta_base = _capture(ddb, delta_rows, delta_marque)

    # Detection (Q1) from stored hashes: exact, and blind to `_id` renumbering.
    fresh = _drifted(delta_rows, delta_marque, 4_500_000)
    expected = {"added": ADDED + CHANGED, "retired": CHANGED + REMOVED, "kept": ROWS - CHANGED - REMOVED}
    assert _diff(ddb, delta_base["id"], fresh) == expected
    renumbered = [dict(row, _id=9_000_000 + index) for index, row in enumerate(fresh)]
    assert _diff(ddb, delta_base["id"], renumbered) == expected
    # Two rows that differ only in `_id` are ONE content twice: a multiset, not a set.
    doubled = fresh + [dict(fresh[-1], _id=9_999_999)]
    assert _diff(ddb, delta_base["id"], doubled)["added"] == expected["added"] + 1

    def whole() -> None:
        # Today: the drifted tozar re-captured WHOLE into a new snapshot, built,
        # compacted (active), and the old one compacted (superseded).
        _capture(ddb, _drifted(whole_base["rows"], whole_marque, 3_500_000), whole_marque)
        assert _compact(ddb, whole_base["key"])["mode"] == "superseded"

    def delta() -> None:
        # Option A: a delta snapshot of the changed rows only (the added rows
        # and the new version of the changed one), through the same build and
        # compaction, and one narrow retirement row per removed content.
        changed = fresh[:CHANGED] + fresh[len(fresh) - ADDED:]
        added = _capture(ddb, changed, f"{delta_marque}#delta")
        retired = [row for row in delta_rows[:CHANGED]] + delta_rows[ROWS - REMOVED:]
        ids = ", ".join(f"'{row['_id']}'" for row in retired)
        ddb.psql(f"insert into public.{PROBE} select '{added['id']}', v.snapshot_id, v.upstream_record_id, "
                 f"v.content_sha256 from public.catalog_variants v where v.snapshot_id = '{delta_base['id']}' "
                 f"and v.upstream_record_id in ({ids})")

    measured = [_measure(ddb, "whole re-capture (today)", whole), _measure(ddb, "delta (option A)", delta)]

    # Option C: whole re-captures of one 1,000-row tozar, a plain VACUUM after
    # each (what autovacuum does), the file size after each cycle.
    cycle_rows = _rows(cycle_marque, ROWS, 5_000_000)
    previous = _capture(ddb, cycle_rows, cycle_marque)
    _settle_capture_runs(ddb)
    files = [_bytes(ddb, full=True)]
    for cycle in range(1, CYCLES + 1):
        current = _capture(ddb, [dict(r, koah_sus=(r.get("koah_sus") or 0) + cycle) for r in cycle_rows],
                           cycle_marque)
        assert _compact(ddb, previous["key"])["mode"] == "superseded"
        previous = current
        files.append(_bytes(ddb, full=False))
    _settle_capture_runs(ddb)
    files.append(_bytes(ddb, full=True))

    with capsys.disabled():
        print(f"\nPR-DELTA-0 churn of one drifted {ROWS:,}-row tozar (+{ADDED} added, {CHANGED} changed, "
              f"-{REMOVED} removed); per table: inserted/updated/deleted/dead tuples")
        for m in measured:
            tables = "; ".join(f"{t.removeprefix('catalog_')} {d[0]}/{d[1]}/{d[2]}/{d[3]}"
                               for t, d in m["per_table"].items() if any(d))
            print(f"  {m['label']}: tuples written {m['written']:,}, dead {m['dead']:,}, WAL {m['wal']:,} B, "
                  f"file growth after plain VACUUM {m['file_growth']:,} B, live growth {m['live_growth']:,} B "
                  f"[{tables}]")
        print(f"  option C, {CYCLES} whole re-captures of a {ROWS:,}-row tozar with a plain VACUUM after each, "
              f"file bytes (first and last after VACUUM FULL): " + " -> ".join(f"{b:,}" for b in files))

    whole_m, delta_m = measured
    assert delta_m["written"] * 20 < whole_m["written"]
    assert delta_m["dead"] * 20 < whole_m["dead"]
    assert delta_m["wal"] < whole_m["wal"]

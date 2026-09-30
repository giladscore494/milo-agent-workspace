"""PR-L2: payload compaction, offline (the in-memory mirror).

The database function is the authority and is exercised on ephemeral
PostgreSQL (tests/test_register_compaction_postgres.py). Here: the capture
job compacts after a complete build and reports it; every Python reader
answers identically before and after; a compacted snapshot is never rebuilt
from the database; the operator entrypoint (dry-run first) and the archive of
a Prepare snapshot written from its stored rows; the original record fetched
from its archive and checked.
"""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import UUID

import pytest

from backend.catalog import operator_capture as entrypoint
from backend.catalog.government import source as src
from backend.catalog.government.capture_scope import CaptureScope
from backend.catalog.government.ingest import GovernmentCatalogIngestor
from backend.catalog.government.preparation import _same_variant_rows
from backend.catalog.government.projection import GovernmentCatalogProjection, GovernmentProjectionError
from backend.catalog.government.query import (GovernmentCatalogQuery, identity_projection, reading_projection,
                                              reading_unstated, unstated_fields)
from backend.catalog.register import archive as arc
from backend.catalog.register import compaction
from backend.catalog.register import variants as mapper
from backend.testing.memory_repository import MemoryRepository
from tests.test_catalog_variants import fixture_rows
from tests.test_register_capture import (BUCKET, RELEASE_SHA, TOYOTA, USER, FakeWriter, api_env,  # noqa: F401
                                         capture_env, captured_world, claimed_lease, no_sockets, scoped_client,
                                         snapshot_by_key, world)


class FakeReader(FakeWriter):
    """The same bucket, read back (objectViewer)."""

    def get(self, name: str) -> bytes:
        if name not in self.objects:
            raise arc.ArchiveWriteError("no such object")
        return self.objects[name]


def rows_with_shapes() -> list[dict[str, Any]]:
    rows = fixture_rows()
    base = next(r for r in rows if r["_id"] == 37425)
    return rows + [dict(base, _id=90001, degem_cd=990001, ramat_gimur=None),
                   dict(base, _id=90002, degem_cd=990002, ramat_gimur=""),
                   dict(base, _id=90003, degem_cd=990003, delek_cd=None)]


def uncompacted(records=None):
    writer = FakeReader()
    repo, w, _version, report, _writer = captured_world(records or rows_with_shapes(), writer=writer, compact=False)
    snapshot = snapshot_by_key(repo, report.units[0].snapshot_key)
    return repo, snapshot, writer


def readers(repo: MemoryRepository, snapshot: dict[str, Any]) -> dict[str, Any]:
    query = GovernmentCatalogQuery(repo, snapshot_key=snapshot["snapshot_key"])
    page = query.list_variants(limit=100)
    answers: dict[str, Any] = {"page": [(v.upstream_record_id, v.register_codes) for v in page.items]}
    for item in page.items:
        resolved = query.resolve_variant(item.manufacturer, item.commercial_model, item.model_year_start,
                                         trim=item.trim, official_model_code=item.official_model_code,
                                         identity_dimensions=item.identity_dimensions, **item.register_codes)
        answers[item.upstream_record_id] = (resolved.match_count, dict(resolved.identity_projection),
                                            resolved.unstated_fields)
    answers["facts"] = sorted(repo._coverage_facts(c)[:2] for c in repo.catalog_candidates.values()
                              if str(c["snapshot_id"]) == str(snapshot["id"]))
    answers["duplicates"] = _same_variant_rows(query, page.items[0], page.items, {})
    return answers


def test_the_capture_job_compacts_a_built_archived_snapshot_and_every_reader_answers_the_same():
    repo, snapshot, _writer = uncompacted()
    before = readers(repo, snapshot)
    assert before["37425"][0] == 1 and before["90001"][2] == ("ramat_gimur",)
    answer = compaction.compact(repo, snapshot["snapshot_key"], apply=True)
    assert answer["status"] == "compacted" and answer["payloads_removed"] == 19
    assert all(r["payload"] is None for r in repo.catalog_raw_records.values()
               if str(r["snapshot_id"]) == str(snapshot["id"]))
    assert readers(repo, snapshot) == before
    assert compaction.compact(repo, snapshot["snapshot_key"], apply=True)["status"] == "unchanged"
    # The capture job does the same by itself, and reports it.
    _repo, _w, _v, report, _wr = captured_world(rows_with_shapes())
    assert report.units[0].compaction == {"status": "compacted"}
    assert report.units[0].as_document()["compaction"] == {"status": "compacted"}


def test_the_typed_reading_is_the_payloads_projection_for_every_committed_row():
    repo, snapshot, _writer = uncompacted()
    payloads = {r["upstream_record_id"]: r["payload"] for r in repo.catalog_raw_records.values()
                if str(r["snapshot_id"]) == str(snapshot["id"])}
    compaction.compact(repo, snapshot["snapshot_key"], apply=True)
    for record_id, payload in payloads.items():
        reading = repo.catalog_compacted_record_reading(snapshot["id"], record_id)
        assert reading_projection(reading) == identity_projection(payload)
        assert reading_unstated(reading) == unstated_fields(payload)


def test_a_compacted_snapshot_is_never_rebuilt_from_the_database(monkeypatch, capsys):
    repo, snapshot, _writer = uncompacted()
    compaction.compact(repo, snapshot["snapshot_key"], apply=True)
    # The current mapper's complete build is unchanged, without a read.
    assert mapper.build_snapshot_variants(repo, snapshot["id"])["status"] == "unchanged"
    monkeypatch.setattr(mapper, "MAPPER_VERSION", "gov.wltp.variant-mapper.9")
    with pytest.raises(mapper.VariantMappingError) as refused:
        mapper.build_snapshot_variants(repo, snapshot["id"])
    assert refused.value.reason_code == "CATALOG_VARIANT_SNAPSHOT_COMPACTED"
    assert mapper.main(["--snapshot-key", snapshot["snapshot_key"]], repository=repo) == mapper.EXIT_REFUSED
    assert "REFUSED CATALOG_VARIANT_SNAPSHOT_COMPACTED" in capsys.readouterr().out
    # The whole-snapshot projection (no production caller) refuses too.
    with pytest.raises(GovernmentProjectionError) as projection:
        GovernmentCatalogProjection(repo, snapshot_key=snapshot["snapshot_key"]).list_manufacturers()
    assert projection.value.reason_code == "GOV_PROJECTION_SNAPSHOT_COMPACTED"


def test_the_compaction_after_a_build_never_raises_and_reports_a_refusal():
    repo, snapshot, _writer = uncompacted()
    assert compaction.compact_after_build(repo, snapshot["snapshot_key"], {"status": "failed"}) == \
        {"status": "skipped"}
    repo._register_state()["archives"].clear()
    assert compaction.compact_after_build(repo, snapshot["snapshot_key"], {"status": "built"}) == \
        {"status": "refused", "code": "CATALOG_COMPACTION_ARCHIVE_MISSING"}

    class Broken:
        def compact_register_snapshot(self, *_args):
            raise RuntimeError("database down")

    assert compaction.compact_after_build(Broken(), "cs1.x", {"status": "built"}) == \
        {"status": "failed", "code": "CATALOG_COMPACTION_FAILED"}


def test_every_precondition_is_refused_by_the_mirror():
    repo, snapshot, _writer = uncompacted()
    key = snapshot["snapshot_key"]
    assert compaction.compact(repo, "cs1." + "0" * 32, apply=True)["code"] == "CATALOG_COMPACTION_SNAPSHOT_UNKNOWN"
    build = repo._variants_state()["builds"][(str(snapshot["id"]), mapper.MAPPER_VERSION)]
    build["completed_at"] = None
    assert compaction.compact(repo, key, apply=True)["code"] == "CATALOG_COMPACTION_BUILD_INCOMPLETE"
    build["completed_at"] = "now"
    (unit,) = [u for u in repo._register_state()["units"].values() if u.get("snapshot_id") == str(snapshot["id"])]
    unit["count_verified"] = False
    assert compaction.compact(repo, key, apply=True)["code"] == "CATALOG_COMPACTION_COUNT_UNVERIFIED"
    unit["count_verified"] = True
    record = next(r for r in repo.catalog_raw_records.values() if str(r["snapshot_id"]) == str(snapshot["id"]))
    record["payload"] = dict(record["payload"], tozeret_cd="0413")
    answer = compaction.compact(repo, key, apply=True)
    assert (answer["code"], answer["mismatched_rows"]) == ("CATALOG_COMPACTION_TYPED_MISMATCH", 1)
    assert all(r["payload"] is not None for r in repo.catalog_raw_records.values())


# -- the operator entrypoint ------------------------------------------------------------

def test_the_operator_path_is_dry_run_first_then_apply(capsys):
    repo, snapshot, writer = uncompacted()
    key = snapshot["snapshot_key"]
    assert compaction.main(["--snapshot-key", key, "--dry-run"], repository=repo, archive_client=writer) == 0
    assert capsys.readouterr().out.startswith(f"READY snapshot_key={key} raw_rows=19")
    assert all(r["payload"] is not None for r in repo.catalog_raw_records.values())
    assert compaction.main(["--snapshot-key", key, "--apply"], repository=repo, archive_client=writer) == 0
    assert capsys.readouterr().out.startswith(f"COMPACTED snapshot_key={key} raw_rows=19 payloads_removed=19")
    assert compaction.main(["--snapshot-key", key, "--apply"], repository=repo, archive_client=writer) == 0
    assert capsys.readouterr().out.startswith("UNCHANGED ")
    for argv in (["--snapshot-key", key], ["--snapshot-key", "bad key", "--apply"],
                 ["--snapshot-key", key, "--apply", "--dry-run"], ["--apply"]):
        assert compaction.main(argv, repository=repo, archive_client=writer) == compaction.EXIT_REFUSED
        assert "REFUSED CATALOG_COMPACTION_REQUEST_INVALID" in capsys.readouterr().out
    assert compaction.main(["--snapshot-key", "cs1." + "0" * 32, "--apply"], repository=repo,
                           archive_client=writer) == compaction.EXIT_REFUSED
    assert "REFUSED CATALOG_COMPACTION_SNAPSHOT_UNKNOWN" in capsys.readouterr().out


def test_the_original_record_comes_from_the_archive_and_is_checked(capsys):
    repo, snapshot, writer = uncompacted()
    original = next(r["payload"] for r in repo.catalog_raw_records.values()
                    if str(r["snapshot_id"]) == str(snapshot["id"]) and r["upstream_record_id"] == "37425")
    compaction.compact(repo, snapshot["snapshot_key"], apply=True)
    assert compaction.source_record(repo, writer, snapshot["id"], "37425") == original
    assert compaction.main(["--snapshot-key", snapshot["snapshot_key"], "--show-record", "37425"],
                           repository=repo, archive_client=writer) == 0
    assert capsys.readouterr().out == "RECORD " + arc.canonical_line(original)
    # Another object under the name, or a line that is not the row: refused.
    (name,) = writer.objects
    stored = writer.objects[name]
    lines = arc.read_lines(stored)
    lines[lines.index(original)] = dict(original, ramat_gimur="X")
    writer.objects[name] = arc.build(lines).data
    with pytest.raises(compaction.CompactionError) as unreadable:
        compaction.source_record(repo, writer, snapshot["id"], "37425")
    assert unreadable.value.code == "CATALOG_ARCHIVE_UNREADABLE"
    repo._register_state()["archives"][str(snapshot["id"])]["sha256"] = hashlib.sha256(writer.objects[name]).hexdigest()
    with pytest.raises(compaction.CompactionError) as mismatch:
        compaction.source_record(repo, writer, snapshot["id"], "37425")
    assert mismatch.value.code == "CATALOG_ARCHIVE_LINE_MISMATCH"
    with pytest.raises(compaction.CompactionError, match="CATALOG_ARCHIVE_UNREADABLE"):
        compaction.source_record(repo, FakeReader(), snapshot["id"], "37425")


def test_a_prepare_snapshot_gets_its_archive_from_its_stored_rows_then_is_compacted(capsys):
    """The Toyota snapshots Prepare captured before PR-D1 have no archive: the
    apply writes it from the stored rows (capture order) with PR-D1's writer --
    the same object a register capture of the same rows writes -- then compacts."""
    records = rows_with_shapes()
    repo, w = world()
    code, doc = entrypoint.prepare_capture_run(repo, conversation_id=UUID(w["conversation"]), requested_by=USER,
                                               idempotency_key="k", env=capture_env(MILO_RELEASE_SHA=RELEASE_SHA))
    assert code == entrypoint.EXIT_OK, doc
    lease = claimed_lease(repo, doc["preparation"]["run_id"])
    prepared = GovernmentCatalogIngestor(repo, lease, client=scoped_client(records)).ingest_resource(
        src.WLTP_RESOURCE_ID, capture_scope=CaptureScope.for_register_marque(TOYOTA))
    snapshot = snapshot_by_key(repo, prepared.snapshot_key)
    assert mapper.build_snapshot_variants(repo, snapshot["id"])["status"] == "built"
    assert repo.register_snapshot_archive(snapshot["id"]) is None
    writer = FakeReader()
    key = snapshot["snapshot_key"]
    assert compaction.main(["--snapshot-key", key, "--dry-run"], repository=repo, archive_client=writer) == 0
    assert "archive=from-database" in capsys.readouterr().out and writer.objects == {}
    assert compaction.main(["--snapshot-key", key, "--apply"], repository=repo, archive_client=None) == 1
    assert "FAILED CATALOG_ARCHIVE_NOT_CONFIGURED" in capsys.readouterr().out
    assert compaction.main(["--snapshot-key", key, "--apply"], repository=repo, archive_client=writer) == 0
    assert capsys.readouterr().out.startswith("COMPACTED ")
    (name, data), = writer.objects.items()
    archive = repo.register_snapshot_archive(snapshot["id"])
    assert archive["gcs_uri"] == f"gs://{BUCKET}/{name}" and archive["sha256"] == hashlib.sha256(data).hexdigest()
    # Exactly the object a register capture of the same rows writes.
    _r, _w2, _v, _report, captured = captured_world(records, compact=False)
    assert list(captured.objects.values()) == [data]
    assert compaction.source_record(repo, writer, snapshot["id"], "90002")["ramat_gimur"] == ""


def test_nothing_is_written_before_the_database_checks_every_line():
    """The archive of stored rows is uploaded only once the database says the
    rows are exactly an archive's lines AND every line is its row."""
    repo, snapshot, _writer = uncompacted()
    writer = FakeReader()

    repo.register_snapshot_archive = lambda _sid: None
    original = repo.catalog_register_snapshot_archivable
    repo.catalog_register_snapshot_archivable = lambda _sid: (_ for _ in ()).throw(RuntimeError("count"))
    with pytest.raises(compaction.CompactionError) as failed:
        compaction.archive_from_database(repo, snapshot, writer)
    assert failed.value.code == "CATALOG_ARCHIVE_WRITE_FAILED" and writer.objects == {}
    repo.catalog_register_snapshot_archivable = original
    repo.catalog_raw_record_lines_mismatched = lambda _sid, first, lines: 1 if first == 0 else 0
    with pytest.raises(compaction.CompactionError) as failed:
        compaction.archive_from_database(repo, snapshot, writer)
    assert failed.value.code == "CATALOG_ARCHIVE_LINE_MISMATCH" and writer.objects == {}


def test_the_capture_job_keeps_a_superseded_capture_as_its_referenced_rows(capsys):
    """After the tozar's new snapshot is compacted, the capture job compacts the
    older one SUPERSEDED: nothing references it, so no row stays; it is never
    browsed again, and its archive answers for every record."""
    records = rows_with_shapes()
    writer = FakeReader()
    repo, w, _v, report, _wr = captured_world(records, writer=writer)
    old = snapshot_by_key(repo, report.units[0].snapshot_key)
    code, doc = entrypoint.prepare_capture_run(repo, conversation_id=UUID(w["conversation"]), requested_by=USER,
                                               idempotency_key="k2", env=capture_env(MILO_RELEASE_SHA=RELEASE_SHA))
    assert code == entrypoint.EXIT_OK, doc
    lease = claimed_lease(repo, doc["preparation"]["run_id"])
    changed = [dict(r, koah_sus=(r.get("koah_sus") or 0) + 1) for r in records]
    prepared = GovernmentCatalogIngestor(repo, lease, client=scoped_client(changed)).ingest_resource(
        src.WLTP_RESOURCE_ID, capture_scope=CaptureScope.for_register_marque(TOYOTA))
    new = snapshot_by_key(repo, prepared.snapshot_key)
    assert mapper.build_snapshot_variants(repo, new["id"])["status"] == "built"
    assert repo.catalog_register_superseded_snapshots(new["snapshot_key"])[0]["snapshot_key"] == old["snapshot_key"]
    assert compaction.main(["--snapshot-key", new["snapshot_key"], "--apply"], repository=repo,
                           archive_client=writer) == 0
    capsys.readouterr()
    done = compaction.compact_after_build(repo, new["snapshot_key"], {"status": "unchanged"}, writer=writer)
    assert done == {"status": "unchanged",
                    "superseded": [{"snapshot_key": old["snapshot_key"], "status": "compacted"}]}
    assert repo.register_snapshot_archived(old["id"])
    assert not [r for r in repo.catalog_raw_records.values() if str(r["snapshot_id"]) == str(old["id"])]
    with pytest.raises(Exception, match="could not be answered") as refused:
        GovernmentCatalogQuery(repo, snapshot_key=old["snapshot_key"]).list_variants(limit=5)
    assert getattr(refused.value.__context__, "code", None) == "CATALOG_SNAPSHOT_ARCHIVED"
    assert compaction.source_record(repo, writer, old["id"], "90002") == \
        next(r for r in records if r["_id"] == 90002)
    assert readers(repo, new)["37425"][0] == 1
    # Again: nothing left to do.
    assert compaction.compact_after_build(repo, new["snapshot_key"], {"status": "unchanged"}, writer=writer) == \
        {"status": "unchanged"}


def test_the_archive_client_reads_an_object_back():
    class Response:
        status_code, content = 200, b"gz"

    class Session:
        def get(self, url, params, timeout):
            assert params == {"alt": "media"} and "/o/register%2Fx" in url
            return Response()

    assert arc.GcsArchiveWriter(BUCKET, session_factory=Session).get("register/x") == b"gz"
    Response.status_code = 404
    with pytest.raises(arc.ArchiveWriteError, match="HTTP 404"):
        arc.GcsArchiveWriter(BUCKET, session_factory=Session).get("register/x")


def test_a_mapper_bump_is_refused_while_compaction_has_no_rebuild_from_the_archive():
    """S2: a compacted snapshot is read through its compaction's mapper version.
    Until a rebuild-from-archive exists, no migration after PR-L2 may redefine
    the variant mapper version (the release would hide every compacted tozar),
    and the Python mapper is the database's; the deployed gate refuses a
    database whose live compactions name another mapper
    (CATALOG_COMPACTION_MAPPER, production-verify.sh)."""
    import re
    from pathlib import Path

    migrations = sorted((Path(__file__).resolve().parents[1] / "supabase" / "migrations").glob("*.sql"))
    defining = [m for m in migrations
                if "create or replace function public.catalog_variant_mapper_version()" in m.read_text()]
    assert all(m.name < "20261002000100" for m in defining), \
        "a mapper-version bump after PR-L2 needs rebuild-from-archive first (compacted snapshots have no payload)"
    latest = defining[-1].read_text()
    body = latest[latest.index("create or replace function public.catalog_variant_mapper_version()"):]
    assert re.search(r"'([^']+)'", body).group(1) == mapper.MAPPER_VERSION
    verify = (Path(__file__).resolve().parents[1] / "scripts" / "deploy" / "production-verify.sh").read_text()
    assert "catalog_register_compaction_mapper_mismatches()" in verify and \
        'fact DATABASE_READY NO "CATALOG_COMPACTION_MAPPER' in verify


def test_the_release_read_only_role_gains_maintain_on_the_two_compacted_tables_and_nothing_else():
    """The VACUUM path's only new power: this migration grants the read-only roles exactly these
    reads, and MAINTAIN (release role only, PostgreSQL 17) on exactly the two compacted tables;
    never a write privilege (the PostgreSQL test reads back that none exists anywhere)."""
    import re
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "supabase" / "migrations"
            / "20261002000100_catalog_register_compaction.sql").read_text()
    sql = re.sub(r"--[^\n]*", "", text)
    granted = sorted(" ".join(g.split()) for g in re.findall(r"format\('(grant [^']*) to %I',\s*ro\.rolname\)", sql))
    assert granted == [
        "grant execute on function public.catalog_register_compaction_mapper_mismatches()",
        "grant execute on function public.catalog_register_maintenance_blockers()",
        "grant execute on function public.catalog_variant_candidate_reading(public.catalog_variants)",
        "grant maintain on table public.catalog_raw_records, public.catalog_candidate_variants",
        "grant select on table public.catalog_candidate_variants_resolved",
        "grant select on table public.catalog_register_snapshot_compactions",
    ]
    # Every grant to a role the loop names goes through that list: no other grant names ro.
    assert len(re.findall(r"\bto %I',\s*ro\.rolname", sql)) == len(granted)
    # MAINTAIN only for the release read-only role, only where the privilege exists.
    guard = "if ro.rolname like 'milo\\_release\\_readonly\\_%' and current_setting('server_version_num')::integer >= 170000 then"
    assert sql.index(guard) < sql.index("grant maintain") and sql.count("grant maintain") == 1
    # No write privilege is granted to anyone but service_role, and no role is granted ALL.
    for grant in re.findall(r"\bgrant\s+([a-z, ]+?)\s+on\s+(?:table|all tables)[^;']*?\bto\s+([a-z_%I]+)", sql, re.I):
        privileges, grantee = grant
        if re.search(r"\b(insert|update|delete|truncate|all)\b", privileges, re.I):
            assert grantee == "service_role", grant

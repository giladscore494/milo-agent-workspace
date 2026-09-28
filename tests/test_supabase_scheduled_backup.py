"""PR-OBS OBS-1/OBS-2: the scheduled backup's dump/encrypt/upload tool and the
monthly restore test, executed end to end against ephemeral PostgreSQL and an
in-process fake of the Cloud Storage JSON API.

The source database is the confirmed baseline plus every migration, seeded
with SYNTHETIC rows only (the migration suite's own world builders), read
through a BYPASSRLS role that -- like production's read-only role -- holds
SELECT on every table and nothing on sequences.

PostgreSQL-backed: skipped only when no server binaries exist, and a FAILURE
instead when MILO_REQUIRE_PG_TESTS is set (tests/test_ci_workflow_static.py).
"""

from __future__ import annotations

import http.server
import importlib.util
import json
import os
import socket
import stat
import subprocess
import sys
import threading
import urllib.parse
from pathlib import Path

import pytest

from tests import test_migrations_postgres as pgm

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "scripts" / "ops" / "supabase_backup.py"
MIGRATIONS = REPO / "supabase" / "migrations"
PASSPHRASE = "synthetic-test-passphrase-7f3a9c"
SOURCE_PORT = "54995"
TARGET_PORT = "54996"
SECOND_TARGET_PORT = "54997"
RO_ROLE = "milo_backup_test_ro"


def _load_tool():
    spec = importlib.util.spec_from_file_location("milo_supabase_backup", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


backup = _load_tool()


# -- a fake Cloud Storage JSON API -------------------------------------------------

class FakeGcs:
    """The three calls the tool makes: media upload (with ifGenerationMatch),
    list, and alt=media download. Bearer token required on every call."""

    TOKEN = "fake-gcs-token"

    def __init__(self):
        self.objects: dict[tuple[str, str], bytes] = {}
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, status, body=b"", content_type="application/json"):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _authorized(self):
                if self.headers.get("Authorization") != f"Bearer {FakeGcs.TOKEN}":
                    self._reply(401)
                    return False
                return True

            def do_POST(self):
                if not self._authorized():
                    return
                parts = urllib.parse.urlsplit(self.path)
                query = dict(urllib.parse.parse_qsl(parts.query))
                bucket = urllib.parse.unquote(parts.path.split("/b/")[1].split("/")[0])
                data = self.rfile.read(int(self.headers["Content-Length"]))
                key = (bucket, query["name"])
                if query.get("ifGenerationMatch") != "0":
                    return self._reply(400)
                if key in fake.objects:
                    return self._reply(412, b'{"error": {"code": 412}}')
                fake.objects[key] = data
                import base64
                import hashlib
                meta = {"name": query["name"], "size": str(len(data)),
                        "md5Hash": base64.b64encode(hashlib.md5(data).digest()).decode()}
                self._reply(200, json.dumps(meta).encode())

            def do_GET(self):
                if not self._authorized():
                    return
                parts = urllib.parse.urlsplit(self.path)
                query = dict(urllib.parse.parse_qsl(parts.query))
                segments = parts.path.split("/")
                bucket = urllib.parse.unquote(segments[4])
                if len(segments) == 6 and segments[5] == "o":
                    items = [{"name": name, "size": str(len(data))}
                             for (b, name), data in sorted(fake.objects.items())
                             if b == bucket and name.startswith(query.get("prefix", ""))]
                    return self._reply(200, json.dumps({"items": items}).encode())
                name = urllib.parse.unquote(segments[6])
                data = fake.objects.get((bucket, name))
                if data is None or query.get("alt") != "media":
                    return self._reply(404)
                self._reply(200, data, "application/octet-stream")

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()


# -- fixtures -------------------------------------------------------------------------

@pytest.fixture(scope="module")
def pg_bin():
    return pgm._require_pg_bin()


@pytest.fixture(scope="module")
def source(pg_bin):
    server = pgm.EphemeralPostgres(pg_bin, port=SOURCE_PORT)
    server.start()
    try:
        server.create_database()
        server.psql(file=pgm.BASELINE)
        server.psql(sql=pgm.SEED_LEGACY_ROWS)
        server.psql(sql=pgm.SUPABASE_AUTH_SHIM)
        for migration in pgm.MIGRATIONS:
            server.psql(file=migration)
        world = pgm._wsp_world(server)
        pgm._coverage_seed(server, world, 0, "enriched")
        # Production's read-only role: SELECT on every table, BYPASSRLS, and
        # NO privilege on any sequence.
        server.psql(f"create role {RO_ROLE} login bypassrls; "
                    f"grant usage on schema public to {RO_ROLE}; "
                    f"grant select on all tables in schema public to {RO_ROLE};")
        assert server.psql(
            "select count(*) filter (where case when c.relkind = 'S' then "
            f"has_sequence_privilege('{RO_ROLE}', c.oid, 'SELECT') else false end) "
            "from pg_class c join pg_namespace n on n.oid = c.relnamespace where n.nspname = 'public'"
        ) == "0"
        yield server
    finally:
        server.stop()


def _url(server, role="postgres") -> str:
    return f"postgresql://{role}@/milo?host={server.dir}&port={server.port}"


def _env(pg_bin, **extra) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("MILO_BACKUP", "MILO_RESTORE", "MILO_GCS"))}
    env.update({"MILO_PG_BIN": pg_bin, "MILO_BACKUP_PASSPHRASE": PASSPHRASE,
                "GITHUB_RUN_ID": "4242", "GITHUB_SHA": "b" * 40})
    env.update(extra)
    return env


def _tool(env, *args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(TOOL), *args], env=env, capture_output=True,
                          text=True, timeout=600)


def _assert_nothing_secret(result: subprocess.CompletedProcess, *secrets: str) -> None:
    text = result.stdout + result.stderr
    for secret in (PASSPHRASE, FakeGcs.TOKEN, "postgresql://", "@/milo", *secrets):
        assert secret not in text, f"output leaked {secret!r}"


@pytest.fixture(scope="module")
def created(source, pg_bin, tmp_path_factory):
    out = tmp_path_factory.mktemp("backup") / "out"
    env = _env(pg_bin, MILO_BACKUP_DB_URL=_url(source, RO_ROLE))
    result = _tool(env, "create", "--out", str(out))
    assert result.returncode == 0, result.stdout + result.stderr
    _assert_nothing_secret(result, source.dir)
    return out, result


@pytest.fixture
def gcs():
    fake = FakeGcs()
    yield fake
    fake.close()


# -- create -------------------------------------------------------------------------

def test_create_leaves_exactly_the_encrypted_bundle_and_its_manifest(created):
    out, result = created
    names = sorted(path.name for path in out.iterdir())
    assert len(names) == 2
    bundle = next(out.glob("*.tar.gz.enc"))
    manifest = json.loads(next(out.glob("*.manifest.json")).read_text())
    assert manifest["format"] == "milo-supabase-scheduled-backup-v1"
    assert manifest["cipher"] == "aes-256-cbc" and manifest["kdf"] == "pbkdf2-sha256"
    assert manifest["pbkdf2_iterations"] == 600000
    assert manifest["encrypted_file"] == bundle.name
    assert manifest["encrypted_size_bytes"] == bundle.stat().st_size
    assert manifest["encrypted_sha256"] == backup.sha256_file(bundle)
    assert manifest["scope"]["schema"] == "public" and manifest["scope"]["data"] == "public"
    assert "excluded" in manifest["scope"]["sequence_data"]
    assert manifest["server_major"] == backup.tool_major("pg_dump") if os.environ.get("MILO_PG_BIN") \
        else manifest["server_major"] >= 15
    # No plaintext survived, anywhere in the output, and the files are private.
    assert not list(out.glob("*.dump")) and not list(out.glob("*.tar.gz"))
    assert stat.S_IMODE(bundle.stat().st_mode) & 0o077 == 0
    # Encrypted: the bundle is not a gzip stream and names no table.
    raw = bundle.read_bytes()
    assert raw[:8] == b"Salted__"
    assert b"catalog_raw_records" not in raw


def test_the_bundle_decrypts_with_the_manual_workflows_exact_command(created, tmp_path):
    out, _ = created
    bundle = next(out.glob("*.tar.gz.enc"))
    target = tmp_path / "plain.tar.gz"
    subprocess.run(["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "600000", "-md", "sha256",
                    "-in", str(bundle), "-out", str(target), "-pass", "env:P"],
                   env={**os.environ, "P": PASSPHRASE}, check=True, capture_output=True)
    subprocess.run(["tar", "-C", str(tmp_path), "-xzf", str(target)], check=True)
    subprocess.run(["sha256sum", "-c", "checksums.sha256"], cwd=tmp_path, check=True, capture_output=True)
    assert (tmp_path / "public.dump").stat().st_size > 0


def test_a_pg_dump_of_another_major_is_refused_clearly(source, pg_bin, tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for tool in ("psql", "pg_restore"):
        (fake_bin / tool).symlink_to(Path(pg_bin) / tool)
    fake = fake_bin / "pg_dump"
    fake.write_text("#!/bin/sh\necho 'pg_dump (PostgreSQL) 99.1'\n")
    fake.chmod(0o755)
    env = _env(pg_bin, MILO_PG_BIN=str(fake_bin), MILO_BACKUP_DB_URL=_url(source, RO_ROLE))
    result = _tool(env, "create", "--out", str(tmp_path / "out"))
    assert result.returncode == 1
    assert "FAIL PG_DUMP_VERSION_MISMATCH: pg_dump major 99 does not match the server major" in result.stdout
    assert not (tmp_path / "out").exists() or not any((tmp_path / "out").iterdir())
    _assert_nothing_secret(result, source.dir)


def test_a_connection_failure_prints_no_connection_string(pg_bin, tmp_path):
    env = _env(pg_bin, MILO_BACKUP_DB_URL="postgresql://someone.secret:hunter2@127.0.0.1:1/db")
    result = _tool(env, "create", "--out", str(tmp_path / "out"))
    assert result.returncode == 1
    assert result.stdout.startswith("FAIL SOURCE_UNREACHABLE")
    _assert_nothing_secret(result, "hunter2", "someone.secret")


def test_missing_configuration_is_refused_by_name_only(pg_bin, tmp_path):
    result = _tool(_env(pg_bin), "create", "--out", str(tmp_path / "out"))
    assert result.returncode == 1
    assert result.stdout.strip() == "FAIL CONFIG_MISSING: MILO_BACKUP_DB_URL is not set"


# -- upload / fetch --------------------------------------------------------------------

def test_upload_is_create_only_and_fetch_latest_returns_the_newest(created, gcs, pg_bin, tmp_path):
    out, _ = created
    env = _env(pg_bin, MILO_GCS_ENDPOINT=gcs.endpoint, MILO_GCS_ACCESS_TOKEN=FakeGcs.TOKEN)
    manifest = json.loads(next(out.glob("*.manifest.json")).read_text())
    date = manifest["created_at"][:10]

    # An older backup already in the bucket: fetch-latest must not pick it.
    older = f"supabase/2000-01-01/milo-supabase-public-20000101T000000Z-1"
    gcs.objects[("milo-test-backups", older + ".manifest.json")] = b"{}"

    first = _tool(env, "upload", "--dir", str(out), "--bucket", "milo-test-backups")
    assert first.returncode == 0, first.stdout
    names = sorted(name for _, name in gcs.objects)
    assert f"supabase/{date}/{manifest['encrypted_file']}" in names
    assert f"supabase/{date}/{manifest['encrypted_file'].replace('.tar.gz.enc', '.manifest.json')}" in names
    _assert_nothing_secret(first)

    # The precondition: the same object is never overwritten.
    again = _tool(env, "upload", "--dir", str(out), "--bucket", "milo-test-backups")
    assert again.returncode == 1
    assert again.stdout.startswith("FAIL BACKUP_OBJECT_EXISTS")

    fetched = tmp_path / "fetched"
    result = _tool(env, "fetch-latest", "--bucket", "milo-test-backups", "--out", str(fetched))
    assert result.returncode == 0, result.stdout
    assert (fetched / manifest["encrypted_file"]).read_bytes() == (out / manifest["encrypted_file"]).read_bytes()


def test_fetch_refuses_a_bundle_that_does_not_match_its_manifest(created, gcs, pg_bin, tmp_path):
    out, _ = created
    env = _env(pg_bin, MILO_GCS_ENDPOINT=gcs.endpoint, MILO_GCS_ACCESS_TOKEN=FakeGcs.TOKEN)
    assert _tool(env, "upload", "--dir", str(out), "--bucket", "milo-test-backups").returncode == 0
    for key in list(gcs.objects):
        if key[1].endswith(".tar.gz.enc"):
            gcs.objects[key] = gcs.objects[key][:-1] + b"\x00"
    result = _tool(env, "fetch-latest", "--bucket", "milo-test-backups", "--out", str(tmp_path / "f"))
    assert result.returncode == 1
    assert result.stdout.startswith("FAIL BUNDLE_MISMATCH")


def test_an_empty_bucket_has_no_latest_backup(gcs, pg_bin, tmp_path):
    env = _env(pg_bin, MILO_GCS_ENDPOINT=gcs.endpoint, MILO_GCS_ACCESS_TOKEN=FakeGcs.TOKEN)
    result = _tool(env, "fetch-latest", "--bucket", "milo-test-backups", "--out", str(tmp_path / "f"))
    assert result.returncode == 1
    assert result.stdout.startswith("FAIL NO_BACKUP_FOUND")


# -- restore ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def restored(created, pg_bin):
    out, _ = created
    server = pgm.EphemeralPostgres(pg_bin, port=TARGET_PORT)
    server.start()
    try:
        server.create_database()
        env = _env(pg_bin, MILO_RESTORE_DB_URL=_url(server))
        result = _tool(env, "restore", "--dir", str(out), "--migrations", str(MIGRATIONS))
        yield server, result
    finally:
        server.stop()


def test_restore_verifies_tables_and_prints_counts_only(restored, source):
    server, result = restored
    assert result.returncode == 0, result.stdout + result.stderr
    lines = result.stdout.strip().splitlines()
    counts = {line.split()[1]: int(line.split()[2]) for line in lines if line.startswith("COUNT ")}
    assert set(counts) == {"catalog_raw_records", "catalog_variant_coverage", "runs"}
    for table, count in counts.items():
        assert count == int(source.psql(f"select count(*) from public.{table}")) > 0
    assert lines[-1].startswith("PASS restore verified:")
    assert all(line.startswith(("COUNT ", "PASS ")) for line in lines)
    _assert_nothing_secret(result, server.dir)


def test_restore_reproduces_every_row_and_every_migration_table(restored, source):
    server, _ = restored
    expected = backup.migration_tables(MIGRATIONS)
    present = set(server.psql("select tablename from pg_tables where schemaname = 'public'").splitlines())
    assert set(expected) <= present
    for table in ("catalog_raw_records", "catalog_candidate_variants", "catalog_variant_coverage", "runs"):
        digest = f"select md5(string_agg(t::text, ',' order by t::text)) from public.{table} t"
        assert server.psql(digest) == source.psql(digest), table


def test_restore_resets_owned_sequences_past_the_restored_rows(restored):
    server, _ = restored
    owned = int(server.psql(
        "select count(*) from pg_depend d join pg_class s on s.oid = d.objid "
        "where d.classid = 'pg_class'::regclass and s.relkind = 'S' and d.deptype in ('a', 'i')"))
    assert owned > 0
    # For every sequence OWNED by a public column, the next value it hands out
    # is above the largest restored value (the dump carries no sequence data).
    server.psql("""
        do $$
        declare r record; top bigint; nxt bigint;
        begin
          for r in
            select s.oid::regclass as seq, t.oid::regclass as tbl, a.attname as col
            from pg_class s join pg_namespace n on n.oid = s.relnamespace and n.nspname = 'public'
            join pg_depend d on d.classid = 'pg_class'::regclass and d.objid = s.oid
              and d.refclassid = 'pg_class'::regclass and d.deptype in ('a', 'i')
            join pg_class t on t.oid = d.refobjid
            join pg_attribute a on a.attrelid = t.oid and a.attnum = d.refobjsubid
            where s.relkind = 'S'
          loop
            execute format('select max(%I)::bigint from %s', r.col, r.tbl) into top;
            execute format('select case when is_called then last_value + 1 else last_value end from %s',
                           r.seq) into nxt;
            if nxt <= coalesce(top, 0) then
              raise exception 'sequence % would reuse %', r.seq, top;
            end if;
          end loop;
        end $$""")


def test_restore_refuses_a_database_that_is_not_fresh(restored, created, pg_bin):
    server, _ = restored
    out, _ = created
    env = _env(pg_bin, MILO_RESTORE_DB_URL=_url(server))
    result = _tool(env, "restore", "--dir", str(out), "--migrations", str(MIGRATIONS))
    assert result.returncode == 1
    assert result.stdout.startswith("FAIL RESTORE_TARGET_NOT_EMPTY")


def test_restore_with_the_wrong_passphrase_fails_before_touching_the_target(created, pg_bin):
    out, _ = created
    server = pgm.EphemeralPostgres(pg_bin, port=SECOND_TARGET_PORT)
    server.start()
    try:
        server.create_database()
        env = _env(pg_bin, MILO_RESTORE_DB_URL=_url(server), MILO_BACKUP_PASSPHRASE="wrong-passphrase")
        result = _tool(env, "restore", "--dir", str(out), "--migrations", str(MIGRATIONS))
        assert result.returncode == 1
        assert result.stdout.startswith("FAIL DECRYPT_FAILED")
        assert server.psql("select count(*) from pg_tables where schemaname = 'public'") == "0"
        _assert_nothing_secret(result, "wrong-passphrase")
    finally:
        server.stop()


# -- pure helpers --------------------------------------------------------------------------

def test_migration_tables_are_real_tables_of_the_migrated_schema(source):
    expected = backup.migration_tables(MIGRATIONS)
    present = set(source.psql("select tablename from pg_tables where schemaname = 'public'").splitlines())
    assert set(expected) <= present, sorted(set(expected) - present)
    for table in ("catalog_raw_records", "catalog_variant_coverage", "catalog_source_snapshots"):
        assert table in expected


def test_migration_table_parser_follows_create_drop_and_rename(tmp_path):
    (tmp_path / "001.sql").write_text(
        "create table if not exists public.a (id int);\n"
        "-- create table public.commented (id int);\n"
        "CREATE TABLE b(id int);\ncreate table auth.users (id uuid);\n"
        "create temp table scratch (id int);\n")
    (tmp_path / "002.sql").write_text("drop table if exists public.b;\nalter table public.a rename to c;\n")
    assert backup.migration_tables(tmp_path) == ["c"]


@pytest.mark.parametrize("raw", [
    'connection to server at "db.abcdefgh.supabase.co" (10.1.2.3), port 5432 failed: FATAL: password authentication failed for user "milo_ro.abc"',
    "could not connect postgresql://u:secretpw@host:5432/db",
    "user=someone password=secretpw host=db.example",
    "notify ops@example.com Bearer ya29.a0AfH6SMsecret",
])
def test_redaction_removes_connection_identity_and_token_material(raw):
    line = backup.redact(raw)
    for secret in ("supabase.co", "10.1.2.3", "milo_ro", "secretpw", "ops@example.com", "ya29.a0AfH6SMsecret",
                   "someone", "db.example"):
        assert secret not in line

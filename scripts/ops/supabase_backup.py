#!/usr/bin/env python3
"""Scheduled Supabase backup to Cloud Storage, and its monthly restore test.

Used by .github/workflows/backup-supabase-scheduled.yml (PR-OBS, OBS-1/OBS-2).
Standard library only; the PostgreSQL client tools, openssl and tar are
called as subprocesses.

Subcommands
-----------
server-major
    Print the source server's major version (digits only).
create --out DIR
    pg_dump (custom format) of schema ``public``, schema AND data, through the
    read-only role; the pg_dump major must EQUAL the server major. Bundle,
    encrypt exactly like the manual workflow (aes-256-cbc, pbkdf2-sha256,
    600,000 iterations), decrypt again and verify the checksums, write the
    manifest, delete every plaintext file. DIR then holds exactly two files.
upload --dir DIR --bucket BUCKET
    Create-only (ifGenerationMatch=0) upload of the encrypted bundle, then
    its manifest, to gs://BUCKET/supabase/<UTC date>/.
fetch-latest --bucket BUCKET --out DIR
    The newest manifest under supabase/ and the bundle it names; the bundle's
    size and sha256 must match the manifest.
restore --dir DIR --migrations DIR
    Decrypt, verify, restore into the target database (a throwaway
    PostgreSQL of the SAME major), then verify: every table the migrations
    create exists, and catalog_raw_records, catalog_variant_coverage and runs
    are non-empty. Prints counts only.

Environment
-----------
MILO_BACKUP_DB_URL      source connection URL (the read-only role)
MILO_BACKUP_PASSPHRASE  encryption passphrase
MILO_RESTORE_DB_URL     restore target (restore only)
MILO_PG_BIN             directory holding pg_dump / pg_restore / psql of the
                        server's major (default: those on PATH)
MILO_GCS_ACCESS_TOKEN   OAuth token for Cloud Storage (default:
                        ``gcloud auth print-access-token``)
MILO_GCS_ENDPOINT       Cloud Storage endpoint (tests only)

Nothing this tool prints contains a connection string, a passphrase, a token
or an e-mail address: subprocess errors are reduced to one redacted line, and
no command line is ever echoed.

Sequence data
-------------
The read-only role holds SELECT on every table but not on sequences, and
pg_dump reads a sequence's current value with ``SELECT last_value FROM seq``.
The dump therefore EXCLUDES sequence DATA (the definitions are kept) and the
restore resets every owned sequence to max(column)+1 -- the value the next
insert needs. The manifest says so.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import http.client
import socket
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

FORMAT = "milo-supabase-scheduled-backup-v1"
PREFIX = "supabase/"
DUMP_NAME = "public.dump"
INFO_NAME = "dump-info.json"
CHECKSUMS_NAME = "checksums.sha256"
BUNDLE_SUFFIX = ".tar.gz.enc"
MANIFEST_SUFFIX = ".manifest.json"
REQUIRED_NONEMPTY_TABLES = ("catalog_raw_records", "catalog_variant_coverage", "runs")
DEFAULT_GCS_ENDPOINT = "https://storage.googleapis.com"
HTTP_TIMEOUT_SECONDS = 300
#: Cloud Storage calls: attempts, and the backoff before each retry (seconds).
HTTP_ATTEMPTS = 4
HTTP_BACKOFF_SECONDS = (2, 4, 8)
RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
#: The restore test refuses a newest backup older than this: a silently
#: stopped daily backup must not pass the monthly test on an old bundle.
DEFAULT_MAX_BACKUP_AGE_HOURS = 48

#: The objects Supabase provides outside ``public`` that the public schema
#: references (auth.users foreign keys, auth.uid() in policies, the API
#: roles in policies, pgcrypto). A vanilla PostgreSQL gets inert stand-ins
#: before the restore; nothing in them is restored data.
RESTORE_SHIM_SQL = """
create schema if not exists auth;
create table if not exists auth.users (id uuid primary key);
create or replace function auth.uid() returns uuid language sql stable as $$
  select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid
$$;
do $$
declare name text;
begin
  foreach name in array array['anon', 'authenticated', 'service_role'] loop
    if not exists (select 1 from pg_roles where rolname = name) then
      execute format('create role %I nologin', name);
    end if;
  end loop;
end $$;
create schema if not exists extensions;
create extension if not exists pgcrypto with schema extensions;
"""

#: Every sequence OWNED by a public column is set so the next value follows
#: the largest one restored (sequence data is not in the dump; see above).
RESET_SEQUENCES_SQL = """
do $$
declare r record; top bigint;
begin
  for r in
    select s.oid::regclass as seq, t.oid::regclass as tbl, a.attname as col
    from pg_class s
    join pg_namespace n on n.oid = s.relnamespace and n.nspname = 'public'
    join pg_depend d on d.classid = 'pg_class'::regclass and d.objid = s.oid
      and d.refclassid = 'pg_class'::regclass and d.deptype in ('a', 'i')
    join pg_class t on t.oid = d.refobjid
    join pg_attribute a on a.attrelid = t.oid and a.attnum = d.refobjsubid
    where s.relkind = 'S'
  loop
    execute format('select max(%I)::bigint from %s', r.col, r.tbl) into top;
    perform setval(r.seq, coalesce(top, 0) + 1, false);
  end loop;
end $$;
"""


class BackupError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# -- output hygiene ------------------------------------------------------------

_REDACTIONS = [
    (re.compile(r"postgres(?:ql)?://\S+", re.I), "<connection>"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "<email>"),
    (re.compile(r"\b(?:host|hostaddr|user|password|dbname|port)\s*=\s*\S+", re.I), "<param>"),
    (re.compile(r'"[^"]*"'), '"<redacted>"'),
    (re.compile(r"\(\d{1,3}(?:\.\d{1,3}){3}\)|\b\d{1,3}(?:\.\d{1,3}){3}\b"), "<address>"),
    (re.compile(r"ya29\.[A-Za-z0-9._-]+|Bearer\s+\S+", re.I), "<token>"),
]


def redact(text: str, limit: int = 240) -> str:
    """One line -- the first that says ``error:``/``FATAL:``, else the first --
    with anything connection-, identity- or token-shaped removed."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    first = next((line for line in lines if re.search(r"\b(error|fatal):", line, re.I)),
                 lines[0] if lines else "")
    for pattern, replacement in _REDACTIONS:
        first = pattern.sub(replacement, first)
    return first[:limit]


def say(line: str) -> None:
    print(line, flush=True)


# -- helpers ---------------------------------------------------------------------

def _env(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise BackupError("CONFIG_MISSING", f"{name} is not set")
    return value


def _tool(name: str) -> str:
    base = (os.environ.get("MILO_PG_BIN") or "").strip()
    path = os.path.join(base, name) if base else shutil.which(name)
    if not path or not os.access(path, os.X_OK):
        raise BackupError("PG_TOOL_MISSING", f"{name} was not found"
                          + (" in MILO_PG_BIN" if base else " on PATH"))
    return path


def _run(cmd: list[str], code: str, *, env: dict[str, str] | None = None,
         stdout=subprocess.PIPE, input_text: str | None = None) -> str:
    """Run a command; on failure raise ``code`` with ONE redacted stderr line.

    The command line itself is never shown: it can hold a connection URL.
    """
    result = subprocess.run(cmd, stdout=stdout, stderr=subprocess.PIPE, text=True,
                            env=env, input=input_text)
    if result.returncode != 0:
        detail = redact(result.stderr)
        raise BackupError(code, f"{Path(cmd[0]).name} exited {result.returncode}"
                          + (f": {detail}" if detail else ""))
    return result.stdout if isinstance(result.stdout, str) else ""


#: libpq URL query parameters and the environment variable each becomes.
_LIBPQ_PARAMS = {
    "host": "PGHOST", "hostaddr": "PGHOSTADDR", "port": "PGPORT", "dbname": "PGDATABASE",
    "user": "PGUSER", "password": "PGPASSWORD", "sslmode": "PGSSLMODE",
    "sslrootcert": "PGSSLROOTCERT", "sslcert": "PGSSLCERT", "sslkey": "PGSSLKEY",
    "connect_timeout": "PGCONNECT_TIMEOUT", "application_name": "PGAPPNAME", "options": "PGOPTIONS",
    "target_session_attrs": "PGTARGETSESSIONATTRS", "channel_binding": "PGCHANNELBINDING",
    "gssencmode": "PGGSSENCMODE",
}


def pg_env(url: str) -> dict[str, str]:
    """The connection URL as libpq ENVIRONMENT variables, so it never reaches
    a command line (argv is readable by other processes of the same user).

    Refuses (by parameter NAME only) anything it cannot translate exactly.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in {"postgres", "postgresql"}:
        raise BackupError("CONFIG_INVALID", "the database URL is not a postgresql:// URL")
    netloc = parts.netloc
    userinfo, _, hostport = netloc.rpartition("@") if "@" in netloc else ("", "", netloc)
    if "," in hostport:
        raise BackupError("CONFIG_INVALID", "multi-host database URLs are not supported")
    env = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    values: dict[str, str] = {}
    if userinfo:
        user, _, password = userinfo.partition(":")
        values["user"] = urllib.parse.unquote(user)
        if password:
            values["password"] = urllib.parse.unquote(password)
    if hostport:
        host, _, port = hostport.rpartition(":") if not hostport.endswith("]") and ":" in hostport \
            else (hostport, "", "")
        values["host"] = urllib.parse.unquote(host.strip("[]"))
        if port:
            values["port"] = port
    dbname = urllib.parse.unquote(parts.path.lstrip("/"))
    if dbname:
        values["dbname"] = dbname
    for name, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True):
        if name not in _LIBPQ_PARAMS:
            raise BackupError("CONFIG_INVALID", f"unsupported database URL parameter {name!r}")
        values[name] = value
    for name, value in values.items():
        if value:
            env[_LIBPQ_PARAMS[name]] = value
    return env


def _pg(tool: str, args: list[str], url: str, code: str, *, input_text: str | None = None) -> str:
    """Run a PostgreSQL client tool against ``url`` (passed via the environment)."""
    return _run([_tool(tool), *args], code, env=pg_env(url), input_text=input_text)


def _psql(url: str, sql: str, code: str) -> str:
    return _pg("psql", ["-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1"], url, code, input_text=sql).strip()


def tool_major(name: str) -> int:
    out = _run([_tool(name), "--version"], "PG_TOOL_MISSING")
    match = re.search(r"\(PostgreSQL\)\s+(\d+)", out) or re.search(r"\s(\d+)(?:\.\d+)?", out)
    if not match:
        raise BackupError("PG_TOOL_VERSION_UNREADABLE", f"{name} --version could not be read")
    return int(match.group(1))


def server_major(url: str) -> int:
    raw = _psql(url, "show server_version_num;", "SOURCE_UNREACHABLE")
    if not raw.isdigit():
        raise BackupError("SOURCE_VERSION_UNREADABLE", "server_version_num could not be read")
    return int(raw) // 10000


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def md5_b64_file(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return base64.b64encode(digest.digest()).decode()


def _openssl(decrypt: bool, source: Path, target: Path) -> None:
    """The manual workflow's exact cipher: aes-256-cbc, pbkdf2-sha256, 600,000
    iterations; the passphrase is read from the environment, never argv."""
    _env("MILO_BACKUP_PASSPHRASE")
    if decrypt:
        cmd = ["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "600000", "-md", "sha256"]
    else:
        cmd = ["openssl", "enc", "-aes-256-cbc", "-salt", "-pbkdf2", "-iter", "600000", "-md", "sha256"]
    cmd += ["-in", str(source), "-out", str(target), "-pass", "env:MILO_BACKUP_PASSPHRASE"]
    _run(cmd, "DECRYPT_FAILED" if decrypt else "ENCRYPT_FAILED")


def _write_checksums(directory: Path, names: list[str]) -> None:
    lines = [f"{sha256_file(directory / name)}  {name}\n" for name in names]
    (directory / CHECKSUMS_NAME).write_text("".join(lines), encoding="utf-8")


def _verify_checksums(directory: Path) -> list[str]:
    path = directory / CHECKSUMS_NAME
    if not path.is_file():
        raise BackupError("CHECKSUMS_MISSING", "the bundle has no checksum list")
    names = []
    for line in path.read_text(encoding="utf-8").splitlines():
        digest, _, name = line.partition("  ")
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or "/" in name or not name:
            raise BackupError("CHECKSUMS_INVALID", "the checksum list is malformed")
        if not (directory / name).is_file() or sha256_file(directory / name) != digest:
            raise BackupError("CHECKSUM_MISMATCH", f"{name} does not match its checksum")
        names.append(name)
    return names


def _safe_extract(archive: Path, target: Path) -> None:
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile() or "/" in member.name or member.name.startswith("."):
                raise BackupError("BUNDLE_INVALID", "the bundle holds an unexpected entry")
        tar.extractall(target, filter="data")


# -- create --------------------------------------------------------------------

def _scratch_dir() -> str | None:
    runner_temp = (os.environ.get("RUNNER_TEMP") or "").strip()
    return runner_temp if runner_temp and os.path.isdir(runner_temp) else None


def _public_sequences(url: str) -> list[str]:
    raw = _psql(url, "select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace "
                     "where n.nspname = 'public' and c.relkind = 'S' order by 1;", "SOURCE_QUERY_FAILED")
    return [line for line in raw.splitlines() if line]


def create(out: Path) -> dict:
    url = _env("MILO_BACKUP_DB_URL")
    _env("MILO_BACKUP_PASSPHRASE")
    source_major = server_major(url)
    dump_major = tool_major("pg_dump")
    if dump_major != source_major:
        raise BackupError("PG_DUMP_VERSION_MISMATCH",
                          f"pg_dump major {dump_major} does not match the server major {source_major}; "
                          f"install postgresql-client-{source_major} and point MILO_PG_BIN at it")
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise BackupError("OUTPUT_NOT_EMPTY", "the output directory must be empty")
    os.umask(0o077)
    created_at = datetime.now(UTC)
    stamp = created_at.strftime("%Y%m%dT%H%M%SZ")
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    stem = f"milo-supabase-public-{stamp}-{run_id}"
    # Plaintext only under the runner's own temp dir (cleaned with the job).
    work = Path(tempfile.mkdtemp(prefix="milo-backup-plain-", dir=_scratch_dir()))
    verify = Path(tempfile.mkdtemp(prefix="milo-backup-verify-", dir=_scratch_dir()))
    try:
        sequences = _public_sequences(url)
        cmd = ["--format=custom", "--schema=public", "--no-password",
               "--lock-wait-timeout=120s", f"--file={work / DUMP_NAME}"]
        for name in sequences:
            cmd.append("--exclude-table-data=public.\"" + name.replace('"', '""') + "\"")
        _pg("pg_dump", cmd, url, "PG_DUMP_FAILED")
        listing = _run([_tool("pg_restore"), "--list", str(work / DUMP_NAME)], "DUMP_UNREADABLE")
        tables = sum(1 for line in listing.splitlines() if re.search(r"\sTABLE public \S+ ", line))
        table_data = sum(1 for line in listing.splitlines() if " TABLE DATA public " in line)
        if tables == 0 or table_data == 0:
            raise BackupError("DUMP_EMPTY", "the dump holds no public tables or no table data")
        pg_dump_version = _run([_tool("pg_dump"), "--version"], "PG_TOOL_MISSING").strip()
        info = {"server_major": source_major, "pg_dump_version": pg_dump_version,
                "tables": tables, "sequence_data_excluded": len(sequences)}
        (work / INFO_NAME).write_text(json.dumps(info, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        _write_checksums(work, [DUMP_NAME, INFO_NAME])
        plain = work / f"{stem}.tar.gz"
        with tarfile.open(plain, "w:gz") as tar:
            for name in (DUMP_NAME, INFO_NAME, CHECKSUMS_NAME):
                tar.add(work / name, arcname=name)
        bundle = out / f"{stem}{BUNDLE_SUFFIX}"
        _openssl(False, plain, bundle)
        # Decryptability and checksums, before the plaintext is gone.
        _openssl(True, bundle, verify / "bundle.tar.gz")
        _safe_extract(verify / "bundle.tar.gz", verify)
        _verify_checksums(verify)
        manifest = {
            "format": FORMAT,
            "encrypted_file": bundle.name,
            "encrypted_sha256": sha256_file(bundle),
            "encrypted_size_bytes": bundle.stat().st_size,
            "created_at": created_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source_sha": os.environ.get("GITHUB_SHA", ""),
            "workflow_run_id": run_id,
            "cipher": "aes-256-cbc",
            "kdf": "pbkdf2-sha256",
            "pbkdf2_iterations": 600000,
            "server_major": source_major,
            "pg_dump_version": pg_dump_version,
            "dump_format": "pg_dump custom (-Fc)",
            "scope": {"schema": "public", "data": "public", "owners_and_acls": "included",
                      "roles_included": False,
                      "sequence_data": "excluded; restore resets each owned sequence to max(column)+1",
                      "managed_schemas_excluded": ["auth", "storage"]},
            "tables": tables,
        }
        manifest_path = out / f"{stem}{MANIFEST_SUFFIX}"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(verify, ignore_errors=True)
    remaining = sorted(path.name for path in out.iterdir())
    if remaining != sorted([bundle.name, manifest_path.name]):
        raise BackupError("PLAINTEXT_LEFT", "the output directory holds more than the bundle and manifest")
    say(f"PASS backup created: {bundle.name} ({manifest['encrypted_size_bytes']} bytes encrypted, "
        f"{tables} tables, server major {source_major}); decryptability and checksums verified; "
        "plaintext removed")
    return manifest


# -- Cloud Storage (JSON API) --------------------------------------------------

def _token() -> str:
    token = (os.environ.get("MILO_GCS_ACCESS_TOKEN") or "").strip()
    if token:
        return token
    return _run(["gcloud", "auth", "print-access-token"], "GCS_AUTH_FAILED").strip()


def _endpoint() -> str:
    return (os.environ.get("MILO_GCS_ENDPOINT") or DEFAULT_GCS_ENDPOINT).rstrip("/")


def _bucket(name: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]", name or ""):
        raise BackupError("BUCKET_INVALID", "the bucket name is not a valid Cloud Storage bucket name")
    return name


_TRANSIENT = (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError,
              http.client.HTTPException)


def _call(method: str, url: str, *, body_path: Path | None = None, headers: dict | None = None,
          sink: Path | None = None) -> tuple[int, bytes, int]:
    """One Cloud Storage call with bounded retries on transient failures.

    Returns (status, body, attempt). The body is streamed to ``sink`` when
    given (a download). A retryable status or a connection error is retried
    up to HTTP_ATTEMPTS times; anything else is returned as is. Never raises a
    raw network exception: exhaustion is GCS_UNREACHABLE, by class name only.
    """
    last = ""
    for attempt in range(1, HTTP_ATTEMPTS + 1):
        if attempt > 1:
            # MILO_GCS_BACKOFF_SCALE exists for tests only (0 = no wait).
            scale = float(os.environ.get("MILO_GCS_BACKOFF_SCALE") or 1)
            time.sleep(HTTP_BACKOFF_SECONDS[min(attempt - 2, len(HTTP_BACKOFF_SECONDS) - 1)] * scale)
        handle = body_path.open("rb") if body_path is not None else None
        try:
            request = urllib.request.Request(url, data=handle, method=method, headers={
                "Authorization": f"Bearer {_token()}", **(headers or {})})
            try:
                response = urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS)
            except urllib.error.HTTPError as exc:
                response = exc
            status = getattr(response, "status", None) or response.getcode()
            if status in RETRYABLE_STATUSES:
                response.read()
                last = f"HTTP {status}"
                continue
            if sink is not None and status == 200:
                with sink.open("wb") as out:
                    shutil.copyfileobj(response, out, 1 << 20)
                return status, b"", attempt
            return status, response.read(), attempt
        except _TRANSIENT as exc:
            last = type(exc).__name__
            continue
        finally:
            if handle is not None:
                handle.close()
    raise BackupError("GCS_UNREACHABLE", f"Cloud Storage {method} failed after {HTTP_ATTEMPTS} attempts ({last})")


def upload_object(bucket: str, name: str, path: Path) -> dict:
    """Create-only upload: ifGenerationMatch=0 refuses an existing object."""
    query = urllib.parse.urlencode({"uploadType": "media", "name": name, "ifGenerationMatch": "0"})
    url = f"{_endpoint()}/upload/storage/v1/b/{urllib.parse.quote(bucket, safe='')}/o?{query}"
    size = path.stat().st_size
    status, body, attempt = _call("POST", url, body_path=path, headers={
        "Content-Type": "application/octet-stream", "Content-Length": str(size)})
    if status == 412 and attempt > 1:
        # A retried create may have landed the first time; the writer cannot
        # read the object to tell. Fail visibly rather than guess.
        raise BackupError("GCS_UPLOAD_UNCERTAIN", f"gs://{bucket}/{name} exists after a retried upload; "
                          "an earlier attempt may have stored it (the writer cannot read it to check)")
    if status == 412:
        raise BackupError("BACKUP_OBJECT_EXISTS", f"gs://{bucket}/{name} already exists (create-only upload refused)")
    if status != 200:
        raise BackupError("GCS_UPLOAD_FAILED", f"upload of {name} failed with HTTP {status}")
    try:
        meta = json.loads(body)
    except ValueError:
        raise BackupError("GCS_UPLOAD_FAILED", f"upload of {name} returned an unreadable answer")
    if str(meta.get("size")) != str(size) or meta.get("md5Hash") not in (None, md5_b64_file(path)):
        raise BackupError("GCS_UPLOAD_MISMATCH", f"the stored {name} does not match the local file")
    return meta


def upload(directory: Path, bucket: str) -> str:
    bucket = _bucket(bucket)
    manifests = sorted(directory.glob(f"*{MANIFEST_SUFFIX}"))
    if len(manifests) != 1:
        raise BackupError("MANIFEST_MISSING", "exactly one manifest is expected in the backup directory")
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    bundle = directory / manifest["encrypted_file"]
    if not bundle.is_file() or sha256_file(bundle) != manifest["encrypted_sha256"]:
        raise BackupError("BUNDLE_MISMATCH", "the bundle does not match its manifest")
    date = manifest["created_at"][:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise BackupError("MANIFEST_INVALID", "the manifest has no creation date")
    prefix = f"{PREFIX}{date}/"
    # The bundle first: a manifest never names a bundle that is not stored.
    upload_object(bucket, prefix + bundle.name, bundle)
    upload_object(bucket, prefix + manifests[0].name, manifests[0])
    say(f"PASS uploaded gs://{bucket}/{prefix}{bundle.name} and its manifest (create-only)")
    return prefix + bundle.name


def _list(bucket: str, prefix: str) -> list[dict]:
    items: list[dict] = []
    token = None
    for _ in range(1000):
        params = {"prefix": prefix, "fields": "items(name,size),nextPageToken", "maxResults": "1000"}
        if token:
            params["pageToken"] = token
        url = f"{_endpoint()}/storage/v1/b/{urllib.parse.quote(bucket, safe='')}/o?{urllib.parse.urlencode(params)}"
        status, body, _ = _call("GET", url)
        if status != 200:
            raise BackupError("GCS_LIST_FAILED", f"listing gs://{bucket}/{prefix} failed with HTTP {status}")
        try:
            page = json.loads(body or b"{}")
        except ValueError:
            raise BackupError("GCS_LIST_FAILED", "the listing was not readable")
        items += page.get("items") or []
        token = page.get("nextPageToken")
        if not token:
            return items
    raise BackupError("GCS_LIST_FAILED", "the listing did not end")


def download_object(bucket: str, name: str, target: Path) -> None:
    url = (f"{_endpoint()}/storage/v1/b/{urllib.parse.quote(bucket, safe='')}/o/"
           f"{urllib.parse.quote(name, safe='')}?alt=media")
    status, _, _ = _call("GET", url, sink=target)
    if status != 200:
        raise BackupError("GCS_DOWNLOAD_FAILED", f"download of {name} failed with HTTP {status}")


def fetch_latest(bucket: str, out: Path, max_age_hours: float = DEFAULT_MAX_BACKUP_AGE_HOURS) -> dict:
    bucket = _bucket(bucket)
    out.mkdir(parents=True, exist_ok=True)
    manifests = sorted(item["name"] for item in _list(bucket, PREFIX)
                       if item.get("name", "").endswith(MANIFEST_SUFFIX))
    if not manifests:
        raise BackupError("NO_BACKUP_FOUND", f"no backup manifest under gs://{bucket}/{PREFIX}")
    latest = manifests[-1]
    manifest_path = out / Path(latest).name
    download_object(bucket, latest, manifest_path)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError:
        manifest = {}
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT \
            or not re.fullmatch(r"[A-Za-z0-9._-]+", str(manifest.get("encrypted_file", ""))):
        raise BackupError("MANIFEST_INVALID", f"{latest} is not a {FORMAT} manifest")
    try:
        created = datetime.strptime(manifest["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except (KeyError, TypeError, ValueError):
        raise BackupError("MANIFEST_INVALID", f"{latest} has no valid created_at")
    age_hours = (datetime.now(UTC) - created).total_seconds() / 3600
    if age_hours > max_age_hours:
        raise BackupError("BACKUP_STALE", f"the newest backup ({manifest['created_at']}) is {age_hours:.0f}h old; "
                          f"more than {max_age_hours:.0f}h means the daily backup has stopped")
    bundle_name = latest.rsplit("/", 1)[0] + "/" + manifest["encrypted_file"]
    bundle = out / manifest["encrypted_file"]
    download_object(bucket, bundle_name, bundle)
    if bundle.stat().st_size != manifest["encrypted_size_bytes"] or sha256_file(bundle) != manifest["encrypted_sha256"]:
        raise BackupError("BUNDLE_MISMATCH", "the downloaded bundle does not match its manifest")
    say(f"PASS latest backup: gs://{bucket}/{bundle_name} ({manifest['encrypted_size_bytes']} bytes, "
        f"created {manifest['created_at']}, server major {manifest['server_major']})")
    return manifest


# -- restore -------------------------------------------------------------------

_SQL_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)
_IDENT = r'(?:"[^"]+"|[A-Za-z_][A-Za-z0-9_$]*)'
_CREATE_TABLE = re.compile(
    rf"\bcreate\s+(?:unlogged\s+)?table\s+(?:if\s+not\s+exists\s+)?(?:(?:public)\s*\.\s*)?({_IDENT})\s*\(",
    re.I)
_DROP_TABLE = re.compile(rf"\bdrop\s+table\s+(?:if\s+exists\s+)?(?:public\s*\.\s*)?({_IDENT})", re.I)
_RENAME_TABLE = re.compile(
    rf"\balter\s+table\s+(?:if\s+exists\s+)?(?:only\s+)?(?:public\s*\.\s*)?({_IDENT})\s+rename\s+to\s+({_IDENT})",
    re.I)


def _unquote(identifier: str) -> str:
    return identifier[1:-1] if identifier.startswith('"') else identifier.lower()


def migration_tables(migrations: Path) -> list[str]:
    """Every public table the migrations leave in place, in file order."""
    tables: dict[str, None] = {}
    files = sorted(migrations.glob("*.sql"))
    if not files:
        raise BackupError("MIGRATIONS_MISSING", "no migration files were found")
    for path in files:
        text = _SQL_COMMENT.sub(" ", path.read_text(encoding="utf-8"))
        events = [(m.start(), "create", _unquote(m.group(1)), None) for m in _CREATE_TABLE.finditer(text)]
        events += [(m.start(), "drop", _unquote(m.group(1)), None) for m in _DROP_TABLE.finditer(text)]
        events += [(m.start(), "rename", _unquote(m.group(1)), _unquote(m.group(2)))
                   for m in _RENAME_TABLE.finditer(text)]
        for _, kind, name, new in sorted(events):
            if kind == "create":
                tables[name] = None
            elif kind == "drop":
                tables.pop(name, None)
            elif name in tables:
                tables.pop(name)
                tables[new] = None
    return list(tables)


_AUTH_FK = re.compile(
    rf"ALTER\s+TABLE\s+(?:ONLY\s+)?public\.({_IDENT})\s+ADD\s+CONSTRAINT\s+{_IDENT}\s+"
    rf"FOREIGN\s+KEY\s+\(({_IDENT})\)\s+REFERENCES\s+auth\.users\s*\(\s*id\s*\)", re.I)


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _require_local_target(url: str) -> None:
    """The restore DROPS schema public: only a local throwaway database."""
    host = pg_env(url).get("PGHOST", "")
    if not (host in _LOCAL_HOSTS or host.startswith("/")):
        raise BackupError("RESTORE_TARGET_NOT_LOCAL",
                          "the restore target must be a local throwaway database (localhost or a unix socket)")


def restore(directory: Path, migrations: Path) -> dict[str, int]:
    target = _env("MILO_RESTORE_DB_URL")
    _require_local_target(target)
    manifests = sorted(directory.glob(f"*{MANIFEST_SUFFIX}"))
    if len(manifests) != 1:
        raise BackupError("MANIFEST_MISSING", "exactly one manifest is expected in the restore directory")
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    bundle = directory / manifest["encrypted_file"]
    if sha256_file(bundle) != manifest["encrypted_sha256"]:
        raise BackupError("BUNDLE_MISMATCH", "the bundle does not match its manifest")
    expected_major = int(manifest["server_major"])
    target_major = server_major(target)
    if target_major != expected_major:
        raise BackupError("RESTORE_MAJOR_MISMATCH",
                          f"the restore database is PostgreSQL {target_major}; the backup is from {expected_major}. "
                          f"Set the production-backup environment variable MILO_BACKUP_PG_MAJOR={expected_major}")
    restore_major = tool_major("pg_restore")
    if restore_major != expected_major:
        raise BackupError("PG_RESTORE_VERSION_MISMATCH",
                          f"pg_restore major {restore_major} does not match the backup's {expected_major}")
    expected_tables = migration_tables(migrations)
    work = Path(tempfile.mkdtemp(prefix="milo-restore-", dir=_scratch_dir()))
    try:
        _openssl(True, bundle, work / "bundle.tar.gz")
        _safe_extract(work / "bundle.tar.gz", work)
        (work / "bundle.tar.gz").unlink()
        _verify_checksums(work)
        dump = work / DUMP_NAME
        # The target must be a FRESH database: the dump recreates schema
        # public itself, so the empty default one is dropped first -- and a
        # database that already holds public tables is refused, never wiped.
        existing = _psql(target, "select count(*) from pg_tables where schemaname = 'public';",
                         "RESTORE_TARGET_UNREADABLE")
        if existing != "0":
            raise BackupError("RESTORE_TARGET_NOT_EMPTY",
                              "the restore database already holds public tables; use a fresh database")
        _psql(target, "drop schema if exists public cascade;", "RESTORE_SHIM_FAILED")
        _psql(target, RESTORE_SHIM_SQL, "RESTORE_SHIM_FAILED")
        # pg_restore needs -d to restore into a database (without it, it
        # writes SQL to stdout). Only the database NAME goes on argv; host,
        # user and password come from the environment (pg_env).
        database = pg_env(target).get("PGDATABASE", "postgres")

        def _restore(section: str, code: str) -> None:
            _pg("pg_restore", [f"--section={section}", "--no-owner", "--no-privileges",
                               "--exit-on-error", "-d", database, str(dump)], target, code)

        _restore("pre-data", "RESTORE_SCHEMA_FAILED")
        _restore("data", "RESTORE_DATA_FAILED")
        # auth.users is outside the backup: the ids public rows reference are
        # given stand-in rows so the foreign keys can be re-created.
        post_data = _run([_tool("pg_restore"), "--section=post-data", "-f", "-", str(dump)], "DUMP_UNREADABLE")
        seeds = []
        for table, column in sorted(set(_AUTH_FK.findall(post_data))):
            seeds.append(f"insert into auth.users (id) select distinct {column} from public.{table} "
                         f"where {column} is not null on conflict do nothing;")
        if seeds:
            _psql(target, "\n".join(seeds), "RESTORE_AUTH_SEED_FAILED")
        _restore("post-data", "RESTORE_POST_DATA_FAILED")
        _psql(target, RESET_SEQUENCES_SQL, "RESTORE_SEQUENCE_RESET_FAILED")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    present = set(_psql(target, "select tablename from pg_tables where schemaname = 'public';",
                        "RESTORE_VERIFY_FAILED").splitlines())
    missing = [name for name in expected_tables if name not in present]
    if missing:
        raise BackupError("RESTORE_TABLE_MISSING", "tables the migrations create are missing after restore: "
                          + ", ".join(missing[:20]))
    counts: dict[str, int] = {}
    for table in REQUIRED_NONEMPTY_TABLES:
        raw = _psql(target, f"select count(*) from public.{_quote(table)};", "RESTORE_VERIFY_FAILED")
        counts[table] = int(raw)
        say(f"COUNT {table} {counts[table]}")
    empty = [table for table, count in counts.items() if count == 0]
    if empty:
        raise BackupError("RESTORE_TABLE_EMPTY", "restored tables are empty: " + ", ".join(empty))
    say(f"PASS restore verified: {len(expected_tables)} migration tables present; "
        f"{len(present)} public tables restored")
    return counts


# -- entry -------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("server-major")
    p = sub.add_parser("create")
    p.add_argument("--out", required=True, type=Path)
    p = sub.add_parser("upload")
    p.add_argument("--dir", required=True, type=Path)
    p.add_argument("--bucket", required=True)
    p = sub.add_parser("fetch-latest")
    p.add_argument("--bucket", required=True)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_BACKUP_AGE_HOURS)
    p = sub.add_parser("restore")
    p.add_argument("--dir", required=True, type=Path)
    p.add_argument("--migrations", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "server-major":
            say(str(server_major(_env("MILO_BACKUP_DB_URL"))))
        elif args.command == "create":
            create(args.out)
        elif args.command == "upload":
            upload(args.dir, args.bucket)
        elif args.command == "fetch-latest":
            fetch_latest(args.bucket, args.out, args.max_age_hours)
        else:
            restore(args.dir, args.migrations)
    except BackupError as exc:
        say(f"FAIL {exc.code}: {exc.message}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""The immutable archive of a captured snapshot (PR-D1, D1-8).

One object per captured snapshot::

    gs://<MILO_REGISTER_ARCHIVE_BUCKET>/register/<resource_id>/<tozar_sha256[:16]>/<snapshot_key>.jsonl.gz

holding the FULL upstream records, one per line, in capture order and in
canonical form (sorted keys, compact separators, the register's own
characters). The gzip stream is written with a fixed mtime and no file name,
so the same records always produce the same bytes and the same sha256.

A record's line is its ``source_locator.capture_index + 1`` -- the database
view ``catalog_register_archive_lines`` exposes it per raw record, so PR-L can
cite a single record from the archive by (uri, line).

A snapshot whose archive the database already recorded is not written again
(the capture job checks the record first). Otherwise the write is CREATE-ONLY
(``ifGenerationMatch=0``); if the object already exists (a retry whose first
answer was lost, a crash before the record) it is a success only when the
object's own recorded sha256 (custom metadata, read with objectViewer) and
size match. Anything else is a failure, and the snapshot stays inactive
(``CATALOG_ARCHIVE_WRITE_FAILED``). Nothing here ever deletes an object.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import quote

GCS_UPLOAD_URL = "https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o"
GCS_OBJECT_URL = "https://storage.googleapis.com/storage/v1/b/{bucket}/o/{name}"
TIMEOUT_SECONDS = 120
ATTEMPTS = 3
RETRYABLE = frozenset({408, 429, 500, 502, 503, 504})

CREATED = "created"
EXISTS_VERIFIED = "exists_verified"
EXISTS_UNVERIFIED = "exists_unverified"

_BUCKET = re.compile(r"^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$")
_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$")
_RESOURCE = re.compile(r"^[0-9a-f-]{36}$")


class ArchiveWriteError(Exception):
    """The archive could not be written or verified. Carries no URL or body."""

    code = "CATALOG_ARCHIVE_WRITE_FAILED"


@dataclass(frozen=True)
class ArchiveObject:
    data: bytes
    sha256: str
    byte_size: int
    line_count: int


def tozar_prefix(tozar: str) -> str:
    return hashlib.sha256(tozar.encode("utf-8")).hexdigest()[:16]


def object_name(resource_id: str, tozar: str, snapshot_key: str) -> str:
    if not _RESOURCE.fullmatch(resource_id) or not _KEY.fullmatch(snapshot_key):
        raise ArchiveWriteError("invalid archive identity")
    return f"register/{resource_id}/{tozar_prefix(tozar)}/{snapshot_key}.jsonl.gz"


def gcs_uri(bucket: str, name: str) -> str:
    return f"gs://{bucket}/{name}"


def canonical_line(record: Mapping[str, Any]) -> str:
    return json.dumps(dict(record), sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"


def build(records: Iterable[Mapping[str, Any]]) -> ArchiveObject:
    """The deterministic object for records GIVEN IN CAPTURE ORDER."""
    buffer = io.BytesIO()
    lines = 0
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=0, compresslevel=9) as stream:
        for record in records:
            stream.write(canonical_line(record).encode("utf-8"))
            lines += 1
    data = buffer.getvalue()
    return ArchiveObject(data=data, sha256=hashlib.sha256(data).hexdigest(),
                         byte_size=len(data), line_count=lines)


def read_lines(data: bytes) -> list[dict[str, Any]]:
    """Every record of an archive object, in line order (line n is index n-1)."""
    with gzip.GzipFile(fileobj=io.BytesIO(data), mode="rb") as stream:
        return [json.loads(line) for line in stream.read().decode("utf-8").splitlines()]


class GcsArchiveWriter:
    """Create-only upload through the Cloud Storage JSON API.

    Authenticated with the runtime identity (Application Default Credentials),
    which holds ``roles/storage.objectCreator`` and ``roles/storage.objectViewer``
    on the archive bucket only
    (scripts/ops/setup-register-archive.sh): it can create, never overwrite
    and never delete. `session_factory` is the test seam.
    """

    def __init__(self, bucket: str, *, session_factory: Callable[[], Any] | None = None) -> None:
        if not _BUCKET.fullmatch(bucket or ""):
            raise ArchiveWriteError("the archive bucket is not configured")
        self.bucket = bucket
        self._session_factory = session_factory

    def _session(self) -> Any:
        if self._session_factory is not None:
            return self._session_factory()
        import google.auth
        from google.auth.transport.requests import AuthorizedSession

        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/devstorage.read_write"])
        return AuthorizedSession(credentials)

    def put(self, name: str, archive: ArchiveObject) -> str:
        """CREATED, EXISTS_VERIFIED or EXISTS_UNVERIFIED; else ArchiveWriteError."""
        try:
            session = self._session()
        except Exception:
            raise ArchiveWriteError("no storage session") from None
        boundary = f"milo-{uuid.uuid4().hex}"
        metadata = {"name": name, "contentType": "application/gzip",
                    "metadata": {"sha256": archive.sha256, "line_count": str(archive.line_count)}}
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
                f"{json.dumps(metadata)}\r\n--{boundary}\r\nContent-Type: application/gzip\r\n\r\n"
                ).encode("utf-8") + archive.data + f"\r\n--{boundary}--\r\n".encode("utf-8")
        url = GCS_UPLOAD_URL.format(bucket=quote(self.bucket, safe=""))
        params = {"uploadType": "multipart", "ifGenerationMatch": "0"}
        status = 0
        for attempt in range(1, ATTEMPTS + 1):
            try:
                response = session.post(url, params=params, data=body, timeout=TIMEOUT_SECONDS,
                                        headers={"Content-Type": f"multipart/related; boundary={boundary}"})
                status = int(response.status_code)
            except Exception:
                status = 0
                if attempt < ATTEMPTS:
                    continue
                raise ArchiveWriteError("the archive upload did not complete") from None
            if status == 200:
                try:
                    stored = response.json()
                except Exception:
                    raise ArchiveWriteError("the archive upload answer is unreadable") from None
                if str(stored.get("size")) != str(archive.byte_size):
                    raise ArchiveWriteError("the stored archive size differs")
                return CREATED
            if status == 412:
                # A retried create may have landed on an earlier attempt, or a
                # previous capture of this snapshot wrote it: verify, never
                # overwrite.
                return self._existing(session, name, archive)
            if status in RETRYABLE and attempt < ATTEMPTS:
                continue
            break
        raise ArchiveWriteError(f"the archive upload failed with HTTP {status}")

    def _existing(self, session: Any, name: str, archive: ArchiveObject) -> str:
        url = GCS_OBJECT_URL.format(bucket=quote(self.bucket, safe=""), name=quote(name, safe=""))
        try:
            response = session.get(url, params={"fields": "size,metadata"}, timeout=TIMEOUT_SECONDS)
            status = int(response.status_code)
        except Exception:
            return EXISTS_UNVERIFIED
        if status != 200:
            # Without objectViewer the object cannot be verified: never success.
            return EXISTS_UNVERIFIED
        try:
            stored = response.json()
        except Exception:
            return EXISTS_UNVERIFIED
        recorded = (stored.get("metadata") or {}).get("sha256")
        if recorded == archive.sha256 and str(stored.get("size")) == str(archive.byte_size):
            return EXISTS_VERIFIED
        raise ArchiveWriteError("a different object already exists under that name")


__all__ = ["ArchiveObject", "ArchiveWriteError", "CREATED", "EXISTS_UNVERIFIED", "EXISTS_VERIFIED",
           "GcsArchiveWriter", "build", "canonical_line", "gcs_uri", "object_name", "read_lines",
           "tozar_prefix"]

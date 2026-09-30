"""PR-L2: a built, archived register snapshot keeps no raw payload in the database.

After a snapshot's variants are completely built under the current mapper
version AND its archive is recorded, `public.compact_register_snapshot`
removes the stored payloads (the rows, their keys, `payload_sha256` and the
archive line stay; see migration 20261002000100 for every precondition and
refusal). Readers then take the register codes, the content hash and the
identity reading from the snapshot's typed variant rows.

* The capture job compacts a snapshot right after its build completes
  (`compact_after_build`; reported in the unit's document, never failing it).
* The operator path (`python -m backend.catalog.register.compaction`, run by
  the **Register variants** workflow with `compact = dry-run | apply`)
  compacts one already built snapshot by key, dry-run first. A Prepare
  snapshot captured before PR-D1 has no archive: `--apply` first writes it
  from the stored rows, in capture order, with PR-D1's create-only writer
  (`archive_from_database`), then compacts.
* Anything that needs the original record fetches its archive line from Cloud
  Storage and has the database check it against the row's `payload_sha256`
  (`source_record`, `--show-record`).
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from typing import Any, Mapping, Sequence

from backend.catalog.government import source as src
from backend.catalog.register import archive as archive_module
from backend.catalog.register import config as register_config

#: The static codes this module reports (the database's refusals included).
COMPACTION_REASONS: Mapping[str, str] = {
    "CATALOG_COMPACTION_REQUEST_INVALID": "one snapshot key is required",
    "CATALOG_COMPACTION_SNAPSHOT_UNKNOWN": "no Government snapshot has that key",
    "CATALOG_COMPACTION_SNAPSHOT_INELIGIBLE": "only an activated whole-tozar Government snapshot is compacted",
    "CATALOG_COMPACTION_ARCHIVE_MISSING": "the snapshot has no recorded archive holding every stored row",
    "CATALOG_COMPACTION_COUNT_UNVERIFIED": "the snapshot's capture is not count-verified",
    "CATALOG_COMPACTION_BUILD_INCOMPLETE": "the snapshot's variant build under the current mapper is not complete",
    "CATALOG_COMPACTION_TYPED_MISMATCH": "a row's typed variant does not read exactly as its payload",
    "CATALOG_COMPACTION_FAILED": "the compaction did not complete",
    "CATALOG_ARCHIVE_NOT_CONFIGURED": "no archive bucket is configured",
    "CATALOG_ARCHIVE_WRITE_FAILED": "the snapshot's archive could not be written or verified",
    "CATALOG_ARCHIVE_UNREADABLE": "the archive object could not be read or does not match its record",
    "CATALOG_ARCHIVE_LINE_MISMATCH": "the archive line is not the stored row",
}


class CompactionError(Exception):
    def __init__(self, code: str) -> None:
        if code not in COMPACTION_REASONS:
            raise ValueError("compaction code must come from the static allowlist")
        super().__init__(code)
        self.code = code


def compact(repository: Any, snapshot_key: str, *, apply: bool) -> dict[str, Any]:
    """The database's answer: status ready / compacted / unchanged / refused (+ code)."""
    answer = repository.compact_register_snapshot(str(snapshot_key), bool(apply))
    if not isinstance(answer, Mapping) or answer.get("status") not in ("ready", "compacted", "unchanged", "refused"):
        raise CompactionError("CATALOG_COMPACTION_FAILED")
    return dict(answer)


def compact_after_build(repository: Any, snapshot_key: str | None, build: Mapping[str, Any]) -> dict[str, Any]:
    """The capture job's compaction of a snapshot it just built. Never raises:
    a refusal or a failure leaves the payloads in place and is reported."""
    if not snapshot_key or (build or {}).get("status") not in ("built", "unchanged"):
        return {"status": "skipped"}
    try:
        answer = compact(repository, snapshot_key, apply=True)
    except Exception:  # noqa: BLE001 - reduced to a static code
        return {"status": "failed", "code": "CATALOG_COMPACTION_FAILED"}
    report = {"status": answer["status"]}
    if answer.get("code") in COMPACTION_REASONS:
        report["code"] = answer["code"]
    return report


# -- the archive of a Prepare snapshot, from its stored rows -------------------------

def _stored_rows(repository: Any, snapshot_id: str) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    while True:
        page = repository.list_catalog_raw_records(snapshot_id, limit=500, offset=len(rows))
        rows.extend(page)
        if len(page) < 500:
            return rows


def archive_from_database(repository: Any, snapshot: Mapping[str, Any], writer: Any, *,
                          resource_id: str = src.WLTP_RESOURCE_ID) -> Mapping[str, Any]:
    """Write (create-only) and record the archive of a snapshot that has none,
    from its stored payloads in capture order: the same lines, and so the same
    object, PR-D1's capture writes. Idempotent: a recorded archive stands."""
    snapshot_id = str(snapshot["id"])
    recorded = repository.register_snapshot_archive(snapshot_id)
    if recorded is not None:
        return recorded
    if writer is None:
        raise CompactionError("CATALOG_ARCHIVE_NOT_CONFIGURED")
    tozar = (((snapshot.get("retrieval_metadata") or {}).get("capture_scope") or {}).get("filters") or {}).get("tozar")
    rows = _stored_rows(repository, snapshot_id)
    if not isinstance(tozar, str) or any(not isinstance(row.get("payload"), Mapping)
                                         or "capture_index" not in (row.get("source_locator") or {})
                                         for row in rows):
        raise CompactionError("CATALOG_ARCHIVE_WRITE_FAILED")
    rows.sort(key=lambda row: int(row["source_locator"]["capture_index"]))
    obj = archive_module.build(row["payload"] for row in rows)
    try:
        name = archive_module.object_name(resource_id, tozar, str(snapshot["snapshot_key"]))
        outcome = writer.put(name, obj)
    except archive_module.ArchiveWriteError:
        raise CompactionError("CATALOG_ARCHIVE_WRITE_FAILED") from None
    if outcome not in (archive_module.CREATED, archive_module.EXISTS_VERIFIED):
        raise CompactionError("CATALOG_ARCHIVE_WRITE_FAILED")
    try:
        return repository.record_register_snapshot_archive_from_database(
            snapshot_id, archive_module.gcs_uri(writer.bucket, name), obj.byte_size, obj.sha256, obj.line_count)
    except Exception:  # noqa: BLE001 - reduced to a static code
        raise CompactionError("CATALOG_ARCHIVE_WRITE_FAILED") from None


# -- the original record, from the archive ------------------------------------------

def source_record(repository: Any, reader: Any, snapshot_id: str, upstream_record_id: str) -> dict[str, Any]:
    """ONE stored row's original register record, from its archive line.

    The object must be the recorded one (its sha256), and the line must be the
    row (the database's own digest of it equals `payload_sha256`)."""
    archive = repository.register_snapshot_archive(str(snapshot_id))
    row = repository.catalog_raw_record_by_upstream_id(str(snapshot_id), str(upstream_record_id),
                                                       allow_incomplete=True)
    index = ((row or {}).get("source_locator") or {}).get("capture_index")
    if archive is None or row is None or not isinstance(index, int):
        raise CompactionError("CATALOG_ARCHIVE_UNREADABLE")
    bucket, _, name = str(archive["gcs_uri"]).removeprefix("gs://").partition("/")
    if reader is None or getattr(reader, "bucket", None) != bucket:
        raise CompactionError("CATALOG_ARCHIVE_UNREADABLE")
    try:
        data = reader.get(name)
    except archive_module.ArchiveWriteError:
        raise CompactionError("CATALOG_ARCHIVE_UNREADABLE") from None
    if hashlib.sha256(data).hexdigest() != archive["sha256"]:
        raise CompactionError("CATALOG_ARCHIVE_UNREADABLE")
    lines = archive_module.read_lines(data)
    if not 0 <= index < len(lines):
        raise CompactionError("CATALOG_ARCHIVE_LINE_MISMATCH")
    record = lines[index]
    if not repository.catalog_raw_record_payload_matches(str(row["id"]), archive_module.canonical_line(record)):
        raise CompactionError("CATALOG_ARCHIVE_LINE_MISMATCH")
    return record


# -- the operator entrypoint --------------------------------------------------------

EXIT_OK, EXIT_FAILED, EXIT_REFUSED = 0, 1, 2
_SNAPSHOT_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$")


def _open_repository() -> Any:  # pragma: no cover - production wiring
    from backend.config import get_settings
    from backend.repository import SupabaseRepository

    return SupabaseRepository(get_settings())


def default_archive_client() -> Any:
    bucket = register_config.load().archive_bucket
    return archive_module.GcsArchiveWriter(bucket) if bucket else None


def _line(answer: Mapping[str, Any]) -> str:
    return " ".join(f"{name}={answer[name]}" for name in (
        "snapshot_key", "raw_rows", "payloads_removed", "mapper_version", "bytes_before", "bytes_after",
        "payload_bytes", "mismatched_rows", "variant_rows") if answer.get(name) is not None)


def main(argv: Sequence[str] | None = None, *, repository: Any = None, archive_client: Any = None) -> int:
    """Prints ONE outcome line -- READY / COMPACTED / UNCHANGED / REFUSED /
    FAILED / RECORD -- with the snapshot key, counts and static codes (RECORD:
    the verified register record itself, public register data)."""
    parser = argparse.ArgumentParser(prog="python -m backend.catalog.register.compaction")
    parser.add_argument("--snapshot-key", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--show-record", metavar="UPSTREAM_RECORD_ID")
    try:
        args, extra = parser.parse_known_args(list(argv or []))
    except SystemExit:
        extra, args = ["invalid"], None
    if extra or args is None or not _SNAPSHOT_KEY.fullmatch(args.snapshot_key):
        print("REFUSED CATALOG_COMPACTION_REQUEST_INVALID: --snapshot-key and one of --dry-run / --apply / --show-record")
        return EXIT_REFUSED
    repo = repository if repository is not None else _open_repository()
    client = archive_client if archive_client is not None else default_archive_client()
    key = args.snapshot_key
    try:
        snapshot = repo.find_active_catalog_snapshot("government", src.WLTP_RESOURCE_ID, key)
        if args.show_record is not None:
            if snapshot is None:
                print(f"REFUSED CATALOG_COMPACTION_SNAPSHOT_UNKNOWN: {key}")
                return EXIT_REFUSED
            record = source_record(repo, client, str(snapshot["id"]), args.show_record)
            print("RECORD " + archive_module.canonical_line(record).rstrip("\n"))
            return EXIT_OK
        # A Prepare snapshot has no archive yet: written from its stored rows first.
        archived = snapshot is not None and repo.register_snapshot_archive(str(snapshot["id"])) is not None
        if snapshot is not None and not archived and args.apply:
            archive_from_database(repo, snapshot, client)
        answer = compact(repo, key, apply=bool(args.apply))
    except CompactionError as failure:
        print(f"FAILED {failure.code}: {key}")
        return EXIT_FAILED
    except Exception:  # noqa: BLE001 - reduced to a static code
        print(f"FAILED CATALOG_COMPACTION_FAILED: {key}")
        return EXIT_FAILED
    if answer["status"] == "refused" and answer.get("code") == "CATALOG_COMPACTION_ARCHIVE_MISSING" \
            and args.dry_run and snapshot is not None and not archived:
        print(f"READY {_line(answer)} archive=from-database (written by --apply, then compacted)")
        return EXIT_OK
    if answer["status"] == "refused":
        code = answer.get("code") if answer.get("code") in COMPACTION_REASONS else "CATALOG_COMPACTION_FAILED"
        print(f"REFUSED {code}: {_line(answer)}")
        return EXIT_REFUSED
    print(f"{answer['status'].upper()} {_line(answer)}")
    return EXIT_OK


__all__ = ["COMPACTION_REASONS", "CompactionError", "archive_from_database", "compact",
           "compact_after_build", "default_archive_client", "main", "source_record"]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))

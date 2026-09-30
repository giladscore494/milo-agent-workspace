"""PR-L2: a built, archived register snapshot keeps no raw payload in the database.

`public.compact_register_snapshot` (migration 20261002000100 holds every
precondition and refusal) has two modes:

* ACTIVE -- the tozar's rank-1 snapshot, once its variants are completely
  built under the current mapper version AND its archive is recorded: every
  payload is removed and every candidate keeps only its keys; readers take
  the register codes, the content hash and the identity from the snapshot's
  typed variant rows (`catalog_candidate_variants_resolved`).
* SUPERSEDED -- an older snapshot of the tozar, once nothing live can read it:
  it keeps only the rows something references, as skeletons, and drops its
  variants; its archive is the record (`source_record` reads it).

* The capture job compacts a snapshot right after its build completes, then
  the tozar's superseded snapshots (`compact_after_build`; reported in the
  unit's document, never failing it).
* The operator path (`python -m backend.catalog.register.compaction`, run by
  the **Register variants** workflow with `compact = dry-run | apply`)
  compacts one snapshot by key, dry-run first. A snapshot with no archive
  (a Prepare snapshot captured before PR-D1) has it written from its stored
  rows first -- only once every other precondition holds, the database has
  checked the rows are exactly an archive's lines, and every line is checked
  against its row's `payload_sha256` (`archive_from_database`).
* Anything that needs the original record fetches its archive line from Cloud
  Storage, the object checked against its recorded sha256 and the line
  against the row (`source_record`, `--show-record`).
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
    "CATALOG_COMPACTION_TYPED_MISMATCH": "a row's typed variant does not read exactly as its payload or candidate",
    "CATALOG_COMPACTION_SNAPSHOT_IN_USE": "something live can still read the superseded snapshot",
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


def _outcome(answer: Mapping[str, Any]) -> dict[str, Any]:
    report = {"status": answer["status"]}
    if answer.get("code") in COMPACTION_REASONS:
        report["code"] = answer["code"]
    return report


def compact_after_build(repository: Any, snapshot_key: str | None, build: Mapping[str, Any], *,
                        writer: Any = None) -> dict[str, Any]:
    """The capture job's compaction of a snapshot it just built, then of the
    tozar's superseded snapshots (their archive written from their rows first
    when they have none). Never raises: a refusal or a failure leaves the rows
    in place and is reported."""
    if not snapshot_key or (build or {}).get("status") not in ("built", "unchanged"):
        return {"status": "skipped"}
    try:
        report = _outcome(compact(repository, snapshot_key, apply=True))
    except Exception:  # noqa: BLE001 - reduced to a static code
        return {"status": "failed", "code": "CATALOG_COMPACTION_FAILED"}
    if report["status"] not in ("compacted", "unchanged"):
        return report
    superseded: list[dict[str, Any]] = []
    try:
        older = list(repository.catalog_register_superseded_snapshots(snapshot_key))
    except Exception:  # noqa: BLE001 - reduced to a static code
        older = []
        report["superseded_code"] = "CATALOG_COMPACTION_FAILED"
    for snapshot in older:
        key = str(snapshot["snapshot_key"])
        try:
            superseded.append({"snapshot_key": key, **_prepared_compaction(repository, snapshot, writer)})
        except CompactionError as failure:
            superseded.append({"snapshot_key": key, "status": "failed", "code": failure.code})
        except Exception:  # noqa: BLE001 - reduced to a static code
            superseded.append({"snapshot_key": key, "status": "failed", "code": "CATALOG_COMPACTION_FAILED"})
    if superseded:
        report["superseded"] = superseded
    return report


def _prepared_compaction(repository: Any, snapshot: Mapping[str, Any], writer: Any) -> dict[str, Any]:
    """Apply ONE snapshot's compaction, writing its archive from its stored rows
    first only when that is the one precondition left (a dry-run says so)."""
    key = str(snapshot["snapshot_key"])
    if repository.register_snapshot_archive(str(snapshot["id"])) is None \
            and compact(repository, key, apply=False).get("code") == "CATALOG_COMPACTION_ARCHIVE_MISSING":
        archive_from_database(repository, snapshot, writer)
    return _outcome(compact(repository, key, apply=True))


# -- the archive of a Prepare snapshot, from its stored rows -------------------------

#: Archive lines checked against their rows per database call.
LINE_CHECK_PAGE = 500

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
    try:
        # The database first: the stored rows are exactly an archive's lines.
        expected = int(repository.catalog_register_snapshot_archivable(snapshot_id))
    except Exception:  # noqa: BLE001 - reduced to a static code
        raise CompactionError("CATALOG_ARCHIVE_WRITE_FAILED") from None
    rows = _stored_rows(repository, snapshot_id)
    if not isinstance(tozar, str) or len(rows) != expected \
            or any(not isinstance(row.get("payload"), Mapping)
                   or "capture_index" not in (row.get("source_locator") or {}) for row in rows):
        raise CompactionError("CATALOG_ARCHIVE_WRITE_FAILED")
    rows.sort(key=lambda row: int(row["source_locator"]["capture_index"]))
    lines = [archive_module.canonical_line(row["payload"]) for row in rows]
    # Every line is its row, by the database's own digest, before anything is
    # written (and so before any payload can be removed).
    for first in range(0, len(lines), LINE_CHECK_PAGE):
        if repository.catalog_raw_record_lines_mismatched(snapshot_id, first, lines[first:first + LINE_CHECK_PAGE]):
            raise CompactionError("CATALOG_ARCHIVE_LINE_MISMATCH")
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

def _archive_lines(repository: Any, reader: Any, snapshot_id: str) -> list[dict[str, Any]]:
    """The snapshot's archive, exactly the recorded object (its sha256)."""
    archive = repository.register_snapshot_archive(str(snapshot_id))
    if archive is None:
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
    return archive_module.read_lines(data)


def source_record(repository: Any, reader: Any, snapshot_id: str, upstream_record_id: str) -> dict[str, Any]:
    """ONE row's original register record, from its archive line.

    The object must be the recorded one (its sha256). An active snapshot's
    line must also be its stored row (the database's own digest of it equals
    `payload_sha256`). A superseded snapshot kept as skeletons is not browsed
    (CATALOG_SNAPSHOT_ARCHIVED): its record is the archive line of that
    upstream id -- every line was checked against its row before any row was
    removed, and the object is the one recorded then."""
    if repository.register_snapshot_archived(str(snapshot_id)):
        matches = [line for line in _archive_lines(repository, reader, snapshot_id)
                   if str(line.get("_id")) == str(upstream_record_id)]
        if len(matches) != 1:
            raise CompactionError("CATALOG_ARCHIVE_LINE_MISMATCH")
        return matches[0]
    row = repository.catalog_raw_record_by_upstream_id(str(snapshot_id), str(upstream_record_id),
                                                       allow_incomplete=True)
    index = ((row or {}).get("source_locator") or {}).get("capture_index")
    if row is None or not isinstance(index, int):
        raise CompactionError("CATALOG_ARCHIVE_UNREADABLE")
    lines = _archive_lines(repository, reader, snapshot_id)
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
        "snapshot_key", "mode", "raw_rows", "kept_rows", "payloads_removed", "mapper_version", "bytes_before",
        "bytes_after", "payload_bytes", "mismatched_rows", "variant_rows") if answer.get(name) is not None)


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
        # A snapshot with no archive has it written from its stored rows first,
        # only once every other precondition holds (_prepared_compaction).
        archived = snapshot is not None and repo.register_snapshot_archive(str(snapshot["id"])) is not None
        if snapshot is not None and args.apply:
            if not archived \
                    and compact(repo, key, apply=False).get("code") == "CATALOG_COMPACTION_ARCHIVE_MISSING":
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

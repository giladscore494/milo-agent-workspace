"""In-memory mirrors of PR-L1's variant RPCs (migration 20260930000100), mixed
into `MemoryRepository`. The database functions are the authority and are
exercised on ephemeral PostgreSQL (tests/test_catalog_variants_postgres.py);
these mirrors let the build, the capture job and the API run offline."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from backend.catalog import coverage as catalog_coverage
from backend.catalog.government import query as query_module
from backend.catalog.register import retention
from backend.catalog.register import variants as mapper
from backend.errors import AppError

_LEVELS = (catalog_coverage.LEVEL_IDENTITY, catalog_coverage.LEVEL_GOVERNMENT_FIELDS)
_MAX_PAGE = 100


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _refused(code: str) -> AppError:
    return AppError(code, mapper.VARIANT_REASONS.get(code, "invalid browser query"), 409)


class VariantsMemoryMixin:
    def _variants_state(self) -> dict[str, Any]:
        state = getattr(self, "_variants", None)
        if state is None:
            state = {"builds": {}, "rows": {}}
            self._variants = state
        return state

    def catalog_variant_build_state(self, snapshot_id: str, mapper_version: str) -> dict[str, Any] | None:
        row = self._variants_state()["builds"].get((str(snapshot_id), mapper_version))
        return dict(row) if row else None

    def _buildable(self, snapshot: dict[str, Any] | None) -> str:
        filters = ((snapshot or {}).get("retrieval_metadata") or {}).get("capture_scope") or {}
        filters = filters.get("filters") if isinstance(filters, dict) else None
        # The NEWEST register unit that captured this snapshot decides.
        units = sorted((u for u in self._register_state()["units"].values()
                        if str(u.get("snapshot_id")) == str((snapshot or {}).get("id"))),
                       key=lambda u: (str(u.get("updated_at")), str(u.get("id"))))
        unverified = bool(units) and units[-1].get("count_verified") is False
        if (snapshot is None or snapshot.get("source_family") != "government" or not snapshot.get("activated_at")
                or snapshot.get("validation_state") != "complete"
                or int(snapshot.get("stored_record_count") or 0) != int(snapshot.get("declared_record_count") or -1)
                or not isinstance(filters, dict) or set(filters) != {"tozar"} or unverified):
            raise _refused("CATALOG_VARIANT_SNAPSHOT_INELIGIBLE")
        # PR-L1b: rank 1 only (activated_at desc, then id).
        if any(retention.scoped_tozar(other) == filters["tozar"] and other.get("activated_at")
               and other.get("source_family") == "government" and str(other["id"]) != str(snapshot["id"])
               and (str(other["activated_at"]), str(snapshot["id"])) > (str(snapshot["activated_at"]), str(other["id"]))
               for other in self.catalog_snapshots.values()):
            raise _refused("CATALOG_VARIANT_SNAPSHOT_SUPERSEDED")
        return str(filters["tozar"])

    def record_catalog_variants(self, snapshot_id: str, mapper_version: str,
                                rows: list[dict[str, Any]]) -> dict[str, Any]:
        state = self._variants_state()
        with self.lock:
            if mapper_version != mapper.MAPPER_VERSION:
                raise _refused("CATALOG_VARIANT_MAPPER_MISMATCH")
            ids = [str(row.get("upstream_record_id")) for row in rows]
            if len(rows) > mapper.BUILD_BATCH_ROWS or len(set(ids)) != len(ids):
                raise _refused("CATALOG_VARIANT_ROWS_INVALID")
            snapshot = self._snapshot_by_id(snapshot_id)
            tozar = self._buildable(snapshot)
            records = {row["upstream_record_id"]: row for row in self.catalog_raw_records.values()
                       if row["snapshot_id"] == str(snapshot_id)}
            if any(i not in records for i in ids):
                raise _refused("CATALOG_VARIANT_ROWS_INVALID")
            for row in rows:
                try:
                    mapper.check_equipment(row.get("equipment") or {})
                except mapper.VariantMappingError:
                    raise _refused("CATALOG_VARIANT_EQUIPMENT_KEY_UNKNOWN") from None
            build = state["builds"].setdefault((str(snapshot_id), mapper_version), {
                "snapshot_id": str(snapshot_id), "mapper_version": mapper_version,
                "snapshot_key": snapshot["snapshot_key"], "tozar": tozar,
                "activated_at": snapshot["activated_at"], "expected_rows": int(snapshot["stored_record_count"]),
                "built_rows": 0, "completed_at": None, "created_at": _now(), "updated_at": _now()})
            candidates = {c["raw_record_id"]: c for c in sorted(self.catalog_candidates.values(),
                                                                  key=lambda c: str(c["id"]), reverse=True)}
            archived = str(snapshot_id) in self._register_state()["archives"]
            inserted = 0
            for row in rows:
                key = (str(snapshot_id), row["upstream_record_id"], mapper_version)
                if key in state["rows"]:
                    continue
                record = records[row["upstream_record_id"]]
                candidate = candidates.get(record["id"])
                locator = record.get("source_locator") or {}
                state["rows"][key] = {
                    **row, "id": str(uuid4()), "snapshot_id": str(snapshot_id),
                    "snapshot_key": snapshot["snapshot_key"], "mapper_version": mapper_version,
                    "content_sha256": catalog_coverage.variant_content_sha256(record["payload"]),
                    "archive_line": int(locator["capture_index"]) + 1
                    if archived and "capture_index" in locator else None,
                    "variant_identity_key": catalog_coverage.candidate_identity_key(candidate, record["payload"])
                    if candidate else None,
                    "created_at": _now()}
                inserted += 1
            built = [v for (sid, _u, mv), v in state["rows"].items()
                     if sid == str(snapshot_id) and mv == mapper_version]
            build.update(built_rows=len(built), updated_at=_now())
            if len(built) == build["expected_rows"] and not build["completed_at"]:
                build["completed_at"] = _now()
            if build["completed_at"]:
                # PR-L1b: a complete build re-measures the units that captured it.
                for unit in self._register_state()["units"].values():
                    if str(unit.get("snapshot_id")) == str(snapshot_id) and unit.get("status") == "captured":
                        unit.update(measured_bytes=self._snapshot_bytes(str(snapshot_id)),
                                    measurement_method="memory:json_length+variants")
            written = self._refresh_variant_ledger(snapshot, built, {
                state["rows"][(str(snapshot_id), i, mapper_version)]["variant_identity_key"] for i in ids})
            return {"snapshot_key": snapshot["snapshot_key"], "mapper_version": mapper_version,
                    "rows": len(rows), "inserted": inserted, "ledger_written": written,
                    "built_rows": build["built_rows"], "expected_rows": build["expected_rows"],
                    "complete": build["completed_at"] is not None}

    def _refresh_variant_ledger(self, snapshot: dict[str, Any], built: list[dict[str, Any]],
                                keys: set[str | None]) -> int:
        """Mirror of the ledger step of `record_catalog_variants`."""
        rank = catalog_coverage.STATUS_RANK
        written = 0
        for key in sorted(k for k in keys if k):
            contents = {v["content_sha256"] for v in built if v["variant_identity_key"] == key}
            collision = len(contents) > 1
            incoming = {"status": catalog_coverage.FAILED if collision else catalog_coverage.ENRICHED,
                        "last_run_id": str(snapshot["created_by_run_id"]), "snapshot_key": snapshot["snapshot_key"],
                        "content_sha256": catalog_coverage.collision_content_sha256(contents) if collision
                        else next(iter(contents)),
                        "vocabulary_version": catalog_coverage.VOCABULARY_VERSION,
                        "reason_code": catalog_coverage.KEY_COLLISION if collision else None}
            for level in _LEVELS:
                row = self.catalog_variant_coverage.get((key, level))
                if row is None:
                    self.catalog_variant_coverage[(key, level)] = {
                        "id": str(uuid4()), "variant_identity_key": key, "level": level, **incoming,
                        "created_at": _now(), "updated_at": _now()}
                    written += 1
                    continue
                newer = any(s.get("snapshot_key") == row["snapshot_key"] and s.get("activated_at")
                            and str(s["activated_at"]) > str(snapshot["activated_at"])
                            for s in self.catalog_snapshots.values())
                replace = not newer and (
                    row["content_sha256"] != incoming["content_sha256"]
                    or rank[incoming["status"]] > rank[row["status"]]
                    or (rank[incoming["status"]] == rank[row["status"]]
                        and any(row.get(n) != incoming[n] for n in ("status", "last_run_id", "snapshot_key",
                                                                    "vocabulary_version", "reason_code"))))
                if replace:
                    row.update(incoming, updated_at=_now())
                    written += 1
        return written

    # -- PR-L2: payload compaction (20261002000100) ----------------------------------------
    def _compactions(self) -> dict[str, dict[str, Any]]:
        """snapshot id -> its ACTIVE compaction ("variants" readers)."""
        return self._variants_state().setdefault("compactions", {})

    def _skeletons(self) -> dict[str, dict[str, Any]]:
        """snapshot id -> its SUPERSEDED compaction ("archive" readers)."""
        return self._variants_state().setdefault("skeletons", {})

    def register_snapshot_archived(self, snapshot_id: str) -> bool:
        return str(snapshot_id) in self._skeletons()

    def catalog_register_uncompacted_captures(self) -> int:
        """Mirror: finished register captures whose snapshot was never compacted."""
        units = list(self._register_state()["units"].values())
        live = {str(u["group_id"]) for u in units if u.get("status") in ("requested", "capturing")}
        activated = {str(s["id"]) for s in self.catalog_snapshots.values() if s.get("activated_at")}
        return len({str(u["snapshot_id"]) for u in units
                    if u.get("status") == "captured" and str(u.get("group_id")) not in live
                    and str(u.get("snapshot_id")) in activated
                    and str(u["snapshot_id"]) not in self._compactions()
                    and str(u["snapshot_id"]) not in self._skeletons()})

    def _compacted_variant(self, record: dict[str, Any]) -> dict[str, Any] | None:
        done = self._compactions().get(str(record["snapshot_id"]))
        if done is None:
            return None
        return self._variants_state()["rows"].get(
            (str(record["snapshot_id"]), record["upstream_record_id"], done["mapper_version"]))

    def _record_facts(self, record: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """(codes source, content hash) of one raw record: its payload while it
        exists, the compacted snapshot's variant afterwards (the SQL helpers)."""
        if record.get("payload") is not None:
            return record["payload"], catalog_coverage.variant_content_sha256(record["payload"])
        variant = self._compacted_variant(record) or {}
        codes = {field: (str(variant[field]) if isinstance(variant.get(field), int) else variant.get(field))
                 for _name, field in catalog_coverage.REGISTER_IDENTITY_FIELDS}
        return codes, variant.get("content_sha256")

    @staticmethod
    def _reading(variant: dict[str, Any]) -> dict[str, Any]:
        fields = {name: variant.get(name) for name in query_module.IDENTITY_RECORD_FIELDS}
        for _name, field in catalog_coverage.REGISTER_IDENTITY_FIELDS:
            value = variant.get(field)
            fields[field] = str(value) if isinstance(value, int) else value
        return {"upstream_record_id": variant["upstream_record_id"], "mapper_version": variant["mapper_version"],
                "content_sha256": variant["content_sha256"], "fields": fields,
                "parse_issue_fields": sorted({i["field"] for i in variant.get("parse_issues") or []})}

    def _tozar_snapshots(self, tozar: Any) -> list[dict[str, Any]]:
        """The tozar's activated snapshots, rank 1 first (activated_at desc, then id)."""
        same = [s for s in self.catalog_snapshots.values()
                if s.get("source_family") == "government" and s.get("activated_at")
                and (((s.get("retrieval_metadata") or {}).get("capture_scope") or {}).get("filters") or {})
                .get("tozar") == tozar]
        same.sort(key=lambda s: str(s["id"]))
        same.sort(key=lambda s: str(s["activated_at"]), reverse=True)
        return same

    def _referenced_candidates(self) -> set[str]:
        return ({str(i.get("candidate_id")) for i in self.work_scope_queue_items}
                | {str(link.get("candidate_id")) for link in self.catalog_evidence_links.values()}
                | {str(p.get("candidate_id")) for p in self.catalog_canonical_field_provenance}
                | {str(v.get("promoted_from_candidate_id")) for v in self.catalog_model_variants}
                | {str(r.get("candidate_id")) for r in self.catalog_variant_reservations.values()})

    def catalog_register_superseded_snapshots(self, snapshot_key: str) -> list[dict[str, Any]]:
        snapshot = next((s for s in self.catalog_snapshots.values() if s.get("snapshot_key") == snapshot_key), None)
        tozar = (((snapshot or {}).get("retrieval_metadata") or {}).get("capture_scope") or {}).get("filters", {})
        if snapshot is None or not isinstance(tozar, dict) or "tozar" not in tozar:
            return []
        return [{"id": s["id"], "snapshot_key": s["snapshot_key"], "retrieval_metadata": s.get("retrieval_metadata")}
                for s in self._tozar_snapshots(tozar["tozar"])
                if s["id"] != snapshot["id"] and str(s["id"]) not in self._skeletons()]

    def catalog_register_snapshot_archivable(self, snapshot_id: str) -> int:
        snapshot = self._snapshot_by_id(snapshot_id)
        records = [r for r in self.catalog_raw_records.values() if str(r["snapshot_id"]) == str(snapshot_id)]
        indexes = sorted(int((r.get("source_locator") or {}).get("capture_index", -1)) for r in records)
        if snapshot is None or not snapshot.get("activated_at") or indexes != list(range(len(records))) \
                or int(snapshot.get("declared_record_count") or -1) != len(records):
            raise AppError("CATALOG_CAPTURE_COUNT_MISMATCH", "the stored rows are not an archive's lines", 409)
        return len(records)

    def catalog_raw_record_lines_mismatched(self, snapshot_id: str, first_index: int, lines: list[str]) -> int:
        by_index = {int((r.get("source_locator") or {}).get("capture_index", -1)): r
                    for r in self.catalog_raw_records.values() if str(r["snapshot_id"]) == str(snapshot_id)}
        return sum(1 for offset, line in enumerate(lines)
                   if (row := by_index.get(first_index + offset)) is None or row.get("payload") is None
                   or not self.catalog_raw_record_payload_matches(str(row["id"]), line))

    def compact_register_snapshot(self, snapshot_key: str, apply: bool, *, verified_sha256: str | None = None,
                                  caller_run_id: str | None = None) -> dict[str, Any]:
        """Mirror of `compact_register_snapshot` (the candidates' identity stays:
        the in-memory readers read the candidate rows themselves -- candidate
        slimming is covered by the Postgres suite only); its losslessness check
        is the Python readers' own: the typed reading answers exactly as the
        payload. An apply needs the archive's bytes verified (the sha256)."""
        with self.lock:
            snapshot = next((s for s in self.catalog_snapshots.values()
                             if s.get("snapshot_key") == snapshot_key and s.get("source_family") == "government"), None)
            refused = {"status": "refused", "snapshot_key": snapshot_key}
            if snapshot is None:
                return {**refused, "code": "CATALOG_COMPACTION_SNAPSHOT_UNKNOWN"}
            sid = str(snapshot["id"])
            if sid in self._skeletons():
                return {"status": "unchanged", "snapshot_key": snapshot_key, **self._skeletons()[sid]}
            filters = ((snapshot.get("retrieval_metadata") or {}).get("capture_scope") or {}).get("filters")
            if not snapshot.get("activated_at") or not isinstance(filters, dict) or set(filters) != {"tozar"}:
                return {**refused, "code": "CATALOG_COMPACTION_SNAPSHOT_INELIGIBLE"}
            active = self._tozar_snapshots(filters["tozar"])[0]
            done = self._compactions().get(sid)
            if done is not None and active["id"] == snapshot["id"]:
                return {"status": "unchanged", "snapshot_key": snapshot_key, **done}
            records = [r for r in self.catalog_raw_records.values() if str(r["snapshot_id"]) == sid]
            units = sorted((u for u in self._register_state()["units"].values() if str(u.get("snapshot_id")) == sid),
                           key=lambda u: (str(u.get("updated_at")), str(u.get("id"))))
            if (units and units[-1].get("count_verified") is not True) or \
                    int(snapshot.get("stored_record_count") or 0) != int(snapshot.get("declared_record_count") or -1) \
                    or int(snapshot.get("stored_record_count") or 0) != len(records):
                return {**refused, "code": "CATALOG_COMPACTION_COUNT_UNVERIFIED"}
            if active["id"] != snapshot["id"]:
                return self._compact_superseded(snapshot, active, records, units, apply, verified_sha256)
            # Active mode waits for live readers too (the mirror models reservations).
            mine = {str(c["id"]) for c in self.catalog_candidates.values() if str(c["snapshot_id"]) == sid}
            if any(str(r.get("candidate_id")) in mine and (self.runs.get(str(r.get("run_id"))) or {}).get("status")
                   not in (None, "completed", "partial_success", "failed", "cancelled", "timed_out", "budget_exhausted")
                   for r in self.catalog_variant_reservations.values()):
                return {**refused, "code": "CATALOG_COMPACTION_SNAPSHOT_IN_USE", "mode": "active"}
            build = self._variants_state()["builds"].get((sid, mapper.MAPPER_VERSION))
            rows = self._variants_state()["rows"]
            variants = {r["upstream_record_id"]: rows.get((sid, r["upstream_record_id"], mapper.MAPPER_VERSION))
                        for r in records}
            if not build or not build.get("completed_at") or any(v is None for v in variants.values()):
                return {**refused, "code": "CATALOG_COMPACTION_BUILD_INCOMPLETE"}
            mismatched = sum(1 for r in records if not self._reads_as_payload(r["payload"], variants[r["upstream_record_id"]]))
            if mismatched:
                return {**refused, "code": "CATALOG_COMPACTION_TYPED_MISMATCH", "mismatched_rows": mismatched}
            archive = self._register_state()["archives"].get(sid)
            if archive is None or int(archive["line_count"]) != len(records):
                return {**refused, "code": "CATALOG_COMPACTION_ARCHIVE_MISSING"}
            if apply and verified_sha256 != archive["sha256"]:
                return {**refused, "code": "CATALOG_COMPACTION_ARCHIVE_UNVERIFIED"}
            before = self._snapshot_bytes(sid)
            if not apply:
                return {"status": "ready", "snapshot_key": snapshot_key, "raw_rows": len(records),
                        "mapper_version": mapper.MAPPER_VERSION, "bytes_before": before}
            for record in records:
                record["payload"] = None
            done = {"raw_rows": len(records), "mapper_version": mapper.MAPPER_VERSION,
                    "bytes_before": before, "bytes_after": self._snapshot_bytes(sid)}
            self._compactions()[sid] = done
            for unit in units:
                if unit.get("status") == "captured":
                    unit.update(measured_bytes=done["bytes_after"], measurement_method="memory:json_length+variants")
            return {"status": "compacted", "snapshot_key": snapshot_key, "payloads_removed": len(records), **done}

    def _compact_superseded(self, snapshot: dict[str, Any], active: dict[str, Any], records: list[dict[str, Any]],
                            units: list[dict[str, Any]], apply: bool,
                            verified_sha256: str | None = None) -> dict[str, Any]:
        """The superseded mode: referenced rows kept as skeletons, the rest and
        every variant dropped, the archive the record (the SQL's in-use checks
        mirrored for what this repository models: open reservations)."""
        sid, key = str(snapshot["id"]), snapshot["snapshot_key"]
        refused = {"status": "refused", "snapshot_key": key, "mode": "superseded"}
        build = self._variants_state()["builds"].get((str(active["id"]), mapper.MAPPER_VERSION))
        if not build or not build.get("completed_at"):
            return {**refused, "code": "CATALOG_COMPACTION_BUILD_INCOMPLETE"}
        candidates = [c for c in self.catalog_candidates.values() if str(c["snapshot_id"]) == sid]
        mine = {str(c["id"]) for c in candidates}
        if any(str(r.get("candidate_id")) in mine for r in self.catalog_variant_reservations.values()):
            return {**refused, "code": "CATALOG_COMPACTION_SNAPSHOT_IN_USE"}
        referenced = self._referenced_candidates()
        kept_records = {str(c["raw_record_id"]) for c in candidates if str(c["id"]) in referenced}
        archive = self._register_state()["archives"].get(sid)
        if archive is None or int(archive["line_count"]) != len(records):
            return {**refused, "code": "CATALOG_COMPACTION_ARCHIVE_MISSING"}
        if apply and verified_sha256 != archive["sha256"]:
            return {**refused, "code": "CATALOG_COMPACTION_ARCHIVE_UNVERIFIED"}
        before = self._snapshot_bytes(sid)
        if not apply:
            return {"status": "ready", "snapshot_key": key, "mode": "superseded", "raw_rows": len(records),
                    "kept_rows": len(kept_records), "bytes_before": before}
        variants = self._variants_state()
        for row_key in [k for k in variants["rows"] if k[0] == sid]:
            del variants["rows"][row_key]
        for build_key in [k for k in variants["builds"] if k[0] == sid]:
            del variants["builds"][build_key]
        for cand_key in [k for k, c in self.catalog_candidates.items()
                         if str(c["snapshot_id"]) == sid and str(c["id"]) not in referenced]:
            del self.catalog_candidates[cand_key]
        for rec_key in [k for k, r in self.catalog_raw_records.items()
                        if str(r["snapshot_id"]) == sid and str(r["id"]) not in kept_records]:
            del self.catalog_raw_records[rec_key]
        for record in self.catalog_raw_records.values():
            if str(record["snapshot_id"]) == sid:
                record["payload"] = None
        done = {"mode": "superseded", "raw_rows": len(records), "kept_rows": len(kept_records),
                "bytes_before": before, "bytes_after": self._snapshot_bytes(sid)}
        self._skeletons()[sid] = done
        for unit in units:
            if unit.get("status") == "captured":
                unit.update(measured_bytes=done["bytes_after"], measurement_method="memory:json_length+variants")
        return {"status": "compacted", "snapshot_key": key, **done}

    def _reads_as_payload(self, payload: dict[str, Any] | None, variant: dict[str, Any]) -> bool:
        if payload is None:
            return False
        reading = self._reading(variant)
        codes, content = self._record_facts({"payload": payload})
        return (content == variant["content_sha256"]
                and all(reading["fields"][f] == catalog_coverage.register_code(payload, f)
                        for _name, f in catalog_coverage.REGISTER_IDENTITY_FIELDS)
                and query_module.reading_projection(reading) == query_module.identity_projection(payload)
                and query_module.reading_unstated(reading) == query_module.unstated_fields(payload))

    def catalog_compacted_record_reading(self, snapshot_id: Any, upstream_record_id: str, *,
                                         allow_incomplete: bool = False) -> dict[str, Any] | None:
        self._readable_snapshot(snapshot_id, allow_incomplete)
        record = next((r for r in self.catalog_raw_records.values() if str(r["snapshot_id"]) == str(snapshot_id)
                       and r["upstream_record_id"] == str(upstream_record_id)), None)
        variant = self._compacted_variant(record) if record else None
        return self._reading(variant) if variant else None

    def record_register_snapshot_archive_from_database(self, snapshot_id: str, gcs_uri: str, byte_size: int,
                                                       sha256: str, line_count: int) -> dict[str, Any]:
        snapshot = self._snapshot_by_id(snapshot_id)
        if snapshot is None or not snapshot.get("activated_at"):
            raise AppError("CATALOG_REGISTER_REQUEST_INVALID", "unknown snapshot", 409)
        return self.record_register_snapshot_archive(
            snapshot["created_by_run_id"], snapshot_id, gcs_uri, byte_size, sha256, line_count,
            worker_id="", attempt=0, lease_token="", _leased=False)

    def catalog_raw_record_payload_matches(self, raw_record_id: str, line: str) -> bool:
        from backend.catalog.digest import catalog_payload_digest

        record = next((r for r in self.catalog_raw_records.values() if str(r["id"]) == str(raw_record_id)), None)
        return record is not None and record["payload_sha256"] == catalog_payload_digest(json.loads(line))

    # -- the discovery tree ----------------------------------------------------------------
    def _current_variant_snapshots(self) -> set[str]:
        """Per tozar, the newest-activated complete build under this mapper."""
        newest: dict[str, dict[str, Any]] = {}
        for build in self._variants_state()["builds"].values():
            if build["mapper_version"] != mapper.MAPPER_VERSION or not build["completed_at"]:
                continue
            held = newest.get(build["tozar"])
            # The database's order: activated_at DESC, then snapshot_id ASC.
            if held is None or (str(build["activated_at"]), held["snapshot_id"]) > \
                    (str(held["activated_at"]), build["snapshot_id"]):
                newest[build["tozar"]] = build
        return {b["snapshot_id"] for b in newest.values()}

    def _current_variants(self, filters: dict[str, Any]) -> list[dict[str, Any]]:
        state = self._variants_state()
        current = self._current_variant_snapshots()
        return [v for (sid, _u, mv), v in state["rows"].items()
                if sid in current and mv == mapper.MAPPER_VERSION
                and (filters.get("segment") is None or v["vehicle_segment"] == filters["segment"])
                and (filters.get("year_from") is None or (v["shnat_yitzur"] or 0) >= filters["year_from"])
                and (filters.get("year_to") is None
                     or (v["shnat_yitzur"] is not None and v["shnat_yitzur"] <= filters["year_to"]))
                and (filters.get("delek_cd") is None or v["delek_cd"] == filters["delek_cd"])
                and (filters.get("merkav") is None or v["merkav"] == filters["merkav"])]

    @staticmethod
    def _browser_page(items: list[dict[str, Any]], limit: int, offset: int) -> dict[str, Any]:
        if not (1 <= limit <= _MAX_PAGE and 0 <= offset <= 100000):
            raise AppError("CATALOG_BROWSER_QUERY_INVALID", "invalid browser query", 422)
        return {"total": len(items), "limit": limit, "offset": offset, "items": items[offset:offset + limit]}

    @staticmethod
    def _grouped(rows: list[dict[str, Any]], column: str) -> dict[Any, list[dict[str, Any]]]:
        groups: dict[Any, list[dict[str, Any]]] = {}
        for row in rows:
            groups.setdefault(row[column], []).append(row)
        return groups

    def catalog_browser_manufacturers(self, filters: dict[str, Any], *, limit: int, offset: int) -> dict[str, Any]:
        groups = self._grouped(self._current_variants(filters), "tozar")
        return self._browser_page([{"tozar": t, "variants": len(rows)}
                                   for t, rows in sorted(groups.items(), key=lambda i: i[0].encode())],
                                  limit, offset)

    def catalog_browser_models(self, tozar: str, filters: dict[str, Any], *, limit: int, offset: int) -> dict[str, Any]:
        rows = [v for v in self._current_variants(filters) if v["tozar"] == tozar]
        groups = self._grouped(rows, "kinuy_mishari")
        return self._browser_page([
            {"kinuy_mishari": m, "variants": len(rs),
             "year_min": min((r["shnat_yitzur"] for r in rs if r["shnat_yitzur"] is not None), default=None),
             "year_max": max((r["shnat_yitzur"] for r in rs if r["shnat_yitzur"] is not None), default=None)}
            for m, rs in sorted(groups.items(), key=lambda i: (i[0] is None, (i[0] or "").encode()))], limit, offset)

    def catalog_browser_years(self, tozar: str, kinuy_mishari: str, filters: dict[str, Any], *,
                              limit: int, offset: int) -> dict[str, Any]:
        rows = [v for v in self._current_variants(filters)
                if v["tozar"] == tozar and v["kinuy_mishari"] == kinuy_mishari]
        groups = self._grouped(rows, "shnat_yitzur")
        return self._browser_page([{"shnat_yitzur": y, "variants": len(rs)}
                                   for y, rs in sorted(groups.items(), key=lambda i: -(i[0] or 0))],
                                  limit, offset)

    def catalog_browser_variants(self, tozar: str, kinuy_mishari: str, shnat_yitzur: int,
                                 filters: dict[str, Any], *, limit: int, offset: int) -> dict[str, Any]:
        rows = [v for v in self._current_variants(filters) if v["tozar"] == tozar
                and v["kinuy_mishari"] == kinuy_mishari and v["shnat_yitzur"] == shnat_yitzur]
        rows.sort(key=lambda v: ((v["degem_nm"] is None, (v["degem_nm"] or "").encode()),
                                 (v["ramat_gimur"] is None, (v["ramat_gimur"] or "").encode()),
                                 v["upstream_record_id"].encode(), v["id"]))
        page = self._browser_page(rows, limit, offset)
        items = []
        for row in page["items"]:
            item = {k: v for k, v in row.items() if k not in ("id", "snapshot_id", "created_at")}
            item["coverage"] = {
                level: {"status": ledger["status"], "reason_code": ledger.get("reason_code"),
                        "current": ledger["content_sha256"] == row["content_sha256"]}
                for (key, level), ledger in self.catalog_variant_coverage.items()
                if key == row["variant_identity_key"]}
            items.append(item)
        return {**page, "items": items}

    def catalog_browser_facets(self, tozar: str | None) -> dict[str, Any]:
        rows = [v for v in self._current_variants({}) if tozar is None or v["tozar"] == tozar]
        years = [v["shnat_yitzur"] for v in rows if v["shnat_yitzur"] is not None]
        segments = self._grouped(rows, "vehicle_segment")
        fuels = self._grouped([v for v in rows if v["delek_cd"] is not None], "delek_cd")
        bodies = self._grouped([v for v in rows if v["merkav"] is not None], "merkav")
        return {"segments": [{"value": s, "variants": len(r)} for s, r in sorted(segments.items())],
                "fuels": [{"delek_cd": c, "delek_nm": min((x["delek_nm"] for x in r if x["delek_nm"]), default=None),
                           "variants": len(r)} for c, r in sorted(fuels.items())],
                "bodies": [{"merkav": m, "variants": len(r)} for m, r in sorted(bodies.items(),
                                                                             key=lambda i: i[0].encode())],
                "year_min": min(years, default=None), "year_max": max(years, default=None)}


__all__ = ["VariantsMemoryMixin"]

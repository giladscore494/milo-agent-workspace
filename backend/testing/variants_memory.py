"""In-memory mirrors of PR-L1's variant RPCs (migration 20260930000100), mixed
into `MemoryRepository`. The database functions are the authority and are
exercised on ephemeral PostgreSQL (tests/test_catalog_variants_postgres.py);
these mirrors let the build, the capture job and the API run offline."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from backend.catalog import coverage as catalog_coverage
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

    # -- the discovery tree ----------------------------------------------------------------
    def _current_variant_snapshots(self) -> set[str]:
        """Per tozar, the newest-activated complete build under this mapper."""
        newest: dict[str, dict[str, Any]] = {}
        for build in self._variants_state()["builds"].values():
            if build["mapper_version"] != mapper.MAPPER_VERSION or not build["completed_at"]:
                continue
            held = newest.get(build["tozar"])
            if held is None or (str(build["activated_at"]), build["snapshot_id"]) > \
                    (str(held["activated_at"]), held["snapshot_id"]):
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

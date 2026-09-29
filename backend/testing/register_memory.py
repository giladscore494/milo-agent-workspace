"""In-memory mirrors of PR-D1's register RPCs (migration 20260929000100),
mixed into `MemoryRepository`. The database functions are the authority and
are exercised on ephemeral PostgreSQL (tests/test_register_migration_postgres.py);
these mirrors let the API and capture-job tests run offline."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from backend.catalog.government.directory import register_version as directory_version
from backend.catalog.register import retention
from backend.errors import AppError

_TERMINAL = {"completed", "partial_success", "failed", "cancelled", "timed_out", "budget_exhausted"}
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,79}$")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _as_time(value: Any) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


class RegisterMemoryMixin:
    #: What `pg_database_size` answers in these tests (production measured 107 MB).
    register_database_bytes: int = 107 * 1000 * 1000

    def _register_state(self) -> dict[str, Any]:
        state = getattr(self, "_register", None)
        if state is None:
            state = {"directories": [], "groups": {}, "units": {}, "archives": {}}
            self._register = state
        return state

    # -- directory ------------------------------------------------------------
    def record_register_directory(self, resource_id: str, fetched_at: str,
                                  units: list[dict[str, Any]]) -> dict[str, Any]:
        state = self._register_state()
        tozars = [str(unit["tozar"]) for unit in units]
        if (len(tozars) != len(set(tozars)) or len(units) > 5000
                or any(not 1 <= len(t) <= 200 for t in tozars)):
            raise AppError("CATALOG_REGISTER_DIRECTORY_INVALID", "invalid register directory", 409)
        version = directory_version(resource_id, units)
        # Only the CURRENT (newest) version counts: A -> B -> A records A again.
        if state["directories"] and state["directories"][-1]["version"]["register_version"] == version:
            return {"decision": "unchanged", "version": dict(state["directories"][-1]["version"])}
        row = {"id": str(uuid4()), "resource_id": resource_id, "register_version": version,
               "unit_count": len(units), "total_rows": sum(int(u["expected_rows"]) for u in units),
               "fetched_at": fetched_at, "created_at": _now()}
        state["directories"].append({"version": row, "units": sorted(
            ({"tozar": str(u["tozar"]), "expected_rows": int(u["expected_rows"])} for u in units),
            key=lambda u: u["tozar"])})
        return {"decision": "created", "version": dict(row)}

    def latest_register_directory(self) -> dict[str, Any] | None:
        directories = self._register_state()["directories"]
        if not directories:
            return None
        latest = directories[-1]
        return {"version": dict(latest["version"]), "units": [dict(u) for u in latest["units"]]}

    # -- capture requests --------------------------------------------------------
    def request_register_capture(self, register_version: str, tozars: list[str], requested_by: UUID, *,
                                 group_max_rows: int, capacity_limit_bytes: int, bytes_per_row: int,
                                 grace_seconds: int) -> dict[str, Any]:
        state = self._register_state()
        with self.lock:
            if not tozars or len(set(tozars)) != len(tozars):
                raise AppError("CATALOG_REGISTER_REQUEST_INVALID", "invalid", 409)
            latest = self.latest_register_directory()
            if latest is None or latest["version"]["register_version"] != register_version:
                raise AppError("CATALOG_REGISTER_VERSION_STALE", "stale", 409)
            expected = {u["tozar"]: u["expected_rows"] for u in latest["units"]}
            grace = timedelta(seconds=grace_seconds)
            new: list[str] = []
            for tozar in tozars:
                if tozar not in expected:
                    raise AppError("CATALOG_REGISTER_UNIT_UNKNOWN", "unknown", 409)
                unit = state["units"].get((register_version, tozar))
                if unit is None:
                    new.append(tozar)
                    continue
                if unit["status"] == "captured":
                    continue
                retry = unit["status"] == "failed" or self._group_stale(unit["group_id"], grace)
                if retry:
                    new.append(tozar)
            if not new:
                return {"decision": "existing", "group": None, "units": sorted(
                    (dict(state["units"][(register_version, t)]) for t in tozars
                     if (register_version, t) in state["units"]), key=lambda u: u["tozar"])}
            rows = sum(expected[t] for t in new)
            if len(new) > 1 and rows > group_max_rows:
                raise AppError("CATALOG_REGISTER_GROUP_TOO_LARGE", "too large", 409)
            inflight = sum(int(u["expected_rows"]) for u in state["units"].values()
                           if u["status"] in ("requested", "capturing")
                           and not (u["register_version"] == register_version and u["tozar"] in new)
                           and not self._group_stale(u["group_id"], grace))
            current = int(self.register_database_bytes)
            projected = current + (rows + inflight) * int(bytes_per_row)
            if projected > capacity_limit_bytes:
                raise AppError("CATALOG_CAPACITY_THRESHOLD_EXCEEDED",
                               f"current={current} projected={projected} limit={capacity_limit_bytes}", 409)
            group = {"id": str(uuid4()), "kind": "capture", "register_version": register_version,
                     "requested_by": str(requested_by),
                     "expected_rows": rows, "run_id": None, "trigger_state": "claimed", "execution_name": None,
                     "claimed_at": _now(), "triggered_at": None, "updated_at": _now()}
            state["groups"][group["id"]] = group
            for tozar in new:
                previous = state["units"].get((register_version, tozar))
                state["units"][(register_version, tozar)] = {
                    "id": previous["id"] if previous else str(uuid4()), "group_id": group["id"],
                    "register_version": register_version, "tozar": tozar, "expected_rows": expected[tozar],
                    "attempt": (previous["attempt"] + 1) if previous else 1, "status": "requested",
                    "failure_code": None, "snapshot_id": None, "snapshot_key": None, "api_total": None,
                    "captured_rows": None, "count_verified": None, "measured_bytes": None,
                    "measurement_method": None, "created_at": _now(), "updated_at": _now()}
            return {"decision": "claimed", "group": dict(group), "units": sorted(
                (dict(u) for u in state["units"].values() if u["group_id"] == group["id"]),
                key=lambda u: u["tozar"])}

    def _group_stale(self, group_id: str, grace: timedelta) -> bool:
        """Mirror of public.catalog_register_group_stale."""
        group = self._register_state()["groups"].get(str(group_id))
        if group is None:
            return True
        now = datetime.now(UTC)
        run = self.runs.get(str(group.get("run_id"))) if group.get("run_id") else None
        started = _as_time(group.get("triggered_at") or group["claimed_at"])
        return bool(
            group["trigger_state"] == "trigger_failed"
            or (group["trigger_state"] == "claimed" and not group.get("run_id")
                and _as_time(group["claimed_at"]) < now - grace)
            or (run is not None and run.get("status") in _TERMINAL)
            or (run is not None and run.get("status") == "queued" and not run.get("worker_id") and started < now - grace)
            or (run is not None and run.get("status") in ("starting", "running") and run.get("lease_expires_at")
                and _as_time(run["lease_expires_at"]) < now - grace))

    def request_register_directory_refresh(self, requested_by: UUID, *, grace_seconds: int) -> dict[str, Any]:
        """Mirror of public.request_register_directory_refresh: one live refresh at a time."""
        state = self._register_state()
        with self.lock:
            latest = max((g for g in state["groups"].values() if g.get("kind") == "directory"),
                         key=lambda g: str(g["claimed_at"]), default=None)
            if latest is not None and not self._group_stale(latest["id"], timedelta(seconds=grace_seconds)):
                return {"decision": "existing", "group": dict(latest)}
            group = {"id": str(uuid4()), "kind": "directory", "register_version": None,
                     "requested_by": str(requested_by), "expected_rows": 0, "run_id": None,
                     "trigger_state": "claimed", "execution_name": None, "claimed_at": _now(),
                     "triggered_at": None, "updated_at": _now()}
            state["groups"][group["id"]] = group
            return {"decision": "claimed", "group": dict(group)}

    def record_register_capture_trigger(self, group_id: Any, *, run_id: Any, trigger_state: str,
                                        execution_name: str | None) -> dict[str, Any]:
        state = self._register_state()
        with self.lock:
            group = state["groups"].get(str(group_id))
            if group is None or group["trigger_state"] != "claimed" or (
                    group["run_id"] is not None and str(run_id) != str(group["run_id"])):
                raise AppError("CATALOG_REGISTER_TRIGGER_CONFLICT", "conflict", 409)
            if run_id is not None:
                group["run_id"] = str(run_id)
            group["trigger_state"] = trigger_state
            if trigger_state != "claimed":
                group["execution_name"] = execution_name
                group["triggered_at"] = _now()
            if trigger_state == "trigger_failed":
                for unit in state["units"].values():
                    if unit["group_id"] == group["id"] and unit["status"] == "requested":
                        unit.update(status="failed", failure_code="CATALOG_REGISTER_TRIGGER_FAILED",
                                    updated_at=_now())
            return dict(group)

    def register_capture_group(self, group_id: Any) -> dict[str, Any]:
        state = self._register_state()
        group = state["groups"].get(str(group_id))
        return {"group": dict(group) if group else None, "units": sorted(
            (dict(u) for u in state["units"].values() if u["group_id"] == str(group_id)),
            key=lambda u: u["tozar"])}

    def register_capture_groups(self, group_ids: list[str]) -> list[dict[str, Any]]:
        groups = self._register_state()["groups"]
        return [dict(groups[g]) for g in group_ids if g in groups]

    def register_capture_units(self) -> list[dict[str, Any]]:
        return [dict(u) for u in self._register_state()["units"].values()]

    # -- the capture job's writes ---------------------------------------------------
    def _snapshot_by_id(self, snapshot_id: str) -> dict[str, Any] | None:
        for row in self.catalog_snapshots.values():
            if str(row["id"]) == str(snapshot_id):
                return row
        return None

    def count_catalog_raw_records(self, snapshot_id: str) -> int:
        return sum(1 for (sid, _key) in self.catalog_raw_records if str(sid) == str(snapshot_id))

    def _snapshot_bytes(self, snapshot_id: str) -> int:
        size = sum(len(json.dumps(row, default=str)) for (sid, _k), row in self.catalog_raw_records.items()
                   if str(sid) == str(snapshot_id))
        return size + sum(len(json.dumps(row, default=str)) for (sid, _k), row in self.catalog_candidates.items()
                          if str(sid) == str(snapshot_id))

    def record_register_unit_status(self, run_id: UUID, unit_id: str, status: str, failure_code: str | None,
                                    snapshot_id: str | None, api_total: int | None, captured_rows: int | None,
                                    count_verified: bool | None, *, worker_id: str, attempt: int,
                                    lease_token: str) -> dict[str, Any]:
        state = self._register_state()
        with self.lock:
            self._catalog_lease(run_id, worker_id, attempt, lease_token)
            if status not in ("capturing", "captured", "failed") or (
                    status == "failed" and not (failure_code and _CODE.fullmatch(failure_code))) or (
                    status != "failed" and failure_code is not None):
                raise AppError("CATALOG_REGISTER_REQUEST_INVALID", "invalid", 409)
            unit = next((u for u in state["units"].values() if u["id"] == str(unit_id)), None)
            if unit is None:
                raise AppError("CATALOG_REGISTER_UNIT_UNKNOWN", "unknown", 409)
            if str(state["groups"][unit["group_id"]].get("run_id")) != str(run_id):
                raise AppError("CATALOG_REGISTER_UNIT_NOT_THIS_RUN", "not this run", 409)
            if unit["status"] == "captured":
                return dict(unit)
            snapshot = self._snapshot_by_id(snapshot_id) if snapshot_id else None
            if status == "captured" and (
                    snapshot is None or snapshot.get("activated_at") is None or count_verified is not True
                    or api_total is None or captured_rows is None or api_total != captured_rows
                    or int(snapshot.get("stored_record_count") or 0) != captured_rows
                    or str(snapshot["id"]) not in state["archives"]):
                raise AppError("CATALOG_REGISTER_CAPTURE_UNVERIFIED", "unverified", 409)
            unit.update(status=status, failure_code=failure_code, updated_at=_now())
            if snapshot is not None:
                unit.update(snapshot_id=str(snapshot["id"]), snapshot_key=snapshot["snapshot_key"])
                if status != "capturing":
                    unit.update(measured_bytes=self._snapshot_bytes(snapshot["id"]),
                                measurement_method="memory:json_length")
            for key, value in (("api_total", api_total), ("captured_rows", captured_rows),
                               ("count_verified", count_verified)):
                if value is not None:
                    unit[key] = value
            return dict(unit)

    def record_register_snapshot_archive(self, run_id: UUID, snapshot_id: str, gcs_uri: str, byte_size: int,
                                         sha256: str, line_count: int, *, worker_id: str, attempt: int,
                                         lease_token: str) -> dict[str, Any]:
        state = self._register_state()
        with self.lock:
            self._catalog_lease(run_id, worker_id, attempt, lease_token)
            snapshot = self._snapshot_by_id(snapshot_id)
            if snapshot is None:
                raise AppError("CATALOG_REGISTER_REQUEST_INVALID", "unknown snapshot", 409)
            if not str(gcs_uri).endswith(f"/{snapshot['snapshot_key']}.jsonl.gz") \
                    or f"/register/{snapshot.get('resource_id')}/" not in str(gcs_uri):
                raise AppError("CATALOG_REGISTER_REQUEST_INVALID", "the archive object does not name this snapshot", 409)
            lines = sum(1 for (sid, _k), row in self.catalog_raw_records.items()
                        if str(sid) == str(snapshot_id) and "capture_index" in (row.get("source_locator") or {}))
            if lines != line_count or int(snapshot.get("declared_record_count") or -1) != line_count:
                raise AppError("CATALOG_CAPTURE_COUNT_MISMATCH", "count mismatch", 409)
            existing = state["archives"].get(str(snapshot_id))
            if existing is not None:
                if (existing["sha256"], existing["gcs_uri"], existing["byte_size"]) == (sha256, gcs_uri, byte_size):
                    return dict(existing)
                raise AppError("CATALOG_ARCHIVE_CONFLICT", "conflict", 409)
            row = {"id": str(uuid4()), "snapshot_id": str(snapshot_id), "snapshot_key": snapshot["snapshot_key"],
                   "gcs_uri": gcs_uri, "byte_size": int(byte_size), "sha256": sha256, "line_count": int(line_count),
                   "line_basis": "source_locator.capture_index+1", "recorded_by_run_id": str(run_id),
                   "created_at": _now()}
            state["archives"][str(snapshot_id)] = row
            return dict(row)

    def register_snapshot_archive(self, snapshot_id: str) -> dict[str, Any] | None:
        row = self._register_state()["archives"].get(str(snapshot_id))
        return dict(row) if row else None

    def catalog_database_bytes(self) -> int:
        return int(self.register_database_bytes)

    # -- retention ----------------------------------------------------------------------
    def _prunable(self) -> list[dict[str, Any]]:
        referenced_ids: set[str] = set()
        for link in self.catalog_evidence_links.values():
            referenced_ids.add(str(link.get("snapshot_id")))
        for row in (list(self.catalog_canonical_field_provenance) + list(self.catalog_snapshot_adoptions)
                    + list(self.work_scope_units) + list(self.work_scope_batches)
                    + list(self.work_scope_queue_items)):
            if row.get("snapshot_id"):
                referenced_ids.add(str(row["snapshot_id"]))
        referenced_keys = {str(row.get("snapshot_key")) for row in self.catalog_variant_coverage.values()}
        for checkpoint in self.checkpoints:
            government = (checkpoint.get("artifacts") or {}).get("government")
            if isinstance(government, dict) and government.get("snapshot_key"):
                referenced_keys.add(str(government["snapshot_key"]))
        live = {rid for rid, run in self.runs.items() if run.get("status") not in _TERMINAL}
        latest_unit: dict[str, dict[str, Any]] = {}
        for unit in sorted(self._register_state()["units"].values(), key=lambda u: str(u["updated_at"])):
            if unit["status"] == "captured" and unit.get("snapshot_id"):
                latest_unit[unit["tozar"]] = unit
        referenced_ids |= {str(u["snapshot_id"]) for u in latest_unit.values()}
        rows = retention.prunable(self.catalog_snapshots.values(), referenced_ids=referenced_ids,
                                  referenced_keys=referenced_keys, live_run_ids=live)
        return [{"snapshot_id": str(r["id"]), "snapshot_key": r["snapshot_key"],
                 "tozar": retention.scoped_tozar(r), "validation_state": r.get("validation_state"),
                 "activated_at": r.get("activated_at"), "raw_rows": self.count_catalog_raw_records(r["id"]),
                 "estimated_bytes": self._snapshot_bytes(r["id"])} for r in rows]

    def prunable_register_snapshots(self) -> list[dict[str, Any]]:
        return self._prunable()

    def prune_register_snapshots(self, snapshot_keys: list[str], digest: str) -> dict[str, Any]:
        with self.lock:
            rows = self._prunable()
            current = retention.prune_digest(r["snapshot_key"] for r in rows)
            if current != digest or retention.prune_digest(snapshot_keys) != digest:
                raise AppError("CATALOG_PRUNE_DIGEST_MISMATCH", "digest mismatch", 409)
            ids = {r["snapshot_id"] for r in rows}
            candidates = [k for k in self.catalog_candidates if str(k[0]) in ids]
            records = [k for k in self.catalog_raw_records if str(k[0]) in ids]
            for key in candidates:
                del self.catalog_candidates[key]
            for key in records:
                del self.catalog_raw_records[key]
            for key in [k for k, row in self.catalog_snapshots.items() if str(row["id"]) in ids]:
                del self.catalog_snapshots[key]
            return {"snapshots": len(ids), "raw_records": len(records), "candidates": len(candidates),
                    "digest": digest}


__all__ = ["RegisterMemoryMixin"]

"""In-memory mirror of PR-D3's manufacturer normalisation RPCs (migration
20261003000100), mixed into `MemoryRepository`. The database functions are the
authority (tests/test_manufacturer_normalization_postgres.py); this mirror
lets the API, the capture job's mode and the approval run offline."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from backend.catalog.register import normalization
from backend.errors import AppError


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _refused(code: str) -> AppError:
    return AppError(code, code.lower().replace("_", " "), 409)


class NormalizationMemoryMixin:
    def _norm_state(self) -> dict[str, Any]:
        state = getattr(self, "_normalization", None)
        if state is None:
            state = {"proposals": {}, "versions": [], "roles": {}, "rejections": []}
            self._normalization = state
        return state

    def project_member_role(self, project_id: UUID, user_id: UUID) -> str | None:
        key = (str(project_id), str(user_id))
        if key not in self.members:
            return None
        return self._norm_state()["roles"].get(key, "owner")

    def manufacturer_normalization_current(self) -> dict[str, Any]:
        versions = self._norm_state()["versions"]
        if not versions:
            return {"version": 0, "entries": []}
        latest = versions[-1]
        return {"version": latest["version"], "entries": [
            dict(entry) for _name, entry in sorted(latest["entries"].items(), key=lambda i: i[0].encode())]}

    def catalog_manufacturer_evidence(self) -> list[dict[str, Any]]:
        by_tozar: dict[str, list[dict[str, Any]]] = {}
        for row in self._current_variants({}):
            by_tozar.setdefault(row["tozar"], []).append(row)
        return [{"tozar": tozar, "rows": len(rows),
                 "tozeret_cd": sorted({r["tozeret_cd"] for r in rows if r.get("tozeret_cd") is not None}),
                 "tozeret_nm": sorted({r["tozeret_nm"] for r in rows if r.get("tozeret_nm")})[:3],
                 "samples": sorted({r["kinuy_mishari"] for r in rows if r.get("kinuy_mishari")})[:3]}
                for tozar, rows in sorted(by_tozar.items(), key=lambda i: i[0].encode())]

    def request_manufacturer_normalization(self, requested_by: UUID, input_rows: list[dict[str, Any]], *,
                                           grace_seconds: int) -> dict[str, Any]:
        names = [row.get("name") for row in input_rows]
        if not input_rows or len(input_rows) > normalization.MAX_INPUT_NAMES or len(set(names)) != len(names) \
                or not all(isinstance(n, str) and 1 <= len(n) <= 200 for n in names) \
                or any(not isinstance(v, str) or len(v) > normalization.MAX_SAMPLE_CHARS
                       for row in input_rows for v in [*(row.get("tozeret_nm") or []), *(row.get("samples") or [])]):
            raise _refused("CATALOG_NORMALIZATION_REQUEST_INVALID")
        digest = hashlib.sha256(json.dumps(input_rows, sort_keys=True).encode()).hexdigest()
        register, state = self._register_state(), self._norm_state()
        with self.lock:
            latest = max((g for g in register["groups"].values() if g.get("kind") == "normalisation"),
                         key=lambda g: str(g["claimed_at"]), default=None)
            if latest is not None and not self._group_stale(latest["id"], timedelta(seconds=grace_seconds)):
                proposal = next(p for p in state["proposals"].values() if p["group_id"] == latest["id"])
                return {"decision": "existing", "group": dict(latest),
                        "proposal": {k: v for k, v in proposal.items() if k != "input"}}
            answered = self.latest_manufacturer_normalization_proposal("proposed")
            if answered is not None and answered["input_sha256"] == digest:
                return {"decision": "reused", "group": None, "proposal": answered}
            if latest is not None:
                ready_at = datetime.fromisoformat(str(latest["claimed_at"])) + timedelta(
                    seconds=normalization.REQUEST_COOLDOWN_SECONDS)
                if ready_at > datetime.now(UTC):
                    proposal = next(p for p in state["proposals"].values() if p["group_id"] == latest["id"])
                    return {"decision": "cooldown", "group": dict(latest),
                            "proposal": {k: v for k, v in proposal.items() if k != "input"},
                            "retry_after_seconds": int((ready_at - datetime.now(UTC)).total_seconds()) + 1}
            group = {"id": str(uuid4()), "kind": "normalisation", "register_version": None,
                     "requested_by": str(requested_by), "expected_rows": 0, "run_id": None,
                     "trigger_state": "claimed", "execution_name": None, "claimed_at": _now(),
                     "triggered_at": None, "updated_at": _now()}
            register["groups"][group["id"]] = group
            proposal = {"id": str(uuid4()), "group_id": group["id"], "requested_by": str(requested_by),
                        "input": json.loads(json.dumps(input_rows)),
                        "input_sha256": digest,
                        "status": "requested", "groups": None, "reason_code": None, "model": None,
                        "created_at": _now(), "updated_at": _now()}
            state["proposals"][proposal["id"]] = proposal
            return {"decision": "claimed", "group": dict(group),
                    "proposal": {k: v for k, v in proposal.items() if k != "input"}}

    def latest_manufacturer_normalization_proposal(self, status: str | None = None) -> dict[str, Any] | None:
        proposals = [p for p in self._norm_state()["proposals"].values() if status in (None, p["status"])]
        latest = max(proposals, key=lambda p: (str(p["created_at"]), p["id"]), default=None)
        return None if latest is None else {k: v for k, v in latest.items() if k != "input"}

    def _latest_normalisation_group(self) -> dict[str, Any] | None:
        return max((g for g in self._register_state()["groups"].values() if g.get("kind") == "normalisation"),
                   key=lambda g: (str(g["claimed_at"]), g["id"]), default=None)

    def requested_manufacturer_normalization(self) -> dict[str, Any] | None:
        group = self._latest_normalisation_group()
        if group is None or group.get("run_id") is None:
            return None
        proposal = next(p for p in self._norm_state()["proposals"].values() if p["group_id"] == group["id"])
        return {"id": proposal["id"], "run_id": str(group["run_id"])} if proposal["status"] == "requested" else None

    def claim_manufacturer_normalization(self, proposal_id: str, run_id: UUID, worker_id: str,
                                         lease_seconds: int = 300) -> dict[str, Any]:
        with self.lock:
            if self.requested_manufacturer_normalization() != {"id": str(proposal_id), "run_id": str(run_id)}:
                raise _refused("CATALOG_NORMALIZATION_NOT_REQUESTED")
            try:
                return self.claim_run(UUID(str(run_id)), worker_id, lease_seconds=lease_seconds)
            except AppError:
                raise _refused("CATALOG_NORMALIZATION_ALREADY_CLAIMED") from None

    def manufacturer_normalization_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        proposal = self._norm_state()["proposals"].get(str(proposal_id))
        if proposal is None:
            return None
        group = self._register_state()["groups"].get(proposal["group_id"]) or {}
        return {**json.loads(json.dumps(proposal)), "run_id": group.get("run_id")}

    def record_manufacturer_normalization_proposal(self, run_id: UUID, proposal_id: str, status: str,
                                                   groups: list[dict[str, Any]] | None, reason_code: str | None,
                                                   model: str, *, worker_id: str, attempt: int,
                                                   lease_token: str) -> dict[str, Any]:
        with self.lock:
            self._catalog_lease(run_id, worker_id, attempt, lease_token)
            proposal = self._norm_state()["proposals"].get(str(proposal_id))
            group = self._register_state()["groups"].get((proposal or {}).get("group_id")) or {}
            if proposal is None or str(group.get("run_id")) != str(run_id):
                raise _refused("CATALOG_NORMALIZATION_NOT_THIS_RUN")
            if proposal["status"] != "requested":
                raise _refused("CATALOG_NORMALIZATION_ALREADY_RECORDED")
            names = [row["name"] for row in proposal["input"]]
            if status == "proposed":
                try:
                    valid = normalization.validate_groups(json.dumps({"groups": groups}), names) == groups
                except normalization.NormalizationRefused:
                    valid = False
                if not valid or reason_code is not None:
                    raise _refused("CATALOG_NORMALIZATION_OUTPUT_INVALID")
            elif status != "refused" or groups is not None or not reason_code:
                raise _refused("CATALOG_NORMALIZATION_OUTPUT_INVALID")
            proposal.update(status=status, groups=groups, reason_code=reason_code, model=model, updated_at=_now())
            return {k: v for k, v in proposal.items() if k != "input"}

    def reject_manufacturer_normalization_group(self, rejected_by: UUID, group: dict[str, Any]) -> dict[str, Any]:
        state = self._norm_state()
        with self.lock:
            members = sorted(group.get("members") or [], key=str.encode)
            rule, proposal_id = group.get("rule_id"), group.get("proposal_id")
            proposal = state["proposals"].get(str(proposal_id)) if proposal_id else None
            tozars = {unit["tozar"] for unit in (self.latest_register_directory() or {"units": []})["units"]}
            if not members or len(set(members)) != len(members) or bool(rule) == bool(proposal_id) or (
                    rule and (rule not in normalization.RULES or not set(members) <= tozars)) or (
                    proposal_id and (proposal is None or proposal["status"] != "proposed" or not any(
                        g["canonical"] == group.get("canonical") and sorted(g["members"], key=str.encode) == members
                        for g in proposal.get("groups") or []))):
                raise _refused("CATALOG_NORMALIZATION_REJECTION_INVALID")
            row = {"canonical_name": group["canonical"], "members": members, "rule_id": rule,
                   "proposal_id": proposal_id, "rejected_by": str(rejected_by)}
            # One record per group (the SQL's group_key): the first rejection stands.
            key = {k: row[k] for k in ("canonical_name", "members", "rule_id", "proposal_id")}
            existing = next((r for r in state["rejections"]
                             if {k: r[k] for k in key} == key), None)
            if existing is None:
                state["rejections"].append(row)
            return dict(existing or row)

    def manufacturer_normalization_rejections(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._norm_state()["rejections"]]

    def approve_manufacturer_normalization(self, approved_by: UUID, expected_version: int,
                                           entries: list[dict[str, Any]]) -> dict[str, Any]:
        state = self._norm_state()
        with self.lock:
            current = state["versions"][-1] if state["versions"] else {"version": 0, "entries": {}}
            if current["version"] != int(expected_version):
                raise _refused("CATALOG_NORMALIZATION_VERSION_STALE")
            directory = self.latest_register_directory() or {"units": []}
            tozars = {unit["tozar"] for unit in directory["units"]}
            new: dict[str, dict[str, Any]] = {}
            for entry in entries:
                name, provenance = entry.get("source_tozar"), entry.get("provenance")
                proposal = state["proposals"].get(str(entry.get("proposal_id")))
                group = next((g for g in (proposal or {}).get("groups") or []
                              if name in g["members"] and g["canonical"] == entry.get("canonical_name")), None)
                if name not in tozars or name in new or (
                        provenance == "rule" and (entry.get("rule_id") not in normalization.RULES
                                                  or "proposal_id" in entry)) or (
                        provenance == "model" and ("rule_id" in entry or proposal is None
                                                   or proposal["status"] != "proposed" or group is None)) or (
                        provenance not in ("rule", "model")):
                    raise _refused("CATALOG_NORMALIZATION_APPROVAL_INVALID")
                new[name] = {"source_tozar": name, "canonical_name": entry["canonical_name"],
                             "provenance": provenance, "rule_id": entry.get("rule_id"),
                             "proposal_id": entry.get("proposal_id"),
                             "confidence": group["confidence"] if group else None, "approved_at": _now()}
            merged = {**current["entries"], **new}
            state["versions"].append({"version": current["version"] + 1, "approved_by": str(approved_by),
                                      "entries": merged})
            return {"version": current["version"] + 1, "entry_count": len(merged)}

"""A small stateful PostgREST stand-in for the Stage D db-probe tests.

The Stage D cleanup has to prove things about DATABASE state: that an
interrupted run really became terminal, that no active run is left holding
a concurrency slot, and that no budget reservation is left dangling in
`reserved`. A mock that just returns canned rows cannot show that, because
the whole question is whether the probe's writes actually changed
anything.

So this keeps real mutable rows and implements the narrow slice of
PostgREST the probe uses: `eq.` filters, `in.(…)` filters, PATCH with
filters (returning the rows it actually matched), the exact-count header
path, and the RPCs the cleanup calls: `settle_model_call_budget`, the
`claim_run_lease` the canonical cleanup acquires a REAL lease through, and
`finalize_run_guarded`, the only primitive allowed to commit a terminal status
and its terminal event together — each with its own
`status = 'reserved'` compare-and-set, so releasing a reservation twice
fails here exactly as it would in production.

It is deliberately not a general PostgREST: an unrecognised request raises
rather than returning an empty list, because a silently empty result would
let a test pass for the wrong reason.
"""

from __future__ import annotations

import json
import urllib.parse
from datetime import UTC, datetime, timedelta


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _in_seconds(seconds) -> str:
    return (datetime.now(UTC) + timedelta(seconds=int(seconds))).isoformat().replace("+00:00", "Z")

TERMINAL = ("completed", "partial_success", "failed", "cancelled", "timed_out", "budget_exhausted")
ACTIVE = ("queued", "launching", "starting", "running", "waiting", "cancellation_requested")


class FakePostgrest:
    def __init__(self, runs=None, reservations=None, conversations=None):
        self.runs = [dict(r) for r in (runs or [])]
        self.reservations = [dict(r) for r in (reservations or [])]
        self.conversations = [dict(c) for c in (conversations or [])]
        self.calls: list[tuple[str, str]] = []
        self.rpc_calls: list[dict] = []
        #: Terminal events the canonical finalizer committed with a status.
        self.events: list[dict] = []

    # -- helpers --------------------------------------------------------
    @staticmethod
    def _parse(path: str) -> tuple[str, dict[str, list[str]]]:
        table, _, query = path.partition("?")
        return table, urllib.parse.parse_qs(query, keep_blank_values=True)

    @staticmethod
    def _matches(row: dict, params: dict[str, list[str]]) -> bool:
        for key, values in params.items():
            if key in ("select", "limit", "order", "offset"):
                continue
            spec = values[0]
            if spec.startswith("eq."):
                if str(row.get(key)) != spec[3:]:
                    return False
            elif spec.startswith("in."):
                allowed = {v.strip() for v in spec[3:].strip("()").split(",") if v.strip()}
                if str(row.get(key)) not in allowed:
                    return False
            else:
                raise AssertionError(f"fake PostgREST: unsupported filter {key}={spec}")
        return True

    def _table(self, name: str) -> list[dict]:
        return {
            "/rest/v1/runs": self.runs,
            "/rest/v1/model_call_budget_reservations": self.reservations,
            "/rest/v1/conversations": self.conversations,
        }[name]

    # -- the probe's HTTP surface ---------------------------------------
    def call(self, method: str, path: str, body=None, headers=None):
        self.calls.append((method, path))

        if path == "/rest/v1/rpc/settle_model_call_budget":
            self.rpc_calls.append(dict(body or {}))
            if (body or {}).get("p_status") not in ("settled", "released", "overage"):
                return 400, {"message": "invalid model-call settlement status"}
            for row in self.reservations:
                if str(row.get("id")) == str((body or {}).get("p_reservation_id")):
                    # The RPC's own CAS: only a still-reserved row settles.
                    if row.get("status") != "reserved":
                        return 400, {"message": "reservation already settled or missing"}
                    row["status"] = (body or {}).get("p_status")
                    row["actual_cost"] = (body or {}).get("p_actual_cost")
                    row["rejection_reason"] = (body or {}).get("p_rejection_reason")
                    return 200, [row]
            return 400, {"message": "reservation already settled or missing"}

        if path == "/rest/v1/rpc/claim_run_lease":
            self.rpc_calls.append(dict(body or {}))
            # Parity with `claim_run_lease`: an identity-less run is not
            # claimable at all, a live foreign lease matches no row (so the
            # caller waits for database-clock expiry instead of stealing), and a
            # reclaim after expiry increments the attempt.
            run_id = str((body or {}).get("p_run_id"))
            worker_id = (body or {}).get("p_worker_id")
            for row in self.runs:
                if str(row.get("id")) != run_id:
                    continue
                if not isinstance(row.get("run_identity"), dict):
                    return 200, []
                holder, expires = row.get("worker_id"), row.get("lease_expires_at")
                expired = not expires or expires <= utc_now_iso()
                if holder and holder != worker_id and not expired:
                    return 200, []
                attempt = int(row.get("attempt") or 1)
                if holder and holder != worker_id and expired:
                    attempt += 1
                row.update({
                    "status": row["status"] if row["status"] == "cancellation_requested" else "starting",
                    "worker_id": worker_id, "attempt": attempt,
                    "lease_token": f"stage-d-cleanup-lease-{attempt}",
                    "lease_expires_at": _in_seconds((body or {}).get("p_lease_seconds") or 300),
                })
                return 200, [dict(row)]
            return 200, []

        if path == "/rest/v1/rpc/finalize_run_guarded":
            self.rpc_calls.append(dict(body or {}))
            # Parity with `finalize_run_guarded`: terminal status only, a
            # mandatory compare-and-set on the observed status, and the full
            # run + worker + attempt + lease fence. The terminal event commits
            # with the status, so a rejected finalization leaves neither.
            payload = dict(body or {})
            if payload.get("p_status") not in TERMINAL:
                return 400, {"message": "finalize_run_guarded requires a terminal status"}
            run_id = str(payload.get("p_run_id"))
            for row in self.runs:
                if str(row.get("id")) != run_id:
                    continue
                fenced = (row.get("worker_id") == payload.get("p_worker_id")
                          and int(row.get("attempt") or 0) == int(payload.get("p_attempt") or -1)
                          and row.get("lease_token") == payload.get("p_lease_token"))
                if not fenced:
                    return 400, {"message": "STALE_WORKER_WRITE"}
                if row.get("status") != payload.get("p_expected_status"):
                    return 400, {"message": "STALE_WORKER_WRITE: status moved"}
                row.update({"status": payload["p_status"],
                            "finished_at": payload.get("p_finished_at"),
                            "error": payload.get("p_error")})
                if payload.get("p_event_type"):
                    self.events.append({"run_id": run_id, "event_type": payload["p_event_type"],
                                        "payload": payload.get("p_event_payload") or {}})
                return 200, [dict(row)]
            return 400, {"message": "run not found"}

        table, params = self._parse(path)
        rows = self._table(table)

        if method == "GET":
            return 200, [dict(r) for r in rows if self._matches(r, params)]

        if method == "PATCH":
            matched = [r for r in rows if self._matches(r, params)]
            for row in matched:
                row.update(body or {})
            return 200, [dict(r) for r in matched]

        raise AssertionError(f"fake PostgREST: unsupported {method} {path}")

    def count_exact(self, path: str):
        table, params = self._parse(path)
        return sum(1 for r in self._table(table) if self._matches(r, params))

    # -- assertions the tests make --------------------------------------
    def run(self, run_id: str) -> dict:
        return next(r for r in self.runs if str(r["id"]) == run_id)

    def reserved_count(self, run_id: str) -> int:
        return sum(1 for r in self.reservations
                   if str(r.get("run_id")) == run_id and r.get("status") == "reserved")

    def mutating_calls(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p in self.calls if m in ("POST", "PATCH", "PUT", "DELETE")]


def wire(db, monkeypatch, fake: FakePostgrest) -> FakePostgrest:
    monkeypatch.setattr(db, "call", fake.call)
    monkeypatch.setattr(db, "count_exact", fake.count_exact)
    return fake

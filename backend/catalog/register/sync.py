"""PR-SYNC-1: one incremental register sync, inside the capture job (`--register-sync`).

One execution, one run lease, no operator decision:

1. Check (2 requests): `package_show` and the register's exact total. Both as
   the last sync recorded them for the CURRENT directory version (its `basis`)
   and nothing left to do: straight to the rolling refresh. More moved tozars
   than the budget can count ends the run `GOV_SYNC_DIRECTORY_TOO_LARGE`
   (one Refresh directory resolves it), never a silent loop.
2. Light directory, only on a change or when a tozar captured under the
   current version has another count: the directory's own scan, and a `_count` only for a
   tozar whose count moved or that is new (`directory.light_directory`),
   recorded through `record_register_directory` (a new version only on change).
3. Diff, no requests, recomputed from the database every run: tozars never
   captured, then those whose last unit failed or went stale, then those whose
   captured count differs from the directory (captured under an older
   version), each class in UTF-8 byte order. Nothing is kept anywhere else,
   so a crashed, throttled or cancelled run loses nothing, and nothing is ever
   given up.
4. Rolling refresh, only when that backlog is empty: the `SYNC_REFRESH_TOZARS`
   tozars captured longest ago under an older directory version (a tozar
   already captured under the current version is answered `existing` by the
   database and is never re-requested). An unchanged one re-captures into the
   SAME snapshot (the reuse path).
5. Capture: consecutive slices of the work list, each claimed through
   `request_register_capture` (group cap and capacity guard), bound to THIS
   run (`record_register_capture_trigger`) and captured by `capture_group`.
   One run owns its groups one after another; nothing runs in parallel.
6. Budget: every send, retries included, is charged to ONE `_Budget` of
   `SYNC_MAX_REQUESTS` (`MeteredTransport`). A unit whose estimate does not fit
   what is left is not started. The client has NO throttle schedule: the
   firewall's first answer (403 page or 429) ends the sync (`GOV_SYNC_THROTTLED`),
   and nothing more is sent.
7. One summary line (`SYNC_SUMMARY|...`), on stdout and in the run's output.
"""

from __future__ import annotations

import math
import time
from typing import Any, Callable, Mapping

from backend.catalog.government import source as src
from backend.catalog.government.directory import (DEFAULT_MAX_SECONDS, _Budget, light_directory,
                                                  source_total)
from backend.catalog.government.source import GovernmentSourceError
from backend.catalog.register import config as register_config
from backend.catalog.register import service
from backend.catalog.register.capture import RegisterCaptureError, capture_group
from backend.errors import AppError

#: The hard request cap of one sync: every data.gov.il send, retries included.
SYNC_MAX_REQUESTS = 80
#: Tozars re-captured per run by the rolling refresh, once the backlog is empty.
SYNC_REFRESH_TOZARS = 2
#: Requests kept free when fitting a unit: one retry never fails it halfway.
SYNC_RETRY_HEADROOM = 1
_BASIS = ("upstream_version", "metadata_modified", "source_total")


def _utf8(value: str) -> bytes:
    return value.encode("utf-8")


class MeteredTransport:
    """The transport of a sync's client: each send is charged to the run's
    budget BEFORE it goes out, the firewall's answer is remembered, and after
    it nothing is sent at all."""

    def __init__(self, transport: Any, *, max_requests: int = SYNC_MAX_REQUESTS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._transport = transport
        self.budget = _Budget(max_requests=max_requests, max_seconds=math.inf, clock=clock)
        self.throttled = False

    @property
    def remaining(self) -> int:
        return self.budget.max_requests - self.budget.used

    def get(self, url: str, **kwargs: Any) -> Any:
        if self.throttled:
            raise GovernmentSourceError("GOV_SYNC_THROTTLED")
        self.budget.spend()
        response = self._transport.get(url, **kwargs)
        media = str(response.content_type).split(";", 1)[0].strip().lower()
        if int(response.status) == 429 or (int(response.status) == 403 and media in src.WAF_BLOCK_MEDIA_TYPES):
            self.throttled = True
        return response


def estimate(rows: int, page_limit: int) -> int:
    """A unit's requests: `package_show`, its pages, the fresh count."""
    return 1 + max(1, math.ceil(int(rows) / page_limit)) + 1


def plan(expected: Mapping[str, int], views: Mapping[str, Mapping[str, Any]], version: str) -> list[str]:
    """The backlog, in its fixed order (step 3)."""
    never, failed, drifted = [], [], []
    for tozar, rows in expected.items():
        view = views.get(tozar) or {"state": "not_captured"}
        if view["state"] == "not_captured":
            never.append(tozar)
        elif view["state"] == "failed":
            failed.append(tozar)
        elif (view["state"] == "captured" and view.get("captured_rows") != rows
              and view.get("register_version") != version):
            drifted.append(tozar)
    return [tozar for part in (never, failed, drifted) for tozar in sorted(part, key=_utf8)]


def refresh(expected: Mapping[str, int], views: Mapping[str, Mapping[str, Any]], version: str) -> list[str]:
    """The rolling refresh (step 4): captured longest ago, re-requestable."""
    candidates = [t for t in expected if (views.get(t) or {}).get("state") == "captured"
                  and views[t].get("register_version") != version]
    candidates.sort(key=lambda t: (str(views[t].get("updated_at") or ""), _utf8(t)))
    return candidates[:SYNC_REFRESH_TOZARS]


def _own_group(repository: Any, run_id: Any) -> Mapping[str, Any]:
    for group in repository.register_directory_groups(1):
        if str(group.get("run_id")) == str(run_id):
            return group
    raise RegisterCaptureError("CATALOG_REGISTER_UNIT_NOT_THIS_RUN")


def run_sync(repository: Any, lease: Any, *, client: Any, meter: MeteredTransport, archive_writer: Any,
             env: Mapping[str, str] | None = None, cancellation_checker: Callable[[], bool] | None = None,
             event_sink: Callable[[str, Mapping[str, Any]], None] | None = None) -> dict[str, Any]:
    requested_by = _own_group(repository, lease.run_id)["requested_by"]
    directory = repository.latest_register_directory()
    if directory is None:
        raise RegisterCaptureError("CATALOG_REGISTER_NO_DIRECTORY")
    basis = (service.last_sync(repository, before_run=lease.run_id) or {}).get("basis")
    def guard() -> _Budget:  # the directory helpers' own cap: what is left of the run's
        return _Budget(max_requests=meter.remaining, max_seconds=DEFAULT_MAX_SECONDS, clock=time.monotonic)

    stop, changed, reason, work, backlog = "complete", False, "", [], []
    try:
        metadata = client.package_show(src.WLTP_RESOURCE_ID)
        seen = {"upstream_version": metadata.upstream_version,
                "metadata_modified": metadata.resource_metadata_modified,
                "source_total": source_total(client, guard())}
        version = directory["version"]["register_version"]
        expected = {str(u["tozar"]): int(u["expected_rows"]) for u in directory["units"]}
        views = service.unit_views(repository)
        # Drift that only the directory can explain: a tozar captured under the
        # CURRENT version with another count (an older-version capture with
        # another count is backlog, fixed by capturing it again).
        changed = not basis or basis.get("directory_version") != version or any(
            basis.get(key) != seen[key] for key in _BASIS) or any(
            v.get("state") == "captured" and v.get("register_version") == version
            and v.get("captured_rows") != expected.get(t) for t, v in views.items() if t in expected)
        if changed:
            reason = "GOV_SYNC_DIRECTORY_TOO_LARGE"  # if the light directory runs out of budget
            found = light_directory(client, expected, guard())
            repository.record_register_directory(found.resource_id, found.fetched_at.isoformat(), found.rpc_units())
            directory = repository.latest_register_directory()
            version = directory["version"]["register_version"]
            expected = {str(u["tozar"]): int(u["expected_rows"]) for u in directory["units"]}
        basis, reason = {**seen, "directory_version": version}, ""
        backlog = plan(expected, views, version)
        work = backlog or refresh(expected, views, version)
    except GovernmentSourceError as failure:
        if not meter.throttled and failure.reason_code != "GOV_DIRECTORY_REQUEST_BUDGET_EXCEEDED":
            raise
        stop = "throttled" if meter.throttled else "budget"
        reason = "GOV_SYNC_THROTTLED" if meter.throttled else reason
        version = directory["version"]["register_version"]
        expected = {str(u["tozar"]): int(u["expected_rows"]) for u in directory["units"]}
        views = service.unit_views(repository)
        work = backlog = plan(expected, views, version)
    outcomes: dict[str, Any] = {}
    config = register_config.load(env)
    position = 0

    def stop_before(unit: Mapping[str, Any]) -> bool:
        return meter.throttled or estimate(unit["expected_rows"], client.page_limit) + SYNC_RETRY_HEADROOM \
            > meter.remaining

    while stop == "complete" and position < len(work):
        chosen, rows, cost = [], 0, 0
        for tozar in work[position:]:
            if chosen and rows + expected[tozar] > config.group_max_rows:
                break
            if cost + estimate(expected[tozar], client.page_limit) + SYNC_RETRY_HEADROOM > meter.remaining:
                break
            chosen.append(tozar)
            rows, cost = rows + expected[tozar], cost + estimate(expected[tozar], client.page_limit)
        if not chosen:
            stop = "budget"
            break
        try:
            claim = repository.request_register_capture(
                version, chosen, requested_by, group_max_rows=config.group_max_rows,
                capacity_limit_bytes=config.capacity_limit_bytes,
                bytes_per_row=service.claim_bytes_per_row(repository, config),
                grace_seconds=service.START_GRACE_SECONDS)
        except AppError as refused:
            if refused.code != "CATALOG_CAPACITY_THRESHOLD_EXCEEDED":
                raise
            stop = "capacity"
            break
        position += len(chosen)
        if claim["decision"] != "claimed":
            continue
        group_id = str(claim["group"]["id"])
        for state in ("claimed", "triggered"):
            repository.record_register_capture_trigger(group_id, run_id=str(lease.run_id), trigger_state=state,
                                                       execution_name=None)
        report = capture_group(repository, lease, client=client, group_id=group_id, archive_writer=archive_writer,
                               cancellation_checker=cancellation_checker, event_sink=event_sink,
                               stop_before_unit=stop_before)
        outcomes.update((unit.tozar, unit) for unit in report.units)
        if meter.throttled:
            stop, reason = "throttled", "GOV_SYNC_THROTTLED"
        elif meter.remaining <= 0 or any(unit.failure_code == "GOV_DIRECTORY_REQUEST_BUDGET_EXCEEDED"
                                         for unit in report.units):
            stop = "budget"
        elif len(report.units) < len(claim.get("units") or []):
            stop = "budget"
    document = _document(expected, views, version, work, backlog, outcomes, stop=stop, changed=changed,
                         used=meter.budget.used, basis=basis)
    # How the run ends: a failed unit fails it too (its pending snapshot is adopted later).
    document["reason_code"] = reason or ("CATALOG_REGISTER_CAPTURE_FAILED" if document["failure_codes"] else "")
    return document


def _document(expected: Mapping[str, int], views: Mapping[str, Mapping[str, Any]], version: str,
              work: list[str], backlog: list[str], outcomes: Mapping[str, Any], *, stop: str, changed: bool,
              used: int, basis: Any) -> dict[str, Any]:
    done = {t for t, unit in outcomes.items() if unit.status == "captured"}
    reused = {t for t in done if (views.get(t) or {}).get("state") == "captured"
              and outcomes[t].snapshot_key == views[t].get("snapshot_key")}
    failed = len(outcomes) - len(done)
    left = [t for t in backlog if t not in done]

    def covered(tozar: str) -> int:
        if tozar in outcomes:
            return int(outcomes[tozar].captured_rows or 0) if tozar in done else 0
        view = views.get(tozar) or {}
        return int(view.get("captured_rows") or 0) if view.get("state") == "captured" else 0

    fields = {"changed": str(changed).lower(), "directory_version": version[:12], "work": len(work),
              "captured": len(done - reused), "reused": len(reused), "failed": failed,
              "deferred": len(work) - len(outcomes), "requests": f"{used}/{SYNC_MAX_REQUESTS}", "stop": stop,
              "backlog": f"{len(left)}/{sum(expected[t] for t in left)}",
              "coverage": f"{sum(covered(t) for t in expected)}/{sum(expected.values())}"}
    return {"summary": "SYNC_SUMMARY|" + "|".join(f"{key}={value}" for key, value in fields.items()),
            "stop": stop, "basis": basis, "failure_codes": sorted({u.failure_code for u in outcomes.values()
                                                                    if u.failure_code})}


__all__ = ["MeteredTransport", "SYNC_MAX_REQUESTS", "SYNC_REFRESH_TOZARS", "estimate", "plan", "refresh",
           "run_sync"]

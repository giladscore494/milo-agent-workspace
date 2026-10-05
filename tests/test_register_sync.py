"""PR-SYNC-1: the incremental register sync -- the check, the light directory,
the diff, the rolling refresh, the request budget, the throttle stop and the
single flight -- offline, against a fake data.gov.il.

The fake serves the register from a {tozar: rows} map through the real
`DataGovClient` (every response shape the client and the directory check),
so a sync runs end to end: the API's route, the capture job's `--register-sync`
mode, `request_register_capture`, `capture_group` / `capture_unit` and the
in-memory mirrors of the register RPCs. No socket is opened.
"""

from __future__ import annotations

import json
from typing import Any, Mapping
from uuid import UUID

import pytest

from backend import capture_invocation as ci
from backend.catalog import operator_capture as entrypoint
from backend.catalog.government import source as src
from backend.catalog.government.client import DataGovClient
from backend.catalog.government.directory import _Budget, light_directory, register_version
from backend.catalog.government.source import GovernmentSourceError
from backend.catalog.government.transport import HttpResponse
from backend.catalog.register import service as register_service
from backend.catalog.register import sync as register_sync
from backend.errors import AppError
from backend.testing import government_capture as capture_fixtures
from backend.testing.memory_repository import MemoryRepository
from tests.test_catalog_operator_capture import authorized_argv, capture_env
from tests.test_register_capture import (RELEASE_SHA, USER, DirectoryClient, FakeTrigger, FakeWriter,  # noqa: F401
                                         api_env, as_user, client, directory, no_sockets, request, world)

JEEP, MERCEDES, AUDI = "Jeep", "מרצדס בנץ", "אאודי"


class FakeRegister:
    """data.gov.il for one resource: `package_show`, the unfiltered total, the
    tozar scan, the filtered counts and the filtered capture pages, all from
    `counts`. `block` names send numbers (1-based, this source's whole life)
    answered by the firewall's HTML 403 page."""

    def __init__(self, counts: Mapping[str, int], *, modified: str = "2026-09-14T02:41:31.842626") -> None:
        self.counts = dict(counts)
        self.modified = modified
        self.block: set[int] = set()
        self.sends = 0
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.statuses: list[int] = []
        template = capture_fixtures.page_document(0)["result"]
        self._fields, self._row = template["fields"], template["records"][0]

    def rows(self, tozar: str) -> list[dict[str, Any]]:
        base = 1 + sum(len(name) * 7919 for name in tozar) * 1000
        return [dict(self._row, _id=base + n, tozar=tozar, degem_nm=f"D{n % 9}", kinuy_mishari=f"M{n % 5}")
                for n in range(self.counts.get(tozar, 0))]

    def _response(self, body: Any, status: int = 200, media: str = "application/json") -> HttpResponse:
        self.statuses.append(status)
        data = body if isinstance(body, bytes) else capture_fixtures.encode({"success": True, "result": body})
        return HttpResponse(status=status, body=data, content_type=media, final_url=src.action_url(src.PACKAGE_SHOW))

    def get(self, url: str, *, params: Mapping[str, str], connect_timeout: float, read_timeout: float,
            max_bytes: int) -> HttpResponse:
        self.sends += 1
        action = str(url).rsplit("/", 1)[-1]
        self.calls.append((action, dict(params)))
        if self.sends in self.block:
            return self._response(b"<html>Access denied</html>", 403, "text/html")
        if action == src.PACKAGE_SHOW:
            document = json.loads(capture_fixtures.package_body())
            for resource in document["result"]["resources"]:
                if resource["id"] == src.WLTP_RESOURCE_ID:
                    resource["last_modified"] = resource["metadata_modified"] = self.modified
            return self._response(capture_fixtures.encode(document))
        result: dict[str, Any] = {"resource_id": params["resource_id"]}
        filters = json.loads(params["filters"]) if "filters" in params else None
        limit, offset = int(params["limit"]), int(params.get("offset") or 0)
        if params.get("fields") == "tozar":  # the directory scan
            everything = [t for t in sorted(self.counts) for _ in range(self.counts[t])]
            result.update(total=len(everything), total_was_estimated=False, records=[
                {"_id": offset + i + 1, "tozar": t} for i, t in enumerate(everything[offset:offset + limit])])
        elif filters is None:  # the register's exact total
            result.update(total=sum(self.counts.values()), total_was_estimated=False, records=[])
        elif limit == 0:  # one tozar's count
            result.update(total=self.counts.get(filters["tozar"], 0), records=[], filters=filters)
        else:  # one capture page of one tozar
            rows = self.rows(filters["tozar"])
            result.update(total=len(rows), total_was_estimated=False, records=rows[offset:offset + limit],
                          filters=filters, limit=limit, offset=offset, fields=self._fields,
                          records_format="objects")
        return self._response(result)


class Harness:
    """The Register page's API and the capture job, one sync at a time."""

    def __init__(self, monkeypatch: Any, capsys: Any, repo: MemoryRepository, w: Mapping[str, Any],
                 source: FakeRegister) -> None:
        self.monkeypatch, self.capsys, self.repo, self.w, self.source = monkeypatch, capsys, repo, w, source
        self.writer = FakeWriter()
        self.sleeps: list[float] = []
        self.capture_calls = 0
        original = repo.request_register_capture

        def counted(*args: Any, **kwargs: Any) -> Any:
            self.capture_calls += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(repo, "request_register_capture", counted)
        monkeypatch.setattr(entrypoint, "_open_repository", lambda: repo)
        monkeypatch.setattr(entrypoint, "_open_transport", lambda: source)
        monkeypatch.setattr(entrypoint, "_open_archive_writer", lambda env: self.writer)
        monkeypatch.setattr(entrypoint, "DataGovClient",
                            lambda *a, **k: DataGovClient(*a, sleep_fn=self.sleeps.append, **k))

    def sync(self) -> tuple[int, dict[str, str], dict[str, Any]]:
        trigger = FakeTrigger()
        answer = client(self.repo, trigger).post(f"/projects/{self.w['project']}/register/sync",
                                                 headers=as_user(), json={"conversation_id": self.w["conversation"]})
        assert answer.status_code == 202, answer.text
        (invocation,) = trigger.calls
        args = list(invocation.entrypoint_args)
        assert "--register-sync" in args and dict(invocation.env_overrides) == {ci.REGISTER_SWITCH: "true"}
        run_id = args[args.index("--run-id") + 1]
        status = entrypoint.main(authorized_argv(run_id, **{"--register-sync": True}),
                                 env=capture_env(MILO_RELEASE_SHA=RELEASE_SHA, MILO_ENABLE_REGISTER_CAPTURE_JOB="true"))
        # The next sync comes later: this run's lease has lapsed (adoption's
        # own rule for a snapshot a failed run left pending).
        self.repo.runs[run_id]["lease_expires_at"] = "2000-01-01T00:00:00+00:00"
        line, rendered = self.capsys.readouterr().out.split("\n", 1)
        document = json.loads(rendered)
        assert line == document["register"]["sync"]["summary"]
        assert self.repo.runs[run_id]["output"]["sync"]["summary"] == line
        return status, summary(line), document


def summary(line: str) -> dict[str, str]:
    head, *fields = line.split("|")
    assert head == "SYNC_SUMMARY"
    return dict(field.split("=", 1) for field in fields)


def captured(repo: MemoryRepository) -> dict[str, dict[str, Any]]:
    return {t: v for t, v in register_service.unit_views(repo).items() if v["state"] == "captured"}


def small_world(counts: Mapping[str, int]) -> tuple[MemoryRepository, dict[str, Any], FakeRegister]:
    repo, w = world()
    directory(repo, counts)
    return repo, w, FakeRegister(counts)


# =============================================================================
# 1. no change
# =============================================================================

def test_an_unchanged_complete_register_costs_two_requests_and_captures_nothing(monkeypatch, capsys):
    repo, w, source = small_world({"Alfa": 3, "Beta": 2})
    harness = Harness(monkeypatch, capsys, repo, w, source)
    status, first, _ = harness.sync()
    assert status == entrypoint.EXIT_OK and first["stop"] == "complete" and first["captured"] == "2"
    sends, calls, directories = source.sends, harness.capture_calls, len(repo._register_state()["directories"])
    status, second, _ = harness.sync()
    assert status == entrypoint.EXIT_OK
    assert source.sends - sends == 2 and [a for a, _ in source.calls[sends:]] == [src.PACKAGE_SHOW,
                                                                                    src.DATASTORE_SEARCH]
    assert harness.capture_calls == calls and len(repo._register_state()["directories"]) == directories
    assert second == {"changed": "false", "directory_version": first["directory_version"], "work": "0",
                      "captured": "0", "reused": "0", "failed": "0", "deferred": "0", "requests": "2/80",
                      "stop": "complete", "backlog": "0/0", "coverage": "5/5"}


# =============================================================================
# 2. the light directory
# =============================================================================

def test_the_light_directory_counts_only_the_changed_and_the_new_tozars():
    fake = DirectoryClient({"Alfa": 5, "Beta": 4, "Gamma": 2, "Delta": 7})
    found = light_directory(fake, {"Alfa": 5, "Beta": 3, "Delta": 7, "Gone": 1}, _Budget(80, 3000, lambda: 0.0))
    counted = [json.loads(c["filters"])["tozar"] for c in fake.calls if "filters" in c]
    assert counted == ["Beta", "Gamma"] and len(fake.scans) == 1 and found.requests == 3
    assert {u.tozar: u.expected_rows for u in found.units} == {"Alfa": 5, "Beta": 4, "Gamma": 2, "Delta": 7}
    assert found.register_version == register_version(
        src.WLTP_RESOURCE_ID, [{"tozar": t, "expected_rows": n} for t, n in fake.counts.items()])
    with pytest.raises(GovernmentSourceError) as refused:
        light_directory(DirectoryClient({"Alfa": 5, "Beta": 4}, miscount="Beta"), {"Alfa": 5, "Beta": 3},
                        _Budget(80, 3000, lambda: 0.0))
    assert refused.value.reason_code == "GOV_DIRECTORY_RESULT_INVALID"


def test_a_sync_records_the_full_version_and_refuses_a_scan_count_mismatch(monkeypatch, capsys):
    repo, w, source = small_world({"Alfa": 3, "Beta": 2})
    source.counts.update(Beta=3, Gamma=1)
    harness = Harness(monkeypatch, capsys, repo, w, source)
    status, line, _ = harness.sync()
    assert status == entrypoint.EXIT_OK and line["changed"] == "true"
    counted = [json.loads(p["filters"])["tozar"] for a, p in source.calls if p.get("limit") == "0" and "filters" in p]
    assert counted[:2] == ["Beta", "Gamma"]  # the directory's two counts, then the units' own
    latest = repo.latest_register_directory()["version"]["register_version"]
    assert latest == register_version(src.WLTP_RESOURCE_ID, [{"tozar": t, "expected_rows": n}
                                                             for t, n in source.counts.items()])
    assert line["directory_version"] == latest[:12] and line["backlog"] == "0/0"
    # A count that disagrees with the scan: no directory, nothing captured.
    repo, w, source = small_world({"Alfa": 3})
    source.counts["Beta"] = 2
    original = source.get

    def lying(url: str, **kwargs: Any) -> HttpResponse:
        if kwargs["params"].get("limit") == "0" and "Beta" in kwargs["params"].get("filters", ""):
            source.counts["Beta"] = 9
            try:
                return original(url, **kwargs)
            finally:
                source.counts["Beta"] = 2
        return original(url, **kwargs)

    source.get = lying
    harness = Harness(monkeypatch, capsys, repo, w, source)
    trigger = FakeTrigger()
    client(repo, trigger).post(f"/projects/{w['project']}/register/sync", headers=as_user(),
                               json={"conversation_id": w["conversation"]})
    args = list(trigger.calls[0].entrypoint_args)
    status = entrypoint.main(authorized_argv(args[args.index("--run-id") + 1], **{"--register-sync": True}),
                             env=capture_env(MILO_RELEASE_SHA=RELEASE_SHA, MILO_ENABLE_REGISTER_CAPTURE_JOB="true"))
    document = json.loads(capsys.readouterr().out)
    assert status == entrypoint.EXIT_FAILED and document["reason_code"] == "GOV_DIRECTORY_RESULT_INVALID"
    assert harness.capture_calls == 0 and len(repo._register_state()["directories"]) == 1
    assert repo.catalog_snapshots == {} or not captured(repo)


# =============================================================================
# 3. the diff's order
# =============================================================================

def test_the_work_list_is_never_failed_drift_each_in_byte_order_and_stable():
    version, old = "b" * 64, "a" * 64
    expected = {"Zulu": 4, "אאודי": 5279, "Alfa": 3, "Echo": 9, "Kilo": 8, "Mike": 7, "Oscar": 6, "Bravo": 2}
    views = {
        "Echo": {"state": "failed", "failure_code": "CATALOG_REGISTER_CAPTURE_INTERRUPTED"},
        "Bravo": {"state": "failed", "failure_code": "GOV_HTTP_STATUS_UNEXPECTED"},
        "אאודי": {"state": "captured", "captured_rows": 5278, "register_version": old},  # drift, old version
        "Kilo": {"state": "captured", "captured_rows": 7, "register_version": old},  # drift
        "Mike": {"state": "captured", "captured_rows": 7, "register_version": old},  # old version, same count
        "Oscar": {"state": "captured", "captured_rows": 5, "register_version": version},  # not re-requestable
    }
    order = ["Alfa", "Zulu", "Bravo", "Echo", "Kilo", "אאודי"]
    assert register_sync.plan(expected, views, version) == order
    reversed_input = dict(reversed(list(expected.items())))
    assert register_sync.plan(reversed_input, dict(reversed(list(views.items()))), version) == order
    # The refresh: only once nothing is left, captured longest ago first.
    views["Mike"]["updated_at"], views["Kilo"]["updated_at"] = "2026-09-01", "2026-09-02"
    views["אאודי"]["updated_at"] = "2026-09-03"
    assert register_sync.refresh(expected, views, version) == ["Mike", "Kilo"]


# =============================================================================
# 4. the rolling refresh reuses an unchanged snapshot
# =============================================================================

def test_a_rolling_refresh_of_an_unchanged_tozar_reuses_its_snapshot(monkeypatch, capsys):
    repo, w, source = small_world({"Alfa": 3})
    harness = Harness(monkeypatch, capsys, repo, w, source)
    harness.sync()
    before = captured(repo)["Alfa"]
    # The register grows (its total moves) while the resource's published
    # version stays: Alfa's content and version are unchanged. (A snapshot key
    # carries `upstream_version`: after a republish the refresh writes anew.)
    source.counts["Beta"] = 2
    status, grown, _ = harness.sync()  # a change: the light directory, then the backlog (Beta) only
    assert status == entrypoint.EXIT_OK and (grown["changed"], grown["work"], grown["captured"]) == ("true", "1", "1")
    snapshots, rows = len(repo.catalog_snapshots), len(repo.catalog_raw_records)
    status, line, _ = harness.sync()
    assert status == entrypoint.EXIT_OK
    # Beta is captured under the current version: only Alfa is refreshed.
    assert (line["work"], line["reused"], line["captured"], line["backlog"]) == ("1", "1", "0", "0/0")
    after = captured(repo)["Alfa"]
    assert after["snapshot_key"] == before["snapshot_key"] and after["register_version"] != before["register_version"]
    assert (len(repo.catalog_snapshots), len(repo.catalog_raw_records)) == (snapshots, rows)


# =============================================================================
# 5. the request budget
# =============================================================================

def test_work_past_the_budget_is_deferred_and_taken_first_by_the_next_run(monkeypatch, capsys):
    counts = {f"T{i:02d}": 5 for i in range(30)}
    repo, w, source = small_world(counts)
    harness = Harness(monkeypatch, capsys, repo, w, source)
    status, first, _ = harness.sync()
    assert status == entrypoint.EXIT_OK
    assert source.sends <= 80 and first["requests"] == f"{source.sends}/80"
    assert (first["work"], first["captured"], first["deferred"], first["stop"]) == ("30", "25", "5", "budget")
    assert first["backlog"] == "5/25"
    assert sorted(captured(repo)) == sorted(counts)[:25]
    status, second, _ = harness.sync()
    assert (second["work"], second["captured"], second["deferred"], second["stop"]) == ("5", "5", "0", "complete")
    assert sorted(captured(repo)) == sorted(counts) and second["requests"] == "17/80"


# =============================================================================
# 6. the firewall's first answer ends the sync
# =============================================================================

def test_a_waf_403_on_the_third_unit_ends_the_sync_at_once(monkeypatch, capsys):
    counts = {f"U{i}": 4 for i in range(5)}
    repo, w, source = small_world(counts)
    source.block = {10}  # check 2, one scan page, then 3 sends per unit: the 3rd unit's first send
    harness = Harness(monkeypatch, capsys, repo, w, source)
    status, line, document = harness.sync()
    assert status == entrypoint.EXIT_FAILED and document["reason_code"] == "GOV_SYNC_THROTTLED"
    assert source.sends == 10 and source.statuses.count(403) == 1
    assert all(wait <= 1.0 for wait in harness.sleeps)  # the pace interval only: no 60/180/300 s backoff
    assert (line["stop"], line["captured"], line["failed"], line["deferred"]) == ("throttled", "2", "1", "2")
    views = register_service.unit_views(repo)
    assert [views[f"U{i}"]["state"] for i in range(5)] == ["captured", "captured", "failed", "failed", "failed"]
    assert views["U2"]["failure_code"] == "GOV_HTTP_STATUS_UNEXPECTED"
    assert {views[f"U{i}"]["failure_code"] for i in (3, 4)} == {"CATALOG_REGISTER_CAPTURE_INTERRUPTED"}
    active = [r for r in repo.catalog_snapshots.values() if r.get("activated_at")]
    assert {r["snapshot_key"] for r in active} == {views["U0"]["snapshot_key"], views["U1"]["snapshot_key"]}
    # The page shows the last sync; the next run takes the failed ones and finishes.
    page = client(repo, FakeTrigger()).get(f"/projects/{w['project']}/register", headers=as_user()).json()
    assert page["last_sync"]["summary"].endswith("|stop=throttled|backlog=3/12|coverage=8/20")
    status, again, _ = harness.sync()
    assert status == entrypoint.EXIT_OK and (again["captured"], again["backlog"]) == ("3", "0/0")


# =============================================================================
# 7. the capacity guard
# =============================================================================

def test_a_capacity_refusal_stops_the_sync_with_nothing_partial(monkeypatch, capsys):
    repo, w, source = small_world({"Alfa": 3, "Beta": 2})
    repo.register_database_bytes = 10 ** 12
    harness = Harness(monkeypatch, capsys, repo, w, source)
    status, line, _ = harness.sync()
    assert status == entrypoint.EXIT_OK and harness.capture_calls == 1
    assert (line["stop"], line["captured"], line["deferred"], line["backlog"]) == ("capacity", "0", "2", "2/5")
    assert repo.register_capture_units() == [] and repo.catalog_snapshots == {}


# =============================================================================
# 8. single flight
# =============================================================================

def test_one_register_job_at_a_time():
    repo, w = world()
    version = directory(repo)
    body = {"conversation_id": w["conversation"]}
    request(repo, w, version, ["טויוטה"])  # a live group capture
    trigger = FakeTrigger()
    api = client(repo, trigger)
    refused = api.post(f"/projects/{w['project']}/register/sync", headers=as_user(), json=body)
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "CATALOG_REGISTER_BUSY"
    assert trigger.calls == []
    repo, w = world()
    version = directory(repo)
    api = client(repo, trigger)
    assert api.post(f"/projects/{w['project']}/register/sync", headers=as_user(), json=body | {
        "conversation_id": w["conversation"]}).status_code == 202
    for path, extra in (("sync", {}), ("captures", {"register_version": version, "tozars": ["טויוטה"]})):
        again = api.post(f"/projects/{w['project']}/register/{path}", headers=as_user(),
                         json={"conversation_id": w["conversation"], **extra})
        assert again.status_code == 409 and again.json()["error"]["code"] == "CATALOG_REGISTER_BUSY", path
    assert len(trigger.calls) == 1 and repo.register_capture_units() == []


def test_the_sync_mode_is_one_register_mode_among_three(capsys):
    for extra in ({"--register-directory": True}, {"--register-group-id": str(UUID(int=7))}):
        status = entrypoint.main(authorized_argv(UUID(int=1), **{"--register-sync": True, **extra}),
                                 env=capture_env(MILO_ENABLE_REGISTER_CAPTURE_JOB="true"))
        assert status == entrypoint.EXIT_REFUSED
        assert json.loads(capsys.readouterr().out)["reason_code"] == "CAPTURE_REGISTER_ARGUMENTS_INVALID"


# =============================================================================
# 9-10. the production replay (1.10): 93 missing tozars, Jeep / Mercedes +1
# =============================================================================

def production(monkeypatch, capsys) -> tuple[Harness, dict[str, int]]:
    """137 tozars, 101,691 directory rows; 44 captured (97,953 rows, Jeep and
    Mercedes one row past the directory); 93 small ones (3,740 rows) missing."""
    missing = {f"יצרן {i:03d}": 40 for i in range(93)}
    missing["יצרן 050"] = 60
    big = {JEEP: 1421, MERCEDES: 433, AUDI: 5279}
    others = {f"Make {i:02d}": 100 for i in range(41)}
    others["Make 00"] = 101_691 - 3_740 - sum(big.values()) - 100 * 40
    recorded = {**missing, **big, **others}
    assert (len(recorded), sum(recorded.values()), sum(missing.values())) == (137, 101_691, 3_740)
    repo, w = world()
    old = directory(repo, recorded)
    state = repo._register_state()
    for n, (tozar, rows) in enumerate({**others, **big}.items()):
        rows += 1 if tozar in (JEEP, MERCEDES) else 0
        state["units"][(old, tozar)] = {
            "id": str(UUID(int=n + 1)), "group_id": str(UUID(int=999)), "register_version": old, "tozar": tozar,
            "expected_rows": rows, "attempt": 1, "status": "captured", "failure_code": None,
            "snapshot_id": str(UUID(int=n + 1000)), "snapshot_key": f"gov-seed-{n}", "api_total": rows,
            "captured_rows": rows, "count_verified": True, "measured_bytes": rows * 2900,
            "measurement_method": "seed", "created_at": f"2026-09-{1 + n % 28:02d}T00:00:00+00:00",
            "updated_at": f"2026-09-{2 + (n + 5) % 26:02d}T00:00:00+00:00"}
    source = FakeRegister({**recorded, JEEP: 1422, MERCEDES: 434})
    return Harness(monkeypatch, capsys, repo, w, source), missing


def test_the_production_replay_s_first_run_plan(monkeypatch, capsys):
    harness, missing = production(monkeypatch, capsys)
    status, line, document = harness.sync()
    assert status == entrypoint.EXIT_OK
    version = harness.repo.latest_register_directory()["version"]["register_version"]
    first = sorted(missing, key=str.encode)[:21]
    left = sum(missing.values()) - sum(missing[t] for t in first)
    expected_line = ("SYNC_SUMMARY|changed=true|directory_version=" + version[:12] + "|work=93|captured=21|"
                     "reused=0|failed=0|deferred=72|requests=78/80|stop=budget|"
                     f"backlog=72/{left}|coverage={97_953 + 21 * 40}/101693")
    assert document["register"]["sync"]["summary"] == expected_line
    # 2 checks + 11 scan pages + exactly two counts (Jeep, Mercedes), then 3 per small tozar.
    counts = [json.loads(p["filters"])["tozar"] for a, p in harness.source.calls[:15] if "filters" in p]
    assert counts == sorted([JEEP, MERCEDES], key=str.encode) and line["requests"] == "78/80"
    assert sorted(t for t in captured(harness.repo) if t in missing) == sorted(first)
    print(expected_line)


def test_the_production_replay_converges_with_throttled_runs(monkeypatch, capsys):
    harness, missing = production(monkeypatch, capsys)
    seen: dict[str, int] = {}
    lines: list[dict[str, str]] = []
    for run in range(1, 12):
        backlog_phase = not lines or lines[-1]["backlog"] != "0/0"
        before = set(captured(harness.repo))
        if run in (2, 4):
            harness.source.block = {harness.source.sends + 20}
        status, line, _ = harness.sync()
        lines.append(line)
        if not backlog_phase:
            break
        new = [t for t in captured(harness.repo) if t in missing and t not in before]
        assert new or line["stop"] == "throttled", (run, line)
        assert int(line["work"]) == int(line["backlog"].split("/")[0]) + int(line["captured"]), (run, line)
        for tozar in new:
            assert tozar not in seen, tozar
            seen[tozar] = run
    stops = [line["stop"] for line in lines]
    assert stops[1] == stops[3] == "throttled" and set(seen) == set(missing)
    done = next(i for i, line in enumerate(lines) if line["backlog"] == "0/0")
    assert lines[done]["coverage"] == "101693/101693"
    # The refresh starts on the first run after the backlog is empty, never before.
    assert all(int(line["work"]) == int(line["deferred"]) + int(line["captured"]) + int(line["failed"])
               + int(line["reused"]) for line in lines)
    assert [line["work"] for line in lines[:done + 1]] == [str(93 - len(
        [t for t in seen if seen[t] < run])) for run in range(1, done + 2)]
    refresh = lines[done + 1]
    assert len(lines) == done + 2 and (refresh["work"], refresh["backlog"], refresh["stop"]) == ("2", "0/0", "complete")
    print(f"converged: backlog empty after run {done + 1} of which 2 throttled; refresh from run {done + 2}")
    assert done + 1 == 6


# =============================================================================
# review findings: no silent directory loop, no rescan for backlog drift,
# single flight re-checked after each claim
# =============================================================================

def test_more_moved_tozars_than_the_budget_can_count_fails_loudly_before_counting(monkeypatch, capsys):
    counts = {f"T{i:02d}": 5 for i in range(90)}
    repo, w, source = small_world(counts)
    source.counts = {t: 6 for t in counts}
    harness = Harness(monkeypatch, capsys, repo, w, source)
    status, line, document = harness.sync()
    assert status == entrypoint.EXIT_FAILED and document["reason_code"] == "GOV_SYNC_DIRECTORY_TOO_LARGE"
    assert (line["requests"], line["stop"], line["captured"]) == ("3/80", "budget", "0")  # no count spent
    assert len(repo._register_state()["directories"]) == 1
    # One full Refresh directory, and the next sync proceeds.
    repo.record_register_directory(src.WLTP_RESOURCE_ID, "2026-10-05T00:00:00+00:00",
                                   [{"tozar": t, "expected_rows": n} for t, n in source.counts.items()])
    status, line, _ = harness.sync()
    assert status == entrypoint.EXIT_OK and line["changed"] == "true" and line["captured"] == "25"


def test_drift_under_an_older_version_is_backlog_and_reads_no_directory(monkeypatch, capsys):
    repo, w, source = small_world({"Alfa": 3, "Beta": 2})
    harness = Harness(monkeypatch, capsys, repo, w, source)
    harness.sync()
    source.counts.update(Beta=3, Gamma=1)
    source.block = {source.sends + 2 + 1 + 2 + 1}  # after the check, the scan and two counts: the 1st unit
    status, line, _ = harness.sync()
    assert line["stop"] == "throttled" and line["changed"] == "true"
    sends = source.sends
    status, line, _ = harness.sync()
    assert status == entrypoint.EXIT_OK and line["changed"] == "false"
    assert not [p for _a, p in source.calls[sends:] if p.get("fields") == "tozar"]
    assert (line["work"], line["captured"], line["backlog"]) == ("2", "2", "0/0")


def test_single_flight_is_checked_again_after_each_claim(monkeypatch):
    repo, w = world()
    version = directory(repo)
    request(repo, w, version, ["טויוטה"])  # a live group capture...
    monkeypatch.setattr(register_service, "register_busy", lambda _repo: False)  # ...the sync's check missed
    trigger = FakeTrigger()
    refused = client(repo, trigger).post(f"/projects/{w['project']}/register/sync", headers=as_user(),
                                         json={"conversation_id": w["conversation"]})
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "CATALOG_REGISTER_BUSY"
    assert trigger.calls == [] and repo.register_directory_groups(1)[0]["trigger_state"] == "trigger_failed"
    # A capture whose first look missed a live sync.
    repo, w = world()
    version = directory(repo)
    assert client(repo, trigger).post(f"/projects/{w['project']}/register/sync", headers=as_user(),
                                      json={"conversation_id": w["conversation"]}).status_code == 202
    real, looks = repo.register_directory_groups, []

    def first_look_misses(limit: int) -> list[dict[str, Any]]:
        looks.append(limit)
        return [] if len(looks) == 1 else real(limit)

    monkeypatch.setattr(repo, "register_directory_groups", first_look_misses)
    with pytest.raises(AppError) as refusal:
        request(repo, w, version, ["טויוטה"], trigger)
    assert refusal.value.code == "CATALOG_REGISTER_BUSY" and len(trigger.calls) == 1
    assert {u["status"] for u in repo.register_capture_units()} == {"failed"}

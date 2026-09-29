"""PR-D1: register capture -- the directory, the capture job, the archive, the
capacity guard, the API and retention -- offline, over the committed R5 rows.

No socket is opened. Every Government byte is the committed R5 capture read
through the R5 manifest gate, re-shaped only where a test says so (a scoped
page's query echo, a directory read). The database functions themselves are
exercised on ephemeral PostgreSQL in tests/test_register_migration_postgres.py;
here the in-memory mirrors (backend/testing/register_memory.py) stand in.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import socket
from typing import Any, Mapping
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from backend import capture_invocation as ci
from backend.catalog import operator_capture as entrypoint
from backend.catalog.government import source as src
from backend.catalog.government.capture_scope import CaptureScope
from backend.catalog.government.client import DataGovClient
from backend.catalog.government.directory import (DirectoryUnit, discover_directory,
                                                  register_version)
from backend.catalog.government.ingest import GovernmentCatalogIngestor
from backend.catalog.government.source import GovernmentSourceError
from backend.catalog.register import archive as arc
from backend.catalog.register import config as register_config
from backend.catalog.register import coverage as coverage_module
from backend.catalog.register import prune as prune_module
from backend.catalog.register import retention
from backend.catalog.register import service as register_service
from backend.catalog.register.capture import capture_group
from backend.catalog.scope import prepare_trigger as trig
from backend.dependencies import get_capture_trigger, get_repository
from backend.errors import AppError
from backend.main import app
from backend.testing import government_capture as capture_fixtures
from backend.testing.government_capture import FixtureTransport
from backend.testing.memory_repository import MemoryRepository
from tests.test_catalog_operator_capture import (PROJECT_REF, SUPABASE_URL, authorized_argv, capture_env,
                                                 prepare_argv)
from tests.test_work_scope_preparation import TOYOTA, planned_records, scoped_page

USER = UUID("11111111-2222-4333-8444-555555555555")
OUTSIDER = UUID("99999999-2222-4333-8444-555555555555")
RELEASE_SHA = "84cd8696119c24662a954d0f0e23195268dab23f"
LEXUS = "לקסוס"
BUCKET = "milo-test-register-archive"


@pytest.fixture(autouse=True)
def no_sockets(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("an offline register test attempted a network connection")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(autouse=True)
def api_env(monkeypatch):
    """The API's environment with ONLY register capture on: no run creation,
    no run-start gateway flag, no Arm, no paid execution."""
    monkeypatch.setenv(register_service.REGISTER_FLAG, "true")
    monkeypatch.setenv("MILO_RELEASE_SHA", RELEASE_SHA)
    monkeypatch.setenv("MILO_EXPECTED_SUPABASE_PROJECT_REF", PROJECT_REF)
    monkeypatch.setenv("SUPABASE_URL", SUPABASE_URL)
    monkeypatch.setenv("MILO_RATE_LIMIT_RUN_CREATION_USER", "1000")
    monkeypatch.setenv("MILO_RATE_LIMIT_REGISTER_ACTIONS_USER", "1000")
    for flag in ("MILO_ENABLE_RUN_CREATION", "GATEWAY_ALLOW_RUN_START_ROUTES", "MILO_ENABLE_PAID_EXECUTION",
                 "MILO_ENABLE_WORK_SCOPE_BATCHES", "MILO_ENABLE_EXECUTION_CONTROL"):
        monkeypatch.delenv(flag, raising=False)
    yield
    app.dependency_overrides.clear()


# =============================================================================
# helpers
# =============================================================================

class FakeTrigger:
    def __init__(self, *, refusal: str | None = None, state: str = trig.TRIGGERED) -> None:
        self.refusal = refusal
        self.state = state
        self.calls: list[ci.Invocation] = []

    def release_refusal(self) -> str | None:
        return self.refusal

    def run(self, invocation: ci.Invocation) -> trig.TriggerOutcome:
        self.calls.append(invocation)
        return trig.TriggerOutcome(self.state, "milo-catalog-capture-x7k2p")


class FakeWriter:
    """An archive bucket: create-only, remembers every object it holds."""

    def __init__(self, *, fail: bool = False, outcome: str | None = None) -> None:
        self.bucket = BUCKET
        self.objects: dict[str, bytes] = {}
        self.fail = fail
        self.outcome = outcome

    def put(self, name: str, archive: arc.ArchiveObject) -> str:
        if self.fail:
            raise arc.ArchiveWriteError("upload failed")
        if self.outcome is not None:
            return self.outcome
        if name in self.objects:
            return arc.EXISTS_VERIFIED if self.objects[name] == archive.data else arc.EXISTS_UNVERIFIED
        self.objects[name] = archive.data
        return arc.CREATED


def world(repo: MemoryRepository | None = None) -> tuple[MemoryRepository, dict[str, Any]]:
    repo = repo or MemoryRepository()
    for user in (USER, OUTSIDER):
        repo.seed_user(str(user))
    project = str(uuid4())
    repo.seed_project(project, f"p-{project[:8]}", "P", [str(USER)], workflow_key="swarm_v2")
    conversation = repo.create_conversation(UUID(project), "register", USER)["id"]
    return repo, {"project": project, "conversation": conversation}


def directory(repo: MemoryRepository, units: Mapping[str, int] | None = None) -> str:
    units = units if units is not None else {TOYOTA: 28, LEXUS: 5000}
    answer = repo.record_register_directory(
        src.WLTP_RESOURCE_ID, "2026-09-29T10:00:00+00:00",
        [{"tozar": tozar, "expected_rows": rows} for tozar, rows in units.items()])
    return answer["version"]["register_version"]


def request(repo: MemoryRepository, w: Mapping[str, Any], version: str, tozars: list[str],
            trigger: FakeTrigger | None = None, env: Mapping[str, str] | None = None):
    import os

    return register_service.request_capture(
        repo, USER, UUID(w["project"]), register_version=version, tozars=tozars,
        conversation_id=UUID(w["conversation"]), trigger=trigger or FakeTrigger(),
        env=dict(os.environ, **(env or {})))


def count_body(total: int, marque: str = TOYOTA) -> bytes:
    """The source's answer to a fresh `limit=0` count of that exact tozar."""
    document = json.loads(scoped_page([], marque=marque))
    document["result"].update(total=total, limit=0, records=[])
    return capture_fixtures.encode(document)


def scoped_client(records: list[Mapping[str, Any]], marque: str = TOYOTA, *,
                  fresh_total: int | None = None, transport: FixtureTransport | None = None) -> DataGovClient:
    bodies: dict[Any, bytes] = {0: scoped_page(records, marque=marque)}
    if fresh_total is not None:
        bodies["count"] = count_body(fresh_total, marque)
    return DataGovClient(transport or FixtureTransport(bodies=bodies),
                         page_limit=entrypoint.CAPTURE_PAGE_LIMIT, sleep_fn=lambda _s: None)


def claimed_lease(repo: MemoryRepository, run_id: str):
    from backend.engines.swarm_v2.evidence import WorkerLease

    claimed = repo.claim_run(run_id, "capture-worker")
    return WorkerLease(claimed["id"], "capture-worker", int(claimed["attempt"]), claimed["lease_token"])


def captured_world(records=None, *, writer: FakeWriter | None = None, client: DataGovClient | None = None):
    """The API claims ONE tozar; the capture job captures it."""
    repo, w = world()
    version = directory(repo)
    answer, started = request(repo, w, version, [TOYOTA])
    assert started and answer["decision"] == "claimed"
    group = repo.register_capture_group(answer["group_id"])["group"]
    lease = claimed_lease(repo, group["run_id"])
    writer = writer if writer is not None else FakeWriter()
    report = capture_group(repo, lease, client=client or scoped_client(records or planned_records()),
                           group_id=answer["group_id"], archive_writer=writer)
    return repo, w, version, report, writer


def the_unit(repo: MemoryRepository, tozar: str = TOYOTA) -> dict[str, Any]:
    (unit,) = [u for u in repo.register_capture_units() if u["tozar"] == tozar]
    return unit


def snapshot_by_key(repo: MemoryRepository, key: str) -> dict[str, Any]:
    (row,) = [r for r in repo.catalog_snapshots.values() if r["snapshot_key"] == key]
    return row


# =============================================================================
# 1. the directory (D1-2)
# =============================================================================

class DirectoryClient:
    """The client seam the directory uses (`_request`), over a fake register."""

    def __init__(self, counts: Mapping[str | None, int], *, extra_fields: bool = False) -> None:
        self.counts = dict(counts)
        self.extra_fields = extra_fields
        self.calls: list[dict[str, str]] = []

    def _request(self, action: str, params: Mapping[str, str]):
        assert action == src.DATASTORE_SEARCH
        self.calls.append(dict(params))
        result: dict[str, Any] = {"resource_id": params["resource_id"]}
        if params.get("distinct") == "true":
            values = sorted(self.counts, key=lambda v: ("" if v is None else v).encode("utf-8"))
            offset, limit = int(params["offset"]), int(params["limit"])
            page = values[offset:offset + limit]
            result["records"] = [{"tozar": v, **({"degem": "x"} if self.extra_fields else {})} for v in page]
        else:
            tozar = json.loads(params["filters"])["tozar"]
            result.update(total=self.counts[tozar], records=[], filters=params["filters"])
        return {"success": True, "result": result}, None, None


def test_the_directory_is_metadata_only_bounded_and_exact():
    # A padded value and one carrying a right-to-left mark are values no scoped
    # capture can filter on (CaptureScope refuses them): counted, never units.
    fake = DirectoryClient({TOYOTA: 28, LEXUS: 5000, "טויוטה ": 3, "מאזדה\u200f": 2, None: 7})
    found = discover_directory(fake, clock=lambda: 0.0)
    assert [(u.tozar, u.expected_rows) for u in found.units] == [(TOYOTA, 28), (LEXUS, 5000)]
    assert found.unfilterable_values == 3 and found.total_rows == 5028
    # One distinct read, then one count per tozar -- limit=0, never a row.
    distinct, *counts = fake.calls
    assert distinct["fields"] == "tozar" and distinct["distinct"] == "true"
    assert all(call["limit"] == "0" and "fields" not in call for call in counts)
    assert found.requests == 3


def test_the_directory_refuses_past_its_request_or_time_cap():
    fake = DirectoryClient({TOYOTA: 28, LEXUS: 5000})
    with pytest.raises(GovernmentSourceError) as refused:
        discover_directory(fake, max_requests=2, clock=lambda: 0.0)
    assert refused.value.reason_code == "GOV_DIRECTORY_REQUEST_BUDGET_EXCEEDED"
    ticks = iter([0.0, 0.0, 1000.0, 1000.0])
    with pytest.raises(GovernmentSourceError) as late:
        discover_directory(DirectoryClient({TOYOTA: 28, LEXUS: 5000}), max_seconds=10, clock=lambda: next(ticks))
    assert late.value.reason_code == "GOV_DIRECTORY_TIME_BUDGET_EXCEEDED"


def test_a_distinct_read_carrying_row_payload_is_refused():
    with pytest.raises(GovernmentSourceError) as refused:
        discover_directory(DirectoryClient({TOYOTA: 1}, extra_fields=True), clock=lambda: 0.0)
    assert refused.value.reason_code == "GOV_DIRECTORY_RESULT_INVALID"


def test_the_version_is_stable_and_changes_only_with_the_content():
    units = [DirectoryUnit(TOYOTA, 28), DirectoryUnit(LEXUS, 5000)]
    version = register_version(src.WLTP_RESOURCE_ID, units)
    assert version == register_version(src.WLTP_RESOURCE_ID, list(reversed(units)))
    assert version != register_version(src.WLTP_RESOURCE_ID, [DirectoryUnit(TOYOTA, 29), units[1]])
    assert version != register_version(src.WLTP_RESOURCE_ID, [DirectoryUnit(TOYOTA + " ", 28), units[1]])
    # The canonical text, byte for byte (the database recomputes the same).
    text = (f"gov.register.directory.1\n{src.WLTP_RESOURCE_ID}\n"
            + "".join(f"{json.dumps(t, ensure_ascii=False)}:{n}\n" for t, n in sorted(
                [(TOYOTA, 28), (LEXUS, 5000)], key=lambda p: p[0].encode("utf-8"))))
    assert version == hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_a_new_directory_version_only_on_change():
    repo = MemoryRepository()
    first = repo.record_register_directory(src.WLTP_RESOURCE_ID, "t1", [{"tozar": TOYOTA, "expected_rows": 28}])
    again = repo.record_register_directory(src.WLTP_RESOURCE_ID, "t2", [{"tozar": TOYOTA, "expected_rows": 28}])
    changed = repo.record_register_directory(src.WLTP_RESOURCE_ID, "t3", [{"tozar": TOYOTA, "expected_rows": 29}])
    assert (first["decision"], again["decision"], changed["decision"]) == ("created", "unchanged", "created")
    assert first["version"]["register_version"] == again["version"]["register_version"]


# =============================================================================
# 2. the archive (D1-8)
# =============================================================================

def test_the_archive_object_is_deterministic_canonical_and_hashable():
    records = planned_records()[:5]
    built = arc.build(records)
    assert built.data == arc.build(records).data  # gzip mtime pinned
    assert built.sha256 == hashlib.sha256(built.data).hexdigest() and built.byte_size == len(built.data)
    lines = gzip.decompress(built.data).decode("utf-8").splitlines()
    assert lines == [json.dumps(r, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for r in records]
    assert arc.read_lines(built.data) == records
    name = arc.object_name(src.WLTP_RESOURCE_ID, TOYOTA, "cs1.abc")
    assert name == (f"register/{src.WLTP_RESOURCE_ID}/"
                    f"{hashlib.sha256(TOYOTA.encode('utf-8')).hexdigest()[:16]}/cs1.abc.jsonl.gz")


class FakeResponse:
    def __init__(self, status: int, body: Any = None) -> None:
        self.status_code = status
        self._body = body

    def json(self) -> Any:
        return self._body


class FakeSession:
    def __init__(self, post_status: int, get: FakeResponse | None = None, size: int | None = None) -> None:
        self.post_status, self.get_answer, self.size = post_status, get, size
        self.posts: list[dict[str, Any]] = []

    def post(self, url, params, data, timeout, headers):
        self.posts.append({"url": url, "params": dict(params), "headers": headers})
        return FakeResponse(self.post_status, {"size": str(self.size)})

    def get(self, url, params, timeout):
        return self.get_answer


def test_the_upload_is_create_only_and_a_precondition_conflict_is_verified_never_overwritten():
    built = arc.build(planned_records()[:3])
    session = FakeSession(200, size=built.byte_size)
    assert arc.GcsArchiveWriter(BUCKET, session_factory=lambda: session).put("n", built) == arc.CREATED
    assert session.posts[0]["params"] == {"uploadType": "multipart", "ifGenerationMatch": "0"}
    same = FakeResponse(200, {"size": str(built.byte_size), "metadata": {"sha256": built.sha256}})
    assert arc.GcsArchiveWriter(BUCKET, session_factory=lambda: FakeSession(412, same)).put("n", built) \
        == arc.EXISTS_VERIFIED
    other = FakeResponse(200, {"size": str(built.byte_size), "metadata": {"sha256": "0" * 64}})
    with pytest.raises(arc.ArchiveWriteError):
        arc.GcsArchiveWriter(BUCKET, session_factory=lambda: FakeSession(412, other)).put("n", built)
    # objectCreator cannot read the object: unverified, never success.
    assert arc.GcsArchiveWriter(BUCKET, session_factory=lambda: FakeSession(412, FakeResponse(403))).put(
        "n", built) == arc.EXISTS_UNVERIFIED
    with pytest.raises(arc.ArchiveWriteError):
        arc.GcsArchiveWriter(BUCKET, session_factory=lambda: FakeSession(403)).put("n", built)


# =============================================================================
# 3. the capture job (D1-3/4/8)
# =============================================================================

def test_one_tozar_is_captured_verified_archived_measured_and_activated():
    records = planned_records()
    repo, _w, _version, report, writer = captured_world(records)
    (outcome,) = report.units
    assert (outcome.status, outcome.api_total, outcome.captured_rows) == ("captured", 28, 28)
    unit = the_unit(repo)
    assert unit["status"] == "captured" and unit["count_verified"] is True
    assert unit["measured_bytes"] > 0 and unit["measurement_method"]
    snapshot = snapshot_by_key(repo, outcome.snapshot_key)
    assert snapshot["activated_at"] is not None
    archive = repo.register_snapshot_archive(snapshot["id"])
    (name, data), = writer.objects.items()
    assert archive["gcs_uri"] == f"gs://{BUCKET}/{name}"
    assert archive["sha256"] == hashlib.sha256(data).hexdigest() and archive["byte_size"] == len(data)
    assert archive["line_count"] == 28 and archive["line_basis"] == "source_locator.capture_index+1"
    # Every stored raw record is line capture_index+1 of the object, verbatim.
    lines = arc.read_lines(data)
    stored = [row for (sid, _k), row in repo.catalog_raw_records.items() if str(sid) == str(snapshot["id"])]
    assert len(stored) == 28
    for row in stored:
        assert lines[int(row["source_locator"]["capture_index"])] == row["payload"]


def test_the_register_snapshot_is_the_snapshot_prepare_makes():
    """Prepare and the coverage ledger are untouched: the same Toyota rows make
    the SAME snapshot (key and content hash) with or without register capture."""
    records = planned_records()
    repo, _w, _version, report, _writer = captured_world(records)
    register_snapshot = snapshot_by_key(repo, report.units[0].snapshot_key)

    plain = MemoryRepository()
    _p, pw = world(plain)
    code, doc = entrypoint.prepare_capture_run(plain, conversation_id=UUID(pw["conversation"]),
                                               requested_by=USER, idempotency_key="k",
                                               env=capture_env(MILO_RELEASE_SHA=RELEASE_SHA))
    assert code == entrypoint.EXIT_OK, doc
    lease = claimed_lease(plain, doc["preparation"]["run_id"])
    prepared = GovernmentCatalogIngestor(plain, lease, client=scoped_client(records)).ingest_resource(
        src.WLTP_RESOURCE_ID, capture_scope=CaptureScope.for_register_marque(TOYOTA))
    prepare_snapshot = snapshot_by_key(plain, prepared.snapshot_key)
    assert prepared.snapshot_key == register_snapshot["snapshot_key"]
    for field in ("content_sha256", "declared_record_count", "stored_record_count"):
        assert prepare_snapshot.get(field) == register_snapshot.get(field), field
    timing = {"started_at", "completed_at"}
    assert ({k: v for k, v in prepare_snapshot["retrieval_metadata"].items() if k not in timing}
            == {k: v for k, v in register_snapshot["retrieval_metadata"].items() if k not in timing})

    def content(r: MemoryRepository, snapshot: Mapping[str, Any]):
        rows = sorted(json.dumps(row["payload"], sort_keys=True, ensure_ascii=False)
                      for (sid, _k), row in r.catalog_raw_records.items() if str(sid) == str(snapshot["id"]))
        candidates = sorted(json.dumps({k: v for k, v in row.items() if k not in (
            "id", "snapshot_id", "created_at", "updated_at", "raw_record_id", "created_by_run_id")},
            sort_keys=True, default=str, ensure_ascii=False)
            for (sid, _k), row in r.catalog_candidates.items() if str(sid) == str(snapshot["id"]))
        return hashlib.sha256("\n".join(rows).encode()).hexdigest(), hashlib.sha256(
            "\n".join(candidates).encode()).hexdigest()

    assert content(repo, register_snapshot) == content(plain, prepare_snapshot)
    # No coverage ledger row is written by a register capture.
    assert repo.catalog_variant_coverage == {}


def test_the_count_is_a_fresh_exact_limit_zero_count_taken_after_the_rows():
    records = planned_records()
    transport = FixtureTransport(bodies={0: scoped_page(records)})
    repo, _w, _version, report, _writer = captured_world(
        client=scoped_client(records, transport=transport))
    (outcome,) = report.units
    assert outcome.status == "captured"
    counts = [i for i, (_action, params) in enumerate(transport.calls) if params.get("limit") == "0"]
    # Exactly one count, after every page of the capture, for that exact tozar.
    assert counts == [len(transport.calls) - 1]
    params = transport.calls[counts[0]][1]
    assert json.loads(params["filters"]) == {"tozar": TOYOTA} and "q" not in params
    assert the_unit(repo)["api_total"] == len(records)


def test_a_count_mismatch_keeps_the_snapshot_inactive_and_out_of_prepare():
    # The capture itself is complete and self-consistent (its reported total
    # matches its rows); the FRESH count at its end says the source now holds
    # one more row for that tozar. No counter is patched.
    records = planned_records()
    repo, _w, _version, report, writer = captured_world(
        client=scoped_client(records, fresh_total=len(records) + 1))
    (outcome,) = report.units
    assert (outcome.status, outcome.failure_code) == ("failed", "CATALOG_CAPTURE_COUNT_MISMATCH")
    unit = the_unit(repo)
    assert unit["status"] == "failed" and unit["failure_code"] == "CATALOG_CAPTURE_COUNT_MISMATCH"
    assert unit["count_verified"] is False
    assert (unit["api_total"], unit["captured_rows"]) == (len(records) + 1, None)
    snapshot = snapshot_by_key(repo, outcome.snapshot_key)
    assert snapshot["activated_at"] is None
    # Prepare resolves ACTIVE snapshots only: this one is never used.
    assert repo.find_active_catalog_snapshot(src.GOVERNMENT_SOURCE_FAMILY, src.WLTP_RESOURCE_ID,
                                             outcome.snapshot_key) is None
    assert writer.objects == {}


@pytest.mark.parametrize("writer, code", [
    (FakeWriter(fail=True), "CATALOG_ARCHIVE_WRITE_FAILED"),
    (FakeWriter(outcome=arc.EXISTS_UNVERIFIED), "CATALOG_ARCHIVE_WRITE_FAILED"),
    (None, "CATALOG_ARCHIVE_NOT_CONFIGURED"),
])
def test_no_recorded_archive_no_activation(writer, code):
    if writer is None:
        repo, w = world()
        version = directory(repo)
        answer, _ = request(repo, w, version, [TOYOTA])
        lease = claimed_lease(repo, repo.register_capture_group(answer["group_id"])["group"]["run_id"])
        report = capture_group(repo, lease, client=scoped_client(planned_records()),
                               group_id=answer["group_id"], archive_writer=None)
    else:
        repo, _w, _version, report, _writer = captured_world(writer=writer)
    (outcome,) = report.units
    assert (outcome.status, outcome.failure_code) == ("failed", code)
    assert snapshot_by_key(repo, outcome.snapshot_key)["activated_at"] is None
    assert repo.register_snapshot_archive(snapshot_by_key(repo, outcome.snapshot_key)["id"]) is None


def test_the_database_refuses_a_captured_unit_without_an_archive():
    repo, w = world()
    version = directory(repo)
    answer, _ = request(repo, w, version, [TOYOTA])
    lease = claimed_lease(repo, repo.register_capture_group(answer["group_id"])["group"]["run_id"])
    report = GovernmentCatalogIngestor(repo, lease, client=scoped_client(planned_records())).ingest_resource(
        src.WLTP_RESOURCE_ID, capture_scope=CaptureScope.for_register_marque(TOYOTA))
    unit = the_unit(repo)
    with pytest.raises(AppError) as refused:
        repo.record_register_unit_status(lease.run_id, unit["id"], "captured", None, report.snapshot_id, 28, 28,
                                         True, worker_id=lease.worker_id, attempt=lease.attempt,
                                         lease_token=lease.lease_token)
    assert refused.value.code == "CATALOG_REGISTER_CAPTURE_UNVERIFIED"


def test_a_group_is_captured_only_by_its_own_run():
    repo, w = world()
    version = directory(repo)
    answer, _ = request(repo, w, version, [TOYOTA])
    _p, other = world(repo)
    code, doc = entrypoint.prepare_capture_run(repo, conversation_id=UUID(other["conversation"]),
                                               requested_by=USER, idempotency_key="other",
                                               env=capture_env(MILO_RELEASE_SHA=RELEASE_SHA))
    lease = claimed_lease(repo, doc["preparation"]["run_id"])
    from backend.catalog.register.capture import RegisterCaptureError

    with pytest.raises(RegisterCaptureError) as refused:
        capture_group(repo, lease, client=scoped_client(planned_records()), group_id=answer["group_id"],
                      archive_writer=FakeWriter())
    assert refused.value.reason_code == "CATALOG_REGISTER_UNIT_NOT_THIS_RUN"


def test_the_capture_job_entrypoint_runs_the_invocation_the_api_sent(capsys, monkeypatch):
    repo, w = world()
    version = directory(repo)
    trigger = FakeTrigger()
    answer, _ = request(repo, w, version, [TOYOTA], trigger)
    (invocation,) = trigger.calls
    assert dict(invocation.env_overrides) == {"MILO_ENABLE_REGISTER_CAPTURE_JOB": "true"}
    args = list(invocation.entrypoint_args)
    assert args[args.index("--register-group-id") + 1] == answer["group_id"]
    run_id = args[args.index("--run-id") + 1]
    writer = FakeWriter()
    monkeypatch.setattr(entrypoint, "_open_repository", lambda: repo)
    monkeypatch.setattr(entrypoint, "_open_transport",
                        lambda: FixtureTransport(bodies={0: scoped_page(planned_records())}))
    monkeypatch.setattr(entrypoint, "_open_archive_writer", lambda env: writer)
    argv = authorized_argv(run_id, **{"--register-group-id": answer["group_id"]})
    env = capture_env(MILO_RELEASE_SHA=RELEASE_SHA, MILO_ENABLE_REGISTER_CAPTURE_JOB="true")
    status = entrypoint.main(argv, env=env)
    document = json.loads(capsys.readouterr().out)
    assert status == entrypoint.EXIT_OK, document
    assert document["register"]["group"]["captured"] == 1
    assert repo.runs[run_id]["status"] == "completed"
    # The register text never leaves in the report... except its own tozar,
    # which is the unit's name, not row content.
    assert "kinuy_mishari" not in json.dumps(document, ensure_ascii=False)


def test_the_capture_job_refuses_register_mode_without_its_switch(capsys, monkeypatch):
    monkeypatch.setattr(entrypoint, "_open_repository", lambda: pytest.fail("no repository"))
    monkeypatch.setattr(entrypoint, "_open_transport", lambda: pytest.fail("no transport"))
    argv = authorized_argv(uuid4(), **{"--register-group-id": str(uuid4())})
    assert entrypoint.main(argv, env=capture_env()) == entrypoint.EXIT_REFUSED
    assert json.loads(capsys.readouterr().out)["reason_code"] == "CAPTURE_REGISTER_CAPTURE_DISABLED"
    both = authorized_argv(uuid4(), **{"--register-group-id": str(uuid4()), "--register-directory": True})
    assert entrypoint.main(both, env=capture_env(MILO_ENABLE_REGISTER_CAPTURE_JOB="true")) == entrypoint.EXIT_REFUSED
    assert json.loads(capsys.readouterr().out)["reason_code"] == "CAPTURE_REGISTER_ARGUMENTS_INVALID"


# =============================================================================
# 4. the API side: idempotency, group cap, capacity, release (D1-3/4)
# =============================================================================

def test_capture_is_idempotent_per_tozar_and_directory_version():
    repo, w = world()
    version = directory(repo)
    trigger = FakeTrigger()
    first, started = request(repo, w, version, [TOYOTA], trigger)
    again, started_again = request(repo, w, version, [TOYOTA], trigger)
    assert started and not started_again
    assert again["decision"] == "existing" and len(trigger.calls) == 1
    assert len(repo._register_state()["groups"]) == 1


def test_a_group_over_the_cap_is_refused_and_one_large_tozar_goes_alone():
    repo, w = world()
    version = directory(repo, {TOYOTA: 28, LEXUS: 10_001})
    trigger = FakeTrigger()
    with pytest.raises(AppError) as refused:
        request(repo, w, version, [TOYOTA, LEXUS], trigger)
    assert refused.value.code == "CATALOG_REGISTER_GROUP_TOO_LARGE" and refused.value.status_code == 422
    assert trigger.calls == [] and repo.register_capture_units() == []
    _answer, started = request(repo, w, version, [LEXUS], trigger)
    assert started and len(trigger.calls) == 1


def test_the_group_cap_is_configurable():
    repo, w = world()
    version = directory(repo, {TOYOTA: 28, LEXUS: 100})
    with pytest.raises(AppError):
        request(repo, w, version, [TOYOTA, LEXUS], env={"MILO_REGISTER_GROUP_MAX_ROWS": "100"})
    assert request(repo, w, version, [TOYOTA, LEXUS], env={"MILO_REGISTER_GROUP_MAX_ROWS": "128"})[1]


def test_the_capacity_guard_refuses_with_the_exact_numbers_and_starts_nothing():
    repo, w = world()
    repo.register_database_bytes = 399_000_000
    version = directory(repo, {TOYOTA: 28, LEXUS: 1000})
    trigger = FakeTrigger()
    with pytest.raises(register_service.CapacityRefusal) as refused:
        request(repo, w, version, [LEXUS], trigger)
    # 399,000,000 + 1000 x 3500 = 402,500,000 > 0.80 x 500,000,000.
    assert refused.value.capacity == {"current_bytes": 399_000_000, "projected_bytes": 402_500_000,
                                      "limit_bytes": 400_000_000}
    assert refused.value.code == "CATALOG_CAPACITY_THRESHOLD_EXCEEDED"
    assert trigger.calls == [] and repo.register_capture_units() == []
    assert not [r for r in repo.runs.values()
                if (r.get("run_identity") or {}).get("workflow_key") == "operator_capture"]
    # The same capture fits under a configured, larger capacity.
    assert request(repo, w, version, [LEXUS], trigger, env={"MILO_DB_CAPACITY_BYTES": "600000000"})[1]


def test_a_capture_job_off_the_release_is_refused_before_anything_is_written():
    repo, w = world()
    version = directory(repo)
    for refusal, code in ((trig.JOB_NOT_RELEASE, "CATALOG_REGISTER_JOB_NOT_RELEASE"),
                          (trig.JOB_UNREADABLE, "CATALOG_REGISTER_JOB_UNREADABLE")):
        trigger = FakeTrigger(refusal=refusal)
        with pytest.raises(AppError) as refused:
            request(repo, w, version, [TOYOTA], trigger)
        assert refused.value.code == code and trigger.calls == []
    assert repo.register_capture_units() == []


def test_a_stale_version_or_unknown_tozar_is_refused():
    repo, w = world()
    version = directory(repo)
    for version_, tozars, code in ((("0" * 64), [TOYOTA], "CATALOG_REGISTER_VERSION_STALE"),
                                   (version, ["טויוטה "], "CATALOG_REGISTER_UNIT_UNKNOWN")):
        with pytest.raises(AppError) as refused:
            request(repo, w, version_, tozars)
        assert refused.value.code == code


def test_a_failed_trigger_is_retryable():
    repo, w = world()
    version = directory(repo)
    with pytest.raises(AppError) as refused:
        request(repo, w, version, [TOYOTA], FakeTrigger(state=trig.TRIGGER_FAILED))
    assert refused.value.code == "CATALOG_REGISTER_TRIGGER_FAILED"
    assert the_unit(repo)["status"] == "failed"
    assert request(repo, w, version, [TOYOTA], FakeTrigger())[1] is True
    assert the_unit(repo)["attempt"] == 2


def client(repo: MemoryRepository, trigger: Any) -> TestClient:
    app.dependency_overrides[get_repository] = lambda: repo
    app.dependency_overrides[get_capture_trigger] = lambda: trigger
    return TestClient(app)


def as_user(user: UUID = USER) -> dict[str, str]:
    return {"x-milo-auth-user-id": str(user)}


def test_the_register_page_shows_every_state_totals_and_capacity():
    repo, w, version, _report, _writer = captured_world()
    api = client(repo, FakeTrigger())
    page = api.get(f"/projects/{w['project']}/register", headers=as_user())
    assert page.status_code == 200, page.text
    body = page.json()
    states = {u["tozar"]: u for u in body["units"]}
    assert states[TOYOTA]["state"] == "captured" and states[TOYOTA]["verified"] is True
    assert states[TOYOTA]["measured_bytes_per_row"] > 0
    assert states[LEXUS]["state"] == "not_captured"
    assert body["totals"] == {"units_total": 2, "units_captured": 1, "rows_total": 5028, "rows_captured": 28}
    assert body["capacity"]["limit_bytes"] == 400_000_000 and body["group_max_rows"] == 10_000
    assert body["directory"]["register_version"] == version
    assert body["can_capture"] is True and body["can_refresh_directory"] is True


def test_the_api_captures_answers_202_then_200_and_a_capacity_body():
    repo, w = world()
    version = directory(repo)
    api = client(repo, FakeTrigger())
    body = {"register_version": version, "tozars": [TOYOTA], "conversation_id": w["conversation"]}
    first = api.post(f"/projects/{w['project']}/register/captures", headers=as_user(), json=body)
    again = api.post(f"/projects/{w['project']}/register/captures", headers=as_user(), json=body)
    assert (first.status_code, again.status_code) == (202, 200), first.text
    repo.register_database_bytes = 399_999_000
    refused = api.post(f"/projects/{w['project']}/register/captures", headers=as_user(),
                       json={**body, "tozars": [LEXUS]})
    assert refused.status_code == 409
    error = refused.json()["error"]
    assert error["code"] == "CATALOG_CAPACITY_THRESHOLD_EXCEEDED"
    # The first request's 28 rows are still in flight: they count too.
    assert error["capacity"] == {"current_bytes": 399_999_000,
                                 "projected_bytes": 399_999_000 + (5000 + 28) * 3500,
                                 "limit_bytes": 400_000_000}


def test_a_non_member_is_refused_and_nothing_is_written():
    repo, w = world()
    version = directory(repo)
    trigger = FakeTrigger()
    api = client(repo, trigger)
    body = {"register_version": version, "tozars": [TOYOTA], "conversation_id": w["conversation"]}
    assert api.post(f"/projects/{w['project']}/register/captures", headers=as_user(OUTSIDER),
                    json=body).status_code == 404
    assert api.get(f"/projects/{w['project']}/register", headers=as_user(OUTSIDER)).status_code == 404
    assert trigger.calls == [] and repo.register_capture_units() == []


def test_the_flag_off_hides_the_page_and_refuses_both_writes(monkeypatch):
    monkeypatch.delenv(register_service.REGISTER_FLAG)
    repo, w = world()
    version = directory(repo)
    trigger = FakeTrigger()
    api = client(repo, trigger)
    page = api.get(f"/projects/{w['project']}/register", headers=as_user())
    assert page.status_code == 404 and page.json()["error"]["code"] == "CATALOG_REGISTER_DISABLED"
    for path, body in (("captures", {"register_version": version, "tozars": [TOYOTA],
                                      "conversation_id": w["conversation"]}),
                       ("directory", {"conversation_id": w["conversation"]})):
        refused = api.post(f"/projects/{w['project']}/register/{path}", headers=as_user(), json=body)
        assert refused.status_code == 403
        assert refused.json()["error"]["code"] == "EXECUTION_SURFACE_DISABLED"
    assert trigger.calls == []


def test_capture_needs_neither_run_creation_nor_the_run_start_flag():
    """The fixture deleted MILO_ENABLE_RUN_CREATION and
    GATEWAY_ALLOW_RUN_START_ROUTES: the capture still starts."""
    repo, w = world()
    version = directory(repo)
    trigger = FakeTrigger()
    api = client(repo, trigger)
    started = api.post(f"/projects/{w['project']}/register/captures", headers=as_user(), json={
        "register_version": version, "tozars": [TOYOTA], "conversation_id": w["conversation"]})
    assert started.status_code == 202 and len(trigger.calls) == 1


def test_a_directory_refresh_executes_the_directory_invocation():
    repo, w = world()
    trigger = FakeTrigger()
    api = client(repo, trigger)
    started = api.post(f"/projects/{w['project']}/register/directory", headers=as_user(),
                       json={"conversation_id": w["conversation"]})
    assert started.status_code == 202, started.text
    (invocation,) = trigger.calls
    assert "--register-directory" in invocation.entrypoint_args


def test_one_directory_refresh_at_a_time():
    repo, w = world()
    trigger = FakeTrigger()
    api = client(repo, trigger)
    body = {"conversation_id": w["conversation"]}
    first = api.post(f"/projects/{w['project']}/register/directory", headers=as_user(), json=body)
    assert first.status_code == 202, first.text
    # A second press while the first is live answers it, and executes nothing.
    again = api.post(f"/projects/{w['project']}/register/directory", headers=as_user(), json=body)
    assert again.status_code == 200, again.text
    assert again.json() == {**first.json(), "started": False}
    assert len(trigger.calls) == 1
    # Once that run has ended, a new refresh starts.
    repo.runs[first.json()["run_id"]]["status"] = "completed"
    third = api.post(f"/projects/{w['project']}/register/directory", headers=as_user(), json=body)
    assert third.status_code == 202 and third.json()["group_id"] != first.json()["group_id"]
    assert len(trigger.calls) == 2


def test_register_actions_have_their_own_rate_limit_bucket(monkeypatch):
    from backend import main as main_module
    from backend import rate_limit

    seen: list[str] = []
    real = main_module.enforce_rate_limit
    monkeypatch.setattr(main_module, "enforce_rate_limit",
                        lambda category, identifier, *a: (seen.append(category), real(category, identifier, *a)))
    rate_limit.reset_for_tests()
    monkeypatch.setenv("MILO_RATE_LIMIT_REGISTER_ACTIONS_USER", "1")
    repo, w = world()
    version = directory(repo)
    api = client(repo, FakeTrigger())
    refresh = api.post(f"/projects/{w['project']}/register/directory", headers=as_user(),
                       json={"conversation_id": w["conversation"]})
    assert refresh.status_code == 202, refresh.text
    capture = api.post(f"/projects/{w['project']}/register/captures", headers=as_user(), json={
        "register_version": version, "tozars": [TOYOTA], "conversation_id": w["conversation"]})
    assert capture.status_code == 429
    assert seen == ["register_actions_user", "register_actions_user"]
    rate_limit.reset_for_tests()


# =============================================================================
# 5. retention (D1-5) and REGISTER_COVERAGE (D1-6)
# =============================================================================

def _snapshot(key: str, tozar: str | None, activated: str | None, **extra) -> dict[str, Any]:
    metadata = {"capture_scope": {"filters": {"tozar": tozar}}} if tozar else {}
    return {"id": str(uuid4()), "snapshot_key": key, "source_family": "government",
            "retrieval_metadata": metadata, "activated_at": activated, "created_by_run_id": None, **extra}


def test_retention_keeps_the_active_the_previous_and_every_referenced_snapshot():
    rows = [_snapshot("cs1.t1", TOYOTA, "2026-09-01"), _snapshot("cs1.t2", TOYOTA, "2026-09-02"),
            _snapshot("cs1.t3", TOYOTA, "2026-09-03"), _snapshot("cs1.t0", TOYOTA, None),
            _snapshot("cs1.ref", TOYOTA, None), _snapshot("cs1.cov", TOYOTA, None),
            _snapshot("cs1.live", TOYOTA, None, created_by_run_id="live-run"),
            _snapshot("cs1.whole", None, "2026-08-01")]
    referenced = {rows[4]["id"]}
    chosen = retention.prunable(rows, referenced_ids=referenced, referenced_keys={"cs1.cov"},
                                live_run_ids={"live-run"})
    assert [r["snapshot_key"] for r in chosen] == ["cs1.t0", "cs1.t1"]


def test_the_digest_is_sorted_and_the_database_s():
    keys = ["cs1.b", "cs1.a"]
    assert retention.prune_digest(keys) == hashlib.sha256(b"cs1.a\ncs1.b\n").hexdigest()
    assert retention.prune_digest([]) == hashlib.sha256(b"").hexdigest()


class PruneRepo:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.pruned: list[tuple[list[str], str]] = []

    def prunable_register_snapshots(self):
        return list(self.rows)

    def prune_register_snapshots(self, keys, digest):
        self.pruned.append((list(keys), digest))
        return {"snapshots": len(keys), "raw_records": 10, "candidates": 4}


ROWS = [{"snapshot_key": "cs1.a", "raw_rows": 5, "estimated_bytes": 1000},
        {"snapshot_key": "cs1.b", "raw_rows": 7, "estimated_bytes": 2000}]


def test_the_dry_run_lists_rows_bytes_and_the_digest_and_changes_nothing(capsys):
    repo = PruneRepo(ROWS)
    assert prune_module.main(["--list"], repository=repo) == 0
    out = capsys.readouterr().out
    assert "PRUNABLE cs1.a rows=5 estimated_bytes=1000" in out
    assert "TOTAL snapshots=2 rows=12 estimated_bytes=3000" in out
    assert f"DIGEST {retention.prune_digest(['cs1.a', 'cs1.b'])}" in out
    assert repo.pruned == []


@pytest.mark.parametrize("argv, code", [
    (["--apply", "--confirm", "prune", "--digest", "a" * 64], "CATALOG_PRUNE_NOT_CONFIRMED"),
    (["--apply", "--confirm", "PRUNE", "--digest", "xyz"], "CATALOG_PRUNE_REQUEST_INVALID"),
    (["--apply", "--confirm", "PRUNE", "--digest", "a" * 64], "CATALOG_PRUNE_DIGEST_MISMATCH"),
    (["--list", "--confirm", "PRUNE"], "CATALOG_PRUNE_REQUEST_INVALID"),
])
def test_apply_refuses_without_the_word_or_the_exact_digest(argv, code, capsys):
    repo = PruneRepo(ROWS)
    assert prune_module.main(argv, repository=repo) == prune_module.EXIT_REFUSED
    assert f"REFUSED {code}" in capsys.readouterr().out
    assert repo.pruned == []


def test_apply_prunes_exactly_the_listed_digest(capsys):
    repo = PruneRepo(ROWS)
    digest = retention.prune_digest(["cs1.a", "cs1.b"])
    assert prune_module.main(["--apply", "--confirm", "PRUNE", "--digest", digest], repository=repo) == 0
    assert repo.pruned == [(["cs1.a", "cs1.b"], digest)]
    assert "(archive objects untouched)" in capsys.readouterr().out


def test_the_memory_prune_never_selects_a_referenced_snapshot_and_refuses_a_stale_digest():
    repo, _w, _version, report, _writer = captured_world()
    kept = report.units[0].snapshot_key
    # An older, unreferenced, inactive Toyota snapshot is the only prunable one.
    old = _snapshot("cs1.old", TOYOTA, None)
    repo.catalog_snapshots[("government", src.WLTP_RESOURCE_ID, "cs1.old")] = old
    listed = repo.prunable_register_snapshots()
    assert [row["snapshot_key"] for row in listed] == ["cs1.old"]
    assert kept not in {row["snapshot_key"] for row in listed}
    with pytest.raises(AppError) as refused:
        repo.prune_register_snapshots(["cs1.old"], "0" * 64)
    assert refused.value.code == "CATALOG_PRUNE_DIGEST_MISMATCH"
    result = repo.prune_register_snapshots(["cs1.old"], retention.prune_digest(["cs1.old"]))
    assert result["snapshots"] == 1
    assert snapshot_by_key(repo, kept)["activated_at"] is not None


def test_register_coverage_is_informational_and_fails_only_above_the_threshold():
    config = register_config.load({})
    coverage = {"register_version": "a" * 64, "units_total": 10, "units_captured": 3, "rows_total": 1000,
                "rows_captured": 300, "unverified_snapshots": 1, "database_bytes": 120_000_000}
    result, detail = coverage_module.coverage_line(coverage, config, config_source="reviewed defaults")
    assert result == "INFO"
    assert detail == ("directory " + "a" * 64 + "; units 3/10; rows 300/1000; database 120000000/400000000 bytes "
                      "(0.80 of 500000000, reviewed defaults); unverified snapshots 1")
    over, _ = coverage_module.coverage_line({**coverage, "database_bytes": 400_000_001}, config,
                                            config_source="x")
    assert over == "FAIL"


def test_register_coverage_reads_the_api_capacity_from_its_plain_env_only(tmp_path, capsys):
    service = {"spec": {"template": {"spec": {"containers": [{"env": [
        {"name": "MILO_DB_CAPACITY_BYTES", "value": "1000"},
        {"name": "MILO_DB_CAPACITY_THRESHOLD", "value": "0.5"},
        {"name": "SUPABASE_SECRET_KEY", "valueFrom": {"secretKeyRef": {"name": "S", "key": "latest"}}},
    ]}]}}}}
    path = tmp_path / "service.json"
    path.write_text(json.dumps(service))
    import io

    status = coverage_module.main(["--service-json", str(path)],
                                  stdin=io.StringIO(json.dumps({"database_bytes": 600})))
    out = capsys.readouterr().out
    assert status == 1 and out.startswith("REGISTER_COVERAGE=FAIL ") and "600/500 bytes" in out
    assert "SUPABASE_SECRET_KEY" not in out
    assert coverage_module.main([], stdin=io.StringIO("null")) == 0
    assert "REGISTER_COVERAGE=INFO not available" in capsys.readouterr().out


# =============================================================================
# 6. the review's findings, each held by a test
# =============================================================================

def test_a_failed_unit_fails_its_run_so_a_retry_and_prepare_can_adopt_its_snapshot(capsys, monkeypatch):
    """Blocker 1: an archive failure leaves a PENDING snapshot; the run must end
    `failed` so the next writer of the same content adopts it."""
    repo, w = world()
    version = directory(repo)
    trigger = FakeTrigger()
    answer, _ = request(repo, w, version, [TOYOTA], trigger)
    args = list(trigger.calls[0].entrypoint_args)
    run_id = args[args.index("--run-id") + 1]
    monkeypatch.setattr(entrypoint, "_open_repository", lambda: repo)
    monkeypatch.setattr(entrypoint, "_open_transport",
                        lambda: FixtureTransport(bodies={0: scoped_page(planned_records())}))
    monkeypatch.setattr(entrypoint, "_open_archive_writer", lambda env: FakeWriter(fail=True))
    env = capture_env(MILO_RELEASE_SHA=RELEASE_SHA, MILO_ENABLE_REGISTER_CAPTURE_JOB="true")
    status = entrypoint.main(authorized_argv(run_id, **{"--register-group-id": answer["group_id"]}), env=env)
    document = json.loads(capsys.readouterr().out)
    assert status == entrypoint.EXIT_FAILED and document["reason_code"] == "CATALOG_REGISTER_CAPTURE_FAILED"
    assert repo.runs[run_id]["status"] == "failed"
    pending_key = document["register"]["group"]["units"][0]["snapshot_key"]
    assert snapshot_by_key(repo, pending_key)["activated_at"] is None
    assert the_unit(repo)["count_verified"] is True  # the count passed; only the archive failed

    # "Capture again", once the failed run's lease has lapsed (adoption's own
    # rule, unchanged): a new group and run adopt the pending snapshot.
    repo.runs[run_id]["lease_expires_at"] = "2000-01-01T00:00:00+00:00"
    retry, started = request(repo, w, version, [TOYOTA], trigger)
    assert started
    args = list(trigger.calls[-1].entrypoint_args)
    run_id = args[args.index("--run-id") + 1]
    writer = FakeWriter()
    monkeypatch.setattr(entrypoint, "_open_archive_writer", lambda env: writer)
    status = entrypoint.main(authorized_argv(run_id, **{"--register-group-id": retry["group_id"]}), env=env)
    document = json.loads(capsys.readouterr().out)
    assert status == entrypoint.EXIT_OK, document
    assert document["register"]["group"]["units"][0]["snapshot_key"] == pending_key
    assert snapshot_by_key(repo, pending_key)["activated_at"] is not None
    assert the_unit(repo)["status"] == "captured"


def test_prepare_can_adopt_a_snapshot_a_failed_register_run_left_pending():
    repo, w = world()
    version = directory(repo)
    answer, _ = request(repo, w, version, [TOYOTA])
    run_id = repo.register_capture_group(answer["group_id"])["group"]["run_id"]
    lease = claimed_lease(repo, run_id)
    report = capture_group(repo, lease, client=scoped_client(planned_records()), group_id=answer["group_id"],
                           archive_writer=FakeWriter(fail=True))
    entrypoint._finalize(repo, lease, document={}, reason_code="CATALOG_REGISTER_CAPTURE_FAILED", cancelled=False)
    assert repo.runs[run_id]["status"] == "failed"
    repo.runs[run_id]["lease_expires_at"] = "2000-01-01T00:00:00+00:00"  # the lease has lapsed
    # Prepare's scoped capture of the same tozar (no archive hook) now adopts it.
    code, doc = entrypoint.prepare_capture_run(repo, conversation_id=UUID(w["conversation"]), requested_by=USER,
                                               idempotency_key="prepare", env=capture_env(MILO_RELEASE_SHA=RELEASE_SHA))
    prepare_lease = claimed_lease(repo, doc["preparation"]["run_id"])
    prepared = GovernmentCatalogIngestor(repo, prepare_lease, client=scoped_client(planned_records())).ingest_resource(
        src.WLTP_RESOURCE_ID, capture_scope=CaptureScope.for_register_marque(TOYOTA))
    assert prepared.snapshot_key == report.units[0].snapshot_key
    snapshot = snapshot_by_key(repo, prepared.snapshot_key)
    assert snapshot["activated_at"] is not None
    # Prepare activated it WITHOUT an archive (Prepare writes none).
    assert repo.register_snapshot_archive(snapshot["id"]) is None

    # The next register capture of that tozar meets the already-active
    # snapshot (same key, no archive row): it writes and verifies the archive
    # before it marks the unit captured -- and doing it twice is a no-op.
    retry, started = request(repo, w, version, [TOYOTA])
    assert started
    retry_lease = claimed_lease(repo, repo.register_capture_group(retry["group_id"])["group"]["run_id"])
    writer = FakeWriter()
    again = capture_group(repo, retry_lease, client=scoped_client(planned_records()),
                          group_id=retry["group_id"], archive_writer=writer)
    (outcome,) = again.units
    assert (outcome.status, outcome.snapshot_key) == ("captured", prepared.snapshot_key)
    archive = repo.register_snapshot_archive(snapshot["id"])
    assert archive is not None and archive["line_count"] == len(planned_records())
    (data,) = writer.objects.values()
    assert hashlib.sha256(data).hexdigest() == archive["sha256"]
    unit = the_unit(repo)
    assert (unit["status"], unit["count_verified"], unit["snapshot_key"]) == (
        "captured", True, prepared.snapshot_key)
    # Idempotent: the same unit again trusts the recorded archive and writes nothing.
    from backend.catalog.register.capture import capture_unit

    (group_unit,) = repo.register_capture_group(retry["group_id"])["units"]
    replay_writer = FakeWriter()
    replay = capture_unit(repo, retry_lease, client=scoped_client(planned_records()), unit=group_unit,
                          archive_writer=replay_writer)
    assert replay.status == "captured" and replay_writer.objects == {}
    assert repo.register_snapshot_archive(snapshot["id"]) == archive


def test_a_recorded_archive_is_trusted_for_the_same_snapshot():
    """Nit 8: a re-capture of an archived snapshot never rebuilds and compares gzip bytes."""
    repo, w, version, report, writer = captured_world()
    snapshot = snapshot_by_key(repo, report.units[0].snapshot_key)
    recorded = repo.register_snapshot_archive(snapshot["id"])
    from backend.catalog.register.capture import _Archiver

    # The record short-circuits before any build, write or lease is needed.
    archiver = _Archiver(repo, None, FakeWriter(fail=True), src.WLTP_RESOURCE_ID)
    assert archiver.ensure(TOYOTA, object(), snapshot) == recorded


def test_a_register_that_reverts_records_its_old_version_again():
    repo = MemoryRepository()
    a = [{"tozar": TOYOTA, "expected_rows": 28}]
    b = [{"tozar": TOYOTA, "expected_rows": 29}]
    first = repo.record_register_directory(src.WLTP_RESOURCE_ID, "t1", a)
    repo.record_register_directory(src.WLTP_RESOURCE_ID, "t2", b)
    back = repo.record_register_directory(src.WLTP_RESOURCE_ID, "t3", a)
    assert back["decision"] == "created"
    assert repo.latest_register_directory()["version"]["register_version"] == first["version"]["register_version"]


def test_the_capacity_guard_counts_rows_still_in_flight():
    repo, w = world()
    repo.register_database_bytes = 394_700_000
    version = directory(repo, {TOYOTA: 28, LEXUS: 1500})
    assert request(repo, w, version, [LEXUS])[1]  # 394,700,000 + 1500 x 3500 = 399,950,000: fits
    with pytest.raises(register_service.CapacityRefusal) as refused:
        # Alone it would fit (394,798,000); with LEXUS still in flight it does not.
        request(repo, w, version, [TOYOTA])
    assert refused.value.capacity["projected_bytes"] == 394_700_000 + (28 + 1500) * 3500


def test_the_capacity_guard_ignores_a_killed_job_across_a_directory_version_change():
    """Only LIVE in-flight work reserves capacity: a unit whose job was killed
    (its lease expired long past the grace) under an OLD directory version
    reserves nothing for a capture of the NEW version."""
    from datetime import UTC, datetime, timedelta

    repo, w = world()
    repo.register_database_bytes = 394_700_000
    old = directory(repo, {TOYOTA: 28, LEXUS: 1500})
    answer, started = request(repo, w, old, [LEXUS])
    assert started
    run_id = repo.register_capture_group(answer["group_id"])["group"]["run_id"]
    claimed_lease(repo, run_id)  # the job started: running, under a lease
    new = directory(repo, {TOYOTA: 28, LEXUS: 1501})
    assert new != old
    # Live: the old unit still counts (394,700,000 + (1501 + 1500) x 3500 > 400,000,000).
    with pytest.raises(register_service.CapacityRefusal) as refused:
        request(repo, w, new, [LEXUS])
    assert refused.value.capacity["projected_bytes"] == 394_700_000 + (1501 + 1500) * 3500
    # Killed: the job's lease expired an hour ago and nothing renewed it.
    repo.runs[run_id]["lease_expires_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    assert repo.runs[run_id]["status"] in ("starting", "running")
    answer, started = request(repo, w, new, [LEXUS])
    assert started and answer["decision"] == "claimed"


def test_a_stalled_capture_reads_failed_and_can_be_captured_again():
    from datetime import UTC, datetime, timedelta

    repo, w = world()
    version = directory(repo)
    trigger = FakeTrigger()
    answer, _ = request(repo, w, version, [TOYOTA], trigger)
    group = repo._register_state()["groups"][answer["group_id"]]
    run = repo.runs[group["run_id"]]
    old = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    group["triggered_at"] = group["claimed_at"] = old
    # A run no worker ever claimed, long past the grace.
    assert run["status"] == "queued" and not run.get("worker_id")
    api = client(repo, trigger)
    page = api.get(f"/projects/{w['project']}/register", headers=as_user()).json()
    toyota = next(u for u in page["units"] if u["tozar"] == TOYOTA)
    assert (toyota["state"], toyota["failure_code"]) == ("failed", "CATALOG_REGISTER_CAPTURE_STALLED")
    assert request(repo, w, version, [TOYOTA], trigger)[1] is True


def test_an_expired_lease_long_past_the_grace_is_retryable():
    from datetime import UTC, datetime, timedelta

    repo, w = world()
    version = directory(repo)
    trigger = FakeTrigger()
    answer, _ = request(repo, w, version, [TOYOTA], trigger)
    run_id = repo.register_capture_group(answer["group_id"])["group"]["run_id"]
    claimed_lease(repo, run_id)
    repo.runs[run_id]["lease_expires_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    assert repo.runs[run_id]["status"] in ("starting", "running")
    assert request(repo, w, version, [TOYOTA], trigger)[1] is True


def test_a_project_whose_engine_does_not_read_the_catalog_has_no_register():
    repo = MemoryRepository()
    repo.seed_user(str(USER))
    project = str(uuid4())
    repo.seed_project(project, "p-v1", "P", [str(USER)], workflow_key="vehicle_catalog_v1")
    conversation = repo.create_conversation(UUID(project), "c", USER)["id"]
    version = directory(repo)
    api = client(repo, FakeTrigger())
    assert api.get(f"/projects/{project}/register", headers=as_user()).status_code == 404
    refused = api.post(f"/projects/{project}/register/captures", headers=as_user(), json={
        "register_version": version, "tozars": [TOYOTA], "conversation_id": conversation})
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "CATALOG_REGISTER_WORKFLOW_UNSUPPORTED"


def test_retention_keeps_the_latest_captured_register_snapshot():
    repo, _w, _version, report, _writer = captured_world()
    key = report.units[0].snapshot_key
    # Two newer activations of the same tozar would otherwise make it prunable.
    snapshot = snapshot_by_key(repo, key)
    for label in ("n1", "n2"):
        newer = dict(snapshot, id=str(uuid4()), snapshot_key=f"cs1.{label}",
                     activated_at="2099-01-0%s" % ("1" if label == "n1" else "2"))
        repo.catalog_snapshots[("government", src.WLTP_RESOURCE_ID, newer["snapshot_key"])] = newer
    assert key not in {row["snapshot_key"] for row in repo.prunable_register_snapshots()}

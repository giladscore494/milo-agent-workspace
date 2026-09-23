"""Scoped catalog PR2: scoped preparation, the durable queue, batches, and
exact-batch runs -- offline, over the committed R5 register rows.

Every Government byte here is the committed R5 capture read through the R5
manifest gate. A SCOPED page is one of those pages with its query echo changed
from `q=RAV4` to `filters={"tozar": "טויוטה"}` -- every committed row states
that marque -- which is exactly the difference between the pinned query and the
per-marque capture scoped preparation performs. No socket is ever opened.
"""

from __future__ import annotations

import hashlib
import json
import socket
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID, uuid4

import pytest

from backend.catalog import operator_capture as entrypoint
from backend.catalog.government import client as client_module
from backend.catalog.government import source as src
from backend.catalog.government.capture_scope import (CaptureScope, CaptureScopeError,
                                                      declared_scope, is_scoped)
from backend.catalog.government.client import DataGovClient
from backend.catalog.government.ingest import (GovernmentCatalogIngestor,
                                               GovernmentIngestionError)
from backend.catalog.government.preparation import (BATCH_ARTIFACT_KEY,
                                                    GovernmentPreparationError,
                                                    prepare_government_work)
from backend.catalog.government.projection import (GovernmentProjectionError,
                                                   resolve_active_snapshot)
from backend.catalog.government.refresh import GovernmentCatalogRefresh
from backend.catalog.government.source import GovernmentSourceError
from backend.catalog.scope import contract as wsc
from backend.catalog.scope.preparation import WorkScopePreparationError, prepare_work_scope
from backend.engines.swarm_v2.evidence import WorkerLease
from backend.errors import AppError
from backend.testing import government_capture as capture_fixtures
from backend.testing.government_capture import PINNED_QUERY, FixtureTransport
from backend.testing.memory_repository import MemoryRepository
from tests.run_factory import identity_kwargs
from tests.test_catalog_operator_capture import (PROJECT_REF, SUPABASE_URL, authorized_argv,
                                                 capture_env, prepare_argv)

TOYOTA = "טויוטה"
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def no_sockets(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("an offline preparation test attempted a network connection")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture(autouse=True)
def process_environment(monkeypatch):
    """The capture entrypoint verifies the project against `os.environ` too."""
    monkeypatch.setenv("SUPABASE_URL", SUPABASE_URL)


# =============================================================================
# helpers
# =============================================================================

def committed_records(count: int, *, start: int = 0) -> list[dict[str, Any]]:
    rows = (capture_fixtures.page_document(0)["result"]["records"]
            + capture_fixtures.page_document(100)["result"]["records"])
    return [dict(row) for row in rows[start:start + count]]


def planned_records() -> list[dict[str, Any]]:
    """28 committed Toyota rows, re-read so their canonical order is known.

    23 readable rows in 2019 (`MODEL-00`..`MODEL-22`), 2 readable rows in 2016
    (outside a 2018+ plan), and 3 rows stating fuel code 2 (`דיזל`, diesel) --
    a code the reviewed vocabulary does NOT name, so they read as `ambiguous`.
    That is the real vocabulary gap, reproduced on register rows.
    """
    rows = committed_records(28)
    for index, row in enumerate(rows):
        row["kinuy_mishari"] = f"MODEL-{index:02d}"
        row["shnat_yitzur"] = 2019
    for row in rows[23:25]:
        row["shnat_yitzur"] = 2016
    for row in rows[25:]:
        row["shnat_yitzur"] = 2020
        row["delek_cd"], row["delek_nm"] = 2, "דיזל"
    return rows


def scoped_page(records: list[Mapping[str, Any]], *, marque: str = TOYOTA,
                limit: int = entrypoint.CAPTURE_PAGE_LIMIT) -> bytes:
    """One committed page, re-shaped as the `filters={"tozar": marque}` page."""
    document = capture_fixtures.page_document(0)
    result = document["result"]
    result.pop("q", None)
    result["filters"] = {"tozar": marque}
    result["limit"], result["offset"] = limit, 0
    result["total"], result["total_was_estimated"] = len(records), False
    result["records"] = list(records)
    return capture_fixtures.encode(document)


def whole_page(records: list[Mapping[str, Any]]) -> bytes:
    document = capture_fixtures.page_document(0)
    result = document["result"]
    result.pop("q", None)
    result["limit"], result["offset"] = entrypoint.CAPTURE_PAGE_LIMIT, 0
    result["total"], result["total_was_estimated"] = len(records), False
    result["records"] = list(records)
    return capture_fixtures.encode(document)


def scoped_client(records: list[Mapping[str, Any]], *,
                  marque: str = TOYOTA) -> tuple[DataGovClient, FixtureTransport]:
    transport = FixtureTransport(bodies={0: scoped_page(records, marque=marque)})
    return DataGovClient(transport, page_limit=entrypoint.CAPTURE_PAGE_LIMIT,
                         sleep_fn=lambda _seconds: None), transport


def pinned_client() -> DataGovClient:
    return DataGovClient(FixtureTransport(), page_limit=capture_fixtures.PINNED_PAGE_LIMIT,
                         sleep_fn=lambda _seconds: None)


def seeded_conversation(repository: MemoryRepository) -> tuple[str, str]:
    user, project = str(uuid4()), str(uuid4())
    repository.seed_user(user)
    repository.seed_project(project, f"ws-{project[:8]}", "Plan", [user], workflow_key="swarm_v2")
    conversation = repository.create_conversation(UUID(project), "plan", UUID(user))
    return conversation["id"], user


def plan_world(repository: MemoryRepository, units=("toyota", "lexus"), **fields) -> dict:
    conversation, user = seeded_conversation(repository)
    scope = wsc.scope_from_fields({"units": list(units), "model_year_from": 2018,
                                   "model_year_to": None, "max_items": 25, "batch_size": 10,
                                   **fields})
    created = repository.create_work_scope(
        UUID(conversation), UUID(user),
        {"scope_text": scope.canonical_text(), "input_kind": "edit", "instruction": None,
         "notes": []})
    return {"conversation": conversation, "user": user, "scope": scope,
            "plan": created["work_scope"]["id"], "digest": scope.digest()}


def swarm_lease(repository: MemoryRepository, conversation: str | None = None,
                user: str | None = None) -> WorkerLease:
    """An ordinary Swarm V2 run holding a real lease."""
    if conversation is None:
        conversation, user = seeded_conversation(repository)
    run = repository.create_message_and_run(
        conversation, "go", {}, requested_by=user, idempotency_key=str(uuid4()),
        request_fingerprint="fp", **identity_kwargs(repository, conversation))["run"]
    claimed = repository.claim_run(run["id"], f"worker-{run['id'][:6]}")
    return WorkerLease(claimed["id"], f"worker-{run['id'][:6]}", int(claimed["attempt"]),
                       claimed["lease_token"])


def swarm_run(repository: MemoryRepository, world: Mapping[str, Any]) -> str:
    return repository.create_message_and_run(
        world["conversation"], "batch", {}, requested_by=world["user"],
        idempotency_key=str(uuid4()), request_fingerprint="fp",
        **identity_kwargs(repository, world["conversation"]))["run"]["id"]


def capture_lease(repository: MemoryRepository, capsys) -> WorkerLease:
    """An OPERATOR CAPTURE run, made by the supported `--prepare`, then claimed."""
    conversation, user = seeded_conversation(repository)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(entrypoint, "_open_repository", lambda: repository)
        status = entrypoint.main(prepare_argv(conversation, user), env=capture_env())
    document = json.loads(capsys.readouterr().out)
    assert status == entrypoint.EXIT_OK, document
    run_id = document["preparation"]["run_id"]
    claimed = repository.claim_run(run_id, "capture-worker")
    return WorkerLease(claimed["id"], "capture-worker", int(claimed["attempt"]),
                       claimed["lease_token"])


def scoped_snapshot(repository: MemoryRepository, lease: WorkerLease,
                    records: list[Mapping[str, Any]], *, marque: str = TOYOTA):
    client, _transport = scoped_client(records, marque=marque)
    report = GovernmentCatalogIngestor(repository, lease, client=client).ingest_resource(
        src.WLTP_RESOURCE_ID, capture_scope=CaptureScope.for_register_marque(marque))
    return repository.find_active_catalog_snapshot(src.GOVERNMENT_SOURCE_FAMILY,
                                                   src.WLTP_RESOURCE_ID, report.snapshot_key)


def units(snapshot: Mapping[str, Any] | None, *, state: str = "captured",
          reason: str | None = None) -> list[dict[str, Any]]:
    return [{"unit_key": "toyota", "priority": 1, "state": state, "register_marque": TOYOTA,
             "snapshot_id": None if snapshot is None else snapshot["id"],
             "reason_code": reason},
            {"unit_key": "lexus", "priority": 2, "state": "register_unverified",
             "register_marque": None, "snapshot_id": None, "reason_code": None}]


def prepare(repository, lease, world, submitted, *, revision: int = 1, digest: str | None = None):
    return repository.prepare_work_scope_queue(
        lease.run_id, {"work_scope_id": world["plan"], "revision": revision,
                       "scope_digest": digest or world["digest"], "units": submitted},
        worker_id=lease.worker_id, attempt=lease.attempt, lease_token=lease.lease_token)


def app_error(code: str, call, *args, **kwargs) -> AppError:
    with pytest.raises(AppError) as failure:
        call(*args, **kwargs)
    assert failure.value.code == code, failure.value.code
    return failure.value


# =============================================================================
# 1. the scope contract
# =============================================================================

def test_the_scope_filters_text_is_the_clients_own_canonical_filters():
    scope = CaptureScope.for_register_marque(TOYOTA)
    assert scope.filters_text() == client_module._canonical_filters({"tozar": TOYOTA})
    assert scope.query() == {"filters": '{"tozar":"טויוטה"}'}
    assert scope.key() == hashlib.sha256(scope.filters_text().encode("utf-8")).hexdigest()
    assert scope.as_metadata() == {"contract": "gov.capture_scope.1",
                                   "filters": {"tozar": TOYOTA}, "scope_key": scope.key()}


@pytest.mark.parametrize("filters", [
    {}, {"kinuy_mishari": "RAV4"}, {"tozar": ""}, {"tozar": " טויוטה"}, {"tozar": "טויוטה\n"},
    {"tozar": "x" * 121}, {"tozar": 7}, {"tozar": "טוי‏וטה"}, {"tozar": TOYOTA, "q": "x"},
    "tozar=טויוטה",
])
def test_a_scope_is_one_exact_register_marque_or_nothing(filters):
    with pytest.raises(CaptureScopeError):
        CaptureScope.from_filters(filters)


def test_a_declaration_is_parsed_strictly_and_an_unreadable_one_fails_closed():
    scope = CaptureScope.for_register_marque(TOYOTA)
    good = {"retrieval_metadata": {"query": scope.query(), "capture_scope": scope.as_metadata()}}
    assert declared_scope(good) == scope and is_scoped(good)
    assert declared_scope({"retrieval_metadata": {"query": {}}}) is None
    assert not is_scoped({"retrieval_metadata": {"query": {"q": "RAV4"}}})
    broken = [
        {"query": scope.query(), "capture_scope": {**scope.as_metadata(), "scope_key": "0" * 64}},
        {"query": {"q": "RAV4"}, "capture_scope": scope.as_metadata()},
        {"query": scope.query(), "capture_scope": {**scope.as_metadata(), "extra": 1}},
        {"query": scope.query(), "capture_scope": {**scope.as_metadata(), "contract": "x"}},
        {"query": scope.query(), "capture_scope": "toyota"},
    ]
    for metadata in broken:
        with pytest.raises(CaptureScopeError):
            declared_scope({"retrieval_metadata": metadata})
        assert is_scoped({"retrieval_metadata": metadata})


# =============================================================================
# 2. a scoped snapshot is never the register
# =============================================================================

def test_a_scoped_capture_declares_itself_and_never_answers_for_the_register():
    repository = MemoryRepository()
    lease = swarm_lease(repository)
    register = GovernmentCatalogIngestor(repository, lease, client=pinned_client()) \
        .ingest_resource(src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY))
    scoped = scoped_snapshot(repository, lease, committed_records(12))
    scope = CaptureScope.for_register_marque(TOYOTA)
    assert declared_scope(scoped) == scope
    assert scoped["retrieval_metadata"]["query"] == scope.query()

    # Newer, active and usable -- and still never the answer to an unscoped read.
    for reader in (lambda: resolve_active_snapshot(
                       repository, resource_id=src.WLTP_RESOURCE_ID, snapshot_key=None,
                       allow_incomplete=False),
                   lambda: repository.list_active_catalog_snapshots(
                       src.GOVERNMENT_SOURCE_FAMILY, resource_id=src.WLTP_RESOURCE_ID)[0]):
        assert reader()["snapshot_key"] == register.snapshot_key
    assert [row["snapshot_key"] for row in repository.list_active_catalog_snapshots(
        src.GOVERNMENT_SOURCE_FAMILY, resource_id=src.WLTP_RESOURCE_ID)] == [register.snapshot_key]
    # A run that is not batch-bound pins the register, exactly as before.
    assert prepare_government_work(repository).snapshot_key == register.snapshot_key

    # A SCOPED read answers only from that scope, pinned or not.
    assert resolve_active_snapshot(repository, resource_id=src.WLTP_RESOURCE_ID, snapshot_key=None,
                                   allow_incomplete=False, capture_scope=scope)["id"] == scoped["id"]
    assert [row["id"] for row in repository.list_active_catalog_snapshots(
        src.GOVERNMENT_SOURCE_FAMILY, resource_id=src.WLTP_RESOURCE_ID,
        capture_scope_key=scope.key())] == [scoped["id"]]
    other = CaptureScope.for_register_marque("מאזדה")
    with pytest.raises(GovernmentProjectionError) as refusal:
        resolve_active_snapshot(repository, resource_id=src.WLTP_RESOURCE_ID,
                                snapshot_key=scoped["snapshot_key"], allow_incomplete=False,
                                capture_scope=other)
    assert refusal.value.reason_code == "GOV_PROJECTION_SNAPSHOT_SCOPE_MISMATCH"
    with pytest.raises(GovernmentProjectionError) as refusal:
        resolve_active_snapshot(repository, resource_id=src.WLTP_RESOURCE_ID,
                                snapshot_key=register.snapshot_key, allow_incomplete=False,
                                capture_scope=scope)
    assert refusal.value.reason_code == "GOV_PROJECTION_SNAPSHOT_SCOPE_MISMATCH"
    # Naming a key is naming the snapshot.
    assert resolve_active_snapshot(repository, resource_id=src.WLTP_RESOURCE_ID,
                                   snapshot_key=scoped["snapshot_key"],
                                   allow_incomplete=False)["id"] == scoped["id"]


def test_with_only_scoped_snapshots_there_is_no_register_to_read():
    repository = MemoryRepository()
    scoped_snapshot(repository, swarm_lease(repository), committed_records(5))
    with pytest.raises(GovernmentProjectionError) as refusal:
        resolve_active_snapshot(repository, resource_id=src.WLTP_RESOURCE_ID, snapshot_key=None,
                                allow_incomplete=False)
    assert refusal.value.reason_code == "GOV_PROJECTION_NO_ACTIVE_SNAPSHOT"
    with pytest.raises(GovernmentPreparationError) as refusal:
        prepare_government_work(repository)
    assert refusal.value.code == "GOVERNMENT_SNAPSHOT_UNAVAILABLE"


def test_a_scoped_capture_sends_its_own_query_and_is_never_adopted_unscoped():
    repository = MemoryRepository()
    lease = swarm_lease(repository)
    scope = CaptureScope.for_register_marque(TOYOTA)
    client, transport = scoped_client(committed_records(5))
    with pytest.raises(GovernmentSourceError) as refusal:
        GovernmentCatalogIngestor(repository, lease, client=client).ingest_resource(
            src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY), capture_scope=scope)
    assert refusal.value.reason_code == "GOV_CAPTURE_SCOPE_MISMATCH"
    assert transport.calls == []  # refused before a request was sent

    scoped_snapshot(repository, lease, committed_records(5))
    client, transport = scoped_client(committed_records(5))
    # The same filtered content WITHOUT the declaration would replay onto the
    # scoped row -- and is refused rather than adopted as unscoped.
    with pytest.raises(GovernmentIngestionError) as refusal:
        GovernmentCatalogIngestor(repository, lease, client=client).ingest_resource(
            src.WLTP_RESOURCE_ID, query=scope.query())
    assert refusal.value.reason_code == "GOV_SNAPSHOT_SCOPE_MISMATCH"
    sent = [params for action, params in transport.calls if action == src.DATASTORE_SEARCH]
    assert sent and all(params.get("filters") == scope.filters_text() and "q" not in params
                        for params in sent)


# =============================================================================
# 3. refresh is query-aware
# =============================================================================

def test_a_scoped_refresh_is_never_unchanged_merely_because_the_register_is():
    repository = MemoryRepository()
    lease = swarm_lease(repository)
    register = GovernmentCatalogIngestor(repository, lease, client=pinned_client()) \
        .ingest_resource(src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY))
    scope = CaptureScope.for_register_marque(TOYOTA)
    client, transport = scoped_client(committed_records(12))
    first = GovernmentCatalogRefresh(repository, lease, client=client,
                                     capture_scope=scope).sync_if_changed()
    # The register snapshot is at the same upstream version, and the scoped
    # refresh still CAPTURED -- and compared against no other scope.
    assert first.changed and first.report is not None
    assert first.report.capture_scope_key == scope.key()
    assert first.diff is not None and first.diff.previous_snapshot_key == ""
    assert first.active_snapshot_key != register.snapshot_key
    # Now the scope's own snapshot is at the register's version: unchanged.
    client, transport = scoped_client(committed_records(12))
    again = GovernmentCatalogRefresh(repository, lease, client=client,
                                     capture_scope=scope).sync_if_changed()
    assert not again.changed and again.active_snapshot_key == first.active_snapshot_key
    assert [action for action, _params in transport.calls] == [src.PACKAGE_SHOW]
    with pytest.raises(GovernmentSourceError):
        GovernmentCatalogRefresh(repository, lease, client=client,
                                 capture_scope=scope).sync_if_changed(query=dict(PINNED_QUERY))


def test_the_register_refresh_never_compares_with_a_scoped_snapshot():
    repository = MemoryRepository()
    lease = swarm_lease(repository)
    scoped = scoped_snapshot(repository, lease, committed_records(12))
    transport = FixtureTransport(bodies={0: whole_page(committed_records(12))})
    client = DataGovClient(transport, page_limit=entrypoint.CAPTURE_PAGE_LIMIT,
                           sleep_fn=lambda _seconds: None)
    outcome = GovernmentCatalogRefresh(repository, lease, client=client).sync_if_changed()
    # Same upstream version as the scoped snapshot, and yet a capture: the
    # scoped snapshot is not the register, so it is not "what MILO holds".
    assert outcome.changed and outcome.report is not None
    assert outcome.report.capture_scope_key == ""
    assert outcome.diff is not None and outcome.diff.previous_snapshot_key == ""
    assert outcome.active_snapshot_key != scoped["snapshot_key"]


# =============================================================================
# 4. preparation, in the in-memory mirror of the database
# =============================================================================

def test_a_revision_is_prepared_into_a_deterministic_bounded_queue(capsys):
    repository = MemoryRepository()
    world = plan_world(repository)
    lease = capture_lease(repository, capsys)
    snapshot = scoped_snapshot(repository, lease, planned_records())
    summary = prepare(repository, lease, world, units(snapshot))
    assert summary["replayed"] is False
    preparation = summary["preparation"]
    assert (preparation["unit_count"], preparation["prepared_unit_count"],
            preparation["queued_item_count"], preparation["batch_count"]) == (2, 1, 23, 3)
    toyota, lexus = summary["units"]
    assert (toyota["state"], toyota["readable_count"], toyota["ambiguous_count"],
            toyota["eligible_count"], toyota["queued_count"]) == ("prepared", 23, 3, 23, 23)
    assert (lexus["state"], lexus["reason_code"]) == ("register_unverified",
                                                     "WORK_SCOPE_REGISTER_UNVERIFIED")
    assert [(b["batch_number"], b["item_count"], b["first_position"], b["unit_key"])
            for b in summary["batches"]] == [(1, 10, 1, "toyota"), (2, 10, 11, "toyota"),
                                             (3, 3, 21, "toyota")]
    items = sorted(repository.work_scope_queue_items, key=lambda row: row["position"])
    candidates = {row["id"]: row for row in repository.catalog_candidates.values()}
    assert [candidates[item["candidate_id"]]["commercial_model"] for item in items] == [
        f"MODEL-{index:02d}" for index in range(23)]
    assert [item["batch_position"] for item in items] == list(range(1, 11)) * 2 + [1, 2, 3]
    # Replay answers with what was stored; a different decision is refused.
    again = prepare(repository, lease, world, units(snapshot))
    assert again["replayed"] is True and again["preparation"]["id"] == preparation["id"]
    assert len(repository.work_scope_queue_items) == 23
    other = scoped_snapshot(repository, lease, committed_records(3, start=40))
    app_error("WORK_SCOPE_ALREADY_PREPARED", prepare, repository, lease, world, units(other))


def test_the_plan_limit_caps_the_queue(capsys):
    repository = MemoryRepository()
    world = plan_world(repository, max_items=12)
    lease = capture_lease(repository, capsys)
    summary = prepare(repository, lease, world,
                      units(scoped_snapshot(repository, lease, planned_records())))
    assert summary["preparation"]["queued_item_count"] == 12
    assert [batch["item_count"] for batch in summary["batches"]] == [10, 2]


def test_preparation_fails_closed_in_the_mirror(capsys):
    repository = MemoryRepository()
    world = plan_world(repository)
    lease = capture_lease(repository, capsys)
    snapshot = scoped_snapshot(repository, lease, planned_records())
    # Only an OPERATOR CAPTURE run prepares.
    app_error("WORK_SCOPE_PREPARATION_RUN_INVALID", prepare, repository,
              swarm_lease(repository), world, units(snapshot))
    app_error("WORK_SCOPE_STALE", prepare, repository, lease, world, units(snapshot),
              digest="0" * 64)
    app_error("WORK_SCOPE_PREPARATION_INVALID", prepare, repository, lease, world,
              list(reversed(units(snapshot))))
    mazda = scoped_snapshot(repository, lease, committed_records(3), marque="מאזדה")
    app_error("WORK_SCOPE_UNIT_SNAPSHOT_INVALID", prepare, repository, lease, world, units(mazda))
    register = GovernmentCatalogIngestor(repository, lease, client=pinned_client()) \
        .ingest_resource(src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY))
    unscoped = repository.find_active_catalog_snapshot(
        src.GOVERNMENT_SOURCE_FAMILY, src.WLTP_RESOURCE_ID, register.snapshot_key)
    app_error("WORK_SCOPE_UNIT_SNAPSHOT_INVALID", prepare, repository, lease, world,
              units(unscoped))
    # Once revised, the old revision is stale.
    revised = wsc.scope_from_fields({"units": ["toyota"], "model_year_from": None,
                                     "model_year_to": None, "max_items": 25, "batch_size": 10})
    repository.revise_work_scope(UUID(world["plan"]), 1, world["digest"], UUID(world["user"]),
                                 {"scope_text": revised.canonical_text(), "input_kind": "edit",
                                  "instruction": None, "notes": []})
    app_error("WORK_SCOPE_STALE", prepare, repository, lease, world, units(snapshot))


def test_a_mostly_ambiguous_manufacturer_is_stated_and_queues_nothing(capsys):
    """The vocabulary gap, on register rows: diesel rows read as `ambiguous`."""
    repository = MemoryRepository()
    world = plan_world(repository, units=("toyota",), model_year_from=None)
    lease = capture_lease(repository, capsys)
    rows = committed_records(5)
    for row in rows[:3]:
        row["delek_cd"], row["delek_nm"] = 2, "דיזל"
    summary = prepare(repository, lease, world,
                      units(scoped_snapshot(repository, lease, rows))[:1])
    unit = summary["units"][0]
    assert (unit["state"], unit["reason_code"], unit["readable_count"],
            unit["ambiguous_count"], unit["queued_count"]) == (
        "vocabulary_insufficient", "WORK_SCOPE_VOCABULARY_INSUFFICIENT", 2, 3, 0)
    assert summary["batches"] == [] and summary["preparation"]["prepared_unit_count"] == 0


# =============================================================================
# 5. the whole Government side, through the real capture job entrypoint
# =============================================================================

def execute_scoped(repository, transport, world, capsys, monkeypatch, *, digest=None,
                   env=None) -> tuple[int, dict[str, Any]]:
    monkeypatch.setattr(entrypoint, "_open_transport", lambda: transport)
    monkeypatch.setattr(entrypoint, "_open_repository", lambda: repository)
    conversation, user = seeded_conversation(repository)
    status = entrypoint.main(prepare_argv(conversation, user), env=capture_env())
    run_id = json.loads(capsys.readouterr().out)["preparation"]["run_id"]
    argv = authorized_argv(run_id, **{"--work-scope-id": world["plan"],
                                      "--work-scope-revision": "1",
                                      "--work-scope-digest": digest or world["digest"]})
    status = entrypoint.main(argv, env=env or capture_env(
        MILO_ENABLE_WORK_SCOPE_PREPARATION="true"))
    return status, json.loads(capsys.readouterr().out)


def test_the_capture_job_prepares_one_exact_plan_revision(capsys, monkeypatch):
    repository = MemoryRepository()
    world = plan_world(repository)
    transport = FixtureTransport(bodies={0: scoped_page(planned_records())})
    status, document = execute_scoped(repository, transport, world, capsys, monkeypatch)
    assert status == entrypoint.EXIT_OK, document
    prepared = document["work_scope"]
    assert (prepared["revision"], prepared["scope_digest"], prepared["replayed"]) == (
        1, world["digest"], False)
    assert (prepared["queued_item_count"], prepared["batch_count"]) == (23, 3)
    assert [(unit["unit_key"], unit["state"], unit["capture"]) for unit in prepared["units"]] == [
        ("toyota", "prepared", "landed"), ("lexus", "register_unverified", "none")]
    # ONE Government read path: package_show, then the scoped datastore pages.
    # The unverified marque was never queried, and nothing sent a `q`.
    searches = [params for action, params in transport.calls if action == src.DATASTORE_SEARCH]
    assert searches and all(params["filters"] == '{"tozar":"טויוטה"}' and "q" not in params
                            and params["limit"] == "1000" for params in searches)
    # No register text leaves in the sanitized report.
    assert TOYOTA not in json.dumps(document, ensure_ascii=False)
    # Re-running the same revision is a replay over an unchanged register.
    status, again = execute_scoped(repository, FixtureTransport(
        bodies={0: scoped_page(planned_records())}), world, capsys, monkeypatch)
    assert status == entrypoint.EXIT_OK and again["work_scope"]["replayed"] is True
    assert again["work_scope"]["units"][0]["capture"] == "unchanged"


def test_the_capture_job_never_prepares_a_stale_revision(capsys, monkeypatch):
    repository = MemoryRepository()
    world = plan_world(repository)
    transport = FixtureTransport(bodies={0: scoped_page(planned_records())})
    status, document = execute_scoped(repository, transport, world, capsys, monkeypatch,
                                      digest="0" * 64)
    assert status == entrypoint.EXIT_FAILED
    assert document["reason_code"] == "WORK_SCOPE_PREPARATION_STALE"
    assert repository.work_scope_preparations == {}
    # Refused before a single Government request.
    assert transport.calls == []


@pytest.mark.parametrize("overrides,reason", [
    ({"--work-scope-id": str(uuid4())}, "CAPTURE_WORK_SCOPE_ARGUMENTS_INVALID"),
    ({"--work-scope-id": "not-a-uuid", "--work-scope-revision": "1",
      "--work-scope-digest": "a" * 64}, "CAPTURE_WORK_SCOPE_ARGUMENTS_INVALID"),
    ({"--work-scope-id": str(uuid4()), "--work-scope-revision": "0",
      "--work-scope-digest": "a" * 64}, "CAPTURE_WORK_SCOPE_ARGUMENTS_INVALID"),
    ({"--work-scope-id": str(uuid4()), "--work-scope-revision": "01",
      "--work-scope-digest": "a" * 64}, "CAPTURE_WORK_SCOPE_ARGUMENTS_INVALID"),
    ({"--work-scope-id": str(uuid4()), "--work-scope-revision": "١",
      "--work-scope-digest": "a" * 64}, "CAPTURE_WORK_SCOPE_ARGUMENTS_INVALID"),
    ({"--work-scope-id": str(uuid4()), "--work-scope-revision": "1",
      "--work-scope-digest": "A" * 64}, "CAPTURE_WORK_SCOPE_ARGUMENTS_INVALID"),
    ({"--work-scope-id": str(uuid4()), "--work-scope-revision": "1",
      "--work-scope-digest": "a" * 64}, "CAPTURE_WORK_SCOPE_PREPARATION_DISABLED"),
])
def test_the_scoped_mode_refuses_before_any_side_effect(overrides, reason, capsys, monkeypatch):
    def tripwire() -> Any:
        raise AssertionError("a refused preparation constructed a seam")

    monkeypatch.setattr(entrypoint, "_open_transport", tripwire)
    monkeypatch.setattr(entrypoint, "_open_repository", tripwire)
    status = entrypoint.main(authorized_argv(uuid4(), **overrides), env=capture_env())
    document = json.loads(capsys.readouterr().out)
    assert (status, document["reason_code"]) == (entrypoint.EXIT_REFUSED, reason)


def test_the_scoped_arguments_belong_to_the_capture_only(capsys, monkeypatch):
    monkeypatch.setattr(entrypoint, "_open_repository",
                        lambda: (_ for _ in ()).throw(AssertionError("constructed")))
    scoped = {"--work-scope-id": str(uuid4()), "--work-scope-revision": "1",
              "--work-scope-digest": "a" * 64}
    for argv in (prepare_argv(uuid4(), uuid4(), **scoped),
                 ["--plan", *[token for pair in scoped.items() for token in pair]]):
        status = entrypoint.main(argv, env=capture_env(MILO_ENABLE_WORK_SCOPE_PREPARATION="true"))
        document = json.loads(capsys.readouterr().out)
        assert (status, document["reason_code"]) == (
            entrypoint.EXIT_REFUSED, "CAPTURE_ARGUMENT_NOT_VALID_IN_MODE")


# =============================================================================
# 6. a batch run is handed exactly its batch
# =============================================================================

def prepared_world(capsys) -> tuple[MemoryRepository, dict, dict]:
    repository = MemoryRepository()
    world = plan_world(repository)
    lease = capture_lease(repository, capsys)
    summary = prepare(repository, lease, world,
                      units(scoped_snapshot(repository, lease, planned_records())))
    return repository, world, summary


def test_a_bound_run_is_prepared_from_exactly_its_batch_and_resumes_it(capsys):
    repository, world, summary = prepared_world(capsys)
    second = summary["batches"][1]
    run = swarm_run(repository, world)
    repository.bind_work_scope_batch_run(UUID(second["id"]), UUID(run), 1, world["digest"],
                                         UUID(world["user"]))
    preparation = prepare_government_work(repository, run_id=run)
    assert preparation.snapshot_key == second["snapshot_key"]
    assert [item.commercial_model for item in preparation.queue] == [
        f"MODEL-{index:02d}" for index in range(10, 20)]
    assert (preparation.total_candidates, preparation.bounded) == (10, False)
    assert preparation.work_scope_batch == {
        "batch_id": second["id"], "work_scope_id": world["plan"], "revision": 1,
        "scope_digest": world["digest"], "batch_number": 2, "attempt": 1}
    artifact = preparation.as_artifact()
    assert artifact[BATCH_ARTIFACT_KEY] == preparation.work_scope_batch
    # A resumed attempt reads the SAME batch, in the SAME order.
    checkpoint = {"phase": "government_prepared", "artifacts": {"government": artifact}}
    resumed = prepare_government_work(repository, run_id=run, checkpoint=checkpoint)
    assert resumed.resumed and resumed.queue == preparation.queue
    assert resumed.work_scope_batch == preparation.work_scope_batch
    # The binding is the authority: another run cannot resume this record, and
    # an unbound record cannot resume on a bound run.
    other = swarm_run(repository, world)
    with pytest.raises(GovernmentPreparationError) as refusal:
        prepare_government_work(repository, run_id=other, checkpoint=checkpoint)
    assert refusal.value.code == "GOVERNMENT_PREPARATION_RECORD_INVALID"
    unbound = dict(artifact)
    unbound.pop(BATCH_ARTIFACT_KEY)
    with pytest.raises(GovernmentPreparationError) as refusal:
        prepare_government_work(repository, run_id=run,
                                checkpoint={"artifacts": {"government": unbound}})
    assert refusal.value.code == "GOVERNMENT_PREPARATION_RECORD_INVALID"


def test_an_unreadable_binding_fails_closed_rather_than_meaning_unbound():
    class Broken(MemoryRepository):
        answer: Any = None

        def work_scope_batch_for_run(self, run_id):
            if self.answer is None:
                raise AppError("REPOSITORY_ERROR", "down", 502)
            return self.answer

    repository = Broken()
    with pytest.raises(GovernmentPreparationError) as refusal:
        prepare_government_work(repository, run_id=uuid4())
    assert refusal.value.code == "GOVERNMENT_QUEUE_UNAVAILABLE"
    repository.answer = {"batch": "nope"}
    with pytest.raises(GovernmentPreparationError) as refusal:
        prepare_government_work(repository, run_id=uuid4())
    assert refusal.value.code == "GOVERNMENT_BATCH_INVALID"


def test_the_binding_mirror_allows_one_live_batch_per_plan_and_never_a_stale_one(capsys):
    repository, world, summary = prepared_world(capsys)
    first, second, third = (UUID(batch["id"]) for batch in summary["batches"])
    user, digest = UUID(world["user"]), world["digest"]
    run_a = UUID(swarm_run(repository, world))
    assert repository.bind_work_scope_batch_run(first, run_a, 1, digest, user)["attempt"] == 1
    assert repository.bind_work_scope_batch_run(first, run_a, 1, digest, user)["replayed"] is True
    run_b = UUID(swarm_run(repository, world))
    app_error("WORK_SCOPE_BATCH_IN_PROGRESS", repository.bind_work_scope_batch_run,
              second, run_b, 1, digest, user)
    app_error("WORK_SCOPE_BATCH_RUN_TAKEN", repository.bind_work_scope_batch_run,
              second, run_a, 1, digest, user)
    app_error("WORK_SCOPE_BATCH_NOT_FOUND", repository.bind_work_scope_batch_run,
              second, run_b, 1, digest, uuid4())
    app_error("WORK_SCOPE_STALE", repository.bind_work_scope_batch_run,
              second, run_b, 1, "0" * 64, user)
    repository.runs[str(run_a)]["status"] = "completed"
    assert repository.bind_work_scope_batch_run(second, run_b, 1, digest, user)["attempt"] == 1
    repository.runs[str(run_b)]["status"] = "failed"
    app_error("WORK_SCOPE_BATCH_ALREADY_COMPLETED", repository.bind_work_scope_batch_run,
              first, UUID(swarm_run(repository, world)), 1, digest, user)
    retry = UUID(swarm_run(repository, world))
    assert repository.bind_work_scope_batch_run(second, retry, 1, digest, user)["attempt"] == 2
    repository.runs[str(retry)]["status"] = "cancelled"
    revised = wsc.scope_from_fields({"units": ["toyota"], "model_year_from": None,
                                     "model_year_to": None, "max_items": 25, "batch_size": 10})
    repository.revise_work_scope(UUID(world["plan"]), 1, digest, user,
                                 {"scope_text": revised.canonical_text(), "input_kind": "edit",
                                  "instruction": None, "notes": []})
    app_error("WORK_SCOPE_STALE", repository.bind_work_scope_batch_run,
              third, UUID(swarm_run(repository, world)), 1, digest, user)


# =============================================================================
# 7. the Supabase repository calls exactly the reviewed RPCs
# =============================================================================

class _RpcFailure:
    def __init__(self, message: str) -> None:
        self.message = message

    def execute(self):
        raise RuntimeError(self.message)


def _supabase(result=None, error: str | None = None):
    from backend.repository.supabase import SupabaseRepository
    from tests.test_repository_supabase import FakeClient, FakeResult

    client = FakeClient()

    def rpc(name, params):
        client.rpc_calls.append((name, params))
        if error is not None:
            return _RpcFailure(error)
        return type("Rpc", (), {"execute": lambda self: FakeResult(result)})()

    client.rpc = rpc
    repository = SupabaseRepository.__new__(SupabaseRepository)
    repository.client = client
    return repository


def test_the_supabase_preparation_rpcs_send_exactly_their_parameters():
    repo = _supabase(result={"preparation": {"id": "p"}, "units": [], "batches": []})
    run, plan = uuid4(), str(uuid4())
    body = {"work_scope_id": plan, "revision": 1, "scope_digest": "a" * 64, "units": []}
    assert repo.prepare_work_scope_queue(run, body, worker_id="w", attempt=2,
                                         lease_token="t")["preparation"] == {"id": "p"}
    assert repo.client.rpc_calls[-1] == ("prepare_work_scope_queue", {
        "p_run_id": str(run), "p_worker_id": "w", "p_attempt": 2, "p_lease_token": "t",
        "p_preparation": body})
    batch, bound_by = uuid4(), uuid4()
    repo = _supabase(result={"id": "b", "attempt": 1})
    repo.bind_work_scope_batch_run(batch, run, 3, "b" * 64, bound_by)
    assert repo.client.rpc_calls[-1] == ("bind_work_scope_batch_run", {
        "p_batch_id": str(batch), "p_run_id": str(run), "p_expected_revision": 3,
        "p_expected_digest": "b" * 64, "p_bound_by": str(bound_by)})
    assert _supabase(result=None).work_scope_batch_for_run(run) is None
    repo = _supabase(result={"binding": {}, "batch": {"id": "x"}, "items": []})
    assert repo.work_scope_batch_for_run(run)["batch"] == {"id": "x"}
    assert repo.client.rpc_calls[-1] == ("work_scope_batch_for_run", {"p_run_id": str(run)})


@pytest.mark.parametrize("message,code", [
    ("WORK_SCOPE_STALE", "WORK_SCOPE_STALE"),
    ("WORK_SCOPE_ALREADY_PREPARED", "WORK_SCOPE_ALREADY_PREPARED"),
    ("WORK_SCOPE_PREPARATION_RUN_INVALID", "WORK_SCOPE_PREPARATION_RUN_INVALID"),
    ("WORK_SCOPE_PREPARATION_INVALID", "WORK_SCOPE_PREPARATION_INVALID"),
    ("WORK_SCOPE_UNIT_SNAPSHOT_INVALID", "WORK_SCOPE_UNIT_SNAPSHOT_INVALID"),
    ("WORK_SCOPE_BATCH_IN_PROGRESS", "WORK_SCOPE_BATCH_IN_PROGRESS"),
    ("WORK_SCOPE_BATCH_ALREADY_COMPLETED", "WORK_SCOPE_BATCH_ALREADY_COMPLETED"),
    ("WORK_SCOPE_BATCH_RUN_TAKEN", "WORK_SCOPE_BATCH_RUN_TAKEN"),
    ("WORK_SCOPE_BATCH_RUN_INVALID", "WORK_SCOPE_BATCH_RUN_INVALID"),
    ("WORK_SCOPE_BATCH_NOT_FOUND", "WORK_SCOPE_BATCH_NOT_FOUND"),
    ("WORK_SCOPE_NOT_FOUND", "WORK_SCOPE_NOT_FOUND"),
    ("relation catalog_x does not exist at 'secret value'", "REPOSITORY_ERROR"),
])
def test_the_supabase_preparation_refusals_are_static_codes(message, code):
    repo = _supabase(error=message)
    error = app_error(code, repo.bind_work_scope_batch_run, uuid4(), uuid4(), 1, "a" * 64,
                      uuid4())
    assert "secret" not in error.message


def test_the_scoped_listing_is_a_database_predicate():
    from tests.test_repository_supabase import FakeClient, FakeQuery

    from backend.repository.supabase import SupabaseRepository

    repository = SupabaseRepository.__new__(SupabaseRepository)
    repository.client = FakeClient()
    queries: list[FakeQuery] = []
    original = repository.client.table

    def table(name):
        query = original(name)
        queries.append(query)
        return query

    repository.client.table = table
    repository.list_active_catalog_snapshots("government", resource_id=src.WLTP_RESOURCE_ID)
    repository.list_active_catalog_snapshots("government", resource_id=src.WLTP_RESOURCE_ID,
                                             capture_scope_key="c" * 64)
    unscoped, scoped = queries
    assert ("is", "retrieval_metadata->capture_scope", "null") in unscoped.filters
    assert ("eq", "retrieval_metadata->capture_scope->>scope_key", "c" * 64) in scoped.filters
    assert not any(f[1] == "retrieval_metadata->capture_scope" for f in scoped.filters)


# =============================================================================
# 8. the capture job keeps its module when it overrides its arguments
# =============================================================================

def test_the_capture_job_execution_keeps_the_entrypoint_module():
    """`gcloud run jobs execute --args` REPLACES the container's args.

    The job is defined as `python -m backend.catalog.operator_capture`, so an
    execution that overrides `--args` with only the entrypoint's own arguments
    runs `python --prepare ...` -- which is not the capture at all. Every
    execution therefore restates the module first.
    """
    script = (REPO / "scripts/catalog/government-production-capture.sh").read_text(encoding="utf-8")
    execute = script.split("execute_job() {")[1].split("\n}\n")[0]
    assert '--args "-m,${MILO_CAPTURE_ENTRYPOINT_MODULE},${args_csv}"' in execute
    assert '--args "$args_csv"' not in execute
    # The execution name is gcloud's STDOUT alone: stderr is never folded into
    # it, and no line is picked out of mixed output.
    assert "2>&1" not in execute and "tail" not in execute


#: What the mock `gcloud logging read` answers unless a test says otherwise.
SUCCEEDED_DOCUMENT = {"entrypoint": "catalog.government.capture", "status": "succeeded",
                      "reason_code": "", "reason": "",
                      "work_scope": {"queued_item_count": 23, "batch_count": 3}}
#: The advisory real gcloud prints to STDERR after an execution, which must
#: never become the execution name.
GCLOUD_ADVISORY = ("Or visit https://console.cloud.google.com/run/jobs/executions/details/"
                   "us-central1/test-capture-execution-1?project=test-project")


def _capture_script(tmp_path: Path, *extra: str, gcloud: bool = False,
                    mode: str = "--prepare-work-scope",
                    execution_stdout: str = "test-capture-execution-1\n",
                    execution_exit: int = 0, document: dict | None = None,
                    psql: str | None = None, env: dict[str, str] | None = None):
    """Run the capture script as an operator would, with stand-ins on PATH.

    `gcloud` puts the mock gcloud first on PATH. `psql`, when given, is the
    source of a `psql` stand-in placed beside it -- a mock, or a wrapper that
    logs its call and runs the real client -- which records each call in
    `psql.log`. `env` adds variables, such as the one the operator config names
    for the read-only database URL.
    """
    import os
    import subprocess

    from tests.test_production_operator_bundle import _fake_config

    overrides = env or {}
    env = dict(os.environ)
    env["PATH"] = "/usr/bin:/bin"
    env["MILO_OPERATOR_CONFIG"] = ""
    bin_dir = tmp_path / "bin"
    if gcloud or psql is not None:
        bin_dir.mkdir(exist_ok=True)
        env["PATH"] = f"{bin_dir}:/usr/bin:/bin"
    if psql is not None:
        stand_in = bin_dir / "psql"
        stand_in.write_text(psql, encoding="utf-8")
        stand_in.chmod(0o755)
        env["MOCK_PSQL_LOG"] = str(tmp_path / "psql.log")
    if gcloud:
        log = tmp_path / "gcloud.log"
        mock = bin_dir / "gcloud"
        # `run jobs execute` prints its machine-readable answer to STDOUT and
        # then, like real gcloud, advisory text to STDERR -- flushed in that
        # order, so output that mixes the two streams ends with the advisory.
        mock.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "with open(os.environ['MOCK_GCLOUD_LOG'], 'a') as handle:\n"
            "    handle.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "if sys.argv[1:4] == ['run', 'jobs', 'execute']:\n"
            "    sys.stdout.write(os.environ['MOCK_GCLOUD_EXECUTION_STDOUT'])\n"
            "    sys.stdout.flush()\n"
            "    sys.stderr.write(os.environ['MOCK_GCLOUD_ADVISORY'] + '\\n')\n"
            "    sys.stderr.flush()\n"
            "    sys.exit(int(os.environ['MOCK_GCLOUD_EXECUTION_EXIT']))\n"
            "elif sys.argv[1:3] == ['logging', 'read']:\n"
            "    print(os.environ['MOCK_GCLOUD_DOCUMENT'])\n",
            encoding="utf-8")
        mock.chmod(0o755)
        env["MOCK_GCLOUD_LOG"] = str(log)
        env["MOCK_GCLOUD_EXECUTION_STDOUT"] = execution_stdout
        env["MOCK_GCLOUD_EXECUTION_EXIT"] = str(execution_exit)
        env["MOCK_GCLOUD_ADVISORY"] = GCLOUD_ADVISORY
        env["MOCK_GCLOUD_DOCUMENT"] = json.dumps(document or SUCCEEDED_DOCUMENT)
    env.update(overrides)
    result = subprocess.run(
        ["bash", str(REPO / "scripts/catalog/government-production-capture.sh"),
         mode, "--operator-config", str(_fake_config(tmp_path)), *extra],
        capture_output=True, text=True, check=False, cwd=REPO, env=env)
    return result, (tmp_path / "gcloud.log") if gcloud else None


SCOPED_VALUES = ("--run-id", "00000000-0000-4000-8000-000000000010",
                 "--work-scope-id", "00000000-0000-4000-8000-000000000020",
                 "--work-scope-revision", "3", "--work-scope-digest", "d" * 64)


@pytest.mark.parametrize("extra,message", [
    ((), "--enable-catalog-execution is required"),
    (("--enable-catalog-execution",), "requires --run-id"),
    (("--enable-catalog-execution", *SCOPED_VALUES[:2]), "--work-scope-id must be"),
    (("--enable-catalog-execution", *SCOPED_VALUES[:6], "--work-scope-digest", "D" * 64),
     "--work-scope-digest must be"),
    (("--enable-catalog-execution", *SCOPED_VALUES[:4], "--work-scope-revision", "0",
      "--work-scope-digest", "d" * 64), "--work-scope-revision must be"),
    (("--enable-catalog-execution", *SCOPED_VALUES), "--enable-work-scope-preparation is required"),
])
def test_the_scoped_capture_mode_refuses_before_any_cloud_call(tmp_path, extra, message):
    result, _log = _capture_script(tmp_path, *extra)
    assert result.returncode != 0
    assert message in result.stderr, result.stderr


def test_the_scoped_capture_mode_runs_the_entrypoint_with_one_execution_override(tmp_path):
    result, log = _capture_script(tmp_path, "--enable-catalog-execution",
                                  "--enable-work-scope-preparation", *SCOPED_VALUES, gcloud=True)
    assert result.returncode == 0, result.stderr
    assert "WORK_SCOPE_PREPARATION_STATUS=succeeded" in result.stdout
    assert "WORK_SCOPE_QUEUED_ITEMS=23" in result.stdout
    assert "WORK_SCOPE_BATCHES=3" in result.stdout
    # The execution is EXACTLY the name gcloud printed on stdout: the advisory
    # it printed to stderr afterwards reached the operator, never the name.
    executions = [line for line in result.stdout.splitlines() if line.startswith("Execution: ")]
    assert executions == ["Execution: test-capture-execution-1"]
    assert GCLOUD_ADVISORY in result.stderr
    assert "Or visit" not in result.stdout
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    # Cloud Logging is read for that exact execution, and only for it.
    reads = [call for call in calls if call[:2] == ["logging", "read"]]
    assert [call[2] for call in reads] == [
        'resource.type=cloud_run_job AND '
        'labels."run.googleapis.com/execution_name"=test-capture-execution-1']
    execute = next(call for call in calls if call[:3] == ["run", "jobs", "execute"])
    args = execute[execute.index("--args") + 1].split(",")
    # The module FIRST, because --args replaces the job's own container args.
    assert args[:2] == ["-m", "backend.catalog.operator_capture"]
    assert args[2] == "--execute"
    for flag, value in zip(SCOPED_VALUES[2::2], SCOPED_VALUES[3::2]):
        assert args[args.index(flag) + 1] == value
    assert args[args.index("--run-id") + 1] == SCOPED_VALUES[1]
    # The switch is turned on for THIS execution only; the job keeps it off.
    assert execute[execute.index("--update-env-vars") + 1] == \
        "MILO_ENABLE_WORK_SCOPE_PREPARATION=true"
    assert not any(call[:3] == ["run", "jobs", "update"] or call[:3] == ["run", "jobs", "create"]
                   for call in calls)


@pytest.mark.parametrize("execution_stdout,execution_exit", [
    ("", 1),                                    # a failed execution: nothing on stdout
    ("", 0),                                    # nothing at all
    (GCLOUD_ADVISORY + "\n", 0),                 # prose where the name belongs
    ("test-capture-execution-1\ntest-capture-execution-2\n", 0),  # two names
    ("Test-Capture-Execution-1\n", 0),           # not a Cloud Run execution name
    ("test-capture-execution-1 \n", 0),          # trailing text on the line
])
def test_the_scoped_capture_mode_fails_closed_without_one_exact_execution_name(
        tmp_path, execution_stdout, execution_exit):
    result, log = _capture_script(tmp_path, "--enable-catalog-execution",
                                  "--enable-work-scope-preparation", *SCOPED_VALUES, gcloud=True,
                                  execution_stdout=execution_stdout,
                                  execution_exit=execution_exit)
    assert result.returncode != 0
    assert "printed no single well-formed execution name on stdout" in result.stderr
    assert "WORK_SCOPE_PREPARATION_STATUS" not in result.stdout
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [call[:3] for call in calls] == [["run", "jobs", "execute"]]
    # No log is read for an execution that cannot be named exactly.
    assert not any(call[:2] == ["logging", "read"] for call in calls)


def test_a_named_execution_that_gcloud_reports_failed_is_judged_by_its_own_document(tmp_path):
    refused = {"entrypoint": "catalog.government.capture", "status": "refused",
               "reason_code": "WORK_SCOPE_STALE", "reason": "the plan changed"}
    result, log = _capture_script(tmp_path, "--enable-catalog-execution",
                                  "--enable-work-scope-preparation", *SCOPED_VALUES, gcloud=True,
                                  execution_exit=1, document=refused)
    assert result.returncode != 0
    assert "WARN: gcloud run jobs execute exited 1 for execution test-capture-execution-1" \
        in result.stderr
    assert "WORK_SCOPE_PREPARATION_STATUS=refused" in result.stdout
    assert "work-scope preparation did not succeed" in result.stderr
    assert "WORK_SCOPE_STALE" in result.stderr
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    reads = [call for call in calls if call[:2] == ["logging", "read"]]
    assert [call[2] for call in reads] == [
        'resource.type=cloud_run_job AND '
        'labels."run.googleapis.com/execution_name"=test-capture-execution-1']


# =============================================================================
# 9. a whole capture: the entrypoint's own success, and exactly its snapshot
# =============================================================================

#: The resource the capture job is pinned to (scripts/deploy/deployment-contract.sh).
WHOLE_RESOURCE = "142afde2-6228-49f9-8a29-9b6c3a0cbe40"
#: A snapshot key in the database's own shape.
CAPTURED_KEY = "cs1." + "a" * 32
#: The whole capture's own arguments: the master switch and a prepared run.
CAPTURE_RUN = ("--enable-catalog-execution", "--run-id", "00000000-0000-4000-8000-000000000030")
#: The variable the fake operator config names for the read-only database URL.
DB_URL_ENV = "MILO_TEST_DB_URL_ABSENT"

#: A `psql` stand-in: it records each call and answers ONE row per snapshot
#: key from MOCK_PSQL_ROWS -- the exact lookup the wrapper must make.
MOCK_PSQL = (
    "#!/usr/bin/env python3\n"
    "import json, os, sys\n"
    "sql = sys.stdin.read()\n"
    "with open(os.environ['MOCK_PSQL_LOG'], 'a') as handle:\n"
    "    handle.write(json.dumps({'argv': sys.argv[1:], 'sql': sql}) + '\\n')\n"
    "if os.environ.get('MOCK_PSQL_EXIT'):\n"
    "    sys.exit(int(os.environ['MOCK_PSQL_EXIT']))\n"
    "keys = [arg.split('=', 1)[1] for arg in sys.argv[1:] if arg.startswith('snapshot_key=')]\n"
    "row = json.loads(os.environ.get('MOCK_PSQL_ROWS', '{}')).get(keys[0] if keys else '')\n"
    "if row is not None:\n"
    "    print('|'.join(str(field) for field in row))\n")


def _captured(key: str | None = CAPTURED_KEY, *, status: str = "succeeded") -> dict:
    """A whole capture's document, as `operator_capture` writes it on success."""
    capture = {"outcome": "changed", "resource_id": WHOLE_RESOURCE, "upstream_version": "1",
               "upstream_version_kind": "dataset_version", "no_op": False,
               "diff_unavailable": False, "research_required": False}
    if key is not None:
        capture["active_snapshot_key"] = key
    return {"entrypoint": "catalog.government.capture", "status": status, "reason_code": "",
            "reason": "", "capture": capture}


def _snapshot_row(key: str = CAPTURED_KEY, *, answered: str | None = None,
                  resource: str = WHOLE_RESOURCE, state: str = "complete", active: str = "t",
                  scoped: str = "f", declared: int = 40, stored: int = 40, raws: int = 40,
                  candidates: int = 40) -> list:
    """One `catalog_source_snapshots` row, in the order the wrapper selects it."""
    return ["00000000-0000-4000-8000-0000000000aa", answered or key, resource, state, active,
            scoped, declared, stored, raws, candidates]


def _whole_capture(tmp_path: Path, document: dict, *, rows: dict | None = None,
                   database: bool = True, **env: str):
    extra_env = {"MOCK_PSQL_ROWS": json.dumps(rows or {}), **env}
    if database:
        extra_env[DB_URL_ENV] = "postgresql://verification.invalid/milo"
    result, log = _capture_script(tmp_path, *CAPTURE_RUN, gcloud=True, mode="--capture",
                                  document=document, psql=MOCK_PSQL, env=extra_env)
    psql_log = tmp_path / "psql.log"
    psql_calls = ([json.loads(line) for line in psql_log.read_text(encoding="utf-8").splitlines()]
                  if psql_log.exists() else [])
    return result, log, psql_calls


def test_a_successful_whole_capture_is_accepted_and_verifies_exactly_its_snapshot(tmp_path):
    result, log, psql_calls = _whole_capture(
        tmp_path, _captured(), rows={CAPTURED_KEY: _snapshot_row()})
    assert result.returncode == 0, result.stderr
    # `succeeded` is the entrypoint's own word for a capture that did its work.
    assert "CAPTURE_STATUS=succeeded" in result.stdout
    assert f"CAPTURED_SNAPSHOT_KEY={CAPTURED_KEY}" in result.stdout
    assert f"GOVERNMENT_SNAPSHOT_KEY={CAPTURED_KEY}" in result.stdout
    assert "USABLE_GOVERNMENT_SNAPSHOT=YES" in result.stdout
    # The ONE snapshot the document named, by key -- never "the newest".
    assert len(psql_calls) == 1
    assert f"snapshot_key={CAPTURED_KEY}" in psql_calls[0]["argv"]
    assert "s.snapshot_key = :'snapshot_key'" in psql_calls[0]["sql"]
    assert "order by" not in psql_calls[0]["sql"] and "limit" not in psql_calls[0]["sql"]
    # The execution name is still gcloud's stdout alone, and the module still
    # leads the execution's arguments.
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    execute = next(call for call in calls if call[:3] == ["run", "jobs", "execute"])
    assert execute[execute.index("--args") + 1].split(",")[:3] == [
        "-m", "backend.catalog.operator_capture", "--execute"]
    assert [call[2] for call in calls if call[:2] == ["logging", "read"]] == [
        'resource.type=cloud_run_job AND '
        'labels."run.googleapis.com/execution_name"=test-capture-execution-1']


def test_without_a_database_the_exact_snapshot_is_named_for_manual_verification(tmp_path):
    result, _log, psql_calls = _whole_capture(tmp_path, _captured(), database=False)
    assert result.returncode == 0, result.stderr
    assert f"verify snapshot {CAPTURED_KEY} here" in result.stdout
    assert "USABLE_GOVERNMENT_SNAPSHOT" not in result.stdout
    assert psql_calls == []


@pytest.mark.parametrize("status", ["refused", "failed", "captured", "completed", ""])
def test_a_capture_that_did_not_succeed_is_refused(tmp_path, status):
    result, _log, psql_calls = _whole_capture(
        tmp_path, _captured(status=status), rows={CAPTURED_KEY: _snapshot_row()})
    assert result.returncode != 0
    assert "capture did not succeed" in result.stderr
    assert "CAPTURED_SNAPSHOT_KEY" not in result.stdout
    assert psql_calls == []


@pytest.mark.parametrize("key", [
    None,                        # the document names no snapshot
    "",                          # names an empty one
    "cs1." + "A" * 32,           # not the database's key shape
    "cs2." + "a" * 32,
    "cs1." + "a" * 31,
    CAPTURED_KEY + " ",          # trailing text on the key
    "latest",
])
def test_a_successful_capture_without_a_valid_snapshot_key_fails_closed(tmp_path, key):
    result, _log, psql_calls = _whole_capture(
        tmp_path, _captured(key), rows={CAPTURED_KEY: _snapshot_row()})
    assert result.returncode != 0
    assert "names no valid capture.active_snapshot_key" in result.stderr
    assert "USABLE_GOVERNMENT_SNAPSHOT=YES" not in result.stdout
    # Nothing is looked up in its place.
    assert psql_calls == []


def test_a_document_without_a_capture_section_fails_closed(tmp_path):
    document = _captured()
    del document["capture"]
    result, _log, psql_calls = _whole_capture(tmp_path, document)
    assert result.returncode != 0
    assert "names no valid capture.active_snapshot_key" in result.stderr
    assert psql_calls == []


@pytest.mark.parametrize("row,message", [
    (None, f"the captured snapshot {CAPTURED_KEY} does not exist"),
    (_snapshot_row(scoped="t"), "is a scoped manufacturer capture, not the whole register"),
    (_snapshot_row(resource="another-resource"), "belongs to resource another-resource"),
    (_snapshot_row(state="pending"), "is not complete (validation_state=pending)"),
    (_snapshot_row(state="failed"), "is not complete (validation_state=failed)"),
    (_snapshot_row(active="f"), "is not active"),
    (_snapshot_row(stored=39), "stored (39) != declared (40)"),
    (_snapshot_row(candidates=0), "no candidate variants"),
    (_snapshot_row(answered="cs1." + "b" * 32), "the database answered for"),
])
def test_the_captured_snapshot_itself_must_be_usable_or_the_capture_fails(tmp_path, row, message):
    rows = {} if row is None else {CAPTURED_KEY: row}
    result, _log, psql_calls = _whole_capture(tmp_path, _captured(), rows=rows)
    assert result.returncode != 0
    assert message in result.stderr, result.stderr
    assert "USABLE_GOVERNMENT_SNAPSHOT=YES" not in result.stdout
    assert [f"snapshot_key={CAPTURED_KEY}" in call["argv"] for call in psql_calls] == [True]


def test_an_unreadable_database_fails_the_verification(tmp_path):
    result, _log, psql_calls = _whole_capture(
        tmp_path, _captured(), rows={CAPTURED_KEY: _snapshot_row()}, MOCK_PSQL_EXIT="2")
    assert result.returncode != 0
    assert f"snapshot {CAPTURED_KEY} could not be read from the database" in result.stderr
    assert "USABLE_GOVERNMENT_SNAPSHOT=YES" not in result.stdout
    assert len(psql_calls) == 1


def test_the_capture_job_definition_pins_scoped_preparation_off():
    contract = (REPO / "scripts/deploy/deployment-contract.sh").read_text(encoding="utf-8")
    pinned = contract.split("MILO_CAPTURE_PINNED_OFF_FLAGS=(")[1].split(")")[0]
    assert "MILO_ENABLE_WORK_SCOPE_PREPARATION=false" in pinned
    assert 'MILO_WORK_SCOPE_PREPARATION_FLAG_NAME="MILO_ENABLE_WORK_SCOPE_PREPARATION"' in contract
    # Named once, as the entrypoint reads it.
    assert entrypoint.WORK_SCOPE_PREPARATION_FLAG == "MILO_ENABLE_WORK_SCOPE_PREPARATION"

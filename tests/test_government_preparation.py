"""Government preparation: after the lease, before the provider.

A website-triggered catalog-processing run (a trusted `swarm_v2` run in a
deployment whose posture allows the Government read) must proceed in exactly
this order and no other:

    claim (run + worker + attempt + lease)
      -> resolve and PIN one usable immutable Government snapshot
      -> select a deterministic, bounded, resumable work queue from the
         PERSISTED candidate state of that snapshot
      -> only then construct or reach any paid provider path

These tests prove each arrow from the outside: the queue is read from durable
candidate state in the repository's deterministic order; a resumed attempt of
the SAME run pins the SAME snapshot by exact key and walks the SAME queue;
per-item progress is reconstructed from durable evidence and events; and the
real worker, with the read enabled and no usable snapshot, is refused through
the canonical finalizer with no provider path ever constructed.

The persistence is the existing `run_checkpoints` authority -- no new event
type was registered, so the event-registry fingerprint bound into every
RunIdentity is unchanged by this stage.
"""

from __future__ import annotations

import json
import socket
from uuid import UUID, uuid4

import pytest

import backend.worker.main as worker_main
from backend.catalog.execution import (CATALOG_EXECUTION_FLAG, CATALOG_PROMOTION_FLAG,
                                       GOVERNMENT_READ_FLAG)
from backend.catalog.government import preparation as prep
from backend.catalog.government.preparation import (ARTIFACT_KEY, ARTIFACT_SCHEMA,
                                                    GOVERNMENT_WORK_QUEUE_LIMIT,
                                                    PREPARATION_PHASE, PROGRESS_EVIDENCED,
                                                    PROGRESS_PENDING, PROGRESS_PROMOTED,
                                                    GovernmentPreparationError,
                                                    government_work_progress,
                                                    prepare_government_work)
from backend.errors import AppError
from backend.event_registry import fingerprint as event_registry_fingerprint
from backend.run_identity import RunIdentity
from backend.testing.catalog_review_seed import land_pinned_government_snapshot
from backend.testing.memory_repository import MemoryRepository, _variant_page_key
from tests.run_factory import identity_kwargs
from test_swarm_v2_smoke_offline import (PROJECT, USER, FakeKimiCompletions, build_repo,
                                         run_worker_directly, swarm_env)

#: The registry fingerprint this branch shipped with. The preparation stage
#: persists through `run_checkpoints`, so it must not have moved.
REGISTRY_FINGERPRINT_BEFORE_PREPARATION = event_registry_fingerprint()


# =============================================================================
# helpers
# =============================================================================

def seeded() -> tuple[MemoryRepository, str, str]:
    repository = MemoryRepository()
    user, project = str(uuid4()), str(uuid4())
    repository.seed_user(user)
    repository.seed_project(project, "gov-prep", "Gov prep", [user], workflow_key="swarm_v2")
    return repository, user, project


def landed() -> tuple[MemoryRepository, str, str, str]:
    repository, user, project = seeded()
    key = land_pinned_government_snapshot(repository, user_id=user, project_id=project)
    return repository, user, project, key


def queued_candidates(repository: MemoryRepository, snapshot_id: str) -> list[dict]:
    rows = [row for row in repository.catalog_candidates.values()
            if row["snapshot_id"] == snapshot_id and row["status"] == prep.QUEUED_CANDIDATE_STATUS]
    return sorted(rows, key=_variant_page_key)


def events_of(repository: MemoryRepository, run_id) -> list[dict]:
    return [row for row in repository.run_events if str(row["run_id"]) == str(run_id)]


def event_types(repository: MemoryRepository, run_id) -> list[str]:
    return [row["event_type"] for row in events_of(repository, run_id)]


def government_env(monkeypatch, **overrides) -> None:
    swarm_env(monkeypatch, **{CATALOG_EXECUTION_FLAG: "true", GOVERNMENT_READ_FLAG: "true",
                              CATALOG_PROMOTION_FLAG: "false", **overrides})


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """No test here may open a socket: the product worker never imports."""
    def refuse(*_args, **_kwargs):
        raise AssertionError("a Government preparation test attempted a network connection")
    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture
def no_transport(monkeypatch):
    """Constructing the upstream transport anywhere is a test failure."""
    import backend.catalog.government.transport as transport_module

    def refuse(*_args, **_kwargs):
        raise AssertionError("the product worker constructed a data.gov.il transport")
    monkeypatch.setattr(transport_module, "HttpsDataGovTransport", refuse)


# =============================================================================
# 1. the persistence choice
# =============================================================================

def test_preparation_persists_through_checkpoints_and_registers_no_event_type():
    """The pin and the queue live in `run_checkpoints`; the registry is untouched."""
    from backend import event_registry

    assert event_registry_fingerprint() == REGISTRY_FINGERPRINT_BEFORE_PREPARATION
    assert PREPARATION_PHASE not in event_registry.EVENT_TYPES
    assert "government_prepared" not in event_registry.EVENT_TYPES
    assert not any(name.startswith("government_") for name in event_registry.EVENT_TYPES)


# =============================================================================
# 2. the queue: deterministic, bounded, from persisted candidate state
# =============================================================================

def test_no_usable_snapshot_is_a_static_refusal():
    repository, *_ = seeded()
    with pytest.raises(GovernmentPreparationError) as raised:
        prepare_government_work(repository)
    assert raised.value.code == "GOVERNMENT_SNAPSHOT_UNAVAILABLE"
    assert raised.value.reason_code == "GOV_PROJECTION_NO_ACTIVE_SNAPSHOT"
    assert raised.value.safe_message == prep.PREPARATION_REASONS["GOVERNMENT_SNAPSHOT_UNAVAILABLE"]


def test_the_queue_is_deterministic_bounded_and_read_from_persisted_candidate_state():
    repository, _user, _project, key = landed()
    first = prepare_government_work(repository, limit=5)
    second = prepare_government_work(repository, limit=5)
    assert first == second
    assert first.resumed is False
    assert first.snapshot_key == key
    expected = queued_candidates(repository, first.snapshot_id)
    assert [item.candidate_key for item in first.queue] == \
        [row["candidate_key"] for row in expected[:5]]
    assert len(first.queue) == min(5, len(expected))
    assert first.total_candidates == len(expected)
    assert first.bounded is (len(expected) > 5)
    # Every item is an UNREAD reading of the pinned snapshot.
    by_id = {row["id"]: row for row in expected}
    for item in first.queue:
        assert by_id[item.candidate_id]["status"] == prep.QUEUED_CANDIDATE_STATUS
        assert item.manufacturer and item.commercial_model


def test_the_default_bound_is_the_promotion_bound():
    repository, *_ = landed()
    prepared = prepare_government_work(repository)
    assert GOVERNMENT_WORK_QUEUE_LIMIT == 25
    assert len(prepared.queue) <= GOVERNMENT_WORK_QUEUE_LIMIT
    # Asking for more never widens it.
    wider = prepare_government_work(repository, limit=10_000)
    assert len(wider.queue) <= GOVERNMENT_WORK_QUEUE_LIMIT
    assert wider.queue == prepared.queue


def test_a_queue_read_failure_is_a_refusal_never_an_empty_queue():
    repository, *_ = landed()

    class Failing(MemoryRepository):
        pass
    failing = Failing()
    failing.__dict__.update(repository.__dict__)

    def broken(*_args, **_kwargs):
        raise AppError("REPOSITORY_ERROR", "connection reset by peer", 502)
    failing.catalog_candidate_variant_page = broken  # type: ignore[method-assign]
    with pytest.raises(GovernmentPreparationError) as raised:
        prepare_government_work(failing)
    assert raised.value.code == "GOVERNMENT_QUEUE_UNAVAILABLE"
    assert "connection reset" not in str(raised.value)


# =============================================================================
# 3. resume: same run, same snapshot, same queue
# =============================================================================

def test_a_resumed_attempt_reuses_the_pinned_snapshot_and_the_same_queue():
    repository, *_ , key = landed()
    first = prepare_government_work(repository, limit=4)
    # The preparation record itself...
    preparation_checkpoint = {"phase": PREPARATION_PHASE,
                              "artifacts": {ARTIFACT_KEY: first.as_artifact()}}
    resumed = prepare_government_work(repository, checkpoint=preparation_checkpoint)
    assert resumed.resumed is True
    assert resumed.snapshot_key == key == first.snapshot_key
    assert resumed.snapshot_id == first.snapshot_id
    assert resumed.queue == first.queue
    assert resumed.total_candidates == first.total_candidates
    # ...and an ENGINE checkpoint that carries the record forward.
    engine_checkpoint = {"phase": "swarm_v2",
                         "artifacts": {"swarm_state": {"anything": True},
                                       ARTIFACT_KEY: first.as_artifact()}}
    again = prepare_government_work(repository, checkpoint=engine_checkpoint)
    assert again.queue == first.queue and again.snapshot_key == key
    # A resume never re-selects: the bound passed now is irrelevant.
    narrower = prepare_government_work(repository, checkpoint=preparation_checkpoint, limit=1)
    assert narrower.queue == first.queue


def test_a_pinned_snapshot_that_cannot_be_resolved_refuses_the_resume():
    repository, *_ = landed()
    first = prepare_government_work(repository)
    record = first.as_artifact()
    record["snapshot_key"] = "gov:wltp:not-this-one"
    with pytest.raises(GovernmentPreparationError) as raised:
        prepare_government_work(repository, checkpoint={"artifacts": {ARTIFACT_KEY: record}})
    assert raised.value.code == "GOVERNMENT_SNAPSHOT_UNAVAILABLE"
    assert raised.value.reason_code == "GOV_PROJECTION_SNAPSHOT_UNKNOWN"


@pytest.mark.parametrize("corrupt", [
    lambda r: r.update(schema="something-else/9"),
    lambda r: r.update(queue="not-a-list"),
    lambda r: r.update(queue=r["queue"] + r["queue"][:1]),          # duplicate item
    lambda r: r["queue"].append({"candidate_key": "", "candidate_id": "x",
                                 "manufacturer": "m", "commercial_model": "c"}),
    lambda r: r.update(resource_id="not-an-allowed-resource"),
], ids=["schema", "queue-shape", "duplicate", "blank-key", "resource"])
def test_a_corrupt_preparation_record_is_refused_not_repaired(corrupt):
    repository, *_ = landed()
    record = prepare_government_work(repository, limit=3).as_artifact()
    corrupt(record)
    with pytest.raises(GovernmentPreparationError) as raised:
        prepare_government_work(repository, checkpoint={"artifacts": {ARTIFACT_KEY: record}})
    assert raised.value.code == "GOVERNMENT_PREPARATION_RECORD_INVALID"


def test_a_checkpoint_without_a_record_selects_afresh():
    repository, *_ = landed()
    fresh = prepare_government_work(repository, checkpoint={"phase": "swarm_v2",
                                                             "artifacts": {"swarm_state": {}}})
    assert fresh.resumed is False


# =============================================================================
# 4. progress: reconstructed from durable state and events
# =============================================================================

def test_progress_is_reconstructed_from_durable_evidence_and_promotion_events():
    repository, user, project, _key = landed()
    conversation = repository.create_conversation(UUID(project), "c", UUID(user))
    run = repository.create_message_and_run(
        UUID(conversation["id"]), "go", {}, UUID(user), None, "fp",
        **identity_kwargs(repository, conversation["id"]))["run"]
    prepared = prepare_government_work(repository, limit=4)
    evidenced, promoted, *rest = [item.candidate_key for item in prepared.queue]

    class Durable(MemoryRepository):
        def catalog_run_pending_promotions(self, run_id, tool_operation, *, limit=25):
            assert str(run_id) == run["id"]
            assert tool_operation == "catalog.government_vehicle.resolve_variant"
            return [{"candidate_key": evidenced}, {"candidate_key": "cv1.someone-elses"}]
    durable = Durable()
    durable.__dict__.update(repository.__dict__)
    durable.run_events.append({"id": 1, "run_id": run["id"], "event_type": "catalog_variant_promoted",
                               "payload": {"candidate_key": promoted, "promoted": True}})
    durable.run_events.append({"id": 2, "run_id": run["id"], "event_type": "catalog_promotion_refused",
                               "payload": {"candidate_key": rest[0], "promoted": False}})

    progress = government_work_progress(durable, UUID(run["id"]), prepared)
    assert progress[evidenced] == PROGRESS_EVIDENCED
    assert progress[promoted] == PROGRESS_PROMOTED
    assert all(progress[key] == PROGRESS_PENDING for key in rest)
    context = prepared.work_context(progress)
    assert context["remaining"] == len(rest)
    assert [item["progress"] for item in context["items"]] == \
        [PROGRESS_EVIDENCED, PROGRESS_PROMOTED] + [PROGRESS_PENDING] * len(rest)
    assert context["snapshot_key"] == prepared.snapshot_key


def test_a_repository_without_a_catalog_schema_reports_no_progress_and_no_error():
    repository, *_ = landed()
    prepared = prepare_government_work(repository, limit=2)

    class Bare:
        pass
    progress = government_work_progress(Bare(), uuid4(), prepared)
    assert set(progress.values()) == {PROGRESS_PENDING}


# =============================================================================
# 5. the real worker: after the lease, before the provider
# =============================================================================

def provider_tripwires(monkeypatch) -> None:
    """Every paid-provider seam raises if it is ever reached."""
    import backend.engines.swarm_v2 as swarm_package
    import backend.provider_authority as authority_module

    def tripped(*_args, **_kwargs):
        raise AssertionError("a paid provider path was constructed before preparation refused")
    monkeypatch.setattr(worker_main, "build_guarded_client_factory", tripped)
    monkeypatch.setattr(authority_module, "ProviderAdapter", tripped)
    monkeypatch.setattr(swarm_package, "ModelGateway", tripped)


def create_run(repository, conversation_id) -> UUID:
    created = repository.create_message_and_run(
        UUID(conversation_id), "process the register", {}, UUID(USER),
        f"gov-{uuid4().hex[:8]}", "fp", **identity_kwargs(repository, conversation_id))
    return UUID(str(created["run"]["id"]))


def test_the_worker_refuses_after_the_lease_and_before_any_provider_path(monkeypatch, no_transport):
    """Read ON, no usable snapshot: refused through the canonical finalizer,
    with the lease held and no provider seam ever constructed."""
    government_env(monkeypatch)
    provider_tripwires(monkeypatch)
    repository, conversation_id = build_repo()
    run_id = create_run(repository, conversation_id)

    assert worker_main.execute_run(run_id, repository) == 0

    run = repository.get_run(run_id)
    # AFTER the lease: the run was claimed by this worker...
    assert run["worker_id"] and run["attempt"] == 1
    types = event_types(repository, run_id)
    assert types[0] == "run_started"
    # ...and refused through the finalizer, BEFORE the provider.
    assert run["status"] == "failed"
    assert run["error"]["code"] == "GOVERNMENT_SNAPSHOT_UNAVAILABLE"
    assert run["error"]["message"] == prep.PREPARATION_REASONS["GOVERNMENT_SNAPSHOT_UNAVAILABLE"]
    assert types[-1] == "run_failed"
    failed = [e for e in events_of(repository, run_id) if e["event_type"] == "run_failed"][-1]
    assert failed["payload"]["reason"] == "GOV_PROJECTION_NO_ACTIVE_SNAPSHOT"
    assert "commander_plan_created" not in types
    assert "checkpoint_saved" not in types
    assert repository.checkpoints == []
    # The refusal is the run's canonical product outcome: refused before execution.
    assert failed["payload"]["code"] == "GOVERNMENT_SNAPSHOT_UNAVAILABLE"


def test_a_lease_that_cannot_be_taken_prepares_nothing(monkeypatch, no_transport):
    """Preparation is after the lease: a run another worker holds is never prepared."""
    government_env(monkeypatch)
    monkeypatch.setenv("MILO_WORKER_CLAIM_WAIT_SECONDS", "0")
    provider_tripwires(monkeypatch)
    repository, conversation_id = build_repo()
    land_pinned_government_snapshot(repository, user_id=USER, project_id=PROJECT)
    run_id = create_run(repository, conversation_id)
    repository.claim_run(run_id, "another-worker", lease_seconds=300)
    reads: list[str] = []
    original = MemoryRepository.list_active_catalog_snapshots

    def counting(self, *args, **kwargs):
        reads.append("snapshot")
        return original(self, *args, **kwargs)
    monkeypatch.setattr(MemoryRepository, "list_active_catalog_snapshots", counting)

    with pytest.raises(AppError) as raised:
        worker_main.execute_run(run_id, repository)
    assert raised.value.code == "RUN_ALREADY_CLAIMED"
    assert reads == []
    assert repository.checkpoints == []


def test_the_worker_prepares_after_the_lease_and_before_the_first_paid_call(monkeypatch, no_transport):
    """The positive path: claim -> pin -> queue -> checkpoint -> Commander."""
    government_env(monkeypatch)
    repository, conversation_id = build_repo()
    key = land_pinned_government_snapshot(repository, user_id=USER, project_id=PROJECT)
    completions = FakeKimiCompletions()
    run_id = run_worker_directly(repository, conversation_id, monkeypatch, completions,
                                 idempotency_key=f"gov-prep-{uuid4().hex[:8]}")

    assert worker_main.execute_run(run_id, repository) == 0

    types = event_types(repository, run_id)
    prepared_events = [e for e in events_of(repository, run_id)
                       if e["event_type"] == "checkpoint_saved"
                       and (e.get("payload") or {}).get("phase") == PREPARATION_PHASE]
    assert len(prepared_events) == 1
    payload = prepared_events[0]["payload"]
    assert payload["snapshot_key"] == key and payload["resumed"] is False
    assert 0 < payload["queued"] <= GOVERNMENT_WORK_QUEUE_LIMIT
    assert payload["remaining"] == payload["queued"]
    # Order: the lease (run_started) precedes preparation, which precedes the
    # first paid call (the Commander's plan).
    started = types.index("run_started")
    prepared_at = [i for i, e in enumerate(events_of(repository, run_id))
                   if e is prepared_events[0]][0]
    planned = types.index("commander_plan_created")
    assert started < prepared_at < planned

    # The FIRST durable checkpoint is the preparation record, written under the
    # lease; every engine checkpoint after it carries the same record forward.
    own = [c for c in repository.checkpoints if str(c["run_id"]) == str(run_id)]
    assert own[0]["phase"] == PREPARATION_PHASE
    record = own[0]["artifacts"][ARTIFACT_KEY]
    assert record["schema"] == ARTIFACT_SCHEMA and record["snapshot_key"] == key
    assert len(own) > 1
    for later in own[1:]:
        assert later["phase"] == "swarm_v2"
        assert later["artifacts"][ARTIFACT_KEY] == record
        assert "swarm_state" in later["artifacts"]

    # The Commander received the server-owned work selection through its
    # existing context seam, and nothing else defined the source.
    first_call = completions.calls[0]
    user_message = json.loads([m for m in first_call["messages"] if m["role"] == "user"][0]["content"])
    work = user_message["context"]["government_work"]
    assert work["snapshot_key"] == key
    assert work["source"] == "israel_ministry_of_transport_vehicle_register"
    assert [item["candidate_key"] for item in work["items"]] == \
        [item["candidate_key"] for item in record["queue"]]
    assert all(item["progress"] == PROGRESS_PENDING for item in work["items"])
    assert repository.get_run(run_id)["status"] in {"completed", "partial_success"}


def test_a_replacement_worker_resumes_the_same_pinned_queue_and_starts_the_engine_fresh(
        monkeypatch, no_transport):
    """Attempt 1 prepared and died; attempt 2 pins the same snapshot, walks the
    same queue, writes no second preparation record and hands the engine no
    preparation checkpoint to mistake for engine state."""
    government_env(monkeypatch)
    repository, conversation_id = build_repo()
    key = land_pinned_government_snapshot(repository, user_id=USER, project_id=PROJECT)
    run_id = create_run(repository, conversation_id)

    # Attempt 1: claimed, prepared, then gone (its lease lapses).
    crashed = repository.claim_run(run_id, "worker-crashed", lease_seconds=1)
    first = prepare_government_work(repository)
    repository.save_checkpoint({
        "run_id": str(run_id), "attempt": 1, "workflow_key": "swarm_v2",
        "engine_version": RunIdentity.from_record(crashed["run_identity"]).engine_version,
        "phase": PREPARATION_PHASE, "completed_tasks": [], "failures": [],
        "artifacts": {ARTIFACT_KEY: first.as_artifact()}, "token_usage": {}},
        worker_id="worker-crashed", attempt=1, lease_token=crashed["lease_token"])
    repository.runs[str(run_id)]["lease_expires_at"] = "2000-01-01T00:00:00+00:00"

    # Attempt 2: the real worker, reclaiming.
    completions = FakeKimiCompletions()
    monkeypatch.setenv("MILO_WORKER_CLAIM_WAIT_SECONDS", "0")
    from test_swarm_v2_smoke_offline import patch_client
    patch_client(monkeypatch, completions)
    assert worker_main.execute_run(run_id, repository) == 0

    run = repository.get_run(run_id)
    assert run["attempt"] == 2
    types = event_types(repository, run_id)
    assert "run_resumed" in types
    prepared_events = [e for e in events_of(repository, run_id)
                       if e["event_type"] == "checkpoint_saved"
                       and (e.get("payload") or {}).get("phase") == PREPARATION_PHASE]
    assert len(prepared_events) == 1
    assert prepared_events[0]["payload"]["resumed"] is True
    assert prepared_events[0]["payload"]["snapshot_key"] == key
    own = [c for c in repository.checkpoints if str(c["run_id"]) == str(run_id)]
    assert [c["phase"] for c in own].count(PREPARATION_PHASE) == 1
    # The engine started FRESH (a plan was requested), never from the
    # preparation record, and its checkpoints carry the pin forward.
    assert "commander_plan_created" in types
    assert any("CommanderPlan JSON Schema" in " ".join(
        m.get("content", "") for m in call["messages"] if m.get("role") == "system")
        for call in completions.calls)
    for later in own[1:]:
        assert later["artifacts"][ARTIFACT_KEY] == first.as_artifact()
    user_message = json.loads([m for m in completions.calls[0]["messages"]
                               if m["role"] == "user"][0]["content"])
    assert [i["candidate_key"] for i in user_message["context"]["government_work"]["items"]] == \
        [i.candidate_key for i in first.queue]


def test_a_chat_run_with_the_read_off_is_never_prepared(monkeypatch, no_transport):
    """Posture off: no snapshot read, no preparation record, no work context."""
    swarm_env(monkeypatch, **{CATALOG_EXECUTION_FLAG: "false", GOVERNMENT_READ_FLAG: "false",
                              CATALOG_PROMOTION_FLAG: "false"})
    repository, conversation_id = build_repo()
    land_pinned_government_snapshot(repository, user_id=USER, project_id=PROJECT)
    completions = FakeKimiCompletions()
    run_id = run_worker_directly(repository, conversation_id, monkeypatch, completions,
                                 idempotency_key=f"chat-{uuid4().hex[:8]}")
    assert worker_main.execute_run(run_id, repository) == 0
    own = [c for c in repository.checkpoints if str(c["run_id"]) == str(run_id)]
    assert all(c["phase"] != PREPARATION_PHASE for c in own)
    assert all(ARTIFACT_KEY not in (c.get("artifacts") or {}) for c in own)
    user_message = json.loads([m for m in completions.calls[0]["messages"]
                               if m["role"] == "user"][0]["content"])
    assert "government_work" not in user_message["context"]

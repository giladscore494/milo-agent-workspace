"""Government preparation: after the lease, before the provider.

A website-triggered catalog-processing run (a trusted `swarm_v2` run in a
deployment whose posture allows the Government read) must proceed in exactly
this order and no other:

    claim (run + worker + attempt + lease)
      -> read the Mapping Plan batch the database bound the run to
      -> resolve and PIN that batch's one immutable scoped Government snapshot
      -> take exactly the batch's candidates, in batch order, as the
         deterministic, bounded, resumable work queue
      -> only then construct or reach any paid provider path

These tests prove each arrow from the outside: the queue is exactly the bound
batch; a run bound to no batch is refused before any snapshot is read (scoped
catalog PR3: there is no "newest snapshot, first N candidates" scope any more);
a resumed attempt of the SAME run pins the SAME snapshot by exact key and walks
the SAME queue while the binding still names the same batch; per-item progress
is reconstructed from durable evidence and events; and the real worker refuses
through the canonical finalizer, with no provider path ever constructed, both an
unbound catalog-reading run and a batch run whose deployment has the read off.

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
                                                    BATCH_ARTIFACT_KEY,
                                                    GOVERNMENT_WORK_QUEUE_LIMIT,
                                                    PREPARATION_PHASE, PROGRESS_EVIDENCED,
                                                    PROGRESS_PENDING, PROGRESS_PROMOTED,
                                                    GovernmentPreparationError,
                                                    government_work_progress,
                                                    prepare_government_work,
                                                    refuse_bound_run_without_read)
from backend.errors import AppError
from backend.event_registry import fingerprint as event_registry_fingerprint
from backend.run_identity import RunIdentity
from backend.testing.catalog_review_seed import land_pinned_government_snapshot
from backend.testing.memory_repository import MemoryRepository
from backend.testing.work_scope_seed import seed_prepared_plan, start_batch_run
from tests.run_factory import identity_kwargs
from test_swarm_v2_smoke_offline import (PROJECT, USER, FakeKimiCompletions, build_repo,
                                         patch_client, swarm_env)

#: The registry fingerprint this branch shipped with. The preparation stage
#: persists through `run_checkpoints`, so it must not have moved.
REGISTRY_FINGERPRINT_BEFORE_PREPARATION = event_registry_fingerprint()


# =============================================================================
# helpers
# =============================================================================

def seeded() -> tuple[MemoryRepository, str, str]:
    """A Swarm V2 project with one member and one conversation."""
    repository = MemoryRepository()
    user, project = str(uuid4()), str(uuid4())
    repository.seed_user(user)
    repository.seed_project(project, "gov-prep", "Gov prep", [user], workflow_key="swarm_v2")
    conversation = repository.create_conversation(UUID(project), "c", UUID(user))
    return repository, user, conversation["id"]


def bound(repository: MemoryRepository | None = None, user: str | None = None,
          conversation: str | None = None) -> tuple[MemoryRepository, dict, str]:
    """A prepared plan (batches of 10, 10 and 5) and a run bound to batch 1."""
    if repository is None:
        repository, user, conversation = seeded()
    plan = seed_prepared_plan(repository, user_id=user, conversation_id=conversation)
    run = start_batch_run(repository, plan)["run"]["id"]
    return repository, plan, run


def unbound_run(repository: MemoryRepository, user: str, conversation: str) -> str:
    return repository.create_message_and_run(
        UUID(conversation), "process the register", {}, UUID(user), f"gov-{uuid4().hex[:8]}",
        "fp", **identity_kwargs(repository, conversation))["run"]["id"]


def batch_items(repository: MemoryRepository, run: str) -> list[dict]:
    return repository.work_scope_batch_for_run(UUID(run))["items"]


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
# 2. the queue: exactly the bound batch, and nothing for an unbound run
# =============================================================================

def test_an_unbound_run_is_refused_before_any_snapshot_is_read(monkeypatch):
    """No other catalog execution scope exists: not "the newest snapshot, first
    N candidates", even with a perfectly usable register snapshot active."""
    repository, user, conversation = seeded()
    land_pinned_government_snapshot(repository, user_id=user,
                                    project_id=repository.get_conversation(conversation)["project_id"])
    run = unbound_run(repository, user, conversation)
    reads: list[str] = []
    for name in ("list_active_catalog_snapshots", "find_active_catalog_snapshot",
                 "catalog_candidate_variant_page"):
        original = getattr(MemoryRepository, name)

        def counted(self, *args, _name=name, _original=original, **kwargs):
            reads.append(_name)
            return _original(self, *args, **kwargs)
        monkeypatch.setattr(MemoryRepository, name, counted)
    for run_id in (run, None):
        with pytest.raises(GovernmentPreparationError) as raised:
            prepare_government_work(repository, run_id=run_id)
        assert raised.value.code == "GOVERNMENT_BATCH_REQUIRED"
        assert raised.value.safe_message == prep.PREPARATION_REASONS["GOVERNMENT_BATCH_REQUIRED"]
    assert reads == []


def test_the_queue_is_exactly_the_bound_batch_in_batch_order():
    repository, plan, run = bound()
    first = prepare_government_work(repository, run_id=run)
    second = prepare_government_work(repository, run_id=run)
    assert first == second
    assert first.resumed is False
    assert first.snapshot_key == plan["snapshot_key"]
    items = batch_items(repository, run)
    assert [item.candidate_key for item in first.queue] == [row["candidate_key"] for row in items]
    assert [item.candidate_id for item in first.queue] == [row["candidate_id"] for row in items]
    assert (len(first.queue), first.total_candidates, first.bounded) == (10, 10, False)
    batch = plan["batches"][0]
    assert first.work_scope_batch == {
        "batch_id": batch["id"], "work_scope_id": plan["work_scope_id"], "revision": 1,
        "scope_digest": plan["digest"], "batch_number": 1, "attempt": 1}
    # Every item is an UNREAD reading of the pinned scoped snapshot.
    candidates = {row["id"]: row for row in repository.catalog_candidates.values()}
    for item in first.queue:
        assert candidates[item.candidate_id]["status"] == prep.QUEUED_CANDIDATE_STATUS
        assert candidates[item.candidate_id]["snapshot_id"] == first.snapshot_id
        assert item.manufacturer and item.commercial_model


def test_a_batch_never_exceeds_the_promotion_bound():
    """A batch is at most 20 candidates (a database CHECK), inside the 25 a run
    may promote: a run is never asked to research more than it could promote."""
    assert GOVERNMENT_WORK_QUEUE_LIMIT == 25
    repository, _plan, run = bound()
    assert len(prepare_government_work(repository, run_id=run).queue) <= 20 < \
        GOVERNMENT_WORK_QUEUE_LIMIT


def test_a_binding_read_failure_is_a_refusal_never_an_unbound_run():
    repository, _plan, run = bound()

    class Failing(MemoryRepository):
        pass
    failing = Failing()
    failing.__dict__.update(repository.__dict__)

    def broken(*_args, **_kwargs):
        raise AppError("REPOSITORY_ERROR", "connection reset by peer", 502)
    failing.work_scope_batch_for_run = broken  # type: ignore[method-assign]
    for call in (lambda: prepare_government_work(failing, run_id=run),
                 lambda: refuse_bound_run_without_read(failing, run)):
        with pytest.raises(GovernmentPreparationError) as raised:
            call()
        assert raised.value.code == "GOVERNMENT_QUEUE_UNAVAILABLE"
        assert "connection reset" not in str(raised.value)


def test_with_the_read_off_only_a_bound_run_is_refused():
    repository, plan, run = bound()
    with pytest.raises(GovernmentPreparationError) as raised:
        refuse_bound_run_without_read(repository, run)
    assert raised.value.code == "GOVERNMENT_READ_REQUIRED"
    refuse_bound_run_without_read(repository, unbound_run(repository, plan["user_id"],
                                                          plan["conversation_id"]))

    class Bare:
        pass
    refuse_bound_run_without_read(Bare(), run)  # no Mapping Plan schema: nothing is bound


# =============================================================================
# 3. resume: same run, same batch, same snapshot, same queue
# =============================================================================

def test_a_resumed_attempt_reuses_the_pinned_snapshot_and_the_same_queue():
    repository, plan, run = bound()
    first = prepare_government_work(repository, run_id=run)
    # The preparation record itself...
    preparation_checkpoint = {"phase": PREPARATION_PHASE,
                              "artifacts": {ARTIFACT_KEY: first.as_artifact()}}
    resumed = prepare_government_work(repository, checkpoint=preparation_checkpoint, run_id=run)
    assert resumed.resumed is True
    assert resumed.snapshot_key == plan["snapshot_key"] == first.snapshot_key
    assert resumed.snapshot_id == first.snapshot_id
    assert resumed.queue == first.queue
    assert resumed.total_candidates == first.total_candidates
    assert resumed.work_scope_batch == first.work_scope_batch
    # ...and an ENGINE checkpoint that carries the record forward.
    engine_checkpoint = {"phase": "swarm_v2",
                         "artifacts": {"swarm_state": {"anything": True},
                                       ARTIFACT_KEY: first.as_artifact()}}
    again = prepare_government_work(repository, checkpoint=engine_checkpoint, run_id=run)
    assert again.queue == first.queue and again.snapshot_key == plan["snapshot_key"]


def test_a_pinned_snapshot_that_cannot_be_resolved_refuses_the_resume():
    repository, _plan, run = bound()
    record = prepare_government_work(repository, run_id=run).as_artifact()
    record["snapshot_key"] = "gov:wltp:not-this-one"
    with pytest.raises(GovernmentPreparationError) as raised:
        prepare_government_work(repository, checkpoint={"artifacts": {ARTIFACT_KEY: record}},
                                run_id=run)
    assert raised.value.code == "GOVERNMENT_SNAPSHOT_UNAVAILABLE"
    assert raised.value.reason_code == "GOV_PROJECTION_SNAPSHOT_UNKNOWN"


@pytest.mark.parametrize("corrupt", [
    lambda r: r.update(schema="something-else/9"),
    lambda r: r.update(queue="not-a-list"),
    lambda r: r.update(queue=r["queue"] + r["queue"][:1]),          # duplicate item
    lambda r: r["queue"].append({"candidate_key": "", "candidate_id": "x",
                                 "manufacturer": "m", "commercial_model": "c"}),
    lambda r: r.update(resource_id="not-an-allowed-resource"),
    lambda r: r.pop(BATCH_ARTIFACT_KEY),                            # names no batch
    lambda r: r[BATCH_ARTIFACT_KEY].update(attempt=9),              # another attempt
], ids=["schema", "queue-shape", "duplicate", "blank-key", "resource", "no-batch",
        "other-attempt"])
def test_a_corrupt_preparation_record_is_refused_not_repaired(corrupt):
    repository, _plan, run = bound()
    record = prepare_government_work(repository, run_id=run).as_artifact()
    corrupt(record)
    with pytest.raises(GovernmentPreparationError) as raised:
        prepare_government_work(repository, checkpoint={"artifacts": {ARTIFACT_KEY: record}},
                                run_id=run)
    assert raised.value.code == "GOVERNMENT_PREPARATION_RECORD_INVALID"


def test_a_checkpoint_without_a_record_prepares_the_batch_afresh():
    repository, _plan, run = bound()
    fresh = prepare_government_work(repository, checkpoint={"phase": "swarm_v2",
                                                             "artifacts": {"swarm_state": {}}},
                                    run_id=run)
    assert fresh.resumed is False and len(fresh.queue) == 10


# =============================================================================
# 4. progress: reconstructed from durable state and events
# =============================================================================

def test_progress_is_reconstructed_from_durable_evidence_and_promotion_events():
    repository, _plan, run = bound()
    prepared = prepare_government_work(repository, run_id=run)
    evidenced, promoted, *rest = [item.candidate_key for item in prepared.queue]

    class Durable(MemoryRepository):
        def catalog_run_pending_promotions(self, run_id, tool_operation, *, limit=25):
            assert str(run_id) == run
            assert tool_operation == "catalog.government_vehicle.resolve_variant"
            return [{"candidate_key": evidenced}, {"candidate_key": "cv1.someone-elses"}]
    durable = Durable()
    durable.__dict__.update(repository.__dict__)
    durable.run_events.append({"id": 1, "run_id": run, "event_type": "catalog_variant_promoted",
                               "payload": {"candidate_key": promoted, "promoted": True}})
    durable.run_events.append({"id": 2, "run_id": run, "event_type": "catalog_promotion_refused",
                               "payload": {"candidate_key": rest[0], "promoted": False}})

    progress = government_work_progress(durable, UUID(run), prepared)
    assert progress[evidenced] == PROGRESS_EVIDENCED
    assert progress[promoted] == PROGRESS_PROMOTED
    assert all(progress[key] == PROGRESS_PENDING for key in rest)
    context = prepared.work_context(progress)
    assert context["remaining"] == len(rest)
    assert [item["progress"] for item in context["items"]] == \
        [PROGRESS_EVIDENCED, PROGRESS_PROMOTED] + [PROGRESS_PENDING] * len(rest)
    assert context["snapshot_key"] == prepared.snapshot_key


def test_a_repository_without_a_catalog_schema_reports_no_progress_and_no_error():
    repository, _plan, run = bound()
    prepared = prepare_government_work(repository, run_id=run)

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


def worker_world() -> tuple[MemoryRepository, str, dict]:
    """The smoke project, a prepared plan in its conversation, nothing started."""
    repository, conversation_id = build_repo()
    plan = seed_prepared_plan(repository, user_id=USER, conversation_id=conversation_id)
    return repository, conversation_id, plan


def assert_refused_before_the_provider(repository, run_id, code: str) -> None:
    run = repository.get_run(run_id)
    # AFTER the lease: the run was claimed by this worker...
    assert run["worker_id"] and run["attempt"] == 1
    types = event_types(repository, run_id)
    assert types[0] == "run_started"
    # ...and refused through the finalizer, BEFORE the provider.
    assert run["status"] == "failed"
    assert run["error"]["code"] == code
    assert run["error"]["message"] == prep.PREPARATION_REASONS[code]
    assert types[-1] == "run_failed"
    failed = [e for e in events_of(repository, run_id) if e["event_type"] == "run_failed"][-1]
    # The refusal is the run's canonical product outcome: refused before execution.
    assert failed["payload"]["code"] == code
    assert "commander_plan_created" not in types
    assert "checkpoint_saved" not in types
    assert [c for c in repository.checkpoints if str(c["run_id"]) == str(run_id)] == []


def test_the_worker_refuses_an_unbound_catalog_run_after_the_lease(monkeypatch, no_transport):
    """Read ON, a usable register snapshot, and a chat run bound to no batch:
    refused through the canonical finalizer, with no provider seam built."""
    government_env(monkeypatch)
    provider_tripwires(monkeypatch)
    repository, conversation_id = build_repo()
    land_pinned_government_snapshot(repository, user_id=USER, project_id=PROJECT)
    run_id = UUID(unbound_run(repository, USER, conversation_id))

    assert worker_main.execute_run(run_id, repository) == 0
    assert_refused_before_the_provider(repository, run_id, "GOVERNMENT_BATCH_REQUIRED")


def test_the_worker_refuses_a_batch_run_whose_read_is_off(monkeypatch, no_transport):
    """Read OFF and a batch-bound run: refused, never run without its catalog."""
    swarm_env(monkeypatch, **{CATALOG_EXECUTION_FLAG: "false", GOVERNMENT_READ_FLAG: "false",
                              CATALOG_PROMOTION_FLAG: "false"})
    provider_tripwires(monkeypatch)
    repository, _conversation, plan = worker_world()
    run_id = UUID(start_batch_run(repository, plan)["run"]["id"])

    assert worker_main.execute_run(run_id, repository) == 0
    assert_refused_before_the_provider(repository, run_id, "GOVERNMENT_READ_REQUIRED")


def test_a_lease_that_cannot_be_taken_prepares_nothing(monkeypatch, no_transport):
    """Preparation is after the lease: a run another worker holds is never prepared."""
    government_env(monkeypatch)
    monkeypatch.setenv("MILO_WORKER_CLAIM_WAIT_SECONDS", "0")
    provider_tripwires(monkeypatch)
    repository, _conversation, plan = worker_world()
    run_id = UUID(start_batch_run(repository, plan)["run"]["id"])
    repository.claim_run(run_id, "another-worker", lease_seconds=300)
    reads: list[str] = []
    for name in ("list_active_catalog_snapshots", "find_active_catalog_snapshot",
                 "work_scope_batch_for_run"):
        original = getattr(MemoryRepository, name)

        def counting(self, *args, _name=name, _original=original, **kwargs):
            reads.append(_name)
            return _original(self, *args, **kwargs)
        monkeypatch.setattr(MemoryRepository, name, counting)

    with pytest.raises(AppError) as raised:
        worker_main.execute_run(run_id, repository)
    assert raised.value.code == "RUN_ALREADY_CLAIMED"
    assert reads == []
    assert [c for c in repository.checkpoints if str(c["run_id"]) == str(run_id)] == []


def test_the_worker_prepares_after_the_lease_and_before_the_first_paid_call(monkeypatch, no_transport):
    """The positive path: claim -> batch -> pin -> queue -> checkpoint -> Commander."""
    government_env(monkeypatch)
    repository, _conversation, plan = worker_world()
    run_id = UUID(start_batch_run(repository, plan)["run"]["id"])
    items = batch_items(repository, str(run_id))
    completions = FakeKimiCompletions()
    patch_client(monkeypatch, completions)

    assert worker_main.execute_run(run_id, repository) == 0

    types = event_types(repository, run_id)
    prepared_events = [e for e in events_of(repository, run_id)
                       if e["event_type"] == "checkpoint_saved"
                       and (e.get("payload") or {}).get("phase") == PREPARATION_PHASE]
    assert len(prepared_events) == 1
    payload = prepared_events[0]["payload"]
    assert payload["snapshot_key"] == plan["snapshot_key"] and payload["resumed"] is False
    assert payload["queued"] == len(items) == 10
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
    assert record["schema"] == ARTIFACT_SCHEMA and record["snapshot_key"] == plan["snapshot_key"]
    assert record[BATCH_ARTIFACT_KEY]["batch_id"] == plan["batches"][0]["id"]
    assert len(own) > 1
    for later in own[1:]:
        assert later["phase"] == "swarm_v2"
        assert later["artifacts"][ARTIFACT_KEY] == record
        assert "swarm_state" in later["artifacts"]

    # The Commander received exactly the batch, through its existing context
    # seam, and the server-composed instruction as the objective.
    first_call = completions.calls[0]
    user_message = json.loads([m for m in first_call["messages"] if m["role"] == "user"][0]["content"])
    work = user_message["context"]["government_work"]
    assert work["snapshot_key"] == plan["snapshot_key"]
    assert work["source"] == "israel_ministry_of_transport_vehicle_register"
    assert [item["candidate_key"] for item in work["items"]] == \
        [row["candidate_key"] for row in items]
    assert all(item["progress"] == PROGRESS_PENDING for item in work["items"])
    assert repository.get_run(run_id)["status"] in {"completed", "partial_success"}


def test_a_replacement_worker_resumes_the_same_pinned_batch_and_starts_the_engine_fresh(
        monkeypatch, no_transport):
    """Attempt 1 prepared and died; attempt 2 pins the same snapshot, walks the
    same batch, writes no second preparation record and hands the engine no
    preparation checkpoint to mistake for engine state."""
    government_env(monkeypatch)
    repository, _conversation, plan = worker_world()
    run_id = UUID(start_batch_run(repository, plan)["run"]["id"])

    # Attempt 1: claimed, prepared, then gone (its lease lapses).
    crashed = repository.claim_run(run_id, "worker-crashed", lease_seconds=1)
    first = prepare_government_work(repository, run_id=run_id)
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
    assert prepared_events[0]["payload"]["snapshot_key"] == plan["snapshot_key"]
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
    patch_client(monkeypatch, completions)
    run_id = UUID(unbound_run(repository, USER, conversation_id))
    assert worker_main.execute_run(run_id, repository) == 0
    own = [c for c in repository.checkpoints if str(c["run_id"]) == str(run_id)]
    assert all(c["phase"] != PREPARATION_PHASE for c in own)
    assert all(ARTIFACT_KEY not in (c.get("artifacts") or {}) for c in own)
    user_message = json.loads([m for m in completions.calls[0]["messages"]
                               if m["role"] == "user"][0]["content"])
    assert "government_work" not in user_message["context"]
    assert repository.get_run(run_id)["status"] in {"completed", "partial_success"}

"""Scoped catalog PR3: batch runs, continuation and progress -- offline.

What this proves
----------------

1.  The repository contract (the in-memory mirror of
    `20260924000100_catalog_work_scope_batch_runs.sql`, held to the same
    answers as `tests/test_migrations_postgres.py`): one start is one message,
    one queued run with its immutable identity and one binding; a replay is the
    same answer; a double submission is the running batch, never a second run;
    only the NEXT batch of an unpaused head revision starts; `completed` and
    `partial_success` settle a batch; an interrupted batch is retried as its
    next attempt; progress is derived from the bound runs and their events.
2.  The Supabase repository calls exactly the reviewed RPCs and maps their
    refusals to static codes.
3.  The API: the start goes through the existing identity gate and the
    existing launch step (compare-and-set, launcher, failure handling), and is
    gated by BOTH run creation and the batch flag; pause / resume by the batch
    flag; progress is a membership-scoped read.
4.  End to end through the REAL worker: one batch per request, each run
    handed exactly its batch, no automatic next batch, and a plan that
    completes when its last batch settles.

Offline and deterministic: an autouse fixture refuses every outbound
connection, the Government rows are the committed R5 capture and every model
call is the offline Kimi fake.
"""

from __future__ import annotations

import json
import socket
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

import backend.worker.main as worker_main
from backend.catalog.execution import (CATALOG_EXECUTION_FLAG, CATALOG_PROMOTION_FLAG,
                                       GOVERNMENT_READ_FLAG)
from backend.catalog.scope import batches as wsb
from backend.catalog.scope import contract as wsc
from backend.catalog.scope import service as ws
from backend.dependencies import get_job_launcher, get_repository
from backend.errors import AppError, NotFoundError
from backend.execution_guard import SURFACE_RULES, find_disabled_surface
from backend.job_launcher import JobLaunchUncertain
from backend.main import app
from backend.repository.supabase import SupabaseRepository
from backend.run_identity import RunIdentity
from backend.testing.memory_repository import MemoryRepository
from backend.testing.work_scope_seed import (prepare_plan_head, seed_prepared_plan,
                                             start_batch_run)
from test_swarm_v2_smoke_offline import (FakeKimiCompletions, InlineWorkerLauncher, patch_client,
                                         swarm_env)

USER = UUID("11111111-2222-4333-8444-555555555555")
OUTSIDER = UUID("99999999-2222-4333-8444-555555555555")
BATCHES = wsb.WORK_SCOPE_BATCHES_FLAG
RUN_CREATION = wsb.RUN_CREATION_FLAG
TERMINAL = ("completed", "partial_success", "failed", "cancelled", "timed_out", "budget_exhausted")


@pytest.fixture(autouse=True)
def no_outbound_connections(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("a batch test attempted a network connection")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


# =============================================================================
# helpers
# =============================================================================

def world(workflow_key: str = "swarm_v2") -> tuple[MemoryRepository, str, str]:
    repo = MemoryRepository()
    repo.seed_user(str(USER))
    repo.seed_user(str(OUTSIDER))
    project = str(uuid4())
    repo.seed_project(project, f"p-{project[:8]}", "P", [str(USER)], workflow_key=workflow_key)
    conversation = repo.create_conversation(UUID(project), "plan", USER)["id"]
    return repo, project, conversation


def prepared(**plan_fields) -> tuple[MemoryRepository, dict]:
    """A plan prepared into batches of 10, 10 and 5 Toyota candidates."""
    repo, _project, conversation = world()
    plan = seed_prepared_plan(repo, user_id=str(USER), conversation_id=conversation, **plan_fields)
    return repo, plan


def start(repo: MemoryRepository, plan: dict, batch: str, *, key: str | None = None,
          user: UUID = USER, revision: int | None = None, digest: str | None = None,
          fingerprint: str | None = None, workflow_key: str = "swarm_v2",
          max_user: int | None = None, max_project: int | None = None) -> dict:
    """ONE call to the repository's batch-run creator, as the API makes it."""
    run_id = uuid4()
    identity = RunIdentity.bind(run_id, workflow_key).as_record()
    revision = plan["revision"] if revision is None else revision
    digest = plan["digest"] if digest is None else digest
    return repo.create_work_scope_batch_run(
        UUID(plan["work_scope_id"]), UUID(batch), revision, digest, run_id=run_id,
        run_identity=identity, content="Mapping plan batch", metadata={"requested_by": str(user)},
        requested_by=user, idempotency_key=key or f"key-{uuid4().hex}",
        request_fingerprint=fingerprint or wsb.batch_fingerprint(
            UUID(plan["work_scope_id"]), revision, digest, UUID(batch)),
        max_user_active=max_user, max_project_active=max_project)


def refused(code: str, call, *args, **kwargs) -> AppError:
    with pytest.raises(AppError) as failure:
        call(*args, **kwargs)
    assert failure.value.code == code, failure.value.code
    return failure.value


def counts(repo: MemoryRepository, plan: dict) -> tuple[int, int, int]:
    """(Swarm V2 runs, messages, bindings) in the plan's conversation."""
    conversation = plan["conversation_id"]
    runs = [run for run in repo.runs.values() if run["conversation_id"] == conversation
            and (run.get("run_identity") or {}).get("workflow_key") == "swarm_v2"]
    messages = [m for m in repo.messages if m["conversation_id"] == conversation
                and m["content"] != "operator catalog capture run; not executed by a model worker"]
    return len(runs), len(messages), len(repo.work_scope_batch_runs)


def finish(repo: MemoryRepository, run_id: str, status: str) -> None:
    repo.runs[str(run_id)]["status"] = status


def batch_ids(plan: dict) -> list[str]:
    return [batch["id"] for batch in plan["batches"]]


# =============================================================================
# 1. the repository contract (the in-memory mirror)
# =============================================================================

def test_a_start_is_one_message_one_run_and_one_binding():
    repo, plan = prepared()
    first, second, _third = batch_ids(plan)
    started = start(repo, plan, first, key="start-1")
    assert started["created"] is True
    run, binding = started["run"], started["binding"]
    assert (run["status"], run["launch_state"], run["conversation_id"]) == (
        "queued", "pending", plan["conversation_id"])
    assert run["run_identity"]["workflow_key"] == "swarm_v2"
    assert (binding["batch_id"], binding["run_id"], binding["attempt"]) == (first, run["id"], 1)
    assert counts(repo, plan) == (1, 1, 1)
    # The same request again is the same answer, and nothing is written twice.
    again = start(repo, plan, first, key="start-1")
    assert (again["created"], again["run"]["id"]) == (False, run["id"])
    refused("IDEMPOTENCY_CONFLICT", start, repo, plan, first, key="start-1",
            fingerprint="another-request")
    # A double submission with a fresh key is the batch already running.
    double = start(repo, plan, first, key="start-1-bis")
    assert (double["created"], double["run"]["id"]) == (False, run["id"])
    refused("WORK_SCOPE_BATCH_IN_PROGRESS", start, repo, plan, second, key="start-2")
    assert counts(repo, plan) == (1, 1, 1)
    # The worker reads exactly this batch for this run.
    assert repo.work_scope_batch_for_run(UUID(run["id"]))["batch"]["id"] == first


def test_a_start_fails_closed_and_writes_nothing():
    repo, plan = prepared()
    first, second, _third = batch_ids(plan)
    refused("WORK_SCOPE_STALE", start, repo, plan, first, digest="0" * 64)
    refused("WORK_SCOPE_STALE", start, repo, plan, first, revision=2)
    refused("WORK_SCOPE_BATCH_NOT_NEXT", start, repo, plan, second)
    with pytest.raises(NotFoundError):
        start(repo, plan, first, user=OUTSIDER)
    refused("WORK_SCOPE_BATCH_RUN_INVALID", start, repo, plan, first,
            workflow_key="vehicle_catalog_v1")
    refused("USER_CONCURRENCY_LIMIT", start, repo, plan, first, max_user=0)
    refused("PROJECT_CONCURRENCY_LIMIT", start, repo, plan, first, max_project=0)
    other_repo_plan = prepared()[1]
    with pytest.raises(NotFoundError):
        start(repo, plan, other_repo_plan["batches"][0]["id"])
    with pytest.raises(AppError) as missing_key:
        repo.create_work_scope_batch_run(
            UUID(plan["work_scope_id"]), UUID(first), 1, plan["digest"], run_id=uuid4(),
            run_identity={}, content="x", metadata={}, requested_by=USER, idempotency_key="",
            request_fingerprint="fp")
    assert missing_key.value.code == "WORK_SCOPE_BATCH_IDEMPOTENCY_REQUIRED"
    assert counts(repo, plan) == (0, 0, 0)


def test_continuation_runs_in_order_settles_and_retries_interrupted_batches():
    repo, plan = prepared()
    first, second, third = batch_ids(plan)
    finish(repo, start(repo, plan, first)["run"]["id"], "completed")
    progress = repo.work_scope_progress(UUID(plan["work_scope_id"]))["preparation"]
    assert progress["next"]["batch_id"] == second
    refused("WORK_SCOPE_BATCH_NOT_NEXT", start, repo, plan, third)
    refused("WORK_SCOPE_BATCH_ALREADY_COMPLETED", start, repo, plan, first)
    two = start(repo, plan, second)
    finish(repo, two["run"]["id"], "failed")
    progress = repo.work_scope_progress(UUID(plan["work_scope_id"]))["preparation"]
    assert (progress["next"]["batch_id"], progress["next"]["state"],
            progress["next"]["attempts"]) == (second, "interrupted", 1)
    refused("WORK_SCOPE_BATCH_NOT_NEXT", start, repo, plan, third)
    retry = start(repo, plan, second)
    assert retry["binding"]["attempt"] == 2
    # partial_success SETTLES a batch: its run finished its work.
    finish(repo, retry["run"]["id"], "partial_success")
    for status in ("cancelled", "timed_out", "budget_exhausted"):
        finish(repo, start(repo, plan, third)["run"]["id"], status)
    last = start(repo, plan, third)
    assert last["binding"]["attempt"] == 4
    finish(repo, last["run"]["id"], "completed")
    progress = repo.work_scope_progress(UUID(plan["work_scope_id"]))["preparation"]
    assert progress["next"] is None
    assert progress["batches"] == {"total": 3, "settled": 3, "active": 0, "interrupted": 0}
    for batch in batch_ids(plan):
        refused("WORK_SCOPE_BATCH_ALREADY_COMPLETED", start, repo, plan, batch)


def test_pause_and_resume_are_append_only_and_hold_the_plan():
    repo, plan = prepared()
    scope = UUID(plan["work_scope_id"])
    first = batch_ids(plan)[0]
    paused = repo.set_work_scope_paused(scope, True, USER)
    assert (paused["changed"], paused["paused"], paused["control"]["sequence"]) == (True, True, 1)
    assert repo.set_work_scope_paused(scope, True, USER)["changed"] is False
    refused("WORK_SCOPE_PAUSED", start, repo, plan, first)
    run = repo.create_message_and_run(
        UUID(plan["conversation_id"]), "x", {}, USER, None, "fp",
        run_id=(run_id := uuid4()), run_identity=RunIdentity.bind(run_id, "swarm_v2").as_record()
    )["run"]["id"]
    refused("WORK_SCOPE_PAUSED", repo.bind_work_scope_batch_run, UUID(first), UUID(run), 1,
            plan["digest"], USER)
    assert repo.work_scope_progress(scope)["paused"] is True
    resumed = repo.set_work_scope_paused(scope, False, USER)
    assert (resumed["changed"], resumed["control"]["sequence"]) == (True, 2)
    assert repo.set_work_scope_paused(scope, False, USER)["changed"] is False
    started = start(repo, plan, first)
    repo.set_work_scope_paused(scope, True, USER)
    # A pause never touches the batch already running.
    assert repo.runs[started["run"]["id"]]["status"] == "queued"
    with pytest.raises(NotFoundError):
        repo.set_work_scope_paused(scope, False, OUTSIDER)
    assert [(row["sequence"], row["action"]) for row in repo.work_scope_controls] == [
        (1, "pause"), (2, "resume"), (3, "pause")]


def revise(repo: MemoryRepository, plan: dict) -> None:
    """Revise the plan past the revision it was prepared from (Toyota only)."""
    fields = {"units": ["toyota"], "model_year_from": None, "model_year_to": None,
              "max_items": 25, "batch_size": 10}
    repo.revise_work_scope(UUID(plan["work_scope_id"]), plan["revision"], plan["digest"], USER,
                           {"scope_text": wsc.scope_from_fields(fields).canonical_text(),
                            "input_kind": "edit", "instruction": None, "notes": []})


def test_a_replay_that_would_launch_obeys_the_start_rules_as_they_stand_now():
    repo, plan = prepared()
    scope = UUID(plan["work_scope_id"])
    first = batch_ids(plan)[0]
    run_id = start(repo, plan, first, key="replay-1")["run"]["id"]
    # Replaying a run no worker was started for LAUNCHES it: that is a start,
    # and a paused plan starts nothing.
    repo.set_work_scope_paused(scope, True, USER)
    for launch_state in ("pending", "launch_failed"):
        repo.runs[run_id]["launch_state"] = launch_state
        refused("WORK_SCOPE_PAUSED", start, repo, plan, first, key="replay-1")
    # A run that was launched (or may have been) is only reported, never
    # launched again, so its replay answers whatever the plan's state.
    for launch_state in ("launching", "launched", "launch_unknown"):
        repo.runs[run_id]["launch_state"] = launch_state
        assert start(repo, plan, first, key="replay-1")["run"]["id"] == run_id
    repo.runs[run_id]["launch_state"] = "launch_failed"
    repo.set_work_scope_paused(scope, False, USER)
    again = start(repo, plan, first, key="replay-1")
    assert (again["created"], again["run"]["id"]) == (False, run_id)
    # Revised past the batch: a stale revision never launches.
    revise(repo, plan)
    refused("WORK_SCOPE_STALE", start, repo, plan, first, key="replay-1")
    repo.runs[run_id]["launch_state"] = "launched"
    assert start(repo, plan, first, key="replay-1")["run"]["id"] == run_id
    # A closed plan starts nothing either.
    repo.runs[run_id]["launch_state"] = "pending"
    repo.work_scopes[str(scope)]["closed_at"] = repo.work_scopes[str(scope)]["created_at"]
    refused("WORK_SCOPE_NOT_EDITABLE", start, repo, plan, first, key="replay-1")
    assert counts(repo, plan) == (1, 1, 1)


def test_progress_is_derived_from_bound_runs_and_their_promotion_events():
    repo, plan = prepared()
    scope = UUID(plan["work_scope_id"])
    first, second, _third = batch_ids(plan)
    progress = repo.work_scope_progress(scope)
    assert (progress["revision"], progress["paused"], progress["live"]) == (1, False, None)
    assert progress["preparation"]["items"] == {"total": 25, "promoted": 0, "refused": 0,
                                                "unresolved": 0}
    run = start(repo, plan, first)["run"]["id"]
    live = repo.work_scope_progress(scope)["live"]
    assert (live["batch_id"], live["run_id"], live["run_status"], live["launch_state"]) == (
        first, run, "queued", "pending")
    items = repo.work_scope_batch_for_run(UUID(run))["items"]
    keys = [item["candidate_key"] for item in items]
    outsider = repo.work_scope_queue_items[10]["candidate_key"]
    assert repo.work_scope_queue_items[10]["batch_id"] == second
    for event_type, key, promoted in (
            ("catalog_variant_promoted", keys[0], True), ("catalog_variant_promoted", keys[1], True),
            ("catalog_promotion_refused", keys[2], False),
            ("catalog_promotion_refused", keys[3], False), ("catalog_variant_promoted", keys[3], True),
            ("catalog_variant_promoted", keys[4], False),
            ("catalog_variant_promoted", outsider, True),
            ("catalog_variant_promoted", "cc1." + "0" * 32, True)):
        repo.append_run_event(UUID(run), event_type, {"message": "x", "payload": {
            "candidate_key": key, "promoted": promoted}})
    finish(repo, run, "completed")
    prepared_view = repo.work_scope_progress(scope)["preparation"]
    assert prepared_view["recent"][0] | {"run_id": None} == {
        "batch_id": first, "batch_number": 1, "unit_key": "toyota", "item_count": 10,
        "state": "completed", "attempts": 1, "run_id": None, "run_status": "completed",
        "promoted": 3, "refused": 1, "unresolved": 6}
    assert prepared_view["items"] == {"total": 25, "promoted": 4, "refused": 1, "unresolved": 6}
    toyota = prepared_view["units"][0]
    assert (toyota["batch_count"], toyota["settled_batches"], toyota["promoted"],
            toyota["refused"], toyota["unresolved"]) == (3, 1, 4, 1, 6)
    assert prepared_view["next"]["batch_id"] == second


def test_the_mirror_projects_through_the_service_the_same_way_the_api_does(monkeypatch):
    monkeypatch.setenv(BATCHES, "true")
    monkeypatch.setenv(RUN_CREATION, "true")
    repo, plan = prepared()
    view = wsb.progress(repo, USER, UUID(plan["work_scope_id"]))
    assert view["status"] == "ready"
    assert view["controls"]["start"] == {
        "available": True, "blocked_by": None, "retry": False, "relaunch": False,
        "batch": {"batch_id": batch_ids(plan)[0], "batch_number": 1, "unit_key": "toyota",
                  "item_count": 10, "state": "pending", "attempts": 0}}
    assert view["preparation"]["items"] == {"total": 25, "promoted": 0, "refused": 0,
                                            "unresolved": 0, "completed": 0, "remaining": 25}
    assert [(unit["unit_key"], unit["progress"]) for unit in view["preparation"]["units"]] == [
        ("toyota", "pending"), ("lexus", "not_queued")]
    with pytest.raises(NotFoundError):
        wsb.progress(repo, OUTSIDER, UUID(plan["work_scope_id"]))
    monkeypatch.delenv(BATCHES)
    assert wsb.progress(repo, USER, UUID(plan["work_scope_id"]))["controls"]["start"][
        "blocked_by"] == "batches_disabled"


def test_an_unreadable_progress_answer_is_refused_whole():
    repo, plan = prepared()

    class Corrupt(MemoryRepository):
        def work_scope_progress(self, work_scope_id):
            answer = MemoryRepository.work_scope_progress(self, work_scope_id)
            answer["preparation"]["items"]["promoted"] = -3
            return answer
    corrupt = Corrupt()
    corrupt.__dict__.update(repo.__dict__)
    refused("WORK_SCOPE_PROGRESS_UNAVAILABLE", wsb.progress, corrupt, USER,
            UUID(plan["work_scope_id"]))


def test_an_unlaunched_batch_is_launched_only_for_the_head_revision_never_cancelled(monkeypatch):
    monkeypatch.setenv(BATCHES, "true")
    monkeypatch.setenv(RUN_CREATION, "true")
    monkeypatch.setenv(wsb.RUN_CANCELLATION_FLAG, "true")
    live = {"batch_id": str(uuid4()), "batch_number": 1, "revision": 1, "unit_key": "toyota",
            "item_count": 10, "attempt": 1, "run_id": str(uuid4()), "run_status": "queued",
            "launch_state": "launch_failed"}
    raw = {"work_scope_id": str(uuid4()), "revision": 1, "digest": "a" * 64, "closed": False,
           "paused": False, "live": live, "preparation": None}
    for launch_state in ("launch_failed", "pending"):
        controls = wsb._progress_view({**raw, "live": {**live, "launch_state": launch_state}})[
            "controls"]
        head = controls["start"]
        assert (head["available"], head["relaunch"], head["batch"]["batch_id"]) == (
            True, True, live["batch_id"])
        # No worker was started, so nothing would finalize a cancellation.
        assert controls["cancel"]["available"] is False
        # The plan was revised past that batch: a stale revision never launches.
        revised = wsb._progress_view({**raw, "revision": 2,
                                      "live": {**live, "launch_state": launch_state}})["controls"]
        assert (revised["start"]["available"], revised["start"]["relaunch"],
                revised["start"]["blocked_by"], revised["cancel"]["available"]) == (
            False, False, "batch_running", False)
        # Paused: nothing launches.
        paused = wsb._progress_view({**raw, "paused": True,
                                     "live": {**live, "launch_state": launch_state}})["controls"]
        assert (paused["start"]["relaunch"], paused["start"]["blocked_by"]) == (False, "paused")
    # A launch that happened, or may have, is never offered again. Only one a
    # worker will finalize can be cancelled: `launched`. An unresolved launch
    # (`launching`, `launch_unknown`) may have no worker at all.
    for launch_state, cancellable in (("launching", False), ("launched", True),
                                      ("launch_unknown", False)):
        controls = wsb._progress_view({**raw, "live": {**live, "launch_state": launch_state}})[
            "controls"]
        assert (controls["start"]["relaunch"], controls["cancel"]["available"]) == (
            False, cancellable)


# =============================================================================
# 2. the Supabase repository
# =============================================================================

class _Rpc:
    def __init__(self, calls: list, name: str, params: dict, result: Any, error: str | None):
        self.calls, self.name, self.params = calls, name, params
        self.result, self.error = result, error

    def execute(self):
        self.calls.append((self.name, self.params))
        if self.error is not None:
            raise RuntimeError(self.error)
        return type("Response", (), {"data": self.result})()


def supabase(result: Any = None, error: str | None = None) -> tuple[SupabaseRepository, list]:
    calls: list = []
    repo = SupabaseRepository.__new__(SupabaseRepository)
    repo.client = type("Client", (), {"rpc": lambda _self, name, params: _Rpc(
        calls, name, params, result, error)})()
    return repo, calls


def test_the_supabase_batch_rpcs_send_exactly_their_parameters():
    scope, batch, run, user = uuid4(), uuid4(), uuid4(), uuid4()
    repo, calls = supabase([{"run": {"id": str(run)}, "binding": {"id": "b"}, "created": True}])
    repo.create_work_scope_batch_run(
        scope, batch, 3, "a" * 64, run_id=run, run_identity={"run_id": str(run)},
        content="c", metadata={"requested_by": str(user)}, requested_by=user,
        idempotency_key="ui-key-1", request_fingerprint="fp", max_user_active=1,
        max_project_active=None)
    assert calls == [("create_work_scope_batch_run", {
        "p_work_scope_id": str(scope), "p_batch_id": str(batch), "p_expected_revision": 3,
        "p_expected_digest": "a" * 64, "p_run_id": str(run),
        "p_run_identity": {"run_id": str(run)}, "p_content": "c",
        "p_metadata": {"requested_by": str(user)}, "p_requested_by": str(user),
        "p_idempotency_key": "ui-key-1", "p_request_fingerprint": "fp",
        "p_max_user_active": 1, "p_max_project_active": None})]
    repo, calls = supabase({"changed": True, "paused": True, "control": {}})
    repo.set_work_scope_paused(scope, True, user)
    assert calls == [("set_work_scope_paused", {"p_work_scope_id": str(scope), "p_paused": True,
                                                "p_requested_by": str(user)})]
    repo, calls = supabase({"work_scope_id": str(scope)})
    assert repo.work_scope_progress(scope) == {"work_scope_id": str(scope)}
    assert calls == [("work_scope_progress", {"p_work_scope_id": str(scope)})]
    repo, _calls = supabase(None)
    assert repo.work_scope_progress(scope) is None


@pytest.mark.parametrize(("message", "code"), [
    ("WORK_SCOPE_STALE", "WORK_SCOPE_STALE"),
    ("WORK_SCOPE_PAUSED", "WORK_SCOPE_PAUSED"),
    ("WORK_SCOPE_BATCH_NOT_NEXT", "WORK_SCOPE_BATCH_NOT_NEXT"),
    ("WORK_SCOPE_BATCH_IN_PROGRESS", "WORK_SCOPE_BATCH_IN_PROGRESS"),
    ("WORK_SCOPE_BATCH_ALREADY_COMPLETED", "WORK_SCOPE_BATCH_ALREADY_COMPLETED"),
    ("WORK_SCOPE_BATCH_RUN_INVALID", "WORK_SCOPE_BATCH_RUN_INVALID"),
    ("WORK_SCOPE_BATCH_IDEMPOTENCY_REQUIRED", "WORK_SCOPE_BATCH_IDEMPOTENCY_REQUIRED"),
    ("IDEMPOTENCY_CONFLICT", "IDEMPOTENCY_CONFLICT"),
    ("USER_CONCURRENCY_LIMIT", "USER_CONCURRENCY_LIMIT"),
    ("PROJECT_CONCURRENCY_LIMIT", "PROJECT_CONCURRENCY_LIMIT"),
    ("RUN_IDENTITY_WORKFLOW_DRIFT: project workflow changed", "RUN_IDENTITY_WORKFLOW_DRIFT"),
    ("RUN_IDENTITY_INVALID: identity must be an object", "RUN_IDENTITY_INVALID"),
    ("duplicate key value violates something internal", "REPOSITORY_ERROR"),
])
def test_the_supabase_batch_refusals_are_static_codes(message, code):
    repo, _calls = supabase(error=message)
    with pytest.raises(AppError) as failure:
        repo.create_work_scope_batch_run(
            uuid4(), uuid4(), 1, "a" * 64, run_id=uuid4(), run_identity={}, content="c",
            metadata={}, requested_by=uuid4(), idempotency_key="k", request_fingerprint="fp")
    assert failure.value.code == code
    assert "internal" not in failure.value.message
    for missing, resource in (("WORK_SCOPE_BATCH_NOT_FOUND", "work_scope_batch"),
                              ("WORK_SCOPE_NOT_FOUND", "work_scope")):
        repo, _calls = supabase(error=missing)
        with pytest.raises(NotFoundError):
            repo.set_work_scope_paused(uuid4(), True, uuid4())
        del resource


# =============================================================================
# 3. the API
# =============================================================================

class RecordingLauncher:
    def __init__(self, failure: Exception | None = None):
        self.launched: list[str] = []
        self.failure = failure

    def launch(self, run_id):
        self.launched.append(str(run_id))
        if self.failure is not None:
            raise self.failure
        return {"mode": "recording", "execution": f"recorded-{len(self.launched)}"}


@pytest.fixture()
def launcher():
    recorder = RecordingLauncher()
    app.dependency_overrides[get_job_launcher] = lambda: recorder
    yield recorder
    app.dependency_overrides.clear()


@pytest.fixture()
def enabled(monkeypatch):
    monkeypatch.setenv(BATCHES, "true")
    monkeypatch.setenv(RUN_CREATION, "true")
    monkeypatch.setenv("MILO_RATE_LIMIT_RUN_CREATION_USER", "1000")
    monkeypatch.setenv("MILO_RATE_LIMIT_RUN_CREATION_PROJECT", "1000")


def client(repo) -> TestClient:
    app.dependency_overrides[get_repository] = lambda: repo
    return TestClient(app)


def as_user(user: UUID = USER) -> dict[str, str]:
    return {"x-milo-auth-user-id": str(user)}


def start_body(plan: dict, batch: str, *, key: str | None = None, **overrides) -> dict:
    return {"expected_revision": plan["revision"], "expected_digest": plan["digest"],
            "batch_id": batch, "idempotency_key": key or f"ui-{uuid4().hex}", **overrides}


def post_start(repo, plan: dict, batch: str, *, user: UUID = USER, **body):
    return client(repo).post(f"/work-scopes/{plan['work_scope_id']}/runs",
                             json=start_body(plan, batch, **body), headers=as_user(user))


def get_progress(repo, plan: dict, user: UUID = USER):
    return client(repo).get(f"/work-scopes/{plan['work_scope_id']}/progress",
                            headers=as_user(user))


def test_the_batch_writes_are_gated_before_the_body_is_read(launcher, monkeypatch):
    repo, plan = prepared()
    first = batch_ids(plan)[0]
    scope = plan["work_scope_id"]
    for off in (BATCHES, RUN_CREATION):
        monkeypatch.setenv(BATCHES, "true")
        monkeypatch.setenv(RUN_CREATION, "true")
        monkeypatch.delenv(off)
        for response in (post_start(repo, plan, first),
                         client(repo).post(f"/work-scopes/{scope}/runs", content=b"not json",
                                           headers=as_user())):
            assert response.status_code == 403, off
            assert response.json()["error"]["code"] == "EXECUTION_SURFACE_DISABLED"
    monkeypatch.delenv(BATCHES)
    for action in ("pause", "resume"):
        response = client(repo).post(f"/work-scopes/{scope}/{action}", headers=as_user())
        assert response.status_code == 403
    assert launcher.launched == [] and counts(repo, plan) == (0, 0, 0)
    assert repo.work_scope_controls == []
    # Both flags name the start; the batch flag alone names pause / resume.
    start_rules = {flag for method, flag, pattern, _surface in SURFACE_RULES
                   if method == "POST" and pattern.match(f"/work-scopes/{scope}/runs")}
    assert start_rules == {BATCHES, RUN_CREATION}
    for action in ("pause", "resume"):
        assert {flag for method, flag, pattern, _surface in SURFACE_RULES
                if method == "POST" and pattern.match(f"/work-scopes/{scope}/{action}")} == {BATCHES}
    assert find_disabled_surface("GET", f"/work-scopes/{scope}/progress") is None


def test_the_progress_read_is_membership_scoped_and_ungated(launcher, monkeypatch):
    monkeypatch.delenv(BATCHES, raising=False)
    repo, plan = prepared()
    response = get_progress(repo, plan)
    assert response.status_code == 200
    body = response.json()
    assert (body["status"], body["controls"]["start"]["available"],
            body["controls"]["start"]["blocked_by"]) == ("ready", False, "batches_disabled")
    assert get_progress(repo, plan, OUTSIDER).status_code == 404
    assert client(repo).get(f"/work-scopes/{uuid4()}/progress", headers=as_user()).status_code == 404


def test_a_start_goes_through_the_one_launch_path(launcher, enabled):
    repo, plan = prepared()
    first, second, _third = batch_ids(plan)
    response = post_start(repo, plan, first, key="ui-start-000001")
    assert response.status_code == 202, response.text
    body = response.json()
    assert (body["batch_id"], body["attempt"], body["created"], body["status"]) == (
        first, 1, True, "queued")
    run = repo.runs[body["run_id"]]
    # The existing launch step: the compare-and-set, the launcher, the event.
    assert launcher.launched == [body["run_id"]]
    assert run["launch_state"] == "launched"
    assert "run_created" in [e["event_type"] for e in repo.run_events
                             if e["run_id"] == body["run_id"]]
    # The run's instruction was composed by the server, never typed.
    assert run["input"]["content"].startswith("Mapping plan batch 1 of 3 (plan revision 1)")
    assert "Toyota" in run["input"]["content"]
    assert run["request_fingerprint"] == wsb.batch_fingerprint(
        UUID(plan["work_scope_id"]), 1, plan["digest"], UUID(first))
    # The same request again: the same run, never a second launch.
    replay = post_start(repo, plan, first, key="ui-start-000001")
    assert (replay.status_code, replay.json()["run_id"], replay.json()["created"]) == (
        202, body["run_id"], False)
    # A double submission (a fresh key): the running batch, never a second run.
    double = post_start(repo, plan, first)
    assert (double.status_code, double.json()["run_id"], double.json()["created"]) == (
        202, body["run_id"], False)
    assert launcher.launched == [body["run_id"]]
    # The next batch waits for this one.
    waiting = post_start(repo, plan, second)
    assert (waiting.status_code, waiting.json()["error"]["code"]) == (409, "WORK_SCOPE_BATCH_NOT_NEXT")
    assert counts(repo, plan) == (1, 1, 1)
    # The capability says batches may start; the progress says one is running.
    capabilities = client(repo).get(f"/projects/{repo.get_conversation(plan['conversation_id'])['project_id']}"
                                    "/work-scope/capabilities", headers=as_user()).json()
    assert capabilities["can_start_batches"] is True and capabilities["can_prepare"] is False
    progress = get_progress(repo, plan).json()
    assert progress["status"] == "running"
    assert progress["live"]["run_id"] == body["run_id"]
    assert progress["controls"]["start"]["blocked_by"] == "batch_running"


@pytest.mark.parametrize(("overrides", "code", "status"), [
    ({"expected_digest": "0" * 64}, "WORK_SCOPE_STALE", 409),
    ({"expected_revision": 2}, "WORK_SCOPE_STALE", 409),
    ({"batch": 1}, "WORK_SCOPE_BATCH_NOT_NEXT", 409),
    ({"batch_id": "not-a-uuid"}, "VALIDATION_ERROR", 422),
    ({"idempotency_key": "short"}, "VALIDATION_ERROR", 422),
    ({"expected_revision": "1"}, "VALIDATION_ERROR", 422),
])
def test_a_refused_start_creates_and_launches_nothing(launcher, enabled, overrides, code, status):
    repo, plan = prepared()
    batch = batch_ids(plan)[overrides.pop("batch", 0)]
    body = start_body(plan, batch, **overrides)
    response = client(repo).post(f"/work-scopes/{plan['work_scope_id']}/runs", json=body,
                                 headers=as_user())
    assert response.status_code == status, response.text
    if code != "VALIDATION_ERROR":
        assert response.json()["error"]["code"] == code
    assert launcher.launched == [] and counts(repo, plan) == (0, 0, 0)


def test_a_paused_plan_and_a_stranger_start_nothing(launcher, enabled):
    repo, plan = prepared()
    first = batch_ids(plan)[0]
    scope = plan["work_scope_id"]
    paused = client(repo).post(f"/work-scopes/{scope}/pause", headers=as_user())
    assert paused.status_code == 200
    assert (paused.json()["changed"], paused.json()["paused"],
            paused.json()["progress"]["controls"]["start"]["blocked_by"]) == (True, True, "paused")
    refusal = post_start(repo, plan, first)
    assert (refusal.status_code, refusal.json()["error"]["code"]) == (409, "WORK_SCOPE_PAUSED")
    resumed = client(repo).post(f"/work-scopes/{scope}/resume", headers=as_user())
    assert (resumed.status_code, resumed.json()["changed"], resumed.json()["paused"]) == (
        200, True, False)
    for response in (post_start(repo, plan, first, user=OUTSIDER),
                     client(repo).post(f"/work-scopes/{scope}/pause", headers=as_user(OUTSIDER))):
        assert response.status_code == 404
    assert launcher.launched == [] and counts(repo, plan) == (0, 0, 0)


def test_a_failed_launch_is_relaunched_as_the_same_run_never_duplicated(enabled):
    repo, plan = prepared()
    first = batch_ids(plan)[0]
    failing = RecordingLauncher(failure=RuntimeError("cloud run said no"))
    app.dependency_overrides[get_job_launcher] = lambda: failing
    try:
        response = post_start(repo, plan, first, key="ui-launch-fails-1")
        assert (response.status_code, response.json()["error"]["code"]) == (502, "JOB_LAUNCH_FAILED")
        run_id = failing.launched[0]
        assert repo.runs[run_id]["launch_state"] == "launch_failed"
        progress = get_progress(repo, plan).json()
        assert progress["controls"]["start"]["relaunch"] is True
        assert progress["controls"]["start"]["batch"]["batch_id"] == first
        # Starting the same batch again relaunches THAT run, with a fresh key too.
        failing.failure = None
        again = post_start(repo, plan, first)
        assert (again.status_code, again.json()["run_id"], again.json()["created"]) == (
            202, run_id, False)
        assert failing.launched == [run_id, run_id]
        assert repo.runs[run_id]["launch_state"] == "launched"
        assert counts(repo, plan) == (1, 1, 1)
    finally:
        app.dependency_overrides.clear()


def test_a_replayed_start_never_launches_a_paused_or_stale_batch(enabled):
    repo, plan = prepared()
    first = batch_ids(plan)[0]
    scope = plan["work_scope_id"]
    failing = RecordingLauncher(failure=RuntimeError("cloud run said no"))
    app.dependency_overrides[get_job_launcher] = lambda: failing
    try:
        response = post_start(repo, plan, first, key="ui-replay-held-1")
        assert (response.status_code, response.json()["error"]["code"]) == (502, "JOB_LAUNCH_FAILED")
        run_id = failing.launched[0]
        failing.failure = None
        assert client(repo).post(f"/work-scopes/{scope}/pause", headers=as_user()).status_code == 200
        # The same request again would launch the run: a paused plan starts nothing.
        held = post_start(repo, plan, first, key="ui-replay-held-1")
        assert (held.status_code, held.json()["error"]["code"]) == (409, "WORK_SCOPE_PAUSED")
        assert failing.launched == [run_id]
        assert repo.runs[run_id]["launch_state"] == "launch_failed"
        assert client(repo).post(f"/work-scopes/{scope}/resume", headers=as_user()).status_code == 200
        resumed = post_start(repo, plan, first, key="ui-replay-held-1")
        assert (resumed.status_code, resumed.json()["run_id"], resumed.json()["created"]) == (
            202, run_id, False)
        assert failing.launched == [run_id, run_id]
        # Once launched, a replay only reports the run, even under a pause.
        client(repo).post(f"/work-scopes/{scope}/pause", headers=as_user())
        reported = post_start(repo, plan, first, key="ui-replay-held-1")
        assert (reported.status_code, reported.json()["run_id"]) == (202, run_id)
        assert failing.launched == [run_id, run_id]
        # A revised plan never launches its old revision's batch.
        client(repo).post(f"/work-scopes/{scope}/resume", headers=as_user())
        repo.runs[run_id]["launch_state"] = "launch_failed"
        revise(repo, plan)
        stale = post_start(repo, plan, first, key="ui-replay-held-1")
        assert (stale.status_code, stale.json()["error"]["code"]) == (409, "WORK_SCOPE_STALE")
        assert failing.launched == [run_id, run_id]
        assert counts(repo, plan) == (1, 1, 1)
    finally:
        app.dependency_overrides.clear()


def test_an_uncertain_launch_is_never_relaunched(enabled):
    repo, plan = prepared()
    first = batch_ids(plan)[0]
    uncertain = RecordingLauncher(failure=JobLaunchUncertain("maybe"))
    app.dependency_overrides[get_job_launcher] = lambda: uncertain
    try:
        response = post_start(repo, plan, first)
        assert (response.status_code, response.json()["error"]["code"]) == (502, "JOB_LAUNCH_UNKNOWN")
        run_id = uncertain.launched[0]
        assert repo.runs[run_id]["launch_state"] == "launch_unknown"
        assert get_progress(repo, plan).json()["controls"]["start"]["relaunch"] is False
        again = post_start(repo, plan, first)
        assert (again.status_code, again.json()["run_id"]) == (202, run_id)
        assert uncertain.launched == [run_id]
    finally:
        app.dependency_overrides.clear()


def test_a_runtime_that_cannot_bind_an_identity_creates_nothing(launcher, enabled, monkeypatch):
    repo, plan = prepared()
    monkeypatch.delenv("MILO_RELEASE_SHA")
    response = post_start(repo, plan, batch_ids(plan)[0])
    assert (response.status_code, response.json()["error"]["code"]) == (
        503, "RUN_IDENTITY_RUNTIME_MISMATCH")
    assert launcher.launched == [] and counts(repo, plan) == (0, 0, 0)


def test_the_concurrency_ceiling_holds_for_batch_runs(launcher, enabled, monkeypatch):
    repo, plan = prepared()
    monkeypatch.setenv("MILO_MAX_CONCURRENT_RUNS_PER_USER", "1")
    # Another run of the same user, elsewhere, is in flight.
    _repo2 = repo
    project = repo.get_conversation(plan["conversation_id"])["project_id"]
    elsewhere = repo.create_conversation(UUID(project), "elsewhere", USER)["id"]
    run_id = uuid4()
    repo.create_message_and_run(UUID(elsewhere), "chat", {}, USER, None, "fp", run_id=run_id,
                                run_identity=RunIdentity.bind(run_id, "swarm_v2").as_record())
    response = post_start(repo, plan, batch_ids(plan)[0])
    assert (response.status_code, response.json()["error"]["code"]) == (429, "USER_CONCURRENCY_LIMIT")
    assert launcher.launched == [] and len(repo.work_scope_batch_runs) == 0


def test_cancel_uses_the_existing_route_and_the_batch_is_retried_as_its_next_attempt(
        launcher, enabled, monkeypatch):
    monkeypatch.setenv("MILO_ENABLE_RUN_CANCELLATION", "true")
    repo, plan = prepared()
    first = batch_ids(plan)[0]
    run_id = post_start(repo, plan, first).json()["run_id"]
    progress = get_progress(repo, plan).json()
    assert progress["controls"]["cancel"] == {"available": True, "run_id": run_id}
    cancelled = client(repo).post(f"/runs/{run_id}/cancel", json={"reason": "stop"},
                                  headers=as_user())
    assert cancelled.status_code == 200
    # The worker finalizes a cancellation; until then the batch is still live.
    assert get_progress(repo, plan).json()["controls"]["cancel"]["available"] is False
    finish(repo, run_id, "cancelled")
    progress = get_progress(repo, plan).json()
    assert progress["live"] is None
    assert progress["controls"]["start"]["retry"] is True
    retry = post_start(repo, plan, first)
    assert (retry.status_code, retry.json()["attempt"], retry.json()["created"]) == (202, 2, True)


def test_the_chat_route_is_unchanged_and_creates_an_unbound_run(launcher, enabled):
    """The generic run route neither binds nor refuses: whether an unbound run
    may READ the catalog is the worker's decision (it refuses one that would),
    so there is exactly one catalog execution scope -- the batch."""
    repo, plan = prepared()
    response = client(repo).post(f"/conversations/{plan['conversation_id']}/runs",
                                 json={"content": "map every Toyota", "metadata": {},
                                       "idempotency_key": "ui-chat-000001"}, headers=as_user())
    assert response.status_code == 202
    assert repo.work_scope_batch_for_run(UUID(response.json()["run_id"])) is None
    assert len(repo.work_scope_batch_runs) == 0


# =============================================================================
# 4. end to end: the real worker, one batch per request, to completion
# =============================================================================

def test_each_batch_runs_as_one_request_and_the_plan_completes(monkeypatch):
    swarm_env(monkeypatch, **{CATALOG_EXECUTION_FLAG: "true", GOVERNMENT_READ_FLAG: "true",
                              CATALOG_PROMOTION_FLAG: "false", BATCHES: "true",
                              "MILO_RATE_LIMIT_RUN_CREATION_PROJECT": "1000"})
    completions = FakeKimiCompletions()
    patch_client(monkeypatch, completions)
    repo, plan = prepared()
    inline = InlineWorkerLauncher(repo)
    app.dependency_overrides[get_job_launcher] = lambda: inline
    try:
        seen_batches: list[list[str]] = []
        for index, batch in enumerate(batch_ids(plan), start=1):
            # Nothing starts by itself: before each request there are exactly
            # as many batch runs as requests made so far.
            assert len(repo.work_scope_batch_runs) == index - 1
            progress = get_progress(repo, plan).json()
            assert progress["controls"]["start"]["batch"]["batch_id"] == batch
            calls_before = len(completions.calls)
            response = post_start(repo, plan, batch)
            assert response.status_code == 202, response.text
            run_id = response.json()["run_id"]
            # The inline launcher ran the REAL worker to its terminal state.
            assert inline.exit_codes[-1] == 0
            assert repo.runs[run_id]["status"] in {"completed", "partial_success"}
            # The Commander was handed exactly this batch.
            user_message = json.loads([m for m in completions.calls[calls_before]["messages"]
                                       if m["role"] == "user"][0]["content"])
            work = user_message["context"]["government_work"]
            items = repo.work_scope_batch_for_run(UUID(run_id))["items"]
            assert [item["candidate_key"] for item in work["items"]] == \
                [item["candidate_key"] for item in items]
            seen_batches.append([item["candidate_key"] for item in items])
        # Every queued candidate was handed to exactly one run, in queue order.
        queue = [row["candidate_key"] for row in sorted(repo.work_scope_queue_items,
                                                       key=lambda row: row["position"])]
        assert [key for batch in seen_batches for key in batch] == queue
        final = get_progress(repo, plan).json()
        assert final["status"] == "complete"
        assert final["preparation"]["batches"]["settled"] == 3
        assert final["controls"]["start"] == {"available": False, "blocked_by": "complete",
                                              "batch": None, "retry": False, "relaunch": False}
        done = post_start(repo, plan, batch_ids(plan)[-1])
        assert (done.status_code, done.json()["error"]["code"]) == (409, "WORK_SCOPE_BATCH_NOT_NEXT")
        assert len(inline.launches) == 3
    finally:
        app.dependency_overrides.clear()


def test_a_chat_run_cannot_read_the_catalog_outside_a_batch(monkeypatch):
    """ONE execution scope: with the catalog read on, a run started from the
    conversation instead of the Mapping Plan is refused by the API before a
    message, a run or a launch exists (the worker refuses such a run too,
    `GOVERNMENT_BATCH_REQUIRED`, which `test_government_preparation.py` holds).
    The same conversation's prepared batch still starts."""
    swarm_env(monkeypatch, **{CATALOG_EXECUTION_FLAG: "true", GOVERNMENT_READ_FLAG: "true",
                              CATALOG_PROMOTION_FLAG: "false", BATCHES: "true",
                              "MILO_RATE_LIMIT_RUN_CREATION_PROJECT": "1000"})
    patch_client(monkeypatch, FakeKimiCompletions())
    repo, plan = prepared()
    inline = InlineWorkerLauncher(repo)
    app.dependency_overrides[get_job_launcher] = lambda: inline
    try:
        before = counts(repo, plan)
        response = client(repo).post(f"/conversations/{plan['conversation_id']}/runs",
                                     json={"content": "map every Toyota", "metadata": {},
                                           "idempotency_key": "ui-chat-000002"},
                                     headers=as_user())
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "CATALOG_RUN_REQUIRES_MAPPING_PLAN"
        # Nothing was written and nothing was launched.
        assert counts(repo, plan) == before and inline.launches == []
        capabilities = client(repo).get(
            f"/projects/{repo.get_conversation(UUID(plan['conversation_id']))['project_id']}"
            "/work-scope/capabilities", headers=as_user()).json()
        assert capabilities["direct_runs"] == {"allowed": False,
                                               "blocked_by": "catalog_batch_required"}
        # The Mapping Plan's batch is the one catalog execution path, and it starts.
        started = post_start(repo, plan, batch_ids(plan)[0])
        assert started.status_code == 202, started.text
        assert repo.work_scope_batch_for_run(UUID(started.json()["run_id"])) is not None
    finally:
        app.dependency_overrides.clear()


def test_the_direct_run_refusal_is_only_for_catalog_reading_swarm_v2(launcher, enabled,
                                                                     monkeypatch):
    """A Vehicle Catalog V1 project, and Swarm V2 with the read off, keep their
    ordinary runs: only a run that WOULD read the catalog is routed away."""
    monkeypatch.setenv(CATALOG_EXECUTION_FLAG, "true")
    monkeypatch.setenv(GOVERNMENT_READ_FLAG, "true")
    v1, v1_project, _v1_conversation = world("vehicle_catalog_v1")
    answer = client(v1).get(f"/projects/{v1_project}/work-scope/capabilities",
                            headers=as_user()).json()
    assert answer["direct_runs"] == {"allowed": True, "blocked_by": None}
    # The master switch alone (read off) does not route a Swarm V2 run away.
    monkeypatch.setenv(GOVERNMENT_READ_FLAG, "false")
    repo, project, conversation = world()
    assert ws.direct_run_blocker("swarm_v2") is None
    ordinary = client(repo).post(f"/conversations/{conversation}/runs",
                                 json={"content": "summarize", "metadata": {},
                                       "idempotency_key": "ui-plain-000001"}, headers=as_user())
    assert ordinary.status_code == 202, ordinary.text
    body = client(repo).get(f"/projects/{project}/work-scope/capabilities",
                            headers=as_user()).json()
    assert body["direct_runs"] == {"allowed": True, "blocked_by": None}
    # Run creation off: the composer is told so, whatever the catalog posture.
    monkeypatch.setenv(RUN_CREATION, "false")
    body = client(repo).get(f"/projects/{project}/work-scope/capabilities",
                            headers=as_user()).json()
    assert body["direct_runs"] == {"allowed": False, "blocked_by": "run_creation_disabled"}
    # The read ON names the routing reason first: it is this project's answer
    # at every stage, not only while run creation is on.
    monkeypatch.setenv(GOVERNMENT_READ_FLAG, "true")
    body = client(repo).get(f"/projects/{project}/work-scope/capabilities",
                            headers=as_user()).json()
    assert body["direct_runs"] == {"allowed": False, "blocked_by": "catalog_batch_required"}
    assert set(ws.DIRECT_RUN_BLOCKERS) == {"catalog_batch_required", "run_creation_disabled"}


# =============================================================================
# 6. a lost launch: reconciled by an operator, never relaunched by itself
# =============================================================================

class ApiProcessDied(BaseException):
    """The API process died mid-request: no `except Exception` handler of the
    launch step runs, so nothing after the launch compare-and-set is recorded."""


def _died(exc: BaseException) -> bool:
    # The test client re-raises it inside an exception group.
    return isinstance(exc, ApiProcessDied) or (
        isinstance(exc, BaseExceptionGroup) and exc.subgroup(ApiProcessDied) is not None)


def _die_mid_launch(repo: MemoryRepository, plan: dict, batch: str, **body) -> str:
    """The REAL start request: the API takes launch ownership -- its launch
    compare-and-set -- and its process dies inside the launcher call, before it
    records launched / launch_failed / launch_unknown."""
    dying = RecordingLauncher(failure=ApiProcessDied())
    previous = app.dependency_overrides.get(get_job_launcher)
    app.dependency_overrides[get_job_launcher] = lambda: dying
    try:
        with pytest.raises(BaseException) as died:
            post_start(repo, plan, batch, **body)
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_job_launcher, None)
        else:
            app.dependency_overrides[get_job_launcher] = previous
    assert _died(died.value), died.value
    run_id = dying.launched[0]
    assert (repo.runs[run_id]["status"], repo.runs[run_id]["launch_state"]) == (
        "queued", "launching")
    return run_id


def _quiet(repo: MemoryRepository, run_id: str, seconds: int = 3600) -> None:
    """Nothing has written the run's row for `seconds`."""
    from datetime import UTC, datetime, timedelta
    repo.runs[run_id]["updated_at"] = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()


def _reconcile(repo: MemoryRepository, run_id: str, outcome: str = "not_launched", *,
               quiet: int = 1800) -> dict:
    return repo.reconcile_lost_launch(UUID(run_id), outcome=outcome, min_quiet_seconds=quiet,
                                      operator="operator@milo-prod.iam.gserviceaccount.com")


def test_an_api_that_dies_after_taking_launch_ownership_leaves_a_launch_nothing_relaunches(
        launcher, enabled, monkeypatch):
    monkeypatch.setenv(wsb.RUN_CANCELLATION_FLAG, "true")
    repo, plan = prepared()
    first, second, _third = batch_ids(plan)
    lost = _die_mid_launch(repo, plan, first, key="ui-lost-1")
    # `launching` is not "not launched": replaying the very request, or a new
    # one for the batch, answers with the same run and launches nothing.
    for key in ("ui-lost-1", "ui-lost-1-again"):
        again = post_start(repo, plan, first, key=key)
        assert (again.status_code, again.json()["run_id"], again.json()["created"]) == (
            202, lost, False)
    assert launcher.launched == []
    # The launch compare-and-set itself refuses it: no second worker.
    assert repo.try_acquire_launch(UUID(lost)) is None
    assert repo.runs[lost]["launch_state"] == "launching"
    held = post_start(repo, plan, second)
    assert (held.status_code, held.json()["error"]["code"]) == (409, "WORK_SCOPE_BATCH_NOT_NEXT")
    progress = get_progress(repo, plan).json()
    assert progress["live"]["launch_state"] == "launching"
    controls = progress["controls"]
    # Neither relaunched nor offered Cancel: a cancellation nobody finalizes
    # would hold the plan for good.
    assert (controls["start"]["available"], controls["start"]["relaunch"],
            controls["start"]["blocked_by"], controls["cancel"]["available"]) == (
        False, False, "batch_running", False)
    assert counts(repo, plan) == (1, 1, 1)


@pytest.mark.parametrize("status,launch_state,cancellable", [
    ("queued", "pending", False),
    ("queued", "launch_failed", False),
    ("queued", "launching", False),
    ("queued", "launch_unknown", False),
    ("queued", "launched", True),
    ("starting", "launching", True),
    ("running", "launched", True),
    ("waiting", "launch_unknown", True),
    ("cancellation_requested", "launched", False),
])
def test_cancel_is_offered_only_for_a_run_a_worker_will_finalize(monkeypatch, status,
                                                                  launch_state, cancellable):
    for flag in (BATCHES, RUN_CREATION, wsb.RUN_CANCELLATION_FLAG):
        monkeypatch.setenv(flag, "true")
    live = {"batch_id": str(uuid4()), "batch_number": 1, "revision": 1, "unit_key": "toyota",
            "item_count": 10, "attempt": 1, "run_id": str(uuid4()), "run_status": status,
            "launch_state": launch_state}
    raw = {"work_scope_id": str(uuid4()), "revision": 1, "digest": "a" * 64, "closed": False,
           "paused": False, "live": live, "preparation": None}
    controls = wsb._progress_view(raw)["controls"]
    assert controls["cancel"] == {"available": cancellable, "run_id": live["run_id"]}
    # Only a launch that never happened or definitely failed is launched again.
    assert controls["start"]["relaunch"] is (
        status == "queued" and launch_state in ("pending", "launch_failed"))


def test_an_uncertain_launch_offers_no_cancel_and_a_launched_run_does(launcher, enabled,
                                                                      monkeypatch):
    monkeypatch.setenv(wsb.RUN_CANCELLATION_FLAG, "true")
    repo, plan = prepared()
    first = batch_ids(plan)[0]
    uncertain = RecordingLauncher(failure=JobLaunchUncertain("maybe"))
    app.dependency_overrides[get_job_launcher] = lambda: uncertain
    response = post_start(repo, plan, first)
    assert response.json()["error"]["code"] == "JOB_LAUNCH_UNKNOWN"
    unknown = uncertain.launched[0]
    assert get_progress(repo, plan).json()["controls"]["cancel"] == {
        "available": False, "run_id": unknown}
    # The operator found the execution: `launched`, and now a worker will
    # finalize a cancellation, so it is offered.
    repo.runs[unknown]["launch_state"] = "launched"
    assert get_progress(repo, plan).json()["controls"]["cancel"] == {
        "available": True, "run_id": unknown}


@pytest.mark.parametrize("posture,code", [
    ({"launch_state": "launch_unknown"}, "LOST_LAUNCH_WRONG_STATE"),
    ({"launch_state": "pending"}, "LOST_LAUNCH_WRONG_STATE"),
    ({"launch_state": "launched"}, "LOST_LAUNCH_WRONG_STATE"),
    ({"status": "starting", "worker_id": "worker-1"}, "LOST_LAUNCH_WRONG_STATE"),
    ({"status": "cancellation_requested"}, "LOST_LAUNCH_WRONG_STATE"),
    ({"lease_token": "t" * 64, "lease_expires_at": "2999-01-01T00:00:00+00:00"},
     "LOST_LAUNCH_CLAIMED"),
    ({"worker_id": "worker-1"}, "LOST_LAUNCH_CLAIMED"),
    ({"started_at": "2026-09-23T00:00:00+00:00"}, "LOST_LAUNCH_CLAIMED"),
])
def test_only_a_quiet_unclaimed_lost_launch_is_ever_reconciled(launcher, enabled, posture, code):
    repo, plan = prepared()
    lost = _die_mid_launch(repo, plan, batch_ids(plan)[0])
    repo.runs[lost].update(posture)
    _quiet(repo, lost)
    before = dict(repo.runs[lost])
    for outcome in ("not_launched", "launched"):
        if outcome == "launched" and posture == {"launch_state": "launched"}:
            continue  # already where that decision leads: an idempotent no-op
        refused(code, _reconcile, repo, lost, outcome)
    assert repo.runs[lost] == before
    assert launcher.launched == []


def test_not_launched_is_refused_when_more_than_the_api_wrote_about_the_run(launcher, enabled):
    repo, plan = prepared()
    lost = _die_mid_launch(repo, plan, batch_ids(plan)[0])
    # The launcher's own record: the launch DID happen before the API died.
    repo.record_run_invocation(UUID(lost), {"mode": "cloud_run", "execution": "exec-1"})
    _quiet(repo, lost)
    refused("LOST_LAUNCH_TRACED", _reconcile, repo, lost)
    assert repo.runs[lost]["launch_state"] == "launching"
    # ... which is exactly what `launched` records.
    assert _reconcile(repo, lost, "launched")["launch_state"] == "launched"


def test_an_operator_reconciles_a_lost_launch_and_the_plan_continues_without_duplicate_execution(
        monkeypatch):
    swarm_env(monkeypatch, **{CATALOG_EXECUTION_FLAG: "true", GOVERNMENT_READ_FLAG: "true",
                              CATALOG_PROMOTION_FLAG: "false", BATCHES: "true",
                              wsb.RUN_CANCELLATION_FLAG: "true",
                              "MILO_RATE_LIMIT_RUN_CREATION_PROJECT": "1000"})
    completions = FakeKimiCompletions()
    patch_client(monkeypatch, completions)
    repo, plan = prepared()
    first, second, _third = batch_ids(plan)
    lost = _die_mid_launch(repo, plan, first, key="ui-lost-plan-1")

    # Never while a launch request could still be in flight, never below the floor.
    refused("LOST_LAUNCH_NOT_QUIET", _reconcile, repo, lost)
    refused("LOST_LAUNCH_THRESHOLD_TOO_SHORT", _reconcile, repo, lost, quiet=60)
    _quiet(repo, lost)
    # The operator checked Cloud Run and found no execution of the run.
    assert _reconcile(repo, lost) == {"reconciled": True, "run_id": lost, "status": "queued",
                                      "launch_state": "launch_failed",
                                      "previous_launch_state": "launching"}
    assert (repo.runs[lost]["status"], repo.runs[lost]["launch_error"]["code"]) == (
        "queued", "RUN_LAUNCH_LOST")
    assert _reconcile(repo, lost)["reconciled"] is False
    events = [e for e in repo.run_events if e["run_id"] == lost]
    assert [e["event_type"] for e in events] == ["launch_failed"]
    assert events[0]["payload"] == {"recoverable": True, "reconciled": True,
                                    "previous_launch_state": "launching"}

    # The existing retry path: the SAME run is offered for launch, and nothing
    # launched it by itself.
    progress = get_progress(repo, plan).json()
    assert (progress["live"]["run_id"], progress["live"]["launch_state"]) == (lost, "launch_failed")
    controls = progress["controls"]
    assert (controls["start"]["available"], controls["start"]["relaunch"],
            controls["start"]["batch"]["batch_id"], controls["cancel"]["available"]) == (
        True, True, first, False)
    assert counts(repo, plan) == (1, 1, 1)

    inline = InlineWorkerLauncher(repo)
    app.dependency_overrides[get_job_launcher] = lambda: inline
    try:
        # A person confirms "Launch batch 1": the same run, launched once, run once.
        launched = post_start(repo, plan, first)
        assert launched.status_code == 202, launched.text
        assert (launched.json()["run_id"], launched.json()["created"]) == (lost, False)
        assert inline.launches == [lost] and inline.exit_codes == [0]
        assert repo.runs[lost]["status"] in {"completed", "partial_success"}
        calls = len(completions.calls)
        assert calls, "the relaunched run did the batch's work"
        # The lost launch's own worker, arriving late, finds a finished run and
        # executes nothing: at most one worker ever executes the run.
        assert worker_main.execute_run(UUID(lost), repo) == 0
        assert len(completions.calls) == calls
        # A second click launches nothing: the batch is settled.
        again = post_start(repo, plan, first)
        assert again.status_code == 409 and inline.launches == [lost]

        # Continuation: the next batch starts only on a person's request.
        progress = get_progress(repo, plan).json()
        assert progress["live"] is None
        assert progress["preparation"]["batches"]["settled"] == 1
        assert progress["controls"]["start"]["batch"]["batch_id"] == second
        continued = post_start(repo, plan, second)
        assert continued.status_code == 202, continued.text
        assert continued.json()["created"] is True and continued.json()["run_id"] != lost
        assert inline.launches == [lost, continued.json()["run_id"]]
        assert repo.runs[continued.json()["run_id"]]["status"] in {"completed", "partial_success"}
    finally:
        app.dependency_overrides.clear()
    assert get_progress(repo, plan).json()["preparation"]["batches"]["settled"] == 2
    assert counts(repo, plan) == (2, 2, 2)


def test_a_lost_launch_confirmed_launched_is_run_once_by_its_worker(monkeypatch):
    swarm_env(monkeypatch, **{CATALOG_EXECUTION_FLAG: "true", GOVERNMENT_READ_FLAG: "true",
                              CATALOG_PROMOTION_FLAG: "false", BATCHES: "true",
                              wsb.RUN_CANCELLATION_FLAG: "true",
                              "MILO_RATE_LIMIT_RUN_CREATION_PROJECT": "1000"})
    completions = FakeKimiCompletions()
    patch_client(monkeypatch, completions)
    repo, plan = prepared()
    lost = _die_mid_launch(repo, plan, batch_ids(plan)[0])
    _quiet(repo, lost)
    # The operator found the execution the dead API had started.
    assert _reconcile(repo, lost, "launched")["launch_state"] == "launched"
    controls = get_progress(repo, plan).json()["controls"]
    # A worker exists to finalize a cancellation now; nothing is relaunched.
    assert (controls["start"]["relaunch"], controls["cancel"]["available"]) == (False, True)
    assert worker_main.execute_run(UUID(lost), repo) == 0
    calls = len(completions.calls)
    assert calls and repo.runs[lost]["status"] in {"completed", "partial_success"}
    # A second worker for the same run executes nothing.
    assert worker_main.execute_run(UUID(lost), repo) == 0
    assert len(completions.calls) == calls


# =============================================================================
# 7. a never-launched batch of a revised plan, and cancellation nobody finishes
# =============================================================================

def _retire(repo: MemoryRepository, run_id: str, *, quiet: int = 1800) -> dict:
    return repo.retire_unlaunched_run(UUID(run_id), min_quiet_seconds=quiet,
                                      operator="operator@milo-prod.iam.gserviceaccount.com")


def _fail_the_launch(repo: MemoryRepository, plan: dict, batch: str, **body) -> str:
    """The real start request, whose launcher DEFINITELY fails: no worker."""
    failing = RecordingLauncher(failure=RuntimeError("cloud run said no"))
    previous = app.dependency_overrides.get(get_job_launcher)
    app.dependency_overrides[get_job_launcher] = lambda: failing
    try:
        response = post_start(repo, plan, batch, **body)
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_job_launcher, None)
        else:
            app.dependency_overrides[get_job_launcher] = previous
    assert response.json()["error"]["code"] == "JOB_LAUNCH_FAILED", response.text
    run_id = failing.launched[0]
    assert (repo.runs[run_id]["status"], repo.runs[run_id]["launch_state"]) == (
        "queued", "launch_failed")
    return run_id


def _cancel(repo, run_id: str):
    return client(repo).post(f"/runs/{run_id}/cancel", json={"reason": "stop"}, headers=as_user())


@pytest.fixture(autouse=True)
def generous_cancellation_limit(monkeypatch):
    """These tests cancel as one user many times over; the per-user rate limit
    is not what they are about."""
    monkeypatch.setenv("MILO_RATE_LIMIT_CANCELLATION", "1000")


@pytest.mark.parametrize("launch_state,code", [
    ("pending", "RUN_NOT_LAUNCHED"), ("launch_failed", "RUN_NOT_LAUNCHED"),
    ("none", "RUN_NOT_LAUNCHED"), ("launching", "RUN_LAUNCH_UNRESOLVED"),
    ("launch_unknown", "RUN_LAUNCH_UNRESOLVED"),
])
def test_the_generic_cancellation_refuses_a_run_no_worker_would_finalize(
        launcher, enabled, monkeypatch, launch_state, code):
    monkeypatch.setenv(wsb.RUN_CANCELLATION_FLAG, "true")
    repo, plan = prepared()
    run_id = start(repo, plan, batch_ids(plan)[0])["run"]["id"]
    repo.runs[run_id]["launch_state"] = launch_state
    before = dict(repo.runs[run_id])
    refused_response = _cancel(repo, run_id)
    assert (refused_response.status_code, refused_response.json()["error"]["code"]) == (409, code)
    # Nothing written: no request, no event.
    assert repo.runs[run_id] == before
    assert not [e for e in repo.run_events if e["run_id"] == run_id]
    # The database write refuses it on its own, whoever calls it.
    refused(code, repo.request_cancellation, UUID(run_id), "stop")
    assert repo.runs[run_id] == before


@pytest.mark.parametrize("status,launch_state", [
    ("queued", "launched"), ("starting", "launching"), ("running", "launched"),
    ("waiting", "launch_unknown"),
])
def test_the_generic_cancellation_still_stops_a_run_a_worker_will_finalize(
        launcher, enabled, monkeypatch, status, launch_state):
    monkeypatch.setenv(wsb.RUN_CANCELLATION_FLAG, "true")
    repo, plan = prepared()
    run_id = start(repo, plan, batch_ids(plan)[0])["run"]["id"]
    repo.runs[run_id].update(status=status, launch_state=launch_state)
    accepted = _cancel(repo, run_id)
    assert (accepted.status_code, accepted.json()["status"]) == (200, "cancellation_requested")
    assert [e["event_type"] for e in repo.run_events if e["run_id"] == run_id] == [
        "cancellation_requested"]
    # Idempotent: the same request again records nothing new.
    assert _cancel(repo, run_id).status_code == 200
    assert len([e for e in repo.run_events if e["run_id"] == run_id]) == 1


class _Query:
    """A recording PostgREST table query: every filter the write carries."""

    def __init__(self, log: list, rows: list):
        self.log, self.rows = log, rows

    def update(self, payload):
        self.log.append(("update", payload["status"]))
        return self

    def eq(self, column, value):
        self.log.append(("eq", column, value))
        return self

    def select(self, *_columns):
        return self

    def execute(self):
        return type("Response", (), {"data": self.rows})()


def _supabase_cancellation(run: dict, rows: list | None = None) -> tuple[SupabaseRepository, list]:
    log: list = []
    repo = SupabaseRepository.__new__(SupabaseRepository)
    repo.client = type("Client", (), {"table": lambda _self, _name: _Query(
        log, [dict(run, status="cancellation_requested")] if rows is None else rows)})()
    repo.get_run = lambda _run_id, user_id=None: run
    return repo, log


def test_the_supabase_cancellation_write_carries_the_guard_itself():
    run_id = uuid4()
    # An unclaimed run: the database matches it only while its launch is
    # recorded `launched`, in the same statement as the write.
    repo, log = _supabase_cancellation({"id": str(run_id), "status": "queued",
                                         "launch_state": "launched"})
    assert repo.request_cancellation(run_id, "stop")["status"] == "cancellation_requested"
    assert log == [("update", "cancellation_requested"), ("eq", "id", str(run_id)),
                   ("eq", "status", "queued"), ("eq", "launch_state", "launched")]
    # A run a worker holds: the status compare-and-set alone.
    repo, log = _supabase_cancellation({"id": str(run_id), "status": "running",
                                         "launch_state": "launching"})
    repo.request_cancellation(run_id, "stop")
    assert ("eq", "launch_state", "launched") not in log and ("eq", "status", "running") in log
    # A run no worker would finalize: refused, and no write is even attempted.
    for launch_state, code in (("pending", "RUN_NOT_LAUNCHED"), ("launch_failed", "RUN_NOT_LAUNCHED"),
                               ("launching", "RUN_LAUNCH_UNRESOLVED"),
                               ("launch_unknown", "RUN_LAUNCH_UNRESOLVED")):
        repo, log = _supabase_cancellation({"id": str(run_id), "status": "queued",
                                             "launch_state": launch_state})
        refused(code, repo.request_cancellation, run_id, "stop")
        assert log == []
    # The run moved between the read and the write: nothing matched, a conflict.
    repo, _log = _supabase_cancellation({"id": str(run_id), "status": "queued",
                                          "launch_state": "launched"}, rows=[])
    refused("RUN_TRANSITION_CONFLICT", repo.request_cancellation, run_id, "stop")


@pytest.mark.parametrize("posture,code", [
    ({"launch_state": "launching"}, "UNLAUNCHED_RUN_WRONG_STATE"),
    ({"launch_state": "launch_unknown"}, "UNLAUNCHED_RUN_WRONG_STATE"),
    ({"launch_state": "launched"}, "UNLAUNCHED_RUN_WRONG_STATE"),
    ({"status": "running", "worker_id": "worker-1"}, "UNLAUNCHED_RUN_WRONG_STATE"),
    ({"lease_token": "t" * 64, "lease_expires_at": "2999-01-01T00:00:00+00:00"},
     "UNLAUNCHED_RUN_CLAIMED"),
    ({"started_at": "2026-09-23T00:00:00+00:00"}, "UNLAUNCHED_RUN_CLAIMED"),
    ({"last_heartbeat_at": "2026-09-23T00:00:00+00:00"}, "UNLAUNCHED_RUN_TRACED"),
])
def test_retirement_refuses_anything_but_a_quiet_unclaimed_never_launched_run(
        launcher, enabled, posture, code):
    repo, plan = prepared()
    run_id = _fail_the_launch(repo, plan, batch_ids(plan)[0])
    repo.runs[run_id].update(posture)
    _quiet(repo, run_id)
    before = dict(repo.runs[run_id])
    refused(code, _retire, repo, run_id)
    assert repo.runs[run_id] == before
    assert launcher.launched == []


def test_retirement_refuses_a_run_with_an_execution_or_paid_work(launcher, enabled):
    repo, plan = prepared()
    run_id = _fail_the_launch(repo, plan, batch_ids(plan)[0])
    repo.record_run_invocation(UUID(run_id), {"mode": "cloud_run", "execution": "exec-1"})
    _quiet(repo, run_id)
    refused("UNLAUNCHED_RUN_TRACED", _retire, repo, run_id)
    assert repo.runs[run_id]["status"] == "queued"


def test_a_worker_that_claims_first_keeps_the_run_and_a_retired_run_is_never_claimed(
        launcher, enabled):
    repo, plan = prepared()
    first, second, _third = batch_ids(plan)
    claimed = _fail_the_launch(repo, plan, first)
    _quiet(repo, claimed)
    repo.claim_run(UUID(claimed), "stray-worker")
    refused("UNLAUNCHED_RUN_WRONG_STATE", _retire, repo, claimed)
    assert (repo.runs[claimed]["status"], repo.runs[claimed]["worker_id"]) == (
        "starting", "stray-worker")
    # The other order: retired first, a late claim gets nothing.
    repo2, plan2 = prepared()
    retired = _fail_the_launch(repo2, plan2, batch_ids(plan2)[0])
    _quiet(repo2, retired)
    assert _retire(repo2, retired)["retired"] is True
    refused("RUN_ALREADY_CLAIMED", repo2.claim_run, UUID(retired), "late-worker")
    assert repo2.runs[retired]["status"] == "cancelled" and not repo2.runs[retired].get("worker_id")


def test_an_operator_releases_a_stale_never_launched_batch_and_the_plan_continues(monkeypatch):
    swarm_env(monkeypatch, **{CATALOG_EXECUTION_FLAG: "true", GOVERNMENT_READ_FLAG: "true",
                              CATALOG_PROMOTION_FLAG: "false", BATCHES: "true",
                              wsb.RUN_CANCELLATION_FLAG: "true",
                              "MILO_RATE_LIMIT_RUN_CREATION_PROJECT": "1000"})
    completions = FakeKimiCompletions()
    patch_client(monkeypatch, completions)
    repo, plan = prepared()
    first = batch_ids(plan)[0]
    held = _fail_the_launch(repo, plan, first, key="ui-stale-1")
    revise(repo, plan)

    # Revised past its batch: it can no longer be launched, nor cancelled, and
    # it holds the plan.
    progress = get_progress(repo, plan).json()
    assert (progress["revision"], progress["live"]["run_id"], progress["live"]["revision"]) == (
        2, held, 1)
    controls = progress["controls"]
    assert (controls["start"]["available"], controls["start"]["relaunch"],
            controls["start"]["blocked_by"], controls["cancel"]["available"]) == (
        False, False, "batch_running", False)
    stale = post_start(repo, plan, first, key="ui-stale-1")
    assert (stale.status_code, stale.json()["error"]["code"]) == (409, "WORK_SCOPE_STALE")
    cancel = _cancel(repo, held)
    assert (cancel.status_code, cancel.json()["error"]["code"]) == (409, "RUN_NOT_LAUNCHED")

    # The operator's guarded retirement: never while anything could be in
    # flight, never below the floor; then once, and idempotently.
    refused("UNLAUNCHED_RUN_NOT_QUIET", _retire, repo, held)
    refused("UNLAUNCHED_RUN_THRESHOLD_TOO_SHORT", _retire, repo, held, quiet=60)
    _quiet(repo, held)
    assert _retire(repo, held) == {"retired": True, "run_id": held, "status": "cancelled",
                                   "previous_status": "queued"}
    assert _retire(repo, held)["retired"] is False
    assert (repo.runs[held]["status"], repo.runs[held]["error"]["code"]) == (
        "cancelled", "RUN_NOT_LAUNCHED")
    terminal = [e for e in repo.run_events if e["run_id"] == held and e["event_type"] == "run_cancelled"]
    assert len(terminal) == 1 and terminal[0]["payload"] == {"code": "RUN_NOT_LAUNCHED"}
    # A worker started for it anyway finds a terminal run and executes nothing.
    assert worker_main.execute_run(UUID(held), repo) == 0
    assert completions.calls == []

    # Released: nothing started by itself. The head revision is prepared by the
    # operator's capture job, and its first batch starts on a person's request.
    progress = get_progress(repo, plan).json()
    assert (progress["live"], progress["status"]) == (None, "not_prepared")
    head = prepare_plan_head(repo, plan["work_scope_id"], user_id=str(USER))
    assert head["revision"] == 2
    head_plan = {**plan, "revision": head["revision"], "digest": head["digest"]}
    next_batch = head["batches"][0]["id"]
    assert get_progress(repo, plan).json()["controls"]["start"]["batch"]["batch_id"] == next_batch
    inline = InlineWorkerLauncher(repo)
    app.dependency_overrides[get_job_launcher] = lambda: inline
    try:
        continued = post_start(repo, head_plan, next_batch)
        assert continued.status_code == 202, continued.text
        assert continued.json()["created"] is True and continued.json()["run_id"] != held
        assert inline.launches == [continued.json()["run_id"]] and inline.exit_codes == [0]
        assert repo.runs[continued.json()["run_id"]]["status"] in {"completed", "partial_success"}
        assert completions.calls, "the next batch did its work"
    finally:
        app.dependency_overrides.clear()
    assert get_progress(repo, plan).json()["preparation"]["batches"]["settled"] == 1
    assert repo.runs[held]["status"] == "cancelled"


def test_the_seed_starts_through_the_same_service_path():
    repo, plan = prepared()
    started = start_batch_run(repo, plan)
    assert started["created"] is True
    assert started["binding"]["batch_id"] == batch_ids(plan)[0]
    assert repo.runs[started["run"]["id"]]["input"]["content"] == wsb.batch_instruction(
        unit_key="toyota", batch_number=1, batch_count=3, item_count=10, revision=1)
    assert ws.WORK_SCOPE_BATCHES_FLAG == wsb.WORK_SCOPE_BATCHES_FLAG == BATCHES

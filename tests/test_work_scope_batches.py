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
from backend.testing.work_scope_seed import seed_prepared_plan, start_batch_run
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
    # A launch that happened, or may have, is never offered again, and CAN be
    # cancelled: a worker exists, or may, to finalize the cancellation.
    for launch_state in ("launching", "launched", "launch_unknown"):
        controls = wsb._progress_view({**raw, "live": {**live, "launch_state": launch_state}})[
            "controls"]
        assert (controls["start"]["relaunch"], controls["cancel"]["available"]) == (False, True)


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
    conversation instead of the Mapping Plan is refused by the worker before
    any provider path exists."""
    swarm_env(monkeypatch, **{CATALOG_EXECUTION_FLAG: "true", GOVERNMENT_READ_FLAG: "true",
                              CATALOG_PROMOTION_FLAG: "false"})
    patch_client(monkeypatch, FakeKimiCompletions())
    repo, plan = prepared()
    inline = InlineWorkerLauncher(repo)
    app.dependency_overrides[get_job_launcher] = lambda: inline
    try:
        response = client(repo).post(f"/conversations/{plan['conversation_id']}/runs",
                                     json={"content": "map every Toyota", "metadata": {},
                                           "idempotency_key": "ui-chat-000002"},
                                     headers=as_user())
        assert response.status_code == 202
        run = repo.runs[response.json()["run_id"]]
        assert run["status"] == "failed"
        assert run["error"]["code"] == "GOVERNMENT_BATCH_REQUIRED"
    finally:
        app.dependency_overrides.clear()


def test_the_seed_starts_through_the_same_service_path():
    repo, plan = prepared()
    started = start_batch_run(repo, plan)
    assert started["created"] is True
    assert started["binding"]["batch_id"] == batch_ids(plan)[0]
    assert repo.runs[started["run"]["id"]]["input"]["content"] == wsb.batch_instruction(
        unit_key="toyota", batch_number=1, batch_count=3, item_count=10, revision=1)
    assert ws.WORK_SCOPE_BATCHES_FLAG == wsb.WORK_SCOPE_BATCHES_FLAG == BATCHES

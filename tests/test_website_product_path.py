"""The website's product path: V3 creation -> immutable identity -> canonical
finalization -> durable ProductOutcome -> API projection.

What the browser renders as "the result" must come from the canonical durable
product path and nothing else. These tests prove the API half of that:

* `GET /runs/{id}` states the canonical ProductOutcome the finalizer recorded
  atomically with the terminal status, and states NOTHING when no trustworthy
  record exists -- live run, non-product terminal, historical run, or a record
  that does not re-validate;
* the projection is never derived from `output` and never accepted from a
  request body;
* a conversation's run HISTORY is membership-scoped, bounded and carries each
  run's immutable identity and outcome, so a completed result outlives the
  browser session that started it;
* the isolated E2E worker terminalizes through `RunFinalizer` for BOTH engines,
  so what Playwright sees is produced by the production path.
"""

from __future__ import annotations

import time
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from backend.dependencies import get_job_launcher, get_repository
from backend.finalization import RunFinalizer, TerminalClaim
from backend.main import _safe_product_outcome, _safe_run_response, app
from backend.testing.memory_repository import MemoryRepository
from tests.run_factory import identity_kwargs


def member(user_id):
    return {"x-milo-auth-user-id": str(user_id)}


@pytest.fixture
def world(monkeypatch):
    for flag in ("MILO_ENABLE_RUN_CREATION", "MILO_ENABLE_RUN_CANCELLATION",
                 "MILO_ENABLE_EXECUTION_CONTROL"):
        monkeypatch.setenv(flag, "true")
    monkeypatch.setenv("MILO_MAX_COST_PER_RUN", "1.00")
    monkeypatch.setenv("MILO_MAX_MODEL_CALLS_PER_RUN", "150")
    monkeypatch.setenv("MILO_MAX_RUN_DURATION_SECONDS", "1800")
    repo = MemoryRepository()
    alice, bob = uuid4(), uuid4()
    v1_project, v2_project = str(uuid4()), str(uuid4())
    repo.seed_user(str(alice)); repo.seed_user(str(bob))
    repo.seed_project(v1_project, "alpha", "Alpha", [str(alice)])
    repo.seed_project(v2_project, "gamma", "Gamma", [str(alice)], workflow_key="swarm_v2")
    v1_conversation = repo.create_conversation(UUID(v1_project), "v1", alice)
    v2_conversation = repo.create_conversation(UUID(v2_project), "v2", alice)
    app.dependency_overrides[get_repository] = lambda: repo
    yield {"repo": repo, "alice": alice, "bob": bob,
           "v1_conversation": UUID(v1_conversation["id"]),
           "v2_conversation": UUID(v2_conversation["id"])}
    app.dependency_overrides.clear()


def create_run(repo, conversation_id, user_id, content="go"):
    created = repo.create_message_and_run(
        conversation_id, content, {}, user_id, f"key-{uuid4()}", f"fp-{uuid4()}",
        **identity_kwargs(repo, conversation_id))
    return UUID(created["run"]["id"])


def claim(repo, run_id):
    claimed = repo.claim_run(run_id, "worker-a")
    lease = {"worker_id": "worker-a", "attempt": claimed["attempt"], "lease_token": claimed["lease_token"]}
    repo.transition_run(run_id, "running", expected_worker_id="worker-a",
                        expected_attempt=lease["attempt"], expected_lease_token=lease["lease_token"])
    return lease


V1_DOCUMENT = {
    "manufacturer": "Alpha", "market": "IL", "period": "2019-2024", "status": "complete",
    "models": [{"canonical_model_name": "Alpha One", "verification_status": "verified"},
               {"canonical_model_name": "Alpha Two", "verification_status": "verified"}],
    "needs_review": [], "rejected": [], "failed_agents": [],
    "pipeline_quality": {"discovery": "success", "normalizer": "success", "technical_enrichment": "success",
                         "verifier": "success", "final_builder": "success", "data_depth": "full_technical"},
}


# ---------------------------------------------------------------------------
# 1. the ProductOutcome projection
# ---------------------------------------------------------------------------
def test_a_live_run_states_no_product_outcome_and_states_its_limits(world):
    repo = world["repo"]
    run_id = create_run(repo, world["v1_conversation"], world["alice"])
    body = TestClient(app).get(f"/runs/{run_id}", headers=member(world["alice"])).json()
    assert body["status"] == "queued"
    assert body["product_outcome"] is None
    assert body["limits"] == {"max_model_calls_per_run": 150, "max_total_tokens_per_run": None,
                              "max_cost_per_run": 1.0, "max_run_duration_seconds": 1800,
                              "max_agent_steps": None}


def test_the_canonical_finalizer_is_the_source_of_the_projected_outcome(world):
    """The finalizer writes the terminal status and the terminal event -- with
    the ProductOutcome record -- in one step, and the run read projects THAT
    record. `output` is untouched and is not what the projection reads."""
    repo = world["repo"]
    run_id = create_run(repo, world["v1_conversation"], world["alice"])
    lease = claim(repo, run_id)
    finalizer = RunFinalizer(repo=repo, run_id=run_id, engine="vehicle_catalog_v1", lease_ctx=lease)
    finalizer.finalize(TerminalClaim.product("vehicle_catalog_v1",
                                             {"status": "complete", "result": V1_DOCUMENT}))

    body = TestClient(app).get(f"/runs/{run_id}", headers=member(world["alice"])).json()
    assert body["status"] == "completed"
    outcome = body["product_outcome"]
    assert outcome["engine"] == "vehicle_catalog_v1"
    assert outcome["semantic_status"] == "complete"
    assert outcome["usability"] == "usable"
    assert outcome["result_kind"] == "usable_result"
    assert outcome["coverage"] == {"produced": 2, "outstanding": 0, "ratio": 1.0}
    assert outcome["blocking"] == []
    assert outcome["payload"]["present"] is True and len(outcome["payload"]["digest"]) == 64
    # The product payload itself is still served, untouched, beside the verdict.
    assert body["output"]["result"]["models"][0]["canonical_model_name"] == "Alpha One"


def test_a_partial_v1_document_projects_a_partial_outcome_with_its_blocking_facts(world):
    repo = world["repo"]
    run_id = create_run(repo, world["v1_conversation"], world["alice"])
    lease = claim(repo, run_id)
    document = dict(V1_DOCUMENT, status="partial_success",
                    models=[{"canonical_model_name": "Alpha One", "verification_status": "verified"},
                            {"canonical_model_name": "Alpha Two", "verification_status": "needs_review"}],
                    needs_review=[{"canonical_model_name": "Alpha Two"}])
    RunFinalizer(repo=repo, run_id=run_id, engine="vehicle_catalog_v1", lease_ctx=lease).finalize(
        TerminalClaim.product("vehicle_catalog_v1", {"status": "partial_success", "result": document}))
    body = TestClient(app).get(f"/runs/{run_id}", headers=member(world["alice"])).json()
    assert body["status"] == "partial_success"
    assert body["product_outcome"]["semantic_status"] == "partial"
    assert body["product_outcome"]["blocking"] == [{"code": "OUTSTANDING_REVIEW_ITEMS", "count": 1}]
    assert body["product_outcome"]["coverage"] == {"produced": 1, "outstanding": 1, "ratio": 0.5}


def test_a_non_product_terminal_projects_no_outcome(world):
    """A cancellation is a terminal, not a product: the finalizer records no
    ProductOutcome for it and the projection says so with `null`."""
    repo = world["repo"]
    run_id = create_run(repo, world["v2_conversation"], world["alice"])
    lease = claim(repo, run_id)
    repo.request_cancellation(run_id, "operator")
    RunFinalizer(repo=repo, run_id=run_id, engine="swarm_v2", lease_ctx=lease).finalize(
        TerminalClaim.cancelled("swarm_v2"))
    body = TestClient(app).get(f"/runs/{run_id}", headers=member(world["alice"])).json()
    assert body["status"] == "cancelled"
    assert body["product_outcome"] is None


def test_a_historical_terminal_without_a_recorded_outcome_projects_null(world):
    """A run finalized before the canonical path has a terminal event carrying
    no record. The projection does not derive one from `output`."""
    repo = world["repo"]
    run_id = create_run(repo, world["v1_conversation"], world["alice"])
    claim(repo, run_id)
    repo.append_run_event(run_id, "run_completed", {"message": "Run completed", "payload": {}})
    repo.mark_run_complete(run_id, {"status": "complete", "result": V1_DOCUMENT})
    body = TestClient(app).get(f"/runs/{run_id}", headers=member(world["alice"])).json()
    assert body["status"] == "completed"
    assert body["output"]["result"]["models"]  # the payload is there...
    assert body["product_outcome"] is None      # ...and is NOT read as a verdict


@pytest.mark.parametrize("record", [
    "complete",                                              # not an object
    {"semantic_status": "excellent"},                        # outside the vocabulary
    {"semantic_status": "complete", "coverage": {"produced": "many"}},  # uncountable
    {"semantic_status": "complete", "blocking": [{"code": "NOT_A_CODE", "count": 1}]},
])
def test_an_untrustworthy_recorded_outcome_is_omitted_not_partially_believed(world, record):
    repo = world["repo"]
    run_id = create_run(repo, world["v1_conversation"], world["alice"])
    claim(repo, run_id)
    repo.append_run_event(run_id, "run_completed",
                          {"message": "Run completed", "payload": {"product_outcome": record}})
    repo.mark_run_complete(run_id, {"status": "complete", "result": V1_DOCUMENT})
    body = TestClient(app).get(f"/runs/{run_id}", headers=member(world["alice"])).json()
    assert body["product_outcome"] is None


def test_the_projection_reads_the_latest_terminal_event_not_the_first(world):
    """A superseded event from an earlier attempt must not outrank the one the
    winning finalization wrote."""
    repo = world["repo"]
    run_id = create_run(repo, world["v1_conversation"], world["alice"])
    claim(repo, run_id)
    repo.append_run_event(run_id, "run_failed", {"message": "old", "payload": {
        "product_outcome": {"engine": "vehicle_catalog_v1", "semantic_status": "refused",
                            "coverage": {"produced": 0, "outstanding": 0}, "blocking": [],
                            "payload": {"present": False}}}})
    repo.append_run_event(run_id, "run_completed", {"message": "new", "payload": {
        "product_outcome": {"engine": "vehicle_catalog_v1", "semantic_status": "complete",
                            "coverage": {"produced": 2, "outstanding": 0}, "blocking": [],
                            "payload": {"present": True}}}})
    repo.mark_run_complete(run_id, {"status": "complete", "result": V1_DOCUMENT})
    body = TestClient(app).get(f"/runs/{run_id}", headers=member(world["alice"])).json()
    assert body["product_outcome"]["semantic_status"] == "complete"


def test_the_projection_never_reads_output_and_tolerates_a_repository_without_history():
    """Direct unit check: no repository (or one without the read) -> null, and
    a payload alone is never enough to produce a verdict."""
    run = {"id": str(uuid4()), "status": "completed",
           "output": {"status": "complete", "result": V1_DOCUMENT}}
    assert _safe_product_outcome(run, None) is None

    class NoHistory:
        pass
    assert _safe_product_outcome(run, NoHistory()) is None
    safe = _safe_run_response(run)
    assert safe["product_outcome"] is None and "limits" in safe


# ---------------------------------------------------------------------------
# 2. run history
# ---------------------------------------------------------------------------
def test_run_history_is_newest_first_bounded_and_carries_identity_and_outcome(world):
    repo = world["repo"]
    conversation = world["v1_conversation"]
    first = create_run(repo, conversation, world["alice"], "first")
    lease = claim(repo, first)
    RunFinalizer(repo=repo, run_id=first, engine="vehicle_catalog_v1", lease_ctx=lease).finalize(
        TerminalClaim.product("vehicle_catalog_v1", {"status": "complete", "result": V1_DOCUMENT}))
    # A terminal run no longer counts against the per-user cap, so a second
    # run in the same conversation is admissible.
    time.sleep(0.002)
    second = create_run(repo, conversation, world["alice"], "second")

    rows = TestClient(app).get(f"/conversations/{conversation}/runs", headers=member(world["alice"])).json()
    assert [row["id"] for row in rows] == [str(second), str(first)]
    assert rows[0]["status"] == "queued" and rows[0]["product_outcome"] is None
    assert rows[1]["status"] == "completed" and rows[1]["product_outcome"]["semantic_status"] == "complete"
    assert rows[1]["run_identity"]["workflow_key"] == "vehicle_catalog_v1"
    # The history is for choosing a run: payloads travel only on the run read.
    for row in rows:
        assert "output" not in row and "input" not in row and "lease_token" not in row

    limited = TestClient(app).get(f"/conversations/{conversation}/runs?limit=1", headers=member(world["alice"])).json()
    assert [row["id"] for row in limited] == [str(second)]


@pytest.mark.parametrize("limit", [0, 51, -1])
def test_run_history_refuses_an_unbounded_page(world, limit):
    response = TestClient(app).get(f"/conversations/{world['v1_conversation']}/runs?limit={limit}",
                                   headers=member(world["alice"]))
    assert response.status_code == 422


def test_run_history_is_membership_scoped(world):
    repo = world["repo"]
    conversation = world["v1_conversation"]
    create_run(repo, conversation, world["alice"])
    stranger = TestClient(app).get(f"/conversations/{conversation}/runs", headers=member(world["bob"]))
    assert stranger.status_code == 404
    assert "queued" not in stranger.text


def test_run_history_survives_a_new_session_by_being_durable_truth(world):
    """The browser forgets its stored run ids across a restart; the history
    endpoint answers from the database, so the same completed result is
    reachable with nothing but the conversation and the session."""
    repo = world["repo"]
    conversation = world["v2_conversation"]
    run_id = create_run(repo, conversation, world["alice"])
    lease = claim(repo, run_id)
    RunFinalizer(repo=repo, run_id=run_id, engine="swarm_v2", lease_ctx=lease).finalize(
        TerminalClaim.failure("swarm_v2", "ENGINE_FAILED", "safe message"))
    # Two independent clients, no shared state between them.
    for _ in range(2):
        rows = TestClient(app).get(f"/conversations/{conversation}/runs", headers=member(world["alice"])).json()
        assert rows[0]["id"] == str(run_id) and rows[0]["status"] == "failed"
        detail = TestClient(app).get(f"/runs/{run_id}", headers=member(world["alice"])).json()
        assert detail["error"] == {"code": "ENGINE_FAILED", "message": "safe message"}
        # A failure records no product outcome (static code only), so the
        # projection is honestly null rather than a manufactured verdict.
        assert detail["product_outcome"] is None


# ---------------------------------------------------------------------------
# 3. the isolated E2E worker uses the production terminal path
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("workflow_key,content,expected_status,expected_semantic", [
    ("vehicle_catalog_v1", "produce the final report", "completed", "complete"),
    ("vehicle_catalog_v1", "produce a partial report", "partial_success", "partial"),
    ("swarm_v2", "produce the final report", "completed", "complete"),
    ("swarm_v2", "produce a partial report please", "partial_success", "partial"),
    ("vehicle_catalog_v1", "please fail", "failed", None),
])
def test_the_e2e_worker_terminalizes_through_the_canonical_finalizer(
        world, workflow_key, content, expected_status, expected_semantic):
    from backend.testing.e2e_app import InProcessFakeWorkerLauncher

    repo = world["repo"]
    # Importing the E2E module installs ITS repository on the shared app; this
    # test reads through the world's repository, so the override is restated.
    app.dependency_overrides[get_repository] = lambda: repo
    conversation = world["v1_conversation"] if workflow_key == "vehicle_catalog_v1" else world["v2_conversation"]
    run_id = create_run(repo, conversation, world["alice"], content)
    InProcessFakeWorkerLauncher(repo).launch(run_id)
    deadline = time.time() + 15
    while time.time() < deadline:
        run = repo.get_run(run_id)
        if run["status"] in {"completed", "partial_success", "failed"}:
            break
        time.sleep(0.05)
    assert run["status"] == expected_status, run
    # The terminal event exists and carries the canonical record: the ONE
    # atomic write the finalizer makes, not a test-only emit.
    terminal = repo.terminal_run_event(run_id)
    assert terminal is not None and terminal["event_type"].startswith("run_")
    response = TestClient(app).get(f"/runs/{run_id}", headers=member(world["alice"]))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["run_identity"]["workflow_key"] == workflow_key
    if expected_semantic is None:
        # A failure has no product to describe: the finalizer records only the
        # static code on its event, so the projection states no verdict.
        assert "product_outcome" not in terminal["payload"]
        assert terminal["payload"]["code"] == "ENGINE_FAILED"
        assert body["product_outcome"] is None
        assert body["error"]["code"] == "ENGINE_FAILED"
        return
    assert terminal["payload"]["product_outcome"]["semantic_status"] == expected_semantic
    assert body["product_outcome"]["semantic_status"] == expected_semantic
    assert body["output"], "the product payload is durable beside the verdict"


def test_the_e2e_worker_honours_a_cancellation_through_the_finalizer(world):
    from backend.testing.e2e_app import InProcessFakeWorkerLauncher

    repo = world["repo"]
    app.dependency_overrides[get_repository] = lambda: repo
    run_id = create_run(repo, world["v1_conversation"], world["alice"], "slow work")
    InProcessFakeWorkerLauncher(repo).launch(run_id)
    deadline = time.time() + 5
    while time.time() < deadline and repo.get_run(run_id)["status"] != "running":
        time.sleep(0.02)
    repo.request_cancellation(run_id, "operator")
    deadline = time.time() + 10
    while time.time() < deadline and repo.get_run(run_id)["status"] != "cancelled":
        time.sleep(0.05)
    assert repo.get_run(run_id)["status"] == "cancelled"
    assert repo.terminal_run_event(run_id)["event_type"] == "run_cancelled"
    body = TestClient(app).get(f"/runs/{run_id}", headers=member(world["alice"])).json()
    assert body["product_outcome"] is None


# ---------------------------------------------------------------------------
# 4. simultaneous website runs cannot bypass the run-level caps
# ---------------------------------------------------------------------------
@pytest.fixture
def capped_world(world, monkeypatch):
    monkeypatch.setenv("MILO_MAX_CONCURRENT_RUNS_PER_USER", "1")
    monkeypatch.setenv("MILO_MAX_CONCURRENT_RUNS_PER_PROJECT", "1")
    return world


def _post_run(conversation_id, user_id, content, key):
    return TestClient(app).post(
        f"/conversations/{conversation_id}/runs",
        json={"content": content, "metadata": {}, "idempotency_key": key},
        headers=member(user_id))


def test_a_second_website_run_is_refused_while_the_first_is_active(capped_world):
    """The per-user cap is enforced from a DATABASE count of active runs,
    server-side, before any row is written -- so two browser tabs, two
    devices or two conversations cannot add up past it."""
    world = capped_world
    first = _post_run(world["v1_conversation"], world["alice"], "first", "website-key-0001")
    assert first.status_code == 202, first.text
    second = _post_run(world["v2_conversation"], world["alice"], "second", "website-key-0002")
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "USER_CONCURRENCY_LIMIT"
    # Nothing was written for the refused request.
    rows = TestClient(app).get(f"/conversations/{world['v2_conversation']}/runs", headers=member(world["alice"])).json()
    assert rows == []


def test_the_cap_is_released_only_by_a_terminal_state(capped_world):
    world = capped_world
    repo = world["repo"]
    first = _post_run(world["v1_conversation"], world["alice"], "first", "website-key-0003")
    run_id = UUID(first.json()["run_id"])
    lease = claim(repo, run_id)
    RunFinalizer(repo=repo, run_id=run_id, engine="vehicle_catalog_v1", lease_ctx=lease).finalize(
        TerminalClaim.product("vehicle_catalog_v1", {"status": "complete", "result": V1_DOCUMENT}))
    second = _post_run(world["v1_conversation"], world["alice"], "second", "website-key-0004")
    assert second.status_code == 202, second.text
    rows = TestClient(app).get(f"/conversations/{world['v1_conversation']}/runs", headers=member(world["alice"])).json()
    assert [row["status"] for row in rows] == ["queued", "completed"]


def test_an_idempotent_replay_returns_the_same_run_rather_than_a_second_one(capped_world):
    world = capped_world
    first = _post_run(world["v1_conversation"], world["alice"], "same", "website-key-0005")
    replay = _post_run(world["v1_conversation"], world["alice"], "same", "website-key-0005")
    assert first.status_code == 202 and replay.status_code in (200, 202)
    assert replay.json()["run_id"] == first.json()["run_id"]
    rows = TestClient(app).get(f"/conversations/{world['v1_conversation']}/runs", headers=member(world["alice"])).json()
    assert len(rows) == 1


def test_runs_are_isolated_per_conversation_and_per_member(world):
    """A run's history and product are visible only through ITS conversation
    and only to a member of ITS project; another conversation's history does
    not carry it and a non-member cannot read it at all."""
    repo = world["repo"]
    v1_run = create_run(repo, world["v1_conversation"], world["alice"], "v1")
    v2_run = create_run(repo, world["v2_conversation"], world["alice"], "v2")
    v1_rows = TestClient(app).get(f"/conversations/{world['v1_conversation']}/runs", headers=member(world["alice"])).json()
    v2_rows = TestClient(app).get(f"/conversations/{world['v2_conversation']}/runs", headers=member(world["alice"])).json()
    assert [row["id"] for row in v1_rows] == [str(v1_run)]
    assert [row["id"] for row in v2_rows] == [str(v2_run)]
    assert v1_rows[0]["run_identity"]["workflow_key"] == "vehicle_catalog_v1"
    assert v2_rows[0]["run_identity"]["workflow_key"] == "swarm_v2"
    for run_id in (v1_run, v2_run):
        assert TestClient(app).get(f"/runs/{run_id}", headers=member(world["bob"])).status_code == 404

"""GET /runs/{id} must return the durable aggregate usage, and only that.

`public.runs.usage` is `jsonb NOT NULL DEFAULT '{}'` (migration 010) and is
written exclusively from `BudgetTracker.snapshot()`. These tests pin the public
response contract for it: the authorized browser read exposes the persisted
aggregate through the typed `RunUsage` schema, an unsettled run reports nothing
rather than zero spend, and no field outside the allowlist can escape.

They reuse the existing FakeRepo/`repo` API fixture, so they exercise the same
authenticated path the browser uses.
"""

import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from backend.budget import BudgetConfig, BudgetTracker
from backend.main import app
from backend.schemas import RunUsage
from tests.test_api import repo  # noqa: F401  (pytest fixture)

# The accepted Swarm V2 production-smoke aggregate. The input/output split is
# this test's own choice; only the totals below are historical.
SMOKE_USAGE = {
    "model_calls": 7,
    "input_tokens": 7_120,
    "output_tokens": 3_250,
    "total_tokens": 10_370,
    "estimated_cost": 0.02,
    "actual_cost": 0.019178,
    "retries": 0,
    "provider_backpressure_events": 0,
    "agent_steps": 7,
    "elapsed_seconds": 41.5,
}

PUBLIC_USAGE_FIELDS = {
    "model_calls",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "estimated_cost",
    "actual_cost",
    "retries",
    "provider_backpressure_events",
    "agent_steps",
    "elapsed_seconds",
}


def member(repo):
    return {"x-milo-auth-user-id": str(repo.user_id)}


def read_run(repo, headers=None):
    return TestClient(app).get(f"/runs/{repo.run_id}", headers=headers or member(repo))


def with_stored_run(repo, **fields):
    """Make the fake repository's authorized run carry extra durable columns."""
    original = repo.get_run

    def get_run(run_id, user_id=None):
        return {**original(run_id, user_id), **fields}

    repo.get_run = get_run
    return repo


def test_run_usage_contract_matches_the_live_budget_snapshot():
    """The public schema and BudgetTracker.snapshot() may not drift apart."""
    tracker = BudgetTracker(BudgetConfig(), kill_switch=lambda: False)
    assert set(tracker.snapshot()) == PUBLIC_USAGE_FIELDS
    assert set(RunUsage.model_fields) == PUBLIC_USAGE_FIELDS
    # A real snapshot validates cleanly against the public contract.
    assert RunUsage.model_validate(tracker.snapshot()).model_calls == 0


def test_a_authorized_read_returns_the_persisted_usage(repo):  # noqa: F811
    with_stored_run(repo, usage=SMOKE_USAGE)
    response = read_run(repo)
    assert response.status_code == 200
    usage = response.json()["usage"]
    assert usage["model_calls"] == 7
    assert usage["input_tokens"] + usage["output_tokens"] == 10_370
    assert usage["total_tokens"] == 10_370
    assert usage["actual_cost"] == 0.019178
    assert usage["retries"] == 0
    assert usage["provider_backpressure_events"] == 0


def test_b_returned_usage_is_numeric_and_only_public_fields(repo):  # noqa: F811
    with_stored_run(repo, usage=SMOKE_USAGE)
    usage = read_run(repo).json()["usage"]
    assert set(usage) == PUBLIC_USAGE_FIELDS
    for name, value in usage.items():
        assert isinstance(value, (int, float)) and not isinstance(value, bool), name
        assert value >= 0, name


@pytest.mark.parametrize("stored", [{}, None])
def test_c_unsettled_usage_reports_nothing_rather_than_zero_spend(repo, stored):  # noqa: F811
    """`{}` is the NOT NULL default for a run that has not settled a call."""
    with_stored_run(repo, usage=stored)
    body = read_run(repo).json()
    assert body["usage"] is None
    # Absent must never be rendered as a zeroed aggregate.
    assert body["usage"] != {name: 0 for name in PUBLIC_USAGE_FIELDS}


def test_c_a_run_row_without_a_usage_column_still_reads(repo):  # noqa: F811
    """The stock fake returns no `usage` key at all; the read must not break."""
    response = read_run(repo)
    assert response.status_code == 200
    assert response.json()["usage"] is None


def test_d_existing_fields_and_launch_sanitization_are_unchanged(repo):  # noqa: F811
    with_stored_run(
        repo,
        usage=SMOKE_USAGE,
        launch_state="launch_unknown",
        launch_error="Traceback: provider host 10.0.0.1 refused connection",
        lease_token="lease-secret-token",
        status="partial_success",
    )
    body = read_run(repo).json()
    assert body["status"] == "partial_success"
    assert body["launch_state"] == "launch_unknown"
    assert body["launch_error_class"] == "worker_launch_unknown"
    assert body["launch_reconciliation_required"] is True
    assert "launch_error" not in body
    assert "lease_token" not in body
    payload = read_run(repo).text
    assert "Traceback" not in payload
    assert "10.0.0.1" not in payload
    assert "lease-secret-token" not in payload


def test_e_unauthorized_and_non_member_reads_remain_denied(repo):  # noqa: F811
    with_stored_run(repo, usage=SMOKE_USAGE)
    client = TestClient(app)
    # No identity at all.
    assert client.get(f"/runs/{repo.run_id}").status_code == 401
    # Authenticated, but not a member of the run's project.
    stranger = client.get(f"/runs/{repo.run_id}", headers={"x-milo-auth-user-id": str(uuid4())})
    assert stranger.status_code == 404
    assert "usage" not in stranger.text
    # A different run id the member does not own.
    assert client.get(f"/runs/{uuid4()}", headers=member(repo)).status_code == 404


def test_f_internal_fields_cannot_escape_through_the_typed_usage(repo):  # noqa: F811
    """Extra keys stored beside the aggregate are ignored, not forwarded."""
    with_stored_run(
        repo,
        usage={
            **SMOKE_USAGE,
            "provider": "moonshot",
            "model": "kimi-k2",
            "api_key": "placeholder-never-returned",
            "prompt": "verify claim-1 against source-1",
            "evidence_fragment": "the catalog lists 41 trims",
            "lease_token": "lease-secret-token",
            "reservation_id": "res-123",
            "ledger_rows": [{"id": 1, "cost": 0.5}],
        },
    )
    body = read_run(repo).json()
    assert set(body["usage"]) == PUBLIC_USAGE_FIELDS
    payload = read_run(repo).text
    for leaked in (
        "moonshot",
        "kimi-k2",
        "placeholder-never-returned",
        "verify claim-1",
        "41 trims",
        "lease-secret-token",
        "res-123",
        "ledger_rows",
    ):
        assert leaked not in payload


def test_f_an_object_of_only_unknown_keys_projects_onto_nothing(repo):  # noqa: F811
    with_stored_run(repo, usage={"provider": "moonshot", "api_key": "placeholder-secret"})
    body = read_run(repo).json()
    assert body["usage"] is None
    assert "placeholder-secret" not in read_run(repo).text


@pytest.mark.parametrize(
    "stored",
    [
        {"model_calls": -1},
        {"actual_cost": -0.5},
        {"model_calls": "seven"},
        {"total_tokens": [10_370]},
        "not-an-object",
        7,
    ],
)
def test_an_unrepresentable_stored_value_degrades_instead_of_failing_the_read(repo, stored):  # noqa: F811
    """The polled run read must not 500 on an unexpected stored usage value."""
    with_stored_run(repo, usage=stored)
    response = read_run(repo)
    assert response.status_code == 200
    assert response.json()["usage"] is None


def test_g_terminal_runs_still_carry_usage(repo):  # noqa: F811
    for status in ("completed", "partial_success", "failed", "cancelled", "timed_out", "budget_exhausted"):
        with_stored_run(repo, usage=SMOKE_USAGE, status=status)
        body = read_run(repo).json()
        assert body["status"] == status
        assert body["usage"]["model_calls"] == 7
        assert body["usage"]["total_tokens"] == 10_370


def test_partial_usage_is_preserved_without_being_completed(repo):  # noqa: F811
    """A snapshot missing fields stays missing; nothing is filled in with 0."""
    with_stored_run(repo, usage={"model_calls": 3, "total_tokens": 512})
    usage = read_run(repo).json()["usage"]
    assert usage["model_calls"] == 3
    assert usage["total_tokens"] == 512
    assert usage["actual_cost"] is None
    assert usage["retries"] is None


# --- Shared cross-language contract fixture -------------------------------
#
# frontend/tests/fixtures/runResponseContract.json holds recorded GET
# /runs/{id} bodies. The frontend parses that same file with lib/runUsage.ts,
# so if this response shape ever changes without the fixture being updated,
# this test fails here rather than the drift being discovered in a browser.

CONTRACT_FIXTURE = Path(__file__).resolve().parents[1] / "frontend" / "tests" / "fixtures" / "runResponseContract.json"


def _recorded(name):
    return json.loads(CONTRACT_FIXTURE.read_text())[name]


def _rendered(repo, recorded):  # noqa: F811
    """Re-render `recorded` through the real app, keyed to this fake's ids."""
    with_stored_run(repo, usage=recorded["usage"], status=recorded["status"])
    body = read_run(repo).json()
    # Ids are per-run; the fixture uses fixed placeholders for them.
    return {**body, "id": recorded["id"], "conversation_id": recorded["conversation_id"]}


def test_h_recorded_settled_response_still_matches_the_live_app(repo):  # noqa: F811
    recorded = _recorded("settled")
    assert _rendered(repo, recorded) == recorded


def test_h_recorded_unsettled_response_still_matches_the_live_app(repo):  # noqa: F811
    recorded = _recorded("unsettled")
    # The unsettled fixture records `usage: null`; the durable row holds `{}`.
    with_stored_run(repo, usage={}, status=recorded["status"])
    body = read_run(repo).json()
    assert {**body, "id": recorded["id"], "conversation_id": recorded["conversation_id"]} == recorded


def test_h_fixture_ids_are_well_formed(repo):  # noqa: F811
    for name in ("settled", "unsettled"):
        recorded = _recorded(name)
        UUID(recorded["id"])
        UUID(recorded["conversation_id"])


# --- Real repository path -------------------------------------------------
#
# The tests above drive a hand-written fake. These drive MemoryRepository, the
# same implementation the E2E stack runs, so the `{}` NOT NULL default and the
# real `update_run_usage` write are exercised rather than assumed.


@pytest.fixture
def memory_repo(monkeypatch):
    from backend.dependencies import get_repository
    from backend.testing.memory_repository import MemoryRepository

    for flag in ("MILO_ENABLE_RUN_CREATION", "MILO_ENABLE_EXECUTION_CONTROL"):
        monkeypatch.delenv(flag, raising=False)
    repo = MemoryRepository()
    repo.user_id = uuid4()
    repo.stranger_id = uuid4()
    project_id = str(uuid4())
    repo.seed_user(str(repo.user_id))
    repo.seed_project(project_id, "alpha", "Alpha", [str(repo.user_id)])
    conversation = repo.create_conversation(UUID(project_id), "c", repo.user_id)
    message = repo.create_user_message(UUID(conversation["id"]), "go", {})
    run = repo.create_queued_run(
        UUID(conversation["id"]), message["id"], "go", {}, requested_by=repo.user_id
    )
    repo.run_id = UUID(run["id"])
    app.dependency_overrides[get_repository] = lambda: repo
    yield repo
    app.dependency_overrides.clear()


def test_real_repository_defaults_a_new_run_to_no_recorded_usage(memory_repo):
    """create_queued_run stores `{}`, mirroring the NOT NULL DEFAULT."""
    assert memory_repo.runs[str(memory_repo.run_id)]["usage"] == {}
    body = read_run(memory_repo).json()
    assert body["usage"] is None


def test_real_repository_usage_write_reaches_the_authorized_browser(memory_repo):
    memory_repo.update_run_usage(memory_repo.run_id, dict(SMOKE_USAGE))
    body = read_run(memory_repo).json()
    assert body["usage"] == SMOKE_USAGE
    assert set(body["usage"]) == PUBLIC_USAGE_FIELDS


def test_real_repository_usage_is_still_denied_to_non_members(memory_repo):
    memory_repo.update_run_usage(memory_repo.run_id, dict(SMOKE_USAGE))
    stranger = TestClient(app).get(
        f"/runs/{memory_repo.run_id}",
        headers={"x-milo-auth-user-id": str(memory_repo.stranger_id)},
    )
    assert stranger.status_code == 404
    assert "model_calls" not in stranger.text


def test_real_budget_tracker_snapshot_survives_the_round_trip(memory_repo):
    """A snapshot produced by the real tracker reaches the browser intact."""
    tracker = BudgetTracker(BudgetConfig(), kill_switch=lambda: False)
    snapshot = tracker.snapshot()
    memory_repo.update_run_usage(memory_repo.run_id, snapshot)
    usage = read_run(memory_repo).json()["usage"]
    assert set(usage) == set(snapshot)
    # A tracker that has done nothing reports explicit zeroes, which is a
    # different durable fact from `{}` and must not be flattened to null.
    assert usage["model_calls"] == 0
    assert usage["total_tokens"] == 0

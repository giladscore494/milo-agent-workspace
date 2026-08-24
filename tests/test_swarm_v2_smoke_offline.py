"""Offline Swarm V2 smoke simulation with provider-shaped Kimi fakes.

Recreates the production smoke end to end — trusted gateway request →
membership/routing → run creation + launch CAS → worker claim/lease →
real SwarmV2 engine over the REAL budget guard and provider scheduler →
durable terminal persistence — with an OpenAI-compatible fake that mirrors
the exact Moonshot/Kimi completion and usage envelopes. No network, no
paid call: the provider client is process-local.

Proves the two mandatory smoke outcomes:
  * one successful minimal no-tool Swarm run reaching status=completed;
  * one safely classified Commander failure with exactly one Worker
    attempt and no effective retry (a simulated Cloud Run retry is a
    no-op success, never RUN_ALREADY_CLAIMED).
plus fault injection at the persistence, lease, model, scheduler, budget
and checkpoint boundaries.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

import backend.worker.main as worker_main
from backend.budget import build_guarded_client_factory
from backend.dependencies import get_job_launcher, get_repository
from backend.errors import AppError
from backend.main import app
from backend.testing.memory_repository import MemoryRepository

USER = "aaaaaaaa-2222-4222-8222-000000000001"
PROJECT = "bbbbbbbb-2222-4222-8222-000000000001"


# --- provider-shaped Kimi fakes ---------------------------------------------

def kimi_usage(prompt=120, completion=40):
    """The exact envelope Moonshot returns: token counts, no cost field."""
    return SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion,
                           total_tokens=prompt + completion)


def kimi_response(content, usage=kimi_usage(), tool_calls=None, refusal=None):
    message = SimpleNamespace(role="assistant", content=content,
                              tool_calls=tool_calls, refusal=refusal)
    choice = SimpleNamespace(index=0, message=message, finish_reason="stop")
    return SimpleNamespace(id="chatcmpl-offline", model="kimi-k2.6",
                           choices=[choice], usage=usage)


def minimal_plan(num_tasks=2):
    tasks = [{
        "task_id": f"t{i}", "goal": f"answer smoke question {i}", "scope": "smoke",
        "dependencies": [], "tools": [],
        "output_schema": {"type": "object", "properties": {"answer": {"type": "string"}},
                          "required": ["answer"], "additionalProperties": False},
        "evidence": {"minimum_sources": 0, "required_fields": [], "min_confidence": 0.0},
        "priority": 1, "recursion_depth": 0, "estimated_cost_units": 1,
        "completion": {"required_outputs": ["answer"], "evidence_satisfied": False,
                       "allow_partial": False},
    } for i in range(num_tasks)]
    return {"version": "1", "objective": "smoke objective", "graph": {"tasks": tasks},
            "assignments": [{"task_id": t["task_id"], "worker_role": "generalist",
                             "context_task_ids": []} for t in tasks],
            "max_replans": 1, "estimated_cost_units": 10}


class FakeKimiCompletions:
    """Dispatches on the real prompts the gateway sends for each role."""

    def __init__(self, plan_body=None, worker_body=None, decision_body=None,
                 plan_exception=None):
        self.plan_body = plan_body if plan_body is not None else json.dumps(minimal_plan())
        self.worker_body = worker_body if worker_body is not None else json.dumps({"answer": "42"})
        self.decision_body = decision_body if decision_body is not None else json.dumps(
            {"decision": "FINISH", "plan": None, "reason": "complete"})
        self.plan_exception = plan_exception
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        system = " ".join(m.get("content", "") for m in kwargs.get("messages", [])
                          if m.get("role") == "system")
        if "CommanderPlan JSON Schema" in system:
            if self.plan_exception is not None:
                raise self.plan_exception
            return kimi_response(self.plan_body)
        if "CommanderDecision JSON Schema" in system:
            return kimi_response(self.decision_body)
        return kimi_response(self.worker_body)


def fake_kimi_client(completions):
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


# --- offline stack ----------------------------------------------------------

def swarm_env(monkeypatch, **overrides):
    """The deployed worker contract, minus real credentials and pacing."""
    values = {
        "MILO_ENABLE_PAID_EXECUTION": "true",
        "KIMI_API_KEY": "offline-test-key-not-a-secret",
        "MILO_COMMANDER_MODEL_ALLOWLIST": "kimi-k2.6",
        "MILO_COMMANDER_MODEL": "kimi-k2.6",
        "MILO_SWARM_WORKER_MODEL": "kimi-k2.6",
        "MILO_MODEL_BASE_URL": "https://api.moonshot.ai/v1",
        "MILO_MAX_MODEL_CALLS_PER_RUN": "200",
        "MILO_MAX_TOTAL_TOKENS_PER_RUN": "900000",
        "MILO_MAX_ESTIMATED_COST_PER_RUN": "4.00",
        "MILO_MAX_COST_PER_RUN": "3.00",
        "MILO_MAX_RUN_DURATION_SECONDS": "3300",
        "MILO_MAX_RETRIES": "15",
        "MILO_ESTIMATED_COST_PER_CALL": "0.02",
        "MILO_DAILY_USER_BUDGET": "5.00",
        "MILO_SWARM_MAX_ACTIVE_WORKERS": "8",
        "MILO_PROVIDER_MAX_CONCURRENCY": "8",
        "MILO_PROVIDER_RPM_LIMIT": "100000",
        "MILO_ENABLE_RUN_CREATION": "true",
        # The in-memory API limiter is keyed by the module-constant user;
        # raise the per-minute cap so unrelated tests never trip it.
        "MILO_RATE_LIMIT_RUN_CREATION_USER": "100",
    }
    values.update(overrides)
    for key, value in values.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)


def build_repo():
    repo = MemoryRepository()
    repo.seed_user(USER)
    repo.seed_project(PROJECT, "swarm-smoke", "Swarm Smoke", [USER])
    repo.projects[PROJECT]["workflow_key"] = "swarm_v2"
    conversation = repo.create_conversation(UUID(PROJECT), "smoke", UUID(USER))
    return repo, conversation["id"]


class InlineWorkerLauncher:
    """Runs the real worker entrypoint synchronously in place of Cloud Run."""

    def __init__(self, repo):
        self.repo = repo
        self.launches = []
        self.exit_codes = []

    def launch(self, run_id):
        self.launches.append(str(run_id))
        self.exit_codes.append(worker_main.execute_run(run_id, self.repo))
        return {"mode": "inline-test", "run_id": str(run_id), "execution": f"inline-{len(self.launches)}"}


def patch_client(monkeypatch, completions):
    monkeypatch.setattr(
        worker_main, "build_guarded_client_factory",
        lambda tracker: build_guarded_client_factory(
            tracker, inner_factory=lambda api_key, base_url: fake_kimi_client(completions)),
    )


@pytest.fixture()
def offline_stack(monkeypatch):
    swarm_env(monkeypatch)
    repo, conversation_id = build_repo()
    launcher = InlineWorkerLauncher(repo)
    app.dependency_overrides[get_repository] = lambda: repo
    app.dependency_overrides[get_job_launcher] = lambda: launcher
    try:
        yield repo, conversation_id, launcher
    finally:
        app.dependency_overrides.clear()


def create_run(client, conversation_id, idempotency_key="smoke-000001"):
    return client.post(
        f"/conversations/{conversation_id}/runs",
        json={"content": "minimal no-tool swarm smoke", "metadata": {},
              "idempotency_key": idempotency_key},
        headers={"x-milo-auth-user-id": USER},
    )


# --- the two mandatory smoke outcomes ---------------------------------------

def test_offline_smoke_gateway_to_completed_terminal_state(offline_stack, monkeypatch):
    repo, conversation_id, launcher = offline_stack
    completions = FakeKimiCompletions()
    patch_client(monkeypatch, completions)

    response = create_run(TestClient(app), conversation_id)
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]

    assert launcher.exit_codes == [0]
    run = repo.get_run(UUID(run_id))
    assert run["status"] == "completed"
    assert run["output"]["status"] == "complete"
    assert run["attempt"] == 1
    # plan + 2 tasks + replan; the verifier is skipped with no evidence.
    assert len(completions.calls) == 4
    assert run["usage"]["model_calls"] == 4
    assert run["usage"]["retries"] == 0
    assert run["usage"]["actual_cost"] > 0

    event_types = [e["event_type"] for e in repo.run_events if str(e["run_id"]) == run_id]
    for expected in ("run_started", "commander_plan_created", "task_started",
                     "task_completed", "verification_completed", "run_completed"):
        assert expected in event_types, event_types

    checkpoints = [c for c in repo.checkpoints if str(c.get("run_id")) == run_id]
    assert checkpoints, "swarm run must persist checkpoints"
    for checkpoint in checkpoints:
        # The exact NOT NULL columns of production run_checkpoints.
        assert checkpoint.get("engine_version") == "swarm_v2.1"
        assert checkpoint.get("workflow_key") == "swarm_v2"
        assert checkpoint.get("phase") == "swarm_v2"

    # Zero dangling budget reservations: every daily-budget reservation row
    # (they carry a status field; tracker telemetry rows do not) was settled.
    ledger = getattr(repo, "usage_ledger", [])
    reservations = [r for r in ledger if r.get("status") == "reserved"]
    assert reservations and all(r.get("settled") for r in reservations)

    # The provider key never reaches any durable surface.
    durable = json.dumps({"runs": {k: {kk: str(vv) for kk, vv in v.items()} for k, v in repo.runs.items()},
                          "events": [str(e) for e in repo.run_events],
                          "checkpoints": [str(c) for c in repo.checkpoints]})
    assert "offline-test-key-not-a-secret" not in durable

    # Idempotent replay: same key returns the same run without a second launch.
    replay = create_run(TestClient(app), conversation_id)
    assert replay.status_code == 202
    assert replay.json()["run_id"] == run_id
    assert launcher.launches == [run_id]


def test_offline_smoke_classified_commander_failure_one_attempt_no_retry(offline_stack, monkeypatch):
    repo, conversation_id, launcher = offline_stack
    completions = FakeKimiCompletions(plan_body="this is not json {{")
    patch_client(monkeypatch, completions)

    response = create_run(TestClient(app), conversation_id, idempotency_key="smoke-fail-0001")
    assert response.status_code == 202
    run_id = response.json()["run_id"]

    # The worker handled the failure: exit 0, durable classified terminal.
    assert launcher.exit_codes == [0]
    run = repo.get_run(UUID(run_id))
    assert run["status"] == "failed"
    assert run["error"] == {"code": "COMMANDER_PLAN_JSON_INVALID",
                            "message": "Commander plan JSON is invalid"}
    assert run["attempt"] == 1
    # Exactly one bounded in-run semantic repair: two paid Commander calls,
    # never three, counted as one semantic retry inside the same attempt.
    assert len(completions.calls) == 2
    assert run["usage"]["model_calls"] == 2
    assert run["usage"]["retries"] == 1
    repair_system = " ".join(m.get("content", "")
                             for m in completions.calls[1]["messages"]
                             if m.get("role") == "system")
    assert "JSON_DECODE_FAILED" in repair_system
    assert "this is not json {{" not in repair_system

    # The run_failed event carries the bounded static classification only.
    failed_events = [e for e in repo.run_events if str(e["run_id"]) == run_id
                     and e["event_type"] == "run_failed"]
    assert failed_events
    assert failed_events[-1]["payload"] == {
        "code": "COMMANDER_PLAN_JSON_INVALID",
        "validation_reason": "JSON_DECODE_FAILED"}

    # Every daily-budget reservation settled: no dangling reservation.
    reservations = [r for r in getattr(repo, "usage_ledger", [])
                    if r.get("status") == "reserved"]
    assert reservations and all(r.get("settled") for r in reservations)

    # A Cloud Run retry against the durably finalized run is a no-op success
    # — never a RUN_ALREADY_CLAIMED failure loop.
    events_before = len(repo.run_events)
    assert worker_main.execute_run(UUID(run_id), repo) == 0
    run = repo.get_run(UUID(run_id))
    assert run["status"] == "failed"
    assert run["attempt"] == 1
    assert len(completions.calls) == 2
    assert len(repo.run_events) == events_before


# --- Kimi envelope edge cases through the real gateway ----------------------

def gateway_for(completions, monkeypatch=None, tracker=None):
    from backend.budget import BudgetConfig, BudgetTracker
    from backend.engines.swarm_v2 import ModelGateway
    from backend.provider_scheduler import ProviderLimitsConfig, ProviderScheduler

    tracker = tracker or BudgetTracker(BudgetConfig(), kill_switch=lambda: True)
    scheduler = ProviderScheduler(ProviderLimitsConfig(
        max_concurrency=8, rpm_limit=None, tpm_limit=None,
        max_rate_limit_retries=1, max_backpressure_wait_seconds=1.0,
        backoff_base_seconds=0.01, backoff_max_seconds=0.01))
    return ModelGateway(
        guarded_client_factory=build_guarded_client_factory(
            tracker, inner_factory=lambda key, url: fake_kimi_client(completions)),
        scheduler=scheduler, api_key="offline", base_url="https://api.moonshot.ai/v1")


class ScriptedCompletions:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.mark.parametrize("response", [
    kimi_response(None),                       # null content
    kimi_response(""),                         # empty content
    kimi_response(["chunked", "content"]),     # list content
    kimi_response(None, refusal="I cannot"),  # refusal envelope
    kimi_response(None, tool_calls=[SimpleNamespace(  # unexpected tool call
        id="c1", type="builtin_function",
        function=SimpleNamespace(name="$web_search", arguments="{}"))]),
])
def test_gateway_rejects_non_string_plan_envelopes_with_stable_code(response):
    from backend.engines.swarm_v2 import CommanderPlanFailure

    gateway = gateway_for(ScriptedCompletions(response))
    with pytest.raises(CommanderPlanFailure) as failure:
        gateway.create_plan(model="kimi-k2.6", objective="o", context={})
    assert failure.value.code == "COMMANDER_COMPLETION_SHAPE_INVALID"


@pytest.mark.parametrize("usage", [
    None,                                        # usage entirely absent
    SimpleNamespace(prompt_tokens=7),            # partial usage
    {"prompt_tokens": 7, "completion_tokens": 3},  # dict-shaped usage
])
def test_guarded_client_tolerates_absent_partial_and_dict_usage(usage):
    from backend.budget import BudgetConfig, BudgetTracker

    tracker = BudgetTracker(BudgetConfig(), kill_switch=lambda: True)
    completions = ScriptedCompletions(kimi_response(json.dumps(minimal_plan()), usage=usage))
    gateway = gateway_for(completions, tracker=tracker)
    content = gateway.create_plan(model="kimi-k2.6", objective="o", context={})
    assert json.loads(content)["version"] == "1"
    assert tracker.model_calls == 1
    assert tracker.stop is None


def test_provider_exception_text_never_reaches_durable_failure(monkeypatch, offline_stack):
    repo, conversation_id, launcher = offline_stack
    sentinel = "api key sk-PROVIDER-SENTINEL leaked stacktrace"
    completions = FakeKimiCompletions(plan_exception=RuntimeError(sentinel))
    patch_client(monkeypatch, completions)

    response = create_run(TestClient(app), conversation_id, idempotency_key="smoke-exc-00001")
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    assert launcher.exit_codes == [0]
    run = repo.get_run(UUID(run_id))
    assert run["status"] == "failed"
    assert run["error"]["code"] == "COMMANDER_COMPLETION_FAILED"
    assert sentinel not in json.dumps([str(e) for e in repo.run_events])
    assert sentinel not in json.dumps({k: str(v) for k, v in run.items()})


def test_replan_decision_failure_is_classified_not_raw(monkeypatch, offline_stack):
    repo, conversation_id, launcher = offline_stack
    completions = FakeKimiCompletions(decision_body="{\"decision\": \"NOT_A_DECISION\"}")
    patch_client(monkeypatch, completions)

    response = create_run(TestClient(app), conversation_id, idempotency_key="smoke-replan-01")
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    assert launcher.exit_codes == [0]
    run = repo.get_run(UUID(run_id))
    assert run["status"] == "failed"
    assert run["error"]["code"] == "COMMANDER_DECISION_INVALID"


def test_provider_429_storm_is_backpressure_not_semantic_retries(monkeypatch, offline_stack):
    repo, conversation_id, launcher = offline_stack

    class RateLimited(Exception):
        status_code = 429

    completions = FakeKimiCompletions(plan_exception=RateLimited("Error code: 429"))
    patch_client(monkeypatch, completions)
    monkeypatch.setenv("MILO_PROVIDER_MAX_RATE_LIMIT_RETRIES", "1")
    monkeypatch.setenv("MILO_PROVIDER_BACKOFF_BASE_SECONDS", "0.01")
    monkeypatch.setenv("MILO_PROVIDER_BACKOFF_MAX_SECONDS", "0.01")
    monkeypatch.setenv("MILO_PROVIDER_MAX_BACKPRESSURE_WAIT_SECONDS", "5")

    response = create_run(TestClient(app), conversation_id, idempotency_key="smoke-429-0001")
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    assert launcher.exit_codes == [0]
    run = repo.get_run(UUID(run_id))
    assert run["status"] == "failed"
    assert run["error"]["code"] == "COMMANDER_COMPLETION_FAILED"
    assert run["usage"]["retries"] == 0  # 429s never consume semantic retries
    assert run["usage"]["provider_backpressure_events"] >= 1


# --- fault injection at every boundary --------------------------------------

def run_worker_directly(repo, conversation_id, monkeypatch, completions,
                        idempotency_key="direct-000001"):
    """Create a queued run without the API and execute the worker directly."""
    patch_client(monkeypatch, completions)
    message = repo.create_user_message(UUID(conversation_id), "smoke", {})
    run = repo.create_queued_run(UUID(conversation_id), message["id"], "smoke", {},
                                 requested_by=UUID(USER), idempotency_key=idempotency_key,
                                 request_fingerprint="f")
    return UUID(str(run["id"]))


def test_checkpoint_persistence_failure_escapes_and_is_never_handled(monkeypatch):
    swarm_env(monkeypatch)
    repo, conversation_id = build_repo()

    original = MemoryRepository.save_checkpoint

    def broken(self, checkpoint, worker_id=None, attempt=None, lease_token=None):
        raise AppError("REPOSITORY_ERROR", "guarded persistence operation failed", 502)

    monkeypatch.setattr(MemoryRepository, "save_checkpoint", broken)
    run_id = run_worker_directly(repo, conversation_id, monkeypatch, FakeKimiCompletions())
    with pytest.raises(AppError, match="guarded persistence"):
        worker_main.execute_run(run_id, repo)
    monkeypatch.setattr(MemoryRepository, "save_checkpoint", original)
    # The run was NOT falsely finalized: Cloud Run may retry it.
    assert repo.get_run(run_id)["status"] not in {"failed", "completed"}


def test_infeasible_model_call_budget_fails_before_spending_on_tasks(monkeypatch):
    """With one model-call slot the plan cannot fit (tasks + replan +
    verifier), so the engine's feasibility gate stops the run after the
    single Commander call instead of burning task calls."""
    swarm_env(monkeypatch, MILO_MAX_MODEL_CALLS_PER_RUN="1")
    repo, conversation_id = build_repo()
    completions = FakeKimiCompletions()
    run_id = run_worker_directly(repo, conversation_id, monkeypatch, completions)
    assert worker_main.execute_run(run_id, repo) == 0
    run = repo.get_run(run_id)
    assert run["status"] == "failed"
    assert run["error"]["code"] == "SWARM_V2_EXECUTION_FAILED"
    assert len(completions.calls) == 1


def test_token_budget_trip_produces_durable_budget_terminal(monkeypatch):
    swarm_env(monkeypatch, MILO_MAX_TOTAL_TOKENS_PER_RUN="100000")
    repo, conversation_id = build_repo()
    completions = FakeKimiCompletions()

    class HeavyUsage(FakeKimiCompletions):
        def create(self, **kwargs):
            response = FakeKimiCompletions.create(self, **kwargs)
            response.usage = kimi_usage(prompt=60000, completion=39000)
            return response

    heavy = HeavyUsage()
    run_id = run_worker_directly(repo, conversation_id, monkeypatch, heavy)
    assert worker_main.execute_run(run_id, repo) == 0
    run = repo.get_run(run_id)
    assert run["status"] == "budget_exhausted"
    # Depending on task interleaving the trip is caught either at reserve
    # (REACHED, before the call) or at settle (EXCEEDED, actual overage).
    assert run["error"]["code"] in {"TOTAL_TOKEN_LIMIT_REACHED", "TOTAL_TOKEN_LIMIT_EXCEEDED"}
    assert run["usage"]["model_calls"] >= 1
    assert run["usage"]["total_tokens"] >= 99000


def test_incompatible_checkpoint_is_a_classified_terminal_failure(monkeypatch):
    swarm_env(monkeypatch)
    repo, conversation_id = build_repo()
    run_id = run_worker_directly(repo, conversation_id, monkeypatch, FakeKimiCompletions())
    repo.checkpoints.append({
        "run_id": str(run_id), "workflow_key": "swarm_v2", "phase": "swarm_v2",
        "engine_version": "swarm_v1.0",
        "artifacts": {"swarm_state": {"run_id": str(run_id), "objective": "x",
                                      "engine_version": "swarm_v1.0"}},
        "token_usage": {},
    })
    assert worker_main.execute_run(run_id, repo) == 0
    run = repo.get_run(run_id)
    assert run["status"] == "failed"
    assert run["error"]["code"] == "SWARM_V2_EXECUTION_FAILED"


def test_cancellation_mid_run_reaches_cancelled_terminal(monkeypatch):
    swarm_env(monkeypatch)
    repo, conversation_id = build_repo()

    class CancellingCompletions(FakeKimiCompletions):
        def __init__(self, repo):
            super().__init__()
            self._repo = repo
            self._run_id = None

        def create(self, **kwargs):
            response = super().create(**kwargs)
            if self._run_id and len(self.calls) == 1:
                self._repo.request_cancellation(self._run_id)
            return response

    completions = CancellingCompletions(repo)
    run_id = run_worker_directly(repo, conversation_id, monkeypatch, completions)
    completions._run_id = run_id
    assert worker_main.execute_run(run_id, repo) == 0
    assert repo.get_run(run_id)["status"] == "cancelled"


def test_terminal_run_claim_is_noop_for_every_terminal_state(monkeypatch):
    swarm_env(monkeypatch)
    for status in ("completed", "failed", "cancelled", "timed_out", "budget_exhausted", "partial_success"):
        repo, conversation_id = build_repo()
        run_id = run_worker_directly(repo, conversation_id, monkeypatch,
                                     FakeKimiCompletions(), idempotency_key=f"terminal-{status}")
        repo.runs[str(run_id)]["status"] = status
        events_before = len(repo.run_events)
        assert worker_main.execute_run(run_id, repo) == 0
        assert repo.runs[str(run_id)]["status"] == status
        assert len(repo.run_events) == events_before


def test_active_foreign_lease_claim_conflict_escapes_after_bounded_wait(monkeypatch):
    swarm_env(monkeypatch)
    monkeypatch.setenv("MILO_WORKER_CLAIM_WAIT_SECONDS", "0")
    repo, conversation_id = build_repo()
    run_id = run_worker_directly(repo, conversation_id, monkeypatch, FakeKimiCompletions())
    repo.claim_run(run_id, "other-live-worker", lease_seconds=300)
    with pytest.raises(AppError) as conflict:
        worker_main.execute_run(run_id, repo)
    assert conflict.value.code == "RUN_ALREADY_CLAIMED"
    assert repo.get_run(run_id)["worker_id"] == "other-live-worker"


def test_expired_foreign_lease_is_reclaimed_with_new_attempt(monkeypatch):
    swarm_env(monkeypatch)
    monkeypatch.setenv("MILO_WORKER_CLAIM_WAIT_SECONDS", "2")
    repo, conversation_id = build_repo()
    run_id = run_worker_directly(repo, conversation_id, monkeypatch, FakeKimiCompletions())
    repo.claim_run(run_id, "dead-worker", lease_seconds=300)
    repo.runs[str(run_id)]["lease_expires_at"] = "2000-01-01T00:00:00+00:00"
    assert worker_main.execute_run(run_id, repo) == 0
    run = repo.get_run(run_id)
    assert run["status"] == "completed"
    assert run["attempt"] == 2
    assert run["worker_id"] != "dead-worker"


def test_lost_lease_during_execution_escapes_without_stale_terminal_write(monkeypatch):
    swarm_env(monkeypatch)
    repo, conversation_id = build_repo()

    class LeaseStealingCompletions(FakeKimiCompletions):
        def __init__(self, repo):
            super().__init__()
            self._repo = repo
            self._run_id = None

        def create(self, **kwargs):
            response = super().create(**kwargs)
            if self._run_id and len(self.calls) == 1:
                # Another worker takes over: token and attempt change.
                row = self._repo.runs[str(self._run_id)]
                row["worker_id"] = "thief"
                row["lease_token"] = "stolen"
                row["attempt"] = int(row["attempt"]) + 1
            return response

    completions = LeaseStealingCompletions(repo)
    run_id = run_worker_directly(repo, conversation_id, monkeypatch, completions)
    completions._run_id = run_id
    with pytest.raises(AppError) as escaped:
        worker_main.execute_run(run_id, repo)
    # Supabase surfaces stale-lease writes as RUN_LEASE_LOST; the memory
    # repository mirrors the same guard as RUN_TRANSITION_CONFLICT.
    assert escaped.value.code in {"RUN_LEASE_LOST", "RUN_TRANSITION_CONFLICT"}
    run = repo.get_run(run_id)
    assert run["worker_id"] == "thief"
    assert run["status"] not in {"failed", "completed"}

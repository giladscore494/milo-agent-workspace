from __future__ import annotations

from uuid import uuid4

import pytest

from backend.errors import AppError
from backend.testing.memory_repository import MemoryRepository
from backend.worker.engine import EngineRegistry, EngineResolver
from backend.worker.main import execute_run


class FakeEngine:
    def __init__(self, workflow_key: str, calls: list[str]) -> None:
        self.workflow_key = workflow_key
        self.calls = calls

    def run(self, run):
        self.calls.append(self.workflow_key)
        return {"status": "success", "result": {"workflow_key": self.workflow_key}}


def seeded_run(workflow_key="vehicle_catalog_v1", metadata=None):
    repo = MemoryRepository()
    project_id, user_id = uuid4(), uuid4()
    repo.seed_user(str(user_id))
    repo.seed_project(str(project_id), "routing", "Routing", [str(user_id)])
    repo.projects[str(project_id)]["workflow_key"] = workflow_key
    conversation = repo.create_conversation(project_id, "routing")
    created = repo.create_message_and_run(
        conversation["id"], "route me", metadata or {}, user_id, "routing-key", "fingerprint"
    )
    return repo, created["run"]["id"], project_id


def test_resolver_uses_trusted_v1_project_route_and_ignores_untrusted_input():
    repo, run_id, _ = seeded_run(metadata={"workflow_key": "alternate_fake"})
    run = repo.get_run(run_id)
    run["workflow_key"] = "alternate_fake"
    run["input"]["workflow_key"] = "alternate_fake"
    registry = EngineRegistry({"vehicle_catalog_v1": lambda: FakeEngine("vehicle_catalog_v1", [])})

    resolved = EngineResolver(repo, registry).resolve(run)

    assert resolved.workflow_key == "vehicle_catalog_v1"


def test_alternate_allowlisted_fake_engine_is_selected_without_production_package(monkeypatch):
    monkeypatch.delenv("MILO_ENABLE_PAID_EXECUTION", raising=False)
    repo, run_id, _ = seeded_run("alternate_fake", {"workflow_key": "vehicle_catalog_v1"})
    calls = []
    registry = EngineRegistry({
        "vehicle_catalog_v1": lambda: FakeEngine("vehicle_catalog_v1", calls),
        "alternate_fake": lambda: FakeEngine("alternate_fake", calls),
    })

    assert execute_run(run_id, repo, engine_registry=registry) == 0
    assert calls == ["alternate_fake"]


def test_unknown_workflow_fails_closed_without_engine_instantiation():
    repo, run_id, _ = seeded_run("attacker.module.Engine")
    instantiated = []
    registry = EngineRegistry({"vehicle_catalog_v1": lambda: instantiated.append(True)})

    assert execute_run(run_id, repo, engine_registry=registry) == 1
    assert instantiated == []
    assert repo.get_run(run_id)["error"]["code"] == "ENGINE_NOT_ALLOWED"


def test_checkpoint_lookup_and_writes_use_resolved_workflow(monkeypatch):
    monkeypatch.delenv("MILO_ENABLE_PAID_EXECUTION", raising=False)
    repo, run_id, _ = seeded_run("alternate_fake")
    looked_up = []
    original_lookup = repo.latest_checkpoint

    def recording_lookup(candidate_run_id, workflow_key=None):
        looked_up.append(workflow_key)
        return original_lookup(candidate_run_id, workflow_key)

    repo.latest_checkpoint = recording_lookup
    calls = []
    registry = EngineRegistry({"alternate_fake": lambda: FakeEngine("alternate_fake", calls)})

    assert execute_run(run_id, repo, engine_registry=registry) == 0
    assert looked_up == ["alternate_fake"]


def test_cross_workflow_checkpoint_is_not_resumed(monkeypatch):
    monkeypatch.delenv("MILO_ENABLE_PAID_EXECUTION", raising=False)
    repo, run_id, _ = seeded_run("alternate_fake")
    repo.checkpoints.append({
        "id": "wrong-workflow-checkpoint",
        "run_id": str(run_id),
        "workflow_key": "vehicle_catalog_v1",
        "phase": "summary",
        "artifacts": {"final_builder": {"parsed": {"status": "success", "wrong": True}}},
    })
    calls = []
    registry = EngineRegistry({"alternate_fake": lambda: FakeEngine("alternate_fake", calls)})

    assert execute_run(run_id, repo, engine_registry=registry) == 0
    assert calls == ["alternate_fake"]
    assert not any(event["event_type"] == "run_resumed" for event in repo.run_events)


def test_registry_rejects_unknown_key_directly():
    with pytest.raises(AppError) as exc:
        EngineRegistry({"vehicle_catalog_v1": lambda: FakeEngine("vehicle_catalog_v1", [])}).require("unknown")
    assert exc.value.code == "ENGINE_NOT_ALLOWED"

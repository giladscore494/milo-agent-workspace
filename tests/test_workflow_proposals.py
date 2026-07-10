from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from backend.dependencies import get_repository
from backend.main import app
from backend.workflow_proposals import compile_proposal


class ProposalRepo:
    def __init__(self):
        self.proposal_id = uuid4()
        self.project_id = uuid4()
        self.conversation_id = uuid4()
        self.message_id = uuid4()
        self.run_id = uuid4()
        self.user_id = uuid4()
        self.proposals = {}
        self.runs = []

    def _check_member(self, user_id):
        if user_id is not None and UUID(str(user_id)) != self.user_id:
            from backend.errors import NotFoundError
            raise NotFoundError("project", str(user_id))

    def get_project(self, project_id, user_id=None):
        self._check_member(user_id)
        return {"id": self.project_id, "slug": "p", "name": "P", "workflow_key": "vehicle_catalog_v1", "configuration": {}}

    def create_workflow_proposal(self, user_request, proposal, project_id=None, created_by=None):
        row = {"id": self.proposal_id, "user_request": user_request, "project_id": project_id, "created_by": created_by, "created_at": datetime.now(UTC).isoformat(), "updated_at": datetime.now(UTC).isoformat(), **proposal}
        self.proposals[self.proposal_id] = row
        return row

    def get_workflow_proposal(self, proposal_id, user_id=None):
        self._check_member(user_id)
        return self.proposals[UUID(str(proposal_id))]

    def update_workflow_proposal(self, proposal_id, fields):
        row = self.get_workflow_proposal(proposal_id)
        row.update(fields)
        row["updated_at"] = datetime.now(UTC).isoformat()
        return row

    def create_project_from_proposal(self, proposal_id, slug, name, description, configuration, created_by=None):
        return {"id": self.project_id, "slug": slug, "name": name, "description": description, "workflow_key": "chat_architect_v1", "configuration": configuration}

    def get_conversation(self, conversation_id, user_id=None):
        self._check_member(user_id)
        return {"id": self.conversation_id, "project_id": self.project_id, "title": "t"}

    def create_user_message(self, conversation_id, content, metadata):
        return {"id": self.message_id, "conversation_id": conversation_id, "role": "user", "content": content, "metadata": metadata}

    def create_queued_run(self, conversation_id, user_message_id, content, metadata):
        self.runs.append(metadata)
        return {"id": self.run_id, "conversation_id": conversation_id, "status": "queued"}


@pytest.fixture
def repo(monkeypatch):
    fake = ProposalRepo()
    monkeypatch.delenv("MILO_ENABLE_RUN_CREATION", raising=False)
    monkeypatch.delenv("MILO_ENABLE_PROPOSAL_MUTATIONS", raising=False)
    app.dependency_overrides[get_repository] = lambda: fake
    yield fake
    app.dependency_overrides.clear()


def test_good_request_is_approved_with_visible_internet_policy():
    proposal = compile_proposal("Create a current market research report with citations for Acme robotics buyers")
    assert proposal["status"] == "approved"
    assert proposal["estimates"]["planned_agents"] >= 5
    assert proposal["estimates"]["search_enabled_agents"] >= 1
    assert all(agent["internet_policy"] and agent["internet_reason"] for agent in proposal["draft"]["agents"])


def test_ambiguous_request_requires_revision():
    proposal = compile_proposal("Do something")
    assert proposal["status"] == "revision_required"
    assert "ambiguity" in proposal["critiques"][-1]["findings"]


def test_missing_verifier_is_repaired():
    proposal = compile_proposal("Create a sourced report about current EV charging standards", force_missing_verifier=True)
    assert proposal["status"] == "approved"
    assert proposal["repair_count"] == 1
    assert "verifier" in [agent["role"] for agent in proposal["draft"]["agents"]]


def test_excessive_budget_is_repaired_to_cost_ceiling():
    proposal = compile_proposal("Create a huge current report with citations about global automotive suppliers", budget_preference="excessive")
    assert proposal["status"] == "approved"
    assert proposal["estimates"]["planned_agents"] <= 8
    assert proposal["estimates"]["cost_warning"] in {"normal", "high"}


def test_bad_internet_classification_hits_repair_cap():
    proposal = compile_proposal("Create a current report with citations about battery startups", force_bad_internet=True)
    assert proposal["status"] == "revision_required"
    assert proposal["repair_count"] == 2
    assert "wrong internet policy" in proposal["critiques"][-1]["findings"]


def test_proposal_mutations_and_run_start_disabled_by_default(repo):
    client = TestClient(app)
    proposal_id = repo.create_workflow_proposal("seed", compile_proposal("Create a current report with citations about charging networks"))["id"]

    created = client.post("/workflow-proposals", json={"user_request": "Create a current report with citations about charging networks"})
    approved = client.post(f"/workflow-proposals/{proposal_id}/approve", json={})
    revised = client.post(f"/workflow-proposals/{proposal_id}/revise", json={"user_request": "Create a current cited report about freight brokers"})
    project = client.post(f"/workflow-proposals/{proposal_id}/project", json={"slug": "x", "name": "X"})
    run = client.post(f"/workflow-proposals/{proposal_id}/runs", json={"conversation_id": str(repo.conversation_id), "content": "start"})

    assert created.status_code == 403
    assert approved.status_code == 403
    assert revised.status_code == 403
    assert project.status_code == 403
    assert run.status_code == 403
    assert repo.runs == []


def test_proposal_stage_can_be_enabled_explicitly(repo, monkeypatch):
    monkeypatch.setenv("MILO_ENABLE_PROPOSAL_MUTATIONS", "true")
    client = TestClient(app)
    member = {"x-milo-auth-user-id": str(repo.user_id)}
    created = client.post("/workflow-proposals", json={"user_request": "Do something", "project_id": str(repo.project_id)}, headers=member)
    assert created.status_code == 201
    assert created.json()["created_by"] == str(repo.user_id)
    assert created.json()["project_id"] == str(repo.project_id)
    proposal_id = created.json()["id"]
    revised = client.post(f"/workflow-proposals/{proposal_id}/revise", json={"user_request": "Create a current cited report about freight brokers"}, headers=member)
    assert revised.status_code == 200
    assert revised.json()["status"] == "approved"
    assert revised.json()["critiques"]


def test_enabled_proposal_mutations_still_require_authentication(repo, monkeypatch):
    monkeypatch.setenv("MILO_ENABLE_PROPOSAL_MUTATIONS", "true")
    client = TestClient(app)
    created = client.post("/workflow-proposals", json={"user_request": "Do something", "project_id": str(repo.project_id)})
    assert created.status_code == 401
    assert repo.proposals == {}


def test_enabled_proposal_mutations_deny_non_members_with_404(repo, monkeypatch):
    monkeypatch.setenv("MILO_ENABLE_PROPOSAL_MUTATIONS", "true")
    client = TestClient(app)
    stranger = {"x-milo-auth-user-id": str(uuid4())}
    created = client.post("/workflow-proposals", json={"user_request": "Do something", "project_id": str(repo.project_id)}, headers=stranger)
    assert created.status_code == 404
    assert repo.proposals == {}
    repo.create_workflow_proposal("seed", compile_proposal("Create a current report with citations about charging networks"), repo.project_id, repo.user_id)
    for action, body in (("approve", {}), ("reject", {}), ("revise", {"user_request": "Another cited current report"})):
        response = client.post(f"/workflow-proposals/{repo.proposal_id}/{action}", json=body, headers=stranger)
        assert response.status_code == 404, action
    assert repo.proposals[repo.proposal_id]["user_request"] == "seed"

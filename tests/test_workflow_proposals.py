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
        self.proposals = {}
        self.runs = []

    def create_workflow_proposal(self, user_request, proposal):
        row = {"id": self.proposal_id, "user_request": user_request, "created_at": datetime.now(UTC).isoformat(), "updated_at": datetime.now(UTC).isoformat(), **proposal}
        self.proposals[self.proposal_id] = row
        return row

    def get_workflow_proposal(self, proposal_id):
        return self.proposals[UUID(str(proposal_id))]

    def update_workflow_proposal(self, proposal_id, fields):
        row = self.get_workflow_proposal(proposal_id)
        row.update(fields)
        row["updated_at"] = datetime.now(UTC).isoformat()
        return row

    def create_project_from_proposal(self, proposal_id, slug, name, description, configuration):
        return {"id": self.project_id, "slug": slug, "name": name, "description": description, "workflow_key": "chat_architect_v1", "configuration": configuration}

    def get_conversation(self, conversation_id):
        return {"id": self.conversation_id, "project_id": self.project_id, "title": "t"}

    def create_user_message(self, conversation_id, content, metadata):
        return {"id": self.message_id, "conversation_id": conversation_id, "role": "user", "content": content, "metadata": metadata}

    def create_queued_run(self, conversation_id, user_message_id, content, metadata):
        self.runs.append(metadata)
        return {"id": self.run_id, "conversation_id": conversation_id, "status": "queued"}


@pytest.fixture
def repo():
    fake = ProposalRepo()
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


def test_approval_gates_project_and_run_start(repo):
    client = TestClient(app)
    created = client.post("/workflow-proposals", json={"user_request": "Create a current report with citations about charging networks"})
    assert created.status_code == 201
    proposal_id = created.json()["id"]

    blocked_project = client.post(f"/workflow-proposals/{proposal_id}/project", json={"slug": "x", "name": "X"})
    blocked_run = client.post(f"/workflow-proposals/{proposal_id}/runs", json={"conversation_id": str(repo.conversation_id), "content": "start"})
    assert blocked_project.status_code == 409
    assert blocked_run.status_code == 409
    assert repo.runs == []

    approved = client.post(f"/workflow-proposals/{proposal_id}/approve", json={})
    assert approved.status_code == 200
    started = client.post(f"/workflow-proposals/{proposal_id}/runs", json={"conversation_id": str(repo.conversation_id), "content": "start"})
    assert started.status_code == 202
    assert repo.runs == [{"proposal_id": proposal_id}]


def test_revise_recompiles_and_persists_critiques(repo):
    client = TestClient(app)
    created = client.post("/workflow-proposals", json={"user_request": "Do something"})
    proposal_id = created.json()["id"]
    revised = client.post(f"/workflow-proposals/{proposal_id}/revise", json={"user_request": "Create a current cited report about freight brokers"})
    assert revised.status_code == 200
    assert revised.json()["status"] == "approved"
    assert revised.json()["critiques"]

"""API package namespace."""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import Depends, FastAPI

from backend.dependencies import get_repository
from backend.errors import AppError, install_error_handlers
from backend.repository import Repository
from backend.schemas import (
    Conversation,
    ConversationCreate,
    HealthResponse,
    Project,
    ProposalCreate,
    ProposalDecision,
    ProposalProjectCreate,
    ProposalRevise,
    ProposalRunCreate,
    Run,
    RunCancelRequest,
    RunCancelResponse,
    RunCreate,
    RunCreated,
    RunEvent,
    WorkflowProposal,
)
from backend.workflow_proposals import compile_proposal, ensure_approved

app = FastAPI(title="MILO Agent Workspace API")
install_error_handlers(app)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse()


@app.get("/projects", response_model=list[Project])
def list_projects(repo: Repository = Depends(get_repository)) -> list[dict]:
    return repo.list_projects()


@app.get("/projects/{project_id}", response_model=Project)
def get_project(project_id: UUID, repo: Repository = Depends(get_repository)) -> dict:
    return repo.get_project(project_id)


@app.post("/projects/{project_id}/conversations", response_model=Conversation, status_code=201)
def create_conversation(project_id: UUID, request: ConversationCreate, repo: Repository = Depends(get_repository)) -> dict:
    return repo.create_conversation(project_id, request.title)


@app.get("/conversations/{conversation_id}", response_model=Conversation)
def get_conversation(conversation_id: UUID, repo: Repository = Depends(get_repository)) -> dict:
    return repo.get_conversation(conversation_id)


@app.post("/conversations/{conversation_id}/runs", response_model=RunCreated, status_code=202)
def create_run(conversation_id: UUID, request: RunCreate, repo: Repository = Depends(get_repository)) -> RunCreated:
    repo.get_conversation(conversation_id)
    message = repo.create_user_message(conversation_id, request.content, request.metadata)
    run = repo.create_queued_run(conversation_id, message["id"], request.content, request.metadata)
    return RunCreated(run_id=run["id"], status=run["status"])


@app.get("/runs/{run_id}", response_model=Run)
def get_run(run_id: UUID, repo: Repository = Depends(get_repository)) -> dict:
    return repo.get_run(run_id)


@app.get("/runs/{run_id}/events", response_model=list[RunEvent])
def get_run_events(run_id: UUID, repo: Repository = Depends(get_repository)) -> list[dict]:
    return repo.list_run_events(run_id)


@app.post("/runs/{run_id}/cancel", response_model=RunCancelResponse)
def cancel_run(run_id: UUID, request: RunCancelRequest, repo: Repository = Depends(get_repository)) -> RunCancelResponse:
    run = repo.request_cancellation(run_id, request.reason)
    repo.append_run_event(run_id, "cancellation_requested", {"message": request.reason or "Cancellation requested", "payload": {"reason": request.reason}})
    return RunCancelResponse(run_id=run["id"], status=run["status"])


@app.post("/workflow-proposals", response_model=WorkflowProposal, status_code=201)
def create_workflow_proposal(request: ProposalCreate, repo: Repository = Depends(get_repository)) -> dict:
    proposal = compile_proposal(request.user_request, request.budget_preference, request.force_missing_verifier, request.force_bad_internet)
    return repo.create_workflow_proposal(request.user_request, proposal)


@app.get("/workflow-proposals/{proposal_id}", response_model=WorkflowProposal)
def get_workflow_proposal(proposal_id: UUID, repo: Repository = Depends(get_repository)) -> dict:
    return repo.get_workflow_proposal(proposal_id)


@app.post("/workflow-proposals/{proposal_id}/approve", response_model=WorkflowProposal)
def approve_workflow_proposal(proposal_id: UUID, request: ProposalDecision, repo: Repository = Depends(get_repository)) -> dict:
    proposal = repo.get_workflow_proposal(proposal_id)
    if proposal["status"] != "approved":
        raise AppError("PROPOSAL_NOT_APPROVABLE", "only critic-approved proposals can be approved", 409)
    return repo.update_workflow_proposal(proposal_id, {"approved_at": datetime.now(UTC).isoformat(), "rejected_at": None})


@app.post("/workflow-proposals/{proposal_id}/reject", response_model=WorkflowProposal)
def reject_workflow_proposal(proposal_id: UUID, request: ProposalDecision, repo: Repository = Depends(get_repository)) -> dict:
    repo.get_workflow_proposal(proposal_id)
    return repo.update_workflow_proposal(proposal_id, {"status": "rejected", "rejected_at": datetime.now(UTC).isoformat(), "approved_at": None})


@app.post("/workflow-proposals/{proposal_id}/revise", response_model=WorkflowProposal)
def revise_workflow_proposal(proposal_id: UUID, request: ProposalRevise, repo: Repository = Depends(get_repository)) -> dict:
    repo.get_workflow_proposal(proposal_id)
    proposal = compile_proposal(request.user_request, request.budget_preference)
    return repo.update_workflow_proposal(proposal_id, {"user_request": request.user_request, **proposal})


@app.post("/workflow-proposals/{proposal_id}/project", response_model=Project, status_code=201)
def create_project_from_proposal(proposal_id: UUID, request: ProposalProjectCreate, repo: Repository = Depends(get_repository)) -> dict:
    proposal = repo.get_workflow_proposal(proposal_id)
    ensure_approved(proposal)
    return repo.create_project_from_proposal(proposal_id, request.slug, request.name, request.description, {"proposal": proposal})


@app.post("/workflow-proposals/{proposal_id}/runs", response_model=RunCreated, status_code=202)
def start_approved_proposal_run(proposal_id: UUID, request: ProposalRunCreate, repo: Repository = Depends(get_repository)) -> RunCreated:
    proposal = repo.get_workflow_proposal(proposal_id)
    ensure_approved(proposal)
    repo.get_conversation(request.conversation_id)
    metadata = {**request.metadata, "proposal_id": str(proposal_id)}
    message = repo.create_user_message(request.conversation_id, request.content, metadata)
    run = repo.create_queued_run(request.conversation_id, message["id"], request.content, metadata)
    return RunCreated(run_id=run["id"], status=run["status"])

"""API package namespace."""

from datetime import UTC, datetime
import os
from uuid import UUID

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from backend.config import get_settings
from backend.auth import AuthenticatedUser, get_authenticated_user
from backend.dependencies import get_job_launcher, get_repository
from backend.job_launcher import JobLauncher
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
    ToolAccessRequestCreate, ToolGrantCreate, ToolUsageCreate, SourceCreate, ClaimCreate, ConflictCreate,
)
from backend.workflow_proposals import compile_proposal, ensure_approved

settings = get_settings()
app = FastAPI(title=settings.api_title)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list, allow_credentials=True, allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["Authorization", "Content-Type"])

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cache-Control", "no-store")
        return response

app.add_middleware(SecurityHeadersMiddleware)
install_error_handlers(app)


def require_stage_enabled(flag: str, surface: str) -> None:
    if os.getenv(flag, "").strip().lower() not in {"1", "true", "yes", "on"}:
        raise AppError("EXECUTION_SURFACE_DISABLED", f"{surface} is disabled", 403)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse()


@app.get("/projects", response_model=list[Project])
def list_projects(user: AuthenticatedUser = Depends(get_authenticated_user), repo: Repository = Depends(get_repository)) -> list[dict]:
    return repo.list_projects(user.user_id)


@app.get("/projects/{project_id}", response_model=Project)
def get_project(project_id: UUID, user: AuthenticatedUser = Depends(get_authenticated_user), repo: Repository = Depends(get_repository)) -> dict:
    return repo.get_project(project_id, user.user_id)


@app.post("/projects/{project_id}/conversations", response_model=Conversation, status_code=201)
def create_conversation(project_id: UUID, request: ConversationCreate, user: AuthenticatedUser = Depends(get_authenticated_user), repo: Repository = Depends(get_repository)) -> dict:
    return repo.create_conversation(project_id, request.title, user.user_id)


@app.get("/conversations/{conversation_id}", response_model=Conversation)
def get_conversation(conversation_id: UUID, user: AuthenticatedUser = Depends(get_authenticated_user), repo: Repository = Depends(get_repository)) -> dict:
    return repo.get_conversation(conversation_id, user.user_id)


@app.post("/conversations/{conversation_id}/runs", response_model=RunCreated, status_code=202)
def create_run(conversation_id: UUID, request: RunCreate, repo: Repository = Depends(get_repository), launcher: JobLauncher = Depends(get_job_launcher)) -> RunCreated:
    require_stage_enabled("MILO_ENABLE_RUN_CREATION", "conversation run creation")
    repo.get_conversation(conversation_id)
    message = repo.create_user_message(conversation_id, request.content, request.metadata)
    run = repo.create_queued_run(conversation_id, message["id"], request.content, request.metadata)
    launch = launcher.launch(UUID(str(run["id"])))
    if hasattr(repo, "record_run_invocation"):
        repo.record_run_invocation(UUID(str(run["id"])), launch)
    if hasattr(repo, "append_run_event"):
        repo.append_run_event(UUID(str(run["id"])), "run_created", {"message": "Run queued and worker invocation requested", "payload": {"launcher": launch.get("mode"), "execution": launch.get("execution", "")}})
    return RunCreated(run_id=run["id"], status=run["status"])


@app.get("/runs/{run_id}", response_model=Run)
def get_run(run_id: UUID, repo: Repository = Depends(get_repository)) -> dict:
    return repo.get_run(run_id)


@app.get("/runs/{run_id}/events", response_model=list[RunEvent])
def get_run_events(run_id: UUID, repo: Repository = Depends(get_repository)) -> list[dict]:
    return repo.list_run_events(run_id)


@app.post("/runs/{run_id}/cancel", response_model=RunCancelResponse)
def cancel_run(run_id: UUID, request: RunCancelRequest, repo: Repository = Depends(get_repository)) -> RunCancelResponse:
    require_stage_enabled("MILO_ENABLE_EXECUTION_CONTROL", "run cancellation")
    run = repo.request_cancellation(run_id, request.reason)
    repo.append_run_event(run_id, "cancellation_requested", {"message": request.reason or "Cancellation requested", "payload": {"reason": request.reason}})
    return RunCancelResponse(run_id=run["id"], status=run["status"])


@app.post("/workflow-proposals", response_model=WorkflowProposal, status_code=201)
def create_workflow_proposal(request: ProposalCreate, repo: Repository = Depends(get_repository)) -> dict:
    require_stage_enabled("MILO_ENABLE_PROPOSAL_MUTATIONS", "workflow proposal creation")
    proposal = compile_proposal(request.user_request, request.budget_preference, request.force_missing_verifier, request.force_bad_internet)
    return repo.create_workflow_proposal(request.user_request, proposal)


@app.get("/workflow-proposals/{proposal_id}", response_model=WorkflowProposal)
def get_workflow_proposal(proposal_id: UUID, repo: Repository = Depends(get_repository)) -> dict:
    return repo.get_workflow_proposal(proposal_id)


@app.post("/workflow-proposals/{proposal_id}/approve", response_model=WorkflowProposal)
def approve_workflow_proposal(proposal_id: UUID, request: ProposalDecision, repo: Repository = Depends(get_repository)) -> dict:
    require_stage_enabled("MILO_ENABLE_PROPOSAL_MUTATIONS", "workflow proposal approval")
    proposal = repo.get_workflow_proposal(proposal_id)
    if proposal["status"] != "approved":
        raise AppError("PROPOSAL_NOT_APPROVABLE", "only critic-approved proposals can be approved", 409)
    return repo.update_workflow_proposal(proposal_id, {"approved_at": datetime.now(UTC).isoformat(), "rejected_at": None})


@app.post("/workflow-proposals/{proposal_id}/reject", response_model=WorkflowProposal)
def reject_workflow_proposal(proposal_id: UUID, request: ProposalDecision, repo: Repository = Depends(get_repository)) -> dict:
    require_stage_enabled("MILO_ENABLE_PROPOSAL_MUTATIONS", "workflow proposal rejection")
    repo.get_workflow_proposal(proposal_id)
    return repo.update_workflow_proposal(proposal_id, {"status": "rejected", "rejected_at": datetime.now(UTC).isoformat(), "approved_at": None})


@app.post("/workflow-proposals/{proposal_id}/revise", response_model=WorkflowProposal)
def revise_workflow_proposal(proposal_id: UUID, request: ProposalRevise, repo: Repository = Depends(get_repository)) -> dict:
    require_stage_enabled("MILO_ENABLE_PROPOSAL_MUTATIONS", "workflow proposal revision")
    repo.get_workflow_proposal(proposal_id)
    proposal = compile_proposal(request.user_request, request.budget_preference)
    return repo.update_workflow_proposal(proposal_id, {"user_request": request.user_request, **proposal})


@app.post("/workflow-proposals/{proposal_id}/project", response_model=Project, status_code=201)
def create_project_from_proposal(proposal_id: UUID, request: ProposalProjectCreate, repo: Repository = Depends(get_repository)) -> dict:
    require_stage_enabled("MILO_ENABLE_PROPOSAL_MUTATIONS", "workflow proposal project creation")
    proposal = repo.get_workflow_proposal(proposal_id)
    ensure_approved(proposal)
    return repo.create_project_from_proposal(proposal_id, request.slug, request.name, request.description, {"proposal": proposal})


@app.post("/workflow-proposals/{proposal_id}/runs", response_model=RunCreated, status_code=202)
def start_approved_proposal_run(proposal_id: UUID, request: ProposalRunCreate, repo: Repository = Depends(get_repository), launcher: JobLauncher = Depends(get_job_launcher)) -> RunCreated:
    require_stage_enabled("MILO_ENABLE_RUN_CREATION", "workflow proposal run creation")
    proposal = repo.get_workflow_proposal(proposal_id)
    ensure_approved(proposal)
    repo.get_conversation(request.conversation_id)
    metadata = {**request.metadata, "proposal_id": str(proposal_id)}
    message = repo.create_user_message(request.conversation_id, request.content, metadata)
    run = repo.create_queued_run(request.conversation_id, message["id"], request.content, metadata)
    launch = launcher.launch(UUID(str(run["id"])))
    if hasattr(repo, "record_run_invocation"):
        repo.record_run_invocation(UUID(str(run["id"])), launch)
    if hasattr(repo, "append_run_event"):
        repo.append_run_event(UUID(str(run["id"])), "run_created", {"message": "Run queued and worker invocation requested", "payload": {"launcher": launch.get("mode"), "execution": launch.get("execution", "")}})
    return RunCreated(run_id=run["id"], status=run["status"])


@app.post("/runs/{run_id}/tool-access-requests", status_code=201)
def create_tool_access_request(run_id: UUID, request: ToolAccessRequestCreate, repo: Repository = Depends(get_repository)) -> dict:
    require_stage_enabled("MILO_ENABLE_EXECUTION_CONTROL", "tool access requests")
    row = repo.create_tool_access_request(run_id, request.model_dump())
    repo.append_run_event(run_id, "tool_access_requested", {"message": f"{request.agent} requested {request.tool}", "agent": request.agent, "payload": row})
    return row

@app.post("/runs/{run_id}/tool-grants", status_code=201)
def create_tool_grant(run_id: UUID, request: ToolGrantCreate, repo: Repository = Depends(get_repository)) -> dict:
    require_stage_enabled("MILO_ENABLE_EXECUTION_CONTROL", "tool grants")
    payload = request.model_dump()
    row = repo.create_tool_grant(run_id, payload)
    repo.append_run_event(run_id, "tool_access_granted", {"message": f"{request.tool} granted to {request.agent}", "agent": request.agent, "payload": row})
    return row

@app.post("/runs/{run_id}/tool-usage", status_code=201)
def create_tool_usage(run_id: UUID, request: ToolUsageCreate, repo: Repository = Depends(get_repository)) -> dict:
    require_stage_enabled("MILO_ENABLE_EXECUTION_CONTROL", "tool usage")
    row = repo.create_tool_usage(run_id, request.model_dump())
    repo.append_run_event(run_id, "tool_used", {"message": f"{request.agent} used {request.tool}", "agent": request.agent, "payload": row})
    return row

@app.post("/runs/{run_id}/sources", status_code=201)
def create_source(run_id: UUID, request: SourceCreate, repo: Repository = Depends(get_repository)) -> dict:
    require_stage_enabled("MILO_ENABLE_EXECUTION_CONTROL", "source recording")
    row = repo.create_source(run_id, request.model_dump())
    repo.append_run_event(run_id, "source_recorded", {"message": request.title, "agent": request.agent, "payload": row})
    return row

@app.post("/runs/{run_id}/claims", status_code=201)
def create_claim(run_id: UUID, request: ClaimCreate, repo: Repository = Depends(get_repository)) -> dict:
    require_stage_enabled("MILO_ENABLE_EXECUTION_CONTROL", "claim recording")
    row = repo.create_claim(run_id, request.model_dump())
    repo.append_run_event(run_id, "claim_recorded", {"message": f"{request.entity_key}.{request.field_key}", "agent": request.agent, "payload": row})
    return row

@app.post("/runs/{run_id}/conflicts", status_code=201)
def create_conflict(run_id: UUID, request: ConflictCreate, repo: Repository = Depends(get_repository)) -> dict:
    require_stage_enabled("MILO_ENABLE_EXECUTION_CONTROL", "conflict recording")
    row = repo.create_conflict(run_id, request.model_dump())
    repo.append_run_event(run_id, "conflict_detected", {"message": f"{request.entity_key}.{request.field_key}", "payload": row})
    return row

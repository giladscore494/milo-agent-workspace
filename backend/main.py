"""API package namespace."""

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from backend.budget import BudgetConfig
from backend.config import get_settings
from backend.auth import AuthenticatedUser, get_authenticated_user
from backend.dependencies import get_job_launcher, get_repository
from backend.execution_guard import ExecutionSurfaceGuardMiddleware, is_stage_enabled
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
    WorkerRunCompleteRequest, WorkerRunEventCreate, WorkerRunFailRequest,
)
from backend.rate_limit import enforce_rate_limit
from backend.runtime import EVENT_TYPES, TERMINAL_STATES
from backend.worker_auth import WorkerIdentity, get_verified_worker
from backend.workflow_proposals import compile_proposal, ensure_approved

settings = get_settings()
app = FastAPI(title=settings.api_title)
# Added first so it sits innermost of the middleware stack while still running
# before routing and request-body validation for every request.
app.add_middleware(ExecutionSurfaceGuardMiddleware)
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


# Defense-in-depth assertion; ExecutionSurfaceGuardMiddleware is the
# authoritative guard and already rejects disabled surfaces before validation.
def require_stage_enabled(flag: str, surface: str) -> None:
    if not is_stage_enabled(flag):
        raise AppError("EXECUTION_SURFACE_DISABLED", f"{surface} is disabled", 403)


def _request_fingerprint(content: str, metadata: dict) -> str:
    canonical = json.dumps({"content": content, "metadata": metadata}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _enforce_concurrency_limits(repo: Repository, user: AuthenticatedUser, conversation_id: UUID) -> None:
    """Server-side concurrency caps, applied before any run row is created."""
    config = BudgetConfig.from_env()
    if config.max_concurrent_runs_per_user is not None and hasattr(repo, "count_active_runs_for_user"):
        if repo.count_active_runs_for_user(user.user_id) >= config.max_concurrent_runs_per_user:
            raise AppError("USER_CONCURRENCY_LIMIT", "too many active runs for this user", 429)
    if config.max_concurrent_runs_per_project is not None and hasattr(repo, "count_active_runs_for_project"):
        conversation = repo.get_conversation(conversation_id)
        project_id = conversation.get("project_id")
        if project_id and repo.count_active_runs_for_project(project_id) >= config.max_concurrent_runs_per_project:
            raise AppError("PROJECT_CONCURRENCY_LIMIT", "too many active runs for this project", 429)


def _create_and_launch_run(repo: Repository, launcher: JobLauncher, user: AuthenticatedUser, conversation_id: UUID, content: str, metadata: dict, idempotency_key: str | None = None) -> RunCreated:
    """Create the user message + queued run and request a worker launch.

    Callers must have already authorized the authenticated user against the
    conversation's project membership; no mutation happens before that.

    Idempotency: the key is scoped to (authenticated user, conversation).
    A replay with the same key and payload returns the original run without
    a second launch; the same key with a different payload is a 409. A run
    whose previous launch attempt failed (launch_state == 'launch_failed'
    while still queued) is safely relaunched instead of duplicated.
    """
    enforce_rate_limit("run_creation_user", str(user.user_id))
    enforce_rate_limit("run_creation_project", str(conversation_id))
    fingerprint = _request_fingerprint(content, metadata)
    run = None
    if idempotency_key and hasattr(repo, "find_run_by_idempotency"):
        existing = repo.find_run_by_idempotency(conversation_id, user.user_id, idempotency_key)
        if existing is not None:
            if existing.get("request_fingerprint") not in (None, fingerprint):
                raise AppError("IDEMPOTENCY_CONFLICT", "idempotency key was already used with a different payload", 409)
            if existing.get("status") != "queued" or existing.get("launch_state") != "launch_failed":
                # Original run is authoritative: no new message, no new run,
                # and never a second worker launch for the same request.
                return RunCreated(run_id=existing["id"], status=existing["status"])
            run = existing  # recoverable failed launch: retry launch only
    if run is None:
        _enforce_concurrency_limits(repo, user, conversation_id)
        metadata = {**metadata, "requested_by": str(user.user_id)}
        if idempotency_key:
            metadata["idempotency_key"] = idempotency_key
        message = repo.create_user_message(conversation_id, content, metadata)
        run = repo.create_queued_run(conversation_id, message["id"], content, metadata, requested_by=user.user_id, idempotency_key=idempotency_key, request_fingerprint=fingerprint)
    run_id = UUID(str(run["id"]))
    if run.get("status") not in (None, "queued"):
        # Cancelled-before-launch or an already-progressed duplicate: never launch.
        return RunCreated(run_id=run["id"], status=run["status"])
    if run.get("launch_state") in {"launching", "launched"}:
        # A concurrent request already owns the launch.
        return RunCreated(run_id=run["id"], status=run["status"])
    if hasattr(repo, "set_launch_state"):
        repo.set_launch_state(run_id, "launching")
    try:
        launch = launcher.launch(run_id)
    except Exception as exc:
        # Leave a clear recoverable state: the run stays queued with
        # launch_state=launch_failed and can be retried with the same key.
        if hasattr(repo, "set_launch_state"):
            repo.set_launch_state(run_id, "launch_failed", error={"message": str(exc)[:500]})
        if hasattr(repo, "append_run_event"):
            repo.append_run_event(run_id, "launch_failed", {"message": "Worker launch failed; run remains queued and the request can be retried", "payload": {"recoverable": True}})
        raise AppError("JOB_LAUNCH_FAILED", "worker launch failed; the run remains queued and can be retried with the same idempotency key", 502) from exc
    if hasattr(repo, "record_run_invocation"):
        repo.record_run_invocation(run_id, launch)
    if hasattr(repo, "set_launch_state"):
        repo.set_launch_state(run_id, "launched")
    if hasattr(repo, "append_run_event"):
        repo.append_run_event(run_id, "run_created", {"message": "Run queued and worker invocation requested", "payload": {"launcher": launch.get("mode"), "execution": launch.get("execution", "")}})
    return RunCreated(run_id=run["id"], status=run["status"])


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse()


@app.get("/projects", response_model=list[Project])
def list_projects(user: AuthenticatedUser = Depends(get_authenticated_user), repo: Repository = Depends(get_repository)) -> list[dict]:
    return repo.list_projects(user.user_id)


@app.get("/projects/{project_id}", response_model=Project)
def get_project(project_id: UUID, user: AuthenticatedUser = Depends(get_authenticated_user), repo: Repository = Depends(get_repository)) -> dict:
    return repo.get_project(project_id, user.user_id)


@app.get("/projects/{project_id}/conversations", response_model=list[Conversation])
def list_conversations(project_id: UUID, user: AuthenticatedUser = Depends(get_authenticated_user), repo: Repository = Depends(get_repository)) -> list[dict]:
    repo.get_project(project_id, user.user_id)
    return repo.list_conversations(project_id)


@app.post("/projects/{project_id}/conversations", response_model=Conversation, status_code=201)
def create_conversation(project_id: UUID, request: ConversationCreate, user: AuthenticatedUser = Depends(get_authenticated_user), repo: Repository = Depends(get_repository)) -> dict:
    return repo.create_conversation(project_id, request.title, user.user_id)


@app.get("/conversations/{conversation_id}", response_model=Conversation)
def get_conversation(conversation_id: UUID, user: AuthenticatedUser = Depends(get_authenticated_user), repo: Repository = Depends(get_repository)) -> dict:
    return repo.get_conversation(conversation_id, user.user_id)


@app.post("/conversations/{conversation_id}/runs", response_model=RunCreated, status_code=202)
def create_run(conversation_id: UUID, request: RunCreate, user: AuthenticatedUser = Depends(get_authenticated_user), repo: Repository = Depends(get_repository), launcher: JobLauncher = Depends(get_job_launcher)) -> RunCreated:
    require_stage_enabled("MILO_ENABLE_RUN_CREATION", "conversation run creation")
    # Membership authorization must precede every mutation and the launch.
    repo.get_conversation(conversation_id, user.user_id)
    return _create_and_launch_run(repo, launcher, user, conversation_id, request.content, request.metadata, request.idempotency_key)


@app.get("/runs/{run_id}", response_model=Run)
def get_run(run_id: UUID, user: AuthenticatedUser = Depends(get_authenticated_user), repo: Repository = Depends(get_repository)) -> dict:
    return repo.get_run(run_id, user_id=user.user_id)


@app.get("/runs/{run_id}/events", response_model=list[RunEvent])
def get_run_events(run_id: UUID, after_event_id: int | None = None, user: AuthenticatedUser = Depends(get_authenticated_user), repo: Repository = Depends(get_repository)) -> list[dict]:
    return repo.list_run_events(run_id, user_id=user.user_id, after_event_id=after_event_id)


@app.post("/runs/{run_id}/cancel", response_model=RunCancelResponse)
def cancel_run(run_id: UUID, request: RunCancelRequest, user: AuthenticatedUser = Depends(get_authenticated_user), repo: Repository = Depends(get_repository)) -> RunCancelResponse:
    require_stage_enabled("MILO_ENABLE_RUN_CANCELLATION", "run cancellation")
    enforce_rate_limit("cancellation", str(user.user_id))
    # Membership authorization before any mutation; 404 for non-members.
    run = repo.get_run(run_id, user_id=user.user_id)
    status = str(run.get("status", ""))
    if status in {"cancellation_requested", "cancelled"}:
        # Idempotent replay: no duplicate cancellation record or event.
        return RunCancelResponse(run_id=run["id"], status=status)
    if status in TERMINAL_STATES:
        raise AppError("RUN_ALREADY_FINISHED", f"run is already {status} and cannot be cancelled", 409)
    run = repo.request_cancellation(run_id, request.reason)
    repo.append_run_event(run_id, "cancellation_requested", {"message": request.reason or "Cancellation requested", "payload": {"reason": request.reason, "requested_by": str(user.user_id)}})
    return RunCancelResponse(run_id=run["id"], status=run["status"])


@app.post("/workflow-proposals", response_model=WorkflowProposal, status_code=201)
def create_workflow_proposal(request: ProposalCreate, user: AuthenticatedUser = Depends(get_authenticated_user), repo: Repository = Depends(get_repository)) -> dict:
    require_stage_enabled("MILO_ENABLE_PROPOSAL_MUTATIONS", "workflow proposal creation")
    # Membership authorization before the proposal row is created.
    repo.get_project(request.project_id, user.user_id)
    proposal = compile_proposal(request.user_request, request.budget_preference, request.force_missing_verifier, request.force_bad_internet)
    return repo.create_workflow_proposal(request.user_request, proposal, project_id=request.project_id, created_by=user.user_id)


@app.get("/workflow-proposals/{proposal_id}", response_model=WorkflowProposal)
def get_workflow_proposal(proposal_id: UUID, user: AuthenticatedUser = Depends(get_authenticated_user), repo: Repository = Depends(get_repository)) -> dict:
    # Membership-scoped since migration 008; legacy proposals without
    # ownership return 404 for every browser identity. The surface flag
    # stays default-off like every other execution surface.
    require_stage_enabled("MILO_ENABLE_PROPOSAL_READS", "workflow proposal read")
    return repo.get_workflow_proposal(proposal_id, user_id=user.user_id)


@app.post("/workflow-proposals/{proposal_id}/approve", response_model=WorkflowProposal)
def approve_workflow_proposal(proposal_id: UUID, request: ProposalDecision, user: AuthenticatedUser = Depends(get_authenticated_user), repo: Repository = Depends(get_repository)) -> dict:
    require_stage_enabled("MILO_ENABLE_PROPOSAL_MUTATIONS", "workflow proposal approval")
    proposal = repo.get_workflow_proposal(proposal_id, user_id=user.user_id)
    if proposal["status"] != "approved":
        raise AppError("PROPOSAL_NOT_APPROVABLE", "only critic-approved proposals can be approved", 409)
    return repo.update_workflow_proposal(proposal_id, {"approved_at": datetime.now(UTC).isoformat(), "rejected_at": None})


@app.post("/workflow-proposals/{proposal_id}/reject", response_model=WorkflowProposal)
def reject_workflow_proposal(proposal_id: UUID, request: ProposalDecision, user: AuthenticatedUser = Depends(get_authenticated_user), repo: Repository = Depends(get_repository)) -> dict:
    require_stage_enabled("MILO_ENABLE_PROPOSAL_MUTATIONS", "workflow proposal rejection")
    repo.get_workflow_proposal(proposal_id, user_id=user.user_id)
    return repo.update_workflow_proposal(proposal_id, {"status": "rejected", "rejected_at": datetime.now(UTC).isoformat(), "approved_at": None})


@app.post("/workflow-proposals/{proposal_id}/revise", response_model=WorkflowProposal)
def revise_workflow_proposal(proposal_id: UUID, request: ProposalRevise, user: AuthenticatedUser = Depends(get_authenticated_user), repo: Repository = Depends(get_repository)) -> dict:
    require_stage_enabled("MILO_ENABLE_PROPOSAL_MUTATIONS", "workflow proposal revision")
    repo.get_workflow_proposal(proposal_id, user_id=user.user_id)
    proposal = compile_proposal(request.user_request, request.budget_preference)
    return repo.update_workflow_proposal(proposal_id, {"user_request": request.user_request, **proposal})


@app.post("/workflow-proposals/{proposal_id}/project", response_model=Project, status_code=201)
def create_project_from_proposal(proposal_id: UUID, request: ProposalProjectCreate, user: AuthenticatedUser = Depends(get_authenticated_user), repo: Repository = Depends(get_repository)) -> dict:
    require_stage_enabled("MILO_ENABLE_PROPOSAL_MUTATIONS", "workflow proposal project creation")
    proposal = repo.get_workflow_proposal(proposal_id, user_id=user.user_id)
    ensure_approved(proposal)
    return repo.create_project_from_proposal(proposal_id, request.slug, request.name, request.description, {"proposal": proposal}, created_by=user.user_id)


@app.post("/workflow-proposals/{proposal_id}/runs", response_model=RunCreated, status_code=202)
def start_approved_proposal_run(proposal_id: UUID, request: ProposalRunCreate, user: AuthenticatedUser = Depends(get_authenticated_user), repo: Repository = Depends(get_repository), launcher: JobLauncher = Depends(get_job_launcher)) -> RunCreated:
    require_stage_enabled("MILO_ENABLE_RUN_CREATION", "workflow proposal run creation")
    proposal = repo.get_workflow_proposal(proposal_id, user_id=user.user_id)
    ensure_approved(proposal)
    repo.get_conversation(request.conversation_id, user.user_id)
    metadata = {**request.metadata, "proposal_id": str(proposal_id)}
    return _create_and_launch_run(repo, launcher, user, request.conversation_id, request.content, metadata, request.idempotency_key)


# --- Internal worker mutation surfaces -------------------------------------
# These routes use the service-to-service worker identity boundary
# (backend/worker_auth.py) and never browser-user authentication. The
# execution flag gates the surface, but a verified, allowlisted worker
# identity is always required in addition: the flag alone never authorizes.

@app.post("/runs/{run_id}/tool-access-requests", status_code=201)
def create_tool_access_request(run_id: UUID, request: ToolAccessRequestCreate, worker: WorkerIdentity = Depends(get_verified_worker), repo: Repository = Depends(get_repository)) -> dict:
    require_stage_enabled("MILO_ENABLE_EXECUTION_CONTROL", "tool access requests")
    row = repo.create_tool_access_request(run_id, request.model_dump())
    repo.append_run_event(run_id, "tool_access_requested", {"message": f"{request.agent} requested {request.tool}", "agent": request.agent, "payload": row})
    return row

@app.post("/runs/{run_id}/tool-grants", status_code=201)
def create_tool_grant(run_id: UUID, request: ToolGrantCreate, worker: WorkerIdentity = Depends(get_verified_worker), repo: Repository = Depends(get_repository)) -> dict:
    require_stage_enabled("MILO_ENABLE_EXECUTION_CONTROL", "tool grants")
    payload = request.model_dump()
    row = repo.create_tool_grant(run_id, payload)
    repo.append_run_event(run_id, "tool_access_granted", {"message": f"{request.tool} granted to {request.agent}", "agent": request.agent, "payload": row})
    return row

@app.post("/runs/{run_id}/tool-usage", status_code=201)
def create_tool_usage(run_id: UUID, request: ToolUsageCreate, worker: WorkerIdentity = Depends(get_verified_worker), repo: Repository = Depends(get_repository)) -> dict:
    require_stage_enabled("MILO_ENABLE_EXECUTION_CONTROL", "tool usage")
    row = repo.create_tool_usage(run_id, request.model_dump())
    repo.append_run_event(run_id, "tool_used", {"message": f"{request.agent} used {request.tool}", "agent": request.agent, "payload": row})
    return row

@app.post("/runs/{run_id}/sources", status_code=201)
def create_source(run_id: UUID, request: SourceCreate, worker: WorkerIdentity = Depends(get_verified_worker), repo: Repository = Depends(get_repository)) -> dict:
    require_stage_enabled("MILO_ENABLE_EXECUTION_CONTROL", "source recording")
    row = repo.create_source(run_id, request.model_dump())
    repo.append_run_event(run_id, "source_recorded", {"message": request.title, "agent": request.agent, "payload": row})
    return row

@app.post("/runs/{run_id}/claims", status_code=201)
def create_claim(run_id: UUID, request: ClaimCreate, worker: WorkerIdentity = Depends(get_verified_worker), repo: Repository = Depends(get_repository)) -> dict:
    require_stage_enabled("MILO_ENABLE_EXECUTION_CONTROL", "claim recording")
    row = repo.create_claim(run_id, request.model_dump())
    repo.append_run_event(run_id, "claim_recorded", {"message": f"{request.entity_key}.{request.field_key}", "agent": request.agent, "payload": row})
    return row

@app.post("/runs/{run_id}/conflicts", status_code=201)
def create_conflict(run_id: UUID, request: ConflictCreate, worker: WorkerIdentity = Depends(get_verified_worker), repo: Repository = Depends(get_repository)) -> dict:
    require_stage_enabled("MILO_ENABLE_EXECUTION_CONTROL", "conflict recording")
    row = repo.create_conflict(run_id, request.model_dump())
    repo.append_run_event(run_id, "conflict_detected", {"message": f"{request.entity_key}.{request.field_key}", "payload": row})
    return row


@app.post("/internal/runs/{run_id}/events", status_code=201)
def create_worker_run_event(run_id: UUID, request: WorkerRunEventCreate, worker: WorkerIdentity = Depends(get_verified_worker), repo: Repository = Depends(get_repository)) -> dict:
    require_stage_enabled("MILO_ENABLE_EXECUTION_CONTROL", "worker run events")
    if request.event_type not in EVENT_TYPES:
        raise AppError("UNKNOWN_EVENT_TYPE", f"unknown event type {request.event_type}", 422)
    return repo.append_run_event(run_id, request.event_type, {"message": request.message, "agent": request.agent, "phase": request.phase, "progress": request.progress, "payload": {**request.payload, "worker_identity": worker.service_account_email}})


@app.post("/internal/runs/{run_id}/complete")
def complete_run_from_worker(run_id: UUID, request: WorkerRunCompleteRequest, worker: WorkerIdentity = Depends(get_verified_worker), repo: Repository = Depends(get_repository)) -> dict:
    require_stage_enabled("MILO_ENABLE_EXECUTION_CONTROL", "worker run completion")
    run = repo.mark_run_complete(run_id, request.output)
    repo.append_run_event(run_id, "run_completed", {"message": "Run completed by worker", "payload": {"worker_identity": worker.service_account_email}})
    return run


@app.post("/internal/runs/{run_id}/fail")
def fail_run_from_worker(run_id: UUID, request: WorkerRunFailRequest, worker: WorkerIdentity = Depends(get_verified_worker), repo: Repository = Depends(get_repository)) -> dict:
    require_stage_enabled("MILO_ENABLE_EXECUTION_CONTROL", "worker run failure")
    run = repo.mark_run_failed(run_id, request.code, request.message)
    repo.append_run_event(run_id, "run_failed", {"message": request.message, "payload": {"code": request.code, "worker_identity": worker.service_account_email}})
    return run

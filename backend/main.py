from uuid import UUID
from fastapi import Depends, FastAPI
from backend.dependencies import get_repository
from backend.errors import install_error_handlers
from backend.repository import Repository
from backend.schemas import Conversation, ConversationCreate, HealthResponse, Project, Run, RunCreate, RunCreated, RunEvent

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
    run = repo.create_queued_run(conversation_id, UUID(str(message["id"])), request.content, request.metadata)
    return RunCreated(run_id=run["id"], status=run["status"])


@app.get("/runs/{run_id}", response_model=Run)
def get_run(run_id: UUID, repo: Repository = Depends(get_repository)) -> dict:
    return repo.get_run(run_id)


@app.get("/runs/{run_id}/events", response_model=list[RunEvent])
def get_run_events(run_id: UUID, repo: Repository = Depends(get_repository)) -> list[dict]:
    return repo.list_run_events(run_id)

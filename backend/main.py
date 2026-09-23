"""API package namespace."""

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError
from starlette.middleware.base import BaseHTTPMiddleware

from backend.budget import BudgetConfig
from backend.catalog import review as catalog_review
from backend.catalog.scope import batches as work_scope_batches
from backend.catalog.scope import service as work_scopes
from backend.config import get_settings
from backend.auth import AuthenticatedUser, get_authenticated_user
from backend.dependencies import get_job_launcher, get_repository
from backend.execution_guard import ExecutionSurfaceGuardMiddleware, is_stage_enabled
from backend.job_launcher import JobLauncher, JobLaunchUncertain
from backend.errors import AppError, install_error_handlers
from backend.repository import Repository
from backend.schemas import (
    CatalogCanonicalPage,
    CatalogPageMeta,
    CatalogReviewPage,
    Conversation,
    ConversationCreate,
    EffectiveConcurrency,
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
    RunIdentityRecord, ProductOutcomeRecord, RunLimits, RunSummary,
    RunUsage,
    WorkflowProposal,
    ToolAccessRequestCreate, ToolGrantCreate, ToolUsageCreate, SourceCreate, ClaimCreate, ConflictCreate,
    WorkerRunCompleteRequest, WorkerRunEventCreate, WorkerRunFailRequest,
    WorkScopeBatchRunCreated, WorkScopeBatchStart, WorkScopeCapabilities,
    WorkScopeControlResult, WorkScopeCreate, WorkScopeDirectory, WorkScopeMutationResult,
    WorkScopeOpen, WorkScopeProgress, WorkScopeRevise, WorkScopeState,
)
from backend.rate_limit import enforce_rate_limit
from backend.event_registry import is_known_event_type
from backend.run_identity import (
    PRODUCT_WORKFLOW_KEYS,
    RUN_IDENTITY_FIELD,
    RunIdentity,
    RunIdentityError,
    execution_identity_problems,
    require_identity,
)
from backend.runtime import TERMINAL_STATES
from backend.finalization import RunFinalizer, TerminalClaim
from backend.vehicle_catalog_scope import (SCOPE_METADATA_KEY, VehicleCatalogScopeError,
                                           refuse_supplied_scope,
                                           scope_from_project_configuration)
from backend.product_outcome import ProductOutcomeError, outcome_from_record
from backend.worker_auth import WorkerIdentity, get_verified_worker
from backend.workflow_proposals import compile_proposal, ensure_approved

settings = get_settings()

# Fail closed at import/startup on a dangerous production configuration
# combination (execution without budgets, wildcard CORS, secret in
# NEXT_PUBLIC_*, in-memory rate limiter in production, etc.). Local/dev only
# warns. Kept before app construction so a misconfigured production image
# refuses to start rather than serving unsafe defaults.
from backend.production_config import validate_production_config  # noqa: E402

validate_production_config()

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


SAFE_LAUNCH_ERROR_CLASSES = {
    "launch_failed": "worker_launch_failed",
    "launch_unknown": "worker_launch_unknown",
}


def _safe_run_usage(raw: object) -> RunUsage | None:
    """Project the durable runs.usage object onto the bounded public contract.

    ``runs.usage`` is ``jsonb NOT NULL DEFAULT '{}'`` (migration 010), so a run
    that has not settled a model call stores an empty object. That means
    "nothing recorded", not "zero spend", and is returned as null rather than
    as a row of zeroes.

    Anything that cannot be represented as RunUsage is dropped instead of
    raising. This is the endpoint the workspace polls, so degrading to "no
    usage reported" -- which the client already handles -- is safer than
    failing the whole run read on an unexpected stored value. Keys outside the
    schema are ignored by it and can never reach the browser.
    """
    if not isinstance(raw, dict) or not raw:
        return None
    try:
        usage = RunUsage.model_validate(raw)
    except ValidationError:
        return None
    # An object carrying only unrecognized keys projects onto nothing.
    return usage if usage.model_dump(exclude_none=True) else None


def _safe_run_identity(raw: object, run_id: object) -> RunIdentityRecord | None:
    """Project only a TRUSTWORTHY stored identity onto the browser contract.

    Browser reads stay available when legacy/corrupt identity is encountered,
    but the identity itself is omitted. The frontend then renders a bounded
    identity-unavailable state; it never falls back to today's project workflow.
    """
    if not isinstance(raw, dict) or not raw:
        return None
    try:
        identity = RunIdentity.from_record(raw, run_id=run_id)
        return RunIdentityRecord.model_validate(identity.as_record())
    except (RunIdentityError, ValidationError):
        return None


def _catalog_page_meta(page: catalog_review.CatalogPage) -> CatalogPageMeta:
    """The page metadata a bounded catalog read states.

    `total` and `has_more` come straight from the page, which took the total
    from the database's own exact count and left it `None` when the database
    stated none. Neither is derived from `len(items)` here, and neither is
    defaulted to `0`/`False`: an unknown total rendered as zero would be the
    claim that the catalog is empty.
    """
    return CatalogPageMeta(limit=page.limit, offset=page.offset, total=page.total,
                           has_more=page.has_more)


#: The events the canonical finalizer writes atomically with a terminal status.
TERMINAL_EVENT_TYPES = frozenset({"run_completed", "run_partial_success", "run_failed", "run_cancelled"})

#: "Not supplied": lets a history page hand pre-fetched limits and terminal
#: events to the projection instead of re-reading them per row.
_UNSET = object()


def _safe_product_outcome(run: dict, repo: Repository | None) -> ProductOutcomeRecord | None:
    """Project the canonical ProductOutcome the finalizer recorded, or null.

    The record lives on the terminal event `finalize_run_guarded` inserted in
    the same transaction as the terminal status (backend/finalization.py), so
    it is read from there and nowhere else: not derived from `output` here,
    not accepted from a request body, not reconstructed from other events. A
    live run, a terminal that carries no product (cancellation, timeout,
    budget stop), a historical run finalized before the canonical path, and a
    record that does not re-validate through `outcome_from_record` all project
    `null` -- an absent verdict is stated as absent, never invented.
    """
    if repo is None or not hasattr(repo, "terminal_run_event"):
        return None
    if str(run.get("status") or "") not in TERMINAL_STATES:
        return None
    try:
        event = repo.terminal_run_event(run.get("id"))
    except Exception:
        return None
    return _project_product_outcome(event)


def _project_product_outcome(event: object) -> ProductOutcomeRecord | None:
    """The pure half of the projection: one terminal event -> record or null."""
    if not isinstance(event, dict) or event.get("event_type") not in TERMINAL_EVENT_TYPES:
        return None
    payload = event.get("payload")
    record = payload.get("product_outcome") if isinstance(payload, dict) else None
    if not isinstance(record, dict):
        return None
    try:
        outcome = outcome_from_record(record)
        return ProductOutcomeRecord.model_validate(outcome.as_record())
    except (ProductOutcomeError, ValidationError, TypeError, ValueError):
        return None


def _effective_concurrency() -> EffectiveConcurrency | None:
    """The concurrency this deployment really runs at, from server-owned truth.

    Resolved from the ONE canonical runtime policy (`resolve_runtime_policy`),
    the provider configuration it yields, and the organization ceiling
    (`QuotaConfig`). The provider-admitted widths are computed exactly as the
    engines compute them: Swarm V2 runs
    ``min(configured active workers, provider concurrency)`` and V1 runs its
    technical units through the same per-process provider profile. A policy
    that refuses to resolve (a misconfigured paid deployment) states ``None``
    rather than a plausible number.
    """
    from backend.provider_quota import (SEARCH_BASIC, SEARCH_PRO, SEARCH_QPS_VERIFIED,
                                        QuotaConfig)
    from backend.runtime_policy import RuntimePolicyError, resolve_runtime_policy

    try:
        policy = resolve_runtime_policy()
        provider = policy.provider_limits()
        budget = policy.budget_config()
    except (RuntimePolicyError, ValueError):
        return None
    try:
        organization = QuotaConfig.from_env()
        ceiling: int | None = int(organization.max_concurrency)
        search_basic, search_pro = (int(organization.search_qps[0]), int(organization.search_qps[1]))
    except ValueError:
        ceiling, search_basic, search_pro = None, None, None
    v1 = int(policy.v1_technical_parallelism)
    v2 = int(policy.swarm_max_active_workers)
    per_process = int(provider.max_concurrency)
    effective = per_process if ceiling is None else min(per_process, ceiling)
    return EffectiveConcurrency(
        v1_technical_parallelism=v1,
        v2_max_active_workers=v2,
        provider_max_concurrency=per_process,
        provider_organization_ceiling=ceiling,
        provider_effective_concurrency=effective,
        v1_provider_admitted=min(v1, effective),
        v2_provider_admitted=min(v2, effective),
        max_concurrent_runs_per_user=budget.max_concurrent_runs_per_user,
        max_concurrent_runs_per_project=budget.max_concurrent_runs_per_project,
        search_basic_qps=search_basic,
        search_pro_qps=search_pro,
        search_qps_verified=bool(SEARCH_QPS_VERIFIED[SEARCH_BASIC] and SEARCH_QPS_VERIFIED[SEARCH_PRO]),
        paid_posture=bool(policy.paid),
    )


def _run_limits() -> RunLimits | None:
    """The per-run ceilings this deployment enforces, as plain numbers."""
    try:
        config = BudgetConfig.from_env()
    except Exception:
        return None
    limits = RunLimits(
        max_model_calls_per_run=config.max_model_calls_per_run,
        max_total_tokens_per_run=config.max_total_tokens_per_run,
        max_cost_per_run=config.max_cost_per_run,
        max_run_duration_seconds=config.max_run_duration_seconds,
        max_agent_steps=config.max_agent_steps,
        concurrency=_effective_concurrency(),
    )
    return limits if limits.model_dump(exclude_none=True) else None


def _safe_run_response(run: dict, repo: Repository | None = None, *,
                       limits: RunLimits | None | object = _UNSET,
                       terminal_event: object = _UNSET) -> dict:
    """Return a browser-safe run shape.

    Launch exception messages are operational data and may include provider,
    platform or stack details. Browser responses only expose a finite
    classification and whether operator reconciliation is required.

    Aggregate usage is the run's own authoritative accounting and is exposed
    through the typed RunUsage contract; the browser must never reconstruct it
    by summing events.
    """
    launch_state = run.get("launch_state") or "pending"
    safe = dict(run)
    safe["launch_state"] = launch_state
    safe["launch_error_class"] = SAFE_LAUNCH_ERROR_CLASSES.get(launch_state)
    safe["launch_reconciliation_required"] = launch_state == "launch_unknown"
    safe["usage"] = _safe_run_usage(run.get("usage"))
    # The run's own immutable identity, so the browser can render a historical
    # run as the engine it WAS rather than as whatever its project is today.
    safe["run_identity"] = _safe_run_identity(run.get(RUN_IDENTITY_FIELD), run.get("id"))
    # The canonical product verdict and the ceilings the run executes under.
    # Both are projections of durable/deployment truth; neither is derived
    # from the payload or from anything the browser could have supplied.
    if terminal_event is _UNSET:
        safe["product_outcome"] = _safe_product_outcome(run, repo)
    else:
        terminal = str(run.get("status") or "") in TERMINAL_STATES
        safe["product_outcome"] = _project_product_outcome(terminal_event) if terminal else None
    safe["limits"] = _run_limits() if limits is _UNSET else limits
    safe.pop("launch_error", None)
    safe.pop("lease_token", None)
    return safe


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
    project_id = repo.get_conversation(conversation_id).get("project_id")
    enforce_rate_limit("run_creation_project", str(project_id or conversation_id))
    # The Vehicle Catalog scope is resolved by the SERVER from the project's
    # own configuration and bound below. A request that tries to name it is
    # refused before anything is read or written, so the value a run carries
    # under that key can only ever be the one this function put there.
    try:
        refuse_supplied_scope(metadata)
    except VehicleCatalogScopeError as exc:
        raise AppError(exc.code, exc.safe_message, 422) from None
    fingerprint = _request_fingerprint(content, metadata)
    config = BudgetConfig.from_env()
    run = None
    if idempotency_key and hasattr(repo, "find_run_by_idempotency"):
        existing = repo.find_run_by_idempotency(conversation_id, user.user_id, idempotency_key)
        if existing is not None:
            if existing.get("request_fingerprint") not in (None, fingerprint):
                raise AppError("IDEMPOTENCY_CONFLICT", "idempotency key was already used with a different payload", 409)
            run = existing
    if run is None:
        metadata = {**metadata, "requested_by": str(user.user_id)}
        if idempotency_key:
            metadata["idempotency_key"] = idempotency_key

        if not hasattr(repo, "create_message_and_run"):
            # Console 6 requires identity to be part of the SAME durable
            # transaction that inserts the run. A repository without that
            # primitive cannot safely create an executable run.
            raise AppError(
                "RUN_IDENTITY_ATOMIC_CREATION_REQUIRED",
                "repository cannot create a run with immutable identity atomically",
                503,
            )

        project = repo.get_project(project_id) if project_id else {}
        workflow_key = project.get("workflow_key")
        if workflow_key == "vehicle_catalog_v1":
            # V1 maps exactly the scope its PROJECT configures -- the trusted
            # relation, the same one the run's workflow is read from. It is
            # bound into the run in the creation transaction below, so it is
            # immutable afterwards. A project that configures no valid scope
            # is refused HERE, before any message, run or launch exists,
            # rather than silently mapping the engine defaults.
            try:
                scope = scope_from_project_configuration(project.get("configuration"))
            except VehicleCatalogScopeError as exc:
                raise AppError(exc.code, exc.safe_message, 409) from None
            metadata[SCOPE_METADATA_KEY] = scope.as_record()
        new_run_id, identity = _new_run_identity(workflow_key)

        # Transaction-safe path: idempotent replay, concurrency admission,
        # message insert, run insert and immutable identity all commit together.
        # The SQL function re-checks the trusted project workflow inside that
        # same transaction, closing the read->insert drift window.
        result = repo.create_message_and_run(
            conversation_id,
            content,
            metadata,
            user.user_id,
            idempotency_key,
            fingerprint,
            config.max_concurrent_runs_per_user,
            config.max_concurrent_runs_per_project,
            run_id=new_run_id,
            run_identity=identity.as_record(),
        )
        run = result["run"]
        if not result.get("created", True) and run.get("request_fingerprint") not in (None, fingerprint):
            raise AppError("IDEMPOTENCY_CONFLICT", "idempotency key was already used with a different payload", 409)

    return _launch_created_run(repo, launcher, run)


def _new_run_identity(workflow_key: object) -> tuple[UUID, RunIdentity]:
    """A fresh run id and the immutable identity it will be BORN with.

    Refused before anything is written when this runtime cannot bind an
    executable identity -- the same gate for every run this API creates.
    """
    new_run_id = uuid4()
    try:
        identity = RunIdentity.bind(new_run_id, str(workflow_key or ""))
    except RunIdentityError as exc:
        raise AppError(
            "RUN_IDENTITY_NOT_BOUND",
            "the run's immutable identity could not be established",
            409,
        ) from exc
    if execution_identity_problems(identity):
        # A run must not be born already unable to execute. In particular,
        # an absent/malformed MILO_RELEASE_SHA is not recorded as an empty
        # wildcard and left queued for a worker to refuse later.
        raise AppError(
            "RUN_IDENTITY_RUNTIME_MISMATCH",
            "this runtime cannot bind an executable immutable run identity",
            503,
        )
    return new_run_id, identity


def _launch_created_run(repo: Repository, launcher: JobLauncher, run: dict) -> RunCreated:
    """Launch a run the durable creator returned: THE launch step, for every run.

    The identity gate, the launch compare-and-set, the launcher call and its
    failure handling are the same whether the run came from a conversation
    request or from a Mapping Plan batch. A replayed run is launched only if
    its launch never happened or definitely failed; anything else returns the
    existing run untouched.
    """
    run_id = UUID(str(run["id"]))
    # No executable run may cross the launch boundary without a persisted,
    # readable identity. Legacy rows remain history, not runnable work.
    try:
        persisted_identity = require_identity(run)
    except RunIdentityError as exc:
        raise AppError(
            "RUN_IDENTITY_REQUIRED",
            "run has no trustworthy immutable identity and cannot be launched",
            409,
        ) from exc
    if execution_identity_problems(persisted_identity):
        # Includes idempotent replay of a queued run created by an older
        # release/policy. Do not launch a Cloud Run execution that the worker
        # is guaranteed to refuse at its own pre-claim identity gate.
        raise AppError(
            "RUN_IDENTITY_RUNTIME_MISMATCH",
            "run identity does not match this runtime and cannot be launched",
            409,
        )
    if run.get("status") not in (None, "queued"):
        # Cancelled-before-launch or an already-progressed duplicate: never launch.
        return RunCreated(run_id=run["id"], status=run["status"])
    if run.get("launch_state") == "launch_unknown":
        # Uncertain launch outcome: an execution may already be running, so
        # never trigger a second one automatically. Reconciliation is manual
        # or via worker lease expiry.
        return RunCreated(run_id=run["id"], status=run["status"])
    # Atomic launch ownership: exactly one request wins the CAS and calls
    # JobLauncher.launch(); concurrent replays observe launching/launched
    # and return the existing run without launching again.
    if hasattr(repo, "try_acquire_launch"):
        acquired = repo.try_acquire_launch(run_id)
        if acquired is None:
            current = repo.get_run(run_id)
            return RunCreated(run_id=current["id"], status=current["status"])
        run = acquired
    elif hasattr(repo, "set_launch_state"):
        # Legacy two-step path for simple test fakes without the CAS. It now
        # mirrors try_acquire_launch's acquirable set exactly rather than
        # naming the two states it happened to think of, so a fake can never
        # be laxer than production: only a pending or previously-failed launch
        # may be started. In particular 'none' -- the state an operator
        # capture run rests in -- is not launchable here either. A fake that
        # models no launch_state at all keeps its previous behaviour.
        if run.get("launch_state") not in {None, "pending", "launch_failed"}:
            return RunCreated(run_id=run["id"], status=run["status"])
        repo.set_launch_state(run_id, "launching")
    try:
        launch = launcher.launch(run_id)
    except JobLaunchUncertain as exc:
        # Park for reconciliation: never auto-relaunch after an uncertain
        # response, because a worker may already be executing this run.
        if hasattr(repo, "set_launch_state"):
            repo.set_launch_state(run_id, "launch_unknown", error={"message": str(exc)[:500]})
        if hasattr(repo, "append_run_event"):
            repo.append_run_event(run_id, "launch_failed", {"message": "Worker launch outcome unknown; run parked for reconciliation", "payload": {"recoverable": False, "reconciliation_required": True}})
        raise AppError("JOB_LAUNCH_UNKNOWN", "worker launch outcome is unknown; the run is parked for reconciliation and will not be launched twice", 502) from exc
    except Exception as exc:
        # Definite failure: leave a clear recoverable state; a retry with the
        # same idempotency key re-acquires launch ownership via the CAS.
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


# --- CODE-3: the bounded, read-only catalog review surface -------------------
#
# Two GETs, and nothing else. There is no POST/PUT/PATCH/DELETE counterpart
# anywhere in this file, no catalog write is reachable from either handler, and
# neither is listed in `execution_guard.SURFACE_RULES` -- DELIBERATELY.
#
# Reading durable state is not execution. `MILO_ENABLE_CATALOG_EXECUTION` stops
# the catalog WRITE path so an operator can roll back without stopping the
# product, and it is explicitly non-destructive: the rows stay. The operator who
# just pulled that switch is the one who most needs to see what is there, so
# gating this read behind it would make the rollback blind. The same reasoning
# rules out the paid-execution flag, run creation and promotion enablement.
#
# The project id is an AUTHORIZATION ANCHOR, not an owner. The durable catalog
# is global; `repo.get_project(project_id, user.user_id)` is the repository's
# normal membership check and raises the same non-disclosing 404 a non-member
# receives everywhere else, and only then does the global read happen. Nothing
# below pretends a canonical row belongs to the project.


# Neither handler DECLARES its query parameters, and that is load-bearing.
#
# A declared `limit: int` is bound and validated by FastAPI BEFORE the handler
# body runs, so `?limit=abc` answers 422 before the membership check -- and a
# non-member would then receive a different response depending on what they
# sent, which is a disclosure through the authorization boundary. Reading the
# raw query string inside the handler is what keeps the four steps in order:
#
#     1. authenticate        (the dependency below)
#     2. authorize membership (`repo.get_project`, the non-disclosing 404)
#     3. validate the query contract (names, duplicates, then values)
#     4. read the catalog
#
# The accepted names are `catalog_review.CANONICAL_QUERY_PARAMETERS` and
# `REVIEW_QUERY_PARAMETERS`; anything else fails closed on one static code.


@app.get("/projects/{project_id}/catalog/canonical", response_model=CatalogCanonicalPage)
def get_catalog_canonical_page(
    project_id: UUID,
    request: Request,
    user: AuthenticatedUser = Depends(get_authenticated_user),
    repo: Repository = Depends(get_repository),
) -> CatalogCanonicalPage:
    """A bounded page of the CURRENT canonical catalog.

    Accepts `limit`, `offset`, `manufacturer`, `commercial_model`, `model_year`
    and `canonical_key`, each at most once. There is no parameter for a table, a
    column, an ordering, a page bound or a status -- those are server data in
    `backend/catalog/review.py` and in the repository method it calls -- and a
    name outside the allowlist is refused rather than ignored.
    """
    repo.get_project(project_id, user.user_id)
    query = catalog_review.canonical_query(request.query_params)
    page = catalog_review.canonical_catalog(repo, **query)
    return CatalogCanonicalPage(page=_catalog_page_meta(page), items=page.items)


@app.get("/projects/{project_id}/catalog/review-candidates", response_model=CatalogReviewPage)
def get_catalog_review_candidates(
    project_id: UUID,
    request: Request,
    user: AuthenticatedUser = Depends(get_authenticated_user),
    repo: Repository = Depends(get_repository),
) -> CatalogReviewPage:
    """A bounded page of candidates whose durable status is `ready_for_review`.

    Accepts `limit`, `offset`, `manufacturer`, `commercial_model` and
    `model_year`, each at most once. The snapshot is resolved by the
    repository's own trusted rule against the pinned WLTP resource constant, and
    the status is fixed: `resource_id`, `snapshot_key`, `snapshot_id`, `status`
    and `allow_incomplete` are not accepted here, so a browser cannot point this
    anywhere else and cannot be told it inspected something it did not.
    """
    repo.get_project(project_id, user.user_id)
    query = catalog_review.review_query(request.query_params)
    page = catalog_review.review_candidates(repo, **query)
    return CatalogReviewPage(
        available=page.available, unavailable_reason=page.unavailable_reason,
        status=catalog_review.REVIEW_CANDIDATE_STATUS, snapshot=page.snapshot,
        page=_catalog_page_meta(page), items=page.items)


# --- Mapping plans: the canonical WorkScope (backend/catalog/scope/) ---------
#
# Four reads and two writes. The reads are membership-authorized and ungated,
# for the reason CODE-3's reads are: reading a plan executes nothing, and the
# operator who turned the writes off still needs to see what exists. The two
# writes are behind `MILO_ENABLE_WORK_SCOPE_MUTATIONS`, enforced before the body
# is read by `ExecutionSurfaceGuardMiddleware` and again here.
#
# Nothing below prepares Government data, creates a run, reserves a budget or
# reaches a provider. A plan is a draft, and `capabilities` says so.


@app.get("/projects/{project_id}/work-scope/capabilities", response_model=WorkScopeCapabilities)
def get_work_scope_capabilities(project_id: UUID, user: AuthenticatedUser = Depends(get_authenticated_user), repo: Repository = Depends(get_repository)) -> dict:
    return work_scopes.capabilities(repo, user.user_id, project_id)


@app.get("/projects/{project_id}/work-scope/directory", response_model=WorkScopeDirectory)
def get_work_scope_directory(project_id: UUID, user: AuthenticatedUser = Depends(get_authenticated_user), repo: Repository = Depends(get_repository)) -> dict:
    return work_scopes.directory(repo, user.user_id, project_id)


@app.get("/conversations/{conversation_id}/work-scopes/open", response_model=WorkScopeOpen)
def get_open_work_scope(conversation_id: UUID, user: AuthenticatedUser = Depends(get_authenticated_user), repo: Repository = Depends(get_repository)) -> dict:
    return work_scopes.open_work_scope(repo, user.user_id, conversation_id)


@app.post("/conversations/{conversation_id}/work-scopes", response_model=WorkScopeMutationResult, status_code=201)
def create_work_scope(conversation_id: UUID, request: WorkScopeCreate, user: AuthenticatedUser = Depends(get_authenticated_user), repo: Repository = Depends(get_repository)) -> dict:
    require_stage_enabled(work_scopes.WORK_SCOPE_MUTATIONS_FLAG, "work scope creation")
    return work_scopes.create_work_scope(repo, user.user_id, conversation_id, request.instruction, request.edit)


@app.get("/work-scopes/{work_scope_id}", response_model=WorkScopeState)
def get_work_scope(work_scope_id: UUID, user: AuthenticatedUser = Depends(get_authenticated_user), repo: Repository = Depends(get_repository)) -> dict:
    return work_scopes.work_scope(repo, user.user_id, work_scope_id)


@app.post("/work-scopes/{work_scope_id}/revisions", response_model=WorkScopeMutationResult)
def revise_work_scope(work_scope_id: UUID, request: WorkScopeRevise, user: AuthenticatedUser = Depends(get_authenticated_user), repo: Repository = Depends(get_repository)) -> dict:
    require_stage_enabled(work_scopes.WORK_SCOPE_MUTATIONS_FLAG, "work scope revision")
    return work_scopes.revise_work_scope(repo, user.user_id, work_scope_id, request.expected_revision,
                                         request.expected_digest, request.instruction, request.edit)


# --- Mapping Plan batch runs (backend/catalog/scope/batches.py) --------------
#
# One read and three writes. The read is membership-authorized and ungated, like
# the plan reads above. Starting a batch is RUN CREATION: it needs
# `MILO_ENABLE_RUN_CREATION` AND `MILO_ENABLE_WORK_SCOPE_BATCHES`, it is rate
# limited as run creation, and it goes through the same identity gate and the
# same launch step as every other run. Pause and resume need the batches flag.
# Each is enforced by `ExecutionSurfaceGuardMiddleware` before the body is read,
# and again here. There is no automatic next batch anywhere: one request, one
# batch run.


@app.get("/work-scopes/{work_scope_id}/progress", response_model=WorkScopeProgress)
def get_work_scope_progress(work_scope_id: UUID, user: AuthenticatedUser = Depends(get_authenticated_user), repo: Repository = Depends(get_repository)) -> dict:
    return work_scope_batches.progress(repo, user.user_id, work_scope_id)


@app.post("/work-scopes/{work_scope_id}/runs", response_model=WorkScopeBatchRunCreated, status_code=202)
def start_work_scope_batch(work_scope_id: UUID, request: WorkScopeBatchStart, user: AuthenticatedUser = Depends(get_authenticated_user), repo: Repository = Depends(get_repository), launcher: JobLauncher = Depends(get_job_launcher)) -> dict:
    require_stage_enabled("MILO_ENABLE_RUN_CREATION", "work scope batch run creation")
    require_stage_enabled(work_scope_batches.WORK_SCOPE_BATCHES_FLAG, "work scope batch runs")
    enforce_rate_limit("run_creation_user", str(user.user_id))
    # Membership, workflow, a stale head and a batch that is not next are all
    # refused here, before an identity is bound or anything is written.
    prepared = work_scope_batches.batch_request(
        repo, user.user_id, work_scope_id, expected_revision=request.expected_revision,
        expected_digest=request.expected_digest, batch_id=request.batch_id)
    enforce_rate_limit("run_creation_project", prepared["project_id"])
    new_run_id, identity = _new_run_identity(work_scope_batches.BATCH_RUN_WORKFLOW)
    config = BudgetConfig.from_env()
    created = work_scope_batches.create_batch_run(
        repo, user.user_id, work_scope_id, expected_revision=request.expected_revision,
        expected_digest=request.expected_digest, batch_id=request.batch_id,
        idempotency_key=request.idempotency_key, content=prepared["content"],
        fingerprint=prepared["fingerprint"], run_id=new_run_id,
        run_identity=identity.as_record(),
        max_user_active=config.max_concurrent_runs_per_user,
        max_project_active=config.max_concurrent_runs_per_project)
    binding = created["binding"]
    launched = _launch_created_run(repo, launcher, created["run"])
    return {"run_id": launched.run_id, "status": launched.status,
            "work_scope_id": work_scope_id, "batch_id": binding["batch_id"],
            "attempt": binding["attempt"], "created": bool(created.get("created"))}


@app.post("/work-scopes/{work_scope_id}/pause", response_model=WorkScopeControlResult)
def pause_work_scope(work_scope_id: UUID, user: AuthenticatedUser = Depends(get_authenticated_user), repo: Repository = Depends(get_repository)) -> dict:
    require_stage_enabled(work_scope_batches.WORK_SCOPE_BATCHES_FLAG, "work scope pause")
    return work_scope_batches.set_paused(repo, user.user_id, work_scope_id, True)


@app.post("/work-scopes/{work_scope_id}/resume", response_model=WorkScopeControlResult)
def resume_work_scope(work_scope_id: UUID, user: AuthenticatedUser = Depends(get_authenticated_user), repo: Repository = Depends(get_repository)) -> dict:
    require_stage_enabled(work_scope_batches.WORK_SCOPE_BATCHES_FLAG, "work scope resume")
    return work_scope_batches.set_paused(repo, user.user_id, work_scope_id, False)


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
    return _safe_run_response(repo.get_run(run_id, user_id=user.user_id), repo)


@app.get("/runs/{run_id}/export")
def export_run(run_id: UUID, user: AuthenticatedUser = Depends(get_authenticated_user), repo: Repository = Depends(get_repository)) -> dict:
    """The canonical export of ONE finished run, for the browser.

    A READ over exactly the run named, behind the same membership
    authorization as `GET /runs/{id}` (a non-member sees 404), wrapped by the
    one export authority `backend.export_envelope.build_export_envelope`. No
    export logic lives in the browser: the envelope it downloads is the
    document this route returned, byte for byte.

    Fail closed, with a static reason: a live run, a run whose immutable
    identity is absent or unreadable, a non-product workflow, an identity
    not bound to a release, and a stored Swarm V2 product that is not
    contract-valid are all refused (409 RUN_NOT_EXPORTABLE). The envelope
    is re-validated before it leaves, and its usage is the same bounded
    public projection the run read exposes.
    """
    from backend.export_envelope import (ExportRefused, build_export_envelope,
                                         validate_export_envelope)

    run = repo.get_run(run_id, user_id=user.user_id)
    try:
        envelope = build_export_envelope(run)
        validate_export_envelope(envelope)
    except ExportRefused as exc:
        # Every ExportRefused message is a static, code-owned reason.
        raise AppError("RUN_NOT_EXPORTABLE", f"run cannot be exported: {exc}", 409) from None
    usage = _safe_run_usage(run.get("usage"))
    envelope["usage"] = usage.model_dump(mode="json") if usage is not None else {}
    return envelope


#: The most runs one history read returns. The list is for choosing a run to
#: reopen; it is bounded so a busy conversation cannot make the read unbounded.
MAX_RUN_HISTORY = 50


@app.get("/conversations/{conversation_id}/runs", response_model=list[RunSummary])
def list_conversation_runs(conversation_id: UUID, limit: int = 20, user: AuthenticatedUser = Depends(get_authenticated_user), repo: Repository = Depends(get_repository)) -> list[dict]:
    """A conversation's run history, newest first, membership-scoped.

    This is what lets a completed result outlive the browser session: the run
    ids the workspace remembers live in session storage, and a browser restart
    forgets them. The history is durable truth read back through the same
    authorization the single-run read uses, and each row carries the run's
    immutable identity and canonical outcome so the browser can list a
    historical run as the engine and the verdict it actually was.
    """
    if limit < 1 or limit > MAX_RUN_HISTORY:
        raise AppError("INVALID_LIMIT", f"limit must be between 1 and {MAX_RUN_HISTORY}", 422)
    if not hasattr(repo, "list_conversation_runs"):
        raise AppError("RUN_HISTORY_UNAVAILABLE", "run history is not available on this repository", 503)
    # Membership authorization precedes every read; a non-member sees 404.
    repo.get_conversation(conversation_id, user.user_id)
    rows = repo.list_conversation_runs(conversation_id, user_id=user.user_id, limit=limit)
    # One limits read and ONE terminal-event read for the whole page, never
    # one of each per row.
    limits = _run_limits()
    events: dict[str, dict] = {}
    if hasattr(repo, "terminal_run_events"):
        try:
            events = repo.terminal_run_events([row["id"] for row in rows if row.get("id")]) or {}
        except Exception:
            events = {}
    return [_safe_run_response(row, repo, limits=limits, terminal_event=events.get(str(row.get("id"))))
            for row in rows]


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

# ---------------------------------------------------------------------------
# Worker mutation surfaces.
# ---------------------------------------------------------------------------
#
# These nine routes are the HTTP face of the worker's durable writes, and they
# were the alternate persistence path this repository's fencing did not cover.
# A request proved it came from an approved worker SERVICE IDENTITY and was
# then trusted to mutate run-owned state: events, tool access, tool grants,
# evidence and -- through /complete and /fail -- the run's TERMINAL status. A
# worker whose lease had been reclaimed still held valid credentials, so it
# could finish a run another worker was executing.
#
# Four of them were additionally dead on arrival: `create_tool_usage`,
# `create_source`, `create_claim` and `create_conflict` call repository methods
# whose lease arguments became keyword-only and REQUIRED when the evidence
# writes were fenced (migration 20260823000100), and the routes never passed
# them, so every call raised TypeError. They were fenced at the repository and
# left unfenced at the route.
#
# Every one of them now carries the same `run + attempt + worker + lease`
# contract the in-process worker carries (`WorkerLeaseFence`), and every write
# travels through the guarded RPC that settles it against the live lease under
# the DATABASE clock. Identity answers "is this a worker?"; the fence answers
# "is this THE worker of THIS attempt of THIS run?", and only the second one
# stops a stale worker.
#
# `request.content()` is what gets written and it never includes the fence:
# the lease token is a credential, and the guarded evidence RPCs refuse any
# payload carrying one outright.


@app.post("/runs/{run_id}/tool-access-requests", status_code=201)
def create_tool_access_request(run_id: UUID, request: ToolAccessRequestCreate, worker: WorkerIdentity = Depends(get_verified_worker), repo: Repository = Depends(get_repository)) -> dict:
    require_stage_enabled("MILO_ENABLE_EXECUTION_CONTROL", "tool access requests")
    lease = request.lease()
    row = repo.create_tool_access_request(run_id, request.content(), **lease)
    repo.append_run_event(run_id, "tool_access_requested", {"message": f"{request.agent} requested {request.tool}", "agent": request.agent, "payload": row}, **lease)
    return row

@app.post("/runs/{run_id}/tool-grants", status_code=201)
def create_tool_grant(run_id: UUID, request: ToolGrantCreate, worker: WorkerIdentity = Depends(get_verified_worker), repo: Repository = Depends(get_repository)) -> dict:
    require_stage_enabled("MILO_ENABLE_EXECUTION_CONTROL", "tool grants")
    lease = request.lease()
    row = repo.create_tool_grant(run_id, request.content(), **lease)
    repo.append_run_event(run_id, "tool_access_granted", {"message": f"{request.tool} granted to {request.agent}", "agent": request.agent, "payload": row}, **lease)
    return row

@app.post("/runs/{run_id}/tool-usage", status_code=201)
def create_tool_usage(run_id: UUID, request: ToolUsageCreate, worker: WorkerIdentity = Depends(get_verified_worker), repo: Repository = Depends(get_repository)) -> dict:
    require_stage_enabled("MILO_ENABLE_EXECUTION_CONTROL", "tool usage")
    lease = request.lease()
    row = repo.create_tool_usage(run_id, request.content(), **lease)
    repo.append_run_event(run_id, "tool_used", {"message": f"{request.agent} used {request.tool}", "agent": request.agent, "payload": row}, **lease)
    return row

@app.post("/runs/{run_id}/sources", status_code=201)
def create_source(run_id: UUID, request: SourceCreate, worker: WorkerIdentity = Depends(get_verified_worker), repo: Repository = Depends(get_repository)) -> dict:
    require_stage_enabled("MILO_ENABLE_EXECUTION_CONTROL", "source recording")
    lease = request.lease()
    row = repo.create_source(run_id, request.content(), **lease)
    repo.append_run_event(run_id, "source_recorded", {"message": request.title, "agent": request.agent, "payload": row}, **lease)
    return row

@app.post("/runs/{run_id}/claims", status_code=201)
def create_claim(run_id: UUID, request: ClaimCreate, worker: WorkerIdentity = Depends(get_verified_worker), repo: Repository = Depends(get_repository)) -> dict:
    require_stage_enabled("MILO_ENABLE_EXECUTION_CONTROL", "claim recording")
    lease = request.lease()
    row = repo.create_claim(run_id, request.content(), **lease)
    repo.append_run_event(run_id, "claim_recorded", {"message": f"{request.entity_key}.{request.field_key}", "agent": request.agent, "payload": row}, **lease)
    return row

@app.post("/runs/{run_id}/conflicts", status_code=201)
def create_conflict(run_id: UUID, request: ConflictCreate, worker: WorkerIdentity = Depends(get_verified_worker), repo: Repository = Depends(get_repository)) -> dict:
    require_stage_enabled("MILO_ENABLE_EXECUTION_CONTROL", "conflict recording")
    lease = request.lease()
    row = repo.create_conflict(run_id, request.content(), **lease)
    repo.append_run_event(run_id, "conflict_detected", {"message": f"{request.entity_key}.{request.field_key}", "payload": row}, **lease)
    return row


@app.post("/internal/runs/{run_id}/events", status_code=201)
def create_worker_run_event(run_id: UUID, request: WorkerRunEventCreate, worker: WorkerIdentity = Depends(get_verified_worker), repo: Repository = Depends(get_repository)) -> dict:
    require_stage_enabled("MILO_ENABLE_EXECUTION_CONTROL", "worker run events")
    # The CANONICAL vocabulary (`backend/event_registry.py`). This check used
    # to run against a set that named no Swarm V2 type at all, so the API
    # refused `task_started` -- an event the V2 engine emits on every task --
    # while the durable sink wrote it without checking anything.
    if not is_known_event_type(request.event_type):
        raise AppError("UNKNOWN_EVENT_TYPE", "unknown event type", 422)
    # The four terminal events are the canonical finalizer's alone: it writes
    # them in the same transaction as the terminal status, and the run read
    # projects the ProductOutcome from the latest of them. A worker request
    # body may not append one, so it cannot shadow the finalizer's record.
    if request.event_type in TERMINAL_EVENT_TYPES:
        raise AppError("TERMINAL_EVENT_RESERVED", "terminal events are written only by the canonical finalizer", 422)
    return repo.append_run_event(run_id, request.event_type, {"message": request.message, "agent": request.agent, "phase": request.phase, "progress": request.progress, "payload": {**request.payload, "worker_identity": worker.service_account_email}}, **request.lease())


@app.post("/internal/runs/{run_id}/complete")
def complete_run_from_worker(run_id: UUID, request: WorkerRunCompleteRequest, worker: WorkerIdentity = Depends(get_verified_worker), repo: Repository = Depends(get_repository)) -> dict:
    require_stage_enabled("MILO_ENABLE_EXECUTION_CONTROL", "worker run completion")
    run = repo.get_run(run_id)
    try:
        identity = require_identity(run)
    except RunIdentityError as exc:
        raise AppError("RUN_IDENTITY_REQUIRED", "worker completion requires immutable run identity", 409) from exc
    if identity.workflow_key not in PRODUCT_WORKFLOW_KEYS:
        raise AppError(
            "ENGINE_NOT_ALLOWED",
            "control-plane run identity cannot use the product worker completion surface",
            403,
        )
    if execution_identity_problems(identity):
        raise AppError(
            "RUN_IDENTITY_RUNTIME_MISMATCH",
            "worker completion runtime does not match the run identity",
            409,
        )
    finalizer = RunFinalizer(repo, run_id, identity.workflow_key, request.lease())
    result = finalizer.finalize(TerminalClaim.product(
        identity.workflow_key,
        request.output,
        event_payload={"worker_identity": worker.service_account_email},
    ))
    return repo.get_run(run_id) if result.status else run


@app.post("/internal/runs/{run_id}/fail")
def fail_run_from_worker(run_id: UUID, request: WorkerRunFailRequest, worker: WorkerIdentity = Depends(get_verified_worker), repo: Repository = Depends(get_repository)) -> dict:
    require_stage_enabled("MILO_ENABLE_EXECUTION_CONTROL", "worker run failure")
    run = repo.get_run(run_id)
    try:
        identity = require_identity(run)
    except RunIdentityError as exc:
        raise AppError("RUN_IDENTITY_REQUIRED", "worker failure requires immutable run identity", 409) from exc
    if identity.workflow_key not in PRODUCT_WORKFLOW_KEYS:
        raise AppError(
            "ENGINE_NOT_ALLOWED",
            "control-plane run identity cannot use the product worker failure surface",
            403,
        )
    if execution_identity_problems(identity):
        raise AppError(
            "RUN_IDENTITY_RUNTIME_MISMATCH",
            "worker failure runtime does not match the run identity",
            409,
        )
    finalizer = RunFinalizer(repo, run_id, identity.workflow_key, request.lease())
    result = finalizer.finalize(TerminalClaim.failure(
        identity.workflow_key,
        request.code,
        request.message,
        event_payload={"worker_identity": worker.service_account_email},
    ))
    return repo.get_run(run_id) if result.status else run

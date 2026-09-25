from datetime import datetime
from typing import Any, ClassVar
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_serializer

DEFAULT_CONVERSATION_TITLE = "New conversation"


def normalize_conversation_title(title: str | None) -> str:
    """Non-null conversation title for the NOT NULL conversations.title column.

    Missing/None/blank/whitespace-only titles become the safe default;
    explicit titles are preserved (surrounding whitespace stripped).
    """
    normalized = (title or "").strip()
    return normalized or DEFAULT_CONVERSATION_TITLE


class HealthResponse(BaseModel):
    status: str = "ok"


class Project(BaseModel):
    id: UUID
    slug: str
    name: str
    description: str | None = None
    workflow_key: str
    configuration: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    model_config = ConfigDict(from_attributes=True)


class ConversationCreate(BaseModel):
    # The request may omit the title entirely; the contract guarantees a
    # non-null value so inserts can never violate conversations.title NOT NULL.
    title: str = DEFAULT_CONVERSATION_TITLE

    @field_validator("title", mode="before")
    @classmethod
    def _non_null_title(cls, value: Any) -> str:
        if value is not None and not isinstance(value, str):
            raise ValueError("title must be a string")
        return normalize_conversation_title(value)


class Conversation(BaseModel):
    id: UUID
    project_id: UUID
    title: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class RunCreate(BaseModel):
    content: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=8, max_length=128)


class RunUsage(BaseModel):
    """Browser-safe aggregate usage for one run.

    This mirrors ``BudgetTracker.snapshot()`` (backend/budget.py) field for
    field, which is the ONLY shape ever written to ``runs.usage``: the worker
    persists it through ``update_run_usage`` after every settled provider call
    and through ``transition_run`` on a budget terminal.

    The contract is deliberately a closed set of non-negative numbers. Extra
    keys are ignored rather than forwarded, so nothing outside this allowlist
    can reach the browser through it -- no provider or model identity, prompt,
    model response, evidence text, reservation, lease, ledger row or error
    detail, none of which the snapshot carries in the first place.

    Every field is optional. A run that has not settled a call stores ``{}``
    (migration 010's ``NOT NULL DEFAULT``), and absent is never rewritten as
    zero: "nothing recorded yet" and "zero model calls" are different facts.
    """

    # `model_` is a pydantic protected namespace; `model_calls` is the durable
    # column name and is not renamed for the wire.
    model_config = ConfigDict(extra="ignore", protected_namespaces=())

    model_calls: int | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    estimated_cost: float | None = Field(default=None, ge=0)
    actual_cost: float | None = Field(default=None, ge=0)
    retries: int | None = Field(default=None, ge=0)
    provider_backpressure_events: int | None = Field(default=None, ge=0)
    # Backend-counted model-backed steps; NOT a count of UI agents.
    agent_steps: int | None = Field(default=None, ge=0)
    elapsed_seconds: float | None = Field(default=None, ge=0)
    # PR-R reasoning-aware breakdown (backend.model_usage). Counts only: no
    # reasoning text ever reaches this contract. `reasoning_tokens` sums what
    # the provider REPORTED; `reasoning_tokens_estimated` sums estimates for
    # calls whose usage did not report it (`reasoning_estimated_calls` of them).
    cached_input_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens_estimated: int | None = Field(default=None, ge=0)
    reasoning_estimated_calls: int | None = Field(default=None, ge=0)
    answer_tokens: int | None = Field(default=None, ge=0)


class RunIdentityRecord(BaseModel):
    """The run's immutable identity, as the browser is allowed to see it.

    A CLOSED shape, like every other browser-visible contract here: the model
    names exactly the dimensions `backend/run_identity.py` binds, so a column
    that somehow grew an extra key cannot reach the browser through it. None of
    it is secret -- an engine version, a policy digest and a release SHA are
    public facts about which code admitted the run -- and all of it is what
    lets the browser render a HISTORICAL run as the engine it actually was
    rather than as whatever its project is today.
    """

    model_config = ConfigDict(extra="ignore")

    identity_version: str
    run_id: UUID
    workflow_key: str
    engine_version: str
    policy_version: str
    policy_fingerprint: str
    release_sha: str = ""
    event_registry_version: str
    event_registry_fingerprint: str


class ProductOutcomeCoverage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    produced: int = Field(ge=0)
    outstanding: int = Field(ge=0)
    ratio: float | None = Field(default=None, ge=0, le=1)


class ProductOutcomeBlockingItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    code: str
    count: int = Field(ge=1)


class ProductOutcomePayloadReference(BaseModel):
    """Presence, digest, size and top-level shape -- never content."""

    model_config = ConfigDict(extra="ignore")

    present: bool
    digest: str | None = None
    byte_size: int | None = Field(default=None, ge=0)
    # The payload's top-level KEY NAMES (bounded, sorted) -- a shape, not content.
    shape: list[str] | None = None


class ProductOutcomeRecord(BaseModel):
    """The canonical ProductOutcome, as the browser is allowed to see it.

    This is ``backend/product_outcome.py``'s ``as_record`` form, re-validated
    on the way out: every field is a static vocabulary entry, a count or a
    digest. It is read from the terminal event the canonical finalizer wrote
    in the same transaction as the terminal status, never derived by the API
    from the payload and never supplied by a worker request body. A run whose
    terminal event carries no trustworthy record projects ``null``.
    """

    model_config = ConfigDict(extra="ignore")

    engine: str
    semantic_status: str
    usability: str
    result_kind: str | None = None
    coverage: ProductOutcomeCoverage
    blocking: list[ProductOutcomeBlockingItem] = Field(default_factory=list)
    payload: ProductOutcomePayloadReference


class EffectiveConcurrency(BaseModel):
    """The concurrency this deployment really runs at, from server-owned truth.

    Every number is DERIVED from the canonical `RuntimePolicy`
    (``backend/runtime_policy.py``), the resolved provider configuration and
    the organization ceiling -- never typed into the browser. Two widths are
    stated separately on purpose: the LOGICAL engine width (how many workers
    or technical units an engine may run at once) and the PROVIDER-ADMITTED
    width (how many provider requests the reviewed per-process profile and
    the organization ceiling actually let through). The smaller one binds,
    and ``*_provider_admitted`` is exactly that minimum, computed the way the
    executor computes it. An unresolvable policy states ``null``: nothing here
    is ever invented.
    """

    model_config = ConfigDict(extra="ignore", protected_namespaces=())

    #: vehicle_catalog_v1: technical units executed in parallel (logical).
    v1_technical_parallelism: int | None = None
    #: swarm_v2: logical active workers before provider capacity is applied.
    v2_max_active_workers: int | None = None
    #: The reviewed per-process provider concurrency (MILO_PROVIDER_MAX_CONCURRENCY).
    provider_max_concurrency: int | None = None
    #: The organization-wide inference ceiling every process shares.
    provider_organization_ceiling: int | None = None
    #: What the provider admits for this process: min(per-process, ceiling).
    provider_effective_concurrency: int | None = None
    #: min(engine width, provider effective) -- the width that binds in practice.
    v1_provider_admitted: int | None = None
    v2_provider_admitted: int | None = None
    #: Server-side admission caps applied before a run row exists.
    max_concurrent_runs_per_user: int | None = None
    max_concurrent_runs_per_project: int | None = None
    #: Web search admission, per endpoint, per second.
    search_basic_qps: int | None = None
    search_pro_qps: int | None = None
    #: Whether the search QPS values are provider-verified or the conservative fallback.
    search_qps_verified: bool = False
    #: Whether this deployment resolved the policy in the paid posture.
    paid_posture: bool = False


class RunLimits(BaseModel):
    """The per-run ceilings this deployment enforces, as plain numbers.

    They come from the API's own ``BudgetConfig`` (the canonical runtime
    policy applied to this deployment), so the browser can show spend AGAINST
    a limit instead of a bare number. Nothing here is a secret or an
    infrastructure detail: an unset ceiling is ``null``, never invented.
    """

    model_config = ConfigDict(extra="ignore", protected_namespaces=())

    max_model_calls_per_run: int | None = None
    max_total_tokens_per_run: int | None = None
    max_cost_per_run: float | None = None
    max_run_duration_seconds: int | None = None
    max_agent_steps: int | None = None
    #: The effective runtime concurrency, from server-owned truth. `null`
    #: when the canonical policy cannot be resolved for this deployment.
    concurrency: EffectiveConcurrency | None = None


class Run(BaseModel):
    id: UUID
    conversation_id: UUID
    status: str
    attempt: int = 1
    started_at: datetime | None = None
    finished_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    cancellation_requested_at: datetime | None = None
    cancellation_reason: str | None = None
    input: dict[str, Any] | None = None
    output: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    launch_state: str | None = None
    launch_error_class: str | None = None
    launch_reconciliation_required: bool = False
    # Authoritative aggregate usage for this run. `null` means nothing has been
    # recorded yet; it is never a synonym for zero spend.
    usage: RunUsage | None = None
    # `null` for a run created before identities existed. It is never
    # defaulted: an unpinned run is unpinned, and every consumer says so.
    run_identity: RunIdentityRecord | None = None
    # The canonical ProductOutcome the finalizer recorded with the terminal
    # status. `null` while the run is live, for a non-product terminal (a
    # cancellation, a timeout, a budget stop) and for a historical run that
    # predates canonical finalization. Never derived from `output` here.
    product_outcome: ProductOutcomeRecord | None = None
    # The ceilings the run executes under, so spend can be read against them.
    limits: RunLimits | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class RunSummary(BaseModel):
    """One row of a conversation's run history.

    The same browser-safe projection as `Run` MINUS the payloads: a history
    list is for choosing a run to reopen, and the product itself is read
    through `GET /runs/{id}` once one is chosen. Bounded by the endpoint.
    """

    id: UUID
    conversation_id: UUID
    status: str
    attempt: int = 1
    started_at: datetime | None = None
    finished_at: datetime | None = None
    launch_state: str | None = None
    launch_error_class: str | None = None
    launch_reconciliation_required: bool = False
    usage: RunUsage | None = None
    run_identity: RunIdentityRecord | None = None
    product_outcome: ProductOutcomeRecord | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class RunEvent(BaseModel):
    id: int  # production run_events.id is bigint; run_id (below) remains UUID
    run_id: UUID
    event_type: str
    message: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    agent: str | None = None
    phase: str | None = None
    progress: dict[str, Any] | None = None
    created_at: datetime | None = None


class RunCreated(BaseModel):
    run_id: UUID
    status: str


class RunCancelRequest(BaseModel):
    reason: str | None = None


class RunCancelResponse(BaseModel):
    run_id: UUID
    status: str


class RunCheckpoint(BaseModel):
    id: UUID
    run_id: UUID
    engine_version: str
    workflow_key: str
    phase: str
    completed_tasks: list[str] = Field(default_factory=list)
    artifacts: dict[str, Any] = Field(default_factory=dict)
    failures: list[dict[str, Any]] = Field(default_factory=list)
    token_usage: dict[str, int] = Field(default_factory=dict)
    last_event: dict[str, Any] | None = None
    attempt: int = 1
    created_at: datetime | None = None


class ProposalCreate(BaseModel):
    project_id: UUID
    user_request: str = Field(min_length=1)
    budget_preference: str | None = None
    force_missing_verifier: bool = False
    force_bad_internet: bool = False


class ProposalRevise(BaseModel):
    user_request: str = Field(min_length=1)
    budget_preference: str | None = None


class ProposalDecision(BaseModel):
    reason: str | None = None


class WorkflowProposal(BaseModel):
    id: UUID
    status: str
    user_request: str
    created_by: UUID | None = None
    project_id: UUID | None = None
    task_spec: dict[str, Any]
    draft: dict[str, Any]
    critiques: list[dict[str, Any]] = Field(default_factory=list)
    estimates: dict[str, Any]
    repair_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None
    approved_at: datetime | None = None
    rejected_at: datetime | None = None


class ProposalProjectCreate(BaseModel):
    slug: str
    name: str
    description: str | None = None


class ProposalRunCreate(BaseModel):
    conversation_id: UUID
    content: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=8, max_length=128)


class WorkerLeaseFence(BaseModel):
    """The `run + attempt + worker + lease` ownership contract, on the wire.

    Every worker-side durable mutation inside the process already carries this
    (``backend/worker/main.py`` builds it once as ``lease_ctx`` and hands it to
    every repository write). The HTTP surfaces did not: a request authenticated
    as a worker SERVICE IDENTITY was accepted without any statement about which
    run, attempt and lease it was acting under -- so a replaced worker whose
    credentials were still valid could append events, open tool access, and
    complete or fail a run it no longer owned.

    Identity answers "is this a worker?". This answers "is this THE worker of
    THIS attempt of THIS run?", and only the database can settle it: these
    three values are checked against the live lease, under the database clock,
    inside the guarded RPC. They are required, never defaulted.
    """

    worker_id: str = Field(min_length=1)
    attempt: int = Field(ge=1)
    lease_token: str = Field(min_length=1)

    #: The fence's own keys, so no caller has to remember them.
    FENCE_FIELDS: ClassVar[tuple[str, ...]] = ("worker_id", "attempt", "lease_token")

    def lease(self) -> dict[str, Any]:
        """The ownership contract, as the repository's keyword arguments."""
        return {name: getattr(self, name) for name in self.FENCE_FIELDS}

    def content(self) -> dict[str, Any]:
        """The request WITHOUT the fence, in JSON-native types.

        The fence authorizes the write; it is never part of what is written.
        `lease_token` in particular is a credential: the guarded evidence RPCs
        refuse outright any payload carrying a `lease_token` or `token` key, so
        folding the fence into the row would not merely be untidy, it would be
        rejected by the database.

        `mode="json"` is load-bearing, not tidiness. Every one of these
        payloads is now handed to a guarded RPC as ONE `jsonb` argument, and a
        Python-mode dump leaves `UUID` and `datetime` objects in it -- which
        the PostgREST client cannot serialize. A tool grant carries both
        (`request_id`, `expires_at`), so the write would fail on the first real
        request rather than on a test fake that never serializes anything.
        """
        return self.model_dump(mode="json", exclude=set(self.FENCE_FIELDS))


class ToolAccessRequestCreate(WorkerLeaseFence):
    agent: str
    tool: str
    reason: str
    scope: dict[str, Any] = Field(default_factory=dict)
    requested_limits: dict[str, Any] = Field(default_factory=dict)
    trigger: dict[str, Any] | None = None

class ToolGrantCreate(WorkerLeaseFence):
    request_id: UUID | None = None
    agent: str
    tool: str
    max_searches: int
    max_rounds: int
    domains: list[str] | None = None
    expires_at: datetime
    approver_policy: str

class ToolUsageCreate(WorkerLeaseFence):
    grant_id: UUID
    agent: str
    tool: str
    operation: str
    query: str | None = None
    url: str | None = None
    status: str = "succeeded"
    error: dict[str, Any] | None = None

class SourceCreate(WorkerLeaseFence):
    agent: str
    url: str
    title: str
    domain: str
    source_type: str
    source_strength: str
    source_date: str | None = None
    query: str
    tool_operation: str

class ClaimCreate(WorkerLeaseFence):
    entity_key: str
    field_key: str
    value: Any
    unit: str | None = None
    time_scope: dict[str, Any] = Field(default_factory=dict)
    geography: str | None = None
    market: str | None = None
    source_id: UUID
    source_strength: str
    confidence: float
    agent: str
    status: str = "active"

class ConflictCreate(WorkerLeaseFence):
    entity_key: str
    field_key: str
    claim_ids: list[UUID]
    outcome: str = "unresolved_needs_review"
    rationale: str | None = None


class WorkerRunEventCreate(WorkerLeaseFence):
    event_type: str = Field(min_length=1)
    message: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    agent: str | None = None
    phase: str | None = None
    progress: dict[str, Any] | None = None


class WorkerRunCompleteRequest(WorkerLeaseFence):
    output: dict[str, Any] = Field(default_factory=dict)


class WorkerRunFailRequest(WorkerLeaseFence):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# CODE-3: the bounded, read-only catalog review contracts.
# ---------------------------------------------------------------------------
#
# Explicit response models rather than `dict[str, Any]`, because these are the
# only catalog rows that reach a browser. `extra="forbid"` is the point of
# them: `backend/catalog/review.py` builds each item key by key from a closed
# allowlist, and this is the second, independent check that nothing else got in
# -- a stored column that somehow reached the projection fails validation here
# instead of being serialized.
#
# Every optional field means ABSENT, never zero and never empty text. A field
# the durable row does not state is `null`, which is a different answer from
# "it states nothing", and neither is ever rendered as a value.


class CatalogIdentityDimensions(BaseModel):
    """The closed R4 identity dimensions a catalog row may state.

    Named fields rather than a free map: a jsonb column reaching a browser as
    an open object is exactly the unprojected blob the CODE-3 contract forbids,
    and this makes the vocabulary a property of the response schema.
    Mirrors `CANDIDATE_IDENTITY_DIMENSIONS` in `backend/catalog/contracts.py`.

    A dimension the row does not state is an ABSENT KEY, not a null -- the same
    rule `stated_identity_dimensions` enforces on the way in, held to on the way
    out. There is no "unknown" value here, because an unstated dimension and a
    dimension stated as nothing are not the same thing and neither is a guess.
    """

    model_config = ConfigDict(extra="forbid")

    body_style: str | None = None
    drivetrain: str | None = None
    engine_code: str | None = None
    fuel_type: str | None = None
    generation: str | None = None
    market: str | None = None
    propulsion_technology: str | None = None
    transmission: str | None = None

    @model_serializer(mode="wrap")
    def _only_stated_dimensions(self, handler: Any) -> dict[str, Any]:
        return {name: value for name, value in handler(self).items() if value is not None}


class CanonicalCatalogItem(BaseModel):
    """One canonical variant as the review surface states it."""

    model_config = ConfigDict(extra="forbid")

    canonical_key: str
    model_canonical_key: str | None = None
    manufacturer: str | None = None
    commercial_model: str | None = None
    model_year_start: int | None = None
    model_year_end: int | None = None
    official_model_code: str | None = None
    trim: str | None = None
    identity_dimensions: CatalogIdentityDimensions = Field(
        default_factory=CatalogIdentityDimensions)
    promoted_at: str | None = None
    revised_at: str | None = None


class CatalogReviewCandidateItem(BaseModel):
    """One candidate awaiting review. NOT a canonical row, and never shown as one."""

    model_config = ConfigDict(extra="forbid")

    candidate_key: str
    status: str
    manufacturer: str | None = None
    commercial_model: str | None = None
    model_year_start: int | None = None
    model_year_end: int | None = None
    official_model_code: str | None = None
    trim: str | None = None
    identity_dimensions: CatalogIdentityDimensions = Field(
        default_factory=CatalogIdentityDimensions)


class CatalogReviewSnapshot(BaseModel):
    """What is being reviewed: the active Government snapshot's safe metadata."""

    model_config = ConfigDict(extra="forbid")

    snapshot_key: str
    resource_id: str | None = None
    package_id: str | None = None
    publisher: str | None = None
    dataset_title: str | None = None
    dataset_market_scope: str | None = None
    upstream_version: str | None = None
    upstream_version_kind: str | None = None
    activated_at: str | None = None
    declared_record_count: int | None = None
    stored_record_count: int | None = None
    normalization_contract: str | None = None
    normalization_issue_count: int | None = None


class CatalogPageMeta(BaseModel):
    """The page itself: what was asked for, and what the database said.

    `total` and `has_more` are `null` for "the database did not state one".
    Neither is ever inferred from the page length, and neither is ever reported
    as `0`/`false` to fill a gap -- an unknown total that rendered as zero would
    be the claim that the catalog is empty.
    """

    model_config = ConfigDict(extra="forbid")

    limit: int
    offset: int
    total: int | None = None
    has_more: bool | None = None


class CatalogCanonicalPage(BaseModel):
    """One bounded page of the current canonical catalog."""

    model_config = ConfigDict(extra="forbid")

    page: CatalogPageMeta
    items: list[CanonicalCatalogItem] = Field(default_factory=list)


class CatalogReviewPage(BaseModel):
    """One bounded page of `ready_for_review` candidates, or why there is none.

    `available` false is an HONEST unavailable state, not an empty catalog: the
    items are empty, the snapshot is null and `page.total` is null rather than
    zero, and `unavailable_reason` says which condition held.
    """

    model_config = ConfigDict(extra="forbid")

    available: bool
    unavailable_reason: str | None = None
    status: str
    snapshot: CatalogReviewSnapshot | None = None
    page: CatalogPageMeta
    items: list[CatalogReviewCandidateItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Mapping plans: the canonical WorkScope (`backend/catalog/scope/`).
# ---------------------------------------------------------------------------
#
# Requests carry EITHER an instruction OR an edit. The edit is deliberately an
# open object here: every rule about it -- its closed key set, its types, its
# ranges -- lives in ONE validator (`contract.scope_from_fields`), which the
# instruction path goes through too, so a click and a sentence are refused for
# the same reasons in the same words. Responses are closed shapes, a second
# check over the projection `backend/catalog/scope/service.py` builds key by key.


class WorkScopeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instruction: str | None = None
    edit: dict[str, Any] | None = None


class WorkScopeRevise(WorkScopeCreate):
    # The exact head this revision is made against. Both must still match.
    expected_revision: int = Field(ge=1, strict=True)
    expected_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class WorkScopeLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_units: int
    max_items: int
    default_max_items: int
    max_batch_size: int
    default_batch_size: int
    min_model_year: int
    max_model_year: int
    max_instruction_chars: int


class WorkScopeDirectRuns(BaseModel):
    """Whether the ordinary composer may create a run in this project, and why not."""

    model_config = ConfigDict(extra="forbid")

    allowed: bool
    blocked_by: str | None = None


class WorkScopeCapabilities(BaseModel):
    """Whether the Mapping Plan applies, and the server's own bounds for it."""

    model_config = ConfigDict(extra="forbid")

    available: bool
    reason: str | None = None
    contract: str
    directory_version: str
    limits: WorkScopeLimits
    can_prepare: bool
    can_start_batches: bool
    direct_runs: WorkScopeDirectRuns


class WorkScopeOrigin(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    label: str


class WorkScopeEntryCoverage(BaseModel):
    """`canonical_variants` is null unless `state` is `known`; never a zero."""

    model_config = ConfigDict(extra="forbid")

    state: str
    canonical_variants: int | None = None


class WorkScopeDirectoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    name: str
    name_he: str
    origin: str
    register_marque: str | None = None
    register_marque_verified: bool
    coverage: WorkScopeEntryCoverage


class WorkScopeCoverageSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available: bool
    catalog_variants: int | None = None
    attributed_variants: int | None = None


class WorkScopeDirectory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    directory_version: str
    origins: list[WorkScopeOrigin]
    entries: list[WorkScopeDirectoryEntry]
    coverage: WorkScopeCoverageSummary


class WorkScopeNote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    terms: list[str] = Field(default_factory=list)
    units: list[str] = Field(default_factory=list)


class WorkScopePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract: str
    directory_version: str
    units: list[str]
    model_year_from: int | None = None
    model_year_to: int | None = None
    max_items: int
    batch_size: int


class WorkScopeRevisionView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int
    digest: str
    input_kind: str
    instruction: str | None = None
    notes: list[WorkScopeNote] = Field(default_factory=list)
    created_at: str | None = None


class WorkScopeState(BaseModel):
    """One plan: its head (revision + digest), what it says, and its history."""

    model_config = ConfigDict(extra="forbid")

    work_scope_id: UUID
    conversation_id: UUID
    project_id: UUID
    status: str
    revision: int
    digest: str
    current: bool
    plan: WorkScopePlan
    head: WorkScopeRevisionView
    history: list[WorkScopeRevisionView] = Field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None


class WorkScopeOpen(BaseModel):
    model_config = ConfigDict(extra="forbid")

    work_scope: WorkScopeState | None = None


class WorkScopeMutationResult(BaseModel):
    """`applied` is false when the request was understood and changed nothing."""

    model_config = ConfigDict(extra="forbid")

    applied: bool
    notes: list[WorkScopeNote] = Field(default_factory=list)
    work_scope: WorkScopeState


# --- Mapping Plan batch runs (scoped catalog PR3) ----------------------------
#
# A start names the head it was made against (revision AND digest) and the batch
# it means. All three are preconditions the database re-checks under the plan's
# row lock; none of them chooses anything. Responses are closed shapes over the
# projection `backend/catalog/scope/batches.py` builds key by key.


class WorkScopeBatchStart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1, strict=True)
    expected_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    batch_id: UUID
    idempotency_key: str = Field(min_length=8, max_length=128)


class WorkScopeBatchRunCreated(RunCreated):
    """The run a start created -- or, for a replay or a double submission, the
    run that already executes that batch (`created: false`)."""

    model_config = ConfigDict(extra="forbid")

    work_scope_id: UUID
    batch_id: UUID
    attempt: int
    created: bool


class WorkScopeProgressUnit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unit_key: str
    name: str
    priority: int
    state: str
    reason_code: str | None = None
    progress: str
    readable_count: int
    ambiguous_count: int
    eligible_count: int
    queued_count: int
    batch_count: int
    settled_batches: int
    active: bool
    promoted: int
    refused: int
    unresolved: int


class WorkScopeProgressBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: UUID
    batch_number: int
    unit_key: str
    item_count: int
    state: str
    attempts: int
    run_id: UUID | None = None
    run_status: str | None = None
    promoted: int | None = None
    refused: int | None = None
    unresolved: int | None = None


class WorkScopeProgressLive(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: UUID
    batch_number: int
    revision: int
    unit_key: str
    item_count: int
    attempt: int
    run_id: UUID
    run_status: str
    launch_state: str | None = None


class WorkScopeProgressBatchTotals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int
    settled: int
    active: int
    interrupted: int
    remaining: int


class WorkScopeProgressItemTotals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int
    promoted: int
    refused: int
    unresolved: int
    completed: int
    remaining: int


class WorkScopeProgressPreparation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int
    prepared_at: str | None = None
    unit_count: int
    prepared_unit_count: int
    units: list[WorkScopeProgressUnit]
    next: WorkScopeProgressBatch | None = None
    recent: list[WorkScopeProgressBatch] = Field(default_factory=list)
    batches: WorkScopeProgressBatchTotals
    items: WorkScopeProgressItemTotals


class WorkScopeStartControl(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available: bool
    blocked_by: str | None = None
    batch: WorkScopeProgressBatch | None = None
    retry: bool
    relaunch: bool


class WorkScopeToggleControl(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available: bool


class WorkScopeCancelControl(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available: bool
    run_id: UUID | None = None


class WorkScopeControls(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: WorkScopeStartControl
    pause: WorkScopeToggleControl
    resume: WorkScopeToggleControl
    cancel: WorkScopeCancelControl


class WorkScopeProgress(BaseModel):
    """Where one plan stands, derived from durable state only."""

    model_config = ConfigDict(extra="forbid")

    work_scope_id: UUID
    revision: int
    digest: str
    closed: bool
    paused: bool
    status: str
    live: WorkScopeProgressLive | None = None
    preparation: WorkScopeProgressPreparation | None = None
    controls: WorkScopeControls


class WorkScopeControlResult(BaseModel):
    """`changed` is false when the plan was already in the requested state."""

    model_config = ConfigDict(extra="forbid")

    changed: bool
    paused: bool
    progress: WorkScopeProgress

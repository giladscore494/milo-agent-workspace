"""Declarative workflow contracts for the preserved MILO vehicle catalog pipeline.

The objects in this module describe the existing imperative implementation without
changing runtime behavior.  The compiler is intentionally deterministic and
side-effect free: it validates a blueprint and returns an ordered execution plan.
"""

from __future__ import annotations

from collections import defaultdict, deque
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import core


class InternetPolicy(StrEnum):
    REQUIRED = "required"
    DISABLED = "disabled"
    OPTIONAL = "optional"


class OutputSchemaDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    definition: dict[str, Any] = Field(alias="schema")
    required_keys: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("definition")
    @classmethod
    def schema_must_be_object(cls, value: dict[str, Any]) -> dict[str, Any]:
        if value.get("type") != "object":
            raise ValueError("output schema must be a JSON object schema")
        return value


class ToolPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed_tools: tuple[str, ...] = Field(default_factory=tuple)
    internet_policy: InternetPolicy
    max_tool_rounds: int = Field(ge=0, le=core.MAX_TOOL_ROUNDS)

    @model_validator(mode="after")
    def web_search_matches_internet_policy(self) -> "ToolPolicy":
        has_web_search = "$web_search" in self.allowed_tools
        if self.internet_policy == InternetPolicy.REQUIRED and not has_web_search:
            raise ValueError("required internet policy must allow $web_search")
        if self.internet_policy == InternetPolicy.DISABLED and has_web_search:
            raise ValueError("disabled internet policy cannot allow $web_search")
        return self


class SourcePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    require_source_attribution: bool = True
    no_unsupported_claims: bool = True
    explicit_missing_data: bool = True
    preferred_sources: tuple[str, ...] = Field(default_factory=tuple)


class ExecutionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_tokens: int = Field(gt=0)
    fallback_max_tokens: int | None = Field(default=None, gt=0)
    timeout_seconds: int = Field(gt=0)
    max_retries: int = Field(ge=0)
    concurrency_limit: int = Field(ge=1)
    chunk_size: int | None = Field(default=None, gt=0)
    temperature: float = Field(ge=0, le=2)


class ValidationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output_schema: OutputSchemaDefinition
    require_json: bool = True
    required_top_keys: tuple[str, ...] = Field(default_factory=tuple)
    non_empty_lists: tuple[str, ...] = Field(default_factory=tuple)
    validator: str | None = None

    @model_validator(mode="after")
    def required_keys_are_declared(self) -> "ValidationPolicy":
        missing = set(self.required_top_keys) - set(self.output_schema.required_keys)
        if missing:
            raise ValueError(f"required_top_keys not declared in schema: {sorted(missing)}")
        return self


class FailurePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    retryable_errors: tuple[str, ...] = Field(default_factory=tuple)
    fallback: Literal["none", "compact_prompt", "python_merge", "partial_result"] = "none"
    allow_partial_success: bool = False
    stop_on_failure: bool = False


class CompletionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    criteria: tuple[str, ...]
    checkpoint_after: bool = True

    @field_validator("criteria")
    @classmethod
    def criteria_required(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("completion criteria are required")
        return value


class TaskSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    name: str
    responsibility: str
    dependencies: tuple[str, ...] = Field(default_factory=tuple)
    completion: CompletionPolicy


class AgentSpec(TaskSpec):
    instructions: str
    input_schema: OutputSchemaDefinition
    output_schema: OutputSchemaDefinition
    tools: ToolPolicy
    source_policy: SourcePolicy
    execution: ExecutionPolicy
    validation: ValidationPolicy
    failure: FailurePolicy
    allow_role_overlap_with: tuple[str, ...] = Field(default_factory=tuple)

    @property
    def internet_policy(self) -> InternetPolicy:
        return self.tools.internet_policy


class QualityContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    structured_schemas: bool = True
    no_unsupported_claims: bool = True
    explicit_missing_data: bool = True
    source_attribution: bool = True
    validation: bool = True
    fallback: bool = True
    partial_success: bool = True
    checkpoints: bool = True
    verifier: bool = True
    deterministic_final_assembly_when_possible: bool = True
    explicit_internet_policy: bool = True

    @model_validator(mode="after")
    def cannot_be_bypassed(self) -> "QualityContract":
        disabled = [name for name, value in self.model_dump().items() if value is not True]
        if disabled:
            raise ValueError(f"mandatory quality contract flags cannot be disabled: {disabled}")
        return self


class WorkflowSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    name: str
    tasks: tuple[TaskSpec | AgentSpec, ...]
    quality_contract: QualityContract
    completion: CompletionPolicy


class ProjectBlueprint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    name: str
    workflows: tuple[WorkflowSpec, ...]
    quality_contract: QualityContract


class CompiledWorkflow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_key: str
    ordered_task_keys: tuple[str, ...]
    agents: tuple[str, ...]


KNOWN_TOOLS = frozenset({"$web_search"})
MAX_BUDGET_TOKENS = 100_000


def _json_object_schema(name: str, required: tuple[str, ...]) -> OutputSchemaDefinition:
    return OutputSchemaDefinition(name=name, schema={"type": "object", "additionalProperties": True}, required_keys=required)


GENERIC_INPUT_SCHEMA = _json_object_schema("generic_input", ())
DISCOVERY_OUTPUT_SCHEMA = _json_object_schema("discovery_output", ("agent", "models"))
ITEMS_OUTPUT_SCHEMA = _json_object_schema("items_output", ("agent", "items", "missing_data", "extra_candidate_models"))
NORMALIZER_OUTPUT_SCHEMA = _json_object_schema("normalizer_output", ("agent", "canonical_models", "rejected_items", "needs_review"))
VERIFIER_OUTPUT_SCHEMA = _json_object_schema("verifier_output", ("agent", "verified_models", "rejected_data_points", "needs_review"))
FINAL_OUTPUT_SCHEMA = _json_object_schema("final_output", ("manufacturer", "market", "period", "status", "models", "pipeline_quality"))
SUMMARY_OUTPUT_SCHEMA = _json_object_schema("summary_output", ("summary",))
PYTHON_OUTPUT_SCHEMA = _json_object_schema("python_output", ("status",))


def agent_template(
    *,
    key: str,
    name: str,
    responsibility: str,
    instructions: str,
    output_schema: OutputSchemaDefinition,
    internet_policy: InternetPolicy,
    max_tokens: int,
    dependencies: tuple[str, ...] = (),
    chunk_size: int | None = None,
    fallback_max_tokens: int | None = None,
    allow_partial: bool = False,
    stop_on_failure: bool = False,
    validator: str | None = None,
    required_top_keys: tuple[str, ...] = (),
    non_empty_lists: tuple[str, ...] = (),
) -> AgentSpec:
    return AgentSpec(
        key=key,
        name=name,
        responsibility=responsibility,
        dependencies=dependencies,
        completion=CompletionPolicy(criteria=("validated output emitted",)),
        instructions=instructions,
        input_schema=GENERIC_INPUT_SCHEMA,
        output_schema=output_schema,
        tools=ToolPolicy(allowed_tools=("$web_search",) if internet_policy == InternetPolicy.REQUIRED else (), internet_policy=internet_policy, max_tool_rounds=core.MAX_TOOL_ROUNDS if internet_policy == InternetPolicy.REQUIRED else 0),
        source_policy=SourcePolicy(preferred_sources=("official local/importer", "local automotive portals", "used-market listings")),
        execution=ExecutionPolicy(max_tokens=max_tokens, fallback_max_tokens=fallback_max_tokens, timeout_seconds=180, max_retries=core.API_CONCURRENCY_MAX_RETRIES, concurrency_limit=core.MAX_PARALLEL_KIMI_CALLS, chunk_size=chunk_size, temperature=core.SEARCH_TEMPERATURE),
        validation=ValidationPolicy(output_schema=output_schema, required_top_keys=required_top_keys or output_schema.required_keys, non_empty_lists=non_empty_lists, validator=validator),
        failure=FailurePolicy(retryable_errors=tuple(core.RETRYABLE_ERRORS), fallback="compact_prompt" if fallback_max_tokens else "partial_result" if allow_partial else "none", allow_partial_success=allow_partial, stop_on_failure=stop_on_failure),
    )


def python_task(key: str, name: str, responsibility: str, dependencies: tuple[str, ...], output_schema: OutputSchemaDefinition) -> TaskSpec:
    return TaskSpec(key=key, name=name, responsibility=responsibility, dependencies=dependencies, completion=CompletionPolicy(criteria=(f"{output_schema.name} produced deterministically",)))


TEMPLATES: dict[str, AgentSpec] = {
    "web_discovery": agent_template(key="web_discovery", name="Web discovery", responsibility="Find a narrow set of candidate entities from sourced web results.", instructions="Search immediately; return only sourced JSON candidates; never answer from memory.", output_schema=DISCOVERY_OUTPUT_SCHEMA, internet_policy=InternetPolicy.REQUIRED, max_tokens=core.MAX_DISCOVERY_TOKENS, fallback_max_tokens=core.MAX_DISCOVERY_FALLBACK_TOKENS, validator="validate_discovery_schema", non_empty_lists=("models",)),
    "normalizer_deduper": agent_template(key="normalizer_deduper", name="Normalizer/deduper", responsibility="Canonicalize, dedupe, and reject unsupported candidates.", instructions="Normalize only supplied candidates; do not browse or invent facts.", output_schema=NORMALIZER_OUTPUT_SCHEMA, internet_policy=InternetPolicy.DISABLED, max_tokens=2500, stop_on_failure=True, validator="validate_normalizer_schema", non_empty_lists=("canonical_models",)),
    "evidence_enrichment": agent_template(key="evidence_enrichment", name="Evidence enrichment", responsibility="Enrich assigned fields with attributed evidence only.", instructions="Search for assigned fields only and preserve missing data explicitly.", output_schema=ITEMS_OUTPUT_SCHEMA, internet_policy=InternetPolicy.REQUIRED, max_tokens=core.MAX_TECHNICAL_AGENT_TOKENS, chunk_size=core.TECHNICAL_MODEL_CHUNK_SIZE, allow_partial=True, validator="validate_items_schema"),
    "claim_verifier": agent_template(key="source_verifier", name="Claim verifier", responsibility="Verify relevance, source strength, and conflicts for assembled claims.", instructions="Verify compact structured data; do not perform broad new research or invent missing data.", output_schema=VERIFIER_OUTPUT_SCHEMA, internet_policy=InternetPolicy.DISABLED, max_tokens=core.MAX_VERIFIER_TOKENS, chunk_size=core.VERIFIER_MODEL_CHUNK_SIZE, allow_partial=True, validator="validate_verifier_schema"),
    "conflict_resolver": agent_template(key="conflict_resolver", name="Conflict resolver", responsibility="Resolve explicitly supplied conflicts according to source policy.", instructions="Prefer local official sources, then local portals, then used-market evidence; otherwise mark needs_review.", output_schema=ITEMS_OUTPUT_SCHEMA, internet_policy=InternetPolicy.DISABLED, max_tokens=2000, allow_partial=True),
    "targeted_gap_filler": agent_template(key="targeted_gap_filler", name="Targeted gap filler", responsibility="Fill explicit gaps with targeted searches only.", instructions="Search only named gaps; return missing_data when evidence is unavailable.", output_schema=ITEMS_OUTPUT_SCHEMA, internet_policy=InternetPolicy.REQUIRED, max_tokens=2000, allow_partial=True),
    "deterministic_builder": agent_template(key="final_builder", name="Deterministic builder", responsibility="Assemble final JSON deterministically from verified artifacts.", instructions="Use deterministic Python assembly when possible; do not create unsupported claims.", output_schema=FINAL_OUTPUT_SCHEMA, internet_policy=InternetPolicy.DISABLED, max_tokens=core.MAX_FINAL_BUILDER_TOKENS, stop_on_failure=True),
    "user_facing_summary": agent_template(key="hebrew_summary", name="User-facing summary", responsibility="Summarize final JSON for the end user in Hebrew.", instructions="Summarize only the final JSON; do not browse or add new facts.", output_schema=SUMMARY_OUTPUT_SCHEMA, internet_policy=InternetPolicy.DISABLED, max_tokens=core.MAX_SUMMARY_TOKENS),
}


def build_milo_blueprint() -> ProjectBlueprint:
    discovery = tuple(
        agent_template(key=a.key, name=a.name, responsibility=a.responsibility, instructions=a.description, output_schema=DISCOVERY_OUTPUT_SCHEMA, internet_policy=InternetPolicy.REQUIRED, max_tokens=core.MAX_DISCOVERY_TOKENS, fallback_max_tokens=core.MAX_DISCOVERY_FALLBACK_TOKENS, validator="validate_discovery_schema", non_empty_lists=("models",))
        for a in core.DISCOVERY_AGENTS
    )
    normalizer = TEMPLATES["normalizer_deduper"].model_copy(update={"dependencies": ("python_discovery_merge",)})
    technical = tuple(
        agent_template(key=a.key, name=a.name, responsibility=a.responsibility, instructions=a.description, output_schema=ITEMS_OUTPUT_SCHEMA, internet_policy=InternetPolicy.REQUIRED, max_tokens=core.technical_max_tokens(a.key), fallback_max_tokens=core.TECHNICAL_FALLBACK_TOKENS[a.key], dependencies=("normalizer_deduper",), chunk_size=core.TECHNICAL_MODEL_CHUNK_SIZE, allow_partial=True, validator="validate_items_schema")
        for a in core.TECHNICAL_AGENTS
    )
    verifier = TEMPLATES["claim_verifier"].model_copy(update={"dependencies": tuple(a.key for a in core.TECHNICAL_AGENTS)})
    summary = TEMPLATES["user_facing_summary"].model_copy(update={"dependencies": ("final_builder",)})
    tasks: tuple[TaskSpec | AgentSpec, ...] = (
        *discovery,
        python_task("python_discovery_merge", "Python discovery merge", "Deterministically merge discovery candidates.", tuple(a.key for a in core.DISCOVERY_AGENTS), PYTHON_OUTPUT_SCHEMA),
        normalizer,
        *technical,
        verifier,
        python_task("final_builder", "Final Python builder", "Deterministically assemble final catalog JSON.", ("source_verifier",), FINAL_OUTPUT_SCHEMA),
        summary,
    )
    qc = QualityContract()
    return ProjectBlueprint(key="milo", name="MILO vehicle catalog", quality_contract=qc, workflows=(WorkflowSpec(key="vehicle_catalog_v1", name="Vehicle catalog v1", tasks=tasks, quality_contract=qc, completion=CompletionPolicy(criteria=("final_builder success or explicit failure", "Hebrew summary attempted after final builder"))),))


def compile_workflow(workflow: WorkflowSpec) -> CompiledWorkflow:
    keys = [task.key for task in workflow.tasks]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate task keys are not allowed")
    task_by_key = {task.key: task for task in workflow.tasks}
    unknown_deps = sorted({dep for task in workflow.tasks for dep in task.dependencies if dep not in task_by_key})
    if unknown_deps:
        raise ValueError(f"unknown dependencies: {unknown_deps}")
    agents = [task for task in workflow.tasks if isinstance(task, AgentSpec)]
    if not any(task.key == "source_verifier" for task in workflow.tasks):
        raise ValueError("workflow must include source_verifier")
    if not any(task.key == "final_builder" for task in workflow.tasks):
        raise ValueError("workflow must include final_builder")
    for agent in agents:
        unknown = set(agent.tools.allowed_tools) - KNOWN_TOOLS
        if unknown:
            raise ValueError(f"unknown tools for {agent.key}: {sorted(unknown)}")
        if agent.execution.max_tokens > MAX_BUDGET_TOKENS or (agent.execution.fallback_max_tokens or 0) > MAX_BUDGET_TOKENS:
            raise ValueError(f"unbounded budget for {agent.key}")
    by_resp: dict[str, list[str]] = defaultdict(list)
    for agent in agents:
        by_resp[agent.responsibility.strip().lower()].append(agent.key)
    for _, overlapping in by_resp.items():
        if len(overlapping) > 1:
            allowed = all(set(overlapping) - {agent.key} <= set(agent.allow_role_overlap_with) for agent in agents if agent.key in overlapping)
            if not allowed:
                raise ValueError(f"unsafe overlapping roles: {overlapping}")
    indegree = {key: 0 for key in keys}
    children: dict[str, list[str]] = defaultdict(list)
    for task in workflow.tasks:
        for dep in task.dependencies:
            indegree[task.key] += 1
            children[dep].append(task.key)
    queue = deque([key for key in keys if indegree[key] == 0])
    ordered: list[str] = []
    while queue:
        key = queue.popleft()
        ordered.append(key)
        for child in children[key]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if len(ordered) != len(keys):
        raise ValueError("circular dependencies are not allowed")
    return CompiledWorkflow(workflow_key=workflow.key, ordered_task_keys=tuple(ordered), agents=tuple(a.key for a in agents))


def compile_project(blueprint: ProjectBlueprint) -> tuple[CompiledWorkflow, ...]:
    return tuple(compile_workflow(workflow) for workflow in blueprint.workflows)

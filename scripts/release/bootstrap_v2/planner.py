"""Pure planner: evidence in, desired state and frozen mutation plan out.

The planner performs no I/O, reads no environment, and calls no provider.
It consumes the typed discovered world produced by global discovery and
emits an immutable :class:`MutationPlan` plus its canonical JSON and full
lowercase SHA-256 digest (``MILO_PLAN_DIGEST``).

Secrets never enter the plan: secret-bearing operations carry SHA-256
fingerprints as intended post-state digests, never values.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .model import (
    CloudRunEnvVar,
    CloudRunResourceState,
    DECISIVE_OUTCOMES,
    IamPolicyState,
    Mode,
    MutationOperation,
    MutationPlan,
    OperationType,
    PostWriteRead,
    ProbeOutcome,
    Provider,
    RedisIdentity,
    ResourceIdentity,
    SecretState,
    UpstashDatabaseState,
    VercelEnvVarState,
    VercelProjectState,
    WifState,
)
from .policy import (
    API_SECRET_REFS,
    BootstrapConfig,
    DEPRECATED_METADATA_KEYS,
    EXECUTION_FLAG_REQUIRED_VALUE,
    EXECUTION_FLAGS,
    NUMERIC_PIN_REQUIRED_SECRETS,
    SECRET_MANAGER_RESOURCE_NAMES,
    VERCEL_MANAGED_VARS,
    WORKER_SECRET_REFS,
)

ABSENT_DIGEST = "absent"


class PlannerEvidenceError(Exception):
    """Raised when planning is attempted from non-decisive evidence."""


@dataclass(frozen=True, slots=True)
class Observed:
    """One discovery observation: a classified outcome plus typed state."""

    outcome: ProbeOutcome
    state: object | None = None

    def require_decisive(self, what: str) -> None:
        if self.outcome not in DECISIVE_OUTCOMES:
            raise PlannerEvidenceError(
                f"cannot plan from non-decisive evidence for {what}: "
                f"{self.outcome.value}"
            )


@dataclass(frozen=True, slots=True)
class DiscoveredWorld:
    """Complete global read-only discovery snapshot."""

    upstash_database: Observed
    service_accounts: tuple[tuple[str, Observed], ...]
    secrets: tuple[tuple[str, Observed], ...]
    secret_iam: tuple[tuple[str, Observed], ...]
    wif: Observed
    run_invoker_iam: Observed
    worker_job: Observed
    api_service: Observed
    vercel_project: Observed
    vercel_env: tuple[tuple[str, Observed], ...]
    redis_identity: RedisIdentity | None = None
    redis_secret_version_addition_required: bool = False


def canonical_json(payload: object) -> str:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )


def state_digest(payload: object | None) -> str:
    if payload is None:
        return ABSENT_DIGEST
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _operation_payload(op: MutationOperation) -> dict[str, object]:
    return {
        "sequence": op.sequence,
        "provider": op.provider.value,
        "operation_type": op.operation_type.value,
        "resource": {
            "provider": op.resource.provider.value,
            "kind": op.resource.kind,
            "name": op.resource.name,
            "scope": op.resource.scope,
        },
        "expected_pre_state_digest": op.expected_pre_state_digest,
        "intended_post_state_digest": op.intended_post_state_digest,
        "reason": op.reason,
        "idempotency_key": op.idempotency_key,
        "can_incur_cost": op.can_incur_cost,
        "has_safe_compensation": op.has_safe_compensation,
        "post_write_read": {
            "description": op.post_write_read.description,
            "expected_post_state_digest": op.post_write_read.expected_post_state_digest,
        },
    }


def plan_to_canonical_json(plan: MutationPlan) -> str:
    return canonical_json(
        {
            "schema": "milo-bootstrap-v2-plan",
            "mode": plan.mode.value,
            "bootstrap_sha": plan.bootstrap_sha,
            "operations": [_operation_payload(op) for op in plan.operations],
        }
    )


def plan_digest(plan: MutationPlan) -> str:
    """MILO_PLAN_DIGEST: full lowercase SHA-256 of the canonical plan JSON."""
    return hashlib.sha256(plan_to_canonical_json(plan).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Desired-state derivation (pure)
# ---------------------------------------------------------------------------


def _upstash_state_payload(state: UpstashDatabaseState) -> dict[str, object]:
    return {
        "database_id": state.database_id,
        "name": state.name,
        "state": state.state,
        "tls": state.tls,
        "region": state.region,
        "endpoint": state.endpoint,
        "rest_url": state.rest_url,
    }


def _env_payload(env: tuple[CloudRunEnvVar, ...]) -> list[dict[str, str]]:
    return sorted(
        (
            {
                "name": var.name,
                "value": var.value,
                "secret_name": var.secret_name,
                "secret_version": var.secret_version,
            }
            for var in env
        ),
        key=lambda item: item["name"],
    )


def _cloud_run_payload(
    service_account: str, env: tuple[CloudRunEnvVar, ...]
) -> dict[str, object]:
    return {"service_account": service_account, "env": _env_payload(env)}


def desired_cloud_run_env(
    observed: CloudRunResourceState,
    config: BootstrapConfig,
    redis: RedisIdentity,
    secret_refs: dict[str, str],
) -> tuple[CloudRunEnvVar, ...]:
    """Derive the intended env set for one Cloud Run resource.

    Only the application container's env feeds this function; container
    selection happens in ``validators.cloud_run`` before planning. Observed
    variables that the bootstrap does not manage are preserved verbatim.
    Deprecated keys are removed. No last-write-wins: duplicate observed
    names must already have been rejected by validators.
    """

    managed: dict[str, CloudRunEnvVar] = {}
    for flag in EXECUTION_FLAGS:
        managed[flag] = CloudRunEnvVar(name=flag, value=EXECUTION_FLAG_REQUIRED_VALUE)
    managed["GATEWAY_ALLOW_EXECUTION_ROUTES"] = CloudRunEnvVar(
        name="GATEWAY_ALLOW_EXECUTION_ROUTES", value=EXECUTION_FLAG_REQUIRED_VALUE
    )
    managed["ENVIRONMENT"] = CloudRunEnvVar(name="ENVIRONMENT", value="production")
    managed["MILO_BOOTSTRAP_SHA"] = CloudRunEnvVar(
        name="MILO_BOOTSTRAP_SHA", value=config.bootstrap_sha
    )
    managed["UPSTASH_REDIS_REST_URL"] = CloudRunEnvVar(
        name="UPSTASH_REDIS_REST_URL", value=redis.rest_url
    )
    managed["MILO_REDIS_DB_ID"] = CloudRunEnvVar(
        name="MILO_REDIS_DB_ID", value=redis.database_id
    )
    managed["MILO_REDIS_TOKEN_FINGERPRINT"] = CloudRunEnvVar(
        name="MILO_REDIS_TOKEN_FINGERPRINT", value=redis.token_fingerprint_sha256
    )
    managed["MILO_REDIS_SECRET_VERSION"] = CloudRunEnvVar(
        name="MILO_REDIS_SECRET_VERSION", value=redis.enabled_secret_version
    )
    for env_name, secret_name in secret_refs.items():
        if secret_name in NUMERIC_PIN_REQUIRED_SECRETS:
            managed[env_name] = CloudRunEnvVar(
                name=env_name,
                secret_name=secret_name,
                secret_version=redis.enabled_secret_version,
            )
        else:
            observed_var = next(
                (
                    var
                    for container in observed.containers
                    for var in container.env
                    if var.name == env_name and var.is_secret_ref()
                ),
                None,
            )
            observed_version = (
                observed_var.secret_version
                if observed_var is not None and observed_var.secret_version
                else "1"
            )
            managed[env_name] = CloudRunEnvVar(
                name=env_name,
                secret_name=secret_name,
                secret_version=observed_version,
            )

    preserved: dict[str, CloudRunEnvVar] = {}
    for container in observed.containers:
        for var in container.env:
            if var.name in DEPRECATED_METADATA_KEYS:
                continue
            if var.name in managed:
                continue
            preserved[var.name] = var

    combined = {**preserved, **managed}
    return tuple(sorted(combined.values(), key=lambda var: var.name))


# ---------------------------------------------------------------------------
# Plan construction (pure)
# ---------------------------------------------------------------------------


def _next_sequence(counter: list[int]) -> int:
    counter[0] += 1
    return counter[0]


def build_plan(config: BootstrapConfig, world: DiscoveredWorld) -> MutationPlan:
    """Generate the frozen mutation plan from decisive discovery evidence.

    Ordering follows the mandatory stage order: Upstash, GCP identities and
    secrets (worker prerequisites before API), IAM, Cloud Run (worker job
    before API service), Vercel.
    """

    operations: list[MutationOperation] = []
    seq = [0]

    # ---- Stage A: Upstash --------------------------------------------------
    world.upstash_database.require_decisive("upstash database")
    if world.upstash_database.outcome is ProbeOutcome.CLEANLY_ABSENT:
        resource = ResourceIdentity(
            provider=Provider.UPSTASH,
            kind="redis_database",
            name=config.upstash_database_name,
            scope="upstash",
        )
        desired = {
            "name": config.upstash_database_name,
            "tls": True,
            "region": config.gcp_region,
            "state": "active",
        }
        operations.append(
            MutationOperation(
                sequence=_next_sequence(seq),
                provider=Provider.UPSTASH,
                operation_type=OperationType.UPSTASH_CREATE_DATABASE,
                resource=resource,
                expected_pre_state_digest=ABSENT_DIGEST,
                intended_post_state_digest=state_digest(desired),
                reason="production Redis database proven cleanly absent",
                idempotency_key=f"{OperationType.UPSTASH_CREATE_DATABASE.value}:{resource.key()}",
                can_incur_cost=True,
                has_safe_compensation=False,
                post_write_read=PostWriteRead(
                    description="reread created database detail by returned id",
                    expected_post_state_digest=state_digest(desired),
                ),
            )
        )

    # ---- Stage B: GCP identities and secrets -------------------------------
    # Worker identity prerequisites complete before API configuration; plan
    # creates in worker, gateway, api order for identity resources.
    sa_order = (
        config.worker_service_account,
        config.gateway_service_account,
        config.api_service_account,
    )
    observed_sas = dict(world.service_accounts)
    for email in sa_order:
        observed = observed_sas.get(email)
        if observed is None:
            raise PlannerEvidenceError(f"missing discovery evidence for {email}")
        observed.require_decisive(f"service account {email}")
        if observed.outcome is ProbeOutcome.CLEANLY_ABSENT:
            resource = ResourceIdentity(
                provider=Provider.GCP,
                kind="service_account",
                name=email,
                scope=config.gcp_project_id,
            )
            desired = {"email": email, "disabled": False}
            operations.append(
                MutationOperation(
                    sequence=_next_sequence(seq),
                    provider=Provider.GCP,
                    operation_type=OperationType.GCP_CREATE_SERVICE_ACCOUNT,
                    resource=resource,
                    expected_pre_state_digest=ABSENT_DIGEST,
                    intended_post_state_digest=state_digest(desired),
                    reason="required identity proven cleanly absent",
                    idempotency_key=f"{OperationType.GCP_CREATE_SERVICE_ACCOUNT.value}:{resource.key()}",
                    can_incur_cost=False,
                    has_safe_compensation=False,
                    post_write_read=PostWriteRead(
                        description="reread service account by exact email",
                        expected_post_state_digest=state_digest(desired),
                    ),
                )
            )

    observed_secrets = dict(world.secrets)
    for secret_name in SECRET_MANAGER_RESOURCE_NAMES:
        observed = observed_secrets.get(secret_name)
        if observed is None:
            raise PlannerEvidenceError(
                f"missing discovery evidence for secret {secret_name}"
            )
        observed.require_decisive(f"secret {secret_name}")
        if observed.outcome is ProbeOutcome.CLEANLY_ABSENT:
            resource = ResourceIdentity(
                provider=Provider.GCP,
                kind="secret",
                name=secret_name,
                scope=config.gcp_project_id,
            )
            desired = {"name": secret_name, "exists": True}
            operations.append(
                MutationOperation(
                    sequence=_next_sequence(seq),
                    provider=Provider.GCP,
                    operation_type=OperationType.GCP_CREATE_SECRET,
                    resource=resource,
                    expected_pre_state_digest=ABSENT_DIGEST,
                    intended_post_state_digest=state_digest(desired),
                    reason="explicitly named secret proven cleanly absent",
                    idempotency_key=f"{OperationType.GCP_CREATE_SECRET.value}:{resource.key()}",
                    can_incur_cost=False,
                    has_safe_compensation=False,
                    post_write_read=PostWriteRead(
                        description="reread secret by exact resource name",
                        expected_post_state_digest=state_digest(desired),
                    ),
                )
            )

    if world.redis_secret_version_addition_required:
        if world.redis_identity is None:
            raise PlannerEvidenceError(
                "redis version addition requires a coherent redis identity"
            )
        resource = ResourceIdentity(
            provider=Provider.GCP,
            kind="secret_version",
            name="UPSTASH_REDIS_REST_TOKEN",
            scope=config.gcp_project_id,
        )
        operations.append(
            MutationOperation(
                sequence=_next_sequence(seq),
                provider=Provider.GCP,
                operation_type=OperationType.GCP_ADD_SECRET_VERSION,
                resource=resource,
                expected_pre_state_digest=state_digest(
                    {"fingerprint": "mismatch-requires-new-version"}
                ),
                intended_post_state_digest=world.redis_identity.token_fingerprint_sha256,
                reason="exact fingerprint reconciliation requires a new Redis token version",
                idempotency_key=f"{OperationType.GCP_ADD_SECRET_VERSION.value}:{resource.key()}",
                can_incur_cost=False,
                has_safe_compensation=False,
                post_write_read=PostWriteRead(
                    description="reread enabled version and payload fingerprint",
                    expected_post_state_digest=world.redis_identity.token_fingerprint_sha256,
                ),
            )
        )

    # ---- Stage C: IAM -------------------------------------------------------
    world.wif.require_decisive("workload identity federation")
    for secret_name, observed in world.secret_iam:
        observed.require_decisive(f"iam policy of secret {secret_name}")
        state = observed.state
        desired_members = _intended_secret_accessors(secret_name, config)
        current_members: tuple[str, ...] = ()
        if isinstance(state, IamPolicyState):
            for binding in state.bindings:
                if (
                    binding.role == "roles/secretmanager.secretAccessor"
                    and not binding.condition_expression
                ):
                    current_members = binding.members
        if tuple(sorted(current_members)) != tuple(sorted(desired_members)):
            resource = ResourceIdentity(
                provider=Provider.GCP,
                kind="secret_iam_policy",
                name=secret_name,
                scope=config.gcp_project_id,
            )
            desired = {
                "role": "roles/secretmanager.secretAccessor",
                "members": sorted(desired_members),
                "condition": "",
            }
            operations.append(
                MutationOperation(
                    sequence=_next_sequence(seq),
                    provider=Provider.GCP,
                    operation_type=OperationType.GCP_SET_SECRET_IAM,
                    resource=resource,
                    expected_pre_state_digest=state_digest(
                        {"members": sorted(current_members)}
                    ),
                    intended_post_state_digest=state_digest(desired),
                    reason="per-secret accessor set differs from intended consumers",
                    idempotency_key=f"{OperationType.GCP_SET_SECRET_IAM.value}:{resource.key()}",
                    can_incur_cost=False,
                    has_safe_compensation=True,
                    post_write_read=PostWriteRead(
                        description="reread secret iam policy and compare exactly",
                        expected_post_state_digest=state_digest(desired),
                    ),
                )
            )

    world.run_invoker_iam.require_decisive("cloud run invoker policy")
    invoker_state = world.run_invoker_iam.state
    desired_invokers = (f"serviceAccount:{config.gateway_service_account}",)
    current_invokers: tuple[str, ...] = ()
    if isinstance(invoker_state, IamPolicyState):
        for binding in invoker_state.bindings:
            if binding.role == "roles/run.invoker" and not binding.condition_expression:
                current_invokers = binding.members
    if tuple(sorted(current_invokers)) != tuple(sorted(desired_invokers)):
        resource = ResourceIdentity(
            provider=Provider.GCP,
            kind="run_invoker_policy",
            name=config.cloud_run_api_service,
            scope=config.gcp_project_id,
        )
        desired = {
            "role": "roles/run.invoker",
            "members": sorted(desired_invokers),
            "condition": "",
        }
        operations.append(
            MutationOperation(
                sequence=_next_sequence(seq),
                provider=Provider.GCP,
                operation_type=OperationType.GCP_SET_RUN_INVOKER_IAM,
                resource=resource,
                expected_pre_state_digest=state_digest(
                    {"members": sorted(current_invokers)}
                ),
                intended_post_state_digest=state_digest(desired),
                reason="api run.invoker must be exactly the gateway service account",
                idempotency_key=f"{OperationType.GCP_SET_RUN_INVOKER_IAM.value}:{resource.key()}",
                can_incur_cost=False,
                has_safe_compensation=True,
                post_write_read=PostWriteRead(
                    description="reread run.invoker policy and compare exactly",
                    expected_post_state_digest=state_digest(desired),
                ),
            )
        )

    # ---- Stage D: Cloud Run (worker job first, then API service) -----------
    if world.redis_identity is not None:
        for kind, observed, secret_refs, name in (
            ("worker_job", world.worker_job, WORKER_SECRET_REFS, config.cloud_run_worker_job),
            ("api_service", world.api_service, API_SECRET_REFS, config.cloud_run_api_service),
        ):
            observed.require_decisive(f"cloud run {kind}")
            state = observed.state
            if not isinstance(state, CloudRunResourceState):
                raise PlannerEvidenceError(f"cloud run {kind} state missing")
            desired_sa = (
                config.worker_service_account
                if kind == "worker_job"
                else config.api_service_account
            )
            desired_env = desired_cloud_run_env(
                state, config, world.redis_identity, secret_refs
            )
            desired_payload = _cloud_run_payload(desired_sa, desired_env)
            if len(state.containers) != 1:
                raise PlannerEvidenceError(
                    f"cloud run {kind} has {len(state.containers)} candidate "
                    "application containers; ambiguous evidence cannot be planned"
                )
            observed_env = state.containers[0].env
            observed_payload = _cloud_run_payload(state.service_account, observed_env)
            if observed_payload != desired_payload:
                resource = ResourceIdentity(
                    provider=Provider.GCP,
                    kind=f"cloud_run_{kind}",
                    name=name,
                    scope=config.gcp_project_id,
                )
                op_type = (
                    OperationType.GCP_UPDATE_WORKER_JOB_CONFIG
                    if kind == "worker_job"
                    else OperationType.GCP_UPDATE_API_SERVICE_CONFIG
                )
                operations.append(
                    MutationOperation(
                        sequence=_next_sequence(seq),
                        provider=Provider.GCP,
                        operation_type=op_type,
                        resource=resource,
                        expected_pre_state_digest=state_digest(observed_payload),
                        intended_post_state_digest=state_digest(desired_payload),
                        reason=f"{kind} configuration drift from intended state",
                        idempotency_key=f"{op_type.value}:{resource.key()}",
                        can_incur_cost=False,
                        has_safe_compensation=False,
                        post_write_read=PostWriteRead(
                            description=f"reread {kind} config and compare exactly",
                            expected_post_state_digest=state_digest(desired_payload),
                        ),
                    )
                )

    # ---- Stage E: Vercel ----------------------------------------------------
    world.vercel_project.require_decisive("vercel project")
    if world.redis_identity is not None:
        observed_env = dict(world.vercel_env)
        desired_values: dict[str, str] = {
            "GATEWAY_ALLOW_EXECUTION_ROUTES": _fingerprint("false"),
            "NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI": _fingerprint("false"),
            "UPSTASH_REDIS_REST_URL": _fingerprint(world.redis_identity.rest_url),
            "UPSTASH_REDIS_REST_TOKEN": world.redis_identity.token_fingerprint_sha256,
            "MILO_REDIS_TOKEN_FINGERPRINT": _fingerprint(
                world.redis_identity.token_fingerprint_sha256
            ),
        }
        for key in VERCEL_MANAGED_VARS:
            desired_fp = desired_values[key]
            observed_var = observed_env.get(key)
            current_fp = ""
            if observed_var is not None:
                observed_var.require_decisive(f"vercel env var {key}")
                if isinstance(observed_var.state, VercelEnvVarState):
                    current_fp = observed_var.state.value_fingerprint_sha256
            if current_fp != desired_fp:
                resource = ResourceIdentity(
                    provider=Provider.VERCEL,
                    kind="env_var",
                    name=key,
                    scope=config.vercel_project_id or config.vercel_project_name,
                )
                operations.append(
                    MutationOperation(
                        sequence=_next_sequence(seq),
                        provider=Provider.VERCEL,
                        operation_type=OperationType.VERCEL_SET_ENV_VAR,
                        resource=resource,
                        expected_pre_state_digest=(
                            state_digest({"fingerprint": current_fp})
                            if current_fp
                            else ABSENT_DIGEST
                        ),
                        intended_post_state_digest=desired_fp,
                        reason="managed vercel variable differs from intended value",
                        idempotency_key=f"{OperationType.VERCEL_SET_ENV_VAR.value}:{resource.key()}",
                        can_incur_cost=False,
                        has_safe_compensation=True,
                        post_write_read=PostWriteRead(
                            description=f"reread vercel env var {key} and compare fingerprint",
                            expected_post_state_digest=desired_fp,
                        ),
                    )
                )

    return MutationPlan(
        mode=Mode.APPLY,
        bootstrap_sha=config.bootstrap_sha,
        operations=tuple(operations),
    )


def _intended_secret_accessors(
    secret_name: str, config: BootstrapConfig
) -> tuple[str, ...]:
    """The exact accessor member set per secret. No project-wide grants."""

    api = f"serviceAccount:{config.api_service_account}"
    worker = f"serviceAccount:{config.worker_service_account}"
    consumers: dict[str, tuple[str, ...]] = {
        "SUPABASE_URL": (api, worker),
        "SUPABASE_SECRET_KEY": (api, worker),
        "KIMI_API_KEY": (worker,),
        "UPSTASH_REDIS_REST_TOKEN": (api, worker),
    }
    return consumers.get(secret_name, ())


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

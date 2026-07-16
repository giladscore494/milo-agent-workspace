"""Typed domain model for bootstrap v2.

Every object is frozen. There are no mutable module-level failure flags and
no competing blocker counters anywhere in this package: the single source of
failure truth is the immutable ``RunResult`` assembled in ``result.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Mode(Enum):
    PLAN = "plan"
    APPLY = "apply"
    AUDIT = "audit"


class Severity(Enum):
    PASS = "pass"
    INFO = "info"
    WARN = "warn"
    BLOCKED = "blocked"
    UNVERIFIED = "unverified"


class RunStatus(Enum):
    PLANNED = "planned"
    BLOCKED = "blocked"
    PARTIAL = "partial"
    APPLIED = "applied"
    AUDITED = "audited"


class Stage(Enum):
    INITIAL = "initial"
    LOCAL_GUARD_VERIFIED = "local_guard_verified"
    GLOBAL_DISCOVERY_COMPLETE = "global_discovery_complete"
    PLAN_FROZEN = "plan_frozen"
    APPLY_AUTHORIZED = "apply_authorized"
    UPSTASH_STAGE_VERIFIED = "upstash_stage_verified"
    GCP_IDENTITY_SECRET_STAGE_VERIFIED = "gcp_identity_secret_stage_verified"
    IAM_STAGE_VERIFIED = "iam_stage_verified"
    CLOUD_RUN_STAGE_VERIFIED = "cloud_run_stage_verified"
    VERCEL_STAGE_VERIFIED = "vercel_stage_verified"
    FINAL_AUDIT_VERIFIED = "final_audit_verified"
    METADATA_COMMITTED = "metadata_committed"
    COMPLETE = "complete"
    BLOCKED = "blocked"


class Provider(Enum):
    LOCAL = "local"
    GCP = "gcp"
    UPSTASH = "upstash"
    VERCEL = "vercel"
    GITHUB = "github"


class ProbeOutcome(Enum):
    """Classified result of one provider read.

    Only ``CLEANLY_ABSENT`` is absence. A permission error, network error,
    parser failure, timeout, disabled API, or unknown error is never
    interpreted as "resource does not exist".
    """

    PRESENT = "present"
    CLEANLY_ABSENT = "cleanly_absent"
    PERMISSION_DENIED = "permission_denied"
    AUTH_FAILURE = "auth_failure"
    API_DISABLED = "api_disabled"
    RATE_LIMITED = "rate_limited"
    NETWORK_FAILURE = "network_failure"
    MALFORMED_OUTPUT = "malformed_output"
    TIMEOUT = "timeout"
    UNKNOWN_ERROR = "unknown_error"


#: Outcomes that positively answer "does the resource exist?".
DECISIVE_OUTCOMES = frozenset({ProbeOutcome.PRESENT, ProbeOutcome.CLEANLY_ABSENT})


class OperationType(Enum):
    UPSTASH_CREATE_DATABASE = "upstash_create_database"
    GCP_CREATE_SERVICE_ACCOUNT = "gcp_create_service_account"
    GCP_CREATE_SECRET = "gcp_create_secret"
    GCP_ADD_SECRET_VERSION = "gcp_add_secret_version"
    GCP_SET_SECRET_IAM = "gcp_set_secret_iam"
    GCP_SET_WIF_IAM = "gcp_set_wif_iam"
    GCP_SET_RUN_INVOKER_IAM = "gcp_set_run_invoker_iam"
    GCP_UPDATE_WORKER_JOB_CONFIG = "gcp_update_worker_job_config"
    GCP_UPDATE_API_SERVICE_CONFIG = "gcp_update_api_service_config"
    VERCEL_SET_ENV_VAR = "vercel_set_env_var"


class MetadataStatus(Enum):
    COMMITTED = "committed"
    WITHHELD = "withheld"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True, slots=True)
class ResourceIdentity:
    """Exact identity of one external resource."""

    provider: Provider
    kind: str
    name: str
    scope: str = ""

    def key(self) -> str:
        return f"{self.provider.value}:{self.kind}:{self.scope}:{self.name}"


@dataclass(frozen=True, slots=True)
class Finding:
    code: str
    severity: Severity
    message: str
    stage: Stage
    critical: bool = True
    requires_manual: bool = False
    unknown: bool = False

    def blocks_apply(self) -> bool:
        """Apply mode has no soft critical findings."""
        if not self.critical:
            return False
        if self.severity in (Severity.WARN, Severity.BLOCKED, Severity.UNVERIFIED):
            return True
        return self.requires_manual or self.unknown


@dataclass(frozen=True, slots=True)
class Evidence:
    """One classified observation of external state.

    ``observed_json`` is the canonical JSON of the observed payload (empty
    string when nothing was observed). Evidence is immutable; staleness is
    decided by re-running discovery, never by editing evidence.
    """

    source: str
    outcome: ProbeOutcome
    resource: ResourceIdentity | None = None
    observed_json: str = ""
    stale: bool = False

    def is_usable(self) -> bool:
        return self.outcome in DECISIVE_OUTCOMES and not self.stale


@dataclass(frozen=True, slots=True)
class ReadOperation:
    sequence: int
    provider: Provider
    description: str
    outcome: ProbeOutcome
    resource: ResourceIdentity | None = None
    critical: bool = True


@dataclass(frozen=True, slots=True)
class PostWriteRead:
    """Declaration of the exact read that must follow a mutation."""

    description: str
    expected_post_state_digest: str


@dataclass(frozen=True, slots=True)
class MutationOperation:
    sequence: int
    provider: Provider
    operation_type: OperationType
    resource: ResourceIdentity
    expected_pre_state_digest: str
    intended_post_state_digest: str
    reason: str
    idempotency_key: str
    can_incur_cost: bool
    has_safe_compensation: bool
    post_write_read: PostWriteRead


@dataclass(frozen=True, slots=True)
class MutationPlan:
    """Immutable, ordered mutation plan generated by the pure planner."""

    mode: Mode
    bootstrap_sha: str
    operations: tuple[MutationOperation, ...] = ()

    def operation_keys(self) -> frozenset[str]:
        return frozenset(op.idempotency_key for op in self.operations)


@dataclass(frozen=True, slots=True)
class MutationRecord:
    """Ledger entry for one attempted external write."""

    sequence: int
    provider: Provider
    operation_type: OperationType
    resource: ResourceIdentity
    idempotency_key: str
    declared: bool
    executed: bool
    succeeded: bool
    error_class: str = ""


@dataclass(frozen=True, slots=True)
class PostWriteVerification:
    idempotency_key: str
    verified: bool
    observed_post_state_digest: str
    expected_post_state_digest: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class RecoveryStep:
    order: int
    description: str


@dataclass(frozen=True, slots=True)
class StageResult:
    stage: Stage
    findings: tuple[Finding, ...] = ()
    reads: tuple[ReadOperation, ...] = ()
    mutations: tuple[MutationRecord, ...] = ()
    verifications: tuple[PostWriteVerification, ...] = ()


@dataclass(frozen=True, slots=True)
class RunResult:
    """The single authoritative result object.

    Exit code, human summary, JSON report, metadata eligibility and workflow
    conclusion all derive from this object and nothing else.
    """

    mode: Mode
    status: RunStatus
    starting_sha: str
    trusted_ref: str
    plan_digest: str
    last_completed_stage: Stage
    findings: tuple[Finding, ...] = ()
    reads: tuple[ReadOperation, ...] = ()
    mutations: tuple[MutationRecord, ...] = ()
    verifications: tuple[PostWriteVerification, ...] = ()
    created_resources: tuple[ResourceIdentity, ...] = ()
    recovery_steps: tuple[RecoveryStep, ...] = ()
    metadata_status: MetadataStatus = MetadataStatus.NOT_APPLICABLE

    def has_blocker(self) -> bool:
        if any(f.severity is Severity.BLOCKED for f in self.findings):
            return True
        if self.mode is Mode.APPLY and any(f.blocks_apply() for f in self.findings):
            return True
        if any(m.executed and not m.succeeded for m in self.mutations):
            return True
        if any(not v.verified for v in self.verifications):
            return True
        if any(not m.declared for m in self.mutations):
            return True
        return False

    def exit_code(self) -> int:
        if self.has_blocker():
            return 1
        if self.status in (RunStatus.BLOCKED, RunStatus.PARTIAL):
            return 1
        return 0

    def metadata_eligible(self) -> bool:
        return (
            self.mode is Mode.APPLY
            and self.status is RunStatus.APPLIED
            and not self.has_blocker()
        )


# --------------------------------------------------------------------------
# Provider-specific discovered-state models
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LocalIdentityState:
    repository: str
    head_sha: str
    ref: str
    worktree_clean: bool
    environment: str
    operator_ack: str
    python_ok: bool
    tooling_ok: bool
    dotenv_influence: tuple[str, ...] = ()
    deprecated_metadata_keys: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class UpstashDatabaseState:
    database_id: str
    name: str
    state: str
    tls: bool
    region: str
    endpoint: str
    rest_url: str
    token_fingerprint_sha256: str = ""


@dataclass(frozen=True, slots=True)
class GcpServiceAccountState:
    email: str
    exists: bool
    disabled: bool = False


@dataclass(frozen=True, slots=True)
class SecretState:
    name: str
    exists: bool
    enabled_versions: tuple[str, ...] = ()
    latest_enabled_version: str = ""
    payload_fingerprint_sha256: str = ""


@dataclass(frozen=True, slots=True)
class IamBinding:
    role: str
    members: tuple[str, ...]
    condition_expression: str = ""
    condition_title: str = ""


@dataclass(frozen=True, slots=True)
class IamPolicyState:
    resource: ResourceIdentity
    etag: str
    bindings: tuple[IamBinding, ...] = ()


@dataclass(frozen=True, slots=True)
class WifState:
    pool_id: str
    provider_id: str
    issuer_uri: str
    allowed_audiences: tuple[str, ...]
    attribute_mapping: tuple[tuple[str, str], ...]
    attribute_condition: str
    pool_state: str = ""
    provider_state: str = ""


@dataclass(frozen=True, slots=True)
class CloudRunEnvVar:
    name: str
    value: str = ""
    secret_name: str = ""
    secret_version: str = ""

    def is_secret_ref(self) -> bool:
        return bool(self.secret_name)


@dataclass(frozen=True, slots=True)
class CloudRunContainerState:
    name: str
    image: str
    env: tuple[CloudRunEnvVar, ...] = ()


@dataclass(frozen=True, slots=True)
class CloudRunResourceState:
    resource: ResourceIdentity
    service_account: str
    containers: tuple[CloudRunContainerState, ...] = ()
    ingress: str = ""
    allows_unauthenticated: bool = False


@dataclass(frozen=True, slots=True)
class VercelProjectState:
    project_id: str
    org_id: str
    name: str


@dataclass(frozen=True, slots=True)
class VercelEnvVarState:
    key: str
    target: tuple[str, ...]
    value_fingerprint_sha256: str = ""
    env_var_id: str = ""


@dataclass(frozen=True, slots=True)
class RedisIdentity:
    """One coherent cross-provider Redis identity.

    Every field must originate from the same selected database; mixing
    fields from different databases is rejected by
    ``validators.redis.verify_redis_identity_coherence``.
    """

    database_id: str
    database_name: str
    rest_url: str
    token_fingerprint_sha256: str
    secret_resource_name: str
    enabled_secret_version: str
    api_secret_version_pin: str
    worker_secret_version_pin: str
    vercel_value_fingerprint_sha256: str
    logical_environment: str = "production"


@dataclass(frozen=True, slots=True)
class MetadataV3:
    """Closed metadata schema, version 3.

    Fields are the exact allowlisted keys; construction with unknown keys is
    impossible by design. Validation of values happens in
    ``validators.metadata``.
    """

    MILO_METADATA_SCHEMA_VERSION: str
    MILO_BOOTSTRAP_STATUS: str
    MILO_ENVIRONMENT: str
    MILO_BOOTSTRAP_SHA: str
    MILO_PLAN_DIGEST: str
    MILO_METADATA_GENERATED_AT: str
    GITHUB_REPOSITORY: str
    GITHUB_RUN_ID: str
    GITHUB_WORKFLOW_REF: str
    GITHUB_HEAD_REF: str
    GCP_PROJECT_ID: str
    GCP_PROJECT_NUMBER: str
    GCP_REGION: str
    CLOUD_RUN_API_SERVICE: str
    CLOUD_RUN_WORKER_JOB: str
    API_SERVICE_ACCOUNT: str
    WORKER_SERVICE_ACCOUNT: str
    GATEWAY_IDENTITY: str
    SUPABASE_PROJECT_REF: str
    VERCEL_PROJECT: str
    VERCEL_PROJECT_ID: str
    VERCEL_ORG_ID: str
    PRODUCTION_ORIGIN: str
    MILO_REDIS_LOGICAL_ENVIRONMENT: str
    UPSTASH_REDIS_REST_URL: str
    SUPABASE_URL_SECRET_NAME: str
    SUPABASE_SERVICE_KEY_SECRET_NAME: str
    PROVIDER_KEY_SECRET_NAME: str
    REDIS_TOKEN_SECRET_NAME: str
    MILO_REDIS_DB_ID: str
    MILO_REDIS_TOKEN_FINGERPRINT: str
    MILO_REDIS_SECRET_VERSION: str

    def as_mapping(self) -> dict[str, str]:
        return {name: getattr(self, name) for name in METADATA_V3_KEYS}


METADATA_V3_KEYS: tuple[str, ...] = tuple(
    MetadataV3.__dataclass_fields__  # noqa: SLF001 - public dataclass API
)

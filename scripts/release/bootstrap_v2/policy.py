"""Validated configuration and the compatibility contracts of Phase 8.

Live values are never hardcoded into provider logic. They enter as
validated configuration (`BootstrapConfig`), with the currently documented
production values recorded here as *defaults that must still be verified
exactly against live state* during discovery.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields

# ---------------------------------------------------------------------------
# Contract constants (names, not secrets)
# ---------------------------------------------------------------------------

EXECUTION_FLAGS: tuple[str, ...] = (
    "MILO_ENABLE_RUN_CREATION",
    "MILO_ENABLE_PROPOSAL_MUTATIONS",
    "MILO_ENABLE_PROPOSAL_READS",
    "MILO_ENABLE_RUN_CANCELLATION",
    "MILO_ENABLE_EXECUTION_CONTROL",
    "MILO_ENABLE_PAID_EXECUTION",
)

#: The only accepted value for every execution flag. No empty, zero, no,
#: off, uppercase, or missing representation is accepted.
EXECUTION_FLAG_REQUIRED_VALUE = "false"

BUDGET_VARS: tuple[str, ...] = (
    "MILO_MAX_COST_PER_RUN",
    "MILO_DAILY_USER_BUDGET",
    "MILO_DAILY_PROJECT_BUDGET",
    "MILO_MAX_MODEL_CALLS_PER_RUN",
    "MILO_MAX_TOTAL_TOKENS_PER_RUN",
    "MILO_MAX_RUN_DURATION_SECONDS",
)

#: Approved Stage-A upper bounds for budget values (inclusive).
BUDGET_UPPER_BOUNDS: dict[str, float] = {
    "MILO_MAX_COST_PER_RUN": 5.0,
    "MILO_DAILY_USER_BUDGET": 25.0,
    "MILO_DAILY_PROJECT_BUDGET": 100.0,
    "MILO_MAX_MODEL_CALLS_PER_RUN": 200.0,
    "MILO_MAX_TOTAL_TOKENS_PER_RUN": 2_000_000.0,
    "MILO_MAX_RUN_DURATION_SECONDS": 3_600.0,
}

CLOUD_RUN_PLAIN_ENV_VARS: tuple[str, ...] = (
    "ENVIRONMENT",
    "ALLOWED_CORS_ORIGINS",
    "MILO_GATEWAY_AUDIENCE",
    "MILO_APPROVED_GATEWAY_IDENTITIES",
    "MILO_APPROVED_WORKER_IDENTITIES",
    "MILO_WORKER_AUDIENCE",
    "JOB_LAUNCHER",
    "GATEWAY_ALLOW_EXECUTION_ROUTES",
    "UPSTASH_REDIS_REST_URL",
    "MILO_REDIS_DB_ID",
    "MILO_REDIS_TOKEN_FINGERPRINT",
    "MILO_REDIS_SECRET_VERSION",
    "MILO_BOOTSTRAP_SHA",
)

SECRET_MANAGER_RESOURCE_NAMES: tuple[str, ...] = (
    "SUPABASE_URL",
    "SUPABASE_SECRET_KEY",
    "KIMI_API_KEY",
    "UPSTASH_REDIS_REST_TOKEN",
)

#: Cloud Run env-var name -> Secret Manager resource name, per consumer.
API_SECRET_REFS: dict[str, str] = {
    "SUPABASE_URL": "SUPABASE_URL",
    "SUPABASE_SECRET_KEY": "SUPABASE_SECRET_KEY",
    "UPSTASH_REDIS_REST_TOKEN": "UPSTASH_REDIS_REST_TOKEN",
}

WORKER_SECRET_REFS: dict[str, str] = {
    "SUPABASE_URL": "SUPABASE_URL",
    "SUPABASE_SECRET_KEY": "SUPABASE_SECRET_KEY",
    "KIMI_API_KEY": "KIMI_API_KEY",
    "UPSTASH_REDIS_REST_TOKEN": "UPSTASH_REDIS_REST_TOKEN",
}

#: Secret refs that must be pinned to an exact numeric version (never
#: ``latest``) on both consumers.
NUMERIC_PIN_REQUIRED_SECRETS: tuple[str, ...] = ("UPSTASH_REDIS_REST_TOKEN",)

VERCEL_REUSED_VARS: tuple[str, ...] = (
    "CLOUD_RUN_API_URL",
    "GCP_PROJECT_NUMBER",
    "GCP_WORKLOAD_IDENTITY_POOL_ID",
    "GCP_WORKLOAD_IDENTITY_POOL_PROVIDER_ID",
    "GCP_SERVICE_ACCOUNT_EMAIL",
    "NEXT_PUBLIC_SUPABASE_URL",
    "NEXT_PUBLIC_SUPABASE_ANON_KEY",
)

VERCEL_MANAGED_VARS: tuple[str, ...] = (
    "GATEWAY_ALLOW_EXECUTION_ROUTES",
    "NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI",
    "UPSTASH_REDIS_REST_URL",
    "UPSTASH_REDIS_REST_TOKEN",
    "MILO_REDIS_TOKEN_FINGERPRINT",
)

VERCEL_FORBIDDEN_VARS: tuple[str, ...] = (
    "SUPABASE_SERVICE_ROLE_KEY",
    "SUPABASE_SECRET_KEY",
    "KIMI_API_KEY",
    "MOONSHOT_API_KEY",
)

GITHUB_SECRET_NAMES: tuple[str, ...] = (
    "UPSTASH_EMAIL",
    "UPSTASH_API_KEY",
    "VERCEL_TOKEN",
)

GITHUB_VARIABLE_NAMES: tuple[str, ...] = (
    "GITHUB_WIF_PROVIDER_RESOURCE",
    "GITHUB_BOOTSTRAP_OPERATOR_SA",
    "VERCEL_ORG_ID",
    "VERCEL_PROJECT_ID",
    "VERCEL_WIF_POOL_ID",
    "VERCEL_WIF_PROVIDER_ID",
    "VERCEL_WIF_ISSUER",
    "VERCEL_WIF_ALLOWED_AUDIENCE",
    "VERCEL_WIF_ATTRIBUTE_CONDITION",
    "VERCEL_WIF_ATTRIBUTE_MAPPING",
    "VERCEL_WIF_PRINCIPAL_SET",
)

COMPAT_SCRIPT_ALIASES: tuple[str, ...] = (
    "MILO_OPERATOR_ACK",
    "MILO_UPSTASH_EMAIL",
    "MILO_UPSTASH_APIKEY",
    "VERCEL_TOKEN",
    "VERCEL_ORG_ID",
    "VERCEL_PROJECT_ID",
)

#: Deprecated key: must be removed from Cloud Run, verified absent from both
#: API and worker, and rejected everywhere in metadata v3.
DEPRECATED_METADATA_KEYS: tuple[str, ...] = ("MILO_RELEASE_SHA",)

#: Legacy runtime identity: may be discovered and reported for migration,
#: never silently accepted as both API and worker identity.
LEGACY_RUNTIME_IDENTITY_PATTERN = re.compile(
    r"^id-kimi-agent-runner@[a-z0-9-]+\.iam\.gserviceaccount\.com$"
)

FORBIDDEN_IAM_PRINCIPALS: tuple[str, ...] = ("allUsers", "allAuthenticatedUsers")

REQUIRED_GCP_APIS: tuple[str, ...] = (
    "run.googleapis.com",
    "artifactregistry.googleapis.com",
    "secretmanager.googleapis.com",
    "iamcredentials.googleapis.com",
    "sts.googleapis.com",
    "iam.googleapis.com",
)

OPERATOR_ACK_EXPECTED = "I_UNDERSTAND_THIS_CHANGES_PRODUCTION"

_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SA_EMAIL_RE = re.compile(r"^[a-z0-9-]{6,30}@[a-z0-9.-]+\.iam\.gserviceaccount\.com$")
_PLACEHOLDER_RE = re.compile(r"[<>]|changeme|placeholder|example\.com|TODO", re.IGNORECASE)


class ConfigError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class BootstrapConfig:
    """Operator-facing configuration, validated at construction.

    Defaults document the current production values but every value is
    re-verified exactly against live discovery before any mutation.
    """

    bootstrap_sha: str
    gcp_project_id: str = "big-cabinet-457321-t7"
    gcp_region: str = "us-central1"
    cloud_run_api_service: str = "milo-agent-api"
    cloud_run_worker_job: str = "milo-agent-worker"
    cloud_run_api_url: str = "https://milo-agent-api-beplbca7yq-uc.a.run.app"
    api_service_account: str = "milo-agent-api@big-cabinet-457321-t7.iam.gserviceaccount.com"
    worker_service_account: str = "milo-agent-worker@big-cabinet-457321-t7.iam.gserviceaccount.com"
    gateway_service_account: str = "milo-vercel-gateway@big-cabinet-457321-t7.iam.gserviceaccount.com"
    wif_pool_id: str = "vercel-gateway"
    wif_provider_id: str = "vercel-team"
    wif_issuer: str = "https://oidc.vercel.com/chatswarm"
    wif_allowed_audience: str = "https://vercel.com/chatswarm"
    wif_attribute_condition: str = (
        "owner:chatswarm:project:milo-agent-workspace:environment:production"
    )
    upstash_database_name: str = "milo-production"
    upstash_database_id: str = ""
    vercel_project_id: str = ""
    vercel_org_id: str = ""
    vercel_project_name: str = "milo-agent-workspace"
    environment: str = "production"
    operator_ack: str = ""

    def __post_init__(self) -> None:
        problems = tuple(self.validation_problems())
        if problems:
            raise ConfigError("; ".join(problems))

    def validation_problems(self) -> list[str]:
        problems: list[str] = []
        if not _FULL_SHA_RE.match(self.bootstrap_sha):
            problems.append("bootstrap_sha must be a full 40-character lowercase sha")
        for name in (
            "api_service_account",
            "worker_service_account",
            "gateway_service_account",
        ):
            value = getattr(self, name)
            if not _SA_EMAIL_RE.match(value):
                problems.append(f"{name} is not a valid service-account email: {value!r}")
        emails = {
            self.api_service_account,
            self.worker_service_account,
            self.gateway_service_account,
        }
        if len(emails) != 3:
            problems.append("api, worker and gateway service accounts must be distinct")
        for spec in fields(self):
            value = getattr(self, spec.name)
            if isinstance(value, str) and _PLACEHOLDER_RE.search(value):
                problems.append(f"{spec.name} contains a placeholder value")
        if self.environment != "production":
            problems.append("environment must be exactly 'production'")
        if not self.cloud_run_api_url.startswith("https://"):
            problems.append("cloud_run_api_url must be https")
        if not self.wif_issuer.startswith("https://oidc.vercel.com/"):
            problems.append("wif_issuer must be a Vercel OIDC issuer")
        return problems

    def uses_legacy_runtime_identity(self) -> bool:
        return any(
            LEGACY_RUNTIME_IDENTITY_PATTERN.match(email)
            for email in (
                self.api_service_account,
                self.worker_service_account,
                self.gateway_service_account,
            )
        )

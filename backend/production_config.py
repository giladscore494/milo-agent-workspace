"""Central production configuration validation.

Separates configuration into clearly-scoped groups and validates
combinations at startup. Every execution-related value is OFF by default;
production must fail closed when a dangerous or incomplete combination is
detected.

This module is import-safe (no side effects). Call
``validate_production_config()`` explicitly (startup hook, the
``check-production-config`` operator script, or tests). Local/dev
environments only warn; production raises.
"""

from __future__ import annotations

import os
import json
import sys
from dataclasses import dataclass, field
from typing import Callable

from backend.budget import BudgetConfig

TRUE_VALUES = {"1", "true", "yes", "on"}


class ProductionConfigError(RuntimeError):
    """Sanitized startup failure whose string form is stable and bounded."""

    def __init__(self, issues: list[ConfigIssue]) -> None:
        self.codes = tuple(sorted({issue.code for issue in issues}))
        super().__init__(f"CONFIG_VALIDATION_FAILED[{','.join(self.codes)}]")


def _flag(env: dict[str, str], name: str) -> bool:
    return (env.get(name) or "").strip().lower() in TRUE_VALUES


def _is_production(env: dict[str, str]) -> bool:
    return (env.get("ENVIRONMENT") or "local").strip().lower() == "production"


@dataclass
class ConfigIssue:
    level: str  # "error" | "warning"
    code: str
    message: str


@dataclass
class ConfigReport:
    issues: list[ConfigIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[ConfigIssue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[ConfigIssue]:
        return [i for i in self.issues if i.level == "warning"]

    def ok(self) -> bool:
        return not self.errors


# Configuration scopes (documented separation; values themselves live in the
# environment/secret stores, never in the repository).
PUBLIC_FRONTEND_KEYS = ("NEXT_PUBLIC_SUPABASE_URL", "NEXT_PUBLIC_SUPABASE_ANON_KEY", "NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI")
GATEWAY_KEYS = ("CLOUD_RUN_API_URL", "GATEWAY_ALLOW_EXECUTION_ROUTES", "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN")
BACKEND_KEYS = ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "ALLOWED_CORS_ORIGINS", "ENVIRONMENT")
WORKER_KEYS = ("MILO_WORKER_AUDIENCE", "MILO_APPROVED_WORKER_IDENTITIES")
EXECUTION_FLAGS = (
    "MILO_ENABLE_RUN_CREATION",
    "MILO_ENABLE_PROPOSAL_MUTATIONS",
    "MILO_ENABLE_PROPOSAL_READS",
    "MILO_ENABLE_RUN_CANCELLATION",
    "MILO_ENABLE_EXECUTION_CONTROL",
    "MILO_ENABLE_PAID_EXECUTION",
)

# NEXT_PUBLIC_* values ship to the browser bundle: secret material is banned.
FORBIDDEN_PUBLIC_SUBSTRINGS = ("service_role", "service-role", "sb_secret", "secret_key", "secretkey", "private_key")


def validate(env: dict[str, str] | None = None) -> ConfigReport:
    """Return a report of configuration issues without raising."""
    env = dict(os.environ if env is None else env)
    report = ConfigReport()
    production = _is_production(env)

    def error(code: str, message: str) -> None:
        report.issues.append(ConfigIssue("error", code, message))

    def warn(code: str, message: str) -> None:
        report.issues.append(ConfigIssue("warning", code, message))

    # 1. Secret material must never be in NEXT_PUBLIC_* variables.
    for key, value in env.items():
        if key.startswith("NEXT_PUBLIC_") and value:
            lowered = value.lower()
            if any(token in lowered for token in FORBIDDEN_PUBLIC_SUBSTRINGS):
                error("PUBLIC_CONTAINS_SECRET", f"{key} appears to contain secret material and is exposed to the browser")

    # 2. CORS must never be a wildcard.
    cors = (env.get("ALLOWED_CORS_ORIGINS") or "").strip()
    origins = [o.strip() for o in cors.split(",") if o.strip()]
    if "*" in origins or cors == "*":
        error("CORS_WILDCARD", "ALLOWED_CORS_ORIGINS must list explicit origins; wildcard is forbidden")
    if production and not origins:
        error("CORS_MISSING", "ALLOWED_CORS_ORIGINS must be set in production")

    # 3. Execution requires budget caps.
    budget = BudgetConfig.from_env(env)
    execution_enabled = _flag(env, "MILO_ENABLE_RUN_CREATION")
    paid_enabled = _flag(env, "MILO_ENABLE_PAID_EXECUTION")
    if execution_enabled:
        missing = budget.missing_mandatory()
        if missing:
            (error if production else warn)("EXECUTION_WITHOUT_BUDGET", f"run creation enabled without mandatory budget caps: {', '.join(missing)}")

    # 4. Paid execution requires a provider key AND budget caps.
    if paid_enabled:
        if not (env.get("KIMI_API_KEY") or env.get("MOONSHOT_API_KEY")):
            # We never read the value; only assert the variable name is present.
            (error if production else warn)("PAID_WITHOUT_PROVIDER_KEY", "paid execution enabled without a provider API key configured")
        if budget.missing_mandatory():
            error("PAID_WITHOUT_BUDGET", "paid execution enabled without mandatory budget caps")

    # 5. Worker mutations require service-to-service auth configuration.
    if _flag(env, "MILO_ENABLE_EXECUTION_CONTROL"):
        if not (env.get("MILO_WORKER_AUDIENCE") or "").strip():
            (error if production else warn)("WORKER_AUTH_AUDIENCE_MISSING", "worker mutations enabled without MILO_WORKER_AUDIENCE")
        if not (env.get("MILO_APPROVED_WORKER_IDENTITIES") or "").strip():
            (error if production else warn)("WORKER_ALLOWLIST_EMPTY", "worker mutations enabled without MILO_APPROVED_WORKER_IDENTITIES")

    # 5b. Browser routes require verified gateway identity in production.
    gateway_audience = (env.get("MILO_GATEWAY_AUDIENCE") or "").strip()
    gateway_identities = {i.strip().lower() for i in (env.get("MILO_APPROVED_GATEWAY_IDENTITIES") or "").split(",") if i.strip()}
    worker_identities = {i.strip().lower() for i in (env.get("MILO_APPROVED_WORKER_IDENTITIES") or "").split(",") if i.strip()}
    if production and (not gateway_audience or not gateway_identities):
        error("GATEWAY_AUTH_MISSING", "production requires MILO_GATEWAY_AUDIENCE and a non-empty MILO_APPROVED_GATEWAY_IDENTITIES; browser identity headers are never trusted bare")
    if production and _flag(env, "MILO_ALLOW_INSECURE_DEV_IDENTITY"):
        error("INSECURE_DEV_IDENTITY_IN_PRODUCTION", "MILO_ALLOW_INSECURE_DEV_IDENTITY is test/local-only and forbidden in production")

    # 5c. Gateway and worker identities must be strictly separated: a shared
    # service account (or a shared audience) would let the worker mint
    # browser identities or vice versa.
    overlap = gateway_identities & worker_identities
    if overlap:
        error("SHARED_GATEWAY_WORKER_IDENTITY", f"identities approved for both gateway and worker roles: {', '.join(sorted(overlap))}")

    # 5d. Test adapters may never reach production configuration.
    if production:
        if (env.get("CLOUD_RUN_AUTH_MODE") or "").strip().lower() == "e2e-test":
            error("TEST_ADAPTER_IN_PRODUCTION", "CLOUD_RUN_AUTH_MODE=e2e-test is a test-only escape and is forbidden in production")
        if _flag(env, "MILO_E2E_INPROCESS_WORKER"):
            error("TEST_ADAPTER_IN_PRODUCTION", "MILO_E2E_INPROCESS_WORKER is a test-only adapter and is forbidden in production")
        if (env.get("MILO_WORKER_ENGINE") or "").strip():
            error("TEST_ADAPTER_IN_PRODUCTION", "MILO_WORKER_ENGINE selects a non-production engine (e.g. the zero-cost mock) and is forbidden in production")

    # 5e. Staging must be pinned to its declared dependencies (fail closed):
    # a staging deployment must never be able to silently point at the
    # production Supabase or Redis environment. Values are never echoed.
    if (env.get("ENVIRONMENT") or "").strip().lower() == "staging":
        from urllib.parse import urlparse

        expected_ref = (env.get("MILO_EXPECTED_SUPABASE_PROJECT_REF") or "").strip()
        supabase_url = (env.get("SUPABASE_URL") or "").strip()
        if not expected_ref:
            error("STAGING_DEPENDENCY_UNPINNED", "ENVIRONMENT=staging requires MILO_EXPECTED_SUPABASE_PROJECT_REF so the runtime refuses any non-staging Supabase project")
        elif supabase_url:
            host = (urlparse(supabase_url).hostname or "").lower()
            if host != f"{expected_ref.lower()}.supabase.co":
                error("STAGING_DEPENDENCY_MISMATCH", "SUPABASE_URL does not match the expected staging Supabase project ref (values not shown)")
        expected_redis_host = (env.get("MILO_EXPECTED_REDIS_HOST") or "").strip().lower()
        redis_url = (env.get("UPSTASH_REDIS_REST_URL") or "").strip()
        if not expected_redis_host:
            error("STAGING_DEPENDENCY_UNPINNED", "ENVIRONMENT=staging requires MILO_EXPECTED_REDIS_HOST so the runtime refuses any non-staging Redis endpoint")
        elif redis_url:
            host = (urlparse(redis_url).hostname or "").lower()
            if host != expected_redis_host:
                error("STAGING_DEPENDENCY_MISMATCH", "UPSTASH_REDIS_REST_URL host does not match the expected staging Redis host (values not shown)")

    # 6. Public execution UI cannot imply backend run creation.
    public_ui = _flag(env, "NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI")
    if public_ui and not execution_enabled:
        # Allowed: the UI renders a disabled state. Surface as a warning so
        # operators confirm it is intentional.
        warn("UI_WITHOUT_BACKEND_EXECUTION", "public execution UI is enabled while backend run creation is disabled; it must render a clear disabled state")

    # 7. Production must not use the in-memory rate limiter.
    if production and not (env.get("UPSTASH_REDIS_REST_URL") and env.get("UPSTASH_REDIS_REST_TOKEN")):
        error("PROD_MEMORY_RATE_LIMITER", "production requires a shared rate-limit store (UPSTASH_REDIS_REST_URL/TOKEN)")

    # 8. Gateway execution routes should not be open while backend disabled.
    if _flag(env, "GATEWAY_ALLOW_EXECUTION_ROUTES") and not execution_enabled:
        warn("GATEWAY_EXECUTION_OPEN_BACKEND_DISABLED", "gateway execution routes are open while backend run creation is disabled")

    # 9. Mandatory production backend settings.
    if production:
        for key in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"):
            if not (env.get(key) or "").strip():
                error("MISSING_BACKEND_SETTING", f"{key} is required in production")

    return report


def validate_production_config(env: dict[str, str] | None = None, on_error: Callable[[str], None] | None = None) -> ConfigReport:
    """Validate and fail closed in production when errors are present."""
    env = dict(os.environ if env is None else env)
    report = validate(env)
    environment = (env.get("ENVIRONMENT") or "").strip().lower()
    # Production always fails closed on any error. Staging additionally
    # fails closed so a misconfigured staging deployment (e.g. pointed at a
    # non-staging Supabase or Redis dependency) never starts.
    if not report.ok() and (_is_production(env) or environment == "staging"):
        # Emit a structured, value-free diagnostic before import aborts.  Cloud
        # Run may truncate Python tracebacks; this one-line record preserves the
        # exact validation codes without ever serializing configuration values.
        diagnostic = json.dumps({
            "event": "production_config_validation_failed",
            "environment": environment or "production",
            "codes": sorted({i.code for i in report.errors}),
        }, sort_keys=True)
        if on_error is not None:
            on_error(diagnostic)
        else:
            print(diagnostic, file=sys.stderr, flush=True)
        raise ProductionConfigError(report.errors)
    return report

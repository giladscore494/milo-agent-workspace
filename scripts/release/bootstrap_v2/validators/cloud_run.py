"""Cloud Run validators: container-specific, exact, fail-closed.

Environment variables are never flattened across containers; the intended
application container is identified explicitly and ambiguity blocks.
"""

from __future__ import annotations

import math
import re

from ..model import (
    CloudRunContainerState,
    CloudRunEnvVar,
    CloudRunResourceState,
    Finding,
    Severity,
    Stage,
)
from ..policy import (
    BUDGET_UPPER_BOUNDS,
    BUDGET_VARS,
    DEPRECATED_METADATA_KEYS,
    EXECUTION_FLAG_REQUIRED_VALUE,
    EXECUTION_FLAGS,
)

_NUMERIC_VERSION_RE = re.compile(r"^[1-9][0-9]*$")
_SECRET_LIKE_NAMES = frozenset(
    {
        "SUPABASE_SECRET_KEY",
        "SUPABASE_SERVICE_ROLE_KEY",
        "KIMI_API_KEY",
        "MOONSHOT_API_KEY",
        "UPSTASH_REDIS_REST_TOKEN",
    }
)


def _blocked(code: str, message: str, stage: Stage) -> Finding:
    return Finding(code=code, severity=Severity.BLOCKED, message=message, stage=stage)


def select_application_container(
    resource: CloudRunResourceState,
    expected_container_names: tuple[str, ...],
    stage: Stage,
) -> tuple[CloudRunContainerState | None, tuple[Finding, ...]]:
    """Explicitly identify the intended application container.

    Selection order: exactly one container total, or exactly one container
    whose name matches the expected names. Multiple candidates block.
    """

    containers = resource.containers
    if not containers:
        return None, (
            _blocked(
                "CLOUD_RUN_NO_CONTAINER",
                f"{resource.resource.key()} has no containers",
                stage,
            ),
        )
    if len(containers) == 1:
        return containers[0], ()

    named = tuple(c for c in containers if c.name in expected_container_names)
    if len(named) == 1:
        return named[0], ()
    return None, (
        _blocked(
            "CLOUD_RUN_AMBIGUOUS_CONTAINER",
            f"{resource.resource.key()} has {len(containers)} containers and "
            f"{len(named)} matching expected names; refusing to flatten env "
            "across containers",
            stage,
        ),
    )


def find_env_conflicts(
    container: CloudRunContainerState, stage: Stage
) -> tuple[Finding, ...]:
    """Duplicate names and plain+secret conflicts block. Never last-write-wins."""

    findings: list[Finding] = []
    seen: dict[str, CloudRunEnvVar] = {}
    plain_names: set[str] = set()
    secret_names: set[str] = set()
    for var in container.env:
        if var.name in seen:
            findings.append(
                _blocked(
                    "CLOUD_RUN_DUPLICATE_ENV",
                    f"duplicate env name {var.name!r} in container "
                    f"{container.name!r}; last-write-wins is forbidden",
                    stage,
                )
            )
        seen[var.name] = var
        if var.is_secret_ref():
            secret_names.add(var.name)
        else:
            plain_names.add(var.name)
    for name in sorted(plain_names & secret_names):
        findings.append(
            _blocked(
                "CLOUD_RUN_PLAIN_SECRET_CONFLICT",
                f"env {name!r} has both a plain and a secret definition",
                stage,
            )
        )
    return tuple(findings)


def _env_map(container: CloudRunContainerState) -> dict[str, CloudRunEnvVar]:
    return {var.name: var for var in container.env}


def validate_budgets(
    container: CloudRunContainerState, stage: Stage
) -> tuple[Finding, ...]:
    """Every budget must be present, plain, finite, numeric, > 0, in bounds."""

    findings: list[Finding] = []
    env = _env_map(container)
    for name in BUDGET_VARS:
        var = env.get(name)
        if var is None:
            findings.append(
                _blocked("BUDGET_MISSING", f"budget {name} is missing", stage)
            )
            continue
        if var.is_secret_ref():
            findings.append(
                _blocked(
                    "BUDGET_SECRET_BACKED",
                    f"budget {name} must be plain, not secret-backed",
                    stage,
                )
            )
            continue
        raw = var.value.strip()
        try:
            value = float(raw)
        except ValueError:
            findings.append(
                _blocked(
                    "BUDGET_MALFORMED", f"budget {name}={raw!r} is not numeric", stage
                )
            )
            continue
        if math.isnan(value) or math.isinf(value):
            findings.append(
                _blocked(
                    "BUDGET_NOT_FINITE", f"budget {name}={raw!r} is not finite", stage
                )
            )
            continue
        if value <= 0:
            findings.append(
                _blocked(
                    "BUDGET_NOT_POSITIVE",
                    f"budget {name}={raw!r} must be greater than zero",
                    stage,
                )
            )
            continue
        bound = BUDGET_UPPER_BOUNDS[name]
        if value > bound:
            findings.append(
                _blocked(
                    "BUDGET_ABOVE_STAGE_A_BOUND",
                    f"budget {name}={raw!r} exceeds approved upper bound {bound}",
                    stage,
                )
            )
    return tuple(findings)


def validate_execution_flags(
    container: CloudRunContainerState, stage: Stage
) -> tuple[Finding, ...]:
    """All execution flags must exist as plain vars equal to exactly 'false'."""

    findings: list[Finding] = []
    env = _env_map(container)
    for flag in EXECUTION_FLAGS + ("GATEWAY_ALLOW_EXECUTION_ROUTES",):
        var = env.get(flag)
        if var is None:
            findings.append(
                _blocked(
                    "EXECUTION_FLAG_MISSING",
                    f"execution flag {flag} is missing (missing is not false)",
                    stage,
                )
            )
            continue
        if var.is_secret_ref():
            findings.append(
                _blocked(
                    "EXECUTION_FLAG_SECRET_BACKED",
                    f"execution flag {flag} must be a plain variable",
                    stage,
                )
            )
            continue
        if var.value != EXECUTION_FLAG_REQUIRED_VALUE:
            findings.append(
                _blocked(
                    "EXECUTION_FLAG_NOT_FALSE",
                    f"execution flag {flag}={var.value!r} must equal exactly "
                    f"'{EXECUTION_FLAG_REQUIRED_VALUE}'",
                    stage,
                )
            )
    return tuple(findings)


def validate_secret_refs(
    container: CloudRunContainerState,
    expected_refs: dict[str, str],
    numeric_pin_required: tuple[str, ...],
    stage: Stage,
) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    env = _env_map(container)
    for env_name, secret_name in expected_refs.items():
        var = env.get(env_name)
        if var is None:
            findings.append(
                _blocked(
                    "SECRET_REF_MISSING",
                    f"required secret reference {env_name} is missing",
                    stage,
                )
            )
            continue
        if not var.is_secret_ref():
            findings.append(
                _blocked(
                    "SECRET_CONFIGURED_AS_PLAIN",
                    f"{env_name} must be a Secret Manager reference, found a "
                    "plain-text value",
                    stage,
                )
            )
            continue
        if var.secret_name != secret_name:
            findings.append(
                _blocked(
                    "SECRET_REF_WRONG_RESOURCE",
                    f"{env_name} references secret {var.secret_name!r}, expected "
                    f"{secret_name!r}",
                    stage,
                )
            )
        if secret_name in numeric_pin_required:
            if not _NUMERIC_VERSION_RE.match(var.secret_version):
                findings.append(
                    _blocked(
                        "SECRET_REF_PIN_NOT_NUMERIC",
                        f"{env_name} version pin {var.secret_version!r} must be an "
                        "exact numeric version (never 'latest')",
                        stage,
                    )
                )
    for name in sorted(_SECRET_LIKE_NAMES):
        var = env.get(name)
        if var is not None and not var.is_secret_ref() and var.value:
            findings.append(
                _blocked(
                    "SERVER_SECRET_AS_PLAIN_TEXT",
                    f"server secret {name} is configured as plain text",
                    stage,
                )
            )
    return tuple(findings)


def validate_deprecated_keys(
    container: CloudRunContainerState, stage: Stage
) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    env = _env_map(container)
    for name in DEPRECATED_METADATA_KEYS:
        if name in env:
            findings.append(
                _blocked(
                    "DEPRECATED_ENV_PRESENT",
                    f"deprecated variable {name} must be removed and absent",
                    stage,
                )
            )
    return tuple(findings)


def validate_resource(
    resource: CloudRunResourceState,
    expected_service_account: str,
    expected_refs: dict[str, str],
    numeric_pin_required: tuple[str, ...],
    expected_container_names: tuple[str, ...],
    stage: Stage,
    expected_bootstrap_sha: str = "",
) -> tuple[Finding, ...]:
    """Full container-specific validation of one Cloud Run resource."""

    findings: list[Finding] = []

    if resource.allows_unauthenticated:
        findings.append(
            _blocked(
                "CLOUD_RUN_UNAUTHENTICATED",
                f"{resource.resource.key()} allows unauthenticated access",
                stage,
            )
        )
    if resource.service_account != expected_service_account:
        findings.append(
            _blocked(
                "CLOUD_RUN_WRONG_SERVICE_ACCOUNT",
                f"{resource.resource.key()} runs as "
                f"{resource.service_account!r}, expected "
                f"{expected_service_account!r}",
                stage,
            )
        )

    container, selection_findings = select_application_container(
        resource, expected_container_names, stage
    )
    findings.extend(selection_findings)
    if container is None:
        return tuple(findings)

    findings.extend(find_env_conflicts(container, stage))
    findings.extend(validate_execution_flags(container, stage))
    findings.extend(validate_budgets(container, stage))
    findings.extend(validate_secret_refs(container, expected_refs, numeric_pin_required, stage))
    findings.extend(validate_deprecated_keys(container, stage))

    if expected_bootstrap_sha:
        env = _env_map(container)
        var = env.get("MILO_BOOTSTRAP_SHA")
        if var is None or var.value != expected_bootstrap_sha:
            findings.append(
                _blocked(
                    "BOOTSTRAP_SHA_MISMATCH",
                    "MILO_BOOTSTRAP_SHA does not equal the bootstrap "
                    "reconciliation identity (note: this variable never proves "
                    "the running application image)",
                    stage,
                )
            )
    return tuple(findings)

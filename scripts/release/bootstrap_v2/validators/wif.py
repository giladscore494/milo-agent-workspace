"""Workload Identity Federation validators: exactness everywhere.

Missing, partial, or invalid WIF evidence blocks before any IAM mutation.
"""

from __future__ import annotations

from ..model import Finding, Severity, Stage, WifState
from ..policy import BootstrapConfig


def _blocked(code: str, message: str, stage: Stage) -> Finding:
    return Finding(code=code, severity=Severity.BLOCKED, message=message, stage=stage)


REQUIRED_ATTRIBUTE_MAPPING: tuple[tuple[str, str], ...] = (
    ("google.subject", "assertion.sub"),
)


def verify_wif(
    state: WifState,
    config: BootstrapConfig,
    stage: Stage = Stage.GLOBAL_DISCOVERY_COMPLETE,
) -> tuple[Finding, ...]:
    findings: list[Finding] = []

    checks: tuple[tuple[str, str, str], ...] = (
        ("WIF_POOL_MISMATCH", state.pool_id, config.wif_pool_id),
        ("WIF_PROVIDER_MISMATCH", state.provider_id, config.wif_provider_id),
        ("WIF_ISSUER_MISMATCH", state.issuer_uri, config.wif_issuer),
        (
            "WIF_CONDITION_MISMATCH",
            state.attribute_condition,
            config.wif_attribute_condition,
        ),
    )
    for code, actual, expected in checks:
        if actual != expected:
            findings.append(
                _blocked(
                    code,
                    f"live value {actual!r} does not exactly equal expected "
                    f"{expected!r}",
                    stage,
                )
            )

    if tuple(state.allowed_audiences) != (config.wif_allowed_audience,):
        findings.append(
            _blocked(
                "WIF_AUDIENCE_MISMATCH",
                f"allowed audiences {list(state.allowed_audiences)!r} must be "
                f"exactly [{config.wif_allowed_audience!r}]",
                stage,
            )
        )

    mapping = dict(state.attribute_mapping)
    for key, expected_value in REQUIRED_ATTRIBUTE_MAPPING:
        if mapping.get(key) != expected_value:
            findings.append(
                _blocked(
                    "WIF_ATTRIBUTE_MAPPING_MISMATCH",
                    f"attribute mapping {key}={mapping.get(key)!r} must equal "
                    f"{expected_value!r}",
                    stage,
                )
            )

    for label, value in (("pool", state.pool_state), ("provider", state.provider_state)):
        if value and value.upper() != "ACTIVE":
            findings.append(
                _blocked(
                    "WIF_NOT_ACTIVE",
                    f"wif {label} state is {value!r}, expected ACTIVE",
                    stage,
                )
            )

    return tuple(findings)


def expected_principal_set(project_number: str, pool_id: str, vercel_project_id: str) -> str:
    return (
        "principalSet://iam.googleapis.com/projects/"
        f"{project_number}/locations/global/workloadIdentityPools/"
        f"{pool_id}/attribute.project/{vercel_project_id}"
    )

"""IAM policy validators: exact role / member / condition equality.

Same-role bindings with different conditions are never merged. Unexpected
accessors are blocking, never warnings. Broad principals are always
blocking.
"""

from __future__ import annotations

from ..model import Finding, IamBinding, IamPolicyState, Severity, Stage
from ..policy import FORBIDDEN_IAM_PRINCIPALS


def _blocked(code: str, message: str, stage: Stage) -> Finding:
    return Finding(code=code, severity=Severity.BLOCKED, message=message, stage=stage)


def bindings_for_role(
    policy: IamPolicyState, role: str
) -> tuple[IamBinding, ...]:
    """All bindings for a role, kept separate per condition (never merged)."""
    return tuple(b for b in policy.bindings if b.role == role)


def find_forbidden_principals(policy: IamPolicyState, stage: Stage) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    for binding in policy.bindings:
        for member in binding.members:
            if member in FORBIDDEN_IAM_PRINCIPALS:
                findings.append(
                    _blocked(
                        "IAM_BROAD_PRINCIPAL",
                        f"forbidden broad principal {member!r} present in "
                        f"{binding.role} on {policy.resource.key()}",
                        stage,
                    )
                )
    return tuple(findings)


def verify_exact_policy(
    policy: IamPolicyState,
    role: str,
    expected_members: tuple[str, ...],
    stage: Stage,
    expected_condition_expression: str = "",
) -> tuple[Finding, ...]:
    """Verify one role has exactly one binding with the exact member set and
    exact condition, and that no unrelated same-role binding exists."""

    findings: list[Finding] = list(find_forbidden_principals(policy, stage))
    role_bindings = bindings_for_role(policy, role)

    if not role_bindings:
        if expected_members:
            findings.append(
                _blocked(
                    "IAM_BINDING_MISSING",
                    f"no {role} binding on {policy.resource.key()}",
                    stage,
                )
            )
        return tuple(findings)

    if len(role_bindings) != 1:
        findings.append(
            _blocked(
                "IAM_BINDING_COUNT",
                f"expected exactly one {role} binding on {policy.resource.key()}, "
                f"found {len(role_bindings)} (conditions are never merged)",
                stage,
            )
        )
        return tuple(findings)

    binding = role_bindings[0]
    if binding.condition_expression != expected_condition_expression:
        findings.append(
            _blocked(
                "IAM_CONDITION_MISMATCH",
                f"{role} binding condition {binding.condition_expression!r} does "
                f"not equal expected {expected_condition_expression!r}",
                stage,
            )
        )

    expected = set(expected_members)
    actual = set(binding.members)
    unexpected = sorted(actual - expected)
    missing = sorted(expected - actual)
    if unexpected:
        findings.append(
            _blocked(
                "IAM_UNEXPECTED_MEMBER",
                f"unexpected {role} members on {policy.resource.key()}: "
                f"{', '.join(unexpected)} (unexpected accessors block, they are "
                "not warnings)",
                stage,
            )
        )
    if missing:
        findings.append(
            _blocked(
                "IAM_MISSING_MEMBER",
                f"missing {role} members on {policy.resource.key()}: "
                f"{', '.join(missing)}",
                stage,
            )
        )
    return tuple(findings)


def verify_secret_accessors(
    policy: IamPolicyState,
    intended_members: tuple[str, ...],
    stage: Stage,
) -> tuple[Finding, ...]:
    """Per-secret accessor policy: exactly the intended consumers."""
    findings = list(
        verify_exact_policy(
            policy,
            "roles/secretmanager.secretAccessor",
            intended_members,
            stage,
        )
    )
    # Any other role granted directly on a secret is unexpected surface.
    for binding in policy.bindings:
        if binding.role != "roles/secretmanager.secretAccessor":
            findings.append(
                _blocked(
                    "IAM_UNEXPECTED_SECRET_ROLE",
                    f"unexpected role {binding.role} granted directly on "
                    f"{policy.resource.key()}",
                    stage,
                )
            )
    return tuple(findings)

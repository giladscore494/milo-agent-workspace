"""Redis identity validators.

Fingerprints are always the full lowercase SHA-256 (64 hexadecimal
characters). Truncated fingerprints are rejected everywhere. A fingerprint
is consistency evidence, not authentication.
"""

from __future__ import annotations

import hashlib
import re

from ..model import Finding, ProbeOutcome, RedisIdentity, Severity, Stage, UpstashDatabaseState

FULL_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_NUMERIC_VERSION_RE = re.compile(r"^[1-9][0-9]*$")


def fingerprint_sha256(payload: bytes | str) -> str:
    data = payload.encode("utf-8") if isinstance(payload, str) else payload
    return hashlib.sha256(data).hexdigest()


def is_full_fingerprint(value: str) -> bool:
    return bool(FULL_FINGERPRINT_RE.match(value))


def _blocked(code: str, message: str, stage: Stage) -> Finding:
    return Finding(code=code, severity=Severity.BLOCKED, message=message, stage=stage)


def select_database(
    databases: tuple[UpstashDatabaseState, ...],
    expected_id: str,
    expected_name: str,
    stage: Stage = Stage.GLOBAL_DISCOVERY_COMPLETE,
) -> tuple[UpstashDatabaseState | None, tuple[Finding, ...]]:
    """Select exactly one database by exact ID or exact case-sensitive name.

    Ambiguity is blocking; arbitrary selection is forbidden.
    """

    findings: list[Finding] = []
    if expected_id:
        matches = tuple(db for db in databases if db.database_id == expected_id)
        if len(matches) == 1:
            return matches[0], ()
        if len(matches) > 1:
            findings.append(
                _blocked(
                    "REDIS_DUPLICATE_ID",
                    f"multiple databases returned for id {expected_id}",
                    stage,
                )
            )
            return None, tuple(findings)
        findings.append(
            _blocked(
                "REDIS_ID_NOT_FOUND",
                f"expected database id {expected_id} not present in list response",
                stage,
            )
        )
        return None, tuple(findings)

    matches = tuple(db for db in databases if db.name == expected_name)
    if len(matches) > 1:
        findings.append(
            _blocked(
                "REDIS_AMBIGUOUS_NAME",
                f"database name {expected_name!r} is ambiguous "
                f"({len(matches)} matches); refusing arbitrary selection",
                stage,
            )
        )
        return None, tuple(findings)
    if not matches:
        return None, ()
    return matches[0], ()


def verify_database_detail(
    selected: UpstashDatabaseState,
    detail: UpstashDatabaseState,
    stage: Stage,
) -> tuple[Finding, ...]:
    """List/detail equality: every identity field must agree exactly."""

    findings: list[Finding] = []
    if detail.database_id != selected.database_id:
        findings.append(
            _blocked(
                "REDIS_LIST_DETAIL_ID_MISMATCH",
                "detail response returned a different database id than the list "
                f"selection ({detail.database_id!r} != {selected.database_id!r})",
                stage,
            )
        )
    if detail.name != selected.name:
        findings.append(
            _blocked(
                "REDIS_LIST_DETAIL_NAME_MISMATCH",
                "detail and list responses disagree on database name",
                stage,
            )
        )
    if detail.state.lower() != "active":
        findings.append(
            _blocked(
                "REDIS_NOT_ACTIVE",
                f"database state is {detail.state!r}, expected active",
                stage,
            )
        )
    if detail.tls is not True:
        findings.append(
            _blocked("REDIS_TLS_NOT_TRUE", "database TLS must be exactly true", stage)
        )
    if not detail.endpoint:
        findings.append(
            _blocked("REDIS_ENDPOINT_MISSING", "database endpoint missing", stage)
        )
    if not detail.rest_url.startswith("https://") or (
        detail.endpoint and detail.endpoint not in detail.rest_url
    ):
        findings.append(
            _blocked(
                "REDIS_REST_URL_NOT_CANONICAL",
                "rest url is not the canonical https url of the database endpoint",
                stage,
            )
        )
    return tuple(findings)


def verify_redis_identity_coherence(
    identity: RedisIdentity,
    source_database: UpstashDatabaseState,
    stage: Stage = Stage.GLOBAL_DISCOVERY_COMPLETE,
) -> tuple[Finding, ...]:
    """One coherent identity: every field originates from the same database."""

    findings: list[Finding] = []

    if identity.database_id != source_database.database_id:
        findings.append(
            _blocked(
                "REDIS_IDENTITY_MIXED_DB_ID",
                "identity database id does not match the selected database",
                stage,
            )
        )
    if identity.database_name != source_database.name:
        findings.append(
            _blocked(
                "REDIS_IDENTITY_MIXED_NAME",
                "identity database name does not match the selected database",
                stage,
            )
        )
    if identity.rest_url != source_database.rest_url:
        findings.append(
            _blocked(
                "REDIS_IDENTITY_MIXED_URL",
                "identity rest url does not match the selected database",
                stage,
            )
        )
    for label, value in (
        ("token_fingerprint_sha256", identity.token_fingerprint_sha256),
        ("vercel_value_fingerprint_sha256", identity.vercel_value_fingerprint_sha256),
    ):
        if not is_full_fingerprint(value):
            findings.append(
                _blocked(
                    "REDIS_FINGERPRINT_NOT_FULL",
                    f"{label} must be a full lowercase sha-256 (64 hex chars)",
                    stage,
                )
            )
    if identity.vercel_value_fingerprint_sha256 and is_full_fingerprint(
        identity.vercel_value_fingerprint_sha256
    ) and identity.vercel_value_fingerprint_sha256 != identity.token_fingerprint_sha256:
        findings.append(
            _blocked(
                "REDIS_VERCEL_FINGERPRINT_MISMATCH",
                "vercel token fingerprint differs from the secret manager token "
                "fingerprint; fields originate from different tokens",
                stage,
            )
        )
    if not identity.secret_resource_name:
        findings.append(
            _blocked(
                "REDIS_SECRET_RESOURCE_MISSING",
                "secret manager resource name missing from redis identity",
                stage,
            )
        )
    for label, version in (
        ("enabled_secret_version", identity.enabled_secret_version),
        ("api_secret_version_pin", identity.api_secret_version_pin),
        ("worker_secret_version_pin", identity.worker_secret_version_pin),
    ):
        if not _NUMERIC_VERSION_RE.match(version):
            findings.append(
                _blocked(
                    "REDIS_VERSION_NOT_NUMERIC",
                    f"{label} must be an exact numeric version, got {version!r}",
                    stage,
                )
            )
    if (
        identity.api_secret_version_pin != identity.enabled_secret_version
        or identity.worker_secret_version_pin != identity.enabled_secret_version
    ):
        findings.append(
            _blocked(
                "REDIS_VERSION_PIN_MISMATCH",
                "api/worker secret pins must equal the enabled secret version",
                stage,
            )
        )
    if identity.logical_environment != "production":
        findings.append(
            _blocked(
                "REDIS_LOGICAL_ENV_WRONG",
                "logical environment must be exactly 'production'",
                stage,
            )
        )
    return tuple(findings)


def classify_absence(outcome: ProbeOutcome) -> bool:
    """Only a positively identified not-found state is absence."""
    return outcome is ProbeOutcome.CLEANLY_ABSENT

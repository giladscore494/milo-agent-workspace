"""Construction of the single authoritative RunResult.

Status derivation rules (Phase 14):

- a failed audit cannot be ``AUDITED``;
- a partial apply cannot be ``APPLIED``;
- a metadata failure cannot be ``APPLIED``;
- unverified live state cannot be ``APPLIED``;
- any blocker produces a nonzero exit.

The RunResult built here is the only source of process exit status, human
summary, JSON report, metadata eligibility, and workflow conclusion.
"""

from __future__ import annotations

from .model import (
    Finding,
    MetadataStatus,
    Mode,
    MutationRecord,
    PostWriteVerification,
    ReadOperation,
    RecoveryStep,
    ResourceIdentity,
    RunResult,
    RunStatus,
    Severity,
    Stage,
)


class ResultIntegrityError(Exception):
    """Raised when a caller attempts to construct a dishonest RunResult."""


def _any_blocker(
    findings: tuple[Finding, ...],
    mutations: tuple[MutationRecord, ...],
    verifications: tuple[PostWriteVerification, ...],
    mode: Mode,
) -> bool:
    if any(f.severity is Severity.BLOCKED for f in findings):
        return True
    if mode is Mode.APPLY and any(f.blocks_apply() for f in findings):
        return True
    if any(m.executed and not m.succeeded for m in mutations):
        return True
    if any(not m.declared for m in mutations):
        return True
    if any(not v.verified for v in verifications):
        return True
    return False


def derive_status(
    mode: Mode,
    last_completed_stage: Stage,
    findings: tuple[Finding, ...],
    mutations: tuple[MutationRecord, ...],
    verifications: tuple[PostWriteVerification, ...],
    metadata_status: MetadataStatus,
) -> RunStatus:
    """Derive the only truthful final status for a run."""

    blocker = _any_blocker(findings, mutations, verifications, mode)
    executed_writes = any(m.executed for m in mutations)

    if mode is Mode.PLAN:
        return RunStatus.BLOCKED if blocker else RunStatus.PLANNED

    if mode is Mode.AUDIT:
        if blocker or last_completed_stage is not Stage.COMPLETE:
            return RunStatus.BLOCKED
        return RunStatus.AUDITED

    # APPLY
    if blocker or last_completed_stage is not Stage.COMPLETE:
        return RunStatus.PARTIAL if executed_writes else RunStatus.BLOCKED
    if metadata_status is not MetadataStatus.COMMITTED:
        # Metadata failure cannot be APPLIED.
        return RunStatus.PARTIAL if executed_writes else RunStatus.BLOCKED
    return RunStatus.APPLIED


def build_run_result(
    mode: Mode,
    starting_sha: str,
    trusted_ref: str,
    plan_digest: str,
    last_completed_stage: Stage,
    findings: tuple[Finding, ...] = (),
    reads: tuple[ReadOperation, ...] = (),
    mutations: tuple[MutationRecord, ...] = (),
    verifications: tuple[PostWriteVerification, ...] = (),
    created_resources: tuple[ResourceIdentity, ...] = (),
    recovery_steps: tuple[RecoveryStep, ...] = (),
    metadata_status: MetadataStatus = MetadataStatus.NOT_APPLICABLE,
) -> RunResult:
    """Build the authoritative RunResult, refusing dishonest combinations."""

    status = derive_status(
        mode,
        last_completed_stage,
        findings,
        mutations,
        verifications,
        metadata_status,
    )

    blocker = _any_blocker(findings, mutations, verifications, mode)
    if status is RunStatus.AUDITED and blocker:
        raise ResultIntegrityError("a failed audit cannot be AUDITED")
    if status is RunStatus.APPLIED and blocker:
        raise ResultIntegrityError("a run with blockers cannot be APPLIED")
    if status is RunStatus.APPLIED and metadata_status is not MetadataStatus.COMMITTED:
        raise ResultIntegrityError("a metadata failure cannot be APPLIED")
    if metadata_status is MetadataStatus.COMMITTED and status is not RunStatus.APPLIED:
        raise ResultIntegrityError(
            "metadata may be committed only by a fully applied run"
        )
    if status in (RunStatus.PARTIAL, RunStatus.BLOCKED) and (
        metadata_status is MetadataStatus.COMMITTED
    ):
        raise ResultIntegrityError("metadata cannot be generated after a partial run")

    return RunResult(
        mode=mode,
        status=status,
        starting_sha=starting_sha,
        trusted_ref=trusted_ref,
        plan_digest=plan_digest,
        last_completed_stage=last_completed_stage,
        findings=findings,
        reads=reads,
        mutations=mutations,
        verifications=verifications,
        created_resources=created_resources,
        recovery_steps=recovery_steps,
        metadata_status=metadata_status,
    )

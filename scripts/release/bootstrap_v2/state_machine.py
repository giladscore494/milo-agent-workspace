"""Monotonic state machine for bootstrap v2.

The state machine is the only component allowed to coordinate provider
operations. Stages advance strictly in ``STAGE_ORDER``; any critical failure
transitions immediately and irrevocably (within the run) to
``Stage.BLOCKED``. Recovery from BLOCKED happens only through a fresh run
with fresh discovery.
"""

from __future__ import annotations

from .model import (
    Finding,
    Mode,
    MutationPlan,
    MutationRecord,
    ProbeOutcome,
    Severity,
    Stage,
    StageResult,
)

STAGE_ORDER: tuple[Stage, ...] = (
    Stage.INITIAL,
    Stage.LOCAL_GUARD_VERIFIED,
    Stage.GLOBAL_DISCOVERY_COMPLETE,
    Stage.PLAN_FROZEN,
    Stage.APPLY_AUTHORIZED,
    Stage.UPSTASH_STAGE_VERIFIED,
    Stage.GCP_IDENTITY_SECRET_STAGE_VERIFIED,
    Stage.IAM_STAGE_VERIFIED,
    Stage.CLOUD_RUN_STAGE_VERIFIED,
    Stage.VERCEL_STAGE_VERIFIED,
    Stage.FINAL_AUDIT_VERIFIED,
    Stage.METADATA_COMMITTED,
    Stage.COMPLETE,
)

_STAGE_INDEX = {stage: index for index, stage in enumerate(STAGE_ORDER)}

#: Read outcomes that constitute a failed critical read.
_FAILED_READ_OUTCOMES = frozenset(
    {
        ProbeOutcome.PERMISSION_DENIED,
        ProbeOutcome.AUTH_FAILURE,
        ProbeOutcome.API_DISABLED,
        ProbeOutcome.RATE_LIMITED,
        ProbeOutcome.NETWORK_FAILURE,
        ProbeOutcome.MALFORMED_OUTPUT,
        ProbeOutcome.TIMEOUT,
        ProbeOutcome.UNKNOWN_ERROR,
    }
)


class StageBlocked(Exception):
    """Raised by the gate when a stage result is not clean.

    Carries the machine-readable reasons so the engine can convert them into
    findings on the single authoritative RunResult.
    """

    def __init__(self, stage: Stage, reasons: tuple[str, ...]) -> None:
        self.stage = stage
        self.reasons = reasons
        super().__init__(f"stage {stage.value} blocked: " + "; ".join(reasons))


class IllegalTransition(Exception):
    pass


def stage_clean_reasons(
    stage_result: StageResult,
    mode: Mode,
    frozen_plan: MutationPlan | None = None,
) -> tuple[str, ...]:
    """Pure evaluation of every blocking condition for one stage result."""

    reasons: list[str] = []

    for read in stage_result.reads:
        if read.critical and read.outcome in _FAILED_READ_OUTCOMES:
            reasons.append(
                f"critical read failed ({read.outcome.value}): {read.description}"
            )

    for finding in stage_result.findings:
        if finding.severity is Severity.BLOCKED:
            reasons.append(f"blocking finding {finding.code}: {finding.message}")
        elif mode is Mode.APPLY and finding.blocks_apply():
            reasons.append(
                "critical finding not clean during apply "
                f"({finding.severity.value}"
                f"{', manual' if finding.requires_manual else ''}"
                f"{', unknown' if finding.unknown else ''})"
                f" {finding.code}: {finding.message}"
            )

    for record in stage_result.mutations:
        if not record.declared:
            reasons.append(
                f"undeclared write in mutation ledger: {record.idempotency_key}"
            )
        if record.executed and not record.succeeded:
            reasons.append(f"mutation failed: {record.idempotency_key}")
        if frozen_plan is not None and record.idempotency_key not in frozen_plan.operation_keys():
            reasons.append(
                f"mutation absent from frozen plan: {record.idempotency_key}"
            )

    for verification in stage_result.verifications:
        if not verification.verified:
            reasons.append(
                f"post-write verification failed: {verification.idempotency_key}"
                + (f" ({verification.detail})" if verification.detail else "")
            )

    return tuple(reasons)


def require_stage_clean(
    stage_result: StageResult,
    mode: Mode = Mode.APPLY,
    frozen_plan: MutationPlan | None = None,
) -> None:
    """The single mandatory gate between stages.

    Raises :class:`StageBlocked` when the stage result contains any critical
    read failure; missing, malformed, ambiguous or stale evidence (surfaced
    as blocking findings by validators); inexact identity; a critical WARN /
    MANUAL / UNKNOWN / UNVERIFIED finding during apply; a failed mutation; a
    failed post-write verification; drift from the frozen plan; or an
    undeclared write in the mutation ledger.
    """

    reasons = stage_clean_reasons(stage_result, mode, frozen_plan)
    if reasons:
        raise StageBlocked(stage_result.stage, reasons)


class BootstrapStateMachine:
    """Monotonic stage tracker. BLOCKED is terminal for the run."""

    def __init__(self) -> None:
        self._current: Stage = Stage.INITIAL
        self._history: list[Stage] = [Stage.INITIAL]

    @property
    def current(self) -> Stage:
        return self._current

    @property
    def history(self) -> tuple[Stage, ...]:
        return tuple(self._history)

    @property
    def blocked(self) -> bool:
        return self._current is Stage.BLOCKED

    def last_completed_stage(self) -> Stage:
        if self._current is Stage.BLOCKED:
            for stage in reversed(self._history):
                if stage is not Stage.BLOCKED:
                    return stage
        return self._current

    def block(self) -> None:
        if self._current is Stage.COMPLETE:
            raise IllegalTransition("cannot block a completed run")
        self._current = Stage.BLOCKED
        self._history.append(Stage.BLOCKED)

    def advance(self, to_stage: Stage) -> None:
        if self._current is Stage.BLOCKED:
            raise IllegalTransition(
                "run is BLOCKED; no later stage can start in this run"
            )
        if to_stage is Stage.BLOCKED:
            self.block()
            return
        if to_stage not in _STAGE_INDEX:
            raise IllegalTransition(f"unknown stage {to_stage!r}")
        expected_index = _STAGE_INDEX[self._current] + 1
        if _STAGE_INDEX[to_stage] != expected_index:
            raise IllegalTransition(
                f"illegal transition {self._current.value} -> {to_stage.value};"
                f" expected {STAGE_ORDER[expected_index].value}"
                if expected_index < len(STAGE_ORDER)
                else f"illegal transition from terminal stage {self._current.value}"
            )
        self._current = to_stage
        self._history.append(to_stage)

    def complete_stage(
        self,
        stage_result: StageResult,
        to_stage: Stage,
        mode: Mode,
        frozen_plan: MutationPlan | None = None,
    ) -> None:
        """Gate then advance; on any unclean condition, transition to BLOCKED."""

        try:
            require_stage_clean(stage_result, mode, frozen_plan)
        except StageBlocked:
            self.block()
            raise
        self.advance(to_stage)


def blocked_finding(stage: Stage, code: str, message: str) -> Finding:
    return Finding(
        code=code,
        severity=Severity.BLOCKED,
        message=message,
        stage=stage,
        critical=True,
    )

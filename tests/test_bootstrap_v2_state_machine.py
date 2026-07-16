"""Pure tests for the monotonic state machine and the single gate."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts" / "release"))

from bootstrap_v2.model import (  # noqa: E402
    Finding,
    Mode,
    MutationPlan,
    MutationRecord,
    OperationType,
    PostWriteVerification,
    ProbeOutcome,
    Provider,
    ReadOperation,
    ResourceIdentity,
    Severity,
    Stage,
    StageResult,
)
from bootstrap_v2.state_machine import (  # noqa: E402
    STAGE_ORDER,
    BootstrapStateMachine,
    IllegalTransition,
    StageBlocked,
    require_stage_clean,
    stage_clean_reasons,
)

RES = ResourceIdentity(provider=Provider.GCP, kind="secret", name="X", scope="p")
STAGE = Stage.GLOBAL_DISCOVERY_COMPLETE


def test_full_legal_walk_reaches_complete():
    machine = BootstrapStateMachine()
    for stage in STAGE_ORDER[1:]:
        machine.advance(stage)
    assert machine.current is Stage.COMPLETE
    assert machine.last_completed_stage() is Stage.COMPLETE


def test_skipping_a_stage_is_illegal():
    machine = BootstrapStateMachine()
    machine.advance(Stage.LOCAL_GUARD_VERIFIED)
    with pytest.raises(IllegalTransition):
        machine.advance(Stage.PLAN_FROZEN)


def test_going_backwards_is_illegal():
    machine = BootstrapStateMachine()
    machine.advance(Stage.LOCAL_GUARD_VERIFIED)
    machine.advance(Stage.GLOBAL_DISCOVERY_COMPLETE)
    with pytest.raises(IllegalTransition):
        machine.advance(Stage.LOCAL_GUARD_VERIFIED)


def test_blocked_is_terminal_for_the_run():
    machine = BootstrapStateMachine()
    machine.advance(Stage.LOCAL_GUARD_VERIFIED)
    machine.block()
    assert machine.blocked
    for stage in STAGE_ORDER:
        with pytest.raises(IllegalTransition):
            machine.advance(stage)
    assert machine.last_completed_stage() is Stage.LOCAL_GUARD_VERIFIED


def test_complete_cannot_be_blocked():
    machine = BootstrapStateMachine()
    for stage in STAGE_ORDER[1:]:
        machine.advance(stage)
    with pytest.raises(IllegalTransition):
        machine.block()


def test_gate_blocks_critical_read_failure():
    for outcome in (
        ProbeOutcome.PERMISSION_DENIED,
        ProbeOutcome.AUTH_FAILURE,
        ProbeOutcome.API_DISABLED,
        ProbeOutcome.RATE_LIMITED,
        ProbeOutcome.NETWORK_FAILURE,
        ProbeOutcome.MALFORMED_OUTPUT,
        ProbeOutcome.TIMEOUT,
        ProbeOutcome.UNKNOWN_ERROR,
    ):
        result = StageResult(
            stage=STAGE,
            reads=(
                ReadOperation(
                    sequence=1,
                    provider=Provider.GCP,
                    description="d",
                    outcome=outcome,
                ),
            ),
        )
        with pytest.raises(StageBlocked):
            require_stage_clean(result, Mode.PLAN)


def test_gate_passes_decisive_reads():
    result = StageResult(
        stage=STAGE,
        reads=(
            ReadOperation(
                sequence=1,
                provider=Provider.GCP,
                description="d",
                outcome=ProbeOutcome.CLEANLY_ABSENT,
            ),
        ),
    )
    require_stage_clean(result, Mode.APPLY)


def test_gate_blocks_manual_and_unknown_only_during_apply():
    manual = Finding(
        code="M",
        severity=Severity.WARN,
        message="manual step",
        stage=STAGE,
        critical=True,
        requires_manual=True,
    )
    result = StageResult(stage=STAGE, findings=(manual,))
    require_stage_clean(result, Mode.PLAN)  # plan tolerates
    with pytest.raises(StageBlocked):
        require_stage_clean(result, Mode.APPLY)


def test_gate_blocks_failed_mutation():
    record = MutationRecord(
        sequence=1,
        provider=Provider.GCP,
        operation_type=OperationType.GCP_CREATE_SECRET,
        resource=RES,
        idempotency_key="k",
        declared=True,
        executed=True,
        succeeded=False,
    )
    with pytest.raises(StageBlocked):
        require_stage_clean(StageResult(stage=STAGE, mutations=(record,)), Mode.APPLY)


def test_gate_blocks_undeclared_write():
    record = MutationRecord(
        sequence=1,
        provider=Provider.GCP,
        operation_type=OperationType.GCP_CREATE_SECRET,
        resource=RES,
        idempotency_key="k",
        declared=False,
        executed=False,
        succeeded=False,
    )
    reasons = stage_clean_reasons(
        StageResult(stage=STAGE, mutations=(record,)), Mode.APPLY
    )
    assert any("undeclared" in reason for reason in reasons)


def test_gate_blocks_mutation_absent_from_frozen_plan():
    record = MutationRecord(
        sequence=1,
        provider=Provider.GCP,
        operation_type=OperationType.GCP_CREATE_SECRET,
        resource=RES,
        idempotency_key="not-in-plan",
        declared=True,
        executed=True,
        succeeded=True,
    )
    empty_plan = MutationPlan(mode=Mode.APPLY, bootstrap_sha="a" * 40)
    with pytest.raises(StageBlocked):
        require_stage_clean(
            StageResult(stage=STAGE, mutations=(record,)), Mode.APPLY, empty_plan
        )


def test_gate_blocks_failed_post_write_verification():
    verification = PostWriteVerification(
        idempotency_key="k",
        verified=False,
        observed_post_state_digest="a",
        expected_post_state_digest="b",
    )
    with pytest.raises(StageBlocked):
        require_stage_clean(
            StageResult(stage=STAGE, verifications=(verification,)), Mode.APPLY
        )


def test_complete_stage_blocks_machine_on_unclean_result():
    machine = BootstrapStateMachine()
    bad = StageResult(
        stage=Stage.LOCAL_GUARD_VERIFIED,
        findings=(
            Finding(
                code="B",
                severity=Severity.BLOCKED,
                message="m",
                stage=Stage.LOCAL_GUARD_VERIFIED,
            ),
        ),
    )
    with pytest.raises(StageBlocked):
        machine.complete_stage(bad, Stage.LOCAL_GUARD_VERIFIED, Mode.PLAN)
    assert machine.blocked

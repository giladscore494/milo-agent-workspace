"""Pure tests for the bootstrap v2 typed domain model and RunResult rules."""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts" / "release"))

from bootstrap_v2.model import (  # noqa: E402
    DECISIVE_OUTCOMES,
    Finding,
    METADATA_V3_KEYS,
    MetadataStatus,
    Mode,
    MutationRecord,
    OperationType,
    PostWriteVerification,
    ProbeOutcome,
    Provider,
    ResourceIdentity,
    RunStatus,
    Severity,
    Stage,
)
from bootstrap_v2.result import (  # noqa: E402
    ResultIntegrityError,
    build_run_result,
    derive_status,
)

RES = ResourceIdentity(provider=Provider.GCP, kind="secret", name="X", scope="p")


def finding(severity=Severity.BLOCKED, critical=True, manual=False, unknown=False):
    return Finding(
        code="T",
        severity=severity,
        message="m",
        stage=Stage.GLOBAL_DISCOVERY_COMPLETE,
        critical=critical,
        requires_manual=manual,
        unknown=unknown,
    )


def mutation(executed=True, succeeded=True, declared=True, seq=1):
    return MutationRecord(
        sequence=seq,
        provider=Provider.GCP,
        operation_type=OperationType.GCP_CREATE_SECRET,
        resource=RES,
        idempotency_key="k",
        declared=declared,
        executed=executed,
        succeeded=succeeded,
    )


def test_models_are_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        RES.name = "other"  # type: ignore[misc]
    f = finding()
    with pytest.raises(dataclasses.FrozenInstanceError):
        f.severity = Severity.PASS  # type: ignore[misc]


def test_only_positive_not_found_is_absence():
    assert ProbeOutcome.CLEANLY_ABSENT in DECISIVE_OUTCOMES
    assert ProbeOutcome.PRESENT in DECISIVE_OUTCOMES
    for outcome in ProbeOutcome:
        if outcome not in DECISIVE_OUTCOMES:
            assert outcome not in (ProbeOutcome.PRESENT, ProbeOutcome.CLEANLY_ABSENT)
    assert ProbeOutcome.PERMISSION_DENIED not in DECISIVE_OUTCOMES
    assert ProbeOutcome.NETWORK_FAILURE not in DECISIVE_OUTCOMES
    assert ProbeOutcome.MALFORMED_OUTPUT not in DECISIVE_OUTCOMES


@pytest.mark.parametrize(
    "severity,manual,unknown,expected",
    [
        (Severity.PASS, False, False, False),
        (Severity.INFO, False, False, False),
        (Severity.WARN, False, False, True),
        (Severity.BLOCKED, False, False, True),
        (Severity.UNVERIFIED, False, False, True),
        (Severity.PASS, True, False, True),
        (Severity.INFO, False, True, True),
    ],
)
def test_apply_has_no_soft_critical_findings(severity, manual, unknown, expected):
    assert finding(severity, True, manual, unknown).blocks_apply() is expected


def test_noncritical_findings_never_block_apply():
    assert not finding(Severity.WARN, critical=False).blocks_apply()
    assert not finding(Severity.UNVERIFIED, critical=False, manual=True).blocks_apply()


def test_metadata_v3_keys_are_closed_and_complete():
    assert len(METADATA_V3_KEYS) == 32
    assert "MILO_RELEASE_SHA" not in METADATA_V3_KEYS
    assert "UPSTASH_REDIS_REST_TOKEN" not in METADATA_V3_KEYS
    for required in (
        "MILO_METADATA_SCHEMA_VERSION",
        "MILO_PLAN_DIGEST",
        "MILO_REDIS_DB_ID",
        "MILO_REDIS_TOKEN_FINGERPRINT",
        "MILO_REDIS_SECRET_VERSION",
        "GATEWAY_IDENTITY",
    ):
        assert required in METADATA_V3_KEYS


def test_derive_status_plan():
    assert derive_status(Mode.PLAN, Stage.PLAN_FROZEN, (), (), (), MetadataStatus.NOT_APPLICABLE) is RunStatus.PLANNED
    assert (
        derive_status(Mode.PLAN, Stage.PLAN_FROZEN, (finding(),), (), (), MetadataStatus.NOT_APPLICABLE)
        is RunStatus.BLOCKED
    )


def test_failed_audit_cannot_be_audited():
    status = derive_status(
        Mode.AUDIT, Stage.COMPLETE, (finding(),), (), (), MetadataStatus.NOT_APPLICABLE
    )
    assert status is RunStatus.BLOCKED
    status = derive_status(
        Mode.AUDIT, Stage.GLOBAL_DISCOVERY_COMPLETE, (), (), (), MetadataStatus.NOT_APPLICABLE
    )
    assert status is RunStatus.BLOCKED


def test_partial_apply_cannot_be_applied():
    status = derive_status(
        Mode.APPLY,
        Stage.IAM_STAGE_VERIFIED,
        (),
        (mutation(succeeded=False),),
        (),
        MetadataStatus.WITHHELD,
    )
    assert status is RunStatus.PARTIAL


def test_metadata_failure_cannot_be_applied():
    status = derive_status(
        Mode.APPLY, Stage.COMPLETE, (), (mutation(),), (), MetadataStatus.WITHHELD
    )
    assert status is RunStatus.PARTIAL


def test_unverified_live_state_cannot_be_applied():
    bad_verification = PostWriteVerification(
        idempotency_key="k",
        verified=False,
        observed_post_state_digest="a",
        expected_post_state_digest="b",
    )
    status = derive_status(
        Mode.APPLY,
        Stage.COMPLETE,
        (),
        (mutation(),),
        (bad_verification,),
        MetadataStatus.COMMITTED,
    )
    assert status is RunStatus.PARTIAL


def test_undeclared_write_is_a_blocker():
    result = build_run_result(
        mode=Mode.APPLY,
        starting_sha="a" * 40,
        trusted_ref="ref",
        plan_digest="d" * 64,
        last_completed_stage=Stage.IAM_STAGE_VERIFIED,
        mutations=(mutation(declared=False, executed=False, succeeded=False),),
        metadata_status=MetadataStatus.WITHHELD,
    )
    assert result.has_blocker()
    assert result.exit_code() != 0


def test_any_blocker_produces_nonzero_exit():
    result = build_run_result(
        mode=Mode.PLAN,
        starting_sha="a" * 40,
        trusted_ref="ref",
        plan_digest="",
        last_completed_stage=Stage.GLOBAL_DISCOVERY_COMPLETE,
        findings=(finding(),),
    )
    assert result.status is RunStatus.BLOCKED
    assert result.exit_code() == 1


def test_committed_metadata_requires_applied_status():
    with pytest.raises(ResultIntegrityError):
        build_run_result(
            mode=Mode.APPLY,
            starting_sha="a" * 40,
            trusted_ref="ref",
            plan_digest="d" * 64,
            last_completed_stage=Stage.IAM_STAGE_VERIFIED,
            mutations=(mutation(succeeded=False),),
            metadata_status=MetadataStatus.COMMITTED,
        )


def test_metadata_eligibility_only_for_clean_apply():
    applied = build_run_result(
        mode=Mode.APPLY,
        starting_sha="a" * 40,
        trusted_ref="ref",
        plan_digest="d" * 64,
        last_completed_stage=Stage.COMPLETE,
        mutations=(mutation(),),
        metadata_status=MetadataStatus.COMMITTED,
    )
    assert applied.status is RunStatus.APPLIED
    assert applied.metadata_eligible()
    assert applied.exit_code() == 0

"""Full fault-injection matrix for the bootstrap v2 engine.

Every case proves, using sequence numbers and the complete mutation
ledger: first blocker -> zero later mutations -> no metadata -> nonzero
exit -> truthful final status.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts" / "release"))
sys.path.insert(0, str(REPO))

from bootstrap_v2.model import MetadataStatus, Mode, RunResult, RunStatus  # noqa: E402
from bootstrap_v2.validators import metadata as metadata_validators  # noqa: E402
from tests.bootstrap_v2_fakes import FakeWorld, make_engine, make_happy_world  # noqa: E402

ALL_DRIFT = {
    "worker_flag_true": True,
    "api_flag_true": True,
    "vercel_managed_missing": True,
    "sa_absent": True,
    "secret_iam_missing_member": True,
    "secret_version_stale": True,
}


def assert_first_blocker_semantics(
    result: RunResult, world: FakeWorld, output_dir: Path
) -> None:
    """The core guarantee, asserted from the ledger, not from one command."""

    # nonzero exit and truthful status
    assert result.exit_code() != 0
    executed = [m for m in result.mutations if m.executed]
    if executed:
        assert result.status is RunStatus.PARTIAL
    else:
        assert result.status is RunStatus.BLOCKED

    # no metadata, ever
    assert result.metadata_status is not MetadataStatus.COMMITTED
    assert not (output_dir / "bootstrap-metadata-v3.env").exists()

    # zero later mutations after the first failure, proven by sequence
    failure_seqs = [m.sequence for m in result.mutations if m.executed and not m.succeeded]
    failure_seqs += [m.sequence for m in result.mutations if not m.declared]
    failed_verifications = {v.idempotency_key for v in result.verifications if not v.verified}
    failure_seqs += [
        m.sequence for m in result.mutations if m.idempotency_key in failed_verifications
    ]
    if failure_seqs:
        first_failure = min(failure_seqs)
        later = [m for m in result.mutations if m.sequence > first_failure]
        assert later == [], f"mutations after first blocker: {later}"

    # the ledger is complete: every provider-level write call was gated
    assert len(world.log.writes()) == len(result.mutations)


# ---------------------------------------------------------- discovery reads

DISCOVERY_READ_FAULTS = [
    ("upstash", {"list_databases": "network"}),
    ("upstash", {"list_databases": "auth_failure"}),
    ("upstash", {"get_database": "malformed"}),
    ("gcp", {"list_enabled_services": "permission_denied"}),
    ("gcp", {"describe_service_account": "permission_denied"}),
    ("gcp", {"describe_secret": "timeout"}),
    ("gcp", {"access_secret_payload": "permission_denied"}),
    ("gcp", {"get_secret_iam": "network"}),
    ("gcp", {"get_service_account_iam": "malformed"}),
    ("gcp", {"get_run_invoker_iam": "permission_denied"}),
    ("gcp", {"describe_wif": "api_disabled"}),
    ("gcp", {"describe_run_job": "rate_limited"}),
    ("gcp", {"describe_run_service": "network"}),
    ("vercel", {"get_project": "auth_failure"}),
    ("vercel", {"list_env": "malformed"}),
]


@pytest.mark.parametrize("provider,faults", DISCOVERY_READ_FAULTS)
def test_discovery_read_failure_blocks_with_zero_mutations(
    provider, faults, tmp_path
):
    kwargs = {f"{provider}_faults": faults}
    world = make_happy_world(drift=dict(ALL_DRIFT), **kwargs)
    result = make_engine(world, Mode.APPLY, tmp_path).run()
    assert result.status is RunStatus.BLOCKED
    assert result.mutations == ()
    assert world.log.writes() == []
    assert_first_blocker_semantics(result, world, tmp_path)


@pytest.mark.parametrize("provider,faults", DISCOVERY_READ_FAULTS[:6])
def test_discovery_read_failure_blocks_plan_mode_too(provider, faults, tmp_path):
    kwargs = {f"{provider}_faults": faults}
    world = make_happy_world(**kwargs)
    result = make_engine(world, Mode.PLAN, tmp_path).run()
    assert result.status is RunStatus.BLOCKED
    assert result.exit_code() != 0
    assert world.log.writes() == []


# ---------------------------------------------------------- mutation failures

MUTATION_FAULTS = [
    ("upstash", {"create_database": "fail_before_mutation"}, {"database_absent": True}),
    ("gcp", {"create_service_account": "fail_before_mutation"}, {"drift": {"sa_absent": True}}),
    ("gcp", {"add_secret_version": "fail_before_mutation"}, {"drift": {"secret_version_stale": True}}),
    ("gcp", {"set_secret_iam": "fail_before_mutation"}, {"drift": dict(ALL_DRIFT)}),
    ("gcp", {"update_run_job": "fail_before_mutation"}, {"drift": dict(ALL_DRIFT)}),
    ("gcp", {"update_run_service": "fail_before_mutation"}, {"drift": {"api_flag_true": True, "vercel_managed_missing": True}}),
    ("vercel", {"set_env_var": "fail_before_mutation"}, {"drift": dict(ALL_DRIFT)}),
]


@pytest.mark.parametrize("provider,faults,world_kwargs", MUTATION_FAULTS)
def test_mutation_failure_stops_everything_later(
    provider, faults, world_kwargs, tmp_path
):
    kwargs = dict(world_kwargs)
    kwargs[f"{provider}_faults"] = faults
    world = make_happy_world(**kwargs)
    result = make_engine(world, Mode.APPLY, tmp_path).run()
    failed = [m for m in result.mutations if m.executed and not m.succeeded]
    assert len(failed) == 1
    assert_first_blocker_semantics(result, world, tmp_path)


# ------------------------------------------- lying writes / wrong post-state

LYING_WRITE_FAULTS = [
    ("upstash", {"create_database": "report_success_no_apply"}, {"database_absent": True}),
    ("gcp", {"create_service_account": "report_success_no_apply"}, {"drift": {"sa_absent": True}}),
    ("gcp", {"add_secret_version": "report_success_no_apply"}, {"drift": {"secret_version_stale": True}}),
    ("gcp", {"set_secret_iam": "report_success_no_apply"}, {"drift": dict(ALL_DRIFT)}),
    ("gcp", {"update_run_job": "report_success_no_apply"}, {"drift": dict(ALL_DRIFT)}),
    ("vercel", {"set_env_var": "report_success_no_apply"}, {"drift": {"vercel_managed_missing": True}}),
    ("gcp", {"set_secret_iam": "apply_wrong_state"}, {"drift": dict(ALL_DRIFT)}),
    ("gcp", {"update_run_job": "apply_wrong_state"}, {"drift": dict(ALL_DRIFT)}),
    ("vercel", {"set_env_var": "apply_wrong_state"}, {"drift": {"vercel_managed_missing": True}}),
    ("upstash", {"create_database": "apply_wrong_state"}, {"database_absent": True}),
]


@pytest.mark.parametrize("provider,faults,world_kwargs", LYING_WRITE_FAULTS)
def test_unverified_post_state_blocks_later_mutations(
    provider, faults, world_kwargs, tmp_path
):
    kwargs = dict(world_kwargs)
    kwargs[f"{provider}_faults"] = faults
    world = make_happy_world(**kwargs)
    result = make_engine(world, Mode.APPLY, tmp_path).run()
    assert any(not v.verified for v in result.verifications) or any(
        f.code
        in (
            "UPSTASH_POST_CREATE_MISMATCH",
            "UPSTASH_POST_CREATE_UNVERIFIED",
        )
        for f in result.findings
    )
    assert_first_blocker_semantics(result, world, tmp_path)


# ---------------------------------------------------------- post-write reads


def test_post_write_read_failure_blocks(tmp_path):
    # describe_service_account: #1 discovery, #2 (worker missing -> absent),
    # rediscovery #4-6, post-create reread is the 7th describe call.
    world = make_happy_world(
        drift={"sa_absent": True},
        gcp_faults={"describe_service_account#7": "network"},
    )
    result = make_engine(world, Mode.APPLY, tmp_path).run()
    assert any(not v.verified for v in result.verifications)
    assert_first_blocker_semantics(result, world, tmp_path)


# ---------------------------------------------------------- final audit


def test_final_audit_read_failure_withholds_metadata(tmp_path):
    # describe_run_job calls in a clean apply: discovery, rediscovery, final
    # audit -> fail only the third.
    world = make_happy_world(gcp_faults={"describe_run_job#3": "network"})
    result = make_engine(world, Mode.APPLY, tmp_path).run()
    assert result.exit_code() != 0
    assert result.metadata_status is MetadataStatus.WITHHELD
    assert not (tmp_path / "bootstrap-metadata-v3.env").exists()


def test_concurrent_drift_between_plan_and_apply_blocks(tmp_path):
    # The vercel env inventory changes between the freeze discovery and the
    # apply rediscovery -> regenerated digest differs -> block, zero writes.
    world = make_happy_world(drift={"vercel_managed_missing": True})

    original_list_env = world.vercel.list_env
    calls = {"n": 0}

    def flapping_list_env(project_id):
        calls["n"] += 1
        if calls["n"] == 2:  # mutate world between discovery and rediscovery
            world.vercel.env_values["MILO_REDIS_TOKEN_FINGERPRINT"] = "x" * 64
        return original_list_env(project_id)

    world.vercel.list_env = flapping_list_env
    result = make_engine(world, Mode.APPLY, tmp_path).run()
    assert result.status is RunStatus.BLOCKED
    assert any(f.code == "PLAN_DIGEST_DRIFT" for f in result.findings)
    assert world.log.writes() == []


def test_wrong_approved_digest_blocks_before_mutation(tmp_path):
    world = make_happy_world(drift={"vercel_managed_missing": True})
    result = make_engine(
        world, Mode.APPLY, tmp_path, approved_plan_digest="0" * 64
    ).run()
    assert result.status is RunStatus.BLOCKED
    assert any(f.code == "PLAN_DIGEST_NOT_APPROVED" for f in result.findings)
    assert world.log.writes() == []


# ---------------------------------------------------------- metadata failure


def test_metadata_write_failure_cannot_be_applied(tmp_path, monkeypatch):
    world = make_happy_world(drift={"vercel_managed_missing": True})

    def boom(metadata, output_dir):
        raise OSError("disk full")

    monkeypatch.setattr(metadata_validators, "write_metadata_atomically", boom)
    result = make_engine(world, Mode.APPLY, tmp_path).run()
    assert result.status is not RunStatus.APPLIED
    assert result.metadata_status is MetadataStatus.WITHHELD
    assert result.exit_code() != 0
    assert not (tmp_path / "bootstrap-metadata-v3.env").exists()


# ---------------------------------------------------------- local guard


@pytest.mark.parametrize(
    "override",
    [
        {"worktree_clean": False},
        {"head_sha": "0" * 40},
        {"repository": "attacker/elsewhere"},
        {"environment": "staging"},
        {"operator_ack": ""},
        {"operator_ack": "yes"},
        {"ref": "feature/unauthorized"},
        {"deprecated_metadata_keys": ("MILO_RELEASE_SHA",)},
        {"tooling_ok": False},
    ],
)
def test_local_guard_failures_block_before_any_read(override, tmp_path):
    from tests.bootstrap_v2_fakes import FakeLocal

    world = make_happy_world()
    world.local = FakeLocal(world.config, **override)
    result = make_engine(world, Mode.APPLY, tmp_path).run()
    assert result.status is RunStatus.BLOCKED
    assert world.log.writes() == []
    assert world.log.records == []  # blocked before the first provider read


def test_forbidden_env_override_blocks(tmp_path):
    world = make_happy_world()
    result = make_engine(
        world,
        Mode.APPLY,
        tmp_path,
        environ={"UPSTASH_REDIS_REST_TOKEN": "sneaky"},
    ).run()
    assert result.status is RunStatus.BLOCKED
    assert any(f.code == "LOCAL_ENV_OVERRIDE" for f in result.findings)
    assert world.log.writes() == []

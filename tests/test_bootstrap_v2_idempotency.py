"""Idempotency and secret-leakage tests for the bootstrap v2 engine."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts" / "release"))
sys.path.insert(0, str(REPO))

from bootstrap_v2.model import MetadataStatus, Mode, RunStatus  # noqa: E402
from bootstrap_v2.report import render_human_summary, run_result_to_dict, write_json_report  # noqa: E402
from tests.bootstrap_v2_fakes import (  # noqa: E402
    ALL_WRITE_LABELS,
    REDIS_TOKEN,
    make_engine,
    make_happy_world,
)

ALL_DRIFT = {
    "worker_flag_true": True,
    "api_flag_true": True,
    "vercel_managed_missing": True,
    "sa_absent": True,
    "secret_iam_missing_member": True,
    "secret_version_stale": True,
}


def test_successful_apply_twice_second_run_writes_nothing(tmp_path):
    world = make_happy_world(drift=dict(ALL_DRIFT))
    first = make_engine(world, Mode.APPLY, tmp_path / "run1").run()
    assert first.status is RunStatus.APPLIED
    first_writes = len(world.log.writes())
    assert first_writes > 0

    second = make_engine(world, Mode.APPLY, tmp_path / "run2").run()
    assert second.status is RunStatus.APPLIED
    assert len(world.log.writes()) == first_writes  # zero new writes
    assert second.mutations == ()


def test_existing_correct_state_yields_zero_writes(tmp_path):
    world = make_happy_world()
    result = make_engine(world, Mode.APPLY, tmp_path).run()
    assert result.status is RunStatus.APPLIED
    assert world.log.writes() == []
    assert result.mutations == ()


def test_matching_redis_token_avoids_version_rotation(tmp_path):
    world = make_happy_world(drift={"vercel_managed_missing": True})
    result = make_engine(world, Mode.APPLY, tmp_path).run()
    assert result.status is RunStatus.APPLIED
    assert not any(
        record.operation == "add_secret_version" for record in world.log.writes()
    )


def test_exact_iam_cloud_run_and_vercel_avoid_writes(tmp_path):
    world = make_happy_world(drift={"sa_absent": True})
    result = make_engine(world, Mode.APPLY, tmp_path).run()
    assert result.status is RunStatus.APPLIED
    operations = {record.operation for record in world.log.writes()}
    assert operations == {"create_service_account"}


def test_database_created_on_first_run_adopted_on_second(tmp_path):
    world = make_happy_world(database_absent=True, drift={"worker_flag_true": True})
    first = make_engine(world, Mode.APPLY, tmp_path / "run1").run()
    assert first.status is RunStatus.PARTIAL
    assert first.metadata_status is MetadataStatus.WITHHELD
    assert [r.operation for r in world.log.writes()] == ["create_database"]
    assert first.created_resources
    assert first.recovery_steps

    second = make_engine(world, Mode.APPLY, tmp_path / "run2").run()
    assert second.status is RunStatus.APPLIED
    creates = [r for r in world.log.writes() if r.operation == "create_database"]
    assert len(creates) == 1  # adopted, not duplicated


def test_interruption_after_every_individual_mutation_is_resumable(tmp_path):
    # Learn the write sequence of a full drift apply.
    reference = make_happy_world(drift=dict(ALL_DRIFT))
    result = make_engine(reference, Mode.APPLY, tmp_path / "ref").run()
    assert result.status is RunStatus.APPLIED
    sequence = [record.operation for record in reference.log.writes()]
    assert len(sequence) >= 5

    for index, label in enumerate(sequence):
        if index + 1 >= len(sequence):
            continue
        next_label = sequence[index + 1]
        # Interrupt: the write after `label` fails before mutation.
        world = make_happy_world(drift=dict(ALL_DRIFT))
        for holder in (world.gcp.faults, world.vercel.faults, world.upstash.faults):
            holder.clear()
        target = _faults_holder(world, next_label)
        target[next_label] = "fail_before_mutation"

        partial = make_engine(world, Mode.APPLY, tmp_path / f"p{index}").run()
        assert partial.status is RunStatus.PARTIAL, label
        assert partial.metadata_status is MetadataStatus.WITHHELD

        # Resume: clear the fault and rerun from fresh discovery.
        target.clear()
        resumed = make_engine(world, Mode.APPLY, tmp_path / f"r{index}").run()
        assert resumed.status is RunStatus.APPLIED, label

        # A rerun never duplicates a create nor broadens access: each
        # resource-mutating label ran successfully at most twice overall
        # (once before interruption, once with identical intent after).
        for op_label in set(sequence):
            applied = [
                r for r in world.log.writes() if r.operation == op_label
            ]
            assert len(applied) <= 2, (op_label, applied)


def _faults_holder(world, label: str):
    if label in ("create_database",):
        return world.upstash.faults
    if label in ("set_env_var",):
        return world.vercel.faults
    return world.gcp.faults


def test_all_write_labels_are_covered_by_the_matrix():
    # Guard: if a new write appears in the fakes, the fault matrix and the
    # interruption walk must learn about it.
    assert ALL_WRITE_LABELS == {
        "create_database",
        "create_service_account",
        "create_secret",
        "add_secret_version",
        "set_secret_iam",
        "set_gateway_wif_iam",
        "set_run_invoker_iam",
        "update_run_job",
        "update_run_service",
        "set_env_var",
    }


def test_audit_mode_never_mutates_even_on_drift(tmp_path):
    world = make_happy_world(drift=dict(ALL_DRIFT))
    result = make_engine(world, Mode.AUDIT, tmp_path).run()
    assert result.status is RunStatus.BLOCKED  # drift found
    assert world.log.writes() == []
    assert result.mutations == ()


def test_plan_mode_never_mutates(tmp_path):
    world = make_happy_world(drift=dict(ALL_DRIFT), database_absent=True)
    result = make_engine(world, Mode.PLAN, tmp_path).run()
    assert result.status is RunStatus.PLANNED
    assert world.log.writes() == []
    assert (tmp_path / "bootstrap-v2-plan.json").exists()


# ---------------------------------------------------------- secret leakage


def _all_output_text(result, tmp_path: Path) -> str:
    chunks = [
        render_human_summary(result),
        json.dumps(run_result_to_dict(result)),
    ]
    for path in tmp_path.rglob("*"):
        if path.is_file():
            chunks.append(path.read_text(errors="replace"))
    return "\n".join(chunks)


def test_no_secret_leakage_in_any_output(tmp_path):
    world = make_happy_world(drift=dict(ALL_DRIFT))
    result = make_engine(world, Mode.APPLY, tmp_path).run()
    assert result.status is RunStatus.APPLIED
    write_json_report(result, tmp_path)
    text = _all_output_text(result, tmp_path)
    assert REDIS_TOKEN not in text
    # the fingerprint (non-secret) is allowed; the raw token never
    assert "fake-upstash-rest-token" not in text


def test_no_secret_leakage_on_failure_paths(tmp_path):
    world = make_happy_world(
        drift=dict(ALL_DRIFT), vercel_faults={"set_env_var": "fail_before_mutation"}
    )
    result = make_engine(world, Mode.APPLY, tmp_path).run()
    assert result.status is RunStatus.PARTIAL
    write_json_report(result, tmp_path)
    text = _all_output_text(result, tmp_path)
    assert REDIS_TOKEN not in text


def test_plan_artifact_and_metadata_contain_no_token(tmp_path):
    world = make_happy_world(drift=dict(ALL_DRIFT))
    plan_result = make_engine(world, Mode.PLAN, tmp_path / "plan").run()
    assert plan_result.status is RunStatus.PLANNED
    apply_result = make_engine(world, Mode.APPLY, tmp_path / "apply").run()
    assert apply_result.status is RunStatus.APPLIED
    for path in (
        tmp_path / "plan" / "bootstrap-v2-plan.json",
        tmp_path / "apply" / "bootstrap-metadata-v3.env",
    ):
        assert path.exists()
        assert REDIS_TOKEN not in path.read_text()


def test_report_redaction_backstop():
    from bootstrap_v2.report import redact

    assert "REDACTED" in redact("Authorization: Bearer abc.def-ghi")
    assert "REDACTED" in redact("UPSTASH_API_KEY=supersecret")
    assert "REDACTED" in redact("https://user:pass@host.example/path")
    assert redact("plain text stays") == "plain text stays"

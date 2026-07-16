"""Regression tests for the adversarial-review findings (Phase 18).

Each test pins a defect found and fixed during the two mandatory reviews so
it cannot reappear.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts" / "release"))
sys.path.insert(0, str(REPO))

from bootstrap_v2.adapters.upstash import UpstashAdapter  # noqa: E402
from bootstrap_v2.adapters.vercel import VercelAdapter  # noqa: E402
from bootstrap_v2.adapters.vercel import HttpResponse as VercelHttpResponse  # noqa: E402
from bootstrap_v2.model import (  # noqa: E402
    IamBinding,
    MetadataStatus,
    Mode,
    ProbeOutcome,
    RunStatus,
    SecretState,
)
from bootstrap_v2.planner import PlannerEvidenceError, desired_cloud_run_env  # noqa: E402
from bootstrap_v2.policy import WORKER_SECRET_REFS  # noqa: E402
from tests.bootstrap_v2_fakes import (  # noqa: E402
    REDIS_TOKEN,
    make_cloud_run_state,
    make_config,
    make_engine,
    make_happy_world,
)
from tests.test_bootstrap_v2_planner import make_redis_identity  # noqa: E402

CONFIG = make_config()


# --- finding: secret-version prediction ignored disabled versions ----------


def test_new_redis_version_prediction_accounts_for_disabled_versions(tmp_path):
    """GCP numbers versions across ALL states: with enabled=7 but disabled
    versions up to 9, the next add creates 10 and every pin must say 10."""

    world = make_happy_world(drift={"secret_version_stale": True})
    current = world.gcp.secrets["UPSTASH_REDIS_REST_TOKEN"]
    world.gcp.secrets["UPSTASH_REDIS_REST_TOKEN"] = SecretState(
        name=current.name,
        exists=True,
        enabled_versions=current.enabled_versions,
        latest_enabled_version="7",
        highest_version="9",  # versions 8 and 9 exist but are disabled
    )
    result = make_engine(world, Mode.APPLY, tmp_path).run()
    assert result.status is RunStatus.APPLIED
    assert world.gcp.secrets["UPSTASH_REDIS_REST_TOKEN"].latest_enabled_version == "10"
    worker_env = {
        var.name: var for var in world.gcp.worker.containers[0].env
    }
    assert worker_env["UPSTASH_REDIS_REST_TOKEN"].secret_version == "10"
    assert worker_env["MILO_REDIS_SECRET_VERSION"].value == "10"
    metadata = (tmp_path / "bootstrap-metadata-v3.env").read_text()
    assert "MILO_REDIS_SECRET_VERSION=10" in metadata


# --- finding: vercel `target` may be a bare string --------------------------


def test_vercel_string_target_is_recognized_as_production():
    body = json.dumps(
        {
            "envs": [
                {
                    "id": "env_1",
                    "key": "CLOUD_RUN_API_URL",
                    "value": "https://example.run.app",
                    "target": "production",
                }
            ]
        }
    ).encode()

    adapter = VercelAdapter(
        "tok", "team", transport=lambda m, u, h, b: VercelHttpResponse(200, body)
    )
    probe = adapter.list_env("prj")
    assert probe.outcome is ProbeOutcome.PRESENT
    assert probe.env_vars[0].target == ("production",)


# --- finding: post-create region verification was a tautology ---------------


def test_wrong_region_database_fails_post_create_verification(tmp_path):
    world = make_happy_world(
        database_absent=True,
        upstash_faults={"create_database": "apply_wrong_region"},
    )
    result = make_engine(world, Mode.APPLY, tmp_path).run()
    assert result.status is RunStatus.PARTIAL
    assert any(not v.verified for v in result.verifications)
    assert result.metadata_status is MetadataStatus.WITHHELD
    # the created resource is still recorded for adoption by a rerun
    assert result.created_resources


# --- finding: unexpected exceptions escaped the single-RunResult contract ---


def test_unexpected_adapter_exception_still_yields_a_run_result(tmp_path):
    world = make_happy_world()

    def boom(pool_id, provider_id, project_number):
        raise RuntimeError("adapter bug")

    world.gcp.describe_wif = boom
    result = make_engine(world, Mode.APPLY, tmp_path).run()
    assert result.status is RunStatus.BLOCKED
    assert result.exit_code() == 1
    assert any(f.code == "RUN_ABORTED_UNEXPECTED" for f in result.findings)
    assert world.log.writes() == []


# --- finding: non-bool tls was silently coerced ------------------------------


def test_non_bool_tls_is_malformed_output():
    from bootstrap_v2.adapters.upstash import HttpResponse

    body = json.dumps(
        [
            {
                "database_id": "db-1",
                "database_name": "milo-production",
                "state": "active",
                "tls": "true",  # undocumented string shape
                "region": "us-central1",
                "endpoint": "x.upstash.io",
            }
        ]
    ).encode()
    adapter = UpstashAdapter(
        "a@b.c", "key", transport=lambda m, u, h, b: HttpResponse(200, body)
    )
    assert adapter.list_databases().outcome is ProbeOutcome.MALFORMED_OUTPUT


# --- finding: fabricated version "1" for absent secret refs -----------------


def test_missing_secret_ref_adopts_discovered_latest_version():
    observed = make_cloud_run_state(CONFIG, is_job=True)
    stripped = observed.containers[0]
    without_kimi = tuple(v for v in stripped.env if v.name != "KIMI_API_KEY")
    import dataclasses

    observed = dataclasses.replace(
        observed,
        containers=(dataclasses.replace(stripped, env=without_kimi),),
    )
    desired = desired_cloud_run_env(
        observed,
        CONFIG,
        make_redis_identity(),
        WORKER_SECRET_REFS,
        {"KIMI_API_KEY": "3"},
    )
    by_name = {var.name: var for var in desired}
    assert by_name["KIMI_API_KEY"].secret_version == "3"


def test_missing_secret_ref_with_no_enabled_version_blocks_planning():
    observed = make_cloud_run_state(CONFIG, is_job=True)
    stripped = observed.containers[0]
    without_kimi = tuple(v for v in stripped.env if v.name != "KIMI_API_KEY")
    import dataclasses

    observed = dataclasses.replace(
        observed,
        containers=(dataclasses.replace(stripped, env=without_kimi),),
    )
    with pytest.raises(PlannerEvidenceError):
        desired_cloud_run_env(
            observed,
            CONFIG,
            make_redis_identity(),
            WORKER_SECRET_REFS,
            {},
        )


# --- finding: unauthenticated access invisible to the strict validator ------


def test_public_invoker_marks_api_service_unauthenticated(tmp_path):
    world = make_happy_world()
    world.gcp.invoker_policy = type(world.gcp.invoker_policy)(
        resource=world.gcp.invoker_policy.resource,
        etag="etag-public",
        bindings=(
            IamBinding(role="roles/run.invoker", members=("allUsers",)),
        ),
    )
    result = make_engine(world, Mode.AUDIT, tmp_path).run()
    assert result.status is RunStatus.BLOCKED
    codes = {f.code for f in result.findings}
    assert "IAM_BROAD_PRINCIPAL" in codes
    assert "CLOUD_RUN_UNAUTHENTICATED" in codes
    assert world.log.writes() == []


# --- finding: planner treated a failed vercel inventory read as absence -----


def test_planner_rejects_empty_vercel_inventory():
    from bootstrap_v2.planner import build_plan
    from tests.test_bootstrap_v2_planner import make_world

    with pytest.raises(PlannerEvidenceError):
        build_plan(CONFIG, make_world(vercel_env=()))


# --- finding: no leak of the raw token through the new code paths -----------


def test_version_rotation_paths_leak_no_secret(tmp_path):
    world = make_happy_world(drift={"secret_version_stale": True})
    result = make_engine(world, Mode.APPLY, tmp_path).run()
    assert result.status is RunStatus.APPLIED
    from bootstrap_v2.report import render_human_summary, run_result_to_dict

    text = render_human_summary(result) + json.dumps(run_result_to_dict(result))
    for path in tmp_path.rglob("*"):
        if path.is_file():
            text += path.read_text(errors="replace")
    assert REDIS_TOKEN not in text

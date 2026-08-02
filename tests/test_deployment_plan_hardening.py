"""Hardening proofs for scripts/release/generate-deployment-plan.sh.

The generator emits a command plan only — it deploys nothing. These tests run
it and assert that the emitted plan cannot instruct an operator to:

- tag an image with anything but the full 40-character release SHA;
- use the destructive ``--set-env-vars`` / ``--set-secrets`` forms, which
  would delete every production variable and secret binding not repeated in
  the command;
- omit a Stage A variable or leave an execution flag on;
- bind ``KIMI_API_KEY`` to the API service;
- deploy the API before the worker job, or execute the worker.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
GENERATOR = REPO / "scripts" / "release" / "generate-deployment-plan.sh"

FULL_SHA = "0123456789abcdef0123456789abcdef01234567"
SHORT_SHA = FULL_SHA[:12]

EXECUTION_FLAGS = (
    "MILO_ENABLE_RUN_CREATION",
    "MILO_ENABLE_PROPOSAL_MUTATIONS",
    "MILO_ENABLE_PROPOSAL_READS",
    "MILO_ENABLE_RUN_CANCELLATION",
    "MILO_ENABLE_EXECUTION_CONTROL",
    "MILO_ENABLE_PAID_EXECUTION",
)


def generate(tmp_path: Path, *args: str) -> tuple[subprocess.CompletedProcess, str, dict]:
    output = tmp_path / "plan.md"
    report = tmp_path / "plan.json"
    result = subprocess.run(
        [
            "bash",
            str(GENERATOR),
            "--release-sha",
            *(args or (FULL_SHA,)),
            "--output",
            str(output),
            "--json-output",
            str(report),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
    )
    plan = output.read_text() if output.exists() else ""
    checks = json.loads(report.read_text()) if report.exists() else {}
    return result, plan, checks


@pytest.fixture(scope="module")
def plan(tmp_path_factory) -> str:
    result, text, _ = generate(tmp_path_factory.mktemp("plan"))
    assert result.returncode == 0, result.stdout + result.stderr
    return text


def command_lines(plan_text: str) -> str:
    """Only the indented command templates (prose names forbidden flags)."""
    return "\n".join(line for line in plan_text.splitlines() if line.startswith("    "))


def deploy_command(plan_text: str, prefix: str) -> str:
    lines = command_lines(plan_text).splitlines()
    for index, line in enumerate(lines):
        if line.strip().startswith(prefix):
            block = [line]
            cursor = index
            while block[-1].rstrip().endswith("\\"):
                cursor += 1
                block.append(lines[cursor])
            return "\n".join(block)
    raise AssertionError(f"command not found in plan: {prefix!r}")


# ---------------------------------------------------------------------------
# immutable, full-SHA image identity
# ---------------------------------------------------------------------------


def test_short_sha_is_rejected(tmp_path):
    result, plan_text, checks = generate(tmp_path, SHORT_SHA)
    assert result.returncode != 0
    assert plan_text == ""
    assert any(
        check["name"] == "release-sha" and check["status"] == "BLOCKED"
        for check in checks.get("checks", [])
    )


def test_mutable_tags_are_rejected(tmp_path):
    for tag in ("latest", "prod", "stable", "main", "master"):
        result, plan_text, _ = generate(tmp_path, tag)
        assert result.returncode != 0, f"mutable tag accepted: {tag}"
        assert plan_text == ""


def test_every_image_reference_uses_the_full_sha(plan):
    references = [
        token
        for line in plan.splitlines()
        for token in line.split()
        if "milo-api:" in token or "milo-worker:" in token
    ]
    assert references
    for reference in references:
        tag = reference.split(":")[-1]
        assert tag == FULL_SHA, f"image reference is not tagged with the full SHA: {reference}"
        assert len(tag) == 40


def test_revision_suffix_is_documented_as_a_revision_name_not_an_image_tag(plan):
    assert f"--revision-suffix rel-{FULL_SHA[:12]}" in plan
    assert "is a Cloud Run *revision name*, not an image" in plan


# ---------------------------------------------------------------------------
# non-destructive environment / secret updates
# ---------------------------------------------------------------------------


def test_no_command_uses_destructive_env_or_secret_flags(plan):
    commands = command_lines(plan)
    for forbidden in ("--set-env-vars", "--set-secrets", "--clear-env-vars", "--clear-secrets"):
        assert forbidden not in commands, f"plan command uses {forbidden}"


def test_both_deploy_commands_use_update_flags(plan):
    for prefix in ("gcloud run jobs deploy", "gcloud run deploy"):
        command = deploy_command(plan, prefix)
        assert "--update-env-vars" in command
        assert "--update-secrets" in command


def test_plan_proves_no_variable_was_dropped(plan):
    assert "comm -23 api-env-before.txt api-env-after.txt" in plan
    assert "comm -23 worker-env-before.txt worker-env-after.txt" in plan
    assert "expect: EMPTY (nothing was dropped)" in plan


def test_comma_containing_values_use_the_alternate_delimiter(plan):
    api = deploy_command(plan, "gcloud run deploy")
    assert "'^@^" in api
    assert "@ALLOWED_CORS_ORIGINS=<PRODUCTION_ORIGINS>@" in api


# ---------------------------------------------------------------------------
# Stage A posture
# ---------------------------------------------------------------------------


def test_api_command_pins_every_stage_a_variable(plan):
    api = deploy_command(plan, "gcloud run deploy")
    assert "JOB_LAUNCHER=disabled" in api
    assert "JOB_LAUNCHER=cloud_run" not in plan
    for flag in EXECUTION_FLAGS:
        assert f"{flag}=false" in api, f"API command omits {flag}"
        assert f"{flag}=true" not in plan


def test_worker_command_pins_every_execution_flag(plan):
    worker = deploy_command(plan, "gcloud run jobs deploy")
    for flag in EXECUTION_FLAGS:
        assert f"{flag}=false" in worker, f"worker command omits {flag}"
    # JOB_LAUNCHER is an API-only variable.
    assert "JOB_LAUNCHER" not in worker


# ---------------------------------------------------------------------------
# provider key stays worker-only
# ---------------------------------------------------------------------------


def test_api_command_never_binds_a_provider_key(plan):
    api = deploy_command(plan, "gcloud run deploy")
    assert "KIMI_API_KEY" not in api
    assert "MOONSHOT_API_KEY" not in api
    assert "must NOT appear in the API secret" in plan


# ---------------------------------------------------------------------------
# ordering and non-execution
# ---------------------------------------------------------------------------


def test_worker_is_deployed_before_the_api(plan):
    assert plan.index("gcloud run jobs deploy") < plan.index("gcloud run deploy ")


def test_plan_never_executes_the_worker(plan):
    assert "gcloud run jobs execute" not in command_lines(plan)
    assert "Do NOT" in plan and "gcloud run jobs execute" in plan  # prose forbids it
    assert "expect: no new executions" in plan


# ---------------------------------------------------------------------------
# post-deployment verification coverage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expected",
    [
        "image_summary.digest",  # release digest
        "spec.template.spec.serviceAccountName",  # API identity
        "spec.template.spec.template.spec.serviceAccountName",  # worker identity
        "containers[0].env[].name",  # variable NAMES
        "valueFrom.secretKeyRef.secret",  # secret REFERENCES
        "gcloud run services get-iam-policy",  # private IAM
        "gcloud run jobs get-iam-policy",
        "expect: no allUsers",
        "gcloud run jobs executions list",  # no worker execution
    ],
)
def test_verification_step_present(plan, expected):
    assert expected in plan


# ---------------------------------------------------------------------------
# the generator refuses to emit an unsafe plan
# ---------------------------------------------------------------------------


def test_generator_self_checks_all_pass(tmp_path):
    result, _, checks = generate(tmp_path)
    assert result.returncode == 0
    statuses = {check["name"]: check["status"] for check in checks["checks"]}
    for name in (
        "env-update-mode",
        "image-tags",
        "stage-a-flags",
        "provider-key-scope",
        "no-worker-execution",
        "deploy-order",
    ):
        assert statuses.get(name) == "PASS", f"{name} -> {statuses.get(name)}"


def test_generator_blocks_a_plan_containing_a_destructive_flag(tmp_path):
    """The self-check is what makes the plan safe to edit; prove it bites."""
    # The generator resolves its library and repo root relative to itself, so
    # it is copied into an equivalent throwaway tree before being patched.
    root = tmp_path / "repo"
    shutil.copytree(GENERATOR.parent, root / "scripts" / "release")
    (root / "Dockerfile.api").write_text("FROM scratch\n")
    (root / "Dockerfile.worker").write_text("FROM scratch\n")
    patched = root / "scripts" / "release" / "generate-deployment-plan.sh"
    source = GENERATOR.read_text().replace(
        "--update-secrets SUPABASE_SERVICE_ROLE_KEY=<SUPABASE_SERVICE_KEY_SECRET>:latest",
        "--set-secrets SUPABASE_SERVICE_ROLE_KEY=<SUPABASE_SERVICE_KEY_SECRET>:latest",
        1,
    )
    patched.write_text(source)
    output = tmp_path / "plan.md"
    result = subprocess.run(
        ["bash", str(patched), "--release-sha", FULL_SHA, "--output", str(output)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode != 0
    assert "destructive" in result.stdout
    assert not output.exists()

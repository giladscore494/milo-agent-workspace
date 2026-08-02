"""The two deployment tools must emit the same deployment.

``scripts/deploy/cloud-run.sh`` deploys production directly;
``scripts/release/generate-deployment-plan.sh`` emits the command plan a human
operator runs by hand. They are two paths to the same production state, so a
difference between them is not a cosmetic inconsistency — it is one path
deploying something the other never validated.

The failure this file exists to catch: the two tools naming *different image
repositories* (``…/api`` vs ``…/milo-api``). Both look correct in isolation;
together they mean the plan tells the operator to push and deploy an image
that the executable script never builds, and that no rollback target exists
for. The same reasoning covers the Supabase/Upstash bindings and the Stage A
provider-key posture.

Both tools are run for real here — nothing is deployed, contacted or built:
``cloud-run.sh`` runs in apply mode against the mocked ``gcloud`` from
``test_cloud_run_deploy_apply_mock``, and the generator only ever writes
markdown.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
from test_cloud_run_deploy_apply_mock import Deployment

REPO = Path(__file__).resolve().parents[1]
GENERATOR = REPO / "scripts" / "release" / "generate-deployment-plan.sh"
CONTRACT = REPO / "scripts" / "deploy" / "deployment-contract.sh"

FULL_SHA = "0123456789abcdef0123456789abcdef01234567"
PROVIDER_KEYS = ("KIMI_API_KEY", "MOONSHOT_API_KEY")

DELIMITER = CONTRACT.read_text().split('MILO_ENV_VAR_DELIMITER="', 1)[1].split('"', 1)[0]


# ---------------------------------------------------------------------------
# what each tool emits
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def script_commands(tmp_path_factory) -> dict[str, str]:
    """The gcloud commands scripts/deploy/cloud-run.sh actually issues."""
    deployment = Deployment(tmp_path_factory.mktemp("deploy"))
    result = deployment.run()
    assert result.returncode == 0, result.stderr
    return {
        "api": deployment.command("run deploy "),
        "worker": deployment.command("run jobs deploy"),
    }


@pytest.fixture(scope="module")
def plan_commands(tmp_path_factory) -> dict[str, str]:
    """The gcloud commands the generated operator plan instructs."""
    output = tmp_path_factory.mktemp("plan") / "plan.md"
    result = subprocess.run(
        ["bash", str(GENERATOR), "--release-sha", FULL_SHA, "--output", str(output)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    lines = [line for line in output.read_text().splitlines() if line.startswith("    ")]

    def block(prefix: str) -> str:
        for index, line in enumerate(lines):
            if line.strip().startswith(prefix):
                collected = [line]
                cursor = index
                while collected[-1].rstrip().endswith("\\"):
                    cursor += 1
                    collected.append(lines[cursor])
                return "\n".join(collected)
        raise AssertionError(f"command not found in plan: {prefix!r}")

    return {"api": block("gcloud run deploy"), "worker": block("gcloud run jobs deploy")}


def image_repository(command: str) -> str:
    """The repository path segment of the --image reference, e.g. 'api'."""
    for token in command.split():
        if "docker.pkg.dev/" in token:
            return token.rsplit("/", 1)[-1].split(":")[0]
    raise AssertionError(f"no image reference in command: {command!r}")


def flag_value(command: str, flag: str) -> str:
    """The single argument following a flag, unquoted."""
    tokens = command.split()
    return tokens[tokens.index(flag) + 1].strip("'")


def _pairs(raw: str, separator: str) -> dict[str, str]:
    return dict(
        item.split("=", 1) for item in raw.split(separator) if "=" in item
    )


def secret_bindings(command: str) -> dict[str, str]:
    """{ENV_NAME: VERSION} for every --update-secrets binding.

    Only the environment name and the version are comparable across tools:
    the secret RESOURCE name is concrete in cloud-run.sh and a manifest
    placeholder in the generated plan.
    """
    raw = flag_value(command, "--update-secrets")
    return {name: value.rsplit(":", 1)[-1] for name, value in _pairs(raw, ",").items()}


def env_bindings(command: str) -> dict[str, str]:
    raw = flag_value(command, "--update-env-vars").removeprefix(f"^{DELIMITER}^")
    return _pairs(raw, DELIMITER)


def env_names(command: str) -> set[str]:
    return set(env_bindings(command))


# ---------------------------------------------------------------------------
# image identity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("resource", ["api", "worker"])
def test_both_tools_use_the_same_image_repository(script_commands, plan_commands, resource):
    """The plan must deploy the image the executable script builds."""
    assert image_repository(script_commands[resource]) == image_repository(plan_commands[resource])


def test_image_repositories_are_the_documented_production_paths(script_commands, plan_commands):
    """Changing either path orphans every pushed image and rollback target."""
    for commands in (script_commands, plan_commands):
        assert image_repository(commands["api"]) == "api"
        assert image_repository(commands["worker"]) == "worker"


def test_api_and_worker_images_are_distinct(script_commands):
    assert image_repository(script_commands["api"]) != image_repository(script_commands["worker"])


# ---------------------------------------------------------------------------
# secret bindings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("resource", ["api", "worker"])
def test_both_tools_bind_the_same_secret_variables(script_commands, plan_commands, resource):
    script = secret_bindings(script_commands[resource])
    plan = secret_bindings(plan_commands[resource])
    assert set(script) == set(plan), (
        f"{resource}: cloud-run.sh binds {sorted(script)}, the plan binds {sorted(plan)}"
    )


@pytest.mark.parametrize("resource", ["api", "worker"])
def test_both_tools_bind_the_same_secret_versions(script_commands, plan_commands, resource):
    assert secret_bindings(script_commands[resource]) == secret_bindings(plan_commands[resource])


def test_supabase_and_upstash_are_bound_on_both_resources(script_commands, plan_commands):
    expected = {
        "SUPABASE_URL",
        "SUPABASE_SERVICE_ROLE_KEY",
        "UPSTASH_REDIS_REST_URL",
        "UPSTASH_REDIS_REST_TOKEN",
    }
    for commands in (script_commands, plan_commands):
        for resource in ("api", "worker"):
            assert expected <= set(secret_bindings(commands[resource]))


def test_api_and_worker_share_one_supabase_and_upstash_binding_set(script_commands):
    """A Supabase URL that differs by runtime is two sources of truth."""
    assert secret_bindings(script_commands["api"]) == secret_bindings(script_commands["worker"])


# ---------------------------------------------------------------------------
# Stage A posture
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("resource", ["api", "worker"])
def test_neither_tool_binds_a_provider_key(script_commands, plan_commands, resource):
    for commands in (script_commands, plan_commands):
        for key in PROVIDER_KEYS:
            assert key not in commands[resource], f"{resource} binds {key} during Stage A"


def test_both_tools_set_the_same_required_api_variables(script_commands, plan_commands):
    required = {
        "ENVIRONMENT",
        "JOB_LAUNCHER",
        "GCP_PROJECT_ID",
        "GCP_REGION",
        "CLOUD_RUN_WORKER_JOB",
        "ALLOWED_CORS_ORIGINS",
        "MILO_GATEWAY_AUDIENCE",
        "MILO_APPROVED_GATEWAY_IDENTITIES",
    }
    assert required <= env_names(script_commands["api"])
    assert required <= env_names(plan_commands["api"])


def test_both_tools_set_the_same_required_worker_variables(script_commands, plan_commands):
    required = {"ENVIRONMENT", "GCP_PROJECT_ID", "GCP_REGION"}
    assert required <= env_names(script_commands["worker"])
    assert required <= env_names(plan_commands["worker"])


@pytest.mark.parametrize("resource", ["api", "worker"])
def test_both_tools_pin_every_execution_flag_off(script_commands, plan_commands, resource):
    flags = {
        "MILO_ENABLE_RUN_CREATION",
        "MILO_ENABLE_PROPOSAL_MUTATIONS",
        "MILO_ENABLE_PROPOSAL_READS",
        "MILO_ENABLE_RUN_CANCELLATION",
        "MILO_ENABLE_EXECUTION_CONTROL",
        "MILO_ENABLE_PAID_EXECUTION",
    }
    for commands in (script_commands, plan_commands):
        assert flags <= env_names(commands[resource])
        bindings = env_bindings(commands[resource])
        for flag in flags:
            assert bindings[flag] == "false"


@pytest.mark.parametrize("resource", ["api", "worker"])
def test_both_tools_use_non_destructive_update_flags(script_commands, plan_commands, resource):
    for commands in (script_commands, plan_commands):
        assert "--update-env-vars" in commands[resource]
        assert "--update-secrets" in commands[resource]
        for destructive in ("--set-env-vars", "--set-secrets", "--clear-env-vars"):
            assert destructive not in commands[resource]


@pytest.mark.parametrize("resource", ["api", "worker"])
def test_both_tools_use_the_same_env_var_delimiter(script_commands, plan_commands, resource):
    for commands in (script_commands, plan_commands):
        assert f"^{DELIMITER}^" in commands[resource]


def test_the_delimiter_cannot_split_an_approved_gateway_identity(script_commands):
    """Every service account email contains '@' — it cannot be the delimiter."""
    assert DELIMITER not in "@."
    identities = env_bindings(script_commands["api"])["MILO_APPROVED_GATEWAY_IDENTITIES"]
    assert "@" in identities and DELIMITER not in identities


# ---------------------------------------------------------------------------
# the contract itself
# ---------------------------------------------------------------------------


def test_both_tools_source_the_shared_contract():
    """Alignment is structural, not a convention two files happen to follow."""
    for tool in (
        REPO / "scripts" / "deploy" / "cloud-run.sh",
        GENERATOR,
        REPO / "scripts" / "release" / "generate-rollback-plan.sh",
    ):
        assert "deployment-contract.sh" in tool.read_text(), f"{tool.name} defines its own copy"


def test_both_tools_check_the_live_state_before_any_mutation(tmp_path):
    """Neither path may discover a legacy provider key after deploying."""
    script = (REPO / "scripts" / "deploy" / "cloud-run.sh").read_text()
    assert script.index("  require_no_live_provider_key_bindings") < script.index(
        "gcloud builds submit"
    )

    output = tmp_path / "plan.md"
    subprocess.run(
        ["bash", str(GENERATOR), "--release-sha", FULL_SHA, "--output", str(output)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    plan = output.read_text()
    for key in PROVIDER_KEYS:
        assert plan.index(key) < plan.index("docker build"), (
            f"the plan must look for {key} on the live resources before building"
        )


def test_contract_execution_flags_match_the_backend_definition():
    """The deployed posture and the runtime's own list cannot disagree."""
    from backend.production_config import EXECUTION_FLAGS

    contract = CONTRACT.read_text()
    literal = contract.split("MILO_STAGE_A_EXECUTION_FLAGS=(", 1)[1].split("\n)", 1)[0]
    assert set(EXECUTION_FLAGS) == set(re.findall(r"(MILO_ENABLE_[A-Z_]+)=false", literal))


def test_generated_plan_reports_the_alignment_checks_as_passing(tmp_path):
    report = tmp_path / "plan.json"
    subprocess.run(
        [
            "bash",
            str(GENERATOR),
            "--release-sha",
            FULL_SHA,
            "--output",
            str(tmp_path / "plan.md"),
            "--json-output",
            str(report),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    statuses = {check["name"]: check["status"] for check in json.loads(report.read_text())["checks"]}
    for name in ("image-repository", "secret-bindings", "gateway-identity", "provider-key-scope"):
        assert statuses.get(name) == "PASS", f"{name} -> {statuses.get(name)}"

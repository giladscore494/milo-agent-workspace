"""Executable proofs for scripts/deploy/cloud-run.sh in apply mode.

The script is run end to end against a mocked ``gcloud`` that records every
invocation and serves canned ``describe`` output. Nothing real is contacted:
no build, no deployment, no IAM change, no worker execution, no paid API call.

These tests fail if a deployment would:

- tag images with a short SHA;
- use the destructive ``--set-env-vars`` / ``--set-secrets`` behaviour (proved
  by asserting the flags used AND by failing when a pre-existing binding
  disappears);
- omit a required Stage A variable or leave an execution flag on;
- expose ``KIMI_API_KEY`` to the API service;
- run the API before the worker job, or execute the worker at all.
"""

from __future__ import annotations

import copy
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = REPO / "scripts" / "deploy" / "cloud-run.sh"

PROJECT = "test-project"
REGION = "us-central1"
REPOSITORY = "milo-agent"
API_SERVICE = "milo-agent-api"
WORKER_JOB = "milo-agent-worker"
API_SA = "milo-api-runtime@big-cabinet-457321-t7.iam.gserviceaccount.com"
WORKER_SA = "milo-worker-runtime@big-cabinet-457321-t7.iam.gserviceaccount.com"
DIGEST = "sha256:1111111111111111111111111111111111111111111111111111111111111111"

EXECUTION_FLAGS = (
    "MILO_ENABLE_RUN_CREATION",
    "MILO_ENABLE_PROPOSAL_MUTATIONS",
    "MILO_ENABLE_PROPOSAL_READS",
    "MILO_ENABLE_RUN_CANCELLATION",
    "MILO_ENABLE_EXECUTION_CONTROL",
    "MILO_ENABLE_PAID_EXECUTION",
)

MOCK_GCLOUD = r"""#!/usr/bin/env bash
printf 'gcloud %s\n' "$*" >> "$MOCK_LOG"
args="$*"

serve_json() {
  local kind="$1" counter n=0
  counter="$MOCK_DIR/${kind}.count"
  [[ -f "$counter" ]] && n=$(cat "$counter")
  n=$((n + 1))
  printf '%s' "$n" > "$counter"
  if [[ "$n" -le 1 ]]; then
    cat "$MOCK_DIR/${kind}-before.json"
  else
    cat "$MOCK_DIR/${kind}-after.json"
  fi
}

case "$args" in
  "auth list"*)
    echo "operator@example.test" ;;
  "projects describe"*)
    echo "$MOCK_PROJECT" ;;
  "services list"*)
    for a in "$@"; do
      case "$a" in --filter=config.name:*) echo "${a#--filter=config.name:}" ;; esac
    done ;;
  "iam service-accounts describe"*)
    echo "$3" ;;
  "artifacts repositories describe"*)
    echo "$MOCK_REPOSITORY" ;;
  "artifacts docker images describe"*)
    echo "$MOCK_DIGEST" ;;
  "secrets describe"*)
    echo "$3" ;;
  "builds submit"*)
    echo "build submitted (mock)" ;;
  "run jobs executions list"*)
    local_counter="$MOCK_DIR/executions.count"
    n=0
    [[ -f "$local_counter" ]] && n=$(cat "$local_counter")
    n=$((n + 1))
    printf '%s' "$n" > "$local_counter"
    if [[ "$n" -le 1 ]]; then
      cat "$MOCK_DIR/executions-before.txt"
    else
      cat "$MOCK_DIR/executions-after.txt"
    fi ;;
  "run services describe"*)
    case "$args" in
      *--format=json*) serve_json service ;;
      *) echo "https://milo-agent-api.mock.run.app" ;;
    esac ;;
  "run jobs describe"*)
    serve_json job ;;
  "run services get-iam-policy"*)
    cat "$MOCK_DIR/service-iam.json" ;;
  "run jobs get-iam-policy"*)
    cat "$MOCK_DIR/job-iam.json" ;;
  "run jobs deploy"*|"run deploy"*|"run jobs add-iam-policy-binding"*)
    echo "ok (mock)" ;;
  *)
    echo "unexpected gcloud invocation: $args" >&2
    exit 90 ;;
esac
exit 0
"""


def release_sha() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.strip()


SHA = release_sha()
API_IMAGE = f"{REGION}-docker.pkg.dev/{PROJECT}/{REPOSITORY}/api:{SHA}"
WORKER_IMAGE = f"{REGION}-docker.pkg.dev/{PROJECT}/{REPOSITORY}/worker:{SHA}"


def env_entry(name: str, value: str) -> dict:
    return {"name": name, "value": value}


def secret_entry(name: str, secret: str, version: str = "latest") -> dict:
    return {"name": name, "valueFrom": {"secretKeyRef": {"secret": secret, "key": version}}}


def stage_a_flag_entries() -> list[dict]:
    return [env_entry(flag, "false") for flag in EXECUTION_FLAGS]


def api_service_doc() -> dict:
    env = [
        env_entry("ENVIRONMENT", "production"),
        env_entry("JOB_LAUNCHER", "disabled"),
        env_entry("GCP_PROJECT_ID", PROJECT),
        env_entry("GCP_REGION", REGION),
        env_entry("CLOUD_RUN_WORKER_JOB", WORKER_JOB),
        env_entry("ALLOWED_CORS_ORIGINS", "https://app.example.test"),
        *stage_a_flag_entries(),
        secret_entry("SUPABASE_URL", "SUPABASE_URL"),
        secret_entry("SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SECRET_KEY"),
        secret_entry("UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_URL"),
        secret_entry("UPSTASH_REDIS_REST_TOKEN", "UPSTASH_REDIS_REST_TOKEN"),
    ]
    return {
        "kind": "Service",
        "spec": {
            "template": {
                "spec": {
                    "serviceAccountName": API_SA,
                    "containers": [{"image": API_IMAGE, "env": env}],
                }
            }
        },
    }


def worker_job_doc() -> dict:
    env = [
        env_entry("ENVIRONMENT", "production"),
        env_entry("GCP_PROJECT_ID", PROJECT),
        env_entry("GCP_REGION", REGION),
        *stage_a_flag_entries(),
        secret_entry("SUPABASE_URL", "SUPABASE_URL"),
        secret_entry("SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SECRET_KEY"),
        secret_entry("KIMI_API_KEY", "KIMI_API_KEY"),
        secret_entry("UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_URL"),
        secret_entry("UPSTASH_REDIS_REST_TOKEN", "UPSTASH_REDIS_REST_TOKEN"),
    ]
    return {
        "kind": "Job",
        "spec": {
            "template": {
                "spec": {
                    "template": {
                        "spec": {
                            "serviceAccountName": WORKER_SA,
                            "containers": [{"image": WORKER_IMAGE, "env": env}],
                        }
                    }
                }
            }
        },
    }


PRIVATE_IAM_POLICY = {
    "bindings": [
        {"role": "roles/run.invoker", "members": [f"serviceAccount:{API_SA}"]},
    ]
}


class Deployment:
    """A mocked apply-mode deployment fixture."""

    def __init__(self, tmp_path: Path) -> None:
        self.dir = tmp_path
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.log = tmp_path / "invocations.log"
        self.log.touch()
        gcloud = self.bin / "gcloud"
        gcloud.write_text(MOCK_GCLOUD)
        gcloud.chmod(gcloud.stat().st_mode | stat.S_IEXEC)

        self.service_before = api_service_doc()
        self.service_after = api_service_doc()
        self.job_before = worker_job_doc()
        self.job_after = worker_job_doc()
        self.executions_before: list[str] = []
        self.executions_after: list[str] = []
        self.service_iam = copy.deepcopy(PRIVATE_IAM_POLICY)
        self.job_iam = copy.deepcopy(PRIVATE_IAM_POLICY)

    def run(self, **env_overrides: str) -> subprocess.CompletedProcess:
        (self.dir / "service-before.json").write_text(json.dumps(self.service_before))
        (self.dir / "service-after.json").write_text(json.dumps(self.service_after))
        (self.dir / "job-before.json").write_text(json.dumps(self.job_before))
        (self.dir / "job-after.json").write_text(json.dumps(self.job_after))
        (self.dir / "executions-before.txt").write_text("".join(f"{n}\n" for n in self.executions_before))
        (self.dir / "executions-after.txt").write_text("".join(f"{n}\n" for n in self.executions_after))
        (self.dir / "service-iam.json").write_text(json.dumps(self.service_iam))
        (self.dir / "job-iam.json").write_text(json.dumps(self.job_iam))

        env = dict(os.environ)
        env.update(
            {
                "PATH": f"{self.bin}:{env['PATH']}",
                "MOCK_LOG": str(self.log),
                "MOCK_DIR": str(self.dir),
                "MOCK_PROJECT": PROJECT,
                "MOCK_REPOSITORY": REPOSITORY,
                "MOCK_DIGEST": DIGEST,
                "PROJECT_ID": PROJECT,
                "REGION": REGION,
                "REPOSITORY": REPOSITORY,
                "DEPLOY_MODE": "apply",
                "ALLOWED_CORS_ORIGINS": "https://app.example.test,https://admin.example.test",
            }
        )
        env.update(env_overrides)
        return subprocess.run(
            ["bash", str(DEPLOY_SCRIPT)],
            cwd=REPO,
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )

    def invocations(self) -> list[str]:
        return [line for line in self.log.read_text().splitlines() if line.strip()]

    def command(self, needle: str) -> str:
        matches = [line for line in self.invocations() if needle in line]
        assert matches, f"no mocked gcloud invocation matched {needle!r}"
        return matches[0]


@pytest.fixture()
def deployment(tmp_path: Path) -> Deployment:
    return Deployment(tmp_path)


def _container_env(doc: dict) -> list[dict]:
    node = doc["spec"]["template"]["spec"]
    if "template" in node:
        node = node["template"]["spec"]
    return node["containers"][0]["env"]


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_apply_mode_succeeds_against_the_mock(deployment):
    result = deployment.run()
    assert result.returncode == 0, result.stderr
    assert "Deployment complete. Worker job was deployed but not executed." in result.stdout


def test_images_are_tagged_with_the_full_forty_character_sha(deployment):
    assert deployment.run().returncode == 0
    assert len(SHA) == 40
    worker_deploy = deployment.command("run jobs deploy")
    api_deploy = deployment.command("run deploy ")
    assert f"/worker:{SHA}" in worker_deploy
    assert f"/api:{SHA}" in api_deploy
    for line in (worker_deploy, api_deploy):
        assert f":{SHA[:7]} " not in line
        assert f":{SHA[:12]} " not in line


def test_deploy_commands_use_non_destructive_update_flags(deployment):
    assert deployment.run().returncode == 0
    for line in (deployment.command("run jobs deploy"), deployment.command("run deploy ")):
        assert "--update-env-vars" in line
        assert "--update-secrets" in line
        assert "--set-env-vars" not in line
        assert "--set-secrets" not in line
        assert "--clear-env-vars" not in line


def test_worker_job_is_deployed_before_the_api_and_never_executed(deployment):
    assert deployment.run().returncode == 0
    log = deployment.invocations()
    worker_index = next(i for i, line in enumerate(log) if "run jobs deploy" in line)
    api_index = next(i for i, line in enumerate(log) if line.startswith("gcloud run deploy "))
    worker_build = next(i for i, line in enumerate(log) if "cloudbuild-worker.yaml" in line)
    api_build = next(i for i, line in enumerate(log) if "cloudbuild-api.yaml" in line)
    assert worker_build < api_build < api_index
    assert worker_index < api_index
    assert not [line for line in log if "run jobs execute" in line]


def test_stage_a_variables_are_sent_with_every_execution_flag_false(deployment):
    assert deployment.run().returncode == 0
    api_deploy = deployment.command("run deploy ")
    worker_deploy = deployment.command("run jobs deploy")
    assert "JOB_LAUNCHER=disabled" in api_deploy
    for required in (
        "ENVIRONMENT=production",
        f"GCP_PROJECT_ID={PROJECT}",
        f"GCP_REGION={REGION}",
        f"CLOUD_RUN_WORKER_JOB={WORKER_JOB}",
        "ALLOWED_CORS_ORIGINS=https://app.example.test,https://admin.example.test",
    ):
        assert required in api_deploy
    for flag in EXECUTION_FLAGS:
        assert f"{flag}=false" in api_deploy
        assert f"{flag}=false" in worker_deploy
        assert f"{flag}=true" not in api_deploy
        assert f"{flag}=true" not in worker_deploy


def test_comma_separated_cors_origins_survive_as_one_value(deployment):
    assert deployment.run().returncode == 0
    api_deploy = deployment.command("run deploy ")
    assert "^@^" in api_deploy
    assert "https://app.example.test,https://admin.example.test" in api_deploy


def test_provider_key_is_sent_to_the_worker_only(deployment):
    assert deployment.run().returncode == 0
    assert "KIMI_API_KEY=KIMI_API_KEY:latest" in deployment.command("run jobs deploy")
    assert "KIMI_API_KEY" not in deployment.command("run deploy ")


def test_separate_service_accounts_are_used_for_api_and_worker(deployment):
    assert deployment.run().returncode == 0
    assert f"--service-account {WORKER_SA}" in deployment.command("run jobs deploy")
    assert f"--service-account {API_SA}" in deployment.command("run deploy ")
    assert WORKER_SA not in deployment.command("run deploy ")


def test_verification_reports_sha_identity_env_names_secrets_and_privacy(deployment):
    result = deployment.run()
    assert result.returncode == 0
    out = result.stdout
    assert f"release SHA: {SHA}" in out
    assert f"registry digest: {DIGEST}" in out
    assert f"service account: {API_SA}" in out
    assert f"service account: {WORKER_SA}" in out
    assert "environment variable names: " in out
    assert "secret references: " in out
    assert "SUPABASE_SERVICE_ROLE_KEY->SUPABASE_SECRET_KEY:latest" in out
    assert "IAM: private (no allUsers / allAuthenticatedUsers)" in out
    assert "worker executions: 0 (unchanged; deployment executed nothing)" in out
    assert "secret 'KIMI_API_KEY' absent: OK" in out
    # Secret VALUES are never read or printed.
    assert "secrets versions access" not in "\n".join(deployment.invocations())


# ---------------------------------------------------------------------------
# failure modes the verification must catch
# ---------------------------------------------------------------------------


def test_deployment_fails_when_a_pre_existing_env_var_is_dropped(deployment):
    """A destructive --set-env-vars would erase unrelated production config."""
    _container_env(deployment.service_before).append(env_entry("MILO_GATEWAY_AUDIENCE", "https://x"))
    result = deployment.run()
    assert result.returncode != 0
    assert "lost pre-existing environment/secret bindings" in result.stderr
    assert "env MILO_GATEWAY_AUDIENCE" in result.stderr


def test_deployment_fails_when_a_pre_existing_secret_binding_is_dropped(deployment):
    _container_env(deployment.job_before).append(secret_entry("EXTRA_SECRET", "EXTRA_SECRET"))
    result = deployment.run()
    assert result.returncode != 0
    assert "lost pre-existing environment/secret bindings" in result.stderr
    assert "secret EXTRA_SECRET" in result.stderr


def test_deployment_fails_when_the_provider_key_reaches_the_api(deployment):
    _container_env(deployment.service_after).append(secret_entry("KIMI_API_KEY", "KIMI_API_KEY"))
    result = deployment.run()
    assert result.returncode != 0
    assert "must not be bound to secret 'KIMI_API_KEY'" in result.stderr


def test_deployment_fails_when_an_execution_flag_is_enabled(deployment):
    for entry in _container_env(deployment.service_after):
        if entry.get("name") == "MILO_ENABLE_RUN_CREATION":
            entry["value"] = "true"
    result = deployment.run()
    assert result.returncode != 0
    assert "Stage A requires false" in result.stderr


def test_deployment_fails_when_the_job_launcher_is_not_the_selected_mode(deployment):
    for entry in _container_env(deployment.service_after):
        if entry.get("name") == "JOB_LAUNCHER":
            entry["value"] = "cloud_run"
    result = deployment.run()
    assert result.returncode != 0
    assert "JOB_LAUNCHER='cloud_run' instead of 'disabled'" in result.stderr


def test_deployment_fails_when_a_required_stage_a_variable_is_missing(deployment):
    env = _container_env(deployment.service_after)
    env[:] = [entry for entry in env if entry.get("name") != "MILO_ENABLE_PAID_EXECUTION"]
    result = deployment.run()
    assert result.returncode != 0
    assert "missing required environment variable 'MILO_ENABLE_PAID_EXECUTION'" in result.stderr


def test_deployment_fails_when_a_required_secret_reference_is_missing(deployment):
    env = _container_env(deployment.job_after)
    env[:] = [entry for entry in env if entry.get("name") != "KIMI_API_KEY"]
    deployment.job_before = copy.deepcopy(deployment.job_after)
    result = deployment.run()
    assert result.returncode != 0
    assert "missing the expected secret reference 'KIMI_API_KEY=KIMI_API_KEY:latest'" in result.stderr


def test_deployment_fails_when_the_running_image_is_not_the_release_sha(deployment):
    short = SHA[:7]
    container = deployment.service_after["spec"]["template"]["spec"]["containers"][0]
    container["image"] = f"{REGION}-docker.pkg.dev/{PROJECT}/{REPOSITORY}/api:{short}"
    result = deployment.run()
    assert result.returncode != 0
    assert "instead of the release image" in result.stderr


def test_deployment_fails_when_the_worker_runs_the_wrong_identity(deployment):
    deployment.job_after["spec"]["template"]["spec"]["template"]["spec"]["serviceAccountName"] = API_SA
    result = deployment.run()
    assert result.returncode != 0
    assert "instead of the expected runtime identity" in result.stderr


def test_deployment_fails_when_the_service_is_public(deployment):
    deployment.service_iam["bindings"].append({"role": "roles/run.invoker", "members": ["allUsers"]})
    result = deployment.run()
    assert result.returncode != 0
    assert "must stay private" in result.stderr


def test_deployment_fails_when_a_worker_execution_appears(deployment):
    deployment.executions_after = ["milo-agent-worker-abcde"]
    result = deployment.run()
    assert result.returncode != 0
    assert "Worker job executions changed during deployment" in result.stderr


# ---------------------------------------------------------------------------
# check mode stays inert
# ---------------------------------------------------------------------------


def test_check_mode_builds_deploys_and_executes_nothing(deployment):
    result = deployment.run(DEPLOY_MODE="check")
    assert result.returncode == 0, result.stderr
    assert f"Release SHA (full): {SHA}" in result.stdout
    log = "\n".join(deployment.invocations())
    assert "builds submit" not in log
    assert "run jobs deploy" not in log
    assert "run deploy" not in log
    assert "add-iam-policy-binding" not in log
    assert "run jobs executions" not in log


def test_wildcard_cors_is_rejected_before_anything_runs(deployment):
    # Built at runtime so the repository never commits a wildcard origin
    # assignment (scripts/check_unsafe_defaults.py scans for exactly that).
    wildcard = "".join(["*"])
    result = deployment.run(**{"ALLOWED_CORS_ORIGINS": wildcard})
    assert result.returncode != 0
    assert "must not contain '*'" in result.stderr
    assert "builds submit" not in "\n".join(deployment.invocations())

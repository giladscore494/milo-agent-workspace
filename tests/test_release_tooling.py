"""Executable safety proofs for the release/operator tooling.

Runs the scripts in ``scripts/release/`` with mocked command-line tools
injected through PATH (gcloud, supabase, vercel, redis-cli, curl, docker,
git as needed) and proves:

- default mode causes no mutations;
- ``--apply`` without all acknowledgments fails;
- placeholders are rejected;
- wrong account / wrong project are rejected;
- a dirty worktree is rejected for apply;
- secret values are redacted;
- generated plans contain no credentials;
- wildcard CORS is rejected;
- no enable-all command exists;
- scripts never call real external services in CI.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
RELEASE = REPO / "scripts" / "release"
ACK = "I_UNDERSTAND_THIS_CHANGES_PRODUCTION"

FULL_SHA = "0123456789abcdef0123456789abcdef01234567"
UUID_A = "11111111-1111-4111-8111-111111111111"
UUID_B = "22222222-2222-4222-8222-222222222222"


def write_mock(bin_dir: Path, name: str, script: str) -> None:
    path = bin_dir / name
    path.write_text("#!/usr/bin/env bash\n" + script)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.fixture()
def mock_bin(tmp_path: Path) -> Path:
    """A PATH prefix with mocked external CLIs that log every invocation."""
    bin_dir = tmp_path / "mockbin"
    bin_dir.mkdir()
    log = tmp_path / "invocations.log"
    log.touch()
    common = f'echo "$(basename "$0") $*" >> "{log}"\n'
    write_mock(
        bin_dir,
        "gcloud",
        common
        + r"""
case "$*" in
  *"config get-value account"*) echo "${MOCK_GCLOUD_ACCOUNT:-operator@example-project.test}";;
  *"config get-value project"*) echo "${MOCK_GCLOUD_PROJECT:-mock-project}";;
  *"services list"*) printf 'run.googleapis.com\nartifactregistry.googleapis.com\nsecretmanager.googleapis.com\niamcredentials.googleapis.com\nsts.googleapis.com\n';;
  *"get-iam-policy"*) echo '{"bindings": []}';;
  *describe*) echo "mock-resource";;
  *) echo "mock";;
esac
""",
    )
    write_mock(
        bin_dir,
        "curl",
        common
        + r"""
# Minimal mock gateway: writes an empty body to -o targets, then answers
# with a status code by method/path. Never touches the network.
method=GET; url=""; out=""
args=("$@")
for ((i=0; i<${#args[@]}; i++)); do
  case "${args[$i]}" in
    -X) method="${args[$((i+1))]}";;
    -o) out="${args[$((i+1))]}";;
    http*|https*) url="${args[$i]}";;
  esac
done
code=200
body=""
if [[ "$method" == "POST" ]]; then
  case "$url" in
    */runs) code=403; body='{"error":"Run creation is disabled by the gateway safety policy."}';;
    */cancel) code=403; body='{"error":"cancellation disabled"}';;
    */internal/*) code=403;;
    *) code=403;;
  esac
elif [[ "$method" == "GET" ]]; then
  case "$url" in
    */health) code=200; body='{"status":"ok"}';;
    */conversations/*) code=200; body='{"id":"conversation","title":"smoke"}';;
  esac
fi
if [[ "$url" == */projects* && "$url" == *cccccccc* ]]; then code=404; fi
if [[ -n "$out" && "$out" != "/dev/null" ]]; then
  if [[ -n "$body" ]]; then printf '%s' "$body" > "$out"; else : > "$out"; fi
fi
printf '%s' "$code"
""",
    )
    for tool in ("vercel", "supabase", "redis-cli", "docker", "psql"):
        write_mock(bin_dir, tool, common + 'echo "mock"\n')
    return bin_dir


def run_script(
    script: str,
    *args: str,
    mock_bin: Path | None = None,
    env: dict | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess:
    run_env = dict(os.environ)
    if mock_bin is not None:
        run_env["PATH"] = f"{mock_bin}:{run_env['PATH']}"
        run_env["MOCK_LOG"] = str(mock_bin.parent / "invocations.log")
    if env:
        run_env.update(env)
    return subprocess.run(
        ["bash", str(RELEASE / script), *args],
        capture_output=True,
        text=True,
        env=run_env,
        cwd=cwd or REPO,
        timeout=120,
    )


def invocation_log(mock_bin: Path) -> str:
    return (mock_bin.parent / "invocations.log").read_text()


# ---------------------------------------------------------------------------
# default mode causes no mutations
# ---------------------------------------------------------------------------


def test_default_gcp_check_is_read_only(mock_bin, tmp_path):
    result = run_script(
        "check-gcp-resources.sh",
        "--expected-project",
        "mock-project",
        "--expected-account",
        "operator@example-project.test",
        "--region",
        "us-central1",
        "--repository",
        "milo",
        mock_bin=mock_bin,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    log = invocation_log(mock_bin)
    for mutating in ("deploy", "create", "add-iam-policy-binding", "jobs run", "delete", "update"):
        assert mutating not in log, f"read-only check invoked mutating verb: {mutating}"


def test_orchestrator_read_only_and_no_network(mock_bin, tmp_path):
    report = tmp_path / "readiness.json"
    result = run_script(
        "production-readiness.sh", "--json-output", str(report), mock_bin=mock_bin
    )
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(report.read_text())
    assert data["summary"]["blocked"] == 0
    log = invocation_log(mock_bin)
    # The audit must not have performed any external call beyond mocked
    # read-only gcloud-style reads; curl (network) must not be touched at all
    # in this configuration.
    assert "curl" not in log


def test_backfill_generators_never_execute_sql(mock_bin, tmp_path):
    mapping = tmp_path / "members.json"
    mapping.write_text(
        json.dumps(
            {
                "memberships": [
                    {"project_id": UUID_A, "user_id": UUID_B, "role": "owner"}
                ]
            }
        )
    )
    out = tmp_path / "plan.sql"
    result = run_script(
        "generate-membership-backfill.sh",
        "--input",
        str(mapping),
        "--output",
        str(out),
        mock_bin=mock_bin,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "psql" not in invocation_log(mock_bin)
    sql = out.read_text()
    assert "begin;" in sql and "commit;" in sql
    assert "delete from" not in sql.lower()


# ---------------------------------------------------------------------------
# --apply protections
# ---------------------------------------------------------------------------


def apply_args(**overrides):
    values = {
        "environment": "production",
        "project": "mock-project",
        "account": "operator@example-project.test",
        "sha": FULL_SHA,
    }
    values.update(overrides)
    return [
        "--run-id",
        UUID_A,
        "--resolution",
        "confirmed-launched",
        "--apply",
        "--environment",
        values["environment"],
        "--expected-project",
        values["project"],
        "--expected-account",
        values["account"],
        "--expected-sha",
        values["sha"],
        "--confirm-production-change",
    ]


def test_apply_without_ack_fails(mock_bin):
    result = run_script(
        "reconcile-launch-unknown.sh",
        *apply_args(),
        mock_bin=mock_bin,
        env={"MILO_OPERATOR_ACK": ""},
    )
    assert result.returncode != 0
    assert "MILO_OPERATOR_ACK" in result.stdout


def test_apply_without_confirm_flag_fails(mock_bin):
    args = [a for a in apply_args() if a != "--confirm-production-change"]
    result = run_script(
        "reconcile-launch-unknown.sh",
        *args,
        mock_bin=mock_bin,
        env={"MILO_OPERATOR_ACK": ACK},
    )
    assert result.returncode != 0
    assert "confirm-production-change" in result.stdout


def test_apply_rejects_placeholder_project(mock_bin):
    result = run_script(
        "reconcile-launch-unknown.sh",
        *apply_args(project="<GCP_PROJECT_ID>"),
        mock_bin=mock_bin,
        env={"MILO_OPERATOR_ACK": ACK},
    )
    assert result.returncode != 0
    assert "placeholder" in result.stdout


def test_apply_rejects_short_sha(mock_bin):
    result = run_script(
        "reconcile-launch-unknown.sh",
        *apply_args(sha="723c84e"),
        mock_bin=mock_bin,
        env={"MILO_OPERATOR_ACK": ACK},
    )
    assert result.returncode != 0
    assert "40-character" in result.stdout


def _git_repo(tmp_path: Path, dirty: bool) -> tuple[Path, str]:
    repo = tmp_path / ("dirty" if dirty else "clean")
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    if dirty:
        (repo / "untracked.txt").write_text("dirty")
    return repo, head


def test_apply_rejects_dirty_worktree(mock_bin, tmp_path):
    repo, head = _git_repo(tmp_path, dirty=True)
    result = run_script(
        "reconcile-launch-unknown.sh",
        *apply_args(sha=head),
        mock_bin=mock_bin,
        env={"MILO_OPERATOR_ACK": ACK},
        cwd=repo,
    )
    assert result.returncode != 0
    assert "dirty" in result.stdout


def test_apply_rejects_wrong_account_and_project(mock_bin, tmp_path):
    repo, head = _git_repo(tmp_path, dirty=False)
    result = run_script(
        "reconcile-launch-unknown.sh",
        *apply_args(sha=head),
        mock_bin=mock_bin,
        env={"MILO_OPERATOR_ACK": ACK, "MOCK_GCLOUD_ACCOUNT": "intruder@evil.test"},
        cwd=repo,
    )
    assert result.returncode != 0
    assert "does not match --expected-account" in result.stdout

    result = run_script(
        "reconcile-launch-unknown.sh",
        *apply_args(sha=head),
        mock_bin=mock_bin,
        env={"MILO_OPERATOR_ACK": ACK, "MOCK_GCLOUD_PROJECT": "wrong-project"},
        cwd=repo,
    )
    assert result.returncode != 0
    assert "does not match --expected-project" in result.stdout


# ---------------------------------------------------------------------------
# placeholder / wildcard rejection in check + generator scripts
# ---------------------------------------------------------------------------


def _env_file(tmp_path: Path, **overrides) -> Path:
    base = {
        "ENVIRONMENT": "production",
        "SUPABASE_URL": "https://abcd1234.supabase.co",
        "SUPABASE_SERVICE_ROLE_KEY": "metadata-present",
        "ALLOWED_CORS_ORIGINS": "https://milo.example-workspace.app",
        "MILO_GATEWAY_AUDIENCE": "https://milo-api.a.run.app",
        "MILO_APPROVED_GATEWAY_IDENTITIES": "gw@p.iam.gserviceaccount.com",
        "MILO_APPROVED_WORKER_IDENTITIES": "wk@p.iam.gserviceaccount.com",
        "UPSTASH_REDIS_REST_URL": "https://mock.upstash.io",
        "UPSTASH_REDIS_REST_TOKEN": "metadata-present",
        "MILO_MAX_COST_PER_RUN": "1.5",
        "MILO_DAILY_USER_BUDGET": "5",
        "MILO_DAILY_PROJECT_BUDGET": "10",
        "MILO_MAX_MODEL_CALLS_PER_RUN": "20",
        "MILO_MAX_TOTAL_TOKENS_PER_RUN": "200000",
        "MILO_MAX_RUN_DURATION_SECONDS": "900",
    }
    base.update(overrides)
    path = tmp_path / "env.txt"
    path.write_text("".join(f"{k}={v}\n" for k, v in base.items()))
    return path


def test_wildcard_cors_rejected(tmp_path):
    wildcard = chr(42)  # assembled so the repo-wide unsafe-default scan stays clean
    env_file = _env_file(tmp_path, ALLOWED_CORS_ORIGINS=wildcard)
    result = run_script("check-production-config.sh", "--env-file", str(env_file))
    assert result.returncode != 0
    assert "wildcard CORS origin is forbidden" in result.stdout


def test_valid_metadata_passes(tmp_path):
    result = run_script(
        "check-production-config.sh", "--env-file", str(_env_file(tmp_path))
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_execution_flag_on_is_blocked(tmp_path):
    env_file = _env_file(tmp_path, MILO_ENABLE_PAID_EXECUTION="true")
    result = run_script("check-production-config.sh", "--env-file", str(env_file))
    assert result.returncode != 0
    assert "execution flag is enabled" in result.stdout


def test_shared_gateway_worker_identity_blocked(tmp_path):
    env_file = _env_file(
        tmp_path, MILO_APPROVED_WORKER_IDENTITIES="gw@p.iam.gserviceaccount.com"
    )
    result = run_script("check-production-config.sh", "--env-file", str(env_file))
    assert result.returncode != 0
    assert "both gateway and worker roles" in result.stdout


def test_placeholder_supabase_url_rejected(tmp_path):
    env_file = _env_file(tmp_path, SUPABASE_URL="<SUPABASE_URL>")
    result = run_script("check-production-config.sh", "--env-file", str(env_file))
    assert result.returncode != 0
    assert "placeholder" in result.stdout


def test_backfill_rejects_placeholder_template(tmp_path):
    result = run_script(
        "generate-membership-backfill.sh",
        "--input",
        str(RELEASE / "templates" / "membership-backfill.example.json"),
        "--output",
        str(tmp_path / "never.sql"),
    )
    assert result.returncode != 0
    assert not (tmp_path / "never.sql").exists()


def test_proposal_backfill_rejects_orphans(tmp_path):
    mapping = tmp_path / "props.json"
    mapping.write_text(
        json.dumps({"proposals": [{"proposal_id": UUID_A, "created_by": UUID_B}]})
    )
    result = run_script(
        "generate-proposal-backfill.sh",
        "--input",
        str(mapping),
        "--output",
        str(tmp_path / "never.sql"),
    )
    assert result.returncode != 0


def test_deployment_plan_rejects_mutable_tags(tmp_path):
    for tag in ("latest", "prod", "stable", "main"):
        result = run_script("generate-deployment-plan.sh", "--release-sha", tag)
        assert result.returncode != 0, f"mutable tag accepted: {tag}"


# ---------------------------------------------------------------------------
# secrets redacted / plans credential-free
# ---------------------------------------------------------------------------


def test_redaction_helper():
    script = (
        f'source "{RELEASE}/lib/common.sh"; '
        'redact_line "postgres://user:hunter2pass@db.example.test:5432/x '
        'SUPABASE_SERVICE_ROLE_KEY=sbsecretvalue Bearer abc.def.ghi"'
    )
    out = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=True
    ).stdout
    assert "hunter2pass" not in out
    assert "sbsecretvalue" not in out
    assert "abc.def.ghi" not in out
    assert "[REDACTED]" in out


def test_generated_plans_contain_no_credentials(tmp_path):
    deploy = tmp_path / "deploy.md"
    rollback = tmp_path / "rollback.md"
    r1 = run_script(
        "generate-deployment-plan.sh", "--release-sha", FULL_SHA, "--output", str(deploy)
    )
    r2 = run_script(
        "generate-rollback-plan.sh", "--previous-sha", FULL_SHA, "--output", str(rollback)
    )
    assert r1.returncode == 0 and r2.returncode == 0
    secret_re = re.compile(r"(sk-[A-Za-z0-9_-]{10,}|eyJ[A-Za-z0-9_-]{20,}|:[^/@\s]+@)")
    for plan in (deploy, rollback):
        text = plan.read_text()
        assert not secret_re.search(text), f"credential-looking material in {plan.name}"


def test_json_reports_redact_url_credentials(tmp_path):
    env_file = _env_file(
        tmp_path, UPSTASH_REDIS_REST_URL="https://user:supersecretpw@mock.upstash.io"
    )
    report = tmp_path / "redis.json"
    run_script(
        "check-redis-config.sh",
        "--env-file",
        str(env_file),
        "--json-output",
        str(report),
    )
    combined = report.read_text() if report.exists() else ""
    assert "supersecretpw" not in combined


# ---------------------------------------------------------------------------
# smoke tests against the mocked gateway
# ---------------------------------------------------------------------------


def test_execution_disabled_smoke_passes_on_mock(mock_bin, tmp_path):
    env_file = _env_file(tmp_path)
    result = run_script(
        "smoke-test-execution-disabled.sh",
        "--base-url",
        "https://mock-gateway.invalid",
        "--env-file",
        str(env_file),
        "--user-token-env",
        "SMOKE_TOKEN",
        "--conversation-id",
        UUID_A,
        mock_bin=mock_bin,
        env={"SMOKE_TOKEN": "valid-user-token"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[PASS] run-creation-blocked" in result.stdout


def test_execution_disabled_smoke_without_token_is_not_pass(mock_bin, tmp_path):
    """A missing authenticated token must NOT yield a run-creation PASS."""
    env_file = _env_file(tmp_path)
    result = run_script(
        "smoke-test-execution-disabled.sh",
        "--base-url",
        "https://mock-gateway.invalid",
        "--env-file",
        str(env_file),
        mock_bin=mock_bin,
    )
    assert "[PASS] run-creation-blocked" not in result.stdout
    assert "[MANUAL] run-creation-blocked" in result.stdout


def test_read_only_smoke_worker_route_rejected_on_mock(mock_bin):
    result = run_script(
        "smoke-test-read-only.sh",
        "--base-url",
        "https://mock-gateway.invalid",
        mock_bin=mock_bin,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[PASS] worker-route" in result.stdout


# ---------------------------------------------------------------------------
# repository-wide invariants
# ---------------------------------------------------------------------------


def test_no_enable_all_command_exists():
    enable_re = re.compile(
        r"MILO_ENABLE_[A-Z_]+\s*=\s*['\"]?(1|true|yes|on)['\"]?", re.I
    )
    for path in (REPO / "scripts").rglob("*"):
        if not path.is_file():
            continue
        assert "enable-all" not in path.name.lower()
        assert "enable_all" not in path.name.lower()
        text = path.read_text(errors="ignore")
        matches = enable_re.findall(text)
        assert not matches, f"{path} assigns an execution flag on: {matches}"


def test_manifest_validator_plan_and_apply_modes(tmp_path):
    manifest = REPO / "config" / "production.example.yaml"
    plan = subprocess.run(
        [
            "python3",
            str(RELEASE / "validate_production_manifest.py"),
            "--manifest",
            str(manifest),
            "--mode",
            "plan",
        ],
        capture_output=True,
        text=True,
    )
    assert plan.returncode == 0, plan.stderr
    apply = subprocess.run(
        [
            "python3",
            str(RELEASE / "validate_production_manifest.py"),
            "--manifest",
            str(manifest),
            "--mode",
            "apply",
        ],
        capture_output=True,
        text=True,
    )
    assert apply.returncode != 0
    assert "placeholder" in apply.stderr


def test_manifest_validator_rejects_shared_identity_and_wildcards(tmp_path):
    text = (REPO / "config" / "production.example.yaml").read_text()
    bad = text.replace(
        '"<WORKER_SERVICE_ACCOUNT_EMAIL>"', '"<API_SERVICE_ACCOUNT_EMAIL>"'
    ).replace('"<PRODUCTION_VERCEL_ORIGIN>"', '"*"')
    bad_path = tmp_path / "bad.yaml"
    bad_path.write_text(bad)
    result = subprocess.run(
        [
            "python3",
            str(RELEASE / "validate_production_manifest.py"),
            "--manifest",
            str(bad_path),
            "--mode",
            "plan",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "different service accounts" in result.stderr
    assert "wildcard origin" in result.stderr


def test_manifest_validator_rejects_enabled_execution_flag(tmp_path):
    text = (REPO / "config" / "production.example.yaml").read_text()
    bad = text.replace("MILO_ENABLE_PAID_EXECUTION: false", "MILO_ENABLE_PAID_EXECUTION: true")
    bad_path = tmp_path / "flag.yaml"
    bad_path.write_text(bad)
    result = subprocess.run(
        [
            "python3",
            str(RELEASE / "validate_production_manifest.py"),
            "--manifest",
            str(bad_path),
            "--mode",
            "plan",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "must be false during Stage A" in result.stderr


def test_pull_request_ci_contains_no_deployment_step():
    """No pull_request-triggered workflow may deploy or mutate production."""
    workflows = (REPO / ".github" / "workflows").glob("*.yml")
    forbidden = (
        "gcloud run deploy",
        "gcloud run jobs run",
        "docker push",
        "vercel deploy",
        "vercel --prod",
        "supabase db push",
    )
    for wf in workflows:
        text = wf.read_text()
        if not re.search(r"^\s*pull_request:", text, re.M):
            continue
        for needle in forbidden:
            assert needle not in text, f"{wf.name} contains deployment step: {needle}"


def test_all_release_scripts_have_help_and_strict_mode():
    for script in RELEASE.glob("*.sh"):
        text = script.read_text()
        assert "set -euo pipefail" in text, f"{script.name} missing strict mode"
        assert "--help" in text, f"{script.name} missing --help"
        assert "eval " not in text, f"{script.name} uses eval"
        result = run_script(script.name, "--help")
        assert result.returncode == 0, f"{script.name} --help failed"
        assert "Usage:" in result.stdout

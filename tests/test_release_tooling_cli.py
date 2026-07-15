"""Strict external-CLI proofs for the release/operator tooling.

Unlike the permissive smoke mocks in ``test_release_tooling.py``, the mocks
here FAIL LOUDLY (nonzero exit + ``MOCK-UNAUTHORIZED`` marker) on any command
the test did not explicitly authorize, reject every mutation verb, and return
realistic ``gcloud`` / ``vercel`` / ``psql`` / ``curl`` output built from the
fixtures under ``tests/fixtures/``. They exist to catch the exact class of
real-world defects the earlier mocked CI suite missed:

- wrong Cloud Run Job service-account metadata path;
- an existing job with no explicit SA misreported as "job not found";
- unsupported/invented Vercel CLI syntax;
- a mutation "success" that changed zero rows;
- a bare 401 accepted as proof that execution is disabled.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
RELEASE = REPO / "scripts" / "release"
FIX = REPO / "tests" / "fixtures"
CR = FIX / "cloud_run"
ACK = "I_UNDERSTAND_THIS_CHANGES_PRODUCTION"

FULL_SHA = "0123456789abcdef0123456789abcdef01234567"
UUID_A = "11111111-1111-4111-8111-111111111111"

PROJECT = "milo-prod"
REGION = "us-central1"
API_SA = "milo-api-sa@milo-prod.iam.gserviceaccount.com"
WORKER_SA = "milo-worker-sa@milo-prod.iam.gserviceaccount.com"

# --------------------------------------------------------------------------
# strict mock CLIs
# --------------------------------------------------------------------------

GCLOUD_MOCK = r"""#!/usr/bin/env bash
printf 'gcloud %s\n' "$*" >> "$MOCK_LOG"
all="$*"
case "$all" in
  *"run deploy"*|*"jobs deploy"*|*"jobs run"*|*"jobs execute"*|*"services update"*|\
  *"jobs update"*|*"add-iam-policy-binding"*|*"remove-iam-policy-binding"*|\
  *"set-iam-policy"*|*"secrets create"*|*"versions add"*|*"services delete"*|\
  *"jobs delete"*|*"secrets delete"*|*"services replace"*)
    printf 'MOCK-FORBIDDEN gcloud mutation: %s\n' "$all" >&2; exit 97;;
esac
proj=""; region=""
args=("$@")
for ((i=0; i<${#args[@]}; i++)); do
  case "${args[$i]}" in
    --project) proj="${args[$((i+1))]}";;
    --region) region="${args[$((i+1))]}";;
  esac
done
if [[ -n "${MOCK_EXPECT_PROJECT:-}" && -n "$proj" && "$proj" != "$MOCK_EXPECT_PROJECT" ]]; then
  printf 'MOCK wrong project: %s\n' "$proj" >&2; exit 96
fi
if [[ -n "${MOCK_EXPECT_REGION:-}" && -n "$region" && "$region" != "$MOCK_EXPECT_REGION" ]]; then
  printf 'MOCK wrong region: %s\n' "$region" >&2; exit 96
fi
api_iam="${MOCK_API_IAM:-}"; [[ -z "$api_iam" ]] && api_iam='{"bindings":[]}'
job_iam="${MOCK_JOB_IAM:-}"; [[ -z "$job_iam" ]] && job_iam='{"bindings":[]}'
proj_policy="${MOCK_PROJECT_POLICY:-}"; [[ -z "$proj_policy" ]] && proj_policy='{"bindings":[]}'
secret_policy="${MOCK_SECRET_POLICY:-}"; [[ -z "$secret_policy" ]] && secret_policy='{"bindings":[]}'
case "$all" in
  *"config get-value account"*) printf '%s\n' "${MOCK_GCLOUD_ACCOUNT:-operator@milo-prod.iam.gserviceaccount.com}";;
  *"config get-value project"*) printf '%s\n' "${MOCK_GCLOUD_PROJECT:-milo-prod}";;
  *"services list"*) printf 'run.googleapis.com\nartifactregistry.googleapis.com\nsecretmanager.googleapis.com\niamcredentials.googleapis.com\nsts.googleapis.com\n';;
  *"run services describe"*)
    case "${MOCK_SERVICE:-present}" in
      missing) printf 'ERROR: (gcloud.run.services.describe) NOT_FOUND: service\n' >&2; exit 1;;
      *) cat "${MOCK_SERVICE_JSON:?MOCK_SERVICE_JSON unset}";;
    esac;;
  *"run jobs describe"*)
    case "${MOCK_JOB:-present}" in
      missing) printf 'ERROR: (gcloud.run.jobs.describe) NOT_FOUND: Resource milo-worker not found\n' >&2; exit 1;;
      error) printf 'ERROR: (gcloud.run.jobs.describe) PERMISSION_DENIED\n' >&2; exit 1;;
      *) cat "${MOCK_JOB_JSON:?MOCK_JOB_JSON unset}";;
    esac;;
  *"run services get-iam-policy"*) printf '%s\n' "$api_iam";;
  *"run jobs get-iam-policy"*) printf '%s\n' "$job_iam";;
  *"artifacts repositories describe"*) printf 'projects/p/locations/r/repositories/milo\n';;
  *"iam service-accounts describe"*) printf 'sa@milo-prod.iam.gserviceaccount.com\n';;
  *"projects get-iam-policy"*) printf '%s\n' "$proj_policy";;
  *"secrets versions list"*) printf '%s' "${MOCK_SECRET_VERSIONS-1}";;
  *"secrets get-iam-policy"*) printf '%s\n' "$secret_policy";;
  *"secrets list"*) printf '%s' "${MOCK_SECRETS_LIST-}";;
  *) printf 'MOCK-UNAUTHORIZED gcloud command: %s\n' "$all" >&2; exit 98;;
esac
"""

VERCEL_MOCK = r"""#!/usr/bin/env bash
printf 'vercel %s\n' "$*" >> "$MOCK_LOG"
all="$*"
case "$all" in
  *deploy*|*"env add"*|*"env rm"*|*"env remove"*|*promote*|*link*|*"--prod"*)
    printf 'MOCK-FORBIDDEN vercel mutation/deploy: %s\n' "$all" >&2; exit 97;;
esac
case "$all" in
  *"--scope-project"*|*"--environment"*)
    printf 'Error: unknown or unsupported option in: %s\n' "$all" >&2; exit 1;;
esac
case "$all" in
  whoami*)
    if [[ "${MOCK_VERCEL_AUTH:-ok}" == "fail" ]]; then
      printf 'Error: You are not logged in. Run vercel login.\n' >&2; exit 1
    fi
    printf '%s\n' "${MOCK_VERCEL_USER:-milo-team}";;
  "env ls production"*)
    case "${MOCK_VERCEL_ENV:-ok}" in
      authfail) printf 'Error: Not authorized to access this project.\n' >&2; exit 1;;
      unlinked) printf 'Error: Your codebase is not linked to a project. Run vercel link.\n' >&2; exit 1;;
      *) cat "${MOCK_VERCEL_ENV_FILE:?MOCK_VERCEL_ENV_FILE unset}";;
    esac;;
  *) printf 'MOCK-UNAUTHORIZED vercel command: %s\n' "$all" >&2; exit 98;;
esac
"""

PSQL_MOCK = r"""#!/usr/bin/env bash
printf 'psql %s\n' "$*" >> "$MOCK_LOG"
sql=""
args=("$@")
for ((i=0; i<${#args[@]}; i++)); do
  [[ "${args[$i]}" == "-c" ]] && sql="${args[$((i+1))]}"
done
case "$sql" in
  *"with upd as (update"*)
    if [[ "${MOCK_PSQL_FAIL:-}" == "update" ]]; then printf 'ERROR: deadlock detected\n' >&2; exit 1; fi
    upd="${MOCK_PSQL_UPDATE:-}"; [[ -z "$upd" ]] && upd='1|launched|queued'
    printf '%s\n' "$upd";;
  *"update public.runs"*|*"insert into"*|*"delete from"*)
    printf 'MOCK-UNAUTHORIZED unguarded mutation: %s\n' "$sql" >&2; exit 98;;
  *"select launch_state ||"*)
    if [[ "${MOCK_PSQL_FAIL:-}" == "read" ]]; then printf 'ERROR: connection refused\n' >&2; exit 1; fi
    printf '%s\n' "${MOCK_PSQL_READ-}";;
  *"model_call_budget_reservations"*) printf '%s\n' "${MOCK_PSQL_BUDGET:-0}";;
  *"launch_state = 'launch_unknown'"*) printf '%s\n' "${MOCK_PSQL_LIST-}";;
  *) printf 'MOCK-UNAUTHORIZED psql query: %s\n' "$sql" >&2; exit 98;;
esac
"""

CURL_MOCK = r"""#!/usr/bin/env bash
printf 'curl %s\n' "$*" >> "$MOCK_LOG"
method=GET; out=/dev/null; url=""; hasauth=0
args=("$@")
for ((i=0; i<${#args[@]}; i++)); do
  case "${args[$i]}" in
    -X) method="${args[$((i+1))]}";;
    -o) out="${args[$((i+1))]}";;
    -H) [[ "${args[$((i+1))]}" == Authorization:* ]] && hasauth=1;;
    http://*|https://*) url="${args[$i]}";;
  esac
done
run_body="${MOCK_CURL_RUN_BODY:-}"
[[ -z "$run_body" ]] && run_body='{"error":"Run creation is disabled by the gateway safety policy."}'
code=200; body='{"status":"ok"}'
case "$url" in
  */health) code=200; body='{"status":"ok"}';;
  */runs)
    if [[ "$method" == "POST" ]]; then
      if [[ "$hasauth" -eq 1 ]]; then
        code="${MOCK_CURL_RUN_CODE:-403}"; body="$run_body"
      else
        code=401; body='{"error":"unauthorized"}'
      fi
    fi;;
  */cancel) code=403; body='{"error":"cancellation disabled"}';;
  */ping) code="${MOCK_CURL_PING_CODE:-200}"; body="PONG";;
  *) code=200; body='{"status":"ok"}';;
esac
[[ "$out" != "/dev/null" ]] && printf '%s' "$body" > "$out"
printf '%s' "$code"
"""

MOCKS = {
    "gcloud": GCLOUD_MOCK,
    "vercel": VERCEL_MOCK,
    "psql": PSQL_MOCK,
    "curl": CURL_MOCK,
}


@pytest.fixture()
def strict_bin(tmp_path: Path):
    """A PATH prefix of strict mock CLIs plus a helper to run release scripts."""
    bin_dir = tmp_path / "strictbin"
    bin_dir.mkdir()
    log = tmp_path / "invocations.log"
    log.touch()
    for name, body in MOCKS.items():
        path = bin_dir / name
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def run(script: str, *args: str, env: dict | None = None, cwd: Path | None = None):
        run_env = dict(os.environ)
        run_env["PATH"] = f"{bin_dir}:{run_env['PATH']}"
        run_env["MOCK_LOG"] = str(log)
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

    run.log = log  # type: ignore[attr-defined]
    run.bin_dir = bin_dir  # type: ignore[attr-defined]
    return run


def read_log(run) -> str:
    return run.log.read_text()


def assert_no_mock_errors(result):
    combined = result.stdout + result.stderr
    for marker in ("MOCK-UNAUTHORIZED", "MOCK-FORBIDDEN", "MOCK wrong project", "MOCK wrong region"):
        assert marker not in combined, f"strict mock rejected a command: {combined}"


# --------------------------------------------------------------------------
# gcloud: Cloud Run service + job structure
# --------------------------------------------------------------------------


def _gcp_env(**over) -> dict:
    env = {
        "MOCK_EXPECT_PROJECT": PROJECT,
        "MOCK_EXPECT_REGION": REGION,
        "MOCK_SERVICE_JSON": str(CR / "service.json"),
        "MOCK_JOB_JSON": str(CR / "job.json"),
    }
    env.update(over)
    return env


def test_gcp_service_and_job_pass_with_correct_paths(strict_bin):
    result = strict_bin(
        "check-gcp-resources.sh",
        "--expected-project", PROJECT,
        "--region", REGION,
        "--api-service", "milo-api",
        "--worker-job", "milo-worker",
        "--api-sa", API_SA,
        "--worker-sa", WORKER_SA,
        env=_gcp_env(),
    )
    assert_no_mock_errors(result)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[PASS] cloud-run:worker-sa-explicit" in result.stdout
    assert "[PASS] cloud-run:worker-sa" in result.stdout
    assert "[PASS] cloud-run:api-sa" in result.stdout
    # The correct describe uses --format json (structured), never the old
    # value(spec.template.template.spec...) formatted-string path.
    log = read_log(strict_bin)
    assert "run jobs describe milo-worker" in log
    assert "spec.template.template.spec" not in log


def test_gcp_job_exists_without_explicit_sa_is_blocking_not_missing(strict_bin):
    result = strict_bin(
        "check-gcp-resources.sh",
        "--expected-project", PROJECT,
        "--region", REGION,
        "--worker-job", "milo-worker",
        env=_gcp_env(MOCK_JOB_JSON=str(CR / "job_no_sa.json")),
    )
    assert_no_mock_errors(result)
    assert result.returncode != 0
    assert "[PASS] cloud-run:worker-job" in result.stdout  # the job DOES exist
    assert "[BLOCKED] cloud-run:worker-sa-explicit" in result.stdout
    assert "not found" not in result.stdout.split("worker-sa-explicit")[0].split("worker-job")[-1]


def test_gcp_missing_job_is_warn_not_found(strict_bin):
    result = strict_bin(
        "check-gcp-resources.sh",
        "--expected-project", PROJECT,
        "--region", REGION,
        "--worker-job", "milo-worker",
        env=_gcp_env(MOCK_JOB="missing"),
    )
    assert_no_mock_errors(result)
    assert "[WARN] cloud-run:worker-job" in result.stdout
    assert "not found" in result.stdout


def test_gcp_missing_service_is_warn(strict_bin):
    result = strict_bin(
        "check-gcp-resources.sh",
        "--expected-project", PROJECT,
        "--region", REGION,
        "--api-service", "milo-api",
        env=_gcp_env(MOCK_SERVICE="missing"),
    )
    assert_no_mock_errors(result)
    assert "[WARN] cloud-run:api" in result.stdout


def test_gcp_job_permission_error_is_manual_not_missing(strict_bin):
    result = strict_bin(
        "check-gcp-resources.sh",
        "--expected-project", PROJECT,
        "--region", REGION,
        "--worker-job", "milo-worker",
        env=_gcp_env(MOCK_JOB="error"),
    )
    assert_no_mock_errors(result)
    assert "[MANUAL] cloud-run:worker-job" in result.stdout


def test_gcp_shared_identity_blocked(strict_bin):
    # Worker job runs as the same SA as the API -> identity separation failure.
    result = strict_bin(
        "check-gcp-resources.sh",
        "--expected-project", PROJECT,
        "--region", REGION,
        "--worker-job", "milo-worker",
        "--api-sa", WORKER_SA,  # deliberately equal to the job's SA
        env=_gcp_env(),
    )
    assert_no_mock_errors(result)
    assert "[BLOCKED] cloud-run:shared-identity" in result.stdout


def test_gcp_passes_exact_project_and_region_to_describes(strict_bin):
    # Prove the script forwards the exact --project and --region to the Cloud
    # Run describes (so it can never inspect the wrong project/region).
    result = strict_bin(
        "check-gcp-resources.sh",
        "--expected-project", PROJECT,
        "--region", REGION,
        "--api-service", "milo-api",
        "--worker-job", "milo-worker",
        env=_gcp_env(),
    )
    assert_no_mock_errors(result)
    log = read_log(strict_bin)
    assert f"run services describe milo-api --region {REGION} --project {PROJECT}" in log
    assert f"run jobs describe milo-worker --region {REGION} --project {PROJECT}" in log


def test_gcp_no_mutation_commands(strict_bin):
    result = strict_bin(
        "check-gcp-resources.sh",
        "--expected-project", PROJECT,
        "--region", REGION,
        "--api-service", "milo-api",
        "--worker-job", "milo-worker",
        "--api-sa", API_SA,
        "--worker-sa", WORKER_SA,
        "--repository", "milo",
        env=_gcp_env(),
    )
    log = read_log(strict_bin)
    for verb in ("deploy", "jobs run", "add-iam-policy-binding", "update", "delete", "create"):
        assert verb not in log, f"read-only check issued mutation: {verb}"


# --------------------------------------------------------------------------
# vercel
# --------------------------------------------------------------------------


def _link_project(cwd: Path, project_id: str = "prj_test") -> None:
    (cwd / ".vercel").mkdir(parents=True, exist_ok=True)
    (cwd / ".vercel" / "project.json").write_text(
        json.dumps({"projectId": project_id, "orgId": "team_test"})
    )


def test_vercel_names_parsed_and_pass(strict_bin, tmp_path):
    linked = tmp_path / "frontend"
    linked.mkdir()
    _link_project(linked)
    result = strict_bin(
        "check-vercel-config.sh",
        "--project", "milo-frontend",
        "--vercel-cwd", str(linked),
        env={"MOCK_VERCEL_ENV_FILE": str(FIX / "vercel" / "env_ls_production.txt")},
    )
    assert_no_mock_errors(result)
    assert "[PASS] vercel:auth" in result.stdout
    assert "[PASS] vercel:project" in result.stdout
    assert "[PASS] vercel:var:CLOUD_RUN_API_URL" in result.stdout
    assert "[PASS] vercel:var:NEXT_PUBLIC_SUPABASE_ANON_KEY" in result.stdout
    # Only variable NAMES were consulted — the "Encrypted" value column must
    # never appear as a parsed value.
    assert "Encrypted" not in result.stdout
    log = read_log(strict_bin)
    assert "env ls production" in log
    assert "--scope-project" not in log


def test_vercel_unsupported_flag_would_be_rejected(strict_bin, tmp_path):
    # Prove the strict mock rejects the OLD invented syntax, so a regression to
    # `vercel env ls production --scope-project` can never silently pass.
    linked = tmp_path / "frontend"
    linked.mkdir()
    _link_project(linked)
    result = subprocess.run(
        ["bash", "-c", 'vercel env ls production --scope-project milo-frontend'],
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{strict_bin.bin_dir}:{os.environ['PATH']}", "MOCK_LOG": str(strict_bin.log)},
    )
    assert result.returncode != 0
    assert "unsupported option" in (result.stdout + result.stderr)


def test_vercel_unlinked_project_is_blocked(strict_bin, tmp_path):
    unlinked = tmp_path / "frontend"
    unlinked.mkdir()  # no .vercel/project.json
    result = strict_bin(
        "check-vercel-config.sh",
        "--project", "milo-frontend",
        "--vercel-cwd", str(unlinked),
        env={"MOCK_VERCEL_ENV_FILE": str(FIX / "vercel" / "env_ls_production.txt")},
    )
    assert result.returncode != 0
    assert "[BLOCKED] vercel:link" in result.stdout


def test_vercel_auth_failure_is_blocked_not_empty(strict_bin, tmp_path):
    linked = tmp_path / "frontend"
    linked.mkdir()
    _link_project(linked)
    result = strict_bin(
        "check-vercel-config.sh",
        "--project", "milo-frontend",
        "--vercel-cwd", str(linked),
        env={
            "MOCK_VERCEL_AUTH": "fail",
            "MOCK_VERCEL_ENV_FILE": str(FIX / "vercel" / "env_ls_production.txt"),
        },
    )
    assert result.returncode != 0
    assert "[BLOCKED] vercel:auth" in result.stdout
    # An auth failure must never be classified as a normal empty result.
    assert "[MANUAL] vercel:env" not in result.stdout


def test_vercel_wrong_project_is_blocked(strict_bin, tmp_path):
    linked = tmp_path / "frontend"
    linked.mkdir()
    _link_project(linked)
    result = strict_bin(
        "check-vercel-config.sh",
        "--project", "milo-frontend",
        "--vercel-cwd", str(linked),
        env={"MOCK_VERCEL_ENV_FILE": str(FIX / "vercel" / "env_ls_wrong_project.txt")},
    )
    assert result.returncode != 0
    assert "[BLOCKED] vercel:wrong-project" in result.stdout


def test_vercel_empty_environment_distinct_from_failure(strict_bin, tmp_path):
    linked = tmp_path / "frontend"
    linked.mkdir()
    _link_project(linked)
    result = strict_bin(
        "check-vercel-config.sh",
        "--project", "milo-frontend",
        "--vercel-cwd", str(linked),
        env={"MOCK_VERCEL_ENV_FILE": str(FIX / "vercel" / "env_ls_empty.txt")},
    )
    assert_no_mock_errors(result)
    # Empty environment: auth OK, identity OK, but required vars are missing.
    assert "[PASS] vercel:auth" in result.stdout
    assert "[WARN] vercel:env-empty" in result.stdout
    assert "[BLOCKED] vercel:var:CLOUD_RUN_API_URL" in result.stdout


# --------------------------------------------------------------------------
# psql: reconcile-launch-unknown mutation semantics
# --------------------------------------------------------------------------


def _reconcile_apply(strict_bin, repo, head, resolution, **env):
    base = {"MILO_OPERATOR_ACK": ACK, "MOCK_GCLOUD_ACCOUNT": "operator@milo-prod.iam.gserviceaccount.com", "MOCK_GCLOUD_PROJECT": PROJECT, "MILO_DB": "postgres://ignored"}
    base.update(env)
    return strict_bin(
        "reconcile-launch-unknown.sh",
        "--run-id", UUID_A,
        "--resolution", resolution,
        "--apply",
        "--environment", "production",
        "--expected-project", PROJECT,
        "--expected-account", "operator@milo-prod.iam.gserviceaccount.com",
        "--expected-sha", head,
        "--confirm-production-change",
        "--database-url-env", "MILO_DB",
        "--audit-file", str(repo / "audit.log"),
        env=base,
        cwd=repo,
    )


def _git_repo(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    return repo, head


def test_reconcile_one_row_update_passes_and_audits_after_success(strict_bin, tmp_path):
    repo, head = _git_repo(tmp_path)
    result = _reconcile_apply(
        strict_bin, repo, head, "confirmed-launched",
        MOCK_PSQL_READ="launch_unknown|queued|none",
        MOCK_PSQL_UPDATE="1|launched|queued",
    )
    assert_no_mock_errors(result)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[PASS] apply:confirmed-launched" in result.stdout
    audit = (repo / "audit.log").read_text()
    assert "resolution=confirmed-launched" in audit
    assert "prev_launch_state=launch_unknown" in audit
    assert "new_launch_state=launched" in audit


def test_reconcile_zero_row_update_is_blocked_no_audit(strict_bin, tmp_path):
    repo, head = _git_repo(tmp_path)
    result = _reconcile_apply(
        strict_bin, repo, head, "confirmed-launched",
        MOCK_PSQL_READ="launch_unknown|queued|none",
        MOCK_PSQL_UPDATE="0||",  # a concurrent change means zero rows matched
    )
    assert result.returncode != 0
    assert "[BLOCKED] apply:confirmed-launched" in result.stdout
    assert "zero rows" in result.stdout
    assert not (repo / "audit.log").exists()


def test_reconcile_wrong_current_state_is_blocked_no_mutation(strict_bin, tmp_path):
    repo, head = _git_repo(tmp_path)
    # 'pending' is neither the required current state (launch_unknown) nor the
    # target (launched) for confirmed-launched -> BLOCKED, never a mutation.
    result = _reconcile_apply(
        strict_bin, repo, head, "confirmed-launched",
        MOCK_PSQL_READ="pending|queued|none",
    )
    assert result.returncode != 0
    assert "[BLOCKED] apply:state" in result.stdout
    log = read_log(strict_bin)
    assert "with upd as (update" not in log  # never attempted the mutation
    assert not (repo / "audit.log").exists()


def test_reconcile_invalid_state_blocked(strict_bin, tmp_path):
    repo, head = _git_repo(tmp_path)
    result = _reconcile_apply(
        strict_bin, repo, head, "confirmed-launched",
        MOCK_PSQL_READ="launching|running|none",  # neither target nor required
    )
    assert result.returncode != 0
    assert "[BLOCKED] apply:state" in result.stdout
    assert not (repo / "audit.log").exists()


def test_reconcile_requeue_wrong_status_blocked(strict_bin, tmp_path):
    repo, head = _git_repo(tmp_path)
    result = _reconcile_apply(
        strict_bin, repo, head, "requeue",
        MOCK_PSQL_READ="launch_failed|running|none",  # status must be queued
    )
    assert result.returncode != 0
    assert "[BLOCKED] apply:status" in result.stdout
    assert not (repo / "audit.log").exists()


def test_reconcile_requeue_active_lease_blocked(strict_bin, tmp_path):
    repo, head = _git_repo(tmp_path)
    result = _reconcile_apply(
        strict_bin, repo, head, "requeue",
        MOCK_PSQL_READ="launch_failed|queued|active",  # active worker lease
    )
    assert result.returncode != 0
    assert "[BLOCKED] apply:lease" in result.stdout
    assert not (repo / "audit.log").exists()


def test_reconcile_requeue_happy_path(strict_bin, tmp_path):
    repo, head = _git_repo(tmp_path)
    result = _reconcile_apply(
        strict_bin, repo, head, "requeue",
        MOCK_PSQL_READ="launch_failed|queued|none",
        MOCK_PSQL_UPDATE="1|pending|queued",
    )
    assert_no_mock_errors(result)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[PASS] apply:requeue" in result.stdout


def test_reconcile_db_failure_is_blocked(strict_bin, tmp_path):
    repo, head = _git_repo(tmp_path)
    result = _reconcile_apply(
        strict_bin, repo, head, "confirmed-launched",
        MOCK_PSQL_READ="launch_unknown|queued|none",
        MOCK_PSQL_FAIL="update",
    )
    assert result.returncode != 0
    assert "[BLOCKED] apply:db" in result.stdout
    assert not (repo / "audit.log").exists()


def test_reconcile_read_failure_is_blocked(strict_bin, tmp_path):
    repo, head = _git_repo(tmp_path)
    result = _reconcile_apply(
        strict_bin, repo, head, "confirmed-launched",
        MOCK_PSQL_FAIL="read",
    )
    assert result.returncode != 0
    assert "[BLOCKED] apply:db" in result.stdout
    assert not (repo / "audit.log").exists()


def test_reconcile_idempotent_repeat_is_noop_not_new_success(strict_bin, tmp_path):
    repo, head = _git_repo(tmp_path)
    # The run is already in the target state (a prior successful apply).
    result = _reconcile_apply(
        strict_bin, repo, head, "confirmed-launched",
        MOCK_PSQL_READ="launched|completed|none",
    )
    assert "[NOT_APPLICABLE] apply:confirmed-launched" in result.stdout
    assert "already in launch_state" in result.stdout
    log = read_log(strict_bin)
    assert "with upd as (update" not in log
    assert not (repo / "audit.log").exists()


def test_reconcile_missing_psql_blocked_before_audit(strict_bin, tmp_path):
    repo, head = _git_repo(tmp_path)
    # Build a curated PATH that has git + coreutils + the mock gcloud, but NO
    # psql at all (the real /usr/bin/psql must not leak in), so apply mode
    # passes the guard and then blocks on the missing psql BEFORE any audit.
    toolbin = tmp_path / "toolbin"
    toolbin.mkdir()
    needed = ["git", "bash", "mktemp", "rm", "tr", "grep", "sed", "date", "cat", "wc", "dirname", "env", "mkdir", "chmod"]
    for tool in needed:
        real = subprocess.run(["bash", "-lc", f"command -v {tool}"], capture_output=True, text=True).stdout.strip()
        if real:
            (toolbin / tool).symlink_to(real)
    (toolbin / "gcloud").symlink_to(strict_bin.bin_dir / "gcloud")
    assert subprocess.run(["bash", "-c", "command -v psql"], env={"PATH": str(toolbin)}, capture_output=True).returncode != 0
    result = subprocess.run(
        ["bash", str(RELEASE / "reconcile-launch-unknown.sh"),
         "--run-id", UUID_A, "--resolution", "confirmed-launched", "--apply",
         "--environment", "production", "--expected-project", PROJECT,
         "--expected-account", "operator@milo-prod.iam.gserviceaccount.com",
         "--expected-sha", head, "--confirm-production-change",
         "--database-url-env", "MILO_DB", "--audit-file", str(repo / "audit.log")],
        capture_output=True, text=True, cwd=repo,
        env={
            "PATH": str(toolbin),
            "MILO_OPERATOR_ACK": ACK,
            "MILO_DB": "postgres://x",
            "MOCK_LOG": str(strict_bin.log),
            "MOCK_GCLOUD_ACCOUNT": "operator@milo-prod.iam.gserviceaccount.com",
            "MOCK_GCLOUD_PROJECT": PROJECT,
        },
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "psql unavailable" in result.stdout
    assert not (repo / "audit.log").exists()


def test_reconcile_default_list_mode_is_read_only(strict_bin, tmp_path):
    result = strict_bin(
        "reconcile-launch-unknown.sh",
        "--database-url-env", "MILO_DB",
        env={"MILO_DB": "postgres://x", "MOCK_PSQL_LIST": ""},
    )
    log = read_log(strict_bin)
    assert "update public.runs" not in log
    assert "with upd as (update" not in log


# --------------------------------------------------------------------------
# curl: execution-disabled smoke test
# --------------------------------------------------------------------------


def _env_file(tmp_path: Path, **over) -> Path:
    base = {
        "MILO_ENABLE_PAID_EXECUTION": "false",
        "MILO_ENABLE_RUN_CREATION": "false",
        "GATEWAY_ALLOW_EXECUTION_ROUTES": "false",
    }
    base.update(over)
    path = tmp_path / "flags.env"
    path.write_text("".join(f"{k}={v}\n" for k, v in base.items()))
    return path


def _smoke(strict_bin, tmp_path, *extra, token="tok", token_val="valid-user-token", **env):
    args = [
        "smoke-test-execution-disabled.sh",
        "--base-url", "https://mock-gateway.invalid",
        "--env-file", str(_env_file(tmp_path)),
        *extra,
    ]
    run_env = dict(env)
    if token is not None:
        run_env[token] = token_val
    return strict_bin(*args, env=run_env)


def test_smoke_missing_token_is_not_pass(strict_bin, tmp_path):
    result = _smoke(strict_bin, tmp_path, token=None)
    assert_no_mock_errors(result)
    assert "[MANUAL] run-creation-blocked" in result.stdout
    assert "[PASS] run-creation-blocked" not in result.stdout


def test_smoke_invalid_token_401_is_not_pass(strict_bin, tmp_path):
    result = _smoke(
        strict_bin, tmp_path,
        "--user-token-env", "SMOKE_TOKEN", "--conversation-id", UUID_A,
        token="SMOKE_TOKEN", token_val="",  # empty -> no auth header -> 401
    )
    # Empty token means the env var is unpopulated -> MANUAL (not PASS).
    assert "[PASS] run-creation-blocked" not in result.stdout
    assert "[MANUAL] run-creation-blocked" in result.stdout


def test_smoke_authenticated_403_with_app_error_passes(strict_bin, tmp_path):
    result = _smoke(
        strict_bin, tmp_path,
        "--user-token-env", "SMOKE_TOKEN", "--conversation-id", UUID_A,
        token="SMOKE_TOKEN", token_val="valid-user-token",
    )
    assert_no_mock_errors(result)
    assert "[PASS] run-creation-blocked" in result.stdout


def test_smoke_authenticated_403_generic_body_is_blocked(strict_bin, tmp_path):
    result = _smoke(
        strict_bin, tmp_path,
        "--user-token-env", "SMOKE_TOKEN", "--conversation-id", UUID_A,
        token="SMOKE_TOKEN", token_val="valid-user-token",
        MOCK_CURL_RUN_CODE="403", MOCK_CURL_RUN_BODY='{"error":"forbidden"}',
    )
    assert "[BLOCKED] run-creation-blocked" in result.stdout


def test_smoke_authenticated_success_is_blocked(strict_bin, tmp_path):
    for code in ("200", "201", "202"):
        result = _smoke(
            strict_bin, tmp_path,
            "--user-token-env", "SMOKE_TOKEN", "--conversation-id", UUID_A,
            token="SMOKE_TOKEN", token_val="valid-user-token",
            MOCK_CURL_RUN_CODE=code,
        )
        assert "[BLOCKED] run-creation-blocked" in result.stdout, code
        assert "SUCCEEDED" in result.stdout


def test_smoke_malformed_conversation_id_cannot_pass(strict_bin, tmp_path):
    result = _smoke(
        strict_bin, tmp_path,
        "--user-token-env", "SMOKE_TOKEN", "--conversation-id", "not-a-uuid",
        token="SMOKE_TOKEN", token_val="valid-user-token",
    )
    assert "[PASS] run-creation-blocked" not in result.stdout
    assert "[BLOCKED] run-creation-blocked" in result.stdout


# --------------------------------------------------------------------------
# check-secret-metadata
# --------------------------------------------------------------------------


def _secret(strict_bin, *specs, **env):
    base = {"MOCK_GCLOUD_PROJECT": PROJECT}
    base.update(env)
    args = ["check-secret-metadata.sh", "--expected-project", PROJECT]
    for spec in specs:
        args += ["--secret", spec]
    return strict_bin(*args, env=base)


def test_secret_exists_with_intended_consumer_pass(strict_bin):
    policy = json.dumps({"bindings": [{"role": "roles/secretmanager.secretAccessor", "members": [f"serviceAccount:{API_SA}"]}]})
    result = _secret(
        strict_bin, f"milo-supabase-key={API_SA}",
        MOCK_SECRETS_LIST="milo-supabase-key\n",
        MOCK_SECRET_POLICY=policy,
        MOCK_SECRET_VERSIONS="1\n",
    )
    assert_no_mock_errors(result)
    assert "[PASS] secret:milo-supabase-key" in result.stdout
    assert f"[PASS] secret:milo-supabase-key:consumer:{API_SA}" in result.stdout


def test_secret_missing_is_blocked(strict_bin):
    result = _secret(
        strict_bin, f"milo-supabase-key={API_SA}",
        MOCK_SECRETS_LIST="some-other-secret\n",
    )
    assert result.returncode != 0
    assert "[BLOCKED] secret:milo-supabase-key" in result.stdout
    assert "not found" in result.stdout


def test_secret_missing_intended_consumer_blocked(strict_bin):
    policy = json.dumps({"bindings": [{"role": "roles/secretmanager.secretAccessor", "members": ["serviceAccount:someone-else@milo-prod.iam.gserviceaccount.com"]}]})
    result = _secret(
        strict_bin, f"milo-supabase-key={API_SA}",
        MOCK_SECRETS_LIST="milo-supabase-key\n",
        MOCK_SECRET_POLICY=policy,
        MOCK_SECRET_VERSIONS="1\n",
    )
    assert result.returncode != 0
    assert f"[BLOCKED] secret:milo-supabase-key:consumer:{API_SA}" in result.stdout


def test_secret_unexpected_accessor_warned(strict_bin):
    policy = json.dumps({"bindings": [{"role": "roles/secretmanager.secretAccessor", "members": [f"serviceAccount:{API_SA}", "serviceAccount:stranger@milo-prod.iam.gserviceaccount.com"]}]})
    result = _secret(
        strict_bin, f"milo-supabase-key={API_SA}",
        MOCK_SECRETS_LIST="milo-supabase-key\n",
        MOCK_SECRET_POLICY=policy,
        MOCK_SECRET_VERSIONS="1\n",
    )
    assert_no_mock_errors(result)
    assert "[WARN] secret:milo-supabase-key:extra-accessor" in result.stdout
    assert "stranger@" in result.stdout


def test_secret_project_wide_accessor_blocked(strict_bin):
    proj_policy = json.dumps({"bindings": [{"role": "roles/secretmanager.secretAccessor", "members": ["serviceAccount:broad@milo-prod.iam.gserviceaccount.com"]}]})
    result = _secret(
        strict_bin, f"milo-supabase-key={API_SA}",
        MOCK_SECRETS_LIST="milo-supabase-key\n",
        MOCK_PROJECT_POLICY=proj_policy,
        MOCK_SECRET_VERSIONS="1\n",
    )
    assert result.returncode != 0
    assert "[BLOCKED] secrets:project-wide-accessor" in result.stdout


def test_secret_wildcard_principal_blocked(strict_bin):
    policy = json.dumps({"bindings": [{"role": "roles/secretmanager.secretAccessor", "members": ["allUsers"]}]})
    result = _secret(
        strict_bin, f"milo-supabase-key={API_SA}",
        MOCK_SECRETS_LIST="milo-supabase-key\n",
        MOCK_SECRET_POLICY=policy,
        MOCK_SECRET_VERSIONS="1\n",
    )
    assert result.returncode != 0
    assert "[BLOCKED] secret:milo-supabase-key:wildcard" in result.stdout


def test_secret_no_expectations_is_manual_not_pass(strict_bin):
    result = _secret(strict_bin, MOCK_SECRETS_LIST="milo-supabase-key\n")
    assert "[MANUAL] secrets:expected" in result.stdout
    assert "No Secret Manager verification was performed" in result.stdout


def test_secret_placeholder_name_rejected(strict_bin):
    result = _secret(
        strict_bin, f"<SECRET_NAME>={API_SA}",
        MOCK_SECRETS_LIST="milo-supabase-key\n",
    )
    assert result.returncode != 0
    assert "placeholder" in result.stdout


# --------------------------------------------------------------------------
# aggregate report
# --------------------------------------------------------------------------


def _write_report(path: Path, checks: list[tuple[str, str, str]]) -> None:
    counts = {"pass": 0, "warn": 0, "blocked": 0, "manual": 0, "not_applicable": 0}
    key = {"PASS": "pass", "WARN": "warn", "BLOCKED": "blocked", "MANUAL": "manual", "NOT_APPLICABLE": "not_applicable"}
    for status, _n, _d in checks:
        counts[key[status]] += 1
    path.write_text(json.dumps({
        "script": path.stem,
        "summary": counts,
        "checks": [{"status": s, "name": n, "detail": d} for s, n, d in checks],
    }))


def test_aggregate_totals_equal_sum_of_all_checks(tmp_path):
    top = tmp_path / "top.json"
    sub_a = tmp_path / "a.json"
    sub_b = tmp_path / "b.json"
    _write_report(top, [("PASS", "git", ""), ("MANUAL", "tool:gcloud", "install gcloud")])
    _write_report(sub_a, [("PASS", "x", ""), ("BLOCKED", "y", "bad"), ("WARN", "z", "")])
    _write_report(sub_b, [("MANUAL", "m", "install gcloud"), ("NOT_APPLICABLE", "n", "")])
    out = tmp_path / "agg.json"
    result = subprocess.run(
        ["python3", str(RELEASE / "aggregate_reports.py"),
         "--top-level", str(top),
         "--sub-report", f"a={sub_a}", "--sub-report", f"b={sub_b}",
         "--output", str(out)],
        capture_output=True, text=True,
    )
    assert result.returncode == 1  # one blocked -> nonzero
    data = json.loads(out.read_text())
    # Independently computed expected totals: PASS = git + x = 2; WARN = z = 1;
    # BLOCKED = y = 1; MANUAL = tool:gcloud + m = 2; NOT_APPLICABLE = n = 1.
    assert data["summary"] == {"pass": 2, "warn": 1, "blocked": 1, "manual": 2, "not_applicable": 1}
    assert len(data["blocking_findings"]) == 1
    assert data["blocking_findings"][0]["source"] == "a"
    # Two MANUALs share the same prerequisite text -> de-duplicated to one.
    assert len(data["manual_actions_remaining"]) == 1
    assert set(data["sub_reports"].keys()) == {"a", "b"}


def test_aggregate_missing_sub_report_is_blocking(tmp_path):
    top = tmp_path / "top.json"
    _write_report(top, [("PASS", "git", "")])
    out = tmp_path / "agg.json"
    result = subprocess.run(
        ["python3", str(RELEASE / "aggregate_reports.py"),
         "--top-level", str(top),
         "--sub-report", f"gone={tmp_path / 'does-not-exist.json'}",
         "--output", str(out)],
        capture_output=True, text=True,
    )
    assert result.returncode == 1
    data = json.loads(out.read_text())
    assert data["summary"]["blocked"] == 1
    assert any(f["check"] == "gone:report-unreadable" for f in data["blocking_findings"])


def test_aggregate_corrupt_sub_report_is_blocking(tmp_path):
    top = tmp_path / "top.json"
    _write_report(top, [("PASS", "git", "")])
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json")
    out = tmp_path / "agg.json"
    result = subprocess.run(
        ["python3", str(RELEASE / "aggregate_reports.py"),
         "--top-level", str(top),
         "--sub-report", f"bad={bad}",
         "--output", str(out)],
        capture_output=True, text=True,
    )
    assert result.returncode == 1
    data = json.loads(out.read_text())
    # Aggregate JSON is still valid despite the corrupt input.
    assert data["summary"]["blocked"] == 1


def test_aggregate_clean_reports_exit_zero(tmp_path):
    top = tmp_path / "top.json"
    sub = tmp_path / "s.json"
    _write_report(top, [("PASS", "git", "")])
    _write_report(sub, [("PASS", "a", ""), ("MANUAL", "b", "do thing")])
    out = tmp_path / "agg.json"
    result = subprocess.run(
        ["python3", str(RELEASE / "aggregate_reports.py"),
         "--top-level", str(top), "--sub-report", f"s={sub}", "--output", str(out)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    data = json.loads(out.read_text())
    assert data["summary"]["blocked"] == 0
    for required in ("summary", "blocking_findings", "warnings", "manual_actions_remaining", "sub_reports"):
        assert required in data

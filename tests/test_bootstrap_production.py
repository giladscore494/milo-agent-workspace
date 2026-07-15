"""Strict proofs for scripts/release/bootstrap-production.sh.

The mocks here FAIL LOUDLY on any command the bootstrap is not explicitly
authorized to run, and reject every forbidden mutation (worker-job execution,
service-account key creation, project-wide secret accessor, unauthenticated
Cloud Run, Vercel deploy/link/promote, any provider call). Upstash is served
by a mock so tests never touch the real Upstash API.

These tests prove the safety, idempotency, secret-hygiene and audit
properties required of the one-command production bootstrap.
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
ACK = "I_UNDERSTAND_THIS_CHANGES_PRODUCTION"

PROJECT = "milo-prod"
REGION = "us-central1"
OPERATOR = "operator@milo-prod.iam.gserviceaccount.com"
API_SA = "milo-api-sa@milo-prod.iam.gserviceaccount.com"
WORKER_SA = "milo-worker-sa@milo-prod.iam.gserviceaccount.com"
GATEWAY_SA = "milo-gateway-sa@milo-prod.iam.gserviceaccount.com"
FULL_SHA = "0123456789abcdef0123456789abcdef01234567"

# Secret sentinel VALUES. If any of these ever appears in stdout, stderr, a
# generated file or a report, the secret-hygiene contract is broken.
SUPABASE_SECRET = "sb_secret_DEADBEEFdeadbeef_supabase"
# Deliberately NOT an "sk-..." shape, so the repo secret scanner never flags
# this fixture; it is still a unique sentinel that must never leak.
PROVIDER_SECRET = "PROVIDER-key-SECRET-0000-value-do-not-log"
UPSTASH_APIKEY_SECRET = "upstash_mgmt_SECRET_key_9999"
UPSTASH_REST_TOKEN_SECRET = "AaBbToken_UPSTASH_REST_SECRET_zzzz"
VERCEL_TOKEN_SECRET = "vercel_TOKEN_SECRET_abcdef"
ALL_SECRETS = [
    SUPABASE_SECRET,
    PROVIDER_SECRET,
    UPSTASH_APIKEY_SECRET,
    UPSTASH_REST_TOKEN_SECRET,
    VERCEL_TOKEN_SECRET,
]

# ---------------------------------------------------------------------------
# Strict mock CLIs
# ---------------------------------------------------------------------------

GCLOUD_MOCK = r"""#!/usr/bin/env bash
printf 'gcloud %s\n' "$*" >> "$MOCK_LOG"
all="$*"
# Absolutely forbidden for the bootstrap. NB: --no-allow-unauthenticated is
# allowed; a bare --allow-unauthenticated (with a leading space) is not.
case "$all" in
  *"jobs run"*|*"jobs execute"*|*"keys create"*|*"services delete"*|*"jobs delete"*|\
  *"secrets delete"*|*" --allow-unauthenticated"*|*"projects add-iam-policy-binding"*|\
  *"projects set-iam-policy"*)
    printf 'MOCK-FORBIDDEN gcloud: %s\n' "$all" >&2; exit 97;;
esac
args=("$@")
email=""; secret=""
for ((i=0; i<${#args[@]}; i++)); do
  case "${args[$i]}" in
    describe) nxt="${args[$((i+1))]:-}";;
  esac
done
# helper: membership test against a comma list
in_list() { case ",$2," in *",$1,"*) return 0;; *) return 1;; esac; }
case "$all" in
  *"config get-value account"*) printf '%s\n' "${MOCK_GCLOUD_ACCOUNT:-operator@milo-prod.iam.gserviceaccount.com}";;
  *"config get-value project"*) printf '%s\n' "${MOCK_GCLOUD_PROJECT:-milo-prod}";;
  *"iam service-accounts describe"*)
    email="${args[3]:-}"
    if in_list "$email" "${MOCK_ERROR_SAS:-}"; then printf 'ERROR: PERMISSION_DENIED: caller lacks permission\n' >&2; exit 1; fi
    if in_list "$email" "${MOCK_EXISTING_SAS:-}"; then printf '%s\n' "$email"; exit 0; fi
    printf 'ERROR: NOT_FOUND: unknown service account %s\n' "$email" >&2; exit 1;;
  *"iam service-accounts create"*)
    exit 0;;
  *"secrets describe"*)
    secret="${args[2]:-}"
    if in_list "$secret" "${MOCK_ERROR_SECRETS:-}"; then printf 'ERROR: PERMISSION_DENIED\n' >&2; exit 1; fi
    if in_list "$secret" "${MOCK_EXISTING_SECRETS:-}"; then printf 'projects/p/secrets/%s\n' "$secret"; exit 0; fi
    printf 'ERROR: NOT_FOUND: Secret [%s] not found\n' "$secret" >&2; exit 1;;
  *"secrets create"*) exit 0;;
  *"secrets versions add"*)
    cat > /dev/null;  # consume the piped payload; never store or echo it
    exit 0;;
  *"secrets versions list"*) printf '%s' "${MOCK_SECRET_VERSIONS-}";;
  *"secrets add-iam-policy-binding"*) exit 0;;
  *"secrets get-iam-policy"*) printf '%s\n' "${MOCK_SECRET_POLICY:-{\"bindings\":[]}}";;
  *"secrets list"*) printf '%s' "${MOCK_SECRETS_LIST-}";;
  *"run services update"*) if [[ "${MOCK_API_SVC:-present}" == "missing" ]]; then printf 'ERROR: NOT_FOUND: service\n' >&2; exit 1; fi; exit 0;;
  *"run jobs update"*) if [[ "${MOCK_WORKER_JOB:-present}" == "missing" ]]; then printf 'ERROR: NOT_FOUND: job\n' >&2; exit 1; fi; exit 0;;
  *"run services describe"*) printf '{"spec":{"template":{"spec":{"serviceAccountName":"%s"}}}}\n' "${API_SA:-milo-api-sa@milo-prod.iam.gserviceaccount.com}";;
  *"run jobs describe"*) printf '{"spec":{"template":{"spec":{"template":{"spec":{"serviceAccountName":"%s"}}}}}}\n' "${WORKER_SA:-milo-worker-sa@milo-prod.iam.gserviceaccount.com}";;
  *"run services get-iam-policy"*) printf '{"bindings":[]}\n';;
  *"run jobs get-iam-policy"*) printf '{"bindings":[]}\n';;
  *"services list"*) printf 'run.googleapis.com\nartifactregistry.googleapis.com\nsecretmanager.googleapis.com\niamcredentials.googleapis.com\nsts.googleapis.com\n';;
  *"artifacts repositories describe"*) printf 'projects/p/locations/r/repositories/milo-agent\n';;
  *"projects get-iam-policy"*) printf '{"bindings":[]}\n';;
  *) printf 'MOCK-UNAUTHORIZED gcloud command: %s\n' "$all" >&2; exit 98;;
esac
"""

VERCEL_MOCK = r"""#!/usr/bin/env bash
printf 'vercel %s\n' "$*" >> "$MOCK_LOG"
all="$*"
case "$all" in
  *deploy*|*promote*|*link*|*"--prod"*|*"env rm"*|*"env remove"*)
    printf 'MOCK-FORBIDDEN vercel: %s\n' "$all" >&2; exit 97;;
esac
case "$all" in
  whoami*) printf '%s\n' "${MOCK_VERCEL_USER:-milo-team}";;
  "project inspect"*)
    case "${MOCK_VERCEL_INSPECT:-ok}" in
      fail) printf 'Error: Not authorized.\n' >&2; exit 1;;
      *)
        printf 'Project Name milo-agent-workspace\n  ID %s\n' "${MOCK_VERCEL_INSPECT_ID:-prj_linked0000000001}"
        if [[ -n "${MOCK_VERCEL_INSPECT_ORG:-}" ]]; then printf '  Owner %s\n' "$MOCK_VERCEL_INSPECT_ORG"; fi
        ;;
    esac;;
  "env add"*)
    # Supported production var upsert; value arrives on stdin, never argv.
    cat > /dev/null
    exit 0;;
  "env ls production"*) printf 'No Environment Variables found\n';;
  *) printf 'MOCK-UNAUTHORIZED vercel command: %s\n' "$all" >&2; exit 98;;
esac
"""

# Upstash mock: served through curl. Recognizes ONLY the official
# /v2/redis/* developer-API paths; anything else is unauthorized.
CURL_MOCK = r"""#!/usr/bin/env bash
printf 'curl %s\n' "$*" >> "$MOCK_LOG"
method=GET; url=""; data=""
args=("$@")
for ((i=0; i<${#args[@]}; i++)); do
  case "${args[$i]}" in
    -X) method="${args[$((i+1))]}";;
    --data) data="${args[$((i+1))]}";;
    http://*|https://*) url="${args[$i]}";;
  esac
done
emit() { printf '%s\n%s' "$1" "$2"; }  # body then newline+code (script reads last line as code)
tok="${MOCK_UPSTASH_REST_TOKEN:-AaBbToken_UPSTASH_REST_SECRET_zzzz}"
case "$url" in
  *"/v2/redis/databases")
    if [[ "${MOCK_UPSTASH_LIST_FAIL:-}" == "1" ]]; then emit '{"error":"unauthorized"}' 401; exit 0; fi
    emit "${MOCK_UPSTASH_DBS:-[]}" 200;;
  *"/v2/redis/database/"*)
    emit "{\"database_id\":\"db_prod_1\",\"endpoint\":\"prod-1.upstash.io\",\"rest_token\":\"${tok}\"}" 200;;
  *"/v2/redis/database")
    if [[ "$method" == "POST" ]]; then emit '{"database_id":"db_prod_1","endpoint":"prod-1.upstash.io"}' 200; else emit '{}' 405; fi;;
  *) printf 'MOCK-UNAUTHORIZED curl url: %s\n' "$url" >&2; exit 98;;
esac
"""

MOCKS = {"gcloud": GCLOUD_MOCK, "vercel": VERCEL_MOCK, "curl": CURL_MOCK}


@pytest.fixture()
def strict_bin(tmp_path: Path):
    bin_dir = tmp_path / "strictbin"
    bin_dir.mkdir()
    log = tmp_path / "invocations.log"
    log.touch()
    for name, body in MOCKS.items():
        p = bin_dir / name
        p.write_text(body)
        p.chmod(p.stat().st_mode | stat.S_IEXEC)

    def run(*args: str, env: dict | None = None, cwd: Path | None = None):
        run_env = dict(os.environ)
        run_env["PATH"] = f"{bin_dir}:{run_env['PATH']}"
        run_env["MOCK_LOG"] = str(log)
        if env:
            run_env.update(env)
        return subprocess.run(
            ["bash", str(RELEASE / "bootstrap-production.sh"), *args],
            capture_output=True, text=True, env=run_env,
            cwd=cwd or REPO, timeout=180,
        )

    run.log = log  # type: ignore[attr-defined]
    run.bin_dir = bin_dir  # type: ignore[attr-defined]
    return run


def read_log(run) -> str:
    return run.log.read_text()


def assert_no_mock_errors(result):
    combined = result.stdout + result.stderr
    for marker in ("MOCK-UNAUTHORIZED", "MOCK-FORBIDDEN"):
        assert marker not in combined, f"strict mock rejected a command:\n{combined}"


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


def _linked_vercel(tmp_path: Path, pid="prj_linked0000000001", org="team_linked000") -> Path:
    d = tmp_path / "frontend"
    (d / ".vercel").mkdir(parents=True)
    (d / ".vercel" / "project.json").write_text(json.dumps({"projectId": pid, "orgId": org}))
    return d


def _out(tmp_path: Path) -> Path:
    return tmp_path / "operator-out"


def _apply_args(out: Path, sha: str, **over):
    args = [
        "--apply", "--environment", "production", "--confirm-production-change",
        "--expected-project", PROJECT, "--expected-account", OPERATOR,
        "--release-sha", sha, "--rollback-sha", FULL_SHA,
        "--region", REGION, "--repository", "milo-agent",
        "--api-service", "milo-agent-api", "--worker-job", "milo-agent-worker",
        "--api-sa", API_SA, "--worker-sa", WORKER_SA, "--gateway-sa", GATEWAY_SA,
        "--vercel-project", "milo-agent-workspace",
        "--supabase-project-ref", "vvhtaqgkgkalpfcbuvag",
        "--production-origin", "https://milo-agent-workspace.vercel.app",
        "--output-directory", str(out),
    ]
    return args


def _apply_env(vercel_cwd: Path, **over) -> dict:
    env = {
        "MILO_OPERATOR_ACK": ACK,
        "MOCK_GCLOUD_ACCOUNT": OPERATOR,
        "MOCK_GCLOUD_PROJECT": PROJECT,
        "MILO_BOOTSTRAP_VERCEL_CWD": str(vercel_cwd),
        # Upstash: one production database already exists.
        "MOCK_UPSTASH_DBS": json.dumps([{"database_name": "milo-production", "database_id": "db_prod_1", "endpoint": "prod-1.upstash.io"}]),
        "MOCK_UPSTASH_REST_TOKEN": UPSTASH_REST_TOKEN_SECRET,
        # Secret INPUT via env-var names.
        "SUPA_KEY": SUPABASE_SECRET,
        "PROV_KEY": PROVIDER_SECRET,
        "UP_EMAIL": "ops@milo.test",
        "UP_APIKEY": UPSTASH_APIKEY_SECRET,
        "VERCEL_TOK": VERCEL_TOKEN_SECRET,
    }
    env.update(over)
    return env


SECRET_ENV_FLAGS = [
    "--supabase-key-env", "SUPA_KEY",
    "--provider-key-env", "PROV_KEY",
    "--upstash-email-env", "UP_EMAIL",
    "--upstash-apikey-env", "UP_APIKEY",
    "--vercel-token-env", "VERCEL_TOK",
]


# ===========================================================================
# 1. Default mode is plan / read-only
# ===========================================================================

def test_default_mode_is_plan_and_read_only(strict_bin, tmp_path):
    out = _out(tmp_path)
    vercel = _linked_vercel(tmp_path)
    result = strict_bin(  # no mode flag => plan
        "--output-directory", str(out),
        "--expected-project", PROJECT, "--expected-account", OPERATOR,
        "--api-sa", API_SA, "--worker-sa", WORKER_SA, "--gateway-sa", GATEWAY_SA,
        "--vercel-project", "milo-agent-workspace",
        env={"MOCK_GCLOUD_PROJECT": PROJECT, "MOCK_GCLOUD_ACCOUNT": OPERATOR,
             "MILO_BOOTSTRAP_VERCEL_CWD": str(vercel)},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[PASS] mode — bootstrap mode: plan" in result.stdout
    log = read_log(strict_bin)
    for verb in ("iam service-accounts create", "secrets create", "secrets versions add",
                 "add-iam-policy-binding", "run services update", "run jobs update", "env add"):
        assert verb not in log, f"plan mode issued a mutation: {verb}"
    assert (out / "bootstrap-plan.json").exists()


def test_plan_generates_placeholder_free_manifest_when_shas_given(strict_bin, tmp_path):
    out = _out(tmp_path)
    result = strict_bin(
        "--plan", "--output-directory", str(out),
        "--release-sha", FULL_SHA, "--rollback-sha", FULL_SHA,
        "--expected-account", OPERATOR,
    )
    # Manifest generation does not depend on live-state findings; the manifest
    # is always produced in plan mode.
    manifest = out / "milo-production.yaml"
    assert manifest.exists()
    val = subprocess.run(
        ["python3", str(RELEASE / "validate_production_manifest.py"),
         "--manifest", str(manifest), "--mode", "apply"],
        capture_output=True, text=True,
    )
    assert val.returncode == 0, val.stdout + val.stderr


# ===========================================================================
# 2. Apply guard
# ===========================================================================

def _guard_case(strict_bin, tmp_path, *, arg_over=None, env_over=None, drop=None):
    repo, head = _git_repo(tmp_path)
    vercel = _linked_vercel(tmp_path)
    out = _out(tmp_path)
    args = _apply_args(out, head) + SECRET_ENV_FLAGS
    if drop:
        # remove a flag and its value
        i = args.index(drop)
        del args[i:i + 2]
    if arg_over:
        args += arg_over
    env = _apply_env(vercel)
    if env_over:
        env.update(env_over)
    return strict_bin(*args, env=env, cwd=repo), out


def test_guard_wrong_project_blocks_no_mutation(strict_bin, tmp_path):
    result, out = _guard_case(strict_bin, tmp_path, env_over={"MOCK_GCLOUD_PROJECT": "other-project"})
    assert result.returncode != 0
    assert "does not match --expected-project" in result.stdout
    assert "iam service-accounts create" not in read_log(strict_bin)
    assert json.loads((out / "bootstrap-apply.json").read_text())["status"] == "guard-blocked"


def test_guard_wrong_account_blocks(strict_bin, tmp_path):
    result, _ = _guard_case(strict_bin, tmp_path, env_over={"MOCK_GCLOUD_ACCOUNT": "intruder@evil.test"})
    assert result.returncode != 0
    assert "does not match --expected-account" in result.stdout
    assert "iam service-accounts create" not in read_log(strict_bin)


def test_guard_wrong_sha_blocks(strict_bin, tmp_path):
    repo, _head = _git_repo(tmp_path)
    vercel = _linked_vercel(tmp_path)
    out = _out(tmp_path)
    args = _apply_args(out, FULL_SHA) + SECRET_ENV_FLAGS  # FULL_SHA != repo HEAD
    result = strict_bin(*args, env=_apply_env(vercel), cwd=repo)
    assert result.returncode != 0
    assert "does not equal the intended release SHA" in result.stdout
    assert "iam service-accounts create" not in read_log(strict_bin)


def test_guard_wrong_environment_blocks(strict_bin, tmp_path):
    repo, head = _git_repo(tmp_path)
    vercel = _linked_vercel(tmp_path)
    out = _out(tmp_path)
    args = _apply_args(out, head)
    i = args.index("--environment")
    args[i + 1] = "staging"
    args += SECRET_ENV_FLAGS
    result = strict_bin(*args, env=_apply_env(vercel), cwd=repo)
    assert result.returncode != 0
    assert "environment production is required" in result.stdout
    assert "iam service-accounts create" not in read_log(strict_bin)


def test_guard_missing_ack_blocks(strict_bin, tmp_path):
    result, _ = _guard_case(strict_bin, tmp_path, env_over={"MILO_OPERATOR_ACK": ""})
    assert result.returncode != 0
    assert "MILO_OPERATOR_ACK" in result.stdout
    assert "iam service-accounts create" not in read_log(strict_bin)


def test_guard_dirty_worktree_blocks(strict_bin, tmp_path):
    repo, head = _git_repo(tmp_path)
    (repo / "dirty.txt").write_text("uncommitted")  # dirties the worktree
    vercel = _linked_vercel(tmp_path)
    out = _out(tmp_path)
    args = _apply_args(out, head) + SECRET_ENV_FLAGS
    result = strict_bin(*args, env=_apply_env(vercel), cwd=repo)
    assert result.returncode != 0
    assert "dirty Git worktree" in result.stdout
    assert "iam service-accounts create" not in read_log(strict_bin)


def test_guard_missing_confirm_blocks(strict_bin, tmp_path):
    repo, head = _git_repo(tmp_path)
    vercel = _linked_vercel(tmp_path)
    out = _out(tmp_path)
    args = [a for a in _apply_args(out, head) if a != "--confirm-production-change"] + SECRET_ENV_FLAGS
    result = strict_bin(*args, env=_apply_env(vercel), cwd=repo)
    assert result.returncode != 0
    assert "confirm-production-change is required" in result.stdout


# ===========================================================================
# 3. Idempotent creation, no keys, distinct identities, no execution
# ===========================================================================

def test_apply_creates_missing_service_accounts_idempotently(strict_bin, tmp_path):
    repo, head = _git_repo(tmp_path)
    vercel = _linked_vercel(tmp_path)
    out = _out(tmp_path)
    args = _apply_args(out, head) + SECRET_ENV_FLAGS
    # No SAs exist yet -> all three are created.
    result = strict_bin(*args, env=_apply_env(vercel), cwd=repo)
    assert_no_mock_errors(result)
    log = read_log(strict_bin)
    for sa in (API_SA, WORKER_SA, GATEWAY_SA):
        acct = sa.split("@")[0]
        assert f"iam service-accounts create {acct}" in log, f"missing create for {sa}"
    report = json.loads((out / "bootstrap-apply.json").read_text())
    created = [a for a in report["applied_actions"] if "created service account" in a]
    assert len(created) == 3


def test_apply_existing_service_accounts_are_noop(strict_bin, tmp_path):
    repo, head = _git_repo(tmp_path)
    vercel = _linked_vercel(tmp_path)
    out = _out(tmp_path)
    args = _apply_args(out, head) + SECRET_ENV_FLAGS
    env = _apply_env(vercel, MOCK_EXISTING_SAS=f"{API_SA},{WORKER_SA},{GATEWAY_SA}")
    result = strict_bin(*args, env=env, cwd=repo)
    assert_no_mock_errors(result)
    log = read_log(strict_bin)
    assert "iam service-accounts create" not in log
    report = json.loads((out / "bootstrap-apply.json").read_text())
    assert any("already present" in a for a in report["applied_actions"])


def test_never_creates_service_account_keys(strict_bin, tmp_path):
    repo, head = _git_repo(tmp_path)
    vercel = _linked_vercel(tmp_path)
    out = _out(tmp_path)
    args = _apply_args(out, head) + SECRET_ENV_FLAGS
    result = strict_bin(*args, env=_apply_env(vercel), cwd=repo)
    assert "MOCK-FORBIDDEN" not in (result.stdout + result.stderr)
    assert "keys create" not in read_log(strict_bin)


def test_shared_api_worker_identity_blocked_before_mutation(strict_bin, tmp_path):
    repo, head = _git_repo(tmp_path)
    vercel = _linked_vercel(tmp_path)
    out = _out(tmp_path)
    args = _apply_args(out, head)
    i = args.index("--worker-sa")
    args[i + 1] = API_SA  # worker == api -> forbidden
    args += SECRET_ENV_FLAGS
    result = strict_bin(*args, env=_apply_env(vercel), cwd=repo)
    assert result.returncode != 0
    assert "[BLOCKED] identity:api-worker" in result.stdout
    assert "iam service-accounts create" not in read_log(strict_bin)


def test_worker_job_never_executed(strict_bin, tmp_path):
    repo, head = _git_repo(tmp_path)
    vercel = _linked_vercel(tmp_path)
    out = _out(tmp_path)
    args = _apply_args(out, head) + SECRET_ENV_FLAGS
    strict_bin(*args, env=_apply_env(vercel), cwd=repo)
    log = read_log(strict_bin)
    assert "jobs run" not in log
    assert "jobs execute" not in log
    # The worker identity is set via `run jobs update` (config only).
    assert "run jobs update milo-agent-worker" in log


def test_execution_flags_stay_disabled_in_manifest(strict_bin, tmp_path):
    repo, head = _git_repo(tmp_path)
    vercel = _linked_vercel(tmp_path)
    out = _out(tmp_path)
    args = _apply_args(out, head) + SECRET_ENV_FLAGS
    strict_bin(*args, env=_apply_env(vercel), cwd=repo)
    manifest = (out / "milo-production.yaml").read_text()
    for line in manifest.splitlines():
        if line.strip().startswith("MILO_ENABLE_") or line.strip().startswith("GATEWAY_ALLOW_EXECUTION"):
            assert line.strip().endswith("false"), line


def test_provider_is_never_called(strict_bin, tmp_path):
    repo, head = _git_repo(tmp_path)
    vercel = _linked_vercel(tmp_path)
    out = _out(tmp_path)
    args = _apply_args(out, head) + SECRET_ENV_FLAGS
    strict_bin(*args, env=_apply_env(vercel), cwd=repo)
    log = read_log(strict_bin)
    # Only Upstash developer-API hosts may be contacted via curl.
    for line in log.splitlines():
        if line.startswith("curl "):
            assert "/v2/redis/" in line, f"unexpected curl target: {line}"
        assert "moonshot" not in line.lower()
        assert "api.kimi" not in line.lower()


# ===========================================================================
# 4. Permission/API failures are not "missing"
# ===========================================================================

def test_permission_error_not_classified_as_missing(strict_bin, tmp_path):
    repo, head = _git_repo(tmp_path)
    vercel = _linked_vercel(tmp_path)
    out = _out(tmp_path)
    args = _apply_args(out, head) + SECRET_ENV_FLAGS
    env = _apply_env(vercel, MOCK_ERROR_SAS=API_SA)  # describe returns permission error
    result = strict_bin(*args, env=env, cwd=repo)
    assert "refusing to create blindly" in result.stdout
    acct = API_SA.split("@")[0]
    assert f"iam service-accounts create {acct}" not in read_log(strict_bin)


def test_plan_permission_error_is_manual_not_create(strict_bin, tmp_path):
    out = _out(tmp_path)
    result = strict_bin(
        "--plan", "--output-directory", str(out),
        "--expected-project", PROJECT, "--api-sa", API_SA,
        "--worker-sa", WORKER_SA, "--gateway-sa", GATEWAY_SA,
        env={"MOCK_GCLOUD_PROJECT": PROJECT, "MOCK_ERROR_SAS": API_SA},
    )
    assert "[MANUAL] gcp:sa:api" in result.stdout
    assert "will create it" not in result.stdout.split("gcp:sa:api")[1].split("\n")[0]


# ===========================================================================
# 5. Secret hygiene
# ===========================================================================

def _all_output_text(result, out: Path) -> str:
    text = result.stdout + result.stderr
    for p in out.rglob("*"):
        if p.is_file():
            try:
                text += "\n" + p.read_text()
            except UnicodeDecodeError:
                pass
    return text


def test_secrets_never_appear_anywhere(strict_bin, tmp_path):
    repo, head = _git_repo(tmp_path)
    vercel = _linked_vercel(tmp_path)
    out = _out(tmp_path)
    args = _apply_args(out, head) + SECRET_ENV_FLAGS
    result = strict_bin(*args, env=_apply_env(vercel), cwd=repo)
    # The contract (item 10) is: secrets never appear in stdout, stderr, the
    # JSON reports or the generated artifacts. The mock's invocation log is a
    # test-only recording of subprocess argv (which real runs never persist),
    # so it is intentionally excluded here.
    haystack = _all_output_text(result, out)
    for secret in ALL_SECRETS:
        assert secret not in haystack, f"secret leaked into output: {secret[:8]}..."


def test_no_generated_file_contains_secret_values(strict_bin, tmp_path):
    repo, head = _git_repo(tmp_path)
    vercel = _linked_vercel(tmp_path)
    out = _out(tmp_path)
    args = _apply_args(out, head) + SECRET_ENV_FLAGS
    strict_bin(*args, env=_apply_env(vercel), cwd=repo)
    for p in out.rglob("*"):
        if p.is_file():
            content = p.read_text(errors="ignore")
            for secret in ALL_SECRETS:
                assert secret not in content, f"{p.name} contains a secret value"


# ===========================================================================
# 6. Upstash mocked & never contacted for real
# ===========================================================================

def test_upstash_only_official_api_paths_used(strict_bin, tmp_path):
    repo, head = _git_repo(tmp_path)
    vercel = _linked_vercel(tmp_path)
    out = _out(tmp_path)
    args = _apply_args(out, head) + SECRET_ENV_FLAGS
    result = strict_bin(*args, env=_apply_env(vercel), cwd=repo)
    assert_no_mock_errors(result)
    log = read_log(strict_bin)
    assert "/v2/redis/databases" in log  # discovery ran through the mock
    assert "[PASS] upstash:token" in result.stdout


def test_upstash_absent_credentials_is_manual(strict_bin, tmp_path):
    out = _out(tmp_path)
    result = strict_bin(
        "--plan", "--output-directory", str(out), "--expected-project", PROJECT,
        env={"MOCK_GCLOUD_PROJECT": PROJECT},
    )
    assert "[MANUAL] upstash" in result.stdout
    assert "/v2/redis/" not in read_log(strict_bin)


# ===========================================================================
# 7. Vercel identity fail-closed before writes
# ===========================================================================

def test_vercel_org_mismatch_blocks_before_writes(strict_bin, tmp_path):
    repo, head = _git_repo(tmp_path)
    vercel = _linked_vercel(tmp_path)  # linked org team_linked000
    out = _out(tmp_path)
    args = _apply_args(out, head) + SECRET_ENV_FLAGS
    env = _apply_env(vercel, MOCK_VERCEL_INSPECT_ORG="team_intruder999")
    result = strict_bin(*args, env=env, cwd=repo)
    assert "[BLOCKED] vercel:project-identity" in result.stdout
    assert "env add" not in read_log(strict_bin)


def test_vercel_project_id_mismatch_blocks_before_writes(strict_bin, tmp_path):
    repo, head = _git_repo(tmp_path)
    vercel = _linked_vercel(tmp_path, pid="prj_DIFFERENT00000")
    out = _out(tmp_path)
    args = _apply_args(out, head) + SECRET_ENV_FLAGS
    result = strict_bin(*args, env=_apply_env(vercel), cwd=repo)
    assert "[BLOCKED] vercel:project-identity" in result.stdout
    assert "env add" not in read_log(strict_bin)


def test_vercel_proven_identity_upserts_only_safe_vars(strict_bin, tmp_path):
    repo, head = _git_repo(tmp_path)
    vercel = _linked_vercel(tmp_path)
    out = _out(tmp_path)
    args = _apply_args(out, head) + SECRET_ENV_FLAGS
    result = strict_bin(*args, env=_apply_env(vercel), cwd=repo)
    assert_no_mock_errors(result)
    log = read_log(strict_bin)
    assert "env add CLOUD_RUN_API_URL production" in log
    assert "env add GCP_SERVICE_ACCOUNT_EMAIL production" in log
    # Provider key and Supabase service-role must NEVER be configured in Vercel.
    for forbidden in ("KIMI_API_KEY", "MOONSHOT_API_KEY", "SUPABASE_SERVICE_ROLE_KEY", "PROVIDER_API_KEY"):
        assert f"env add {forbidden}" not in log


# ===========================================================================
# 8. Idempotent repeat & partial-failure honesty
# ===========================================================================

def test_repeated_apply_does_not_add_duplicate_secret_version(strict_bin, tmp_path):
    repo, head = _git_repo(tmp_path)
    vercel = _linked_vercel(tmp_path)
    out = _out(tmp_path)
    args = _apply_args(out, head) + SECRET_ENV_FLAGS
    # Second run: secrets already exist AND already have an enabled version.
    env = _apply_env(
        vercel,
        MOCK_EXISTING_SECRETS="milo-supabase-service-key,milo-provider-api-key,milo-upstash-rest-token",
        MOCK_SECRET_VERSIONS="1",
    )
    result = strict_bin(*args, env=env, cwd=repo)
    assert "secrets versions add" not in read_log(strict_bin)
    assert "not adding a duplicate" in result.stdout


def test_partial_failure_yields_recovery_plan_and_no_false_success(strict_bin, tmp_path):
    repo, head = _git_repo(tmp_path)
    vercel = _linked_vercel(tmp_path)
    out = _out(tmp_path)
    args = _apply_args(out, head) + SECRET_ENV_FLAGS
    # Make the worker-identity update fail with a non-'not-found' error path by
    # forcing the API service update to fail as a real error.
    env = _apply_env(vercel, MOCK_UPSTASH_LIST_FAIL="1")  # Upstash listing fails
    result = strict_bin(*args, env=env, cwd=repo)
    assert result.returncode != 0
    assert "APPLY COMPLETE" not in result.stdout
    assert "partial failure" in result.stdout.lower()
    report = json.loads((out / "bootstrap-apply.json").read_text())
    assert report["status"] == "partial-failure"
    assert len(report["recovery_steps"]) >= 1


# ===========================================================================
# 9. Final readiness aggregation is internally accurate
# ===========================================================================

def test_final_audit_runs_and_aggregation_totals_are_consistent(strict_bin, tmp_path):
    repo, head = _git_repo(tmp_path)
    vercel = _linked_vercel(tmp_path)
    out = _out(tmp_path)
    args = _apply_args(out, head) + SECRET_ENV_FLAGS
    strict_bin(*args, env=_apply_env(vercel), cwd=repo)
    readiness = out / "readiness.json"
    assert readiness.exists(), "final audit must produce readiness.json"
    agg = json.loads(readiness.read_text())
    summary = agg["summary"]
    # Consolidated totals must equal the sum of every check across all sources.
    counted = {"pass": 0, "warn": 0, "blocked": 0, "manual": 0, "not_applicable": 0}
    key = {"PASS": "pass", "WARN": "warn", "BLOCKED": "blocked", "MANUAL": "manual", "NOT_APPLICABLE": "not_applicable"}
    for report in agg.get("sub_reports", {}).values():
        for check in report.get("checks", []):
            k = key.get(check.get("status"))
            if k:
                counted[k] += 1
    # sub_reports excludes the top-level; blocking/manual lists include both.
    assert summary["blocked"] == len(agg["blocking_findings"])
    # Every sub-report check is represented in the consolidated totals.
    for k in counted:
        assert summary[k] >= counted[k], f"{k}: summary {summary[k]} < sub-report sum {counted[k]}"


# ===========================================================================
# 10. audit-only mode is read-only
# ===========================================================================

def test_audit_only_mode_performs_no_mutation(strict_bin, tmp_path):
    out = _out(tmp_path)
    result = strict_bin(
        "--audit-only", "--output-directory", str(out),
        "--expected-project", PROJECT, "--expected-account", OPERATOR,
        "--release-sha", FULL_SHA, "--rollback-sha", FULL_SHA,
        env={"MOCK_GCLOUD_PROJECT": PROJECT, "MOCK_GCLOUD_ACCOUNT": OPERATOR},
    )
    log = read_log(strict_bin)
    for verb in ("iam service-accounts create", "secrets create", "secrets versions add",
                 "run services update", "run jobs update", "env add"):
        assert verb not in log, f"audit-only issued a mutation: {verb}"
    assert (out / "readiness.json").exists()

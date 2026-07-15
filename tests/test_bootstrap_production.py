"""Strict proofs for scripts/release/bootstrap-production.sh.

The mocks FAIL LOUDLY on any command the bootstrap is not explicitly authorized
to run, and reject every forbidden mutation (worker-job execution,
service-account key creation, project-wide secret accessor, unauthenticated
Cloud Run, Vercel deploy/promote/link, any provider call). Upstash is served by
a mock so tests never touch the real Upstash API.

Focus of this suite: the bootstrap ADOPTS the operator's existing Secret
Manager and Vercel resources, prompts only after inspection, configures the
LIVE Cloud Run env/secret references, and its final audit inspects live state
(not just a self-generated manifest).
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

# Existing (adopted) Secret Manager resource names.
S_SUPA_URL = "SUPABASE_URL"
S_SUPA_KEY = "SUPABASE_SECRET_KEY"
S_PROVIDER = "KIMI_API_KEY"
S_REDIS = "UPSTASH_REDIS_REST_TOKEN"
ALL_SECRET_NAMES = f"{S_SUPA_URL},{S_SUPA_KEY},{S_PROVIDER},{S_REDIS}"

# Secret sentinel VALUES that must never leak.
SUPABASE_SECRET = "sb_secret_DEADBEEFdeadbeef_supabase"
PROVIDER_SECRET = "PROVIDER-key-SECRET-0000-value-do-not-log"
UPSTASH_APIKEY_SECRET = "upstash_mgmt_SECRET_key_9999"
UPSTASH_REST_TOKEN_SECRET = "AaBbToken_UPSTASH_REST_SECRET_zzzz"
VERCEL_TOKEN_SECRET = "vercel_TOKEN_SECRET_abcdef"
ALL_SECRETS = [SUPABASE_SECRET, PROVIDER_SECRET, UPSTASH_APIKEY_SECRET, UPSTASH_REST_TOKEN_SECRET, VERCEL_TOKEN_SECRET]

API_URL = f"https://milo-agent-api-641579813332.{REGION}.run.app"

# ---------------------------------------------------------------------------
# Live Cloud Run describe fixtures (correctly configured).
# ---------------------------------------------------------------------------
_FLAGS_FALSE = {
    "MILO_ENABLE_RUN_CREATION": "false", "MILO_ENABLE_PROPOSAL_MUTATIONS": "false",
    "MILO_ENABLE_PROPOSAL_READS": "false", "MILO_ENABLE_RUN_CANCELLATION": "false",
    "MILO_ENABLE_EXECUTION_CONTROL": "false", "MILO_ENABLE_PAID_EXECUTION": "false",
}
_BUDGETS = {
    "MILO_MAX_COST_PER_RUN": "0.50", "MILO_DAILY_USER_BUDGET": "2",
    "MILO_DAILY_PROJECT_BUDGET": "10", "MILO_MAX_MODEL_CALLS_PER_RUN": "20",
    "MILO_MAX_TOTAL_TOKENS_PER_RUN": "200000", "MILO_MAX_RUN_DURATION_SECONDS": "1800",
}


def _env_list(plain: dict, secret_refs: dict) -> list:
    out = [{"name": k, "value": v} for k, v in plain.items()]
    for k, name in secret_refs.items():
        out.append({"name": k, "valueFrom": {"secretKeyRef": {"name": name, "key": "latest"}}})
    return out


def api_service_json(*, plain_over=None, secret_over=None, drop=None, sa=API_SA) -> dict:
    plain = {"ENVIRONMENT": "production", "ALLOWED_CORS_ORIGINS": "https://milo-agent-workspace.vercel.app",
             "JOB_LAUNCHER": "disabled", "GATEWAY_ALLOW_EXECUTION_ROUTES": "false",
             "MILO_GATEWAY_AUDIENCE": API_URL, "MILO_APPROVED_GATEWAY_IDENTITIES": GATEWAY_SA,
             "MILO_APPROVED_WORKER_IDENTITIES": WORKER_SA, "UPSTASH_REDIS_REST_URL": "https://prod-1.upstash.io"}
    plain.update(_FLAGS_FALSE); plain.update(_BUDGETS)
    secret_refs = {"SUPABASE_URL": S_SUPA_URL, "SUPABASE_SECRET_KEY": S_SUPA_KEY, "UPSTASH_REDIS_REST_TOKEN": S_REDIS}
    if plain_over:
        plain.update(plain_over)
    if secret_over:
        secret_refs.update(secret_over)
    for k in (drop or []):
        plain.pop(k, None); secret_refs.pop(k, None)
    return {"spec": {"template": {"spec": {"serviceAccountName": sa, "containers": [{"env": _env_list(plain, secret_refs)}]}}}}


def worker_job_json(*, plain_over=None, secret_over=None, drop=None, sa=WORKER_SA) -> dict:
    plain = {"ENVIRONMENT": "production", "MILO_WORKER_AUDIENCE": API_URL,
             "MILO_APPROVED_WORKER_IDENTITIES": WORKER_SA, "UPSTASH_REDIS_REST_URL": "https://prod-1.upstash.io"}
    plain.update(_FLAGS_FALSE); plain.update(_BUDGETS)
    secret_refs = {"SUPABASE_URL": S_SUPA_URL, "SUPABASE_SECRET_KEY": S_SUPA_KEY,
                   "KIMI_API_KEY": S_PROVIDER, "UPSTASH_REDIS_REST_TOKEN": S_REDIS}
    if plain_over:
        plain.update(plain_over)
    if secret_over:
        secret_refs.update(secret_over)
    for k in (drop or []):
        plain.pop(k, None); secret_refs.pop(k, None)
    return {"spec": {"template": {"spec": {"template": {"spec": {"serviceAccountName": sa, "containers": [{"env": _env_list(plain, secret_refs)}]}}}}}}


# ---------------------------------------------------------------------------
# Strict mock CLIs
# ---------------------------------------------------------------------------

GCLOUD_MOCK = r"""#!/usr/bin/env bash
printf 'gcloud %s\n' "$*" >> "$MOCK_LOG"
all="$*"
case "$all" in
  *"jobs run"*|*"jobs execute"*|*"keys create"*|*"services delete"*|*"jobs delete"*|\
  *"secrets delete"*|*" --allow-unauthenticated"*|*"projects add-iam-policy-binding"*|\
  *"projects set-iam-policy"*)
    printf 'MOCK-FORBIDDEN gcloud: %s\n' "$all" >&2; exit 97;;
esac
args=("$@")
in_list() { case ",$2," in *",$1,"*) return 0;; *) return 1;; esac; }
case "$all" in
  *"config get-value account"*) printf '%s\n' "${MOCK_GCLOUD_ACCOUNT:-operator@milo-prod.iam.gserviceaccount.com}";;
  *"config get-value project"*) printf '%s\n' "${MOCK_GCLOUD_PROJECT:-milo-prod}";;
  *"iam service-accounts describe"*)
    e="${args[3]:-}"
    if in_list "$e" "${MOCK_ERROR_SAS:-}"; then printf 'ERROR: PERMISSION_DENIED\n' >&2; exit 1; fi
    if in_list "$e" "${MOCK_EXISTING_SAS:-}"; then printf '%s\n' "$e"; exit 0; fi
    printf 'ERROR: NOT_FOUND: unknown service account %s\n' "$e" >&2; exit 1;;
  *"iam service-accounts create"*) exit 0;;
  *"iam service-accounts get-iam-policy"*)
    if [[ "${MOCK_WIF_BINDING:-present}" == "present" ]]; then printf '{"bindings":[{"role":"roles/iam.workloadIdentityUser","members":["principalSet://x"]}]}\n'; else printf '{"bindings":[]}\n'; fi;;
  *"iam workload-identity-pools providers describe"*) printf '{"oidc":{"issuerUri":"https://oidc.vercel.com/team"}}\n';;
  *"iam workload-identity-pools describe"*)
    case "${MOCK_WIF_POOL:-present}" in
      missing) printf 'ERROR: NOT_FOUND: pool\n' >&2; exit 1;;
      *) printf 'projects/p/locations/global/workloadIdentityPools/pool\n';;
    esac;;
  *"secrets describe"*)
    s="${args[2]:-}"
    if in_list "$s" "${MOCK_ERROR_SECRETS:-}"; then printf 'ERROR: PERMISSION_DENIED\n' >&2; exit 1; fi
    if in_list "$s" "${MOCK_EXISTING_SECRETS:-}"; then printf 'projects/p/secrets/%s\n' "$s"; exit 0; fi
    printf 'ERROR: NOT_FOUND: Secret [%s] not found\n' "$s" >&2; exit 1;;
  *"secrets create"*) exit 0;;
  *"secrets versions add"*) cat > /dev/null; exit 0;;
  *"secrets versions list"*)
    s="${args[3]:-}"
    if in_list "$s" "${MOCK_SECRET_VERSION_ERROR:-}"; then printf 'ERROR: PERMISSION_DENIED\n' >&2; exit 1; fi
    if in_list "$s" "${MOCK_SECRETS_WITH_VERSIONS:-}"; then printf '1\n'; else printf ''; fi;;
  *"secrets add-iam-policy-binding"*) exit 0;;
  *"secrets get-iam-policy"*) printf '%s\n' "${MOCK_SECRET_POLICY:-{\"bindings\":[]}}";;
  *"secrets list"*) printf '%s' "${MOCK_SECRETS_LIST-}";;
  *"run services update"*) if [[ "${MOCK_API_SVC:-present}" == "missing" ]]; then printf 'ERROR: NOT_FOUND: service\n' >&2; exit 1; fi; exit 0;;
  *"run jobs update"*) if [[ "${MOCK_WORKER_JOB:-present}" == "missing" ]]; then printf 'ERROR: NOT_FOUND: job\n' >&2; exit 1; fi; exit 0;;
  *"run services add-iam-policy-binding"*) exit 0;;
  *"run services describe"*) if [[ -n "${MOCK_SERVICE_JSON:-}" ]]; then cat "$MOCK_SERVICE_JSON"; else printf '{"spec":{"template":{"spec":{"serviceAccountName":"milo-api-sa@milo-prod.iam.gserviceaccount.com"}}}}\n'; fi;;
  *"run jobs describe"*) if [[ -n "${MOCK_JOB_JSON:-}" ]]; then cat "$MOCK_JOB_JSON"; else printf '{"spec":{"template":{"spec":{"template":{"spec":{"serviceAccountName":"milo-worker-sa@milo-prod.iam.gserviceaccount.com"}}}}}}\n'; fi;;
  *"run services get-iam-policy"*)
    if [[ "${MOCK_RUN_INVOKER:-present}" == "present" ]]; then printf '{"bindings":[{"role":"roles/run.invoker","members":["serviceAccount:milo-gateway-sa@milo-prod.iam.gserviceaccount.com"]}]}\n'; else printf '{"bindings":[]}\n'; fi;;
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
  *deploy*|*promote*|*link*|*"--prod"*)
    printf 'MOCK-FORBIDDEN vercel: %s\n' "$all" >&2; exit 97;;
esac
case "$all" in
  whoami*) printf '%s\n' "${MOCK_VERCEL_USER:-milo-team}";;
  "project inspect"*)
    case "${MOCK_VERCEL_INSPECT:-ok}" in
      fail) printf 'Error: Not authorized.\n' >&2; exit 1;;
      *) printf 'Project Name milo-agent-workspace\n  ID %s\n' "${MOCK_VERCEL_INSPECT_ID:-prj_linked0000000001}"
         if [[ -n "${MOCK_VERCEL_INSPECT_ORG:-}" ]]; then printf '  Owner %s\n' "$MOCK_VERCEL_INSPECT_ORG"; fi
         exit 0;;
    esac;;
  "env ls production"*)
    if [[ "${MOCK_VERCEL_ENV_FAIL:-}" == "1" ]]; then printf 'Error: not authorized\n' >&2; exit 1; fi
    for n in ${MOCK_VERCEL_ENV_NAMES:-}; do printf '%s Encrypted Production 1d\n' "$n"; done;;
  "env rm"*) exit 0;;
  "env add"*) cat > /dev/null; exit 0;;
  *) printf 'MOCK-UNAUTHORIZED vercel command: %s\n' "$all" >&2; exit 98;;
esac
"""

CURL_MOCK = r"""#!/usr/bin/env bash
printf 'curl %s\n' "$*" >> "$MOCK_LOG"
method=GET; url=""
args=("$@")
for ((i=0; i<${#args[@]}; i++)); do
  case "${args[$i]}" in
    -X) method="${args[$((i+1))]}";;
    http://*|https://*) url="${args[$i]}";;
  esac
done
emit() { printf '%s\n%s' "$1" "$2"; }
tok="${MOCK_UPSTASH_REST_TOKEN:-AaBbToken_UPSTASH_REST_SECRET_zzzz}"
case "$url" in
  *"/v2/redis/databases")
    if [[ "${MOCK_UPSTASH_LIST_FAIL:-}" == "1" ]]; then emit '{"error":"unauthorized"}' 401; exit 0; fi
    emit "${MOCK_UPSTASH_DBS:-[]}" 200;;
  *"/v2/redis/database/"*) emit "{\"database_id\":\"db_prod_1\",\"endpoint\":\"prod-1.upstash.io\",\"rest_token\":\"${tok}\"}" 200;;
  *"/v2/redis/database") if [[ "$method" == "POST" ]]; then emit '{"database_id":"db_prod_1","endpoint":"prod-1.upstash.io"}' 200; else emit '{}' 405; fi;;
  *) printf 'MOCK-UNAUTHORIZED curl url: %s\n' "$url" >&2; exit 98;;
esac
"""

MOCKS = {"gcloud": GCLOUD_MOCK, "vercel": VERCEL_MOCK, "curl": CURL_MOCK}

VERCEL_REUSE = "CLOUD_RUN_API_URL GCP_PROJECT_NUMBER GCP_WORKLOAD_IDENTITY_POOL_ID GCP_WORKLOAD_IDENTITY_POOL_PROVIDER_ID GCP_SERVICE_ACCOUNT_EMAIL NEXT_PUBLIC_SUPABASE_URL NEXT_PUBLIC_SUPABASE_ANON_KEY"


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
            capture_output=True, text=True, env=run_env, cwd=cwd or REPO, timeout=180,
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
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()
    return repo, head


def _linked_vercel(tmp_path: Path, pid="prj_linked0000000001", org="team_linked000") -> Path:
    d = tmp_path / "frontend"
    (d / ".vercel").mkdir(parents=True)
    (d / ".vercel" / "project.json").write_text(json.dumps({"projectId": pid, "orgId": org}))
    return d


def _out(tmp_path: Path) -> Path:
    return tmp_path / "operator-out"


def _write_live_fixtures(tmp_path: Path, service=None, job=None):
    svc = tmp_path / "live-api.json"
    jb = tmp_path / "live-job.json"
    svc.write_text(json.dumps(service if service is not None else api_service_json()))
    jb.write_text(json.dumps(job if job is not None else worker_job_json()))
    return svc, jb


def _apply_args(out: Path, sha: str):
    return [
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


def _apply_env(vercel_cwd: Path, svc=None, job=None, **over) -> dict:
    env = {
        "MILO_OPERATOR_ACK": ACK,
        "MOCK_GCLOUD_ACCOUNT": OPERATOR, "MOCK_GCLOUD_PROJECT": PROJECT,
        "MILO_BOOTSTRAP_VERCEL_CWD": str(vercel_cwd),
        "MOCK_UPSTASH_DBS": json.dumps([{"database_name": "milo-production", "database_id": "db_prod_1", "endpoint": "prod-1.upstash.io"}]),
        "MOCK_UPSTASH_REST_TOKEN": UPSTASH_REST_TOKEN_SECRET,
        # All four existing secrets already carry an enabled version (adoption).
        "MOCK_EXISTING_SECRETS": ALL_SECRET_NAMES,
        "MOCK_SECRETS_WITH_VERSIONS": ALL_SECRET_NAMES,
        # Existing Vercel production variables present (reuse).
        "MOCK_VERCEL_ENV_NAMES": VERCEL_REUSE,
        # Secret INPUT env vars (only used if a secret must be created/repaired).
        "SUPA_URL": "https://vvhtaqgkgkalpfcbuvag.supabase.co",
        "SUPA_KEY": SUPABASE_SECRET, "PROV_KEY": PROVIDER_SECRET,
        "UP_EMAIL": "ops@milo.test", "UP_APIKEY": UPSTASH_APIKEY_SECRET, "VERCEL_TOK": VERCEL_TOKEN_SECRET,
    }
    if svc or job:
        s, j = _write_live_fixtures(vercel_cwd.parent, svc, job)
        env["MOCK_SERVICE_JSON"] = str(s)
        env["MOCK_JOB_JSON"] = str(j)
    env.update(over)
    return env


SECRET_ENV_FLAGS = [
    "--supabase-url-env", "SUPA_URL", "--supabase-key-env", "SUPA_KEY",
    "--provider-key-env", "PROV_KEY", "--upstash-email-env", "UP_EMAIL",
    "--upstash-apikey-env", "UP_APIKEY", "--vercel-token-env", "VERCEL_TOK",
]


def _apply(strict_bin, tmp_path, *, svc="ok", job="ok", extra_args=None, **env_over):
    repo, head = _git_repo(tmp_path)
    vercel = _linked_vercel(tmp_path)
    out = _out(tmp_path)
    args = _apply_args(out, head) + SECRET_ENV_FLAGS + (extra_args or [])
    svc_j = api_service_json() if svc == "ok" else svc
    job_j = worker_job_json() if job == "ok" else job
    env = _apply_env(vercel, svc=svc_j, job=job_j, **env_over)
    return strict_bin(*args, env=env, cwd=repo), out


# ===========================================================================
# 1. Default mode is plan / read-only
# ===========================================================================

def test_default_mode_is_plan_and_read_only(strict_bin, tmp_path):
    out = _out(tmp_path); vercel = _linked_vercel(tmp_path)
    result = strict_bin(
        "--output-directory", str(out), "--expected-project", PROJECT, "--expected-account", OPERATOR,
        "--api-sa", API_SA, "--worker-sa", WORKER_SA, "--gateway-sa", GATEWAY_SA,
        env={"MOCK_GCLOUD_PROJECT": PROJECT, "MOCK_GCLOUD_ACCOUNT": OPERATOR,
             "MILO_BOOTSTRAP_VERCEL_CWD": str(vercel), "MOCK_EXISTING_SECRETS": ALL_SECRET_NAMES,
             "MOCK_SECRETS_WITH_VERSIONS": ALL_SECRET_NAMES},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[PASS] mode — bootstrap mode: plan" in result.stdout
    log = read_log(strict_bin)
    for verb in ("iam service-accounts create", "secrets create", "secrets versions add",
                 "add-iam-policy-binding", "run services update", "run jobs update", "env add", "env rm"):
        assert verb not in log, f"plan mode issued a mutation: {verb}"


# ===========================================================================
# 2. Secret adoption + inspect-before-prompt
# ===========================================================================

def test_adopt_existing_secrets_zero_prompt_zero_add_zero_create(strict_bin, tmp_path):
    result, out = _apply(strict_bin, tmp_path)  # all four secrets exist + enabled version
    assert_no_mock_errors(result)
    log = read_log(strict_bin)
    assert "secrets create" not in log, "must not re-create adopted secrets"
    assert "secrets versions add" not in log, "must not add a version when one is already enabled"
    for label in ("supabase-url", "supabase-key", "provider-key", "redis-token"):
        assert f"[PASS] gcp:secret:{label}:reuse" in result.stdout
    report = json.loads((out / "bootstrap-apply.json").read_text())
    assert any("adopted existing secret" in a for a in report["applied_actions"])


def test_missing_secret_with_version_only_that_one_added(strict_bin, tmp_path):
    # SUPABASE_SECRET_KEY exists but has NO enabled version; the other three are
    # adopted. Only SUPABASE_SECRET_KEY gets a version added.
    result, out = _apply(
        strict_bin, tmp_path,
        MOCK_SECRETS_WITH_VERSIONS=f"{S_SUPA_URL},{S_PROVIDER},{S_REDIS}",  # not S_SUPA_KEY
    )
    assert_no_mock_errors(result)
    log = read_log(strict_bin)
    adds = [ln for ln in log.splitlines() if "secrets versions add" in ln]
    assert len(adds) == 1, f"exactly one versions add expected, got: {adds}"
    assert S_SUPA_KEY in adds[0]
    assert "secrets create" not in log  # it exists, only the version was missing


def test_secret_permission_error_is_not_no_version(strict_bin, tmp_path):
    # versions list returns a permission error for SUPABASE_SECRET_KEY -> must be
    # INSPECTION_ERROR (BLOCKED), never treated as "no enabled version".
    result, _ = _apply(strict_bin, tmp_path, MOCK_SECRET_VERSION_ERROR=S_SUPA_KEY)
    assert "[BLOCKED] gcp:secret:supabase-key" in result.stdout
    assert "refusing to prompt or create blindly" in result.stdout
    log = read_log(strict_bin)
    # No version added and no creation for the errored secret.
    assert not any("secrets versions add " + S_SUPA_KEY in ln for ln in log.splitlines())


def test_missing_secret_without_value_is_manual_not_created(strict_bin, tmp_path):
    # KIMI_API_KEY missing and NO value supplied -> MANUAL, no create, no prompt.
    repo, head = _git_repo(tmp_path)
    vercel = _linked_vercel(tmp_path)
    out = _out(tmp_path)
    args = _apply_args(out, head) + [
        "--upstash-email-env", "UP_EMAIL", "--upstash-apikey-env", "UP_APIKEY", "--vercel-token-env", "VERCEL_TOK",
    ]  # deliberately NO --provider-key-env
    env = _apply_env(vercel, svc=api_service_json(), job=worker_job_json(),
                     MOCK_SECRETS_WITH_VERSIONS=f"{S_SUPA_URL},{S_SUPA_KEY},{S_REDIS}",
                     MOCK_EXISTING_SECRETS=f"{S_SUPA_URL},{S_SUPA_KEY},{S_REDIS}")  # KIMI missing
    result = strict_bin(*args, env=env, cwd=repo)
    assert "[MANUAL] gcp:secret:provider-key" in result.stdout
    log = read_log(strict_bin)
    assert not any(f"secrets create {S_PROVIDER}" in ln for ln in log.splitlines())


# ===========================================================================
# 3. Apply guard (unchanged safety)
# ===========================================================================

def test_guard_wrong_project_blocks(strict_bin, tmp_path):
    result, out = _apply(strict_bin, tmp_path, MOCK_GCLOUD_PROJECT="other-project")
    assert result.returncode != 0
    assert "does not match --expected-project" in result.stdout
    assert "iam service-accounts create" not in read_log(strict_bin)
    assert json.loads((out / "bootstrap-apply.json").read_text())["status"] == "guard-blocked"


def test_guard_wrong_sha_blocks(strict_bin, tmp_path):
    repo, _head = _git_repo(tmp_path)
    vercel = _linked_vercel(tmp_path)
    out = _out(tmp_path)
    args = _apply_args(out, FULL_SHA) + SECRET_ENV_FLAGS  # FULL_SHA != HEAD
    result = strict_bin(*args, env=_apply_env(vercel), cwd=repo)
    assert result.returncode != 0
    assert "does not equal the intended release SHA" in result.stdout
    assert "iam service-accounts create" not in read_log(strict_bin)


def test_guard_dirty_worktree_blocks(strict_bin, tmp_path):
    repo, head = _git_repo(tmp_path)
    (repo / "dirty.txt").write_text("x")
    vercel = _linked_vercel(tmp_path)
    out = _out(tmp_path)
    args = _apply_args(out, head) + SECRET_ENV_FLAGS
    result = strict_bin(*args, env=_apply_env(vercel), cwd=repo)
    assert result.returncode != 0
    assert "dirty Git worktree" in result.stdout


def test_guard_missing_ack_blocks(strict_bin, tmp_path):
    result, _ = _apply(strict_bin, tmp_path, MILO_OPERATOR_ACK="")
    assert result.returncode != 0
    assert "MILO_OPERATOR_ACK" in result.stdout


def test_shared_api_worker_identity_blocked_before_mutation(strict_bin, tmp_path):
    repo, head = _git_repo(tmp_path)
    vercel = _linked_vercel(tmp_path)
    out = _out(tmp_path)
    args = _apply_args(out, head)
    args[args.index("--worker-sa") + 1] = API_SA
    args += SECRET_ENV_FLAGS
    result = strict_bin(*args, env=_apply_env(vercel), cwd=repo)
    assert result.returncode != 0
    assert "[BLOCKED] identity:api-worker" in result.stdout
    assert "iam service-accounts create" not in read_log(strict_bin)


# ===========================================================================
# 4. No keys, worker never executed, no provider, flags disabled
# ===========================================================================

def test_never_creates_service_account_keys(strict_bin, tmp_path):
    result, _ = _apply(strict_bin, tmp_path)
    assert "MOCK-FORBIDDEN" not in (result.stdout + result.stderr)
    assert "keys create" not in read_log(strict_bin)


def test_worker_job_never_executed_but_configured(strict_bin, tmp_path):
    _apply(strict_bin, tmp_path)
    log = read_log(strict_bin)
    assert "jobs run" not in log and "jobs execute" not in log
    assert "run jobs update milo-agent-worker" in log  # configured, not executed


def test_provider_never_called(strict_bin, tmp_path):
    _apply(strict_bin, tmp_path)
    for line in read_log(strict_bin).splitlines():
        if line.startswith("curl "):
            assert "/v2/redis/" in line, f"unexpected curl target: {line}"
        assert "moonshot" not in line.lower() and "api.kimi" not in line.lower()


def test_execution_flags_false_in_manifest_and_cloud_run(strict_bin, tmp_path):
    _apply(strict_bin, tmp_path)
    log = read_log(strict_bin)
    # Cloud Run updates set every execution flag to false.
    upd = [ln for ln in log.splitlines() if "run services update" in ln or "run jobs update" in ln]
    assert upd, "expected Cloud Run configuration updates"
    for ln in upd:
        assert "MILO_ENABLE_PAID_EXECUTION=false" in ln
        assert "MILO_ENABLE_RUN_CREATION=false" in ln
    api_upd = [ln for ln in upd if "services update" in ln][0]
    assert "JOB_LAUNCHER=disabled" in api_upd
    assert "GATEWAY_ALLOW_EXECUTION_ROUTES=false" in api_upd


# ===========================================================================
# 5. Live Cloud Run env + secret references are configured
# ===========================================================================

def test_cloud_run_env_and_secret_refs_configured(strict_bin, tmp_path):
    result, _ = _apply(strict_bin, tmp_path)
    log = read_log(strict_bin)
    api_upd = [ln for ln in log.splitlines() if "run services update" in ln][0]
    assert "--update-secrets" in api_upd
    assert f"SUPABASE_SECRET_KEY={S_SUPA_KEY}:latest" in api_upd
    assert f"UPSTASH_REDIS_REST_TOKEN={S_REDIS}:latest" in api_upd
    assert "MILO_GATEWAY_AUDIENCE=" in api_upd
    worker_upd = [ln for ln in log.splitlines() if "run jobs update" in ln][0]
    assert f"KIMI_API_KEY={S_PROVIDER}:latest" in worker_upd
    # Live audit confirms the configured env/secret refs on the (fixture) live state.
    assert "[PASS] live:api:secret-ref:SUPABASE_SECRET_KEY" in result.stdout
    assert "[PASS] live:worker:secret-ref:KIMI_API_KEY" in result.stdout
    assert "[PASS] live:api:job-launcher" in result.stdout


def test_final_audit_fails_when_live_config_differs_from_manifest(strict_bin, tmp_path):
    # The generated manifest is valid, but the LIVE API service is missing the
    # Supabase secret reference and has an execution flag enabled -> the audit
    # must FAIL (it does not trust the manifest alone).
    bad_service = api_service_json(plain_over={"MILO_ENABLE_RUN_CREATION": "true"}, drop=["SUPABASE_SECRET_KEY"])
    result, out = _apply(strict_bin, tmp_path, svc=bad_service)
    assert result.returncode != 0
    assert "[BLOCKED] live:api:secret-ref:SUPABASE_SECRET_KEY" in result.stdout
    assert "[BLOCKED] live:api:flag:MILO_ENABLE_RUN_CREATION" in result.stdout
    report = json.loads((out / "bootstrap-apply.json").read_text())
    assert report["status"] == "partial-failure"


# ===========================================================================
# 6. Vercel adoption
# ===========================================================================

def test_vercel_existing_vars_reused_not_rewritten(strict_bin, tmp_path):
    result, _ = _apply(strict_bin, tmp_path)
    assert_no_mock_errors(result)
    log = read_log(strict_bin)
    # Reuse vars are only listed, never re-added or removed.
    for v in ("CLOUD_RUN_API_URL", "GCP_SERVICE_ACCOUNT_EMAIL", "NEXT_PUBLIC_SUPABASE_ANON_KEY"):
        assert f"[PASS] vercel:reuse:{v}" in result.stdout
        assert f"env add {v} " not in log
        assert f"env rm {v} " not in log


def test_vercel_managed_kill_switches_set_false(strict_bin, tmp_path):
    # Neither kill switch present yet -> both CREATED as false.
    result, _ = _apply(strict_bin, tmp_path)
    log = read_log(strict_bin)
    assert "env add GATEWAY_ALLOW_EXECUTION_ROUTES production" in log
    assert "env add NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI production" in log
    assert "[PASS] vercel:var:GATEWAY_ALLOW_EXECUTION_ROUTES" in result.stdout


def test_vercel_existing_managed_var_is_updated_not_blind_add(strict_bin, tmp_path):
    # GATEWAY_ALLOW_EXECUTION_ROUTES already present -> UPDATE path = rm then add.
    names = VERCEL_REUSE + " GATEWAY_ALLOW_EXECUTION_ROUTES"
    result, _ = _apply(strict_bin, tmp_path, MOCK_VERCEL_ENV_NAMES=names)
    log = read_log(strict_bin)
    assert "env rm GATEWAY_ALLOW_EXECUTION_ROUTES production" in log
    assert "env add GATEWAY_ALLOW_EXECUTION_ROUTES production" in log
    assert "UPDATE" in result.stdout


def test_vercel_never_adds_server_secrets(strict_bin, tmp_path):
    _apply(strict_bin, tmp_path)
    log = read_log(strict_bin)
    for forbidden in ("KIMI_API_KEY", "MOONSHOT_API_KEY", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SECRET_KEY"):
        assert f"env add {forbidden}" not in log


def test_vercel_org_mismatch_blocks_before_writes(strict_bin, tmp_path):
    result, _ = _apply(strict_bin, tmp_path, MOCK_VERCEL_INSPECT_ORG="team_intruder999")
    assert "[BLOCKED] vercel:project-identity" in result.stdout
    assert "env add" not in read_log(strict_bin)
    assert "env rm" not in read_log(strict_bin)


# ===========================================================================
# 7. WIF federation adoption/verification
# ===========================================================================

def test_wif_adopted_and_verified(strict_bin, tmp_path):
    result, _ = _apply(strict_bin, tmp_path, extra_args=["--wif-pool", "milo-pool", "--wif-provider", "vercel"])
    assert_no_mock_errors(result)
    assert "[PASS] wif:pool" in result.stdout
    assert "[PASS] wif:provider" in result.stdout
    assert "[PASS] wif:gateway-binding" in result.stdout
    assert "[PASS] wif:run-invoker" in result.stdout


def test_wif_missing_gateway_binding_blocks(strict_bin, tmp_path):
    result, _ = _apply(strict_bin, tmp_path, extra_args=["--wif-pool", "milo-pool", "--wif-provider", "vercel"],
                       MOCK_WIF_BINDING="absent")
    assert "[BLOCKED] wif:gateway-binding" in result.stdout


def test_wif_run_invoker_bound_when_absent(strict_bin, tmp_path):
    result, _ = _apply(strict_bin, tmp_path, extra_args=["--wif-pool", "milo-pool", "--wif-provider", "vercel"],
                       MOCK_RUN_INVOKER="absent")
    log = read_log(strict_bin)
    assert "run services add-iam-policy-binding milo-agent-api" in log
    assert "roles/run.invoker" in log
    assert "[PASS] wif:run-invoker" in result.stdout


# ===========================================================================
# 8. Secret hygiene, idempotency, partial failure, aggregation
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


def test_secrets_never_appear_in_output(strict_bin, tmp_path):
    result, out = _apply(strict_bin, tmp_path)
    haystack = _all_output_text(result, out)
    for secret in ALL_SECRETS:
        assert secret not in haystack, f"secret leaked: {secret[:8]}..."


def test_repeated_apply_is_idempotent(strict_bin, tmp_path):
    # Everything already exists/adopted -> zero create, zero versions add.
    result, _ = _apply(strict_bin, tmp_path, MOCK_EXISTING_SAS=f"{API_SA},{WORKER_SA},{GATEWAY_SA}")
    log = read_log(strict_bin)
    assert "iam service-accounts create" not in log
    assert "secrets create" not in log
    assert "secrets versions add" not in log
    assert "already present" in result.stdout


def test_partial_failure_yields_recovery_plan_and_no_false_success(strict_bin, tmp_path):
    result, out = _apply(strict_bin, tmp_path, MOCK_UPSTASH_LIST_FAIL="1")
    assert result.returncode != 0
    assert "APPLY COMPLETE" not in result.stdout
    assert "partial failure" in result.stdout.lower()
    report = json.loads((out / "bootstrap-apply.json").read_text())
    assert report["status"] == "partial-failure"
    assert len(report["recovery_steps"]) >= 1


def test_upstash_absent_credentials_is_manual(strict_bin, tmp_path):
    out = _out(tmp_path)
    result = strict_bin(
        "--plan", "--output-directory", str(out), "--expected-project", PROJECT,
        env={"MOCK_GCLOUD_PROJECT": PROJECT, "MOCK_EXISTING_SECRETS": ALL_SECRET_NAMES,
             "MOCK_SECRETS_WITH_VERSIONS": ALL_SECRET_NAMES},
    )
    assert "[MANUAL] upstash" in result.stdout
    assert "/v2/redis/" not in read_log(strict_bin)


def test_final_audit_runs_and_aggregation_consistent(strict_bin, tmp_path):
    _result, out = _apply(strict_bin, tmp_path)
    readiness = out / "readiness.json"
    assert readiness.exists()
    agg = json.loads(readiness.read_text())
    summary = agg["summary"]
    assert summary["blocked"] == len(agg["blocking_findings"])


def test_audit_only_mode_is_read_only(strict_bin, tmp_path):
    out = _out(tmp_path)
    svc, jb = _write_live_fixtures(tmp_path, api_service_json(), worker_job_json())
    result = strict_bin(
        "--audit-only", "--output-directory", str(out),
        "--expected-project", PROJECT, "--expected-account", OPERATOR,
        "--release-sha", FULL_SHA, "--rollback-sha", FULL_SHA,
        env={"MOCK_GCLOUD_PROJECT": PROJECT, "MOCK_GCLOUD_ACCOUNT": OPERATOR,
             "MOCK_SERVICE_JSON": str(svc), "MOCK_JOB_JSON": str(jb),
             "MOCK_EXISTING_SECRETS": ALL_SECRET_NAMES, "MOCK_SECRETS_WITH_VERSIONS": ALL_SECRET_NAMES},
    )
    log = read_log(strict_bin)
    for verb in ("iam service-accounts create", "secrets create", "secrets versions add",
                 "run services update", "run jobs update", "env add", "env rm"):
        assert verb not in log, f"audit-only issued a mutation: {verb}"
    assert (out / "readiness.json").exists()
    # Live audit still runs read-only describes.
    assert "run services describe milo-agent-api" in log


# ===========================================================================
# 9. Generated manifest
# ===========================================================================

def test_generated_manifest_has_no_placeholders_and_adopted_names(strict_bin, tmp_path):
    _result, out = _apply(strict_bin, tmp_path)
    manifest = out / "milo-production.yaml"
    val = subprocess.run(
        ["python3", str(RELEASE / "validate_production_manifest.py"), "--manifest", str(manifest), "--mode", "apply"],
        capture_output=True, text=True,
    )
    assert val.returncode == 0, val.stdout + val.stderr
    text = manifest.read_text()
    assert f'name: "{S_SUPA_KEY}"' in text
    assert f'name: "{S_PROVIDER}"' in text
    assert f'name: "{S_REDIS}"' in text

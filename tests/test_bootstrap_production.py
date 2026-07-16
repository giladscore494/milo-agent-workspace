"""Strict proofs for scripts/release/bootstrap-production.sh (hardened).

Mocks FAIL LOUDLY on any command the bootstrap is not authorized to run, reject
every forbidden mutation, and reject a Vercel `--token` argv (the token must
travel only in the environment). Upstash is served by a mock. These tests prove
the eight hardening blockers: exact Vercel identity from a clean checkout,
exact live Cloud Run value validation, the official Upstash create/select/URL
contract, the Redis credential transaction (pinned version + fingerprint), and
exact WIF verification.
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
API_URL = f"https://milo-agent-api-641579813332.{REGION}.run.app"
ORIGIN = "https://milo-agent-workspace.vercel.app"

S_SUPA_URL = "SUPABASE_URL"
S_SUPA_KEY = "SUPABASE_SECRET_KEY"
S_PROVIDER = "KIMI_API_KEY"
S_REDIS = "UPSTASH_REDIS_REST_TOKEN"
ALL_SECRET_NAMES = f"{S_SUPA_URL},{S_SUPA_KEY},{S_PROVIDER},{S_REDIS}"

SUPABASE_SECRET = "sb_secret_DEADBEEFdeadbeef_supabase"
PROVIDER_SECRET = "PROVIDER-key-SECRET-0000-value-do-not-log"
UPSTASH_APIKEY_SECRET = "upstash_mgmt_SECRET_key_9999"
UPSTASH_REST_TOKEN_SECRET = "AaBbToken_UPSTASH_REST_SECRET_zzzz"
VERCEL_TOKEN_SECRET = "vercel_TOKEN_SECRET_abcdef"
REDIS_FP = "12266e478e28faf0"  # sha256(UPSTASH_REST_TOKEN_SECRET)[:16]
ALL_SECRETS = [SUPABASE_SECRET, PROVIDER_SECRET, UPSTASH_APIKEY_SECRET, UPSTASH_REST_TOKEN_SECRET, VERCEL_TOKEN_SECRET]

VPID = "prj_linked0000000001"
VORG = "team_linked000"

WIF_POOL = "milo-pool"
WIF_PROVIDER = "vercel"
WIF_ISSUER = "https://oidc.vercel.com/milo-team"
WIF_AUDIENCE = "https://vercel.com/milo-team"
WIF_COND = "assertion.aud == 'milo'"
WIF_MAPPING = json.dumps({"google.subject": "assertion.sub", "attribute.aud": "assertion.aud"})
WIF_PRINCIPAL = "principalSet://iam.googleapis.com/projects/641579813332/locations/global/workloadIdentityPools/milo-pool/attribute.aud/milo"

# Live Cloud Run describe fixtures (correct).
_FLAGS = {k: "false" for k in (
    "MILO_ENABLE_RUN_CREATION", "MILO_ENABLE_PROPOSAL_MUTATIONS", "MILO_ENABLE_PROPOSAL_READS",
    "MILO_ENABLE_RUN_CANCELLATION", "MILO_ENABLE_EXECUTION_CONTROL", "MILO_ENABLE_PAID_EXECUTION")}
_BUDGETS = {"MILO_MAX_COST_PER_RUN": "0.50", "MILO_DAILY_USER_BUDGET": "2", "MILO_DAILY_PROJECT_BUDGET": "10",
            "MILO_MAX_MODEL_CALLS_PER_RUN": "20", "MILO_MAX_TOTAL_TOKENS_PER_RUN": "200000", "MILO_MAX_RUN_DURATION_SECONDS": "1800"}
REDIS_URL = "https://prod-1.upstash.io"
REDIS_VER = "4"


def _env_list(plain, secret_refs):
    out = [{"name": k, "value": v} for k, v in plain.items()]
    for k, (name, ver) in secret_refs.items():
        out.append({"name": k, "valueFrom": {"secretKeyRef": {"name": name, "key": ver}}})
    return out


def api_service_json(*, plain_over=None, secret_over=None, drop=None, sa=API_SA):
    plain = {"ENVIRONMENT": "production", "ALLOWED_CORS_ORIGINS": ORIGIN, "JOB_LAUNCHER": "disabled",
             "GATEWAY_ALLOW_EXECUTION_ROUTES": "false", "MILO_GATEWAY_AUDIENCE": API_URL,
             "MILO_APPROVED_GATEWAY_IDENTITIES": GATEWAY_SA, "MILO_APPROVED_WORKER_IDENTITIES": WORKER_SA,
             "UPSTASH_REDIS_REST_URL": REDIS_URL,
             "MILO_BOOTSTRAP_SHA": FULL_SHA, "MILO_REDIS_DB_ID": "db_prod_1",
             "MILO_REDIS_TOKEN_FINGERPRINT": REDIS_FP, "MILO_REDIS_SECRET_VERSION": REDIS_VER}
    plain.update(_FLAGS); plain.update(_BUDGETS)
    refs = {"SUPABASE_URL": (S_SUPA_URL, "latest"), "SUPABASE_SECRET_KEY": (S_SUPA_KEY, "latest"),
            "UPSTASH_REDIS_REST_TOKEN": (S_REDIS, REDIS_VER)}
    if plain_over:
        plain.update(plain_over)
    if secret_over:
        refs.update(secret_over)
    for k in (drop or []):
        plain.pop(k, None); refs.pop(k, None)
    return {"spec": {"template": {"spec": {"serviceAccountName": sa, "containers": [{"env": _env_list(plain, refs)}]}}}}


def worker_job_json(*, plain_over=None, secret_over=None, drop=None, sa=WORKER_SA):
    plain = {"ENVIRONMENT": "production", "MILO_WORKER_AUDIENCE": API_URL,
             "MILO_APPROVED_WORKER_IDENTITIES": WORKER_SA, "UPSTASH_REDIS_REST_URL": REDIS_URL,
             "MILO_BOOTSTRAP_SHA": FULL_SHA, "MILO_REDIS_DB_ID": "db_prod_1",
             "MILO_REDIS_TOKEN_FINGERPRINT": REDIS_FP, "MILO_REDIS_SECRET_VERSION": REDIS_VER}
    plain.update(_FLAGS); plain.update(_BUDGETS)
    refs = {"SUPABASE_URL": (S_SUPA_URL, "latest"), "SUPABASE_SECRET_KEY": (S_SUPA_KEY, "latest"),
            "KIMI_API_KEY": (S_PROVIDER, "latest"), "UPSTASH_REDIS_REST_TOKEN": (S_REDIS, REDIS_VER)}
    if plain_over:
        plain.update(plain_over)
    if secret_over:
        refs.update(secret_over)
    for k in (drop or []):
        plain.pop(k, None); refs.pop(k, None)
    return {"spec": {"template": {"spec": {"template": {"spec": {"serviceAccountName": sa, "containers": [{"env": _env_list(plain, refs)}]}}}}}}


PROVIDER_JSON_OK = json.dumps({
    "oidc": {"issuerUri": WIF_ISSUER, "allowedAudiences": [WIF_AUDIENCE]},
    "attributeMapping": {"google.subject": "assertion.sub", "attribute.aud": "assertion.aud"},
    "attributeCondition": WIF_COND,
})
GATEWAY_POLICY_OK = json.dumps({"bindings": [{"role": "roles/iam.workloadIdentityUser", "members": [WIF_PRINCIPAL]}]})
RUN_POLICY_OK = json.dumps({"bindings": [{"role": "roles/run.invoker", "members": [f"serviceAccount:{GATEWAY_SA}"]}]})

# ---------------------------------------------------------------------------
GCLOUD_MOCK = r"""#!/usr/bin/env bash
printf 'gcloud %s\n' "$*" >> "$MOCK_LOG"
all="$*"
case "$all" in
  *"jobs run"*|*"jobs execute"*|*"keys create"*|*"services delete"*|*"jobs delete"*|\
  *"secrets delete"*|*" --allow-unauthenticated"*|*"projects add-iam-policy-binding"*|*"projects set-iam-policy"*)
    printf 'MOCK-FORBIDDEN gcloud: %s\n' "$all" >&2; exit 97;;
esac
args=("$@")
empty_pol='{"bindings":[]}'; empty_json='{}'
in_list() { case ",$2," in *",$1,"*) return 0;; *) return 1;; esac; }
case "$all" in
  *"config get-value account"*) printf '%s\n' "${MOCK_GCLOUD_ACCOUNT:-operator@milo-prod.iam.gserviceaccount.com}";;
  *"config get-value project"*) printf '%s\n' "${MOCK_GCLOUD_PROJECT:-milo-prod}";;
  *"iam service-accounts describe"*)
    e="${args[3]:-}"
    in_list "$e" "${MOCK_ERROR_SAS:-}" && { printf 'ERROR: PERMISSION_DENIED\n' >&2; exit 1; }
    in_list "$e" "${MOCK_EXISTING_SAS:-}" && { printf '%s\n' "$e"; exit 0; }
    printf 'ERROR: NOT_FOUND: %s\n' "$e" >&2; exit 1;;
  *"iam service-accounts create"*) exit 0;;
  *"iam service-accounts get-iam-policy"*) printf '%s\n' "${MOCK_WIF_GATEWAY_POLICY:-$empty_pol}";;
  *"iam workload-identity-pools providers describe"*) printf '%s\n' "${MOCK_WIF_PROVIDER_JSON:-$empty_json}";;
  *"iam workload-identity-pools describe"*)
    [[ "${MOCK_WIF_POOL:-present}" == "missing" ]] && { printf 'ERROR: NOT_FOUND: pool\n' >&2; exit 1; }
    [[ "${MOCK_WIF_POOL:-present}" == "error" ]] && { printf 'ERROR: PERMISSION_DENIED\n' >&2; exit 1; }
    printf 'projects/p/locations/global/workloadIdentityPools/pool\n';;
  *"secrets describe"*)
    s="${args[2]:-}"
    in_list "$s" "${MOCK_ERROR_SECRETS:-}" && { printf 'ERROR: PERMISSION_DENIED\n' >&2; exit 1; }
    in_list "$s" "${MOCK_EXISTING_SECRETS:-}" && { printf 'projects/p/secrets/%s\n' "$s"; exit 0; }
    printf 'ERROR: NOT_FOUND: Secret [%s] not found\n' "$s" >&2; exit 1;;
  *"secrets create"*) exit 0;;
  *"secrets versions access"*)
    case "${MOCK_REDIS_ACCESS_MODE:-ok}" in
      denied) printf 'ERROR: PERMISSION_DENIED: secretmanager.versions.access denied
' >&2; exit 1;;
      api) printf 'ERROR: INTERNAL: backend error
' >&2; exit 1;;
      empty) printf '';;
      *) printf '%s' "${MOCK_REDIS_SM_PAYLOAD-$MOCK_UPSTASH_REST_TOKEN}";;
    esac;;
  *"secrets versions add"*)
    cat > /dev/null
    printf 'projects/p/secrets/x/versions/%s\n' "${MOCK_REDIS_ADD_VERSION:-4}";;
  *"secrets versions list"*)
    s="${args[3]:-}"
    in_list "$s" "${MOCK_SECRET_VERSION_ERROR:-}" && { printf 'ERROR: PERMISSION_DENIED\n' >&2; exit 1; }
    if [[ "$s" == "UPSTASH_REDIS_REST_TOKEN" ]]; then
      in_list "$s" "${MOCK_SECRETS_WITH_VERSIONS:-}" && printf 'projects/p/secrets/%s/versions/%s\n' "$s" "${MOCK_REDIS_ENABLED_VERSION:-3}" || printf ''
    else
      in_list "$s" "${MOCK_SECRETS_WITH_VERSIONS:-}" && printf '1\n' || printf ''
    fi;;
  *"secrets add-iam-policy-binding"*) exit 0;;
  *"secrets get-iam-policy"*)
    case "${MOCK_SECRET_POLICY:-}" in
      __DENIED__) printf 'ERROR: PERMISSION_DENIED: caller lacks secretmanager.secrets.getIamPolicy\n' >&2; exit 1;;
      __APIERROR__) printf 'ERROR: INTERNAL: backend error\n' >&2; exit 1;;
    esac
    if [[ -n "${MOCK_SECRET_POLICY:-}" ]]; then
      printf '%s\n' "${MOCK_SECRET_POLICY}"
    else
      m=""
      for x in ${MOCK_SECRET_ACCESSOR_MEMBERS:-}; do m="${m:+$m,}\"$x\""; done
      printf '{"bindings":[{"role":"roles/secretmanager.secretAccessor","members":[%s]}]}\n' "$m"
    fi;;
  *"secrets list"*) printf '%s' "${MOCK_SECRETS_LIST-}";;
  *"run services update"*) [[ "${MOCK_API_SVC:-present}" == "missing" ]] && { printf 'ERROR: NOT_FOUND\n' >&2; exit 1; }; exit 0;;
  *"run jobs update"*) [[ "${MOCK_WORKER_JOB:-present}" == "missing" ]] && { printf 'ERROR: NOT_FOUND\n' >&2; exit 1; }; exit 0;;
  *"run services add-iam-policy-binding"*) exit 0;;
  *"run services describe"*) [[ -n "${MOCK_SERVICE_JSON:-}" ]] && cat "$MOCK_SERVICE_JSON" || printf '{}';;
  *"run jobs describe"*) [[ -n "${MOCK_JOB_JSON:-}" ]] && cat "$MOCK_JOB_JSON" || printf '{}';;
  *"run services get-iam-policy"*)
    # Failure sentinels: the initial policy read can be denied or fail.
    case "${MOCK_RUN_INVOKER_POLICY:-}" in
      __DENIED__) printf 'ERROR: PERMISSION_DENIED: caller lacks run.services.getIamPolicy\n' >&2; exit 1;;
      __APIERROR__) printf 'ERROR: INTERNAL: backend error\n' >&2; exit 1;;
    esac
    # Simulate a mutated live policy: once the bind was issued, return the
    # AFTER policy (when configured) so re-verification reads post-mutation state.
    if [[ -n "${MOCK_RUN_INVOKER_POLICY_AFTER:-}" ]] && grep -q "run services add-iam-policy-binding" "$MOCK_LOG" 2> /dev/null; then
      printf '%s\n' "${MOCK_RUN_INVOKER_POLICY_AFTER}"
    else
      printf '%s\n' "${MOCK_RUN_INVOKER_POLICY:-$empty_pol}"
    fi;;
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
  *"--token"*) printf 'MOCK-FORBIDDEN vercel token in argv: %s\n' "$all" >&2; exit 97;;
  *deploy*|*promote*|*redeploy*|*link*|*"--prod"*|*"env rm"*|*"env remove"*)
    printf 'MOCK-FORBIDDEN vercel: %s\n' "$all" >&2; exit 97;;
esac
case "$all" in
  "--version")
    printf 'Vercel CLI %s\n%s\n' "${MOCK_VERCEL_VERSION:-56.2.1}" "${MOCK_VERCEL_VERSION:-56.2.1}";;
  "env --help")
    if [[ -n "${MOCK_VERCEL_ENV_HELP:-}" ]]; then printf '%s\n' "${MOCK_VERCEL_ENV_HELP}"
    else printf '  add     name [environment]\n  list    [environment]\n  pull    [filename]\n  remove  name [environment]\n  run     command\n  update  name [environment]\n'; fi
    exit 0;;
  "env run --help")
    printf '%s\n' "${MOCK_VERCEL_ENVRUN_HELP:--e,  --environment <TARGET>}"; exit 0;;
  "env update --help")
    printf '%s\n' "${MOCK_VERCEL_ENVUPDATE_HELP:--y,  --yes}"; exit 0;;
  whoami*) printf '%s\n' "${MOCK_VERCEL_USER:-milo-team}";;
  "project inspect"*)
    [[ "${MOCK_VERCEL_INSPECT:-ok}" == "fail" ]] && { printf 'Error: not authorized\n' >&2; exit 1; }
    printf 'Project Name %s\n  ID %s\n  Owner %s\n' "${MOCK_VERCEL_INSPECT_NAME:-milo-agent-workspace}" "${MOCK_VERCEL_INSPECT_ID:-prj_linked0000000001}" "${MOCK_VERCEL_INSPECT_ORG:-team_linked000}"
    exit 0;;
  "env ls production"*)
    [[ "${MOCK_VERCEL_ENV_FAIL:-}" == "1" ]] && { printf 'Error: not authorized\n' >&2; exit 1; }
    for n in ${MOCK_VERCEL_ENV_NAMES:-}; do printf '%s Encrypted Production 1d\n' "$n"; done;;
  "env add"*) cat > /dev/null; exit 0;;
  "env update"*) cat > /dev/null; exit 0;;
  "env run"*)
    # Isolation canary: record whether the variable under verification is
    # visible in THIS subprocess environment. The real CLI overlays its own
    # process environment over the downloaded production records, so the
    # invoker must have scrubbed the name; "set" here means a caller-side
    # override could have forged the verification result.
    if [[ -n "${MILO_VERIFY_NAME:-}" ]]; then
      if [[ -n "$(eval "printf '%s' \"\${${MILO_VERIFY_NAME}+x}\"")" ]]; then
        printf 'envrun-sees %s=set\n' "$MILO_VERIFY_NAME" >> "$MOCK_LOG"
      else
        printf 'envrun-sees %s=unset\n' "$MILO_VERIFY_NAME" >> "$MOCK_LOG"
      fi
    fi
    case "$all" in
      *sha256sum*) printf '%s\n' "${MOCK_VERCEL_FP:-nofp}";;
      *) printf '%s\n' "${MOCK_VERCEL_ENVRUN:-MATCH}";;
    esac;;
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
case "$url" in
  *"/v2/redis/databases")
    [[ "${MOCK_UPSTASH_LIST_FAIL:-}" == "1" ]] && { emit '{"error":"x"}' 401; exit 0; }
    emit "${MOCK_UPSTASH_DBS:-[]}" 200;;
  *"/v2/redis/database/"*) emit "${MOCK_UPSTASH_DETAIL:?MOCK_UPSTASH_DETAIL unset}" 200;;
  *"/v2/redis/database")
    if [[ "$method" == "POST" ]]; then
      [[ "${MOCK_UPSTASH_CREATE_FAIL:-}" == "1" ]] && { emit '{"error":"x"}' 400; exit 0; }
      emit "${MOCK_UPSTASH_CREATE_RESP:-{\"database_id\":\"db_prod_1\"}}" 200
    else emit '{}' 405; fi;;
  *) printf 'MOCK-UNAUTHORIZED curl url: %s\n' "$url" >&2; exit 98;;
esac
"""

MOCKS = {"gcloud": GCLOUD_MOCK, "vercel": VERCEL_MOCK, "curl": CURL_MOCK}

VERCEL_REUSE = "CLOUD_RUN_API_URL GCP_PROJECT_NUMBER GCP_WORKLOAD_IDENTITY_POOL_ID GCP_WORKLOAD_IDENTITY_POOL_PROVIDER_ID GCP_SERVICE_ACCOUNT_EMAIL NEXT_PUBLIC_SUPABASE_URL NEXT_PUBLIC_SUPABASE_ANON_KEY"
# Documented Upstash database object shape: database_id/database_name/region
# (+ primary_region for global)/state/tls/endpoint/rest_token. `platform` is a
# create-REQUEST parameter and never appears in a response.
DEFAULT_DETAIL = json.dumps({"database_id": "db_prod_1", "database_name": "milo-production", "endpoint": "prod-1.upstash.io",
                             "state": "active", "tls": True, "region": "global", "primary_region": "us-central1",
                             "rest_token": UPSTASH_REST_TOKEN_SECRET})
DEFAULT_DBS = json.dumps([{"database_name": "milo-production", "database_id": "db_prod_1", "endpoint": "prod-1.upstash.io"}])


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
        # Never leak the outer VERCEL_* identity into the subprocess.
        for k in ("VERCEL_PROJECT_ID", "VERCEL_ORG_ID", "VERCEL_TOKEN"):
            run_env.pop(k, None)
        if env:
            run_env.update(env)
        return subprocess.run(["bash", str(RELEASE / "bootstrap-production.sh"), *args],
                              capture_output=True, text=True, env=run_env, cwd=cwd or REPO, timeout=180)

    run.log = log  # type: ignore[attr-defined]
    run.bin_dir = bin_dir  # type: ignore[attr-defined]
    return run


def read_log(run):
    return run.log.read_text()


def mutation_log(run):
    """Log lines excluding read-only --help/--version capability probes."""
    return "\n".join(ln for ln in read_log(run).splitlines()
                     if "--help" not in ln and "--version" not in ln)


def assert_no_mock_errors(result):
    combined = result.stdout + result.stderr
    for marker in ("MOCK-UNAUTHORIZED", "MOCK-FORBIDDEN"):
        assert marker not in combined, f"strict mock rejected a command:\n{combined}"


def _git_repo(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for c in (["git", "init", "-q"], ["git", "config", "user.email", "t@t"], ["git", "config", "user.name", "t"]):
        subprocess.run(c, cwd=repo, check=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "i"], cwd=repo, check=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()
    return repo, head


def _out(tmp_path):
    return tmp_path / "operator-out"


def _write_fixtures(tmp_path, service, job):
    svc = tmp_path / "live-api.json"
    jb = tmp_path / "live-job.json"
    svc.write_text(json.dumps(service))
    jb.write_text(json.dumps(job))
    return svc, jb


def _fixture_spec(desc):
    spec = desc["spec"]["template"]["spec"]
    return spec if "containers" in spec else spec["template"]["spec"]


def _fixture_plain_get(desc, name):
    for entry in _fixture_spec(desc)["containers"][0].get("env", []):
        if entry.get("name") == name and "value" in entry:
            return entry["value"]
    return None


def _fixture_set_plain(desc, name, value):
    env = _fixture_spec(desc)["containers"][0].setdefault("env", [])
    for entry in env:
        if entry.get("name") == name and "value" in entry:
            entry["value"] = value
            return
    env.append({"name": name, "value": value})


def _apply_args(out, sha, wif=False):
    a = [
        "--apply", "--environment", "production", "--confirm-production-change",
        "--expected-project", PROJECT, "--expected-account", OPERATOR,
        "--release-sha", sha, "--rollback-sha", FULL_SHA, "--region", REGION,
        "--repository", "milo-agent", "--api-service", "milo-agent-api", "--worker-job", "milo-agent-worker",
        "--api-sa", API_SA, "--worker-sa", WORKER_SA, "--gateway-sa", GATEWAY_SA,
        "--vercel-project", "milo-agent-workspace", "--vercel-project-id", VPID, "--vercel-org-id", VORG,
        "--supabase-project-ref", "vvhtaqgkgkalpfcbuvag", "--production-origin", ORIGIN,
        "--vercel-token-env", "VERCEL_TOK", "--upstash-email-env", "UP_EMAIL", "--upstash-apikey-env", "UP_APIKEY",
        "--supabase-url-env", "SUPA_URL", "--supabase-key-env", "SUPA_KEY", "--provider-key-env", "PROV_KEY",
        "--output-directory", str(out),
    ]
    if wif:
        a += ["--wif-pool", WIF_POOL, "--wif-provider", WIF_PROVIDER, "--wif-issuer", WIF_ISSUER,
              "--wif-audience", WIF_AUDIENCE, "--wif-attribute-condition", WIF_COND,
              "--wif-attribute-mapping", WIF_MAPPING, "--wif-principal-set", WIF_PRINCIPAL]
    return a


def _env(tmp_path, service=None, job=None, release_sha=None, **over):
    service = service or api_service_json()
    job = job or worker_job_json()
    # Bind the live fixtures to the actual audited bootstrap SHA — but never
    # clobber a fixture a test deliberately customized (drift tests).
    if release_sha:
        for desc in (service, job):
            if _fixture_plain_get(desc, "MILO_BOOTSTRAP_SHA") == FULL_SHA:
                _fixture_set_plain(desc, "MILO_BOOTSTRAP_SHA", release_sha)
    svc, jb = _write_fixtures(tmp_path, service, job)
    e = {
        "MILO_OPERATOR_ACK": ACK, "MOCK_GCLOUD_ACCOUNT": OPERATOR, "MOCK_GCLOUD_PROJECT": PROJECT,
        "MOCK_SERVICE_JSON": str(svc), "MOCK_JOB_JSON": str(jb),
        "MOCK_UPSTASH_DBS": DEFAULT_DBS, "MOCK_UPSTASH_DETAIL": DEFAULT_DETAIL,
        "MOCK_UPSTASH_REST_TOKEN": UPSTASH_REST_TOKEN_SECRET,
        "MOCK_EXISTING_SECRETS": ALL_SECRET_NAMES, "MOCK_SECRETS_WITH_VERSIONS": ALL_SECRET_NAMES,
        "MOCK_EXISTING_SAS": f"{API_SA},{WORKER_SA},{GATEWAY_SA}",
        "MOCK_SECRETS_LIST": f"{S_SUPA_URL}\n{S_SUPA_KEY}\n{S_PROVIDER}\n{S_REDIS}\n",
        "MOCK_REDIS_ENABLED_VERSION": REDIS_VER, "MOCK_REDIS_SM_PAYLOAD": UPSTASH_REST_TOKEN_SECRET,
        "MOCK_SECRET_ACCESSOR_MEMBERS": f"serviceAccount:{API_SA} serviceAccount:{WORKER_SA}",
        "MOCK_VERCEL_ENV_NAMES": VERCEL_REUSE, "MOCK_VERCEL_INSPECT_ID": VPID, "MOCK_VERCEL_INSPECT_ORG": VORG,
        "MOCK_VERCEL_ENVRUN": "MATCH", "MOCK_VERCEL_FP": REDIS_FP,
        "MOCK_WIF_PROVIDER_JSON": PROVIDER_JSON_OK, "MOCK_WIF_GATEWAY_POLICY": GATEWAY_POLICY_OK, "MOCK_RUN_INVOKER_POLICY": RUN_POLICY_OK,
        "SUPA_URL": "https://vvhtaqgkgkalpfcbuvag.supabase.co", "SUPA_KEY": SUPABASE_SECRET, "PROV_KEY": PROVIDER_SECRET,
        "UP_EMAIL": "ops@milo.test", "UP_APIKEY": UPSTASH_APIKEY_SECRET, "VERCEL_TOK": VERCEL_TOKEN_SECRET,
    }
    e.update(over)
    return e


def _apply(strict_bin, tmp_path, *, service=None, job=None, wif=False, **over):
    repo, head = _git_repo(tmp_path)
    out = _out(tmp_path)
    args = _apply_args(out, head, wif=wif)
    return strict_bin(*args, env=_env(tmp_path, service, job, release_sha=head, **over), cwd=repo), out


# ===========================================================================
# Default plan / read-only
# ===========================================================================

def test_default_mode_plan_read_only(strict_bin, tmp_path):
    out = _out(tmp_path)
    result = strict_bin("--output-directory", str(out), "--expected-project", PROJECT,
                        "--api-sa", API_SA, "--worker-sa", WORKER_SA, "--gateway-sa", GATEWAY_SA,
                        env={"MOCK_GCLOUD_PROJECT": PROJECT, "MOCK_EXISTING_SECRETS": ALL_SECRET_NAMES,
                             "MOCK_SECRETS_WITH_VERSIONS": ALL_SECRET_NAMES})
    assert "[PASS] mode — bootstrap mode: plan" in result.stdout
    log = mutation_log(strict_bin)
    for verb in ("iam service-accounts create", "secrets create", "secrets versions add",
                 "run services update", "run jobs update", "env add", "env update"):
        assert verb not in log


# ===========================================================================
# B1 — Vercel identity from a clean checkout (no .vercel), token never in argv
# ===========================================================================

def test_vercel_identity_no_dotvercel_and_token_not_in_argv(strict_bin, tmp_path):
    result, _ = _apply(strict_bin, tmp_path)
    assert_no_mock_errors(result)  # a --token argv would trip MOCK-FORBIDDEN
    assert "[PASS] vercel:project-identity" in result.stdout
    log = read_log(strict_bin)
    assert "project inspect milo-agent-workspace" in log
    assert "--token" not in log  # never passed on the CLI


def test_vercel_missing_identity_inputs_blocks_apply(strict_bin, tmp_path):
    repo, head = _git_repo(tmp_path)
    out = _out(tmp_path)
    args = [a for a in _apply_args(out, head) if a not in ("--vercel-project-id", VPID, "--vercel-org-id", VORG)]
    result = strict_bin(*args, env=_env(tmp_path, release_sha=head), cwd=repo)
    assert result.returncode != 0
    assert "[BLOCKED] vercel:identity-inputs" in result.stdout
    assert "env add" not in mutation_log(strict_bin) and "env update" not in mutation_log(strict_bin)


def test_vercel_wrong_project_id_blocks_before_writes(strict_bin, tmp_path):
    result, _ = _apply(strict_bin, tmp_path, MOCK_VERCEL_INSPECT_ID="prj_OTHER")
    assert "[BLOCKED] vercel:project-identity" in result.stdout
    assert "env add" not in mutation_log(strict_bin) and "env update" not in mutation_log(strict_bin)


def test_vercel_wrong_org_blocks_before_writes(strict_bin, tmp_path):
    result, _ = _apply(strict_bin, tmp_path, MOCK_VERCEL_INSPECT_ORG="team_intruder")
    assert "[BLOCKED] vercel:project-identity" in result.stdout
    assert "env add" not in mutation_log(strict_bin)


def test_vercel_wrong_name_blocks_before_writes(strict_bin, tmp_path):
    result, _ = _apply(strict_bin, tmp_path, MOCK_VERCEL_INSPECT_NAME="some-other-project")
    assert "[BLOCKED] vercel:project-identity" in result.stdout
    assert "env add" not in mutation_log(strict_bin)


def test_vercel_existing_var_uses_env_update_not_remove_add(strict_bin, tmp_path):
    names = VERCEL_REUSE + " GATEWAY_ALLOW_EXECUTION_ROUTES"
    result, _ = _apply(strict_bin, tmp_path, MOCK_VERCEL_ENV_NAMES=names)
    assert_no_mock_errors(result)
    log = read_log(strict_bin)
    assert "env update GATEWAY_ALLOW_EXECUTION_ROUTES production" in log
    assert "env rm" not in log  # never remove-then-add


def test_vercel_absent_var_uses_env_add(strict_bin, tmp_path):
    result, _ = _apply(strict_bin, tmp_path)  # kill switches absent
    log = read_log(strict_bin)
    assert "env add GATEWAY_ALLOW_EXECUTION_ROUTES production" in log


def test_vercel_never_adds_server_secrets(strict_bin, tmp_path):
    _apply(strict_bin, tmp_path)
    log = read_log(strict_bin)
    for f in ("KIMI_API_KEY", "SUPABASE_SECRET_KEY", "SUPABASE_SERVICE_ROLE_KEY", "MOONSHOT_API_KEY"):
        assert f"env add {f}" not in log and f"env update {f}" not in log


# B8 — exact non-secret value + fingerprint verification (in-memory)

def test_vercel_exact_values_verified_in_memory(strict_bin, tmp_path):
    result, _ = _apply(strict_bin, tmp_path)
    assert "[PASS] audit:vercel:value:GATEWAY_ALLOW_EXECUTION_ROUTES" in result.stdout
    assert "[PASS] audit:vercel:fingerprint:UPSTASH_REDIS_REST_TOKEN" in result.stdout
    # env run injected the verifier; the raw value never appears.
    assert UPSTASH_REST_TOKEN_SECRET not in result.stdout


def test_vercel_value_mismatch_blocks(strict_bin, tmp_path):
    result, _ = _apply(strict_bin, tmp_path, MOCK_VERCEL_ENVRUN="MISMATCH")
    assert result.returncode != 0
    assert "[BLOCKED] audit:vercel:value:GATEWAY_ALLOW_EXECUTION_ROUTES" in result.stdout


def test_vercel_fingerprint_mismatch_blocks(strict_bin, tmp_path):
    result, _ = _apply(strict_bin, tmp_path, MOCK_VERCEL_FP="deadbeefdeadbeef")
    assert result.returncode != 0
    assert "[BLOCKED] audit:vercel:fingerprint:UPSTASH_REDIS_REST_TOKEN" in result.stdout


# ===========================================================================
# B2 — Live audit blocks on every incorrect/missing exact value
# ===========================================================================

@pytest.mark.parametrize("mutate,expect", [
    (dict(plain_over={"ENVIRONMENT": "development"}), "live:api:ENVIRONMENT"),
    (dict(drop=["ENVIRONMENT"]), "live:api:ENVIRONMENT"),
    (dict(drop=["MILO_ENABLE_RUN_CREATION"]), "live:api:flag:MILO_ENABLE_RUN_CREATION"),
    (dict(plain_over={"MILO_ENABLE_RUN_CREATION": ""}), "live:api:flag:MILO_ENABLE_RUN_CREATION"),
    (dict(plain_over={"MILO_ENABLE_RUN_CREATION": "true"}), "live:api:flag:MILO_ENABLE_RUN_CREATION"),
    (dict(plain_over={"MILO_ENABLE_RUN_CREATION": "0"}), "live:api:flag:MILO_ENABLE_RUN_CREATION"),
    (dict(drop=["GATEWAY_ALLOW_EXECUTION_ROUTES"]), "live:api:GATEWAY_ALLOW_EXECUTION_ROUTES"),
    (dict(plain_over={"MILO_GATEWAY_AUDIENCE": "https://evil.example"}), "live:api:MILO_GATEWAY_AUDIENCE"),
    (dict(plain_over={"MILO_APPROVED_GATEWAY_IDENTITIES": "intruder@x.iam.gserviceaccount.com"}), "live:api:MILO_APPROVED_GATEWAY_IDENTITIES"),
    (dict(plain_over={"ALLOWED_CORS_ORIGINS": ORIGIN + ",https://extra.example"}), "live:api:ALLOWED_CORS_ORIGINS"),
    (dict(plain_over={"ALLOWED_CORS_ORIGINS": "https://wrong.example"}), "live:api:ALLOWED_CORS_ORIGINS"),
    (dict(secret_over={"SUPABASE_SECRET_KEY": ("WRONG_SECRET", "latest")}), "live:api:secret-ref:SUPABASE_SECRET_KEY"),
    (dict(secret_over={"UPSTASH_REDIS_REST_TOKEN": (S_REDIS, "latest")}), "live:api:secret-ref:UPSTASH_REDIS_REST_TOKEN"),
    (dict(plain_over={"MILO_MAX_COST_PER_RUN": "0"}), "live:api:budget:MILO_MAX_COST_PER_RUN"),
    (dict(plain_over={"MILO_MAX_COST_PER_RUN": "-1"}), "live:api:budget:MILO_MAX_COST_PER_RUN"),
    (dict(plain_over={"MILO_MAX_COST_PER_RUN": "abc"}), "live:api:budget:MILO_MAX_COST_PER_RUN"),
    (dict(drop=["MILO_MAX_COST_PER_RUN"]), "live:api:budget:MILO_MAX_COST_PER_RUN"),
])
def test_live_api_exact_value_blocks(strict_bin, tmp_path, mutate, expect):
    result, _ = _apply(strict_bin, tmp_path, service=api_service_json(**mutate))
    assert result.returncode != 0
    assert f"[BLOCKED] {expect}" in result.stdout


def test_live_worker_wrong_audience_blocks(strict_bin, tmp_path):
    result, _ = _apply(strict_bin, tmp_path, job=worker_job_json(plain_over={"MILO_WORKER_AUDIENCE": "https://bad"}))
    assert result.returncode != 0
    assert "[BLOCKED] live:worker:MILO_WORKER_AUDIENCE" in result.stdout


def test_live_all_correct_passes_live_checks(strict_bin, tmp_path):
    result, _ = _apply(strict_bin, tmp_path)
    assert "[PASS] live:api:ENVIRONMENT" in result.stdout
    assert "[PASS] live:api:secret-ref:UPSTASH_REDIS_REST_TOKEN" in result.stdout  # pinned version
    assert "[PASS] live:worker:secret-ref:KIMI_API_KEY" in result.stdout


# ===========================================================================
# B3 — Upstash create payload + URL normalization
# ===========================================================================

def test_upstash_create_request_uses_official_fields(strict_bin, tmp_path):
    # No matching db -> CREATE with database_name/platform/primary_region.
    result, _ = _apply(strict_bin, tmp_path, MOCK_UPSTASH_DBS="[]",
                       MOCK_UPSTASH_CREATE_RESP=json.dumps({"database_id": "db_prod_1"}))
    assert_no_mock_errors(result)
    log = read_log(strict_bin)
    body_line = [ln for ln in log.splitlines() if "/v2/redis/database" in ln and "POST" in ln]
    assert body_line, "expected a create POST"
    assert '"database_name":"milo-production"' in body_line[0]
    assert '"platform":"gcp"' in body_line[0]
    assert '"primary_region":"us-central1"' in body_line[0]


def test_upstash_endpoint_slug_normalized(strict_bin, tmp_path):
    detail = json.dumps({"database_id": "db_prod_1", "database_name": "milo-production", "endpoint": "informed-mongoose-123",
                         "state": "active", "tls": True, "region": "global", "primary_region": "us-central1", "rest_token": UPSTASH_REST_TOKEN_SECRET})
    result, _ = _apply(strict_bin, tmp_path, MOCK_UPSTASH_DETAIL=detail,
                       service=api_service_json(plain_over={"UPSTASH_REDIS_REST_URL": "https://informed-mongoose-123.upstash.io"}),
                       job=worker_job_json(plain_over={"UPSTASH_REDIS_REST_URL": "https://informed-mongoose-123.upstash.io"}))
    assert "canonical Redis REST URL: https://informed-mongoose-123.upstash.io" in result.stdout


def test_upstash_malformed_endpoint_blocks(strict_bin, tmp_path):
    detail = json.dumps({"database_id": "db_prod_1", "database_name": "milo-production", "endpoint": "bad host!!",
                         "state": "active", "tls": True, "region": "us-central1", "rest_token": UPSTASH_REST_TOKEN_SECRET})
    result, _ = _apply(strict_bin, tmp_path, MOCK_UPSTASH_DETAIL=detail)
    assert "[BLOCKED] upstash:validate" in result.stdout


def test_upstash_suspended_db_blocks(strict_bin, tmp_path):
    detail = json.dumps({"database_id": "db_prod_1", "database_name": "milo-production", "endpoint": "h.upstash.io",
                         "state": "suspended", "tls": True, "region": "us-central1", "rest_token": UPSTASH_REST_TOKEN_SECRET})
    result, _ = _apply(strict_bin, tmp_path, MOCK_UPSTASH_DETAIL=detail)
    assert "[BLOCKED] upstash:validate" in result.stdout


# ===========================================================================
# B4 — exact Upstash selection
# ===========================================================================

def test_upstash_two_exact_matches_blocks(strict_bin, tmp_path):
    dbs = json.dumps([{"database_name": "milo-production", "database_id": "a"}, {"database_name": "milo-production", "database_id": "b"}])
    result, _ = _apply(strict_bin, tmp_path, MOCK_UPSTASH_DBS=dbs)
    assert "[BLOCKED] upstash:select" in result.stdout
    assert "keys create" not in read_log(strict_bin)


def test_upstash_explicit_id_source_of_truth(strict_bin, tmp_path):
    dbs = json.dumps([{"database_name": "unrelated", "database_id": "db_prod_1"}])
    repo, head = _git_repo(tmp_path)
    out = _out(tmp_path)
    args = _apply_args(out, head) + ["--upstash-database-id", "db_prod_1"]
    result = strict_bin(*args, env=_env(tmp_path, release_sha=head, MOCK_UPSTASH_DBS=dbs), cwd=repo)
    assert "selected exactly one production Redis" in result.stdout or "[PASS] upstash:token" in result.stdout




def test_upstash_detail_matching_selected_id_passes(strict_bin, tmp_path):
    result, _ = _apply(strict_bin, tmp_path)
    assert "[PASS] upstash:url" in result.stdout
    assert "[BLOCKED] upstash:validate" not in result.stdout


@pytest.mark.parametrize("detail", [
    json.dumps({"database_id": "db_other", "database_name": "milo-production", "endpoint": "prod-1.upstash.io", "state": "active", "tls": True, "region": "global", "primary_region": "us-central1", "rest_token": UPSTASH_REST_TOKEN_SECRET}),
    json.dumps({"database_name": "milo-production", "endpoint": "prod-1.upstash.io", "state": "active", "tls": True, "region": "global", "primary_region": "us-central1", "rest_token": UPSTASH_REST_TOKEN_SECRET}),
])
def test_upstash_detail_id_mismatch_or_missing_blocks_before_mutation(strict_bin, tmp_path, detail):
    result, out = _apply(strict_bin, tmp_path, MOCK_UPSTASH_DETAIL=detail)
    assert result.returncode != 0
    assert "[BLOCKED] upstash:validate" in result.stdout
    log = read_log(strict_bin)
    for forbidden in ("secrets versions add", "run services update", "run jobs update", "env add", "env update"):
        assert forbidden not in log
    assert not (out / "milo-production.metadata.env").exists()


def test_upstash_explicit_requested_id_differing_from_detail_id_blocks(strict_bin, tmp_path):
    detail = json.dumps({"database_id": "db_other", "database_name": "milo-production", "endpoint": "prod-1.upstash.io", "state": "active", "tls": True, "region": "global", "primary_region": "us-central1", "rest_token": UPSTASH_REST_TOKEN_SECRET})
    repo, head = _git_repo(tmp_path)
    out = _out(tmp_path)
    args = _apply_args(out, head) + ["--upstash-database-id", "db_prod_1"]
    result = strict_bin(*args, env=_env(tmp_path, release_sha=head, MOCK_UPSTASH_DETAIL=detail), cwd=repo)
    assert result.returncode != 0
    assert "[BLOCKED] upstash:validate" in result.stdout
    log = read_log(strict_bin)
    assert "secrets versions add" not in log
    assert "run services update" not in log
    assert "env update" not in log


def test_upstash_nonexistent_id_blocks(strict_bin, tmp_path):
    repo, head = _git_repo(tmp_path)
    out = _out(tmp_path)
    args = _apply_args(out, head) + ["--upstash-database-id", "nope"]
    result = strict_bin(*args, env=_env(tmp_path, release_sha=head, MOCK_UPSTASH_DBS="[]"), cwd=repo)
    assert "[BLOCKED] upstash:select" in result.stdout


def test_upstash_only_creates_when_no_exact_match(strict_bin, tmp_path):
    # An exact match exists -> must NOT create.
    result, _ = _apply(strict_bin, tmp_path)
    log = read_log(strict_bin)
    post = [ln for ln in log.splitlines() if "/v2/redis/database" in ln and "POST" in ln]
    assert not post, "must not create when an exact match exists"


# ===========================================================================
# B5 — Redis credential transaction
# ===========================================================================

def test_redis_pins_exact_version_and_no_rotation_on_match(strict_bin, tmp_path):
    # SM already holds the selected token (payload == upstash token) at version 3.
    result, _ = _apply(strict_bin, tmp_path, MOCK_REDIS_SM_PAYLOAD=UPSTASH_REST_TOKEN_SECRET, MOCK_REDIS_ENABLED_VERSION="3",
                       service=api_service_json(secret_over={"UPSTASH_REDIS_REST_TOKEN": (S_REDIS, "3")}, plain_over={"MILO_REDIS_SECRET_VERSION": "3"}),
                       job=worker_job_json(secret_over={"UPSTASH_REDIS_REST_TOKEN": (S_REDIS, "3")}, plain_over={"MILO_REDIS_SECRET_VERSION": "3"}))
    assert "no rotation" in result.stdout.lower() or "NO rotation" in result.stdout
    log = read_log(strict_bin)
    assert "secrets versions add" not in log  # fingerprint matched -> no rotation
    upd = [ln for ln in log.splitlines() if "run services update" in ln][0]
    assert f"UPSTASH_REDIS_REST_TOKEN={S_REDIS}:3" in upd  # pinned exact version, not latest


def test_redis_rotates_when_token_differs_and_pins_new_version(strict_bin, tmp_path):
    # SM payload differs from selected token -> add a new version (4) and pin it.
    result, _ = _apply(strict_bin, tmp_path, MOCK_REDIS_SM_PAYLOAD="an-old-different-token", MOCK_REDIS_ADD_VERSION="4",
                       service=api_service_json(secret_over={"UPSTASH_REDIS_REST_TOKEN": (S_REDIS, "4")}),
                       job=worker_job_json(secret_over={"UPSTASH_REDIS_REST_TOKEN": (S_REDIS, "4")}))
    log = read_log(strict_bin)
    assert "secrets versions add" in log
    upd = [ln for ln in log.splitlines() if "run services update" in ln][0]
    assert f"UPSTASH_REDIS_REST_TOKEN={S_REDIS}:4" in upd


def test_redis_ledger_present_in_report(strict_bin, tmp_path):
    _result, out = _apply(strict_bin, tmp_path)
    report = json.loads((out / "bootstrap-apply.json").read_text())
    assert "redis_reconciliation_ledger" in report
    assert any("selected" in x for x in report["redis_reconciliation_ledger"])


def test_redis_secret_version_error_not_no_version(strict_bin, tmp_path):
    result, _ = _apply(strict_bin, tmp_path, MOCK_SECRET_VERSION_ERROR=S_REDIS)
    assert "[BLOCKED] redis:reconcile" in result.stdout


@pytest.mark.parametrize("mode", ["denied", "api", "empty"])
def test_redis_payload_read_failure_blocks_before_any_rotation_or_downstream_mutation(strict_bin, tmp_path, mode):
    result, out = _apply(strict_bin, tmp_path, MOCK_REDIS_ACCESS_MODE=mode)
    assert result.returncode != 0
    assert "[BLOCKED] redis:reconcile" in result.stdout
    log = read_log(strict_bin)
    for forbidden in ("secrets versions add", "run services update", "run jobs update"):
        assert forbidden not in log, f"payload-read failure must not allow {forbidden}"
    mutation_lines = [ln for ln in log.splitlines() if "vercel env add" in ln or "vercel env update" in ln]
    for forbidden_name in ("UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN", "MILO_REDIS_TOKEN_FINGERPRINT"):
        assert not any(forbidden_name in ln for ln in mutation_lines), f"payload-read failure must not mutate Vercel {forbidden_name}"
    assert not (out / "milo-production.metadata.env").exists()
    assert "metadata:generated" not in result.stdout
    assert "APPLY COMPLETE" not in result.stdout


# ===========================================================================
# B6 — WIF exact verification
# ===========================================================================

def test_wif_exact_valid_passes(strict_bin, tmp_path):
    result, _ = _apply(strict_bin, tmp_path, wif=True)
    assert_no_mock_errors(result)
    for c in ("wif:issuer", "wif:audience", "wif:attribute-mapping", "wif:attribute-condition", "wif:gateway-binding", "wif:run-invoker"):
        assert f"[PASS] {c}" in result.stdout, c


def test_wif_partial_config_blocks(strict_bin, tmp_path):
    repo, head = _git_repo(tmp_path)
    out = _out(tmp_path)
    args = _apply_args(out, head) + ["--wif-pool", WIF_POOL, "--wif-provider", WIF_PROVIDER]  # only 2 of 6
    result = strict_bin(*args, env=_env(tmp_path, release_sha=head), cwd=repo)
    assert "[BLOCKED] wif:partial" in result.stdout


@pytest.mark.parametrize("provider,gateway,run,expect", [
    (json.dumps({"oidc": {"issuerUri": "https://evil", "allowedAudiences": [WIF_AUDIENCE]}, "attributeMapping": {"google.subject": "x"}, "attributeCondition": WIF_COND}), GATEWAY_POLICY_OK, RUN_POLICY_OK, "wif:issuer"),
    (json.dumps({"oidc": {"issuerUri": WIF_ISSUER, "allowedAudiences": [WIF_AUDIENCE, "https://extra"]}, "attributeMapping": {"google.subject": "x"}, "attributeCondition": WIF_COND}), GATEWAY_POLICY_OK, RUN_POLICY_OK, "wif:audience"),
    (json.dumps({"oidc": {"issuerUri": WIF_ISSUER, "allowedAudiences": [WIF_AUDIENCE]}, "attributeMapping": {}, "attributeCondition": WIF_COND}), GATEWAY_POLICY_OK, RUN_POLICY_OK, "wif:attribute-mapping"),
    (json.dumps({"oidc": {"issuerUri": WIF_ISSUER, "allowedAudiences": [WIF_AUDIENCE]}, "attributeMapping": {"google.subject": "x"}, "attributeCondition": ""}), GATEWAY_POLICY_OK, RUN_POLICY_OK, "wif:attribute-condition"),
    (PROVIDER_JSON_OK, json.dumps({"bindings": [{"role": "roles/iam.workloadIdentityUser", "members": ["principalSet://iam.googleapis.com/BROADER"]}]}), RUN_POLICY_OK, "wif:gateway-binding"),
    (PROVIDER_JSON_OK, json.dumps({"bindings": [{"role": "roles/iam.workloadIdentityUser", "members": ["allUsers"]}]}), RUN_POLICY_OK, "wif:gateway-binding"),
    (PROVIDER_JSON_OK, GATEWAY_POLICY_OK, json.dumps({"bindings": [{"role": "roles/run.invoker", "members": ["allUsers"]}]}), "wif:run-invoker"),
    (PROVIDER_JSON_OK, GATEWAY_POLICY_OK, json.dumps({"bindings": [{"role": "roles/run.invoker", "members": [f"serviceAccount:{GATEWAY_SA}", "serviceAccount:other@x.iam.gserviceaccount.com"]}]}), "wif:run-invoker"),
])
def test_wif_exact_mismatches_block(strict_bin, tmp_path, provider, gateway, run, expect):
    result, _ = _apply(strict_bin, tmp_path, wif=True,
                       MOCK_WIF_PROVIDER_JSON=provider, MOCK_WIF_GATEWAY_POLICY=gateway, MOCK_RUN_INVOKER_POLICY=run)
    assert result.returncode != 0
    assert f"[BLOCKED] {expect}" in result.stdout


# ===========================================================================
# Guard, keys, worker, provider, secret hygiene, idempotency, aggregation
# ===========================================================================

def test_guard_wrong_project_blocks(strict_bin, tmp_path):
    result, out = _apply(strict_bin, tmp_path, MOCK_GCLOUD_PROJECT="other")
    assert result.returncode != 0
    assert "does not match --expected-project" in result.stdout
    assert json.loads((out / "bootstrap-apply.json").read_text())["status"] == "guard-blocked"


def test_guard_dirty_worktree_blocks(strict_bin, tmp_path):
    repo, head = _git_repo(tmp_path)
    (repo / "d.txt").write_text("x")
    out = _out(tmp_path)
    result = strict_bin(*_apply_args(out, head), env=_env(tmp_path, release_sha=head), cwd=repo)
    assert result.returncode != 0
    assert "dirty Git worktree" in result.stdout


def test_shared_identity_blocked_before_mutation(strict_bin, tmp_path):
    repo, head = _git_repo(tmp_path)
    out = _out(tmp_path)
    args = _apply_args(out, head)
    args[args.index("--worker-sa") + 1] = API_SA
    result = strict_bin(*args, env=_env(tmp_path, release_sha=head), cwd=repo)
    assert result.returncode != 0
    assert "[BLOCKED] identity:api-worker" in result.stdout
    assert "iam service-accounts create" not in read_log(strict_bin)


def test_no_keys_no_worker_exec(strict_bin, tmp_path):
    _apply(strict_bin, tmp_path)
    log = read_log(strict_bin)
    assert "keys create" not in log and "jobs run" not in log and "jobs execute" not in log
    assert "run jobs update milo-agent-worker" in log


def test_provider_never_called(strict_bin, tmp_path):
    _apply(strict_bin, tmp_path)
    for ln in read_log(strict_bin).splitlines():
        if ln.startswith("curl "):
            assert "/v2/redis/" in ln
        assert "moonshot" not in ln.lower() and "api.kimi" not in ln.lower()


def _all_text(result, out):
    t = result.stdout + result.stderr
    for p in out.rglob("*"):
        if p.is_file():
            try:
                t += "\n" + p.read_text()
            except UnicodeDecodeError:
                pass
    return t


def test_secrets_never_leak(strict_bin, tmp_path):
    result, out = _apply(strict_bin, tmp_path)
    hay = _all_text(result, out)
    for s in ALL_SECRETS:
        assert s not in hay, f"leak {s[:8]}"
    # The fingerprint must be present (non-secret) but the token must not.
    assert UPSTASH_REST_TOKEN_SECRET not in read_log(strict_bin)


def test_adopt_existing_secrets_no_add(strict_bin, tmp_path):
    result, _ = _apply(strict_bin, tmp_path)
    assert_no_mock_errors(result)
    log = read_log(strict_bin)
    # supabase/provider adopted (enabled versions) -> no create/add for them.
    assert "secrets create" not in log
    for lbl in ("supabase-url", "supabase-key", "provider-key"):
        assert f"[PASS] gcp:secret:{lbl}:reuse" in result.stdout


def test_partial_failure_recovery_no_false_success(strict_bin, tmp_path):
    result, out = _apply(strict_bin, tmp_path, MOCK_UPSTASH_LIST_FAIL="1")
    assert result.returncode != 0
    assert "APPLY COMPLETE" not in result.stdout
    report = json.loads((out / "bootstrap-apply.json").read_text())
    assert report["status"] == "partial-failure"
    assert report["recovery_steps"]


def test_final_aggregation_consistent(strict_bin, tmp_path):
    _result, out = _apply(strict_bin, tmp_path)
    agg = json.loads((out / "readiness.json").read_text())
    assert agg["summary"]["blocked"] == len(agg["blocking_findings"])


def test_audit_only_read_only(strict_bin, tmp_path):
    out = _out(tmp_path)
    svc, jb = _write_fixtures(tmp_path, api_service_json(secret_over={"UPSTASH_REDIS_REST_TOKEN": (S_REDIS, "3")}, plain_over={"MILO_REDIS_SECRET_VERSION": "3"}),
                              worker_job_json(secret_over={"UPSTASH_REDIS_REST_TOKEN": (S_REDIS, "3")}, plain_over={"MILO_REDIS_SECRET_VERSION": "3"}))
    result = strict_bin("--audit-only", "--output-directory", str(out), "--expected-project", PROJECT,
                        "--expected-account", OPERATOR, "--release-sha", FULL_SHA, "--rollback-sha", FULL_SHA,
                        env={"MOCK_GCLOUD_PROJECT": PROJECT, "MOCK_GCLOUD_ACCOUNT": OPERATOR,
                             "MOCK_SERVICE_JSON": str(svc), "MOCK_JOB_JSON": str(jb),
                             "MOCK_EXISTING_SECRETS": ALL_SECRET_NAMES, "MOCK_SECRETS_WITH_VERSIONS": ALL_SECRET_NAMES,
                             "MOCK_REDIS_ENABLED_VERSION": "3", "MOCK_REDIS_SM_PAYLOAD": UPSTASH_REST_TOKEN_SECRET})
    log = mutation_log(strict_bin)
    for verb in ("iam service-accounts create", "secrets create", "secrets versions add",
                 "run services update", "run jobs update", "env add", "env update", "curl "):
        assert verb not in log, f"audit-only mutation/mgmt call: {verb}"
    # Without stored metadata AND without management credentials the audit
    # contract is explicit and fail-closed (never a silent degrade).
    assert "[BLOCKED] audit:contract" in result.stdout
    assert result.returncode != 0
    assert (out / "readiness.json").exists()
    assert "run services describe milo-agent-api" in log


def test_generated_manifest_apply_valid(strict_bin, tmp_path):
    _result, out = _apply(strict_bin, tmp_path)
    val = subprocess.run(["python3", str(RELEASE / "validate_production_manifest.py"),
                          "--manifest", str(out / "milo-production.yaml"), "--mode", "apply"],
                         capture_output=True, text=True)
    assert val.returncode == 0, val.stdout + val.stderr


# ===========================================================================
# C1 — Vercel token never in argv (check-vercel-config.sh)
# ===========================================================================

def test_check_vercel_config_never_uses_token_argv(strict_bin, tmp_path):
    # Direct invocation with the token-rejecting vercel mock; a --token argv
    # would trip MOCK-FORBIDDEN. The token travels only via the environment.
    report = tmp_path / "cvc.json"
    run_env = dict(os.environ)
    run_env["PATH"] = f"{strict_bin.bin_dir}:{run_env['PATH']}"
    run_env["MOCK_LOG"] = str(strict_bin.log)
    run_env.update({"MOCK_VERCEL_ENV_NAMES": VERCEL_REUSE, "MOCK_VERCEL_INSPECT_ID": VPID,
                    "MOCK_VERCEL_INSPECT_ORG": VORG, "VTOK": VERCEL_TOKEN_SECRET})
    result = subprocess.run(
        ["bash", str(RELEASE / "check-vercel-config.sh"), "--project", "milo-agent-workspace",
         "--project-id", VPID, "--org-id", VORG, "--token-env", "VTOK", "--json-output", str(report)],
        capture_output=True, text=True, env=run_env, cwd=REPO, timeout=60)
    assert "MOCK-FORBIDDEN" not in (result.stdout + result.stderr)
    log = read_log(strict_bin)
    for ln in log.splitlines():
        if ln.startswith("vercel "):
            assert "--token" not in ln, f"token leaked into argv: {ln}"
    assert VERCEL_TOKEN_SECRET not in read_log(strict_bin)
    assert "[PASS] vercel:project-identity" in result.stdout


# ===========================================================================
# C2 — audit-only is a complete fail-closed audit
# ===========================================================================

def _audit_only(strict_bin, tmp_path, *, wif=False, **over):
    out = _out(tmp_path)
    svc, jb = _write_fixtures(tmp_path, api_service_json(secret_over={"UPSTASH_REDIS_REST_TOKEN": (S_REDIS, "3")}, plain_over={"MILO_REDIS_SECRET_VERSION": "3"}),
                              worker_job_json(secret_over={"UPSTASH_REDIS_REST_TOKEN": (S_REDIS, "3")}, plain_over={"MILO_REDIS_SECRET_VERSION": "3"}))
    args = ["--audit-only", "--output-directory", str(out), "--expected-project", PROJECT,
            "--expected-account", OPERATOR, "--release-sha", FULL_SHA, "--rollback-sha", FULL_SHA,
            "--region", REGION, "--repository", "milo-agent",
            "--api-service", "milo-agent-api", "--worker-job", "milo-agent-worker",
            "--api-sa", API_SA, "--worker-sa", WORKER_SA, "--gateway-sa", GATEWAY_SA,
            "--supabase-project-ref", "vvhtaqgkgkalpfcbuvag", "--production-origin", ORIGIN,
            "--vercel-project", "milo-agent-workspace", "--vercel-project-id", VPID, "--vercel-org-id", VORG,
            "--vercel-token-env", "VERCEL_TOK", "--upstash-email-env", "UP_EMAIL", "--upstash-apikey-env", "UP_APIKEY"]
    if wif:
        args += ["--wif-pool", WIF_POOL, "--wif-provider", WIF_PROVIDER, "--wif-issuer", WIF_ISSUER,
                 "--wif-audience", WIF_AUDIENCE, "--wif-attribute-condition", WIF_COND,
                 "--wif-attribute-mapping", WIF_MAPPING, "--wif-principal-set", WIF_PRINCIPAL]
    env = {"MOCK_GCLOUD_PROJECT": PROJECT, "MOCK_GCLOUD_ACCOUNT": OPERATOR,
           "MOCK_SERVICE_JSON": str(svc), "MOCK_JOB_JSON": str(jb),
           "MOCK_EXISTING_SECRETS": ALL_SECRET_NAMES, "MOCK_SECRETS_WITH_VERSIONS": ALL_SECRET_NAMES,
           "MOCK_REDIS_ENABLED_VERSION": "3", "MOCK_REDIS_SM_PAYLOAD": UPSTASH_REST_TOKEN_SECRET,
           "MOCK_UPSTASH_DBS": DEFAULT_DBS, "MOCK_UPSTASH_DETAIL": DEFAULT_DETAIL, "MOCK_UPSTASH_REST_TOKEN": UPSTASH_REST_TOKEN_SECRET,
           "MOCK_VERCEL_ENV_NAMES": VERCEL_REUSE, "MOCK_VERCEL_INSPECT_ID": VPID, "MOCK_VERCEL_INSPECT_ORG": VORG,
           "MOCK_VERCEL_ENVRUN": "MATCH", "MOCK_VERCEL_FP": REDIS_FP,
           "MOCK_WIF_PROVIDER_JSON": PROVIDER_JSON_OK, "MOCK_WIF_GATEWAY_POLICY": GATEWAY_POLICY_OK, "MOCK_RUN_INVOKER_POLICY": RUN_POLICY_OK,
           "UP_EMAIL": "ops@milo.test", "UP_APIKEY": UPSTASH_APIKEY_SECRET, "VERCEL_TOK": VERCEL_TOKEN_SECRET}
    env.update(over)
    return strict_bin(*args, env=env), out


def test_audit_only_blocks_without_wif_evidence(strict_bin, tmp_path):
    result, _ = _audit_only(strict_bin, tmp_path, wif=False)
    assert result.returncode != 0
    assert "[BLOCKED] wif" in result.stdout
    # no mutation performed in audit-only
    log = mutation_log(strict_bin)
    for verb in ("run services update", "secrets versions add", "env add", "env update"):
        assert verb not in log


def test_audit_only_proves_wif_and_redis_with_full_evidence(strict_bin, tmp_path):
    result, _ = _audit_only(strict_bin, tmp_path, wif=True)
    assert "[PASS] wif:issuer" in result.stdout
    assert "[PASS] wif:attribute-mapping" in result.stdout
    assert "[PASS] wif:run-invoker" in result.stdout
    assert "[PASS] redis:reconcile" in result.stdout
    assert "[PASS] audit:vercel:fingerprint:UPSTASH_REDIS_REST_TOKEN" in result.stdout
    log = mutation_log(strict_bin)
    for verb in ("run services update", "secrets versions add", "env add", "env update", "secrets create"):
        assert verb not in log, f"audit-only mutated: {verb}"


def test_audit_only_redis_mismatch_blocks(strict_bin, tmp_path):
    # Upstash token differs from the active Secret Manager version -> BLOCKED,
    # and audit-only never rotates.
    result, _ = _audit_only(strict_bin, tmp_path, wif=True, MOCK_REDIS_SM_PAYLOAD="a-stale-token")
    assert result.returncode != 0
    assert "[BLOCKED] redis:reconcile" in result.stdout
    assert "secrets versions add" not in read_log(strict_bin)


# ===========================================================================
# C3 — Redis-dependent mutations are gated
# ===========================================================================

def test_redis_reconcile_failure_blocks_cloud_run_and_vercel(strict_bin, tmp_path):
    result, _ = _apply(strict_bin, tmp_path, MOCK_SECRET_VERSION_ERROR=S_REDIS)
    assert result.returncode != 0
    assert "[BLOCKED] redis:reconcile" in result.stdout
    assert "[BLOCKED] gcp:redis-gate" in result.stdout
    log = read_log(strict_bin)
    # No Cloud Run config and no Redis Vercel var updates after the failure.
    assert "run services update" not in log and "run jobs update" not in log
    assert "env add UPSTASH_REDIS_REST_TOKEN" not in log and "env update UPSTASH_REDIS_REST_TOKEN" not in log


def test_cloud_run_pins_no_latest_fallback(strict_bin, tmp_path):
    result, _ = _apply(strict_bin, tmp_path)
    log = read_log(strict_bin)
    for ln in log.splitlines():
        if "run services update" in ln or "run jobs update" in ln:
            assert f"UPSTASH_REDIS_REST_TOKEN={S_REDIS}:latest" not in ln
            assert f"UPSTASH_REDIS_REST_TOKEN={S_REDIS}:" in ln  # pinned numeric
    assert result is not None


# ===========================================================================
# C4 — exact WIF attributeMapping
# ===========================================================================

@pytest.mark.parametrize("mapping,expect_detail", [
    (json.dumps({"google.subject": "WRONG"}), "wrong-expression"),
    (json.dumps({"attribute.aud": "assertion.aud"}), "missing"),
    (json.dumps({"google.subject": "assertion.sub", "attribute.aud": "assertion.aud", "attribute.extra": "x"}), "extra"),
])
def test_wif_attribute_mapping_mismatch_blocks(strict_bin, tmp_path, mapping, expect_detail):
    provider = json.dumps({"oidc": {"issuerUri": WIF_ISSUER, "allowedAudiences": [WIF_AUDIENCE]},
                           "attributeMapping": json.loads(mapping), "attributeCondition": WIF_COND})
    result, _ = _apply(strict_bin, tmp_path, wif=True, MOCK_WIF_PROVIDER_JSON=provider)
    assert result.returncode != 0
    line = [l for l in result.stdout.splitlines() if "[BLOCKED] wif:attribute-mapping" in l]
    assert line, result.stdout
    assert expect_detail in line[0]


# ===========================================================================
# C5 — Upstash validation fail-closed
# ===========================================================================

@pytest.mark.parametrize("detail", [
    json.dumps({"database_id": "db_prod_1", "database_name": "milo-production", "tls": True, "region": "us-central1", "endpoint": "h.upstash.io", "rest_token": UPSTASH_REST_TOKEN_SECRET}),  # missing state
    json.dumps({"database_id": "db_prod_1", "database_name": "milo-production", "state": "active", "region": "us-central1", "endpoint": "h.upstash.io", "rest_token": UPSTASH_REST_TOKEN_SECRET}),  # missing tls
    json.dumps({"database_id": "db_prod_1", "database_name": "milo-production", "state": "active", "tls": True, "endpoint": "h.upstash.io", "rest_token": UPSTASH_REST_TOKEN_SECRET}),  # missing region
    json.dumps({"database_id": "db_prod_1", "database_name": "milo-production", "state": "active", "tls": True, "region": "global", "endpoint": "h.upstash.io", "rest_token": UPSTASH_REST_TOKEN_SECRET}),  # global without primary_region
    json.dumps({"database_id": "db_prod_1", "database_name": "milo-production", "state": "active", "tls": True, "region": "eu-central-1", "endpoint": "h.upstash.io", "rest_token": UPSTASH_REST_TOKEN_SECRET}),  # wrong region
    json.dumps({"database_id": "db_prod_1", "database_name": "milo-production", "state": "active", "tls": True, "region": "global", "primary_region": "us-central1", "endpoint": "evil.example.com", "rest_token": UPSTASH_REST_TOKEN_SECRET}),  # foreign host
])
def test_upstash_validation_fail_closed(strict_bin, tmp_path, detail):
    result, _ = _apply(strict_bin, tmp_path, MOCK_UPSTASH_DETAIL=detail)
    assert result.returncode != 0
    assert "[BLOCKED] upstash:validate" in result.stdout


def test_upstash_documented_response_without_platform_passes(strict_bin, tmp_path):
    # The documented get_database response has NO `platform` field (it is a
    # create-request-only parameter); a real response must never be blocked
    # for lacking it.
    result, _ = _apply(strict_bin, tmp_path)  # DEFAULT_DETAIL has no platform
    assert "[BLOCKED] upstash:validate" not in result.stdout
    assert "[PASS] upstash:url" in result.stdout


# ===========================================================================
# C6 — mandatory exact Vercel verify + non-finite budgets
# ===========================================================================

def test_vercel_env_run_failure_blocks_in_apply(strict_bin, tmp_path):
    # env run returns unexpected output -> BLOCKED (never MANUAL) in apply.
    result, _ = _apply(strict_bin, tmp_path, MOCK_VERCEL_ENVRUN="UNEXPECTED_OUTPUT")
    assert result.returncode != 0
    assert "[BLOCKED] audit:vercel:value:GATEWAY_ALLOW_EXECUTION_ROUTES" in result.stdout


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", "inf"])
def test_non_finite_budget_blocks(strict_bin, tmp_path, bad):
    result, _ = _apply(strict_bin, tmp_path, service=api_service_json(plain_over={"MILO_MAX_COST_PER_RUN": bad}))
    assert result.returncode != 0
    assert "[BLOCKED] live:api:budget:MILO_MAX_COST_PER_RUN" in result.stdout


def test_workflow_plan_job_gated_on_mode():
    import yaml
    wf = yaml.safe_load((REPO / ".github/workflows/bootstrap-production.yml").read_text())
    assert wf["jobs"]["plan"]["if"] == "${{ github.event.inputs.mode == 'plan' }}"
    assert wf["jobs"]["apply"]["if"] == "${{ github.event.inputs.mode == 'apply' }}"


def test_workflow_installs_pin_and_runs_contract_preflight():
    text = (REPO / ".github/workflows/bootstrap-production.yml").read_text()
    assert "scripts/release/VERCEL_CLI_VERSION" in text  # single source of truth
    assert "39.3.0" not in text  # the broken pin (no env update/run) is gone
    assert "test_contract_vercel_cli.py" in text
    assert "test_contract_upstash_api.py" in text
    assert "MILO_REQUIRE_VERCEL_CLI_CONTRACT" in text


# ===========================================================================
# D1 — Vercel CLI capability preflight (fail closed before any mutation)
# ===========================================================================

def test_vercel_cli_preflight_passes_and_reports_version(strict_bin, tmp_path):
    result, _ = _apply(strict_bin, tmp_path)
    assert "[PASS] vercel:cli-version" in result.stdout
    log = read_log(strict_bin)
    assert "vercel --version" in log
    assert "vercel env --help" in log


def test_vercel_cli_nonnumeric_version_blocks_before_mutations(strict_bin, tmp_path):
    result, _ = _apply(strict_bin, tmp_path, MOCK_VERCEL_VERSION="canary-20260101")
    assert result.returncode != 0
    assert "[BLOCKED] vercel:cli-contract" in result.stdout
    log = mutation_log(strict_bin)
    assert "env add" not in log and "env update" not in log


def test_vercel_cli_missing_update_subcommand_blocks_before_mutations(strict_bin, tmp_path):
    crippled = "  add     name [environment]\n  list    [environment]\n  pull    [filename]\n  remove  name [environment]\n  run     command\n"
    result, _ = _apply(strict_bin, tmp_path, MOCK_VERCEL_ENV_HELP=crippled)
    assert result.returncode != 0
    assert "[BLOCKED] vercel:cli-contract" in result.stdout
    log = mutation_log(strict_bin)
    assert "env add" not in log and "env update" not in log


def test_vercel_cli_missing_env_run_environment_blocks(strict_bin, tmp_path):
    result, _ = _apply(strict_bin, tmp_path, MOCK_VERCEL_ENVRUN_HELP="no environment flag here")
    assert result.returncode != 0
    assert "[BLOCKED] vercel:cli-contract" in result.stdout
    assert "env add" not in mutation_log(strict_bin)


def test_vercel_cli_contract_failure_blocks_audit_only(strict_bin, tmp_path):
    # A broken CLI must surface as BLOCKED in the audit too — the exact Vercel
    # value verification silently not running must never count as ready.
    result, _ = _audit_only(strict_bin, tmp_path, wif=True, MOCK_VERCEL_VERSION="canary-x")
    assert result.returncode != 0
    assert "[BLOCKED] audit:vercel:cli-contract" in result.stdout


def test_vercel_cli_contract_failure_is_manual_in_plan(strict_bin, tmp_path):
    out = _out(tmp_path)
    result = strict_bin("--output-directory", str(out), "--expected-project", PROJECT,
                        "--api-sa", API_SA, "--worker-sa", WORKER_SA, "--gateway-sa", GATEWAY_SA,
                        env={"MOCK_GCLOUD_PROJECT": PROJECT, "MOCK_EXISTING_SECRETS": ALL_SECRET_NAMES,
                             "MOCK_SECRETS_WITH_VERSIONS": ALL_SECRET_NAMES,
                             "MOCK_VERCEL_VERSION": "canary-20260101"})
    assert "[MANUAL] vercel:cli-contract" in result.stdout


# ===========================================================================
# D2 — env-run verification is isolated from local env and .env overrides
# ===========================================================================

def test_env_run_isolated_from_caller_environment_override(strict_bin, tmp_path):
    # A polluted caller environment tries to shadow the production value of a
    # verified kill switch. The isolated invocation must scrub it: the CLI
    # subprocess must NOT see the caller's value, and the verdict must come
    # from the (mocked) production records.
    result, _ = _apply(strict_bin, tmp_path,
                       GATEWAY_ALLOW_EXECUTION_ROUTES="true")  # hostile local override
    log = read_log(strict_bin)
    assert "envrun-sees GATEWAY_ALLOW_EXECUTION_ROUTES=unset" in log, (
        "the caller-side override leaked into the `vercel env run` subprocess")
    assert "[PASS] audit:vercel:value:GATEWAY_ALLOW_EXECUTION_ROUTES" in result.stdout


def test_env_run_override_cannot_forge_a_match(strict_bin, tmp_path):
    # Production (mock) reports MISMATCH; a caller-side override must not be
    # able to turn that into a MATCH.
    result, _ = _apply(strict_bin, tmp_path, MOCK_VERCEL_ENVRUN="MISMATCH",
                       GATEWAY_ALLOW_EXECUTION_ROUTES="false")
    assert result.returncode != 0
    assert "[BLOCKED] audit:vercel:value:GATEWAY_ALLOW_EXECUTION_ROUTES" in result.stdout


def test_env_run_uses_isolated_empty_cwd(strict_bin, tmp_path):
    result, _ = _apply(strict_bin, tmp_path)
    envrun_lines = [ln for ln in read_log(strict_bin).splitlines()
                    if ln.startswith("vercel env run") and "--help" not in ln]
    assert envrun_lines, "expected isolated env run invocations"
    for ln in envrun_lines:
        assert "-e production" in ln  # production-only records
        assert "--cwd" in ln          # never the checkout (no .env overlay)
        assert str(REPO) not in ln
    assert result is not None


def test_env_run_refuses_passthrough_identity_names(strict_bin, tmp_path):
    # Names the CLI needs from the environment can never be proven via env run.
    report = tmp_path / "cvc.json"
    run_env = dict(os.environ)
    run_env["PATH"] = f"{strict_bin.bin_dir}:{run_env['PATH']}"
    run_env["MOCK_LOG"] = str(strict_bin.log)
    run_env.update({"MOCK_VERCEL_ENV_NAMES": VERCEL_REUSE, "MOCK_VERCEL_INSPECT_ID": VPID,
                    "MOCK_VERCEL_INSPECT_ORG": VORG, "VTOK": VERCEL_TOKEN_SECRET})
    result = subprocess.run(
        ["bash", str(RELEASE / "check-vercel-config.sh"), "--project", "milo-agent-workspace",
         "--project-id", VPID, "--org-id", VORG, "--token-env", "VTOK", "--strict-values",
         "--expect", "VERCEL_TOKEN=whatever", "--json-output", str(report)],
        capture_output=True, text=True, env=run_env, cwd=REPO, timeout=60)
    assert "[BLOCKED] vercel:value:VERCEL_TOKEN" in result.stdout
    assert "cannot be isolated" in result.stdout


# ===========================================================================
# D3 — explicit fail-closed audit-only contract (metadata-assisted path)
# ===========================================================================

def _now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _metadata_file(tmp_path, *, db_id="db_prod_1", fp=REDIS_FP, ver="3",
                   url="https://prod-1.upstash.io", status="applied",
                   release_sha=FULL_SHA, project=PROJECT, environment="production",
                   generated_at=None, drop=()):
    lines = {
        "MILO_METADATA_SCHEMA_VERSION": "2",
        "MILO_BOOTSTRAP_STATUS": status,
        "MILO_ENVIRONMENT": environment,
        "MILO_BOOTSTRAP_SHA": release_sha,
        "MILO_METADATA_GENERATED_AT": generated_at or _now_iso(),
        "GCP_PROJECT_ID": project,
        "MILO_REDIS_DB_ID": db_id,
        "MILO_REDIS_TOKEN_FINGERPRINT": fp,
        "MILO_REDIS_SECRET_VERSION": ver,
        "UPSTASH_REDIS_REST_URL": url,
    }
    md = tmp_path / "stored.metadata.env"
    md.write_text("# GENERATED non-secret production metadata. NO secret values.\n" +
                  "".join(f"{k}={v}\n" for k, v in lines.items() if k not in drop))
    return md


def _audit_metadata(strict_bin, tmp_path, metadata_path, extra_args=None, **over):
    """audit-only WITHOUT Upstash management credentials, metadata-driven
    (metadata-assisted credentialed audit: gcloud + Vercel token only)."""
    out = _out(tmp_path)
    svc, jb = _write_fixtures(tmp_path, api_service_json(secret_over={"UPSTASH_REDIS_REST_TOKEN": (S_REDIS, "3")}, plain_over={"MILO_REDIS_SECRET_VERSION": "3"}),
                              worker_job_json(secret_over={"UPSTASH_REDIS_REST_TOKEN": (S_REDIS, "3")}, plain_over={"MILO_REDIS_SECRET_VERSION": "3"}))
    args = ["--audit-only", "--output-directory", str(out), "--expected-project", PROJECT,
            "--expected-account", OPERATOR, "--release-sha", FULL_SHA, "--rollback-sha", FULL_SHA,
            "--region", REGION, "--repository", "milo-agent",
            "--api-service", "milo-agent-api", "--worker-job", "milo-agent-worker",
            "--api-sa", API_SA, "--worker-sa", WORKER_SA, "--gateway-sa", GATEWAY_SA,
            "--supabase-project-ref", "vvhtaqgkgkalpfcbuvag", "--production-origin", ORIGIN,
            "--vercel-project", "milo-agent-workspace", "--vercel-project-id", VPID, "--vercel-org-id", VORG,
            "--vercel-token-env", "VERCEL_TOK",
            "--wif-pool", WIF_POOL, "--wif-provider", WIF_PROVIDER, "--wif-issuer", WIF_ISSUER,
            "--wif-audience", WIF_AUDIENCE, "--wif-attribute-condition", WIF_COND,
            "--wif-attribute-mapping", WIF_MAPPING, "--wif-principal-set", WIF_PRINCIPAL]
    if metadata_path is not None:
        args += ["--audit-metadata", str(metadata_path)]
    if extra_args:
        args += list(extra_args)
    env = {"MOCK_GCLOUD_PROJECT": PROJECT, "MOCK_GCLOUD_ACCOUNT": OPERATOR,
           "MOCK_SERVICE_JSON": str(svc), "MOCK_JOB_JSON": str(jb),
           "MOCK_EXISTING_SECRETS": ALL_SECRET_NAMES, "MOCK_SECRETS_WITH_VERSIONS": ALL_SECRET_NAMES,
           "MOCK_REDIS_ENABLED_VERSION": "3", "MOCK_REDIS_SM_PAYLOAD": UPSTASH_REST_TOKEN_SECRET,
           "MOCK_VERCEL_ENV_NAMES": VERCEL_REUSE, "MOCK_VERCEL_INSPECT_ID": VPID, "MOCK_VERCEL_INSPECT_ORG": VORG,
           "MOCK_VERCEL_ENVRUN": "MATCH", "MOCK_VERCEL_FP": REDIS_FP,
           "MOCK_WIF_PROVIDER_JSON": PROVIDER_JSON_OK, "MOCK_WIF_GATEWAY_POLICY": GATEWAY_POLICY_OK, "MOCK_RUN_INVOKER_POLICY": RUN_POLICY_OK,
           "VERCEL_TOK": VERCEL_TOKEN_SECRET}
    env.update(over)
    return strict_bin(*args, env=env), out


def test_audit_contract_metadata_assisted_passes(strict_bin, tmp_path):
    md = _metadata_file(tmp_path)
    result, _ = _audit_metadata(strict_bin, tmp_path, md)
    assert "[PASS] audit:contract" in result.stdout
    assert "[PASS] audit:metadata" in result.stdout
    assert "[PASS] redis:audit" in result.stdout
    log = mutation_log(strict_bin)
    # no Upstash management API call, no mutation of any kind.
    assert "curl " not in log
    for verb in ("secrets versions add", "secrets create", "run services update",
                 "run jobs update", "env add", "env update", "iam service-accounts create"):
        assert verb not in log, f"metadata-assisted audit mutated: {verb}"


def test_audit_contract_neither_source_blocks(strict_bin, tmp_path):
    result, _ = _audit_metadata(strict_bin, tmp_path, None)
    assert result.returncode != 0
    assert "[BLOCKED] audit:contract" in result.stdout
    assert "curl " not in mutation_log(strict_bin)


def test_audit_metadata_missing_file_blocks(strict_bin, tmp_path):
    result, _ = _audit_metadata(strict_bin, tmp_path, tmp_path / "nope.env")
    assert result.returncode != 0
    assert "[BLOCKED] audit:metadata" in result.stdout


@pytest.mark.parametrize("mutation", [
    dict(drop=("MILO_REDIS_SECRET_VERSION",)),
    dict(drop=("MILO_REDIS_TOKEN_FINGERPRINT",)),
    dict(drop=("MILO_REDIS_DB_ID",)),
    dict(drop=("UPSTASH_REDIS_REST_URL",)),
    dict(fp="not-a-fingerprint"),
    dict(ver="latest"),
    dict(ver="0"),
    dict(url="https://evil.example.com"),
    # Provenance / binding / freshness (fail closed on every violation):
    dict(status="partial-failure"),
    dict(status="planned"),
    dict(drop=("MILO_BOOTSTRAP_STATUS",)),
    dict(release_sha="e" * 40),                 # a different release
    dict(release_sha="not-a-full-sha"),
    dict(drop=("MILO_BOOTSTRAP_SHA",)),
    dict(project="some-other-project"),
    dict(drop=("GCP_PROJECT_ID",)),
    dict(environment="staging"),
    dict(drop=("MILO_ENVIRONMENT",)),
    dict(generated_at="2020-01-01T00:00:00Z"),  # stale (older than max age)
    dict(generated_at="2999-01-01T00:00:00Z"),  # future timestamp
    dict(generated_at="not-a-timestamp"),
    dict(drop=("MILO_METADATA_GENERATED_AT",)),
])
def test_audit_metadata_invalid_blocks(strict_bin, tmp_path, mutation):
    md = _metadata_file(tmp_path, **mutation)
    result, _ = _audit_metadata(strict_bin, tmp_path, md)
    assert result.returncode != 0
    assert "[BLOCKED] audit:metadata" in result.stdout




@pytest.mark.parametrize("extra_line", [
    "MILO_METADATA_SCHEMA_VERSION=1\n",
    "MILO_METADATA_SCHEMA_VERSION=3\n",
    "MILO_METADATA_SCHEMA_VERSION=two\n",
])
def test_audit_metadata_schema_version_must_be_exactly_two(strict_bin, tmp_path, extra_line):
    md = _metadata_file(tmp_path)
    text = md.read_text().replace("MILO_METADATA_SCHEMA_VERSION=2\n", extra_line)
    md.write_text(text)
    result, _ = _audit_metadata(strict_bin, tmp_path, md)
    assert result.returncode != 0
    assert "[BLOCKED] audit:metadata" in result.stdout


@pytest.mark.parametrize("mutate", [
    lambda text: text + "MILO_REDIS_DB_ID=db_prod_1\n",
    lambda text: text.replace("MILO_METADATA_SCHEMA_VERSION=2\n", ""),
    lambda text: text + "MALFORMED_LINE_WITHOUT_EQUALS\n",
    lambda text: text.replace("MILO_REDIS_DB_ID=db_prod_1\n", "MILO_REDIS_DB_ID= db_prod_1 \n"),
])
def test_audit_metadata_strict_parser_rejects_duplicates_missing_malformed_and_normalized_values(strict_bin, tmp_path, mutate):
    md = _metadata_file(tmp_path)
    md.write_text(mutate(md.read_text()))
    result, _ = _audit_metadata(strict_bin, tmp_path, md)
    assert result.returncode != 0
    assert "[BLOCKED] audit:metadata" in result.stdout


def test_audit_metadata_requires_release_sha_argument(strict_bin, tmp_path):
    # The audit is bootstrap-bound: without --release-sha the metadata path is
    # BLOCKED (never audited unbound).
    md = _metadata_file(tmp_path)
    out = _out(tmp_path)
    result = strict_bin("--audit-only", "--audit-metadata", str(md),
                        "--output-directory", str(out), "--expected-project", PROJECT,
                        env={"MOCK_GCLOUD_PROJECT": PROJECT,
                             "MOCK_EXISTING_SECRETS": ALL_SECRET_NAMES,
                             "MOCK_SECRETS_WITH_VERSIONS": ALL_SECRET_NAMES,
                             "MOCK_REDIS_ENABLED_VERSION": "3",
                             "MOCK_REDIS_SM_PAYLOAD": UPSTASH_REST_TOKEN_SECRET})
    assert result.returncode != 0
    assert "[BLOCKED] audit:metadata" in result.stdout
    assert "--release-sha is required" in result.stdout


def test_audit_metadata_wrong_db_id_blocks_against_live(strict_bin, tmp_path):
    # Blocker regression: a well-formed metadata file whose database ID does
    # not match the LIVE Cloud Run identity metadata must block the audit.
    md = _metadata_file(tmp_path, db_id="db_wrong_identity")
    result, _ = _audit_metadata(strict_bin, tmp_path, md)
    assert result.returncode != 0
    assert "[BLOCKED] live:api:MILO_REDIS_DB_ID" in result.stdout
    assert "[BLOCKED] live:worker:MILO_REDIS_DB_ID" in result.stdout


def test_audit_metadata_max_age_flag_controls_staleness(strict_bin, tmp_path):
    from datetime import datetime, timedelta, timezone
    two_days_ago = (datetime.now(timezone.utc) - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")
    md = _metadata_file(tmp_path, generated_at=two_days_ago)
    # Within the default 720h window: loads fine.
    result, _ = _audit_metadata(strict_bin, tmp_path, md)
    assert "[PASS] audit:metadata" in result.stdout
    # With a 24h window the same metadata is stale: BLOCKED.
    result2, _ = _audit_metadata(strict_bin, tmp_path, md,
                                 extra_args=["--audit-metadata-max-age-hours", "24"])
    assert result2.returncode != 0
    assert "STALE" in result2.stdout


def test_audit_metadata_live_version_mismatch_blocks(strict_bin, tmp_path):
    md = _metadata_file(tmp_path, ver="3")
    result, _ = _audit_metadata(strict_bin, tmp_path, md, MOCK_REDIS_ENABLED_VERSION="7")
    assert result.returncode != 0
    assert "[BLOCKED] redis:audit" in result.stdout
    assert "secrets versions add" not in mutation_log(strict_bin)


def test_audit_metadata_live_fingerprint_mismatch_blocks(strict_bin, tmp_path):
    md = _metadata_file(tmp_path)
    result, _ = _audit_metadata(strict_bin, tmp_path, md, MOCK_REDIS_SM_PAYLOAD="a-rotated-token")
    assert result.returncode != 0
    assert "[BLOCKED] redis:audit" in result.stdout


def test_audit_metadata_rejected_outside_audit_only(strict_bin, tmp_path):
    out = _out(tmp_path)
    result = strict_bin("--plan", "--audit-metadata", str(_metadata_file(tmp_path)),
                        "--output-directory", str(out))
    assert result.returncode != 0
    assert "only valid with --audit-only" in result.stderr


# ===========================================================================
# D4 — WIF inspection failures are evidence_missing (fail closed)
# ===========================================================================

def test_wif_pool_permission_error_blocks_in_apply(strict_bin, tmp_path):
    result, _ = _apply(strict_bin, tmp_path, wif=True, MOCK_WIF_POOL="error")
    assert result.returncode != 0
    line = [ln for ln in result.stdout.splitlines() if "[BLOCKED] wif:pool" in ln]
    assert line, result.stdout
    assert "NOT proven" in line[0]


def test_wif_pool_permission_error_blocks_in_audit_only(strict_bin, tmp_path):
    result, _ = _audit_only(strict_bin, tmp_path, wif=True, MOCK_WIF_POOL="error")
    assert result.returncode != 0
    assert "[BLOCKED] wif:pool" in result.stdout


def test_wif_provider_unreadable_blocks_in_apply(strict_bin, tmp_path):
    result, _ = _apply(strict_bin, tmp_path, wif=True, MOCK_WIF_PROVIDER_JSON="not-json-at-all")
    assert result.returncode != 0
    assert "[BLOCKED] wif:provider" in result.stdout


# ===========================================================================
# D5 — IAM re-verification after mutation (re-read the live policy)
# ===========================================================================

def test_run_invoker_bind_is_reverified_after_mutation(strict_bin, tmp_path):
    result, _ = _apply(strict_bin, tmp_path, wif=True,
                       MOCK_RUN_INVOKER_POLICY='{"bindings":[]}',
                       MOCK_RUN_INVOKER_POLICY_AFTER=RUN_POLICY_OK)
    log = read_log(strict_bin)
    assert "run services add-iam-policy-binding" in log
    line = [ln for ln in result.stdout.splitlines() if "[PASS] wif:run-invoker" in ln]
    assert line and "re-verified" in line[0], result.stdout


def test_run_invoker_bind_not_effective_blocks(strict_bin, tmp_path):
    # The bind succeeds but the re-read policy still lacks the gateway SA:
    # the mutation did not take effect and readiness must be refused.
    result, _ = _apply(strict_bin, tmp_path, wif=True,
                       MOCK_RUN_INVOKER_POLICY='{"bindings":[]}',
                       MOCK_RUN_INVOKER_POLICY_AFTER='{"bindings":[]}')
    assert result.returncode != 0
    line = [ln for ln in result.stdout.splitlines() if "[BLOCKED] wif:run-invoker" in ln]
    assert line and "did not take effect" in line[0], result.stdout


def test_run_invoker_broad_principal_after_bind_blocks(strict_bin, tmp_path):
    broad = json.dumps({"bindings": [{"role": "roles/run.invoker",
                                      "members": [f"serviceAccount:{GATEWAY_SA}", "allUsers"]}]})
    result, _ = _apply(strict_bin, tmp_path, wif=True,
                       MOCK_RUN_INVOKER_POLICY='{"bindings":[]}',
                       MOCK_RUN_INVOKER_POLICY_AFTER=broad)
    assert result.returncode != 0
    assert "[BLOCKED] wif:run-invoker" in result.stdout


def test_secret_accessor_grants_are_reverified(strict_bin, tmp_path):
    result, _ = _apply(strict_bin, tmp_path)
    line = [ln for ln in result.stdout.splitlines()
            if f"[PASS] gcp:accessor:{S_SUPA_URL}:{API_SA}" in ln]
    assert line and "re-verified" in line[0], result.stdout


def test_secret_accessor_mutation_not_effective_blocks(strict_bin, tmp_path):
    # add-iam-policy-binding "succeeds" but the re-read policy has no binding.
    result, _ = _apply(strict_bin, tmp_path, MOCK_SECRET_POLICY='{"bindings":[]}')
    assert result.returncode != 0
    line = [ln for ln in result.stdout.splitlines()
            if f"[BLOCKED] gcp:accessor:{S_SUPA_URL}:{API_SA}" in ln]
    assert line and "did not take effect" in line[0], result.stdout


def test_secret_accessor_broad_principal_blocks_without_mutation(strict_bin, tmp_path):
    # A pre-existing broad principal is detected on the PRE-mutation read:
    # the policy must be repaired first, and no binding is ever added to it.
    broad = json.dumps({"bindings": [{"role": "roles/secretmanager.secretAccessor",
                                      "members": [f"serviceAccount:{API_SA}", "allUsers"]}]})
    result, _ = _apply(strict_bin, tmp_path, MOCK_SECRET_POLICY=broad)
    assert result.returncode != 0
    assert f"[BLOCKED] gcp:accessor:{S_SUPA_URL}:{API_SA}" in result.stdout
    assert "secrets add-iam-policy-binding" not in read_log(strict_bin)


@pytest.mark.parametrize("bad_policy", [
    "__DENIED__",              # permission failure on the initial read
    "__APIERROR__",            # API/backend failure on the initial read
    "this is { not json",      # malformed policy JSON
])
def test_secret_accessor_unreadable_policy_blocks_before_mutation(strict_bin, tmp_path, bad_policy):
    result, _ = _apply(strict_bin, tmp_path, MOCK_SECRET_POLICY=bad_policy)
    assert result.returncode != 0
    line = [ln for ln in result.stdout.splitlines()
            if f"[BLOCKED] gcp:accessor:{S_SUPA_URL}:{API_SA}" in ln]
    assert line and "no IAM mutation is issued" in line[0], result.stdout
    assert "secrets add-iam-policy-binding" not in read_log(strict_bin), (
        "an IAM mutation was issued although the secret IAM state was unreadable")


# ===========================================================================
# E1 — trustworthy metadata lifecycle (written only after full success)
# ===========================================================================

def test_metadata_withheld_on_partial_failure(strict_bin, tmp_path):
    result, out = _apply(strict_bin, tmp_path, MOCK_UPSTASH_LIST_FAIL="1")
    assert result.returncode != 0
    assert not (out / "milo-production.metadata.env").exists(), (
        "audit-grade metadata must never exist after a partial failure")
    assert "metadata:withheld" in result.stdout


def test_metadata_written_only_after_full_success(strict_bin, tmp_path):
    # Model the fully-converged production state (managed Vercel vars present).
    names = (VERCEL_REUSE + " GATEWAY_ALLOW_EXECUTION_ROUTES NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI"
             " UPSTASH_REDIS_REST_URL UPSTASH_REDIS_REST_TOKEN MILO_REDIS_TOKEN_FINGERPRINT")
    result, out = _apply(strict_bin, tmp_path, wif=True, MOCK_VERCEL_ENV_NAMES=names)
    assert result.returncode == 0, result.stdout + result.stderr
    md_path = out / "milo-production.metadata.env"
    assert md_path.exists()
    md = md_path.read_text()
    report = json.loads((out / "bootstrap-apply.json").read_text())
    assert report["status"] == "applied"
    # Release-bound, project/environment-bound, timestamped, status applied.
    assert f"MILO_BOOTSTRAP_SHA={report['head_sha']}" in md
    assert "MILO_BOOTSTRAP_STATUS=applied" in md
    assert "MILO_ENVIRONMENT=production" in md
    assert f"GCP_PROJECT_ID={PROJECT}" in md
    assert "MILO_METADATA_GENERATED_AT=" in md
    assert "MILO_REDIS_DB_ID=db_prod_1" in md
    assert f"MILO_REDIS_TOKEN_FINGERPRINT={REDIS_FP}" in md
    assert f"MILO_REDIS_SECRET_VERSION={REDIS_VER}" in md
    assert "UPSTASH_REDIS_REST_URL=https://prod-1.upstash.io" in md
    # And it round-trips through the audit it was made for.
    result2, _ = _audit_metadata(strict_bin, tmp_path, md_path,
                                 extra_args=["--release-sha", report["head_sha"]])
    # NB: _audit_metadata passes its own --release-sha FULL_SHA first; the
    # later flag wins in the parser, binding the audit to the applied release.
    assert "[PASS] audit:metadata" in result2.stdout


def test_plan_never_writes_audit_metadata(strict_bin, tmp_path):
    out = _out(tmp_path)
    result = strict_bin("--output-directory", str(out), "--expected-project", PROJECT,
                        "--api-sa", API_SA, "--worker-sa", WORKER_SA, "--gateway-sa", GATEWAY_SA,
                        env={"MOCK_GCLOUD_PROJECT": PROJECT, "MOCK_EXISTING_SECRETS": ALL_SECRET_NAMES,
                             "MOCK_SECRETS_WITH_VERSIONS": ALL_SECRET_NAMES})
    assert "[PASS] mode — bootstrap mode: plan" in result.stdout
    assert not (out / "milo-production.metadata.env").exists(), (
        "plan output must never look like audit-grade metadata")


def test_audit_only_never_writes_audit_metadata(strict_bin, tmp_path):
    _result, out = _audit_only(strict_bin, tmp_path, wif=True)
    assert not (out / "milo-production.metadata.env").exists()


# ===========================================================================
# E2 — live Redis/release identity verification (Cloud Run control plane)
# ===========================================================================

def test_apply_configures_release_sha_on_cloud_run(strict_bin, tmp_path):
    _result, out = _apply(strict_bin, tmp_path)
    report = json.loads((out / "bootstrap-apply.json").read_text())
    log = read_log(strict_bin)
    api_upd = [ln for ln in log.splitlines() if "run services update" in ln][0]
    job_upd = [ln for ln in log.splitlines() if "run jobs update" in ln][0]
    assert f"MILO_BOOTSTRAP_SHA={report['head_sha']}" in api_upd
    assert f"MILO_BOOTSTRAP_SHA={report['head_sha']}" in job_upd


@pytest.mark.parametrize("drift,expect", [
    (dict(plain_over={"MILO_REDIS_DB_ID": "db_other"}), "live:api:MILO_REDIS_DB_ID"),
    (dict(drop=["MILO_REDIS_DB_ID"]), "live:api:MILO_REDIS_DB_ID"),
    (dict(plain_over={"MILO_REDIS_TOKEN_FINGERPRINT": "deadbeefdeadbeef"}), "live:api:MILO_REDIS_TOKEN_FINGERPRINT"),
    (dict(plain_over={"MILO_REDIS_SECRET_VERSION": "999"}), "live:api:MILO_REDIS_SECRET_VERSION"),
    (dict(plain_over={"MILO_BOOTSTRAP_SHA": "f" * 40}), "live:api:MILO_BOOTSTRAP_SHA"),
    (dict(drop=["MILO_BOOTSTRAP_SHA"]), "live:api:MILO_BOOTSTRAP_SHA"),
])
def test_live_identity_drift_blocks_apply(strict_bin, tmp_path, drift, expect):
    result, _ = _apply(strict_bin, tmp_path, service=api_service_json(**drift))
    assert result.returncode != 0
    assert f"[BLOCKED] {expect}" in result.stdout


def test_live_identity_drift_blocks_audit_only(strict_bin, tmp_path):
    # Credentialed deep audit: the selected Upstash db id must equal the live
    # Cloud Run identity metadata.
    result, _ = _audit_only(strict_bin, tmp_path, wif=True,
                            MOCK_UPSTASH_DBS=json.dumps([{"database_name": "milo-production",
                                                          "database_id": "db_live_mismatch"}]),
                            MOCK_UPSTASH_DETAIL=json.dumps({
                                "database_id": "db_live_mismatch", "database_name": "milo-production",
                                "endpoint": "prod-1.upstash.io", "state": "active", "tls": True,
                                "region": "global", "primary_region": "us-central1",
                                "rest_token": UPSTASH_REST_TOKEN_SECRET}))
    assert result.returncode != 0
    assert "[BLOCKED] live:api:MILO_REDIS_DB_ID" in result.stdout


def test_vercel_expects_fingerprint_variable(strict_bin, tmp_path):
    _result, _out_dir = _apply(strict_bin, tmp_path)
    log = read_log(strict_bin)
    # The audit verifies the managed non-secret fingerprint variable in Vercel
    # via the isolated env-run path (check-vercel-config --expect).
    assert "envrun-sees MILO_REDIS_TOKEN_FINGERPRINT=unset" in log


# ===========================================================================
# E3 — IAM inspection failures block BEFORE any mutation
# ===========================================================================

@pytest.mark.parametrize("bad_policy", [
    "__DENIED__",              # permission failure on the initial read
    "__APIERROR__",            # API/backend failure on the initial read
    "this is { not json",      # malformed policy JSON
])
def test_run_invoker_unreadable_policy_blocks_before_mutation(strict_bin, tmp_path, bad_policy):
    result, _ = _apply(strict_bin, tmp_path, wif=True, MOCK_RUN_INVOKER_POLICY=bad_policy)
    assert result.returncode != 0
    line = [ln for ln in result.stdout.splitlines() if "[BLOCKED] wif:run-invoker" in ln]
    assert line and "no IAM mutation is issued" in line[0], result.stdout
    assert "run services add-iam-policy-binding" not in read_log(strict_bin), (
        "an IAM mutation was issued although the current IAM state was unreadable")


def test_run_invoker_unreadable_policy_manual_in_plan(strict_bin, tmp_path):
    out = _out(tmp_path)
    result = strict_bin("--output-directory", str(out), "--expected-project", PROJECT,
                        "--api-sa", API_SA, "--worker-sa", WORKER_SA, "--gateway-sa", GATEWAY_SA,
                        env={"MOCK_GCLOUD_PROJECT": PROJECT, "MOCK_EXISTING_SECRETS": ALL_SECRET_NAMES,
                             "MOCK_SECRETS_WITH_VERSIONS": ALL_SECRET_NAMES,
                             "MOCK_RUN_INVOKER_POLICY": "__DENIED__"})
    assert "[MANUAL] wif:run-invoker" in result.stdout
    assert "run services add-iam-policy-binding" not in read_log(strict_bin)


# ===========================================================================
# E4 — workflow: metadata preservation + operational audit-only job
# ===========================================================================

def _load_workflow():
    import yaml
    wf = yaml.safe_load((REPO / ".github/workflows/bootstrap-production.yml").read_text())
    triggers = wf.get("on", wf.get(True))
    return wf, triggers


def test_workflow_audit_only_job_is_gated_and_read_only():
    wf, triggers = _load_workflow()
    assert "audit-only" in triggers["workflow_dispatch"]["inputs"]["mode"]["options"]
    audit = wf["jobs"]["audit"]
    assert audit["if"] == "${{ github.event.inputs.mode == 'audit-only' }}"
    run_steps = [s for s in audit["steps"] if "bootstrap-production.sh" in s.get("run", "")]
    assert len(run_steps) == 2
    for step in run_steps:
        assert "--audit-only" in step["run"]
        assert "--apply" not in step["run"]
        assert "--confirm-production-change" not in step["run"]
    metadata_steps = [s for s in run_steps if "metadata audit" in s["name"]]
    deep_steps = [s for s in run_steps if "deep audit" in s["name"]]
    assert len(metadata_steps) == len(deep_steps) == 1
    assert metadata_steps[0]["if"] == "${{ github.event.inputs.metadata_run_id != '' }}"
    assert deep_steps[0]["if"] == "${{ github.event.inputs.metadata_run_id == '' }}"
    assert "--upstash-email-env" not in metadata_steps[0]["run"]
    assert "--upstash-apikey-env" not in metadata_steps[0]["run"]
    assert "--upstash-email-env" in deep_steps[0]["run"]
    assert "--upstash-apikey-env" in deep_steps[0]["run"]
    # It can consume the preserved metadata artifact from a successful apply.
    downloads = [s for s in audit["steps"] if str(s.get("uses", "")).startswith("actions/download-artifact")]
    assert downloads and downloads[0]["with"]["name"] == "bootstrap-production-metadata"
    assert downloads[0]["if"] == "${{ github.event.inputs.metadata_run_id != '' }}"
    # Cross-run artifact download requires actions:read — without a job-level
    # permission grant the workflow-level block (contents/id-token only)
    # would make the download fail in the real GitHub Actions runtime.
    assert audit["permissions"]["actions"] == "read"
    assert downloads[0]["with"]["github-token"], "cross-run download needs an explicit token"


def test_workflow_preserves_metadata_only_on_success():
    wf, _triggers = _load_workflow()
    steps = wf["jobs"]["apply"]["steps"]
    uploads = [s for s in steps
               if str(s.get("uses", "")).startswith("actions/upload-artifact")
               and s.get("with", {}).get("name") == "bootstrap-production-metadata"]
    assert len(uploads) == 1
    md = uploads[0]
    assert md["if"] == "${{ success() }}", "metadata must never be uploaded from a failed/partial apply"
    assert md["with"]["if-no-files-found"] == "error"
    assert "milo-production.metadata.env" in md["with"]["path"]


def test_workflow_metadata_audit_step_contains_no_upstash_secret_references():
    text = (REPO / ".github/workflows/bootstrap-production.yml").read_text()
    start = text.index("Run fail-closed metadata audit")
    end = text.index("Run fail-closed deep audit", start)
    block = text[start:end]
    assert "UPSTASH_EMAIL" not in block
    assert "UPSTASH_API" not in block
    assert "upstash-email-env" not in block
    assert "upstash-apikey-env" not in block
    assert "test_contract_upstash_api.py" not in block


def test_workflow_deep_audit_requires_upstash_credentials_and_no_metadata_args():
    text = (REPO / ".github/workflows/bootstrap-production.yml").read_text()
    start = text.index("Run fail-closed deep audit")
    end = text.index("Upload redacted audit reports", start)
    block = text[start:end]
    assert "test -n \"${MILO_UPSTASH_EMAIL:-}\"" in block
    assert "test -n \"${MILO_UPSTASH_APIKEY:-}\"" in block
    assert "--upstash-email-env MILO_UPSTASH_EMAIL" in block
    assert "--upstash-apikey-env MILO_UPSTASH_APIKEY" in block
    assert "--audit-metadata" not in block

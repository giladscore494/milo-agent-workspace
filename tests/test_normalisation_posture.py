"""PR-D3: the normalisation job -- the only surface that holds the provider key
while the stage is on -- read by production-preflight.sh and
production-verify.sh against a PATH-shimmed gcloud (never a real project):
present + flag on PASS, absent + flag off PASS, anything else BLOCKED; and the
capture identity's `iam:capture-cannot-read-provider-key` stays PASS in every
state, since no script grants it an accessor."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from tests.test_scoped_rollout_contract import DB_OK, READY, SITE_OFF, _verdict, _verify_tree

REPO = Path(__file__).resolve().parents[1]
PREFLIGHT = REPO / "scripts" / "deploy" / "production-preflight.sh"
JOB = "test-capture-normalisation"
FLAG = "MILO_ENABLE_MANUFACTURER_NORMALISATION"

#: Answers every read the preflight makes; the job listing, the API's env and
#: the secrets' IAM from the test's state. It never mutates anything.
#: The permission sets the mocks answer `gcloud iam roles describe` with (the
#: Cloud Run and basic roles, as IAM documents them; abridged to what matters).
ROLES = {
    "roles/run.jobsExecutor": ["run.executions.get", "run.executions.list", "run.jobs.get", "run.jobs.list",
                               "run.jobs.run", "run.operations.get"],
    "roles/run.jobsExecutorWithOverrides": ["run.executions.get", "run.jobs.get", "run.jobs.run",
                                            "run.jobs.runWithOverrides"],
    "roles/run.invoker": ["run.jobs.run", "run.routes.invoke"],
    "roles/run.viewer": ["run.jobs.get", "run.jobs.list", "run.services.get"],
    "roles/run.developer": ["run.jobs.create", "run.jobs.run", "run.jobs.runWithOverrides"],
    "roles/editor": ["run.jobs.runWithOverrides", "secretmanager.versions.access"],
    "roles/logging.logWriter": ["logging.logEntries.create"],
    # A custom role that cannot override but can REWRITE the job's definition.
    "projects/test-project/roles/jobEditor": ["run.jobs.get", "run.jobs.run", "run.jobs.update"],
}
REWRITE = ("run.jobs.runWithOverrides", "run.jobs.update", "run.jobs.create", "run.jobs.replace",
           "run.jobs.setIamPolicy")

GCLOUD = r'''#!/usr/bin/env python3
import json, os, sys
ROLES = ''' + repr(ROLES) + r'''
a = sys.argv[1:]
if a[:2] == ["auth", "list"]: print("operator@example.test"); sys.exit(0)
if a[:3] == ["config", "get-value", "project"]: print("test-project"); sys.exit(0)
if a[:3] == ["run", "jobs", "list"]:
    assert a[3:7] == ["--region", "test-region", "--project", "test-project"], a
    if os.environ.get("T_LIST_FAIL"): sys.exit(1)
    name = a[7].split("=", 2)[2]
    if os.environ.get("T_JOB") == "1": print(name)
    sys.exit(0)
if a[:3] == ["run", "services", "describe"] and "--format=json" in a:
    if os.environ.get("T_API_FAIL"): sys.exit(1)
    env = [{"name": "MILO_ENABLE_MANUFACTURER_NORMALISATION", "value": os.environ.get("T_FLAG", "false")}]
    print(json.dumps({"spec": {"template": {"spec": {"containers": [{"env": env}]}}}})); sys.exit(0)
if a[:2] == ["secrets", "get-iam-policy"]:
    if a[2] in os.environ.get("T_POLICY_FAIL", "").split(","): sys.exit(1)
    capture = "serviceAccount:capture@test.iam.gserviceaccount.com"
    bindings = [{"role": "roles/secretmanager.secretAccessor",
                 "members": ["serviceAccount:worker@test.iam.gserviceaccount.com"]}]
    # The capture identity reads the Supabase pair; anything else only when a test says so.
    if a[2] in ("TEST_SUPABASE_URL", "TEST_SUPABASE_KEY") \
            or (a[2] == "TEST_PROVIDER_KEY" and os.environ.get("T_CAPTURE_READS_KEY")):
        bindings[0]["members"].append(capture)
    if a[2] in os.environ.get("T_CAPTURE_CONDITIONAL", "").split(","):
        bindings.append({"role": "roles/secretmanager.secretAccessor", "members": [capture],
                         "condition": {"title": "until-friday", "expression": "request.time < timestamp('2099-01-01T00:00:00Z')"}})
    print(json.dumps({"bindings": bindings}))
    sys.exit(0)
if a[:2] == ["projects", "get-iam-policy"]:
    assert a[2] == "test-project", a
    if os.environ.get("T_PROJECT_FAIL"): sys.exit(1)
    bindings = [{"role": "roles/logging.logWriter", "members": ["serviceAccount:api@test.iam.gserviceaccount.com",
                                                                "serviceAccount:capture@test.iam.gserviceaccount.com"]}]
    for pair in filter(None, os.environ.get("T_PROJECT_ROLES", "").split(",")):
        who, role = pair.split(":", 1)
        bindings.append({"role": role, "members": [f"serviceAccount:{who}@test.iam.gserviceaccount.com"]})
    print(json.dumps({"bindings": bindings})); sys.exit(0)
if a[:3] == ["run", "jobs", "get-iam-policy"]:
    assert a[4:8] == ["--region", "test-region", "--project", "test-project"], a
    if os.environ.get("T_JOB_POLICY_FAIL"): sys.exit(1)
    role = os.environ.get("T_API_JOB_ROLE", "roles/run.jobsExecutor")
    print(json.dumps({"bindings": [{"role": role, "members": ["serviceAccount:api@test.iam.gserviceaccount.com"]},
                                   {"role": "roles/run.viewer",
                                    "members": ["serviceAccount:api@test.iam.gserviceaccount.com"]}]}))
    sys.exit(0)
if a[:3] == ["iam", "roles", "describe"]:
    if a[3] in os.environ.get("T_ROLE_FAIL", "").split(","): sys.exit(1)
    print(json.dumps({"name": a[3], "includedPermissions": ROLES[a[3]]})); sys.exit(0)
for verb in ("create", "update", "delete", "add-iam-policy-binding", "remove-iam-policy-binding", "execute"):
    assert verb not in a, "the preflight is read-only: " + " ".join(a)
sys.exit(0)
'''

CONFIG = """GCP_PROJECT_ID=test-project
GCP_REGION=test-region
ARTIFACT_REGISTRY_REPOSITORY=test-repo
CLOUD_RUN_API_SERVICE=test-api
CLOUD_RUN_WORKER_JOB=test-worker
CLOUD_RUN_CAPTURE_JOB=test-capture
API_SERVICE_ACCOUNT=api@test.iam.gserviceaccount.com
WORKER_SERVICE_ACCOUNT=worker@test.iam.gserviceaccount.com
CAPTURE_SERVICE_ACCOUNT=capture@test.iam.gserviceaccount.com
SUPABASE_PROJECT_REF=abcdefghijklmnopqrst
SECRET_SUPABASE_URL=TEST_SUPABASE_URL
SECRET_SUPABASE_SERVICE_KEY=TEST_SUPABASE_KEY
SECRET_REDIS_URL=TEST_REDIS_URL
SECRET_REDIS_TOKEN=TEST_REDIS_TOKEN
SECRET_PROVIDER_API_KEY=TEST_PROVIDER_KEY
"""

STATES = [  # job present, API flag, expected
    ("1", "true", "PASS"),
    ("1", "false", "BLOCKED"),
    ("0", "false", "PASS"),
    ("0", "true", "BLOCKED"),
]


def preflight(tmp_path: Path, config: str = CONFIG, **state: str) -> dict[str, dict]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "gcloud").write_text(GCLOUD)
    (bin_dir / "gcloud").chmod(0o755)
    config_path = tmp_path / "operator.env"
    config_path.write_text(config)
    report = tmp_path / "report.json"
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path), **state}
    subprocess.run(["bash", str(PREFLIGHT), "--operator-config", str(config_path), "--json-output", str(report)],
                   capture_output=True, text=True, env=env, timeout=180, cwd=REPO)
    return {check["name"]: check for check in json.loads(report.read_text())["checks"]}


@pytest.mark.parametrize(("job", "flag", "expected"), STATES)
def test_the_preflight_reports_the_normalisation_job_against_its_flag(tmp_path, job, flag, expected):
    checks = preflight(tmp_path, T_JOB=job, T_FLAG=flag)
    check = checks["cloud-run:normalisation-job"]
    assert check["status"] == expected, check
    assert check["detail"].startswith(JOB + ": ")
    # The capture identity never reads the provider key, whatever the stage.
    assert checks["iam:capture-cannot-read-provider-key"]["status"] == "PASS"


def test_an_unreadable_job_listing_is_blocked_never_absent(tmp_path):
    check = preflight(tmp_path, T_LIST_FAIL="1")["cloud-run:normalisation-job"]
    assert check["status"] == "BLOCKED" and "could not be listed" in check["detail"]
    # Nor is an unreadable API flag ever "off".
    check = preflight(tmp_path / "api", T_JOB="0", T_API_FAIL="1")["cloud-run:normalisation-job"]
    assert check["status"] == "BLOCKED" and "could not be described" in check["detail"]


@pytest.mark.parametrize(("job", "flag", "expected"), STATES)
def test_a_capture_identity_that_reads_the_key_is_blocked_in_every_state(tmp_path, job, flag, expected):
    checks = preflight(tmp_path, T_JOB=job, T_FLAG=flag, T_CAPTURE_READS_KEY="1")
    assert checks["iam:capture-cannot-read-provider-key"]["status"] == "BLOCKED"
    assert checks["cloud-run:normalisation-job"]["status"] == expected


def test_the_stage_on_without_a_distinct_capture_identity_is_blocked(tmp_path):
    shared = CONFIG.replace("CAPTURE_SERVICE_ACCOUNT=capture@test.iam.gserviceaccount.com",
                            "CAPTURE_SERVICE_ACCOUNT=worker@test.iam.gserviceaccount.com")
    check = preflight(tmp_path, shared, T_JOB="1", T_FLAG="true")["cloud-run:normalisation-job"]
    assert check["status"] == "BLOCKED" and "no identity distinct from the worker" in check["detail"]
    off = preflight(tmp_path / "off", shared, T_JOB="0", T_FLAG="false")["cloud-run:normalisation-job"]
    assert off["status"] == "PASS"


def _verify(tmp_path: Path, job: str, flag: str, list_fails: bool = False,
            api_job_role: str = "roles/run.jobsExecutor"):
    tree = _verify_tree(tmp_path, readiness=READY, website=SITE_OFF, migration_detail=DB_OK)
    listing = "exit 1" if list_fails else (f"echo {JOB}" if job == "1" else "exit 0")
    described = json.dumps({"spec": {"template": {"spec": {"containers": [{"env": [
        {"name": FLAG, "value": flag}]}]}}}})
    api = "serviceAccount:api@test.iam.gserviceaccount.com"
    job_policy = json.dumps({"bindings": [{"role": api_job_role, "members": [api]}]})
    roles = "".join(f"  \"iam roles describe {role} --format=json\") printf '%s\\n' "
                    f"'{json.dumps({'includedPermissions': permissions})}' ;;\n"
                    for role, permissions in ROLES.items())
    source = tree.bin.joinpath("gcloud").read_text().replace(
        'case "$*" in\n',
        'case "$*" in\n'
        f'  "run jobs list --region test-region --project test-project --filter=metadata.name={JOB} '
        f'--format=value(metadata.name)") {listing} ;;\n'
        f"  *\"services describe\"*--format=json*) printf '%s\\n' '{described}' ;;\n"
        f'  "run jobs get-iam-policy {JOB} --region test-region --project test-project --format=json") '
        f"printf '%s\\n' '{job_policy}' ;;\n"
        f"  \"projects get-iam-policy test-project --format=json\") printf '%s\\n' '{{\"bindings\": []}}' ;;\n"
        + roles, 1)
    tree.tool("gcloud", source)
    return tree.run("production-verify.sh", "--gate", "deployed")


@pytest.mark.parametrize(("job", "flag", "expected"), STATES)
def test_the_deployed_gate_reads_the_normalisation_job_against_its_flag(tmp_path, job, flag, expected):
    result = _verify(tmp_path, job, flag)
    assert f"NORMALISATION_JOB={expected} ({JOB}: " in result.stdout, result.stdout
    assert _verdict(result.stdout)["CODE_DEPLOYED"] == ("VERIFIED" if expected == "PASS" else "NO")
    assert result.returncode == (0 if expected == "PASS" else 1)


@pytest.mark.parametrize(("role", "expected"), [("roles/run.jobsExecutor", "PASS"),
                                              ("roles/run.jobsExecutorWithOverrides", "BLOCKED"),
                                              ("roles/run.developer", "BLOCKED"),
                                              ("projects/test-project/roles/jobEditor", "BLOCKED")])
def test_the_deployed_gate_refuses_an_api_that_can_override_the_job(tmp_path, role, expected):
    """BLOCKER 1: while the job holds the provider key, an API that may run it
    with overrides could run `python -c ...` with the key: not deployed."""
    result = _verify(tmp_path, "1", "true", api_job_role=role)
    assert f"NORMALISATION_JOB_OVERRIDES={expected} (" in result.stdout, result.stdout
    assert _verdict(result.stdout)["CODE_DEPLOYED"] == ("VERIFIED" if expected == "PASS" else "NO")


def test_the_deployed_gate_cannot_prove_an_unlisted_job(tmp_path):
    result = _verify(tmp_path, "0", "false", list_fails=True)
    assert "NORMALISATION_JOB=UNVERIFIED (the Cloud Run jobs could not be listed)" in result.stdout
    assert _verdict(result.stdout)["CODE_DEPLOYED"] == "UNVERIFIED" and result.returncode == 1


def test_no_script_grants_the_capture_identity_a_secret_it_must_not_read():
    """The normalisation job runs as the worker identity: nothing that turns the
    stage on grants any identity a secret; bootstrap grants the capture identity
    the Supabase pair only."""
    for script in ("scripts/catalog/government-production-capture.sh",
                   "scripts/deploy/website-execution-activate.sh"):
        # Every command on one line (continuations joined), whitespace collapsed.
        text = " ".join(re.sub(r"\\\n", " ", (REPO / script).read_text()).split())
        assert "secrets add-iam-policy-binding" not in text, script
    ensure = (REPO / "scripts/catalog/government-production-capture.sh").read_text()
    body = ensure[ensure.index("ensure_normalisation_job() {"):]
    body = body[:body.index("\n}\n")]
    assert '--service-account "$worker_sa"' in body and "CAPTURE_SERVICE_ACCOUNT" not in body
    bootstrap = (REPO / "scripts/deploy/gcp-bootstrap.sh").read_text()
    capture_grants = re.findall(r'for key in ([A-Z_ ]+); do\n\s+bind_accessor "\$\(milo_op "\$key"\)" '
                                r'"\$\(milo_op CAPTURE_SERVICE_ACCOUNT\)"', bootstrap)
    assert capture_grants == ["SECRET_SUPABASE_URL SECRET_SUPABASE_SERVICE_KEY"]


# =============================================================================
# website-execution-activate.sh --apply / --remove-manufacturer-normalisation,
# with the REAL capture script, against a strict, stateful gcloud.
# =============================================================================

ACTIVATE_GCLOUD = r'''#!/usr/bin/env python3
"""gcloud with one project's Cloud Run jobs, API service and secret IAM,
remembered in $T_STATE. Anything it does not know fails loudly."""
import json, os, sys
ROLES = ''' + repr(ROLES) + r'''
a = sys.argv[1:]
with open(os.environ["OPS_TEST_CALLS"], "a") as log:
    log.write("gcloud " + " ".join(a) + "\n")
path = os.environ["T_STATE"]
state = json.load(open(path))
def save():
    json.dump(state, open(path, "w"))
def flag(name):
    for arg in a:
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
    return a[a.index(name) + 1] if name in a else None
def doc(entry):
    env = [{"name": k, "value": v} for k, v in entry.get("env", {}).items()]
    env += [{"name": k, "valueFrom": {"secretKeyRef": {"name": v, "key": "latest"}}}
            for k, v in entry.get("secrets", {}).items()]
    containers = [{"image": entry.get("image", ""), "env": env, "args": entry.get("args", []),
                   **({"command": [entry["command"]]} if entry.get("command") else {})}]
    return {"spec": {"template": {"spec": {"serviceAccountName": entry.get("sa", ""),
                                           "template": {"spec": {"containers": containers}},
                                           "containers": containers}}}}
def pairs(value):
    delim = ","
    if value.startswith("^"):
        delim, value = value[1], value[3:]
    return dict(p.split("=", 1) for p in value.split(delim) if p)
region = ["--region", "test-region", "--project", "test-project"]
if a[:2] == ["auth", "list"]: print("operator@example.test"); sys.exit(0)
if a[:3] == ["config", "get-value", "project"]: print("test-project"); sys.exit(0)
if a[:4] == ["artifacts", "docker", "tags", "list"]:
    tag = flag("--filter").split(":", 1)[1]
    package = a[4]
    print(f"{package}/tags/{tag}\t{package}/versions/sha256:" + "7" * 64); sys.exit(0)
if a[:3] == ["run", "services", "list"]:
    assert a[3:7] == region and a[8] == "--format=value(metadata.name)", a
    if os.environ.get("T_SERVICES_LIST_FAIL"): sys.exit(1)
    if a[7] == "--filter=metadata.name=test-api" and not os.environ.get("T_NO_API"): print("test-api")
    sys.exit(0)
if a[:2] == ["run", "services"]:
    assert a[3] == "test-api" and a[4:8] == region, a
    api = state["api"]
    if a[2] == "describe":
        print(json.dumps(doc(api))); sys.exit(0)
    if a[2] == "update":
        if os.environ.get("T_API_UPDATE_FAIL"): sys.exit(1)
        api["env"].update(pairs(flag("--update-env-vars"))); save(); sys.exit(0)
if a[:2] == ["run", "jobs"]:
    verb, jobs = a[2], state["jobs"]
    if verb == "list":
        assert a[3:7] == region and a[8] == "--format=value(metadata.name)", a
        if os.environ.get("T_LIST_FAIL"): sys.exit(1)
        name = a[7].split("=", 2)[2]
        if name in jobs: print(name)
        sys.exit(0)
    name = a[3]
    assert (flag("--region"), flag("--project")) == ("test-region", "test-project"), a
    if verb == "describe":
        if name not in jobs: sys.exit(1)
        print(json.dumps(doc(jobs[name]))); sys.exit(0)
    if verb in ("create", "update"):
        assert (verb == "create") == (name not in jobs), a
        secrets = {k: v.split(":")[0] for k, v in pairs(flag("--set-secrets")).items()}
        jobs[name] = {"image": flag("--image"), "sa": flag("--service-account"),
                      "env": pairs(flag("--set-env-vars")), "secrets": secrets, "iam": [],
                      "args": (flag("--args") or "").split(","), "command": flag("--command")}
        save(); sys.exit(0)
    if verb == "delete":
        assert a[4:] == region + ["--quiet"], a
        if not os.environ.get("T_DELETE_IGNORED"): jobs.pop(name)
        save(); sys.exit(0)
    if verb == "get-iam-policy":
        bindings = {}
        for role, member in jobs[name]["iam"]:
            bindings.setdefault(role, []).append(member)
        print(json.dumps({"bindings": [{"role": r, "members": m} for r, m in bindings.items()]})); sys.exit(0)
    if verb == "add-iam-policy-binding":
        entry = [flag("--role"), flag("--member")]
        # STRICT: nothing may let anyone run the key-holding job with overrides.
        assert not (name.endswith("-normalisation") and set(ROLES[entry[0]]) & {
            "run.jobs.runWithOverrides", "run.jobs.update", "run.jobs.create", "run.jobs.replace",
            "run.jobs.setIamPolicy"}), a
        if entry not in jobs[name]["iam"]: jobs[name]["iam"].append(entry)
        save(); sys.exit(0)
if a[:2] == ["projects", "get-iam-policy"]:
    assert a[2:] == ["test-project", "--format=json"], a
    bindings = [{"role": r, "members": [m]} for r, m in state.get("project_iam", [])]
    print(json.dumps({"bindings": bindings})); sys.exit(0)
if a[:3] == ["iam", "roles", "describe"]:
    assert a[4:] == ["--format=json"], a
    print(json.dumps({"name": a[3], "includedPermissions": ROLES[a[3]]})); sys.exit(0)
if a[:3] == ["secrets", "versions", "list"]:
    sys.exit(0)  # an optional secret (SENTRY_DSN) with no version: unbound
if a[:2] == ["secrets", "get-iam-policy"]:
    bindings = [{"role": r, "members": [m], **({"condition": {"title": "c"}} if c else {})}
                for r, m, c in state["secret_iam"].get(a[2], [])]
    print(json.dumps({"bindings": bindings})); sys.exit(0)
if a[:2] == ["secrets", "remove-iam-policy-binding"]:
    assert "--all" in a and flag("--role") == "roles/secretmanager.secretAccessor", a
    state["secret_iam"][a[2]] = [b for b in state["secret_iam"].get(a[2], [])
                                 if not (b[0] == flag("--role") and b[1] == flag("--member"))]
    save(); sys.exit(0)
with open(os.environ["OPS_TEST_CALLS"], "a") as log:
    log.write("UNMOCKED\n")
sys.stderr.write("UNMOCKED gcloud " + " ".join(a) + "\n"); sys.exit(2)
'''

WORKER_SA = "serviceAccount:worker@test-project.iam.gserviceaccount.com"
CAPTURE_MEMBER = "serviceAccount:capture@test-project.iam.gserviceaccount.com"
API_MEMBER = "serviceAccount:api@test-project.iam.gserviceaccount.com"


def activation(tmp_path: Path, *, capture_reads=(), capture_sa: str | None = None):
    from tests.test_ops_workflows import OpsTree

    tree = OpsTree(tmp_path)
    extra = "SECRET_REDIS_URL=UPSTASH_URL\nSECRET_REDIS_TOKEN=UPSTASH_TOKEN\n"
    config = tree.config.read_text() + extra
    if capture_sa is not None:
        config = re.sub(r"^CAPTURE_SERVICE_ACCOUNT=.*$", f"CAPTURE_SERVICE_ACCOUNT={capture_sa}", config, flags=re.M)
    tree.config.write_text(config)
    (tree.root / "scripts" / "deploy" / "production-verify.sh").write_text("exit 0\n")
    subprocess.run(["git", "-c", "user.email=t@example.test", "-c", "user.name=t", "commit", "-qam", "gate"],
                   cwd=tree.root, check=True)
    tree.tool("gcloud", ACTIVATE_GCLOUD)
    worker_env = {"MILO_ENABLE_PAID_EXECUTION": "false"}
    state = {
        "api": {"env": {"MILO_ENABLE_REGISTER_CAPTURE": "true", "CLOUD_RUN_CAPTURE_JOB": "test-capture",
                        "MILO_ENABLE_RUN_CREATION": "false",
                        "MILO_ENABLE_PAID_EXECUTION": "false"}},
        "jobs": {"test-worker": {"env": worker_env, "secrets": {}, "iam": []},
                 "test-capture": {"env": {}, "secrets": {"SUPABASE_URL": "SUPABASE_URL"}, "iam": []}},
        "secret_iam": {"KIMI_API_KEY": [["roles/secretmanager.secretAccessor", WORKER_SA, False]]
                       + [[role, CAPTURE_MEMBER, cond] for role, cond in capture_reads]},
    }
    path = tmp_path / "gcloud-state.json"
    path.write_text(json.dumps(state))
    return tree, {"T_STATE": str(path)}, path


def _run(tree, env, mode, **extra):
    return tree.run("../deploy/website-execution-activate.sh", f"--{mode}-manufacturer-normalisation",
                    extra_env={**env, **extra})


def test_apply_binds_the_key_on_the_normalisation_job_only_as_the_worker(tmp_path):
    tree, env, path = activation(tmp_path)
    result = _run(tree, env, "apply")
    assert result.returncode == 0, result.stdout + result.stderr
    state = json.loads(path.read_text())
    job = state["jobs"][JOB]
    assert job["sa"] == WORKER_SA.split(":", 1)[1]
    assert job["secrets"]["KIMI_API_KEY"] == "KIMI_API_KEY"
    assert {job["secrets"]["UPSTASH_REDIS_REST_URL"], job["secrets"]["UPSTASH_REDIS_REST_TOKEN"]} == {
        "UPSTASH_URL", "UPSTASH_TOKEN"}
    # The RuntimePolicy caps the one call runs under (the reviewed envelope: this worker has none).
    assert job["env"]["MILO_MAX_COST_PER_RUN"] == "3.00" and job["env"]["MILO_DAILY_USER_BUDGET"] == "10.00"
    assert job["env"]["MILO_ENABLE_PAID_EXECUTION"] == "false"
    # BLOCKER 1: the job's invocation is its definition -- the claim mode, its
    # switch baked on, no run id -- and the API may run it but never override it.
    assert job["env"]["MILO_ENABLE_MANUFACTURER_NORMALISATION_JOB"] == "true"
    assert job["args"][:2] == ["-m", "backend.catalog.operator_capture"]
    assert job["args"][-1] == "--normalisation-claim" and "--run-id" not in job["args"]
    assert ["roles/run.jobsExecutor", API_MEMBER] in job["iam"]
    assert ["roles/run.viewer", API_MEMBER] in job["iam"]
    assert all(not set(ROLES[role]) & set(REWRITE) for role, member in job["iam"] if member == API_MEMBER)
    assert "API_MEMBER holds no role carrying run.jobs.runWithOverrides".replace("API_MEMBER", API_MEMBER) \
        in result.stdout
    # No other job holds the key, and the capture identity reads nothing.
    assert "KIMI_API_KEY" not in state["jobs"]["test-capture"]["secrets"]
    assert [b for b in state["secret_iam"]["KIMI_API_KEY"] if b[1] == CAPTURE_MEMBER] == []
    assert state["api"]["env"][FLAG] == "true" and state["api"]["env"]["CLOUD_RUN_NORMALISATION_JOB"] == JOB
    assert state["api"]["env"]["MILO_ENABLE_PAID_EXECUTION"] == "false"
    assert "UNMOCKED" not in tree.tool_calls()


@pytest.mark.parametrize("role", ["roles/run.developer", "roles/editor"])
def test_apply_unwinds_when_the_api_could_override_the_job_through_the_project(tmp_path, role):
    tree, env, path = activation(tmp_path)
    state = json.loads(path.read_text())
    state["project_iam"] = [[role, API_MEMBER]]
    path.write_text(json.dumps(state))
    result = _run(tree, env, "apply")
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"{role} (project: " in result.stdout and "could run the normalisation job with overrides" in result.stderr
    after = json.loads(path.read_text())
    assert JOB not in after["jobs"] and after["api"]["env"].get(FLAG) != "true"


def test_apply_refuses_a_capture_identity_that_is_the_workers(tmp_path):
    tree, env, path = activation(tmp_path, capture_sa="worker@test-project.iam.gserviceaccount.com")
    result = _run(tree, env, "apply")
    assert result.returncode == 2 and "distinct from WORKER_SERVICE_ACCOUNT" in result.stderr
    assert JOB not in json.loads(path.read_text())["jobs"]
    assert not any("run jobs create" in c or "services update" in c for c in tree.tool_calls())


def test_a_failed_apply_removes_the_job_that_holds_the_key(tmp_path):
    tree, env, path = activation(tmp_path)
    result = _run(tree, env, "apply", T_API_UPDATE_FAIL="1")
    assert result.returncode == 1
    assert f"The normalisation job {JOB} was removed again (read back)." in result.stderr
    assert JOB not in json.loads(path.read_text())["jobs"]


def test_apply_revokes_a_capture_accessor_it_finds(tmp_path):
    tree, env, path = activation(tmp_path, capture_reads=[("roles/secretmanager.secretAccessor", True)])
    result = _run(tree, env, "apply")
    assert result.returncode == 0, result.stdout + result.stderr
    assert [b for b in json.loads(path.read_text())["secret_iam"]["KIMI_API_KEY"] if b[1] == CAPTURE_MEMBER] == []
    # Any other role is named and refused, never silently kept: the job is removed again.
    tree, env, path = activation(tmp_path / "owner", capture_reads=[("roles/secretmanager.admin", False)])
    result = _run(tree, env, "apply")
    assert result.returncode == 1 and "roles/secretmanager.admin" in result.stderr
    assert JOB not in json.loads(path.read_text())["jobs"]


def test_remove_deletes_the_job_even_when_the_api_update_fails(tmp_path):
    tree, env, path = activation(tmp_path)
    assert _run(tree, env, "apply").returncode == 0
    result = _run(tree, env, "remove", T_API_UPDATE_FAIL="1")
    assert result.returncode == 1 and "NOT fully removed" in result.stderr
    assert JOB not in json.loads(path.read_text())["jobs"]
    assert f"The normalisation job {JOB} is absent (read back)." in result.stdout
    removed = _run(tree, env, "remove")
    assert removed.returncode == 0, removed.stdout + removed.stderr
    assert json.loads(path.read_text())["api"]["env"][FLAG] == "false"


def test_remove_fails_while_the_job_survives_or_cannot_be_listed(tmp_path):
    tree, env, path = activation(tmp_path)
    assert _run(tree, env, "apply").returncode == 0
    for extra in ({"T_DELETE_IGNORED": "1"}, {"T_LIST_FAIL": "1"}):
        result = _run(tree, env, "remove", **extra)
        assert result.returncode == 1 and "is still there, or could not be listed" in result.stderr
    assert "UNMOCKED" not in tree.tool_calls()


# =============================================================================
# The pre-merge review: the capture identity's check fails CLOSED, covers the
# quota store too, and no runtime identity reaches secrets through the project;
# the API never holds run.jobs.runWithOverrides on the key-holding job.
# =============================================================================

CAPTURE_CHECK = "iam:capture-cannot-read-provider-key"


@pytest.mark.parametrize("secret", ["TEST_PROVIDER_KEY", "TEST_REDIS_URL", "TEST_REDIS_TOKEN"])
def test_an_unreadable_secret_policy_is_blocked_never_a_pass(tmp_path, secret):
    check = preflight(tmp_path, T_JOB="0", T_FLAG="false", T_POLICY_FAIL=secret)[CAPTURE_CHECK]
    assert check["status"] == "BLOCKED" and "could not be read" in check["detail"] and secret in check["detail"]


@pytest.mark.parametrize("secret", ["TEST_PROVIDER_KEY", "TEST_REDIS_URL", "TEST_REDIS_TOKEN"])
def test_a_conditional_capture_binding_on_the_key_or_the_quota_store_is_blocked(tmp_path, secret):
    check = preflight(tmp_path, T_JOB="0", T_FLAG="false", T_CAPTURE_CONDITIONAL=secret)[CAPTURE_CHECK]
    assert check["status"] == "BLOCKED", check
    assert f"{secret} (roles/secretmanager.secretAccessor conditional)" in check["detail"]
    assert "normalisation-off" in check["detail"]


def test_the_capture_check_passes_only_on_what_it_read(tmp_path):
    check = preflight(tmp_path, T_JOB="0", T_FLAG="false")[CAPTURE_CHECK]
    assert check["status"] == "PASS" and "quota store (read back)" in check["detail"]


@pytest.mark.parametrize(("roles", "expected"), [
    ("", "PASS"),
    ("capture:roles/secretmanager.secretAccessor", "BLOCKED"),
    ("api:roles/secretmanager.admin", "BLOCKED"),
    ("api:roles/editor", "BLOCKED"),
    ("capture:roles/owner", "BLOCKED"),
    ("worker:roles/editor", "PASS"),          # only the capture and API identities are held to it here
])
def test_no_runtime_identity_reaches_every_secret_through_the_project(tmp_path, roles, expected):
    check = preflight(tmp_path, T_JOB="0", T_FLAG="false", T_PROJECT_ROLES=roles)["iam:no-project-level-secret-access"]
    assert check["status"] == expected, check
    if expected == "BLOCKED":
        # The report redacts identities; the role it names is enough to act on.
        assert f"reach every secret: [REDACTED] {roles.split(':', 1)[1]}." in check["detail"]


def test_an_unreadable_project_policy_is_blocked(tmp_path):
    check = preflight(tmp_path, T_JOB="0", T_FLAG="false", T_PROJECT_FAIL="1")["iam:no-project-level-secret-access"]
    assert check["status"] == "BLOCKED" and "could not be read" in check["detail"]


@pytest.mark.parametrize(("state", "expected", "needle"), [
    ({}, "PASS", "holds no role carrying run.jobs.runWithOverrides"),
    ({"T_API_JOB_ROLE": "roles/run.jobsExecutorWithOverrides"}, "BLOCKED",
     "roles/run.jobsExecutorWithOverrides (job: run.jobs.runWithOverrides)"),
    ({"T_API_JOB_ROLE": "projects/test-project/roles/jobEditor"}, "BLOCKED",
     "projects/test-project/roles/jobEditor (job: run.jobs.update)"),
    ({"T_PROJECT_ROLES": "api:roles/run.developer"}, "BLOCKED", "roles/run.developer (project: "),
    ({"T_JOB_POLICY_FAIL": "1"}, "BLOCKED", "could not be read"),
    ({"T_ROLE_FAIL": "roles/run.jobsExecutor"}, "BLOCKED", "permissions of roles/run.jobsExecutor"),
])
def test_the_api_may_run_the_key_holding_job_but_never_override_it(tmp_path, state, expected, needle):
    check = preflight(tmp_path, T_JOB="1", T_FLAG="true", **state)["iam:api-cannot-override-normalisation-job"]
    assert check["status"] == expected and needle in check["detail"], check
    # No job, nothing holds the key: nothing to override.
    absent = preflight(tmp_path / "absent", T_JOB="0", T_FLAG="false", **state)
    assert absent["iam:api-cannot-override-normalisation-job"]["status"] == "PASS"


def test_a_job_left_behind_points_the_operator_at_normalisation_off(tmp_path):
    check = preflight(tmp_path, T_JOB="1", T_FLAG="false")["cloud-run:normalisation-job"]
    assert check["status"] == "BLOCKED"
    assert "Website stage -> stage = normalisation-off" in check["detail"]
    assert "website-stage.sh --stage normalisation-off" in check["detail"]


MINIMAL_DROP = ("MILO_WORKER_AUDIENCE", "MILO_GATEWAY_AUDIENCE", "MILO_APPROVED_GATEWAY_IDENTITIES",
                "PRODUCTION_ORIGIN", "SECRET_PROVIDER_API_KEY")


def test_the_removal_needs_no_activation_config_and_tolerates_no_api_service(tmp_path):
    """Turning normalisation OFF (deploy step 6, the kill switch's path, the
    normalisation-off stage) never fails for want of a gateway, worker or
    policy value; a project with no API service has no flag to turn off; a
    listing that fails is a failure."""
    tree, env, path = activation(tmp_path)
    assert _run(tree, env, "apply").returncode == 0
    config = tree.config.read_text()
    tree.config.write_text("".join(line + "\n" for line in config.splitlines()
                                   if not line.startswith(MINIMAL_DROP)))
    removed = _run(tree, env, "remove")
    assert removed.returncode == 0, removed.stdout + removed.stderr
    assert JOB not in json.loads(path.read_text())["jobs"]
    assert json.loads(path.read_text())["api"]["env"][FLAG] == "false"
    none = _run(tree, env, "remove", T_NO_API="1")
    assert none.returncode == 0 and "No API service test-api" in none.stdout
    unlisted = _run(tree, env, "remove", T_SERVICES_LIST_FAIL="1")
    assert unlisted.returncode == 1 and "could not be listed, so the API flag is not proved off" in unlisted.stderr
    assert "UNMOCKED" not in tree.tool_calls()

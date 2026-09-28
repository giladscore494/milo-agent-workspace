"""PR-Ops3: the build identity, the setup that reproduces it, and the deployer preflight.

What this proves, offline
-------------------------

1.  Both image builds run AS the dedicated build identity: cloud-run.sh passes
    ``--service-account projects/<project>/serviceAccounts/<CLOUD_BUILD_SERVICE_ACCOUNT>``
    to both ``gcloud builds submit`` calls, and both Cloud Build configs log to
    Cloud Logging only (required with a user-specified service account). The
    Compute Engine default service account, Cloud Build's legacy account or a
    malformed value is refused before anything is contacted.
2.  CLOUD_BUILD_SERVICE_ACCOUNT is read from the operator configuration, and
    the deploy paths (deploy.sh, production-activate.sh) fail clearly when it
    is missing or wrong; a direct Cloud Shell cloud-run.sh keeps working.
3.  setup-wif.sh --plan lists the new steps (the APIs, the deployer's
    project-level storage.bucketViewer, the build identity and its three
    bindings, the deployer's actAs on it) and never binds the Compute default
    service account -- a deployer binding found on it is planned as UNBIND.
4.  preflight-deployer.sh (deploy.yml ``preflight_as_deployer``) changes
    nothing: against a recording gcloud and curl it makes only read-only calls
    and testIamPermissions probes, reports EVERY gap at once, and never prints
    the access token.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.test_cloud_run_deploy_apply_mock import Deployment
from tests.test_ops_workflows import (
    CHANGE_LINE, COMPUTE_SA, DEPLOYER, OPERATOR_ENV, SENTINEL_FRAGMENTS, WIF_GCLOUD, OpsTree, steps,
    triggers, workflow)
from tests.test_scoped_rollout_contract import OPERATOR_ENV as ROLLOUT_ENV
from tests.test_scoped_rollout_contract import _orchestrator

REPO = Path(__file__).resolve().parents[1]
DEPLOY = REPO / "scripts" / "deploy"
CLOUD_RUN = (DEPLOY / "cloud-run.sh").read_text(encoding="utf-8")
BUILD_SA = "milo-cloudbuild@test-project.iam.gserviceaccount.com"
BUILD_LINE = f"CLOUD_BUILD_SERVICE_ACCOUNT={BUILD_SA}\n"
WRONG_BUILD_SAS = {
    "123456789-compute@developer.gserviceaccount.com": "Compute Engine default service account",
    "123456789@cloudbuild.gserviceaccount.com": "legacy default service account",
    "not-an-email": "not a user-managed service account email",
}


def without_build_sa(text: str) -> str:
    return "".join(line for line in text.splitlines(keepends=True)
                   if not line.startswith("CLOUD_BUILD_SERVICE_ACCOUNT="))


def with_build_sa(text: str, value: str) -> str:
    return without_build_sa(text) + f"CLOUD_BUILD_SERVICE_ACCOUNT={value}\n"


# =============================================================================
# 1. the builds run as the build identity, logging to Cloud Logging only
# =============================================================================

@pytest.mark.parametrize("config", ["cloudbuild-api.yaml", "cloudbuild-worker.yaml"])
def test_every_cloud_build_config_logs_to_cloud_logging_only(config):
    doc = yaml.safe_load((DEPLOY / config).read_text(encoding="utf-8"))
    assert doc["options"]["logging"] == "CLOUD_LOGGING_ONLY"
    assert "serviceAccount" not in doc, "the identity is passed by cloud-run.sh, from the operator config"


def test_both_build_submits_name_the_build_identity():
    submits = [line for line in CLOUD_RUN.splitlines() if "gcloud builds submit" in line]
    assert len(submits) == 1, "one submit helper, called for the worker and then the API"
    assert '--service-account "$BUILD_SERVICE_ACCOUNT_RESOURCE"' in submits[0]
    assert [line.split()[1] for line in CLOUD_RUN.splitlines() if line.startswith("run_build ")] == \
        ['"Worker"', '"API"']
    assert 'BUILD_SERVICE_ACCOUNT_RESOURCE="projects/$PROJECT_ID/serviceAccounts/$CLOUD_BUILD_SERVICE_ACCOUNT"' \
        in CLOUD_RUN
    # Validated with the other configuration guards, before anything is contacted.
    preflight = CLOUD_RUN.index("preflight() {")
    assert CLOUD_RUN.index("  require_build_service_account", preflight) < \
        CLOUD_RUN.index("gcloud auth list", preflight)


def test_the_apply_mode_builds_run_as_the_configured_build_identity(tmp_path):
    deployment = Deployment(tmp_path)
    result = deployment.run(CLOUD_BUILD_SERVICE_ACCOUNT=BUILD_SA)
    assert result.returncode == 0, result.stderr
    log = deployment.invocations()
    builds = [line for line in log if line.startswith("gcloud builds submit")]
    resource = f"--service-account projects/test-project/serviceAccounts/{BUILD_SA}"
    assert len(builds) == 2 and all(resource in line for line in builds)
    assert not [line for line in log if "compute@developer.gserviceaccount.com" in line]
    describe = next(i for i, line in enumerate(log) if f"iam service-accounts describe {BUILD_SA}" in line)
    assert describe < log.index(builds[0])
    assert f"Cloud Build service account: {BUILD_SA}" in result.stdout


def test_a_direct_cloud_shell_deploy_keeps_working_and_builds_as_milo_cloudbuild(tmp_path):
    """The owner's Cloud Shell path: no new variable, same command, same builds."""
    deployment = Deployment(tmp_path)
    result = deployment.run()
    assert result.returncode == 0, result.stderr
    builds = [line for line in deployment.invocations() if line.startswith("gcloud builds submit")]
    assert len(builds) == 2 and all(
        "serviceAccounts/milo-cloudbuild@big-cabinet-457321-t7.iam.gserviceaccount.com" in line
        for line in builds)


@pytest.mark.parametrize("value,message", list(WRONG_BUILD_SAS.items()))
def test_cloud_run_refuses_any_other_build_identity_before_contacting_anything(tmp_path, value, message):
    deployment = Deployment(tmp_path)
    result = deployment.run(CLOUD_BUILD_SERVICE_ACCOUNT=value)
    assert result.returncode != 0
    assert message in result.stderr
    assert deployment.invocations() == [], "nothing may be contacted with a wrong build identity"


def test_check_mode_also_requires_the_build_identity_to_exist(tmp_path):
    deployment = Deployment(tmp_path)
    result = deployment.run(DEPLOY_MODE="check", CLOUD_BUILD_SERVICE_ACCOUNT=BUILD_SA)
    assert result.returncode == 0, result.stderr
    assert any(f"iam service-accounts describe {BUILD_SA}" in line for line in deployment.invocations())
    assert not [line for line in deployment.invocations() if "builds submit" in line]


# =============================================================================
# 2. the operator configuration carries CLOUD_BUILD_SERVICE_ACCOUNT
# =============================================================================

def contract_check(value: str) -> subprocess.CompletedProcess:
    script = f'source {DEPLOY / "deployment-contract.sh"}; milo_build_service_account_problem "$1"'
    return subprocess.run(["bash", "-c", script, "check", value], capture_output=True, text=True)


def test_the_contract_accepts_only_a_user_managed_build_identity():
    assert contract_check(BUILD_SA).returncode == 0
    assert contract_check("milo-cloudbuild@big-cabinet-457321-t7.iam.gserviceaccount.com").returncode == 0
    missing = contract_check("")
    assert missing.returncode == 1 and "CLOUD_BUILD_SERVICE_ACCOUNT is not set" in missing.stdout
    for value, message in WRONG_BUILD_SAS.items():
        result = contract_check(value)
        assert result.returncode == 1 and message in result.stdout, value


def test_the_example_operator_configuration_names_the_build_identity():
    example = (REPO / "config" / "production-operator.env.example").read_text(encoding="utf-8")
    assert "CLOUD_BUILD_SERVICE_ACCOUNT=milo-cloudbuild@big-cabinet-457321-t7.iam.gserviceaccount.com\n" \
        in example


@pytest.mark.parametrize("value,message", [
    (None, "CLOUD_BUILD_SERVICE_ACCOUNT is not set"),
    *WRONG_BUILD_SAS.items(),
])
def test_the_deploy_workflow_refuses_a_configuration_without_the_build_identity(tmp_path, value, message):
    tree = OpsTree(tmp_path)
    tree.config.write_text(without_build_sa(OPERATOR_ENV) if value is None
                           else with_build_sa(OPERATOR_ENV, value), encoding="utf-8")
    result = tree.run("deploy.sh", "--sha", tree.sha, "--dry-run")
    assert result.returncode == 2
    assert message in result.stderr
    assert "SUMMARY|" not in result.stdout, "refused before the first step"
    assert tree.tool_calls() == []


@pytest.mark.parametrize("value,message", [
    (None, "the operator configuration has no CLOUD_BUILD_SERVICE_ACCOUNT"),
    *WRONG_BUILD_SAS.items(),
])
def test_production_activate_refuses_to_deploy_without_the_build_identity(tmp_path, value, message):
    tree = _orchestrator(tmp_path)
    tree.config.write_text(without_build_sa(ROLLOUT_ENV) if value is None
                           else with_build_sa(ROLLOUT_ENV, value), encoding="utf-8")
    result = tree.run("production-activate.sh", "--deploy", "--force-redeploy")
    assert result.returncode != 0
    assert message in result.stderr
    assert not any(call.startswith("cloud-run.sh") for call in tree.calls())


def test_production_activate_hands_the_build_identity_to_cloud_run(tmp_path):
    tree = _orchestrator(tmp_path)
    tree.stub("scripts/deploy/cloud-run.sh",
              'printf "cloud-run.sh BUILD_SA=%s\\n" "$CLOUD_BUILD_SERVICE_ACCOUNT" >> "$MILO_TEST_LOG"')
    result = tree.run("production-activate.sh", "--deploy", "--force-redeploy")
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"cloud-run.sh BUILD_SA={BUILD_SA}" in tree.calls()


def test_production_activate_refuses_a_shell_exporting_another_build_identity(tmp_path):
    tree = _orchestrator(tmp_path)
    result = tree.run("production-activate.sh", "--deploy", "--force-redeploy",
                      env={"CLOUD_BUILD_SERVICE_ACCOUNT": COMPUTE_SA})
    assert result.returncode != 0 and "Unset it" in result.stderr
    assert not any(call.startswith("cloud-run.sh") for call in tree.calls())


# =============================================================================
# 3. setup-wif.sh reproduces the build identity, never the Compute default SA
# =============================================================================

def wif_tree(tmp_path: Path, initial: dict | None = None) -> tuple[OpsTree, dict[str, str]]:
    tree = OpsTree(tmp_path)
    tree.tool("gcloud", WIF_GCLOUD)
    return tree, {"OPS_TEST_WIF_STATE": str(tmp_path / "wif.json"),
                  "OPS_TEST_WIF_INITIAL": json.dumps(initial or {})}


def test_setup_wif_plan_lists_every_new_step(tmp_path):
    tree, env = wif_tree(tmp_path)
    plan = tree.run("setup-wif.sh", "--plan", extra_env=env)
    assert plan.returncode == 0, plan.stdout + plan.stderr
    for api in ("cloudresourcemanager", "iam", "iamcredentials", "sts", "run", "cloudbuild",
                "artifactregistry", "serviceusage"):
        assert re.search(rf"^CREATE enable {api}\.googleapis\.com$", plan.stdout, re.M), api
    for line in (
        "BIND   project role roles/storage.bucketViewer -> milo-github-deployer@test-project.iam.gserviceaccount.com",
        f"CREATE build service account {BUILD_SA} (no key is ever created)",
        f"BIND   project role roles/logging.logWriter -> {BUILD_SA}",
        f"BIND   roles/artifactregistry.writer on repository test-repo only -> {BUILD_SA} (push both images)",
        f"BIND   roles/storage.objectViewer on gs://test-project_cloudbuild only -> {BUILD_SA} (read the uploaded source)",
        f"BIND   roles/iam.serviceAccountUser on {BUILD_SA} (that account only)",
        "CREATE build source bucket gs://test-project_cloudbuild (Cloud Build's default; uniform access)",
    ):
        assert line in plan.stdout, line
    changes = [line for line in plan.stdout.splitlines() if CHANGE_LINE.match(line)]
    assert not [line for line in changes if "compute@developer" in line], \
        "the Compute default service account is never bound"
    # The build identity is created before the deployer's actAs is bound on it.
    assert plan.stdout.index(f"CREATE build service account {BUILD_SA}") < \
        plan.stdout.index(f"roles/iam.serviceAccountUser on {BUILD_SA}")


def test_setup_wif_unbinds_the_deployer_from_the_compute_default_sa_and_never_binds_it(tmp_path):
    tree, env = wif_tree(tmp_path, {
        "sa_bindings": {COMPUTE_SA: [["roles/iam.serviceAccountUser", DEPLOYER],
                                     ["roles/iam.serviceAccountUser", "user:owner@example.test"]]},
        "project_bindings": [["roles/iam.serviceAccountUser", DEPLOYER]],
    })
    plan = tree.run("setup-wif.sh", "--plan", extra_env=env)
    assert plan.returncode == 0, plan.stdout + plan.stderr
    assert f"UNBIND roles/iam.serviceAccountUser on {COMPUTE_SA} (the Compute default SA) <- " \
           "milo-github-deployer@test-project.iam.gserviceaccount.com" in plan.stdout
    assert "UNBIND project-wide roles/iam.serviceAccountUser <- " \
           "milo-github-deployer@test-project.iam.gserviceaccount.com" in plan.stdout
    applied = tree.run("setup-wif.sh", "--apply", extra_env=env)
    assert applied.returncode == 0, applied.stdout + applied.stderr
    state = json.loads((tmp_path / "wif.json").read_text())
    # The deployer only: the owner's own binding on the Compute SA is left alone.
    assert state["sa_bindings"][COMPUTE_SA] == [["roles/iam.serviceAccountUser", "user:owner@example.test"]]
    assert ["roles/iam.serviceAccountUser", DEPLOYER] not in state["project_bindings"]
    assert not [call for call in tree.tool_calls()
                if COMPUTE_SA in call and "add-iam-policy-binding" in call]
    again = tree.run("setup-wif.sh", "--plan", extra_env=env)
    assert "PLAN: 0 change(s)" in again.stdout and CHANGE_LINE.findall(again.stdout) == []


def test_setup_wif_plans_nothing_on_the_project_as_the_owner_set_it_up(tmp_path):
    """The current project after the hand steps: --plan after --apply lists 0 changes."""
    tree, env = wif_tree(tmp_path)
    assert tree.run("setup-wif.sh", "--apply", extra_env=env).returncode == 0
    tree.calls.write_text("", encoding="utf-8")
    again = tree.run("setup-wif.sh", "--plan", extra_env=env)
    assert again.returncode == 0
    assert "PLAN: 0 change(s)" in again.stdout
    assert f"OK     build identity roles/artifactregistry.writer on repository test-repo" in again.stdout
    assert not [call for call in tree.tool_calls() if " add-iam-policy-binding " in call or " create " in call]


def test_setup_wif_reports_extra_roles_on_the_build_identity(tmp_path):
    tree, env = wif_tree(tmp_path, {"project_bindings": [["roles/editor", f"serviceAccount:{BUILD_SA}"]]})
    plan = tree.run("setup-wif.sh", "--plan", extra_env=env)
    assert plan.returncode == 0, plan.stderr
    assert f"WARN   {BUILD_SA} also holds project role(s) roles/editor" in plan.stdout


@pytest.mark.parametrize("value,message", [
    (None, "CLOUD_BUILD_SERVICE_ACCOUNT"),
    (COMPUTE_SA, "Compute Engine default service account"),
    ("milo-cloudbuild@other-project.iam.gserviceaccount.com", "must be a service account of test-project"),
])
def test_setup_wif_refuses_a_missing_or_foreign_build_identity(tmp_path, value, message):
    tree, env = wif_tree(tmp_path)
    tree.config.write_text(without_build_sa(OPERATOR_ENV) if value is None
                           else with_build_sa(OPERATOR_ENV, value), encoding="utf-8")
    result = tree.run("setup-wif.sh", "--plan", extra_env=env)
    assert result.returncode == 2
    assert message in result.stderr
    assert tree.tool_calls() == []


def test_setup_wif_apply_changes_nothing_without_the_image_repository(tmp_path):
    tree, env = wif_tree(tmp_path, {"repositories": []})
    result = tree.run("setup-wif.sh", "--apply", extra_env=env)
    assert result.returncode == 1
    assert "gcp-bootstrap.sh --apply first. Nothing was changed." in result.stderr
    assert not (tmp_path / "wif.json").exists(), "no mutation was made"


# =============================================================================
# 4. preflight_as_deployer: every read-only call, every gap at once, no change
# =============================================================================

ACCESS_TOKEN = "ya29.SENTINEL-ACCESS-TOKEN-4444"
ALL_APIS = ("cloudresourcemanager.googleapis.com iam.googleapis.com iamcredentials.googleapis.com "
            "sts.googleapis.com run.googleapis.com cloudbuild.googleapis.com "
            "artifactregistry.googleapis.com serviceusage.googleapis.com "
            "secretmanager.googleapis.com logging.googleapis.com").split()

# A recording gcloud that answers every read-only call; OPS_TEST_FAIL maps a
# call prefix to [exit status, stderr] for the ones a test makes fail.
PREFLIGHT_GCLOUD = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
joined = " ".join(args)
with open(os.environ["OPS_TEST_CALLS"], "a") as log:
    log.write("gcloud " + joined + "\n")
for prefix, (status, stderr) in json.loads(os.environ.get("OPS_TEST_FAIL") or "{}").items():
    if joined.startswith(prefix):
        sys.stderr.write(stderr + "\n"); sys.exit(status)
if args[:2] == ["auth", "list"]:
    print("milo-github-deployer@test-project.iam.gserviceaccount.com")
elif args[:2] == ["auth", "print-access-token"]:
    print(os.environ["OPS_TEST_TOKEN"])
elif args[:2] == ["config", "get-value"]:
    print("test-project")
elif args[:2] == ["projects", "describe"]:
    print("123456789" if "projectNumber" in joined else "test-project")
elif args[:2] == ["services", "list"]:
    print("\n".join(json.loads(os.environ["OPS_TEST_APIS"])))
elif args[:3] == ["storage", "buckets", "list"]:
    print("test-project_cloudbuild")
elif args[:3] == ["run", "jobs", "get-iam-policy"]:
    # OPS_TEST_POLICIES: {job: {role: [member, ...]}}; absent means no bindings.
    policy = json.loads(os.environ.get("OPS_TEST_POLICIES") or "{}").get(args[3], {})
    print(json.dumps({"bindings": [{"role": r, "members": m} for r, m in policy.items()]}))
elif any(verb in args[:5] for verb in ("describe", "list", "get-iam-policy", "read")):
    print("{}" if "--format=json" in args else "ok")
else:
    sys.stderr.write("unexpected gcloud " + joined + "\n"); sys.exit(90)
'''

# A recording curl: the Authorization header must arrive on stdin (-H @-); the
# answer is every requested permission, minus OPS_TEST_DENY[url-fragment],
# except that the Compute default SA answers with what OPS_TEST_COMPUTE holds.
PREFLIGHT_CURL = r'''#!/usr/bin/env python3
import json, os, sys, urllib.parse
args = sys.argv[1:]
header = sys.stdin.read()
url = next(arg for arg in args if arg.startswith("https://"))
with open(os.environ["OPS_TEST_CALLS"], "a") as log:
    log.write("curl " + " ".join(args) + " header-on-stdin="
              + str(header.strip() == "Authorization: Bearer " + os.environ["OPS_TEST_TOKEN"]) + "\n")
if "--data" in args:
    asked = json.loads(args[args.index("--data") + 1])["permissions"]
else:
    asked = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["permissions"]
if "-compute@developer.gserviceaccount.com" in url:
    held = json.loads(os.environ.get("OPS_TEST_COMPUTE") or "[]")
else:
    denied = set()
    for fragment, permissions in json.loads(os.environ.get("OPS_TEST_DENY") or "{}").items():
        if fragment in url:
            denied |= set(permissions)
    held = [permission for permission in asked if permission not in denied]
print(json.dumps({"permissions": held} if held else {}))
print("200", end="")
'''

READ_ONLY_CALL = re.compile(
    r"^gcloud (auth list|auth print-access-token|config get-value|projects describe|services list"
    r"|iam service-accounts describe|artifacts repositories describe|artifacts docker tags list"
    r"|secrets (describe|versions list|get-iam-policy)|run (services|jobs) (describe|get-iam-policy)"
    r"|run jobs executions list|storage buckets list|builds list|logging read) ")


def preflight_tree(tmp_path: Path) -> OpsTree:
    tree = OpsTree(tmp_path)
    tree.tool("gcloud", PREFLIGHT_GCLOUD)
    tree.tool("curl", PREFLIGHT_CURL)
    return tree


def run_preflight(tree: OpsTree, **env: object) -> subprocess.CompletedProcess:
    extra = {"OPS_TEST_TOKEN": ACCESS_TOKEN, "OPS_TEST_APIS": json.dumps(ALL_APIS)}
    extra.update({key: value if isinstance(value, str) else json.dumps(value) for key, value in env.items()})
    return tree.run("preflight-deployer.sh", "--sha", tree.sha, extra_env=extra)


def assert_changed_nothing_and_leaked_nothing(tree: OpsTree, result: subprocess.CompletedProcess) -> None:
    calls = tree.tool_calls()
    gcloud = [call for call in calls if call.startswith("gcloud ")]
    assert gcloud and all(READ_ONLY_CALL.match(call + " ") for call in gcloud), \
        [call for call in gcloud if not READ_ONLY_CALL.match(call + " ")]
    for verb in ("builds submit", " deploy ", " update ", " create ", "add-iam-policy-binding",
                 "remove-iam-policy-binding", "services enable", " execute ", " delete "):
        assert not [call for call in gcloud if verb in call], verb
    curls = [call for call in calls if call.startswith("curl ")]
    assert curls and all(re.search(r":testIamPermissions |/iam/testPermissions\?", call) for call in curls)
    assert all(call.endswith("header-on-stdin=True") for call in curls), "the token travels on stdin"
    assert not [call for call in calls if call.split()[0] not in ("gcloud", "curl")]
    shown = result.stdout + result.stderr + tree.summary.read_text(encoding="utf-8") + "\n".join(calls)
    assert ACCESS_TOKEN not in shown, "the access token was printed or put on a command line"
    for fragment in SENTINEL_FRAGMENTS:
        assert fragment not in shown


def test_a_clean_preflight_passes_and_changes_nothing(tmp_path):
    tree = preflight_tree(tmp_path)
    result = run_preflight(tree)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SUMMARY|preflight|PASS|" in result.stdout
    assert "SUMMARY|identity|PASS|running as milo-github-deployer@" in result.stdout
    assert_changed_nothing_and_leaked_nothing(tree, result)
    calls = tree.tool_calls()
    # The build submit and actAs permissions are probed, never exercised.
    project_probe = next(call for call in calls if "cloudresourcemanager.googleapis.com/v1/projects/"
                         "test-project:testIamPermissions" in call)
    assert "cloudbuild.builds.create" in project_probe and "storage.buckets.list" in project_probe
    for account in (BUILD_SA, "api@test-project", "worker@test-project", "capture@test-project",
                    COMPUTE_SA):
        assert any(f"serviceAccounts/{account}" in call and "iam.serviceAccounts.actAs" in call
                   for call in calls), account
    assert "SUMMARY|act-as:compute-default (must be refused)|PASS|does not hold iam.serviceAccounts.actAs" \
        in result.stdout
    # Every read-only call the deploy makes, including the ones a deploy run
    # first tripped over: projects describe, and the project bucket list.
    for needle in ("gcloud projects describe test-project", "gcloud services list --enabled",
                   f"gcloud iam service-accounts describe {BUILD_SA}",
                   "gcloud storage buckets list --project test-project",
                   "gcloud builds list", "gcloud logging read",
                   "gcloud run jobs executions list", "gcloud artifacts repositories describe test-repo"):
        assert any(call.startswith(needle) for call in calls), needle


def test_the_preflight_expects_the_api_identitys_job_bindings_and_never_makes_them(tmp_path):
    """PR-E'2: the website's Prepare GETs the capture job and the worker job, so
    the API identity's roles/run.viewer on each is on the preflight's expected
    list beside the executor binding. The deployer's run.admin makes them (the
    website stage, after the deploy); the preflight reads each job's policy,
    reports each binding and probes run.jobs.setIamPolicy on the job itself."""
    api = "serviceAccount:api@test-project.iam.gserviceaccount.com"
    tree = preflight_tree(tmp_path)
    result = run_preflight(tree, OPS_TEST_POLICIES={
        "test-worker": {"roles/run.jobsExecutorWithOverrides": [api], "roles/run.viewer": [api]},
        "test-capture": {"roles/run.jobsExecutorWithOverrides": [api]}})
    assert result.returncode == 0, result.stdout + result.stderr
    assert_changed_nothing_and_leaked_nothing(tree, result)
    out = result.stdout
    assert "SUMMARY|binding:worker-job:roles/run.viewer|PASS|" in out
    assert "SUMMARY|binding:worker-job:roles/run.jobsExecutorWithOverrides|PASS|" in out
    assert "SUMMARY|binding:capture-job:roles/run.jobsExecutorWithOverrides|PASS|" in out
    # Not bound yet: reported, never made -- the website stage binds it.
    assert re.search(r"^SUMMARY\|binding:capture-job:roles/run\.viewer\|INFO\|not bound yet on test-capture; "
                     r"website-stage\.sh --stage web-preparation binds it", out, re.M)
    calls = tree.tool_calls()
    for job in ("test-worker", "test-capture"):
        assert f"gcloud run jobs get-iam-policy {job} --region test-region --project test-project --format=json" \
            in calls
        probe = next(c for c in calls if f"/locations/test-region/jobs/{job}:testIamPermissions" in c)
        for permission in ("run.jobs.get", "run.jobs.getIamPolicy", "run.jobs.setIamPolicy"):
            assert permission in probe
        assert f"SUMMARY|permissions:job-iam:{job}|PASS|" in out


def test_the_preflight_names_a_deployer_that_cannot_bind_on_a_job(tmp_path):
    tree = preflight_tree(tmp_path)
    result = run_preflight(tree, OPS_TEST_DENY={"jobs/test-capture:testIamPermissions":
                                                ["run.jobs.setIamPolicy"]})
    assert result.returncode == 1
    report = result.stdout.split("== Preflight report ==", 1)[1]
    assert "run.jobs.setIamPolicy (permissions:job-iam:test-capture)" in report
    assert_changed_nothing_and_leaked_nothing(tree, result)


def test_the_preflight_does_not_probe_a_capture_job_that_does_not_exist_yet(tmp_path):
    tree = preflight_tree(tmp_path)
    result = run_preflight(tree, OPS_TEST_FAIL={"run jobs describe test-capture": [
        1, "ERROR: (gcloud.run.jobs.describe) Cannot find job [test-capture] NOT_FOUND"]})
    assert result.returncode == 0, result.stdout + result.stderr
    assert not [c for c in tree.tool_calls() if "jobs/test-capture:testIamPermissions" in c]
    assert any("jobs/test-worker:testIamPermissions" in c for c in tree.tool_calls())


def test_the_preflight_reports_every_gap_at_once(tmp_path):
    tree = preflight_tree(tmp_path)
    result = run_preflight(
        tree,
        OPS_TEST_APIS=[api for api in ALL_APIS if api not in ("cloudresourcemanager.googleapis.com",
                                                              "run.googleapis.com")],
        OPS_TEST_FAIL={
            "projects describe test-project --format=value(projectId)": [1, (
                "ERROR: (gcloud.projects.describe) PERMISSION_DENIED: Cloud Resource Manager API has not "
                "been used in project 123456789 before or it is disabled. Enable it by visiting "
                "https://console.developers.google.com/apis/api/cloudresourcemanager.googleapis.com/overview"
                "?project=123456789\n- '@type': type.googleapis.com/google.rpc.ErrorInfo\n  reason: "
                "SERVICE_DISABLED")],
            "storage buckets list": [1, (
                "ERROR: (gcloud.storage.buckets.list) HTTPError 403: milo-github-deployer@test-project."
                "iam.gserviceaccount.com does not have storage.buckets.list access to the Google Cloud "
                "project. Permission 'storage.buckets.list' denied on resource (or it may not exist).")],
            "run services describe test-api": [1, (
                "ERROR: (gcloud.run.services.describe) Cannot find service [test-api] NOT_FOUND")],
        },
        OPS_TEST_DENY={"projects/test-project:testIamPermissions": ["cloudbuild.builds.create"],
                       f"serviceAccounts/{BUILD_SA}": ["iam.serviceAccounts.actAs"]},
        OPS_TEST_COMPUTE=["iam.serviceAccounts.actAs"],
    )
    assert result.returncode == 1, result.stdout + result.stderr
    report = result.stdout.split("== Preflight report ==", 1)[1]
    for needle in ("MISSING API (2):", "cloudresourcemanager.googleapis.com", "run.googleapis.com",
                   "MISSING PERMISSION (4):", "storage.buckets.list (build-source-bucket-list)",
                   "cloudbuild.builds.create (permissions:project)",
                   "cloudbuild.builds.create (permissions:build-wait)",
                   "iam.serviceAccounts.actAs (act-as:milo-cloudbuild (build identity))",
                   "MISSING RESOURCE (1):", "api-service",
                   "MUST NOT HOLD (1):", "act-as:compute-default (must be refused): holds iam.serviceAccounts.actAs"):
        assert needle in report, needle
    assert "SUMMARY|preflight|FAIL|2 API(s)" in result.stdout
    # It never stopped at the first failure: the LAST read and the LAST probe ran.
    calls = tree.tool_calls()
    assert any(call.startswith("gcloud logging read") for call in calls)
    assert any("-compute@developer.gserviceaccount.com:testIamPermissions" in call for call in calls)
    assert_changed_nothing_and_leaked_nothing(tree, result)


def test_the_preflight_dry_run_calls_nothing(tmp_path):
    tree = OpsTree(tmp_path)
    result = tree.run("preflight-deployer.sh", "--sha", tree.sha, "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert tree.tool_calls() == []
    assert "DRY-RUN: gcloud storage buckets list --project test-project" in result.stdout
    assert "testIamPermissions: iam.serviceAccounts.actAs" in result.stdout
    assert "builds submit" not in result.stdout


def test_the_preflight_refuses_a_configuration_without_the_build_identity(tmp_path):
    tree = OpsTree(tmp_path)
    tree.config.write_text(without_build_sa(OPERATOR_ENV), encoding="utf-8")
    result = tree.run("preflight-deployer.sh", "--sha", tree.sha, "--dry-run")
    assert result.returncode == 2 and "CLOUD_BUILD_SERVICE_ACCOUNT" in result.stderr


def test_deploy_yml_offers_the_read_only_preflight_instead_of_the_deploy():
    doc = workflow("deploy.yml")
    option = triggers(doc)["workflow_dispatch"]["inputs"]["preflight_as_deployer"]
    assert option["type"] == "boolean" and option["default"] is False and option["required"] is False
    by_name = {step.get("name"): step for step in steps(doc)}
    preflight = by_name["Preflight as the deployer (read-only, deploys nothing)"]
    assert preflight["if"] == "${{ inputs.preflight_as_deployer }}"
    assert "bash scripts/ops/preflight-deployer.sh" in preflight["run"]
    assert "secrets." not in json.dumps(preflight), "the preflight needs no secret"
    assert by_name["Deploy"]["if"] == "${{ !inputs.preflight_as_deployer }}"
    names = list(by_name)
    assert names.index("Authenticate to Google Cloud (Workload Identity Federation, no key)") < \
        names.index("Preflight as the deployer (read-only, deploys nothing)") < names.index("Deploy")

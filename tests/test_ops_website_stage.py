"""PR-Ops2 B: the website's plan tools after a phone deploy.

A Stage A deploy turns off MILO_ENABLE_WORK_SCOPE_MUTATIONS (Stage P, plan
authoring) and MILO_ENABLE_WORK_SCOPE_PREPARATION_REQUESTS (E', the Prepare
button). What this proves, offline:

1.  website-stage.yml and deploy.yml's restore_website_stage input: shape,
    validation, and the scripts they run.
2.  website-stage.sh runs exactly the canonical tools, in order, per stage --
    dry run and applied -- and never Stage 2 (no --apply-backend, no runtime
    policy, no provider key). A stage it does not know is refused.
3.  deploy.sh restores the named stage ONLY after steps 1-10 passed: a deploy
    that fails at any step restores nothing; permanent mode restores nothing.
4.  deployed-release.sh finds the release production runs.
5.  No output or job summary of any of it contains a secret.
6.  setup-wif.sh already grants everything --ensure-job and the capture-job
    binding need, and nothing more.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from tests.test_ops_workflows import (
    OPS,
    SENTINEL_FRAGMENTS,
    WORKFLOWS,
    OpsTree,
    steps,
    triggers,
    workflow,
)

STAGES = ("plan-authoring", "web-preparation", "both")
RESTORE_VALUES = ("none", "plan-authoring", "web-preparation", "both", "register-capture", "catalog-browser",
                  "manufacturer-normalisation", "all")
BUCKET_LINE = "REGISTER_ARCHIVE_BUCKET=test-project-milo-register-archive\n"
# PR-D3: the normalisation stage binds the shared quota store on the capture job.
BUCKET_LINE += "SECRET_REDIS_URL=UPSTASH_REDIS_REST_URL\nSECRET_REDIS_TOKEN=UPSTASH_REDIS_REST_TOKEN\n"
STAGE2_MARKERS = ("--apply-backend", "--apply-runtime-policy", "--update-secrets", "KIMI_API_KEY=",
                  "MILO_ENABLE_PAID_EXECUTION=true", "MILO_ENABLE_RUN_CREATION=true", "arm.sh")


def assert_no_secret(tree: OpsTree, *outputs: str) -> None:
    shown = "".join(outputs) + (tree.summary.read_text(encoding="utf-8") if tree.summary.exists() else "")
    for fragment in SENTINEL_FRAGMENTS:
        assert fragment not in shown, f"a secret was printed: {fragment}"


def assert_no_stage2(text: str) -> None:
    for marker in STAGE2_MARKERS:
        assert marker not in text, f"Stage 2 was touched: {marker}"


# =============================================================================
# 1. the workflows
# =============================================================================

def test_website_stage_workflow_offers_exactly_the_eight_stages_and_validates_them():
    doc = workflow("website-stage.yml")
    inputs = triggers(doc)["workflow_dispatch"]["inputs"]
    assert inputs["stage"]["type"] == "choice"
    assert inputs["stage"]["options"] == ["both", "plan-authoring", "web-preparation", "register-capture",
                                         "catalog-browser", "manufacturer-normalisation", "normalisation-off",
                                         "all"]
    assert inputs["dry_run"]["type"] == "boolean" and inputs["dry_run"]["default"] is False
    assert inputs["sha"]["required"] is False
    first = steps(doc)[0]
    assert first["env"]["STAGE_INPUT"] == "${{ inputs.stage }}"
    assert ("plan-authoring | web-preparation | both | register-capture | catalog-browser | manufacturer-normalisation"
            " | normalisation-off | all) ;;") in first["run"]
    assert "refs/heads/main" in first["run"] and "^[0-9a-f]{40}$" in first["run"]
    assert doc["concurrency"] == {"group": "milo-production-operations", "cancel-in-progress": False}
    (job,) = doc["jobs"].values()
    assert job["environment"] == "production"


def test_website_stage_workflow_runs_from_the_deployed_release():
    names = [step.get("name", "") for step in steps(workflow("website-stage.yml"))]
    runs = {step.get("name", ""): step.get("run", "") for step in steps(workflow("website-stage.yml"))}
    release = names.index("The release production runs")
    checkout = names.index("Check out that release (a commit on main)")
    apply = names.index("Turn the website stage on")
    assert release < checkout < apply
    assert "bash scripts/ops/deployed-release.sh" in runs["The release production runs"]
    assert 'git merge-base --is-ancestor "${RELEASE_SHA}" origin/main' in runs[names[checkout]]
    assert 'git checkout --quiet --detach "${RELEASE_SHA}"' in runs[names[checkout]]
    assert "bash scripts/ops/website-stage.sh" in runs["Turn the website stage on"]
    apply_step = steps(workflow("website-stage.yml"))[apply]
    assert apply_step["env"]["MILO_READONLY_DB_URL"] == "${{ secrets.MILO_READONLY_DB_URL }}"


def test_deploy_offers_restore_website_stage_defaulting_to_both():
    doc = workflow("deploy.yml")
    restore = triggers(doc)["workflow_dispatch"]["inputs"]["restore_website_stage"]
    assert restore["type"] == "choice" and restore["default"] == "both"
    assert sorted(restore["options"]) == sorted(RESTORE_VALUES)
    (deploy,) = [step for step in steps(doc) if step.get("name") == "Deploy"]
    assert deploy["env"]["RESTORE_INPUT"] == "${{ inputs.restore_website_stage || 'both' }}"
    assert '--restore-website-stage "${RESTORE_INPUT}"' in deploy["run"]


def test_deploy_restores_nothing_after_a_failed_step_by_construction():
    """The restore is step 11 INSIDE deploy.sh (tested below); no workflow step
    after the deploy can run once it failed."""
    all_steps = steps(workflow("deploy.yml"))
    assert all_steps[-1]["name"] == "Deploy"
    for step in all_steps:
        assert "always()" not in str(step.get("if", "")) and "failure()" not in str(step.get("if", ""))
        assert step.get("continue-on-error") in (None, False)


# =============================================================================
# 2. website-stage.sh
# =============================================================================

@pytest.mark.parametrize("stage", STAGES)
def test_website_stage_dry_run_prints_the_canonical_tools_in_order_and_calls_nothing(tmp_path, stage):
    tree = OpsTree(tmp_path)
    result = tree.run("website-stage.sh", "--stage", stage, "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert tree.tool_calls() == [], "a dry run called an external tool"
    assert_no_secret(tree, result.stdout, result.stderr)
    assert_no_stage2(result.stdout + result.stderr)
    plan = [line for line in result.stdout.splitlines() if line.startswith("DRY-RUN:")]
    expected = {
        "plan-authoring": ["--apply-plan-authoring"],
        "web-preparation": ["--ensure-job --enable-catalog-execution", "--apply-web-preparation"],
        "both": ["--apply-plan-authoring", "--ensure-job --enable-catalog-execution",
                 "--apply-web-preparation"],
    }[stage]
    assert len(plan) == len(expected)
    for line, needle in zip(plan, expected):
        assert needle in line
        tool = "government-production-capture.sh" if "ensure-job" in needle else "website-execution-activate.sh"
        assert tool in line
    summary = re.findall(r"^SUMMARY\|([^|]+)\|DRY-RUN\|", result.stdout, re.M)
    assert summary == {"plan-authoring": ["1 plan-authoring"],
                       "web-preparation": ["2 capture-job", "3 web-preparation"],
                       "both": ["1 plan-authoring", "2 capture-job", "3 web-preparation"]}[stage]
    assert "| Step | Result | Detail |" in tree.summary.read_text(encoding="utf-8")


@pytest.mark.parametrize("bad", ["", "stage2", "armed", "BOTH", "both;true", "plan-authoring "])
def test_website_stage_refuses_an_unknown_stage(tmp_path, bad):
    tree = OpsTree(tmp_path)
    result = tree.run("website-stage.sh", "--stage", bad, "--dry-run")
    assert result.returncode == 2
    assert ("--stage must be plan-authoring, web-preparation, both, register-capture, catalog-browser, "
            "manufacturer-normalisation, normalisation-off, all or none" in result.stderr)
    assert tree.tool_calls() == []
    assert tree.run("website-stage.sh", "--dry-run").returncode == 2


def test_normalisation_off_is_its_own_stage_through_the_removal_path(tmp_path):
    """PR-D3 review item 6: normalisation can be turned off alone -- no deploy,
    no kill switch -- through the path that deletes the job and revokes the
    capture identity's access, each read back."""
    tree = OpsTree(tmp_path)
    result = tree.run("website-stage.sh", "--stage", "normalisation-off", "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert tree.tool_calls() == []
    (plan,) = [line for line in result.stdout.splitlines() if line.startswith("DRY-RUN:")]
    assert plan.endswith("--remove-manufacturer-normalisation")
    assert re.findall(r"^SUMMARY\|([^|]+)\|DRY-RUN\|", result.stdout, re.M) == ["1 normalisation-off"]


def test_website_stage_all_is_both_then_the_register_page_then_the_catalog_browser(tmp_path):
    tree = OpsTree(tmp_path)
    assert tree.run("website-stage.sh", "--stage", "all", "--dry-run").returncode == 2, \
        "all ran without the register archive bucket"
    tree.config.write_text(tree.config.read_text() + BUCKET_LINE)
    result = tree.run("website-stage.sh", "--stage", "all", "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert tree.tool_calls() == []
    plan = [line for line in result.stdout.splitlines() if line.startswith("DRY-RUN:")]
    needles = ["--apply-plan-authoring", "--ensure-job --enable-catalog-execution", "--apply-web-preparation",
               "--apply-register-capture", "--apply-catalog-browser"]
    assert len(plan) == len(needles) and all(n in line for line, n in zip(plan, needles))
    assert re.findall(r"^SUMMARY\|([^|]+)\|DRY-RUN\|", result.stdout, re.M) == [
        "1 plan-authoring", "2 capture-job", "3 web-preparation", "4 register-capture", "5 catalog-browser"]
    assert_no_stage2(result.stdout + result.stderr)
    assert_no_secret(tree, result.stdout, result.stderr)


def test_website_stage_none_changes_nothing(tmp_path):
    tree = OpsTree(tmp_path)
    result = tree.run("website-stage.sh", "--stage", "none")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SUMMARY|1 website-stage|SKIPPED|" in result.stdout
    assert tree.tool_calls() == []


def test_web_preparation_needs_the_capture_job_in_the_operator_configuration(tmp_path):
    tree = OpsTree(tmp_path)
    tree.config.write_text(tree.config.read_text().replace("CLOUD_RUN_CAPTURE_JOB=test-capture\n", ""))
    result = tree.run("website-stage.sh", "--stage", "web-preparation", "--dry-run")
    assert result.returncode == 2
    assert "CLOUD_RUN_CAPTURE_JOB" in result.stdout + result.stderr
    # Plan authoring alone does not need it.
    assert tree.run("website-stage.sh", "--stage", "plan-authoring", "--dry-run").returncode == 0


# =============================================================================
# 3. an applied deploy, end to end, against stand-ins
# =============================================================================

STUB = """#!/usr/bin/env bash
printf 'stub %s %s\\n' "$(basename "$0")" "$*" >> "$OPS_TEST_CALLS"
{extra}
exit "${{{status}:-0}}"
"""

DEPLOY_GCLOUD = r'''#!/usr/bin/env python3
"""gcloud with one worker job and one API service whose env it remembers."""
import json, os, sys
args = sys.argv[1:]
with open(os.environ["OPS_TEST_CALLS"], "a") as log:
    log.write("gcloud " + " ".join(args) + "\n")
path = os.environ["OPS_TEST_GCLOUD_STATE"]
state = json.load(open(path)) if os.path.exists(path) else {
    "jobs": {"test-worker": {"MILO_CAPTURE_REPLAY": "false"}},
    "services": {"test-api": {}}}
def doc(env):
    containers = [{"env": [{"name": k, "value": v} for k, v in env.items()]}]
    return {"spec": {"template": {"spec": {"template": {"spec": {"containers": containers}},
                                           "containers": containers}}}}
if args[:2] == ["auth", "list"]:
    print("operator@example.test"); sys.exit(0)
if args[:3] == ["config", "get-value", "project"]:
    print("test-project"); sys.exit(0)
if args[:1] == ["run"] and args[2] == "describe":
    print(json.dumps(doc(state[args[1]].get(args[3], {})))); sys.exit(0)
if args[:1] == ["run"] and args[2] == "update":
    env = state[args[1]].setdefault(args[3], {})
    for index, arg in enumerate(args):
        if arg == "--update-env-vars":
            value = args[index + 1]
            if value.startswith("^"):
                delimiter, value = value[1], value[3:]
            else:
                delimiter = ","
            for pair in value.split(delimiter):
                name, _, val = pair.partition("=")
                env[name] = val
    json.dump(state, open(path, "w")); sys.exit(0)
if args[:3] == ["run", "jobs", "list"] and args[3:7] == ["--region", "test-region", "--project", "test-project"] \
        and len(args) == 9 and args[7].startswith("--filter=metadata.name=") \
        and args[8] == "--format=value(metadata.name)":
    if os.environ.get("OPS_TEST_JOBS_LIST_EXIT"):
        sys.exit(int(os.environ["OPS_TEST_JOBS_LIST_EXIT"]))
    name = args[7].split("=", 2)[2]
    if name in state["jobs"]:
        print(name)
    sys.exit(0)
if args[:3] == ["run", "jobs", "delete"] and args[4:] == ["--region", "test-region", "--project", "test-project",
                                                          "--quiet"]:
    if not os.environ.get("OPS_TEST_DELETE_IGNORED"):
        state["jobs"].pop(args[3])
    json.dump(state, open(path, "w")); sys.exit(0)
if args[:2] == ["secrets", "get-iam-policy"] and args[3:] == ["--project", "test-project", "--format=json"]:
    if os.environ.get("OPS_TEST_SECRET_POLICY_EXIT"):
        sys.exit(int(os.environ["OPS_TEST_SECRET_POLICY_EXIT"]))
    bindings = [{"role": r, "members": [m]} for r, m in state.get("secret_iam", {}).get(args[2], [])]
    print(json.dumps({"bindings": bindings})); sys.exit(0)
if args[:2] == ["secrets", "remove-iam-policy-binding"]:
    assert "--all" in args and args[3:5] == ["--project", "test-project"], args
    member, role = args[args.index("--member") + 1], args[args.index("--role") + 1]
    if not os.environ.get("OPS_TEST_REVOKE_IGNORED"):
        state.setdefault("secret_iam", {})[args[2]] = [
            b for b in state.get("secret_iam", {}).get(args[2], []) if b != [role, member]]
    json.dump(state, open(path, "w")); sys.exit(0)
sys.stderr.write("unmocked gcloud " + " ".join(args) + "\n"); sys.exit(2)
'''


class ChecksApi(BaseHTTPRequestHandler):
    """GitHub's check-runs endpoint: the four mandatory CI jobs, green."""

    def do_GET(self):  # noqa: N802 -- the http.server API
        body = json.dumps({"check_runs": [
            {"id": index, "name": name, "status": "completed", "conclusion": "success"}
            for index, name in enumerate(("offline-checks", "frontend-and-docker", "postgres-checks", "e2e"))
        ]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


@pytest.fixture()
def checks_api():
    server = HTTPServer(("127.0.0.1", 0), ChecksApi)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def deploy_tree(tmp_path: Path, api_url: str) -> tuple[OpsTree, dict[str, str]]:
    """A release whose canonical tools are stand-ins that log their calls."""
    tree = OpsTree(tmp_path)
    tree.config.write_text(tree.config.read_text() + BUCKET_LINE)
    stubs = {
        "scripts/release/check-migration-state.sh":
            "echo '[PASS] remote: remote schema classified as fully-migrated (100/100 migrations)'",
        "scripts/deploy/website-execution-check.sh":
            "echo FRONTEND_RELEASE=VERIFIED; echo GATEWAY_RUN_START_ENABLED=DISABLED",
        "scripts/deploy/production-activate.sh": "",
        "scripts/deploy/production-verify.sh": "",
        "scripts/deploy/website-execution-activate.sh": "",
        "scripts/catalog/government-production-capture.sh": "",
    }
    for relative, extra in stubs.items():
        status = "OPS_TEST_STATUS_" + re.sub(r"[^A-Z]", "_", Path(relative).stem.upper())
        (tree.root / relative).write_text(STUB.format(extra=extra, status=status), encoding="utf-8")
    subprocess.run(["git", "-c", "user.email=t@example.test", "-c", "user.name=t", "commit", "-qam",
                    "stand-ins"], cwd=tree.root, check=True)
    tree.sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tree.root, check=True,
                              capture_output=True, text=True).stdout.strip()
    tree.tool("gcloud", DEPLOY_GCLOUD)
    tree.tool("psql", "#!/usr/bin/env bash\nprintf 'psql\\n' >> \"$OPS_TEST_CALLS\"\necho 0\n")
    env = {"OPS_TEST_GCLOUD_STATE": str(tmp_path / "gcloud.json"), "GITHUB_API_URL": api_url,
           "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}
    return tree, env


def stub_calls(tree: OpsTree) -> list[str]:
    return [line[len("stub "):] for line in tree.tool_calls() if line.startswith("stub ")]


def restore_calls(tree: OpsTree) -> list[str]:
    """The website stage the deploy restores (step 11); step 6's removal of
    manufacturer normalisation is a reset, not a restore."""
    return [call for call in stub_calls(tree)
            if call.startswith(("website-execution-activate.sh", "government-production-capture.sh"))
            and not call.endswith("--remove-manufacturer-normalisation")]


@pytest.mark.parametrize("restore", RESTORE_VALUES)
def test_a_successful_deploy_restores_exactly_the_named_stage_after_the_deployed_gate(
        tmp_path, checks_api, restore):
    tree, env = deploy_tree(tmp_path, checks_api)
    result = tree.run("deploy.sh", "--sha", tree.sha, "--restore-website-stage", restore, extra_env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert_no_secret(tree, result.stdout, result.stderr)
    calls = stub_calls(tree)
    gate = next(index for index, call in enumerate(calls) if call.startswith("production-verify.sh"))
    expected = {
        "none": [],
        "plan-authoring": ["website-execution-activate.sh --operator-config {c} --apply-plan-authoring"],
        "web-preparation": [
            "government-production-capture.sh --operator-config {c} --ensure-job --enable-catalog-execution",
            "website-execution-activate.sh --operator-config {c} --apply-web-preparation"],
        "both": [
            "website-execution-activate.sh --operator-config {c} --apply-plan-authoring",
            "government-production-capture.sh --operator-config {c} --ensure-job --enable-catalog-execution",
            "website-execution-activate.sh --operator-config {c} --apply-web-preparation"],
        "register-capture": [
            "government-production-capture.sh --operator-config {c} --ensure-job --enable-catalog-execution",
            "website-execution-activate.sh --operator-config {c} --apply-register-capture"],
        "catalog-browser": ["website-execution-activate.sh --operator-config {c} --apply-catalog-browser"],
        "manufacturer-normalisation": [
            "government-production-capture.sh --operator-config {c} --ensure-job --enable-catalog-execution",
            "website-execution-activate.sh --operator-config {c} --apply-register-capture",
            "website-execution-activate.sh --operator-config {c} --apply-manufacturer-normalisation"],
        "all": [
            "website-execution-activate.sh --operator-config {c} --apply-plan-authoring",
            "government-production-capture.sh --operator-config {c} --ensure-job --enable-catalog-execution",
            "website-execution-activate.sh --operator-config {c} --apply-web-preparation",
            "website-execution-activate.sh --operator-config {c} --apply-register-capture",
            "website-execution-activate.sh --operator-config {c} --apply-catalog-browser"],
    }[restore]
    assert restore_calls(tree) == [line.format(c=tree.config) for line in expected]
    # Every restore call comes after the deployed gate passed.
    assert all(calls.index(call) > gate for call in restore_calls(tree))
    assert_no_stage2(result.stdout + "\n".join(tree.tool_calls()))
    summary = re.findall(r"^SUMMARY\|(\d+[a-d]?) [^|]+\|([A-Z-]+)\|", result.stdout, re.M)
    assert [step for step, _ in summary][:10] == [str(n) for n in range(1, 11)]
    assert all(outcome in ("PASS", "SKIPPED") for _, outcome in summary)
    if restore == "none":
        assert "SUMMARY|11 website-stage|SKIPPED|restore_website_stage=none" in result.stdout
    else:
        steps_passed = set(re.findall(r"^SUMMARY\|(11[a-e] [^|]+)\|PASS\|", result.stdout, re.M))
        assert steps_passed == {
            "plan-authoring": {"11a plan-authoring"},
            "web-preparation": {"11b capture-job", "11c web-preparation"},
            "both": {"11a plan-authoring", "11b capture-job", "11c web-preparation"},
            "register-capture": {"11b capture-job", "11c register-capture"},
            "catalog-browser": {"11a catalog-browser"},
            "manufacturer-normalisation": {"11a capture-job", "11b register-capture",
                                           "11c manufacturer-normalisation"},
            "all": {"11a plan-authoring", "11b capture-job", "11c web-preparation", "11d register-capture",
                    "11e catalog-browser"},
        }[restore]


@pytest.mark.parametrize("failing,step", [
    ("OPS_TEST_STATUS_PRODUCTION_ACTIVATE", "7 activate"),
    ("OPS_TEST_STATUS_PRODUCTION_VERIFY", "10 deployed-gate"),
])
def test_a_failed_deploy_restores_nothing(tmp_path, checks_api, failing, step):
    tree, env = deploy_tree(tmp_path, checks_api)
    result = tree.run("deploy.sh", "--sha", tree.sha, "--restore-website-stage", "both",
                      extra_env={**env, failing: "1"})
    assert result.returncode == 1
    assert f"SUMMARY|{step}|FAIL|" in result.stdout
    assert restore_calls(tree) == [], "a failed deploy restored the website stage"
    assert "SUMMARY|11" not in result.stdout
    assert_no_secret(tree, result.stdout, result.stderr)


def test_a_deploy_that_fails_before_it_starts_restores_nothing(tmp_path, checks_api):
    tree, env = deploy_tree(tmp_path, checks_api)
    result = tree.run("deploy.sh", "--sha", "0" * 40, "--restore-website-stage", "both", extra_env=env)
    assert result.returncode == 1
    assert restore_calls(tree) == [] and "SUMMARY|11" not in result.stdout


def test_permanent_mode_restores_nothing(tmp_path, checks_api):
    tree, env = deploy_tree(tmp_path, checks_api)
    result = tree.run("deploy.sh", "--sha", tree.sha, "--permanent-mode", "true",
                      "--restore-website-stage", "both", extra_env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert restore_calls(tree) == []
    assert "SUMMARY|11 website-stage|SKIPPED|permanent operating mode" in result.stdout


def test_a_failed_restore_fails_the_deploy_run_but_says_the_release_is_deployed(tmp_path, checks_api):
    tree, env = deploy_tree(tmp_path, checks_api)
    result = tree.run("deploy.sh", "--sha", tree.sha, "--restore-website-stage", "both",
                      extra_env={**env, "OPS_TEST_STATUS_GOVERNMENT_PRODUCTION_CAPTURE": "1"})
    assert result.returncode == 1
    assert "SUMMARY|10 deployed-gate|PASS|" in result.stdout
    assert "SUMMARY|11b capture-job|FAIL|" in result.stdout
    # E' is never applied over a capture job that was not ensured.
    assert not [call for call in restore_calls(tree) if "--apply-web-preparation" in call]


# =============================================================================
# the deploy dry run, with every restore value
# =============================================================================

@pytest.mark.parametrize("restore", ("register-capture", "manufacturer-normalisation", "all"))
def test_a_deploy_restoring_the_register_page_without_its_bucket_refuses_before_it_starts(tmp_path, restore):
    tree = OpsTree(tmp_path)
    result = tree.run("deploy.sh", "--sha", tree.sha, "--restore-website-stage", restore, "--dry-run")
    assert result.returncode == 2 and "REGISTER_ARCHIVE_BUCKET" in result.stderr
    assert tree.tool_calls() == [] and "SUMMARY|1 " not in result.stdout


@pytest.mark.parametrize("restore", RESTORE_VALUES)
def test_deploy_dry_run_with_each_restore_value(tmp_path, restore):
    tree = OpsTree(tmp_path)
    tree.config.write_text(tree.config.read_text() + BUCKET_LINE)
    result = tree.run("deploy.sh", "--sha", tree.sha, "--restore-website-stage", restore, "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert tree.tool_calls() == []
    assert_no_secret(tree, result.stdout, result.stderr)
    after_gate = result.stdout.split("SUMMARY|10 deployed-gate|DRY-RUN|", 1)[1]
    assert_no_stage2(after_gate)
    assert ("--apply-plan-authoring" in after_gate) == (restore in ("plan-authoring", "both", "all"))
    assert after_gate.count("--ensure-job --enable-catalog-execution") == (
        restore not in ("none", "plan-authoring", "catalog-browser"))
    assert ("--apply-web-preparation" in after_gate) == (restore in ("web-preparation", "both", "all"))
    assert ("--apply-register-capture" in after_gate) == (
        restore in ("register-capture", "manufacturer-normalisation", "all"))
    assert ("--apply-catalog-browser" in after_gate) == (restore in ("catalog-browser", "all"))
    # Never part of `all`: it binds a paid provider key.
    assert ("--apply-manufacturer-normalisation" in after_gate) == (restore == "manufacturer-normalisation")
    if restore == "none":
        assert "SUMMARY|11 website-stage|SKIPPED|" in result.stdout
    assert result.stdout.count("DRY RUN: nothing was called") == 1


def test_deploy_dry_run_in_permanent_mode_restores_nothing(tmp_path):
    tree = OpsTree(tmp_path)
    result = tree.run("deploy.sh", "--sha", tree.sha, "--permanent-mode", "true",
                      "--restore-website-stage", "both", "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SUMMARY|11 website-stage|SKIPPED|permanent operating mode" in result.stdout
    assert "--apply-plan-authoring" not in result.stdout and "--ensure-job" not in result.stdout


@pytest.mark.parametrize("bad", ["", "stage2", "everything", "Both", "all "])
def test_deploy_refuses_an_unknown_restore_value(tmp_path, bad):
    tree = OpsTree(tmp_path)
    result = tree.run("deploy.sh", "--sha", tree.sha, "--restore-website-stage", bad, "--dry-run")
    assert result.returncode == 2
    assert ("--restore-website-stage must be none, plan-authoring, web-preparation, both, register-capture,"
            " catalog-browser, manufacturer-normalisation or all") in result.stderr
    assert tree.tool_calls() == []


# =============================================================================
# 4. the release production runs
# =============================================================================

def test_deployed_release_dry_run_calls_nothing_and_answers_head(tmp_path):
    tree = OpsTree(tmp_path)
    output = tmp_path / "github_output"
    result = tree.run("deployed-release.sh", "--sha", "", "--dry-run",
                      extra_env={"GITHUB_OUTPUT": str(output)})
    assert result.returncode == 0, result.stdout + result.stderr
    assert tree.tool_calls() == []
    assert output.read_text() == f"sha={tree.sha}\n"
    assert_no_secret(tree, result.stdout, result.stderr)


def test_deployed_release_reads_the_api_and_the_worker_and_requires_them_to_agree(tmp_path):
    tree = OpsTree(tmp_path)
    tree.tool("gcloud", DEPLOY_GCLOUD)
    state = tmp_path / "gcloud.json"
    release = "a" * 40

    def world(api: str, worker: str) -> None:
        state.write_text(json.dumps({"jobs": {"test-worker": {"MILO_RELEASE_SHA": worker}},
                                     "services": {"test-api": {"MILO_RELEASE_SHA": api}}}))

    output = tmp_path / "github_output"
    env = {"OPS_TEST_GCLOUD_STATE": str(state), "GITHUB_OUTPUT": str(output)}
    world(release, release)
    result = tree.run("deployed-release.sh", extra_env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert output.read_text() == f"sha={release}\n"
    assert not [call for call in tree.tool_calls() if " update " in call]

    output.write_text("")
    world(release, "b" * 40)
    mismatch = tree.run("deployed-release.sh", extra_env=env)
    assert mismatch.returncode == 1 and "SUMMARY|0 release|FAIL|" in mismatch.stdout
    assert output.read_text() == ""

    given = tree.run("deployed-release.sh", "--sha", "c" * 40, extra_env=env)
    assert given.returncode == 0 and output.read_text() == f"sha={'c' * 40}\n"
    assert tree.run("deployed-release.sh", "--sha", "abc", extra_env=env).returncode == 2


# =============================================================================
# 6. the deployer's permissions
# =============================================================================

def test_setup_wif_covers_the_capture_job_identity_with_or_without_its_own_account():
    """--ensure-job deploys the capture job AS CAPTURE_SERVICE_ACCOUNT, or the
    worker identity when none is configured; both are in the actAs list, and
    run.admin (jobs create/update, jobs setIamPolicy) and artifactregistry.reader
    (the worker image's exact tag, docker tags list) are the only project roles it uses."""
    capture = (Path(__file__).resolve().parents[1] / "scripts" / "catalog"
               / "government-production-capture.sh").read_text(encoding="utf-8")
    assert '[[ -n "$CAPTURE_SA" ]] || CAPTURE_SA="$(milo_op WORKER_SERVICE_ACCOUNT)"' in capture
    gcloud_verbs = set(re.findall(r"gcloud ((?:run jobs|artifacts docker [a-z]+|run services) [a-z-]+)", capture))
    assert gcloud_verbs <= {"run jobs create", "run jobs update", "run jobs describe", "run jobs execute",
                            "run jobs add-iam-policy-binding", "run jobs executions",
                            "artifacts docker tags list"}, gcloud_verbs
    # The image check is the shared tags-list lookup (artifactregistry.reader).
    assert 'milo_image_digest_lookup "$WORKER_IMAGE"' in capture
    wif = (OPS / "setup-wif.sh").read_text(encoding="utf-8")
    assert 'ACT_AS=("$(milo_op API_SERVICE_ACCOUNT)" "$(milo_op WORKER_SERVICE_ACCOUNT)")' in wif
    assert '[[ -n "$CAPTURE_SA" ]] && ACT_AS+=("$CAPTURE_SA")' in wif
    assert '"roles/run.admin"' in wif and '"roles/artifactregistry.reader"' in wif



# =============================================================================
# PR-D3: the Stage A reset deletes the normalisation job and reads it back
# =============================================================================

NORMALISATION_JOB = "test-capture-normalisation"
CAPTURE_MEMBER = "serviceAccount:capture@test-project.iam.gserviceaccount.com"
ACCESSOR = "roles/secretmanager.secretAccessor"


def with_normalisation_job(tmp_path: Path, *, stray_accessor: bool = True) -> Path:
    """The normalisation job left behind, and -- stray -- the capture identity
    holding an accessor on the provider key and on both quota-store secrets."""
    state = tmp_path / "gcloud.json"
    stray = [[ACCESSOR, CAPTURE_MEMBER]] if stray_accessor else []
    state.write_text(json.dumps({"jobs": {"test-worker": {"MILO_CAPTURE_REPLAY": "false"},
                                          NORMALISATION_JOB: {"MILO_ENABLE_PAID_EXECUTION": "false"}},
                                 "services": {"test-api": {}},
                                 "secret_iam": {name: stray + [[ACCESSOR, "serviceAccount:worker@test-project"
                                                                          ".iam.gserviceaccount.com"]]
                                                for name in ("KIMI_API_KEY", "UPSTASH_URL", "UPSTASH_TOKEN")}}))
    return state


def real_removal(tree: OpsTree) -> None:
    """Step 6 runs the REAL --remove-manufacturer-normalisation path (the kill
    switch's), against the stateful gcloud above."""
    source = Path(__file__).resolve().parents[1] / "scripts" / "deploy" / "website-execution-activate.sh"
    (tree.root / "scripts" / "deploy" / "website-execution-activate.sh").write_text(source.read_text())
    tree.config.write_text(tree.config.read_text() + "SECRET_REDIS_URL=UPSTASH_URL\nSECRET_REDIS_TOKEN=UPSTASH_TOKEN\n")
    subprocess.run(["git", "-c", "user.email=t@example.test", "-c", "user.name=t", "commit", "-qam", "removal"],
                   cwd=tree.root, check=True)
    tree.sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tree.root, check=True,
                              capture_output=True, text=True).stdout.strip()


def test_the_stage_a_reset_removes_normalisation_and_reads_it_back(tmp_path, checks_api):
    tree, env = deploy_tree(tmp_path, checks_api)
    real_removal(tree)
    state = with_normalisation_job(tmp_path)
    result = tree.run("deploy.sh", "--sha", tree.sha, "--restore-website-stage", "none", extra_env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    after = json.loads(state.read_text())
    assert NORMALISATION_JOB not in after["jobs"]
    # The stray accessor is revoked on all three secrets; the worker's is untouched.
    for name, bindings in after["secret_iam"].items():
        assert [ACCESSOR, CAPTURE_MEMBER] not in bindings, name
        assert len(bindings) == 1, name
    calls = tree.tool_calls()
    delete = f"gcloud run jobs delete {NORMALISATION_JOB} --region test-region --project test-project --quiet"
    revoke = next(c for c in calls if c.startswith("gcloud secrets remove-iam-policy-binding KIMI_API_KEY"))
    activate = calls.index(next(c for c in calls if c.startswith("stub production-activate.sh")))
    assert calls.index(delete) < activate and calls.index(revoke) < activate
    assert "SUMMARY|6 stage2-reset|PASS|" in result.stdout
    assert "capture identity's access revoked (read back)" in result.stdout
    # Absent already, nothing held: read, nothing deleted or revoked again.
    again = tree.run("deploy.sh", "--sha", tree.sha, "--restore-website-stage", "none", extra_env=env)
    assert again.returncode == 0, again.stdout + again.stderr
    assert tree.tool_calls().count(delete) == 1
    assert sum(c.startswith("gcloud secrets remove-iam-policy-binding") for c in tree.tool_calls()) == 3


@pytest.mark.parametrize("failure, cause", [
    ({"OPS_TEST_DELETE_IGNORED": "1"}, "is still there, or could not be listed"),
    ({"OPS_TEST_JOBS_LIST_EXIT": "1"}, "is still there, or could not be listed"),
    ({"OPS_TEST_REVOKE_IGNORED": "1"}, "provider access could not be revoked or read back"),
    ({"OPS_TEST_SECRET_POLICY_EXIT": "1"}, "provider access could not be revoked or read back")])
def test_a_normalisation_left_behind_by_the_reset_stops_the_deploy(tmp_path, checks_api, failure, cause):
    tree, env = deploy_tree(tmp_path, checks_api)
    real_removal(tree)
    with_normalisation_job(tmp_path)
    result = tree.run("deploy.sh", "--sha", tree.sha, "--restore-website-stage", "none",
                      extra_env={**env, **failure})
    assert result.returncode == 1, result.stdout
    assert "SUMMARY|6 stage2-reset|FAIL|manufacturer normalisation is not fully removed" in result.stdout
    assert cause in result.stdout + result.stderr
    assert not any(c.startswith("stub production-activate.sh") for c in tree.tool_calls())


def test_permanent_mode_keeps_the_normalisation_job(tmp_path, checks_api):
    tree, env = deploy_tree(tmp_path, checks_api)
    state = with_normalisation_job(tmp_path)
    result = tree.run("deploy.sh", "--sha", tree.sha, "--permanent-mode", "true", extra_env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert NORMALISATION_JOB in json.loads(state.read_text())["jobs"]
    assert not any("jobs delete" in c or "jobs list" in c for c in tree.tool_calls())
    assert not any(c.endswith("--remove-manufacturer-normalisation") for c in stub_calls(tree))

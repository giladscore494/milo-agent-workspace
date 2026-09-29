"""PR-Ops: operate production from a phone -- the workflows and their scripts.

What this proves, offline
-------------------------

1.  Every production workflow is `workflow_dispatch` only, main only, keyless
    (Workload Identity Federation through google-github-actions/auth), holds
    `id-token: write` and no other write permission, runs in the `production`
    environment (the kill switch: `production-kill-switch`, no reviewer, its
    own concurrency group) and never cancels itself mid-run.
2.  No workflow step prints a secret, a connection string or a key: a secret
    reaches a step ONLY through that step's `env:`, never through `run:` text,
    `with:` or job-level env; no step uses `set -x`; no `run:` interpolates an
    input or a secret expression directly.
3.  Every workflow script has a dry run that prints the complete plan and
    calls NOTHING -- run here with stand-ins for gcloud, psql, curl, npx and
    vercel that fail the test when touched -- and whose output, and job
    summary, never contain a sentinel secret placed in the environment.
4.  The replay capture flag is refused `on` with live runs, and touches only
    the worker job.
5.  setup-wif.sh --plan changes nothing, --apply makes exactly the planned
    changes, and a second --plan finds nothing left to do; its condition
    admits only this repository, on main, in the two environments.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github" / "workflows"
OPS = REPO / "scripts" / "ops"
PRODUCTION_WORKFLOWS = ("deploy.yml", "kill-switch.yml", "capture-flag.yml", "gates.yml", "arm.yml",
                        "website-stage.yml")
OPS_SCRIPTS = ("common.sh", "deploy.sh", "kill-switch.sh", "capture-flag.sh", "gates.sh", "arm.sh",
               "setup-wif.sh", "write-operator-config.sh", "link-vercel.sh", "website-stage.sh",
               "deployed-release.sh", "preflight-deployer.sh")

#: Sentinel secrets: if one of these ever reaches stdout, stderr or the job
#: summary, a script printed a secret.
SENTINELS = {
    "MILO_READONLY_DB_URL": "postgresql://ro_user:S3NTINEL-DB-PASSWORD@db.sentinel.supabase.co:5432/postgres",
    "GH_TOKEN": "ghs_SENTINELGITHUBTOKEN000000000000",
    "GITHUB_TOKEN": "ghs_SENTINELGITHUBTOKEN111111111111",
    "VERCEL_TOKEN": "SENTINEL-VERCEL-TOKEN-2222",
}
SENTINEL_FRAGMENTS = ("S3NTINEL-DB-PASSWORD", "SENTINELGITHUBTOKEN", "SENTINEL-VERCEL-TOKEN",
                      "db.sentinel.supabase.co")

OPERATOR_ENV = (
    "GCP_PROJECT_ID=test-project\nGCP_REGION=test-region\nARTIFACT_REGISTRY_REPOSITORY=test-repo\n"
    "CLOUD_RUN_API_SERVICE=test-api\nCLOUD_RUN_WORKER_JOB=test-worker\n"
    "CLOUD_RUN_CAPTURE_JOB=test-capture\n"
    "API_SERVICE_ACCOUNT=api@test-project.iam.gserviceaccount.com\n"
    "WORKER_SERVICE_ACCOUNT=worker@test-project.iam.gserviceaccount.com\n"
    "CAPTURE_SERVICE_ACCOUNT=capture@test-project.iam.gserviceaccount.com\n"
    "CLOUD_BUILD_SERVICE_ACCOUNT=milo-cloudbuild@test-project.iam.gserviceaccount.com\n"
    "SUPABASE_PROJECT_REF=abcdefghijklmnopqrst\n"
    "SECRET_SUPABASE_URL=SUPABASE_URL\nSECRET_SUPABASE_SERVICE_KEY=SUPABASE_SECRET_KEY\n"
    "SECRET_PROVIDER_API_KEY=KIMI_API_KEY\nPRODUCTION_ORIGIN=https://site.test\n"
    "READONLY_DATABASE_URL_ENV=MILO_READONLY_DB_URL\n"
    "MILO_GATEWAY_AUDIENCE=https://test-api.example.test\n"
    "MILO_APPROVED_GATEWAY_IDENTITIES=gateway@test-project.iam.gserviceaccount.com\n"
    "MILO_WORKER_AUDIENCE=https://test-api.example.test/worker\n")


def workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def triggers(doc: dict) -> dict:
    # PyYAML reads the bare key `on` as the boolean True.
    return doc.get("on", doc.get(True))


def steps(doc: dict) -> list[dict]:
    (job,) = doc["jobs"].values()
    return job["steps"]


# =============================================================================
# 1. the workflows' shape
# =============================================================================

@pytest.mark.parametrize("name", PRODUCTION_WORKFLOWS)
def test_every_production_workflow_is_dispatch_only_keyless_and_serialized(name):
    doc = workflow(name)
    assert set(triggers(doc)) == {"workflow_dispatch"}, "never on push, schedule or pull_request"
    assert doc["permissions"] == {"contents": "read", "id-token": "write",
                                  **({"checks": "read"} if name == "deploy.yml" else {})}
    assert doc["concurrency"]["cancel-in-progress"] is False
    (job,) = doc["jobs"].values()
    expected_env = "production-kill-switch" if name == "kill-switch.yml" else "production"
    assert job["environment"] == expected_env
    assert "env" not in job and "env" not in doc, "no job- or workflow-level env: secrets per step only"
    all_steps = steps(doc)
    # Refused unless dispatched from main -- the FIRST step, before any checkout.
    assert 'refs/heads/main' in all_steps[0]["run"]
    auth = [step for step in all_steps if str(step.get("uses", "")).startswith("google-github-actions/auth@")]
    assert len(auth) == 1
    assert auth[0]["with"] == {
        "workload_identity_provider": "${{ vars.GCP_WORKLOAD_IDENTITY_PROVIDER }}",
        "service_account": "${{ vars.GCP_DEPLOY_SERVICE_ACCOUNT }}"}
    text = (WORKFLOWS / name).read_text(encoding="utf-8")
    assert "credentials_json" not in text and "GOOGLE_APPLICATION_CREDENTIALS" not in text
    # The workflow runs the matching ops script, and nothing that deploys by hand.
    assert re.search(r"bash scripts/ops/[a-z-]+\.sh", text)


def test_the_kill_switch_never_waits_behind_another_operation():
    groups = {name: workflow(name)["concurrency"]["group"] for name in PRODUCTION_WORKFLOWS}
    assert groups["kill-switch.yml"] == "milo-production-kill-switch"
    assert {groups[name] for name in PRODUCTION_WORKFLOWS if name != "kill-switch.yml"} == \
        {"milo-production-operations"}


def test_arm_and_the_kill_switch_require_a_typed_confirmation_to_apply():
    for name, word in (("arm.yml", "ARM"), ("kill-switch.yml", "KILL")):
        first = steps(workflow(name))[0]["run"]
        assert f'"${{CONFIRM_INPUT}}" != "{word}"' in first


def test_deploy_takes_an_exact_sha_on_main_and_reads_permanent_mode_from_a_variable():
    doc = workflow("deploy.yml")
    assert triggers(doc)["workflow_dispatch"]["inputs"]["sha"]["required"] is True
    text = (WORKFLOWS / "deploy.yml").read_text(encoding="utf-8")
    assert "git merge-base --is-ancestor" in text
    assert "PERMANENT_MODE: ${{ vars.MILO_PERMANENT_MODE || 'false' }}" in text


def test_capture_flag_offers_only_on_and_off():
    options = triggers(workflow("capture-flag.yml"))["workflow_dispatch"]["inputs"]["state"]["options"]
    assert options == ["off", "on"]


# =============================================================================
# 2. no workflow step prints a secret
# =============================================================================

SECRET_EXPRESSION = re.compile(r"\$\{\{\s*secrets\.([A-Z_]+)\s*\}\}")


@pytest.mark.parametrize("name", PRODUCTION_WORKFLOWS)
def test_a_secret_reaches_a_step_only_through_that_steps_env(name):
    for step in steps(workflow(name)):
        for key, value in step.items():
            if key == "env":
                continue
            assert not SECRET_EXPRESSION.search(json.dumps(value)), f"{name}: secret in {key}"
        for variable, value in (step.get("env") or {}).items():
            match = SECRET_EXPRESSION.search(str(value))
            if match:
                # Bound under its own name, as the whole value -- never embedded.
                assert str(value).strip() == f"${{{{ secrets.{match.group(1)} }}}}"
                assert variable == match.group(1)


@pytest.mark.parametrize("name", PRODUCTION_WORKFLOWS)
def test_no_run_step_echoes_a_secret_or_interpolates_an_expression(name):
    secret_names = set(SECRET_EXPRESSION.findall((WORKFLOWS / name).read_text(encoding="utf-8")))
    for step in steps(workflow(name)):
        script = step.get("run")
        if script is None:
            continue
        assert "${{" not in script, f"{name}: an expression is interpolated into a run script"
        assert "set -x" not in script and "set -o xtrace" not in script
        for secret in secret_names | {"GH_TOKEN"}:
            assert f"${secret}" not in script and f"${{{secret}}}" not in script, \
                f"{name}: a run step references the secret {secret}"


@pytest.mark.parametrize("script", OPS_SCRIPTS)
def test_no_ops_script_traces_or_prints_a_secret_variable(script):
    text = (OPS / script).read_text(encoding="utf-8")
    assert "set -x" not in text and "set -o xtrace" not in text
    for secret in SENTINELS:
        for use in re.finditer(rf"\$\{{?{secret}\b", text):
            line = text[:use.start()].rsplit("\n", 1)[-1] + text[use.start():].split("\n", 1)[0]
            assert not re.search(r"\b(echo|printf)\b", line), f"{script}: prints {secret}: {line}"


# =============================================================================
# 3. every script's dry run calls nothing and prints no secret
# =============================================================================

FORBIDDEN_TOOL = """#!/usr/bin/env bash
printf '%s %s\\n' "$(basename "$0")" "$*" >> "$OPS_TEST_CALLS"
exit 97
"""


class OpsTree:
    """A throwaway git checkout holding the ops scripts and the tools they wrap."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "repo"
        for relative in ("scripts/ops", "scripts/deploy", "scripts/release", "scripts/catalog", "backend"):
            shutil.copytree(REPO / relative, self.root / relative,
                            ignore=shutil.ignore_patterns("__pycache__"))
        (self.root / "frontend").mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "-c", "user.email=t@example.test", "-c", "user.name=t", "add", "-A"],
                       cwd=self.root, check=True)
        subprocess.run(["git", "-c", "user.email=t@example.test", "-c", "user.name=t", "commit",
                        "-q", "-m", "release"], cwd=self.root, check=True)
        self.sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.root, check=True,
                                  capture_output=True, text=True).stdout.strip()
        self.config = tmp_path / "operator.env"
        self.config.write_text(OPERATOR_ENV, encoding="utf-8")
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.calls = tmp_path / "calls.log"
        self.calls.write_text("", encoding="utf-8")
        self.summary = tmp_path / "summary.md"
        for tool in ("gcloud", "psql", "curl", "npx", "vercel", "wget"):
            self.tool(tool, FORBIDDEN_TOOL)

    def tool(self, name: str, text: str) -> None:
        path = self.bin / name
        path.write_text(text, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def run(self, script: str, *args: str, extra_env: dict[str, str] | None = None):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("MILO_", "GITHUB_", "GCP_"))}
        env.update(SENTINELS)
        env.update({"PATH": f"{self.bin}:/usr/bin:/bin", "OPS_TEST_CALLS": str(self.calls),
                    "MILO_OPERATOR_CONFIG": str(self.config),
                    "GITHUB_STEP_SUMMARY": str(self.summary),
                    "GITHUB_REPOSITORY": "giladscore494/milo-agent-workspace"})
        env.update(extra_env or {})
        return subprocess.run(["bash", str(self.root / "scripts" / "ops" / script), *args],
                              cwd=self.root, capture_output=True, text=True, env=env, timeout=120)

    def tool_calls(self) -> list[str]:
        return [line for line in self.calls.read_text(encoding="utf-8").splitlines() if line]


WS = ("--work-scope-id", "00000000-0000-4000-8000-000000000020",
      "--work-scope-revision", "3", "--work-scope-digest", "d" * 64)


def dry_runs(tree: OpsTree) -> dict[str, tuple[str, ...]]:
    return {
        "deploy.sh": ("--sha", tree.sha, "--permanent-mode", "false", "--dry-run"),
        "deploy.sh (permanent)": ("--sha", tree.sha, "--permanent-mode", "true", "--dry-run"),
        "kill-switch.sh": ("--vercel-deployment", "https://milo-abc.vercel.app", "--dry-run"),
        "capture-flag.sh on": ("on", "--dry-run"),
        "capture-flag.sh off": ("off", "--dry-run"),
        "gates.sh": ("--gate", "prepared", *WS, "--dry-run"),
        "arm.sh": (*WS, "--dry-run"),
        "preflight-deployer.sh": ("--sha", tree.sha, "--dry-run"),
    }


@pytest.mark.parametrize("label", ["deploy.sh", "deploy.sh (permanent)", "kill-switch.sh",
                                   "capture-flag.sh on", "capture-flag.sh off", "gates.sh",
                                   "arm.sh", "preflight-deployer.sh"])
def test_every_dry_run_calls_nothing_and_prints_no_secret(tmp_path, label):
    tree = OpsTree(tmp_path)
    args = dry_runs(tree)[label]
    result = tree.run(label.split(" ")[0], *args)
    assert result.returncode == 0, result.stdout + result.stderr
    assert tree.tool_calls() == [], f"{label} called an external tool in a dry run"
    shown = result.stdout + result.stderr + tree.summary.read_text(encoding="utf-8")
    for fragment in SENTINEL_FRAGMENTS:
        assert fragment not in shown, f"{label} printed a secret"
    assert "SUMMARY|" in result.stdout
    assert "| Step | Result | Detail |" in tree.summary.read_text(encoding="utf-8")


def test_the_deploy_dry_run_states_every_step_of_the_r_block_in_order(tmp_path):
    tree = OpsTree(tmp_path)
    result = tree.run("deploy.sh", "--sha", tree.sha, "--dry-run")
    steps_seen = re.findall(r"^SUMMARY\|(\d+) ", result.stdout, re.M)
    assert steps_seen == [str(n) for n in range(1, 12)]
    for needle in ("check-migration-state.sh", "website-execution-check.sh",
                   "--remove-secrets KIMI_API_KEY", "production-activate.sh --all",
                   "--force-redeploy", "MILO_COMMANDER_MODEL=kimi-k3",
                   "production-verify.sh", "--gate deployed"):
        assert needle in result.stdout, needle
    permanent = tree.run("deploy.sh", "--sha", tree.sha, "--permanent-mode", "true", "--dry-run")
    assert "--preserve-stage" in permanent.stdout and "--force-redeploy" not in permanent.stdout
    assert "--remove-secrets" not in permanent.stdout
    assert "SUMMARY|6 stage2-reset|SKIPPED|permanent operating mode" in permanent.stdout


def test_deploy_refuses_a_checkout_that_is_not_the_requested_sha(tmp_path):
    tree = OpsTree(tmp_path)
    result = tree.run("deploy.sh", "--sha", "0" * 40, "--dry-run")
    assert result.returncode == 1
    assert "SUMMARY|1 release-checkout|FAIL|" in result.stdout
    assert tree.tool_calls() == []
    assert tree.run("deploy.sh", "--sha", "abc").returncode == 2
    assert tree.run("deploy.sh", "--sha", tree.sha, "--permanent-mode", "yes").returncode == 2


def test_container_facts_never_print_a_plain_text_provider_key(tmp_path):
    doc = {"spec": {"template": {"spec": {"template": {"spec": {"containers": [{"env": [
        {"name": "KIMI_API_KEY", "value": "sk-SENTINEL-PLAIN-KEY"},
        {"name": "MILO_CAPTURE_REPLAY", "value": "false"},
        {"name": "MOONSHOT_API_KEY", "valueFrom": {"secretKeyRef": {"name": "M", "key": "latest"}}},
    ]}]}}}}}}
    script = f"source {OPS / 'common.sh'}; ops_container_facts"
    result = subprocess.run(["bash", "-c", script], input=json.dumps(doc), capture_output=True,
                            text=True, env={**os.environ, "MILO_OPERATOR_CONFIG": "/nonexistent"})
    assert result.returncode == 0, result.stderr
    assert "sk-SENTINEL-PLAIN-KEY" not in result.stdout
    assert "env\tKIMI_API_KEY\t<not read>" in result.stdout
    assert "env\tMILO_CAPTURE_REPLAY\tfalse" in result.stdout
    assert "secret\tMOONSHOT_API_KEY" in result.stdout


# =============================================================================
# 4. the replay capture flag
# =============================================================================

PSQL_COUNT = """#!/usr/bin/env bash
printf '%s\\n' "psql" >> "$OPS_TEST_CALLS"
printf '%s\\n' "$OPS_TEST_LIVE_RUNS"
"""
GCLOUD_WORKER = """#!/usr/bin/env bash
printf 'gcloud %s\\n' "$*" >> "$OPS_TEST_CALLS"
if [[ "$1 $2 $3" == "run jobs update" ]]; then
  for arg in "$@"; do [[ "$arg" == MILO_CAPTURE_REPLAY=* ]] && printf '%s' "${arg#*=}" > "$OPS_TEST_STATE"; done
  exit 0
fi
if [[ "$1 $2 $3" == "run jobs describe" ]]; then
  printf '{"spec":{"template":{"spec":{"template":{"spec":{"containers":[{"env":[{"name":"MILO_CAPTURE_REPLAY","value":"%s"}]}]}}}}}}' "$(cat "$OPS_TEST_STATE" 2>/dev/null || echo false)"
  exit 0
fi
exit 0
"""


def capture_tree(tmp_path: Path) -> tuple[OpsTree, dict[str, str]]:
    tree = OpsTree(tmp_path)
    tree.tool("psql", PSQL_COUNT)
    tree.tool("gcloud", GCLOUD_WORKER)
    return tree, {"OPS_TEST_STATE": str(tmp_path / "flag")}


def test_the_capture_flag_is_refused_on_while_a_run_is_live(tmp_path):
    tree, env = capture_tree(tmp_path)
    result = tree.run("capture-flag.sh", "on", extra_env={**env, "OPS_TEST_LIVE_RUNS": "2"})
    assert result.returncode == 1
    assert "SUMMARY|1 live-runs|FAIL|2 non-terminal run(s) exist" in result.stdout
    assert not [call for call in tree.tool_calls() if "update" in call]


def test_the_capture_flag_touches_the_worker_job_only_and_says_to_turn_it_off(tmp_path):
    tree, env = capture_tree(tmp_path)
    result = tree.run("capture-flag.sh", "on", extra_env={**env, "OPS_TEST_LIVE_RUNS": "0"})
    assert result.returncode == 0, result.stdout + result.stderr
    updates = [call for call in tree.tool_calls() if " update " in call]
    assert updates == ["gcloud run jobs update test-worker --region test-region --project "
                       "test-project --update-env-vars MILO_CAPTURE_REPLAY=true"]
    assert not [call for call in tree.tool_calls() if "services" in call]
    assert "Turn the replay capture OFF after ONE run" in tree.summary.read_text(encoding="utf-8")
    off = tree.run("capture-flag.sh", "off", extra_env={**env, "OPS_TEST_LIVE_RUNS": "5"})
    assert off.returncode == 0, off.stdout + off.stderr
    assert "SUMMARY|2 worker-flag|PASS|MILO_CAPTURE_REPLAY=false on test-worker" in off.stdout


# =============================================================================
# 5. the operator configuration and the Workload Identity setup
# =============================================================================

@pytest.mark.parametrize("text,message", [
    ("", "is empty"),
    (OPERATOR_ENV.replace("READONLY_DATABASE_URL_ENV=MILO_READONLY_DB_URL",
                          "READONLY_DATABASE_URL_ENV=OTHER"), "must be MILO_READONLY_DB_URL"),
    (OPERATOR_ENV + "NOTE=postgresql://u:p@h/db\n", "never a secret value"),
    ("not a pair\n", "malformed line"),
])
def test_the_operator_configuration_holds_identifiers_only(tmp_path, text, message):
    tree = OpsTree(tmp_path)
    result = tree.run("write-operator-config.sh",
                      extra_env={"MILO_OPERATOR_CONFIG_TEXT": text, "RUNNER_TEMP": str(tmp_path)})
    assert result.returncode != 0
    assert message in result.stderr
    assert "u:p@h" not in result.stdout + result.stderr


def test_a_valid_operator_configuration_is_written_and_exported(tmp_path):
    tree = OpsTree(tmp_path)
    github_env = tmp_path / "github_env"
    result = tree.run("write-operator-config.sh",
                      extra_env={"MILO_OPERATOR_CONFIG_TEXT": OPERATOR_ENV,
                                 "RUNNER_TEMP": str(tmp_path), "GITHUB_ENV": str(github_env)})
    assert result.returncode == 0, result.stderr
    assert github_env.read_text() == f"MILO_OPERATOR_CONFIG={tmp_path / 'production-operator.env'}\n"
    assert "test-project" not in result.stdout


WIF_GCLOUD = r'''#!/usr/bin/env python3
"""A gcloud stand-in with a tiny IAM world: describes answer what creates made."""
import json, os, sys
args = sys.argv[1:]
path = os.environ["OPS_TEST_WIF_STATE"]
state = {"apis": [], "pools": [], "providers": {}, "accounts": [], "project_bindings": [],
         "sa_bindings": {}, "buckets": [], "bucket_bindings": [], "repositories": ["test-repo"],
         "repo_bindings": []}
state.update(json.loads(os.environ.get("OPS_TEST_WIF_INITIAL") or "{}"))
if os.path.exists(path):
    state = json.load(open(path))
with open(os.environ["OPS_TEST_CALLS"], "a") as log:
    log.write("gcloud " + " ".join(args) + "\n")
def save():
    json.dump(state, open(path, "w"))
def flag(name):
    for index, arg in enumerate(args):
        if arg == name:
            return args[index + 1]
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
    raise SystemExit(3)
def policy(bindings):
    print(json.dumps({"bindings": [{"role": r, "members": [m]} for r, m in bindings]}))
    sys.exit(0)
def remove(bindings):
    pair = [flag("--role"), flag("--member")]
    if pair not in bindings:
        sys.stderr.write("binding not found\n"); sys.exit(1)
    bindings.remove(pair); save(); sys.exit(0)
joined = " ".join(args)
if args[:2] == ["auth", "list"]:
    print("owner@example.test"); sys.exit(0)
if args[:3] == ["config", "get-value", "project"]:
    print("test-project"); sys.exit(0)
if args[:2] == ["projects", "describe"]:
    print("123456789"); sys.exit(0)
if args[:2] == ["services", "list"]:
    api = flag("--filter").split(":", 1)[1]
    print(api if api in state["apis"] else ""); sys.exit(0)
if args[:2] == ["services", "enable"]:
    state["apis"].append(args[2]); save(); sys.exit(0)
if args[:3] == ["iam", "workload-identity-pools", "describe"]:
    sys.exit(0 if args[3] in state["pools"] else 1)
if args[:3] == ["iam", "workload-identity-pools", "create"]:
    state["pools"].append(args[3]); save(); sys.exit(0)
if args[:4] == ["iam", "workload-identity-pools", "providers", "describe"]:
    if args[4] not in state["providers"]:
        sys.stderr.write("ERROR: (gcloud) NOT_FOUND: Requested entity was not found.\n"); sys.exit(1)
    if "--format=json" in args:
        print(json.dumps({"attributeCondition": state["providers"][args[4]],
                          "attributeMapping": state.get("mappings", {}).get(args[4], {})}))
        sys.exit(0)
    print(state["providers"][args[4]]); sys.exit(0)
if args[:4] == ["iam", "workload-identity-pools", "providers", "create-oidc"]:
    state.setdefault("mappings", {})[args[4]] = dict(
        item.split("=", 1) for item in flag("--attribute-mapping").split(","))
if args[:4] == ["iam", "workload-identity-pools", "providers", "create-oidc"] or \
        args[:4] == ["iam", "workload-identity-pools", "providers", "update-oidc"]:
    state["providers"][args[4]] = flag("--attribute-condition"); save(); sys.exit(0)
if args[:3] == ["iam", "service-accounts", "describe"]:
    sys.exit(0 if args[3].split("@")[0] in state["accounts"] else 1)
if args[:3] == ["iam", "service-accounts", "create"]:
    state["accounts"].append(args[3]); save(); sys.exit(0)
if args[:2] == ["projects", "get-iam-policy"]:
    policy(state["project_bindings"])
if args[:2] == ["projects", "add-iam-policy-binding"]:
    state["project_bindings"].append([flag("--role"), flag("--member")]); save(); sys.exit(0)
if args[:2] == ["projects", "remove-iam-policy-binding"]:
    remove(state["project_bindings"])
if args[:3] == ["iam", "service-accounts", "get-iam-policy"]:
    policy(state["sa_bindings"].get(args[3], []))
if args[:3] == ["iam", "service-accounts", "add-iam-policy-binding"]:
    state["sa_bindings"].setdefault(args[3], []).append([flag("--role"), flag("--member")]); save()
    sys.exit(0)
if args[:3] == ["iam", "service-accounts", "remove-iam-policy-binding"]:
    remove(state["sa_bindings"].setdefault(args[3], []))
if args[:3] == ["storage", "buckets", "describe"]:
    sys.exit(0 if args[3] in state["buckets"] else 1)
if args[:3] == ["storage", "buckets", "create"]:
    state["buckets"].append(args[3]); save(); sys.exit(0)
if args[:3] == ["storage", "buckets", "get-iam-policy"]:
    if args[3] not in state["buckets"]:
        sys.exit(1)
    policy(state["bucket_bindings"])
if args[:3] == ["storage", "buckets", "add-iam-policy-binding"]:
    if args[3] not in state["buckets"]:
        sys.stderr.write("NOT_FOUND bucket\n"); sys.exit(1)
    state["bucket_bindings"].append([flag("--role"), flag("--member")]); save(); sys.exit(0)
if args[:3] == ["artifacts", "repositories", "describe"]:
    sys.exit(0 if args[3] in state["repositories"] else 1)
if args[:3] == ["artifacts", "repositories", "get-iam-policy"]:
    if args[3] not in state["repositories"]:
        sys.exit(1)
    policy(state["repo_bindings"])
if args[:3] == ["artifacts", "repositories", "add-iam-policy-binding"]:
    state["repo_bindings"].append([flag("--role"), flag("--member")]); save(); sys.exit(0)
sys.stderr.write("unmocked gcloud " + joined + "\n"); sys.exit(2)
'''

READ_ONLY_VERBS = ("describe", "list", "get-iam-policy", "get-value")


DEPLOYER = "serviceAccount:milo-github-deployer@test-project.iam.gserviceaccount.com"
BUILD_SA = "milo-cloudbuild@test-project.iam.gserviceaccount.com"
COMPUTE_SA = "123456789-compute@developer.gserviceaccount.com"
CHANGE_LINE = re.compile(r"^(CREATE|UPDATE|BIND|UNBIND) ", re.M)


def test_setup_wif_plans_applies_once_and_is_idempotent(tmp_path):
    tree = OpsTree(tmp_path)
    tree.tool("gcloud", WIF_GCLOUD)
    env = {"OPS_TEST_WIF_STATE": str(tmp_path / "wif.json")}

    plan = tree.run("setup-wif.sh", "--plan", extra_env=env)
    assert plan.returncode == 0, plan.stdout + plan.stderr
    mutations = [call for call in tree.tool_calls()
                 if not any(verb in call.split()[:5] for verb in READ_ONLY_VERBS)
                 and not call.startswith("gcloud auth list")]
    assert mutations == [], "--plan changed something"
    condition = ("assertion.repository == 'giladscore494/milo-agent-workspace' && "
                 "assertion.ref == 'refs/heads/main' && "
                 "assertion.environment in ['production', 'production-kill-switch', 'production-backup']")
    assert condition in plan.stdout
    planned = len(CHANGE_LINE.findall(plan.stdout))
    assert planned > 0
    assert "GCP_WORKLOAD_IDENTITY_PROVIDER=projects/123456789/locations/global/" \
           "workloadIdentityPools/milo-github/providers/github-actions" in plan.stdout
    assert f"CLOUD_BUILD_SERVICE_ACCOUNT={BUILD_SA}" in plan.stdout

    tree.calls.write_text("", encoding="utf-8")
    applied = tree.run("setup-wif.sh", "--apply", extra_env=env)
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert len(CHANGE_LINE.findall(applied.stdout)) == planned
    assert ("PASS   provider github-actions condition read back: repository "
            "giladscore494/milo-agent-workspace, ref refs/heads/main, environments "
            "production, production-kill-switch, production-backup") in applied.stdout
    calls = tree.tool_calls()
    assert not [call for call in calls if "keys create" in call], "no key is ever created"
    wif_user = [call for call in calls if "roles/iam.workloadIdentityUser" in call]
    assert len(wif_user) == 2 and not [call for call in wif_user if "attribute.repository/" in call]
    assert deployer_wif_members(state_path_of(tmp_path)) == DEPLOYER_PRINCIPALS
    state = json.loads((tmp_path / "wif.json").read_text())
    deployer_roles = {role for role, member in state["project_bindings"] if member == DEPLOYER}
    assert deployer_roles == {"roles/run.admin", "roles/cloudbuild.builds.editor",
                              "roles/artifactregistry.reader", "roles/secretmanager.viewer",
                              "roles/iam.serviceAccountViewer",
                              "roles/serviceusage.serviceUsageConsumer", "roles/logging.viewer",
                              "roles/storage.bucketViewer"}
    assert not {"roles/owner", "roles/editor", "roles/iam.serviceAccountUser"} & deployer_roles
    # The build identity: exactly three bindings, each on the narrowest resource.
    build = f"serviceAccount:{BUILD_SA}"
    assert "milo-cloudbuild" in state["accounts"]
    assert [role for role, member in state["project_bindings"] if member == build] == \
        ["roles/logging.logWriter"]
    assert state["repo_bindings"] == [["roles/artifactregistry.writer", build]]
    assert [role for role, member in state["bucket_bindings"] if member == build] == \
        ["roles/storage.objectViewer"]
    assert "gs://test-project_cloudbuild" in state["buckets"]
    # actAs: the three runtime identities and the build identity, each on that
    # account only -- never the Compute default service account.
    act_as = [call for call in calls if "roles/iam.serviceAccountUser" in call]
    assert len(act_as) == 4 and all(call.startswith("gcloud iam service-accounts add-iam-policy-binding")
                                    for call in act_as)
    assert sorted(account for account, bindings in state["sa_bindings"].items()
                  if ["roles/iam.serviceAccountUser", DEPLOYER] in bindings) == sorted([
        "api@test-project.iam.gserviceaccount.com", "worker@test-project.iam.gserviceaccount.com",
        "capture@test-project.iam.gserviceaccount.com", BUILD_SA])
    assert not [call for call in calls if COMPUTE_SA in call and "add-iam-policy-binding" in call]
    assert set(state["apis"]) == {
        "cloudresourcemanager.googleapis.com", "iam.googleapis.com", "iamcredentials.googleapis.com",
        "sts.googleapis.com", "run.googleapis.com", "cloudbuild.googleapis.com",
        "artifactregistry.googleapis.com", "serviceusage.googleapis.com", "logging.googleapis.com"}

    again = tree.run("setup-wif.sh", "--plan", extra_env=env)
    assert again.returncode == 0
    assert CHANGE_LINE.findall(again.stdout) == []
    assert "PLAN: 0 change(s)" in again.stdout


def test_ci_lints_the_workflows_and_the_ops_scripts():
    ci = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    assert "actionlint -color .github/workflows/*.yml" in ci
    assert "actionlint-py==" in ci
    assert "scripts/ops/*.sh" in ci and "scripts/release:scripts/deploy:scripts/catalog:scripts/ops" in ci


@pytest.mark.skipif(shutil.which("actionlint") is None, reason="actionlint is not installed here")
def test_actionlint_accepts_every_workflow():
    result = subprocess.run(["actionlint", *sorted(str(path) for path in WORKFLOWS.glob("*.yml"))],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_reviewed_worker_model_env_is_a_valid_model_contract():
    from backend.model_profiles import validate_swarm_model_contract

    contract = (REPO / "scripts" / "deploy" / "deployment-contract.sh").read_text(encoding="utf-8")
    block = contract.split("MILO_REVIEWED_WORKER_MODEL_ENV=(", 1)[1].split(")", 1)[0]
    env = dict(line.strip().strip('"').split("=", 1) for line in block.splitlines() if line.strip())
    assert env == {"MILO_COMMANDER_MODEL": "kimi-k3",
                   "MILO_COMMANDER_MODEL_ALLOWLIST": "kimi-k3,kimi-k2.6",
                   "MILO_SWARM_WORKER_MODEL": "kimi-k2.6"}
    assert validate_swarm_model_contract(env, require_present=True) == \
        ("kimi-k3", "kimi-k2.6", ("kimi-k3", "kimi-k2.6"))



# -- PR-OBS: production-backup in the provider condition ----------------------------

SETUP_WIF_TEXT = (OPS / "setup-wif.sh").read_text(encoding="utf-8")
OLD_CONDITION = ("assertion.repository == 'giladscore494/milo-agent-workspace' && "
                 "assertion.ref == 'refs/heads/main' && "
                 "assertion.environment in ['production', 'production-kill-switch']")
NEW_CONDITION = ("assertion.repository == 'giladscore494/milo-agent-workspace' && "
                 "assertion.ref == 'refs/heads/main' && "
                 "assertion.environment in ['production', 'production-kill-switch', 'production-backup']")


def test_allowed_environments_are_exactly_the_three():
    match = re.search(r"^ALLOWED_ENVIRONMENTS=\((.*)\)$", SETUP_WIF_TEXT, re.M)
    assert match, "ALLOWED_ENVIRONMENTS is not a single literal array"
    assert re.findall(r'"([^"]+)"', match.group(1)) == ["production", "production-kill-switch", "production-backup"]


def test_the_condition_still_requires_the_repository_and_main():
    line = next(l for l in SETUP_WIF_TEXT.splitlines() if l.startswith("CONDITION="))
    assert line == ('CONDITION="assertion.repository == \'${GITHUB_REPOSITORY_NAME}\' && '
                    "assertion.ref == 'refs/heads/main' && "
                    'assertion.environment in [${environments_cel%, }]"')
    # The deployer: exactly the production and production-kill-switch
    # environment principalSets (the repository-wide one is only removed).
    assert re.search(r'^DEPLOYER_ENVIRONMENTS=\("production" "production-kill-switch"\)$', SETUP_WIF_TEXT, re.M)
    assert ('DEPLOYER_PRINCIPALS+=("principalSet://iam.googleapis.com/${POOL_NAME}/attribute.environment/${environment}")'
            in SETUP_WIF_TEXT)
    assert '--member "$REPOSITORY_PRINCIPALS" --role roles/iam.workloadIdentityUser' in SETUP_WIF_TEXT
    assert "add-iam-policy-binding \"$DEPLOY_SA\" --project \"$PROJECT_ID\" \\\n      --member \"$REPOSITORY" \
        not in SETUP_WIF_TEXT
    assert 'POOL_ID="milo-github"' in SETUP_WIF_TEXT and 'PROVIDER_ID="github-actions"' in SETUP_WIF_TEXT


def test_an_existing_provider_is_updated_in_place_and_read_back(tmp_path):
    tree = OpsTree(tmp_path)
    tree.tool("gcloud", WIF_GCLOUD)
    state_path = tmp_path / "wif.json"
    env = {"OPS_TEST_WIF_STATE": str(state_path)}
    assert tree.run("setup-wif.sh", "--apply", extra_env=env).returncode == 0
    # The provider as it is in production today: the two-environment condition.
    state = json.loads(state_path.read_text())
    state["providers"]["github-actions"] = OLD_CONDITION
    state_path.write_text(json.dumps(state))
    before = {key: value for key, value in state.items() if key != "providers"}

    plan = tree.run("setup-wif.sh", "--plan", extra_env=env)
    assert CHANGE_LINE.findall(plan.stdout) == ["UPDATE"]
    tree.calls.write_text("", encoding="utf-8")
    applied = tree.run("setup-wif.sh", "--apply", extra_env=env)
    assert applied.returncode == 0, applied.stdout + applied.stderr
    calls = tree.tool_calls()
    assert [c for c in calls if "providers update-oidc" in c]
    assert not [c for c in calls if "providers create-oidc" in c or "pools create" in c]
    after = json.loads(state_path.read_text())
    assert after["providers"]["github-actions"] == NEW_CONDITION
    # Nothing else moved: the deployer's and every other binding are identical.
    assert {key: value for key, value in after.items() if key != "providers"} == before
    assert "PASS   provider github-actions condition read back" in applied.stdout


def test_a_read_back_that_does_not_match_is_a_failure(tmp_path):
    tree = OpsTree(tmp_path)
    tree.tool("gcloud", WIF_GCLOUD.replace(
        'state["providers"][args[4]] = flag("--attribute-condition"); save(); sys.exit(0)',
        'state["providers"][args[4]] = flag("--attribute-condition").replace(", \'production-backup\'", ""); '
        'save(); sys.exit(0)'))
    result = tree.run("setup-wif.sh", "--apply", extra_env={"OPS_TEST_WIF_STATE": str(tmp_path / "wif.json")})
    assert result.returncode == 1
    assert "FAIL   provider github-actions condition read back does not pin" in result.stderr
    assert "PASS   provider" not in result.stdout



def test_a_forged_read_back_is_a_failure_not_a_pass(tmp_path):
    tree = OpsTree(tmp_path)
    tree.tool("gcloud", WIF_GCLOUD.replace(
        'state["providers"][args[4]] = flag("--attribute-condition"); save(); sys.exit(0)',
        'state["providers"][args[4]] = flag("--attribute-condition") + " || true"; save(); sys.exit(0)'))
    result = tree.run("setup-wif.sh", "--apply", extra_env={"OPS_TEST_WIF_STATE": str(tmp_path / "wif.json")})
    assert result.returncode == 1
    assert "PASS   provider" not in result.stdout


def test_the_in_place_update_changes_the_condition_only(tmp_path):
    tree = OpsTree(tmp_path)
    tree.tool("gcloud", WIF_GCLOUD)
    state_path = tmp_path / "wif.json"
    env = {"OPS_TEST_WIF_STATE": str(state_path)}
    assert tree.run("setup-wif.sh", "--apply", extra_env=env).returncode == 0
    state = json.loads(state_path.read_text())
    state["providers"]["github-actions"] = OLD_CONDITION
    state_path.write_text(json.dumps(state))
    tree.calls.write_text("", encoding="utf-8")
    assert tree.run("setup-wif.sh", "--apply", extra_env=env).returncode == 0
    update = [c for c in tree.tool_calls() if "providers update-oidc" in c]
    assert len(update) == 1
    assert "--attribute-mapping" not in update[0] and "--issuer-uri" not in update[0]
    assert "--attribute-condition" in update[0]


# -- PR-OBS follow-up: the deployer narrowed to production + production-kill-switch ----

POOL = "projects/123456789/locations/global/workloadIdentityPools/milo-github"
DEPLOYER_EMAIL = "milo-github-deployer@test-project.iam.gserviceaccount.com"
REPOSITORY_PRINCIPAL = f"principalSet://iam.googleapis.com/{POOL}/attribute.repository/giladscore494/milo-agent-workspace"
DEPLOYER_PRINCIPALS = [f"principalSet://iam.googleapis.com/{POOL}/attribute.environment/production",
                       f"principalSet://iam.googleapis.com/{POOL}/attribute.environment/production-kill-switch"]


def state_path_of(tmp_path: Path) -> Path:
    return tmp_path / "wif.json"


def deployer_wif_members(state_path: Path) -> list[str]:
    state = json.loads(state_path.read_text())
    return sorted(member for role, member in state["sa_bindings"].get(DEPLOYER_EMAIL, [])
                  if role == "roles/iam.workloadIdentityUser")


def production_today(tmp_path: Path) -> tuple[OpsTree, dict[str, str]]:
    """Everything setup-wif.sh made before this PR: the two-environment
    condition and the deployer bound to the REPOSITORY-wide principalSet."""
    tree = OpsTree(tmp_path)
    tree.tool("gcloud", WIF_GCLOUD)
    state_path = state_path_of(tmp_path)
    env = {"OPS_TEST_WIF_STATE": str(state_path)}
    assert tree.run("setup-wif.sh", "--apply", extra_env=env).returncode == 0
    state = json.loads(state_path.read_text())
    state["providers"]["github-actions"] = OLD_CONDITION
    state["sa_bindings"][DEPLOYER_EMAIL] = [
        pair for pair in state["sa_bindings"][DEPLOYER_EMAIL] if pair[0] != "roles/iam.workloadIdentityUser"]
    state["sa_bindings"][DEPLOYER_EMAIL].append(["roles/iam.workloadIdentityUser", REPOSITORY_PRINCIPAL])
    state_path.write_text(json.dumps(state))
    tree.calls.write_text("", encoding="utf-8")
    return tree, env


def _index(calls: list[str], *needles: str) -> list[int]:
    return [i for i, call in enumerate(calls) if all(needle in call for needle in needles)]


def test_the_deployer_is_narrowed_first_then_the_condition_admits_backup(tmp_path):
    tree, env = production_today(tmp_path)
    plan = tree.run("setup-wif.sh", "--plan", extra_env=env)
    assert plan.returncode == 0, plan.stdout + plan.stderr
    # Planned in the order they apply: two BINDs, the UNBIND, then the UPDATE.
    assert CHANGE_LINE.findall(plan.stdout) == ["BIND", "BIND", "UNBIND", "UPDATE"]
    tree.calls.write_text("", encoding="utf-8")

    applied = tree.run("setup-wif.sh", "--apply", extra_env=env)
    assert applied.returncode == 0, applied.stdout + applied.stderr
    calls = tree.tool_calls()
    add_production = _index(calls, "service-accounts add-iam-policy-binding", "attribute.environment/production ")
    add_kill_switch = _index(calls, "service-accounts add-iam-policy-binding", "attribute.environment/production-kill-switch")
    removal = _index(calls, "service-accounts remove-iam-policy-binding", REPOSITORY_PRINCIPAL)
    readbacks = _index(calls, "service-accounts get-iam-policy", DEPLOYER_EMAIL)
    update = _index(calls, "providers update-oidc")
    assert len(add_production) == len(add_kill_switch) == len(removal) == len(update) == 1
    additions = max(add_production[0], add_kill_switch[0])
    # A read-back of the deployer's policy between the additions and the removal...
    assert [i for i in readbacks if additions < i < removal[0]]
    # ...another after the removal, and the condition update only after that.
    assert [i for i in readbacks if removal[0] < i < update[0]]
    assert deployer_wif_members(state_path_of(tmp_path)) == DEPLOYER_PRINCIPALS
    out = applied.stdout
    assert out.index("PASS   milo-github-deployer environment bindings read back: production production-kill-switch") \
        < out.index("PASS   milo-github-deployer workloadIdentityUser members read back: exactly principalSet "
                    "attribute.environment/production and attribute.environment/production-kill-switch of pool milo-github") \
        < out.index("PASS   provider github-actions condition read back")
    assert json.loads(state_path_of(tmp_path).read_text())["providers"]["github-actions"] == NEW_CONDITION
    again = tree.run("setup-wif.sh", "--plan", extra_env=env)
    assert CHANGE_LINE.findall(again.stdout) == []


def test_a_failed_environment_binding_removes_nothing_and_leaves_the_condition(tmp_path):
    tree, env = production_today(tmp_path)
    tree.tool("gcloud", WIF_GCLOUD.replace(
        'if args[:3] == ["iam", "service-accounts", "add-iam-policy-binding"]:',
        'if args[:3] == ["iam", "service-accounts", "add-iam-policy-binding"] and "production-kill-switch" in joined:\n'
        '    sys.stderr.write("PERMISSION_DENIED\\n"); sys.exit(1)\n'
        'if args[:3] == ["iam", "service-accounts", "add-iam-policy-binding"]:'))
    result = tree.run("setup-wif.sh", "--apply", extra_env=env)
    assert result.returncode != 0
    calls = tree.tool_calls()
    assert not [c for c in calls if "remove-iam-policy-binding" in c]
    assert not [c for c in calls if "update-oidc" in c]
    state = json.loads(state_path_of(tmp_path).read_text())
    assert ["roles/iam.workloadIdentityUser", REPOSITORY_PRINCIPAL] in state["sa_bindings"][DEPLOYER_EMAIL]
    assert state["providers"]["github-actions"] == OLD_CONDITION


def test_a_binding_that_does_not_read_back_removes_nothing(tmp_path):
    tree, env = production_today(tmp_path)
    # The add "succeeds" but the binding never lands.
    tree.tool("gcloud", WIF_GCLOUD.replace(
        'if args[:3] == ["iam", "service-accounts", "add-iam-policy-binding"]:',
        'if args[:3] == ["iam", "service-accounts", "add-iam-policy-binding"] and "attribute.environment" in joined:\n'
        '    sys.exit(0)\n'
        'if args[:3] == ["iam", "service-accounts", "add-iam-policy-binding"]:'))
    result = tree.run("setup-wif.sh", "--apply", extra_env=env)
    assert result.returncode == 1
    assert "environment binding did not read back. Nothing was removed" in result.stderr
    calls = tree.tool_calls()
    assert not [c for c in calls if "remove-iam-policy-binding" in c or "update-oidc" in c]


def test_another_member_left_on_the_deployer_is_a_failure_and_the_condition_waits(tmp_path):
    tree, env = production_today(tmp_path)
    state = json.loads(state_path_of(tmp_path).read_text())
    other = f"principalSet://iam.googleapis.com/{POOL}/attribute.environment/production-backup"
    state["sa_bindings"][DEPLOYER_EMAIL].append(["roles/iam.workloadIdentityUser", other])
    state_path_of(tmp_path).write_text(json.dumps(state))
    plan = tree.run("setup-wif.sh", "--plan", extra_env=env)
    assert "WARN   milo-github-deployer has 1 other workloadIdentityUser member(s)" in plan.stdout
    result = tree.run("setup-wif.sh", "--apply", extra_env=env)
    assert result.returncode == 1
    assert "members are not exactly the production and production-kill-switch" in result.stderr
    assert "PASS   milo-github-deployer workloadIdentityUser members" not in result.stdout
    assert not [c for c in tree.tool_calls() if "update-oidc" in c]
    assert json.loads(state_path_of(tmp_path).read_text())["providers"]["github-actions"] == OLD_CONDITION


def test_a_provider_without_the_environment_mapping_changes_nothing(tmp_path):
    tree, env = production_today(tmp_path)
    state = json.loads(state_path_of(tmp_path).read_text())
    state["mappings"]["github-actions"].pop("attribute.environment")
    state_path_of(tmp_path).write_text(json.dumps(state))
    result = tree.run("setup-wif.sh", "--apply", extra_env=env)
    assert result.returncode == 1
    assert "does not map attribute.environment" in result.stderr
    assert not [c for c in tree.tool_calls()
                if "add-iam-policy-binding" in c and "attribute.environment" in c or "update-oidc" in c
                or "remove-iam-policy-binding" in c]


def test_no_workflow_outside_production_and_the_kill_switch_uses_the_deployer():
    assert not list((REPO / ".github").glob("actions/**/*.y*ml")), "a composite action would need this check too"
    for path in sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")]):
        text = path.read_text(encoding="utf-8")
        if "GCP_DEPLOY_SERVICE_ACCOUNT" not in text:
            continue
        (job,) = yaml.safe_load(text)["jobs"].values()
        assert job["environment"] in ("production", "production-kill-switch"), path.name
    backup = (WORKFLOWS / "backup-supabase-scheduled.yml").read_text(encoding="utf-8")
    assert "GCP_DEPLOY_SERVICE_ACCOUNT" not in backup


def test_an_unreadable_provider_is_a_failure_before_anything_changes(tmp_path):
    tree, env = production_today(tmp_path)
    tree.tool("gcloud", WIF_GCLOUD.replace(
        'if args[:4] == ["iam", "workload-identity-pools", "providers", "describe"]:',
        'if args[:4] == ["iam", "workload-identity-pools", "providers", "describe"] and "--format=json" in args:\n'
        '    sys.stderr.write("PERMISSION_DENIED\\n"); sys.exit(1)\n'
        'if args[:4] == ["iam", "workload-identity-pools", "providers", "describe"]:'))
    result = tree.run("setup-wif.sh", "--apply", extra_env=env)
    assert result.returncode == 1 and "could not be read" in result.stderr
    assert not [c for c in tree.tool_calls() if "iam-policy-binding" in c and "workloadIdentityUser" in c
                or "update-oidc" in c]


def test_a_failed_removal_leaves_the_condition_unchanged(tmp_path):
    tree, env = production_today(tmp_path)
    tree.tool("gcloud", WIF_GCLOUD.replace(
        'if args[:3] == ["iam", "service-accounts", "remove-iam-policy-binding"]:',
        'if args[:3] == ["iam", "service-accounts", "remove-iam-policy-binding"]:\n'
        '    sys.stderr.write("PERMISSION_DENIED\\n"); sys.exit(1)\n'
        'if False:'))
    result = tree.run("setup-wif.sh", "--apply", extra_env=env)
    assert result.returncode != 0
    assert not [c for c in tree.tool_calls() if "update-oidc" in c]
    assert json.loads(state_path_of(tmp_path).read_text())["providers"]["github-actions"] == OLD_CONDITION


def test_a_conditional_environment_binding_is_not_the_exact_member(tmp_path):
    tree, env = production_today(tmp_path)
    # Present, but only under an IAM condition: it must not count.
    tree.tool("gcloud", WIF_GCLOUD.replace(
        'print(json.dumps({"bindings": [{"role": r, "members": [m]} for r, m in bindings]}))',
        'print(json.dumps({"bindings": [dict({"role": r, "members": [m]}, **({"condition": {"title": "t"}} '
        'if "attribute.environment/production-kill-switch" in m else {})) for r, m in bindings]}))'))
    result = tree.run("setup-wif.sh", "--apply", extra_env=env)
    assert result.returncode == 1
    assert "members are not exactly the production and production-kill-switch" in result.stderr
    assert not [c for c in tree.tool_calls() if "update-oidc" in c]

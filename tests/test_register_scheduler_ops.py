"""PR-SYNC-2 operations, offline against a stand-in gcloud: the one-time
scheduler setup (scripts/ops/setup-register-scheduler.sh), the deploy's
scheduler identity step, the preflight's read-only check, and the kill switch.

Proves: --plan is stable, changes nothing and prints no secret; --apply
converges once (the Cloud Scheduler API, the keyless account, invoker on the
API ONLY, the hourly job with its OIDC token, deadline and no retries) and a
second run changes nothing; --check reads only and reports a missing job as
GAP, an unreadable one as UNREADABLE, a drifted job as GAP and an account
holding anything more than invoker on the API as FAIL; deploy.sh writes the
identity from the operator configuration; the kill switch closes register
capture on the API, which turns every tick into a skip (tests/
test_register_autosync.py).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from tests.test_ops_workflows import REPO, SENTINEL_FRAGMENTS, OpsTree

API_URL = "https://test-api-abc123-uc.a.run.app"
AUDIENCE = "https://test-api.example.test"  # OPERATOR_ENV's MILO_GATEWAY_AUDIENCE
SA = "milo-register-scheduler@test-project.iam.gserviceaccount.com"
MEMBER = f"serviceAccount:{SA}"

SCHEDULER_GCLOUD = r'''#!/usr/bin/env python3
"""gcloud with a tiny world: APIs, service accounts, Cloud Run IAM, Cloud Scheduler."""
import json, os, sys
args = sys.argv[1:]
path = os.environ["OPS_TEST_SCHEDULER_STATE"]
state = json.load(open(path)) if os.path.exists(path) else {}
state.setdefault("apis", []); state.setdefault("accounts", []); state.setdefault("jobs", {})
state.setdefault("run_policies", {"test-api": [], "test-other": []}); state.setdefault("project", [])
state.setdefault("keys", 0)
with open(os.environ["OPS_TEST_CALLS"], "a") as log:
    log.write("gcloud " + " ".join(args) + "\n")
def save():
    json.dump(state, open(path, "w"))
def flag(name):
    for i, a in enumerate(args):
        if a == name:
            return args[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None
def deny():
    sys.stderr.write("ERROR: (gcloud) PERMISSION_DENIED: caller lacks permission\n"); sys.exit(1)
def policy(bindings):
    roles = {}
    for role, member in bindings:
        roles.setdefault(role, []).append(member)
    return json.dumps({"bindings": [{"role": r, "members": m} for r, m in roles.items()]})
if args[:2] == ["auth", "list"]:
    print("owner@example.test"); sys.exit(0)
if args[:3] == ["config", "get-value", "project"]:
    print("test-project"); sys.exit(0)
if args[:2] == ["services", "list"]:
    print("\n".join(a for a in state["apis"] if a in (flag("--filter") or ""))); sys.exit(0)
if args[:2] == ["services", "enable"]:
    state["apis"].append(args[2]); save(); sys.exit(0)
if args[:3] == ["iam", "service-accounts", "describe"]:
    sys.exit(0 if args[3] in state["accounts"] else 1)
if args[:3] == ["iam", "service-accounts", "create"]:
    state["accounts"].append(args[3] + "@" + flag("--project") + ".iam.gserviceaccount.com"); save(); sys.exit(0)
if args[:4] == ["iam", "service-accounts", "keys", "list"]:
    if os.environ.get("OPS_TEST_KEYS_DENIED"): deny()
    print("\n".join(f"key{i}" for i in range(state["keys"]))); sys.exit(0)
if args[:3] == ["run", "services", "describe"]:
    print(os.environ.get("OPS_TEST_API_URL", "")); sys.exit(0)
if args[:3] == ["run", "services", "list"]:
    if os.environ.get("OPS_TEST_LIST_DENIED"): deny()
    print("\n".join(sorted(state["run_policies"]))); sys.exit(0)
if args[:3] == ["run", "services", "get-iam-policy"]:
    if os.environ.get("OPS_TEST_IAM_DENIED"): deny()
    print(policy(state["run_policies"].get(args[3], []))); sys.exit(0)
if args[:3] == ["run", "services", "add-iam-policy-binding"]:
    state["run_policies"][args[3]].append([flag("--role"), flag("--member")]); save(); sys.exit(0)
if args[:2] == ["projects", "get-iam-policy"]:
    if os.environ.get("OPS_TEST_IAM_DENIED"): deny()
    print(policy(state["project"])); sys.exit(0)
if args[:3] == ["scheduler", "jobs", "describe"]:
    if os.environ.get("OPS_TEST_SCHEDULER_DENIED"): deny()
    job = state["jobs"].get(args[3])
    if job is None:
        sys.stderr.write("ERROR: (gcloud.scheduler.jobs.describe) NOT_FOUND: Job not found.\n"); sys.exit(1)
    print(json.dumps(job)); sys.exit(0)
if args[:4] in (["scheduler", "jobs", "create", "http"], ["scheduler", "jobs", "update", "http"]):
    state["jobs"][args[4]] = {"schedule": flag("--schedule"), "timeZone": flag("--time-zone"),
        "attemptDeadline": flag("--attempt-deadline"), "state": "ENABLED",
        "retryConfig": {"retryCount": int(flag("--max-retry-attempts"))},
        "httpTarget": {"uri": flag("--uri"), "httpMethod": flag("--http-method"),
                       "oidcToken": {"serviceAccountEmail": flag("--oidc-service-account-email"),
                                     "audience": flag("--oidc-token-audience")}}}
    save(); sys.exit(0)
sys.stderr.write("unmocked gcloud " + " ".join(args) + "\n"); sys.exit(2)
'''

MUTATING = re.compile(r"services enable|service-accounts create|add-iam-policy-binding|jobs (create|update)"
                      r"|keys create|remove-iam-policy-binding|services update")


def scheduler_tree(tmp_path: Path) -> tuple[OpsTree, dict[str, str]]:
    tree = OpsTree(tmp_path)
    tree.tool("gcloud", SCHEDULER_GCLOUD)
    return tree, {"OPS_TEST_SCHEDULER_STATE": str(tmp_path / "scheduler.json"), "OPS_TEST_API_URL": API_URL}


def state(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "scheduler.json").read_text())


def mutations(tree: OpsTree) -> list[str]:
    return [call for call in tree.tool_calls() if MUTATING.search(call)]


def no_secret(*texts: str) -> None:
    for fragment in SENTINEL_FRAGMENTS:
        assert fragment not in "".join(texts)
    assert "owner@example.test" not in "".join(texts)  # the operator's own account


def verdict(output: str) -> str:
    (line,) = [line for line in output.splitlines() if line.startswith("SUMMARY|scheduler|")]
    return line.split("|", 3)[2]


# =============================================================================
# --plan
# =============================================================================

def test_the_plan_is_stable_changes_nothing_and_prints_no_secret(tmp_path):
    tree, env = scheduler_tree(tmp_path)
    first = tree.run("setup-register-scheduler.sh", extra_env=env)
    second = tree.run("setup-register-scheduler.sh", "--plan", extra_env=env)
    assert first.returncode == 0, first.stdout + first.stderr
    assert first.stdout == second.stdout and mutations(tree) == []
    for needle in ("ENABLE  cloudscheduler.googleapis.com",
                   "CREATE  service account milo-register-scheduler (no keys)",
                   "BIND    roles/run.invoker for milo-register-scheduler on test-api only",
                   f"CREATE  milo-register-sync-tick: hourly at :07 UTC, POST {API_URL}/internal/register/sync-tick",
                   "PLAN ONLY -- nothing was changed."):
        assert needle in first.stdout, needle
    assert re.findall(r"^SUMMARY\|(\w+)\|", first.stdout, re.M) == ["api", "account", "invoker", "job", "setup"]
    assert "WARN    MILO_GATEWAY_AUDIENCE is not the API URL" in first.stdout
    no_secret(first.stdout, first.stderr, tree.summary.read_text())


# =============================================================================
# --apply, then idempotent
# =============================================================================

def test_apply_converges_once_with_invoker_on_the_api_only(tmp_path):
    tree, env = scheduler_tree(tmp_path)
    env["OPS_TEST_API_URL"] = AUDIENCE  # production: the gateway audience IS the API URL
    applied = tree.run("setup-register-scheduler.sh", "--apply", extra_env=env)
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert "SUMMARY|scheduler|PASS|" in applied.stdout and "SUMMARY|setup|PASS|" in applied.stdout
    world = state(tmp_path)
    assert world["apis"] == ["cloudscheduler.googleapis.com"] and world["accounts"] == [SA]
    assert world["run_policies"] == {"test-api": [["roles/run.invoker", MEMBER]], "test-other": []}
    assert world["project"] == [] and world["keys"] == 0
    assert world["jobs"]["milo-register-sync-tick"] == {
        "schedule": "7 * * * *", "timeZone": "Etc/UTC", "attemptDeadline": "60s", "state": "ENABLED",
        "retryConfig": {"retryCount": 0},
        "httpTarget": {"uri": f"{AUDIENCE}/internal/register/sync-tick", "httpMethod": "POST",
                       "oidcToken": {"serviceAccountEmail": SA, "audience": AUDIENCE}}}
    assert not any("keys create" in call for call in tree.tool_calls())
    before = len(mutations(tree))
    again = tree.run("setup-register-scheduler.sh", "--apply", extra_env=env)
    assert again.returncode == 0 and len(mutations(tree)) == before
    plan = tree.run("setup-register-scheduler.sh", extra_env=env)
    assert "DRY-RUN" not in plan.stdout and plan.stdout.count("\nOK      ") + plan.stdout.startswith("OK") >= 4
    no_secret(applied.stdout, applied.stderr, again.stdout, plan.stdout)


def test_apply_corrects_a_drifted_job(tmp_path):
    tree, env = scheduler_tree(tmp_path)
    env["OPS_TEST_API_URL"] = AUDIENCE
    assert tree.run("setup-register-scheduler.sh", "--apply", extra_env=env).returncode == 0
    world = state(tmp_path)
    world["jobs"]["milo-register-sync-tick"]["retryConfig"] = {"retryCount": 3}
    world["jobs"]["milo-register-sync-tick"]["httpTarget"]["uri"] = "https://elsewhere.test/x"
    (tmp_path / "scheduler.json").write_text(json.dumps(world))
    check = tree.run("setup-register-scheduler.sh", "--check", extra_env=env)
    assert verdict(check.stdout) == "GAP" and "differs in: url retries" in check.stdout
    plan = tree.run("setup-register-scheduler.sh", extra_env=env)
    assert "UPDATE  milo-register-sync-tick (url retries)" in plan.stdout
    assert tree.run("setup-register-scheduler.sh", "--apply", extra_env=env).returncode == 0
    assert state(tmp_path)["jobs"]["milo-register-sync-tick"]["retryConfig"] == {"retryCount": 0}


# =============================================================================
# --check: read-only verdicts
# =============================================================================

def test_check_reads_only_and_reports_each_state(tmp_path):
    tree, env = scheduler_tree(tmp_path)
    env["OPS_TEST_API_URL"] = AUDIENCE
    missing = tree.run("setup-register-scheduler.sh", "--check", extra_env=env)
    assert verdict(missing.stdout) == "GAP" and "does not exist" in missing.stdout
    assert tree.run("setup-register-scheduler.sh", "--apply", extra_env=env).returncode == 0
    calls = len(tree.tool_calls())
    assert verdict(tree.run("setup-register-scheduler.sh", "--check", extra_env=env).stdout) == "PASS"
    denied = tree.run("setup-register-scheduler.sh", "--check", extra_env={**env, "OPS_TEST_SCHEDULER_DENIED": "1"})
    assert verdict(denied.stdout) == "UNREADABLE" and "Verify from Cloud Shell" in denied.stdout
    iam = tree.run("setup-register-scheduler.sh", "--check", extra_env={**env, "OPS_TEST_IAM_DENIED": "1"})
    assert verdict(iam.stdout) == "UNREADABLE"
    # A listing it cannot read never reads as "no extra grant" or "no key".
    for unreadable in ("OPS_TEST_KEYS_DENIED", "OPS_TEST_LIST_DENIED"):
        check = tree.run("setup-register-scheduler.sh", "--check", extra_env={**env, unreadable: "1"})
        assert verdict(check.stdout) == "UNREADABLE", (unreadable, check.stdout)
    assert not [call for call in tree.tool_calls()[calls:] if MUTATING.search(call)]
    # Anything more than invoker on the API is a FAIL: on the project, on another service, a key.
    for widen in ({"project": [["roles/viewer", MEMBER]]},
                  {"run_policies": {"test-api": [["roles/run.invoker", MEMBER], ["roles/run.admin", MEMBER]],
                                    "test-other": []}},
                  {"run_policies": {"test-api": [["roles/run.invoker", MEMBER]],
                                    "test-other": [["roles/run.invoker", MEMBER]]}},
                  {"keys": 1}):
        world = state(tmp_path)
        saved = json.loads(json.dumps(world))
        world.update(widen)
        (tmp_path / "scheduler.json").write_text(json.dumps(world))
        check = tree.run("setup-register-scheduler.sh", "--check", extra_env=env)
        assert verdict(check.stdout) == "FAIL", (widen, check.stdout)
        (tmp_path / "scheduler.json").write_text(json.dumps(saved))
    no_secret(missing.stdout, denied.stdout, iam.stdout)


def test_the_preflight_maps_the_check_like_the_archive_check():
    text = (REPO / "scripts" / "deploy" / "production-preflight.sh").read_text(encoding="utf-8")
    block = text[text.index("SCHEDULER_CHECK="):text.index("# Gateway / frontend binding.")]
    assert "setup-register-scheduler.sh\" --check" in block
    assert 'PASS) record_check PASS "scheduler:register-sync-tick"' in block
    assert 'FAIL) record_check BLOCKED "scheduler:register-sync-tick"' in block
    assert 'UNREADABLE) record_check WARN "scheduler:register-sync-tick"' in block
    assert '*) record_check WARN "scheduler:register-sync-tick"' in block


# =============================================================================
# deploy.sh writes the identity; the kill switch closes the register on the API
# =============================================================================

def test_the_deploy_writes_the_scheduler_identity_from_the_operator_config(tmp_path):
    tree = OpsTree(tmp_path)
    result = tree.run("deploy.sh", "--sha", tree.sha, "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert tree.tool_calls() == []
    assert ("DRY-RUN: gcloud run services update test-api --region test-region --project test-project "
            f"--update-env-vars MILO_REGISTER_SCHEDULER_IDENTITY={SA}") in result.stdout
    assert "SUMMARY|9b scheduler-identity|DRY-RUN|" in result.stdout
    order = [line.split("|")[1] for line in result.stdout.splitlines() if line.startswith("SUMMARY|")]
    assert order.index("9 worker-contract") < order.index("9b scheduler-identity") < order.index("10 deployed-gate")


def test_the_kill_switch_closes_register_capture_on_the_api_so_ticks_skip(tmp_path):
    tree = OpsTree(tmp_path)
    result = tree.run("kill-switch.sh", "--vercel-deployment", "https://milo-abc.vercel.app", "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    api_updates = [line for line in result.stdout.splitlines()
                   if "services update" in line and "test-api" in line]
    assert any("MILO_ENABLE_REGISTER_CAPTURE=false" in line for line in api_updates), result.stdout

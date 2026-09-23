"""The Production rollout of the Mapping Plan -> prepared batch -> Swarm V2 path.

What this proves, offline (no cloud, no database, no network):

1.  The stage contract (`scripts/deploy/deployment-contract.sh`): each flag is
    enabled only on the component and stage that needs it -- the Mapping Plan
    flags on the API, the Government read and paid execution on the worker,
    scoped preparation on neither (the capture job alone, one execution), and
    promotion nowhere. The RPC inventory the readiness check requires is the
    one the migrations create and the repository calls.
2.  `website-execution-activate.sh`: Stage P opens plan writes only; Stage 2
    applies exactly the contract, only behind a passing `prepared` gate for a
    NAMED plan revision, and reads every value back.
3.  `production-activate.sh`: the release is deployed (images built) before
    anything can capture; `--all` never captures and never starts anything;
    a resumed sequence skips what is already done instead of redoing it.
4.  `website-execution-check.sh`: VERIFIED only from actual values and the
    deployed website's observable behaviour; a missing access, a failed
    request or a variable that merely EXISTS is never a YES.
5.  `production-verify.sh`: the exact migration set, the named revision's
    scoped readiness and every other fact are reported separately, and a gate
    passes only when every fact it requires is VERIFIED.

The real SQL behind (1) and (5) runs in `tests/test_migrations_postgres.py`.
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

REPO = Path(__file__).resolve().parents[1]
DEPLOY = REPO / "scripts" / "deploy"
CONTRACT = DEPLOY / "deployment-contract.sh"
ACTIVATE = DEPLOY / "website-execution-activate.sh"
ORCHESTRATE = DEPLOY / "production-activate.sh"
CHECK = DEPLOY / "website-execution-check.sh"
VERIFY = DEPLOY / "production-verify.sh"
READINESS = DEPLOY / "work-scope-readiness.sh"

WS_ID = "11111111-2222-4333-8444-555555555555"
WS_DIGEST = "d" * 64
WS_ARGS = ["--work-scope-id", WS_ID, "--work-scope-revision", "2", "--work-scope-digest", WS_DIGEST]


def contract_array(name: str) -> list[str]:
    out = subprocess.run(
        ["bash", "-c", f'source "{CONTRACT}"; printf "%s\\n" "${{{name}[@]}}"'],
        capture_output=True, text=True, check=True).stdout
    return [line for line in out.splitlines() if line]


def contract_value(name: str) -> str:
    return subprocess.run(["bash", "-c", f'source "{CONTRACT}"; printf "%s" "${name}"'],
                          capture_output=True, text=True, check=True).stdout


# =============================================================================
# 1. the stage contract
# =============================================================================

def test_each_flag_is_enabled_only_where_it_is_required():
    api_on = set(contract_array("MILO_STAGE2_API_ENABLE_FLAGS"))
    api_off = set(contract_array("MILO_STAGE2_API_PINNED_OFF_FLAGS"))
    job_on = set(contract_array("MILO_STAGE2_WORKER_ENABLE_FLAGS"))
    job_off = set(contract_array("MILO_STAGE2_WORKER_PINNED_OFF_FLAGS"))
    plan = contract_array("MILO_PLAN_AUTHORING_API_ENABLE_FLAGS")

    # Finding 1: the batch path's two API flags are opened -- on the API.
    assert {"MILO_ENABLE_WORK_SCOPE_MUTATIONS", "MILO_ENABLE_WORK_SCOPE_BATCHES",
            "MILO_ENABLE_RUN_CREATION"} <= api_on
    # ... and never on the worker, which neither authors plans nor starts batches.
    assert not {"MILO_ENABLE_WORK_SCOPE_MUTATIONS", "MILO_ENABLE_WORK_SCOPE_BATCHES"} & job_on
    # Scoped preparation belongs to ONE capture-job execution: pinned off on
    # both product surfaces at every stage.
    assert "MILO_ENABLE_WORK_SCOPE_PREPARATION" in api_off & job_off
    assert "MILO_ENABLE_WORK_SCOPE_PREPARATION" not in api_on | job_on | set(plan)
    # Promotion is a separate decision: pinned off on both.
    assert "MILO_ENABLE_CATALOG_PROMOTION" in api_off & job_off
    # Paid execution and the provider live on the worker only.
    assert "MILO_ENABLE_PAID_EXECUTION" in job_on and "MILO_ENABLE_PAID_EXECUTION" in api_off
    assert {"MILO_ENABLE_CATALOG_EXECUTION", "MILO_ENABLE_GOVERNMENT_CATALOG_READ"} <= job_on
    # The API mirrors the Government read so run creation can route ordinary
    # Swarm V2 runs to the Mapping Plan (CATALOG_RUN_REQUIRES_MAPPING_PLAN).
    assert {"MILO_ENABLE_CATALOG_EXECUTION", "MILO_ENABLE_GOVERNMENT_CATALOG_READ"} <= api_on
    # Stage P opens plan authoring and NOTHING that can start a run.
    assert plan == ["MILO_ENABLE_WORK_SCOPE_MUTATIONS"]
    # No flag is both on and off on one surface, and every one is a real flag.
    assert not api_on & api_off and not job_on & job_off
    from backend.production_config import EXECUTION_FLAGS
    assert (api_on | api_off | job_on | job_off | set(plan)) <= set(EXECUTION_FLAGS)
    # The capture job keeps preparation pinned off in its definition.
    assert "MILO_ENABLE_WORK_SCOPE_PREPARATION=false" in contract_array("MILO_CAPTURE_PINNED_OFF_FLAGS")


def test_the_api_routing_mirror_matches_the_workers_own_gate():
    """The API refuses an unbound Swarm V2 run exactly when the worker would."""
    from backend.catalog.execution import government_read_enabled
    from backend.catalog.scope.service import direct_run_blocker
    on = set(contract_array("MILO_STAGE2_API_ENABLE_FLAGS"))
    env = {name: "true" for name in on}
    assert government_read_enabled(env) is True
    import os as _os
    saved = {k: _os.environ.get(k) for k in env}
    try:
        _os.environ.update(env)
        assert direct_run_blocker("swarm_v2") == "catalog_batch_required"
        assert direct_run_blocker("vehicle_catalog_v1") is None
    finally:
        for key, value in saved.items():
            if value is None:
                _os.environ.pop(key, None)
            else:
                _os.environ[key] = value


def test_the_readiness_rpc_inventory_is_the_migrations_and_the_repositorys():
    rpcs = contract_array("MILO_WORK_SCOPE_RPCS")
    tables = contract_array("MILO_WORK_SCOPE_TABLES")
    migrations = "\n".join((REPO / "supabase" / "migrations" / name).read_text(encoding="utf-8")
                           for name in ("20260922000100_catalog_work_scopes.sql",
                                        "20260923000100_catalog_work_scope_preparation.sql",
                                        "20260924000100_catalog_work_scope_batch_runs.sql"))
    repository = (REPO / "backend" / "repository" / "supabase.py").read_text(encoding="utf-8")
    for rpc in rpcs:
        assert f"create or replace function public.{rpc}(" in migrations, rpc
        assert f'rpc("{rpc}"' in repository, f"{rpc} is not called by the runtime"
    # Every work-scope RPC the runtime calls is in the inventory.
    called = set(re.findall(r'rpc\("([a-z_]*work_scope[a-z_]*)"', repository))
    assert called <= set(rpcs), called - set(rpcs)
    for table in tables:
        assert f"create table if not exists public.{table} (" in migrations, table


# =============================================================================
# harness: a throwaway copy of the scripts, with stand-ins on PATH
# =============================================================================

OPERATOR_ENV = (
    "GCP_PROJECT_ID=test-project\nGCP_REGION=test-region\nARTIFACT_REGISTRY_REPOSITORY=test-repo\n"
    "CLOUD_RUN_API_SERVICE=test-api\nCLOUD_RUN_WORKER_JOB=test-worker\n"
    "CLOUD_RUN_CAPTURE_JOB=test-capture\n"
    "API_SERVICE_ACCOUNT=api@test.iam.gserviceaccount.com\n"
    "WORKER_SERVICE_ACCOUNT=worker@test.iam.gserviceaccount.com\n"
    "SUPABASE_PROJECT_REF=abcdefghijklmnopqrst\n"
    "SECRET_SUPABASE_URL=TEST_SUPABASE_URL\nSECRET_SUPABASE_SERVICE_KEY=TEST_SUPABASE_KEY\n"
    "SECRET_REDIS_URL=TEST_REDIS_URL\nSECRET_REDIS_TOKEN=TEST_REDIS_TOKEN\n"
    "SECRET_PROVIDER_API_KEY=TEST_PROVIDER_KEY\n"
    "PRODUCTION_ORIGIN=https://site.test\n"
    "MILO_GATEWAY_AUDIENCE=https://api.test\n"
    "MILO_APPROVED_GATEWAY_IDENTITIES=gateway@test.iam.gserviceaccount.com\n"
    "MILO_WORKER_AUDIENCE=https://api.test\n"
    "READONLY_DATABASE_URL_ENV=MILO_TEST_RO_DB_URL\n")


def _executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


LOGGING_STUB = """#!/usr/bin/env bash
printf '%s %s\\n' "$(basename "$0")" "$*" >> "$MILO_TEST_LOG"
{extra}
"""


class Tree:
    """A git-backed copy of the deploy scripts under tmp_path.

    `real` scripts are copied verbatim; every other script a real one invokes
    is a logging stub whose behaviour a test sets with `stub()`.
    """

    def __init__(self, tmp_path: Path, real: tuple[str, ...]):
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.root = tmp_path / "repo"
        self.log = tmp_path / "calls.log"
        self.log.write_text("", encoding="utf-8")
        deploy = self.root / "scripts" / "deploy"
        deploy.mkdir(parents=True)
        for name in ("operator-config.sh", "deployment-contract.sh", *real):
            shutil.copy2(DEPLOY / name, deploy / name)
        shutil.copytree(REPO / "backend", self.root / "backend",
                        ignore=shutil.ignore_patterns("__pycache__"))
        (self.root / "frontend").mkdir()
        self.config = tmp_path / "operator.env"
        self.config.write_text(OPERATOR_ENV, encoding="utf-8")
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        for command in ("git", "python3", "bash", "awk", "grep", "sed", "cat", "date", "tee",
                        "tail", "head", "mktemp", "rm", "basename", "dirname", "tr", "sort",
                        "env", "printf", "chmod", "mv"):
            found = shutil.which(command)
            if found:
                os.symlink(found, self.bin / command)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-q", "--allow-empty", "-m", "release"], check=True)
        self.sha = subprocess.run(["git", "-C", str(self.root), "rev-parse", "HEAD"],
                                  capture_output=True, text=True, check=True).stdout.strip()

    def stub(self, relative: str, extra: str = "exit 0") -> None:
        _executable(self.root / relative, LOGGING_STUB.format(extra=extra))

    def tool(self, name: str, source: str) -> None:
        _executable(self.bin / name, source)

    def run(self, script: str, *args: str, env: dict[str, str] | None = None,
            timeout: int = 120) -> subprocess.CompletedProcess:
        environment = {"PATH": str(self.bin), "HOME": str(self.root), "MILO_TEST_LOG": str(self.log),
                       "TMPDIR": str(self.root.parent), **(env or {})}
        return subprocess.run(["bash", str(self.root / "scripts" / "deploy" / script),
                               "--operator-config", str(self.config), *args],
                              capture_output=True, text=True, env=environment, timeout=timeout)

    def calls(self) -> list[str]:
        return [line for line in self.log.read_text(encoding="utf-8").splitlines() if line]


# =============================================================================
# 2. website-execution-activate.sh
# =============================================================================

def _plan_lines(tree: Tree) -> dict[str, str]:
    result = tree.run("website-execution-activate.sh", "--plan")
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    commands: dict[str, str] = {}
    for index, line in enumerate(lines):
        if line.startswith("# --- Stage P, API"):
            commands["plan_api"] = lines[index + 3]
        elif line.startswith("# --- Stage 2, API"):
            commands["api"] = lines[index + 6]
        elif line.startswith("# --- Stage 2, worker: execution"):
            commands["job"] = lines[index + 4]
    return commands


def test_the_activation_plan_puts_each_flag_on_its_component(tmp_path):
    tree = Tree(tmp_path, ("website-execution-activate.sh",))
    commands = _plan_lines(tree)
    api, job, plan_api = commands["api"], commands["job"], commands["plan_api"]
    for flag in ("MILO_ENABLE_WORK_SCOPE_MUTATIONS", "MILO_ENABLE_WORK_SCOPE_BATCHES",
                 "MILO_ENABLE_RUN_CREATION", "MILO_ENABLE_GOVERNMENT_CATALOG_READ"):
        assert f"{flag}=true" in api
    for flag in ("MILO_ENABLE_WORK_SCOPE_PREPARATION", "MILO_ENABLE_PAID_EXECUTION",
                 "MILO_ENABLE_CATALOG_PROMOTION"):
        assert f"{flag}=false" in api
    assert "JOB_LAUNCHER=cloud_run" in api
    for flag in ("MILO_ENABLE_PAID_EXECUTION", "MILO_ENABLE_GOVERNMENT_CATALOG_READ",
                 "MILO_ENABLE_EXECUTION_CONTROL"):
        assert f"{flag}=true" in job
    for flag in ("MILO_ENABLE_WORK_SCOPE_MUTATIONS", "MILO_ENABLE_WORK_SCOPE_BATCHES",
                 "MILO_ENABLE_WORK_SCOPE_PREPARATION", "MILO_ENABLE_CATALOG_PROMOTION"):
        assert f"{flag}=false" in job
    # The reviewed RuntimePolicy rides with Stage 2 on the worker.
    assert "MILO_MAX_MODEL_CALLS_PER_RUN=150" in job and "MILO_PROVIDER_TPM_LIMIT=" in job
    # Stage P: exactly one flag, on the API.
    assert plan_api.strip() == "--update-env-vars '^;^MILO_ENABLE_WORK_SCOPE_MUTATIONS=true'"


@pytest.mark.parametrize("args,message", [
    (("--apply-backend",), "requires --work-scope-id"),
    (("--apply-backend", "--skip-stage1-check", *WS_ARGS), "refused with --apply-backend"),
    (("--apply-plan-authoring", "--skip-stage1-check"), "refused with --apply-plan-authoring"),
])
def test_stage2_is_never_applied_without_a_named_prepared_revision(tmp_path, args, message):
    tree = Tree(tmp_path, ("website-execution-activate.sh",))
    tree.tool("gcloud", LOGGING_STUB.format(extra="exit 0"))
    result = tree.run("website-execution-activate.sh", *args)
    assert result.returncode == 2
    assert message in result.stderr
    assert tree.calls() == []


STATEFUL_GCLOUD = r'''#!/usr/bin/env python3
"""A gcloud stand-in whose describes answer what its updates applied.

MILO_TEST_GCLOUD_FAIL names a substring: an update whose arguments contain it
fails WITHOUT applying (a partial gcloud failure). After every applied update a
snapshot of the whole state is appended to MILO_TEST_GCLOUD_SNAPSHOTS, so a
test can inspect every intermediate posture the sequence passed through.
"""
import json, os, sys
state_path = os.environ["MILO_TEST_GCLOUD_STATE"]
state = json.load(open(state_path)) if os.path.exists(state_path) else {"service": {}, "job": {}}
state.setdefault("secrets", {"service": {}, "job": {}})
args = sys.argv[1:]
with open(os.environ["MILO_TEST_LOG"], "a") as log:
    log.write("gcloud " + " ".join(args) + "\n")
if args[:2] == ["auth", "list"]:
    print("operator@test"); sys.exit(0)
if args[:3] == ["config", "get-value", "project"]:
    print("test-project"); sys.exit(0)
kind = "service" if args[:2] == ["run", "services"] else "job" if args[:2] == ["run", "jobs"] else None
if kind and args[2] == "update":
    fail = os.environ.get("MILO_TEST_GCLOUD_FAIL")
    if fail and fail in " ".join(args):
        sys.stderr.write("ERROR: (gcloud.run) simulated failure\n"); sys.exit(1)
    for flag, value in zip(args, args[1:]):
        if flag == "--update-env-vars":
            delim, body = value[1], value[3:]
            for pair in body.split(delim):
                k, v = pair.split("=", 1)
                state[kind][k] = v
        if flag == "--update-secrets":
            for pair in value.split(","):
                k, ref = pair.split("=", 1)
                state["secrets"][kind][k] = ref.split(":", 1)[0]
    json.dump(state, open(state_path, "w"))
    snapshots = os.environ.get("MILO_TEST_GCLOUD_SNAPSHOTS")
    if snapshots:
        with open(snapshots, "a") as handle:
            handle.write(json.dumps(state) + "\n")
    sys.exit(0)
if kind and args[2] == "describe":
    if "--format=json" in args:
        env = [{"name": k, "value": v} for k, v in state[kind].items()]
        env += [{"name": k, "valueFrom": {"secretKeyRef": {"name": v, "key": "latest"}}}
                for k, v in state["secrets"][kind].items()]
        doc = {"spec": {"template": {"spec": {"containers": [{"env": env}]}}}}
        print(json.dumps(doc)); sys.exit(0)
    sys.exit(0)
sys.exit(0)
'''

#: What the pre-check reads: the website's run-start posture.
RUN_START_CLOSED = "echo 'GATEWAY_RUN_START_ENABLED=DISABLED (every run start is refused)'; exit 1"


def _stage2_tree(tmp_path, *, website: str = RUN_START_CLOSED, gate: str = "exit 0"):
    tree = Tree(tmp_path, ("website-execution-activate.sh",))
    tree.tool("gcloud", STATEFUL_GCLOUD)
    tree.stub("scripts/deploy/production-verify.sh", gate)
    tree.stub("scripts/deploy/website-execution-check.sh", website)
    env = {"MILO_TEST_GCLOUD_STATE": str(tmp_path / "state.json"),
           "MILO_TEST_GCLOUD_SNAPSHOTS": str(tmp_path / "snapshots.jsonl")}
    return tree, env


def _snapshots(tmp_path) -> list[dict]:
    path = tmp_path / "snapshots.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _website_could_start_a_run(state: dict, *, run_start_gateway_open: bool) -> bool:
    # A website start needs the gateway's run-start permission AND the API's
    # run creation + batches. The activation script never opens the gateway.
    service = state.get("service", {})
    return (run_start_gateway_open and service.get("MILO_ENABLE_RUN_CREATION") == "true"
            and service.get("MILO_ENABLE_WORK_SCOPE_BATCHES") == "true")


def test_stage2_applies_only_behind_the_prepared_gate_and_reads_every_value_back(tmp_path):
    tree, env = _stage2_tree(tmp_path, gate="exit 1")

    # The gate refuses: NOTHING is updated.
    refused = tree.run("website-execution-activate.sh", "--apply-backend", *WS_ARGS, env=env)
    assert refused.returncode == 1 and "Nothing was changed" in refused.stderr
    assert not [c for c in tree.calls() if " update " in c]
    gate = [c for c in tree.calls() if c.startswith("production-verify.sh")]
    assert gate and "--gate prepared" in gate[0] and f"--work-scope-id {WS_ID}" in gate[0]

    # The gate passes: the WORKER (env, then secret) first, then the API.
    tree.stub("scripts/deploy/production-verify.sh", "exit 0")
    applied = tree.run("website-execution-activate.sh", "--apply-backend", *WS_ARGS, env=env)
    assert applied.returncode == 0, applied.stdout + applied.stderr
    updates = [c for c in tree.calls() if " update " in c]
    assert updates[0].startswith("gcloud run jobs update test-worker") and "--update-env-vars" in updates[0]
    assert updates[1].endswith("--update-secrets KIMI_API_KEY=TEST_PROVIDER_KEY:latest")
    assert updates[2].startswith("gcloud run services update test-api")
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["service"]["MILO_ENABLE_WORK_SCOPE_BATCHES"] == "true"
    assert state["service"]["MILO_ENABLE_WORK_SCOPE_PREPARATION"] == "false"
    assert state["job"]["MILO_ENABLE_WORK_SCOPE_PREPARATION"] == "false"
    assert state["job"]["MILO_ENABLE_CATALOG_PROMOTION"] == "false"
    assert state["secrets"]["job"] == {"KIMI_API_KEY": "TEST_PROVIDER_KEY"}
    assert state["secrets"]["service"] == {} and "KIMI_API_KEY" not in state["service"]
    # The website is still closed: the last step is printed, never applied.
    assert "The website STILL REFUSES every run start" in applied.stdout
    assert "--gate armed" in applied.stdout
    assert "vercel env add GATEWAY_ALLOW_RUN_START_ROUTES production" in applied.stdout
    assert not any(c.startswith("vercel") for c in tree.calls())


@pytest.mark.parametrize("website,why", [
    ("echo 'GATEWAY_RUN_START_ENABLED=VERIFIED (the gateway proxies run starts)'; exit 0", "open"),
    ("echo 'GATEWAY_RUN_START_ENABLED=UNVERIFIED (the run-start posture could not be proved)'; exit 1",
     "unprovable"),
    ("echo 'FRONTEND_CODE_WIRED=VERIFIED'; exit 1", "absent"),
])
def test_stage2_refuses_unless_the_website_run_start_path_is_proved_closed(tmp_path, website, why):
    tree, env = _stage2_tree(tmp_path, website=website)
    result = tree.run("website-execution-activate.sh", "--apply-backend", *WS_ARGS, env=env)
    assert result.returncode == 1, why
    assert "run-start path is not VERIFIED CLOSED" in result.stderr
    assert not [c for c in tree.calls() if " update " in c]
    # Refused before the gate or anything else ran.
    assert not any(c.startswith("production-verify.sh") for c in tree.calls())


def test_no_intermediate_posture_can_start_a_run_from_the_website(tmp_path):
    '''A person clicking "Start batch" at ANY moment of Stage 2 is refused.

    Every posture the sequence passes through is inspected. The gateway's
    run-start permission is never opened by the script, so the website
    refuses every start at every step; and even judged by the backend alone,
    the API never allows a start before the worker is fully armed.
    '''
    tree, env = _stage2_tree(tmp_path)
    assert tree.run("website-execution-activate.sh", "--apply-backend", *WS_ARGS,
                    env=env).returncode == 0
    snapshots = _snapshots(tmp_path)
    assert len(snapshots) == 3
    for state in snapshots:
        assert not _website_could_start_a_run(state, run_start_gateway_open=False)
        if state["service"].get("MILO_ENABLE_RUN_CREATION") == "true":
            assert state["job"].get("MILO_ENABLE_PAID_EXECUTION") == "true"
            assert state["secrets"]["job"].get("KIMI_API_KEY") == "TEST_PROVIDER_KEY"
    # Only the final posture has the API armed.
    assert [s["service"].get("MILO_ENABLE_RUN_CREATION") for s in snapshots] == [None, None, "true"]


@pytest.mark.parametrize("failing,worker_env,worker_secret", [
    ("--update-env-vars ^;^MILO_ENABLE_EXECUTION_CONTROL", False, False),  # worker env
    ("--update-secrets", True, False),                                     # worker secret
    ("run services update", True, True),                                   # the API
])
def test_a_partial_gcloud_failure_never_arms_the_api_ahead_of_the_worker(
        tmp_path, failing, worker_env, worker_secret):
    tree, env = _stage2_tree(tmp_path)
    env["MILO_TEST_GCLOUD_FAIL"] = failing
    result = tree.run("website-execution-activate.sh", "--apply-backend", *WS_ARGS, env=env)
    assert result.returncode == 1
    assert "the website still refuses every run start" in result.stderr
    state = json.loads((tmp_path / "state.json").read_text()) if (tmp_path / "state.json").exists() \
        else {"service": {}, "job": {}, "secrets": {"service": {}, "job": {}}}
    # The API never allows a run start after a failed step.
    assert state["service"].get("MILO_ENABLE_RUN_CREATION") != "true"
    assert (state["job"].get("MILO_ENABLE_PAID_EXECUTION") == "true") == worker_env
    assert ("KIMI_API_KEY" in state.get("secrets", {}).get("job", {})) == worker_secret
    for snapshot in _snapshots(tmp_path):
        assert not _website_could_start_a_run(snapshot, run_start_gateway_open=False)


def test_a_worker_whose_secret_does_not_read_back_stops_before_the_api(tmp_path):
    tree, env = _stage2_tree(tmp_path)
    # gcloud reports success but the binding is not what was asked for.
    state = tmp_path / "state.json"
    wrapper = STATEFUL_GCLOUD.replace(
        'state["secrets"][kind][k] = ref.split(":", 1)[0]',
        'state["secrets"][kind][k] = "SOME_OTHER_SECRET"')
    tree.tool("gcloud", wrapper)
    result = tree.run("website-execution-activate.sh", "--apply-backend", *WS_ARGS, env=env)
    assert result.returncode == 1
    assert "MISMATCH KIMI_API_KEY" in result.stdout
    assert "the API was NOT changed" in result.stderr
    assert "MILO_ENABLE_RUN_CREATION" not in json.loads(state.read_text())["service"]


def test_stage_p_opens_plan_writes_and_proves_run_creation_is_still_off(tmp_path):
    tree, env = _stage2_tree(tmp_path)
    state = tmp_path / "state.json"
    # A live API with run creation ON is not a Stage P posture: refused on read-back.
    state.write_text(json.dumps({"service": {"MILO_ENABLE_RUN_CREATION": "true",
                                             "MILO_ENABLE_WORK_SCOPE_BATCHES": "false"},
                                 "job": {}}))
    wrong = tree.run("website-execution-activate.sh", "--apply-plan-authoring", env=env)
    assert wrong.returncode == 1 and "MISMATCH MILO_ENABLE_RUN_CREATION" in wrong.stdout
    state.write_text(json.dumps({"service": {"MILO_ENABLE_RUN_CREATION": "false",
                                             "MILO_ENABLE_WORK_SCOPE_BATCHES": "false"},
                                 "job": {}}))
    ok = tree.run("website-execution-activate.sh", "--apply-plan-authoring", env=env)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "--gate deployed" in next(c for c in tree.calls() if c.startswith("production-verify.sh"))
    job_updates = [c for c in tree.calls() if c.startswith("gcloud run jobs update")]
    assert job_updates == [], "Stage P never touches the worker"
    # Its Vercel half opens plan writes and REMOVES the run-start permission.
    assert "vercel env add GATEWAY_ALLOW_EXECUTION_ROUTES production" in ok.stdout
    assert "vercel env rm GATEWAY_ALLOW_RUN_START_ROUTES production --yes" in ok.stdout
    assert "vercel env add GATEWAY_ALLOW_RUN_START_ROUTES" not in ok.stdout


# =============================================================================
# 3. production-activate.sh: the order, and resuming
# =============================================================================

# What work-scope-readiness.sh prints for a valid, never-prepared head revision.
UNPREPARED = (
    "echo 'DATABASE_READ=VERIFIED (read-only role sees service-only rows)'\n"
    "echo 'WORK_SCOPE_SCHEMA=VERIFIED (7 tables with RLS, 9 RPCs service_role-only)'\n"
    f"echo 'WORK_SCOPE_ID={WS_ID}'\n"
    "echo 'WORK_SCOPE_PLAN=VERIFIED (revision 2 is the open head with that exact digest)'\n"
    "echo 'WORK_SCOPE_PREPARED=NO (revision 2 has not been prepared. Run the scoped preparation)'\n"
    "echo 'EVIDENCE_READY=NO (no scoped Government snapshot is linked to this revision)'\n"
    "echo 'BATCH_READY=NO (no batch exists for this revision)'\n"
    "echo 'WORK_SCOPE_READINESS=NO (3 fact(s) NO, 0 UNVERIFIED)'\n"
    "exit 1")


def _orchestrator(tmp_path, *, deployed: bool = False, database_ok: bool = True,
                  prepared: bool = False, readiness: str | None = None,
                  real_readiness: bool = False) -> Tree:
    real = ("production-activate.sh", "work-scope-readiness.sh") if real_readiness \
        else ("production-activate.sh",)
    tree = Tree(tmp_path, real)
    tree.stub("scripts/deploy/production-preflight.sh")
    verify = ('if [[ "$*" == *"--gate database"* ]]; then exit {db}; fi\n'
              'if [[ "$*" == *"--gate deployed"* ]]; then {deployed}; exit 0; fi\n'
              'exit 0').format(db=0 if database_ok else 1,
                               deployed="echo CODE_DEPLOYED=VERIFIED" if deployed
                               else "echo CODE_DEPLOYED=NO")
    tree.stub("scripts/deploy/production-verify.sh", verify)
    tree.stub("scripts/deploy/cloud-run.sh",
              'printf "cloud-run.sh DEPLOY_MODE=%s PROJECT_ID=%s JOB_LAUNCHER_MODE=%s REF=%s CORS=%s\\n" '
              '"$DEPLOY_MODE" "$PROJECT_ID" "$JOB_LAUNCHER_MODE" "$MILO_EXPECTED_SUPABASE_PROJECT_REF" '
              '"$ALLOWED_CORS_ORIGINS" >> "$MILO_TEST_LOG"')
    tree.stub("scripts/deploy/website-execution-activate.sh")
    if not real_readiness:
        tree.stub("scripts/deploy/work-scope-readiness.sh",
                  readiness if readiness is not None
                  else "echo WORK_SCOPE_PREPARED=VERIFIED; exit 0" if prepared
                  else UNPREPARED)
    tree.stub("scripts/catalog/government-production-capture.sh",
              'if [[ "$*" == *"--prepare "* || "$*" == *"--prepare" ]]; then '
              'echo PREPARED_RUN_ID=00000000-0000-4000-8000-0000000000aa; fi')
    tree.stub("scripts/release/execution_gate_chain.py")
    return tree


def test_all_deploys_before_anything_else_and_never_captures(tmp_path):
    tree = _orchestrator(tmp_path)
    result = tree.run("production-activate.sh", "--all")
    assert result.returncode == 0, result.stdout + result.stderr
    calls = [c.split()[0] for c in tree.calls() if "DEPLOY_MODE=" not in c]
    assert calls == ["production-preflight.sh", "production-verify.sh",  # database gate
                     "production-verify.sh",                              # already deployed?
                     "cloud-run.sh",                                      # build + deploy
                     "production-verify.sh"]                              # gate deployed
    deploy = next(c for c in tree.calls() if c.startswith("cloud-run.sh DEPLOY_MODE"))
    assert "DEPLOY_MODE=apply PROJECT_ID=test-project JOB_LAUNCHER_MODE=disabled" in deploy
    assert "REF=abcdefghijklmnopqrst CORS=https://site.test" in deploy
    assert "--gate database" in tree.calls()[1]
    assert not any(c.startswith("government-production-capture.sh") for c in tree.calls())
    assert not any(c.startswith("website-execution-activate.sh") for c in tree.calls())


def test_all_stops_before_deploying_onto_a_database_missing_its_migrations(tmp_path):
    tree = _orchestrator(tmp_path, database_ok=False)
    result = tree.run("production-activate.sh", "--all")
    assert result.returncode == 1
    assert "Deploy Supabase Migrations workflow" in result.stderr
    assert not any(c.startswith("cloud-run.sh") for c in tree.calls())


def test_a_resumed_all_does_not_redeploy_a_deployed_release(tmp_path):
    """A redeploy would return a Stage P / Stage 2 surface to Stage A."""
    tree = _orchestrator(tmp_path, deployed=True)
    result = tree.run("production-activate.sh", "--all")
    assert result.returncode == 0, result.stderr
    assert "SKIPPED: API and worker already run" in result.stdout
    assert not any(c.startswith("cloud-run.sh") for c in tree.calls())
    forced = tree.run("production-activate.sh", "--deploy", "--force-redeploy")
    assert forced.returncode == 0
    assert any(c.startswith("cloud-run.sh DEPLOY_MODE=apply") for c in tree.calls())


def test_the_deploy_refuses_a_shell_aimed_at_another_project(tmp_path):
    tree = _orchestrator(tmp_path)
    result = tree.run("production-activate.sh", "--deploy", env={"PROJECT_ID": "someone-else"})
    assert result.returncode != 0 and "Unset it" in result.stderr
    assert not any(c.startswith("cloud-run.sh") for c in tree.calls())


def test_preparing_a_revision_runs_in_order_with_a_fresh_attempt_key(tmp_path):
    tree = _orchestrator(tmp_path)
    result = tree.run("production-activate.sh", "--prepare-work-scope", *WS_ARGS,
                      "--enable-catalog-execution", "--enable-work-scope-preparation")
    assert result.returncode == 0, result.stdout + result.stderr
    capture = [c for c in tree.calls() if c.startswith("government-production-capture.sh")]
    assert "--ensure-job" in capture[0]
    assert "--prepare " in capture[1] + " " and re.search(
        r"--idempotency-key work-scope-11111111-\d{8}T\d{6}Z", capture[1])
    assert "--prepare-work-scope" in capture[2]
    assert "--run-id 00000000-0000-4000-8000-0000000000aa" in capture[2]
    assert f"--work-scope-id {WS_ID}" in capture[2] and "--enable-work-scope-preparation" in capture[2]
    last = tree.calls()[-1]
    assert last.startswith("production-verify.sh") and "--gate prepared" in last


def test_a_prepared_revision_is_never_prepared_again(tmp_path):
    tree = _orchestrator(tmp_path, prepared=True)
    result = tree.run("production-activate.sh", "--prepare-work-scope", *WS_ARGS,
                      "--enable-catalog-execution", "--enable-work-scope-preparation")
    assert result.returncode == 0, result.stderr
    assert "SKIPPED: this revision is already prepared" in result.stdout
    assert not any(c.startswith("government-production-capture.sh") for c in tree.calls())


PREPARE = ("--prepare-work-scope", *WS_ARGS,
           "--enable-catalog-execution", "--enable-work-scope-preparation")


def _stopped_before_any_capture(tree: Tree, result: subprocess.CompletedProcess) -> None:
    assert result.returncode == 1, result.stdout + result.stderr
    assert not any(c.startswith("government-production-capture.sh") for c in tree.calls())
    # Nothing after the stop either: not even the prepared-gate verify.
    assert not any("--gate prepared" in c for c in tree.calls())
    assert "Nothing was executed" in result.stderr
    # The stop names the read-only check that resolves it.
    assert "read -rs MILO_TEST_RO_DB_URL && export MILO_TEST_RO_DB_URL" in result.stderr
    assert f"bash scripts/deploy/work-scope-readiness.sh --operator-config {tree.config} " \
           f"{' '.join(WS_ARGS)}" in result.stderr
    assert "PROCEEDING" not in result.stdout


@pytest.mark.parametrize("readiness", [
    # no read-only connection string
    "echo 'DATABASE_READ=UNVERIFIED (set $MILO_TEST_RO_DB_URL)'; "
    "echo 'WORK_SCOPE_READINESS=UNVERIFIED (1 fact(s) could not be verified)'; exit 3",
    # the database was read, the plan could not be
    "echo DATABASE_READ=VERIFIED; echo WORK_SCOPE_SCHEMA=VERIFIED; "
    "echo 'WORK_SCOPE_PLAN=UNVERIFIED (the plan query failed)'; exit 3",
    # the plan was read, its preparation could not be
    "echo DATABASE_READ=VERIFIED; echo WORK_SCOPE_SCHEMA=VERIFIED; "
    f"echo WORK_SCOPE_ID={WS_ID}; echo WORK_SCOPE_PLAN=VERIFIED; "
    "echo 'WORK_SCOPE_PREPARED=UNVERIFIED (the preparation query failed)'; exit 3",
], ids=["no-db-url", "plan-unread", "preparation-unread"])
def test_unverified_readiness_never_reaches_a_capture_command(tmp_path, readiness):
    tree = _orchestrator(tmp_path, readiness=readiness)
    result = tree.run("production-activate.sh", *PREPARE)
    _stopped_before_any_capture(tree, result)
    assert "STOP: readiness is UNVERIFIED" in result.stderr


FAKE_PSQL = r"""#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
values = {}
for i, a in enumerate(args):
    if a == "-v" and "=" in args[i + 1]:
        k, v = args[i + 1].split("=", 1); values[k] = v
sql = sys.stdin.read()
mode = os.environ.get("MILO_TEST_PSQL", "unprepared")
if mode == "refused":
    sys.exit(2)
if "rolbypassrls" in sql:
    print("false" if mode == "rls-bound" else "true")
elif ":'tables'" in sql:
    print("\n".join(f"{t}|true|true" for t in values["tables"].split(",")))
elif ":'rpcs'" in sql:
    print("\n".join(f"{r}|1|true|false" for r in values["rpcs"].split(",")))
elif "from public.catalog_work_scopes s" in sql and "workflow_key" in sql:
    d = values["ws_digest"]
    print(f"true|{values['ws_rev']}|{d}|swarm_v2|{d}|false")
elif "catalog_work_scope_preparations p" in sql and "public.runs" in sql:
    pass  # never prepared
else:
    sys.exit(3)
"""


@pytest.mark.parametrize("env,psql,detail", [
    ({}, None, "set $MILO_TEST_RO_DB_URL to a read-only connection string"),
    ({"MILO_TEST_RO_DB_URL": "postgresql://ro@db.test/postgres"}, None, "psql is not installed"),
    ({"MILO_TEST_RO_DB_URL": "postgresql://ro@db.test/postgres", "MILO_TEST_PSQL": "refused"},
     FAKE_PSQL, "the read-only connection failed or was refused"),
    ({"MILO_TEST_RO_DB_URL": "postgresql://ro@db.test/postgres", "MILO_TEST_PSQL": "rls-bound"},
     FAKE_PSQL, "subject to row-level security"),
], ids=["no-read-only-url", "no-psql", "connection-refused", "rls-bound-role"])
def test_the_real_readiness_check_unverified_stops_the_preparation(tmp_path, env, psql, detail):
    """The real work-scope-readiness.sh, not a stand-in: UNVERIFIED is exit 3."""
    tree = _orchestrator(tmp_path, real_readiness=True)
    if psql:
        tree.tool("psql", psql)
    result = tree.run("production-activate.sh", *PREPARE, env=env)
    assert "DATABASE_READ=UNVERIFIED" in result.stdout and detail in result.stdout
    _stopped_before_any_capture(tree, result)
    assert "STOP: readiness is UNVERIFIED" in result.stderr


def test_the_real_readiness_check_proving_an_unprepared_revision_lets_it_prepare(tmp_path):
    """The success path is matched against what the real check prints."""
    tree = _orchestrator(tmp_path, real_readiness=True)
    tree.tool("psql", FAKE_PSQL)
    result = tree.run("production-activate.sh", *PREPARE,
                      env={"MILO_TEST_RO_DB_URL": "postgresql://ro@db.test/postgres"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "WORK_SCOPE_PREPARED=NO (revision 2 has not been prepared." in result.stdout
    assert "PROCEEDING" in result.stdout
    capture = [c for c in tree.calls() if c.startswith("government-production-capture.sh")]
    assert len(capture) == 3
    assert "--ensure-job" in capture[0] and "--prepare " in capture[1] + " "
    assert "--prepare-work-scope" in capture[2]


BROKEN_PREPARATION = (
    "echo DATABASE_READ=VERIFIED; echo WORK_SCOPE_SCHEMA=VERIFIED; "
    f"echo WORK_SCOPE_ID={WS_ID}; echo WORK_SCOPE_PLAN=VERIFIED; "
    "echo WORK_SCOPE_PREPARATION_ID=99999999-0000-4000-8000-000000000000; "
    "echo 'WORK_SCOPE_PREPARED=NO (the preparation names another digest)'; exit 1")


@pytest.mark.parametrize("readiness,reason", [
    ("echo 'FAIL: unknown argument'; exit 2", "failed unexpectedly (exit 2"),
    ("exit 127", "failed unexpectedly (exit 127"),
    ("echo WORK_SCOPE_READINESS=VERIFIED; exit 0", "failed unexpectedly (exit 0"),
    ("exit 1", "without proving"),
    # a NO that is not "never prepared": preparing again would duplicate it
    (BROKEN_PREPARATION, "without proving"),
    (BROKEN_PREPARATION.replace("the preparation names another digest",
                                "more than one preparation answered for one revision")
     .replace("echo WORK_SCOPE_PREPARATION_ID=99999999-0000-4000-8000-000000000000; ", ""),
     "without proving"),
    # every fact present but the database read is not
    (UNPREPARED.replace("echo 'DATABASE_READ=VERIFIED (read-only role sees service-only rows)'\n", ""),
     "without proving"),
    # the answer is about another plan
    (UNPREPARED.replace(WS_ID, "99999999-2222-4333-8444-555555555555"), "without proving"),
    # the plan or schema is NO
    ("echo DATABASE_READ=VERIFIED; echo 'WORK_SCOPE_SCHEMA=NO (missing tables)'; exit 1",
     "cannot be prepared"),
    ("echo DATABASE_READ=VERIFIED; echo WORK_SCOPE_SCHEMA=VERIFIED; "
     "echo 'WORK_SCOPE_PLAN=NO (revision 2 is not the head)'; exit 1", "cannot be prepared"),
], ids=["usage-error", "crashed", "exit-0-without-facts", "exit-1-without-facts",
        "broken-preparation", "duplicate-preparation", "no-database-read", "another-plan",
        "schema-no", "stale-plan"])
def test_any_readiness_answer_short_of_proof_stops_the_preparation(tmp_path, readiness, reason):
    tree = _orchestrator(tmp_path, readiness=readiness)
    result = tree.run("production-activate.sh", *PREPARE)
    _stopped_before_any_capture(tree, result)
    assert reason in result.stderr


def test_a_prepared_revision_is_skipped_and_verified_even_if_later_facts_are_unverified(tmp_path):
    """Idempotent: an already-prepared revision creates no capture run; the
    read-only prepared gate (not this step) decides whether it is usable."""
    tree = _orchestrator(tmp_path, readiness=(
        "echo DATABASE_READ=VERIFIED; echo WORK_SCOPE_SCHEMA=VERIFIED; "
        f"echo WORK_SCOPE_ID={WS_ID}; echo WORK_SCOPE_PLAN=VERIFIED; "
        "echo WORK_SCOPE_PREPARED=VERIFIED; echo 'EVIDENCE_READY=UNVERIFIED (query failed)'; exit 3"))
    result = tree.run("production-activate.sh", *PREPARE)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SKIPPED: this revision is already prepared" in result.stdout
    assert not any(c.startswith("government-production-capture.sh") for c in tree.calls())
    last = tree.calls()[-1]
    assert last.startswith("production-verify.sh") and "--gate prepared" in last


@pytest.mark.parametrize("args,message", [
    (("--prepare-work-scope", "--enable-catalog-execution", "--enable-work-scope-preparation"),
     "requires --work-scope-id"),
    (("--prepare-work-scope", *WS_ARGS), "requires --enable-catalog-execution"),
    (("--website", "--deploy"), "may not be combined"),
])
def test_the_orchestrator_refuses_an_incomplete_or_mixed_request(tmp_path, args, message):
    tree = _orchestrator(tmp_path)
    result = tree.run("production-activate.sh", *args)
    assert result.returncode == 2 and message in result.stderr
    assert tree.calls() == []


# =============================================================================
# 4. website-execution-check.sh: three answers, never a false YES
# =============================================================================

FAKE_CURL = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
url = args[-1]
out = args[args.index("-o") + 1]
answers = json.loads(os.environ["MILO_TEST_HTTP"])
with open(os.environ["MILO_TEST_LOG"], "a") as log:
    log.write("curl " + " ".join(args) + "\n")
for suffix, (code, body) in answers.items():
    if url.endswith(suffix):
        open(out, "w").write(body if isinstance(body, str) else json.dumps(body))
        sys.stdout.write(str(code)); sys.exit(0)
sys.stdout.write("000"); sys.exit(7)
'''

DESCRIBE_GCLOUD = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["MILO_TEST_LOG"], "a") as log:
    log.write("gcloud " + " ".join(args) + "\n")
envs = json.loads(os.environ["MILO_TEST_ENVS"])
kind = "service" if args[:3] == ["run", "services", "describe"] else "job" if args[:3] == ["run", "jobs", "describe"] else None
if kind is None or kind not in envs:
    sys.exit(1)
env = [{"name": k, "valueFrom": {"secretKeyRef": {"name": v[7:], "key": "latest"}}}
       if isinstance(v, str) and v.startswith("secret:") else {"name": k, "value": v}
       for k, v in envs[kind].items()]
print(json.dumps({"spec": {"template": {"spec": {"containers": [{"env": env}]}}}}))
'''


def _armed_envs() -> dict[str, dict[str, str]]:
    service = {name: "true" for name in contract_array("MILO_STAGE2_API_ENABLE_FLAGS")}
    service.update({name: "false" for name in contract_array("MILO_STAGE2_API_PINNED_OFF_FLAGS")})
    service.update({"JOB_LAUNCHER": "cloud_run", "MILO_WORKER_AUDIENCE": "https://api.test",
                    "MILO_APPROVED_WORKER_IDENTITIES": "worker@test.iam.gserviceaccount.com"})
    job = {name: "true" for name in contract_array("MILO_STAGE2_WORKER_ENABLE_FLAGS")}
    job.update({name: "false" for name in contract_array("MILO_STAGE2_WORKER_PINNED_OFF_FLAGS")})
    job["KIMI_API_KEY"] = "secret:TEST_PROVIDER_KEY"
    return {"service": service, "job": job}


def _website(sha: str, *, ui=True, gateway=True, run_start=True,
             probe=(401, {"error": "Authentication required."}),
             health=(200, {"status": "ok"}), status_code=200) -> dict:
    return {
        "/api/deployment-status": (status_code, {"contract": "milo-website-deployment/1",
                                                 "execution_ui": ui,
                                                 "gateway_execution_routes": gateway,
                                                 "gateway_run_start_routes": run_start,
                                                 "commit_sha": sha}),
        "/api/gateway/workflow-proposals/00000000-0000-4000-8000-000000000000": probe,
        "/api/gateway/health": health,
    }


def _check(tmp_path, *, http=None, envs=None, readiness="exit 0", args=WS_ARGS):
    tree = Tree(tmp_path, ("website-execution-check.sh",))
    # FRONTEND_CODE_WIRED reads the checkout; give the copy the real frontend files.
    for relative in ("frontend/lib/api.ts", "frontend/components/conversation/TaskComposer.tsx",
                     "frontend/app/api/gateway/[...path]/route.ts",
                     "frontend/app/api/deployment-status/route.ts",
                     "frontend/lib/server/gatewayPolicy.ts"):
        target = tree.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / relative, target)
    tree.tool("curl", FAKE_CURL)
    tree.tool("gcloud", DESCRIBE_GCLOUD)
    tree.stub("scripts/deploy/work-scope-readiness.sh", readiness)
    answers = http(tree.sha) if callable(http) else http if http is not None else _website(tree.sha)
    env = {"MILO_TEST_HTTP": json.dumps(answers),
           "MILO_TEST_ENVS": json.dumps(envs if envs is not None else _armed_envs())}
    return tree, tree.run("website-execution-check.sh", *args, env=env)


def _facts(stdout: str) -> dict[str, str]:
    facts = {}
    for line in stdout.splitlines():
        match = re.match(r"^([A-Z_]+)=([A-Z_]+)", line)
        if match:
            facts[match.group(1)] = match.group(2)
    return facts


def test_every_fact_verified_is_the_only_active_stage(tmp_path):
    tree, result = _check(tmp_path)
    facts = _facts(result.stdout)
    assert result.returncode == 0, result.stdout + result.stderr
    for name in ("FRONTEND_CODE_WIRED", "FRONTEND_RELEASE", "TASK_COMPOSER_VISIBLE",
                 "GATEWAY_EXECUTION_ENABLED", "GATEWAY_RUN_START_ENABLED", "GATEWAY_BACKEND_BINDING",
                 "BACKEND_EXECUTION_ARMED", "MAPPING_PLAN_BATCH_PATH"):
        assert facts[name] == "VERIFIED", (name, result.stdout)
    assert facts["WEBSITE_EXECUTION_STAGE_ACTIVE"] == "VERIFIED"
    # Read-only: GET only, never a body, never a credential.
    for call in (c for c in tree.calls() if c.startswith("curl")):
        assert " -X " not in call and "--data" not in call and "authorization" not in call.lower()


@pytest.mark.parametrize("change,fact,value", [
    # The gateway variable is off and the gateway refuses by policy: DISABLED.
    (dict(gateway=False,
          probe=(403, {"error": "This API route is not allowed by the gateway policy."})),
     "GATEWAY_EXECUTION_ENABLED", "DISABLED"),
    # The status claims on, but the running gateway still refuses: never YES.
    (dict(probe=(403, {"error": "This API route is not allowed by the gateway policy."})),
     "GATEWAY_EXECUTION_ENABLED", "UNVERIFIED"),
    # The status says on but the behaviour could not be observed: UNVERIFIED.
    (dict(probe=(429, {"error": "Too many requests."})), "GATEWAY_EXECUTION_ENABLED", "UNVERIFIED"),
    # A Vercel SSO / protection page answering 401 is NOT the gateway's 401.
    (dict(probe=(401, "<html>Authentication Required</html>")), "GATEWAY_EXECUTION_ENABLED",
     "UNVERIFIED"),
    # The two sources disagree: UNVERIFIED, never YES.
    (dict(gateway=False), "GATEWAY_EXECUTION_ENABLED", "UNVERIFIED"),
    # Stage P / Stage 2 arming: plan writes open, run starts CLOSED -- the
    # pre-open posture, which is exactly not an active stage.
    (dict(run_start=False), "GATEWAY_RUN_START_ENABLED", "DISABLED"),
    # The status says run starts are open while the gateway refuses even the
    # execution routes: a contradiction, never a YES.
    (dict(gateway=False, probe=(403, {"error": "This API route is not allowed by the gateway policy."})),
     "GATEWAY_RUN_START_ENABLED", "UNVERIFIED"),
    # The execution UI was off at BUILD time.
    (dict(ui=False), "TASK_COMPOSER_VISIBLE", "DISABLED"),
    # The site predates the status contract, or is unreachable.
    (dict(status_code=404), "TASK_COMPOSER_VISIBLE", "UNVERIFIED"),
    (dict(health=(502, {"error": "Private API gateway request failed."})),
     "GATEWAY_BACKEND_BINDING", "NO"),
])
def test_no_inconclusive_or_disabled_signal_becomes_an_active_stage(tmp_path, change, fact, value):
    _, result = _check(tmp_path, http=lambda sha: _website(sha, **change))
    facts = _facts(result.stdout)
    assert facts[fact] == value, result.stdout
    assert result.returncode == 1
    assert facts["WEBSITE_EXECUTION_STAGE_ACTIVE"] in {"DISABLED", "UNVERIFIED"}


def test_the_website_built_from_another_commit_is_not_the_release(tmp_path):
    _, result = _check(tmp_path, http=_website("f" * 40))
    assert _facts(result.stdout)["FRONTEND_RELEASE"] == "NO"
    assert result.returncode == 1


def test_an_unreachable_site_and_cloud_is_unverified_everywhere(tmp_path):
    _, result = _check(tmp_path, http={}, envs={})
    facts = _facts(result.stdout)
    for name in ("FRONTEND_RELEASE", "TASK_COMPOSER_VISIBLE", "GATEWAY_EXECUTION_ENABLED",
                 "GATEWAY_BACKEND_BINDING", "BACKEND_EXECUTION_ARMED"):
        assert facts[name] == "UNVERIFIED", (name, result.stdout)
    assert facts["WEBSITE_EXECUTION_STAGE_ACTIVE"] == "UNVERIFIED"
    assert result.returncode == 1


@pytest.mark.parametrize("surface,name,bad", [
    ("service", "MILO_ENABLE_WORK_SCOPE_BATCHES", "false"),
    ("service", "MILO_ENABLE_WORK_SCOPE_MUTATIONS", None),
    ("service", "MILO_ENABLE_WORK_SCOPE_PREPARATION", "true"),
    ("service", "JOB_LAUNCHER", "disabled"),
    ("job", "MILO_ENABLE_GOVERNMENT_CATALOG_READ", "false"),
    ("job", "MILO_ENABLE_CATALOG_PROMOTION", "true"),
    ("job", "MILO_ENABLE_WORK_SCOPE_PREPARATION", "true"),
    ("job", "KIMI_API_KEY", None),
    ("service", "KIMI_API_KEY", "secret:TEST_PROVIDER_KEY"),
])
def test_every_backend_gate_is_read_by_value(tmp_path, surface, name, bad):
    envs = _armed_envs()
    if bad is None:
        envs[surface].pop(name)
    else:
        envs[surface][name] = bad
    _, result = _check(tmp_path, envs=envs)
    assert _facts(result.stdout)["BACKEND_EXECUTION_ARMED"] == "NO", result.stdout
    assert result.returncode == 1


def test_the_batch_path_must_be_named_and_ready(tmp_path):
    _, unnamed = _check(tmp_path / "a", args=())
    assert _facts(unnamed.stdout)["MAPPING_PLAN_BATCH_PATH"] == "UNVERIFIED"
    assert unnamed.returncode == 1
    _, not_ready = _check(tmp_path / "b", readiness="echo BATCH_READY=NO; exit 1")
    assert _facts(not_ready.stdout)["MAPPING_PLAN_BATCH_PATH"] == "NO"
    _, unknown = _check(tmp_path / "c", readiness="echo DATABASE_READ=UNVERIFIED; exit 3")
    assert _facts(unknown.stdout)["MAPPING_PLAN_BATCH_PATH"] == "UNVERIFIED"


# =============================================================================
# 5. production-verify.sh: separate facts, exact gates
# =============================================================================

def _verify_tree(tmp_path, *, migration_detail: str, migration_status: str = "PASS",
                 readiness: str, website: str) -> Tree:
    tree = Tree(tmp_path, ("production-verify.sh",))
    report = {"summary": {"blocked": 0 if migration_status == "PASS" else 1},
              "checks": [{"status": migration_status, "name": "remote:state",
                          "detail": migration_detail}]}
    tree.stub("scripts/release/check-migration-state.sh",
              'out=""; while [[ $# -gt 0 ]]; do [[ "$1" == --json-output ]] && out="$2"; shift; done; '
              f"printf '%s' '{json.dumps(report)}' > \"$out\"")
    tree.stub("scripts/deploy/work-scope-readiness.sh",
              'if [[ "$*" == *--schema-only* ]]; then echo WORK_SCOPE_SCHEMA=VERIFIED; exit 0; fi\n'
              + readiness)
    tree.stub("scripts/deploy/website-execution-check.sh", website)
    tree.stub("scripts/release/runtime_policy_manifest.py", "exit 0")
    image = f"test-region-docker.pkg.dev/test-project/test-repo/worker:{tree.sha}"
    api_image = f"test-region-docker.pkg.dev/test-project/test-repo/api:{tree.sha}"
    tree.tool("gcloud", "#!/usr/bin/env bash\n"
                        'case "$*" in\n'
                        f'  *"services describe"*"containers[0].image"*) echo {api_image} ;;\n'
                        f'  *"jobs describe"*"containers[0].image"*) echo {image} ;;\n'
                        f'  *MILO_RELEASE_SHA*) echo {tree.sha} ;;\n'
                        "esac\nexit 0\n")
    return tree


READY = ("echo EVIDENCE_READY=VERIFIED '(scoped)'; echo BATCH_READY=VERIFIED '(batch 1)'; "
         "echo WORK_SCOPE_READINESS=VERIFIED; exit 0")
SITE_ON = ("echo GATEWAY_EXECUTION_ENABLED=VERIFIED; echo GATEWAY_BACKEND_BINDING=VERIFIED; "
           "echo GATEWAY_RUN_START_ENABLED=VERIFIED; "
           "echo TASK_COMPOSER_VISIBLE=VERIFIED; echo FRONTEND_RELEASE=VERIFIED; "
           "echo BACKEND_EXECUTION_ARMED=VERIFIED; exit 0")
#: The pre-open posture: everything armed, run starts proved CLOSED.
SITE_ARMED = ("echo GATEWAY_EXECUTION_ENABLED=VERIFIED; echo GATEWAY_BACKEND_BINDING=VERIFIED; "
              "echo GATEWAY_RUN_START_ENABLED=DISABLED; "
              "echo TASK_COMPOSER_VISIBLE=VERIFIED; echo FRONTEND_RELEASE=VERIFIED; "
              "echo BACKEND_EXECUTION_ARMED=VERIFIED; exit 1")
SITE_OFF = ("echo GATEWAY_EXECUTION_ENABLED=DISABLED; echo GATEWAY_BACKEND_BINDING=VERIFIED; "
            "echo GATEWAY_RUN_START_ENABLED=DISABLED; "
            "echo TASK_COMPOSER_VISIBLE=DISABLED; echo FRONTEND_RELEASE=VERIFIED; "
            "echo BACKEND_EXECUTION_ARMED=NO; exit 1")


def _verdict(stdout: str) -> dict[str, str]:
    block = stdout.split("== VERDICT", 1)[1]
    return {m.group(1): m.group(2) for m in re.finditer(
        r"^ [* ] ([A-Z_]+)\s+([A-Z]+)(?:\s+\[needs [A-Z]+\])?$", block, re.M)}


def test_the_prepared_gate_needs_the_named_revision_not_a_snapshot(tmp_path):
    tree = _verify_tree(tmp_path, readiness=READY, website=SITE_OFF,
                        migration_detail="remote schema classified as fully-migrated (41/41)")
    unnamed = tree.run("production-verify.sh", "--gate", "prepared")
    verdict = _verdict(unnamed.stdout)
    assert unnamed.returncode == 1
    assert verdict["EVIDENCE_READY"] == "UNVERIFIED" and verdict["BATCH_READY"] == "UNVERIFIED"
    assert "a generic snapshot is never readiness" in unnamed.stdout

    named = tree.run("production-verify.sh", "--gate", "prepared", *WS_ARGS,
                     env={"MILO_TEST_RO_DB_URL": ""})
    verdict = _verdict(named.stdout)
    assert verdict["CODE_DEPLOYED"] == "VERIFIED" and verdict["DATABASE_READY"] == "VERIFIED"
    assert verdict["EVIDENCE_READY"] == "VERIFIED" and verdict["BATCH_READY"] == "VERIFIED"
    # No read-only DB: the runs table is unread, so the prepared gate still fails.
    assert verdict["RUNS_QUIESCENT"] == "UNVERIFIED" and named.returncode == 1
    # The website is reported separately and is not part of this gate.
    assert verdict["WEBSITE_ENABLED"] == "DISABLED" and verdict["GATEWAY_ENABLED"] == "DISABLED"
    assert verdict["PAID_EXECUTION_READY"] == "NO"


def test_a_partially_migrated_database_is_not_ready(tmp_path):
    tree = _verify_tree(tmp_path, readiness=READY, website=SITE_ON, migration_status="PASS",
                        migration_detail="remote schema classified as partially-migrated (38/41 local migrations applied)")
    result = tree.run("production-verify.sh", "--gate", "database")
    assert _verdict(result.stdout)["DATABASE_READY"] == "NO"
    assert result.returncode == 1


def test_the_deployed_gate_passes_on_code_and_the_exact_schema_only(tmp_path):
    tree = _verify_tree(tmp_path, readiness=READY, website=SITE_OFF,
                        migration_detail="remote schema classified as fully-migrated (41/41)")
    result = tree.run("production-verify.sh", "--gate", "deployed")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RESULT: OK" in result.stdout


def test_the_verifier_never_reports_a_generic_snapshot_as_readiness():
    text = VERIFY.read_text(encoding="utf-8")
    assert "USABLE_GOVERNMENT_SNAPSHOT=YES" not in text
    assert "DETERMINISTIC_QUEUE_READY=YES" not in text
    assert "informational only" in text


def _armed_tree(tmp_path, website: str) -> Tree:
    tree = _verify_tree(tmp_path, readiness=READY, website=website,
                        migration_detail="remote schema classified as fully-migrated (41/41)")
    # The runs table reads as quiescent, and the paid prerequisites resolve.
    tree.tool("psql", "#!/usr/bin/env bash\necho 0\n")
    _executable(tree.root / "scripts" / "release" / "runtime_policy_manifest.py",
                "import sys\nsys.exit(0)\n")
    tree.tool("gcloud", tree.bin.joinpath("gcloud").read_text().replace(
        "esac\nexit 0", '  *"secrets describe"*) exit 0 ;;\nesac\nexit 0'))
    return tree


def test_the_pre_open_gate_needs_run_starts_proved_closed(tmp_path):
    """`armed` is the gate BEFORE the last step: it passes only while every run
    start is still refused, and `active` is the check AFTER it."""
    env = {"MILO_TEST_RO_DB_URL": "postgresql://read-only@db.test/postgres"}
    armed = _armed_tree(tmp_path / "armed", SITE_ARMED)
    before = armed.run("production-verify.sh", "--gate", "armed", *WS_ARGS, env=env)
    verdict = _verdict(before.stdout)
    assert verdict["RUN_START_PATH"] == "DISABLED", before.stdout
    assert before.returncode == 0, before.stdout + before.stderr
    # The same posture is NOT active: the website cannot start anything yet.
    not_yet = armed.run("production-verify.sh", "--gate", "active", *WS_ARGS, env=env)
    assert not_yet.returncode == 1 and "RUN_START_PATH=DISABLED (needs VERIFIED)" in not_yet.stderr

    # Opened (or opened too early): the pre-open gate refuses, the check passes.
    opened = _armed_tree(tmp_path / "opened", SITE_ON)
    too_late = opened.run("production-verify.sh", "--gate", "armed", *WS_ARGS, env=env)
    assert too_late.returncode == 1 and "RUN_START_PATH=VERIFIED (needs DISABLED)" in too_late.stderr
    after = opened.run("production-verify.sh", "--gate", "active", *WS_ARGS, env=env)
    assert after.returncode == 0, after.stdout + after.stderr

    # Unprovable is neither.
    unknown = _armed_tree(tmp_path / "unknown", SITE_ARMED.replace(
        "GATEWAY_RUN_START_ENABLED=DISABLED", "GATEWAY_RUN_START_ENABLED=UNVERIFIED"))
    for gate in ("armed", "active"):
        result = unknown.run("production-verify.sh", "--gate", gate, *WS_ARGS, env=env)
        assert result.returncode == 1 and _verdict(result.stdout)["RUN_START_PATH"] == "UNVERIFIED"

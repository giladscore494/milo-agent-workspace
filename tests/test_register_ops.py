"""PR-D1 operations, offline against stand-ins: the register archive setup
(scripts/ops/setup-register-archive.sh), retention (scripts/ops/register-
retention.sh and its workflow), the register-capture website stage, and the
REGISTER_COVERAGE line of the gates.

Proves: the archive setup converges once and is idempotent, grants the
capture identity roles/storage.objectCreator on that bucket only, and its
check reports a public bucket or a delete-capable application role as FAIL;
retention lists read-only and prunes only with PRUNE and the exact digest,
through the capture job, never touching an archive object; the website stage
runs the canonical tools in order and needs the archive bucket; the gates
line is informational except above the threshold; nothing prints a secret.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

from tests.test_ops_workflows import (OPS, SENTINEL_FRAGMENTS, WORKFLOWS, OpsTree, steps, triggers,
                                      workflow)

BUCKET = "test-project-milo-register-archive"
CAPTURE = "serviceAccount:capture@test-project.iam.gserviceaccount.com"
DIGEST = "d" * 64

STORAGE_GCLOUD = r'''#!/usr/bin/env python3
"""gcloud with a tiny Cloud Storage world (buckets + bucket IAM)."""
import json, os, sys
args = sys.argv[1:]
path = os.environ["OPS_TEST_STORAGE_STATE"]
state = json.load(open(path)) if os.path.exists(path) else {"buckets": {}, "bindings": {}}
with open(os.environ["OPS_TEST_CALLS"], "a") as log:
    log.write("gcloud " + " ".join(args) + "\n")
def save():
    json.dump(state, open(path, "w"))
def flag(name):
    for i, a in enumerate(args):
        if a == name:
            return args[i + 1]
    return None
if args[:2] == ["auth", "list"]:
    print("owner@example.test"); sys.exit(0)
if args[:3] == ["config", "get-value", "project"]:
    print("test-project"); sys.exit(0)
if args[:3] == ["config", "get-value", "account"]:
    print("owner@example.test"); sys.exit(0)
name = args[3].replace("gs://", "") if len(args) > 3 else ""
if os.environ.get("OPS_TEST_IAM_DENIED") and "get-iam-policy" in args:
    # The deployer: it may describe the bucket but holds no IAM read on the
    # bucket or the project.
    sys.stderr.write("ERROR: PERMISSION_DENIED: storage.buckets.getIamPolicy / resourcemanager.projects.getIamPolicy\n")
    sys.exit(1)
if os.environ.get("OPS_TEST_IAM_ERROR") and "get-iam-policy" in args:
    sys.stderr.write("ERROR: (gcloud) 503 backend error\n"); sys.exit(1)
if args[:3] == ["run", "jobs", "describe"]:
    print(json.loads(os.environ.get("OPS_TEST_JOB_IMAGES", "{}")).get(args[3], "")); sys.exit(0)
if args[:3] == ["storage", "buckets", "describe"]:
    if name not in state["buckets"]:
        sys.exit(1)
    print(json.dumps(state["buckets"][name])); sys.exit(0)
if args[:3] == ["storage", "buckets", "create"]:
    state["buckets"][name] = {"location": flag("--location").upper(),
        "uniform_bucket_level_access": "--uniform-bucket-level-access" in args,
        "public_access_prevention": "enforced" if "--public-access-prevention" in args else "inherited"}
    save(); sys.exit(0)
if args[:3] == ["storage", "buckets", "update"]:
    b = state["buckets"][name]
    if "--uniform-bucket-level-access" in args: b["uniform_bucket_level_access"] = True
    if "--public-access-prevention" in args: b["public_access_prevention"] = "enforced"
    save(); sys.exit(0)
if args[:3] == ["storage", "buckets", "get-iam-policy"]:
    if name not in state["buckets"]:
        sys.exit(1)
    bindings = state["bindings"].get(name, [])
    print(json.dumps({"bindings": [{"role": r, "members": [m]} for r, m in bindings]})); sys.exit(0)
if args[:2] == ["projects", "get-iam-policy"]:
    print(json.dumps({"bindings": [{"role": r, "members": [m]} for r, m in state.get("project_bindings", [])]}))
    sys.exit(0)
if args[:3] == ["storage", "buckets", "add-iam-policy-binding"]:
    state["bindings"].setdefault(name, []).append([flag("--role"), flag("--member")]); save(); sys.exit(0)
sys.stderr.write("unmocked gcloud " + " ".join(args) + "\n"); sys.exit(2)
'''


def archive_tree(tmp_path: Path, *, bucket: str | None = BUCKET) -> tuple[OpsTree, dict[str, str]]:
    tree = OpsTree(tmp_path)
    tree.tool("gcloud", STORAGE_GCLOUD)
    if bucket is not None:
        tree.config.write_text(tree.config.read_text() + f"REGISTER_ARCHIVE_BUCKET={bucket}\n")
    return tree, {"OPS_TEST_STORAGE_STATE": str(tmp_path / "storage.json")}


def mutations(tree: OpsTree) -> list[str]:
    return [c for c in tree.tool_calls() if re.search(r"buckets (create|update|add-iam-policy-binding)", c)]


def no_secret(*texts: str) -> None:
    for fragment in SENTINEL_FRAGMENTS:
        assert fragment not in "".join(texts)


# =============================================================================
# 1. setup-register-archive.sh
# =============================================================================

def test_the_archive_setup_plans_applies_once_and_is_idempotent(tmp_path):
    tree, env = archive_tree(tmp_path)
    plan = tree.run("setup-register-archive.sh", extra_env=env)
    assert plan.returncode == 0, plan.stdout + plan.stderr
    assert mutations(tree) == [], "--plan changed something"
    assert re.findall(r"^(CREATE|UPDATE|BIND) ", plan.stdout, re.M) == ["CREATE", "BIND", "BIND"]
    assert tree.run("setup-register-archive.sh", "--check", extra_env=env).stdout.startswith("UNREADABLE ")

    applied = tree.run("setup-register-archive.sh", "--apply", extra_env=env)
    assert applied.returncode == 0, applied.stdout + applied.stderr
    state = json.loads((tmp_path / "storage.json").read_text())
    assert state["buckets"][BUCKET] == {"location": "US-CENTRAL1", "uniform_bucket_level_access": True,
                                        "public_access_prevention": "enforced"}
    # Exactly two grants, on this bucket only: create, and read back. No delete.
    assert state["bindings"] == {BUCKET: [["roles/storage.objectCreator", CAPTURE],
                                          ["roles/storage.objectViewer", CAPTURE]]}
    assert "PASS bucket test-project-milo-register-archive: us-central1" in applied.stdout
    # Identities are named by their configuration key, never by address.
    for out in (plan.stdout, plan.stderr, applied.stdout, applied.stderr):
        assert "@" not in out
    again = tree.run("setup-register-archive.sh", "--apply", extra_env=env)
    assert again.returncode == 0
    assert re.findall(r"^(CREATE|UPDATE|BIND) ", again.stdout, re.M) == []
    assert json.loads((tmp_path / "storage.json").read_text()) == state
    no_secret(plan.stdout, plan.stderr, applied.stdout, applied.stderr)


def test_without_a_capture_account_the_worker_identity_is_the_one_granted(tmp_path):
    tree, env = archive_tree(tmp_path)
    tree.config.write_text(tree.config.read_text().replace(
        "CAPTURE_SERVICE_ACCOUNT=capture@test-project.iam.gserviceaccount.com\n", ""))
    assert tree.run("setup-register-archive.sh", "--apply", extra_env=env).returncode == 0
    state = json.loads((tmp_path / "storage.json").read_text())
    worker = "serviceAccount:worker@test-project.iam.gserviceaccount.com"
    assert state["bindings"][BUCKET] == [["roles/storage.objectCreator", worker],
                                         ["roles/storage.objectViewer", worker]]


def test_the_check_reports_a_gap_without_a_bucket_name(tmp_path):
    tree, env = archive_tree(tmp_path, bucket=None)
    check = tree.run("setup-register-archive.sh", "--check", extra_env=env)
    assert check.returncode == 0 and check.stdout.startswith("GAP REGISTER_ARCHIVE_BUCKET is not configured")
    assert tree.run("setup-register-archive.sh", "--apply", extra_env=env).returncode == 2


def test_a_delete_capable_role_or_a_public_bucket_is_a_failure(tmp_path):
    tree, env = archive_tree(tmp_path)
    assert tree.run("setup-register-archive.sh", "--apply", extra_env=env).returncode == 0
    path = tmp_path / "storage.json"
    state = json.loads(path.read_text())
    state["bindings"][BUCKET].append(["roles/storage.objectAdmin",
                                      "serviceAccount:api@test-project.iam.gserviceaccount.com"])
    path.write_text(json.dumps(state))
    check = tree.run("setup-register-archive.sh", "--check", extra_env=env)
    assert check.stdout.startswith("FAIL an application identity holds a delete-capable role on")
    assert "roles/storage.objectAdmin" in check.stdout
    applied = tree.run("setup-register-archive.sh", "--apply", extra_env=env)
    assert applied.returncode == 1 and "delete-capable" in applied.stderr
    state["bindings"][BUCKET] = [["roles/storage.objectCreator", CAPTURE], ["roles/storage.objectViewer", CAPTURE],
                                 ["roles/storage.objectViewer", "allUsers"]]
    path.write_text(json.dumps(state))
    assert tree.run("setup-register-archive.sh", "--check", extra_env=env).stdout.startswith(
        "FAIL bucket test-project-milo-register-archive is not closed to the public")


def test_the_deployer_check_verifies_the_bucket_posture_and_leaves_iam_to_the_operator(tmp_path):
    tree, env = archive_tree(tmp_path)
    assert tree.run("setup-register-archive.sh", "--apply", extra_env=env).returncode == 0
    deployer = {**env, "OPS_TEST_IAM_DENIED": "1"}
    applied = len(mutations(tree))
    check = tree.run("setup-register-archive.sh", "--check", extra_env=deployer)
    assert check.returncode == 0
    assert check.stdout.startswith(f"PARTIAL bucket {BUCKET}: us-central1, uniform access, "
                                   "public access prevention enforced; its IAM")
    assert "Verify from Cloud Shell: bash scripts/ops/setup-register-archive.sh --check" in check.stdout
    # It asked, and was refused (the stub denies getIamPolicy); nothing changed.
    assert any("buckets get-iam-policy" in call for call in tree.tool_calls())
    assert len(mutations(tree)) == applied
    # The operator (IAM readable) still gets the whole check.
    assert tree.run("setup-register-archive.sh", "--check", extra_env=env).stdout.startswith("PASS ")
    # Only a DENIED IAM read is PARTIAL; any other failure stays UNREADABLE.
    assert tree.run("setup-register-archive.sh", "--check",
                    extra_env={**env, "OPS_TEST_IAM_ERROR": "1"}).stdout.startswith("UNREADABLE the IAM policy")
    path = tmp_path / "storage.json"
    state = json.loads(path.read_text())
    state["buckets"][BUCKET]["public_access_prevention"] = "inherited"
    path.write_text(json.dumps(state))
    assert tree.run("setup-register-archive.sh", "--check", extra_env=deployer).stdout.startswith(
        f"FAIL bucket {BUCKET} is not closed to the public")
    state["buckets"][BUCKET].update(public_access_prevention="enforced", location="EU")
    path.write_text(json.dumps(state))
    assert tree.run("setup-register-archive.sh", "--check", extra_env=deployer).stdout.startswith(
        f"GAP bucket {BUCKET} is not in us-central1")
    no_secret(check.stdout, check.stderr)
    assert "@" not in check.stdout + check.stderr


def activation_tree(tmp_path: Path) -> tuple[OpsTree, dict[str, str]]:
    tree, env = archive_tree(tmp_path)
    (tree.root / "scripts" / "deploy" / "production-verify.sh").write_text(VERIFY_STUB)
    # The capture job is not on the release image: the gate AFTER the archive
    # refuses, so reaching it proves the archive gate passed.
    env["OPS_TEST_JOB_IMAGES"] = json.dumps({"test-worker": "us-docker.pkg.dev/p/r/milo@sha256:" + "1" * 64})
    return tree, env


def activate(tree: OpsTree, env: dict[str, str]):
    return tree.run("../deploy/website-execution-activate.sh", "--apply-register-capture", extra_env=env)


def test_the_register_activation_passes_the_archive_gate_as_the_deployer_with_a_warning(tmp_path):
    tree, env = activation_tree(tmp_path)
    assert tree.run("setup-register-archive.sh", "--apply", extra_env=env).returncode == 0
    before, calls = len(mutations(tree)), len(tree.tool_calls())
    result = activate(tree, {**env, "OPS_TEST_IAM_DENIED": "1"})
    out = result.stdout + result.stderr
    assert f"PARTIAL bucket {BUCKET}" in result.stdout
    assert "WARN: the bucket posture is verified; its IAM is not readable by this identity." in result.stdout
    assert "Verify from Cloud Shell: bash scripts/ops/setup-register-archive.sh --check" in result.stdout
    assert "the register archive is not set up" not in out
    assert "the capture job does not run the release image" in out and result.returncode == 1
    assert len(mutations(tree)) == before
    assert not any("add-iam-policy-binding" in c or "services update" in c for c in tree.tool_calls()[calls:])
    no_secret(result.stdout, result.stderr)


def test_the_register_activation_still_refuses_a_missing_or_public_bucket_as_the_deployer(tmp_path):
    tree, env = activation_tree(tmp_path)
    missing = activate(tree, {**env, "OPS_TEST_IAM_DENIED": "1"})
    assert missing.returncode == 1 and "the register archive is not set up" in missing.stderr
    assert tree.run("setup-register-archive.sh", "--apply", extra_env=env).returncode == 0
    path = tmp_path / "storage.json"
    state = json.loads(path.read_text())
    state["buckets"][BUCKET]["public_access_prevention"] = "inherited"
    path.write_text(json.dumps(state))
    public = activate(tree, {**env, "OPS_TEST_IAM_DENIED": "1"})
    assert public.returncode == 1 and "the register archive is not set up" in public.stderr
    assert f"FAIL bucket {BUCKET} is not closed to the public" in public.stdout


def test_the_preflight_reports_the_archive_as_a_gap_and_a_violation_as_blocked():
    text = (Path(OPS).parents[1] / "scripts" / "deploy" / "production-preflight.sh").read_text()
    block = text[text.index('ARCHIVE_CHECK="$('):text.index("# Gateway / frontend binding.")]
    assert 'PASS\\ *) record_check PASS "storage:register-archive"' in block
    assert 'FAIL\\ *) record_check BLOCKED "storage:register-archive"' in block
    assert 'UNREADABLE\\ *) record_check WARN "storage:register-archive"' in block
    assert 'PARTIAL\\ *) record_check WARN "storage:register-archive"' in block
    assert '*) record_check WARN "storage:register-archive"' in block
    # Placed with the capture identity's checks, and read-only.
    assert text.index("iam:capture-cannot-read-provider-key") < text.index('ARCHIVE_CHECK="$(')
    assert "--check" in block and "--apply" not in block.split("Remediation")[0]


# =============================================================================
# 2. retention
# =============================================================================

RETENTION_PSQL = """#!/usr/bin/env bash
printf '%s\\n' "psql" >> "$OPS_TEST_CALLS"
printf 'PRUNABLE cs1.a rows=5 estimated_bytes=1000\\nTOTAL snapshots=%s rows=5 estimated_bytes=1000 variant_builds=%s\\nDIGEST %s\\n' \\
  "${OPS_TEST_PRUNABLE:-1}" "${OPS_TEST_VARIANT_BUILDS:-0}" "$OPS_TEST_DIGEST"
"""
RETENTION_GCLOUD = """#!/usr/bin/env bash
printf 'gcloud %s\\n' "$*" >> "$OPS_TEST_CALLS"
case "$*" in
  "auth list"*) echo owner@example.test ;;
  "config get-value project"*) echo test-project ;;
  "run jobs execute"*) echo milo-catalog-capture-abc12 ;;
  "run jobs executions describe"*) printf 'Completed\\tTrue\\n' ;;
  "logging read"*) echo "PRUNED snapshots=1 raw_records=5 candidates=3 (archive objects untouched)" ;;
esac
"""


def retention_tree(tmp_path: Path) -> OpsTree:
    tree = OpsTree(tmp_path)
    tree.tool("psql", RETENTION_PSQL)
    tree.tool("gcloud", RETENTION_GCLOUD)
    return tree


def test_the_dry_run_lists_read_only_and_names_the_digest(tmp_path):
    tree = retention_tree(tmp_path)
    result = tree.run("register-retention.sh", "--list", extra_env={"OPS_TEST_DIGEST": DIGEST})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PRUNABLE cs1.a rows=5 estimated_bytes=1000" in result.stdout
    assert f"digest={DIGEST}" in result.stdout
    assert [c for c in tree.tool_calls() if not c.startswith("psql")] == [], "a dry run ran something else"
    no_secret(result.stdout, result.stderr, tree.summary.read_text())


def test_apply_with_another_digest_is_refused_before_the_job(tmp_path):
    tree = retention_tree(tmp_path)
    result = tree.run("register-retention.sh", "--apply", "--confirm", "PRUNE", "--digest", "e" * 64,
                      extra_env={"OPS_TEST_DIGEST": DIGEST})
    assert result.returncode == 1 and "CATALOG_PRUNE_DIGEST_MISMATCH" in result.stdout + result.stderr
    assert not [c for c in tree.tool_calls() if "jobs execute" in c]


def test_apply_prunes_through_the_capture_job_with_the_exact_digest(tmp_path):
    tree = retention_tree(tmp_path)
    result = tree.run("register-retention.sh", "--apply", "--confirm", "PRUNE", "--digest", DIGEST,
                      extra_env={"OPS_TEST_DIGEST": DIGEST, "MILO_RETENTION_POLL_SECONDS": "0"})
    assert result.returncode == 0, result.stdout + result.stderr
    (execute,) = [c for c in tree.tool_calls() if "jobs execute" in c]
    assert "test-capture" in execute
    assert f"--args=-m,backend.catalog.register.prune,--apply,--confirm,PRUNE,--digest,{DIGEST}" in execute
    assert "SUMMARY|register-retention apply|PASS|snapshots=1 raw_records=5" in result.stdout
    # Database rows only: no storage command is ever issued.
    assert not [c for c in tree.tool_calls() if "storage" in c]
    no_secret(result.stdout, result.stderr, tree.summary.read_text())


def test_apply_needs_the_word_and_a_digest(tmp_path):
    tree = retention_tree(tmp_path)
    for args in (("--apply", "--confirm", "prune", "--digest", DIGEST), ("--apply", "--confirm", "PRUNE"),
                 ("--list", "--digest", DIGEST)):
        assert tree.run("register-retention.sh", *args, extra_env={"OPS_TEST_DIGEST": DIGEST}).returncode == 2
    assert tree.tool_calls() == []


def test_nothing_prunable_runs_no_job(tmp_path):
    tree = retention_tree(tmp_path)
    result = tree.run("register-retention.sh", "--apply", "--confirm", "PRUNE", "--digest", DIGEST,
                      extra_env={"OPS_TEST_DIGEST": DIGEST, "OPS_TEST_PRUNABLE": "0"})
    assert result.returncode == 0 and "nothing is prunable" in result.stdout
    assert not [c for c in tree.tool_calls() if "jobs execute" in c]


def test_old_mapper_variant_builds_alone_are_still_pruned(tmp_path):
    """PR-L1b: no snapshot, but an old mapper version's variant build: the job runs."""
    tree = retention_tree(tmp_path)
    result = tree.run("register-retention.sh", "--apply", "--confirm", "PRUNE", "--digest", DIGEST,
                      extra_env={"OPS_TEST_DIGEST": DIGEST, "OPS_TEST_PRUNABLE": "0", "OPS_TEST_VARIANT_BUILDS": "1",
                                 "MILO_RETENTION_POLL_SECONDS": "0"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert [c for c in tree.tool_calls() if "jobs execute" in c]


def test_the_retention_workflow_is_dry_run_first_and_confirmed_to_apply():
    doc = workflow("register-retention.yml")
    inputs = triggers(doc)["workflow_dispatch"]["inputs"]
    assert inputs["mode"]["default"] == "dry-run" and inputs["mode"]["options"] == ["dry-run", "apply"]
    (job,) = doc["jobs"].values()
    assert job["environment"] == "production"
    first = steps(doc)[0]["run"]
    assert '"${CONFIRM_INPUT}" != "PRUNE"' in first and "^[0-9a-f]{64}$" in first
    last = steps(doc)[-1]
    assert last["env"]["MILO_READONLY_DB_URL"] == "${{ secrets.MILO_READONLY_DB_URL }}"
    assert "register-retention.sh --list" in last["run"] and "--apply --confirm" in last["run"]


# =============================================================================
# 2b. the catalog variant backfill (PR-L1)
# =============================================================================

VARIANTS_GCLOUD = RETENTION_GCLOUD.replace(
    '"logging read"*) echo "PRUNED snapshots=1 raw_records=5 candidates=3 (archive objects untouched)" ;;',
    '"logging read"*) echo "${OPS_TEST_OUTCOME:-BUILT snapshot_key=cs1.a status=built rows=5/5}" ;;')
SNAPSHOT_KEY = "cs1." + "b" * 32


def variants_tree(tmp_path: Path) -> OpsTree:
    tree = OpsTree(tmp_path)
    tree.tool("gcloud", VARIANTS_GCLOUD)
    return tree


def test_the_variant_backfill_dry_run_calls_nothing(tmp_path):
    tree = variants_tree(tmp_path)
    result = tree.run("register-variants.sh", "--snapshot-key", SNAPSHOT_KEY, "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert tree.tool_calls() == []
    assert "backend.catalog.register.variants" in result.stdout and SNAPSHOT_KEY in result.stdout


def test_the_variant_backfill_runs_the_capture_job_for_one_snapshot(tmp_path):
    tree = variants_tree(tmp_path)
    result = tree.run("register-variants.sh", "--snapshot-key", SNAPSHOT_KEY,
                      extra_env={"MILO_VARIANTS_POLL_SECONDS": "0"})
    assert result.returncode == 0, result.stdout + result.stderr
    (execute,) = [c for c in tree.tool_calls() if "jobs execute" in c]
    assert "test-capture" in execute
    assert f"--args=-m,backend.catalog.register.variants,--snapshot-key,{SNAPSHOT_KEY}" in execute
    assert "SUMMARY|register-variants|PASS|snapshot_key=cs1.a status=built rows=5/5" in result.stdout
    no_secret(result.stdout, result.stderr, tree.summary.read_text())
    refused = tree.run("register-variants.sh", "--snapshot-key", SNAPSHOT_KEY,
                       extra_env={"MILO_VARIANTS_POLL_SECONDS": "0",
                                  "OPS_TEST_OUTCOME": "REFUSED CATALOG_VARIANT_SNAPSHOT_UNKNOWN: x"})
    assert refused.returncode == 1 and "SUMMARY|register-variants|FAIL|" in refused.stdout


def test_the_variant_backfill_refuses_a_malformed_key(tmp_path):
    tree = variants_tree(tmp_path)
    for key in ("", "bad key", "a,b", "-x"):
        assert tree.run("register-variants.sh", "--snapshot-key", key).returncode == 2
    assert tree.tool_calls() == []


def test_the_variant_workflow_runs_from_main_in_production():
    doc = workflow("register-variants.yml")
    inputs = triggers(doc)["workflow_dispatch"]["inputs"]
    assert inputs["snapshot_key"]["required"] is True
    (job,) = doc["jobs"].values()
    assert job["environment"] == "production"
    assert doc["concurrency"] == {"group": "milo-production-operations", "cancel-in-progress": False}
    first = steps(doc)[0]["run"]
    assert "refs/heads/main" in first and "^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$" in first
    assert 'register-variants.sh --snapshot-key "${SNAPSHOT_KEY_INPUT}"' in steps(doc)[-1]["run"]


# =============================================================================
# 3. the register-capture website stage
# =============================================================================

def test_the_register_stage_runs_the_canonical_tools_in_order(tmp_path):
    tree = OpsTree(tmp_path)
    tree.config.write_text(tree.config.read_text() + f"REGISTER_ARCHIVE_BUCKET={BUCKET}\n")
    result = tree.run("website-stage.sh", "--stage", "register-capture", "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    plan = [line for line in result.stdout.splitlines() if line.startswith("DRY-RUN:")]
    assert len(plan) == 2
    assert "government-production-capture.sh" in plan[0] and "--ensure-job --enable-catalog-execution" in plan[0]
    assert "website-execution-activate.sh" in plan[1] and "--apply-register-capture" in plan[1]
    for marker in ("--apply-backend", "--apply-web-preparation", "--apply-plan-authoring",
                   "MILO_ENABLE_RUN_CREATION=true", "GATEWAY_ALLOW_RUN_START_ROUTES"):
        assert marker not in result.stdout
    assert tree.tool_calls() == []
    no_secret(result.stdout, result.stderr)


def test_the_register_stage_needs_the_archive_bucket(tmp_path):
    tree = OpsTree(tmp_path)
    result = tree.run("website-stage.sh", "--stage", "register-capture", "--dry-run")
    assert result.returncode == 2 and "REGISTER_ARCHIVE_BUCKET" in result.stdout + result.stderr


def test_the_register_activation_gates_on_the_archive_and_reads_back():
    text = (Path(OPS).parents[1] / "scripts" / "deploy" / "website-execution-activate.sh").read_text()
    block = text[text.index('if [[ "$MODE" == "apply-register-capture" ]]'):text.index("# --apply-backend (Stage 2)")]
    assert block.index("production-verify.sh") < block.index("setup-register-archive.sh")
    assert block.index("setup-register-archive.sh") < block.index("ensure_job_binding")
    assert block.index("ensure_job_binding") < block.index("gcloud run services update")
    assert '"MILO_ENABLE_PAID_EXECUTION=${DISABLED}"' in block
    assert "MILO_ENABLE_RUN_CREATION" not in block and "GATEWAY_ALLOW_RUN_START_ROUTES" not in block


# =============================================================================
# 4. REGISTER_COVERAGE in the gates
# =============================================================================

VERIFY_STUB = """#!/usr/bin/env bash
echo "CODE_DEPLOYED=VERIFIED (stub)"
echo "DATABASE_READY=VERIFIED (stub)"
"""
COVERAGE_PSQL = """#!/usr/bin/env bash
printf '%s\\n' "psql $*" | sed 's/postgresql:[^ ]*/<url>/' >> "$OPS_TEST_CALLS"
printf '{"register_version": "%s", "units_total": 10, "units_captured": 3, "rows_total": 1000, "rows_captured": 300, "unverified_snapshots": 0, "database_bytes": %s}\\n' "$(printf 'a%.0s' $(seq 64))" "$OPS_TEST_DB_BYTES"
"""
SERVICE_GCLOUD = """#!/usr/bin/env bash
printf 'gcloud %s\\n' "$*" >> "$OPS_TEST_CALLS"
case "$*" in
  "run services describe"*) echo '{"spec":{"template":{"spec":{"containers":[{"env":[{"name":"MILO_DB_CAPACITY_BYTES","value":"500000000"}]}]}}}}' ;;
esac
"""


def gates_tree(tmp_path: Path) -> OpsTree:
    tree = OpsTree(tmp_path)
    verify = tree.root / "scripts" / "deploy" / "production-verify.sh"
    verify.write_text(VERIFY_STUB)
    tree.tool("psql", COVERAGE_PSQL)
    tree.tool("gcloud", SERVICE_GCLOUD)
    # The gates workflow runs setup-python and installs backend/requirements.txt
    # first; the tree's PATH (the stubs, then /usr/bin) would otherwise find a
    # bare system python3 without the backend's dependencies.
    tree.tool("python3", f'#!/usr/bin/env bash\nexec "{sys.executable}" "$@"\n')
    return tree


def test_register_coverage_is_informational_below_the_threshold(tmp_path):
    tree = gates_tree(tmp_path)
    result = tree.run("gates.sh", "--gate", "deployed", extra_env={"OPS_TEST_DB_BYTES": "120000000"})
    assert result.returncode == 0, result.stdout + result.stderr
    line = next(l for l in result.stdout.splitlines() if l.startswith("REGISTER_COVERAGE="))
    assert line.startswith("REGISTER_COVERAGE=INFO directory " + "a" * 64)
    assert "units 3/10; rows 300/1000; database 120000000/400000000 bytes" in line
    assert "unverified snapshots 0" in line
    assert "SUMMARY|REGISTER_COVERAGE|INFO|directory" in result.stdout
    no_secret(result.stdout, result.stderr, tree.summary.read_text())


def test_register_coverage_fails_the_gate_above_the_threshold(tmp_path):
    tree = gates_tree(tmp_path)
    result = tree.run("gates.sh", "--gate", "deployed", extra_env={"OPS_TEST_DB_BYTES": "400000001"})
    assert result.returncode == 1
    assert "SUMMARY|REGISTER_COVERAGE|FAIL|" in result.stdout
    assert "above the register capacity threshold" in result.stdout
    no_secret(result.stdout, result.stderr, tree.summary.read_text())


def test_a_coverage_report_that_fails_otherwise_is_not_available_never_a_gate_failure(tmp_path):
    tree = gates_tree(tmp_path)
    # The report itself cannot run: python3 exits 1 with a traceback and no
    # REGISTER_COVERAGE line (and 2 for an interpreter error).
    for code in ("1", "2"):
        tree.tool("python3", f"#!/usr/bin/env bash\necho 'Traceback (most recent call last):' >&2\nexit {code}\n")
        result = tree.run("gates.sh", "--gate", "deployed", extra_env={"OPS_TEST_DB_BYTES": "400000001"})
        assert result.returncode == 0, result.stdout + result.stderr
        assert f"REGISTER_COVERAGE=INFO not available (the coverage report did not run: exit {code})" \
            in result.stdout
        assert "SUMMARY|REGISTER_COVERAGE|INFO|not available" in result.stdout
        assert "above the register capacity threshold" not in result.stdout
        assert "Traceback" not in result.stdout + result.stderr
    # A non-FAIL line with a non-zero exit is not a FAIL either.
    tree.tool("python3", "#!/usr/bin/env bash\necho 'REGISTER_COVERAGE=INFO directory x'\nexit 1\n")
    result = tree.run("gates.sh", "--gate", "deployed", extra_env={"OPS_TEST_DB_BYTES": "1"})
    assert result.returncode == 0 and "REGISTER_COVERAGE=INFO not available" in result.stdout


def test_a_project_level_delete_capable_role_is_a_failure(tmp_path):
    tree, env = archive_tree(tmp_path)
    assert tree.run("setup-register-archive.sh", "--apply", extra_env=env).returncode == 0
    path = tmp_path / "storage.json"
    state = json.loads(path.read_text())
    state["project_bindings"] = [["roles/editor", "serviceAccount:worker@test-project.iam.gserviceaccount.com"]]
    path.write_text(json.dumps(state))
    check = tree.run("setup-register-archive.sh", "--check", extra_env=env)
    assert check.stdout.startswith("FAIL an application identity holds a delete-capable role on") \
        and "roles/editor" in check.stdout

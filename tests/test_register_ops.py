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
import subprocess
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
    assert inputs["operation"]["default"] == "prune" and inputs["operation"]["options"] == ["prune", "vacuum-full"]
    (job,) = doc["jobs"].values()
    assert job["environment"] == "production"
    first = steps(doc)[0]["run"]
    assert '"${CONFIRM_INPUT}" != "PRUNE"' in first and "^[0-9a-f]{64}$" in first
    prune, vacuum_step = steps(doc)[-2:]
    assert prune["if"] == "${{ inputs.operation == 'prune' }}"
    assert prune["env"]["MILO_READONLY_DB_URL"] == "${{ secrets.MILO_READONLY_DB_URL }}"
    assert "register-retention.sh --list" in prune["run"] and "--apply --confirm" in prune["run"]
    # The owner's credential reaches the space reclamation step only.
    assert vacuum_step["if"] == "${{ inputs.operation == 'vacuum-full' }}"
    assert vacuum_step["env"]["SUPABASE_DB_PASSWORD"] == "${{ secrets.SUPABASE_DB_PASSWORD }}"
    assert not [step for step in steps(doc) if step is not vacuum_step
                and "SUPABASE_DB_PASSWORD" in json.dumps(step.get("env") or {})]
    assert "register-vacuum.sh --sizes" in vacuum_step["run"]
    assert 'register-vacuum.sh --apply --confirm "${CONFIRM_INPUT}"' in vacuum_step["run"]
    assert '"${CONFIRM_INPUT}" != "VACUUM"' in first


# =============================================================================
# 2a. space reclamation (PR-L2): VACUUM (FULL, ANALYZE) of the compacted tables
# =============================================================================

VACUUM_PSQL = """#!/usr/bin/env bash
# Both connections arrive through libpq's variables; no URL is ever on argv.
owner_user=0; [[ "${PGUSER:-}" == postgres || "${PGUSER:-}" == postgres.* ]] && owner_user=1
if [[ -n "${PGUSER:-}" && "$owner_user" -eq 0 ]]; then
  who=" [readonly PGUSER=$PGUSER PGHOST=$PGHOST PGPORT=$PGPORT PGDATABASE=$PGDATABASE]"
else
  who="${PGUSER:+ [owner PGUSER=$PGUSER PGHOST=$PGHOST PGPORT=$PGPORT PGSSLMODE=$PGSSLMODE]}"
fi
printf 'psql%s%s %s\\n' "$who" "${PGOPTIONS:+ [PGOPTIONS=$PGOPTIONS]}" "$*" | sed 's/postgresql:[^ ]*/<url>/' >> "$OPS_TEST_CALLS"
[[ "$*" != *postgresql:* && "$*" != *postgres:* ]] || exit 8
# Each password arrives through PGPASSWORD only, and only its own.
if [[ -n "${PGUSER:-}" && "$owner_user" -eq 0 ]]; then [[ "${PGPASSWORD:-}" == "$OPS_TEST_RO_PASSWORD" ]] || exit 7
elif [[ -n "${PGUSER:-}" ]]; then [[ "${PGPASSWORD:-}" == "$OPS_TEST_OWNER_PASSWORD" ]] || exit 7
else exit 7; fi
case "$*" in
  *"vacuum (full"*) exit "${OPS_TEST_VACUUM_STATUS:-0}" ;;
  *"'OWNER '"*)
    [[ -z "${OPS_TEST_OWNER_UNREACHABLE:-}" ]] || exit 2
    printf 'OWNER catalog_raw_records=%s\\nOWNER catalog_candidate_variants=PASS\\n' "${OPS_TEST_OWNER:-PASS}"
    exit 0 ;;
  *"'GATE runs="*)
    live=0; [[ -n "${OPS_TEST_LIVE_AT:-}" && "$*" == *"public.${OPS_TEST_LIVE_AT}'"* ]] && live=1
    printf 'GATE runs=%s register_captures=0 database=%s table=%s\\n' "$live" \\
      "${OPS_TEST_DB_BYTES:-144542867}" "${OPS_TEST_TABLE_BYTES:-62169088}"
    exit 0 ;;
esac
# The candidates' table is the larger here: the rewrite order (smaller first) stays raw_records first.
printf 'SIZE catalog_raw_records total=%s heap=1 toast=8192\\nSIZE catalog_candidate_variants total=70000000 heap=1 toast=8192\\n' \\
  "${OPS_TEST_RAW_BYTES:-62169088}"
printf 'DATABASE bytes=%s\\nLIVE runs=%s register_captures=%s\\n' "${OPS_TEST_DB_BYTES:-144542867}" \\
  "${OPS_TEST_LIVE_RUNS:-0}" "${OPS_TEST_LIVE_CAPTURES:-0}"
"""

OWNER_PASSWORD = "S3NTINEL-OWNER-PASSWORD"
#: The read-only URL of a Supabase pooler (transaction mode): the owner connects to the same host in
#: session mode (5432) as postgres.<project ref>.
POOLER_URL = "postgresql://ro.abcdefghijklmnopqrst:S3NTINEL-DB-PASSWORD@aws-0-us-east-1.pooler.supabase.com:6543/postgres"
OWNER_ENV = {"MILO_READONLY_DB_URL": POOLER_URL, "SUPABASE_DB_PASSWORD": OWNER_PASSWORD,
             "SUPABASE_PROJECT_ID": "abcdefghijklmnopqrst", "OPS_TEST_OWNER_PASSWORD": OWNER_PASSWORD,
             "OPS_TEST_RO_PASSWORD": "S3NTINEL-DB-PASSWORD"}


def vacuum_tree(tmp_path: Path) -> OpsTree:
    tree = OpsTree(tmp_path)
    tree.tool("psql", VACUUM_PSQL)
    return tree


def vacuum(tree: OpsTree, *args: str, **env: str):
    return tree.run("register-vacuum.sh", *args, extra_env={**OWNER_ENV, **env})


def test_the_vacuum_dry_run_reads_sizes_and_the_owner_only(tmp_path):
    tree = vacuum_tree(tmp_path)
    result = vacuum(tree, "--sizes")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SIZE catalog_raw_records total=62169088" in result.stdout
    assert "SUMMARY|register-vacuum sizes|PASS|pg_database_size 144542867 bytes; live runs 0" in result.stdout
    for table in ("catalog_raw_records", "catalog_candidate_variants"):
        assert f"SUMMARY|register-vacuum owner {table}|PASS|" in result.stdout
    assert not [c for c in tree.tool_calls() if "vacuum (full" in c]
    # The owner connects to the pooler's host in SESSION mode, as postgres.<ref>, over TLS.
    (owner,) = [c for c in tree.tool_calls() if "[owner" in c]
    assert "PGUSER=postgres.abcdefghijklmnopqrst PGHOST=aws-0-us-east-1.pooler.supabase.com PGPORT=5432" in owner
    assert "PGSSLMODE=require" in owner
    denied = vacuum(tree, "--sizes", OPS_TEST_OWNER="FAIL")
    assert denied.returncode == 0 and "SUMMARY|register-vacuum owner catalog_raw_records|FAIL|" in denied.stdout
    no_secret(result.stdout, result.stderr, tree.summary.read_text(), "\n".join(tree.tool_calls()))
    assert OWNER_PASSWORD not in result.stdout + result.stderr + "\n".join(tree.tool_calls())
    # The read-only URL never reaches psql's argv: its parts arrive as libpq's
    # variables, its password as PGPASSWORD (the stub refuses anything else).
    reads = [c for c in tree.tool_calls() if "[readonly" in c]
    assert reads and all("PGUSER=ro.abcdefghijklmnopqrst PGHOST=aws-0-us-east-1.pooler.supabase.com "
                         "PGPORT=6543 PGDATABASE=postgres" in c for c in reads)
    assert not [c for c in tree.tool_calls() if "<url>" in c or "S3NTINEL-DB-PASSWORD" in c]


def test_the_read_only_role_holds_no_maintain_and_the_owner_rewrites():
    """PR-L2 should-fix 3: MAINTAIN would also let the read-only credential LOCK,
    CLUSTER and REINDEX; the rewrite is the owner's."""
    text = (OPS / "register-vacuum.sh").read_text()
    assert "MAINTAIN" not in re.sub(r"#[^\n]*", "", text).replace("holds no MAINTAIN", "")
    assert 'PGPASSWORD="$SUPABASE_DB_PASSWORD"' in text and "owner_psql -q -c" in text


def test_the_vacuum_needs_its_word_the_owner_and_quiet(tmp_path):
    tree = vacuum_tree(tmp_path)
    for args in (("--apply",), ("--apply", "--confirm", "vacuum"), ("--sizes", "--confirm", "VACUUM")):
        assert vacuum(tree, *args).returncode == 2
    assert tree.tool_calls() == []
    for refused in ({"OPS_TEST_OWNER": "FAIL"}, {"OPS_TEST_OWNER_UNREACHABLE": "1"}, {"SUPABASE_DB_PASSWORD": ""},
                    {"MILO_READONLY_DB_URL": "postgresql://ro:S3NTINEL-DB-PASSWORD@db.sentinel.example.com/postgres"}):
        result = vacuum(tree, "--apply", "--confirm", "VACUUM", **refused)
        assert result.returncode == 1 and "CATALOG_VACUUM_NOT_PERMITTED" in result.stderr, refused
    assert not [c for c in tree.tool_calls() if "vacuum (full" in c]


def test_liveness_and_headroom_are_checked_before_each_table(tmp_path):
    tree = vacuum_tree(tmp_path)
    # Live before the SECOND table: the first was rewritten, the second is not.
    result = vacuum(tree, "--apply", "--confirm", "VACUUM", OPS_TEST_LIVE_AT="catalog_candidate_variants")
    assert result.returncode == 1 and "CATALOG_VACUUM_BLOCKED before public.catalog_candidate_variants" in result.stdout
    assert [c.split("public.")[-1] for c in tree.tool_calls() if "vacuum (full" in c] == ["catalog_raw_records"]
    # No room for the copy: 400,000,000 + 50,000,000 x 1.1 = 455,000,000 > 450,000,000.
    rewritten = len([c for c in tree.tool_calls() if "vacuum (full" in c])
    result = vacuum(tree, "--apply", "--confirm", "VACUUM", OPS_TEST_DB_BYTES="400000000",
                    OPS_TEST_TABLE_BYTES="50000000")
    assert result.returncode == 1
    assert ("CATALOG_VACUUM_NO_HEADROOM before public.catalog_raw_records: pg_database_size 400000000 + 50000000"
            " x 1.1 = 455000000 > 450000000 bytes") in result.stdout
    assert len([c for c in tree.tool_calls() if "vacuum (full" in c]) == rewritten  # nothing was rewritten


def test_the_vacuum_rewrites_each_table_as_the_owner_with_a_lock_timeout(tmp_path):
    tree = vacuum_tree(tmp_path)
    result = vacuum(tree, "--apply", "--confirm", "VACUUM")
    assert result.returncode == 0, result.stdout + result.stderr
    rewrites = [c for c in tree.tool_calls() if "vacuum (full" in c]
    assert [c.split("public.")[-1] for c in rewrites] == ["catalog_raw_records", "catalog_candidate_variants"]
    assert all("[owner PGUSER=postgres.abcdefghijklmnopqrst" in c and "PGOPTIONS" not in c
               and "set lock_timeout = '5s'" in c and "<url>" not in c for c in rewrites)
    gates = [c for c in tree.tool_calls() if "'GATE runs=" in c]
    assert len(gates) == 2 and all("[owner" not in c for c in gates)
    assert "SUMMARY|register-vacuum headroom catalog_raw_records|PASS|" in result.stdout
    assert "SUMMARY|register-vacuum apply|PASS|pg_database_size 144542867 -> 144542867 bytes" in result.stdout
    failed = vacuum(tree, "--apply", "--confirm", "VACUUM", OPS_TEST_VACUUM_STATUS="1")
    assert failed.returncode == 1 and "did not complete" in failed.stdout
    no_secret(result.stdout, result.stderr, tree.summary.read_text(), "\n".join(tree.tool_calls()))
    assert OWNER_PASSWORD not in result.stdout + result.stderr + "\n".join(tree.tool_calls())


#: Production, 2.10 (Register retention #12): the sizes the fixed order refused with.
PROD_DATABASE, PROD_RAW, PROD_CANDIDATES = 364_915_859, 85_417_984, 59_473_920
#: The candidates' table after its rewrite (heap 17.6 MB of 59.5 MB total).
PROD_CANDIDATES_AFTER, PROD_RAW_AFTER = 40_000_000, 60_000_000

#: A stateful stub: a rewrite shrinks its table and pg_database_size by what it frees.
SIZED_PSQL = """#!/usr/bin/env bash
printf 'psql%s %s\\n' "${PGUSER:+ [$PGUSER]}" "$*" >> "$OPS_TEST_CALLS"
# shellcheck disable=SC1090
source "$OPS_TEST_STATE"
case "$*" in
  *"vacuum (full"*)
    args="$*" && table="${args##*public.}" && after="${table}_after"
    database=$(( database - ${!table} + ${!after} ))
    printf -v "$table" '%s' "${!after}"
    declare -p database catalog_raw_records catalog_candidate_variants \\
      catalog_raw_records_after catalog_candidate_variants_after > "$OPS_TEST_STATE"
    exit 0 ;;
  *"'OWNER '"*)
    printf 'OWNER catalog_raw_records=PASS\\nOWNER catalog_candidate_variants=PASS\\n'
    exit 0 ;;
  *"'GATE runs="*)
    [[ "$*" =~ public\\.([a-z_]+)\\'::regclass ]] && table="${BASH_REMATCH[1]}"
    runs=0 captures=0
    if [[ "${OPS_TEST_LIVE_AT:-}" == "$table" ]]; then printf -v "${OPS_TEST_LIVE_KIND:-runs}" 1; fi
    printf 'GATE runs=%s register_captures=%s database=%s table=%s\\n' "$runs" "$captures" "$database" "${!table}"
    exit 0 ;;
esac
raw="$catalog_raw_records" candidates="$catalog_candidate_variants"
# The fixed order of before (raw_records first): the size read ranks raw_records the smaller.
[[ -z "${OPS_TEST_FIXED_ORDER:-}" ]] || candidates=$(( raw + 1 ))
printf 'SIZE catalog_raw_records total=%s heap=1 toast=8192\\nSIZE catalog_candidate_variants total=%s heap=1 toast=0\\n' \\
  "$raw" "$candidates"
printf 'DATABASE bytes=%s\\nLIVE runs=0 register_captures=0\\n' "$database"
"""


def sized_vacuum(tmp_path: Path, *, database: int = PROD_DATABASE, **env: str):
    tmp_path.mkdir()
    tree = OpsTree(tmp_path)
    tree.tool("psql", SIZED_PSQL)
    state = tmp_path / "db-state.sh"
    state.write_text(f"database={database}\ncatalog_raw_records={PROD_RAW}\n"
                     f"catalog_candidate_variants={PROD_CANDIDATES}\ncatalog_raw_records_after={PROD_RAW_AFTER}\n"
                     f"catalog_candidate_variants_after={PROD_CANDIDATES_AFTER}\n")
    result = vacuum(tree, "--apply", "--confirm", "VACUUM", OPS_TEST_STATE=str(state), **env)
    rewritten = [c.split("public.")[-1] for c in tree.tool_calls() if "vacuum (full" in c]
    return result, rewritten, tree.tool_calls()


def test_the_smaller_table_is_rewritten_first_and_frees_the_larger_ones_headroom(tmp_path):
    """PR-VAC: production refused the fixed order before its first table; smaller first, both fit."""
    old, rewritten, _ = sized_vacuum(tmp_path / "fixed", OPS_TEST_FIXED_ORDER="1")
    assert old.returncode == 1 and rewritten == []
    assert ("CATALOG_VACUUM_NO_HEADROOM before public.catalog_raw_records: pg_database_size 364915859 + 85417984"
            " x 1.1 = 458875641 > 450000000 bytes") in old.stdout
    result, rewritten, calls = sized_vacuum(tmp_path / "sized")
    assert result.returncode == 0, result.stdout + result.stderr
    assert rewritten == ["catalog_candidate_variants", "catalog_raw_records"]
    assert ("SUMMARY|register-vacuum order|PASS|smaller first: catalog_candidate_variants 59473920 bytes,"
            " then catalog_raw_records 85417984 bytes") in result.stdout
    assert ("SUMMARY|register-vacuum headroom catalog_candidate_variants|PASS|pg_database_size 364915859"
            " + 59473920 x 1.1 = 430337171 <= 450000000 bytes") in result.stdout
    # 364,915,859 - 59,473,920 + 40,000,000 = 345,441,939 before raw_records.
    assert ("SUMMARY|register-vacuum headroom catalog_raw_records|PASS|pg_database_size 345441939"
            " + 85417984 x 1.1 = 439401721 <= 450000000 bytes") in result.stdout
    assert "SUMMARY|register-vacuum apply|PASS|pg_database_size 364915859 -> 320023955 bytes" in result.stdout
    # The order is read with the read-only role (the sizes' own SQL, once more right before the loop),
    # and the gate still runs before EACH table, read-only.
    sizes = [c for c in calls if "'SIZE '" in c]
    gates = [c for c in calls if "'GATE runs=" in c]
    assert len(sizes) == 3 and len(gates) == 2
    assert all("[postgres." not in c for c in sizes + gates)
    assert calls.index(sizes[1]) < calls.index(gates[0])
    no_secret(result.stdout, result.stderr, "\n".join(calls))
    assert OWNER_PASSWORD not in result.stdout + result.stderr + "\n".join(calls)


def test_the_smaller_first_order_keeps_every_refusal(tmp_path):
    # Even the smaller table does not fit: 400,000,000 + 59,473,920 x 1.1 > 450,000,000; nothing is rewritten.
    result, rewritten, _ = sized_vacuum(tmp_path / "full", database=400_000_000)
    assert result.returncode == 1 and rewritten == []
    assert ("CATALOG_VACUUM_NO_HEADROOM before public.catalog_candidate_variants: pg_database_size 400000000"
            " + 59473920 x 1.1 = 465421312 > 450000000 bytes") in result.stdout
    assert "REFUSED CATALOG_VACUUM_NO_HEADROOM" in result.stderr
    # Live before the SECOND (larger) table: a run, or a register capture; only the smaller was rewritten.
    for kind, shown in (("runs", "1 live run(s), 0 live register capture(s)"),
                        ("captures", "0 live run(s), 1 live register capture(s)")):
        result, rewritten, _ = sized_vacuum(tmp_path / kind, OPS_TEST_LIVE_AT="catalog_raw_records",
                                         OPS_TEST_LIVE_KIND=kind)
        assert result.returncode == 1 and rewritten == ["catalog_candidate_variants"], kind
        assert f"CATALOG_VACUUM_BLOCKED before public.catalog_raw_records: {shown}" in result.stdout
        assert "REFUSED CATALOG_VACUUM_BLOCKED" in result.stderr


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


def test_the_compaction_runs_the_capture_job_dry_run_first(tmp_path):
    """PR-L2: `--compact dry-run|apply` runs the compaction module instead."""
    tree = variants_tree(tmp_path)
    for mode, line in (("dry-run", "READY snapshot_key=cs1.a raw_rows=5"),
                       ("apply", "COMPACTED snapshot_key=cs1.a raw_rows=5 payloads_removed=5")):
        result = tree.run("register-variants.sh", "--snapshot-key", SNAPSHOT_KEY, "--compact", mode,
                          extra_env={"MILO_VARIANTS_POLL_SECONDS": "0", "OPS_TEST_OUTCOME": line})
        assert result.returncode == 0, result.stdout + result.stderr
        execute = [c for c in tree.tool_calls() if "jobs execute" in c][-1]
        assert f"--args=-m,backend.catalog.register.compaction,--snapshot-key,{SNAPSHOT_KEY},--{mode}" in execute
        assert f"SUMMARY|register-variants|PASS|{line}" in result.stdout
    refused = tree.run("register-variants.sh", "--snapshot-key", SNAPSHOT_KEY, "--compact", "apply",
                       extra_env={"MILO_VARIANTS_POLL_SECONDS": "0",
                                  "OPS_TEST_OUTCOME": "REFUSED CATALOG_COMPACTION_BUILD_INCOMPLETE: x"})
    assert refused.returncode == 1 and "SUMMARY|register-variants|FAIL|" in refused.stdout
    calls = len(tree.tool_calls())
    assert tree.run("register-variants.sh", "--snapshot-key", SNAPSHOT_KEY, "--compact", "now").returncode == 2
    assert len(tree.tool_calls()) == calls


# P49: Cloud Logging makes the execution's outcome line readable some time
# after the execution completes. The read-back repeats, bounded; no line is a FAIL.
LAGGING_GCLOUD = RETENTION_GCLOUD.replace(
    '"logging read"*) echo "PRUNED snapshots=1 raw_records=5 candidates=3 (archive objects untouched)" ;;',
    '"logging read"*)\n'
    '    reads=$(( $(cat "$OPS_TEST_LOG_READS" 2> /dev/null || echo 0) + 1 )); echo "$reads" > "$OPS_TEST_LOG_READS"\n'
    '    [ -n "${OPS_TEST_LOG_FAIL:-}" ] && { echo "ERROR: (gcloud.logging.read) PERMISSION_DENIED" >&2; exit 1; }\n'
    '    echo "Starting capture job"\n'
    '    if [ "$reads" -ge "${OPS_TEST_LOG_READY_AT:-1}" ]; then echo "$OPS_TEST_OUTCOME"; fi\n'
    '    ;;')


def lagging(tmp_path: Path, script: str, *args: str, **env: str) -> tuple[subprocess.CompletedProcess, int, OpsTree]:
    tree = OpsTree(tmp_path)
    tree.tool("gcloud", LAGGING_GCLOUD)
    tree.tool("psql", RETENTION_PSQL)
    reads = tmp_path / "log-reads"
    result = tree.run(script, *args, extra_env={"OPS_TEST_LOG_READS": str(reads), **env})
    return result, int(reads.read_text()) if reads.exists() else 0, tree


def test_the_outcome_line_is_read_again_until_cloud_logging_has_it(tmp_path):
    line = "READY snapshot_key=cs1.a mode=active raw_rows=5279 kept_rows=5279"
    result, reads, tree = lagging(tmp_path, "register-variants.sh", "--snapshot-key", SNAPSHOT_KEY,
                                  "--compact", "dry-run", MILO_VARIANTS_POLL_SECONDS="0",
                                  MILO_VARIANTS_LOG_POLL_SECONDS="0", MILO_VARIANTS_LOG_WAIT_SECONDS="10",
                                  OPS_TEST_LOG_READY_AT="4", OPS_TEST_OUTCOME=line)
    assert result.returncode == 0, result.stdout + result.stderr
    assert reads == 4, "read again until the line was there, then stopped"
    assert line in result.stdout.splitlines()
    assert f"SUMMARY|register-variants|PASS|{line}" in result.stdout
    no_secret(result.stdout, result.stderr, tree.summary.read_text())


def test_no_outcome_line_within_the_bounded_wait_fails_the_step(tmp_path):
    result, reads, _tree = lagging(tmp_path, "register-variants.sh", "--snapshot-key", SNAPSHOT_KEY,
                                   "--compact", "dry-run", MILO_VARIANTS_POLL_SECONDS="0",
                                   MILO_VARIANTS_LOG_POLL_SECONDS="0", MILO_VARIANTS_LOG_WAIT_SECONDS="3",
                                   OPS_TEST_LOG_READY_AT="999", OPS_TEST_OUTCOME="READY snapshot_key=cs1.a")
    assert result.returncode == 1, "the execution succeeded, but without its line the step is not a PASS"
    assert reads == 4, "bounded: the first read, then one per second of the 3 s wait"
    assert "<no outcome line in the execution log after 3s>" in result.stdout
    assert "gcloud logging read exited" not in result.stderr, "each read succeeded; the line was never there"
    assert re.search(r"^SUMMARY\|register-variants\|FAIL\|execution milo-catalog-capture-abc12 succeeded: "
                     r"no outcome line in its log after 3s", result.stdout, re.M)
    assert "|PASS|" not in result.stdout


def test_a_failing_log_read_is_reported_and_fails_the_step(tmp_path):
    result, reads, _tree = lagging(tmp_path, "register-variants.sh", "--snapshot-key", SNAPSHOT_KEY,
                                   MILO_VARIANTS_POLL_SECONDS="0", MILO_VARIANTS_LOG_POLL_SECONDS="0",
                                   MILO_VARIANTS_LOG_WAIT_SECONDS="2", OPS_TEST_LOG_FAIL="1",
                                   OPS_TEST_OUTCOME="BUILT snapshot_key=cs1.a status=built rows=5/5")
    assert result.returncode == 1 and reads == 3
    assert "gcloud logging read exited 1 (after 0s)" in result.stderr
    assert "PERMISSION_DENIED" not in result.stdout + result.stderr, "the gcloud error text is not echoed"
    assert "SUMMARY|register-variants|FAIL|" in result.stdout


def test_the_bounded_wait_defaults_to_two_minutes_every_five_seconds():
    for script, prefix in (("register-variants.sh", "MILO_VARIANTS"), ("register-retention.sh", "MILO_RETENTION")):
        text = (OPS / script).read_text()
        assert f'log_wait="${{{prefix}_LOG_WAIT_SECONDS:-120}}" log_poll="${{{prefix}_LOG_POLL_SECONDS:-5}}"' in text
        assert "ops_execution_outcome " in text and "for _attempt in" not in text, script


def test_the_prune_reads_its_outcome_again_and_fails_without_one(tmp_path):
    args = ("register-retention.sh", "--apply", "--confirm", "PRUNE", "--digest", DIGEST)
    env = {"OPS_TEST_DIGEST": DIGEST, "MILO_RETENTION_POLL_SECONDS": "0", "MILO_RETENTION_LOG_POLL_SECONDS": "0",
           "MILO_RETENTION_LOG_WAIT_SECONDS": "3", "OPS_TEST_OUTCOME": "PRUNED snapshots=1 raw_records=5"}
    result, reads, _tree = lagging(tmp_path / "late", *args, OPS_TEST_LOG_READY_AT="3", **env)
    assert result.returncode == 0 and reads == 3, result.stdout + result.stderr
    assert "SUMMARY|register-retention apply|PASS|snapshots=1 raw_records=5" in result.stdout
    result, reads, _tree = lagging(tmp_path / "never", *args, OPS_TEST_LOG_READY_AT="999", **env)
    assert result.returncode == 1 and reads == 4
    assert "no outcome line in its log after 3s" in result.stdout
    assert "register-retention apply|PASS|" not in result.stdout
    assert "gcloud logging read exited" not in result.stderr, "each read succeeded; the line was never there"


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
    # PR-L2: the compaction, dry-run first, from the same dispatch.
    assert inputs["compact"]["options"] == ["none", "dry-run", "apply"] and inputs["compact"]["default"] == "none"
    assert '--compact "${COMPACT_INPUT}"' in steps(doc)[-1]["run"]


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


def test_the_read_only_url_maps_its_parameters_or_refuses(tmp_path):
    """Every URL parameter libpq honours reaches its variable (never argv); one
    it cannot map refuses instead of connecting differently."""
    tree = vacuum_tree(tmp_path)
    mapped = vacuum(tree, "--sizes", MILO_READONLY_DB_URL=POOLER_URL + "?sslmode=require&application_name=milo-vacuum")
    assert mapped.returncode == 0, mapped.stdout + mapped.stderr
    unknown = vacuum(tree, "--sizes", MILO_READONLY_DB_URL=POOLER_URL + "?host=elsewhere.example.com")
    assert unknown.returncode == 1 and "the sizes could not be read (read-only role)" in unknown.stderr
    no_secret(mapped.stdout, mapped.stderr, unknown.stdout, unknown.stderr, "\n".join(tree.tool_calls()))

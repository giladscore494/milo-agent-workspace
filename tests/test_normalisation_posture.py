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
GCLOUD = r'''#!/usr/bin/env python3
import json, os, sys
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
    env = [{"name": "MILO_ENABLE_MANUFACTURER_NORMALISATION", "value": os.environ.get("T_FLAG", "false")}]
    print(json.dumps({"spec": {"template": {"spec": {"containers": [{"env": env}]}}}})); sys.exit(0)
if a[:2] == ["secrets", "get-iam-policy"]:
    members = ["serviceAccount:worker@test.iam.gserviceaccount.com"]
    print(json.dumps({"bindings": [{"role": "roles/secretmanager.secretAccessor", "members": members}]}))
    sys.exit(0)
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


def preflight(tmp_path: Path, **state: str) -> dict[str, dict]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "gcloud").write_text(GCLOUD)
    (bin_dir / "gcloud").chmod(0o755)
    config = tmp_path / "operator.env"
    config.write_text(CONFIG)
    report = tmp_path / "report.json"
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path), **state}
    subprocess.run(["bash", str(PREFLIGHT), "--operator-config", str(config), "--json-output", str(report)],
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


def _verify(tmp_path: Path, job: str, flag: str, list_fails: bool = False):
    tree = _verify_tree(tmp_path, readiness=READY, website=SITE_OFF, migration_detail=DB_OK)
    listing = "exit 1" if list_fails else (f"echo {JOB}" if job == "1" else "exit 0")
    described = json.dumps({"spec": {"template": {"spec": {"containers": [{"env": [
        {"name": FLAG, "value": flag}]}]}}}})
    source = tree.bin.joinpath("gcloud").read_text().replace(
        'case "$*" in\n',
        'case "$*" in\n'
        f'  "run jobs list --region test-region --project test-project --filter=metadata.name={JOB} '
        f'--format=value(metadata.name)") {listing} ;;\n'
        f"  *\"services describe\"*--format=json*) printf '%s\\n' '{described}' ;;\n", 1)
    tree.tool("gcloud", source)
    return tree.run("production-verify.sh", "--gate", "deployed")


@pytest.mark.parametrize(("job", "flag", "expected"), STATES)
def test_the_deployed_gate_reads_the_normalisation_job_against_its_flag(tmp_path, job, flag, expected):
    result = _verify(tmp_path, job, flag)
    assert f"NORMALISATION_JOB={expected} ({JOB}: " in result.stdout, result.stdout
    assert _verdict(result.stdout)["CODE_DEPLOYED"] == ("VERIFIED" if expected == "PASS" else "NO")
    assert result.returncode == (0 if expected == "PASS" else 1)


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
        text = (REPO / script).read_text()
        assert "secrets add-iam-policy-binding" not in text, script
    ensure = (REPO / "scripts/catalog/government-production-capture.sh").read_text()
    body = ensure[ensure.index("ensure_normalisation_job() {"):]
    body = body[:body.index("\n}\n")]
    assert '--service-account "$worker_sa"' in body and "CAPTURE_SERVICE_ACCOUNT" not in body
    bootstrap = (REPO / "scripts/deploy/gcp-bootstrap.sh").read_text()
    capture_grants = re.findall(r'for key in ([A-Z_ ]+); do\n\s+bind_accessor "\$\(milo_op "\$key"\)" '
                                r'"\$\(milo_op CAPTURE_SERVICE_ACCOUNT\)"', bootstrap)
    assert capture_grants == ["SECRET_SUPABASE_URL SECRET_SUPABASE_SERVICE_KEY"]

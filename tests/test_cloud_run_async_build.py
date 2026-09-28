"""PR-Ops3b: build-log streaming never decides a deploy's result.

cloud-run.sh submits each image build with ``--async``, reads the build id
from stdout alone, polls ``gcloud builds describe`` to a terminal status within
a deadline, shows a redacted, bounded log tail for anything but SUCCESS, and
proves the exact image tag exists before deploying it -- worker first, then
the API. Everything runs against the mocked gcloud of
tests/test_cloud_run_deploy_apply_mock.py; nothing real is contacted.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.test_cloud_run_deploy_apply_mock import API_IMAGE, WORKER_IMAGE, Deployment
from tests.test_ops_build_identity import preflight_tree, run_preflight

REPO = Path(__file__).resolve().parents[1]
WORKER_BUILD = "11111111-1111-4111-8111-111111111111"
API_BUILD = "22222222-2222-4222-8222-222222222222"
FAST = {"BUILD_POLL_SECONDS": "1"}

#: Credentials a build log could carry; none may reach the deploy's output.
LOG_SECRETS = {
    "SUPABASE_SECRET_KEY=sb_secret_LOGSENTINEL0001": "sb_secret_LOGSENTINEL0001",
    "psql postgresql://ro_user:LOG-SENTINEL-PW@db.sentinel.supabase.co:5432/postgres":
        "LOG-SENTINEL-PW",
    "Authorization: Bearer ya29.LOGSENTINELTOKEN0002": "ya29.LOGSENTINELTOKEN0002",
    "echo sb_secret_LOGSENTINELBARE3": "sb_secret_LOGSENTINELBARE3",
    # Assembled, so the repository secret scan does not see a PEM header here.
    "-----BEGIN " + "PRIVATE KEY----- MIIEvLOGSENTINELKEY4": "MIIEvLOGSENTINELKEY4",
}


def write(deployment: Deployment, name: str, text: str) -> None:
    (deployment.dir / name).write_text(text, encoding="utf-8")


def build_log(total: int = 100) -> str:
    """A build log as `gcloud logging read --order=desc` returns it: newest first."""
    lines = [f"Step log line {n:03d}" for n in range(1, total + 1)]
    lines[-5:-5] = list(LOG_SECRETS)          # secrets among the newest lines
    return "\n".join(reversed(lines)) + "\n"


def calls(deployment: Deployment, needle: str) -> list[str]:
    return [line for line in deployment.invocations() if needle in line]


def assert_nothing_deployed(deployment: Deployment) -> None:
    assert calls(deployment, "run jobs deploy") == []
    assert calls(deployment, "gcloud run deploy ") == []
    assert calls(deployment, "add-iam-policy-binding") == []
    assert calls(deployment, "builds cancel") == [], "stopping never touches the build"


@pytest.fixture()
def deployment(tmp_path: Path) -> Deployment:
    return Deployment(tmp_path)


# ---------------------------------------------------------------------------
# success
# ---------------------------------------------------------------------------

def test_success_submits_async_polls_verifies_and_deploys_worker_first(deployment):
    result = deployment.run(**FAST)
    assert result.returncode == 0, result.stderr
    submits = calls(deployment, "builds submit")
    assert len(submits) == 2
    for line in submits:
        assert "--async" in line and "--format=value(id)" in line
    log = deployment.invocations()

    def at(needle: str) -> int:
        return next(i for i, line in enumerate(log) if needle in line)

    order = [at("cloudbuild-worker.yaml"), at(f"builds describe {WORKER_BUILD}"),
             at(f"artifacts docker images describe {WORKER_IMAGE}"),
             at("cloudbuild-api.yaml"), at(f"builds describe {API_BUILD}"),
             at(f"artifacts docker images describe {API_IMAGE}"),
             at("run jobs deploy"), at("gcloud run deploy ")]
    assert order == sorted(order), "submit, poll, image check -- worker, then API, then the deploys"
    assert "--region us-central1" in calls(deployment, f"builds describe {WORKER_BUILD}")[0]
    assert calls(deployment, "logging read") == [], "a successful build's log is never read"
    assert f"Worker build {WORKER_BUILD}: SUCCESS" in result.stdout
    assert f"API build {API_BUILD}: SUCCESS" in result.stdout


def test_a_build_still_working_is_polled_until_it_succeeds(deployment):
    """WORKING, then SUCCESS: the poll waits for the terminal status."""
    counter = deployment.dir / "describes"
    mock = (deployment.bin / "gcloud").read_text(encoding="utf-8")
    (deployment.bin / "gcloud").write_text(mock.replace(
        '  "builds describe"*)\n',
        '  "builds describe"*)\n'
        f'    n=$(( $(cat "{counter}" 2>/dev/null || echo 0) + 1 )); echo "$n" > "{counter}"\n'
        '    if [[ "$n" -lt 3 ]]; then echo WORKING; exit 0; fi\n', 1), encoding="utf-8")
    result = deployment.run(**FAST)
    assert result.returncode == 0, result.stderr
    assert len(calls(deployment, f"builds describe {WORKER_BUILD}")) == 3


# ---------------------------------------------------------------------------
# a build that does not succeed stops the deploy
# ---------------------------------------------------------------------------

def test_failure_prints_the_last_80_log_lines_redacted_and_stops(deployment):
    write(deployment, f"build-status-{WORKER_BUILD}", "FAILURE\n")
    write(deployment, "build-log", build_log())
    result = deployment.run(**FAST)
    assert result.returncode != 0
    assert f"The Worker build {WORKER_BUILD} ended FAILURE. Nothing was deployed." in result.stderr
    read = calls(deployment, "logging read")
    assert len(read) == 1
    assert f"resource.type=build AND resource.labels.build_id={WORKER_BUILD}" in read[0]
    assert "--limit 80" in read[0] and "--order=desc" in read[0]
    tail = [line.split("| ", 1)[1] for line in result.stderr.splitlines() if line.startswith("    | ")]
    assert len(tail) == 80, "bounded to the last 80 lines"
    numbered = [line for line in tail if line.startswith("Step log line")]
    assert numbered == [f"Step log line {n:03d}" for n in range(26, 101)], "the newest lines, oldest first"
    for secret in LOG_SECRETS.values():
        assert secret not in result.stdout + result.stderr, secret
    assert "SUPABASE_SECRET_KEY=[REDACTED]" in result.stderr
    assert "[REDACTED PRIVATE KEY]" in result.stderr
    # The API was never built, and nothing was deployed.
    assert calls(deployment, "cloudbuild-api.yaml") == []
    assert calls(deployment, "artifacts docker images describe") == []
    assert_nothing_deployed(deployment)


def test_failure_with_an_unreadable_log_says_so_and_still_stops(deployment):
    write(deployment, f"build-status-{WORKER_BUILD}", "FAILURE\n")
    write(deployment, "build-log-unreadable", "")
    result = deployment.run(**FAST)
    assert result.returncode != 0
    assert f"the log of build {WORKER_BUILD} could not be read (gcloud logging read exit 1)" in result.stderr
    assert "the deploy stops regardless" in result.stderr
    assert "ended FAILURE. Nothing was deployed." in result.stderr
    assert_nothing_deployed(deployment)


def test_failure_with_an_empty_log_says_so_and_still_stops(deployment):
    write(deployment, f"build-status-{WORKER_BUILD}", "FAILURE\n")
    result = deployment.run(**FAST)
    assert result.returncode != 0
    assert "could not be read: no entries yet" in result.stderr
    assert_nothing_deployed(deployment)


@pytest.mark.parametrize("status", ["INTERNAL_ERROR", "TIMEOUT", "CANCELLED", "EXPIRED"])
def test_every_other_terminal_status_stops_the_deploy(deployment, status):
    write(deployment, f"build-status-{API_BUILD}", f"{status}\n")
    write(deployment, "build-log", build_log(10))
    result = deployment.run(**FAST)
    assert result.returncode != 0
    assert f"The API build {API_BUILD} ended {status}." in result.stderr
    assert len(calls(deployment, "logging read")) == 1
    assert_nothing_deployed(deployment)


def test_the_deadline_stops_the_deploy_and_names_the_running_build(deployment):
    write(deployment, f"build-status-{WORKER_BUILD}", "WORKING\n")
    write(deployment, "build-log", build_log(5))
    result = deployment.run(BUILD_DEADLINE_SECONDS="1", **FAST)
    assert result.returncode != 0
    assert f"The Worker build {WORKER_BUILD} is still WORKING after the 1s deadline." in result.stderr
    assert f"gcloud builds describe {WORKER_BUILD} --project=test-project --region=us-central1" \
        in result.stderr
    assert len(calls(deployment, f"builds describe {WORKER_BUILD}")) >= 2
    assert len(calls(deployment, "logging read")) == 1
    assert_nothing_deployed(deployment)


def test_an_unreadable_status_is_polled_until_the_deadline(deployment):
    """A describe that fails is not a result -- neither success nor failure."""
    mock = (deployment.bin / "gcloud").read_text(encoding="utf-8")
    (deployment.bin / "gcloud").write_text(mock.replace(
        '  "builds describe"*)\n', '  "builds describe"*)\n    echo "transient" >&2; exit 1\n', 1),
        encoding="utf-8")
    result = deployment.run(BUILD_DEADLINE_SECONDS="1", **FAST)
    assert result.returncode != 0
    assert "is still unreadable after the 1s deadline" in result.stderr
    assert_nothing_deployed(deployment)


@pytest.mark.parametrize("name,value", [
    ("BUILD_DEADLINE_SECONDS", "0"), ("BUILD_DEADLINE_SECONDS", "30m"), ("BUILD_POLL_SECONDS", "0"),
])
def test_a_bad_deadline_or_poll_interval_is_refused_before_anything_runs(deployment, name, value):
    result = deployment.run(**{name: value})
    assert result.returncode != 0 and name in result.stderr
    assert deployment.invocations() == []


# ---------------------------------------------------------------------------
# the build id
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("output,status", [
    ("", "0"),                                                     # missing
    ("build submitted\n", "0"),                                    # not an id
    ("11111111-1111-4111-8111-11111111111\n", "0"),                # one digit short
    (f"{WORKER_BUILD}\n{API_BUILD}\n", "0"),                       # two ids
    (f"{WORKER_BUILD} extra\n", "0"),                              # id with trailing text
    (f"{WORKER_BUILD}\n", "1"),                                    # an id, but submit failed
])
def test_anything_but_one_well_formed_build_id_is_refused(deployment, output, status):
    write(deployment, "submit-output", output)
    write(deployment, "submit-status", status)
    result = deployment.run(**FAST)
    assert result.returncode != 0
    assert "did not return exactly one well-formed build id on stdout. A build MAY have started" \
        in result.stderr
    assert "gcloud builds list --project=test-project --region=us-central1 --limit=5" in result.stderr
    assert calls(deployment, "builds describe") == []
    assert len(calls(deployment, "builds submit")) == 1, "the API build is not submitted"
    assert_nothing_deployed(deployment)


# ---------------------------------------------------------------------------
# the image must exist at the exact tag
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("label,image,build", [("Worker", WORKER_IMAGE, WORKER_BUILD),
                                               ("API", API_IMAGE, API_BUILD)])
def test_a_missing_image_after_success_stops_the_deploy(deployment, label, image, build):
    write(deployment, "missing-images", image + "\n")
    result = deployment.run(**FAST)
    assert result.returncode != 0
    assert f"The {label} build {build} reported SUCCESS, but {image} is not in Artifact Registry." \
        in result.stderr
    assert_nothing_deployed(deployment)


# ---------------------------------------------------------------------------
# redaction, and the Cloud Shell path
# ---------------------------------------------------------------------------

def test_the_redaction_matches_the_release_library_and_bounds_each_line():
    contract = REPO / "scripts" / "deploy" / "deployment-contract.sh"
    library = REPO / "scripts" / "release" / "lib" / "common.sh"
    samples = ["DATABASE_URL=postgresql://u:pw@h:5432/db", "token: abc", "Authorization: Bearer xyz.1",
               "https://user:pass@example.test/path", "plain build output line"]
    for sample in samples:
        ours = subprocess.run(["bash", "-c", f"source {contract}; printf '%s\\n' \"$1\" | milo_redact_stream",
                               "x", sample], capture_output=True, text=True, check=True).stdout.strip()
        theirs = subprocess.run(["bash", "-c", f"source {library}; redact_line \"$1\"", "x", sample],
                                capture_output=True, text=True, check=True).stdout.strip()
        assert ours == theirs, sample
    long_line = subprocess.run(["bash", "-c", f"source {contract}; printf '%s\\n' \"$1\" | milo_redact_stream",
                                "x", "a" * 2000], capture_output=True, text=True, check=True).stdout.strip()
    assert long_line == "a" * 500 + " [...]"


def test_the_cloud_shell_path_is_the_same_command():
    """No new variable is required: the deadline and poll interval have defaults."""
    script = (REPO / "scripts" / "deploy" / "cloud-run.sh").read_text(encoding="utf-8")
    assert "BUILD_DEADLINE_SECONDS=${BUILD_DEADLINE_SECONDS:-1800}" in script
    assert "BUILD_POLL_SECONDS=${BUILD_POLL_SECONDS:-15}" in script
    activate = (REPO / "scripts" / "deploy" / "production-activate.sh").read_text(encoding="utf-8")
    assert "BUILD_DEADLINE_SECONDS" not in activate


# ---------------------------------------------------------------------------
# the preflight probes the async path's permissions
# ---------------------------------------------------------------------------

def test_the_preflight_probes_what_the_async_build_path_needs(tmp_path):
    tree = preflight_tree(tmp_path)
    result = run_preflight(tree)
    assert result.returncode == 0, result.stdout + result.stderr
    probe = [call for call in tree.tool_calls() if call.startswith("curl ") and "cloudbuild.builds.get" in call
             and "logging.logEntries.list" in call and '"cloudbuild.builds.create"' in call
             and "artifactregistry.dockerimages.get" in call]
    assert probe, "one testIamPermissions probe covers submit, poll, log read and image check"
    assert "SUMMARY|permissions:build-wait|PASS|holds cloudbuild.builds.create cloudbuild.builds.get " \
           "logging.logEntries.list artifactregistry.dockerimages.get" in result.stdout


def test_the_preflight_names_a_missing_build_poll_or_log_permission(tmp_path):
    tree = preflight_tree(tmp_path)
    result = run_preflight(tree, OPS_TEST_DENY={
        "projects/test-project:testIamPermissions": ["cloudbuild.builds.get", "logging.logEntries.list"]})
    assert result.returncode == 1
    report = result.stdout.split("== Preflight report ==", 1)[1]
    assert "cloudbuild.builds.get (permissions:build-wait)" in report
    assert "logging.logEntries.list (permissions:build-wait)" in report

"""PR-Ops3b: build-log streaming never decides a deploy's result.

cloud-run.sh submits each image build with ``--async``, reads the build id
from stdout alone, polls ``gcloud builds describe`` to a terminal status within
a deadline, shows a redacted, bounded log tail for anything but SUCCESS, and
proves the exact image tag exists before deploying it -- worker first, then
the API. Everything runs against the mocked gcloud of
tests/test_cloud_run_deploy_apply_mock.py; nothing real is contacted.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from tests.test_cloud_run_deploy_apply_mock import API_IMAGE, DIGEST, WORKER_IMAGE, Deployment
from tests.test_ops_build_identity import preflight_tree, run_preflight

REPO = Path(__file__).resolve().parents[1]
WORKER_BUILD = "11111111-1111-4111-8111-111111111111"
API_BUILD = "22222222-2222-4222-8222-222222222222"
FAST = {"BUILD_POLL_SECONDS": "1"}
SHA = WORKER_IMAGE.rsplit(":", 1)[1]
WORKER_PATH = WORKER_IMAGE.rsplit(":", 1)[0]
API_PATH = API_IMAGE.rsplit(":", 1)[0]

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
             at(f"artifacts docker tags list {WORKER_PATH} --filter=tag:{SHA}"),
             at("cloudbuild-api.yaml"), at(f"builds describe {API_BUILD}"),
             at(f"artifacts docker tags list {API_PATH} --filter=tag:{SHA}"),
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
    assert calls(deployment, "artifacts docker tags list") == []
    assert_nothing_deployed(deployment)


def test_failure_with_an_unreadable_log_says_so_and_still_stops(deployment):
    write(deployment, f"build-status-{WORKER_BUILD}", "FAILURE\n")
    write(deployment, "build-log-unreadable", "")
    result = deployment.run(**FAST)
    assert result.returncode != 0
    assert f"the log of build {WORKER_BUILD} could not be read (gcloud logging read exit 1)" in result.stderr
    assert "the deploy stops regardless" in result.stderr
    # The read's own error is shown, not swallowed.
    assert "gcloud logging read said (redacted, at most 20 lines):" in result.stderr
    assert "PERMISSION_DENIED: logging.logEntries.list" in result.stderr
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
    """A describe that fails is not a result -- neither success nor failure --
    and at the deadline what the last failed one said is shown, redacted."""
    mock = (deployment.bin / "gcloud").read_text(encoding="utf-8")
    (deployment.bin / "gcloud").write_text(mock.replace(
        '  "builds describe"*)\n',
        '  "builds describe"*)\n'
        '    echo "ERROR: (gcloud.builds.describe) transient: Bearer ya29.DESCRIBESENTINEL5" >&2; exit 4\n', 1),
        encoding="utf-8")
    result = deployment.run(BUILD_DEADLINE_SECONDS="1", **FAST)
    assert result.returncode != 0
    assert "is still unreadable after the 1s deadline" in result.stderr
    assert f"status reads of build {WORKER_BUILD} FAILED (a failed read is not a status)" in result.stderr
    assert "the last failed status read (gcloud builds describe, exit 4) said" in result.stderr
    assert "    | ERROR: (gcloud.builds.describe) transient: Bearer [REDACTED]" in result.stderr
    assert "ya29.DESCRIBESENTINEL5" not in result.stdout + result.stderr
    assert_nothing_deployed(deployment)


def test_a_failed_status_read_then_success_proceeds(deployment):
    """One failed describe is not a verdict: the next poll decides."""
    counter = deployment.dir / "describes"
    mock = (deployment.bin / "gcloud").read_text(encoding="utf-8")
    (deployment.bin / "gcloud").write_text(mock.replace(
        '  "builds describe"*)\n',
        '  "builds describe"*)\n'
        f'    n=$(( $(cat "{counter}" 2>/dev/null || echo 0) + 1 )); echo "$n" > "{counter}"\n'
        '    if [[ "$n" -eq 1 ]]; then echo "ERROR: transient" >&2; exit 1; fi\n', 1), encoding="utf-8")
    result = deployment.run(**FAST)
    assert result.returncode == 0, result.stderr
    assert len(calls(deployment, f"builds describe {WORKER_BUILD}")) == 2
    assert f"Worker build {WORKER_BUILD}: SUCCESS" in result.stdout


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
    assert f"The {label} build {build} reported SUCCESS, but {image} is not in Artifact Registry: " \
        f"the tag lookup succeeded and lists no tag exactly '{SHA}'" in result.stderr
    assert "FAILED" not in result.stderr, "an absent tag is not a failed call"
    assert_nothing_deployed(deployment)


# ---------------------------------------------------------------------------
# PR-Ops3c: the exact tag, through `docker tags list`, never `images describe`
# ---------------------------------------------------------------------------

OTHER_DIGEST = "sha256:" + "2" * 64


def tag_row(image_path: str, tag: str, digest: str) -> str:
    """One `--format=value(tag,version)` row, as gcloud names both: resource paths."""
    package = ("projects/test-project/locations/us-central1/repositories/milo-agent/packages/"
               + image_path.rsplit("/", 1)[1])
    return f"{package}/tags/{tag}\t{package}/versions/{digest}\n"


def test_a_present_tag_shows_its_digest_and_never_describes_the_image(deployment):
    result = deployment.run(**FAST)
    assert result.returncode == 0, result.stderr
    assert f"Worker image: {WORKER_IMAGE} ({DIGEST})" in result.stdout
    assert f"API image: {API_IMAGE} ({DIGEST})" in result.stdout
    for path in (WORKER_PATH, API_PATH):
        [call] = calls(deployment, f"artifacts docker tags list {path} ")[:1]
        assert call == f"gcloud artifacts docker tags list {path} --filter=tag:{SHA} " \
                       f"--format=value(tag,version)"
    # The mock's describe fails as production's does (Container Analysis):
    # nothing on the deploy path may depend on it.
    assert calls(deployment, "images describe") == []
    assert f"registry digest: {DIGEST}" in result.stdout


def test_an_absent_tag_is_not_in_artifact_registry(deployment):
    write(deployment, "tags-list-output", "")
    result = deployment.run(**FAST)
    assert result.returncode != 0
    assert f"The Worker build {WORKER_BUILD} reported SUCCESS, but {WORKER_IMAGE} is not in " \
           "Artifact Registry" in result.stderr
    assert "FAILED" not in result.stderr
    assert calls(deployment, "cloudbuild-api.yaml") == [], "the API is not built"
    assert_nothing_deployed(deployment)


#: What a failed lookup can carry on stderr; none of it may be printed as is.
LOOKUP_SECRETS = ("ya29.LOOKUPSENTINEL6", "sb_secret_LOOKUPSENTINEL7", "LOOKUP-SENTINEL-PW")


def test_a_failed_lookup_shows_its_stderr_and_is_not_called_missing(deployment):
    stderr = (
        "ERROR: (gcloud.artifacts.docker.tags.list) PERMISSION_DENIED: Permission "
        "'artifactregistry.tags.list' denied on resource (or it may not exist).\n"
        "Authorization: Bearer ya29.LOOKUPSENTINEL6\n"
        "SUPABASE_SECRET_KEY=sb_secret_LOOKUPSENTINEL7\n"
        "proxy https://proxyuser:LOOKUP-SENTINEL-PW@proxy.example.test\n"
        + "".join(f"noise line {n:03d}\n" for n in range(1, 101))
    )
    write(deployment, "tags-list-stderr", stderr)
    write(deployment, "tags-list-status", "1")
    result = deployment.run(**FAST)
    assert result.returncode != 0
    out = result.stdout + result.stderr
    assert "not in Artifact Registry" not in out, "a failed call is never 'missing'"
    assert f"the Artifact Registry tag lookup for {WORKER_IMAGE} FAILED (exit 1). This is NOT a " \
           "missing image: whether it exists is unknown." in result.stderr
    assert "gcloud artifacts docker tags list (exit 1) said (redacted, at most 20 lines):" in result.stderr
    assert "    | ERROR: (gcloud.artifacts.docker.tags.list) PERMISSION_DENIED: Permission " \
           "'artifactregistry.tags.list' denied" in result.stderr
    # Redacted and bounded.
    for secret in LOOKUP_SECRETS:
        assert secret not in out, secret
    assert "Authorization: Bearer [REDACTED]" in result.stderr
    assert "SUPABASE_SECRET_KEY=[REDACTED]" in result.stderr
    assert "noise line 016" in result.stderr and "noise line 017" not in result.stderr
    assert_nothing_deployed(deployment)


@pytest.mark.parametrize("tags", [
    [f"release-{SHA}", f"{SHA}-rc1", f"{SHA}0", f"x{SHA}"],
    [SHA[:-1], SHA[1:]],
])
def test_a_tag_that_only_contains_the_sha_is_not_accepted(deployment, tags):
    """The filter's ':' is a substring match: those rows come back, and are refused."""
    write(deployment, "tags-list-output", "".join(tag_row(WORKER_PATH, tag, OTHER_DIGEST) for tag in tags))
    result = deployment.run(**FAST)
    assert result.returncode != 0
    assert f"{WORKER_IMAGE} is not in Artifact Registry: the tag lookup succeeded and lists no tag " \
           f"exactly '{SHA}'" in result.stderr
    assert OTHER_DIGEST not in result.stdout
    assert_nothing_deployed(deployment)


def test_only_the_exact_tag_decides_the_digest(deployment):
    rows = (tag_row(WORKER_PATH, f"release-{SHA}", OTHER_DIGEST) + tag_row(WORKER_PATH, SHA, DIGEST)
            + tag_row(WORKER_PATH, f"{SHA}-rc1", OTHER_DIGEST))
    write(deployment, "tags-list-output", rows)
    result = deployment.run(**FAST)
    assert result.returncode == 0, result.stderr
    assert f"Worker image: {WORKER_IMAGE} ({DIGEST})" in result.stdout
    assert OTHER_DIGEST not in result.stdout + result.stderr


@pytest.mark.parametrize("rows", [
    tag_row(WORKER_PATH, SHA, DIGEST) + tag_row(WORKER_PATH, SHA, OTHER_DIGEST),   # two digests
    tag_row(WORKER_PATH, SHA, "sha256:abc"),                                       # not a digest
    tag_row(WORKER_PATH, SHA, "md5:" + "1" * 32),
    f"{SHA}\n",                                                                     # no version
])
def test_the_exact_tag_must_yield_exactly_one_sha256_digest(deployment, rows):
    write(deployment, "tags-list-output", rows)
    result = deployment.run(**FAST)
    assert result.returncode != 0
    assert f"the tag '{SHA}' on {WORKER_PATH} did not resolve to exactly one sha256 digest" in result.stderr
    assert_nothing_deployed(deployment)


def test_the_exact_tag_matching_itself():
    """milo_exact_tag_digests: last path segment, exact, de-duplicated."""
    contract = REPO / "scripts" / "deploy" / "deployment-contract.sh"
    rows = (tag_row(WORKER_PATH, SHA, DIGEST) + tag_row(WORKER_PATH, SHA, DIGEST)
            + tag_row(WORKER_PATH, f"a{SHA}", OTHER_DIGEST) + f"{SHA}\t{OTHER_DIGEST}\n")
    out = subprocess.run(["bash", "-c", f"source {contract}; milo_exact_tag_digests \"$1\"", "x", SHA],
                         input=rows, capture_output=True, text=True, check=True).stdout
    assert out.splitlines() == [DIGEST, OTHER_DIGEST]
    argv = subprocess.run(["bash", "-c", f"source {contract}; milo_tags_list_command \"$1\"; "
                           "printf '%s\\n' \"${MILO_TAGS_LIST_COMMAND[@]}\"", "x", WORKER_IMAGE],
                          capture_output=True, text=True, check=True).stdout.splitlines()
    assert argv == ["gcloud", "artifacts", "docker", "tags", "list", WORKER_PATH,
                    f"--filter=tag:{SHA}", "--format=value(tag,version)"]


def test_the_build_path_hides_no_gcloud_error():
    """No `2>/dev/null` and no `|| true` on the build path's gcloud calls."""
    script = (REPO / "scripts" / "deploy" / "cloud-run.sh").read_text(encoding="utf-8")
    build_path = script[script.index("submit_build() {"):script.index("# run_build LABEL")]
    assert "2>/dev/null" not in build_path and "|| true" not in build_path
    assert "images describe" not in script


def _code_part(line: str) -> str:
    """A line without its comment: a whole-line comment, or a trailing
    ` # ...` (shell, YAML and Python all comment that way)."""
    if line.lstrip().startswith("#"):
        return ""
    return re.split(r"\s#(?:\s|$)", line, maxsplit=1)[0]


def test_no_script_or_workflow_calls_images_describe():
    """`gcloud artifacts docker images describe` also reads Container Analysis,
    which the deployer does not hold; where that API is enabled it fails for
    an image that exists. Only comments may name it, in scripts/ and .github/."""
    offenders = []
    for root in (REPO / "scripts", REPO / ".github"):
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if re.search(r"images\s+describe", _code_part(line)):
                    offenders.append(f"{path.relative_to(REPO)}:{number}: {line.strip()}")
    assert offenders == []


def test_the_guard_sees_code_and_ignores_comments():
    assert "images describe" in _code_part("  gcloud artifacts docker images describe x")
    assert _code_part("x=1  # images describe") == "x=1 "
    assert "images describe" not in _code_part("# never `images describe`")
    assert "images describe" not in _code_part('  "roles/x"   # images describe (old)')
    assert "images describe" in _code_part("    gcloud artifacts docker images describe ${REF}")


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
             and "artifactregistry.tags.list" in call]
    assert probe, "one testIamPermissions probe covers submit, poll, log read and image check"
    assert "SUMMARY|permissions:build-wait|PASS|holds cloudbuild.builds.create cloudbuild.builds.get " \
           "logging.logEntries.list artifactregistry.tags.list" in result.stdout
    build_wait = '{"permissions": ["cloudbuild.builds.create", "cloudbuild.builds.get", ' \
                 '"logging.logEntries.list", "artifactregistry.tags.list"]}'
    assert [call for call in tree.tool_calls() if build_wait in call], \
        "the image step is proved by artifactregistry.tags.list, not artifactregistry.dockerimages.get"


def test_the_preflight_runs_the_deploys_exact_tag_lookup_and_no_image_describe(tmp_path):
    tree = preflight_tree(tmp_path)
    result = run_preflight(tree)
    assert result.returncode == 0, result.stdout + result.stderr
    gcloud = [call for call in tree.tool_calls() if call.startswith("gcloud ")]
    assert not [call for call in gcloud if "images describe" in call], "no Container Analysis read"
    lookups = [call for call in gcloud if call.startswith("gcloud artifacts docker tags list ")]
    assert len(lookups) == 2
    for image, call in zip(("api", "worker"), lookups):
        path = call.split()[5]
        assert path.endswith(f"/{image}") and ":" not in path, call
        assert call.endswith(f" --filter=tag:{tree.sha} --format=value(tag,version)"), call
    assert "SUMMARY|api-image-tag|PASS|" in result.stdout
    assert "SUMMARY|worker-image-tag|PASS|" in result.stdout
    # The same argv cloud-run.sh builds (one definition, deployment-contract.sh).
    script = (REPO / "scripts" / "ops" / "preflight-deployer.sh").read_text(encoding="utf-8")
    assert script.count('"${MILO_TAGS_LIST_COMMAND[@]}"') == 2
    deploy = (REPO / "scripts" / "deploy" / "cloud-run.sh").read_text(encoding="utf-8")
    assert 'milo_image_digest_lookup "$image" "$GCLOUD_ERROR_FILE"' in deploy
    contract = (REPO / "scripts" / "deploy" / "deployment-contract.sh").read_text(encoding="utf-8")
    assert 'output=$("${MILO_TAGS_LIST_COMMAND[@]}" 2>"$error_file")' in contract


def test_the_preflight_names_a_missing_tags_list_permission(tmp_path):
    tree = preflight_tree(tmp_path)
    result = run_preflight(tree, OPS_TEST_DENY={
        "projects/test-project:testIamPermissions": ["artifactregistry.tags.list"]})
    assert result.returncode == 1
    report = result.stdout.split("== Preflight report ==", 1)[1]
    assert "artifactregistry.tags.list (permissions:build-wait)" in report


def test_the_preflight_names_a_missing_build_poll_or_log_permission(tmp_path):
    tree = preflight_tree(tmp_path)
    result = run_preflight(tree, OPS_TEST_DENY={
        "projects/test-project:testIamPermissions": ["cloudbuild.builds.get", "logging.logEntries.list"]})
    assert result.returncode == 1
    report = result.stdout.split("== Preflight report ==", 1)[1]
    assert "cloudbuild.builds.get (permissions:build-wait)" in report
    assert "logging.logEntries.list (permissions:build-wait)" in report

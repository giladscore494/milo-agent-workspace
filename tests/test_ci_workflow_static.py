"""The CI gate contract: what `.github/workflows/ci.yml` and `repo-scan.yml`
must keep enforcing on every pull request (there is no post-merge run).

Why this exists: the release runbook
(docs/production-readiness/SCOPED_BATCH_PRODUCTION_RUNBOOK.md) accepts a
commit only when the mandatory `ci` jobs are green, so those jobs ARE the
release gate. Before this module nothing failed if an edit quietly dropped
the strict PostgreSQL flag, the pipefail/skip enforcement, a scan step or the
bundle-scan requirement, or made a required job conditional.

Two kinds of coverage, in the style of test_supabase_migration_workflow_static:

  * STRUCTURAL assertions over the PARSED workflows (PyYAML, installed with
    backend/requirements.txt via uvicorn[standard]) -- job and step
    identity, env values, trigger filters, conditions. Commands are matched
    as whole command lines with comments stripped, so a comment or an
    unrelated step can never satisfy them;
  * EXECUTABLE assertions -- the PostgreSQL gate script and Repo Scan's
    integrity and change-scope scripts are extracted from the workflow and
    run under bash against stubs / throwaway git repositories, so "a skip
    fails the gate" and "an unrelated PR skips only the prototype lint" are
    demonstrated rather than asserted about strings.

Nothing here contacts GitHub or any network service.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
CI = REPO / ".github" / "workflows" / "ci.yml"
REPO_SCAN = REPO / ".github" / "workflows" / "repo-scan.yml"

MANDATORY_CI_JOBS = ("offline-checks", "frontend-and-docker", "postgres-checks", "e2e")
PG_GATE_MODULES = ("tests/test_migrations_postgres.py", "tests/test_worker_rpc_acl_postgres.py")
PG_GATE_STEP = "Executable migration + concurrency + RLS + RPC ACL suite (skips forbidden)"
BACKEND_STEP = "Backend offline tests"

BASH = shutil.which("bash")


# -----------------------------------------------------------------------------
# Parsing helpers
# -----------------------------------------------------------------------------

def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def triggers(workflow: dict) -> dict:
    # YAML 1.1 reads a bare `on` key as boolean True.
    return workflow.get("on", workflow.get(True))


def job(workflow: dict, name: str) -> dict:
    jobs = workflow["jobs"]
    assert name in jobs, f"mandatory job {name!r} is missing"
    return jobs[name]


def step(job_def: dict, name: str) -> dict:
    matches = [s for s in job_def["steps"] if s.get("name") == name]
    assert len(matches) == 1, f"expected exactly one step named {name!r}, found {len(matches)}"
    return matches[0]


def command_lines(step_def: dict) -> list[str]:
    """The executable lines of a step's `run`, comments and blanks removed."""
    lines = []
    for raw in (step_def.get("run") or "").splitlines():
        line = raw.split(" #", 1)[0].strip() if not raw.strip().startswith("#") else ""
        if line:
            lines.append(line)
    return lines


def steps_running(job_def: dict, command: str) -> list[dict]:
    """Steps that execute `command` as a whole command line."""
    return [s for s in job_def["steps"] if command in command_lines(s)]


def assert_runs(job_def: dict, command: str, *, working_directory: str | None = None) -> dict:
    found = steps_running(job_def, command)
    assert found, f"no step runs `{command}`"
    s = found[0]
    if working_directory is not None:
        assert s.get("working-directory") == working_directory, (command, s.get("working-directory"))
    return s


def assert_unconditional(job_name: str, job_def: dict) -> None:
    assert "if" not in job_def, f"job {job_name} is conditional: {job_def['if']!r}"
    assert not job_def.get("continue-on-error"), f"job {job_name} may fail without failing CI"
    for s in job_def["steps"]:
        label = s.get("name") or s.get("uses")
        assert "if" not in s, f"{job_name}: step {label!r} is conditional: {s['if']!r}"
        assert not s.get("continue-on-error"), f"{job_name}: step {label!r} may fail silently"


def pytest_invocation(step_def: dict) -> list[str]:
    lines = [line for line in command_lines(step_def) if re.match(r"^(python -m )?pytest\b", line)]
    assert len(lines) == 1, f"expected one pytest command, found {lines}"
    return shlex.split(lines[0].split("|", 1)[0])


# -----------------------------------------------------------------------------
# ci.yml: mandatory jobs, triggers, no conditions
# -----------------------------------------------------------------------------

def test_every_mandatory_ci_job_exists():
    jobs = load(CI)["jobs"]
    for name in MANDATORY_CI_JOBS:
        assert name in jobs, name


def test_ci_runs_on_every_pull_request_without_path_filters_and_not_after_merge():
    on = triggers(load(CI))
    assert "pull_request" in on
    pr = on["pull_request"] or {}
    for key in ("paths", "paths-ignore", "branches", "branches-ignore", "types"):
        assert key not in pr, f"pull_request trigger is narrowed by {key}"
    # The release gate is CI on the merged PR's latest commit plus tree
    # equality with the merge commit (SCOPED_BATCH_PRODUCTION_RUNBOOK.md), so
    # there is deliberately no post-merge push run.
    assert "push" not in on


def test_no_mandatory_job_or_step_is_conditional_or_allowed_to_fail():
    workflow = load(CI)
    for name in MANDATORY_CI_JOBS:
        assert_unconditional(name, job(workflow, name))


def test_the_release_runbook_names_every_mandatory_job():
    runbook = (REPO / "docs" / "production-readiness" / "SCOPED_BATCH_PRODUCTION_RUNBOOK.md").read_text()
    section = runbook.split("Check CI for that exact commit", 1)[1].split("**Stop if**", 1)[0]
    for name in MANDATORY_CI_JOBS:
        assert f"`{name}`" in section, f"the runbook's CI acceptance omits {name}"
    assert set(load(CI)["jobs"]) <= set(MANDATORY_CI_JOBS), (
        "a new ci job must be added to MANDATORY_CI_JOBS and to the runbook")


# -----------------------------------------------------------------------------
# PostgreSQL: strict gate, nothing dropped from both jobs, no silent skips
# -----------------------------------------------------------------------------

def test_postgres_checks_is_strict():
    pg = job(load(CI), "postgres-checks")
    assert str(pg["env"]["MILO_REQUIRE_PG_TESTS"]) == "1"
    gate = step(pg, PG_GATE_STEP)
    lines = command_lines(gate)
    assert "set -o pipefail" in lines
    assert lines.index("set -o pipefail") < next(i for i, l in enumerate(lines) if l.startswith("pytest"))
    args = pytest_invocation(gate)
    for module in PG_GATE_MODULES:
        assert module in args, f"postgres-checks no longer runs {module}"
    assert "-rs" in args
    assert_runs(pg, "python scripts/check_migrations.py")


def test_offline_checks_ignores_only_modules_the_strict_gate_runs():
    workflow = load(CI)
    offline = job(workflow, "offline-checks")
    args = pytest_invocation(step(offline, BACKEND_STEP))
    assert "tests" in args, "the backend step no longer collects the tests/ tree"
    ignored = {a.split("=", 1)[1] for a in args if a.startswith("--ignore=")}
    deselect = [a for a in args if a in ("-k", "-m", "--deselect") or a.startswith(("--deselect", "-k", "-m"))]
    assert not deselect, f"the backend suite deselects tests: {deselect}"
    gate_args = pytest_invocation(step(job(workflow, "postgres-checks"), PG_GATE_STEP))
    for path in ignored:
        if path.startswith("tests/"):
            assert path in gate_args, f"{path} is ignored by offline-checks AND not run by postgres-checks"
    assert "legacy/milo-streamlit-v1/test_websearch.py" in ignored  # paid calls; AGENTS.md


def _modules_that_start_postgres() -> list[str]:
    found = []
    for path in sorted((REPO / "tests").glob("test_*.py")):
        if path.name == Path(__file__).name:
            continue
        text = path.read_text()
        if "initdb" in text or "EphemeralPostgres" in text:
            found.append(f"tests/{path.name}")
    return found


def test_every_postgres_module_sits_behind_a_strict_gate():
    """A module that starts a real PostgreSQL either runs in postgres-checks,
    or runs in offline-checks with MILO_REQUIRE_PG_TESTS honoured -- never in
    a place where a missing binary turns it into a green skip."""
    workflow = load(CI)
    offline = job(workflow, "offline-checks")
    gate_args = pytest_invocation(step(job(workflow, "postgres-checks"), PG_GATE_STEP))
    offline_args = pytest_invocation(step(offline, BACKEND_STEP))
    ignored = {a.split("=", 1)[1] for a in offline_args if a.startswith("--ignore=")}
    modules = _modules_that_start_postgres()
    assert set(PG_GATE_MODULES) <= set(modules), modules
    for module in modules:
        if module in gate_args:
            continue
        assert module not in ignored, f"{module} is run by no job"
        assert str(offline["env"].get("MILO_REQUIRE_PG_TESTS")) == "1", (
            f"{module} runs in offline-checks, which does not forbid PostgreSQL skips")
        text = (REPO / module).read_text()
        assert "MILO_REQUIRE_PG_TESTS" in text or "_require_pg_bin" in text, (
            f"{module} has a PostgreSQL skip path that ignores MILO_REQUIRE_PG_TESTS")


def test_offline_checks_has_the_history_the_release_binding_tests_need():
    offline = job(load(CI), "offline-checks")
    checkout = next(s for s in offline["steps"] if str(s.get("uses", "")).startswith("actions/checkout"))
    assert (checkout.get("with") or {}).get("fetch-depth") == 0


def _write_stub_pytest(bin_dir: Path, *, output: str, status: int) -> Path:
    log = bin_dir / "pytest-args"
    stub = bin_dir / "pytest"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$@" > "{log}"\n'
        f"printf '%s\\n' {shlex.quote(output)}\n"
        f"exit {status}\n")
    stub.chmod(0o755)
    return log


@pytest.mark.skipif(BASH is None, reason="bash unavailable")
@pytest.mark.parametrize("output,status,passes", [
    ("314 passed in 250.00s", 0, True),
    ("313 passed, 1 skipped in 250.00s", 0, False),   # a skip fails the gate
    ("SKIPPED [1] tests/x.py:1: no binaries", 0, False),
    ("313 passed, 1 failed in 250.00s", 1, False),   # pipefail: tee cannot mask it
    ("", 1, False),                                    # MILO_REQUIRE_PG_TESTS error
])
def test_the_postgres_gate_script_fails_on_skips_and_failures(tmp_path, output, status, passes):
    script = step(job(load(CI), "postgres-checks"), PG_GATE_STEP)["run"]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = _write_stub_pytest(bin_dir, output=output, status=status)
    result = subprocess.run(
        [BASH, "-e", "-c", script], cwd=tmp_path, capture_output=True, text=True, timeout=60,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)})
    assert (result.returncode == 0) is passes, (output, status, result.stdout, result.stderr)
    for module in PG_GATE_MODULES:
        assert module in log.read_text().split()


# -----------------------------------------------------------------------------
# Core scans, readiness self-check, frontend and images
# -----------------------------------------------------------------------------

def test_offline_checks_keeps_the_core_safety_scans():
    offline = job(load(CI), "offline-checks")
    for command in ("python scripts/check_migrations.py",
                    "python scripts/secret_scan.py",
                    "python scripts/check_unsafe_defaults.py",
                    "python scripts/release/validate_production_manifest.py --manifest config/production.example.yaml --mode plan",
                    "bash scripts/release/production-readiness.sh"):
        assert_runs(offline, command)
    shellcheck = [line for s in offline["steps"] for line in command_lines(s)
                  if line.startswith("shellcheck -x -S warning")]
    assert shellcheck, "the release/operator ShellCheck lint is gone"
    for target in ("scripts/release/*.sh", "scripts/deploy/*.sh", "scripts/catalog/*.sh"):
        assert target in shellcheck[0].split(), target


def test_the_readiness_step_is_labelled_offline_not_production_verification():
    offline = job(load(CI), "offline-checks")
    readiness = assert_runs(offline, "bash scripts/release/production-readiness.sh")
    assert "OFFLINE" in readiness["name"] and "does NOT verify live production" in readiness["name"]


def test_frontend_and_docker_keeps_every_build_and_frontend_gate():
    fe = job(load(CI), "frontend-and-docker")
    assert_runs(fe, "docker build -f Dockerfile.api -t milo-agent-api:ci .")
    assert_runs(fe, "docker build -f Dockerfile.worker -t milo-agent-worker:ci .")
    ordered = ["npm ci", "npm run build", "npx tsc --noEmit", "npm test -- --run",
               "npm run test:static", "npm run test:secrets"]
    positions = []
    for command in ordered:
        s = assert_runs(fe, command, working_directory="frontend")
        positions.append(fe["steps"].index(s))
    assert positions == sorted(positions), "frontend gates are out of order (the bundle scan needs the build)"
    secrets = assert_runs(fe, "npm run test:secrets", working_directory="frontend")
    assert str((secrets.get("env") or {}).get("MILO_REQUIRE_BUNDLE_SCAN")) == "1"


def test_e2e_runs_the_isolated_suite():
    e2e = job(load(CI), "e2e")
    assert_runs(e2e, "npx playwright test", working_directory="frontend")


# -----------------------------------------------------------------------------
# Repo Scan: always-emitted check, always-run archive integrity
# -----------------------------------------------------------------------------

INTEGRITY_STEP = "Archive integrity (SHA256 manifest, always)"
SCOPE_STEP = "Decide whether the prototype snapshot is affected"


def test_repo_scan_always_reports_and_always_verifies_the_archive():
    workflow = load(REPO_SCAN)
    on = triggers(workflow)
    assert "pull_request" in on and not (on["pull_request"] or {})
    assert "push" not in on
    scan = job(workflow, "scan")
    assert scan["name"] == "Lint & Test"
    assert "if" not in scan and not scan.get("continue-on-error")
    integrity = step(scan, INTEGRITY_STEP)
    assert "if" not in integrity
    assert "sha256sum --strict -c archive/SHA256SUMS.txt" in command_lines(integrity)
    assert scan["steps"].index(integrity) < scan["steps"].index(step(scan, SCOPE_STEP))
    for name in ("Lint (ruff)", "Run tests"):
        s = step(scan, name)
        # Only the scope decision may switch these off, and only on an explicit
        # 'false' -- a missing or failed decision runs them.
        assert s.get("if") == "steps.scope.outputs.run_prototype != 'false'", name
    assert "ruff check ." in command_lines(step(scan, "Lint (ruff)"))
    assert "pytest -v test_safe_agent_pipeline.py test_safety_guards.py" in command_lines(step(scan, "Run tests"))


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, timeout=60).stdout.strip()


def _snapshot_repo(tmp_path: Path) -> Path:
    """A throwaway git repository holding a copy of the real snapshots."""
    root = tmp_path / "repo"
    for rel in [*(line.split()[1] for line in (REPO / "archive" / "SHA256SUMS.txt").read_text().splitlines()),
                "archive/SHA256SUMS.txt", "backend/unrelated.py"]:
        src = REPO / rel
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.exists():
            shutil.copyfile(src, dst)
        else:
            dst.write_text("x = 1\n")
    _git(root, "init", "-q")
    _git(root, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "add", "-A")
    _git(root, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-q", "-m", "base")
    return root


def _commit_change(root: Path, rel: str) -> None:
    path = root / rel
    path.write_text(path.read_text() + "\n# change\n")
    _git(root, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-qam", f"touch {rel}")


def _run_step(root: Path, name: str, env: dict | None = None) -> subprocess.CompletedProcess:
    script = step(job(load(REPO_SCAN), "scan"), name)["run"]
    return subprocess.run([BASH, "-e", "-c", script], cwd=root, capture_output=True, text=True,
                          timeout=60, env={"PATH": "/usr/bin:/bin", "HOME": str(root), **(env or {})})


@pytest.mark.skipif(BASH is None or shutil.which("sha256sum") is None, reason="bash/sha256sum unavailable")
def test_the_archive_integrity_step_passes_on_the_real_snapshot_and_fails_on_drift(tmp_path):
    root = _snapshot_repo(tmp_path)
    assert _run_step(root, INTEGRITY_STEP).returncode == 0

    (root / "legacy" / "milo-streamlit-v1" / "app.py").write_text("tampered\n")
    assert _run_step(root, INTEGRITY_STEP).returncode != 0

    root = _snapshot_repo(tmp_path / "second")
    extra = root / "legacy" / "milo-streamlit-v1" / "extra.py"
    extra.write_text("x = 1\n")
    _git(root, "add", str(extra))
    assert _run_step(root, INTEGRITY_STEP).returncode != 0, "an unlisted snapshot file must fail"


def _scope(root: Path, tmp_path: Path, **env: str) -> str:
    output = tmp_path / "gh-output"
    output.write_text("")
    result = _run_step(root, SCOPE_STEP, {"GITHUB_OUTPUT": str(output), **env})
    assert result.returncode == 0, result.stderr
    values = dict(line.split("=", 1) for line in output.read_text().splitlines() if "=" in line)
    return values["run_prototype"]


@pytest.mark.skipif(BASH is None, reason="bash unavailable")
@pytest.mark.parametrize("event", ["pull_request", "push"])
@pytest.mark.parametrize("changed,expected", [
    ("backend/unrelated.py", "false"),
    ("legacy/milo-streamlit-v1/app.py", "true"),
    ("legacy/milo-streamlit-v1/test_safety_guards.py", "true"),
    ("archive/SHA256SUMS.txt", "true"),
])
def test_the_prototype_checks_run_exactly_when_the_snapshot_can_have_changed(tmp_path, event, changed, expected):
    root = _snapshot_repo(tmp_path)
    base = _git(root, "rev-parse", "HEAD")
    _commit_change(root, changed)
    key = "PR_BASE_SHA" if event == "pull_request" else "PUSH_BEFORE_SHA"
    assert _scope(root, tmp_path, EVENT_NAME=event, **{key: base}) == expected


@pytest.mark.skipif(BASH is None, reason="bash unavailable")
@pytest.mark.parametrize("env", [
    {"EVENT_NAME": "push", "PUSH_BEFORE_SHA": "0" * 40},              # first push / new ref
    {"EVENT_NAME": "push", "PUSH_BEFORE_SHA": ""},
    {"EVENT_NAME": "pull_request", "PR_BASE_SHA": "f" * 40},            # base not in the clone
    {"EVENT_NAME": "pull_request", "PR_BASE_SHA": "not-a-sha; exit 0"},
])
def test_an_undeterminable_change_set_runs_the_prototype_checks(tmp_path, env):
    root = _snapshot_repo(tmp_path)
    _commit_change(root, "backend/unrelated.py")
    assert _scope(root, tmp_path, **env) == "true"


def test_the_workflow_change_itself_forces_the_prototype_checks(tmp_path):
    """Editing repo-scan.yml must exercise what it edits."""
    text = step(job(load(REPO_SCAN), "scan"), SCOPE_STEP)["run"]
    assert r"\.github/workflows/repo-scan\.yml$" in text


def test_this_guard_runs_in_the_mandatory_backend_job():
    offline = load(CI)["jobs"]["offline-checks"]
    args = pytest_invocation(step(offline, BACKEND_STEP))
    ignored = {a.split("=", 1)[1] for a in args if a.startswith("--ignore=")}
    assert "tests" in args and f"tests/{Path(__file__).name}" not in ignored
    assert os.path.basename(__file__) == "test_ci_workflow_static.py"

"""PR-Ops2 A: the checkout stays clean after authenticating to Google Cloud.

google-github-actions/auth writes gha-creds-<hash>.json into GITHUB_WORKSPACE
(create_credentials_file defaults to true). The first keyless deploy stopped at
production-preflight.sh `release:worktree-clean` because of it, and without a
.gcloudignore `gcloud builds submit .` would have uploaded it to the Cloud
Build source bucket. What this proves:

1.  .gitignore, .dockerignore and .gcloudignore all exclude gha-creds-*.json.
2.  A workspace holding gha-creds-abc.json is clean for git_worktree_clean --
    and release:worktree-clean is NOT weakened: any other untracked or modified
    file still makes it dirty.
3.  The credentials file is not in the gcloud upload list, and the upload list
    is otherwise exactly what gcloud's generated default gave before
    (`gcloud meta list-files-for-upload` when gcloud is installed; a static
    check of .gcloudignore always).
4.  Every workflow that authenticates checks `git status --porcelain` in the
    step right after the auth step.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_ops_workflows import PRODUCTION_WORKFLOWS, steps, workflow

REPO = Path(__file__).resolve().parents[1]
CREDS = "gha-creds-abc.json"
# What gcloud generates when a directory has .git / .gitignore and no
# .gcloudignore (googlecloudsdk/command_lib/util/gcloudignore.py,
# DEFAULT_IGNORE_FILE): the build context before this file existed.
GCLOUD_DEFAULT = [".gcloudignore", ".git", ".gitignore", "#!include:.gitignore"]


def lines(name: str) -> list[str]:
    return [line.strip() for line in (REPO / name).read_text(encoding="utf-8").splitlines()]


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-c", "user.email=t@example.test", "-c", "user.name=t", *args],
                          cwd=cwd, check=True, capture_output=True, text=True)


def release_copy(tmp_path: Path) -> Path:
    """The tracked files of this checkout, committed in a fresh repository."""
    root = tmp_path / "release"
    tracked = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, check=True,
                             capture_output=True).stdout.decode().split("\0")
    for relative in filter(None, tracked):
        source = REPO / relative
        if not source.is_file():
            continue
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    git(root.parent, "init", "-q", str(root))
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "release")
    return root


# =============================================================================
# 1. the three ignore files
# =============================================================================

@pytest.mark.parametrize("name", [".gitignore", ".dockerignore", ".gcloudignore"])
def test_every_ignore_file_excludes_the_auth_credentials_file(name):
    assert "gha-creds-*.json" in lines(name)


def test_gcloudignore_keeps_gclouds_generated_default_and_only_adds_the_credentials_file():
    patterns = [line for line in lines(".gcloudignore") if line and not line.startswith("# ")
                and line != "#"]
    assert patterns == [*GCLOUD_DEFAULT, "gha-creds-*.json"]


# =============================================================================
# 2. release:worktree-clean
# =============================================================================

def worktree_clean(root: Path) -> bool:
    script = f"source {REPO / 'scripts/release/lib/common.sh'}; git_worktree_clean"
    return subprocess.run(["bash", "-c", script], cwd=root).returncode == 0


def test_a_workspace_holding_the_credentials_file_is_clean(tmp_path):
    root = release_copy(tmp_path)
    assert worktree_clean(root)
    (root / CREDS).write_text('{"type": "external_account"}', encoding="utf-8")
    assert worktree_clean(root), "the auth step's credentials file must not make the checkout dirty"
    assert git(root, "status", "--porcelain").stdout == ""


@pytest.mark.parametrize("change", ["untracked", "modified", "credentials-lookalike"])
def test_release_worktree_clean_still_blocks_every_other_change(tmp_path, change):
    root = release_copy(tmp_path)
    (root / CREDS).write_text("{}", encoding="utf-8")
    if change == "untracked":
        (root / "backend" / "stray.py").write_text("x = 1\n", encoding="utf-8")
    elif change == "modified":
        with (root / "backend" / "main.py").open("a", encoding="utf-8") as handle:
            handle.write("# local change\n")
    else:
        (root / "gha-creds-abc.py").write_text("x = 1\n", encoding="utf-8")
    assert not worktree_clean(root)


def test_the_preflight_check_itself_is_unchanged():
    common = (REPO / "scripts/release/lib/common.sh").read_text(encoding="utf-8")
    assert 'git_worktree_clean() {\n  [[ -z "$(git status --porcelain 2> /dev/null)" ]]\n}' in common
    preflight = (REPO / "scripts/deploy/production-preflight.sh").read_text(encoding="utf-8")
    assert 'if git_worktree_clean; then\n  record_check PASS "release:worktree-clean"' in preflight


# =============================================================================
# 3. the gcloud upload list
# =============================================================================

def test_the_credentials_file_is_excluded_from_the_upload_statically(tmp_path):
    """.gcloudignore's patterns (its #!include:.gitignore expanded) evaluated by
    git's own gitignore matcher, which gcloudignore follows: the credentials
    file is excluded and every file the two images are built from is not."""
    patterns = []
    for line in lines(".gcloudignore"):
        if line == "#!include:.gitignore":
            patterns.extend(lines(".gitignore"))
        else:
            patterns.append(line)
    rules = tmp_path / "rules"
    rules.write_text("\n".join(patterns) + "\n", encoding="utf-8")
    probe = tmp_path / "probe"
    git(tmp_path, "init", "-q", str(probe))
    candidates = [CREDS, "gha-creds-0123456789abcdef.json", "Dockerfile.api", "Dockerfile.worker",
                  "backend/main.py", "backend/requirements.txt", "scripts/deploy/cloudbuild-api.yaml",
                  "scripts/deploy/cloudbuild-worker.yaml", ".dockerignore"]
    result = subprocess.run(["git", "-c", f"core.excludesFile={rules}", "check-ignore", "--no-index",
                             *candidates], cwd=probe, capture_output=True, text=True)
    assert set(result.stdout.split()) == {CREDS, "gha-creds-0123456789abcdef.json"}


@pytest.mark.skipif(shutil.which("gcloud") is None, reason="gcloud is not installed here")
def test_gcloud_uploads_the_same_files_as_before_without_the_credentials_file(tmp_path):
    root = release_copy(tmp_path)
    (root / CREDS).write_text("{}", encoding="utf-8")
    env = {**os.environ, "CLOUDSDK_CONFIG": str(tmp_path / "gcloud-config"),
           "CLOUDSDK_CORE_DISABLE_PROMPTS": "1"}
    listed = subprocess.run(["gcloud", "meta", "list-files-for-upload", "."], cwd=root, env=env,
                            check=True, capture_output=True, text=True).stdout.split()
    assert CREDS not in listed
    tracked = set(git(root, "ls-files").stdout.split())
    # Before: gcloud's generated default (every tracked file but .gitignore).
    # Now: the same, and .gcloudignore itself (which did not exist) is not sent.
    assert set(listed) == tracked - {".gitignore", ".gcloudignore"}

    # And the default really was that: without .gcloudignore, the credentials
    # file WAS uploaded -- the bug this closes.
    (root / ".gcloudignore").unlink()
    (root / ".gitignore").write_text(
        (root / ".gitignore").read_text(encoding="utf-8").replace("gha-creds-*.json\n", ""), encoding="utf-8")
    before = subprocess.run(["gcloud", "meta", "list-files-for-upload", "."], cwd=root, env=env,
                            check=True, capture_output=True, text=True).stdout.split()
    assert CREDS in before
    assert set(before) - {CREDS} == set(listed)


# =============================================================================
# 4. the workflows check it right after authenticating
# =============================================================================

@pytest.mark.parametrize("name", PRODUCTION_WORKFLOWS)
def test_the_step_right_after_auth_proves_the_checkout_clean(name):
    all_steps = steps(workflow(name))
    (auth,) = [index for index, step in enumerate(all_steps)
               if str(step.get("uses", "")).startswith("google-github-actions/auth@")]
    check = all_steps[auth + 1]
    assert check["name"] == "The checkout is still clean after authenticating"
    script = check["run"]
    assert script.startswith("set -euo pipefail\n")
    assert 'dirty="$(git status --porcelain)"' in script
    assert "--ignored" not in script and "--untracked-files=no" not in script
    assert "exit 1" in script and "::error::" in script
    assert "grep -qxF 'gha-creds-*.json' .gcloudignore" in script
    assert "env" not in check, "the check needs no secret"


def test_the_clean_check_fails_on_a_stray_file_and_passes_on_the_credentials_file(tmp_path):
    root = release_copy(tmp_path)
    script = steps(workflow("deploy.yml"))
    (auth,) = [index for index, step in enumerate(script)
               if str(step.get("uses", "")).startswith("google-github-actions/auth@")]
    body = script[auth + 1]["run"]
    (root / CREDS).write_text("{}", encoding="utf-8")
    ok = subprocess.run(["bash", "-c", body], cwd=root, capture_output=True, text=True)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    (root / "stray.txt").write_text("x", encoding="utf-8")
    dirty = subprocess.run(["bash", "-c", body], cwd=root, capture_output=True, text=True)
    assert dirty.returncode == 1
    assert "?? stray.txt" in dirty.stdout and CREDS not in dirty.stdout

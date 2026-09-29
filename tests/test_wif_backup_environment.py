"""PR-OBS: the scheduled backup's GitHub environment `production-backup` is
admitted by the EXISTING deploy WIF provider (scripts/ops/setup-wif.sh), and
the deploy preflight reports a provider that does not admit it yet as a GAP
(WARN) -- never as a reason to stop a deploy."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CHECK = REPO / "scripts" / "deploy" / "check-wif-environment.sh"
PREFLIGHT = REPO / "scripts" / "deploy" / "production-preflight.sh"
SETUP_WIF = REPO / "scripts" / "ops" / "setup-wif.sh"
SETUP_BACKUP = REPO / "scripts" / "ops" / "setup-backup.sh"
REPOSITORY = "giladscore494/milo-agent-workspace"
NEW = (f"assertion.repository == '{REPOSITORY}' && assertion.ref == 'refs/heads/main' && "
       "assertion.environment in ['production', 'production-kill-switch', 'production-backup']")
OLD = (f"assertion.repository == '{REPOSITORY}' && assertion.ref == 'refs/heads/main' && "
       "assertion.environment in ['production', 'production-kill-switch']")


def _run_check(tmp_path: Path, condition: str | None) -> subprocess.CompletedProcess:
    gcloud = tmp_path / "gcloud"
    gcloud.write_text("#!/usr/bin/env bash\n"
                      'printf "%s\\n" "$*" >> "$CALLS"\n'
                      'if [[ -z "$CONDITION" ]]; then echo "PERMISSION_DENIED" >&2; exit 1; fi\n'
                      'printf "%s\\n" "$CONDITION"\n')
    gcloud.chmod(0o755)
    env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}", CONDITION=condition or "",
               CALLS=str(tmp_path / "calls"))
    return subprocess.run(["bash", str(CHECK), "test-project"], capture_output=True, text=True,
                          env=env, timeout=60)


def test_the_new_condition_passes(tmp_path):
    result = _run_check(tmp_path, NEW)
    assert result.returncode == 0
    assert result.stdout == (f"PASS provider github-actions admits production-backup (repository {REPOSITORY} "
                             "and refs/heads/main still pinned)\n")
    call = (tmp_path / "calls").read_text()
    assert call.startswith("iam workload-identity-pools providers describe github-actions "
                           "--workload-identity-pool milo-github --location global --project test-project")


def test_the_condition_in_production_today_is_a_gap_not_a_failure(tmp_path):
    result = _run_check(tmp_path, OLD)
    assert result.returncode == 0
    assert result.stdout.startswith("GAP provider github-actions does not admit production-backup yet")
    assert "scripts/ops/setup-wif.sh --apply" in result.stdout


def test_an_unreadable_provider_is_reported_not_failed(tmp_path):
    result = _run_check(tmp_path, None)
    assert result.returncode == 0
    assert result.stdout.startswith("UNREADABLE provider milo-github/github-actions")


@pytest.mark.parametrize("condition", [
    "assertion.ref == 'refs/heads/main' && assertion.environment in ['production-backup']",
    f"assertion.repository == '{REPOSITORY}' && assertion.environment in ['production-backup']",
    f"assertion.repository == 'someone/else' && assertion.ref == 'refs/heads/main' && "
    "assertion.environment in ['production-backup']",
])
def test_a_condition_without_the_repository_and_main_pins_is_never_a_pass(tmp_path, condition):
    result = _run_check(tmp_path, condition)
    assert result.returncode == 0
    assert result.stdout.startswith("GAP provider github-actions condition is not the one setup-wif.sh writes")


def test_the_check_reads_the_pool_and_provider_ids_from_setup_wif():
    text = SETUP_WIF.read_text()
    assert 'POOL_ID="milo-github"' in text and 'PROVIDER_ID="github-actions"' in text
    check = CHECK.read_text()
    assert "milo-github" not in check.replace("milo-agent-workspace", "")  # never repeated


def test_the_preflight_maps_the_check_to_pass_or_warn_only():
    text = PREFLIGHT.read_text()
    block = text[text.index("WIF_BACKUP_CHECK="):text.index("# No worker execution may be in flight")]
    assert 'record_check PASS "wif:admits-production-backup"' in block
    assert 'record_check WARN "wif:admits-production-backup"' in block
    assert "BLOCKED" not in block


def test_a_deploy_preflight_with_the_old_condition_still_passes(tmp_path):
    """The exact mapping the preflight uses, through the real record_check /
    finish_checks: the old condition yields WARN and RESULT: OK (exit 0)."""
    gap = _run_check(tmp_path, OLD).stdout.strip()
    text = PREFLIGHT.read_text()
    block = text[text.index("case \"$WIF_BACKUP_CHECK\" in"):text.index("# No worker execution may be in flight")]
    script = (f'source "{REPO}/scripts/release/lib/common.sh"\n'
              f"WIF_BACKUP_CHECK={gap!r}\n" + block + '\nfinish_checks "preflight-under-test" ""\n')
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "WARN" in result.stdout and "wif:admits-production-backup" in result.stdout
    assert "RESULT: OK" in result.stdout


def test_setup_backup_binds_the_environment_principal_set_not_the_repository_one():
    text = SETUP_BACKUP.read_text()
    assert 'PRINCIPAL="principalSet://iam.googleapis.com/${POOL_NAME}/attribute.environment/${ENVIRONMENT_NAME}"' in text
    assert "attribute.repository/" not in text
    assert 'POOL_ID="milo-github"' in text and 'PROVIDER_ID="github-actions"' in text
    # It reads the provider and never writes it.
    for verb in ("providers create-oidc", "providers update-oidc", "workload-identity-pools create"):
        assert verb not in text
    assert re.search(r'^ENVIRONMENT_NAME="production-backup"$', text, re.M)


FORGED = [
    # An appended `||` makes the whole condition true in CEL.
    NEW + " || true",
    OLD + " || assertion.environment in ['production-backup']",
    NEW.replace(" && assertion.ref", " || assertion.ref"),
    "!(" + NEW + ")",
]


@pytest.mark.parametrize("condition", FORGED)
def test_a_forged_condition_is_never_a_pass(tmp_path, condition):
    result = _run_check(tmp_path, condition)
    assert result.returncode == 0
    assert not result.stdout.startswith("PASS"), result.stdout


def test_the_preflight_names_unreadable_as_unverifiable_not_as_a_missing_setup(tmp_path):
    unreadable = _run_check(tmp_path, None).stdout.strip()
    text = PREFLIGHT.read_text()
    block = text[text.index("case \"$WIF_BACKUP_CHECK\" in"):text.index("# No worker execution may be in flight")]
    script = (f'source "{REPO}/scripts/release/lib/common.sh"\nPROJECT_ID=test-project\n'
              f"WIF_BACKUP_CHECK={unreadable!r}\n" + block + '\nfinish_checks "preflight-under-test" ""\n')
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "not verifiable with this identity" in result.stdout
    assert "check-wif-environment.sh test-project" in result.stdout
    assert "setup-wif.sh --apply" not in result.stdout
    assert "RESULT: OK" in result.stdout

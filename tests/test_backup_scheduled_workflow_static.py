"""PR-OBS OBS-1/OBS-2: the contract of .github/workflows/backup-supabase-scheduled.yml,
and of the manual backup workflow it must leave untouched."""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "backup-supabase-scheduled.yml"
MANUAL = REPO / ".github" / "workflows" / "backup-supabase-production.yml"
TOOL = REPO / "scripts" / "ops" / "supabase_backup.py"


def load() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def triggers(workflow: dict) -> dict:
    return workflow.get("on", workflow.get(True))


def cron_minutes(workflow: dict) -> list[tuple[str, str]]:
    return [tuple(entry["cron"].split()[:2]) for entry in triggers(workflow)["schedule"]]


def test_schedules_are_daily_and_monthly_and_off_the_hour():
    on = triggers(load())
    crons = [entry["cron"] for entry in on["schedule"]]
    assert crons == ["17 2 * * *", "41 4 3 * *"]
    for minute, _hour in cron_minutes(load()):
        assert minute not in ("0", "30"), "scheduled on the hour or half hour"
    assert on["workflow_dispatch"]["inputs"]["job"]["options"] == ["backup", "restore-test"]


def test_each_job_runs_on_exactly_its_own_schedule_or_dispatch():
    jobs = load()["jobs"]
    assert set(jobs) == {"backup", "restore-test"}
    assert "github.event.schedule == '17 2 * * *'" in jobs["backup"]["if"]
    assert "inputs.job == 'backup'" in jobs["backup"]["if"]
    assert "github.event.schedule == '41 4 3 * *'" in jobs["restore-test"]["if"]
    assert "inputs.job == 'restore-test'" in jobs["restore-test"]["if"]


def test_both_jobs_use_the_reviewerless_backup_environment_and_minimal_permissions():
    workflow = load()
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] is False
    for job in workflow["jobs"].values():
        assert job["environment"] == "production-backup"
        assert job["permissions"] == {"contents": "read", "id-token": "write"}
        assert "env" not in job, "secrets reach a step through that step's env only"
        first = job["steps"][0]
        assert 'refs/heads/main' in first["run"]


def test_authentication_is_keyless_and_uses_the_dedicated_identities():
    text = WORKFLOW.read_text()
    assert "credentials_json" not in text and "GOOGLE_APPLICATION_CREDENTIALS" not in text
    jobs = load()["jobs"]
    expected = {"backup": "${{ secrets.MILO_BACKUP_WRITER_SA }}",
                "restore-test": "${{ secrets.MILO_BACKUP_READER_SA }}"}
    for name, account in expected.items():
        auth = [s for s in jobs[name]["steps"] if str(s.get("uses", "")).startswith("google-github-actions/auth@")]
        assert len(auth) == 1
        assert auth[0]["with"] == {"workload_identity_provider": "${{ vars.MILO_BACKUP_WIF_PROVIDER }}",
                                   "service_account": account}
    # The deploy identity is never used here.
    assert "GCP_DEPLOY_SERVICE_ACCOUNT" not in text


def test_secrets_are_step_scoped_and_never_interpolated_into_scripts():
    for job in load()["jobs"].values():
        for step in job["steps"]:
            run = step.get("run", "")
            assert "${{" not in run, f"expression interpolated into a script: {step.get('name')}"
            assert "set -x" not in run
    # Only these steps receive secret material.
    jobs = load()["jobs"]
    with_secrets = {(name, step["name"]) for name, job in jobs.items() for step in job["steps"]
                    if "secrets." in str(step.get("env", ""))}
    assert with_secrets == {
        ("backup", "Install the PostgreSQL client of the server's major"),
        ("backup", "Create, encrypt and verify the backup"),
        ("backup", "Report the failure to Sentry (only when SENTRY_DSN is set)"),
        ("restore-test", "Restore into the throwaway database and verify"),
        ("restore-test", "Report the failure to Sentry (only when SENTRY_DSN is set)"),
    }


def test_a_failure_is_red_and_reported_once_when_sentry_is_configured():
    for name, job in load()["jobs"].items():
        reports = [s for s in job["steps"] if s.get("if") == "failure()"]
        assert len(reports) == 1
        assert reports[0]["env"] == {"SENTRY_DSN": "${{ secrets.SENTRY_DSN }}"}
        assert f"--job {name}" in reports[0]["run"]
        for step in job["steps"]:
            assert not step.get("continue-on-error"), "a failing step must fail the job"


def test_the_restore_test_runs_a_postgres_service_of_the_backup_major():
    service = load()["jobs"]["restore-test"]["services"]["postgres"]
    assert service["image"] == "postgres:${{ vars.MILO_BACKUP_PG_MAJOR || '17' }}"
    restore = [s for s in load()["jobs"]["restore-test"]["steps"] if s.get("name", "").startswith("Restore")][0]
    assert "--migrations supabase/migrations" in restore["run"]


def test_the_tool_encrypts_exactly_like_the_manual_workflow():
    tool = TOOL.read_text()
    manual = MANUAL.read_text()
    for fragment in ("-aes-256-cbc", "-pbkdf2", "-iter 600000", "-md sha256"):
        assert fragment in manual
    assert '["openssl", "enc", "-aes-256-cbc", "-salt", "-pbkdf2", "-iter", "600000", "-md", "sha256"]' in tool
    assert '["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "600000", "-md", "sha256"]' in tool
    assert '"-pass", "env:MILO_BACKUP_PASSPHRASE"' in tool
    assert '"ifGenerationMatch": "0"' in tool


def test_the_manual_backup_workflow_is_unchanged_by_this_pr():
    # Pinned to its content at main 85f1a4c (PR-EV #158): the manual workflow
    # keeps working exactly as before.
    digest = hashlib.sha256(MANUAL.read_bytes()).hexdigest()
    base = subprocess.run(["git", "show", "85f1a4c:.github/workflows/backup-supabase-production.yml"],
                          cwd=REPO, capture_output=True)
    if base.returncode == 0:
        assert digest == hashlib.sha256(base.stdout).hexdigest()
    text = MANUAL.read_text()
    assert re.search(r"^on:\n  workflow_dispatch:", text, re.M)
    assert "environment: production" in text

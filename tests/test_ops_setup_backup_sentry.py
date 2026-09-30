"""PR-OBS: scripts/ops/setup-backup.sh and scripts/ops/setup-sentry.sh against
a stateful gcloud/gh stand-in (tests/ops_setup_stubs.py).

Proves: they converge from nothing to the required state; a second run
changes nothing (idempotent); --check changes nothing; every grant is
least-privilege and bucket/secret-scoped; every printed line is PASS/FAIL;
no secret value reaches argv or output.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from tests.ops_setup_stubs import STUB_SOURCE

REPO = Path(__file__).resolve().parents[1]
SETUP_BACKUP = REPO / "scripts" / "ops" / "setup-backup.sh"
SETUP_SENTRY = REPO / "scripts" / "ops" / "setup-sentry.sh"
PROJECT = "milo-test-project"
RO_URL = "postgresql://milo_ro.secretref:ro-password-SENTINEL@db.example.test:5432/postgres"
DSN = "https://dsnpublickeySENTINEL@o1.ingest.sentry.io/4242"
REPOSITORY = "giladscore494/milo-agent-workspace"
#: The deploy provider's condition after `setup-wif.sh --apply` (PR-OBS)...
PROVIDER_CONDITION = (f"assertion.repository == '{REPOSITORY}' && assertion.ref == 'refs/heads/main' && "
                      "assertion.environment in ['production', 'production-kill-switch', 'production-backup']")
#: ...and before it (production today).
OLD_PROVIDER_CONDITION = (f"assertion.repository == '{REPOSITORY}' && assertion.ref == 'refs/heads/main' && "
                          "assertion.environment in ['production', 'production-kill-switch']")


POOL = "principalSet://iam.googleapis.com/projects/123456789/locations/global/workloadIdentityPools/milo-github"
DEPLOYER_KEY = f"sa:milo-github-deployer@{PROJECT}.iam.gserviceaccount.com"
NARROWED = [f"{POOL}/attribute.environment/production", f"{POOL}/attribute.environment/production-kill-switch"]


class Env:
    def __init__(self, tmp_path: Path):
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        for name in ("gcloud", "gh"):
            path = self.bin / name
            path.write_text(STUB_SOURCE)
            path.chmod(0o755)
        self.state = tmp_path / "state.json"
        self.log = tmp_path / "argv.log"
        self.log.write_text("")
        self.config = tmp_path / "operator.env"
        self.config.write_text(
            f"GCP_PROJECT_ID={PROJECT}\nGCP_REGION=us-central1\n"
            "CLOUD_RUN_API_SERVICE=milo-agent-api\nCLOUD_RUN_WORKER_JOB=milo-agent-worker\n"
            f"API_SERVICE_ACCOUNT=milo-api-runtime@{PROJECT}.iam.gserviceaccount.com\n"
            f"WORKER_SERVICE_ACCOUNT=milo-worker-runtime@{PROJECT}.iam.gserviceaccount.com\n"
            f"CAPTURE_SERVICE_ACCOUNT=milo-catalog-capture@{PROJECT}.iam.gserviceaccount.com\n")
        ro = self.home / ".milo_ro_url"
        ro.write_text(RO_URL + "\n")
        ro.chmod(0o600)
        # The existing deploy pool and provider (setup-wif.sh owns them).
        # ...and the deployer already narrowed by setup-wif.sh --apply.
        self.state.write_text(json.dumps({
            "pools": ["milo-github"],
            "providers": {"github-actions": {"condition": PROVIDER_CONDITION}},
            "policies": {DEPLOYER_KEY: {"bindings": [{"role": "roles/iam.workloadIdentityUser",
                                                      "members": NARROWED}]}}}))

    def run(self, script: Path, *args: str) -> subprocess.CompletedProcess:
        env = {"PATH": f"{self.bin}:{os.environ['PATH']}", "HOME": str(self.home),
               "STUB_STATE": str(self.state), "STUB_LOG": str(self.log), "LANG": "C.UTF-8"}
        return subprocess.run(["bash", str(script), "--operator-config", str(self.config), *args],
                              env=env, capture_output=True, text=True, timeout=120)

    def data(self) -> dict:
        return json.loads(self.state.read_text()) if self.state.exists() else {}

    def mutations(self) -> list:
        return self.data().get("mutations", [])


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def _only_pass_fail(result):
    for line in (result.stdout + result.stderr).splitlines():
        assert line.startswith(("PASS ", "FAIL ")), f"not a PASS/FAIL line: {line!r}"


def _no_secret_anywhere(env: Env, result, *extra: str):
    passphrase = (env.home / ".milo_backup_passphrase").read_text() \
        if (env.home / ".milo_backup_passphrase").exists() else "\0"
    text = result.stdout + result.stderr
    argv = env.log.read_text()
    for secret in (RO_URL, "ro-password-SENTINEL", passphrase, DSN, "dsnpublickeySENTINEL", *extra):
        assert secret not in text, "a secret reached the output"
        assert secret not in argv, "a secret reached a command line"
    assert "@" not in text, "an e-mail-shaped value reached the output"


# -- setup-backup.sh -------------------------------------------------------------------

def test_setup_backup_converges_least_privilege(env):
    first = env.run(SETUP_BACKUP, "--pg-major", "17")
    assert first.returncode == 0, first.stdout + first.stderr
    _only_pass_fail(first)
    _no_secret_anywhere(env, first)
    data = env.data()
    bucket = data["buckets"][f"gs://{PROJECT}-milo-supabase-backups"]
    assert bucket["location"] == "US-CENTRAL1"
    assert bucket["uniform_bucket_level_access"] is True
    assert bucket["public_access_prevention"] == "enforced"
    assert bucket["retention_policy"] == {"retentionPeriod": "604800", "isLocked": False}
    assert bucket["lifecycle_config"] == {"rule": [{"action": {"type": "Delete"}, "condition": {"age": 30}}]}
    writer = f"serviceAccount:milo-backup-writer@{PROJECT}.iam.gserviceaccount.com"
    reader = f"serviceAccount:milo-backup-reader@{PROJECT}.iam.gserviceaccount.com"
    bucket_policy = data["policies"][f"bucket:gs://{PROJECT}-milo-supabase-backups"]["bindings"]
    assert {b["role"]: b["members"] for b in bucket_policy} == {
        "roles/storage.objectCreator": [writer], "roles/storage.objectViewer": [reader]}
    # No project-level role, for anyone.
    assert data["policies"].get("project", {"bindings": []})["bindings"] == []
    # The existing pool and provider are READ, never created or changed.
    assert data["pools"] == ["milo-github"]
    assert data["providers"] == {"github-actions": {"condition": PROVIDER_CONDITION}}
    assert not [m for m in env.mutations() if m[0] in ("create-pool", "create-oidc", "update-oidc")]
    principal = ("principalSet://iam.googleapis.com/projects/123456789/locations/global/"
                 "workloadIdentityPools/milo-github/attribute.environment/production-backup")
    for account in ("milo-backup-writer", "milo-backup-reader"):
        bindings = data["policies"][f"sa:{account}@{PROJECT}.iam.gserviceaccount.com"]["bindings"]
        assert bindings == [{"role": "roles/iam.workloadIdentityUser", "members": [principal]}]
    # The environment: no reviewer, main only, secrets set from stdin.
    environment = data["environments"]["production-backup"]
    assert environment["rules"] == []
    assert environment["policy"] == {"protected_branches": False, "custom_branch_policies": True}
    assert [p["name"] for p in environment["branches"]] == ["main"]
    passphrase_file = env.home / ".milo_backup_passphrase"
    assert stat.S_IMODE(passphrase_file.stat().st_mode) == 0o600
    passphrase = passphrase_file.read_text()
    assert len(passphrase) >= 32 and "\n" not in passphrase
    assert environment["secrets"]["MILO_BACKUP_PASSPHRASE"] == passphrase
    assert environment["secrets"]["MILO_BACKUP_DB_URL"] == RO_URL
    assert environment["secrets"]["MILO_BACKUP_WRITER_SA"] == writer.split(":", 1)[1]
    assert environment["variables"] == {
        "MILO_BACKUP_BUCKET": f"{PROJECT}-milo-supabase-backups",
        "MILO_BACKUP_PG_MAJOR": "17"}
    # The deployer is never touched.
    assert not any("milo-github-deployer" in json.dumps(m) for m in env.mutations())


def test_setup_backup_is_idempotent(env):
    assert env.run(SETUP_BACKUP, "--pg-major", "17").returncode == 0
    before = env.data()
    passphrase = (env.home / ".milo_backup_passphrase").read_text()
    count = len(before["mutations"])
    second = env.run(SETUP_BACKUP, "--pg-major", "17")
    assert second.returncode == 0, second.stdout
    after = env.data()
    new = [m for m in after["mutations"][count:] if m[0] != "secret-set"]
    assert new == [], f"a second run changed {new}"
    # Secrets are re-written with the SAME values (gh cannot compare), nothing else.
    assert (env.home / ".milo_backup_passphrase").read_text() == passphrase
    assert after["environments"] == before["environments"]
    for key in ("buckets", "policies", "pools", "providers", "accounts", "apis"):
        assert after[key] == before[key], key
    check = env.run(SETUP_BACKUP, "--check", "--pg-major", "17")
    assert check.returncode == 0 and "FAIL" not in check.stdout


def test_an_enabled_api_with_similarly_named_services_is_found_exactly(env):
    """`config.name:X` is not exact: production answered storage.googleapis.com
    with bigquerystorage, storage-api, storage-component and storage, one per
    line, and the check read "not enabled". The exact filter finds it, and
    nothing is enabled again."""
    assert env.run(SETUP_BACKUP, "--pg-major", "17").returncode == 0
    data = env.data()
    data["apis"] = ["bigquerystorage.googleapis.com", "storage-api.googleapis.com",
                    "storage-component.googleapis.com", "storage.googleapis.com",
                    *(a for a in data["apis"] if a != "storage.googleapis.com")]
    env.state.write_text(json.dumps(data))
    count = len(data["mutations"])
    for args in (("--check",), ()):
        result = env.run(SETUP_BACKUP, *args, "--pg-major", "17")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "PASS API storage.googleapis.com enabled" in result.stdout
        assert "FAIL" not in result.stdout
    assert not [m for m in env.mutations()[count:] if m[0] == "enable"]
    assert '--filter=config.name=storage.googleapis.com' in env.log.read_text()


def test_a_missing_environment_is_created_on_apply_and_a_fail_line_on_check(env):
    """`gh api` answers a missing environment with its 404 document on stdout
    and exit 1. That body was concatenated with '{}', python3 crashed and
    `set -e` ended the script silently before the environment was created."""
    check = env.run(SETUP_BACKUP, "--check", "--pg-major", "17")
    assert check.returncode == 1
    _only_pass_fail(check)
    assert ("FAIL GitHub environment production-backup is not as required (missing)" in check.stdout)
    # The script went on past the environment: the secrets were still checked.
    assert "FAIL environment secret MILO_BACKUP_DB_URL is not set in production-backup" in check.stdout
    assert "Traceback" not in check.stderr and "production-backup" not in env.data().get("environments", {})
    applied = env.run(SETUP_BACKUP, "--pg-major", "17")
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert "PASS GitHub environment production-backup: no required reviewer, deployments from main only" \
        in applied.stdout
    assert "production-backup" in env.data()["environments"]


def test_an_unreadable_environment_answer_is_a_fail_line_not_a_silent_exit(env):
    assert env.run(SETUP_BACKUP, "--pg-major", "17").returncode == 0
    data = env.data()
    data["gh_environment_garbage"] = True
    env.state.write_text(json.dumps(data))
    check = env.run(SETUP_BACKUP, "--check", "--pg-major", "17")
    assert check.returncode == 1
    _only_pass_fail(check)
    assert "FAIL GitHub environment production-backup is not as required (unreadable)" in check.stdout
    assert "PASS environment secret MILO_BACKUP_DB_URL is set (value not shown)" in check.stdout


def test_no_setup_or_deploy_script_matches_an_api_by_substring():
    """The same `config.name:` pattern was in setup-wif.sh and cloud-run.sh."""
    for script in sorted((REPO / "scripts").rglob("*.sh")):
        assert "config.name:" not in script.read_text(), script


def test_setup_backup_check_changes_nothing_and_fails_on_a_fresh_project(env):
    result = env.run(SETUP_BACKUP, "--check", "--pg-major", "17")
    assert result.returncode == 1
    _only_pass_fail(result)
    assert env.mutations() == []
    assert not (env.home / ".milo_backup_passphrase").exists()


def test_setup_backup_refuses_to_replace_a_passphrase_whose_local_copy_is_lost(env):
    assert env.run(SETUP_BACKUP, "--pg-major", "17").returncode == 0
    original = env.data()["environments"]["production-backup"]["secrets"]["MILO_BACKUP_PASSPHRASE"]
    (env.home / ".milo_backup_passphrase").unlink()
    result = env.run(SETUP_BACKUP, "--pg-major", "17")
    assert result.returncode == 1
    assert "restore the file from the password manager" in result.stdout
    assert env.data()["environments"]["production-backup"]["secrets"]["MILO_BACKUP_PASSPHRASE"] == original
    rotated = env.run(SETUP_BACKUP, "--pg-major", "17", "--rotate-passphrase")
    assert rotated.returncode == 0, rotated.stdout
    assert env.data()["environments"]["production-backup"]["secrets"]["MILO_BACKUP_PASSPHRASE"] \
        == (env.home / ".milo_backup_passphrase").read_text() != original


def test_setup_backup_reports_an_extra_role_as_a_failure(env):
    assert env.run(SETUP_BACKUP, "--pg-major", "17").returncode == 0
    data = env.data()
    key = f"bucket:gs://{PROJECT}-milo-supabase-backups"
    data["policies"][key]["bindings"].append({
        "role": "roles/storage.objectAdmin",
        "members": [f"serviceAccount:milo-backup-writer@{PROJECT}.iam.gserviceaccount.com"]})
    env.state.write_text(json.dumps(data))
    result = env.run(SETUP_BACKUP, "--check", "--pg-major", "17")
    assert result.returncode == 1
    assert "FAIL milo-backup-writer holds more than roles/storage.objectCreator" in result.stdout


def test_setup_backup_refuses_a_group_readable_url_file(env):
    (env.home / ".milo_ro_url").chmod(0o644)
    result = env.run(SETUP_BACKUP, "--pg-major", "17")
    assert result.returncode == 1
    assert "FAIL the read-only URL file ~/.milo_ro_url is missing or not mode 600" in result.stdout


def test_setup_backup_rejects_unknown_arguments(env):
    result = env.run(SETUP_BACKUP, "--delete-everything")
    assert result.returncode == 2
    assert result.stdout.startswith("FAIL unknown argument")


# -- setup-sentry.sh ---------------------------------------------------------------------

def _dsn_file(env: Env) -> Path:
    path = env.home / ".milo_sentry_dsn"
    path.write_text(DSN + "\n")
    path.chmod(0o600)
    return path


def test_setup_sentry_without_a_dsn_keeps_reporting_off(env):
    result = env.run(SETUP_SENTRY, "--no-github")
    assert result.returncode == 0, result.stdout
    _only_pass_fail(result)
    data = env.data()
    assert data["secrets"] == {"SENTRY_DSN": []}
    assert "error reporting stays OFF" in result.stdout
    members = {m for b in data["policies"]["secret:SENTRY_DSN"]["bindings"] for m in b["members"]}
    assert members == {f"serviceAccount:milo-{n}@{PROJECT}.iam.gserviceaccount.com"
                       for n in ("api-runtime", "worker-runtime", "catalog-capture")}
    assert {b["role"] for b in data["policies"]["secret:SENTRY_DSN"]["bindings"]} == \
        {"roles/secretmanager.secretAccessor"}


def test_setup_sentry_stores_the_dsn_once_and_is_idempotent(env):
    assert env.run(SETUP_BACKUP, "--pg-major", "17").returncode == 0
    dsn_file = _dsn_file(env)
    first = env.run(SETUP_SENTRY, "--dsn-file", str(dsn_file))
    assert first.returncode == 0, first.stdout
    _only_pass_fail(first)
    _no_secret_anywhere(env, first)
    data = env.data()
    assert data["secrets"]["SENTRY_DSN"] == [DSN]
    assert data["environments"]["production-backup"]["secrets"]["SENTRY_DSN"] == DSN
    count = len(data["mutations"])
    second = env.run(SETUP_SENTRY, "--dsn-file", str(dsn_file))
    assert second.returncode == 0
    new = [m for m in env.data()["mutations"][count:] if m[0] != "secret-set"]
    assert new == []
    assert env.data()["secrets"]["SENTRY_DSN"] == [DSN]


def test_setup_sentry_refuses_a_malformed_or_readable_dsn_file(env):
    path = env.home / ".milo_sentry_dsn"
    path.write_text("not a dsn")
    path.chmod(0o600)
    bad = env.run(SETUP_SENTRY, "--dsn-file", str(path), "--no-github")
    assert bad.returncode == 1 and "does not hold a Sentry DSN" in bad.stdout
    path.write_text(DSN)
    path.chmod(0o644)
    readable = env.run(SETUP_SENTRY, "--dsn-file", str(path), "--no-github")
    assert readable.returncode == 1 and "not mode 600" in readable.stdout
    assert env.data()["secrets"]["SENTRY_DSN"] == []


@pytest.mark.parametrize("script", [SETUP_BACKUP, SETUP_SENTRY])
def test_a_missing_operator_configuration_is_a_fail_line(env, script):
    env.config.unlink()
    result = env.run(script)
    assert result.returncode == 2
    assert result.stdout.startswith("FAIL the operator configuration could not be loaded")
    assert env.mutations() == []


def test_setup_sentry_disable_stores_disabled_and_keeps_the_binding_valid(env):
    dsn_file = _dsn_file(env)
    assert env.run(SETUP_SENTRY, "--dsn-file", str(dsn_file), "--no-github").returncode == 0
    off = env.run(SETUP_SENTRY, "--disable", "--no-github")
    assert off.returncode == 0, off.stdout
    _only_pass_fail(off)
    assert env.data()["secrets"]["SENTRY_DSN"] == [DSN, "disabled"]
    again = env.run(SETUP_SENTRY, "--disable", "--no-github")
    assert again.returncode == 0
    assert env.data()["secrets"]["SENTRY_DSN"] == [DSN, "disabled"]



def test_setup_backup_reports_drift_it_must_not_fix_silently(env):
    assert env.run(SETUP_BACKUP, "--pg-major", "17").returncode == 0
    data = env.data()
    bucket = data["buckets"][f"gs://{PROJECT}-milo-supabase-backups"]
    bucket["location"] = "EU"
    bucket["retention_policy"]["isLocked"] = True
    env.state.write_text(json.dumps(data))
    count = len(data["mutations"])
    result = env.run(SETUP_BACKUP, "--pg-major", "17")
    assert result.returncode == 1
    assert "(location retention-locked)" in result.stdout
    # A wrong location or a locked policy is never "fixed" by recreating anything.
    assert not [m for m in env.data()["mutations"][count:] if m[0].endswith("-bucket")]


def test_setup_backup_refuses_until_setup_wif_admits_production_backup(env):
    data = env.data()
    data["providers"]["github-actions"]["condition"] = OLD_PROVIDER_CONDITION
    env.state.write_text(json.dumps(data))
    result = env.run(SETUP_BACKUP, "--pg-major", "17")
    assert result.returncode == 1
    assert ("FAIL provider github-actions does not admit production-backup: "
            "run scripts/ops/setup-wif.sh --apply first") in result.stdout
    # It never changes the provider itself.
    assert env.data()["providers"]["github-actions"]["condition"] == OLD_PROVIDER_CONDITION
    assert not [m for m in env.mutations() if m[0] in ("create-oidc", "update-oidc", "create-pool")]


@pytest.mark.parametrize("condition", [
    PROVIDER_CONDITION + " || true",
    OLD_PROVIDER_CONDITION + " || assertion.environment in ['production-backup']",
    # repository or ref clause loosened: refused even though the env is listed
    "assertion.ref == 'refs/heads/main' && assertion.environment in ['production-backup']",
    f"assertion.repository == '{REPOSITORY}' && assertion.environment in ['production-backup']",
    f"assertion.repository == '{REPOSITORY}' && assertion.ref == 'refs/heads/dev' && "
    "assertion.environment in ['production-backup']",
])
def test_setup_backup_refuses_a_provider_that_does_not_pin_repository_and_main(env, condition):
    data = env.data()
    data["providers"]["github-actions"]["condition"] = condition
    env.state.write_text(json.dumps(data))
    result = env.run(SETUP_BACKUP, "--check", "--pg-major", "17")
    assert result.returncode == 1
    assert "FAIL provider github-actions does not admit production-backup" in result.stdout



def test_setup_backup_fails_when_someone_else_can_impersonate_a_backup_identity(env):
    assert env.run(SETUP_BACKUP, "--pg-major", "17").returncode == 0
    data = env.data()
    key = f"sa:milo-backup-writer@{PROJECT}.iam.gserviceaccount.com"
    data["policies"][key]["bindings"].append({
        "role": "roles/iam.workloadIdentityUser",
        "members": ["principalSet://iam.googleapis.com/projects/123456789/locations/global/"
                    f"workloadIdentityPools/milo-github/attribute.repository/{REPOSITORY}"]})
    env.state.write_text(json.dumps(data))
    result = env.run(SETUP_BACKUP, "--check", "--pg-major", "17")
    assert result.returncode == 1
    assert "FAIL milo-backup-writer: its own IAM policy grants more than workloadIdentityUser" in result.stdout



def _deployer_members(env, members, *, condition=None):
    data = env.data()
    binding = {"role": "roles/iam.workloadIdentityUser", "members": members}
    if condition:
        binding["condition"] = condition
    data["policies"][DEPLOYER_KEY] = {"bindings": [binding]}
    env.state.write_text(json.dumps(data))


def test_setup_backup_refuses_while_the_deployer_holds_the_repository_wide_binding(env):
    _deployer_members(env, NARROWED + [f"{POOL}/attribute.repository/{REPOSITORY}"])
    for args in ((), ("--check",)):
        result = env.run(SETUP_BACKUP, *args, "--pg-major", "17")
        assert result.returncode == 1
        assert ("FAIL milo-github-deployer still holds workloadIdentityUser for the repository-wide "
                "principalSet: run scripts/ops/setup-wif.sh --apply first") in result.stdout
        assert env.mutations() == [], "nothing may change before the deployer is narrowed"


@pytest.mark.parametrize("members, condition", [
    ([f"{POOL}/*"], None),                                             # pool-wide
    (NARROWED + [f"{POOL}/attribute.environment/production-backup"], None),
    (NARROWED[:1], None),                                              # only one of the two
    ([], None),                                                        # none at all
    (NARROWED, {"title": "expires", "expression": "request.time < timestamp('2030-01-01T00:00:00Z')"}),
])
def test_setup_backup_refuses_unless_the_deployer_has_exactly_the_two_environments(env, members, condition):
    _deployer_members(env, members, condition=condition)
    result = env.run(SETUP_BACKUP, "--check", "--pg-major", "17")
    assert result.returncode == 1
    assert "FAIL milo-github-deployer workloadIdentityUser members are not exactly the production and " \
           "production-kill-switch environment principalSets" in result.stdout
    assert env.mutations() == []


def test_setup_backup_proceeds_once_the_deployer_is_narrowed(env):
    result = env.run(SETUP_BACKUP, "--pg-major", "17")
    assert result.returncode == 0, result.stdout
    assert ("PASS milo-github-deployer is impersonable only from the production and "
            "production-kill-switch environments") in result.stdout


def test_setup_backup_refuses_when_the_deployer_policy_cannot_be_read(env):
    data = env.data()
    data["unreadable_policies"] = [DEPLOYER_KEY]
    env.state.write_text(json.dumps(data))
    result = env.run(SETUP_BACKUP, "--pg-major", "17")
    assert result.returncode == 1
    assert "FAIL the IAM policy of milo-github-deployer could not be read" in result.stdout
    assert env.mutations() == []

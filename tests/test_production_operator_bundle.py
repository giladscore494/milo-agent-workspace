"""Executable safety proofs for the production activation operator bundle.

These tests exist because the bundle's whole value is that an operator can run
four commands without re-deriving the contract each time. That is only safe if
the contract the scripts encode cannot drift from the code that enforces it,
and if the mutating paths cannot be reached by accident.

Nothing here contacts GCP, data.gov.il or any database. The scripts are read
as text and executed only in modes that are documented as read-only, with a
fabricated operator configuration pointing at obviously non-production names.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CONTRACT = REPO / "scripts/deploy/deployment-contract.sh"
CAPTURE_SCRIPT = REPO / "scripts/catalog/government-production-capture.sh"
PREFLIGHT = REPO / "scripts/deploy/production-preflight.sh"
VERIFY = REPO / "scripts/deploy/production-verify.sh"
ACTIVATE = REPO / "scripts/deploy/production-activate.sh"
BOOTSTRAP = REPO / "scripts/deploy/gcp-bootstrap.sh"
MANIFEST = REPO / "scripts/release/runtime_policy_manifest.py"

BUNDLE_SCRIPTS = [CAPTURE_SCRIPT, PREFLIGHT, VERIFY, ACTIVATE, BOOTSTRAP]


def _contract_value(name: str) -> str:
    """Read a scalar assignment out of the sourced contract."""
    text = CONTRACT.read_text(encoding="utf-8")
    match = re.search(rf'^{re.escape(name)}="([^"]*)"$', text, re.M)
    assert match, f"{name} is not declared in deployment-contract.sh"
    return match.group(1)


# ---------------------------------------------------------------------------
# The contract cannot drift from the code it mirrors
# ---------------------------------------------------------------------------

def test_capture_pins_equal_the_values_the_code_enforces():
    """operator_capture.py refuses any package, resource or bound but these.

    If the contract and the code ever disagree, the capture job would be built
    with arguments the entrypoint rejects (CAPTURE_RESOURCE_NOT_SUPPORTED /
    CAPTURE_BOUNDS_NOT_SUPPORTED) and the operator would learn about it only
    after a Cloud Run execution failed.
    """
    from backend.catalog import operator_capture as capture
    from backend.catalog.government import source

    assert _contract_value("MILO_CAPTURE_PACKAGE_ID") == source.CKAN_PACKAGE_ID
    assert _contract_value("MILO_CAPTURE_RESOURCE_ID") == source.WLTP_RESOURCE_ID
    assert _contract_value("MILO_CAPTURE_PAGE_LIMIT") == str(capture.CAPTURE_PAGE_LIMIT)
    assert _contract_value("MILO_CAPTURE_MAX_PAGES") == str(capture.CAPTURE_MAX_PAGES)
    assert _contract_value("MILO_CAPTURE_MAX_RECORDS") == str(capture.CAPTURE_MAX_RECORDS)


def test_capture_acknowledgements_match_exactly():
    """Both acknowledgements are matched by equality, so a typo is a refusal."""
    from backend.catalog import operator_capture as capture

    assert _contract_value("MILO_CAPTURE_EGRESS_ACK") == capture.EGRESS_ACKNOWLEDGEMENT
    assert _contract_value("MILO_CAPTURE_SCHEMA_ACK") == capture.SCHEMA_REPORT_ACKNOWLEDGEMENT


def test_capture_entrypoint_module_is_importable_and_is_the_real_one():
    """The job runs `python -m <module>`; a wrong name is a crash at runtime."""
    module = _contract_value("MILO_CAPTURE_ENTRYPOINT_MODULE")
    assert module == "backend.catalog.operator_capture"
    __import__(module)


def test_capture_master_flag_name_matches_code2():
    from backend.catalog.execution import CATALOG_EXECUTION_FLAG

    assert _contract_value("MILO_CAPTURE_MASTER_FLAG_NAME") == CATALOG_EXECUTION_FLAG


# ---------------------------------------------------------------------------
# Posture the bundle must never be able to weaken
# ---------------------------------------------------------------------------

def test_repository_never_commits_the_catalog_switch_as_enabled():
    """The enabled VALUE is the operator's to supply, never the repo's.

    scripts/check_unsafe_defaults.py enforces this across tracked files; this
    test pins the specific reason the bundle takes the name-only detour, so a
    future edit that "simplifies" it by inlining `=true` fails here with an
    explanation rather than only in a scanner.
    """
    text = CONTRACT.read_text(encoding="utf-8") + CAPTURE_SCRIPT.read_text(encoding="utf-8")
    assert not re.search(r"MILO_ENABLE_CATALOG_EXECUTION\s*=\s*['\"]?(1|true|yes|on)\b",
                         text, re.I), (
        "the catalog master switch must never be committed as enabled; the "
        "capture script takes it from --enable-catalog-execution instead")


def test_capture_job_never_binds_a_provider_credential():
    """A capture spends no model money, so it must not reach a provider key."""
    from backend.catalog import operator_capture as capture  # noqa: F401

    script = CAPTURE_SCRIPT.read_text(encoding="utf-8")
    secret_builder = script.split("build_secret_args()", 1)[1].split("}", 1)[0]
    for forbidden in ("KIMI_API_KEY", "MOONSHOT_API_KEY", "PROVIDER_API_KEY"):
        assert forbidden not in secret_builder, (
            f"{forbidden} must never be bound to the capture job")


def test_capture_pins_paid_execution_and_promotion_off():
    text = CONTRACT.read_text(encoding="utf-8")
    pinned = text.split("MILO_CAPTURE_PINNED_OFF_FLAGS=(", 1)[1].split(")", 1)[0]
    for flag in ("MILO_ENABLE_PAID_EXECUTION=false",
                 "MILO_ENABLE_CATALOG_PROMOTION=false",
                 "MILO_ENABLE_GOVERNMENT_CATALOG_READ=false",
                 "MILO_ENABLE_RUN_CREATION=false"):
        assert flag in pinned, f"{flag} must be pinned by the capture job definition"


def test_mutating_capture_modes_refuse_without_the_explicit_operator_flag(tmp_path):
    """--ensure-job / --prepare / --capture / --all all require the flag.

    This is the property that makes enabling the catalog a deliberate act: the
    operator's own command line is the record of the decision.
    """
    config = _fake_config(tmp_path)
    for mode in ("--ensure-job", "--prepare", "--capture", "--all"):
        result = subprocess.run(
            ["bash", str(CAPTURE_SCRIPT), mode, "--operator-config", str(config)],
            capture_output=True, text=True, check=False, cwd=REPO)
        assert result.returncode != 0, f"{mode} must refuse without the flag"
        assert "--enable-catalog-execution is required" in result.stderr, result.stderr


# ---------------------------------------------------------------------------
# Read-only modes really are read-only
# ---------------------------------------------------------------------------

def _fake_config(tmp_path: Path) -> Path:
    """An operator config that is obviously not production."""
    config = tmp_path / "operator.env"
    config.write_text(
        "GCP_PROJECT_ID=test-project-not-production\n"
        "GCP_REGION=test-region\n"
        "GCP_PROJECT_NUMBER=000000000000\n"
        "ARTIFACT_REGISTRY_REPOSITORY=test-repo\n"
        "CLOUD_RUN_API_SERVICE=test-api\n"
        "CLOUD_RUN_WORKER_JOB=test-worker\n"
        "CLOUD_RUN_CAPTURE_JOB=test-capture\n"
        "API_SERVICE_ACCOUNT=api@test.iam.gserviceaccount.com\n"
        "WORKER_SERVICE_ACCOUNT=worker@test.iam.gserviceaccount.com\n"
        "CAPTURE_SERVICE_ACCOUNT=capture@test.iam.gserviceaccount.com\n"
        "GATEWAY_SERVICE_ACCOUNT=gateway@test.iam.gserviceaccount.com\n"
        "SUPABASE_PROJECT_REF=testprojectref\n"
        "SECRET_SUPABASE_URL=TEST_SUPABASE_URL\n"
        "SECRET_SUPABASE_SERVICE_KEY=TEST_SUPABASE_SECRET_KEY\n"
        "SECRET_REDIS_URL=TEST_REDIS_URL\n"
        "SECRET_REDIS_TOKEN=TEST_REDIS_TOKEN\n"
        "SECRET_PROVIDER_API_KEY=TEST_PROVIDER_KEY\n"
        "PRODUCTION_ORIGIN=https://example.test\n"
        "MILO_GATEWAY_AUDIENCE=https://example.test\n"
        "MILO_APPROVED_GATEWAY_IDENTITIES=gateway@test.iam.gserviceaccount.com\n"
        "CAPTURE_CONVERSATION_ID=00000000-0000-4000-8000-000000000001\n"
        "CAPTURE_REQUESTED_BY=00000000-0000-4000-8000-000000000002\n"
        "CAPTURE_IDEMPOTENCY_KEY=test-capture-0001\n"
        "READONLY_DATABASE_URL_ENV=MILO_TEST_DB_URL_ABSENT\n",
        encoding="utf-8")
    return config


def _no_gcloud_env() -> dict[str, str]:
    """A PATH with no gcloud, so a read-only mode cannot reach a cloud at all."""
    env = dict(os.environ)
    env["PATH"] = "/usr/bin:/bin"
    env["MILO_OPERATOR_CONFIG"] = ""
    return env


def test_capture_plan_mode_performs_no_cloud_call(tmp_path):
    """--plan is the default and must not need, or use, gcloud."""
    config = _fake_config(tmp_path)
    result = subprocess.run(
        ["bash", str(CAPTURE_SCRIPT), "--plan", "--operator-config", str(config)],
        capture_output=True, text=True, check=False, cwd=REPO, env=_no_gcloud_env())
    assert result.returncode == 0, result.stderr
    assert "PLAN" in result.stdout
    assert "Nothing below has been executed" in result.stdout
    # The plan states the real pinned upstream identity.
    assert _contract_value("MILO_CAPTURE_RESOURCE_ID") in result.stdout
    # And never a provider credential.
    assert "KIMI_API_KEY" not in result.stdout


def test_preflight_offline_mode_makes_no_remote_call(tmp_path):
    config = _fake_config(tmp_path)
    result = subprocess.run(
        ["bash", str(PREFLIGHT), "--offline", "--operator-config", str(config)],
        capture_output=True, text=True, check=False, cwd=REPO, env=_no_gcloud_env())
    # Offline preflight may still report BLOCKED findings (a dirty worktree in
    # a developer checkout, for instance). What it must never do is claim it
    # checked the live project.
    assert "--offline: every live check skipped" in result.stdout
    assert "gcloud:context" not in result.stdout


def test_every_bundle_script_refuses_an_unknown_argument(tmp_path):
    """A mistyped flag must never be silently ignored by a mutating tool."""
    config = _fake_config(tmp_path)
    for script in BUNDLE_SCRIPTS:
        result = subprocess.run(
            ["bash", str(script), "--definitely-not-a-flag",
             "--operator-config", str(config)],
            capture_output=True, text=True, check=False, cwd=REPO, env=_no_gcloud_env())
        assert result.returncode != 0, f"{script.name} accepted an unknown argument"


def test_bootstrap_defaults_to_plan_and_never_deletes():
    """--plan is the default, and no path in the script deletes or replaces."""
    text = BOOTSTRAP.read_text(encoding="utf-8")
    assert 'MODE="plan"' in text
    for destructive in ("gcloud secrets delete", "gcloud iam service-accounts delete",
                        "gcloud run services delete", "gcloud run jobs delete",
                        "remove-iam-policy-binding", "--set-iam-policy"):
        assert destructive not in text, f"bootstrap must never run: {destructive}"


def test_activate_requires_an_explicit_step():
    result = subprocess.run(["bash", str(ACTIVATE)],
                            capture_output=True, text=True, check=False, cwd=REPO)
    assert result.returncode != 0
    assert "choose at least one step" in result.stderr


def test_activate_capture_requires_the_catalog_flag():
    result = subprocess.run(["bash", str(ACTIVATE), "--capture"],
                            capture_output=True, text=True, check=False, cwd=REPO)
    assert result.returncode != 0
    assert "--capture requires --enable-catalog-execution" in result.stderr


# ---------------------------------------------------------------------------
# The RuntimePolicy manifest is derived, not maintained
# ---------------------------------------------------------------------------

def test_manifest_lists_every_policy_dimension():
    from backend.runtime_policy import POLICY_DIMENSIONS

    result = subprocess.run(
        ["python3", str(MANIFEST), "--format", "json"],
        capture_output=True, text=True, check=False, cwd=REPO)
    assert result.returncode == 0, result.stderr
    import json
    document = json.loads(result.stdout)
    listed = {row["dimension"] for row in document["dimensions"]}
    assert listed == {d.name for d in POLICY_DIMENSIONS}


def test_manifest_marks_exactly_the_mandatory_set_as_mandatory():
    from backend.runtime_policy import MANDATORY_FOR_PAID_EXECUTION

    import json
    result = subprocess.run(["python3", str(MANIFEST), "--format", "json"],
                            capture_output=True, text=True, check=False, cwd=REPO)
    document = json.loads(result.stdout)
    mandatory = {row["dimension"] for row in document["dimensions"]
                 if row["required"] == "MANDATORY"}
    assert mandatory == set(MANDATORY_FOR_PAID_EXECUTION)


def test_manifest_without_live_read_claims_nothing_about_live_values():
    """A declared-only run must not report PASS for a binding it never saw."""
    import json
    result = subprocess.run(["python3", str(MANIFEST), "--format", "json"],
                            capture_output=True, text=True, check=False, cwd=REPO)
    document = json.loads(result.stdout)
    assert document["surface"].startswith("not read")
    assert {row["status"] for row in document["dimensions"]} == {"UNKNOWN"}


def test_manifest_live_read_requires_its_target():
    result = subprocess.run(
        ["python3", str(MANIFEST), "--live-from-cloud-run"],
        capture_output=True, text=True, check=False, cwd=REPO)
    assert result.returncode == 2
    assert "--project" in result.stderr


def test_manifest_never_prints_a_secret_backed_value():
    """Secret-backed variables are recorded as bound, never as their value."""
    text = MANIFEST.read_text(encoding="utf-8")
    assert '"<secret-backed>"' in text
    assert "secretmanager" not in text.lower() or "access" not in text.lower(), (
        "the manifest must never access a secret version")


@pytest.mark.parametrize("script", BUNDLE_SCRIPTS)
def test_bundle_scripts_are_executable_and_parse(script):
    assert os.access(script, os.X_OK), f"{script.name} is not executable"
    result = subprocess.run(["bash", "-n", str(script)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr

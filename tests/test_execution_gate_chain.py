"""The gate chain must describe the gates the code actually enforces.

The whole value of `scripts/release/execution_gate_chain.py` is that an
operator can read it instead of the source and not be surprised tomorrow. That
is only true if every flag name in it is a name some layer really reads, and if
the two gates that are easy to miss stay called out. A stale entry here is
worse than no document, because it would be trusted.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CHAIN = REPO / "scripts/release/execution_gate_chain.py"
CHECK = REPO / "scripts/deploy/website-execution-check.sh"
ACTIVATE = REPO / "scripts/deploy/website-execution-activate.sh"


def _import_chain():
    import sys
    directory = str(CHAIN.parent)
    if directory not in sys.path:
        sys.path.insert(0, directory)
    import execution_gate_chain  # type: ignore
    return execution_gate_chain


def test_every_backend_execution_flag_appears_in_the_chain():
    """A flag the backend gates on that the chain omits is a trap."""
    from backend.production_config import EXECUTION_FLAGS

    chain_text = CHAIN.read_text(encoding="utf-8")
    missing = [flag for flag in EXECUTION_FLAGS if flag not in chain_text]
    assert not missing, f"execution flags missing from the gate chain: {missing}"


def test_frontend_gate_names_match_what_the_frontend_reads():
    api = (REPO / "frontend/lib/api.ts").read_text(encoding="utf-8")
    policy = (REPO / "frontend/lib/server/gatewayPolicy.ts").read_text(encoding="utf-8")
    assert "NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI" in api
    assert "GATEWAY_ALLOW_EXECUTION_ROUTES" in policy
    assert "GATEWAY_ALLOW_RUN_START_ROUTES" in policy

    chain_text = CHAIN.read_text(encoding="utf-8")
    assert "NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI" in chain_text
    assert "GATEWAY_ALLOW_EXECUTION_ROUTES" in chain_text
    assert "GATEWAY_ALLOW_RUN_START_ROUTES" in chain_text


def test_job_launcher_is_documented_because_it_is_the_silent_one():
    """JOB_LAUNCHER is not an MILO_ENABLE_* flag and is not in EXECUTION_FLAGS.

    Left at its default the run row is created and displayed while nothing
    executes it, which looks like a product bug rather than a configuration
    gap. It must stay in the chain with that behaviour spelled out.
    """
    from backend.config import Settings

    assert Settings.model_fields["job_launcher"].default == "disabled"
    module = _import_chain()
    launcher = [g for g in module.GATE_CHAIN if g.name == "JOB_LAUNCHER"]
    assert launcher, "JOB_LAUNCHER must be in the gate chain"
    assert "cloud_run" in launcher[0].required_for_first_run
    assert "never" in launcher[0].failure_behavior_when_off.lower()


def test_execution_control_records_its_two_required_companions():
    """Enabling EXECUTION_CONTROL without them takes the API DOWN, not open."""
    config = (REPO / "backend/production_config.py").read_text(encoding="utf-8")
    assert "WORKER_AUTH_AUDIENCE_MISSING" in config
    assert "WORKER_ALLOWLIST_EMPTY" in config

    module = _import_chain()
    entry = [g for g in module.GATE_CHAIN
             if "MILO_WORKER_AUDIENCE" in g.name and "MILO_APPROVED_WORKER_IDENTITIES" in g.name]
    assert entry, "the worker-identity prerequisite must be its own gate entry"
    assert "FAILS TO START" in entry[0].failure_behavior_when_off


def test_execution_control_gates_the_worker_callback_routes():
    """The flag is not just an API surface: it gates /internal/runs/... too."""
    guard = (REPO / "backend/execution_guard.py").read_text(encoding="utf-8")
    assert re.search(r"MILO_ENABLE_EXECUTION_CONTROL.*internal/runs", guard, re.S)


def test_promotion_is_marked_not_required_and_stays_off():
    module = _import_chain()
    promotion = [g for g in module.GATE_CHAIN
                 if g.name == "MILO_ENABLE_CATALOG_PROMOTION"]
    assert promotion
    assert promotion[0].required_for_first_run.startswith("NO")


def test_ui_flag_is_marked_build_time_and_gateway_flag_is_not():
    module = _import_chain()
    by_name = {g.name: g for g in module.GATE_CHAIN}
    ui = by_name["NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI"]
    gateway = by_name["GATEWAY_ALLOW_EXECUTION_ROUTES"]
    assert "REBUILD" in ui.requires_redeploy, (
        "the UI flag is inlined at build time; saying otherwise would send an "
        "operator to change an env var that cannot affect the served bundle")
    assert "REBUILD" not in gateway.requires_redeploy


def test_chain_renders_in_both_formats():
    for fmt in ("text", "json"):
        result = subprocess.run(["python3", str(CHAIN), "--format", fmt],
                                capture_output=True, text=True, check=False, cwd=REPO)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip()


# ---------------------------------------------------------------------------
# The website scripts
# ---------------------------------------------------------------------------

def test_check_script_never_sends_the_run_creation_post():
    """Proving the gate by creating a run would create a run."""
    text = CHECK.read_text(encoding="utf-8")
    assert "-X POST" not in text and "--request POST" not in text, (
        "website-execution-check.sh must never issue a POST; the run-creation "
        "gate is proved from configuration, not by exercising it")
    # No curl invocation may carry a request body either.
    for line in text.splitlines():
        if "curl" in line:
            assert not re.search(r"(^|\s)(-d|--data\b|--data-raw|--data-binary)(\s|=)", line), (
                f"curl must not send a body: {line.strip()}")


WEBSITE_FACTS = ("FRONTEND_CODE_WIRED", "FRONTEND_RELEASE", "TASK_COMPOSER_VISIBLE",
                 "GATEWAY_EXECUTION_ENABLED", "GATEWAY_RUN_START_ENABLED", "GATEWAY_BACKEND_BINDING",
                 "BACKEND_EXECUTION_ARMED", "MAPPING_PLAN_BATCH_PATH")


def test_check_script_reports_every_fact_separately():
    text = CHECK.read_text(encoding="utf-8")
    for key in WEBSITE_FACTS:
        assert f"fact {key} " in text or f'"{key}"' in text or f" {key} " in text, (
            f"{key} must be reported separately")


def test_stage_active_requires_every_fact_verified():
    """WEBSITE_EXECUTION_STAGE_ACTIVE=VERIFIED only when every fact is VERIFIED."""
    text = CHECK.read_text(encoding="utf-8")
    final = text.split("Only every fact VERIFIED means the stage is active.", 1)[1]
    loop = final.split("for name in", 1)[1].split("; do", 1)[0]
    for key in WEBSITE_FACTS:
        assert key in loop, f"the stage-active condition must include {key}"
    verified = final.split("WEBSITE_EXECUTION_STAGE_ACTIVE=VERIFIED", 1)[0]
    assert 'if [[ "$ALL_VERIFIED" -eq 1 ]]' in verified
    # There is no YES left to reach by presence alone.
    assert "WEBSITE_EXECUTION_STAGE_ACTIVE=YES" not in text
    assert "CONFIGURED_VALUE_UNVERIFIED" not in text and "LIKELY_YES" not in text


def test_check_script_is_offline_safe_and_read_only(tmp_path):
    config = tmp_path / "operator.env"
    config.write_text(
        "GCP_PROJECT_ID=test-project\nGCP_REGION=test-region\n"
        "CLOUD_RUN_API_SERVICE=test-api\nCLOUD_RUN_WORKER_JOB=test-worker\n"
        "PRODUCTION_ORIGIN=https://example.test\n", encoding="utf-8")
    result = subprocess.run(
        ["bash", str(CHECK), "--offline", "--operator-config", str(config)],
        capture_output=True, text=True, check=False, cwd=REPO)
    assert "FRONTEND_CODE_WIRED=VERIFIED" in result.stdout
    assert "TASK_COMPOSER_VISIBLE=UNVERIFIED" in result.stdout
    assert "WEBSITE_EXECUTION_STAGE_ACTIVE=UNVERIFIED" in result.stdout
    # Offline proves nothing about the deployment, so it never exits 0.
    assert result.returncode != 0


def test_activate_defaults_to_plan_and_never_applies_vercel():
    text = ACTIVATE.read_text(encoding="utf-8")
    assert 'MODE="plan"' in text
    # The Vercel commands are PRINTED inside a heredoc, never executed.
    assert "print_frontend_commands" in text
    body = text.split("print_frontend_commands() {", 1)[1].split("\n}", 1)[0]
    # Every vercel line must sit INSIDE the heredoc that the function cats, so
    # it is operator instruction text rather than a command this script runs.
    assert "cat << EOC" in body, "the frontend commands must be emitted via a heredoc"
    heredoc = body.split("cat << EOC", 1)[1].split("EOC", 1)[0]
    vercel_lines = [ln for ln in body.splitlines() if ln.strip().startswith("vercel ")]
    assert vercel_lines, "the operator still needs the exact Vercel commands"
    for line in vercel_lines:
        assert line in heredoc, (
            "every vercel line must be inside the heredoc, never executed here")


def test_activate_never_enables_promotion():
    contract = (REPO / "scripts/deploy/deployment-contract.sh").read_text(encoding="utf-8")

    def array(name: str) -> str:
        return contract.split(f"{name}=(", 1)[1].split(")", 1)[0]

    for pinned in ("MILO_STAGE2_API_PINNED_OFF_FLAGS", "MILO_STAGE2_WORKER_PINNED_OFF_FLAGS"):
        assert "MILO_ENABLE_CATALOG_PROMOTION" in array(pinned)
    for enabled in ("MILO_STAGE2_API_ENABLE_FLAGS", "MILO_STAGE2_WORKER_ENABLE_FLAGS",
                    "MILO_PLAN_AUTHORING_API_ENABLE_FLAGS"):
        assert "MILO_ENABLE_CATALOG_PROMOTION" not in array(enabled)
    text = ACTIVATE.read_text(encoding="utf-8")
    assert "MILO_STAGE2_WORKER_PINNED_OFF_FLAGS" in text


def test_activate_sets_the_launcher_and_the_worker_identity():
    """The two easy-to-miss gates must be applied, not left to memory."""
    text = ACTIVATE.read_text(encoding="utf-8")
    assert "JOB_LAUNCHER=cloud_run" in text
    assert "MILO_WORKER_AUDIENCE=" in text
    assert "MILO_APPROVED_WORKER_IDENTITIES=" in text


def test_activate_refuses_without_the_worker_audience(tmp_path):
    config = tmp_path / "operator.env"
    config.write_text(
        "GCP_PROJECT_ID=test-project\nGCP_REGION=test-region\n"
        "CLOUD_RUN_API_SERVICE=test-api\nCLOUD_RUN_WORKER_JOB=test-worker\n"
        "SECRET_PROVIDER_API_KEY=TEST_KEY\nMILO_GATEWAY_AUDIENCE=https://example.test\n"
        "MILO_APPROVED_GATEWAY_IDENTITIES=gw@test.iam.gserviceaccount.com\n"
        "PRODUCTION_ORIGIN=https://example.test\n"
        "MILO_WORKER_AUDIENCE=\nMILO_APPROVED_WORKER_IDENTITIES=\n"
        "WORKER_SERVICE_ACCOUNT=\n", encoding="utf-8")
    result = subprocess.run(
        ["bash", str(ACTIVATE), "--plan", "--operator-config", str(config)],
        capture_output=True, text=True, check=False, cwd=REPO)
    assert result.returncode != 0
    assert "MILO_WORKER_AUDIENCE" in result.stderr


def test_repository_commits_no_enabled_execution_flag_in_the_activation_script():
    """The activation script sets flags true at RUNTIME, never as a literal."""
    text = ACTIVATE.read_text(encoding="utf-8")
    for flag in ("MILO_ENABLE_RUN_CREATION", "MILO_ENABLE_PAID_EXECUTION",
                 "MILO_ENABLE_CATALOG_EXECUTION", "MILO_ENABLE_GOVERNMENT_CATALOG_READ",
                 "MILO_ENABLE_EXECUTION_CONTROL"):
        assert not re.search(rf"{flag}\s*=\s*['\"]?(1|true|yes|on)\b", text, re.I), (
            f"{flag} must not be committed as enabled; it is assembled at runtime")

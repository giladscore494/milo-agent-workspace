"""Contract tests for the REAL pinned Vercel CLI.

These tests exercise the actual `vercel` binary (installed in CI at the exact
version recorded in scripts/release/VERCEL_CLI_VERSION) and prove that every
subcommand the release tooling depends on exists:

  * `vercel env ls / add / pull / remove` (baseline environment management);
  * `vercel env update --yes` (idempotent in-place update, value on stdin);
  * `vercel env run --environment` (production-only in-memory verification).

Only `--version` and `--help` are ever executed: no authentication, no network
mutation and — critically — NO DEPLOY of any kind. When the CLI is not on
PATH the real-CLI tests skip, unless MILO_REQUIRE_VERCEL_CLI_CONTRACT=1 (set
in CI after installing the pin), which turns a missing CLI into a failure so
the contract can never be silently skipped where it must hold.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PIN_FILE = REPO / "scripts" / "release" / "VERCEL_CLI_VERSION"
NUMERIC_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
REQUIRED_ENV_SUBCOMMANDS = ("add", "list", "pull", "remove", "run", "update")

REQUIRE = os.environ.get("MILO_REQUIRE_VERCEL_CLI_CONTRACT") == "1"


def _cli() -> str | None:
    return os.environ.get("MILO_VERCEL_CLI") or shutil.which("vercel")


def _run(*args: str) -> str:
    """Run the real CLI with a help/version-only argv and return its output."""
    for arg in args:
        assert not any(tok in arg for tok in ("deploy", "promote", "redeploy")), \
            f"contract tests must never invoke a deploy-like command: {arg}"
    cli = _cli()
    assert cli, "vercel CLI missing"
    result = subprocess.run(
        [cli, *args], capture_output=True, text=True, timeout=120,
        env={**os.environ, "CI": "1", "VERCEL_TELEMETRY_DISABLED": "1"},
    )
    return result.stdout + result.stderr


def _need_cli():
    if _cli() is None:
        if REQUIRE:
            pytest.fail(
                "MILO_REQUIRE_VERCEL_CLI_CONTRACT=1 but no `vercel` CLI is on "
                "PATH; install the pinned version from scripts/release/VERCEL_CLI_VERSION"
            )
        pytest.skip("vercel CLI not installed; real-CLI contract checks skipped")


def _pin() -> str:
    return PIN_FILE.read_text(encoding="utf-8").strip()


# ---------------------------------------------------------------------------
# Pin hygiene (always runs, no CLI needed)
# ---------------------------------------------------------------------------

def test_pin_file_is_exact_numeric_version():
    pin = _pin()
    assert NUMERIC_VERSION_RE.match(pin), (
        f"scripts/release/VERCEL_CLI_VERSION must contain an exact numeric "
        f"x.y.z release (never 'latest'/'canary'), got {pin!r}"
    )


def test_workflows_install_from_the_pin_file():
    # Both the bootstrap workflow and CI must install the CLI from the single
    # pinned source of truth — never a hardcoded or floating version.
    for wf in ("bootstrap-production.yml", "ci.yml"):
        text = (REPO / ".github" / "workflows" / wf).read_text(encoding="utf-8")
        assert "scripts/release/VERCEL_CLI_VERSION" in text, (
            f"{wf} must install the Vercel CLI from scripts/release/VERCEL_CLI_VERSION"
        )
        assert "vercel@latest" not in text


# ---------------------------------------------------------------------------
# Real-CLI contract (skips without the CLI; required in CI)
# ---------------------------------------------------------------------------

def test_cli_version_is_numeric_and_matches_pin():
    _need_cli()
    out = _run("--version")
    versions = [ln.strip() for ln in out.splitlines() if NUMERIC_VERSION_RE.match(ln.strip())]
    assert versions, f"`vercel --version` did not report an exact numeric version:\n{out}"
    if REQUIRE:
        assert versions[0] == _pin(), (
            f"installed CLI {versions[0]} does not equal the pinned {_pin()}"
        )


def test_env_subcommands_exist():
    _need_cli()
    out = _run("env", "--help")
    for sub in REQUIRED_ENV_SUBCOMMANDS:
        assert re.search(rf"^\s*{sub}\s", out, re.MULTILINE), (
            f"`vercel env` does not list the required `{sub}` subcommand:\n{out}"
        )


def test_env_run_supports_environment_flag():
    _need_cli()
    out = _run("env", "run", "--help")
    assert "--environment" in out, (
        f"`vercel env run` must support --environment for production-only "
        f"verification:\n{out}"
    )


def test_env_update_supports_non_interactive_yes():
    _need_cli()
    out = _run("env", "update", "--help")
    assert "--yes" in out, (
        f"`vercel env update` must support --yes for non-interactive in-place "
        f"updates:\n{out}"
    )


def test_shared_cli_contract_helper_accepts_the_real_cli():
    """vercel_cli_contract (lib/common.sh) — the exact preflight the bootstrap
    runs before any mutation — must return OK|<numeric> for the real CLI."""
    _need_cli()
    script = (
        f'source "{REPO}/scripts/release/lib/common.sh"; vercel_cli_contract'
    )
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=180,
        env={**os.environ, "CI": "1", "VERCEL_TELEMETRY_DISABLED": "1"},
    )
    out = result.stdout.strip()
    assert out.startswith("OK|"), f"vercel_cli_contract rejected the real CLI: {out}"
    assert NUMERIC_VERSION_RE.match(out.split("|", 1)[1])

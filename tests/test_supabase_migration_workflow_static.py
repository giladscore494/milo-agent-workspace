"""The production migration workflow's manual authorization gate.

Two kinds of coverage live here, in the same dependency-free style as
tests/test_supabase_backup_workflow_static.py:

  * STATIC assertions over the workflow text -- that the gate exists, that it
    runs before anything that can touch production, and that the apply step's
    own condition repeats the SHA equality;
  * EXECUTABLE assertions -- the gate's shell script is extracted from the
    workflow and actually run under bash with the environment GitHub Actions
    would hand it, so "a wrong SHA fails closed" is demonstrated rather than
    asserted about a string.

The executable half is what makes this a regression test: a future edit that
keeps the words but breaks the logic (an `||` for an `&&`, a dropped anchor in
the hex pattern, a `-n` where an equality was meant) still fails here.

Nothing in this module contacts GitHub, Supabase or any network service, and
no real SHA, secret or project reference appears in it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

WORKFLOW = Path(".github/workflows/deploy-supabase-migrations.yml")

GATE_STEP = "Validate manual authorization against the reviewed SHA"
LINK_STEP = "Link production Supabase project"
APPLY_STEP = "Apply production migrations"
SECRETS_STEP = "Verify required Supabase secrets are configured"
CLI_STEP = "Set up Supabase CLI"

# Obviously synthetic: 40 hex characters that are not any real commit.
AUTHORIZED_SHA = "a" * 40
OTHER_SHA = "b" * 40
CONFIRMATION = "APPLY_PRODUCTION_MIGRATIONS"


def workflow_text() -> str:
    return WORKFLOW.read_text()


def job_header() -> str:
    """Everything from the job declaration down to its first step.

    This is exactly the region a job-level `env:` lives in. GitHub materialises
    a job-level `env:` into the environment of EVERY step, so a credential
    declared here is present before the authorization gate runs.
    """
    text = workflow_text()
    return text.split("  deploy-supabase-migrations:", 1)[1].split("    steps:", 1)[0]


def step_block(name: str) -> str:
    """The YAML block of one step, from its `- name:` to the next step's."""
    steps = workflow_text().split("    steps:", 1)[1]
    start = steps.index(f"- name: {name}")
    rest = steps[start + 1 :]
    end = rest.find("\n      - name: ")
    return steps[start:] if end == -1 else steps[start : start + 1 + end]


def secret_names(block: str) -> set[str]:
    """Names bound to a `secrets.*` expression inside a block."""
    return {
        line.split(":", 1)[0].strip()
        for line in block.splitlines()
        if "${{ secrets." in line and ":" in line
    }


# ---------------------------------------------------------------------------
# static: the gate exists, and it runs before anything can reach production
# ---------------------------------------------------------------------------
def test_workflow_dispatch_requires_an_expected_sha_input():
    text = workflow_text()
    dispatch = text.split("workflow_dispatch:", 1)[1].split("permissions:", 1)[0]

    assert "expected_sha:" in dispatch
    # Required for BOTH modes, so the reviewed commit is always explicit.
    expected = dispatch.split("expected_sha:", 1)[1].split("confirmation:", 1)[0]
    assert "required: true" in expected
    assert "type: string" in expected


def test_authorization_gate_runs_before_any_production_capable_step():
    text = workflow_text()

    gate = text.index(GATE_STEP)
    # Every step that reads a production secret, installs the CLI, links the
    # project or pushes must come after the gate.
    for later in (CLI_STEP, SECRETS_STEP, LINK_STEP, APPLY_STEP):
        assert gate < text.index(later), f"{later} must run after the authorization gate"

    # And no STEP that invokes the Supabase CLI may precede it. Scoped to the
    # job's steps, so the `on: push: paths:` filter -- which legitimately names
    # supabase/migrations/** -- is not mistaken for a production action.
    steps = text.split("    steps:", 1)[1]
    before_gate = steps[: steps.index(GATE_STEP)]
    # Comments are prose about the gate, not actions: compare executable lines.
    executable = "\n".join(
        line for line in before_gate.splitlines() if not line.lstrip().startswith("#")
    ).lower()
    assert "supabase" not in executable
    assert "secrets." not in executable


def test_gate_fails_closed_rather_than_skipping():
    text = workflow_text()
    gate = text.split(GATE_STEP, 1)[1].split("- name: Set up Python", 1)[0]

    # A step-level `if:` that evaluates false SKIPS the step and the run stays
    # green; refusal has to be a nonzero exit.
    assert gate.count("exit 1") == 3
    assert "^[0-9a-f]{40}$" in gate
    assert 'if [ "${GITHUB_SHA}" != "${EXPECTED_SHA_INPUT}" ]' in gate
    # The gate is scoped to manual dispatch, leaving push behaviour untouched.
    assert "github.event_name == 'workflow_dispatch'" in gate


PRODUCTION_SECRETS = (
    "SUPABASE_ACCESS_TOKEN",
    "SUPABASE_DB_PASSWORD",
    "SUPABASE_PROJECT_ID",
)


def test_no_production_secret_is_declared_at_job_scope():
    """A job-level `env:` reaches every step, including the gate.

    Declaring the Supabase credentials there puts them in front of an
    unauthorized dispatch even when no step reads them -- which is the
    boundary this workflow claims to hold. They belong on individual steps.
    """
    header = job_header()

    assert "secrets." not in header, (
        "no production credential may be declared at job scope; "
        "a job-level env: is materialised into every step's environment"
    )
    for name in PRODUCTION_SECRETS:
        assert f"{name}: ${{{{ secrets.{name} }}}}" not in header

    # The two non-credential values may stay at job scope.
    assert "SUPABASE_MIGRATIONS_AUTO_APPLY" in header
    assert "SAFE_FAILURE_MESSAGE" in header


def test_no_production_secret_appears_anywhere_before_the_gate():
    """The strongest form of the claim, and the cheapest to keep true.

    Because a job-level `env:` is textually above `steps:`, a single ordering
    assertion over the whole file covers both the job block and any step that
    might later be inserted ahead of the gate.
    """
    text = workflow_text()
    gate = text.index(GATE_STEP)

    assert "secrets." not in text[:gate], (
        "a production credential is introduced before the authorization gate"
    )
    # ...and they really are used later, so the assertion above is not vacuous.
    assert "secrets." in text[gate:]


def test_authorization_gate_receives_only_the_three_dispatch_inputs():
    block = step_block(GATE_STEP)
    env = block.split("env:", 1)[1].split("run:", 1)[0]
    bound = {line.split(":", 1)[0].strip() for line in env.splitlines() if ":" in line}

    assert bound == {"EXPECTED_SHA_INPUT", "MODE_INPUT", "CONFIRMATION_INPUT"}
    assert secret_names(block) == set()
    assert "secrets." not in block


@pytest.mark.parametrize(
    ("step", "required"),
    [
        (SECRETS_STEP, {"SUPABASE_ACCESS_TOKEN", "SUPABASE_DB_PASSWORD", "SUPABASE_PROJECT_ID"}),
        (LINK_STEP, {"SUPABASE_ACCESS_TOKEN", "SUPABASE_DB_PASSWORD", "SUPABASE_PROJECT_ID"}),
        ("Display remote migration history", {"SUPABASE_ACCESS_TOKEN", "SUPABASE_DB_PASSWORD"}),
        ("Run mandatory production dry-run preflight", {"SUPABASE_ACCESS_TOKEN", "SUPABASE_DB_PASSWORD"}),
        (APPLY_STEP, {"SUPABASE_ACCESS_TOKEN", "SUPABASE_DB_PASSWORD"}),
        ("Display remote migration history after apply", {"SUPABASE_ACCESS_TOKEN", "SUPABASE_DB_PASSWORD"}),
    ],
)
def test_production_steps_still_receive_the_credentials_they_need(step, required):
    """Scoping the secrets down must not starve the Supabase CLI.

    Each of these steps is a separate process: linking earlier does not carry
    the access token or database password into a later one. Anything a step's
    script names must be in that step's own environment.
    """
    block = step_block(step)
    assert secret_names(block) == required

    # Whatever the script dereferences must actually be bound in the step.
    body = block.split("run:", 1)[1]
    for name in PRODUCTION_SECRETS:
        if f"${name}" in body or f"${{{name}}}" in body:
            assert name in required, f"{step} dereferences {name} without binding it"


def test_dry_run_and_apply_carry_an_identical_credential_environment():
    """So the dry-run is a genuine canary for the apply.

    If this scoping were ever insufficient, the preflight fails first and
    nothing is pushed -- which is only true while the two environments match.
    """
    assert secret_names(step_block("Run mandatory production dry-run preflight")) == secret_names(
        step_block(APPLY_STEP)
    )


@pytest.mark.parametrize(
    "step",
    [
        "Checkout repository",
        GATE_STEP,
        "Set up Python",
        "Validate repository migrations",
        CLI_STEP,
        "Report push dry-run-only bootstrap state",
        "Report manual dry-run completion",
    ],
)
def test_unrelated_steps_receive_no_production_credential(step):
    assert secret_names(step_block(step)) == set()
    assert "secrets." not in step_block(step)


def test_gate_consults_no_repository_or_environment_variable():
    text = workflow_text()
    gate = text.split(GATE_STEP, 1)[1].split("- name: Set up Python", 1)[0]

    # No `vars.*` may relax the manual gate.
    assert "vars." not in gate
    assert "SUPABASE_MIGRATIONS_AUTO_APPLY" not in gate
    # And it never echoes a secret.
    assert "secrets." not in gate


def test_apply_step_condition_repeats_the_sha_equality():
    text = workflow_text()

    apply_conditions = [
        line
        for line in text.splitlines()
        if "if:" in line and "inputs.mode == 'apply'" in line
    ]
    assert apply_conditions, "the apply path must be conditioned on mode == apply"
    for condition in apply_conditions:
        assert "inputs.expected_sha == github.sha" in condition
        assert f"inputs.confirmation == '{CONFIRMATION}'" in condition


def test_automatic_push_apply_remains_default_off():
    text = workflow_text()

    # Auto-apply is opt-in through a variable that must stay unset, and the
    # push path is unchanged by the manual gate.
    assert "vars.SUPABASE_MIGRATIONS_AUTO_APPLY == 'true'" in text
    assert "SUPABASE_MIGRATIONS_AUTO_APPLY: ${{ vars.SUPABASE_MIGRATIONS_AUTO_APPLY }}" in text
    # Nothing in the repository turns it on.
    for path in (Path(".github/workflows"), Path("scripts"), Path("config")):
        for candidate in path.rglob("*"):
            if candidate.is_file() and candidate.suffix in {".yml", ".yaml", ".sh", ".py"}:
                body = candidate.read_text(errors="ignore")
                assert "SUPABASE_MIGRATIONS_AUTO_APPLY: true" not in body
                assert "SUPABASE_MIGRATIONS_AUTO_APPLY=true" not in body


def test_dry_run_never_reaches_the_apply_path():
    text = workflow_text()

    dry_run_steps = [
        line
        for line in text.splitlines()
        if "if:" in line and "inputs.mode == 'dry-run'" in line
    ]
    assert dry_run_steps, "the dry-run report step must be conditioned on the mode"
    for line in dry_run_steps:
        assert "supabase db push --linked\n" not in line

    # The apply step is reachable only through mode == 'apply' (or the
    # unchanged push path); 'dry-run' never appears in its condition.
    apply_condition = text.split(APPLY_STEP, 1)[1].split("run:", 1)[0]
    assert "dry-run" not in apply_condition
    assert "--dry-run" in text  # the mandatory preflight still runs


# ---------------------------------------------------------------------------
# executable: run the extracted gate script under bash
# ---------------------------------------------------------------------------
def extract_gate_script() -> str:
    """Pull the gate's `run:` block out of the workflow and de-indent it."""
    step = workflow_text().split(GATE_STEP, 1)[1].split("- name: Set up Python", 1)[0]
    body = step.split("run: |", 1)[1]
    lines = body.splitlines()
    indent = min(
        (len(line) - len(line.lstrip()) for line in lines if line.strip()),
        default=0,
    )
    dedented = "\n".join(line[indent:] if line.strip() else "" for line in lines)
    return dedented.strip("\n")


def run_gate(*, github_sha: str, expected_sha: str, mode: str, confirmation: str):
    bash = shutil.which("bash")
    if bash is None:  # pragma: no cover - bash is present on every supported runner
        pytest.skip("bash unavailable")
    return subprocess.run(
        [bash, "-c", extract_gate_script()],
        env={
            "PATH": "/usr/bin:/bin",
            "GITHUB_SHA": github_sha,
            "EXPECTED_SHA_INPUT": expected_sha,
            "MODE_INPUT": mode,
            "CONFIRMATION_INPUT": confirmation,
        },
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_extracted_gate_script_is_the_real_one():
    script = extract_gate_script()
    assert script.startswith("set -euo pipefail")
    assert "^[0-9a-f]{40}$" in script


def test_apply_with_matching_sha_and_confirmation_is_authorized():
    result = run_gate(
        github_sha=AUTHORIZED_SHA,
        expected_sha=AUTHORIZED_SHA,
        mode="apply",
        confirmation=CONFIRMATION,
    )
    assert result.returncode == 0, result.stderr
    assert "Manual authorization validated for mode: apply" in result.stdout


@pytest.mark.parametrize(
    ("expected_sha", "label"),
    [
        (OTHER_SHA, "a different full SHA"),
        ("", "a missing SHA"),
        (AUTHORIZED_SHA[:7], "a short SHA"),
        (AUTHORIZED_SHA[:39], "a 39-character SHA"),
        (AUTHORIZED_SHA.upper(), "an uppercase SHA"),
        ("main", "a branch name"),
        ("latest main", "a human phrase"),
        ("g" * 40, "40 non-hex characters"),
        (f"{AUTHORIZED_SHA} ", "a trailing space"),
        (f" {AUTHORIZED_SHA}", "a leading space"),
        (f"{AUTHORIZED_SHA}\nrm -rf /", "an injected second line"),
    ],
)
def test_apply_is_refused_for_any_sha_that_is_not_the_reviewed_one(expected_sha, label):
    result = run_gate(
        github_sha=AUTHORIZED_SHA,
        expected_sha=expected_sha,
        mode="apply",
        confirmation=CONFIRMATION,
    )
    assert result.returncode != 0, f"{label} must be refused"
    assert "No production migrations were applied." in result.stdout


def test_apply_with_correct_sha_but_wrong_confirmation_is_refused():
    for confirmation in ("", "apply", "apply_production_migrations", "yes"):
        result = run_gate(
            github_sha=AUTHORIZED_SHA,
            expected_sha=AUTHORIZED_SHA,
            mode="apply",
            confirmation=confirmation,
        )
        assert result.returncode != 0
        assert "APPLY_PRODUCTION_MIGRATIONS" in result.stdout
        assert "No production migrations were applied." in result.stdout


def test_dry_run_still_requires_the_reviewed_sha():
    ok = run_gate(
        github_sha=AUTHORIZED_SHA,
        expected_sha=AUTHORIZED_SHA,
        mode="dry-run",
        confirmation="",
    )
    assert ok.returncode == 0, ok.stderr

    for bad in ("", OTHER_SHA, AUTHORIZED_SHA[:7]):
        refused = run_gate(
            github_sha=AUTHORIZED_SHA,
            expected_sha=bad,
            mode="dry-run",
            confirmation="",
        )
        assert refused.returncode != 0


def test_gate_never_echoes_its_inputs_beyond_the_mode():
    result = run_gate(
        github_sha=AUTHORIZED_SHA,
        expected_sha=OTHER_SHA,
        mode="apply",
        confirmation=CONFIRMATION,
    )
    combined = result.stdout + result.stderr
    assert AUTHORIZED_SHA not in combined
    assert OTHER_SHA not in combined

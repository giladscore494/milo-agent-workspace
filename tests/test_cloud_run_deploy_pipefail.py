"""Deterministic pipefail proofs for the lookups in scripts/deploy/cloud-run.sh.

The script runs under ``set -euo pipefail``. A lookup written as
``printf ... | grep -q PATTERN`` (or as an awk that ``exit``s on its first
match) stops reading as soon as it finds an early entry; when the input is
larger than a pipe buffer the writer is still blocked on the pipe, gets
SIGPIPE, and pipefail turns the WHOLE pipeline into a failure. A binding that
exists then reads as missing -- and, inverted inside an ``if``, a provider key
or a public IAM member that exists reads as absent.

These tests extract the real function definitions from the script and run
them under the script's own shell options against multi-megabyte inputs whose
matching entry comes FIRST. That reproduces the failure on every run, without
depending on CPU load or scheduling. Every positive match is paired with a
genuine refusal, so the fix cannot pass by accepting everything.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = REPO / "scripts" / "deploy" / "cloud-run.sh"
CONTRACT = REPO / "scripts" / "deploy" / "deployment-contract.sh"
SCRIPT = DEPLOY_SCRIPT.read_text()

# Far larger than a Linux pipe buffer (64 KiB), so a reader that exits on the
# first line always leaves the writer blocked on a closed pipe.
PADDING_LINES = 40_000


def _function(name: str) -> str:
    match = re.search(rf"^{re.escape(name)}\(\) \{{\n.*?^\}}\n", SCRIPT, re.M | re.S)
    assert match, f"{name}() not found in {DEPLOY_SCRIPT}"
    return match.group(0)


FUNCTIONS = "\n".join(
    _function(name)
    for name in (
        "report_field",
        "binding_identities",
        "has_binding",
        "secret_reference_for",
        "assert_bindings_preserved",
        "verify_env_names",
        "verify_secret_refs",
        "verify_no_provider_key",
        "verify_no_public_access",
    )
)

HARNESS_PREAMBLE = f"""
set -euo pipefail
source {CONTRACT}
fail() {{ printf 'FAIL: %s\\n' "$*" >&2; exit 1; }}
PROJECT_ID=test-project
REGION=us-central1
{FUNCTIONS}
"""


def _padding(kind: str) -> str:
    """Many lines that sort AFTER every real binding name."""
    if kind == "secret":
        return "".join(
            f"secret\tZZ_PAD_SECRET_{i:06d}\tZZ_PAD_SECRET_{i:06d}:latest\n"
            for i in range(PADDING_LINES)
        )
    return "".join(f"env\tZZ_PAD_{i:06d}_{'x' * 24}\n" for i in range(PADDING_LINES))


def run_bash(tmp_path: Path, body: str, **files: str) -> subprocess.CompletedProcess:
    """Run BODY after the extracted functions. Each FILES entry becomes a
    shell variable holding that file's content (read without a pipe)."""
    loads = []
    for var, content in files.items():
        path = tmp_path / f"{var}.txt"
        path.write_text(content)
        # Never exported: a multi-megabyte environment would break every exec.
        loads.append(f'declare +x {var}; {var}=$(<"{path}")')
    script = HARNESS_PREAMBLE + "\n".join(loads) + "\n" + body + "\n"
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=120
    )


def report(*lines: str, padding: str = "env") -> str:
    return "".join(f"{line}\n" for line in lines) + _padding(padding)


# ---------------------------------------------------------------------------
# The inputs really do reproduce the failure mode being fixed.
# ---------------------------------------------------------------------------


def test_the_old_piped_lookup_reports_an_early_match_as_missing(tmp_path):
    """Control: with these inputs the pre-fix form fails deterministically.

    If this ever stops failing, the inputs no longer exercise SIGPIPE and the
    tests below would prove nothing.
    """
    result = run_bash(
        tmp_path,
        """
old_has_binding() { printf '%s\\n' "$1" | grep -Fxq "$2"; }
for attempt in 1 2 3; do
  if old_has_binding "$INPUT" "$(printf 'env\\tFIRST')"; then echo found; else echo "missing:$?"; fi
done
""",
        INPUT=report("env\tFIRST"),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["missing:141"] * 3


def test_no_pipeline_in_the_script_ends_in_an_early_exiting_reader():
    """No lookup that may stop at its first match is fed by a pipe."""
    early_exit_after_pipe = re.compile(r"\|\s*(grep\s+-[A-Za-z]*q|awk\b[^\n]*\bexit\b)")
    offenders = [line for line in SCRIPT.splitlines() if early_exit_after_pipe.search(line)]
    assert offenders == []


# ---------------------------------------------------------------------------
# has_binding
# ---------------------------------------------------------------------------


def test_has_binding_finds_an_early_entry_in_a_large_input(tmp_path):
    result = run_bash(
        tmp_path,
        """
for attempt in 1 2 3 4 5; do
  has_binding "$INPUT" "$(printf 'env\\tALLOWED_CORS_ORIGINS')"
done
echo found
""",
        INPUT=report("env\tALLOWED_CORS_ORIGINS"),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "found"


def test_has_binding_finds_the_last_entry_in_a_large_input(tmp_path):
    result = run_bash(
        tmp_path,
        """has_binding "$INPUT" "$(printf 'env\\tLAST')" && echo found""",
        INPUT=_padding("env") + "env\tLAST\n",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "found"


def test_has_binding_refuses_an_entry_that_is_not_there(tmp_path):
    result = run_bash(
        tmp_path,
        """
if has_binding "$INPUT" "$(printf 'env\\tNOT_BOUND')"; then echo found; else echo "missing:$?"; fi
""",
        INPUT=report("env\tALLOWED_CORS_ORIGINS"),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "missing:1"


@pytest.mark.parametrize(
    "needle",
    [
        "env\tALLOWED",  # a prefix of a real line
        "env\tALLOWED_CORS_ORIGINS_EXTRA",  # a line extending a real one
        "ALLOWED_CORS_ORIGINS",  # a suffix of a real line
        "env\tALLOWED.CORS.ORIGINS",  # regex metacharacters are literal
        "secret\tSUPABASE_URL\tSUPABASE_URL:1",  # same name, different version
        "",  # an empty needle matches no non-empty line
    ],
)
def test_has_binding_still_matches_whole_lines_literally(tmp_path, needle):
    result = run_bash(
        tmp_path,
        """if has_binding "$INPUT" "$NEEDLE"; then echo found; else echo missing; fi""",
        INPUT=report("env\tALLOWED_CORS_ORIGINS", "secret\tSUPABASE_URL\tSUPABASE_URL:latest"),
        NEEDLE=needle,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "missing"


def test_has_binding_treats_a_leading_dash_as_data_not_an_option(tmp_path):
    result = run_bash(
        tmp_path,
        """
has_binding "$INPUT" "-v" && echo found
if has_binding "$INPUT" "-x"; then echo found; else echo missing; fi
""",
        INPUT="-v\n" + _padding("env"),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["found", "missing"]


# ---------------------------------------------------------------------------
# verify_env_names / verify_secret_refs
# ---------------------------------------------------------------------------


def test_verify_env_names_accepts_early_required_names_in_a_large_report(tmp_path):
    result = run_bash(
        tmp_path,
        """verify_env_names "API service" "$REPORT" ALLOWED_CORS_ORIGINS ENVIRONMENT GCP_PROJECT_ID""",
        REPORT=report("env\tALLOWED_CORS_ORIGINS", "env\tENVIRONMENT", "env\tGCP_PROJECT_ID"),
    )
    assert result.returncode == 0, result.stderr
    assert "environment variable names: ALLOWED_CORS_ORIGINS ENVIRONMENT GCP_PROJECT_ID" in result.stdout


def test_verify_env_names_still_refuses_a_missing_required_name(tmp_path):
    result = run_bash(
        tmp_path,
        """verify_env_names "API service" "$REPORT" ALLOWED_CORS_ORIGINS GCP_REGION""",
        REPORT=report("env\tALLOWED_CORS_ORIGINS", "env\tENVIRONMENT"),
    )
    assert result.returncode != 0
    assert "API service is missing required environment variable 'GCP_REGION'." in result.stderr


def test_verify_secret_refs_accepts_an_early_reference_in_a_large_report(tmp_path):
    result = run_bash(
        tmp_path,
        """verify_secret_refs "Worker job" "$REPORT" SUPABASE_URL=SUPABASE_URL:latest""",
        REPORT=report("secret\tSUPABASE_URL\tSUPABASE_URL:latest", padding="secret"),
    )
    assert result.returncode == 0, result.stderr
    assert "secret references: SUPABASE_URL->SUPABASE_URL:latest" in result.stdout


@pytest.mark.parametrize(
    "expected",
    [
        "UPSTASH_REDIS_REST_TOKEN=UPSTASH_REDIS_REST_TOKEN:latest",  # not bound at all
        "SUPABASE_URL=SUPABASE_URL:1",  # bound, but to another version
        "SUPABASE_URL=OTHER_SECRET:latest",  # bound, but to another secret
    ],
)
def test_verify_secret_refs_still_refuses_a_missing_or_changed_reference(tmp_path, expected):
    result = run_bash(
        tmp_path,
        """verify_secret_refs "Worker job" "$REPORT" "$EXPECTED\"""",
        REPORT=report("secret\tSUPABASE_URL\tSUPABASE_URL:latest", padding="secret"),
        EXPECTED=expected,
    )
    assert result.returncode != 0
    assert f"Worker job is missing the expected secret reference '{expected}'." in result.stderr


# ---------------------------------------------------------------------------
# assert_bindings_preserved / report_field / secret_reference_for
# ---------------------------------------------------------------------------

BEFORE = (
    "env\tALLOWED_CORS_ORIGINS\n"
    "env\tENVIRONMENT\n"
    "secret\tSUPABASE_URL\tSUPABASE_URL:latest\n"
)


def test_preserved_bindings_are_found_early_in_a_large_after_state(tmp_path):
    result = run_bash(
        tmp_path,
        """assert_bindings_preserved "API service" "$(binding_identities "$BEFORE")" "$(binding_identities "$AFTER")\"""",
        BEFORE=BEFORE,
        AFTER=BEFORE + _padding("env"),
    )
    assert result.returncode == 0, result.stderr
    assert "preserved bindings: OK" in result.stdout


def test_a_dropped_binding_is_still_refused_in_a_large_after_state(tmp_path):
    result = run_bash(
        tmp_path,
        """assert_bindings_preserved "API service" "$(binding_identities "$BEFORE")" "$(binding_identities "$AFTER")\"""",
        BEFORE=BEFORE,
        AFTER="env\tALLOWED_CORS_ORIGINS\nsecret\tSUPABASE_URL\tSUPABASE_URL:latest\n" + _padding("env"),
    )
    assert result.returncode != 0
    assert "lost pre-existing environment/secret bindings" in result.stderr
    assert "env ENVIRONMENT was removed" in result.stderr


def test_a_remapped_secret_is_still_refused_in_a_large_after_state(tmp_path):
    """The remap lookup itself (an awk that exits on its first match) must not
    abort the script before the refusal is reported."""
    result = run_bash(
        tmp_path,
        """assert_bindings_preserved "API service" "$(binding_identities "$BEFORE")" "$(binding_identities "$AFTER")\"""",
        BEFORE=BEFORE,
        AFTER="env\tALLOWED_CORS_ORIGINS\nenv\tENVIRONMENT\nsecret\tSUPABASE_URL\tOTHER:2\n"
        + _padding("secret"),
    )
    assert result.returncode != 0
    assert "secret SUPABASE_URL was REMAPPED from SUPABASE_URL:latest to OTHER:2" in result.stderr


def test_report_field_reads_an_early_record_from_a_large_report(tmp_path):
    result = run_bash(
        tmp_path,
        """
value=$(report_field "$REPORT" image)
printf 'image=%s\\n' "$value"
""",
        REPORT=report("image\tregistry/api:abc", "service-account\tsa@example.test"),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "image=registry/api:abc"


def test_secret_reference_for_reads_an_early_record_from_a_large_input(tmp_path):
    result = run_bash(
        tmp_path,
        """
ref=$(secret_reference_for "$INPUT" SUPABASE_URL)
printf 'ref=%s\\n' "$ref"
""",
        INPUT=report("secret\tSUPABASE_URL\tSUPABASE_URL:latest", padding="secret"),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ref=SUPABASE_URL:latest"


# ---------------------------------------------------------------------------
# Refusals that a SIGPIPE would have turned into silent passes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    ["env\tKIMI_API_KEY", "secret\tMOONSHOT_API_KEY\tMOONSHOT_API_KEY:latest"],
)
def test_an_early_provider_key_in_a_large_report_is_refused(tmp_path, line):
    result = run_bash(
        tmp_path,
        """verify_no_provider_key "API service" "$REPORT\"""",
        REPORT=report(line),
    )
    assert result.returncode != 0
    name = line.split("\t")[1]
    assert f"API service carries provider key '{name}'." in result.stderr


def test_a_large_report_without_a_provider_key_passes(tmp_path):
    result = run_bash(
        tmp_path,
        """verify_no_provider_key "API service" "$REPORT\"""",
        REPORT=report("env\tKIMI_API_KEY_ROTATION_NOTE", "env\tMOONSHOT_API"),
    )
    assert result.returncode == 0, result.stderr
    assert "provider keys absent (KIMI_API_KEY MOONSHOT_API_KEY): OK" in result.stdout


def _iam_policy(first_members: list[str]) -> str:
    members = first_members + [
        f"serviceAccount:pad-{i:06d}@example.iam.gserviceaccount.com" for i in range(PADDING_LINES)
    ]
    rows = ",\n".join(f'        "{m}"' for m in members)
    return (
        '{\n  "bindings": [\n    {\n      "members": [\n'
        + rows
        + '\n      ],\n      "role": "roles/run.invoker"\n    }\n  ]\n}\n'
    )


@pytest.mark.parametrize("member", ["allUsers", "allAuthenticatedUsers"])
@pytest.mark.parametrize("kind", ["service", "job"])
def test_an_early_public_member_in_a_large_iam_policy_is_refused(tmp_path, member, kind):
    result = run_bash(
        tmp_path,
        f"""
gcloud() {{ printf '%s' "$POLICY"; }}
verify_no_public_access {kind} milo-agent-api
""",
        POLICY=_iam_policy([member]),
    )
    assert result.returncode != 0
    assert f"{kind} 'milo-agent-api' grants access to allUsers/allAuthenticatedUsers." in result.stderr


def test_a_large_private_iam_policy_passes(tmp_path):
    result = run_bash(
        tmp_path,
        """
gcloud() { printf '%s' "$POLICY"; }
verify_no_public_access service milo-agent-api
""",
        POLICY=_iam_policy(["serviceAccount:milo-gateway@example.iam.gserviceaccount.com"]),
    )
    assert result.returncode == 0, result.stderr
    assert "IAM: private (no allUsers / allAuthenticatedUsers)" in result.stdout


# ---------------------------------------------------------------------------
# A helper that fails can never turn a refusal into a pass
# ---------------------------------------------------------------------------


def test_a_failing_name_extraction_aborts_the_provider_key_check(tmp_path):
    """If the names cannot be extracted, "no provider key" is not concluded."""
    result = run_bash(
        tmp_path,
        """
awk() { return 2; }
verify_no_provider_key "API service" "$REPORT"
echo reached-after-check
""",
        REPORT=report("env\tKIMI_API_KEY"),
    )
    assert result.returncode != 0
    assert "provider keys absent" not in result.stdout
    assert "reached-after-check" not in result.stdout


def test_a_failing_policy_read_aborts_the_public_access_check(tmp_path):
    result = run_bash(
        tmp_path,
        """
gcloud() { return 1; }
verify_no_public_access service milo-agent-api
echo reached-after-check
""",
    )
    assert result.returncode != 0
    assert "IAM: private" not in result.stdout
    assert "reached-after-check" not in result.stdout


def test_the_answering_checks_start_no_subprocess_that_could_fail():
    """has_binding and the IAM check decide in the shell itself: an exit status
    of 1 from them can only ever mean "not present"."""
    def code(name: str) -> str:
        return "\n".join(
            line for line in _function(name).splitlines() if not line.lstrip().startswith("#")
        )

    assert "grep" not in code("has_binding")
    body = code("verify_no_public_access")
    assert "grep" not in body
    assert '[[ "$policy" =~ $public_members ]]' in body

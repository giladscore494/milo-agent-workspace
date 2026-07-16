"""Static workflow-trust and forbidden-architecture tests for bootstrap v2.

These enforce the machine-detectable items of the forbidden-pattern list in
docs/production-readiness/BOOTSTRAP_V2_ARCHITECTURE.md.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "bootstrap-production-v2.yml"
BOOTSTRAP_DIR = REPO / "scripts" / "release" / "bootstrap_v2"
LAUNCHER_SH = REPO / "scripts" / "release" / "bootstrap-production-v2.sh"
LAUNCHER_PY = REPO / "scripts" / "release" / "bootstrap-production-v2.py"


def workflow_text() -> str:
    return WORKFLOW.read_text()


def bootstrap_sources() -> dict[Path, str]:
    files = sorted(BOOTSTRAP_DIR.rglob("*.py")) + [LAUNCHER_PY]
    return {path: path.read_text() for path in files}


def _strip_comments_and_docstrings(text: str) -> str:
    """Executable code only: drop # comments and string-literal docstrings."""

    import ast
    import io
    import tokenize

    result = []
    try:
        tokens = tokenize.generate_tokens(io.StringIO(text).readline)
        for token in tokens:
            if token.type == tokenize.COMMENT:
                continue
            if token.type == tokenize.STRING and token.string.startswith(('"""', "'''")):
                continue
            result.append(token.string)
    except tokenize.TokenizeError:
        return text
    return " ".join(result)


def job_block(text: str, job: str) -> str:
    match = re.search(rf"^  {job}:\n(.*?)(?=^  \w+:|\Z)", text, re.M | re.S)
    assert match, f"job {job} not found"
    return match.group(0)


# ------------------------------------------------------------- workflow trust


def test_workflow_exists_and_is_dispatch_only():
    text = workflow_text()
    assert "workflow_dispatch:" in text
    assert "pull_request" not in text
    assert "pull_request_target" not in text
    assert re.search(r"^on:\n  workflow_dispatch:", text, re.M)


def test_untrusted_refs_fail_before_any_authentication():
    text = workflow_text()
    ref_check = text.index('if [ "${GITHUB_REF}" != "${TRUSTED_REF}" ]')
    first_auth = text.index("google-github-actions/auth@")
    assert ref_check < first_auth
    guard = job_block(text, "guard")
    assert "permissions: {}" in guard
    assert "secrets." not in guard  # the guard job holds no credentials
    assert "google-github-actions" not in guard


def test_pr_refs_cannot_run_apply():
    text = workflow_text()
    assert "TRUSTED_REF: refs/heads/main" in text
    apply = job_block(text, "apply")
    assert "needs: guard" in apply
    guard = job_block(text, "guard")
    assert "exit 1" in guard


def test_all_actions_pinned_to_immutable_full_shas_with_version_comments():
    for line in workflow_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("uses:"):
            assert re.search(
                r"uses: [\w./-]+@[0-9a-f]{40} # v[\w.]+", stripped
            ), f"unpinned or uncommented action: {stripped}"


def test_top_level_permissions_are_empty_and_jobs_are_least_privilege():
    text = workflow_text()
    assert re.search(r"^permissions: \{\}$", text, re.M)
    for job in ("plan", "apply", "audit"):
        block = job_block(text, job)
        assert "contents: read" in block
        assert "contents: write" not in block
        assert "actions: write" not in block
    guard = job_block(text, "guard")
    assert "id-token" not in guard


def test_id_token_only_in_jobs_that_authenticate_to_gcp():
    text = workflow_text()
    for job in ("plan", "apply", "audit"):
        block = job_block(text, job)
        assert ("id-token: write" in block) == ("google-github-actions/auth@" in block)


def test_plan_job_has_no_write_capable_production_credentials():
    plan = job_block(workflow_text(), "plan")
    assert "GITHUB_BOOTSTRAP_READONLY_SA" in plan
    assert "GITHUB_BOOTSTRAP_OPERATOR_SA" not in plan
    assert "environment:" not in plan


def test_apply_requires_environment_approval_and_digest():
    apply = job_block(workflow_text(), "apply")
    assert "environment: production" in apply
    assert "GITHUB_BOOTSTRAP_OPERATOR_SA" in apply
    assert "--approved-plan-digest" in apply
    assert "--confirm-production-change" in apply
    guard = job_block(workflow_text(), "guard")
    assert "approved_plan_digest" in guard
    assert "I_UNDERSTAND_THIS_CHANGES_PRODUCTION" in guard


def test_concurrency_prevents_overlap_and_audit_never_cancels_apply():
    text = workflow_text()
    apply = job_block(text, "apply")
    audit = job_block(text, "audit")
    assert "group: bootstrap-v2-apply" in apply
    assert "cancel-in-progress: false" in apply
    assert "group: bootstrap-v2-audit" in audit
    assert "cancel-in-progress: false" in audit
    assert "bootstrap-v2-apply" not in audit


def test_audit_job_runs_audit_mode_only():
    audit = job_block(workflow_text(), "audit")
    assert "--mode audit" in audit
    assert "--mode apply" not in audit
    assert "--confirm-production-change" not in audit
    assert "MILO_OPERATOR_ACK" not in audit


def test_inputs_are_strictly_validated():
    guard = job_block(workflow_text(), "guard")
    assert "grep -Eq '^[0-9a-f]{40}$'" in guard
    assert "grep -Eq '^[0-9a-f]{64}$'" in guard
    assert '"${INPUT_SHA}" != "${GITHUB_SHA}"' in guard


def test_run_scripts_take_inputs_via_env_not_inline_expressions():
    text = workflow_text()
    for match in re.finditer(r"run: \|\n((?:          .*\n)+)", text):
        assert not re.findall(r"\$\{\{", match.group(1)), match.group(1)


def test_pr_triggered_workflows_hold_no_bootstrap_secrets():
    for workflow in (REPO / ".github" / "workflows").glob("*.yml"):
        text = workflow.read_text()
        if "pull_request" in text:
            for name in ("UPSTASH_API_KEY", "UPSTASH_EMAIL", "VERCEL_TOKEN"):
                assert name not in text, f"{workflow.name} exposes {name} to PRs"


# ------------------------------------------------------- launcher purity


def test_bash_launcher_is_exec_only():
    text = LAUNCHER_SH.read_text()
    lines = [
        line
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert lines[0] == "#!/usr/bin/env bash" or text.startswith("#!/usr/bin/env bash")
    meaningful = [l for l in lines if not l.startswith("#!")]
    assert meaningful == [
        "set -euo pipefail",
        'exec python3 "$(dirname "${BASH_SOURCE[0]}")/bootstrap-production-v2.py" "$@"',
    ]
    code = "\n".join(meaningful)
    for forbidden in ("gcloud", "vercel", "curl", "upstash", "jq", "grep", "sed", "awk", "if ", "for ", "case "):
        assert forbidden not in code, forbidden


def test_python_launcher_only_delegates():
    text = LAUNCHER_PY.read_text()
    assert "from bootstrap_v2.cli import main" in text
    for forbidden in ("subprocess", "gcloud", "urllib", "requests"):
        assert forbidden not in text, forbidden


# ------------------------------------------- forbidden architecture (static)


def test_no_or_true_anywhere_in_bootstrap_sources():
    for path, text in bootstrap_sources().items():
        assert "|| true" not in text, path
    assert "|| true" not in LAUNCHER_SH.read_text()


def test_no_deploy_execute_or_promotion_capability():
    forbidden_patterns = (
        r"run['\"]?,\s*['\"]deploy",  # gcloud run deploy
        r"jobs['\"]?,\s*['\"]execute",  # gcloud run jobs execute
        r"docker\s+push",
        r"vercel\s+deploy",
        r"--prod\b",
        r"promote",
        r"redeploy",
    )
    for path, text in bootstrap_sources().items():
        code = _strip_comments_and_docstrings(text)
        for pattern in forbidden_patterns:
            assert not re.search(pattern, code), (path, pattern)


def test_no_service_account_key_creation():
    for path, text in bootstrap_sources().items():
        assert not re.search(r"keys['\"]?,\s*['\"]create", text), path
        assert "keys create" not in text, path


def test_no_allow_unauthenticated_grant():
    for path, text in bootstrap_sources().items():
        for match in re.finditer(r"--(?:no-)?allow-unauthenticated", text):
            assert match.group(0) == "--no-allow-unauthenticated", (
                path,
                match.group(0),
            )


def test_no_broad_principal_grant_capability():
    # allUsers / allAuthenticatedUsers may appear only in the forbidden list
    # (policy.py) and in validators that reject them — never in adapters.
    adapters = {
        path: text
        for path, text in bootstrap_sources().items()
        if "adapters" in str(path)
    }
    for path, text in adapters.items():
        assert "allUsers" not in text, path
        assert "allAuthenticatedUsers" not in text, path


def test_no_truncated_fingerprints_in_sources():
    for path, text in bootstrap_sources().items():
        assert "[:16]" not in text, path
        assert not re.search(r"hexdigest\(\)\[", text), path


def test_no_project_wide_secret_accessor_grant():
    for path, text in bootstrap_sources().items():
        if "adapters" in str(path):
            assert "add-iam-policy-binding" not in text, path
            assert "projects add-iam-policy-binding" not in text, path


def test_milo_release_sha_never_written_only_removed():
    for path, text in bootstrap_sources().items():
        for line in text.splitlines():
            if "MILO_RELEASE_SHA" not in line:
                continue
            assert not re.search(r"MILO_RELEASE_SHA['\"]?\s*[:=]\s*['\"][^'\"]", line), (
                path,
                line,
            )


def test_execution_flags_can_only_equal_false_in_planner():
    from pathlib import Path as _P

    policy = (BOOTSTRAP_DIR / "policy.py").read_text()
    assert 'EXECUTION_FLAG_REQUIRED_VALUE = "false"' in policy
    planner = (BOOTSTRAP_DIR / "planner.py").read_text()
    assert "EXECUTION_FLAG_REQUIRED_VALUE" in planner
    assert '"true"' not in planner


def test_no_model_provider_calls_in_bootstrap():
    for path, text in bootstrap_sources().items():
        for word in ("moonshot", "api.moonshot", "kimi.chat", "chat/completions"):
            assert word not in text.lower() or "KIMI_API_KEY" in text, (path, word)
        assert "openai" not in text.lower(), path


def test_one_source_of_failure_truth_no_module_level_flags():
    for path, text in bootstrap_sources().items():
        assert not re.search(r"^\s*FAILED\s*=", text, re.M), path
        assert not re.search(r"^BLOCKERS\s*=\s*0", text, re.M), path
        assert not re.search(r"global\s+(failed|blocked|errors)", text), path


def test_metadata_v3_schema_is_closed_in_code():
    metadata = (BOOTSTRAP_DIR / "validators" / "metadata.py").read_text()
    assert "METADATA_UNKNOWN_KEY" in metadata
    assert "METADATA_DUPLICATE_KEY" in metadata
    assert "METADATA_FORBIDDEN_KEY" in metadata
    assert "MILO_RELEASE_SHA" in metadata  # present in the forbidden list


def test_bootstrap_v2_never_parses_human_cli_text_for_identity():
    gcp = (BOOTSTRAP_DIR / "adapters" / "gcp.py").read_text()
    assert '"--format", "json"' in gcp or '--format=json' in gcp
    # every gcloud describe/list read requests structured output
    # (get-iam-policy prefixes receive --format json at the call site)
    for match in re.finditer(r'\(\s*"gcloud",[^)]*\)', gcp, re.S):
        block = match.group(0)
        if any(verb in block for verb in ('"describe"', '"list"')):
            assert "json" in block, block

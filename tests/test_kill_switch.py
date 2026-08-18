"""Executable safety proofs for the hardened Stage C kill switch.

Runs kill-switch.sh against a mocked gcloud (PATH shim) — no network and no
real GCP mutation. Proves: robust active-execution detection (absent/null
completion fields), cancel-only-active behavior, idempotent reruns,
fail-closed postconditions and truthful exit status/messages."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
KILL_SWITCH = REPO / "scripts" / "release" / "stage-c" / "kill-switch.sh"


API_OK = {"spec": {"template": {"spec": {"containers": [{"env": [
    {"name": "MILO_ENABLE_RUN_CREATION", "value": "false"},
    {"name": "JOB_LAUNCHER", "value": "disabled"},
    {"name": "MILO_ENABLE_PAID_EXECUTION", "value": "false"},
]}]}}}}

WORKER_OK = {"spec": {"template": {"spec": {"template": {"spec": {"containers": [{"env": [
    {"name": "MILO_ENABLE_PAID_EXECUTION", "value": "false"},
]}]}}}}}}


def worker_with_bound_key():
    return {"spec": {"template": {"spec": {"template": {"spec": {"containers": [{"env": [
        {"name": "MILO_ENABLE_PAID_EXECUTION", "value": "false"},
        {"name": "KIMI_API_KEY", "valueFrom": {"secretKeyRef": {"name": "KIMI_API_KEY"}}},
    ]}]}}}}}}


def execution(name, *, completion_time="2026-08-18T02:19:11Z", condition=None, status_present=True):
    payload = {"metadata": {"name": name}}
    if status_present:
        status = {}
        if completion_time is not None or condition is None:
            status["completionTime"] = completion_time
        if condition is not None:
            status["conditions"] = [{"type": "Completed", "status": condition}]
        payload["status"] = status
    return payload


def active_execution(name):
    """Genuinely running: completionTime null AND no terminal condition."""
    return {"metadata": {"name": name}, "status": {"completionTime": None, "conditions": [{"type": "Completed", "status": "Unknown"}]}}


MOCK_GCLOUD = r"""#!/usr/bin/env bash
args="$*"
printf '%s\n' "${args}" >> "${MOCK_LOG}"
case "${args}" in
  *"jobs update"*"--remove-secrets"*)
    exit "${MOCK_REMOVE_SECRETS_EXIT:-0}" ;;
  *"jobs update"*"--remove-env-vars"*)
    exit "${MOCK_REMOVE_ENV_VARS_EXIT:-0}" ;;
  *"jobs update"*)
    exit "${MOCK_JOBS_UPDATE_EXIT:-0}" ;;
  *"services update"*)
    exit "${MOCK_SERVICES_UPDATE_EXIT:-0}" ;;
  *"executions list"*)
    n=$(( $(cat "${MOCK_DIR}/list-count" 2>/dev/null || echo 0) + 1 ))
    echo "${n}" > "${MOCK_DIR}/list-count"
    if [ -f "${MOCK_DIR}/executions-${n}.json" ]; then
      cat "${MOCK_DIR}/executions-${n}.json"
    else
      cat "${MOCK_DIR}/executions.json"
    fi ;;
  *"executions cancel"*)
    if [ "${MOCK_CANCEL_EXIT:-0}" != "0" ]; then exit "${MOCK_CANCEL_EXIT}"; fi
    if [ -f "${MOCK_DIR}/executions-after-cancel.json" ]; then
      cp "${MOCK_DIR}/executions-after-cancel.json" "${MOCK_DIR}/executions.json"
    fi
    ;;
  *"services describe"*)
    cat "${MOCK_DIR}/api.json" ;;
  *"jobs describe"*)
    cat "${MOCK_DIR}/worker.json" ;;
  *)
    echo "unexpected gcloud invocation: ${args}" >&2
    exit 9 ;;
esac
"""


def run_kill_switch(tmp_path, *, executions, after_cancel=None, api=API_OK, worker=WORKER_OK, env=None, sequence=None):
    """Run kill-switch.sh against the mocked gcloud.

    ``sequence`` maps a 1-based executions-list call number to a listing (or
    raw string) served for exactly that call; other calls fall back to the
    ``executions`` fixture (which ``after_cancel`` rewrites on cancel)."""
    mock_dir = tmp_path / "mock"
    mock_dir.mkdir(exist_ok=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (mock_dir / "executions.json").write_text(json.dumps(executions))
    if after_cancel is not None:
        (mock_dir / "executions-after-cancel.json").write_text(json.dumps(after_cancel))
    for call_number, listing in (sequence or {}).items():
        body = listing if isinstance(listing, str) else json.dumps(listing)
        (mock_dir / f"executions-{call_number}.json").write_text(body)
    (mock_dir / "list-count").write_text("0")
    (mock_dir / "api.json").write_text(json.dumps(api))
    (mock_dir / "worker.json").write_text(json.dumps(worker))
    gcloud = bin_dir / "gcloud"
    gcloud.write_text(MOCK_GCLOUD)
    gcloud.chmod(0o755)
    log = mock_dir / "gcloud.log"
    log.write_text("")
    full_env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "MOCK_DIR": str(mock_dir),
        "MOCK_LOG": str(log),
        "KILL_SWITCH_CANCEL_VERIFY_ATTEMPTS": "2",
        "KILL_SWITCH_CANCEL_VERIFY_DELAY_SECONDS": "0",
        **(env or {}),
    }
    result = subprocess.run(["bash", str(KILL_SWITCH)], capture_output=True, text=True, env=full_env, timeout=60)
    return result, log.read_text()


def test_bash_syntax_is_valid():
    check = subprocess.run(["bash", "-n", str(KILL_SWITCH)], capture_output=True, text=True)
    assert check.returncode == 0, check.stderr


def test_zero_executions_succeeds_without_cancelling(tmp_path):
    result, log = run_kill_switch(tmp_path, executions=[])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "KILL SWITCH APPLIED" in result.stdout
    assert "executions cancel" not in log
    # The fail-closed mutations still ran.
    assert "jobs update" in log
    assert "services update" in log
    assert "MILO_ENABLE_PAID_EXECUTION=false" in log
    assert "MILO_ENABLE_RUN_CREATION=false,JOB_LAUNCHER=disabled" in log
    # BOTH provider-key aliases are removed, as secrets AND as env vars.
    for alias in ("KIMI_API_KEY", "MOONSHOT_API_KEY"):
        assert f"--remove-secrets={alias}" in log
        assert f"--remove-env-vars={alias}" in log
    # The final zero-active verification ran even though the initial
    # listing was already empty.
    assert log.count("executions list") >= 2
    assert "Final verification: zero active worker executions." in result.stdout
    assert result.stdout.index("Final verification") < result.stdout.index("KILL SWITCH APPLIED")


def test_one_active_execution_is_cancelled_and_verified(tmp_path):
    result, log = run_kill_switch(
        tmp_path,
        executions=[active_execution("milo-agent-worker-abc12")],
        after_cancel=[execution("milo-agent-worker-abc12")],
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Cancelling active execution milo-agent-worker-abc12" in result.stdout
    assert log.count("executions cancel") == 1
    assert "milo-agent-worker-abc12" in log
    assert "KILL SWITCH APPLIED" in result.stdout


def test_terminal_execution_is_left_alone(tmp_path):
    result, log = run_kill_switch(tmp_path, executions=[execution("milo-agent-worker-mcfrx")])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "executions cancel" not in log


def test_mixed_terminal_and_active_cancels_only_the_active_one(tmp_path):
    result, log = run_kill_switch(
        tmp_path,
        executions=[
            execution("milo-agent-worker-done1"),
            active_execution("milo-agent-worker-live1"),
            execution("milo-agent-worker-done2", completion_time=None, condition="False"),
        ],
        after_cancel=[
            execution("milo-agent-worker-done1"),
            execution("milo-agent-worker-live1"),
            execution("milo-agent-worker-done2", completion_time=None, condition="False"),
        ],
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert log.count("executions cancel") == 1
    cancel_lines = [line for line in log.splitlines() if "executions cancel" in line]
    assert "milo-agent-worker-live1" in cancel_lines[0]
    assert "done1" not in cancel_lines[0] and "done2" not in cancel_lines[0]


@pytest.mark.parametrize("nonterminal", [
    # completionTime explicitly null, no conditions.
    {"metadata": {"name": "milo-agent-worker-null1"}, "status": {"completionTime": None}},
    # completionTime absent entirely.
    {"metadata": {"name": "milo-agent-worker-miss1"}, "status": {"conditions": [{"type": "Completed", "status": "Unknown"}]}},
    # status object missing entirely.
    {"metadata": {"name": "milo-agent-worker-nost1"}},
])
def test_missing_or_null_completion_fields_are_treated_as_active(tmp_path, nonterminal):
    name = nonterminal["metadata"]["name"]
    result, log = run_kill_switch(tmp_path, executions=[nonterminal], after_cancel=[execution(name)])
    assert result.returncode == 0, result.stdout + result.stderr
    assert log.count("executions cancel") == 1
    assert name in log


def test_already_fail_closed_state_is_idempotent(tmp_path):
    """Rerun semantics: every provider alias binding is already gone, so
    each per-alias removal fails; that stays a tolerated no-op because the
    postconditions independently prove absence."""
    result, log = run_kill_switch(tmp_path, executions=[], env={"MOCK_REMOVE_SECRETS_EXIT": "1", "MOCK_REMOVE_ENV_VARS_EXIT": "1"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "KILL SWITCH APPLIED" in result.stdout
    update_lines = [line for line in log.splitlines() if "jobs update" in line]
    # One paid-flag update plus one secret + one env-var removal per alias.
    assert len(update_lines) == 5
    assert sum("--remove-secrets" in line for line in update_lines) == 2
    assert sum("--remove-env-vars" in line for line in update_lines) == 2


def test_safe_repeated_execution(tmp_path):
    for _ in range(2):
        result, _ = run_kill_switch(tmp_path, executions=[])
        assert result.returncode == 0, result.stdout + result.stderr
        assert "KILL SWITCH APPLIED" in result.stdout


def test_cancellation_failure_is_critical_and_truthful(tmp_path):
    result, _ = run_kill_switch(
        tmp_path,
        executions=[active_execution("milo-agent-worker-live1")],
        env={"MOCK_CANCEL_EXIT": "1"},
    )
    assert result.returncode != 0
    assert "KILL SWITCH APPLIED" not in result.stdout
    assert "KILL SWITCH CRITICAL" in result.stderr
    assert "KILL SWITCH INCOMPLETE" in result.stderr


def test_execution_still_active_after_bounded_verification_fails(tmp_path):
    # Cancel "succeeds" but the execution never leaves the active state; the
    # bounded re-verification must fail closed instead of polling forever.
    result, log = run_kill_switch(
        tmp_path,
        executions=[active_execution("milo-agent-worker-stuck1")],
        after_cancel=[active_execution("milo-agent-worker-stuck1")],
    )
    assert result.returncode != 0
    assert "still active after bounded cancellation" in result.stderr
    assert "KILL SWITCH APPLIED" not in result.stdout
    # Bounded: exactly initial list + verify-attempt lists, no runaway loop.
    assert log.count("executions list") <= 4


def test_failed_worker_postcondition_exits_nonzero(tmp_path):
    result, _ = run_kill_switch(tmp_path, executions=[], worker=worker_with_bound_key())
    assert result.returncode != 0
    assert "KILL SWITCH APPLIED" not in result.stdout
    assert "worker postcondition failed" in result.stderr


def test_failed_api_postcondition_exits_nonzero(tmp_path):
    bad_api = {"spec": {"template": {"spec": {"containers": [{"env": [
        {"name": "MILO_ENABLE_RUN_CREATION", "value": "true"},
        {"name": "JOB_LAUNCHER", "value": "cloud_run"},
    ]}]}}}}
    result, _ = run_kill_switch(tmp_path, executions=[], api=bad_api)
    assert result.returncode != 0
    assert "KILL SWITCH APPLIED" not in result.stdout
    assert "API postcondition failed" in result.stderr


def test_malformed_execution_listing_fails_closed(tmp_path):
    mock_dir = tmp_path / "mock"
    mock_dir.mkdir(exist_ok=True)
    (mock_dir / "executions.json").write_text("this is not json")
    (mock_dir / "api.json").write_text(json.dumps(API_OK))
    (mock_dir / "worker.json").write_text(json.dumps(WORKER_OK))
    result, _ = run_kill_switch(tmp_path, executions=[])  # rewrites executions.json
    assert result.returncode == 0  # sanity: valid listing passes
    (mock_dir / "executions.json").write_text("this is not json")
    bin_dir = tmp_path / "bin"
    log = mock_dir / "gcloud.log"
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "MOCK_DIR": str(mock_dir),
        "MOCK_LOG": str(log),
        "KILL_SWITCH_CANCEL_VERIFY_ATTEMPTS": "2",
        "KILL_SWITCH_CANCEL_VERIFY_DELAY_SECONDS": "0",
    }
    rerun = subprocess.run(["bash", str(KILL_SWITCH)], capture_output=True, text=True, env=env, timeout=60)
    assert rerun.returncode != 0
    assert "could not determine active worker executions" in rerun.stderr
    assert "KILL SWITCH APPLIED" not in rerun.stdout


def test_never_prints_secret_values(tmp_path):
    result, _ = run_kill_switch(tmp_path, executions=[], worker=worker_with_bound_key())
    combined = result.stdout + result.stderr
    assert "secretKeyRef" not in combined
    assert "sk-" not in combined


def test_env_pinning_still_refuses_hostile_overrides(tmp_path):
    result, _ = run_kill_switch(tmp_path, executions=[], env={"STAGE_C_PROJECT": "attacker-project-123"})
    assert result.returncode != 0
    assert "STAGE C REFUSED" in result.stderr


# ---------------------------------------------------------------------------
# Provider-key alias coverage: the worker accepts KIMI_API_KEY OR
# MOONSHOT_API_KEY, so the kill switch must remove and verify BOTH.
# ---------------------------------------------------------------------------


def worker_with(env_entries):
    return {"spec": {"template": {"spec": {"template": {"spec": {"containers": [{"env": [
        {"name": "MILO_ENABLE_PAID_EXECUTION", "value": "false"},
        *env_entries,
    ]}]}}}}}}


def api_with(extra_entries):
    spec = json.loads(json.dumps(API_OK))
    spec["spec"]["template"]["spec"]["containers"][0]["env"].extend(extra_entries)
    return spec


def secret_entry(name):
    return {"name": name, "valueFrom": {"secretKeyRef": {"name": name}}}


def literal_entry(name):
    return {"name": name, "value": "unit-test-literal-value"}


def test_worker_with_only_moonshot_alias_bound_fails_postcondition(tmp_path):
    result, _ = run_kill_switch(tmp_path, executions=[], worker=worker_with([secret_entry("MOONSHOT_API_KEY")]))
    assert result.returncode != 0
    assert "KILL SWITCH APPLIED" not in result.stdout
    assert "worker postcondition failed" in result.stderr


@pytest.mark.parametrize("alias", ["KIMI_API_KEY", "MOONSHOT_API_KEY"])
def test_worker_with_literal_provider_value_fails_postcondition(tmp_path, alias):
    result, _ = run_kill_switch(tmp_path, executions=[], worker=worker_with([literal_entry(alias)]))
    assert result.returncode != 0
    assert "KILL SWITCH APPLIED" not in result.stdout
    assert "worker postcondition failed" in result.stderr


def test_both_aliases_are_removed_when_bound(tmp_path):
    """Both aliases bound → a removal invocation is issued per alias and per
    binding kind, each in its own gcloud call so one alias's failure can
    never hide behind the other's success; the (post-removal) describe then
    proves both absent."""
    result, log = run_kill_switch(tmp_path, executions=[])
    assert result.returncode == 0, result.stdout + result.stderr
    removal_lines = [line for line in log.splitlines() if "--remove-secrets" in line or "--remove-env-vars" in line]
    assert len(removal_lines) == 4
    for line in removal_lines:  # one alias per invocation, never combined
        assert line.count("API_KEY") == 1


@pytest.mark.parametrize("entry", [secret_entry("KIMI_API_KEY"), secret_entry("MOONSHOT_API_KEY"), literal_entry("MOONSHOT_API_KEY")])
def test_api_with_any_provider_alias_fails_postcondition(tmp_path, entry):
    result, _ = run_kill_switch(tmp_path, executions=[], api=api_with([entry]))
    assert result.returncode != 0
    assert "KILL SWITCH APPLIED" not in result.stdout
    assert "API postcondition failed" in result.stderr


def test_one_alias_removal_failure_is_not_hidden_when_still_bound(tmp_path):
    """--remove-secrets fails for one alias that IS still bound: the
    tolerated removal failure cannot produce success, because the worker
    postcondition independently detects the surviving binding."""
    result, _ = run_kill_switch(
        tmp_path,
        executions=[],
        worker=worker_with([secret_entry("MOONSHOT_API_KEY")]),
        env={"MOCK_REMOVE_SECRETS_EXIT": "1"},
    )
    assert result.returncode != 0
    assert "KILL SWITCH APPLIED" not in result.stdout
    assert "worker postcondition failed" in result.stderr


def test_alias_fixtures_never_leak_values(tmp_path):
    result, log = run_kill_switch(tmp_path, executions=[], worker=worker_with([literal_entry("MOONSHOT_API_KEY")]))
    combined = result.stdout + result.stderr + log
    assert "unit-test-literal-value" not in combined
    assert "secretKeyRef" not in result.stdout + result.stderr


# ---------------------------------------------------------------------------
# Unconditional final active-execution verification
# ---------------------------------------------------------------------------


def test_execution_appearing_only_in_final_verification_is_cancelled_before_success(tmp_path):
    """Initial listing empty; a fresh execution becomes visible only in the
    final verification listing. It must be cancelled and re-verified before
    any success claim."""
    result, log = run_kill_switch(
        tmp_path,
        executions=[],
        sequence={1: [], 2: [active_execution("milo-agent-worker-late1")]},
        after_cancel=[execution("milo-agent-worker-late1")],
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert log.count("executions cancel") == 1
    assert "milo-agent-worker-late1" in log
    assert log.count("executions list") >= 3  # initial, final-detect, re-verify
    assert "KILL SWITCH APPLIED" in result.stdout


def test_execution_appearing_in_final_verification_that_never_settles_blocks_success(tmp_path):
    result, _ = run_kill_switch(
        tmp_path,
        executions=[active_execution("milo-agent-worker-late2")],
        sequence={1: []},
        after_cancel=[active_execution("milo-agent-worker-late2")],
    )
    assert result.returncode != 0
    assert "KILL SWITCH APPLIED" not in result.stdout
    assert "still active after bounded cancellation" in result.stderr


def test_final_execution_list_parse_failure_returns_nonzero(tmp_path):
    """Initial listing is fine and empty; the FINAL verification listing is
    unparseable — success must not be claimed."""
    result, _ = run_kill_switch(tmp_path, executions=[], sequence={2: "this is not json"})
    assert result.returncode != 0
    assert "KILL SWITCH APPLIED" not in result.stdout
    assert "could not determine active worker executions" in result.stderr

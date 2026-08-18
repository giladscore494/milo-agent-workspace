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
  *"jobs update"*)
    exit "${MOCK_JOBS_UPDATE_EXIT:-0}" ;;
  *"services update"*)
    exit "${MOCK_SERVICES_UPDATE_EXIT:-0}" ;;
  *"executions list"*)
    cat "${MOCK_DIR}/executions.json" ;;
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


def run_kill_switch(tmp_path, *, executions, after_cancel=None, api=API_OK, worker=WORKER_OK, env=None):
    mock_dir = tmp_path / "mock"
    mock_dir.mkdir(exist_ok=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (mock_dir / "executions.json").write_text(json.dumps(executions))
    if after_cancel is not None:
        (mock_dir / "executions-after-cancel.json").write_text(json.dumps(after_cancel))
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
    """Rerun semantics: the provider secret binding is already gone, so
    --remove-secrets fails; the flag-only fallback and postconditions still
    succeed."""
    result, log = run_kill_switch(tmp_path, executions=[], env={"MOCK_REMOVE_SECRETS_EXIT": "1"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "KILL SWITCH APPLIED" in result.stdout
    # Fallback flag-only update ran after the remove-secrets failure.
    update_lines = [line for line in log.splitlines() if "jobs update" in line]
    assert len(update_lines) == 2
    assert "--remove-secrets" in update_lines[0]
    assert "--remove-secrets" not in update_lines[1]


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
    assert "still active after cancellation" in result.stderr
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

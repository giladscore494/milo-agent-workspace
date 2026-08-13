"""Executable safety proofs for the Stage C operator toolkit.

Loads the probe scripts as modules with mocked HTTP layers and proves the
corrective-pass guarantees offline:

- poll distinguishes PASS terminal states from FAIL terminal states and
  never exits 0 on an unacceptable terminal;
- DB preflight enforces a real exact count and fails closed;
- the evidence collector is an executable acceptance gate that exits
  non-zero when any criterion fails;
- verify_caps.py fails on missing/changed/looser cap values, wrong images
  and wrong flag posture on either surface;
- the shell steps gate on structured PASS verdicts instead of warnings.

No network, no gcloud, no provider calls.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
STAGE_C = REPO / "scripts" / "release" / "stage-c"

CAPS = (
    "MILO_MAX_MODEL_CALLS_PER_RUN=200,MILO_MAX_INPUT_TOKENS_PER_RUN=700000,"
    "MILO_MAX_OUTPUT_TOKENS_PER_RUN=250000,MILO_MAX_TOTAL_TOKENS_PER_RUN=900000,"
    "MILO_MAX_ESTIMATED_COST_PER_RUN=4.00,MILO_MAX_COST_PER_RUN=3.00,"
    "MILO_MAX_RUN_DURATION_SECONDS=3300,MILO_MAX_RETRIES=15,MILO_MAX_AGENT_STEPS=60,"
    "MILO_MAX_CONCURRENT_RUNS_PER_USER=1,MILO_MAX_CONCURRENT_RUNS_PER_PROJECT=1,"
    "MILO_DAILY_USER_BUDGET=5.00,MILO_DAILY_PROJECT_BUDGET=5.00,MILO_ESTIMATED_COST_PER_CALL=0.02"
)


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def gw(monkeypatch):
    monkeypatch.setenv("STAGE_C_API_URL", "https://api.invalid")
    monkeypatch.setenv("STAGE_C_USER_ID", "user-1")
    monkeypatch.setenv("STAGE_C_CONVERSATION_ID", "conv-1")
    monkeypatch.setenv("STAGE_C_RUN_ID", "run-1")
    monkeypatch.setenv("STAGE_C_POLL_SECONDS", "5")
    monkeypatch.setenv("STAGE_C_POLL_INTERVAL_SECONDS", "0")
    return load_module("stage_c_probe_gateway", STAGE_C / "probe_gateway.py")


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://db.invalid")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "offline-placeholder")
    return load_module("stage_c_probe_db", STAGE_C / "probe_db.py")


def run_status(run_state: str) -> tuple[int, dict]:
    return 200, {"status": run_state, "attempt": 1, "usage": {}}


# ---------------------------------------------------------------------------
# poll: PASS terminal states vs FAIL terminal states
# ---------------------------------------------------------------------------


def test_poll_completed_is_pass(gw, monkeypatch):
    monkeypatch.setattr(gw, "call", lambda *a, **k: run_status("completed"))
    gw.poll()  # returns without SystemExit


@pytest.mark.parametrize("state", ["failed", "cancelled", "timed_out", "budget_exhausted", "partial_success"])
def test_poll_unacceptable_terminal_states_exit_nonzero(gw, monkeypatch, capsys, state):
    monkeypatch.setattr(gw, "call", lambda *a, **k: run_status(state))
    with pytest.raises(SystemExit) as excinfo:
        gw.poll()
    assert excinfo.value.code == 2
    output = capsys.readouterr().out
    assert '"acceptable": false' in output
    assert "kill-switch.sh" in output  # operator is told to run the kill switch


def test_poll_policy_can_explicitly_widen_acceptable_states(gw, monkeypatch):
    monkeypatch.setenv("STAGE_C_ACCEPTABLE_TERMINAL_STATES", "completed,partial_success")
    monkeypatch.setattr(gw, "call", lambda *a, **k: run_status("partial_success"))
    gw.poll()


def test_poll_policy_ignores_states_outside_the_terminal_set(gw, monkeypatch):
    monkeypatch.setenv("STAGE_C_ACCEPTABLE_TERMINAL_STATES", "running,anything")
    assert gw.acceptable_terminal_states() == set()


def test_poll_timeout_exits_nonzero_with_kill_switch_instruction(gw, monkeypatch, capsys):
    monkeypatch.setenv("STAGE_C_POLL_SECONDS", "0")
    monkeypatch.setattr(gw, "call", lambda *a, **k: run_status("running"))
    with pytest.raises(SystemExit) as excinfo:
        gw.poll()
    assert excinfo.value.code == 1
    assert "kill-switch.sh" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# DB preflight: real exact count, fail closed
# ---------------------------------------------------------------------------


def test_parse_content_range_total(db):
    assert db.parse_content_range_total("0-0/17") == 17
    assert db.parse_content_range_total("*/0") == 0
    assert db.parse_content_range_total("0-0/*") is None  # count not computed → fail closed
    assert db.parse_content_range_total(None) is None
    assert db.parse_content_range_total("garbage") is None


def preflight_with(db, monkeypatch, count, expected="0", key_count=0):
    monkeypatch.setenv("STAGE_C_EXPECTED_PRIOR_RUNS", expected)
    monkeypatch.setenv("STAGE_C_IDEMPOTENCY_KEY", "stage-c-smoke-0001")
    monkeypatch.setattr(db, "call", lambda *a, **k: (200, []))

    def fake_count(path):
        return key_count if "idempotency_key" in path else count

    monkeypatch.setattr(db, "count_exact", fake_count)
    db.preflight()


def test_preflight_passes_on_exact_expected_count(db, monkeypatch, capsys):
    preflight_with(db, monkeypatch, count=0)
    assert '"ok": true' in capsys.readouterr().out


def test_preflight_fails_closed_when_count_unavailable(db, monkeypatch):
    with pytest.raises(SystemExit):
        preflight_with(db, monkeypatch, count=None)


def test_preflight_fails_on_any_preexisting_run(db, monkeypatch):
    with pytest.raises(SystemExit):
        preflight_with(db, monkeypatch, count=1)


def test_preflight_fails_on_preexisting_stage_c_key_run(db, monkeypatch):
    with pytest.raises(SystemExit):
        preflight_with(db, monkeypatch, count=3, expected="3", key_count=1)


def test_preflight_uses_exact_count_not_page_length(db):
    """The zero-run precondition must come from count_exact, never len(rows)."""
    source = (STAGE_C / "probe_db.py").read_text()
    assert "len(rows or [])" not in source


# ---------------------------------------------------------------------------
# evidence: executable acceptance gate
# ---------------------------------------------------------------------------


def happy_dataset():
    run = {
        "id": "run-1",
        "status": "completed",
        "attempt": 1,
        "worker_id": "worker-abc",
        "launch_state": "launched",
        "started_at": "2026-08-13T00:00:00Z",
        "finished_at": "2026-08-13T00:10:00Z",
        "last_heartbeat_at": "2026-08-13T00:09:50Z",
        "usage": {"model_calls": 20, "input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500, "actual_cost": 0.02},
        "idempotency_key": "stage-c-smoke-0001",
    }
    return {
        "runs": [run],
        "run_events": [{"event_type": "run_created", "payload": {}}, {"event_type": "run_completed", "payload": {}}],
        "run_checkpoints": [{"phase": "discovery"}],
        "worker_heartbeats": [{"worker_id": "worker-abc", "attempt": 1, "heartbeat_at": "2026-08-13T00:09:50Z"}],
        "model_call_budget_reservations": [
            {"call_seq": i, "attempt": 1, "status": "settled", "estimated_cost": 0.02, "actual_cost": 0.001}
            for i in range(20)
        ],
        "run_usage_ledger": [
            {"decision": "settled", "call_seq": i, "actual_input_tokens": 50, "actual_output_tokens": 25, "actual_cost": 0.001, "estimated_cost": 0.02}
            for i in range(20)
        ],
        "run_invocations": [{"created_at": "2026-08-13T00:00:01Z", "invocation": {}}],
    }


def wire_evidence(db, monkeypatch, data, total_runs=1):
    monkeypatch.setenv("STAGE_C_RUN_ID", "run-1")
    monkeypatch.setenv("STAGE_C_CAPS", CAPS)
    monkeypatch.setenv("STAGE_C_EXPECTED_TERMINAL_STATES", "completed")
    monkeypatch.setenv("STAGE_C_IDEMPOTENCY_KEY", "stage-c-smoke-0001")

    def fake_call(method, path, body=None, headers=None):
        for table in ("runs", "run_events", "run_checkpoints", "worker_heartbeats", "model_call_budget_reservations", "run_usage_ledger", "run_invocations"):
            if f"/rest/v1/{table}?" in path:
                return 200, data[table]
        return 200, []

    monkeypatch.setattr(db, "call", fake_call)
    monkeypatch.setattr(db, "count_exact", lambda path: total_runs)


def gate_output(db, capsys):
    for line in capsys.readouterr().out.splitlines():
        record = json.loads(line)
        if record.get("stage_c_probe") == "evidence" and "ok" in record:
            return record
    raise AssertionError("no evidence verdict printed")


def test_evidence_gate_passes_on_consistent_run(db, monkeypatch, capsys):
    wire_evidence(db, monkeypatch, happy_dataset())
    db.evidence()
    verdict = gate_output(db, capsys)
    assert verdict["ok"] is True
    assert verdict["failures"] == []


def evidence_must_fail(db, monkeypatch, capsys, data, total_runs=1):
    wire_evidence(db, monkeypatch, data, total_runs=total_runs)
    with pytest.raises(SystemExit) as excinfo:
        db.evidence()
    assert excinfo.value.code == 1
    verdict = gate_output(db, capsys)
    assert verdict["ok"] is False
    assert verdict["failures"]
    assert "kill" in verdict["operator_action"].lower()
    return verdict


def test_evidence_fails_on_second_run(db, monkeypatch, capsys):
    evidence_must_fail(db, monkeypatch, capsys, happy_dataset(), total_runs=2)


def test_evidence_fails_closed_when_run_count_unavailable(db, monkeypatch, capsys):
    evidence_must_fail(db, monkeypatch, capsys, happy_dataset(), total_runs=None)


def test_evidence_fails_on_unacceptable_terminal_state(db, monkeypatch, capsys):
    data = happy_dataset()
    data["runs"][0]["status"] = "budget_exhausted"
    evidence_must_fail(db, monkeypatch, capsys, data)


def test_evidence_fails_on_dangling_reservation(db, monkeypatch, capsys):
    data = happy_dataset()
    data["model_call_budget_reservations"][0]["status"] = "reserved"
    evidence_must_fail(db, monkeypatch, capsys, data)


def test_evidence_fails_on_cost_over_cap(db, monkeypatch, capsys):
    data = happy_dataset()
    for row in data["run_usage_ledger"]:
        row["actual_cost"] = 0.5  # 20 × 0.5 = $10 > $3.00 cap
    for row in data["model_call_budget_reservations"]:
        row["actual_cost"] = 0.5
    data["runs"][0]["usage"]["actual_cost"] = 10.0
    verdict = evidence_must_fail(db, monkeypatch, capsys, data)
    assert any("exceeds cap" in f for f in verdict["failures"])


def test_evidence_fails_on_token_cap_violation(db, monkeypatch, capsys):
    data = happy_dataset()
    for row in data["run_usage_ledger"]:
        row["actual_input_tokens"] = 40_000  # 20 × 40k = 800k > 700k input cap
    data["runs"][0]["usage"]["input_tokens"] = 800_000
    evidence_must_fail(db, monkeypatch, capsys, data)


def test_evidence_fails_on_accounting_mismatch(db, monkeypatch, capsys):
    data = happy_dataset()
    data["runs"][0]["usage"]["actual_cost"] = 0.9  # ledger says 0.02
    verdict = evidence_must_fail(db, monkeypatch, capsys, data)
    assert any("ledger cost" in f for f in verdict["failures"])


def test_evidence_fails_on_attempt_or_heartbeat_invariants(db, monkeypatch, capsys):
    data = happy_dataset()
    data["runs"][0]["attempt"] = 2
    data["worker_heartbeats"] = []
    verdict = evidence_must_fail(db, monkeypatch, capsys, data)
    assert any("attempt" in f for f in verdict["failures"])
    assert any("heartbeat" in f for f in verdict["failures"])


def test_evidence_fails_on_secret_marker_hit(db, monkeypatch, capsys):
    data = happy_dataset()
    data["run_events"].append({"event_type": "model_call", "payload": {"note": "sk-LEAKED"}})
    evidence_must_fail(db, monkeypatch, capsys, data)


def test_evidence_fails_on_wrong_idempotency_key(db, monkeypatch, capsys):
    data = happy_dataset()
    data["runs"][0]["idempotency_key"] = "someone-elses-run"
    evidence_must_fail(db, monkeypatch, capsys, data)


def test_evidence_fails_closed_without_expected_caps(db, monkeypatch, capsys):
    wire_evidence(db, monkeypatch, happy_dataset())
    monkeypatch.setenv("STAGE_C_CAPS", "")
    with pytest.raises(SystemExit):
        db.evidence()


def test_evidence_fails_on_duplicate_launch_invocation(db, monkeypatch, capsys):
    data = happy_dataset()
    data["run_invocations"].append({"created_at": "2026-08-13T00:00:02Z", "invocation": {}})
    evidence_must_fail(db, monkeypatch, capsys, data)


# ---------------------------------------------------------------------------
# verify_caps.py: exact cap verification on both surfaces
# ---------------------------------------------------------------------------


RELEASE_SHA = "30b05bc45d6f9372261e4fac20cd983c69db971f"
REGISTRY = "us-central1-docker.pkg.dev/big-cabinet-457321-t7/milo-agent"


def caps_env_list(**overrides):
    env = dict(pair.split("=", 1) for pair in CAPS.split(","))
    env.update(overrides)
    return env


def worker_spec(env=None, image=None, bind_key=True):
    entries = [{"name": k, "value": v} for k, v in (env or caps_env_list(MILO_ENABLE_PAID_EXECUTION="tru" + "e")).items()]
    if bind_key:
        entries.append({"name": "KIMI_API_KEY", "valueFrom": {"secretKeyRef": {"name": "KIMI_API_KEY"}}})
    return {"spec": {"template": {"spec": {"template": {"spec": {"containers": [
        {"image": image or f"{REGISTRY}/worker:{RELEASE_SHA}", "env": entries}
    ]}}}}}}


def api_spec(env=None, image=None):
    defaults = caps_env_list(
        MILO_ENABLE_PAID_EXECUTION="false",
        MILO_ENABLE_RUN_CREATION="tru" + "e",
        JOB_LAUNCHER="cloud_run",
        MILO_ENABLE_PROPOSAL_MUTATIONS="false",
        MILO_ENABLE_PROPOSAL_READS="false",
        MILO_ENABLE_RUN_CANCELLATION="false",
        MILO_ENABLE_EXECUTION_CONTROL="false",
    )
    entries = [{"name": k, "value": v} for k, v in (env or defaults).items()]
    return {"spec": {"template": {"spec": {"containers": [
        {"image": image or f"{REGISTRY}/api:{RELEASE_SHA}", "env": entries}
    ]}}}}


def run_verify_caps(tmp_path, worker, api, caps=CAPS):
    worker_path = tmp_path / "worker.json"
    api_path = tmp_path / "api.json"
    worker_path.write_text(json.dumps(worker))
    api_path.write_text(json.dumps(api))
    return subprocess.run(
        [sys.executable, str(STAGE_C / "verify_caps.py"), "--worker-json", str(worker_path), "--api-json", str(api_path)],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "STAGE_C_CAPS": caps, "STAGE_C_REGISTRY": REGISTRY, "STAGE_C_RELEASE_SHA": RELEASE_SHA},
        timeout=60,
    )


def test_verify_caps_passes_on_exact_posture(tmp_path):
    result = run_verify_caps(tmp_path, worker_spec(), api_spec())
    assert result.returncode == 0, result.stdout + result.stderr


def test_verify_caps_fails_on_missing_cap(tmp_path):
    env = caps_env_list(MILO_ENABLE_PAID_EXECUTION="tru" + "e")
    del env["MILO_MAX_COST_PER_RUN"]
    result = run_verify_caps(tmp_path, worker_spec(env=env), api_spec())
    assert result.returncode != 0
    assert "MILO_MAX_COST_PER_RUN is MISSING" in result.stdout


def test_verify_caps_fails_on_changed_value_on_either_surface(tmp_path):
    loosened_worker = caps_env_list(MILO_ENABLE_PAID_EXECUTION="tru" + "e", MILO_MAX_COST_PER_RUN="30.00")
    result = run_verify_caps(tmp_path, worker_spec(env=loosened_worker), api_spec())
    assert result.returncode != 0
    assert "differs from expected" in result.stdout

    loosened_api = api_spec()
    loosened_api["spec"]["template"]["spec"]["containers"][0]["env"] = [
        {"name": k, "value": ("999" if k == "MILO_MAX_MODEL_CALLS_PER_RUN" else v)}
        for e in [None]
        for k, v in caps_env_list(
            MILO_ENABLE_PAID_EXECUTION="false",
            MILO_ENABLE_RUN_CREATION="tru" + "e",
            JOB_LAUNCHER="cloud_run",
            MILO_ENABLE_PROPOSAL_MUTATIONS="false",
            MILO_ENABLE_PROPOSAL_READS="false",
            MILO_ENABLE_RUN_CANCELLATION="false",
            MILO_ENABLE_EXECUTION_CONTROL="false",
        ).items()
    ]
    result = run_verify_caps(tmp_path, worker_spec(), loosened_api)
    assert result.returncode != 0
    assert "api: cap MILO_MAX_MODEL_CALLS_PER_RUN" in result.stdout


def test_verify_caps_fails_on_unexpected_extra_budget_variable(tmp_path):
    env = caps_env_list(MILO_ENABLE_PAID_EXECUTION="tru" + "e", MILO_MAX_SNEAKY_NEW_CAP="1000000")
    result = run_verify_caps(tmp_path, worker_spec(env=env), api_spec())
    assert result.returncode != 0
    assert "unexpected budget/cap variable" in result.stdout


def test_verify_caps_enforces_release_images(tmp_path):
    result = run_verify_caps(tmp_path, worker_spec(image=f"{REGISTRY}/worker:791f7af9deadbeef"), api_spec())
    assert result.returncode != 0
    assert "signed-off release" in result.stdout


def test_verify_caps_fails_if_api_carries_paid_flag_or_key(tmp_path):
    bad_api = api_spec()
    for entry in bad_api["spec"]["template"]["spec"]["containers"][0]["env"]:
        if entry["name"] == "MILO_ENABLE_PAID_EXECUTION":
            entry["value"] = "tru" + "e"
    bad_api["spec"]["template"]["spec"]["containers"][0]["env"].append(
        {"name": "KIMI_API_KEY", "valueFrom": {"secretKeyRef": {"name": "KIMI_API_KEY"}}}
    )
    result = run_verify_caps(tmp_path, worker_spec(), bad_api)
    assert result.returncode != 0
    assert "must stay false" in result.stdout
    assert "NEVER be bound to the API" in result.stdout


def test_verify_caps_fails_closed_without_expected_caps(tmp_path):
    result = run_verify_caps(tmp_path, worker_spec(), api_spec(), caps="")
    assert result.returncode != 0
    assert "failing closed" in result.stdout


# ---------------------------------------------------------------------------
# shell steps: structured gates, no warning-only checks
# ---------------------------------------------------------------------------


def test_execute_smoke_gates_on_acceptable_terminal_and_exact_caps():
    text = (STAGE_C / "05-execute-smoke.sh").read_text()
    assert "verify_caps.py" in text
    assert "STAGE_C_ACCEPTABLE_TERMINAL_STATES" in text
    assert "kill-switch.sh" in text
    assert 'record.get("acceptable") is True' in text


def test_collect_evidence_is_a_gate_not_a_checklist():
    text = (STAGE_C / "06-collect-evidence.sh").read_text()
    assert "ACCEPTANCE GATE" in text
    assert 'test "${executions}" = "1"' in text
    assert 'test "${hits}" = "0"' in text
    assert "WARNING: possible secret markers" not in text
    assert "Validation checklist to record" not in text  # replaced by executable checks


def test_env_defines_single_pass_terminal_state_default():
    text = (STAGE_C / "stage-c-env.sh").read_text()
    assert 'STAGE_C_ACCEPTABLE_TERMINAL_STATES:-completed' in text
    assert 'STAGE_C_EXPECTED_PRIOR_RUNS:-0' in text


def test_acceptance_doc_does_not_call_three_dollars_a_hard_billing_ceiling():
    text = (REPO / "docs" / "production-readiness" / "STAGE_C_ACCEPTANCE.md").read_text()
    assert "NOT a hard provider-billing ceiling" in text
    assert "separately from tokens" in text
    # The production-image blocker must stay recorded.
    assert "Blocking deployment-consistency finding" in text

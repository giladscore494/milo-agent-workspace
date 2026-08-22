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
import shutil
import subprocess
import sys
from decimal import Decimal
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

# The pinned Attempt 7 run identity and baselines. Attempts 5+6 occurred
# historically, but public.runs currently holds exactly 1 row — Attempt
# 6's (37912575-…); Attempt 5's row (58daa7de-…) is absent and the
# reason for its absence is unverified. 2 Worker executions historically
# occurred (Attempt 5 milo-agent-worker-d2gfx, Attempt 6
# milo-agent-worker-mcfrx), but Attempt 5's was explicitly deleted at
# 2026-08-18T02:00:48Z (google.cloud.run.v1.Executions.DeleteExecution,
# Cloud Audit Logs), so Cloud Run currently exposes exactly 1 visible
# terminal execution. On both surfaces the executable live baseline is
# what production actually holds: 1 run row, 1 visible execution.
ATTEMPT7_KEY = "stage-c-smoke-attempt-7-20260819"
CONSUMED_KEY = "stage-c-smoke-0001"  # Attempt 5/6 identity — never reused
EXPECTED_PRIOR_RUNS = "1"
EXPECTED_PRIOR_EXECUTIONS = "1"

# The exact seven pinned worker-only provider settings (Tier 2 confirmed;
# envelope deliberately below the account ceiling; concurrency stays 2).
PROVIDER_LIMITS = (
    "MILO_PROVIDER_MAX_CONCURRENCY=2,MILO_PROVIDER_RPM_LIMIT=350,"
    "MILO_PROVIDER_TPM_LIMIT=2400000,MILO_PROVIDER_MAX_RATE_LIMIT_RETRIES=5,"
    "MILO_PROVIDER_MAX_BACKPRESSURE_WAIT_SECONDS=240,"
    "MILO_PROVIDER_BACKOFF_BASE_SECONDS=2,MILO_PROVIDER_BACKOFF_MAX_SECONDS=30"
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


def openapi_spec(db, drop_rpc=None, drop_arg_from=None, mangle=None):
    """A PostgREST-shaped Swagger 2.0 document advertising the Stage C RPCs."""
    paths = {}
    for rpc, args in db.REQUIRED_RPC_ARGS.items():
        if rpc == drop_rpc:
            continue
        advertised = set(args)
        if rpc == drop_arg_from:
            advertised.discard(sorted(args)[0])
        paths[f"/rpc/{rpc}"] = {"post": {"parameters": [
            {"in": "body", "name": "args", "schema": {"type": "object", "properties": {a: {"type": "string"} for a in sorted(advertised)}}}
        ]}}
    spec = {"swagger": "2.0", "paths": paths}
    if mangle == "paths":
        spec["paths"] = "not-a-dict"
    elif mangle == "no_body_schema":
        for entry in paths.values():
            entry["post"]["parameters"] = [{"in": "body", "name": "args"}]
    return spec


def preflight_with(db, monkeypatch, count, expected=EXPECTED_PRIOR_RUNS, key_count=0, spec=None, root_status=200, record=None, key=ATTEMPT7_KEY):
    if expected is None:
        monkeypatch.delenv("STAGE_C_EXPECTED_PRIOR_RUNS", raising=False)
    else:
        monkeypatch.setenv("STAGE_C_EXPECTED_PRIOR_RUNS", expected)
    if key is None:
        monkeypatch.delenv("STAGE_C_IDEMPOTENCY_KEY", raising=False)
    else:
        monkeypatch.setenv("STAGE_C_IDEMPOTENCY_KEY", key)
    resolved_spec = openapi_spec(db) if spec is None else spec

    def fake_call(method, path, body=None, headers=None):
        if record is not None:
            record.append((method, path))
        if path == "/rest/v1/":
            return root_status, resolved_spec
        if method == "POST" and "/rest/v1/rpc/" in path:
            # A real parameterized RPC answers a bodyless probe with a
            # function-resolution 404 (PGRST202) even though it EXISTS.
            return 404, {"code": "PGRST202", "message": "Could not find the function"}
        return 200, []

    monkeypatch.setattr(db, "call", fake_call)

    def fake_count(path):
        return key_count if "idempotency_key" in path else count

    monkeypatch.setattr(db, "count_exact", fake_count)
    db.preflight()


def test_preflight_passes_on_exact_pinned_baseline(db, monkeypatch, capsys):
    """Exactly 1 prior run row (Attempt 6's — Attempt 5's row is absent
    from production for an unverified reason) and 0 Attempt 7 key rows is
    the ONLY acceptable pre-run state."""
    preflight_with(db, monkeypatch, count=1)
    out = capsys.readouterr().out
    assert '"ok": true' in out
    assert '"expected_prior_runs": "1"' in out


def test_preflight_fails_closed_when_count_unavailable(db, monkeypatch):
    with pytest.raises(SystemExit):
        preflight_with(db, monkeypatch, count=None)


@pytest.mark.parametrize("count", [0, 2, 3])
def test_preflight_fails_on_any_count_other_than_the_pinned_baseline(db, monkeypatch, count):
    """Fewer rows than the baseline (Attempt 6's row vanished too?) fails
    exactly like more rows (an unexpected row — including the superseded
    2-row expectation and an Attempt 5-style row resurfacing) — the count
    must be exact."""
    with pytest.raises(SystemExit):
        preflight_with(db, monkeypatch, count=count)


def test_preflight_fails_on_preexisting_attempt_7_key_run(db, monkeypatch, capsys):
    with pytest.raises(SystemExit):
        preflight_with(db, monkeypatch, count=1, key_count=1)
    assert ATTEMPT7_KEY in capsys.readouterr().out


@pytest.mark.parametrize("expected", [None, "", "two", "-1", "2.0"])
def test_preflight_fails_closed_on_missing_or_invalid_baseline_config(db, monkeypatch, capsys, expected):
    with pytest.raises(SystemExit):
        preflight_with(db, monkeypatch, count=1, expected=expected)
    assert "STAGE_C_EXPECTED_PRIOR_RUNS" in capsys.readouterr().out


def test_preflight_fails_closed_without_idempotency_key(db, monkeypatch, capsys):
    with pytest.raises(SystemExit):
        preflight_with(db, monkeypatch, count=1, key=None)
    assert "STAGE_C_IDEMPOTENCY_KEY" in capsys.readouterr().out


def test_preflight_uses_exact_count_not_page_length(db):
    """The zero-run precondition must come from count_exact, never len(rows)."""
    source = (STAGE_C / "probe_db.py").read_text()
    assert "len(rows or [])" not in source


# ---------------------------------------------------------------------------
# RPC surface introspection: non-mutating and signature-safe
# (Stage C attempt 2 falsely classified existing parameterized RPCs as
# MISSING because POST {} gets a function-resolution 404 from PostgREST)
# ---------------------------------------------------------------------------


def test_preflight_passes_when_all_rpc_metadata_present(db, monkeypatch, capsys):
    preflight_with(db, monkeypatch, count=1)
    out = capsys.readouterr().out
    assert '"ok": true' in out
    for rpc in db.REQUIRED_RPC_ARGS:
        assert f'"rpc_{rpc}": "present"' in out


def test_preflight_fails_when_one_rpc_genuinely_absent(db, monkeypatch, capsys):
    spec = openapi_spec(db, drop_rpc="heartbeat_run_guarded")
    with pytest.raises(SystemExit):
        preflight_with(db, monkeypatch, count=1, spec=spec)
    out = capsys.readouterr().out
    assert '"rpc_heartbeat_run_guarded": "MISSING"' in out
    # The others are still individually verified as present.
    assert '"rpc_create_message_and_run_v2": "present"' in out


def test_preflight_fails_closed_when_metadata_endpoint_unavailable(db, monkeypatch, capsys):
    with pytest.raises(SystemExit):
        preflight_with(db, monkeypatch, count=1, root_status=503, spec={"error": "unavailable"})
    out = capsys.readouterr().out
    assert "failing closed" in out
    assert '"UNVERIFIED"' in out
    assert '"present"' not in out.split('"reservations_attempt_column"')[0]  # no rpc assumed present


@pytest.mark.parametrize("mangle", ["paths", "no_body_schema"])
def test_preflight_fails_closed_on_malformed_metadata(db, monkeypatch, capsys, mangle):
    spec = openapi_spec(db, mangle=mangle)
    with pytest.raises(SystemExit):
        preflight_with(db, monkeypatch, count=1, spec=spec)
    assert "failing closed" in capsys.readouterr().out


def test_existing_rpc_answering_404_to_empty_post_is_not_classified_missing(db, monkeypatch, capsys):
    """Attempt-2 regression: the function EXISTS but a bodyless POST would
    get a signature-mismatch 404. The fake call() in preflight_with answers
    exactly that 404 for every RPC POST — the new implementation must
    classify every RPC present (from metadata) without ever noticing."""
    preflight_with(db, monkeypatch, count=1)
    out = capsys.readouterr().out
    assert '"ok": true' in out
    assert '"MISSING"' not in out


def test_preflight_rpc_checks_perform_no_mutating_posts(db, monkeypatch):
    calls = []
    preflight_with(db, monkeypatch, count=1, record=calls)
    posts = [(m, p) for m, p in calls if m != "GET"]
    assert posts == [], f"preflight made non-GET calls: {posts}"
    assert not any("/rest/v1/rpc/" in p for _, p in calls), "preflight touched an RPC endpoint"


def test_preflight_signature_mismatch_fails_closed(db, monkeypatch, capsys):
    spec = openapi_spec(db, drop_arg_from="update_run_usage_guarded")
    with pytest.raises(SystemExit):
        preflight_with(db, monkeypatch, count=1, spec=spec)
    out = capsys.readouterr().out
    assert '"rpc_update_run_usage_guarded": "SIGNATURE_MISMATCH"' in out


def test_preflight_baseline_checks_still_enforced_alongside_rpc_metadata(db, monkeypatch, capsys):
    """RPC metadata all present must not mask an off-baseline run count."""
    with pytest.raises(SystemExit):
        preflight_with(db, monkeypatch, count=2)
    out = capsys.readouterr().out
    assert '"existing_runs": "2"' in out
    with pytest.raises(SystemExit):
        preflight_with(db, monkeypatch, count=1, key_count=1)


def test_required_rpc_args_match_release_migrations(db):
    """The expected argument names are pinned to the migration definitions."""
    migrations = REPO / "supabase" / "migrations"
    corrective = (migrations / "20260810000600_corrective_lease_and_attempt_hardening.sql").read_text()
    setof = (migrations / "20260810000400_setof_rpc_returns.sql").read_text()
    for rpc, args in db.REQUIRED_RPC_ARGS.items():
        source = setof if rpc == "create_message_and_run_v2" else corrective
        for arg in args:
            assert arg in source, f"{rpc}: expected arg {arg} not found in the release migration"


def test_setof_wrapper_base_rpc_acl_dependencies_are_pinned():
    """SECURITY INVOKER wrappers require service_role EXECUTE on their base RPCs."""
    migrations = REPO / "supabase" / "migrations"
    setof = (migrations / "20260810000400_setof_rpc_returns.sql").read_text()
    acl = (migrations / "20260818000100_stage_c_service_role_rpc_acl.sql").read_text()

    dependencies = {
        "create_message_and_run_v2": (
            "public.create_message_and_run(",
            "public.create_message_and_run(uuid, text, jsonb, uuid, text, text, integer, integer)",
        ),
        "create_project_from_proposal_with_owner_v2": (
            "public.create_project_from_proposal_with_owner(",
            "public.create_project_from_proposal_with_owner(uuid, text, text, text, jsonb, uuid)",
        ),
    }

    assert "SECURITY DEFINER" not in setof.upper()

    for wrapper, (base_call, signature) in dependencies.items():
        assert f"function public.{wrapper}(" in setof
        assert f"return next {base_call}" in setof
        assert signature in acl
        assert "grant execute on function %s to service_role" in acl


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
        "idempotency_key": ATTEMPT7_KEY,
    }
    return {
        "runs": [run],
        "run_events": [{"event_type": "run_created", "payload": {}}, {"event_type": "run_completed", "payload": {}}],
        "run_checkpoints": [{"phase": "discovery"}],
        "worker_heartbeats": [{"worker_id": "worker-abc", "attempt": 1, "heartbeat_at": "2026-08-13T00:09:50Z"}],
        "model_call_budget_reservations": [
            {"call_seq": i, "attempt": 1, "status": "settled", "estimated_cost": 0.02, "actual_cost": 0.001}
            for i in range(1, 21)
        ],
        "run_usage_ledger": [
            {"decision": "settled", "call_seq": i, "actual_input_tokens": 50, "actual_output_tokens": 25, "actual_cost": 0.001, "estimated_cost": 0.02}
            for i in range(1, 21)
        ],
        # Real migration-006 columns (launcher/execution_name/created_at) —
        # the table has NO `invocation` column.
        "run_invocations": [{"created_at": "2026-08-13T00:00:01Z", "launcher": "cloud_run", "execution_name": "milo-agent-worker-gggdc"}],
    }


def wire_evidence(db, monkeypatch, data, total_runs=2, key_runs=1, prior=EXPECTED_PRIOR_RUNS, errors=None):
    monkeypatch.setenv("STAGE_C_RUN_ID", "run-1")
    monkeypatch.setenv("STAGE_C_CAPS", CAPS)
    monkeypatch.setenv("STAGE_C_EXPECTED_TERMINAL_STATES", "completed")
    monkeypatch.setenv("STAGE_C_IDEMPOTENCY_KEY", ATTEMPT7_KEY)
    if prior is None:
        monkeypatch.delenv("STAGE_C_EXPECTED_PRIOR_RUNS", raising=False)
    else:
        monkeypatch.setenv("STAGE_C_EXPECTED_PRIOR_RUNS", prior)

    def fake_call(method, path, body=None, headers=None):
        for table in ("runs", "run_events", "run_checkpoints", "worker_heartbeats", "model_call_budget_reservations", "run_usage_ledger", "run_invocations"):
            if f"/rest/v1/{table}?" in path:
                if errors and table in errors:
                    return errors[table]
                return 200, data[table]
        return 200, []

    monkeypatch.setattr(db, "call", fake_call)

    def fake_count(path):
        return key_runs if "idempotency_key" in path else total_runs

    monkeypatch.setattr(db, "count_exact", fake_count)


def gate_output(db, capsys):
    for line in capsys.readouterr().out.splitlines():
        record = json.loads(line)
        if record.get("stage_c_probe") == "evidence" and "ok" in record:
            return record
    raise AssertionError("no evidence verdict printed")


def test_evidence_gate_passes_on_exactly_one_new_run_over_the_baseline(db, monkeypatch, capsys):
    """2 total runs (pinned baseline 1 + the one authorized Attempt 7 run,
    which carries the fresh key) passes the count gate."""
    wire_evidence(db, monkeypatch, happy_dataset())
    db.evidence()
    verdict = gate_output(db, capsys)
    assert verdict["ok"] is True
    assert verdict["failures"] == []
    assert verdict["total_runs"] == 2
    assert verdict["runs_with_current_key"] == 1


def evidence_must_fail(db, monkeypatch, capsys, data, total_runs=2, key_runs=1, prior=EXPECTED_PRIOR_RUNS, errors=None):
    wire_evidence(db, monkeypatch, data, total_runs=total_runs, key_runs=key_runs, prior=prior, errors=errors)
    with pytest.raises(SystemExit) as excinfo:
        db.evidence()
    assert excinfo.value.code == 1
    verdict = gate_output(db, capsys)
    assert verdict["ok"] is False
    assert verdict["failures"]
    assert "kill" in verdict["operator_action"].lower()
    return verdict


@pytest.mark.parametrize("total_runs", [1, 3, 4])
def test_evidence_fails_unless_exactly_one_run_was_added(db, monkeypatch, capsys, total_runs):
    """1 total = the new run never landed; 3 or more = extra new run(s)
    slipped in. Every total other than 2 fails the exact-increment gate."""
    verdict = evidence_must_fail(db, monkeypatch, capsys, happy_dataset(), total_runs=total_runs)
    assert any("expected exactly 2" in f for f in verdict["failures"])


@pytest.mark.parametrize("key_runs", [0, 2])
def test_evidence_fails_unless_exactly_one_run_carries_the_attempt_7_key(db, monkeypatch, capsys, key_runs):
    verdict = evidence_must_fail(db, monkeypatch, capsys, happy_dataset(), key_runs=key_runs)
    assert any("authorized idempotency key" in f for f in verdict["failures"])


@pytest.mark.parametrize("prior", [None, "", "two"])
def test_evidence_fails_closed_on_missing_or_invalid_baseline(db, monkeypatch, capsys, prior):
    verdict = evidence_must_fail(db, monkeypatch, capsys, happy_dataset(), prior=prior)
    assert any("STAGE_C_EXPECTED_PRIOR_RUNS" in f for f in verdict["failures"])


def test_evidence_fails_closed_when_run_count_unavailable(db, monkeypatch, capsys):
    evidence_must_fail(db, monkeypatch, capsys, happy_dataset(), total_runs=None)


def test_historical_failed_run_cannot_satisfy_the_attempt_7_acceptance(db, monkeypatch, capsys):
    """A pre-existing Attempt 6-style row (consumed key, terminal `failed`)
    fails on BOTH the key attribution and the terminal-state policy even
    when the totals look right."""
    data = happy_dataset()
    data["runs"][0]["idempotency_key"] = CONSUMED_KEY
    data["runs"][0]["status"] = "failed"
    verdict = evidence_must_fail(db, monkeypatch, capsys, data)
    assert any("historical run cannot satisfy" in f for f in verdict["failures"])
    assert any("not in acceptance policy" in f for f in verdict["failures"])


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
    data["runs"][0]["usage"]["actual_cost"] = 0.9  # reservations say 0.02
    verdict = evidence_must_fail(db, monkeypatch, capsys, data)
    assert any("unrounded reservation total" in f for f in verdict["failures"])


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
    data["run_invocations"].append({"created_at": "2026-08-13T00:00:02Z", "launcher": "cloud_run", "execution_name": "milo-agent-worker-zzzzz"})
    evidence_must_fail(db, monkeypatch, capsys, data)


# ---------------------------------------------------------------------------
# Attempt 7 corrective — defect 1: run_invocations schema + PostgREST error
# shapes must fail closed, never masquerade as row collections
# ---------------------------------------------------------------------------


# The real PostgREST answer to the old `select=created_at,invocation` query:
# migration 006 defines launcher/execution_name/payload/created_at — there
# is NO `invocation` column. len() of this dict is 4, which the old gate
# reported as "run_invocations=4, expected exactly 1".
PGRST_MISSING_COLUMN_ERROR = (400, {
    "code": "42703",
    "details": None,
    "hint": None,
    "message": "column run_invocations.invocation does not exist",
})


def test_evidence_queries_only_real_run_invocations_columns(db):
    source = (STAGE_C / "probe_db.py").read_text()
    assert "run_invocations?run_id=eq.{run_id}&select=launcher,execution_name,created_at" in source
    assert "select=created_at,invocation" not in source


def test_evidence_fails_closed_on_run_invocations_postgrest_error(db, monkeypatch, capsys):
    verdict = evidence_must_fail(
        db, monkeypatch, capsys, happy_dataset(),
        errors={"run_invocations": PGRST_MISSING_COLUMN_ERROR},
    )
    assert any("fail-closed: run_invocations query" in f and "42703" in f for f in verdict["failures"])
    # The error dict must never be counted as rows (the old defect produced
    # a bogus "run_invocations=4" from len() of the 4-key error object).
    assert not any(f.startswith("run_invocations=") for f in verdict["failures"])
    assert "invocations" not in verdict


@pytest.mark.parametrize("table", ["run_usage_ledger", "model_call_budget_reservations", "run_events", "worker_heartbeats"])
def test_evidence_fails_closed_on_any_postgrest_error_shape(db, monkeypatch, capsys, table):
    verdict = evidence_must_fail(
        db, monkeypatch, capsys, happy_dataset(),
        errors={table: (500, {"message": "canceling statement due to statement timeout"})},
    )
    assert any(f"fail-closed: {table} query" in f for f in verdict["failures"])


def test_evidence_fails_closed_on_non_list_success_body(db, monkeypatch, capsys):
    """Even an HTTP 200 body that is not a row list must never be counted."""
    verdict = evidence_must_fail(
        db, monkeypatch, capsys, happy_dataset(),
        errors={"run_invocations": (200, {"status": "ok"})},
    )
    assert any("fail-closed: run_invocations query" in f for f in verdict["failures"])


# ---------------------------------------------------------------------------
# Attempt 7 corrective — defect 2: Decimal, call_seq-aware one-to-one cost
# reconciliation (the exact production 84-call rounding shape must PASS;
# tampering, duplicates and missing calls must FAIL; caps stay strict)
# ---------------------------------------------------------------------------


def attempt7_rounding_dataset():
    """The exact 84-call Attempt 7 rounding shape (run 8b4a4277-…).

    Reservation rows hold the UNROUNDED per-call costs (plain numeric,
    migration 015); each settled ledger row holds the SAME cost rounded to
    6 decimals (numeric(12,6), migration 013). 22 calls carry a per-row
    rounding drift of +0.0000004 — every row within the 0.0000005
    half-ulp — so the aggregate signed drift is +0.0000088, larger than
    the old fixed 5e-6 aggregate tolerance: the old gate false-failed on
    exactly this legitimate production shape. run.usage.actual_cost is the
    worker's unrounded running total rounded once (0.252069); tokens
    total 312,018 over 84 calls.
    """
    unrounded, rounded = {}, {}
    for seq in range(1, 85):
        if seq <= 22:
            unrounded[seq], rounded[seq] = 0.0029996, 0.003000
        elif seq <= 83:
            unrounded[seq], rounded[seq] = 0.003001, 0.003001
        else:
            unrounded[seq], rounded[seq] = 0.0030168, 0.0030168
    data = happy_dataset()
    data["runs"][0]["usage"] = {
        "model_calls": 84,
        "input_tokens": 252_000,
        "output_tokens": 60_018,
        "total_tokens": 312_018,
        "actual_cost": 0.252069,
    }
    data["model_call_budget_reservations"] = [
        {"call_seq": seq, "attempt": 1, "status": "settled", "estimated_cost": 0.02, "actual_cost": unrounded[seq]}
        for seq in range(1, 85)
    ]
    data["run_usage_ledger"] = [
        {"decision": "reserved", "call_seq": seq, "actual_input_tokens": None, "actual_output_tokens": None, "actual_cost": None, "estimated_cost": 0.02}
        for seq in range(1, 85)
    ] + [
        {"decision": "settled", "call_seq": seq, "actual_input_tokens": 3000, "actual_output_tokens": 715 if seq <= 42 else 714, "actual_cost": rounded[seq], "estimated_cost": None}
        for seq in range(1, 85)
    ]
    return data


def test_attempt_7_production_rounding_shape_passes(db, monkeypatch, capsys):
    wire_evidence(db, monkeypatch, attempt7_rounding_dataset())
    db.evidence()
    verdict = gate_output(db, capsys)
    assert verdict["ok"] is True, verdict["failures"]
    assert verdict["failures"] == []
    assert verdict["matched_calls"] == 84
    assert verdict["settled_cost_sum"] == "0.2520690"      # unrounded reservation total
    assert verdict["ledger_actual_cost_sum"] == "0.2520778"  # per-row 6-dp rounded total


def test_attempt_7_shape_exceeds_the_old_aggregate_tolerance(db):
    """Documents the false failure: the legitimate aggregate signed drift
    (0.0000088) is larger than the removed fixed 5e-6 tolerance, and no
    fixed aggregate tolerance can be right for every call count."""
    drift = Decimal("0.2520778") - Decimal("0.2520690")
    assert drift == Decimal("0.0000088")
    assert drift > Decimal("0.000005")
    assert not hasattr(db, "COST_TOLERANCE")


def test_reconciliation_rejects_tampering_beyond_per_row_rounding(db, monkeypatch, capsys):
    """A ledger row moved by just 2 ulps of the 7th decimal — far below the
    old aggregate tolerance — is beyond legitimate 6-dp rounding and fails."""
    data = attempt7_rounding_dataset()
    settled = [r for r in data["run_usage_ledger"] if r["decision"] == "settled"]
    assert settled[0]["call_seq"] == 1
    settled[0]["actual_cost"] = 0.003001  # reservation 0.0029996 → drift 0.0000014
    verdict = evidence_must_fail(db, monkeypatch, capsys, data)
    assert any("call_seq 1" in f and "rounding tolerance" in f for f in verdict["failures"])


def test_reconciliation_rejects_gross_tampering(db, monkeypatch, capsys):
    data = attempt7_rounding_dataset()
    settled = [r for r in data["run_usage_ledger"] if r["decision"] == "settled"]
    settled[-1]["actual_cost"] = 0.01
    verdict = evidence_must_fail(db, monkeypatch, capsys, data)
    assert any("call_seq 84" in f and "rounding tolerance" in f for f in verdict["failures"])


def test_reconciliation_rejects_duplicate_ledger_call_seq(db, monkeypatch, capsys):
    data = attempt7_rounding_dataset()
    data["run_usage_ledger"].append({"decision": "settled", "call_seq": 5, "actual_input_tokens": 0, "actual_output_tokens": 0, "actual_cost": 0.003, "estimated_cost": None})
    verdict = evidence_must_fail(db, monkeypatch, capsys, data)
    assert any("duplicate settled ledger row for call_seq 5" in f for f in verdict["failures"])


def test_reconciliation_rejects_duplicate_reservation_call_seq(db, monkeypatch, capsys):
    data = attempt7_rounding_dataset()
    data["model_call_budget_reservations"].append({"call_seq": 5, "attempt": 1, "status": "settled", "estimated_cost": 0.02, "actual_cost": 0.003001})
    verdict = evidence_must_fail(db, monkeypatch, capsys, data)
    assert any("duplicate reservation for call_seq 5" in f for f in verdict["failures"])


def test_reconciliation_rejects_missing_ledger_call(db, monkeypatch, capsys):
    data = attempt7_rounding_dataset()
    data["run_usage_ledger"] = [r for r in data["run_usage_ledger"] if not (r["decision"] == "settled" and r["call_seq"] == 7)]
    verdict = evidence_must_fail(db, monkeypatch, capsys, data)
    assert any("missing a settled ledger row: [7]" in f for f in verdict["failures"])


def test_reconciliation_rejects_unmatched_ledger_call(db, monkeypatch, capsys):
    data = attempt7_rounding_dataset()
    data["run_usage_ledger"].append({"decision": "settled", "call_seq": 999, "actual_input_tokens": 0, "actual_output_tokens": 0, "actual_cost": 0.003, "estimated_cost": None})
    verdict = evidence_must_fail(db, monkeypatch, capsys, data)
    assert any("without a settled reservation: [999]" in f for f in verdict["failures"])


def test_usage_total_is_compared_to_the_unrounded_reservation_total(db, monkeypatch, capsys):
    """Rewriting run.usage to the per-row-ROUNDED ledger sum (0.2520778)
    must fail: the worker snapshots the unrounded running total, so only
    0.252069 is consistent — this is the comparison the old gate got
    backwards by comparing usage to the ledger sum."""
    data = attempt7_rounding_dataset()
    data["runs"][0]["usage"]["actual_cost"] = 0.252078
    verdict = evidence_must_fail(db, monkeypatch, capsys, data)
    assert any("unrounded reservation total" in f for f in verdict["failures"])


def test_cost_caps_stay_strict_with_no_rounding_tolerance(db, monkeypatch, capsys):
    """One micro-dollar over the $3.00 cap fails — reconciliation tolerances
    never loosen the caps."""
    data = attempt7_rounding_dataset()
    over = 3.000001
    data["runs"][0]["usage"]["actual_cost"] = over
    data["model_call_budget_reservations"] = [
        {"call_seq": 1, "attempt": 1, "status": "settled", "estimated_cost": 0.02, "actual_cost": over}
    ]
    data["run_usage_ledger"] = [
        {"decision": "settled", "call_seq": 1, "actual_input_tokens": 3000, "actual_output_tokens": 714, "actual_cost": over, "estimated_cost": None}
    ]
    data["runs"][0]["usage"].update({"model_calls": 1, "input_tokens": 3000, "output_tokens": 714, "total_tokens": 3714})
    verdict = evidence_must_fail(db, monkeypatch, capsys, data)
    assert any("exceeds cap" in f for f in verdict["failures"])


# ---------------------------------------------------------------------------
# verify_caps.py: exact cap verification on both surfaces
# ---------------------------------------------------------------------------


# The Attempt 7 RUNTIME release pin: the merge commit of the reviewed
# PR #50 runtime changes (deliberately NOT the release-tooling PR's own
# merge commit — that would be a self-referential pin).
RELEASE_SHA = "88224bccc836f80f3dc1d173306a1aa63cddcc7a"
REGISTRY = "us-central1-docker.pkg.dev/big-cabinet-457321-t7/milo-agent"


def caps_env_list(**overrides):
    env = dict(pair.split("=", 1) for pair in CAPS.split(","))
    env.update(overrides)
    return env


def provider_env_list(**overrides):
    env = dict(pair.split("=", 1) for pair in PROVIDER_LIMITS.split(","))
    env.update(overrides)
    return env


def worker_env_list(**overrides):
    env = caps_env_list(MILO_ENABLE_PAID_EXECUTION="tru" + "e")
    env.update(provider_env_list())
    env.update(overrides)
    return env


def worker_spec(env=None, image=None, bind_key=True, extra=None):
    entries = [{"name": k, "value": v} for k, v in (env or worker_env_list()).items()]
    if bind_key:
        entries.append({"name": "KIMI_API_KEY", "valueFrom": {"secretKeyRef": {"name": "KIMI_API_KEY"}}})
    entries.extend(extra or [])
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


def run_verify_caps(tmp_path, worker, api, caps=CAPS, provider_limits=PROVIDER_LIMITS):
    worker_path = tmp_path / "worker.json"
    api_path = tmp_path / "api.json"
    worker_path.write_text(json.dumps(worker))
    api_path.write_text(json.dumps(api))
    return subprocess.run(
        [sys.executable, str(STAGE_C / "verify_caps.py"), "--worker-json", str(worker_path), "--api-json", str(api_path)],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "STAGE_C_CAPS": caps,
            "STAGE_C_WORKER_PROVIDER_LIMITS": provider_limits,
            "STAGE_C_REGISTRY": REGISTRY,
            "STAGE_C_RELEASE_SHA": RELEASE_SHA,
        },
        timeout=60,
    )


def test_verify_caps_passes_on_exact_posture(tmp_path):
    result = run_verify_caps(tmp_path, worker_spec(), api_spec())
    assert result.returncode == 0, result.stdout + result.stderr


def test_verify_caps_fails_on_missing_cap(tmp_path):
    env = worker_env_list()
    del env["MILO_MAX_COST_PER_RUN"]
    result = run_verify_caps(tmp_path, worker_spec(env=env), api_spec())
    assert result.returncode != 0
    assert "MILO_MAX_COST_PER_RUN is MISSING" in result.stdout


def test_verify_caps_fails_on_changed_value_on_either_surface(tmp_path):
    loosened_worker = worker_env_list(MILO_MAX_COST_PER_RUN="30.00")
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
    env = worker_env_list(MILO_MAX_SNEAKY_NEW_CAP="1000000")
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
# verify_caps.py: worker-only provider operating envelope (Attempt 7)
# ---------------------------------------------------------------------------


def test_verify_caps_requires_all_seven_provider_limits_exact_on_worker(tmp_path):
    """The passing posture above already includes them; here every single
    pinned setting must be present with the exact pinned value."""
    result = run_verify_caps(tmp_path, worker_spec(), api_spec())
    assert result.returncode == 0, result.stdout + result.stderr
    assert "7 provider limits exact on worker only" in result.stdout
    for key in provider_env_list():
        env = worker_env_list()
        del env[key]
        missing = run_verify_caps(tmp_path, worker_spec(env=env), api_spec())
        assert missing.returncode != 0, key
        assert f"provider limit {key} is MISSING" in missing.stdout


def test_verify_caps_fails_on_changed_worker_provider_value(tmp_path):
    env = worker_env_list(MILO_PROVIDER_MAX_CONCURRENCY="10")  # no V2 parallelism
    result = run_verify_caps(tmp_path, worker_spec(env=env), api_spec())
    assert result.returncode != 0
    assert "MILO_PROVIDER_MAX_CONCURRENCY" in result.stdout
    assert "differs from pinned" in result.stdout


def test_verify_caps_fails_on_unexpected_worker_provider_variable(tmp_path):
    env = worker_env_list(MILO_PROVIDER_SNEAKY_SETTING="1")
    result = run_verify_caps(tmp_path, worker_spec(env=env), api_spec())
    assert result.returncode != 0
    assert "MILO_PROVIDER_SNEAKY_SETTING" in result.stdout
    assert "not pinned in STAGE_C_WORKER_PROVIDER_LIMITS" in result.stdout


def test_verify_caps_fails_on_unexpected_worker_provider_secret_binding(tmp_path):
    """An unexpected MILO_PROVIDER_* variable cannot hide inside a Secret
    Manager binding: the not-pinned scan covers worker_env ∪ worker_secrets."""
    sneaky = {"name": "MILO_PROVIDER_HIDDEN_TOKEN", "valueFrom": {"secretKeyRef": {"name": "HIDDEN_SECRET"}}}
    result = run_verify_caps(tmp_path, worker_spec(extra=[sneaky]), api_spec())
    assert result.returncode != 0
    assert "worker: unexpected provider variable MILO_PROVIDER_HIDDEN_TOKEN" in result.stdout
    assert "not pinned in STAGE_C_WORKER_PROVIDER_LIMITS" in result.stdout
    # The binding's secret NAME payload is never echoed beyond the var name.
    assert "HIDDEN_SECRET" not in result.stdout + result.stderr


def test_verify_caps_fails_when_a_pinned_provider_limit_arrives_as_a_secret_binding(tmp_path):
    """A pinned limit supplied only via a binding is not a literal exact
    value — it fails as MISSING rather than being trusted."""
    env = worker_env_list()
    del env["MILO_PROVIDER_RPM_LIMIT"]
    binding = {"name": "MILO_PROVIDER_RPM_LIMIT", "valueFrom": {"secretKeyRef": {"name": "RPM_SECRET"}}}
    result = run_verify_caps(tmp_path, worker_spec(env=env, extra=[binding]), api_spec())
    assert result.returncode != 0
    assert "provider limit MILO_PROVIDER_RPM_LIMIT is MISSING" in result.stdout


def test_verify_caps_fails_on_any_provider_variable_on_api(tmp_path):
    """Provider limits are never silently applied to the API — a single
    MILO_PROVIDER_* variable there fails, pinned value or not."""
    bad_api = api_spec()
    bad_api["spec"]["template"]["spec"]["containers"][0]["env"].append(
        {"name": "MILO_PROVIDER_MAX_CONCURRENCY", "value": "2"}
    )
    result = run_verify_caps(tmp_path, worker_spec(), bad_api)
    assert result.returncode != 0
    assert "api: provider variable MILO_PROVIDER_MAX_CONCURRENCY must NEVER be set" in result.stdout


def test_verify_caps_fails_closed_without_pinned_provider_limits(tmp_path):
    result = run_verify_caps(tmp_path, worker_spec(), api_spec(), provider_limits="")
    assert result.returncode != 0
    assert "STAGE_C_WORKER_PROVIDER_LIMITS is empty" in result.stdout


def test_verify_caps_fails_closed_on_incomplete_pinned_provider_limits(tmp_path):
    result = run_verify_caps(
        tmp_path, worker_spec(), api_spec(), provider_limits="MILO_PROVIDER_MAX_CONCURRENCY=2"
    )
    assert result.returncode != 0
    assert "missing pinned setting" in result.stdout


def test_verify_caps_fails_on_literal_provider_key_value_on_worker(tmp_path):
    env = worker_env_list(KIMI_API_KEY="sk-LITERAL-LEAK")
    result = run_verify_caps(tmp_path, worker_spec(env=env), api_spec())
    assert result.returncode != 0
    assert "worker: KIMI_API_KEY is present as a LITERAL env value" in result.stdout
    # Error output names the variable but never the secret value.
    assert "sk-LITERAL-LEAK" not in result.stdout + result.stderr


def test_verify_caps_fails_on_literal_provider_key_value_on_api(tmp_path):
    bad_api = api_spec()
    bad_api["spec"]["template"]["spec"]["containers"][0]["env"].append(
        {"name": "MOONSHOT_API_KEY", "value": "sk-LITERAL-LEAK"}
    )
    result = run_verify_caps(tmp_path, worker_spec(), bad_api)
    assert result.returncode != 0
    assert "api: MOONSHOT_API_KEY is present as a literal env value" in result.stdout
    assert "sk-LITERAL-LEAK" not in result.stdout + result.stderr


def test_verify_caps_still_requires_worker_secret_binding(tmp_path):
    """Provider-secret posture stays worker-only: the enabled posture needs
    the KIMI_API_KEY Secret Manager binding on the worker."""
    result = run_verify_caps(tmp_path, worker_spec(bind_key=False), api_spec())
    assert result.returncode != 0
    assert "worker: provider key is not bound" in result.stdout


# ---------------------------------------------------------------------------
# shell steps: structured gates, no warning-only checks
# ---------------------------------------------------------------------------


def test_execute_smoke_gates_on_acceptable_terminal_and_exact_caps():
    text = (STAGE_C / "05-execute-smoke.sh").read_text()
    assert "verify_caps.py" in text
    assert "STAGE_C_ACCEPTABLE_TERMINAL_STATES" in text
    assert "kill-switch.sh" in text
    assert 'record.get("acceptable") is True' in text
    # Pre-run exact baseline gate: structured JSON via verify_executions.py
    # against the pinned prior-execution count — no wc -l line counting.
    assert 'verify_executions.py --expected-total "${STAGE_C_EXPECTED_PRIOR_EXECUTIONS}"' in text
    assert "wc -l" not in text


def test_deploy_step_gates_on_the_exact_execution_baseline():
    text = (STAGE_C / "02-deploy-images.sh").read_text()
    assert 'verify_executions.py --expected-total "${STAGE_C_EXPECTED_PRIOR_EXECUTIONS}"' in text
    assert "wc -l" not in text


def test_posture_verification_gates_on_the_exact_execution_baseline():
    text = (STAGE_C / "03b-verify-stage-c-posture.sh").read_text()
    assert 'verify_executions.py --expected-total "${STAGE_C_EXPECTED_PRIOR_EXECUTIONS}"' in text


def test_collect_evidence_is_a_gate_not_a_checklist():
    text = (STAGE_C / "06-collect-evidence.sh").read_text()
    assert "ACCEPTANCE GATE" in text
    # Post-run exact increment gate: pinned baseline + exactly one new
    # execution, decided from structured JSON, not a fragile line count.
    assert "expected_total_executions=$((STAGE_C_EXPECTED_PRIOR_EXECUTIONS + 1))" in text
    assert 'verify_executions.py --expected-total "${expected_total_executions}"' in text
    assert "wc -l" not in text
    assert 'test "${hits}" = "0"' in text
    assert "WARNING: possible secret markers" not in text
    assert "Validation checklist to record" not in text  # replaced by executable checks
    # The DB gate receives the pinned run baseline for its 2-total check.
    assert '"STAGE_C_EXPECTED_PRIOR_RUNS=${STAGE_C_EXPECTED_PRIOR_RUNS}"' in text


def test_stage_c_log_collection_handles_structured_and_text_payloads():
    execute = (STAGE_C / "05-execute-smoke.sh").read_text()
    evidence = (STAGE_C / "06-collect-evidence.sh").read_text()

    # Cloud Run Python probes emit structured records as jsonPayload in
    # production. Operator gates must not silently look only at textPayload.
    assert execute.count("--format='json(textPayload,jsonPayload)'") == 1
    assert evidence.count("--format='json(textPayload,jsonPayload)'") == 2
    assert "value(textPayload)" not in execute
    assert "value(textPayload)" not in evidence

    # run_probe renders structured payloads back into JSON lines so the
    # existing explicit ok/terminal verdict parsers continue to gate on them.
    for script in (execute, evidence):
        assert 'if not isinstance(record, dict):' in script
        assert 'record.get("jsonPayload")' in script
        assert '"stage_c_probe" in payload' in script
        assert 'json.loads(text)' in script
        assert 'file=sys.stderr' in script
        assert 'json.dumps(payload, sort_keys=True)' in script

    for script in (execute, evidence):
        renderer_tail = script.split("for record in json.load(sys.stdin):", 1)[1].split("\n}", 1)[0]
        assert "' || true" not in renderer_tail


# ---------------------------------------------------------------------------
# Attempt 7 corrective — defect 3: a probe execution that completes nonzero
# must never lose its execution name; its structured failure logs are
# retrieved anyway, and an unestablishable name fails clearly instead of
# issuing an invalid empty logging filter
# ---------------------------------------------------------------------------


ATTEMPT7_RUN_ID = "8b4a4277-fdf0-41b2-8515-d7e1d50e441b"

# Mock gcloud: the evidence probe execution completes NONZERO (terminal
# Completed=False) — the exact case where the old `--wait` form lost
# exec_name — while Cloud Logging still holds its structured failure line.
FAILED_PROBE_GCLOUD = r'''#!/usr/bin/env bash
{ printf '%s\x1f' "$@"; printf '\n'; } >> "__LOG__"
case "$*" in
  *"run jobs execute"*)
    echo "stagec-db-probe-fail1"
    ;;
  *"run jobs executions describe"*)
    echo '{"metadata":{"name":"stagec-db-probe-fail1"},"status":{"completionTime":"2026-08-21T00:00:00Z","conditions":[{"type":"Completed","status":"False"}]}}'
    ;;
  *"logging read"*)
    echo '[{"textPayload":"{\"stage_c_probe\": \"evidence\", \"ok\": false, \"failures\": [\"total_runs=3, expected exactly 2 (the pinned prior baseline of 1 plus exactly one new authorized run)\"]}"}]'
    ;;
esac
exit 0
'''

# Mock gcloud: execution creation itself fails — no name can ever be
# established, so the gate must refuse clearly and issue NO logging call.
NO_NAME_GCLOUD = r'''#!/usr/bin/env bash
{ printf '%s\x1f' "$@"; printf '\n'; } >> "__LOG__"
case "$*" in
  *"run jobs execute"*)
    echo "ERROR: (gcloud.run.jobs.execute) job not found" >&2
    exit 1
    ;;
esac
exit 0
'''


def run_collect_evidence(tmp_path, mock_gcloud_script):
    bin_dir = tmp_path / "mockbin"
    bin_dir.mkdir()
    log = tmp_path / "gcloud-args.log"
    mock = bin_dir / "gcloud"
    mock.write_text(mock_gcloud_script.replace("__LOG__", str(log)))
    mock.chmod(0o755)
    result = subprocess.run(
        ["bash", str(STAGE_C / "06-collect-evidence.sh"), ATTEMPT7_RUN_ID],
        capture_output=True,
        text=True,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        timeout=120,
    )
    calls = log.read_text().splitlines() if log.exists() else []
    return result, [c.split("\x1f") for c in calls]


def test_failed_probe_execution_keeps_its_name_and_its_structured_failure_logs(tmp_path):
    result, calls = run_collect_evidence(tmp_path, FAILED_PROBE_GCLOUD)
    # The gate fails (the probe reported failures) — but with evidence.
    assert result.returncode != 0
    assert "DB evidence gate reported failures" in result.stdout
    # The probe's structured failure record was retrieved and surfaced.
    assert '"ok": false' in result.stdout
    assert "total_runs=3" in result.stdout
    # The logging filter used the preserved execution name — never empty.
    logging_calls = [c for c in calls if "logging" in c and "read" in c]
    assert logging_calls, "no log retrieval happened for the failed probe"
    filters = "\x1f".join(logging_calls[0])
    assert 'execution_name"=stagec-db-probe-fail1' in filters
    assert 'execution_name"=\x1f' not in filters and not filters.rstrip().endswith('execution_name"=')
    # The terminal verdict came from the structured describe, not a lost
    # gcloud exit status.
    assert any("describe" in c for c in calls)


def test_unestablishable_execution_name_fails_clearly_with_no_empty_filter(tmp_path):
    result, calls = run_collect_evidence(tmp_path, NO_NAME_GCLOUD)
    assert result.returncode != 0
    assert "could not establish the execution name" in result.stderr
    # No logging read may ever be issued without an execution name (the
    # old defect issued an invalid empty `execution_name=` filter).
    assert not any("logging" in c and "read" in c for c in calls)


def test_collect_evidence_launches_probes_async_and_waits_on_the_named_execution():
    text = (STAGE_C / "06-collect-evidence.sh").read_text()
    assert "--async --format='value(metadata.name)'" in text
    code_lines = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    assert not any("--wait" in line for line in code_lines)
    assert "wait_for_probe_execution" in text
    assert "execution_state.py" in text
    assert "could not establish the execution name" in text


@pytest.mark.parametrize("payload,expected", [
    (json.dumps({"metadata": {"name": "e1"}, "status": {"conditions": [{"type": "Completed", "status": "True"}]}}), "succeeded"),
    (json.dumps({"metadata": {"name": "e1"}, "status": {"conditions": [{"type": "Completed", "status": "False"}], "completionTime": "2026-08-21T00:00:00Z"}}), "failed"),
    # Terminal without a positive Completed condition: fail-safe as failed.
    (json.dumps({"status": {"completionTime": "2026-08-21T00:00:00Z"}}), "failed"),
    (json.dumps({"status": {"conditions": [{"type": "Completed", "status": "Unknown"}]}}), "running"),
    (json.dumps({"status": {}}), "running"),
    (json.dumps(None), "running"),
    ("not json at all", "running"),
])
def test_execution_state_verdicts_are_fail_safe(payload, expected):
    result = subprocess.run(
        [sys.executable, str(STAGE_C / "execution_state.py")],
        input=payload,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


# ---------------------------------------------------------------------------
# stage-c-env.sh: authorized constants are pinned; inherited shell values
# cannot widen or redirect the authorization
# ---------------------------------------------------------------------------


AUTHORIZED = {
    "STAGE_C_PROJECT": "big-cabinet-457321-t7",
    "STAGE_C_REGION": "us-central1",
    "STAGE_C_RELEASE_SHA": RELEASE_SHA,
    "STAGE_C_IDEMPOTENCY_KEY": ATTEMPT7_KEY,
    "STAGE_C_EXPECTED_PRIOR_RUNS": EXPECTED_PRIOR_RUNS,
    "STAGE_C_EXPECTED_PRIOR_EXECUTIONS": EXPECTED_PRIOR_EXECUTIONS,
    "STAGE_C_ACCEPTABLE_TERMINAL_STATES": "completed",
    "STAGE_C_WORKER_PROVIDER_LIMITS": PROVIDER_LIMITS,
}


def source_stage_c_env(overrides=None):
    """Source stage-c-env.sh the way every step script does and dump the
    resulting authorized values (empty env apart from the overrides)."""
    dump = "; ".join(f'echo "{k}=${{{k}}}"' for k in list(AUTHORIZED) + ["STAGE_C_CAPS", "STAGE_C_REGISTRY", "STAGE_C_REPO_URL"])
    return subprocess.run(
        ["bash", "-c", f"set -euo pipefail; source '{STAGE_C / 'stage-c-env.sh'}'; {dump}"],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", **(overrides or {})},
        timeout=60,
    )


def test_env_exports_the_exact_authorized_constants():
    result = source_stage_c_env()
    assert result.returncode == 0, result.stderr
    for key, value in AUTHORIZED.items():
        assert f"{key}={value}" in result.stdout
    assert f"STAGE_C_CAPS={CAPS}" in result.stdout  # existing budget caps kept exact
    # The runtime pin is the reviewed PR #50 merge commit, not the old
    # release and not a truncated prefix.
    assert "STAGE_C_RELEASE_SHA=88224bccc836f80f3dc1d173306a1aa63cddcc7a" in result.stdout
    assert "30b05bc45d6f9372261e4fac20cd983c69db971f" not in result.stdout
    # The Attempt 7 identity is fresh; the consumed Attempt 5/6 key is gone.
    assert f"STAGE_C_IDEMPOTENCY_KEY={ATTEMPT7_KEY}" in result.stdout
    assert CONSUMED_KEY not in result.stdout


def test_env_pins_all_seven_provider_limits_exactly():
    result = source_stage_c_env()
    assert result.returncode == 0, result.stderr
    line = next(row for row in result.stdout.splitlines() if row.startswith("STAGE_C_WORKER_PROVIDER_LIMITS="))
    pinned = dict(pair.split("=", 1) for pair in line.split("=", 1)[1].split(","))
    assert pinned == {
        "MILO_PROVIDER_MAX_CONCURRENCY": "2",  # intentionally 2 — no V2 parallelism
        "MILO_PROVIDER_RPM_LIMIT": "350",
        "MILO_PROVIDER_TPM_LIMIT": "2400000",
        "MILO_PROVIDER_MAX_RATE_LIMIT_RETRIES": "5",
        "MILO_PROVIDER_MAX_BACKPRESSURE_WAIT_SECONDS": "240",
        "MILO_PROVIDER_BACKOFF_BASE_SECONDS": "2",
        "MILO_PROVIDER_BACKOFF_MAX_SECONDS": "30",
    }
    # Provider scheduling must NOT ride along in STAGE_C_CAPS (caps are
    # applied and verified on BOTH surfaces; the envelope is worker-only).
    caps_line = next(row for row in result.stdout.splitlines() if row.startswith("STAGE_C_CAPS="))
    assert "MILO_PROVIDER_" not in caps_line


@pytest.mark.parametrize("var,hostile", [
    ("STAGE_C_PROJECT", "attacker-project-123"),
    ("STAGE_C_REGION", "europe-west1"),
    ("STAGE_C_RELEASE_SHA", "791f7af9" + "0" * 32),  # a different valid-looking SHA
    ("STAGE_C_RELEASE_SHA", "30b05bc45d6f9372261e4fac20cd983c69db971f"),  # the superseded release
    ("STAGE_C_EXPECTED_PRIOR_RUNS", "0"),  # pretending the history is empty
    ("STAGE_C_EXPECTED_PRIOR_RUNS", "2"),  # the superseded count — Attempt 5's row is absent
    ("STAGE_C_EXPECTED_PRIOR_RUNS", "5"),
    ("STAGE_C_EXPECTED_PRIOR_EXECUTIONS", "0"),
    ("STAGE_C_EXPECTED_PRIOR_EXECUTIONS", "2"),  # the pre-deletion count — no longer the live baseline
    ("STAGE_C_ACCEPTABLE_TERMINAL_STATES", "completed,failed,budget_exhausted"),
])
def test_env_refuses_inherited_override_of_authorized_constants(var, hostile):
    result = source_stage_c_env({var: hostile})
    assert result.returncode != 0, f"{var} override was silently accepted"
    assert "STAGE C REFUSED" in result.stderr
    assert var in result.stderr
    # The hostile value must never surface as the exported posture.
    assert f"{var}={hostile}" not in result.stdout


@pytest.mark.parametrize("var,hostile", [
    ("STAGE_C_CAPS", "MILO_MAX_COST_PER_RUN=300.00"),
    ("STAGE_C_WORKER_PROVIDER_LIMITS", "MILO_PROVIDER_MAX_CONCURRENCY=100,MILO_PROVIDER_RPM_LIMIT=500"),
    ("STAGE_C_REGISTRY", "us-central1-docker.pkg.dev/attacker-project/evil"),
    ("STAGE_C_REPO_URL", "https://github.com/attacker/fork.git"),
    ("STAGE_C_API_URL", "https://attacker.example.run.app"),
    ("STAGE_C_IDEMPOTENCY_KEY", "second-run-key"),
    ("STAGE_C_IDEMPOTENCY_KEY", "stage-c-smoke-0001"),  # the consumed Attempt 5/6 key
])
def test_env_refuses_redirection_of_derived_authorization_surface(var, hostile):
    result = source_stage_c_env({var: hostile})
    assert result.returncode != 0, f"{var} override was silently accepted"
    assert "STAGE C REFUSED" in result.stderr


def test_env_accepts_values_equal_to_the_authorized_constants():
    """Re-sourcing (every step script sources the file) must stay idempotent."""
    result = source_stage_c_env(dict(AUTHORIZED))
    assert result.returncode == 0, result.stderr


def test_step_scripts_abort_when_env_refuses():
    """A conflicting environment must stop a step script before any gcloud
    call — the refusal happens at source time under set -e."""
    for script in ("05-execute-smoke.sh", "06-collect-evidence.sh", "kill-switch.sh"):
        text = (STAGE_C / script).read_text()
        assert "set -euo pipefail" in text
        assert "source ./stage-c-env.sh" in text
    result = subprocess.run(
        ["bash", str(STAGE_C / "03b-verify-stage-c-posture.sh")],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "STAGE_C_PROJECT": "attacker-project-123"},
        timeout=60,
    )
    assert result.returncode != 0
    assert "STAGE C REFUSED" in result.stderr
    assert "gcloud" not in result.stdout  # refused before doing anything


# ---------------------------------------------------------------------------
# probe-source transport: gcloud env-var delimiter must never split the source
# (Stage C attempt 1 failed on '^@^' splitting at stage-c-smoke@invalid.milo)
# ---------------------------------------------------------------------------


DELIM = ":::"


def gcloud_env_dict(set_env_vars_arg: str) -> dict[str, str]:
    """Replicate gcloud's ^DELIM^ env-var splitting semantics."""
    assert set_env_vars_arg.startswith("^"), set_env_vars_arg[:20]
    delim, _, rest = set_env_vars_arg[1:].partition("^")
    env = {}
    for pair in rest.split(delim):
        key, _, value = pair.partition("=")
        env[key] = value
    return env


def run_create_probes(stage_c_dir: Path, tmp_path: Path):
    """Run 04-create-probes.sh with a mocked gcloud that records argv verbatim."""
    bin_dir = tmp_path / "mockbin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "gcloud-args.log"
    mock = bin_dir / "gcloud"
    mock.write_text('#!/usr/bin/env bash\nprintf "%s\\0" "$@" >> "' + str(log) + '"\n')
    mock.chmod(0o755)
    result = subprocess.run(
        ["bash", str(stage_c_dir / "04-create-probes.sh")],
        capture_output=True,
        text=True,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        timeout=120,
    )
    args = log.read_bytes().decode().split("\0") if log.exists() else []
    return result, args


def test_delimiter_absent_from_both_probe_sources_and_at_sign_present():
    db_source = (STAGE_C / "probe_db.py").read_text()
    gw_source = (STAGE_C / "probe_gateway.py").read_text()
    for name, source in (("probe_db.py", db_source), ("probe_gateway.py", gw_source)):
        assert DELIM not in source, f"{name} contains the transport delimiter {DELIM!r}"
        # The regression trigger must stay covered: '@' IS in the sources,
        # so a single-character '@' delimiter would split them again.
        assert "@" in source, name


def test_probe_sources_survive_gcloud_transport_byte_for_byte(tmp_path):
    result, args = run_create_probes(STAGE_C, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    env_args = [a for a in args if a.startswith("--set-env-vars=")]
    assert len(env_args) == 2, "expected one env-var arg per probe job"
    db_env = gcloud_env_dict(env_args[0].removeprefix("--set-env-vars="))
    gw_env = gcloud_env_dict(env_args[1].removeprefix("--set-env-vars="))
    # Byte-for-byte identical to the files on disk after gcloud splitting.
    assert db_env["PROBE_SOURCE"] == (STAGE_C / "probe_db.py").read_text()
    assert gw_env["PROBE_SOURCE"] == (STAGE_C / "probe_gateway.py").read_text()
    # The literal that broke attempt 1 survives whole.
    assert 'os.environ.get("STAGE_C_TEST_EMAIL", "stage-c-smoke@invalid.milo")' in db_env["PROBE_SOURCE"]
    # And the transported sources are still valid Python.
    compile(db_env["PROBE_SOURCE"], "probe_db.py", "exec")
    compile(gw_env["PROBE_SOURCE"], "probe_gateway.py", "exec")
    assert db_env["STAGE_C_MODE"] == "preflight"
    assert gw_env["STAGE_C_MODE"] == "create"


def test_delimiter_collision_fails_closed_before_any_gcloud_call(tmp_path):
    staged = tmp_path / "stage-c"
    shutil.copytree(STAGE_C, staged)
    with open(staged / "probe_db.py", "a", encoding="utf-8") as fh:
        fh.write(f'\nCOLLIDING = "{DELIM}"\n')
    result, args = run_create_probes(staged, tmp_path)
    assert result.returncode != 0
    assert "STAGE C REFUSED" in result.stderr
    assert "DB_SOURCE" in result.stderr
    assert args == [], "gcloud was invoked despite the delimiter collision"


def test_create_probes_introduces_no_paid_flags_or_extra_mutations(tmp_path):
    result, args = run_create_probes(STAGE_C, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    blob = "\0".join(args)
    # Only the two authorized disposable job creations; nothing else.
    assert blob.count("jobs\0create") == 2
    for forbidden in ("services\0update", "jobs\0update", "jobs\0execute", "builds\0submit", "MILO_ENABLE_", "KIMI_API_KEY="):
        assert forbidden not in blob, f"unexpected mutation surface: {forbidden}"
    # The gateway probe binds no secrets; the DB probe binds only the two
    # Supabase secrets that identity already reads.
    secret_args = [a for a in args if a.startswith("--set-secrets=")]
    assert secret_args == ["--set-secrets=SUPABASE_URL=SUPABASE_URL:latest,SUPABASE_SERVICE_ROLE_KEY=SUPABASE_SECRET_KEY:latest"]


def test_run_probe_helpers_use_checked_multichar_delimiter():
    for script in ("05-execute-smoke.sh", "06-collect-evidence.sh"):
        text = (STAGE_C / script).read_text()
        assert 'delim=":::"' in text, script
        assert "STAGE C REFUSED" in text, script
        assert '"^@^' not in text, f"{script} still uses the collision-prone @ delimiter"
    assert '"^@^' not in (STAGE_C / "04-create-probes.sh").read_text()


def test_acceptance_doc_records_attempt_1_failed_safely():
    text = (REPO / "docs" / "production-readiness" / "STAGE_C_ACCEPTANCE.md").read_text()
    assert "FAILED SAFELY at DB preflight" in text
    assert "unterminated string literal" in text
    assert "stage-c-smoke@invalid.milo" in text


def test_acceptance_doc_does_not_call_three_dollars_a_hard_billing_ceiling():
    text = (REPO / "docs" / "production-readiness" / "STAGE_C_ACCEPTANCE.md").read_text()
    assert "NOT a hard provider-billing ceiling" in text
    assert "separately from tokens" in text
    # The production-image blocker must stay recorded.
    assert "Blocking deployment-consistency finding" in text


def test_acceptance_doc_records_attempt_7_truthfully():
    """The paid run completed, the gate false-failed, the kill switch
    completed — and Stage C is still NOT PASSED with no second run."""
    text = (REPO / "docs" / "production-readiness" / "STAGE_C_ACCEPTANCE.md").read_text()
    assert "8b4a4277-fdf0-41b2-8515-d7e1d50e441b" in text
    assert "milo-agent-worker-gggdc" in text
    assert "FALSE-FAILED" in text
    assert "STAGE C NOT PASSED" in text
    assert "$0.252069" in text
    assert "312,018" in text
    assert "no second paid run is permitted" in text.lower()
    assert "kill switch" in text.lower()
    assert "08-recover-evidence.md" in text
    # The report must never claim the acceptance evidence passed.
    assert "STAGE C PASSED" not in text


def test_recovery_runbook_is_pinned_and_fail_closed():
    """08-recover-evidence.md: manual-only, ONE pinned RUN_ID, worker paid
    flag and provider aliases untouched, launcher stays disabled, restore
    + probe deletion + exact-count proofs present, and no committed
    executable line pairs an execution flag with an enabled value."""
    text = (STAGE_C / "08-recover-evidence.md").read_text()
    assert "REQUIRES_MANUAL_OPERATOR_CONFIGURATION" in text
    assert text.count('RUN_ID="8b4a4277-fdf0-41b2-8515-d7e1d50e441b"') == 1
    # Fail-closed invariants.
    assert 'MILO_ENABLE_PAID_EXECUTION") == "false"' in text
    assert "MOONSHOT_API_KEY" in text and "KIMI_API_KEY" in text
    assert 'JOB_LAUNCHER") == "disabled"' in text
    # Exact-count proofs: post-run baseline is 2 runs / 2 executions.
    assert text.count("--expected-total 2") == 2
    assert "06-collect-evidence.sh" in text
    # The only enablement is the manual replay-surface flag via the
    # operator-run-time STAGE_C_ON indirection (03-enable pattern): the
    # literal enable value never appears in a committed line.
    assert 'MILO_ENABLE_RUN_CREATION=${STAGE_C_ON}' in text
    assert "MILO_ENABLE_RUN_CREATION=true" not in text
    assert "MILO_ENABLE_PAID_EXECUTION=${STAGE_C_ON}" not in text
    assert "MILO_ENABLE_PAID_EXECUTION=true" not in text
    assert "--update-secrets" not in text  # never binds a provider key
    # Restore + cleanup.
    assert "MILO_ENABLE_RUN_CREATION=false" in text
    assert "kill-switch.sh" in text
    assert text.count("jobs delete") == 2
    # It must never invoke the paid-run path.
    assert "05-execute-smoke.sh" not in text
    assert "STAGE_C_MODE=create" not in text


# ---------------------------------------------------------------------------
# verify_executions.py: exact Worker-execution baseline gate (Attempt 7 —
# 1 VISIBLE terminal historical execution before the run, 2 after;
# fail-safe on anything unverifiable). 2 executions historically occurred,
# but Attempt 5's was deleted on 2026-08-18, so only Attempt 6's is
# visible in Cloud Run and countable by the gate.
# ---------------------------------------------------------------------------


def terminal_execution(name, *, completion_time="2026-08-18T02:19:11Z", condition=None):
    status = {}
    if completion_time is not None:
        status["completionTime"] = completion_time
    if condition is not None:
        status["conditions"] = [{"type": "Completed", "status": condition}]
    return {"metadata": {"name": name}, "status": status}


def active_execution(name):
    return {"metadata": {"name": name}, "status": {"completionTime": None, "conditions": [{"type": "Completed", "status": "Unknown"}]}}


# The one execution Cloud Run still exposes: Attempt 6's, terminal
# `failed` (Completed=False). Attempt 5's execution is NOT here — it was
# deleted on 2026-08-18 and a listing can never contain it again.
VISIBLE_BASELINE = [
    terminal_execution("milo-agent-worker-mcfrx", completion_time=None, condition="False"),
]
# What a listing would look like if a second historical-looking terminal
# execution (re)appeared — e.g. an Attempt 5-style resurrection. With the
# visible baseline pinned at 1, this is a violation, not the baseline.
TWO_TERMINAL_EXECUTIONS = VISIBLE_BASELINE + [
    terminal_execution("milo-agent-worker-d2gfx"),
]


def run_verify_executions(listing, expected_total):
    payload = listing if isinstance(listing, str) else json.dumps(listing)
    return subprocess.run(
        [sys.executable, str(STAGE_C / "verify_executions.py"), "--expected-total", str(expected_total)],
        input=payload,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
        timeout=60,
    )


def test_exactly_one_visible_terminal_execution_passes_the_pre_run_gate():
    result = run_verify_executions(VISIBLE_BASELINE, 1)
    assert result.returncode == 0, result.stdout + result.stderr
    verdict = json.loads(result.stdout)
    assert verdict["ok"] is True
    assert verdict["terminal"] == 1
    assert verdict["nonterminal"] == 0


@pytest.mark.parametrize("listing", [
    [],  # zero — the visible Attempt 6 execution vanished too?
    TWO_TERMINAL_EXECUTIONS,  # two — the deleted Attempt 5 execution cannot reappear
    VISIBLE_BASELINE + [
        terminal_execution("milo-agent-worker-extra"),
        terminal_execution("milo-agent-worker-extra2"),
    ],  # three
])
def test_pre_run_gate_fails_on_any_count_other_than_the_pinned_baseline(listing):
    result = run_verify_executions(listing, 1)
    assert result.returncode != 0
    assert json.loads(result.stdout)["ok"] is False


def test_pre_run_gate_fails_on_a_new_active_execution():
    """A new active execution before authorization blocks the run even when
    the total still looks like the baseline."""
    result = run_verify_executions([active_execution("milo-agent-worker-live1")], 1)
    assert result.returncode != 0
    verdict = json.loads(result.stdout)
    assert verdict["nonterminal"] == 1
    assert any("nonterminal" in p for p in verdict["problems"])


@pytest.mark.parametrize("unverifiable", [
    {"metadata": {"name": "milo-agent-worker-null1"}, "status": {"completionTime": None}},
    {"metadata": {"name": "milo-agent-worker-miss1"}, "status": {"conditions": [{"type": "Completed", "status": "Unknown"}]}},
    {"metadata": {"name": "milo-agent-worker-nost1"}},
    {"metadata": {"name": "milo-agent-worker-bad1"}, "status": "not-a-dict"},
])
def test_missing_null_or_malformed_status_fails_safe_as_nonterminal(unverifiable):
    """Total matches the baseline (1) but the status proves nothing —
    fail-safe as nonterminal/unverifiable."""
    result = run_verify_executions([unverifiable], 1)
    assert result.returncode != 0
    assert json.loads(result.stdout)["nonterminal"] == 1


def test_exactly_two_visible_terminal_executions_pass_the_post_run_gate():
    listing = VISIBLE_BASELINE + [terminal_execution("milo-agent-worker-attempt7")]
    result = run_verify_executions(listing, 2)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["ok"] is True


@pytest.mark.parametrize("extra", [
    [],  # the new execution never happened — still 1
    [terminal_execution("milo-agent-worker-attempt7"), terminal_execution("milo-agent-worker-sneaky")],  # 3
])
def test_post_run_gate_fails_unless_exactly_one_execution_was_added(extra):
    """A second new execution cannot pass unnoticed: the exact-total check
    catches it whether it is terminal or active."""
    result = run_verify_executions(VISIBLE_BASELINE + extra, 2)
    assert result.returncode != 0
    assert json.loads(result.stdout)["ok"] is False


def test_post_run_gate_fails_on_a_still_active_new_execution():
    result = run_verify_executions(VISIBLE_BASELINE + [active_execution("milo-agent-worker-attempt7")], 2)
    assert result.returncode != 0
    assert json.loads(result.stdout)["nonterminal"] == 1


@pytest.mark.parametrize("listing", ["this is not json", '{"not": "a list"}', "null"])
def test_structured_listing_parse_failures_return_nonzero(listing):
    result = run_verify_executions(listing, 1)
    assert result.returncode != 0
    assert json.loads(result.stdout)["ok"] is False
    assert "failing closed" in result.stdout


def test_execution_gate_output_never_claims_success_with_problems():
    result = run_verify_executions([active_execution("x")], 1)
    verdict = json.loads(result.stdout)
    assert verdict["ok"] is False and verdict["problems"] and result.returncode != 0


# ---------------------------------------------------------------------------
# acceptance report: current status is truthful about Attempts 5–7
# ---------------------------------------------------------------------------


def acceptance_doc():
    return (REPO / "docs" / "production-readiness" / "STAGE_C_ACCEPTANCE.md").read_text()


def test_acceptance_doc_records_attempts_5_and_6_truthfully():
    text = acceptance_doc()
    assert "58daa7de-76f5-4359-93e1-3a767c912c20" in text  # Attempt 5 run
    assert "37912575-f9ce-4437-893d-7dfa45c53aa9" in text  # Attempt 6 run
    assert "claim_run_lease" in text
    assert "RETRY_LIMIT_REACHED" in text
    assert "47,380" in text
    assert "$0.03976" in text
    assert "Attempt 6\nauthorization is consumed" in text or "Attempt 6 authorization is consumed" in text or "ATTEMPT 6 AUTHORIZATION\nCONSUMED" in text


def test_acceptance_doc_distinguishes_occurred_from_visible_executions():
    """2 Worker executions historically occurred, but Attempt 5's was
    explicitly deleted (proven by Cloud Audit Logs) — the doc must state
    both facts and never claim both executions remain visible/preserved."""
    text = acceptance_doc()
    assert "milo-agent-worker-d2gfx" in text
    assert "milo-agent-worker-mcfrx" in text
    assert "2 Worker executions occurred before Attempt 7" in text
    assert "2026-08-18T02:00:48" in text
    assert "google.cloud.run.v1.Executions.DeleteExecution" in text
    # Post-Attempt-7 visible posture: exactly 2 (Attempts 6 + 7); Attempt
    # 5's deleted execution is still never counted as visible.
    assert "Worker executions visible in Cloud Run: **2**" in text
    assert "no longer visible" in text
    # The superseded both-executions-preserved claims are gone.
    assert "Worker executions: **2**, both terminal (historical, preserved)" not in text
    assert "is never cancelled or deleted" not in text


def test_acceptance_doc_distinguishes_occurred_from_present_database_rows():
    """Two attempts occurred historically, but only Attempt 6's run row is
    currently present in public.runs; Attempt 5's row is absent for an
    unverified reason. The doc must state both facts, never claim both
    database rows are preserved, and never invent an audited deletion of
    the row."""
    text = acceptance_doc()
    assert "exactly 1 run row" in text
    assert "absent" in text
    assert "unverified" in text
    assert "no audited deletion is claimed" in text
    # The superseded both-rows-preserved claims are gone.
    assert "holds **2 MILO runs**" not in text
    assert "MILO runs: **2**" not in text
    assert "The Attempt 5 database run row remains preserved" not in text
    assert "exactly 2 prior database runs" not in text
    assert "3 database runs" not in text
    assert "total_runs=3" not in text


def test_acceptance_doc_states_the_current_pins_and_boundaries():
    text = acceptance_doc()
    assert "STAGE C NOT PASSED" in text
    assert "88224bccc836f80f3dc1d173306a1aa63cddcc7a" in text
    assert "stage-c-smoke-attempt-7-20260819" in text
    assert "Tier 2" in text
    assert "deliberately below" in text  # envelope under the Tier 2 ceiling
    assert "Stage D remains unauthorized" in text
    # Merging the preparation PR must never read as run authorization.
    assert "authorizes nothing" in text


# ---------------------------------------------------------------------------
# 02-deploy-images.sh: execution-baseline and fail-closed posture gates run
# BEFORE the first production mutation and are repeated after deployment
# (mocked gcloud — no network, no real mutation)
# ---------------------------------------------------------------------------


DEPLOY_WORKER_FAIL_CLOSED = {"spec": {"template": {"spec": {"template": {"spec": {"containers": [{"env": [
    {"name": "MILO_ENABLE_PAID_EXECUTION", "value": "false"},
]}]}}}}}}

DEPLOY_API_FAIL_CLOSED = {"spec": {"template": {"spec": {"containers": [{"env": [
    {"name": "MILO_ENABLE_RUN_CREATION", "value": "false"},
    {"name": "JOB_LAUNCHER", "value": "disabled"},
    {"name": "MILO_ENABLE_PAID_EXECUTION", "value": "false"},
]}]}}}}


def deploy_worker_with(extra_entries):
    spec = json.loads(json.dumps(DEPLOY_WORKER_FAIL_CLOSED))
    spec["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["env"].extend(extra_entries)
    return spec


def deploy_api_with(extra_entries):
    spec = json.loads(json.dumps(DEPLOY_API_FAIL_CLOSED))
    spec["spec"]["template"]["spec"]["containers"][0]["env"].extend(extra_entries)
    return spec


DEPLOY_MOCK_GCLOUD = r"""#!/usr/bin/env bash
args="$*"
printf '%s\n' "${args}" >> "${MOCK_LOG}"
serve() { # base-name — serves the -after variant once a mutation happened
  if [ -e "${MOCK_DIR}/deployed" ] && [ -f "${MOCK_DIR}/${1}-after.json" ]; then
    cat "${MOCK_DIR}/${1}-after.json"
  else
    cat "${MOCK_DIR}/${1}.json"
  fi
}
case "${args}" in
  *"jobs update"*|*"services update"*)
    touch "${MOCK_DIR}/deployed"
    exit 0 ;;
  *"executions list"*)
    serve executions ;;
  *"services describe"*"value(status.conditions"*)
    cat "${MOCK_DIR}/ready.txt" ;;
  *"services describe"*)
    serve api ;;
  *"jobs describe"*)
    serve worker ;;
  *)
    echo "unexpected gcloud invocation: ${args}" >&2
    exit 9 ;;
esac
"""


def run_deploy(tmp_path, *, executions=None, worker=None, api=None,
               executions_after=None, worker_after=None, api_after=None, ready="True"):
    mock_dir = tmp_path / "mock"
    mock_dir.mkdir(exist_ok=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (mock_dir / "executions.json").write_text(json.dumps(VISIBLE_BASELINE if executions is None else executions))
    (mock_dir / "worker.json").write_text(json.dumps(DEPLOY_WORKER_FAIL_CLOSED if worker is None else worker))
    (mock_dir / "api.json").write_text(json.dumps(DEPLOY_API_FAIL_CLOSED if api is None else api))
    if executions_after is not None:
        (mock_dir / "executions-after.json").write_text(json.dumps(executions_after))
    if worker_after is not None:
        (mock_dir / "worker-after.json").write_text(json.dumps(worker_after))
    if api_after is not None:
        (mock_dir / "api-after.json").write_text(json.dumps(api_after))
    (mock_dir / "ready.txt").write_text(f"{ready}\n")
    gcloud = bin_dir / "gcloud"
    gcloud.write_text(DEPLOY_MOCK_GCLOUD)
    gcloud.chmod(0o755)
    log = mock_dir / "gcloud.log"
    log.write_text("")
    result = subprocess.run(
        ["bash", str(STAGE_C / "02-deploy-images.sh")],
        capture_output=True,
        text=True,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "MOCK_DIR": str(mock_dir), "MOCK_LOG": str(log)},
        timeout=120,
    )
    return result, log.read_text().splitlines()


def mutation_lines(log_lines):
    return [line for line in log_lines if "jobs update" in line or "services update" in line]


def test_deploy_gates_run_before_any_mutation_and_again_after(tmp_path):
    result, log = run_deploy(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK: release images deployed" in result.stdout
    mutations = mutation_lines(log)
    assert len(mutations) == 2  # worker image + API image, nothing else
    first_mutation = log.index(mutations[0])
    # BOTH read-only gates completed before the first mutating call.
    pre = log[:first_mutation]
    assert any("executions list" in line for line in pre)
    assert any("jobs describe" in line for line in pre)
    assert any("services describe" in line for line in pre)
    # And BOTH gates ran again after the last mutating call.
    post = log[log.index(mutations[1]) + 1:]
    assert any("executions list" in line for line in post)
    assert any("jobs describe" in line for line in post)
    assert sum("services describe" in line for line in post) >= 2  # Ready + posture
    assert result.stdout.count("pre-deploy") >= 2
    assert result.stdout.count("post-deploy") >= 2


@pytest.mark.parametrize("listing", [
    [],  # 0 — the one visible execution vanished too?
    TWO_TERMINAL_EXECUTIONS,  # 2 — the deleted Attempt 5 execution cannot reappear
    [{"metadata": {"name": "milo-agent-worker-live1"}, "status": {"completionTime": None}}],  # active
    "this is not json",
])
def test_deploy_blocks_before_mutation_on_execution_baseline_violations(tmp_path, listing):
    executions = listing if isinstance(listing, list) else None
    if isinstance(listing, str):
        result, log = run_deploy_raw_listing(tmp_path, listing)
    else:
        result, log = run_deploy(tmp_path, executions=executions)
    assert result.returncode != 0
    assert "BLOCKED (pre-deploy)" in result.stdout
    assert mutation_lines(log) == [], "production was mutated despite a failed pre-gate"


def run_deploy_raw_listing(tmp_path, raw):
    result, log = run_deploy(tmp_path)  # builds the mock tree (happy files)
    (tmp_path / "mock" / "executions.json").write_text(raw)
    (tmp_path / "mock" / "gcloud.log").write_text("")
    deployed = tmp_path / "mock" / "deployed"
    if deployed.exists():
        deployed.unlink()
    rerun = subprocess.run(
        ["bash", str(STAGE_C / "02-deploy-images.sh")],
        capture_output=True,
        text=True,
        env={"PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin", "MOCK_DIR": str(tmp_path / "mock"), "MOCK_LOG": str(tmp_path / "mock" / "gcloud.log")},
        timeout=120,
    )
    return rerun, (tmp_path / "mock" / "gcloud.log").read_text().splitlines()


def test_deploy_blocks_before_mutation_when_worker_is_not_fail_closed(tmp_path):
    hot_worker = deploy_worker_with([])
    hot_worker["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["env"] = [
        {"name": "MILO_ENABLE_PAID_EXECUTION", "value": "true"},
    ]
    result, log = run_deploy(tmp_path, worker=hot_worker)
    assert result.returncode != 0
    assert "BLOCKED (pre-deploy): worker is not fail-closed" in result.stdout
    assert mutation_lines(log) == []


@pytest.mark.parametrize("bad_env", [
    [{"name": "MILO_ENABLE_RUN_CREATION", "value": "true"}, {"name": "JOB_LAUNCHER", "value": "disabled"}],
    [{"name": "MILO_ENABLE_RUN_CREATION", "value": "false"}, {"name": "JOB_LAUNCHER", "value": "cloud_run"}],
    [{"name": "MILO_ENABLE_RUN_CREATION", "value": "false"}, {"name": "JOB_LAUNCHER", "value": "disabled"}, {"name": "MILO_ENABLE_PAID_EXECUTION", "value": "true"}],
])
def test_deploy_blocks_before_mutation_when_api_is_not_fail_closed(tmp_path, bad_env):
    bad_api = {"spec": {"template": {"spec": {"containers": [{"env": bad_env}]}}}}
    result, log = run_deploy(tmp_path, api=bad_api)
    assert result.returncode != 0
    assert "BLOCKED (pre-deploy): API is not fail-closed" in result.stdout
    assert mutation_lines(log) == []


def secret_binding(name):
    return {"name": name, "valueFrom": {"secretKeyRef": {"name": name}}}


def literal_value(name):
    return {"name": name, "value": "unit-test-literal-value"}


@pytest.mark.parametrize("entry", [
    secret_binding("KIMI_API_KEY"), secret_binding("MOONSHOT_API_KEY"),
    literal_value("KIMI_API_KEY"), literal_value("MOONSHOT_API_KEY"),
])
def test_deploy_blocks_on_provider_alias_on_worker_without_printing_values(tmp_path, entry):
    result, log = run_deploy(tmp_path, worker=deploy_worker_with([entry]))
    assert result.returncode != 0
    assert "BLOCKED (pre-deploy): worker is not fail-closed" in result.stdout
    assert mutation_lines(log) == []
    assert "unit-test-literal-value" not in result.stdout + result.stderr
    assert "secretKeyRef" not in result.stdout + result.stderr


@pytest.mark.parametrize("entry", [
    secret_binding("KIMI_API_KEY"), secret_binding("MOONSHOT_API_KEY"),
    literal_value("KIMI_API_KEY"), literal_value("MOONSHOT_API_KEY"),
])
def test_deploy_blocks_on_provider_alias_on_api_without_printing_values(tmp_path, entry):
    result, log = run_deploy(tmp_path, api=deploy_api_with([entry]))
    assert result.returncode != 0
    assert "BLOCKED (pre-deploy): API is not fail-closed" in result.stdout
    assert mutation_lines(log) == []
    assert "unit-test-literal-value" not in result.stdout + result.stderr
    assert "secretKeyRef" not in result.stdout + result.stderr


def test_deploy_fails_when_the_posture_regresses_after_deployment(tmp_path):
    """Pre-gates pass, images deploy, but the post-deploy re-check finds a
    provider binding on the worker — the step exits non-zero."""
    result, log = run_deploy(tmp_path, worker_after=deploy_worker_with([secret_binding("KIMI_API_KEY")]))
    assert result.returncode != 0
    assert len(mutation_lines(log)) == 2  # the deploy did happen
    assert "BLOCKED (post-deploy): worker is not fail-closed" in result.stdout
    assert "OK: release images deployed" not in result.stdout


def test_deploy_fails_when_a_new_execution_appears_during_deployment(tmp_path):
    result, log = run_deploy(
        tmp_path,
        executions_after=VISIBLE_BASELINE + [terminal_execution("milo-agent-worker-surprise")],
    )
    assert result.returncode != 0
    assert len(mutation_lines(log)) == 2
    assert "BLOCKED (post-deploy)" in result.stdout
    assert "OK: release images deployed" not in result.stdout


def test_deploy_fails_when_api_not_ready(tmp_path):
    result, _ = run_deploy(tmp_path, ready="False")
    assert result.returncode != 0
    assert "BLOCKED: API service not Ready" in result.stdout


# ---------------------------------------------------------------------------
# 02-deploy-images.sh: the paid flag must be the LITERAL "false" — a secret
# binding or an absent flag can never pass the fail-closed gate
# ---------------------------------------------------------------------------


PAID_FLAG_BINDING = {"name": "MILO_ENABLE_PAID_EXECUTION", "valueFrom": {"secretKeyRef": {"name": "PAID_FLAG_SECRET"}}}
PAID_FLAG_FALSE = {"name": "MILO_ENABLE_PAID_EXECUTION", "value": "false"}
API_BASE_ENV = [
    {"name": "MILO_ENABLE_RUN_CREATION", "value": "false"},
    {"name": "JOB_LAUNCHER", "value": "disabled"},
]


def worker_spec_with_env(entries):
    return {"spec": {"template": {"spec": {"template": {"spec": {"containers": [{"env": entries}]}}}}}}


def api_spec_with_env(entries):
    return {"spec": {"template": {"spec": {"containers": [{"env": entries}]}}}}


@pytest.mark.parametrize("worker_env", [
    [PAID_FLAG_BINDING],                   # bound instead of literal
    [PAID_FLAG_FALSE, PAID_FLAG_BINDING],  # literal false PLUS a binding
    [],                                    # absent — no fallback allowed
])
def test_deploy_blocks_before_mutation_on_non_literal_worker_paid_flag(tmp_path, worker_env):
    result, log = run_deploy(tmp_path, worker=worker_spec_with_env(worker_env))
    assert result.returncode != 0
    assert "BLOCKED (pre-deploy): worker is not fail-closed" in result.stdout
    assert mutation_lines(log) == [], "production was mutated despite a non-literal paid flag"
    combined = result.stdout + result.stderr
    assert "secretKeyRef" not in combined
    assert "PAID_FLAG_SECRET" not in combined


@pytest.mark.parametrize("api_env", [
    API_BASE_ENV + [PAID_FLAG_BINDING],
    API_BASE_ENV + [PAID_FLAG_FALSE, PAID_FLAG_BINDING],
    API_BASE_ENV,  # paid flag absent — no fallback allowed
])
def test_deploy_blocks_before_mutation_on_non_literal_api_paid_flag(tmp_path, api_env):
    result, log = run_deploy(tmp_path, api=api_spec_with_env(api_env))
    assert result.returncode != 0
    assert "BLOCKED (pre-deploy): API is not fail-closed" in result.stdout
    assert mutation_lines(log) == [], "production was mutated despite a non-literal paid flag"
    combined = result.stdout + result.stderr
    assert "secretKeyRef" not in combined
    assert "PAID_FLAG_SECRET" not in combined


def test_deploy_fails_when_paid_flag_becomes_secret_bound_after_deployment(tmp_path):
    """The literal-only rule is re-enforced by the post-deploy gate too."""
    result, log = run_deploy(
        tmp_path,
        worker_after=worker_spec_with_env([PAID_FLAG_BINDING]),
    )
    assert result.returncode != 0
    assert len(mutation_lines(log)) == 2  # deploy happened, then the gate caught it
    assert "BLOCKED (post-deploy): worker is not fail-closed" in result.stdout
    assert "OK: release images deployed" not in result.stdout
    assert "secretKeyRef" not in result.stdout + result.stderr


def test_deploy_gate_requires_the_exact_literal_false():
    """No-fallback proof at the source level: the deploy gate never reads
    the paid flag with a default."""
    text = (STAGE_C / "02-deploy-images.sh").read_text()
    assert 'values.get("MILO_ENABLE_PAID_EXECUTION", "false")' not in text
    assert text.count('"MILO_ENABLE_PAID_EXECUTION" not in secret_refs') == 2  # worker + API
    assert text.count('values.get("MILO_ENABLE_PAID_EXECUTION") == "false"') == 2

"""Stage C production DB probe (runs inside `stagec-db-probe` Cloud Run job).

Runs AS the API runtime service account with SUPABASE_URL and
SUPABASE_SERVICE_ROLE_KEY bound from Secret Manager (that identity's
existing access — no new grants). Talks to PostgREST/auth-admin with the
Python standard library only, prints structured JSON evidence and NEVER
prints a secret value.

Modes (env STAGE_C_MODE):
  preflight — verify migration-dependent schema/RPC surface + zero runs
  setup     — idempotently create the operator test user, dedicated
              stage-c-smoke project, membership and conversation
  evidence  — full post-run evidence for env STAGE_C_RUN_ID
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ["SUPABASE_URL"].rstrip("/")
KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
TEST_EMAIL = os.environ.get("STAGE_C_TEST_EMAIL", "stage-c-smoke@invalid.milo")
PROJECT_SLUG = "stage-c-smoke"


def call(method: str, path: str, body: dict | list | None = None, headers: dict | None = None):
    request = urllib.request.Request(
        BASE + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "apikey": KEY,
            "Authorization": f"Bearer {KEY}",
            "Content-Type": "application/json",
            **(headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return exc.code, {"raw": raw.decode(errors="replace")[:300]}


def fail(reason: str) -> None:
    print(json.dumps({"stage_c_probe": "BLOCKED", "reason": reason}))
    sys.exit(1)


def preflight() -> None:
    checks: dict[str, str] = {}
    # Migration 012/000400: transactional run-creation RPC must exist.
    status, body = call("POST", "/rest/v1/rpc/create_message_and_run_v2", {})
    checks["rpc_create_message_and_run_v2"] = "present" if status != 404 else "MISSING"
    # Migration 000600: attempt-aware reservation identity.
    status, _ = call("GET", "/rest/v1/model_call_budget_reservations?select=attempt&limit=1")
    checks["reservations_attempt_column"] = "present" if status == 200 else "MISSING"
    # Migration 012: lease tokens on runs.
    status, _ = call("GET", "/rest/v1/runs?select=lease_token,launch_state&limit=1")
    checks["runs_lease_columns"] = "present" if status == 200 else "MISSING"
    # Migration 000300/000600 guarded RPC surface.
    for rpc in ("transition_run_worker_guarded", "heartbeat_run_guarded", "update_run_usage_guarded", "settle_model_call_budget_guarded"):
        status, _ = call("POST", f"/rest/v1/rpc/{rpc}", {})
        checks[f"rpc_{rpc}"] = "present" if status != 404 else "MISSING"
    # Ledger table (013/000500).
    status, _ = call("GET", "/rest/v1/run_usage_ledger?select=id&limit=1")
    checks["run_usage_ledger"] = "present" if status == 200 else "MISSING"
    # Zero prior runs (Stage C precondition).
    status, rows = call("GET", "/rest/v1/runs?select=id", headers={"Prefer": "count=exact", "Range": "0-0"})
    checks["existing_runs"] = str(len(rows or []))
    missing = {k: v for k, v in checks.items() if v == "MISSING"}
    print(json.dumps({"stage_c_probe": "preflight", "checks": checks, "ok": not missing}))
    if missing:
        sys.exit(1)


def setup() -> None:
    # 1. Operator-controlled test user (admin API; no password flow used).
    status, body = call("POST", "/auth/v1/admin/users", {"email": TEST_EMAIL, "email_confirm": True})
    if status in (200, 201):
        user_id = body["id"]
    else:
        status, body = call("GET", "/auth/v1/admin/users?per_page=1000")
        users = body.get("users", body) if isinstance(body, dict) else body
        matches = [u for u in users if u.get("email") == TEST_EMAIL]
        if not matches:
            fail(f"test user creation failed (HTTP {status}) and no existing user found")
        user_id = matches[0]["id"]
    # 2. Dedicated project (only the test user will be a member).
    status, body = call(
        "POST",
        "/rest/v1/projects",
        {
            "slug": PROJECT_SLUG,
            "name": "Stage C smoke",
            "description": "Operator-controlled Stage C smoke-test project",
            "workflow_key": "vehicle_catalog_v1",
            "configuration": {"stage": "stage-c"},
        },
        headers={"Prefer": "return=representation"},
    )
    if status == 201:
        project_id = body[0]["id"]
    else:
        status, rows = call("GET", f"/rest/v1/projects?slug=eq.{PROJECT_SLUG}&select=id")
        if status != 200 or not rows:
            fail(f"project create/lookup failed (HTTP {status})")
        project_id = rows[0]["id"]
    # 3. Membership (idempotent upsert).
    status, _ = call(
        "POST",
        "/rest/v1/project_members",
        {"project_id": project_id, "user_id": user_id, "role": "owner"},
        headers={"Prefer": "resolution=merge-duplicates"},
    )
    if status not in (200, 201):
        fail(f"membership upsert failed (HTTP {status})")
    # 4. Conversation (reuse if present).
    status, rows = call("GET", f"/rest/v1/conversations?project_id=eq.{project_id}&select=id&limit=1")
    if status == 200 and rows:
        conversation_id = rows[0]["id"]
    else:
        status, body = call(
            "POST",
            "/rest/v1/conversations",
            {"project_id": project_id, "title": "stage-c-smoke"},
            headers={"Prefer": "return=representation"},
        )
        if status != 201:
            fail(f"conversation creation failed (HTTP {status})")
        conversation_id = body[0]["id"]
    print(json.dumps({
        "stage_c_probe": "setup",
        "user_id": user_id,
        "project_id": project_id,
        "conversation_id": conversation_id,
        "members": "test user only",
    }))


SECRET_MARKERS = ("sk-", "KIMI_API_KEY", "MOONSHOT_API_KEY", "service_role", "sb_secret")


def evidence() -> None:
    run_id = os.environ["STAGE_C_RUN_ID"]
    out: dict = {"stage_c_probe": "evidence", "run_id": run_id}
    # Run row (lease token excluded from output).
    _, rows = call(
        "GET",
        f"/rest/v1/runs?id=eq.{run_id}"
        "&select=id,status,attempt,worker_id,launch_state,started_at,finished_at,"
        "last_heartbeat_at,lease_expires_at,usage,error,requested_by,idempotency_key",
    )
    if not rows:
        fail("run row not found")
    out["run"] = rows[0]
    # Total runs — must be exactly 1 (no duplicate execution).
    _, allruns = call("GET", "/rest/v1/runs?select=id,status")
    out["total_runs"] = len(allruns or [])
    # Lifecycle events.
    _, events = call("GET", f"/rest/v1/run_events?run_id=eq.{run_id}&select=event_type,agent,phase,created_at,payload&order=id.asc")
    out["event_count"] = len(events or [])
    out["event_types"] = [e["event_type"] for e in (events or [])]
    # Secret-leak scan over event payloads/messages (counts only).
    blob = json.dumps(events or [])
    out["secret_marker_hits"] = {m: blob.count(m) for m in SECRET_MARKERS if blob.count(m)}
    # Checkpoints.
    _, checkpoints = call("GET", f"/rest/v1/run_checkpoints?run_id=eq.{run_id}&select=phase,attempt,created_at&order=created_at.asc")
    out["checkpoints"] = [c["phase"] for c in (checkpoints or [])]
    # Heartbeats.
    _, beats = call("GET", f"/rest/v1/worker_heartbeats?run_id=eq.{run_id}&select=worker_id,attempt,heartbeat_at&order=heartbeat_at.desc")
    out["heartbeat_count"] = len(beats or [])
    out["latest_heartbeat"] = (beats or [{}])[0]
    # Budget reservations: settled vs dangling.
    _, reservations = call("GET", f"/rest/v1/model_call_budget_reservations?run_id=eq.{run_id}&select=call_seq,attempt,status,estimated_cost,actual_cost")
    reservations = reservations or []
    by_status: dict[str, int] = {}
    for r in reservations:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    out["reservations_total"] = len(reservations)
    out["reservations_by_status"] = by_status
    out["dangling_reservations"] = by_status.get("reserved", 0)
    out["reserved_cost_sum"] = round(sum(float(r["estimated_cost"] or 0) for r in reservations), 6)
    out["settled_cost_sum"] = round(sum(float(r["actual_cost"] or 0) for r in reservations if r["status"] in ("settled", "overage")), 6)
    # Usage ledger decisions.
    _, ledger = call("GET", f"/rest/v1/run_usage_ledger?run_id=eq.{run_id}&select=decision,call_seq,actual_input_tokens,actual_output_tokens,actual_cost,estimated_cost")
    ledger = ledger or []
    decisions: dict[str, int] = {}
    for row in ledger:
        decisions[row["decision"]] = decisions.get(row["decision"], 0) + 1
    out["ledger_decisions"] = decisions
    out["ledger_actual_cost_sum"] = round(sum(float(r["actual_cost"] or 0) for r in ledger), 6)
    out["ledger_input_tokens"] = sum(int(r["actual_input_tokens"] or 0) for r in ledger)
    out["ledger_output_tokens"] = sum(int(r["actual_output_tokens"] or 0) for r in ledger)
    # Launch invocation audit.
    _, invocations = call("GET", f"/rest/v1/run_invocations?run_id=eq.{run_id}&select=created_at,invocation")
    out["invocations"] = len(invocations or [])
    print(json.dumps(out, default=str))


def main() -> None:
    mode = os.environ.get("STAGE_C_MODE", "preflight")
    if mode == "preflight":
        preflight()
    elif mode == "setup":
        setup()
    elif mode == "evidence":
        evidence()
    else:
        fail(f"unknown STAGE_C_MODE {mode!r}")


if __name__ == "__main__":
    main()

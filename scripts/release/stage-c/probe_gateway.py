"""Stage C gateway probe (runs inside `stagec-gw-probe` Cloud Run job).

Runs AS the approved Vercel gateway service account with NO secrets bound.
Mints a Google-signed identity token for the private API from the metadata
server (exactly like the real gateway) and drives the canonical path:
POST /conversations/{id}/runs → launcher → Cloud Run worker. Prints
structured JSON only; the identity token is never printed.

Modes (env STAGE_C_MODE):
  create — create the ONE run (fixed idempotency key) and immediately
           verify the idempotent replay returns the same run id
  poll   — poll run status through the API until a terminal state; exits 0
           ONLY if the terminal state is in the Stage C acceptance policy
           (STAGE_C_ACCEPTABLE_TERMINAL_STATES, default "completed");
           any other terminal state exits non-zero and instructs the
           operator to run the kill switch
  replay — post-completion replay with the same key; must return the same
           run id and create no new run

Exit codes: 0 = PASS, 1 = infrastructure/timeout failure, 2 = the run
reached a terminal state that the acceptance policy does not accept.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

API = os.environ["STAGE_C_API_URL"].rstrip("/")
USER_ID = os.environ["STAGE_C_USER_ID"]
CONVERSATION_ID = os.environ["STAGE_C_CONVERSATION_ID"]
# Fallback matches the pinned Attempt 7 key in stage-c-env.sh; the step
# scripts always transport the pinned value explicitly. The Attempt 5/6
# key (stage-c-smoke-0001) is consumed history and is never reused.
IDEMPOTENCY_KEY = os.environ.get("STAGE_C_IDEMPOTENCY_KEY", "stage-c-smoke-attempt-7-20260819")

# Every state the run lifecycle can terminate in.
TERMINAL = {"completed", "partial_success", "failed", "cancelled", "timed_out", "budget_exhausted"}

KILL_SWITCH_ACTION = (
    "RUN THE KILL SWITCH NOW: scripts/release/stage-c/kill-switch.sh — then "
    "collect evidence with 06-collect-evidence.sh and record the failure in "
    "STAGE_C_ACCEPTANCE.md. Do NOT start another run."
)

RUN_REQUEST = {
    "content": "Stage C production smoke test - vehicle catalog run",
    "metadata": {"stage": "stage-c-smoke"},
    "idempotency_key": IDEMPOTENCY_KEY,
}


def acceptable_terminal_states() -> set[str]:
    """Stage C acceptance policy: ONLY these terminal states are a PASS.

    `failed`, `cancelled`, `timed_out`, `budget_exhausted` and
    `partial_success` are controlled fail-closed terminals — they prove the
    safety rails, but they are NOT an acceptable smoke-test outcome unless
    the operator explicitly widens the policy via the env var.
    """
    raw = os.environ.get("STAGE_C_ACCEPTABLE_TERMINAL_STATES", "completed")
    states = {s.strip() for s in raw.split(",") if s.strip()}
    return states & TERMINAL


def identity_token() -> str:
    request = urllib.request.Request(
        "http://metadata.google.internal/computeMetadata/v1/instance/"
        f"service-accounts/default/identity?audience={API}",
        headers={"Metadata-Flavor": "Google"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.read().decode()


def call(method: str, path: str, body: dict | None = None):
    token = identity_token()
    request = urllib.request.Request(
        API + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "X-Milo-Gateway-Token": token,
            "x-milo-auth-user-id": USER_ID,
            "x-milo-auth-user-email": "stage-c-smoke@invalid.milo",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return exc.code, {"raw": raw.decode(errors="replace")[:300]}


def create() -> None:
    status, body = call("GET", "/health")
    if status != 200:
        print(json.dumps({"stage_c_probe": "create", "ok": False, "step": "health", "http": status, "body": body}))
        sys.exit(1)
    status, first = call("POST", f"/conversations/{CONVERSATION_ID}/runs", RUN_REQUEST)
    if status != 202:
        print(json.dumps({"stage_c_probe": "create", "ok": False, "step": "create", "http": status, "body": first}))
        sys.exit(1)
    status2, replay = call("POST", f"/conversations/{CONVERSATION_ID}/runs", RUN_REQUEST)
    same = status2 == 202 and replay and replay.get("run_id") == first.get("run_id")
    print(json.dumps({
        "stage_c_probe": "create",
        "ok": bool(same),
        "run_id": first.get("run_id"),
        "status": first.get("status"),
        "immediate_replay": {"http": status2, "same_run": bool(same)},
    }))
    if not same:
        sys.exit(1)


def poll() -> None:
    run_id = os.environ["STAGE_C_RUN_ID"]
    deadline = time.time() + int(os.environ.get("STAGE_C_POLL_SECONDS", "3500"))
    interval = int(os.environ.get("STAGE_C_POLL_INTERVAL_SECONDS", "20"))
    acceptable = acceptable_terminal_states()
    last = None
    while time.time() < deadline:
        status, run = call("GET", f"/runs/{run_id}")
        if status == 200 and run:
            state = run.get("status")
            if state != last:
                print(json.dumps({"stage_c_probe": "poll", "status": state, "usage": run.get("usage")}), flush=True)
                last = state
            if state in TERMINAL:
                is_pass = state in acceptable
                print(json.dumps({
                    "stage_c_probe": "poll",
                    "terminal": state,
                    "acceptable": is_pass,
                    "acceptance_policy": sorted(acceptable),
                    "run": {
                        "status": state,
                        "attempt": run.get("attempt"),
                        "started_at": run.get("started_at"),
                        "finished_at": run.get("finished_at"),
                        "usage": run.get("usage"),
                        "error": run.get("error"),
                    },
                }), flush=True)
                if is_pass:
                    return
                print(json.dumps({
                    "stage_c_probe": "poll",
                    "verdict": "FAIL",
                    "reason": (
                        f"terminal state {state!r} is NOT in the Stage C acceptance policy "
                        f"{sorted(acceptable)} — this smoke run FAILED"
                    ),
                    "operator_action": KILL_SWITCH_ACTION,
                }), flush=True)
                sys.exit(2)
        time.sleep(interval)
    print(json.dumps({
        "stage_c_probe": "poll",
        "terminal": None,
        "verdict": "FAIL",
        "reason": "poll timeout — the run never reached a terminal state within the window",
        "operator_action": KILL_SWITCH_ACTION,
    }), flush=True)
    sys.exit(1)


def replay() -> None:
    run_id = os.environ["STAGE_C_RUN_ID"]
    status, body = call("POST", f"/conversations/{CONVERSATION_ID}/runs", RUN_REQUEST)
    same = status == 202 and body and body.get("run_id") == run_id
    print(json.dumps({
        "stage_c_probe": "replay",
        "ok": bool(same),
        "http": status,
        "returned_run_id": (body or {}).get("run_id"),
        "expected_run_id": run_id,
        "returned_status": (body or {}).get("status"),
    }))
    if not same:
        sys.exit(1)


def main() -> None:
    mode = os.environ.get("STAGE_C_MODE", "create")
    if mode == "create":
        create()
    elif mode == "poll":
        poll()
    elif mode == "replay":
        replay()
    else:
        print(json.dumps({"stage_c_probe": "error", "reason": f"unknown mode {mode!r}"}))
        sys.exit(1)


if __name__ == "__main__":
    main()

"""Stage D gateway probe (runs inside `stage-d-gw-probe` Cloud Run job).

Runs AS the approved Vercel gateway service account with NO secrets bound.
Mints a Google-signed identity token for the private API from the metadata
server (exactly like the real gateway) and drives the canonical path:
POST /conversations/{id}/runs -> launcher -> Cloud Run worker. Prints
structured JSON only; the identity token is never printed.

This is the ONLY caller that can reach run creation while Stage D is
enabled: the Vercel/browser execution surface stays disabled
(GATEWAY_ALLOW_EXECUTION_ROUTES is never turned on), so the operator-
controlled probe identity plus project membership confine run creation to
the dedicated stage-d-smoke user/project/conversation.

Modes (env STAGE_D_MODE):
  create — create the ONE run (fixed idempotency key) and immediately
           verify the idempotent replay returns the same run id
  poll   — poll run status through the API until a terminal state; exits 0
           ONLY if the terminal state is in the Stage D acceptance policy
           (STAGE_D_ACCEPTABLE_TERMINAL_STATES, default "completed");
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

API = os.environ["STAGE_D_API_URL"].rstrip("/")
USER_ID = os.environ["STAGE_D_USER_ID"]
CONVERSATION_ID = os.environ["STAGE_D_CONVERSATION_ID"]
TEST_EMAIL = os.environ.get("STAGE_D_TEST_EMAIL", "stage-d-smoke@invalid.milo")
# No fallback: the authorized key is transported explicitly by every step
# script from stage-d-env.sh. A probe that invented a key could create a
# run outside the one authorized identity, so a missing key is fatal.
IDEMPOTENCY_KEY = os.environ["STAGE_D_IDEMPOTENCY_KEY"]

# The prepared Government capture's identity. The Stage D run must never
# borrow it — doing so would replay the capture instead of creating a new
# run. Checked here as well as in probe_db.py: the two probes run under
# different identities, and neither is allowed to assume the other ran.
GOV_CAPTURE_KEY = os.environ.get("STAGE_D_GOV_CAPTURE_KEY", "")
GOV_CAPTURE_RUN_ID = os.environ.get("STAGE_D_GOV_CAPTURE_RUN_ID", "")
GOV_CAPTURE_OPERATION = "catalog.government.capture"

# Every state the run lifecycle can terminate in.
TERMINAL = {"completed", "partial_success", "failed", "cancelled", "timed_out", "budget_exhausted"}

KILL_SWITCH_ACTION = (
    "RUN THE KILL SWITCH NOW: scripts/release/stage-d/kill-switch.sh — then "
    "collect evidence with 06-collect-evidence.sh and record the failure in "
    "docs/production-readiness/STAGE_D_AUTHORIZATION.md. Do NOT start another run."
)

# The run request carries NO catalog/capture marker: `metadata` reaches
# `input.metadata`, and `milo_operation: catalog.government.capture` is the
# operator-capture marker. Stage D is an ordinary bounded model run.
RUN_REQUEST = {
    "content": "Stage D expansion step 1 - bounded production vehicle catalog run",
    "metadata": {"stage": "stage-d-smoke"},
    "idempotency_key": IDEMPOTENCY_KEY,
}


def refuse(reason: str) -> None:
    print(json.dumps({"stage_d_probe": "BLOCKED", "ok": False, "reason": reason}))
    sys.exit(1)


def assert_not_the_capture_identity() -> None:
    """Fail closed before any API call if the identities collide."""
    if GOV_CAPTURE_KEY and IDEMPOTENCY_KEY == GOV_CAPTURE_KEY:
        refuse(
            "STAGE_D_IDEMPOTENCY_KEY equals the Government capture key — creating this run would "
            "replay the prepared capture; refusing before any API call"
        )
    metadata = RUN_REQUEST.get("metadata") or {}
    if metadata.get("milo_operation") == GOV_CAPTURE_OPERATION:
        refuse("the Stage D run request carries the operator-capture marker; refusing before any API call")


def acceptable_terminal_states() -> set[str]:
    """Stage D acceptance policy: ONLY these terminal states are a PASS.

    `failed`, `cancelled`, `timed_out`, `budget_exhausted` and
    `partial_success` are controlled fail-closed terminals — they prove the
    safety rails, but they are NOT an acceptable outcome unless the
    operator explicitly widens the policy via the env var.
    """
    raw = os.environ.get("STAGE_D_ACCEPTABLE_TERMINAL_STATES", "completed")
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
            "x-milo-auth-user-email": TEST_EMAIL,
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
    assert_not_the_capture_identity()
    status, body = call("GET", "/health")
    if status != 200:
        print(json.dumps({"stage_d_probe": "create", "ok": False, "step": "health", "http": status, "body": body}))
        sys.exit(1)
    status, first = call("POST", f"/conversations/{CONVERSATION_ID}/runs", RUN_REQUEST)
    if status != 202:
        print(json.dumps({"stage_d_probe": "create", "ok": False, "step": "create", "http": status, "body": first}))
        sys.exit(1)
    run_id = (first or {}).get("run_id")
    # The API must never have handed back the prepared capture run.
    if GOV_CAPTURE_RUN_ID and run_id == GOV_CAPTURE_RUN_ID:
        print(json.dumps({
            "stage_d_probe": "create",
            "ok": False,
            "step": "create",
            "reason": "run creation returned the prepared Government capture run id",
            "operator_action": KILL_SWITCH_ACTION,
        }))
        sys.exit(1)
    status2, replay_body = call("POST", f"/conversations/{CONVERSATION_ID}/runs", RUN_REQUEST)
    same = status2 == 202 and replay_body and replay_body.get("run_id") == run_id
    print(json.dumps({
        "stage_d_probe": "create",
        "ok": bool(same),
        "run_id": run_id,
        "status": (first or {}).get("status"),
        "immediate_replay": {"http": status2, "same_run": bool(same)},
    }))
    if not same:
        sys.exit(1)


def poll() -> None:
    run_id = os.environ["STAGE_D_RUN_ID"]
    # Default window: comfortably above MILO_MAX_RUN_DURATION_SECONDS=1800
    # plus launch latency, and below the 3600s Cloud Run job timeout.
    deadline = time.time() + int(os.environ.get("STAGE_D_POLL_SECONDS", "2400"))
    interval = int(os.environ.get("STAGE_D_POLL_INTERVAL_SECONDS", "20"))
    acceptable = acceptable_terminal_states()
    last = None
    while time.time() < deadline:
        status, run = call("GET", f"/runs/{run_id}")
        if status == 200 and run:
            state = run.get("status")
            if state != last:
                print(json.dumps({"stage_d_probe": "poll", "status": state, "usage": run.get("usage")}), flush=True)
                last = state
            if state in TERMINAL:
                is_pass = state in acceptable
                print(json.dumps({
                    "stage_d_probe": "poll",
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
                    "stage_d_probe": "poll",
                    "verdict": "FAIL",
                    "reason": (
                        f"terminal state {state!r} is NOT in the Stage D acceptance policy "
                        f"{sorted(acceptable)} — this run FAILED"
                    ),
                    "operator_action": KILL_SWITCH_ACTION,
                }), flush=True)
                sys.exit(2)
        time.sleep(interval)
    print(json.dumps({
        "stage_d_probe": "poll",
        "terminal": None,
        "verdict": "FAIL",
        "reason": "poll timeout — the run never reached a terminal state within the window",
        "operator_action": KILL_SWITCH_ACTION,
    }), flush=True)
    sys.exit(1)


def replay() -> None:
    assert_not_the_capture_identity()
    run_id = os.environ["STAGE_D_RUN_ID"]
    status, body = call("POST", f"/conversations/{CONVERSATION_ID}/runs", RUN_REQUEST)
    same = status == 202 and body and body.get("run_id") == run_id
    print(json.dumps({
        "stage_d_probe": "replay",
        "ok": bool(same),
        "http": status,
        "returned_run_id": (body or {}).get("run_id"),
        "expected_run_id": run_id,
        "returned_status": (body or {}).get("status"),
    }))
    if not same:
        sys.exit(1)


def main() -> None:
    mode = os.environ.get("STAGE_D_MODE", "create")
    if mode == "create":
        create()
    elif mode == "poll":
        poll()
    elif mode == "replay":
        replay()
    else:
        print(json.dumps({"stage_d_probe": "error", "ok": False, "reason": f"unknown mode {mode!r}"}))
        sys.exit(1)


if __name__ == "__main__":
    main()

"""Stage C gateway probe (runs inside `stagec-gw-probe` Cloud Run job).

Runs AS the approved Vercel gateway service account with NO secrets bound.
Mints a Google-signed identity token for the private API from the metadata
server (exactly like the real gateway) and drives the canonical path:
POST /conversations/{id}/runs → launcher → Cloud Run worker. Prints
structured JSON only; the identity token is never printed.

Modes (env STAGE_C_MODE):
  create — create the ONE run (fixed idempotency key) and immediately
           verify the idempotent replay returns the same run id
  poll   — poll run status through the API until a terminal state
  replay — post-completion replay with the same key; must return the same
           run id and create no new run
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
IDEMPOTENCY_KEY = os.environ.get("STAGE_C_IDEMPOTENCY_KEY", "stage-c-smoke-0001")
TERMINAL = {"completed", "partial_success", "failed", "cancelled", "timed_out", "budget_exhausted"}

RUN_REQUEST = {
    "content": "Stage C production smoke test - vehicle catalog run",
    "metadata": {"stage": "stage-c-smoke"},
    "idempotency_key": IDEMPOTENCY_KEY,
}


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
    last = None
    while time.time() < deadline:
        status, run = call("GET", f"/runs/{run_id}")
        if status == 200 and run:
            state = run.get("status")
            if state != last:
                print(json.dumps({"stage_c_probe": "poll", "status": state, "usage": run.get("usage")}), flush=True)
                last = state
            if state in TERMINAL:
                print(json.dumps({"stage_c_probe": "poll", "terminal": state, "run": {
                    "status": state,
                    "attempt": run.get("attempt"),
                    "started_at": run.get("started_at"),
                    "finished_at": run.get("finished_at"),
                    "usage": run.get("usage"),
                    "error": run.get("error"),
                }}))
                return
        time.sleep(20)
    print(json.dumps({"stage_c_probe": "poll", "terminal": None, "reason": "poll timeout"}))
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

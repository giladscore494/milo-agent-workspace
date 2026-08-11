"""Upstash-REST-compatible rate-limit store for isolated Stage B staging.

Implements exactly the protocol surface MILO's rate limiters use
(`backend/rate_limit.py`, `frontend/lib/server/rateLimit.ts`): the
`POST /pipeline` endpoint with INCR / PEXPIRE (NX) / PTTL plus a few basics,
authenticated with `Authorization: Bearer <token>`. Deployed as a single
Cloud Run instance (min=max=1) so its in-memory keyspace is genuinely shared
across every MILO API instance — which is the property the Stage B rate-limit
acceptance tests need. TLS comes from Cloud Run's HTTPS endpoint.

Staging-only. Never deployed to production; production uses real Upstash.
The keyspace only ever contains `rl:<category>:<sha256-prefix>` counters —
no private data. State is intentionally ephemeral: a restart resets
counters, which is acceptable for rate limiting.
"""

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ["SHIM_TOKEN"]  # required; no default — fail closed
PORT = int(os.getenv("PORT", "8080"))

_lock = threading.Lock()
_values: dict[str, int | str] = {}
_expiry: dict[str, float] = {}  # key -> monotonic deadline (seconds)


def _purge(key: str) -> None:
    deadline = _expiry.get(key)
    if deadline is not None and time.monotonic() >= deadline:
        _values.pop(key, None)
        _expiry.pop(key, None)


def _execute(command: list) -> object:
    if not command:
        return {"error": "empty command"}
    op = str(command[0]).upper()
    args = command[1:]
    if op == "PING":
        return "PONG"
    if op == "INCR":
        key = str(args[0])
        _purge(key)
        value = int(_values.get(key, 0)) + 1
        _values[key] = value
        return value
    if op == "PEXPIRE":
        key = str(args[0])
        _purge(key)
        if key not in _values:
            return 0
        nx = any(str(a).upper() == "NX" for a in args[2:])
        if nx and key in _expiry:
            return 0
        _expiry[key] = time.monotonic() + int(args[1]) / 1000.0
        return 1
    if op == "PTTL":
        key = str(args[0])
        _purge(key)
        if key not in _values:
            return -2
        if key not in _expiry:
            return -1
        return max(0, int((_expiry[key] - time.monotonic()) * 1000))
    if op == "GET":
        key = str(args[0])
        _purge(key)
        value = _values.get(key)
        return None if value is None else str(value)
    if op == "SET":
        key = str(args[0])
        _values[key] = str(args[1])
        _expiry.pop(key, None)
        return "OK"
    if op == "DEL":
        removed = 0
        for raw in args:
            key = str(raw)
            _purge(key)
            if key in _values:
                _values.pop(key, None)
                _expiry.pop(key, None)
                removed += 1
        return removed
    return {"error": f"unsupported command {op}"}


class Handler(BaseHTTPRequestHandler):
    server_version = "milo-staging-redis-shim/1"

    def _reply(self, status: int, body: object) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _authorized(self) -> bool:
        return self.headers.get("Authorization", "") == f"Bearer {TOKEN}"

    def do_GET(self) -> None:  # health only
        if self.path == "/health":
            self._reply(200, {"status": "ok"})
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._authorized():
            self._reply(401, {"error": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            body = json.loads(self.rfile.read(length) or b"null")
        except json.JSONDecodeError:
            self._reply(400, {"error": "invalid JSON"})
            return
        with _lock:
            if self.path == "/pipeline" and isinstance(body, list):
                self._reply(200, [{"result": _execute(command)} for command in body])
            elif self.path == "/" and isinstance(body, list):
                self._reply(200, {"result": _execute(body)})
            else:
                self._reply(400, {"error": "unsupported request"})

    def log_message(self, fmt: str, *args: object) -> None:
        # Keys are hashed identifiers; still, keep logs quiet.
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

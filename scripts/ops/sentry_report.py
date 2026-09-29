#!/usr/bin/env python3
"""Report ONE failed-workflow event to Sentry -- a no-op without SENTRY_DSN.

Used by the `if: failure()` step of .github/workflows/backup-supabase-scheduled.yml.
Standard library only (no SDK on the runner). The event carries the workflow
and job names, the run id, the commit and the event name -- never a log line,
a secret, an input or any environment value beyond those.

  sentry_report.py --workflow NAME --job NAME

Exit status is always 0: reporting a failure must never mask or replace it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
import uuid
from datetime import UTC, datetime
from urllib.parse import urlsplit

_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def parse_dsn(dsn: str) -> tuple[str, str] | None:
    """(envelope URL, public key) of a DSN, or None when it is not one."""
    parts = urlsplit((dsn or "").strip())
    project = parts.path.strip("/").rsplit("/", 1)[-1]
    if parts.scheme not in {"https", "http"} or not parts.hostname or not parts.username \
            or not project.isdigit():
        return None
    prefix = parts.path.strip("/").rsplit("/", 1)[0] if "/" in parts.path.strip("/") else ""
    host = parts.hostname + (f":{parts.port}" if parts.port else "")
    base = f"{parts.scheme}://{host}" + (f"/{prefix}" if prefix else "")
    return f"{base}/api/{project}/envelope/", parts.username


def build_event(workflow: str, job: str, env: dict[str, str]) -> dict:
    sha = env.get("GITHUB_SHA", "")
    run_id = env.get("GITHUB_RUN_ID", "")
    event_name = env.get("GITHUB_EVENT_NAME", "")
    return {
        "event_id": uuid.uuid4().hex,
        "timestamp": datetime.now(UTC).isoformat(),
        "platform": "other",
        "level": "error",
        "logger": "github-actions",
        "message": {"message": f"{workflow} / {job} failed"},
        "release": sha if re.fullmatch(r"[0-9a-f]{40}", sha) else None,
        "environment": "production",
        "tags": {
            "service": workflow,
            "job": job,
            "workflow_run_id": run_id if run_id.isdigit() else "",
            "event": event_name if _NAME.fullmatch(event_name or "") else "",
        },
    }


def main(argv: list[str] | None = None, env: dict[str, str] | None = None,
         opener=urllib.request.urlopen) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--job", required=True)
    args = parser.parse_args(argv)
    source = dict(os.environ) if env is None else env
    if not (_NAME.fullmatch(args.workflow) and _NAME.fullmatch(args.job)):
        print("sentry: workflow and job must be plain names; nothing reported")
        return 0
    dsn = (source.get("SENTRY_DSN") or "").strip()
    if not dsn or dsn.lower() in {"disabled", "off", "none", "false", "0"}:
        print("sentry: SENTRY_DSN is not configured; nothing reported")
        return 0
    parsed = parse_dsn(dsn)
    if parsed is None:
        print("sentry: SENTRY_DSN is not a valid DSN; nothing reported")
        return 0
    url, key = parsed
    event = build_event(args.workflow, args.job, source)
    body = "\n".join([
        json.dumps({"event_id": event["event_id"], "sent_at": event["timestamp"]}),
        json.dumps({"type": "event"}),
        json.dumps(event),
    ]).encode()
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/x-sentry-envelope",
        "X-Sentry-Auth": f"Sentry sentry_version=7, sentry_client=milo-ops/1, sentry_key={key}",
    })
    try:
        with opener(request, timeout=15) as response:
            status = getattr(response, "status", 200)
    except Exception as exc:  # the report must never fail the step
        print(f"sentry: report not delivered ({type(exc).__name__})")
        return 0
    print(f"sentry: failure reported (HTTP {status})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

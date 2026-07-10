"""Centralized execution-surface kill switch.

This guard runs as pure ASGI middleware, before FastAPI routing and
request-body validation. A disabled surface therefore returns a structured
403 even for empty, malformed or schema-invalid request bodies, and no
repository mutation, queued-run creation, proposal mutation, cancellation,
tool grant or ``JobLauncher.launch()`` call can happen while it is disabled.

All three flags are disabled by default and must never be enabled in
repository defaults, Dockerfiles, CI, Vercel or Cloud Run configuration
without a separately approved execution stage:

- ``MILO_ENABLE_RUN_CREATION``       gates queued-run creation.
- ``MILO_ENABLE_PROPOSAL_MUTATIONS`` gates workflow-proposal writes.
- ``MILO_ENABLE_EXECUTION_CONTROL``  gates run cancellation and the internal
  worker mutation surfaces (tool access requests/grants/usage, sources,
  claims, conflicts). These worker surfaces additionally require a
  service-to-service authorization model (for example verified Cloud Run
  service identity) before they may ever be enabled; the flag alone is not
  sufficient for production.
- ``MILO_ENABLE_PROPOSAL_READS``     gates ``GET /workflow-proposals/{id}``,
  which has no ownership relationship in the current schema (the
  ``workflow_proposals`` table carries no ``created_by`` user or project
  foreign key), so it cannot be membership-scoped yet and stays disabled.
"""

import os
import re
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from fastapi.responses import JSONResponse

_SEGMENT = r"[^/]+"

# (method, flag, path pattern, human-readable surface name)
SURFACE_RULES: tuple[tuple[str, str, re.Pattern[str], str], ...] = (
    ("POST", "MILO_ENABLE_RUN_CREATION", re.compile(rf"^/conversations/{_SEGMENT}/runs/?$"), "conversation run creation"),
    ("POST", "MILO_ENABLE_RUN_CREATION", re.compile(rf"^/workflow-proposals/{_SEGMENT}/runs/?$"), "workflow proposal run creation"),
    ("POST", "MILO_ENABLE_PROPOSAL_MUTATIONS", re.compile(r"^/workflow-proposals/?$"), "workflow proposal creation"),
    ("POST", "MILO_ENABLE_PROPOSAL_MUTATIONS", re.compile(rf"^/workflow-proposals/{_SEGMENT}/(approve|reject|revise|project)/?$"), "workflow proposal mutation"),
    ("POST", "MILO_ENABLE_EXECUTION_CONTROL", re.compile(rf"^/runs/{_SEGMENT}/(cancel|tool-access-requests|tool-grants|tool-usage|sources|claims|conflicts)/?$"), "run execution control"),
    ("GET", "MILO_ENABLE_PROPOSAL_READS", re.compile(rf"^/workflow-proposals/{_SEGMENT}/?$"), "workflow proposal read"),
)


def is_stage_enabled(flag: str) -> bool:
    return os.getenv(flag, "").strip().lower() in {"1", "true", "yes", "on"}


def find_disabled_surface(method: str, path: str) -> tuple[str, str] | None:
    """Return (flag, surface) when the request targets a disabled surface."""
    normalized = method.upper()
    for rule_method, flag, pattern, surface in SURFACE_RULES:
        if normalized == rule_method and pattern.match(path) and not is_stage_enabled(flag):
            return flag, surface
    return None


Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]


class ExecutionSurfaceGuardMiddleware:
    """Reject disabled execution surfaces before routing/validation runs."""

    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]]):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            disabled = find_disabled_surface(scope.get("method", ""), scope.get("path", ""))
            if disabled is not None:
                flag, surface = disabled
                response = JSONResponse(
                    status_code=403,
                    content={"error": {"code": "EXECUTION_SURFACE_DISABLED", "message": f"{surface} is disabled ({flag} is not enabled)"}},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)

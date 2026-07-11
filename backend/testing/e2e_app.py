"""Isolated E2E backend entrypoint (test-only).

Runs the REAL FastAPI app — same routes, same execution guard, same worker
authentication code — with test-only adapters injected at the seams that
production configures differently:

- MemoryRepository instead of Supabase (isolated per process);
- an in-process fake worker instead of a Cloud Run job (mock model adapter
  only; no paid model call is possible);
- a deterministic worker-token verifier instead of Google certificates.

Security behavior is NOT weakened: authorization, execution flags,
idempotency, budget gates and worker identity checks all run production
code. Never deploy this module.

Usage:  uvicorn backend.testing.e2e_app:app --port 8100
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any
from uuid import UUID

from backend.budget import BudgetConfig, BudgetExceeded, BudgetTracker
from backend.dependencies import get_job_launcher, get_repository
from backend.gateway_auth import get_gateway_token_verifier
from backend.main import app
from backend.testing.memory_repository import MemoryRepository
from backend.worker_auth import get_token_verifier

ALICE = "aaaaaaaa-1111-4111-8111-000000000001"
BOB = "aaaaaaaa-1111-4111-8111-000000000002"
MALLORY = "aaaaaaaa-1111-4111-8111-000000000003"
PROJECT_ALPHA = "bbbbbbbb-1111-4111-8111-000000000001"
PROJECT_BETA = "bbbbbbbb-1111-4111-8111-000000000002"

APPROVED_WORKER_SA = "e2e-worker@example-project.iam.gserviceaccount.com"
UNAPPROVED_WORKER_SA = "e2e-intruder@example-project.iam.gserviceaccount.com"
APPROVED_GATEWAY_SA = "e2e-gateway@example-project.iam.gserviceaccount.com"


def build_repository() -> MemoryRepository:
    repo = MemoryRepository()
    for user in (ALICE, BOB, MALLORY):
        repo.seed_user(user)
    repo.seed_project(PROJECT_ALPHA, "alpha-research", "Alpha Research", [ALICE])
    repo.seed_project(PROJECT_BETA, "beta-catalog", "Beta Catalog", [BOB])
    return repo


class E2ETokenVerifier:
    """Deterministic stand-in for Google ID token verification.

    Serves both boundaries; separation is still enforced by the production
    allowlist checks (a worker token on a browser route fails the gateway
    allowlist and vice versa).
    """

    def verify(self, token: str, audience: str) -> dict[str, Any]:
        claims = {
            "iss": "https://accounts.google.com",
            "aud": audience,
            "email_verified": True,
            "sub": "e2e",
        }
        if token == "e2e-valid-worker-token":
            return {**claims, "email": APPROVED_WORKER_SA}
        if token == "e2e-unapproved-worker-token":
            return {**claims, "email": UNAPPROVED_WORKER_SA}
        if token == "e2e-local-gateway-token":
            return {**claims, "email": APPROVED_GATEWAY_SA}
        raise ValueError("Could not verify token signature")


class MockUsage:
    prompt_tokens = 1000
    completion_tokens = 200


class MockModelResponse:
    usage = MockUsage()


class InProcessFakeWorkerLauncher:
    """Simulates the Cloud Run worker in a daemon thread with mocked model
    calls. Exercises polling, cancellation, budget exhaustion, timeout,
    failure and completion paths without any external service."""

    def __init__(self, repo: MemoryRepository):
        self.repo = repo

    def launch(self, run_id: UUID) -> dict[str, str]:
        thread = threading.Thread(target=self._run, args=(run_id,), daemon=True)
        thread.start()
        return {"mode": "e2e-inprocess", "run_id": str(run_id), "execution": f"e2e-{run_id}"}

    def _emit(self, run_id: UUID, event_type: str, message: str, **extra: Any) -> None:
        self.repo.append_run_event(run_id, event_type, {"message": message, **extra})

    def _run(self, run_id: UUID) -> None:
        repo = self.repo
        try:
            time.sleep(0.2)
            run = repo.get_run(run_id)
            content = str((run.get("input") or {}).get("content") or "").lower()
            repo.transition_run(run_id, "starting", started_at=run.get("started_at"))
            self._emit(run_id, "run_started", "Run started", payload={"worker": "e2e"})
            repo.transition_run(run_id, "running")

            if "timeout" in content:
                tracker = BudgetTracker(BudgetConfig(max_run_duration_seconds=1), kill_switch=lambda: True, clock=time.monotonic, event_emitter=lambda t, p: self._emit(run_id, t, p.get("message", t), payload=p.get("payload", {})))
                tracker._started_at = time.monotonic() - 5
                try:
                    tracker.before_call()
                except BudgetExceeded as exc:
                    repo.transition_run(run_id, exc.terminal_status, error={"code": exc.code, "message": exc.message}, finished_at=None, usage=tracker.snapshot())
                    return
            if "exhaust budget" in content:
                tracker = BudgetTracker(BudgetConfig(max_model_calls_per_run=2, estimated_cost_per_call=0.01), kill_switch=lambda: True, event_emitter=lambda t, p: self._emit(run_id, t, p.get("message", t), payload=p.get("payload", {})), usage_recorder=lambda usage: repo.update_run_usage(run_id, usage))
                try:
                    while True:  # every iteration is a MOCKED call, gated first
                        tracker.before_call()
                        _ = MockModelResponse()
                        tracker.after_call(MockUsage.prompt_tokens, MockUsage.completion_tokens)
                        self._emit(run_id, "agent_progress", "Mocked model call recorded", agent="researcher")
                except BudgetExceeded as exc:
                    repo.transition_run(run_id, exc.terminal_status, error={"code": exc.code, "message": exc.message}, usage=tracker.snapshot())
                    return
            if "fail" in content:
                self._emit(run_id, "run_failed", "The worker hit an internal error. Diagnostics were recorded server-side.", payload={"code": "ENGINE_FAILED"})
                repo.mark_run_failed(run_id, "ENGINE_FAILED", "The worker hit an internal error. Diagnostics were recorded server-side.")
                return

            steps = 40 if "slow" in content else 3
            for index in range(steps):
                current = repo.get_run(run_id)
                if current["status"] == "cancellation_requested":
                    self._emit(run_id, "run_cancelled", "Run cancelled", payload={"step": index})
                    repo.transition_run(run_id, "cancelled", finished_at=None)
                    return
                self._emit(run_id, "agent_progress", f"step {index + 1}/{steps}", agent="researcher", phase="research", progress={"percent": int(100 * (index + 1) / steps)})
                time.sleep(0.35 if "slow" in content else 0.15)
            self._emit(run_id, "source_recorded", "Example source", agent="researcher", payload={"id": "src-1", "title": "Example source", "domain": "example.com", "url": "https://example.com", "source_type": "web", "source_strength": "high"})
            self._emit(run_id, "run_completed", "Run completed", payload={})
            repo.mark_run_complete(run_id, {"summary": "E2E mocked output", "artifacts": {"report": "final report body"}})
        except Exception as exc:  # pragma: no cover - defensive
            try:
                repo.mark_run_failed(run_id, "E2E_WORKER_CRASH", str(exc)[:200])
            except Exception:
                pass


_repo = build_repository()
app.dependency_overrides[get_repository] = lambda: _repo
app.dependency_overrides[get_token_verifier] = lambda: E2ETokenVerifier()
app.dependency_overrides[get_gateway_token_verifier] = lambda: E2ETokenVerifier()
if os.getenv("MILO_E2E_INPROCESS_WORKER", "").lower() == "true":
    _launcher = InProcessFakeWorkerLauncher(_repo)
    app.dependency_overrides[get_job_launcher] = lambda: _launcher

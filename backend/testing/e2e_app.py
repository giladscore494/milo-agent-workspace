"""Isolated E2E backend entrypoint (test-only).

Runs the REAL FastAPI app — same routes, same execution guard, same worker
authentication code — with test-only adapters injected at the seams that
production configures differently:

- MemoryRepository instead of Supabase (isolated per process);
- an in-process fake worker instead of a Cloud Run job (mock model adapter
  only; no paid model call is possible);
- a deterministic worker-token verifier instead of Google certificates.

The fake launcher can also raise the PRODUCTION `JobLaunchUncertain`
condition on demand (see `UNCERTAIN_LAUNCH_MARKER`), so the
`launch_unknown` path is exercised through the real API handling rather
than by writing the state directly.

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
from backend.job_launcher import JobLaunchUncertain
from backend.main import app
from backend.testing.catalog_review_seed import seed_catalog_review_state
from backend.testing.memory_repository import MemoryRepository
from backend.worker_auth import get_token_verifier

ALICE = "aaaaaaaa-1111-4111-8111-000000000001"
BOB = "aaaaaaaa-1111-4111-8111-000000000002"
MALLORY = "aaaaaaaa-1111-4111-8111-000000000003"
PROJECT_ALPHA = "bbbbbbbb-1111-4111-8111-000000000001"
PROJECT_BETA = "bbbbbbbb-1111-4111-8111-000000000002"
# Swarm V2 project: the ONLY workflow whose runs reach the typed final-result
# surface. Its workflow_key is trusted project state, exactly as in production.
PROJECT_GAMMA = "bbbbbbbb-1111-4111-8111-000000000003"

APPROVED_WORKER_SA = "e2e-worker@example-project.iam.gserviceaccount.com"
UNAPPROVED_WORKER_SA = "e2e-intruder@example-project.iam.gserviceaccount.com"
APPROVED_GATEWAY_SA = "e2e-gateway@example-project.iam.gserviceaccount.com"


def build_repository() -> MemoryRepository:
    repo = MemoryRepository()
    for user in (ALICE, BOB, MALLORY):
        repo.seed_user(user)
    repo.seed_project(PROJECT_ALPHA, "alpha-research", "Alpha Research", [ALICE])
    repo.seed_project(PROJECT_BETA, "beta-catalog", "Beta Catalog", [BOB])
    repo.seed_project(PROJECT_GAMMA, "gamma-swarm", "Gamma Swarm", [ALICE],
                      workflow_key="swarm_v2")
    # CODE-3: durable catalog state for the read-only review surface. The
    # catalog is GLOBAL rather than project-owned, so this is seeded once and is
    # readable by any member of any project -- which is exactly the property the
    # E2E suite checks, along with a non-member being refused. Offline: the
    # committed R5 capture fixtures, no socket, no model call.
    #
    # A seed that cannot run must not take the stack down: an empty catalog is a
    # state the surface is required to present honestly, and leaving the backend
    # up means the E2E suite exercises that path instead of failing to start.
    try:
        seed_catalog_review_state(repo, user_id=ALICE, project_id=PROJECT_ALPHA)
    except Exception:  # pragma: no cover - test harness resilience only
        pass
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


def build_swarm_v2_product_result(content: str) -> dict[str, Any]:
    """Build a REAL Swarm V2 product payload for the E2E stack.

    The payload is assembled by the SAME production code the engine uses --
    `FinalBuilder` over `finalize_product_outcome` -- so the E2E asserts the
    browser against a payload production could actually have written, not
    against a hand-typed lookalike. Only the evidence and the verdicts are
    mocked; the outcome policy, the status/kind pairing and the review
    composition are the shipped ones.

    No model is called and no source is fetched to produce any of it.
    """
    from backend.engines.swarm_v2.builder import FinalBuilder
    from backend.engines.swarm_v2.contracts import EvidenceReference, VerificationVerdict

    run_reference = "e2e-swarm-run"
    supports = "source evidence supports claim"

    def evidence(claim_id: str, field: str, value: Any, source_id: str) -> EvidenceReference:
        return EvidenceReference(
            claim_id=claim_id, source_id=source_id, run_id=run_reference,
            task_id="government_record_2026", entity="toyota_rav4_phev", field=field,
            geography="IL", market="IL", time_scope={"model_year": 2026},
            value=value, confidence=0.9, supported=True)

    builder = FinalBuilder()
    lowered = content.lower()

    if "no usable" in lowered:
        # Nothing verified and nothing disproved -> partial_success/no_usable_result.
        return builder.build([], [], task_failures=[
            {"task_id": "government_record_2026", "code": "R5_GOV_RECORD_AMBIGUOUS"}])

    if "incomplete task" in lowered:
        # EVERY gathered claim is VERIFIED; the run is partial solely because a
        # separate task failed. The product copy must stay true for this — it
        # may not say a claim went unverified.
        #
        # The trigger deliberately avoids the substring "fail": the generic
        # failure path in `_run` above matches `"fail" in content` and would
        # mark the run failed before it ever reaches a product outcome.
        return builder.build(
            [evidence("claim-fuel", "fuel_type", "plug-in hybrid", "src-gov-1")],
            [VerificationVerdict(claim_id="claim-fuel", verdict="verified", reason=supports)],
            task_failures=[{"task_id": "catalog_lookup", "code": "TASK_FAILED"}])

    if "rejected claim" in lowered:
        # A REJECTED verdict makes the run partial WITHOUT writing a
        # needs_review row, so this is a backend-valid partial_result whose
        # needs_review is empty. The product surface must stay honest about it.
        return builder.build(
            [evidence("claim-fuel", "fuel_type", "plug-in hybrid", "src-gov-1"),
             evidence("claim-hp", "horsepower_hp", 302, "src-gov-2")],
            [VerificationVerdict(claim_id="claim-fuel", verdict="verified", reason=supports),
             VerificationVerdict(claim_id="claim-hp", verdict="rejected",
                                 reason="source evidence does not support claim")])

    if "partial" in lowered:
        # A verified field PLUS an unresolved conflict, a task failure and a
        # coverage gap -> partial_success/partial_result.
        return builder.build(
            [evidence("claim-fuel", "fuel_type", "plug-in hybrid", "src-gov-1"),
             evidence("claim-disp", "engine_displacement_cc", 2487, "src-gov-2")],
            [VerificationVerdict(claim_id="claim-fuel", verdict="verified", reason=supports),
             VerificationVerdict(claim_id="claim-disp", verdict="needs_review",
                                 reason="unresolved conflict")],
            task_failures=[{"task_id": "catalog_lookup", "code": "R5_GOV_RECORD_AMBIGUOUS"}],
            coverage_gaps=[{"task_id": "compile_report", "code": "EVIDENCE_REQUIREMENTS_UNMET"}],
            conflict_claim_ids=["claim-disp"])

    # Default: everything verified, with ONE field carrying two verified values
    # so the browser is asserted against the "more than one value, none chosen"
    # case rather than only the easy one -> complete/usable_result.
    return builder.build(
        [evidence("claim-hp-1", "horsepower_hp", 302, "src-gov-1"),
         evidence("claim-hp-2", "horsepower_hp", 306, "src-gov-2"),
         evidence("claim-fuel", "fuel_type", "plug-in hybrid", "src-gov-1")],
        [VerificationVerdict(claim_id="claim-hp-1", verdict="verified", reason=supports),
         VerificationVerdict(claim_id="claim-hp-2", verdict="verified", reason=supports),
         VerificationVerdict(claim_id="claim-fuel", verdict="verified", reason=supports)])


#: Task content that makes the test launcher report an UNCERTAIN launch.
#: Deliberately avoids the substrings the worker's own branches match
#: ("fail", "timeout", "exhaust budget", "partial", "slow").
UNCERTAIN_LAUNCH_MARKER = "uncertain launch"


def build_vehicle_catalog_v1_result(content: str) -> dict[str, Any]:
    """A vehicle_catalog_v1 RUN ENVELOPE shaped exactly like the engine's.

    `backend/engines/vehicle_catalog_v1/engine.py` returns
    ``{"status", "result", "summary", ...}`` where ``result`` is the
    deterministic final document from ``core.build_final_json_python``:
    ``manufacturer``, ``market``, ``period``, ``status``, ``models`` (each with
    a ``verification_status``), ``needs_review``, ``rejected``,
    ``failed_agents`` and ``pipeline_quality``. The canonical outcome reader
    counts THIS document, so the E2E product path exercises the same V1
    derivation production uses. "partial" in the task text yields a
    needs_review model, which the reader demotes to `partial`.
    """
    partial = "partial" in content
    models = [
        {"canonical_model_name": "Alpha One", "model_name_he": "אלפא 1", "verification_status": "verified",
         "confidence": "high", "source_strength": "official_israel", "years": "2021-2024",
         "engine": "1.5 turbo", "fuel_type": "petrol", "power_hp": 150, "transmission": "automatic",
         "sources": ["https://example.com/alpha-one"]},
        {"canonical_model_name": "Alpha Two", "model_name_he": "אלפא 2",
         "verification_status": "needs_review" if partial else "verified",
         "confidence": "medium", "source_strength": "israeli_auto_portal", "years": "2019-2023",
         "engine": "2.0", "fuel_type": "hybrid", "power_hp": 180, "transmission": "automatic",
         "sources": ["https://example.com/alpha-two"]},
    ]
    final = {
        "manufacturer": "Alpha", "market": "IL", "period": "2019-2024",
        "status": "partial_success" if partial else "complete",
        "models": models,
        "needs_review": [m for m in models if m["verification_status"] == "needs_review"],
        "rejected": [],
        "failed_agents": [],
        "pipeline_quality": {"discovery": "success", "normalizer": "success",
                             "technical_enrichment": "success", "verifier": "success",
                             "final_builder": "success", "data_depth": "full_technical"},
        "token_usage": {},
        "final_builder_method": "python_merge_success",
        "technical_items_merged_count": 8,
    }
    return {"status": final["status"], "result": final,
            "summary": "E2E mocked output: two Alpha models were catalogued for the Israeli market.",
            "input_tokens": 1200, "output_tokens": 300, "elapsed_seconds": 1.0}


class InProcessFakeWorkerLauncher:
    """Simulates the Cloud Run worker in a daemon thread with mocked model
    calls. Exercises polling, cancellation, budget exhaustion, timeout,
    failure and completion paths without any external service."""

    def __init__(self, repo: MemoryRepository):
        self.repo = repo

    def _run_content(self, run_id: UUID) -> str:
        try:
            return str((self.repo.get_run(run_id).get("input") or {}).get("content") or "").lower()
        except Exception:  # pragma: no cover - defensive; the run was just created
            return ""

    def launch(self, run_id: UUID) -> dict[str, str]:
        if UNCERTAIN_LAUNCH_MARKER in self._run_content(run_id):
            # The PRODUCTION uncertainty condition, raised at the production
            # seam. `CloudRunJobLauncher.launch` raises exactly this when the
            # HTTP request may have reached Google before the connection broke:
            # an execution might already be running, so the API must park the
            # run as `launch_unknown` and never relaunch it on its own.
            #
            # Nothing downstream is stubbed. `_create_and_launch_run` handles
            # this exception in production code, and NO worker thread is
            # started here -- which is what makes "the launcher was not invoked
            # twice" observable: a second invocation would append a second
            # `launch_failed` event.
            raise JobLaunchUncertain(
                "E2E: Cloud Run Job launch outcome unknown (test-only launcher seam)")
        thread = threading.Thread(target=self._run, args=(run_id,), daemon=True)
        thread.start()
        return {"mode": "e2e-inprocess", "run_id": str(run_id), "execution": f"e2e-{run_id}"}

    def _emit(self, run_id: UUID, event_type: str, message: str,
              lease: dict[str, Any] | None = None, **extra: Any) -> None:
        """Append one durable event. The lease travels as the repository's
        keyword arguments -- the ownership contract -- and NEVER inside the
        payload, so no lease token can be served back by the events read."""
        self.repo.append_run_event(run_id, event_type, {"message": message, **extra}, **(lease or {}))

    def _workflow_key(self, run_id: UUID) -> str:
        """The run's IMMUTABLE identity decides the engine, exactly like
        `EngineResolver`. The project's current workflow and the run's own
        input never select it: a run born as V1 finalizes as V1 even if its
        project were switched to V2 while it ran."""
        from backend.run_identity import require_identity

        return require_identity(self.repo.get_run(run_id)).workflow_key

    def _lease(self, run_id: UUID) -> dict[str, Any]:
        """Claim the run the way the real worker does, and hold its lease.

        Every durable write below carries this lease, and the canonical
        finalizer commits the terminal status and the terminal event under it
        -- so what the browser polls in E2E is produced by the same fenced,
        atomic path as production, not by a test-only shortcut."""
        claimed = self.repo.claim_run(run_id, "e2e-worker")
        return {"worker_id": "e2e-worker", "attempt": claimed.get("attempt", 1),
                "lease_token": claimed.get("lease_token")}

    def _advance(self, run_id: UUID, lease: dict[str, Any], status: str, **fields: Any) -> bool:
        """Mirror the real worker's claim-time semantics: a cancellation that
        lands before/between the startup transitions must finalize the run as
        cancelled through the canonical finalizer, never crash into a failed
        terminal (the invalid cancellation_requested -> running race)."""
        from backend.errors import AppError

        if self.repo.get_run(run_id)["status"] == "cancellation_requested":
            return False
        try:
            self.repo.transition_run(run_id, status, expected_worker_id=lease["worker_id"],
                                     expected_attempt=lease["attempt"],
                                     expected_lease_token=lease["lease_token"], **fields)
            return True
        except AppError:
            if self.repo.get_run(run_id)["status"] == "cancellation_requested":
                return False
            raise

    def _run(self, run_id: UUID) -> None:
        """One mocked execution, terminalized ONLY through `RunFinalizer`.

        The finalizer derives the durable status from the canonical
        ProductOutcome (backend/product_outcome.py) and records that outcome on
        the terminal event in the same transaction as the status -- which is
        exactly what `GET /runs/{id}` projects back as `product_outcome`. No
        branch here writes a terminal status itself.
        """
        from backend.finalization import RunFinalizer, TerminalClaim

        from backend.errors import AppError

        repo = self.repo
        time.sleep(0.2)
        try:
            lease = self._lease(run_id)
        except AppError:
            # The run is already claimed by another worker or is no longer
            # claimable. A worker that holds no lease writes NOTHING -- exactly
            # the fencing rule production enforces at the database.
            return
        engine = self._workflow_key(run_id)
        finalizer = RunFinalizer(repo=repo, run_id=run_id, engine=engine, lease_ctx=lease)
        try:
            run = repo.get_run(run_id)
            content = str((run.get("input") or {}).get("content") or "").lower()

            def cancel(step: int | None = None) -> None:
                finalizer.finalize(TerminalClaim.cancelled(engine))

            if run["status"] == "cancellation_requested":
                cancel()
                return
            self._emit(run_id, "run_started", "Run started", payload={"worker": "e2e"}, lease=lease)
            if not self._advance(run_id, lease, "running"):
                cancel()
                return

            def emit(event_type: str, payload: dict[str, Any]) -> None:
                self._emit(run_id, event_type, payload.get("message", event_type),
                           payload=payload.get("payload", {}), lease=lease)

            if "timeout" in content:
                tracker = BudgetTracker(BudgetConfig(max_run_duration_seconds=1), kill_switch=lambda: True,
                                        clock=time.monotonic, event_emitter=emit)
                tracker._started_at = time.monotonic() - 5
                try:
                    tracker.before_call()
                except BudgetExceeded as exc:
                    finalizer.finalize(TerminalClaim.budget_stop(engine, exc, usage=tracker.snapshot()))
                    return
            if "exhaust budget" in content:
                tracker = BudgetTracker(BudgetConfig(max_model_calls_per_run=2, estimated_cost_per_call=0.01),
                                        kill_switch=lambda: True, event_emitter=emit,
                                        usage_recorder=lambda usage: repo.update_run_usage(run_id, usage, **lease))
                try:
                    while True:  # every iteration is a MOCKED call, gated first
                        tracker.before_call()
                        _ = MockModelResponse()
                        tracker.after_call(MockUsage.prompt_tokens, MockUsage.completion_tokens)
                        self._emit(run_id, "agent_progress", "Mocked model call recorded", agent="researcher", lease=lease)
                except BudgetExceeded as exc:
                    finalizer.finalize(TerminalClaim.budget_stop(engine, exc, usage=tracker.snapshot()))
                    return
            if "fail" in content:
                finalizer.finalize(TerminalClaim.failure(
                    engine, "ENGINE_FAILED",
                    "The worker hit an internal error. Diagnostics were recorded server-side."))
                return

            steps = 40 if "slow" in content else 3
            for index in range(steps):
                if repo.get_run(run_id)["status"] == "cancellation_requested":
                    cancel(index)
                    return
                self._emit(run_id, "agent_progress", f"step {index + 1}/{steps}", agent="researcher",
                           phase="research", progress={"percent": int(100 * (index + 1) / steps)}, lease=lease)
                time.sleep(0.35 if "slow" in content else 0.15)
            self._emit(run_id, "source_recorded", "Example source", agent="researcher",
                       payload={"id": "src-1", "title": "Example source", "domain": "example.com",
                                "url": "https://example.com", "source_type": "web", "source_strength": "high"},
                       lease=lease)

            if engine == "swarm_v2":
                # The shipped V2 contract builds the payload; the canonical
                # finalizer decides `completed` vs `partial_success` from it.
                output: dict[str, Any] = build_swarm_v2_product_result(content)
            else:
                output = build_vehicle_catalog_v1_result(content)
            finalizer.finalize(TerminalClaim.product(engine, output))
        except Exception as exc:  # pragma: no cover - defensive
            # A crash is terminalized through the SAME finalizer, under the
            # lease this worker holds; a finalizer that cannot write (lease
            # lost, run moved) raises, and nothing else writes a status.
            try:
                finalizer.finalize(TerminalClaim.failure(engine, "E2E_WORKER_CRASH", str(exc)[:200]))
            except Exception:
                pass


_repo = build_repository()
app.dependency_overrides[get_repository] = lambda: _repo
app.dependency_overrides[get_token_verifier] = lambda: E2ETokenVerifier()
app.dependency_overrides[get_gateway_token_verifier] = lambda: E2ETokenVerifier()
if os.getenv("MILO_E2E_INPROCESS_WORKER", "").lower() == "true":
    _launcher = InProcessFakeWorkerLauncher(_repo)
    app.dependency_overrides[get_job_launcher] = lambda: _launcher

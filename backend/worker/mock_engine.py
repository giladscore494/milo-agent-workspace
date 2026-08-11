"""Zero-cost mock lifecycle engine for isolated staging (Stage B).

Selected only via MILO_WORKER_ENGINE=mock, which backend/production_config.py
rejects in production (TEST_ADAPTER_IN_PRODUCTION). The engine constructs no
provider client and performs no network call: "model calls" are simulated
through the injected BudgetTracker with fabricated token counts and mock
costs, so the full budget/reservation lifecycle (reserve → settle, limits,
daily budgets) executes against the real database with zero provider spend.

Scenarios are chosen per run through input.metadata.mock_scenario:

- ``happy`` (default) — three phases with events, agent steps, simulated
  model calls and checkpoints, ending in a successful result whose final
  checkpoint is fast-path resumable (phase "summary" + final_builder).
- ``fail`` — deterministic engine failure (worker marks the run failed).
- ``wait_for_cancellation`` — polls the cancellation checker (up to
  ``mock_wait_seconds``, default 60) so mid-execution cancellation can be
  exercised; times out as a failure rather than hanging.
- ``budget_loop`` — keeps simulating model calls until a budget limit
  trips (BudgetExceeded propagates to the worker, which records the
  terminal budget status). ``mock_max_loop`` (default 1000) bounds the
  loop defensively.

The MILO production pipeline (vehicle_catalog_v1) is untouched; this is an
additive, staging-only adapter behind the same Engine protocol.
"""

import time
from typing import Any, Callable

from backend.runtime import CancellationRequested

MOCK_INPUT_TOKENS = 1000
MOCK_OUTPUT_TOKENS = 200
MOCK_COST_PER_CALL = 0.001


class MockLifecycleEngine:
    workflow_key = "vehicle_catalog_v1"
    engine_version = "mock-lifecycle-1"

    def __init__(
        self,
        event_sink: Callable[[str, dict[str, Any]], None],
        checkpoint_sink: Callable[[str, dict[str, Any]], None],
        cancellation_checker: Callable[[], bool],
        agent_step_callback: Callable[[str, str], None] | None = None,
        retry_callback: Callable[[str, str, str], None] | None = None,
        budget_tracker: Any | None = None,
    ) -> None:
        self.event_sink = event_sink
        self.checkpoint_sink = checkpoint_sink
        self.cancellation_checker = cancellation_checker
        self.agent_step_callback = agent_step_callback
        self.retry_callback = retry_callback
        self.budget_tracker = budget_tracker

    def _metadata(self, run: dict[str, Any]) -> dict[str, Any]:
        metadata = ((run.get("input") or {}).get("metadata")) or {}
        return metadata if isinstance(metadata, dict) else {}

    def _check_cancelled(self) -> None:
        if self.cancellation_checker():
            raise CancellationRequested()

    def _simulated_model_call(self) -> None:
        if self.budget_tracker is None:
            return
        self.budget_tracker.before_call()
        self.budget_tracker.after_call(
            input_tokens=MOCK_INPUT_TOKENS,
            output_tokens=MOCK_OUTPUT_TOKENS,
            cost=MOCK_COST_PER_CALL,
        )

    def _checkpoint(self, phase: str, artifacts: dict[str, Any], completed: list[str]) -> None:
        self.checkpoint_sink(phase, {
            "engine_version": self.engine_version,
            "workflow_key": self.workflow_key,
            "phase": phase,
            "completed_tasks": completed,
            "artifacts": artifacts,
            "failures": [],
            "token_usage": {"input_tokens": MOCK_INPUT_TOKENS * len(completed), "output_tokens": MOCK_OUTPUT_TOKENS * len(completed)},
        })

    def run(self, run: dict[str, Any]) -> dict[str, Any]:
        metadata = self._metadata(run)
        scenario = str(metadata.get("mock_scenario") or "happy").strip().lower()

        if scenario == "fail":
            self.event_sink("agent_failed", {"agent": "mock", "phase": "discovery", "message": "Mock engine deterministic failure"})
            return {"status": "failed", "error": {"code": "MOCK_ENGINE_FAILURE", "message": "mock scenario requested a failure"}}

        if scenario == "wait_for_cancellation":
            deadline = time.monotonic() + float(metadata.get("mock_wait_seconds") or 60)
            self.event_sink("phase_started", {"phase": "discovery", "message": "Mock engine waiting for cancellation"})
            while time.monotonic() < deadline:
                self._check_cancelled()
                time.sleep(0.5)
            return {"status": "failed", "error": {"code": "MOCK_CANCELLATION_TIMEOUT", "message": "cancellation was never requested"}}

        if scenario == "budget_loop":
            max_loop = int(metadata.get("mock_max_loop") or 1000)
            self.event_sink("phase_started", {"phase": "technical", "message": "Mock engine driving budget limits"})
            for seq in range(max_loop):
                self._check_cancelled()
                if self.agent_step_callback:
                    self.agent_step_callback("mock-budget", "technical")
                # BudgetExceeded propagates to the worker entrypoint, which
                # records the terminal budget status under the lease.
                self._simulated_model_call()
            return {"status": "failed", "error": {"code": "MOCK_BUDGET_NEVER_TRIPPED", "message": f"no budget limit tripped in {max_loop} simulated calls"}}

        # -- happy path -------------------------------------------------------
        phases = ("discovery", "technical", "summary")
        completed: list[str] = []
        artifacts: dict[str, Any] = {}
        for phase in phases:
            self._check_cancelled()
            if self.agent_step_callback:
                self.agent_step_callback(f"mock-{phase}", phase)
            self.event_sink("phase_started", {"phase": phase, "message": f"Mock phase started: {phase}"})
            self._simulated_model_call()
            completed.append(phase)
            if phase == "summary":
                artifacts["final_builder"] = {"parsed": {"status": "success", "models": [], "mock": True}}
                artifacts["hebrew_summary"] = {"parsed": {"summary": "mock staging run"}}
            else:
                artifacts[phase] = {"parsed": {"mock": True}}
            self._checkpoint(phase, artifacts, list(completed))
            self.event_sink("phase_completed", {"phase": phase, "message": f"Mock phase completed: {phase}", "progress": {"percent": int(100 * len(completed) / len(phases))}})
        return {
            "status": "success",
            "result": {"status": "success", "models": [], "mock": True},
            "summary": "mock staging run",
            "results": artifacts,
        }

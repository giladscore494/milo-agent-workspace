"""Minimal Engine-protocol boundary; execution is intentionally outside S1-PR2."""

from typing import Any
from .commander import Commander


class SwarmV2Engine:
    workflow_key = "swarm_v2"

    def __init__(self, *, commander: Commander):
        self._commander = commander

    def run(self, run: dict[str, Any]) -> dict[str, Any]:
        run_input = run.get("input") or {}
        plan = self._commander.plan(
            requested_model=run_input.get("commander_model", "auto_best_available"),
            objective=run_input.get("objective", ""),
            context=run_input.get("context", {}),
        )
        return {"status": "plan_validated", "plan": plan.model_dump(mode="json")}

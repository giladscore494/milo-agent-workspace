"""PR-T: the engine's run-terminal refusals, each with a static code.

Run 3c72bfbc failed with ``SWARM_V2_EXECUTION_FAILED`` and nothing else: the
engine raised a bare ``ValueError("completion criteria not satisfied")``, the
worker's handler could not tell it apart from any other exception, and the
real cause had to be reconstructed from the event stream. Each orchestration
refusal now raises :class:`SwarmExecutionFailure` with ONE code from a static
allowlist, which the worker propagates to ``run.error.code`` and the
``run_failed`` event unchanged.

It stays a ``ValueError`` subclass, with the historical message as its safe
message, so every existing caller that catches ``ValueError`` -- and every test
that matches the old text -- keeps working.
"""

from __future__ import annotations

SWARM_V2_COMPLETION_CRITERIA_UNMET = "SWARM_V2_COMPLETION_CRITERIA_UNMET"
SWARM_V2_REQUIRED_TASK_FAILED = "SWARM_V2_REQUIRED_TASK_FAILED"
SWARM_V2_REPLAN_REQUIRES_GAP = "SWARM_V2_REPLAN_REQUIRES_GAP"
SWARM_V2_MAX_REPLANS_EXCEEDED = "SWARM_V2_MAX_REPLANS_EXCEEDED"
SWARM_V2_REPLAN_REWRITES_COMPLETED = "SWARM_V2_REPLAN_REWRITES_COMPLETED"

#: Code -> safe message. The messages are the exact text the bare ValueErrors
#: carried before, and are static: no task id, plan text or model output.
EXECUTION_FAILURE_MESSAGES = {
    SWARM_V2_COMPLETION_CRITERIA_UNMET: "completion criteria not satisfied",
    SWARM_V2_REQUIRED_TASK_FAILED: "required task execution failed",
    SWARM_V2_REPLAN_REQUIRES_GAP: "replan requires an unresolved gap or conflict",
    SWARM_V2_MAX_REPLANS_EXCEEDED: "maximum replans exceeded",
    SWARM_V2_REPLAN_REWRITES_COMPLETED: "replan cannot revise or discard completed tasks",
}
EXECUTION_FAILURE_CODES = frozenset(EXECUTION_FAILURE_MESSAGES)


class SwarmExecutionFailure(ValueError):
    """An orchestration refusal carrying ONLY a static, allowlisted code."""

    def __init__(self, code: str):
        if code not in EXECUTION_FAILURE_CODES:
            raise ValueError("execution failure code must come from the static allowlist")
        self.code = code
        self.safe_message = EXECUTION_FAILURE_MESSAGES[code]
        super().__init__(self.safe_message)


__all__ = ["EXECUTION_FAILURE_CODES", "EXECUTION_FAILURE_MESSAGES",
           "SWARM_V2_COMPLETION_CRITERIA_UNMET", "SWARM_V2_MAX_REPLANS_EXCEEDED",
           "SWARM_V2_REPLAN_REQUIRES_GAP", "SWARM_V2_REPLAN_REWRITES_COMPLETED",
           "SWARM_V2_REQUIRED_TASK_FAILED", "SwarmExecutionFailure"]

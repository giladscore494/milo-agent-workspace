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

import json
import logging

from backend.budget import BudgetExceeded
from backend.errors import AppError
from backend.runtime import CancellationRequested

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


#: PR-X: the infrastructure faults that keep today's terminal handling even
#: after paid work completed -- a cancellation, a budget stop, and the
#: repository/lease boundary (AppError: lease lost, repository unavailable
#: after retries). Everything else that fails after the first completed task
#: DEGRADES instead (see outcome.DEGRADED_STEP_CODES).
INFRASTRUCTURE_FAULTS: tuple[type[BaseException], ...] = (
    CancellationRequested, BudgetExceeded, AppError)

#: What is never degraded: the infrastructure faults, and an AssertionError --
#: an internal invariant the engine itself relies on was violated, so its own
#: state can no longer be trusted to finalize a truthful result.
NOT_DEGRADABLE: tuple[type[BaseException], ...] = (*INFRASTRUCTURE_FAULTS, AssertionError)

_DEGRADED_LOG = logging.getLogger("milo.swarm_v2.degraded")


def log_step_degraded(step: str, code: str, exception_class: str) -> None:
    """ONE structured line per degraded step: static step and code, class name only.

    Never the exception message, which can quote plan, tool or provider
    material.
    """
    _DEGRADED_LOG.warning(json.dumps({"event": "swarm_step_degraded", "step": step,
                                      "code": code, "exception_class": exception_class},
                                     sort_keys=True))


__all__ = ["EXECUTION_FAILURE_CODES", "INFRASTRUCTURE_FAULTS", "NOT_DEGRADABLE",
           "log_step_degraded", "EXECUTION_FAILURE_MESSAGES",
           "SWARM_V2_COMPLETION_CRITERIA_UNMET", "SWARM_V2_MAX_REPLANS_EXCEEDED",
           "SWARM_V2_REPLAN_REQUIRES_GAP", "SWARM_V2_REPLAN_REWRITES_COMPLETED",
           "SWARM_V2_REQUIRED_TASK_FAILED", "SwarmExecutionFailure"]

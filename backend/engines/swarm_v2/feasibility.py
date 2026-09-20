"""What a plan can cost at WORST, computed before anything is spent.

The point of a pre-flight check is to refuse a plan the run cannot finish. The
previous check required ``len(pending) + 2`` model calls -- one per task plus
two -- which is not a worst case, it is barely a minimum: it counted no worker
repair, no replan decision, no correction round, and no agent steps at all.

A plan could therefore pass preflight and still fail predictably mid-run, after
real money had been spent. Concretely, with the production ``PlanLimits()``
defaults (64 tasks) against ``MILO_MAX_AGENT_STEPS=56``, a 54-to-64 task plan
was *guaranteed* to trip ``AGENT_STEP_LIMIT_REACHED`` around step 56 -- and the
preflight floor for 64 pending tasks was 66 model calls against a realistic
worst case of 136.

Every number here is a bound this repository already enforces elsewhere, so the
estimate cannot drift from what the engine actually does:

* one worker model call per task, plus at most one bounded repair each
  (``worker.MAX_WORKER_OUTPUT_MODEL_ATTEMPTS``);
* one Commander decision per replan round plus the final one that ends the run
  (``CommanderPlan.max_replans``);
* at least one verifier batch -- the EXACT count needs an evidence set that
  does not exist yet and is checked again, exactly, in ``_run_verification``;
* the one bounded correction round (``correction.MIN_CORRECTION_MODEL_CALLS``).

``agent_steps`` equals the model-call count because every guarded
``ModelGateway.call`` records exactly one step before it dispatches.

Retries counted here are the SEMANTIC ones (a Commander repair, a worker-output
repair). Provider 429 backpressure is deliberately absent: the scheduler
absorbs it and it never consumes the semantic allowance. Provider ATTEMPTS are
not free, though -- each one re-enters the shared organization admission gate --
so they are reported separately for capacity planning.
"""

from __future__ import annotations

from dataclasses import dataclass

from .correction import MAX_CORRECTION_ROUNDS, MIN_CORRECTION_MODEL_CALLS
from .worker import MAX_WORKER_OUTPUT_MODEL_ATTEMPTS

#: One bounded semantic repair is available to the initial Commander plan.
MAX_COMMANDER_PLAN_REPAIRS = 1

#: The floor this check can assert without an evidence set. The exact batch
#: count is re-checked in the engine once evidence exists.
MIN_VERIFIER_BATCHES = 1


@dataclass(frozen=True)
class WorstCase:
    """The most a plan can consume on each independently-enforced dimension."""

    model_calls: int
    agent_steps: int
    retries: int
    #: Real provider attempts, including the bounded 429 retries the scheduler
    #: may make. Each one takes organization RPM/TPM/concurrency, so it is the
    #: number that matters for shared-quota planning even though it never
    #: consumes the semantic retry allowance.
    provider_attempts: int


def plan_worst_case(pending_tasks: int, *, max_replans: int,
                    max_rate_limit_retries: int = 0,
                    verifier_batches: int = MIN_VERIFIER_BATCHES) -> WorstCase:
    """Bound one plan's remaining cost. Conservative by construction.

    ``pending_tasks`` is what is left to execute, so a resumed run is charged
    only for the work it has not already paid for.
    """
    pending = max(0, int(pending_tasks))
    replans = max(0, int(max_replans))
    batches = max(MIN_VERIFIER_BATCHES, int(verifier_batches))

    worker_calls = pending
    worker_repairs = pending * (MAX_WORKER_OUTPUT_MODEL_ATTEMPTS - 1)
    # One decision per replan round, plus the decision that ends the run.
    commander_decisions = replans + 1
    correction = MIN_CORRECTION_MODEL_CALLS * MAX_CORRECTION_ROUNDS

    model_calls = (worker_calls + worker_repairs + commander_decisions +
                   batches + correction + MAX_COMMANDER_PLAN_REPAIRS)
    retries = worker_repairs + MAX_COMMANDER_PLAN_REPAIRS
    provider_attempts = model_calls * (1 + max(0, int(max_rate_limit_retries)))
    return WorstCase(model_calls=model_calls, agent_steps=model_calls,
                     retries=retries, provider_attempts=provider_attempts)


#: The fewest model calls ANY successful run can take: the Commander plan, one
#: worker call, the Commander decision that ends the run, and one verifier
#: batch. No repair, no replan and no correction round are assumed.
MINIMUM_VIABLE_MODEL_CALLS = 4


def envelope_supports_a_run(remaining: Any) -> bool:
    """Can this envelope pay for the cheapest run that could ever succeed?

    Checked BEFORE the Commander is asked to plan, so an envelope that cannot
    work refuses without spending anything at all. The per-plan worst case is
    still checked separately once a plan exists; this only rules out the case
    where no plan could have helped.
    """
    return (remaining.model_calls >= MINIMUM_VIABLE_MODEL_CALLS and
            remaining.agent_steps >= MINIMUM_VIABLE_MODEL_CALLS and
            remaining.tasks >= 1)


__all__ = ["MAX_COMMANDER_PLAN_REPAIRS", "MINIMUM_VIABLE_MODEL_CALLS",
           "MIN_VERIFIER_BATCHES", "WorstCase", "envelope_supports_a_run",
           "plan_worst_case"]

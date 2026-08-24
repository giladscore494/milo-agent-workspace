"""Commander facade: model output stays inert until its firewall approves it."""

import inspect
from typing import Any, Callable, Mapping
from backend.budget import BudgetExceeded
from backend.errors import AppError
from backend.runtime import CancellationRequested
from .adapters import CommanderClient
from .contracts import CommanderDecision, CommanderPlan
from .models import CommanderModelResolver
from .validation import (VALIDATION_REASONS, PlanJsonError, PlanLimitError,
                         PlanSchemaError, PlanValidationError, PlanValidator)
from .evidence import safe_durable_value


# The only failure family eligible for the single in-run semantic repair:
# the completion arrived but was rejected by JSON decoding, the strict
# contract, or the deterministic firewall. Provider/infrastructure
# failures, cancellations and budget stops are never repaired.
REPAIRABLE_PLAN_FAILURES = frozenset({
    "COMMANDER_PLAN_JSON_INVALID",
    "COMMANDER_PLAN_SCHEMA_INVALID",
    "COMMANDER_PLAN_LIMIT_EXCEEDED",
})


class Commander:
    def __init__(self, *, client: CommanderClient, resolver: CommanderModelResolver,
                 validator: PlanValidator,
                 retry_callback: Callable[[str, str, str], None] | None = None):
        self._client = client
        self._resolver = resolver
        self._validator = validator
        self._retry_callback = retry_callback

    def plan(self, *, requested_model: str, objective: str, context: Mapping[str, Any]) -> CommanderPlan:
        model = self._resolver.resolve(requested_model)
        try:
            return self._plan_attempt(model=model, objective=objective,
                                      context=context, repair_reason=None)
        except CommanderPlanFailure as failure:
            if failure.code not in REPAIRABLE_PLAN_FAILURES or not self._supports_repair():
                raise
            # Exactly ONE bounded semantic repair inside the same run. It is
            # a normal guarded model call (budget reservation, scheduler,
            # accounting) and counts as one semantic retry. The model
            # receives only the static safe reason code: the rejected plan
            # and raw validation diagnostics never leave this boundary. A
            # second invalid response escapes below with no third attempt.
            reason = failure.validation_reason or "SCHEMA_CONSTRAINT_FAILED"
            if self._retry_callback is not None:
                self._retry_callback("commander", "planning", reason)
            return self._plan_attempt(model=model, objective=objective,
                                      context=context, repair_reason=reason)

    def _supports_repair(self) -> bool:
        try:
            parameters = inspect.signature(self._client.create_plan).parameters.values()
        except (TypeError, ValueError):
            return False
        return any(parameter.name == "repair_reason" or
                   parameter.kind is inspect.Parameter.VAR_KEYWORD
                   for parameter in parameters)

    def _plan_attempt(self, *, model: str, objective: str, context: Mapping[str, Any],
                      repair_reason: str | None) -> CommanderPlan:
        extra = {} if repair_reason is None else {"repair_reason": repair_reason}
        try:
            inert_json = self._client.create_plan(model=model, objective=objective,
                                                  context=context, **extra)
        except (CancellationRequested, BudgetExceeded, CommanderPlanFailure, AppError):
            # AppError is the persistence/repository boundary (e.g. a lease
            # or usage write failing inside the guarded client): it must
            # escape as infrastructure, never as a handled Commander failure.
            raise
        except Exception:
            raise CommanderPlanFailure("COMMANDER_COMPLETION_FAILED") from None
        try:
            return self._validator.validate(inert_json)
        except PlanJsonError as exc:
            raise CommanderPlanFailure("COMMANDER_PLAN_JSON_INVALID",
                                       validation_reason=exc.reason) from None
        except PlanLimitError as exc:
            raise CommanderPlanFailure("COMMANDER_PLAN_LIMIT_EXCEEDED",
                                       validation_reason=exc.reason) from None
        except PlanValidationError as exc:
            raise CommanderPlanFailure("COMMANDER_PLAN_SCHEMA_INVALID",
                                       validation_reason=exc.reason) from None

    def replan(self, *, requested_model: str, objective: str,
               summary: Mapping[str, Any]) -> CommanderDecision:
        """Give the model only the compact, provider-neutral run summary.

        Replan failures carry the same stable, sanitized codes as planning:
        raw provider errors and validation diagnostics (which can embed model
        output) never leave this boundary. Replanning deliberately has NO
        semantic repair attempt: it stays exactly one guarded call.
        """
        model = self._resolver.resolve(requested_model)
        create = getattr(self._client, "create_replan", None)
        try:
            inert = (create(model=model, objective=objective, summary=summary) if create else
                     self._client.create_plan(model=model, objective=objective, context={"status": summary}))
        except (CancellationRequested, BudgetExceeded, CommanderPlanFailure, AppError):
            raise
        except Exception:
            raise CommanderPlanFailure("COMMANDER_COMPLETION_FAILED") from None
        try:
            decision = CommanderDecision.model_validate_json(inert) if isinstance(inert, (str, bytes)) else CommanderDecision.model_validate(inert)
            safe_durable_value(decision.reason)
        except Exception:
            raise CommanderPlanFailure("COMMANDER_DECISION_INVALID") from None
        if decision.plan is not None:
            # Re-parse through the same deterministic firewall; nested Pydantic
            # validation alone is deliberately not authorization.
            try:
                validated = self._validator.validate(decision.plan.model_dump(mode="json"))
            except PlanJsonError as exc:
                raise CommanderPlanFailure("COMMANDER_PLAN_JSON_INVALID",
                                           validation_reason=exc.reason) from None
            except PlanLimitError as exc:
                raise CommanderPlanFailure("COMMANDER_PLAN_LIMIT_EXCEEDED",
                                           validation_reason=exc.reason) from None
            except PlanValidationError as exc:
                raise CommanderPlanFailure("COMMANDER_PLAN_SCHEMA_INVALID",
                                           validation_reason=exc.reason) from None
            decision = decision.model_copy(update={"plan": validated})
        return decision

    def validate_saved_plan(self, candidate: Mapping[str, Any]) -> CommanderPlan:
        """Re-authorize checkpoint data through the deterministic firewall."""
        return self._validator.validate(dict(candidate))


class CommanderPlanFailure(RuntimeError):
    """Sanitized planning failure safe for durable status and telemetry."""

    MESSAGES = {
        "COMMANDER_COMPLETION_FAILED": "Commander completion failed",
        "COMMANDER_COMPLETION_SHAPE_INVALID": "Commander completion shape is invalid",
        "COMMANDER_PLAN_JSON_INVALID": "Commander plan JSON is invalid",
        "COMMANDER_PLAN_SCHEMA_INVALID": "Commander plan schema is invalid",
        "COMMANDER_PLAN_LIMIT_EXCEEDED": "Commander plan exceeds safety limits",
        "COMMANDER_DECISION_INVALID": "Commander replan decision is invalid",
    }

    def __init__(self, code: str, *, validation_reason: str | None = None):
        self.code = code
        self.safe_message = self.MESSAGES[code]
        if validation_reason is not None and validation_reason not in VALIDATION_REASONS:
            raise ValueError("validation reason must come from the static allowlist")
        self.validation_reason = validation_reason
        super().__init__(self.safe_message)

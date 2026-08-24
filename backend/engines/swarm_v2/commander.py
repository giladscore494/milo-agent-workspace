"""Commander facade: model output stays inert until its firewall approves it."""

from typing import Any, Mapping
from backend.budget import BudgetExceeded
from backend.runtime import CancellationRequested
from .adapters import CommanderClient
from .contracts import CommanderDecision, CommanderPlan
from .models import CommanderModelResolver
from .validation import (PlanJsonError, PlanLimitError, PlanSchemaError,
                         PlanValidationError, PlanValidator)
from .evidence import safe_durable_value


class Commander:
    def __init__(self, *, client: CommanderClient, resolver: CommanderModelResolver, validator: PlanValidator):
        self._client = client
        self._resolver = resolver
        self._validator = validator

    def plan(self, *, requested_model: str, objective: str, context: Mapping[str, Any]) -> CommanderPlan:
        model = self._resolver.resolve(requested_model)
        try:
            inert_json = self._client.create_plan(model=model, objective=objective, context=context)
        except (CancellationRequested, BudgetExceeded, CommanderPlanFailure):
            raise
        except Exception:
            raise CommanderPlanFailure("COMMANDER_COMPLETION_FAILED") from None
        try:
            return self._validator.validate(inert_json)
        except PlanJsonError:
            raise CommanderPlanFailure("COMMANDER_PLAN_JSON_INVALID") from None
        except PlanSchemaError:
            raise CommanderPlanFailure("COMMANDER_PLAN_SCHEMA_INVALID") from None
        except PlanLimitError:
            raise CommanderPlanFailure("COMMANDER_PLAN_LIMIT_EXCEEDED") from None
        except PlanValidationError:
            raise CommanderPlanFailure("COMMANDER_PLAN_SCHEMA_INVALID") from None

    def replan(self, *, requested_model: str, objective: str,
               summary: Mapping[str, Any]) -> CommanderDecision:
        """Give the model only the compact, provider-neutral run summary."""
        model = self._resolver.resolve(requested_model)
        create = getattr(self._client, "create_replan", None)
        inert = (create(model=model, objective=objective, summary=summary) if create else
                 self._client.create_plan(model=model, objective=objective, context={"status": summary}))
        decision = CommanderDecision.model_validate_json(inert) if isinstance(inert, (str, bytes)) else CommanderDecision.model_validate(inert)
        safe_durable_value(decision.reason)
        if decision.plan is not None:
            # Re-parse through the same deterministic firewall; nested Pydantic
            # validation alone is deliberately not authorization.
            validated = self._validator.validate(decision.plan.model_dump(mode="json"))
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
    }

    def __init__(self, code: str):
        self.code = code
        self.safe_message = self.MESSAGES[code]
        super().__init__(self.safe_message)

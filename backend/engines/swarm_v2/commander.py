"""Commander facade: model output stays inert until its firewall approves it."""

import inspect
import json
import logging
from typing import Any, Callable, Mapping

from pydantic import ValidationError
from backend.budget import BudgetExceeded
from backend.errors import AppError
from backend.provider_streaming import ProviderTransportFailure
from backend.runtime import CancellationRequested
from .adapters import CommanderClient
from .contracts import CommanderDecision, CommanderPlan
from .models import CommanderModelResolver
from .completion import COMPLETION_MESSAGES, TRUNCATION_CODES
from .request_builder import ModelRequestRefused
from .validation import (VALIDATION_REASONS, PlanJsonError, PlanLimitError,
                         PlanSchemaError, PlanValidationError, PlanValidator)
from .evidence import safe_durable_value

_LOG = logging.getLogger("milo.swarm_v2.commander")

#: The only top-level names a replan decision may carry. A diagnostic line
#: may NAME these; any other key is only ever COUNTED.
DECISION_FIELDS = frozenset(CommanderDecision.model_fields)

#: Bound on the distinct error types one diagnostic line lists.
MAX_DIAGNOSTIC_ERROR_TYPES = 20

#: The static type recorded when the decision parsed but its reason was
#: refused by the durable-value firewall (not a pydantic error).
DURABLE_VALUE_REJECTED = "durable_value_rejected"


def _decision_diagnostics(inert: Any, error: Exception) -> dict[str, Any]:
    """Describe WHY a replan decision was refused, carrying no provider material.

    Error TYPES only (pydantic's static vocabulary, never a message, location
    or input); the top-level keys only where they are decision field names;
    every other key only as a count; and the length of `reason` -- never its
    text. Run c4b8bb54 failed on COMMANDER_DECISION_INVALID with nothing in
    the logs to say which rule the answer broke.
    """
    if isinstance(error, ValidationError):
        types = sorted({str(item.get("type", "")) for item in error.errors()})
    else:
        types = [DURABLE_VALUE_REJECTED]
    candidate: Any = inert
    if isinstance(inert, (str, bytes)):
        try:
            candidate = json.loads(inert)
        except (TypeError, ValueError):
            candidate = None
    keys = [key for key in candidate if isinstance(key, str)] \
        if isinstance(candidate, Mapping) else []
    known = sorted(DECISION_FIELDS.intersection(keys))
    reason = candidate.get("reason") if isinstance(candidate, Mapping) else None
    return {"event": "commander_decision_invalid",
            "error_types": types[:MAX_DIAGNOSTIC_ERROR_TYPES],
            "top_level_fields": known,
            "unknown_field_count": (len(candidate) - len(known)
                                    if isinstance(candidate, Mapping) else 0),
            "reason_length": len(reason) if isinstance(reason, str) else None}


# The only failure family eligible for the single in-run semantic repair:
# the completion arrived but was rejected by JSON decoding, the strict
# contract, or the deterministic firewall. Provider/infrastructure
# failures, cancellations and budget stops are never repaired.
REPAIRABLE_PLAN_FAILURES = frozenset({
    "COMMANDER_PLAN_JSON_INVALID",
    "COMMANDER_PLAN_SCHEMA_INVALID",
    "COMMANDER_PLAN_LIMIT_EXCEEDED",
    # PR-R: a completion cut off by the output cap. Repaired once, by
    # ESCALATION (cap doubled or effort lowered), and only when an escalation
    # exists; the repair is an ordinary guarded call, so its worst-case
    # reservation must pass every dollar ceiling before it is sent.
    "MODEL_REASONING_EXHAUSTED_OUTPUT",
    "MODEL_OUTPUT_TRUNCATED",
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
            extra: dict[str, Any] = {}
            if failure.code in TRUNCATION_CODES:
                if failure.escalation is None:
                    # Nothing can change (cap already at the role maximum and
                    # the effort cannot drop): repeating the call would buy
                    # the same truncation again.
                    raise
                reason = failure.code
                extra["repair_shape"] = failure.escalation
            else:
                reason = failure.validation_reason or "SCHEMA_CONSTRAINT_FAILED"
            # Exactly ONE bounded semantic repair inside the same run. It is
            # a normal guarded model call (budget reservation, scheduler,
            # accounting) and counts as one semantic retry. The model
            # receives only the static safe reason code: the rejected plan
            # and raw validation diagnostics never leave this boundary. A
            # second invalid response escapes below with no third attempt.
            if self._retry_callback is not None:
                self._retry_callback("commander", "planning", reason)
            return self._plan_attempt(model=model, objective=objective,
                                      context=context, repair_reason=reason, **extra)

    def _supports_repair(self) -> bool:
        try:
            parameters = inspect.signature(self._client.create_plan).parameters.values()
        except (TypeError, ValueError):
            return False
        return any(parameter.name == "repair_reason" or
                   parameter.kind is inspect.Parameter.VAR_KEYWORD
                   for parameter in parameters)

    def _plan_attempt(self, *, model: str, objective: str, context: Mapping[str, Any],
                      repair_reason: str | None, repair_shape: Any = None) -> CommanderPlan:
        extra: dict[str, Any] = {} if repair_reason is None else {"repair_reason": repair_reason}
        if repair_shape is not None:
            extra["repair_shape"] = repair_shape
        try:
            inert_json = self._client.create_plan(model=model, objective=objective,
                                                  context=context, **extra)
        except (CancellationRequested, BudgetExceeded, CommanderPlanFailure, AppError,
                ModelRequestRefused, ProviderTransportFailure):
            # AppError is the persistence/repository boundary (e.g. a lease
            # or usage write failing inside the guarded client): it must
            # escape as infrastructure, never as a handled Commander failure.
            # ModelRequestRefused is a model-contract refusal decided before
            # any request existed; it keeps its own static code.
            # ProviderTransportFailure (PR-S) is a transport outcome already
            # named with its static code (deadline, stream interrupted, HTTP
            # status); folding it into COMMANDER_COMPLETION_FAILED is what hid
            # run 5145ca65's cause. It is never repaired here.
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
        except (CancellationRequested, BudgetExceeded, CommanderPlanFailure, AppError,
                ModelRequestRefused, ProviderTransportFailure):
            raise
        except Exception:
            raise CommanderPlanFailure("COMMANDER_COMPLETION_FAILED") from None
        try:
            decision = CommanderDecision.model_validate_json(inert) if isinstance(inert, (str, bytes)) else CommanderDecision.model_validate(inert)
            safe_durable_value(decision.reason)
        except Exception as exc:
            try:
                diagnostics = _decision_diagnostics(inert, exc)
            except Exception:
                # Diagnostics never change the outcome: the static failure
                # below is raised whatever the refused answer looked like.
                diagnostics = {"event": "commander_decision_invalid",
                               "error_types": [], "top_level_fields": [],
                               "unknown_field_count": 0, "reason_length": None}
            _LOG.warning(json.dumps(diagnostics, sort_keys=True))
            raise CommanderPlanFailure("COMMANDER_DECISION_INVALID") from None
        if decision.plan is not None:
            # Re-parse through the same deterministic firewall; nested Pydantic
            # validation alone is deliberately not authorization.
            try:
                validated = self._validator.validate(decision.plan.model_dump(mode="json"))
            except PlanJsonError as exc:
                raise self._replan_plan_failure("COMMANDER_PLAN_JSON_INVALID", exc.reason) from None
            except PlanLimitError as exc:
                raise self._replan_plan_failure("COMMANDER_PLAN_LIMIT_EXCEEDED", exc.reason) from None
            except PlanValidationError as exc:
                raise self._replan_plan_failure("COMMANDER_PLAN_SCHEMA_INVALID", exc.reason) from None
            decision = decision.model_copy(update={"plan": validated})
        return decision

    @staticmethod
    def _replan_plan_failure(code: str, reason: str) -> "CommanderPlanFailure":
        """The replan's plan was refused by the firewall: ONE line, codes only."""
        _LOG.warning(json.dumps({"event": "commander_replan_plan_invalid", "code": code,
                                 "validation_reason": reason}, sort_keys=True))
        return CommanderPlanFailure(code, validation_reason=reason)

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
        # PR-R completion classification (backend/engines/swarm_v2/completion.py).
        **COMPLETION_MESSAGES,
    }

    def __init__(self, code: str, *, validation_reason: str | None = None,
                 escalation: Any = None):
        self.code = code
        self.safe_message = self.MESSAGES[code]
        if validation_reason is not None and validation_reason not in VALIDATION_REASONS:
            raise ValueError("validation reason must come from the static allowlist")
        self.validation_reason = validation_reason
        #: The ONE escalated (cap, effort) a truncation may be repaired with;
        #: None when the failure is not repairable that way.
        self.escalation = escalation
        super().__init__(self.safe_message)

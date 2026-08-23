"""Commander facade: model output stays inert until its firewall approves it."""

from typing import Any, Mapping
from .adapters import CommanderClient
from .contracts import CommanderPlan
from .models import CommanderModelResolver
from .validation import PlanValidator


class Commander:
    def __init__(self, *, client: CommanderClient, resolver: CommanderModelResolver, validator: PlanValidator):
        self._client = client
        self._resolver = resolver
        self._validator = validator

    def plan(self, *, requested_model: str, objective: str, context: Mapping[str, Any]) -> CommanderPlan:
        model = self._resolver.resolve(requested_model)
        inert_json = self._client.create_plan(model=model, objective=objective, context=context)
        return self._validator.validate(inert_json)

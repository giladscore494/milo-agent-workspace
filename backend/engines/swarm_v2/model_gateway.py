"""The single guarded model-call path for every Swarm V2 role."""
from __future__ import annotations
import json
from typing import Any, Callable, Mapping
from backend.provider_scheduler import ProviderScheduler, estimate_request_tokens
from backend.runtime import CancellationRequested
from .contracts import CommanderDecision, CommanderPlan


def _commander_instructions(contract: type[CommanderPlan] | type[CommanderDecision],
                            allowed_tools: tuple[str, ...]) -> str:
    """Return bounded, canonical, server-owned Commander instructions."""
    payload = {
        "contract": contract.__name__,
        "json_schema": contract.model_json_schema(),
        "tool_authorization": {
            "allowed_names": list(allowed_tools),
            "rule": ("Every task tools entry must use only an allowed name. "
                     "When allowed_names is empty, every task must use tools: []."),
        },
        "output_rule": "Return only one JSON object matching json_schema.",
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))

class ModelGateway:
    """Compose the existing guarded client and per-run provider scheduler."""
    def __init__(self, *, guarded_client_factory: Callable[[str, str], Any], scheduler: ProviderScheduler,
                 api_key: str, base_url: str, cancellation_checker: Callable[[], bool] | None = None,
                 agent_step_callback: Callable[[str, str], None] | None = None,
                 allowed_tools: frozenset[str] = frozenset()):
        self._client = guarded_client_factory(api_key, base_url)
        self._scheduler, self._cancelled, self._agent_step = scheduler, cancellation_checker, agent_step_callback
        self._allowed_tools = tuple(sorted(allowed_tools))

    def call(self, *, model: str, messages: list[dict[str, Any]], agent: str, phase: str,
             max_tokens: int | None = None, **kwargs: Any) -> Any:
        if self._cancelled and self._cancelled():
            raise CancellationRequested("RUN_CANCELLED")
        if self._agent_step:
            self._agent_step(agent, phase)
        request = {"model": model, "messages": messages, **kwargs}
        if max_tokens is not None:
            request["max_tokens"] = max_tokens
        return self._scheduler.execute(lambda: self._client.chat.completions.create(**request),
            estimated_tokens=estimate_request_tokens(messages, max_tokens), agent=agent, phase=phase)

    def create_plan(self, *, model: str, objective: str, context: Mapping[str, Any]) -> str | bytes | dict[str, Any]:
        response = self.call(model=model, agent="commander", phase="planning", response_format={"type": "json_object"},
            messages=[{"role": "system", "content": _commander_instructions(CommanderPlan, self._allowed_tools)},
                      {"role": "user", "content": json.dumps({"objective": objective, "context": context}, sort_keys=True)}])
        try:
            return response.choices[0].message.content
        except (AttributeError, IndexError, TypeError):
            if isinstance(response, (str, bytes, dict)):
                return response
            raise ValueError("model gateway received an invalid completion response") from None

    def create_replan(self, *, model: str, objective: str, summary: Mapping[str, Any]) -> str | bytes | dict[str, Any]:
        response = self.call(model=model, agent="commander", phase="replanning", response_format={"type": "json_object"},
            messages=[{"role": "system", "content": _commander_instructions(CommanderDecision, self._allowed_tools)},
                      {"role": "user", "content": json.dumps({"objective": objective, "status": summary}, sort_keys=True)}])
        try:
            return response if isinstance(response, (str, bytes, dict)) else response.choices[0].message.content
        except (AttributeError, IndexError, TypeError):
            raise ValueError("model gateway received an invalid completion response") from None

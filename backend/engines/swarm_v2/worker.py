"""Taxonomy-neutral worker using only injected gateway and tools."""
from __future__ import annotations
import json
from dataclasses import dataclass
from typing import Any, Mapping
from backend.runtime import CancellationRequested
from backend.tools import ToolContext, ToolError, ToolRegistry
from backend.tools.registry import validate_json_schema
from .contracts import DynamicTask
from .model_gateway import ModelGateway

@dataclass(frozen=True)
class TaskResult:
    task_id: str
    status: str
    output: Mapping[str, Any] | None = None
    error: Mapping[str, Any] | None = None

class GenericWorker:
    def __init__(self, *, gateway: ModelGateway, tools: ToolRegistry, model: str, tool_context: ToolContext):
        self._gateway, self._tools, self._model, self._tool_context = gateway, tools, model, tool_context

    def execute(self, task: DynamicTask, dependency_outputs: Mapping[str, Any]) -> TaskResult:
        try:
            tool_outputs = {requirement.name: self._tools.execute(requirement.name, self._tool_context, {"query": task.goal})
                            for requirement in task.tools}
            response = self._gateway.call(model=self._model, agent=f"worker:{task.task_id}", phase="execute",
                messages=[{"role": "user", "content": json.dumps({"goal": task.goal, "scope": task.scope,
                    "dependencies": dependency_outputs, "tools": tool_outputs, "output_schema": task.output_schema}, sort_keys=True)}],
                response_format={"type": "json_object"})
            content = response if isinstance(response, dict) else response.choices[0].message.content
            output = content if isinstance(content, dict) else json.loads(content)
            validate_json_schema(task.output_schema, output, "$model_output")
            return TaskResult(task.task_id, "completed", output=output)
        except CancellationRequested:
            raise
        except ToolError as exc:
            return TaskResult(task.task_id, "failed", error=exc.as_dict()["error"])
        except Exception as exc:
            return TaskResult(task.task_id, "failed", error={"code": "TASK_FAILED", "message": str(exc)})

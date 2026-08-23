"""Engine contract and trusted, allowlist-only workflow routing."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from backend.errors import AppError


class Engine(Protocol):
    workflow_key: str

    def run(self, run: dict[str, Any]) -> dict[str, Any]: ...


EngineFactory = Callable[[], Engine]


class EngineRegistry:
    """An immutable allowlist of workflow keys and their local factories."""

    def __init__(self, factories: Mapping[str, EngineFactory]) -> None:
        self._factories = dict(factories)
        if not self._factories or any(not key or not callable(factory) for key, factory in self._factories.items()):
            raise ValueError("engine registry requires named, callable factories")

    def require(self, workflow_key: str) -> EngineFactory:
        try:
            return self._factories[workflow_key]
        except KeyError as exc:
            raise AppError("ENGINE_NOT_ALLOWED", f"workflow is not allowlisted: {workflow_key}", 403) from exc


@dataclass(frozen=True)
class ResolvedEngine:
    workflow_key: str
    factory: EngineFactory


class EngineResolver:
    """Resolve only through run -> conversation -> project trusted records."""

    def __init__(self, repository: Any, registry: EngineRegistry) -> None:
        self.repository = repository
        self.registry = registry

    def resolve(self, run: dict[str, Any]) -> ResolvedEngine:
        conversation_id = run.get("conversation_id")
        if not conversation_id:
            raise AppError("ENGINE_NOT_ALLOWED", "run has no trusted conversation", 403)
        conversation = self.repository.get_conversation(conversation_id)
        project_id = conversation.get("project_id")
        if not project_id:
            raise AppError("ENGINE_NOT_ALLOWED", "conversation has no trusted project", 403)
        project = self.repository.get_project(project_id)
        workflow_key = project.get("workflow_key")
        if not isinstance(workflow_key, str) or not workflow_key:
            raise AppError("ENGINE_NOT_ALLOWED", "project has no allowed workflow", 403)
        return ResolvedEngine(workflow_key=workflow_key, factory=self.registry.require(workflow_key))


__all__ = ["Engine", "EngineFactory", "EngineRegistry", "EngineResolver", "ResolvedEngine"]

"""Engine contract and trusted, allowlist-only workflow routing."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from backend.errors import AppError
from backend.run_identity import RunIdentity, RunIdentityError, persisted_identity


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
    #: The run's persisted identity, or ``None`` for a run created before the
    #: identity column existed. Never invented: an unpinned run is reported as
    #: unpinned, and callers decide what that permits.
    identity: RunIdentity | None = None

    @property
    def pinned(self) -> bool:
        """Was this routing decision READ from the run, or derived for it?"""
        return self.identity is not None


class EngineResolver:
    """Route a run to its engine from what the run IS, not from what a project
    happens to say today.

    This used to resolve ``run -> conversation -> project`` and take the
    PROJECT's current ``workflow_key`` as the answer, every time a worker
    claimed the run. The relation is trusted, but the answer is not stable:
    a project switched from ``vehicle_catalog_v1`` to ``swarm_v2`` between a
    run's creation and its launch -- or between its first attempt and a Cloud
    Run retry -- changed what that run WAS. The same run row could execute as
    V1 on attempt 1 and as V2 on attempt 2, with the second attempt resuming
    the first's checkpoint into a different engine.

    So routing now READS the identity bound at run creation
    (``backend/run_identity.py``), which is exactly what the engine question
    needed all along: an answer decided once, before execution, from the
    trusted relation, and immutable afterwards. Resume and retry get the same
    answer as the first attempt because they read the same record.

    The project relation remains the resolver's ONLY fallback, and only for a
    run created before identities existed. That is not an inference: such a run
    recorded no engine, so there is nothing to preserve, and this is the
    behaviour it has always had. It is reported as unpinned
    (``ResolvedEngine.pinned`` is False) rather than passed off as a pinned
    answer, and nothing writes an identity onto it afterwards -- binding an
    identity to a run that has already executed would be inventing history.
    """

    def __init__(self, repository: Any, registry: EngineRegistry) -> None:
        self.repository = repository
        self.registry = registry

    def resolve(self, run: dict[str, Any]) -> ResolvedEngine:
        try:
            identity = persisted_identity(run)
        except RunIdentityError as exc:
            # A present-but-untrustworthy identity is never downgraded to the
            # legacy path: that would let a corrupted record buy a re-derived
            # engine, which is the drift this class exists to remove.
            raise AppError("ENGINE_NOT_ALLOWED",
                           "the run's persisted identity cannot be trusted", 403) from exc
        if identity is not None:
            return ResolvedEngine(workflow_key=identity.workflow_key,
                                  factory=self.registry.require(identity.workflow_key),
                                  identity=identity)
        workflow_key = self._legacy_workflow_key(run)
        return ResolvedEngine(workflow_key=workflow_key,
                              factory=self.registry.require(workflow_key),
                              identity=None)

    def _legacy_workflow_key(self, run: dict[str, Any]) -> str:
        """The pre-identity route: trusted server relations only, as before."""
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
        return workflow_key


__all__ = ["Engine", "EngineFactory", "EngineRegistry", "EngineResolver", "ResolvedEngine"]

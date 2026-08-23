"""Constructor-injected boundaries used by the future Swarm runtime."""

from typing import Any, Mapping, Protocol


class CommanderClient(Protocol):
    def create_plan(self, *, model: str, objective: str, context: Mapping[str, Any]) -> str | bytes | dict[str, Any]: ...


class StateStore(Protocol):
    def load(self, run_id: str) -> Mapping[str, Any] | None: ...
    def save(self, run_id: str, state: Mapping[str, Any]) -> None: ...


class EventSink(Protocol):
    def emit(self, event: Mapping[str, Any]) -> None: ...


class CancellationChecker(Protocol):
    def __call__(self) -> None: ...

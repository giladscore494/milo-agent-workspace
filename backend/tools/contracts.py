"""Provider-neutral, fail-closed tool contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping, Protocol

from backend.runtime import CancellationRequested


class ToolMode(str, Enum):
    READ = "read"
    WRITE = "write"


@dataclass(frozen=True)
class ToolContext:
    scopes: frozenset[str] = field(default_factory=frozenset)
    capabilities: frozenset[str] = field(default_factory=frozenset)
    write_approved: bool = False
    cancellation_checker: Callable[[], bool] | None = None

    def check_cancelled(self) -> None:
        if self.cancellation_checker and self.cancellation_checker():
            raise CancellationRequested("RUN_CANCELLED")


@dataclass(frozen=True)
class ToolOperation:
    """One named, individually schema'd capability of a registered tool.

    A tool is a namespace; an OPERATION is the unit a plan selects and the
    unit the Registry validates. Each operation owns its own closed input and
    output schema, so `vehicle_catalog.get_model(make, model)` and
    `vehicle_catalog.list_models(make)` can never be confused for one another
    or share a single lowest-common-denominator payload shape.
    """

    name: str
    description: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]


class ToolError(Exception):
    """A safe, structured tool failure (never a provider traceback)."""

    def __init__(self, code: str, message: str, *, tool: str | None = None):
        super().__init__(message)
        self.code, self.message, self.tool = code, message, tool

    def as_dict(self) -> dict[str, Any]:
        return {"ok": False, "error": {"code": self.code, "message": self.message, "tool": self.tool}}


class Tool(Protocol):
    name: str
    description: str
    mode: ToolMode
    required_scope: str
    operations: Mapping[str, ToolOperation]

    def execute(self, context: ToolContext, operation: str,
                payload: Mapping[str, Any]) -> Mapping[str, Any]: ...

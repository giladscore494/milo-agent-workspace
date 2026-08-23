"""Serializable planning state; persistence is supplied by an injected adapter."""

from typing import Any
from pydantic import ConfigDict
from .contracts import StrictContract


class SwarmState(StrictContract):
    model_config = ConfigDict(extra="forbid", strict=True)
    run_id: str
    objective: str
    approved_plan: dict[str, Any] | None = None

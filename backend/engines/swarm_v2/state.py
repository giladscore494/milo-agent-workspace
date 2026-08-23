"""Versioned, fail-closed Swarm V2 checkpoint state."""

from typing import Any
from pydantic import ConfigDict, Field
from .contracts import StrictContract
from .evidence import safe_durable_value


class SwarmState(StrictContract):
    model_config = ConfigDict(extra="forbid", strict=True)
    run_id: str
    objective: str
    engine_version: str = "swarm_v2.1"
    workflow_key: str = "swarm_v2"
    graph_revision: int = 1
    approved_plan: dict[str, Any] | None = None
    completed_task_ids: list[str] = Field(default_factory=list)
    task_outputs: dict[str, dict[str, Any]] = Field(default_factory=dict)
    evidence_references: list[dict[str, Any]] = Field(default_factory=list)
    replans: list[dict[str, Any]] = Field(default_factory=list)
    verifier_state: dict[str, dict[str, Any]] = Field(default_factory=dict)
    usage_snapshot: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def resume(cls, raw: Any, *, run_id: str, workflow_key: str = "swarm_v2") -> "SwarmState":
        safe_durable_value(raw)
        state = cls.model_validate(raw)
        if state.run_id != run_id or state.workflow_key != workflow_key or state.engine_version != "swarm_v2.1":
            raise ValueError("incompatible Swarm V2 checkpoint")
        if len(state.completed_task_ids) != len(set(state.completed_task_ids)):
            raise ValueError("checkpoint contains duplicate completed tasks")
        return state

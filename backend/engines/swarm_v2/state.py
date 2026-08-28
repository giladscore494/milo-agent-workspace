"""Versioned, fail-closed Swarm V2 checkpoint state."""

from typing import Any
from pydantic import ConfigDict, Field
from .contracts import StrictContract
from .evidence import safe_durable_value
from .grounding import VERIFIER_GROUNDING_VERSION


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
    # The verifier-grounding contract behind verifier_state. A checkpoint
    # written before source-grounded verification existed simply lacks the
    # field and loads as 0, which is exactly what B5 needs to know: its
    # model-backed verdicts were reached from metadata alone and must be
    # revalidated rather than trusted. A version this release does not
    # understand fails closed instead of being read optimistically.
    verifier_grounding_version: int = Field(default=0, ge=0,
                                            le=VERIFIER_GROUNDING_VERSION)
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

from uuid import uuid4

import pytest

from backend.runtime import Checkpoint, InMemoryEventSink, InvalidTransition, RunEventRecord, validate_transition


def test_state_transitions_reject_invalid_terminal_move():
    validate_transition("queued", "starting")
    validate_transition("running", "partial_success")
    with pytest.raises(InvalidTransition):
        validate_transition("completed", "running")


def test_in_memory_event_persistence_requires_typed_events():
    sink = InMemoryEventSink()
    run_id = uuid4()
    event = sink.emit(RunEventRecord(run_id=run_id, type="run_created", message="created", payload={"x": 1}))
    assert sink.events == [event]
    assert event.as_payload()["run_id"] == str(run_id)
    assert event.as_payload()["type"] == "run_created"
    with pytest.raises(ValueError):
        sink.emit(RunEventRecord(run_id=run_id, type="ENGINE_COMPLETED", message="legacy"))


def test_checkpoint_record_preserves_partial_artifacts_and_resume_boundary():
    run_id = uuid4()
    checkpoint = Checkpoint(
        run_id=run_id,
        engine_version="vehicle_catalog_v1.stage3",
        workflow_key="vehicle_catalog_v1",
        phase="technical_enrichment:dimensions_specs",
        completed_tasks=["discovery_phase", "python_discovery_merge", "normalizer_deduper", "technical_enrichment_phase"],
        artifacts={"technical_enrichment_phase": [{"agent": "dimensions_specs", "status": "partial"}]},
        failures=[{"agent": "pricing", "error": "timeout"}],
        token_usage={"input_tokens": 10, "output_tokens": 5},
        last_event={"type": "checkpoint_saved"},
        attempt=2,
    ).to_record()
    assert checkpoint["phase"] == "technical_enrichment:dimensions_specs"
    assert checkpoint["artifacts"]["technical_enrichment_phase"][0]["status"] == "partial"
    assert checkpoint["failures"][0]["agent"] == "pricing"
    assert checkpoint["attempt"] == 2

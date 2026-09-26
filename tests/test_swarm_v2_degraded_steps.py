"""PR-X S4: production-scale proof that paid, completed work is never discarded.

Built on tests/swarm_v2_production_fixture.py: a 6,374-row Government
snapshot, a 20-item prepared queue (one placeholder, one duplicate-identity
pair) and a Commander plan whose task output schemas use enum / description /
minimum and one stripped annotation. Everything below the plan is the real
engine, tool, evidence board, verifier, builder and assembler.

1. The whole batch runs end to end offline and reaches partial_success with
   vehicles AND unresolved groups, without one whole-snapshot read.
2. For each step S3 made degradable, the failure is injected at THAT step after
   11 completed tasks: the run is partial_success, its evidence is kept, the
   result carries exactly the step's static code, the one log line is written,
   and no paid call is made twice for that step.
"""

from __future__ import annotations

import json
import logging

import pytest

import backend.engines.swarm_v2.engine as engine_module
from backend.catalog.result.assembler import VehicleCatalogResultAssembler
from backend.engines.swarm_v2 import validate_product_outcome
from backend.engines.swarm_v2.commander import CommanderPlanFailure
from backend.engines.swarm_v2.contracts import DynamicTask
from backend.engines.swarm_v2.outcome import degraded_review_item, with_degraded_review
from backend.provider_streaming import PROVIDER_STREAM_INTERRUPTED, ProviderTransportFailure
from swarm_v2_production_fixture import (DUPLICATE_IDS, OUTPUT_SCHEMA, PLACEHOLDER_ID,
                                         QUEUE_ITEMS, SNAPSHOT_ROWS, build_production_run)

DEGRADED_LOGGER = "milo.swarm_v2.degraded"
ELEVEN = 11


def degraded_lines(caplog) -> list[dict]:
    return [json.loads(record.getMessage()) for record in caplog.records
            if record.name == DEGRADED_LOGGER]


def state_of(run) -> dict:
    return run.checkpoints[-1]["artifacts"]["swarm_state"]


# =============================================================================
# 1. the production-scale fixture, end to end
# =============================================================================

def test_the_fixture_is_production_shaped():
    run = build_production_run()
    snapshot_rows = sum(1 for row in run.repository.catalog_candidates.values()
                        if row["snapshot_id"] == run.preparation.snapshot_id)
    assert snapshot_rows == SNAPSHOT_ROWS
    # 20 batch items; the placeholder is excluded, the duplicate pair is kept.
    assert run.preparation.excluded == ((PLACEHOLDER_ID, "EXCLUDED_PLACEHOLDER_SOURCE_RECORD"),)
    assert len(run.preparation.queue) == QUEUE_ITEMS - 1
    identities = [(item.commercial_model, item.official_model_code, item.trim)
                  for item in run.preparation.queue]
    assert len(identities) - len(set(identities)) == 1   # one duplicate-identity pair
    # enum / description / minimum are kept; `format` is stripped by name.
    task = DynamicTask.model_validate(run.plan["graph"]["tasks"][0])
    properties = task.output_schema["properties"]
    assert properties["register_answer"]["enum"] == ["resolved", "ambiguous", "not_found"]
    assert properties["match_count"]["minimum"] == 0
    assert "description" in properties["summary"]
    assert "format" not in properties["checked_on"]
    assert "format" in OUTPUT_SCHEMA["properties"]["checked_on"]
    assert task.stripped_output_keywords == ["format"]


def test_the_whole_batch_reaches_partial_success_with_vehicles_and_unresolved_groups():
    run = build_production_run()
    result = run.run()

    assert (result["status"], result["result_kind"]) == ("partial_success", "partial_result")
    validate_product_outcome(result)
    # 17 distinct register rows resolved into vehicles; the duplicate pair is
    # ONE ambiguous group naming both rows, never merged into a vehicle.
    assert result["summary"]["vehicles_resolved"] == 17 == len(result["vehicles"])
    (group,) = result["unresolved_groups"]
    assert group["outcome"] == "unresolved_ambiguous"
    assert sorted(group["record_ids"]) == sorted(DUPLICATE_IDS)
    assert sorted(item["code"] for item in result["needs_review"]) == \
        ["CANDIDATE_UNRESOLVED_AMBIGUOUS"] * 2
    # One worker call per task, no verifier model call (register claims verify
    # deterministically), one Commander decision.
    assert len(run.worker_gateway.calls) == QUEUE_ITEMS - 1
    assert run.verifier_gateway.calls == 0
    assert run.commander.replans == 1
    # No whole-snapshot read, and nothing near the snapshot's size was read.
    assert run.spy.whole_snapshot_reads == []
    assert 0 < run.spy.catalog_rows_read < 200 < SNAPSHOT_ROWS


# =============================================================================
# 2. every degradable step, injected after 11 completed tasks
# =============================================================================

@pytest.fixture
def baseline():
    """The same 11-task run with nothing injected."""
    run = build_production_run(tasks=ELEVEN)
    result = run.run()
    assert result["status"] == "partial_success"
    return run, result


def evidence_shape(run) -> list[tuple]:
    """The checkpointed evidence, minus the per-run identifiers."""
    return sorted((item["task_id"], item["field"], json.dumps(item["value"]))
                  for item in state_of(run)["evidence_references"])


def fields_shape(result) -> dict:
    return {field: sorted(json.dumps(entry["value"]) for entry in entries)
            for field, entries in result["fields"].items()}


def assert_degraded(run, result, code, baseline, caplog, *, fields_kept: bool = True):
    base_run, base_result = baseline
    assert result["status"] == "partial_success"
    validate_product_outcome(result)
    # Evidence kept: the same durable claims, the same checkpointed references.
    assert len(run.claims()) == len(base_run.claims()) > 0
    assert evidence_shape(run) == evidence_shape(base_run)
    assert sorted(state_of(run)["completed_task_ids"]) == [f"t{i:02d}" for i in range(1, 12)]
    if fields_kept:
        assert fields_shape(result) == fields_shape(base_result)
    # Exactly the step's static code, once.
    codes = [item["code"] for item in result["needs_review"]]
    assert codes.count(code) == 1
    assert degraded_review_item(code) in result["needs_review"]
    assert [line["code"] for line in degraded_lines(caplog)] == [code]
    # No paid call was made twice: 11 worker calls, one Commander decision,
    # no verifier model call.
    assert len(run.worker_gateway.calls) == ELEVEN
    assert run.commander.replans == 1
    assert run.verifier_gateway.calls == 0


@pytest.mark.parametrize("decision", [
    {"decision": "FINISH", "plan": None, "reason": "done", "unexpected": 1},
    "not json at all",
])
def test_commander_replan_rejected(decision, baseline, caplog):
    caplog.set_level(logging.INFO, logger=DEGRADED_LOGGER)
    run = build_production_run(tasks=ELEVEN, decisions=[decision])
    result = run.run()
    assert_degraded(run, result, "COMMANDER_REPLAN_REJECTED", baseline, caplog)
    assert state_of(run)["replans"] == [{"decision": "REJECTED",
                                         "code": "COMMANDER_DECISION_INVALID",
                                         "graph_revision": 1}]
    assert run.ledger.count("replan") == 1
    # Resume from the final checkpoint: the Commander is not asked again.
    before = run.commander.replans
    again = run.run(checkpoint=run.checkpoints[-1])
    assert run.commander.replans == before
    assert again["needs_review"] == result["needs_review"]


def test_a_replan_that_rewrites_completed_work_is_rejected_not_fatal(baseline, caplog):
    caplog.set_level(logging.INFO, logger=DEGRADED_LOGGER)
    probe = build_production_run(tasks=ELEVEN)
    rewritten = json.loads(json.dumps(probe.plan))
    rewritten["graph"]["tasks"][0]["goal"] = "a different goal for a completed task"
    run = build_production_run(tasks=ELEVEN, decisions=[
        {"decision": "REVISE_TASK", "plan": rewritten, "reason": "revise"}])
    result = run.run()
    assert_degraded(run, result, "COMMANDER_REPLAN_REJECTED", baseline, caplog)
    assert state_of(run)["replans"][0]["code"] == "SWARM_V2_REPLAN_REWRITES_COMPLETED"


def test_coverage_check_failed(baseline, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger=DEGRADED_LOGGER)
    run = build_production_run(tasks=ELEVEN)
    monkeypatch.setattr(run.engine, "_coverage_gaps",
                        lambda *_a, **_k: (_ for _ in ()).throw(KeyError("gap")))
    result = run.run()
    assert_degraded(run, result, "COVERAGE_CHECK_FAILED", baseline, caplog)


def test_conflict_detection_failed(baseline, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger=DEGRADED_LOGGER)
    run = build_production_run(tasks=ELEVEN)
    monkeypatch.setattr(engine_module, "conflict_groups",
                        lambda *_a, **_k: (_ for _ in ()).throw(TypeError("grouping")))
    result = run.run()
    assert_degraded(run, result, "CONFLICT_DETECTION_FAILED", baseline, caplog)


def base_verdicts(baseline):
    base_run, _ = baseline
    return base_run.evidence.verdicts


class CountingVerifier:
    """Wraps the real verifier; its paid step raises a provider transport failure."""

    def __init__(self, inner):
        self._inner = inner
        self.paid_steps = 0

    def prepare(self, *args, **kwargs):
        return self._inner.prepare(*args, **kwargs)

    def verify_prepared(self, *args, **kwargs):
        self.paid_steps += 1
        raise ProviderTransportFailure(PROVIDER_STREAM_INTERRUPTED, role="verifier:verify")


def test_verifier_failed_keeps_evidence_and_is_never_paid_twice(baseline, caplog):
    caplog.set_level(logging.INFO, logger=DEGRADED_LOGGER)
    holder = {}

    def wrap(kwargs):
        holder["verifier"] = CountingVerifier(kwargs["verifier"])
        return {"verifier": holder["verifier"]}

    run = build_production_run(tasks=ELEVEN, engine_kwargs=wrap)
    result = run.run()
    # Register claims settle DETERMINISTICALLY before any paid batch, so those
    # verdicts are real and kept (and durable); only the paid step failed.
    assert_degraded(run, result, "VERIFIER_FAILED", baseline, caplog)
    assert holder["verifier"].paid_steps == 1
    assert len(run.evidence.verdicts) == len(base_verdicts(baseline))
    assert state_of(run)["degraded_steps"] == [
        {"step": "verification", "code": "VERIFIER_FAILED", "graph_revision": 1}]
    # A resume of the same pass does not pay for verification again.
    again = run.run(checkpoint=run.checkpoints[-1])
    assert holder["verifier"].paid_steps == 1
    assert again["needs_review"] == result["needs_review"]


def test_verification_budget_insufficient_after_eleven_tasks(caplog):
    """The pre-flight refusal: the model-backed verifier harness (11 tasks)."""
    from backend.engines.swarm_v2 import RemainingBudget
    from test_swarm_v2_replan_rejection import TASK_IDS, build

    caplog.set_level(logging.INFO, logger=DEGRADED_LOGGER)
    engine, run, client, calls, checkpoints, _events, _ledger = build(
        [{"decision": "FINISH", "plan": None, "reason": "done"}])
    engine._remaining_budget = lambda: RemainingBudget(
        cost_units=100_000, tool_calls=100, tasks=64,
        model_calls=100 if len(calls) < len(TASK_IDS) else 0)
    gateway_calls = []
    real = engine._verifier._gateway.call
    engine._verifier._gateway.call = lambda **kw: gateway_calls.append(1) or real(**kw)
    result = engine.run(run)
    assert sorted(calls) == TASK_IDS
    assert result["status"] == "partial_success" and result["fields"] == {}
    assert degraded_review_item("VERIFICATION_BUDGET_INSUFFICIENT") in result["needs_review"]
    assert gateway_calls == []          # the impossible sequence was never started
    assert client.replans == 1
    assert len(checkpoints[-1]["artifacts"]["swarm_state"]["evidence_references"]) == 11
    assert [line["code"] for line in degraded_lines(caplog)] == \
        ["VERIFICATION_BUDGET_INSUFFICIENT"]


def test_candidate_outcomes_failed(baseline, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger=DEGRADED_LOGGER)
    run = build_production_run(tasks=ELEVEN)
    monkeypatch.setattr(run.engine, "_candidate_outcomes",
                        lambda *_a, **_k: (_ for _ in ()).throw(ValueError("outcomes")))
    result = run.run()
    assert_degraded(run, result, "CANDIDATE_OUTCOMES_FAILED", baseline, caplog)
    assert "candidate_outcomes" not in result and "vehicles" not in result


def test_assembler_failed_omits_only_the_vehicle_view(baseline, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger=DEGRADED_LOGGER)
    run = build_production_run(tasks=ELEVEN)
    monkeypatch.setattr(VehicleCatalogResultAssembler, "assemble",
                        lambda *_a, **_k: (_ for _ in ()).throw(KeyError("view")))
    result = run.run()
    assert_degraded(run, result, "ASSEMBLER_FAILED", baseline, caplog)
    _base_run, base_result = baseline
    assert {"vehicles", "unresolved_groups", "summary"} <= set(base_result)
    assert not {"vehicles", "unresolved_groups", "summary"} & set(result)
    # Every existing key keeps its value.
    assert [(item["task_id"], item["outcome"]) for item in result["candidate_outcomes"]] == \
        [(item["task_id"], item["outcome"]) for item in base_result["candidate_outcomes"]]


def test_final_build_degraded_retries_without_the_register_view(baseline, caplog):
    caplog.set_level(logging.INFO, logger=DEGRADED_LOGGER)
    seen = []

    def wrap(kwargs):
        real = kwargs["builder"]

        class FlakyBuilder:
            def build(self, evidence, verdicts, **kw):
                seen.append(bool(kw["candidate_outcomes"]))
                if kw["candidate_outcomes"]:
                    raise RuntimeError("builder defect")
                return real.build(evidence, verdicts, **kw)
        return {"builder": FlakyBuilder()}

    run = build_production_run(tasks=ELEVEN, engine_kwargs=wrap)
    result = run.run()
    assert_degraded(run, result, "FINAL_BUILD_DEGRADED", baseline, caplog)
    assert seen == [True, False]
    assert "candidate_outcomes" not in result and "vehicles" not in result


def test_final_build_degraded_to_the_static_items_when_the_builder_cannot_build(
        baseline, caplog):
    caplog.set_level(logging.INFO, logger=DEGRADED_LOGGER)

    class BrokenBuilder:
        def build(self, *_a, **_k):
            raise RuntimeError("builder defect")

    run = build_production_run(tasks=ELEVEN, engine_kwargs=lambda _k: {"builder": BrokenBuilder()})
    result = run.run()
    assert_degraded(run, result, "FINAL_BUILD_DEGRADED", baseline, caplog, fields_kept=False)
    assert (result["status"], result["result_kind"]) == ("partial_success", "no_usable_result")
    assert result["needs_review"][-1] == {"code": "NO_USABLE_RESULT"}


def test_catalog_promotion_failed_on_the_eleven_task_result(baseline):
    """The worker step runs AFTER the engine returned (see
    test_swarm_v2_degraded_outcome for the real execute_run); applied here to
    the production-scale 11-task payload, it keeps every key and value."""
    _run, result = baseline
    degraded = with_degraded_review(result, "CATALOG_PROMOTION_FAILED")
    assert degraded["status"] == "partial_success"
    assert degraded["fields"] == result["fields"]
    assert degraded["vehicles"] == result["vehicles"]
    assert degraded["needs_review"] == [*result["needs_review"],
                                        degraded_review_item("CATALOG_PROMOTION_FAILED")]


# =============================================================================
# 3. what stays fatal
# =============================================================================

def test_a_transport_failure_of_the_replan_still_fails_the_run():
    failure = ProviderTransportFailure(PROVIDER_STREAM_INTERRUPTED, role="commander:replanning")
    run = build_production_run(tasks=ELEVEN, decisions=[failure])
    with pytest.raises(ProviderTransportFailure):
        run.run()
    assert len(run.claims()) > 0  # the evidence is still durable for a resume


def test_a_completion_failure_of_the_replan_still_fails_the_run():
    run = build_production_run(tasks=ELEVEN, decisions=[
        CommanderPlanFailure("COMMANDER_COMPLETION_FAILED")])
    with pytest.raises(CommanderPlanFailure) as caught:
        run.run()
    assert caught.value.code == "COMMANDER_COMPLETION_FAILED"

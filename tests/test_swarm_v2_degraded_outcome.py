"""PR-X S3: paid, completed work is never discarded after the first task completes.

Unit coverage for the static degraded-step vocabulary, the one structured log
line, the replan-RULE refusals (which now reject the proposal instead of
failing the run when evidence exists), and the worker's catalog-promotion
step. The per-step injection tests at production scale live in
tests/test_swarm_v2_degraded_steps.py (S4).
"""

from __future__ import annotations

import json
import logging

import pytest

import backend.catalog.pipeline as pipeline_module
from backend.engines.swarm_v2.failures import (NOT_DEGRADABLE, SwarmExecutionFailure,
                                               log_step_degraded)
from backend.engines.swarm_v2.outcome import (DEGRADED_STEP_CODES, NO_USABLE_RESULT_CODE,
                                              ProductOutcomeError, degraded_review_item,
                                              finalize_product_outcome,
                                              validate_product_outcome,
                                              with_degraded_review)
from backend.errors import AppError
from test_swarm_v2 import plan, task
from test_swarm_v2_replan_rejection import TASK_IDS, build, eleven_task_plan

USABLE = {"answer": [{"value": "v", "provenance": {"claim_id": "c1"}}]}


# --- the static vocabulary ---------------------------------------------------

def test_every_degraded_code_is_static_and_maps_to_a_static_step():
    assert set(DEGRADED_STEP_CODES) == {
        "COMMANDER_REPLAN_REJECTED", "COVERAGE_CHECK_FAILED", "CONFLICT_DETECTION_FAILED",
        "VERIFIER_FAILED", "VERIFICATION_BUDGET_INSUFFICIENT", "CANDIDATE_OUTCOMES_FAILED",
        "ASSEMBLER_FAILED", "FINAL_BUILD_DEGRADED", "CATALOG_PROMOTION_FAILED"}
    for code, step in DEGRADED_STEP_CODES.items():
        assert code.isupper() and step.islower()
        assert degraded_review_item(code) == {"task_id": step, "code": code}


def test_an_unknown_code_is_refused():
    with pytest.raises(ValueError):
        degraded_review_item("anything the model wrote")


def test_a_degraded_item_makes_a_usable_result_partial_never_complete():
    final = finalize_product_outcome(fields=USABLE,
                                     coverage_gaps=[degraded_review_item("VERIFIER_FAILED")])
    assert (final["status"], final["result_kind"]) == ("partial_success", "partial_result")
    validate_product_outcome(final)


# --- with_degraded_review (the worker's post-finalization step) -------------

def test_with_degraded_review_demotes_complete_to_partial_success():
    complete = finalize_product_outcome(fields=USABLE)
    assert complete["status"] == "complete"
    degraded = with_degraded_review(complete, "CATALOG_PROMOTION_FAILED")
    assert (degraded["status"], degraded["result_kind"]) == ("partial_success", "partial_result")
    assert degraded["needs_review"] == [degraded_review_item("CATALOG_PROMOTION_FAILED")]
    assert degraded["fields"] == complete["fields"]
    # Idempotent: the item is never listed twice.
    assert with_degraded_review(degraded, "CATALOG_PROMOTION_FAILED") == degraded


def test_with_degraded_review_keeps_the_single_trailing_marker():
    empty = finalize_product_outcome(fields={})
    degraded = with_degraded_review(empty, "CATALOG_PROMOTION_FAILED")
    assert degraded["needs_review"] == [degraded_review_item("CATALOG_PROMOTION_FAILED"),
                                        {"code": NO_USABLE_RESULT_CODE}]
    validate_product_outcome(degraded)


def test_with_degraded_review_carries_every_other_key_through():
    outcome = {**finalize_product_outcome(fields=USABLE),
               "candidate_outcomes": [{"outcome": "resolved", "task_id": "t"}]}
    assert with_degraded_review(outcome, "CATALOG_PROMOTION_FAILED")["candidate_outcomes"] == \
        outcome["candidate_outcomes"]


def test_with_degraded_review_refuses_an_invalid_payload():
    with pytest.raises(ProductOutcomeError):
        with_degraded_review({"status": "complete"}, "CATALOG_PROMOTION_FAILED")


# --- the one structured log line ---------------------------------------------

def test_the_degraded_line_names_the_class_only(caplog):
    caplog.set_level(logging.INFO, logger="milo.swarm_v2.degraded")
    log_step_degraded("verification", "VERIFIER_FAILED", "KeyError")
    (record,) = caplog.records
    assert json.loads(record.getMessage()) == {
        "event": "swarm_step_degraded", "step": "verification", "code": "VERIFIER_FAILED",
        "exception_class": "KeyError"}


def test_infrastructure_faults_and_invariant_violations_are_never_degraded():
    from backend.budget import BudgetExceeded
    from backend.runtime import CancellationRequested
    assert {CancellationRequested, BudgetExceeded, AppError, AssertionError} <= set(NOT_DEGRADABLE)


# --- replan RULE refusals: rejected with evidence, fatal without -------------

def rule_break(name: str) -> tuple[dict, dict]:
    """(initial plan, ADD_TASKS decision) that breaks one replan rule."""
    initial = eleven_task_plan()
    extended = plan([*initial["graph"]["tasks"], task("t12", "more")], max_replans=1)
    if name == "SWARM_V2_REPLAN_REWRITES_COMPLETED":
        extended["graph"]["tasks"][0]["goal"] = "a different goal"
    if name == "SWARM_V2_MAX_REPLANS_EXCEEDED":
        # A real gap (t01's evidence requirement names a field no claim
        # states), so the rule that bites is the spent allowance.
        initial = eleven_task_plan(max_replans=0)
        initial["graph"]["tasks"][0]["evidence"]["required_fields"] = ["other"]
        extended = plan([*initial["graph"]["tasks"], task("t12", "more")], max_replans=0)
    return initial, {"decision": "ADD_TASKS", "plan": extended, "reason": "more"}


@pytest.mark.parametrize("code", ["SWARM_V2_REPLAN_REQUIRES_GAP",
                                  "SWARM_V2_MAX_REPLANS_EXCEEDED"])
def test_a_rule_refusal_with_evidence_rejects_the_proposal(code, caplog):
    caplog.set_level(logging.INFO, logger="milo.swarm_v2.degraded")
    initial, decision = rule_break(code)
    engine, run, client, calls, checkpoints, _events, ledger = build([decision], initial=initial)
    result = engine.run(run)
    assert sorted(calls) == TASK_IDS and client.replans == 1
    assert result["status"] == "partial_success"
    assert degraded_review_item("COMMANDER_REPLAN_REJECTED") in result["needs_review"]
    assert checkpoints[-1]["artifacts"]["swarm_state"]["replans"] == [
        {"decision": "REJECTED", "code": code, "graph_revision": 1}]
    assert ledger.kinds.count("replan") == 1
    lines = [json.loads(r.getMessage()) for r in caplog.records]
    assert lines == [{"event": "swarm_step_degraded", "step": "commander_replan",
                      "code": "COMMANDER_REPLAN_REJECTED",
                      "exception_class": "SwarmExecutionFailure"}]


def test_a_rule_refusal_with_nothing_usable_fails_with_its_code_as_before():
    initial, decision = rule_break("SWARM_V2_MAX_REPLANS_EXCEEDED")
    engine, run, client, *_ = build([decision], initial=initial, fail_tasks=True)
    with pytest.raises(SwarmExecutionFailure) as caught:
        engine.run(run)
    assert caught.value.code == "SWARM_V2_MAX_REPLANS_EXCEEDED"
    assert client.replans == 1


# --- the worker's catalog promotion ------------------------------------------

def run_with_promotion(monkeypatch, error: BaseException):
    from test_catalog_execution_flag import run_swarm_with_catalog_flag

    def failing_promote(self):
        raise error

    monkeypatch.setattr(pipeline_module.CatalogPromotionPipeline, "promote", failing_promote)
    return run_swarm_with_catalog_flag(monkeypatch, "true", read="true", promotion="true")


def test_a_promotion_defect_finalizes_the_verified_result_as_partial_success(monkeypatch,
                                                                            caplog):
    caplog.set_level(logging.INFO, logger="milo.swarm_v2.degraded")
    _record, repo, run_id, _ = run_with_promotion(monkeypatch, KeyError("provider text"))
    run = repo.get_run(run_id)
    assert run["status"] == "partial_success"
    assert degraded_review_item("CATALOG_PROMOTION_FAILED") in run["output"]["needs_review"]
    validate_product_outcome(run["output"])
    lines = [json.loads(r.getMessage()) for r in caplog.records
             if r.name == "milo.swarm_v2.degraded"]
    assert {"event": "swarm_step_degraded", "step": "catalog_promotion",
            "code": "CATALOG_PROMOTION_FAILED", "exception_class": "KeyError"} in lines
    assert "provider text" not in json.dumps(run["output"])


def test_a_promotion_infrastructure_fault_still_escapes(monkeypatch):
    with pytest.raises(AppError):
        run_with_promotion(monkeypatch, AppError("LEASE_LOST", "lease lost", 409))

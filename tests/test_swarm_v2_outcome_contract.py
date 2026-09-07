"""R1: the truthful Swarm V2 product-outcome contract.

Technical execution finishing is not a product result. These tests pin the
one deterministic, application-owned classification that separates a useful
verified result from a partial one, from no usable result at all, and from a
confirmed negative — and prove that nothing model-controlled can move it.
"""

from __future__ import annotations

import json
from copy import deepcopy
import pytest

import backend.worker.main as worker_main
from backend.engines.swarm_v2 import (
    ALLOWED_OUTCOMES, NO_USABLE_RESULT_CODE, RESULT_KINDS, BoundedTaskExecutor,
    EvidenceReference, FinalBuilder, ProductOutcomeError, SwarmState, SwarmV2Engine,
    TaskResult, TrustedNegativeResult, VerificationVerdict, Verifier, decide_outcome,
    durable_run_status, finalize_product_outcome, validate_product_outcome,
)
from backend.errors import AppError
from test_swarm_v2 import plan, task
from test_swarm_v2_stage1_e2e import Plans, StubResolver, VerifyGateway, Worker, commander
from test_worker import WorkerRepo

# --- the exact payload the old implementation returned -----------------------

OLD_EMPTY_SUCCESS = {"status": "complete", "fields": {}, "needs_review": []}

MARKER = {"code": NO_USABLE_RESULT_CODE}


def ref(claim_id: str, *, field: str = "answer", value="42", supported: bool = True,
        task_id: str = "a") -> EvidenceReference:
    return EvidenceReference(claim_id=claim_id, source_id=f"s-{claim_id}", run_id="run-1",
                             task_id=task_id, field=field, value=value, confidence=0.9,
                             supported=supported)


def verdict(claim_id: str, kind: str) -> VerificationVerdict:
    return VerificationVerdict(claim_id=claim_id, verdict=kind, reason="source evidence")


def no_evidence_engine(*, evidence_loader=lambda _: [], **kwargs) -> SwarmV2Engine:
    """The real engine over a no-tool, no-evidence plan: tasks all succeed."""
    planned = task("a", "a")
    planned["tools"] = []
    planned["evidence"] = {"minimum_sources": 0, "required_fields": [], "min_confidence": 0.0}
    planned["completion"]["evidence_satisfied"] = False
    client = Plans(plan([planned]), [{"decision": "FINISH", "plan": None, "reason": "done"}])
    return SwarmV2Engine(
        commander=commander(client),
        executor=BoundedTaskExecutor(worker_factory=lambda: Worker([]), max_active_workers=1),
        verifier=Verifier(gateway=VerifyGateway(), model="fake", resolver=StubResolver()),
        evidence_loader=evidence_loader, **kwargs)


def run_engine(engine: SwarmV2Engine, **extra) -> dict:
    return engine.run({"id": "run-1", "input": {"objective": "o", "commander_model": "fake"},
                       **extra})


# --- 1 / regression: an empty result can never be full success ---------------

def test_regression_empty_builder_output_is_never_the_old_empty_success():
    """Fails against the pre-R1 implementation, which returned exactly
    {"status": "complete", "fields": {}, "needs_review": []}."""
    result = FinalBuilder().build([], [])
    assert result != OLD_EMPTY_SUCCESS
    assert result["status"] == "partial_success"
    assert result["result_kind"] == "no_usable_result"
    assert result["fields"] == {}
    assert result["needs_review"] == [MARKER]


def test_builder_with_no_evidence_and_no_verdicts_cannot_return_full_success():
    result = FinalBuilder().build([], [])
    assert (result["status"], result["result_kind"]) != ("complete", "usable_result")
    assert result["status"] != "complete"


# --- 2: successful execution, nothing verified -------------------------------

def test_successful_tasks_with_no_verified_fields_are_no_usable_result():
    result = run_engine(no_evidence_engine())
    assert result["status"] == "partial_success"
    assert result["result_kind"] == "no_usable_result"
    assert result["fields"] == {}
    assert result["needs_review"] == [MARKER]
    assert [item for item in result["needs_review"]
            if item.get("code") == NO_USABLE_RESULT_CODE] == [MARKER]


def test_the_empty_result_marker_is_added_exactly_once():
    already_marked = finalize_product_outcome(fields={}, verdict_review=[MARKER])
    assert already_marked["needs_review"] == [MARKER]
    assert already_marked["result_kind"] == "no_usable_result"


def test_the_empty_result_marker_is_static_and_carries_no_free_text():
    result = FinalBuilder().build([], [])
    marker = result["needs_review"][0]
    assert marker == {"code": NO_USABLE_RESULT_CODE}
    assert set(marker) == {"code"}
    assert marker["code"].isupper()


# --- 3 / 4: rejected and needs_review evidence is not a useful result ---------

def test_only_rejected_verdicts_is_not_a_useful_result():
    result = FinalBuilder().build([ref("c1"), ref("c2")],
                                  [verdict("c1", "rejected"), verdict("c2", "rejected")])
    assert result["fields"] == {}
    assert result["status"] == "partial_success"
    assert result["result_kind"] == "no_usable_result"
    assert MARKER in result["needs_review"]


def test_only_needs_review_verdicts_is_no_usable_result():
    result = FinalBuilder().build([ref("c1")], [verdict("c1", "needs_review")])
    assert result["fields"] == {}
    assert result["status"] == "partial_success"
    assert result["result_kind"] == "no_usable_result"
    assert [item["provenance"]["claim_id"] for item in result["needs_review"]
            if "provenance" in item] == ["c1"]
    assert result["needs_review"][-1] == MARKER


def test_a_verified_but_unsupported_claim_is_not_a_useful_result():
    result = FinalBuilder().build([ref("c1", supported=False)], [verdict("c1", "verified")])
    assert result["fields"] == {}
    assert result["result_kind"] == "no_usable_result"


# --- 5: an empty value collection is not usefulness --------------------------

@pytest.mark.parametrize("fields", [{}, {"answer": []}, {"answer": [], "price": []}])
def test_a_field_key_with_an_empty_collection_is_not_usable(fields):
    result = finalize_product_outcome(fields=fields)
    assert result["result_kind"] == "no_usable_result"
    assert result["status"] == "partial_success"


def test_a_field_key_with_one_entry_is_usable():
    result = finalize_product_outcome(fields={"answer": [{"value": 1}]})
    assert result["result_kind"] == "usable_result"
    assert result["status"] == "complete"


# --- 6 / 7 / 8: verified fields, with and without outstanding items -----------

def test_one_verified_field_with_no_issues_is_complete_and_usable():
    result = FinalBuilder().build([ref("c1")], [verdict("c1", "verified")])
    assert result["status"] == "complete"
    assert result["result_kind"] == "usable_result"
    assert [item["value"] for item in result["fields"]["answer"]] == ["42"]
    assert result["needs_review"] == []


def test_one_verified_field_plus_a_review_verdict_is_partial():
    result = FinalBuilder().build([ref("c1"), ref("c2", value="43")],
                                  [verdict("c1", "verified"), verdict("c2", "needs_review")])
    assert result["status"] == "partial_success"
    assert result["result_kind"] == "partial_result"
    assert len(result["fields"]["answer"]) == 1
    assert MARKER not in result["needs_review"]


@pytest.mark.parametrize("issue", [
    {"task_failures": [{"task_id": "b", "code": "TASK_FAILED"}]},
    {"coverage_gaps": [{"task_id": "b", "code": "EVIDENCE_REQUIREMENTS_UNMET"}]},
    {"conflict_claim_ids": ["c1"]},
])
def test_one_verified_field_plus_an_unresolved_item_stays_partial(issue):
    result = FinalBuilder().build([ref("c1")], [verdict("c1", "verified")], **issue)
    assert result["status"] == "partial_success"
    assert result["result_kind"] == "partial_result"
    assert result["fields"]["answer"]
    assert MARKER not in result["needs_review"]


def test_a_rejected_verdict_alongside_a_verified_field_stays_partial():
    """A rejected verdict produces no review entry of its own; it must still
    keep the run partial rather than silently disappearing."""
    result = FinalBuilder().build([ref("c1"), ref("c2", value="43")],
                                  [verdict("c1", "verified"), verdict("c2", "rejected")])
    assert result["status"] == "partial_success"
    assert result["result_kind"] == "partial_result"


# --- 9: not_found is never inferred ------------------------------------------

def test_empty_output_is_never_inferred_as_not_found():
    assert FinalBuilder().build([], [])["result_kind"] != "not_found"
    assert finalize_product_outcome(fields={})["result_kind"] == "no_usable_result"
    assert finalize_product_outcome(fields={"answer": []},
                                    verdict_review=[])["result_kind"] == "no_usable_result"
    assert run_engine(no_evidence_engine())["result_kind"] != "not_found"


def test_not_found_requires_a_typed_trusted_negative_signal():
    signal = TrustedNegativeResult(code="TRUSTED_SOURCE_NO_MATCH", source_id="s1", task_id="a")
    decided = decide_outcome(usable_fields=False, has_blocking_items=False,
                             trusted_negative=signal)
    assert (decided.status, decided.result_kind) == ("complete", "not_found")
    # Unresolved items mean the negative was never established.
    assert decide_outcome(usable_fields=False, has_blocking_items=True,
                          trusted_negative=signal).result_kind == "no_usable_result"
    # An untyped lookalike -- exactly what model output or arbitrary durable
    # metadata could supply -- fails closed instead of declaring not_found.
    for untyped in ({"code": "TRUSTED_SOURCE_NO_MATCH"}, "TRUSTED_SOURCE_NO_MATCH",
                    True, 1, ["TRUSTED_SOURCE_NO_MATCH"]):
        with pytest.raises(ProductOutcomeError):
            decide_outcome(usable_fields=False, has_blocking_items=False,
                           trusted_negative=untyped)
        with pytest.raises(ProductOutcomeError):
            finalize_product_outcome(fields={}, trusted_negative=untyped)
    with pytest.raises(Exception):
        TrustedNegativeResult(code="ANYTHING_ELSE", source_id="s1", task_id="a")


def test_no_producer_of_a_trusted_negative_signal_exists_yet():
    """R1 defines the boundary; it never fabricates a confirmed negative.

    The Verifier, the workers and the tool registry cannot yet return a typed
    "no match", so nothing in backend/ constructs the signal.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    definition = root / "backend" / "engines" / "swarm_v2" / "outcome.py"
    producers = [path.relative_to(root) for path in (root / "backend").rglob("*.py")
                 if path != definition and "TrustedNegativeResult(" in path.read_text()]
    assert producers == [], producers


def test_the_engine_never_supplies_a_trusted_negative_signal():
    """`not_found` stays unreachable in production until a real tool can
    produce the typed signal: the engine hands the builder facts only."""
    seen = []

    class RecordingBuilder(FinalBuilder):
        def build(self, evidence, verdicts, **kwargs):
            seen.append(kwargs)
            return FinalBuilder.build(self, evidence, verdicts, **kwargs)

    result = run_engine(no_evidence_engine(builder=RecordingBuilder()))
    assert seen and all(item.get("trusted_negative") is None for item in seen)
    assert set(seen[-1]) == {"task_failures", "coverage_gaps", "conflict_claim_ids"}
    assert result["result_kind"] == "no_usable_result"


# --- 10: nothing model-controlled can choose the outcome ---------------------

def test_model_written_task_output_cannot_force_complete_or_set_result_kind():
    """A worker model returns whatever the plan's schema allows. None of it is
    a verified product field, and none of it may reach the classification."""
    class ClaimingWorker:
        def execute(self, spec, dependencies):
            return TaskResult(spec.task_id, "completed", {
                "answer": "done", "status": "complete", "result_kind": "usable_result",
                "needs_review": [], "fields": {"answer": [{"value": "invented"}]}})

    planned = task("a", "a")
    planned["tools"] = []
    planned["evidence"] = {"minimum_sources": 0, "required_fields": [], "min_confidence": 0.0}
    planned["completion"]["evidence_satisfied"] = False
    client = Plans(plan([planned]), [{"decision": "FINISH", "plan": None, "reason": "done"}])
    engine = SwarmV2Engine(
        commander=commander(client),
        executor=BoundedTaskExecutor(worker_factory=ClaimingWorker, max_active_workers=1),
        verifier=Verifier(gateway=VerifyGateway(), model="fake", resolver=StubResolver()),
        evidence_loader=lambda _: [])
    result = run_engine(engine)
    assert result["status"] == "partial_success"
    assert result["result_kind"] == "no_usable_result"
    assert result["fields"] == {}


def test_a_commander_plan_without_evidence_requirements_cannot_declare_success():
    """Model-written plan requirements are not sufficient to establish product
    success: the smoke plan asks for no evidence at all and still cannot
    produce `complete`."""
    result = run_engine(no_evidence_engine())
    assert result["status"] != "complete"


def test_the_result_kind_vocabulary_is_statically_allowlisted():
    assert RESULT_KINDS == {"usable_result", "partial_result", "no_usable_result", "not_found"}
    assert {status for status, _ in ALLOWED_OUTCOMES} == {"complete", "partial_success"}
    assert ("complete", "no_usable_result") not in ALLOWED_OUTCOMES
    assert ("complete", "partial_result") not in ALLOWED_OUTCOMES


@pytest.mark.parametrize("payload", [
    {"status": "complete", "result_kind": "no_usable_result", "fields": {}, "needs_review": []},
    {"status": "complete", "result_kind": "usable_result", "fields": {}, "needs_review": []},
    {"status": "complete", "fields": {"a": [1]}, "needs_review": []},
    {"status": "success", "result_kind": "usable_result", "fields": {"a": [1]}, "needs_review": []},
    {"status": "complete", "result_kind": "totally_fine", "fields": {"a": [1]}, "needs_review": []},
    {"status": "complete", "result_kind": "usable_result", "fields": {"a": [1]}},
    {"result_kind": "usable_result", "fields": {"a": [1]}, "needs_review": []},
    "complete",
])
def test_a_contradictory_or_unknown_outcome_is_refused_not_trusted(payload):
    with pytest.raises(ProductOutcomeError):
        validate_product_outcome(payload)


def test_a_self_consistent_outcome_maps_to_its_durable_run_status():
    assert durable_run_status(FinalBuilder().build([], [])) == "partial_success"
    assert durable_run_status(FinalBuilder().build(
        [ref("c1")], [verdict("c1", "verified")])) == "completed"


# --- 11: the outer worker maps, it does not guess ----------------------------

class SwarmWorkerRepo(WorkerRepo):
    """The V1 worker fixture, routed to swarm_v2."""

    def get_project(self, project_id):
        return {"id": project_id, "workflow_key": "swarm_v2"}


def worker_result(repo, result):
    class FakeEngine:
        workflow_key = "swarm_v2"

        def run(self, run):
            return result

    return worker_main.execute_run(repo.run_id, repo, FakeEngine())


def test_worker_maps_no_usable_result_to_partial_success_and_never_run_completed():
    repo = SwarmWorkerRepo()
    assert worker_result(repo, FinalBuilder().build([], [])) == 0
    assert repo.completed is None
    assert repo.partial is not None
    assert repo.partial[1]["result_kind"] == "no_usable_result"
    types = [event[1] for event in repo.events]
    assert "run_partial_success" in types
    assert "run_completed" not in types
    assert "run_failed" not in types


def test_worker_maps_a_usable_result_to_completed():
    repo = SwarmWorkerRepo()
    result = FinalBuilder().build([ref("c1")], [verdict("c1", "verified")])
    assert worker_result(repo, result) == 0
    assert repo.completed is not None
    assert repo.completed[1]["result_kind"] == "usable_result"
    assert [event[1] for event in repo.events][-1] == "run_completed"


def test_worker_refuses_a_swarm_result_that_only_looks_useful():
    """The old mapping completed ANY non-failed object carrying a generic
    `result` key. That path is gone for Swarm V2."""
    repo = SwarmWorkerRepo()
    assert worker_result(repo, {"status": "complete", "result": {"models": []}}) == 0
    assert repo.completed is None
    assert repo.partial is None
    assert repo.failed[1] == "SWARM_V2_OUTCOME_INVALID"
    types = [event[1] for event in repo.events]
    assert "run_completed" not in types and "run_partial_success" not in types


def test_worker_refuses_to_finalize_partial_success_it_cannot_express():
    """A repository that cannot record partial_success must never have an
    unusable result written to it as `completed`."""
    class NoTransitionRepo(SwarmWorkerRepo):
        """A repository build without the partial_success transition at all."""

        def __getattribute__(self, name):
            if name == "transition_run":
                raise AttributeError(name)
            return super().__getattribute__(name)

    repo = NoTransitionRepo()
    with pytest.raises(AppError) as failure:
        worker_result(repo, FinalBuilder().build([], []))
    assert failure.value.code == "RUN_FINALIZATION_UNAVAILABLE"
    assert repo.completed is None and repo.partial is None


def test_worker_never_leaks_the_offending_payload_when_it_refuses():
    repo = SwarmWorkerRepo()
    sentinel = "provider secret sentinel"
    worker_result(repo, {"status": "complete", "result": {"raw": sentinel}})
    assert sentinel not in json.dumps([str(event) for event in repo.events])
    assert sentinel not in json.dumps(list(repo.failed[1:]))


# --- 12 / 13 / 14: V1, cancellation and terminal states are untouched --------

def test_v1_completion_behavior_is_unchanged():
    for result in ({"status": "success", "result": {"models": [1]}},
                   {"status": "complete", "result": {"models": [1]}},
                   # the generic non-failed `result` fallback V1 relies on
                   {"status": "partial", "result": {"models": [1]}}):
        repo = WorkerRepo()

        class V1Engine:
            workflow_key = "vehicle_catalog_v1"

            def run(self, run):
                return result

        assert worker_main.execute_run(repo.run_id, repo, V1Engine()) == 0
        assert repo.completed is not None and repo.completed[1] == result
        assert [event[1] for event in repo.events][-1] == "run_completed"


def test_v1_partial_success_behavior_is_unchanged():
    repo = WorkerRepo()

    class V1Engine:
        workflow_key = "vehicle_catalog_v1"

        def run(self, run):
            return {"status": "partial_success", "result": {"models": []}}

    assert worker_main.execute_run(repo.run_id, repo, V1Engine()) == 0
    assert repo.partial[1]["status"] == "partial_success"
    assert "result_kind" not in repo.partial[1]
    assert [event[1] for event in repo.events][-1] == "run_partial_success"


def test_cancellation_remains_cancellation():
    from backend.runtime import CancellationRequested

    repo = SwarmWorkerRepo()
    transitions = []
    original = repo.transition_run

    def record(run_id, status, **kwargs):
        transitions.append(status)
        return original(run_id, status, **kwargs)

    repo.transition_run = record

    class CancellingEngine:
        workflow_key = "swarm_v2"

        def run(self, run):
            raise CancellationRequested()

    assert worker_main.execute_run(repo.run_id, repo, CancellingEngine()) == 0
    assert transitions[-1] == "cancelled"
    assert [event[1] for event in repo.events][-1] == "run_cancelled"
    assert repo.completed is None and repo.partial is None


@pytest.mark.parametrize("terminal,code", [
    ("timed_out", "RUN_DURATION_LIMIT_REACHED"),
    ("budget_exhausted", "TOTAL_TOKEN_LIMIT_REACHED"),
])
def test_timeout_and_budget_exhaustion_keep_their_terminal_statuses(terminal, code):
    from backend.budget import BudgetExceeded

    repo = SwarmWorkerRepo()
    transitions = []
    original = repo.transition_run
    repo.transition_run = lambda run_id, status, **kw: (
        transitions.append(status), original(run_id, status, **kw))[1]

    class StoppedEngine:
        workflow_key = "swarm_v2"

        def run(self, run):
            raise BudgetExceeded(code, "limit reached", event_type="budget_exhausted",
                                 terminal_status=terminal)

    assert worker_main.execute_run(repo.run_id, repo, StoppedEngine()) == 0
    assert transitions[-1] == terminal
    assert repo.completed is None and repo.partial is None


def test_a_genuine_execution_failure_is_not_downgraded_to_partial_success():
    repo = SwarmWorkerRepo()

    class BrokenEngine:
        workflow_key = "swarm_v2"

        def run(self, run):
            raise RuntimeError("provider secret sentinel")

    assert worker_main.execute_run(repo.run_id, repo, BrokenEngine()) == 0
    assert repo.failed[1] == "SWARM_V2_EXECUTION_FAILED"
    assert repo.partial is None and repo.completed is None


def test_an_infrastructure_failure_still_escapes_rather_than_finalizing():
    repo = SwarmWorkerRepo()

    class BrokenEngine:
        workflow_key = "swarm_v2"

        def run(self, run):
            raise AppError("REPOSITORY_ERROR", "guarded persistence operation failed", 502)

    with pytest.raises(AppError):
        worker_main.execute_run(repo.run_id, repo, BrokenEngine())
    assert repo.partial is None and repo.completed is None and repo.failed is None


# --- 15 / 16: resume determinism and unchanged accounting --------------------

def test_resume_from_a_compatible_checkpoint_is_deterministic_and_marks_once():
    initial = plan([task("done", "alpha", ), task("next", "beta", dependencies=["done"])],
                   contexts={"next": ["done"]})
    for item in initial["graph"]["tasks"]:
        item["tools"] = []
        item["evidence"] = {"minimum_sources": 0, "required_fields": [], "min_confidence": 0.0}
        item["completion"]["evidence_satisfied"] = False
    state = SwarmState(run_id="run-1", objective="resume", approved_plan=initial,
                       completed_task_ids=["done"], task_outputs={"done": {"answer": "done"}})
    outcomes, checkpoints = [], []
    for _ in range(2):
        client = Plans(initial, [{"decision": "FINISH", "plan": None, "reason": "done"}])
        engine = SwarmV2Engine(
            commander=commander(client),
            executor=BoundedTaskExecutor(worker_factory=lambda: Worker([]), max_active_workers=2),
            verifier=Verifier(gateway=VerifyGateway(), model="fake", resolver=StubResolver()),
            evidence_loader=lambda _: [],
            checkpoint_sink=lambda phase, value: checkpoints.append(deepcopy(value)))
        outcomes.append(engine.run({
            "id": "run-1", "input": {"commander_model": "fake"},
            "checkpoint": {"artifacts": {"swarm_state": state.model_dump(mode="json")}}}))
    assert outcomes[0] == outcomes[1]
    assert outcomes[0]["result_kind"] == "no_usable_result"
    assert outcomes[0]["needs_review"] == [MARKER]
    # The marker lives in the product output only; it never accumulates in the
    # durable checkpoint, so a third resume would produce the same one marker.
    assert all(NO_USABLE_RESULT_CODE not in json.dumps(item) for item in checkpoints)


def test_the_outcome_contract_adds_no_model_call_and_no_usage():
    """Classification is pure application code: it never asks a model."""
    calls = []

    class CountingGateway(VerifyGateway):
        def call(self, **kwargs):
            calls.append(kwargs)
            return VerifyGateway.call(self, **kwargs)

    snapshots = []
    engine = no_evidence_engine(usage_snapshot=lambda: {"model_calls": len(calls)},
                                checkpoint_sink=lambda phase, value: snapshots.append(
                                    value["token_usage"]))
    result = run_engine(engine)
    assert result["result_kind"] == "no_usable_result"
    assert calls == []          # no evidence -> no verifier batch, as before
    assert snapshots[-1] == {"model_calls": 0}


# --- 17: safe serialization ---------------------------------------------------

def test_the_final_output_is_safely_serializable_and_carries_no_raw_material():
    from backend.engines.swarm_v2.evidence import safe_durable_value

    result = FinalBuilder().build([ref("c1"), ref("c2", value="43")],
                                  [verdict("c1", "verified"), verdict("c2", "needs_review")],
                                  task_failures=[{"task_id": "b", "code": "TASK_FAILED"}])
    encoded = json.dumps(safe_durable_value(result), sort_keys=True)
    for forbidden in ("chain_of_thought", "prompt", "messages", "Traceback",
                      "fragment_text", "api_key", "system"):
        assert forbidden not in encoded
    assert json.loads(encoded) == result


def test_the_no_usable_result_marker_survives_safe_serialization():
    from backend.engines.swarm_v2.evidence import safe_durable_value

    result = safe_durable_value(FinalBuilder().build([], []))
    assert result["needs_review"] == [MARKER]
    assert json.loads(json.dumps(result))["result_kind"] == "no_usable_result"


# --- 18: the historical smoke's product claim -------------------------------

def test_the_historical_no_tool_smoke_no_longer_claims_an_empty_success():
    """The offline smoke keeps every infrastructure assertion; its product
    expectation is now the truthful one. Pinning it here means the smoke
    cannot quietly drift back to describing an empty run as a success."""
    import pathlib

    source = (pathlib.Path(__file__).resolve().parent
              / "test_swarm_v2_smoke_offline.py").read_text()
    assert 'assert run["output"]["status"] == "complete"' not in source
    assert 'assert run["output"]["result_kind"] == "no_usable_result"' in source
    assert 'assert run["usage"]["model_calls"] == 4' in source
    assert 'assert checkpoint.get("engine_version") == "swarm_v2.1"' in source

"""PR-T: a register ambiguity is an outcome, not a failure.

Production run 3c72bfbc planned nine Government-register tasks and completed
all nine (40 claims, $0.227). Task t04 asked `resolve_variant` about two
duplicate-signature candidates -- TOYOTA 4RUNNER 2026 LIMITED, official code
TZNA55L-GKZSZA -- and the register truthfully answered that two identical rows
match (37350 / 37439): `resolved=false, ambiguous=true, match_count=2`. An
ambiguous answer quotes no single row, so it produced no evidence; t04 had
`allow_partial=false`; the Commander's replan added no task; and the engine
raised a bare `ValueError("completion criteria not satisfied")`, which the
worker reported as the generic SWARM_V2_EXECUTION_FAILED. The whole batch was
lost and verification never ran.

These tests replay that run offline through the REAL engine, executor,
GenericWorker, ToolRegistry (validating against the real `resolve_variant`
schemas), Commander + PlanValidator, Verifier and FinalBuilder. The worker
completes tasks through a deterministic output strategy, so no model and no
provider is called for task output.
"""

from __future__ import annotations

import dataclasses
import hashlib
from uuid import uuid4

import pytest

import backend.worker.main as worker_main
from backend.engines.swarm_v2 import (BoundedTaskExecutor, Commander, CommanderModelResolver,
                                      EvidenceReference, FinalBuilder, GenericWorker,
                                      PlanLimits, PlanValidator, SwarmV2Engine, TaskResult,
                                      Verifier, validate_product_outcome)
from backend.engines.swarm_v2.failures import (EXECUTION_FAILURE_CODES,
                                               EXECUTION_FAILURE_MESSAGES,
                                               SwarmExecutionFailure)
from backend.engines.swarm_v2.resolution import (CANDIDATE_GAP_CODES, SOFT_GAP_CODES,
                                                 candidate_outcome)
from backend.engines.swarm_v2.state import SwarmState
from backend.engines.swarm_v2.validation import SOURCE_FIRST_TOOL_POLICY, provider_plan_policy
from backend.product_outcome import derive_product_outcome
from backend.tools import ToolContext, ToolError, ToolMode, ToolRegistry
from backend.tools.government_vehicle import (GOVERNMENT_TOOL_NAME, GOVERNMENT_TOOL_SCOPE,
                                              OPERATIONS)
from test_swarm_v2_outcome_contract import SwarmWorkerRepo
from test_swarm_v2_stage1_e2e import Plans, StubResolver, VerifyGateway

AMBIGUOUS_CODE = "TZNA55L-GKZSZA"
AMBIGUOUS_ROWS = ("37350", "37439")

#: The eight candidates run 3c72bfbc resolved, one row each.
RESOLVED = {
    "t01": ("COROLLA", "ZWE211L-DEXNBW"), "t02": ("RAV4", "AXAH54L-ANXGBW"),
    "t03": ("C-HR", "ZYX11L-GHXNBW"), "t05": ("YARIS", "MXPH10L-AHXNBW"),
    "t06": ("CAMRY", "AXVH71L-AEXNBW"), "t07": ("HIGHLANDER", "AXUH78L-AWXGBW"),
    "t08": ("LAND CRUISER", "GDJ250L-GKTEZW"), "t09": ("BZ4X", "XEAM10L-CWYLBW"),
}

PROVENANCE = {"snapshot_key": "wltp-snapshot-1", "resource_id": "wltp-resource",
              "upstream_version": "2026-09-01", "upstream_version_kind": "last_modified",
              "content_sha256": "a" * 64, "normalization_contract": "gov-normalize/1",
              "normalization_issue_count": 0}

REQUEST_VERIFICATION = {"decision": "REQUEST_VERIFICATION", "plan": None,
                        "reason": "the register answered every candidate"}


def variant(record_id: str, model: str, code: str) -> dict:
    return {"candidate_id": f"cand-{record_id}", "status": "candidate",
            "manufacturer": "TOYOTA", "commercial_model": model, "model_year_start": 2026,
            "model_year_end": 2026, "official_model_code": code,
            "upstream_record_id": record_id, "resource_id": "wltp-resource"}


class ReplayRegister:
    """The Government register as run 3c72bfbc read it.

    Registered under the real tool name with the real `resolve_variant`
    operation, so the Registry validates every payload and every result
    against the production schemas before the worker sees it.
    """

    name = GOVERNMENT_TOOL_NAME
    description = "replay of the register reads of run 3c72bfbc"
    mode = ToolMode.READ
    required_scope = GOVERNMENT_TOOL_SCOPE
    operations = {"resolve_variant": OPERATIONS["resolve_variant"]}

    def __init__(self, *, unavailable: frozenset[str] = frozenset()):
        self.calls: list[dict] = []
        self._unavailable = unavailable

    def execute(self, context, operation, payload):
        self.calls.append(dict(payload))
        code = payload["official_model_code"]
        if code in self._unavailable:
            raise ToolError("GOV_QUERY_UNAVAILABLE", "the register could not be read",
                            tool=self.name)
        if code == AMBIGUOUS_CODE:
            return {"resolved": False, "ambiguous": True, "match_count": 2,
                    "variants": [variant(row, "4RUNNER", code) for row in AMBIGUOUS_ROWS],
                    "provenance": dict(PROVENANCE)}
        if code.startswith("MISSING"):
            return {"resolved": False, "ambiguous": False, "match_count": 0,
                    "variants": [], "provenance": dict(PROVENANCE)}
        record_id = str(int(hashlib.sha256(code.encode()).hexdigest()[:8], 16))
        return {"resolved": True, "ambiguous": False, "match_count": 1,
                "variants": [variant(record_id, payload["commercial_model"], code)],
                "source_record": {"upstream_record_id": record_id},
                "provenance": dict(PROVENANCE)}


class EvidenceSink:
    """Stands in for the trusted mapper + Evidence Board: a RESOLVED register
    answer becomes one claim; an ambiguous or empty one records nothing
    (the real mapper returns NO_EVIDENCE for exactly those)."""

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.claims: list[EvidenceReference] = []

    def __call__(self, record):
        if not record.result.get("resolved"):
            return
        row = record.result["variants"][0]
        self.claims.append(EvidenceReference(
            claim_id=f"claim-{record.task_id}-{record.call_id}",
            source_id=f"gov-{row['upstream_record_id']}", run_id=self.run_id,
            task_id=record.task_id, entity=f"vehicle:{row['upstream_record_id']}",
            field="model_code", value=row["official_model_code"], confidence=0.95))

    def loader(self, results):
        done = {key for key, value in results.items() if value.status == "completed"}
        return [claim for claim in self.claims if claim.task_id in done]


class NoModel:
    """Every task completes through the deterministic strategy: a model call is a bug."""

    def call(self, **_kwargs):
        raise AssertionError("no worker model call is expected in this replay")


def misleading_summary(*, task, tool_outputs, dependency_outputs):
    # The worker output claims every candidate resolved. The per-candidate
    # outcome must come from the TOOL result, never from this text.
    return {"summary": "all candidates resolved", "register_ambiguous": False}


def gov_call(call_id: str, model: str, code: str, *, trim: str | None = None) -> dict:
    arguments = {"manufacturer": "TOYOTA", "commercial_model": model, "model_year": 2026,
                 "official_model_code": code}
    if trim:
        arguments["trim"] = trim
    return {"call_id": call_id, "name": GOVERNMENT_TOOL_NAME, "operation": "resolve_variant",
            "arguments": arguments, "dependency_bindings": []}


def gov_task(task_id: str, calls: list[dict], *, allow_partial: bool = False,
             evidence: bool = True) -> dict:
    return {
        "task_id": task_id, "goal": f"resolve the register variant for {task_id}",
        "scope": f"Israeli register, candidate set {task_id}", "dependencies": [],
        "tools": calls,
        "output_schema": {"type": "object",
                          "properties": {"summary": {"type": "string"},
                                         "register_ambiguous": {"type": "boolean"}},
                          "required": ["summary"], "additionalProperties": False},
        "evidence": ({"minimum_sources": 1, "required_fields": ["model_code"], "min_confidence": 0.5}
                     if evidence else
                     {"minimum_sources": 0, "required_fields": [], "min_confidence": 0.5}),
        "priority": 50, "recursion_depth": 0, "estimated_cost_units": 10,
        "completion": {"required_outputs": ["summary"], "evidence_satisfied": evidence,
                       "allow_partial": allow_partial},
    }


def gov_plan(tasks: list[dict], *, max_replans: int = 1) -> dict:
    return {"version": "1", "objective": "verify the Toyota 2026 register candidates",
            "graph": {"tasks": tasks},
            "assignments": [{"task_id": item["task_id"], "worker_role": "register reader",
                             "context_task_ids": []} for item in tasks],
            "max_replans": max_replans,
            "estimated_cost_units": sum(item["estimated_cost_units"] for item in tasks)}


def run_3c72bfbc_plan() -> dict:
    """Nine tasks, every one `allow_partial=false`; t04 verifies the two
    duplicate-signature 4RUNNER LIMITED candidates."""
    tasks = [gov_task(task_id, [gov_call("c1", model, code)])
             for task_id, (model, code) in RESOLVED.items()]
    tasks.insert(3, gov_task("t04", [gov_call("c1", "4RUNNER", AMBIGUOUS_CODE, trim="LIMITED"),
                                     gov_call("c2", "4RUNNER", AMBIGUOUS_CODE, trim="LIMITED")]))
    return gov_plan(tasks)


def build(plan: dict, decisions: list[dict], *, register: ReplayRegister | None = None,
          drop_outcomes: bool = False, events: list | None = None,
          checkpoints: list | None = None):
    run_id = str(uuid4())
    register = register or ReplayRegister()
    tools = ToolRegistry([register])
    sink = EvidenceSink(run_id)
    context = ToolContext(scopes=frozenset({GOVERNMENT_TOOL_SCOPE}))

    def worker():
        real = GenericWorker(gateway=NoModel(), tools=tools, model="fake", tool_context=context,
                             tool_result_sink=sink, task_output_strategy=misleading_summary)
        if not drop_outcomes:
            return real

        class PrePRTWorker:
            """What the worker handed the engine before PR-T: no typed outcomes."""

            def execute(self, spec, dependencies):
                return dataclasses.replace(real.execute(spec, dependencies), resolutions=())

        return PrePRTWorker()

    client = Plans(plan, decisions)
    commander = Commander(client=client, resolver=CommanderModelResolver(("fake",), {"fake"}),
                          validator=PlanValidator(allowed_tools=tools.descriptors(),
                                                  limits=PlanLimits(max_tasks=10,
                                                                    max_tool_calls=30)))
    engine = SwarmV2Engine(
        commander=commander,
        executor=BoundedTaskExecutor(worker_factory=worker, max_active_workers=2),
        verifier=Verifier(gateway=VerifyGateway(), model="fake", resolver=StubResolver()),
        builder=FinalBuilder(), evidence_loader=sink.loader,
        event_sink=(lambda kind, payload: events.append((kind, payload)))
        if events is not None else None,
        checkpoint_sink=(lambda _phase, checkpoint: checkpoints.append(checkpoint))
        if checkpoints is not None else None)
    run = {"id": run_id, "input": {"objective": "o", "commander_model": "fake"}}
    return engine, run, register, sink


# =============================================================================
# 1. the replay: 8 resolved + 1 ambiguous -> partial_success, verified
# =============================================================================

def test_replay_of_run_3c72bfbc_finalizes_partial_success_with_verification():
    events: list = []
    engine, run, register, sink = build(run_3c72bfbc_plan(), [REQUEST_VERIFICATION],
                                        events=events)
    result = engine.run(run)

    # Every planned register read ran exactly once: nothing was discarded.
    assert len(register.calls) == 10
    # Verification RAN, over the eight resolved candidates' evidence.
    kinds = [kind for kind, _ in events]
    assert "verification_completed" in kinds
    assert len(sink.claims) == 8

    assert result["status"] == "partial_success"
    assert result["result_kind"] == "partial_result"
    # One field key, the eight resolved candidates' verified values under it.
    assert {entry["provenance"]["task_id"] for entry in result["fields"]["model_code"]} == set(RESOLVED)

    # The ambiguity is a SOFT coverage gap -- never a task failure, never the
    # hard EVIDENCE_REQUIREMENTS_UNMET that used to fail the run.
    assert result["needs_review"] == [{"task_id": "t04", "code": "CANDIDATE_UNRESOLVED_AMBIGUOUS"}]

    # Per-candidate outcomes, typed from the tool result -- although the
    # worker's own output claimed `register_ambiguous: false`.
    outcomes = result["candidate_outcomes"]
    assert [(item["task_id"], item["call_id"], item["outcome"]) for item in outcomes] == [
        *[(task_id, "c1", "resolved") for task_id in sorted(RESOLVED) if task_id < "t04"],
        ("t04", "c1", "unresolved_ambiguous"), ("t04", "c2", "unresolved_ambiguous"),
        *[(task_id, "c1", "resolved") for task_id in sorted(RESOLVED) if task_id > "t04"],
    ]
    ambiguous = [item for item in outcomes if item["task_id"] == "t04"]
    for item in ambiguous:
        assert item["match_count"] == 2
        assert item["record_ids"] == list(AMBIGUOUS_ROWS)
        assert item["candidate"] == {"manufacturer": "TOYOTA", "commercial_model": "4RUNNER",
                                     "model_year": 2026, "trim": "LIMITED",
                                     "official_model_code": AMBIGUOUS_CODE}

    # The payload is exactly one the canonical contract accepts.
    assert validate_product_outcome(result).status == "partial_success"
    outcome = derive_product_outcome("swarm_v2", result)
    assert outcome.semantic_status == "partial" and outcome.coverage.produced == 8


def test_the_replay_through_the_worker_is_run_partial_success_not_run_failed():
    engine, run, _register, _sink = build(run_3c72bfbc_plan(), [REQUEST_VERIFICATION])
    result = engine.run(run)
    repo = SwarmWorkerRepo()

    class Replayed:
        workflow_key = "swarm_v2"

        def run(self, _run):
            return result

    assert worker_main.execute_run(repo.run_id, repo, Replayed()) == 0
    assert repo.failed is None and repo.partial is not None
    types = [event[1] for event in repo.events]
    assert types[-1] == "run_partial_success" and "run_failed" not in types


def test_the_typed_outcomes_survive_a_resume_without_re_running_the_tool():
    checkpoints: list = []
    engine, run, _register, _sink = build(run_3c72bfbc_plan(), [REQUEST_VERIFICATION],
                                          checkpoints=checkpoints)
    first = engine.run(run)
    state = checkpoints[-1]["artifacts"]["swarm_state"]
    assert [item["outcome"] for item in state["task_resolutions"]["t04"]] == [
        "unresolved_ambiguous", "unresolved_ambiguous"]
    SwarmState.resume(state, run_id=run["id"])  # a PR-T checkpoint is a valid checkpoint

    # Resume from the checkpoint saved right after the LAST task completed:
    # nothing re-executes, and the ambiguity is still an answer.
    last_task = next(item for item in reversed(checkpoints)
                     if len(item["completed_tasks"]) == 9
                     and not item["artifacts"]["swarm_state"]["verifier_state"])
    register = ReplayRegister()
    resumed, _run, _reg, sink = build(run_3c72bfbc_plan(), [REQUEST_VERIFICATION],
                                      register=register)
    sink.run_id = run["id"]
    sink.claims = [EvidenceReference.model_validate(item)
                   for item in last_task["artifacts"]["swarm_state"]["evidence_references"]]
    again = resumed.run({**run, "checkpoint": last_task})
    assert register.calls == []
    assert again["needs_review"] == first["needs_review"]
    assert again["candidate_outcomes"] == first["candidate_outcomes"]


# =============================================================================
# 2. finalization never discards completed work
# =============================================================================

def test_a_hard_gap_beside_accepted_evidence_is_partial_success_not_a_failure():
    """Even WITHOUT typed outcomes (the pre-PR-T worker), t04's untyped
    evidence gap no longer throws away the eight resolved candidates."""
    events: list = []
    engine, run, _register, _sink = build(run_3c72bfbc_plan(), [REQUEST_VERIFICATION],
                                          drop_outcomes=True, events=events)
    result = engine.run(run)
    assert result["status"] == "partial_success"
    assert result["result_kind"] == "partial_result"
    assert result["needs_review"] == [{"task_id": "t04", "code": "EVIDENCE_REQUIREMENTS_UNMET"}]
    assert "candidate_outcomes" not in result
    assert "verification_completed" in [kind for kind, _ in events]


def test_a_failed_required_task_beside_accepted_evidence_is_partial_success():
    plan = gov_plan([gov_task("t01", [gov_call("c1", "COROLLA", "ZWE211L-DEXNBW")]),
                     gov_task("t02", [gov_call("c1", "RAV4", "BROKEN-1")])])
    engine, run, _register, _sink = build(
        plan, [REQUEST_VERIFICATION], register=ReplayRegister(unavailable=frozenset({"BROKEN-1"})))
    result = engine.run(run)
    assert result["status"] == "partial_success"
    assert result["needs_review"] == [{"task_id": "t02", "code": "GOV_QUERY_UNAVAILABLE"}]
    assert [entry["provenance"]["task_id"] for entry in result["fields"]["model_code"]] == ["t01"]


def test_an_unresolved_candidate_is_a_gap_even_without_an_evidence_requirement():
    """`complete` means nothing is outstanding. An ambiguous candidate is."""
    plan = gov_plan([gov_task("t01", [gov_call("c1", "COROLLA", "ZWE211L-DEXNBW")]),
                     gov_task("t04", [gov_call("c1", "4RUNNER", AMBIGUOUS_CODE)],
                              evidence=False)])
    engine, run, _register, _sink = build(plan, [REQUEST_VERIFICATION])
    result = engine.run(run)
    assert result["status"] == "partial_success"
    assert result["needs_review"] == [{"task_id": "t04", "code": "CANDIDATE_UNRESOLVED_AMBIGUOUS"}]


def test_a_not_found_candidate_is_typed_and_soft():
    plan = gov_plan([gov_task("t01", [gov_call("c1", "COROLLA", "ZWE211L-DEXNBW")]),
                     gov_task("t02", [gov_call("c1", "SUPRA", "MISSING-1")])])
    engine, run, _register, _sink = build(plan, [REQUEST_VERIFICATION])
    result = engine.run(run)
    assert result["status"] == "partial_success"
    assert result["needs_review"] == [{"task_id": "t02", "code": "CANDIDATE_UNRESOLVED_NOT_FOUND"}]
    missing = [item for item in result["candidate_outcomes"] if item["task_id"] == "t02"]
    assert missing == [{"task_id": "t02", "call_id": "c1", "outcome": "unresolved_not_found",
                        "match_count": 0, "record_ids": [],
                        "candidate": {"manufacturer": "TOYOTA", "commercial_model": "SUPRA",
                                      "model_year": 2026, "official_model_code": "MISSING-1"}}]


def test_only_ambiguous_candidates_is_an_answer_not_a_failure():
    plan = gov_plan([gov_task("t04", [gov_call("c1", "4RUNNER", AMBIGUOUS_CODE)])])
    engine, run, _register, _sink = build(plan, [REQUEST_VERIFICATION])
    result = engine.run(run)
    assert (result["status"], result["result_kind"]) == ("partial_success", "no_usable_result")
    assert result["candidate_outcomes"][0]["outcome"] == "unresolved_ambiguous"


# =============================================================================
# 3. nothing usable -> failed, with a static code
# =============================================================================

def test_nothing_usable_and_a_failed_required_task_fails_with_a_static_code():
    plan = gov_plan([gov_task("t01", [gov_call("c1", "COROLLA", "BROKEN-1")])])
    engine, run, _register, _sink = build(
        plan, [REQUEST_VERIFICATION], register=ReplayRegister(unavailable=frozenset({"BROKEN-1"})))
    with pytest.raises(SwarmExecutionFailure) as caught:
        engine.run(run)
    assert caught.value.code == "SWARM_V2_REQUIRED_TASK_FAILED"


def test_nothing_usable_and_a_hard_gap_fails_completion_criteria_unmet():
    plan = gov_plan([gov_task("t04", [gov_call("c1", "4RUNNER", AMBIGUOUS_CODE)])])
    engine, run, _register, _sink = build(plan, [REQUEST_VERIFICATION], drop_outcomes=True)
    with pytest.raises(SwarmExecutionFailure) as caught:
        engine.run(run)
    assert caught.value.code == "SWARM_V2_COMPLETION_CRITERIA_UNMET"
    # Still a ValueError with its historical text, for every existing caller.
    assert isinstance(caught.value, ValueError)
    assert str(caught.value) == "completion criteria not satisfied"


# =============================================================================
# 4. every replan refusal has its own static code
#
# PR-X S3: with evidence in hand the refused PROPOSAL is rejected -- recorded
# in the checkpoint with its static code -- and the run finalizes with its
# work as partial_success, carrying COMMANDER_REPLAN_REJECTED. With nothing
# usable it still fails with that same code (tests/test_swarm_v2_degraded_steps.py).
# =============================================================================


def rejected_with(plan: dict, decision: dict) -> tuple[dict, list[dict]]:
    checkpoints: list = []
    engine, run, _register, _sink = build(plan, [decision], checkpoints=checkpoints)
    result = engine.run(run)
    assert result["status"] == "partial_success"
    assert {"task_id": "commander_replan", "code": "COMMANDER_REPLAN_REJECTED"} in \
        result["needs_review"]
    return result, checkpoints[-1]["artifacts"]["swarm_state"]["replans"]

def two_task_plan(**kwargs) -> dict:
    return gov_plan([gov_task("t01", [gov_call("c1", "COROLLA", "ZWE211L-DEXNBW")]),
                     gov_task("t04", [gov_call("c1", "4RUNNER", AMBIGUOUS_CODE)])], **kwargs)


def add_tasks(plan: dict) -> dict:
    return {"decision": "ADD_TASKS", "plan": plan, "reason": "add a targeted task"}


def test_replan_without_any_gap_is_swarm_v2_replan_requires_gap():
    plan = gov_plan([gov_task("t01", [gov_call("c1", "COROLLA", "ZWE211L-DEXNBW")])])
    extended = gov_plan([*plan["graph"]["tasks"],
                         gov_task("t02", [gov_call("c1", "RAV4", "AXAH54L-ANXGBW")])])
    _result, replans = rejected_with(plan, add_tasks(extended))
    assert replans == [{"decision": "REJECTED", "code": "SWARM_V2_REPLAN_REQUIRES_GAP",
                        "graph_revision": 1}]


def test_replan_past_the_plan_allowance_is_swarm_v2_max_replans_exceeded():
    plan = two_task_plan(max_replans=0)
    extended = gov_plan([*plan["graph"]["tasks"],
                         gov_task("t05", [gov_call("c1", "YARIS", "MXPH10L-AHXNBW")])],
                        max_replans=0)
    _result, replans = rejected_with(plan, add_tasks(extended))
    assert replans == [{"decision": "REJECTED", "code": "SWARM_V2_MAX_REPLANS_EXCEEDED",
                        "graph_revision": 1}]


def test_replan_that_rewrites_a_completed_task_is_swarm_v2_replan_rewrites_completed():
    plan = two_task_plan()
    rewritten = two_task_plan()
    rewritten["graph"]["tasks"][0]["goal"] = "a different goal for a completed task"
    _result, replans = rejected_with(plan, add_tasks(rewritten))
    assert replans == [{"decision": "REJECTED", "code": "SWARM_V2_REPLAN_REWRITES_COMPLETED",
                        "graph_revision": 1}]


def test_the_failure_vocabulary_is_closed_and_keeps_the_historical_messages():
    assert EXECUTION_FAILURE_CODES == {
        "SWARM_V2_COMPLETION_CRITERIA_UNMET", "SWARM_V2_REQUIRED_TASK_FAILED",
        "SWARM_V2_REPLAN_REQUIRES_GAP", "SWARM_V2_MAX_REPLANS_EXCEEDED",
        "SWARM_V2_REPLAN_REWRITES_COMPLETED"}
    with pytest.raises(ValueError, match="allowlist"):
        SwarmExecutionFailure("free text from somewhere")
    assert EXECUTION_FAILURE_MESSAGES["SWARM_V2_REQUIRED_TASK_FAILED"] == \
        "required task execution failed"


# =============================================================================
# 5. the worker propagates the code to run.error.code and run_failed
# =============================================================================

def raising(exc: Exception):
    class Engine:
        workflow_key = "swarm_v2"

        def run(self, _run):
            raise exc

    return Engine()


@pytest.mark.parametrize("code", sorted(EXECUTION_FAILURE_CODES))
def test_each_static_code_reaches_run_error_and_the_run_failed_event(code, capsys):
    repo = SwarmWorkerRepo()
    assert worker_main.execute_run(repo.run_id, repo, raising(SwarmExecutionFailure(code))) == 0
    assert repo.failed[1] == code
    assert repo.failed[2] == EXECUTION_FAILURE_MESSAGES[code]
    run_failed = [event for event in repo.events if event[1] == "run_failed"]
    assert run_failed and run_failed[-1][2]["payload"]["code"] == code
    assert f"code={code} exception_class=SwarmExecutionFailure" in capsys.readouterr().out


def test_an_unexpected_exception_logs_its_class_and_code_but_never_its_message(capsys):
    repo = SwarmWorkerRepo()
    exc = KeyError("provider secret sentinel")
    assert worker_main.execute_run(repo.run_id, repo, raising(exc)) == 0
    assert repo.failed[1] == "SWARM_V2_EXECUTION_FAILED"
    out = capsys.readouterr().out
    assert "code=SWARM_V2_EXECUTION_FAILED exception_class=KeyError" in out
    assert "sentinel" not in out


# =============================================================================
# 6. the typed outcome and the plan policy
# =============================================================================

def test_the_outcome_is_read_only_from_the_resolve_variant_tool_result():
    ambiguous = {"resolved": False, "ambiguous": True, "match_count": 2, "variants": [],
                 "provenance": dict(PROVENANCE)}
    common = {"task_id": "t", "call_id": "c", "arguments": {"manufacturer": "TOYOTA"}}
    assert candidate_outcome(tool=GOVERNMENT_TOOL_NAME, operation="resolve_variant",
                             result=ambiguous, **common)["outcome"] == "unresolved_ambiguous"
    # Another operation of the same tool, a different tool, or a result whose
    # typed fields are not exactly booleans and an integer: no outcome at all.
    assert candidate_outcome(tool=GOVERNMENT_TOOL_NAME, operation="search_codes",
                             result=ambiguous, **common) is None
    assert candidate_outcome(tool="search", operation="resolve_variant",
                             result=ambiguous, **common) is None
    assert candidate_outcome(tool=GOVERNMENT_TOOL_NAME, operation="resolve_variant",
                             result={**ambiguous, "ambiguous": "true"}, **common) is None
    assert set(CANDIDATE_GAP_CODES.values()) == SOFT_GAP_CODES


def test_a_task_result_without_outcomes_is_unchanged():
    assert TaskResult("t", "completed", {"a": 1}).resolutions == ()


def test_the_commander_is_told_to_accept_ambiguity_for_duplicate_verification():
    rule = next(line for line in SOURCE_FIRST_TOOL_POLICY[GOVERNMENT_TOOL_NAME]
                if "duplicate candidates" in line)
    assert "allow_partial" in rule and "unresolved_ambiguous" in rule
    assert rule in provider_plan_policy(PlanLimits(), [GOVERNMENT_TOOL_NAME])["source_policy"]
    assert rule not in provider_plan_policy(PlanLimits(), ["search"])["source_policy"]
    # And the firewall admits such a task either way: the typed outcome, not
    # the flag, is what makes an ambiguity satisfy completion.
    validator = PlanValidator(allowed_tools=ToolRegistry([ReplayRegister()]).descriptors())
    for allow_partial in (True, False):
        validator.validate(gov_plan([gov_task("t04", [
            gov_call("c1", "4RUNNER", AMBIGUOUS_CODE), gov_call("c2", "4RUNNER", AMBIGUOUS_CODE)],
            allow_partial=allow_partial)]))

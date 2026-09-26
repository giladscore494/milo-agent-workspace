"""Phase 2: the vehicle-centric result, replayed on run 6825eb96's inputs.

Today's Swarm V2 result groups values BY FIELD under a model-level entity: all
4RUNNER 2026 variants share one entity, so `result.fields.trim` is a list of
eight trims and nobody can tell which trim belongs to which vehicle. The
assembler adds `vehicles`, `unresolved_groups` and `summary` beside the
existing keys, from the SAME inputs the builder already has.

`tests/replay_6825eb96.py` rebuilds those inputs: eight resolved
register rows, one ambiguous answer asked by t04 and t05 (rows 37350 / 37439),
and claims produced by the real Government evidence mapper.
`tests/fixtures/replay_6825eb96_pre_pr2_final.json` is the output of the
builder BEFORE this change on those inputs, committed so "the old keys are
unchanged" is a byte comparison, not a re-derivation.
"""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path

import pytest

from backend.catalog.result import assembler as assembler_module
from backend.catalog.result.assembler import (VEHICLE_REVIEW_CODES,
                                              VehicleCatalogResultAssembler)
from backend.engines.swarm_v2 import validate_product_outcome
from backend.engines.swarm_v2.builder import FinalBuilder
from backend.engines.swarm_v2.contracts import EvidenceReference, VerificationVerdict
from backend.export_envelope import SCHEMA_VERSION
from backend.product_outcome import derive_product_outcome
from replay_6825eb96 import (AMBIGUOUS_ROWS, AMBIGUOUS_TASKS, PLACEHOLDER_TASK,
                                             RESOLVED_ROWS, replay_inputs)

GOLDEN = Path(__file__).parent / "fixtures" / "replay_6825eb96_pre_pr2_final.json"
OLD_KEYS = ("status", "result_kind", "fields", "needs_review", "candidate_outcomes")
NEW_KEYS = ("vehicles", "unresolved_groups", "summary")


def build(inputs: dict) -> dict:
    return FinalBuilder().build(inputs["evidence"], inputs["verdicts"],
                                task_failures=inputs["task_failures"],
                                coverage_gaps=inputs["coverage_gaps"],
                                candidate_outcomes=inputs["candidate_outcomes"])


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def expected_rows(include_placeholder: bool) -> dict[str, tuple[str, str, str]]:
    """record -> (task, trim, official model code)."""
    return {row: (task, trim, code) for task, (row, _model, code, trim) in RESOLVED_ROWS.items()
            if include_placeholder or task != PLACEHOLDER_TASK}


# =============================================================================
# the replay
# =============================================================================

@pytest.mark.parametrize("include_placeholder,count", [(True, 8), (False, 7)])
def test_each_vehicle_carries_exactly_its_own_trim_and_code(include_placeholder, count):
    final = build(replay_inputs(include_placeholder=include_placeholder))
    vehicles = final["vehicles"]
    expected = expected_rows(include_placeholder)
    assert len(vehicles) == count
    assert [item["vehicle_key"] for item in vehicles] == sorted(expected)
    for vehicle in vehicles:
        task, trim, code = expected[vehicle["vehicle_key"]]
        assert vehicle["fields"]["trim"]["value"] == trim
        assert vehicle["fields"]["official_model_code"]["value"] == code
        assert vehicle["identity"]["trim"] == trim
        assert vehicle["identity"]["official_model_code"] == code
        assert vehicle["identity"]["manufacturer"] == "TOYOTA"
        assert vehicle["identity"]["model_year"] == 2026
        for field in vehicle["fields"].values():
            assert field["verdict"] == "verified"
            # No vehicle contains a value from another task.
            assert {entry["task_id"] for entry in field["provenance"]} == {task}
        assert vehicle["sources"] == [f"source-{task}"]
        assert vehicle["needs_review"] == []
    # The defect this phase addresses is still visible in the OLD key.
    assert len(final["fields"]["trim"]) == count


def test_the_placeholder_row_is_handled_like_any_resolved_row_in_old_data():
    vehicles = {item["vehicle_key"]: item for item in build(replay_inputs())["vehicles"]}
    placeholder = vehicles["37363"]
    assert placeholder["identity"]["commercial_model"] == "11111"
    assert placeholder["fields"]["official_model_code"]["value"] == "11111111"
    assert placeholder["fields"]["trim"]["value"] == "SE"


def test_the_ambiguous_answer_is_one_group_and_never_a_vehicle():
    final = build(replay_inputs())
    (group,) = final["unresolved_groups"]
    assert group == {
        "outcome": "unresolved_ambiguous",
        "candidate": {"manufacturer": "TOYOTA", "commercial_model": "4RUNNER",
                      "model_year": 2026, "trim": "LIMITED",
                      "official_model_code": "TZNA55L-GKZSZA"},
        "record_ids": list(AMBIGUOUS_ROWS),
        "task_ids": list(AMBIGUOUS_TASKS),
    }
    keys = {item["vehicle_key"] for item in final["vehicles"]}
    assert not keys & set(AMBIGUOUS_ROWS)
    # The ambiguous tasks' soft gaps are about the group, not about a vehicle.
    assert all(item["needs_review"] == [] for item in final["vehicles"])
    assert final["summary"] == {"vehicles_resolved": 8, "vehicles_with_review": 0,
                                "unresolved_ambiguous": 1, "unresolved_not_found": 0}


def test_the_old_keys_are_byte_identical_to_the_pre_pr2_builder():
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    for name, include in (("with_placeholder", True), ("without_placeholder", False)):
        final = build(replay_inputs(include_placeholder=include))
        assert set(final) == set(OLD_KEYS) | set(NEW_KEYS)
        assert canonical({key: final[key] for key in OLD_KEYS}) == canonical(golden[name])


def test_the_payload_is_deterministic():
    first = canonical(build(replay_inputs()))
    second = canonical(build(replay_inputs()))
    assert first == second
    # Input order does not matter either.
    shuffled = replay_inputs()
    for key in ("evidence", "verdicts", "candidate_outcomes", "coverage_gaps"):
        shuffled[key] = list(reversed(shuffled[key]))
    assert canonical(VehicleCatalogResultAssembler().assemble(**shuffled)) == \
        canonical(VehicleCatalogResultAssembler().assemble(**replay_inputs()))


def test_the_payload_stays_contract_valid_and_the_export_version_is_unchanged():
    final = build(replay_inputs())
    outcome = validate_product_outcome(final)
    assert (outcome.status, outcome.result_kind) == ("partial_success", "partial_result")
    assert derive_product_outcome("swarm_v2", final).semantic_status != "refused"
    # Purely additive inside `result`: the envelope shape is untouched.
    assert SCHEMA_VERSION == "milo-run-export/2"


def test_a_run_without_register_outcomes_is_byte_for_byte_unchanged():
    inputs = replay_inputs()
    final = FinalBuilder().build(inputs["evidence"], inputs["verdicts"])
    assert set(final) == {"status", "result_kind", "fields", "needs_review"}


# =============================================================================
# the linkage rules
# =============================================================================

def _claim(claim_id: str, task_id: str, locator: str | None, *, field: str = "trim",
           value="X") -> EvidenceReference:
    return EvidenceReference(claim_id=claim_id, source_id=f"src-{claim_id}", run_id="r",
                             task_id=task_id, entity="model:2026", field=field, value=value,
                             locator=locator, confidence=0.9)


def _locator(row: str, field: str = "ramat_gimur") -> str:
    return json.dumps(["record_field", f"snap:{row}", [field], None, None, None],
                      separators=(",", ":"))


def _resolved(task: str, row: str, **candidate) -> dict:
    return {"task_id": task, "call_id": "c1", "outcome": "resolved", "match_count": 1,
            "candidate": {"manufacturer": "TOYOTA", "commercial_model": "4RUNNER",
                          "model_year": 2026, **candidate}, "record_ids": [row]}


def _verified(*claims: EvidenceReference, verdict: str = "verified") -> list:
    return [VerificationVerdict(claim_id=claim.claim_id, verdict=verdict, reason="r")
            for claim in claims]


def test_a_claim_attaches_only_when_task_and_record_locator_both_match():
    own = _claim("own", "t02", _locator("37254"), value="SR5")
    other_record = _claim("other-record", "t02", _locator("37291"), value="LIMITED")
    other_task = _claim("other-task", "t03", _locator("37254"), value="WRONG")
    no_locator = _claim("web", "t02", None, value="from a web page")
    span = _claim("span", "t02", json.dumps(["document_span", "page-1", [], None, 0, 10],
                                            separators=(",", ":")), value="span")
    garbage = _claim("garbage", "t02", "not-a-locator", value="garbage")
    claims = [own, other_record, other_task, no_locator, span, garbage]
    result = VehicleCatalogResultAssembler().assemble(
        evidence=claims, verdicts=_verified(*claims),
        candidate_outcomes=[_resolved("t02", "37254"), _resolved("t03", "37291")])
    vehicles = {item["vehicle_key"]: item for item in result["vehicles"]}
    assert vehicles["37254"]["fields"]["trim"]["provenance"] == \
        [{"claim_id": "own", "source_id": "src-own", "task_id": "t02"}]
    assert vehicles["37254"]["fields"]["trim"]["value"] == "SR5"
    # 37291 was resolved by t03, and t03's only claim names another row.
    assert vehicles["37291"]["fields"] == {}


def test_values_that_disagree_for_one_row_are_not_shown_and_are_reported():
    first = _claim("a", "t02", _locator("37254"), value="SR5")
    second = _claim("b", "t09", _locator("37254"), value="SR5 PREMIUM")
    result = VehicleCatalogResultAssembler().assemble(
        evidence=[first, second], verdicts=_verified(first, second),
        candidate_outcomes=[_resolved("t02", "37254", trim="SR5"),
                            _resolved("t09", "37254", trim="SR5 PREMIUM")])
    (vehicle,) = result["vehicles"]
    assert "trim" not in vehicle["fields"]
    assert vehicle["identity"]["trim"] is None
    assert {"code": "FIELD_VALUES_DISAGREE", "field": "trim"} in vehicle["needs_review"]
    assert {"code": "IDENTITY_ARGUMENTS_DISAGREE"} in vehicle["needs_review"]
    assert result["summary"]["vehicles_with_review"] == 1


def test_verdicts_decide_what_is_shown():
    verified = _claim("v", "t02", _locator("37254", "degem_nm"), field="official_model_code")
    review = _claim("n", "t02", _locator("37254"), field="trim", value="SR5")
    rejected = _claim("x", "t02", _locator("37254", "shnat_yitzur"), field="model_year_start",
                      value=2025)
    unjudged = _claim("u", "t02", _locator("37254", "delek_cd"), field="fuel", value="petrol")
    verdicts = [*_verified(verified), *_verified(review, verdict="needs_review"),
                *_verified(rejected, verdict="rejected")]
    result = VehicleCatalogResultAssembler().assemble(
        evidence=[verified, review, rejected, unjudged], verdicts=verdicts,
        candidate_outcomes=[_resolved("t02", "37254")])
    (vehicle,) = result["vehicles"]
    assert vehicle["fields"]["official_model_code"]["verdict"] == "verified"
    assert vehicle["fields"]["trim"]["verdict"] == "needs_review"
    assert "model_year_start" not in vehicle["fields"]
    assert "fuel" not in vehicle["fields"]
    assert vehicle["needs_review"] == [
        {"code": "FIELD_NEEDS_REVIEW", "field": "trim"},
        {"code": "FIELD_REJECTED", "field": "model_year_start"},
        {"code": "FIELD_UNVERIFIED", "field": "fuel"},
    ]
    assert {entry["code"] for entry in vehicle["needs_review"]} <= VEHICLE_REVIEW_CODES


def test_task_codes_reach_the_vehicle_but_candidate_gaps_do_not():
    result = VehicleCatalogResultAssembler().assemble(
        evidence=[], verdicts=[],
        candidate_outcomes=[_resolved("t02", "37254"),
                            {"task_id": "t02", "call_id": "c2", "outcome": "unresolved_not_found",
                             "match_count": 0, "candidate": {"manufacturer": "TOYOTA",
                                                             "commercial_model": "4RUNNER",
                                                             "model_year": 2026, "trim": "X"},
                             "record_ids": []}],
        coverage_gaps=[{"task_id": "t02", "code": "CANDIDATE_UNRESOLVED_NOT_FOUND"},
                       {"task_id": "t02", "code": "EVIDENCE_REQUIREMENTS_UNMET"},
                       {"task_id": "t07", "code": "EVIDENCE_REQUIREMENTS_UNMET"}],
        task_failures=[{"task_id": "t02", "code": "TOOL_EXECUTION_FAILED"}])
    (vehicle,) = result["vehicles"]
    assert vehicle["needs_review"] == [
        {"code": "EVIDENCE_REQUIREMENTS_UNMET", "task_id": "t02"},
        {"code": "TOOL_EXECUTION_FAILED", "task_id": "t02"},
    ]
    (group,) = result["unresolved_groups"]
    assert group["outcome"] == "unresolved_not_found" and group["record_ids"] == []
    assert group["candidate"]["trim"] == "X" and group["task_ids"] == ["t02"]
    assert result["summary"]["unresolved_not_found"] == 1


def test_a_resolved_outcome_without_exactly_one_row_is_never_a_vehicle():
    broken = _resolved("t02", "37254")
    broken["record_ids"] = ["37254", "37291"]
    empty = _resolved("t03", "37291")
    empty["record_ids"] = []
    result = VehicleCatalogResultAssembler().assemble(
        evidence=[], verdicts=[], candidate_outcomes=[broken, empty])
    assert result["vehicles"] == []


def test_the_assembler_takes_no_task_output_and_does_no_io():
    parameters = set(inspect.signature(VehicleCatalogResultAssembler.assemble).parameters)
    assert parameters == {"self", "evidence", "verdicts", "candidate_outcomes",
                          "coverage_gaps", "task_failures"}
    tree = ast.parse(inspect.getsource(assembler_module))
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names}
    imported |= {node.module for node in ast.walk(tree)
                 if isinstance(node, ast.ImportFrom) and node.module}
    assert imported == {"__future__", "json", "typing",
                        "backend.engines.swarm_v2.contracts",
                        "backend.engines.swarm_v2.current_verdict",
                        "backend.engines.swarm_v2.evidence_contracts",
                        "backend.engines.swarm_v2.resolution"}
    called = {node.func.id for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert not called & {"open", "print", "input", "eval", "exec"}

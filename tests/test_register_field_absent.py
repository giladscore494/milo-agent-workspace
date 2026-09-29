"""P32 (PR-HYG): a resolved register row that states no value for a required
evidence field is the soft REGISTER_FIELD_ABSENT, not EVIDENCE_REQUIREMENTS_UNMET.

Production: `ramat_gimur` is null in 284 of 6,374 Toyota rows and `delek_cd`
in 66. The register has nothing to quote for those rows, so the evidence
mapper emits no `trim` / fuel fact for them. That is a true answer, not a
failed task. The end-to-end replay case is in tests/test_replay_runs.py.
"""

from __future__ import annotations

from types import SimpleNamespace

from backend.catalog.government.evidence import GOVERNMENT_FIELD_SOURCES, GOVERNMENT_TOOL_NAME
from backend.engines.swarm_v2.engine import SwarmV2Engine
from backend.engines.swarm_v2.resolution import (REGISTER_EVIDENCE_FIELDS, REGISTER_FIELD_ABSENT,
                                                 SOFT_GAP_CODES, candidate_outcome)

RECORD = {"upstream_record_id": "38683", "tozar": "טויוטה", "kinuy_mishari": "RAV4",
          "shnat_yitzur": 2022, "degem_nm": "AXAA54L-CNZVBA", "ramat_gimur": "ADVENTURE",
          "delek_cd": 1, "delek_nm": "בנזין"}


def _outcome(record: dict | None, *, resolved: bool = True) -> dict:
    result = {"resolved": resolved, "ambiguous": False, "match_count": 1 if resolved else 0,
              "variants": [{"upstream_record_id": "38683"}] if resolved else []}
    if record is not None:
        result["source_record"] = record
    return candidate_outcome(task_id="t01", call_id="c1", tool=GOVERNMENT_TOOL_NAME,
                             operation="resolve_variant", arguments={}, result=result)


def test_the_field_table_is_the_evidence_mappers():
    assert REGISTER_EVIDENCE_FIELDS == {field: register for field, register, _ctx, _unit
                                        in GOVERNMENT_FIELD_SOURCES}
    assert REGISTER_FIELD_ABSENT in SOFT_GAP_CODES


def test_a_resolved_outcome_names_the_fields_its_row_does_not_state():
    assert "register_fields_absent" not in _outcome(RECORD)
    no_trim = {k: v for k, v in RECORD.items() if k != "ramat_gimur"}
    assert _outcome(no_trim)["register_fields_absent"] == ["trim"]
    no_fuel = {**RECORD, "delek_cd": None}
    assert _outcome(no_fuel)["register_fields_absent"] == ["identity_dimensions.fuel_type"]
    # Only a RESOLVED row with its server-built record says anything.
    assert "register_fields_absent" not in _outcome(None)
    assert "register_fields_absent" not in _outcome(no_trim, resolved=False)


def _gaps(required: list[str], fields: set[str], absent: list[str] | None, *,
          sources: int = 1) -> list[dict[str, str]]:
    task = SimpleNamespace(
        task_id="t01", completion=SimpleNamespace(required_outputs=(), evidence_satisfied=True),
        evidence=SimpleNamespace(min_confidence=0.9, minimum_sources=sources, required_fields=required))
    plan = SimpleNamespace(graph=SimpleNamespace(tasks=[task]))
    results = {"t01": SimpleNamespace(status="completed", output={})}
    evidence = [SimpleNamespace(task_id="t01", supported=True, confidence=0.95,
                                source_id="s1", field=field) for field in sorted(fields)]
    outcome = {"task_id": "t01", "outcome": "resolved"}
    if absent is not None:
        outcome["register_fields_absent"] = absent
    return SwarmV2Engine._coverage_gaps(plan, results, evidence, {"t01": [outcome]})


def test_a_shortfall_of_exactly_absent_fields_is_soft():
    assert _gaps(["trim", "official_model_code"], {"official_model_code"}, ["trim"]) == \
        [{"task_id": "t01", "code": REGISTER_FIELD_ABSENT}]


def test_any_other_shortfall_stays_hard():
    hard = [{"task_id": "t01", "code": "EVIDENCE_REQUIREMENTS_UNMET"}]
    # The row states the field, the evidence still lacks it.
    assert _gaps(["trim"], set(), None) == hard
    # Absent explains only part of what is missing.
    assert _gaps(["trim", "official_model_code"], set(), ["trim"]) == hard
    # The source minimum is not met either.
    assert _gaps(["trim"], set(), ["trim"], sources=2) == hard
    # Nothing missing: no gap at all.
    assert _gaps(["trim"], {"trim"}, ["identity_dimensions.fuel_type"]) == []

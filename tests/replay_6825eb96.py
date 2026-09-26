"""Test-only: the inputs FinalBuilder had at the end of run 6825eb96, rebuilt offline.

What is taken from the run, and what is not
-------------------------------------------

From the run (Gate 0, partial_success): ten tasks; eight `resolve_variant`
calls that RESOLVED, one register row each -- 37363, 37254, 37291, 37293,
37096, 37098, 37425, 37316 -- and one AMBIGUOUS answer asked twice, by t04 and
by t05, matching rows 37350 and 37439. 37363 is the placeholder row
(kinuy_mishari "11111", degem_nm "11111111", trim SE, 2026) that PR-U now keeps
out of the work queue; it is included here because old data holds it.

NOT from the run: the export document itself is not in this repository, and
nothing here reads production. The per-row trims and model codes below are
fixture values chosen to be distinct per row, so that "each vehicle carries
exactly its own trim and code" is a checkable property. Every claim is built
by the REAL trusted Government evidence mapper from a `resolve_variant`
result, so the entities, field keys, units and record locators are exactly
what production writes -- including the defect this phase addresses: every
4RUNNER 2026 row shares ONE model-level entity.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from backend.catalog.government.evidence import GovernmentVariantEvidenceMapper
from backend.engines.swarm_v2.contracts import EvidenceReference, VerificationVerdict
from backend.engines.swarm_v2.resolution import candidate_outcome

RUN_ID = "6825eb96-1550-4464-8e8a-ecb1eca3e967"
SNAPSHOT_KEY = "gov-wltp-toyota-6825eb96"
PROVENANCE = {"snapshot_key": SNAPSHOT_KEY, "resource_id": "142afde2-6228-49f9-8a29-9b6c3a0cbe40",
              "package_id": "vehicle-models", "upstream_version": "2026.09.1",
              "upstream_version_kind": "dataset_version", "content_sha256": "c" * 64,
              "normalization_contract": "gov.wltp.normalize.1",
              "normalization_issue_count": 0}

#: task -> (register row, commercial model, official model code, trim)
RESOLVED_ROWS: dict[str, tuple[str, str, str, str]] = {
    "t01": ("37363", "11111", "11111111", "SE"),
    "t02": ("37254", "4RUNNER", "TRN285L-GKTSKA", "SR5"),
    "t03": ("37291", "4RUNNER", "TRN285L-GKTLKA", "LIMITED"),
    "t06": ("37293", "4RUNNER", "TRN285L-GKTXKA", "TRD PRO"),
    "t07": ("37096", "4RUNNER", "TRN285L-GKTHKA", "TRAILHUNTER"),
    "t08": ("37098", "4RUNNER", "TRN285L-GKTPKA", "PLATINUM"),
    "t09": ("37425", "4RUNNER", "TRN285L-GKTOKA", "TRD OFF-ROAD"),
    "t10": ("37316", "4RUNNER", "TRN285L-GKTMKA", "SR5 PREMIUM"),
}
AMBIGUOUS_TASKS = ("t04", "t05")
AMBIGUOUS_ROWS = ("37350", "37439")
AMBIGUOUS_CODE = "TZNA55L-GKZSZA"
PLACEHOLDER_TASK = "t01"


def _variant(row: str, model: str, code: str, trim: str) -> dict[str, Any]:
    return {"candidate_id": f"cand-{row}", "candidate_key": f"key-{row}", "status": "candidate",
            "manufacturer": "TOYOTA", "commercial_model": model, "model_year_start": 2026,
            "model_year_end": 2026, "official_model_code": code, "trim": trim,
            "identity_dimensions": {"fuel_type": "petrol"},
            "upstream_record_id": row, "resource_id": PROVENANCE["resource_id"]}


def _resolved_result(row: str, model: str, code: str, trim: str) -> dict[str, Any]:
    return {"resolved": True, "ambiguous": False, "match_count": 1,
            "variants": [_variant(row, model, code, trim)],
            "source_record": {"upstream_record_id": row, "tozar": "TOYOTA",
                              "kinuy_mishari": model, "shnat_yitzur": 2026,
                              "degem_nm": code, "ramat_gimur": trim,
                              "delek_cd": 1, "delek_nm": "בנזין"},
            "provenance": dict(PROVENANCE)}


def _ambiguous_result() -> dict[str, Any]:
    return {"resolved": False, "ambiguous": True, "match_count": 2,
            "variants": [_variant(row, "4RUNNER", AMBIGUOUS_CODE, "LIMITED")
                         for row in AMBIGUOUS_ROWS],
            "provenance": dict(PROVENANCE)}


def replay_inputs(*, include_placeholder: bool = True) -> dict[str, Any]:
    """Evidence, verdicts and candidate outcomes, exactly as FinalBuilder receives them."""
    mapper = GovernmentVariantEvidenceMapper()
    outcomes: list[dict[str, Any]] = []
    evidence: list[EvidenceReference] = []
    for task_id, (row, model, code, trim) in RESOLVED_ROWS.items():
        if task_id == PLACEHOLDER_TASK and not include_placeholder:
            continue
        arguments = {"manufacturer": "TOYOTA", "commercial_model": model, "model_year": 2026,
                     "trim": trim, "official_model_code": code}
        result = _resolved_result(row, model, code, trim)
        outcomes.append(candidate_outcome(task_id=task_id, call_id="c1",
                                          tool="catalog.government_vehicle",
                                          operation="resolve_variant",
                                          arguments=arguments, result=result))
        bundle = mapper.map(SimpleNamespace(result=result))
        for index, fact in enumerate(bundle.facts):
            evidence.append(EvidenceReference(
                claim_id=f"claim-{task_id}-{index}", source_id=f"source-{task_id}",
                run_id=RUN_ID, task_id=task_id, entity=fact.entity_key, field=fact.field_key,
                geography=fact.geography, market=fact.market,
                time_scope=dict(fact.time_scope), value=fact.value, unit=fact.unit,
                locator=fact.locator.locator_key, identity=dict(fact.identity),
                confidence=0.95))
    for task_id in AMBIGUOUS_TASKS:
        arguments = {"manufacturer": "TOYOTA", "commercial_model": "4RUNNER", "model_year": 2026,
                     "trim": "LIMITED", "official_model_code": AMBIGUOUS_CODE}
        outcomes.append(candidate_outcome(task_id=task_id, call_id="c1",
                                          tool="catalog.government_vehicle",
                                          operation="resolve_variant",
                                          arguments=arguments, result=_ambiguous_result()))
    # The engine orders outcomes by (task_id, call_id).
    outcomes.sort(key=lambda item: (item["task_id"], item["call_id"]))
    verdicts = [VerificationVerdict(claim_id=item.claim_id, verdict="verified",
                                    reason="government register states this field")
                for item in evidence]
    gaps = [{"task_id": task_id, "code": "CANDIDATE_UNRESOLVED_AMBIGUOUS"}
            for task_id in AMBIGUOUS_TASKS]
    return {"evidence": evidence, "verdicts": verdicts, "candidate_outcomes": outcomes,
            "coverage_gaps": gaps, "task_failures": []}

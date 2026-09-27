"""E'-6: the coverage ledger derives pre-PR-2 outputs through the finalize path's assembler.

Run 6825eb96 finished before PR-2, so its `runs.output` carries exactly
`fields`, `status`, `result_kind`, `needs_review` and `candidate_outcomes` --
no `vehicles`, no `unresolved_groups`. `derive_coverage` reads only those two
keys, so the ledger backfill recorded all ten of its queue items `failed`.

The backfill now builds the two keys with the SAME `VehicleCatalogResultAssembler`
`FinalBuilder` runs, from the same inputs (the stored typed outcomes and the
evidence and verdicts of the run's last engine checkpoint). Re-running it turns
6825eb96 into its real statuses and never weakens a row it does not improve.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from backend.catalog import coverage
from backend.catalog.coverage import (ENRICHED, FAILED, PENDING, UNRESOLVED_AMBIGUOUS,
                                      assembled_output, backfill, derive_coverage)
from backend.catalog.government.preparation import ARTIFACT_KEY
from backend.engines.swarm_v2.grounding import VERIFIER_GROUNDING_VERSION
from tests.test_catalog_variant_coverage import (GATE0, World, ledger_by_record,
                                                 production_inputs, production_result)

#: The keys 6825eb96's recorded output carries -- exactly these, nothing else.
PRE_PR2_KEYS = {"fields", "status", "result_kind", "needs_review", "candidate_outcomes"}
RESOLVED = ("37096", "37098", "37254", "37291", "37293", "37316", "37363", "37425")
AMBIGUOUS = ("37350", "37439")


def pre_pr2_output(recording: dict[str, Any]) -> dict[str, Any]:
    """The recorded output shape: the builder's result minus PR-2's keys."""
    final = production_result(recording)
    return {key: final[key] for key in PRE_PR2_KEYS}


def engine_state(recording: dict[str, Any]) -> dict[str, Any]:
    """The `swarm_state` part of the run's last engine checkpoint."""
    inputs = production_inputs(recording)
    return {"evidence_references": [item.model_dump(mode="json") for item in inputs["evidence"]],
            "verifier_state": {item.claim_id: item.model_dump(mode="json")
                               for item in inputs["verdicts"]},
            "verifier_grounding_version": VERIFIER_GROUNDING_VERSION}


def gate0_world(*, with_state: bool = True) -> tuple[World, dict[str, Any], str]:
    """6825eb96 as production holds it: finished, with the PRE-PR-2 output, a
    last checkpoint carrying its preparation record and its engine state, and
    the ledger the earlier backfill wrote (every handed item `failed`)."""
    world = World()
    plan = world.plan(GATE0["snapshot_rows"])
    run, preparation, _ = world.finish_batch(plan, production_result(GATE0), write_ledger=False)
    world.repository.runs[run]["output"] = pre_pr2_output(GATE0)
    checkpoint = [row for row in world.repository.checkpoints if str(row["run_id"]) == run][-1]
    assert ARTIFACT_KEY in checkpoint["artifacts"]
    if with_state:
        checkpoint["artifacts"]["swarm_state"] = engine_state(GATE0)
    # What the backfill wrote before E'-6: nothing but `failed`.
    old = derive_coverage(world.repository.get_run(run)["output"], tuple(preparation.queue),
                          coverage._pinned_query(world.repository, preparation.snapshot_key))
    # Every handed item (production: 10; this replay's queue leaves the
    # placeholder row out, PR-U) and nothing else.
    assert old.counts == {FAILED: len(preparation.queue)}
    world.repository.rebuild_catalog_variant_coverage(
        run, coverage.BATCH_COVERAGE_LEVEL, [dict(entry) for entry in old.entries])
    return world, plan, run


def test_the_fixture_is_the_recorded_6825eb96_output_shape():
    world, _plan, run = gate0_world()
    output = world.repository.get_run(run)["output"]
    assert set(output) == PRE_PR2_KEYS
    assert "vehicles" not in output and "unresolved_groups" not in output
    assert {entry["status"] for entry in world.ledger().values()} == {FAILED}
    # One row per distinct identity key among the handed items (the duplicate
    # group 37350 / 37439 shares one key).
    assert len(world.ledger()) == 8


def test_rerunning_the_backfill_turns_6825eb96_into_its_real_statuses():
    world, plan, run = gate0_world()
    report = backfill(world.repository)
    assert report.as_record()["runs_recorded"] == 1
    assert report.by_run[run] == {ENRICHED: 8, UNRESOLVED_AMBIGUOUS: 2}
    assert ledger_by_record(world, plan, (*RESOLVED, *AMBIGUOUS)) == {
        **{record: ENRICHED for record in RESOLVED},
        **{record: UNRESOLVED_AMBIGUOUS for record in AMBIGUOUS}}
    # A second backfill changes nothing.
    before = world.ledger()
    backfill(world.repository)
    assert world.ledger() == before


def test_the_assembled_view_is_what_the_finalize_path_builds_from_the_same_inputs():
    world, _plan, run = gate0_world()
    checkpoint = world.repository.latest_checkpoint(run)
    assembled = assembled_output(world.repository.get_run(run)["output"], checkpoint)
    finalized = production_result(GATE0)
    canonical = lambda value: json.dumps(value, sort_keys=True, ensure_ascii=False)  # noqa: E731
    assert canonical(assembled["vehicles"]) == canonical(finalized["vehicles"])
    assert canonical(assembled["unresolved_groups"]) == canonical(finalized["unresolved_groups"])
    # The recorded keys are untouched.
    assert {key: assembled[key] for key in PRE_PR2_KEYS} == world.repository.get_run(run)["output"]


def test_the_backfill_never_weakens_a_row_it_does_not_improve():
    world, plan, run = gate0_world()
    # A later run ENRICHED the ambiguous group's variant (same register content).
    key = world.key_of("37350", plan["snapshot_key"])
    row = world.repository.catalog_variant_coverage[(key, coverage.BATCH_COVERAGE_LEVEL)]
    row["status"] = ENRICHED
    backfill(world.repository)
    assert world.ledger()[key]["status"] == ENRICHED
    assert ledger_by_record(world, plan, RESOLVED) == {record: ENRICHED for record in RESOLVED}


def test_without_engine_state_resolved_rows_are_pending_and_groups_still_ambiguous():
    """No readable evidence: nothing is verified, so nothing is called enriched --
    but the typed outcomes still say which rows resolved and which were ambiguous."""
    world, plan, run = gate0_world(with_state=False)
    report = backfill(world.repository)
    assert report.by_run[run] == {PENDING: 8, UNRESOLVED_AMBIGUOUS: 2}
    assert ledger_by_record(world, plan, AMBIGUOUS) == {
        record: UNRESOLVED_AMBIGUOUS for record in AMBIGUOUS}


def test_an_output_with_a_vehicle_view_or_without_outcomes_is_left_as_it_is():
    current = production_result(GATE0)
    assert assembled_output(current, None) is current
    empty = {"status": "failed", "fields": {}, "candidate_outcomes": []}
    assert assembled_output(empty, None) is empty
    old = pre_pr2_output(GATE0)
    frozen = copy.deepcopy(old)
    assembled_output(old, {"artifacts": {"swarm_state": engine_state(GATE0)}})
    assert old == frozen                       # never mutated in place


def test_a_state_of_another_grounding_version_contributes_no_verdict():
    state = engine_state(GATE0)
    state["verifier_grounding_version"] = 0
    view = assembled_output(pre_pr2_output(GATE0), {"artifacts": {"swarm_state": state}})
    assert view["vehicles"] and all(vehicle["fields"] == {} for vehicle in view["vehicles"])

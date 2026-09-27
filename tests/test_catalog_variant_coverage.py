"""PR-Z: the variant coverage ledger and pre-run filtering.

Never pay twice for the same variant at the same level. The ledger
(`backend/catalog/coverage.py`, migration 20260927000100) records what each
finished batch run established per register variant; the queue build and run
preparation leave out what it already settles.

Every world here is built through the real paths: register rows landed by the
real scoped ingestion, plans prepared by the real queue build, batch runs
created by the real run creator, preparation by `prepare_government_work`,
results by the real `FinalBuilder` / vehicle assembler over the recorded
production tool results of runs 6825eb96 and aa63369b, and the ledger written
by the same function the worker's finalize path calls.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from backend.catalog import coverage
from backend.catalog.coverage import (ENRICHED, FAILED, PENDING, UNRESOLVED_AMBIGUOUS,
                                      UNRESOLVED_NOT_FOUND, backfill, candidate_identity_key,
                                      coverage_decision, derive_coverage, record_run_coverage,
                                      variant_content_sha256, variant_identity_key,
                                      variant_identity_text)
from backend.catalog.government import vocabulary
from backend.catalog.government.evidence import GovernmentVariantEvidenceMapper
from backend.catalog.government.preparation import (ARTIFACT_KEY, EXCLUDED_ALREADY_ENRICHED,
                                                    EXCLUDED_KNOWN_UNRESOLVED,
                                                    EXCLUDED_PLACEHOLDER_SOURCE_RECORD,
                                                    PREPARATION_PHASE,
                                                    GovernmentPreparationError,
                                                    prepare_government_work)
from backend.catalog.scope import batches as work_scope_batches
from backend.catalog.scope import contract as wsc
from backend.engines.swarm_v2.builder import FinalBuilder
from backend.engines.swarm_v2.contracts import EvidenceReference, VerificationVerdict
from backend.engines.swarm_v2.resolution import candidate_outcome
from backend.errors import AppError
from backend.finalization import RunFinalizer, TerminalClaim
from backend.testing.memory_repository import MemoryRepository
from backend.testing.work_scope_seed import (committed_records, seed_prepared_plan,
                                             start_batch_run)
from backend.tools import ToolContext
from backend.tools.government_vehicle import GOVERNMENT_TOOL_NAME, GovernmentVehicleTool

REPLAY = Path(__file__).resolve().parent / "replay"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
MIGRATION = (Path(__file__).resolve().parents[1] / "supabase" / "migrations"
             / "20260927000100_catalog_variant_coverage.sql")


def manifest(name: str) -> dict[str, Any]:
    return json.loads((REPLAY / name / "manifest.json").read_text())


AA = manifest("aa63369b")
GATE0 = manifest("6825eb96")
#: aa63369b: eight resolved rows and two duplicate identity groups.
AA_RESOLVED = ("37096", "37098", "37254", "37291", "37293", "37316", "37417", "37425")
AA_GROUPS = (("37350", "37439"), ("37309", "37345"))
AA_GROUP_ROWS = tuple(row for group in AA_GROUPS for row in group)


# =============================================================================
# helpers
# =============================================================================

def production_result(recording: dict[str, Any]) -> dict[str, Any]:
    """The run's final payload, rebuilt from its RECORDED tool results.

    Every `resolve_variant` result goes through the real typed outcome and the
    real trusted Government evidence mapper; every mapped fact is verified (as
    the register's own statement is); the real FinalBuilder assembles it.
    """
    inputs = production_inputs(recording)
    return FinalBuilder().build(inputs["evidence"], inputs["verdicts"],
                                coverage_gaps=inputs["gaps"],
                                candidate_outcomes=inputs["outcomes"])


def production_inputs(recording: dict[str, Any]) -> dict[str, Any]:
    """The inputs FinalBuilder has at the end of the recorded run."""
    mapper = GovernmentVariantEvidenceMapper()
    outcomes: list[dict[str, Any]] = []
    evidence: list[EvidenceReference] = []
    run_id = str(uuid4())
    for entry in recording["tool_results"]:
        outcome = candidate_outcome(task_id=entry["task_id"], call_id=entry["call_id"],
                                    tool=entry["tool"], operation=entry["operation"],
                                    arguments=entry["arguments"], result=entry["result"])
        if outcome is None:
            continue
        outcomes.append(outcome)
        if outcome["outcome"] != "resolved":
            continue
        bundle = mapper.map(SimpleNamespace(result=entry["result"]))
        for index, fact in enumerate(bundle.facts):
            evidence.append(EvidenceReference(
                claim_id=f"claim-{entry['task_id']}-{index}",
                source_id=f"source-{entry['task_id']}", run_id=run_id,
                task_id=entry["task_id"], entity=fact.entity_key, field=fact.field_key,
                geography=fact.geography, market=fact.market,
                time_scope=dict(fact.time_scope), value=fact.value, unit=fact.unit,
                locator=fact.locator.locator_key, identity=dict(fact.identity),
                confidence=0.95))
    outcomes.sort(key=lambda item: (item["task_id"], item["call_id"]))
    verdicts = [VerificationVerdict(claim_id=item.claim_id, verdict="verified",
                                    reason="the register states this field")
                for item in evidence]
    gaps = [{"task_id": item["task_id"], "code": "CANDIDATE_UNRESOLVED_AMBIGUOUS"}
            for item in outcomes if item["outcome"] == UNRESOLVED_AMBIGUOUS]
    return {"evidence": evidence, "verdicts": verdicts, "gaps": gaps, "outcomes": outcomes}


def extra_rows(count: int, *, start: int = 50_000, year: int = 2020) -> list[dict[str, Any]]:
    """Distinct Toyota rows no recording names: committed R5 rows, re-identified."""
    base = committed_records(1)[0]
    rows = []
    for index in range(count):
        row = copy.deepcopy(base)
        row.update({"_id": start + index, "kinuy_mishari": f"EXTRA{index}",
                    "degem_nm": f"EXT-{index:04d}", "ramat_gimur": "BASE",
                    "shnat_yitzur": year})
        rows.append(row)
    return rows


class World:
    """One repository, one user and project; a fresh conversation per plan."""

    def __init__(self) -> None:
        self.repository = MemoryRepository()
        self.user, self.project = str(uuid4()), str(uuid4())
        self.repository.seed_user(self.user)
        self.repository.seed_project(self.project, f"p-{self.project[:8]}", "Coverage",
                                     [self.user], workflow_key="swarm_v2")

    def plan(self, records: list[dict[str, Any]], **fields: Any) -> dict[str, Any]:
        conversation = self.repository.create_conversation(
            UUID(self.project), "c", UUID(self.user))["id"]
        fields.setdefault("max_items", 20)
        fields.setdefault("batch_size", 20)
        return seed_prepared_plan(self.repository, user_id=self.user,
                                  conversation_id=conversation, units=("toyota",),
                                  records=[dict(row) for row in records], **fields)

    def queued_records(self, plan: dict[str, Any]) -> list[str]:
        """The register ids of everything the plan's preparation queued."""
        preparation = plan["preparation"]["id"]
        candidates = {row["id"]: row for row in self.repository.catalog_candidates.values()}
        records = {row["id"]: row for row in self.repository.catalog_raw_records.values()}
        return sorted(records[candidates[item["candidate_id"]]["raw_record_id"]]
                      ["upstream_record_id"]
                      for item in self.repository.work_scope_queue_items
                      if item["preparation_id"] == preparation)

    def unit_coverage(self, plan: dict[str, Any]) -> dict[str, Any]:
        (row,) = [row for row in self.repository.work_scope_unit_coverages
                  if row["preparation_id"] == plan["preparation"]["id"]]
        return row

    def start(self, plan: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """ONE batch run, claimed by a worker and running: (run id, its lease)."""
        run = start_batch_run(self.repository, plan)["run"]["id"]
        return run, self.claim(run)

    def claim(self, run: str, worker: str | None = None) -> dict[str, Any]:
        worker = worker or f"worker-{run[:8]}"
        claimed = self.repository.claim_run(UUID(run), worker)
        lease = {"worker_id": worker, "attempt": claimed["attempt"],
                 "lease_token": claimed["lease_token"]}
        if claimed["status"] != "running":
            self.repository.transition_run(UUID(run), "running", expected_worker_id=worker,
                                           expected_attempt=claimed["attempt"],
                                           expected_lease_token=claimed["lease_token"])
        return lease

    def finish_batch(self, plan: dict[str, Any], result: dict[str, Any], *,
                     write_ledger: bool = True) -> tuple[str, Any, dict[str, Any] | None]:
        """Run ONE batch the way the worker does, minus the models: claim the
        run, prepare it under its lease (which claims the paid work on every
        item), checkpoint the preparation record, finalize the product, and
        settle through the finalize path's own function."""
        run, lease = self.start(plan)
        preparation = prepare_government_work(self.repository, run_id=run, lease=lease)
        self.repository.save_checkpoint(
            {"run_id": run, "phase": PREPARATION_PHASE, "workflow_key": "swarm_v2",
             "completed_tasks": [], "failures": [],
             "artifacts": {ARTIFACT_KEY: preparation.as_artifact()}}, **lease)
        decision = RunFinalizer(self.repository, UUID(run), "swarm_v2", lease).finalize(
            TerminalClaim.product("swarm_v2", result))
        assert decision.status in ("completed", "partial_success"), decision.status
        answer = record_run_coverage(self.repository, run, preparation, result, lease) \
            if write_ledger else None
        return run, preparation, answer

    def ledger(self) -> dict[str, dict[str, Any]]:
        return {key: dict(row) for (key, _level), row
                in self.repository.catalog_variant_coverage.items()}

    def key_of(self, record_id: str, snapshot_key: str | None = None) -> str:
        """The identity key of one register row (of one snapshot, when given)."""
        snapshots = {row["id"]: row["snapshot_key"]
                     for row in self.repository.catalog_snapshots.values()}
        keys = set()
        for record in self.repository.catalog_raw_records.values():
            if record["upstream_record_id"] != record_id or (
                    snapshot_key is not None and snapshots[record["snapshot_id"]] != snapshot_key):
                continue
            candidate = next(row for row in self.repository.catalog_candidates.values()
                             if row["raw_record_id"] == record["id"])
            keys.add(candidate_identity_key(candidate, record["payload"]))
        (key,) = keys
        return key


def aa_world() -> tuple[World, dict[str, Any], str]:
    """aa63369b's twelve register rows enriched exactly as production resolved
    them: its batch run finished with the recorded outcome."""
    world = World()
    plan = world.plan(AA["snapshot_rows"])
    run, _preparation, answer = world.finish_batch(plan, production_result(AA))
    assert answer is not None and answer["written"] == 10
    return world, plan, run


# =============================================================================
# Z1: the variant identity key
# =============================================================================

def test_the_identity_text_is_length_prefixed_and_orders_dimensions_by_code_point():
    text = variant_identity_text("טויוטה", "4RUNNER", 2026, 2026, None, "SR5",
                                 "413", "758", "P",
                                 {"fuel_type": "petrol", "body_style": "suv"})
    assert text == ("milo-variant-identity/2|6:טויוטה|7:4RUNNER|4:2026|4:2026|-|3:SR5"
                    "|3:413|3:758|1:P|10:body_style=3:suv|9:fuel_type=6:petrol")
    # No value can imitate a separator, and absent is not empty.
    assert variant_identity_key("a|4:b", "c", None, None, None, None, None, None, None, {}) \
        != variant_identity_key("a", "b|1:c", None, None, None, None, None, None, None, {})
    assert variant_identity_text("a", "b", None, None, None, None, None, None, None,
                                 {}).endswith("|-|-|-|-|-|-|-")
    assert variant_identity_text("a", "b", None, None, None, None, "", None, None,
                                 {}).endswith("|-|-|-|-|0:|-|-")
    with pytest.raises(ValueError):
        variant_identity_key("a", "b", True, None, None, None, None, None, None, {})


def test_the_register_codes_are_read_verbatim_as_the_database_renders_them():
    # `payload->>'field'`: no trimming, no case folding, no number formatting
    # beyond PostgreSQL's own; absent and JSON null are both absent.
    assert coverage.register_codes({"tozeret_cd": 413, "degem_cd": " 0758 ",
                                    "sug_degem": "p"}) == ("413", " 0758 ", "p")
    assert coverage.register_codes({"tozeret_cd": None}) == (None, None, None)
    assert coverage.register_code({"x": True}, "x") == "true"
    assert coverage.register_code({"x": 413.0}, "x") == "413.0"
    assert coverage.register_code({"x": 1e-7}, "x") == "0.0000001"
    with pytest.raises(ValueError):
        coverage.register_code({"x": {"nested": 1}}, "x")
    # Each code changes the key; the same codes in another field do not match.
    row = next(item for item in AA["snapshot_rows"] if str(item["_id"]) == "37254")
    candidate = {"manufacturer": "טויוטה", "commercial_model": "4RUNNER",
                 "model_year_start": 2026, "model_year_end": 2026,
                 "official_model_code": None, "trim": "SR5", "identity_dimensions": {}}
    keys = {candidate_identity_key(candidate, row)}
    for field in ("tozeret_cd", "degem_cd", "sug_degem"):
        keys.add(candidate_identity_key(candidate, {**row, field: "changed"}))
    assert len(keys) == 4
    assert candidate_identity_key(candidate, {**row, "_id": 1}) == \
        candidate_identity_key(candidate, row)


def test_the_key_is_stable_across_two_snapshots_of_the_same_row():
    world = World()
    row = next(item for item in AA["snapshot_rows"] if str(item["_id"]) == "37254")
    # The second capture holds the SAME row under another register `_id` (the
    # register reuses and renumbers ids across captures) beside another row,
    # so it is a different snapshot with different content.
    renumbered = {**copy.deepcopy(row), "_id": 91_254}
    first = world.plan([row])
    second = world.plan([renumbered, *extra_rows(1)])
    assert first["snapshot_key"] != second["snapshot_key"]
    key = world.key_of("37254", first["snapshot_key"])
    assert world.key_of("91254", second["snapshot_key"]) == key
    # The content hash ignores the volatile `_id` too; the stored digest does not.
    records = {row["upstream_record_id"]: row
               for row in world.repository.catalog_raw_records.values()}
    assert variant_content_sha256(records["37254"]["payload"]) == \
        variant_content_sha256(records["91254"]["payload"])
    assert records["37254"]["payload_sha256"] != records["91254"]["payload_sha256"]


def test_the_eight_aa63369b_variants_get_eight_keys_and_each_true_duplicate_group_one():
    world = World()
    plan = world.plan(AA["snapshot_rows"])
    keys = {record: world.key_of(record, plan["snapshot_key"])
            for record in (*AA_RESOLVED, *AA_GROUP_ROWS)}
    assert len({keys[record] for record in AA_RESOLVED}) == 8
    # The REPLAY's group rows are stand-ins whose content minus `_id` is
    # identical -- true duplicates, so they share one key under /2 as well.
    # (Production's real 37350 / 37439 and 37309 / 37345 are different
    # vehicles with different `degem_cd`: see the Z2-4 fixture tests below.)
    for left, right in AA_GROUPS:
        rows = {str(row["_id"]): {k: v for k, v in row.items() if k != "_id"}
                for row in AA["snapshot_rows"]}
        assert rows[left] == rows[right]
    # 37350 / 37439 share one key; so do 37309 / 37345; the two groups differ,
    # and no group shares a key with a resolved variant.
    for left, right in AA_GROUPS:
        assert keys[left] == keys[right]
    assert len({keys[group[0]] for group in AA_GROUPS}) == 2
    assert not {keys[group[0]] for group in AA_GROUPS} & {keys[r] for r in AA_RESOLVED}


def test_the_upstream_record_id_alone_is_not_the_key():
    world = World()
    plan = world.plan(AA["snapshot_rows"])
    other = copy.deepcopy(next(item for item in AA["snapshot_rows"]
                               if str(item["_id"]) == "37254"))
    other.update({"_id": 37254 + 1_000_000, "ramat_gimur": "ANOTHER TRIM"})
    # The same register id naming a DIFFERENT row in another capture.
    reused = {**other, "_id": 37254}
    second = world.plan([reused, *extra_rows(1)])
    assert world.key_of("37254", second["snapshot_key"]) != \
        world.key_of("37254", plan["snapshot_key"])


def test_the_database_vocabulary_version_is_the_reviewed_one():
    text = MIGRATION.read_text()
    assert f"select '{vocabulary.VOCABULARY_VERSION}'::text;" in text
    assert "select 'milo-variant-identity/2'" in text
    assert coverage.VARIANT_IDENTITY_CONTRACT == "milo-variant-identity/2"
    assert "milo-variant-identity/1" not in text


# =============================================================================
# the ONE rule
# =============================================================================

C1, C2 = "a" * 64, "b" * 64
V = vocabulary.VOCABULARY_VERSION


@pytest.mark.parametrize("status,content,vocab,include,expected", [
    (None, C1, V, False, "queue"),
    (ENRICHED, C1, V, False, "excluded_already_enriched"),
    (ENRICHED, C1, "old", False, "excluded_already_enriched"),   # vocabulary: n/a
    (ENRICHED, C1, V, True, "excluded_already_enriched"),        # include: unresolved only
    (ENRICHED, C2, V, False, "queue"),                           # changed content
    (UNRESOLVED_AMBIGUOUS, C1, V, False, "excluded_known_unresolved"),
    (UNRESOLVED_NOT_FOUND, C1, V, False, "excluded_known_unresolved"),
    (UNRESOLVED_AMBIGUOUS, C2, V, False, "queue"),
    (UNRESOLVED_AMBIGUOUS, C1, "gov.wltp.vocabulary.1", False, "queue"),
    (UNRESOLVED_AMBIGUOUS, C1, V, True, "queue"),
    (FAILED, C1, V, False, "queue"),
    (PENDING, C1, V, False, "queue"),
])
def test_the_filtering_rule(status, content, vocab, include, expected):
    assert coverage_decision(status, content, vocab, C1, include_unresolved=include) == expected


# =============================================================================
# Z2: the ledger, derived from durable run data only
# =============================================================================

def test_the_finalize_path_records_aa63369b_exactly():
    world, plan, run = aa_world()
    ledger = world.ledger()
    key = {record: world.key_of(record, plan["snapshot_key"])
           for record in (*AA_RESOLVED, *AA_GROUP_ROWS)}
    assert len(ledger) == 10
    for record in AA_RESOLVED:
        assert ledger[key[record]]["status"] == ENRICHED
    for left, _right in AA_GROUPS:
        assert ledger[key[left]]["status"] == UNRESOLVED_AMBIGUOUS
    for row in ledger.values():
        assert row["last_run_id"] == run and row["level"] == "register"
        assert row["snapshot_key"] == plan["snapshot_key"]
        assert row["vocabulary_version"] == vocabulary.VOCABULARY_VERSION


def test_a_resolved_row_without_a_verified_field_is_pending_and_an_unsettled_one_failed():
    world = World()
    plan = world.plan(AA["snapshot_rows"])
    result = production_result(AA)
    # Nothing verified on 37254 (awaiting review); the t08 task (37417) left
    # no outcome at all.
    for vehicle in result["vehicles"]:
        if vehicle["vehicle_key"] == "37254":
            for field in vehicle["fields"].values():
                field["verdict"] = "needs_review"
    result["vehicles"] = [item for item in result["vehicles"] if item["vehicle_key"] != "37417"]
    world.finish_batch(plan, result)
    ledger = world.ledger()
    assert ledger[world.key_of("37254")]["status"] == PENDING
    assert ledger[world.key_of("37417")]["status"] == FAILED
    assert ledger[world.key_of("37096")]["status"] == ENRICHED


def test_a_not_found_answer_is_recorded_against_the_queue_items_it_asked_about():
    world = World()
    plan = world.plan(AA["snapshot_rows"])
    result = production_result(AA)
    target = next(item for item in result["vehicles"] if item["vehicle_key"] == "37098")
    result["vehicles"].remove(target)
    identity = dict(target["identity"])
    result["unresolved_groups"].append({"outcome": UNRESOLVED_NOT_FOUND, "candidate": identity,
                                        "record_ids": [], "task_ids": ["t05"]})
    world.finish_batch(plan, result)
    assert world.ledger()[world.key_of("37098")]["status"] == UNRESOLVED_NOT_FOUND


def test_the_ledger_write_is_idempotent_and_never_weakens_what_it_knows():
    world, plan, run = aa_world()
    before = world.ledger()
    preparation = prepare_government_work(world.repository, run_id=run,
                                          checkpoint=world.repository.latest_checkpoint(run))
    derived = derive_coverage(world.repository.get_run(run)["output"], preparation.queue,
                              coverage._pinned_query(world.repository, plan["snapshot_key"]))
    again = world.repository.rebuild_catalog_variant_coverage(
        run, "register", [dict(entry) for entry in derived.entries])
    assert again["written"] == 0 and world.ledger() == before
    # A later run of the same content that settles nothing never erases an
    # enrichment; a changed row replaces whatever was recorded.
    key = world.key_of("37254")
    candidate = next(item.candidate_id for item in preparation.queue
                     if item.official_model_code == "TRN285L-GKTSKA")
    world.repository.rebuild_catalog_variant_coverage(
        run, "register", [{"candidate_id": candidate, "status": FAILED}])
    assert world.ledger() == before
    assert world.ledger()[key]["status"] == ENRICHED


def test_the_ledger_refuses_what_durable_state_does_not_support():
    world, plan, run = aa_world()
    candidate = next(iter(world.repository.catalog_candidates.values()))["id"]
    with pytest.raises(AppError) as unfinished:
        world.repository.rebuild_catalog_variant_coverage(
            start_batch_run(world.repository, world.plan(extra_rows(1)))["run"]["id"],
            "register", [{"candidate_id": candidate, "status": ENRICHED}])
    assert unfinished.value.code == "CATALOG_COVERAGE_RUN_NOT_FINISHED"
    other = world.plan(extra_rows(2, start=60_000))
    foreign = next(row["id"] for row in world.repository.catalog_candidates.values()
                   if row["snapshot_id"] != next(
                       s["id"] for s in world.repository.catalog_snapshots.values()
                       if s["snapshot_key"] == plan["snapshot_key"]))
    with pytest.raises(AppError) as outside:
        world.repository.rebuild_catalog_variant_coverage(
            run, "register", [{"candidate_id": foreign, "status": ENRICHED}])
    assert outside.value.code == "CATALOG_COVERAGE_CANDIDATE_INVALID"
    for entries in ([{"candidate_id": candidate, "status": "done"}],
                    [{"candidate_id": candidate, "status": ENRICHED, "x": 1}]):
        with pytest.raises(AppError) as invalid:
            world.repository.rebuild_catalog_variant_coverage(run, "register", entries)
        assert invalid.value.code == "CATALOG_COVERAGE_INVALID"
    with pytest.raises(AppError) as level:
        world.repository.rebuild_catalog_variant_coverage(
            run, "web", [{"candidate_id": candidate, "status": ENRICHED}])
    assert level.value.code == "CATALOG_COVERAGE_INVALID"
    # The finalize path's write is accepted from the finalizing lease only.
    with pytest.raises(AppError) as stale:
        world.repository.record_catalog_variant_coverage(
            run, "register", [{"candidate_id": candidate, "status": ENRICHED}],
            worker_id="someone-else", attempt=1, lease_token="x")
    assert stale.value.code == "RUN_LEASE_LOST"
    assert other  # the second plan exists only to hold a foreign snapshot


def test_a_ledger_write_failure_never_changes_the_run_outcome():
    world = World()
    plan = world.plan(AA["snapshot_rows"])
    result = production_result(AA)

    class Failing:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            if name == "record_catalog_variant_coverage":
                def refuse(*_args, **_kwargs):
                    raise AppError("REPOSITORY_ERROR", "coverage ledger call failed", 502)
                return refuse
            return getattr(self._inner, name)

    run, preparation, _ = world.finish_batch(plan, result, write_ledger=False)
    lines: list[str] = []
    run_row = world.repository.get_run(run)
    lease = {key: run_row[key] for key in ("worker_id", "attempt", "lease_token")}
    assert record_run_coverage(Failing(world.repository), run, preparation, result, lease,
                               log=lines.append) is None
    assert lines == [f"catalog coverage ledger write failed: run_id={run} "
                     "exception_class=AppError"]
    assert world.repository.get_run(run)["status"] == run_row["status"] == "partial_success"
    assert world.repository.get_run(run)["output"] == result
    assert world.ledger() == {}


# =============================================================================
# Z3a: the queue build
# =============================================================================

def test_toyota_2018_plus_queues_none_of_the_enriched_or_known_ambiguous_variants():
    world, _plan, _run = aa_world()
    others = extra_rows(5)
    revision = world.plan([*AA["snapshot_rows"], *others], model_year_from=2018)
    queued = world.queued_records(revision)
    assert queued == sorted(str(row["_id"]) for row in others)
    assert not set(queued) & {*AA_RESOLVED, *AA_GROUP_ROWS}
    unit = world.unit_coverage(revision)
    assert unit["excluded_already_enriched"] == 8
    assert unit["excluded_known_unresolved"] == 4        # two groups of two rows
    assert sorted(entry["upstream_record_id"] for entry in unit["excluded_records"]) == \
        sorted((*AA_RESOLVED, *AA_GROUP_ROWS))
    assert {entry["reason"] for entry in unit["excluded_records"]
            if entry["upstream_record_id"] in AA_GROUP_ROWS} == {"excluded_known_unresolved"}
    assert unit["include_unresolved"] is False
    # The summary carries the counts; `eligible_count` still counts every row.
    (summary_unit,) = world.repository._work_scope_preparation_summary(
        revision["preparation"], True)["units"]
    assert summary_unit["coverage"]["excluded_already_enriched"] == 8
    assert summary_unit["eligible_count"] == 17 and summary_unit["queued_count"] == 5


def test_changing_one_rows_content_requeues_exactly_that_variant():
    world, _plan, _run = aa_world()
    rows = copy.deepcopy(AA["snapshot_rows"])
    changed = next(row for row in rows if str(row["_id"]) == "37096")
    changed["koah_sus"] = int(changed["koah_sus"]) + 1       # not identity: same key
    revision = world.plan(rows, model_year_from=2018)
    assert world.queued_records(revision) == ["37096"]
    unit = world.unit_coverage(revision)
    assert (unit["excluded_already_enriched"], unit["excluded_known_unresolved"]) == (7, 4)


def test_include_unresolved_requeues_the_ambiguous_groups_only():
    world, _plan, _run = aa_world()
    revision = world.plan(AA["snapshot_rows"], model_year_from=2018, include_unresolved=True)
    assert world.queued_records(revision) == sorted(AA_GROUP_ROWS)
    unit = world.unit_coverage(revision)
    assert unit["include_unresolved"] is True
    assert (unit["excluded_already_enriched"], unit["excluded_known_unresolved"]) == (8, 0)


def test_a_vocabulary_change_requeues_known_unresolved_but_not_enriched(monkeypatch):
    world, _plan, _run = aa_world()
    # The reviewed vocabulary moves on (its database copy with it).
    monkeypatch.setattr(coverage, "VOCABULARY_VERSION", "gov.wltp.vocabulary.3")
    revision = world.plan(AA["snapshot_rows"], model_year_from=2018)
    assert world.queued_records(revision) == sorted(AA_GROUP_ROWS)


def test_a_plan_the_ledger_settles_whole_queues_nothing():
    world, _plan, _run = aa_world()
    revision = world.plan(AA["snapshot_rows"])
    assert world.queued_records(revision) == []
    assert revision["batches"] == []
    assert revision["preparation"]["queued_item_count"] == 0
    # The empty queue is stated per unit, and nothing can start.
    view = work_scope_batches.progress(world.repository, UUID(world.user),
                                       UUID(revision["work_scope_id"]))
    assert view["status"] == "nothing_queued"
    assert view["preparation"]["units"][0]["coverage"] == {
        "enriched": 8, "ambiguous": 4, "pending": 0, "queued": 0}


def test_the_limit_is_spent_only_on_what_the_ledger_does_not_settle():
    world, _plan, _run = aa_world()
    others = extra_rows(6)
    revision = world.plan([*AA["snapshot_rows"], *others], max_items=4, batch_size=2)
    assert len(world.queued_records(revision)) == 4
    view = work_scope_batches.progress(world.repository, UUID(world.user),
                                       UUID(revision["work_scope_id"]))
    assert view["preparation"]["units"][0]["coverage"] == {
        "enriched": 8, "ambiguous": 4, "pending": 2, "queued": 4}


def test_include_unresolved_is_an_optional_plan_key_that_keeps_old_digests():
    base = {"units": ["toyota"], "model_year_from": 2018, "model_year_to": None,
            "max_items": 100, "batch_size": 10}
    plain = wsc.scope_from_fields(base)
    assert wsc.INCLUDE_UNRESOLVED_KEY not in plain.canonical_text()
    assert wsc.scope_from_fields({**base, "include_unresolved": False}).digest() == plain.digest()
    flagged = wsc.scope_from_fields({**base, "include_unresolved": True})
    assert flagged.as_record()["include_unresolved"] is True
    assert flagged.digest() != plain.digest()
    assert wsc.scope_from_text(flagged.canonical_text()) == flagged
    assert wsc.stored_record_valid(flagged.as_record())
    assert not wsc.stored_record_valid({**plain.as_record(), "include_unresolved": False})
    for bad in ("yes", 1, None):
        with pytest.raises(wsc.WorkScopeError):
            wsc.scope_from_fields({**base, "include_unresolved": bad})


# =============================================================================
# Z3b: run preparation
# =============================================================================

def test_run_preparation_leaves_out_what_the_ledger_settled_since_the_queue_was_built():
    world = World()
    others = extra_rows(3)
    # Prepared BEFORE aa63369b's batch finished: the queue holds all fifteen.
    later = world.plan([*AA["snapshot_rows"], *others])
    assert len(world.queued_records(later)) == 15
    first = world.plan(AA["snapshot_rows"])
    world.finish_batch(first, production_result(AA))

    run = start_batch_run(world.repository, later)["run"]["id"]
    preparation = prepare_government_work(world.repository, run_id=run)
    assert sorted(item.commercial_model for item in preparation.queue) == \
        sorted(row["kinuy_mishari"] for row in others)
    assert preparation.excluded_already_enriched == 8
    assert preparation.excluded_known_unresolved == 4
    artifact = preparation.as_artifact()
    assert artifact["excluded_already_enriched"] == 8
    assert artifact["excluded_known_unresolved"] == 4
    assert artifact["excluded_placeholder"] == 0
    reasons = {entry["upstream_record_id"]: entry["reason"]
               for entry in artifact["excluded_records"]}
    assert reasons == {**{record: EXCLUDED_ALREADY_ENRICHED for record in AA_RESOLVED},
                       **{record: EXCLUDED_KNOWN_UNRESOLVED for record in AA_GROUP_ROWS}}
    # A resumed attempt keeps the recorded queue and exclusions.
    resumed = prepare_government_work(world.repository, run_id=run, checkpoint={
        "phase": PREPARATION_PHASE, "artifacts": {ARTIFACT_KEY: artifact}})
    assert resumed.resumed and resumed.excluded == preparation.excluded


def test_a_run_whose_batch_the_ledger_settles_whole_is_refused_before_any_paid_call():
    world = World()
    later = world.plan(AA["snapshot_rows"])
    first = world.plan(AA["snapshot_rows"])
    world.finish_batch(first, production_result(AA))
    run = start_batch_run(world.repository, later)["run"]["id"]
    with pytest.raises(GovernmentPreparationError) as refused:
        prepare_government_work(world.repository, run_id=run)
    assert refused.value.code == "GOVERNMENT_BATCH_ALREADY_COVERED"


def test_placeholders_alone_still_refuse_as_placeholders():
    world = World()
    placeholder = copy.deepcopy(AA["snapshot_rows"][0])
    placeholder.update({"_id": 37363, "kinuy_mishari": "11111", "degem_nm": "11111111"})
    plan = world.plan([placeholder])
    run = start_batch_run(world.repository, plan)["run"]["id"]
    with pytest.raises(GovernmentPreparationError) as refused:
        prepare_government_work(world.repository, run_id=run)
    assert refused.value.code == "GOVERNMENT_BATCH_ONLY_PLACEHOLDERS"


def test_a_record_written_before_the_ledger_resumes_with_no_ledger_exclusions():
    world = World()
    plan = world.plan(AA["snapshot_rows"])
    run = start_batch_run(world.repository, plan)["run"]["id"]
    artifact = prepare_government_work(world.repository, run_id=run).as_artifact()
    old = {key: value for key, value in artifact.items()
           if key not in ("excluded_already_enriched", "excluded_known_unresolved")}
    resumed = prepare_government_work(world.repository, run_id=run, checkpoint={
        "artifacts": {ARTIFACT_KEY: old}})
    assert resumed.excluded == ()
    # A record that states a ledger exclusion it does not count is refused.
    lying = {**old, "excluded_records": [{"upstream_record_id": "37096",
                                          "reason": EXCLUDED_ALREADY_ENRICHED}]}
    with pytest.raises(GovernmentPreparationError) as refused:
        prepare_government_work(world.repository, run_id=run, checkpoint={
            "artifacts": {ARTIFACT_KEY: lying}})
    assert refused.value.code == "GOVERNMENT_PREPARATION_RECORD_INVALID"
    assert EXCLUDED_PLACEHOLDER_SOURCE_RECORD  # the PR-U reason is unchanged


def test_a_ledger_read_that_fails_refuses_the_run():
    world = World()
    plan = world.plan(AA["snapshot_rows"])
    run = start_batch_run(world.repository, plan)["run"]["id"]

    class Broken:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            if name == "catalog_variant_coverage_for_batch":
                def refuse(*_args, **_kwargs):
                    raise AppError("REPOSITORY_ERROR", "coverage ledger call failed", 502)
                return refuse
            return getattr(self._inner, name)

    with pytest.raises(GovernmentPreparationError) as refused:
        prepare_government_work(Broken(world.repository), run_id=run)
    assert refused.value.code == "GOVERNMENT_QUEUE_UNAVAILABLE"


# =============================================================================
# Z4: the progress read the website renders
# =============================================================================

def test_progress_states_unit_counts_only_when_the_preparation_recorded_them():
    world, plan, _run = aa_world()
    view = work_scope_batches.progress(world.repository, UUID(world.user),
                                       UUID(plan["work_scope_id"]))
    (unit,) = view["preparation"]["units"]
    # aa63369b's own plan was prepared on an empty ledger: 12 queued, none left out.
    assert unit["coverage"] == {"enriched": 0, "ambiguous": 0, "pending": 0, "queued": 12}
    # A preparation with no recorded counts (written before the ledger) renders
    # without them.
    world.repository.work_scope_unit_coverages.clear()
    view = work_scope_batches.progress(world.repository, UUID(world.user),
                                       UUID(plan["work_scope_id"]))
    assert "coverage" not in view["preparation"]["units"][0]


def test_progress_refuses_counts_that_do_not_add_up():
    world, plan, _run = aa_world()
    world.repository.work_scope_unit_coverages[0]["excluded_already_enriched"] = 50
    with pytest.raises(AppError) as refused:
        work_scope_batches.progress(world.repository, UUID(world.user),
                                    UUID(plan["work_scope_id"]))
    assert refused.value.code == "WORK_SCOPE_PROGRESS_UNAVAILABLE"


# =============================================================================
# Z5: a production-scale snapshot reads only bounded pages
# =============================================================================

REAL_TOYOTA_SNAPSHOT_SIZE = 6_374


class BoundedSpy:
    """Refuses every whole-snapshot and whole-ledger read; records every page."""

    REFUSED = ("list_catalog_raw_records", "list_catalog_candidates")

    def __init__(self, inner: MemoryRepository) -> None:
        self._inner = inner
        self.calls: list[tuple[str, int]] = []

    def __getattr__(self, name: str):
        attribute = getattr(self._inner, name)
        if name in self.REFUSED:
            def refused(*_args, **_kwargs):
                raise AssertionError(f"a whole-snapshot read via {name}")
            return refused
        if (name.startswith("catalog_") or name.endswith("_coverage")) and callable(attribute):
            def counted(*args, **kwargs):
                result = attribute(*args, **kwargs)
                rows = len(result) if isinstance(result, list) else (
                    len(result.get("items") or []) if isinstance(result, dict) else 0)
                self.calls.append((name, rows))
                return result
            return counted
        return attribute


def grow_snapshot(world: World, snapshot_key: str, size: int) -> None:
    """Grow one pinned snapshot in memory to `size` rows of distinct filler."""
    snapshot = next(row for row in world.repository.catalog_snapshots.values()
                    if row["snapshot_key"] == snapshot_key)
    template_record = next(row for row in world.repository.catalog_raw_records.values()
                           if row["snapshot_id"] == snapshot["id"])
    template_candidate = next(row for row in world.repository.catalog_candidates.values()
                              if row["raw_record_id"] == template_record["id"])
    existing = sum(1 for row in world.repository.catalog_candidates.values()
                   if row["snapshot_id"] == snapshot["id"])
    for index in range(size - existing):
        record = copy.deepcopy(template_record)
        record.update({"id": str(uuid4()), "record_key": f"cr1.{index:032x}",
                       "upstream_record_id": str(200_000 + index)})
        record["payload"]["_id"] = 200_000 + index
        record["payload"]["kinuy_mishari"] = f"FILLER{index % 97}"
        candidate = copy.deepcopy(template_candidate)
        candidate.update({"id": str(uuid4()), "candidate_key": f"cc1.{index:032x}",
                          "raw_record_id": record["id"],
                          "commercial_model": f"FILLER{index % 97}",
                          "official_model_code": f"FILL-{index:05d}"})
        world.repository.catalog_raw_records[(snapshot["id"], record["record_key"])] = record
        world.repository.catalog_candidates[(snapshot["id"], candidate["candidate_key"])] = \
            candidate
    metadata = snapshot["retrieval_metadata"]
    for field in ("reported_total", "captured_record_count", "normalized_record_count"):
        metadata[field] = size
    snapshot["stored_record_count"] = snapshot["declared_record_count"] = size


def test_a_production_scale_snapshot_is_read_in_bounded_pages_only():
    world = World()
    later = world.plan([*AA["snapshot_rows"], *extra_rows(3)])
    first = world.plan(AA["snapshot_rows"])
    grow_snapshot(world, first["snapshot_key"], REAL_TOYOTA_SNAPSHOT_SIZE)
    grow_snapshot(world, later["snapshot_key"], REAL_TOYOTA_SNAPSHOT_SIZE)
    result = production_result(AA)

    # The finalize path: bounded identity pages over a 6 374-row snapshot.
    run, preparation, _ = world.finish_batch(first, result, write_ledger=False)
    spy = BoundedSpy(world.repository)
    run_row = world.repository.get_run(run)
    lease = {key: run_row[key] for key in ("worker_id", "attempt", "lease_token")}
    answer = record_run_coverage(spy, run, preparation, result, lease)
    assert answer is not None and answer["written"] == 10
    pages = [rows for name, rows in spy.calls if name == "catalog_candidate_variant_page"]
    assert pages and max(pages) <= coverage.MAX_IDENTITY_SCAN_ROWS
    assert sum(pages) < 50                       # nothing near 6 374 rows

    # Run preparation: ONE ledger read of the batch's own items.
    spy = BoundedSpy(world.repository)
    other = start_batch_run(world.repository, later)["run"]["id"]
    prepared = prepare_government_work(spy, run_id=other)
    assert prepared.excluded_already_enriched == 8
    reads = [rows for name, rows in spy.calls if name == "catalog_variant_coverage_for_batch"]
    assert reads == [15]
    assert sum(rows for _name, rows in spy.calls) < 100

    # The backfill: keyset pages of runs, one run at a time, bounded pages.
    spy = BoundedSpy(world.repository)
    report = backfill(spy, dry_run=True)
    assert report.runs_recorded == 1
    listing = [rows for name, rows in spy.calls if name == "catalog_variant_coverage_runs"]
    assert listing and max(listing) <= coverage.MAX_BACKFILL_PAGE
    assert sum(rows for _name, rows in spy.calls) < 100


# =============================================================================
# the backfill reproduces 6825eb96 and aa63369b
# =============================================================================

def recorded_world(recording: dict[str, Any]) -> tuple[World, dict[str, Any], str]:
    """A finished batch run whose output is the recording's production outcome,
    with NO ledger row written -- as every run before PR-Z."""
    world = World()
    plan = world.plan(recording["snapshot_rows"])
    run, _preparation, _ = world.finish_batch(plan, production_result(recording),
                                              write_ledger=False)
    assert world.ledger() == {}
    return world, plan, run


def ledger_by_record(world: World, plan: dict[str, Any], records) -> dict[str, str]:
    ledger = world.ledger()
    return {record: ledger[world.key_of(record, plan["snapshot_key"])]["status"]
            for record in records}


def test_the_backfill_reproduces_aa63369b():
    world, plan, run = recorded_world(AA)
    report = backfill(world.repository)
    assert report.as_record()["runs_recorded"] == 1
    assert report.by_run[run] == {ENRICHED: 8, UNRESOLVED_AMBIGUOUS: 4}
    assert ledger_by_record(world, plan, (*AA_RESOLVED, *AA_GROUP_ROWS)) == {
        **{record: ENRICHED for record in AA_RESOLVED},
        **{record: UNRESOLVED_AMBIGUOUS for record in AA_GROUP_ROWS}}
    assert len(world.ledger()) == 10              # 8 variants + 2 duplicate groups
    # Identical to what the finalize path writes.
    live, live_plan, _ = aa_world()
    assert {world.key_of(r, plan["snapshot_key"]): world.ledger()[
                world.key_of(r, plan["snapshot_key"])]["status"]
            for r in (*AA_RESOLVED, *AA_GROUP_ROWS)} == \
        {live.key_of(r, live_plan["snapshot_key"]): live.ledger()[
            live.key_of(r, live_plan["snapshot_key"])]["status"]
         for r in (*AA_RESOLVED, *AA_GROUP_ROWS)}
    # A second backfill changes nothing.
    before = world.ledger()
    backfill(world.repository)
    assert world.ledger() == before


def test_the_backfill_reproduces_6825eb96():
    resolved = ("37096", "37098", "37254", "37291", "37293", "37316", "37363", "37425")
    world, plan, run = recorded_world(GATE0)
    report = backfill(world.repository)
    assert report.by_run[run] == {ENRICHED: 8, UNRESOLVED_AMBIGUOUS: 2}
    assert ledger_by_record(world, plan, (*resolved, "37350", "37439")) == {
        **{record: ENRICHED for record in resolved},
        "37350": UNRESOLVED_AMBIGUOUS, "37439": UNRESOLVED_AMBIGUOUS}
    # 8 variants (the placeholder 37363 included: old data holds it) + ONE
    # duplicate group, asked twice (t04, t05).
    assert len(world.ledger()) == 9


def test_both_runs_share_the_replay_37350_37439_key_across_their_snapshots():
    # The replay stand-ins are true duplicates (identical content minus `_id`).
    world = World()
    gate0 = world.plan(GATE0["snapshot_rows"])
    aa = world.plan(AA["snapshot_rows"])
    assert gate0["snapshot_key"] != aa["snapshot_key"]
    assert world.key_of("37350", gate0["snapshot_key"]) == \
        world.key_of("37439", aa["snapshot_key"]) == world.key_of("37350", aa["snapshot_key"])


def test_the_backfill_script_prints_one_report():
    from scripts.catalog.backfill_variant_coverage import main  # noqa: PLC0415

    world, _plan, run = recorded_world(AA)
    import contextlib
    import io
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert main(["--dry-run"], repository=world.repository) == 0
    report = json.loads(out.getvalue())
    assert report["dry_run"] is True and report["runs_recorded"] == 1
    assert report["by_run"][run] == {ENRICHED: 8, UNRESOLVED_AMBIGUOUS: 4}
    assert world.ledger() == {}                   # a dry run writes nothing


# =============================================================================
# the real worker: the finalize path writes the ledger, and a failed write
# changes nothing about the run
# =============================================================================

def _worker_batch(monkeypatch):
    import backend.worker.main as worker_main
    from backend.catalog.execution import (CATALOG_EXECUTION_FLAG, CATALOG_PROMOTION_FLAG,
                                           GOVERNMENT_READ_FLAG)
    from test_government_placeholder_rows import register_rows
    from test_swarm_v2_smoke_offline import (USER, FakeKimiCompletions, build_repo,
                                             patch_client, swarm_env)

    swarm_env(monkeypatch, **{CATALOG_EXECUTION_FLAG: "true", GOVERNMENT_READ_FLAG: "true",
                              CATALOG_PROMOTION_FLAG: "false"})
    repository, conversation_id = build_repo()
    plan = seed_prepared_plan(repository, user_id=USER, conversation_id=conversation_id,
                              units=("toyota",), max_items=20, batch_size=20,
                              records=register_rows())
    run_id = UUID(start_batch_run(repository, plan)["run"]["id"])
    patch_client(monkeypatch, FakeKimiCompletions())
    return worker_main, repository, run_id


def test_the_worker_writes_the_ledger_after_the_run_is_finalized(monkeypatch):
    worker_main, repository, run_id = _worker_batch(monkeypatch)
    assert worker_main.execute_run(run_id, repository) == 0
    run = repository.get_run(run_id)
    # The offline completions settle no candidate: the run finishes with no
    # vehicle, so each of the four handed variants (the placeholder is never
    # handed) is recorded `failed` -- and will be queued again.
    assert run["status"] == "partial_success"
    rows = list(repository.catalog_variant_coverage.values())
    assert len(rows) == 4
    assert {row["status"] for row in rows} == {FAILED}
    assert {row["last_run_id"] for row in rows} == {str(run_id)}
    handed = {item["candidate_id"] for item in repository.work_scope_batch_for_run(run_id)["items"]
              if item["commercial_model"] != "11111"}
    candidates = {row["id"]: row for row in repository.catalog_candidates.values()}
    records = {row["id"]: row for row in repository.catalog_raw_records.values()}
    assert {row["variant_identity_key"] for row in rows} == \
        {candidate_identity_key(candidates[candidate],
                                records[candidates[candidate]["raw_record_id"]]["payload"])
         for candidate in handed}
    # The worker claimed all four before its first model call, and the
    # settlement released every claim with the ledger write.
    assert repository.catalog_variant_reservations == {}


def test_a_failed_ledger_write_in_the_worker_leaves_the_run_exactly_as_finalized(
        monkeypatch, capsys):
    worker_main, repository, run_id = _worker_batch(monkeypatch)

    def refuse(*_args, **_kwargs):
        raise AppError("REPOSITORY_ERROR", "coverage ledger call failed", 502)

    monkeypatch.setattr(repository, "record_catalog_variant_coverage", refuse)
    assert worker_main.execute_run(run_id, repository) == 0
    assert repository.get_run(run_id)["status"] == "partial_success"
    assert repository.catalog_variant_coverage == {}
    assert (f"catalog coverage ledger write failed: run_id={run_id} "
            "exception_class=AppError") in capsys.readouterr().out
    # PR-Z (strengthened): the finished run's claims were NOT released with
    # the failed write -- they keep blocking every other run ...
    claims = repository.catalog_variant_reservations
    assert len(claims) == 4 and {row["run_id"] for row in claims.values()} == {str(run_id)}
    assert [row["run_id"] for row in repository.catalog_variant_reservations_settling()] == \
        [str(run_id)]
    # ... until the automatic reconciliation settles the run from its durable
    # output: the same ledger the finalize path would have written.
    assert reconcile_pending_settlements(repository) == {"settled": 1, "deferred": 0}
    assert repository.catalog_variant_reservations == {}
    assert sorted(row["status"] for row in repository.catalog_variant_coverage.values()) == \
        [FAILED] * 4
    assert {row["last_run_id"] for row in repository.catalog_variant_coverage.values()} == \
        {str(run_id)}


# =============================================================================
# The paid-work claim: two plans, crashes and stale attempts never pay twice
# =============================================================================

from datetime import UTC, datetime, timedelta  # noqa: E402

from backend.catalog.coverage import (RESERVATION_TAKEOVER_GRACE_SECONDS,  # noqa: E402
                                      reconcile_pending_settlements, settle_run)
from backend.catalog.government.preparation import EXCLUDED_RESERVED_BY_ANOTHER_RUN  # noqa: E402


def _expire(world: World, run: str, *, seconds_ago: int) -> None:
    """The run's lease lapsed `seconds_ago` (its worker died, nothing renewed it)."""
    world.repository.runs[run]["lease_expires_at"] = \
        (datetime.now(UTC) - timedelta(seconds=seconds_ago)).isoformat()


def _claims(world: World) -> dict[str, str]:
    """variant identity key -> owning run, for every live claim row."""
    return {key: row["run_id"] for (key, _level), row
            in world.repository.catalog_variant_reservations.items()}


def _fail(world: World, run: str, lease: dict[str, Any]) -> None:
    RunFinalizer(world.repository, UUID(run), "swarm_v2", lease).finalize(
        TerminalClaim.failure("swarm_v2", "SWARM_V2_EXECUTION_FAILED", "failed"))


def test_two_plans_racing_for_one_variant_only_one_may_pay():
    world = World()
    first, second = world.plan(AA["snapshot_rows"]), world.plan(AA["snapshot_rows"])
    run_a, lease_a = world.start(first)
    run_b, lease_b = world.start(second)
    # Both plans queued all twelve: neither is settled in the ledger.
    assert len(world.queued_records(first)) == len(world.queued_records(second)) == 12
    a = prepare_government_work(world.repository, run_id=run_a, lease=lease_a)
    assert len(a.queue) == 12 and a.excluded == ()
    assert set(_claims(world).values()) == {run_a} and len(_claims(world)) == 10
    # B reaches preparation while A is still paying: B pays for nothing.
    with pytest.raises(GovernmentPreparationError) as refused:
        prepare_government_work(world.repository, run_id=run_b, lease=lease_b)
    assert refused.value.code == "GOVERNMENT_BATCH_RESERVED"
    assert set(_claims(world).values()) == {run_a}          # B took no claim
    # Claiming again is idempotent: the same answer, the same owner.
    again = prepare_government_work(world.repository, run_id=run_a, lease=lease_a)
    assert [item.candidate_id for item in again.queue] == [item.candidate_id for item in a.queue]


def test_a_second_plan_keeps_only_what_no_other_run_owns():
    world = World()
    first = world.plan(AA["snapshot_rows"])
    others = extra_rows(3)
    second = world.plan([*AA["snapshot_rows"], *others])
    run_a, lease_a = world.start(first)
    prepare_government_work(world.repository, run_id=run_a, lease=lease_a)
    run_b, lease_b = world.start(second)
    b = prepare_government_work(world.repository, run_id=run_b, lease=lease_b)
    assert sorted(item.commercial_model for item in b.queue) == \
        sorted(row["kinuy_mishari"] for row in others)
    assert b.excluded_reserved == 12
    assert {reason for _, reason in b.excluded} == {EXCLUDED_RESERVED_BY_ANOTHER_RUN}
    assert b.as_artifact()["excluded_reserved"] == 12


def test_different_variants_are_claimed_independently():
    world = World()
    run_a, lease_a = world.start(world.plan(extra_rows(3, start=70_000)))
    run_b, lease_b = world.start(world.plan(extra_rows(3, start=80_000, year=2021)))
    a = prepare_government_work(world.repository, run_id=run_a, lease=lease_a)
    b = prepare_government_work(world.repository, run_id=run_b, lease=lease_b)
    assert (len(a.queue), len(b.queue)) == (3, 3)
    assert sorted(_claims(world).values()) == sorted([run_a] * 3 + [run_b] * 3)


def test_a_claim_at_another_level_does_not_collide():
    world = World()
    plan = world.plan(AA["snapshot_rows"])
    other, _ = world.start(world.plan(extra_rows(1)))
    key = world.key_of("37254", plan["snapshot_key"])
    # A LIVE run's claim on the same variant at a different level of work.
    world.repository.catalog_variant_reservations[(key, "web")] = {
        "id": str(uuid4()), "variant_identity_key": key, "level": "web", "run_id": other,
        "attempt": 1, "batch_id": "x", "candidate_id": "x", "content_sha256": "c" * 64,
        "previous_run_id": None, "reserved_at": "", "updated_at": ""}
    run, lease = world.start(plan)
    prepared = prepare_government_work(world.repository, run_id=run, lease=lease)
    assert len(prepared.queue) == 12
    assert world.repository.catalog_variant_reservations[(key, "register")]["run_id"] == run
    assert world.repository.catalog_variant_reservations[(key, "web")]["run_id"] == other


def test_a_stale_attempt_can_neither_claim_settle_nor_steal():
    world = World()
    plan = world.plan(AA["snapshot_rows"])
    run, first = world.start(plan)
    ids = [item["candidate_id"]
           for item in world.repository.work_scope_batch_for_run(UUID(run))["items"]]
    world.repository.acquire_catalog_variant_reservations(run, "register", ids, **first)
    # The first attempt's worker dies; a second attempt reclaims the run.
    _expire(world, run, seconds_ago=5)
    second = world.claim(run, worker="worker-retry")
    assert second["attempt"] == first["attempt"] + 1
    for call in (lambda: world.repository.acquire_catalog_variant_reservations(
                     run, "register", ids, **first),
                 lambda: world.repository.record_catalog_variant_coverage(
                     run, "register", [], **first)):
        with pytest.raises(AppError) as stale:
            call()
        assert stale.value.code == "RUN_LEASE_LOST"
    # The current attempt takes over its own run's claims ...
    answer = world.repository.acquire_catalog_variant_reservations(run, "register", ids, **second)
    assert {item["decision"] for item in answer["items"]} == {"reserved"}
    assert {row["attempt"] for row in world.repository.catalog_variant_reservations.values()} \
        == {second["attempt"]}
    # ... and another plan's run cannot steal them while it is live.
    other, other_lease = world.start(world.plan(AA["snapshot_rows"]))
    with pytest.raises(GovernmentPreparationError) as refused:
        prepare_government_work(world.repository, run_id=other, lease=other_lease)
    assert refused.value.code == "GOVERNMENT_BATCH_RESERVED"


def test_an_interrupted_run_becomes_retryable_by_one_explicit_rule():
    world = World()
    run_a, lease_a = world.start(world.plan(AA["snapshot_rows"]))
    prepare_government_work(world.repository, run_id=run_a, lease=lease_a)
    run_b, lease_b = world.start(world.plan(AA["snapshot_rows"]))
    # A's worker died: within the takeover grace the claim still blocks ...
    _expire(world, run_a, seconds_ago=RESERVATION_TAKEOVER_GRACE_SECONDS - 60)
    with pytest.raises(GovernmentPreparationError) as held:
        prepare_government_work(world.repository, run_id=run_b, lease=lease_b)
    assert held.value.code == "GOVERNMENT_BATCH_RESERVED"
    # ... and past it the claim is dead and passes to B, atomically.
    _expire(world, run_a, seconds_ago=RESERVATION_TAKEOVER_GRACE_SECONDS + 60)
    b = prepare_government_work(world.repository, run_id=run_b, lease=lease_b)
    assert len(b.queue) == 12 and set(_claims(world).values()) == {run_b}
    claims = world.repository.catalog_variant_reservations.values()
    assert {row["previous_run_id"] for row in claims} == {run_a}
    # A is reclaimed later: its resumed attempt re-claims before any paid call
    # and is refused, because the paid work is B's now.
    checkpoint = {"phase": PREPARATION_PHASE, "artifacts": {ARTIFACT_KEY: prepare_government_work(
        world.repository, run_id=run_a).as_artifact()}}
    lease_a2 = world.claim(run_a, worker="worker-a-retry")
    with pytest.raises(GovernmentPreparationError) as lost:
        prepare_government_work(world.repository, run_id=run_a, lease=lease_a2,
                                checkpoint=checkpoint)
    assert lost.value.code == "GOVERNMENT_RESERVATION_LOST"
    assert set(_claims(world).values()) == {run_b}


def test_a_failed_run_releases_its_claim_to_the_next_claimant_at_once():
    world = World()
    run_a, lease_a = world.start(world.plan(AA["snapshot_rows"]))
    prepare_government_work(world.repository, run_id=run_a, lease=lease_a)
    _fail(world, run_a, lease_a)
    run_b, lease_b = world.start(world.plan(AA["snapshot_rows"]))
    assert len(prepare_government_work(world.repository, run_id=run_b, lease=lease_b).queue) == 12


def test_a_resumed_attempt_keeps_its_own_claims():
    world = World()
    run, lease = world.start(world.plan(AA["snapshot_rows"]))
    prepared = prepare_government_work(world.repository, run_id=run, lease=lease)
    checkpoint = {"phase": PREPARATION_PHASE, "artifacts": {ARTIFACT_KEY: prepared.as_artifact()}}
    _expire(world, run, seconds_ago=RESERVATION_TAKEOVER_GRACE_SECONDS + 60)
    retry = world.claim(run, worker="worker-retry")
    resumed = prepare_government_work(world.repository, run_id=run, lease=retry,
                                      checkpoint=checkpoint)
    assert resumed.resumed and len(resumed.queue) == 12
    assert set(_claims(world).values()) == {run}


def test_a_finished_run_whose_settlement_failed_still_blocks_and_recovers_automatically():
    # Paid work completed, the run is durably finalized, and the settlement
    # write never lands (the worker dies, or the write fails).
    world = World()
    run_a, _preparation, _ = world.finish_batch(world.plan(AA["snapshot_rows"]),
                                                production_result(AA), write_ledger=False)
    assert world.repository.get_run(run_a)["status"] == "partial_success"
    assert world.ledger() == {} and set(_claims(world).values()) == {run_a}
    assert world.repository.catalog_variant_reservations_settling(limit=50) == [
        {"run_id": run_a, "finished_at": world.repository.get_run(run_a)["finished_at"]}]

    # A second plan's run: its preparation meets A's unsettled claims, settles
    # A from A's own durable output, and then pays for nothing.
    second = world.plan(AA["snapshot_rows"])
    run_b, lease_b = world.start(second)
    with pytest.raises(GovernmentPreparationError) as refused:
        prepare_government_work(world.repository, run_id=run_b, lease=lease_b)
    assert refused.value.code == "GOVERNMENT_BATCH_ALREADY_COVERED"
    assert _claims(world) == {}
    assert world.repository.catalog_variant_reservations_settling(limit=50) == []
    # The ledger is exactly what a normal settlement writes.
    normal, normal_plan, _ = aa_world()
    fields = ("status", "content_sha256", "level", "vocabulary_version")
    assert {key: tuple(row[name] for name in fields) for key, row in world.ledger().items()} == \
        {key: tuple(row[name] for name in fields) for key, row in normal.ledger().items()}
    assert {row["last_run_id"] for row in world.ledger().values()} == {run_a}


def test_while_settlement_keeps_failing_the_finished_run_keeps_blocking():
    world = World()
    run_a, _preparation, _ = world.finish_batch(world.plan(AA["snapshot_rows"]),
                                                production_result(AA), write_ledger=False)

    class NoSettlement:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            if name == "rebuild_catalog_variant_coverage":
                def refuse(*_args, **_kwargs):
                    raise AppError("REPOSITORY_ERROR", "coverage ledger call failed", 502)
                return refuse
            return getattr(self._inner, name)

    run_b, lease_b = world.start(world.plan(AA["snapshot_rows"]))
    with pytest.raises(GovernmentPreparationError) as refused:
        prepare_government_work(NoSettlement(world.repository), run_id=run_b, lease=lease_b)
    assert refused.value.code == "GOVERNMENT_BATCH_RESERVED"
    assert set(_claims(world).values()) == {run_a} and world.ledger() == {}
    # Even past any takeover grace: a FINISHED run's claim never goes dead.
    _expire(world, run_a, seconds_ago=RESERVATION_TAKEOVER_GRACE_SECONDS * 10)
    with pytest.raises(GovernmentPreparationError):
        prepare_government_work(NoSettlement(world.repository), run_id=run_b, lease=lease_b)
    assert set(_claims(world).values()) == {run_a}


def test_the_queue_build_sweep_settles_unsettled_runs_and_is_idempotent():
    world = World()
    run_a, _preparation, _ = world.finish_batch(world.plan(AA["snapshot_rows"]),
                                                production_result(AA), write_ledger=False)
    assert reconcile_pending_settlements(world.repository) == {"settled": 1, "deferred": 0}
    settled = world.ledger()
    assert len(settled) == 10 and _claims(world) == {}
    # Converges: sweeping, settling and settling again change nothing.
    assert reconcile_pending_settlements(world.repository) == {"settled": 0, "deferred": 0}
    assert settle_run(world.repository, run_a) is True
    assert world.ledger() == settled
    # The next queue build is built from that ledger.
    assert world.queued_records(world.plan(AA["snapshot_rows"])) == []


def test_the_operator_preparation_settles_before_it_builds_the_queue(monkeypatch):
    from backend.catalog.scope import preparation as scope_preparation

    calls: list[str] = []
    monkeypatch.setattr(scope_preparation.catalog_coverage, "reconcile_pending_settlements",
                        lambda repository, **_kwargs: calls.append("reconcile") or {})

    class Stop(Exception):
        pass

    def stop(*_args, **_kwargs):
        calls.append("capture")
        raise Stop()

    monkeypatch.setattr(scope_preparation, "read_prepared_revision",
                        lambda *_args, **_kwargs: SimpleNamespace(units=("toyota",)))
    monkeypatch.setattr(scope_preparation, "capture_unit", stop)
    with pytest.raises(Stop):
        scope_preparation.prepare_work_scope(object(), SimpleNamespace(run_id="r"), client=None,
                                             work_scope_id=str(uuid4()), revision=1,
                                             digest="a" * 64)
    assert calls == ["reconcile", "capture"]


# =============================================================================
# PR-Z2: the key carries the Government's registration identifiers; a
# duplicate is ONLY identical content; anything else sharing a key collides
# =============================================================================

#: Five REAL production register rows (tests/fixtures): the pairs the /1 key
#: collapsed -- 37350 / 37439 and 37309 / 37345 -- are different vehicles.
REAL = json.loads((FIXTURES / "production_4runner_2026_rows.json").read_text())
REAL_ROWS = {str(row["_id"]): row for row in REAL["rows"]}
REAL_PAIRS = (("37350", "37439"), ("37309", "37345"))
REAL_PAIR_ROWS = tuple(record for pair in REAL_PAIRS for record in pair)


def real_aa_rows() -> list[dict[str, Any]]:
    """aa63369b's snapshot with the five rows production actually holds in
    place of their reconstructed stand-ins."""
    return [copy.deepcopy(REAL_ROWS.get(str(row["_id"]), row)) for row in AA["snapshot_rows"]]


def _without_id(row: dict[str, Any]) -> dict[str, Any]:
    return {name: value for name, value in row.items() if name != "_id"}


def snapshot_candidates(world: World, snapshot_key: str) -> dict[str, tuple[dict, dict]]:
    """register id -> (candidate row, raw payload) of one snapshot."""
    snapshot = next(row["id"] for row in world.repository.catalog_snapshots.values()
                    if row["snapshot_key"] == snapshot_key)
    records = {row["id"]: row for row in world.repository.catalog_raw_records.values()}
    return {str(records[row["raw_record_id"]]["upstream_record_id"]):
            (row, records[row["raw_record_id"]]["payload"])
            for row in world.repository.catalog_candidates.values()
            if row["snapshot_id"] == snapshot}


def tool_recording(world: World, plan: dict[str, Any]) -> dict[str, Any]:
    """aa63369b's shape -- ONE resolve_variant task per distinct stated identity
    -- answered by the REAL Government tool over the plan's own snapshot."""
    tool = GovernmentVehicleTool(world.repository, snapshot_key=plan["snapshot_key"])
    asked: dict[str, dict[str, Any]] = {}
    for _record, (candidate, _payload) in sorted(snapshot_candidates(
            world, plan["snapshot_key"]).items()):
        arguments = {"manufacturer": candidate["manufacturer"],
                     "commercial_model": candidate["commercial_model"],
                     "model_year": candidate["model_year_start"]}
        for name, field in (("trim", "trim"), ("official_model_code", "official_model_code")):
            if candidate.get(field):
                arguments[name] = candidate[field]
        asked.setdefault(json.dumps(arguments, sort_keys=True, ensure_ascii=False), arguments)
    return {"tool_results": [
        {"task_id": f"t{index:02d}", "call_id": "c1", "tool": GOVERNMENT_TOOL_NAME,
         "operation": "resolve_variant", "arguments": arguments,
         "result": tool.execute(ToolContext(), "resolve_variant", arguments)}
        for index, arguments in enumerate(asked.values(), start=1)]}


def test_z2_the_register_codes_give_each_real_row_its_own_key():
    world = World()
    plan = world.plan(real_aa_rows())
    candidates = snapshot_candidates(world, plan["snapshot_key"])
    # The reviewed normalization states exactly what production stored.
    for record, stored in REAL["production_candidates"].items():
        candidate, _payload = candidates[record]
        assert {name: candidate[name] for name in stored} == stored, record
    keys = {record: candidate_identity_key(*candidates[record]) for record in REAL_ROWS}
    assert len(set(keys.values())) == 5
    # Every one of them is a different vehicle ...
    assert len({json.dumps(_without_id(row), sort_keys=True) for row in REAL["rows"]}) == 5
    # ... which the identity columns alone (/1's rendering, codes absent) cannot see.
    collapsed = {record: variant_identity_key(
        candidate["manufacturer"], candidate["commercial_model"], candidate["model_year_start"],
        candidate["model_year_end"], candidate["official_model_code"], candidate["trim"],
        None, None, None, candidate["identity_dimensions"])
        for record, (candidate, _payload) in candidates.items() if record in REAL_ROWS}
    assert len(set(collapsed.values())) == 3
    for left, right in REAL_PAIRS:
        assert collapsed[left] == collapsed[right] and keys[left] != keys[right]
        assert REAL_ROWS[left]["degem_cd"] != REAL_ROWS[right]["degem_cd"]


def test_z2_aa63369b_backfill_then_fresh_preparations_queue_none_and_never_ping_pong():
    world = World()
    rows = real_aa_rows()
    # A plan prepared BEFORE the run finished: its queue holds all twelve.
    early = world.plan(rows)
    assert len(world.queued_records(early)) == 12
    plan = world.plan(rows)
    result = production_result(tool_recording(world, plan))
    run, _preparation, _ = world.finish_batch(plan, result, write_ledger=False)
    assert world.ledger() == {}
    report = backfill(world.repository)
    assert report.as_record()["runs_recorded"] == 1
    assert report.by_run[run] == {ENRICHED: 8, UNRESOLVED_AMBIGUOUS: 4}
    ledger = world.ledger()
    # Twelve rows, twelve variants: 8 resolved + the 4 rows of the two pairs,
    # each under its OWN key (the /1 key gave 10: the pairs collapsed to 2).
    assert len(ledger) == 12
    assert all(row["reason_code"] is None for row in ledger.values())
    assert ledger_by_record(world, plan, REAL_ROWS) == {
        "37425": ENRICHED, **{record: UNRESOLVED_AMBIGUOUS for record in REAL_PAIR_ROWS}}

    # A fresh preparation queues none of them; a second one, neither; and
    # neither changes a byte of the ledger.
    for _attempt in range(2):
        revision = world.plan(rows)
        assert world.queued_records(revision) == []
        unit = world.unit_coverage(revision)
        assert (unit["excluded_already_enriched"], unit["excluded_known_unresolved"]) == (8, 4)
        assert world.ledger() == ledger
    # The early plan's run preparation -- twice -- is refused before any paid
    # call, and leaves the ledger as it was.
    started = start_batch_run(world.repository, early)["run"]["id"]
    for _attempt in range(2):
        with pytest.raises(GovernmentPreparationError) as refused:
            prepare_government_work(world.repository, run_id=started)
        assert refused.value.code == "GOVERNMENT_BATCH_ALREADY_COVERED"
        assert world.ledger() == ledger
    # A second backfill changes nothing either.
    backfill(world.repository)
    assert world.ledger() == ledger


def test_z2_settle_keys_never_picks_between_different_contents():
    c1, c2, c3 = "1" * 64, "2" * 64, "3" * 64
    # One key, one content: the strongest status.
    assert coverage.settle_keys([("k", c1, FAILED), ("k", c1, ENRICHED)], {"k": [c1]}) == {
        "k": coverage.KeySettlement(ENRICHED, c1)}
    # A true duplicate group: still one content.
    assert coverage.settle_keys([("k", c1, UNRESOLVED_AMBIGUOUS)], {"k": [c1, c1]})["k"] \
        == coverage.KeySettlement(UNRESOLVED_AMBIGUOUS, c1)
    # Two contents among the entries: a collision, whatever the statuses.
    collision = coverage.KeySettlement(FAILED, coverage.collision_content_sha256([c1, c2]),
                                       coverage.KEY_COLLISION)
    assert coverage.settle_keys([("k", c1, ENRICHED), ("k", c2, PENDING)], {}) == \
        {"k": collision}
    # A second content elsewhere in the snapshot collides as well.
    assert coverage.settle_keys([("k", c2, ENRICHED)], {"k": [c1, c2]}) == {"k": collision}
    # The collision content is order-free, and names every content involved.
    assert coverage.collision_content_sha256([c2, c1, c2]) == \
        coverage.collision_content_sha256([c1, c2]) != \
        coverage.collision_content_sha256([c1, c2, c3])
    # Another key is untouched by it.
    assert coverage.settle_keys([("k", c1, ENRICHED), ("j", c3, ENRICHED)],
                                {"k": [c1, c2], "j": [c3]})["j"] == \
        coverage.KeySettlement(ENRICHED, c3)


def _collapsed_codes(monkeypatch) -> None:
    """A key WITHOUT the registration identifiers -- /1's collapse -- so the real
    pairs share a key while their content differs."""
    monkeypatch.setattr(coverage, "register_codes", lambda payload: (None, None, None))


def test_z2_a_key_collision_is_recorded_failed_and_the_run_is_untouched(monkeypatch):
    _collapsed_codes(monkeypatch)
    world = World()
    rows = real_aa_rows()
    plan = world.plan(rows)
    result = production_result(tool_recording(world, plan))
    run, preparation, answer = world.finish_batch(plan, result)
    assert answer is not None and answer["collisions"] == 2
    ledger = world.ledger()
    candidates = snapshot_candidates(world, plan["snapshot_key"])
    for left, right in REAL_PAIRS:
        key = candidate_identity_key(*candidates[left])
        assert key == candidate_identity_key(*candidates[right])
        row = ledger[key]
        # Never one of the two picked: failed, with the reason and every content.
        assert (row["status"], row["reason_code"]) == (FAILED, coverage.KEY_COLLISION)
        assert row["content_sha256"] == coverage.collision_content_sha256(
            [variant_content_sha256(candidates[record][1]) for record in (left, right)])
    assert world.repository.get_run(run)["status"] == "partial_success"
    assert world.repository.get_run(run)["output"] == result
    # Stable: settling the same run again changes nothing -- no ping-pong.
    lease = {key: world.repository.get_run(run)[key]
             for key in ("worker_id", "attempt", "lease_token")}
    again = record_run_coverage(world.repository, run, preparation, result, lease)
    assert again is not None and again["written"] == 0 and again["collisions"] == 2
    assert world.ledger() == ledger
    # A failed variant is queued again: none of the colliding rows is excluded.
    revision = world.plan(rows)
    assert world.queued_records(revision) == sorted(REAL_PAIR_ROWS)


def test_z2_a_collision_with_a_row_the_run_never_named_still_collides(monkeypatch):
    _collapsed_codes(monkeypatch)
    world = World()
    plan = world.plan(real_aa_rows())
    run, _preparation, _ = world.finish_batch(
        plan, production_result(tool_recording(world, plan)), write_ledger=False)
    candidates = snapshot_candidates(world, plan["snapshot_key"])
    # ONE entry, for 37350 only; 37439 shares its key in the same snapshot.
    answer = world.repository.rebuild_catalog_variant_coverage(
        run, "register", [{"candidate_id": candidates["37350"][0]["id"], "status": ENRICHED}])
    assert (answer["written"], answer["collisions"]) == (1, 1)
    (row,) = world.ledger().values()
    assert (row["status"], row["reason_code"]) == (FAILED, coverage.KEY_COLLISION)


def test_z2_true_duplicates_settle_as_one_variant_without_a_collision():
    world = World()
    real = REAL_ROWS["37350"]
    twin = {**copy.deepcopy(real), "_id": 90_350}                  # identical minus _id
    plan = world.plan([copy.deepcopy(real), twin])
    run, _preparation, _ = world.finish_batch(
        plan, production_result(tool_recording(world, plan)), write_ledger=False)
    candidates = snapshot_candidates(world, plan["snapshot_key"])
    assert candidate_identity_key(*candidates["37350"]) == \
        candidate_identity_key(*candidates["90350"])
    answer = world.repository.rebuild_catalog_variant_coverage(
        run, "register", [{"candidate_id": candidates["37350"][0]["id"], "status": ENRICHED},
                          {"candidate_id": candidates["90350"][0]["id"],
                           "status": UNRESOLVED_AMBIGUOUS}])
    assert (answer["written"], answer["collisions"]) == (1, 0)
    (row,) = world.ledger().values()
    assert (row["status"], row["reason_code"]) == (ENRICHED, None)
    assert row["content_sha256"] == variant_content_sha256(real)


def _replay_snapshots() -> list[tuple[str, list[dict[str, Any]]]]:
    found = []
    for path in sorted(REPLAY.glob("*/manifest.json")):
        rows = json.loads(path.read_text()).get("snapshot_rows") or []
        if rows:
            found.append((path.parent.name, rows))
    found.append(("production-4runner-2026", [copy.deepcopy(row) for row in REAL["rows"]]))
    return found


@pytest.mark.parametrize("name,rows", _replay_snapshots(), ids=lambda value: value
                         if isinstance(value, str) else None)
def test_z2_property_rows_that_share_a_key_have_identical_content(name, rows):
    """Z2-5: over EVERY replay fixture's snapshot (and the real rows), two rows
    with the same variant identity key have identical content minus `_id`."""
    world = World()
    plan = world.plan(rows)
    by_key: dict[str, list[dict[str, Any]]] = {}
    for _record, (candidate, payload) in snapshot_candidates(world, plan["snapshot_key"]).items():
        by_key.setdefault(candidate_identity_key(candidate, payload), []).append(payload)
    assert by_key, name
    for key, payloads in by_key.items():
        contents = {json.dumps(_without_id(payload), sort_keys=True, ensure_ascii=False)
                    for payload in payloads}
        assert len(contents) == 1, (name, key, len(payloads))

"""PR-Z3: `resolve_variant` can use the register's own identifiers.

With the Government's registration identifiers -- `tozeret_cd`, `degem_cd`,
`sug_degem` -- every Toyota row of the production snapshot has its own
identity (PR-Z2), yet `resolve_variant` narrowed only by marque, model, year,
trim, official model code and dimensions, so rows differing only in those
codes always came back ambiguous. aa63369b paid for two such "ambiguities":
LIMITED 37350 / 37439 (degem_cd 758 / 839) and TRAILHUNTER 37309 / 37345
(degem_cd 722 / 753).

The codes are not a caller's choice. Each queue item IS one register row and
the server reads its codes; a call that states codes is answered only when
they name a row the server handed the run. Every row here is landed through
the real scoped ingestion and read through the real query layer and tool.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from backend.catalog import coverage
from backend.catalog.government.evidence import GovernmentVariantEvidenceMapper
from backend.catalog.government.preparation import (REGISTER_CODE_RULE, GovernmentPreparation,
                                                    GovernmentPreparationError,
                                                    GovernmentWorkItem,
                                                    prepare_government_work)
from backend.catalog.government.query import MAX_RESOLUTION_MATCHES, GovernmentCatalogQuery
from backend.catalog.result.assembler import VehicleCatalogResultAssembler
from backend.engines.swarm_v2.contracts import PlannedToolCall
from backend.engines.swarm_v2.resolution import candidate_outcome
from backend.engines.swarm_v2.validation import PlanValidator
from backend.tools import ToolContext, ToolError, ToolRegistry
from backend.tools.government_vehicle import (GOVERNMENT_TOOL_NAME, GOVERNMENT_TOOL_SCOPE,
                                              REGISTER_CODE_NOT_IN_BATCH,
                                              GovernmentVehicleTool, handed_register_rows,
                                              register_code_plan_rule)
from test_catalog_variant_coverage import AA, production_result
from test_government_placeholder_rows import seeded_batch

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REPLAY = Path(__file__).resolve().parent / "replay"
REAL = json.loads((FIXTURES / "production_4runner_2026_rows.json").read_text())
REAL_ROWS = {str(row["_id"]): row for row in REAL["rows"]}
REAL_PAIRS = (("37350", "37439"), ("37309", "37345"))
CODE_NAMES = ("register_manufacturer_code", "register_model_code", "vehicle_type_code")
CONTEXT = ToolContext(scopes=frozenset({GOVERNMENT_TOOL_SCOPE}))


# =============================================================================
# helpers
# =============================================================================

def real_aa_rows() -> list[dict[str, Any]]:
    """aa63369b's twelve register rows, with the five production actually holds
    in place of their reconstructed stand-ins."""
    return [copy.deepcopy(REAL_ROWS.get(str(row["_id"]), row)) for row in AA["snapshot_rows"]]


class Batch:
    """ONE prepared batch run over `rows`: the repository, its preparation, and
    the tool bound to the run's own handed queue -- as the worker wires it."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.repository, self.run = seeded_batch([copy.deepcopy(row) for row in rows])
        self.preparation = prepare_government_work(self.repository, run_id=self.run)
        self.handed = handed_register_rows(self.preparation.queue)
        self.tool = GovernmentVehicleTool(self.repository,
                                          snapshot_key=self.preparation.snapshot_key,
                                          handed_rows=self.handed)
        self.registry = ToolRegistry([self.tool])
        records = {row["id"]: row for row in self.repository.catalog_raw_records.values()}
        self.record_of = {candidate["id"]: str(records[candidate["raw_record_id"]]
                                               ["upstream_record_id"])
                          for candidate in self.repository.catalog_candidates.values()}

    def item(self, record_id: str) -> GovernmentWorkItem:
        (item,) = [item for item in self.preparation.queue
                   if self.record_of[item.candidate_id] == record_id]
        return item

    def resolve(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Through the Registry: input and output validated against the schema."""
        return dict(self.registry.execute(GOVERNMENT_TOOL_NAME, "resolve_variant", CONTEXT,
                                          arguments))


def arguments_of(item: GovernmentWorkItem, *, codes: bool) -> dict[str, Any]:
    """The call the Commander is told to plan for one handed item."""
    arguments = {"manufacturer": item.manufacturer, "commercial_model": item.commercial_model,
                 "model_year": item.model_year_start}
    if item.trim:
        arguments["trim"] = item.trim
    if item.official_model_code:
        arguments["official_model_code"] = item.official_model_code
    if codes:
        arguments.update(item.register_codes)
    return arguments


def rows_of(result: dict[str, Any]) -> list[str]:
    return sorted(variant["upstream_record_id"] for variant in result["variants"])


# =============================================================================
# Z3-7: the real production rows
# =============================================================================

def test_each_real_row_resolves_uniquely_when_its_codes_are_stated():
    batch = Batch(REAL["rows"])
    assert len(batch.preparation.queue) == 5
    for record in sorted(REAL_ROWS):
        item = batch.item(record)
        # The item carries its own row's codes, verbatim `payload->>'field'`.
        assert item.register_codes == dict(zip(CODE_NAMES, coverage.register_codes(
            REAL_ROWS[record])))
        result = batch.resolve(arguments_of(item, codes=True))
        assert (result["resolved"], result["ambiguous"], result["match_count"]) == \
            (True, False, 1), record
        assert rows_of(result) == [record]
        # Z3-5: the identity projection names the exact registration.
        source = result["source_record"]
        assert (source["tozeret_cd"], source["degem_cd"], source["sug_degem"]) == \
            coverage.register_codes(REAL_ROWS[record])
        # P32 (PR-L1b): the raw absence, read from the row itself.
        assert result["register_unstated"] == [name for name in ("ramat_gimur", "delek_cd")
                                               if REAL_ROWS[record].get(name) in (None, "")]
        assert "distinguishing" not in result


def test_without_codes_the_real_pairs_stay_ambiguous_exactly_as_before():
    batch = Batch(REAL["rows"])
    for left, right in REAL_PAIRS:
        result = batch.resolve(arguments_of(batch.item(left), codes=False))
        assert (result["resolved"], result["ambiguous"], result["match_count"]) == \
            (False, True, 2)
        assert rows_of(result) == sorted((left, right))
        assert "source_record" not in result          # nothing is quoted
        # Z3-4: per match, only what tells the matches apart.
        # In the page's own order; compared here by register id.
        assert sorted(result["distinguishing"],
                      key=lambda entry: entry["upstream_record_id"]) == [
            {"upstream_record_id": record, "tozeret_cd": "413",
             "degem_cd": str(REAL_ROWS[record]["degem_cd"]), "sug_degem": "P"}
            for record in sorted((left, right))]
    # 37425's identity is unique without codes, as it always was.
    alone = batch.resolve(arguments_of(batch.item("37425"), codes=False))
    assert (alone["resolved"], alone["match_count"]) == (True, 1)


def test_the_ambiguous_listing_is_bounded_and_quotes_no_identity():
    base = REAL_ROWS["37350"]
    rows = [{**copy.deepcopy(base), "_id": 93_000 + index, "degem_cd": 900 + index}
            for index in range(MAX_RESOLUTION_MATCHES + 3)]
    # 23 rows in the snapshot, 20 of them handed: the ambiguity is over the
    # snapshot's rows, and the listing stops at the resolution bound.
    batch = Batch(rows)
    result = batch.resolve(arguments_of(batch.preparation.queue[0], codes=False))
    assert result["ambiguous"] and result["match_count"] == MAX_RESOLUTION_MATCHES + 3
    assert len(result["distinguishing"]) == len(result["variants"]) == MAX_RESOLUTION_MATCHES
    assert all(set(entry) == {"upstream_record_id", "tozeret_cd", "degem_cd", "sug_degem"}
               for entry in result["distinguishing"])
    assert "source_record" not in result


# =============================================================================
# Z3-2: the server binding
# =============================================================================

def test_codes_that_name_a_row_outside_the_batch_are_refused_before_any_read():
    snapshot_only = {**copy.deepcopy(REAL_ROWS["37439"])}
    batch = Batch([REAL_ROWS["37350"]])
    # 37439 is not in this batch; its codes name a row the run was not handed.
    item = batch.item("37350")
    outside = {**arguments_of(item, codes=True), "register_model_code":
               str(snapshot_only["degem_cd"])}
    reads: list[str] = []
    inner = batch.repository.catalog_candidate_variant_page

    def counted(*args, **kwargs):
        reads.append("page")
        return inner(*args, **kwargs)

    batch.repository.catalog_candidate_variant_page = counted
    with pytest.raises(ToolError) as refused:
        batch.resolve(outside)
    assert refused.value.code == REGISTER_CODE_NOT_IN_BATCH == \
        "GOVERNMENT_REGISTER_CODE_NOT_IN_BATCH"
    # Static: it quotes no code and no row.
    assert "839" not in refused.value.message and "37439" not in refused.value.message
    assert reads == []
    # The handed row's own codes, with ANOTHER identity, are refused too.
    with pytest.raises(ToolError) as other_identity:
        batch.resolve({**arguments_of(item, codes=True), "trim": "SR5"})
    assert other_identity.value.code == REGISTER_CODE_NOT_IN_BATCH
    # A subset of the handed row's own codes is accepted: that row still
    # satisfies every stated filter.
    partial = batch.resolve({**arguments_of(item, codes=False),
                             "register_model_code": item.register_model_code})
    assert rows_of(partial) == ["37350"]


def test_a_tool_bound_to_no_queue_refuses_every_code():
    batch = Batch(REAL["rows"])
    unbound = GovernmentVehicleTool(batch.repository,
                                    snapshot_key=batch.preparation.snapshot_key)
    registry = ToolRegistry([unbound])
    item = batch.item("37350")
    with pytest.raises(ToolError) as refused:
        registry.execute(GOVERNMENT_TOOL_NAME, "resolve_variant", CONTEXT,
                         arguments_of(item, codes=True))
    assert refused.value.code == REGISTER_CODE_NOT_IN_BATCH
    # ... and answers every call without codes exactly as before.
    result = registry.execute(GOVERNMENT_TOOL_NAME, "resolve_variant", CONTEXT,
                              arguments_of(item, codes=False))
    assert result["match_count"] == 2


def test_the_query_filters_in_the_database_in_one_bounded_read():
    batch = Batch(REAL["rows"])
    calls: list[dict[str, Any]] = []
    inner = batch.repository.catalog_candidate_variant_page

    def spy(snapshot_id, **kwargs):
        calls.append(dict(kwargs))
        return inner(snapshot_id, **kwargs)

    batch.repository.catalog_candidate_variant_page = spy
    query = GovernmentCatalogQuery(batch.repository,
                                   snapshot_key=batch.preparation.snapshot_key)
    item = batch.item("37439")
    result = query.resolve_variant(item.manufacturer, item.commercial_model,
                                   item.model_year_start, trim=item.trim,
                                   official_model_code=item.official_model_code,
                                   **item.register_codes)
    assert result.match_count == 1 and result.variant.upstream_record_id == "37439"
    (call,) = calls
    assert call["limit"] == MAX_RESOLUTION_MATCHES
    assert {name: call[name] for name in CODE_NAMES} == item.register_codes


# =============================================================================
# Z3-3: the work items, the rule, and the plan firewall
# =============================================================================

def test_work_items_carry_codes_and_the_commander_gets_the_rule():
    batch = Batch(REAL["rows"])
    context = batch.preparation.work_context({})
    assert context["register_code_rule"] == REGISTER_CODE_RULE
    for entry in context["items"]:
        assert set(CODE_NAMES) <= set(entry)
    # The artifact round-trips, codes included.
    artifact = batch.preparation.as_artifact()
    assert [GovernmentWorkItem.from_record(item) for item in artifact["queue"]] == \
        list(batch.preparation.queue)


@pytest.mark.parametrize("name", sorted(path.parent.name
                                        for path in REPLAY.glob("*/manifest.json")))
def test_a_record_written_before_pr_z3_is_read_back_unchanged(name):
    manifest = json.loads((REPLAY / name / "manifest.json").read_text())
    for record in manifest["preparation"]["queue"]:
        item = GovernmentWorkItem.from_record(record)
        assert item.register_codes == {}
        assert item.as_record() == {key: value for key, value in record.items()
                                    if key != "progress"}
    preparation = GovernmentPreparation(
        snapshot_key="s", snapshot_id="", resource_id="r", upstream_version="v",
        upstream_version_kind="k",
        queue=tuple(GovernmentWorkItem.from_record(item)
                    for item in manifest["preparation"]["queue"]),
        total_candidates=1, bounded=False, resumed=False)
    assert "register_code_rule" not in preparation.work_context({})
    assert handed_register_rows(preparation.queue) == ()


@pytest.mark.parametrize("bad", [758, None, ["758"], {"a": 1}])
def test_a_malformed_code_in_a_record_is_refused(bad):
    record = {"candidate_key": "k", "candidate_id": "c", "manufacturer": "טויוטה",
              "commercial_model": "4RUNNER", "register_model_code": bad}
    with pytest.raises(GovernmentPreparationError):
        GovernmentWorkItem.from_record(record)


def _call(arguments: dict[str, Any], **extra: Any) -> PlannedToolCall:
    return PlannedToolCall(call_id="c1", name=GOVERNMENT_TOOL_NAME,
                           operation="resolve_variant", arguments=arguments, **extra)


def test_the_plan_rule_requires_the_handed_items_codes():
    batch = Batch(REAL["rows"])
    rule = register_code_plan_rule(batch.handed)
    item = batch.item("37350")
    assert rule(_call(arguments_of(item, codes=True))) is None
    assert rule(_call(arguments_of(item, codes=False))) == "REGISTER_CODES_REQUIRED"
    wrong = {**arguments_of(item, codes=True), "register_model_code": "999"}
    assert rule(_call(wrong)) == "REGISTER_CODE_NOT_IN_BATCH"
    # An identity no handed item states is not this rule's business.
    assert rule(_call({"manufacturer": "טויוטה", "commercial_model": "COROLLA",
                       "model_year": 2020})) is None
    # A code a dependency binding supplies is decided by the tool at run time.
    bound = _call(arguments_of(item, codes=False), dependency_bindings=[
        {"argument": "register_model_code", "task_id": "t0", "path": ["code"]}])
    assert rule(bound) is None
    # Another operation is untouched.
    other = PlannedToolCall(call_id="c1", name=GOVERNMENT_TOOL_NAME, operation="get_variants",
                            arguments={"manufacturer": "טויוטה", "commercial_model": "4RUNNER"})
    assert rule(other) is None
    # No handed codes (every preparation recorded before PR-Z3): no rule.
    assert register_code_plan_rule(())(_call(arguments_of(item, codes=False))) is None


def _plan(arguments: dict[str, Any]) -> dict[str, Any]:
    schema = {"type": "object", "properties": {"answer": {"type": "string"}},
              "required": ["answer"], "additionalProperties": False}
    task = {"task_id": "t01", "goal": "resolve one register candidate", "scope": "register",
            "dependencies": [],
            "tools": [{"call_id": "c1", "name": GOVERNMENT_TOOL_NAME,
                       "operation": "resolve_variant", "arguments": arguments,
                       "dependency_bindings": []}],
            "output_schema": schema,
            "evidence": {"minimum_sources": 1, "required_fields": [], "min_confidence": 0.5},
            "priority": 1, "recursion_depth": 0, "estimated_cost_units": 1,
            "completion": {"required_outputs": ["answer"], "evidence_satisfied": True,
                           "allow_partial": False}}
    return {"version": "1", "objective": "o", "graph": {"tasks": [task]},
            "assignments": [{"task_id": "t01", "worker_role": "reader",
                             "context_task_ids": []}],
            "max_replans": 1, "estimated_cost_units": 1}


def test_the_firewall_refuses_a_plan_that_omits_or_invents_codes():
    from backend.engines.swarm_v2.validation import PlanValidationError

    batch = Batch(REAL["rows"])
    item = batch.item("37439")
    descriptors = batch.registry.descriptors()
    bound = PlanValidator(allowed_tools=descriptors,
                          call_rules=(register_code_plan_rule(batch.handed),))
    bound.validate(_plan(arguments_of(item, codes=True)))
    with pytest.raises(PlanValidationError) as missing:
        bound.validate(_plan(arguments_of(item, codes=False)))
    assert missing.value.reason == "REGISTER_CODES_REQUIRED"
    with pytest.raises(PlanValidationError) as invented:
        bound.validate(_plan({**arguments_of(item, codes=True), "register_model_code": "1"}))
    assert invented.value.reason == "REGISTER_CODE_NOT_IN_BATCH"
    # A firewall with no server rule -- the replay posture -- admits it as before.
    PlanValidator(allowed_tools=descriptors).validate(_plan(arguments_of(item, codes=False)))


# =============================================================================
# Z3-5: evidence and the vehicle output name the exact registration
# =============================================================================

def test_the_registration_reaches_the_outcome_and_the_vehicle_but_not_the_facts():
    batch = Batch(REAL["rows"])
    item = batch.item("37439")
    with_codes = batch.resolve(arguments_of(item, codes=True))
    outcome = candidate_outcome(task_id="t01", call_id="c1", tool=GOVERNMENT_TOOL_NAME,
                                operation="resolve_variant",
                                arguments=arguments_of(item, codes=True), result=with_codes)
    assert outcome["registration"] == {"tozeret_cd": "413", "degem_cd": "839",
                                       "sug_degem": "P"}
    view = VehicleCatalogResultAssembler().assemble(
        evidence=[], verdicts=[], candidate_outcomes=[outcome], coverage_gaps=[],
        task_failures=[])
    (vehicle,) = view["vehicles"]
    assert vehicle["vehicle_key"] == "37439"
    assert vehicle["registration"] == outcome["registration"]
    # The evidence facts are exactly those a source record without codes gives.
    mapper = GovernmentVariantEvidenceMapper()
    without = copy.deepcopy(with_codes)
    for field in ("tozeret_cd", "degem_cd", "sug_degem"):
        without["source_record"].pop(field)

    def facts(result: dict[str, Any]) -> list[tuple[Any, ...]]:
        return [(fact.field_key, fact.value, fact.locator.locator_key)
                for fact in mapper.map(SimpleNamespace(result=result)).facts]

    assert facts(with_codes) == facts(without) and facts(with_codes)
    # An outcome recorded without codes carries no registration, and its
    # vehicle keeps its exact pre-PR-Z3 shape.
    old = candidate_outcome(task_id="t01", call_id="c1", tool=GOVERNMENT_TOOL_NAME,
                            operation="resolve_variant",
                            arguments=arguments_of(item, codes=False), result=without)
    assert "registration" not in old
    (plain,) = VehicleCatalogResultAssembler().assemble(
        evidence=[], verdicts=[], candidate_outcomes=[old], coverage_gaps=[],
        task_failures=[])["vehicles"]
    assert set(plain) == {"vehicle_key", "identity", "fields", "needs_review", "sources"}


# =============================================================================
# Z3-6 / Z3-7: the aa63369b batch, reconstructed on the register codes
# =============================================================================

def _recording(batch: Batch, *, codes: bool) -> dict[str, Any]:
    """RECONSTRUCTED, not recorded: one resolve_variant task per handed item (the
    shape the register-code rule asks for), each answered by the REAL tool,
    bound to the batch, over aa63369b's rows with the real five in place."""
    results, asked = [], set()
    for index, item in enumerate(batch.preparation.queue, start=1):
        arguments = arguments_of(item, codes=codes)
        key = json.dumps(arguments, sort_keys=True, ensure_ascii=False)
        if key in asked:
            continue
        asked.add(key)
        results.append({"task_id": f"t{index:02d}", "call_id": "c1",
                        "tool": GOVERNMENT_TOOL_NAME, "operation": "resolve_variant",
                        "arguments": arguments, "result": batch.resolve(arguments)})
    return {"provenance": {"kind": "reconstructed",
                           "source": "derived from aa63369b's batch on the current tool"},
            "tool_results": results}


def test_reconstructed_aa63369b_now_ends_with_twelve_vehicles_and_no_unresolved_group():
    batch = Batch(real_aa_rows())
    assert len(batch.preparation.queue) == 12
    assert all(item.register_codes for item in batch.preparation.queue)
    # Without the codes: exactly the historical outcome -- 8 vehicles and the
    # two "ambiguous" groups production paid for.
    before = production_result(_recording(batch, codes=False))
    assert (len(before["vehicles"]), len(before["unresolved_groups"])) == (8, 2)
    # With them: every handed row resolves to itself.
    after = production_result(_recording(batch, codes=True))
    assert (len(after["vehicles"]), len(after["unresolved_groups"])) == (12, 0)
    assert sorted(vehicle["vehicle_key"] for vehicle in after["vehicles"]) == \
        sorted(str(row["_id"]) for row in AA["snapshot_rows"])
    assert after["summary"]["unresolved_ambiguous"] == 0
    for vehicle in after["vehicles"]:
        if vehicle["vehicle_key"] in REAL_ROWS:
            codes = coverage.register_codes(REAL_ROWS[vehicle["vehicle_key"]])
            assert vehicle["registration"] == dict(zip(("tozeret_cd", "degem_cd",
                                                        "sug_degem"), codes))


def test_only_identical_content_stays_ambiguous():
    """Z3-6: the ONE remaining ambiguity is rows identical minus `_id` -- PR-Z2's
    duplicate group -- and the codes cannot, and must not, split it."""
    real = REAL_ROWS["37350"]
    twin = {**copy.deepcopy(real), "_id": 94_350}
    batch = Batch([real, twin, REAL_ROWS["37439"]])
    item = batch.item("37350")
    assert item.duplicate_identity_record_ids == ("37350", "94350")
    result = batch.resolve(arguments_of(item, codes=True))
    assert (result["ambiguous"], result["match_count"]) == (True, 2)
    assert rows_of(result) == ["37350", "94350"]
    assert {entry["degem_cd"] for entry in result["distinguishing"]} == {"758"}

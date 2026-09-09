"""R5: one real, read-only vehicle evidence proof.

Everything here is offline. No network, no provider, no paid call, no
browser capture, no production Supabase and no committed secret: the proof
reads pinned fixtures captured once, by hand, as an explicit development
action, and every assertion below is a deterministic function of those bytes.

This module currently covers the R5 execution seam. The three-source vehicle
proof itself lands with the pinned Yeda/Government/Web fixtures.

--- the deterministic zero-model execution seam ------------------------------

R5 must prove a real evidence path with ZERO model and provider calls, which
needs one thing the engine did not have: a way for a TOOL-COMPLETE structured
task to finish without a worker model call. `GenericWorker` gained exactly
one optional, constructor-injected `TaskOutputStrategy` for that, and these
tests pin its boundaries:

*   absent -- the production default -- the model-backed path is unchanged;
*   present, it is reached only after the tool loop has already executed and
    validated every planned call, so it can never influence tool material,
    evidence acquisition or verification;
*   its output is re-validated against the task's own closed `output_schema`
    through the same path a model completion travels;
*   a strategy that fails NEVER falls back to a model call, because a silent
    fallback would make "zero model calls" unprovable.
"""

from __future__ import annotations

import json
import shutil
from copy import deepcopy
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from backend.engines.swarm_v2 import (
    DETERMINISTIC_OUTPUT_REASONS,
    MAX_TASK_OUTPUT_JSON_BYTES,
    WORKER_OUTPUT_REASONS,
    BoundedTaskExecutor,
    Commander,
    CommanderModelResolver,
    EvidenceReference,
    FinalBuilder,
    GenericWorker,
    PlanLimits,
    PlanValidator,
    SwarmV2Engine,
    TaskGraph,
    Verifier,
    validate_product_outcome,
)
from backend.engines.swarm_v2.evidence import EvidenceBoard, WorkerLease
from backend.engines.swarm_v2.evidence_mapping import TrustedEvidenceAcquisition
from backend.engines.swarm_v2.grounding import RepositoryEvidenceResolver
from backend.engines.swarm_v2.support import VERIFIER_CONTRACT_VERSION
from backend.testing.r5_proof.commander import (TOYOTA_RAV4_PHEV_IL_2021,
                                                DeterministicProofCommanderClient,
                                                UnknownProofRequest)
from backend.testing.r5_proof.strategy import VehicleProofOutputStrategy
from backend.engines.swarm_v2.contracts import CommanderPlan, DynamicTask, PlannedToolCall
from backend.engines.swarm_v2.evidence_bounds import IDENTITY_DIMENSIONS
from backend.engines.swarm_v2.evidence_mapping import (PRODUCTION_EVIDENCE_MAPPERS,
                                                       EvidenceMappingError)
from backend.engines.swarm_v2.tool_calls import ToolCallRecord
from backend.testing.r5_proof import manifest as proof_manifest
from backend.testing.r5_proof.identity import VehicleIdentityError, vehicle_entity_key
from backend.testing.r5_proof.mappers import (YEDA_FACT_FIELDS, YEDA_SOURCE_TYPE,
                                              proof_evidence_mappers)
from backend.testing.r5_proof.tools import YEDA_MARKET_PRESENCE, YedaVehicleCatalogTool
from backend.tools import ToolContext, ToolError, ToolRegistry

from test_swarm_v2_r3_evidence_contract import R3GuardedRepository
from test_swarm_v2_tool_contract import CONTEXT, StubGateway, get_model, registry, spec

#: The one run this proof executes under.
RUN_UUID = UUID("5a5f0000-0000-4000-8000-000000000005")


def only(items):
    """Exactly one item, or a failure that says so."""
    values = list(items)
    assert len(values) == 1, f"expected exactly one item, got {len(values)}"
    return values[0]


# --- the pinned proof source ------------------------------------------------

PROOF_SCOPES = frozenset({"yeda:catalog_read"})
PROOF_CONTEXT = ToolContext(scopes=PROOF_SCOPES)

#: The selected vehicle, as the pinned Yeda catalog states it.
VEHICLE = {"make": "Toyota", "commercial_model": "RAV4", "market": "IL", "model_year": 2021}
#: The one dimension that separates it from the four other RAV4 variants the
#: catalog states for the same commercial model and the same year.
NARROWED = {**VEHICLE, "fuel_type": "plug_in_hybrid"}


def proof_registry() -> ToolRegistry:
    return ToolRegistry([YedaVehicleCatalogTool()])


def yeda_result(**overrides):
    """Run the real registered operation against the pinned fixture."""
    payload = {**NARROWED, **overrides}
    return proof_registry().execute("yeda.vehicle_catalog", "get_model_variant",
                                    PROOF_CONTEXT, payload)


def yeda_bundle(result=None):
    record = ToolCallRecord(task_id="proof-yeda", call_id="yeda-1",
                            tool="yeda.vehicle_catalog", operation="get_model_variant",
                            result=result if result is not None else yeda_result())
    return proof_evidence_mappers().map(record)


@pytest.fixture
def relocated_fixtures(tmp_path, monkeypatch):
    """A byte-identical copy of the committed fixture root, safe to mutate.

    The real committed fixtures are never touched: a test that needs to prove
    tamper-detection edits the copy, so a failing run can never leave modified
    evidence behind in the repository.
    """
    def relocate(mutate=None):
        root = tmp_path / "fixtures"
        shutil.copytree(proof_manifest.FIXTURE_ROOT, root)
        if mutate is not None:
            mutate(root)
        monkeypatch.setattr(proof_manifest, "FIXTURE_ROOT", root)
        monkeypatch.setattr(proof_manifest, "MANIFEST_PATH", root / "manifest.json")
        return root
    return relocate


# --- helpers ----------------------------------------------------------------

def worker_with(strategy, *bodies, tools=None, context=CONTEXT):
    """One worker plus the gateway that records every model call it makes."""
    gateway = StubGateway(*bodies)
    return GenericWorker(gateway=gateway, tools=tools if tools is not None else registry(),
                         model="fake", tool_context=context,
                         task_output_strategy=strategy), gateway


def constant(value):
    """A trusted strategy that states one fixed output, ignoring its inputs."""
    def strategy(*, task, tool_outputs, dependency_outputs):
        return value
    return strategy


# =============================================================================
# 1. the production default is untouched
# =============================================================================

def test_without_a_strategy_the_worker_still_calls_the_model():
    """The seam is opt-in. Injecting nothing keeps the exact previous path."""
    worker, gateway = worker_with(None, {"answer": "from the model"})
    result = worker.execute(spec(get_model()), {})
    assert result.status == "completed"
    assert result.output == {"answer": "from the model"}
    assert len(gateway.calls) == 1


def test_the_production_worker_wiring_injects_no_strategy():
    """R5 adds no production capability.

    The deterministic strategy is selected by trusted wiring only, and the
    production wiring in backend/worker/main.py deliberately selects none --
    so every production Swarm V2 task keeps its model-backed worker output.
    """
    worker_main = Path("backend/worker/main.py").read_text()
    assert "task_output_strategy" not in worker_main


# =============================================================================
# 2. a tool-complete task finishes with no model call at all
# =============================================================================

def test_a_deterministic_strategy_completes_a_task_with_zero_model_calls():
    worker, gateway = worker_with(constant({"answer": "stated by trusted code"}))
    result = worker.execute(spec(get_model()), {})
    assert result.status == "completed"
    assert result.output == {"answer": "stated by trusted code"}
    # The whole point: a completed structured task that cost no completion.
    assert gateway.calls == []


def test_the_strategy_receives_only_validated_tool_material_and_dependencies():
    """A strategy reads what the server already validated, and nothing else.

    The tool outputs it sees are keyed by `call_id` and are exactly the
    Registry-validated results of the approved plan's own calls -- the same
    trusted material `build_worker_request` would otherwise place in a prompt.
    """
    seen = {}

    def strategy(*, task, tool_outputs, dependency_outputs):
        seen.update(task_id=task.task_id, tools=dict(tool_outputs),
                    dependencies=dict(dependency_outputs))
        return {"answer": tool_outputs["vehicle_lookup"]["rows"][0]}

    worker, gateway = worker_with(strategy)
    result = worker.execute(spec(get_model(), task_id="lookup"), {})
    assert result.status == "completed"
    # The value came out of the real tool result, not out of a model.
    assert result.output == {"answer": "Toyota Corolla"}
    assert seen["task_id"] == "lookup"
    assert seen["tools"] == {"vehicle_lookup": {"rows": ["Toyota Corolla"]}}
    assert seen["dependencies"] == {}
    assert gateway.calls == []


def test_the_strategy_runs_only_after_every_planned_call_has_succeeded():
    """The strategy is downstream of the tool loop, never a way around it.

    A task whose planned call is refused never reaches the strategy, so a
    deterministic answer can never be produced from absent tool material --
    the seam finishes a tool-COMPLETE task and nothing else.
    """
    reached = []

    def strategy(*, task, tool_outputs, dependency_outputs):
        reached.append(dict(tool_outputs))
        return {"answer": "reached"}

    # The registry refuses the call outright: no scope was granted.
    denied, denied_gateway = worker_with(strategy,
                                         context=ToolContext(scopes=frozenset()))
    denied_result = denied.execute(spec(get_model()), {})
    assert denied_result.status == "failed"
    assert denied_result.error["code"] == "TOOL_SCOPE_REQUIRED"
    assert reached == []
    assert denied_gateway.calls == []

    # A call that succeeds with an EMPTY answer is still a completed call, and
    # the strategy sees that real emptiness rather than a substitute for it.
    empty, empty_gateway = worker_with(strategy)
    empty_result = empty.execute(
        spec(get_model(arguments={"make": "Ford", "model": "Focus"})), {})
    assert empty_result.status == "completed"
    assert reached == [{"vehicle_lookup": {"rows": []}}]
    assert empty_gateway.calls == []


# =============================================================================
# 3. the strategy is held to the task's own closed contract
# =============================================================================

@pytest.mark.parametrize("produced", [
    {"answer": 7},                              # wrong property type
    {"answer": "ok", "extra": "smuggled"},       # a field the schema forbids
    {},                                          # a required property missing
    "not a json object",                        # not the declared object at all
    None,
])
def test_deterministic_output_that_fails_the_task_schema_fails_closed(produced):
    worker, gateway = worker_with(constant(produced))
    result = worker.execute(spec(get_model()), {})
    assert result.status == "failed"
    assert result.error["code"] == "WORKER_OUTPUT_STRATEGY_INVALID"
    # No silent fallback: a server defect never becomes a paid completion.
    assert gateway.calls == []


def test_oversized_deterministic_output_is_refused_by_the_durable_bound():
    oversized = {"answer": "x" * (MAX_TASK_OUTPUT_JSON_BYTES + 1)}
    worker, gateway = worker_with(constant(oversized))
    result = worker.execute(spec(get_model()), {})
    assert result.status == "failed"
    assert result.error["code"] == "WORKER_OUTPUT_STRATEGY_INVALID"
    assert gateway.calls == []


def test_a_strategy_that_raises_fails_closed_and_leaks_nothing():
    def strategy(*, task, tool_outputs, dependency_outputs):
        raise RuntimeError("secret-bearing internal detail")

    worker, gateway = worker_with(strategy)
    result = worker.execute(spec(get_model()), {})
    assert result.status == "failed"
    assert result.error["code"] == "WORKER_OUTPUT_STRATEGY_FAILED"
    # Only the static code and its static message travel.
    assert "secret-bearing" not in str(result.error)
    assert gateway.calls == []


def test_a_deterministic_failure_is_never_a_repairable_model_failure():
    """The two vocabularies are disjoint, by construction.

    WORKER_OUTPUT_REASONS is the closed family a bounded model repair may
    answer. A deterministic strategy has no model to repair with, so its
    reasons must never appear there -- otherwise a strategy defect could earn
    a paid repair call and quietly break the zero-model guarantee.
    """
    assert DETERMINISTIC_OUTPUT_REASONS.isdisjoint(WORKER_OUTPUT_REASONS)
    assert DETERMINISTIC_OUTPUT_REASONS == {"WORKER_OUTPUT_STRATEGY_FAILED",
                                            "WORKER_OUTPUT_STRATEGY_INVALID"}


# =============================================================================
# 4. nothing a client, a plan or a model controls can select the strategy
# =============================================================================

def test_no_plan_or_run_input_field_can_name_a_deterministic_strategy():
    """The closed plan contracts have no seam for it.

    A client-controlled `skip_model`, an `approved_plan` override or a raw
    strategy field would make the zero-model path reachable from untrusted
    input. The contracts are `extra="forbid"`, so the absence is enforced
    rather than merely observed.
    """
    for contract in (PlannedToolCall, DynamicTask, CommanderPlan):
        fields = set(contract.model_fields)
        assert not fields & {"task_output_strategy", "skip_model", "deterministic",
                             "strategy", "approved_plan"}
    with pytest.raises(Exception):
        PlannedToolCall.model_validate({"call_id": "c1", "name": "mock.search",
                                        "operation": "search", "arguments": {},
                                        "task_output_strategy": "deterministic"})


def test_the_strategy_is_reachable_only_through_the_worker_constructor():
    """Trusted wiring is the ONE selection path.

    The engine, the executor, the commander and the plan validator never
    mention the seam, so no orchestration layer can turn it on from data.
    """
    for module in ("engine.py", "executor.py", "commander.py", "validation.py",
                   "model_gateway.py"):
        source = Path("backend/engines/swarm_v2", module).read_text()
        assert "task_output_strategy" not in source


# =============================================================================
# 5. R5 introduces no production tool registration and no write capability
# =============================================================================

def test_the_production_tool_registry_is_still_empty():
    assert ToolRegistry().allowed_names == frozenset()
    worker_main = Path("backend/worker/main.py").read_text()
    assert "tools = ToolRegistry()" in worker_main


# =============================================================================
# 6. the pinned fixtures are exactly what the manifest says they are
# =============================================================================

def test_every_committed_fixture_matches_its_manifest_checksum():
    """The gate every proof read passes through.

    Re-hashing on every run is what makes "pinned" mean something: a fixture
    that drifted, was regenerated from a different upstream version, or was
    edited by hand stops the proof instead of producing different evidence
    under the same provenance.
    """
    verified = proof_manifest.verify_all_fixtures()
    assert "yeda" in verified
    manifest = proof_manifest.load_manifest()
    assert manifest["manifest_version"] == proof_manifest.MANIFEST_VERSION
    for key in verified:
        entry = manifest["sources"][key]
        for field in proof_manifest.REQUIRED_SOURCE_FIELDS:
            assert entry.get(field), f"{key} is missing {field}"
        for field in proof_manifest.REQUIRED_BY_KIND[entry["source_kind"]]:
            assert entry.get(field), f"{key} is missing {field}"
        assert entry["fixture_kind"] in proof_manifest.FIXTURE_KINDS


def test_one_modified_byte_fails_the_fixture_closed(relocated_fixtures):
    def flip_one_byte(root):
        path = root / "yeda" / "rav4_model_record.json"
        raw = path.read_bytes()
        # 306 hp -> 305 hp: a single digit, plausible, and fatal.
        path.write_bytes(raw.replace(b'"horsepower_hp": 306', b'"horsepower_hp": 305', 1))

    relocated_fixtures(flip_one_byte)
    with pytest.raises(proof_manifest.ProofManifestError) as failure:
        proof_manifest.load_fixture("yeda")
    assert failure.value.reason_code == "R5_FIXTURE_CHECKSUM_MISMATCH"


def test_a_missing_fixture_and_an_unmanifested_source_both_fail_closed(relocated_fixtures):
    relocated_fixtures(lambda root: (root / "yeda" / "rav4_model_record.json").unlink())
    with pytest.raises(proof_manifest.ProofManifestError) as failure:
        proof_manifest.load_fixture("yeda")
    assert failure.value.reason_code == "R5_FIXTURE_MISSING"
    with pytest.raises(proof_manifest.ProofManifestError) as unknown:
        proof_manifest.source_entry("no_such_source")
    assert unknown.value.reason_code == "R5_MANIFEST_SOURCE_UNKNOWN"


@pytest.mark.parametrize("relative", ["/etc/passwd", "../../../etc/passwd", ""])
def test_a_fixture_path_can_never_escape_the_committed_root(relative):
    with pytest.raises(proof_manifest.ProofManifestError) as failure:
        proof_manifest._resolved_fixture_path(relative)
    assert failure.value.reason_code == "R5_FIXTURE_PATH_INVALID"


def test_the_yeda_source_is_pinned_to_an_immutable_commit_and_is_self_consistent():
    """A branch name is not a version.

    The manifest pins the repository, the commit, the catalog path, the git
    blob and the upstream file digest, and the canonical URL is the immutable
    GitHub blob URL that CONTAINS that commit -- never an unpinned `main` URL
    whose content can change underneath the evidence.
    """
    entry = proof_manifest.source_entry("yeda")
    commit, path = entry["commit_sha"], entry["repository_path"]
    assert len(commit) == 40 and all(c in "0123456789abcdef" for c in commit)
    assert len(entry["blob_sha"]) == 40
    assert entry["canonical_url"] == \
        f"https://github.com/{entry['repository']}/blob/{commit}/{path}"
    assert "/blob/main/" not in entry["canonical_url"]
    # The tool reports the SAME pinned identity it was manifested with, so the
    # provenance a mapper reads can never disagree with the manifest.
    source = yeda_result()["source"]
    assert source["commit_sha"] == commit
    assert source["blob_sha"] == entry["blob_sha"]
    assert source["repository_path"] == path
    assert source["canonical_url"] == entry["canonical_url"]
    assert source["catalog_hash"] == entry["upstream_catalog_hash"]


# =============================================================================
# 7. the read-only proof tool: bounded, closed and fail-closed
# =============================================================================

def test_make_model_and_year_alone_can_never_silently_merge_ambiguous_variants():
    """The central identity rule, proven against the real catalog.

    The pinned catalog states FIVE Toyota RAV4 variants covering model year
    2021. Asking for the commercial model and the year is therefore not a
    question with one answer, and the operation refuses it rather than
    returning whichever variant happens to come first.
    """
    with pytest.raises(ToolError) as failure:
        proof_registry().execute("yeda.vehicle_catalog", "get_model_variant",
                                 PROOF_CONTEXT, dict(VEHICLE))
    assert failure.value.code == "R5_YEDA_VARIANT_AMBIGUOUS"
    # One real narrowing dimension is enough, and it resolves to one variant.
    assert yeda_result()["record_locator"] == {"model_index": 860, "variant_index": 4}


def test_the_tool_fails_closed_on_an_identity_the_catalog_does_not_hold():
    for payload, expected in (
        ({**NARROWED, "make": "Mazda"}, "R5_YEDA_RECORD_NOT_FOUND"),
        ({**NARROWED, "commercial_model": "Corolla"}, "R5_YEDA_RECORD_NOT_FOUND"),
        ({**NARROWED, "market": "DE"}, "R5_YEDA_RECORD_NOT_FOUND"),
        ({**NARROWED, "model_year": 1998}, "R5_YEDA_VARIANT_NOT_FOUND"),
        ({**NARROWED, "drivetrain": "FWD"}, "R5_YEDA_VARIANT_NOT_FOUND"),
    ):
        with pytest.raises(ToolError) as failure:
            proof_registry().execute("yeda.vehicle_catalog", "get_model_variant",
                                     PROOF_CONTEXT, payload)
        assert failure.value.code == expected, payload


def test_the_tool_output_satisfies_its_own_closed_schema_and_rejects_unknown_fields():
    """The Registry validates in both directions, on the real operation."""
    result = yeda_result()          # would raise TOOL_OUTPUT_INVALID otherwise
    assert set(result) == {"attributed_source_urls", "declared_missing_identity", "model",
                           "record_id", "record_locator", "source", "variant"}
    with pytest.raises(ToolError) as failure:
        proof_registry().execute("yeda.vehicle_catalog", "get_model_variant",
                                 PROOF_CONTEXT, {**NARROWED, "generation": "XA50"})
    assert failure.value.code == "TOOL_INPUT_INVALID"


def test_the_proof_tool_exposes_no_write_operation_and_no_bulk_read():
    tool = YedaVehicleCatalogTool()
    assert tool.mode.value == "read"
    # ONE operation, and it returns ONE identified variant. There is
    # deliberately no list-all, no search and no whole-catalog escape hatch,
    # so the 7.3 MB upstream document can never cross this boundary.
    assert set(tool.operations) == {"get_model_variant"}
    for name in ("list", "search", "all", "dump", "write", "update", "patch"):
        assert not any(name in operation for operation in tool.operations)


def test_the_market_presence_label_is_read_through_a_closed_vocabulary():
    """`market` in this catalog is a PRESENCE label, not a market identifier.

    Across the pinned catalog the field takes the values IL, IL-confirmed,
    IL-likely and global-reference-only. Treating it as a market name would
    make a global reference record answer an Israeli-market question, so it is
    read through a closed map and an unknown value fails closed.
    """
    assert "global-reference-only" not in YEDA_MARKET_PRESENCE
    result = yeda_result()
    assert result["model"]["market"] == "IL"
    assert result["model"]["market_presence"] == "confirmed"
    tool = YedaVehicleCatalogTool()
    with pytest.raises(ToolError) as failure:
        tool._market_of({"market": "global-reference-only"})
    assert failure.value.code == "R5_YEDA_MARKET_UNKNOWN"


def test_the_real_coverage_gap_is_reported_rather_than_filled_in():
    """The catalog states no generation, no official model code and no trim.

    That is a real absence in the real source. It is reported as one, and the
    variant object simply has no `trim` key -- there is no placeholder, no
    empty string and no inferred value standing in for evidence nobody has.
    """
    result = yeda_result()
    assert result["declared_missing_identity"] == ["generation", "model_code", "trim"]
    assert set(result["declared_missing_identity"]) <= set(IDENTITY_DIMENSIONS)
    assert "trim" not in result["variant"]


# =============================================================================
# 8. the trusted mapper: exact provenance, honest semantics
# =============================================================================

def test_the_yeda_mapper_preserves_version_locator_unit_and_identity_exactly():
    bundle = yeda_bundle()
    # The source version is the pinned COMMIT: an immutable upstream
    # identifier, never a retrieval timestamp and never a fragment hash.
    assert bundle.source.version.version_key == \
        "git_commit:" + proof_manifest.source_entry("yeda")["commit_sha"]
    assert bundle.locator_scope == ("yeda.models.860.variants.4",)

    by_field = {fact.field_key: fact for fact in bundle.facts}
    assert set(by_field) == {name for name, _, _ in YEDA_FACT_FIELDS}
    # Value AND unit travel together; a numeric fact without a unit is a
    # contract violation, so this is enforced rather than merely observed.
    assert (by_field["horsepower_hp"].value, by_field["horsepower_hp"].unit) == (306, "hp")
    assert (by_field["fuel_type"].value, by_field["fuel_type"].unit) == ("plug_in_hybrid", None)
    # Every fact names the UPSTREAM field it was read from.
    assert by_field["nominal_engine_displacement_l"].locator.field_path == \
        ("engine_displacement_l",)
    assert by_field["horsepower_hp"].locator.field_path == ("horsepower_hp",)
    for fact in bundle.facts:
        assert fact.market == "IL" and fact.geography == "IL"
        assert dict(fact.time_scope) == {"year_start": 2021, "year_end": 2026}
        # Only the dimensions the record STATES. Absence is unknown, not equal.
        assert dict(fact.identity) == {"body_style": "SUV", "drivetrain": "AWD",
                                       "engine": "2.5L", "transmission": "cvt"}
        assert "generation" not in fact.identity and "model_code" not in fact.identity


def test_a_nominal_engine_class_label_is_never_recorded_as_an_exact_displacement():
    """The semantic rule that keeps two different measurements apart.

    The catalog states an engine-CLASS label ("2.5L" / 2.5), which is not a
    homologated displacement. Recording it as `engine_displacement_cc` would
    put it in the same R4 comparison scope as an exact homologation figure and
    manufacture either a contradiction or an agreement neither source states.
    """
    fields = {name for name, _, _ in YEDA_FACT_FIELDS}
    assert "nominal_engine_displacement_l" in fields
    assert "engine_displacement_cc" not in fields
    assert "engine_displacement_l" not in fields
    nominal = next(fact for fact in yeda_bundle().facts
                   if fact.field_key == "nominal_engine_displacement_l")
    assert (nominal.value, nominal.unit) == (2.5, "l")


def test_the_yeda_source_type_is_authoritative_for_nothing():
    """A derived catalog closes no conflict, however confident it sounds.

    R4's authority policy is field-specific AND source-type-specific, and it
    fails closed on a type it does not know. Yeda is an aggregated secondary
    catalog, not a government registry and not a manufacturer publication, so
    it can never settle a contradiction on its own.
    """
    from backend.engines.swarm_v2.conflict_policy import SOURCE_TYPE_AUTHORITY, is_authoritative
    assert YEDA_SOURCE_TYPE not in SOURCE_TYPE_AUTHORITY
    for field in ("fuel_type", "horsepower_hp", "nominal_engine_displacement_l",
                  "list_price", "reliability_score", "model_code"):
        assert not is_authoritative(YEDA_SOURCE_TYPE, field)
    assert yeda_bundle().source.source_type == YEDA_SOURCE_TYPE


def test_every_fragment_is_a_typed_projection_bound_to_its_own_locator():
    bundle = yeda_bundle()
    assert len(bundle.fragments) == len(bundle.facts)
    for fragment in bundle.fragments:
        # A deterministic rendering of a structured record can never be
        # presented as a verbatim quote from a document.
        assert fragment.fragment_type == "structured_projection"
        assert fragment.locator.kind == "record_field"
    # Each fact's locator is one of its own bundle's fragment locators, so a
    # located fact is always backed by focused evidence at that exact place.
    fragment_locators = {item.locator.locator_key for item in bundle.fragments}
    assert all(fact.locator.locator_key in fragment_locators for fact in bundle.facts)


def test_an_unmapped_operation_can_never_fall_back_to_generic_extraction():
    """No silent path from an unknown operation to evidence."""
    record = ToolCallRecord(task_id="t", call_id="c", tool="yeda.vehicle_catalog",
                            operation="some_other_operation", result=yeda_result())
    with pytest.raises(EvidenceMappingError) as failure:
        proof_evidence_mappers().map(record)
    assert failure.value.reason_code == "EVIDENCE_MAPPER_NOT_REGISTERED"


@pytest.mark.parametrize("forged", [
    {"answer": "the model says 306 hp"},
    "306 hp",
    None,
])
def test_a_model_completion_can_never_enter_the_evidence_path(forged):
    with pytest.raises(EvidenceMappingError) as failure:
        proof_evidence_mappers().map(forged)
    assert failure.value.reason_code == "EVIDENCE_SOURCE_NOT_TRUSTED"


def test_the_entity_key_is_one_shared_conservative_identity():
    """One entity contract, shared, and fail-closed on an unknown market."""
    assert vehicle_entity_key(make="Toyota", commercial_model="RAV4", market="IL") == \
        "toyota:rav4:il"
    # Formatting is not identity: spacing and case never make two vehicles.
    assert vehicle_entity_key(make=" toyota ", commercial_model="RAV 4", market="IL") == \
        vehicle_entity_key(make="Toyota", commercial_model="rav-4", market="IL")
    with pytest.raises(VehicleIdentityError):
        vehicle_entity_key(make="Toyota", commercial_model="RAV4", market="DE")
    assert all(fact.entity_key == "toyota:rav4:il" for fact in yeda_bundle().facts)


def test_the_proof_mappers_are_not_production_mappers():
    assert PRODUCTION_EVIDENCE_MAPPERS.registered == frozenset()
    assert proof_evidence_mappers().registered == {
        ("yeda.vehicle_catalog", "get_model_variant")}


# =============================================================================
# 9. the WHOLE path, through the real engine, with zero model calls
# =============================================================================
#
# This is the section that makes R5 a proof rather than a collection of unit
# tests. It runs the real SwarmV2Engine over the real Commander, PlanValidator,
# BoundedTaskExecutor, GenericWorker, ToolRegistry, ToolCallRecord,
# TrustedEvidenceAcquisition, EvidenceBoard, RepositoryEvidenceResolver,
# Verifier and FinalBuilder -- and every model dependency in it is POISONED, so
# any provider path that were reached would fail the test immediately rather
# than quietly returning a plausible answer.

class PoisonedModelDependency(AssertionError):
    """Raised the instant any Commander/Worker/Verifier model path is used."""


class PoisonGateway:
    """A ModelGateway stand-in that can only fail, and counts being asked.

    A mock that RETURNS a response is not evidence of zero model use: the call
    still happened, and a run could depend on it. This one makes the call
    itself fatal, so "no model was used" is proven by the run completing at
    all rather than by inspecting a counter afterwards -- though the counter is
    asserted too.
    """

    def __init__(self):
        self.calls: list[dict] = []

    def call(self, **kwargs):
        self.calls.append(kwargs)
        raise PoisonedModelDependency(
            f"a model call reached the provider path: {kwargs.get('agent')}")

    # The two client shapes Commander probes for, poisoned the same way.
    def create_plan(self, **kwargs):
        self.calls.append(kwargs)
        raise PoisonedModelDependency("a Commander completion was requested")

    def create_replan(self, **kwargs):
        self.calls.append(kwargs)
        raise PoisonedModelDependency("a Commander replan completion was requested")


class ProofRepository(R3GuardedRepository):
    """The R3 guarded evidence repository plus the two R4 durable writes.

    Deliberately a SUBCLASS of the repository the R3 suite already proves,
    rather than a new persistence layer: R5 introduces no evidence store of
    its own, so the same guarded-RPC guarantees -- lease on every write,
    idempotency on `evidence_key`, provenance validated instead of trusted --
    cover the R5 path unchanged.
    """

    def __init__(self, lease):
        super().__init__(lease)
        self.verdicts: dict[str, dict] = {}
        self.supports: list[dict] = []
        self.resolutions: dict[str, dict] = {}
        self.checkpoints: list[dict] = []

    def list_structured_facts_for_sources(self, run_id, source_ids, *, limit=200):
        """The third bounded internal read the R4 grounding resolver performs."""
        wanted = {str(item) for item in source_ids}
        rows = [row for row in self.claims.values()
                if row["run_id"] == str(run_id) and str(row["source_id"]) in wanted]
        rows.sort(key=lambda row: (str(row["source_id"]), row["id"]))
        return rows[:limit]

    def record_claim_verdict(self, run_id, payload, **kwargs):
        self._assert_lease(run_id, kwargs, "verdict")
        claim = next((row for row in self.claims.values()
                      if row["id"] == str(payload["claim_id"])), None)
        if claim is None:
            raise AssertionError("invalid claim verdict claim")
        for link in payload["support"]:
            fragment = next((row for row in self.fragments.values()
                             if row["id"] == str(link["fragment_id"])), None)
            if fragment is None:
                raise AssertionError("verdict support link does not name durable evidence")
            if str(fragment["source_id"]) != str(claim["source_id"]):
                raise AssertionError("verdict support link belongs to another source")
            if fragment["content_hash"] != link["content_hash"]:
                raise AssertionError("verdict support link content hash mismatch")
        row = self.verdicts.setdefault(payload["evidence_key"],
                                       {"id": str(uuid4()), "run_id": str(run_id), **payload})
        if row["verdict"] != payload["verdict"] or row["reason"] != payload["reason"]:
            raise AssertionError("claim verdict idempotency conflict")
        self.supports = [item for item in self.supports if item["verdict_id"] != row["id"]]
        self.supports.extend({"verdict_id": row["id"], **link} for link in payload["support"])
        return row

    def record_conflict_resolution(self, run_id, payload, **kwargs):
        self._assert_lease(run_id, kwargs, "resolution")
        return self.resolutions.setdefault(
            payload["evidence_key"], {"id": str(uuid4()), "run_id": str(run_id), **payload})


def proof_run(*, checkpoint=None, repository=None, board=None, checkpoints=None):
    """Execute the compiled proof plan through the REAL engine, end to end.

    Every model dependency is the poison gateway: the Commander client, the
    worker's gateway and the verifier's gateway. Nothing here supplies a
    scripted completion, so the run can only finish if no model path is
    reached at all.
    """
    lease = WorkerLease(RUN_UUID, "worker-r5", 1, "lease-token-r5")
    repository = repository if repository is not None else ProofRepository(lease)
    board = board if board is not None else EvidenceBoard(repository, lease)
    poison = PoisonGateway()
    registry_of_proof = proof_registry()

    commander = Commander(
        client=DeterministicProofCommanderClient(TOYOTA_RAV4_PHEV_IL_2021.key),
        resolver=CommanderModelResolver(("compiled",), {"compiled"}),
        # The SAME deterministic firewall a model-authored plan passes.
        validator=PlanValidator(allowed_tools=registry_of_proof.descriptors(),
                                limits=PlanLimits(max_tasks=8, max_tool_calls=8,
                                                  max_replans=0)))
    acquisition = TrustedEvidenceAcquisition(board=board, mappers=proof_evidence_mappers())
    executor = BoundedTaskExecutor(
        worker_factory=lambda: GenericWorker(
            gateway=poison, tools=registry_of_proof, model="unused",
            tool_context=PROOF_CONTEXT,
            tool_result_sink=acquisition,
            task_output_strategy=VehicleProofOutputStrategy()),
        max_active_workers=1)
    engine = SwarmV2Engine(
        commander=commander, executor=executor,
        verifier=Verifier(gateway=poison, model="unused",
                          resolver=RepositoryEvidenceResolver(repository, run_id=RUN_UUID)),
        builder=FinalBuilder(),
        evidence_loader=lambda _: [EvidenceReference.model_validate(item)
                                   for item in board.references()],
        checkpoint_sink=(None if checkpoints is None
                         else lambda phase, value: checkpoints.append(deepcopy(value))),
        verdict_sink=board.record_verification_verdict,
        resolution_sink=board.record_conflict_resolution)
    payload = {"id": str(RUN_UUID),
               "input": {"objective": "prove one real vehicle evidence path",
                         "commander_model": "compiled"}}
    if checkpoint is not None:
        payload["checkpoint"] = checkpoint
    return engine.run(payload), repository, poison, board


def test_the_whole_proof_runs_through_the_real_engine_with_zero_model_calls():
    """The acceptance path, end to end, offline.

    real pinned Yeda fixture -> validated read-only ToolRegistry operation ->
    ToolCallRecord -> trusted operation-specific mapper -> versioned source ->
    focused fragments and structured facts -> lease-guarded EvidenceBoard ->
    RepositoryEvidenceResolver -> R4 deterministic verification -> FinalBuilder
    -> truthful R1 product outcome.
    """
    checkpoints = []
    result, repository, poison, board = proof_run(checkpoints=checkpoints)

    # 1. Not one model or provider call anywhere in the run.
    assert poison.calls == []

    # 2. Durable evidence was written in the contract's order, under the lease.
    assert repository.writes[:4] == ["source", "fragment", "fragment", "fragment"]
    assert repository.writes.count("source") == 1
    assert len(repository.claims) == 3 and len(repository.fragments) == 3

    # 3. The source is pinned to the real commit, and every claim is located.
    source = only(list(repository.sources.values()))
    assert (source["source_version_kind"], source["source_version_id"]) == \
        ("git_commit", proof_manifest.source_entry("yeda")["commit_sha"])
    assert all(row["evidence_locator"] for row in repository.claims.values())

    # 4. Every claim was settled DETERMINISTICALLY, with durable support.
    assert len(repository.verdicts) == 3
    assert {row["verdict"] for row in repository.verdicts.values()} == {"verified"}
    assert {row["verification_mode"] for row in repository.verdicts.values()} == \
        {"deterministic_structured"}
    assert {row["verifier_contract_version"] for row in repository.verdicts.values()} == \
        {VERIFIER_CONTRACT_VERSION}
    assert len(repository.supports) == 3

    # 5. The R1 product outcome is valid, and its fields are verified material.
    assert validate_product_outcome(result)
    assert result["fields"]["fuel_type"][0]["value"] == "plug_in_hybrid"
    assert result["fields"]["horsepower_hp"][0]["value"] == 306
    assert result["fields"]["nominal_engine_displacement_l"][0]["value"] == 2.5
    # Provenance travels into the product output: every field names the claim,
    # the source and the run that produced it.
    for entries in result["fields"].values():
        for entry in entries:
            assert entry["provenance"]["source_id"] == source["id"]
            assert entry["provenance"]["run_id"] == str(RUN_UUID)

    # 6. The checkpoint carries the same verdicts, so a resume replays them.
    state = checkpoints[-1]["artifacts"]["swarm_state"]
    assert len(state["evidence_references"]) == 3
    assert {item["reason"] for item in state["verifier_state"].values()} == \
        {"R4_STRUCTURED_MATCH"}


def test_the_proof_would_fail_if_it_bypassed_any_required_stage():
    """The stages are load-bearing, not decorative.

    Each disabled stage below is one the proof is REQUIRED to use. If a future
    change let the run reach a result without it, this test fails -- which is
    the point: it is the guard against quietly re-implementing the path.
    """
    lease = WorkerLease(RUN_UUID, "worker-r5", 1, "lease-token-r5")

    # (a) Without ToolRegistry validation there is no validated result at all:
    # the registry refuses an operation whose scope was never granted, so no
    # ToolCallRecord, no mapper, no evidence.
    repository = ProofRepository(lease)
    board = EvidenceBoard(repository, lease)
    acquisition = TrustedEvidenceAcquisition(board=board, mappers=proof_evidence_mappers())
    worker = GenericWorker(gateway=PoisonGateway(), tools=proof_registry(), model="unused",
                           tool_context=ToolContext(scopes=frozenset()),
                           tool_result_sink=acquisition,
                           task_output_strategy=VehicleProofOutputStrategy())
    task = TOYOTA_RAV4_PHEV_IL_2021.tasks[0]
    graph = TaskGraph.model_validate({"tasks": [task.as_plan_task()]})
    denied = worker.execute(graph.tasks[0], {})
    assert denied.status == "failed" and denied.error["code"] == "TOOL_SCOPE_REQUIRED"
    assert repository.sources == {} and repository.claims == {}

    # (b) Without TrustedEvidenceAcquisition the tool result stays MATERIAL:
    # the task still completes, and not one durable evidence row exists.
    unwired_repository = ProofRepository(lease)
    unwired = GenericWorker(gateway=PoisonGateway(), tools=proof_registry(), model="unused",
                            tool_context=PROOF_CONTEXT,
                            task_output_strategy=VehicleProofOutputStrategy())
    completed = unwired.execute(graph.tasks[0], {})
    assert completed.status == "completed"
    assert unwired_repository.sources == {} and unwired_repository.claims == {}

    # (c) Without RepositoryEvidenceResolver reading the structured facts, the
    # deterministic comparison has nothing to compare against, so verification
    # falls through to the grounded MODEL verifier -- which is poisoned.
    class NoStructuredFacts(ProofRepository):
        def list_structured_facts_for_sources(self, run_id, source_ids, *, limit=200):
            return []

    with pytest.raises(PoisonedModelDependency):
        proof_run(repository=NoStructuredFacts(
            WorkerLease(RUN_UUID, "worker-r5", 1, "lease-token-r5")))


def test_replaying_the_proof_duplicates_no_durable_evidence():
    """Replay is read-only and idempotent on the durable rows.

    The same source version, the same locators and the same content replay
    onto the SAME rows, so a resumed or retried run cannot inflate the
    evidence behind a result.
    """
    lease = WorkerLease(RUN_UUID, "worker-r5", 1, "lease-token-r5")
    repository = ProofRepository(lease)
    first, _, _, _ = proof_run(repository=repository)
    counts = (len(repository.sources), len(repository.fragments), len(repository.claims),
              len(repository.verdicts), len(repository.supports))
    second, _, _, _ = proof_run(repository=repository)
    assert second == first
    assert (len(repository.sources), len(repository.fragments), len(repository.claims),
            len(repository.verdicts), len(repository.supports)) == counts
    assert counts == (1, 3, 3, 3, 3)


def test_a_stale_worker_lease_fails_every_durable_proof_write_closed():
    """Evidence is only ever written under the ACTIVE lease."""
    lease = WorkerLease(RUN_UUID, "worker-r5", 1, "lease-token-r5")
    repository = ProofRepository(lease)
    stale = EvidenceBoard(repository, WorkerLease(RUN_UUID, "worker-r5", 1, "superseded"))
    acquisition = TrustedEvidenceAcquisition(board=stale, mappers=proof_evidence_mappers())
    record = ToolCallRecord(task_id="yeda_variant", call_id="yeda-1",
                            tool="yeda.vehicle_catalog", operation="get_model_variant",
                            result=yeda_result())
    with pytest.raises(AssertionError, match="STALE_WORKER_WRITE"):
        acquisition.acquire(record)
    assert repository.sources == {}


def test_the_compiled_plan_passes_the_real_firewall_and_grants_nothing():
    """A compiled plan is inert until PlanValidator approves it.

    It gets no shortcut: same contract, same limits, same semantic rules a
    model-authored plan faces. And it cannot grant itself anything -- scopes
    live on the server-owned ToolContext, which no plan can reach.
    """
    client = DeterministicProofCommanderClient(TOYOTA_RAV4_PHEV_IL_2021.key)
    validator = PlanValidator(allowed_tools=proof_registry().descriptors(),
                              limits=PlanLimits(max_tasks=8, max_tool_calls=8, max_replans=0))
    plan = validator.validate(client.create_plan(model="compiled", objective="anything at all",
                                                 context={"hostile": "input"}))
    call = only(only(plan.graph.tasks).tools)
    assert (call.name, call.operation) == ("yeda.vehicle_catalog", "get_model_variant")
    # The arguments came from the static table, never from the objective or
    # the context the caller supplied.
    assert call.arguments == {"make": "Toyota", "commercial_model": "RAV4", "market": "IL",
                              "model_year": 2021, "fuel_type": "plug_in_hybrid"}
    assert "hostile" not in json.dumps(plan.model_dump(mode="json"))
    # No scope, capability or write approval is expressible in a plan at all.
    assert "scopes" not in json.dumps(plan.model_dump(mode="json"))


def test_an_unknown_proof_request_has_no_plan_at_all():
    with pytest.raises(UnknownProofRequest):
        DeterministicProofCommanderClient("some-other-vehicle")

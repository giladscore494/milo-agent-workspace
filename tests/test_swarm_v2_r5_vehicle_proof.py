"""R5: one real, read-only vehicle evidence proof.

Everything here is offline. No network, no provider, no paid call, no
browser capture, no production Supabase and no committed secret: the proof
reads pinned fixtures captured once, by hand, as an explicit development
action, and every assertion below is a deterministic function of those bytes.

This module covers the whole R5 proof: the execution seam, the three pinned
real sources -- the Yeda catalog, the Israeli Ministry of Transport
vehicle-model register and the official Toyota Israel archived-model page --
and the truthful partial outcome they actually support.

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

import hashlib
import json
import re
import shutil
from copy import deepcopy
from pathlib import Path
from typing import Mapping
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
    VerificationVerdict,
    Verifier,
    validate_product_outcome,
)
from backend.engines.swarm_v2.outcome import NO_USABLE_RESULT_CODE
from backend.engines.swarm_v2.evidence import EvidenceBoard, WorkerLease
from backend.engines.swarm_v2.evidence_mapping import TrustedEvidenceAcquisition
from backend.engines.swarm_v2.grounding import RepositoryEvidenceResolver
from backend.engines.swarm_v2.support import VERIFIER_CONTRACT_VERSION
from backend.testing.r5_proof.commander import (GOVERNMENT_RECORD_ID_2021,
                                                TOYOTA_RAV4_PHEV_IL_2021,
                                                DeterministicProofCommanderClient,
                                                UnknownProofRequest)
from backend.testing.r5_proof.strategy import VehicleProofOutputStrategy
from backend.engines.swarm_v2.contracts import CommanderPlan, DynamicTask, PlannedToolCall
from backend.engines.swarm_v2.evidence_bounds import IDENTITY_DIMENSIONS
from backend.engines.swarm_v2.evidence_mapping import (PRODUCTION_EVIDENCE_MAPPER_OPERATIONS,
                                                       EvidenceMappingError)
from backend.engines.swarm_v2.tool_calls import ToolCallRecord
from backend.testing.r5_proof import manifest as proof_manifest
from backend.testing.r5_proof.identity import VehicleIdentityError, vehicle_entity_key
from backend.testing.r5_proof import government as government_module
from backend.testing.r5_proof.government import (GOVERNMENT_DATASET_MARKET, MAKE_BY_TOZAR,
                                                 MAX_SCANNED_RECORDS,
                                                 R5_GOV_PAGINATION_REASONS, UNMAPPED_FIELDS,
                                                 WLTP_PAGE_PLAN, WLTP_PAGE_SOURCE_KEYS,
                                                 WLTP_RESOURCE_ID,
                                                 GovernmentVehicleRegistryTool)
from backend.testing.r5_proof.mappers import (GOVERNMENT_FACT_FIELDS, GOVERNMENT_SOURCE_TYPE,
                                              WEB_SOURCE_TYPE, YEDA_FACT_FIELDS,
                                              YEDA_SOURCE_TYPE, proof_evidence_mappers)
from backend.testing.r5_proof.tools import YEDA_MARKET_PRESENCE, YedaVehicleCatalogTool
from scripts import r5_capture_fixtures
from backend.testing.r5_proof.web import (TOYOTA_RAV4_PHEV_STATEMENTS,
                                          WEB_TEXT_PROJECTION_VERSION,
                                          ToyotaArchivedModelDocumentTool,
                                          visible_text_projection)
from backend.engines.swarm_v2.comparison import scope_identity
from backend.engines.swarm_v2.conflict_policy import (SOURCE_TYPE_AUTHORITY, conflict_groups,
                                                      is_authoritative)
from backend.tools import ToolContext, ToolError, ToolMode, ToolRegistry

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

PROOF_SCOPES = frozenset({"yeda:catalog_read", "gov_il:registry_read",
                          "web:saved_document_read"})
PROOF_CONTEXT = ToolContext(scopes=PROOF_SCOPES)

#: The selected vehicle, as the pinned Yeda catalog states it.
VEHICLE = {"make": "Toyota", "commercial_model": "RAV4", "market": "IL", "model_year": 2021}
#: The one dimension that separates it from the four other RAV4 variants the
#: catalog states for the same commercial model and the same year.
NARROWED = {**VEHICLE, "fuel_type": "plug_in_hybrid"}

#: The same vehicle as the Israeli register identifies it, narrowed by exactly
#: the dimensions the catalog also states -- and by no more, so the ambiguous
#: model year stays genuinely ambiguous.
GOVERNMENT_REQUEST = {"resource_id": WLTP_RESOURCE_ID, "make": "Toyota",
                      "commercial_model": "RAV4", "market": "IL",
                      "fuel_type": "plug_in_hybrid", "propulsion_technology": "plug_in",
                      "drivetrain": "awd"}

#: The saved official page, as Toyota Israel itself names the model.
WEB_REQUEST = {"document_id": "toyota_il_rav4_phev", "make": "Toyota",
               "commercial_model": "RAV4 Plug-in", "market": "IL"}


def proof_registry() -> ToolRegistry:
    return ToolRegistry([YedaVehicleCatalogTool(), GovernmentVehicleRegistryTool(),
                         ToyotaArchivedModelDocumentTool()])


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


def government_result(**overrides):
    """Run the real registered registry operation against the pinned pages."""
    payload = {**GOVERNMENT_REQUEST, "model_year": 2021,
               "expected_record_id": GOVERNMENT_RECORD_ID_2021, **overrides}
    return proof_registry().execute("gov_il.vehicle_registry", "get_model_record",
                                    PROOF_CONTEXT, payload)


def government_bundle(result=None):
    record = ToolCallRecord(task_id="proof-gov", call_id="gov-1",
                            tool="gov_il.vehicle_registry", operation="get_model_record",
                            result=result if result is not None else government_result())
    return proof_evidence_mappers().map(record)


def web_result(**overrides):
    """Run the real registered saved-document operation against the page."""
    return proof_registry().execute("toyota.archived_model_document",
                                    "read_archived_model_document", PROOF_CONTEXT,
                                    {**WEB_REQUEST, **overrides})


def web_bundle(result=None):
    record = ToolCallRecord(task_id="proof-web", call_id="web-1",
                            tool="toyota.archived_model_document",
                            operation="read_archived_model_document",
                            result=result if result is not None else web_result())
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

def test_none_of_r5s_proof_tools_reached_the_production_registry():
    """R5's three proof tools are registered nowhere but in the proof registry.

    The production registry was empty when this test was written and is not
    any more: Catalog PR3 registers ONE tool, the bounded Government catalog
    read, and CODE-2 makes even that one conditional on the catalog execution
    flag. What still has to be true -- and is what this test now says -- is
    that none of R5's own fixtures is it, at EITHER flag value.
    """
    worker_main = Path("backend/worker/main.py").read_text()
    # Pinned to the snapshot the Government preparation stage resolved after
    # the lease: still exactly one READ tool, still the wrapper, never the reader.
    assert ("tools = ToolRegistry( [GovernmentVehicleTool(repo, snapshot_key=preparation.snapshot_key)] "
            "if government_read_enabled else [])") in " ".join(worker_main.split())
    for name in ("yeda.vehicle_catalog", "gov_il.vehicle_registry",
                 "toyota.archived_model_document"):
        assert name not in worker_main


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
    # All three real source families are committed, checksum-gated, and read
    # through the SAME gate. None of them is described in prose only -- and
    # the Government family is committed as ALL THREE pages of its query, not
    # as the prefix of one.
    assert set(verified) == {"yeda", "government_package", "government_wltp_page_1",
                             "government_wltp_page_2", "government_wltp_page_3",
                             "web_toyota_rav4_phev"}
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
    """R5's three proof mappers are disjoint from the production allowlist.

    Catalog PR3 made that allowlist non-empty (one Government operation), so
    "the proof mappers are not production mappers" is now a DISJOINTNESS
    assertion rather than an emptiness one -- which is what it always meant.
    """
    assert not (proof_evidence_mappers().registered & PRODUCTION_EVIDENCE_MAPPER_OPERATIONS)
    assert PRODUCTION_EVIDENCE_MAPPER_OPERATIONS == {
        ("catalog.government_vehicle", "resolve_variant")}
    assert proof_evidence_mappers().registered == {
        ("yeda.vehicle_catalog", "get_model_variant"),
        ("gov_il.vehicle_registry", "get_model_record"),
        ("toyota.archived_model_document", "read_archived_model_document")}


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


def proof_run(*, checkpoint=None, repository=None, board=None, checkpoints=None, events=None):
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
        event_sink=(None if events is None
                    else lambda kind, payload: events.append((kind, payload))),
        verdict_sink=board.record_verification_verdict,
        resolution_sink=board.record_conflict_resolution)
    payload = {"id": str(RUN_UUID),
               "input": {"objective": "prove one real vehicle evidence path",
                         "commander_model": "compiled"}}
    if checkpoint is not None:
        payload["checkpoint"] = checkpoint
    return engine.run(payload), repository, poison, board


def test_the_whole_proof_runs_through_the_real_engine_with_zero_model_calls():
    """The acceptance path, end to end, offline, across all three sources.

    real pinned Yeda / Government / Web fixtures -> validated read-only
    ToolRegistry operations -> ToolCallRecords -> trusted operation-specific
    mappers -> versioned sources -> focused fragments and structured facts ->
    lease-guarded EvidenceBoard -> RepositoryEvidenceResolver -> R4
    deterministic verification -> FinalBuilder -> truthful R1 product outcome.
    """
    checkpoints = []
    result, repository, poison, board = proof_run(checkpoints=checkpoints)

    # 1. Not one model or provider call anywhere in the run.
    assert poison.calls == []

    # 2. All THREE real source families participated, each pinned to the
    # strongest immutable version it actually publishes.
    assert len(repository.sources) == 3
    assert {(row["source_version_kind"], row["source_version_id"])
            for row in repository.sources.values()} == {
        ("git_commit", proof_manifest.source_entry("yeda")["commit_sha"]),
        ("dataset_version", proof_manifest.source_entry("government_wltp_page_1")["source_version"]),
        ("content_sha256", proof_manifest.source_entry("web_toyota_rav4_phev")["upstream_sha256"]),
    }
    assert {row["source_type"] for row in repository.sources.values()} == {
        YEDA_SOURCE_TYPE, GOVERNMENT_SOURCE_TYPE, WEB_SOURCE_TYPE}

    # 3. Durable evidence was written in the contract's order, under the lease,
    # and every claim is located.
    assert repository.writes[:4] == ["source", "fragment", "fragment", "fragment"]
    assert repository.writes.count("source") == 3
    assert len(repository.claims) == 9 and len(repository.fragments) == 9
    assert all(row["evidence_locator"] for row in repository.claims.values())
    # A structured record is projected; a document is quoted. The fragment type
    # is decided by the locator shape, so neither can impersonate the other.
    assert {row["fragment_type"] for row in repository.fragments.values()} == \
        {"structured_projection", "verbatim_excerpt"}

    # 4. Every claim was settled DETERMINISTICALLY, with durable support.
    assert len(repository.verdicts) == 9
    assert {row["verdict"] for row in repository.verdicts.values()} == {"verified"}
    assert {row["verification_mode"] for row in repository.verdicts.values()} == \
        {"deterministic_structured"}
    assert {row["verifier_contract_version"] for row in repository.verdicts.values()} == \
        {VERIFIER_CONTRACT_VERSION}
    assert len(repository.supports) == 9

    # 5. The R1 product outcome is valid, and every source contributed a
    # verified field the answer actually rests on.
    assert validate_product_outcome(result)
    assert result["fields"]["fuel_type"][0]["value"] == "plug_in_hybrid"
    assert result["fields"]["horsepower_hp"][0]["value"] == 306
    assert result["fields"]["nominal_engine_displacement_l"][0]["value"] == 2.5
    assert result["fields"]["engine_displacement_cc"][0]["value"] == 2487
    assert result["fields"]["official_model_code"][0]["value"] == "AXAP54L ANXMBK"
    assert result["fields"]["marketing_status"][0]["value"] == "ended"
    contributing = {entry["provenance"]["task_id"]
                    for entries in result["fields"].values() for entry in entries}
    assert contributing == {"yeda_variant", "government_record", "web_archived_status"}
    # Provenance travels into the product output: every field names the claim,
    # the source and the run that produced it.
    known_sources = {row["id"] for row in repository.sources.values()}
    for entries in result["fields"].values():
        for entry in entries:
            assert entry["provenance"]["source_id"] in known_sources
            assert entry["provenance"]["run_id"] == str(RUN_UUID)

    # 6. The outcome is `partial_success / partial_result` because the evidence
    # holds BOTH usable verified material and one real unresolved item -- not
    # because anything here asked for that pair. The unresolved item is the
    # model year the register genuinely cannot answer conservatively.
    assert (result["status"], result["result_kind"]) == ("partial_success", "partial_result")
    assert result["needs_review"] == [{"task_id": "government_record_2026",
                                       "code": "R5_GOV_RECORD_AMBIGUOUS"}]

    # 7. The checkpoint carries the same verdicts, so a resume replays them.
    state = checkpoints[-1]["artifacts"]["swarm_state"]
    assert len(state["evidence_references"]) == 9
    assert {item["reason"] for item in state["verifier_state"].values()} == \
        {"R4_STRUCTURED_MATCH"}


def test_the_partial_outcome_is_decided_by_the_canonical_policy_alone():
    """`partial_success / partial_result` is derived, never asserted.

    The proof does not construct its own status: it hands the builder what the
    run found, and `decide_outcome` pairs "at least one usable verified field"
    with "at least one unresolved item". Removing either half changes the
    outcome, which is what shows the pair was earned rather than chosen.
    """
    result, _, _, board = proof_run()
    references = [EvidenceReference.model_validate(item) for item in board.references()]
    verdicts = [VerificationVerdict(claim_id=item.claim_id, verdict="verified",
                                    reason="R4_STRUCTURED_MATCH")
                for item in references]

    # The same verified evidence with NOTHING outstanding is a complete run...
    without_gap = FinalBuilder().build(references, verdicts)
    assert (without_gap["status"], without_gap["result_kind"]) == ("complete", "usable_result")
    # ...and the same unresolved item with NO usable field is not partial at all.
    without_fields = FinalBuilder().build([], [], task_failures=result["needs_review"])
    assert (without_fields["status"], without_fields["result_kind"]) == \
        ("partial_success", "no_usable_result")
    # Only both together produce what the real run produced.
    both = FinalBuilder().build(references, verdicts, task_failures=result["needs_review"])
    assert (both["status"], both["result_kind"]) == ("partial_success", "partial_result")
    assert (result["status"], result["result_kind"]) == (both["status"], both["result_kind"])


def test_the_unresolved_item_is_a_real_ambiguity_in_the_captured_records():
    """The gap in `needs_review` is a property of the data, not of the plan.

    Two committed registry rows answer the 2026 identity the catalog states,
    and they differ ONLY by trim -- which the catalog does not state. Naming
    the trim resolves it; that is what makes the refusal a real limit of the
    sources rather than a tool that cannot find anything.
    """
    with pytest.raises(ToolError) as failure:
        proof_registry().execute("gov_il.vehicle_registry", "get_model_record",
                                 PROOF_CONTEXT, {**GOVERNMENT_REQUEST, "model_year": 2026})
    assert failure.value.code == "R5_GOV_RECORD_AMBIGUOUS"

    # The two rows are real, distinct, and separated by exactly one dimension.
    resolved = [proof_registry().execute(
        "gov_il.vehicle_registry", "get_model_record", PROOF_CONTEXT,
        {**GOVERNMENT_REQUEST, "model_year": 2026, "trim": trim})
        for trim in ("SE-PLUGIN", "XSE-PLUGIN")]
    assert [item["record_id"] for item in resolved] == [37392, 37393]
    first, second = (item["variant"] for item in resolved)
    assert {key for key in first if first[key] != second[key]} == {"trim"}
    # And the same identity for 2021 has exactly ONE answer, so the 2026
    # refusal is not the tool simply being unable to select anything.
    assert government_result()["record_id"] == GOVERNMENT_RECORD_ID_2021


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
    assert counts == (3, 9, 9, 9, 9)


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
    planned = {task.task_id: only(task.tools) for task in plan.graph.tasks}
    assert {(call.name, call.operation) for call in planned.values()} == {
        ("yeda.vehicle_catalog", "get_model_variant"),
        ("gov_il.vehicle_registry", "get_model_record"),
        ("toyota.archived_model_document", "read_archived_model_document")}
    # The arguments came from the static table, never from the objective or
    # the context the caller supplied.
    assert planned["yeda_variant"].arguments == {
        "make": "Toyota", "commercial_model": "RAV4", "market": "IL",
        "model_year": 2021, "fuel_type": "plug_in_hybrid"}
    assert planned["government_record"].arguments == {
        **GOVERNMENT_REQUEST, "model_year": 2021,
        "expected_record_id": GOVERNMENT_RECORD_ID_2021}
    assert planned["web_archived_status"].arguments == WEB_REQUEST
    # The 2026 question states NO trim and NO model code, because the catalog
    # states neither. That is what makes its ambiguity real rather than staged.
    ambiguous = planned["government_record_2026"].arguments
    assert ambiguous == {**GOVERNMENT_REQUEST, "model_year": 2026}
    assert "trim" not in ambiguous and "expected_record_id" not in ambiguous
    assert "hostile" not in json.dumps(plan.model_dump(mode="json"))
    # No scope, capability or write approval is expressible in a plan at all.
    assert "scopes" not in json.dumps(plan.model_dump(mode="json"))


def test_an_unknown_proof_request_has_no_plan_at_all():
    with pytest.raises(UnknownProofRequest):
        DeterministicProofCommanderClient("some-other-vehicle")


# =============================================================================
# 10. controlled mutation: a wrong fact is rejected, whatever a model would say
# =============================================================================
#
# The wrong fact is NEVER invented data dressed up as a source. Each case below
# takes a REAL captured fact and changes exactly one thing about it, so what is
# being tested is the comparison contract rather than a fixture. The Yeda
# catalog's nominal engine-class label and a homologated exact displacement are
# deliberately NOT used as a wrong-fact pair: a nominal label and an exact
# measurement are different statements, not a right and a wrong answer, and
# treating them as one would assert a semantic equivalence neither source makes.

def captured_reference(board, field: str):
    """One REAL durable claim of the completed proof, as the verifier sees it."""
    return next(EvidenceReference.model_validate(item) for item in board.references()
                if item["field"] == field)


def settle(repository, reference):
    """Settle ONE reference against the real durable evidence, with no model.

    The verifier's gateway is poisoned, so a case that fell through to the
    grounded model verifier would raise instead of returning a verdict --
    which is what makes "rejected regardless of any model behaviour" a proof
    rather than an assertion about a mock's scripted answer.
    """
    verifier = Verifier(gateway=PoisonGateway(), model="unused",
                        resolver=RepositoryEvidenceResolver(repository, run_id=RUN_UUID))
    plan = verifier.prepare([reference], conflict_claim_ids=set(), existing_verdicts={})
    # Zero batches means deterministic code settled it: no model call was even
    # planned, let alone made.
    assert plan.batches == ()
    return only(plan.settled)


@pytest.mark.parametrize("label, mutation, expected", [
    ("a wrong value", {"value": 305}, "R4_VALUE_MISMATCH"),
    ("an unconvertible unit", {"unit": "kw"}, "R4_UNIT_NOT_CONVERTIBLE"),
    ("no unit at all", {"unit": None}, "R4_UNIT_MISSING"),
    ("a wrong model year", {"time_scope": {"year_start": 2019, "year_end": 2020}},
     "R4_SCOPE_MISMATCH"),
    ("a wrong market", {"market": "DE"}, "R4_SCOPE_MISMATCH"),
    ("a wrong variant identity",
     {"identity": {"body_style": "SUV", "drivetrain": "FWD", "engine": "2.5L",
                   "transmission": "cvt"}}, "R4_IDENTITY_MISMATCH"),
    ("a mismatched source version", {"source_version": "git_commit:" + "a" * 40},
     "R4_SOURCE_VERSION_MISMATCH"),
])
def test_one_controlled_mutation_of_a_real_fact_is_always_rejected(label, mutation, expected):
    _, repository, _, board = proof_run()
    reference = captured_reference(board, "horsepower_hp")
    verdict = settle(repository, reference.model_copy(update=mutation))
    assert verdict.verdict == "rejected", label
    assert verdict.reason == expected, label
    assert verdict.mode == "deterministic_structured"
    # The unmutated fact still verifies against the same durable evidence, so
    # the rejection is the mutation's doing and nothing else.
    assert settle(repository, reference).verdict == "verified"


def test_a_rejected_fact_never_reaches_the_product_fields():
    """A rejected claim is preserved in history and excluded from the answer."""
    _, repository, _, board = proof_run()
    wrong = captured_reference(board, "horsepower_hp").model_copy(update={"value": 305})
    verdict = settle(repository, wrong)
    result = FinalBuilder().build([wrong], [verdict])
    assert result["fields"] == {}
    assert validate_product_outcome(result).result_kind == "no_usable_result"


# =============================================================================
# 11. replay, resume and the durable-record guarantees
# =============================================================================

def test_resume_from_every_saved_checkpoint_duplicates_no_durable_record():
    """A resumed run replays onto the SAME rows, and re-executes no task.

    Every normal checkpoint of a completed proof is resumed from, and after
    each resume the durable evidence, the verdicts and the support links are
    still exactly what one run produced.
    """
    lease = WorkerLease(RUN_UUID, "worker-r5", 1, "lease-token-r5")
    repository = ProofRepository(lease)
    checkpoints = []
    first, _, _, _ = proof_run(repository=repository, checkpoints=checkpoints)
    counts = (len(repository.sources), len(repository.fragments), len(repository.claims),
              len(repository.verdicts), len(repository.supports))
    assert counts == (3, 9, 9, 9, 9)
    assert checkpoints, "the proof produced no checkpoint to resume from"

    for index, checkpoint in enumerate(checkpoints):
        resumed, _, poison, _ = proof_run(repository=repository, checkpoint=checkpoint)
        assert poison.calls == [], f"resume {index} reached a model path"
        assert resumed == first, f"resume {index} changed the product result"
        assert (len(repository.sources), len(repository.fragments), len(repository.claims),
                len(repository.verdicts), len(repository.supports)) == counts, index


def test_a_changed_version_or_locator_creates_distinct_provenance():
    """Two versions of one source can never merge into one durable row.

    Re-acquiring the same record at a DIFFERENT source version is new
    provenance, not an update of the old row -- which is exactly why a claim
    read at one version can never be validated against another.
    """
    lease = WorkerLease(RUN_UUID, "worker-r5", 1, "lease-token-r5")
    repository = ProofRepository(lease)
    board = EvidenceBoard(repository, lease)
    acquisition = TrustedEvidenceAcquisition(board=board, mappers=proof_evidence_mappers())
    result = yeda_result()
    acquisition.acquire(ToolCallRecord(task_id="yeda_variant", call_id="yeda-1",
                                       tool="yeda.vehicle_catalog",
                                       operation="get_model_variant", result=result))
    assert len(repository.sources) == 1

    # The SAME record, read at a different commit of the same catalog.
    other = deepcopy(result)
    other["source"] = {**other["source"], "commit_sha": "b" * 40}
    acquisition.acquire(ToolCallRecord(task_id="yeda_variant", call_id="yeda-1",
                                       tool="yeda.vehicle_catalog",
                                       operation="get_model_variant", result=other))
    assert len(repository.sources) == 2
    assert {row["source_version_id"] for row in repository.sources.values()} == {
        proof_manifest.source_entry("yeda")["commit_sha"], "b" * 40}
    # Same locators, distinct sources: six claims, not three overwritten ones.
    assert len(repository.claims) == 6 and len(repository.fragments) == 6


# =============================================================================
# 12. not_found stays unreachable without a typed trusted negative
# =============================================================================

def test_empty_or_unmatched_ordinary_evidence_never_becomes_not_found():
    """An absent answer is not a proven absence.

    `not_found` requires a TYPED, tool-backed trusted negative. Nothing in this
    proof constructs one -- the read-only tools raise a static "no such
    variant" error, which is a failed lookup, not a source proving the vehicle
    does not exist -- so an empty result stays `no_usable_result`.
    """
    empty = FinalBuilder().build([], [])
    outcome = validate_product_outcome(empty)
    assert (outcome.status, outcome.result_kind) == ("partial_success", "no_usable_result")
    assert empty["needs_review"] == [{"code": NO_USABLE_RESULT_CODE}]

    # An unmatched lookup is a tool ERROR, never a typed negative result.
    with pytest.raises(ToolError) as failure:
        proof_registry().execute("yeda.vehicle_catalog", "get_model_variant", PROOF_CONTEXT,
                                 {**NARROWED, "model_year": 1998})
    assert failure.value.code == "R5_YEDA_VARIANT_NOT_FOUND"


def test_the_proof_constructs_no_trusted_negative_result_anywhere():
    """The one type that could reach `not_found` is never built here."""
    proof_sources = list(Path("backend/testing/r5_proof").rglob("*.py"))
    assert proof_sources
    for path in proof_sources:
        assert "TrustedNegativeResult" not in path.read_text()
    # And the engine still passes the builder no `trusted_negative=` argument,
    # so `not_found` stays unreachable from a real run.
    engine_source = Path("backend/engines/swarm_v2/engine.py").read_text()
    assert "trusted_negative=" not in engine_source


# =============================================================================
# 13. nothing internal leaks to a browser-facing surface
# =============================================================================

def test_run_events_carry_identifiers_only_and_no_source_material():
    """A public run event is not an evidence channel.

    The proof's fixtures contain real source text, real URLs and Hebrew
    titles. None of it may reach the event stream the browser polls.
    """
    events = []
    _, repository, _, board = proof_run(events=events)
    assert events, "the proof emitted no events"
    serialized = json.dumps(events, ensure_ascii=False)
    for leaked in ("plug_in_hybrid", "RAV4", "github.com", "reliabilityAIModelsR2",
                   "make=Toyota", "yeda.models.860"):
        assert leaked not in serialized, leaked
    # Every payload value is a bounded identifier or a static code.
    for _, payload in events:
        for value in payload.values():
            assert value is None or isinstance(value, (str, int, float, bool))
            assert not isinstance(value, str) or len(value) <= 200


def test_the_product_output_carries_no_internal_evidence_material():
    """The browser-visible result names provenance; it never quotes evidence."""
    result, repository, _, _ = proof_run()
    serialized = json.dumps(result, ensure_ascii=False)
    # No fragment text, no locator key, no support link, no verdict internals.
    for leaked in ("make=Toyota", "structured_projection", "record_field",
                   "content_hash", "fragment_id", "verification_mode",
                   "R4_STRUCTURED_MATCH", "commit_sha"):
        assert leaked not in serialized, leaked
    # Durable fragment text exists, and stayed on the service side.
    assert any("make=Toyota" in row["fragment_text"] for row in repository.fragments.values())


# =============================================================================
# 14. the captured Government and Web sources: provenance, gates and refusals
# =============================================================================
#
# Everything below is a property of the material that was actually captured on
# 2026-09-14 and committed byte-for-byte. Nothing here mocks a source, and
# nothing asserts a value that was not read out of the committed bytes.

#: The archived-status sentence the official page must carry, verbatim:
#: "marketing of the RAV4 Plug-in model has ended."
ENDED_SENTENCE = "שיווק הדגם ראב4 פלאג-אין הסתיים."

#: The same sentence as the RAW markup writes it -- inside a `<strong>`, with
#: non-breaking spaces the visible-text projection normalizes away. Editing the
#: committed bytes means editing this form, not the normalized one.
RAW_ENDED_SENTENCE = "שיווק הדגם\u00a0ראב4 פלאג-אין הסתיים.".replace("\\u00a0", "\u00a0")


@pytest.mark.parametrize("key", ["government_package", "government_wltp_page_1",
                                 "government_wltp_page_2", "government_wltp_page_3"])
def test_a_captured_government_fixture_is_the_exact_response_that_was_received(key):
    """An `exact_response` fixture IS the response, not a rendering of it.

    `fixture_sha256` is the digest of the committed bytes and `upstream_sha256`
    is the digest of what the server sent. For an `exact_response` they are
    necessarily equal, and `upstream_committed` says so -- which is exactly what
    lets the two fixtures that are NOT the whole response say the opposite.
    """
    entry, payload = proof_manifest.verify_fixture(key)
    assert entry["fixture_kind"] == "exact_response"
    assert entry["upstream_committed"] is True
    assert entry["fixture_sha256"] == entry["upstream_sha256"]
    assert entry["fixture_byte_count"] == entry["response_byte_count"] == len(payload)
    assert entry["http_status"] == 200
    assert entry["redirect_chain"] == []
    # Requested and final URL agree, on one approved host, over HTTPS.
    assert entry["requested_url"] == entry["final_url"]
    assert entry["final_url"].startswith("https://")


@pytest.mark.parametrize("key, kind", [("yeda", "exact_record_subset"),
                                       ("web_toyota_rav4_phev", "deterministic_projection")])
def test_a_fixture_that_is_not_the_whole_upstream_object_says_so(key, kind):
    """Two digests, two objects, and the manifest never conflates them.

    Neither the 7.3 MB Yeda catalog nor the 356 KB Toyota page is committed.
    What is committed is a bounded piece of each, and the manifest records the
    committed digest and the UPSTREAM digest as separate fields with
    `upstream_committed: false` -- so a reader is never invited to assume the
    stronger claim that the repository holds the whole object.
    """
    entry, payload = proof_manifest.verify_fixture(key)
    assert entry["fixture_kind"] == kind
    assert entry["upstream_committed"] is False
    assert entry["fixture_sha256"] != entry["upstream_sha256"]
    assert entry["fixture_byte_count"] == len(payload)
    # The SOURCE VERSION is still the whole upstream object, never the excerpt.
    if entry.get("source_version_kind") == "content_sha256":
        assert entry["source_version"] == entry["upstream_sha256"]
        assert entry["fixture_byte_count"] < entry["response_byte_count"]


def test_absent_response_metadata_is_recorded_as_absent_and_checked_both_ways():
    """No ETag, no Last-Modified, no revision -- said out loud, and enforced.

    None of the captured responses carried a validator, and the CKAN resource
    publishes no `revision_id`. That is recorded as an explicit null plus a
    declaration, and the manifest gate checks BOTH directions, so neither a
    silently invented value nor an undeclared gap can survive review.
    """
    for key in ("government_wltp_page_1", "web_toyota_rav4_phev"):
        entry = proof_manifest.source_entry(key)
        assert entry["absent_source_metadata"], key
        for name in entry["absent_source_metadata"]:
            assert name in proof_manifest.OPTIONAL_SOURCE_METADATA
            assert name in entry and entry[name] is None, (key, name)

    # Inventing one without withdrawing the declaration is a refusal...
    invented = {**proof_manifest.source_entry("government_wltp_page_1"),
                "etag": 'W/"deadbeef"'}
    with pytest.raises(proof_manifest.ProofManifestError) as failure:
        proof_manifest._validate_source(invented)
    assert failure.value.reason_code == "R5_MANIFEST_INVALID"
    # ...and so is a null nobody declared, which is the shape a later edit
    # would need in order to fill a gap in quietly.
    undeclared = {**proof_manifest.source_entry("government_wltp_page_1"),
                  "absent_source_metadata": ["etag", "resource_revision_id"]}
    with pytest.raises(proof_manifest.ProofManifestError) as second:
        proof_manifest._validate_source(undeclared)
    assert second.value.reason_code == "R5_MANIFEST_INVALID"


@pytest.mark.parametrize("key, relative, before, after", [
    ("government_wltp_page_1", "government/wltp_page_000001.json",
     '"nefah_manoa":2487', '"nefah_manoa":2488'),
    # The third page carries no selected row, and is gated exactly as hard:
    # an unread page is still evidence that the query is complete.
    ("government_wltp_page_3", "government/wltp_page_000003.json",
     '"offset": 200', '"offset": 300'),
    ("web_toyota_rav4_phev", "web/toyota_il_rav4_phev.visible_text.txt",
     "הסתיים", "ממשיך"),
])
def test_one_modified_captured_byte_fails_the_fixture_closed(relocated_fixtures, key,
                                                             relative, before, after):
    """A single edited byte of a captured response stops the proof.

    Both edits are exactly the plausible kind: a homologated displacement off
    by one, and an archived-status word turned into its opposite. Neither
    reaches a parser, because the digest is compared before the bytes are
    decoded at all.
    """
    def flip(root):
        path = root / relative
        raw = path.read_bytes()
        assert before.encode("utf-8") in raw, "the fixture no longer holds the text under test"
        path.write_bytes(raw.replace(before.encode("utf-8"), after.encode("utf-8"), 1))

    relocated_fixtures(flip)
    with pytest.raises(proof_manifest.ProofManifestError) as failure:
        proof_manifest.verify_fixture(key)
    assert failure.value.reason_code in {"R5_FIXTURE_CHECKSUM_MISMATCH",
                                         "R5_FIXTURE_BYTE_COUNT_MISMATCH"}


def test_a_truncated_captured_fixture_is_classified_as_truncation(relocated_fixtures):
    """A short read is a size failure, not a generic checksum failure."""
    def truncate(root):
        path = root / "web" / "toyota_il_rav4_phev.visible_text.txt"
        path.write_bytes(path.read_bytes()[:-64])

    relocated_fixtures(truncate)
    with pytest.raises(proof_manifest.ProofManifestError) as failure:
        proof_manifest.verify_fixture("web_toyota_rav4_phev")
    assert failure.value.reason_code == "R5_FIXTURE_BYTE_COUNT_MISMATCH"


def test_the_government_source_is_pinned_to_the_datasets_own_published_version():
    """A retrieval time is not a version, and neither is a page digest.

    The register's version is the DATASET's own `last_modified`, read from the
    CKAN package metadata captured alongside the rows. The resource publishes
    no `revision_id`, so none is recorded and none is invented.
    """
    entry = proof_manifest.source_entry("government_wltp_page_1")
    assert entry["source_version_kind"] == "dataset_version"
    assert entry["resource_id"] == WLTP_RESOURCE_ID
    assert entry["ckan_package_id"] == "degem-rechev-wltp"
    assert entry["resource_revision_id"] is None

    package = json.loads((proof_manifest.FIXTURE_ROOT / "government" /
                          "package_show.json").read_text(encoding="utf-8"))
    resource = only([item for item in package["result"]["resources"]
                     if item["id"] == WLTP_RESOURCE_ID])
    # The manifest's version IS the dataset's own field, not a copy that drifted.
    assert entry["source_version"] == resource["last_modified"]
    # The resource carries no `revision_id` key AT ALL, which is why the
    # manifest records the absence rather than a value.
    assert "revision_id" not in resource
    # Its published `hash` is kept as provenance and is deliberately not the
    # version: it is an MD5 of the full CSV export, not of the JSON served.
    assert entry["resource_content_hash"] == resource["hash"]
    assert entry["source_version_kind"] != "content_sha256"
    # And it is what the tool reports and what the mapper pins the evidence to.
    result = government_result()
    assert result["source"]["dataset_version"] == entry["source_version"]
    assert government_bundle(result).source.version.version_key == \
        f"dataset_version:{entry['source_version']}"
    assert result["source"]["publisher"] == "ministry_of_transport"


def test_the_committed_record_keeps_its_original_government_id():
    """The row is addressed by the register's OWN `_id`, and nothing else."""
    result = government_result()
    assert result["record_id"] == GOVERNMENT_RECORD_ID_2021
    assert result["durable_record_id"] == f"gov_il.wltp.{GOVERNMENT_RECORD_ID_2021}"
    assert result["record_locator"]["government_id"] == GOVERNMENT_RECORD_ID_2021

    # The manifest names the same `_id` and the exact index it sits at inside
    # the committed response page, so the row is findable in the raw bytes.
    entry = proof_manifest.source_entry("government_wltp_page_1")
    locator = entry["record_locator"]
    assert locator["record_ids"] == [GOVERNMENT_RECORD_ID_2021]
    page = json.loads((proof_manifest.FIXTURE_ROOT /
                       entry["fixture_path"]).read_text(encoding="utf-8"))
    index = locator["record_indexes"][str(GOVERNMENT_RECORD_ID_2021)]
    assert page["result"]["records"][index]["_id"] == GOVERNMENT_RECORD_ID_2021
    assert result["record_locator"]["record_index"] == index
    # Every durable locator names that row.
    assert all(fact.locator.record_id == result["durable_record_id"]
               for fact in government_bundle(result).facts)


@pytest.mark.parametrize("label, payload, expected", [
    ("a wrong asserted record id", {"expected_record_id": 37392},
     "R5_GOV_RECORD_ID_MISMATCH"),
    ("an identity the register does not hold", {"commercial_model": "Corolla"},
     "R5_GOV_RECORD_NOT_FOUND"),
    ("a market outside the dataset's documented scope", {"market": "DE"},
     "R5_GOV_MARKET_OUT_OF_SCOPE"),
    ("a resource no committed page belongs to",
     {"resource_id": "5e87a7a1-2f6f-41c1-8aec-7216d52a6cf6"}, "R5_GOV_RESOURCE_UNKNOWN"),
])
def test_the_government_tool_fails_closed_rather_than_choosing(label, payload, expected):
    """Every wrong request refuses; none of them answers approximately."""
    request = {**GOVERNMENT_REQUEST, "model_year": 2021,
               "expected_record_id": GOVERNMENT_RECORD_ID_2021, **payload}
    with pytest.raises(ToolError) as failure:
        proof_registry().execute("gov_il.vehicle_registry", "get_model_record",
                                 PROOF_CONTEXT, request)
    assert failure.value.code == expected, label


def test_naming_a_record_id_can_never_resolve_an_ambiguity():
    """`expected_record_id` is an assertion, never a tiebreak.

    If it could select, a caller could resolve a genuine ambiguity by fiat,
    which is exactly what the refusal exists to prevent. So a 2026 request
    naming one of the two matching rows fails just as it does naming none.
    """
    for named in (None, 37392, 37393):
        request = {**GOVERNMENT_REQUEST, "model_year": 2026}
        if named is not None:
            request["expected_record_id"] = named
        with pytest.raises(ToolError) as failure:
            proof_registry().execute("gov_il.vehicle_registry", "get_model_record",
                                     PROOF_CONTEXT, request)
        assert failure.value.code == "R5_GOV_RECORD_AMBIGUOUS", named


def test_the_government_tool_reads_only_closed_code_vocabularies():
    """A Hebrew name is never parsed; a code is mapped and its name checked.

    The decoded row must agree with the register's own code/name pairing. A row
    whose `delek_cd` no longer travels with the `delek_nm` this proof was
    reviewed against is drift, and drift must stop the selection rather than be
    interpreted.
    """
    result = government_result()
    upstream = dict(result["upstream_fields"])
    assert (upstream["delek_cd"], upstream["delek_nm"]) == (7, "חשמל/בנזין")
    assert (upstream["technologiat_hanaa_cd"], upstream["technologiat_hanaa_nm"]) == \
        (2, "PLUG IN")
    assert upstream["hanaa_nm"] == "4X4"
    assert result["variant"]["fuel_type"] == "plug_in_hybrid"
    assert result["variant"]["propulsion_technology"] == "plug_in"
    assert result["variant"]["drivetrain"] == "awd"

    tool = GovernmentVehicleRegistryTool()
    assert tool._decode(upstream) is not None
    # A drifted pairing is skipped, so the request finds nothing rather than
    # reading the row through a vocabulary it no longer matches.
    assert tool._decode({**upstream, "delek_nm": "something else"}) is None
    assert tool._decode({**upstream, "delek_cd": 99}) is None
    # And a row whose fuel and propulsion statements disagree is not material.
    assert tool._decode({**upstream, "technologiat_hanaa_cd": 1,
                         "technologiat_hanaa_nm": "היברידי רגיל"}) is None


def test_a_power_figure_with_unresolved_semantics_is_a_gap_not_a_fact():
    """`koah_sus` is captured, reported as unmapped, and never becomes evidence.

    Across the committed plug-in rows it takes both engine-scale and
    system-scale values, and the dataset defines neither. A field whose meaning
    is unresolved IN THE SOURCE cannot be evidence, and mapping it to
    `horsepower_hp` on the strength of its name is exactly the guess this proof
    refuses to make.
    """
    result = government_result()
    unmapped = {item["field"] for item in result["unmapped_fields"]}
    assert "koah_sus" in unmapped
    assert {field for field, _ in UNMAPPED_FIELDS} == unmapped
    # It is not in the operation's declared variant output at all...
    assert "koah_sus" not in result["variant"]
    assert "koah_sus" not in result["upstream_fields"]
    # ...and no Government fact claims a power field.
    assert {field for field, _, _ in GOVERNMENT_FACT_FIELDS} == {
        "engine_displacement_cc", "fuel_type", "official_model_code"}
    assert not any(fact.field_key == "horsepower_hp" for fact in government_bundle().facts)
    # The stated reason is checkable: the captured rows really do disagree.
    page = json.loads((proof_manifest.FIXTURE_ROOT / "government" /
                       "wltp_page_000002.json").read_text(encoding="utf-8"))
    plug_in_power = {row["koah_sus"] for row in page["result"]["records"]
                     if row.get("delek_cd") == 7}
    assert len(plug_in_power) > 1, plug_in_power


# --- the pinned Government query is COMPLETE --------------------------------
#
# The R5 capture asked `data.gov.il` one bounded question -- `q=RAV4` over the
# WLTP resource, 100 rows a page -- and the datastore answered 233 rows in
# three pages. The first R5 round committed two of them, because neither held a
# row the proof selects. That was wrong in a way no single page can reveal:
# every page reports the full total of 233, so 200 committed rows look exactly
# like a complete query. The proof was scanning a PREFIX while its provenance
# named the whole query.
#
# All three pages are committed now, and the tests below hold the completeness
# itself -- not just the bytes -- to the gate.

#: The third page's committed identity, stated here so a re-import that changed
#: it has to change this test too.
PAGE_3_SHA256 = "5f96a2ff3fbdd1f73602e5f89b5446c9e8e5c2ea261b742f88ba84ced308cedc"
PAGE_3_BYTES = 89673

#: The pinned query, as the datastore itself served it.
PAGE_ROW_COUNTS = (100, 100, 33)
PAGE_OFFSETS = (0, 100, 200)
QUERY_TOTAL = 233


def government_page(key, root=None):
    """One committed page's parsed body, read through the checksum gate."""
    _, document = proof_manifest.load_fixture(key)
    return document["result"]


def repin(root, key, mutate_body):
    """Rewrite one committed page AND re-pin its manifest digest.

    Tamper-detection is proven separately, by the byte-level tests above. What
    these tests need is for the checksum gate to PASS, so that the property
    under test is the pagination gate BEHIND it. A mutation that only edited
    bytes would stop at the digest and would prove nothing about whether the
    pages are validated as one query.
    """
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = manifest["sources"][key]
    path = root / entry["fixture_path"]
    body = json.loads(path.read_text(encoding="utf-8"))
    mutate_body(body)
    payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    path.write_bytes(payload)
    entry["fixture_sha256"] = entry["upstream_sha256"] = hashlib.sha256(payload).hexdigest()
    entry["fixture_byte_count"] = entry["response_byte_count"] = len(payload)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")


def edit_manifest(root, mutate):
    """Change the committed provenance without touching a captured byte."""
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mutate(manifest)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")


def government_refusal(**overrides):
    """Run the real registry operation and return the code it refused with."""
    with pytest.raises(ToolError) as failure:
        government_result(**overrides)
    return failure.value.code


def registry_refusal(payload):
    """Refuse a request built from scratch, asserting no record id at all."""
    with pytest.raises(ToolError) as failure:
        proof_registry().execute("gov_il.vehicle_registry", "get_model_record",
                                 PROOF_CONTEXT, payload)
    return failure.value.code


def test_the_third_page_is_committed_at_its_exact_captured_identity():
    """The page the first R5 round left out, pinned byte for byte.

    Size and digest are asserted as literals rather than read back out of the
    manifest, so a re-import that silently produced different bytes under the
    same provenance fails here instead of being re-pinned into agreement.
    """
    entry, payload = proof_manifest.verify_fixture("government_wltp_page_3")
    assert len(payload) == PAGE_3_BYTES
    assert hashlib.sha256(payload).hexdigest() == PAGE_3_SHA256
    assert entry["fixture_sha256"] == entry["upstream_sha256"] == PAGE_3_SHA256
    assert entry["fixture_byte_count"] == entry["response_byte_count"] == PAGE_3_BYTES
    assert entry["fixture_kind"] == "exact_response" and entry["upstream_committed"] is True
    assert entry["retrieved_at_utc"] == "2026-09-14T16:11:13.272Z"
    assert entry["http_status"] == 200 and entry["redirect_chain"] == []
    assert entry["query"] == {"resource_id": WLTP_RESOURCE_ID, "limit": "100",
                              "offset": "200", "q": "RAV4"}


def test_the_third_pages_locator_is_page_level_and_claims_no_record():
    """No row was selected here, so no row is named here.

    Pages one and two carry `record_ids` because the proof reads a row from
    each. This page carries none, and says so, rather than being given a
    plausible-looking id and index to make the three entries look alike.
    """
    locator = proof_manifest.source_entry("government_wltp_page_3")["record_locator"]
    assert "record_ids" not in locator and "record_indexes" not in locator
    assert locator["selected_records"].startswith("none;")
    assert locator["page"] == {"requested_offset": 200, "requested_limit": 100,
                               "returned_record_count": 33, "reported_total": QUERY_TOTAL,
                               "query_token": "RAV4", "resource_id": WLTP_RESOURCE_ID}
    # The pages that DO hold a selected row still name it, and now also carry
    # the same page-level position, so all three locate themselves in the query.
    for key, ids in (("government_wltp_page_1", [GOVERNMENT_RECORD_ID_2021]),
                     ("government_wltp_page_2", [37392, 37393])):
        other = proof_manifest.source_entry(key)["record_locator"]
        assert other["record_ids"] == ids
        assert other["page"]["reported_total"] == QUERY_TOTAL


def test_the_committed_pages_are_the_whole_query_and_nothing_twice():
    """100 + 100 + 33 = 233, which is the total the datastore itself reports.

    Every page states the same total, every page sits at the offset the page
    before it ends at, and no registry row is reachable from two pages -- which
    is what makes 233 the number of DISTINCT rows the proof scans rather than
    the number of times it read something.
    """
    counts, offsets, identities = [], [], []
    for key, expected_offset in zip(WLTP_PAGE_SOURCE_KEYS, PAGE_OFFSETS):
        result = government_page(key)
        assert result["resource_id"] == WLTP_RESOURCE_ID and result["q"] == "RAV4"
        assert result["limit"] == 100
        assert result["total"] == QUERY_TOTAL
        assert result["offset"] == expected_offset
        counts.append(len(result["records"]))
        offsets.append(result["offset"])
        identities.extend(record["_id"] for record in result["records"])

    assert tuple(counts) == PAGE_ROW_COUNTS
    assert tuple(offsets) == PAGE_OFFSETS
    # No gap and no overlap: each page starts exactly where the last one ended.
    assert offsets == [sum(counts[:index]) for index in range(len(counts))]
    assert sum(counts) == QUERY_TOTAL
    assert len(identities) == len(set(identities)) == QUERY_TOTAL
    # And the whole query still fits inside the bound one call may scan.
    assert QUERY_TOTAL <= MAX_SCANNED_RECORDS


def test_the_two_page_query_this_correction_replaces_now_fails_closed():
    """The exact defect being corrected, asserted as a refusal.

    With the plan shortened back to the two pages R5 originally committed,
    every page still passes its own checks -- right offset, right size, right
    query, and the same honest total of 233 -- and the scan still finds the
    2021 row. The ONLY thing wrong is that 200 rows are not 233, and that is
    now the thing that stops the call.
    """
    two_pages = WLTP_PAGE_PLAN[:2]
    assert [key for key, _, _ in two_pages] == ["government_wltp_page_1",
                                                "government_wltp_page_2"]
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(government_module, "WLTP_PAGE_PLAN", two_pages)
        assert government_refusal() == "R5_GOV_PAGINATION_INCOMPLETE"


def test_the_third_page_adds_no_answer_to_either_identity_the_proof_asks():
    """Completeness changed what is PROVEN, not what is ANSWERED.

    This is the honest outcome of committing the page: 2021 still resolves to
    exactly one row and 2026 is still ambiguous between exactly two. The
    difference is that both statements are now about the whole query instead of
    about its first 200 rows.
    """
    assert government_result()["record_id"] == GOVERNMENT_RECORD_ID_2021
    assert registry_refusal({**GOVERNMENT_REQUEST,
                             "model_year": 2026}) == "R5_GOV_RECORD_AMBIGUOUS"

    # Read directly from the third page's own rows: none of them answers
    # either identity, so neither answer could have come from here.
    records = government_page("government_wltp_page_3")["records"]
    assert len(records) == 33
    matching = [record["_id"] for record in records
                if record.get("shnat_yitzur") in (2021, 2026)
                and MAKE_BY_TOZAR.get(str(record.get("tozar"))) == "Toyota"
                and str(record.get("kinuy_mishari")).strip() == "RAV4"
                and record.get("delek_cd") == 7 and record.get("technologiat_hanaa_cd") == 2
                and record.get("hanaa_cd") == 3]
    assert matching == []

    # The page is NOT empty of plug-in RAV4s, which is the point: it holds two
    # 2026 plug-in four-wheel-drive Toyotas the register calls "RAV4 PLUG IN"
    # and "RAV4 PHEV". They stay out of the 2026 answer because the commercial
    # model is matched EXACTLY and never on a substring -- so committing this
    # page tests that promise against real rows instead of asserting it.
    near_misses = {record["_id"]: str(record["kinuy_mishari"]).strip() for record in records
                   if record.get("delek_cd") == 7 and record.get("hanaa_cd") == 3
                   and record.get("shnat_yitzur") == 2026}
    assert near_misses == {37367: "RAV4 PLUG IN", 37372: "RAV4 PHEV"}
    assert government_result()["record_id"] not in near_misses


def test_a_matching_row_on_the_third_page_is_actually_reached(relocated_fixtures):
    """Proof that the third page is SCANNED, not merely committed.

    A page can be checksum-gated, counted and still never read. So one row on
    the third page is turned into a genuine second answer to the 2021 identity
    -- the committed 2021 row, re-identified and re-trimmed -- and the 2021
    question must stop being answerable. If the scan did not reach this page,
    it would still return one row and this test would fail.
    """
    def plant(body):
        source = government_page("government_wltp_page_1")["records"][74]
        assert source["_id"] == GOVERNMENT_RECORD_ID_2021
        body["result"]["records"][0] = {**deepcopy(source), "_id": 999999,
                                        "ramat_gimur": "PRIME AWD XSE"}

    root = relocated_fixtures()
    # Before the mutation, in this very copy, 2021 has exactly one answer.
    assert government_result()["record_id"] == GOVERNMENT_RECORD_ID_2021
    repin(root, "government_wltp_page_3", plant)
    assert government_refusal() == "R5_GOV_RECORD_AMBIGUOUS"
    # And the planted row is reachable by name, so the refusal is the two rows
    # it should be rather than any other failure that also happens to refuse.
    assert government_result(trim="PRIME AWD XSE",
                             expected_record_id=999999)["record_id"] == 999999


@pytest.mark.parametrize("label, key, mutate, expected", [
    ("a page that is no longer committed at all", None,
     lambda root: edit_manifest(root, lambda m: m["sources"].pop("government_wltp_page_3")),
     "R5_GOV_PAGE_MISSING"),
    ("a gap between the second page and the third", "government_wltp_page_3",
     lambda body: body["result"].update(offset=300), "R5_GOV_PAGE_OFFSET_UNEXPECTED"),
    ("a page served at a different page size", "government_wltp_page_3",
     lambda body: body["result"].update(limit=33), "R5_GOV_PAGE_OFFSET_UNEXPECTED"),
    ("a page that answered a different question", "government_wltp_page_3",
     lambda body: body["result"].update(q="COROLLA"), "R5_GOV_PAGE_QUERY_MISMATCH"),
    ("a page that belongs to another resource", "government_wltp_page_3",
     lambda body: body["result"].update(
         resource_id="5e87a7a1-2f6f-41c1-8aec-7216d52a6cf6"),
     "R5_GOV_PAGE_QUERY_MISMATCH"),
    ("pages that disagree about how many rows the query has", "government_wltp_page_3",
     lambda body: body["result"].update(total=200), "R5_GOV_PAGE_TOTAL_INCONSISTENT"),
    ("a page that lost a row", "government_wltp_page_3",
     lambda body: body["result"]["records"].pop(), "R5_GOV_PAGE_COUNT_UNEXPECTED"),
    ("a page that gained one", "government_wltp_page_3",
     lambda body: body["result"]["records"].append(
         deepcopy(body["result"]["records"][0])), "R5_GOV_PAGE_COUNT_UNEXPECTED"),
    ("one registry row reachable from two pages", "government_wltp_page_3",
     lambda body: body["result"]["records"][0].update(_id=GOVERNMENT_RECORD_ID_2021),
     "R5_GOV_RECORD_ID_DUPLICATED"),
    ("a row with no registry identity at all", "government_wltp_page_3",
     lambda body: body["result"]["records"][0].update(_id="36327"),
     "R5_GOV_FIXTURE_INVALID"),
])
def test_an_incomplete_or_inconsistent_pinned_query_fails_closed(relocated_fixtures, label,
                                                                 key, mutate, expected):
    """Every way the committed query could stop being that query is a refusal.

    None of these reaches a smaller scan, a best-effort answer or a warning.
    The checksum of each edited page is re-pinned first, so what is under test
    here is the completeness gate and never the digest gate in front of it.
    """
    root = relocated_fixtures()
    if key is None:
        mutate(root)
    else:
        repin(root, key, mutate)
    assert government_refusal() == expected
    assert expected in R5_GOV_PAGINATION_REASONS or expected == "R5_GOV_FIXTURE_INVALID"


@pytest.mark.parametrize("field, value", [("requested_offset", 300), ("requested_limit", 33),
                                          ("returned_record_count", 34),
                                          ("reported_total", 200)])
def test_moving_a_page_in_its_provenance_alone_also_fails_closed(relocated_fixtures,
                                                                 field, value):
    """The manifest cannot relocate a page the response still contradicts.

    A page's position is stated three times -- in the query the capture
    recorded sending, in the manifest's own page locator, and in the body's
    echo of what it answered. Editing only the provenance leaves the other two
    disagreeing, so a page cannot be quietly re-labelled into a gap.
    """
    root = relocated_fixtures()
    edit_manifest(root, lambda manifest: manifest["sources"]["government_wltp_page_3"]
                  ["record_locator"]["page"].update({field: value}))
    assert government_refusal() == "R5_GOV_PAGE_OFFSET_UNEXPECTED"


@pytest.mark.parametrize("mutate, expected", [
    (lambda path: path.write_bytes(path.read_bytes()[:-128]),
     "R5_FIXTURE_BYTE_COUNT_MISMATCH"),
    (lambda path: path.write_bytes(path.read_bytes().replace(b'"total": 233',
                                                             b'"total": 133', 1)),
     "R5_FIXTURE_CHECKSUM_MISMATCH"),
])
def test_a_truncated_or_tampered_third_page_never_reaches_the_scan(relocated_fixtures,
                                                                   mutate, expected):
    """The digest gate runs first, for the unread page exactly as for the read ones."""
    root = relocated_fixtures()
    mutate(root / "government" / "wltp_page_000003.json")
    with pytest.raises(proof_manifest.ProofManifestError) as failure:
        proof_manifest.verify_fixture("government_wltp_page_3")
    assert failure.value.reason_code == expected
    # And the tool refuses with that same static reason rather than scanning on.
    assert government_refusal() == expected


def test_the_importer_reproduces_the_third_pages_committed_provenance():
    """The committed manifest entry is what the IMPORTER emits, not prose.

    `government_source_entry` is the one rule for a committed government
    source's provenance. Rebuilding page three's entry from the committed bytes
    and the archive facts the manifest itself records must reproduce that entry
    exactly -- so a field edited by hand into a shape the importer would never
    produce (an invented record id, a locator moved off the page it describes)
    is a test failure rather than a plausible line in a diff. Running it twice
    pins the determinism the fixture refresh depends on.
    """
    entry = dict(proof_manifest.source_entry("government_wltp_page_3"))
    archive = proof_manifest.load_manifest()["capture_archive"]
    page = entry["record_locator"]["page"]
    capture_entry = {
        "source_id": "government_wltp_q0_page_000003",
        "source_type": "government_datastore_page",
        "raw_path": entry["record_locator"]["raw_capture_path"],
        "requested_url": entry["requested_url"], "final_url": entry["final_url"],
        "redirect_chain": [], "http_status": 200,
        "finished_utc": entry["retrieved_at_utc"], "content_type": entry["content_type"],
        "resource_id": entry["resource_id"], "query_params": dict(entry["query"]),
        "query_token": page["query_token"],
        "requested_offset": page["requested_offset"],
        "requested_limit": page["requested_limit"],
        "returned_record_count": page["returned_record_count"],
        "reported_total": page["reported_total"],
        "sha256": entry["upstream_sha256"], "byte_count": entry["response_byte_count"],
        "headers": {},
    }
    resource = {"hash": entry["resource_content_hash"],
                "metadata_modified": entry["resource_metadata_modified"],
                "revision_id": entry["resource_revision_id"]}
    path = proof_manifest.FIXTURE_ROOT / entry["fixture_path"]

    def build():
        return r5_capture_fixtures.government_source_entry(
            {"tool": archive["tool"], "capture_id": archive["capture_id"]}, capture_entry,
            "government_wltp_page_3", entry["fixture_path"], path, entry["fixture_sha256"],
            entry["fixture_byte_count"], entry["source_version"], resource)

    assert build() == entry
    assert build() == build()
    # The importer holds the page to its recorded position too, so an archive
    # that described this page wrongly could never have produced this entry.
    moved = {**capture_entry, "requested_offset": 300}
    with pytest.raises(r5_capture_fixtures.CaptureRefused):
        r5_capture_fixtures.government_source_entry(
            {"tool": archive["tool"], "capture_id": archive["capture_id"]}, moved,
            "government_wltp_page_3", entry["fixture_path"], path, entry["fixture_sha256"],
            entry["fixture_byte_count"], entry["source_version"], resource)


def test_the_third_page_is_not_in_the_selected_record_table():
    """A page is imported because it completes the query, not because it holds a row.

    `GOVERNMENT_RECORD_IDS` names the rows the proof actually reads, and the
    third page is deliberately absent from it. That absence is what keeps the
    importer from being handed an invented id to make the three pages look
    uniform.
    """
    assert set(r5_capture_fixtures.IMPORTED_GOVERNMENT.values()) == {
        ("government/package_show.json", "government_package"),
        ("government/wltp_page_000001.json", "government_wltp_page_1"),
        ("government/wltp_page_000002.json", "government_wltp_page_2"),
        ("government/wltp_page_000003.json", "government_wltp_page_3"),
    }
    assert set(r5_capture_fixtures.GOVERNMENT_RECORD_IDS) == {"government_wltp_page_1",
                                                              "government_wltp_page_2"}
    assert "government_wltp_page_3" not in r5_capture_fixtures.GOVERNMENT_RECORD_IDS


# --- the official saved Web document ----------------------------------------

def test_the_web_document_is_pinned_to_the_digest_of_its_whole_body():
    """The page publishes no validator, so its content digest is its version.

    Not a hash of the quoted spans -- the digest of the EXACT FULL captured
    response. A version computed from what happened to be selected would change
    whenever the selection did.
    """
    entry = proof_manifest.source_entry("web_toyota_rav4_phev")
    assert entry["absent_source_metadata"] == ["etag", "last_modified"]
    assert entry["source_version_kind"] == "content_sha256"
    # The version is the digest of the WHOLE captured response, which is NOT
    # what is committed -- the committed file is its visible-text projection.
    assert entry["source_version"] == entry["upstream_sha256"]
    committed = (proof_manifest.FIXTURE_ROOT / entry["fixture_path"]).read_bytes()
    assert hashlib.sha256(committed).hexdigest() == entry["fixture_sha256"]
    assert hashlib.sha256(committed).hexdigest() != entry["source_version"]
    assert web_bundle().source.version.version_key == \
        f"content_sha256:{entry['source_version']}"
    assert web_result()["source"]["canonical_url"] == "https://www.toyota.co.il/cars/RAV4-PHEV"


def test_the_obsolete_web_url_is_provenance_only_and_is_never_the_source():
    """The 404 URL explains the replacement; it never stands in for it.

    The previously documented page returned HTTP 404 and was replaced by the
    official archive page. It may appear in prose about why -- it must never be
    a canonical URL, a fixture, or a document this proof reads.
    """
    obsolete = "https://www.toyota.co.il/models/rav4-plugin"
    assert obsolete not in proof_manifest.MANIFEST_PATH.read_text(encoding="utf-8")
    for path in Path("backend/testing/r5_proof").rglob("*.py"):
        assert obsolete not in path.read_text(encoding="utf-8"), path
    assert web_result()["source"]["canonical_url"] != obsolete


def test_scripts_styles_and_templates_never_become_factual_evidence():
    """What a bundler inlined is not something the page says."""
    projection = _committed_projection()
    for excluded in ("<script", "function(", "window.", "@media", "</", "{", "<"):
        assert excluded not in projection, excluded
    # A page's inlined code is exactly where credential-shaped material lives,
    # and none of it survives into the evidence surface.
    for pattern in (r"pk\.eyJ[A-Za-z0-9_.-]{20,}", r"sk\.ey[A-Za-z0-9_.-]{20,}",
                    r"\bpub[0-9a-f]{24,}", r"\beyJ[A-Za-z0-9_-]{15,}",
                    r"\b[0-9a-f]{32,}\b"):
        assert re.search(pattern, projection) is None, pattern
    # Markup is stripped BEFORE entities are unescaped, so text a page wrote as
    # data can never become an element the stripper then honours.
    assert visible_text_projection("<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>") == \
        "<script>alert(1)</script>"


def _committed_projection() -> str:
    """The committed visible-text projection, read through the checksum gate."""
    _, text = proof_manifest.load_text_fixture("web_toyota_rav4_phev")
    return text


def test_the_committed_projection_is_demonstrably_a_projection_output():
    """Idempotency is the checkable property of a projection.

    The raw page is not committed, so a reviewer inside this repository cannot
    re-derive the projection from it -- that check belongs to whoever holds the
    capture archive, whose digest the manifest records. What CAN be checked
    here is that the committed file is a fixed point of the same versioned
    rule: projecting it again changes nothing. An edited copy of a page, or a
    file produced by a different rule, would not be.
    """
    projection = _committed_projection()
    assert visible_text_projection(projection) == projection
    entry = proof_manifest.source_entry("web_toyota_rav4_phev")
    assert entry["text_projection_version"] == WEB_TEXT_PROJECTION_VERSION
    assert entry["record_locator"]["projection_char_count"] == len(projection)
    # And the tool refuses a committed document that is NOT a fixed point.
    tool = ToyotaArchivedModelDocumentTool()
    with pytest.raises(ToolError) as failure:
        tool._locate("<p>not a projection</p>", TOYOTA_RAV4_PHEV_STATEMENTS[0])
    assert failure.value.code == "R5_WEB_STATEMENT_ABSENT"


def test_the_archived_status_comes_from_visible_text_not_from_metadata():
    """The page says it in four places; only one of them is something it SAYS.

    The captured HTML carries the ended-marketing wording in an `og:description`
    meta tag, a `twitter:description` meta tag, a `name="description"` meta tag
    and a JSON-LD `<script>` -- all of which are machine metadata a site author
    writes for crawlers, and none of which is the page's visible text. The
    projection drops `<head>` and `<script>` wholesale, so the only occurrence
    that survives is the sentence the page actually renders to a reader.
    """
    projection = _committed_projection()
    # The projection keeps exactly one occurrence, and it is the rendered
    # sentence -- not the `og:description`, `twitter:description`,
    # `name="description"` or JSON-LD copies the raw page also carries, all of
    # which live in `<head>` or a `<script>` and are removed with their
    # contents. The raw page is not committed, so the multiplicity is recorded
    # in the proof document rather than re-asserted against bytes this
    # repository does not hold.
    assert projection.count("הסתיים") == 1
    assert projection.count(ENDED_SENTENCE) == 1
    # The raw markup writes it with non-breaking spaces; the projection
    # normalizes them, which is why offsets are stated against the projection.
    assert RAW_ENDED_SENTENCE not in projection
    located = only([item for item in web_result()["statements"]
                    if item["field_key"] == "marketing_status"])
    assert located["text"] == ENDED_SENTENCE


def test_every_web_fact_quotes_an_exact_span_of_the_documents_own_text():
    """A document-span locator has to be checkable by someone with the bytes."""
    projection = _committed_projection()
    result = web_result()
    # The tool reports the digest of the WHOLE projection, so a reviewer can
    # confirm they re-derived the same text before reading any offset.
    assert result["source"]["projection_sha256"] == \
        hashlib.sha256(projection.encode("utf-8")).hexdigest()
    assert result["source"]["projection_char_count"] == len(projection)
    assert result["source"]["text_projection_version"] == WEB_TEXT_PROJECTION_VERSION

    bundle = web_bundle(result)
    assert len(bundle.fragments) == len(TOYOTA_RAV4_PHEV_STATEMENTS)
    for statement, fragment in zip(result["statements"], bundle.fragments):
        # Re-read the span out of the independently re-derived projection.
        assert projection[statement["char_start"]:statement["char_end"]] == statement["text"]
        assert fragment.fragment_type == "verbatim_excerpt"
        assert fragment.text == statement["text"]
        assert (fragment.locator.char_start, fragment.locator.char_end) == \
            (statement["char_start"], statement["char_end"])
        # Each expected phrase occurs exactly once, so the locator is unique.
        assert projection.count(statement["text"]) == 1


def test_the_web_document_states_identity_and_archived_status_and_nothing_else():
    """Exactly what the page says, and no specification it does not say."""
    result = web_result()
    assert result["states_no_technical_specification"] is True
    assert {statement["field_key"] for statement in result["statements"]} == {
        "manufacturer_model_designation", "archived_model_heading", "marketing_status"}
    ended = only([item for item in result["statements"]
                  if item["field_key"] == "marketing_status"])
    # The value is a closed reading of one exact sentence, not a paraphrase.
    assert ended["value"] == "ended"
    assert ended["text"] == ENDED_SENTENCE
    # No technical field can come out of this source at all.
    for fact in web_bundle(result).facts:
        assert fact.field_key not in {"engine_displacement_cc", "horsepower_hp",
                                      "nominal_engine_displacement_l", "fuel_type",
                                      "power_kw", "gross_weight_kg"}
        assert fact.unit is None


@pytest.mark.parametrize("wrong", [{"make": "Honda"}, {"commercial_model": "RAV4"},
                                   {"market": "DE"}])
def test_the_web_tool_fails_closed_on_a_vehicle_the_page_does_not_describe(wrong):
    with pytest.raises(ToolError) as failure:
        web_result(**wrong)
    assert failure.value.code == "R5_WEB_DOCUMENT_IDENTITY_MISMATCH"


def test_an_uncommitted_web_document_has_no_fixture_and_cannot_be_read():
    with pytest.raises(ToolError) as failure:
        web_result(document_id="toyota_il_some_other_model")
    assert failure.value.code == "R5_WEB_DOCUMENT_UNKNOWN"


#: A well-formed replacement for the archived-status sentence: same shape, same
#: line, opposite meaning. Used so the edited document stays a VALID projection
#: and the test exercises the statement requirement rather than tripping the
#: structural check first.
REWORDED_SENTENCE = "שיווק הדגם ראב4 פלאג-אין נמשך."


def test_a_page_that_no_longer_states_it_was_archived_stops_the_proof(relocated_fixtures):
    """Missing archived-status wording is a refusal, not a quieter answer.

    The sentence is REWORDED rather than deleted, and the manifest is re-pinned
    to the edited bytes, so the checksum gate passes and the document is still
    a well-formed projection. What is under test is therefore the STATEMENT
    requirement alone: a page that still proves identity must not be able to
    answer a question about marketing status just because it looks intact.
    """
    def drop_the_sentence(root):
        path = root / "web" / "toyota_il_rav4_phev.visible_text.txt"
        edited = path.read_bytes().replace(ENDED_SENTENCE.encode("utf-8"),
                                           REWORDED_SENTENCE.encode("utf-8"), 1)
        assert edited != path.read_bytes(), "the sentence was not found to reword"
        path.write_bytes(edited)
        manifest_path = root / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        entry = manifest["sources"]["web_toyota_rav4_phev"]
        entry["fixture_sha256"] = hashlib.sha256(edited).hexdigest()
        entry["fixture_byte_count"] = len(edited)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                 encoding="utf-8")

    relocated_fixtures(drop_the_sentence)
    with pytest.raises(ToolError) as failure:
        web_result()
    assert failure.value.code == "R5_WEB_STATEMENT_ABSENT"


# =============================================================================
# 15. reconciliation: what these three sources may and may not conclude jointly
# =============================================================================

def _reference(fact):
    """One StructuredEvidenceFact as the claim the conflict policy groups."""
    return EvidenceReference(claim_id=str(uuid4()), source_id=str(uuid4()),
                             run_id=str(RUN_UUID), task_id="t", entity=fact.entity_key,
                             field=fact.field_key, value=_thawed(fact.value),
                             unit=fact.unit, geography=fact.geography, market=fact.market,
                             time_scope=_thawed(fact.time_scope),
                             identity=_thawed(fact.identity), supported=True,
                             confidence=0.9)


def _thawed(value):
    """A plain, mutable copy of a deep-frozen contract value."""
    if isinstance(value, Mapping):
        return {key: _thawed(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thawed(item) for item in value]
    return value


def _fact(bundle, field):
    return only([item for item in bundle.facts if item.field_key == field])


def _scope(fact):
    """The R4 comparison scope of one R3 structured fact."""
    return scope_identity(entity=fact.entity_key, field=fact.field_key,
                          geography=fact.geography, market=fact.market,
                          time_scope=_thawed(fact.time_scope),
                          identity=_thawed(fact.identity))


def test_source_authority_is_field_specific_and_never_leaks():
    """Being official does not make a source authoritative for everything."""
    # The register is authoritative inside the dataset's documented scope...
    for field in ("engine_displacement_cc", "official_model_code", "fuel_type"):
        assert is_authoritative(GOVERNMENT_SOURCE_TYPE, field), field
    # ...and for nothing outside it, however official the publisher is.
    for field in ("list_price", "reliability_score", "marketing_status", "horsepower_hp"):
        assert not is_authoritative(GOVERNMENT_SOURCE_TYPE, field), field
    # An official manufacturer ARCHIVE page is authoritative for nothing: it
    # establishes who a model is, which is not a licence to state measurements.
    # Typing it `manufacturer_specification` would hand it exactly that.
    for field in ("engine_displacement_cc", "official_model_code", "fuel_type",
                  "marketing_status", "list_price"):
        assert not is_authoritative(WEB_SOURCE_TYPE, field), field
    assert not is_authoritative(YEDA_SOURCE_TYPE, "fuel_type")
    assert WEB_SOURCE_TYPE not in SOURCE_TYPE_AUTHORITY
    assert YEDA_SOURCE_TYPE not in SOURCE_TYPE_AUTHORITY


def test_a_nominal_label_and_an_exact_displacement_never_share_a_scope():
    """The two displacement statements are different statements.

    The catalog states an engine-CLASS label (2.5 l); the register states a
    homologated displacement (2487 cc). They carry different field keys, so
    they can never reach one comparison scope -- which is why this proof
    manufactures neither a contradiction nor an agreement between them.
    """
    catalog = _fact(yeda_bundle(), "nominal_engine_displacement_l")
    register = _fact(government_bundle(), "engine_displacement_cc")
    assert (catalog.value, catalog.unit) == (2.5, "l")
    assert (register.value, register.unit) == (2487, "cc")
    assert _scope(catalog).scope.field != _scope(register).scope.field
    # Same entity, and still not the same statement.
    assert catalog.entity_key == register.entity_key == "toyota:rav4:il"
    assert conflict_groups([_reference(catalog), _reference(register)]) == {}


def test_missing_identity_dimensions_are_never_agreement():
    """Silence is unknown, and unknown never matches a stated dimension.

    The catalog states no model code and no trim; the register states both. So
    a catalog statement and a register statement about the same commercial
    model are NOT statements about the same variant, and R4 keeps them apart
    instead of letting two partially-identified rows merge.
    """
    catalog = _fact(yeda_bundle(), "fuel_type")
    register = _fact(government_bundle(), "fuel_type")
    assert catalog.value == register.value == "plug_in_hybrid"
    assert "model_code" not in catalog.identity and "trim" not in catalog.identity
    assert register.identity["model_code"] == "AXAP54L ANXMBK"
    assert register.identity["trim"] == "PRIME AWD SE"
    # Equal values, one entity, and still different comparison scopes: the
    # register row is model year 2021 only, the catalog variant spans a range,
    # and neither silently adopts the other's identity.
    assert _scope(catalog) != _scope(register)
    assert dict(catalog.time_scope) != dict(register.time_scope)


def test_the_official_page_cannot_widen_into_a_technical_or_shared_claim():
    """An identity statement stays an identity statement.

    The page's own commercial name is `RAV4 Plug-in`, which is not the bare
    `RAV4` the other two sources use, so its entity differs and its statement
    never merges into theirs. Normalizing the name to force a merge is exactly
    the inference this proof declines to make.
    """
    web = web_bundle()
    assert {fact.entity_key for fact in web.facts} == {"toyota:rav4-plug-in:il"}
    assert {fact.entity_key for fact in government_bundle().facts} == {"toyota:rav4:il"}
    # It states no identity dimension and no time scope, so it can never narrow
    # -- or be narrowed by -- a variant statement from another source.
    assert all(not fact.identity and not fact.time_scope for fact in web.facts)
    # Nothing it produces is comparable to a technical claim.
    together = [_reference(fact) for fact in (*web.facts, *government_bundle().facts,
                                              *yeda_bundle().facts)]
    assert conflict_groups(together) == {}


def test_incompatible_variants_do_not_merge_even_within_one_source():
    """Two registry rows of one model year are two variants, not one fact."""
    rows = [government_result(model_year=2026, trim=trim, expected_record_id=record)
            for trim, record in (("SE-PLUGIN", 37392), ("XSE-PLUGIN", 37393))]
    facts = [_fact(government_bundle(row), "engine_displacement_cc") for row in rows]
    # The same displacement, stated for two different trims.
    assert facts[0].value == facts[1].value == 2487
    assert facts[0].identity["trim"] != facts[1].identity["trim"]
    assert _scope(facts[0]) != _scope(facts[1])
    # Equal values under different identities: two statements, no conflict,
    # and -- crucially -- no merge into one better-supported claim.
    assert conflict_groups([_reference(fact) for fact in facts]) == {}


# =============================================================================
# 16. controlled mutation of the captured Government and Web facts
# =============================================================================
#
# As in section 10, every wrong value below is a REAL captured value with
# exactly one thing changed, so what is under test is the comparison contract
# rather than a fixture written to fail.

@pytest.mark.parametrize("label, field, mutation, expected", [
    ("a homologated displacement off by one", "engine_displacement_cc",
     {"value": 2488}, "R4_VALUE_MISMATCH"),
    ("the same number in another unit", "engine_displacement_cc",
     {"unit": "kg"}, "R4_UNIT_NOT_CONVERTIBLE"),
    ("a displacement with no unit", "engine_displacement_cc",
     {"unit": None}, "R4_UNIT_MISSING"),
    ("a different model year", "engine_displacement_cc",
     {"time_scope": {"year_start": 2026, "year_end": 2026}}, "R4_SCOPE_MISMATCH"),
    ("a different market", "engine_displacement_cc", {"market": "DE"}, "R4_SCOPE_MISMATCH"),
    ("a different trim of the same row", "engine_displacement_cc",
     {"identity": {"body_style": "suv", "drivetrain": "awd",
                   "model_code": "AXAP54L ANXMBK", "trim": "XSE"}},
     "R4_IDENTITY_MISMATCH"),
    ("a dataset version the evidence was not read at", "engine_displacement_cc",
     {"source_version": "dataset_version:2020-01-01T00:00:00.000000"},
     "R4_SOURCE_VERSION_MISMATCH"),
    ("an official model code off by one character", "official_model_code",
     {"value": "AXAP54L ANXMBX"}, "R4_VALUE_MISMATCH"),
])
def test_one_controlled_mutation_of_a_real_government_fact_is_rejected(
        label, field, mutation, expected):
    _, repository, _, board = proof_run()
    reference = captured_reference(board, field)
    verdict = settle(repository, reference.model_copy(update=mutation))
    assert verdict.verdict == "rejected", label
    assert verdict.reason == expected, label
    assert verdict.mode == "deterministic_structured"
    # The unmutated fact still verifies against the same durable evidence, so
    # the rejection is the mutation's doing and nothing else.
    assert settle(repository, reference).verdict == "verified"


@pytest.mark.parametrize("label, mutation, expected", [
    ("marketing reported as continuing", {"value": "active"}, "R4_VALUE_MISMATCH"),
    ("a content digest the evidence was not read at",
     {"source_version": "content_sha256:" + "b" * 64}, "R4_SOURCE_VERSION_MISMATCH"),
    ("a market the page never mentions", {"market": "DE"}, "R4_SCOPE_MISMATCH"),
    ("an identity the page never states",
     {"identity": {"trim": "GR SPORT"}}, "R4_IDENTITY_MISMATCH"),
])
def test_one_controlled_mutation_of_a_real_web_fact_is_rejected(label, mutation, expected):
    _, repository, _, board = proof_run()
    reference = captured_reference(board, "marketing_status")
    verdict = settle(repository, reference.model_copy(update=mutation))
    assert verdict.verdict == "rejected", label
    assert verdict.reason == expected, label
    assert verdict.mode == "deterministic_structured"
    assert settle(repository, reference).verdict == "verified"


# =============================================================================
# 17. the proof reaches no network, no production registry and no write path
# =============================================================================

def test_no_proof_module_can_reach_a_network_or_a_database():
    """Offline by construction, not by convention.

    Every module the proof loads is checked for the import that would make a
    live call possible at all. A fixture-backed proof that could open a socket
    is one refactor away from being a live integration nobody reviewed.
    """
    forbidden = ("import requests", "import httpx", "import socket", "urllib.request",
                 "from supabase", "import psycopg", "openai", "webbrowser")
    for path in sorted(Path("backend/testing/r5_proof").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in source, f"{path} names {token}"


def test_the_committed_page_carries_no_secret_of_ours_and_no_captured_session():
    """A saved third-party page is committed whole, and reviewed for what is in it.

    The page is stored byte-for-byte because `fixture_sha256 == upstream_sha256`
    is the entire provenance guarantee -- redacting a byte would break the digest
    chain the proof rests on. So the question is not whether to edit it but what
    it actually contains, and that is checked rather than assumed:

    *   no credential of OURS, in any form the repository's own scanner or the
        patterns below recognise;
    *   no captured session -- the capture retained no `Set-Cookie`, sent no
        `Authorization`, and replayed no cookie, so nothing user-identifying
        was recorded in the first place.

    What the page DOES carry is Toyota's own client-side public tokens -- Mapbox
    publishable (`pk.`) keys and a `pub`-prefixed analytics key -- which every
    visitor to the public site already receives, and which this repository
    neither owns nor can use to reach anything. They are called out in
    `docs/proofs/R5_VEHICLE_EVIDENCE_PROOF.md` so a reviewer sees a considered
    decision rather than an oversight.
    """
    page = _committed_projection()
    # The repository's own scanner patterns, plus the shapes it does not cover.
    # Matched as KEY SHAPES rather than substrings: a bare "sk-" also occurs
    # inside "mask-user-input", and a test that flagged that would be noise
    # rather than a check.
    for pattern in (r"sk-[A-Za-z0-9_-]{20,}", r"sk\.ey[A-Za-z0-9_.-]{20,}",
                    r"service_role[A-Za-z0-9_.-]{20,}", r"-----BEGIN [A-Z ]*PRIVATE KEY",
                    r"(?i)set-cookie\s*:", r"(?i)\bauthorization\s*:\s*bearer",
                    r"(?i)aws_secret_access_key", r"SUPABASE_SERVICE_ROLE"):
        assert re.search(pattern, page) is None, pattern
    # The capture recorded no response validator and no cookie for this page.
    entry = proof_manifest.source_entry("web_toyota_rav4_phev")
    assert set(entry["absent_source_metadata"]) == {"etag", "last_modified"}


def test_the_proof_registers_no_production_tool_and_no_write_operation():
    """Three read-only tools, none of them production, none of them a writer."""
    tools = [YedaVehicleCatalogTool(), GovernmentVehicleRegistryTool(),
             ToyotaArchivedModelDocumentTool()]
    for tool in tools:
        assert tool.mode is ToolMode.READ
        assert len(tool.operations) == 1
        assert "write" not in tool.required_scope
        for operation in tool.operations.values():
            assert operation.input_schema["additionalProperties"] is False
            assert operation.output_schema["additionalProperties"] is False
    # R5's proof tools are registered NOWHERE but in the proof registry. The
    # production registry is built in the worker path and holds exactly one
    # tool -- the bounded Government catalog read -- which is not one of these.
    assert proof_registry().allowed_names == {tool.name for tool in tools}
    assert not (proof_registry().allowed_names & {"catalog.government_vehicle"})
    assert not (proof_evidence_mappers().registered & PRODUCTION_EVIDENCE_MAPPER_OPERATIONS)

"""CODE-2: the independent catalog execution flag, and what it really gates.

Before this stage the Government tool and the canonical promotion pipeline were
built unconditionally in every `swarm_v2` run (`backend/worker/main.py`). The
only things that kept them inert were properties of the deployed environment --
an empty catalog schema, a snapshot nobody had captured -- and none of them was
a switch an operator could reach for. That is CAT-11.

This module states the contract the switch has to satisfy, and it is
deliberately about the TRUSTED WIRING rather than about a UI:

*   `MILO_ENABLE_CATALOG_EXECUTION` is off unless the environment says one of
    the repository's established explicit true forms. Unset, `false`, empty
    and malformed all mean off, because every one of them is a deployment
    nobody deliberately switched on;
*   when it is off the capability is ABSENT, not hidden. No tool in the
    registry, no scope on the ToolContext, no Government name in the prompt the
    Commander actually receives, no mapper, no pipeline and no `promote()`;
*   when it is on, everything Catalog PR3 built behaves exactly as it did;
*   it enables the catalog path and NOTHING else: not paid execution, not run
    creation, not an execution route, and not one line of the V1 engine.

The wiring assertions run the REAL `backend/worker/main.py` over the offline
Swarm stack, so what they observe is production construction rather than a
description of it.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from uuid import uuid4

import pytest

import backend.catalog.execution as catalog_execution
import backend.catalog.pipeline as pipeline_module
import backend.engines.swarm_v2.evidence_mapping as mapping_module
import backend.tools as tools_package
import backend.worker.main as worker_main
from backend.catalog.execution import (CATALOG_EXECUTION_FLAG, CATALOG_PROMOTION_FLAG,
                                       GOVERNMENT_READ_FLAG, CatalogPostureInvalid,
                                       catalog_execution_enabled, catalog_posture,
                                       catalog_promotion_enabled, government_read_enabled)
from backend.tools.government_vehicle import GOVERNMENT_TOOL_NAME, GOVERNMENT_TOOL_SCOPE

from test_swarm_v2_smoke_offline import (FakeKimiCompletions, build_repo,
                                         run_worker_directly, swarm_env)

REPO = Path(__file__).resolve().parents[1]


def load_script(name: str, relative: str):
    """Import an operator script that is not part of an importable package."""
    spec = importlib.util.spec_from_file_location(name, REPO / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# =============================================================================
# 1. the flag itself
# =============================================================================

@pytest.mark.parametrize("value", ["1", "true", "TRUE", "True", "yes", "on", " true "])
def test_established_explicit_true_forms_enable_the_catalog(monkeypatch, value):
    """Exactly the forms `execution_guard`/`production_config` already accept.

    The repository has ONE spelling of "an operator turned this on"; a catalog
    flag with its own dialect would be a second contract to keep in step.
    """
    monkeypatch.setenv(CATALOG_EXECUTION_FLAG, value)
    assert catalog_execution_enabled() is True


@pytest.mark.parametrize("value", ["false", "FALSE", "0", "no", "off", "", "   ",
                                   "maybe", "tru", "1;true", "yes please", "2",
                                   "null", "None", "enabled", "truthy"])
def test_false_empty_and_malformed_values_all_mean_off(monkeypatch, value):
    """A value nobody recognises is a MISCONFIGURATION, and the safe reading of
    a misconfiguration is off. `enabled` and `truthy` are in this list on
    purpose: a substring or prefix test would let either one through."""
    monkeypatch.setenv(CATALOG_EXECUTION_FLAG, value)
    assert catalog_execution_enabled() is False


def test_an_unset_flag_is_off(monkeypatch):
    monkeypatch.delenv(CATALOG_EXECUTION_FLAG, raising=False)
    assert catalog_execution_enabled() is False


def test_the_flag_is_server_side_and_never_browser_public():
    """A `NEXT_PUBLIC_` twin would ship the switch to the browser bundle."""
    assert CATALOG_EXECUTION_FLAG == "MILO_ENABLE_CATALOG_EXECUTION"
    assert not CATALOG_EXECUTION_FLAG.startswith("NEXT_PUBLIC_")
    frontend = REPO / "frontend"
    hits = [path for path in frontend.rglob("*")
            if path.is_file() and path.suffix in {".ts", ".tsx", ".mjs", ".js"}
            and "node_modules" not in path.parts
            and CATALOG_EXECUTION_FLAG in path.read_text(errors="ignore")]
    assert hits == [], f"the server-only catalog flag is referenced in the browser tree: {hits}"


def test_only_the_environment_can_set_it(monkeypatch):
    """Not a plan, not a payload, not run or project metadata.

    `catalog_execution_enabled` takes an optional mapping so a validator can
    check a deployment's metadata, but the production call site passes nothing
    and reads the process environment. A mapping carrying the flag cannot
    change what the default read returns.
    """
    monkeypatch.delenv(CATALOG_EXECUTION_FLAG, raising=False)
    assert catalog_execution_enabled({CATALOG_EXECUTION_FLAG: "true"}) is True
    assert catalog_execution_enabled() is False


# =============================================================================
# 2. the trusted Swarm V2 wiring
# =============================================================================

class WiringRecord:
    """What `make_swarm_engine` actually constructed, captured in place."""

    def __init__(self) -> None:
        self.registries: list[object] = []
        self.granted_scopes: list[frozenset[str]] = []
        self.mapper_registries: list[frozenset[tuple[str, str]]] = []
        self.production_mapper_calls = 0
        self.pipelines = 0
        self.promote_calls = 0

    @property
    def registered_tools(self) -> frozenset[str]:
        assert len(self.registries) == 1, self.registries
        return self.registries[0].allowed_names

    @property
    def descriptor_names(self) -> tuple[str, ...]:
        assert len(self.registries) == 1, self.registries
        return tuple(d.name for d in self.registries[0].descriptors())


def record_wiring(monkeypatch) -> WiringRecord:
    """Subclass every trusted constructor the catalog gate is supposed to
    control, keeping the real behaviour underneath."""
    record = WiringRecord()
    real_registry = tools_package.ToolRegistry
    real_context = tools_package.ToolContext
    real_pipeline = pipeline_module.CatalogPromotionPipeline
    real_acquisition = mapping_module.TrustedEvidenceAcquisition
    real_production_mappers = mapping_module.production_evidence_mappers

    class RecordingToolRegistry(real_registry):
        def __init__(self, tools=()):
            super().__init__(tools)
            record.registries.append(self)

    def recording_context(*args, **kwargs):
        context = real_context(*args, **kwargs)
        record.granted_scopes.append(frozenset(context.scopes))
        return context

    class RecordingAcquisition(real_acquisition):
        def __init__(self, *, board, mappers):
            super().__init__(board=board, mappers=mappers)
            record.mapper_registries.append(frozenset(mappers.registered))

    class RecordingPipeline(real_pipeline):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            record.pipelines += 1

        def promote(self):
            record.promote_calls += 1
            return super().promote()

    def recording_production_mappers():
        record.production_mapper_calls += 1
        return real_production_mappers()

    monkeypatch.setattr(tools_package, "ToolRegistry", RecordingToolRegistry)
    monkeypatch.setattr(tools_package, "ToolContext", recording_context)
    monkeypatch.setattr(mapping_module, "TrustedEvidenceAcquisition", RecordingAcquisition)
    monkeypatch.setattr(mapping_module, "production_evidence_mappers", recording_production_mappers)
    monkeypatch.setattr(pipeline_module, "CatalogPromotionPipeline", RecordingPipeline)
    return record


def run_swarm_with_catalog_flag(monkeypatch, flag, *, read=None, promotion=None):
    """One real Swarm V2 run through `execute_run`, at a given catalog posture.

    `flag` is the master switch. `read` and `promotion` are the two capability
    flags under it; both default to following the master switch, so every
    existing caller keeps describing the same "all on" / "all off" postures it
    always did, and a test that wants the SPLIT states names them explicitly.
    """
    record = record_wiring(monkeypatch)
    swarm_env(monkeypatch)
    for name, value in ((CATALOG_EXECUTION_FLAG, flag),
                        (GOVERNMENT_READ_FLAG, flag if read is None else read),
                        (CATALOG_PROMOTION_FLAG, flag if promotion is None else promotion)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    repo, conversation_id = build_repo()
    completions = FakeKimiCompletions()
    run_id = run_worker_directly(repo, conversation_id, monkeypatch, completions,
                                 idempotency_key=f"catalog-flag-{uuid4().hex[:8]}")
    assert worker_main.execute_run(run_id, repo) == 0
    return record, repo, run_id, completions


def event_types(repo, run_id) -> list[str]:
    return [row["event_type"] for row in repo.run_events if str(row["run_id"]) == str(run_id)]


@pytest.mark.parametrize("flag", [None, "false", "0", "off", "maybe", "", "truthy"],
                         ids=["unset", "false", "zero", "off", "malformed",
                              "empty", "prefix-lookalike"])
def test_a_disabled_flag_leaves_no_catalog_capability_anywhere(monkeypatch, flag):
    """The whole of CAT-11, in one run: the capability is ABSENT.

    Not "registered but unreachable" and not "hidden in the UI" -- there is no
    Government tool in the registry for a plan to name, no scope that would
    admit one, no Government name in the prompt the Commander receives, no
    mapper that could turn a result into evidence, and no promotion pipeline
    to construct or call.
    """
    record, repo, run_id, completions = run_swarm_with_catalog_flag(monkeypatch, flag)

    # The Tool Registry contains no Government tool -- and no replacement.
    assert record.registered_tools == frozenset()
    assert record.descriptor_names == ()

    # No Government scope was granted on any ToolContext.
    assert record.granted_scopes == [frozenset()]
    assert all(GOVERNMENT_TOOL_SCOPE not in scopes for scopes in record.granted_scopes)

    # Commander-visible descriptors: read from the prompt the provider really
    # received, not from an object we could have inspected selectively.
    prompts = json.dumps(completions.calls)
    assert GOVERNMENT_TOOL_NAME not in prompts
    assert GOVERNMENT_TOOL_SCOPE not in prompts

    # No production evidence mapper was built, and the sink that exists maps
    # no operation at all.
    assert record.production_mapper_calls == 0
    assert record.mapper_registries == [frozenset()]

    # No promotion pipeline was constructed, so `promote()` cannot have run.
    assert record.pipelines == 0
    assert record.promote_calls == 0

    # And no catalog event was emitted.
    assert "catalog_variant_promoted" not in event_types(repo, run_id)
    assert "catalog_promotion_refused" not in event_types(repo, run_id)


def test_a_disabled_flag_still_runs_a_legitimate_no_tool_swarm(monkeypatch):
    """Disabling the catalog must not disable Swarm V2.

    The firewall's own rule is "when allowed_tools is empty every task must use
    tools: []", which is exactly the smoke plan. A run with no tools still
    plans, executes its logical tasks and reaches a durable terminal state.
    """
    _, repo, run_id, _ = run_swarm_with_catalog_flag(monkeypatch, None)
    types = event_types(repo, run_id)
    assert "run_started" in types
    assert "commander_plan_created" in types
    assert "task_completed" in types
    assert repo.get_run(run_id)["status"] in {"completed", "partial_success"}


def test_an_enabled_flag_registers_exactly_the_existing_government_tool(monkeypatch):
    """Enabled is the Catalog PR3 wiring, unchanged and not widened.

    Exactly one tool, the existing bounded read-only one; exactly the existing
    server-owned scope; exactly the existing registered mapper and its single
    allowed operation; and the pipeline constructed and called once.
    """
    record, repo, run_id, _ = run_swarm_with_catalog_flag(
        monkeypatch, "true", read="true", promotion="true")

    assert record.registered_tools == frozenset({GOVERNMENT_TOOL_NAME})
    assert record.descriptor_names == (GOVERNMENT_TOOL_NAME,)
    assert record.granted_scopes == [frozenset({GOVERNMENT_TOOL_SCOPE})]
    assert record.production_mapper_calls == 1
    assert record.mapper_registries == [
        frozenset({(GOVERNMENT_TOOL_NAME, "resolve_variant")})]
    assert record.pipelines == 1
    assert record.promote_calls == 1


def test_an_enabled_flag_adds_no_transport_capture_or_write_tool(monkeypatch):
    """No CKAN, no Web, no capture and no WRITE capability comes with it.

    Enabling the catalog widens the registry by exactly one READ tool. It does
    not grant write approval, a `tool:write:` capability, or any capability at
    all: `ToolContext.capabilities` and `write_approved` stay at their defaults
    even in the enabled posture.
    """
    from backend.tools.contracts import ToolMode

    record, *_ = run_swarm_with_catalog_flag(
        monkeypatch, "true", read="true", promotion="true")
    registry = record.registries[0]
    assert registry.allowed_names == frozenset({GOVERNMENT_TOOL_NAME})
    for descriptor in registry.descriptors():
        assert descriptor.mode == ToolMode.READ.value
    assert record.granted_scopes == [frozenset({GOVERNMENT_TOOL_SCOPE})]
    assert all(not scope.startswith("tool:write:") for scope in record.granted_scopes[0])


def test_an_existing_snapshot_stays_inert_for_a_chat_run_while_disabled(monkeypatch):
    """The deployment whose safety used to be accidental.

    A database that DOES hold a usable Government snapshot is exactly the case
    the old wiring had no answer for. With the flag off the read is never
    reached: the tool that performs it is not registered and the pipeline that
    would promote from it is never built, so the durable rows are untouched.
    """
    from backend.testing.memory_repository import MemoryRepository

    reads: list[str] = []
    for name in ("catalog_run_pending_promotions", "find_active_catalog_snapshot"):
        original = getattr(MemoryRepository, name)

        def counted(self, *args, _name=name, _original=original, **kwargs):
            reads.append(_name)
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(MemoryRepository, name, counted)

    record, repo, run_id, _ = run_swarm_with_catalog_flag(monkeypatch, None)
    assert record.pipelines == 0
    assert reads == [], f"a disabled catalog still read durable catalog rows: {reads}"


# =============================================================================
# 3. the flag enables the catalog path and nothing else
# =============================================================================

def test_the_catalog_flag_alone_enables_no_other_execution_surface(monkeypatch):
    """It is not a back door into paid execution, run creation or a route."""
    from backend.budget import paid_execution_enabled
    from backend.execution_guard import SURFACE_RULES, find_disabled_surface, is_stage_enabled

    for name in ("MILO_ENABLE_PAID_EXECUTION", "MILO_ENABLE_RUN_CREATION",
                 "MILO_ENABLE_PROPOSAL_MUTATIONS", "MILO_ENABLE_PROPOSAL_READS",
                 "MILO_ENABLE_RUN_CANCELLATION", "MILO_ENABLE_EXECUTION_CONTROL",
                 "GATEWAY_ALLOW_EXECUTION_ROUTES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(CATALOG_EXECUTION_FLAG, "true")

    assert catalog_execution_enabled() is True
    assert paid_execution_enabled() is False
    assert is_stage_enabled("MILO_ENABLE_RUN_CREATION") is False
    assert is_stage_enabled("MILO_ENABLE_EXECUTION_CONTROL") is False
    # Every HTTP execution surface is still refused, and the catalog flag is
    # not one of the flags that could ever open one.
    assert find_disabled_surface("POST", f"/conversations/{uuid4()}/runs") \
        == ("MILO_ENABLE_RUN_CREATION", "conversation run creation")
    assert CATALOG_EXECUTION_FLAG not in {flag for _, flag, _, _ in SURFACE_RULES}


def test_a_global_kill_switch_still_outranks_the_catalog_flag(monkeypatch):
    """The existing switches stay authoritative: with run creation off there is
    no run for the catalog flag to be enabled inside."""
    from backend.execution_guard import find_disabled_surface

    monkeypatch.setenv(CATALOG_EXECUTION_FLAG, "true")
    monkeypatch.delenv("MILO_ENABLE_RUN_CREATION", raising=False)
    assert find_disabled_surface("POST", f"/conversations/{uuid4()}/runs") is not None


def test_v1_never_consults_the_catalog_flag_and_behaves_identically(monkeypatch):
    """The V1 engine's behaviour is untouched, proven twice over.

    The flag gate lives inside the Swarm V2 factory, which the trusted
    `EngineResolver` reaches only for `workflow_key == "swarm_v2"`. So a V1 run
    must produce the same events and the same checkpoints whatever the flag
    says -- and must never read it at all.
    """
    from test_worker import WorkerRepo

    def v1_run(flag):
        calls: list[str] = []
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(catalog_execution, "catalog_execution_enabled",
                          lambda *a, **k: calls.append("read") or False)
            patch.setenv("MILO_WORKER_ENGINE", "mock")
            patch.delenv("MILO_ENABLE_PAID_EXECUTION", raising=False)
            if flag is None:
                patch.delenv(CATALOG_EXECUTION_FLAG, raising=False)
            else:
                patch.setenv(CATALOG_EXECUTION_FLAG, flag)

            class CheckpointRepo(WorkerRepo):
                def __init__(self):
                    super().__init__()
                    self.checkpoints = []

                def save_checkpoint(self, checkpoint, worker_id=None, attempt=None,
                                    lease_token=None):
                    self._assert_lease(worker_id, attempt, lease_token)
                    self.checkpoints.append(checkpoint)
                    return checkpoint

            repo = CheckpointRepo()
            code = worker_main.execute_run(repo.run_id, repo)
        return (code, [event[1] for event in repo.events],
                [c.get("phase") for c in repo.checkpoints], calls)

    off_code, off_events, off_phases, off_reads = v1_run(None)
    on_code, on_events, on_phases, on_reads = v1_run("true")

    assert off_code == on_code == 0
    assert off_events == on_events
    assert off_phases == on_phases == ["discovery", "technical", "summary"]
    assert off_events[-1] == "run_completed"
    # The V1 path never even asks.
    assert off_reads == [] and on_reads == []


# =============================================================================
# 4. safe defaults, deployment contracts and release tooling
# =============================================================================

def test_the_runtime_execution_flag_inventory_includes_the_catalog_flag():
    from backend.production_config import EXECUTION_FLAGS

    assert CATALOG_EXECUTION_FLAG in EXECUTION_FLAGS


def test_the_unsafe_default_scan_covers_the_catalog_flag():
    """Otherwise a committed `=true` would pass CI unnoticed."""
    scanner = load_script("check_unsafe_defaults", "scripts/check_unsafe_defaults.py")
    assert CATALOG_EXECUTION_FLAG in scanner.EXECUTION_FLAGS
    assert scanner.main() == 0


def test_the_committed_repository_never_enables_the_catalog_flag():
    """The scan is only worth having if it is passing on this tree."""
    pattern = re.compile(rf"{CATALOG_EXECUTION_FLAG}\s*[:=]\s*['\"]?(1|true|yes|on)['\"]?",
                         re.I)
    allowed = ("tests/", "frontend/tests/", "docs/", "scripts/check_unsafe_defaults.py")
    offenders = []
    for path in REPO.rglob("*"):
        if not path.is_file() or path.suffix not in {".py", ".ts", ".tsx", ".sh",
                                                     ".yml", ".yaml", ".json", ".toml",
                                                     ".mjs", ".js", ".env"}:
            continue
        rel = path.relative_to(REPO).as_posix()
        if rel.startswith((".git/", "frontend/node_modules/", "legacy/")):
            continue
        if rel.startswith(allowed):
            continue
        if pattern.search(path.read_text(errors="ignore")):
            offenders.append(rel)
    assert offenders == [], offenders


def test_the_stage_a_deployment_contract_pins_the_catalog_flag_off():
    contract = (REPO / "scripts/deploy/deployment-contract.sh").read_text()
    literal = contract.split("MILO_STAGE_A_EXECUTION_FLAGS=(", 1)[1].split("\n)", 1)[0]
    assert f"{CATALOG_EXECUTION_FLAG}=false" in literal


def test_the_generated_deployment_plan_pins_the_catalog_flag_off():
    generator = (REPO / "scripts/release/generate-deployment-plan.sh").read_text()
    assert f"{CATALOG_EXECUTION_FLAG}=false" in generator


def test_the_production_manifest_validator_inventories_the_catalog_flag():
    validator = load_script("validate_production_manifest",
                            "scripts/release/validate_production_manifest.py")
    assert CATALOG_EXECUTION_FLAG in validator.EXECUTION_FLAGS


def test_the_smoke_env_contract_keeps_the_catalog_flag_off_in_every_posture():
    """Including during an ACTIVE paid smoke: Stage C does not open this."""
    contract = load_script("parse_env_contract",
                           "scripts/release/swarm-v2-smoke/parse_env_contract.py")
    assert contract.FLAGS_AT_REST[CATALOG_EXECUTION_FLAG] == "false"
    assert contract.WORKER_FLAGS_SMOKE[CATALOG_EXECUTION_FLAG] == "false"
    assert contract.API_FLAGS_SMOKE[CATALOG_EXECUTION_FLAG] == "false"


def test_the_execution_disabled_smoke_asserts_the_catalog_flag_is_off():
    """In the flag-posture LOOP, not merely mentioned somewhere in the file."""
    script = (REPO / "scripts/release/smoke-test-execution-disabled.sh").read_text()
    loop = [line for line in script.splitlines()
            if line.lstrip().startswith("for flag in") and "MILO_ENABLE_" in line]
    assert loop, "the execution-disabled smoke has no flag-posture loop"
    assert any(CATALOG_EXECUTION_FLAG in line for line in loop), loop


def test_the_production_config_check_inventories_the_catalog_flag():
    script = (REPO / "scripts/release/check-production-config.sh").read_text()
    assert f"{CATALOG_EXECUTION_FLAG}|" in script, "missing from the variable inventory"
    literal = script.split("EXECUTION_FLAGS=(", 1)[1].split("\n)", 1)[0]
    assert CATALOG_EXECUTION_FLAG in literal


def test_the_kill_switch_disables_catalog_execution_independently():
    """Rollback has to be able to close the catalog without a code rollback.

    Both halves matter: the switch must SET the flag false on the worker (which
    is where the catalog capability is built) and must VERIFY it afterwards, or
    "the kill switch ran" would not mean "the catalog is closed".
    """
    script = (REPO / "scripts/release/stage-c/kill-switch.sh").read_text()
    set_lines = [line for line in script.splitlines()
                 if "--update-env-vars" in line and CATALOG_EXECUTION_FLAG in line]
    assert set_lines, "the kill switch never sets the catalog flag false"
    assert all(f"{CATALOG_EXECUTION_FLAG}=false" in line for line in set_lines)
    worker_verifier = script.split("verify_worker_posture()", 1)[1]
    assert CATALOG_EXECUTION_FLAG in worker_verifier, \
        "the kill switch never verifies the worker's catalog flag"


def test_the_environment_matrix_documents_the_catalog_flag_with_an_off_default():
    matrix = (REPO / "docs/production-readiness/ENVIRONMENT_MATRIX.md").read_text()
    rows = [line for line in matrix.splitlines() if CATALOG_EXECUTION_FLAG in line]
    assert rows, "the catalog flag has no row in the environment matrix"
    assert any("off" in row for row in rows), rows


@pytest.mark.parametrize("document", [
    "docs/production-readiness/MONITORING_AND_INCIDENTS.md",
    "docs/production-readiness/ROLLBACK.md",
    "docs/production-readiness/STAGED_ACTIVATION.md",
    "docs/production-readiness/FINAL_ACCEPTANCE.md",
    "docs/production-readiness/SMOKE_TESTING.md",
    "docs/production-readiness/DEPLOYMENT.md",
    "docs/deployment/swarm-v2-smoke.md",
])
def test_the_readiness_documents_name_the_catalog_kill_switch(document):
    assert CATALOG_EXECUTION_FLAG in (REPO / document).read_text(), document


@pytest.mark.parametrize("document", [
    "docs/production-readiness/MONITORING_AND_INCIDENTS.md",
    "docs/production-readiness/ROLLBACK.md",
])
def test_the_monitoring_documents_explain_both_catalog_events(document):
    text = (REPO / document).read_text()
    assert "catalog_variant_promoted" in text
    assert "catalog_promotion_refused" in text


# =============================================================================
# 5. the backend event vocabulary
# =============================================================================

def test_both_catalog_types_are_recognised_by_the_backend_vocabulary():
    from backend.runtime import CATALOG_EVENT_TYPES, EVENT_TYPES

    assert CATALOG_EVENT_TYPES == frozenset({"catalog_variant_promoted",
                                             "catalog_promotion_refused"})
    assert CATALOG_EVENT_TYPES <= EVENT_TYPES


def test_the_catalog_types_do_not_join_the_v1_vocabulary():
    """`V1_EVENT_TYPES` is what the browser mirrors into `ownsV1Projection`.

    Putting a catalog type in it would hand a catalog event the V1 agent,
    phase, progress and spend projection -- the exact thing F5 closed.
    """
    from backend.event_registry import (CATALOG_EVENT_TYPES, EVENT_TYPES,
                                        OPERATIONAL_EVENT_TYPES,
                                        SWARM_V2_EVENT_TYPES, V1_EVENT_TYPES)

    assert not (CATALOG_EVENT_TYPES & V1_EVENT_TYPES)
    # The acceptance set is the union of ALL FOUR groups. It used to be V1 plus
    # catalog alone, which is why the API refused `task_started` -- an event the
    # V2 engine emits on every task -- while the durable sink wrote it unchecked.
    assert EVENT_TYPES == (V1_EVENT_TYPES | SWARM_V2_EVENT_TYPES
                           | CATALOG_EVENT_TYPES | OPERATIONAL_EVENT_TYPES)


def test_the_worker_event_route_now_accepts_the_two_catalog_types():
    from backend.runtime import EVENT_TYPES

    assert "catalog_variant_promoted" in EVENT_TYPES
    assert "catalog_promotion_refused" in EVENT_TYPES


def test_recognition_is_exact_and_never_a_substring():
    from backend.runtime import EVENT_TYPES

    for invented in ("catalog_variant_promoted_v2", "x_catalog_variant_promoted",
                     "catalog_promotion_refused!", "catalog_", "catalog_promotion"):
        assert invented not in EVENT_TYPES


def test_the_frontend_mirrors_the_same_two_catalog_types():
    """One vocabulary, two languages: a drift here is a silent inert event.

    The browser no longer RESTATES the names -- a hand-maintained mirror is how
    they drifted apart in the first place. It reads the generated manifest, so
    the mirror is proven by checking that manifest against the backend and that
    the TypeScript declares no catalog name of its own.
    """
    import json

    from backend.event_registry import CATALOG_EVENT_TYPES

    manifest = json.loads((REPO / "frontend/lib/eventRegistry.generated.json").read_text())
    assert set(manifest["groups"]["catalog"]) == set(CATALOG_EVENT_TYPES)

    source = (REPO / "frontend/lib/eventVocabulary.ts").read_text()
    assert "CATALOG_EVENT_TYPES: ReadonlySet<string> = group('catalog')" in source
    for name in CATALOG_EVENT_TYPES:
        assert f"'{name}'" not in source, f"the browser restates {name} instead of reading it"


def test_the_frontend_bounds_promoted_field_counts_at_the_backend_maximum():
    """A parity test, in the same shape as the vocabulary mirror above.

    `PromotionAttempt.as_event()` carries `promoted_fields` and
    `unsupported_fields`, and both are subsets of `CANONICAL_VARIANT_FIELDS`:
    `promoted_fields` is one entry per `plan.fields`, and `unsupported_fields`
    is the unsupported subset of the namespaced identity dimensions. So
    `MAX_CANONICAL_FIELDS` bounds both, and the browser projection enforces
    that same bound before it will present a list's length as a field count.

    The bound is DECLARED in the TypeScript, not fetched from here -- there is
    no runtime coupling between the browser and this package. This test is the
    drift alarm: widen `CANONICAL_VARIANT_FIELDS` (add an identity dimension,
    say) and the frontend bound goes stale silently without it.
    """
    from backend.catalog.contracts import (CANONICAL_VARIANT_FIELDS,
                                           MAX_CANONICAL_FIELDS)

    # The backend bound is derived, not hand-written, and must stay that way.
    assert MAX_CANONICAL_FIELDS == len(CANONICAL_VARIANT_FIELDS)

    source = (REPO / "frontend/lib/catalogStatus.ts").read_text()
    declared = re.search(r"export const MAX_CANONICAL_FIELDS\s*=\s*(\d+)\s*;", source)
    assert declared is not None, \
        "frontend/lib/catalogStatus.ts must declare MAX_CANONICAL_FIELDS"
    assert int(declared.group(1)) == MAX_CANONICAL_FIELDS, (
        f"frontend MAX_CANONICAL_FIELDS={declared.group(1)} has drifted from the "
        f"backend contract's {MAX_CANONICAL_FIELDS} "
        f"(len(CANONICAL_VARIANT_FIELDS)); update frontend/lib/catalogStatus.ts")


def test_the_promotion_event_contract_still_carries_both_field_lists():
    """The parity test above is only meaningful while these keys exist.

    If `as_event()` stopped emitting either list, the frontend bound would be
    guarding a field nothing sends and the drift alarm would be inert.
    """
    source = (REPO / "backend/catalog/pipeline.py").read_text()
    assert 'payload["promoted_fields"] = list(self.outcome.promoted_fields)' in source
    assert 'payload["unsupported_fields"] = list(self.outcome.unsupported_fields)' in source


def test_an_enabled_catalog_run_emits_only_recognised_event_types(monkeypatch):
    """Whatever this run emits, the API's own allowlist accepts it."""
    from backend.runtime import EVENT_TYPES

    _, repo, run_id, _ = run_swarm_with_catalog_flag(monkeypatch, "true")
    unknown = sorted({t for t in event_types(repo, run_id) if t not in EVENT_TYPES})
    swarm_only = {"commander_plan_created", "commander_replanned", "task_ready",
                  "task_started", "task_completed", "task_failed", "tool_called",
                  "worker_output_repair_started", "evidence_added", "conflict_found",
                  "grounding_context_resolved", "verification_batch_completed",
                  "verification_completed", "provider_backpressure_wait"}
    assert not (set(unknown) - swarm_only), unknown


# =============================================================================
# 4. reading the register and promoting into the catalog are SEPARATE
# =============================================================================
#
# Before the split there was exactly one flag, so the only configuration that
# let a run read the government register also armed canonical promotion in the
# same breath. There was therefore no posture in which a first, deliberately
# read-only run could be performed at all. These tests are that posture.


def test_read_on_promotion_off_reads_the_register_and_writes_nothing(monkeypatch):
    """The read-only posture, proven end to end through a real run.

    Everything the read capability is supposed to provide is present -- the one
    bounded read tool, its server-owned scope, its single registered mapper --
    and everything promotion would need is absent: no pipeline is constructed,
    so `promote()` cannot have run, no canonical write can have happened and
    neither catalog event is emitted.
    """
    from backend.tools.contracts import ToolMode

    record, repo, run_id, completions = run_swarm_with_catalog_flag(
        monkeypatch, "true", read="true", promotion="false")

    # READ is fully wired.
    assert record.registered_tools == frozenset({GOVERNMENT_TOOL_NAME})
    assert record.granted_scopes == [frozenset({GOVERNMENT_TOOL_SCOPE})]
    assert record.production_mapper_calls == 1
    assert record.mapper_registries == [
        frozenset({(GOVERNMENT_TOOL_NAME, "resolve_variant")})]
    for descriptor in record.registries[0].descriptors():
        assert descriptor.mode == ToolMode.READ.value

    # PROMOTION is absent -- not merely refused at call time.
    assert record.pipelines == 0
    assert record.promote_calls == 0
    assert "catalog_variant_promoted" not in event_types(repo, run_id)
    assert "catalog_promotion_refused" not in event_types(repo, run_id)

    # No write scope or capability came along with the read grant.
    assert all(not scope.startswith("tool:write:")
               for scope in record.granted_scopes[0])


def test_read_on_promotion_off_performs_no_canonical_catalog_write(monkeypatch):
    """The same posture, proven at the repository boundary.

    The read tool may legitimately read durable catalog rows. What must not
    happen is a canonical WRITE, so every mutating catalog entry point on the
    repository is counted and must stay at zero.
    """
    from backend.testing.memory_repository import MemoryRepository

    writes: list[str] = []
    mutators = [name for name in dir(MemoryRepository)
                if name.startswith(("promote_", "upsert_catalog", "insert_catalog",
                                    "update_catalog", "apply_catalog"))]
    assert mutators, "no catalog mutation entry points were found to guard"
    for name in mutators:
        original = getattr(MemoryRepository, name)

        def counted(self, *args, _name=name, _original=original, **kwargs):
            writes.append(_name)
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(MemoryRepository, name, counted)

    record, *_ = run_swarm_with_catalog_flag(
        monkeypatch, "true", read="true", promotion="false")
    assert record.pipelines == 0
    assert writes == [], f"a read-only catalog posture performed writes: {writes}"


def test_promotion_on_read_off_fails_closed(monkeypatch):
    """Promotion without read is a refusal, never a quiet downgrade.

    Silently reading it as "promotion off" would leave an operator believing a
    posture they do not have. It is a contradiction, so it is named.
    """
    monkeypatch.setenv(CATALOG_EXECUTION_FLAG, "true")
    monkeypatch.setenv(GOVERNMENT_READ_FLAG, "false")
    monkeypatch.setenv(CATALOG_PROMOTION_FLAG, "true")
    with pytest.raises(CatalogPostureInvalid):
        catalog_posture()
    assert catalog_promotion_enabled() is False


def test_promotion_requires_read_and_read_never_implies_promotion():
    """The ordering property, stated directly over the predicates."""
    on, off = "true", "false"
    # read alone
    env = {CATALOG_EXECUTION_FLAG: on, GOVERNMENT_READ_FLAG: on, CATALOG_PROMOTION_FLAG: off}
    assert government_read_enabled(env) is True
    assert catalog_promotion_enabled(env) is False
    # both
    env = {CATALOG_EXECUTION_FLAG: on, GOVERNMENT_READ_FLAG: on, CATALOG_PROMOTION_FLAG: on}
    assert government_read_enabled(env) is True
    assert catalog_promotion_enabled(env) is True
    # the master switch still outranks both
    env = {CATALOG_EXECUTION_FLAG: off, GOVERNMENT_READ_FLAG: on, CATALOG_PROMOTION_FLAG: on}
    assert government_read_enabled(env) is False
    assert catalog_promotion_enabled(env) is False


@pytest.mark.parametrize("value", [None, "false", "0", "off", "maybe", "", "truthy"])
def test_both_capability_flags_default_off_for_anything_unrecognised(monkeypatch, value):
    monkeypatch.setenv(CATALOG_EXECUTION_FLAG, "true")
    for name in (GOVERNMENT_READ_FLAG, CATALOG_PROMOTION_FLAG):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    assert government_read_enabled() is False
    assert catalog_promotion_enabled() is False


def test_neither_capability_flag_has_a_browser_facing_twin():
    """Server-only, exactly like the master switch."""
    for flag in (GOVERNMENT_READ_FLAG, CATALOG_PROMOTION_FLAG):
        assert not flag.startswith("NEXT_PUBLIC_")
        frontend = REPO / "frontend"
        hits = [path for path in frontend.rglob("*")
                if path.is_file() and path.suffix in {".ts", ".tsx", ".mjs", ".js"}
                and "node_modules" not in path.parts
                and flag in path.read_text(errors="ignore")]
        assert hits == [], f"{flag} is referenced in the browser tree: {hits}"

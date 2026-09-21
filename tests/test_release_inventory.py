"""The Production preflight inventory, and the release binding it feeds.

Two things are proven here.

1.  THE INVENTORY IS COMPLETE FOR CURRENT MAIN. Stage D's preflight carried a
    hand-written list of six RPCs, pinned when the guarded worker writes
    landed. Everything built since -- the durable execution-usage ledger,
    atomic guarded finalization, the current-verdict authority, the evidence
    and catalog writers, and the run-identity and fencing primitives -- became
    a RUNTIME dependency without becoming a PREFLIGHT requirement. A production
    database missing `finalize_run_guarded` passed every check and would have
    failed on the first paid model call, after the money was spent.

2.  THE RELEASE CHAIN IS CLOSED. `accepted runtime source -> policy bytes ->
    policy fingerprint -> immutable run identity -> built image -> image digest
    -> Stage D authorization`. Every link but one already had a check; the run
    identity is the one that did not exist, so nothing tied the run that
    ACTUALLY EXECUTED to the release that was authorized.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_DIR = REPO_ROOT / "scripts" / "release"
if str(RELEASE_DIR) not in sys.path:
    sys.path.insert(0, str(RELEASE_DIR))

import release_inventory  # noqa: E402
from migration_state import MARKERS, local_migrations  # noqa: E402


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


PROBE = _load(RELEASE_DIR / "stage-d" / "probe_db.py", "stage_d_probe_db")
ENVELOPE = _load(RELEASE_DIR / "stage-d" / "policy_envelope.py", "stage_d_policy_envelope")


# ---------------------------------------------------------------------------
# 1. the inventory
# ---------------------------------------------------------------------------
def test_the_inventory_derives_cleanly_from_the_current_repository():
    inventory = release_inventory.build()
    assert inventory["ok"], inventory["problems"]
    assert inventory["rpcs"], "no required RPCs were derived; the scan cannot be trusted"


def test_every_rpc_the_runtime_calls_is_created_by_a_migration():
    """A call with no defining migration is a dependency production could
    never satisfy."""
    called = release_inventory.runtime_rpc_calls()
    provided = release_inventory.migration_functions()
    missing = sorted(set(called) - set(provided))
    assert not missing, f"these RPCs are called but no migration creates them: {missing}"


def test_the_inventory_covers_every_console_1_to_5_runtime_dependency():
    """REQUIRED REGRESSION 9. Named explicitly so a silent removal of any one
    of them is a test failure and not merely a smaller list."""
    rpcs = release_inventory.build()["rpcs"]
    required = {
        # Console 2 -- durable ExecutionUsageLedger and the guarded usage write.
        "record_run_usage_guarded", "update_run_usage_guarded",
        # Console 3 -- atomic guarded finalization.
        "finalize_run_guarded",
        # Console 4 -- current-verdict authority and evidence support writes.
        "claim_current_verdict_states", "record_claim_verdict_guarded",
        "record_conflict_resolution_guarded", "record_evidence_fragment_guarded",
        # The lease/fencing core every console builds on.
        "claim_run_lease", "heartbeat_run_guarded", "transition_run_worker_guarded",
        "append_run_event_guarded", "save_checkpoint_guarded",
        # Console 6 -- run identity and the newly fenced writers.
        "bind_run_identity", "create_tool_access_request_guarded",
        "create_tool_grant_guarded", "append_usage_ledger_guarded",
    }
    missing = sorted(required - set(rpcs))
    assert not missing, f"the inventory omits runtime dependencies: {missing}"


def test_the_probes_pinned_literal_is_exactly_the_derived_inventory():
    """The probe runs in a bare image and cannot import the deriving module,
    so it keeps a reviewed literal -- and this is what stops that literal
    falling behind the runtime, which is exactly how it fell behind before."""
    derived = {name: set(args) for name, args in
               release_inventory.required_rpc_args().items()}
    pinned = {name: set(args) for name, args in PROBE.REQUIRED_RPC_ARGS.items()}
    assert pinned == derived, (
        "probe_db.py's REQUIRED_RPC_ARGS is not the inventory of current main. "
        "Regenerate with: python3 scripts/release/release_inventory.py rpcs")


def test_the_exact_signature_pins_carry_every_parameter_including_defaults():
    inventory = release_inventory.build()["rpcs"]
    for name, pinned in PROBE.EXACT_RPC_SIGNATURES.items():
        assert name in inventory, f"{name} is pinned exactly but is not required"
        assert set(pinned) == set(inventory[name]["args"]), (
            f"{name}: the exact pin must be every parameter, defaults included")


def test_a_deployment_missing_a_required_rpc_is_blocked():
    observed = {name: entry["args"] for name, entry
                in release_inventory.build()["rpcs"].items()}
    assert release_inventory.missing_from_deployment(observed) == []

    without = dict(observed)
    without.pop("finalize_run_guarded")
    problems = release_inventory.missing_from_deployment(without)
    assert any("finalize_run_guarded" in problem for problem in problems)

    unverifiable = {**observed, "record_run_usage_guarded": None}
    problems = release_inventory.missing_from_deployment(unverifiable)
    assert any("record_run_usage_guarded" in problem for problem in problems)

    renamed = {**observed, "bind_run_identity": ["p_run_id"]}
    problems = release_inventory.missing_from_deployment(renamed)
    assert any("p_identity" in problem for problem in problems)


def test_the_preflight_blocks_on_a_missing_required_rpc_before_any_run(monkeypatch):
    """The preflight runs before the authorized run is created, so a missing
    RPC blocks BEFORE any worker execution."""
    checks: dict[str, str] = {}
    problems: list[str] = []
    paths = {f"/rpc/{name}": {"post": {"parameters": [
        {"in": "body", "schema": {"properties": {arg: {} for arg in
                                                 release_inventory.build()["rpcs"][name]["args"]}}}]}}
        for name in PROBE.REQUIRED_RPC_ARGS}
    del paths["/rpc/finalize_run_guarded"]
    monkeypatch.setattr(PROBE, "call", lambda method, path, *a, **k: (200, {"paths": paths}))

    PROBE.check_rpc_surface(checks, problems)

    assert checks["rpc_finalize_run_guarded"] == "MISSING"
    assert any("finalize_run_guarded" in problem for problem in problems)


def test_an_unavailable_rpc_surface_fails_closed(monkeypatch):
    checks: dict[str, str] = {}
    problems: list[str] = []
    monkeypatch.setattr(PROBE, "call", lambda *a, **k: (503, None))
    PROBE.check_rpc_surface(checks, problems)
    assert problems and all(value == "UNVERIFIED" for value in checks.values())


def test_every_migration_that_creates_a_required_rpc_has_a_drift_marker():
    """A history row claiming a migration applied must be contradictable by
    the object that migration creates."""
    inventory = release_inventory.build()
    local = {entry["version"] for entry in local_migrations(REPO_ROOT / "supabase" / "migrations")}
    # The migration that INTRODUCES the object, not one that redefines it in
    # place: a redefinition creates nothing new, so per the marker rule it
    # correctly declares no marker.
    introducing = {entry["first_migration"] for entry in inventory["rpcs"].values()}
    unmarked = sorted(version for version in introducing
                      if version in local and version not in MARKERS)
    assert not unmarked, f"these migrations create required RPCs but have no marker: {unmarked}"


# ---------------------------------------------------------------------------
# 2. the release binding
# ---------------------------------------------------------------------------
RELEASE = "84cd8696119c24662a954d0f0e23195268dab23f"


def expected(monkeypatch, workflow_key="vehicle_catalog_v1"):
    monkeypatch.setenv("STAGE_D_RELEASE_SHA", RELEASE)
    monkeypatch.setenv("STAGE_D_WORKFLOW_KEY", workflow_key)
    return ENVELOPE.expected_run_identity()


def test_the_expected_run_identity_is_the_reviewed_policy_and_the_accepted_release(monkeypatch):
    from backend.event_registry import REGISTRY_VERSION
    from backend.run_identity import ENGINE_VERSIONS, IDENTITY_VERSION

    record = expected(monkeypatch)
    assert record["policy_fingerprint"] == ENVELOPE.PINNED_POLICY_FINGERPRINT
    assert record["engine_version"] == ENGINE_VERSIONS["vehicle_catalog_v1"]
    assert record["event_registry_version"] == REGISTRY_VERSION
    assert record["identity_version"] == IDENTITY_VERSION
    assert record["release_sha"] == RELEASE
    # run_id is a property of the run, never of the release.
    assert "run_id" not in record


def test_a_run_of_this_release_satisfies_the_binding(monkeypatch):
    from backend.run_identity import RunIdentity

    monkeypatch.setenv("MILO_RELEASE_SHA", RELEASE)
    identity = RunIdentity.bind("11111111-1111-4111-8111-000000000001",
                                "vehicle_catalog_v1").as_record()
    expected(monkeypatch)
    assert ENVELOPE.run_identity_problems(identity) == []


@pytest.mark.parametrize("mutation,expected_field", [
    ({"policy_fingerprint": "0" * 64}, "policy_fingerprint"),
    ({"release_sha": "b" * 40}, "release_sha"),
    ({"workflow_key": "swarm_v2"}, "workflow_key"),
    ({"engine_version": "vehicle_catalog_v1.stage2"}, "engine_version"),
    ({"event_registry_version": "milo-event-registry/0"}, "event_registry_version"),
    ({"release_sha": ""}, "release_sha"),
])
def test_the_binding_detects_a_policy_release_or_engine_mismatch(monkeypatch, mutation, expected_field):
    """REQUIRED REGRESSION 10."""
    from backend.run_identity import RunIdentity

    monkeypatch.setenv("MILO_RELEASE_SHA", RELEASE)
    identity = RunIdentity.bind("11111111-1111-4111-8111-000000000001",
                                "vehicle_catalog_v1").as_record()
    identity.update(mutation)
    expected(monkeypatch)

    problems = ENVELOPE.run_identity_problems(identity)
    assert problems and any(expected_field in problem for problem in problems)


def test_an_unpinned_or_absent_run_identity_is_refused(monkeypatch):
    expected(monkeypatch)
    assert ENVELOPE.run_identity_problems(None), "an unpinned run must be refused"
    assert ENVELOPE.run_identity_problems("not-an-object")
    assert ENVELOPE.run_identity_problems({})


def test_the_release_must_be_stated_for_the_binding_to_mean_anything(monkeypatch):
    from backend.run_identity import RunIdentity

    monkeypatch.delenv("MILO_RELEASE_SHA", raising=False)
    identity = RunIdentity.bind("11111111-1111-4111-8111-000000000001",
                                "vehicle_catalog_v1").as_record()
    monkeypatch.setenv("STAGE_D_RELEASE_SHA", "")
    monkeypatch.setenv("STAGE_D_WORKFLOW_KEY", "vehicle_catalog_v1")
    problems = ENVELOPE.run_identity_problems(identity)
    assert any("STAGE_D_RELEASE_SHA" in problem for problem in problems)


def test_the_probe_refuses_an_unparseable_or_releaseless_expectation(monkeypatch):
    for raw in ("", "   ", "not json", "[]", '{"release_sha": ""}'):
        monkeypatch.setenv("STAGE_D_EXPECTED_RUN_IDENTITY", raw)
        assert PROBE.expected_run_identity() is None, raw
    monkeypatch.setenv("STAGE_D_EXPECTED_RUN_IDENTITY",
                       json.dumps({"release_sha": RELEASE, "workflow_key": "swarm_v2",
                                   "run_id": "ignored"}))
    record = PROBE.expected_run_identity()
    assert record == {"release_sha": RELEASE, "workflow_key": "swarm_v2"}


def test_the_toolkit_may_reference_a_release_without_being_that_commit():
    """PR #103's rule, preserved. The reviewed commit that updates
    STAGE_D_RELEASE_SHA to release R cannot itself be R, so requiring
    HEAD == R would make re-authorization impossible."""
    source = (RELEASE_DIR / "stage-d" / "policy_envelope.py").read_text()
    assert "does **not** have to BE the release commit" in source or \
           "does not have to BE the release commit" in source
    binding = ENVELOPE.release_binding_problems.__doc__ or ""
    assert "NOT a check on which commit is checked out" in " ".join(binding.split())


def test_image_digest_verification_is_not_weakened():
    """A tag match is still never acceptance, on any surface."""
    verify_images = _load(RELEASE_DIR / "stage-d" / "verify_images.py", "stage_d_verify_images")
    problems: list[str] = []
    digest, how = verify_images.reference_digest(
        "reg/worker:" + RELEASE, "reg/worker", "sha256:" + "a" * 64, RELEASE,
        "sha256:" + "b" * 64, "worker job", problems)
    assert digest is None and how == "tag" and problems, (
        "a moved tag must never resolve to acceptance")

    problems = []
    digest, how = verify_images.reference_digest(
        "reg/worker@sha256:" + "b" * 64, "reg/worker", "sha256:" + "a" * 64,
        RELEASE, None, "worker job", problems)
    assert digest is None and how == "digest" and problems


def test_the_deployer_states_the_release_on_both_runtime_surfaces():
    """An unset MILO_RELEASE_SHA makes every run record no release at all."""
    for script in ("cloud-run.sh", "staging-cloud-run.sh"):
        source = (REPO_ROOT / "scripts" / "deploy" / script).read_text()
        assert source.count('"MILO_RELEASE_SHA=$RELEASE_SHA"') == 2, script


def test_verify_caps_refuses_a_deployment_that_states_no_release_or_the_wrong_one():
    verify_caps = _load(RELEASE_DIR / "stage-d" / "verify_caps.py", "stage_d_verify_caps")

    problems: list[str] = []
    verify_caps.check_release_identity({"MILO_RELEASE_SHA": RELEASE},
                                       {"MILO_RELEASE_SHA": RELEASE}, RELEASE, problems)
    assert problems == []

    problems = []
    verify_caps.check_release_identity({}, {"MILO_RELEASE_SHA": RELEASE}, RELEASE, problems)
    assert any("worker" in problem and "MISSING" in problem for problem in problems)

    problems = []
    verify_caps.check_release_identity({"MILO_RELEASE_SHA": "c" * 40},
                                       {"MILO_RELEASE_SHA": RELEASE}, RELEASE, problems)
    assert any("not the accepted release" in problem for problem in problems)

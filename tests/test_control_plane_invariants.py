"""The control-plane invariants, asserted together in one place.

Each authority below already has a full suite of its own; the point of this
file is different. It names, in one list, the properties that must survive
every later change to the control plane, so a refactor that quietly removes one
fails a test that says what was lost rather than only failing whichever
detailed assertion happened to touch it.

Consoles 1-5 established: the canonical RuntimePolicy, the durable
ExecutionUsageLedger, the canonical Finalizer and ProductOutcome, the canonical
Evidence Authority and current verdict, and the canonical ProviderAdapter.
This console adds the immutable run identity, full persistence fencing and the
canonical event registry, and must not weaken any of them.

Catalog promotion's default-off posture is asserted here too, because it is the
one setting whose accidental default would turn a read-only first paid run into
a run that writes canonical facts.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Console 1 -- the canonical RuntimePolicy
# ---------------------------------------------------------------------------
def test_the_runtime_policy_is_still_the_one_authority_and_imports_no_backend():
    from backend.runtime_policy import (POLICY_DIMENSIONS, POLICY_SCHEMA_VERSION,
                                        reviewed_first_run_policy)

    policy = reviewed_first_run_policy()
    assert policy.document()["schema_version"] == POLICY_SCHEMA_VERSION
    assert POLICY_DIMENSIONS, "the policy declares no dimensions"
    # The import discipline that lets every other configuration module depend
    # on it, and that lets the run identity bind its fingerprint without a
    # cycle: nothing from `backend` at MODULE SCOPE. Function-scope imports are
    # deliberate and are what keep the dependency one-directional.
    tree = ast.parse(Path("backend/runtime_policy.py").read_text())
    module_scope = {node.module for node in tree.body
                    if isinstance(node, ast.ImportFrom) and node.module}
    assert not any(module.startswith("backend") for module in module_scope)


def test_the_reviewed_policy_fingerprint_is_stable_for_one_document():
    from backend.runtime_policy import reviewed_first_run_policy

    assert (reviewed_first_run_policy().fingerprint()
            == reviewed_first_run_policy().fingerprint())


# ---------------------------------------------------------------------------
# Console 2 -- the durable ExecutionUsageLedger
# ---------------------------------------------------------------------------
def test_the_usage_ledger_merge_still_never_refunds_a_dimension():
    from backend.execution_usage import merge_usage_snapshots

    merged = merge_usage_snapshots({"model_calls": 5, "input_tokens": 100},
                                   {"model_calls": 2, "input_tokens": 400})
    assert merged["model_calls"] == 5
    assert merged["input_tokens"] == 400


def test_the_guarded_usage_write_still_requires_the_complete_lease():
    import inspect

    from backend.repository.supabase import SupabaseRepository

    params = inspect.signature(SupabaseRepository.record_run_usage).parameters
    assert {"worker_id", "attempt", "lease_token"} <= set(params)
    for name in ("worker_id", "attempt", "lease_token"):
        assert params[name].default is inspect.Parameter.empty


# ---------------------------------------------------------------------------
# Console 3 -- the canonical Finalizer and ProductOutcome
# ---------------------------------------------------------------------------
def test_terminal_authority_still_orders_a_cancellation_above_a_completion():
    from backend.finalization import TERMINAL_AUTHORITY

    assert TERMINAL_AUTHORITY["cancelled"] > TERMINAL_AUTHORITY["timed_out"]
    assert TERMINAL_AUTHORITY["timed_out"] > TERMINAL_AUTHORITY["failed"]
    assert TERMINAL_AUTHORITY["failed"] > TERMINAL_AUTHORITY["partial_success"]
    assert TERMINAL_AUTHORITY["partial_success"] > TERMINAL_AUTHORITY["completed"]


def test_product_outcome_is_still_the_one_classifier_the_export_uses():
    """The export must PROJECT the canonical outcome, never re-derive one."""
    source = Path("backend/export_envelope.py").read_text()
    assert "derive_product_outcome" in source
    assert "from backend.product_outcome import" in source


def test_a_v2_payload_the_contract_could_not_have_produced_is_still_refused():
    from backend.export_envelope import ExportRefused, build_export_envelope
    from backend.run_identity import RunIdentity

    run_id = "11111111-1111-4111-8111-000000000001"
    with pytest.raises(ExportRefused):
        build_export_envelope({
            "id": run_id, "status": "completed",
            "run_identity": RunIdentity.bind(run_id, "swarm_v2").as_record(),
            "output": {"status": "complete", "result_kind": "usable_result",
                       "fields": {}, "needs_review": []}})


# ---------------------------------------------------------------------------
# Console 4 -- the canonical Evidence Authority and current verdict
# ---------------------------------------------------------------------------
def test_the_current_verdict_contract_version_is_still_declared_once():
    import dataclasses

    from backend.engines.swarm_v2.current_verdict import (CURRENT_VERDICT_CONTRACT_VERSION,
                                                          CurrentVerdict)

    assert CURRENT_VERDICT_CONTRACT_VERSION
    fields = {field.name: field for field in dataclasses.fields(CurrentVerdict)}
    assert fields["contract_version"].default == CURRENT_VERDICT_CONTRACT_VERSION


def test_every_evidence_write_still_requires_the_complete_lease():
    import inspect

    from backend.repository.supabase import SupabaseRepository

    for name in ("record_evidence_fragment", "record_claim_verdict",
                 "record_conflict_resolution", "create_source", "create_claim"):
        params = inspect.signature(getattr(SupabaseRepository, name)).parameters
        assert {"worker_id", "attempt", "lease_token"} <= set(params), name


# ---------------------------------------------------------------------------
# Console 5 -- the canonical ProviderAdapter
# ---------------------------------------------------------------------------
def test_the_provider_host_is_still_resolved_from_one_authority():
    from backend.provider_authority import provider_base_url

    assert callable(provider_base_url)
    worker = Path("backend/worker/main.py").read_text()
    assert "from backend.provider_authority import ProviderAdapter, provider_base_url" in worker


def test_the_worker_still_builds_exactly_one_provider_adapter_for_both_engines():
    """Two schedulers meant two descriptions of one provider envelope."""
    source = Path("backend/worker/main.py").read_text()
    assert source.count("provider_adapter = ProviderAdapter(") == 1
    # Handed to V1 and to the V2 gateway as the SAME instance.
    assert "provider_adapter=provider_adapter," in source
    assert "adapter=provider_adapter," in source


# ---------------------------------------------------------------------------
# catalog promotion stays default OFF
# ---------------------------------------------------------------------------
def test_catalog_promotion_is_off_unless_explicitly_and_recognisably_armed(monkeypatch):
    """REQUIRED REGRESSION 11. Unset is off, `false` is off, empty is off, and
    a value nobody recognises is a misconfiguration whose safe reading is off."""
    from backend.catalog.execution import catalog_posture

    for name in ("MILO_ENABLE_CATALOG_EXECUTION", "MILO_ENABLE_GOVERNMENT_CATALOG_READ",
                 "MILO_ENABLE_CATALOG_PROMOTION"):
        monkeypatch.delenv(name, raising=False)
    assert catalog_posture() == {"master": False, "government_read": False,
                                 "promotion": False}

    # Every one of these is a value nobody recognises as "on". `"1 "` is
    # deliberately absent: it strips to the established true form `1`.
    for value in ("", " ", "false", "0", "no", "off", "TRUE-ish", "maybe", "enabled"):
        for name in ("MILO_ENABLE_CATALOG_EXECUTION", "MILO_ENABLE_GOVERNMENT_CATALOG_READ",
                     "MILO_ENABLE_CATALOG_PROMOTION"):
            monkeypatch.setenv(name, value)
        assert catalog_posture()["promotion"] is False, value


def test_promotion_can_never_be_armed_for_data_the_process_may_not_read(monkeypatch):
    from backend.catalog.execution import CatalogPostureInvalid, catalog_posture

    monkeypatch.setenv("MILO_ENABLE_CATALOG_EXECUTION", "true")
    monkeypatch.setenv("MILO_ENABLE_CATALOG_PROMOTION", "true")
    monkeypatch.delenv("MILO_ENABLE_GOVERNMENT_CATALOG_READ", raising=False)
    with pytest.raises(CatalogPostureInvalid):
        catalog_posture()


def test_the_repository_never_commits_the_promotion_flag_as_enabled():
    """The static scan is the authority; this asserts it still covers it."""
    scan = Path("scripts/check_unsafe_defaults.py").read_text()
    for flag in ("MILO_ENABLE_CATALOG_EXECUTION", "MILO_ENABLE_GOVERNMENT_CATALOG_READ",
                 "MILO_ENABLE_CATALOG_PROMOTION"):
        assert flag in scan, flag


# ---------------------------------------------------------------------------
# this console's additions do not weaken the ones above
# ---------------------------------------------------------------------------
def test_the_run_identity_binds_the_reviewed_policy_not_a_deployment_resolved_one():
    """Binding a DEPLOYMENT-resolved policy would make the identity depend on
    which surface created the run: caps go on both the API and the Worker, but
    provider and engine limits belong to the Worker alone, so the two would
    compute different fingerprints for one run."""
    from backend.run_identity import reviewed_policy_fingerprint
    from backend.runtime_policy import reviewed_first_run_policy

    assert reviewed_policy_fingerprint() == reviewed_first_run_policy().fingerprint()
    source = Path("backend/run_identity.py").read_text()
    assert "resolve_runtime_policy" not in source


def test_the_event_registry_transports_product_outcome_and_does_not_redefine_it():
    """Events carry ProductOutcome on the terminal event; the canonical
    outcome remains backend/product_outcome.py's alone."""
    registry_source = Path("backend/event_registry.py").read_text()
    assert "product_outcome" not in registry_source.lower().replace(
        "canonical outcome remains ``backend/product_outcome.py``'s alone.", "")
    from backend.finalization import _TERMINAL_EVENT
    from backend.event_registry import V1_EVENT_TYPES

    assert set(_TERMINAL_EVENT.values()) <= V1_EVENT_TYPES

"""The immutable run identity, and the persistence paths that must be fenced.

Two invariants are proven here, and they are the ones a run's whole later life
depends on:

1.  WHAT A RUN IS is decided once, before execution, and never again. Not by a
    worker re-reading a project row at claim time, not by an export reading the
    caller's own request metadata, and not by any consumer inferring it from a
    checkpoint, an output shape or an event stream.
2.  A WORKER THAT NO LONGER HOLDS THE LEASE cannot mutate run-owned durable
    state through ANY path -- the in-process repository writes, or the HTTP
    surfaces that used to accept a service identity as sufficient.
"""

from __future__ import annotations

import inspect
from uuid import UUID, uuid4

import pytest

from backend.errors import AppError
from backend.event_registry import REGISTRY_VERSION
from backend.export_envelope import ExportRefused, build_export_envelope, validate_export_envelope
from backend.run_identity import (ENGINE_VERSIONS, IDENTITY_FIELDS, IDENTITY_VERSION,
                                  RunIdentity, RunIdentityError,
                                  identity_mutation_problems, persisted_identity,
                                  release_sha, require_identity,
                                  reviewed_policy_fingerprint)
from backend.runtime_policy import POLICY_SCHEMA_VERSION
from backend.testing.memory_repository import MemoryRepository
from backend.worker.engine import EngineRegistry, EngineResolver

RUN_A = "11111111-1111-4111-8111-000000000001"
RUN_B = "22222222-2222-4222-8222-000000000002"


def identity(run_id=RUN_A, workflow_key="swarm_v2", **over):
    record = RunIdentity.bind(run_id, workflow_key).as_record()
    record.update(over)
    return record


# ---------------------------------------------------------------------------
# 1. the identity itself
# ---------------------------------------------------------------------------
def test_a_bound_identity_states_every_dimension_from_the_running_image():
    bound = RunIdentity.bind(RUN_A, "swarm_v2")

    assert bound.run_id == RUN_A
    assert bound.workflow_key == "swarm_v2"
    assert bound.engine_version == ENGINE_VERSIONS["swarm_v2"]
    assert bound.policy_version == POLICY_SCHEMA_VERSION
    assert bound.policy_fingerprint == reviewed_policy_fingerprint()
    assert bound.event_registry_version == REGISTRY_VERSION
    assert bound.identity_version == IDENTITY_VERSION
    assert set(bound.as_record()) == set(IDENTITY_FIELDS)


def test_the_engine_version_registry_is_the_only_place_either_engine_states_one():
    """Two copies of an engine version is how an identity and a checkpoint
    come to disagree about which engine wrote a run."""
    from backend.engines.swarm_v2.state import SwarmState
    from backend.engines.vehicle_catalog_v1.engine import ENGINE_VERSION

    assert ENGINE_VERSION == ENGINE_VERSIONS["vehicle_catalog_v1"]
    assert SwarmState.model_fields["engine_version"].default == ENGINE_VERSIONS["swarm_v2"]


def test_an_unstated_or_malformed_release_is_recorded_as_unstated_never_as_a_wildcard(monkeypatch):
    monkeypatch.delenv("MILO_RELEASE_SHA", raising=False)
    assert release_sha() == ""
    # A short SHA, a tag and a branch name are all "no release stated": a
    # binding an arbitrary string can satisfy is not a binding.
    for bad in ("84cd8696", "v1.2.3", "main", " " * 40, "Z" * 40):
        monkeypatch.setenv("MILO_RELEASE_SHA", bad)
        assert release_sha() == "", bad
    # A full hex SHA is accepted and normalised, so two spellings of one
    # release cannot look like two releases.
    monkeypatch.setenv("MILO_RELEASE_SHA", "  AB" + "c" * 38 + "  ")
    assert release_sha() == ("ab" + "c" * 38)


@pytest.mark.parametrize("broken,code", [
    (None, "RUN_IDENTITY_ABSENT"),
    ("swarm_v2", "RUN_IDENTITY_MALFORMED"),
    ({**identity(), "identity_version": "milo-run-identity/99"}, "RUN_IDENTITY_VERSION_UNKNOWN"),
    ({**identity(), "workflow_key": ""}, "RUN_IDENTITY_INCOMPLETE"),
    ({**identity(), "policy_fingerprint": ""}, "RUN_IDENTITY_INCOMPLETE"),
    ({**identity(), "workflow_key": "not_an_engine"}, "RUN_IDENTITY_WORKFLOW_UNKNOWN"),
    ({**identity(), "engine_version": "swarm_v2.0"}, "RUN_IDENTITY_ENGINE_MISMATCH"),
])
def test_an_untrustworthy_identity_fails_closed_with_a_static_code(broken, code):
    with pytest.raises(RunIdentityError) as raised:
        RunIdentity.from_record(broken)
    assert raised.value.code == code


def test_an_identity_lifted_from_another_run_is_refused():
    with pytest.raises(RunIdentityError) as raised:
        RunIdentity.from_record(identity(RUN_A, "swarm_v2"), run_id=RUN_B)
    assert raised.value.code == "RUN_IDENTITY_RUN_MISMATCH"


def test_a_run_without_an_identity_is_unpinned_not_guessed():
    """`None` means 'created before identities existed', and nothing more."""
    assert persisted_identity({"id": RUN_A}) is None
    with pytest.raises(RunIdentityError) as raised:
        require_identity({"id": RUN_A})
    assert raised.value.code == "RUN_IDENTITY_ABSENT"


def test_an_identity_may_be_rebound_identically_and_never_changed():
    held = identity(RUN_A, "swarm_v2")
    assert identity_mutation_problems(held, dict(held)) == []
    assert identity_mutation_problems(None, held) == []

    changed = {**held, "workflow_key": "vehicle_catalog_v1",
               "engine_version": ENGINE_VERSIONS["vehicle_catalog_v1"]}
    problems = identity_mutation_problems(held, changed)
    assert problems and "immutable" in problems[0]
    assert "workflow_key" in problems[0] and "engine_version" in problems[0]


# ---------------------------------------------------------------------------
# 2. the repository refuses a rewrite (parity with the database trigger)
# ---------------------------------------------------------------------------
def seeded_repo(workflow_key="swarm_v2"):
    repo = MemoryRepository()
    project_id, user_id = uuid4(), uuid4()
    repo.seed_user(str(user_id))
    repo.seed_project(str(project_id), "identity", "Identity", [str(user_id)])
    repo.projects[str(project_id)]["workflow_key"] = workflow_key
    conversation = repo.create_conversation(project_id, "identity")
    created = repo.create_message_and_run(conversation["id"], "go", {}, user_id,
                                          f"key-{uuid4()}", "fingerprint")
    return repo, UUID(str(created["run"]["id"])), project_id


def test_a_bound_run_cannot_be_rebound_to_a_different_engine():
    """REQUIRED REGRESSION 1: workflow/engine identity cannot change after
    run creation. A V2 run must never later look like a V1 one."""
    repo, run_id, _ = seeded_repo("swarm_v2")
    repo.bind_run_identity(run_id, identity(str(run_id), "swarm_v2"))

    with pytest.raises(AppError) as raised:
        repo.bind_run_identity(run_id, identity(str(run_id), "vehicle_catalog_v1"))
    assert raised.value.code == "RUN_IDENTITY_IMMUTABLE"
    assert repo.get_run(run_id)["run_identity"]["workflow_key"] == "swarm_v2"


def test_rebinding_the_identical_identity_is_a_no_op():
    repo, run_id, _ = seeded_repo()
    record = identity(str(run_id), "swarm_v2")
    first = repo.bind_run_identity(run_id, record)
    second = repo.bind_run_identity(run_id, dict(record))
    assert first["run_identity"] == second["run_identity"] == record


def test_an_identity_naming_another_run_is_refused_by_the_binder():
    repo, run_id, _ = seeded_repo()
    with pytest.raises(AppError) as raised:
        repo.bind_run_identity(run_id, identity(RUN_B, "swarm_v2"))
    assert raised.value.code == "RUN_IDENTITY_INVALID"


# ---------------------------------------------------------------------------
# 3. routing reads the identity; a project change cannot move a bound run
# ---------------------------------------------------------------------------
class FakeEngine:
    def __init__(self, workflow_key):
        self.workflow_key = workflow_key

    def run(self, run):
        return {"status": "success", "result": {}}


REGISTRY = EngineRegistry({
    "vehicle_catalog_v1": lambda: FakeEngine("vehicle_catalog_v1"),
    "swarm_v2": lambda: FakeEngine("swarm_v2"),
})


def test_changing_the_project_workflow_cannot_change_a_bound_run():
    """The defect this whole change exists for: the resolver used to read the
    PROJECT's current workflow_key on every claim, so a project switched
    between creation and launch changed what the run was."""
    repo, run_id, project_id = seeded_repo("swarm_v2")
    repo.bind_run_identity(run_id, identity(str(run_id), "swarm_v2"))
    repo.projects[str(project_id)]["workflow_key"] = "vehicle_catalog_v1"

    resolved = EngineResolver(repo, REGISTRY).resolve(repo.get_run(run_id))

    assert resolved.workflow_key == "swarm_v2"
    assert resolved.pinned is True


def test_resume_and_retry_resolve_the_identical_identity():
    """REQUIRED REGRESSION 2: resume/retry preserves exact run identity."""
    repo, run_id, project_id = seeded_repo("swarm_v2")
    record = identity(str(run_id), "swarm_v2")
    repo.bind_run_identity(run_id, record)
    resolver = EngineResolver(repo, REGISTRY)

    first = resolver.resolve(repo.get_run(run_id))
    # A crashed attempt, a reclaimed lease, a changed project, a later attempt.
    repo.claim_run(run_id, "worker-1")
    repo.runs[str(run_id)]["lease_expires_at"] = "1970-01-01T00:00:00+00:00"
    repo.projects[str(project_id)]["workflow_key"] = "vehicle_catalog_v1"
    repo.claim_run(run_id, "worker-2")
    second = resolver.resolve(repo.get_run(run_id))

    assert repo.get_run(run_id)["attempt"] == 2
    assert first.identity == second.identity
    assert second.identity.as_record() == record


def test_a_corrupt_identity_is_never_downgraded_to_the_legacy_project_route():
    repo, run_id, _ = seeded_repo("swarm_v2")
    repo.runs[str(run_id)]["run_identity"] = {"identity_version": "milo-run-identity/1",
                                              "workflow_key": "swarm_v2"}
    with pytest.raises(AppError) as raised:
        EngineResolver(repo, REGISTRY).resolve(repo.get_run(run_id))
    assert raised.value.code == "ENGINE_NOT_ALLOWED"


def test_a_run_created_before_identities_still_routes_and_says_it_is_unpinned():
    repo, run_id, _ = seeded_repo("vehicle_catalog_v1")
    resolved = EngineResolver(repo, REGISTRY).resolve(repo.get_run(run_id))
    assert resolved.workflow_key == "vehicle_catalog_v1"
    assert resolved.pinned is False


# ---------------------------------------------------------------------------
# 4. export
# ---------------------------------------------------------------------------
V2_OUTPUT = {"status": "complete", "result_kind": "usable_result",
             "fields": {"engine": [{"value": "1.6T",
                                    "provenance": {"claim_id": "c1", "source_id": "gov:1",
                                                   "run_id": "r", "task_id": "t",
                                                   "scope": {}}}]},
             "needs_review": []}


def test_a_v2_run_cannot_export_as_v1():
    """REQUIRED REGRESSION 3. The old projection read the CALLER's request
    metadata first and defaulted to 'vehicle_catalog_v1', so a V2 run whose
    input did not name its workflow exported as a V1 one."""
    run = {"id": RUN_A, "status": "completed",
           "run_identity": identity(RUN_A, "swarm_v2"),
           # Both of the sources the old expression trusted, both lying.
           "input": {"workflow_key": "vehicle_catalog_v1"},
           "workflow_key": "vehicle_catalog_v1",
           "output": V2_OUTPUT}

    envelope = build_export_envelope(run)
    validate_export_envelope(envelope)

    assert envelope["engine"] == "swarm_v2"
    assert envelope["run_identity"]["workflow_key"] == "swarm_v2"


def test_export_preserves_the_canonical_product_outcome_and_the_whole_identity():
    """REQUIRED REGRESSION 8."""
    from backend.product_outcome import derive_product_outcome

    record = identity(RUN_A, "swarm_v2")
    run = {"id": RUN_A, "status": "completed", "run_identity": record,
           "output": V2_OUTPUT, "usage": {"model_calls": 3}}

    envelope = build_export_envelope(run)
    validate_export_envelope(envelope)

    canonical = derive_product_outcome("swarm_v2", V2_OUTPUT)
    assert envelope["result_kind"] == canonical.result_kind
    assert envelope["result"] is run["output"], "the result was rewritten"
    assert envelope["run_identity"] == record
    assert envelope["usage"] == {"model_calls": 3}


@pytest.mark.parametrize("run", [
    {"id": RUN_A, "status": "completed", "output": V2_OUTPUT},
    {"id": RUN_A, "status": "completed", "output": V2_OUTPUT, "run_identity": {}},
    {"id": RUN_A, "status": "completed", "output": V2_OUTPUT,
     "run_identity": {**identity(RUN_A, "swarm_v2"), "engine_version": "swarm_v2.0"}},
    {"id": RUN_A, "status": "completed", "output": V2_OUTPUT,
     "run_identity": identity(RUN_B, "swarm_v2")},
])
def test_an_unknown_identity_refuses_the_export_instead_of_guessing(run):
    """REQUIRED REGRESSION 4: unknown identity fails closed."""
    with pytest.raises(ExportRefused) as raised:
        build_export_envelope(run)
    assert "engine identity" in str(raised.value)


def test_validation_refuses_an_envelope_whose_engine_and_identity_disagree():
    envelope = build_export_envelope({"id": RUN_A, "status": "completed",
                                      "run_identity": identity(RUN_A, "swarm_v2"),
                                      "output": V2_OUTPUT})
    envelope["engine"] = "vehicle_catalog_v1"
    with pytest.raises(ExportRefused, match="disagree"):
        validate_export_envelope(envelope)


def test_the_export_module_has_no_engine_fallback_left_in_its_source():
    """Checked over the SOURCE, so a re-introduced default cannot pass review
    merely because the tests above happen to supply an identity."""
    from backend import export_envelope

    source = inspect.getsource(export_envelope.build_export_envelope)
    assert 'or "vehicle_catalog_v1"' not in source
    assert '(run.get("input") or {}).get("workflow_key")' not in source


# ---------------------------------------------------------------------------
# 5. the browser-visible projection
# ---------------------------------------------------------------------------
def test_the_run_read_states_the_identity_and_degrades_instead_of_failing():
    """Strictness belongs where a guess would be laundered into an
    authoritative document. The run read is the endpoint the workspace polls
    several times a second: failing it on one malformed stored field would take
    the workspace down rather than degrade it, so an unreadable identity is
    simply not stated and the browser falls back to the project."""
    from backend.main import _safe_run_identity

    record = identity(RUN_A, "swarm_v2")
    stated = _safe_run_identity(record)
    assert stated is not None and stated.workflow_key == "swarm_v2"

    for unreadable in (None, {}, "swarm_v2", [], {"workflow_key": "swarm_v2"},
                       {**record, "run_id": "not-a-uuid"}):
        assert _safe_run_identity(unreadable) is None, unreadable


def test_the_run_read_never_exposes_the_lease_token():
    from backend.main import _safe_run_response

    safe = _safe_run_response({"id": RUN_A, "status": "running",
                               "lease_token": "secret-lease",
                               "run_identity": identity(RUN_A, "swarm_v2")})
    assert "lease_token" not in safe
    assert safe["run_identity"].workflow_key == "swarm_v2"

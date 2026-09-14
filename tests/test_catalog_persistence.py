"""Catalog PR1: the durable catalog write path, at the repository boundary.

Offline and deterministic. `tests/test_migrations_postgres.py` proves these
rules against real PostgreSQL; this module proves the two things that live
above the database:

*   the Supabase repository reaches the catalog ONLY through the guarded RPCs,
    with a complete lease, never through a direct insert and never through a
    function name assembled from a payload;
*   the in-memory repository used by the isolated stacks MIRRORS the
    invariants rather than accepting everything, so a unit test written
    against it cannot pass on behavior PostgreSQL rejects.

Nothing here opens a connection, applies a migration or calls a provider.
"""

from uuid import UUID, uuid4

import pytest

from backend.catalog.contracts import (CANDIDATE_STATUSES, CATALOG_SOURCE_FAMILIES,
                                       is_evidence_family)
from backend.errors import AppError
from backend.repository.supabase import Repository, SupabaseRepository
from backend.testing.memory_repository import MemoryRepository

from tests.test_repository_supabase import FakeClient  # the same fake, one definition


#: Method -> (guarded RPC, the payload parameter it fills).
CATALOG_WRITES = (
    ("record_catalog_snapshot", "record_catalog_snapshot_guarded", "p_snapshot"),
    ("record_catalog_raw_record", "record_catalog_raw_record_guarded", "p_record"),
    ("activate_catalog_snapshot", "activate_catalog_snapshot_guarded", "p_activation"),
    ("record_catalog_candidate", "record_catalog_candidate_guarded", "p_candidate"),
    ("link_catalog_candidate_evidence", "link_catalog_candidate_evidence_guarded", "p_link"),
)


@pytest.fixture
def repo():
    repository = SupabaseRepository.__new__(SupabaseRepository)
    repository.client = FakeClient()
    return repository


# =============================================================================
# 1. the Supabase boundary
# =============================================================================

@pytest.mark.parametrize("method, rpc, payload_name", CATALOG_WRITES)
def test_every_catalog_write_is_a_guarded_rpc_with_a_complete_lease(repo, method, rpc, payload_name):
    """No direct insert, and the lease travels with every call.

    A direct table insert would bypass the lease check, the idempotency
    identity and the cross-run guards -- all of which live in the database so
    they hold for every caller rather than for one code path.
    """
    payload = {"snapshot_key": "snap-1"}
    getattr(repo, method)(uuid4(), payload, worker_id="worker-1", attempt=3, lease_token="lease-1")
    name, params = repo.client.rpc_calls[-1]
    assert name == rpc
    assert params[payload_name] == payload
    assert (params["p_worker_id"], params["p_attempt"], params["p_lease_token"]) == ("worker-1", 3, "lease-1")
    assert repo.client.inserted == [] and repo.client.updated == []


@pytest.mark.parametrize("method, rpc, payload_name", CATALOG_WRITES)
def test_a_catalog_write_refuses_an_incomplete_lease_before_it_calls_anything(
        repo, method, rpc, payload_name):
    with pytest.raises(Exception, match="complete worker lease is required"):
        getattr(repo, method)(uuid4(), {}, worker_id="", attempt=0, lease_token="")
    assert repo.client.rpc_calls == []


def test_the_rpc_name_is_a_literal_and_is_never_taken_from_the_payload(repo):
    """No dynamic function or table selection from model or user input.

    The payload is passed as ONE jsonb parameter; it never contributes to the
    function name, so nothing a model emits can steer which relation is
    written.
    """
    hostile = {"table": "catalog_models", "rpc": "drop_everything",
               "snapshot_key": "'; drop table public.runs; --"}
    for method, rpc, _ in CATALOG_WRITES:
        getattr(repo, method)(uuid4(), hostile, worker_id="w", attempt=1, lease_token="t")
        assert repo.client.rpc_calls[-1][0] == rpc
    assert {call[0] for call in repo.client.rpc_calls} == {rpc for _, rpc, _ in CATALOG_WRITES}


def test_the_repository_protocol_declares_the_catalog_methods_and_no_promotion():
    """PR1 adds persistence only.

    There is no promote/publish/canonical method on the protocol, because
    there is nothing for one to call: the canonical relations are read-only in
    the database until PR3 grants what it needs.
    """
    declared = set(Repository.__annotations__) | {name for name in dir(Repository)
                                                  if not name.startswith("_")}
    for method, _, _ in CATALOG_WRITES:
        assert method in declared, method
        assert hasattr(SupabaseRepository, method)
        assert hasattr(MemoryRepository, method)
    forbidden = [name for name in dir(SupabaseRepository)
                 if not name.startswith("_")
                 and any(marker in name for marker in ("promote", "canonical_model",
                                                       "publish_catalog", "seed_catalog"))]
    assert forbidden == [], forbidden


# =============================================================================
# 2. the in-memory mirror
# =============================================================================

def leased_run(repository: MemoryRepository) -> tuple[UUID, dict]:
    """One run holding an active lease, through the real memory-repo path."""
    user = uuid4()
    repository.users.add(str(user))
    project = repository.create_project_from_proposal(
        uuid4(), "catalog", "Catalog", None, {}, created_by=user)
    conversation = repository.create_conversation(UUID(project["id"]), "c", user_id=user)
    message = repository.create_user_message(UUID(conversation["id"]), "hello", {})
    run = repository.create_queued_run(UUID(conversation["id"]), message["id"], "hello", {},
                                       requested_by=user, idempotency_key="catalog-key-1")
    claimed = repository.claim_run(UUID(run["id"]), "worker-1", lease_seconds=300)
    return UUID(run["id"]), {"worker_id": "worker-1", "attempt": claimed["attempt"],
                             "lease_token": claimed["lease_token"]}


def snapshot_payload(key="snap-1", *, family="government", declared=1, **extra):
    payload = {"snapshot_key": key, "source_family": family,
               "resource_id": "142afde2-6228-49f9-8a29-9b6c3a0cbe40",
               "upstream_version": "2026.09.1", "upstream_version_kind": "dataset_version",
               "content_sha256": "a" * 64, "retrieved_at": "2026-09-14T16:11:13.272Z",
               "declared_record_count": declared}
    payload.update(extra)
    return payload


def record_payload(snapshot_id, key="rec-1", **extra):
    payload = {"snapshot_id": snapshot_id, "record_key": key, "upstream_record_id": "36327",
               "resource_id": "142afde2-6228-49f9-8a29-9b6c3a0cbe40",
               "payload": {"_id": 36327}, "payload_sha256": "b" * 64}
    payload.update(extra)
    return payload


def candidate_payload(snapshot_id, record_id, key="cand-1", **extra):
    payload = {"snapshot_id": snapshot_id, "raw_record_id": record_id, "candidate_key": key,
               "manufacturer": "Toyota", "commercial_model": "RAV4",
               "model_year_start": 2021, "model_year_end": 2021,
               "official_model_code": "AXAP54L-ANXGBW", "trim": "PRIME AWD SE",
               "identity_dimensions": {"drivetrain": "awd"}, "status": "candidate"}
    payload.update(extra)
    return payload


@pytest.fixture
def memory():
    repository = MemoryRepository()
    run_id, lease = leased_run(repository)
    return repository, run_id, lease


def test_the_canonical_catalog_starts_empty_and_no_write_path_fills_it(memory):
    """The product decision, at the repository boundary.

    Every catalog write this PR adds is exercised; the canonical lists are
    still empty afterwards, and nothing on the repository can append to them.
    """
    repository, run_id, lease = memory
    assert repository.catalog_models == [] and repository.catalog_model_variants == []
    snapshot = repository.record_catalog_snapshot(run_id, snapshot_payload(), **lease)
    record = repository.record_catalog_raw_record(
        run_id, record_payload(snapshot["id"]), **lease)
    repository.activate_catalog_snapshot(run_id, {"snapshot_id": snapshot["id"]}, **lease)
    candidate = repository.record_catalog_candidate(
        run_id, candidate_payload(snapshot["id"], record["id"]), **lease)
    source = repository.create_source(run_id, {"url": "https://example.test/a"})
    repository.link_catalog_candidate_evidence(
        run_id, {"candidate_id": candidate["id"], "source_id": source["id"],
                 "link_key": "link-1", "record_locator": "[]", "source_version": "2026.09.1",
                 "source_version_kind": "dataset_version"}, **lease)
    assert repository.catalog_models == [] and repository.catalog_model_variants == []
    # No legacy row was seeded anywhere either: every catalog row present is
    # one this test wrote.
    assert len(repository.catalog_snapshots) == 1
    assert len(repository.catalog_raw_records) == 1
    assert len(repository.catalog_candidates) == 1
    assert len(repository.catalog_evidence_links) == 1


def test_identical_replay_is_idempotent_and_conflicting_replay_fails_closed(memory):
    repository, run_id, lease = memory
    snapshot = repository.record_catalog_snapshot(run_id, snapshot_payload(declared=2), **lease)
    assert repository.record_catalog_snapshot(
        run_id, snapshot_payload(declared=2), **lease)["id"] == snapshot["id"]
    assert len(repository.catalog_snapshots) == 1
    with pytest.raises(AppError, match="snapshot idempotency conflict"):
        repository.record_catalog_snapshot(run_id, snapshot_payload(declared=3), **lease)

    record = repository.record_catalog_raw_record(run_id, record_payload(snapshot["id"]), **lease)
    assert repository.record_catalog_raw_record(
        run_id, record_payload(snapshot["id"]), **lease)["id"] == record["id"]
    # The snapshot's completeness counter advanced exactly once.
    assert repository.catalog_snapshots["snap-1"]["stored_record_count"] == 1
    with pytest.raises(AppError, match="record idempotency conflict"):
        repository.record_catalog_raw_record(
            run_id, record_payload(snapshot["id"], payload={"_id": 999}), **lease)


def test_a_snapshot_is_usable_only_after_complete_validation(memory):
    """The R5 pagination lesson: 1 of 2 records is not a complete capture."""
    repository, run_id, lease = memory
    snapshot = repository.record_catalog_snapshot(run_id, snapshot_payload(declared=2), **lease)
    repository.record_catalog_raw_record(run_id, record_payload(snapshot["id"]), **lease)
    with pytest.raises(AppError, match="catalog snapshot is incomplete"):
        repository.activate_catalog_snapshot(run_id, {"snapshot_id": snapshot["id"]}, **lease)
    assert repository.catalog_snapshots["snap-1"]["activated_at"] is None

    repository.record_catalog_raw_record(
        run_id, record_payload(snapshot["id"], key="rec-2", upstream_record_id="37392",
                               payload_sha256="c" * 64), **lease)
    activated = repository.activate_catalog_snapshot(run_id, {"snapshot_id": snapshot["id"]}, **lease)
    assert activated["activated_at"] is not None and activated["validation_state"] == "complete"
    # An active snapshot is frozen: no further record may be appended.
    with pytest.raises(AppError, match="an active catalog snapshot is immutable"):
        repository.record_catalog_raw_record(
            run_id, record_payload(snapshot["id"], key="rec-3"), **lease)


def test_a_stale_worker_writes_nothing_on_any_catalog_path(memory):
    """Superseding the lease invalidates every catalog write at once."""
    repository, run_id, lease = memory
    snapshot = repository.record_catalog_snapshot(run_id, snapshot_payload(), **lease)
    before = (len(repository.catalog_snapshots), len(repository.catalog_raw_records))
    stale = [{**lease, "worker_id": "other-worker"},
             {**lease, "attempt": lease["attempt"] + 1},
             {**lease, "lease_token": "not-the-token"}]
    for bad in stale:
        for call in (lambda kw: repository.record_catalog_snapshot(run_id, snapshot_payload("snap-never"), **kw),
                     lambda kw: repository.record_catalog_raw_record(run_id, record_payload(snapshot["id"], "rec-never"), **kw),
                     lambda kw: repository.activate_catalog_snapshot(run_id, {"snapshot_id": snapshot["id"]}, **kw),
                     lambda kw: repository.record_catalog_candidate(run_id, {}, **kw),
                     lambda kw: repository.link_catalog_candidate_evidence(run_id, {}, **kw)):
            with pytest.raises(AppError, match="no longer (held|active)") as refusal:
                call(bad)
            assert refusal.value.code == "RUN_TRANSITION_CONFLICT"
    assert (len(repository.catalog_snapshots), len(repository.catalog_raw_records)) == before
    assert repository.catalog_snapshots["snap-1"]["activated_at"] is None


def test_cross_snapshot_and_cross_run_linkage_are_both_rejected(memory):
    repository, run_id, lease = memory
    first = repository.record_catalog_snapshot(run_id, snapshot_payload("snap-a"), **lease)
    second = repository.record_catalog_snapshot(run_id, snapshot_payload("snap-b"), **lease)
    record = repository.record_catalog_raw_record(run_id, record_payload(first["id"]), **lease)
    # A candidate must be filed under the snapshot its record belongs to.
    with pytest.raises(AppError, match="candidate snapshot mismatch"):
        repository.record_catalog_candidate(
            run_id, candidate_payload(second["id"], record["id"]), **lease)
    # A record may not join a snapshot describing another resource.
    with pytest.raises(AppError, match="resource mismatch"):
        repository.record_catalog_raw_record(
            run_id, record_payload(second["id"], key="rec-x", resource_id="other-resource"), **lease)

    candidate = repository.record_catalog_candidate(
        run_id, candidate_payload(first["id"], record["id"]), **lease)
    # Another run's source cannot be attached to this run's candidate.
    other_run, _ = leased_run(repository)
    foreign = repository.create_source(other_run, {"url": "https://example.test/foreign"})
    with pytest.raises(AppError, match="invalid catalog evidence link source"):
        repository.link_catalog_candidate_evidence(
            run_id, {"candidate_id": candidate["id"], "source_id": foreign["id"],
                     "link_key": "link-x", "record_locator": "[]",
                     "source_version": "2026.09.1",
                     "source_version_kind": "dataset_version"}, **lease)
    assert repository.catalog_evidence_links == {}


def test_a_candidate_may_stay_ambiguous_and_never_carries_a_guessed_identity(memory):
    repository, run_id, lease = memory
    snapshot = repository.record_catalog_snapshot(run_id, snapshot_payload(), **lease)
    record = repository.record_catalog_raw_record(run_id, record_payload(snapshot["id"]), **lease)
    ambiguous = repository.record_catalog_candidate(
        run_id, candidate_payload(snapshot["id"], record["id"], status="ambiguous", trim=None),
        **lease)
    assert ambiguous["status"] == "ambiguous" and ambiguous["trim"] is None
    # Replaying it never resolves it.
    assert repository.record_catalog_candidate(
        run_id, candidate_payload(snapshot["id"], record["id"], status="ambiguous", trim=None),
        **lease)["status"] == "ambiguous"
    # A later reading may revise the READING, never the identity.
    assert repository.record_catalog_candidate(
        run_id, candidate_payload(snapshot["id"], record["id"], status="ready_for_review",
                                  trim=None), **lease)["status"] == "ready_for_review"
    with pytest.raises(AppError, match="candidate idempotency conflict"):
        repository.record_catalog_candidate(
            run_id, candidate_payload(snapshot["id"], record["id"], trim="XSE"), **lease)

    for guess in ({"identity_dimensions": {"drivetrain": ""}},
                  {"identity_dimensions": {"drivetrain": " awd"}},
                  {"identity_dimensions": {"horsepower": "302"}}):
        with pytest.raises(ValueError):
            repository.record_catalog_candidate(
                run_id, candidate_payload(snapshot["id"], record["id"], candidate_key="cand-g",
                                          **guess), **lease)
    with pytest.raises(AppError, match="model year range must be whole"):
        repository.record_catalog_candidate(
            run_id, candidate_payload(snapshot["id"], record["id"], candidate_key="cand-half",
                                      model_year_end=None), **lease)
    with pytest.raises(AppError, match="invalid catalog candidate status"):
        repository.record_catalog_candidate(
            run_id, candidate_payload(snapshot["id"], record["id"], candidate_key="cand-v",
                                      status="verified"), **lease)


def test_the_legacy_catalog_is_unverified_and_can_never_carry_a_verdict(memory):
    """The old catalog suggests; it never confirms.

    It is incomplete and holds incorrect values, so it is not canonical, not
    a seed, and not evidence -- and the trust state is derived from the family
    rather than accepted from the caller, so a trusted path cannot submit it
    as evidence either.
    """
    repository, run_id, lease = memory
    assert set(CATALOG_SOURCE_FAMILIES) == {"government", "manufacturer", "legacy_reference"}
    assert not is_evidence_family("legacy_reference")
    legacy = repository.record_catalog_snapshot(
        run_id, snapshot_payload("snap-legacy", family="legacy_reference"), **lease)
    assert legacy["trust_state"] == "unverified"
    with pytest.raises(AppError, match="pinned to the source family"):
        repository.record_catalog_snapshot(
            run_id, snapshot_payload("snap-forced", family="legacy_reference",
                                     trust_state="evidence"), **lease)

    record = repository.record_catalog_raw_record(run_id, record_payload(legacy["id"]), **lease)
    candidate = repository.record_catalog_candidate(
        run_id, candidate_payload(legacy["id"], record["id"]), **lease)
    source = repository.create_source(run_id, {"url": "https://example.test/legacy"})
    link = {"candidate_id": candidate["id"], "source_id": source["id"],
            "record_locator": "[]", "source_version": "2026.09.1",
            "source_version_kind": "dataset_version"}
    # Discovery is fine...
    repository.link_catalog_candidate_evidence(
        run_id, {**link, "link_key": "link-discovery", "claim_id": "claim-1"}, **lease)
    # ...verification is not.
    with pytest.raises(AppError, match="unverified catalog source cannot carry a verdict"):
        repository.link_catalog_candidate_evidence(
            run_id, {**link, "link_key": "link-verdict", "claim_id": "claim-1",
                     "verdict_id": "verdict-1"}, **lease)
    # A verdict with no claim beside it is never provenance, on any family.
    with pytest.raises(AppError, match="verdict requires its claim"):
        repository.link_catalog_candidate_evidence(
            run_id, {**link, "link_key": "link-orphan", "verdict_id": "verdict-1"}, **lease)


def test_the_candidate_status_vocabulary_admits_ambiguity_as_a_real_answer():
    """`ambiguous` is in the closed vocabulary, and nothing resolves it for us."""
    assert set(CANDIDATE_STATUSES) == {"candidate", "ambiguous", "rejected", "ready_for_review"}

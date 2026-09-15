"""The durable catalog write path at the repository boundary.

Offline and deterministic. `tests/test_migrations_postgres.py` proves these
rules against real PostgreSQL; this module proves the two things that live
above the database:

*   the Supabase repository reaches the catalog ONLY through the guarded RPCs,
    with a complete lease, deriving every identity key and never through a
    direct insert or a function name assembled from a payload;
*   the in-memory repository REJECTS everything PostgreSQL rejects, so a unit
    test written against it cannot pass on behavior the database refuses.

That second property is the point of this module after the corrective round.
Catalog PR1's memory implementation accepted invented claim and verdict ids
and stored whatever locator and version the caller sent, so a test could
"prove" a link was well-formed while PostgreSQL would have refused it. Every
rejection below therefore names the PostgreSQL test that proves the same rule.

Nothing here opens a connection, applies a migration or calls a provider.
"""

import hashlib
from uuid import UUID, uuid4

import pytest

from backend.catalog.contracts import CANDIDATE_STATUSES, CATALOG_SOURCE_FAMILIES, is_evidence_family
from backend.catalog.digest import canonical_payload_text, catalog_payload_digest
from backend.catalog.keys import CatalogKeyError
from backend.catalog.payloads import CatalogPayloadError
from backend.errors import AppError
from backend.repository.supabase import Repository, SupabaseRepository
from backend.testing import evidence_fixtures as F
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

#: The R3 provenance a real source and claim carry, and which a link derives
#: rather than accepts. Imported from the shared fixture so the memory tests
#: and the PostgreSQL cross-backend test cite one definition, not two.
LOCATOR = F.LOCATOR
VERSION_KIND, VERSION_ID = F.SOURCE_VERSION_KIND, F.SOURCE_VERSION_ID
FRAGMENT_TEXT, FRAGMENT_HASH = F.FRAGMENT_TEXT, F.FRAGMENT_HASH


# =============================================================================
# helpers: payloads that are VALID under the corrected contract
# =============================================================================

def snapshot_payload(*, family="government", declared=1, content=None, resource=None, **extra):
    payload = {"source_family": family,
               "resource_id": resource or "142afde2-6228-49f9-8a29-9b6c3a0cbe40",
               "upstream_version": "2026.09.1", "upstream_version_kind": "dataset_version",
               "content_sha256": content or ("a" * 64),
               "retrieved_at": "2026-09-14T16:11:13.272Z", "declared_record_count": declared}
    payload.update(extra)
    return payload


def record_payload(snapshot, *, upstream="36327", body=None, **extra):
    payload = {"snapshot_id": snapshot["id"], "snapshot_key": snapshot["snapshot_key"],
               "resource_id": snapshot["resource_id"], "upstream_record_id": upstream,
               "payload": body or {"_id": 36327}}
    payload.update(extra)
    return payload


def candidate_payload(record, *, status="candidate", **extra):
    payload = {"snapshot_id": record["snapshot_id"], "raw_record_id": record["id"],
               "record_key": record["record_key"], "manufacturer": "Toyota",
               "commercial_model": "RAV4", "model_year_start": 2021, "model_year_end": 2021,
               "official_model_code": "AXAP54L-ANXGBW", "trim": "PRIME AWD SE",
               "identity_dimensions": {"drivetrain": "awd"}, "status": status}
    payload.update(extra)
    return payload


def link_payload(candidate, source, claim, *, verdict=None, **extra):
    payload = {"candidate_id": candidate["id"], "candidate_key": candidate["candidate_key"],
               "source_id": source["id"], "claim_id": claim["id"]}
    if verdict is not None:
        payload["verdict_id"] = verdict["id"]
    payload.update(extra)
    return payload


# =============================================================================
# 1. the Supabase boundary
# =============================================================================

@pytest.fixture
def repo():
    repository = SupabaseRepository.__new__(SupabaseRepository)
    repository.client = FakeClient()
    return repository


def supabase_payload_for(method):
    """A minimal VALID payload for one repository method."""
    snapshot = {"id": str(uuid4()), "snapshot_key": "cs1." + "0" * 32,
                "resource_id": "resource-1"}
    record = {"id": str(uuid4()), "snapshot_id": snapshot["id"],
              "record_key": "cr1." + "0" * 32}
    candidate = {"id": str(uuid4()), "candidate_key": "cc1." + "0" * 32}
    return {
        "record_catalog_snapshot": snapshot_payload(),
        "record_catalog_raw_record": record_payload(snapshot),
        "activate_catalog_snapshot": {"snapshot_id": snapshot["id"]},
        "record_catalog_candidate": candidate_payload(record),
        "link_catalog_candidate_evidence": link_payload(
            candidate, {"id": str(uuid4())}, {"id": str(uuid4())}),
    }[method]


@pytest.mark.parametrize("method, rpc, payload_name", CATALOG_WRITES)
def test_every_catalog_write_is_a_guarded_rpc_with_a_complete_lease(repo, method, rpc, payload_name):
    """No direct insert, and the lease travels with every call.

    A direct table insert would bypass the lease check, the referential
    provenance checks and the fail-closed replay conflict -- all of which live
    in the database so they hold for every caller.
    """
    getattr(repo, method)(uuid4(), supabase_payload_for(method),
                          worker_id="worker-1", attempt=3, lease_token="lease-1")
    name, params = repo.client.rpc_calls[-1]
    assert name == rpc
    assert (params["p_worker_id"], params["p_attempt"], params["p_lease_token"]) == ("worker-1", 3, "lease-1")
    assert repo.client.inserted == [] and repo.client.updated == []


@pytest.mark.parametrize("method, rpc, payload_name", CATALOG_WRITES)
def test_a_catalog_write_refuses_an_incomplete_lease_before_it_calls_anything(
        repo, method, rpc, payload_name):
    with pytest.raises(Exception, match="complete worker lease is required"):
        getattr(repo, method)(uuid4(), supabase_payload_for(method),
                              worker_id="", attempt=0, lease_token="")
    assert repo.client.rpc_calls == []


def test_the_repository_derives_every_identity_key_and_refuses_a_conflicting_one(repo):
    """Keys are structural, never caller-chosen and never model-authored.

    PostgreSQL enforces a key's domain and shape; the FULL derivation is this
    rule, and a caller that supplies a different key believes it is writing a
    different object, so it is refused rather than quietly replaced.
    """
    lease = {"worker_id": "w", "attempt": 1, "lease_token": "t"}
    repo.record_catalog_snapshot(uuid4(), snapshot_payload(), **lease)
    sent = repo.client.rpc_calls[-1][1]["p_snapshot"]
    assert sent["snapshot_key"].startswith("cs1.") and len(sent["snapshot_key"]) == 36
    # Deterministic: the same retrieval derives the same key every time.
    repo.record_catalog_snapshot(uuid4(), snapshot_payload(), **lease)
    assert repo.client.rpc_calls[-1][1]["p_snapshot"]["snapshot_key"] == sent["snapshot_key"]
    # A different retrieval is a different key.
    repo.record_catalog_snapshot(uuid4(), snapshot_payload(content="b" * 64), **lease)
    assert repo.client.rpc_calls[-1][1]["p_snapshot"]["snapshot_key"] != sent["snapshot_key"]

    for method, payload in (
            ("record_catalog_snapshot", {**snapshot_payload(), "snapshot_key": "a-name-i-chose"}),
            ("record_catalog_raw_record",
             {**record_payload({"id": "s", "snapshot_key": "cs1." + "0" * 32,
                                "resource_id": "r"}), "record_key": "cr1." + "f" * 32}),
            ("record_catalog_candidate",
             {**candidate_payload({"id": "r", "snapshot_id": "s", "record_key": "cr1." + "0" * 32}),
              "candidate_key": "cc1." + "f" * 32}),
            ("link_catalog_candidate_evidence",
             {**link_payload({"id": "c", "candidate_key": "cc1." + "0" * 32},
                             {"id": "src"}, {"id": "clm"}), "link_key": "cl1." + "f" * 32})):
        with pytest.raises(CatalogKeyError, match="does not match its trusted derivation"):
            getattr(repo, method)(uuid4(), payload, **lease)
    # None of the refused calls reached the database.
    assert all(call[0] != "record_catalog_raw_record_guarded" for call in repo.client.rpc_calls)


def test_the_repository_never_sends_a_payload_digest_or_a_parent_key(repo):
    """The digest is derived where the bytes are stored, and parent keys are
    derivation inputs rather than columns."""
    lease = {"worker_id": "w", "attempt": 1, "lease_token": "t"}
    snapshot = {"id": str(uuid4()), "snapshot_key": "cs1." + "0" * 32, "resource_id": "r"}
    repo.record_catalog_raw_record(uuid4(), record_payload(snapshot), **lease)
    sent = repo.client.rpc_calls[-1][1]["p_record"]
    assert "payload_sha256" not in sent and "snapshot_key" not in sent
    assert sent["record_key"].startswith("cr1.")
    with pytest.raises(CatalogPayloadError, match="digest is derived, not supplied"):
        repo.record_catalog_raw_record(
            uuid4(), {**record_payload(snapshot), "payload_sha256": "c" * 64}, **lease)


def test_the_rpc_name_is_a_literal_and_is_never_taken_from_the_payload(repo):
    """No dynamic function or table selection from model or user input."""
    lease = {"worker_id": "w", "attempt": 1, "lease_token": "t"}
    hostile = {"table": "catalog_models", "rpc": "drop_everything",
               "resource_id": "'; drop table public.runs; --"}
    for method, rpc, _ in CATALOG_WRITES:
        payload = {**supabase_payload_for(method), **hostile}
        if method == "record_catalog_raw_record":
            payload["resource_id"] = "'; drop table public.runs; --"
        try:
            getattr(repo, method)(uuid4(), payload, **lease)
        except (CatalogPayloadError, CatalogKeyError):
            continue
        assert repo.client.rpc_calls[-1][0] == rpc
    assert {call[0] for call in repo.client.rpc_calls} <= {rpc for _, rpc, _ in CATALOG_WRITES}


def test_the_repository_protocol_declares_the_catalog_methods_and_no_promotion():
    """This round adds persistence corrections only.

    There is no promote/publish/canonical method, because there is nothing for
    one to call: the canonical relations are read-only AND immutable in the
    database until PR3 adds field-level provenance.
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
# 2. the in-memory mirror: it must reject what PostgreSQL rejects
# =============================================================================

def leased_run(repository: MemoryRepository, key="catalog-key-1") -> tuple[UUID, dict]:
    """One run holding an active lease, through the real memory-repo path."""
    user = uuid4()
    repository.users.add(str(user))
    project = repository.create_project_from_proposal(
        uuid4(), f"catalog-{key}", "Catalog", None, {}, created_by=user)
    conversation = repository.create_conversation(UUID(project["id"]), "c", user_id=user)
    message = repository.create_user_message(UUID(conversation["id"]), "hello", {})
    run = repository.create_queued_run(UUID(conversation["id"]), message["id"], "hello", {},
                                       requested_by=user, idempotency_key=key)
    claimed = repository.claim_run(UUID(run["id"]), f"worker-{key}", lease_seconds=300)
    return UUID(run["id"]), {"worker_id": f"worker-{key}", "attempt": claimed["attempt"],
                             "lease_token": claimed["lease_token"]}


def real_evidence(repository: MemoryRepository, run_id: UUID, lease: dict, *,
                  label: str | None = None, verdict="verified", locator=F.LOCATOR,
                  kind=F.SOURCE_VERSION_KIND, version=F.SOURCE_VERSION_ID,
                  support=True, task=F.TASK_KEY, unit=F.FIELD_UNIT):
    """A REAL R3/R4 chain, built from the SHARED fixture both backends use.

    `backend/testing/evidence_fixtures.py` defines these payload shapes once,
    and `tests/test_migrations_postgres.py::test_the_shared_evidence_fixture_is_accepted_by_the_real_rpcs`
    submits the same builders through `upsert_source_guarded`,
    `record_evidence_fragment_guarded`, `create_claim_with_source_guarded` and
    `record_claim_verdict_guarded` against real PostgreSQL. So a chain that
    passes here is a chain the database accepts -- which the previous
    hand-built version was not: it had no `evidence_key`, no `task_key`, a
    locator with no `fragment_type`, no canonical scope identity, no unit on a
    numeric located fact, and neither verification mode nor contract version.
    """
    label = label or f"mem-{uuid4().hex[:10]}"
    source = repository.create_source(
        run_id, F.source_payload(f"{label}-source", task=task, version_kind=kind,
                                 version_id=version), **lease)
    fragment = repository.record_evidence_fragment(
        run_id, F.fragment_payload(f"{label}-fragment", source["id"], task=task,
                                   locator=locator,
                                   fragment_type=F.FRAGMENT_TYPE if locator else None),
        **lease)
    claim = repository.create_claim(
        run_id, F.claim_payload(f"{label}-claim", source["id"], task=task, unit=unit,
                                locator=locator), **lease)
    links = [F.support_link(fragment["id"], locator=locator)] if support else []
    verdict_row = repository.record_claim_verdict(
        run_id, F.verdict_payload(f"{label}-verdict", claim["id"], verdict=verdict,
                                  support=links), **lease)
    return source, fragment, claim, verdict_row


def catalog_chain(repository: MemoryRepository, run_id: UUID, lease, *,
                  family="government", declared=1):
    """A complete, active snapshot with one record and one candidate."""
    snapshot = repository.record_catalog_snapshot(
        run_id, snapshot_payload(family=family, declared=declared,
                                 content=catalog_payload_digest({"seed": family}),
                                 resource="model_technical_catalog_il"
                                 if family == "legacy_reference" else None), **lease)
    record = repository.record_catalog_raw_record(run_id, record_payload(snapshot), **lease)
    repository.activate_catalog_snapshot(run_id, {"snapshot_id": snapshot["id"]}, **lease)
    candidate = repository.record_catalog_candidate(run_id, candidate_payload(record), **lease)
    return snapshot, record, candidate


@pytest.fixture
def memory():
    repository = MemoryRepository()
    run_id, lease = leased_run(repository)
    return repository, run_id, lease


def test_the_canonical_catalog_starts_empty_and_no_write_path_fills_it(memory):
    """Every catalog write is exercised; the canonical lists stay empty."""
    repository, run_id, lease = memory
    assert repository.catalog_models == [] and repository.catalog_model_variants == []
    snapshot, record, candidate = catalog_chain(repository, run_id, lease)
    source, fragment, claim, verdict = real_evidence(repository, run_id, lease)
    repository.link_catalog_candidate_evidence(
        run_id, link_payload(candidate, source, claim, verdict=verdict), **lease)
    assert repository.catalog_models == [] and repository.catalog_model_variants == []
    assert len(repository.catalog_snapshots) == 1
    assert len(repository.catalog_raw_records) == 1
    assert len(repository.catalog_candidates) == 1
    assert len(repository.catalog_evidence_links) == 1


@pytest.mark.parametrize("build, expected", [
    # -> test_only_a_verified_verdict_may_back_a_catalog_evidence_link
    ("needs_review_verdict", "verdict is not verified"),
    ("rejected_verdict", "verdict is not verified"),
    # -> test_a_links_record_locator_must_be_the_claims_own_evidence_locator
    ("forged_locator", "record locator does not match the cited claim"),
    # -> test_a_links_source_version_must_be_the_cited_sources_own_version
    ("forged_version", "source version does not match the cited source"),
    ("forged_version_kind", "source version does not match the cited source"),
    # -> test_an_evidence_link_requires_a_real_claim_and_fails_closed_...
    ("unknown_claim", "invalid catalog evidence link claim"),
    ("unknown_verdict", "invalid catalog evidence link verdict"),
    ("unknown_source", "invalid catalog evidence link source"),
    ("claim_of_another_source", "claim source mismatch"),
    ("verdict_of_another_claim", "verdict claim mismatch"),
    ("claim_without_locator", "states no evidence locator"),
    ("source_without_version", "states no version"),
])
def test_the_memory_link_path_rejects_what_postgresql_rejects(memory, build, expected):
    """Parity, case by case. Each name above points at its PostgreSQL twin.

    Every one of these was ACCEPTED by the PR1 memory implementation, which is
    why a unit test could not be trusted to mean anything about the database.
    """
    repository, run_id, lease = memory
    snapshot, record, candidate = catalog_chain(repository, run_id, lease)
    source, fragment, claim, verdict = real_evidence(repository, run_id, lease)
    payload = link_payload(candidate, source, claim, verdict=verdict)

    if build == "needs_review_verdict":
        _, _, other_claim, other_verdict = real_evidence(repository, run_id, lease,
                                                          verdict="needs_review")
        payload = link_payload(candidate, source, claim, verdict=other_verdict)
        payload["verdict_id"] = other_verdict["id"]
        repository.tool_rows[-1]["claim_id"] = claim["id"]
    elif build == "rejected_verdict":
        _, _, _, other_verdict = real_evidence(repository, run_id, lease, verdict="rejected")
        repository.tool_rows[-1]["claim_id"] = claim["id"]
        payload = link_payload(candidate, source, claim, verdict=other_verdict)
    elif build == "forged_locator":
        payload["record_locator"] = '["document_span","doc-1",[],"Other",900,950]'
    elif build == "forged_version":
        payload["source_version"] = "2099.12.31"
    elif build == "forged_version_kind":
        payload["source_version_kind"] = "content_sha256"
    elif build == "unknown_claim":
        payload["claim_id"] = str(uuid4())
    elif build == "unknown_verdict":
        payload["verdict_id"] = str(uuid4())
    elif build == "unknown_source":
        payload["source_id"] = str(uuid4())
    elif build == "claim_of_another_source":
        _, _, other_claim, _ = real_evidence(repository, run_id, lease)
        payload = link_payload(candidate, source, other_claim)
    elif build == "verdict_of_another_claim":
        _, _, other_claim, other_verdict = real_evidence(repository, run_id, lease)
        payload = link_payload(candidate, source, claim, verdict=other_verdict)
    elif build == "claim_without_locator":
        _, _, bare_claim, _ = real_evidence(repository, run_id, lease, locator=None,
                                            verdict="needs_review")
        bare_source = next(row for row in repository.tool_rows
                           if row["id"] == bare_claim["source_id"])
        payload = link_payload(candidate, bare_source, bare_claim)
    elif build == "source_without_version":
        _, _, bare_claim, _ = real_evidence(repository, run_id, lease, kind=None,
                                            version=None)
        bare_source = next(row for row in repository.tool_rows
                           if row["id"] == bare_claim["source_id"])
        payload = link_payload(candidate, bare_source, bare_claim)

    with pytest.raises(AppError, match=expected):
        repository.link_catalog_candidate_evidence(run_id, payload, **lease)
    assert repository.catalog_evidence_links == {}


def test_a_memory_link_cannot_cite_another_runs_evidence(memory):
    """Cross-run: the cited rows must belong to the run holding the lease."""
    repository, run_id, lease = memory
    _, _, candidate = catalog_chain(repository, run_id, lease)
    other_run, other_lease = leased_run(repository, key="catalog-key-2")
    foreign_source, _, foreign_claim, foreign_verdict = real_evidence(
        repository, other_run, other_lease)
    with pytest.raises(AppError, match="invalid catalog evidence link source"):
        repository.link_catalog_candidate_evidence(
            run_id, link_payload(candidate, foreign_source, foreign_claim,
                                 verdict=foreign_verdict), **lease)
    assert repository.catalog_evidence_links == {}


def test_a_memory_link_derives_its_provenance_from_the_evidence(memory):
    """Stored locator and version come from the claim and the source."""
    repository, run_id, lease = memory
    _, _, candidate = catalog_chain(repository, run_id, lease)
    source, fragment, claim, verdict = real_evidence(repository, run_id, lease)
    link = repository.link_catalog_candidate_evidence(
        run_id, link_payload(candidate, source, claim, verdict=verdict), **lease)
    assert link["record_locator"] == claim["evidence_locator"] == LOCATOR
    assert (link["source_version_kind"], link["source_version"]) == (VERSION_KIND, VERSION_ID)
    # Replaying the identical citation collapses onto the same row...
    again = repository.link_catalog_candidate_evidence(
        run_id, link_payload(candidate, source, claim, verdict=verdict), **lease)
    assert again["id"] == link["id"] and len(repository.catalog_evidence_links) == 1
    # Renaming a citation is not reachable THROUGH the repository at all,
    # because the key is derived from the citation itself: a caller that
    # supplies a different name is refused before any row is consulted. The
    # database's own defence against a rename that got past a backend is the
    # natural unique index, proven in
    # `test_a_rename_cannot_duplicate_a_logical_catalog_identity`.
    with pytest.raises(CatalogKeyError, match="does not match its trusted derivation"):
        repository.link_catalog_candidate_evidence(
            run_id, {**link_payload(candidate, source, claim, verdict=verdict),
                     "link_key": "cl1." + "f" * 32}, **lease)


def test_identical_replay_is_idempotent_and_conflicting_replay_fails_closed(memory):
    repository, run_id, lease = memory
    snapshot = repository.record_catalog_snapshot(run_id, snapshot_payload(declared=2), **lease)
    assert repository.record_catalog_snapshot(
        run_id, snapshot_payload(declared=2), **lease)["id"] == snapshot["id"]
    assert len(repository.catalog_snapshots) == 1
    # Same structural identity, different declared completeness: the key is the
    # same, so this is a conflict rather than a second snapshot.
    with pytest.raises(AppError, match="snapshot idempotency conflict"):
        repository.record_catalog_snapshot(run_id, snapshot_payload(declared=3), **lease)
    # Storing the same retrieval twice under two names is not reachable
    # through the repository, because the key IS the retrieval: a supplied
    # name that disagrees is refused before any row is consulted. The
    # database's own defence, for a write that never went through a
    # repository, is the natural unique index -- proven in
    # `test_a_rename_cannot_duplicate_a_logical_catalog_identity`.
    with pytest.raises(CatalogKeyError, match="does not match its trusted derivation"):
        repository.record_catalog_snapshot(
            run_id, {**snapshot_payload(declared=2), "snapshot_key": "cs1." + "f" * 32}, **lease)

    record = repository.record_catalog_raw_record(run_id, record_payload(snapshot), **lease)
    assert repository.record_catalog_raw_record(
        run_id, record_payload(snapshot), **lease)["id"] == record["id"]
    assert repository.catalog_snapshots[snapshot["snapshot_key"]]["stored_record_count"] == 1
    # The stored digest is the derived one, and a changed payload under the
    # same upstream id fails closed.
    assert record["payload_sha256"] == catalog_payload_digest({"_id": 36327})
    with pytest.raises(AppError, match="record idempotency conflict"):
        repository.record_catalog_raw_record(
            run_id, record_payload(snapshot, body={"_id": 999}), **lease)


def test_a_snapshot_is_usable_only_after_complete_validation(memory):
    """The R5 pagination lesson: 1 of 2 records is not a complete capture."""
    repository, run_id, lease = memory
    snapshot = repository.record_catalog_snapshot(run_id, snapshot_payload(declared=2), **lease)
    repository.record_catalog_raw_record(run_id, record_payload(snapshot), **lease)
    with pytest.raises(AppError, match="catalog snapshot is incomplete"):
        repository.activate_catalog_snapshot(run_id, {"snapshot_id": snapshot["id"]}, **lease)
    assert repository.catalog_snapshots[snapshot["snapshot_key"]]["activated_at"] is None

    repository.record_catalog_raw_record(
        run_id, record_payload(snapshot, upstream="37392", body={"_id": 37392}), **lease)
    activated = repository.activate_catalog_snapshot(run_id, {"snapshot_id": snapshot["id"]}, **lease)
    assert activated["activated_at"] is not None and activated["validation_state"] == "complete"
    with pytest.raises(AppError, match="an active catalog snapshot is immutable"):
        repository.record_catalog_raw_record(
            run_id, record_payload(snapshot, upstream="99999"), **lease)


def test_a_failed_snapshot_is_terminal_in_memory_too(memory):
    """Parity with `test_a_failed_snapshot_is_terminal_and_accepts_no_further_record`.

    PR1's memory implementation gated only on `activated_at`, exactly as the
    database did, so a failed capture kept taking records here as well.
    """
    repository, run_id, lease = memory
    snapshot = repository.record_catalog_snapshot(run_id, snapshot_payload(declared=2), **lease)
    repository.record_catalog_raw_record(run_id, record_payload(snapshot), **lease)
    repository.activate_catalog_snapshot(
        run_id, {"snapshot_id": snapshot["id"], "validation_state": "failed"}, **lease)
    assert repository.catalog_snapshots[snapshot["snapshot_key"]]["validation_state"] == "failed"

    with pytest.raises(AppError, match="a failed catalog snapshot is terminal"):
        repository.record_catalog_raw_record(
            run_id, record_payload(snapshot, upstream="37392", body={"_id": 37392}), **lease)
    with pytest.raises(AppError, match="a failed catalog snapshot is terminal"):
        repository.activate_catalog_snapshot(run_id, {"snapshot_id": snapshot["id"]}, **lease)
    # Re-declaring the same failure is an idempotent no-op.
    repository.activate_catalog_snapshot(
        run_id, {"snapshot_id": snapshot["id"], "validation_state": "failed"}, **lease)
    assert repository.catalog_snapshots[snapshot["snapshot_key"]]["stored_record_count"] == 1


def test_a_snapshot_belongs_to_the_run_that_opened_it(memory):
    """Parity with `test_snapshot_ingestion_and_activation_require_the_creating_runs_lease`."""
    repository, run_id, lease = memory
    snapshot = repository.record_catalog_snapshot(run_id, snapshot_payload(), **lease)
    other_run, other_lease = leased_run(repository, key="catalog-key-3")
    for call in (lambda: repository.record_catalog_raw_record(
                     other_run, record_payload(snapshot), **other_lease),
                 lambda: repository.activate_catalog_snapshot(
                     other_run, {"snapshot_id": snapshot["id"]}, **other_lease)):
        with pytest.raises(AppError, match="does not belong to this run"):
            call()
    assert repository.catalog_snapshots[snapshot["snapshot_key"]]["stored_record_count"] == 0

    # A LATER run may still add evidence to an existing candidate -- the one
    # deliberate cross-run allowance, with evidence of its own.
    repository.record_catalog_raw_record(run_id, record_payload(snapshot), **lease)
    repository.activate_catalog_snapshot(run_id, {"snapshot_id": snapshot["id"]}, **lease)
    record = next(row for row in repository.catalog_raw_records.values())
    candidate = repository.record_catalog_candidate(run_id, candidate_payload(record), **lease)
    source, _, claim, verdict = real_evidence(repository, other_run, other_lease)
    link = repository.link_catalog_candidate_evidence(
        other_run, link_payload(candidate, source, claim, verdict=verdict), **other_lease)
    assert link["run_id"] == str(other_run)


def test_a_stale_worker_writes_nothing_on_any_catalog_path(memory):
    """Superseding the lease invalidates every catalog write at once."""
    repository, run_id, lease = memory
    snapshot = repository.record_catalog_snapshot(run_id, snapshot_payload(), **lease)
    before = (len(repository.catalog_snapshots), len(repository.catalog_raw_records))
    stale = [{**lease, "worker_id": "other-worker"},
             {**lease, "attempt": lease["attempt"] + 1},
             {**lease, "lease_token": "not-the-token"}]
    for bad in stale:
        for call in (lambda kw: repository.record_catalog_snapshot(run_id, snapshot_payload(content="e" * 64), **kw),
                     lambda kw: repository.record_catalog_raw_record(run_id, record_payload(snapshot), **kw),
                     lambda kw: repository.activate_catalog_snapshot(run_id, {"snapshot_id": snapshot["id"]}, **kw),
                     lambda kw: repository.record_catalog_candidate(run_id, candidate_payload({"id": "r", "snapshot_id": "s", "record_key": "cr1." + "0" * 32}), **kw),
                     lambda kw: repository.link_catalog_candidate_evidence(run_id, link_payload({"id": "c", "candidate_key": "cc1." + "0" * 32}, {"id": "s"}, {"id": "cl"}), **kw)):
            with pytest.raises(AppError, match="no longer (held|active)") as refusal:
                call(bad)
            assert refusal.value.code == "RUN_TRANSITION_CONFLICT"
    assert (len(repository.catalog_snapshots), len(repository.catalog_raw_records)) == before
    assert repository.catalog_snapshots[snapshot["snapshot_key"]]["activated_at"] is None


def test_cross_snapshot_linkage_is_rejected(memory):
    repository, run_id, lease = memory
    first = repository.record_catalog_snapshot(run_id, snapshot_payload(content="1" * 64), **lease)
    second = repository.record_catalog_snapshot(run_id, snapshot_payload(content="2" * 64), **lease)
    record = repository.record_catalog_raw_record(run_id, record_payload(first), **lease)
    # A candidate must be filed under the snapshot its record belongs to.
    with pytest.raises(AppError, match="candidate snapshot mismatch"):
        repository.record_catalog_candidate(
            run_id, {**candidate_payload(record), "snapshot_id": second["id"]}, **lease)
    # A record may not join a snapshot describing another resource.
    with pytest.raises(AppError, match="resource mismatch"):
        repository.record_catalog_raw_record(
            run_id, {**record_payload(second), "resource_id": "other-resource"}, **lease)


def test_a_candidate_may_stay_ambiguous_and_never_carries_a_guessed_identity(memory):
    repository, run_id, lease = memory
    snapshot = repository.record_catalog_snapshot(run_id, snapshot_payload(), **lease)
    record = repository.record_catalog_raw_record(run_id, record_payload(snapshot), **lease)
    ambiguous = repository.record_catalog_candidate(
        run_id, candidate_payload(record, status="ambiguous", trim=None), **lease)
    assert ambiguous["status"] == "ambiguous" and ambiguous["trim"] is None
    assert repository.record_catalog_candidate(
        run_id, candidate_payload(record, status="ambiguous", trim=None),
        **lease)["status"] == "ambiguous"
    # A later reading may revise the READING, never the identity: a changed
    # identity derives a different key and is therefore a different candidate.
    assert repository.record_catalog_candidate(
        run_id, candidate_payload(record, status="ready_for_review", trim=None),
        **lease)["status"] == "ready_for_review"
    assert repository.record_catalog_candidate(
        run_id, candidate_payload(record, trim="XSE"), **lease)["id"] != ambiguous["id"]

    for guess in ({"identity_dimensions": {"drivetrain": ""}},
                  {"identity_dimensions": {"drivetrain": " awd"}},
                  {"identity_dimensions": {"horsepower": "302"}}):
        with pytest.raises(ValueError):
            repository.record_catalog_candidate(
                run_id, candidate_payload(record, **guess), **lease)
    with pytest.raises(CatalogPayloadError, match="model year range must be whole"):
        repository.record_catalog_candidate(
            run_id, candidate_payload(record, model_year_end=None), **lease)
    with pytest.raises(AppError, match="invalid catalog candidate status"):
        repository.record_catalog_candidate(
            run_id, candidate_payload(record, status="verified"), **lease)


def test_the_legacy_catalog_is_unverified_and_can_never_carry_a_verdict(memory):
    """The old catalog suggests; it never confirms."""
    repository, run_id, lease = memory
    assert set(CATALOG_SOURCE_FAMILIES) == {"government", "manufacturer", "legacy_reference"}
    assert not is_evidence_family("legacy_reference")
    snapshot, record, candidate = catalog_chain(repository, run_id, lease,
                                                family="legacy_reference")
    assert snapshot["trust_state"] == "unverified"
    with pytest.raises(AppError, match="pinned to the source family"):
        repository.record_catalog_snapshot(
            run_id, snapshot_payload(family="legacy_reference", content="9" * 64,
                                     trust_state="evidence"), **lease)

    source, _, claim, verdict = real_evidence(repository, run_id, lease)
    # Discovery is fine...
    repository.link_catalog_candidate_evidence(
        run_id, link_payload(candidate, source, claim), **lease)
    # ...verification is not.
    with pytest.raises(AppError, match="unverified catalog source cannot carry a verdict"):
        repository.link_catalog_candidate_evidence(
            run_id, link_payload(candidate, source, claim, verdict=verdict), **lease)


def test_the_candidate_status_vocabulary_admits_ambiguity_as_a_real_answer():
    assert set(CANDIDATE_STATUSES) == {"candidate", "ambiguous", "rejected", "ready_for_review"}


# =============================================================================
# 3. the memory evidence writers themselves
# =============================================================================
#
# PR #86 added `record_evidence_fragment`, `record_claim_verdict` and
# `record_conflict_resolution` to close a parity gap -- a test could not build
# a real verdict to cite -- but the three accepted their lease arguments and
# ignored them, and the verdict writer accepted a `verified` verdict with no
# durable support at all. Both are what the guarded RPCs refuse, so a memory
# test could establish an evidence path PostgreSQL would never have created.
#
# The rules below mirror `record_evidence_fragment_guarded` and
# `record_claim_verdict_guarded` in
# `supabase/migrations/20260907000100_r4_deterministic_verification.sql`.

NEW_EVIDENCE_WRITERS = ("record_evidence_fragment", "record_claim_verdict",
                        "record_conflict_resolution")


def evidence_payload_for(method, claim_id="claim", fragment_id="fragment"):
    return {
        "record_evidence_fragment": F.fragment_payload("lease-probe-fragment", "s"),
        "record_claim_verdict": F.verdict_payload("lease-probe-verdict", claim_id,
                                                  verdict="needs_review", support=[]),
        "record_conflict_resolution": {"evidence_key": "res-1", "state": "resolved"},
    }[method]


@pytest.mark.parametrize("method", NEW_EVIDENCE_WRITERS)
def test_no_memory_evidence_writer_accepts_a_missing_lease(memory, method):
    """A lease is REQUIRED, not merely checked when offered.

    The previous round took `**lease` and passed whatever arrived into
    `_assert_active_lease`, which SKIPS whichever component is `None`. Handing
    it nothing therefore meant handing it no checks, and a leaseless write
    succeeded -- while `assert_worker_lease`'s four arguments are mandatory in
    every RPC signature.

    The keyword-only parameters make Python refuse the call, and
    `_evidence_lease` refuses a `None` reaching it by any other route, so the
    contract is pinned rather than left to argument binding.
    """
    repository, run_id, lease = memory
    before = len(repository.tool_rows)
    with pytest.raises(TypeError, match="required keyword-only argument"):
        getattr(repository, method)(run_id, evidence_payload_for(method))
    # ...and the check itself fails closed, independently of how it was called.
    with pytest.raises(AppError, match="requires a complete worker lease") as refusal:
        repository._evidence_lease(run_id, None, None, None)
    assert refusal.value.code == "RUN_TRANSITION_CONFLICT"
    assert len(repository.tool_rows) == before


@pytest.mark.parametrize("method", NEW_EVIDENCE_WRITERS)
@pytest.mark.parametrize("omit", ["worker_id", "attempt", "lease_token"])
def test_no_memory_evidence_writer_accepts_a_partial_lease(memory, method, omit):
    """Each required field, omitted individually, refuses before writing."""
    repository, run_id, lease = memory
    before = len(repository.tool_rows)
    partial = {name: value for name, value in lease.items() if name != omit}
    with pytest.raises(TypeError, match="required keyword-only argument"):
        getattr(repository, method)(run_id, evidence_payload_for(method), **partial)
    # And an explicit None for that field is refused by the check, not skipped.
    with pytest.raises(AppError, match="requires a complete worker lease"):
        getattr(repository, method)(run_id, evidence_payload_for(method),
                                    **{**lease, omit: None})
    assert len(repository.tool_rows) == before


@pytest.mark.parametrize("method", NEW_EVIDENCE_WRITERS)
@pytest.mark.parametrize("blank", ["", "   "])
def test_no_memory_evidence_writer_accepts_a_blank_lease_component(memory, method, blank):
    """An empty string is an absent value, not a lease."""
    repository, run_id, lease = memory
    before = len(repository.tool_rows)
    for field in ("worker_id", "lease_token"):
        with pytest.raises(AppError, match="requires a complete worker lease"):
            getattr(repository, method)(run_id, evidence_payload_for(method),
                                        **{**lease, field: blank})
    assert len(repository.tool_rows) == before


@pytest.mark.parametrize("method", NEW_EVIDENCE_WRITERS)
@pytest.mark.parametrize("break_lease", ["wrong_worker", "wrong_attempt", "wrong_token",
                                         "superseded", "expired"])
def test_no_memory_evidence_writer_accepts_a_broken_lease(memory, method, break_lease):
    """A wrong, superseded or expired lease writes NOTHING, on all three.

    Their PostgreSQL counterparts all call `assert_worker_lease` first, so a
    stale worker is rejected atomically at the database boundary. These three
    took the lease arguments and dropped them.
    """
    repository, run_id, lease = memory
    before = len(repository.tool_rows)
    bad = dict(lease)
    if break_lease == "wrong_worker":
        bad["worker_id"] = "a-different-worker"
    elif break_lease == "wrong_attempt":
        bad["attempt"] = lease["attempt"] + 1
    elif break_lease == "wrong_token":
        bad["lease_token"] = "not-the-token"
    elif break_lease == "superseded":
        # The lease expires and another worker claims the run: the old
        # worker's attempt and token are both superseded.
        repository.runs[str(run_id)]["lease_expires_at"] = "2000-01-01T00:00:00+00:00"
        repository.claim_run(run_id, "a-later-worker", lease_seconds=300)
    elif break_lease == "expired":
        repository.runs[str(run_id)]["lease_expires_at"] = "2000-01-01T00:00:00+00:00"

    with pytest.raises(AppError, match="no longer (held|active)|has expired") as refusal:
        getattr(repository, method)(run_id, evidence_payload_for(method), **bad)
    assert refusal.value.code == "RUN_TRANSITION_CONFLICT"
    assert len(repository.tool_rows) == before, "a refused evidence write must store nothing"
    assert repository.evidence_kinds.get(str(run_id)) is None


def test_a_verified_verdict_without_durable_support_is_rejected(memory):
    """`an accepted verdict must cite durable evidence`, in memory too.

    PR #86's own catalog fixture built exactly this shape -- a `verified`
    verdict with no support -- so every catalog test that cited a verdict was
    citing one `record_claim_verdict_guarded` refuses to create.
    """
    repository, run_id, lease = memory
    source, fragment, claim, _ = real_evidence(repository, run_id, lease)
    before = len(repository.tool_rows)
    with pytest.raises(AppError, match="an accepted verdict must cite durable evidence"):
        repository.record_claim_verdict(
            run_id, F.verdict_payload("unsupported", claim["id"], support=[]), **lease)
    assert len(repository.tool_rows) == before
    # A verdict that does NOT accept the claim may legitimately cite nothing.
    repository.record_claim_verdict(
        run_id, F.verdict_payload("needs-review", claim["id"], verdict="needs_review",
                                  support=[]), **lease)
    # And a verified verdict WITH its durable support is accepted.
    accepted = repository.record_claim_verdict(
        run_id, F.verdict_payload("supported", claim["id"],
                                  support=[F.support_link(fragment["id"])]), **lease)
    assert accepted["verdict"] == "verified"


@pytest.mark.parametrize("build, expected", [
    ("invented_fragment", "invalid support link"),
    ("cross_run_fragment", "invalid support link"),
    ("fragment_of_another_source", "belongs to another source"),
    ("wrong_content_hash", "content hash mismatch"),
    ("wrong_locator", "locator mismatch"),
    ("source_as_fragment", "invalid support link"),
])
def test_forged_or_mismatched_verdict_support_is_rejected(memory, build, expected):
    """Every way a support link could name something it is not."""
    repository, run_id, lease = memory
    source, fragment, claim, _ = real_evidence(repository, run_id, lease)
    link = F.support_link(fragment["id"])

    if build == "invented_fragment":
        link["fragment_id"] = str(uuid4())
    elif build == "cross_run_fragment":
        other_run, other_lease = leased_run(repository, key="catalog-key-support")
        _, foreign_fragment, _, _ = real_evidence(repository, other_run, other_lease)
        link["fragment_id"] = foreign_fragment["id"]
    elif build == "fragment_of_another_source":
        _, other_fragment, _, _ = real_evidence(repository, run_id, lease)
        link["fragment_id"] = other_fragment["id"]
    elif build == "wrong_content_hash":
        link["content_hash"] = "f" * 64
    elif build == "wrong_locator":
        link["locator_key"] = '["document_span","doc-1",[],"Other",900,950]'
    elif build == "source_as_fragment":
        link["fragment_id"] = source["id"]

    before = len(repository.tool_rows)
    with pytest.raises(AppError, match=expected):
        repository.record_claim_verdict(
            run_id, F.verdict_payload(f"forged-{build}", claim["id"], support=[link]),
            **lease)
    assert len(repository.tool_rows) == before


@pytest.mark.parametrize("field, wrong_kind, expected", [
    ("claim_id", "source", "invalid claim"),
    ("claim_id", "verdict", "invalid claim"),
])
def test_a_row_of_the_wrong_type_cannot_stand_in_for_a_claim(memory, field, wrong_kind,
                                                             expected):
    """Every memory evidence row shares `tool_rows`; only its TYPE separates them.

    PostgreSQL reads `public.sources`, `public.claims` and
    `public.claim_verdicts` as separate relations, so a source id simply cannot
    resolve as a claim there. The memory lookup checked only id and run.
    """
    repository, run_id, lease = memory
    source, fragment, claim, verdict = real_evidence(repository, run_id, lease)
    impostor = {"source": source, "verdict": verdict}[wrong_kind]
    with pytest.raises(AppError, match=expected):
        repository.record_claim_verdict(
            run_id, F.verdict_payload(f"impostor-{wrong_kind}", impostor["id"],
                                      verdict="needs_review", support=[]), **lease)


@pytest.mark.parametrize("field, impostor_kind, expected", [
    ("source_id", "claim", "invalid catalog evidence link source"),
    ("source_id", "verdict", "invalid catalog evidence link source"),
    ("claim_id", "source", "invalid catalog evidence link claim"),
    ("claim_id", "verdict", "invalid catalog evidence link claim"),
    ("verdict_id", "source", "invalid catalog evidence link verdict"),
    ("verdict_id", "claim", "invalid catalog evidence link verdict"),
])
def test_a_catalog_link_rejects_an_evidence_row_of_the_wrong_type(memory, field,
                                                                  impostor_kind, expected):
    """A source cannot masquerade as a claim, nor a claim as a verdict."""
    repository, run_id, lease = memory
    _, _, candidate = catalog_chain(repository, run_id, lease)
    source, fragment, claim, verdict = real_evidence(repository, run_id, lease)
    rows = {"source": source, "claim": claim, "verdict": verdict}
    payload = link_payload(candidate, source, claim, verdict=verdict)
    payload[field] = rows[impostor_kind]["id"]
    with pytest.raises(AppError, match=expected):
        repository.link_catalog_candidate_evidence(run_id, payload, **lease)
    assert repository.catalog_evidence_links == {}


# =============================================================================
# 4. the raw-record digest: storage-local, and order-independent within a backend
# =============================================================================

REORDERED_PAYLOADS = [
    ({"a": 1, "b": 2}, {"b": 2, "a": 1}),
    # Keys where bytewise order and PostgreSQL's (length, bytes) order DISAGREE,
    # which is the case that would break any claim of one canonical rendering.
    ({"b": 1, "aa": 2}, {"aa": 2, "b": 1}),
    ({"outer": {"z": 1, "a": 2}, "x": 3}, {"x": 3, "outer": {"a": 2, "z": 1}}),
]


@pytest.mark.parametrize("forward, reversed_", REORDERED_PAYLOADS)
def test_the_memory_digest_is_a_function_of_the_value_not_the_key_order(forward, reversed_):
    """Equal JSON is one value, at any depth, so it is one digest.

    PR #86's digest rendered a Python dict in INSERTION order, so
    `{"a":1,"b":2}` and `{"b":2,"a":1}` -- one value to every JSON reader and
    one `jsonb` to PostgreSQL -- received different digests.
    """
    assert forward == reversed_, "the two payloads must be the same value"
    assert catalog_payload_digest(forward) == catalog_payload_digest(reversed_)
    assert catalog_payload_digest(forward) != catalog_payload_digest({**forward, "zz": 9})


@pytest.mark.parametrize("forward, reversed_", REORDERED_PAYLOADS)
def test_a_reordered_payload_replays_onto_the_same_memory_record(memory, forward, reversed_):
    """The behaviour replay depends on, stated on the repository."""
    repository, run_id, lease = memory
    snapshot = repository.record_catalog_snapshot(run_id, snapshot_payload(declared=1), **lease)
    first = repository.record_catalog_raw_record(
        run_id, record_payload(snapshot, body=forward), **lease)
    again = repository.record_catalog_raw_record(
        run_id, record_payload(snapshot, body=reversed_), **lease)
    assert again["id"] == first["id"]
    assert again["payload_sha256"] == first["payload_sha256"]
    assert repository.catalog_snapshots[snapshot["snapshot_key"]]["stored_record_count"] == 1
    # A genuinely different payload under the same upstream id still conflicts.
    with pytest.raises(AppError, match="record idempotency conflict"):
        repository.record_catalog_raw_record(
            run_id, record_payload(snapshot, body={**forward, "zz": 9}), **lease)


def test_the_memory_digest_is_storage_local_and_says_so():
    """It is NOT PostgreSQL's digest, and nothing claims it is.

    The compact separators make the difference structural rather than
    accidental: PostgreSQL renders `{"a": 1, "b": 2}`, this renders
    `{"a":1,"b":2}`, so the two digests differ for every non-empty object.
    `tests/test_migrations_postgres.py::test_the_raw_record_digest_is_storage_local_…`
    asserts the same inequality from the database side.
    """
    payload = {"a": 1, "b": 2}
    assert canonical_payload_text(payload) == '{"a":1,"b":2}'
    postgres_rendering = '{"a": 1, "b": 2}'
    assert canonical_payload_text(payload) != postgres_rendering
    assert catalog_payload_digest(payload) != hashlib.sha256(
        postgres_rendering.encode("utf-8")).hexdigest()


# =============================================================================
# 4. the verdict REPLAY contract
# =============================================================================
#
# Round 3 gave the three durable evidence writers a real lease and gave the
# fixture a chain PostgreSQL accepts. It left one parity gap: the memory
# verdict replay identity was `(claim_id, verdict, verification_mode,
# verifier_contract_version)` -- it compared neither `reason` nor the durable
# support set, and it had no `deterministic_local` rule at all.
#
# `record_claim_verdict_guarded` in
# `supabase/migrations/20260907000100_r4_deterministic_verification.sql`, as
# corrected by `20260915120000_catalog_integrity_corrections.sql`, is the
# contract. Every case below has a counterpart in
# `tests/test_migrations_postgres.py::test_the_verdict_replay_contract_is_the_databases_own`
# that submits the SAME shared-fixture payloads to the real RPC, so neither
# side can drift alone.


def verdict_scenario(repository, run_id, lease, label):
    """A claim with TWO durable fragments of its own source, ready to cite."""
    source = repository.create_source(run_id, F.source_payload(f"{label}-source"), **lease)
    fragment_a = repository.record_evidence_fragment(
        run_id, F.fragment_payload(f"{label}-fragA", source["id"]), **lease)
    text_b = F.FRAGMENT_TEXT + " (a second bounded excerpt)"
    fragment_b = repository.record_evidence_fragment(
        run_id, F.fragment_payload(f"{label}-fragB", source["id"], text=text_b, index=1),
        **lease)
    claim = repository.create_claim(run_id, F.claim_payload(f"{label}-claim", source["id"]),
                                    **lease)
    return (claim, F.support_link(fragment_a["id"]),
            F.support_link(fragment_b["id"], text=text_b))


def stored_verdict(repository, run_id, key):
    """The one stored verdict row for this replay key."""
    return next(row for row in repository.tool_rows
                if str(row.get("run_id")) == str(run_id)
                and row.get("evidence_key") == key
                and repository.evidence_kinds.get(str(row.get("id"))) == "claim_verdict")


def support_set(row):
    """The durable support of a stored row, as the SET PostgreSQL stores."""
    return {(str(link.get("fragment_id")), link.get("content_hash"),
             link.get("locator_key")) for link in row.get("support") or []}


def assert_unchanged(repository, run_id, key, before):
    """A refused replay writes nothing: same row, same identity, same support."""
    after = stored_verdict(repository, run_id, key)
    assert after["id"] == before["id"]
    assert support_set(after) == support_set(before)
    for field in ("claim_id", "verdict", "reason", "verification_mode",
                  "verifier_contract_version"):
        assert after.get(field) == before.get(field)
    assert len([row for row in repository.tool_rows
                if repository.evidence_kinds.get(str(row.get("id"))) == "claim_verdict"
                and row.get("evidence_key") == key]) == 1


def test_a_replayed_verdict_may_not_change_its_reason(memory):
    """`reason` is part of the verdict, so it is part of the replay identity.

    PostgreSQL compares it alongside claim, verdict, mode and contract; the
    memory identity tuple omitted it, so the SAME evidence key could be
    replayed with a different stated reason and silently return the old row.
    """
    repository, run_id, lease = memory
    claim, link_a, _ = verdict_scenario(repository, run_id, lease, "reason")
    repository.record_claim_verdict(
        run_id, F.verdict_payload("v-reason", claim["id"], support=[link_a]), **lease)
    before = stored_verdict(repository, run_id, "v-reason")

    with pytest.raises(AppError, match="claim verdict idempotency conflict"):
        repository.record_claim_verdict(
            run_id, F.verdict_payload("v-reason", claim["id"], support=[link_a],
                                      reason="A COMPLETELY DIFFERENT REASON"), **lease)
    assert_unchanged(repository, run_id, "v-reason", before)


def test_a_replayed_verdict_may_not_replace_its_support(memory):
    """Swapping one durable fragment for another is a different verdict."""
    repository, run_id, lease = memory
    claim, link_a, link_b = verdict_scenario(repository, run_id, lease, "replace")
    repository.record_claim_verdict(
        run_id, F.verdict_payload("v-replace", claim["id"], support=[link_a]), **lease)
    before = stored_verdict(repository, run_id, "v-replace")

    with pytest.raises(AppError,
                       match="verdict support links do not match the cited evidence"):
        repository.record_claim_verdict(
            run_id, F.verdict_payload("v-replace", claim["id"], support=[link_b]), **lease)
    assert_unchanged(repository, run_id, "v-replace", before)


def test_a_replayed_verdict_may_not_add_support(memory):
    """A replay that cites MORE evidence is not the verdict that was stored.

    This is the case PostgreSQL itself got wrong before
    `20260915120000_catalog_integrity_corrections.sql`: it inserted the added
    link, then compared `count(*)` of the UNION against the cited length. A
    cited superset made those equal, so the addition was accepted AND the
    stored support set was silently grown.
    """
    repository, run_id, lease = memory
    claim, link_a, link_b = verdict_scenario(repository, run_id, lease, "add")
    repository.record_claim_verdict(
        run_id, F.verdict_payload("v-add", claim["id"], support=[link_a]), **lease)
    before = stored_verdict(repository, run_id, "v-add")
    assert support_set(before) == {(link_a["fragment_id"], link_a["content_hash"],
                                    link_a["locator_key"])}

    with pytest.raises(AppError,
                       match="verdict support links do not match the cited evidence"):
        repository.record_claim_verdict(
            run_id, F.verdict_payload("v-add", claim["id"], support=[link_a, link_b]),
            **lease)
    assert_unchanged(repository, run_id, "v-add", before)


@pytest.mark.parametrize("keep", [1, 0])
def test_a_replayed_verdict_may_not_remove_support(memory, keep):
    """Dropping evidence is a contract failure, down to citing none at all."""
    repository, run_id, lease = memory
    claim, link_a, link_b = verdict_scenario(repository, run_id, lease, f"drop{keep}")
    key = f"v-drop-{keep}"
    repository.record_claim_verdict(
        run_id, F.verdict_payload(key, claim["id"], verdict="needs_review",
                                  support=[link_a, link_b]), **lease)
    before = stored_verdict(repository, run_id, key)

    with pytest.raises(AppError,
                       match="verdict support links do not match the cited evidence"):
        repository.record_claim_verdict(
            run_id, F.verdict_payload(key, claim["id"], verdict="needs_review",
                                      support=[link_a][:keep]), **lease)
    assert_unchanged(repository, run_id, key, before)


def test_a_verdict_may_not_cite_the_same_fragment_twice(memory):
    """Support is a SET: a list of two identical links cites ONE fragment.

    PostgreSQL stores support as rows unique on `(verdict_id, fragment_id)`,
    so a duplicated citation can never become two stored links and the count
    can never match. This holds on the FIRST write, not only on a replay.
    """
    repository, run_id, lease = memory
    claim, link_a, _ = verdict_scenario(repository, run_id, lease, "dup")

    with pytest.raises(AppError,
                       match="verdict support links do not match the cited evidence"):
        repository.record_claim_verdict(
            run_id, F.verdict_payload("v-dup-first", claim["id"],
                                      support=[link_a, dict(link_a)]), **lease)
    assert not [row for row in repository.tool_rows
                if row.get("evidence_key") == "v-dup-first"]

    repository.record_claim_verdict(
        run_id, F.verdict_payload("v-dup", claim["id"], support=[link_a]), **lease)
    before = stored_verdict(repository, run_id, "v-dup")
    with pytest.raises(AppError,
                       match="verdict support links do not match the cited evidence"):
        repository.record_claim_verdict(
            run_id, F.verdict_payload("v-dup", claim["id"],
                                      support=[link_a, dict(link_a)]), **lease)
    assert_unchanged(repository, run_id, "v-dup", before)


def test_a_replayed_verdict_accepts_the_same_support_in_any_order(memory):
    """Input order is not identity -- PostgreSQL stores relational rows.

    The stored row is returned unchanged, and the stored support set is still
    the same set. This is the case that must NOT be broken by making the other
    seven fail closed.
    """
    repository, run_id, lease = memory
    claim, link_a, link_b = verdict_scenario(repository, run_id, lease, "order")
    first = repository.record_claim_verdict(
        run_id, F.verdict_payload("v-order", claim["id"], support=[link_a, link_b]),
        **lease)
    before = stored_verdict(repository, run_id, "v-order")

    replay = repository.record_claim_verdict(
        run_id, F.verdict_payload("v-order", claim["id"], support=[link_b, link_a]),
        **lease)
    assert replay["id"] == first["id"]
    assert_unchanged(repository, run_id, "v-order", before)


def test_a_locally_settled_verdict_cites_no_evidence(memory):
    """`deterministic_local` compares no evidence, so it may cite none.

    Memory accepted support on a local verdict; PostgreSQL refuses it.
    """
    repository, run_id, lease = memory
    claim, link_a, _ = verdict_scenario(repository, run_id, lease, "local")

    with pytest.raises(AppError, match="a locally settled verdict cites no evidence"):
        repository.record_claim_verdict(
            run_id, F.verdict_payload("v-local", claim["id"], verdict="needs_review",
                                      mode="deterministic_local", support=[link_a]),
            **lease)
    assert not [row for row in repository.tool_rows
                if row.get("evidence_key") == "v-local"]

    # The legitimate local shape still writes.
    settled = repository.record_claim_verdict(
        run_id, F.verdict_payload("v-local-ok", claim["id"], verdict="needs_review",
                                  mode="deterministic_local", support=[]), **lease)
    assert support_set(stored_verdict(repository, run_id, "v-local-ok")) == set()
    assert settled["verification_mode"] == "deterministic_local"


@pytest.mark.parametrize("support_given", [True, False])
def test_a_verified_verdict_can_never_be_locally_settled(memory, support_given):
    """The two rules meet: local permits no support, verified requires it.

    So `verified` + `deterministic_local` is unreachable from either side --
    with support the local rule refuses it, without support the accepted-
    verdict rule does. Both messages are PostgreSQL's own.
    """
    repository, run_id, lease = memory
    claim, link_a, _ = verdict_scenario(repository, run_id, lease, f"vl{support_given:d}")
    expected = ("a locally settled verdict cites no evidence" if support_given
                else "an accepted verdict must cite durable evidence")

    with pytest.raises(AppError, match=expected):
        repository.record_claim_verdict(
            run_id, F.verdict_payload("v-verified-local", claim["id"], verdict="verified",
                                      mode="deterministic_local",
                                      support=[link_a] if support_given else []), **lease)
    assert not [row for row in repository.tool_rows
                if row.get("evidence_key") == "v-verified-local"]


# -----------------------------------------------------------------------------
# `support` must be a JSON ARRAY when it is supplied at all.
# -----------------------------------------------------------------------------
#
# `support = verdict.get("support") or []` collapsed every FALSY value -- null,
# {}, "", 0, false -- into an empty list, so the `isinstance(support, list)`
# check immediately below it could never fire, and the junk value was stored
# verbatim in the row. Observed at `2b6c77f`: all five accepted, storing
# `support=None`, `{}`, `''`, `0` and `False`.
#
# PostgreSQL separates an ABSENT key from a supplied one -- see
# `evidence_fixtures.SUPPORT_VALUE_CASES` for the mechanism -- and the matrix
# there is run against the real RPC by
# `tests/test_migrations_postgres.py::test_the_verdict_support_field_must_be_a_json_array`.
#
# `needs_review` is used throughout so the "an accepted verdict must cite
# durable evidence" rule cannot mask the type check.


@pytest.mark.parametrize("label, value, accepted", F.SUPPORT_VALUE_CASES)
def test_the_memory_verdict_holds_support_to_a_json_array(memory, label, value, accepted):
    """A missing `support` means none; a supplied one must be an array."""
    repository, run_id, lease = memory
    claim, _, _ = verdict_scenario(repository, run_id, lease, f"type-{label}")
    key = f"v-type-{label}"
    payload = F.verdict_payload_with_support(key, claim["id"], value)

    if accepted:
        row = repository.record_claim_verdict(run_id, payload, **lease)
        assert support_set(stored_verdict(repository, run_id, key)) == set()
        assert row["verdict"] == "needs_review"
        return

    with pytest.raises(AppError, match=F.SUPPORT_TYPE_ERROR):
        repository.record_claim_verdict(run_id, payload, **lease)
    # A refused FIRST write leaves no verdict row and no support state at all.
    assert not [row for row in repository.tool_rows if row.get("evidence_key") == key]
    assert not [row_id for row_id, kind in repository.evidence_kinds.items()
                if kind == "claim_verdict"
                and any(r.get("id") == row_id and r.get("evidence_key") == key
                        for r in repository.tool_rows)]


@pytest.mark.parametrize("label, value", F.REJECTED_SUPPORT_CASES)
def test_a_memory_verdict_replay_holds_support_to_a_json_array(memory, label, value):
    """A malformed `support` on a REPLAY changes nothing that is stored."""
    repository, run_id, lease = memory
    claim, link_a, _ = verdict_scenario(repository, run_id, lease, f"rtype-{label}")
    key = f"v-rtype-{label}"
    repository.record_claim_verdict(
        run_id, F.verdict_payload(key, claim["id"], verdict="needs_review",
                                  support=[link_a]), **lease)
    before = stored_verdict(repository, run_id, key)
    assert support_set(before) == {(link_a["fragment_id"], link_a["content_hash"],
                                    link_a["locator_key"])}

    with pytest.raises(AppError, match=F.SUPPORT_TYPE_ERROR):
        repository.record_claim_verdict(
            run_id, F.verdict_payload_with_support(key, claim["id"], value), **lease)
    assert_unchanged(repository, run_id, key, before)


# =============================================================================
# 6. the durable catalog READS (Catalog PR2)
# =============================================================================
#
# Reads take no lease -- a lease authorizes a durable WRITE, and requiring one
# to look at already-durable catalog state would make a query layer impossible
# to build without holding a run open. What each read must still be is bounded,
# deterministically ordered and restricted to material that may answer a query,
# and that is a property of the query SHAPE rather than of any returned row.
#
# These tests also exist because an earlier revision of this branch defined the
# three concrete readers on the `Repository` PROTOCOL instead of on
# `SupabaseRepository`, where they would never have run in production. Asserting
# that each one actually reaches its table is what makes that a test failure.

CATALOG_READS = (
    ("list_active_catalog_snapshots", ("government",), "catalog_source_snapshots"),
    ("find_active_catalog_snapshot", ("government", "142afde2", "cs1." + "0" * 32),
     "catalog_source_snapshots"),
    ("list_catalog_raw_records", (str(uuid4()),), "catalog_raw_records"),
    ("list_catalog_candidates", (str(uuid4()),), "catalog_candidate_variants"),
)


@pytest.mark.parametrize("method,args,table", CATALOG_READS,
                         ids=[case[0] for case in CATALOG_READS])
def test_the_catalog_reads_are_implemented_on_the_repository_itself(repo, method, args, table):
    """Declared on the Protocol AND implemented on the class that talks to the
    database -- and the implementation is the one that runs."""
    assert getattr(Repository, method, None) is not None
    assert getattr(SupabaseRepository, method) is not getattr(Repository, method)
    assert getattr(repo, method)(*args) in ([], None)
    read = repo.client.selected[-1]
    assert read.table == table
    # Never `*`: a column added later joins a read only when a reviewer adds it.
    assert read.columns and "*" not in read.columns
    assert repo.client.rpc_calls == [] and repo.client.inserted == []


def test_only_active_snapshots_can_answer_a_catalog_query(repo):
    """`activated_at is not null` is a column predicate, not a convention.

    A pending capture is still being appended to and a failed one is the record
    that a capture was unusable, so neither may answer a query at all.
    """
    repo.list_active_catalog_snapshots("government", resource_id="142afde2")
    read = repo.client.selected[-1]
    assert ("not.is", "activated_at", "null") in read.filters
    assert ("eq", "source_family", "government") in read.filters
    assert ("eq", "resource_id", "142afde2") in read.filters
    # Newest activation first, broken by a key that is unique per row.
    assert read.orders == [("activated_at", True), ("snapshot_key", False)]
    assert read.bounds == ("limit", SupabaseRepository.MAX_CATALOG_SNAPSHOT_ROWS)


def test_catalog_reads_order_by_a_collation_free_key_and_are_bounded(repo):
    """The Government identity text is Hebrew, so ordering by it in the
    database would make "the same snapshot produces the same ordered tree"
    depend on the cluster's collation. These order by ASCII keys that are
    unique within their scope; semantic ordering happens above this layer."""
    snapshot = str(uuid4())
    repo.list_catalog_raw_records(snapshot, limit=10, offset=20)
    read = repo.client.selected[-1]
    assert read.orders == [("record_key", False)]
    assert read.bounds == ("range", 20, 29)
    assert ("eq", "snapshot_id", snapshot) in read.filters

    repo.list_catalog_candidates(snapshot, limit=10, offset=0)
    read = repo.client.selected[-1]
    assert read.orders == [("candidate_key", False)]
    assert read.bounds == ("range", 0, 9)


@pytest.mark.parametrize("method,ceiling", [
    ("list_catalog_raw_records", SupabaseRepository.MAX_CATALOG_RECORD_ROWS),
    ("list_catalog_candidates", SupabaseRepository.MAX_CATALOG_CANDIDATE_ROWS),
])
def test_a_caller_cannot_widen_a_catalog_read_past_the_server_bound(repo, method, ceiling):
    getattr(repo, method)(str(uuid4()), limit=10_000, offset=-5)
    read = repo.client.selected[-1]
    assert read.bounds == ("range", 0, ceiling - 1)


def test_the_two_repositories_agree_about_what_a_catalog_read_returns():
    """Bounds and ordering are the same rule in both implementations, so a
    test written against the memory repository is a test of the same contract."""
    memory = MemoryRepository()
    for attribute in ("MAX_CATALOG_SNAPSHOT_ROWS", "MAX_CATALOG_RECORD_ROWS",
                      "MAX_CATALOG_CANDIDATE_ROWS"):
        assert getattr(memory, attribute) == getattr(SupabaseRepository, attribute)
    # And an empty catalog answers nothing rather than raising.
    assert memory.list_active_catalog_snapshots("government") == []
    assert memory.list_catalog_raw_records(str(uuid4())) == []
    assert memory.list_catalog_candidates(str(uuid4())) == []


def test_pinning_a_snapshot_is_an_exact_lookup_not_a_bounded_search(repo):
    """Resolving an explicit `snapshot_key` must not go through the listing.

    The listing is bounded to the NEWEST snapshots, so searching it for a key
    made every active snapshot older than the bound unreachable. This is an
    equality lookup on all four properties, capped at one row, so its cost does
    not grow with the catalog.
    """
    assert repo.find_active_catalog_snapshot("government", "142afde2",
                                             "cs1." + "0" * 32) is None
    read = repo.client.selected[-1]
    assert read.table == "catalog_source_snapshots"
    assert read.columns and "*" not in read.columns
    assert set(read.filters) == {
        ("eq", "source_family", "government"),
        ("eq", "resource_id", "142afde2"),
        ("eq", "snapshot_key", "cs1." + "0" * 32),
        ("not.is", "activated_at", "null"),
    }
    assert read.bounds == ("limit", 1)
    # An exact lookup needs no ordering: at most one row can match.
    assert read.orders == []


def test_the_memory_repository_orders_active_snapshots_exactly_as_supabase_does():
    """`activated_at` DESC, then `snapshot_key` ASC -- the SAME tiebreak.

    Sorting the whole tuple in reverse reverses the tiebreak too, so snapshots
    activated in one instant came back in the opposite order from the
    database's. Every row below shares an activation instant, so the order is
    decided entirely by the tiebreak.
    """
    memory = MemoryRepository()
    stamp = "2026-09-15T12:00:00+00:00"
    for index, key in enumerate(("cs1." + "c" * 32, "cs1." + "a" * 32, "cs1." + "b" * 32)):
        memory.catalog_snapshots[key] = {
            "id": str(uuid4()), "snapshot_key": key, "source_family": "government",
            "resource_id": "142afde2", "activated_at": stamp,
            "validation_state": "complete", "retrieval_metadata": {},
        }
    memory.catalog_snapshots["cs1." + "d" * 32] = {
        "id": str(uuid4()), "snapshot_key": "cs1." + "d" * 32, "source_family": "government",
        "resource_id": "142afde2", "activated_at": "2026-09-16T12:00:00+00:00",
        "validation_state": "complete", "retrieval_metadata": {},
    }
    listed = [row["snapshot_key"]
              for row in memory.list_active_catalog_snapshots("government")]
    assert listed == ["cs1." + "d" * 32, "cs1." + "a" * 32, "cs1." + "b" * 32,
                      "cs1." + "c" * 32]

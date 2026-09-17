"""CODE-3: the bounded, membership-authorized, READ-ONLY catalog review surface.

What this proves
----------------

Two GET routes exist, they answer only for a member, they are bounded, they
project through closed allowlists, and **they cannot write**. The last one is
the property everything else rests on, so it is not asserted by reading the
handler: every request in this module runs through `RecordingRepository`, which
records the name of every repository method the request reached. A write would
have to appear in that list.

Offline and deterministic. Every durable row comes from the committed R5
capture fixtures landed through the production ingestion path, an autouse
fixture makes creating a socket an error, and no model is called anywhere.

`tests/test_migrations_postgres.py` carries the half that depends on real SQL:
that `catalog_candidate_variant_page` filters `ready_for_review` in the
database, that its `total_count` is exact past the end of a page, and that
`catalog_canonical_variant_current` answers the bounded canonical listing. The
in-memory mirror is checked against the same expectations here, but it is the
mirror -- the database is the contract.
"""

from __future__ import annotations

import socket
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.catalog import review as catalog_review
from backend.catalog.contracts import CANDIDATE_STATUSES
from backend.catalog.execution import CATALOG_EXECUTION_FLAG
from backend.catalog.government import source as src
from backend.catalog.government.normalize import NORMALIZATION_CONTRACT, RAW_ONLY_CONTRACT
from backend.catalog.government.projection import MAX_RESULT_ITEMS
from backend.dependencies import get_job_launcher, get_repository
from backend.execution_guard import SURFACE_RULES
from backend.main import app
from backend.schemas import CanonicalCatalogItem, CatalogReviewCandidateItem, CatalogReviewSnapshot
from backend.testing.memory_repository import MemoryRepository
from tests.test_catalog_pr3_swarm_promotion import (ingest, lease_kwargs, leased_run,
                                                    production_path)

CANONICAL_PATH = "/projects/{project_id}/catalog/canonical"
REVIEW_PATH = "/projects/{project_id}/catalog/review-candidates"
CODE3_PATHS = (CANONICAL_PATH, REVIEW_PATH)

#: Every durable catalog WRITE, plus the run/lease operations a read must never
#: reach. Named explicitly so a method added later is not silently exempt.
FORBIDDEN_REPOSITORY_METHODS = (
    "record_catalog_snapshot", "record_catalog_raw_record", "record_catalog_candidate",
    "activate_catalog_snapshot", "link_catalog_candidate_evidence",
    "promote_catalog_variant", "record_conflict_resolution",
    "claim_run", "heartbeat_run", "create_queued_run", "create_message_and_run",
    "create_user_message", "transition_run", "set_launch_state", "try_acquire_launch",
    "append_run_event", "upsert_run_blackboard", "create_agent_message",
)


# =============================================================================
# 0. the module is offline by construction
# =============================================================================

@pytest.fixture(autouse=True)
def no_outbound_connections(monkeypatch):
    """CONNECTING a socket anywhere in this module is a test failure.

    Narrower than the flat "creating a socket is an error" guard the offline
    catalog modules use, and deliberately so: these tests drive the real app
    through `TestClient`, whose blocking portal builds an event-loop self-pipe
    out of a local socket pair. Refusing construction would fail the harness
    rather than the code under test.

    Blocking `connect` and `create_connection` is what actually matters -- they
    are the two paths `requests`, and therefore `HttpsDataGovTransport`, reach
    egress through. A local socket pair never calls either.
    """
    def refuse(*_args, **_kwargs):
        raise AssertionError("a catalog review test attempted a network connection")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


@pytest.fixture(autouse=True)
def catalog_execution_disabled(monkeypatch):
    """The DEFAULT posture for this whole module.

    Every test here runs with the catalog execution path OFF, because that is
    the posture an operator is in during a rollback -- and the review surface
    must answer anyway. A test that needs it on turns it on itself.
    """
    monkeypatch.delenv(CATALOG_EXECUTION_FLAG, raising=False)


# =============================================================================
# helpers
# =============================================================================

class RecordingRepository:
    """Every repository method a request reached, in order.

    Delegation is by `__getattr__`, so a method added to the repository is
    recorded without an edit here -- the wrapper cannot fall behind the object
    it wraps and quietly stop watching a new write.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._inner, name)
        if not callable(attribute):
            return attribute

        def recorded(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(name)
            return attribute(*args, **kwargs)

        return recorded

    @property
    def writes(self) -> list[str]:
        return [name for name in self.calls if name in FORBIDDEN_REPOSITORY_METHODS]


class FailingLauncher:
    """A launcher whose use is a test failure. A read never launches anything."""

    def launch(self, *_args: Any, **_kwargs: Any) -> Any:  # pragma: no cover - must not run
        raise AssertionError("a catalog review read reached the job launcher")


def durable_catalog() -> MemoryRepository:
    """A repository holding the committed R5 capture, promoted once.

    Runs the PRODUCTION orchestration -- the engine half and then
    `CatalogPromotionPipeline.promote` -- so the durable state this surface
    reads is state the product actually produces, not state a test assembled.
    """
    repository = MemoryRepository()
    lease = leased_run(repository)
    ingest(repository, lease)
    production_path(repository, lease)
    return repository


def mark_every_candidate_ready(repository: MemoryRepository) -> int:
    """Move the whole pinned capture to `ready_for_review`, through the RPC.

    233 candidates, which is comfortably past `MAX_REVIEW_PAGE_ITEMS`, so the
    bounding proofs run against a real snapshot rather than a hand-made one.
    """
    lease = leased_run(repository, worker="worker-review")
    records = {row["id"]: row for row in repository.catalog_raw_records.values()}
    snapshots = {row["id"]: row for row in repository.catalog_snapshots.values()}
    for candidate in list(repository.catalog_candidates.values()):
        record = records[candidate["raw_record_id"]]
        repository.record_catalog_candidate(lease.run_id, {
            "snapshot_key": snapshots[candidate["snapshot_id"]]["snapshot_key"],
            "record_key": record["record_key"],
            "snapshot_id": candidate["snapshot_id"],
            "raw_record_id": candidate["raw_record_id"],
            "manufacturer": candidate["manufacturer"],
            "commercial_model": candidate["commercial_model"],
            "model_year_start": candidate["model_year_start"],
            "model_year_end": candidate["model_year_end"],
            "official_model_code": candidate["official_model_code"],
            "trim": candidate["trim"],
            "identity_dimensions": candidate["identity_dimensions"],
            "status": "ready_for_review"}, **lease_kwargs(lease))
    return sum(1 for row in repository.catalog_candidates.values()
               if row["status"] == "ready_for_review")


def seed_canonical_variants(repository: MemoryRepository, count: int) -> None:
    """`count` extra canonical variants, in the shape a promotion writes.

    Deliberately synthesized rather than promoted: a real promotion needs a
    verified evidence chain per variant, and this proof is about the READ
    bound, not about promotion. The rows carry exactly the columns
    `promote_catalog_variant` writes, so the listing under test reads the same
    shape it reads in production.
    """
    model_id = str(uuid4())
    repository.catalog_models.append({
        "id": model_id, "canonical_key": "cm1." + "0" * 32,
        "manufacturer": "SEEDED", "commercial_model": "BOUNDED",
        "created_at": "2026-09-16T00:00:00+00:00"})
    for index in range(count):
        variant_id = str(uuid4())
        repository.catalog_model_variants.append({
            "id": variant_id, "model_id": model_id,
            "canonical_key": f"cv1.{index:032d}",
            "promoted_from_candidate_id": None, "promoted_from_verdict_id": None,
            "created_at": "2026-09-16T00:00:00+00:00"})
        for field_key, value in (("model_year_start", 2020), ("model_year_end", 2021)):
            repository.catalog_canonical_field_provenance.append({
                "id": str(uuid4()), "model_id": model_id, "variant_id": variant_id,
                "field_key": field_key, "field_value": value, "revision": 1,
                "created_at": "2026-09-16T00:00:00+00:00"})


@pytest.fixture
def surface():
    """A client, a recording repository, a member and a project."""
    repository = durable_catalog()
    recording = RecordingRepository(repository)
    user, project = str(uuid4()), str(uuid4())
    repository.seed_user(user)
    repository.seed_project(project, "review", "Review", [user])
    app.dependency_overrides[get_repository] = lambda: recording
    app.dependency_overrides[get_job_launcher] = lambda: FailingLauncher()
    yield TestClient(app), recording, repository, user, project
    app.dependency_overrides.clear()


def member(user: str) -> dict[str, str]:
    return {"x-milo-auth-user-id": user}


# =============================================================================
# 1. authorization: authentication, then membership, then the read
# =============================================================================

@pytest.mark.parametrize("path", CODE3_PATHS)
def test_an_unauthenticated_request_is_rejected_by_the_normal_auth_boundary(surface, path):
    client, recording, _repository, _user, project = surface
    response = client.get(path.format(project_id=project))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"
    # Nothing was read, so nothing about the catalog was disclosed.
    assert recording.calls == []


@pytest.mark.parametrize("path", CODE3_PATHS)
def test_a_non_member_cannot_inspect_the_catalog_through_another_project_id(surface, path):
    client, recording, _repository, _user, project = surface
    response = client.get(path.format(project_id=project), headers=member(str(uuid4())))
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PROJECT_NOT_FOUND"
    # The membership check ran and then the request stopped: no catalog method
    # was reached at all, so a non-member learns nothing -- not even whether
    # the catalog holds anything.
    assert recording.calls == ["get_project"]


@pytest.mark.parametrize("path", CODE3_PATHS)
def test_a_project_that_does_not_exist_is_the_same_non_disclosing_answer(surface, path):
    client, _recording, _repository, user, _project = surface
    response = client.get(path.format(project_id=str(uuid4())), headers=member(user))
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PROJECT_NOT_FOUND"


def test_membership_is_checked_before_any_catalog_read(surface):
    client, recording, _repository, user, project = surface
    client.get(CANONICAL_PATH.format(project_id=project), headers=member(user))
    assert recording.calls[0] == "get_project"


# =============================================================================
# 2. the member's bounded read
# =============================================================================

def test_a_member_can_read_the_bounded_canonical_page(surface):
    client, _recording, _repository, user, project = surface
    response = client.get(CANONICAL_PATH.format(project_id=project), headers=member(user))
    assert response.status_code == 200
    body = response.json()
    assert body["page"]["limit"] == catalog_review.DEFAULT_REVIEW_PAGE_ITEMS
    assert body["page"]["offset"] == 0
    assert body["page"]["total"] == 1
    assert body["page"]["has_more"] is False
    item = body["items"][0]
    assert item["canonical_key"].startswith("cv1.")
    assert item["manufacturer"] and item["commercial_model"]
    assert item["model_year_start"] == item["model_year_end"] == 2022


def test_a_member_can_read_the_bounded_review_page(surface):
    client, _recording, _repository, user, project = surface
    response = client.get(REVIEW_PATH.format(project_id=project), headers=member(user))
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert body["unavailable_reason"] is None
    assert body["status"] == "ready_for_review"
    assert body["snapshot"]["snapshot_key"].startswith("cs1.")
    assert body["page"]["total"] == 1
    assert [item["status"] for item in body["items"]] == ["ready_for_review"]


# =============================================================================
# 3. reading is not execution
# =============================================================================

@pytest.mark.parametrize("path", CODE3_PATHS)
def test_catalog_execution_disabled_does_not_hide_the_read_surface(surface, path, monkeypatch):
    """The rollback property: the switch stops writing, never seeing.

    `MILO_ENABLE_CATALOG_EXECUTION` is already unset by the module fixture, so
    this asserts the surface answers in exactly the posture an operator is in
    after pulling it -- and then again with it explicitly `false`.
    """
    client, _recording, _repository, user, project = surface
    assert client.get(path.format(project_id=project),
                      headers=member(user)).status_code == 200
    monkeypatch.setenv(CATALOG_EXECUTION_FLAG, "false")
    assert client.get(path.format(project_id=project),
                      headers=member(user)).status_code == 200


@pytest.mark.parametrize("path", CODE3_PATHS)
def test_no_execution_flag_gates_the_review_surface(surface, path):
    """Every other kill switch is off too, and the read still answers.

    The API-level flags are unset in this module, so this request runs with
    run creation, proposal mutation/reads, cancellation and execution control
    all disabled -- and none of them is a prerequisite for a durable read.
    """
    client, _recording, _repository, user, project = surface
    assert client.get(path.format(project_id=project),
                      headers=member(user)).status_code == 200


@pytest.mark.parametrize("path", CODE3_PATHS)
def test_the_review_routes_are_not_in_the_execution_surface_guard(path):
    """Deliberate: no `SURFACE_RULES` entry may match a CODE-3 path.

    This is the drift alarm for §4 of the CODE-3 contract. Adding a rule for
    these paths would make the read disappear whenever a flag is off, which is
    precisely the rollback-blinding behaviour the surface exists to avoid.
    """
    concrete = path.format(project_id=str(uuid4()))
    for method, _flag, pattern, _surface in SURFACE_RULES:
        assert not pattern.match(concrete), f"{method} rule matches a CODE-3 read path"


# =============================================================================
# 4. pagination: bounded, refused when malformed, deterministic
# =============================================================================

def test_the_canonical_page_size_cannot_exceed_the_server_maximum(surface):
    client, _recording, _repository, user, project = surface
    response = client.get(CANONICAL_PATH.format(project_id=project),
                          params={"limit": catalog_review.MAX_REVIEW_PAGE_ITEMS + 1},
                          headers=member(user))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "CATALOG_REVIEW_PAGE_INVALID"


@pytest.mark.parametrize("path", CODE3_PATHS)
@pytest.mark.parametrize("params", [
    {"limit": 0}, {"limit": -1}, {"limit": 10_000}, {"limit": "many"}, {"limit": "1.5"},
    {"offset": -1}, {"offset": catalog_review.MAX_REVIEW_OFFSET + 1}, {"offset": "far"},
])
def test_invalid_pagination_values_fail_closed(surface, path, params):
    """Refused, never clamped: a caller who asked for -1 did not ask for 25."""
    client, recording, _repository, user, project = surface
    response = client.get(path.format(project_id=project), params=params,
                          headers=member(user))
    assert response.status_code in (400, 422)
    assert recording.writes == []


@pytest.mark.parametrize("path", CODE3_PATHS)
def test_the_server_maximum_is_never_exceeded_whatever_is_asked(surface, path):
    """Even at the maximum, the repository is called with at most the bound."""
    client, recording, repository, user, project = surface
    mark_every_candidate_ready(repository)
    seed_canonical_variants(repository, 250)
    recording.calls.clear()
    response = client.get(path.format(project_id=project),
                          params={"limit": catalog_review.MAX_REVIEW_PAGE_ITEMS},
                          headers=member(user))
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) <= catalog_review.MAX_REVIEW_PAGE_ITEMS
    assert body["page"]["limit"] == catalog_review.MAX_REVIEW_PAGE_ITEMS


def test_the_review_page_bound_is_within_the_database_bound():
    """CODE-3's bound is the tighter of the two, so both apply."""
    assert catalog_review.MAX_REVIEW_PAGE_ITEMS <= MAX_RESULT_ITEMS
    assert catalog_review.DEFAULT_REVIEW_PAGE_ITEMS <= catalog_review.MAX_REVIEW_PAGE_ITEMS


def test_the_canonical_total_is_exact_and_not_inferred_from_the_page(surface):
    """A page past the end still reports the real total, not zero."""
    client, _recording, repository, user, project = surface
    seed_canonical_variants(repository, 30)
    response = client.get(CANONICAL_PATH.format(project_id=project),
                          params={"limit": 10, "offset": 500}, headers=member(user))
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["page"]["total"] == 31
    assert body["page"]["has_more"] is False


def test_canonical_pagination_ordering_is_deterministic(surface):
    """Two disjoint pages, no overlap, no gap, stable across repeated reads."""
    client, _recording, repository, user, project = surface
    seed_canonical_variants(repository, 40)
    path = CANONICAL_PATH.format(project_id=project)

    def keys(offset: int) -> list[str]:
        response = client.get(path, params={"limit": 10, "offset": offset},
                              headers=member(user))
        assert response.status_code == 200
        return [item["canonical_key"] for item in response.json()["items"]]

    first, second = keys(0), keys(10)
    assert len(first) == len(second) == 10
    assert set(first).isdisjoint(second)
    assert first + second == sorted(first + second)
    assert keys(0) == first  # the same request, twice, is the same page


def test_review_pagination_ordering_is_deterministic(surface):
    client, _recording, repository, user, project = surface
    total = mark_every_candidate_ready(repository)
    assert total > catalog_review.MAX_REVIEW_PAGE_ITEMS
    path = REVIEW_PATH.format(project_id=project)

    def page(offset: int) -> list[str]:
        response = client.get(path, params={"limit": 20, "offset": offset},
                              headers=member(user))
        assert response.status_code == 200
        assert response.json()["page"]["total"] == total
        return [item["candidate_key"] for item in response.json()["items"]]

    first, second = page(0), page(20)
    assert set(first).isdisjoint(second)
    assert page(0) == first


# =============================================================================
# 5. filters are an allowlist, never query control
# =============================================================================

#: Names a caller might reach for that this surface does not accept. Several are
#: real query controls of the layers underneath -- `p_limit`/`p_status` are
#: arguments of `catalog_candidate_variant_page`, `allow_incomplete` is the
#: acknowledgement that lets an incomplete snapshot answer -- which is exactly
#: why being SILENT about them was the defect: a request naming one was answered
#: as though it had been honoured.
UNSUPPORTED_QUERY_PARAMETERS = (
    "order", "sort", "select", "table", "columns", "status", "snapshot_key",
    "snapshot_id", "resource_id", "allow_incomplete", "p_limit", "p_status",
    "q", "filter", "where", "limit_override",
)


@pytest.mark.parametrize("path", CODE3_PATHS)
@pytest.mark.parametrize("param", UNSUPPORTED_QUERY_PARAMETERS)
def test_an_unsupported_query_parameter_fails_closed(surface, path, param):
    """A name this route does not accept is REFUSED, never quietly dropped.

    Silently ignoring one is how `?snapshot_key=X` returns the ACTIVE snapshot
    while the operator believes X was inspected, and how `?status=promoted`
    returns `ready_for_review` rows under a heading the caller did not ask for.
    An answer to a question nobody asked is worse than no answer.
    """
    client, _recording, _repository, user, project = surface
    response = client.get(path.format(project_id=project),
                          params={param: "catalog_raw_records"}, headers=member(user))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "CATALOG_REVIEW_QUERY_PARAMETER_UNSUPPORTED"


@pytest.mark.parametrize("path", CODE3_PATHS)
@pytest.mark.parametrize("param", UNSUPPORTED_QUERY_PARAMETERS)
def test_the_refusal_echoes_neither_the_name_nor_the_value(surface, path, param):
    """A static, code-owned sentence. Nothing the caller sent comes back.

    An error that quoted the parameter would turn the refusal into a probe: a
    caller could enumerate which names the server recognises by reading its own
    input back, and a value echoed into an error is a reflection surface.
    """
    client, _recording, _repository, user, project = surface
    secret_value = "reflected-value-8d41f2"
    response = client.get(path.format(project_id=project),
                          params={param: secret_value}, headers=member(user))
    assert response.status_code == 400
    assert secret_value not in response.text
    # The message is the module's own authored constant, character for
    # character. Asserting `param not in body` would be unsound -- a
    # one-character name like `q` occurs in ordinary English -- and weaker:
    # equality with a constant proves nothing the caller sent can be in it.
    assert response.json()["error"]["message"] == catalog_review.UNSUPPORTED_PARAMETER_MESSAGE
    # ...and the refusal is identical whatever was sent, so it carries no signal
    # about which names the server recognises.
    other = client.get(path.format(project_id=project),
                       params={"totally-different-name": "x"}, headers=member(user))
    assert other.json() == response.json()


@pytest.mark.parametrize("path", CODE3_PATHS)
def test_an_unsupported_parameter_stops_before_any_catalog_read(surface, path):
    """Membership is checked, then the contract, then nothing.

    The ordering matters in both directions: the membership check must still
    run (so a non-member is refused as a non-member), and no catalog method may
    run after it (so a malformed request costs the database nothing).
    """
    client, recording, _repository, user, project = surface
    recording.calls.clear()
    response = client.get(path.format(project_id=project),
                          params={"order": "manufacturer.desc"}, headers=member(user))
    assert response.status_code == 400
    assert recording.calls == ["get_project"]
    assert recording.writes == []


@pytest.mark.parametrize("path", CODE3_PATHS)
@pytest.mark.parametrize("param", UNSUPPORTED_QUERY_PARAMETERS)
def test_a_non_member_sending_an_unsupported_parameter_still_learns_nothing(surface, path, param):
    """The authorization answer comes FIRST and is unchanged.

    If the query contract were checked before membership, a stranger could tell
    a real project from an imaginary one by which error came back. It is not,
    so a non-member receives the same non-disclosing 404 whatever they send.
    """
    client, recording, _repository, _user, project = surface
    stranger = str(uuid4())
    plain = client.get(path.format(project_id=project), headers=member(stranger))
    steered = client.get(path.format(project_id=project), params={param: "x"},
                         headers=member(stranger))
    assert plain.status_code == steered.status_code == 404
    assert plain.json() == steered.json()
    assert steered.json()["error"]["code"] == "PROJECT_NOT_FOUND"
    assert recording.writes == []


@pytest.mark.parametrize("path", CODE3_PATHS)
def test_a_repeated_query_parameter_fails_closed(surface, path):
    """`?limit=10&limit=20` is ambiguous, so it is refused rather than guessed.

    No repository-wide policy defines a reviewed behaviour for a repeated query
    parameter -- `after_event_id` on the run-events route is the only other one
    in the API and states none -- so there is nothing to follow here, and
    silently selecting the first or the last would answer a question the caller
    did not unambiguously ask.
    """
    client, recording, _repository, user, project = surface
    recording.calls.clear()
    response = client.get(f"{path.format(project_id=project)}?limit=10&limit=20",
                          headers=member(user))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "CATALOG_REVIEW_QUERY_PARAMETER_UNSUPPORTED"
    assert recording.calls == ["get_project"]


@pytest.mark.parametrize("path", CODE3_PATHS)
def test_a_repeated_filter_fails_closed_too(surface, path):
    client, _recording, _repository, user, project = surface
    response = client.get(
        f"{path.format(project_id=project)}?manufacturer=A&manufacturer=B",
        headers=member(user))
    assert response.status_code == 400


#: The allowlists the CODE-3 contract states, written out HERE rather than read
#: from the module under test. Reading them from `review.py` would make the
#: acceptance test tautological -- it would pass for any allowlist the code
#: happened to hold, including a wrong one.
EXPECTED_CANONICAL_PARAMETERS = ("limit", "offset", "manufacturer",
                                 "commercial_model", "model_year", "canonical_key")
EXPECTED_REVIEW_PARAMETERS = ("limit", "offset", "manufacturer",
                              "commercial_model", "model_year")


def test_the_per_route_allowlists_are_exactly_the_contract():
    assert catalog_review.CANONICAL_QUERY_PARAMETERS == EXPECTED_CANONICAL_PARAMETERS
    assert catalog_review.REVIEW_QUERY_PARAMETERS == EXPECTED_REVIEW_PARAMETERS
    # The review route accepts a strict subset: it names no canonical key,
    # because a candidate has none.
    assert set(EXPECTED_REVIEW_PARAMETERS) < set(EXPECTED_CANONICAL_PARAMETERS)


@pytest.mark.parametrize("path, allowed", [
    (CANONICAL_PATH, EXPECTED_CANONICAL_PARAMETERS),
    (REVIEW_PATH, EXPECTED_REVIEW_PARAMETERS),
])
def test_every_allowlisted_parameter_is_accepted(surface, path, allowed):
    """The allowlist is exactly what the route accepts -- no more, no less."""
    client, _recording, _repository, user, project = surface
    values = {"limit": "5", "offset": "0", "manufacturer": "Toyota",
              "commercial_model": "RAV4", "model_year": "2022",
              "canonical_key": "cv1." + "a" * 32}
    for name in allowed:
        response = client.get(path.format(project_id=project),
                              params={name: values[name]}, headers=member(user))
        assert response.status_code == 200, (name, response.text)


def test_canonical_key_is_canonical_only_and_is_refused_on_the_review_route(surface):
    """A parameter of the OTHER route is still an unsupported parameter here."""
    client, _recording, _repository, user, project = surface
    response = client.get(REVIEW_PATH.format(project_id=project),
                          params={"canonical_key": "cv1." + "a" * 32},
                          headers=member(user))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "CATALOG_REVIEW_QUERY_PARAMETER_UNSUPPORTED"


@pytest.mark.parametrize("path", CODE3_PATHS)
@pytest.mark.parametrize("param, value", [
    ("resource_id", src.QUANTITY_RESOURCE_ID),
    ("snapshot_key", "cs1." + "f" * 32),
    ("status", "promoted"),
    ("status", "candidate"),
    ("table", "catalog_raw_records"),
    ("select", "*"),
    ("order", "manufacturer.desc"),
    ("allow_incomplete", "true"),
])
def test_the_named_steering_parameters_can_never_reach_the_query(surface, path, param, value):
    """The eight the review named, each refused before any read happens.

    Refusal is the proof that matters. An inert parameter and a honoured one are
    indistinguishable from the caller's side when both return 200.
    """
    client, recording, _repository, user, project = surface
    recording.calls.clear()
    response = client.get(path.format(project_id=project), params={param: value},
                          headers=member(user))
    assert response.status_code == 400
    assert recording.calls == ["get_project"]


@pytest.mark.parametrize("path", CODE3_PATHS)
@pytest.mark.parametrize("value", [
    "' OR 1=1 --", "טויוטה'; drop table catalog_models; --",
    "manufacturer.desc", "*", "catalog_raw_records(payload)",
])
def test_sql_like_filter_values_are_compared_exactly_and_match_nothing(surface, path, value):
    """A filter VALUE is data. It is compared with `=`, never interpolated."""
    client, recording, _repository, user, project = surface
    response = client.get(path.format(project_id=project),
                          params={"manufacturer": value}, headers=member(user))
    assert response.status_code == 200
    assert response.json()["items"] == []
    assert recording.writes == []


@pytest.mark.parametrize("path", CODE3_PATHS)
def test_an_empty_filter_is_refused_rather_than_ignored(surface, path):
    client, _recording, _repository, user, project = surface
    response = client.get(path.format(project_id=project),
                          params={"manufacturer": "   "}, headers=member(user))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "CATALOG_REVIEW_FILTER_INVALID"


@pytest.mark.parametrize("path", CODE3_PATHS)
def test_an_oversized_filter_is_refused(surface, path):
    client, _recording, _repository, user, project = surface
    response = client.get(
        path.format(project_id=project),
        params={"manufacturer": "x" * (catalog_review.MAX_REVIEW_FILTER_CHARS + 1)},
        headers=member(user))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "CATALOG_REVIEW_FILTER_INVALID"


@pytest.mark.parametrize("path", CODE3_PATHS)
@pytest.mark.parametrize("year", [0, 1899, 2201, 99999, -2022])
def test_an_out_of_range_model_year_filter_is_refused(surface, path, year):
    client, _recording, _repository, user, project = surface
    response = client.get(path.format(project_id=project), params={"model_year": year},
                          headers=member(user))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "CATALOG_REVIEW_FILTER_INVALID"


def test_the_allowlisted_filters_actually_select(surface):
    """The filters that ARE supported narrow the page, exactly."""
    client, _recording, _repository, user, project = surface
    path = CANONICAL_PATH.format(project_id=project)
    everything = client.get(path, headers=member(user)).json()
    manufacturer = everything["items"][0]["manufacturer"]
    key = everything["items"][0]["canonical_key"]
    assert client.get(path, params={"manufacturer": manufacturer},
                      headers=member(user)).json()["page"]["total"] == 1
    assert client.get(path, params={"canonical_key": key},
                      headers=member(user)).json()["page"]["total"] == 1
    assert client.get(path, params={"model_year": 2022},
                      headers=member(user)).json()["page"]["total"] == 1
    assert client.get(path, params={"model_year": 1999},
                      headers=member(user)).json()["page"]["total"] == 0


# =============================================================================
# 6. only allowlisted fields reach the response
# =============================================================================

def test_only_allowlisted_canonical_fields_reach_the_response(surface):
    client, _recording, _repository, user, project = surface
    body = client.get(CANONICAL_PATH.format(project_id=project),
                      headers=member(user)).json()
    assert set(body) == {"page", "items"}
    assert set(body["page"]) == {"limit", "offset", "total", "has_more"}
    for item in body["items"]:
        assert set(item) == set(catalog_review.CANONICAL_ITEM_FIELDS)
        assert set(item) == set(CanonicalCatalogItem.model_fields)


def test_only_allowlisted_review_fields_reach_the_response(surface):
    client, _recording, _repository, user, project = surface
    body = client.get(REVIEW_PATH.format(project_id=project), headers=member(user)).json()
    assert set(body) == {"available", "unavailable_reason", "status", "snapshot",
                         "page", "items"}
    assert set(body["snapshot"]) == set(catalog_review.REVIEW_SNAPSHOT_FIELDS)
    assert set(body["snapshot"]) == set(CatalogReviewSnapshot.model_fields)
    for item in body["items"]:
        assert set(item) == set(catalog_review.REVIEW_CANDIDATE_ITEM_FIELDS)
        assert set(item) == set(CatalogReviewCandidateItem.model_fields)


def test_the_internal_columns_of_the_canonical_view_never_reach_a_browser(surface):
    """Named explicitly: these columns EXIST in the view and stay behind it."""
    client, _recording, _repository, user, project = surface
    text = client.get(CANONICAL_PATH.format(project_id=project),
                      headers=member(user)).text
    for column in ("variant_id", "model_id", "promoted_from_candidate_id",
                   "promoted_from_verdict_id", "field_revisions"):
        assert column not in text


def test_the_raw_record_provenance_of_a_candidate_never_reaches_a_browser(surface):
    """The page query JOINS these; the projection drops every one of them."""
    client, _recording, repository, user, project = surface
    mark_every_candidate_ready(repository)
    text = client.get(REVIEW_PATH.format(project_id=project), headers=member(user)).text
    for column in ("upstream_record_id", "raw_record_id", "candidate_id",
                   "source_locator", "payload_sha256", "payload", "snapshot_id"):
        assert column not in text


def test_no_raw_government_row_evidence_model_sql_or_secret_text_reaches_the_response(surface):
    """The whole response body, against everything it must never carry.

    The register field names are the decisive ones: a raw WLTP row is a
    `tozar`/`kinuy_mishari`/`degem_nm` object, so their absence is the proof
    that no preserved payload travelled with the page.
    """
    client, _recording, repository, user, project = surface
    mark_every_candidate_ready(repository)
    bodies = [client.get(path.format(project_id=project), headers=member(user)).text
              for path in CODE3_PATHS]
    forbidden = (
        # raw Government register columns (a preserved payload)
        "tozar", "kinuy_mishari", "degem_nm", "ramat_gimur", "delek_cd", "koah_sus",
        # evidence, verdicts and model material
        "chain_of_thought", "provider_detail", "evidence_fragment", "content_hash",
        "fragment_id", "claim_id", "verdict_id", "locator_key",
        # database, credential and lease material
        "select ", "SELECT ", "insert into", "pg_catalog", "errcode",
        "service_role", "api_key", "authorization", "lease_token", "worker_id",
        "SUPABASE_SERVICE_ROLE_KEY", "sk-",
    )
    for body in bodies:
        for needle in forbidden:
            assert needle not in body, f"{needle!r} reached the review response"


#: What `SupabaseRepository._many` would actually raise: `str(exc)` on a
#: PostgREST failure, which quotes SQL text and row values.
DATABASE_MESSAGE = ('ERROR: relation "catalog_models" does not exist\n'
                    'LINE 3: SELECT canonical_key FROM catalog_models WHERE '
                    "manufacturer = 'טויוטה'")


@pytest.mark.parametrize("path, method", [
    (CANONICAL_PATH, "list_canonical_catalog_variants"),
    (REVIEW_PATH, "list_active_catalog_snapshots"),
    (REVIEW_PATH, "catalog_candidate_variant_page"),
])
def test_a_repository_failure_never_puts_a_database_message_on_the_wire(surface, path, method):
    """A refusal is a classification, never the underlying text.

    Every entry point a repository failure can arrive through, because the two
    surfaces reach the repository by different paths: the canonical listing
    calls it directly, and the review page reaches it through the snapshot
    resolution AND through the bounded reader.
    """
    from backend.errors import AppError

    client, _recording, repository, user, project = surface

    def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise AppError("REPOSITORY_ERROR", DATABASE_MESSAGE, 502)

    setattr(repository, method, explode)
    response = client.get(path.format(project_id=project), headers=member(user))
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "CATALOG_REVIEW_UNAVAILABLE"
    for fragment in ("catalog_models", "SELECT", "LINE 3", "relation", "טויוטה"):
        assert fragment not in response.text


def test_a_backend_failure_is_distinguishable_from_no_snapshot(surface):
    """§12 needs both UI states, so they must not collapse into one answer.

    "There is no snapshot" is a 200 with a typed reason. "The database could
    not answer" is a 502. Reporting the second as the first would tell an
    operator the catalog is empty while it is merely unreachable.
    """
    from backend.errors import AppError

    client, _recording, repository, user, project = surface
    target = REVIEW_PATH.format(project_id=project)

    for row in repository.catalog_snapshots.values():
        row["activated_at"] = None
    absent = client.get(target, headers=member(user))
    assert absent.status_code == 200
    assert absent.json()["unavailable_reason"] == "no_active_snapshot"

    def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise AppError("REPOSITORY_ERROR", DATABASE_MESSAGE, 502)

    repository.list_active_catalog_snapshots = explode
    assert client.get(target, headers=member(user)).status_code == 502


# =============================================================================
# 7. the review page reads ONLY `ready_for_review`, from a USABLE snapshot
# =============================================================================

def test_candidate_review_returns_only_ready_for_review(surface):
    client, _recording, repository, user, project = surface
    statuses = {row["status"] for row in repository.catalog_candidates.values()}
    assert statuses - {"ready_for_review"}, "the fixture must hold other statuses too"
    body = client.get(REVIEW_PATH.format(project_id=project), headers=member(user)).json()
    assert body["items"]
    assert {item["status"] for item in body["items"]} == {"ready_for_review"}


@pytest.mark.parametrize("status", [s for s in CANDIDATE_STATUSES if s != "ready_for_review"])
def test_other_candidate_statuses_cannot_leak_into_the_review_page(surface, status):
    """Every other status, one at a time, held out of the page by the database."""
    client, _recording, repository, user, project = surface
    for row in repository.catalog_candidates.values():
        row["status"] = status
    body = client.get(REVIEW_PATH.format(project_id=project), headers=member(user)).json()
    assert body["available"] is True
    assert body["items"] == []
    assert body["page"]["total"] == 0


def test_a_status_the_page_should_not_have_returned_refuses_the_whole_page(surface):
    """Defence in depth: a read set disagreeing with its own filter is refused.

    Not filtered in Python and not partially returned -- a page that quietly
    dropped the odd rows would look complete and would not be.
    """
    client, _recording, repository, user, project = surface
    inner = repository.catalog_candidate_variant_page

    def lying_page(*args: Any, **kwargs: Any) -> Any:
        kwargs["status"] = None  # answer without the status filter the layer asked for
        return inner(*args, **kwargs)

    repository.catalog_candidate_variant_page = lying_page
    response = client.get(REVIEW_PATH.format(project_id=project), headers=member(user))
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "CATALOG_REVIEW_UNAVAILABLE"


def test_candidate_review_uses_the_active_government_wltp_snapshot(surface):
    client, _recording, repository, user, project = surface
    body = client.get(REVIEW_PATH.format(project_id=project), headers=member(user)).json()
    active = next(row for row in repository.catalog_snapshots.values()
                  if row["activated_at"] is not None)
    assert body["snapshot"]["snapshot_key"] == active["snapshot_key"]
    assert body["snapshot"]["resource_id"] == src.WLTP_RESOURCE_ID
    assert body["snapshot"]["package_id"] == src.CKAN_PACKAGE_ID
    assert active["source_family"] == src.GOVERNMENT_SOURCE_FAMILY


def test_the_review_surface_names_no_resource_and_cannot_be_pointed_elsewhere(surface):
    """The resource is a constant, and naming one is REFUSED rather than ignored.

    The original version of this test asserted the redirected request returned
    the same page as the plain one -- true, but the wrong property: a caller who
    sent `snapshot_key` was told nothing, and a 200 carrying the ACTIVE
    snapshot reads as confirmation that the named one was inspected. Refusing
    is what makes the answer honest.
    """
    client, recording, _repository, user, project = surface
    recording.calls.clear()
    redirected = client.get(REVIEW_PATH.format(project_id=project),
                            params={"resource_id": src.QUANTITY_RESOURCE_ID,
                                    "snapshot_key": "cs1." + "f" * 32},
                            headers=member(user))
    assert redirected.status_code == 400
    assert redirected.json()["error"]["code"] == "CATALOG_REVIEW_QUERY_PARAMETER_UNSUPPORTED"
    # Refused before any catalog read, so neither name reached the resolver.
    assert recording.calls == ["get_project"]
    # And the quantity resource is never named back to the caller either.
    assert src.QUANTITY_RESOURCE_ID not in redirected.text


def test_no_active_snapshot_is_an_explicit_unavailable_state_not_an_empty_catalog(surface):
    """The distinction §6 requires: unavailable is not "reviewed and empty"."""
    client, _recording, repository, user, project = surface
    for row in repository.catalog_snapshots.values():
        row["activated_at"] = None
    body = client.get(REVIEW_PATH.format(project_id=project), headers=member(user)).json()
    assert body["available"] is False
    assert body["unavailable_reason"] == "no_active_snapshot"
    assert body["items"] == []
    assert body["snapshot"] is None
    # NOT zero. A zero total is the claim that a snapshot was read and held
    # nothing, which is a different -- and false -- statement.
    assert body["page"]["total"] is None
    assert body["page"]["has_more"] is None


def _raw_only_reading(_stored: int) -> dict[str, Any]:
    """A VALID raw-only reading: nothing read, so nothing claimed."""
    return {"normalization_contract": RAW_ONLY_CONTRACT, "normalized_record_count": 0,
            "normalization_issue_count": 0, "normalization_issues": [],
            "normalization_issue_records": []}


def _incomplete_reading(stored: int) -> dict[str, Any]:
    """A VALID, INTERNALLY CONSISTENT reading that records a real gap.

    Every rule `parse_normalization_state` enforces is satisfied -- the counts
    sum to the snapshot's own row count, the reason is in the closed
    vocabulary, the entry counts sum to the issue count, and the id list is
    exactly as long as the durable bound implies. So this snapshot is refused
    for the ONE reason that is actually true of it: it holds rows its
    vocabulary could not read.
    """
    issues = 4
    return {"normalization_contract": NORMALIZATION_CONTRACT,
            "normalized_record_count": stored - issues,
            "normalization_issue_count": issues,
            "normalization_issues": [{"reason": "GOV_NORM_MODEL_YEAR_INVALID",
                                      "count": issues}],
            "normalization_issue_records": [str(90_000 + index) for index in range(issues)]}


def _unread_reading(_stored: int) -> dict[str, Any]:
    """No stated reading at all: the contract string is simply absent."""
    return {"normalized_record_count": 0, "normalization_issue_count": 0,
            "normalization_issues": [], "normalization_issue_records": []}


def _malformed_reading(stored: int) -> dict[str, Any]:
    """A reading that claims a different number of rows than the snapshot holds."""
    return {"normalization_contract": NORMALIZATION_CONTRACT,
            "normalized_record_count": stored + 17, "normalization_issue_count": 0,
            "normalization_issues": [], "normalization_issue_records": []}


@pytest.mark.parametrize("reading, reason", [
    (_raw_only_reading, "snapshot_not_normalized"),
    (_incomplete_reading, "snapshot_incomplete"),
    (_unread_reading, "snapshot_not_read"),
    (_malformed_reading, "snapshot_state_invalid"),
])
def test_an_incomplete_snapshot_is_never_silently_the_review_source(surface, reading, reason):
    """Each unusable state names itself; none of them answers.

    The snapshot stays ACTIVE and `complete` throughout -- only its recorded
    reading changes -- so what is proved is that activation alone does not make
    a snapshot answerable.
    """
    client, _recording, repository, user, project = surface
    for row in repository.catalog_snapshots.values():
        row["retrieval_metadata"] = {k: v for k, v in row["retrieval_metadata"].items()
                                     if not k.startswith("normal")}
        row["retrieval_metadata"].update(reading(int(row["stored_record_count"])))
        assert row["activated_at"] is not None and row["validation_state"] == "complete"
    body = client.get(REVIEW_PATH.format(project_id=project), headers=member(user)).json()
    assert body["available"] is False
    assert body["unavailable_reason"] == reason
    assert body["items"] == []
    assert body["snapshot"] is None
    assert body["page"]["total"] is None


def test_a_pending_snapshot_is_not_an_active_one(surface):
    """`validation_state` is part of what makes a snapshot readable."""
    client, _recording, repository, user, project = surface
    for row in repository.catalog_snapshots.values():
        row["validation_state"] = "pending"
        row["activated_at"] = None
    body = client.get(REVIEW_PATH.format(project_id=project), headers=member(user)).json()
    assert body["available"] is False
    assert body["items"] == []


def test_every_unavailable_reason_is_in_the_closed_vocabulary(surface):
    """No Government projection code escapes into the browser contract."""
    client, _recording, repository, user, project = surface
    for row in repository.catalog_snapshots.values():
        row["activated_at"] = None
    body = client.get(REVIEW_PATH.format(project_id=project), headers=member(user)).json()
    assert body["unavailable_reason"] in catalog_review.REVIEW_UNAVAILABLE_REASONS
    assert not body["unavailable_reason"].startswith("GOV_")


def test_an_unusable_snapshot_does_not_hide_the_canonical_catalog(surface):
    """The two surfaces are independent: canonical rows are still readable."""
    client, _recording, repository, user, project = surface
    for row in repository.catalog_snapshots.values():
        row["activated_at"] = None
    canonical = client.get(CANONICAL_PATH.format(project_id=project), headers=member(user))
    assert canonical.status_code == 200
    assert canonical.json()["page"]["total"] == 1


# =============================================================================
# 8. an empty catalog is an empty page, honestly
# =============================================================================

def test_an_empty_canonical_catalog_reports_zero_rather_than_unavailable(surface):
    """Zero here is a real answer: the listing ran and matched nothing."""
    client, _recording, repository, user, project = surface
    repository.catalog_model_variants.clear()
    body = client.get(CANONICAL_PATH.format(project_id=project), headers=member(user)).json()
    assert body["items"] == []
    assert body["page"]["total"] == 0
    assert body["page"]["has_more"] is False


# =============================================================================
# 9. the read cannot write, launch, lease or reach the network
# =============================================================================

@pytest.mark.parametrize("path", CODE3_PATHS)
def test_no_catalog_write_method_is_invoked(surface, path):
    client, recording, repository, user, project = surface
    mark_every_candidate_ready(repository)
    before = {
        "snapshots": {k: dict(v) for k, v in repository.catalog_snapshots.items()},
        "candidates": {k: dict(v) for k, v in repository.catalog_candidates.items()},
        "records": {k: dict(v) for k, v in repository.catalog_raw_records.items()},
        "variants": [dict(row) for row in repository.catalog_model_variants],
        "models": [dict(row) for row in repository.catalog_models],
        "provenance": [dict(row) for row in repository.catalog_canonical_field_provenance],
    }
    recording.calls.clear()
    assert client.get(path.format(project_id=project),
                      headers=member(user)).status_code == 200
    assert recording.writes == []
    assert {k: dict(v) for k, v in repository.catalog_snapshots.items()} == before["snapshots"]
    assert {k: dict(v) for k, v in repository.catalog_candidates.items()} == before["candidates"]
    assert {k: dict(v) for k, v in repository.catalog_raw_records.items()} == before["records"]
    assert [dict(r) for r in repository.catalog_model_variants] == before["variants"]
    assert [dict(r) for r in repository.catalog_models] == before["models"]
    assert [dict(r) for r in repository.catalog_canonical_field_provenance] \
        == before["provenance"]


@pytest.mark.parametrize("path", CODE3_PATHS)
def test_only_read_only_repository_methods_are_reached(surface, path):
    """The whole repository surface a request touches, against the allowlist."""
    client, recording, _repository, user, project = surface
    recording.calls.clear()
    assert client.get(path.format(project_id=project),
                      headers=member(user)).status_code == 200
    assert set(recording.calls) <= set(catalog_review.READ_ONLY_REPOSITORY_METHODS)


@pytest.mark.parametrize("path", CODE3_PATHS)
def test_no_lease_run_claim_or_run_is_created(surface, path):
    client, recording, repository, user, project = surface
    runs_before = {key: dict(value) for key, value in repository.runs.items()}
    recording.calls.clear()
    client.get(path.format(project_id=project), headers=member(user))
    for forbidden in ("claim_run", "heartbeat_run", "create_queued_run",
                      "create_message_and_run", "try_acquire_launch", "set_launch_state"):
        assert forbidden not in recording.calls
    assert {key: dict(value) for key, value in repository.runs.items()} == runs_before


@pytest.mark.parametrize("path", CODE3_PATHS)
def test_no_transport_client_or_model_gateway_is_constructed(surface, path, monkeypatch):
    """Construction itself is made an error, so absence is proved, not assumed."""
    import backend.catalog.government.client as client_module
    import backend.catalog.government.transport as transport_module

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a catalog review read constructed a Government caller")

    monkeypatch.setattr(transport_module.HttpsDataGovTransport, "__init__", refuse)
    monkeypatch.setattr(client_module.DataGovClient, "__init__", refuse)
    client, _recording, _repository, user, project = surface
    assert client.get(path.format(project_id=project),
                      headers=member(user)).status_code == 200


@pytest.mark.parametrize("path", CODE3_PATHS)
def test_repeated_identical_requests_perform_no_mutation(surface, path):
    """Replay: the same GET five times is five identical reads and zero writes."""
    client, recording, repository, user, project = surface
    target = path.format(project_id=project)
    recording.calls.clear()
    bodies = [client.get(target, headers=member(user)).json() for _ in range(5)]
    assert all(body == bodies[0] for body in bodies)
    assert recording.writes == []
    assert len(repository.catalog_model_variants) == 1


# =============================================================================
# 10. there is no mutating counterpart of either route
# =============================================================================

@pytest.mark.parametrize("path", CODE3_PATHS)
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_no_mutating_method_exists_on_a_review_route(surface, path, method):
    client, recording, _repository, user, project = surface
    response = client.request(method, path.format(project_id=project),
                              headers=member(user), json={})
    assert response.status_code == 405
    assert recording.writes == []


def test_the_registered_review_routes_are_get_only():
    """Asserted against the app's own routing table, not against this file."""
    registered = {(route.path, frozenset(route.methods))
                  for route in app.routes if "/catalog/" in getattr(route, "path", "")}
    assert registered == {
        ("/projects/{project_id}/catalog/canonical", frozenset({"GET"})),
        ("/projects/{project_id}/catalog/review-candidates", frozenset({"GET"})),
    }


# =============================================================================
# 11. the projection layer itself, over hostile stored values
# =============================================================================

@pytest.mark.parametrize("stored", [
    {"identity_dimensions": {"not_a_dimension": "x"}},
    {"identity_dimensions": {"body_style": {"nested": "object"}}},
    {"identity_dimensions": "a string, not an object"},
    {"identity_dimensions": ["a", "list"]},
])
def test_an_unreadable_identity_dimension_is_dropped_not_rendered(stored):
    projected = catalog_review.canonical_item({"canonical_key": "cv1." + "0" * 32, **stored})
    assert projected["identity_dimensions"] == {}


@pytest.mark.parametrize("value", [True, False, "2022", 20.22, None, [], {}])
def test_a_model_year_that_is_not_a_whole_number_is_absent_not_zero(value):
    projected = catalog_review.canonical_item({"canonical_key": "cv1." + "0" * 32,
                                               "model_year_start": value})
    assert projected["model_year_start"] is None


@pytest.mark.parametrize("value", ["", "   ", 42, None, {"a": 1}])
def test_an_unusable_text_column_is_absent_not_an_empty_string(value):
    projected = catalog_review.canonical_item({"canonical_key": "cv1." + "0" * 32,
                                               "trim": value})
    assert projected["trim"] is None


def test_a_stored_string_is_bounded_before_it_is_returned():
    projected = catalog_review.canonical_item({"canonical_key": "cv1." + "0" * 32,
                                               "manufacturer": "x" * 5_000})
    assert len(projected["manufacturer"]) == catalog_review.MAX_REVIEW_FILTER_CHARS


def test_the_projection_never_copies_a_column_it_does_not_name():
    projected = catalog_review.canonical_item({
        "canonical_key": "cv1." + "0" * 32,
        "payload": {"tozar": "x"}, "lease_token": "secret", "variant_id": str(uuid4()),
        "field_revisions": {"trim": 3}, "an_unreviewed_column": "value"})
    assert set(projected) == set(catalog_review.CANONICAL_ITEM_FIELDS)


def test_a_page_reports_unknown_rather_than_zero_when_the_total_is_not_stated():
    """PostgREST may decline to count. `None` travels; `0` would be a claim."""
    page = catalog_review.CatalogPage(items=(), limit=25, offset=0, total=None)
    assert page.total is None
    assert page.has_more is None


# =============================================================================
# 12. the RESPONSE BOUNDARY: a secret never reaches the browser at all
# =============================================================================
#
# The frontend parser redacts every durable string before it renders one, and
# that stays. It is not sufficient on its own: by the time it runs, the HTTP
# response has already been delivered to the browser, sat in the network panel,
# and been handled by whatever else observes traffic. "Hidden from the DOM" is
# not "never sent".
#
# So redaction happens on the way OUT of the API too, and these tests read the
# RAW response body rather than a parsed projection.

#: Credential-shaped sentinels, assembled at runtime so no key-shaped literal is
#: committed -- the same rule `frontend/tests/secretSentinels.ts` follows, and
#: the same four shapes, so the two boundaries are swept against one vocabulary.
API_KEY_SENTINEL = "-".join(["sk", "live", "A" * 8 + "B" * 8 + "9876"])
BEARER_SENTINEL = f"Bearer {API_KEY_SENTINEL}"
JWT_SENTINEL = ".".join(["eyJhbGciOiJIUzI1NiJ9", "eyJyb2xlIjoic2VydmljZSJ9",
                         "not-a-real-signature"])
SUPABASE_SECRET_SENTINEL = "_".join(["sb", "secret", "A" * 10 + "B" * 10 + "1234"])
ALL_SECRET_SENTINELS = (API_KEY_SENTINEL, BEARER_SENTINEL, JWT_SENTINEL,
                        SUPABASE_SECRET_SENTINEL)

#: The same four, carried as pytest params under LABELS rather than values.
#: A parametrized value becomes a node id, and pytest writes node ids into
#: `.pytest_cache`; CI runs the suite before `scripts/secret_scan.py`, so a raw
#: sentinel in an id would fail the scan on a generated file. The label keeps
#: the value out of every artifact and makes the test output readable.
SENTINEL_PARAMS = (
    pytest.param(API_KEY_SENTINEL, id="provider_api_key"),
    pytest.param(BEARER_SENTINEL, id="bearer_token"),
    pytest.param(JWT_SENTINEL, id="legacy_jwt"),
    pytest.param(SUPABASE_SECRET_SENTINEL, id="supabase_secret_key"),
)

#: Fragments that must not survive even partially redacted.
SECRET_FRAGMENTS = ("sk-live-", "eyJhbGciOiJIUzI1NiJ9", "Bearer ", "sb_secret_")


def poison_canonical(repository: MemoryRepository, value: str) -> None:
    """Put credential-shaped text in every canonical string CODE-3 may project.

    Durable columns the projection is otherwise ALLOWED to carry -- not fields
    it already drops. A test that poisoned a dropped column would prove only
    that the field allowlist works, which is a different property and is
    already covered above.
    """
    for row in repository.catalog_models:
        row["manufacturer"] = f"Toyota {value}"
        row["commercial_model"] = f"RAV4 {value}"
    for row in repository.catalog_canonical_field_provenance:
        if row["field_key"] in ("trim", "official_model_code"):
            row["field_value"] = f"TRIM {value}"


def poison_review(repository: MemoryRepository, value: str) -> None:
    """The same, for the candidate rows and the snapshot metadata."""
    for row in repository.catalog_candidates.values():
        row["manufacturer"] = f"Toyota {value}"
        row["trim"] = f"TRIM {value}"
    for row in repository.catalog_snapshots.values():
        metadata = dict(row["retrieval_metadata"])
        metadata["dataset_title"] = f"WLTP {value}"
        metadata["publisher"] = f"ministry {value}"
        row["retrieval_metadata"] = metadata


@pytest.mark.parametrize("sentinel", SENTINEL_PARAMS)
def test_a_credential_in_a_canonical_column_never_reaches_the_response(surface, sentinel):
    client, _recording, repository, user, project = surface
    poison_canonical(repository, sentinel)
    response = client.get(CANONICAL_PATH.format(project_id=project), headers=member(user))
    assert response.status_code == 200
    assert sentinel not in response.text
    for fragment in SECRET_FRAGMENTS:
        assert fragment not in response.text


@pytest.mark.parametrize("sentinel", SENTINEL_PARAMS)
def test_a_credential_in_a_candidate_or_snapshot_never_reaches_the_response(surface, sentinel):
    client, _recording, repository, user, project = surface
    poison_review(repository, sentinel)
    response = client.get(REVIEW_PATH.format(project_id=project), headers=member(user))
    assert response.status_code == 200
    assert sentinel not in response.text
    for fragment in SECRET_FRAGMENTS:
        assert fragment not in response.text


def test_the_row_is_still_readable_after_its_credential_is_removed(surface):
    """Redaction, not rejection. A poisoned row is still a reviewable row.

    Refusing the whole page over one odd-looking value would be a denial of
    inspection on the surface an operator reaches for during an incident, so
    the credential is replaced and the rest of the identity survives.
    """
    client, _recording, repository, user, project = surface
    poison_canonical(repository, API_KEY_SENTINEL)
    body = client.get(CANONICAL_PATH.format(project_id=project), headers=member(user)).json()
    item = body["items"][0]
    assert body["page"]["total"] == 1
    assert item["manufacturer"].startswith("Toyota")
    assert item["canonical_key"].startswith("cv1.")
    assert item["model_year_start"] == 2022


def test_ordinary_register_identity_text_is_never_over_redacted(surface):
    """The real pinned capture, unpoisoned, survives byte for byte.

    Over-redaction is the safe failure direction, but it is still a failure if
    it fires on ordinary data -- a marque or a trim that happened to trip a
    pattern would quietly corrupt the catalog an operator is reviewing.
    """
    client, _recording, repository, user, project = surface
    mark_every_candidate_ready(repository)
    canonical = client.get(CANONICAL_PATH.format(project_id=project),
                           headers=member(user)).json()
    review = client.get(REVIEW_PATH.format(project_id=project),
                        params={"limit": 100}, headers=member(user)).json()
    assert canonical["items"][0]["trim"] == "ADVENTURE"
    assert canonical["items"][0]["official_model_code"] == "AXAA54L-ANZVB"
    assert review["snapshot"]["publisher"] == src.GOVERNMENT_PUBLISHER
    assert "[REDACTED]" not in client.get(CANONICAL_PATH.format(project_id=project),
                                          headers=member(user)).text


def test_the_backend_redactor_neutralizes_the_reviewed_marker_vocabulary():
    """The repository already has ONE reviewed secret-marker vocabulary.

    `_FRAGMENT_SECRET_MARKERS` (`backend/engines/swarm_v2/evidence.py`) is the
    evidence-fragment PERSISTENCE boundary: it REJECTS, it is mirrored by
    `supabase/migrations/20260828000200_source_evidence_fragments.sql`, and it
    is a marker set rather than a shape matcher. It is deliberately left where
    it is -- moving it would mean editing a migration's comment.

    This is the drift alarm that keeps the two from disagreeing: every marker
    that vocabulary names must be something the response-boundary redactor also
    neutralizes, so a value the durable boundary would refuse can never be the
    value this boundary prints.
    """
    from backend.engines.swarm_v2.evidence import _FRAGMENT_SECRET_MARKERS
    from backend.redaction import REDACTED, redact_secret_text

    value = "AKIAIOSFODNN7EXAMPLEabc"
    for marker in _FRAGMENT_SECRET_MARKERS:
        # Each marker, in the form it actually occurs. Gluing a token straight
        # onto `-----begin` would build a string that is neither a PEM block
        # nor a labelled secret, and proving the redactor ignores THAT would
        # prove nothing about the vocabulary.
        if marker.startswith("-----"):
            # Assembled, never written literally: a committed PEM header is
            # what `scripts/secret_scan.py` blocks, and a test fixture is not
            # an exemption -- a key-shaped literal trains reviewers and
            # scanners to expect false positives.
            dashes = "-" * 5
            text = (f"prefix {dashes}BEGIN PRIVATE KEY{dashes}\n{value}\n"
                    f"{dashes}END PRIVATE KEY{dashes} suffix")
        elif marker.endswith(("=", ":")):
            text = f"prefix {marker}{value} suffix"
        else:
            text = f"prefix {marker}={value} suffix"
        redacted = redact_secret_text(text)
        assert REDACTED in redacted, marker
        assert value not in redacted, marker


@pytest.mark.parametrize("sentinel", SENTINEL_PARAMS)
def test_the_backend_and_frontend_redactors_agree_on_every_sentinel(sentinel):
    """Two independent boundaries, one vocabulary.

    The frontend sentinels are the authority (`frontend/tests/secretSentinels.ts`)
    because the browser sweep is the one a reviewer can see in a bundle scan.
    The backend must neutralize each of them too, or the response would carry
    something only the browser knows to hide.
    """
    from backend.redaction import REDACTED, redact_secret_text

    redacted = redact_secret_text(f"value {sentinel} tail")
    assert sentinel not in redacted
    assert REDACTED in redacted
    assert redacted.endswith("tail")


def test_the_redactor_leaves_public_supabase_configuration_alone():
    """`sb_publishable_…` is public configuration the browser is MEANT to hold.

    Anchoring on `sb_` rather than `sb_secret_` would hide a legitimate value
    while protecting nothing -- the same distinction
    `frontend/lib/sanitize.ts` draws, held to here so the two cannot diverge.
    """
    from backend.redaction import redact_secret_text

    public = "_".join(["sb", "publishable", "C" * 10 + "D" * 10 + "5678"])
    assert redact_secret_text(f"key {public}") == f"key {public}"


def test_the_redactor_is_total_over_unusable_input():
    """It is a boundary, so it never raises: anything in, a string out."""
    from backend.redaction import redact_secret_text

    assert redact_secret_text("") == ""
    assert redact_secret_text("ordinary text") == "ordinary text"

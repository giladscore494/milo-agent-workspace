"""Catalog PR2: the Government capture, the writes it makes, and what it refuses.

Offline and deterministic. Every byte read here is a committed R5 fixture,
re-hashed against the R5 manifest before it reaches the client, and an autouse
fixture makes creating a socket an error for the whole module -- so "no network
call" is enforced rather than asserted.

`tests/test_migrations_postgres.py` proves the durable half against real
PostgreSQL. What lives here is everything above the database: the completeness
gate, the retry boundary, the normalization rules, the lease requirement at the
repository boundary, replay and refresh, and the internal query projection.
"""

from __future__ import annotations

import copy
import json
import socket
from pathlib import Path
from uuid import uuid4

import pytest

from backend.catalog.contracts import stated_source_locator
from backend.catalog.government import normalize, projection, snapshot as snapshot_module
from backend.catalog.government import source as src
from backend.catalog.government import vocabulary as vocab
from backend.catalog.government.client import DataGovClient, schema_fingerprint
from backend.catalog.government.ingest import (GovernmentCatalogIngestor,
                                               GovernmentIngestionError)
from backend.catalog.government.normalize import (GovernmentNormalizationError,
                                                  read_wltp_record)
from backend.catalog.government.projection import (GovernmentCatalogProjection,
                                                   GovernmentProjectionError)
from backend.catalog.government.source import GovernmentSourceError
from backend.engines.swarm_v2.evidence import WorkerLease
from backend.errors import AppError
from backend.runtime import CancellationRequested
from backend.testing import government_capture as capture_fixtures
from backend.testing.government_capture import (FixtureTransport, PINNED_PAGE_COUNTS,
                                                PINNED_PAGE_LIMIT, PINNED_QUERY,
                                                PINNED_RECORD_ID, PINNED_TOTAL, encode,
                                                page_document)
from backend.testing.memory_repository import MemoryRepository
from backend.tools.registry import ToolRegistry

GOVERNMENT_PACKAGE = Path("backend/catalog/government")


# =============================================================================
# 0. the module is offline by construction
# =============================================================================

@pytest.fixture(autouse=True)
def no_sockets(monkeypatch):
    """Creating a socket anywhere in this module is a test failure.

    A fixture-backed suite that COULD open a connection is one refactor away
    from being a live integration nobody reviewed, so the capability is removed
    rather than left unused.
    """
    def refuse(*_args, **_kwargs):
        raise AssertionError("an offline catalog test attempted a network connection")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def test_only_the_transport_module_can_reach_a_network():
    """Offline by construction: one module may name an HTTP library.

    `transport.py` is the single seam that can open a socket, and it is
    constructed explicitly -- no production entrypoint builds one in this PR.
    Every other module in the package is pure logic over material a transport
    already returned, which is what makes the whole capture path testable with
    no network at all.
    """
    forbidden = ("import requests", "import httpx", "import socket", "urllib.request",
                 "from supabase", "import psycopg", "openai", "webbrowser")
    for path in sorted(GOVERNMENT_PACKAGE.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for token in forbidden:
            if path.name == "transport.py" and token == "import requests":
                continue
            assert token not in source, f"{path} names {token}"


def test_no_module_in_the_package_calls_a_provider_or_a_model():
    """No Kimi, no gateway, no scheduler, no budget: this path is free."""
    for path in sorted(GOVERNMENT_PACKAGE.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for token in ("ModelGateway", "model_gateway", "chat.completions", "moonshot",
                      "BudgetTracker", "ProviderScheduler", "api_key"):
            assert token not in source, f"{path} names {token}"


# =============================================================================
# helpers
# =============================================================================

def leased_run(repository: MemoryRepository, worker: str = "worker-1") -> WorkerLease:
    """A real run holding a real active lease, through the ordinary path."""
    user, project = str(uuid4()), str(uuid4())
    repository.seed_user(user)
    repository.seed_project(project, f"p-{worker}", "P", [user])
    conversation = repository.create_conversation(project, "c", user)
    message = repository.create_user_message(conversation["id"], "go", {})
    run = repository.create_queued_run(conversation["id"], message["id"], "go", {},
                                       requested_by=user)
    claimed = repository.claim_run(run["id"], worker)
    return WorkerLease(claimed["id"], worker, int(claimed["attempt"]), claimed["lease_token"])


def client(**kwargs) -> DataGovClient:
    transport = kwargs.pop("transport", None) or FixtureTransport()
    kwargs.setdefault("page_limit", PINNED_PAGE_LIMIT)
    kwargs.setdefault("sleep_fn", lambda _seconds: None)
    return DataGovClient(transport, **kwargs)


def ingest(repository: MemoryRepository, lease: WorkerLease, **kwargs):
    ingestor = GovernmentCatalogIngestor(repository, lease, client=client(**kwargs))
    return ingestor.ingest_resource(src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY))


@pytest.fixture
def repository() -> MemoryRepository:
    return MemoryRepository()


@pytest.fixture
def ingested(repository):
    lease = leased_run(repository)
    return lease, ingest(repository, lease)


def refuses(reason_code, call, *args, **kwargs):
    with pytest.raises(GovernmentSourceError) as failure:
        call(*args, **kwargs)
    assert failure.value.reason_code == reason_code
    return failure.value


# =============================================================================
# 1. the three committed pages are ONE complete query
# =============================================================================

def test_the_three_committed_pages_form_one_complete_query():
    """`q=RAV4&limit=100` reports 233 rows and was served as 100 + 100 + 33.

    All three are read, in the server's own pagination order, and the capture
    is complete only because the page lengths sum to the total the datastore
    itself reported on every page.
    """
    capture = client().capture_resource(src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY))
    assert capture.reported_total == PINNED_TOTAL
    assert tuple(page.record_count for page in capture.pages) == PINNED_PAGE_COUNTS
    assert tuple(page.offset for page in capture.pages) == (0, 100, 200)
    assert capture.record_count == PINNED_TOTAL
    assert len({record["_id"] for _, record in capture.located_records()}) == PINNED_TOTAL


def test_each_page_checksum_is_the_committed_response_digest():
    """The captured-response checksum is the digest of the bytes themselves.

    Proven against the R5 manifest, which recorded each page's `fixture_sha256`
    when the signed capture archive was imported -- so this is a cross-check
    against provenance written by a different round, not a self-consistent
    restatement.
    """
    from backend.testing.r5_proof.manifest import source_entry

    capture = client().capture_resource(src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY))
    for page, key in zip(capture.pages, capture_fixtures.PAGE_SOURCE_KEYS.values()):
        entry = source_entry(key)
        assert page.body_sha256 == entry["fixture_sha256"] == entry["upstream_sha256"]
        assert page.byte_count == entry["fixture_byte_count"] == entry["response_byte_count"]


def test_the_resource_version_is_the_resources_own_metadata():
    """Read from `package_show`, never invented and never the response digest."""
    metadata = client().package_show(src.WLTP_RESOURCE_ID)
    assert metadata.upstream_version_kind == "dataset_version"
    assert metadata.upstream_version == "2026-09-14T02:41:31.842626"
    assert metadata.publisher == src.GOVERNMENT_PUBLISHER
    # The resource's published `hash` is kept as provenance and is NOT the
    # version: it is an MD5 of the full CSV export, not of the JSON served.
    assert metadata.resource_content_hash == "79ab5917935c722fa6de8460a594a778"
    assert metadata.resource_content_hash != metadata.upstream_version


def test_a_resource_with_no_version_is_refused_rather_than_pinned_to_a_digest():
    package = json.loads(capture_fixtures.package_body().decode("utf-8"))
    for resource in package["result"]["resources"]:
        resource.pop("last_modified", None)
        resource.pop("revision_id", None)
    transport = FixtureTransport(bodies={"package": encode(package)})
    refuses("GOV_RESOURCE_UNVERSIONED", client(transport=transport).package_show,
            src.WLTP_RESOURCE_ID)


# =============================================================================
# 2 & 3. a page that is not this query's page fails closed
# =============================================================================

def page_with(at, **changes):
    """The committed page captured at offset `at`, with the stated changes.

    The parameter is named `at` rather than `offset` precisely because
    `offset` is one of the fields a test mutates.
    """
    document = page_document(at)
    document["result"].update(changes)
    return encode(document)


PAGE_REFUSALS = (
    # (label, offset the body is served at, the mutated body, expected reason)
    ("a page served at the wrong offset", 100, lambda: capture_fixtures.page_body(0),
     "GOV_PAGE_OFFSET_UNEXPECTED"),
    ("a page that echoes a different offset", 0, lambda: page_with(0, offset=50),
     "GOV_PAGE_OFFSET_UNEXPECTED"),
    ("a page served at a different page size", 0, lambda: page_with(0, limit=50),
     "GOV_PAGE_LIMIT_UNEXPECTED"),
    ("a page answering another query", 100, lambda: page_with(100, q="COROLLA"),
     "GOV_QUERY_ECHO_MISMATCH"),
    ("a page answering another resource", 100,
     lambda: page_with(100, resource_id=src.QUANTITY_RESOURCE_ID),
     "GOV_RESOURCE_ECHO_MISMATCH"),
    ("a page reporting another total", 100, lambda: page_with(100, total=234),
     "GOV_PAGE_TOTAL_INCONSISTENT"),
    ("a page reporting an ESTIMATED total", 0, lambda: page_with(0, total_was_estimated=True),
     "GOV_TOTAL_ESTIMATED"),
    ("a page that is short", 0, lambda: page_with(0, records=page_document(0)["result"]["records"][:99]),
     "GOV_PAGE_COUNT_UNEXPECTED"),
    ("a page that is long", 200,
     lambda: page_with(200, records=page_document(200)["result"]["records"]
                       + page_document(0)["result"]["records"][:1]),
     "GOV_PAGE_COUNT_UNEXPECTED"),
    ("a page that is not served as objects", 0, lambda: page_with(0, records_format="csv"),
     "GOV_RECORDS_FORMAT_UNEXPECTED"),
    ("a page declaring a different field schema", 100,
     lambda: page_with(100, fields=page_document(100)["result"]["fields"][:-1]),
     "GOV_SCHEMA_DRIFT"),
    ("a page declaring no field schema", 0, lambda: page_with(0, fields=[]),
     "GOV_SCHEMA_INVALID"),
)


@pytest.mark.parametrize("label,offset,body,reason",
                         PAGE_REFUSALS, ids=[case[0] for case in PAGE_REFUSALS])
def test_a_page_that_is_not_this_querys_page_fails_closed(label, offset, body, reason):
    transport = FixtureTransport(bodies={offset: body()})
    refuses(reason, client(transport=transport).capture_resource,
            src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY))


def test_a_reordered_or_overlapping_page_is_refused_as_a_duplicate_row():
    """Page one's rows served again at offset 100, with the offset echo fixed.

    Every per-page check then passes -- the offset, the page size, the query,
    the resource, the total and the row count are all correct -- and the
    capture is still refused, because a row reachable twice would become two
    candidates for one vehicle: an ambiguity manufactured by the pagination
    rather than stated by the register.
    """
    transport = FixtureTransport(bodies={100: page_with(0, offset=100)})
    refuses("GOV_RECORD_ID_DUPLICATED", client(transport=transport).capture_resource,
            src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY))


def test_a_missing_page_cannot_be_hidden_by_a_page_that_reports_the_full_total():
    """The R5 lesson, as a property of the client.

    Two of three pages hold 200 rows and BOTH honestly report 233, so a
    prefix looks exactly like a complete query from inside any single page.
    Here the third page is replaced by an empty one and the capture is refused
    rather than answering from 200 of 233 rows.
    """
    transport = FixtureTransport(bodies={200: page_with(200, records=[])})
    refuses("GOV_PAGE_COUNT_UNEXPECTED", client(transport=transport).capture_resource,
            src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY))


def test_an_unsuccessful_envelope_is_never_read_as_data():
    document = page_document(0)
    document["success"] = False
    transport = FixtureTransport(bodies={0: encode(document)})
    refuses("GOV_ENVELOPE_UNSUCCESSFUL", client(transport=transport).capture_resource,
            src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY))


def test_a_query_parameter_nobody_sent_is_refused_in_both_directions():
    """A page that applied a filter nobody asked for is not this query's page."""
    transport = FixtureTransport(bodies={0: page_with(0, filters={"tozar": "טויוטה"})})
    refuses("GOV_QUERY_ECHO_MISMATCH", client(transport=transport).capture_resource,
            src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY))


def test_paging_parameters_can_never_come_from_a_caller():
    """`limit` and `offset` are the client's, so the boundary arithmetic the
    completeness gate depends on cannot be supplied from outside."""
    for rejected in ({"limit": "10"}, {"offset": "5"}, {"sort": "_id"}):
        refuses("GOV_QUERY_ECHO_MISMATCH", client().capture_resource,
                src.WLTP_RESOURCE_ID, query=rejected)


def test_only_allowlisted_actions_and_resources_exist():
    refuses("GOV_ACTION_NOT_ALLOWED", src.action_url, "datastore_delete")
    refuses("GOV_RESOURCE_NOT_ALLOWED", src.require_allowed_resource, str(uuid4()))
    assert src.action_url(src.PACKAGE_SHOW).startswith("https://data.gov.il/api/3/action/")
    assert src.ALLOWED_RESOURCE_IDS == {src.WLTP_RESOURCE_ID, src.QUANTITY_RESOURCE_ID}


def test_a_response_from_an_unapproved_host_is_refused_and_not_retried():
    transport = FixtureTransport(final_url="https://data.gov.il.evil.test/api/3/action/package_show")
    refuses("GOV_REDIRECTED_OFF_HOST", client(transport=transport).capture_resource,
            src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY))
    assert len(transport.calls) == 1


def test_a_non_json_response_is_refused_before_it_is_parsed():
    transport = FixtureTransport(content_type="text/html")
    refuses("GOV_RESPONSE_NOT_JSON", client(transport=transport).capture_resource,
            src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY))


# =============================================================================
# 4. one explicit, fail-closed `_id` policy
# =============================================================================

@pytest.mark.parametrize("identity", [None, "36327", 36327.5, True, [], {}],
                         ids=["null", "string", "float", "boolean", "array", "object"])
def test_a_row_without_an_integer_id_fails_the_whole_capture(identity):
    """ONE policy, no variants: `_id` is a JSON integer or the capture stops.

    A digit STRING is refused too. A row with no usable register identity
    cannot be stored idempotently or pointed back at the register, so there is
    no shape in which it is quietly kept.
    """
    document = page_document(0)
    document["result"]["records"][0]["_id"] = identity
    transport = FixtureTransport(bodies={0: encode(document)})
    refuses("GOV_RECORD_ID_INVALID", client(transport=transport).capture_resource,
            src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY))


def test_a_row_with_no_id_key_at_all_is_refused_the_same_way():
    document = page_document(0)
    document["result"]["records"][0].pop("_id")
    transport = FixtureTransport(bodies={0: encode(document)})
    refuses("GOV_RECORD_ID_INVALID", client(transport=transport).capture_resource,
            src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY))


def test_a_row_that_is_not_an_object_is_refused():
    document = page_document(0)
    document["result"]["records"][0] = ["not", "an", "object"]
    transport = FixtureTransport(bodies={0: encode(document)})
    refuses("GOV_RECORD_SHAPE_INVALID", client(transport=transport).capture_resource,
            src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY))


# =============================================================================
# 5 & 6. the retry boundary is finite, and deterministic failures never reach it
# =============================================================================

def test_the_bounds_actually_reach_the_transport_and_are_finite():
    transport = FixtureTransport()
    client(transport=transport).package_show(src.WLTP_RESOURCE_ID)
    connect, read, max_bytes = transport.bounds[0]
    assert 0 < connect == src.CONNECT_TIMEOUT_SECONDS < float("inf")
    assert 0 < read == src.READ_TIMEOUT_SECONDS < float("inf")
    assert 0 < max_bytes == src.MAX_RESPONSE_BYTES


def test_a_network_failure_is_retried_a_finite_number_of_times():
    slept: list[float] = []
    transport = FixtureTransport(transport_failures=99)
    refuses("GOV_TRANSPORT_FAILED",
            client(transport=transport, sleep_fn=slept.append).capture_resource,
            src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY))
    assert len(transport.calls) == src.MAX_ATTEMPTS_PER_REQUEST == 3
    # Backoff is fixed, finite and in order; the last attempt does not sleep.
    assert slept == list(src.RETRY_BACKOFF_SECONDS)
    assert all(0 < value < float("inf") for value in slept)


def test_a_transient_failure_that_clears_is_retried_and_then_succeeds():
    transport = FixtureTransport(transport_failures=2)
    metadata = client(transport=transport).package_show(src.WLTP_RESOURCE_ID)
    assert metadata.resource_id == src.WLTP_RESOURCE_ID
    assert len(transport.calls) == 3


@pytest.mark.parametrize("status", sorted(src.RETRYABLE_STATUS_CODES))
def test_429_and_a_transient_5xx_are_retried_to_the_same_finite_bound(status):
    transport = FixtureTransport(statuses=[status] * 9)
    refuses("GOV_HTTP_STATUS_UNEXPECTED", client(transport=transport).package_show,
            src.WLTP_RESOURCE_ID)
    assert len(transport.calls) == src.MAX_ATTEMPTS_PER_REQUEST


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 501])
def test_a_non_transient_status_is_not_retried(status):
    transport = FixtureTransport(statuses=[status] * 9)
    refuses("GOV_HTTP_STATUS_UNEXPECTED", client(transport=transport).package_show,
            src.WLTP_RESOURCE_ID)
    assert len(transport.calls) == 1


@pytest.mark.parametrize("label,transport_kwargs,reason", [
    ("a schema failure", {"bodies": {0: page_with(0, fields=[])}}, "GOV_SCHEMA_INVALID"),
    ("an identity failure", {"bodies": {0: page_with(0, resource_id="other")}},
     "GOV_RESOURCE_ECHO_MISMATCH"),
    ("a validation failure", {"bodies": {0: page_with(0, total=-1)}}, "GOV_TOTAL_INVALID"),
], ids=lambda value: value if isinstance(value, str) else "")
def test_a_deterministic_failure_is_never_retried(label, transport_kwargs, reason):
    """Structural, not a matter of discipline.

    `_request` returns only after the transport, the status, the host, the
    media type, the size and the CKAN envelope have passed, so every schema,
    identity and pagination rule is applied by its CALLER -- outside the retry
    loop, where it cannot be retried even by mistake.
    """
    transport = FixtureTransport(**transport_kwargs)
    refuses(reason, client(transport=transport).capture_resource,
            src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY))
    # One request for `package_show`, one for the page that failed.
    assert len(transport.calls) == 2


def test_cancellation_stops_a_capture_between_pages(repository):
    """A capture that is cancelled writes nothing, because nothing is written
    until the whole query has been validated."""
    seen: list[int] = []

    def cancelled() -> bool:
        seen.append(1)
        return len(seen) > 3

    with pytest.raises(CancellationRequested):
        client(cancellation_checker=cancelled).capture_resource(
            src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY))
    assert repository.catalog_snapshots == {}


# =============================================================================
# 7. over-limit responses fail BEFORE any persistence
# =============================================================================

@pytest.mark.parametrize("label,kwargs,reason", [
    ("more pages than the bound", {"max_pages": 2}, "GOV_PAGE_BUDGET_EXCEEDED"),
    ("more records than the bound", {"max_records": 100}, "GOV_RECORD_BUDGET_EXCEEDED"),
    ("a row larger than the durable bound", {"max_record_chars": 64}, "GOV_PAYLOAD_TOO_LARGE"),
], ids=lambda value: value if isinstance(value, str) else "")
def test_an_over_limit_capture_fails_before_a_single_durable_write(label, kwargs, reason,
                                                                   repository):
    lease = leased_run(repository)
    with pytest.raises(GovernmentSourceError) as failure:
        ingest(repository, lease, **kwargs)
    assert failure.value.reason_code == reason
    assert repository.catalog_snapshots == {}
    assert repository.catalog_raw_records == {}
    assert repository.catalog_candidates == {}


def test_a_truncated_response_is_refused_rather_than_parsed_as_a_prefix():
    transport = FixtureTransport(truncated=True)
    refuses("GOV_RESPONSE_TOO_LARGE", client(transport=transport).package_show,
            src.WLTP_RESOURCE_ID)


def test_the_retrieval_metadata_stays_inside_the_durable_bound():
    """Checked HERE, so an over-long metadata object is a local refusal rather
    than a database error halfway through an ingestion."""
    capture = client().capture_resource(src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY))
    metadata = snapshot_module.retrieval_metadata(capture)
    rendered = json.dumps(metadata, separators=(",", ":"), ensure_ascii=False)
    assert len(rendered) <= src.MAX_RETRIEVAL_METADATA_CHARS
    assert metadata["page_checksums_inline"] is True
    assert [entry["sha256"] for entry in metadata["page_checksums"]] == \
        [page.body_sha256 for page in capture.pages]
    # And the inline list is dropped -- never truncated -- once a capture has
    # more pages than fit, while the chain still commits to every checksum.
    many = copy.copy(capture)
    object.__setattr__(many, "pages", capture.pages * 9)
    wide = snapshot_module.retrieval_metadata(many)
    assert wide["page_checksums_inline"] is False and "page_checksums" not in wide
    assert len(json.dumps(wide, separators=(",", ":"), ensure_ascii=False)) \
        <= src.MAX_RETRIEVAL_METADATA_CHARS
    assert wide["page_chain_sha256"] != metadata["page_chain_sha256"]


def test_the_inline_page_checksum_limit_still_fits_the_durable_bound():
    """The worst inline case, computed rather than assumed."""
    capture = client().capture_resource(src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY))
    widest = copy.copy(capture)
    pages = (capture.pages * (src.MAX_INLINE_PAGE_CHECKSUMS // len(capture.pages) + 1))
    object.__setattr__(widest, "pages", pages[:src.MAX_INLINE_PAGE_CHECKSUMS])
    metadata = snapshot_module.retrieval_metadata(widest)
    assert metadata["page_checksums_inline"] is True
    assert len(metadata["page_checksums"]) == src.MAX_INLINE_PAGE_CHECKSUMS
    assert len(json.dumps(metadata, separators=(",", ":"), ensure_ascii=False)) \
        <= src.MAX_RETRIEVAL_METADATA_CHARS


# =============================================================================
# 8. a partial snapshot cannot activate, and cancellation leaves none
# =============================================================================

def test_a_partial_snapshot_cannot_activate(repository):
    """The completeness gate is the repository's, not the ingestor's."""
    lease = leased_run(repository)
    capture = client().capture_resource(src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY))
    snapshot = repository.record_catalog_snapshot(
        lease.run_id, snapshot_module.snapshot_payload(capture),
        worker_id=lease.worker_id, attempt=lease.attempt, lease_token=lease.lease_token)
    payload, _ = next(iter(snapshot_module.raw_record_payloads(capture, snapshot)))
    repository.record_catalog_raw_record(lease.run_id, payload, worker_id=lease.worker_id,
                                         attempt=lease.attempt, lease_token=lease.lease_token)
    with pytest.raises(AppError) as failure:
        repository.activate_catalog_snapshot(
            lease.run_id, {"snapshot_id": snapshot["id"]}, worker_id=lease.worker_id,
            attempt=lease.attempt, lease_token=lease.lease_token)
    assert failure.value.code == "CATALOG_SNAPSHOT_INCOMPLETE"
    assert repository.catalog_snapshots[snapshot["snapshot_key"]]["activated_at"] is None


def test_a_cancelled_ingestion_leaves_no_active_snapshot(repository):
    lease = leased_run(repository)
    written: list[int] = []

    def cancelled() -> bool:
        written.append(1)
        return len(written) > 30

    ingestor = GovernmentCatalogIngestor(repository, lease, client=client(),
                                         cancellation_checker=cancelled)
    with pytest.raises(CancellationRequested):
        ingestor.ingest_resource(src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY))
    snapshots = list(repository.catalog_snapshots.values())
    assert len(snapshots) == 1
    assert snapshots[0]["activated_at"] is None
    assert snapshots[0]["validation_state"] == "pending"
    assert 0 < snapshots[0]["stored_record_count"] < PINNED_TOTAL
    # And nothing downstream can see a non-active snapshot.
    assert repository.list_active_catalog_snapshots(src.GOVERNMENT_SOURCE_FAMILY) == []
    with pytest.raises(GovernmentProjectionError) as refusal:
        GovernmentCatalogProjection(repository).dataset_metadata()
    assert refusal.value.reason_code == "GOV_PROJECTION_NO_ACTIVE_SNAPSHOT"


def test_a_snapshot_that_cannot_activate_is_terminated_as_failed(repository, monkeypatch):
    """A validation failure terminates the capture rather than parking it.

    Simulated at the only place it can honestly come from: a snapshot whose
    declared total is one more than the capture can ever supply. The
    completeness gate then refuses activation, and the ingestor records that
    refusal AS a failure -- `failed` is terminal in the schema, so a capture
    found unusable can never become complete afterwards by being written to
    again.
    """
    lease = leased_run(repository)
    real = repository.record_catalog_snapshot

    def inflate(run_id, payload, **kwargs):
        row = real(run_id, payload, **kwargs)
        repository.catalog_snapshots[row["snapshot_key"]]["declared_record_count"] += 1
        return row

    monkeypatch.setattr(repository, "record_catalog_snapshot", inflate)
    ingestor = GovernmentCatalogIngestor(repository, lease, client=client())
    with pytest.raises(GovernmentIngestionError) as failure:
        ingestor.ingest_resource(src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY))
    assert failure.value.reason_code == "GOV_SNAPSHOT_NOT_ACTIVATED"
    snapshot = next(iter(repository.catalog_snapshots.values()))
    assert snapshot["validation_state"] == "failed"
    assert snapshot["activated_at"] is None


# =============================================================================
# 9. every durable write requires the EXACT active lease
# =============================================================================

def test_every_catalog_write_requires_the_exact_active_lease(repository):
    lease = leased_run(repository)
    capture = client().capture_resource(src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY))
    payload = snapshot_module.snapshot_payload(capture)
    good = {"worker_id": lease.worker_id, "attempt": lease.attempt,
            "lease_token": lease.lease_token}
    for wrong in ({"worker_id": "another-worker"}, {"attempt": lease.attempt + 1},
                  {"lease_token": "not-the-token"}):
        with pytest.raises(AppError) as failure:
            repository.record_catalog_snapshot(lease.run_id, payload, **{**good, **wrong})
        assert failure.value.code == "RUN_TRANSITION_CONFLICT"
    assert repository.catalog_snapshots == {}

    snapshot = repository.record_catalog_snapshot(lease.run_id, payload, **good)
    record_payload, record = next(iter(snapshot_module.raw_record_payloads(capture, snapshot)))
    for wrong in ({"worker_id": "another-worker"}, {"attempt": lease.attempt + 1},
                  {"lease_token": "not-the-token"}):
        with pytest.raises(AppError):
            repository.record_catalog_raw_record(lease.run_id, record_payload,
                                                 **{**good, **wrong})
    assert repository.catalog_raw_records == {}

    row = repository.record_catalog_raw_record(lease.run_id, record_payload, **good)
    candidate = read_wltp_record(record).candidate_payload(row)
    for wrong in ({"worker_id": "another-worker"}, {"attempt": lease.attempt + 1},
                  {"lease_token": "not-the-token"}):
        with pytest.raises(AppError):
            repository.record_catalog_candidate(lease.run_id, candidate, **{**good, **wrong})
    assert repository.catalog_candidates == {}
    for wrong in ({"worker_id": "another-worker"}, {"attempt": lease.attempt + 1},
                  {"lease_token": "not-the-token"}):
        with pytest.raises(AppError):
            repository.activate_catalog_snapshot(lease.run_id, {"snapshot_id": snapshot["id"]},
                                                 **{**good, **wrong})
    assert snapshot["activated_at"] is None


def test_a_superseded_lease_cannot_finish_a_capture(repository):
    """A worker that lost its lease writes nothing, mid-ingestion or not."""
    lease = leased_run(repository)
    capture = client().capture_resource(src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY))
    snapshot = repository.record_catalog_snapshot(
        lease.run_id, snapshot_module.snapshot_payload(capture), worker_id=lease.worker_id,
        attempt=lease.attempt, lease_token=lease.lease_token)
    repository.runs[str(lease.run_id)]["lease_expires_at"] = "2000-01-01T00:00:00+00:00"
    superseded = repository.claim_run(lease.run_id, "worker-2")   # the lease moves on
    assert superseded["attempt"] > lease.attempt
    payload, _ = next(iter(snapshot_module.raw_record_payloads(capture, snapshot)))
    with pytest.raises(AppError):
        repository.record_catalog_raw_record(
            lease.run_id, payload, worker_id=lease.worker_id, attempt=lease.attempt,
            lease_token=lease.lease_token)
    assert repository.catalog_raw_records == {}


# =============================================================================
# 10 & 11. replay, refresh and history
# =============================================================================

def test_exact_replay_creates_no_duplicate_snapshot_row_or_candidate(repository, ingested):
    lease, first = ingested
    assert (len(repository.catalog_snapshots), len(repository.catalog_raw_records),
            len(repository.catalog_candidates)) == (1, PINNED_TOTAL, PINNED_TOTAL)
    second = ingest(repository, lease)
    assert second.snapshot_key == first.snapshot_key
    assert second.content_sha256 == first.content_sha256
    assert (len(repository.catalog_snapshots), len(repository.catalog_raw_records),
            len(repository.catalog_candidates)) == (1, PINNED_TOTAL, PINNED_TOTAL)
    # A no-change refresh is a no-op, not a second ingestion.
    assert second.candidate_count == 0 and second.activated is True


def test_a_later_run_reuses_an_already_active_identical_snapshot(repository, ingested):
    """Discovered and left alone: a later run never mutates another run's capture."""
    first_lease, first = ingested
    second_lease = leased_run(repository, worker="worker-2")
    report = ingest(repository, second_lease)
    assert report.reused_existing is True
    assert report.snapshot_key == first.snapshot_key
    assert report.created_by_run_id == str(first_lease.run_id)
    assert (len(repository.catalog_snapshots), len(repository.catalog_raw_records),
            len(repository.catalog_candidates)) == (1, PINNED_TOTAL, PINNED_TOTAL)


def test_a_later_run_cannot_adopt_another_runs_unfinished_capture(repository):
    first = leased_run(repository)
    capture = client().capture_resource(src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY))
    repository.record_catalog_snapshot(
        first.run_id, snapshot_module.snapshot_payload(capture), worker_id=first.worker_id,
        attempt=first.attempt, lease_token=first.lease_token)
    second = leased_run(repository, worker="worker-2")
    with pytest.raises(GovernmentIngestionError) as failure:
        ingest(repository, second)
    assert failure.value.reason_code == "GOV_SNAPSHOT_OWNED_BY_ANOTHER_RUN"
    assert repository.catalog_raw_records == {}


def test_changed_content_creates_a_distinct_snapshot_and_preserves_history(repository, ingested):
    """One changed byte of source content is a different snapshot identity.

    The previous snapshot stays active with every row it captured: history is
    preserved rather than replaced, which is what makes a rollback a pointer
    change instead of a re-fetch.
    """
    lease, first = ingested
    document = page_document(0)
    document["result"]["records"][0]["koah_sus"] = 999      # a real field, one value changed
    transport = FixtureTransport(bodies={0: encode(document)})
    second = ingest(repository, lease, transport=transport)

    assert second.snapshot_key != first.snapshot_key
    assert second.content_sha256 != first.content_sha256
    assert second.upstream_version == first.upstream_version    # same upstream version
    assert len(repository.catalog_snapshots) == 2
    assert len(repository.catalog_raw_records) == 2 * PINNED_TOTAL
    previous = repository.catalog_snapshots[first.snapshot_key]
    assert previous["activated_at"] is not None
    assert previous["stored_record_count"] == PINNED_TOTAL


def test_a_failed_refresh_does_not_replace_the_last_valid_active_snapshot(repository, ingested):
    lease, first = ingested
    transport = FixtureTransport(bodies={200: page_with(200, records=[])})
    with pytest.raises(GovernmentSourceError):
        ingest(repository, lease, transport=transport)
    active = repository.list_active_catalog_snapshots(src.GOVERNMENT_SOURCE_FAMILY)
    assert [row["snapshot_key"] for row in active] == [first.snapshot_key]
    assert len(repository.catalog_snapshots) == 1


def test_the_same_record_cannot_be_stored_twice_under_a_different_key(repository, ingested):
    """The identity is DERIVED, so a caller cannot rename a row into a duplicate."""
    lease, report = ingested
    snapshot = repository.catalog_snapshots[report.snapshot_key]
    capture = client().capture_resource(src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY))
    payload, _ = next(iter(snapshot_module.raw_record_payloads(capture, snapshot)))
    from backend.catalog.keys import CatalogKeyError

    with pytest.raises(CatalogKeyError):
        repository.record_catalog_raw_record(
            lease.run_id, {**payload, "record_key": "cr1." + "0" * 32},
            worker_id=lease.worker_id, attempt=lease.attempt, lease_token=lease.lease_token)
    assert len(repository.catalog_raw_records) == PINNED_TOTAL


def test_a_record_states_the_exact_page_and_index_it_was_captured_at(repository, ingested):
    """Cross-checked against R5's own manifest, which recorded index 74 for
    `_id` 36327 on page one when the capture archive was imported."""
    _, report = ingested
    snapshot = repository.catalog_snapshots[report.snapshot_key]
    row = next(item for item in repository.catalog_raw_records.values()
               if item["upstream_record_id"] == str(PINNED_RECORD_ID))
    assert row["source_locator"] == {"page_number": 1, "page_offset": 0,
                                     "page_index": 74, "capture_index": 74}
    assert stated_source_locator(row["source_locator"]) == row["source_locator"]
    positions = [item["source_locator"]["capture_index"]
                 for item in repository.catalog_raw_records.values()
                 if item["snapshot_id"] == snapshot["id"]]
    assert sorted(positions) == list(range(PINNED_TOTAL))    # one row per position


def test_the_stored_payload_is_the_register_row_verbatim(repository, ingested):
    """Unknown fields are preserved: a row edited on the way in is no longer
    the row the register published."""
    _, _report = ingested
    row = next(item for item in repository.catalog_raw_records.values()
               if item["upstream_record_id"] == str(PINNED_RECORD_ID))
    original = next(record for record in page_document(0)["result"]["records"]
                    if record["_id"] == PINNED_RECORD_ID)
    assert row["payload"] == original
    # Including the fields this catalog deliberately never reads.
    assert {"koah_sus", "dg_metach_solela", "mishkal_kolel", "rank"} <= set(row["payload"])


# =============================================================================
# 12, 13 & 14. normalization
# =============================================================================

def wltp_record(**changes):
    record = copy.deepcopy(next(item for item in page_document(0)["result"]["records"]
                                if item["_id"] == PINNED_RECORD_ID))
    record.update(changes)
    return record


def test_normalization_is_deterministic_under_input_and_key_order(repository):
    """A reading is a function of the row's VALUES only."""
    record = wltp_record()
    shuffled = {key: record[key] for key in sorted(record, reverse=True)}
    assert read_wltp_record(shuffled) == read_wltp_record(record)
    # And over the whole capture, twice, in both page orders.
    capture = client().capture_resource(src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY))
    forward = [read_wltp_record(row) for _, row in capture.located_records()]
    backward = [read_wltp_record(row) for _, row in reversed(list(capture.located_records()))]
    assert forward == list(reversed(backward))


def test_the_register_row_reads_exactly_as_r5_established_it():
    reading = read_wltp_record(wltp_record())
    assert reading.manufacturer == "טויוטה"        # the register's own marque, untranslated
    assert reading.commercial_model == "RAV4"
    assert (reading.model_year_start, reading.model_year_end) == (2021, 2021)
    assert reading.official_model_code == "AXAP54L ANXMBK"
    assert reading.trim == "PRIME AWD SE"
    assert reading.identity_dimensions == {"fuel_type": "plug_in_hybrid",
                                           "propulsion_technology": "plug_in",
                                           "drivetrain": "awd", "body_style": "suv"}
    assert reading.engine_displacement_cc == 2487
    assert reading.status == "candidate" and reading.unresolved_dimensions == ()


def test_a_code_label_contradiction_fails_closed_rather_than_choosing_a_side():
    """A known code travelling with a label it is not paired with means the
    register and this reading disagree about what the code MEANS."""
    for changes in ({"delek_nm": "בנזין"},                       # code 7, petrol's label
                    {"hanaa_nm": "4X2"},                          # code 3, 4X2's label
                    {"technologiat_hanaa_nm": "היברידי רגיל"}):   # code 2, hybrid's label
        with pytest.raises(GovernmentNormalizationError) as failure:
            read_wltp_record(wltp_record(**changes))
        assert failure.value.reason_code == "GOV_NORM_LABEL_CONTRADICTION"


def test_an_unknown_identity_code_produces_an_explicitly_ambiguous_candidate():
    """The register stated something this vocabulary could not read.

    The dimension is left UNSTATED -- guessing is what the code-first rule
    exists to prevent -- and the candidate says so, rather than looking like a
    row that simply had no drivetrain.
    """
    reading = read_wltp_record(wltp_record(hanaa_cd=99, hanaa_nm="משהו אחר"))
    assert reading.status == "ambiguous"
    assert reading.unresolved_dimensions == ("drivetrain",)
    assert "drivetrain" not in reading.identity_dimensions


def test_the_registers_own_unknown_marker_is_an_absence_not_an_ambiguity():
    """`לא ידוע קוד` is the register saying it has nothing to state.

    Nothing was left unread, so the reading is complete and the dimension is
    simply an absent key -- never `''` and never `"unknown"`.
    """
    reading = read_wltp_record(wltp_record(hanaa_cd=None, hanaa_nm="לא ידוע קוד"))
    assert reading.status == "candidate" and reading.unresolved_dimensions == ()
    assert "drivetrain" not in reading.identity_dimensions
    assert vocab.is_declared_unknown("לא ידוע קוד 0")
    assert not vocab.is_declared_unknown("לא ידוע קוד בכלל")


def test_an_uncoded_propulsion_name_is_read_only_from_the_closed_table():
    """21 captured rows state `הנעה רגילה` with NO code at all."""
    conventional = read_wltp_record(wltp_record(
        delek_cd=1, delek_nm="בנזין", technologiat_hanaa_cd=None,
        technologiat_hanaa_nm="הנעה רגילה"))
    assert conventional.identity_dimensions["propulsion_technology"] == "conventional"
    assert conventional.status == "candidate"
    # Any other uncoded name is left unresolved rather than interpreted.
    unknown = read_wltp_record(wltp_record(technologiat_hanaa_cd=None,
                                           technologiat_hanaa_nm="הנעה חדשה"))
    assert unknown.status == "ambiguous"
    assert "propulsion_technology" in unknown.unresolved_dimensions


def test_a_fuel_and_propulsion_that_cannot_both_hold_is_a_refusal():
    with pytest.raises(GovernmentNormalizationError) as failure:
        read_wltp_record(wltp_record(delek_cd=1, delek_nm="בנזין",
                                     technologiat_hanaa_cd=2, technologiat_hanaa_nm="PLUG IN"))
    assert failure.value.reason_code == "GOV_NORM_FUEL_PROPULSION_CONTRADICTION"


def test_koah_sus_is_never_read_as_horsepower():
    """Its semantics are unresolved IN THE SOURCE, so it is not evidence."""
    reading = read_wltp_record(wltp_record(koah_sus=306))
    assert "horsepower_hp" not in reading.identity_dimensions
    assert not any("horsepower" in str(value) for value in reading.identity_dimensions.values())
    unmapped = dict(normalize.UNMAPPED_FIELDS)
    assert "koah_sus" in unmapped and "unresolved in the source" in unmapped["koah_sus"]
    for field in ("dg_metach_solela", "mishkal_kolel", "automatic_ind", "sug_degem"):
        assert field in unmapped


def test_no_row_level_market_field_is_ever_invented():
    """Israeli scope belongs to the SOURCE, and is recorded once on it."""
    reading = read_wltp_record(wltp_record())
    assert "market" not in reading.identity_dimensions
    capture = client().capture_resource(src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY))
    metadata = snapshot_module.retrieval_metadata(capture)
    assert metadata["dataset_market_scope"] == src.GOVERNMENT_DATASET_MARKET == "IL"
    assert all("market" not in record for _, record in capture.located_records())


@pytest.mark.parametrize("changes,reason", [
    ({"tozar": "   "}, "GOV_NORM_MANUFACTURER_MISSING"),
    ({"kinuy_mishari": None}, "GOV_NORM_MODEL_MISSING"),
    ({"shnat_yitzur": "2021"}, "GOV_NORM_MODEL_YEAR_INVALID"),
    ({"shnat_yitzur": 1800}, "GOV_NORM_MODEL_YEAR_INVALID"),
    ({"kinuy_mishari": "X" * 201}, "GOV_NORM_IDENTITY_TOO_LONG"),
])
def test_an_unusable_identity_is_refused_with_a_static_reason(changes, reason):
    with pytest.raises(GovernmentNormalizationError) as failure:
        read_wltp_record(wltp_record(**changes))
    assert failure.value.reason_code == reason


def test_the_quantity_resource_has_no_guessed_normalization():
    """Allowlisted for CAPTURE, deliberately unread for IDENTITY."""
    with pytest.raises(GovernmentNormalizationError) as failure:
        read_wltp_record(wltp_record(), resource_id=src.QUANTITY_RESOURCE_ID)
    assert failure.value.reason_code == "GOV_NORM_RESOURCE_UNSUPPORTED"


def test_the_pr2_vocabulary_is_the_one_r5_reads():
    """ONE definition of what a register code means.

    R5 selects the subset it reviewed from this module, so a change to the
    MEANING of a shared code breaks the R5 proof immediately instead of
    quietly.
    """
    from backend.testing.r5_proof import government as r5

    for code, pairing in r5.FUEL_BY_CODE.items():
        assert vocab.FUEL_BY_CODE[code] == pairing
    for code, pairing in r5.PROPULSION_BY_CODE.items():
        assert vocab.PROPULSION_BY_CODE[code] == pairing
    for code, pairing in r5.DRIVETRAIN_BY_CODE.items():
        assert vocab.DRIVETRAIN_BY_CODE[code] == pairing
    assert dict(r5.BODY_STYLE_BY_MERKAV).items() <= dict(vocab.BODY_STYLE_BY_MERKAV).items()
    assert r5.CONSISTENT_FUEL_PROPULSION is vocab.CONSISTENT_FUEL_PROPULSION
    assert r5.UPSTREAM_FIELDS is vocab.GOVERNMENT_IDENTITY_FIELDS
    assert dict(r5.UNMAPPED_FIELDS).items() <= dict(vocab.UNMAPPED_FIELD_REASONS).items()


def test_similarity_never_merges_two_commercial_models(repository, ingested):
    """The register spells thirteen commercial models in this one query."""
    _, _report = ingested
    models = {row["commercial_model"] for row in repository.catalog_candidates.values()}
    assert {"RAV4", "RAV4 HYBRID", "RAV4 PLUG-IN", "RAV4 PHEV", "TOYOTA RAV4"} <= models
    assert len(models) == 13


def test_a_model_year_with_several_trims_stays_several_candidates(repository, ingested):
    """Never the first row, and never one merged identity."""
    _, _report = ingested
    rows = [row for row in repository.catalog_candidates.values()
            if row["commercial_model"] == "RAV4" and row["model_year_start"] == 2021]
    assert len(rows) == 2
    assert {row["trim"] for row in rows} == {"PRIME AWD SE", "XLE HYBRID"}
    assert len({row["raw_record_id"] for row in rows}) == 2


def test_the_whole_captured_query_normalizes_without_a_silent_drop(repository, ingested):
    _, report = ingested
    assert report.candidate_count == PINNED_TOTAL
    assert report.rejected_record_count == 0
    assert report.candidate_status_counts == {"candidate": PINNED_TOTAL}
    assert report.stored_record_count == report.declared_record_count == PINNED_TOTAL


def test_an_unreadable_row_is_reported_and_its_raw_record_is_still_durable(repository):
    """A refusal loses nothing: the row is durable either way, and the reason
    travels in the report rather than being inferable from a missing candidate."""
    lease = leased_run(repository)
    document = page_document(0)
    document["result"]["records"][0]["delek_nm"] = "לא נכון"       # a real contradiction
    broken_id = str(document["result"]["records"][0]["_id"])
    report = ingest(repository, lease,
                    transport=FixtureTransport(bodies={0: encode(document)}))
    assert report.stored_record_count == PINNED_TOTAL
    assert report.candidate_count == PINNED_TOTAL - 1
    assert report.rejected_records == ((broken_id, "GOV_NORM_LABEL_CONTRADICTION"),)
    assert any(row["upstream_record_id"] == broken_id
               for row in repository.catalog_raw_records.values())
    assert report.activated is True


# =============================================================================
# 15, 16. the internal projection
# =============================================================================

def test_every_projected_candidate_traces_to_its_row_resource_and_version(repository, ingested):
    _, report = ingested
    view = GovernmentCatalogProjection(repository)
    page = view.list_variants("טויוטה", "RAV4", model_year=2021)
    assert page.total == 2
    for variant in page.items:
        assert variant.upstream_record_id.isdigit()
        assert variant.resource_id == src.WLTP_RESOURCE_ID
        assert variant.upstream_version == report.upstream_version
        assert variant.upstream_version_kind == "dataset_version"
        assert variant.snapshot_key == report.snapshot_key
        assert set(variant.source_locator) == {"page_number", "page_offset", "page_index",
                                               "capture_index"}
        record = view.get_record(variant.upstream_record_id)
        assert record.raw_record_id == variant.raw_record_id
        assert record.payload["_id"] == int(variant.upstream_record_id)


def test_the_projection_states_the_dataset_and_the_snapshot_it_answers_from(repository, ingested):
    _, report = ingested
    metadata = GovernmentCatalogProjection(repository).dataset_metadata()
    assert metadata.snapshot_key == report.snapshot_key
    assert metadata.source_family == "government" and metadata.trust_state == "evidence"
    assert metadata.publisher == src.GOVERNMENT_PUBLISHER
    assert metadata.package_id == src.CKAN_PACKAGE_ID
    assert metadata.dataset_market_scope == "IL"
    assert metadata.schema_fingerprint == report.schema_fingerprint
    assert metadata.page_count == 3
    assert metadata.declared_record_count == metadata.stored_record_count == PINNED_TOTAL
    assert metadata.query == dict(PINNED_QUERY)


def test_the_same_snapshot_produces_the_same_ordered_government_tree(repository, ingested):
    _, _report = ingested

    def rendered() -> str:
        page = GovernmentCatalogProjection(repository).government_tree()
        return json.dumps([
            {"manufacturer": node["manufacturer"],
             "models": [{"commercial_model": model["commercial_model"],
                         "model_years": [{"model_year": year["model_year"],
                                          "variants": [variant.candidate_key
                                                       for variant in year["variants"]]}
                                         for year in model["model_years"]]}
                        for model in node["models"]]}
            for node in page.items], sort_keys=True, ensure_ascii=False)

    assert rendered() == rendered()
    tree = GovernmentCatalogProjection(repository).government_tree()
    assert tree.total == 1
    models = tree.items[0]["models"]
    assert [model["commercial_model"] for model in models] == sorted(
        model["commercial_model"] for model in models)
    rav4 = next(model for model in models if model["commercial_model"] == "RAV4")
    assert [year["model_year"] for year in rav4["model_years"]] == \
        [2020, 2021, 2022, 2023, 2024, 2025, 2026]


def test_the_projection_paginates_explicitly_and_reports_what_remains(repository, ingested):
    _, _report = ingested
    view = GovernmentCatalogProjection(repository)
    first = view.list_variants("טויוטה", "RAV4", limit=10, offset=0)
    assert len(first.items) == 10 and first.total == 106 and first.has_more
    second = view.list_variants("טויוטה", "RAV4", limit=10, offset=10)
    assert [item.candidate_key for item in first.items] != \
        [item.candidate_key for item in second.items]
    last = view.list_variants("טויוטה", "RAV4", limit=10, offset=100)
    assert len(last.items) == 6 and not last.has_more
    # The page bound is the server's, not the caller's.
    assert view.list_variants("טויוטה", "RAV4", limit=10_000).limit == projection.MAX_RESULT_ITEMS


def test_the_projection_returns_ambiguity_rather_than_choosing(repository, ingested):
    _, _report = ingested
    view = GovernmentCatalogProjection(repository)
    ambiguous = view.resolve_variant("טויוטה", "RAV4", 2021)
    assert ambiguous.ambiguous and ambiguous.variant is None and len(ambiguous.matches) == 2
    resolved = view.resolve_variant("טויוטה", "RAV4", 2021, trim="PRIME AWD SE")
    assert not resolved.ambiguous
    assert resolved.variant.upstream_record_id == str(PINNED_RECORD_ID)
    years = {item.model_year: item for item in
             view.list_model_years("טויוטה", "RAV4").items}
    assert years[2021].variant_count == 2 and not years[2021].resolves_to_one_variant
    assert years[2020].resolves_to_one_variant


def test_an_exact_code_lookup_is_exact(repository, ingested):
    _, _report = ingested
    view = GovernmentCatalogProjection(repository)
    assert [item.upstream_record_id
            for item in view.find_by_model_code("AXAP54L ANXMBK").items] == [str(PINNED_RECORD_ID)]
    assert view.find_by_model_code("AXAP54L").total == 0        # never a prefix
    assert view.find_by_model_code("ANXMBK").total == 0         # never a substring


def test_the_projection_reads_only_active_snapshots(repository, ingested):
    """A newer PENDING capture never displaces the active answer."""
    lease, first = ingested
    capture = client().capture_resource(src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY))
    document = page_document(0)
    document["result"]["records"][0]["koah_sus"] = 7
    pending_capture = client(transport=FixtureTransport(bodies={0: encode(document)})) \
        .capture_resource(src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY))
    repository.record_catalog_snapshot(
        lease.run_id, snapshot_module.snapshot_payload(pending_capture),
        worker_id=lease.worker_id, attempt=lease.attempt, lease_token=lease.lease_token)
    assert len(repository.catalog_snapshots) == 2
    assert GovernmentCatalogProjection(repository).dataset_metadata().snapshot_key == \
        first.snapshot_key
    assert snapshot_module.snapshot_content_sha256(capture) == first.content_sha256


def test_a_snapshot_beyond_the_projection_bound_is_refused_not_truncated(repository, ingested):
    _, _report = ingested
    view = GovernmentCatalogProjection(repository, max_candidates=10)
    with pytest.raises(GovernmentProjectionError) as failure:
        view.dataset_metadata()
    assert failure.value.reason_code == "GOV_PROJECTION_BOUND_EXCEEDED"


# =============================================================================
# 17 & 19. what this PR does NOT connect
# =============================================================================

def test_the_canonical_catalog_stays_empty(repository, ingested):
    """No repository method can write one, so there is no path to try."""
    _, _report = ingested
    assert repository.catalog_models == [] and repository.catalog_model_variants == []
    for name in dir(repository):
        assert "catalog_model" not in name or name in ("catalog_models",
                                                       "catalog_model_variants")


def test_no_claim_verdict_or_evidence_link_is_manufactured_by_ingestion(repository, ingested):
    """PR2 provenance is snapshot -> raw record -> candidate. Evidence mapping
    and verdict-backed promotion are PR3's."""
    _, _report = ingested
    assert repository.tool_rows == []
    assert repository.catalog_evidence_links == {}
    source = (GOVERNMENT_PACKAGE / "ingest.py").read_text(encoding="utf-8")
    for token in ("create_claim", "record_claim_verdict", "link_catalog_candidate_evidence",
                  "create_source", "record_evidence_fragment"):
        assert token not in source


def test_the_production_tool_registry_is_still_empty():
    """No `GovernmentVehicleTool`, no registration, no scope, no grant."""
    assert ToolRegistry().allowed_names == frozenset()
    registry = Path("backend/tools/registry.py").read_text(encoding="utf-8")
    assert "government" not in registry.lower()
    worker = Path("backend/worker/main.py").read_text(encoding="utf-8")
    # The registry is still constructed with no tools at all, and nothing in
    # the worker imports, names or grants this capability. (The worker's own
    # comment mentions Government to say it is NOT registered, which is the
    # statement being preserved rather than a wiring.)
    assert "tools = ToolRegistry()" in worker
    assert "catalog.government" not in worker
    assert "GovernmentVehicleTool" not in worker
    assert "gov_il" not in worker
    # And the projection is deliberately not a Tool: no operations mapping, no
    # schemas, no required scope.
    for attribute in ("operations", "input_schema", "output_schema", "required_scope", "mode"):
        assert not hasattr(GovernmentCatalogProjection, attribute)


def test_nothing_outside_the_package_and_its_tests_imports_it_yet():
    """PR2 builds the capability; PR3 connects it."""
    # `backend/testing/r5_proof/government.py` reads the shared code/label
    # vocabulary, which is the point of moving it: ONE definition of what a
    # register code means, selected down to the subset R5 reviewed.
    allowed = {"backend/catalog/government", "backend/testing/government_capture.py",
               "backend/testing/r5_proof/government.py",
               "tests/test_catalog_government_ingestion.py"}
    for path in sorted(Path("backend").rglob("*.py")):
        text = str(path)
        if any(text.startswith(prefix) for prefix in allowed):
            continue
        assert "catalog.government" not in path.read_text(encoding="utf-8"), text


# =============================================================================
# the three digests are three different things
# =============================================================================

def test_the_snapshot_identity_is_not_a_page_digest_and_not_a_payload_digest(repository,
                                                                            ingested):
    _, report = ingested
    capture = client().capture_resource(src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY))
    page_digests = {page.body_sha256 for page in capture.pages}
    payload_digests = {row["payload_sha256"] for row in repository.catalog_raw_records.values()}
    assert report.content_sha256 not in page_digests
    assert report.content_sha256 not in payload_digests
    assert not page_digests & payload_digests
    # The identity basis is readable, so a reviewer can see WHICH property
    # differs between two captures rather than only that a digest does.
    basis = json.loads(snapshot_module.snapshot_content_basis(capture))
    assert basis["contract"] == "gov.snapshot.1"
    assert [entry["sha256"] for entry in basis["pages"]] == \
        [page.body_sha256 for page in capture.pages]
    assert "retrieved_at" not in basis and "started_at" not in basis


def test_the_page_chain_commits_to_the_order_of_the_pages():
    capture = client().capture_resource(src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY))
    checksums = [page.body_sha256 for page in capture.pages]
    assert snapshot_module.page_chain_digest(checksums) != \
        snapshot_module.page_chain_digest(list(reversed(checksums)))
    assert snapshot_module.page_chain_digest(checksums) == \
        snapshot_module.page_chain_digest(tuple(checksums))
    assert snapshot_module.page_chain_digest(checksums) not in checksums


def test_the_schema_fingerprint_is_a_function_of_the_declared_schema_alone():
    first, third = page_document(0)["result"], page_document(200)["result"]
    assert schema_fingerprint(first) == schema_fingerprint(third)     # 100 rows vs 33
    renamed = copy.deepcopy(first)
    renamed["fields"][5]["id"] = "tozar_renamed"
    assert schema_fingerprint(renamed) != schema_fingerprint(first)
    retyped = copy.deepcopy(first)
    retyped["fields"][5]["type"] = "numeric"
    assert schema_fingerprint(retyped) != schema_fingerprint(first)
    assert schema_fingerprint(first).startswith("gov.schema.1:")


def test_the_caller_never_supplies_a_stored_payload_digest(repository, ingested):
    from backend.catalog.payloads import CatalogPayloadError, prepare_raw_record

    lease, report = ingested
    snapshot = repository.catalog_snapshots[report.snapshot_key]
    with pytest.raises(CatalogPayloadError):
        prepare_raw_record({"snapshot_id": snapshot["id"],
                            "snapshot_key": snapshot["snapshot_key"],
                            "resource_id": snapshot["resource_id"],
                            "upstream_record_id": "1", "payload": {"_id": 1},
                            "payload_sha256": "a" * 64})
    assert "payload_sha256" not in json.dumps(
        [payload for payload, _ in snapshot_module.raw_record_payloads(
            client().capture_resource(src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY)),
            snapshot)][:1])
